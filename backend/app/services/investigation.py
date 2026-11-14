"""수사 지원: 사건 목록, 사건 개요, 통화 간 단서 연결, 유사 공식 사례.

통화 중에 쌓인 대화 상태·단서·대응 기록을 신고와 초동 조사에 바로 쓸 수 있는 형태로 묶는다.
docs/API_SPEC_V2.md 10절 참고.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app import state_repository
from backend.app.codes import DAMAGE_ANSWERS, FOLLOWUP_ANSWERS, SCAM_TYPES, STAGE_LABELS
from backend.app.database import get_connection
from backend.app.services import claude_client, official_cases, report_builder


logger = logging.getLogger(__name__)

# 값이 같으면 같은 사기 조직일 가능성이 높은 단서 유형. 기관명·이름·금액은 우연히 겹치기 쉬워 뺀다.
LINKABLE_TYPES = ("account", "phone", "url", "case_number")
MIN_LINK_DIGITS = 6  # 숫자 단서는 이보다 짧으면 연결 근거로 쓰지 않는다
OVERVIEW_MIN_TURNS = 4

OVERVIEW_SYSTEM = """너는 보이스피싱 의심 통화를 경찰·금융회사 담당자가 빠르게 파악하도록 정리하는 작성자다.
통화 전사문과 자동 분석 결과가 주어진다. 아래 형식의 한국어 평문으로 사건 개요를 쓴다.

- 3~5문장. 전사문에 실제로 나온 사실만 쓴다. 추측, 조언, 경고 문구는 쓰지 않는다.
- 제3자 시점의 과거형으로 쓴다("~라고 밝혔다", "~을 요구했다").
- 순서: 상대방이 내세운 신원과 명분 → 요구한 행동(정보, 앱, 송금)과 금액·계좌 → 사용자의 반응과 피해 여부.
- 전사는 음성 인식 결과라 오탈자가 있다. 확실하지 않은 고유명사는 "~로 들리는"이라고 적는다.
- 사용자가 통화 후 확인 질문에 답한 내용이 있으면 마지막 문장에 반영한다.
- 머리말, 목록, 마크다운 없이 문장만 쓴다."""


def _ensure_tables() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS case_overviews (
                log_id INTEGER PRIMARY KEY,
                basis TEXT NOT NULL,
                overview TEXT NOT NULL,
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            )
            """
        )


def list_cases(min_risk: float = 0.0, limit: int = 100) -> list[dict]:
    """통화를 최근순으로 요약해 반환한다(사건 목록 화면용)."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.name, c.file_type, c.risk_score, c.risk_level, c.phishing_type, c.created_at,
                   (SELECT COUNT(*) FROM call_messages m WHERE m.log_id = c.id) AS message_count,
                   (SELECT COUNT(*) FROM case_entities e WHERE e.log_id = c.id) AS entity_count,
                   (SELECT s.stage FROM conversation_states s WHERE s.log_id = c.id ORDER BY s.version DESC LIMIT 1) AS stage,
                   (SELECT s.scam_type FROM conversation_states s WHERE s.log_id = c.id ORDER BY s.version DESC LIMIT 1) AS scam_type,
                   (SELECT a.response FROM agent_actions a
                     WHERE a.log_id = c.id AND a.action_type = 'followup_check' AND a.status = 'answered'
                     ORDER BY a.id DESC LIMIT 1) AS followup_answer
            FROM call_logs c
            WHERE c.risk_score >= ?
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (min_risk, limit),
        ).fetchall()

    linked = _linked_call_ids()
    cases = []
    for row in rows:
        stage = row["stage"] or "none"
        scam_type = row["scam_type"] or "none"
        answer = row["followup_answer"] or ""
        cases.append(
            {
                "log_id": row["id"],
                "name": row["name"],
                "file_type": row["file_type"],
                "created_at": row["created_at"],
                "risk_score": round(row["risk_score"], 4),
                "risk_level": row["risk_level"],
                "scam_type": scam_type,
                # 상태 추적 없이 분석된 통화(파일 분석 등)는 통화 기록의 유형을 쓴다.
                "phishing_type": SCAM_TYPES[scam_type] if scam_type != "none" else (row["phishing_type"] or SCAM_TYPES["none"]),
                "stage": stage,
                "stage_label": STAGE_LABELS.get(stage, stage),
                "message_count": row["message_count"],
                "entity_count": row["entity_count"],
                "followup_answer": answer,
                "followup_label": FOLLOWUP_ANSWERS.get(answer, ""),
                "damage_reported": answer in DAMAGE_ANSWERS,
                "linked": row["id"] in linked,
            }
        )
    return cases


def _usable_for_link(entity_type: str, normalized: str) -> bool:
    if entity_type in ("account", "phone", "case_number"):
        return normalized.isdigit() and len(normalized) >= MIN_LINK_DIGITS
    return len(normalized) >= 6


def _linked_call_ids() -> set[int]:
    """다른 통화와 단서가 겹치는 통화 id 집합."""
    placeholders = ",".join("?" * len(LINKABLE_TYPES))
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT entity_type, normalized, GROUP_CONCAT(DISTINCT log_id) AS log_ids
            FROM case_entities
            WHERE entity_type IN ({placeholders})
            GROUP BY entity_type, normalized
            HAVING COUNT(DISTINCT log_id) > 1
            """,
            LINKABLE_TYPES,
        ).fetchall()
    linked: set[int] = set()
    for row in rows:
        if _usable_for_link(row["entity_type"], row["normalized"]):
            linked.update(int(value) for value in row["log_ids"].split(","))
    return linked


