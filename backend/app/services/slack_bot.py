"""Slack에서 절차 안내를 쓰는 창구. docs/slack.md 참고.

조회는 말과 명령으로, 입력은 양식으로 받는다.
- 봇을 부르면(@VoiceGuard 질문) 수사 지원 도우미가 저장된 조문과 수사 데이터를 찾아 스레드에 답한다. 도우미는 읽기만 한다.
- `/vg 사건 51`, `/vg 법령 범죄인 인도 청구`, `/vg 기한`은 규칙표와 조문 저장소의 결과를 그대로 보여 준다(AI를 거치지 않는다).
- `/vg 입력 51`이나 카드의 '사건 사실 입력' 버튼은 입력 양식을 띄운다. 저장하면 규칙표로 다시 계산해 채널에 올린다.
  사실을 AI가 옮겨 적다가 숫자나 날짜가 달라지면 판정이 바뀌므로, 입력은 양식으로만 받는다.
- 기한이 내일·오늘이거나 지난 사건은 정해 둔 채널에 알린다.

지키는 것
- Slack이 보낸 요청인지는 서명(SLACK_SIGNING_SECRET)으로 확인한다. 토큰과 서명 비밀값은 어디에도 기록하지 않는다.
- 사건을 볼 수 있는 채널(SLACK_ALLOWED_CHANNELS)과 사실을 넣을 수 있는 사람(SLACK_ALLOWED_USERS)은 적어 둔 것만 허용한다.
  비워 두면 아무도 쓸 수 없다(대시보드가 관리 코드로 막는 것과 같은 까닭이다). 모두에게 열려면 `*`를 적는다.
- Slack에는 3초 안에 답해야 하므로, 채널에 글을 올리는 일과 도우미의 답은 뒤에서 한다.

환경 변수: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_ALLOWED_CHANNELS, SLACK_ALLOWED_USERS, SLACK_TEAM_ID(선택),
SLACK_DEADLINE_CHANNEL, SLACK_DEADLINE_HOURS(기본 8-20), SLACK_DEADLINE_VIA_N8N(1이면 봇 대신 n8n 웹훅으로 알림).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from backend.app.database import get_connection
from backend.app.services import case_guidance, legal_kb, webhook_emitter

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"
TIMEOUT_SEC = 4  # 양식을 여는 호출은 3초 안에 끝나야 한다. 다른 호출도 오래 붙들지 않는다
MAX_SKEW_SEC = 300  # 이보다 오래된 요청은 다시 보낸 것(재전송 공격)일 수 있어 받지 않는다
SECTION_MAX = 2900  # Slack의 section 글자 한도는 3,000자
KEY_TEXT_MAX = 1500
SNIPPET_MAX = 500
ANSWER_MAX = 3800
MAX_BLOCKS = 45  # 메시지 하나에 블록 50개까지
LAW_RESULTS = 4
MAX_DEADLINE_LINES = 30
OVERDUE_DAYS = 7
MAX_WAITING_MENTIONS = 6
ACTION_OPEN_FACTS = "vg_open_facts"
ACTION_OPEN_DASHBOARD = "vg_open_dashboard"
CALLBACK_FACTS = "vg_facts"
INPUT_ACTION = "v"
CLEAR_DATES = "clear_dates"
LEVEL_LABELS = {"duty": "해야 할 일", "need": "입력 필요", "caution": "주의", "info": "안내"}
BASIC_RULES = ("alloc-roles", "alloc-warrant", "alloc-supplement", "alloc-supplement-info", "alloc-consult", "alloc-referral")
_KST = timezone(timedelta(hours=9))
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
_seen_lock = threading.Lock()
_seen_events: "OrderedDict[str, float]" = OrderedDict()
# 도우미의 답은 한 건에 수십 초가 걸릴 수 있다. 서버의 다른 일(명령 응답, 통화 분석)이 쓰는 실행기를 막지 않도록 따로 둔다.
_mention_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="slack-mention")
_job_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="slack-job")
_waiting_lock = threading.Lock()
_waiting_mentions = 0


class SlackError(RuntimeError):
    """Slack 호출이 실패했다. 문장에는 토큰이나 주소의 비밀 부분을 담지 않는다."""


# ---------------------------------------------------------------- 설정


def bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "").strip()


def signing_secret() -> str:
    return os.environ.get("SLACK_SIGNING_SECRET", "").strip()


def is_configured() -> bool:
    """Slack에서 오는 요청을 받을 수 있는지(봇 토큰과 서명 비밀값이 모두 있어야 한다)."""
    return bool(bot_token() and signing_secret())


def _id_set(name: str) -> set[str]:
    return {part.strip() for part in os.environ.get(name, "").split(",") if part.strip()}


def deadline_channel() -> str:
    return os.environ.get("SLACK_DEADLINE_CHANNEL", "").strip()


def _allowed(name: str, value: str) -> bool:
    """허용 목록에 있는지. 목록이 비어 있으면 아무도 허용하지 않는다. `*`는 모두 허용."""
    allowed = _id_set(name)
    return "*" in allowed or (bool(value) and value in allowed)


def _channel_allowed(channel: str) -> bool:
    return _allowed("SLACK_ALLOWED_CHANNELS", channel)


def _user_can_edit(user: str) -> bool:
    return _allowed("SLACK_ALLOWED_USERS", user)


def _team_allowed(team: str) -> bool:
    """SLACK_TEAM_ID를 적어 두면 그 워크스페이스에서 온 요청만 받는다."""
    expected = os.environ.get("SLACK_TEAM_ID", "").strip()
    return not expected or team == expected


def _channel_refusal(channel: str) -> str:
    return (f"이 채널(`{esc(channel or '알 수 없음')}`)은 허용 목록에 없습니다. "
            "서버 설정 SLACK_ALLOWED_CHANNELS에 이 채널 ID를 넣으면 쓸 수 있습니다.")


def _user_refusal(user: str) -> str:
    return (f"사건 사실을 입력할 권한이 없습니다(사용자 ID `{esc(user or '알 수 없음')}`). "
            "서버 설정 SLACK_ALLOWED_USERS에 이 ID를 넣어야 합니다.")


def kst_today() -> date:
    return case_guidance.today_kst()


# ---------------------------------------------------------------- 서명 확인과 호출


def verify_signature(timestamp: str, body: bytes, signature: str, now: Optional[float] = None) -> bool:
    """Slack이 보낸 요청인지 확인한다(HMAC-SHA256, 5분이 지난 요청은 받지 않는다)."""
    secret = signing_secret()
    if not secret or not timestamp or not signature:
        return False
    if not (timestamp.isascii() and timestamp.isdigit()):
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > MAX_SKEW_SEC:
        return False
    expected = "v0=" + hmac.new(secret.encode("utf-8"), b"v0:" + timestamp.encode("ascii") + b":" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8", "replace"))


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json; charset=utf-8", **headers}, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
        raw = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": raw.strip().lower() == "ok", "raw": raw[:80]}


def api(method: str, payload: dict) -> dict:
    """Slack Web API를 부른다. 실패하면 SlackError(이유만 담고 토큰은 담지 않는다)."""
    token = bot_token()
    if not token:
        raise SlackError("SLACK_BOT_TOKEN이 설정되지 않았습니다.")
    try:
        body = _post_json(f"{API_BASE}/{method}", payload, {"Authorization": f"Bearer {token}"})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SlackError(f"Slack 호출에 실패했습니다({method}): {type(exc).__name__}") from None
    if not body.get("ok"):
        raise SlackError(f"Slack이 요청을 받지 않았습니다({method}): {body.get('error', 'unknown')}")
    return body


def post_message(channel: str, text: str, blocks: Optional[list[dict]] = None, thread_ts: str = "") -> None:
    """채널에 글을 올린다. 글 속 주소의 미리보기는 끈다(Slack이 바깥 주소를 대신 불러오지 않게)."""
    payload: dict = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    api("chat.postMessage", payload)


def _respond(response_url: str, payload: dict) -> None:
    """명령이나 버튼이 준 답장 주소로 글을 올린다. Slack 주소가 아니면 보내지 않는다."""
    if not response_url.startswith("https://hooks.slack.com/"):
        raise SlackError("답장 주소가 Slack 주소가 아닙니다.")
    try:
        body = _post_json(response_url, {"unfurl_links": False, "unfurl_media": False, **payload}, {})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SlackError(f"Slack 답장에 실패했습니다: {type(exc).__name__}") from None
    if not body.get("ok", True):
        raise SlackError("Slack이 답장을 받지 않았습니다.")


# ---------------------------------------------------------------- 글 만들기


def esc(text: Any) -> str:
    """Slack 글에서 뜻이 있는 글자(&, <, >)를 바꾼다. 조문에는 '<개정 2026.9.29>' 같은 꺾쇠가 많다."""
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cut(text: str, limit: int, tail: str = "…") -> str:
    return text if len(text) <= limit else text[: max(limit - len(tail), 0)].rstrip() + tail


def link(url: str, label: str) -> str:
    """Slack 링크. 주소가 http(s)가 아니거나 링크 문법을 깨는 글자가 있으면 글자만 보인다."""
    url = str(url or "")
    if not re.match(r"^https?://", url) or any(char in url for char in "|<> \n"):
        return esc(label)
    return f"<{url.replace('&', '&amp;')}|{esc(label)}>"


def to_mrkdwn(text: str) -> str:
    """도우미가 쓴 Markdown을 Slack 글로 바꾼다(굵게, 제목, 목록, 링크)."""
    text = esc(text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    text = re.sub(r"(?m)^(\s*)[-*]\s+", lambda m: m.group(1) + "• ", text)
    text = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*$", lambda m: f"*{m.group(1).strip('*')}*", text)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"*{m.group(1)}*", text)
    return cut(text, ANSWER_MAX)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": cut(text, SECTION_MAX)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": cut(text, SECTION_MAX)}]}


def due_text(deadline: Optional[dict]) -> str:
    if not deadline:
        return ""
    if not deadline.get("due"):
        return f"기한: ‘{deadline.get('from_label', '')}’을 넣으면 계산"
    left = int(deadline["days_left"])
    when = f"지남({-left}일)" if left < 0 else "오늘까지" if left == 0 else f"{left}일 남음"
    return f"기한 {deadline['due']} · {when}"


def _basis_links(item: dict) -> str:
    return " · ".join(link(basis.get("url", ""), basis.get("ref", "")) for basis in item.get("basis", []))


def _basis_refs(item: dict) -> str:
    return ", ".join(esc(basis.get("ref", "")) for basis in item.get("basis", []))


def public_case_url(log_id: int) -> str:
    base = webhook_emitter.public_base_url()
    return f"{base}/dashboard#case={int(log_id)}" if base else ""


def _key_item_section(item: dict) -> dict:
    """해야 할 일·입력 필요·주의 항목 하나. 글이 길면 근거 링크를 조 이름만으로 줄이고, 그래도 길면 글을 줄였다고 밝힌다."""
    due = due_text(item.get("deadline"))
    head = f"*[{LEVEL_LABELS.get(item['level'], '안내')}] {esc(item['title'])}*" + (f"  ·  {due}" if due else "")
    alerts = ["_" + esc(alert) + "_" for alert in item.get("alerts", [])]
    body = esc(cut(item.get("text", ""), KEY_TEXT_MAX, " …(전체 글은 대시보드에 있습니다)"))
    for basis in (_basis_links(item), _basis_refs(item)):
        lines = [head, body] + (["근거: " + basis] if basis else []) + alerts
        text = "\n".join(lines)
        if len(text) <= SECTION_MAX:
            return _section(text)
    return _section("\n".join([head, esc(cut(item.get("text", ""), 900, " …(전체 글은 대시보드에 있습니다)")), "근거: " + cut(_basis_refs(item), 600)] + alerts))


def guidance_blocks(result: dict, name: str = "") -> list[dict]:
    """절차 안내를 Slack 카드로 만든다. 해야 할 일·입력 필요·주의는 글까지, 나머지는 제목과 근거 조문 이름을 보인다.

    항목을 말없이 빼지 않는다. 블록 수 한도 때문에 다 싣지 못하면 몇 개가 남았는지 적는다.
    """
    log_id = int(result["log_id"])
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": cut(f"사건 #{log_id} 절차 안내" + (f" · {name}" if name else ""), 150)}},
        _section(f"*{esc(result.get('headline', ''))}*"),
    ]
    notes = []
    if result.get("from_hints"):
        notes.append("자동 추정: " + ", ".join(esc(item["label"]) for item in result["from_hints"]))
    signals = [signal["word"] for signal in (result.get("hints") or {}).get("signals", []) if signal.get("strong")]
    if signals:
        notes.append("해외 연계 단서: " + ", ".join(esc(word) for word in signals))
    if notes:
        blocks.append(_context(" | ".join(notes)))

    items = list(result.get("items", []))
    key_items = [item for item in items if item.get("level") in ("duty", "need", "caution")]
    basics = [item for item in items if item.get("id") in BASIC_RULES and item not in key_items]
    rest = [item for item in items if item not in key_items and item not in basics]
    tail_blocks = 4  # 기본 절차, 참고 문구, 버튼, 남은 항목 알림
    left_out = 0
    for item in key_items:
        if len(blocks) >= MAX_BLOCKS - tail_blocks:
            left_out += 1
            continue
        blocks.append(_key_item_section(item))

    if rest and len(blocks) < MAX_BLOCKS - tail_blocks - 1:
        blocks.append({"type": "divider"})
        lines: list[str] = ["*안내*"]
        for item in rest:
            due = due_text(item.get("deadline"))
            refs = _basis_refs(item)
            line = f"• *{esc(item['title'])}*" + (f" ({due})" if due else "") + (f" — {cut(refs, 300)}" if refs else "")
            if sum(len(existing) + 1 for existing in lines) + len(line) > SECTION_MAX:
                if len(blocks) >= MAX_BLOCKS - tail_blocks:
                    left_out += 1
                    continue
                blocks.append(_section("\n".join(lines)))
                lines = []
            lines.append(line)
        if lines and lines != ["*안내*"]:
            if len(blocks) < MAX_BLOCKS - tail_blocks:
                blocks.append(_section("\n".join(lines)))
            else:
                left_out += len(lines)
    elif rest:
        left_out += len(rest)
    if left_out:
        blocks.append(_context(f"이 밖에 {left_out}개 항목은 대시보드에서 볼 수 있습니다."))
    if basics:
        blocks.append(_context("기본 절차: " + ", ".join(esc(item["title"]) for item in basics) + " (대시보드에서 볼 수 있습니다)"))
    blocks.append(_context("받아 둔 조문과 규칙표로 계산한 참고 자료이며 법률 자문이 아닙니다. 판정은 규칙표가 했고 AI는 관여하지 않았습니다."))

    buttons = [{"type": "button", "action_id": ACTION_OPEN_FACTS, "style": "primary", "value": str(log_id),
                "text": {"type": "plain_text", "text": "사건 사실 입력"}}]
    url = public_case_url(log_id)
    if url:
        buttons.append({"type": "button", "action_id": ACTION_OPEN_DASHBOARD, "url": url, "text": {"type": "plain_text", "text": "대시보드에서 열기"}})
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


def legal_blocks(query: str, results: list[dict]) -> list[dict]:
    """조문 찾기 결과. 조문은 받아 둔 글 그대로이며 앞부분만 보이고, 전체는 국가법령정보센터 링크로 연다."""
    if not results:
        return [_section(f"‘{esc(cut(query, 80))}’에 맞는 조문을 받아 둔 법령에서 찾지 못했습니다. 법령 이름이나 조 번호를 넣어 보십시오(예: `/vg 법령 중수청법 43조`).")]
    blocks = [_section(f"*조문 찾기* · ‘{esc(cut(query, 80))}’ · {len(results)}건")]
    for item in results:
        title = link(item.get("url", ""), item["ref"] + (f" ({item['heading']})" if item.get("heading") else ""))
        flags = [f"시행 {item.get('effective_date', '')}"]
        if item.get("article_changed"):
            flags.append("새 판에서 바뀐 조문(다시 받아야 함)")
        elif item.get("law_changed"):
            flags.append("법령의 새 판 시행(다시 받아야 함)")
        if item.get("upcoming_change"):
            flags.append(f"{item['upcoming_change']} 개정 예정")
        lines = [f"*{title}*", esc(" · ".join(flags)), esc(cut(item.get("text", ""), SNIPPET_MAX))]
        if item.get("pending_note"):
            lines.append("_시행일이 따로 정해진 부분: " + esc(cut(item["pending_note"], 300)) + "_")
        if item.get("law_note"):
            lines.append("_서명 문구: " + esc(cut(item["law_note"], 300)) + "_")
        blocks.append(_section("\n".join(lines)))
    status = legal_kb.status()
    blocks.append(_context(f"AI가 만든 글이 아니라 미리 받아 둔 조문 그대로입니다(법령·조약 {status['law_count']}개, 받은 날 {str(status.get('latest_fetch', ''))[:10]})."))
    return blocks


def help_blocks() -> list[dict]:
    return [
        _section("*VoiceGuard 수사 지원 사용법*\n"
                 "• `/vg 사건 51` — 사건의 절차 안내(통보 여부, 기한, 해외 연계 절차)\n"
                 "• `/vg 법령 범죄인 인도 청구` — 받아 둔 조문 찾기(법령 이름과 조 번호도 됩니다: `중수청법 43조`)\n"
                 "• `/vg 입력 51` — 사건 사실 입력 양식(이득액, 인지한 날, 피의자 국외 여부 등)\n"
                 "• `/vg 기한` — 곧 닥치는 기한 목록\n"
                 "• 봇을 부르고 질문하기(`@VoiceGuard 51번 사건은 어디에 통보해야 해?`) — 도우미가 조문을 찾아 스레드에 답합니다"),
        _context("조회는 말과 명령으로, 입력은 양식으로 받습니다. 판정은 규칙표가 하고 도우미는 설명만 합니다."),
    ]


def deadline_line(item: dict) -> str:
    left = int(item["days_left"])
    when = f"{-left}일 지남" if left < 0 else "오늘까지" if left == 0 else "내일까지" if left == 1 else f"{left}일 남음"
    name = f" {esc(cut(item['name'], 40))}" if item.get("name") else ""
    return f"*사건 #{item['log_id']}*{name} — {esc(item['title'])}: {item['due']} ({when})"


def deadline_blocks(items: list[dict], within_days: int) -> list[dict]:
    if not items:
        return [_section(f"앞으로 {within_days}일 안에 닥치는 기한이 없습니다. 기한은 ‘인지한 날’ 같은 날짜를 넣은 사건만 계산합니다(예: `/vg 입력 51`).")]
    lines = ["*곧 닥치는 기한*"]
    for item in items[:MAX_DEADLINE_LINES]:
        line = "• " + deadline_line(item)
        if sum(len(existing) + 1 for existing in lines) + len(line) > SECTION_MAX:
            break
        lines.append(line)
    blocks = [_section("\n".join(lines))]
    shown = len(lines) - 1
    if shown < len(items):
        blocks.append(_context(f"이 밖에 {len(items) - shown}건이 더 있습니다. 전체는 대시보드에서 볼 수 있습니다."))
    blocks.append(_context("기한은 시작일에서 계산한 참고값입니다. 공휴일과 기관별 기준은 반영하지 않았습니다."))
    return blocks


# ---------------------------------------------------------------- 입력 양식


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": cut(text, 75)}, "value": value}


_BOOL_OPTIONS = [_option("모름", "unknown"), _option("예", "yes"), _option("아니오", "no")]


def facts_modal(result: dict, meta: dict) -> dict:
    """사건 사실 입력 양식.

    수사관이 확인해 저장한 값만 채워 보여 준다. 통화 기록에서 자동 추정한 값은 칸 아래에 적어 두고 ‘모름’으로 둔다.
    그래야 아무것도 바꾸지 않고 저장했을 때 추정값이 확인한 사실로 바뀌지 않는다.
    죄명 후보는 추정한 값에 표시를 해 두되, 그대로 두고 저장하면 추정으로 남긴다(private_metadata의 hinted_charges로 견준다).
    """
    log_id = int(result["log_id"])
    confirmed, hints = result.get("facts", {}), result.get("hints", {}) or {}
    options = result.get("options", {})

    def block(block_id: str, label: str, element: dict, help_text: str = "") -> dict:
        made = {"type": "input", "block_id": block_id, "optional": True, "label": {"type": "plain_text", "text": cut(label, 1900)},
                "element": {**element, "action_id": INPUT_ACTION}}
        if help_text.strip():
            made["hint"] = {"type": "plain_text", "text": cut(help_text.strip(), 1900)}
        return made

    charge_options = [_option(item["label"], item["code"]) for item in options.get("charges", [])]
    charges: dict = {"type": "checkboxes", "options": charge_options}
    charge_help = (options.get("help") or {}).get("charges", "")
    hinted_charges = None
    if "charges" in confirmed:
        shown = confirmed["charges"]
    else:
        shown = hints.get("charges") or []
        if shown:
            hinted_charges = list(shown)
            charge_help += " 표시된 값은 통화 기록에서 자동 추정한 것입니다. 그대로 두면 추정으로 남고, 바꾸면 확인한 값으로 저장됩니다."
    chosen = [option for option in charge_options if option["value"] in shown]
    if chosen:
        charges["initial_options"] = chosen

    amount: dict = {"type": "plain_text_input", "placeholder": {"type": "plain_text", "text": "예: 600000000 또는 6억"}}
    if confirmed.get("gain_amount") is not None:
        amount["initial_value"] = str(confirmed["gain_amount"])
    amount_help = (options.get("help") or {}).get("gain_amount", "")
    if hints.get("amount_mentioned"):
        amount_help += f" 통화에서 요구한 금액은 {int(hints['amount_mentioned']):,}원입니다. 이득액과 다를 수 있어 자동으로 넣지 않습니다."

    blocks = [
        _section(f"*사건 #{log_id}* · 확인한 값만 저장됩니다. 자동 추정한 값은 칸 아래에 적혀 있으며, 맞으면 ‘예’를 골라 확정합니다. "
                 "‘모름’으로 두면 확인한 값이 지워집니다."),
        block("charges", "죄명 후보", charges, charge_help),
        block("gain_amount", "이득액(원)", amount, amount_help),
    ]
    filled_dates = []
    for item in options.get("dates", []):
        element: dict = {"type": "datepicker"}
        if confirmed.get(item["fact"]):
            element["initial_date"] = confirmed[item["fact"]]
            filled_dates.append(item)
        blocks.append(block(item["fact"], item["label"], element, item.get("help", "")))
    if filled_dates:  # Slack의 날짜 칸은 한 번 고르면 비울 수 없어, 지우는 칸을 따로 둔다
        blocks.append(block(CLEAR_DATES, "넣어 둔 날짜 지우기", {"type": "checkboxes", "options": [_option(f"‘{item['label']}’ 지우기", item["fact"]) for item in filled_dates]},
                            "잘못 넣은 날짜를 지울 때만 표시합니다."))
    for item in options.get("bools", []):
        value = confirmed.get(item["fact"])
        initial = _BOOL_OPTIONS[1] if value is True else _BOOL_OPTIONS[2] if value is False else _BOOL_OPTIONS[0]
        help_text = item.get("help", "")
        if value is None and hints.get(item["fact"]) is not None:
            help_text += f" 통화 기록에서 자동 추정한 값은 ‘{'예' if hints[item['fact']] else '아니오'}’입니다. 맞으면 같은 값을 골라 확정합니다."
        blocks.append(block(item["fact"], item["label"], {"type": "static_select", "options": _BOOL_OPTIONS, "initial_option": initial}, help_text))
    meta_out = {**meta, "log_id": log_id}
    if hinted_charges is not None:
        meta_out["hinted_charges"] = hinted_charges
    return {
        "type": "modal", "callback_id": CALLBACK_FACTS,
        "private_metadata": json.dumps(meta_out, ensure_ascii=False)[:2900],
        "title": {"type": "plain_text", "text": "사건 사실 입력"},
        "submit": {"type": "plain_text", "text": "저장하고 다시 계산"},
        "close": {"type": "plain_text", "text": "닫기"},
        "blocks": blocks,
    }


_EOK = re.compile(r"^(\d+(?:\.\d+)?)\s*억\s*(?:(\d+(?:\.\d+)?)\s*(천만|만))?\s*원?$")


def parse_amount(text: str) -> Optional[int]:
    """이득액 칸의 글을 원 단위 숫자로 바꾼다. 읽는 모양은 숫자(쉼표 가능)와 ‘6억’, ‘6.5억’, ‘1억 5천만’, ‘1억 5000만’뿐이다.

    ‘1억5천’처럼 단위를 줄인 말은 뜻이 갈릴 수 있어 받지 않는다(ValueError).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    digits = re.sub(r"[,\s원]", "", raw)
    if digits.isascii() and digits.isdigit():
        return int(digits)
    matched = _EOK.match(raw.replace(",", ""))
    if matched:
        amount = float(matched.group(1)) * 100_000_000
        if matched.group(2):
            amount += float(matched.group(2)) * (10_000_000 if matched.group(3) == "천만" else 10_000)
        return int(round(amount))
    raise ValueError("이득액은 숫자(원)로 넣습니다. 예: 600000000, 6억, 1억 5천만")


