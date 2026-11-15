"""통화 중 단서 즉시 대조.

상태 추적기가 통화에서 새 계좌·전화번호·주소·사건번호를 뽑으면, 이미 저장된 다른 통화에 같은 값이 있는지 바로 찾는다.
같은 계좌가 다른 의심 통화에도 나왔다면 통화 중인 사용자에게 바로 알리고(대응 카드), 수사 화면에는 연결로 남는다.
통화에 나온 주소는 접속하지 않고 모양만 점검한다(문자 검사와 같은 규칙).

알림 기준은 수사 화면의 연결 기준보다 엄격하다. 은행 대표번호(1588-…)처럼 사기범도 정상 통화도 말하는 번호로 알리면
정상 통화에 경고가 뜨므로, 계좌는 10자리 이상, 전화번호는 휴대전화·인터넷전화(010·070·050)만 쓴다.
"""

from __future__ import annotations

from backend.app.database import get_connection
from backend.app.state_repository import normalize_entity
from backend.app.services.message_analyzer import inspect_urls


ALERT_TYPES = ("account", "phone", "url", "case_number")
MAX_CALLS_PER_MATCH = 5


def _alertable(entity_type: str, normalized: str) -> bool:
    if entity_type == "account":
        return normalized.isdigit() and len(normalized) >= 10
    if entity_type == "phone":
        return normalized.isdigit() and len(normalized) >= 10 and normalized.startswith(("010", "070", "050"))
    if entity_type == "case_number":
        return len(normalized) >= 6
    if entity_type == "url":
        return len(normalized) >= 6
    return False


def match_entities(log_id: int, entities: list[dict]) -> list[dict]:
    """새로 뽑힌 단서 중 다른 통화에도 나온 것을 찾는다.

    반환: [{"entity_type", "value", "calls": [{"log_id", "called_at", "risk_level", "risk_score", "phishing_type"}]}]
    """
    matches: list[dict] = []
    with get_connection() as connection:
        for entity in entities:
            entity_type = entity.get("entity_type", "")
            value = str(entity.get("value", "")).strip()
            if entity_type not in ALERT_TYPES or not value:
                continue
            normalized = normalize_entity(entity_type, value)
            if not _alertable(entity_type, normalized):
                continue
            rows = connection.execute(
                """
                SELECT DISTINCT c.id, c.created_at, c.risk_level, c.risk_score, c.phishing_type
                FROM case_entities e JOIN call_logs c ON c.id = e.log_id
                WHERE e.entity_type = ? AND e.normalized = ? AND e.log_id != ?
                ORDER BY c.id DESC
                LIMIT ?
                """,
                (entity_type, normalized, log_id, MAX_CALLS_PER_MATCH),
            ).fetchall()
            if rows:
                matches.append(
                    {
                        "entity_type": entity_type,
                        "value": value,
                        "calls": [
                            {
                                "log_id": row["id"],
                                "called_at": row["created_at"],
                                "risk_level": row["risk_level"],
                                "risk_score": round(row["risk_score"], 4),
                                "phishing_type": row["phishing_type"] or "",
                            }
                            for row in rows
                        ],
                    }
                )
    return matches


def risky_urls(entities: list[dict]) -> list[dict]:
    """통화에 나온 주소 중 모양이 위험한 것(단축 주소, IP 주소, 기관 이름을 넣어 꾸민 주소 등)."""
    urls = [str(entity.get("value", "")) for entity in entities if entity.get("entity_type") == "url" and entity.get("value")]
    if not urls:
        return []
    return [found for found in inspect_urls(" ".join(urls)) if found["verdict"] == "suspicious"]
