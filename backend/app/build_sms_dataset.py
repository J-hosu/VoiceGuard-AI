"""스미싱 문자 분류기 학습 데이터 구성.

입력: data/raw/smishing/smishing.csv
      Hugging Face `jmjmjm3/kor-smishing-message` (CC BY-NC-SA 4.0, 비상업 조건).
      컬럼 content(문자 본문), label(정상|스미싱), explanation(판단 근거 설명)
출력: data/SmishingSMS.json  {"cases": [{"id", "label", "group", "turns": [{"turn_index": 1, "text"}]}]}
      통화 분류기 학습 스크립트(train_transformer.py)를 그대로 쓰기 위해 같은 형식으로 만든다.

같은 틀에 숫자·주소만 바뀐 문자가 많아서, 숫자와 링크를 가린 본문을 group으로 삼는다.
해시 분할(SPLIT=hash)에서 같은 틀의 문자가 학습과 평가에 나뉘어 들어가지 않게 하기 위해서다.

실행:
    .venv/bin/python -m backend.app.build_sms_dataset
    SPLIT=hash TRAINING_DATA=data/SmishingSMS.json MODEL=klue/roberta-base \\
        KOELECTRA_MODEL_DIR=models/sms_roberta .venv/bin/python -m backend.app.train_transformer
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter

from backend.app.paths import DATA_DIR


SOURCE_PATH = DATA_DIR / "raw" / "smishing" / "smishing.csv"
OUT_PATH = DATA_DIR / "SmishingSMS.json"
LABELS = {"정상": 0, "스미싱": 1}
MIN_CHARS = 4
MAX_CHARS = 1500

_URL = re.compile(r"(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?", re.I)
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def template_key(text: str) -> str:
    """숫자와 링크를 가려 같은 틀의 문자를 한 묶음으로 본다."""
    masked = _DIGITS.sub("0", _URL.sub("<url>", text.lower()))
    return _SPACES.sub(" ", masked).strip()


def main() -> None:
    csv.field_size_limit(10**9)
    cases = []
    seen: set[str] = set()
    counts: Counter = Counter()
    with open(SOURCE_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            label = LABELS.get((row.get("label") or "").strip())
            text = _SPACES.sub(" ", (row.get("content") or "")).strip()[:MAX_CHARS]
            if label is None or len(text) < MIN_CHARS or text in seen:
                continue
            seen.add(text)
            counts[label] += 1
            prefix = "smishing_sms" if label else "normal_sms"
            group = hashlib.md5(template_key(text).encode("utf-8")).hexdigest()[:16]
            cases.append(
                {
                    "id": f"{prefix}_{counts[label]:05d}",
                    "label": label,
                    "group": f"sms_{group}",
                    "turns": [{"turn_index": 1, "text": text}],
                }
            )

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False)
    groups = len({case["group"] for case in cases})
    print(f"{OUT_PATH}: {len(cases)}건 (스미싱 {counts[1]}, 정상 {counts[0]}), 묶음 {groups}개")


if __name__ == "__main__":
    main()