def facts_from_view(view: dict, meta: Optional[dict] = None) -> tuple[dict, dict]:
    """양식에 넣은 값을 사건 사실로 바꾼다. (사실, 칸별 오류)를 돌려준다.

    자동 추정으로 표시해 둔 죄명 후보를 그대로 둔 채 저장했으면 죄명은 넘기지 않는다(추정으로 남긴다).
    """
    values = (view.get("state") or {}).get("values") or {}
    meta = meta or {}
    facts: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def field(block_id: str) -> dict:
        return (values.get(block_id) or {}).get(INPUT_ACTION) or {}

    chosen = [option["value"] for option in field("charges").get("selected_options") or []]
    hinted = meta.get("hinted_charges")
    if not (isinstance(hinted, list) and sorted(hinted) == sorted(chosen)):
        facts["charges"] = chosen
    try:
        facts["gain_amount"] = parse_amount(field("gain_amount").get("value") or "")
    except ValueError as exc:
        errors["gain_amount"] = str(exc)
    for fact in case_guidance.DATE_FACTS:
        if fact in values:
            facts[fact] = field(fact).get("selected_date") or None
    for option in field(CLEAR_DATES).get("selected_options") or []:
        if option.get("value") in case_guidance.DATE_FACTS:
            facts[option["value"]] = None
    for fact in case_guidance.BOOL_FACTS:
        if fact in values:
            picked = (field(fact).get("selected_option") or {}).get("value")
            facts[fact] = True if picked == "yes" else False if picked == "no" else None
    return facts, errors


