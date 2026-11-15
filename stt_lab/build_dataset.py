"""정렬 결과(meta/aligned.jsonl)로 Whisper 추가 학습·평가 데이터를 만든다.

- 시험(test): 평가용(valid) 녹음 중 대본 일치 점수 0.5 이상·대본 포함률 0.8 이상 → eval_asr.py, diar_eval.py
- 학습(train): 학습용(train) 녹음 중 대본과 맞은 것. 시험 녹음과 같은 대화(같은 대화 번호 또는 같은 글)를 읽은 녹음은 뺀다.
- 개발(dev): 학습용 녹음 가운데 대화 번호 기준 3%를 떼어 학습 중 손실·CER을 본다(학습에는 안 씀).
- 예제 A: 문장 하나(실시간 조각처럼 짧은 음성) → 그 문장, 시각 토큰 없음
- 예제 B: 이어진 문장 2개 이상(28초 이하) → 절반은 시각 토큰(<|시작|> 문장 <|끝|>)을 넣어 긴 녹음 전사 형식도 유지
문장 시각은 정렬이 믿을 만한 문장(ok: 일치율 0.7 이상, 말 속도 정상, 순서 맞음)만 쓴다.

출력: meta/ds_train.jsonl, meta/ds_dev.jsonl, meta/test_files.json, meta/ds_summary.json
사용: ~/voiceguard/.venv/bin/python -X utf8 scripts/build_dataset.py
환경 변수: CAP_HOURS_<도메인>=시간 (예: CAP_HOURS_shop=40) 도메인별 학습 시간 상한
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path.home() / "voiceguard_ft"
MAX_WIN = 28.0
PAD_BEFORE, PAD_AFTER = 0.20, 0.30
_norm = re.compile(r"[^0-9A-Za-z가-힣]")


def norm(t):
    return _norm.sub("", t)


def h(s):
    return int(hashlib.md5(s.encode("utf-8")).hexdigest(), 16)


def known(t):
    return t.get("start") is not None and t.get("sim", 0) >= 0.5


def examples_for(r, path):
    dur = r["seconds"]
    turns = r["turns"]
    out = []

    def left_bound(i):
        return max([turns[k]["end"] for k in range(i) if known(turns[k])], default=0.0)

    def right_bound(j):
        return min([turns[k]["start"] for k in range(j + 1, len(turns)) if known(turns[k])], default=dur)

    # A: 문장 하나
    for i, t in enumerate(turns):
        if not t.get("ok"):
            continue
        s = min(t["start"], max(t["start"] - PAD_BEFORE, left_bound(i), 0.0))
        e = max(t["end"], min(t["end"] + PAD_AFTER, right_bound(i), dur))
        if 0.5 <= e - s <= 25.0:
            # 절반은 시각 토큰을 붙인다: 서비스(faster-whisper)는 짧은 실시간 조각도 시각 토큰 방식으로 풀기 때문
            out.append({"kind": "A", "path": path, "s": round(s, 2), "e": round(e, 2), "text": t["text"], "spk": t["spk"],
                        "ts": h(f"{path}:A{i}") % 2 == 0, "ts_s": round(t["start"] - s, 2), "ts_e": round(t["end"] - s, 2)})

    # B: 이어진 문장 묶음
    i = 0
    while i < len(turns):
        if not turns[i].get("ok"):
            i += 1
            continue
        win_s = min(turns[i]["start"], max(turns[i]["start"] - PAD_BEFORE, left_bound(i), 0.0))
        j = i
        segs = []
        while j < len(turns) and turns[j].get("ok") and turns[j]["end"] + PAD_AFTER - win_s <= MAX_WIN:
            segs.append(turns[j])
            j += 1
        if len(segs) >= 2:
            win_e = max(segs[-1]["end"], min(segs[-1]["end"] + PAD_AFTER, right_bound(j - 1), dur))
            out.append({
                "kind": "B", "path": path, "s": round(win_s, 2), "e": round(win_e, 2),
                "ts": h(f"{path}:{i}") % 2 == 0,
                "segs": [{"s": round(t["start"] - win_s, 2), "e": round(t["end"] - win_s, 2), "text": t["text"]} for t in segs],
            })
            i = j
        else:
            i += 1
    return out


def main():
    rows = [json.loads(l) for l in open(ROOT / "meta" / "aligned.jsonl", encoding="utf-8")]
    files = {}
    for line in open(ROOT / "meta" / "files.jsonl", encoding="utf-8"):
        f = json.loads(line)
        files[f["id"]] = f
    matched = [r for r in rows if r.get("turns")]

    # 시험 세트
    test = [r for r in matched if r["split"] == "valid" and r["match"]["score"] >= 0.5 and r["match"]["contain"] >= 0.8]
    test_groups = {r["match"]["group"] for r in matched if r["split"] == "valid"}
    test_texts = {norm(" ".join(t["text"] for t in r["turns"])) for r in matched if r["split"] == "valid"}

    train_rows, leaked = [], []
    for r in matched:
        if r["split"] != "train":
            continue
        text = norm(" ".join(t["text"] for t in r["turns"]))
        if r["match"]["group"] in test_groups or text in test_texts:
            leaked.append(r["id"])
            continue
        train_rows.append(r)

    caps = {k[len("CAP_HOURS_"):]: float(v) for k, v in os.environ.items() if k.startswith("CAP_HOURS_")}
    train, dev = [], []
    hours = defaultdict(float)
    by_domain_examples = defaultdict(lambda: {"A": 0, "B": 0, "hours_A": 0.0, "hours_B": 0.0})
    for r in sorted(train_rows, key=lambda r: h(r["id"])):
        dom = r["domain"]
        path = files[r["id"]]["path"]
        exs = examples_for(r, path)
        if not exs:
            continue
        is_dev = h(r["match"]["group"]) % 100 < 3
        if not is_dev:
            rec_hours = sum(e["e"] - e["s"] for e in exs if e["kind"] == "A") / 3600
            if dom in caps and hours[dom] + rec_hours > caps[dom]:
                continue
            hours[dom] += rec_hours
        for e in exs:
            e["rec"] = r["id"]
            e["domain"] = dom
            (dev if is_dev else train).append(e)
            if not is_dev:
                d = by_domain_examples[dom]
                d[e["kind"]] += 1
                d["hours_" + e["kind"]] += (e["e"] - e["s"]) / 3600

    (ROOT / "meta" / "ds_train.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in train), encoding="utf-8")
    (ROOT / "meta" / "ds_dev.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in dev), encoding="utf-8")
    test_out = [{
        "id": r["id"], "path": files[r["id"]]["path"], "seconds": r["seconds"], "domain": r["domain"],
        "score": r["match"]["score"], "ref": " ".join(t["text"] for t in r["turns"]),
        "turns": [{k: t.get(k) for k in ("start", "end", "sim", "ok", "spk", "text")} for t in r["turns"]],
    } for r in test]
    (ROOT / "meta" / "test_files.json").write_text(json.dumps(test_out, ensure_ascii=False), encoding="utf-8")

    summary = {
        "train_examples": len(train), "dev_examples": len(dev), "train_recordings": len({e["rec"] for e in train}),
        "dev_recordings": len({e["rec"] for e in dev}), "excluded_same_dialogue_as_test": len(leaked),
        "test_recordings": len(test), "test_hours": round(sum(r["seconds"] for r in test) / 3600, 1),
        "test_by_domain": {d: sum(1 for r in test if r["domain"] == d) for d in sorted({r["domain"] for r in test})},
        "train_by_domain": {d: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in x.items()} for d, x in by_domain_examples.items()},
        "caps": caps,
    }
    (ROOT / "meta" / "ds_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
