"""Slack이 부르는 주소. docs/slack.md 참고.

- POST /slack/commands      `/vg …` 명령
- POST /slack/interactions  버튼 누름과 입력 양식 제출
- POST /slack/events        봇을 부른 글(Events API)

Slack은 쿠키나 접속 코드를 보낼 수 없으므로 접속 코드 게이트는 이 경로를 지나가게 두고,
여기서 요청마다 Slack 서명을 확인한다. 서명이 맞지 않으면 아무 일도 하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from backend.app.services import slack_bot

router = APIRouter()


async def _verified_body(request: Request) -> bytes:
    if not slack_bot.is_configured():
        raise HTTPException(status_code=503, detail="Slack 연동이 설정되지 않았습니다(SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET).")
    body = await request.body()
    headers = request.headers
    if not slack_bot.verify_signature(headers.get("x-slack-request-timestamp", ""), body, headers.get("x-slack-signature", "")):
        raise HTTPException(status_code=401, detail="Slack 서명이 맞지 않습니다.")
    return body


def _form(body: bytes) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items()}


def _reply(payload: Optional[dict]) -> Response:
    """돌려줄 내용이 없으면 빈 200(Slack은 이것을 '받았다'로 본다)."""
    return JSONResponse(payload) if payload else Response(status_code=200)


@router.post("/slack/commands")
async def slack_commands(request: Request) -> Response:
    """`/vg 사건 51`, `/vg 법령 …`, `/vg 입력 51`, `/vg 기한`."""
    form = _form(await _verified_body(request))
    return _reply(await asyncio.to_thread(slack_bot.handle_command, form))


@router.post("/slack/interactions")
async def slack_interactions(request: Request) -> Response:
    """'사건 사실 입력' 버튼과 입력 양식 제출."""
    form = _form(await _verified_body(request))
    try:
        payload = json.loads(form.get("payload") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="payload를 읽지 못했습니다.") from None
    jobs: list[tuple] = []
    reply = await asyncio.to_thread(slack_bot.handle_interaction, payload, lambda function, *args: jobs.append((function, args)))
    for function, args in jobs:  # 채널에 올리는 일은 답한 뒤에 한다(Slack에는 3초 안에 답해야 한다)
        slack_bot.run_in_background(function, *args)
    return _reply(reply)


@router.post("/slack/events")
async def slack_events(request: Request) -> Response:
    """봇을 부른 글. 3초 안에 받았다고 답하고, 도우미의 답은 뒤에서 스레드에 단다."""
    body = await _verified_body(request)
    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="본문을 읽지 못했습니다.") from None
    reply, mention = slack_bot.handle_event(payload)
    if mention is not None:  # 같은 알림을 다시 보낸 것은 handle_event가 걸러 낸다
        slack_bot.submit_mention(mention)
    return _reply(reply)
