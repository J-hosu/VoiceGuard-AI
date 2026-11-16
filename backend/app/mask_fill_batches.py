"""v3c 학습 통화에서 가린 자리를 찾아 번호를 붙이고, 치환 모델에게 줄 묶음 파일을 만든다.

가린 자리는 ⟦n|원래 표시⟧로 바꿔 보여 준다. 모델은 번호마다 값만 돌려주므로 다른 글자는 바뀌지 않는다.
찾는 자리:
  - 기호 가림: ●, ★, ◆, ▲, ■, ○, OO, ㅇㅇ, XX, ** 등(AI Hub 변환기가 지우던 것)
  - AI Hub 한 글자 가림: 홀로 쓴 'O', 'ㅇ'(예: O시 분, ㅇ동 ㅇ호)
  - 0으로 채운 번호(000-00-0000), 한글로 읽은 0(공공공, 영영영)
  - 일부러 넣은 채움 값: 홍길동, 1234, 5678, 일이삼사, 오육칠팔, ____, pseudonym
실제 녹취를 받아 적은 출처(금감원, 일반 통화)는 0이나 '공공공', '홍길동'이 실제 말일 수 있어 기호 가림만 본다.

출력:
  spans.json         통화별 가린 자리 목록과 자리표시 발화(build_dataset_v5.py가 씀)
  test_batch_*.txt   모델 비교용 40통화(출처별로 고름)
  full_batch_*.txt   전체 치환용(30통화씩)
  batches.json       묶음 파일과 묶음마다 든 (통화 id, 자리 수)
사용: KEEP_MASK=1로 convert_aihub_calls.py를 다시 돌린 폴더를 keepmask_dir로 준다.
  python -X utf8 backend/app/mask_fill_batches.py <repo> <keepmask_dir> <out_dir>
"""
import ast
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

SYMBOL = r"[○●〇◯□■◆◇★☆※▲△▼▽]+|(?<![A-Za-z])(?:O{2,}|ㅇㅇ+|X{2,}|x{2,}|\*{2,})(?![A-Za-z])|(?<![A-Za-z])O(?:[\s\-]+O)+(?![A-Za-z])"
SINGLE = r"(?<![A-Za-z])O(?![A-Za-z])|(?<![ㄱ-ㅎ])ㅇ(?![ㄱ-ㅎ])"
ZEROS = r"(?<![\d,.])0{2,}(?:[- ]0{2,})+(?!\d)|(?<![\d,.])0{4,}(?!\d)"
HANGUL_ZERO = r"(?:공\s?){3,}|(?:영\s?){3,}"
DUMMY = r"(?<!\d)1234(?!\d)|(?<!\d)5678(?!\d)|홍길동|일이삼사|오육칠팔|_{2,}|pseudonym[\w.@-]*"
SEP = r"[\s\-.]*"


def span_regex(*units: str) -> re.Pattern:
    unit = "|".join(f"(?:{u})" for u in units)
    return re.compile(rf"(?:{unit})(?:{SEP}(?:{unit}))*")


AIHUB = ("normal_aihubfin", "normal_aihubminwon")
SYNTHETIC = ("normal_kisa", "normal_twin", "phishing_official")
SPAN_BY_KIND = {
    "aihub": span_regex(SYMBOL, SINGLE, ZEROS, HANGUL_ZERO, DUMMY),
    "synthetic": span_regex(SYMBOL, ZEROS, HANGUL_ZERO, DUMMY),
    "real": span_regex(SYMBOL),
}
DESCRIBE = {
    "normal_aihubfin": "정상 · 금융 콜센터 상담(AI Hub)",
    "normal_aihubminwon": "정상 · 민원 콜센터 상담(AI Hub)",
    "normal_kisa": "정상 · 피싱 피해 상담(KISA 합성)",
    "normal_twin": "정상 · 생성한 정상 통화",
    "phishing_official": "보이스피싱 · 공식 사례로 생성한 사기 통화",
    "phishing_susagigwan": "보이스피싱 · 금감원 공개 녹취",
    "phishing_daechul": "보이스피싱 · 금감원 공개 녹취",
    "normal_phone": "정상 · 일반 통화",
    "normal_finance": "정상 · 금융 통화",
    "normal_outbound": "정상 · 아웃바운드 통화",
}
SPEAKER = {"agent": "상담사", "customer": "고객"}


