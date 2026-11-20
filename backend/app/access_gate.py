"""행사·시연용 접속 코드 확인.

ACCESS_CODE 환경변수가 설정돼 있으면 접속 코드를 아는 사람만 웹 앱과 API를 쓸 수 있다.
(비어 있으면 아무 제한 없이 동작한다. 로컬 개발 기본값.)

  - QR 주소  https://<host>/?code=<코드>  로 들어오면 쿠키를 심고 코드를 뺀 주소로 돌려보낸다.
  - 쿠키가 없는 채로 페이지를 열면 코드 입력 화면을 보여준다(홈 화면에 추가한 앱이 쿠키를
    따로 쓰는 아이폰 대비).
  - API·WebSocket은 쿠키, X-Access-Code 헤더, code 쿼리 중 하나가 맞아야 통과한다.

ADMIN_CODE도 설정하면 관리 경로(is_admin_path: 대시보드, 수사·품질·평가·수법 카드·학습 사례 API,
API 문서)는 접속 코드만으로는 열리지 않고 관리 코드가 따로 있어야 한다.

  - 관리 경로 페이지를 열면 관리 코드 입력 화면을 보여주고, 맞으면 vg_admin 쿠키를 심는다.
    관리 코드는 주소(쿼리)로 받지 않는다. 주소는 서버 접속 기록과 브라우저 기록에 남기 때문이다.
  - API는 vg_admin 쿠키나 X-Admin-Code 헤더(n8n)가 맞아야 통과한다.
  - 관리 코드로는 참가자 기능도 쓸 수 있다(대시보드가 /calls/{id}/report를 연다).
  - ADMIN_CODE가 비어 있으면 예전처럼 접속 코드 하나로 관리 경로까지 열린다(서버 시작 때 경고 한 줄,
    API 문서는 main.py가 api_docs_settings()로 끈다). 이때는 X-Admin-Code 헤더에 접속 코드를 넣어도 통과한다
    (n8n 템플릿을 그대로 쓸 수 있게).

틀린 관리 코드 제출(입력 화면·헤더)은 접속 IP마다 1분에 10번까지만 받고, 넘으면 429로 잠깐 막는다.
참가자 접속 코드는 횟수를 세지 않는다(행사장처럼 여러 명이 같은 IP를 쓰면 다른 사람이 틀린 탓에 막힐 수 있다).
한글 자판으로 친 코드처럼 맞을 수 없는 입력도 세지 않는다. 쿠키가 맞는 요청은 막지 않는다.
코드 비교는 UTF-8 바이트로 해서 한글 자판으로 친 코드도 오류 없이 '맞지 않음'으로 처리한다.

WebSocket까지 막아야 하므로 Starlette BaseHTTPMiddleware가 아닌 순수 ASGI 미들웨어로 만든다.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlencode


COOKIE_NAME = "vg_access"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
ADMIN_COOKIE_NAME = "vg_admin"
ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 7
# 코드 없이도 열려 있어야 하는 경로(상태 확인, 코드 제출, 앱 아이콘).
# 앱 아이콘과 manifest는 브라우저가 쿠키 없이 가져가므로(홈 화면에 추가할 때) 열어 둔다.
OPEN_PATHS = {
    "/health",
    "/access",
    "/manifest.json",
    "/favicon.ico",
    "/icon-192.png",
    "/icon-512.png",
    "/apple-touch-icon.png",
}
# 관리 코드가 있어야 하는 경로. 경로가 같거나 '<앞부분>/'로 시작하면 해당한다.
ADMIN_PREFIXES = (
    "/dashboard",
    "/investigation",
    "/guidance",
    "/legal",
    "/quality",
    "/eval",
    "/knowledge",
    "/training-cases",
    "/docs",
    "/redoc",
)
ADMIN_PATHS = {"/openapi.json"}

# 틀린 관리 코드 제출 제한: 접속 IP마다 FAILURE_WINDOW_SEC초에 MAX_FAILURES번. 참가자 접속 코드는 세지 않는다.
MAX_FAILURES = 10
FAILURE_WINDOW_SEC = 60.0
MAX_TRACKED_CLIENTS = 10000
LIMITED_KINDS = ("admin",)
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

MSG_NEED_ACCESS = "접속 코드가 필요합니다."
MSG_NEED_ADMIN = "관리 코드가 필요합니다."
MSG_TOO_MANY = "틀린 코드 입력이 많아 잠시 막았습니다. 1분 뒤에 다시 입력해 주세요."
MSG_KEYBOARD = "영문 자판으로 바꿔 다시 입력해 주세요."

# 입력 칸: 한글 자판이 덜 뜨도록 접속 코드는 이메일 자판(inputmode=email), 관리 코드는 비밀번호 칸을 쓴다.
_FORMS = {
    "access": {
        "lead": "시연 참가자용 접속 코드를 입력해 주세요.",
        "input": (
            '<input name="code" type="text" inputmode="email" autocomplete="off" autocorrect="off" '
            'autocapitalize="characters" spellcheck="false" aria-label="접속 코드" '
            'aria-describedby="code-hint" autofocus required>'
        ),
        "wrong": "접속 코드가 맞지 않습니다.",
        "back": "",
    },
    "admin": {
        "lead": "관리 화면입니다. 관리 코드를 입력해 주세요.",
        "input": (
            '<input name="code" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" '
            'aria-label="관리 코드" aria-describedby="code-hint" autofocus required>'
        ),
        "wrong": "관리 코드가 맞지 않습니다.",
        "back": '<a class="back" href="/">앱으로 돌아가기</a>',
    },
}

_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0B1220"><title>VoiceGuard AI</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0B1220;color:#E5ECF6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;padding:24px}
.card{width:100%;max-width:360px;background:#131C2E;border:1px solid #243049;border-radius:20px;padding:28px 24px}
h1{font-size:20px;margin:0 0 6px}p{font-size:14px;line-height:1.6;color:#9FB0C8;margin:0 0 20px}
input{width:100%;font-size:18px;letter-spacing:2px;text-align:center;padding:14px;border-radius:12px;border:1px solid #2F3E5C;background:#0B1220;color:#fff}
input:focus{outline:2px solid #3B82F6;outline-offset:1px}
.hint{font-size:13px;text-align:center;margin:10px 0 0}
button{width:100%;margin-top:14px;padding:14px;font-size:16px;font-weight:700;border:0;border-radius:12px;background:#2563EB;color:#fff}
.err{color:#F87171;font-size:13px;margin:12px 0 0;min-height:18px}
.back{display:block;margin-top:12px;text-align:center;font-size:13px;color:#9FB0C8}
</style></head><body><form class="card" method="post" action="/access">
<h1>VoiceGuard AI</h1><p>__LEAD__</p>
__INPUT__
<p class="hint" id="code-hint">영문 자판으로 입력해 주세요.</p>
<input type="hidden" name="kind" value="__KIND__">
<input type="hidden" name="next" value="__NEXT__">
<button type="submit">들어가기</button><div class="err" role="alert">__ERROR__</div>__BACK__</form>
<script>(function(){var n=document.querySelector('input[name="next"]');if(n&&location.hash&&n.value.indexOf('#')<0)n.value+=location.hash;})();</script>
</body></html>"""


