"""대화 상태 추적기.

통화에 새 발화가 쌓일 때마다 Claude에게 "이전 상태 + 전사문"을 주고, 사기 단계·유형·
화자 역할·발화별 수법 태그·피해자 순응도·수사 단서를 구조화 출력으로 받아 저장한다.
빠른 경로(KoELECTRA/규칙)와 달리 대화의 의도와 흐름을 읽는 느린 경로다.

docs/API_SPEC_V2.md 2절(코드값), 4.2절(state_update) 참고.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.app import state_repository
from backend.app.codes import COMPLIANCE, ENTITY_TYPES, ROLES, SCAM_TYPES, STAGES, TACTICS, stage_index
from backend.app.services import claude_client


logger = logging.getLogger(__name__)

# 프롬프트에 싣는 최근 발화 수. 그보다 앞선 내용은 이전 상태의 요약으로 전달된다.
MAX_TRANSCRIPT_TURNS = 80
# 새 발화가 이 글자 수보다 적으면(추임새 등) LLM을 부르지 않고 다음 발화를 기다린다.
MIN_NEW_CHARS = 8

SYSTEM_PROMPT = """너는 보이스피싱 피해를 실시간으로 막는 앱의 '대화 상태 추적기'다.
사용자가 통화하는 동안 전사된 발화가 몇 초 간격으로 쌓이고, 그때마다 네가 호출된다.
네 출력은 화면의 단계 표시, 경고 문구 선택, 보호자 알림 여부를 결정하는 데 쓰인다.
그래서 정상 통화를 사기로 모는 것도, 진행 중인 사기를 놓치는 것도 모두 실제 피해로 이어진다.

[입력의 한계]
- 전사문은 음성 인식 결과라 오탈자와 끊긴 문장이 있다. 뜻이 통하는 범위에서 읽어라.
  기관명·이름이 어색하게 적힌 것은 인식 오류일 수 있으니 그것만으로 의심하지 않는다.
- 한 문장이 여러 발화로 쪼개져 들어온다. 조각 하나가 아니라 이어진 흐름으로 판단하라.
- 화자 구분이 없다. 한 발화에 두 사람의 말이 섞여 있을 수도 있다. 내용으로 역할을 추정하라.

[역할 role]
- suspect: 전화를 건 상대방(사기 의심 대상이 될 수 있는 쪽). 정상 통화라면 상담원·지인 등 상대방.
- user: 앱 사용자(보호 대상).
- unknown: 정말 구분이 안 되는 발화에만 쓴다. 안내·요구를 하는 쪽이 suspect, 그에 답하거나 묻는 쪽이 user다.
  정상 통화에서도 역할은 똑같이 구분한다.

[단계 stage] 지금까지 도달한 가장 높은 단계 하나를 고른다.
- none: 정상 통화이거나 아직 판단할 근거가 없다.
- approach: 접근. 신원과 용건을 밝히며 대화를 연다 (사기 의심 정황이 있을 때만).
  용건이 대출·계좌·카드·수사라는 것만으로는 approach가 아니다. 아래 위험 신호가 하나도 없으면 none이다.
- impersonation: 사칭·신뢰 형성. 기관명, 직함, 사건번호 등을 내세워 믿게 만든다.
- pressure: 위기 조성. 범죄 연루, 처벌, 계좌 동결, 가족 위험, 시간 압박으로 불안하게 한다.
- isolation: 고립. 비밀 유지, 통화 유지, 주변에 알리지 말 것, 장소 이동을 요구한다.
- extraction: 정보·앱 요구. 개인정보, 인증번호, 비밀번호, 원격제어 앱 설치, 링크 접속을 요구한다.
- payment: 금전 요구. 이체, 송금, 현금 인출·전달, 선입금, 상품권 구매를 요구한다.
단계를 건너뛰는 수법도 있으니 실제로 나타난 가장 높은 단계를 고르면 된다.
정상 통화로 판단하면 stage와 scam_type을 모두 none으로 둔다.
이전 상태는 참고일 뿐이다. 새 발화에서 정상 신호(지점 방문·공식 대표번호 안내, 보낼 돈이 없다는 설명,
천천히 확인하라는 권유)가 나오고 위험 신호가 없으면 단계와 위험도를 주저 없이 none과 낮은 값으로 되돌린다.

