"""통화용 단순 화자 분리: 말 조각마다 목소리 특징(임베딩)을 뽑아 두 사람으로 나눈다. sherpa 기본 파이프라인과 비교용.

방식 (MODE)
- offline: 통화 후. 전사 단어 시각으로 말 조각을 만든다(단어 사이 0.3초 이상 쉬면 끊고, 6초를 넘으면 끊음).
           조각 임베딩을 2개 묶음으로 나눈다(코사인 평균 연결 군집). 0.6초 미만 조각은 시간상 가까운 조각을 따른다.
- online:  실시간. 앱과 같은 무음 조각(1.2~10초)이 올 때마다 임베딩을 뽑아,
           첫 조각을 화자1로 두고, 화자1과 유사도가 TAU 미만이면 화자2를 만들고, 이후는 가까운 쪽 평균에 붙인다.
- enroll:  실시간 + 내 목소리 등록. 고객(=사용자 역할) 첫 두 문장으로 '내 목소리'를 만들고(채점에서 뺌),
           조각마다 등록 목소리와 유사도가 TAU 이상이면 '나', 아니면 '상대'.
채점: diar_eval.py와 같은 DER(경계 ±0.25초 제외)과 문장 단위 화자 정확도. online/enroll은 조각 단위 정확도도 낸다.

사용: PYTHONPATH=pylib:pylib_sherpa ~/voiceguard/.venv/bin/python -X utf8 scripts/diar_simple.py \
        <embedding.onnx> <MODE> <out.json> [녹음 수=120] [TAU=0.5] [작업 수=12]
"""
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

ROOT = Path.home() / "voiceguard_ft"
EMB, MODE, OUT = sys.argv[1], sys.argv[2], Path(sys.argv[3])
LIMIT = int(sys.argv[4]) if len(sys.argv) > 4 else 120
TAU = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
WORKERS = int(sys.argv[6]) if len(sys.argv) > 6 else 12
SR, STEP, COLLAR = 16000, 0.01, 0.25
SPK = {"agent": 0, "customer": 1}
MIN_S, MAX_S, SIL_S, SIL_RMS, FRAME = 1.2, 10.0, 0.25, 0.01, 320
_ext = None


def extractor():
    global _ext
    if _ext is None:
        import sherpa_onnx
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB, num_threads=1))
    return _ext


_proj = None