def _changes_text(changes: dict, hints: dict) -> str:
    parts = []
    for key, value in changes.items():
        label = case_guidance.FACT_LABELS.get(key, key)
        if value is None:
            shown = "지움" + ("(통화 기록의 자동 추정이 다시 쓰입니다)" if hints.get(key) not in (None, []) else "")
        elif key == "charges":
            shown = ", ".join(case_guidance.CHARGES.get(code, code).split("(")[0] for code in value) or "없음"
        elif key == "gain_amount":
            shown = f"{int(value):,}원"
        elif isinstance(value, bool):
            shown = "예" if value else "아니오"
        else:
            shown = str(value)
        parts.append(f"{esc(label)}: {esc(shown)}")
    return " / ".join(parts)


# ---------------------------------------------------------------- 명령


_COMMANDS = {
    "사건": "case", "안내": "case", "case": "case",
    "법령": "law", "조문": "law", "law": "law",
    "입력": "facts", "사실": "facts", "facts": "facts",
    "기한": "deadlines", "deadline": "deadlines", "deadlines": "deadlines",
    "도움말": "help", "help": "help",
}


def parse_command(text: str) -> tuple[str, str]:
    """명령 글을 (종류, 나머지)로 나눈다. 아는 명령어가 아니면 조문 찾기로 본다."""
    text = (text or "").strip()
    if not text:
        return "help", ""
    head, _, rest = text.partition(" ")
    if head.lstrip("#").isdigit():
        return "case", head.lstrip("#")
    kind = _COMMANDS.get(head.lower())
    if kind is None:
        return "law", text
    return kind, rest.strip()


