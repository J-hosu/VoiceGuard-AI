"""화자 분리 학습: 목소리 특징(임베딩)을 우리 통화 데이터에 맞게 LDA로 다시 투영한다.

ONNX 임베딩 모델 자체는 다시 학습할 수 없으므로, 그 출력 위에 '같은 사람은 가깝게, 다른 사람은 멀게' 보이도록
선형 변환(LDA)을 학습한다. 화자 인식에서 쓰는 표준적인 도메인 맞춤 방법이다.
학습 데이터: 학습용(train) 녹음의 정렬된 문장(ok, 1초 이상). 화자 라벨 = (녹음, 역할).
주의: AI Hub 녹음은 같은 성우가 여러 녹음에 나올 수 있어, 서로 다른 라벨이 같은 사람일 수 있다(학습이 조금 덜 깨끗함).
산출: diar_models/lda_<임베딩 이름>.pkl → diar_simple.py가 PROJ 환경 변수로 읽어 임베딩에 적용한다.

사용: PYTHONPATH=pylib:pylib_sherpa ~/voiceguard/.venv/bin/python -X utf8 scripts/diar_lda.py <embedding.onnx> [녹음 수=1500] [차원=128] [작업 수=12]
"""
import hashlib
import json
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path.home() / "voiceguard_ft"
EMB = sys.argv[1]
N_REC = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
DIM = int(sys.argv[3]) if len(sys.argv) > 3 else 128
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 12
SR = 16000
MAX_PER_ROLE = 12
_ext = None


def extractor():
    global _ext
    if _ext is None:
        import sherpa_onnx
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB, num_threads=1))
    return _ext


def embed(audio):
    ext = extractor()
    s = ext.create_stream()
    s.accept_waveform(SR, audio)
    s.input_finished()
    v = np.array(ext.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def work(args):
    rec, path = args
    import soundfile as sf
    audio, _ = sf.read(path, dtype="float32")
    out = []
    count = {"agent": 0, "customer": 0}
    for t in rec["turns"]:
        if not t.get("ok") or t["spk"] not in count or t["end"] - t["start"] < 1.0 or count[t["spk"]] >= MAX_PER_ROLE:
            continue
        count[t["spk"]] += 1
        out.append((f"{rec['id']}:{t['spk']}", embed(audio[int(t["start"] * SR):int(t["end"] * SR)])))
    return out


if __name__ == "__main__":
    from sklearn.discriminant_analysis import LinearDiscriminantAnalysis

    files = {json.loads(l)["id"]: json.loads(l)["path"] for l in open(ROOT / "meta" / "files.jsonl", encoding="utf-8")}
    rows = [json.loads(l) for l in open(ROOT / "meta" / "aligned.jsonl", encoding="utf-8")]
    rows = [r for r in rows if r.get("split") == "train" and r.get("turns")]
    rows = sorted(rows, key=lambda r: hashlib.md5(r["id"].encode()).hexdigest())[:N_REC]
    t0 = time.time()
    labels, X = [], []
    with ProcessPoolExecutor(WORKERS) as pool:
        for part in pool.map(work, [(r, files[r["id"]]) for r in rows], chunksize=4):
            for lab, v in part:
                labels.append(lab)
                X.append(v)
    X = np.stack(X)
    _, y = np.unique(labels, return_inverse=True)
    keep = np.bincount(y)[y] >= 2   # 문장이 2개 이상인 화자만
    X, y = X[keep], y[keep]
    dim = min(DIM, X.shape[1], len(np.unique(y)) - 1)
    lda = LinearDiscriminantAnalysis(solver="eigen", shrinkage="auto", n_components=dim).fit(X, y)
    out = ROOT / "diar_models" / f"lda_{Path(EMB).stem}.pkl"
    out.write_bytes(pickle.dumps(lda))
    print(json.dumps({"recordings": len(rows), "embeddings": int(len(X)), "speakers": int(len(np.unique(y))),
                      "in_dim": int(X.shape[1]), "out_dim": int(dim), "minutes": round((time.time() - t0) / 60, 1),
                      "saved": str(out)}, ensure_ascii=False))
