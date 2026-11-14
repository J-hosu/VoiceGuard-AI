"""VoiceGuard AI FastAPI 애플리케이션 진입점."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

from backend.app.access_gate import AccessGateMiddleware
from backend.app.api.agent_routes import router as agent_router
from backend.app.api.investigation_routes import router as investigation_router
from backend.app.api.routes import router as rest_router
from backend.app.api.websocket import router as websocket_router
from backend.app.database import init_db
from backend.app.paths import ENV_PATH, PROJECT_ROOT
from backend.app.services import agent_loop
from backend.app.services.call_analyzer import preload_koelectra_model


if load_dotenv:
    load_dotenv(ENV_PATH)

logger = logging.getLogger(__name__)


DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://localhost:8081",
    "http://localhost:8082",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:8081",
    "http://127.0.0.1:8082",
]


def _get_cors_origins() -> list[str]:
    """CORS를 허용할 프론트엔드 origin 목록을 기본값과 환경변수에서 읽는다."""
    raw_origins = os.getenv("CORS_ALLOW_ORIGINS", "")
    env_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return list(dict.fromkeys(DEFAULT_CORS_ORIGINS + env_origins))


def _get_cors_allow_credentials(cors_origins: list[str]) -> bool:
    """와일드카드 Origin을 쓰는 경우에는 브라우저 규칙상 credentials를 끈다."""
    return "*" not in cors_origins


async def preload_whisper_model() -> None:
    """서버 시작 시 Whisper STT 모델을 미리 로드한다. 실패해도 서버는 계속 뜬다."""
    try:
        from backend.app.mp3_json import load_model

        await asyncio.to_thread(load_model)
    except Exception as exc:
        logger.warning("Whisper 모델 사전 로드 실패. 첫 전사 요청 때 다시 시도합니다: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """API 서버 시작 시 DB와 모델 리소스를 준비한다."""
    init_db()
    await preload_koelectra_model()
    await preload_whisper_model()
    # 구조화 출력 스키마 준비 호출은 수십 초가 걸릴 수 있어 서버 시작을 막지 않고 뒤에서 돌린다.
    warm_up_task = asyncio.create_task(asyncio.to_thread(agent_loop.warm_up))
    yield
    warm_up_task.cancel()


app = FastAPI(
    title="Voice Phishing RAG Detection API",
    description="실시간 통화 음성을 분석해 보이스피싱 위험도와 핵심근거를 생성",
    version="0.1.0",
    lifespan=lifespan,
)
cors_origins = _get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=_get_cors_allow_credentials(cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(rest_router)
app.include_router(agent_router)
app.include_router(investigation_router)
app.include_router(websocket_router)
# 접속 코드 확인(ACCESS_CODE가 설정됐을 때만 동작). 가장 바깥에서 HTTP와 WebSocket을 모두 거른다.
app.add_middleware(AccessGateMiddleware)


# 웹 앱(Expo 웹 빌드 결과)을 API와 같은 주소에서 제공한다. 같은 출처라 CORS 설정이 필요 없고
# 브라우저 마이크가 요구하는 HTTPS도 터널 주소 하나로 해결된다. 폴더가 없으면 API만 동작한다.
WEB_DIST_DIR = Path(os.getenv("WEB_DIST_DIR") or PROJECT_ROOT / "web_dist").resolve()

if WEB_DIST_DIR.is_dir():

    def _web_file(relative: str) -> Path | None:
        candidate = (WEB_DIST_DIR / relative).resolve()
        if candidate.is_file() and WEB_DIST_DIR in candidate.parents:
            return candidate
        return None

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_web_app(full_path: str) -> FileResponse:
        """정적 파일이 있으면 그 파일을, 없으면 앱 화면(index.html)을 돌려준다(화면 이동은 앱이 처리)."""
        target = _web_file(full_path) if full_path else None
        if target is None and full_path:
            target = _web_file(f"{full_path}.html")
        if target is None:
            target = WEB_DIST_DIR / "index.html"
        # 파일명에 해시가 붙는 번들은 오래 캐시하고, 화면 파일은 새 배포가 바로 보이게 캐시하지 않는다.
        hashed = "/_expo/static/" in target.as_posix() or "/assets/" in target.as_posix()
        cache = "public, max-age=31536000, immutable" if hashed else "no-cache"
        return FileResponse(target, headers={"Cache-Control": cache})