[유형 scam_type]
none(정상), agency(수사기관·금융당국 사칭), loan(대출 빙자), kidnap(납치·사고 협박),
acquaintance(가족·지인 사칭), smishing(문자·택배·카드 배송 등으로 링크나 앱 설치 유도), other.

[수법 태그 tactics] 발화 하나에 여러 개 붙일 수 있다. 해당 없으면 빈 배열.
태그는 상대방(suspect)의 행동을 가리킨다. 사용자(user) 발화에는 정상 신호 태그를 포함해 어떤 태그도 붙이지 않는다.
authority_claim 기관·직함 사칭 / crime_involvement 범죄 연루 통보 / legal_threat 구속·처벌 위협 /
urgency 시간 압박 / secrecy 비밀 유지 강요 / isolation 통화 유지·이동 지시 /
info_request 개인정보·인증번호·비밀번호 요구 / app_install 앱 설치·원격제어 유도 / link_click 링크 접속 유도 /
money_transfer 이체·송금 요구 / cash_withdrawal 현금 인출·전달 요구 / loan_bait 저금리·선상환 미끼 /
official_channel (정상 신호) 공식 앱·지점·대표번호로 직접 처리하라고 안내 /
no_pressure (정상 신호) 거절을 받아들이거나 천천히 결정하라고 권유.
사기 수법 태그는 그 행위를 실제로 하는 발화에만 붙인다. "인증번호는 절대 요구하지 않습니다"처럼
위험 행위를 부정하거나 조심하라고 알려주는 발화에는 붙이지 않는다.
authority_claim은 사칭이 의심될 때만 붙인다. 정상 통화에서 상담원이 소속을 밝히는 것은 해당하지 않는다.

[순응도 compliance] 사용자가 상대의 요구를 얼마나 따르고 있는가.
none(따르는 정황 없음 또는 사용자 발화 없음), hesitant(불안해하거나 망설임),
complying(요구에 응하겠다고 하거나 따르는 중), completed(송금·정보 제공·앱 설치를 이미 했다고 말함).

[위험도 risk] 이 통화가 보이스피싱일 확률을 0~1로.
- 단어가 아니라 의도를 본다. 대출·계좌·검찰이라는 단어가 나와도 정상 상담이나 예방 안내일 수 있다.
- 은행·기관의 정상 영업, 공식 창구 안내, 보이스피싱 예방 교육, 가족 간 일상적 송금 부탁은 낮게 준다.
- 위험 신호는 요구와 압박이다: 송금·선입금·수수료·기존 대출 상환 요구, 개인정보·인증번호 요구,
  앱 설치·링크 접속 유도, 비밀 유지·통화 유지 강요, 범죄 연루·처벌 위협, 지금 당장 하라는 재촉.
  이런 신호가 하나도 없이 상대가 소속을 밝히고 용건(대출 심사 결과, 서류 안내, 방문 일정 등)을
  설명하는 통화는 주제가 금융이어도 0.3을 넘기지 않는다. 사용자가 먼저 신청한 건에 대한 안내면 더 낮다.
- 기관을 사칭하며 사건 연루를 통보하는 초기 단계는 금전 요구가 아직 없어도 0.6 이상으로 본다.
- 근거가 부족한 통화 초반에는 섣불리 높이지 말고, 정황이 쌓이는 만큼 올린다.

[predicted_next] 사기 정황이 있을 때, 이 흐름이면 다음에 올 가능성이 가장 높은 단계. 없으면 none.

[summary] 지금 통화에서 무슨 일이 벌어지고 있는지 사용자에게 보여줄 한두 문장(한국어, 평문, 120자 이내).
단정하지 말고 "~하고 있습니다", "~로 보입니다"처럼 쓴다. 정상 통화면 그렇게 적는다.
'사칭'이라는 말은 사칭 정황이 드러났을 때만 쓴다. 정황이 없으면 "은행 직원이라고 밝힌 상대방이"처럼
중립적으로 쓰고, 정상으로 보이는 통화에 확인을 권하는 경고 문구를 넣지 않는다.

