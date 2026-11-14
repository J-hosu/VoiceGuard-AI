"""v2 REST 라우터: 대화 상태, 타임라인, 에이전트 행동, 통화 종료, 피드백.

docs/API_SPEC_V2.md 5절 참고.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from backend.app import state_repository
from backend.app.api.response_logger import log_api_response
from backend.app.codes import FEEDBACK_VERDICTS
from backend.app.repository import get_call_log
from backend.app.services import agent_loop, report_builder, response_agent


router = APIRouter()


class ActionRespondRequest(BaseModel):
    """행동 카드 반응 또는 후속 확인 답."""

    response: str = Field(..., description="acknowledged, dismissed, answered")
    answer: Optional[str] = Field(None, description="followup_check에 answered로 답할 때의 값")
    source: str = Field("app", description="app, telegram, n8n")


class FeedbackRequest(BaseModel):
    """통화 판정에 대한 사용자 피드백."""

    verdict: str = Field(..., description="false_positive, confirmed_phishing, missed_phishing, unsure")
    comment: str = Field("", description="자유 입력")
    damage: Optional[dict] = Field(None, description="confirmed_phishing일 때 피해 정보")


def _require_call(log_id: int) -> None:
    try:
        get_call_log(log_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/calls/{log_id}/state")
def get_call_state(log_id: int) -> dict:
    """통화의 최신 대화 상태(단계·유형·순응도·태그·단서)를 반환한다."""
    _require_call(log_id)
    return log_api_response("GET /calls/{log_id}/state", agent_loop.get_state_response(log_id))


@router.get("/calls/{log_id}/timeline")
def get_call_timeline(log_id: int) -> dict:
    """단계 전환, 태그가 붙은 발화, 에이전트 행동을 시간순으로 반환한다."""
    _require_call(log_id)
    return log_api_response("GET /calls/{log_id}/timeline", agent_loop.get_timeline_response(log_id))


@router.get("/calls/{log_id}/actions")
def get_call_actions(log_id: int) -> dict:
    """통화에서 에이전트가 결정한 행동 목록을 반환한다."""
    _require_call(log_id)
    response = {"log_id": log_id, "actions": state_repository.list_actions(log_id)}
    return log_api_response("GET /calls/{log_id}/actions", response)


@router.post("/calls/{log_id}/actions/{action_id}/respond")
async def respond_to_action(log_id: int, action_id: int, body: ActionRespondRequest) -> dict:
    """행동 카드에 대한 반응을 기록한다. 후속 확인 답이면 다음 행동을 함께 반환한다."""
    _require_call(log_id)
    try:
        result = await asyncio.to_thread(
            response_agent.handle_response,
            log_id,
            action_id,
            body.response,
            body.answer,
            body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return log_api_response("POST /calls/{log_id}/actions/{action_id}/respond", result)


@router.post("/calls/{log_id}/end")
async def end_call(log_id: int) -> dict:
    """통화를 마무리한다. 마지막 상태를 확정하고 통화 후 행동(신고 안내, 후속 확인)을 만든다."""
    _require_call(log_id)
    result = await asyncio.to_thread(agent_loop.end_call, log_id)
    response = {"log_id": log_id, "final": result["final"], "actions": result["actions"]}
    return log_api_response("POST /calls/{log_id}/end", response)


@router.get("/calls/{log_id}/report")
def get_call_report(log_id: int, format: str = "json"):
    """경찰·금융회사 제출용 요약을 반환한다. format=md면 바로 읽을 수 있는 텍스트로 준다."""
    _require_call(log_id)
    report = report_builder.build_report(log_id)
    if format == "md":
        # 브라우저 탭에서 바로 보이도록 text/plain으로 내려준다(text/markdown은 다운로드로 처리되는 경우가 많다).
        return PlainTextResponse(report_builder.render_markdown(report), media_type="text/plain; charset=utf-8")
    if format != "json":
        raise HTTPException(status_code=422, detail="format은 json 또는 md여야 합니다.")
    return log_api_response("GET /calls/{log_id}/report", report)


@router.post("/calls/{log_id}/feedback")
def create_feedback(log_id: int, body: FeedbackRequest) -> dict:
    """오탐 신고·피해 확인 등 사용자 피드백을 저장한다."""
    _require_call(log_id)
    if body.verdict not in FEEDBACK_VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict는 {', '.join(FEEDBACK_VERDICTS)} 중 하나여야 합니다.")
    feedback = state_repository.insert_feedback(
        log_id=log_id,
        verdict=body.verdict,
        comment=body.comment,
        damage=body.damage,
        source="app",
    )
    return log_api_response("POST /calls/{log_id}/feedback", feedback)