def access_code() -> str:
    return os.environ.get("ACCESS_CODE", "").strip()


def admin_code() -> str:
    return os.environ.get("ADMIN_CODE", "").strip()


def is_admin_path(path: str) -> bool:
    """관리 코드가 있어야 하는 경로인지."""
    if path in ADMIN_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in ADMIN_PREFIXES)


def api_docs_settings() -> dict:
    """FastAPI 문서(/docs, /redoc, /openapi.json) 설정. 접속 코드가 걸린 공개 서버에서는 참가자에게 열지 않는다.

    ADMIN_CODE도 있으면 문서는 켜 두고 관리 경로로 막는다. 접속 코드만 있으면 문서를 끈다. 둘 다 없으면(로컬 개발) 그대로.
    """
    if access_code() and not admin_code():
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


def startup_warning() -> Optional[str]:
    """서버 시작 때 남길 경고. 관리 경로가 접속 코드와 따로 보호되지 않으면 그 사실을 알린다."""
    code, admin = access_code(), admin_code()
    if not code:
        if admin:
            return "ACCESS_CODE가 비어 있어 접속 확인이 꺼져 있습니다. ADMIN_CODE만으로는 관리 화면이 보호되지 않고 웹 앱과 모든 API가 코드 없이 열립니다."
        return "ACCESS_CODE와 ADMIN_CODE가 비어 있어 웹 앱과 모든 API가 코드 없이 열립니다(로컬 개발용)."
    if not admin:
        return "ADMIN_CODE가 비어 있어 관리 화면(대시보드·수사·평가·수법 카드·학습 사례)도 접속 코드(ACCESS_CODE) 하나로 열립니다. API 문서는 꺼 둡니다."
    if _matches(admin, code):
        return "ADMIN_CODE가 ACCESS_CODE와 같아 관리 화면이 따로 보호되지 않습니다."
    return None


