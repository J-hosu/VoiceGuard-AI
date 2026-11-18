# stt_lab — 전사 모델 추가 학습·평가

AI Hub 민원(콜센터) 음성으로 Whisper를 추가 학습하고, 기본 사전학습 모델·리턴제로(RTZR)와 같은 시험 녹음으로 비교하는 스크립트입니다. 로컬 화자 분리(sherpa-onnx)도 같은 정답으로 평가합니다.

- **결과:** 추가 학습한 모델은 실제 통화의 전화번호를 지우는 문제가 있어 서비스에 쓰지 않습니다. 서비스의 기본 전사 모델은 기본 사전학습 large-v3 그대로입니다. 로컬 화자 분리는 실험만 했고 앱 흐름에는 연결하지 않았습니다. 수치와 자세한 이유는 [음성 인식·화자 분리 성능](../docs/tech/transcription.md)에 있습니다.
- **실행 위치:** GPU 서버의 `~/voiceguard_ft/`에서 실행했습니다. 이 폴더의 파일은 그 아래 `scripts/`에 두었습니다.
- **데이터:** 음성·대본 데이터는 AI Hub 이용 조건 때문에 저장소에 넣지 않습니다.
- **리턴제로 키:** 리턴제로 평가는 `~/voiceguard_ft/rtzr.env`의 `RTZR_CLIENT_ID`, `RTZR_CLIENT_SECRET`을 읽습니다. 키는 저장소에 넣지 않습니다.

## 스크립트

| 순서 | 스크립트 | 하는 일 |
| --- | --- | --- |
| 0 | `make_manifest.py` | AI Hub zip 목록을 ASCII 파일 이름으로 정리([데이터 준비와 운영 기록](#데이터-준비와-운영-기록)) |
| 1 | `prepare.py` | zip 속 mp3·m4a를 16 kHz 모노 FLAC으로 변환, 녹음 목록 작성 |
| 2 | `transcribe.py` | large-v3로 전사해 단어 시각 확보(음성과 정답 대본 정렬용, 배치 추론) |
| 3 | `match_align.py` | 전사 결과와 가장 비슷한 대본 대화를 찾고, 대본 문장마다 음성 속 시각·일치율 결정. 개인정보 마스킹 표기를 소리 내 읽었거나 숫자 표기가 다른 문장은 제외 |
| 4 | `build_dataset.py` | 학습(문장 하나 / 이어진 문장 묶음)·개발·시험 세트 구성. 시험 녹음과 같은 대본을 읽은 학습 녹음은 제외 |
| 5 | `train_lora.py` | LoRA(large-v3·turbo) 또는 전체 미세조정(small) |
| 6 | `merge_convert.py` | 합친 모델을 faster-whisper 형식(GPU float16, CPU int8)으로 변환 |
| 7 | `eval_asr.py`, `eval_rtzr.py` | 같은 시험 녹음으로 문자 오류율(CER) 측정: 파일 전체 / 앱과 같은 실시간 조각 |
| 8 | `compare.py` | 비교표, 쌍대 부트스트랩 95% 구간, 숫자 표기·녹음 세션 묶음별 결과 |
| — | `diar_simple.py`, `diar_eval.py`, `spk_check.py`, `diar_lda.py` | 로컬 화자 분리 평가(통화 후 / 실시간), 목소리 특징 점검, LDA 투영 학습 시험(채택하지 않음) |
| 9 | `robust_nonspeech.py`, `run_robust.sh` | 서비스 적용 적합성 평가: 무음·비음성 구간에서 텍스트를 지어내는지, 전사한 정상·사기 통화로 분류기 결과가 유지되는지 |
| 10 | `run_cpu_eval.sh` (`eval_asr.py` `PROFILE=cpu`) | GPU 없을 때의 서비스 설정(CPU int8, beam 1)으로 같은 시험 녹음의 CER 측정 |
| 11 | `cpu_latency_local.py` | 노트북 CPU에서 서비스 전사 경로(`mp3_json.transcribe_audio`)의 조각당·파일당 시간 측정 |
| — | `refine_sim.py` | 통화 중 전사 보정 및 화자 구분(최근 20초는 실시간 조각, 지난 구간은 화자 분리와 함께 재전사) 시뮬레이션: 리턴제로 / 로컬 |
| — | `fss_prepare.py` | 금감원 공개 사기 녹취를 16 kHz FLAC으로 바꿔 전사 목록 작성(분류기 v4 데이터용) |
| — | `diar_local_test.py` | 앱 코드로 옮긴 로컬 화자 분리 모듈(`backend/app/services/local_diarization.py`)을 노트북 CPU에서 시험(앱 흐름에는 연결하지 않음) |
| — | `smoke.py` | 학습 코드가 끝까지 도는지 확인하는 작은 데이터 구성(실제 학습에는 쓰지 않음) |
| — | `run_*.sh` | 위 단계를 이어 돌리는 서버 스크립트 |

## 데이터 준비와 운영 기록

- **zip 직접 디코딩:** [`prepare.py`](prepare.py)는 zip 속 음성을 디스크에 풀지 않고 바로 디코딩해 FLAC으로 바꿉니다.
- **파일 이름:** GPU 서버의 로케일이 EUC-KR이라, [`make_manifest.py`](make_manifest.py)가 파일 이름을 ASCII로 바꾸고 원래 이름은 목록에 남깁니다.
- **리턴제로 사용량:** 평가에 무료 제공량 600분 중 약 300분을 썼습니다.
- **외부 전송 승인:** 기본 설정은 전사를 자체 서버에서 처리하므로, 시험 음성을 리턴제로 API로 보내는 평가도 담당자 승인을 받은 뒤에 했습니다.
