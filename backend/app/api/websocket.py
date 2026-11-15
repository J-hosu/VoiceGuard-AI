"""실시간 통화 오디오 분석 WebSocket 라우터."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from backend.app import state_repository
from backend.app.api.response_logger import log_api_request, log_api_response
from backend.app.repository import create_call_log, get_call_log, insert_call_message, list_call_messages
from backend.app.schemas import CallLogCreate, CallMessageCreate
from backend.app.services import evidence
from backend.app.services import agent_loop, audio_transcriber, call_analyzer, live_refine, response_agent, rtzr_client


logger = logging.getLogger(__name__)
router = APIRouter()

# 연결이 끊긴 뒤에도 끝까지 돌아야 하는 마무리 작업(통화 종료 처리)의 참조를 잡아 둔다.
_background_tasks: set[asyncio.Task] = set()

# 통화 중 정리(refine, STT_REFINE=rtzr): 예약 확인 간격, 요청 하나의 상한(보통 2초), 연속 실패 허용 횟수,
# 통화 종료 정리 상한(넘으면 건너뛴다. 통화 후 전체 재전사가 어차피 모두 바꾼다).
REFINE_TICK_S = 1.0
REFINE_REQUEST_TIMEOUT_S = 30.0
REFINE_MAX_FAILURES = 3
REFINE_FLUSH_TIMEOUT_S = 6.0


class _AgentSession:
    """WebSocket 연결 하나에 붙는 능동형 대응 루프 실행기.

    오디오 응답(빠른 경로)을 막지 않도록 상태 추적·대응 결정은 별도 태스크에서 돈다.
    한 번에 한 사이클만 실행하고, 도는 동안 새 발화가 오면 끝난 뒤 한 번 더 돈다.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.log_id: Optional[int] = None
        self.enabled = True
        self.ended = False
        self._task: Optional[asyncio.Task] = None
        self._dirty = False

    def notify(self) -> None:
        """새 발화가 저장됐음을 알린다."""
        if not self.enabled or self.ended or self.log_id is None:
            return
        if self._task is not None and not self._task.done():
            self._dirty = True
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            self._dirty = False
            try:
                cycle = await asyncio.to_thread(agent_loop.run_cycle, self.log_id)
            except Exception:
                logger.exception("능동형 대응 루프 실행 실패")
                cycle = None
            if cycle:
                await self.emit(cycle["state_update"], cycle["actions"])
            if not self._dirty:
                return

    async def emit(self, state_update: Optional[dict], actions: list[dict]) -> None:
        """상태 갱신과 행동을 클라이언트로 보낸다. agent_action은 action.id 기준 upsert다."""
        if state_update:
            await _send_json(self.websocket, state_update)
        for action in actions:
            await _send_json(
                self.websocket,
                {"type": "agent_action", "log_id": action["log_id"], "action": action},
            )

    async def finish(self, send: bool, before_end: Optional[asyncio.Task] = None) -> Optional[dict]:
        """통화를 마무리한다. 진행 중인 사이클을 기다린 뒤 마지막 상태와 통화 후 행동을 만든다.

        before_end(통화 종료 정리 태스크)가 있으면 call_ended를 보내기 전에 끝나기를 기다린다. 클라이언트는
        call_ended를 받으면 곧 연결을 닫으므로, 마지막 transcript_refined가 그보다 먼저 도착해야 한다.
        """
        if self.ended or self.log_id is None:
            return None
        self.ended = True
        if self._task is not None and not self._task.done():
            await asyncio.wait([self._task])

        if not self.enabled:
            call = get_call_log(self.log_id)
            result = {
                "state_update": None,
                "actions": [],
                "final": {"risk_score": round(call.risk_score, 4), "risk_level": call.risk_level},
            }
        else:
            try:
                result = await asyncio.to_thread(agent_loop.end_call, self.log_id)
            except Exception:
                logger.exception("통화 종료 처리 실패")
                return None

        if send:
            await self.emit(result["state_update"], result["actions"])
            if before_end is not None:
                await asyncio.wait([before_end])
            await _send_json(
                self.websocket,
                {"type": "call_ended", "log_id": self.log_id, "final": result["final"]},
            )
        return result


