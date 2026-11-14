"""정상 금융 통화(하드 네거티브) 전사본을 Claude Batch API로 생성한다.

원본 학습셋의 '직접 작성한 아웃바운드 권유 시나리오'와 '금융상담 데이터셋'을 대체한다.
피싱과 어휘가 겹치는 정상 통화(대출·계좌·인증 등)가 없으면 모델이
"금융 용어 = 피싱"이라는 지름길을 학습하기 때문에 정상 클래스에 넣는다.

출력:
  data/normal/outbound_generated.json  (금융사가 먼저 거는 권유·안내 전화)
  data/normal/finance_generated.json   (고객이 먼저 거는 콜센터 상담)
  [{"category", "turns": [{"speaker", "text"}]}]

실행:
    .venv/bin/python -m backend.app.generate_hard_negatives
"""
import json
import os
import time

import anthropic
from dotenv import load_dotenv

from backend.app.paths import DATA_DIR, ENV_PATH
from backend.app.services.claude_client import DEFAULT_MODEL


OUT_DIR = DATA_DIR / "normal"
CALLS_PER_REQUEST = 5
REPEAT = 2  # 카테고리당 요청 수

OUTBOUND = [
    "은행 대출 상담 신청 고객에게 콜백",
    "저금리 대환대출(갈아타기) 권유",
    "전세자금대출 만기 연장 안내",
    "신용카드 혜택 및 신규 발급 권유",
    "카드론·현금서비스 이용 안내",
    "예적금 만기 및 재예치 안내",
    "보험 갱신 및 신규 가입 권유",
    "보험금 청구 진행 상황 안내",
    "증권사 계좌 개설 이벤트 및 투자상품 안내",
    "통신사 요금제 변경 및 결합상품 권유",
    "가입 후 해피콜(불완전판매 확인)",
    "연체 발생 공식 안내 (본인 명의 계좌 상환 안내)",
    "은행의 보이스피싱 예방 안내 전화",
]

INBOUND = [
    "증권 계좌 개설 절차 문의",
    "주식 매수·매도 주문 오류 문의",
    "인터넷뱅킹 비밀번호 재설정",
    "공동인증서 재발급 문의",
    "이체 한도 변경 요청",
    "체크카드·신용카드 분실 신고",
    "대출 중도상환 수수료 문의",
    "대출 금리 인하 요구권 문의",
    "펀드 환매 및 수익률 문의",
    "연금저축 계좌 이전 문의",
    "해외 송금 방법 문의",
    "예금 잔액 증명서 발급",
    "보험 약관대출 문의",
    "자동이체 등록·해지",
    "휴면계좌 조회 및 해지",
    "신용점수 하락 사유 문의",
    "카드 결제일·결제금액 변경",
    "주택담보대출 서류 준비 문의",
    "청약통장 납입 문의",
    "금융사기 의심 문자를 받았다는 고객 문의",
    "외화 환전 예약 문의",
    "증권 계좌 배당금 입금 문의",
    "모바일 OTP 등록 문의",
]

COMMON_RULES = """요구사항:
- 실제 전화 녹음을 음성인식(STT)한 결과처럼 쓴다: 구어체, 추임새(네, 아, 음, 예예), 말 끊김, 맞장구, 되묻기 포함. 문장부호는 최소한으로.
- 각 통화는 14~30턴, 상담원과 고객이 번갈아 말한다.
- 정상 금융 업무의 특징이 자연스럽게 드러나야 한다: 공식 앱·대표번호·지점 방문 안내, 고객의 거절이나 보류를 존중, 입출금은 본인 명의 계좌로만, 본인확인은 생년월일 앞자리 등 일반적인 범위만, 시간 압박이나 비밀 유지 요구 없음.
- 대출, 금리, 한도, 상환, 계좌, 이체, 인증, 신용점수 같은 단어가 자연스럽게 등장해야 한다.
- 실제 한국 금융사·통신사 이름을 다양하게 사용한다. 상담원은 성과 직함(예: 박 주임)으로 소개한다.
- 금액, 날짜, 금리, 나이는 아라비아 숫자로 쓴다. 전화번호·계좌번호는 쓰지 않는다.
- {n}개 통화는 회사, 상품, 고객의 나이대와 반응(관심, 거절, 바쁨, 질문이 많음, 의심함)이 서로 달라야 한다.

아래 JSON 배열만 출력한다. 다른 텍스트나 코드펜스는 출력하지 마라.
[{{"turns": [{{"speaker": "상담원" 또는 "고객", "text": "..."}}]}}]"""

