# RunPod로 시연 서버 옮기기

H100에서 돌던 시연 백엔드를 RunPod GPU pod 한 대로 옮기는 순서다. 2026-11-16 현재 공개 주소는 H100에 연결되어 있고, 옮기는 날 이 문서대로 한 번에 진행한다. 키·접속 코드·터널 자격 증명 같은 비밀 값은 이 문서와 저장소에 없고 이름만 적었다.

- **지금**: 휴대폰 → Cloudflare 터널(`<터널 도메인>`) → 노트북 cloudflared → SSH 터널 → H100 uvicorn(`127.0.0.1:8000`)
- **옮긴 뒤**: 휴대폰 → Cloudflare 터널(같은 `<터널 도메인>`) → pod 안 cloudflared → pod uvicorn(`127.0.0.1:8000`). 노트북은 경로에서 빠진다. 주소·QR·접속 코드는 그대로다.
- **모델은 pod에서 직접 돌린다**: 실시간 전사 Whisper large-v3(GPU), 통화 분류기 v5와 문자 분류기(둘 다 KLUE-RoBERTa). 밖으로 나가는 것은 Claude API(전사문, 문자·문서 점검에 올린 글과 사진), 리턴제로(통화 중 정리본과 통화 후 화자 분리에 쓰는 음성), 설정했다면 n8n 알림뿐이다. 전사 설정은 H100과 같다(`STT_LIVE_ENGINE=local`, `STT_FILE_ENGINE=rtzr`, `STT_REFINE=rtzr`).
- H100은 공용 GPU라 다른 작업과 나눠 쓰면 느려졌다(조각당 혼자 0.13초, 학습과 나눠 쓸 때 0.73초). pod의 GPU는 혼자 쓴다.

RunPod 가격은 2026-11-12 가격표, 재고와 동작은 11-16 조사 기준이다. 빌리기 전에 다시 확인한다.

## 1. 노트북에서 준비

1. **RunPod 계정**: Settings → SSH Public Keys에 노트북 공개 키를 넣는다. 크레딧을 넣고 자동 충전을 켠다(10절). 국내 카드 결제는 확인하지 않았으니 미리 소액으로 해 본다.
2. **코드**: 시연에 쓸 커밋을 push하고 해시를 적어 둔다.
3. **웹 빌드**: 같은 커밋에서 만든다. 셸에 넣은 값이 `frontend/.env`보다 우선한다.
   ```powershell
   cd frontend
   $env:EXPO_PUBLIC_API_BASE = 'same-origin'; npm run build:web; Remove-Item Env:EXPO_PUBLIC_API_BASE
   Select-String -Path dist\_expo\static\js\web\*.js -Pattern '127.0.0.1:8000' -SimpleMatch   # 아무것도 안 나와야 한다
   cd ..
   ```
4. **DB**: H100이 있으면 그날 서버 DB를 받아 온다. 돌고 있는 DB 파일을 그대로 복사하지 말고 SQLite backup으로 뜬다.
   ```bash
   # H100
   cd ~/voiceguard && .venv/bin/python -c "import sqlite3; d=sqlite3.connect('../voiceguard_tools/vg_move.db'); sqlite3.connect('data/voice_phishing.db').backup(d); d.close()"
   ```
   ```powershell
   # 노트북(저장소 루트)
   scp -P <H100 포트> -i <H100 키> <계정>@<H100 주소>:voiceguard_tools/vg_move.db data\h100_backup\meta_full\voice_phishing_move.db
   ```
   H100을 이미 반납했으면 `data\h100_backup\meta_full\voice_phishing_backup_1116_1135.db`(11-16 11:34)를 `voice_phishing_move.db`로 복사해 쓴다. 노트북의 `data\voice_phishing.db`는 학습 사례가 0건이라 쓰면 유사 사례 검색이 빈다.
5. **.env**: 저장소 밖에 둔 서버 .env 백업을 준비한다(5절에서 고친다).
6. **리턴제로**: 콘솔에서 남은 사용 시간을 본다(10절).

