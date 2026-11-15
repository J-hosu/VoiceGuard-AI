"""실제 음성 통화 파일로 분류기 점수를 잰다 (Whisper 전사 → 런타임과 같은 방식으로 채점).

학습·평가 데이터는 모두 글로 된 전사문이다. 실제 서비스는 음성을 Whisper로 받아 적은 글을 보므로,
받아쓰기 오류가 섞인 글에서도 오경보가 없는지 따로 확인한다. 전사는 한 번만 하고(TRANSCRIPTS 파일에
저장) 모델만 바꿔 다시 채점한다. 운영 DB에는 아무것도 쓰지 않는다.

실행 (서버):
    AUDIO_DIR=data/aihub/audio_valid .venv/bin/python -X utf8 -m backend.app.eval_audio
    KOELECTRA_MODEL_DIR=models/roberta_v3 AUDIO_DIR=data/aihub/audio_valid .venv/bin/python -X utf8 -m backend.app.eval_audio

환경변수:
    AUDIO_DIR    음성 파일 폴더 (mp3/wav/m4a). 파일 이름의 첫 '_' 앞부분을 분류 이름으로 쓴다
    LABEL        파일들의 정답 (기본 0 = 정상 통화)
    TRANSCRIPTS  전사 저장 파일 (기본 <AUDIO_DIR>/transcripts.json)
    KOELECTRA_MODEL_DIR  채점할 모델
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from backend.app.eval_external import score_all
from backend.app.paths import KOELECTRA_MODEL_DIR
from backend.app.services.koelectra_scorer import TH_DANGER, TH_WARNING


AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "data/aihub/audio_valid"))
LABEL = int(os.environ.get("LABEL", "0"))
TRANSCRIPTS = Path(os.environ.get("TRANSCRIPTS") or AUDIO_DIR / "transcripts.json")


def load_or_transcribe() -> dict[str, list[dict]]:
    cache: dict[str, list[dict]] = {}
    if TRANSCRIPTS.exists():
        cache = json.loads(TRANSCRIPTS.read_text(encoding="utf-8"))
    files = sorted(p for p in AUDIO_DIR.iterdir() if p.suffix.lower() in (".mp3", ".wav", ".m4a"))
    missing = [p for p in files if p.name not in cache]
    if missing:
        from backend.app.mp3_json import transcribe_audio  # Whisper는 전사할 파일이 있을 때만 불러온다

        for path in missing:
            started = time.monotonic()
            segments = transcribe_audio(str(path))
            cache[path.name] = [
                {"start": seg.get("start"), "end": seg.get("end"), "text": str(seg.get("text", "")).strip()}
                for seg in segments
                if str(seg.get("text", "")).strip()
            ]
            print(f"  전사 {path.name}: 구간 {len(cache[path.name])}개, {time.monotonic() - started:.1f}초", flush=True)
        TRANSCRIPTS.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return {name: cache[name] for name in sorted(cache) if (AUDIO_DIR / name).exists()}


def main() -> None:
    transcripts = load_or_transcribe()
    cases = [
        {
            "id": name,
            "label": LABEL,
            "category": name.split("_", 1)[0],
            "turns": [{"text": seg["text"]} for seg in segments],
        }
        for name, segments in transcripts.items()
        if segments
    ]
    rows = score_all(cases)
    print(f"\n모델: {KOELECTRA_MODEL_DIR}")
    print(f"음성 {len(rows)}개 ({AUDIO_DIR}), 정답 {'사기' if LABEL else '정상'} · 주의 {TH_WARNING} / 강한 경고 {TH_DANGER}")
    print(f"  {'파일':<34}{'구간':>5}{'길이(초)':>9}{'최고점':>8}{'마지막':>8}  첫 경보 구간")
    for row, case in zip(rows, cases):
        segments = transcripts[case["id"]]
        length = max((seg.get("end") or 0) for seg in segments)
        print(f"  {row['id'][:32]:<34}{len(segments):>5}{length:>9.0f}{row['max_score']:>8.2f}{(row['scores'] or [0])[-1]:>8.2f}  {row['first_alarm_turn'] or '-'}")
    alarmed = [row for row in rows if row["max_score"] >= TH_WARNING]
    danger = [row for row in rows if row["max_score"] >= TH_DANGER]
    print(f"\n통화 중 한 번이라도 주의 이상: {len(alarmed)}/{len(rows)} · 강한 경고 이상: {len(danger)}/{len(rows)}")


if __name__ == "__main__":
    main()
