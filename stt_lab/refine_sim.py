"""실시간 화면에 화자 구분까지 보여 주는 방법 비교: 기존 실시간 조각 방식 vs '뒤따라 다시 받아 적기' vs 통화 후 파일 전체.

같은 녹음으로 세 방식을 재고, 엔진은 리턴제로(rtzr) 또는 로컬(local)을 고른다.
- live   : 앱과 같은 무음 조각(1.2~10초)을 조각마다 받아 적는다(지금 방식). 화자 구분 없음.
- refine : 최근 LIVE초는 live 결과를 보여 주고, STEP초마다 'LIVE초 넘게 지난 마지막 조각 끝'까지를 다시 받아 적어 바꿔 끼운다.
           요청 구간은 [마지막 정리 끝 - CONTEXT, 새 끝]: 앞 CONTEXT초를 문맥으로 같이 보내 화자와 문맥을 잇는다.
           새 구간의 단어만 붙이고(단어 시각 기준), 문맥 구간에서 이번 결과의 화자와 지금까지 붙인 화자가 겹친 시간으로 번호를 맞춘다.
           · rtzr : 파일 전사 API(화자 분리, 구간 20초 이상이면 2명 지정, 단어 시각 켬)
           · local: 로컬 Whisper(단어 시각) + 로컬 화자 분리(지금까지 무음 조각 전체를 3D-Speaker 임베딩으로 2명 다시 묶기)
- full   : 통화가 끝난 뒤 파일 전체를 받아 적고 화자를 나눈다(rtzr 파일 전사 / local Whisper + 로컬 화자 분리).
지표: CER, 문장 단위 화자 정확도, 정리 요청 하나 처리 시간, 정리본이 붙을 때 그 구간 끝이 지난 시간, 리턴제로 과금 배수.

사용: PYTHONPATH=pylib:pylib_sherpa ~/voiceguard/.venv/bin/python -X utf8 scripts/refine_sim.py <rtzr|local> <out.json> [녹음 수=8]
환경 변수: STEP(15) LIVE(20) CONTEXT(40) MODEL(local Whisper, 기본 large-v3)
"""
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import requests
import soundfile as sf
from scipy.optimize import linear_sum_assignment

ROOT = Path.home() / "voiceguard_ft"
MODE, OUT = sys.argv[1], Path(sys.argv[2])
N_REC = int(sys.argv[3]) if len(sys.argv) > 3 else 8
STEP = float(os.environ.get("STEP", "15"))
LIVE = float(os.environ.get("LIVE", "20"))
CONTEXT = float(os.environ.get("CONTEXT", "40"))
SR = 16000
API = "https://openapi.vito.ai"
WSS = "wss://openapi.vito.ai/v1/transcribe:streaming"
_norm = re.compile(r"[^0-9A-Za-z가-힣]")
MIN_S, MAX_S, SIL_S, SIL_RMS, FRAME = 1.2, 10.0, 0.25, 0.01, 320
_HALLUCINATION = re.compile(r"시청해\s*주셔서|구독(과|\s*좋아요)|좋아요와\s*구독|자막\s*(제공|by)|MBC\s*뉴스|KBS\s*뉴스|다음\s*영상에서"
                            r"|이\s*시각\s*세계|뉴스\s*였습니다|한글\s*자막")


def edits(ref, hyp):
    r, h = _norm.sub("", ref), _norm.sub("", hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1], len(r)


STEP_F, COLLAR = 0.01, 0.25
SPK = {"agent": 0, "customer": 1}