def find_links(log_id: int) -> list[dict]:
    """이 통화와 같은 계좌·전화번호·주소·사건번호가 나온 다른 통화를 찾는다."""
    placeholders = ",".join("?" * len(LINKABLE_TYPES))
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT mine.entity_type, mine.value, mine.normalized, other.log_id AS other_log_id,
                   other.value AS other_value, c.created_at, c.risk_score, c.phishing_type
            FROM case_entities mine
            JOIN case_entities other
              ON other.entity_type = mine.entity_type AND other.normalized = mine.normalized AND other.log_id != mine.log_id
            JOIN call_logs c ON c.id = other.log_id
            WHERE mine.log_id = ? AND mine.entity_type IN ({placeholders})
            ORDER BY other.log_id DESC
            """,
            (log_id, *LINKABLE_TYPES),
        ).fetchall()

    links: dict[int, dict] = {}
    for row in rows:
        if not _usable_for_link(row["entity_type"], row["normalized"]):
            continue
        link = links.setdefault(
            row["other_log_id"],
            {
                "log_id": row["other_log_id"],
                "called_at": row["created_at"],
                "risk_score": round(row["risk_score"], 4),
                "phishing_type": row["phishing_type"] or "",
                "shared": [],
            },
        )
        shared = {"entity_type": row["entity_type"], "value": row["value"]}
        if shared not in link["shared"]:
            link["shared"].append(shared)
    return list(links.values())


def get_overview(log_id: int, report: dict) -> str:
    """사건 개요를 반환한다. 통화 내용이 바뀌지 않았으면 저장해 둔 것을 쓴다.

    위험 통화(주의 이상)만 LLM으로 작성한다. 실패하거나 대상이 아니면 추적기 요약을 그대로 쓴다.
    """
    fallback = report["summary"]
    if report["risk_level"] == "low" or len(report["transcript"]) < OVERVIEW_MIN_TURNS or not claude_client.is_configured():
        return fallback

    followups = "|".join(f"{item['answer']}" for item in report["followups"])
    basis = f"{len(report['transcript'])}:{report['transcript'][-1]['turn_index']}:{report['stage']}:{followups}"
    _ensure_tables()
    with get_connection() as connection:
        row = connection.execute("SELECT basis, overview FROM case_overviews WHERE log_id = ?", (log_id,)).fetchone()
    if row and row["basis"] == basis:
        return row["overview"]

    role_labels = report_builder.ROLE_LABELS
    lines = [
        f"[자동 분석] 유형: {report['phishing_type']} / 도달 단계: {STAGE_LABELS.get(report['stage'], report['stage'])}",
        "[전사문]",
        *[f"{m['turn_index']}. [{role_labels.get(m['role'], '미상')}] {m['content']}" for m in report["transcript"][-120:]],
    ]
    if report["followups"]:
        lines.append("[통화 후 사용자 확인]")
        lines.extend(f"- {item['question']} → {FOLLOWUP_ANSWERS.get(item['answer'], item['answer'])}" for item in report["followups"])
    model = claude_client.report_model()
    try:
        overview = claude_client.complete("\n".join(lines), system=OVERVIEW_SYSTEM, max_tokens=1200, effort="low", model=model).strip()
    except Exception as exc:
        logger.warning("사건 개요 작성 실패. 추적기 요약을 사용합니다: %s", exc)
        return fallback
    if not overview:
        return fallback

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO case_overviews(log_id, basis, overview, model) VALUES (?, ?, ?, ?)
            ON CONFLICT(log_id) DO UPDATE SET basis = excluded.basis, overview = excluded.overview,
                model = excluded.model, created_at = strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')
            """,
            (log_id, basis, overview, model),
        )
    return overview


def similar_official_cases(report: dict, top_k: int = 2) -> list[dict]:
    """위험 통화에 한해 비슷한 공식 사례를 찾는다."""
    if report["risk_level"] == "low":
        return []
    # 사용자 발화보다 상대방 발화가 수법을 드러내므로 상대방 발화만 모아 비교한다(없으면 전체).
    everything = " ".join(m["content"] for m in report["transcript"])
    suspect = " ".join(m["content"] for m in report["transcript"] if m["role"] == "suspect")
    found = official_cases.find_similar(suspect or everything, scam_type=report["scam_type"], top_k=top_k)
    # 화자 추정이 어긋난 통화는 상대방 발화만으로는 못 찾을 수 있어 전체 전사로 한 번 더 찾는다.
    return found or official_cases.find_similar(everything, scam_type=report["scam_type"], top_k=top_k)


def enrich_report(report: dict) -> dict:
    """신고용 요약에 수사 지원 항목을 더한다(LLM 호출이 들어갈 수 있어 스레드에서 실행할 것)."""
    log_id = report["log_id"]
    report["overview"] = get_overview(log_id, report)
    report["links"] = find_links(log_id)
    report["similar_official_cases"] = similar_official_cases(report)
    report["damage_reported"] = any(item["answer"] in DAMAGE_ANSWERS for item in report["followups"])
    return report


def full_report(log_id: int) -> dict:
    """신고용 요약 + 사건 개요 + 연결된 통화 + 유사 공식 사례."""
    return enrich_report(report_builder.build_report(log_id))


def stats(cases: Optional[list[dict]] = None) -> dict:
    """사건 목록 상단에 보여줄 집계."""
    cases = cases if cases is not None else list_cases()
    risky = [case for case in cases if case["risk_level"] != "low"]
    by_type: dict[str, int] = {}
    for case in risky:
        by_type[case["phishing_type"] or "기타"] = by_type.get(case["phishing_type"] or "기타", 0) + 1
    return {
        "total": len(cases),
        "risky": len(risky),
        "high": sum(1 for case in cases if case["risk_level"] == "high"),
        "damage_reported": sum(1 for case in cases if case["damage_reported"]),
        "linked": sum(1 for case in cases if case["linked"]),
        "by_type": by_type,
    }