class _LiveSession:
    """WebSocket 연결 하나의 실시간 전사 보조: 받은 오디오의 시각, 리턴제로 스트리밍 연결, 통화 중 정리(refine).

    - 조각마다 16kHz로 풀어 '통화 시작 후 초'(t_start/t_end)를 센다. 클라이언트가 통째로 무음인 조각은 보내지 않으므로
      벽시계가 아니라 받은 오디오 길이 기준이다. 정리본도 같은 시간축이라 화면에서 실시간 조각과 시각으로 맞춘다.
    - STT_REFINE=rtzr면 REFINE_TICK_S마다 확인해, 마지막 정리 뒤 REFINE_STEP초가 지났고 REFINE_LIVE초 넘게 지난 조각이
      있으면 그 끝까지를 리턴제로 파일 전사로 다시 받아 적어 transcript_refined를 보낸다(live_refine 참고).
      정리는 통화마다 한 번에 하나만 돈다(작업 스레드). 연달아 실패하면 그 통화의 정리를 끄고 실시간 조각만 보여 준다.
    """

    def __init__(self, websocket: Optional[WebSocket]) -> None:
        self.websocket = websocket
        self.log_id: Optional[int] = None
        self.audio = live_refine.CallAudio()
        self.stream: Optional[rtzr_client.StreamSession] = None
        self.refiner: Optional[live_refine.Refiner] = None
        self._refine_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._last_refine_at = 0.0
        self._failures = 0
        self._refine_off = False
        self._ending = False

    def start(self, log_id: int) -> None:
        """새 통화를 시작한다. 같은 연결에서 start가 다시 오면 앞 통화의 연결과 예약을 먼저 정리한다."""
        self.close()
        refine = audio_transcriber.refine_enabled()
        self.log_id = log_id
        self.audio = live_refine.CallAudio(keep_samples=refine)
        self.stream = audio_transcriber.open_live_stream()
        self.refiner = live_refine.Refiner(log_id, live_refine.RefineSettings.from_env()) if refine else None
        self._last_refine_at, self._failures = 0.0, 0
        self._refine_off = self._ending = False
        if self.refiner is not None:
            self._loop_task = asyncio.create_task(self._refine_loop())
            live_refine.register_call(log_id, self.stop_refine)

    def stop_refine(self) -> None:
        """더 정리하지 않는다. 통화 후 전체 재전사가 시작되거나 정리가 계속 실패하면 부른다.

        다시 보낼 일이 없으므로 모아 둔 오디오도 버린다(긴 통화에서 메모리가 계속 늘지 않게).
        """
        self._refine_off = True
        self.audio.keep_samples = False
        self.audio.prune(self.audio.duration)

    def start_flush(self) -> Optional[asyncio.Task]:
        """통화 종료(end) 때 아직 정리하지 않은 끝부분을 한 번 더 정리하는 태스크를 띄운다. 정리를 안 쓰면 None.

        돌고 있던 정리가 있으면 그것부터 기다린다. 합쳐서 REFINE_FLUSH_TIMEOUT_S 안에 끝나지 않으면 건너뛴다
        (통화 후 전체 재전사가 어차피 모두 바꾼다).
        """
        self._ending = True
        if self.refiner is None or self._refine_off:
            return None
        self._flush_task = asyncio.create_task(self._flush())
        return self._flush_task

    def close(self) -> None:
        """정리 예약을 멈추고 스트리밍 연결을 닫는다(EOS). 통화 종료나 연결 끊김 때 부르며 여러 번 불러도 된다."""
        self._refine_off = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        if self.log_id is not None:
            live_refine.unregister_call(self.log_id, self.stop_refine)
        stream, self.stream = self.stream, None
        if stream is not None:
            # EOS·닫기 인사는 네트워크를 기다리므로 이벤트 루프를 막지 않게 작업 스레드에서 한다.
            task = asyncio.create_task(asyncio.to_thread(stream.close))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    async def _refine_loop(self) -> None:
        while not (self._refine_off or self._ending):
            await asyncio.sleep(REFINE_TICK_S)
            now = time.monotonic()
            if self.refiner is None or now - self._last_refine_at < self.refiner.settings.step:
                continue
            if self.refiner.plan(self.audio.chunks, now) is None:
                continue
            self._last_refine_at = now
            try:
                await self._refine()
            except Exception:  # 예상 못 한 오류도 통화는 막지 않는다. 이 통화의 정리만 끈다.
                logger.exception("통화 중 정리 처리 실패(log_id=%s). 이 통화는 실시간 조각만 보여 줍니다.", self.log_id)
                self.stop_refine()

    async def _flush(self) -> None:
        try:
            await asyncio.wait_for(self._refine(final=True), REFINE_FLUSH_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.info("통화 종료 정리가 %.0f초 안에 끝나지 않아 건너뜁니다(log_id=%s).", REFINE_FLUSH_TIMEOUT_S, self.log_id)
        except Exception:
            logger.exception("통화 종료 정리 실패(log_id=%s)", self.log_id)

    async def _refine(self, final: bool = False) -> None:
        """정리 요청 하나를 보내 결과를 붙이고 transcript_refined를 보낸다.

        구간은 잠금 안에서 고른다(앞 요청이 정리 끝을 옮겼을 수 있으므로). 실패하면 정리 끝을 그대로 두어
        다음 차례에 그 구간까지 다시 보낸다. final=True면 받은 오디오 끝까지 정리한다(통화 종료).
        """
        async with self._refine_lock:
            refiner = self.refiner
            if refiner is None or self._refine_off:
                return
            if final:
                window = refiner.window_to(self.audio.duration)
            else:
                window = refiner.plan(self.audio.chunks, time.monotonic())
            if window is None:
                return
            samples = self.audio.window(window.start, window.end)
            try:
                result = await asyncio.to_thread(
                    rtzr_client.transcribe_file,
                    samples,
                    spk_count=window.spk_count,
                    timeout=REFINE_REQUEST_TIMEOUT_S,
                )
            except Exception as exc:
                self._failures += 1
                logger.warning("통화 중 정리(리턴제로) 실패 %d회(log_id=%s): %s", self._failures, self.log_id, exc)
                if self._failures >= REFINE_MAX_FAILURES:
                    logger.warning("정리가 계속 실패해 이 통화(log_id=%s)는 실시간 조각만 보여 줍니다.", self.log_id)
                    self.stop_refine()
                return
            if refiner is not self.refiner:  # 기다리는 동안 같은 연결에서 새 통화가 시작됐다
                return
            self._failures = 0
            update = refiner.apply(window, result.words)
            self.audio.prune(refiner.done - refiner.settings.context)
            sides = await asyncio.to_thread(
                _speaker_sides, refiner.log_id, list(refiner.segments), dict(self.audio.message_spans)
            )
            if self.websocket is not None:
                await _send_json(self.websocket, refiner.event(update, sides))


@router.websocket("/ws/calls/analyze")
async def analyze_call_messages(websocket: WebSocket) -> None:
    """
    클라이언트가 3~4초 단위로 보내는 통화 음성 chunk를 받아 실시간 분석을 수행한다.

    start는 통화 로그만 생성한다. 이후 오디오 chunk를 전사한 뒤 전사된 문장을 저장한다.
    mp3/wav도 테스트/호환 목적으로 지원하며, 기존 JSON 텍스트 메시지도 유지한다.
    """
    await websocket.accept()
    # 오디오 응답과 비동기 상태 갱신이 같은 소켓에 동시에 쓰지 않도록 전송을 직렬화한다.
    websocket.state.send_lock = asyncio.Lock()
    agent = _AgentSession(websocket)
    live = _LiveSession(websocket)
    log_id: Optional[int] = None
    audio_format = "m4a"
    chunk_index = 0
    client_host = websocket.client.host if websocket.client else None

    try:
        while True:
            packet = await websocket.receive()
            if packet.get("type") == "websocket.disconnect":
                return

            if packet.get("bytes") is not None:
                incoming_chunk_index = chunk_index + 1 if log_id is not None else None
                log_api_request(
                    "WS /ws/calls/analyze",
                    {
                        "frame": "bytes",
                        "log_id": log_id,
                        "client_host": client_host,
                        "chunk_index": incoming_chunk_index,
                        "audio_format": audio_format,
                        "byte_size": len(packet["bytes"] or b""),
                    },
                )
                if log_id is None:
                    if not await _send_json(
                        websocket,
                        {
                            "type": "error",
                            "message": "오디오 chunk를 보내기 전에 먼저 start 메시지로 통화 기록을 생성해야 합니다.",
                        }
                    ):
                        return
                    continue

                chunk_index += 1
                response = await _handle_audio_chunk(
                    log_id=log_id,
                    audio_bytes=packet["bytes"] or b"",
                    audio_format=audio_format,
                    chunk_index=chunk_index,
                    agent_mode=agent.enabled,
                    live=live,
                )
                if not await _send_json(websocket, response):
                    return
                if response.get("message_count"):
                    agent.notify()
                continue

            if packet.get("text") is None:
                log_api_request(
                    "WS /ws/calls/analyze",
                    {
                        "frame": str(packet.get("type", "unknown")),
                        "log_id": log_id,
                        "client_host": client_host,
                    },
                )
                if not await _send_json(websocket, {"type": "error", "message": "지원하지 않는 WebSocket 메시지입니다."}):
                    return
                continue

            payload = _parse_ws_json(packet["text"])
            event_type = payload.get("type", "message")
            log_api_request(
                "WS /ws/calls/analyze",
                {
                    "frame": "text",
                    "log_id": log_id,
                    "client_host": client_host,
                    "event_type": event_type,
                    "payload": payload,
                },
            )

            if event_type == "start":
                if "file_type" not in payload:
                    if not await _send_json(
                        websocket,
                        {
                            "type": "error",
                            "message": "start 메시지에는 file_type='realtime'이 필요합니다. 프론트 번들이 오래됐거나 fipe 오타를 보내고 있습니다.",
                        },
                    ):
                        return
                    continue

                audio_format = audio_transcriber.normalize_audio_format(str(payload.get("audio_format", audio_format)))
                call = create_call_log(
                    CallLogCreate(
                        device_id=payload.get("device_id"),
                        name=str(payload.get("name", "실시간 통화")),
                        file_type=str(payload.get("file_type", "realtime")),
                    )
                )
                log_id = call.id
                chunk_index = 0
                agent.log_id = log_id
                agent.enabled = bool(payload.get("agent", True))
                live.start(log_id)
                if not await _send_json(
                    websocket,
                    {
                        "type": "call_started",
                        "call": call.model_dump(),
                        "audio_format": audio_format,
                    }
                ):
                    return
                continue

            if event_type == "audio_chunk":
                if log_id is None:
                    if not await _send_json(
                        websocket,
                        {
                            "type": "error",
                            "message": "오디오 chunk를 보내기 전에 먼저 start 메시지로 통화 기록을 생성해야 합니다.",
                        }
                    ):
                        return
                    continue

                raw_audio = audio_transcriber.decode_audio_chunk_payload(payload)
                chunk_index = int(payload.get("chunk_index") or (chunk_index + 1))
                chunk_format = audio_transcriber.normalize_audio_format(str(payload.get("audio_format", audio_format)))
                response = await _handle_audio_chunk(
                    log_id=log_id,
                    audio_bytes=raw_audio,
                    audio_format=chunk_format,
                    chunk_index=chunk_index,
                    agent_mode=agent.enabled,
                    live=live,
                )
                if not await _send_json(websocket, response):
                    return
                if response.get("message_count"):
                    agent.notify()
                continue

            if event_type in ("action_response", "followup_answer"):
                if not await _handle_action_event(websocket, agent, log_id, event_type, payload):
                    return
                continue

            if event_type == "end":
                if log_id is None:
                    if not await _send_json(websocket, {"type": "error", "message": "종료할 통화가 없습니다."}):
                        return
                    continue
                await agent.finish(send=True, before_end=live.start_flush())
                live.close()
                continue

            if event_type != "message":
                if not await _send_json(websocket, {"type": "error", "message": "지원하지 않는 메시지 타입입니다."}):
                    return
                continue

            if log_id is None:
                if not await _send_json(
                    websocket,
                    {
                        "type": "error",
                        "message": "통화 발화를 보내기 전에 먼저 start 메시지로 통화 기록을 생성해야 합니다.",
                    }
                ):
                    return
                continue

            saved_message = insert_call_message(
                log_id=log_id,
                message=CallMessageCreate(
                    content=str(payload.get("content", "")),
                    turn_index=payload.get("turn_index"),
                ),
            )
            detection = await asyncio.to_thread(
                call_analyzer.detect_and_persist,
                log_id=log_id,
                top_k=int(payload.get("top_k", 5)),
                # 대응 루프가 켜져 있으면 근거 문장은 상태 추적기 요약이 대신한다.
                use_llm_evidence=not agent.enabled,
                agent_mode=agent.enabled,
            )
            response = call_analyzer.build_client_text_analysis_response(
                log_id,
                saved_message.model_dump(),
                detection,
            )
            if not await _send_json(websocket, response):
                return
            agent.notify()
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.exception("WebSocket 실시간 분석 처리 실패")
        await _send_json(websocket, {"type": "error", "message": str(exc)})
        await _close_websocket(websocket)
    finally:
        live.close()
        # 클라이언트가 end 없이 끊어도 마지막 상태와 통화 후 행동은 남긴다(REST로 조회 가능).
        if not agent.ended and agent.log_id is not None:
            task = asyncio.create_task(agent.finish(send=False))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)


