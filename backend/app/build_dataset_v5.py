"""통화 분류기 v5 학습 데이터: v3c 데이터에서 가린 개인정보 자리를 그럴듯한 가짜 값으로 채운다.

v3c는 AI Hub 상담 통화의 가림 표시(●, ㅇㅇ, OO, ★ 등)를 지운 채 배웠다. 실제 통화를 받아 적은 글에는
이름·번호·주소가 그대로 나오므로, 가린 자리를 값으로 채워 학습 글과 실제 글의 차이를 줄인다.
통화·라벨·group·나머지 글자는 v3c와 똑같고, 가린 자리만 바뀐다.

입력:
  data/PhishCatch-Data-roberta_v4.json  source 필드가 없는 행이 v3c 데이터(7,992건)
  SPANS  가린 자리 목록(mask_fill_batches.py가 만든 spans.json)
  FILLS  채운 값(쉼표로 여러 파일): {"calls": [{"id", "values": [{"n", "value"}]}]}
출력:
  data/PhishCatch-Data-roberta_v5.json  v3c 데이터 + 채운 글
  data/PhishCatch-Data-v2_filled.json   그중 v2 평가 세트에 든 통화(AI Hub 통화 제외)

KISA 원본에 섞인 docx·PDF 2행(대화 없음)은 뺀다.
값이 없거나 형식 검사를 통과하지 못한 자리, 모델이 개인정보가 아니라고 원래 표시를 돌려준 자리는 v3c 글로 둔다
(AI Hub 통화는 변환기의 clean_text로 지우고, 다른 출처는 원래 글 그대로).

실행:
    SPANS=.../spans.json FILLS=.../fills.json python -X utf8 -m backend.app.build_dataset_v5
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

from backend.app.convert_aihub_calls import clean_text
from backend.app.paths import DATA_DIR

SOURCE = DATA_DIR / "PhishCatch-Data-roberta_v4.json"
OUT = DATA_DIR / "PhishCatch-Data-roberta_v5.json"
OUT_V2 = DATA_DIR / "PhishCatch-Data-v2_filled.json"

# KISA 원본 zip에 섞여 있던 docx·PDF를 글로 읽은 행. 대화가 한 줄도 없어 뺀다.
BINARY_ROWS = {"normal_kisa118_1282", "normal_kisa118_1283"}
PLACEHOLDER = re.compile(r"⟦(\d+)\|[^⟧]*⟧")
MASK_CHARS = re.compile(r"[○●〇◯□■◆◇★☆※▲△▼▽⟦⟧|]|ㅇㅇ|(?<![A-Za-z])(?:O{2,}|X{2,}|\*{2,})(?![A-Za-z])")
ZERO_MASK = re.compile(r"^[0\s\-]+$")
HANGUL_ZERO = re.compile(r"^[공영\s]+$")
HANGUL_DIGITS = re.compile(r"[공영일이삼사오육칠팔구]")
SPACES = re.compile(r"\s+")


def valid(value: str, orig: str) -> bool:
    """채운 값이 쓸 만한지 본다. 가림 기호가 남았거나, 숫자 자리에 숫자가 없거나, 지나치게 길면 버린다."""
    if not value or MASK_CHARS.search(value):
        return False
    if len(value) > max(40, 4 * len(orig) + 10):
        return False
    stripped = orig.strip()
    if ZERO_MASK.match(stripped) and not re.search(r"\d", value):
        return False
    if HANGUL_ZERO.match(stripped) and not (HANGUL_DIGITS.search(value) or re.search(r"\d", value)):
        return False
    return True


def main() -> None:
    spans = json.load(open(os.environ["SPANS"], encoding="utf-8"))
    fills: dict[str, dict[int, str]] = {}
    for path in os.environ["FILLS"].split(","):
        for call in json.load(open(path, encoding="utf-8"))["calls"]:
            fills.setdefault(call["id"], {}).update({int(v["n"]): str(v["value"]) for v in call["values"]})

    rows = [r for r in json.load(open(SOURCE, encoding="utf-8"))["cases"] if not r.get("source")]
    stats: Counter = Counter()
    stats["binary_rows_dropped"] = sum(1 for r in rows if r["id"] in BINARY_ROWS)
    rows = [r for r in rows if r["id"] not in BINARY_ROWS]
    out_rows = []
    for row in rows:
        call = spans.get(row["id"])
        if not call:
            out_rows.append(row)
            continue
        orig = {s["n"]: s["orig"] for s in call["spans"]}
        values = fills.get(row["id"], {})
        aihub = row["id"].startswith(("normal_aihubfin", "normal_aihubminwon"))
        stats["calls_with_spans"] += 1
        stats["calls_filled" if values else "calls_missing"] += 1

        def v3c_text(n: int) -> str:
            """채우지 못한 자리는 v3c가 본 글로 둔다."""
            return f" {clean_text(orig[n])} " if aihub else orig[n]

        def substitute(match: re.Match) -> str:
            n = int(match.group(1))
            value = values.get(n, "")
            stats["spans"] += 1
            if value.strip() == orig[n].strip():  # 모델이 개인정보가 아니라고 본 자리
                stats["kept_as_v3c"] += 1
                return v3c_text(n)
            if valid(value, orig[n]):
                stats["filled"] += 1
                return value
            stats["fallback_v3c"] += 1
            return v3c_text(n)

        turns = []
        for turn in call["turns"]:
            text = SPACES.sub(" ", PLACEHOLDER.sub(substitute, turn["text"])).strip()
            if text:
                turns.append({"turn_index": turn["turn_index"], "text": text})
        new_row = dict(row)
        new_row["turns"] = turns
        out_rows.append(new_row)

    json.dump({"cases": out_rows}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    v2_rows = [r for r in out_rows if not r["id"].startswith(("normal_aihubfin", "normal_aihubminwon"))]
    json.dump({"cases": v2_rows}, open(OUT_V2, "w", encoding="utf-8"), ensure_ascii=False)
    print(dict(stats))
    print(f"저장: {OUT} ({len(out_rows)}건), {OUT_V2} ({len(v2_rows)}건)")


if __name__ == "__main__":
    main()
