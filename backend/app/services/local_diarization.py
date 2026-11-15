"""로컬 화자 분리 (시험·보관용 — 앱 흐름에는 연결하지 않았다).

음성을 외부로 보내지 않고 이 컴퓨터에서 통화의 두 사람을 나눈다.
1) 앱과 같은 기준으로 무음 조각을 만든다(1.2초 넘게 쌓인 뒤 0.25초 조용하면 자르고, 10초면 강제로 자름).
2) 조각마다 목소리 특징(임베딩)을 뽑는다: sherpa-onnx의 3D-Speaker ERes2Net 공개 모델
   (models/diar/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx, GitHub 공개본이라 토큰 불필요, CPU로 동작).
3) 통화는 두 사람이므로 길이 가중 k-means로 2명으로 묶는다.

측정(AI Hub 시험 녹음 120개, docs/tech/transcription.md): 통화 후 문장 단위 98.5%, 실시간 다시 묶기 조각 단위 97.7%.
CPU 코어 하나로 5초 조각 임베딩 약 0.19초.

- diarize(audio): 통화 후. [(시작초, 끝초, 화자 0/1)]
- OnlineDiarizer: 실시간. add()로 조각을 넣을 때마다 지금까지 조각 전체를 다시 묶고, 번호는 이전 중심과 맞춰 유지한다
  (첫 화자 = 0). 앞 조각의 화자가 바뀔 수 있어 labels()로 전체를 다시 받을 수 있다.
시험: python -m backend.app.services.local_diarization <음성 파일>
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
MODEL = Path(os.environ.get("DIAR_EMBEDDING_MODEL", str(ROOT / "models" / "diar" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")))
SR = 16000
MIN_S, MAX_S, SIL_S, SIL_RMS, FRAME = 1.2, 10.0, 0.25, 0.01, 320
MIN_EMBED_S = 0.6        # 이보다 짧은 조각은 목소리 특징이 불안정해 가까운 조각의 화자를 따른다
SAME_SPEAKER = 0.5       # 아직 한 사람만 들린 것으로 보는 유사도 기준(실시간)
_extractor = None


def _get_extractor():
    global _extractor
    if _extractor is None:
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=int(os.environ.get("DIAR_THREADS", "2")))
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _extractor


def embed(piece: np.ndarray) -> np.ndarray:
    ext = _get_extractor()
    stream = ext.create_stream()
    stream.accept_waveform(SR, piece.astype(np.float32))
    stream.input_finished()
    v = np.asarray(ext.compute(stream), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def silence_chunks(audio: np.ndarray, offset: float = 0.0) -> list[tuple[float, float]]:
    chunks, start = [], 0
    for end in range(FRAME, len(audio) + FRAME, FRAME):
        end = min(end, len(audio))
        length = end - start
        if length >= MAX_S * SR:
            chunks.append((start, end))
            start = end
            continue
        if length >= MIN_S * SR:
            tail = audio[max(start, end - int(SIL_S * SR)):end]
            if len(tail) and float(np.sqrt(np.mean(tail ** 2))) < SIL_RMS:
                chunks.append((start, end))
                start = end
    if start < len(audio):
        chunks.append((start, len(audio)))
    return [(offset + a / SR, offset + b / SR) for a, b in chunks if float(np.sqrt(np.mean(audio[a:b] ** 2))) >= SIL_RMS]


def _kmeans2(embs: list[np.ndarray], spans: list[tuple[float, float]]):
    from sklearn.cluster import KMeans

    E = np.stack(embs)
    w = np.array([e - s for s, e in spans])
    km = KMeans(n_clusters=2, n_init=4, random_state=0).fit(E, sample_weight=w)
    cent = km.cluster_centers_ / np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True)
    return cent


def _fill_short(spans, labels, long_idx):
    """짧은 조각은 시간상 가장 가까운 긴 조각의 화자를 따른다."""
    mids = np.array([(spans[i][0] + spans[i][1]) / 2 for i in long_idx])
    out = []
    for i, (s, e) in enumerate(spans):
        if labels[i] is None:
            labels[i] = labels[long_idx[int(np.argmin(np.abs(mids - (s + e) / 2)))]]
        out.append((s, e, int(labels[i])))
    return out


def diarize(audio: np.ndarray) -> list[tuple[float, float, int]]:
    """통화 후 화자 분리: [(시작초, 끝초, 화자 0/1)]. 첫 화자가 0이 되게 번호를 맞춘다."""
    spans = silence_chunks(audio)
    long_idx = [i for i, (s, e) in enumerate(spans) if e - s >= MIN_EMBED_S]
    if len(long_idx) < 2:
        return [(s, e, 0) for s, e in spans]
    embs = [embed(audio[int(spans[i][0] * SR):int(spans[i][1] * SR)]) for i in long_idx]
    cent = _kmeans2(embs, [spans[i] for i in long_idx])
    raw = [int(np.argmax(cent @ v)) for v in embs]
    first = raw[0]
    labels: list[int | None] = [None] * len(spans)
    for i, lab in zip(long_idx, raw):
        labels[i] = 0 if lab == first else 1
    return _fill_short(spans, labels, long_idx)


class OnlineDiarizer:
    """실시간 화자 분리: 조각이 올 때마다 지금까지 조각 전체를 2명으로 다시 묶는다."""

    def __init__(self) -> None:
        self.spans: list[tuple[float, float]] = []
        self.embs: list[np.ndarray] = []
        self.cent: np.ndarray | None = None
        self.first: np.ndarray | None = None

    def add(self, piece: np.ndarray, start: float, end: float) -> int:
        """조각 하나를 넣고 그 조각의 화자(0/1)를 돌려준다. 짧은 조각은 직전 화자를 따른다."""
        if end - start < MIN_EMBED_S or len(piece) < int(MIN_EMBED_S * SR):
            return self._label_of(len(self.embs) - 1) if self.embs else 0
        v = embed(piece)
        self.spans.append((start, end))
        self.embs.append(v)
        if self.first is None:
            self.first = v
            return 0
        if self.cent is None and all(float(e @ self.first) >= SAME_SPEAKER for e in self.embs):
            return 0  # 아직 한 사람만 들림
        cent = _kmeans2(self.embs, self.spans)
        if self.cent is None:  # 처음 나눌 때: 첫 화자를 0번으로
            if float(cent[1] @ self.first) > float(cent[0] @ self.first):
                cent = cent[[1, 0]]
        else:  # 이전 중심과 번호를 맞춘다
            sim = cent @ self.cent.T
            if sim[0, 1] + sim[1, 0] > sim[0, 0] + sim[1, 1]:
                cent = cent[[1, 0]]
        self.cent = cent
        return int(np.argmax(cent @ v))

    def _label_of(self, i: int) -> int:
        if self.cent is None or i < 0:
            return 0
        return int(np.argmax(self.cent @ self.embs[i]))

    def labels(self) -> list[tuple[float, float, int]]:
        """지금 중심 기준으로 지금까지 조각 전체의 화자(다시 묶인 결과)."""
        return [(s, e, self._label_of(i)) for i, (s, e) in enumerate(self.spans)]


def _main(path: str) -> None:
    from faster_whisper import decode_audio

    audio = decode_audio(path, sampling_rate=SR)
    t0 = time.perf_counter()
    segs = diarize(audio)
    took = time.perf_counter() - t0
    dur = len(audio) / SR
    print(f"{Path(path).name}: {dur:.1f}초, 조각 {len(segs)}개, 화자 {len({s[2] for s in segs})}명, CPU {took:.2f}초 (실시간의 {took / dur:.3f}배)")
    for s, e, k in segs[:12]:
        print(f"  {s:6.1f}~{e:6.1f}초  화자{'AB'[k]}")


if __name__ == "__main__":
    _main(sys.argv[1])
