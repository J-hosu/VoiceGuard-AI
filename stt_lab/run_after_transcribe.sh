#!/bin/bash
# 받아 적기가 끝나면 자동으로: 대본 맞추기 → 학습 데이터 → LoRA 학습 시작 + 원래 모델 기준 성능(같은 시험 목록)
cd ~/voiceguard_ft
export PYTHONPATH=pylib
PY=~/voiceguard/.venv/bin/python
while pgrep -f "[s]cripts/transcribe" > /dev/null; do sleep 30; done
echo "transcription done $(date -u +%H:%M)"
$PY -X utf8 scripts/match_align.py 16 > logs_align_all.txt 2>&1
$PY -X utf8 scripts/build_dataset.py > logs_build.txt 2>&1
cat logs_build.txt
OUT=runs/lora_r32_v1 EPOCHS=1 BATCH=16 ACCUM=2 LR=3e-4 EVAL_EVERY=300 MEM_FRAC=0.45 WORKERS=12 \
  setsid nohup $PY -X utf8 scripts/train_lora.py > logs_train_v1.txt 2>&1 < /dev/null &
echo "training started $(date -u +%H:%M)"
$PY -X utf8 scripts/eval_asr.py large-v3 runs/eval_base_full.json 25 full > logs_eval_base_full.txt 2>&1
$PY -X utf8 scripts/eval_asr.py large-v3 runs/eval_base_live.json 10 live > logs_eval_base_live.txt 2>&1
echo "base eval done $(date -u +%H:%M)"
tail -12 logs_eval_base_full.txt; tail -12 logs_eval_base_live.txt
