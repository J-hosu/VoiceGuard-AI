"""국가법령정보센터(law.go.kr)에서 법령 본문을 받아 조문 단위 JSON으로 저장한다.

수사 지원의 법령 안내는 AI가 그때그때 찾아 답하지 않는다. 공식 출처에서 미리 받아 저장한 조문만 쓴다
(docs/investigation-guidance-design.md 4장).

- 받는 곳: 국가법령정보센터 웹 화면이 본문을 불러올 때 쓰는 요청(lsInfoR.do). 열쇠 없이 받을 수 있다.
  법제처 Open API(open.law.go.kr)는 신청과 서버 주소 등록이 끝나야 열리므로, 승인되면 이 모듈의 받는 부분만 바꾼다.
- 저장: 받은 원본(HTML)은 data/legal_raw/에 그대로 두고(깃에 올리지 않는다), 조문으로 나눈 결과만
  backend/app/legal_data/kr/<id>.json에 둔다. 결과에는 받은 주소, 받은 시각, 원본 해시, 시행일, 공포 정보를 남긴다.
- 받을 법령 목록: backend/app/legal_data/sources.json

실행(저장소 루트에서):
    .venv/Scripts/python.exe -X utf8 -m backend.app.fetch_legal             # 목록 전부
    .venv/Scripts/python.exe -X utf8 -m backend.app.fetch_legal 형사소송법    # 이름에 이 글자가 들어간 것만
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from backend.app.paths import APP_DIR, DATA_DIR


LEGAL_DIR = APP_DIR / "legal_data"
SOURCES_PATH = LEGAL_DIR / "sources.json"
OUT_DIR = LEGAL_DIR / "kr"
RAW_DIR = DATA_DIR / "legal_raw"

SITE = "https://www.law.go.kr"
USER_AGENT = "Mozilla/5.0 (compatible; PublicLawFetcher/1.0)"
REQUEST_INTERVAL_SEC = 1.0
TIMEOUT_SEC = 40.0
# 본문 요청의 고정 값: 한글 본문(010202), 공포문 미포함.
BODY_PARAMS = {"chrClsCd": "010202", "ancYnChk": "0"}

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
_SPACE = re.compile(r"[ \t\r\f\v ]+")
_NOTE = re.compile(r"\s*<[^<>]*\d{4}\.[^<>]*>")  # 장 제목 뒤의 '<개정 2010. 3. 31.>' 같은 표시
_LEVEL = re.compile(r"^제\s*\d+\s*(편|장|절|관)")
_LEVELS = ("편", "장", "절", "관")


class FetchError(RuntimeError):
    """법령을 받지 못했거나 화면 구조가 달라 조문을 나누지 못했다."""


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


def _date(dotted: str) -> str:
    """'2026. 10. 2.' → '2026-10-02'. 모양이 다르면 받은 글을 그대로 돌려준다."""
    numbers = re.findall(r"\d+", dotted)
    if len(numbers) != 3:
        return dotted.strip()
    return f"{int(numbers[0]):04d}-{int(numbers[1]):02d}-{int(numbers[2]):02d}"


def page_url(name: str) -> str:
    """법령 이름으로 여는 화면 주소(사람이 확인할 때 쓰는 주소이기도 하다)."""
    return f"{SITE}/{urllib.parse.quote('법령')}/{urllib.parse.quote(name.replace(' ', ''))}"


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
    """본문 HTML을 법령 정보와 조문 목록으로 나눈다. 부칙은 넣지 않는다."""
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
                level = _LEVEL.match(titles[0])
                depth = _LEVELS.index(level.group(1)) if level else 1
                for lower in _LEVELS[depth:]:
                    path.pop(lower, None)
                path[_LEVELS[depth]] = titles[0]
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


def fetch_law(source: dict) -> dict:
    """법령 하나를 받아 저장하고 요약 정보를 돌려준다."""
    name = source["name"]
    lsi_seq, ef_yd = resolve(name)
    time.sleep(REQUEST_INTERVAL_SEC)
    body = fetch_body(lsi_seq, ef_yd)
    raw = body.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    # 받은 글을 바이트 그대로 둔다(글로 쓰면 윈도우에서 줄바꿈이 바뀌어 해시와 맞지 않게 된다).
    (RAW_DIR / f"{source['id']}_{lsi_seq}.html").write_bytes(raw)
    parsed = parse_law(body)
    record = {
        "id": source["id"],
        "name": name,
        "title": parsed["title"] or name,
        "kind": source.get("kind", "법률"),
        "aliases": source.get("aliases", []),
        "group": source.get("group", "common"),
        "jurisdiction": "KR",
        "effective_date": parsed["effective_date"] or f"{ef_yd[:4]}-{ef_yd[4:6]}-{ef_yd[6:]}",
        "promulgation": parsed["promulgation"],
        "lsi_seq": lsi_seq,
        "source": "국가법령정보센터",
        "source_url": page_url(name),
        "fetched_from": f"{SITE}/LSW/lsInfoR.do?lsiSeq={lsi_seq}&efYd={ef_yd}",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "raw_sha256": digest,
        "provisions": parsed["provisions"],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{source['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return record


def main(argv: list[str]) -> int:
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["laws"]
    wanted = [s for s in sources if not argv or any(word in s["name"] for word in argv)]
    if not wanted:
        print("받을 법령이 없습니다(이름을 확인하세요).")
        return 1
    failed = 0
    for source in wanted:
        try:
            record = fetch_law(source)
        except Exception as exc:  # 하나가 실패해도 나머지는 받는다
            failed += 1
            print(f"실패  {source['name']}: {exc}")
        else:
            print(
                f"받음  {record['name']} · 시행 {record['effective_date']} · {record['promulgation']} · "
                f"조문 {len(record['provisions'])}개"
            )
        time.sleep(REQUEST_INTERVAL_SEC)
    print(f"끝: {len(wanted) - failed}개 받음, {failed}개 실패")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