async def _handle_action_event(
    websocket: WebSocket,
    agent: _AgentSession,
    log_id: Optional[int],
    event_type: str,
    payload: dict,
) -> bool:
    """행동 카드 반응(action_response)과 후속 확인 답(followup_answer)을 처리한다."""
    if log_id is None:
        return await _send_json(websocket, {"type": "error", "message": "진행 중인 통화가 없습니다."})

    try:
        action_id = int(payload.get("action_id"))
        if event_type == "followup_answer":
            result = await asyncio.to_thread(
                response_agent.handle_response, log_id, action_id, "answered", payload.get("answer")
            )
        else:
            result = await asyncio.to_thread(
                response_agent.handle_response, log_id, action_id, str(payload.get("response", ""))
            )
    except (TypeError, ValueError) as exc:
        return await _send_json(websocket, {"type": "error", "message": str(exc)})

    # 갱신된 행동과 새 행동을 같은 agent_action 이벤트로 보낸다(클라이언트는 id로 upsert).
    await agent.emit(None, [result["action"], *result["next_actions"]])
    return True


async def _send_json(websocket: WebSocket, payload: dict) -> bool:
    """닫혔거나 닫히는 중인 WebSocket에는 응답을 보내지 않는다."""
    try:
        log_api_response("WS /ws/calls/analyze", payload)
        lock = getattr(websocket.state, "send_lock", None)
        if lock is None:
            await websocket.send_json(payload)
        else:
            async with lock:
                await websocket.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):
        logger.info("이미 종료된 WebSocket이라 응답 전송을 생략합니다.")
        return False


