"""피싱용 위조 문서 점검 평가.

평가 세트 폴더(기본 data/eval_documents)의 labels.json과 이미지로 문서 점검 기능의 정확도를 잰다.
  labels.json: [{"file": "images/xxx.jpg", "label": "forged" | "genuine", "doc_type", "source_url",
                 "publisher", "note", "split": "dev" | "test", "sha256", "caption_visible": bool}]
라벨은 공개한 기관·언론이 "사기범이 만든 문서"라고 밝힌 것만 forged, 발급 기관이 공개한 실제 문서·서식만
genuine이다(세트 구성 방법은 같은 폴더의 README.md).

이미지만 넣는다(사용자 상황 설명은 넣지 않는다). 기관 카드뉴스에서 가져온 이미지에는 '가짜' 같은 문구가
남아 있을 수 있어서, 그런 이미지(caption_visible)와 아닌 이미지의 결과를 나눠서도 보여준다.

실행:
    .venv/bin/python -m backend.app.eval_documents                 # 전체
    SPLIT=dev .venv/bin/python -m backend.app.eval_documents       # dev만(프롬프트 조정용)
    SPLIT=test .venv/bin/python -m backend.app.eval_documents      # test만(최종 보고용)
    REPORT_MODEL=claude-haiku-5-5 ... 으로 모델을 바꿔 비교할 수 있다. Claude API 비용이 든다.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from backend.app.paths import DATA_DIR, ENV_PATH
from backend.app.services import claude_client, document_analyzer
from backend.app.services.koelectra_scorer import TH_WARNING


EVAL_DIR = Path(os.environ.get("EVAL_DOCS_DIR") or DATA_DIR / "eval_documents")
SPLIT = os.environ.get("SPLIT", "").strip().lower()
WORKERS = int(os.environ.get("WORKERS", "4"))
OUT_PATH = DATA_DIR / "eval_documents_last_run.json"
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def _run_one(item: dict) -> dict:
    path = EVAL_DIR / item["file"]
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    record = {**item, "risk_score": None, "readable": None, "flags": [], "doc_type_pred": "", "error": ""}
    if media_type is None:
        record["error"] = f"지원하지 않는 확장자: {path.suffix}"
        return record
    try:
        result = document_analyzer.analyze(media_type, path.read_bytes())
    except Exception as exc:  # 한 장이 실패해도 나머지는 계속 잰다
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return record
    record.update(
        risk_score=result["risk_score"],
        readable=result["readable"],
        flags=[flag["code"] for flag in result["flags"]],
        doc_type_pred=result["doc_type"],
        explanation=result["explanation"],
        evidence=result["evidence"],
    )
    return record


def _metrics(rows: list[dict], threshold: float) -> dict:
    tp = sum(1 for r in rows if r["label"] == "forged" and r["risk_score"] >= threshold)
    fn = sum(1 for r in rows if r["label"] == "forged" and r["risk_score"] < threshold)
    fp = sum(1 for r in rows if r["label"] == "genuine" and r["risk_score"] >= threshold)
    tn = sum(1 for r in rows if r["label"] == "genuine" and r["risk_score"] < threshold)
    total = tp + fn + fp + tn
    return {
        "n": total,
        "accuracy": round((tp + tn) / total, 4) if total else None,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "fpr": round(fp / (fp + tn), 4) if fp + tn else None,
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
    }


def _fmt(name: str, m: dict) -> str:
    def pct(value):
        return "-" if value is None else f"{value * 100:.1f}%"

    return (
        f"  {name:<26} n={m['n']:<3} 정확도 {pct(m['accuracy'])}  정밀도 {pct(m['precision'])}  "
        f"재현율 {pct(m['recall'])}  오경보율 {pct(m['fpr'])}  (TP {m['tp']} FN {m['fn']} FP {m['fp']} TN {m['tn']})"
    )


def main() -> None:
    load_dotenv(ENV_PATH)
    if not claude_client.is_configured():
        raise SystemExit("Claude API 키가 없어 평가할 수 없습니다.")
    with open(EVAL_DIR / "labels.json", encoding="utf-8") as f:
        items = json.load(f)
    if SPLIT:
        items = [item for item in items if item.get("split") == SPLIT]
    print(f"평가 세트 {EVAL_DIR} · {len(items)}장 (split={SPLIT or '전체'}) · 모델 {claude_client.report_model()}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        rows = list(pool.map(_run_one, items))

    failed = [r for r in rows if r["risk_score"] is None]
    scored = [r for r in rows if r["risk_score"] is not None]
    print(f"\n채점 {len(scored)}장 · 실패 {len(failed)}장 · 판독 불가 {sum(1 for r in scored if r['readable'] is False)}장")
    for threshold in (TH_WARNING, 0.5):
        print(f"\n[기준 {threshold}]")
        for split in ("dev", "test"):
            part = [r for r in scored if r.get("split") == split]
            if part:
                print(_fmt(split, _metrics(part, threshold)))
        print(_fmt("전체", _metrics(scored, threshold)))
        print(_fmt("문구 보이는 이미지", _metrics([r for r in scored if r.get("caption_visible")], threshold)))
        print(_fmt("문구 없는 이미지", _metrics([r for r in scored if not r.get("caption_visible")], threshold)))

    by_type: dict[tuple, list] = defaultdict(list)
    for r in scored:
        by_type[(r["label"], r.get("doc_type", ""))].append(r)
    print(f"\n문서 유형별 (기준 {TH_WARNING} 이상을 위조로 판정한 비율)")
    for (label, doc_type), group in sorted(by_type.items()):
        hit = sum(1 for r in group if r["risk_score"] >= TH_WARNING)
        print(f"  {label:<8} {doc_type[:28]:<30} {hit}/{len(group)}")

    wrong = [
        r for r in scored
        if (r["label"] == "forged") != (r["risk_score"] >= TH_WARNING)
    ]
    print(f"\n오답 {len(wrong)}건")
    for r in wrong:
        print(f"  [{r['label']}→{r['risk_score']:.2f}] {r['file']} · {r.get('doc_type', '')} · 판독 {r['doc_type_pred']} · 신호 {r['flags']}")
        print(f"      {str(r.get('explanation', ''))[:160]}")
    for r in failed:
        print(f"  [실패] {r['file']}: {r['error']}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"\n상세: {OUT_PATH}")


if __name__ == "__main__":
    main()
