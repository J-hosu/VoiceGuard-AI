"""신고용 요약(신고 패키지) 생성.

사용자가 경찰·금융회사에 낼 수 있도록 통화의 판정, 단계별 경과, 위험 발화, 추출 단서,
AI 대응 기록을 한 문서로 묶는다. 저장된 기록만으로 조립하며 LLM을 호출하지 않는다
(제출 문서에 없는 사실이 섞이지 않게 하기 위함). docs/API_SPEC_V2.md 5.7절.
"""

from __future__ import annotations

from backend.app import state_repository
from backend.app.codes import FOLLOWUP_ANSWERS, STAGE_LABELS, TACTIC_LABELS, phishing_type_label
from backend.app.repository import get_call_log
from backend.app.services import agent_loop


ENTITY_LABELS = {
    "account": "계좌번호",
    "phone": "전화번호",
    "url": "웹 주소",
    "org": "언급된 기관·회사",
    "person": "이름·직함",
    "amount": "요구 금액",
    "app": "설치 요구 앱",
    "case_number": "사건번호",
}
ROLE_LABELS = {"suspect": "상대방", "user": "사용자", "unknown": "미상"}
RISK_LABELS = {"low": "낮음", "medium": "주의", "high": "높음"}


def build_report(log_id: int) -> dict:
    """신고용 요약을 dict로 만든다."""
    call = get_call_log(log_id)
    state = state_repository.get_latest_state(log_id)
    timeline = agent_loop.get_timeline_response(log_id)

    key_utterances = [
        {
            "turn_index": message["turn_index"],
            "role": message["role"],
            "content": message["content"],
            "tactics": [t for t in message["tactics"] if TACTIC_LABELS.get(t, {}).get("risk")],
        }
        for message in timeline["messages"]
        if any(TACTIC_LABELS.get(t, {}).get("risk") for t in message["tactics"])
    ]
    followups = [
        {"question": a["payload"].get("question", a["title"]), "answer": a["response"], "answered_at": a["responded_at"]}
        for a in timeline["actions"]
        if a["action_type"] == "followup_check" and a["status"] == "answered"
    ]
    actions = [
        {"at": a["created_at"], "action_type": a["action_type"], "title": a["title"], "reason": a["reason"]}
        for a in timeline["actions"]
        if a["action_type"] != "followup_check"
    ]

    return {
        "log_id": log_id,
        "called_at": call.created_at,
        "risk_score": round(call.risk_score, 4),
        "risk_level": call.risk_level,
        "scam_type": state["scam_type"] if state else "none",
        "phishing_type": state["phishing_type"] if state else phishing_type_label("none"),
        "stage": state["stage"] if state else "none",
        "summary": (state["summary"] if state else "") or call.core_evidence,
        "stages": timeline["stages"],
        "key_utterances": key_utterances,
        "entities": state_repository.list_entities(log_id),
        "actions": actions,
        "followups": followups,
        "feedback": [{"verdict": f["verdict"], "comment": f["comment"]} for f in state_repository.list_feedback(log_id)],
        "transcript": [
            {"turn_index": m["turn_index"], "role": m["role"], "content": m["content"]} for m in timeline["messages"]
        ],
    }


