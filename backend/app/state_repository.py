"""v2 저장소 계층: 대화 상태, 발화 태그, 수사 단서, 에이전트 행동, 피드백.

v1 저장소(repository.py)와 같은 SQLite 연결을 쓴다. 반환값은 API 응답에 그대로
실을 수 있는 dict다 (docs/API_SPEC_V2.md 참고).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from backend.app.codes import phishing_type_label, stage_index
from backend.app.database import get_connection


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 대화 상태


def _state_row_to_dict(row: Any) -> dict:
    data = dict(row)
    return {
        "log_id": data["log_id"],
        "state_version": data["version"],
        "stage": data["stage"],
        "stage_index": stage_index(data["stage"]),
        "scam_type": data["scam_type"],
        "phishing_type": phishing_type_label(data["scam_type"]),
        "compliance": data["compliance"],
        "tracker_risk": round(float(data["tracker_risk"]), 4),
        "predicted_next": data["predicted_next"],
        "summary": data["summary"] or "",
        "last_turn_index": data["last_turn_index"],
        "model_version": data["model_version"] or "",
        "created_at": data["created_at"],
    }


def get_latest_state(log_id: int) -> Optional[dict]:
    """통화의 최신 대화 상태를 반환한다. 아직 없으면 None."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM conversation_states WHERE log_id = ? ORDER BY version DESC LIMIT 1",
            (log_id,),
        ).fetchone()
    return _state_row_to_dict(row) if row else None


def list_states(log_id: int) -> list[dict]:
    """통화의 상태 이력을 버전 순으로 반환한다."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM conversation_states WHERE log_id = ? ORDER BY version ASC",
            (log_id,),
        ).fetchall()
    return [_state_row_to_dict(row) for row in rows]


def insert_state(
    log_id: int,
    stage: str,
    scam_type: str,
    compliance: str,
    tracker_risk: float,
    predicted_next: Optional[str],
    summary: str,
    last_turn_index: int,
    model_version: str,
) -> dict:
    """새 상태 버전을 저장하고 저장된 상태를 반환한다."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM conversation_states WHERE log_id = ?",
            (log_id,),
        ).fetchone()
        version = int(row["version"]) + 1
        connection.execute(
            """
            INSERT INTO conversation_states(
                log_id, version, stage, scam_type, compliance, tracker_risk,
                predicted_next, summary, last_turn_index, model_version, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                version,
                stage,
                scam_type,
                compliance,
                tracker_risk,
                predicted_next,
                summary,
                last_turn_index,
                model_version,
                _now(),
            ),
        )
    state = get_latest_state(log_id)
    assert state is not None
    return state


# ---------------------------------------------------------------- 발화 태그


def list_tagged_messages(log_id: int) -> list[dict]:
    """통화 발화를 역할·수법 태그와 함께 순서대로 반환한다."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, turn_index, content, role, tactics, created_at
            FROM call_messages
            WHERE log_id = ?
            ORDER BY turn_index ASC, id ASC
            """,
            (log_id,),
        ).fetchall()
    return [
        {
            "message_id": row["id"],
            "turn_index": row["turn_index"],
            "content": row["content"],
            "role": row["role"] or "unknown",
            "tactics": json.loads(row["tactics"] or "[]"),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def update_message_tags(log_id: int, tags: list[dict]) -> list[dict]:
    """발화별 역할·수법 태그를 저장하고, 실제로 바뀐 발화만 반환한다."""
    changed: list[dict] = []
    with get_connection() as connection:
        for tag in tags:
            row = connection.execute(
                "SELECT id, role, tactics FROM call_messages WHERE log_id = ? AND turn_index = ? ORDER BY id LIMIT 1",
                (log_id, tag["turn_index"]),
            ).fetchone()
            if row is None:
                continue
            new_tactics = json.dumps(tag["tactics"], ensure_ascii=False)
            if (row["role"] or "unknown") == tag["role"] and (row["tactics"] or "[]") == new_tactics:
                continue
            connection.execute(
                "UPDATE call_messages SET role = ?, tactics = ? WHERE id = ?",
                (tag["role"], new_tactics, row["id"]),
            )
            changed.append(
                {
                    "message_id": row["id"],
                    "turn_index": tag["turn_index"],
                    "role": tag["role"],
                    "tactics": tag["tactics"],
                }
            )
    return changed


# ---------------------------------------------------------------- 수사 단서


def normalize_entity(entity_type: str, value: str) -> str:
    """사건 간 연결에 쓸 수 있도록 단서 값을 정규화한다."""
    value = value.strip()
    if entity_type in ("account", "phone", "amount", "case_number"):
        digits = re.sub(r"\D", "", value)
        return digits or value.lower()
    if entity_type == "url":
        normalized = re.sub(r"^https?://", "", value.lower())
        return re.sub(r"^www\.", "", normalized).rstrip("/")
    return re.sub(r"\s+", "", value.lower())


def insert_entities(log_id: int, entities: list[dict]) -> list[dict]:
    """새 단서만 저장하고(통화 안에서 유형+정규화 값 기준 중복 제거) 저장된 것을 반환한다."""
    inserted: list[dict] = []
    with get_connection() as connection:
        for entity in entities:
            value = str(entity.get("value", "")).strip()
            entity_type = entity.get("entity_type", "")
            if not value or not entity_type:
                continue
            normalized = normalize_entity(entity_type, value)
            exists = connection.execute(
                "SELECT 1 FROM case_entities WHERE log_id = ? AND entity_type = ? AND normalized = ?",
                (log_id, entity_type, normalized),
            ).fetchone()
            if exists:
                continue
            connection.execute(
                """
                INSERT INTO case_entities(log_id, entity_type, value, normalized, turn_index, context)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (log_id, entity_type, value, normalized, entity.get("turn_index"), entity.get("context", "")),
            )
            inserted.append(
                {"entity_type": entity_type, "value": value, "turn_index": entity.get("turn_index")}
            )
    return inserted


