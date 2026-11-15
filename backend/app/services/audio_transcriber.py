"""오디오 전사와 실시간 chunk 입력 정규화 서비스.

전사 엔진은 .env로 고른다. 기본은 모두 로컬(Whisper)이고 음성을 밖으로 보내지 않는다.

    STT_LIVE_ENGINE  local | rtzr   통화 중 실시간 조각. rtzr는 통화마다 리턴제로 스트리밍 연결 하나
    STT_FILE_ENGINE  local | rtzr   녹음 파일 분석, 통화 후 전체 재전사. rtzr는 화자 분리(2명) 결과를 준다
    STT_REFINE       off | rtzr     통화 중 지난 구간을 화자 분리와 함께 다시 받아 적기(live_refine, websocket)

rtzr를 골랐는데 RTZR_CLIENT_ID/RTZR_CLIENT_SECRET이 없거나 호출이 실패하면 경고를 남기고 그 요청은 로컬로 받아 적는다.
"""

from __future__ import annotations

import base64
import inspect
import io
import logging
import math
import os
import tempfile
import wave
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import HTTPException

from backend.app.services import rtzr_client


logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_ENGINES = ("local", "rtzr")
# 녹음 파일 리턴제로 전사를 기다리는 상한. 보통 녹음 길이보다 훨씬 빨리 끝나고, 넘기면 로컬로 받아 적는다.
RECORDING_TIMEOUT_S = 600.0


def transcribe_audio_file(audio_path: str, realtime: bool = False) -> list[dict]:
    """오디오 파일을 전사 모듈에 넘겨 발화 segment 목록으로 변환한다."""
    try:
        from backend.app.mp3_json import transcribe_audio
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=501,
            detail="오디오 전사 모듈(backend.app.mp3_json)이 아직 설정되어 있지 않습니다.",
        ) from exc

    kwargs = {"realtime": realtime} if "realtime" in inspect.signature(transcribe_audio).parameters else {}
    return transcribe_audio(audio_path, **kwargs)


def transcribe_audio_bytes(audio_bytes: bytes, audio_format: str) -> list[dict]:
    """메모리로 받은 mp3/wav/m4a chunk를 임시 파일로 저장한 뒤 전사한다."""
    suffix = f".{normalize_audio_format(audio_format)}"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        return transcribe_audio_file(tmp.name, realtime=True)
    finally:
        os.unlink(tmp.name)


# ---------------------------------------------------------------- 엔진 선택


def _engine(name: str) -> str:
    value = os.getenv(name, "local").strip().lower() or "local"
    if value not in _ENGINES:
        logger.warning("%s=%s는 알 수 없는 값이라 local로 받아 적습니다(local 또는 rtzr).", name, value)
        return "local"
    return value


def live_engine() -> str:
    """통화 중 실시간 조각 엔진(STT_LIVE_ENGINE). 기본 local."""
    return _engine("STT_LIVE_ENGINE")


def file_engine() -> str:
    """녹음 파일·통화 후 전체 재전사 엔진(STT_FILE_ENGINE). 기본 local."""
    return _engine("STT_FILE_ENGINE")


def refine_enabled() -> bool:
    """STT_REFINE=rtzr이고 리턴제로 키가 있으면 True.

    정리(refine)에는 로컬 대안을 두지 않는다. 키가 없으면 경고만 남기고 끄며, 화면에는 실시간 조각만 나온다.
    """
    value = os.getenv("STT_REFINE", "off").strip().lower() or "off"
    if value == "off":
        return False
    if value != "rtzr":
        logger.warning("STT_REFINE=%s는 알 수 없는 값이라 정리를 끕니다(off 또는 rtzr).", value)
        return False
    if not rtzr_client.is_configured():
        logger.warning("STT_REFINE=rtzr인데 RTZR_CLIENT_ID/RTZR_CLIENT_SECRET이 없어 통화 중 정리를 끕니다.")
        return False
    return True


def open_live_stream() -> Optional[rtzr_client.StreamSession]:
    """STT_LIVE_ENGINE=rtzr면 통화 하나에 쓸 스트리밍 연결을 만든다(실제 연결은 첫 조각에서 연다). 아니면 None."""
    if live_engine() != "rtzr":
        return None
    if not rtzr_client.is_configured():
        logger.warning("STT_LIVE_ENGINE=rtzr인데 RTZR_CLIENT_ID/RTZR_CLIENT_SECRET이 없어 로컬 Whisper로 받아 적습니다.")
        return None
    return rtzr_client.StreamSession()


