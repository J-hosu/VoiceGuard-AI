"""전사 정확도 비교표: 원래 Whisper / 추가 학습 Whisper / 리턴제로를 같은 시험 녹음으로 비교한다.

입력 (있는 것만 쓴다)
  파일 전체: runs/eval_base_full.json, runs/eval_ft_full.json, runs/eval_rtzr_batch.json, runs/eval_rtzr_whisper_batch.json
  실시간 조각: runs/eval_base_live.json, runs/eval_ft_live.json, runs/eval_rtzr_stream.json
  노트북 설정(CPU int8·beam 1): runs/cpu/eval_*_{full,live}.json (run_cpu_eval.sh)
비교는 모든 시스템에 결과가 있는 녹음(id 교집합)만 쓴다. CER = 전체 편집 수 / 전체 정답 글자 수(공백·문장부호 제외).
시스템 간 차이는 녹음 단위 쌍대 부트스트랩(2,000회)으로 95% 구간을 낸다(구간이 0을 지나지 않으면 차이가 있다고 본다).
출력: 화면 표 + runs/compare.json
사용: ~/voiceguard/.venv/bin/python -X utf8 scripts/compare.py
"""
import json
from pathlib import Path

import numpy as np

ROOT = Path.home() / "voiceguard_ft" / "runs"
SYSTEMS = {
    "full": [("large-v3 원래", "eval_base_full.json"), ("large-v3 추가 학습", "eval_ft_full.json"),
             ("turbo 원래", "eval_turbo_base_full.json"), ("turbo 추가 학습", "eval_turbo_ft_full.json"),
             ("small 원래", "eval_small_base_full.json"), ("small 추가 학습", "eval_small_ft_full.json"),
             ("small 원래 · CPU", "cpu/eval_small_base_full.json"), ("small 추가 학습 · CPU", "cpu/eval_small_ft_full.json"),
             ("turbo 원래 · CPU", "cpu/eval_turbo_base_full.json"), ("turbo 추가 학습 · CPU", "cpu/eval_turbo_ft_full.json"),
             ("리턴제로", "eval_rtzr_batch.json")],
    "live": [("large-v3 원래", "eval_base_live.json"), ("large-v3 추가 학습", "eval_ft_live.json"),
             ("turbo 원래", "eval_turbo_base_live.json"), ("turbo 추가 학습", "eval_turbo_ft_live.json"),
             ("small 원래", "eval_small_base_live.json"), ("small 추가 학습", "eval_small_ft_live.json"),
             ("small 원래 · CPU", "cpu/eval_small_base_live.json"), ("small 추가 학습 · CPU", "cpu/eval_small_ft_live.json"),
             ("turbo 원래 · CPU", "cpu/eval_turbo_base_live.json"), ("turbo 추가 학습 · CPU", "cpu/eval_turbo_ft_live.json"),
             ("리턴제로", "eval_rtzr_stream.json")],
}
PAIRS = [("large-v3 원래", "large-v3 추가 학습"), ("turbo 원래", "turbo 추가 학습"), ("small 원래", "small 추가 학습"),
         ("리턴제로", "large-v3 추가 학습"), ("리턴제로", "turbo 추가 학습"), ("리턴제로", "small 추가 학습"),
         ("리턴제로", "large-v3 원래"),
         # 노트북 서비스 설정(CPU int8·beam 1, runs/cpu/): 설정 때문에 잃는 정확도, CPU에서 고를 모델
         ("small 추가 학습", "small 추가 학습 · CPU"), ("turbo 추가 학습", "turbo 추가 학습 · CPU"),
         ("small 원래 · CPU", "small 추가 학습 · CPU"), ("turbo 원래 · CPU", "turbo 추가 학습 · CPU"),
         ("small 추가 학습 · CPU", "turbo 추가 학습 · CPU"), ("리턴제로", "small 추가 학습 · CPU"), ("리턴제로", "turbo 추가 학습 · CPU")]
rng = np.random.default_rng(0)
report = {}

# 녹음마다 묶음 정보: 숫자 표기 규칙(대본이 숫자를 아라비아 숫자로 적었나 한글로 적었나)과
# 가장 가까운 학습 녹음과의 시간 차(같은 녹음 세션이면 학습 효과가 부풀 수 있다)
import re
from datetime import datetime

META = Path.home() / "voiceguard_ft" / "meta"
_files = [json.loads(l) for l in open(META / "files.jsonl", encoding="utf-8")]
_manifest = json.loads((META / "manifest.json").read_text(encoding="utf-8"))
_ts = re.compile(r"(\d{8}) (\d{6})")


def _when(f):
    m = _ts.search(f["orig"])
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None


_train_times = {}
for f in _files:
    if f["split"] == "train" and _when(f):
        _train_times.setdefault(f["domain"], []).append(_when(f))
