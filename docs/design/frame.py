"""화면 캡처에 둥근 모서리와 옅은 테두리를 입힌다. GitHub의 밝은 화면과 어두운 화면 어디서나 캡처의 가장자리가 보이게 하려는 것이다.

    python docs/design/frame.py <반지름> <그림> [<그림> ...]

- 반지름은 그림 폭에 대한 비율(%)이다. 휴대폰 화면은 6.8, 넓은 화면과 대시보드 조각은 1.8을 쓴다.
- 이미 입힌 그림은 건너뛴다(PNG 안에 표시를 남긴다). 새로 찍은 화면에만 다시 실행하면 된다.
- 색 수를 줄여 둔 그림(팔레트 PNG)은 입힌 뒤 다시 256색으로 줄인다.
- Pillow가 필요하다.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

SS = 4  # 가장자리를 매끄럽게 하려고 4배로 그렸다가 줄인다
EDGE = (128, 140, 160, 120)  # 밝은 바탕과 어두운 바탕 모두에서 보이는 중간 밝기


def frame(path: Path, percent: float) -> bool:
    im = Image.open(path)
    if im.info.get("vg-frame") == "1":
        return False
    paletted = im.mode == "P"
    im = im.convert("RGBA")
    w, h = im.size
    radius = max(4, round(w * percent / 100))
    line = max(2, round(w / 500))
    big = (w * SS, h * SS)
    mask = Image.new("L", big, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), radius * SS, fill=255)
    ring = Image.new("RGBA", big, (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), radius * SS, outline=EDGE, width=line * SS)
    im = Image.alpha_composite(im, ring.resize((w, h), Image.LANCZOS))
    im.putalpha(mask.resize((w, h), Image.LANCZOS))
    if paletted:
        im = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    meta = PngInfo()
    meta.add_text("vg-frame", "1")
    im.save(path, optimize=True, pnginfo=meta)
    return True


if __name__ == "__main__":
    r = float(sys.argv[1])
    for name in sys.argv[2:]:
        p = Path(name)
        print(("입힘 " if frame(p, r) else "건너뜀 ") + str(p))
