"""Decode PowerShell's Unicode CLIXML transport as inert bounded text."""
import re
from xml.etree import ElementTree


def decode_output(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    elif len(data) >= 8 and data[1:8:2] == b"\0\0\0\0":
        text = data.decode("utf-16-le", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("oem", errors="replace")
    if "#< CLIXML" not in text:
        return text
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return "PowerShell 输出包含不支持的 XML 声明，未解析。"
    parts = []
    for segment in text.split("#< CLIXML"):
        segment = segment.strip()
        if not segment:
            continue
        if not segment.startswith("<Objs"):
            parts.append(segment)
            continue
        try:
            root = ElementTree.fromstring(segment)
            nodes = list(root.iter())
            if len(nodes) > 10000:
                return "PowerShell 输出结构超过解析上限，未完整显示。"
            for node in root:
                value = node.text if node.tag.rsplit("}", 1)[-1] == "S" else next(
                    (child.text for child in node if child.tag.rsplit("}", 1)[-1] == "ToString"), None)
                if value:
                    parts.append(re.sub(r"_x([A-Fa-f0-9]{4})_", lambda match: chr(int(match[1], 16)), value))
        except ElementTree.ParseError:
            parts.append(segment)
    return "\n".join(parts)
