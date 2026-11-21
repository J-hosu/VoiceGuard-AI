"""법령 지식베이스: 공식 출처에서 미리 받아 둔 조문을 읽어 찾고 돌려준다.

- 조문은 backend/app/fetch_legal.py가 법제처 Open API(또는 국가법령정보센터 화면)에서 받은 것만 있다
  (backend/app/legal_data/kr/*.json: 법령과 조약). 이 모듈은 조문을 만들거나 고치지 않는다. 받은 글, 받은 주소, 받은 시각을 그대로 돌려준다.
- 변경 확인 결과(backend/app/legal_data/updates.json)가 있으면, 저장된 판이 바뀌었는지와 시행 예정인 개정을 조문에 함께 붙인다.
- 수사 지원 도우미와 절차 안내(case_guidance)는 법령에 관한 내용을 여기서 찾은 조문으로만 답한다.
- 검색은 글자가 들어 있는지로 점수를 매기는 단순한 방식이다. 조문이 수천 개 수준이라 메모리에서 바로 훑는다.
  한국어는 조사가 붙어 낱말이 달라지므로 흔한 조사를 떼어 낸 꼴도 함께 찾고, 수사관이 쓰는 줄임말은 법령의 말로 바꿔 찾는다.

LEGAL_DATA_DIR 환경변수로 자료 폴더를 바꿀 수 있다(시험용).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.app.paths import APP_DIR


logger = logging.getLogger(__name__)

DEFAULT_DIR = APP_DIR / "legal_data"
MAX_LIMIT = 20
SNIPPET_CHARS = 320

# 수사관이 쓰는 말 → 법령에 쓰인 말. 찾는 낱말을 넓히는 데만 쓴다.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "보이스피싱": ("전기통신금융사기",),
    "피싱": ("전기통신금융사기",),
    "중수청": ("중대범죄수사청",),
    "공수처": ("고위공직자범죄수사처",),
    "대포통장": ("접근매체",),
    "송환": ("인도", "호송"),
    "넘겨받": ("인도",),
    "인터폴": ("국제형사경찰기구",),
    "무효": ("무효처분", "효력"),
    "기한": ("이내", "기간"),
    "언제까지": ("이내", "기간"),
    "공조": ("공조요청",),
    "범죄단체": ("단체 또는 집단",),
    "시효": ("공소시효", "시효"),
    "환수": ("몰수", "추징"),
    "처벌": ("벌칙", "징역"),
    "형량": ("벌칙", "징역"),
}
# 낱말 끝에서 떼어 볼 조사와 어미(긴 것부터). 뗀 뒤 두 글자 이상 남을 때만 뗀다.
_SUFFIXES = (
    "으로써", "해야", "하면", "하는", "하여", "하고", "한다", "했을", "되는", "되면", "받으면", "받은",
    "으로", "에서", "에게", "까지", "부터", "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만",
)
_STOP = {
    "무엇", "어떻게", "어디", "누가", "누구", "언제", "경우", "관련", "대해", "대한", "알려줘", "하나", "해야", "되나",
    "하는지", "인지", "절차", "방법", "조문", "법령", "근거", "하는", "있는", "하면", "무슨", "어떤", "누구에게",
}
# 같은 점수면 법률을 시행령·부령보다 앞에 둔다.
_KIND_PRIOR = {"법률": 1.0, "조약": 0.9, "대통령령": 0.5}
_SPLIT = re.compile(r"[\s,.;:!?()\[\]{}<>「」『』“”\"'·ㆍ/|]+")
_PENDING_NOTE = re.compile(r"^\[시행일(?::\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2}))?[^\]]*\]")
_ITEM_LINE = re.compile(r"^[가-하]\.\s")  # '가. …', '나. …'
_NOTE_DATE = re.compile(r"(\d{4})(?:년\s*|\.\s*)(\d{1,2})(?:월\s*|\.\s*)(\d{1,2})(?:일|\b)")  # '2029년 8월 3일', '2027.8.5'
_KST = timezone(timedelta(hours=9))


def _today() -> str:
    """오늘 날짜(한국 시간). 법령의 시행일은 한국 날짜로 따진다."""
    return datetime.now(_KST).date().isoformat()
_ARTICLE_REF = re.compile(r"제?\s*(\d+)\s*조(?:\s*의\s*(\d+))?")

_lock = threading.Lock()
_cache: Optional[dict] = None


def data_dir() -> Path:
    return Path(os.environ.get("LEGAL_DATA_DIR") or DEFAULT_DIR)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _load() -> dict:
    """자료 폴더의 법령 파일을 읽어 색인을 만든다. 파일이 없거나 깨져 있으면 그 법령만 건너뛴다."""
    laws: dict[str, dict] = {}
    names: dict[str, str] = {}
    provisions: list[dict] = []
    directory = data_dir() / "kr"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            law_id = str(record["id"])
            items = list(record["provisions"])
        except Exception as exc:
            logger.warning("법령 파일 %s을 읽지 못해 건너뜁니다: %s", path.name, exc)
            continue
        aliases = [str(alias) for alias in record.get("aliases") or []]
        # 화면에 쓰는 짧은 이름: 이름이 길면 목록의 첫 줄임말을 쓴다.
        short = aliases[0] if aliases and len(str(record["name"])) > 12 else str(record["name"])
        laws[law_id] = {
            "id": law_id,
            "name": record["name"],
            "short": short,
            "aliases": aliases,
            "kind": record.get("kind", ""),
            "group": record.get("group", "common"),
            "jurisdiction": record.get("jurisdiction", "KR"),
            "effective_date": record.get("effective_date", ""),
            "lsi_seq": str(record.get("lsi_seq", "")),
            "promulgation": record.get("promulgation", ""),
            "source": record.get("source", ""),
            "source_url": record.get("source_url", ""),
            "fetched_at": record.get("fetched_at", ""),
            "provision_count": len(items),
            "note": _language_note(str(record.get("signature") or "")),
        }
        for label in [law_id, record["name"], *aliases]:
            names[_squash(label)] = law_id
        for item in items:
            provisions.append(
                {
                    "law": law_id,
                    "article": item["article"],
                    "no": int(item.get("no", 0)),
                    "branch": int(item.get("branch", 0)),
                    "heading": item.get("heading", ""),
                    "chapter": item.get("chapter", ""),
                    "text": item.get("text", ""),
                    "deleted": bool(item.get("deleted")),
                }
            )
    by_key = {(p["law"], p["article"]): p for p in provisions}
    updates = _load_updates(laws)
    # 긴 이름부터 맞춰 봐야 '형법'이 '특정경제범죄 가중처벌 등에 관한 법률'의 일부로 잘못 잡히지 않는다.
    ordered_names = sorted(names.items(), key=lambda pair: len(pair[0]), reverse=True)
    return {"laws": laws, "names": ordered_names, "provisions": provisions, "by_key": by_key, "updates": updates}


def _language_note(signature: str) -> str:
    """조약의 서명 문구에서 정본 언어와 해석이 갈릴 때 우선하는 본을 적은 문장만 그대로 골라낸다(없으면 빈 글)."""
    sentences = [part.strip() for part in re.split(r"(?<=다\.)\s*", signature) if part.strip()]
    return " ".join(part for part in sentences if "정본" in part or "우선" in part)


def _load_updates(laws: dict[str, dict]) -> dict:
    """변경 확인 결과를 읽는다. 파일이 없으면 빈 결과.

    확인한 때의 판(일련번호와 시행일)이 지금 저장된 판과 다르면 그 법령의 결과는 버린다. 조문을 다시 받은 뒤에
    낡은 '법령 변경됨'이나 '개정 예정' 표시가 남지 않게 하려는 것이다(다시 확인하기 전까지 '확인 전'이 된다).
    다만 다시 받은 뒤의 확인이 실패했다면 그 사실은 남긴다('확인 실패').

    법령마다 남기는 것: state, versions(시행 예정이던 판들. changed 등은 바로 앞 판과 견준 차이),
    outdated(저장된 판이 옛 판일 때 지금 판에서 달라진 조. 모르면 None), failed(마지막 확인이 실패했는지).
    """
    result = {"checked_at": "", "source": "", "entries": {}}
    path = data_dir() / "updates.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception as exc:
        logger.warning("변경 확인 파일을 읽지 못해 건너뜁니다: %s", exc)
        raw = {}
    result["checked_at"] = str(raw.get("checked_at") or "")
    result["source"] = str(raw.get("source") or "")
    for law_id, entry in (raw.get("laws") or {}).items():
        if law_id not in laws or not isinstance(entry, dict):
            continue
        stored = entry.get("stored") or {}
        if stored and (str(stored.get("mst")) != laws[law_id]["lsi_seq"] or str(stored.get("effective_date")) != laws[law_id]["effective_date"]):
            if entry.get("last_error"):
                result["entries"][law_id] = {"state": "unknown", "versions": [], "outdated": None, "failed": True}
            continue
        versions = [
            {"effective_date": str(version.get("effective_date") or ""), "promulgation": str(version.get("promulgation") or ""),
             "changed": [str(a) for a in version.get("changed") or []], "added": [str(a) for a in version.get("added") or []],
             "removed": [str(a) for a in version.get("removed") or []], "deferred": _deferred(version.get("deferred"))}
            for version in entry.get("upcoming") or []
        ]
        diff = entry.get("current_diff")
        outdated = [str(a) for a in (diff.get("changed") or []) + (diff.get("removed") or [])] if isinstance(diff, dict) else None
        result["entries"][law_id] = {"state": str(entry.get("state") or "unknown"), "versions": versions, "outdated": outdated, "failed": bool(entry.get("last_error"))}
    return result


def _deferred(value) -> Optional[dict]:
    """부칙에서 찾은 '그날 시행되는 조'({articles, note}). 모양이 다르면 None."""
    if not isinstance(value, dict) or not value.get("articles"):
        return None
    return {"articles": [str(article) for article in value["articles"]], "note": str(value.get("note") or "")}


def _upcoming_label(versions: list[dict]) -> str:
    """법령 목록에 보일 말. 조문 글이 바뀌는 판은 '개정 예정', 글은 같고 일부 규정만 그날 시행되는 판은 '일부 규정 시행 예정'."""
    amended = [v["effective_date"] for v in versions if v["changed"] or v["added"] or v["removed"]]
    partial = [v["effective_date"] for v in versions if not (v["changed"] or v["added"] or v["removed"])]
    parts = ([", ".join(amended) + " 개정 예정"] if amended else []) + ([", ".join(partial) + " 일부 규정 시행 예정"] if partial else [])
    return " · ".join(parts)


def _law_update(updates: dict, law_id: str, today: str) -> tuple[str, list[dict], Optional[set[str]]]:
    """(상태, 아직 시행 전인 판들, 저장된 글이 이미 옛 글이 된 조들).

    시행 예정일이 이미 지난 판이 있으면 저장된 판은 옛 판이므로 changed로 본다. 셋째 값은 상태가 changed일 때만
    뜻이 있다: 지금 판과 달라진 조의 묶음이고, 어느 조가 달라졌는지 모르면 None이다.
    """
    entry = updates["entries"].get(law_id)
    if not entry:
        return "", [], set()
    future = [version for version in entry["versions"] if version["effective_date"] > today]
    passed = [version for version in entry["versions"] if version["effective_date"] <= today]
    state = entry["state"]
    if state == "changed":  # 확인한 때 이미 옛 판이었다: 그때 견준 차이가 있어야 조 단위로 말할 수 있다
        outdated = set(entry["outdated"]) if entry["outdated"] is not None else None
    else:
        outdated = set()
    if passed:
        if state == "current":
            state = "changed"
        if outdated is not None:
            for version in passed:
                outdated.update(version["changed"])
                outdated.update(version["removed"])
    return state, future, outdated


def _pending_note(text: str, today: str) -> str:
    """조문에 '[시행일: 2027.8.5] …'처럼 시행일이 따로 적힌 줄이 있으면 그 줄을 돌려준다.

    날짜가 이미 지난 표시는 건너뛴다. 날짜 없이 '[시행일] …'로만 적힌 줄은 시행 여부를 글만으로 알 수 없어 그대로 돌려주되,
    날짜를 정하는 내용이 이어지는 줄('가. …', '나. …', 대괄호 주석)에 있으므로 그 줄들까지 함께 돌려준다.
    """
    lines = [line.strip() for line in text.split("\n")]
    for position, line in enumerate(lines):
        matched = _PENDING_NOTE.match(line)
        if not matched:
            continue
        if matched.group(1):
            if f"{int(matched.group(1)):04d}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}" <= today:
                continue
            return line
        tail = []
        for following in lines[position + 1:]:
            if not (_ITEM_LINE.match(following) or following.startswith("[")) or _PENDING_NOTE.match(following):
                break
            tail.append(following)
        # 이어지는 줄마다 날짜가 적혀 있고 그 날짜가 모두 지났으면 더 알릴 것이 없다. 하나라도 날짜를 알 수 없으면 그대로 알린다.
        dates = [_NOTE_DATE.findall(following) for following in tail if _ITEM_LINE.match(following)] or [_NOTE_DATE.findall(" ".join(tail))]
        if all(dates) and all(f"{int(y):04d}-{int(m):02d}-{int(d):02d}" <= today for found in dates for y, m, d in found):
            continue
        return " ".join([line, *tail])
    return ""


def _index() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return _cache


def reload() -> None:
    """자료를 다시 읽는다(수집 직후나 시험에서 쓴다)."""
    global _cache
    with _lock:
        _cache = None


def laws() -> list[dict]:
    return list(_index()["laws"].values())


def resolve_law(text: str) -> Optional[str]:
    """법령 이름·줄임말·id를 법령 id로 바꾼다. 못 찾으면 None."""
    key = _squash(text)
    if not key:
        return None
    for name, law_id in _index()["names"]:
        if key == name:
            return law_id
    return None


def normalize_article(text: str) -> Optional[str]:
    """'43조', '제 42 조의 3' 같은 표기를 '제43조', '제42조의3'으로 맞춘다."""
    matched = _ARTICLE_REF.search(text or "")
    if not matched:
        return None
    return f"제{int(matched.group(1))}조" + (f"의{int(matched.group(2))}" if matched.group(2) else "")


def article_url(law: dict, article: str) -> str:
    """국가법령정보센터에서 그 조문을 여는 주소. 조약은 조문별 주소가 없어 조약 화면 주소를 쓴다."""
    if law.get("kind") == "조약" and law.get("source_url"):
        return law["source_url"]
    name = urllib.parse.quote(law["name"].replace(" ", ""))
    return f"https://www.law.go.kr/{urllib.parse.quote('법령')}/{name}/{urllib.parse.quote(article)}"


def _public(provision: dict, full: bool = True) -> dict:
    law = _index()["laws"][provision["law"]]
    today = _today()
    state, future, outdated = _law_update(_index()["updates"], law["id"], today)
    changes = [version["effective_date"] for version in future if provision["article"] in version["changed"] or provision["article"] in version["removed"]]
    deferred = [version["deferred"]["note"] for version in future if version["deferred"] and provision["article"] in version["deferred"]["articles"]]
    text = provision["text"]
    return {
        "law_id": law["id"],
        "law": law["name"],
        "law_short": law["short"],
        "article": provision["article"],
        "heading": provision["heading"],
        "ref": f"{law['short']} {provision['article']}",
        "text": text if full else (text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")),
        "deleted": provision["deleted"],
        "effective_date": law["effective_date"],
        "source": law["source"],
        "url": article_url(law, provision["article"]),
        "fetched_at": law["fetched_at"],
        "kind": law["kind"],
        # 조약이면 서명 문구 가운데 정본 언어와 우선하는 본을 적은 문장(법령은 빈 글).
        "law_note": law["note"],
        # 변경 확인 결과: 이 조문이 바뀔 예정인 가장 이른 날(없으면 빈 글), 저장된 판이 이미 옛 판이 됐는지.
        "upcoming_change": min(changes) if changes else "",
        "law_changed": state == "changed",
        # 저장된 판이 옛 판일 때 이 조문의 글이 지금 판에서 달라졌는지: True(달라짐) · False(그대로) · None(어느 조가 달라졌는지 모름).
        "article_changed": (None if outdated is None else provision["article"] in outdated) if state == "changed" else False,
        # 시행일이 따로 정해진 부분이 있으면 그 표시(조문 안의 '[시행일: 2027.8.5] …' 줄이나 부칙의 해당 구절). 없으면 빈 글.
        "pending_note": _pending_note(provision["text"], today) or (f"부칙: {deferred[0]}" if deferred else ""),
    }


def get(law: str, article: str, full: bool = True) -> Optional[dict]:
    """법령과 조문 번호로 조문 하나를 돌려준다. 없으면 None."""
    law_id = resolve_law(law)
    normalized = normalize_article(article)
    if not law_id or not normalized:
        return None
    provision = _index()["by_key"].get((law_id, normalized))
    return _public(provision, full) if provision else None


def _find_law_in(query: str) -> tuple[Optional[str], str]:
    """질문 안에 법령 이름이 있으면 (법령 id, 그 이름을 뺀 나머지)를 돌려준다."""
    squashed = _squash(query)
    for name, law_id in _index()["names"]:
        if len(name) >= 2 and not name.isascii() and name in squashed:
            # 띄어쓰기가 다를 수 있으므로 원래 글에서는 글자 사이 공백을 허용해 지운다.
            pattern = r"\s*".join(re.escape(char) for char in name)
            return law_id, re.sub(pattern, " ", query, count=1)
    return None, query


def _term_groups(query: str) -> list[tuple[str, ...]]:
    """질문을 낱말 묶음으로 나눈다. 묶음 하나는 같은 뜻으로 볼 낱말들(원래 꼴, 조사 뗀 꼴, 법령의 말)이다."""
    groups: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for token in _SPLIT.split(query):
        token = token.strip()
        if len(token) < 2 or token in _STOP or token in seen:
            continue
        seen.add(token)
        variants = [token]
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                variants.append(token[: -len(suffix)])
                break
        for variant in list(variants):
            for key, words in SYNONYMS.items():
                if key in variant:
                    variants.extend(words)
        groups.append(tuple(dict.fromkeys(variant for variant in variants if variant not in _STOP)))
    return [group for group in groups if group]


def search(query: str, law: Optional[str] = None, group: Optional[str] = None, limit: int = 8) -> list[dict]:
    """조문을 찾는다. law는 법령 이름·줄임말, group은 common·allocation·crossborder.

    질문에 법령 이름과 조문 번호가 함께 있으면(예: '중수청법 43조') 그 조문을 맨 앞에 둔다.
    """
    index = _index()
    limit = min(max(int(limit), 1), MAX_LIMIT)
    law_id = resolve_law(law) if law else None
    if law and not law_id:
        return []
    named_law, rest = _find_law_in(query)
    law_id = law_id or named_law
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    direct = normalize_article(rest) if law_id else None
    if direct and (law_id, direct) in index["by_key"]:
        results.append(_public(index["by_key"][(law_id, direct)], full=False))
        seen.add((law_id, direct))
        rest = _ARTICLE_REF.sub(" ", rest, count=1)

    groups = _term_groups(rest)
    scored: list[tuple[float, int, dict]] = []
    for order, provision in enumerate(index["provisions"]):
        if provision["deleted"] or (law_id and provision["law"] != law_id):
            continue
        if group and index["laws"][provision["law"]]["group"] != group:
            continue
        if (provision["law"], provision["article"]) in seen:
            continue
        if not groups:
            continue
        # 낱말 묶음을 몇 개나 맞혔는지가 먼저이고, 그다음이 제목에 있는지와 본문에 나온 횟수다.
        score, matched = 0.0, 0
        for variants in groups:
            best = 0.0
            for variant in variants:
                value = (5.0 if variant in provision["heading"] else 0.0) + min(provision["text"].count(variant), 3)
                best = max(best, value)
            if best:
                matched += 1
                score += best
        if not matched:
            continue
        score += 10.0 * matched + _KIND_PRIOR.get(index["laws"][provision["law"]]["kind"], 0.0)
        scored.append((score, order, provision))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _score, _order, provision in scored[: limit - len(results)]:
        results.append(_public(provision, full=False))
    return results[:limit]


def status() -> dict:
    """받아 둔 법령의 목록과 받은 시각(화면과 도우미가 '어느 시점의 조문인지' 알리는 데 쓴다)."""
    index = _index()
    updates = index["updates"]
    today = _today()
    fetched = sorted(law["fetched_at"] for law in index["laws"].values() if law["fetched_at"])
    sources = list(dict.fromkeys(law["source"] for law in index["laws"].values() if law["source"]))
    per_law = {law_id: _law_update(updates, law_id, today) for law_id in index["laws"]}
    upcoming = [
        {"law_id": law_id, "law": index["laws"][law_id]["short"], "effective_date": version["effective_date"], "promulgation": version["promulgation"],
         "changed": version["changed"], "added": version["added"], "removed": version["removed"], "deferred": version["deferred"]}
        for law_id, (_state, versions, _outdated) in per_law.items() for version in versions
    ]
    return {
        "law_count": len(index["laws"]),
        "provision_count": len(index["provisions"]),
        "fetched_from": ", ".join(sources) or "국가법령정보센터(law.go.kr)",
        "oldest_fetch": fetched[0] if fetched else "",
        "latest_fetch": fetched[-1] if fetched else "",
        "laws": [
            {
                **{key: law[key] for key in ("id", "name", "short", "kind", "group", "effective_date", "promulgation", "source_url", "fetched_at", "provision_count")},
                # 변경 확인: current(저장된 판이 지금 시행 중) · changed(새 판이 시행됨, 다시 받아야 함) · unknown(확인 실패) · 빈 글(확인한 적 없음)
                "update": per_law[law["id"]][0],
                "upcoming": [version["effective_date"] for version in per_law[law["id"]][1]],
                "upcoming_label": _upcoming_label(per_law[law["id"]][1]),
                "update_failed": bool(updates["entries"].get(law["id"], {}).get("failed")),  # 마지막 확인이 실패해 앞선 결과를 보여 주는 중
            }
            for law in index["laws"].values()
        ],
        "updates": {
            "checked_at": updates["checked_at"],
            "changed": [index["laws"][law_id]["short"] for law_id, (state, _versions, _outdated) in per_law.items() if state == "changed"],
            "upcoming": sorted(upcoming, key=lambda item: (item["effective_date"], item["law"])),
        },
    }