def _log_id(text: str) -> Optional[int]:
    found = re.search(r"\d{1,9}", text or "")
    return int(found.group(0)) if found else None


def _ephemeral(text: str, blocks: Optional[list[dict]] = None) -> dict:
    payload: dict = {"response_type": "ephemeral", "text": text}
    if blocks:
        payload["blocks"] = blocks
    return payload


def _case_name(log_id: int) -> str:
    from backend.app.repository import get_call_log

    try:
        return get_call_log(log_id).name or ""
    except ValueError:
        return ""


def open_facts_modal(trigger_id: str, log_id: int, meta: dict) -> None:
    result = case_guidance.guidance(log_id, kst_today())
    api("views.open", {"trigger_id": trigger_id, "view": facts_modal(result, meta)})


def handle_command(form: dict) -> Optional[dict]:
    """`/vg …` 명령을 처리해 Slack에 돌려줄 답을 만든다. 양식을 띄운 경우에는 None(빈 답)."""
    channel, user = form.get("channel_id", ""), form.get("user_id", "")
    kind, arg = parse_command(form.get("text", ""))
    if kind == "help":
        return _ephemeral("VoiceGuard 수사 지원 사용법", help_blocks())
    if not _team_allowed(form.get("team_id", "")):
        return _ephemeral("이 워크스페이스에서는 쓸 수 없습니다.")
    if not _channel_allowed(channel):
        return _ephemeral(_channel_refusal(channel))
    if kind == "law":
        if not arg:
            return _ephemeral("찾을 말을 함께 넣어 주십시오. 예: `/vg 법령 범죄인 인도 청구`")
        results = legal_kb.search(arg, limit=LAW_RESULTS)
        return {"response_type": "in_channel", "text": f"조문 찾기: {arg}", "blocks": legal_blocks(arg, results)}
    if kind == "deadlines":
        items = case_guidance.upcoming_deadlines(within_days=7, today=kst_today())
        return {"response_type": "in_channel", "text": "곧 닥치는 기한", "blocks": deadline_blocks(items, 7)}

    log_id = _log_id(arg)
    if log_id is None:
        return _ephemeral("사건 번호를 함께 넣어 주십시오. 예: `/vg 사건 51`")
    try:
        if kind == "facts":
            if not _user_can_edit(user):
                return _ephemeral(_user_refusal(user))
            open_facts_modal(form.get("trigger_id", ""), log_id, {"channel": channel, "response_url": form.get("response_url", "")})
            return None
        result = case_guidance.guidance(log_id, kst_today())
    except ValueError:
        return _ephemeral(f"사건 #{log_id}을 찾지 못했습니다.")
    except SlackError as exc:
        logger.warning("Slack 입력 양식을 열지 못했습니다: %s", exc)
        return _ephemeral("입력 양식을 열지 못했습니다. 잠시 뒤 다시 시도해 주십시오.")
    return {"response_type": "in_channel", "text": f"사건 #{log_id} 절차 안내: {result['headline']}", "blocks": guidance_blocks(result, _case_name(log_id))}


