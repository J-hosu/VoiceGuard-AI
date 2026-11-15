"""리턴제로(RTZR) STT를 로컬 Whisper와 같은 시험 녹음·같은 채점으로 잰다.

- batch:  녹음 파일 전사 API(POST /v1/transcribe, 5초마다 조회). 화자 분리(2명)를 켜서 화자 정확도도 함께 잰다.
- stream: 실시간 WebSocket API. 통화(녹음) 하나에 연결 하나를 열고, 앱과 같은 무음 조각(1.2~10초)을 보낸 뒤
          조각마다 {"type":"Finalize"}로 문장을 확정시켜 결과를 받는다(앱의 조각 방식과 같은 조건).
          조각마다 'Finalize를 보낸 뒤 확정 결과를 받기까지 걸린 시간'을 잰다.
채점: eval_asr.py와 같은 CER(공백·문장부호 제외), 시험 목록(meta/test_files_frozen.json)도 같은 순서로 고른다.
키: ~/voiceguard_ft/rtzr.env 의 RTZR_CLIENT_ID, RTZR_CLIENT_SECRET (출력하지 않는다)

사용: PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/eval_rtzr.py <batch|stream> <out.json> [도메인별 녹음 수] [MODEL=sommers]
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

ROOT = Path.home() / "voiceguard_ft"
API = "https://openapi.vito.ai"
WSS = "wss://openapi.vito.ai/v1/transcribe:streaming"
MODE, OUT = sys.argv[1], Path(sys.argv[2])
PER_DOMAIN = int(sys.argv[3]) if len(sys.argv) > 3 else 25
MODEL = os.environ.get("MODEL", "sommers")
WORKERS = int(os.environ.get("WORKERS", "4"))
SR = 16000
MIN_S, MAX_S, SIL_S, SIL_RMS, FRAME = 1.2, 10.0, 0.25, 0.01, 320
_norm = re.compile(r"[^0-9A-Za-z가-힣]")


def load_key():
    env = {}
    for line in (ROOT / "rtzr.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env["RTZR_CLIENT_ID"], env["RTZR_CLIENT_SECRET"]


_token = {"value": None, "exp": 0}
_lock = threading.Lock()


def token():
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 1800:
            return _token["value"]
        cid, secret = load_key()
        r = requests.post(f"{API}/v1/authenticate", data={"client_id": cid, "client_secret": secret}, timeout=30)
        r.raise_for_status()
        body = r.json()
        _token["value"], _token["exp"] = body["access_token"], float(body.get("expire_at", time.time() + 6 * 3600))
        return _token["value"]


def edits(ref, hyp):
    r, h = _norm.sub("", ref), _norm.sub("", hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1], len(r)


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
    return [(a, b) for a, b in chunks if float(np.sqrt(np.mean(audio[a:b] ** 2))) >= SIL_RMS]


# ---------- 화자 분리 채점 (diar_simple.py와 같은 규칙) ----------
STEP, COLLAR = 0.01, 0.25
SPK = {"agent": 0, "customer": 1}


def diar_score(t, hyp):
    from scipy.optimize import linear_sum_assignment
    n = int(t["seconds"] / STEP) + 1
    ref = np.full(n, -1, dtype=np.int8)
    scored = np.zeros(n, dtype=bool)
    oks = [x for x in t["turns"] if x.get("ok")]
    spans, cur = [], None
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
    return out


# ---------- batch ----------
def run_batch(t):
    config = {"model_name": MODEL, "domain": "CALL", "use_diarization": True, "diarization": {"spk_count": 2},
              "use_itn": True, "use_disfluency_filter": True, "use_profanity_filter": False,
              "use_paragraph_splitter": True}
    if MODEL == "whisper":
        config["language"] = "ko"
    t0 = time.time()
    for attempt in range(5):
        with open(t["path"], "rb") as f:
            r = requests.post(f"{API}/v1/transcribe", headers={"Authorization": f"Bearer {token()}"},
                              files={"file": (Path(t["path"]).name, f, "audio/flac")},
                              data={"config": json.dumps(config)}, timeout=120)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    tid = r.json()["id"]
    while True:
        time.sleep(5)
        g = requests.get(f"{API}/v1/transcribe/{tid}", headers={"Authorization": f"Bearer {token()}"}, timeout=60)
        if g.status_code == 429:
            time.sleep(5)
            continue
        g.raise_for_status()
        body = g.json()
        if body["status"] == "completed":
            break
        if body["status"] == "failed":
            return {"id": t["id"], "error": body.get("error")}
    utts = body["results"]["utterances"]
    hyp = " ".join(u["msg"] for u in utts)
    e, n = edits(t["ref"], hyp)
    row = {"id": t["id"], "domain": t["domain"], "seconds": t["seconds"], "full_edits": e, "ref_len": n,
           "full_hyp": hyp, "wall_s": round(time.time() - t0, 1),
           "n_speakers": len({u["spk"] for u in utts})}
    row.update(diar_score(t, [(u["start_at"] / 1000, (u["start_at"] + u["duration"]) / 1000, int(u["spk"])) for u in utts]))
    return row


# ---------- stream ----------
def run_stream(t):
    from websockets.sync.client import connect
    audio, _ = sf.read(t["path"], dtype="float32")
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    q = (f"?sample_rate={SR}&encoding=LINEAR16&model_name={'sommers_ko' if MODEL == 'sommers' else MODEL}"
         f"&domain=CALL&use_itn=true&use_disfluency_filter=true&use_profanity_filter=false")
    texts, lat, empty = [], [], 0
    with connect(WSS + q, additional_headers={"Authorization": f"Bearer {token()}"}, open_timeout=30,
                 max_size=None) as ws:
        for a, b in silence_chunks(audio):
            if b - a < SR // 2:
                continue
            piece = pcm[a:b].tobytes()
            step = SR // 5 * 2  # 0.2초씩
            for i in range(0, len(piece), step):
                ws.send(piece[i:i + step])
            ws.send(json.dumps({"type": "Finalize"}))
            sent = time.perf_counter()
            got, last = [], None
            deadline = sent + 4.0
            while True:
                timeout = (0.4 if got else max(0.05, deadline - time.perf_counter()))
                try:
                    msg = ws.recv(timeout=timeout)
                except TimeoutError:
                    break
                data = json.loads(msg)
                if data.get("final"):
                    alt = (data.get("alternatives") or [{}])[0]
                    if alt.get("text", "").strip():
                        got.append(alt["text"].strip())
                    last = time.perf_counter()
                if time.perf_counter() > deadline:
                    break
            if got:
                texts.append(" ".join(got))
                lat.append(last - sent)
            else:
                empty += 1
        ws.send("EOS")
        try:
            while True:  # 마지막 결과까지 비운다
                data = json.loads(ws.recv(timeout=2.0))
                if data.get("final"):
                    alt = (data.get("alternatives") or [{}])[0]
                    if alt.get("text", "").strip():
                        texts.append(alt["text"].strip())
        except Exception:
            pass
    hyp = " ".join(texts)
    e, n = edits(t["ref"], hyp)
    return {"id": t["id"], "domain": t["domain"], "seconds": t["seconds"], "live_edits": e, "ref_len": n,
            "live_hyp": hyp, "chunks_with_text": len(lat), "chunks_empty": empty,
            "finalize_latency_mean": round(float(np.mean(lat)), 3) if lat else None,
            "finalize_latency_p90": round(float(np.percentile(lat, 90)), 3) if lat else None}


if __name__ == "__main__":
    tests = json.loads((ROOT / "meta" / "test_files_frozen.json").read_text(encoding="utf-8"))
    by_dom = defaultdict(list)
    for t in sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest()):
        by_dom[t["domain"]].append(t)
    chosen = [t for d in sorted(by_dom) for t in by_dom[d][:PER_DOMAIN]]
    minutes = sum(t["seconds"] for t in chosen) / 60
    print(f"{MODE} model={MODEL}: {len(chosen)} recordings, {minutes:.0f} audio minutes (free quota is 600 min once)", flush=True)
    fn = run_batch if MODE == "batch" else run_stream
    rows, t0 = [], time.time()
    with ThreadPoolExecutor(WORKERS) as pool:
        for i, row in enumerate(pool.map(fn, chosen), 1):
            rows.append(row)
            if i % 10 == 0 or i == len(chosen):
                print(f"{i}/{len(chosen)} in {(time.time() - t0) / 60:.1f} min", flush=True)
    ok = [r for r in rows if "error" not in r]
    key = "full" if MODE == "batch" else "live"
    agg = lambda rs: round(sum(r[f"{key}_edits"] for r in rs) / max(1, sum(r["ref_len"] for r in rs)), 4)
    summary = {"engine": f"rtzr-{MODEL}", "mode": MODE, "recordings": len(ok), "errors": len(rows) - len(ok),
               "audio_minutes": round(minutes, 1), f"{key}_cer": agg(ok),
               f"{key}_cer_by_domain": {d: agg([r for r in ok if r["domain"] == d]) for d in sorted(by_dom)}}
    if MODE == "batch":
        tot = sum(r["total"] for r in ok) or 1
        summary["diar_turn_accuracy"] = round(sum(r["turns_right"] for r in ok) / max(1, sum(r["turns_counted"] for r in ok)), 4)
        summary["diar_DER"] = round(sum(r["miss"] + r["fa"] + r["conf"] for r in ok) / tot, 4)
        summary["wall_s_mean"] = round(float(np.mean([r["wall_s"] for r in ok])), 1)
    else:
        lats = [r["finalize_latency_mean"] for r in ok if r["finalize_latency_mean"] is not None]
        summary["finalize_latency_mean"] = round(float(np.mean(lats)), 3) if lats else None
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