PROMPTS = {
    "outbound": "금융사·통신사 상담원이 고객에게 먼저 거는 정상적인 권유·안내 전화의 통화 전사본 {n}개를 만들어라.\n주제: {category}\n\n" + COMMON_RULES,
    "inbound": "고객이 금융사 콜센터에 먼저 전화해서 상담받는 정상적인 통화의 전사본 {n}개를 만들어라.\n주제: {category}\n\n" + COMMON_RULES,
}


def build_requests():
    requests = []
    for kind, categories in [("outbound", OUTBOUND), ("inbound", INBOUND)]:
        for ci, category in enumerate(categories):
            for r in range(REPEAT):
                prompt = PROMPTS[kind].format(n=CALLS_PER_REQUEST, category=category)
                requests.append({
                    "custom_id": f"{kind}-{ci:02d}-{r}",
                    "params": {
                        "model": os.getenv("LLM_MODEL", DEFAULT_MODEL),
                        "max_tokens": 16000,
                        "output_config": {"effort": "low"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                })
    return requests


def parse_calls(text):
    start, end = text.find("["), text.rfind("]")
    calls = json.loads(text[start:end + 1])
    return [c for c in calls if isinstance(c, dict) and c.get("turns")]


def main():
    load_dotenv(ENV_PATH)
    client = anthropic.Anthropic()
    requests = build_requests()
    batch = client.messages.batches.create(requests=requests)
    print(f"배치 생성: {batch.id} ({len(requests)}건 요청)")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        print(f"  진행 중... {batch.request_counts}")
        time.sleep(30)

    category_of = {}
    for kind, categories in [("outbound", OUTBOUND), ("inbound", INBOUND)]:
        for ci, category in enumerate(categories):
            for r in range(REPEAT):
                category_of[f"{kind}-{ci:02d}-{r}"] = category

    results = {"outbound": [], "inbound": []}
    failed = []
    usage_in = usage_out = 0
    for item in client.messages.batches.results(batch.id):
        if item.result.type != "succeeded":
            failed.append((item.custom_id, item.result.type))
            continue
        message = item.result.message
        usage_in += message.usage.input_tokens
        usage_out += message.usage.output_tokens
        text = "".join(b.text for b in message.content if b.type == "text")
        try:
            calls = parse_calls(text)
        except (ValueError, json.JSONDecodeError):
            failed.append((item.custom_id, "parse_error"))
            continue
        kind = item.custom_id.split("-")[0]
        for call in calls:
            results[kind].append({"category": category_of[item.custom_id], "turns": call["turns"]})

    os.makedirs(OUT_DIR, exist_ok=True)
    for kind, fname in [("outbound", "outbound_generated.json"), ("inbound", "finance_generated.json")]:
        # 결과는 순서가 보장되지 않으므로 카테고리 기준으로 정렬해 재실행 시에도 id가 안정되게 한다.
        rows = sorted(results[kind], key=lambda c: c["category"])
        with open(OUT_DIR / fname, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"{fname}: {len(rows)}건")
    print(f"토큰 사용량: 입력 {usage_in:,} / 출력 {usage_out:,}")
    if failed:
        print(f"실패 {len(failed)}건: {failed}")


if __name__ == "__main__":
    main()