def turn_accuracy(t, hyp):
    n = int(t["seconds"] / STEP_F) + 1
    lab = np.full(n, -1, dtype=np.int16)
    for a, b, l in sorted(hyp, key=lambda h: h[1] - h[0], reverse=True):
        lab[int(a / STEP_F):min(n, int(b / STEP_F) + 1)] = l
    k = max(int(lab.max()) + 1, 1)
    conf = np.zeros((2, k), dtype=np.int64)
    for x in t["turns"]:
        if not x.get("ok"):
            continue
        seg = lab[int((x["start"] + COLLAR) / STEP_F):int((x["end"] - COLLAR) / STEP_F) + 1]
        seg = seg[seg >= 0]
        if len(seg):
            conf[SPK.get(x["spk"], 0), int(np.bincount(seg).argmax())] += 1
    r_idx, c_idx = linear_sum_assignment(-conf)
    return int(conf[r_idx, c_idx].sum()), int(conf.sum())


def silence_chunks(audio, offset=0.0):
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
    return [(offset + a / SR, offset + b / SR) for a, b in chunks if float(np.sqrt(np.mean(audio[a:b] ** 2))) >= SIL_RMS]


def plan(chunks, duration):
    """[(요청 시각, 요청 시작, 요청 끝, 이전 정리 끝)] — 끝은 항상 실시간 조각 경계(무음)"""
    ends = [e for _s, e in chunks]
    out, done, t = [], 0.0, STEP
    while t < duration:
        cands = [e for e in ends if done + 5.0 < e <= t - LIVE]
        if cands:
            b = max(cands)
            out.append((t, max(0.0, done - CONTEXT), b, done))
            done = b
        t += STEP
    if duration - done > 0.5:
        out.append((duration, max(0.0, done - CONTEXT), duration, done))
    return out


def map_speakers(words_ctx, accepted):
    """문맥 구간에서 이번 결과의 화자(지역 번호)와 지금까지 붙인 화자(전역 번호)가 겹친 시간으로 번호를 맞춘다"""
    local_ids = sorted({k for *_x, k in words_ctx})
    glob_ids = sorted({g for *_x, g in accepted})
    if not accepted or not local_ids:
        return {}
    ov = np.zeros((len(local_ids), len(glob_ids)))
    for s, e, _w, k in words_ctx:
        for gs, ge, _t, g in accepted:
            o = min(e, ge) - max(s, gs)
            if o > 0:
                ov[local_ids.index(k), glob_ids.index(g)] += o
    mapping = {}
    if ov.sum() > 0:
        r_idx, c_idx = linear_sum_assignment(-ov)
        mapping = {local_ids[r]: glob_ids[c] for r, c in zip(r_idx, c_idx) if ov[r, c] > 0}
    for k in local_ids:
        if k not in mapping:
            free = [g for g in glob_ids if g not in mapping.values()]
            mapping[k] = free[0] if free else max(glob_ids) + 1
    return mapping


def group_words(words):
    """같은 화자의 이어진 단어를 발화로 묶는다: [(시작, 끝, 글, 화자)]"""
    out = []
    for s, e, w, k in words:
        if out and out[-1][3] == k and s - out[-1][1] < 1.0:
            ps, _pe, pt, _pk = out[-1]
            out[-1] = (ps, e, (pt + " " + w).strip(), k)
        else:
            out.append((s, e, w.strip(), k))
    return out


# ---------------- 리턴제로 ----------------
_tok = {"v": None, "exp": 0}


def token():
    if _tok["v"] and time.time() < _tok["exp"] - 1800:
        return _tok["v"]
    env = dict(l.split("=", 1) for l in (ROOT / "rtzr.env").read_text().splitlines() if "=" in l)
    r = requests.post(f"{API}/v1/authenticate", data={"client_id": env["RTZR_CLIENT_ID"], "client_secret": env["RTZR_CLIENT_SECRET"]}, timeout=30)
    r.raise_for_status()
    _tok["v"], _tok["exp"] = r.json()["access_token"], float(r.json().get("expire_at", time.time() + 6 * 3600))
    return _tok["v"]


