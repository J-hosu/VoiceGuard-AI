"""법령 찾기와 절차 안내 REST 라우터. docs/investigation-guidance-design.md 참고.

법령 내용은 공식 출처에서 미리 받아 둔 조문(legal_kb)에서만 나오고, 담당 기관·기한 판정은 규칙표(case_guidance)가 한다.
수사 지원 화면용이라 관리 경로다(ADMIN_CODE가 있으면 관리 코드가 필요하다).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.app.repository import get_call_log
from backend.app.services import case_guidance, legal_kb, slack_bot


router = APIRouter()


class FactsRequest(BaseModel):
    """수사관이 확인한 사건 사실. 넣은 항목만 바뀌고, 값이 null이면 그 항목을 지운다."""

    facts: dict[str, Any] = Field(default_factory=dict, description="항목 이름 → 값. 항목은 GET /guidance/cases/{log_id}의 options 참고")


def _require_call(log_id: int) -> None:
    try:
        get_call_log(log_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/legal/status")
def legal_status() -> dict:
    """받아 둔 법령 목록, 시행일, 받은 시각."""
    return legal_kb.status()


@router.get("/legal/search")
def legal_search(q: str, law: str = "", group: str = "", limit: int = 8) -> dict:
    """받아 둔 조문에서 찾는다. law는 법령 이름이나 줄임말(예: 중수청법), group은 common·allocation·crossborder."""
    if not q.strip():
        raise HTTPException(status_code=422, detail="찾을 말(q)이 비어 있습니다.")
    if law.strip() and legal_kb.resolve_law(law) is None:
        raise HTTPException(status_code=404, detail="받아 둔 법령 가운데 그런 이름이 없습니다.")
    results = legal_kb.search(q, law=law.strip() or None, group=group.strip() or None, limit=limit)
    return {"query": q, "count": len(results), "results": results}


@router.get("/legal/provisions")
def legal_provision(law: str, article: str) -> dict:
    """조문 하나의 전문. 예: law=중수청법, article=제43조"""
    provision = legal_kb.get(law, article)
    if provision is None:
        raise HTTPException(status_code=404, detail="받아 둔 조문 가운데 그 조문이 없습니다.")
    return provision


@router.get("/guidance/cases/{log_id}")
async def case_guidance_view(log_id: int) -> dict:
    """통화 하나의 절차 안내: 사건 사실(확인한 값과 자동 추정), 담당 기관 판정, 해외 연계 안내, 기한, 근거 조문."""
    _require_call(log_id)
    return await asyncio.to_thread(case_guidance.guidance, log_id)


@router.post("/guidance/cases/{log_id}/facts")
async def save_case_facts(log_id: int, body: FactsRequest) -> dict:
    """수사관이 확인한 사건 사실을 저장하고, 다시 계산한 절차 안내를 돌려준다."""
    _require_call(log_id)
    try:
        await asyncio.to_thread(case_guidance.save_facts, log_id, body.facts, "", "dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await asyncio.to_thread(case_guidance.guidance, log_id)


@router.get("/guidance/deadlines")
async def guidance_deadlines(within_days: int = 7) -> dict:
    """사건 사실이 저장된 통화들의 기한 가운데 곧 닥치거나 막 지난 것."""
    within = min(max(int(within_days), 0), 60)
    items = await asyncio.to_thread(case_guidance.upcoming_deadlines, within)
    return {"within_days": within, "deadlines": items, "note": case_guidance.DEADLINE_NOTE}


@router.post("/guidance/deadlines/notify")
async def notify_guidance_deadlines() -> dict:
    """기한이 내일·오늘이거나 지난 사건을 지금 Slack으로 알린다(평소에는 서버가 15분마다 확인한다). 보낼 곳이 없으면 via가 빈 글이다."""
    return await asyncio.to_thread(slack_bot.notify_deadlines)
