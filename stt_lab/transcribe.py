"""meta/files.jsonl의 FLAC을 faster-whisper large-v3(배치 추론)로 받아 적어 단어 시각까지 meta/asr/<zip 이름>.jsonl에 쌓는다.

정답 대본을 음성의 어느 구간에 맞출지 정하는 용도라(정렬용), 배치 추론 + beam 1로 빠르게 돈다.
실서비스 설정(beam 5, 비배치)과의 비교 평가는 eval_asr.py가 따로 한다.

사용 (서버, ~/voiceguard_ft):
  PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/transcribe.py [valid|train|all] [batch=24] [limit]
이미 받아 적은 파일은 건너뛴다(중간에 멈춰도 이어서 돈다).
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import soundfile as sf
from faster_whisper import BatchedInferencePipeline, WhisperModel

ROOT = Path.home() / "voiceguard_ft"
want = sys.argv[1] if len(sys.argv) > 1 else "all"
batch = int(sys.argv[2]) if len(sys.argv) > 2 else 24
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
# 여러 프로세스로 나눠 돌릴 때: SHARD=k/n (zip 단위로 나눠 같은 출력 파일에 두 프로세스가 쓰지 않게 한다)
shard_k, shard_n = (int(x) for x in os.environ.get("SHARD", "0/1").split("/"))

files = [json.loads(line) for line in open(ROOT / "meta" / "files.jsonl", encoding="utf-8")]
files = [f for f in files if want == "all" or f["split"] == want]
only = os.environ.get("ONLY_ZIPS")  # 예: "train_shop_.*|train_kdca_0[169]|train_kdca_10" (나중에 올라온 zip만)
if only:
    import re
    files = [f for f in files if re.fullmatch(only, f["id"].split("/")[0])]
zips = sorted({f["id"].split("/")[0] for f in files})
mine = {z for i, z in enumerate(zips) if i % shard_n == shard_k}
files = [f for f in files if f["id"].split("/")[0] in mine]
max_per_zip = int(os.environ.get("MAX_PER_ZIP", "0"))  # zip마다 앞에서 N개만 (해시 순서로 섞어서)
if max_per_zip:
    import hashlib
    picked = defaultdict(list)
    for f in sorted(files, key=lambda f: hashlib.md5(f["id"].encode()).hexdigest()):
        if len(picked[f["id"].split("/")[0]]) < max_per_zip:
            picked[f["id"].split("/")[0]].append(f)
    files = [f for group in picked.values() for f in group]
out_dir = ROOT / "meta" / "asr"
out_dir.mkdir(parents=True, exist_ok=True)
done = set()
for p in out_dir.glob("*.jsonl"):
    for line in open(p, encoding="utf-8"):
        try:
            done.add(json.loads(line)["id"])
        except json.JSONDecodeError:
            pass
todo = [f for f in files if f["id"] not in done]
if limit:
    todo = todo[:limit]
print(f"{len(todo)} files to transcribe ({sum(f['seconds'] for f in todo) / 3600:.1f} h), {len(done)} already done", flush=True)

model = WhisperModel("large-v3", device="cuda", compute_type="float16")
pipe = BatchedInferencePipeline(model=model)
handles = defaultdict(lambda: None)
t0 = time.time()
audio_sec = 0.0
for n, f in enumerate(todo, 1):
    audio, sr = sf.read(f["path"], dtype="float32")
    segments, _ = pipe.transcribe(audio, language="ko", batch_size=batch, beam_size=1, word_timestamps=True)
    segs = []
    for s in segments:
        words = [[w.word, round(w.start, 2), round(w.end, 2), round(w.probability, 3)] for w in (s.words or [])]
        segs.append({"s": round(s.start, 2), "e": round(s.end, 2), "t": s.text.strip(), "w": words})
    zip_name = f["id"].split("/")[0]
    if handles[zip_name] is None:
        handles[zip_name] = open(out_dir / f"{zip_name}.jsonl", "a", encoding="utf-8")
    handles[zip_name].write(json.dumps({"id": f["id"], "seconds": f["seconds"], "segments": segs}, ensure_ascii=False) + "\n")
    handles[zip_name].flush()
    audio_sec += f["seconds"]
    if n % 25 == 0 or n == len(todo):
        el = time.time() - t0
        print(f"{n}/{len(todo)} files, {audio_sec / 3600:.2f} h audio in {el / 60:.1f} min ({audio_sec / max(el, 1e-6):.0f}x realtime)", flush=True)
for h in handles.values():
    if h:
        h.close()
