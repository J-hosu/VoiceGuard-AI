"""녹음마다 상담사 문장과 고객 문장의 목소리가 실제로 다른지 본다(한 사람이 두 역할을 읽은 녹음 찾기).

정렬이 믿을 만한 문장(1.5초 이상)마다 목소리 특징(임베딩)을 뽑아,
같은 역할끼리 평균 코사인 유사도(within)와 다른 역할 사이 평균 유사도(cross)를 비교한다.
두 사람이면 cross가 within보다 뚜렷이 낮고, 한 사람이면 둘이 비슷하다.

사용: PYTHONPATH=pylib:pylib_sherpa ~/voiceguard/.venv/bin/python -X utf8 scripts/spk_check.py <embedding.onnx> <out.json> [녹음 수=300] [작업 수=12]
"""
import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path.home() / "voiceguard_ft"
EMB = sys.argv[1]
OUT = Path(sys.argv[2])
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 300
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 12
SR = 16000
_ext = None


def extractor():
    global _ext
    if _ext is None:
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB, num_threads=1)
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _ext


def embed(audio):
    ext = extractor()
    stream = ext.create_stream()
    stream.accept_waveform(SR, audio)
    stream.input_finished()
    v = np.array(ext.compute(stream), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def work(t):
    import soundfile as sf
    audio, _ = sf.read(t["path"], dtype="float32")
    embs = {"agent": [], "customer": []}
    for x in t["turns"]:
        if x.get("ok") and x["spk"] in embs and x["end"] - x["start"] >= 1.5:
            embs[x["spk"]].append(embed(audio[int(x["start"] * SR):int(x["end"] * SR)]))
    a, c = np.array(embs["agent"]), np.array(embs["customer"])
    if len(a) < 2 or len(c) < 2:
        return {"id": t["id"], "skip": True}

    def mean_off_diag(m):
        s = m @ m.T
        n = len(m)
        return float((s.sum() - np.trace(s)) / (n * n - n))

    within = (mean_off_diag(a) + mean_off_diag(c)) / 2
    cross = float((a @ c.T).mean())
    return {"id": t["id"], "domain": t["domain"], "within": round(within, 3), "cross": round(cross, 3),
            "gap": round(within - cross, 3), "n_agent": len(a), "n_customer": len(c)}


if __name__ == "__main__":
    tests = json.loads((ROOT / "meta" / "test_files.json").read_text(encoding="utf-8"))
    tests = sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest())[:LIMIT]
    with ProcessPoolExecutor(WORKERS) as pool:
        rows = [r for r in pool.map(work, tests, chunksize=2) if not r.get("skip")]
    gaps = np.array([r["gap"] for r in rows])
    hist = {f"{lo:.2f}~{lo + 0.05:.2f}": int(((gaps >= lo) & (gaps < lo + 0.05)).sum()) for lo in np.arange(-0.1, 0.6, 0.05)}
    summary = {"recordings": len(rows), "gap_median": round(float(np.median(gaps)), 3), "gap_histogram": hist,
               "two_voices_gap_ge_0.15": int((gaps >= 0.15).sum()), "one_voice_gap_lt_0.05": int((gaps < 0.05).sum())}
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
