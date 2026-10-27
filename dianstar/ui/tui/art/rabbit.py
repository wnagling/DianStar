# 读取随包保存的兔子盲文轮廓与明暗，在终端中居中显示。
import json
from functools import lru_cache
from importlib.resources import files

from rich.text import Text


@lru_cache(maxsize=1)
def _variants() -> list[dict[str, object]]:
    data = json.loads(files("dianstar").joinpath("assets", "rabbit.json").read_text(encoding="utf-8"))
    return data["variants"]


def rabbit_lines(width: int, height: int) -> list[Text]:
    """Keep the compact line drawing centered without scaling its characters."""
    for variant in _variants():
        columns = int(variant["columns"])
        rows = variant["rows"]
        if width >= columns and height >= len(rows):
            left = " " * ((width - columns) // 2)
            lines = []
            shadows = variant["shadows"]
            highlights = variant["highlights"]
            for row, shadow, highlight in zip(rows, shadows, highlights):
                line = Text(left)
                for index in range(columns):
                    outline = row[index] if index < len(row) else " "
                    shade = shadow[index] if index < len(shadow) else " "
                    light = highlight[index] if index < len(highlight) else " "
                    if outline != " ":
                        line.append(outline, style="#e7e0cd")
                    elif shade != " ":
                        line.append(shade, style="#6b7080")
                    elif light != " ":
                        line.append(light, style="#776c52")
                    else:
                        line.append(" ")
                lines.append(line)
            return lines
    return []
