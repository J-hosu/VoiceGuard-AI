"""시험 녹음(meta/test_files.json)으로 faster-whisper 모델의 글자 오류율(CER)을 잰다.

- full: 파일 전체 전사 = 서비스의 녹음 파일 분석·통화 종료 후 재전사 설정 (beam 5, VAD, 앞 문맥 끔)
- live: 실시간 조각 = 앱(audioCapture.web.ts)과 같은 무음 자르기 (1.2초 이상 쌓인 뒤 마지막 0.25초가 RMS<0.01이면 자르고,
        10초면 강제로 자름). 조각마다 같은 설정으로 전사해 이어 붙인다.
CER은 공백·문장부호를 빼고 잰다(stt_bench.py와 같음). 정답은 녹음과 맞춘 AI Hub 대본이라 실제 발화와 조금 다를 수 있다.

사용: ~/voiceguard/.venv/bin/python -X utf8 scripts/eval_asr.py <large-v3 | CT2 폴더> <out.json> [도메인별 녹음 수=50] [modes=full,live]
환경 변수: PROFILE=cpu 이면 노트북 서비스 설정(backend/app/mp3_json.py의 CPU 경로)과 같게 돈다:
  int8, 파일 전체 beam 1(WHISPER_BEAM 기본값), 실시간 조각 beam 1·temperature (0.0, 0.4)·출력 128토큰.
  DEVICE, COMPUTE, BEAM_FULL, BEAM_LIVE, CPU_THREADS로 따로 바꿀 수 있다. 결과에 전사 시간(실시간 배수, 조각당 p50/p90)도 남긴다.
"""
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel

ROOT = Path.home() / "voiceguard_ft"
model_path, out_path = sys.argv[1], Path(sys.argv[2])
per_domain = int(sys.argv[3]) if len(sys.argv) > 3 else 50
modes = (sys.argv[4] if len(sys.argv) > 4 else "full,live").split(",")
SR = 16000
MIN_S, MAX_S, SIL_S, SIL_RMS, FRAME = 1.2, 10.0, 0.25, 0.01, 320
_norm = re.compile(r"[^0-9A-Za-z가-힣]")


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


# 서비스(backend/app/mp3_json.py)와 같은 환각 걸러내기
_HALLUCINATION = re.compile(
    r"시청해\s*주셔서|구독(과|\s*좋아요)|좋아요와\s*구독|자막\s*(제공|by)|MBC\s*뉴스|KBS\s*뉴스|다음\s*영상에서"
    r"|이\s*시각\s*세계|뉴스\s*였습니다|한글\s*자막"
)


def _is_hallucination(seg):
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
        return True
    return bool(_HALLUCINATION.search(seg.text))


import os
PROFILE = os.environ.get("PROFILE", "")
DEVICE = os.environ.get("DEVICE", "cpu" if PROFILE == "cpu" else "cuda")
COMPUTE = os.environ.get("COMPUTE", "int8" if DEVICE == "cpu" else "float16")
BEAM_FULL = int(os.environ.get("BEAM_FULL", "1" if PROFILE == "cpu" else "5"))
BEAM_LIVE = int(os.environ.get("BEAM_LIVE", "1" if PROFILE == "cpu" else "5"))
FULL_TEMPS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)          # faster-whisper 기본값
LIVE_TEMPS = (0.0, 0.4) if PROFILE == "cpu" else FULL_TEMPS
LIVE_MAX_NEW = 128 if PROFILE == "cpu" else None
CPU_THREADS = int(os.environ.get("CPU_THREADS", "0"))


def transcribe(model, audio, live=False):
    segments, _ = model.transcribe(
        audio, language="ko", beam_size=BEAM_LIVE if live else BEAM_FULL,
        temperature=LIVE_TEMPS if live else FULL_TEMPS, max_new_tokens=LIVE_MAX_NEW if live else None,
        vad_filter=True, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments if not _is_hallucination(s)).strip()


TEST_FILE = Path(os.environ.get("TEST_FILE", str(ROOT / "meta" / "test_files_frozen.json")))  # 모델 간 비교는 같은 목록으로
tests = json.loads(TEST_FILE.read_text(encoding="utf-8"))
by_dom = defaultdict(list)
for t in sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest()):
    by_dom[t["domain"]].append(t)
chosen = [t for d in sorted(by_dom) for t in by_dom[d][:per_domain]]
print(f"{len(chosen)} recordings, {sum(t['seconds'] for t in chosen) / 3600:.1f} h, modes={modes}, model={model_path}", flush=True)

model = WhisperModel(model_path, device=DEVICE, compute_type=COMPUTE, cpu_threads=CPU_THREADS)
rows, all_chunk_times = [], []
t0 = time.time()
for n, t in enumerate(chosen, 1):
    audio, _ = sf.read(t["path"], dtype="float32")
    row = {"id": t["id"], "domain": t["domain"], "seconds": t["seconds"]}
    if "full" in modes:
        s0 = time.perf_counter()
        hyp = transcribe(model, audio)
        row["full_sec"] = round(time.perf_counter() - s0, 2)
        row["full_edits"], row["ref_len"] = edits(t["ref"], hyp)
        row["full_hyp"] = hyp
    if "live" in modes:
        spans = silence_chunks(audio)
        texts, times = [], []
        for a, b in spans:
            if b - a < SR // 2:
                continue
            s0 = time.perf_counter()
            text = transcribe(model, audio[a:b], live=True)
            times.append(time.perf_counter() - s0)
            all_chunk_times.append(times[-1])
            if text:
                texts.append(text)
        hyp = " ".join(texts)
        row["live_edits"], row["ref_len"] = edits(t["ref"], hyp)
        row["live_hyp"] = hyp
        row["live_chunks"] = len(spans)
        row["live_sec_per_chunk"] = round(float(np.mean(times)), 3) if times else None
    rows.append(row)
    if n % 20 == 0 or n == len(chosen):
        print(f"{n}/{len(chosen)} in {(time.time() - t0) / 60:.1f} min", flush=True)


def agg(rs, mode):
    e = sum(r[f"{mode}_edits"] for r in rs)
    n = sum(r["ref_len"] for r in rs)
    return round(e / max(n, 1), 4)


summary = {"model": model_path, "recordings": len(rows), "hours": round(sum(r["seconds"] for r in rows) / 3600, 2),
           "device": DEVICE, "compute": COMPUTE, "beam_full": BEAM_FULL, "beam_live": BEAM_LIVE, "profile": PROFILE or "gpu",
           "cpu_threads": CPU_THREADS}
if "full" in modes:
    summary["full_rtf"] = round(sum(r["full_sec"] for r in rows) / max(sum(r["seconds"] for r in rows), 1e-6), 3)
if all_chunk_times:
    summary["live_chunk_p50_s"] = round(float(np.percentile(all_chunk_times, 50)), 3)
    summary["live_chunk_p90_s"] = round(float(np.percentile(all_chunk_times, 90)), 3)
for mode in modes:
    summary[f"{mode}_cer"] = agg(rows, mode)
    summary[f"{mode}_cer_by_domain"] = {d: agg([r for r in rows if r["domain"] == d], mode) for d in sorted(by_dom)}
if "live" in modes:
    summary["live_sec_per_chunk"] = round(float(np.mean([r["live_sec_per_chunk"] for r in rows if r.get("live_sec_per_chunk")])), 3)
out_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)
