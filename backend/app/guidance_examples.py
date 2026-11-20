"""절차 안내를 따라 해 볼 수 있게 지어낸 통화 두 건을 DB에 넣는다. docs/guidance-examples.md 참고.

- 예시 A: 물품 거래를 가장한 사기(형법상 사기만 해당). 이득액 5억 원을 기준으로 통보 여부가 갈리는 것을 본다.
- 예시 B: 기관 사칭 통화에 캄보디아 단서가 나온다. 해외 연계 절차를 본다.

이름, 번호, 금액은 모두 지어낸 것이다. 같은 이름의 통화가 이미 있으면 다시 넣지 않는다.

사용(저장소 루트에서):
    python -m backend.app.guidance_examples
"""

from __future__ import annotations

import sys

from backend.app import database, repository, state_repository
from backend.app.database import get_connection
from backend.app.schemas import CallLogCreate, CallMessageCreate

EXAMPLES = [
    {
        "name": "예시 A · 중고거래 사기(지어낸 통화)",
        "stage": "payment",
        "scam_type": "other",
        "risk": 0.82,
        "summary": "중고 물품 판매를 가장해 선입금을 요구(지어낸 통화)",
        "lines": [
            "중고 장터에서 노트북 문의 주신 분이죠? 판매자 박서준입니다.",
            "네, 아직 판매 중인가요?",
            "문의가 많아서요. 먼저 85만 원을 입금해 주시면 오늘 바로 택배로 보내 드리겠습니다.",
            "안전결제는 수수료가 붙으니 제 계좌로 직접 보내 주세요.",
        ],
        "entities": [{"entity_type": "amount", "value": "85만 원", "turn_index": 3}],
    },
    {
        "name": "예시 B · 캄보디아 연계(지어낸 통화)",
        "stage": "payment",
        "scam_type": "agency",
        "risk": 0.97,
        "summary": "검찰 사칭, 안전계좌 이체 요구(지어낸 통화)",
        "lines": [
            "서울중앙지검 김민수 수사관입니다. 본인 명의 계좌가 범죄에 쓰였습니다.",
            "네? 제 계좌가요?",
            "공범 일당이 캄보디아 프놈펜에서 검거됐습니다. 안전계좌로 8천 6백만 원을 이체하셔야 합니다.",
            "연락은 텔레그램으로 드리겠습니다. +855 12 345 678 번호로 전화가 갈 겁니다.",
        ],
        "entities": [
            {"entity_type": "amount", "value": "8천 6백만 원", "turn_index": 3},
            {"entity_type": "phone", "value": "+855 12 345 678", "turn_index": 4},
        ],
    },
]


def add_examples() -> list[tuple[int, str, bool]]:
    """예시 통화를 넣고 (통화 번호, 이름, 새로 넣었는지)를 돌려준다."""
    database.init_db()
    with get_connection() as connection:
        existing = {row["name"]: row["id"] for row in connection.execute("SELECT id, name FROM call_logs")}
    result = []
    for example in EXAMPLES:
        if example["name"] in existing:
            result.append((existing[example["name"]], example["name"], False))
            continue
        log_id = repository.create_call_log(CallLogCreate(name=example["name"])).id
        for index, content in enumerate(example["lines"], start=1):
            repository.insert_call_message(log_id, CallMessageCreate(content=content, turn_index=index))
        state_repository.insert_state(log_id, example["stage"], example["scam_type"], "none", example["risk"], None,
                                      example["summary"], len(example["lines"]), "example")
        state_repository.insert_entities(log_id, example["entities"])
        result.append((log_id, example["name"], True))
    return result


def main() -> int:
    for log_id, name, created in add_examples():
        print(f"{'넣음' if created else '이미 있음'}  사건 #{log_id}  {name}")
    print("대시보드(/dashboard)의 통화 목록에서 이름으로 찾을 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