def _bytes(value: str) -> bytes:
    return value.encode("utf-8", "surrogatepass")


def _same(a: str, b: str) -> bool:
    """예외 없이 비교한다. hmac.compare_digest는 ASCII가 아닌 str(한글 등)이면 TypeError를 내므로 바이트로 바꾼다."""
    return hmac.compare_digest(_bytes(a), _bytes(b))


def _matches(candidate: str, code: str) -> bool:
    """제출된 코드가 맞는지. 앞뒤 공백과 대소문자는 무시한다. 한글이 섞이면 그냥 '맞지 않음'이다."""
    candidate = (candidate or "").strip()
    return bool(candidate) and bool(code) and _same(candidate.upper(), code.upper())


def _could_match(candidate: str, code: str) -> bool:
    """틀린 횟수로 셀 입력인지. 비었거나, 코드가 ASCII인데 한글 등이 섞여 애초에 맞을 수 없는 입력은 세지 않는다."""
    candidate = (candidate or "").strip()
    return bool(candidate) and (candidate.isascii() or not code.isascii())


def _token(code: str, kind: str = "access") -> str:
    """쿠키에 넣는 값. 코드 원문 대신 해시를 저장한다(접속 코드 쿠키 값은 예전과 같다)."""
    prefix = "voiceguard-admin" if kind == "admin" else "voiceguard"
    return hashlib.sha256(_bytes(f"{prefix}:{code}")).hexdigest()


def _read_headers(scope: dict) -> dict[str, str]:
    """요청 헤더(소문자 이름 → 값). Cookie가 여러 줄로 나뉘어 오면(HTTP/2에서 허용) 하나로 잇는다."""
    headers: dict[str, str] = {}
    for key, value in scope.get("headers", []):
        name, text = key.decode("latin-1").lower(), value.decode("latin-1")
        if name == "cookie" and name in headers:
            headers[name] += "; " + text
        else:
            headers[name] = text
    return headers


def _read_cookies(raw: str) -> dict[str, list[str]]:
    """Cookie 헤더를 이름별 값 목록으로 나눈다.

    http.cookies.SimpleCookie는 이름이 이상한 쿠키가 하나만 섞여도 전부 버리거나 CookieError를 내서 직접 나눈다.
    """
    cookies: dict[str, list[str]] = {}
    for part in raw.split(";"):
        name, sep, value = part.partition("=")
        name = name.strip()
        if sep and name:
            cookies.setdefault(name, []).append(value.strip().strip('"'))
    return cookies


def _first(values: dict[str, list[str]], key: str) -> str:
    return (values.get(key) or [""])[0]


def _client_key(scope: dict, headers: dict[str, str]) -> str:
    """제출 횟수를 셀 접속 IP. Cloudflare 터널·SSH 포워딩으로 들어오면 상대가 이 서버 자신이라
    Cloudflare가 붙이는 CF-Connecting-IP를 쓴다(그 경우에만 믿는다)."""
    client = scope.get("client")
    host = str(client[0]) if client else ""
    if not host or host in _LOOPBACK:
        forwarded = headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            return forwarded[:64]
    return host or "unknown"


def _safe_next(path: str) -> str:
    """리다이렉트 대상은 이 사이트 안의 경로만 허용한다."""
    if not path.startswith("/") or path.startswith("//") or path.startswith("/\\"):
        return "/"
    return path


def _location(target: str) -> bytes:
    """Location 헤더 값. 한글·제어 문자는 퍼센트 인코딩해 헤더가 깨지거나 끼어들지 않게 한다."""
    return quote(target, safe="/?#&=%:@!$'()*+,;~[]").encode("ascii")


