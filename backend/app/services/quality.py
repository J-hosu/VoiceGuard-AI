"""탐지 품질 루프: 사용자 확인(피드백)을 모아 오경보·놓친 사기를 찾고, 평가 세트와 재학습 후보로 잇는다.

- 사용자 확인: 결과 화면의 "이 판정이 맞았나요?"와 통화 후 확인 답(돈을 보냈어요 → 실제 피싱, 잘못된 경고였어요 → 오경보)
- 비교: 사용자가 알려 준 정답과 시스템 판정(위험도 0.70 이상 = 사기로 판정)을 맞대어 오경보·놓친 사기를 센다.
- 평가 세트: 확인된 통화를 회귀 평가 대본 형식으로 만들어, 기본 12개 대본과 함께 다시 돌릴 수 있다.
- 재학습 후보: 확인된 통화를 학습 데이터 형식(PhishCatch cases)으로 내보낸다(build_dataset에 바로 더할 수 있음).
- 평가 실행: eval_loop를 별도 프로세스·별도 DB로 돌려(운영 DB를 건드리지 않음) 결과를 eval_runs에 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Optional

from backend.app.database import get_connection
from backend.app.paths import BACKEND_DIR, DATA_DIR, PROJECT_ROOT


logger = logging.getLogger(__name__)

MODEL_CARD_PATH = BACKEND_DIR / "eval" / "model_card.json"
REGRESSION_SCENARIOS = BACKEND_DIR / "eval" / "scenarios.json"
RUNS_DIR = DATA_DIR / "eval_runs"
EVAL_TIMEOUT_SEC = 1800
MAX_SCENARIO_TURNS = 40

# 사용자 확인 → 정답 (1 = 보이스피싱, 0 = 정상). unsure는 쓰지 않는다.
VERDICT_LABEL = {"confirmed_phishing": 1, "missed_phishing": 1, "false_positive": 0}
VERDICT_TEXT = {
    "confirmed_phishing": "실제 피싱",
    "missed_phishing": "놓친 피싱",
    "false_positive": "오경보",
    "unsure": "잘 모르겠음",
}
SUITES = ("regression", "feedback", "all")

_run_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def model_card() -> dict:
    try:
        return json.loads(MODEL_CARD_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def labeled_calls() -> list[dict]:
    """사용자 확인이 있는 통화(통화마다 가장 최근 확인)와 그때의 시스템 판정."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT f.log_id, f.verdict, f.source, f.created_at AS feedback_at,
                   c.risk_score, c.risk_level, c.phishing_type, c.name, c.created_at AS called_at,
                   (SELECT s.scam_type FROM conversation_states s WHERE s.log_id = c.id ORDER BY s.version DESC LIMIT 1) AS scam_type
            FROM call_feedback f
            JOIN call_logs c ON c.id = f.log_id
            WHERE f.id = (SELECT MAX(f2.id) FROM call_feedback f2 WHERE f2.log_id = f.log_id)
            ORDER BY f.id DESC
            """
        ).fetchall()
    calls = []
    for row in rows:
        label = VERDICT_LABEL.get(row["verdict"])
        predicted = 1 if row["risk_level"] in ("medium", "high") else 0
        if label is None:
            outcome = "unsure"
        elif label == 1:
            outcome = "hit" if predicted else "missed"
        else:
            outcome = "false_alarm" if predicted else "correct_normal"
        calls.append(
            {
                "log_id": row["log_id"],
                "verdict": row["verdict"],
                "verdict_label": VERDICT_TEXT.get(row["verdict"], row["verdict"]),
                "source": row["source"],
                "feedback_at": row["feedback_at"],
                "called_at": row["called_at"],
                "risk_score": round(row["risk_score"] or 0.0, 4),
                "risk_level": row["risk_level"],
                "phishing_type": row["phishing_type"] or "",
                "scam_type": row["scam_type"] or "none",
                "label": label,
                "predicted": predicted,
                "outcome": outcome,
            }
        )
    return calls


def _turn_texts(log_id: int) -> list[str]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT content FROM call_messages WHERE log_id = ? ORDER BY turn_index ASC, id ASC", (log_id,)
        ).fetchall()
    return [str(row["content"]).strip() for row in rows if str(row["content"]).strip()]


def summary() -> dict:
    calls = labeled_calls()
    counts = {key: sum(1 for call in calls if call["outcome"] == key) for key in ("hit", "missed", "false_alarm", "correct_normal", "unsure")}
    judged = counts["hit"] + counts["missed"] + counts["false_alarm"] + counts["correct_normal"]
    return {
        "model": model_card(),
        "feedback": {
            "total": len(calls),
            "counts": counts,
            "agreement": round((counts["hit"] + counts["correct_normal"]) / judged, 4) if judged else None,
            "recent": calls[:12],
        },
        "retrain_candidates": sum(1 for call in calls if call["label"] is not None),
        "eval_scenarios_from_feedback": sum(1 for call in calls if call["outcome"] in ("missed", "false_alarm")),
        "runs": list_runs(limit=6),
    }


def retrain_candidates() -> dict:
    """사용자 확인이 있는 통화를 학습 데이터 형식으로 내보낸다. 오경보·놓친 사기가 가장 값진 재료다."""
    cases = []
    for call in labeled_calls():
        if call["label"] is None:
            continue
        texts = _turn_texts(call["log_id"])
        if len(texts) < 2:
            continue
        kind = "phishing" if call["label"] else "normal"
        cases.append(
            {
                "id": f"{kind}_feedback_{call['log_id']:06d}",
                "label": call["label"],
                "group": f"feedback_{call['log_id']}",
                "source": "user_feedback",
                "feedback": call["verdict"],
                "system_outcome": call["outcome"],
                "turns": [{"turn_index": index, "text": text} for index, text in enumerate(texts, start=1)],
            }
        )
    return {"exported_at": _now(), "cases": cases}


def feedback_scenarios() -> list[dict]:
    """오경보·놓친 사기로 확인된 통화를 회귀 평가 대본으로 만든다."""
    scenarios = []
    for call in labeled_calls():
        if call["outcome"] not in ("missed", "false_alarm"):
            continue
        texts = _turn_texts(call["log_id"])[:MAX_SCENARIO_TURNS]
        if len(texts) < 2:
            continue
        is_scam = call["label"] == 1
        scenarios.append(
            {
                "id": f"feedback_{'missed' if is_scam else 'false_alarm'}_{call['log_id']}",
                "label": 1 if is_scam else 0,
                "scam_type": (call["scam_type"] if call["scam_type"] != "none" else "other") if is_scam else "none",
                "min_stage": "approach" if is_scam else "none",
                "turns": texts,
            }
        )
    return scenarios


# ---------------------------------------------------------------- 평가 실행


def _row_to_run(row) -> dict:
    metrics = json.loads(row["metrics"] or "{}")
    return {"id": row["id"], "target": row["target"], "passed": bool(row["passed"]), "created_at": row["created_at"], **metrics}


def list_runs(limit: int = 10) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_run(row) for row in rows]


def get_run(run_id: int) -> Optional[dict]:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def _update_run(run_id: int, metrics: dict, passed: bool = False) -> None:
    with get_connection() as connection:
        connection.execute(
            "UPDATE eval_runs SET metrics = ?, passed = ? WHERE id = ?",
            (json.dumps(metrics, ensure_ascii=False), 1 if passed else 0, run_id),
        )


def running_run() -> Optional[dict]:
    for run in list_runs(limit=5):
        if run.get("status") == "running":
            return run
    return None


def start_run(suite: str = "regression", trigger: str = "manual") -> dict:
    """회귀 평가를 백그라운드로 시작한다. 이미 도는 평가가 있으면 그것을 돌려준다."""
    if suite not in SUITES:
        raise ValueError(f"suite는 {', '.join(SUITES)} 중 하나여야 합니다.")
    with _run_lock:
        current = running_run()
        if current:
            return current
        scenarios = []
        if suite in ("regression", "all"):
            scenarios.extend(json.loads(REGRESSION_SCENARIOS.read_text(encoding="utf-8"))["scenarios"])
        if suite in ("feedback", "all"):
            scenarios.extend(feedback_scenarios())
        if not scenarios:
            raise ValueError("평가할 대본이 없습니다(사용자 확인으로 생긴 대본이 아직 없음).")
        metrics = {"status": "running", "suite": suite, "trigger": trigger, "scenarios": len(scenarios), "started_at": _now()}
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO eval_runs(target, metrics, passed) VALUES (?, ?, 0)",
                (suite, json.dumps(metrics, ensure_ascii=False)),
            )
            run_id = cursor.lastrowid
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    scenario_path = RUNS_DIR / f"run_{run_id}_scenarios.json"
    scenario_path.write_text(json.dumps({"scenarios": scenarios}, ensure_ascii=False), encoding="utf-8")
    threading.Thread(target=_execute, args=(run_id, metrics, scenario_path), daemon=True).start()
    return get_run(run_id)


def _execute(run_id: int, metrics: dict, scenario_path) -> None:
    out_path = RUNS_DIR / f"run_{run_id}.json"
    env = {
        **os.environ,
        "SCENARIOS": str(scenario_path),
        "EVAL_DB": str(RUNS_DIR / f"run_{run_id}.db"),
        "EVAL_OUT": str(out_path),
        "N8N_WEBHOOK_URL": "",  # 평가 중에는 보호자 알림을 보내지 않는다
        "PYTHONUTF8": "1",
    }
    try:
        subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "backend.app.eval_loop"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, timeout=EVAL_TIMEOUT_SEC, check=False,
        )
        rows = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as exc:  # 평가 실패는 기록만 남긴다
        logger.warning("평가 실행 실패(run %s): %s", run_id, exc)
        _update_run(run_id, {**metrics, "status": "failed", "finished_at": _now(), "error": str(exc)[:300]})
        return
    tp = sum(1 for r in rows if r["label"] == 1 and r["predicted"] == 1)
    fp = sum(1 for r in rows if r["label"] == 0 and r["predicted"] == 1)
    fn = sum(1 for r in rows if r["label"] == 1 and r["predicted"] == 0)
    tn = sum(1 for r in rows if r["label"] == 0 and r["predicted"] == 0)
    passed = sum(1 for r in rows if all(r["checks"].values()))
    warn_turns = [r["warn_turn"] for r in rows if r["label"] == 1 and r.get("warn_turn")]
    result = {
        **metrics,
        "status": "done",
        "finished_at": _now(),
        "passed_count": passed,
        "total": len(rows),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "false_alarm_rate": round(fp / (fp + tn), 4) if fp + tn else None,
        "avg_first_warning_turn": round(sum(warn_turns) / len(warn_turns), 2) if warn_turns else None,
        "failed": [r["id"] for r in rows if not all(r["checks"].values())][:12],
    }
    _update_run(run_id, result, passed=passed == len(rows))
