"""절차 안내: 사건 사실로 담당 기관·통보 의무·기한과 해외 연계 절차를 규칙표에 따라 계산한다.

- 판정은 규칙표(backend/app/legal_data/rules/*.json)가 한다. 규칙의 글은 받아 둔 조문에서만 가져왔고,
  결과마다 근거 조문(legal_kb)을 붙인다. AI는 여기서 아무것도 판단하지 않는다.
- 사건 사실은 두 가지다. 통화 기록에서 자동으로 짐작한 값(hints)과 수사관이 확인해 넣은 값(facts).
  확인한 값이 있으면 그것을 쓰고, 짐작한 값으로 나온 안내에는 '자동 추정'이라고 표시한다.
- 이 안내는 "조문상 이렇게 보인다"는 참고 자료다. 실제 통보·청구는 소속 기관의 지침과 담당 부서 확인을 거친다.

docs/investigation-guidance-design.md 참고.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Any, Optional

from backend.app import state_repository
from backend.app.database import get_connection
from backend.app.repository import get_call_log, list_call_messages
from backend.app.services import legal_kb


logger = logging.getLogger(__name__)

NOTICE = (
    "이 안내는 받아 둔 조문과 규칙표로 계산한 참고 자료이며 법률 자문이 아닙니다. "
    "실제 통보·이첩·청구는 소속 기관의 지침과 담당 부서의 확인을 거쳐야 합니다."
)
DEADLINE_NOTE = (
    "기한은 시작일을 빼고 다음 날부터 센 참고값입니다(형사소송법 제66조 제1항의 계산 방식). "
    "중수청법 시행령의 3일·7일에도 같은 방식이 적용되는지는 받아 둔 조문에 없어, 첫날부터 세면 하루 이릅니다. "
    "말일이 토요일·공휴일인 경우와 기관별 처리 기준은 반영하지 않았습니다."
)

# 죄명 후보 코드 → 화면 이름. 규칙표의 charges 조건이 이 코드를 쓴다.
CHARGES: dict[str, str] = {
    "telefraud": "전기통신금융사기(통신사기피해환급법 제2조 제2호·제15조의2)",
    "fraud": "사기·공갈(형법 제347조·제347조의2·제350조·제350조의2와 그 상습범)",
    "embezzlement": "횡령·배임(형법 제355조·제356조)",
    "eft": "전자금융거래법 위반(접근매체 양도·대여 등)",
    "org_crime": "범죄단체 조직·가입(형법 제114조)",
}
BOOL_FACTS: dict[str, str] = {
    "cio_crime": "공수처법의 고위공직자범죄등에 해당",
    "complaint_unclear": "고소·고발·진정·신고 등의 내용이 불분명하거나 구체적 사실이 적시되지 않음",
    "already_notified": "같은 범죄사실을 중대범죄수사청에 이미 통보함",
    "no_prosecution": "공소권 없음이 명백함",
    "transfer_requested": "중대범죄수사청의 이첩 요청을 받음",
    "overseas_link": "해외와 이어진 사건",
    "suspect_abroad": "피의자가 국외에 있음",
    "passport_ground": "여권법 제12조 제1항 제1호 사유에 해당",
    "coercion_signs": "현지 체포·구금 또는 범죄 피해(감금·강요) 정황",
}
DATE_FACTS: dict[str, str] = {
    "recognized_on": "인지한 날",
    "written_notice_on": "중대범죄수사청이 서면 통보를 받은 날",
    "supplement_requested_on": "보완수사요구를 받은 날",
}
FACT_LABELS: dict[str, str] = {"charges": "죄명 후보", "gain_amount": "이득액(원)", **BOOL_FACTS, **DATE_FACTS}
# 입력 칸 아래에 붙이는 설명. 이름만으로는 법이 정한 요건을 알기 어려운 항목에만 둔다(글은 조문에서 가져왔다).
FACT_HELP: dict[str, str] = {
    "charges": (
        "전기통신금융사기는 전기통신을 이용해 타인을 기망·공갈하여 자금을 송금·이체하게 하는 등의 행위입니다. "
        "재화의 공급이나 용역의 제공 등을 가장한 행위는 제외하고, 대출의 제공·알선·중개를 가장한 행위는 포함합니다"
        "(통신사기피해환급법 제2조 제2호)."
    ),
    "gain_amount": (
        "그 범죄행위로 취득하거나 제3자에게 취득하게 한 재물 또는 재산상 이익의 가액입니다(특정경제범죄법 제3조 제1항). "
        "사기·공갈·횡령·배임은 5억 원 이상인지에 따라 통보 여부가 달라집니다."
    ),
    "already_notified": "중수청법 제43조 제2항에 따른 통보를 이미 한 경우입니다. 다른 기관에 한 통보는 해당하지 않습니다.",
    "passport_ground": (
        "장기 2년 이상의 형에 해당하는 죄로 기소됐거나, 장기 3년 이상의 형에 해당하는 죄로 "
        "기소중지·수사중지(피의자중지)되거나 체포영장·구속영장이 발부된 경우입니다."
    ),
    "recognized_on": "고소·고발·진정·수사의뢰는 접수일, 그 밖에는 입건 전 조사를 위해 사건을 수리·등록한 날과 범죄인지서를 작성한 날 중 빠른 날입니다(시행령 제18조 제2항).",
    "written_notice_on": "중대범죄수사청의 회신 기한 7일을 이 날부터 셉니다(시행령 제19조 제2항).",
    "supplement_requested_on": "보완수사 결과 통보 기한 1개월을 이 날부터 셉니다(형사소송법 제197조의2 제4항).",
    "cio_crime": "공수처법 제2조 제5호의 고위공직자범죄등(고위공직자범죄와 관련범죄)입니다.",
}
MAX_AMOUNT = 10**15

# 통화 기록의 사기 유형 → 죄명 후보(자동 추정). 전기통신을 이용해 속이거나 겁을 줘 돈을 보내게 하는 유형이다.
SCAM_TYPE_CHARGES: dict[str, list[str]] = {
    "agency": ["telefraud", "fraud"],
    "loan": ["telefraud", "fraud"],
    "kidnap": ["telefraud", "fraud"],
    "acquaintance": ["telefraud", "fraud"],
    "smishing": ["telefraud", "fraud"],
    "other": ["fraud"],
}
# 해외 연계 신호. 나라·지명·국가번호가 있으면 '해외와 이어진 사건'으로 짐작하고, 약한 신호는 표시만 한다.
OVERSEAS_STRONG = (
    "캄보디아", "프놈펜", "시아누크빌", "바벳", "포이펫", "보코산", "태국", "방콕", "필리핀", "마닐라", "베트남",
    "라오스", "미얀마", "해외 취업", "해외취업",
)
OVERSEAS_WEAK = ("텔레그램", "코인", "테더", "가상자산", "현지")
_PHONE_KH = re.compile(r"(?<!\d)(?:\+|00)?855[\s-]?\d{2}")
_SNIPPET = 60

_packs_lock = threading.Lock()
_packs: Optional[list[dict]] = None


# ---------------------------------------------------------------- 사건 사실 저장


def _ensure_tables() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS case_facts (
                log_id INTEGER PRIMARY KEY,
                facts TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            )
            """
        )