## 2. pod 고르고 만들기

| 항목 | 고를 것 | 이유 |
| --- | --- | --- |
| 클라우드 | **Secure Cloud** | Community는 개인 호스트라 안정성이 '가변'이고 가동 보장과 네트워크 볼륨이 없다. 스팟(중단 가능) pod는 2026-05에 문서에서 빠졌고, 회수되면 시연이 끊긴다 |
| GPU | **RTX 4090 1장** (24GB, 6 vCPU, $0.89/시간) | large-v3 fp16(최대 3.3~4.4GB)과 분류기 둘이 넉넉히 들어간다. 11-16 제3자 조회에서 Secure 재고가 있었다(더 싼 A5000·A4000·3090은 없었다). 없으면 A40 $0.59 · L4 $0.69 · RTX A6000 $0.69 · L40S $1.09(가장 빠름). RTX 5090·RTX PRO 6000 같은 Blackwell은 피한다(CUDA 12.8 이상 필요, CTranslate2 오류 보고) |
| 지역 | 미국 서부(US-CA·US-WA) 중 재고 있는 곳 | 한국·대만·싱가포르 데이터센터는 없다. 서울에서 약 130~140ms라 1.2~10초 조각에는 문제없다. 일본(AP-JP-1)은 H200($5.29/시간)으로 열었고 더 싼 GPU는 확인되지 않았다 |
| CUDA 필터 | 13.0 이상, 없으면 12.6 이상 | 13.0 이상이면 H100 패키지(torch cu130)를 그대로 쓴다(4절 A) |
| 템플릿 | RunPod PyTorch(가능하면 py3.11) | SSH와 python이 있다. torch는 venv에 따로 깐다 |
| 디스크 | 컨테이너 20GB, 볼륨 40GB(`/workspace`) | 컨테이너 디스크는 Stop하면 지워진다. 코드·venv·모델·DB·로그는 모두 `/workspace`에 둔다 |
| 포트 | TCP 22 | SSH·scp용. 공개는 터널로만 하고 HTTP 포트는 쓰지 않는다 |

네트워크 볼륨($0.07/GB·월)을 쓰면 pod가 지워지거나 잔액이 0이 돼도 데이터가 남고, 같은 데이터센터에 새 pod를 붙일 수 있다. 대신 Secure 전용이고 한 데이터센터에 묶이며 pod를 만들 때만 붙일 수 있다. 이 앱의 SQLite를 네트워크 저장소에서 돌려 본 적은 없으니, 쓴다면 8절 점검을 그 위에서 한다.

만든 뒤 Connect → SSH over exposed TCP에 나온 IP·포트로 들어가 확인한다. 포트는 pod가 리셋되면 바뀐다.

```bash
nvidia-smi | head -12      # GPU 이름, Driver Version, CUDA Version
python3 --version          # 3.11이 아니면 4절에서 H100 목록이 다 깔리는지 본다. 안 되면 py3.11 템플릿으로 다시 만든다
df -h /workspace           # 40GB
TZ=Asia/Seoul date         # KST가 나와야 한다. 다른 이름이면(시간대 자료 없음) 6절 vg.sh의 TZ를 KST-9로 바꾼다
```

## 3. 파일 올리기

코드를 먼저 받는다(pod). 브랜치로 받아야 나중에 `git pull`로 고칠 수 있다.

```bash
git clone -b <브랜치> <저장소 주소> /workspace/voiceguard && git -C /workspace/voiceguard log -1 --oneline   # 1절의 해시와 같은지 본다
```

나머지는 노트북에서 올린다(PowerShell, 저장소 루트). 저장소가 비공개라 clone이 안 되면 첫 줄 아래 주석 세 줄의 `#`을 지우고 실행해 코드를 올린다. 이때는 코드를 고칠 때도 같은 방법으로 다시 올린다.