async def _close_websocket(websocket: WebSocket) -> None:
    """이미 닫힌 WebSocket close 시도를 조용히 무시한다."""
    try:
        await websocket.close()
    except RuntimeError:
        return


async def _handle_audio_chunk(
    log_id: int,
    audio_bytes: bytes,
    audio_format: str,
    chunk_index: int,
    agent_mode: bool = False,
    live: Optional[_LiveSession] = None,
) -> dict:
    """실시간 오디오 chunk를 전사하고 누적 통화 분석 응답을 만든다.

    live는 연결의 실시간 전사 보조다(통화 시각, 리턴제로 스트리밍 연결, 정리용 오디오). 없으면 이 조각만으로 센다.
    """
    started_at = time.monotonic()
    live = live or _LiveSession(None)
    if not audio_bytes:
        return {
            "type": "audio_chunk_error",
            "log_id": log_id,
            "chunk_index": chunk_index,
            "message_count": 0,
            **_current_risk_fields(log_id),
            "message": "빈 오디오 chunk입니다.",
        }

    try:
        evidence.record_audio(log_id, chunk_index, audio_bytes, f"실시간 음성 조각 #{chunk_index}")
    except Exception as exc:  # 증거 기록 실패가 분석을 막지 않게 한다
        logger.warning("음성 조각 해시 기록 실패(log_id=%s, chunk=%s): %s", log_id, chunk_index, exc)

    # 전사보다 먼저 통화 시각을 센다. 전사가 실패한 조각도 통화 시간에 들어가고 정리(refine)가 다시 받아 적는다.
    samples = await asyncio.to_thread(_decode_chunk, audio_bytes, audio_format)
    chunk = live.audio.add(samples, started_at)

    try:
        raw_segments = await asyncio.to_thread(
            audio_transcriber.transcribe_live_chunk, audio_bytes, audio_format, samples, live.stream
        )
        transcribed_at = time.monotonic()
    except HTTPException as exc:
        return {
            "type": "audio_chunk_error",
            "log_id": log_id,
            "chunk_index": chunk_index,
            "message_count": 0,
            **_current_risk_fields(log_id),
            "message": exc.detail,
        }
    except Exception as exc:
        logger.exception("실시간 오디오 chunk 전사 실패")
        return {
            "type": "audio_chunk_error",
            "log_id": log_id,
            "chunk_index": chunk_index,
            "message_count": 0,
            **_current_risk_fields(log_id),
            "message": f"오디오 chunk 전사에 실패했습니다: {exc}",
        }

    segments = audio_transcriber.normalize_transcribed_segments(raw_segments)
    for segment in segments:
        segment["t_start"], segment["t_end"] = live_refine.segment_times(
            chunk, segment["start_time"], segment["end_time"]
        )
    segments, skipped_duplicate_count = _filter_duplicate_segments(log_id, segments)
    saved_messages = []
    for segment in segments:
        # 전사가 성공한 segment만 call_messages에 저장한다.
        saved_messages.append(
            insert_call_message(
                log_id=log_id,
                message=CallMessageCreate(
                    content=segment["text"],
                ),
            )
        )
    # 정리본 화자의 상대방/나는 추적기가 이 발화들에 붙인 역할과 시각을 맞춰 정한다(_speaker_sides).
    for message, segment in zip(saved_messages, segments):
        live.audio.message_spans[message.id] = (segment["t_start"], segment["t_end"])

    if not saved_messages:
        return {
            "type": "audio_chunk_ack",
            "log_id": log_id,
            "chunk_index": chunk_index,
            "message_count": 0,
            "converted_text": "",
            "transcripts": [],
            "skipped_duplicate_count": skipped_duplicate_count,
            **_current_risk_fields(log_id),
            "message": "전사된 발화가 없습니다." if skipped_duplicate_count == 0 else "중복 전사로 판단되어 저장하지 않았습니다.",
        }

    detection = await asyncio.to_thread(
        call_analyzer.detect_and_persist,
        log_id=log_id,
        use_llm_evidence=False,
        agent_mode=agent_mode,
    )
    analyzed_at = time.monotonic()
    response = call_analyzer.build_client_audio_analysis_response(
        log_id=log_id,
        chunk_index=chunk_index,
        saved_messages=[message.model_dump() for message in saved_messages],
        segments=segments,
        detection=detection,
    )
    response["skipped_duplicate_count"] = skipped_duplicate_count
    response["latency_ms"] = {
        "stt": round((transcribed_at - started_at) * 1000),
        "analysis": round((analyzed_at - transcribed_at) * 1000),
        "total": round((analyzed_at - started_at) * 1000),
    }
    return response


