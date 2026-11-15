"""공식 기관이 새로 올린 보이스피싱 피해사례·경보 글 수집.

스케줄러가 하루 한 번 불러 금융감독원과 전기통신금융사기 통합대응단(경찰청)이 올린 최신 글을 평문으로
가져온다. 다음 단계가 글마다 "신종 수법" 카드 초안을 만들고 사람이 검토한다.

출처(source)
  - fss_case: 금융감독원 보이스피싱 피해사례 게시판. 본문은 요약 몇 줄이고 내용은 첨부 한글 문서에 있어
    .hwpx 첨부를 함께 읽는다(ZIP 속 XML이라 표준 라이브러리로 풀린다). 2026-11 기준 마지막 글은 2024-11이다.
  - fss_alert: 금융감독원 소비자경보 게시판. 본문에 경보 요약이 글자로 있다. 보도자료 원문은 PDF 첨부라 읽지
    않는다. 투자·카드·신탁 경보가 섞여 있어 제목 키워드로 피싱·사칭·사기·대출 글만 남긴다.
  - police_alert / police_scenario: 통합대응단(counterscam112.go.kr) 피싱 예·경보와 피싱 시나리오 게시판.
    예·경보는 대부분 카드뉴스 이미지뿐이고, 시나리오는 대개 글자 본문이 있다.

본문이 이미지뿐이라 글자가 거의 없는 글은 text를 ""로 돌려준다. 호출 측은 카드를 만들지 말고 url만
known_urls에 넣어 다음 실행에서 다시 받지 않게 한다. 내려받기에 실패했거나 본문 요소를 못 찾은 글은
돌려주지 않으므로(페이지 구조가 바뀐 경우 등) 다음 실행에서 다시 시도된다.

목록은 1쪽만 보고(통합대응단은 한 쪽에 6~8개), 다음 요청은 앞 요청이 끝나고 0.5초 뒤에 보낸다.
"""

from __future__ import annotations

import codecs
import html
import io
import logging
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from backend.app.services.official_cases import SOURCE_NAMES


logger = logging.getLogger(__name__)

# 연락처·개인 정보를 넣지 않은 일반 UA.
USER_AGENT = "Mozilla/5.0 (compatible; PublicNoticeFetcher/1.0)"
REQUEST_INTERVAL_SEC = 0.5
# 이보다 큰 응답은 잘라 읽는다(잘린 첨부 문서는 읽기 실패로 처리된다).
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 6000
# 글자가 이보다 적으면 이미지 설명 정도로 보고 본문이 없는 글로 친다.
MIN_TEXT_CHARS = 40
# 주제가 섞인 게시판에서 남길 제목 키워드. "피싱"은 보이스·메신저·레터피싱을, "사기"는 전화금융사기를 포함한다.
TOPIC_KEYWORDS = ("피싱", "스미싱", "사칭", "사기", "대출")

FSS = "https://www.fss.or.kr"
COUNTERSCAM = "https://www.counterscam112.go.kr"


@dataclass(frozen=True)
class Board:
    source: str
    list_url: str
    # {id} 자리에 글 번호가 들어가는 정식 주소. 목록 링크의 쪽 번호 등은 빼서 data/official/cases.json과 맞춘다.
    post_url: str
    # 목록의 글 링크(href나 onclick)에서 글 번호를 뽑는 패턴
    link_id: re.Pattern
    # 상세 페이지 본문 요소의 class
    body_class: str
    topic_filter: bool = False


BOARDS = (
    Board(
        "fss_case",
        f"{FSS}/fss/bbs/B0000059/list.do?menuNo=200359",
        f"{FSS}/fss/bbs/B0000059/view.do?nttId={{id}}&menuNo=200359",
        re.compile(r"view\.do\?nttId=(\d+)"),
        "n-dbdata",
    ),
    Board(
        "fss_alert",
        f"{FSS}/fss/bbs/B0000175/list.do?menuNo=200204",
        f"{FSS}/fss/bbs/B0000175/view.do?nttId={{id}}&menuNo=200204",
        re.compile(r"view\.do\?nttId=(\d+)"),
        "n-dbdata",
        topic_filter=True,
    ),
    Board(
        "police_alert",
        f"{COUNTERSCAM}/bbs003/board/boardList.do",
        f"{COUNTERSCAM}/bbs003/board/boardDetail.do?pstSn={{id}}",
        re.compile(r"fn_Detail\('(\d+)'\)"),
        "noticeViewArea",
    ),
    Board(
        "police_scenario",
        f"{COUNTERSCAM}/bbs009/board/boardList.do",
        f"{COUNTERSCAM}/bbs009/board/boardDetail.do?pstSn={{id}}",
        re.compile(r"fn_Detail\('(\d+)'\)"),
        "noticeViewArea",
    ),
)

