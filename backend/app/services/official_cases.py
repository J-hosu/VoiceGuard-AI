"""공식 기관 공개 사례 검색.

금융감독원 피해사례·소비자경보, 경찰청 예·경보(data/official/cases.json) 가운데 지금 통화와
가장 비슷한 사례를 찾아 신고용 요약과 수사 지원 화면에 "유사 공식 사례"로 붙인다.

탐지 점수에는 쓰지 않는다. 재 보니 통화 유형을 맞히는 정확도가 약 80%여서(글자 n-gram 유사도,
같은 사례를 뺀 평가) 판정 근거로 삼기에는 부족하고, 출처 링크가 달린 참고 자료로는 충분했다.
그래서 유형은 상태 추적기가 정한 것을 우선하고, 그 유형 안에서 가장 비슷한 사례를 고른다.

색인은 "사례에서 생성한 사기 통화"(data/generated/official_scam.json)로 만든다. 통화와 사례
설명문을 직접 비교하는 것보다 통화끼리 비교한 뒤 원 사례로 잇는 쪽이 정확했다(유형 일치 65% → 80%).
생성 통화 파일이 없으면 사례 설명문으로 색인한다. 자료 파일이 없으면 빈 목록을 돌려준다.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from backend.app.paths import DATA_DIR
from backend.app.text_normalize import normalize_text


logger = logging.getLogger(__name__)

CASES_PATH = DATA_DIR / "official" / "cases.json"
GENERATED_PATH = DATA_DIR / "generated" / "official_scam.json"
MIN_CASE_CHARS = 300
# 위험 통화에만 붙이는 참고 자료라 낮게 둔다(실제 통화 전사는 생성 통화보다 유사도가 낮게 나온다).
MIN_SIMILARITY = 0.08

SOURCE_NAMES = {
    "fss_case": "금융감독원 보이스피싱 피해사례",
    "fss_alert": "금융감독원 소비자경보",
    "police_alert": "경찰청 통합대응단 피싱 예·경보",
    "police_scenario": "경찰청 통합대응단 피싱 시나리오",
}

_lock = threading.Lock()
_index: Optional[dict] = None


def _load_index() -> dict:
    """사례와 검색 색인을 1회 만든다. 실패하면 빈 색인을 쓴다."""
    global _index
    with _lock:
        if _index is not None:
            return _index
        _index = {"cases": {}, "rows": [], "vectorizer": None, "matrix": None}
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            with open(CASES_PATH, encoding="utf-8") as f:
                raw_cases = json.load(f)
            cases = {
                index: case
                for index, case in enumerate(raw_cases)
                if case.get("has_text") and len(str(case.get("text", ""))) >= MIN_CASE_CHARS
            }
            rows: list[tuple[int, str]] = []
            if GENERATED_PATH.exists():
                with open(GENERATED_PATH, encoding="utf-8") as f:
                    for call in json.load(f):
                        if call["case_index"] in cases:
                            rows.append((call["case_index"], " ".join(turn["text"] for turn in call["turns"])))
            if not rows:
                rows = [(index, f"{case['title']} {case['text']}") for index, case in cases.items()]

            vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True, min_df=2)
            matrix = vectorizer.fit_transform([normalize_text(text) for _, text in rows])
            _index = {"cases": cases, "rows": rows, "vectorizer": vectorizer, "matrix": matrix}
        except FileNotFoundError:
            logger.info("공식 사례 자료(%s)가 없어 유사 사례 검색을 건너뜁니다.", CASES_PATH)
        except Exception:
            logger.exception("공식 사례 색인 생성 실패")
        return _index


def find_similar(text: str, scam_type: Optional[str] = None, top_k: int = 2) -> list[dict]:
    """통화 내용과 비슷한 공식 사례를 유사도 순으로 반환한다(사례당 한 번).

    scam_type을 주면 그 유형의 사례를 먼저 고르고, 모자라면 다른 유형으로 채운다.
    """
    index = _load_index()
    if index["matrix"] is None or not text.strip():
        return []

    from sklearn.metrics.pairwise import linear_kernel

    similarities = linear_kernel(index["vectorizer"].transform([normalize_text(text)]), index["matrix"])[0]
    best: dict[int, float] = {}
    for (case_index, _), similarity in zip(index["rows"], similarities):
        if similarity > best.get(case_index, 0.0):
            best[case_index] = float(similarity)

    def ranked(same_type: bool) -> list[tuple[int, float]]:
        items = [
            (case_index, similarity)
            for case_index, similarity in best.items()
            if similarity >= MIN_SIMILARITY
            and (index["cases"][case_index].get("scam_type") == scam_type) == same_type
        ]
        return sorted(items, key=lambda item: item[1], reverse=True)

    if scam_type and scam_type not in ("none", "other"):
        chosen = (ranked(True) + ranked(False))[:top_k]
    else:
        chosen = sorted(
            ((i, s) for i, s in best.items() if s >= MIN_SIMILARITY), key=lambda item: item[1], reverse=True
        )[:top_k]

    results = []
    for case_index, similarity in chosen:
        case = index["cases"][case_index]
        results.append(
            {
                "title": case.get("title", ""),
                "source": SOURCE_NAMES.get(case.get("source", ""), case.get("source", "")),
                "date": case.get("date", ""),
                "url": case.get("url", ""),
                "scam_type": case.get("scam_type", "other"),
                "similarity": round(similarity, 3),
                "excerpt": " ".join(str(case.get("text", "")).split())[:220],
            }
        )
    return results