# ---------------------------------------------------------------- 버튼과 양식 제출


def _announce(meta: dict, text: str, blocks: list[dict]) -> None:
    """다시 계산한 안내를 채널에 올린다. 봇이 채널에 없으면 명령이 준 답장 주소로 올린다."""
    channel, response_url = meta.get("channel", ""), meta.get("response_url", "")
    if channel:
        try:
            post_message(channel, text, blocks, meta.get("thread_ts", ""))
            return
        except SlackError as exc:
            if not response_url:
                raise
            logger.info("채널에 직접 올리지 못해 답장 주소로 올립니다: %s", exc)
    if not response_url:
        raise SlackError("올릴 채널을 알 수 없습니다.")
    _respond(response_url, {"response_type": "in_channel", "replace_original": False, "text": text, "blocks": blocks})


def announce_saved(meta: dict, user: str, log_id: int, note: str) -> None:
    """저장한 뒤의 안내를 채널에 올린다(뒤에서 돈다). 올리지 못하면 저장한 사람에게만 보이는 글로 알린다."""
    try:
        result = case_guidance.guidance(log_id, kst_today())
        _announce(meta, f"사건 #{log_id} 사건 사실 저장: {result['headline']}", [_context(note)] + guidance_blocks(result, _case_name(log_id)))
        return
    except (SlackError, ValueError) as exc:
        logger.warning("다시 계산한 안내를 채널에 올리지 못했습니다: %s", exc)
    notice = f"사건 #{log_id}의 사건 사실은 저장했습니다. 다만 채널에 올리지는 못했습니다. 봇을 채널에 초대한 뒤 `/vg 사건 {log_id}`로 확인해 주십시오."
    try:
        if meta.get("channel") and user:
            api("chat.postEphemeral", {"channel": meta["channel"], "user": user, "text": notice})
        elif meta.get("response_url"):
            _respond(meta["response_url"], {"response_type": "ephemeral", "replace_original": False, "text": notice})
    except SlackError as exc:
        logger.warning("저장 결과를 사용자에게 알리지 못했습니다: %s", exc)


