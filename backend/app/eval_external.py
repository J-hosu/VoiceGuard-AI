"""학습에 쓰지 않은 외부 통화 파일로 분류기의 오경보율(정상)과 탐지율(사기)을 잰다.

eval_prefix.py는 학습 데이터의 평가 분할을 쓴다. 이 스크립트는 따로 준비한 통화 파일(예: AI Hub
상담 데이터의 Validation 분할)을 런타임과 같은 방식으로 채점한다. 턴 경계마다 "누적 전체"와
"최근 10턴" 중 높은 확률을 점수로 쓰고(koelectra_scorer와 동일), 주의 기준 0.70을 넘었는지 본다.

실행 (서버):
    EVAL_DATA=data/aihub/finance_consult.json,data/aihub/minwon.json EVAL_SPLIT=valid \
        .venv/bin/python -X utf8 -m backend.app.eval_external
    # 학습 후보 고르기(어려운 정상 사례 찾기)용: 체크포인트 턴에서만 빠르게 채점
    EVAL_SPLIT=train ALL_PREFIXES=0 SAMPLE_PER_CATEGORY=1500 OUT=data/aihub/mining_scores.json ...

환경변수:
    EVAL_DATA            통화 파일(PhishCatch cases 형식), 쉼표로 여러 개
    EVAL_SPLIT           valid | train | all (기본 valid). 통화의 split 필드로 거른다
    SAMPLE_PER_CATEGORY  세부 분류(출처+카테고리)마다 최대 통화 수, 0이면 전부 (기본 0)
    ALL_PREFIXES         1이면 모든 턴 경계에서 채점(기본), 0이면 체크포인트 턴만
    MAX_TURNS            기본 40 (eval_prefix와 같음)
    OUT                  채점 결과 파일 (기본 data/eval_external_last_run.json)
    KOELECTRA_MODEL_DIR  평가할 모델 폴더
"""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from pathlib import Path

from backend.app.paths import DATA_DIR, KOELECTRA_MODEL_DIR
from backend.app.predict_transformer import predict_proba
from backend.app.services.koelectra_scorer import TH_DANGER, TH_WARNING, WINDOW


CHECKPOINTS = [1, 2, 3, 5, 8, 12, 20, 30, 40]
MAX_TURNS = int(os.environ.get("MAX_TURNS", "40"))
ALL_PREFIXES = os.environ.get("ALL_PREFIXES", "1") == "1"
EVAL_SPLIT = os.environ.get("EVAL_SPLIT", "valid").strip().lower()
SAMPLE_PER_CATEGORY = int(os.environ.get("SAMPLE_PER_CATEGORY", "0"))
OUT_PATH = Path(os.environ.get("OUT") or DATA_DIR / "eval_external_last_run.json")
BATCH = 64
SEED = 42
# 공유 GPU라 이 프로세스가 쓸 수 있는 메모리 비율을 제한한다(초과하면 다른 작업을 밀어내지 않고 이 작업이 실패한다).
GPU_MEM_FRACTION = float(os.environ.get("GPU_MEM_FRACTION", "0.12"))


def limit_gpu_memory() -> None:
    import torch

    if torch.cuda.is_available():
        torch.cuda.set_per_process_memory_fraction(GPU_MEM_FRACTION)


def release_gpu_cache() -> None:
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def source_of(case: dict) -> str:
    return str(case.get("id", "unknown")).rsplit("_", 1)[0]


def load_cases() -> list[dict]:
    paths = [p.strip() for p in os.environ.get("EVAL_DATA", "").split(",") if p.strip()]
    if not paths:
        raise SystemExit("EVAL_DATA 환경변수에 통화 파일을 지정하세요.")
    cases: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)["cases"]
        if EVAL_SPLIT != "all":
            rows = [case for case in rows if case.get("split", EVAL_SPLIT) == EVAL_SPLIT]
        cases.extend(rows)
    if SAMPLE_PER_CATEGORY > 0:
        by_category: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for case in cases:
            by_category[(source_of(case), case.get("category", ""))].append(case)
        rng = random.Random(SEED)
        sampled: list[dict] = []
        for key in sorted(by_category):
            group = by_category[key]
            sampled.extend(group if len(group) <= SAMPLE_PER_CATEGORY else rng.sample(group, SAMPLE_PER_CATEGORY))
        cases = sampled
    return cases


def score_points(n_turns: int) -> list[int]:
    """채점할 턴 수 목록 (앞 k턴)."""
    last = min(n_turns, MAX_TURNS)
    if ALL_PREFIXES:
        return list(range(1, last + 1))
    return sorted({k for k in CHECKPOINTS if k < last} | {last})


