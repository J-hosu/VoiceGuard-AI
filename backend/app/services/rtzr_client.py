"""리턴제로(RTZR) 음성 인식 API 클라이언트.

키는 .env의 RTZR_CLIENT_ID, RTZR_CLIENT_SECRET이다. 외부 엔진은 기본으로 꺼져 있고, 배포가 .env에서
STT_LIVE_ENGINE / STT_FILE_ENGINE / STT_REFINE을 rtzr로 켤 때만 쓴다(엔진 선택은 audio_transcriber).

- transcribe_file: 파일 전사(화자 분리·단어 시각). 녹음 파일, 통화 후 전체 재전사, 통화 중 정리(live_refine)에 쓴다.
- StreamSession: 통화 하나에 스트리밍 연결 하나. 실시간 조각마다 오디오를 보내고 Finalize로 그 조각을 바로 확정시킨다.
요청 설정은 측정에 쓴 stt_lab/refine_sim.py, stt_lab/eval_rtzr.py와 같다. REST는 표준 라이브러리(urllib)로 부른다.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np


logger = logging.getLogger(__name__)

API_URL = "https://openapi.vito.ai"
STREAM_URL = "wss://openapi.vito.ai/v1/transcribe:streaming"
SAMPLE_RATE = 16000
# 토큰은 6시간 유효하다. 긴 통화 도중 만료되지 않게 만료 30분 전부터는 새로 받는다.
TOKEN_REFRESH_MARGIN_S = 1800
# 파일 전사 결과 조회 간격. 정리 요청 하나가 2초 안팎이라 짧게 둔다.
POLL_INTERVAL_S = 0.8
# 스트리밍: 0.2초(6400바이트)씩 나눠 보내고, Finalize 뒤 확정 결과가 0.4초 끊기거나 4초가 지나면 그 조각을 마친다.
STREAM_FRAME_BYTES = 6400
STREAM_QUIET_S = 0.4
STREAM_MAX_WAIT_S = 4.0
# 스트리밍 연결이 다시 열어도 실패하면 이 시간 동안은 바로 로컬 전사로 넘긴다(조각마다 연결을 기다리느라 밀리지 않게).
STREAM_RETRY_AFTER_S = 30.0

Word = tuple[float, float, str, int]  # (시작 초, 끝 초, 글, 화자 번호)


class RtzrError(RuntimeError):
    """리턴제로 호출 실패(키 없음, 네트워크, 전사 실패). 부르는 쪽은 로컬 전사로 넘기거나 정리를 건너뛴다."""


def is_configured() -> bool:
    """RTZR_CLIENT_ID와 RTZR_CLIENT_SECRET이 모두 있으면 True."""
    return bool(os.getenv("RTZR_CLIENT_ID") and os.getenv("RTZR_CLIENT_SECRET"))


# ---------------------------------------------------------------- 인증


_token_lock = threading.Lock()
_token: dict = {"value": "", "expire_at": 0.0}


def access_token(refresh: bool = False) -> str:
    """접근 토큰을 돌려준다. 프로세스 안에서 나눠 쓰고, 만료가 가깝거나 refresh=True면 새로 받는다."""
    with _token_lock:
        if not refresh and _token["value"] and time.time() < _token["expire_at"] - TOKEN_REFRESH_MARGIN_S:
            return _token["value"]
        client_id = os.getenv("RTZR_CLIENT_ID", "")
        client_secret = os.getenv("RTZR_CLIENT_SECRET", "")
        if not (client_id and client_secret):
            raise RtzrError("RTZR_CLIENT_ID, RTZR_CLIENT_SECRET이 설정되지 않았습니다.")
        body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret}).encode("utf-8")
        try:
            data = _call(
                "POST", "/v1/authenticate", body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=15.0
            )
        except urllib.error.HTTPError as exc:
            raise RtzrError(f"리턴제로 인증 실패: HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise RtzrError(f"리턴제로 인증 요청 실패: {exc}") from exc
        if not data.get("access_token"):
            raise RtzrError("리턴제로 인증 응답에 access_token이 없습니다.")
        _token["value"] = data["access_token"]
        _token["expire_at"] = float(data.get("expire_at") or time.time() + 6 * 3600)
        return _token["value"]


def _call(
    method: str,
    path: str,
    body: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    """REST 호출 하나. 2xx가 아니면 urllib.error.HTTPError를 그대로 올린다(재시도는 _authorized가 정한다)."""
    request = urllib.request.Request(API_URL + path, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _authorized(method: str, path: str, deadline: float, body: Optional[bytes] = None, content_type: str = "") -> dict:
    """토큰을 붙여 부른다. 401이면 토큰을 한 번 새로 받고, 429(요청 한도)면 점점 길게 쉬었다가 다시 부른다."""
    refresh, refreshed, backoff = False, False, 1.0
    while True:
        headers = {"Authorization": f"Bearer {access_token(refresh=refresh)}"}
        refresh = False
        if content_type:
            headers["Content-Type"] = content_type
        try:
            return _call(method, path, body, headers, timeout=max(1.0, deadline - time.monotonic()))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not refreshed:
                refresh = refreshed = True
                continue
            if exc.code == 429 and time.monotonic() + backoff < deadline:
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            raise RtzrError(f"리턴제로 {method} {path} 실패: HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise RtzrError(f"리턴제로 {method} {path} 요청 실패: {exc}") from exc


# ---------------------------------------------------------------- 파일 전사


@dataclass
class FileResult:
    """파일 전사 결과. 시각은 보낸 오디오의 시작 기준 초다."""

    words: list[Word]
    utterances: list[dict]  # [{"start", "end", "text", "spk"}]
    took: float  # 올리고 결과를 받기까지 걸린 초


def transcribe_file(
    audio: Union[np.ndarray, bytes],
    spk_count: int = 2,
    word_timestamps: bool = True,
    timeout: float = 120.0,
    filename: str = "audio.wav",
) -> FileResult:
    """오디오를 파일 전사 API로 받아 적는다. 화자 분리는 항상 켠다.

    audio는 16kHz mono numpy(float -1~1 또는 int16)나 파일 바이트(filename 확장자로 형식을 알린다)다.
    spk_count=0이면 화자 수를 리턴제로가 정한다(정리 구간이 짧을 때). timeout 안에 끝나지 않거나 실패하면 RtzrError.
    """
    if not is_configured():
        raise RtzrError("RTZR_CLIENT_ID, RTZR_CLIENT_SECRET이 설정되지 않았습니다.")
    started = time.monotonic()
    deadline = started + timeout
    if isinstance(audio, (bytes, bytearray)):
        data, name = bytes(audio), filename
    else:
        data, name = encode_wav(audio), "audio.wav"
    config = {
        "model_name": "sommers",
        "domain": "CALL",
        "use_diarization": True,
        "diarization": {"spk_count": int(spk_count)},
        "use_itn": True,
        "use_disfluency_filter": True,
        "use_paragraph_splitter": False,
        "use_word_timestamp": bool(word_timestamps),
    }
    body, content_type = _multipart({"config": json.dumps(config)}, "file", name, data)
    job_id = _authorized("POST", "/v1/transcribe", deadline, body, content_type).get("id")
    if not job_id:
        raise RtzrError("리턴제로 파일 전사 요청 응답에 id가 없습니다.")

    while True:
        if time.monotonic() + POLL_INTERVAL_S > deadline:
            raise RtzrError(f"리턴제로 파일 전사가 {timeout:.0f}초 안에 끝나지 않았습니다.")
        time.sleep(POLL_INTERVAL_S)
        status = _authorized("GET", f"/v1/transcribe/{job_id}", deadline)
        if status.get("status") == "completed":
            break
        if status.get("status") == "failed":
            raise RtzrError(f"리턴제로 파일 전사 실패: {status.get('error')}")

    words, utterances = parse_utterances((status.get("results") or {}).get("utterances") or [])
    return FileResult(words=words, utterances=utterances, took=time.monotonic() - started)


def parse_utterances(utterances: list[dict]) -> tuple[list[Word], list[dict]]:
    """결과 발화 목록을 (단어 목록, 발화 목록)으로 바꾼다. 시각은 ms → 초.

    단어 시각은 파일 시작 기준이다. 단어 시각이 없는 발화는 발화 길이를 글자 수 비율로 나눠 단어 시각을 만든다.
    """
    words: list[Word] = []
    parsed: list[dict] = []
    for utterance in utterances:
        spk = int(utterance.get("spk") or 0)
        start = float(utterance.get("start_at") or 0) / 1000
        duration = float(utterance.get("duration") or 0) / 1000
        text = str(utterance.get("msg") or "").strip()
        if text:
            parsed.append({"start": round(start, 3), "end": round(start + duration, 3), "text": text, "spk": spk})
        items = utterance.get("words") or []
        if items:
            for item in items:
                token = str(item.get("text") or "").strip()
                if not token:
                    continue
                word_start = float(item.get("start_at", utterance.get("start_at")) or 0) / 1000
                words.append((word_start, word_start + float(item.get("duration") or 0) / 1000, token, spk))
            continue
        tokens = text.split()
        total = sum(len(token) for token in tokens) or 1
        cursor = start
        for token in tokens:
            length = duration * len(token) / total
            words.append((cursor, cursor + length, token, spk))
            cursor += length
    return words, parsed


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """multipart/form-data 본문을 만든다(requests 없이 올리기 위해)."""
    boundary = uuid.uuid4().hex
    file_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {file_type}\r\n\r\n".encode("utf-8")
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def to_pcm16(samples: np.ndarray) -> np.ndarray:
    """float(-1~1) 또는 int16 오디오를 리틀 엔디언 int16으로 바꾼다."""
    if samples.dtype == np.int16:
        return samples.astype("<i2", copy=False)
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")


def encode_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """mono 오디오를 16비트 WAV 바이트로 만든다."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(to_pcm16(samples).tobytes())
    return buffer.getvalue()