def group_of(case_id: str) -> str:
    return re.sub(r"\d+$", "", "_".join(case_id.split("_")[:2]))


def kind_of(group: str) -> str:
    if group in AIHUB:
        return "aihub"
    if group in SYNTHETIC:
        return "synthetic"
    return "real"


def turns_of(value):
    return ast.literal_eval(value) if isinstance(value, str) else value


def mark_turns(source_turns, span_re):
    spans, shown = [], []
    for turn in source_turns:
        def mark(match, turn=turn):
            spans.append({"n": len(spans) + 1, "turn_index": turn["turn_index"], "orig": match.group(0)})
            return f"⟦{len(spans)}|{match.group(0).strip()}⟧" + (" " if match.group(0).endswith(" ") else "")

        shown.append({"turn_index": turn["turn_index"], "speaker": turn.get("speaker", ""), "text": span_re.sub(mark, turn["text"])})
    return spans, shown


def render(call) -> str:
    head = f"### CALL {call['id']}  ({DESCRIBE.get(call['group'], call['group'])}"
    if call.get("institution"):
        head += f", 기관: {call['institution']}"
    lines = [head + f", 자리 {len(call['spans'])}개)"]
    for turn in call["turns"]:
        who = SPEAKER.get(turn["speaker"], "")
        lines.append(f"{turn['turn_index']} {who + ': ' if who else ''}{turn['text']}")
    return "\n".join(lines)


def write_batches(selected, out_dir: Path, prefix: str, size: int) -> list[dict]:
    batches = []
    for start in range(0, len(selected), size):
        chunk = selected[start:start + size]
        path = out_dir / f"{prefix}_{start // size + 1:03d}.txt"
        path.write_text("\n\n".join(render(c) for c in chunk) + "\n", encoding="utf-8")
        batches.append({"path": str(path), "calls": [[c["id"], len(c["spans"])] for c in chunk]})
    return batches


def main() -> None:
    repo, keep_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [r for r in json.load(open(repo / "data" / "PhishCatch-Data-roberta_v4.json", encoding="utf-8"))["cases"] if not r.get("source")]
    kept = {}
    for name in ("finance_consult", "minwon"):
        for case in json.load(open(keep_dir / f"{name}.json", encoding="utf-8"))["cases"]:
            kept[case["id"]] = case

    calls = []
    for row in rows:
        group = group_of(row["id"])
        v3_turns = turns_of(row["turns"])
        if row["id"] in kept:
            source_turns = kept[row["id"]]["turns_kept"]
            assert [t["text"] for t in kept[row["id"]]["turns"]] == [t["text"] for t in v3_turns], row["id"]
        else:
            source_turns = v3_turns
        spans, shown = mark_turns(source_turns, SPAN_BY_KIND[kind_of(group)])
        if spans:
            calls.append({"id": row["id"], "group": group, "label": row["label"], "turns": shown, "spans": spans,
                          "institution": kept.get(row["id"], {}).get("institution", "")})

    print("calls with spans:", len(calls), "spans:", sum(len(c["spans"]) for c in calls))
    print(Counter(c["group"] for c in calls).most_common())
    json.dump({c["id"]: c for c in calls}, open(out_dir / "spans.json", "w", encoding="utf-8"), ensure_ascii=False)

    rng = random.Random(20261116)
    by_group = defaultdict(list)
    for c in calls:
        by_group[c["group"]].append(c)
    quota = {"normal_aihubfin": 14, "normal_aihubminwon": 10, "normal_kisa": 6, "phishing_official": 8, "normal_twin": 2}
    test = []
    for g, k in quota.items():
        pool = by_group.get(g, [])
        test += rng.sample(pool, min(k, len(pool)))
    rng.shuffle(test)
    index = {"test": write_batches(test, out_dir, "test_batch", 20),
             "full": write_batches(sorted(calls, key=lambda c: c["id"]), out_dir, "full_batch", 30)}
    json.dump(index, open(out_dir / "batches.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("test batches", len(index["test"]), "full batches", len(index["full"]))


if __name__ == "__main__":
    main()