def score_all(cases: list[dict]) -> list[dict]:
    """모든 통화의 채점 입력을 모아 길이순으로 묶어 한 번에 추론한다(패딩 낭비를 줄임)."""
    inputs: list[tuple[int, int, str]] = []  # (통화 번호, 채점 지점 번호, 텍스트)
    plans: list[list[int]] = []
    for index, case in enumerate(cases):
        texts = [str(turn.get("text", "")).strip() for turn in case["turns"]]
        texts = [text for text in texts if text]
        points = score_points(len(texts)) if texts else []
        plans.append(points)
        for slot, end in enumerate(points):
            inputs.append((index, slot * 2, " ".join(texts[:end])))
            inputs.append((index, slot * 2 + 1, " ".join(texts[max(0, end - WINDOW):end])))

    limit_gpu_memory()
    order = sorted(range(len(inputs)), key=lambda i: len(inputs[i][2]))
    probs = [0.0] * len(inputs)
    for batch_number, start in enumerate(range(0, len(order), BATCH), start=1):
        chunk = order[start:start + BATCH]
        for position, value in zip(chunk, predict_proba([inputs[i][2] for i in chunk])):
            probs[position] = float(value)
        if batch_number % 200 == 0:
            release_gpu_cache()  # 길이가 계속 바뀌는 배치라 캐시가 쌓이지 않게 비운다
            print(f"  채점 {min(start + BATCH, len(order))}/{len(order)}", flush=True)
    release_gpu_cache()

    raw: list[dict[int, float]] = [dict() for _ in cases]
    for (case_index, slot, _), value in zip(inputs, probs):
        raw[case_index][slot] = value

    rows = []
    for index, case in enumerate(cases):
        points = plans[index]
        scores = [max(raw[index][slot * 2], raw[index][slot * 2 + 1]) for slot in range(len(points))]
        first_alarm = next((points[i] for i, score in enumerate(scores) if score >= TH_WARNING), None)
        rows.append(
            {
                "id": case.get("id"),
                "label": case.get("label", 0),
                "source": source_of(case),
                "category": case.get("category", ""),
                "points": points,
                "scores": [round(score, 4) for score in scores],
                "max_score": round(max(scores), 4) if scores else 0.0,
                "first_alarm_turn": first_alarm,
            }
        )
    return rows


def rate(rows: list[dict], predicate) -> float:
    return 100.0 * sum(1 for row in rows if predicate(row)) / max(1, len(rows))


def score_at(row: dict, k: int) -> float:
    """앞 k턴 시점의 점수 (통화가 더 짧으면 마지막 점수)."""
    eligible = [score for point, score in zip(row["points"], row["scores"]) if point <= k]
    return eligible[-1] if eligible else 0.0


def print_table(title: str, groups: dict[str, list[dict]]) -> None:
    print(f"\n{title}")
    print(f"  {'구분':<44}{'n':>6}{'3턴':>8}{'5턴':>8}{'전체':>8}{'한번이라도':>11}{'강한경고':>9}")
    for key in sorted(groups):
        rows = groups[key]
        print(
            f"  {key[:42]:<44}{len(rows):>6}"
            f"{rate(rows, lambda r: score_at(r, 3) >= TH_WARNING):>7.1f}%"
            f"{rate(rows, lambda r: score_at(r, 5) >= TH_WARNING):>7.1f}%"
            f"{rate(rows, lambda r: (r['scores'] or [0])[-1] >= TH_WARNING):>7.1f}%"
            f"{rate(rows, lambda r: r['max_score'] >= TH_WARNING):>10.1f}%"
            f"{rate(rows, lambda r: r['max_score'] >= TH_DANGER):>8.1f}%"
        )


def main() -> None:
    cases = load_cases()
    print(f"모델: {KOELECTRA_MODEL_DIR}")
    print(f"통화 {len(cases)}건 (split={EVAL_SPLIT}, 표본 상한={SAMPLE_PER_CATEGORY or '없음'}, "
          f"{'모든 턴' if ALL_PREFIXES else '체크포인트 턴'} 채점, 최대 {MAX_TURNS}턴) · 주의 {TH_WARNING} / 강한 경고 {TH_DANGER}")
    rows = score_all(cases)

    normal = [row for row in rows if row["label"] == 0]
    phishing = [row for row in rows if row["label"] == 1]
    if normal:
        print_table("정상 통화: 주의 기준 이상 비율 (낮을수록 좋음)", {"전체": normal})
        by_source: dict[str, list[dict]] = defaultdict(list)
        by_category: dict[str, list[dict]] = defaultdict(list)
        for row in normal:
            by_source[row["source"]].append(row)
            by_category[f"{row['source'].replace('normal_', '')} / {row['category']}"].append(row)
        print_table("출처별", by_source)
        print_table("세부 분류별", by_category)
    if phishing:
        print_table("사기 통화: 주의 기준 이상 비율 (높을수록 좋음)", {"전체": phishing})

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    print(f"\n상세: {OUT_PATH}")


if __name__ == "__main__":
    main()
