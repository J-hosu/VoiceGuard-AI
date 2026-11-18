# README 그림 원본

[README](../../README.md)에 쓰는 배너와 그림의 원본입니다. 결과 파일은 `docs/images/readme/`에 있습니다. 그림마다 GitHub의 밝은 화면용과 어두운 화면용(`-dark`)을 함께 만듭니다.

| 원본 | 결과 | README에서 쓰는 곳 | 다시 구울 때 |
| --- | --- | --- | --- |
| `hero.svg` | `hero.webp`<br>`hero-dark.webp` | 맨 위 배너 | 휴대폰 화면(`app_realtime_agent.png`)이나 대시보드 화면(`web_priority.png`)을 교체했을 때 다시 굽습니다. 두 화면은 README 본문과 같은 현재 빌드에서 가상 데이터로 촬영한 것을 씁니다. |
| `numbers.svg` | `numbers.png`<br>`numbers-dark.png` | 소개 바로 아래의 숫자 카드 | 숫자나 기준 모델이 바뀔 때 다시 굽습니다. 세 칸 모두 현재 모델(통화 분류기 v5)의 결과이며, 칸마다 무엇을 어떤 데이터로 잰 값인지 함께 적습니다. 분류기 단독(1·2번 칸)과 전체 시스템(3번 칸)은 이름표 색을 다르게 합니다. README의 표와 대체 문구도 함께 고칩니다. |
| `loop.svg` | `loop.png`<br>`loop-dark.png` | '통화 한 건의 흐름'의 다섯 단계 | 단계 이름, 하는 일, 맡는 구성 요소가 바뀔 때 다시 굽습니다. README의 다섯 단계 목록과 뜻이 같아야 합니다. |
| `timeline.svg` | `timeline.png`<br>`timeline-dark.png` | '통화 한 건의 흐름'의 위험도 추이 | 기준 시연 기록([전화 흐름으로 기능 보기](../one-call.md))을 다시 만들었을 때 다시 굽습니다. 시각과 위험도, 좌표 계산식은 원본의 주석에 있습니다. |
| `architecture.svg` | `architecture.png`<br>`architecture-dark.png` | '시스템 구조'의 요약 구조도 | 구성 요소나 외부로 보내는 데이터가 바뀔 때 다시 굽습니다. 자세한 구조도는 `../diagrams/architecture.html`에 따로 있습니다. |

`docs/images/readme/demo_phone.gif`(휴대폰 화면)와 `demo_wide.gif`(넓은 화면)는 대본 시연을 녹화한 것으로, 이 폴더의 원본에서 만들지 않습니다. 대시보드 화면과 조각 그림은 [수사관 지원용 능동 AI 에이전트 기능](../investigator.md)에 있습니다.

## 굽기

GitHub는 `<img>` 속 SVG에서 웹 글꼴을 쓰지 못합니다. 그래서 `fonts.txt`의 글꼴(Noto Sans KR, Inter)을 불러온 브라우저에서 2배 크기로 찍어 PNG·WebP로 둡니다. `hero.svg`는 `../images/v2/`의 실제 화면 두 장을 합성합니다.

```bash
npm i --prefix ~/.cache/voiceguard-bake puppeteer-core     # 저장소 밖에 한 번만 설치
NODE_PATH=~/.cache/voiceguard-bake/node_modules CHROME="<Chrome 또는 Edge 실행 파일>" node docs/design/bake.js
```

`bake.js`는 이 폴더의 SVG를 모두 두 가지 화면용으로 다시 굽습니다. 그림 이름을 주면 그 그림만 굽습니다(`node docs/design/bake.js numbers loop`). 원본을 고치지 않은 그림은 결과 파일이 달라져도 커밋하지 않습니다.

README에서는 `<picture>`로 화면에 맞는 그림을 고릅니다.

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/readme/numbers-dark.png">
  <img src="docs/images/readme/numbers.png" width="100%" alt="...">
</picture>
```

## 화면 캡처의 가장자리

앱과 대시보드의 화면 캡처(`docs/images/v2/`, `docs/images/readme/`의 `investigator_*`, `one-call_*`)에는 둥근 모서리와 옅은 테두리를 입혔습니다. 밝은 화면과 어두운 화면 어디서나 캡처의 가장자리가 보이게 하려는 것입니다. 화면을 다시 찍으면 `frame.py`를 다시 실행합니다. 이미 입힌 그림은 건너뜁니다.

```bash
python docs/design/frame.py 6.8 docs/images/v2/app_*.png                                   # 휴대폰 화면
python docs/design/frame.py 1.8 docs/images/v2/web_*.png docs/images/readme/investigator_*.png  # 넓은 화면과 조각
```

## 색

그림은 색을 직접 적지 않고 `theme.css`의 클래스를 씁니다(`f-`는 면, `s-`는 선). 색을 바꿀 때는 `theme.css`만 고치고 다시 굽습니다. SVG 파일을 브라우저에서 바로 열면 밝은 화면용으로 보입니다.

| 쓰임 | 밝은 화면 | 어두운 화면 |
| --- | --- | --- |
| 카드 배경 · 테두리 | `#F7F9FC` · `#D3DCE7` | `#0F1724` · `#2B384C` |
| 카드 안의 상자 | `#FFFFFF` | `#162132` |
| 글자 · 보조 글자 · 흐린 글자 | `#0B1424` · `#56657E` · `#7A8AA0` | `#F1F5F9` · `#A6B4C8` · `#8292A9` |
| 신호색 (단위 · 선 · 점) · 신호색 글자 | `#0D9488` · `#0F766E` | `#2DD4BF` · `#5EEAD4` |
| 단계 번호 원 · 번호 | `#0B1424` · `#5EEAD4` | `#134E4A` · `#99F6E4` |
| 주의 (선 · 글자 · 바탕) | `#D97706` · `#B45309` · `#FDF1DC` | `#F59E0B` · `#FCD34D` · `#33270F` |
| 위험 (선 · 글자 · 바탕) | `#DC2626` · `#B91C1C` · `#FCE5E4` | `#F87171` · `#FCA5A5` · `#3A1B1E` |

글자 크기는 1280px 폭 기준 24px 이상으로 둡니다. README 본문 폭에서는 약 0.7배로 줄어 보이기 때문입니다.
