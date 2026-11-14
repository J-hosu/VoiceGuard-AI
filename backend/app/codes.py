"""v2 공통 코드값 (docs/API_SPEC_V2.md 2절과 1:1로 맞춘다)."""

from __future__ import annotations

# 2.1 사기 단계. 리스트 순서가 곧 stage_index다.
STAGES: list[str] = [
    "none",
    "approach",
    "impersonation",
    "pressure",
    "isolation",
    "extraction",
    "payment",
]
STAGE_INDEX: dict[str, int] = {stage: index for index, stage in enumerate(STAGES)}
STAGE_LABELS: dict[str, str] = {
    "none": "정상/미확정",
    "approach": "접근",
    "impersonation": "사칭·신뢰 형성",
    "pressure": "위기 조성",
    "isolation": "고립",
    "extraction": "정보·앱 요구",
    "payment": "금전 요구",
}

# 2.2 사기 유형 코드 → 화면 표시 문자열(phishing_type)
SCAM_TYPES: dict[str, str] = {
    "none": "정상",
    "agency": "수사기관 사칭형",
    "loan": "대출 사기형",
    "kidnap": "납치 협박형",
    "acquaintance": "지인 사칭형",
    "smishing": "스미싱·악성앱 유도형",
    "other": "기타",
}

# 2.3 수법 태그 → v1 matched_patterns 라벨 (정상 신호는 None)
TACTICS: dict[str, str | None] = {
    "authority_claim": "수사기관/공공기관 사칭",
    "crime_involvement": "범죄 연루 압박",
    "legal_threat": "범죄 연루 압박",
    "urgency": "긴급성/비밀 유지 압박",
    "secrecy": "긴급성/비밀 유지 압박",
    "isolation": "긴급성/비밀 유지 압박",
    "info_request": "개인정보/인증 요구",
    "app_install": "앱 설치/원격제어 유도",
    "link_click": "앱 설치/원격제어 유도",
    "money_transfer": "금전 이체 유도",
    "cash_withdrawal": "금전 이체 유도",
    "loan_bait": "대출 사칭/상환금 요구",
    "official_channel": None,
    "no_pressure": None,
}

# 단계 → 그 단계를 드러내는 수법 태그. 타임라인에서 단계가 시작된 발화를 찾을 때 쓴다.
STAGE_TACTICS: dict[str, list[str]] = {
    "approach": ["loan_bait"],
    "impersonation": ["authority_claim"],
    "pressure": ["crime_involvement", "legal_threat", "urgency"],
    "isolation": ["secrecy", "isolation"],
    "extraction": ["info_request", "app_install", "link_click"],
    "payment": ["money_transfer", "cash_withdrawal"],
}

ROLES: list[str] = ["suspect", "user", "unknown"]
COMPLIANCE: list[str] = ["none", "hesitant", "complying", "completed"]

ACTION_TYPES: list[str] = [
    "warn",
    "verify_guide",
    "official_number",
    "guardian_alert",
    "block_guide",
    "evidence_preserve",
    "report_guide",
    "followup_check",
]
ACTION_STATUSES: list[str] = ["delivered", "acknowledged", "dismissed", "answered", "expired", "failed"]

FOLLOWUP_ANSWERS: dict[str, str] = {
    "hung_up": "끊었어요",
    "still_talking": "아직 통화 중이에요",
    "verified_safe": "직접 확인해 보니 정상이었어요",
    "shared_info": "개인정보를 알려줬어요",
    "installed_app": "앱을 설치했어요",
    "transferred_money": "이미 송금했어요",
    "false_alarm": "잘못된 경고였어요",
}
# 피해가 이미 발생했음을 뜻하는 답
DAMAGE_ANSWERS: set[str] = {"shared_info", "installed_app", "transferred_money"}

FEEDBACK_VERDICTS: list[str] = ["false_positive", "confirmed_phishing", "missed_phishing", "unsure"]

ENTITY_TYPES: list[str] = ["account", "phone", "url", "org", "person", "amount", "app", "case_number"]

# 대응 에이전트가 안내할 수 있는 공식 대표번호. LLM이 번호를 지어내지 못하도록
# 기관 키만 고르게 하고 번호는 항상 이 표에서 가져온다.
OFFICIAL_NUMBERS: dict[str, dict[str, str]] = {
    "prosecution": {"org": "검찰청", "phone": "1301"},
    "police": {"org": "경찰청", "phone": "112"},
    "fss": {"org": "금융감독원", "phone": "1332"},
    "kisa": {"org": "한국인터넷진흥원(KISA)", "phone": "118"},
    "bank": {"org": "거래 은행 고객센터", "phone": "카드·통장 뒷면의 대표번호"},
    "card": {"org": "카드사 고객센터", "phone": "카드 뒷면의 대표번호"},
    "family": {"org": "가족·지인 본인", "phone": "평소 저장해 둔 번호"},
}


# 수법 태그 → 화면·문서용 라벨. risk=False는 정상 신호다.
TACTIC_LABELS: dict[str, dict] = {
    "authority_claim": {"label": "기관·직함 사칭", "risk": True},
    "crime_involvement": {"label": "범죄 연루 통보", "risk": True},
    "legal_threat": {"label": "구속·처벌 위협", "risk": True},
    "urgency": {"label": "시간 압박", "risk": True},
    "secrecy": {"label": "비밀 유지 강요", "risk": True},
    "isolation": {"label": "통화 유지·이동 지시", "risk": True},
    "info_request": {"label": "개인정보 요구", "risk": True},
    "app_install": {"label": "앱 설치 유도", "risk": True},
    "link_click": {"label": "링크 접속 유도", "risk": True},
    "money_transfer": {"label": "이체·송금 요구", "risk": True},
    "cash_withdrawal": {"label": "현금 인출 요구", "risk": True},
    "loan_bait": {"label": "저금리·선상환 미끼", "risk": True},
    "official_channel": {"label": "공식 창구 안내", "risk": False},
    "no_pressure": {"label": "강요 없음", "risk": False},
}


def stage_index(stage: str) -> int:
    """단계 코드의 순번을 반환한다. 모르는 값은 0(none)으로 본다."""
    return STAGE_INDEX.get(stage, 0)


def phishing_type_label(scam_type: str) -> str:
    """사기 유형 코드의 표시 문자열을 반환한다."""
    return SCAM_TYPES.get(scam_type, SCAM_TYPES["other"])


def tactics_to_patterns(tactics: list[str]) -> list[str]:
    """수법 태그 목록을 v1 matched_patterns 라벨 목록(중복 제거, 순서 유지)으로 바꾼다."""
    patterns: list[str] = []
    for tactic in tactics:
        label = TACTICS.get(tactic)
        if label and label not in patterns:
            patterns.append(label)
    return patterns
