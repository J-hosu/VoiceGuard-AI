"""화면 캡처에 번호 점, 점선, 짧은 설명을 붙인 그림(SVG)을 만든다. 만든 SVG는 bake.js가 PNG로 굽는다.

    python docs/design/callout.py [이름 ...]

- 입력: docs/design/callouts/<이름>.json (캡처 조각의 크기, 표시할 요소의 위치, 설명 글)과 docs/design/shots/의 캡처 조각.
- 출력: docs/design/<이름>.svg. 왼쪽에 화면, 오른쪽에 설명을 두고 번호가 같은 점끼리 점선으로 잇는다.
- 화면 위에 그리는 표시(테두리, 점, 점선)는 밝은 화면과 어두운 화면 어디서나 같은 색(theme.css의 --mark)을 쓴다.
- 설명은 낱말과 짧은 문장만 적는다. 긴 설명은 그림 아래 문서에 번호로 적는다.
- json에서 고를 수 있는 것: img_w(화면 폭, 기본 800), canvas_w(그림 폭, 기본 1280), header(false면 단계 표시와 제목을 빼고 화면과 설명만),
  label_dx(화면과 설명 사이, 기본 70), title_size·sub_size(설명 글자 크기). 조각마다 폭이 달라도 된다.
- 화면은 지어낸 통화로 찍은 것만 쓴다.
"""
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

DESIGN = Path(__file__).resolve().parent
SPECS = DESIGN / "callouts"

WIDTH = 1280
MARGIN = 28
GAP = 26  # 화면 조각 사이(가운데를 줄였다는 표시가 들어간다)
DOT_R = 14
TITLE_SIZE, SUB_SIZE = 23, 18
TITLE_LINE, SUB_LINE = 30, 25