def rtzr_batch(piece, spk_count):
    """파일 전사 API → 단어 [(시작, 끝, 글, 화자)] (구간 기준 시각), 처리 시간"""
    buf = io.BytesIO()
    sf.write(buf, piece, SR, format="FLAC", subtype="PCM_16")
    cfg = {"model_name": "sommers", "domain": "CALL", "use_diarization": True, "diarization": {"spk_count": spk_count},
           "use_itn": True, "use_disfluency_filter": True, "use_paragraph_splitter": False, "use_word_timestamp": True}
    t0 = time.time()
    for attempt in range(6):
        r = requests.post(f"{API}/v1/transcribe", headers={"Authorization": f"Bearer {token()}"},
                          files={"file": ("w.flac", buf.getvalue(), "audio/flac")}, data={"config": json.dumps(cfg)}, timeout=120)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    tid = r.json()["id"]
    while True:
        time.sleep(0.7)
        g = requests.get(f"{API}/v1/transcribe/{tid}", headers={"Authorization": f"Bearer {token()}"}, timeout=60)
        if g.status_code == 429:
            time.sleep(2)
            continue
        g.raise_for_status()
        body = g.json()
        if body["status"] in ("completed", "failed"):
            break
    took = time.time() - t0
    words = []
    for u in body.get("results", {}).get("utterances", []) if body["status"] == "completed" else []:
        spk = int(u.get("spk", 0))
        ws = u.get("words") or []
        if ws:
            for w in ws:
                s = w.get("start_at", u["start_at"]) / 1000
                d = w.get("duration", 0) / 1000
                words.append((s, s + d, w.get("text", "").strip(), spk))
        else:  # 단어 시각이 없으면 글자 수 비율로 나눈다
            toks = u["msg"].split()
            total = sum(len(x) for x in toks) or 1
            cur = u["start_at"] / 1000
            for x in toks:
                d = u["duration"] / 1000 * len(x) / total
                words.append((cur, cur + d, x, spk))
                cur += d
    return [w for w in words if w[2]], took, body.get("results", {}).get("utterances", [])[:1]


def rtzr_stream_live(audio, chunks):
    from websockets.sync.client import connect
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    q = f"?sample_rate={SR}&encoding=LINEAR16&model_name=sommers_ko&domain=CALL&use_itn=true&use_disfluency_filter=true"
    texts = []
    with connect(WSS + q, additional_headers={"Authorization": f"Bearer {token()}"}, open_timeout=30, max_size=None) as ws:
        for a, b in chunks:
            if b - a < 0.5:
                continue
            piece = pcm[int(a * SR):int(b * SR)].tobytes()
            for i in range(0, len(piece), 6400):
                ws.send(piece[i:i + 6400])
            ws.send(json.dumps({"type": "Finalize"}))
            got, deadline = [], time.perf_counter() + 4.0
            while True:
                try:
                    msg = json.loads(ws.recv(timeout=0.4 if got else max(0.05, deadline - time.perf_counter())))
                except TimeoutError:
                    break
                if msg.get("final"):
                    tx = (msg.get("alternatives") or [{}])[0].get("text", "").strip()
                    if tx:
                        got.append(tx)
                if time.perf_counter() > deadline:
                    break
            texts.extend(got)
        ws.send("EOS")
    return " ".join(texts)


