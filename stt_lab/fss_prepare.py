"""금감원 공개 사기 녹취(서버 ~/dataset/**/*.mp3)를 16kHz 모노 FLAC으로 바꾸고 meta/files_fss.jsonl에 목록을 남긴다.

통화 분류기를 '서비스와 같은 Whisper 전사 말투'로 다시 학습하려고 쓴다(backend/app/build_dataset_v4.py).
파일 이름은 ASCII(fss/NNNN.flac)로 바꾸고 원래 이름은 orig에 둔다.
사용: PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/fss_prepare.py
"""
import glob
import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf
from faster_whisper import decode_audio

ROOT = Path.home() / "voiceguard_ft"
SR = 16000
dest = ROOT / "audio16k" / "fss"
dest.mkdir(parents=True, exist_ok=True)
files = sorted(glob.glob(os.path.expanduser("~/dataset/**/*.mp3"), recursive=True))
rows = []
for i, f in enumerate(files):
    fid = f"{i:04d}"
    out = dest / f"{fid}.flac"
    if not out.exists():
        try:
            audio = decode_audio(f, sampling_rate=SR)
        except Exception:
            continue
        if len(audio) < SR:
            continue
        sf.write(out, np.clip(audio, -1, 1), SR, subtype="PCM_16")
    info = sf.info(out)
    rows.append({"id": f"fss/{fid}", "path": str(out), "split": "fss", "domain": "fss",
                 "orig": os.path.relpath(f, os.path.expanduser("~/dataset")), "seconds": round(info.frames / SR, 2)})
(ROOT / "meta" / "files_fss.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
print(len(rows), "files,", round(sum(r["seconds"] for r in rows) / 3600, 1), "h")
