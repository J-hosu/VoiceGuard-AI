"""신종 수법 수집 → AI 분석 → 사람 검토 → 탐지 반영.

1. 수집: 금감원·경찰청 등 공식 출처의 새 사례·경보를 가져온다(official_sources.fetch_recent).
   대시보드의 '지금 수집' 버튼이나 수사관 자연어 질의가 POST /knowledge/collect(또는 백그라운드 수집)를 부른다.
   n8n 정기 수집 템플릿(n8n/voiceguard_knowledge_collect.json)은 있지만 연결하지 않았다.
2. 분석: 글마다 Claude가 수법 카드(이름, 유형, 요약, 사기범 말투, 알아챌 신호, 대응)로 정리해 '검토 대기'로 저장한다.
   보이스피싱과 관계없는 글은 카드로 만들지 않는다(같은 글을 다시 분석하지 않도록 'skipped'로 남김).
3. 검토: 대시보드에서 사람이 승인·반려한다. 사람이 보지 않은 내용은 탐지에 들어가지 않는다.
4. 반영: 승인된 카드의 신호와 말투를 상태 추적기 프롬프트의 '최근 공식 경보 수법'에 넣는다.
   승인할 때마다 기본 회귀 평가(12개 대본)를 돌려 결과를 남긴다. 평가 DB가 비어 있어 승인한 카드는 평가 입력에 들어가지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from backend.app.database import get_connection
from backend.app.paths import DATA_DIR
from backend.app.services import claude_client


logger = logging.getLogger(__name__)

OFFICIAL_CASES_PATH = DATA_DIR / "official" / "cases.json"
TRACKER_CARD_LIMIT = 8
STATUSES = ("pending", "approved", "rejected", "skipped")
SCAM_TYPE_LABELS = {
    "agency": "수사기관·금융당국 사칭",
    "loan": "대출 빙자",
    "kidnap": "납치·사고 협박",
    "acquaintance": "가족·지인 사칭",
    "smishing": "문자·택배·카드 배송 사칭",
    "other": "기타",
}

CARD_SYSTEM = """너는 보이스피싱 대응 앱의 '신종 수법 분석가'다. 금융감독원·경찰청 같은 공식 기관이 공개한 피해 사례나
소비자 경보 글 하나를 받아, 실시간 통화 탐지에 쓸 '수법 카드' 하나로 정리한다. 카드는 사람이 검토한 뒤에만 탐지에 쓰인다.

- relevant: 전화·문자·메신저로 돈이나 정보를 빼내는 사기(보이스피싱, 스미싱, 기관·지인 사칭, 대출 사기 등)의 수법을 다루면 true.
  금융상품 안내, 일반 보도자료, 행사 안내처럼 수법이 없는 글은 false.
- title: 수법 이름 (40자 이내, 예: "카드 배송 기사 사칭 후 원격제어 앱 설치 유도").
- scam_type: agency, loan, kidnap, acquaintance, smishing, other 중 하나.
- summary: 수법이 어떻게 진행되는지 2~3문장.
- script_phrases: 사기범이 실제로 할 법한 말 3~6개(글에 나온 표현을 우선, 짧은 문장).
- red_flags: 통화·문자 중에 알아챌 수 있는 신호 3~6개.
- recommended_actions: 피해를 막는 행동 2~4개.
- target_groups: 주로 노리는 대상(없으면 빈 배열).
글에 없는 숫자나 사실은 만들지 않는다. 모두 한국어로 쓴다."""

CARD_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "title": {"type": "string"},
        "scam_type": {"type": "string", "enum": list(SCAM_TYPE_LABELS)},
        "summary": {"type": "string"},
        "script_phrases": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "target_groups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "title", "scam_type", "summary", "script_phrases", "red_flags", "recommended_actions", "target_groups"],
    "additionalProperties": False,
}

_collect_lock = threading.Lock()
_block_cache: dict = {"at": 0.0, "text": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                fetched INTEGER DEFAULT 0,
                new_cards INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                started_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
                finished_at TEXT DEFAULT ''
            )
            """
        )


def _card_row(row) -> dict:
    card = dict(row)
    for key in ("script_phrases", "red_flags", "recommended_actions", "target_groups"):
        try:
            card[key] = json.loads(card.get(key) or "[]")
        except ValueError:
            card[key] = []
    card["scam_type_label"] = SCAM_TYPE_LABELS.get(card.get("scam_type", ""), "기타")
    return card