def gate_page(kind: str, next_path: str, error: str = "") -> str:
    """코드 입력 화면. kind는 'access'(참가자) 또는 'admin'(관리).

    next_path는 요청 주소에서 오므로 HTML 이스케이프하고, 그 안에 든 글자가 다시 치환되지 않게 맨 마지막에 넣는다.
    """
    form = _FORMS["admin" if kind == "admin" else "access"]
    return (
        _PAGE.replace("__LEAD__", form["lead"])
        .replace("__INPUT__", form["input"])
        .replace("__KIND__", "admin" if kind == "admin" else "access")
        .replace("__BACK__", form["back"])
        .replace("__ERROR__", html.escape(error))
        .replace("__NEXT__", html.escape(_safe_next(next_path), quote=True))
    )


class FailureLimiter:
    """틀린 코드 제출을 (코드 종류, 접속 IP)마다 세어, 기준을 넘으면 잠깐 막는다. 서버 메모리에만 둔다.

    막힌 동안 들어온 제출은 세지 않으므로 마지막으로 센 실패부터 길어야 window초 뒤에 풀린다.
    """

    def __init__(
        self,
        limit: int = MAX_FAILURES,
        window: float = FAILURE_WINDOW_SEC,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._pruned_at = float("-inf")

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        stamps = [stamp for stamp in self._failures.get(key, ()) if now - stamp < self.window]
        if stamps:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return stamps

    def retry_after(self, kind: str, client: str) -> int:
        """막혀 있으면 풀릴 때까지 남은 초(1 이상), 막히지 않았으면 0."""
        now = self.clock()
        stamps = self._recent((kind, client), now)
        if len(stamps) < self.limit:
            return 0
        return max(1, math.ceil(stamps[-self.limit] + self.window - now))

    def record(self, kind: str, client: str) -> None:
        now = self.clock()
        if len(self._failures) >= MAX_TRACKED_CLIENTS:
            # 메모리 상한: 지난 기록은 window마다 한 번만 훑어 지우고, 그래도 많으면 가장 오래된 것부터 버린다.
            if now - self._pruned_at >= self.window:
                self._pruned_at = now
                for key in list(self._failures):
                    self._recent(key, now)
            while len(self._failures) >= MAX_TRACKED_CLIENTS:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault((kind, client), []).append(now)


class _Check:
    """한 종류의 코드(접속·관리)를 확인한 결과."""

    __slots__ = ("ok", "retry_after")

    def __init__(self, ok: bool = False, retry_after: int = 0):
        self.ok = ok
        self.retry_after = retry_after


class AccessGateMiddleware:
    def __init__(self, app, limiter: Optional[FailureLimiter] = None):
        self.app = app
        self.limiter = limiter or FailureLimiter()

    async def __call__(self, scope, receive, send):
        code = access_code()
        if not code or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        admin = admin_code()
        path = scope.get("path", "")
        headers = _read_headers(scope)
        client = _client_key(scope, headers)
        is_ws = scope["type"] == "websocket"
        method = "GET" if is_ws else scope.get("method", "GET")

        if not is_ws:
            if method == "OPTIONS" or (path in OPEN_PATHS and not (path == "/access" and method == "POST")):
                if path == "/access":
                    await self._html(send, gate_page("access", "/"))
                    return
                await self.app(scope, receive, send)
                return
            if path == "/access" and method == "POST":
                await self._handle_submit(receive, send, code, admin, client)
                return

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        cookies = _read_cookies(headers.get("cookie", ""))
        admin_path = bool(admin) and is_admin_path(path)
        passed: list[str] = []  # 맞은 코드 종류(쿠키를 심을 것)

        if admin:
            admin_check = self._check(
                "admin", admin, cookies.get(ADMIN_COOKIE_NAME, []),
                [headers.get("x-admin-code", "")], client,
            )
            if admin_check.ok:
                passed.append("admin")
            checks = [admin_check]
            if not admin_path:
                access_check = self._check(
                    "access", code, cookies.get(COOKIE_NAME, []),
                    [headers.get("x-access-code", ""), _first(query, "code")], client,
                )
                if access_check.ok:
                    passed.append("access")
                checks.append(access_check)
        else:
            # 관리 코드가 없으면 예전처럼 접속 코드 하나로 모두 연다. X-Admin-Code에 접속 코드를 넣어도 통과한다.
            access_check = self._check(
                "access", code, cookies.get(COOKIE_NAME, []),
                [headers.get("x-access-code", ""), _first(query, "code"), headers.get("x-admin-code", "")], client,
            )
            if access_check.ok:
                passed.append("access")
            checks = [access_check]

        authorized = bool(passed)
        retry_after = 0 if authorized else max(check.retry_after for check in checks)
        need_kind = "admin" if admin_path else "access"

        if is_ws:
            if authorized:
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 4429 if retry_after else 4401})
            return

        wants_page = method == "GET" and "text/html" in headers.get("accept", "")
        if authorized:
            if wants_page and "code" in query:
                # QR 링크로 들어온 첫 요청: 쿠키를 심고 주소창에서 코드를 지운다. 경로가 //로 시작하면 다른 사이트로 가므로 막는다.
                rest = {key: values for key, values in query.items() if key != "code"}
                target = _safe_next(path) + ("?" + urlencode(rest, doseq=True) if rest else "")
                await self._redirect(send, target, [(kind, admin if kind == "admin" else code) for kind in passed])
                return
            await self.app(scope, receive, send)
            return

        limited = [(b"retry-after", str(retry_after).encode())] if retry_after else []
        if wants_page:
            error = MSG_TOO_MANY if retry_after else ""
            await self._html(send, gate_page(need_kind, path, error), status=429 if retry_after else 401, extra=limited)
            return
        if retry_after:
            await self._json(send, 429, MSG_TOO_MANY, limited)
            return
        await self._json(send, 401, MSG_NEED_ADMIN if admin_path else MSG_NEED_ACCESS)

    def _check(self, kind: str, expected: str, cookie_values: list[str], tries: list[str], client: str) -> _Check:
        """쿠키가 맞으면 통과. 아니면 제출된 코드(헤더·쿼리)를 비교하고, 틀리면 실패로 센다."""
        token = _token(expected, kind)
        if any(_same(value, token) for value in cookie_values):
            return _Check(ok=True)
        tries = [value for value in tries if value.strip()]
        if not tries:
            return _Check()
        limited = kind in LIMITED_KINDS
        wait = self.limiter.retry_after(kind, client) if limited else 0
        if wait:
            return _Check(retry_after=wait)
        if any(_matches(value, expected) for value in tries):
            return _Check(ok=True)
        if limited and any(_could_match(value, expected) for value in tries):
            self.limiter.record(kind, client)
        return _Check()

    async def _handle_submit(self, receive, send, code: str, admin: str, client: str) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body") or len(body) > 4096:
                break
        form = parse_qs(body.decode("utf-8", errors="ignore"))
        submitted = _first(form, "code")
        target = _safe_next(_first(form, "next") or "/")
        kind = "admin" if admin and _first(form, "kind") == "admin" else "access"
        expected = admin if kind == "admin" else code

        limited = kind in LIMITED_KINDS
        wait = self.limiter.retry_after(kind, client) if limited else 0
        if wait:
            await self._html(send, gate_page(kind, target, MSG_TOO_MANY), status=429, extra=[(b"retry-after", str(wait).encode())])
            return
        if _matches(submitted, expected):
            await self._redirect(send, target, [(kind, expected)])
            return
        if limited and _could_match(submitted, expected):
            self.limiter.record(kind, client)
        error = _FORMS[kind]["wrong"]
        if not submitted.isascii():
            error += " " + MSG_KEYBOARD
        await self._html(send, gate_page(kind, target, error), status=401)

    @staticmethod
    async def _html(send, page: str, status: int = 200, extra: Optional[list] = None) -> None:
        body = page.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    *(extra or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _json(send, status: int, detail: str, extra: Optional[list] = None) -> None:
        body = json.dumps({"detail": detail}).encode("ascii")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *(extra or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _redirect(send, target: str, cookies: list[tuple[str, str]]) -> None:
        """target으로 보내며 맞힌 코드 종류마다 쿠키를 심는다. cookies는 (종류, 코드) 목록."""
        headers = [(b"location", _location(target)), (b"cache-control", b"no-store"), (b"content-length", b"0")]
        for kind, value in cookies:
            name, max_age = (ADMIN_COOKIE_NAME, ADMIN_COOKIE_MAX_AGE) if kind == "admin" else (COOKIE_NAME, COOKIE_MAX_AGE)
            cookie = f"{name}={_token(value, kind)}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax"
            headers.append((b"set-cookie", cookie.encode("latin-1")))
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