def embed(audio):
    global _proj
    ext = extractor()
    s = ext.create_stream()
    s.accept_waveform(SR, audio)
    s.input_finished()
    v = np.array(ext.compute(s), dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    if os.environ.get("PROJ"):  # diar_lda.py로 학습한 투영(LDA)을 적용
        if _proj is None:
            import pickle
            _proj = pickle.loads(Path(os.environ["PROJ"]).read_bytes())
        v = _proj.transform(v[None, :])[0].astype(np.float32)
        v = v / (np.linalg.norm(v) + 1e-9)
    return v


def silence_chunks(audio):
    chunks, start = [], 0
    for end in range(FRAME, len(audio) + FRAME, FRAME):
        end = min(end, len(audio))
        length = end - start
        if length >= MAX_S * SR:
            chunks.append((start, end))
            start = end
            continue
        if length >= MIN_S * SR:
            tail = audio[max(start, end - int(SIL_S * SR)):end]
            if len(tail) and float(np.sqrt(np.mean(tail ** 2))) < SIL_RMS:
                chunks.append((start, end))
                start = end
    if start < len(audio):
        chunks.append((start, len(audio)))
    return [(a / SR, b / SR) for a, b in chunks if float(np.sqrt(np.mean(audio[a:b] ** 2))) >= SIL_RMS]


def asr_pieces(rec):
    words = [w for s in rec["segments"] for w in s["w"]]
    pieces, cur = [], None
    for _w, ws, we, _p in words:
        if cur and (ws - cur[1] >= 0.3 or we - cur[0] > 6.0):
            pieces.append(cur)
            cur = None
        cur = [ws, we] if cur is None else [cur[0], we]
    if cur:
        pieces.append(cur)
    return [(a, b) for a, b in pieces if b > a]


def score(t, hyp):
    """hyp: [(start, end, label)] → DER 구성요소와 문장 정확도 (diar_eval.py와 같은 규칙)"""
    n = int(t["seconds"] / STEP) + 1
    ref = np.full(n, -1, dtype=np.int8)
    scored = np.zeros(n, dtype=bool)
    oks = [x for x in t["turns"] if x.get("ok")]
    cur = None
    spans = []
    for x in t["turns"]:
        if x.get("ok"):
            cur = [x["start"], x["end"]] if cur is None else [cur[0], x["end"]]
        else:
            if cur:
                spans.append(cur)
            cur = None
    if cur:
        spans.append(cur)
    for a, b in spans:
        scored[int(a / STEP):int(b / STEP) + 1] = True
    for x in oks:
        ref[int(x["start"] / STEP):int(x["end"] / STEP) + 1] = SPK.get(x["spk"], 0)
    for x in oks:
        for edge in (x["start"], x["end"]):
            scored[max(0, int((edge - COLLAR) / STEP)):int((edge + COLLAR) / STEP) + 1] = False
    hyp_lab = np.full(n, -1, dtype=np.int16)
    for a, b, lab in hyp:
        hyp_lab[int(a / STEP):min(n, int(b / STEP) + 1)] = lab
    k = max(int(hyp_lab.max()) + 1, 1)
    conf = np.zeros((2, k), dtype=np.int64)
    m = scored & (ref >= 0) & (hyp_lab >= 0)
    np.add.at(conf, (ref[m], hyp_lab[m]), 1)
    r_idx, c_idx = linear_sum_assignment(-conf)
    mapping = {c: r for r, c in zip(r_idx, c_idx)}
    mapped = np.array([mapping.get(int(h), -2) if h >= 0 else -1 for h in hyp_lab], dtype=np.int16)
    ref_speech = scored & (ref >= 0)
    out = {"miss": int(np.sum(ref_speech & (hyp_lab < 0))), "fa": int(np.sum(scored & (ref < 0) & (hyp_lab >= 0))),
           "conf": int(np.sum(ref_speech & (hyp_lab >= 0) & (mapped != ref))), "total": int(np.sum(ref_speech))}
    right = counted = 0
    for x in oks:
        a, b = int((x["start"] + COLLAR) / STEP), int((x["end"] - COLLAR) / STEP) + 1
        seg = mapped[a:b]
        seg = seg[seg >= 0]
        if len(seg):
            counted += 1
            right += int(np.bincount(seg).argmax() == SPK.get(x["spk"], 0))
    out["turns_right"], out["turns_counted"] = right, counted
    return out, ref


def chunk_truth(ref, a, b):
    seg = ref[int(a / STEP):int(b / STEP) + 1]
    seg = seg[seg >= 0]
    if len(seg) < 0.6 * (b - a) / STEP:   # 정답이 대부분 비어 있으면(정렬 안 된 구간) 채점 안 함
        return None
    vals = np.bincount(seg, minlength=2)
    return int(vals.argmax()) if vals.max() >= 0.8 * len(seg) else -9  # 두 사람이 섞인 조각은 -9


def work(args):
    t, rec = args
    import soundfile as sf
    audio, _ = sf.read(t["path"], dtype="float32")
    t0 = time.time()
    if MODE in ("offline", "offline_chunks"):
        pieces = asr_pieces(rec) if MODE == "offline" else silence_chunks(audio)
        long_idx = [i for i, (a, b) in enumerate(pieces) if b - a >= 0.6]
        if len(long_idx) < 2:
            return {"id": t["id"], "skip": True}
        E = np.stack([embed(audio[int(pieces[i][0] * SR):int(pieces[i][1] * SR)]) for i in long_idx])
        method = os.environ.get("CLUSTER", "kmeans")
        if method == "agglo":
            from sklearn.cluster import AgglomerativeClustering
            lab_long = AgglomerativeClustering(n_clusters=2, metric="cosine", linkage="average").fit_predict(E)
        elif method == "spectral":
            from sklearn.cluster import SpectralClustering
            aff = np.clip((E @ E.T + 1) / 2, 0, 1)
            lab_long = SpectralClustering(n_clusters=2, affinity="precomputed", random_state=0).fit_predict(aff)
        else:  # 길이 가중 k-means (2명)
            from sklearn.cluster import KMeans
            w = np.array([pieces[i][1] - pieces[i][0] for i in long_idx])
            lab_long = KMeans(n_clusters=2, n_init=10, random_state=0).fit_predict(E, sample_weight=w)
        labels = [None] * len(pieces)
        for i, l in zip(long_idx, lab_long):
            labels[i] = int(l)
        mids = np.array([(pieces[i][0] + pieces[i][1]) / 2 for i in long_idx])
        for i, (a, b) in enumerate(pieces):
            if labels[i] is None:
                labels[i] = int(lab_long[int(np.argmin(np.abs(mids - (a + b) / 2)))])
        hyp = [(a, b, l) for (a, b), l in zip(pieces, labels)]
        res, _ref = score(t, hyp)
    else:
        chunks = silence_chunks(audio)
        _, ref = score(t, [])
        hyp, chunk_ok, chunk_n, mixed = [], 0, 0, 0
        if MODE == "recluster":
            # 실시간 + 다시 묶기: 조각이 올 때마다 지금까지의 조각 전체를 k-means(2)로 다시 나누고,
            # 새 묶음을 이전 묶음 중심과 맞대 번호를 유지한다. 그 순간 화면에 표시될 라벨만 채점한다.
            from sklearn.cluster import KMeans
            embs, spans_kept, prev_cent = [], [], None
            first = None
            for a, b in chunks:
                if b - a < 0.6:
                    continue
                e = embed(audio[int(a * SR):int(b * SR)])
                embs.append(e)
                spans_kept.append((a, b))
                if first is None:
                    first = e
                    hyp.append((a, b, 0))
                    continue
                E = np.stack(embs)
                if prev_cent is None and float(e @ first) >= TAU and all(float(x @ first) >= TAU for x in embs):
                    hyp.append((a, b, 0))     # 아직 한 사람만 들림
                    continue
                w = np.array([s[1] - s[0] for s in spans_kept])
                km = KMeans(n_clusters=2, n_init=4, random_state=0).fit(E, sample_weight=w)
                cent = km.cluster_centers_ / np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True)
                if prev_cent is None:
                    ref_c = first[None, :]
                    order = [int(np.argmax(cent @ first)), 1 - int(np.argmax(cent @ first))]  # 첫 화자를 0번으로
                else:
                    sim = cent @ prev_cent.T
                    order = [0, 1] if sim[0, 0] + sim[1, 1] >= sim[0, 1] + sim[1, 0] else [1, 0]
                    order = [order.index(0), order.index(1)]
                cent = cent[order]
                prev_cent = cent
                lab = int(np.argmax(cent @ e))
                hyp.append((a, b, lab))
        elif MODE == "online":
            cents, counts = [], []
            for a, b in chunks:
                if b - a < 0.6:
                    continue
                e = embed(audio[int(a * SR):int(b * SR)])
                if not cents:
                    cents, counts = [e.copy()], [1]
                    lab = 0
                else:
                    sims = [float(e @ (c / np.linalg.norm(c))) for c in cents]
                    if len(cents) == 1 and sims[0] < TAU:
                        cents.append(e.copy())
                        counts.append(1)
                        lab = 1
                    else:
                        lab = int(np.argmax(sims))
                        cents[lab] = cents[lab] + e
                        counts[lab] += 1
                hyp.append((a, b, lab))
        else:  # enroll
            custs = [x for x in t["turns"] if x.get("ok") and x["spk"] == "customer" and x["end"] - x["start"] >= 1.5][:2]
            if len(custs) < 2:
                return {"id": t["id"], "skip": True}
            me = sum(embed(audio[int(x["start"] * SR):int(x["end"] * SR)]) for x in custs)
            me = me / np.linalg.norm(me)
            enroll_end = custs[-1]["end"]
            for a, b in chunks:
                if b - a < 0.6 or a < enroll_end:
                    continue
                e = embed(audio[int(a * SR):int(b * SR)])
                hyp.append((a, b, 1 if float(e @ me) >= TAU else 0))  # 1 = 나(고객), 0 = 상대(상담사)
        res, _ = score(t, hyp)
        for a, b, lab in hyp:
            truth = chunk_truth(ref, a, b)
            if truth is None:
                continue
            if truth == -9:
                mixed += 1
                continue
            chunk_n += 1
            chunk_ok += int(lab == truth) if MODE == "enroll" else 0
        if MODE in ("online", "recluster"):   # 화자 번호가 임의라 최적 대응 후 조각 정확도를 센다
            pairs = [(lab, chunk_truth(ref, a, b)) for a, b, lab in hyp]
            pairs = [(l, tr) for l, tr in pairs if tr is not None and tr >= 0]
            best = 0
            for perm in ({0: 0, 1: 1}, {0: 1, 1: 0}):
                best = max(best, sum(1 for l, tr in pairs if perm.get(l, -1) == tr))
            chunk_ok = best
        res.update({"chunks_scored": chunk_n, "chunks_right": chunk_ok, "chunks_mixed": mixed})
    res.update({"id": t["id"], "domain": t["domain"], "rtf": round((time.time() - t0) / t["seconds"], 4)})
    return res


