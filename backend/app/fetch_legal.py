"""국가법령정보(법제처)에서 법령과 조약 본문을 받아 조문 단위 JSON으로 저장한다.

수사 지원의 법령 안내는 AI가 그때그때 찾아 답하지 않는다. 공식 출처에서 미리 받아 저장한 조문만 쓴다
(docs/investigation-guidance-design.md 4장).

받는 길은 두 가지다.
- Open API(법제처 국가법령정보 공동활용, open.law.go.kr): 인증키(OC)를 환경 변수 LAW_OC에 두면 이 길을 쓴다.
  법령은 '시행일 법령'(target=eflaw)에서 지금 시행 중인 판을 받는다. '현행 법령'(target=law) 본문에는 공포만 되고
  아직 시행 전인 조문이 섞여 나오므로 쓰지 않는다(예: 2027년에 시행될 형사소송법 제197조의4). 조약은 target=trty.
- 국가법령정보센터 웹 화면이 본문을 불러올 때 쓰는 요청(lsInfoR.do): 인증키가 없을 때의 길이다. 법령만 받는다.
  두 길로 받은 조문은 글자가 같다(따옴표 모양과 개정 표시의 날짜 표기만 다르다).

- 저장: 받은 원본은 data/legal_raw/에 두고(깃에 올리지 않는다. 인증키가 섞이지 않게 가린다), 조문으로 나눈 결과만
  backend/app/legal_data/kr/<id>.json에 둔다. 결과에는 받은 주소, 받은 시각, 원본 해시, 시행일, 공포 정보를 남긴다.
- 받을 목록: backend/app/legal_data/sources.json
- 변경 확인: --check는 본문을 고치지 않고, 저장된 판이 지금도 시행 중인 판인지와 시행 예정인 개정을 확인해
  backend/app/legal_data/updates.json에 적는다(Open API가 필요하다).

실행(저장소 루트에서):
    .venv/Scripts/python.exe -X utf8 -m backend.app.fetch_legal             # 목록 전부
    .venv/Scripts/python.exe -X utf8 -m backend.app.fetch_legal 형사소송법    # 이름에 이 글자가 들어간 것만
    .venv/Scripts/python.exe -X utf8 -m backend.app.fetch_legal --check     # 변경 확인만
    옵션: --source auto|api|web (기본 auto: 인증키가 있으면 api)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from backend.app.paths import APP_DIR, DATA_DIR


LEGAL_DIR = APP_DIR / "legal_data"
SOURCES_PATH = LEGAL_DIR / "sources.json"
OUT_DIR = LEGAL_DIR / "kr"
UPDATES_PATH = LEGAL_DIR / "updates.json"
RAW_DIR = DATA_DIR / "legal_raw"

SITE = "https://www.law.go.kr"
API = f"{SITE}/DRF"
USER_AGENT = "Mozilla/5.0 (compatible; PublicLawFetcher/1.0)"
REQUEST_INTERVAL_SEC = 1.0
API_INTERVAL_SEC = 0.4
TIMEOUT_SEC = 60.0
# 웹 본문 요청의 고정 값: 한글 본문(010202), 공포문 미포함.
BODY_PARAMS = {"chrClsCd": "010202", "ancYnChk": "0"}
SOURCE_WEB = "국가법령정보센터"
SOURCE_API = "법제처 국가법령정보 공동활용 Open API"

_LSI_SEQ = re.compile(r"lsiSeq\s*:\s*'(\d+)'")
_EF_YD = re.compile(r"efYd\s*:\s*'(\d{8})'")
_TITLE = re.compile(r"<h2>\s*(.*?)\s*</h2>", re.S)
_HEADER = re.compile(r"\[시행\s*([0-9. ]+?)\]\s*\[([^\]]+)\]")
_ANCHOR = re.compile(r'<a name="(pjuk:\d+|J\d+:\d+)"[^>]*>\s*</a>')
_PARAGRAPH = re.compile(r'<p class="(pty[^"]*|gtit)"[^>]*>(.*?)</p>', re.S)
_LABEL = re.compile(r"<label[^>]*>(.*?)</label>", re.S)
_ARTICLE = re.compile(r"제\s*(\d+)\s*조(?:의\s*(\d+))?\s*(?:\((.*)\))?", re.S)
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>", re.I)
_SPACE = re.compile(r"[ \t\r\f\v 　]+")
_NOTE = re.compile(r"\s*<[^<>]*\d{4}\.[^<>]*>")  # 장 제목 뒤의 '<개정 2010. 3. 31.>' 같은 표시
_LEVEL = re.compile(r"^제\s*\d+\s*(편|장|절|관)")
_LEVELS = ("편", "장", "절", "관")
# API 조문내용 첫머리의 '제43조(다른 수사기관과의 관계)'. 제목 안에 괄호가 한 겹 더 있어도 맞춘다.
_API_LABEL = re.compile(r"^제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(\((?:[^()]|\([^()]*\))*\))?")
# 조약의 조 제목 줄: '제5조 공조요청서'처럼 '조' 뒤에 빈칸이 온다. 본문의 '제5조에 따라', '제1조제4항'은 여기에 걸리지 않는다.
_TREATY_ARTICLE = re.compile(r"^제\s*(\d+)\s*조(?:\s+(.*))?$")
_TREATY_ARTICLE_PAREN = re.compile(r"^제\s*(\d+)\s*조\s*\((.+)\)$")  # '제1조(정의)', '제1조 (정의)'
# '제9조 내지 제11조 삭제 <…>'처럼 여러 조를 한 줄로 지운 모양(조 번호 뒤에 남는 부분)
_RANGE_DELETED = re.compile(r"^(?:내지|부터)\s*제\s*\d+\s*조(?:\s*의\s*\d+)?\s*(?:까지)?\s*삭제")
# 두 길로 받은 글을 견줄 때 지우는 것: 시행일 안내 줄, 개정 표시(날짜 표기가 서로 다르다).
_COMPARE_NOTES = re.compile(r"\[시행일[^\]]*\][^\n]*|<[^<>]*\d{4}[^<>]*>|\[[^\[\]]*\d{4}[^\[\]]*\]")
_COMPARE_CHARS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "＂": '"', "ㆍ": "·", "・": "·", "‧": "·"})


class FetchError(RuntimeError):
    """법령을 받지 못했거나 응답 구조가 달라 조문을 나누지 못했다."""


# ---------------------------------------------------------------- 공통


def api_key() -> str:
    """Open API 인증키(OC). 환경 변수에 없으면 저장소의 .env에서 읽는다. 없으면 빈 글."""
    if not os.environ.get("LAW_OC"):
        try:
            from dotenv import load_dotenv

            load_dotenv(APP_DIR.parent.parent / ".env")
        except Exception:  # dotenv가 없어도 환경 변수만으로 동작한다
            pass
    return (os.environ.get("LAW_OC") or "").strip()


def _mask(text: str) -> str:
    """오류 문장과 저장하는 원본에 인증키가 남지 않게 가린다."""
    key = api_key()
    return text.replace(key, "***") if key else text


def _get(url: str, data: dict | None = None) -> str:
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
        return response.read().decode("utf-8", "replace")


def _plain(fragment: str) -> str:
    """HTML 조각을 화면에 보이는 글로 바꾼다.

    조문 안의 태그는 다른 조문으로 가는 링크 같은 글자 꾸밈이다. 지울 때 빈칸을 넣으면 '제43조제2항'이
    '제43조 제2항'으로, '제301조의2'가 '제301조 의2'로 갈라진다. 그래서 태그는 그대로 지우고
    줄바꿈(<br>)만 줄바꿈으로 남긴다.
    """
    text = html.unescape(_TAG.sub("", _BREAK.sub("\n", fragment)))
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _lines(text: str | None) -> list[str]:
    """한 요소의 글을 줄로 나눈다(줄 안의 빈칸은 하나로, 빈 줄은 버린다)."""
    out = []
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = _SPACE.sub(" ", line).strip()
        if line:
            out.append(line)
    return out


def _all_text(element) -> str:
    """CDATA가 여러 조각으로 나뉘어 있어도 모두 잇는다."""
    return "".join(element.itertext()) if element is not None else ""


def _date(dotted: str) -> str:
    """'2026. 10. 2.' 또는 '20261002' → '2026-10-02'. 모양이 다르면 받은 글을 그대로 돌려준다."""
    text = (dotted or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    numbers = re.findall(r"\d+", text)
    if len(numbers) != 3:
        return text
    return f"{int(numbers[0]):04d}-{int(numbers[1]):02d}-{int(numbers[2]):02d}"


def _dotted(yyyymmdd: str) -> str:
    """'20260929' → '2026. 9. 29.'"""
    if not re.fullmatch(r"\d{8}", yyyymmdd or ""):
        return yyyymmdd or ""
    return f"{int(yyyymmdd[:4])}. {int(yyyymmdd[4:6])}. {int(yyyymmdd[6:])}."


def _number(text: str) -> str:
    """공포번호의 앞자리 0을 뗀다('00651' → '651')."""
    return str(int(text)) if (text or "").isdigit() else (text or "")


def _chapter_step(path: dict[str, str], title: str) -> None:
    """편·장·절·관 제목을 만나면 그 아래 단계를 비우고 자리를 채운다."""
    level = _LEVEL.match(title)
    depth = _LEVELS.index(level.group(1)) if level else 1
    for lower in _LEVELS[depth:]:
        path.pop(lower, None)
    path[_LEVELS[depth]] = title


def page_url(name: str) -> str:
    """법령 이름으로 여는 화면 주소(사람이 확인할 때 쓰는 주소이기도 하다)."""
    return f"{SITE}/{urllib.parse.quote('법령')}/{urllib.parse.quote(name.replace(' ', ''))}"


def comparable(text: str) -> str:
    """두 길(웹 화면, Open API)로 받은 조문을 견줄 때 쓰는 꼴: 빈칸, 따옴표 모양, 개정 표시를 없앤다."""
    return re.sub(r"\s+", "", _COMPARE_NOTES.sub("", text or "").translate(_COMPARE_CHARS))


# ---------------------------------------------------------------- 웹 화면에서 받기


def resolve(name: str) -> tuple[str, str]:
    """법령 이름으로 지금 시행 중인 본문의 일련번호(lsiSeq)와 시행일(efYd)을 찾는다."""
    page = _get(page_url(name))
    seq, ef = _LSI_SEQ.search(page), _EF_YD.search(page)
    if not seq or not ef:
        raise FetchError(f"'{name}' 화면에서 법령 일련번호나 시행일을 찾지 못했습니다(이름을 확인하세요).")
    return seq.group(1), ef.group(1)


def fetch_body(lsi_seq: str, ef_yd: str) -> str:
    return _get(f"{SITE}/LSW/lsInfoR.do", {"lsiSeq": lsi_seq, "efYd": ef_yd, **BODY_PARAMS})


def parse_law(body: str) -> dict:
    """웹 본문 HTML을 법령 정보와 조문 목록으로 나눈다. 부칙은 넣지 않는다."""
    title = _TITLE.search(body)
    head = body[: body.find('id="conScroll"')] if 'id="conScroll"' in body else body[:6000]
    header = _HEADER.search(_SPACE.sub(" ", html.unescape(_TAG.sub(" ", head))))
    main = body.split('<div id="arDivArea">', 1)[0]
    anchors = list(_ANCHOR.finditer(main))
    provisions: list[dict] = []
    path: dict[str, str] = {}  # 편·장·절·관 제목. 조문이 어느 편의 어느 장에 있는지 함께 남긴다.
    for index, anchor in enumerate(anchors):
        block = main[anchor.end(): anchors[index + 1].start() if index + 1 < len(anchors) else len(main)]
        paragraphs = _PARAGRAPH.findall(block)
        if anchor.group(1).startswith("pjuk:"):
            titles = [_NOTE.sub("", _plain(text)).strip() for cls, text in paragraphs if cls == "gtit"]
            if titles:
                _chapter_step(path, titles[0])
            continue
        lines = [(cls, text) for cls, text in paragraphs if cls != "gtit"]
        if not lines:
            continue
        label = _LABEL.search(lines[0][1])
        label_text = _plain(label.group(1)) if label else ""
        matched = _ARTICLE.match(label_text)
        if not matched:
            continue
        first = lines[0][1].replace(label.group(0), "", 1) if label else lines[0][1]
        texts = [_plain(first)] + [_plain(text) for _cls, text in lines[1:]]
        text = "\n".join(line for line in texts if line)
        number, branch = int(matched.group(1)), int(matched.group(2) or 0)
        heading = (matched.group(3) or "").strip()
        provisions.append(
            {
                "article": f"제{number}조" + (f"의{branch}" if branch else ""),
                "no": number,
                "branch": branch,
                "heading": heading,
                "chapter": " > ".join(path[level] for level in _LEVELS if level in path),
                "text": text,
                "deleted": not heading and text.lstrip().startswith("삭제"),
            }
        )
    if not provisions:
        raise FetchError("본문에서 조문을 하나도 찾지 못했습니다(화면 구조가 바뀌었을 수 있습니다).")
    return {
        "title": _plain(title.group(1)) if title else "",
        "effective_date": _date(header.group(1)) if header else "",
        "promulgation": header.group(2).strip() if header else "",
        "provisions": provisions,
    }


def fetch_law_web(source: dict) -> dict:
    """법령 하나를 웹 화면에서 받아 저장할 기록을 만든다."""
    name = source["name"]
    lsi_seq, ef_yd = resolve(name)
    time.sleep(REQUEST_INTERVAL_SEC)
    body = fetch_body(lsi_seq, ef_yd)
    raw = body.encode("utf-8")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # 받은 글을 바이트 그대로 둔다(글로 쓰면 윈도우에서 줄바꿈이 바뀌어 해시와 맞지 않게 된다).
    (RAW_DIR / f"{source['id']}_{lsi_seq}.html").write_bytes(raw)
    parsed = parse_law(body)
    return {
        **_record_head(source),
        "title": parsed["title"] or name,
        "effective_date": parsed["effective_date"] or _date(ef_yd),
        "promulgation": parsed["promulgation"],
        "lsi_seq": lsi_seq,
        "source": SOURCE_WEB,
        "source_url": page_url(name),
        "fetched_from": f"{SITE}/LSW/lsInfoR.do?lsiSeq={lsi_seq}&efYd={ef_yd}",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "provisions": parsed["provisions"],
    }


# ---------------------------------------------------------------- Open API로 받기


def api_get(kind: str, params: dict) -> str:
    """Open API를 부른다. kind는 lawSearch(목록) 또는 lawService(본문). 인증키는 여기서만 붙인다."""
    key = api_key()
    if not key:
        raise FetchError("Open API 인증키(LAW_OC)가 없습니다.")
    url = f"{API}/{kind}.do?" + urllib.parse.urlencode({"OC": key, "type": "XML", **params})
    try:
        body = _get(url)
    except Exception as exc:
        raise FetchError(_mask(f"Open API 요청 실패({kind} {params.get('target')}): {exc}")) from None
    head = body.lstrip()[:300].lower()
    if "<html" in head or "<!doctype" in head or "<" not in head:
        raise FetchError("Open API가 XML이 아닌 응답을 냈습니다(점검 중이거나 주소가 바뀌었을 수 있습니다).")
    if "사용자 정보 검증에 실패" in body or "미신청된 목록" in body:
        raise FetchError("Open API가 요청을 거절했습니다(활용 신청, 등록한 IP·도메인을 확인하세요).")
    return body


def _rows(body: str, tag: str) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FetchError(f"Open API 응답을 읽지 못했습니다: {exc}") from None
    rows = [{child.tag: (child.text or "").strip() for child in row} for row in root.findall(tag)]
    return rows, int(root.findtext("totalCnt") or 0)


def api_versions(name: str) -> list[dict]:
    """시행일 법령 목록에서 이름이 꼭 같은 법령의 판을 모은다(현행연혁코드: 연혁·현행·시행예정)."""
    found: list[dict] = []
    page = 1
    while True:
        rows, total = _rows(api_get("lawSearch", {"target": "eflaw", "query": name, "display": 100, "page": page}), "law")
        found.extend(rows)
        if not rows or len(found) >= total:
            break
        page += 1
        time.sleep(API_INTERVAL_SEC)
    squashed = name.replace(" ", "")
    return [row for row in found if row.get("법령명한글", "").replace(" ", "") == squashed]


def pick_current(rows: list[dict], name: str) -> dict:
    current = [row for row in rows if row.get("현행연혁코드") == "현행"]
    if len(current) != 1:
        raise FetchError(f"'{name}'의 현행 판을 하나로 정하지 못했습니다({len(current)}건, 같은 이름 {len(rows)}건).")
    return current[0]


def upcoming_versions(rows: list[dict]) -> list[dict]:
    """공포됐지만 아직 시행 전인 판(시행일 순)."""
    seen = sorted({(row["시행일자"], row["법령일련번호"], row.get("공포번호", ""), row.get("공포일자", ""), row.get("법령구분명", ""))
                   for row in rows if row.get("현행연혁코드") == "시행예정"})
    return [
        {"effective_date": _date(ef), "mst": mst, "promulgation": f"{kind} 제{_number(number)}호, {_dotted(day)}".strip()}
        for ef, mst, number, day, kind in seen
    ]


def parse_api_law(xml_text: str) -> dict:
    """Open API의 법령 본문 XML을 법령 정보, 조문, 부칙, 별표로 나눈다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"법령 본문 XML을 읽지 못했습니다: {exc}") from None
    info = root.find("기본정보")

    def field(tag: str) -> str:
        return (info.findtext(tag) or "").strip() if info is not None else ""

    path: dict[str, str] = {}
    provisions: list[dict] = []
    block = root.find("조문")
    for unit in block.findall("조문단위") if block is not None else []:
        content = _lines(_all_text(unit.find("조문내용")))
        if (unit.findtext("조문여부") or "").strip() == "전문":  # 편·장·절·관 제목
            title = _NOTE.sub("", " ".join(content)).strip()
            if title:
                _chapter_step(path, title)
            continue
        number, branch = int(unit.findtext("조문번호") or 0), int(unit.findtext("조문가지번호") or 0)
        heading = (unit.findtext("조문제목") or "").strip()
        if heading.startswith("(") and heading.endswith(")"):  # 옛 법령은 제목 칸에 괄호까지 들어 있다
            heading = heading[1:-1].strip()
        ranged = False
        if content:
            first = content[0]
            label = _API_LABEL.match(first)
            if label and _RANGE_DELETED.match(first[label.end():].strip()):
                ranged = True  # 여러 조를 한꺼번에 지운 줄은 글을 그대로 두고 삭제로 표시한다
            elif label:
                # 제목은 본문과 같은 글자를 쓰는 조문내용 쪽을 먼저 쓴다(조문제목 칸은 가운뎃점 모양이 다를 때가 있다).
                if label.group(1):
                    heading = label.group(1)[1:-1].strip()
                first = first[label.end():].strip()
                content = ([first] if first else []) + content[1:]
        lines = list(content)
        for hang in unit.findall("항"):
            lines.extend(_lines(_all_text(hang.find("항내용"))))
            for ho in hang.findall("호"):
                lines.extend(_lines(_all_text(ho.find("호내용"))))
                for mok in ho.findall("목"):
                    lines.extend(_lines(_all_text(mok.find("목내용"))))
        lines.extend(_lines(_all_text(unit.find("조문참고자료"))))
        text = "\n".join(lines)
        provisions.append(
            {
                "article": f"제{number}조" + (f"의{branch}" if branch else ""),
                "no": number,
                "branch": branch,
                "heading": heading,
                "chapter": " > ".join(path[level] for level in _LEVELS if level in path),
                "text": text,
                "deleted": ranged or (not heading and text.lstrip().startswith("삭제")),
                "key": unit.get("조문키", ""),
            }
        )
    if not provisions:
        raise FetchError("본문에서 조문을 하나도 찾지 못했습니다(응답 구조가 바뀌었을 수 있습니다).")

    addenda = []
    block = root.find("부칙")
    for unit in block.findall("부칙단위") if block is not None else []:
        addenda.append({
            "promulgated": _date((unit.findtext("부칙공포일자") or "").strip()),
            "number": _number((unit.findtext("부칙공포번호") or "").strip()),
            "text": "\n".join(_lines(_all_text(unit.find("부칙내용")))),
        })
    appendices = []
    block = root.find("별표")
    for unit in block.findall("별표단위") if block is not None else []:
        kind = (unit.findtext("별표구분") or "").strip()
        appendices.append({
            "kind": kind,
            "number": _number((unit.findtext("별표번호") or "").strip()),
            "branch": _number((unit.findtext("별표가지번호") or "").strip()),
            "title": " ".join(_lines(unit.findtext("별표제목"))),
            # 서식(신청서 같은 양식)은 제목만 둔다. 죄명 목록 같은 별표의 글은 남긴다.
            "text": "\n".join(_lines(_all_text(unit.find("별표내용")))) if kind == "별표" else "",
        })
    return {
        "title": field("법령명_한글"),
        "short_name": field("법령명약칭"),
        "law_id": field("법령ID"),
        "law_kind": field("법종구분"),
        "ministry": field("소관부처"),
        "effective_date": _date(field("시행일자")),
        "promulgation": f"{field('법종구분')} 제{_number(field('공포번호'))}호, {_dotted(field('공포일자'))}, {field('제개정구분')}",
        "provisions": provisions,
        "addenda": addenda,
        "appendices": appendices,
    }