```powershell
$P = <SSH 포트>; $K = "$HOME\.ssh\<키 파일>"; $R = "root@<pod IP>"
# git archive --format=tar -o $env:TEMP\vg.tar <커밋>
# scp -P $P -i $K $env:TEMP\vg.tar "${R}:/workspace/"
# ssh -p $P -i $K $R "mkdir -p /workspace/voiceguard && tar -xf /workspace/vg.tar -C /workspace/voiceguard"
ssh -p $P -i $K $R "mkdir -p /workspace/voiceguard/models /workspace/voiceguard/data/official /workspace/voiceguard/data/generated /workspace/cloudflared /workspace/bin /workspace/logs"
scp -P $P -i $K -r models\koelectra models\sms_roberta "${R}:/workspace/voiceguard/models/"
scp -P $P -i $K data\official\cases.json "${R}:/workspace/voiceguard/data/official/"
scp -P $P -i $K data\h100_backup\server_data\official_scam.json "${R}:/workspace/voiceguard/data/generated/"
scp -P $P -i $K data\h100_backup\meta_full\voice_phishing_move.db "${R}:/workspace/voiceguard/data/voice_phishing.db"
scp -P $P -i $K -r frontend\dist "${R}:/workspace/voiceguard/web_dist"
scp -P $P -i $K data\h100_backup\meta_full\server_env_1116.txt "${R}:/workspace/"
scp -P $P -i $K "$HOME\.cloudflared\<터널ID>.json" "${R}:/workspace/cloudflared/"
scp -P $P -i $K "<서버 .env 백업>" "${R}:/workspace/voiceguard/.env"
```

| 올리는 것 | 쓰는 곳 |
| --- | --- |
| `models/koelectra` | 통화 분류기 v5(기본 경로) |
| `models/sms_roberta` | 문자 분류기(경로 고정) |
| `data/voice_phishing.db` | 학습 사례(유사 사례 검색), 신종 수법 카드, 통화 기록 |
| `data/official/cases.json`, `data/generated/official_scam.json` | 유사 공식 사례 |
| `web_dist` | 웹 앱(백엔드가 같은 주소로 제공) |
| `server_env_1116.txt` | H100 패키지 목록(4절) |
| `<터널ID>.json`, `.env` | 터널 자격 증명, 키·코드 |

올리지 않는 것: `models/whisper_ft`(안 씀), `models/diar`(연결 안 됨), `models/roberta_*`·`models/koelectra_old_*`(예전 모델), `data/h100_backup/web_dist`(예전 빌드), 학습 데이터 JSON.

pod에서 확인한다.

```bash
cd /workspace/voiceguard
sha256sum models/koelectra/model.safetensors    # 752b3084…로 시작하면 v5
python3 -c "import sqlite3; print(sqlite3.connect('data/voice_phishing.db').execute('select count(*) from training_cases').fetchone()[0])"   # 11-16 점검 때 1777. 0이면 잘못된 DB
ls web_dist/index.html data/official/cases.json data/generated/official_scam.json
chmod 600 .env /workspace/cloudflared/*.json
```

웹 빌드를 다시 올릴 때는 먼저 `rm -rf /workspace/voiceguard/web_dist`를 한다(안 하면 `web_dist/dist`로 들어간다).

## 4. 패키지 설치와 GPU 확인 (pod)

H100에서 11-16에 뽑은 패키지 목록(3절에서 올린 `server_env_1116.txt`: Python 3.11.15, torch 2.13.0+cu130, ctranslate2 4.8.5, faster-whisper 1.2.1, transformers 4.57.6, anthropic 2.6.0, websockets 17.4)을 그대로 깐다. `requirements.txt`는 버전이 열려 있어 새로 깔면 다른 조합(예: 11-10에 나온 ctranslate2 4.9.0)이 들어온다.

```bash
python3 -m venv /workspace/venv      # ensurepip 오류면 apt-get update && apt-get install -y python3.11-venv(버전에 맞게)
sed -n '/^# pip-style freeze/,$p' /workspace/server_env_1116.txt | grep -vE '^(#|pip==|setuptools==|wheel==)' > /workspace/h100_freeze.txt
```

**A.** `nvidia-smi`의 CUDA Version이 13.0 이상이면(H100은 드라이버 580, CUDA 13.0) 목록 그대로 깐다.

