"""탐지 품질·신종 수법·관계 그래프·증거 API (수사·운영 대시보드용).

docs/API_SPEC_V2.md 7·8·10절의 일부를 구현한다. /quality·/eval·/knowledge·/investigation은 관리 경로라
ACCESS_CODE와 ADMIN_CODE가 모두 설정돼 있으면 관리 코드가 있어야 열린다(access_gate.py). /calls/{id}/evidence는 참가자 경로다.
n8n 정기 수집 템플릿은 X-Admin-Code 헤더로 POST /knowledge/collect?wait=true 를 부르게 되어 있다(연결하지 않음).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.app.repository import get_call_log
from backend.app.services import evidence, investigation, quality, scam_knowledge


router = APIRouter()


class EvalRunRequest(BaseModel):
    suite: str = Field("regression", description="regression(기본 12개 대본), feedback(사용자 확인 대본), all")


class ReviewRequest(BaseModel):
    decision: str = Field(..., description="approve 또는 reject")
    reviewer: str = Field("", description="검토자 이름(선택)")


# ---------------------------------------------------------------- 품질 루프


@router.get("/quality/summary")
def quality_summary() -> dict:
    return quality.summary()


@router.get("/quality/retrain-candidates")
def retrain_candidates() -> Response:
    """사용자 확인이 있는 통화를 학습 데이터 형식(JSON)으로 내려받는다."""
    body = json.dumps(quality.retrain_candidates(), ensure_ascii=False, indent=1)
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="voiceguard_retrain_candidates.json"'},
    )


@router.post("/eval/run", status_code=202)
def run_eval(body: EvalRunRequest) -> dict:
    try:
        return quality.start_run(body.suite, trigger="dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/eval/runs")
def eval_runs(limit: int = 10) -> dict:
    return {"runs": quality.list_runs(limit=limit)}


# ---------------------------------------------------------------- 신종 수법


@router.post("/knowledge/collect")
async def collect_knowledge(wait: bool = False, max_per_source: int = 8) -> dict:
    """공식 출처에서 새 사례를 모아 수법 카드 후보로 만든다. wait=false면 백그라운드로 돌고 바로 돌아온다."""
    if wait:
        return await asyncio.to_thread(scam_knowledge.collect, max_per_source, "n8n")
    return scam_knowledge.collect_in_background(max_per_source, trigger="dashboard")


@router.get("/knowledge/status")
def knowledge_status() -> dict:
    return scam_knowledge.status()


@router.get("/knowledge/cards")
def knowledge_cards(status: str = "all", limit: int = 100) -> dict:
    if status not in ("all", *scam_knowledge.STATUSES):
        raise HTTPException(status_code=422, detail="status 값이 올바르지 않습니다.")
    return {"cards": scam_knowledge.list_cards(status=status, limit=limit)}


@router.post("/knowledge/cards/{card_id}/review")
def review_knowledge_card(card_id: int, body: ReviewRequest) -> dict:
    if scam_knowledge.get_card(card_id) is None:
        raise HTTPException(status_code=404, detail="수법 카드를 찾을 수 없습니다.")
    try:
        return scam_knowledge.review(card_id, body.decision, body.reviewer)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------- 관계 그래프 · 증거


@router.get("/investigation/graph")
def investigation_graph(min_risk: float = 0.5, limit: int = 80) -> dict:
    return investigation.graph(min_risk=min_risk, limit=limit)


@router.get("/calls/{log_id}/evidence")
def call_evidence(log_id: int) -> dict:
    try:
        get_call_log(log_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="통화 기록을 찾을 수 없습니다.") from exc
    return evidence.verify(log_id)
