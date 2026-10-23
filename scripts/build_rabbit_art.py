# 将兔子原图的轮廓与明暗转换成盲文字符；只在更新素材时运行。
import argparse
import json
from pathlib import Path

from PIL import Image, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "dianstar" / "assets" / "rabbit.json"
ART_SIZES = ((28, 18), (20, 12), (12, 7))
DOT_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def _render(image: Image.Image, columns: int, row_count: int) -> dict[str, object]:
    rgb = image.resize((columns * 2, row_count * 4), Image.Resampling.LANCZOS)
    gray = ImageOps.grayscale(rgb)
    rows = []
    shadows = []
    highlights = []
    for row in range(row_count):
        characters = []
        shadow_characters = []
        highlight_characters = []
        for column in range(columns):
            bits = 0
            shadow_bits = 0
            highlight_bits = 0
            for dx in range(2):
                for dy in range(4):
                    x, y = column * 2 + dx, row * 4 + dy
                    brightness = gray.getpixel((x, y))
                    if brightness < 165:
                        bits |= DOT_BITS[dx][dy]
                    elif columns >= 20:
                        red, green, blue = rgb.getpixel((x, y))
                        dither = BAYER[y % 4][x % 4]
                        if blue >= red + 6 and brightness < 230 and dither < 7:
                            shadow_bits |= DOT_BITS[dx][dy]
                        elif red >= blue + 8 and brightness > 190 and dither < 3:
                            highlight_bits |= DOT_BITS[dx][dy]
            characters.append(chr(0x2800 + bits) if bits else " ")
            shadow_characters.append(chr(0x2800 + shadow_bits) if shadow_bits and not bits else " ")
            highlight_characters.append(chr(0x2800 + highlight_bits) if highlight_bits and not bits else " ")
        rows.append("".join(characters).rstrip())
        shadows.append("".join(shadow_characters).rstrip())
        highlights.append("".join(highlight_characters).rstrip())
    return {"columns": columns, "rows": rows, "shadows": shadows, "highlights": highlights}


def build(source: Path) -> dict[str, object]:
    image = Image.open(source).convert("RGB")
    gray = ImageOps.grayscale(image)
    outline = gray.point(lambda value: 255 if value < 180 else 0)
    bounds = outline.getbbox()
    if bounds is None:
        raise ValueError("图片中没有可识别的兔子轮廓")
    margin = 4
    image = image.crop((max(0, bounds[0] - margin), max(0, bounds[1] - margin),
                        min(image.width, bounds[2] + margin), min(image.height, bounds[3] + margin)))
    return {"variants": [_render(image, columns, rows) for columns, rows in ART_SIZES]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the terminal rabbit outline")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    data = build(args.source)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n".join(data["variants"][0]["rows"]))


if __name__ == "__main__":
    main()