TAGS = {}
for f in _files:
    zip_name = f["id"].split("/")[0] + ".zip"
    style = "숫자 한글 표기 대본" if "220125" in _manifest[zip_name]["source_dir"] else "숫자 아라비아 표기 대본"
    w = _when(f)
    if w is None or f["domain"] not in _train_times:
        gap = "시각 없음"
    else:
        mins = min(abs((w - t).total_seconds()) for t in _train_times[f["domain"]]) / 60
        gap = "학습 녹음과 10분 이내" if mins <= 10 else ("10~60분" if mins <= 60 else "60분 넘게 떨어짐")
    TAGS[f["id"]] = {"style": style, "gap": gap}
for mode, systems in SYSTEMS.items():
    loaded = {}
    for name, fname in systems:
        p = ROOT / fname
        if not p.exists():
            continue
        rows = json.loads(p.read_text(encoding="utf-8"))["rows"]
        key = f"{mode}_edits"
        loaded[name] = {r["id"]: r for r in rows if key in r and "error" not in r}
    if not loaded:
        continue
    ids = sorted(set.intersection(*(set(v) for v in loaded.values())))
    if not ids:
        continue
    any_rows = next(iter(loaded.values()))
    domains = sorted({any_rows[i]["domain"] for i in ids})
    out = {"recordings": len(ids), "audio_hours": round(sum(any_rows[i]["seconds"] for i in ids) / 3600, 2), "systems": {}}
    E = {n: np.array([loaded[n][i][f"{mode}_edits"] for i in ids], dtype=float) for n in loaded}
    N = np.array([any_rows[i]["ref_len"] for i in ids], dtype=float)
    for n in loaded:
        s = {"cer": round(float(E[n].sum() / N.sum()), 4)}
        for d in domains:
            m = np.array([any_rows[i]["domain"] == d for i in ids])
            s[d] = round(float(E[n][m].sum() / N[m].sum()), 4)
        lat = [loaded[n][i].get("live_sec_per_chunk") or loaded[n][i].get("finalize_latency_mean") for i in ids]
        lat = [x for x in lat if x is not None]
        if lat:
            s["sec_per_chunk"] = round(float(np.mean(lat)), 3)
        out["systems"][n] = s
    names = list(loaded)
    pairs = []
    for na, nb in [(x, y) for x, y in PAIRS if x in loaded and y in loaded]:
        if True:
            idx = rng.integers(0, len(ids), size=(2000, len(ids)))
            diffs = E[nb][idx].sum(1) / N[idx].sum(1) - E[na][idx].sum(1) / N[idx].sum(1)
            lo, hi = np.percentile(diffs, [2.5, 97.5])
            point = E[nb].sum() / N.sum() - E[na].sum() / N.sum()
            pairs.append({"a": na, "b": nb, "b_minus_a": round(float(point), 4), "ci95": [round(float(lo), 4), round(float(hi), 4)],
                          "b_better_share": round(float(np.mean(E[nb] / N < E[na] / N)), 3)})
    out["pairs"] = pairs
    out["by_group"] = {}
    for tag in ("style", "gap"):
        for g in sorted({TAGS.get(i, {}).get(tag, "?") for i in ids}):
            m = np.array([TAGS.get(i, {}).get(tag, "?") == g for i in ids])
            out["by_group"][g] = {"recordings": int(m.sum()),
                                  **{n: round(float(E[n][m].sum() / N[m].sum()), 4) for n in loaded}}
    report[mode] = out

    title = "파일 전체 (녹음 업로드·통화 후 재전사)" if mode == "full" else "실시간 조각 (앱과 같은 무음 자르기)"
    print(f"\n### {title}: 녹음 {out['recordings']}개, {out['audio_hours']}시간")
    head = "| 시스템 | 전체 CER | " + " | ".join(domains) + (" | 조각당 지연(초) |" if mode == "live" else " |")
    print(head)
    print("|" + " --- |" * (head.count("|") - 1))
    for n, s in out["systems"].items():
        cells = [f"{s['cer'] * 100:.2f}%"] + [f"{s[d] * 100:.2f}%" for d in domains]
        if mode == "live":
            cells.append(str(s.get("sec_per_chunk", "-")))
        print(f"| {n} | " + " | ".join(cells) + " |")
    for p in pairs:
        print(f"- {p['b']} − {p['a']}: {p['b_minus_a'] * 100:+.2f}%p (95% 구간 {p['ci95'][0] * 100:+.2f} ~ {p['ci95'][1] * 100:+.2f}), "
              f"녹음별로 {p['b']}가 더 나은 비율 {p['b_better_share'] * 100:.0f}%")
    print("\n| 묶음 | 녹음 수 | " + " | ".join(names) + " |")
    print("|" + " --- |" * (len(names) + 2))
    for g, v in out["by_group"].items():
        print(f"| {g} | {v['recordings']} | " + " | ".join(f"{v[n] * 100:.2f}%" for n in names) + " |")
(ROOT / "compare.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
