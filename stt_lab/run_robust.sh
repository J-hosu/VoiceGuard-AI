#!/bin/bash
# 학습한 전사 모델을 서비스에 써도 되는지 점검(학습이 끝나는 대로 모델마다):
#  1) 말 없는 구간에서 글을 지어내는지 (robust_nonspeech.py)
#  2) 실제 정상 상담 녹음 33개에서 통화 분류기 오경보가 늘지 않는지 (backend eval_audio, LABEL=0)
#  3) 금감원 공개 사기 녹취 24개에서 탐지가 유지되는지 (backend eval_audio, LABEL=1)
cd ~/voiceguard_ft
export PYTHONPATH=pylib
PY=~/voiceguard/.venv/bin/python
mkdir -p runs/robust fss_subset
# 금감원 녹취 24개를 이름 해시 순서로 골라 링크해 둔다(원본은 그대로)
$PY -X utf8 -c "
import hashlib, os, glob
files = sorted(glob.glob(os.path.expanduser('~/dataset/**/*.mp3'), recursive=True), key=lambda p: hashlib.md5(p.encode()).hexdigest())[:24]
for i, f in enumerate(files):
    dst = os.path.join('fss_subset', f'scam_{i:02d}.mp3')
    if not os.path.exists(dst):
        os.symlink(f, dst)
print(len(files), 'scam recordings linked')
"
check() {  # $1 이름, $2 모델(이름 또는 CT2 폴더)
  echo "== $1 ($2) $(date -u +%H:%M)"
  $PY -X utf8 scripts/robust_nonspeech.py "$2" runs/robust/nonspeech_$1.json > logs_robust_ns_$1.txt 2>&1
  (cd ~/voiceguard && WHISPER_MODEL="$2" AUDIO_DIR=data/aihub/audio_valid TRANSCRIPTS=$HOME/voiceguard_ft/runs/robust/normal_$1.json LABEL=0 \
     $PY -X utf8 -m backend.app.eval_audio > $HOME/voiceguard_ft/logs_robust_normal_$1.txt 2>&1)
  (cd ~/voiceguard && WHISPER_MODEL="$2" AUDIO_DIR=$HOME/voiceguard_ft/fss_subset TRANSCRIPTS=$HOME/voiceguard_ft/runs/robust/scam_$1.json LABEL=1 \
     $PY -X utf8 -m backend.app.eval_audio > $HOME/voiceguard_ft/logs_robust_scam_$1.txt 2>&1)
  grep -h "통화 중 한 번이라도" logs_robust_normal_$1.txt logs_robust_scam_$1.txt
}
check large_base large-v3
check small_ft $HOME/voiceguard_ft/models_ft/small_v1/ct2
$PY -X utf8 scripts/robust_nonspeech.py small runs/robust/nonspeech_small_base.json > logs_robust_ns_small_base.txt 2>&1
until [ -f models_ft/v1/ct2/model.bin ]; do sleep 60; done
check large_ft $HOME/voiceguard_ft/models_ft/v1/ct2
until [ -f models_ft/turbo_v1/ct2/model.bin ]; do sleep 60; done
check turbo_ft $HOME/voiceguard_ft/models_ft/turbo_v1/ct2
$PY -X utf8 scripts/robust_nonspeech.py large-v3-turbo runs/robust/nonspeech_turbo_base.json > logs_robust_ns_turbo_base.txt 2>&1
echo ROBUST_DONE $(date -u +%H:%M)
