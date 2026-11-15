"""AI Hub 상담 데이터를 통화 학습 데이터 형식(PhishCatch cases)으로 바꾼다.

두 데이터 모두 정상 통화(label 0)다. 보이스피싱과 어휘가 겹치는(계좌, 대출, 이체, 본인 확인, 환급)
진짜 상담이라, "주제어만 보고 사기로 보는" 오경보를 줄이는 어려운 정상 사례가 된다.

  finance : AI Hub 「금융분야 고객상담 데이터」 은행·보험·증권 콜센터 상담 대화 (TX=상담사, RX=고객)
            data/raw/aihub_finance_consult/{Training,Validation}/{01.원천데이터,02.라벨링데이터}/*.zip
  minwon  : AI Hub 「민원(콜센터) 질의-응답 데이터」 금융·보험, 서울 다산콜센터, 질병관리청, 홈쇼핑 콜센터
            data/raw/aihub_minwon/{1.Training,2.Validation}/라벨링데이터_*/**.zip (발화 단위 레코드)

AI Hub의 Training/Validation 구분을 split 필드로 그대로 둔다. Validation은 학습에 넣지 않고
오경보 평가에만 쓴다.

가림 표시 정리: 금융분야 데이터는 숫자·이름을 '●'로, 기관명을 'OO은행'처럼 가렸다. 기존 학습 데이터에는
'●'가 전혀 없고 'OO'는 생성한 사기 통화에만 조금 있어서, 그대로 넣으면 모델이 "●가 있으면 정상"을
배운다. 그래서 'OO은행' 류는 레코드의 실제 기관명으로 바꾸고, 나머지 가림 표시는 지운다.

표준 라이브러리만 쓴다 (내려받은 데이터를 읽으므로 격리 모드로 실행):
    python -I -X utf8 backend/app/convert_aihub_calls.py finance data/raw/aihub_finance_consult data/aihub/finance_consult.json
    python -I -X utf8 backend/app/convert_aihub_calls.py minwon data/raw/aihub_minwon data/aihub/minwon.json
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path


INSTITUTION_SUFFIXES = (
    "저축은행", "은행", "생명보험", "생명", "화재해상보험", "해상화재", "화재", "손해보험", "보험",
    "투자증권", "금융투자", "증권", "자산운용", "캐피탈", "카드", "금융",
)
_MASKED_INSTITUTION = re.compile(
    r"(?:OO|ㅇㅇ|[○〇▲△●]{2,}|xx|XX)\s?(?=(저축은행|은행|카드|생명|화재|손해보험|손보|보험|투자증권|증권|자산운용|캐피탈|금융|페이))"
)
# 기호 가림(●, ▲, △ 등)은 붙은 글자와 상관없이 지우고, 영문자로 쓴 가림(OO, XX)은 영단어 안이 아닐 때만 지운다.
_MASK = re.compile(
    r"[○●〇◯□■◆◇★☆※▲△▼▽]+"
    r"|(?<![A-Za-z])(?:O{2,}|ㅇㅇ+|X{2,}|x{2,}|\*{2,})(?![A-Za-z])"
    r"|(?<![A-Za-z])O(?:[\s\-]+O)+(?![A-Za-z])"  # 'O O O O' 처럼 띄어 쓴 가림
)
_ORPHAN_DASHES = re.compile(r"(?<!\S)-(?:\s+-)*(?!\S)")  # 가린 계좌·전화번호에서 남은 '- -'
_SPACES = re.compile(r"\s+")
MIN_TURNS = 3


def fix_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 표시가 없는 zip 항목 이름은 CP949로 다시 읽는다 (AI Hub zip은 대부분 CP949)."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def institution_stem(name: str) -> str:
    name = (name or "").strip()
    for suffix in INSTITUTION_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def clean_text(text: str, institution: str = "") -> str:
    stem = institution_stem(institution)
    if stem:
        text = _MASKED_INSTITUTION.sub(stem, text)
    # '●'를 지우면 'O●O'가 'O O'가 되는 식으로 새 가림 표시가 드러나므로 바뀌지 않을 때까지 반복한다.
    for _ in range(4):
        cleaned = _SPACES.sub(" ", _ORPHAN_DASHES.sub(" ", _MASK.sub(" ", text)))
        if cleaned == text:
            break
        text = cleaned
    return text.strip()


def label_text(value: object) -> str:
    """라벨 문자열(카테고리 등)의 유니코드 정규화 형태(NFC/NFD)와 공백 차이를 없앤다."""
    return _SPACES.sub(" ", unicodedata.normalize("NFC", str(value or ""))).strip()


def _turns(utterances: list[tuple[str, str]]) -> list[dict]:
    return [
        {"turn_index": index, "speaker": speaker, "text": text}
        for index, (speaker, text) in enumerate(utterances, start=1)
    ]


# ───────────────────────── 금융분야 고객상담 ─────────────────────────

FIN_CATEGORY = {"은행": "bank", "보험": "insurance", "증권": "securities"}


def parse_consulting_content(content: str, institution: str) -> list[tuple[str, str]]:
    """'TX 안녕하세요…\\nRX 네…' 형식을 (화자, 발화) 목록으로 바꾼다. 접두어 없는 줄은 앞 발화에 붙인다."""
    utterances: list[list[str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(TX|RX)\s*[:：]?\s*(.*)$", line)
        if match:
            speaker = "agent" if match.group(1) == "TX" else "customer"
            utterances.append([speaker, match.group(2)])
        elif utterances:
            utterances[-1][1] += " " + line
        else:
            utterances.append(["unknown", line])
    cleaned = [(speaker, clean_text(text, institution)) for speaker, text in utterances]
    return [(speaker, text) for speaker, text in cleaned if text]


def convert_finance(root: Path) -> list[dict]:
    topics: dict[str, str] = {}
    for zip_path in sorted(root.glob("*/02.라벨링데이터/*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                data = json.loads(decode(archive.read(info)))
                source_id = data.get("source", {}).get("source_id")
                topic = data.get("consulting", {}).get("consulting_topic", "")
                if source_id and topic and source_id not in topics:
                    topics[source_id] = topic

    cases = []
    seen: set[str] = set()
    counters: Counter = Counter()
    for zip_path in sorted(root.glob("*/01.원천데이터/*.zip")):
        split = "train" if zip_path.parent.parent.name.lower().startswith("train") else "valid"
        category_ko = zip_path.stem.split("_", 1)[-1]  # TS_은행 -> 은행
        category = FIN_CATEGORY.get(category_ko, "other")
        with zipfile.ZipFile(zip_path) as archive:
            for info in sorted(archive.infolist(), key=fix_name):
                if info.is_dir():
                    continue
                source = json.loads(decode(archive.read(info))).get("source", {})
                source_id = source.get("source_id") or fix_name(info).rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if source_id in seen:
                    continue
                utterances = parse_consulting_content(source.get("consulting_content", ""), source.get("source_institution", ""))
                key = " ".join(text for _, text in utterances)
                if len(utterances) < MIN_TURNS or key in seen:
                    continue
                seen.add(source_id)
                seen.add(key)
                counters[category] += 1
                cases.append(
                    {
                        "id": f"normal_aihubfin_{category}_{counters[category]:05d}",
                        "label": 0,
                        "group": f"aihubfin_{source_id}",
                        "source": "aihub_finance_consult",
                        "split": split,
                        "domain": category_ko,
                        "category": label_text(topics.get(source_id, "")),
                        "institution": source.get("source_institution", ""),
                        "turns": _turns(utterances),
                    }
                )
    return cases


# ───────────────────────── 민원(콜센터) 질의-응답 ─────────────────────────

MINWON_DOMAIN = {"금융/보험": "fin", "금융보험": "fin", "다산콜센터": "dasan", "질병관리본부": "kdca", "K쇼핑": "shop", "쇼핑": "shop"}
TEXT_KEYS = ("고객질문(요청)", "상담사질문(요청)", "고객답변", "상담사답변")


def _record_text(record: dict) -> str:
    for key in TEXT_KEYS:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    for key, value in record.items():  # 키 이름이 조금 다른 파일 대비
        if ("질문" in key or "답변" in key) and str(value or "").strip():
            return str(value).strip()
    return ""


def _domain_code(record: dict, path_text: str) -> str:
    domain = label_text(record.get("도메인"))
    if domain in MINWON_DOMAIN:
        return MINWON_DOMAIN[domain]
    for name, code in MINWON_DOMAIN.items():
        if name in path_text:
            return code
    return "other"


def convert_minwon(root: Path) -> list[dict]:
    cases = []
    seen: set[str] = set()
    counters: Counter = Counter()
    for zip_path in sorted(root.glob("*/라벨링데이터_*/**/*.zip")):
        split = "train" if "Training" in zip_path.parts[-3] or "Training" in str(zip_path) else "valid"
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not fix_name(info).lower().endswith(".json"):
                    continue
                records = json.loads(decode(archive.read(info)))
                if isinstance(records, dict):
                    records = records.get("data") or records.get("documents") or list(records.values())[0]
                dialogues: dict[str, list[dict]] = defaultdict(list)
                for record in records:
                    dialogue_id = str(record.get("대화셋일련번호") or "").strip()
                    if dialogue_id:
                        dialogues[dialogue_id].append(record)
                for dialogue_id, rows in dialogues.items():
                    rows.sort(key=lambda row: int(re.sub(r"\D", "", str(row.get("문장번호") or "0")) or 0))
                    utterances = []
                    for row in rows:
                        text = clean_text(_record_text(row))
                        if not text:
                            continue
                        speaker = "customer" if str(row.get("화자", "")).strip() == "고객" else "agent"
                        if utterances and utterances[-1][0] == speaker:
                            utterances[-1] = (speaker, utterances[-1][1] + " " + text)
                        else:
                            utterances.append((speaker, text))
                    key = " ".join(text for _, text in utterances)
                    if len(utterances) < MIN_TURNS or key in seen:
                        continue
                    seen.add(key)
                    first = rows[0]
                    domain = _domain_code(first, str(zip_path))
                    counters[domain] += 1
                    cases.append(
                        {
                            "id": f"normal_aihubminwon_{domain}_{counters[domain]:06d}",
                            "label": 0,
                            "group": f"aihubminwon_{domain}_{zip_path.stem}_{dialogue_id}",
                            "source": "aihub_minwon",
                            "split": split,
                            "domain": label_text(first.get("도메인") or domain),
                            "category": label_text(first.get("카테고리") or zip_path.stem).upper(),
                            "turns": _turns(utterances),
                        }
                    )
    return cases


def summarize(cases: list[dict]) -> None:
    by = defaultdict(list)
    for case in cases:
        by[(case["split"], case["id"].rsplit("_", 1)[0], case["category"])].append(case)
    print(f"총 {len(cases)}건")
    print(f"{'split':<6}{'group':<30}{'category':<26}{'n':>7}{'turns':>7}{'chars/turn':>11}")
    for (split, group, category), rows in sorted(by.items()):
        turns = sum(len(case["turns"]) for case in rows)
        chars = sum(len(turn["text"]) for case in rows for turn in case["turns"])
        print(f"{split:<6}{group:<30}{category[:24]:<26}{len(rows):>7}{turns / len(rows):>7.1f}{chars / max(1, turns):>11.1f}")
    leftovers = Counter()
    for case in cases:
        text = " ".join(turn["text"] for turn in case["turns"])
        for ch in set(text):
            if not (ch.isalnum() or ch.isspace() or "가" <= ch <= "힣" or ch in ".,?!~%-/:()'\""):
                leftovers[ch] += 1
    print("남은 기호(통화 수):", " ".join(f"{c}:{n}" for c, n in leftovers.most_common(20)))


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ("finance", "minwon"):
        print(__doc__)
        sys.exit(2)
    kind, root, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    cases = convert_finance(root) if kind == "finance" else convert_minwon(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False)
    summarize(cases)
    print(f"저장: {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
