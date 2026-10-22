"""从木棉花参考图提取花瓣轮廓，生成运行时使用的终端点阵。"""

import json
from pathlib import Path

from PIL import Image, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent_try" / "assets" / "kapok-source.png"
TARGET = ROOT / "agent_try" / "assets" / "kapok.json"
DOTS = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (0, 3, 0x40),
        (1, 0, 0x08), (1, 1, 0x10), (1, 2, 0x20), (1, 3, 0x80))


def variant(image: Image.Image, columns: int, rows: int) -> dict[str, object]:
    pixels = image.convert("RGB")
    red = Image.new("L", pixels.size)
    red.putdata([
        255 if r > 95 and r - g > 27 and r - b > 35 and g < 216 else 0
        for r, g, b in pixels.getdata()
    ])
    # Close small watercolor gaps, while leaving the five petal contours intact.
    red = red.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    red = red.resize((columns * 2, rows * 4), Image.Resampling.LANCZOS)
    source = pixels.resize((columns * 2, rows * 4), Image.Resampling.LANCZOS)
    glyphs: list[str] = []
    colors: list[str] = []
    for y in range(rows):
        glyph_row = []
        color_row = []
        for x in range(columns):
            mask = 0
            samples = []
            for dx, dy, bit in DOTS:
                px, py = x * 2 + dx, y * 4 + dy
                if red.getpixel((px, py)) >= 100:
                    mask |= bit
                    samples.append(source.getpixel((px, py)))
            glyph_row.append(chr(0x2800 + mask) if mask else " ")
            if not samples:
                color_row.append(" ")
            else:
                r = sum(v[0] for v in samples) / len(samples)
                g = sum(v[1] for v in samples) / len(samples)
                color_row.append("g" if r > 205 and g > 145 else
                                 "l" if r > 180 and g > 105 else
                                 "d" if r < 150 else "r")
        glyphs.append("".join(glyph_row))
        colors.append("".join(color_row))
    return _complete_lower_right_petal(columns, glyphs, colors)


def _complete_lower_right_petal(
    anchor_columns: int, glyphs: list[str], colors: list[str]
) -> dict[str, object]:
    """Add a hand-drawn tip beyond the old crop, without resampling the base art."""
    if anchor_columns == 42:
        addition = {
            10: ("⢀⣀   ", "rr   "),
            11: ("⣾⣿⣧  ", "rlr  "),
            12: ("⣿⣿⣿⣷⡀", "rlggr"),
            13: ("⣿⣿⣿⣿⡇", "rrlgr"),
            14: ("⣿⣿⣿⡿ ", "rrlr "),
            15: ("⠿⠛⠋  ", "drr  "),
            16: ("⠋    ", "r    "),
        }
        glyphs[16] = glyphs[16][:-1] + "⠉"
        colors[16] = colors[16][:-1] + "r"
        extra = 5
    elif anchor_columns == 30:
        addition = {
            7: ("⣀  ", "r  "),
            8: ("⣶⡄ ", "rl "),
            9: ("⣿⣷⡀", "rgr"),
            10: ("⣿⣿⡇", "rlr"),
            11: ("⠻⡿ ", "dr "),
        }
        extra = 3
    else:
        addition = {
            5: ("⣀ ", "r "),
            6: ("⣿⡄", "rl"),
            7: ("⣿⡇", "rr"),
            8: ("⠟ ", "r "),
        }
        extra = 2
    for y in range(len(glyphs)):
        suffix, shades = addition.get(y, (" " * extra, " " * extra))
        if len(suffix) != extra or len(shades) != extra:
            raise ValueError("Hand-drawn petal width does not match its color data")
        glyphs[y] += suffix
        colors[y] += shades
    # Taper the right petal inward to a point above the bottom petal.
    joint = ({16: (33, "⠿         ", "d         "),
              17: (33, "⣷⠿⠋ ", "drr "),
              18: (26, "⣷⣿⣷⣦⣤⠿⠋", "drrllrr"),
              19: (24, "⡿⠿⠛⠋", "drrr")}
             if anchor_columns == 42 else
             {11: (23, "⠿       ", "d       "),
              12: (25, "⠿⠋ ", "rr "),
              13: (17, "⡿⠿⠛⠋ ", "drrr ")}
             if anchor_columns == 30 else
             {8: (17, "⠿      ", "d      "),
              9: (13, "⡿⠿⠛⠋  ", "drrr  ")})
    for y, (start, contour, shades) in joint.items():
        if len(contour) != len(shades):
            raise ValueError("Petal joint width does not match its color data")
        end = start + len(contour)
        glyphs[y] = glyphs[y][:start] + contour + glyphs[y][end:]
        colors[y] = colors[y][:start] + shades + colors[y][end:]
    return {"columns": anchor_columns + extra, "anchor_columns": anchor_columns,
            "rows": glyphs, "colors": colors}


def main() -> None:
    with Image.open(SOURCE) as source:
        # Exclude the inscription and the black strip; the red mask removes paper.
        flower = source.crop((10, 105, 550, 610))
        variants = [variant(flower, width, height) for width, height in ((42, 20), (30, 14), (22, 10))]
    TARGET.write_text(json.dumps({"variants": variants}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
