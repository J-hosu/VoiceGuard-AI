"""능동형 대응 에이전트.

대화 상태(단계·유형·순응도)와 융합 위험도를 보고 "지금 무엇을 할지"를 결정한다.

역할 분담:
  - 어떤 행동이 가능한지(후보)는 아래 규칙표가 정한다. 같은 행동을 반복하지 않게 막는 것도 규칙이다.
  - 경고 문구, 검증 질문, 어느 기관 번호를 안내할지는 Claude가 통화 내용에 맞춰 쓴다.
  - 전화번호, 지급정지 절차처럼 틀리면 피해가 커지는 내용은 Claude가 쓰지 않고 고정 표에서 가져온다.
  - Claude 호출이 실패해도 고정 문구로 행동은 반드시 나간다.

docs/API_SPEC_V2.md 4.2절(agent_action), 5.4절(행동 응답) 참고.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app import state_repository
from backend.app.codes import DAMAGE_ANSWERS, FOLLOWUP_ANSWERS, OFFICIAL_NUMBERS, STAGE_LABELS
from backend.app.repository import get_call_log
from backend.app.services import claude_client, webhook_emitter


logger = logging.getLogger(__name__)

# 통화 중 후속 확인을 화면에 띄우기까지의 지연(초)과 최대 반복 횟수.
IN_CALL_FOLLOWUP_DELAY_SEC = 45
MAX_IN_CALL_FOLLOWUPS = 3
# 통화 종료 후 후속 확인까지의 지연(초). n8n이 이 값만큼 기다렸다가 다시 묻는다.
POST_CALL_FOLLOWUP_DELAY_SEC = 600

# Claude가 문구를 쓰는 행동. 나머지는 고정 문구다.
LLM_AUTHORED = ("warn", "official_number", "verify_guide")

SYSTEM_PROMPT = """너는 보이스피싱 피해를 실시간으로 막는 앱의 '대응 에이전트'다.
사용자는 지금 사기일 수 있는 전화를 받고 있고, 네가 쓴 문구는 통화 중인 사용자의 화면에 바로 뜬다.
사용자는 긴장한 상태에서 화면을 힐끗 볼 뿐이다. 그래서 한눈에 읽히고 바로 행동할 수 있어야 한다.

[쓰는 법]
- title은 20자 안팎의 행동 지시. message는 80자 이내의 한두 문장. 평문만 쓴다.
- 이 통화에서 실제로 나온 내용(사칭한 기관, 요구한 것)을 짚어서 쓴다. 일반론은 피한다.
- 겁을 주기보다 무엇을 하면 되는지를 말한다. 사기라고 단정하지 말고 "의심됩니다"처럼 쓴다.
- 전화번호와 웹 주소는 쓰지 않는다. official_number 카드에는 시스템이 해당 기관의 공식 번호를
  바로 아래에 붙여 보여주므로 "아래 번호로"라고 쓰면 된다. 번호를 검색해 찾으라고 하지 않는다.
- reason에는 이 행동을 지금 고른 이유를 60자 이내로 적는다. 어떤 단계·정황 때문인지 드러나게.

[행동별 지침]
- warn: 지금 상황을 한 줄로 알리고 가장 중요한 행동 하나(끊기, 요구에 응하지 않기)를 말한다.
- official_number: 상대가 내세운 기관에 사용자가 직접 확인하도록 안내한다. org_key는 주어진 목록에서
  이 통화에 가장 맞는 것 하나를 고른다. 맞는 기관이 없으면 use를 false로 한다.
- verify_guide: 진짜 직원이라면 답할 수 있고 사기범은 곤란해할 질문을 questions에 2~3개 쓴다.
  사용자가 상대에게 그대로 읽을 수 있는 말투로. 지금 묻는 것이 오히려 위험하거나 의미 없으면 use를 false로 한다.

