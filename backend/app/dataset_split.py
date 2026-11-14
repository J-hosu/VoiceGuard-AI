"""학습/평가 분할.

- sklearn(기본): 기존과 같은 80/20 층화 분할(seed 42). 데이터가 바뀌면 분할도 바뀐다.
- hash: 통화의 group(없으면 id)을 해시해 나눈다. 데이터를 추가해도 기존 통화가 학습↔평가를
  오가지 않고, 같은 원본에서 만든 통화들(같은 공식 사례에서 생성한 사기 통화와 정상 쌍둥이
  통화)이 한쪽에만 들어가 평가 누수를 막는다.

SPLIT 환경변수로 고른다.
"""

from __future__ import annotations

import hashlib
import os

from sklearn.model_selection import train_test_split


SEED = 42
EVAL_BUCKETS = 5  # 해시 분할에서 5개 중 1개 버킷(20%)을 평가에 쓴다


def split_mode() -> str:
    return os.environ.get("SPLIT", "sklearn").strip().lower()


def is_eval_group(group: str) -> bool:
    digest = hashlib.md5(group.encode("utf-8")).hexdigest()
    return int(digest, 16) % EVAL_BUCKETS == 0


def split_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """(학습 통화, 평가 통화)를 반환한다."""
    if split_mode() == "hash":
        train = [case for case in cases if not is_eval_group(str(case.get("group") or case["id"]))]
        evaluation = [case for case in cases if is_eval_group(str(case.get("group") or case["id"]))]
        return train, evaluation
    labels = [case["label"] for case in cases]
    train, evaluation = train_test_split(cases, test_size=0.2, stratify=labels, random_state=SEED)
    return train, evaluation