def list_cards(status: str = "all", limit: int = 100) -> list[dict]:
    query = "SELECT * FROM knowledge_cards"
    params: tuple = ()
    if status != "all":
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY id DESC LIMIT ?"
    with get_connection() as connection:
        rows = connection.execute(query, (*params, limit)).fetchall()
    return [_card_row(row) for row in rows]


def get_card(card_id: int) -> Optional[dict]:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM knowledge_cards WHERE id = ?", (card_id,)).fetchone()
    return _card_row(row) if row else None


RECENT_DAYS = 60  # 예전 수집본(학습·유사 사례용)에 있어도 이 기간 안의 글은 수법 카드로 검토받게 한다


def _known_urls() -> set[str]:
    """이미 카드가 된 글과, 예전 수집본 중 오래된 글. 최근 글은 예전 수집본에 있어도 카드 후보로 남긴다."""
    urls: set[str] = set()
    with get_connection() as connection:
        urls.update(row["source_url"] for row in connection.execute("SELECT source_url FROM knowledge_cards").fetchall() if row["source_url"])
    cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    try:
        for item in json.loads(OFFICIAL_CASES_PATH.read_text(encoding="utf-8")):
            if str(item.get("date") or "") < cutoff:
                urls.add(item.get("url", ""))
    except (OSError, ValueError):
        pass  # 예전 수집본이 없으면 카드만으로 중복을 거른다
    urls.discard("")
    return urls


def _analyze(item: dict) -> dict:
    prompt = (
        f"[출처] {item.get('source_name', '')}\n[날짜] {item.get('date', '')}\n[제목] {item.get('title', '')}\n"
        f"[주소] {item.get('url', '')}\n[본문]\n{str(item.get('text', ''))[:6000]}"
    )
    return claude_client.complete_json(
        prompt, schema=CARD_SCHEMA, system=CARD_SYSTEM, max_tokens=2000, effort="low", model=claude_client.report_model()
    )


def _insert_card(item: dict, card: dict, status: str) -> Optional[int]:
    content_hash = hashlib.sha256(f"{item.get('url', '')}|{item.get('title', '')}".encode("utf-8")).hexdigest()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_cards(title, scam_type, summary, script_phrases, red_flags, recommended_actions,
                target_groups, source_name, source_url, published_at, content_hash, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (card.get("title") or item.get("title", ""))[:80],
                card.get("scam_type", "other"),
                card.get("summary", ""),
                json.dumps(card.get("script_phrases", [])[:6], ensure_ascii=False),
                json.dumps(card.get("red_flags", [])[:6], ensure_ascii=False),
                json.dumps(card.get("recommended_actions", [])[:4], ensure_ascii=False),
                json.dumps(card.get("target_groups", [])[:4], ensure_ascii=False),
                item.get("source_name", ""),
                item.get("url", ""),
                item.get("date", ""),
                content_hash,
                status,
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None


def collect(max_per_source: int = 8, trigger: str = "manual") -> dict:
    """공식 출처에서 새 글을 가져와 수법 카드 후보로 만든다. 동시에 두 번 돌지 않는다."""
    from backend.app.services import official_sources

    ensure_tables()
    if not _collect_lock.acquire(blocking=False):
        return {"status": "busy", "message": "이미 수집 중입니다."}
    with get_connection() as connection:
        run_id = connection.execute("INSERT INTO knowledge_runs(trigger) VALUES (?)", (trigger,)).lastrowid
    fetched = new_cards = skipped = 0
    created: list[int] = []
    error = ""
    try:
        if not claude_client.is_configured():
            raise RuntimeError("Claude API 키가 없어 수법을 분석할 수 없습니다.")
        known = _known_urls()
        items = official_sources.fetch_recent(max_per_source=max_per_source, known_urls=known)
        fetched = len(items)
        for item in items:
            if not item.get("url") or item["url"] in known:
                skipped += 1
                continue
            if not str(item.get("text", "")).strip():
                # 본문이 이미지뿐인 글(통합대응단 카드뉴스 등): 분석하지 않고 주소만 남겨 다음 실행에서 다시 받지 않는다.
                _insert_card(item, {"title": item.get("title", ""), "scam_type": "other", "summary": "본문이 이미지뿐이라 분석하지 않음"}, "skipped")
                skipped += 1
                continue
            try:
                card = _analyze(item)
            except Exception as exc:
                logger.warning("수법 카드 분석 실패(%s): %s", item.get("url"), exc)
                skipped += 1
                continue
            card_id = _insert_card(item, card, "pending" if card.get("relevant") else "skipped")
            if card_id and card.get("relevant"):
                new_cards += 1
                created.append(card_id)
            else:
                skipped += 1
    except Exception as exc:
        error = str(exc)[:300]
        logger.warning("신종 수법 수집 실패: %s", exc)
    finally:
        with get_connection() as connection:
            connection.execute(
                "UPDATE knowledge_runs SET status = ?, fetched = ?, new_cards = ?, skipped = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed" if error else "done", fetched, new_cards, skipped, error, _now(), run_id),
            )
        _collect_lock.release()
    return {"status": "failed" if error else "done", "run_id": run_id, "fetched": fetched, "new_cards": new_cards,
            "skipped": skipped, "error": error, "cards": [get_card(card_id) for card_id in created]}


