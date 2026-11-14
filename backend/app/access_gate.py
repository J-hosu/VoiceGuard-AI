"""행사·시연용 접속 코드 확인.

ACCESS_CODE 환경변수가 설정돼 있으면 접속 코드를 아는 사람만 웹 앱과 API를 쓸 수 있다.
(비어 있으면 아무 제한 없이 동작한다. 로컬 개발 기본값.)

  - QR 주소  https://<host>/?code=<코드>  로 들어오면 쿠키를 심고 코드를 뺀 주소로 돌려보낸다.
  - 쿠키가 없는 채로 페이지를 열면 코드 입력 화면을 보여준다(홈 화면에 추가한 앱이 쿠키를
    따로 쓰는 아이폰 대비).
  - API·WebSocket은 쿠키, X-Access-Code 헤더, code 쿼리 중 하나가 맞아야 통과한다.

WebSocket까지 막아야 하므로 Starlette BaseHTTPMiddleware가 아닌 순수 ASGI 미들웨어로 만든다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode


COOKIE_NAME = "vg_access"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
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

GATE_PAGE = """<!doctype html>
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
button{width:100%;margin-top:14px;padding:14px;font-size:16px;font-weight:700;border:0;border-radius:12px;background:#2563EB;color:#fff}
.err{color:#F87171;font-size:13px;margin:12px 0 0;min-height:18px}
</style></head><body><form class="card" method="post" action="/access">
<h1>VoiceGuard AI</h1><p>시연 참가자용 접속 코드를 입력해 주세요.</p>
<input name="code" inputmode="text" autocomplete="off" autocapitalize="characters" aria-label="접속 코드" autofocus required>
<input type="hidden" name="next" value="__NEXT__">
<button type="submit">들어가기</button><div class="err">__ERROR__</div></form></body></html>"""


def access_code() -> str:
    return os.environ.get("ACCESS_CODE", "").strip()


def _token(code: str) -> str:
    """쿠키에 넣는 값. 코드 원문 대신 해시를 저장한다."""
    return hashlib.sha256(f"voiceguard:{code}".encode("utf-8")).hexdigest()


def _matches(candidate: str, code: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.strip().upper(), code.upper())


def _safe_next(path: str) -> str:
    """리다이렉트 대상은 이 사이트 안의 경로만 허용한다."""
    return path if path.startswith("/") and not path.startswith("//") else "/"


class AccessGateMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        code = access_code()
        if not code or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        cookies = SimpleCookie(headers.get("cookie", ""))
        has_cookie = COOKIE_NAME in cookies and hmac.compare_digest(cookies[COOKIE_NAME].value, _token(code))
        query_code = (query.get("code") or [""])[0]
        authorized = has_cookie or _matches(headers.get("x-access-code", ""), code) or _matches(query_code, code)

        if scope["type"] == "websocket":
            if authorized:
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 4401})
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS" or (path in OPEN_PATHS and not (path == "/access" and method == "POST")):
            if path == "/access":
                await self._html(send, GATE_PAGE.replace("__NEXT__", "/").replace("__ERROR__", ""))
                return
            await self.app(scope, receive, send)
            return

        if path == "/access" and method == "POST":
            await self._handle_submit(receive, send, code)
            return

        wants_page = method == "GET" and "text/html" in headers.get("accept", "")
        if authorized:
            if wants_page and query_code:
                # QR로 들어온 첫 요청: 쿠키를 심고 주소창에서 코드를 지운다.
                rest = {key: values for key, values in query.items() if key != "code"}
                target = path + ("?" + urlencode(rest, doseq=True) if rest else "")
                await self._redirect(send, target, set_cookie=_token(code))
                return
            await self.app(scope, receive, send)
            return

        if wants_page:
            await self._html(send, GATE_PAGE.replace("__NEXT__", _safe_next(path)).replace("__ERROR__", ""), status=401)
            return
        await self._json(send, 401, b'{"detail":"\\uc811\\uc18d \\ucf54\\ub4dc\\uac00 \\ud544\\uc694\\ud569\\ub2c8\\ub2e4."}')

    async def _handle_submit(self, receive, send, code: str) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body") or len(body) > 4096:
                break
        form = parse_qs(body.decode("utf-8", errors="ignore"))
        submitted = (form.get("code") or [""])[0]
        target = _safe_next((form.get("next") or ["/"])[0])
        if _matches(submitted, code):
            await self._redirect(send, target, set_cookie=_token(code))
            return
        page = GATE_PAGE.replace("__NEXT__", target).replace("__ERROR__", "접속 코드가 맞지 않습니다.")
        await self._html(send, page, status=401)

    @staticmethod
    async def _html(send, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _json(send, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _redirect(send, target: str, set_cookie: str) -> None:
        cookie = f"{COOKIE_NAME}={set_cookie}; Max-Age={COOKIE_MAX_AGE}; Path=/; HttpOnly; Secure; SameSite=Lax"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", target.encode("latin-1", errors="ignore")),
                    (b"set-cookie", cookie.encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})
