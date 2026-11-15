# README 그림 원본

[README](../../README.md)의 맨 위 배너, 핵심 숫자 띠, 다섯 단계 그림과 수사 지원 절의 확대 조각 두 장을 만드는 원본입니다. 구운 결과는 `docs/images/readme/`에 있고, README는 그 파일만 씁니다.

| 원본 | 결과 | 다시 만들 때 |
| --- | --- | --- |
| `hero.svg` | `hero.webp` | 실시간 분석 화면(`app_realtime_agent.png`)이나 대시보드(`web_priority.png`)를 다시 찍었을 때 |
| `numbers.svg` | `numbers.png` | 통화 분류기를 바꿔 99.3% · 0.7% · 0/32통이 바뀔 때. README의 그림 설명과 대체 문구도 함께 고친다 |
| `loop.svg` | `loop.png` | 다섯 단계 이름이 바뀔 때 |
| `zoom.py` | `zoom_clues.png`, `zoom_priority.png` | `web_case_detail.png`나 `web_priority.png`를 다시 찍었을 때. 잘리는 위치(`CROPS`)도 확인한다 |

## 굽기

GitHub는 `<img>` 속 SVG에서 웹 글꼴을 쓰지 못합니다. 그래서 `fonts.txt`의 글꼴(Noto Sans KR, Inter)을 불러온 브라우저에서 2배 크기로 찍어 PNG·WebP로 둡니다. `hero.svg`는 `../images/v2/`의 실제 화면 두 장을 합성합니다.

```bash
npm i --prefix ~/.cache/voiceguard-bake puppeteer-core     # 저장소 밖에 한 번만 설치
NODE_PATH=~/.cache/voiceguard-bake/node_modules CHROME="<Chrome 또는 Edge 실행 파일>" node docs/design/bake.js
python docs/design/zoom.py                                   # Pillow 필요
```

## 색

밝은 카드 한 벌로 GitHub의 밝은 화면과 어두운 화면에 모두 씁니다. 어두운 화면에서는 테두리 있는 밝은 카드로 보입니다.

| 쓰임 | 색 |
| --- | --- |
| 카드 배경 · 테두리 | `#F7F9FC` · `#D3DCE7` |
| 글자 · 보조 글자 | `#0B1424` · `#56657E` |
| 신호색 (단위 · 화살표 · 점) | `#0D9488` |
| 신호색 글자 | `#0F766E` |
| 단계 번호 원 · 번호 | `#0B1424` · `#5EEAD4` |