_DATE = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})")
_BLOCK_TAGS = frozenset("p div br li tr table ul ol dl dt dd h1 h2 h3 h4 h5 h6 blockquote hr".split())
_CELL_TAGS = frozenset(("td", "th"))
# 통합대응단 예·경보 게시판은 본문 HTML을 이스케이프해 이 요소에 넣어 두고 화면에서 푼다.
_ESCAPED_BODY = re.compile(r"(<p\b[^>]*\bunescape-target\b[^>]*>)(.*?)(</p>)", re.S)
_ANCHOR = re.compile(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_HWPX = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
_HWPX_SECTION = re.compile(r"Contents/section(\d+)\.xml")
# 글자 사이에 끼는 OWPML 요소 가운데 공백·줄바꿈으로 읽을 것. 나머지(형광펜 표시 등)는 글자 없이 넘긴다.
_HWPX_INLINE = {f"{_HWPX}tab": " ", f"{_HWPX}nbSpace": " ", f"{_HWPX}fwSpace": " ", f"{_HWPX}lineBreak": "\n"}

_request_lock = threading.Lock()
_last_request_at = 0.0


def fetch_recent(max_per_source: int = 10, known_urls: set[str] | None = None, timeout: float = 15.0) -> list[dict]:
    """출처마다 목록 1쪽에서 known_urls에 없는 글을 최신순으로 최대 max_per_source개 가져온다.

    known_urls에 있는 글은 상세 페이지를 받지 않고 건너뛴다(개수에도 넣지 않는다).
    각 항목: {"source": "fss_case" 등, "source_name": 한국어 출처 이름, "url": 정식 상세 주소,
              "date": "YYYY-MM-DD" 또는 "", "title": 제목, "text": 본문 평문}.
    text는 문단마다 한 줄로 공백을 정리한 평문이고 최대 MAX_TEXT_CHARS자다. 본문이 이미지뿐이면 ""이다.
    예외를 던지지 않는다. 실패한 출처나 글은 경고 로그만 남기고 건너뛴다. timeout은 요청 하나의 제한 시간(초)이다.
    """
    known = known_urls or set()
    items: list[dict] = []
    for board in BOARDS:
        try:
            posts = _list_posts(board, timeout)
        except Exception as exc:
            logger.warning("%s 목록을 받지 못했습니다: %s", SOURCE_NAMES[board.source], exc)
            continue
        urls = [board.post_url.format(id=post["id"]) for post in posts]
        new_posts = [(post, url) for post, url in zip(posts, urls) if url not in known][: max(max_per_source, 0)]
        for post, url in new_posts:
            try:
                items.append(_fetch_post(board, post, url, timeout))
            except Exception as exc:
                logger.warning("%s 글을 받지 못했습니다(%s): %s", SOURCE_NAMES[board.source], url, exc)
    return items


def _list_posts(board: Board, timeout: float) -> list[dict]:
    """목록 1쪽의 글({"id", "title", "date"})을 최신순으로 돌려준다. 주제가 섞인 게시판은 제목으로 거른다."""
    parser = _ListParser(board.link_id)
    parser.feed(_get_page(board.list_url, timeout))
    parser.close()
    if not parser.posts:
        raise ValueError("목록에서 글을 찾지 못했습니다(페이지 구조가 바뀌었을 수 있음)")
    posts = [
        post
        for post in parser.posts.values()
        if not board.topic_filter or any(keyword in post["title"] for keyword in TOPIC_KEYWORDS)
    ]
    # 위에 고정된 글이 있어도 최신순이 되게 한다(같은 날짜는 목록 순서 유지).
    return sorted(posts, key=lambda post: post["date"], reverse=True)


def _fetch_post(board: Board, post: dict, url: str, timeout: float) -> dict:
    """상세 페이지 본문과 .hwpx 첨부의 글자를 합쳐 결과 항목을 만든다."""
    page = _ESCAPED_BODY.sub(
        lambda match: match.group(1) + html.unescape(match.group(2)) + match.group(3), _get_page(url, timeout)
    )
    parser = _BodyParser(board.body_class)
    parser.feed(page)
    parser.close()
    if not parser.found:
        raise ValueError("본문 요소를 찾지 못했습니다(페이지 구조가 바뀌었을 수 있음)")

    parts = [parser.text()]
    for link in _hwpx_links(page):
        if sum(len(part) for part in parts) >= MAX_TEXT_CHARS:
            break
        # 내려받기 실패는 글 실패로 올려 다음 실행에서 다시 받게 하고, 문서가 깨진 경우만 본문으로 대신한다.
        data, _ = _download(urljoin(url, link), timeout)
        try:
            parts.append(_hwpx_text(data))
        except (zipfile.BadZipFile, ET.ParseError) as exc:
            logger.warning("첨부 문서를 읽지 못했습니다(%s): %s", url, exc)
    text = "\n".join(part for part in parts if part)
    return {
        "source": board.source,
        "source_name": SOURCE_NAMES[board.source],
        "url": url,
        "date": post["date"],
        "title": post["title"],
        "text": text[:MAX_TEXT_CHARS] if len(text) >= MIN_TEXT_CHARS else "",
    }


def _download(url: str, timeout: float) -> tuple[bytes, str | None]:
    """요청 간격을 지켜 내려받고 (본문, 응답 헤더의 문자셋)을 돌려준다."""
    global _last_request_at
    with _request_lock:
        time.sleep(max(0.0, _last_request_at + REQUEST_INTERVAL_SEC - time.monotonic()))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(MAX_DOWNLOAD_BYTES), response.headers.get_content_charset()
        finally:
            _last_request_at = time.monotonic()


def _get_page(url: str, timeout: float) -> str:
    data, charset = _download(url, timeout)
    return _decode(data, charset)


def _decode(data: bytes, charset: str | None) -> str:
    """응답 헤더(없으면 meta 태그)의 문자셋으로 푼다. EUC-KR 계열은 확장 한글까지 담은 CP949로 읽는다."""
    if not charset:
        match = re.search(rb"charset=[\"']?([\w-]+)", data[:4096], re.I)
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        codec = codecs.lookup(charset).name
    except LookupError:
        codec = "cp949" if "949" in charset else "utf-8"
    return data.decode("cp949" if codec == "euc_kr" else codec, errors="replace")


def _normalize(text: str) -> str:
    """줄마다 연속 공백(nbsp 포함)을 하나로 줄이고 빈 줄을 뺀다."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _hwpx_links(page: str) -> list[str]:
    """파일 이름이 .hwpx인 첨부 링크(같은 주소는 한 번만, 게시 순서). 문서뷰어 링크는 이름이 없어 빠진다."""
    links: list[str] = []
    for href, label in _ANCHOR.findall(page):
        href = html.unescape(href)
        if ".hwpx" in label.lower() and href not in links:
            links.append(href)
    return links


def _hwpx_text(data: bytes) -> str:
    """한글 .hwpx(ZIP 속 OWPML XML)의 글자를 문단마다 한 줄로 꺼낸다. 표 안 문단도 문서 순서대로 들어간다."""
    lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        sections = sorted(
            (name for name in archive.namelist() if _HWPX_SECTION.fullmatch(name)),
            key=lambda name: int(_HWPX_SECTION.fullmatch(name).group(1)),
        )
        for name in sections:
            for paragraph in ET.fromstring(archive.read(name)).iter(f"{_HWPX}p"):
                parts = []
                # 바로 아래 run·t만 읽는다. 표 안 문단은 iter가 따로 돌려주므로 두 번 들어가지 않는다.
                for run in paragraph.findall(f"{_HWPX}run"):
                    for t in run.findall(f"{_HWPX}t"):
                        parts.append(t.text or "")
                        for child in t:
                            parts.append(_HWPX_INLINE.get(child.tag, ""))
                            parts.append(child.tail or "")
                lines.append("".join(parts))
    return _normalize("\n".join(lines))


class _ListParser(HTMLParser):
    """목록 페이지에서 글 번호·제목·등록일을 게시 순서대로 모은다.

    글 링크(href나 onclick에 글 번호가 있는 <a>)의 글자를 제목으로, 그 뒤 처음 나오는 날짜만 담긴 글자를
    등록일로 본다. 통합대응단 목록처럼 글자 없는 썸네일 링크와 제목 링크가 따로 있어도 된다.
    """

    def __init__(self, link_id: re.Pattern):
        super().__init__()
        self._link_id = link_id
        self.posts: dict[str, dict] = {}
        self._open_id: str | None = None
        self._open_text: list[str] = []
        self._last: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attributes = dict(attrs)
        match = self._link_id.search(f"{attributes.get('href') or ''} {attributes.get('onclick') or ''}")
        if match:
            self._open_id, self._open_text = match.group(1), []

    def handle_endtag(self, tag):
        if tag == "a" and self._open_id:
            title = " ".join("".join(self._open_text).split())
            if title:
                self._last = self.posts.setdefault(self._open_id, {"id": self._open_id, "title": title, "date": ""})
            self._open_id = None

    def handle_data(self, data):
        if self._open_id:
            self._open_text.append(data)
        elif self._last is not None and not self._last["date"]:
            match = _DATE.fullmatch(data.strip())
            if match:
                self._last["date"] = "-".join(match.groups())


class _BodyParser(HTMLParser):
    """class에 body_class가 있는 첫 요소 안의 글자를 문단마다 한 줄로 모은다(script·style 제외)."""

    def __init__(self, body_class: str):
        super().__init__()
        self._body_class = body_class
        self.found = False
        self._tag = ""
        # 본문 요소와 같은 태그가 열린 깊이. 0으로 돌아오면 본문이 끝난 것이다.
        self._depth = 0
        self._in_script = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if not self.found:
            if self._body_class in (dict(attrs).get("class") or "").split():
                self.found, self._tag, self._depth = True, tag, 1
            return
        if not self._depth:
            return
        if tag == self._tag:
            self._depth += 1
        self._in_script = tag in ("script", "style")
        self._chunks.append("\n" if tag in _BLOCK_TAGS else " " if tag in _CELL_TAGS else "")

    def handle_endtag(self, tag):
        if not self._depth:
            return
        if tag == self._tag:
            self._depth -= 1
        self._in_script = False
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._depth and not self._in_script:
            self._chunks.append(data)

    def text(self) -> str:
        return _normalize("".join(self._chunks))
