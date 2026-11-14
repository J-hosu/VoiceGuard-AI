"""공개 데이터로 학습셋을 구성한다 (build_dataset.py의 원본 데이터를 구할 수 없을 때 대체용).

출력: data/PhishCatch-Data.json (build_dataset.py와 동일 스키마)
  {"cases": [{"id", "label", "turns": [{"turn_index", "text"}]}]}

원본 → 대체 매핑:
  피싱 3종(그놈 목소리 수사기관/대출, 바로 이 목소리) → KorCCVi vishing (금감원 공개 녹취 706건)
  정상 금융상담 + 자유대화                          → KorCCVi non_vishing (AI Hub 전화망 대화 711건)
                                                    + Claude 생성 인바운드 금융상담 (finance_generated.json)
  직접 작성한 아웃바운드 권유 시나리오               → Claude 생성 아웃바운드 통화 (outbound_generated.json)

KorCCVi 전사본은 두 클래스 모두 같은 STT(Naver CLOVA)로 만들어져 STT 스타일이 라벨 지름길이
되지 않는다. 출처: https://github.com/selfcontrol7/Korean_Voice_Phishing_Detection (DATA.md)
금감원 파일명으로 대출 사기형과 그 외(수사기관 사칭 등)를 나눠 id에 표시한다.

실행:
    .venv/bin/python -m backend.app.build_public_dataset
"""
import csv
import glob
import json
import os
import re

from backend.app.build_dataset import make_case
from backend.app.paths import DATA_DIR


OUT_PATH = os.path.join(DATA_DIR, "PhishCatch-Data.json")
KORCCVI_DIR = os.path.join(DATA_DIR, "raw", "korccvi", "Multimodal", "data")

_LOAN = re.compile(r"대출|대환|햇살론|저금리|신용(등급|평점|점수)|금리|상환|보증|선입금|한도")


def _segments_text(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return [s["text"] for s in sorted(d.get("segments", []), key=lambda s: s["start"])]


def _fss_source_names():
    mapping = {}
    with open(os.path.join(KORCCVI_DIR, "fss_source_file_mapping.csv"), encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mapping[os.path.splitext(row["manifest_file"])[0]] = row["fss_source_file"]
    return mapping


def _file_index(path):
    return int(re.search(r"(\d+)\.json$", path).group(1))


def load_phishing():
    sources = _fss_source_names()
    counters = {"daechul": 0, "susagigwan": 0}
    cases = []
    for path in sorted(glob.glob(os.path.join(KORCCVI_DIR, "transcripts", "vishing", "*.json")), key=_file_index):
        stem = os.path.splitext(os.path.basename(path))[0]
        code = "daechul" if _LOAN.search(sources.get(stem, "")) else "susagigwan"
        counters[code] += 1
        cases.append(make_case(f"phishing_{code}_{counters[code]:04d}", 1, _segments_text(path)))
    print(f"phishing/KorCCVi: {len(cases)}건 (대출 사기형 {counters['daechul']} / 수사기관 사칭 등 {counters['susagigwan']})")
    return cases


def load_phone():
    paths = sorted(glob.glob(os.path.join(KORCCVI_DIR, "transcripts", "non_vishing", "*.json")))
    cases = [make_case(f"normal_phone_{i:04d}", 0, _segments_text(p)) for i, p in enumerate(paths, 1)]
    print(f"normal/KorCCVi 전화 대화(AI Hub): {len(cases)}건")
    return cases


def load_generated(fname, code):
    with open(os.path.join(DATA_DIR, "normal", fname), encoding="utf-8") as f:
        calls = json.load(f)
    cases = [
        make_case(f"normal_{code}_{i:04d}", 0, [t["text"] for t in call["turns"]])
        for i, call in enumerate(calls, 1)
    ]
    print(f"normal/{fname}(Claude 생성): {len(cases)}건")
    return cases


def main():
    cases = (
        load_phishing()
        + load_phone()
        + load_generated("finance_generated.json", "finance")
        + load_generated("outbound_generated.json", "outbound")
    )

    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "id 중복 발생"
    empty = [c["id"] for c in cases if not c["turns"]]
    if empty:
        print(f"경고: 발화가 없는 케이스 {len(empty)}건 제외: {empty[:5]}")
        cases = [c for c in cases if c["turns"]]

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False, indent=2)

    n_phish = sum(1 for c in cases if c["label"] == 1)
    n_normal = len(cases) - n_phish
    n_turns = sum(len(c["turns"]) for c in cases)
    size_mb = os.path.getsize(OUT_PATH) / 1024 / 1024
    print(f"\n완료: {OUT_PATH} ({size_mb:.1f}MB)")
    print(f"  전체 {len(cases)}건 = 피싱 {n_phish} + 정상 {n_normal} / 총 발화 {n_turns:,}턴")


if __name__ == "__main__":
    main()
