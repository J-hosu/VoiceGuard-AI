"""능동형 대응 루프 회귀 평가.

backend/eval/scenarios.json의 통화 대본을 두 발화씩 흘려보내며 빠른 경로 → 상태 추적 →
융합 → 대응 결정을 실제로 돌리고, 기대값(라벨·유형·도달 단계)과 비교한다.
운영 DB를 건드리지 않도록 data/eval.db를 새로 만들어 쓴다. Claude API를 호출하므로 비용이 든다.

실행:
    python -m backend.app.eval_loop              # 전체
    python -m backend.app.eval_loop agency loan  # id에 해당 문자열이 들어간 시나리오만
    SCENARIOS=data/aihub/hard_normal_scenarios.json python -m backend.app.eval_loop  # 다른 대본 파일
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from backend.app import database
from backend.app.paths import BACKEND_DIR, DATA_DIR, ENV_PATH

load_dotenv(ENV_PATH)

# 평가를 여러 개 동시에 돌릴 때는 EVAL_DB로 서로 다른 파일을 쓴다.
EVAL_DB_PATH = Path(os.environ.get("EVAL_DB") or DATA_DIR / "eval.db")
SCENARIOS_PATH = Path(os.environ.get("SCENARIOS") or BACKEND_DIR / "eval" / "scenarios.json")
STEP = 2  # 한 번에 흘려보낼 발화 수(실시간에서 추적기가 도는 간격과 비슷하게)


def main(filters: list[str]) -> int:
    # 평가 전용 DB로 바꿔치기한다(저장소 계층은 database.get_connection만 쓴다).
    if EVAL_DB_PATH.exists():
        EVAL_DB_PATH.unlink()
    database.DB_PATH = EVAL_DB_PATH
    database.init_db()

    from backend.app import state_repository
    from backend.app.codes import stage_index
    from backend.app.repository import create_call_log, get_call_log, insert_call_message
    from backend.app.schemas import CallLogCreate, CallMessageCreate
    from backend.app.services import agent_loop, call_analyzer

    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))["scenarios"]
    if filters:
        scenarios = [s for s in scenarios if any(f in s["id"] for f in filters)]

    rows = []
    for scenario in scenarios:
        started = time.monotonic()
        call = create_call_log(CallLogCreate(device_id=None, name=f"eval:{scenario['id']}", file_type="realtime"))
        first_warning_turn = None
        turns = scenario["turns"]
        for offset in range(0, len(turns), STEP):
            for content in turns[offset : offset + STEP]:
                insert_call_message(call.id, CallMessageCreate(content=content))
            call_analyzer.detect_and_persist(call.id, use_llm_evidence=False, agent_mode=True)
            cycle = agent_loop.run_cycle(call.id)
            if cycle and first_warning_turn is None and any(a["action_type"] == "warn" for a in cycle["actions"]):
                first_warning_turn = min(offset + STEP, len(turns))
        agent_loop.end_call(call.id)

        final_call = get_call_log(call.id)
        state = state_repository.get_latest_state(call.id) or {"stage": "none", "scam_type": "none"}
        predicted = 1 if final_call.risk_level in ("medium", "high") else 0
        checks = {
            "label": predicted == scenario["label"],
            "type": state["scam_type"] == scenario["scam_type"],
            "stage": stage_index(state["stage"]) >= stage_index(scenario["min_stage"]),
        }
        if scenario["label"] == 0:
            # 정상 통화는 단계가 none이어야 하고 경고가 한 번도 나가면 안 된다.
            checks["stage"] = state["stage"] == "none" and first_warning_turn is None
        rows.append(
            {
                "id": scenario["id"],
                "label": scenario["label"],
                "predicted": predicted,
                "risk": round(final_call.risk_score, 3),
                "stage": state["stage"],
                "scam_type": state["scam_type"],
                "warn_turn": first_warning_turn,
                "turns": len(turns),
                "actions": [a["action_type"] for a in state_repository.list_actions(call.id)],
                "checks": checks,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        row = rows[-1]
        mark = "PASS" if all(checks.values()) else "FAIL " + ",".join(k for k, ok in checks.items() if not ok)
        print(
            f"{mark:16} {row['id']:34} risk={row['risk']:<5} stage={row['stage']:13} type={row['scam_type']:12} "
            f"warn@{row['warn_turn']}/{row['turns']} {row['seconds']}s"
        )

    tp = sum(1 for r in rows if r["label"] == 1 and r["predicted"] == 1)
    fp = sum(1 for r in rows if r["label"] == 0 and r["predicted"] == 1)
    fn = sum(1 for r in rows if r["label"] == 1 and r["predicted"] == 0)
    tn = sum(1 for r in rows if r["label"] == 0 and r["predicted"] == 0)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    passed = sum(1 for r in rows if all(r["checks"].values()))
    print()
    print(f"시나리오 {passed}/{len(rows)} 통과 | precision={precision:.2f} recall={recall:.2f} "
          f"fpr={(fp / (fp + tn) if fp + tn else 0.0):.2f} (TP={tp} FP={fp} FN={fn} TN={tn})")

    out_path = Path(os.environ.get("EVAL_OUT") or DATA_DIR / "eval_last_run.json")
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세 결과: {out_path}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
