"""将参考图生成的木棉花放在兔子后层，并保护兔子的轮廓。"""

import json
from functools import lru_cache
from importlib.resources import files

from rich.text import Text

from dianstar.ui.tui.art.rabbit import rabbit_lines


_COLORS = {"d": "#a93631", "r": "#d95646", "l": "#f08360", "g": "#edb45e"}
_MOAT = 1


@lru_cache(maxsize=1)
def _variants() -> list[dict[str, object]]:
    data = json.loads(files("dianstar").joinpath("assets", "kapok.json").read_text(encoding="utf-8"))
    return data["variants"]


def _flower_lines(variant: dict[str, object], width: int, left: int) -> list[Text]:
    columns = int(variant["columns"])
    result = []
    for row, colors in zip(variant["rows"], variant["colors"]):
        line = Text(" " * left)
        for glyph, color in zip(row, colors):
            line.append(glyph, style=_COLORS.get(color))
        line.append(" " * (width - left - columns))
        result.append(line)
    return result


def welcome_art_lines(width: int, height: int) -> list[Text]:
    """Layer flower behind the rabbit, leaving a blank moat around its silhouette."""
    variant = next((item for item in _variants()
                    if width >= int(item["columns"]) and height >= len(item["rows"])), None)
    if variant is None:
        return rabbit_lines(width, height)

    # The flower takes the full backdrop; the rabbit uses the next smaller variant.
    rabbit = rabbit_lines(width, 12 if int(variant["columns"]) >= 42 else 7)
    if not rabbit:
        return _flower_lines(variant, width, (width - int(variant["columns"])) // 2)

    # A slight left offset keeps the narrow bridge of the left petals visible.
    flower_left = max(0, (width - int(variant.get("anchor_columns", variant["columns"]))) // 2 - 3)
    flower = _flower_lines(variant, width, flower_left)
    canvas_height = max(len(flower), len(rabbit))
    flower_top = (canvas_height - len(flower)) // 2
    rabbit_top = (canvas_height - len(rabbit)) // 2
    result = []
    for y in range(canvas_height):
        background = (flower[y - flower_top]
                      if flower_top <= y < flower_top + len(flower) else Text(" " * width))
        foreground = (rabbit[y - rabbit_top].copy()
                      if rabbit_top <= y < rabbit_top + len(rabbit) else Text())
        foreground.append(" " * (width - len(foreground.plain)))
        occupied = [x for x, char in enumerate(foreground.plain) if char != " "]
        if occupied:
            start = max(0, min(occupied) - _MOAT)
            end = min(width, max(occupied) + _MOAT + 1)
            line = background[:start] + foreground[start:end] + background[end:]
        else:
            line = background
        result.append(line)
    return result
