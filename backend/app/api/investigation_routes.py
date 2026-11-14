"""수사 지원 REST 라우터와 대시보드 화면. docs/API_SPEC_V2.md 10절 참고."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.app.repository import get_call_log
from backend.app.services import investigation


router = APIRouter()
DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"


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


@router.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """수사 지원 대시보드 화면."""
    if not DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail="대시보드 화면 파일이 없습니다.")
    return FileResponse(DASHBOARD_PATH, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