def transcribe_live_chunk(
    audio_bytes: bytes,
    audio_format: str,
    samples: Optional[np.ndarray] = None,
    stream: Optional[rtzr_client.StreamSession] = None,
) -> list[dict]:
    """실시간 조각 하나를 받아 적는다. 스트리밍 연결(stream)이 있으면 리턴제로로, 실패하면 이 조각은 로컬 Whisper로 받아 적는다.

    samples는 같은 조각을 16kHz로 푼 것이다(decode_audio_16k). 반환 segment의 start/end는 조각 기준 초다.
    """
    if stream is not None and samples is not None and len(samples):
        try:
            return stream.transcribe(samples)
        except Exception as exc:
            logger.warning("리턴제로 실시간 전사 실패, 이 조각은 로컬 Whisper로 받아 적습니다: %s", exc)
    return transcribe_audio_bytes(audio_bytes, audio_format)


def transcribe_recording(audio_path: str) -> list[dict]:
    """녹음 파일 전체를 받아 적는다(녹음 파일 분석, 통화 후 전체 재전사).

    STT_FILE_ENGINE=rtzr면 리턴제로 파일 전사로 두 사람을 나눠 받아 적고, 발화마다 화자 번호(spk)를 붙인다.
    상대방/나는 화자 번호 단위로 label_diarized_speakers가 정한다. 키가 없거나 호출이 실패하면 경고를 남기고
    로컬 Whisper로 받아 적는다(화자 번호 없음).
    """
    if file_engine() == "rtzr":
        if not rtzr_client.is_configured():
            logger.warning("STT_FILE_ENGINE=rtzr인데 RTZR_CLIENT_ID/RTZR_CLIENT_SECRET이 없어 로컬 Whisper로 받아 적습니다.")
        else:
            try:
                path = Path(audio_path)
                # WAV(통화 후 전체 녹음)는 브라우저가 48kHz로 줄 때가 있어 16kHz mono로 맞춰 올리고,
                # m4a·mp3 녹음 파일은 압축된 원본을 그대로 올린다(16kHz WAV로 풀면 몇 배 커진다).
                if path.suffix.lower() == ".wav":
                    audio, filename = decode_audio_16k(path.read_bytes(), "wav"), "audio.wav"
                else:
                    audio, filename = path.read_bytes(), f"audio{path.suffix.lower()}"
                result = rtzr_client.transcribe_file(
                    audio, spk_count=2, timeout=RECORDING_TIMEOUT_S, filename=filename
                )
                return [
                    {
                        "start": round(utterance["start"], 2),
                        "end": round(utterance["end"], 2),
                        "speaker": "",
                        "spk": utterance["spk"],
                        "text": utterance["text"],
                    }
                    for utterance in result.utterances
                ]
            except Exception as exc:
                logger.warning("리턴제로 파일 전사 실패, 로컬 Whisper로 받아 적습니다: %s", exc)
    return transcribe_audio_file(audio_path)


def label_diarized_speakers(segments: list[dict], roles: list[str]) -> bool:
    """화자 분리 번호(spk)가 있는 segment의 speaker를 화자 단위로 화자B(나)/화자A(상대방)로 정한다.

    roles는 segments와 같은 순서의 상태 추적기 역할이다(user=앱 사용자, suspect=상대방, unknown).
    발화마다 따로 정하지 않고 화자 번호별로 역할을 모아 다수결하므로, 추적기가 한두 발화의 역할을 틀려도
    한 사람의 말이 양쪽 말풍선으로 갈리지 않는다. 나는 한 명만 둔다(두 화자가 모두 user 다수면 user 비율이 높은 쪽).
    역할 근거가 전혀 없으면(추적기를 쓰지 않은 통화) 말을 가장 많이 한 화자를 상대방으로, 다른 화자를 나로 둔다.
    반환: 화자 분리 결과가 있어 라벨을 정했으면 True(Whisper 결과면 False, 아무것도 바꾸지 않는다).
    """
    user, suspect, talk = Counter(), Counter(), Counter()
    for index, segment in enumerate(segments):
        spk = segment.get("spk")
        if spk is None:
            continue
        talk[spk] += len(str(segment.get("text", ""))) or 1
        role = roles[index] if index < len(roles) else "unknown"
        if role == "user":
            user[spk] += 1
        elif role == "suspect":
            suspect[spk] += 1
    if not talk:
        return False

    speakers = sorted(talk)
    if any(user[k] or suspect[k] for k in speakers):
        mine = [k for k in speakers if user[k] > suspect[k]]
        me = max(mine, key=lambda k: (user[k] / (user[k] + suspect[k]), user[k])) if mine else None
    else:
        loudest = max(speakers, key=lambda k: talk[k])
        me = next((k for k in speakers if k != loudest), None)
    for segment in segments:
        if segment.get("spk") is not None:
            segment["speaker"] = "화자B" if segment["spk"] == me else "화자A"
    return True