def _result_view(text: str) -> dict:
    return {"type": "modal", "title": {"type": "plain_text", "text": "사건 사실 입력"}, "close": {"type": "plain_text", "text": "닫기"},
            "blocks": [_section(text)]}


def handle_interaction(payload: dict, defer: Optional[Callable] = None) -> Optional[dict]:
    """버튼 누름과 양식 제출을 처리한다. Slack에 돌려줄 답이 없으면 None.

    defer(function, *args)를 주면 채널에 글을 올리는 일을 그쪽에 맡겨 뒤에서 하게 한다(Slack에 3초 안에 답하려는 것이다).
    """
    kind = payload.get("type")
    user = (payload.get("user") or {}).get("id", "")
    team = (payload.get("team") or {}).get("id", "") or (payload.get("user") or {}).get("team_id", "")
    if kind == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        if action.get("action_id") != ACTION_OPEN_FACTS:
            return None  # 링크 버튼 등: 받았다는 답만 한다
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id", "")
        message = payload.get("message") or {}
        meta = {"channel": channel, "thread_ts": message.get("thread_ts") or message.get("ts", ""), "response_url": payload.get("response_url", "")}
        log_id = _log_id(str(action.get("value", "")))
        try:
            if log_id is None:
                raise SlackError("사건 번호를 읽지 못했습니다.")
            if not _team_allowed(team) or not _channel_allowed(channel):
                raise SlackError(_channel_refusal(channel))
            if not _user_can_edit(user):
                raise SlackError(_user_refusal(user))
            open_facts_modal(payload.get("trigger_id", ""), log_id, meta)
        except (SlackError, ValueError) as exc:
            logger.warning("Slack 입력 양식을 열지 못했습니다: %s", exc)
            if meta["response_url"]:
                try:
                    _respond(meta["response_url"], {"response_type": "ephemeral", "replace_original": False, "text": f"입력 양식을 열지 못했습니다. {exc}"})
                except SlackError:
                    pass
        return None

    if kind == "view_submission" and (payload.get("view") or {}).get("callback_id") == CALLBACK_FACTS:
        view = payload["view"]
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
            log_id = int(meta["log_id"])
        except (ValueError, KeyError, TypeError):
            return {"response_action": "update", "view": _result_view("양식의 사건 번호를 읽지 못했습니다. 명령을 다시 실행해 주십시오.")}
        if not _team_allowed(team) or not _user_can_edit(user):
            return {"response_action": "update", "view": _result_view(_user_refusal(user))}
        if meta.get("channel") and not _channel_allowed(meta["channel"]):
            return {"response_action": "update", "view": _result_view(_channel_refusal(meta["channel"]))}
        facts, errors = facts_from_view(view, meta)
        if errors:
            return {"response_action": "errors", "errors": errors}
        try:
            before = case_guidance.get_facts(log_id)
            saved = case_guidance.save_facts(log_id, facts, actor=f"slack:{user}", source="slack")
            hints = case_guidance.derive_hints(log_id)
        except ValueError as exc:
            return {"response_action": "errors", "errors": {"gain_amount": str(exc)}}
        changes = {key: saved.get(key) for key in set(before) | set(saved) if before.get(key) != saved.get(key)}
        who = f"<@{user}>님이" if user else "수사관이"
        note = f"{who} 사건 #{log_id}의 사건 사실을 저장했습니다. " + (f"바뀐 항목 — {_changes_text(changes, hints)}" if changes else "바뀐 항목은 없습니다.")
        if defer is not None:
            defer(announce_saved, meta, user, log_id, note)
        else:
            announce_saved(meta, user, log_id, note)
        return None  # 저장은 끝났다. 양식을 닫는다
    return None