def _parse_date(value: Any) -> Optional[str]:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def clean_facts(raw: dict) -> dict:
    """수사관이 넣은 값을 검사해 아는 항목만 남긴다. 값이 None이면 그 항목을 지운다는 뜻으로 None을 남긴다.

    모양이 틀린 값은 ValueError로 알린다(어느 항목이 틀렸는지 문장에 담는다).
    """
    cleaned: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in FACT_LABELS:
            raise ValueError(f"모르는 항목입니다: {key}")
        if value is None or value == "":
            cleaned[key] = None
        elif key == "charges":
            if not isinstance(value, list) or any(code not in CHARGES for code in value):
                raise ValueError("죄명 후보는 " + ", ".join(CHARGES) + " 가운데서 고릅니다.")
            cleaned[key] = list(dict.fromkeys(value))
        elif key == "gain_amount":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_AMOUNT:
                raise ValueError("이득액은 0 이상의 숫자(원)로 넣습니다.")
            cleaned[key] = int(value)
        elif key in BOOL_FACTS:
            if not isinstance(value, bool):
                raise ValueError(f"'{FACT_LABELS[key]}'은 예(true) 또는 아니오(false)로 넣습니다.")
            cleaned[key] = value
        else:
            parsed = _parse_date(value)
            if parsed is None:
                raise ValueError(f"'{FACT_LABELS[key]}'은 YYYY-MM-DD 모양의 날짜로 넣습니다.")
            cleaned[key] = parsed
    return cleaned


