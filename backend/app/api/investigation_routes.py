"""수사 지원 REST 라우터와 대시보드 화면. docs/API_SPEC_V2.md 10절 참고."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.app.repository import get_call_log
from backend.app.services import investigation, investigator_assistant


router = APIRouter()
DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"


class AssistantTurn(BaseModel):
    """이전 대화 한 개(수사관 질문 또는 도우미 답)."""

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=8000)


class AssistantRequest(BaseModel):
    """수사관 자연어 질의. history가 없으면 session_id로 서버가 기억한 최근 대화를 잇는다."""

    message: str = Field(..., min_length=1, max_length=2000, description="수사관의 질문이나 명령")
    history: list[AssistantTurn] = Field(default_factory=list, max_length=40, description="이전 대화(선택). 최근 10개만 쓴다")
    session_id: str = Field("", max_length=128, description="대화 구분값(선택). n8n 채팅의 sessionId")


def _require_call(log_id: int) -> None:
    try:
        get_call_log(log_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/investigation/cases")
def list_investigation_cases(min_risk: float = 0.0, limit: int = 100) -> dict:
    """통화를 사건 목록으로 반환한다. min_risk로 위험 통화만 걸러 볼 수 있다."""
    cases = investigation.list_cases(min_risk=min_risk, limit=min(max(limit, 1), 500))
    return {"stats": investigation.stats(cases), "cases": cases}


@router.get("/investigation/cases/{log_id}")
async def get_investigation_case(log_id: int) -> dict:
    """사건 상세: 사건 개요, 단계 경과, 단서, 위험 발화, 대응 기록, 연결된 통화, 유사 공식 사례."""
    _require_call(log_id)
    return await asyncio.to_thread(investigation.full_report, log_id)


@router.get("/investigation/cases/{log_id}/links")
def get_investigation_links(log_id: int) -> dict:
    """같은 계좌·전화번호·주소·사건번호가 나온 다른 통화."""
    _require_call(log_id)
    return {"log_id": log_id, "links": investigation.find_links(log_id)}


@router.post("/investigation/assistant")
async def ask_investigation_assistant(body: AssistantRequest) -> dict:
    """수사관의 자연어 질문에 Claude가 수사 데이터를 도구로 찾아 답한다(n8n 채팅용, API_SPEC_V2.md 10.8).

    도구는 읽기만 하고, 새 수법 수집 시작(검토 대기 카드만 만듦)만 예외다. 관리 경로라 ADMIN_CODE가 있으면 관리 코드가 필요하다.
    """
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message가 비어 있습니다.")
    if not investigator_assistant.is_available():
        raise HTTPException(status_code=503, detail="Claude API 키가 설정되지 않아 답할 수 없습니다.")
    history = [turn.model_dump() for turn in body.history]
    return await asyncio.to_thread(investigator_assistant.answer, body.message, history, body.session_id)


@router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """수사 지원 대시보드 화면."""
    if not DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail="대시보드 화면 파일이 없습니다.")
    return FileResponse(DASHBOARD_PATH, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
