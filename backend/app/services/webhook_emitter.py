"""백엔드 → n8n 이벤트 전송. docs/API_SPEC_V2.md 9.1절.

N8N_WEBHOOK_URL이 설정돼 있으면 위험 통화·통화 종료·후속 확인 답을 n8n 웹훅으로 보낸다.
n8n 워크플로가 이 이벤트를 받아 보호자에게 텔레그램으로 알린다(n8n/voiceguard_guardian_alert.json).

통화 분석을 막지 않도록 전송은 별도 스레드에서 하고, 결과는 webhook_events 테이블에 남긴다.
실패하면 잠깐 쉬었다가 최대 3회까지 다시 보낸다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from backend.app.database import get_connection


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
TIMEOUT_SEC = 8


def webhook_url() -> str:
    return os.environ.get("N8N_WEBHOOK_URL", "").strip()


def is_configured() -> bool:
    return bool(webhook_url())


def public_base_url() -> str:
    """알림 메시지에 넣을 서비스 주소(예: https://voiceguard.example.com). 없으면 빈 문자열."""
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def emit(event: str, data: dict) -> None:
    """이벤트를 기록하고 뒤에서 전송한다. 웹훅이 설정되지 않았으면 아무것도 하지 않는다."""
    if not is_configured():
        return
    occurred_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO webhook_events(event, payload, status) VALUES (?, ?, 'pending')",
            (event, json.dumps(data, ensure_ascii=False)),
        )
        row_id = cursor.lastrowid
    body = {"event": event, "event_id": f"evt_{row_id:06d}", "occurred_at": occurred_at, "data": data}
    threading.Thread(target=_deliver, args=(row_id, body), daemon=True).start()


def send_now(event: str, data: dict) -> bool:
    """이벤트를 지금 보내고 전달됐는지 돌려준다. 전달을 확인해야 하는 알림(기한 알림)에 쓴다. 웹훅이 없으면 False."""
    if not is_configured():
        return False
    occurred_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO webhook_events(event, payload, status) VALUES (?, ?, 'pending')",
            (event, json.dumps(data, ensure_ascii=False)),
        )
        row_id = cursor.lastrowid
    return _deliver(row_id, {"event": event, "event_id": f"evt_{row_id:06d}", "occurred_at": occurred_at, "data": data})


def _deliver(row_id: int, body: dict) -> bool:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "VoiceGuard-Webhook/1.0"}
    secret = os.environ.get("N8N_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["X-Webhook-Secret"] = secret

    status = "failed"
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            request = urllib.request.Request(webhook_url(), data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                if 200 <= response.status < 300:
                    status = "sent"
                    break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("n8n 웹훅 전송 실패(%s, %d/%d): %s", body["event"], attempt, MAX_ATTEMPTS, exc)
        time.sleep(1.5 * attempt)

    with get_connection() as connection:
        connection.execute(
            "UPDATE webhook_events SET status = ?, attempts = ?, sent_at = strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') WHERE id = ?",
            (status, attempts, row_id),
        )
    return status == "sent"