[utterances] [신규] 표시가 붙은 발화는 모두 포함한다. 이전 발화는 맥락이 드러나 역할이나 태그를
고쳐야 할 때만 포함한다.

[entities] 수사 단서가 될 구체적인 값만 뽑는다. 발화에 실제로 나온 값을 그대로 적고, 없는 값은 만들지 않는다.
account 계좌번호 / phone 전화번호 / url 웹 주소 / org 사칭한 기관·회사명 / person 밝힌 이름·직함 /
amount 요구한 금액 / app 설치를 요구한 앱 이름 / case_number 사건번호.
org에는 기관·회사의 이름만 적는다("서울중앙지검", "금융감독원"). 사람 무리나 설명 문구("공범 8명",
"안전계좌")는 단서가 아니다. 같은 대상을 person과 org에 겹쳐 적지 않는다.
"XXX"처럼 가려진 값, 음성 인식이 깨져 뜻을 알 수 없는 값은 뽑지 않는다.
이미 추출된 단서는 다시 적지 않는다. 사기 정황이 없는 정상 통화에서는 단서를 뽑지 않는다(빈 배열)."""

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "stage": {"type": "string", "enum": STAGES},
        "scam_type": {"type": "string", "enum": list(SCAM_TYPES)},
        "compliance": {"type": "string", "enum": COMPLIANCE},
        "risk": {"type": "number"},
        "predicted_next": {"type": "string", "enum": STAGES},
        "summary": {"type": "string"},
        "utterances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn_index": {"type": "integer"},
                    "role": {"type": "string", "enum": ROLES},
                    "tactics": {"type": "array", "items": {"type": "string", "enum": list(TACTICS)}},
                },
                "required": ["turn_index", "role", "tactics"],
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ENTITY_TYPES},
                    "value": {"type": "string"},
                    "turn_index": {"type": "integer"},
                },
                "required": ["entity_type", "value", "turn_index"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "stage",
        "scam_type",
        "compliance",
        "risk",
        "predicted_next",
        "summary",
        "utterances",
        "entities",
    ],
    "additionalProperties": False,
}


def has_pending_messages(log_id: int) -> bool:
    """마지막 상태 이후에 분석할 만한 새 발화가 있는지 확인한다."""
    previous = state_repository.get_latest_state(log_id)
    last_turn = previous["last_turn_index"] if previous else 0
    new_chars = sum(
        len(message["content"].strip())
        for message in state_repository.list_tagged_messages(log_id)
        if message["turn_index"] > last_turn
    )
    return new_chars >= MIN_NEW_CHARS


def update_state(log_id: int, force: bool = False, retag_all: bool = False) -> Optional[dict]:
    """새 발화를 반영해 대화 상태를 한 버전 올린다.

    반환: {"state": 상태 dict, "utterance_tags": 바뀐 발화 태그, "entities": 새 단서}
    새 발화가 없거나 LLM 호출에 실패하면 None을 반환한다(통화 분석은 빠른 경로만으로 계속된다).
    force=True면 새 발화가 적어도 실행한다(통화 종료 시 마지막 확정용).
    retag_all=True면 모든 발화를 새 발화로 보고 다시 태깅한다(전체 재전사로 발화가 교체된 뒤).
    """
    if not claude_client.is_configured():
        return None

    messages = state_repository.list_tagged_messages(log_id)
    if not messages:
        return None

    previous = state_repository.get_latest_state(log_id)
    last_turn = previous["last_turn_index"] if previous and not retag_all else 0
    new_messages = [message for message in messages if message["turn_index"] > last_turn]
    new_chars = sum(len(message["content"].strip()) for message in new_messages)
    if not new_messages or (new_chars < MIN_NEW_CHARS and not force):
        return None

    prompt = _build_prompt(
        messages=messages,
        last_turn=last_turn,
        previous=previous,
        # 전체 재전사 뒤에는 발화 번호가 바뀌므로 단서를 처음부터 다시 뽑게 한다.
        known_entities=[] if retag_all else state_repository.list_entities(log_id),
    )
    model = claude_client.tracker_model()
    try:
        output = claude_client.complete_json(
            prompt,
            schema=OUTPUT_SCHEMA,
            system=SYSTEM_PROMPT,
            model=model,
            effort="low",
        )
    except Exception as exc:
        logger.warning("상태 추적기 호출 실패. 이번 발화는 빠른 경로 결과만 사용합니다: %s", exc)
        return None

    valid_turns = {message["turn_index"] for message in messages}
    stage, scam_type = _reconcile_stage(output, previous)
    predicted_next = output.get("predicted_next")
    if predicted_next in (None, "none") or stage_index(predicted_next) <= stage_index(stage):
        predicted_next = None

    changed_tags = state_repository.update_message_tags(
        log_id,
        [
            {
                "turn_index": int(item["turn_index"]),
                "role": item["role"] if item["role"] in ROLES else "unknown",
                # 수법·정상 신호 태그는 상대방의 행동을 가리킨다. 사용자 발화에는 붙이지 않는다.
                "tactics": [] if item["role"] == "user" else [tactic for tactic in item["tactics"] if tactic in TACTICS],
            }
            for item in output.get("utterances", [])
            if int(item["turn_index"]) in valid_turns
        ],
    )
    if retag_all:
        state_repository.delete_entities(log_id)
    new_entities = state_repository.insert_entities(
        log_id,
        [
            entity
            for entity in output.get("entities", [])
            if entity.get("entity_type") in ENTITY_TYPES and int(entity.get("turn_index", 0)) in valid_turns
        ],
    )
    state = state_repository.insert_state(
        log_id=log_id,
        stage=stage,
        scam_type=scam_type,
        compliance=output["compliance"] if output.get("compliance") in COMPLIANCE else "none",
        tracker_risk=max(0.0, min(1.0, float(output.get("risk", 0.0)))),
        predicted_next=predicted_next,
        summary=str(output.get("summary", "")).strip()[:300],
        last_turn_index=max(valid_turns),
        model_version=f"tracker-v1:{model}",
    )
    return {"state": state, "utterance_tags": changed_tags, "entities": new_entities}


def _reconcile_stage(output: dict, previous: Optional[dict]) -> tuple[str, str]:
    """모델 출력의 단계·유형을 검증하고 이전 상태와 모순되지 않게 맞춘다.

    단계는 "지금까지 도달한 가장 높은 단계"라서 뒤로 가지 않는다. 다만 모델이 통화를
    정상으로 다시 판단한 경우(none)는 받아들인다.
    """
    stage = output.get("stage") if output.get("stage") in STAGES else "none"
    scam_type = output.get("scam_type") if output.get("scam_type") in SCAM_TYPES else "none"

    if scam_type == "none" or stage == "none":
        return "none", "none"

    if previous and stage_index(stage) < stage_index(previous["stage"]):
        stage = previous["stage"]
    return stage, scam_type


def _build_prompt(
    messages: list[dict],
    last_turn: int,
    previous: Optional[dict],
    known_entities: list[dict],
) -> str:
    """이전 상태와 전사문을 사용자 프롬프트로 조립한다."""
    lines: list[str] = []
    if previous:
        lines.append("[이전 상태]")
        lines.append(
            f"stage={previous['stage']}, scam_type={previous['scam_type']}, "
            f"compliance={previous['compliance']}, risk={previous['tracker_risk']}"
        )
        lines.append(f"요약: {previous['summary']}")
    else:
        lines.append("[이전 상태] 없음 (통화 시작)")

    if known_entities:
        lines.append("")
        lines.append("[이미 추출된 단서]")
        lines.extend(f"- {entity['entity_type']}: {entity['value']}" for entity in known_entities)

    shown = messages[-MAX_TRANSCRIPT_TURNS:]
    lines.append("")
    if len(shown) < len(messages):
        lines.append(f"[전사문] (앞선 {len(messages) - len(shown)}개 발화는 생략, 위 요약 참고)")
    else:
        lines.append("[전사문]")
    for message in shown:
        is_new = message["turn_index"] > last_turn
        if is_new:
            tag = "[신규]"
        else:
            tactics = ",".join(message["tactics"]) or "-"
            tag = f"[{message['role']}|{tactics}]"
        lines.append(f"{message['turn_index']}. {tag} {message['content']}")

    lines.append("")
    lines.append("위 통화의 현재 상태를 갱신하라.")
    return "\n".join(lines)
