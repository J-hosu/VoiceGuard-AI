"""통화 초반(앞부분만 들은 상태) 탐지 성능 평가.

학습 때의 평가(통화 전체 텍스트, 99%대)는 실시간 상황을 반영하지 못한다. 실제로는 통화가
진행되는 동안 앞부분만 보고 판단해야 하고, 점수가 턴마다 출렁이면 경고가 켜졌다 꺼진다.
이 스크립트는 학습과 같은 80/20 분할(seed 42)의 평가 통화를 앞에서부터 k턴씩 잘라
KoELECTRA 점수를 매기고 다음을 출력한다.

  - 사기 통화: 앞 k턴만으로 주의 기준(0.70) 이상이 되는 비율(조기 탐지율)
  - 정상 통화: 앞 k턴에서 주의 기준 이상이 되는 비율(오경보율)
  - 출렁임: 한 번 기준을 넘었다가 다시 아래로 내려간 통화의 비율

실행:
    .venv/bin/python -m backend.app.eval_prefix            # 현재 models/koelectra
    # v2 데이터의 해시 분할 평가셋으로 후보 모델 평가
    SPLIT=hash TRAINING_DATA=data/PhishCatch-Data-v2.json KOELECTRA_MODEL_DIR=models/koelectra_v2 \
        .venv/bin/python -m backend.app.eval_prefix
    MAX_TURNS=30 .venv/bin/python -m backend.app.eval_prefix
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from backend.app.dataset_split import split_cases
from backend.app.paths import DATA_DIR, TRAINING_DATA_PATH
from backend.app.predict_transformer import predict_proba
from backend.app.services.koelectra_scorer import TH_WARNING, WINDOW


PREFIX_TURNS = [1, 2, 3, 5, 8, 12, 20]
MAX_TURNS = int(os.environ.get("MAX_TURNS", "40"))
BATCH = 64


def load_eval_cases() -> list[dict]:
    """학습 스크립트와 같은 분할(SPLIT 환경변수)의 평가 통화를 반환한다."""
    with open(TRAINING_DATA_PATH, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    _, eval_cases = split_cases(cases)
    return eval_cases


def category_of(case: dict) -> str:
    """통화 id 앞부분으로 출처·유형을 묶는다 (예: phishing_susagigwan_0001 → phishing_susagigwan)."""
    return str(case.get("id", "unknown")).rsplit("_", 1)[0]


def score_prefixes(turn_texts: list[str]) -> list[float]:
    """통화를 1턴, 2턴, ... 순으로 늘려 가며 런타임과 같은 방식(누적 vs 최근 WINDOW턴 중 큰 값)으로 채점한다."""
    turn_texts = turn_texts[:MAX_TURNS]
    inputs: list[str] = []
    for end in range(1, len(turn_texts) + 1):
        inputs.append(" ".join(turn_texts[:end]))
        inputs.append(" ".join(turn_texts[max(0, end - WINDOW):end]))
    probs: list[float] = []
    for start in range(0, len(inputs), BATCH):
        probs.extend(float(p) for p in predict_proba(inputs[start:start + BATCH]))
    return [max(probs[i], probs[i + 1]) for i in range(0, len(probs), 2)]


def main() -> None:
    cases = load_eval_cases()
    rows = []
    for case in cases:
        texts = [str(turn.get("text", "")).strip() for turn in case["turns"]]
        texts = [text for text in texts if text]
        if not texts:
            continue
        rows.append({"id": case.get("id"), "label": case["label"], "category": category_of(case), "scores": score_prefixes(texts)})

    def rate_at(group: list[dict], k: int) -> float:
        hits = [row["scores"][min(k, len(row["scores"])) - 1] >= TH_WARNING for row in group]
        return sum(hits) / len(hits) if hits else 0.0

    def flipped(row: dict) -> bool:
        above = False
        for score in row["scores"]:
            if score >= TH_WARNING:
                above = True
            elif above:
                return True
        return False

    phishing = [row for row in rows if row["label"] == 1]
    normal = [row for row in rows if row["label"] == 0]
    print(f"평가 통화 {len(rows)}건 (사기 {len(phishing)}, 정상 {len(normal)}) · 기준 {TH_WARNING}")
    print("\n앞 k턴만 들었을 때 주의 기준 이상인 비율")
    print("k턴     사기(조기 탐지율)   정상(오경보율)")
    for k in PREFIX_TURNS + [MAX_TURNS]:
        label = f"{k}" if k != MAX_TURNS else f"전체(≤{MAX_TURNS})"
        print(f"{label:<10}{rate_at(phishing, k) * 100:>8.1f}%        {rate_at(normal, k) * 100:>8.1f}%")

    print(f"\n출렁임(기준을 넘었다가 다시 내려감): 사기 {sum(map(flipped, phishing)) / max(1, len(phishing)) * 100:.1f}% · "
          f"정상 {sum(map(flipped, normal)) / max(1, len(normal)) * 100:.1f}%")
    ever = sum(any(score >= TH_WARNING for score in row["scores"]) for row in normal) / max(1, len(normal))
    print(f"정상 통화 중 한 번이라도 기준을 넘은 비율: {ever * 100:.1f}%")

    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
    print("\n유형별 주의 기준 이상 비율 (3턴 / 5턴 / 전체 / 통화 중 한 번이라도)")
    for category, group in sorted(by_category.items()):
        ever_group = sum(any(score >= TH_WARNING for score in row["scores"]) for row in group) / len(group)
        print(
            f"  {category:<32} n={len(group):<4} 3턴 {rate_at(group, 3) * 100:5.1f}%  5턴 {rate_at(group, 5) * 100:5.1f}%  "
            f"전체 {rate_at(group, MAX_TURNS) * 100:5.1f}%  한번이라도 {ever_group * 100:5.1f}%"
        )

    out_path = Path(os.environ.get("EVAL_OUT") or DATA_DIR / "eval_prefix_last_run.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    print(f"\n상세: {out_path}")


if __name__ == "__main__":
    main()
