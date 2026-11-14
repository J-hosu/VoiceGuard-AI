"""능동형 대응 루프: 상태 추적 → 위험도 융합 → 대응 결정을 한 사이클로 묶는다.

WebSocket(실시간)과 REST(통화 종료, 조회)가 같은 함수를 쓴다. 모든 함수는 동기 함수이며
Claude 호출이 들어 있으므로 이벤트 루프에서는 asyncio.to_thread로 실행해야 한다.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app import state_repository
from backend.app.codes import STAGE_TACTICS, phishing_type_label
from backend.app.repository import get_call_log
from backend.app.services import call_analyzer, claude_client, response_agent, state_tracker


logger = logging.getLogger(__name__)


def warm_up() -> None:
    """서버 시작 시 구조화 출력 스키마를 한 번씩 호출해 둔다.

    스키마를 처음 쓰는 요청은 문법 컴파일 때문에 20초 넘게 걸린다(이후 캐시됨).
    첫 통화의 첫 상태 갱신이 그 비용을 치르지 않도록 미리 데운다.
    """
    if not claude_client.is_configured():
        return
    targets = [
        ("상태 추적기", state_tracker.OUTPUT_SCHEMA, state_tracker.SYSTEM_PROMPT, claude_client.tracker_model()),
        ("대응 에이전트", response_agent.OUTPUT_SCHEMA, response_agent.SYSTEM_PROMPT, claude_client.agent_model()),
    ]
    for name, schema, system, model in targets:
        try:
            claude_client.complete_json(
                "[전사문]\n1. [신규] 여보세요\n\n준비 호출이다. 가장 짧게 답하라.",
                schema=schema,
                system=system,
                model=model,
                effort="low",
                max_tokens=1000,
            )
        except Exception as exc:
            logger.warning("%s 스키마 준비 호출 실패(첫 호출이 느릴 수 있음): %s", name, exc)


def run_cycle(log_id: int, force: bool = False) -> Optional[dict]:
    """새 발화를 반영해 상태를 갱신하고 필요한 행동을 결정한다.

    반환: {"state_update": WS state_update 페이로드, "actions": 새 행동 목록}
    갱신할 것이 없으면 None.
    """
    tracked = state_tracker.update_state(log_id, force=force)
    if tracked is None:
        return None

    detection = call_analyzer.apply_state_fusion(log_id)
    if detection is None:
        return None

    state = tracked["state"]
    actions = response_agent.decide(log_id, state, detection)
    return {
        "state_update": {
            "type": "state_update",
            **_public_state(state, detection),
            "utterance_tags": tracked["utterance_tags"],
            "entities": tracked["entities"],
        },
        "actions": actions,
    }


def retrack(log_id: int) -> Optional[dict]:
    """전체 재전사로 발화가 교체된 뒤 상태를 처음부터 다시 추적하고 통화를 마무리한다.

    통화는 이미 끝났으므로 통화 중 행동(경고 등)은 새로 만들지 않고, 통화 후 행동만 만든다.
    반환: 융합된 탐지 결과 dict. 재추적에 실패하면 None(기존 탐지 결과를 그대로 쓴다).
    """
    tracked = state_tracker.update_state(log_id, force=True, retag_all=True)
    detection = call_analyzer.apply_state_fusion(log_id) if tracked else None
    response_agent.on_call_end(log_id)
    return detection


def end_call(log_id: int) -> dict:
    """통화를 마무리한다. 남은 발화를 마지막으로 반영하고 통화 후 행동을 만든다.

    반환: {"state_update": 마지막 상태 갱신(없으면 None), "actions": 새 행동 목록, "final": 최종 요약}
    """
    cycle = run_cycle(log_id, force=True)
    actions = list(cycle["actions"]) if cycle else []
    actions.extend(response_agent.on_call_end(log_id))

    call = get_call_log(log_id)
    state = state_repository.get_latest_state(log_id)
    return {
        "state_update": cycle["state_update"] if cycle else None,
        "actions": actions,
        "final": {
            "stage": state["stage"] if state else "none",
            "stage_index": state["stage_index"] if state else 0,
            "scam_type": state["scam_type"] if state else "none",
            "phishing_type": state["phishing_type"] if state else phishing_type_label("none"),
            "summary": state["summary"] if state else "",
            "risk_score": round(call.risk_score, 4),
            "risk_level": call.risk_level,
        },
    }


def get_state_response(log_id: int) -> dict:
    """GET /calls/{log_id}/state 응답. 태그와 단서는 전체를 담는다."""
    call = get_call_log(log_id)
    state = state_repository.get_latest_state(log_id)
    risk = {"risk_score": round(call.risk_score, 4), "risk_level": call.risk_level}
    messages = state_repository.list_tagged_messages(log_id)
    tags = [
        {
            "message_id": message["message_id"],
            "turn_index": message["turn_index"],
            "role": message["role"],
            "tactics": message["tactics"],
        }
        for message in messages
        if message["role"] != "unknown" or message["tactics"]
    ]
    if state is None:
        return {
            "log_id": log_id,
            "state_version": 0,
            "stage": "none",
            "stage_index": 0,
            "scam_type": "none",
            "phishing_type": phishing_type_label("none"),
            "compliance": "none",
            **risk,
            "predicted_next": None,
            "summary": "",
            "utterance_tags": tags,
            "entities": state_repository.list_entities(log_id),
        }
    return {
        **_public_state(state, risk),
        "utterance_tags": tags,
        "entities": state_repository.list_entities(log_id),
    }


def get_timeline_response(log_id: int) -> dict:
    """GET /calls/{log_id}/timeline 응답. 단계 전환·발화·행동을 묶는다."""
    get_call_log(log_id)
    messages = state_repository.list_tagged_messages(log_id)

    stages: list[dict] = []
    previous_stage = "none"
    previous_last_turn = 0
    for state in state_repository.list_states(log_id):
        if state["stage"] != previous_stage and state["stage"] != "none":
            stages.append(
                {
                    "stage": state["stage"],
                    "stage_index": state["stage_index"],
                    # 이 상태 버전에서 새로 읽은 첫 발화부터 해당 단계가 시작된 것으로 본다.
                    "from_turn": previous_last_turn + 1,
                    "state_version": state["state_version"],
                    "at": state["created_at"],
                }
            )
        previous_stage = state["stage"]
        previous_last_turn = state["last_turn_index"]
    _align_stage_turns(stages, messages)

    return {
        "log_id": log_id,
        "stages": stages,
        "messages": [
            {
                "message_id": message["message_id"],
                "turn_index": message["turn_index"],
                "role": message["role"],
                "content": message["content"],
                "tactics": message["tactics"],
            }
            for message in messages
        ],
        "actions": state_repository.list_actions(log_id),
    }


def _align_stage_turns(stages: list[dict], messages: list[dict]) -> None:
    """단계 시작 발화(from_turn)를 현재 발화 목록 기준으로 맞춘다.

    상태 버전에 기록된 발화 번호는 실시간 청크 기준이라, 통화 종료 후 전체 재전사로 발화가
    교체되면 어긋난다. 그 단계를 드러내는 수법 태그가 처음 붙은 발화를 시작점으로 쓰고,
    그런 발화가 없으면 기록된 번호를 현재 발화 범위 안으로만 맞춘다.
    """
    if not messages:
        return
    last_turn = max(message["turn_index"] for message in messages)
    floor = 1
    for stage in stages:
        tactics = set(STAGE_TACTICS.get(stage["stage"], []))
        tagged = [
            message["turn_index"]
            for message in messages
            if message["turn_index"] >= floor and tactics.intersection(message["tactics"])
        ]
        stage["from_turn"] = tagged[0] if tagged else max(floor, min(stage["from_turn"], last_turn))
        floor = stage["from_turn"]


def _public_state(state: dict, risk: dict) -> dict:
    """내부 필드를 뺀 상태와 융합 위험도를 API 응답 형태로 합친다."""
    return {
        "log_id": state["log_id"],
        "state_version": state["state_version"],
        "stage": state["stage"],
        "stage_index": state["stage_index"],
        "scam_type": state["scam_type"],
        "phishing_type": state["phishing_type"],
        "compliance": state["compliance"],
        "risk_score": risk["risk_score"],
        "risk_level": risk["risk_level"],
        "predicted_next": state["predicted_next"],
        "summary": state["summary"],
    }
