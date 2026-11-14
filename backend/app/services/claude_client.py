"""Claude API 공통 호출 모듈.

근거 생성(evidence_generator), 회색지대 재검토(call_analyzer), LLM 판정(llm_judge)이
모두 이 모듈을 통해 Claude를 호출한다. 키는 .env의 ANTHROPIC_API_KEY, 모델은 LLM_MODEL로 바꿀 수 있다.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_MODEL = "claude-opus-5-5"

_client = None


def is_configured() -> bool:
    """ANTHROPIC_API_KEY가 설정되어 있으면 True를 반환한다."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def model_name() -> str:
    """사용할 Claude 모델 ID를 환경변수 또는 기본값에서 읽는다."""
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def complete(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 2000,
    effort: str = "low",
    model: Optional[str] = None,
) -> str:
    """단일 프롬프트를 Claude에 보내 응답 텍스트를 반환한다.

    실시간 경로에서 쓰이므로 effort는 기본 low로 두고, 안전 분류기가 거절하면
    서버 측 fallback 모델이 이어서 답하도록 한다.
    """
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(timeout=30.0, max_retries=1)

    kwargs = {"system": system} if system else {}
    response = _client.beta.messages.create(
        model=model or model_name(),
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요청을 거절했습니다.")
    return "".join(block.text for block in response.content if block.type == "text").strip()
