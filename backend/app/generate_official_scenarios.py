"""공식 기관이 공개한 피해 사례를 근거로 학습용 통화 전사본을 만든다 (Claude Batch API).

기존 학습 데이터의 사기 통화는 금감원 공개 녹취(수사기관 사칭, 대출 빙자) 두 유형뿐이라
납치 협박, 가족·지인 사칭, 카드배송·택배 사칭, 악성앱 설치 유도 같은 수법을 분류기가
전혀 보지 못했다. 금융감독원 피해사례·소비자경보와 경찰청 예·경보에 실린 사례 설명을
그대로 근거로 주고, 그 수법이 실제 통화에서 어떻게 들릴지를 전사본으로 생성한다.

사례마다 두 종류를 만든다.
  - scam:  그 사례의 수법으로 진행되는 사기 통화 (피해자 반응과 통화가 끊기는 시점을 달리함)
  - twin:  같은 상황이 정상적으로 일어나는 통화 (진짜 카드 배송, 진짜 은행 대출 안내,
           진짜 가족의 부탁 등). 주제는 같고 의도만 다른 쌍둥이를 함께 넣어야
           "주제어 = 사기"라는 지름길을 학습하지 않는다.

입력:  data/official/cases.json
        [{"source", "url", "date", "title", "scam_type", "has_text", "text"}]
출력:  data/generated/official_scam.json, data/generated/official_twin.json
        [{"case_index", "scam_type", "source_url", "turns": [{"speaker", "text"}]}]

실행:
    .venv/bin/python -m backend.app.generate_official_scenarios            # 전체
    LIMIT=3 .venv/bin/python -m backend.app.generate_official_scenarios    # 앞 3개 사례만(시험)
"""

from __future__ import annotations

import json
import os
import time

import anthropic
from dotenv import load_dotenv

from backend.app.paths import DATA_DIR, ENV_PATH
from backend.app.services.claude_client import DEFAULT_MODEL


CASES_PATH = DATA_DIR / "official" / "cases.json"
OUT_DIR = DATA_DIR / "generated"
MIN_CASE_CHARS = 300
MAX_CASE_CHARS = 3500
SCAM_CALLS = 4
TWIN_CALLS = 3
LIMIT = int(os.environ.get("LIMIT", "0"))
# 공식 사례가 적은 유형은 사례 하나로 사기 통화 묶음을 여러 번 만든다(묶음마다 인물 설정을 달리함).
SCAM_REPEATS = {"kidnap": 4, "acquaintance": 3, "smishing": 2}
VARIATIONS = [
    "",
    "이번 묶음의 설정: 전화를 받는 사람은 60~70대이고, 건 사람은 차분하고 사무적으로 말한다.",
    "이번 묶음의 설정: 전화를 받는 사람은 20~30대 직장인이고, 건 사람은 다급하게 몰아친다.",
    "이번 묶음의 설정: 전화를 받는 사람은 40~50대 자영업자이고, 통화 중 되묻기와 말 끊김이 잦다.",
]

SYSTEM = """너는 보이스피싱 탐지 모델의 학습 데이터를 만드는 한국어 통화 전사본 작성자다.
만든 전사본은 통화 중인 사용자에게 사기를 경고하는 탐지 모델의 학습에만 쓰인다.
금융감독원·경찰청이 예방을 위해 공개한 사례 설명이 주어지며, 그 내용을 벗어난 새 수법을 지어내지 않는다.

[전사본 형식]
- 실제 전화 녹음을 음성인식(STT)한 결과처럼 쓴다: 구어체, 추임새(네, 아, 예예, 어), 말 끊김, 되묻기, 맞장구.
  문장부호는 최소한으로 쓰고, 가끔 한 사람의 말이 두 턴으로 쪼개진다.
- 통화는 "여보세요"나 인사로 시작하는 것이 보통이다. 한 턴은 한 사람의 발화다.
- caller는 전화를 건 쪽, user는 전화를 받은 일반인이다.
- 사람 이름, 기관명, 은행명, 금액은 한국에서 흔한 것으로 다양하게 쓴다.
- 전화번호와 계좌번호는 실제로 쓰일 수 없는 형식의 예시 숫자로만 쓴다. 실제 웹 주소나 실제 앱의 설치 방법은 쓰지 않는다
  ("문자로 보내드린 링크", "원격 지원 앱"처럼만 표현한다).
- 통화 하나는 10~28턴이다."""

SCAM_PROMPT = """[공식 사례] ({source_label}, {date})
제목: {title}

{text}

위 사례에 나온 수법으로 진행되는 사기 통화 전사본 {n}개를 만들어라.
- 사례에 나온 접근 방식, 내세운 명분, 요구 사항의 흐름을 따른다. 사례가 문자·메신저로 시작했다면
  그 뒤에 이어지는 전화 통화를 쓴다(통화에서 그 문자를 언급한다).
- {n}개는 서로 달라야 한다:
  1) 피해자가 믿고 끝까지 따르는 통화
  2) 피해자가 의심하며 되묻지만 사기범이 설득을 이어가는 통화
  3) 요구가 본격적으로 나오기 전, 초중반에서 끊기는 통화 (10~14턴)
  4) 피해자가 가족이나 공식 번호로 확인하겠다고 하자 사기범이 막으려 드는 통화
- 사기범의 말투는 실제처럼 차분하고 사무적이거나 다급하다. 과장된 악당 말투는 쓰지 않는다.
{variation}"""

