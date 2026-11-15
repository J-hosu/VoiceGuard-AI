"""학습 데이터 v3: v2에 AI Hub 실제 상담 통화(정상)를 더한다.

추가하는 통화는 모두 정상(label 0)이고, AI Hub의 Training 분할에서만 고른다. Validation 분할은
학습에 넣지 않고 오경보 평가(eval_external)에만 쓴다.

  normal_aihubfin_*      금융분야 고객상담 (은행·보험·증권 콜센터)          data/aihub/finance_consult.json
  normal_aihubminwon_*   민원(콜센터) 질의응답 (금융·보험, 서울 다산콜센터,   data/aihub/minwon.json
                         질병관리청, 홈쇼핑)
  (두 파일은 convert_aihub_calls.py로 만든다)

고르는 방법 (출처마다):
  1) 어려운 정상 사례: 현재 모델로 채점한 결과(MINING, eval_external 체크포인트 채점)에서 통화 중 한 번이라도
     HARD_TH 이상이 된 통화를 점수 높은 순으로 모두 넣는다 (상한 HARD_CAP).
  2) 말투·주제를 넓히기 위해 나머지 통화에서 세부 분류별로 고르게 RANDOM_PER_SOURCE건을 더 넣는다.
정상 통화만 너무 많이 늘리면 모델이 "상담 말투 = 정상"을 배울 수 있어(v2 기록 참고) 건수를 제한한다.

  3) 진짜 상담이라도 사기 핵심 신호(원격 제어 앱 설치, 수사기관·금감원 언급, 안전계좌 등)가 든 통화는 뺀다
     (EXCLUDE_SIGNALS=1, 기본). 이런 통화를 정상으로 가르치면 모델이 신호 자체를 무시하게 된다.

실행 (서버):
    .venv/bin/python -X utf8 -m backend.app.build_dataset_v3
환경변수: HARD_TH(기본 0.5) HARD_CAP(기본 1500) RANDOM_PER_SOURCE(기본 700) MINING(기본 data/aihub/mining_scores.json)
         EXCLUDE_SIGNALS(기본 1) OUT(기본 data/PhishCatch-Data-v3.json) SELECTION(선택 목록 저장 경로)
"""

from __future__ import annotations

import json
import os
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from backend.app.paths import DATA_DIR


BASE_PATH = DATA_DIR / "PhishCatch-Data-v2.json"
OUT_PATH = Path(os.environ.get("OUT") or DATA_DIR / "PhishCatch-Data-v3.json")
SELECTION_PATH = Path(os.environ.get("SELECTION") or DATA_DIR / "aihub" / "v3_selection.json")
# 진짜 상담이라도 보이스피싱의 핵심 신호(원격 제어 앱 설치, 수사기관·금감원 언급, 안전계좌 등)가 들어간 통화를
# 정상으로 가르치면 모델이 그 신호 자체를 무시하게 된다(첫 v3 후보가 카드 배송 + 원격 앱 사기 대본을 놓쳤다).
# 이런 통화는 맥락 판단을 상태 추적기에 맡기고 학습에서 뺀다.
EXCLUDE_SIGNALS = os.environ.get("EXCLUDE_SIGNALS", "1") == "1"
SCAM_SIGNAL = re.compile(
    r"원격\s*(지원|제어|접속|조종|앱|프로그램|서비스|으로)|(앱|어플|애플리케이션|프로그램)[을를]?\s*(설치|깔)"
    r"|보이스\s*피싱|피싱|사기|검찰|경찰|금융감독원|금감원|안전\s*계좌|보호\s*조치|명의\s*도용"
)
SOURCES = {
    "aihubfin": DATA_DIR / "aihub" / "finance_consult.json",
    "aihubminwon": DATA_DIR / "aihub" / "minwon.json",
}
MINING_PATH = Path(os.environ.get("MINING") or DATA_DIR / "aihub" / "mining_scores.json")
HARD_TH = float(os.environ.get("HARD_TH", "0.5"))
HARD_CAP = int(os.environ.get("HARD_CAP", "1500"))
RANDOM_PER_SOURCE = int(os.environ.get("RANDOM_PER_SOURCE", "700"))
SEED = 42


