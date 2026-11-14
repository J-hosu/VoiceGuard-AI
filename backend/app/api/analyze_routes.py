"""통화 외 입력 분석: 문자·메신저 메시지, 문서 이미지. docs/API_SPEC_V2.md 11절."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from backend.app.services import document_analyzer, message_analyzer


router = APIRouter()


async def _read_image(image: Optional[UploadFile]) -> Optional[tuple[str, bytes]]:
    if image is None:
        return None
    data = await image.read()
    if not data:
        return None
    media_type = (image.content_type or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in document_analyzer.MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="jpg, png, webp, gif 이미지만 지원합니다.")
    if len(data) > document_analyzer.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="이미지가 너무 큽니다. 4MB 이하로 줄여서 올려 주세요.")
    return media_type, data


@router.post("/analyze/message")
async def analyze_message(text: str = Form(""), image: Optional[UploadFile] = File(None)) -> dict:
    """문자·메신저 메시지가 스미싱인지 판별한다. 본문(text) 또는 캡처(image) 중 하나는 있어야 한다."""
    picture = await _read_image(image)
    if not text.strip() and picture is None:
        raise HTTPException(status_code=422, detail="메시지 본문을 붙여넣거나 캡처 이미지를 올려 주세요.")
    try:
        return await asyncio.to_thread(message_analyzer.analyze, text, picture)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/analyze/document")
async def analyze_document(image: UploadFile = File(...), note: str = Form("")) -> dict:
    """받은 문서의 사진·캡처에 피싱용 위조 문서의 특징이 있는지 점검한다."""
    picture = await _read_image(image)
    if picture is None:
        raise HTTPException(status_code=422, detail="문서 이미지를 올려 주세요.")
    try:
        return await asyncio.to_thread(document_analyzer.analyze, picture[0], picture[1], note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