후보로 주어진 행동만 작성한다. warn이 후보에 있으면 반드시 작성하고 use는 true로 둔다."""

# 스키마는 요청마다 달라지면 안 된다. 구조화 출력은 스키마를 처음 볼 때 문법을 컴파일하느라
# 수십 초가 걸리고 이후에는 캐시되므로, 후보가 무엇이든 같은 스키마를 쓰고 코드에서 걸러낸다.
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action_type": {"type": "string", "enum": list(LLM_AUTHORED)},
                    "use": {"type": "boolean"},
                    "title": {"type": "string"},
                    "message": {"type": "string"},
                    "reason": {"type": "string"},
                    "org_key": {"type": "string", "enum": [*OFFICIAL_NUMBERS, "none"]},
                    "questions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action_type", "use", "title", "message", "reason", "org_key", "questions"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


def decide(log_id: int, state: dict, detection: dict) -> list[dict]:
    """새 상태에 대해 실행할 행동을 결정·저장하고, 새로 만든 행동 목록을 반환한다."""
    candidates = _eligible_actions(log_id, state, detection)
    if not candidates:
        return []

    authored = _write_with_claude(log_id, state, [c for c in candidates if c in LLM_AUTHORED])
    created: list[dict] = []
    for action_type in candidates:
        spec = _build_action(action_type, state, detection, authored.get(action_type), log_id)
        if spec is None:
            continue
        created.append(
            state_repository.insert_action(
                log_id=log_id,
                state_version=state["state_version"],
                action_type=action_type,
                **spec,
            )
        )
        if action_type == "guardian_alert":
            webhook_emitter.emit("incident.high_risk", _event_data(log_id, state, detection))
    return created


def _event_data(log_id: int, state: Optional[dict], detection: Optional[dict] = None) -> dict:
    """n8n으로 보내는 이벤트 본문의 공통 부분."""
    call = get_call_log(log_id)
    stage = state["stage"] if state else "none"
    base = webhook_emitter.public_base_url()
    return {
        "log_id": log_id,
        "device_id": call.device_id,
        "risk_score": round(detection["risk_score"] if detection else call.risk_score, 4),
        "risk_level": detection["risk_level"] if detection else call.risk_level,
        "phishing_type": (state["phishing_type"] if state else "") or call.phishing_type or "",
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "compliance": state["compliance"] if state else "none",
        "summary": (state["summary"] if state else "") or call.core_evidence or "",
        "dashboard_url": f"{base}/dashboard#case={log_id}" if base else "",
    }


def handle_response(
    log_id: int,
    action_id: int,
    response: str,
    answer: Optional[str] = None,
    source: str = "app",
) -> dict:
    """사용자의 행동 카드 반응을 기록하고, 후속 확인 답이면 다음 행동을 결정한다.

    반환: {"action": 갱신된 행동, "next_actions": 새로 만든 행동 목록}
    """
    action = state_repository.get_action(action_id)
    if action["log_id"] != log_id:
        raise ValueError(f"action_id={action_id}는 log_id={log_id}의 행동이 아닙니다.")
    if response not in ("acknowledged", "dismissed", "answered"):
        raise ValueError("response는 acknowledged, dismissed, answered 중 하나여야 합니다.")

    if response != "answered":
        return {"action": state_repository.update_action_response(action_id, response, ""), "next_actions": []}

    if action["action_type"] != "followup_check":
        raise ValueError("answered는 followup_check 행동에만 쓸 수 있습니다.")
    if answer not in FOLLOWUP_ANSWERS:
        raise ValueError(f"answer는 {', '.join(FOLLOWUP_ANSWERS)} 중 하나여야 합니다.")

    updated = state_repository.update_action_response(action_id, "answered", answer)
    next_actions = _after_followup(log_id, answer, action["state_version"], source)
    return {"action": updated, "next_actions": next_actions}


def on_call_end(log_id: int) -> list[dict]:
    """통화가 끝났을 때 실행할 행동(신고 안내, 통화 후 후속 확인)을 만든다."""
    call = get_call_log(log_id)
    if call.risk_level not in ("medium", "high"):
        return []

    state = state_repository.get_latest_state(log_id)
    version = state["state_version"] if state else 0
    existing = state_repository.list_actions(log_id)
    created: list[dict] = []

    if not _has_action(existing, "report_guide"):
        created.append(state_repository.insert_action(log_id, version, "report_guide", **_report_guide(log_id)))

    # 통화 중에 이미 결과(피해 발생·오탐·정상 확인)를 답했다면 다시 묻지 않는다.
    settled = any(
        action["action_type"] == "followup_check" and action["response"] not in ("", "still_talking")
        for action in existing
    )
    if not settled and not _has_action(existing, "followup_check", phase="post_call"):
        created.append(
            state_repository.insert_action(log_id, version, "followup_check", **_followup("post_call"))
        )
    # 통화 종료 알림은 통화당 한 번만 보낸다(종료 처리가 두 번 불려도 created가 비어 있으면 건너뜀).
    if created:
        webhook_emitter.emit("call.ended", _event_data(log_id, state))
    return created


# ---------------------------------------------------------------- 후보 결정 (규칙)


def _has_action(actions: list[dict], action_type: str, **payload_match: object) -> bool:
    """같은 종류(와 payload 조건)의 행동이 이미 있는지 확인한다."""
    for action in actions:
        if action["action_type"] != action_type:
            continue
        if all(action["payload"].get(key) == value for key, value in payload_match.items()):
            return True
    return False


def _eligible_actions(log_id: int, state: dict, detection: dict) -> list[str]:
    """현재 상태에서 새로 실행할 수 있는 행동 종류를 우선순위 순으로 반환한다."""
    existing = state_repository.list_actions(log_id)
    risk_level = detection["risk_level"]
    stage = state["stage_index"]
    is_scam = state["scam_type"] != "none"
    compliance = state["compliance"]
    candidates: list[str] = []

    if risk_level == "high" and not _has_action(existing, "warn", level="danger"):
        candidates.append("warn")
    elif risk_level == "medium" and not _has_action(existing, "warn"):
        candidates.append("warn")

    if risk_level in ("medium", "high") and is_scam and stage >= 2:
        if not _has_action(existing, "official_number"):
            candidates.append("official_number")
        if compliance != "completed" and not _has_action(existing, "verify_guide"):
            candidates.append("verify_guide")

    if risk_level == "high" and (compliance in ("complying", "completed") or stage >= 6):
        device_id = get_call_log(log_id).device_id
        has_channel = state_repository.count_active_guardians(device_id) > 0 or webhook_emitter.is_configured()
        if has_channel and not _has_action(existing, "guardian_alert"):
            candidates.append("guardian_alert")

    if compliance == "completed" and risk_level in ("medium", "high") and not _has_action(existing, "block_guide"):
        candidates.append("block_guide")

    if risk_level == "high" and not _has_action(existing, "followup_check", phase="in_call"):
        candidates.append("followup_check")

    return candidates


# ---------------------------------------------------------------- 행동 조립


def _build_action(
    action_type: str,
    state: dict,
    detection: dict,
    authored: Optional[dict],
    log_id: int,
) -> Optional[dict]:
    """행동 종류별로 저장할 필드(title, message, reason, payload)를 만든다. None이면 건너뛴다."""
    if action_type == "warn":
        level = "danger" if detection["risk_level"] == "high" else "caution"
        text = authored or _warn_template(state, level)
        return {**_text_fields(text), "payload": {"level": level}}

    if action_type == "official_number":
        if authored is not None and not authored.get("use", True):
            return None
        org_key = (authored or {}).get("org_key")
        if org_key not in OFFICIAL_NUMBERS:
            org_key = _default_org_key(state["scam_type"])
        contact = OFFICIAL_NUMBERS[org_key]
        text = authored or {
            "title": f"{contact['org']}에 직접 확인하세요",
            "message": "통화를 끊고, 상대가 알려준 번호가 아닌 공식 번호로 직접 걸어 확인하세요.",
            "reason": f"{STAGE_LABELS[state['stage']]} 단계에 들어서 직접 확인이 필요합니다.",
        }
        return {**_text_fields(text), "payload": dict(contact)}

    if action_type == "verify_guide":
        if authored is not None and not authored.get("use", True):
            return None
        questions = [q.strip() for q in (authored or {}).get("questions", []) if q.strip()][:3]
        if not questions:
            questions = _default_questions(state["scam_type"])
        text = authored or {
            "title": "이렇게 되물어 보세요",
            "message": "진짜 직원이라면 답할 수 있는 질문입니다. 머뭇거리거나 화를 내면 끊으세요.",
            "reason": "상대의 신원이 확인되지 않았습니다.",
        }
        return {**_text_fields(text), "payload": {"questions": questions}}

    if action_type == "guardian_alert":
        device_id = get_call_log(log_id).device_id
        return {
            "title": "보호자에게 알렸습니다",
            "message": "등록된 보호자에게 지금 통화가 위험할 수 있다고 알렸습니다.",
            "reason": "위험도가 높고 상대의 요구를 따르는 정황이 있습니다.",
            "payload": {"guardian_count": state_repository.count_active_guardians(device_id)},
        }

    if action_type == "block_guide":
        return _block_guide("money", "송금·정보 제공을 이미 했다는 발화가 있었습니다.")

    if action_type == "followup_check":
        return _followup("in_call")

    return None


def _text_fields(text: dict) -> dict:
    return {
        "title": str(text.get("title", "")).strip()[:60] or "주의가 필요합니다",
        "message": str(text.get("message", "")).strip()[:200] or "통화 내용을 다시 확인하세요.",
        "reason": str(text.get("reason", "")).strip()[:200],
    }


def _warn_template(state: dict, level: str) -> dict:
    if level == "danger":
        return {
            "title": "지금 통화를 끊으세요",
            "message": "보이스피싱 위험이 높습니다. 어떤 요구에도 응하지 말고 통화를 종료하세요.",
            "reason": f"{STAGE_LABELS[state['stage']]} 단계까지 진행됐습니다.",
        }
    return {
        "title": "의심스러운 통화입니다",
        "message": "보이스피싱에서 자주 쓰이는 흐름이 보입니다. 개인정보나 돈 이야기가 나오면 응하지 마세요.",
        "reason": f"{STAGE_LABELS[state['stage']]} 단계의 정황이 탐지됐습니다.",
    }


def _default_org_key(scam_type: str) -> str:
    return {
        "agency": "prosecution",
        "loan": "fss",
        "kidnap": "police",
        "acquaintance": "family",
        "smishing": "kisa",
    }.get(scam_type, "police")


def _default_questions(scam_type: str) -> list[str]:
    if scam_type == "agency":
        return [
            "소속과 성함, 사무실 대표번호를 알려주세요. 제가 직접 다시 걸겠습니다.",
            "사건 관련 서류를 등기 우편으로 보내주실 수 있나요?",
        ]
    if scam_type == "loan":
        return [
            "금융회사 등록번호와 지점 대표번호를 알려주세요. 제가 직접 확인하겠습니다.",
            "대출 전에 먼저 돈을 보내야 하는 이유가 무엇인가요?",
        ]
    if scam_type in ("kidnap", "acquaintance"):
        return [
            "본인과 직접 통화하게 해주세요.",
            "우리 가족만 아는 것을 하나 물어보겠습니다.",
        ]
    return [
        "어느 회사의 누구신지, 대표번호를 알려주세요. 제가 직접 다시 걸겠습니다.",
        "이 내용을 공식 앱이나 홈페이지에서도 확인할 수 있나요?",
    ]


def _block_guide(kind: str, reason: str) -> dict:
    """피해 유형별 긴급 조치. 절차와 번호는 고정 문구다."""
    guides = {
        "money": {
            "title": "지금 바로 지급정지를 요청하세요",
            "message": "돈을 보냈다면 시간이 가장 중요합니다. 아래 순서대로 진행하세요.",
            "steps": [
                "112 또는 돈을 보낸 은행 고객센터에 전화해 '지급정지'를 요청하세요.",
                "경찰서에서 '사건사고사실확인원'을 발급받아 3영업일 안에 은행에 피해구제를 신청하세요.",
                "통화 녹음과 이체 내역은 지우지 말고 보관하세요.",
            ],
            "phones": ["112", "1332"],
        },
        "info": {
            "title": "명의도용을 막는 조치를 하세요",
            "message": "알려준 정보로 계좌나 휴대전화가 개설될 수 있습니다. 아래 순서대로 진행하세요.",
            "steps": [
                "금융감독원 '개인정보노출자 사고예방시스템'에 등록해 신규 계좌 개설과 대출을 막으세요.",
                "'계좌정보통합관리서비스'에서 내 계좌를 확인하고 일괄 지급정지를 신청하세요.",
                "'엠세이퍼'에서 내 명의로 개통된 휴대전화가 있는지 확인하세요.",
                "알려준 비밀번호는 바꾸고 인증서는 재발급하세요.",
            ],
            "phones": ["1332", "118"],
        },
        "app": {
            "title": "휴대전화를 먼저 차단하세요",
            "message": "설치한 앱이 전화와 문자를 가로챌 수 있습니다. 아래 순서대로 진행하세요.",
            "steps": [
                "휴대전화를 비행기 모드로 바꾸거나 전원을 끄세요.",
                "다른 전화기로 112나 은행 고객센터에 연락해 지급정지를 요청하세요.",
                "설치한 앱을 삭제하고 통신사 대리점이나 서비스센터에서 점검받으세요.",
                "휴대전화에 저장해 둔 신분증 사진과 인증서는 재발급하세요.",
            ],
            "phones": ["112", "118"],
        },
    }
    guide = guides[kind]
    return {
        "title": guide["title"],
        "message": guide["message"],
        "reason": reason,
        "payload": {"kind": kind, "steps": guide["steps"], "phones": guide["phones"]},
    }


def _report_guide(log_id: int) -> dict:
    return {
        "title": "신고용 요약을 준비했습니다",
        "message": "통화 내용, 단계별 경과, 단서를 정리했습니다. 경찰이나 금융회사에 제출할 수 있습니다.",
        "reason": "위험 통화로 분류되어 기록을 정리했습니다.",
        "payload": {"report_url": f"/calls/{log_id}/report"},
    }


def _followup(phase: str) -> dict:
    """후속 확인 질문. phase는 in_call(통화 중) 또는 post_call(통화 후)."""
    if phase == "in_call":
        answers = {
            "hung_up": "끊었어요",
            "still_talking": "아직 통화 중이에요",
            "false_alarm": "잘못된 경고예요",
        }
        return {
            "title": "통화를 끊으셨나요?",
            "message": "위험 경고 이후 상황을 알려주시면 다음 조치를 안내하겠습니다.",
            "reason": "강한 경고 이후 위험이 해소됐는지 확인합니다.",
            "payload": {
                "phase": phase,
                "question": "통화를 끊으셨나요?",
                "options": [{"answer": key, "label": label} for key, label in answers.items()],
                "due_in_sec": IN_CALL_FOLLOWUP_DELAY_SEC,
            },
        }

    answers = {
        "hung_up": "끊었고 피해는 없어요",
        "verified_safe": "직접 확인해 보니 정상이었어요",
        "shared_info": "개인정보를 알려줬어요",
        "installed_app": "앱을 설치했어요",
        "transferred_money": "돈을 보냈어요",
        "false_alarm": "잘못된 경고였어요",
    }
    return {
        "title": "통화 이후 괜찮으신가요?",
        "message": "피해가 있었다면 지금 알려주세요. 바로 필요한 조치를 안내하겠습니다.",
        "reason": "위험 통화가 끝난 뒤 피해 여부를 확인합니다.",
        "payload": {
            "phase": phase,
            "question": "통화 이후 상황을 알려주세요.",
            "options": [{"answer": key, "label": label} for key, label in answers.items()],
            "due_in_sec": POST_CALL_FOLLOWUP_DELAY_SEC,
        },
    }


# ---------------------------------------------------------------- 후속 확인 답 처리


def _after_followup(log_id: int, answer: str, state_version: int, source: str) -> list[dict]:
    """후속 확인 답에 따라 다음 행동을 만든다. 결과는 피드백으로도 남겨 학습 루프에 쓴다."""
    existing = state_repository.list_actions(log_id)
    created: list[dict] = []

    def add(action_type: str, spec: dict) -> None:
        created.append(state_repository.insert_action(log_id, state_version, action_type, **spec))

    if answer != "still_talking":
        webhook_emitter.emit(
            "followup.answered",
            {
                **_event_data(log_id, state_repository.get_latest_state(log_id)),
                "answer": answer,
                "answer_label": FOLLOWUP_ANSWERS.get(answer, answer),
                "damage": answer in DAMAGE_ANSWERS,
                "source": source,
            },
        )

    if answer in ("false_alarm", "verified_safe"):
        state_repository.insert_feedback(log_id, "false_positive", comment=FOLLOWUP_ANSWERS[answer], source=f"followup:{source}")
        return created

    if answer == "still_talking":
        add(
            "warn",
            {
                "title": "지금 바로 끊으세요",
                "message": "끊어도 불이익은 없습니다. 끊은 뒤 공식 번호로 직접 확인하면 됩니다.",
                "reason": "강한 경고 이후에도 통화가 이어지고 있습니다.",
                "payload": {"level": "danger", "repeat": True},
            },
        )
        in_call_count = sum(
            1
            for action in existing
            if action["action_type"] == "followup_check" and action["payload"].get("phase") == "in_call"
        )
        if in_call_count < MAX_IN_CALL_FOLLOWUPS:
            add("followup_check", _followup("in_call"))
        return created

    if answer in DAMAGE_ANSWERS:
        kind = {"transferred_money": "money", "shared_info": "info", "installed_app": "app"}[answer]
        if not _has_action(existing, "block_guide", kind=kind):
            add("block_guide", _block_guide(kind, f"사용자 답변: {FOLLOWUP_ANSWERS[answer]}"))
        state_repository.insert_feedback(
            log_id,
            "confirmed_phishing",
            comment=FOLLOWUP_ANSWERS[answer],
            damage={"answer": answer},
            source=f"followup:{source}",
        )

    if not _has_action(existing, "report_guide"):
        add("report_guide", _report_guide(log_id))
    return created


# ---------------------------------------------------------------- Claude 문구 작성


def _write_with_claude(log_id: int, state: dict, action_types: list[str]) -> dict[str, dict]:
    """후보 행동의 문구를 Claude로 작성한다. 실패하면 빈 dict를 반환해 고정 문구를 쓰게 한다."""
    if not action_types or not claude_client.is_configured():
        return {}

    recent = state_repository.list_tagged_messages(log_id)[-10:]
    transcript = "\n".join(f"{m['turn_index']}. [{m['role']}] {m['content']}" for m in recent)
    orgs = "\n".join(f"- {key}: {value['org']}" for key, value in OFFICIAL_NUMBERS.items())
    prompt = f"""[현재 상태]
단계: {state['stage']} ({STAGE_LABELS[state['stage']]})
유형: {state['scam_type']} ({state['phishing_type']})
사용자 순응도: {state['compliance']}
요약: {state['summary']}

[최근 발화]
{transcript}

[작성할 후보 행동]
{', '.join(action_types)}

[org_key 목록] (official_number에만 사용. 다른 행동은 none)
{orgs}

후보 행동 각각을 작성하라. questions는 verify_guide에만 채우고 나머지는 빈 배열로 둔다."""

    try:
        output = claude_client.complete_json(
            prompt,
            schema=OUTPUT_SCHEMA,
            system=SYSTEM_PROMPT,
            model=claude_client.agent_model(),
            effort="low",
            max_tokens=2000,
        )
    except Exception as exc:
        logger.warning("대응 에이전트 문구 작성 실패. 고정 문구를 사용합니다: %s", exc)
        return {}

    authored: dict[str, dict] = {}
    for item in output.get("actions", []):
        action_type = item.get("action_type")
        if action_type in action_types and action_type not in authored:
            authored[action_type] = item
    return authored
