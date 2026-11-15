"""통화 누적 내용 분석, 탐지 결과 저장, 클라이언트 응답 생성 서비스."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from backend.app.repository import (
    build_call_text,
    get_call_log,
    insert_notification,
    list_call_messages,
    save_detection_result,
)
from backend.app import state_repository
from backend.app.codes import tactics_to_patterns
from backend.app.schemas import NotificationCreate
from backend.app.services import claude_client
from backend.app.services.koelectra_scorer import MODEL_DIR, TH_DANGER, TH_WARNING, KoElectraScorer, risk_level_of
from backend.app.services.rag_detector import RagPhishingDetector


logger = logging.getLogger(__name__)

detector = RagPhishingDetector()
scorer = KoElectraScorer()
_koelectra_missing_logged = False

# 1차 탐지(KoELECTRA/RAG)가 애매하게 판단하는 구간. 이 구간에 들면 LLM에게 통화 전체를
# 다시 읽혀 재검토한다. 이 범위 밖(특히 90%대 오탐)은 별도의 근거 기반 보정으로 처리한다.
GRAY_ZONE_LOW = 0.30
GRAY_ZONE_HIGH = 0.75

# v2 융합: 빠른 경로 점수의 가중치(나머지는 상태 추적기 점수).
FUSION_FAST_WEIGHT = 0.3
# 추적기가 이 단계 이상을 확신하면 융합 점수를 주의/강한경고 기준선 아래로 내리지 않는다.
STAGE_FLOOR_WARNING = 3  # pressure(위기 조성) 이상
STAGE_FLOOR_DANGER = 5  # extraction(정보·앱 요구) 이상
# 추적기 점수가 이 이상이면(통화를 의심하는 동안) 빠른 경로 점수의 통화 중 최고값을 유지한다.
FAST_HOLD_TRACKER_RISK = 0.5


async def preload_koelectra_model() -> None:
    """서버 시작 시 KoELECTRA 모델을 미리 로드한다."""
    if not scorer.is_ready():
        _log_koelectra_missing()
        return

    try:
        await asyncio.to_thread(scorer.preload)
    except Exception as exc:
        logger.warning("KoELECTRA 모델 사전 로드 실패. RAG 점수로 대체합니다: %s", exc)


def clear_detector_index() -> None:
    """학습 사례가 바뀌었을 때 RAG 인덱스 캐시를 비운다."""
    detector.clear_index()


def detect_and_persist(
    log_id: int,
    top_k: int = 5,
    use_llm_evidence: bool = True,
    agent_mode: bool = False,
) -> dict:
    """
    누적 통화 내용을 탐지하고 결과와 필요 알림 이력을 DB에 저장한다.

    위험도 점수는 KoELECTRA가 우선 담당하고, RAG/규칙 탐지는 근거와 키워드 생성에 사용한다.

    agent_mode=True(v2 실시간 경로)면 회색지대 LLM 재검토를 건너뛰고, 대화 상태 추적기의
    최신 상태가 있을 때 그 점수와 융합한다. 추적기가 통화 전체를 읽고 의도를 판단하므로
    같은 일을 하는 재검토 호출을 빠른 경로에서 한 번 더 할 필요가 없다.
    """
    call_text = build_call_text(log_id)
    koelectra_score = _score_with_koelectra(log_id)
    if koelectra_score is not None:
        preliminary_patterns = detector.rule_detector.find(call_text)
        koelectra_score = _calibrate_koelectra_score(
            text=call_text,
            raw_score=koelectra_score,
            matched_patterns=preliminary_patterns,
        )

    detection = detector.detect(
        text=call_text,
        top_k=top_k,
        risk_score_override=koelectra_score,
        use_llm_evidence=use_llm_evidence,
    )
    retrieved_case_ids = [case.id for case in detection.retrieved_cases]
    base_model_version = "rag-v1" if koelectra_score is None else "koelectra-v1"
    fast_score = detection.risk_score if koelectra_score is None else koelectra_score

    state = state_repository.get_latest_state(log_id) if agent_mode else None
    if state is not None:
        return _persist_fused(
            log_id=log_id,
            fast_score=fast_score,
            state=state,
            rule_patterns=detection.matched_patterns,
            fallback_evidence=detection.core_evidence,
            retrieved_case_ids=retrieved_case_ids,
            base_model_version=base_model_version,
        )

    if koelectra_score is None:
        risk_score = detection.risk_score
        risk_level = detection.risk_level
        is_phishing = detection.is_phishing
    else:
        risk_score = koelectra_score
        risk_level = risk_level_of(koelectra_score)
        is_phishing = koelectra_score >= TH_WARNING

    # 룰 매칭 근거가 하나도 없으면 KoELECTRA 단독 판단이므로 신뢰도가 낮다.
    # 이 경우 회색지대 상한을 강한경고 직전(TH_DANGER)까지 넓혀, "패턴 없는 80%대"
    # 같은 애매한 고점수(예: 정상 대출 문의 오탐)도 LLM 재검토를 받게 한다.
    gray_zone_high = GRAY_ZONE_HIGH if detection.matched_patterns else TH_DANGER
    if not agent_mode and GRAY_ZONE_LOW <= risk_score < gray_zone_high:
        llm_probability = _gray_zone_llm_review(
            text=call_text,
            risk_score=risk_score,
            matched_patterns=detection.matched_patterns,
        )
        if llm_probability is not None:
            risk_score = round((risk_score + llm_probability) / 2, 4)
            if koelectra_score is not None:
                risk_level = risk_level_of(risk_score)
                is_phishing = risk_score >= TH_WARNING
            else:
                risk_level = detector._risk_level(risk_score)
                is_phishing = risk_score >= 0.6

    return _persist_detection(
        log_id=log_id,
        risk_score=risk_score,
        risk_level=risk_level,
        is_phishing=is_phishing,
        matched_patterns=detection.matched_patterns,
        core_evidence=detection.core_evidence,
        retrieved_case_ids=retrieved_case_ids,
        model_version=base_model_version,
        fast_score=fast_score,
    )


def fuse_risk(fast_score: Optional[float], state: dict) -> float:
    """빠른 경로 점수와 상태 추적기 점수를 하나의 위험도로 합친다.

    추적기는 통화 전체의 의도를 읽으므로 더 큰 가중치를 준다. 빠른 경로는 추적기가
    따라오기 전(수 초)의 조기 신호 역할이다. 추적기가 사기 단계를 확신하면 빠른 경로가
    낮게 보더라도(룰셋이 모르는 신종 수법) 경고 기준선 아래로 내려가지 않게 받친다.
    """
    tracker_risk = float(state["tracker_risk"])
    if fast_score is None:
        fused = tracker_risk
    else:
        fused = FUSION_FAST_WEIGHT * fast_score + (1 - FUSION_FAST_WEIGHT) * tracker_risk

    stage = int(state["stage_index"])
    if stage >= STAGE_FLOOR_WARNING and tracker_risk >= TH_WARNING:
        fused = max(fused, TH_WARNING)
    if stage >= STAGE_FLOOR_DANGER and tracker_risk >= TH_DANGER:
        fused = max(fused, TH_DANGER)
    return round(max(0.0, min(1.0, fused)), 4)


def _held_fast_score(log_id: int, fast_score: Optional[float], state: dict) -> Optional[float]:
    """조각난 실시간 전사에서 빠른 경로 점수가 청크마다 출렁이는 것을 받친다.

    문장이 쪼개져 들어오면 KoELECTRA 점수가 한 청크 사이에 0.96 → 0.02처럼 튄다(추적기 점수는
    완만하게 움직인다). 추적기가 통화를 의심하는 동안에는 통화 중 가장 높았던 빠른 경로 점수를
    유지해, 위험도가 경고 기준선 위아래로 오르내리지 않게 한다. 추적기가 정상 쪽으로 보면
    현재 점수만 쓰므로 정상 통화의 일시적 고점은 남지 않는다.
    """
    if fast_score is None or float(state["tracker_risk"]) < FAST_HOLD_TRACKER_RISK:
        return fast_score
    peak = state_repository.get_peak_fast_score(log_id)
    return fast_score if peak is None else max(fast_score, peak)


def apply_state_fusion(log_id: int) -> Optional[dict]:
    """상태 추적기가 새 상태를 만든 직후, 최신 빠른 경로 점수와 융합해 통화 위험도를 갱신한다."""
    state = state_repository.get_latest_state(log_id)
    if state is None:
        return None

    latest = state_repository.get_latest_detection(log_id)
    return _persist_fused(
        log_id=log_id,
        fast_score=state_repository.get_latest_fast_score(log_id),
        state=state,
        rule_patterns=detector.rule_detector.find(build_call_text(log_id)),
        fallback_evidence=latest["core_evidence"] if latest else "",
        retrieved_case_ids=latest["retrieved_case_ids"] if latest else [],
        base_model_version="tracker",
    )


def _persist_fused(
    log_id: int,
    fast_score: Optional[float],
    state: dict,
    rule_patterns: list[str],
    fallback_evidence: str,
    retrieved_case_ids: list[int],
    base_model_version: str,
) -> dict:
    """융합 점수로 탐지 결과를 저장한다. 근거 문장은 추적기 요약을 우선 사용한다."""
    risk_score = fuse_risk(_held_fast_score(log_id, fast_score, state), state)
    is_phishing = risk_score >= TH_WARNING

    # 규칙 패턴에 추적기가 붙인 수법 태그를 더해 근거 키워드로 쓴다.
    matched_patterns = list(rule_patterns)
    tagged_tactics = [
        tactic
        for message in state_repository.list_tagged_messages(log_id)
        for tactic in message["tactics"]
    ]
    for pattern in tactics_to_patterns(tagged_tactics):
        if pattern not in matched_patterns:
            matched_patterns.append(pattern)

    return _persist_detection(
        log_id=log_id,
        risk_score=risk_score,
        risk_level=risk_level_of(risk_score),
        is_phishing=is_phishing,
        matched_patterns=matched_patterns,
        core_evidence=state["summary"] or fallback_evidence,
        retrieved_case_ids=retrieved_case_ids,
        model_version=f"fusion-v1({base_model_version})",
        fast_score=fast_score,
        phishing_type=state["phishing_type"] if state["scam_type"] != "none" else None,
    )


def _persist_detection(
    log_id: int,
    risk_score: float,
    risk_level: str,
    is_phishing: bool,
    matched_patterns: list[str],
    core_evidence: str,
    retrieved_case_ids: list[int],
    model_version: str,
    fast_score: Optional[float] = None,
    phishing_type: Optional[str] = None,
) -> dict:
    """탐지 결과를 저장하고, 처음 high가 됐으면 알림 이력을 남긴 뒤 응답용 dict를 만든다."""
    previous_level = get_call_log(log_id).risk_level
    saved_result = save_detection_result(
        log_id=log_id,
        risk_score=risk_score,
        risk_level=risk_level,
        detected_label=1 if is_phishing else 0,
        core_evidence=core_evidence,
        matched_patterns=matched_patterns,
        retrieved_case_ids=retrieved_case_ids,
        model_version=model_version,
        phishing_type=phishing_type,
        fast_score=fast_score,
    )

    notification = None
    if risk_level == "high" and previous_level != "high":
        notification = insert_notification(
            log_id=log_id,
            notification=NotificationCreate(
                reason=core_evidence or "보이스피싱 위험이 높게 탐지되었습니다.",
                message="보이스피싱 위험이 높게 탐지되었습니다. 통화를 종료하고 공식 대표번호로 확인하세요.",
                status="sent",
            ),
        )

    latest_call = get_call_log(log_id)

    return {
        "is_phishing": is_phishing,
        "risk_level": risk_level,
        "risk_score": round(risk_score, 4),
        "previous_risk_level": previous_level,
        "phishing_type": latest_call.phishing_type,
        "matched_patterns": matched_patterns,
        "core_evidence": core_evidence,
        "saved_result": saved_result.model_dump(),
        "notification": _compact_notification(notification.model_dump()) if notification else None,
    }


def build_client_text_analysis_response(log_id: int, message: dict, detection: dict) -> dict:
    """테스트/하위 호환용 텍스트 발화 분석 응답을 만든다."""
    if not detection["is_phishing"]:
        return {
            "type": "analysis_ack",
            "log_id": log_id,
            "message_id": message["id"],
            "converted_text": message["content"],
            "message": message,  # 프론트가 turn_index로 발화별 위험도를 매칭한다
            "is_phishing": False,
            "risk_score": detection["risk_score"],
            "risk_level": detection["risk_level"],
            "phishing_type": detection["phishing_type"],
        }

    return {
        "type": "phishing_detected",
        "log_id": log_id,
        "message_id": message["id"],
        "converted_text": message["content"],
        "message": message,  # 프론트가 turn_index로 발화별 위험도를 매칭한다
        "is_phishing": True,
        "risk_score": detection["risk_score"],
        "risk_level": detection["risk_level"],
        "phishing_type": detection["phishing_type"],
        "matched_patterns": detection["matched_patterns"],
        "core_evidence": detection["core_evidence"],
        "notification": detection["notification"],
    }


def build_client_audio_analysis_response(
    log_id: int,
    chunk_index: int,
    saved_messages: list[dict],
    segments: list[dict],
    detection: dict,
) -> dict:
    """실시간 오디오 chunk 전사/분석 결과를 클라이언트 응답 형태로 만든다."""
    converted_text = "\n".join(message["content"] for message in saved_messages)
    base_response = {
        "log_id": log_id,
        "chunk_index": chunk_index,
        "message_count": len(saved_messages),
        "message_ids": [message["id"] for message in saved_messages],
        "converted_text": converted_text,
        "transcripts": [
            {
                "message_id": message["id"],
                "turn_index": message["turn_index"],
                "role": segment.get("speaker", "화자A"),
                "content": message["content"],
                "start_time": segment["start_time"],
                "end_time": segment["end_time"],
                # 통화 시작 후 초(이 통화에서 받은 오디오 누적 길이 기준). 정리본(transcript_refined)과 시각을 맞출 때 쓴다.
                "t_start": segment.get("t_start"),
                "t_end": segment.get("t_end"),
            }
            for message, segment in zip(saved_messages, segments)
        ],
        "is_phishing": detection["is_phishing"],
        "risk_score": detection["risk_score"],
        "risk_level": detection["risk_level"],
        "phishing_type": detection["phishing_type"],
    }

    if not detection["is_phishing"]:
        return {
            "type": "audio_analysis_ack",
            **base_response,
        }

    return {
        "type": "audio_phishing_detected",
        **base_response,
        "matched_patterns": detection["matched_patterns"],
        "core_evidence": detection["core_evidence"],
        "notification": detection["notification"],
    }


def _score_with_koelectra(log_id: int) -> Optional[float]:
    """KoELECTRA 모델이 준비되어 있으면 위험도 점수를 계산하고, 실패 시 None을 반환한다."""
    if not scorer.is_ready():
        _log_koelectra_missing()
        return None

    try:
        return scorer.score(list_call_messages(log_id))
    except Exception as exc:
        logger.warning("KoELECTRA 점수 계산 실패. RAG 점수로 대체합니다: %s", exc)
        return None


def _compact_notification(notification: dict) -> dict:
    """클라이언트 응답에는 알림 표시용 최소 필드만 포함한다."""
    return {
        "id": notification["id"],
        "message": notification["message"],
        "status": notification["status"],
        "created_at": notification["created_at"],
    }


def _log_koelectra_missing() -> None:
    """KoELECTRA 모델 미설정 상태를 서버 로그에 한 번만 명확히 남긴다."""
    global _koelectra_missing_logged
    if _koelectra_missing_logged:
        return

    _koelectra_missing_logged = True
    logger.warning(
        "KoELECTRA 모델 폴더가 없어 KoELECTRA 위험도 계산을 사용하지 않습니다. "
        "현재 요청은 RAG/규칙 기반 점수로 대체됩니다. "
        "학습 후 모델을 생성하세요. model_dir=%s",
        MODEL_DIR,
    )


def _calibrate_koelectra_score(text: str, raw_score: float, matched_patterns: list[str]) -> float:
    """
    KoELECTRA가 정상 금융상담을 피싱으로 과탐지하는 경우를 보정한다.

    예: "비밀번호나 인증번호를 요구하지 않습니다"처럼 위험 키워드가 들어 있지만
    실제 의미는 안전 안내인 문장은 모델 점수를 낮춘다.
    """
    if raw_score < TH_WARNING:
        return raw_score

    if _has_strong_phishing_context(matched_patterns):
        return raw_score

    if _has_normal_finance_context(text):
        logger.info(
            "정상 금융상담 문맥으로 KoELECTRA 점수를 보정합니다. raw_score=%.4f calibrated_score=0.4200",
            raw_score,
        )
        return min(raw_score, 0.42)

    # 룰 매칭 근거가 하나도 없는데 KoELECTRA 혼자 고위험을 주장하는 경우.
    # 실제 보이스피싱은 거의 항상 금전이체유도/대출사칭/개인정보요구 등 룰 패턴 중
    # 하나 이상에 걸리므로, 근거 없이 튀는 고점수는 대부분 짧은 발화(인사말 등)를
    # 오탐한 것이다. 다만 KoELECTRA가 극단적으로 확신하는 경우(0.97+)는 룰셋이
    # 못 잡은 새로운 수법일 수 있어 그대로 통과시킨다.
    if not matched_patterns and raw_score < 0.97:
        calibrated = min(raw_score, TH_DANGER - 0.01)
        logger.info(
            "매칭된 위험 패턴 없이 KoELECTRA만 고위험을 주장해 점수를 보정합니다. "
            "raw_score=%.4f calibrated_score=%.4f",
            raw_score,
            calibrated,
        )
        return calibrated

    return raw_score


def _gray_zone_llm_review(
    text: str, risk_score: float, matched_patterns: list[str]
) -> Optional[float]:
    """회색지대(GRAY_ZONE_LOW~HIGH) 점수를 LLM에게 통화 전문을 직접 읽혀 재검토시킨다.

    API 키가 없거나 호출이 실패하면 None을 반환해 원래 점수를 그대로 쓰게 한다
    (LLM 장애가 탐지 자체를 막지 않도록).
    """
    if not claude_client.is_configured():
        return None

    try:
        prompt = f"""너는 보이스피싱 탐지 시스템의 2차 검토 모듈이다.
