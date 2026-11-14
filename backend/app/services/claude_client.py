"""Claude API 공통 호출 모듈.

근거 생성(evidence_generator), 회색지대 재검토(call_analyzer), LLM 판정(llm_judge),
상태 추적기(state_tracker), 대응 에이전트(response_agent)가 모두 이 모듈로 Claude를 호출한다.
키는 .env의 ANTHROPIC_API_KEY다. 용도별 모델은 환경변수로 바꿀 수 있다.

    LLM_MODEL      근거 생성·재검토 기본 모델          (기본 claude-opus-5-5)
    TRACKER_MODEL  실시간 상태 추적기                  (기본 claude-haiku-5-5)
    AGENT_MODEL    실시간 대응 에이전트                (기본 claude-haiku-5-5)
    REPORT_MODEL   보고서·수법 분석                    (기본 claude-opus-5-5)
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

DEFAULT_MODEL = "claude-opus-5-5"
FAST_MODEL = "claude-haiku-5-5"

# 안전 분류기가 거절했을 때 서버 측 fallback을 지원하는 모델 계열.
# Haiku 5.5는 서버 측 fallback이 없어서 거절 시 코드에서 DEFAULT_MODEL로 다시 시도한다.
_SERVER_FALLBACK_PREFIXES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")

logger = logging.getLogger(__name__)

_client = None


class ClaudeRefusal(RuntimeError):
    """Claude가 안전 정책으로 응답을 거절했을 때 발생한다."""


def is_configured() -> bool:
    """ANTHROPIC_API_KEY가 설정되어 있으면 True를 반환한다."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def model_name() -> str:
    """근거 생성·재검토에 쓸 기본 Claude 모델 ID."""
    return os.getenv("LLM_MODEL", DEFAULT_MODEL)


def tracker_model() -> str:
    """실시간 상태 추적기 모델 ID."""
    return os.getenv("TRACKER_MODEL", FAST_MODEL)


def agent_model() -> str:
    """실시간 대응 에이전트 모델 ID."""
    return os.getenv("AGENT_MODEL", FAST_MODEL)


def report_model() -> str:
    """보고서·수법 분석 모델 ID."""
    return os.getenv("REPORT_MODEL", DEFAULT_MODEL)


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(timeout=30.0, max_retries=1)
    return _client


def _create(model: str, **params: Any):
    """모델에 맞는 엔드포인트로 메시지를 생성하고, 거절이면 ClaudeRefusal을 던진다."""
    client = _get_client()
    if model.startswith(_SERVER_FALLBACK_PREFIXES):
        response = client.beta.messages.create(
            model=model,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **params,
        )
    else:
        response = client.messages.create(model=model, **params)

    if response.stop_reason == "refusal":
        raise ClaudeRefusal(f"Claude({model})가 요청을 거절했습니다.")
    return response


def _text_of(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()


def complete(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 2000,
    effort: str = "low",
    model: Optional[str] = None,
) -> str:
    """단일 프롬프트를 Claude에 보내 응답 텍스트를 반환한다.

    실시간 경로에서 쓰이므로 effort는 기본 low다.
    """
    params: dict[str, Any] = {
        "max_tokens": max_tokens,
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        params["system"] = system
    return _text_of(_create(model or model_name(), **params))


def _user_content(prompt: str, images: Optional[list[tuple[str, bytes]]]):
    """이미지가 있으면 이미지 블록들 뒤에 프롬프트 텍스트를 붙인 콘텐츠 목록을 만든다."""
    if not images:
        return prompt
    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
        }
        for media_type, data in images
    ]
    blocks.append({"type": "text", "text": prompt})
    return blocks


def complete_json(
    prompt: str,
    schema: dict,
    system: Optional[str] = None,
    max_tokens: int = 4000,
    effort: str = "low",
    model: Optional[str] = None,
    images: Optional[list[tuple[str, bytes]]] = None,
) -> dict:
    """JSON 스키마를 강제해(구조화 출력) 응답을 dict로 반환한다.

    images에 (media_type, 원본 바이트) 목록을 주면 프롬프트 앞에 이미지로 붙인다(문자 캡처·문서 사진 판독).

    빠른 모델이 거절하면 DEFAULT_MODEL로 한 번 더 시도한다. 보이스피싱 대본은
    협박·사칭 문구가 많아 가벼운 모델의 안전 분류기에 걸릴 수 있기 때문이다.
    """
    params: dict[str, Any] = {
        "max_tokens": max_tokens,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "messages": [{"role": "user", "content": _user_content(prompt, images)}],
    }
    if system:
        params["system"] = system

    chosen = model or model_name()
    try:
        response = _create(chosen, **params)
    except ClaudeRefusal:
        if chosen == DEFAULT_MODEL:
            raise
        logger.warning("%s가 거절해 %s로 다시 시도합니다.", chosen, DEFAULT_MODEL)
        response = _create(DEFAULT_MODEL, **params)

    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude 응답이 max_tokens에서 잘려 JSON이 완성되지 않았습니다.")
    return json.loads(_text_of(response))