def delete_entities(log_id: int) -> None:
    """통화의 단서를 모두 지운다. 전체 재전사로 발화 번호가 바뀐 뒤 다시 추출할 때 쓴다."""
    with get_connection() as connection:
        connection.execute("DELETE FROM case_entities WHERE log_id = ?", (log_id,))


def list_entities(log_id: int) -> list[dict]:
    """통화에서 추출된 단서 전체를 반환한다."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT entity_type, value, turn_index FROM case_entities WHERE log_id = ? ORDER BY id ASC",
            (log_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------- 에이전트 행동


def _action_row_to_dict(row: Any) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "log_id": data["log_id"],
        "action_type": data["action_type"],
        "title": data["title"],
        "message": data["message"],
        "reason": data["reason"] or "",
        "payload": json.loads(data["payload"] or "{}"),
        "status": data["status"],
        "response": data["response"] or "",
        "state_version": data["state_version"],
        "created_at": data["created_at"],
        "responded_at": data["responded_at"] or "",
    }


def insert_action(
    log_id: int,
    state_version: int,
    action_type: str,
    title: str,
    message: str,
    reason: str = "",
    payload: Optional[dict] = None,
) -> dict:
    """에이전트 행동 1건을 저장한다."""
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO agent_actions(log_id, state_version, action_type, title, message, reason, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                state_version,
                action_type,
                title.strip(),
                message.strip(),
                reason.strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                _now(),
            ),
        )
        action_id = int(cursor.lastrowid)
    return get_action(action_id)


def get_action(action_id: int) -> dict:
    """id로 에이전트 행동을 조회한다."""
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        raise ValueError(f"action_id={action_id} 행동을 찾을 수 없습니다.")
    return _action_row_to_dict(row)


def list_actions(log_id: int) -> list[dict]:
    """통화의 에이전트 행동을 시간순으로 반환한다."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM agent_actions WHERE log_id = ? ORDER BY id ASC",
            (log_id,),
        ).fetchall()
    return [_action_row_to_dict(row) for row in rows]


def update_action_response(action_id: int, status: str, response: str) -> dict:
    """사용자의 반응(확인/닫기/후속 확인 답)을 기록한다."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE agent_actions SET status = ?, response = ?, responded_at = ? WHERE id = ?",
            (status, response, _now(), action_id),
        )
    return get_action(action_id)


# ---------------------------------------------------------------- 피드백 · 보호자


def insert_feedback(
    log_id: int,
    verdict: str,
    comment: str = "",
    damage: Optional[dict] = None,
    source: str = "app",
) -> dict:
    """사용자 피드백 1건을 저장한다."""
    created_at = _now()
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO call_feedback(log_id, verdict, comment, damage, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (log_id, verdict, comment.strip(), json.dumps(damage, ensure_ascii=False), source, created_at),
        )
        feedback_id = int(cursor.lastrowid)
    return {
        "id": feedback_id,
        "log_id": log_id,
        "verdict": verdict,
        "comment": comment.strip(),
        "damage": damage,
        "source": source,
        "created_at": created_at,
    }


def list_feedback(log_id: int) -> list[dict]:
    """통화에 달린 피드백을 반환한다."""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM call_feedback WHERE log_id = ? ORDER BY id ASC",
            (log_id,),
        ).fetchall()
    return [{**dict(row), "damage": json.loads(row["damage"] or "null")} for row in rows]


def count_active_guardians(device_id: Optional[int]) -> int:
    """기기에 연결된 활성 보호자 수를 반환한다."""
    if device_id is None:
        return 0
    with get_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM guardians WHERE device_id = ? AND status = 'active'",
            (device_id,),
        ).fetchone()
    return int(row["count"])


# ---------------------------------------------------------------- 빠른 경로 점수


def get_latest_detection(log_id: int) -> Optional[dict]:
    """가장 최근 탐지 결과(빠른 경로 점수, 패턴, 근거)를 반환한다."""
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT risk_score, fast_score, matched_patterns, retrieved_case_ids, core_evidence, model_version
            FROM detection_results
            WHERE log_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (log_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["matched_patterns"] = json.loads(data["matched_patterns"] or "[]")
    data["retrieved_case_ids"] = json.loads(data["retrieved_case_ids"] or "[]")
    return data


def get_peak_fast_score(log_id: int) -> Optional[float]:
    """통화 중 기록된 빠른 경로 점수의 최고값을 반환한다. 없으면 None."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT MAX(fast_score) AS peak FROM detection_results WHERE log_id = ? AND fast_score IS NOT NULL",
            (log_id,),
        ).fetchone()
    return float(row["peak"]) if row and row["peak"] is not None else None


def get_latest_fast_score(log_id: int) -> Optional[float]:
    """가장 최근에 기록된 빠른 경로 점수를 반환한다. 없으면 None."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT fast_score FROM detection_results WHERE log_id = ? AND fast_score IS NOT NULL ORDER BY id DESC LIMIT 1",
            (log_id,),
        ).fetchone()
    return float(row["fast_score"]) if row else None
