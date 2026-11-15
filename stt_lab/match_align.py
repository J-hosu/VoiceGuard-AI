"""받아 적은 글(meta/asr)을 AI Hub 대화 대본(minwon.json)과 맞춰, 대본 문장마다 음성 속 시작·끝 시각을 정한다.

1) 찾기: 같은 도메인 대화 가운데 3글자 묶음이 가장 많이 겹치는 대화를 고른다
   (scipy 희소 행렬로 후보 5개를 좁힌 뒤, 녹음 벤치와 같은 다중집합 Jaccard로 최종 점수를 잰다).
2) 맞추기: 대본 문장을 이어 붙인 글자열과 받아 적은 글자열을 rapidfuzz 편집 연산으로 정렬해,
   문장마다 대응하는 단어 시각(시작·끝)과 일치율(같은 글자 비율)을 구한다.
출력: meta/aligned.jsonl (녹음마다 한 줄), meta/aligned_summary.json

사용 (서버, ~/voiceguard_ft): PYTHONPATH=pylib ~/voiceguard/.venv/bin/python -X utf8 scripts/match_align.py [작업 수=16]
"""
import json
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from rapidfuzz.distance import Levenshtein
from scipy import sparse

ROOT = Path.home() / "voiceguard_ft"
MINWON = Path.home() / "voiceguard" / "data" / "aihub" / "minwon.json"
DOMAIN = {"fin": "금융/보험", "dasan": "다산콜센터", "kdca": "질병관리본부", "shop": "K쇼핑"}
MIN_SCORE = 0.30  # 녹음 벤치(stt_bench.py)와 같은 기준
_norm = re.compile(r"[^0-9A-Za-z가-힣]")
_alnum = re.compile(r"[0-9A-Za-z]")


def norm(text: str) -> str:
    return _norm.sub("", text)


def trigrams(text: str) -> Counter:
    t = norm(text)
    return Counter(t[i:i + 3] for i in range(len(t) - 2))


# ---- 대화 색인 (부모 프로세스에서 만들고 fork로 공유) ----
INDEX = {}


def build_index():
    cases = json.load(open(MINWON, encoding="utf-8"))["cases"]
    by_domain = defaultdict(list)
    for c in cases:
        by_domain[c["domain"]].append(c)
    for code, name in DOMAIN.items():
        dialogs = by_domain.get(name, [])
        vocab = {}
        rows, cols = [], []
        grams = []
        for r, c in enumerate(dialogs):
            g = trigrams(" ".join(t["text"] for t in c["turns"]))
            grams.append(g)
            for tri in g:
                cols.append(vocab.setdefault(tri, len(vocab)))
                rows.append(r)
        mat = sparse.csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, cols)), shape=(len(dialogs), len(vocab)))
        sizes = np.asarray(mat.sum(axis=1)).ravel()
        INDEX[code] = {"dialogs": dialogs, "grams": grams, "vocab": vocab, "mat": mat, "sizes": sizes}
        print(f"index {code}: {len(dialogs)} dialogues, {len(vocab)} trigrams", flush=True)


def best_match(code: str, text: str):
    idx = INDEX[code]
    g = trigrams(text)
    if not g or not idx["dialogs"]:
        return None
    cols = [idx["vocab"][t] for t in g if t in idx["vocab"]]
    if not cols:
        return None
    q = sparse.csr_matrix((np.ones(len(cols), dtype=np.float32), (np.zeros(len(cols), dtype=np.int64), cols)),
                          shape=(1, len(idx["vocab"])))
    inter = np.asarray((idx["mat"] @ q.T).todense()).ravel()
    jac = inter / (idx["sizes"] + len(g) - inter + 1e-9)
    top = np.argsort(-jac)[:5]
    scored = []
    for r in top:
        dg = idx["grams"][r]
        i = sum((g & dg).values())
        u = sum((g | dg).values()) or 1
        contain = i / (sum(dg.values()) or 1)
        scored.append((i / u, contain, int(r)))
    scored.sort(reverse=True)
    score, contain, r = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    return {"row": r, "score": round(score, 3), "contain": round(contain, 3), "second": round(second, 3)}


