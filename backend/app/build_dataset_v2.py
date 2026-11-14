"""학습 데이터 v2: 기존 데이터에 공공기관 공개 자료를 더한다.

기존 data/PhishCatch-Data.json (금감원 녹취 전사 + 정상 통화 + 생성 정상 금융 통화)에 다음을 추가해
data/PhishCatch-Data-v2.json 을 만든다.

  - normal_kisa118:   KISA 118 전화상담 합성데이터(공공데이터포털, 이용허락 제한 없음).
                      피싱·스미싱·해킹 피해를 상담하는 정상 통화라서 "피싱이 주제지만 사기는 아닌"
                      어려운 정상 사례가 된다.  data/raw/kisa118/*.zip
  - phishing_official_<유형>, normal_twin_<유형>:
                      금감원 피해사례·소비자경보, 경찰청 예·경보를 근거로 생성한 사기 통화와
                      같은 주제의 정상 통화(generate_official_scenarios.py 출력).

각 통화에는 group을 붙인다. 같은 공식 사례에서 나온 통화들은 같은 group이라 해시 분할
(SPLIT=hash)에서 학습/평가 한쪽에만 들어간다.

실행:
    .venv/bin/python -m backend.app.build_dataset_v2
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter

from backend.app.paths import DATA_DIR


BASE_PATH = DATA_DIR / "PhishCatch-Data.json"
OUT_PATH = DATA_DIR / "PhishCatch-Data-v2.json"
KISA_DIR = DATA_DIR / "raw" / "kisa118"
GENERATED_DIR = DATA_DIR / "generated"
MIN_TURNS = 4


def _turns(texts: list[str]) -> list[dict]:
    return [{"turn_index": index, "text": text} for index, text in enumerate(texts, start=1)]


def load_kisa() -> list[dict]:
    """KISA 118 상담 통화. 파일 하나가 통화 하나, 한 줄이 한 발화다."""
    cases = []
    seen: set[str] = set()
    for zip_path in sorted(KISA_DIR.glob("*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue
                raw = archive.read(info)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("cp949", errors="ignore")
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                key = " ".join(lines)
                if len(lines) < MIN_TURNS or key in seen:
                    continue
                seen.add(key)
                number = len(cases) + 1
                cases.append({"id": f"normal_kisa118_{number:04d}", "label": 0, "group": f"kisa118_{number:04d}", "turns": _turns(lines)})
    return cases


def load_generated(fname: str, label: int, prefix: str) -> list[dict]:
    path = GENERATED_DIR / fname
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    cases = []
    counters: Counter = Counter()
    for row in rows:
        texts = [str(turn.get("text", "")).strip() for turn in row["turns"]]
        texts = [text for text in texts if text]
        if len(texts) < MIN_TURNS:
            continue
        scam_type = row.get("scam_type", "other")
        counters[scam_type] += 1
        cases.append(
            {
                "id": f"{prefix}_{scam_type}_{counters[scam_type]:04d}",
                "label": label,
                "group": f"official_{row['case_index']:04d}",
                "source_url": row.get("source_url", ""),
                "turns": _turns(texts),
            }
        )
    return cases


def main() -> None:
    with open(BASE_PATH, encoding="utf-8") as f:
        base = json.load(f)["cases"]
    added = (
        load_kisa()
        + load_generated("official_scam.json", 1, "phishing_official")
        + load_generated("official_twin.json", 0, "normal_twin")
    )
    cases = base + added
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"cases": cases}, f, ensure_ascii=False)

    counts = Counter((case["id"].rsplit("_", 1)[0], case["label"]) for case in cases)
    print(f"{OUT_PATH}: 총 {len(cases)}건 (사기 {sum(c['label'] for c in cases)}, 정상 {sum(1 - c['label'] for c in cases)})")
    for (category, label), count in sorted(counts.items()):
        print(f"  {category:<32} label={label} {count}")


if __name__ == "__main__":
    main()
