"""이 PC(노트북 CPU)에서 서비스 전사 경로(backend/app/mp3_json.transcribe_audio)의 속도를 잰다.

정확도(CER)는 서버 CPU에서 같은 설정으로 쟀고(run_cpu_eval.sh), 여기서는 노트북에서의 시간만 본다.
- 실시간: 앱과 같은 무음 기준 조각을 임시 WAV로 써서 transcribe_audio(realtime=True)에 넣는다(서비스와 같은 경로).
- 파일 전체: 고른 녹음 중 가장 긴 것을 transcribe_audio(realtime=False)로 받아 적는다.
모델·장치는 서비스와 같은 환경 변수로 정한다: WHISPER_DEVICE=cpu WHISPER_MODEL=<CT2 폴더> WHISPER_COMPUTE=int8 [WHISPER_CPU_THREADS]

사용: .venv/Scripts/python.exe -X utf8 stt_lab/cpu_latency_local.py <test_files_frozen.json> <files.jsonl> <manifest.json> <out.json> [녹음 수=6]
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import mp3_json  # noqa: E402
from backend.app.services.local_diarization import SR, silence_chunks  # noqa: E402  (앱과 같은 무음 기준)
from faster_whisper import decode_audio  # noqa: E402

tests_path, files_path, manifest_path, out_path = sys.argv[1:5]
N = int(sys.argv[5]) if len(sys.argv) > 5 else 6


def fix(n):
    try:
        return n.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return n


tests = json.loads(Path(tests_path).read_text(encoding="utf-8"))
orig = {json.loads(l)["id"]: json.loads(l)["orig"] for l in open(files_path, encoding="utf-8")}
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
by_dom = defaultdict(list)
for t in sorted(tests, key=lambda t: hashlib.md5(t["id"].encode()).hexdigest()):
    by_dom[t["domain"]].append(t)
chosen = [t for d in sorted(by_dom) for t in by_dom[d][:max(1, N // 4)]][:N]
if os.environ.get("ONLY_ID"):  # 녹음 하나만 빠르게 다시 잴 때
    chosen = [t for t in tests if t["id"] == os.environ["ONLY_ID"]]

runtime = mp3_json.resolve_runtime()
t0 = time.perf_counter()
mp3_json.load_model()
load_s = time.perf_counter() - t0
print(f"model {runtime['model']} ({runtime['device']}, {runtime['compute_type']}, threads={runtime['cpu_threads'] or 'auto'}) load {load_s:.1f}s", flush=True)

chunk_times, chunk_lens, audios = [], [], []
tmpdir = Path(tempfile.mkdtemp(prefix="vg_lat_"))
for t in chosen:
    zip_name = t["id"].split("/")[0]
    with zipfile.ZipFile(manifest[zip_name + ".zip"]["path"]) as zf:
        member = next(m for m in zf.infolist() if fix(m.filename) == orig[t["id"]])
        audio = decode_audio(io.BytesIO(zf.read(member)), sampling_rate=SR)
    audios.append((t["id"], audio))
    took = []
    for s, e in silence_chunks(audio):
        if e - s < 0.5:
            continue
        p = tmpdir / "chunk.wav"
        sf.write(p, audio[int(s * SR):int(e * SR)], SR)
        a = time.perf_counter()
        mp3_json.transcribe_audio(str(p), realtime=True)
        took.append(time.perf_counter() - a)
        chunk_lens.append(e - s)
    chunk_times += took
    print(f"{t['id']} {len(audio) / SR:6.1f}s  chunks {len(took)}  p50 {np.percentile(took, 50):.2f}s  p90 {np.percentile(took, 90):.2f}s", flush=True)

rid, longest = max(audios, key=lambda x: len(x[1]))
p = tmpdir / "full.wav"
sf.write(p, longest, SR)
a = time.perf_counter()
mp3_json.transcribe_audio(str(p), realtime=False)
full_s = time.perf_counter() - a
dur = len(longest) / SR
print(f"full file {rid}: {dur:.0f}s audio in {full_s:.1f}s (x{full_s / dur:.3f})", flush=True)

ct = np.array(chunk_times)
summary = {
    "model": runtime["model"], "device": runtime["device"], "compute": runtime["compute_type"],
    "cpu_threads": runtime["cpu_threads"] or "auto", "logical_cpus": os.cpu_count(), "model_load_s": round(load_s, 1),
    "recordings": len(chosen), "chunks": len(ct), "chunk_audio_mean_s": round(float(np.mean(chunk_lens)), 2),
    "chunk_p50_s": round(float(np.percentile(ct, 50)), 3), "chunk_p90_s": round(float(np.percentile(ct, 90)), 3),
    "chunk_max_s": round(float(ct.max()), 3), "chunk_mean_s": round(float(ct.mean()), 3),
    "realtime_share_over_2s": round(float(np.mean(ct > 2.0)), 3),
    "full_file_id": rid, "full_file_audio_s": round(dur, 1), "full_file_s": round(full_s, 1), "full_file_rtf": round(full_s / dur, 3),
}
Path(out_path).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=1))