TWIN_PROMPT = """[공식 사례] ({source_label}, {date})
제목: {title}

{text}

위 사례에서 사기범이 흉내 낸 상황이 '정상적으로' 일어나는 통화 전사본 {n}개를 만들어라.
탐지 모델이 주제어만 보고 사기로 오판하지 않도록 넣는 정상 통화다.
- 사례와 같은 주제·기관·용건(예: 카드 배송, 대출 안내, 수사기관의 연락, 가족의 돈 부탁, 택배, 부고)이
  등장하지만 실제 직원·가족·지인이 건 정상 통화다.
- 정상 통화의 특징이 자연스럽게 드러난다: 돈을 다른 계좌로 보내라고 하지 않는다, 앱 설치나 링크 접속을
  요구하지 않는다, 비밀로 하라거나 전화를 끊지 말라고 하지 않는다, 공식 대표번호·지점·앱으로 직접
  확인하라고 안내한다, 거절하거나 나중에 하겠다는 말을 받아들인다.
- 그래도 사례와 겹치는 단어(계좌, 명의, 대출, 사건, 송금, 본인 확인 등)가 통화에 자연스럽게 나온다.
- {n}개는 기관·상황·받는 사람의 반응(의심함, 바쁨, 질문이 많음, 흔쾌함)이 서로 달라야 한다."""

SOURCE_LABELS = {
    "fss_case": "금융감독원 보이스피싱 피해사례",
    "fss_alert": "금융감독원 소비자경보",
    "police_alert": "경찰청 통합대응단 피싱 예·경보",
    "police_scenario": "경찰청 통합대응단 피싱 시나리오",
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "speaker": {"type": "string", "enum": ["caller", "user"]},
                                "text": {"type": "string"},
                            },
                            "required": ["speaker", "text"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["turns"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["calls"],
    "additionalProperties": False,
}


def load_cases() -> list[dict]:
    """근거로 쓸 만큼 본문이 있는 사례만 원래 순번(case_index)과 함께 반환한다."""
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    usable = []
    for index, case in enumerate(cases):
        text = str(case.get("text", "")).strip()
        if case.get("has_text") and len(text) >= MIN_CASE_CHARS:
            usable.append({**case, "case_index": index, "text": text[:MAX_CASE_CHARS]})
    return usable[:LIMIT] if LIMIT else usable


def build_requests(cases: list[dict]) -> list[dict]:
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    requests = []
    for case in cases:
        fields = {
            "source_label": SOURCE_LABELS.get(case["source"], case["source"]),
            "date": case.get("date", ""),
            "title": case.get("title", ""),
            "text": case["text"],
        }
        repeats = SCAM_REPEATS.get(case.get("scam_type", "other"), 1)
        jobs = [("scam", SCAM_PROMPT, SCAM_CALLS, repeat) for repeat in range(repeats)]
        jobs.append(("twin", TWIN_PROMPT, TWIN_CALLS, 0))
        for kind, template, count, repeat in jobs:
            fields["variation"] = VARIATIONS[repeat % len(VARIATIONS)] if repeats > 1 else ""
            requests.append(
                {
                    "custom_id": f"{kind}-{case['case_index']:04d}-{repeat}",
                    "params": {
                        "model": model,
                        "max_tokens": 16000,
                        "system": SYSTEM,
                        "output_config": {
                            "effort": "low",
                            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                        },
                        "messages": [{"role": "user", "content": template.format(n=count, **fields)}],
                    },
                }
            )
    return requests


def main() -> None:
    load_dotenv(ENV_PATH)
    client = anthropic.Anthropic()
    cases = load_cases()
    by_index = {case["case_index"]: case for case in cases}
    requests = build_requests(cases)
    batch = client.messages.batches.create(requests=requests)
    print(f"배치 생성: {batch.id} (사례 {len(cases)}건, 요청 {len(requests)}건)", flush=True)

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        print(f"  진행 중... {batch.request_counts}", flush=True)
        time.sleep(30)

    results: dict[str, list[dict]] = {"scam": [], "twin": []}
    failed = []
    usage_in = usage_out = 0
    for item in client.messages.batches.results(batch.id):
        if item.result.type != "succeeded":
            failed.append((item.custom_id, item.result.type))
            continue
        message = item.result.message
        usage_in += message.usage.input_tokens
        usage_out += message.usage.output_tokens
        if message.stop_reason != "end_turn":
            failed.append((item.custom_id, message.stop_reason))
            continue
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            calls = json.loads(text)["calls"]
        except (ValueError, KeyError):
            failed.append((item.custom_id, "parse_error"))
            continue
        kind, index, _repeat = item.custom_id.split("-")
        case = by_index[int(index)]
        for call in calls:
            turns = [turn for turn in call.get("turns", []) if str(turn.get("text", "")).strip()]
            if len(turns) >= 4:
                results[kind].append(
                    {
                        "case_index": case["case_index"],
                        "scam_type": case.get("scam_type", "other"),
                        "source_url": case.get("url", ""),
                        "turns": turns,
                    }
                )

    os.makedirs(OUT_DIR, exist_ok=True)
    for kind, fname in (("scam", "official_scam.json"), ("twin", "official_twin.json")):
        rows = sorted(results[kind], key=lambda row: row["case_index"])
        with open(OUT_DIR / fname, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print(f"{fname}: {len(rows)}건")
    print(f"토큰 사용량: 입력 {usage_in:,} / 출력 {usage_out:,}")
    if failed:
        print(f"실패 {len(failed)}건: {failed[:20]}")


if __name__ == "__main__":
    main()