def render_markdown(report: dict) -> str:
    """신고용 요약을 사람이 읽는 마크다운으로 바꾼다."""
    lines = [
        "# 보이스피싱 의심 통화 신고용 요약",
        "",
        f"- 통화 기록 번호: {report['log_id']}",
        f"- 통화 일시: {report['called_at']}",
        f"- 판정 유형: {report['phishing_type']}",
        f"- 위험도: {round(report['risk_score'] * 100)}% ({RISK_LABELS.get(report['risk_level'], report['risk_level'])})",
        f"- 도달 단계: {STAGE_LABELS.get(report['stage'], report['stage'])}",
        "",
        "이 문서는 VoiceGuard AI가 통화 전사 내용을 자동 분석해 정리한 것입니다. 전사에는 음성 인식 오류가 있을 수 있습니다.",
        "",
        "## 사건 개요" if report.get("overview") else "## 요약",
        "",
        report.get("overview") or report["summary"] or "요약이 없습니다.",
    ]
    if report.get("damage_reported"):
        lines += ["", "**사용자가 통화 후 피해가 있었다고 답했습니다. 아래 '사용자 확인 내용'을 확인하세요.**"]

    if report["stages"]:
        lines += ["", "## 단계별 경과", ""]
        for stage in report["stages"]:
            lines.append(f"- {stage['at']} · {STAGE_LABELS.get(stage['stage'], stage['stage'])} (발화 {stage['from_turn']}번부터)")

    if report["entities"]:
        lines += ["", "## 통화에서 언급된 단서", ""]
        for entity in report["entities"]:
            label = ENTITY_LABELS.get(entity["entity_type"], entity["entity_type"])
            turn = f" (발화 {entity['turn_index']}번)" if entity.get("turn_index") else ""
            lines.append(f"- {label}: {entity['value']}{turn}")

    if report.get("links"):
        lines += ["", "## 다른 통화와 겹치는 단서", ""]
        for link in report["links"]:
            shared = ", ".join(
                f"{ENTITY_LABELS.get(item['entity_type'], item['entity_type'])} {item['value']}" for item in link["shared"]
            )
            lines.append(f"- 통화 기록 {link['log_id']}번 ({link['called_at']}, 위험도 {round(link['risk_score'] * 100)}%): {shared}")

    if report.get("similar_official_cases"):
        lines += ["", "## 유사 공식 사례 (참고)", ""]
        for case in report["similar_official_cases"]:
            lines.append(f"- {case['source']} ({case['date']}) · {case['title']}")
            if case.get("url"):
                lines.append(f"  - {case['url']}")
        lines.append("")
        lines.append("통화 내용과 표현이 비슷한 공개 사례를 자동으로 찾은 것으로, 같은 사건이라는 뜻은 아닙니다.")

    if report["key_utterances"]:
        lines += ["", "## 위험 발화", ""]
        for utterance in report["key_utterances"]:
            tactics = ", ".join(TACTIC_LABELS[t]["label"] for t in utterance["tactics"])
            lines.append(f"- {utterance['turn_index']}번 [{ROLE_LABELS.get(utterance['role'], '미상')}] {utterance['content']}")
            lines.append(f"  - 수법: {tactics}")

    if report["followups"]:
        lines += ["", "## 사용자 확인 내용", ""]
        for followup in report["followups"]:
            answer = FOLLOWUP_ANSWERS.get(followup["answer"], followup["answer"])
            lines.append(f"- {followup['answered_at']} · {followup['question']} → {answer}")

    if report["actions"]:
        lines += ["", "## AI 대응 기록", ""]
        for action in report["actions"]:
            lines.append(f"- {action['at']} · {action['title']}")

    proof = report.get("evidence") or {}
    if proof.get("audio_chunks") or proof.get("transcript_sealed"):
        lines += ["", "## 증거 무결성 (SHA-256)", ""]
        if proof.get("audio_chunks"):
            lines.append(
                f"- 원본 음성 {proof['audio_chunks']}개 조각 ({proof['audio_bytes']:,} bytes, {proof['audio_first_at']} ~ {proof['audio_last_at']})"
                f" · 체인 검증 {'정상' if proof['audio_chain_ok'] else '불일치'}"
            )
            lines.append(f"- 음성 해시 체인 마지막 값: {proof['audio_last_chain_hash']}")
        if proof.get("transcript_sealed"):
            lines.append(
                f"- 전사문 봉인 {proof['transcript_sealed_at']} (발화 {proof['utterances']}개): {proof['transcript_hash']}"
                f" · 현재 전사문과 {'일치' if proof['transcript_ok'] else '불일치'}"
            )
        lines.append("- 음성 원본은 개인정보 보호를 위해 전사 후 지우고 해시만 남긴다. 원본을 따로 보관했다면 위 해시와 맞춰 볼 수 있다.")

    lines += ["", "## 통화 전문", ""]
    for message in report["transcript"]:
        lines.append(f"{message['turn_index']}. [{ROLE_LABELS.get(message['role'], '미상')}] {message['content']}")

    lines += [
        "",
        "## 신고·상담 창구",
        "",
        "- 경찰청 112 (지급정지 요청, 사건 접수)",
        "- 금융감독원 1332 (피해 상담, 피해구제 절차 안내)",
        "- 한국인터넷진흥원 118 (악성 앱, 스미싱 상담)",
    ]
    return "\n".join(lines) + "\n"
