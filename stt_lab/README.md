# stt_lab — 전사 모델 추가 학습·평가

AI Hub 민원(콜센터) 음성으로 Whisper를 추가 학습하고, 원래 모델·리턴제로(RTZR)와 같은 시험 녹음으로 비교하는 스크립트입니다.
로컬 화자 분리(sherpa-onnx)도 같은 정답으로 평가합니다. 결과와 자세한 과정은 [docs/tech/transcription.md](../docs/tech/transcription.md)에 있습니다.

GPU 서버의 `~/voiceguard_ft/` 아래에서 돌렸습니다(`scripts/`에 이 폴더 내용을 둠). 음성·대본 데이터는 AI Hub 이용 조건 때문에 저장소에 넣지 않습니다.

| 순서 | 스크립트 | 하는 일 |
| --- | --- | --- |
| 0 | `make_manifest.py` | AI Hub zip 목록을 ASCII 이름으로 정리(서버 로케일이 EUC-KR) |
| 1 | `prepare.py` | zip 속 mp3·m4a를 16 kHz 모노 FLAC으로 변환, 녹음 목록 작성 |
| 2 | `transcribe.py` | large-v3로 받아 적어 단어 시각을 얻는다(대본 위치 맞추기용, 배치 추론) |
| 3 | `match_align.py` | 받아 적은 글과 가장 비슷한 대본 대화를 찾고, 대본 문장마다 음성 속 시각·일치율을 정한다. 가려진 말·숫자 표기 차이가 있는 문장은 뺀다 |
| 4 | `build_dataset.py` | 학습(문장 하나 / 이어진 문장 묶음)·개발·시험 세트. 시험 녹음과 같은 대본을 읽은 학습 녹음은 뺀다 |
| 5 | `train_lora.py` | LoRA(large-v3·turbo) 또는 전체 미세조정(small) |
| 6 | `merge_convert.py` | 합친 모델을 faster-whisper 형식(GPU float16, CPU int8)으로 변환 |
| 7 | `eval_asr.py`, `eval_rtzr.py` | 같은 시험 녹음으로 글자 오류율(CER): 파일 전체 / 앱과 같은 실시간 조각 |
| 8 | `compare.py` | 비교표, 쌍대 부트스트랩 95% 구간, 숫자 표기·녹음 세션 묶음별 결과 |
| — | `diar_simple.py`, `diar_eval.py`, `spk_check.py`, `diar_lda.py` | 로컬 화자 분리 평가(통화 후 / 실시간), 목소리 특징 점검, LDA 학습 시험 |
| — | `run_*.sh` | 위 단계를 이어 돌리는 서버 스크립트 |

리턴제로 평가는 `~/voiceguard_ft/rtzr.env`의 `RTZR_CLIENT_ID`, `RTZR_CLIENT_SECRET`을 읽습니다. 키는 저장소에 넣지 않습니다.