# ---------------------------------------------------------------- 디코딩


def decode_audio_16k(audio_bytes: bytes, audio_format: str = "wav") -> np.ndarray:
    """오디오 바이트를 16kHz mono float32(-1~1)로 푼다.

    실시간 조각은 대부분 16kHz 16비트 WAV라 표준 라이브러리로 바로 읽는다. 브라우저가 16kHz를 거부하면 48kHz 등
    기본 레이트로 오므로 16kHz로 다시 맞춘다. WAV가 아니거나 읽지 못하면 PyAV(faster-whisper의 decode_audio)로 푼다.
    """
    if audio_format.lower().strip().lstrip(".") == "wav":
        try:
            return _decode_pcm_wav(audio_bytes)
        except (wave.Error, EOFError, ValueError):
            pass
    from faster_whisper.audio import decode_audio

    return decode_audio(io.BytesIO(audio_bytes), sampling_rate=SAMPLE_RATE)


def _decode_pcm_wav(audio_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(audio_bytes), "rb") as reader:
        channels, width, rate = reader.getnchannels(), reader.getsampwidth(), reader.getframerate()
        frames = reader.readframes(reader.getnframes())
    if width != 2:
        raise ValueError("16비트 PCM WAV만 직접 읽는다")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        from scipy.signal import resample_poly

        divisor = math.gcd(rate, SAMPLE_RATE)
        samples = resample_poly(samples, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)
    return samples


def normalize_transcribed_segments(raw_segments: list[dict]) -> list[dict]:
    """전사 모듈별 segment 필드 차이를 API 응답/DB 저장용으로 정규화한다."""
    normalized_segments = []
    for index, segment in enumerate(raw_segments or [], start=1):
        text = str(segment.get("text", segment.get("content", ""))).strip()
        if not text:
            continue

        # 전사 모델의 화자 라벨(A/B)을 프론트 표시용 화자A/화자B로 변환.
        # (프론트는 화자A=상대방/왼쪽, 화자B=나/오른쪽으로 그린다)
        raw_speaker = str(segment.get("speaker", "")).strip().upper()
        speaker = f"화자{raw_speaker}" if raw_speaker in ("A", "B") else "화자A"

        normalized = {
            "chunk_id": index,
            "start_time": segment.get("start", segment.get("start_time")),
            "end_time": segment.get("end", segment.get("end_time")),
            "speaker": speaker,
            "text": text,
        }
        # 리턴제로 화자 분리 번호. 통화 후 화자 단위로 화자A/화자B를 정할 때 쓴다(label_diarized_speakers).
        if segment.get("spk") is not None:
            normalized["spk"] = int(segment["spk"])
        normalized_segments.append(normalized)

    return normalized_segments


def join_transcript_texts(texts: list[str]) -> str:
    """녹음 파일 전체 전사 결과를 화면 표시용 한 문단으로 합친다."""
    return " ".join(text.strip() for text in texts if text and text.strip())


def decode_audio_chunk_payload(payload: dict) -> bytes:
    """base64 JSON 방식으로 전달된 오디오 chunk를 디코딩한다."""
    encoded_audio = str(payload.get("audio_base64", "")).strip()
    if not encoded_audio:
        raise ValueError("audio_chunk 메시지에는 audio_base64가 필요합니다.")

    try:
        return base64.b64decode(encoded_audio, validate=True)
    except Exception as exc:
        raise ValueError("audio_base64를 디코딩할 수 없습니다.") from exc


def normalize_audio_format(audio_format: str) -> str:
    """프론트에서 넘긴 오디오 포맷명을 mp3, wav, m4a로 정규화한다."""
    normalized = audio_format.lower().strip().lstrip(".")
    if normalized in {"mpeg", "mpga"}:
        normalized = "mp3"
    if normalized in {"mp4", "aac"}:
        normalized = "m4a"

    if normalized not in {"mp3", "wav", "m4a"}:
        raise ValueError("audio_format은 mp3, wav, m4a만 지원합니다.")

    return normalized
