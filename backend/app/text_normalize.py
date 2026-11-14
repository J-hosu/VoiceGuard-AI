"""분류기 입력 전처리.

학습 데이터는 출처마다 전사 방식이 달라(문장부호 유무, 띄어쓰기) 모델이 내용 대신
"어느 출처의 전사인가"를 단서로 삼기 쉽다. 문장부호를 지우고 공백을 정리해 그 단서를 줄인다.
학습 때 적용했는지는 모델 폴더의 preprocess.json에 기록하고, 추론은 그 기록을 따른다
(이 파일이 없는 예전 모델은 원문 그대로 추론한다).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


PREPROCESS_FILE = "preprocess.json"
_PUNCTUATION = re.compile(r"[.,?!…~·\"'“”‘’()\[\]<>:;]+")
_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """문장부호를 공백으로 바꾸고 연속 공백을 하나로 줄인다."""
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", text)).strip()


def save_preprocess_config(model_dir: Path, normalize: bool) -> None:
    with open(Path(model_dir) / PREPROCESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"normalize": normalize}, f)


def uses_normalization(model_dir: Path) -> bool:
    path = Path(model_dir) / PREPROCESS_FILE
    if not path.exists():
        return False
    with open(path, encoding="utf-8") as f:
        return bool(json.load(f).get("normalize"))
