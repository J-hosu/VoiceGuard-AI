"""학습 데이터 v4: v3c 데이터 + 서비스와 같은 Whisper 전사 말투의 사기·정상 통화.

왜: v3c는 사기 통화를 금감원 녹취의 사람·외부 엔진 전사(KorCCVi)로 배웠는데, 앱은 Whisper가 무음 조각마다 받아 적은
글을 채점한다. 전사 말투가 다르면 같은 통화라도 점수가 흔들리므로, 사기와 정상 양쪽에 Whisper 전사본을 더해
말투가 정답의 단서가 되지 않게 한다(한쪽에만 넣으면 '말투 = 사기'를 외운다 — v2 때 겪은 문제).

- 사기: 금감원 공개 녹취(서버 ~/dataset) 327개를 large-v3로 받아 적은 것 (stt_lab/fss_prepare.py → transcribe.py)
- 정상: AI Hub 민원(콜센터) 학습용 녹음을 받아 적은 것 중 도메인별 NORMAL_PER_DOMAIN개 (stt_lab 대본 맞추기 결과)
- 발화는 앱과 비슷하게 단어 시각으로 나눈다: 1.2초 넘게 쌓인 뒤 0.25초 넘게 쉬면, 또는 10초가 되면 끊는다.
- 같은 통화가 학습·평가로 갈라지지 않게 group을 맞춘다:
  금감원 녹취는 v3c의 금감원 전사(phishing_susagigwan·phishing_daechul)와 3글자 묶음 포함률이 0.5 이상이면 그 id를 group으로
  (짝이 학습·평가 양쪽에 걸치면 그 녹취는 뺀다),
  AI Hub 녹음은 짝지어진 대본 대화의 group(v3c의 같은 대화와 같은 값)을 쓴다.
- 사기 핵심 신호가 든 정상 통화는 뺀다(v3와 같은 규칙).

출력: data/PhishCatch-Data-roberta_v4.json
사용 (서버 ~/voiceguard): .venv/bin/python -X utf8 -m backend.app.build_dataset_v4
환경 변수: FT_ROOT(기본 ~/voiceguard_ft), NORMAL_PER_DOMAIN(기본 350), FSS_MIN_MATCH(포함률, 기본 0.5)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from backend.app.build_dataset_v3 import SCAM_SIGNAL
from backend.app.dataset_split import is_eval_group

DATA_DIR = Path("data")
FT_ROOT = Path(os.environ.get("FT_ROOT", str(Path.home() / "voiceguard_ft")))
BASE = DATA_DIR / "PhishCatch-Data-roberta_v3c.json"
OUT = DATA_DIR / "PhishCatch-Data-roberta_v4.json"
NORMAL_PER_DOMAIN = int(os.environ.get("NORMAL_PER_DOMAIN", "350"))
FSS_MIN_MATCH = float(os.environ.get("FSS_MIN_MATCH", "0.5"))  # 포함률 기준
MIN_S, MAX_S, GAP_S = 1.2, 10.0, 0.25
_norm = re.compile(r"[^0-9A-Za-z가-힣]")


def trigrams(text: str) -> Counter:
    t = _norm.sub("", text)
    return Counter(t[i:i + 3] for i in range(len(t) - 2))


def containment(a: Counter, b: Counter) -> float:
    inter = sum((a & b).values())
    return inter / (min(sum(a.values()), sum(b.values())) or 1)


def turns_from_words(segments: list[dict]) -> list[dict]:
    """단어 시각으로 앱의 무음 조각과 비슷한 발화를 만든다."""
    words = [w for s in segments for w in s.get("w", [])]
    turns, cur, start, last_end = [], [], None, None
    for text, ws, we, _p in words:
        if cur and ((ws - last_end >= GAP_S and last_end - start >= MIN_S) or we - start > MAX_S):
            turns.append("".join(cur).strip())
            cur, start = [], None
        if start is None:
            start = ws
        cur.append(text)
        last_end = we
    if cur:
        turns.append("".join(cur).strip())
    return [{"turn_index": i + 1, "text": t} for i, t in enumerate(t for t in turns if t)]


def load_asr(names: list[Path]) -> dict[str, dict]:
    out = {}
    for p in names:
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[r["id"]] = r
    return out


def main() -> None:
    base = json.loads(BASE.read_text(encoding="utf-8"))["cases"]
    asr_dir = FT_ROOT / "meta" / "asr"

    # 1) 금감원 녹취 → 사기
    korccvi = [(c["id"], trigrams(" ".join(t["text"] for t in c["turns"])))
               for c in base if c["id"].startswith(("phishing_susagigwan", "phishing_daechul"))]
    fss = load_asr(sorted(asr_dir.glob("fss*.jsonl")))
    scam, matched, dropped = [], 0, 0
    for fid in sorted(fss):
        turns = turns_from_words(fss[fid]["segments"])
        if len(turns) < 2:
            continue
        g = trigrams(" ".join(t["text"] for t in turns))
        # 짧은 쪽이 긴 쪽에 얼마나 들어 있나(포함률): 기존 전사가 녹취 일부만 담아도 같은 통화로 잡는다
        hits = [(cid, containment(g, cg)) for cid, cg in korccvi]
        hits = [(cid, s) for cid, s in hits if s >= FSS_MIN_MATCH]
        sides = {is_eval_group(cid) for cid, _ in hits}
        if len(sides) > 1:   # 같은 녹취의 기존 전사가 학습·평가에 갈라져 있으면 누수를 피하려고 뺀다
            dropped += 1
            continue
        best_id, best = max(hits, key=lambda x: x[1]) if hits else (None, 0.0)
        group = best_id if best_id else f"fss_audio_{fid.replace('/', '_')}"
        matched += bool(best_id)
        scam.append({"id": f"phishing_whisper_fss_{len(scam):04d}", "label": 1, "group": group,
                     "source": "fss_whisper", "match": round(best, 3), "turns": turns})

    # 2) AI Hub 민원 녹음(학습용) → 정상
    aligned = [json.loads(l) for l in open(FT_ROOT / "meta" / "aligned.jsonl", encoding="utf-8")]
    aligned = [r for r in aligned if r.get("split") == "train" and r.get("turns") and r["match"]["score"] >= 0.3]
    asr = load_asr(sorted(p for p in asr_dir.glob("train_*.jsonl")))
    by_domain = defaultdict(list)
    for r in sorted(aligned, key=lambda r: hashlib.md5(r["id"].encode()).hexdigest()):
        rec = asr.get(r["id"])
        if not rec:
            continue
        turns = turns_from_words(rec["segments"])
        text = " ".join(t["text"] for t in turns)
        if len(turns) < 4 or SCAM_SIGNAL.search(text):
            continue
        by_domain[r["domain"]].append({"id": None, "label": 0, "group": r["match"]["group"], "source": "aihub_whisper",
                                       "category": r["domain"], "turns": turns})
    normal = []
    for dom in sorted(by_domain):
        for c in by_domain[dom][:NORMAL_PER_DOMAIN]:
            c["id"] = f"normal_whisper_aihub_{dom}_{len(normal):04d}"
            normal.append(c)

    cases = base + scam + normal
    OUT.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    ev = lambda cs: sum(1 for c in cs if is_eval_group(str(c.get("group") or c["id"])))
    print(json.dumps({
        "base_cases": len(base), "added_scam_whisper": len(scam), "scam_matched_to_korccvi": matched, "scam_dropped_split_conflict": dropped,
        "added_normal_whisper": len(normal), "normal_by_domain": {d: min(len(v), NORMAL_PER_DOMAIN) for d, v in by_domain.items()},
        "total": len(cases), "eval_split_scam_whisper": ev(scam), "eval_split_normal_whisper": ev(normal),
        "label_counts": dict(Counter(c["label"] for c in cases)), "out": str(OUT),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
