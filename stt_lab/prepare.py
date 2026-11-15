"""raw/*.zip 속 음성을 16kHz 모노 FLAC으로 풀고 meta/files.jsonl에 목록을 남긴다.

사용 (서버, ~/voiceguard_ft): PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/prepare.py [작업 수=12]
- 압축은 디스크에 풀지 않고 zip 안의 음성을 바로 디코딩한다(faster_whisper.decode_audio = PyAV).
- 파일 이름은 ASCII(<zip 이름>/<NNNN>.flac)로 바꾸고 원래 이름은 files.jsonl의 orig에 둔다.
- 다 푼 zip은 audio16k/<zip 이름>/.done 으로 표시해 다시 돌려도 건너뛴다(업로드 중인 .part는 대상이 아니다).
"""
import io
import json
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path.home() / "voiceguard_ft"
SR = 16000


def fix(name: str) -> str:
    try:
        return name.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def unpack(zip_path: str) -> str:
    import soundfile as sf
    from faster_whisper import decode_audio

    z = Path(zip_path)
    dest = ROOT / "audio16k" / z.stem
    if (dest / ".done").exists():
        return f"skip {z.name}"
    dest.mkdir(parents=True, exist_ok=True)
    names = {}
    bad = 0
    with zipfile.ZipFile(z) as zf:
        members = sorted((m for m in zf.infolist() if not m.is_dir()), key=lambda m: fix(m.filename))
        for i, member in enumerate(members):
            fid = f"{i:04d}"
            try:
                audio = decode_audio(io.BytesIO(zf.read(member)), sampling_rate=SR)
            except Exception:  # 깨진 파일은 건너뛴다
                bad += 1
                continue
            pcm = np.clip(audio, -1.0, 1.0)
            sf.write(dest / f"{fid}.flac", pcm, SR, subtype="PCM_16")
            names[fid] = {"orig": fix(member.filename), "seconds": round(len(pcm) / SR, 2)}
    (dest / "names.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    (dest / ".done").write_text("ok")
    return f"done {z.name}: {len(names)} files, {bad} unreadable, {sum(v['seconds'] for v in names.values()) / 3600:.1f} h"


def write_index(manifest: dict) -> None:
    rows = []
    for d in sorted((ROOT / "audio16k").iterdir()):
        if not (d / ".done").exists():
            continue
        info = manifest[d.name + ".zip"]
        names = json.loads((d / "names.json").read_text(encoding="utf-8"))
        for fid, v in names.items():
            rows.append({
                "id": f"{d.name}/{fid}", "path": str(d / f"{fid}.flac"), "split": info["split"],
                "domain": info["domain"], "category": info["category"], "orig": v["orig"], "seconds": v["seconds"],
            })
    out = ROOT / "meta" / "files.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    hours = sum(r["seconds"] for r in rows) / 3600
    print(f"index: {len(rows)} files, {hours:.1f} h -> {out}", flush=True)


if __name__ == "__main__":
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    manifest = json.loads((ROOT / "meta" / "manifest.json").read_text(encoding="utf-8"))
    zips = sorted(str(p) for p in (ROOT / "raw").glob("*.zip"))
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(unpack, z) for z in zips]
        for f in as_completed(futures):
            print(f.result(), flush=True)
    write_index(manifest)
