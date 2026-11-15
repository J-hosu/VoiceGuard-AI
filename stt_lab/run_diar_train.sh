#!/bin/bash
# 화자 분리 학습(LDA)과 평가: 학습용 녹음 정렬 → LDA 학습 → 시험 녹음으로 실시간·통화 후 방식 비교
cd ~/voiceguard_ft
export PYTHONPATH=pylib:pylib_sherpa
PY=~/voiceguard/.venv/bin/python
E=diar_models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
P=diar_models/lda_3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.pkl
$PY -X utf8 scripts/match_align.py 8 > logs_align_partial2.txt 2>&1
$PY -X utf8 scripts/diar_lda.py $E 1500 128 8
show() { $PY -X utf8 -c "import sys,json; d=json.load(open(sys.argv[1])); s=d['summary']; print(sys.argv[2], 'tau', s['tau'], 'DER', s['DER'], 'turn_acc', s['turn_speaker_accuracy'], 'chunk_acc', s.get('chunk_accuracy'))" "$1" "$2"; }
for tau in 0.3 0.4 0.5 0.6; do
  PROJ=$P $PY -X utf8 scripts/diar_simple.py $E online runs/d_on_3d_lda_$tau.json 120 $tau 8 > /dev/null 2>&1
  show runs/d_on_3d_lda_$tau.json "online+LDA"
done
PROJ=$P CLUSTER=kmeans $PY -X utf8 scripts/diar_simple.py $E offline_chunks runs/d_offc_3d_lda.json 120 0.5 8 > /dev/null 2>&1
show runs/d_offc_3d_lda.json "offline_chunks+LDA"
echo DIAR_TRAIN_DONE
