#!/bin/bash
# 노트북 서비스 설정(PROFILE=cpu: int8, beam 1, 실시간 조각 temperature 2단계·출력 128토큰)으로
# small·turbo(원래·추가 학습)의 CER과 전사 시간을 GPU 평가와 같은 시험 목록(파일 100개, 실시간 40개)에서 잰다.
# 실시간 모델 선택이 먼저라 live부터 돈다. turbo 추가 학습본은 변환이 끝나면 이어서 잰다.
cd ~/voiceguard_ft
export PYTHONPATH=pylib PROFILE=cpu CPU_THREADS=${CPU_THREADS:-6}
PY=~/voiceguard/.venv/bin/python
mkdir -p runs/cpu
run() { # 이름 모델 모드
  N=$([ $3 = full ] && echo 25 || echo 10)
  [ -f runs/cpu/eval_$1_$3.json ] && return
  $PY -X utf8 scripts/eval_asr.py $2 runs/cpu/eval_$1_$3.json $N $3 > logs_cpu_eval_$1_$3.txt 2>&1 || tail -3 logs_cpu_eval_$1_$3.txt
  echo "$1 $3 $(date -u +%H:%M) $(grep -o '"[a-z]*_cer": [0-9.]*' logs_cpu_eval_$1_$3.txt | head -1) $(grep -o '"\(full_rtf\|live_chunk_p90_s\)": [0-9.]*' logs_cpu_eval_$1_$3.txt | tr '\n' ' ')"
}
for M in live full; do
  run small_ft models_ft/small_v1/ct2_int8 $M
  run small_base small $M
  run turbo_base large-v3-turbo $M
done
until grep -q "^saved.*turbo_v1" logs_merge_turbo.txt 2>/dev/null; do sleep 120; done
for M in live full; do run turbo_ft models_ft/turbo_v1/ct2_int8 $M; done
$PY -X utf8 scripts/compare.py > logs_compare_cpu.txt 2>&1
echo CPU_EVAL_DONE