def _decode_chunk(audio_bytes: bytes, audio_format: str) -> Optional[np.ndarray]:
    """조각을 16kHz mono로 푼다. 풀지 못하면 None(그 조각은 통화 시각에 길이 0으로 들어간다)."""
    try:
        return audio_transcriber.decode_audio_16k(audio_bytes, audio_format)
    except Exception as exc:
        logger.warning("음성 조각을 16kHz로 풀지 못해 통화 시각에 넣지 않습니다: %s", exc)
        return None


def _speaker_sides(log_id: int, segments: list, message_spans: dict) -> dict[str, str]:
    """추적기가 실시간 발화에 붙인 역할로 정리본 화자의 상대방/나를 정한다. DB를 읽으므로 작업 스레드에서 부른다."""
    try:
        roles = {message["message_id"]: message["role"] for message in state_repository.list_tagged_messages(log_id)}
    except Exception as exc:
        logger.warning("발화 역할을 읽지 못해 정리본 화자를 미정으로 둡니다(log_id=%s): %s", log_id, exc)
        roles = {}
    return live_refine.speaker_sides(segments, message_spans, roles)


def _parse_ws_json(raw_text: str) -> dict:
    """WebSocket text frame을 JSON 객체로 변환한다."""
    try:
        payload = json.loads(raw_text)
    except Exception as exc:
        raise ValueError("WebSocket text frame은 JSON 형식이어야 합니다.") from exc

    if not isinstance(payload, dict):
        raise ValueError("WebSocket JSON 메시지는 객체여야 합니다.")

    return payload