```bash
/workspace/venv/bin/python -m pip install -r /workspace/h100_freeze.txt
```

**B.** 12.x면 torch만 CUDA 12 빌드로 깔고 나머지는 같게 맞춘다.

```bash
/workspace/venv/bin/python -m pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cu126
grep -vE '^(torch|triton|nvidia-|cuda-)' /workspace/h100_freeze.txt > /workspace/freeze_cu12.txt
/workspace/venv/bin/python -m pip install -r /workspace/freeze_cu12.txt
```

GPU 확인. 이때 Whisper large-v3(약 3GB)를 `/workspace/hf`에 받아 둔다.

```bash
HF_HOME=/workspace/hf /workspace/venv/bin/python - <<'EOF'
import numpy as np, torch, ctranslate2
print("torch", torch.__version__, "GPU 연산", torch.ones(2, device="cuda").sum().item())
print("ctranslate2", ctranslate2.__version__, "GPU 수", ctranslate2.get_cuda_device_count())
from faster_whisper import WhisperModel
m = WhisperModel("large-v3", device="cuda", compute_type="float16")
list(m.transcribe(np.zeros(32000, dtype=np.float32), language="ko", beam_size=5)[0])
print("Whisper large-v3 GPU 전사 OK")
EOF
```

`GPU 연산 2.0`, `GPU 수 1`, 마지막 줄이 나오면 된다. `libcublas.so.12`나 `libcudnn…so.9`를 못 찾는다는 오류면 아래 두 줄을 실행하고 다시 시험한다. 그걸로 되면 같은 두 줄을 6절 `vg.sh`의 `LD_LIBRARY_PATH` 자리에 넣는다.

```bash
SP=$(/workspace/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
export LD_LIBRARY_PATH=$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib
```

## 5. .env 채우기 (pod)

3절에서 올린 `/workspace/voiceguard/.env`를 아래 표대로 고친다(`nano`나 `vi`). 값은 화면에 찍지 않는다. 이름만 볼 때는 `sed 's/=.*/=…/' .env`.

| 이름 | 값 | 메모 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 서버와 같게 | |
| `RTZR_CLIENT_ID`, `RTZR_CLIENT_SECRET` | 서버와 같게 | |
| `STT_LIVE_ENGINE` | `local` | 실시간 조각은 pod GPU의 Whisper |
| `STT_FILE_ENGINE` | `rtzr` | 통화 후 재전사·녹음 파일(화자 2명 분리) |
| `STT_REFINE` | `rtzr` | 통화 중 정리본 |
| `WHISPER_DEVICE` | `cuda` | `auto`면 CUDA 문제가 있을 때 오류 없이 CPU로 돌아 아주 느려진다 |
| `WHISPER_MODEL` · `WHISPER_COMPUTE` · `WHISPER_BEAM` | `large-v3` · `float16` · `5` | cuda 기본값과 같다. 노트북용 값이 남아 있으면 고친다 |
| `ACCESS_CODE` | 서버와 같게 | 바꾸면 나눠 준 QR과 참가자 쿠키가 모두 안 맞는다 |
| `ADMIN_CODE` | `ACCESS_CODE`와 다른 값 | 대시보드 등 관리 화면 코드. 비우면 시작 로그에 경고가 나오고 접속 코드 하나로 관리 화면까지 열린다. n8n 수집 워크플로의 `admin_code`도 같은 값 |
| `N8N_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET` | 서버와 같게(선택) | pod에서 닿는 주소여야 한다 |
| `PUBLIC_BASE_URL` | `https://<터널 도메인>` | 주소가 같으니 그대로 |
| `TRACKER_MODEL` 등 모델 이름 | 서버에 있던 그대로 | 없으면 코드 기본값 |
| `KOELECTRA_MODEL_DIR`, `WEB_DIST_DIR` | 넣지 않는다 | 기본값이 `models/koelectra`(v5)와 `web_dist`다. 서버 경로가 남아 있으면 분류기 없이 돌거나 웹 앱이 안 뜬다 |