# ---------------------------------------------------------------- 스트리밍 (실시간 조각)


class StreamSession:
    """통화 하나에 붙는 스트리밍 전사 연결. 첫 조각에서 열고 통화가 끝나면 close로 닫는다.

    조각마다 PCM을 보낸 뒤 Finalize로 그때까지 들어온 말을 확정시키고, 확정 결과가 0.4초 끊기거나 4초가 지나면
    그 조각을 마친다. 동기 websockets 클라이언트라 작업 스레드(asyncio.to_thread)에서 불러야 한다.
    클라이언트가 무음 조각을 보내지 않아 연결이 오래 쉬다가 닫힐 수 있으므로, 끊겼으면 한 번 다시 열어 보낸다.
    """

    def __init__(self) -> None:
        self._ws = None
        self._lock = threading.Lock()
        # 지금 연결로 보낸 오디오 길이(초). 결과 시각은 연결 기준이라 조각 기준으로 바꿀 때 뺀다.
        self._sent_s = 0.0
        self._retry_at = 0.0
        self._closed = False

    def transcribe(self, samples: np.ndarray) -> list[dict]:
        """16kHz 조각 하나를 받아 적는다. 반환: [{"start", "end", "speaker": "", "text"}] (조각 기준 초)."""
        with self._lock:
            if self._closed:
                raise RtzrError("이미 닫힌 스트리밍 연결입니다.")
            if time.monotonic() < self._retry_at:
                raise RtzrError("스트리밍 연결 실패 뒤 다시 시도하기를 기다리는 중입니다.")
            pcm = to_pcm16(samples).tobytes()
            duration = len(samples) / SAMPLE_RATE
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    if self._ws is None:
                        self._open(refresh_token=attempt > 0)
                    return self._send_chunk(pcm, duration)
                except Exception as exc:  # 끊긴 연결·네트워크·인증 오류: 연결을 버리고 한 번 더 연다
                    last_error = exc
                    self._drop()
            self._retry_at = time.monotonic() + STREAM_RETRY_AFTER_S
            raise RtzrError(f"리턴제로 스트리밍 전사 실패: {last_error}") from last_error

    def close(self) -> None:
        """EOS를 보내고 연결을 닫는다. 여러 번 불러도 된다."""
        with self._lock:
            self._closed = True
            if self._ws is not None:
                try:
                    self._ws.send("EOS")
                except Exception:
                    pass
            self._drop()

    def _open(self, refresh_token: bool = False) -> None:
        from websockets.sync.client import connect

        query = urllib.parse.urlencode(
            {
                "sample_rate": SAMPLE_RATE,
                "encoding": "LINEAR16",
                "model_name": "sommers_ko",
                "domain": "CALL",
                "use_itn": "true",
                "use_disfluency_filter": "true",
            }
        )
        self._ws = connect(
            f"{STREAM_URL}?{query}",
            additional_headers={"Authorization": f"Bearer {access_token(refresh=refresh_token)}"},
            open_timeout=10,
            close_timeout=2,
            max_size=None,
        )
        self._sent_s = 0.0

    def _send_chunk(self, pcm: bytes, duration: float) -> list[dict]:
        ws = self._ws
        offset = self._sent_s
        for index in range(0, len(pcm), STREAM_FRAME_BYTES):
            ws.send(pcm[index:index + STREAM_FRAME_BYTES])
        ws.send(json.dumps({"type": "Finalize"}))
        self._sent_s += duration

        segments: list[dict] = []
        deadline = time.monotonic() + STREAM_MAX_WAIT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = ws.recv(timeout=min(STREAM_QUIET_S, remaining) if segments else remaining)
            except TimeoutError:
                break
            message = json.loads(raw)
            if not message.get("final"):
                continue
            text = str(((message.get("alternatives") or [{}])[0]).get("text") or "").strip()
            if not text:
                continue
            # 앞 조각의 결과가 늦게 와도 글은 살리고, 시각만 이 조각 안으로 묶는다.
            start = min(max(float(message.get("start_at") or 0) / 1000 - offset, 0.0), duration)
            end = min(max(start + float(message.get("duration") or 0) / 1000, start), duration)
            segments.append({"start": round(start, 2), "end": round(end, 2), "speaker": "", "text": text})
        return segments

    def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
