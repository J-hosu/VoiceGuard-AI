"""보이스피싱 학습 데이터와 통화 로그를 저장하는 SQLite 도우미."""

from __future__ import annotations

import sqlite3

from backend.app.paths import DATA_DIR, DB_PATH


def get_connection() -> sqlite3.Connection:
    """컬럼 이름으로 접근할 수 있는 SQLite 연결을 생성한다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    """필요한 테이블이 없으면 새로 생성한다."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT DEFAULT '',
                text TEXT NOT NULL,
                label INTEGER NOT NULL CHECK (label IN (0, 1)),
                source TEXT DEFAULT '',
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_training_cases_label
            ON training_cases(label)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_case_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                turn_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                FOREIGN KEY (case_id) REFERENCES training_cases(id) ON DELETE CASCADE
            )
            """
        )
        _drop_column_if_exists(connection, "training_case_turns", "role")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_training_case_turns_case_id
            ON training_case_turns(case_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER,
                name TEXT DEFAULT '',
                file_type TEXT NOT NULL DEFAULT 'realtime',
                status TEXT NOT NULL DEFAULT 'normal',
                risk_score REAL NOT NULL DEFAULT 0,
                risk_level TEXT NOT NULL DEFAULT 'low',
                detected_label INTEGER NOT NULL DEFAULT 0 CHECK (detected_label IN (0, 1)),
                phishing_type TEXT DEFAULT '',
                core_evidence TEXT DEFAULT '',
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                updated_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            )
            """
        )
        _ensure_column(connection, "call_logs", "file_type", "TEXT NOT NULL DEFAULT 'realtime'")
        _ensure_column(connection, "call_logs", "phishing_type", "TEXT DEFAULT ''")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS call_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                turn_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
            )
            """
        )
        # v2: 상태 추적기가 채우는 발화별 화자 역할과 수법 태그.
        _ensure_column(connection, "call_messages", "role", "TEXT DEFAULT 'unknown'")
        _ensure_column(connection, "call_messages", "tactics", "TEXT DEFAULT '[]'")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_call_messages_log_id
            ON call_messages(log_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS detection_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                risk_score REAL NOT NULL,
                risk_level TEXT NOT NULL,
                detected_label INTEGER NOT NULL CHECK (detected_label IN (0, 1)),
                core_evidence TEXT NOT NULL,
                matched_patterns TEXT DEFAULT '[]',
                retrieved_case_ids TEXT DEFAULT '[]',
                model_version TEXT DEFAULT 'rag-v1',
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_detection_results_log_id
            ON detection_results(log_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
            )
            """
        )
        # v2: 융합 전 빠른 경로(KoELECTRA/규칙) 점수. 상태 추적기 점수와 다시 융합할 때 쓴다.
        _ensure_column(connection, "detection_results", "fast_score", "REAL")
        _init_v2_tables(connection)


V2_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    age_group TEXT DEFAULT '',
    interests TEXT DEFAULT '[]',
    consent TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS guardians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'telegram',
    chat_id TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    link_code TEXT DEFAULT '',
    link_expires_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_guardians_device_id ON guardians(device_id);
CREATE TABLE IF NOT EXISTS conversation_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'none',
    scam_type TEXT NOT NULL DEFAULT 'none',
    compliance TEXT NOT NULL DEFAULT 'none',
    tracker_risk REAL NOT NULL DEFAULT 0,
    predicted_next TEXT,
    summary TEXT DEFAULT '',
    last_turn_index INTEGER NOT NULL DEFAULT 0,
    model_version TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversation_states_log_id ON conversation_states(log_id, version);
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0,
    action_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    reason TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'delivered',
    response TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    responded_at TEXT DEFAULT '',
    FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_log_id ON agent_actions(log_id);
CREATE TABLE IF NOT EXISTS call_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    comment TEXT DEFAULT '',
    damage TEXT DEFAULT 'null',
    source TEXT DEFAULT 'app',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_call_feedback_log_id ON call_feedback(log_id);
CREATE TABLE IF NOT EXISTS case_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized TEXT NOT NULL,
    turn_index INTEGER,
    context TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_case_entities_log_id ON case_entities(log_id);
CREATE INDEX IF NOT EXISTS idx_case_entities_normalized ON case_entities(entity_type, normalized);
CREATE TABLE IF NOT EXISTS evidence_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    prev_hash TEXT DEFAULT '',
    chain_hash TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    FOREIGN KEY (log_id) REFERENCES call_logs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_evidence_files_log_id ON evidence_files(log_id, chunk_index);
CREATE TABLE IF NOT EXISTS knowledge_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    scam_type TEXT NOT NULL DEFAULT 'other',
    summary TEXT DEFAULT '',
    script_phrases TEXT DEFAULT '[]',
    red_flags TEXT DEFAULT '[]',
    recommended_actions TEXT DEFAULT '[]',
    target_groups TEXT DEFAULT '[]',
    source_name TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT DEFAULT '',
    reviewed_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_cards_hash ON knowledge_cards(content_hash);
CREATE TABLE IF NOT EXISTS detection_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER,
    pattern_label TEXT NOT NULL,
    regex TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    eval_run_id INTEGER,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    metrics TEXT DEFAULT '{}',
    passed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    sent_at TEXT DEFAULT ''
);
"""


def _init_v2_tables(connection: sqlite3.Connection) -> None:
    """v2(능동형 대응 루프·수사 지원) 테이블을 만든다. docs/API_SPEC_V2.md 3절 참고."""
    for statement in V2_TABLES_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    """기존 SQLite DB에 새 컬럼이 없으면 안전하게 추가한다."""
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_column_names = {column["name"] for column in columns}
    if column_name in existing_column_names:
        return

    connection.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
    )


def _drop_column_if_exists(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> None:
    """기존 SQLite DB에 남아 있는 더 이상 쓰지 않는 컬럼을 제거한다."""
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_column_names = {column["name"] for column in columns}
    if column_name not in existing_column_names:
        return

    connection.execute(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