def _current_risk_fields(log_id: int) -> dict:
    """분석을 새로 못 한 응답에도 현재 통화 위험도 필드를 포함한다."""
    try:
        call = get_call_log(log_id)
    except Exception:
        return {"risk_score": 0.0, "risk_level": "low"}

    return {
        "risk_score": round(call.risk_score, 4),
        "risk_level": call.risk_level,
    }


def _filter_duplicate_segments(log_id: int, segments: list[dict]) -> tuple[list[dict], int]:
    """최근 저장된 긴 전사 문장이 반복되면 저장 대상에서 제외한다.

    오디오 청크를 겹쳐(overlap) 보내므로, 새 세그먼트 앞부분이 직전 문장 끝과
    실제로 겹쳐서 들어온다. 겹치는 부분을 먼저 잘라낸 뒤 완전 중복 여부를 판단한다.
    """
    if not segments:
        return [], 0

    recent_messages = list_call_messages(log_id)[-120:]
    recent_texts = {_normalize_duplicate_key(message.content) for message in recent_messages}
    last_text = recent_messages[-1].content if recent_messages else ""
    kept_segments = []
    skipped_count = 0

    for segment in segments:
        original_text = str(segment.get("text", ""))
        stripped_text = _strip_overlap_prefix(last_text, original_text)
        duplicate_key = _normalize_duplicate_key(stripped_text)

        if not stripped_text or (duplicate_key and duplicate_key in recent_texts):
            skipped_count += 1
            continue

        kept_segments.append({**segment, "text": stripped_text})
        last_text = original_text  # 다음 세그먼트와의 겹침은 원본(자르기 전) 기준으로 비교
        if duplicate_key:
            recent_texts.add(duplicate_key)

    return kept_segments, skipped_count


