"""통화 중 '뒤따라 다시 받아 적기'(정리, refine) 계층.

실시간 조각(무음 경계에서 자른 1.2~10초)은 바로 받아 적어 먼저 보여 주고, REFINE_STEP초마다 조금 지난 구간을
리턴제로 파일 전사(화자 분리·단어 시각)로 다시 받아 적어 화자가 붙은 정리본으로 바꿔 끼운다.
- 요청 구간: [마지막 정리 끝 - REFINE_CONTEXT, REFINE_LIVE초 넘게 지난 마지막 조각 끝]. 앞 문맥을 같이 보내 화자와 문맥을 잇는다.
- 붙이는 단어: 마지막 정리 끝(0.05초 여유) 뒤에서 시작한 단어만. 문맥 구간 단어는 화자 번호를 맞추는 데만 쓴다.
- 화자 번호: 문맥 구간에서 이번 결과의 번호와 지금까지 붙인 번호가 겹친 시간으로 짝짓고(헝가리안),
  문맥 구간에 걸친 앞 정리본의 화자도 겹친 시간 투표로 다시 매긴다(relabel).
- 상대방/나: 상태 추적기가 실시간 조각 발화마다 붙인 역할과 정리본 화자 구간이 겹친 시간으로 정한다.
측정(stt_lab/refine_sim.py, 실제 상담 녹음): 글자 오류율 실시간 조각 8.6% → 정리본 4~5%, 화자 정확도 약 90%,
요청 하나 약 2초, 정리본이 화면에 붙기까지 약 23초, 리턴제로 과금은 오디오 길이의 약 2.5배.

이 모듈은 네트워크를 부르지 않는다. 전사는 websocket.py가 rtzr_client로 하고 결과 단어만 넘기므로 가짜 결과로 시험할 수 있다.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from backend.app.services.rtzr_client import Word, to_pcm16


logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# 새로 정리할 오디오가 이보다 짧으면 다음 차례를 기다린다(요청마다 앞 문맥까지 다시 과금되므로).
MIN_NEW_S = 5.0
# 통화 종료 정리: 남은 끝부분이 이보다 짧으면 보내지 않는다.
MIN_WINDOW_S = 0.5
# 단어 시각 오차 여유. 이전 정리 끝보다 이만큼 앞에서 시작한 단어까지는 새 단어로 붙인다.
ACCEPT_SLACK_S = 0.05
# 같은 화자의 단어 사이가 이보다 짧으면 한 구간(말풍선)으로 묶는다.
WORD_GAP_S = 1.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        logger.warning("%s 값이 숫자가 아니어서 기본값 %s를 씁니다.", name, default)
        return default


@dataclass(frozen=True)
class RefineSettings:
    """정리 주기와 구간 길이(초). .env의 REFINE_STEP, REFINE_LIVE, REFINE_CONTEXT, REFINE_SPK_MIN_S로 바꾼다."""

    step: float = 15.0  # 정리 요청 간격
    live: float = 20.0  # 최근 이만큼은 실시간 조각을 그대로 보여 준다(요청 끝은 이보다 오래된 조각 끝)
    context: float = 40.0  # 화자·문맥을 잇기 위해 앞에서 같이 보내는 길이
    spk_min_s: float = 8.0  # 요청 구간이 이 이상이면 화자 수를 2명으로 정해 보낸다(짧으면 리턴제로가 정한다)

    @classmethod
    def from_env(cls) -> "RefineSettings":
        return cls(
            step=_env_float("REFINE_STEP", cls.step),
            live=_env_float("REFINE_LIVE", cls.live),
            context=_env_float("REFINE_CONTEXT", cls.context),
            spk_min_s=_env_float("REFINE_SPK_MIN_S", cls.spk_min_s),
        )


# ---------------------------------------------------------------- 통화 오디오와 시각


@dataclass(frozen=True)
class ChunkSpan:
    """실시간 조각 하나의 통화 시각(초)과 서버가 받은 시각(time.monotonic)."""

    t_start: float
    t_end: float
    received_at: float


class CallAudio:
    """통화 하나에서 받은 오디오를 16kHz로 이어 붙이고 시각을 센다.

    시각은 '통화 시작 후 초'인데, 벽시계가 아니라 지금까지 받은 오디오 길이다(클라이언트가 통째로 무음인 조각은
    보내지 않으므로). 정리 요청도 이 시간축으로 보내므로 실시간 조각과 정리본을 시각으로 맞출 수 있다.
    keep_samples=True(정리를 켠 통화)일 때만 오디오를 남기고, 다시 보낼 일이 없는 앞부분은 prune으로 버린다.
    """

    def __init__(self, keep_samples: bool = False) -> None:
        self.keep_samples = keep_samples
        self.total_samples = 0
        self.chunks: list[ChunkSpan] = []
        # 저장된 실시간 발화 id → (t_start, t_end). 정리본 화자의 상대방/나를 정할 때 추적기 역할과 맞춰 본다.
        self.message_spans: dict[int, tuple[float, float]] = {}
        self._pieces: list[tuple[int, np.ndarray]] = []  # (시작 샘플, PCM16)

    @property
    def duration(self) -> float:
        return self.total_samples / SAMPLE_RATE

    def add(self, samples: Optional[np.ndarray], received_at: float) -> ChunkSpan:
        """조각 하나를 이어 붙이고 그 조각의 시각을 돌려준다. 풀지 못한 조각(None)은 길이 0으로 센다."""
        count = 0 if samples is None else len(samples)
        start = self.total_samples
        self.total_samples += count
        span = ChunkSpan(start / SAMPLE_RATE, self.total_samples / SAMPLE_RATE, received_at)
        if count:
            self.chunks.append(span)
            if self.keep_samples:
                self._pieces.append((start, to_pcm16(samples)))
        return span

    def window(self, start: float, end: float) -> np.ndarray:
        """[start, end) 구간의 오디오(PCM16)를 이어 붙여 돌려준다."""
        lo, hi = round(start * SAMPLE_RATE), round(end * SAMPLE_RATE)
        parts = [
            pcm[max(lo - offset, 0):hi - offset]
            for offset, pcm in self._pieces
            if offset < hi and offset + len(pcm) > lo
        ]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    def prune(self, before: float) -> None:
        """before(초) 전에 끝난 오디오와 조각 기록을 버린다. 정리 요청은 '이전 정리 끝 - 문맥'보다 앞을 다시 보내지 않는다."""
        cut = round(before * SAMPLE_RATE)
        self._pieces = [(offset, pcm) for offset, pcm in self._pieces if offset + len(pcm) > cut]
        self.chunks = [chunk for chunk in self.chunks if chunk.t_end > before]


def segment_times(chunk: ChunkSpan, start: Optional[float], end: Optional[float]) -> tuple[float, float]:
    """조각 안 상대 시각(전사 결과의 start/end 초)을 통화 시작 후 초로 바꾼다. 값이 없으면 조각 경계를 쓴다.

    시작은 조각 끝보다 조금 앞으로 묶는다. 정리본이 이 조각 끝까지 덮으면(refined_until = 조각 끝) 이 발화는
    'refined_until 앞'으로 판정되어 화면에서 빠져야 하기 때문이다.
    """
    latest_start = max(chunk.t_start, chunk.t_end - 0.01)
    t_start = min(max(chunk.t_start + _seconds(start, 0.0), chunk.t_start), latest_start)
    t_end = min(max(chunk.t_start + _seconds(end, chunk.t_end - chunk.t_start), t_start), chunk.t_end)
    return round(t_start, 3), round(t_end, 3)


def _seconds(value: object, default: float) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 정리본


@dataclass
class Segment:
    """정리본 구간 하나(같은 화자의 이어진 단어). 시각은 통화 시작 후 초, speaker는 통화 전체 기준 번호."""

    id: str
    t_start: float
    t_end: float
    speaker: int
    text: str

    def public(self) -> dict:
        return {
            "id": self.id,
            "t_start": round(self.t_start, 3),
            "t_end": round(self.t_end, 3),
            "speaker": int(self.speaker),
            "text": self.text,
        }


@dataclass(frozen=True)
class RefineWindow:
    """정리 요청 하나: [start, end) 구간을 보내고, done(이전 정리 끝) 뒤에서 시작한 단어만 붙인다."""

    start: float
    end: float
    done: float
    spk_count: int


@dataclass
class RefineUpdate:
    segments: list[Segment]  # 이번에 새로 붙은 구간
    relabels: list[dict]  # 화자가 바뀐 앞 구간 [{"id", "speaker"}]
    refined_until: float


class Refiner:
    """통화 하나의 정리본. 요청 구간을 고르고(plan), 결과 단어를 붙이고(apply), 보낼 이벤트를 만든다(event)."""

    def __init__(self, log_id: int, settings: Optional[RefineSettings] = None) -> None:
        self.log_id = log_id
        self.settings = settings or RefineSettings()
        self.done = 0.0  # 여기까지 정리했다(refined_until)
        self.segments: list[Segment] = []
        self._next_id = 1

    def plan(self, chunks: Iterable[ChunkSpan], now: float) -> Optional[RefineWindow]:
        """지금(now, time.monotonic) 보낼 요청 구간. 끝은 'LIVE초 넘게 전에 받은 마지막 조각 끝'(무음 경계)이고,
        새로 정리할 오디오가 MIN_NEW_S초를 넘어야 한다. 보낼 것이 없으면 None."""
        ends = [
            chunk.t_end
            for chunk in chunks
            if chunk.received_at <= now - self.settings.live and chunk.t_end > self.done + MIN_NEW_S
        ]
        return self.window_to(max(ends)) if ends else None

    def window_to(self, end: float) -> Optional[RefineWindow]:
        """이전 정리 끝부터 end까지를 정리하는 요청 구간(앞 문맥 포함). 남은 길이가 너무 짧으면 None."""
        if end - self.done < MIN_WINDOW_S:
            return None
        start = max(0.0, self.done - self.settings.context)
        spk_count = 2 if end - start >= self.settings.spk_min_s else 0
        return RefineWindow(start=start, end=end, done=self.done, spk_count=spk_count)

    def apply(self, window: RefineWindow, words: Iterable[Word]) -> RefineUpdate:
        """이번 요청의 단어(요청 구간 기준 시각, 이번 결과의 화자 번호)를 정리본에 붙인다.

        refine_sim.py run_rtzr(RELABEL=1)와 같은 순서다: 번호 맞추기 → 문맥 구간에 걸친 앞 구간 다시 매기기 → 새 단어 묶기.
        """
        absolute = [(s + window.start, e + window.start, w, int(k)) for s, e, w, k in words]
        context = [word for word in absolute if word[0] < window.done - ACCEPT_SLACK_S]
        new = [word for word in absolute if word[0] >= window.done - ACCEPT_SLACK_S]
        accepted = [(seg.t_start, seg.t_end, seg.text, seg.speaker) for seg in self.segments]
        mapping = map_speakers(context, accepted, sorted({k for *_x, k in absolute}))

        relabels: list[dict] = []
        if context:
            for seg in self.segments:
                if seg.t_end <= window.start:
                    continue
                votes: dict[int, float] = defaultdict(float)
                for s, e, _w, k in context:
                    overlap = min(e, seg.t_end) - max(s, seg.t_start)
                    if overlap > 0:
                        votes[mapping.get(k, k)] += overlap
                if votes:
                    best = max(votes, key=votes.get)
                    if best != seg.speaker:
                        seg.speaker = best
                        relabels.append({"id": seg.id, "speaker": int(best)})

        added: list[Segment] = []
        for s, e, text, k in group_words([(s, e, w, mapping.get(k, k)) for s, e, w, k in new]):
            segment = Segment(id=f"{self.log_id}-{self._next_id}", t_start=s, t_end=e, speaker=int(k), text=text)
            self._next_id += 1
            self.segments.append(segment)
            added.append(segment)
        self.done = window.end
        return RefineUpdate(segments=added, relabels=relabels, refined_until=window.end)

    def event(self, update: RefineUpdate, sides: dict[str, str]) -> dict:
        """transcript_refined 이벤트. segments는 새 구간만, relabels는 화자가 바뀐 앞 구간만, speaker_sides는 전체다."""
        return {
            "type": "transcript_refined",
            "log_id": self.log_id,
            "engine": "rtzr",
            "refined_until": round(update.refined_until, 3),
            "segments": [segment.public() for segment in update.segments],
            "relabels": update.relabels,
            "speaker_sides": sides,
        }


def map_speakers(context: list[Word], accepted: list[Word], local_ids: Optional[list[int]] = None) -> dict[int, int]:
    """이번 결과의 화자 번호 → 통화 전체 번호. 문맥 구간에서 겹친 시간 합이 가장 크도록 짝짓는다(헝가리안).

    겹침이 없는 번호(문맥에 없던 사람 포함)는 아직 짝이 없는 전체 번호를, 그것도 없으면 새 번호를 받는다.
    refine_sim.py는 문맥에 없던 번호를 그대로 써서 다른 사람과 같은 번호가 될 수 있었는데, 여기서는 빈 번호를 준다.
    지금까지 붙인 것이 없으면(첫 정리) 빈 dict를 돌려 이번 번호를 그대로 쓴다.
    """
    context_ids = sorted({k for *_x, k in context})
    local_ids = sorted(set(local_ids or []) | set(context_ids))
    global_ids = sorted({g for *_x, g in accepted})
    if not accepted or not local_ids:
        return {}
    overlap = np.zeros((len(local_ids), len(global_ids)))
    for s, e, _w, k in context:
        for gs, ge, _t, g in accepted:
            seconds = min(e, ge) - max(s, gs)
            if seconds > 0:
                overlap[local_ids.index(k), global_ids.index(g)] += seconds
    mapping: dict[int, int] = {}
    if overlap.sum() > 0:
        rows, cols = linear_sum_assignment(-overlap)
        mapping = {local_ids[r]: global_ids[c] for r, c in zip(rows, cols) if overlap[r, c] > 0}
    for k in local_ids:
        if k not in mapping:
            free = [g for g in global_ids if g not in mapping.values()]
            mapping[k] = free[0] if free else max([*global_ids, *mapping.values()]) + 1
    return mapping


def group_words(words: list[Word]) -> list[Word]:
    """같은 화자의 이어진 단어(사이 1초 미만)를 한 구간으로 묶는다: [(시작, 끝, 글, 화자)]"""
    grouped: list[Word] = []
    for s, e, w, k in words:
        if grouped and grouped[-1][3] == k and s - grouped[-1][1] < WORD_GAP_S:
            start, _end, text, _k = grouped[-1]
            grouped[-1] = (start, e, f"{text} {w}".strip(), k)
        else:
            grouped.append((s, e, w.strip(), k))
    return grouped


def speaker_sides(
    segments: list[Segment],
    message_spans: dict[int, tuple[float, float]],
    roles: dict[int, str],
) -> dict[str, str]:
    """정리본 화자 번호마다 상대방(other)/나(me)/미정(unknown)을 정한다. 키는 번호 문자열이다.

    roles는 상태 추적기가 실시간 발화마다 붙인 역할(message_id → user=앱 사용자 / suspect=상대방 / unknown)이다.
    발화 시각과 각 화자의 정리본 구간이 겹친 시간을 역할별로 더하고, user 근거가 생기면
    'user 겹침 - suspect 겹침'이 가장 큰 화자를 나로, 나머지를 상대방으로 둔다. 실시간 조각에 두 사람 말이 섞여
    user 겹침이 양쪽에 생겨도, 역할과 쪽이 가장 많이 맞는 배치를 고르는 셈이라 상대방 쪽이 나로 뽑히지 않는다.
    user 근거가 아직 없으면 suspect 겹침이 가장 큰 화자만 상대방으로 두고 나머지는 미정이다.
    """
    speakers = sorted({seg.speaker for seg in segments})
    user: dict[int, float] = defaultdict(float)
    suspect: dict[int, float] = defaultdict(float)
    for message_id, role in roles.items():
        span = message_spans.get(message_id)
        if span is None or role not in ("user", "suspect"):
            continue
        bucket = user if role == "user" else suspect
        for seg in segments:
            overlap = min(span[1], seg.t_end) - max(span[0], seg.t_start)
            if overlap > 0:
                bucket[seg.speaker] += overlap

    sides = {str(k): "unknown" for k in speakers}
    if any(user[k] > 0 for k in speakers):
        me = max(speakers, key=lambda k: user[k] - suspect[k])
        sides = {str(k): ("me" if k == me else "other") for k in speakers}
    elif any(suspect[k] > 0 for k in speakers):
        sides[str(max(speakers, key=lambda k: suspect[k]))] = "other"
    return sides


# ---------------------------------------------------------------- 진행 중인 통화


# log_id → 정리를 멈추는 함수. 통화 후 전체 재전사가 시작되면 실시간 정리는 어차피 전부 교체되므로 멈춘다.
_active_calls: dict[int, Callable[[], None]] = {}


def register_call(log_id: int, stop: Callable[[], None]) -> None:
    _active_calls[log_id] = stop


def unregister_call(log_id: int, stop: Callable[[], None]) -> None:
    if _active_calls.get(log_id) == stop:
        del _active_calls[log_id]


def stop_call(log_id: int) -> None:
    """진행 중인 통화의 실시간 정리를 멈춘다(없으면 아무것도 하지 않는다)."""
    stop = _active_calls.pop(log_id, None)
    if stop is not None:
        stop()