def run_rtzr(t, audio):
    chunks = silence_chunks(audio)
    skip = os.environ.get("SKIP_BASE") == "1"
    res = {"live_text": "" if skip else rtzr_stream_live(audio, chunks)}
    # refine
    accepted, lat, lags, billed = [], [], [], 0.0
    for req_t, a, b, done in plan(chunks, t["seconds"]):
        piece = audio[int(a * SR):int(b * SR)]
        if len(piece) < SR // 2:
            continue
        words, took, _ = rtzr_batch(piece, 2 if b - a >= float(os.environ.get("SPK_MIN_S", "20")) else 0)
        lat.append(took)
        lags.append(req_t + took - b)
        billed += max(10.0, b - a)
        words = [(s + a, e + a, w, k) for s, e, w, k in words]
        ctx = [w for w in words if w[0] < done - 0.05]
        new = [w for w in words if w[0] >= done - 0.05]
        mapping = map_speakers(ctx, accepted)
        if os.environ.get("RELABEL") == "1" and ctx:
            for i, (gs, ge, gt, gk) in enumerate(accepted):
                if ge <= a:
                    continue
                votes = defaultdict(float)
                for s, e, _w, k in ctx:
                    o = min(e, ge) - max(s, gs)
                    if o > 0:
                        votes[mapping.get(k, k)] += o
                if votes:
                    accepted[i] = (gs, ge, gt, max(votes, key=votes.get))
        accepted.extend(group_words([(s, e, w, mapping.get(k, k)) for s, e, w, k in new]))
    res.update({"refine": accepted, "lat": lat, "lags": lags, "billed_ratio": billed / t["seconds"]})
    # full
    if skip:
        res.update({"full": [], "full_took": 0.0})
        return res
    words, took, _ = rtzr_batch(audio, 2)
    res.update({"full": group_words(words), "full_took": took})
    return res


# ---------------- 로컬 ----------------
_whisper = None
_ext = None


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(os.environ.get("MODEL", "large-v3"), device="cuda", compute_type="float16")
    return _whisper


def wtranscribe(piece, words=False):
    segs, _ = whisper().transcribe(piece, language="ko", beam_size=5, vad_filter=True,
                                   condition_on_previous_text=False, word_timestamps=words)
    return [s for s in segs if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) and not _HALLUCINATION.search(s.text)]


def embed(piece):
    global _ext
    if _ext is None:
        import sherpa_onnx
        emb = str(ROOT / "diar_models" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
        _ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=2))
    s = _ext.create_stream()
    s.accept_waveform(SR, piece)
    s.input_finished()
    v = np.array(_ext.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def cluster(embs, spans, prev_cent):
    from sklearn.cluster import KMeans
    if len(embs) < 2:
        return [0] * len(embs), prev_cent
    E = np.stack(embs)
    w = np.array([e - s for s, e in spans])
    km = KMeans(n_clusters=2, n_init=4, random_state=0).fit(E, sample_weight=w)
    cent = km.cluster_centers_ / np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True)
    if prev_cent is not None:
        sim = cent @ prev_cent.T
        if sim[0, 1] + sim[1, 0] > sim[0, 0] + sim[1, 1]:
            cent = cent[[1, 0]]
    return [int(np.argmax(cent @ e)) for e in embs], cent


def label_words(words, spans, labels):
    out = []
    for s, e, w in words:
        mid = (s + e) / 2
        j = min(range(len(spans)), key=lambda i: 0.0 if spans[i][0] <= mid <= spans[i][1]
                else min(abs(mid - spans[i][0]), abs(mid - spans[i][1])))
        out.append((s, e, w, labels[j]))
    return out


def run_local(t, audio):
    chunks = silence_chunks(audio)
    live = []
    for a, b in chunks:
        if b - a >= 0.5:
            live += [s.text.strip() for s in wtranscribe(audio[int(a * SR):int(b * SR)])]
    res = {"live_text": " ".join(x for x in live if x)}
    # refine
    accepted, lat, lags = [], [], []
    spans, embs, cent = [], [], None
    for req_t, a, b, done in plan(chunks, t["seconds"]):
        t0 = time.time()
        segs = wtranscribe(audio[int(a * SR):int(b * SR)], words=True)
        words = [(w.start + a, w.end + a, w.word) for s in segs for w in (s.words or [])]
        for cs, ce in [c for c in chunks if c[0] >= done - 0.05 and c[1] <= b + 0.05]:
            if ce - cs >= 0.6:
                spans.append((cs, ce))
                embs.append(embed(audio[int(cs * SR):int(ce * SR)]))
        labels, cent = cluster(embs, spans, cent)
        new = [w for w in words if w[0] >= done - 0.05]
        if spans:
            accepted.extend(group_words(label_words(new, spans, labels)))
        took = time.time() - t0
        lat.append(took)
        lags.append(req_t + took - b)
    res.update({"refine": accepted, "lat": lat, "lags": lags, "billed_ratio": 0.0})
    # full
    t0 = time.time()
    segs = wtranscribe(audio, words=True)
    words = [(w.start, w.end, w.word) for s in segs for w in (s.words or [])]
    fs = [(a, b) for a, b in chunks if b - a >= 0.6]
    fe = [embed(audio[int(a * SR):int(b * SR)]) for a, b in fs]
    labels, _ = cluster(fe, fs, None)
    res.update({"full": group_words(label_words(words, fs, labels)) if fs else [], "full_took": time.time() - t0})
    return res


