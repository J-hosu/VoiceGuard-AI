"""학습 코드 연기 시험(smoke test)용 작은 데이터: 평가용(valid) 정렬 결과로 예제 몇백 개를 meta_smoke/에 만든다.
진짜 학습에는 쓰지 않는다(평가용 녹음이라). 코드가 끝까지 도는지만 본다.
사용: ~/voiceguard/.venv/bin/python -X utf8 scripts/smoke.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import examples_for  # noqa: E402

ROOT = Path.home() / "voiceguard_ft"
files = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "meta" / "files.jsonl", encoding="utf-8")}
rows = [json.loads(l) for l in open(ROOT / "meta" / "aligned.jsonl", encoding="utf-8")]
rows = [r for r in rows if r.get("turns") and r["split"] == "valid"][:30]
exs = []
for r in rows:
    for e in examples_for(r, files[r["id"]]["path"]):
        e["rec"], e["domain"] = r["id"], r["domain"]
        exs.append(e)
out = ROOT / "meta_smoke"
out.mkdir(exist_ok=True)
train, dev = exs[: len(exs) - 40], exs[-40:]
(out / "ds_train.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in train), encoding="utf-8")
(out / "ds_dev.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in dev), encoding="utf-8")
kinds = {k: sum(1 for e in exs if e["kind"] == k) for k in ("A", "B")}
print(len(train), "train /", len(dev), "dev", kinds, "B with ts:", sum(1 for e in exs if e.get("ts")))
print(json.dumps(next(e for e in exs if e["kind"] == "B" and e.get("ts")), ensure_ascii=False)[:600])