# ---------------------------------------------------------------- 말로 묻기(멘션)


def _first_time(event_id: str) -> bool:
    """같은 알림(event)을 두 번 처리하지 않게 한다. Slack은 답이 늦거나 전달에 실패하면 같은 것을 다시 보낸다."""
    if not event_id:
        return True
    with _seen_lock:
        if event_id in _seen_events:
            return False
        _seen_events[event_id] = time.time()
        while len(_seen_events) > 500:
            _seen_events.popitem(last=False)
    return True


def handle_event(body: dict) -> tuple[dict, Optional[dict]]:
    """Events API 요청을 처리한다. (바로 돌려줄 답, 뒤에서 답할 멘션)을 돌려준다."""
    if body.get("type") == "url_verification":
        return {"challenge": str(body.get("challenge", ""))}, None
    event = body.get("event") or {}
    if body.get("type") != "event_callback" or event.get("type") != "app_mention":
        return {}, None
    if event.get("bot_id") or event.get("subtype"):
        return {}, None
    if not _team_allowed(str(body.get("team_id", ""))) or not _channel_allowed(event.get("channel", "")):
        return {}, None
    if not _first_time(str(body.get("event_id", ""))):
        return {}, None
    return {}, event


def answer_mention(event: dict) -> None:
    """봇을 부른 글에 도우미의 답을 스레드로 단다. 도우미는 저장된 조문과 수사 데이터를 읽기만 한다."""
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    question = _MENTION.sub(" ", event.get("text", "")).strip()
    blocks = None
    if not question:
        reply, blocks = "VoiceGuard 수사 지원 사용법", help_blocks()
    else:
        from backend.app.services import investigator_assistant

        if not investigator_assistant.is_available():
            reply = "Claude API 키가 설정되지 않아 말로 묻기는 쓸 수 없습니다. `/vg 사건 51`, `/vg 법령 범죄인 인도 청구` 같은 명령은 쓸 수 있습니다."
        else:
            try:
                result = investigator_assistant.answer(question, None, f"slack:{channel}:{thread_ts}")
                reply = to_mrkdwn(result.get("answer") or "답을 만들지 못했습니다.")
                reply += "\n\n_저장된 조문과 수사 데이터를 찾아 답했습니다. 참고 자료이며 법률 자문이 아닙니다. 사건 사실은 `/vg 입력 51`처럼 양식으로 넣습니다._"
            except Exception:  # 도우미 오류로 Slack 답이 끊기지 않게 한다
                logger.exception("Slack 질문에 답하지 못했습니다.")
                reply = "답을 만드는 중 오류가 났습니다. 잠시 뒤 다시 물어 주십시오."
    try:
        post_message(channel, reply, blocks, thread_ts)
    except SlackError as exc:
        logger.warning("Slack에 답을 올리지 못했습니다: %s", exc)


def _run_mention(event: dict) -> None:
    global _waiting_mentions
    try:
        answer_mention(event)
    finally:
        with _waiting_lock:
            _waiting_mentions -= 1