if __name__ == "__main__":
    tests = json.loads((ROOT / "meta" / "test_files_frozen.json").read_text(encoding="utf-8"))
    by_dom = defaultdict(list)
    for t in sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest()):
        if 50 <= t["seconds"] <= 240:
            by_dom[t["domain"]].append(t)
    chosen = [t for d in sorted(by_dom) for t in by_dom[d][:max(1, N_REC // 4)]]
    rows = []
    for t in chosen:
        audio, _ = sf.read(t["path"], dtype="float32")
        r = (run_rtzr if MODE == "rtzr" else run_local)(t, audio)
        row = {"id": t["id"], "domain": t["domain"], "seconds": t["seconds"]}
        e, n = edits(t["ref"], r["live_text"])
        row.update({"ref_len": n, "live_edits": e})
        for k in ("refine", "full"):
            segs = sorted(r[k], key=lambda x: x[0])
            e, _ = edits(t["ref"], " ".join(x[2] for x in segs))
            right, counted = turn_accuracy(t, [(s, e2, kk) for s, e2, _w, kk in segs])
            row.update({f"{k}_edits": e, f"{k}_turns_right": right, f"{k}_turns_counted": counted,
                        f"{k}_speakers": len({x[3] for x in segs})})
        row.update({"requests": len(r["lat"]), "proc_mean": round(float(np.mean(r["lat"])), 2) if r["lat"] else None,
                    "proc_max": round(float(max(r["lat"])), 2) if r["lat"] else None,
                    "lag_mean": round(float(np.mean(r["lags"][:-1] or r["lags"])), 1) if r["lags"] else None,
                    "billed_ratio": round(r["billed_ratio"], 2), "full_took": round(r["full_took"], 2)})
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def cer(k):
        return round(sum(r[f"{k}_edits"] for r in rows) / max(1, sum(r["ref_len"] for r in rows)), 4)

    def acc(k):
        return round(sum(r[f"{k}_turns_right"] for r in rows) / max(1, sum(r[f"{k}_turns_counted"] for r in rows)), 4)

    summary = {
        "engine": "rtzr sommers" if MODE == "rtzr" else os.environ.get("MODEL", "large-v3"),
        "step": STEP, "live": LIVE, "context": CONTEXT, "recordings": len(rows),
        "audio_minutes": round(sum(r["seconds"] for r in rows) / 60, 1),
        "live_cer": cer("live"), "refine_cer": cer("refine"), "full_cer": cer("full"),
        "refine_speaker_accuracy": acc("refine"), "full_speaker_accuracy": acc("full"),
        "refine_proc_mean_s": round(float(np.mean([r["proc_mean"] for r in rows if r["proc_mean"]])), 2),
        "refine_proc_max_s": round(float(max(r["proc_max"] for r in rows if r["proc_max"])), 2),
        "refine_lag_mean_s": round(float(np.mean([r["lag_mean"] for r in rows if r["lag_mean"] is not None])), 1),
        "refine_billed_ratio": round(float(np.mean([r["billed_ratio"] for r in rows])), 2),
    }
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
