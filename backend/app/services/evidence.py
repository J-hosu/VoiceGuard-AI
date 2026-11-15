"""증거 무결성: 통화 원본(음성 조각·업로드 파일)과 최종 전사문의 SHA-256 해시를 남긴다.

- 음성: 실시간 음성 조각과 업로드한 녹음 파일을 받는 즉시 해시하고, 앞 조각의 체인 해시와 이어 붙여
  `chain = sha256(prev_chain + sha256)`로 기록한다(evidence_files). 조각 하나라도 빠지거나 바뀌면 체인이 맞지 않는다.
  음성 파일 자체는 개인정보 때문에 전사 후 지우므로, 원본을 따로 보관한 경우 그 해시와 맞춰 볼 수 있게 해시만 남긴다.
- 전사문: 통화가 끝날 때 발화 번호와 내용을 차례로 해시해 봉인(chunk_index = -1)한다. 나중에 전사문이 바뀌면 검증에서 드러난다.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from backend.app.database import get_connection


TRANSCRIPT_INDEX = -1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chain(prev_hash: str, digest: str) -> str:
    return _sha256((prev_hash + digest).encode("ascii"))


def record_audio(log_id: Optional[int], chunk_index: int, data: bytes, label: str) -> Optional[dict]:
    """음성 조각(또는 녹음 파일 전체)의 해시를 체인에 붙여 저장한다."""
    if not log_id or not data:
        return None
    digest = _sha256(data)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT chain_hash FROM evidence_files WHERE log_id = ? AND chunk_index >= 0 ORDER BY chunk_index DESC, id DESC LIMIT 1",
            (log_id,),
        ).fetchone()
        prev_hash = row["chain_hash"] if row else ""
        chain_hash = _chain(prev_hash, digest)
        connection.execute(
            """
            INSERT INTO evidence_files(log_id, chunk_index, path, sha256, prev_hash, chain_hash, size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (log_id, chunk_index, label, digest, prev_hash, chain_hash, len(data)),
        )
    return {"chunk_index": chunk_index, "sha256": digest, "chain_hash": chain_hash}


def _transcript_hash(log_id: int) -> tuple[str, int]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT turn_index, content FROM call_messages WHERE log_id = ? ORDER BY turn_index ASC, id ASC",
            (log_id,),
        ).fetchall()
    chain = ""
    for row in rows:
        chain = _chain(chain, _sha256(f"{row['turn_index']}|{row['content']}".encode("utf-8")))
    return chain, len(rows)


def seal_transcript(log_id: int) -> Optional[dict]:
    """최종 전사문을 봉인한다. 다시 부르면(재전사 등) 마지막 봉인으로 바꾼다."""
    digest, count = _transcript_hash(log_id)
    if not count:
        return None
    with get_connection() as connection:
        connection.execute("DELETE FROM evidence_files WHERE log_id = ? AND chunk_index = ?", (log_id, TRANSCRIPT_INDEX))
        connection.execute(
            """
            INSERT INTO evidence_files(log_id, chunk_index, path, sha256, prev_hash, chain_hash, size)
            VALUES (?, ?, ?, ?, '', ?, ?)
            """,
            (log_id, TRANSCRIPT_INDEX, f"전사문 발화 {count}개", digest, digest, count),
        )
    return {"sha256": digest, "utterances": count}


def verify(log_id: int) -> dict:
    """저장된 해시 체인과 전사문 봉인을 다시 계산해 맞는지 확인한다."""
    with get_connection() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT chunk_index, path, sha256, prev_hash, chain_hash, size, created_at FROM evidence_files WHERE log_id = ? ORDER BY chunk_index ASC, id ASC",
                (log_id,),
            ).fetchall()
        ]
    audio = [row for row in rows if row["chunk_index"] >= 0]
    sealed = next((row for row in rows if row["chunk_index"] == TRANSCRIPT_INDEX), None)

    chain_ok = True
    prev = ""
    for row in audio:
        if row["prev_hash"] != prev or row["chain_hash"] != _chain(prev, row["sha256"]):
            chain_ok = False
            break
        prev = row["chain_hash"]

    current, count = _transcript_hash(log_id)
    return {
        "audio_chunks": len(audio),
        "audio_bytes": sum(row["size"] for row in audio),
        "audio_chain_ok": chain_ok,
        "audio_last_chain_hash": audio[-1]["chain_hash"] if audio else "",
        "audio_first_at": audio[0]["created_at"] if audio else "",
        "audio_last_at": audio[-1]["created_at"] if audio else "",
        "transcript_sealed": sealed is not None,
        "transcript_hash": sealed["sha256"] if sealed else "",
        "transcript_sealed_at": sealed["created_at"] if sealed else "",
        "transcript_current_hash": current,
        "transcript_ok": bool(sealed) and sealed["sha256"] == current,
        "utterances": count,
        "chunks": [{key: row[key] for key in ("chunk_index", "path", "sha256", "chain_hash", "size", "created_at")} for row in audio[-20:]],
    }
