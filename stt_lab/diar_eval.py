"""시험 녹음으로 로컬 화자 분리(sherpa-onnx: pyannote 분할 + 목소리 특징 모델 + 묶기)를 평가한다.

정답: 정렬이 믿을 만한 문장(ok)의 시각과 화자(상담사 agent / 고객 customer).
평가 구간: 믿을 만한 문장이 연달아 이어지는 구간(문장 사이 공백 포함). 경계 ±0.25초는 채점하지 않는다(collar).
지표: DER = (놓친 말 + 없는데 잡은 말 + 화자 혼동) / 정답 말 길이, 그리고 문장 단위 화자 정확도
      (문장 구간에서 가장 많이 나온 예측 화자를 최적 대응시킨 뒤 맞은 문장 비율).

사용: PYTHONPATH=pylib:pylib_sherpa ~/voiceguard/.venv/bin/python -X utf8 scripts/diar_eval.py \
        <embedding.onnx> <out.json> [녹음 수=120] [clusters=2] [threshold=0.5] [작업 수=12]
"""
import hashlib
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

ROOT = Path.home() / "voiceguard_ft"
SEG = str(ROOT / "diar_models" / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx")
EMB = sys.argv[1]
OUT = Path(sys.argv[2])
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 120
CLUSTERS = int(sys.argv[4]) if len(sys.argv) > 4 else 2
THRESH = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
WORKERS = int(sys.argv[6]) if len(sys.argv) > 6 else 12
STEP = 0.01
COLLAR = 0.25
SPK = {"agent": 0, "customer": 1}

_sd = None


def diarizer():
    global _sd
    if _sd is None:
        import sherpa_onnx
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG), num_threads=1),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB, num_threads=1),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=CLUSTERS, threshold=THRESH),
            min_duration_on=0.3, min_duration_off=0.5)
        assert cfg.validate(), "bad sherpa config"
        _sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    return _sd


def spans_of(turns):
    """믿을 만한 문장이 연달아 이어지는 구간 [(시작, 끝)]"""
    spans, cur = [], None
    for t in turns:
        if t.get("ok"):
            if cur is None:
                cur = [t["start"], t["end"]]
            else:
                cur[1] = t["end"]
        else:
            if cur:
                spans.append(tuple(cur))
            cur = None
    if cur:
        spans.append(tuple(cur))
    return spans


def work(t):
    import soundfile as sf
    audio, sr = sf.read(t["path"], dtype="float32")
    t0 = time.time()
    result = diarizer().process(audio).sort_by_start_time()
    took = time.time() - t0
    hyp = [(s.start, s.end, s.speaker) for s in result]
    n = int(t["seconds"] / STEP) + 1
    ref = np.full(n, -1, dtype=np.int8)   # -1 말 없음, 0 상담사, 1 고객
    score = np.zeros(n, dtype=bool)        # 채점 구간
    for a, b in spans_of(t["turns"]):
        score[int(a / STEP):int(b / STEP) + 1] = True
    oks = [x for x in t["turns"] if x.get("ok")]
    for x in oks:
        ref[int(x["start"] / STEP):int(x["end"] / STEP) + 1] = SPK.get(x["spk"], 0)
    for x in oks:  # 경계 ±collar 제외
        for edge in (x["start"], x["end"]):
            score[max(0, int((edge - COLLAR) / STEP)):int((edge + COLLAR) / STEP) + 1] = False
    hyp_lab = np.full(n, -1, dtype=np.int16)
    for a, b, spk in sorted(hyp, key=lambda h: h[1] - h[0]):  # 짧은 구간부터 칠하고 긴 구간이 덮는다
        hyp_lab[int(a / STEP):int(b / STEP) + 1] = spk
    k = max(int(hyp_lab.max()) + 1, 1)
    conf = np.zeros((2, k), dtype=np.int64)
    m = score & (ref >= 0) & (hyp_lab >= 0)
    np.add.at(conf, (ref[m], hyp_lab[m]), 1)
    r_idx, c_idx = linear_sum_assignment(-conf)
    mapping = {c: r for r, c in zip(r_idx, c_idx)}
    mapped = np.array([mapping.get(int(h), -2) if h >= 0 else -1 for h in hyp_lab], dtype=np.int16)
    ref_speech = score & (ref >= 0)
    miss = int(np.sum(ref_speech & (hyp_lab < 0)))
    fa = int(np.sum(score & (ref < 0) & (hyp_lab >= 0)))
    confusion = int(np.sum(ref_speech & (hyp_lab >= 0) & (mapped != ref)))
    total = int(np.sum(ref_speech))
    right = counted = 0
    for x in oks:
        a, b = int((x["start"] + COLLAR) / STEP), int((x["end"] - COLLAR) / STEP) + 1
        seg = mapped[a:b]
        seg = seg[seg >= 0]
        if len(seg) == 0:
            continue
        counted += 1
        right += int(np.bincount(seg).argmax() == SPK.get(x["spk"], 0))
    return {"id": t["id"], "domain": t["domain"], "seconds": t["seconds"], "n_hyp_speakers": len({h[2] for h in hyp}),
            "miss": miss, "fa": fa, "conf": confusion, "total": total, "turns_right": right, "turns_counted": counted,
            "rtf": round(took / t["seconds"], 4)}


if __name__ == "__main__":
    tests = json.loads((ROOT / "meta" / "test_files.json").read_text(encoding="utf-8"))
    tests = sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest())[:LIMIT]
    t0 = time.time()
    with ProcessPoolExecutor(WORKERS) as pool:
        rows = list(pool.map(work, tests, chunksize=2))
    tot = sum(r["total"] for r in rows) or 1
    summary = {
        "embedding": Path(EMB).name, "clusters": CLUSTERS, "threshold": THRESH, "recordings": len(rows),
        "DER": round((sum(r["miss"] + r["fa"] + r["conf"] for r in rows)) / tot, 4),
        "miss": round(sum(r["miss"] for r in rows) / tot, 4), "false_alarm": round(sum(r["fa"] for r in rows) / tot, 4),
        "confusion": round(sum(r["conf"] for r in rows) / tot, 4),
        "turn_speaker_accuracy": round(sum(r["turns_right"] for r in rows) / max(1, sum(r["turns_counted"] for r in rows)), 4),
        "mean_rtf_per_core": round(float(np.mean([r["rtf"] for r in rows])), 4),
        "minutes": round((time.time() - t0) / 60, 1),
    }
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