if __name__ == "__main__":
    tests = json.loads((ROOT / "meta" / "test_files.json").read_text(encoding="utf-8"))
    tests = sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest())[:LIMIT]
    asr = {}
    if MODE == "offline":
        want = {t["id"] for t in tests}
        for p in (ROOT / "meta" / "asr").glob("*.jsonl"):
            for line in open(p, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r["id"] in want:
                    asr[r["id"]] = r
    jobs = [(t, asr.get(t["id"])) for t in tests if MODE != "offline" or t["id"] in asr]
    t0 = time.time()
    with ProcessPoolExecutor(WORKERS) as pool:
        rows = [r for r in pool.map(work, jobs, chunksize=2) if not r.get("skip")]
    tot = sum(r["total"] for r in rows) or 1
    summary = {"embedding": Path(EMB).name, "mode": MODE, "tau": TAU, "recordings": len(rows),
               "DER": round(sum(r["miss"] + r["fa"] + r["conf"] for r in rows) / tot, 4),
               "miss": round(sum(r["miss"] for r in rows) / tot, 4), "false_alarm": round(sum(r["fa"] for r in rows) / tot, 4),
               "confusion": round(sum(r["conf"] for r in rows) / tot, 4),
               "turn_speaker_accuracy": round(sum(r["turns_right"] for r in rows) / max(1, sum(r["turns_counted"] for r in rows)), 4),
               "minutes": round((time.time() - t0) / 60, 1)}
    if MODE in ("online", "enroll", "recluster"):
        summary["chunk_accuracy"] = round(sum(r["chunks_right"] for r in rows) / max(1, sum(r["chunks_scored"] for r in rows)), 4)
        summary["chunks_mixed_share"] = round(sum(r["chunks_mixed"] for r in rows) / max(1, sum(r["chunks_mixed"] + r["chunks_scored"] for r in rows)), 4)
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