def align(turns, words):
    """대본 문장(turns)과 단어(words: [글자, 시작, 끝, 확률])를 글자 단위로 정렬해 문장마다 시각과 일치율을 낸다."""
    ref_parts = [norm(t["text"]) for t in turns]
    ref = "".join(ref_parts)
    hyp_parts, hs, he = [], [], []
    for w, ws, we, _p in words:
        s = norm(w)
        if not s:
            continue
        n = len(s)
        for m in range(n):
            hs.append(ws + (we - ws) * m / n)
            he.append(ws + (we - ws) * (m + 1) / n)
        hyp_parts.append(s)
    hyp = "".join(hyp_parts)
    if not ref or not hyp:
        return None, 1.0
    match = [-1] * len(ref)
    equal = [False] * len(ref)
    for op in Levenshtein.opcodes(ref, hyp):
        i1, i2, j1, j2 = op.src_start, op.src_end, op.dest_start, op.dest_end
        if op.tag == "equal":
            for d in range(i2 - i1):
                match[i1 + d] = j1 + d
                equal[i1 + d] = True
        elif op.tag == "replace":
            l1, l2 = i2 - i1, j2 - j1
            for d in range(l1):
                match[i1 + d] = j1 + min(l2 - 1, d * l2 // l1)
    cer = Levenshtein.distance(ref, hyp) / len(ref)
    # 문장마다 대응하는 받아 적은 글자 구간(lo~hi). 문장 사이 '대본에 없는 글자' 수도 센다:
    # 대본에서 지워진 가림말(ㅇㅇㅇ)이나 대본에 없는 말을 화자가 소리 내 읽은 곳이다.
    spans, pos = [], 0
    for part in ref_parts:
        a, b = pos, pos + len(part)
        pos = b
        js = [match[i] for i in range(a, b) if match[i] >= 0]
        spans.append((a, b, js))
    gap_before, prev_hi = [], -1
    for a, b, js in spans:
        if not js:
            gap_before.append(None)
            continue
        gap_before.append(min(js) - prev_hi - 1)
        prev_hi = max(js)
    trailing = len(hyp) - 1 - prev_hi
    out, prev_end = [], 0.0
    for k, (part, (a, b, js)) in enumerate(zip(ref_parts, spans)):
        if b == a or not js:
            out.append({"k": k, "start": None, "end": None, "sim": 0.0, "ok": False})
            continue
        lo, hi = min(js), max(js)
        start, end = hs[lo], he[hi]
        sim = sum(equal[a:b]) / (b - a)
        dur = max(end - start, 1e-3)
        ins = (hi - lo + 1) - len(set(js))                       # 문장 안에서 대본에 없는 글자
        gb = gap_before[k]
        ga = next((g for g in gap_before[k + 1:] if g is not None), trailing)  # 다음 문장 앞 빈틈
        numc = bool(_alnum.search(hyp[lo:hi + 1])) and not _alnum.search(part)  # 숫자·영문 표기 규칙이 다른 대본
        reasons = []
        if sim < 0.7:
            reasons.append("sim")
        if not 1.5 <= (b - a) / dur <= 15:
            reasons.append("speed")
        if start < prev_end - 0.3:
            reasons.append("order")
        if ins > max(2, 0.15 * (hi - lo + 1)):
            reasons.append("inserted")
        if gb > 2 or ga > 2:
            reasons.append("gap_words")
        if numc:
            reasons.append("number_style")
        ok = not reasons
        out.append({"k": k, "start": round(start, 2), "end": round(end, 2), "sim": round(sim, 3), "ok": ok,
                    "why_not": reasons, "ins": ins, "gb": gb, "ga": ga})
        if ok:
            prev_end = end
    return out, round(cer, 4)


def work(rec):
    words = [w for s in rec["segments"] for w in s["w"]]
    text = " ".join(s["t"] for s in rec["segments"])
    code = rec["id"].split("/")[0].split("_")[1]
    m = best_match(code, text)
    row = {"id": rec["id"], "seconds": rec["seconds"], "match": None}
    if not m or m["score"] < MIN_SCORE:
        row["match"] = m
        return row
    case = INDEX[code]["dialogs"][m["row"]]
    turns = case["turns"]
    timing, cer = align(turns, words)
    if timing is None:
        return row
    row["match"] = {**m, "group": case["group"], "split": case["split"], "case_id": case["id"]}
    row["cer_asr"] = cer
    row["turns"] = [
        {**t_time, "spk": t["speaker"], "text": t["text"]} for t, t_time in zip(turns, timing)
    ]
    return row


if __name__ == "__main__":
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    files = {json.loads(l)["id"]: json.loads(l) for l in open(ROOT / "meta" / "files.jsonl", encoding="utf-8")}
    recs = []
    for p in sorted((ROOT / "meta" / "asr").glob("*.jsonl")):
        for line in open(p, encoding="utf-8"):
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:  # 아직 쓰는 중인 마지막 줄
                pass
    recs = list({r["id"]: r for r in recs}.values())  # 같은 녹음을 두 번 받아 적었으면 하나만
    build_index()
    out = []
    with ProcessPoolExecutor(workers) as pool:
        for row in pool.map(work, recs, chunksize=8):
            f = files.get(row["id"], {})
            row["split"] = f.get("split")
            row["domain"] = f.get("domain")
            out.append(row)
    (ROOT / "meta" / "aligned.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")

    summary = defaultdict(lambda: {"files": 0, "hours": 0.0, "matched": 0, "ok_turns": 0, "ok_hours": 0.0, "turns": 0,
                                   "cross_split": 0, "cer": []})
    group_files = defaultdict(set)
    for r in out:
        s = summary[f"{r['split']}/{r['domain']}"]
        s["files"] += 1
        s["hours"] += r["seconds"] / 3600
        if r.get("turns"):
            s["matched"] += 1
            s["cer"].append(r["cer_asr"])
            s["turns"] += len(r["turns"])
            oks = [t for t in r["turns"] if t.get("ok")]
            s["ok_turns"] += len(oks)
            s["ok_hours"] += sum(t["end"] - t["start"] for t in oks) / 3600
            if r["match"]["split"] != r["split"]:
                s["cross_split"] += 1
            group_files[r["match"]["group"]].add(r["split"])
            for t in r["turns"]:
                for why in t.get("why_not", []):
                    s["drop_" + why] = s.get("drop_" + why, 0) + 1
    for s in summary.values():
        s["cer_median"] = round(float(np.median(s["cer"])), 4) if s["cer"] else None
        s["hours"] = round(s["hours"], 1)
        s["ok_hours"] = round(s["ok_hours"], 1)
        del s["cer"]
    shared = sum(1 for v in group_files.values() if len(v) > 1)
    result = {"by_split_domain": dict(summary), "dialogues_matched_by_both_splits": shared}
    (ROOT / "meta" / "aligned_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