def parse_treaty(xml_text: str) -> dict:
    """Open API의 조약 본문 XML을 조약 정보와 조문으로 나눈다. 본문은 [본문]…[/본문] 안의 '제N조 제목' 단위다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"조약 본문 XML을 읽지 못했습니다: {exc}") from None
    info = root.find("조약기본정보")
    body = root.find("조약내용").findtext("조약내용") if root.find("조약내용") is not None else ""
    if info is None or not body:
        raise FetchError("조약 본문을 찾지 못했습니다(응답 구조가 바뀌었을 수 있습니다).")

    def block(name: str) -> str:
        found = re.search(rf"\[{name}\](.*?)\[/{name}\]", body, re.S)
        return "\n".join(_lines(found.group(1))) if found else ""

    provisions: list[dict] = []
    current: dict | None = None
    main = re.search(r"\[본문\](.*?)\[/본문\]", body, re.S)
    if not main:
        raise FetchError("조약 본문 표시([본문])를 찾지 못했습니다(응답 구조가 바뀌었을 수 있습니다).")
    path: dict[str, str] = {}
    for line in _lines(main.group(1)):
        if _LEVEL.match(line) and len(line) <= 40 and not line.endswith(("다.", "다")):
            # 장·절 제목 줄: 앞 조의 본문에 붙이지 않고, 뒤에 오는 조들의 자리로 적는다.
            _chapter_step(path, line)
            current = None
            continue
        heading = _TREATY_ARTICLE_PAREN.match(line) or _TREATY_ARTICLE.match(line)
        name = (heading.group(2) or "").strip() if heading else ""
        # 조 제목 줄은 짧고 문장이 아니다. 본문 가운데의 '제5조 제1항은 …한다.' 같은 줄을 제목으로 잘못 보지 않게 거른다.
        if heading and len(name) <= 40 and not name.endswith(("다.", "다")):
            current = {"article": f"제{int(heading.group(1))}조", "no": int(heading.group(1)), "branch": 0,
                       "heading": name, "chapter": " > ".join(path[level] for level in _LEVELS if level in path), "lines": []}
            provisions.append(current)
        elif current is not None:
            current["lines"].append(line)
    for item in provisions:
        item["text"] = "\n".join(item.pop("lines"))
        item["deleted"] = False
    if not provisions:
        raise FetchError("조약 본문에서 조문을 하나도 찾지 못했습니다.")
    numbers = [item["no"] for item in provisions]
    if numbers != list(range(1, len(numbers) + 1)):
        raise FetchError(f"조약 조문 번호가 차례대로 이어지지 않습니다: {numbers}")
    untitled = [item["article"] for item in provisions if not item["heading"]]
    if untitled:  # 제목이 다음 줄에 오는 모양 등: 조용히 잘못 나누느니 멈춘다
        raise FetchError(f"조약의 조 제목을 읽지 못했습니다: {untitled[:5]}")

    def field(tag: str) -> str:
        return (info.findtext(tag) or "").strip()

    return {
        "title": field("조약명_한글"),
        "treaty_no": field("조약번호"),
        "signed_on": _date(field("서명일자")),
        "effective_date": _date(field("발효일자")),
        "gazetted_on": _date(field("관보게재일자")),
        "promulgation": f"조약 제{field('조약번호')}호, {_dotted(field('관보게재일자'))} 관보 게재",
        "preamble": block("전문"),
        "notice": block("공고문"),
        "signature": block("서명"),  # 정본 언어와 해석이 갈릴 때 어느 본이 우선하는지가 여기에 있다
        "provisions": provisions,
    }


def _record_head(source: dict) -> dict:
    return {
        "id": source["id"],
        "name": source["name"],
        "title": source["name"],
        "kind": source.get("kind", "법률"),
        "aliases": source.get("aliases", []),
        "group": source.get("group", "common"),
        "jurisdiction": "KR",
    }


def _save_raw(name: str, text: str) -> str:
    """원본을 인증키를 가린 채 저장하고 그 해시를 돌려준다."""
    raw = _mask(text).encode("utf-8")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / name).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def fetch_law_api(source: dict) -> dict:
    """법령 하나의 '지금 시행 중인 판'을 Open API로 받아 저장할 기록을 만든다."""
    name = source["name"]
    rows = api_versions(name)
    current = pick_current(rows, name)
    mst, ef_yd = current["법령일련번호"], current["시행일자"]
    time.sleep(API_INTERVAL_SEC)
    xml_text = api_get("lawService", {"target": "eflaw", "MST": mst, "efYd": ef_yd})
    digest = _save_raw(f"{source['id']}_{mst}_{ef_yd}.xml", xml_text)
    parsed = parse_api_law(xml_text)
    return {
        **_record_head(source),
        "title": parsed["title"] or name,
        "short_name": parsed["short_name"],
        "ministry": parsed["ministry"],
        "effective_date": parsed["effective_date"] or _date(ef_yd),
        "promulgation": parsed["promulgation"],
        "law_id": parsed["law_id"],
        "lsi_seq": mst,
        "upcoming": upcoming_versions(rows),
        "source": SOURCE_API,
        "source_url": page_url(name),
        "fetched_from": f"{API}/lawService.do?target=eflaw&MST={mst}&efYd={ef_yd}&type=XML",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw_sha256": digest,
        "provisions": parsed["provisions"],
        "addenda": parsed["addenda"],
        "appendices": parsed["appendices"],
    }


def fetch_treaty(source: dict) -> dict:
    """조약 하나를 Open API로 받아 저장할 기록을 만든다. sources.json의 treaty_id는 조약일련번호다."""
    treaty_id = str(source["treaty_id"])
    xml_text = api_get("lawService", {"target": "trty", "ID": treaty_id})
    digest = _save_raw(f"{source['id']}_{treaty_id}.xml", xml_text)
    parsed = parse_treaty(xml_text)
    if parsed["title"].replace(" ", "") != source["name"].replace(" ", ""):
        raise FetchError(f"조약 이름이 목록과 다릅니다: 받은 이름 '{parsed['title']}'")
    return {
        **_record_head(source),
        "kind": "조약",
        "title": parsed["title"],
        "counterpart": source.get("counterpart", ""),
        "effective_date": parsed["effective_date"],
        "promulgation": parsed["promulgation"],
        "treaty_no": parsed["treaty_no"],
        "signed_on": parsed["signed_on"],
        "lsi_seq": treaty_id,
        "source": SOURCE_API,
        "source_url": f"{SITE}/LSW/trtyInfoP.do?trtySeq={treaty_id}",
        "fetched_from": f"{API}/lawService.do?target=trty&ID={treaty_id}&type=XML",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw_sha256": digest,
        "preamble": parsed["preamble"],
        "notice": parsed["notice"],
        "signature": parsed["signature"],
        "provisions": parsed["provisions"],
    }


# ---------------------------------------------------------------- 받아서 저장하기, 변경 확인


def fetch_law(source: dict, mode: str = "auto") -> dict:
    """법령이나 조약 하나를 받아 저장하고 기록을 돌려준다. mode: auto(인증키가 있으면 api) · api · web."""
    use_api = mode == "api" or (mode == "auto" and bool(api_key()))
    if source.get("target") == "trty":
        if not use_api:
            raise FetchError("조약은 Open API로만 받습니다(LAW_OC가 필요합니다).")
        record = fetch_treaty(source)
    else:
        record = fetch_law_api(source) if use_api else fetch_law_web(source)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{source['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return record


def changed_articles(current: list[dict], other: list[dict]) -> dict:
    """두 판의 조문을 견줘 바뀐 조, 새로 생긴 조, 없어진 조의 번호를 돌려준다."""
    now = {item["article"]: comparable(item.get("heading", "") + item.get("text", "")) for item in current}
    then = {item["article"]: comparable(item.get("heading", "") + item.get("text", "")) for item in other}
    return {
        "changed": [article for article in then if article in now and then[article] != now[article]],
        "added": [article for article in then if article not in now],
        "removed": [article for article in now if article not in then],
    }


_ARTICLE_REF = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
_ADDENDUM_LABEL = re.compile(r"^.*제\s*\d+\s*조(?:\s*의\s*\d+)?\s*\([^()]*\)", re.S)
_ADDENDUM_OWN = re.compile(r"부칙\s*제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*[항호목])*")  # '부칙 제9조제17항'
_OTHER_LAW_REF = re.compile(r"「[^」]*」(?:\s*제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*[항호목])*)?")  # '「형법」 제1조'
_PROMULGATION_NO = re.compile(r"제\s*(\d+)\s*호")


def deferred_by_addenda(addenda: list[dict], effective_date: str, promulgation: str = "", known: set[str] | None = None) -> dict | None:
    """조문 글은 같은데 시행일만 따로 잡힌 판이 무엇을 시행하는지 부칙에서 찾는다.

    부칙의 '…, 제19조제4호는 2028년 10월 2일부터 시행한다'처럼 그 날짜가 적힌 구절에서 조 번호를 뽑는다.
    찾지 못하면 None을 돌려준다(짐작해서 채우지 않는다). 엉뚱한 조에 표시가 붙지 않도록 범위를 좁힌다.
    - promulgation(그 판의 공포 정보, 예: '법률 제21491호, 2026. 3. 24.')을 주면 그 공포번호의 부칙만 본다.
    - 다른 법률을 고치면서 붙은 부칙('부칙(출입국관리법) <…>')은 보지 않는다.
    - '부칙 제9조제17항'처럼 부칙 자신의 조와 '「형법」 제1조'처럼 다른 법률의 조는 뺀다.
    - known(이 법에 실제로 있는 조 번호)을 주면 거기에 없는 조는 뺀다.
    """
    year, month, day = (int(part) for part in effective_date.split("-"))
    stamp = re.compile(rf"{year}년\s*{month}월\s*{day}일부터\s*시행")
    wanted = _PROMULGATION_NO.search(promulgation or "")
    for addendum in reversed(addenda):
        if re.match(r"\s*부칙\s*\(", addendum.get("text", "")):
            continue
        if wanted and str(addendum.get("number", "")).lstrip("0") != wanted.group(1).lstrip("0"):
            continue
        for sentence in re.split(r"(?<=다\.)\s*", addendum.get("text", "")):
            found = stamp.search(sentence)
            if not found:
                continue
            head = sentence[:found.start()]
            cut = max((head.rfind(mark) + len(mark) for mark in ("다만,", "시행하고,", "시행하며,") if mark in head), default=0)
            # 부칙 자신의 조 이름('제1조(시행일)')은 그날 시행되는 조가 아니므로 떼어 낸다.
            clause = _ADDENDUM_LABEL.sub("", head[cut:]).strip()
            own = _OTHER_LAW_REF.sub(" ", _ADDENDUM_OWN.sub(" ", clause))
            articles = list(dict.fromkeys(f"제{int(no)}조" + (f"의{int(branch)}" if branch else "") for no, branch in _ARTICLE_REF.findall(own)))
            if known is not None:
                articles = [article for article in articles if article in known]
            if articles:
                return {"articles": articles, "note": f"{clause} {found.group(0)}".strip()}
    return None


def check_law(source: dict, stored: dict) -> dict:
    """저장된 판이 지금도 시행 중인 판인지와, 시행 예정인 판이 어느 조문을 바꾸는지 확인한다.

    시행 예정인 판의 changed·added·removed는 '바로 앞 판'과 견준 것이다(첫 판은 지금 시행 중인 판과 견준다).
    저장된 판과 견준 누적 차이로 적으면, 한 번 바뀌고 마는 조문이 뒤의 판에서도 계속 '바뀔 예정'으로 보인다.
    저장된 판이 이미 옛 판이면 지금 시행 중인 판과 견준 차이를 current_diff에 적는다.
    """
    rows = api_versions(source["name"])
    current = pick_current(rows, source["name"])
    remote = {"mst": current["법령일련번호"], "effective_date": _date(current["시행일자"])}
    same = str(stored.get("lsi_seq")) == remote["mst"] and stored.get("effective_date") == remote["effective_date"]
    result = {"state": "current" if same else "changed", "stored": {"mst": str(stored.get("lsi_seq")), "effective_date": stored.get("effective_date", "")}, "remote": remote}
    before = stored["provisions"]
    if not same:
        time.sleep(API_INTERVAL_SEC)
        body = api_get("lawService", {"target": "eflaw", "MST": remote["mst"], "efYd": remote["effective_date"].replace("-", "")})
        before = parse_api_law(body)["provisions"]
        result["current_diff"] = changed_articles(stored["provisions"], before)
    known = {item["article"] for item in stored["provisions"]}
    upcoming = []
    for version in upcoming_versions(rows):
        time.sleep(API_INTERVAL_SEC)
        body = api_get("lawService", {"target": "eflaw", "MST": version["mst"], "efYd": version["effective_date"].replace("-", "")})
        after = parse_api_law(body)["provisions"]
        diff = changed_articles(before, after)
        entry = {**version, **diff}
        if not (diff["changed"] or diff["added"] or diff["removed"]):
            entry["deferred"] = deferred_by_addenda(stored.get("addenda") or [], version["effective_date"], version.get("promulgation", ""), known)
        upcoming.append(entry)
        before = after
    result["upcoming"] = upcoming
    return result


def check_updates(sources: list[dict]) -> tuple[dict, list[str]]:
    """목록의 법령마다 변경 여부를 확인해 updates.json에 적는다. (적은 내용, 이번에 확인하지 못한 법령 id)를 돌려준다.

    - 이번에 확인한 법령의 항목만 바꾼다. 이름을 골라 일부만 확인해도 다른 법령의 결과는 남는다.
    - 확인에 실패한 법령은 앞선 결과를 그대로 두고 실패한 사실만 덧붙인다. 네트워크 오류 한 번으로
      '개정 예정' 표시가 사라지면 안 되기 때문이다.
    - checked_at은 법령마다 적고, 맨 위의 checked_at은 그 가운데 가장 오래된 값이다('모두 이때까지는 확인했다').
      한 번도 확인하지 못한 법령이 하나라도 있으면 맨 위의 값은 비운다.
    - 조약은 판이 따로 없어 확인하지 않는다.
    """
    previous: dict = {}
    if UPDATES_PATH.is_file():
        try:
            previous = json.loads(UPDATES_PATH.read_text(encoding="utf-8")).get("laws") or {}
        except Exception as exc:  # 깨진 파일을 덮어쓰면 다른 법령의 앞선 결과가 소리 없이 사라진다
            raise FetchError(f"앞선 변경 확인 파일({UPDATES_PATH.name})을 읽지 못했습니다. 파일을 고치거나 지운 뒤 다시 실행하십시오: {exc}") from None
        if not isinstance(previous, dict):
            raise FetchError(f"앞선 변경 확인 파일({UPDATES_PATH.name})의 모양이 다릅니다. 파일을 고치거나 지운 뒤 다시 실행하십시오.")
    listed = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["laws"]
    known = {item["id"] for item in listed}
    laws: dict[str, dict] = {law_id: entry for law_id, entry in previous.items() if law_id in known and isinstance(entry, dict)}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    failed: list[str] = []
    for source in sources:
        if source.get("target") == "trty":
            continue
        path = OUT_DIR / f"{source['id']}.json"
        try:
            laws[source["id"]] = {**check_law(source, json.loads(path.read_text(encoding="utf-8"))), "checked_at": now}
        except Exception as exc:  # 하나가 실패해도 나머지는 확인한다
            failed.append(source["id"])
            before = laws.get(source["id"]) or {"state": "unknown", "upcoming": []}
            laws[source["id"]] = {**before, "last_error": _mask(str(exc))[:200], "last_failed_at": now}
        state = laws[source["id"]]
        notes = [f"{item['effective_date']} 시행 예정(바뀌는 조 {len(item['changed'])}, 새 조 {len(item['added'])})" for item in state.get("upcoming", [])]
        word = "확인 실패" if source["id"] in failed else "같음" if state["state"] == "current" else "바뀜"
        print(f"{word:6} {source['name']}" + (" · " + ", ".join(notes) if notes else ""))
        time.sleep(API_INTERVAL_SEC)
    stamps = [str((laws.get(item["id"]) or {}).get("checked_at") or "") for item in listed if item.get("target") != "trty"]
    result = {
        "checked_at": min(stamps) if stamps and all(stamps) else "",
        "source": f"{SOURCE_API}(시행일 법령 목록)",
        "laws": laws,
    }
    draft = UPDATES_PATH.with_name(UPDATES_PATH.name + ".tmp")  # 쓰다가 끊겨도 앞선 파일이 깨지지 않게 임시 파일을 거친다
    draft.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(draft, UPDATES_PATH)
    return result, failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="법령·조약 본문을 받아 조문 단위로 저장한다.")
    parser.add_argument("names", nargs="*", help="이름에 이 글자가 들어간 것만 받는다")
    parser.add_argument("--source", choices=("auto", "api", "web"), default="auto")
    parser.add_argument("--check", action="store_true", help="본문은 그대로 두고 변경 여부만 확인한다")
    args = parser.parse_args(argv)
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["laws"]
    wanted = [s for s in sources if not args.names or any(word in s["name"] for word in args.names)]
    if not wanted:
        print("받을 법령이 없습니다(이름을 확인하세요).")
        return 1
    if args.check:
        try:
            result, failed = check_updates(wanted)
        except FetchError as exc:
            print(f"실패  {_mask(str(exc))}")
            return 1
        states = [result["laws"][s["id"]]["state"] for s in wanted if s["id"] in result["laws"] and s["id"] not in failed]
        print(f"끝: 같음 {states.count('current')}개, 바뀜 {states.count('changed')}개, 확인 실패 {len(failed)}개")
        return 1 if failed else 0
    failed = 0
    for source in wanted:
        try:
            record = fetch_law(source, args.source)
        except Exception as exc:  # 하나가 실패해도 나머지는 받는다
            failed += 1
            print(f"실패  {source['name']}: {_mask(str(exc))}")
        else:
            print(
                f"받음  {record['name']} · 시행 {record['effective_date']} · {record['promulgation']} · "
                f"조문 {len(record['provisions'])}개 · {record['source']}"
            )
        time.sleep(API_INTERVAL_SEC if api_key() and args.source != "web" else REQUEST_INTERVAL_SEC)
    print(f"끝: {len(wanted) - failed}개 받음, {failed}개 실패")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
