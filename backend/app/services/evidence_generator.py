"""RAG 탐지 결과를 사람이 이해하기 쉬운 근거 문장으로 생성한다."""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.app.schemas import RetrievedCase
from backend.app.services import claude_client


logger = logging.getLogger(__name__)


class EvidenceGenerator:
    """생성형 모델을 사용할 수 있으면 사용하고, 아니면 안전한 템플릿을 사용한다."""

    def generate(
        self,
        text: str,
        risk_score: float,
        matched_patterns: list[str],
        retrieved_cases: list[RetrievedCase],
        use_llm: bool = True,
    ) -> str:
        """LLM 또는 로컬 대체 템플릿으로 핵심근거를 생성한다."""
        if use_llm and claude_client.is_configured():
            try:
                return self._generate_with_claude(
                    text=text,
                    risk_score=risk_score,
                    matched_patterns=matched_patterns,
                    retrieved_cases=retrieved_cases,
                )
            except Exception as exc:
                # LLM 장애가 탐지 API 장애로 이어지지 않도록 템플릿 핵심근거로 대체한다.
                logger.warning("Claude 근거 생성 실패. 템플릿 근거로 대체합니다: %s", exc)
                return self._generate_template(text, risk_score, matched_patterns, retrieved_cases)

        return self._generate_template(text, risk_score, matched_patterns, retrieved_cases)

    def _generate_with_claude(
        self,
        text: str,
        risk_score: float,
        matched_patterns: list[str],
        retrieved_cases: list[RetrievedCase],
    ) -> str:
        """ANTHROPIC_API_KEY가 설정되어 있으면 Claude로 핵심근거를 생성한다."""
        context: dict[str, Any] = {
            "input_text": text,
            "risk_score": risk_score,
            "matched_patterns": matched_patterns,
            "retrieved_cases": self._compact_retrieved_cases(retrieved_cases),
        }

        prompt = f"""
너는 보이스피싱 탐지 시스템의 근거 생성 모듈이다.
아래 JSON만 근거로 사용해서 한국어 설명을 작성하라.

작성 규칙:
- 입력에 없는 사실은 만들지 않는다.
- 단정 대신 "의심된다", "위험 신호가 있다"처럼 신중하게 표현한다.
- 유사 사례 기반 근거와 규칙 기반 위험 신호를 함께 설명한다.
- 3문장 이내, 300자 이내로 작성한다.
- 화면에 그대로 표시되므로 제목이나 마크다운 기호(**, #, - 등) 없이 평문으로만 쓴다.
- 마지막에 공식 대표번호로 직접 확인하라는 안전 행동을 제안한다.

JSON:
{json.dumps(context, ensure_ascii=False, indent=2)}
"""

        response = claude_client.complete(
            prompt,
            system="보이스피싱 탐지 근거를 간결하고 신중하게 작성한다.",
        )
        return self._trim_text(response, max_length=500)

    def _generate_template(
        self,
        text: str,
        risk_score: float,
        matched_patterns: list[str],
        retrieved_cases: list[RetrievedCase],
    ) -> str:
        """외부 LLM을 호출하지 않고 핵심근거를 생성한다."""
        risk_percent = round(risk_score * 100, 1)
        phishing_cases = [case for case in retrieved_cases if case.label == 1]

        lines = [f"입력 문장의 보이스피싱 위험도는 {risk_percent}%로 계산되었습니다."]

        if matched_patterns:
            lines.append("탐지된 위험 신호는 " + ", ".join(matched_patterns) + "입니다.")
        else:
            lines.append("명확한 규칙 기반 위험 신호는 적게 탐지되었습니다.")

        if phishing_cases:
            best_case = phishing_cases[0]
            lines.append(
                "DB의 보이스피싱 사례 중 가장 유사한 사례와의 유사도는 "
                f"{round(best_case.similarity * 100, 1)}%입니다."
            )
            lines.append(f"유사 사례 출처는 '{best_case.source or 'unknown'}'입니다.")
        else:
            lines.append("상위 유사 사례에는 보이스피싱 라벨 사례가 많지 않습니다.")

        lines.append("의심되는 경우 통화를 종료하고 기관의 공식 대표번호로 직접 확인하세요.")
        return "\n".join(lines)

    def _compact_retrieved_cases(self, retrieved_cases: list[RetrievedCase]) -> list[dict[str, Any]]:
        """LLM 근거 생성에는 유사 사례 전문 대신 짧은 요약만 전달한다."""
        compact_cases: list[dict[str, Any]] = []
        for case in retrieved_cases:
            compact_cases.append(
                {
                    "id": case.id,
                    "external_id": case.external_id,
                    "label": case.label,
                    "source": case.source,
                    "similarity": case.similarity,
                    "snippet": self._trim_text(case.text, max_length=180),
                }
            )
        return compact_cases

    def _trim_text(self, text: str, max_length: int) -> str:
        """긴 텍스트를 지정 길이로 줄인다."""
        normalized = " ".join(text.split())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3].rstrip() + "..."
