"""AI Hub 민원(콜센터) 음성 zip을 서버에 ASCII 이름으로 올리기 위한 목록을 만든다.

사용: python -I -X utf8 make_manifest.py <aihub_minwon 폴더> <out.tsv> <out.json>
TSV: ascii_name <TAB> 절대 경로 <TAB> 바이트 (올리는 순서: 검증 → 학습 mp3 → 학습 m4a)
JSON: ascii_name → {split, domain, category, source_dir, path}
"""
import json
import sys
from pathlib import Path

DOMAIN = {"금융보험": "fin", "다산콜센터": "dasan", "질병관리본부": "kdca", "쇼핑": "shop"}
SPLIT = {"1.Training": "train", "2.Validation": "valid"}

root = Path(sys.argv[1])
rows, meta = [], {}
for z in sorted(root.rglob("*.zip")):
    rel = z.relative_to(root)
    parts = rel.parts
    if "원천데이터" not in parts[1]:
        continue  # 라벨은 서버의 data/aihub/minwon.json에 이미 변환돼 있다
    split, src, domain_ko = SPLIT[parts[0]], parts[1], parts[2]
    domain = DOMAIN[domain_ko]
    category = z.stem
    rows.append((split, domain, src, category, z))

order = {"valid": 0, "train": 1}
rows.sort(key=lambda r: (order[r[0]], r[4].suffix, ".m4a" in str(r[2]) or r[1] == "shop", r[1], r[3]))
counter = {}
lines = []
for split, domain, src, category, z in rows:
    key = (split, domain)
    counter[key] = counter.get(key, 0) + 1
    name = f"{split}_{domain}_{counter[key]:02d}.zip"
    meta[name] = {"split": split, "domain": domain, "category": category, "source_dir": src, "path": str(z)}
    lines.append(f"{name}\t{z}\t{z.stat().st_size}")

Path(sys.argv[2]).write_text("\n".join(lines) + "\n", encoding="utf-8")
Path(sys.argv[3]).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
total = sum(int(l.split("\t")[2]) for l in lines)
print(len(lines), "zips,", round(total / 1e9, 1), "GB")
for l in lines[:3] + lines[-3:]:
    print(l)