def build(spec: dict) -> str:
    parts, callouts = spec["parts"], spec["callouts"]
    img_w = spec.get("img_w", 800)
    canvas_w = spec.get("canvas_w", WIDTH)
    header = spec.get("header", True)  # False면 단계 표시와 제목 없이 화면과 설명만 그린다(슬라이드처럼 제목이 따로 있을 때)
    title_size, sub_size = spec.get("title_size", TITLE_SIZE), spec.get("sub_size", SUB_SIZE)
    title_line, sub_line = round(title_size * TITLE_LINE / TITLE_SIZE), round(sub_size * SUB_LINE / SUB_SIZE)
    has_lead = header and bool(spec.get("lead"))
    top = (104 if has_lead else 78) if header else MARGIN

    # 화면 조각의 자리
    placed, y = [], top
    for part in parts:
        scale = img_w / part["w"]  # 조각마다 폭이 다를 수 있다(다른 화면에서 찍은 조각)
        height = part["h"] * scale
        placed.append({"x": MARGIN, "y": y, "w": img_w, "h": height, "file": part["file"], "scale": scale})
        y += height + GAP
    img_bottom = y - GAP
    img_right = MARGIN + img_w

    # 표시할 요소의 테두리와 점
    marks = []
    for index, item in enumerate(callouts, start=1):
        part = placed[item["part"]]
        scale = part["scale"]
        x, y0 = part["x"] + item["x"] * scale - 6, part["y"] + item["y"] * scale - 5
        w, h = item["w"] * scale + 12, item["h"] * scale + 10
        x, w = max(x, MARGIN + 2), min(w, img_right - 2 - max(x, MARGIN + 2))
        marks.append({"n": index, "x": x, "y": y0, "w": w, "h": h, "dot_x": x + w, "dot_y": y0, "label": item["label"], "sub": item.get("sub", [])})

    # 오른쪽 설명의 자리: 점과 같은 높이에 두되 서로 겹치지 않게 아래로 민다
    label_x = img_right + spec.get("label_dx", 70)
    cursor = top + DOT_R
    for mark in sorted(marks, key=lambda m: m["dot_y"]):
        height = title_line + sub_line * len(mark["sub"]) + 16
        mark["cy"] = max(mark["dot_y"], cursor)
        cursor = mark["cy"] + height
    bottom = max(img_bottom, cursor - 16) + MARGIN
    overflow = (cursor - 16) - (bottom - MARGIN)
    if overflow > 0:
        bottom += overflow

    # 같은 줄에 있는 점은 점선이 겹치므로, 왼쪽 점의 선을 위로 올려 보낸다
    for mark in marks:
        same_row = sorted((other for other in marks if abs(other["dot_y"] - mark["dot_y"]) < 6), key=lambda m: -m["dot_x"])
        mark["rise"] = 22 * same_row.index(mark)

    # 점선이 서로 건너가지 않게 세로 길(lane)을 나눈다: 아래로 많이 꺾이는 선일수록 오른쪽 길을 쓴다
    lanes = sorted(marks, key=lambda m: m["cy"] - m["dot_y"])
    for rank, mark in enumerate(lanes):
        mark["lane"] = img_right + 16 + rank * 9

    out = [
        '<?xml-stylesheet href="theme.css" type="text/css"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{canvas_w}" height="{round(bottom)}" viewBox="0 0 {canvas_w} {round(bottom)}">',
        f'  <!-- callout.py가 callouts/{spec["name"]}.json으로 만든 그림이다. 직접 고치지 말고 json을 고친 뒤 다시 만든다. -->',
        f'  <rect class="card" x="1" y="1" width="{canvas_w - 2}" height="{round(bottom) - 2}" rx="24"/>',
    ]
    # 머리: 단계 표시와 한 줄 설명
    step = spec.get("step", "")
    pill_w = 28 + 17 * len(step)
    if header:
        out.append(f'  <rect class="f-node" x="{MARGIN}" y="24" width="{pill_w}" height="40" rx="20"/>')
        out.append(f'  <text class="kr f-node-ink" x="{MARGIN + pill_w / 2}" y="52" font-size="21" font-weight="700" text-anchor="middle">{escape(step)}</text>')
        out.append(f'  <text class="kr f-ink" x="{MARGIN + pill_w + 16}" y="54" font-size="28" font-weight="800" letter-spacing="-0.5">{escape(spec.get("title", ""))}</text>')
    if has_lead:
        out.append(f'  <text class="kr f-muted" x="{MARGIN}" y="90" font-size="19" font-weight="500">{escape(spec["lead"])}</text>')

    # 화면 조각
    out.append("  <defs>")
    for index, part in enumerate(placed):
        out.append(f'    <clipPath id="clip{index}"><rect x="{part["x"]}" y="{part["y"]:.1f}" width="{part["w"]}" height="{part["h"]:.1f}" rx="12"/></clipPath>')
    out.append("  </defs>")
    for index, part in enumerate(placed):
        out.append(f'  <image xlink:href="shots/{part["file"]}" href="shots/{part["file"]}" x="{part["x"]}" y="{part["y"]:.1f}" width="{part["w"]}" height="{part["h"]:.1f}" '
                   f'preserveAspectRatio="none" clip-path="url(#clip{index})"/>')
        out.append(f'  <rect class="f-none s-border" x="{part["x"]}" y="{part["y"]:.1f}" width="{part["w"]}" height="{part["h"]:.1f}" rx="12" stroke-width="2"/>')
        if index < len(placed) - 1:  # 가운데를 줄였다는 표시
            middle = part["y"] + part["h"] + GAP / 2
            for offset in (-14, 0, 14):
                out.append(f'  <circle class="f-faint" cx="{MARGIN + img_w / 2 + offset}" cy="{middle:.1f}" r="3"/>')

    # 테두리, 점선, 점
    out.append('  <g class="f-none s-mark" stroke-width="3" stroke-linejoin="round">')
    for mark in marks:
        out.append(f'    <rect class="mark-box" x="{mark["x"]:.1f}" y="{mark["y"]:.1f}" width="{mark["w"]:.1f}" height="{mark["h"]:.1f}" rx="9"/>')
    out.append("  </g>")
    out.append('  <g class="f-none s-mark" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="2 7">')
    for mark in marks:
        start_x, end_x = mark["dot_x"] + DOT_R, label_x - DOT_R - 6
        if mark["rise"]:
            high = mark["dot_y"] - mark["rise"]
            dot_x, dot_top, lane_x, label_y = mark['dot_x'], mark['dot_y'] - DOT_R, mark['lane'], mark['cy']
            out.append(f'    <path d="M{dot_x:.1f} {dot_top:.1f} V{high:.1f} H{lane_x:.1f} V{label_y:.1f} H{end_x:.1f}"/>')
        elif abs(mark["cy"] - mark["dot_y"]) < 1.5:
            out.append(f'    <path d="M{start_x:.1f} {mark["dot_y"]:.1f} H{end_x:.1f}"/>')
        else:
            lane = max(mark["lane"], start_x + 4)
            out.append(f'    <path d="M{start_x:.1f} {mark["dot_y"]:.1f} H{lane:.1f} V{mark["cy"]:.1f} H{end_x:.1f}"/>')
    out.append("  </g>")
    for mark in marks:
        for cx, cy in ((mark["dot_x"], mark["dot_y"]), (label_x, mark["cy"])):
            out.append(f'  <circle class="f-mark" cx="{cx:.1f}" cy="{cy:.1f}" r="{DOT_R}"/>')
            out.append(f'  <text class="en f-on-mark" x="{cx:.1f}" y="{cy + 6.5:.1f}" font-size="18" font-weight="800" text-anchor="middle">{mark["n"]}</text>')

    # 오른쪽 설명
    for mark in marks:
        text_x, base = label_x + DOT_R + 12, mark["cy"] + 8
        out.append(f'  <text class="kr f-ink" x="{text_x}" y="{base:.1f}" font-size="{title_size}" font-weight="800" letter-spacing="-0.3">{escape(mark["label"])}</text>')
        for row, line in enumerate(mark["sub"], start=1):
            out.append(f'  <text class="kr f-muted" x="{text_x}" y="{base + 4 + sub_line * row:.1f}" font-size="{sub_size}" font-weight="500">{escape(line)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main(names: list[str]) -> int:
    made = 0
    for path in sorted(SPECS.glob("*.json")):
        if names and path.stem not in names:
            continue
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.setdefault("name", path.stem)
        (DESIGN / f"{path.stem}.svg").write_text(build(spec), encoding="utf-8", newline="\n")
        print("만듦", f"docs/design/{path.stem}.svg")
        made += 1
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
