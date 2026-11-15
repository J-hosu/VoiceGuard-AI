"""말 없는 구간에서 전사 모델이 글을 지어내는지 점검한다 (학습한 모델과 원래 모델 비교용).

- 합성: 디지털 무음, 약한·중간·강한 백색 잡음, 60Hz 험+잡음 — 5초씩, 종류마다 4개
- 실제: 시험 녹음에서 정렬이 믿을 만한 문장 두 개 사이의 빈틈(2초 이상, 앞뒤 0.3초 뺌) 최대 200개
  (사람 맞장구가 섞여 있을 수 있어 절대값보다 모델 간 차이를 본다)
서비스와 같은 설정(beam 5, VAD, 앞 문맥 끔, 환각 문구 필터)으로 받아 적어, 글이 나온 구간 수를 센다.

사용: PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/robust_nonspeech.py <모델 이름|CT2 폴더> <out.json>
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

ROOT = Path.home() / "voiceguard_ft"
SR = 16000
_HALLUCINATION = re.compile(
    r"시청해\s*주셔서|구독(과|\s*좋아요)|좋아요와\s*구독|자막\s*(제공|by)|MBC\s*뉴스|KBS\s*뉴스|다음\s*영상에서"
    r"|이\s*시각\s*세계|뉴스\s*였습니다|한글\s*자막"
)


def transcribe(model, audio):
    segments, _ = model.transcribe(audio, language="ko", beam_size=5, vad_filter=True, condition_on_previous_text=False)
    kept = [s.text.strip() for s in segments
            if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) and not _HALLUCINATION.search(s.text)]
    return " ".join(t for t in kept if t)


def synthetic():
    rng = np.random.default_rng(0)
    n = SR * 5
    t = np.arange(n) / SR
    clips = []
    for seed in range(4):
        r = np.random.default_rng(seed)
        clips.append(("silence", np.zeros(n, dtype=np.float32)))
        for name, amp in (("noise_low", 0.003), ("noise_mid", 0.01), ("noise_high", 0.03)):
            clips.append((name, (r.standard_normal(n) * amp).astype(np.float32)))
        hum = 0.02 * np.sin(2 * np.pi * 60 * t) + r.standard_normal(n) * 0.005
        clips.append(("hum", hum.astype(np.float32)))
    del rng
    return clips


def real_gaps(limit=200):
    tests = json.loads((ROOT / "meta" / "test_files_frozen.json").read_text(encoding="utf-8"))
    gaps = []
    for t in tests:
        turns = t["turns"]
        for a, b in zip(turns, turns[1:]):
            if a.get("ok") and b.get("ok") and b["start"] - a["end"] >= 2.0:
                gaps.append((t["path"], a["end"] + 0.3, b["start"] - 0.3))
    gaps.sort(key=lambda g: hash((g[0], round(g[1], 2))) % 100003)
    return gaps[:limit]


if __name__ == "__main__":
    model_path, out = sys.argv[1], Path(sys.argv[2])
    model = WhisperModel(model_path, device="cuda", compute_type="float16")
    syn_rows = []
    for name, audio in synthetic():
        text = transcribe(model, audio)
        syn_rows.append({"kind": name, "text": text})
    gap_rows = []
    for path, s, e in real_gaps():
        audio, _ = sf.read(path, start=int(s * SR), stop=int(e * SR), dtype="float32")
        text = transcribe(model, audio)
        gap_rows.append({"path": Path(path).parent.name + "/" + Path(path).name, "s": round(s, 2), "e": round(e, 2), "text": text})
    summary = {
        "model": model_path,
        "synthetic_clips": len(syn_rows),
        "synthetic_with_text": sum(1 for r in syn_rows if r["text"]),
        "gaps": len(gap_rows),
        "gaps_with_text": sum(1 for r in gap_rows if r["text"]),
        "gap_text_chars": sum(len(r["text"]) for r in gap_rows),
        "synthetic_examples": [r for r in syn_rows if r["text"]][:10],
        "gap_examples": [r["text"] for r in gap_rows if r["text"]][:15],
    }
    out.write_text(json.dumps({"summary": summary, "synthetic": syn_rows, "gaps": gap_rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
