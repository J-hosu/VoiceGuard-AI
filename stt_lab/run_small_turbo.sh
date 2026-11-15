#!/bin/bash
# H100 이후(노트북 CPU)를 위한 작은 모델: small(전체 미세조정, large-v3 학습과 동시에) → turbo(LoRA, large-v3 학습 뒤)
cd ~/voiceguard_ft
export PYTHONPATH=pylib
PY=~/voiceguard/.venv/bin/python
pick() { if [ -f "$1/best.json" ]; then $PY -c "import json; print(json.load(open('$1/best.json'))['step'])"; else ls -d $1/step* | sed 's/.*step//' | sort -n | tail -1; fi; }
BASE=openai/whisper-small FULL_FT=1 OUT=runs/small_full_v1 LR=1e-5 BATCH=32 ACCUM=1 EVAL_EVERY=400 MEM_FRAC=0.15 WORKERS=8 \
  $PY -X utf8 scripts/train_lora.py > logs_train_small.txt 2>&1
grep '"event": "done"' runs/small_full_v1/train_log.jsonl
STEP=$(pick runs/small_full_v1); echo "small checkpoint: step$STEP"
BASE=openai/whisper-small $PY -X utf8 scripts/merge_convert.py runs/small_full_v1/step$STEP models_ft/small_v1 > logs_merge_small.txt 2>&1 || tail -5 logs_merge_small.txt
for M in full live; do N=$([ $M = full ] && echo 25 || echo 10)
  $PY -X utf8 scripts/eval_asr.py small runs/eval_small_base_$M.json $N $M > logs_eval_small_base_$M.txt 2>&1
  $PY -X utf8 scripts/eval_asr.py models_ft/small_v1/ct2 runs/eval_small_ft_$M.json $N $M > logs_eval_small_ft_$M.txt 2>&1
  $PY -X utf8 scripts/eval_asr.py large-v3-turbo runs/eval_turbo_base_$M.json $N $M > logs_eval_turbo_base_$M.txt 2>&1
done
echo "small + stock turbo eval done $(date -u +%H:%M)"
until grep -q '"event": "done"' runs/lora_r32_v1/train_log.jsonl 2>/dev/null; do sleep 60; done
BASE=openai/whisper-large-v3-turbo OUT=runs/turbo_lora_v1 LR=3e-4 BATCH=16 ACCUM=2 EVAL_EVERY=400 MEM_FRAC=0.35 WORKERS=12 \
  $PY -X utf8 scripts/train_lora.py > logs_train_turbo.txt 2>&1
grep '"event": "done"' runs/turbo_lora_v1/train_log.jsonl
STEP=$(pick runs/turbo_lora_v1); echo "turbo checkpoint: step$STEP"
BASE=openai/whisper-large-v3-turbo $PY -X utf8 scripts/merge_convert.py runs/turbo_lora_v1/step$STEP models_ft/turbo_v1 > logs_merge_turbo.txt 2>&1 || tail -5 logs_merge_turbo.txt
$PY -X utf8 scripts/eval_asr.py models_ft/turbo_v1/ct2 runs/eval_turbo_ft_full.json 25 full > logs_eval_turbo_ft_full.txt 2>&1
$PY -X utf8 scripts/eval_asr.py models_ft/turbo_v1/ct2 runs/eval_turbo_ft_live.json 10 live > logs_eval_turbo_ft_live.txt 2>&1
echo "turbo done $(date -u +%H:%M)"
$PY -X utf8 scripts/compare.py
echo SMALL_TURBO_DONE
