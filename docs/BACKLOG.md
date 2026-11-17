# 추후 구현 사항

코드 점검(2026-11-15)에서 나왔지만 아직 고치지 않은 것들이다. 고치면 이 목록에서 지운다.

## 0. 운영에 남은 일 (사람이 해야 함)

- n8n에 `n8n/voiceguard_knowledge_collect.json`을 가져와 `base_url`과 관리 코드(서버에 `ADMIN_CODE`가 없으면 접속 코드)를 넣는다. 그 전까지 신종 수법 수집은 대시보드의 '지금 수집' 버튼으로만 돈다.
- 수사관 자연어 질의 채팅은 아직 연결하지 않았다. 쓰려면 n8n에 `n8n/voiceguard_investigator_chat.json`을 가져와 '설정' 노드에 `base_url`을 넣고, 'VoiceGuard에 질문' 노드에 Header Auth 자격 증명(Name `X-Admin-Code`, Value 관리 코드)을 만들어 고른 뒤 켠다. 직접 설치한 n8n이면 먼저 버전을 확인한다(1.x는 1.123.22 이상, 2.x는 2.9.3 이상이고 2.10.0 제외). 이어 묻기 기억은 서버 메모리에 있어 백엔드가 uvicorn 프로세스 하나로 돌아야 한다. 연결하면 `docs/investigator.md`·`docs/API_SPEC_V2.md`·README의 '템플릿만 있음' 문구와 참가자 개인정보 안내(`frontend/src/core/config/privacy.ts`)를 고친다.
- 서버에 검토 대기 수법 카드 8장(2026-11-15 첫 수집). 승인하면 실시간 상태 추적기 프롬프트가 바뀌고 회귀 평가가 돈다(Claude API 비용).
- 본문이 이미지뿐인 게시글(경찰청 카드뉴스 등)은 카드로 만들지 않고 주소만 기록한다(`status=skipped`). 이미지 판독은 아직 없다.

## 2. 그 밖에 점검에서 나온 것

- 결과 피드백을 다시 보내면 중복 저장된다(보낸 상태를 화면이 기억하지 않음).
- 서버 로그(`backend/app/api/response_logger.py`)에 전사문 일부가 남는다. 공개 시연 뒤에는 로그를 지우거나 줄인다.
- SQLite 외래 키(`PRAGMA foreign_keys`)를 켜지 않아 `ON DELETE CASCADE`가 동작하지 않는다.
- 기본 설정(모두 로컬)에서는 화자 구분이 없다(실시간 조각은 모두 한 화자, 통화 뒤 역할은 상태 추적기가 내용으로 정한다). 리턴제로를 켜면(시연 서버는 켜 둠) 녹음 분석·통화 뒤 재전사와 통화 중 다시 받아 적기가 화자를 나눈다.
- 쓰지 않는 모듈: `realtime_detector.py`, `llm_judge.py`, `predict.py`, `train_baseline.py`, 루트 `realtime_stt.py`, `convert_nia_dataset.py`.
- `docs/ERD.md`는 v1 표 6개만 그린다.
- 자동 테스트가 없다(`eval_*` 평가 스크립트만 있음).
