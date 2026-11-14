"""문자(스미싱) 판별 평가.

  1) 별도로 쓴 문자 예시(backend/eval/messages.json): 학습 데이터와 다른 문체의 스미싱·정상 문자
  2) 학습에 쓰지 않은 스미싱 문자 데이터 표본(SAMPLE건, 기본 80): 분류기 + Claude 융합 결과

분류기 단독, Claude 단독, 융합 각각의 정답 수를 출력한다. Claude를 호출하므로 API 비용이 든다.

실행:
    .venv/bin/python -m backend.app.eval_messages
    SAMPLE=0 .venv/bin/python -m backend.app.eval_messages     # 별도 예시만
"""

from __future__ import annotations

import json
import os
import random

from dotenv import load_dotenv

from backend.app.dataset_split import is_eval_group
from backend.app.paths import DATA_DIR, ENV_PATH, PROJECT_ROOT
from backend.app.services import message_analyzer
from backend.app.services.koelectra_scorer import TH_WARNING


SAMPLE = int(os.environ.get("SAMPLE", "80"))


def run(name: str, items: list[dict]) -> None:
    correct = {"classifier": 0, "llm": 0, "fused": 0}
    counted = {"classifier": 0, "llm": 0, "fused": 0}
    misses = []
    for item in items:
        result = message_analyzer.analyze(item["text"])
        scores = {"classifier": result["scores"]["classifier"], "llm": result["scores"]["llm"], "fused": result["risk_score"]}
        for key, score in scores.items():
            if score is None:
                continue
            counted[key] += 1
            correct[key] += int((score >= TH_WARNING) == bool(item["label"]))
        if (result["risk_score"] >= TH_WARNING) != bool(item["label"]):
            misses.append((item.get("id", ""), item["label"], scores, item["text"][:70]))
    print(f"\n[{name}] {len(items)}건")
    for key in ("classifier", "llm", "fused"):
        if counted[key]:
            print(f"  {key:<10} {correct[key]}/{counted[key]} ({correct[key] / counted[key] * 100:.1f}%)")
    for miss in misses:
        print("  오답:", miss)


def main() -> None:
    load_dotenv(ENV_PATH)
    with open(PROJECT_ROOT / "backend" / "eval" / "messages.json", encoding="utf-8") as f:
        run("별도 문자 예시", json.load(f))

    data_path = DATA_DIR / "SmishingSMS.json"
    if SAMPLE > 0 and data_path.exists():
        with open(data_path, encoding="utf-8") as f:
            cases = [case for case in json.load(f)["cases"] if is_eval_group(case["group"])]
        rng = random.Random(7)
        positives = rng.sample([c for c in cases if c["label"] == 1], SAMPLE // 2)
        negatives = rng.sample([c for c in cases if c["label"] == 0], SAMPLE // 2)
        items = [{"id": c["id"], "label": c["label"], "text": c["turns"][0]["text"]} for c in positives + negatives]
        run("스미싱 문자 데이터 평가 표본", items)


if __name__ == "__main__":
    main()
