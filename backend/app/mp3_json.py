"""타임스탬프 포함 전사 + dataset.json 스키마 통합 (faster-whisper 로컬 STT 버전)
   - faster-whisper가 전사와 타임스탬프 생성을 수행 (API 키 불필요)
   - GPU(CUDA)가 있으면 large-v3/float16, 없으면 CPU에서 small/int8을 기본으로 쓴다.
   - WHISPER_DEVICE(auto|cuda|cpu), WHISPER_MODEL, WHISPER_COMPUTE, WHISPER_BEAM, WHISPER_CPU_THREADS
     환경변수로 변경 가능
"""
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# 장치별 기본값. CPU에서 large-v3는 실시간 청크를 따라가지 못하므로 작은 모델을 쓴다.
# beam_size는 파일 전체 전사용이다(실시간 청크는 항상 1). CPU에서 beam 5는 실제 통화 녹음에서
# 재생 시간만큼 걸려(노트북 기준) 통화 종료 후 재전사가 너무 늦어지므로 1로 둔다.
_DEVICE_DEFAULTS = {
    "cuda": {"model": "large-v3", "compute_type": "float16", "beam_size": 5},
    "cpu": {"model": "small", "compute_type": "int8", "beam_size": 1},
}


def _cuda_available() -> bool:
    """faster-whisper(CTranslate2)가 쓸 수 있는 CUDA 장치가 있는지 확인한다."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_runtime() -> dict:
    """환경변수와 장치 상태로 Whisper 실행 설정(device, model, compute_type)을 정한다."""
    device = os.environ.get("WHISPER_DEVICE", "auto").strip().lower()
    if device not in ("cuda", "cpu"):
        device = "cuda" if _cuda_available() else "cpu"
    defaults = _DEVICE_DEFAULTS[device]
    return {
        "device": device,
        "model": os.environ.get("WHISPER_MODEL") or defaults["model"],
        "compute_type": os.environ.get("WHISPER_COMPUTE") or defaults["compute_type"],
        "beam_size": int(os.environ.get("WHISPER_BEAM") or defaults["beam_size"]),
        "cpu_threads": int(os.environ.get("WHISPER_CPU_THREADS", "0") or 0),
    }

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

            runtime = resolve_runtime()
            logger.info(
                "Whisper 모델 로드: model=%s device=%s compute_type=%s",
                runtime["model"], runtime["device"], runtime["compute_type"],
            )
            _model = WhisperModel(
                runtime["model"],
                device=runtime["device"],
                compute_type=runtime["compute_type"],
                cpu_threads=runtime["cpu_threads"],
            )
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
    runtime = resolve_runtime()
    full_beam_size = runtime["beam_size"]
    # Whisper는 결과 신뢰도가 낮으면 temperature를 올려가며 최대 6번 다시 디코딩한다. CPU에서는
    # 잡음뿐인 청크 하나가 이 재시도로 수십 초를 잡아먹어 뒤 청크가 밀리므로, 실시간 청크에
    # 한해 재시도를 한 번으로 줄인다.
    temperature = (0.0, 0.4) if realtime and runtime["device"] == "cpu" else (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    last_err = None
    for _ in range(retries + 1):
        try:
            with _model_lock:
                raw_segments, _info = model.transcribe(
                    audio_file_path,
                    language="ko",
                    beam_size=1 if realtime else full_beam_size,
                    temperature=temperature,
                    # 반복 환각에 빠지면 최대 길이까지 디코딩하느라 CPU에서 수십 초가 걸린다.
                    # 실시간 청크(최대 10초)는 그만큼 길 수 없으므로 출력 길이를 묶어 둔다.
                    max_new_tokens=128 if realtime and runtime["device"] == "cpu" else None,
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
