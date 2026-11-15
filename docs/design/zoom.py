# README의 1:1 확대 조각 두 장을 저장소 스크린샷에서 잘라 만든다.
# 대시보드 전체 화면(1440~1536px)은 README 열(830px, 휴대폰 380px)에서 글자를 읽을 수 없어,
# 자세히 보여 줄 칸만 원본 크기로 잘라 모서리와 테두리를 굽는다.
#
#   python docs/design/zoom.py                 # docs/images/v2 → docs/images/readme
#   python docs/design/zoom.py <src> <out>     # 폴더를 직접 지정
#
# web_case_detail.png나 web_priority.png를 다시 찍으면 다시 돌리고, 잘리는 위치(CROPS)가 맞는지 확인한다.
import sys
from pathlib import Path

from PIL import Image, ImageDraw

DOCS = Path(__file__).resolve().parents[1]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else DOCS / 'images' / 'v2'
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else DOCS / 'images' / 'readme'
CROPS = {
    # 사건 상세의 '단서 3개' 카드 안쪽 (카드 테두리 제외)
    'zoom_clues.png': ('web_case_detail.png', (428, 848, 880, 1100)),
    # 왼쪽 통화 목록을 '우선순위순'으로 정렬한 모습: 머리, 필터, 정렬, 처음 세 통화
    'zoom_priority.png': ('web_priority.png', (0, 82, 390, 555)),
}
RADIUS = 14
BORDER = (208, 215, 222, 255)  # GitHub 밝은 테두리색. 어두운 화면에서도 부드러운 가장자리로 보인다
SS = 4  # 모서리를 매끄럽게 그리기 위한 배율

OUT.mkdir(parents=True, exist_ok=True)
for out, (src, box) in CROPS.items():
    im = Image.open(SRC / src).convert('RGBA').crop(box)
    w, h = im.size
    mask = Image.new('L', (w * SS, h * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), RADIUS * SS, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    card = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    card.paste(im, (0, 0), mask)
    ring = Image.new('RGBA', (w * SS, h * SS), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), RADIUS * SS, outline=BORDER, width=SS)
    card.alpha_composite(ring.resize((w, h), Image.LANCZOS))
    card.save(OUT / out, optimize=True)
    print(out, card.size)