## 6. 시작 스크립트와 터널 설정 (pod)

uvicorn은 한 프로세스로만 띄운다(`--workers` 금지). 모델, Whisper 잠금, 통화별 정리 상태가 프로세스 메모리에 있다. 시각은 `TZ=Asia/Seoul`로 맞춘다(DB가 SQLite localtime으로 시각을 적는다).

```bash
cat > /workspace/vg.sh <<'EOF'
#!/bin/bash
# VoiceGuard pod 운영. 사용: vg.sh backend | tunnel-on | tunnel-off | status
export TZ=Asia/Seoul HF_HOME=/workspace/hf HF_HUB_OFFLINE=1
# export LD_LIBRARY_PATH=...   # 4절 GPU 확인에서 라이브러리 오류가 났을 때만
L=/workspace/logs; mkdir -p $L
case "$1" in
  backend)   # 백엔드를 (다시) 띄운다. 진행 중인 통화는 끊긴다
    pkill -f "[w]orkspace/venv/bin/uvicorn"; sleep 3
    [ -f $L/uvicorn.log ] && mv $L/uvicorn.log $L/uvicorn.$(date +%m%d-%H%M%S).log
    cd /workspace/voiceguard
    setsid nohup /workspace/venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 > $L/uvicorn.log 2>&1 < /dev/null &
    for i in $(seq 1 90); do sleep 2; curl -fsS localhost:8000/health > /dev/null && break; done
    curl -fsS localhost:8000/health || { echo "health 실패"; tail -30 $L/uvicorn.log; exit 1; }
    echo; echo "--- 시작 경고(INFO가 아닌 줄) ---"; grep -v INFO $L/uvicorn.log | tail -20 ;;
  tunnel-on)   # 공개 주소를 이 pod로 받는다. 노트북 터널을 먼저 끈다
    pgrep -f "[c]loudflared tunnel" > /dev/null && { echo "이미 켜져 있다"; exit 0; }
    setsid nohup bash -c 'while true; do /workspace/bin/cloudflared tunnel --config /workspace/cloudflared/config.yml run; sleep 5; done' >> $L/cloudflared.log 2>&1 < /dev/null &
    echo "터널 켬" ;;
  tunnel-off)
    pkill -f "[c]loudflared tunnel"; sleep 6; pkill -f "[c]loudflared tunnel"; echo "터널 끔" ;;
  status)
    curl -fsS localhost:8000/health; echo
    pgrep -af "[u]vicorn backend|[c]loudflared tunnel"
    nvidia-smi --query-gpu=name,memory.used,utilization.gpu --format=csv; df -h /workspace ;;
  *) echo "사용: vg.sh backend | tunnel-on | tunnel-off | status" ;;
esac
EOF
chmod +x /workspace/vg.sh

curl -fL -o /workspace/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/download/2026.8.2/cloudflared-linux-amd64
chmod +x /workspace/bin/cloudflared
cat > /workspace/cloudflared/config.yml <<'EOF'
tunnel: <터널ID>
credentials-file: /workspace/cloudflared/<터널ID>.json
protocol: http2
ingress:
  - hostname: <터널 도메인>
    service: http://127.0.0.1:8000
  - service: http_status:404
EOF
```

터널은 노트북 `%USERPROFILE%\.cloudflared\voiceguard.yml`과 같은 것이다. 다른 점은 둘이다. `service`가 노트북의 18000(SSH 터널) 대신 8000이고, pod에서 UDP가 막혀도 연결되도록 `protocol: http2`를 넣었다. cloudflared 2026.8.2는 노트북과 같은 판이다. 계정 인증서(`cert.pem`)는 pod에 올리지 않는다. 터널 실행에는 자격 증명 JSON만 있으면 된다.

백엔드를 띄워 본다(터널은 7절에서 켠다).

```bash
/workspace/vg.sh backend    # {"status":"ok"} 아래에 경고 줄이 없어야 한다
/workspace/vg.sh status     # GPU 메모리를 몇 GB 쓰고 있어야 한다
```

