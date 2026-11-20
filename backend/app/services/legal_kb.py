"""법령 지식베이스: 공식 출처에서 미리 받아 둔 조문을 읽어 찾고 돌려준다.

- 조문은 backend/app/fetch_legal.py가 국가법령정보센터에서 받은 것만 있다(backend/app/legal_data/kr/*.json).
  이 모듈은 조문을 만들거나 고치지 않는다. 받은 글, 받은 주소, 받은 시각을 그대로 돌려준다.
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
_KIND_PRIOR = {"법률": 1.0, "대통령령": 0.5}
_SPLIT = re.compile(r"[\s,.;:!?()\[\]{}<>「」『』“”\"'·/|]+")
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
            "promulgation": record.get("promulgation", ""),
            "source": record.get("source", ""),
            "source_url": record.get("source_url", ""),
            "fetched_at": record.get("fetched_at", ""),
            "provision_count": len(items),
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
    # 긴 이름부터 맞춰 봐야 '형법'이 '특정경제범죄 가중처벌 등에 관한 법률'의 일부로 잘못 잡히지 않는다.
    ordered_names = sorted(names.items(), key=lambda pair: len(pair[0]), reverse=True)
    return {"laws": laws, "names": ordered_names, "provisions": provisions, "by_key": by_key}


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
    """국가법령정보센터에서 그 조문을 여는 주소."""
    name = urllib.parse.quote(law["name"].replace(" ", ""))
    return f"https://www.law.go.kr/{urllib.parse.quote('법령')}/{name}/{urllib.parse.quote(article)}"


def _public(provision: dict, full: bool = True) -> dict:
    law = _index()["laws"][provision["law"]]
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
    fetched = sorted(law["fetched_at"] for law in index["laws"].values() if law["fetched_at"])
    return {
        "law_count": len(index["laws"]),
        "provision_count": len(index["provisions"]),
        "fetched_from": "국가법령정보센터(law.go.kr)",
        "oldest_fetch": fetched[0] if fetched else "",
        "latest_fetch": fetched[-1] if fetched else "",
        "laws": [
            {key: law[key] for key in ("id", "name", "short", "kind", "group", "effective_date", "promulgation", "source_url", "fetched_at", "provision_count")}
            for law in index["laws"].values()
        ],
    }
