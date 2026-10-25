"""聊天 Markdown：为裸链接和行内代码中的网页地址保留可点击样式。"""

from copy import copy
import re

from markdown_it.token import Token
from rich.markdown import Markdown
from rich.cells import get_character_cell_size

from .tools.web_reader import public_url


def is_web_link(url: str) -> bool:
    try:
        public_url(url)
        return True
    except ValueError:
        return False


def _link_tokens(tokens: list[Token]) -> list[Token]:
    decorated, depth = [], 0
    for token in tokens:
        if token.type == "link_open":
            depth += 1
        elif token.type == "link_close":
            depth -= 1
        if token.children and token.type != "image":
            token.children = _link_tokens(token.children)
        if depth or token.type not in {"text", "code_inline"}:
            decorated.append(token)
            continue
        cursor = 0
        for match in re.finditer(r"https?://[^\s<>\"`\u3000-\u303f\uff00-\uffef]+", token.content):
            url = match.group().rstrip(".,;:!?")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            if not is_web_link(url):
                continue
            if match.start() > cursor:
                prefix = copy(token)
                prefix.content = token.content[cursor:match.start()]
                decorated.append(prefix)
            link_text = copy(token)
            link_text.content = url
            decorated.extend([Token("link_open", "a", 1, attrs={"href":url}),
                              link_text, Token("link_close", "a", -1)])
            cursor = match.start() + len(url)
        if cursor < len(token.content):
            suffix = copy(token)
            suffix.content = token.content[cursor:]
            decorated.append(suffix)
        elif not token.content:
            decorated.append(token)
    return decorated


class ChatMarkdown(Markdown):
    def __init__(self, content: str):
        super().__init__(content)
        self.parsed = _link_tokens(self.parsed)


class StreamLines:
    """Fold only newly received characters; the pending display row stays bounded."""

    def __init__(self, width: int):
        self.width = max(1, width)
        self._tail = []
        self._cells = 0

    @property
    def tail(self) -> str:
        return "".join(self._tail)

    def append(self, delta: str) -> list[str]:
        completed = []

        def flush():
            completed.append("".join(self._tail))
            self._tail = []
            self._cells = 0

        def character(char):
            cells = get_character_cell_size(char)
            if self._tail and self._cells + cells > self.width:
                flush()
            self._tail.append(char)
            self._cells += cells

        for char in delta:
            if char == "\n":
                flush()
            elif char == "\t":
                for _ in range(8 - self._cells % 8):
                    character(" ")
            elif ord(char) >= 32:
                character(char)
        return completed