자주 나오는 경고: `Whisper 모델 사전 로드 실패`(4절 확인), `KoELECTRA 모델 폴더가 없어`(`models/koelectra`와 `KOELECTRA_MODEL_DIR` 확인), `ADMIN_CODE` 경고(5절).

## 7. 공개 주소 넘기기

쓰는 사람이 없을 때 한다. 한 터널에 연결기가 둘(노트북·pod)이면 요청이 어느 쪽으로 갈지 알 수 없으니 노트북 쪽을 먼저 끈다.

1. 노트북: 공개 주소 스크립트(`start_public.ps1`)를 끄고, 남은 cloudflared를 끈다.
   ```powershell
   Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process
   ```
2. pod: `/workspace/vg.sh tunnel-on` 뒤 `grep "Registered tunnel connection" /workspace/logs/cloudflared.log | tail -2`로 연결을 확인한다.
3. 노트북: 연결기가 pod 하나뿐인지와 공개 주소를 본다.
   ```powershell
   cloudflared tunnel info <터널 이름>
   curl.exe -s https://<터널 도메인>/health    # {"status":"ok"}
   ```
4. QR과 접속 코드는 그대로 쓴다.

RunPod 프록시 주소(`https://<pod ID>-8000.proxy.runpod.net`)는 쓰지 않는다. 주소가 달라 QR을 새로 만들어야 하고, 첫 응답까지 100초 제한이 있고, WebSocket 지원이 문서에 없다.

**노트북 경로를 그대로 두는 방법**(pod에서 cloudflared가 안 될 때): pod 터널은 켜지 않고 노트북의 SSH 포워딩만 H100 대신 pod로 바꾼다(`start_public.ps1`의 SSH 대상을 바꾸거나 아래처럼 직접 띄운다). 노트북이 잠들거나 인터넷이 끊기면 전체가 멈추고, pod가 리셋되면 SSH 포트를 다시 넣어야 한다.

```powershell
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 18000:127.0.0.1:8000 -p $P -i $K $R
cloudflared tunnel --config $HOME\.cloudflared\voiceguard.yml run    # 다른 창에서
```

## 8. 예열과 점검

**예열**(백엔드를 띄울 때마다): `vg.sh backend` 뒤 1분쯤 기다린다. Claude 응답 형식을 미리 준비하는 호출이 뒤에서 돈다(20초 넘게 걸릴 수 있음). 그다음 공개 주소로 30초짜리 시험 통화를 한 번 한다. 첫 조각의 GPU 준비와 유사 사례 색인 만들기가 이때 끝나서 참가자의 첫 통화가 느려지지 않는다.

**점검**(옮긴 날 한 번씩):