def source_key(case_id: str) -> str:
    """normal_aihubfin_bank_00012 -> aihubfin"""
    parts = case_id.split("_")
    return parts[1] if len(parts) > 2 else ""


def stratified_sample(cases: list[dict], total: int, rng: random.Random) -> list[dict]:
    """세부 분류마다 고르게 total건을 뽑는다 (작은 분류는 전부, 남는 몫은 큰 분류에 나눔)."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_category[case.get("category", "")].append(case)
    for group in by_category.values():
        rng.shuffle(group)
    picked: list[dict] = []
    remaining = dict(by_category)
    budget = total
    while budget > 0 and remaining:
        share = max(1, budget // len(remaining))
        for category in sorted(remaining):
            group = remaining[category]
            take = group[:share]
            picked.extend(take)
            remaining[category] = group[share:]
            budget -= len(take)
            if budget <= 0:
                break
        remaining = {key: value for key, value in remaining.items() if value}
    return picked[:total]


def main() -> None:
    with open(BASE_PATH, encoding="utf-8") as f:
        base = json.load(f)["cases"]
    with open(MINING_PATH, encoding="utf-8") as f:
        mined = {row["id"]: row["max_score"] for row in json.load(f)}

    rng = random.Random(SEED)
    added: list[dict] = []
    selection: list[dict] = []
    for key, path in SOURCES.items():
        with open(path, encoding="utf-8") as f:
            cases = [case for case in json.load(f)["cases"] if case.get("split") == "train" and case["id"] in mined]
        excluded = 0
        if EXCLUDE_SIGNALS:
            kept = [case for case in cases if not SCAM_SIGNAL.search(" ".join(turn["text"] for turn in case["turns"]))]
            excluded = len(cases) - len(kept)
            cases = kept
        print(f"{key}: 사기 핵심 신호가 든 통화 {excluded}건 제외")
        hard = sorted((case for case in cases if mined[case["id"]] >= HARD_TH), key=lambda case: -mined[case["id"]])[:HARD_CAP]
        hard_ids = {case["id"] for case in hard}
        rest = [case for case in cases if case["id"] not in hard_ids]
        extra = stratified_sample(rest, RANDOM_PER_SOURCE, rng)
        for reason, group in (("hard", hard), ("random", extra)):
            for case in group:
                added.append({field: case[field] for field in ("id", "label", "group", "category", "turns")})
                selection.append({"id": case["id"], "reason": reason, "max_score": mined[case["id"]], "category": case.get("category", "")})
        print(f"{key}: 채점된 학습 후보 {len(cases)}건 중 어려운 사례(≥{HARD_TH}) {len(hard)}건 + 무작위 {len(extra)}건")

    cases = base + added
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False)
    SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SELECTION_PATH, "w", encoding="utf-8") as f:
        json.dump(selection, f, ensure_ascii=False, indent=1)

    counts = Counter((case["id"].rsplit("_", 1)[0], case["label"]) for case in cases)
    print(f"\n{OUT_PATH}: 총 {len(cases)}건 (사기 {sum(c['label'] for c in cases)}, 정상 {sum(1 - c['label'] for c in cases)})")
    for (category, label), count in sorted(counts.items()):
        print(f"  {category:<34} label={label} {count}")
    by_reason = Counter((source_key(row["id"]), row["reason"], row["category"]) for row in selection)
    print("\n추가한 AI Hub 통화 (출처, 이유, 세부 분류):")
    for (source, reason, category), count in sorted(by_reason.items()):
        print(f"  {source:<12} {reason:<7} {category[:28]:<30} {count}")
    print(f"\n선택 목록: {SELECTION_PATH}")


if __name__ == "__main__":
    main()
