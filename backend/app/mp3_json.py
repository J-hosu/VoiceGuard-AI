"""타임스탬프 포함 전사 + dataset.json 스키마 통합 (faster-whisper 로컬 STT 버전)
   - H100 GPU에서 faster-whisper(large-v3)가 전사와 타임스탬프 생성을 수행 (API 키 불필요)
   - 모델은 WHISPER_MODEL, 연산 정밀도는 WHISPER_COMPUTE 환경변수로 변경 가능
"""
import json
import os
import re
import threading

MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "float16")

# 무음/잡음 구간에서 Whisper가 지어내는 대표적인 문구 (유튜브 자막 학습 흔적).
# 실제 통화 내용이 아니므로 결과에서 제외한다.
_HALLUCINATION = re.compile(
    r"시청해\s*주셔서|구독(과|\s*좋아요)|좋아요와\s*구독|자막\s*(제공|by)|MBC\s*뉴스|KBS\s*뉴스|다음\s*영상에서"
)

_model = None
_model_lock = threading.Lock()


def load_model():
    """Whisper 모델을 1회 로드한다. 서버 시작 시 미리 호출해 첫 요청 지연을 없앤다."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(MODEL, device="cuda", compute_type=COMPUTE_TYPE)
    return _model


def _is_hallucination(seg) -> bool:
    """무음 확률이 높고 신뢰도가 낮은 구간, 또는 알려진 환각 문구를 걸러낸다."""
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
        return True
    return bool(_HALLUCINATION.search(seg.text))


def transcribe_audio(audio_file_path: str, retries: int = 1, realtime: bool = False) -> list[dict]:
    """오디오 파일을 faster-whisper로 전사해 발화 segment 목록을 반환한다.

    반환 형식: [{"start": 초, "end": 초, "speaker": "", "text": "..."}]
    Whisper는 화자 구분을 하지 않으므로 speaker는 빈 값이다 (정규화 단계에서 화자A로 표시).
    realtime=True(3초 chunk)는 지연을 줄이기 위해 beam_size=1로 디코딩한다.
    """
    model = load_model()
    last_err = None
    for _ in range(retries + 1):
        try:
            with _model_lock:
                raw_segments, _info = model.transcribe(
                    audio_file_path,
                    language="ko",
                    beam_size=1 if realtime else 5,
                    vad_filter=True,
                    condition_on_previous_text=False,
                )
                raw_segments = list(raw_segments)
            break
        except Exception as e:
            last_err = e
    else:
        raise RuntimeError(f"Whisper 전사 실패: {last_err}")

    segments = []
    for seg in raw_segments:
        text = seg.text.strip()
        if not text or _is_hallucination(seg):
            continue
        segments.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "speaker": "",
            "text": text,
        })
    return segments


def transcribe_with_speakers(audio_file_path: str, expected_speakers: int = 2, retries: int = 1) -> list[dict]:
    """이전 호출부 호환용 alias. 실제 반환에는 화자 구분 필드가 없다."""
    return transcribe_audio(audio_file_path, retries=retries)


if __name__ == "__main__":
    import platform
    import sys

    # 사용법: python -m backend.app.mp3_json [오디오 폴더 경로] [phishing_type]
    if len(sys.argv) > 1:
        TEST_DIR = sys.argv[1]
        PHISHING_TYPE = sys.argv[2] if len(sys.argv) > 2 else None
    else:
        if platform.system() == "Darwin":
            TEST_DIR = "/Users/seminy/Desktop/보이스피싱 데이터셋(금감원)/그놈 목소리(수사기관 사칭형)"
        else:
            TEST_DIR = os.path.expanduser("~/dataset/그놈 목소리(수사기관 사칭형)")                  # 원격 서버 데이터셋 경로
        PHISHING_TYPE = "수사기관 사칭형"
    LABEL = "phishing"                 # 이 폴더는 전부 보이스피싱 녹취
    OUTPUT_DIR = os.path.join(TEST_DIR, "results")                       # 결과 파일 저장 폴더
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    OUTPUT_JSON = os.path.join(OUTPUT_DIR, "dataset.json")

    # 중단된 실행 재개: 기존 결과가 있으면 이어서 처리
    dataset = []
    if os.path.exists(OUTPUT_JSON):
        with open(OUTPUT_JSON, encoding="utf-8") as f:
            dataset = json.load(f)
        print(f"기존 결과 {len(dataset)}개 call 로드 — 이어서 진행")
    done_ids = {c["call_id"] for c in dataset}

    audio_files = [f for f in sorted(os.listdir(TEST_DIR)) if f.lower().endswith((".mp3", ".wav", ".m4a"))]

    for idx, filename in enumerate(audio_files, 1):
        call_id = os.path.splitext(filename)[0]
        if call_id in done_ids:
            continue

        print(f"=== [{idx}/{len(audio_files)}] {filename} ===")
        segments_raw = transcribe_audio(os.path.join(TEST_DIR, filename))

        segments = [
            {
                "chunk_id": i + 1,
                "start_time": seg.get("start"),
                "end_time": seg.get("end"),
                "text": seg.get("text", "").strip(),
                "matched_patterns": []
            }
            for i, seg in enumerate(segments_raw)
        ]

        dataset.append({
            "call_id": call_id,
            "label": LABEL,
            "phishing_type": PHISHING_TYPE,
            "segments": segments
        })
        print(f"발언 {len(segments)}개 추출\n")

        # 장시간 실행 대비: 파일 하나 끝날 때마다 결과 갱신 저장
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(dataset, f, ensure_ascii=False, indent=2)

    print(f"✅ 총 {len(dataset)}개 call → {OUTPUT_JSON}")