1. **상태**: `vg.sh status`, 노트북에서 `curl.exe -s https://<터널 도메인>/health`.
2. **실제 통화**: 휴대폰을 모바일 데이터로 바꾸고 QR로 들어가 사기 대본 2~3분, 정상 대본 1통을 건다.
   - 실시간 전사가 몇 초 안에 뜨고, 사기 대본은 위험도가 오르고 정상 대본은 낮게 남는다.
   - 30초쯤부터 화자가 나뉜 정리본이 나오고 약 15초마다 바뀐다.
   - 끝내면 결과 화면의 전사가 두 화자로 나뉜다(통화 후 리턴제로 재전사).
   - 조각당 전사 시간(ms)을 본다: `grep -o '"stt": [0-9]*' /workspace/logs/uvicorn.log | tail -30`. H100을 혼자 쓸 때 약 130ms였다. 통화 한 통이 초당 약 0.4조각을 보내므로, GPU를 70% 안쪽으로 쓰려면 동시 5통은 약 350ms 이하, 10통은 약 180ms 이하여야 한다. 휴대폰 2~3대로 같이 걸어 보고 동시 참가 수를 정한다. 기준을 넘으면 참가 수를 줄이거나 L40S로 pod를 새로 만든다.
   - **동시 실시간 통화 수 (2026-11-17 실측 기반 추정, 부하 시험은 하지 않음)**: Whisper는 조각을 한 번에 하나씩 받아 적으므로, 동시 통화 수 ≈ 0.7 ÷ (0.4조각/초 × 조각당 전사 시간)이다.

     | 서버 | 조각당 전사 시간(같은 금감원 녹취) | 동시 실시간 통화 |
     | --- | --- | --- |
     | 학교 H100, 혼자 쓸 때 | 중앙값 약 0.4초 (깨끗한 상담 녹음은 0.13~0.2초) | 약 4통 (깨끗한 녹음이면 7~8통) |
     | 학교 H100, 학습 등 다른 작업과 나눠 쓸 때 | 약 0.7초 | 약 2통 |
     | RunPod H100 SXM (AP-JP-1) | 중앙값 약 0.7초, p90 약 0.9초 | 약 2~3통 |

     넘치면 오류가 나지는 않고 전사와 경고가 점점 늦어진다. 앱 화면을 보거나 녹음 파일·문자·문서를 검사하는 사람은 GPU를 거의 쓰지 않으므로 이 수에 들어가지 않는다. 더 받으려면 Whisper 모델을 2~3개 띄워 조각을 나눠 받아 적게 한다(H100 메모리는 충분하다, 아직 구현하지 않음). 통화 후 재전사나 녹음 파일을 서버 Whisper로 전사하는 동안(리턴제로 소진 뒤나 `STT_FILE_ENGINE=local`일 때)에는 다른 통화의 실시간 전사와 경고가 그 시간만큼 멈추므로, 동시 참가 수를 정할 때 함께 센다.
3. **10~15분 통화** 1통: 중간에 끊기지 않는지(Cloudflare 경유), 정리본이 끝까지 바뀌는지, 끝낸 뒤 두 화자 결과가 나오는지 본다. 통화 후 재전사는 요청 하나라서 Cloudflare 제한(약 100초) 안에 끝나야 한다. 넘으면 앱은 실시간 조각으로 결과를 만든다.
4. **리턴제로 끄기 연습**:
   ```bash
   cd /workspace/voiceguard
   sed -i 's/^STT_REFINE=.*/STT_REFINE=off/; s/^STT_FILE_ENGINE=.*/STT_FILE_ENGINE=local/' .env
   time /workspace/vg.sh backend    # 다시 띄우는 데 걸린 시간을 적어 둔다
   ```
   통화 1통을 건다. 실시간 전사와 경고는 그대로고, 정리본은 나오지 않고, 통화 후 재전사는 pod의 large-v3가 한다(화자는 추적기가 정한 역할로 나눈다). 확인한 뒤 되돌린다.
   ```bash
   sed -i 's/^STT_REFINE=.*/STT_REFINE=rtzr/; s/^STT_FILE_ENGINE=.*/STT_FILE_ENGINE=rtzr/' .env && /workspace/vg.sh backend
   ```
   리턴제로 무료 사용량이 소진되면(429 A0001) 서버가 로그에 '리턴제로 무료 사용량이 소진되어 이후 전사는 서버 Whisper로 처리하고 통화 중 정리는 끕니다'를 한 번 남기고 스스로 서버 Whisper로 바꾼다(백엔드를 다시 켜기 전까지 유지). 리턴제로 장애로 로그에 `리턴제로 … 실패`가 이어지거나, 다시 켠 뒤에도 끈 상태를 유지하려면(`grep 리턴제로 /workspace/logs/uvicorn.log | tail`) 이 off/local 상태로 돌린다. 통화 중 경고는 실시간 조각으로 정하므로 달라지지 않는다.
5. **Restart 연습**(선택): RunPod 화면에서 Restart → 다시 SSH → `vg.sh backend` → `vg.sh tunnel-on` → 예열. 걸린 시간을 적어 둔다.

## 9. 운영 중 지킬 것