def get_facts(log_id: int) -> dict:
    """수사관이 확인해 넣은 사건 사실. 없으면 빈 dict.

    읽기만 한다(표도 만들지 않는다). 도우미의 읽기 전용 도구가 이 길을 쓰기 때문이다.
    표는 사실을 처음 저장할 때 만든다.
    """
    try:
        with get_connection() as connection:
            row = connection.execute("SELECT facts FROM case_facts WHERE log_id = ?", (log_id,)).fetchone()
    except sqlite3.OperationalError:  # 아직 아무도 사실을 저장한 적이 없어 표가 없다
        return {}
    if row is None:
        return {}
    try:
        stored = json.loads(row["facts"] or "{}")
    except ValueError:
        return {}
    return {key: value for key, value in stored.items() if key in FACT_LABELS and value is not None}


def save_facts(log_id: int, raw: dict) -> dict:
    """사건 사실을 고친다. 넣은 항목만 바뀌고, None을 넣은 항목은 지워진다. 저장된 전체를 돌려준다."""
    get_call_log(log_id)  # 없는 통화면 ValueError
    changes = clean_facts(raw)
    _ensure_tables()
    facts = get_facts(log_id)
    for key, value in changes.items():
        if value is None:
            facts.pop(key, None)
        else:
            facts[key] = value
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO case_facts (log_id, facts, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
            ON CONFLICT(log_id) DO UPDATE SET facts = excluded.facts, updated_at = excluded.updated_at
            """,
            (log_id, json.dumps(facts, ensure_ascii=False)),
        )
    return facts


# ---------------------------------------------------------------- 통화 기록에서 짐작하기


_UNIT = {"천": 1000, "백": 100, "십": 10}


def _small_number(text: str) -> Optional[int]:
    """'8천6백', '3000', '5' 같은 만 단위 아래 숫자를 읽는다. 못 읽으면 None."""
    if not text:
        return 0
    if re.fullmatch(r"\d+", text):
        return int(text)
    total, digits = 0, ""
    for char in text:
        if char.isdigit():
            digits += char
        elif char in _UNIT:
            total += (int(digits) if digits else 1) * _UNIT[char]
            digits = ""
        else:
            return None
    return total + (int(digits) if digits else 0)


def parse_krw(text: str) -> Optional[int]:
    """'8천 6백만 원', '5억', '1억 2천만원', '3,000,000원'을 원 단위 숫자로 바꾼다. 단위가 없는 숫자는 읽지 않는다."""
    compact = re.sub(r"[\s,]", "", text or "")
    matched = re.search(r"((?:[\d천백십]+억)?(?:[\d천백십]+만)?[\d천백십]*)원?", compact)
    body = matched.group(1) if matched else ""
    if not body or not any(unit in body for unit in "억만") and not re.fullmatch(r"\d{5,}", body):
        return None
    eok, rest = body.split("억", 1) if "억" in body else ("", body)
    man, tail = rest.split("만", 1) if "만" in rest else ("", rest)
    parts = [_small_number(eok), _small_number(man), _small_number(tail)]
    if any(part is None for part in parts):
        return None
    value = parts[0] * 100_000_000 + parts[1] * 10_000 + parts[2]
    return value if 0 < value <= MAX_AMOUNT else None


def derive_hints(log_id: int) -> dict:
    """통화 기록에서 사건 사실을 짐작한다. 확인된 사실이 아니므로 화면에 '자동 추정'으로 표시한다."""
    hints: dict[str, Any] = {"signals": []}
    state = state_repository.get_latest_state(log_id) or {}
    charges = SCAM_TYPE_CHARGES.get(str(state.get("scam_type") or ""), [])
    if charges:
        hints["charges"] = list(charges)

    entities = state_repository.list_entities(log_id)
    amounts = [value for value in (parse_krw(str(e.get("value", ""))) for e in entities if e.get("entity_type") == "amount") if value]
    if amounts:
        hints["amount_mentioned"] = max(amounts)  # 통화에서 요구한 금액이다. 이득액이 아니므로 규칙에는 쓰지 않는다.

    seen: set[str] = set()
    strong = False
    for message in list_call_messages(log_id):
        content = message.content or ""
        found = [word for word in OVERSEAS_STRONG if word in content]
        if _PHONE_KH.search(content):
            found.append("국가번호 855")
        weak = [word for word in OVERSEAS_WEAK if word in content]
        for word in found + weak:
            if word in seen:
                continue
            seen.add(word)
            position = max(content.find(word.split()[0]) if word != "국가번호 855" else 0, 0)
            start = max(position - _SNIPPET // 2, 0)
            hints["signals"].append(
                {"word": word, "strong": word in found, "turn_index": message.turn_index, "text": content[start:start + _SNIPPET]}
            )
        strong = strong or bool(found)
    for entity in entities:
        if entity.get("entity_type") == "phone" and re.match(r"^(?:00)?855\d{6,}", re.sub(r"\D", "", str(entity.get("value", "")))):
            if "국가번호 855" not in seen:
                seen.add("국가번호 855")
                hints["signals"].append({"word": "국가번호 855", "strong": True, "turn_index": entity.get("turn_index"), "text": str(entity.get("value", ""))})
            strong = True
    if strong:
        hints["overseas_link"] = True
    return hints


# ---------------------------------------------------------------- 규칙표


def _load_packs() -> list[dict]:
    packs = []
    directory = legal_kb.data_dir() / "rules"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
            pack["rules"] = list(pack["rules"])
        except Exception as exc:
            logger.warning("규칙표 %s을 읽지 못해 건너뜁니다: %s", path.name, exc)
            continue
        packs.append(pack)
    # 수사 배분을 먼저 돌려야 해외 연계 규칙이 그 결과(notify)를 볼 수 있다.
    packs.sort(key=lambda pack: 0 if pack.get("pack") == "allocation" else 1)
    return packs


def packs() -> list[dict]:
    global _packs
    with _packs_lock:
        if _packs is None:
            _packs = _load_packs()
        return _packs


def reload() -> None:
    global _packs
    with _packs_lock:
        _packs = None
    legal_kb.reload()


def _holds(condition: dict, facts: dict) -> bool:
    if "any" in condition:
        return any(_holds(item, facts) for item in condition["any"])
    if "all" in condition:
        return all(_holds(item, facts) for item in condition["all"])
    value, op, target = facts.get(condition["fact"]), condition["op"], condition.get("value")
    if op == "is":
        return value is not None and value == target
    if op == "is_not":
        return value != target
    if op == "known":
        return value is not None and value != []
    if op == "unknown":
        return value is None or value == []
    if op == "has_any":
        return bool(value) and any(item in value for item in target)
    if op == "has_none":
        return not value or not any(item in value for item in target)
    if op == "gte":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= target
    if op == "lt":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value < target
    raise ValueError(f"모르는 조건입니다: {op}")


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _deadline(spec: dict, facts: dict, today: date) -> dict:
    result = {"label": spec["label"], "from_fact": spec["from"], "from_label": FACT_LABELS.get(spec["from"], spec["from"]), "due": None, "days_left": None}
    start = _parse_date(facts.get(spec["from"]))
    if start is None:
        return result
    begin = date.fromisoformat(start)
    due = _add_months(begin, int(spec["months"])) if "months" in spec else begin + timedelta(days=int(spec["days"]))
    result.update({"from": start, "due": due.isoformat(), "days_left": (due - today).days})
    return result


def evaluate(facts: dict, today: Optional[date] = None) -> dict:
    """사건 사실에 규칙표를 적용한다. facts는 죄명 후보·이득액·예/아니오·날짜가 든 dict다."""
    today = today or date.today()
    working = dict(facts)
    items: list[dict] = []
    for pack in packs():
        for rule in pack["rules"]:
            if not all(_holds(condition, working) for condition in rule.get("when", [])):
                continue
            working.update(rule.get("sets", {}))
            if rule.get("silent"):  # 값만 정하고 화면에는 내지 않는 규칙
                continue
            basis, missing = [], []
            for law, article in rule.get("basis", []):
                provision = legal_kb.get(law, article, full=False)
                if provision is None:
                    missing.append(f"{law} {article}")
                    continue
                basis.append({key: provision[key] for key in ("ref", "heading", "law", "article", "url", "effective_date")})
            if missing:
                logger.warning("규칙 %s의 근거 조문을 지식베이스에서 찾지 못했습니다: %s", rule["id"], ", ".join(missing))
            item = {
                "id": rule["id"],
                "pack": pack.get("pack", ""),
                "group": rule.get("group", ""),
                "title": rule["title"],
                "level": rule.get("level", "info"),
                "text": rule["text"],
                "basis": basis,
                "basis_missing": missing,
                "needs": [{"fact": name, "label": FACT_LABELS.get(name, name)} for name in rule.get("needs", [])],
            }
            if "deadline" in rule:
                item["deadline"] = _deadline(rule["deadline"], working, today)
            items.append(item)
    flags = {
        key: working.get(key)
        for key in ("major", "notify_candidate", "excluded", "light_fraud", "notify", "notify_uncertain", "notified_before", "overseas")
    }
    return {"items": items, "flags": flags}


def _headline(facts: dict, flags: dict, items: list[dict]) -> str:
    if flags.get("notify") and flags.get("notify_uncertain"):
        return "중대범죄수사청 통보 대상으로 볼 수 있습니다. 조문 해석이 갈리는 부분이 있어 통보하는 쪽으로 안내합니다."
    if flags.get("notify"):
        return "중대범죄수사청 통보 대상으로 보입니다."
    if flags.get("notified_before"):
        return "중대범죄수사청에 이미 통보한 사건으로 입력되어 있습니다. 서면 통보와 회신 기한을 확인하십시오."
    if flags.get("excluded"):
        return "중대범죄수사청 통보 대상에서 제외되는 사유가 있습니다."
    if flags.get("light_fraud"):
        return "중대범죄수사청 통보 대상에서 제외되는 것으로 보입니다."
    if any(item["level"] == "need" and item["pack"] == "allocation" for item in items):
        return "사실을 더 넣어야 통보 여부를 볼 수 있습니다."
    if not facts.get("charges"):
        return "죄명 후보가 없어 통보 여부를 보지 못했습니다. 죄명 후보를 골라 주십시오."
    return "이 죄명만으로는 중대범죄수사청 통보 대상으로 보이지 않습니다."


def guidance(log_id: int, today: Optional[date] = None) -> dict:
    """통화 하나의 절차 안내. 없는 통화면 ValueError."""
    get_call_log(log_id)
    confirmed = get_facts(log_id)
    hints = derive_hints(log_id)
    effective = {**{key: value for key, value in hints.items() if key in FACT_LABELS}, **confirmed}
    from_hints = sorted(key for key in effective if key not in confirmed)
    result = evaluate(effective, today)
    status = legal_kb.status()
    headline = _headline(effective, result["flags"], result["items"])
    if "charges" in from_hints:
        # 죄명은 통화의 사기 유형에서 짐작했을 뿐이다(예: 전기통신금융사기의 정의에 맞는지는 사람이 봐야 한다).
        headline += " 죄명 후보는 통화 기록에서 짐작한 값이라 확인이 필요합니다."
    return {
        "log_id": log_id,
        "headline": headline,
        "facts": confirmed,
        "hints": hints,
        "effective": effective,
        "from_hints": [{"fact": key, "label": FACT_LABELS[key]} for key in from_hints],
        "flags": result["flags"],
        "items": result["items"],
        "deadlines": [
            {**item["deadline"], "rule": item["id"], "title": item["title"]} for item in result["items"] if item.get("deadline")
        ],
        "options": {
            "charges": [{"code": code, "label": label} for code, label in CHARGES.items()],
            "bools": [{"fact": key, "label": label, "help": FACT_HELP.get(key, "")} for key, label in BOOL_FACTS.items()],
            "dates": [{"fact": key, "label": label, "help": FACT_HELP.get(key, "")} for key, label in DATE_FACTS.items()],
            "help": {key: FACT_HELP[key] for key in ("charges", "gain_amount")},
        },
        "legal_data": {"fetched_from": status["fetched_from"], "latest_fetch": status["latest_fetch"], "law_count": status["law_count"]},
        "pending": "상대국(캄보디아 등)의 법령과 조약이 정한 요건·기한은 아직 지식베이스에 없습니다.",
        "notice": NOTICE,
        "deadline_note": DEADLINE_NOTE,
    }


def rule_basis_report() -> list[dict]:
    """규칙마다 근거 조문이 지식베이스에 있는지 본다(시험과 점검용)."""
    report = []
    for pack in packs():
        for rule in pack["rules"]:
            if rule.get("silent"):
                continue
            missing = [f"{law} {article}" for law, article in rule.get("basis", []) if legal_kb.get(law, article) is None]
            report.append({"pack": pack.get("pack", ""), "id": rule["id"], "basis": len(rule.get("basis", [])), "missing": missing})
    return report