def submit_mention(event: dict) -> bool:
    """멘션에 뒤에서 답한다. 기다리는 질문이 너무 많으면 받지 않고 바로 그렇게 알린다(False)."""
    global _waiting_mentions
    with _waiting_lock:
        busy = _waiting_mentions >= MAX_WAITING_MENTIONS
        if not busy:
            _waiting_mentions += 1
    if busy:
        _job_pool.submit(_reply_busy, event)
        return False
    _mention_pool.submit(_run_mention, event)
    return True


def _reply_busy(event: dict) -> None:
    try:
        post_message(event.get("channel", ""), "지금 질문이 많아 답하지 못했습니다. 잠시 뒤 다시 물어 주십시오.", None, event.get("thread_ts") or event.get("ts", ""))
    except SlackError as exc:
        logger.warning("Slack에 답을 올리지 못했습니다: %s", exc)


def run_in_background(function: Callable, *args) -> None:
    """채널에 글을 올리는 일처럼 Slack에 답한 뒤에 해도 되는 일을 뒤에서 돌린다."""
    _job_pool.submit(_safely, function, *args)


def _safely(function: Callable, *args) -> None:
    try:
        function(*args)
    except Exception:
        logger.exception("Slack 뒷일을 마치지 못했습니다.")


# ---------------------------------------------------------------- 기한 알림


def _ensure_notice_table() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slack_notices (
                key TEXT PRIMARY KEY,
                via TEXT NOT NULL DEFAULT '',
                sent_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            )
            """
        )


def _stage(days_left: int) -> str:
    return "D-1" if days_left == 1 else "D-day" if days_left == 0 else "overdue"


def deadline_notice_text(item: dict) -> str:
    return (f":alarm_clock: {deadline_line(item)}\n{esc(item.get('label', ''))}\n"
            f"`/vg 사건 {item['log_id']}`로 절차 안내를 볼 수 있습니다.")


def deadline_route() -> str:
    """기한 알림을 보낼 길: slack(봇이 직접), n8n(웹훅, SLACK_DEADLINE_VIA_N8N=1일 때만), 빈 글(보낼 곳 없음).

    n8n 길은 워크플로가 guidance.deadline 이벤트를 처리하도록 고쳐 둔 경우에만 켠다(n8n/voiceguard_slack_alert.json).
    """
    if bot_token() and deadline_channel():
        return "slack"
    via_n8n = os.environ.get("SLACK_DEADLINE_VIA_N8N", "").strip().lower() in ("1", "true", "on", "yes")
    return "n8n" if via_n8n and webhook_emitter.is_configured() else ""


def notify_deadlines(today: Optional[date] = None) -> dict:
    """기한이 내일·오늘이거나 지난 사건을 알린다. 같은 기한의 같은 단계(전날, 당일, 지남)는 한 번만 알린다.

    전달이 확인된 것만 '보냄'으로 기록한다. 실패한 것은 다음 확인 때 다시 보낸다.
    """
    route = deadline_route()
    items = case_guidance.upcoming_deadlines(within_days=1, today=today or kst_today(), overdue_days=OVERDUE_DAYS)
    if not route:
        return {"via": "", "sent": 0, "waiting": len(items)}
    _ensure_notice_table()
    sent = failed = 0
    done: set[str] = set()
    for item in items:
        # 규칙 이름이 아니라 기한의 종류(어느 날짜에서 센 것인지)로 구분한다. 사실을 고쳐 규칙이 바뀌어도 같은 기한을 다시 알리지 않는다.
        key = f"{item['log_id']}:{item.get('from_fact', '')}:{item['due']}:{_stage(int(item['days_left']))}"
        if key in done:
            continue
        with get_connection() as connection:
            if connection.execute("SELECT 1 FROM slack_notices WHERE key = ?", (key,)).fetchone():
                done.add(key)
                continue
        text = deadline_notice_text(item)
        try:
            if route == "slack":
                post_message(deadline_channel(), text)
                delivered = True
            else:
                delivered = webhook_emitter.send_now("guidance.deadline", {
                    "log_id": item["log_id"], "title": item["title"], "due": item["due"], "days_left": item["days_left"],
                    "slack_text": text, "dashboard_url": public_case_url(item["log_id"]),
                })
        except SlackError as exc:
            logger.warning("기한 알림을 보내지 못했습니다(사건 %s): %s", item["log_id"], exc)
            delivered = False
        if not delivered:
            failed += 1
            continue
        done.add(key)
        try:
            with get_connection() as connection:
                connection.execute("INSERT OR IGNORE INTO slack_notices (key, via) VALUES (?, ?)", (key, route))
        except sqlite3.Error:
            logger.exception("기한 알림 기록을 남기지 못했습니다.")
        sent += 1
    return {"via": route, "sent": sent, "waiting": failed}


def _in_send_hours(now: Optional[datetime] = None) -> bool:
    """알림을 보내도 되는 시간인지(한국 시간, 기본 8시~20시)."""
    raw = os.environ.get("SLACK_DEADLINE_HOURS", "8-20")
    try:
        start, end = (int(part) for part in raw.split("-", 1))
    except ValueError:
        start, end = 8, 20
    hour = (now or datetime.now(_KST)).astimezone(_KST).hour
    return start <= hour < end


async def deadline_loop(interval_sec: int = 900) -> None:
    """서버가 떠 있는 동안 15분마다 기한을 확인해 알린다."""
    await asyncio.sleep(60)
    while True:
        try:
            if deadline_route() and _in_send_hours():
                result = await asyncio.to_thread(notify_deadlines)
                if result.get("sent"):
                    logger.info("기한 알림 %d건을 보냈습니다(%s).", result["sent"], result["via"])
        except Exception:
            logger.exception("기한 알림을 확인하지 못했습니다.")
        await asyncio.sleep(interval_sec)
