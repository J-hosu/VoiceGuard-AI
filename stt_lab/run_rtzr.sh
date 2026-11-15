#!/bin/bash
cd ~/voiceguard_ft
export PYTHONPATH=pylib WORKERS=4
~/voiceguard/.venv/bin/python -X utf8 scripts/eval_rtzr.py batch runs/eval_rtzr_batch.json 25 > logs_rtzr_batch.txt 2>&1
~/voiceguard/.venv/bin/python -X utf8 scripts/eval_rtzr.py stream runs/eval_rtzr_stream.json 10 > logs_rtzr_stream.txt 2>&1
echo RTZR_DONE