def collect_in_background(max_per_source: int = 8, trigger: str = "manual") -> dict:
    if _collect_lock.locked():
        return {"status": "busy", "message": "이미 수집 중입니다."}
    threading.Thread(target=collect, kwargs={"max_per_source": max_per_source, "trigger": trigger}, daemon=True).start()
    return {"status": "started"}


def status() -> dict:
    ensure_tables()
    with get_connection() as connection:
        counts = {row["status"]: row["n"] for row in connection.execute("SELECT status, COUNT(*) AS n FROM knowledge_cards GROUP BY status").fetchall()}
        runs = [dict(row) for row in connection.execute("SELECT * FROM knowledge_runs ORDER BY id DESC LIMIT 5").fetchall()]
    return {"counts": {key: counts.get(key, 0) for key in STATUSES}, "collecting": _collect_lock.locked(), "runs": runs}


def review(card_id: int, decision: str, reviewer: str = "") -> dict:
    """사람의 검토 결과를 남긴다. 승인하면 추적기 지식에 들어가고 회귀 평가가 돈다."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision은 approve 또는 reject입니다.")
    new_status = "approved" if decision == "approve" else "rejected"
    with get_connection() as connection:
        connection.execute(
            "UPDATE knowledge_cards SET status = ?, reviewer = ?, reviewed_at = ? WHERE id = ?",
            (new_status, reviewer[:40], _now(), card_id),
        )
    _block_cache["at"] = 0.0  # 다음 추적기 호출 때 새 지식을 읽게 한다
    result = {"card": get_card(card_id), "eval_run": None}
    if decision == "approve":
        from backend.app.services import quality

        try:
            result["eval_run"] = quality.start_run("regression", trigger=f"knowledge:{card_id}")
        except Exception as exc:
            logger.warning("승인 뒤 회귀 평가를 시작하지 못함: %s", exc)
    return result


def tracker_knowledge_block() -> str:
    """승인된 최근 카드로 상태 추적기 프롬프트에 붙일 '최근 공식 경보 수법' 블록을 만든다(60초 캐시)."""
    if time.monotonic() - _block_cache["at"] < 60:
        return _block_cache["text"]
    try:
        cards = list_cards("approved", limit=TRACKER_CARD_LIMIT)
    except Exception:  # 표가 아직 없거나 DB를 못 읽으면 지식 없이 돈다
        cards = []
    if not cards:
        text = ""
    else:
        lines = [
            "",
            "",
            "[최근 공식 경보 수법] 금감원·경찰청이 공개한 사례를 사람이 검토해 승인한 수법이다. 통화가 이 흐름과 같으면",
            "그 유형으로 보고 신호가 쌓이는 만큼 위험도를 올린다. 단어 하나만 같다고 사기로 보지는 않는다.",
        ]
        for card in cards:
            phrases = " / ".join(f"“{phrase}”" for phrase in card["script_phrases"][:3])
            flags = "; ".join(card["red_flags"][:4])
            lines.append(f"- {card['title']} ({card['scam_type']}): 신호 — {flags}. 말투 — {phrases}")
        text = "\n".join(lines)
    _block_cache.update(at=time.monotonic(), text=text)
    return text