- **Stop 대신 Restart.** Stop은 GPU를 놓아 버려서 다시 Start할 때 GPU 0개로 뜰 수 있다. Restart는 같은 기계에서 GPU를 유지한다.
- **실행 중인 pod 설정(포트·디스크·환경 변수)을 고치지 않는다.** 고치면 pod가 초기화되고 `/workspace` 밖은 모두 지워진다.
- **다시 켠 뒤**에는 프로세스가 저절로 뜨지 않는다. `vg.sh backend` → `vg.sh tunnel-on` → 예열 통화. SSH 포트가 바뀌었을 수 있다.
- **고친 뒤**: `.env`나 코드(`git -C /workspace/voiceguard pull`)를 바꾸면 `vg.sh backend`. 화면을 바꿨으면 웹 빌드도 다시 올린다(1절 3번, 3절). 웹 파일만 바꿀 때는 백엔드를 다시 띄우지 않아도 된다.
- 백엔드나 터널을 다시 띄우면 진행 중이던 통화는 끊긴다. 새로 시작한다.
- 시연 전 DB 초기화는 실제로 쓸 pod의 `data/voice_phishing.db`에 한다.
- 로그(`/workspace/logs`)에는 전사문 일부가 남는다.

## 10. 비용과 크레딧

2026-11-12 가격표, Secure Cloud, GPU 1장 기준이다.

| GPU | 시간당 | 하루(24시간) |
| --- | --- | --- |
| RTX 4090 | $0.89 | 약 $21 |
| A40 | $0.59 | 약 $14 |
| L4 · RTX A6000 | $0.69 | 약 $17 |
| L40S | $1.09 | 약 $26 |

- 초 단위로 내고 데이터 전송료는 없다. 볼륨 40GB는 켜져 있을 때 월 약 $4다(GB당 $0.10, 멈춰 있으면 $0.20).
- **크레딧은 환불되지 않는다.** 쓸 날 수 × 하루 비용에 하루치 여유만 더해 넣는다. pod를 만들려면 최소 1시간치가 있어야 한다.
- **잔액이 0이 되면** 모든 pod가 멈추고, 네트워크 볼륨이 없는 pod는 지워져 되살릴 수 없다. 자동 충전을 켜 둔다.
- **Stop 뒤 Start는 GPU 없이 뜰 수 있다**(9절). 설정과 시연 사이가 며칠 비면 Stop하지 말고, pod를 지운 뒤 시연 전에 2~6절을 다시 하거나(1~2시간) 네트워크 볼륨에 올려 두고 같은 데이터센터에 새 pod만 붙인다.
- 마이그레이션(베타)은 새 pod ID·IP를 준다. 공개 주소는 터널이라 그대로지만 SSH 정보는 새로 본다.
- 리턴제로: 이 설정에서 통화 한 통이 통화 길이의 약 3.5배(정리본 약 2.5배, 통화 후 재전사 1배)를 쓴다. 남은 시간이 모자라면 8절의 off/local 상태로 돌린다.

## 11. H100으로 되돌리기 (H100이 남아 있는 동안)

1. pod: `/workspace/vg.sh tunnel-off`
2. H100: 백엔드가 떠 있는지 본다(`curl -s localhost:8000/health`, 꺼져 있으면 `~/voiceguard_tools/restart_backend.sh`).
3. 노트북: `start_public.ps1`을 다시 켠다(노트북 cloudflared → SSH → H100).
4. 노트북: `cloudflared tunnel info <터널 이름>`에 노트북 연결기 하나만 있는지, `curl.exe -s https://<터널 도메인>/health`가 되는지 본다.

두 서버의 DB는 따로다. 옮긴 뒤 생긴 기록은 H100에 없다. H100을 반납한 뒤에는 되돌릴 곳이 없으니 pod를 Restart하거나 새로 만들고(2~6절), 그동안은 시연 영상을 쓴다.

## 12. 끝난 뒤

- 남길 DB·로그를 노트북으로 받는다: `scp -P $P -i $K "${R}:/workspace/voiceguard/data/voice_phishing.db" .`
- pod를 Terminate하고 볼륨(네트워크 볼륨을 썼다면 그것도)을 지운다. 전사문, 키, 터널 자격 증명이 들어 있다.