1차 모델(KoELECTRA/규칙 기반)이 이 통화의 보이스피싱 확률을 {risk_score * 100:.1f}%로
애매하게(회색지대) 판단했다. 아래 통화 내용을 직접 읽고 보이스피싱일 확률을 다시 추정하라.

판단 기준:
- 표면적 단어(대출, 계좌, 확인 등)만으로 판단하지 말고 대화의 실제 의도를 본다.
- 실제 금전 이체, 개인정보/인증번호 요구, 앱 설치 유도, 긴급성/비밀유지 압박 등
  실질적 피해로 이어질 시도가 있는지를 중심으로 본다.
- 1차 탐지된 위험 신호: {', '.join(matched_patterns) if matched_patterns else '없음'}

통화 내용:
{text}

아래 JSON 형식으로만 답하라. 다른 텍스트나 설명은 출력하지 마라.
{{"phishing_probability": 0에서 1 사이 숫자}}"""

        raw = claude_client.complete(
            prompt,
            system="보이스피싱 여부를 신중하고 일관되게 재평가한다.",
        )
        data = json.loads(_extract_json_object(raw))
        probability = float(data["phishing_probability"])
        return max(0.0, min(1.0, probability))
    except Exception as exc:
        logger.warning("회색지대 LLM 재검토 실패. 기존 점수를 그대로 사용합니다: %s", exc)
        return None


def _extract_json_object(raw: str) -> str:
    """모델 응답에서 JSON 객체 부분만 추출한다."""
    raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"JSON 객체 없음: {raw[:80]!r}")
    return raw[start : end + 1]


def _has_strong_phishing_context(matched_patterns: list[str]) -> bool:
    """명확한 피싱 조합이면 정상 상담 보정을 적용하지 않는다."""
    matched = set(matched_patterns)

    if "수사기관/공공기관 사칭" in matched and (
        "범죄 연루 압박" in matched or "금전 이체 유도" in matched
    ):
        return True

    if "대출 사칭/상환금 요구" in matched and "금전 이체 유도" in matched:
        return True

    if "개인정보/인증 요구" in matched and "앱 설치/원격제어 유도" in matched:
        return True

    if "금전 이체 유도" in matched and "긴급성/비밀 유지 압박" in matched:
        return True

    return False


def _has_normal_finance_context(text: str) -> bool:
    """정상 계좌 개설/상담에서 자주 나오는 안전 문맥을 찾는다."""
    safe_patterns = [
        r"계좌\s*개설.*문의",
        r"비대면\s*계좌\s*개설",
        r"본인인증\s*후\s*진행",
        r"신분증만\s*준비",
        r"앱에서도\s*확인",
        r"공식\s*앱",
        r"상담원.*(비밀번호|인증번호).*요구하지",
        r"(비밀번호|인증번호).*요구하지",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in safe_patterns)
