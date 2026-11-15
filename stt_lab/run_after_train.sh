#!/bin/bash
# large-v3 학습이 끝나면: 체크포인트 고르기 → 합치기·변환(GPU float16 + CPU int8) → 같은 시험 녹음으로 평가 → 비교표
cd ~/voiceguard_ft
export PYTHONPATH=pylib
PY=~/voiceguard/.venv/bin/python
RUN=runs/lora_r32_v1
pick() { if [ -f "$1/best.json" ]; then $PY -c "import json; print(json.load(open('$1/best.json'))['step'])"; else ls -d $1/step* | sed 's/.*step//' | sort -n | tail -1; fi; }
until grep -q '"event": "done"' $RUN/train_log.jsonl 2>/dev/null; do sleep 60; done
echo "large-v3 training finished $(date -u +%H:%M)"; grep '"event": "done"' $RUN/train_log.jsonl
STEP=$(pick $RUN); echo "checkpoint: step$STEP"
$PY -X utf8 scripts/merge_convert.py $RUN/step$STEP models_ft/v1 > logs_merge_v1.txt 2>&1 || { tail -5 logs_merge_v1.txt; exit 1; }
$PY -X utf8 scripts/eval_asr.py models_ft/v1/ct2 runs/eval_ft_full.json 25 full > logs_eval_ft_full.txt 2>&1
$PY -X utf8 scripts/eval_asr.py models_ft/v1/ct2 runs/eval_ft_live.json 10 live > logs_eval_ft_live.txt 2>&1
echo "large-v3 ft eval done $(date -u +%H:%M)"
$PY -X utf8 scripts/compare.py
