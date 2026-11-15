"""로컬 화자 분리 모듈(backend/app/services/local_diarization.py)을 이 PC(CPU)에서 시험한다.

정답은 서버에서 정렬한 시험 녹음의 문장 시각·역할(test_files_frozen.json), 음성은 이 PC의 AI Hub zip에서 바로 읽는다.
지표: 통화 후 문장 단위 화자 정확도, 실시간(조각마다 다시 묶기) 문장 단위 정확도, CPU 처리 시간(실시간 배수).

사용: .venv/Scripts/python.exe -X utf8 stt_lab/diar_local_test.py <test_files_frozen.json> <files.jsonl> <manifest.json> [녹음 수=8]
"""
import hashlib
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.services import local_diarization as LD  # noqa: E402
from faster_whisper import decode_audio  # noqa: E402

tests_path, files_path, manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
N = int(sys.argv[4]) if len(sys.argv) > 4 else 8
SPK = {"agent": 0, "customer": 1}
STEP, COLLAR = 0.01, 0.25


def fix(n):
    try:
        return n.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return n


def turn_accuracy(t, hyp):
    n = int(t["seconds"] / STEP) + 1
    lab = np.full(n, -1, dtype=np.int16)
    for a, b, k in hyp:
        lab[int(a / STEP):min(n, int(b / STEP) + 1)] = k
    conf = np.zeros((2, 2), dtype=np.int64)
    for x in t["turns"]:
        if not x.get("ok"):
            continue
        seg = lab[int((x["start"] + COLLAR) / STEP):int((x["end"] - COLLAR) / STEP) + 1]
        seg = seg[seg >= 0]
        if len(seg):
            conf[SPK.get(x["spk"], 0), int(np.bincount(seg, minlength=2).argmax())] += 1
    r, c = linear_sum_assignment(-conf)
    return int(conf[r, c].sum()), int(conf.sum())


tests = json.loads(Path(tests_path).read_text(encoding="utf-8"))
orig = {json.loads(l)["id"]: json.loads(l)["orig"] for l in open(files_path, encoding="utf-8")}
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
by_dom = defaultdict(list)
for t in sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest()):
    by_dom[t["domain"]].append(t)
chosen = [t for d in sorted(by_dom) for t in by_dom[d][:max(1, N // 4)]]

tot = {"off": [0, 0], "on": [0, 0]}
cpu, audio_s = 0.0, 0.0
for t in chosen:
    zip_name, _fid = t["id"].split("/")
    with zipfile.ZipFile(manifest[zip_name + ".zip"]["path"]) as zf:
        member = next(m for m in zf.infolist() if fix(m.filename) == orig[t["id"]])
        audio = decode_audio(io.BytesIO(zf.read(member)), sampling_rate=LD.SR)
    t0 = time.perf_counter()
    off = LD.diarize(audio)
    od = LD.OnlineDiarizer()
    on = []
    for s, e in LD.silence_chunks(audio):
        on.append((s, e, od.add(audio[int(s * LD.SR):int(e * LD.SR)], s, e)))  # 그 순간 화면에 붙는 화자
    took = time.perf_counter() - t0
    cpu += took
    audio_s += len(audio) / LD.SR
    for key, hyp in (("off", off), ("on", on)):
        r, c = turn_accuracy(t, hyp)
        tot[key][0] += r
        tot[key][1] += c
    print(f"{t['id']} {len(audio) / LD.SR:5.1f}초 | 통화 후 {turn_accuracy(t, off)} | 실시간 {turn_accuracy(t, on)} | CPU {took:.1f}초", flush=True)

print(json.dumps({
    "recordings": len(chosen), "audio_minutes": round(audio_s / 60, 1),
    "offline_turn_accuracy": round(tot["off"][0] / max(1, tot["off"][1]), 4),
    "online_turn_accuracy": round(tot["on"][0] / max(1, tot["on"][1]), 4),
    "cpu_seconds_total": round(cpu, 1), "cpu_realtime_factor": round(cpu / max(audio_s, 1e-6), 3),
}, ensure_ascii=False, indent=1))