def _strip_overlap_prefix(previous_text: str, new_text: str, min_overlap: int = 6) -> str:
    """오디오 겹침으로 new_text 앞부분이 previous_text 끝부분과 겹치면 그 부분을 잘라낸다.

    이전 시도들의 문제:
    - 단순 "prev의 끝 L글자 == curr의 앞 L글자" 완전일치 방식은, 겹친 오디오를 두 번
      다르게 전사하면("가능하신" vs "가능하실까요") 끝까지 정확히 일치하는 길이가
      없어서 못 잡는다.
    - 공백 기준 단어 단위 비교는 겹침이 단어 중간에서 잘리면("대출관리팀" →
      "관리팀"부터 시작) 아예 못 잡는다.
    그래서 prev의 꼬리를 여러 길이 L로 잘라가며 curr 시작 부분과 "앞에서부터 몇 글자나
    일치하는지"를 전부 계산하고, 그중 가장 길게 일치하는 지점을 겹침으로 본다. 일치가
    길수록 진짜 겹침일 확률이 높으므로, 충분히 긴 일치(min_overlap)만 인정해 "습니다"
    같은 흔한 어미의 우연한 일치는 걸러낸다.
    """
    prev = previous_text.strip()
    curr = new_text.strip()
    if not prev or not curr:
        return curr

    best_match = 0
    max_len = min(len(prev), len(curr))
    for length in range(1, max_len + 1):
        window = prev[-length:]
        matched = _shared_prefix_len(window, curr)
        if matched > best_match:
            best_match = matched

    if best_match >= min_overlap:
        return curr[best_match:].strip()
    return curr


def _shared_prefix_len(a: str, b: str) -> int:
    """두 문자열이 앞에서부터 몇 글자나 같은지 센다."""
    length = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        length += 1
    return length


def _normalize_duplicate_key(text: str) -> str:
    """짧은 맞장구는 제외하고, 의미 있는 길이의 반복 문장만 중복 키로 사용한다."""
    normalized = " ".join(text.split()).strip()
    if len(normalized) < 8:
        return ""
    return normalized
