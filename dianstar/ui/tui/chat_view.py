"""Chat history, incremental answer rendering and thinking panels."""
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.text import Text
from textual.widgets import Label, RichLog
from dianstar.ui.tui.art.welcome import welcome_art_lines
from dianstar.ui.tui.rendering import ChatMarkdown, StreamLines
from dianstar.ui.tui.widgets import ChatLog


class ChatView:
    """Rendering behavior used by the Textual application."""

    def _render_history(self) -> None:
        if self.busy:
            return
        self._close_copy_menu()
        self._clear_stream()
        self._reset_active_thinking()
        self._expanded_thinking.clear()
        self._chat_blocks = []
        # 旧会话把思考保存在模型消息中，按用户轮次对应，避免重复问题串轮。
        reasoning_turns = []
        for message in self.conversation.model_messages:
            if message.get("role") == "user":
                reasoning_turns.append([])
            elif message.get("role") == "assistant" and reasoning_turns:
                content = message.get("reasoning_content")
                if isinstance(content, str) and content:
                    reasoning_turns[-1].append(content)
        compatible = (len(self.conversation.messages) % 2 == 0
                      and len(reasoning_turns) == len(self.conversation.messages) // 2)
        for index, message in enumerate(self.conversation.messages):
            reasoning = message.get("reasoning_content", "")
            if message["role"] == "assistant" and not reasoning and compatible:
                reasoning = "\n\n".join(reasoning_turns[index // 2])
            if message["role"] == "assistant" and reasoning:
                self._chat_blocks.append({"kind": "thinking", "content": reasoning})
            self._chat_blocks.append({"kind": "message", **message})
        self._redraw_chat()


    def _redraw_chat(self) -> None:
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        if self._chat_blocks:
            self._welcome_only = False
            for index, block in enumerate(self._chat_blocks):
                if block["kind"] == "thinking":
                    if index == self._active_thinking:
                        self._thinking_tail_start = len(chat.lines)
                    chat.write(self._thinking_panel(index), width=max(1, chat.scrollable_content_region.width))
                elif block["kind"] == "stream":
                    self._begin_stream_render()
                    self._paint_stream()
                else:
                    self._write_message(block["role"], block["content"])
        else:
            self._welcome_only = True
            art = welcome_art_lines(max(0, chat.size.width - 4), max(1, chat.size.height - 2))
            content_height = max(0, chat.size.height - 2)
            for _ in range(max(0, (content_height - len(art)) // 2)):
                chat.write(Text(" "))
            for line in art:
                chat.write(line)
        self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")


    def _show_message(self, role: str, content: str) -> None:
        self._chat_blocks.append({"kind": "message", "role": role, "content": content})
        self._write_message(role, content)


    def _write_message(self, role: str, content: str) -> None:
        self._welcome_only = False
        chat = self.query_one("#chat", RichLog)
        chat.write(Text("主包" if role == "user" else "DianStar", style="bold cyan" if role == "user" else "bold green"))
        chat.write(Text(content) if role == "user" else ChatMarkdown(content))
        chat.write("")


    def _stream_answer(self, content: str) -> None:
        """Append the unfinished answer inside the log, with bounded repaint frequency."""
        if not content:
            self._clear_stream(remove=True)
            return
        if self._active_stream is None:
            self._paint_thinking()
            self._active_stream = len(self._chat_blocks)
            self._chat_blocks.append({"kind":"stream", "content":content})
            self._begin_stream_render()
            self._paint_stream()
            self._status("正在回答…")
        else:
            self._chat_blocks[self._active_stream]["content"] = content
            if self._stream_timer is None:
                self._stream_timer = self.set_timer(1 / 30, self._paint_stream)


    def _begin_stream_render(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        self._stream_start = len(chat.lines)
        chat.write(Text("DianStar", style="bold green"))
        self._stream_tail_start = len(chat.lines)
        self._stream_width = chat.scrollable_content_region.width
        self._stream_lines = StreamLines(self._stream_width)
        self._stream_rendered = ""


    def _paint_stream(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        if self._active_stream is None:
            return
        chat = self.query_one("#chat", ChatLog)
        content = self._chat_blocks[self._active_stream]["content"]
        # 已完成的显示行只写一次；长段落也只排版新收到的字符。
        if (self._stream_width != chat.scrollable_content_region.width
                or not content.startswith(self._stream_rendered)):
            self._redraw_chat()
            return
        if content == self._stream_rendered:
            return
        follow = chat.is_vertical_scroll_end
        chat.truncate_tail(self._stream_tail_start)
        width = max(1, chat.scrollable_content_region.width)
        completed = self._stream_lines.append(content[len(self._stream_rendered):])
        if completed:
            chat.write(Text("\n".join(completed), no_wrap=True, overflow="crop"),
                       width=width, scroll_end=False)
        self._stream_tail_start = len(chat.lines)
        if self._stream_lines.tail:
            chat.write(Text(self._stream_lines.tail, no_wrap=True, overflow="crop"),
                       width=width, scroll_end=False)
        self._stream_rendered = content
        chat.refresh()
        if follow:
            chat.call_after_refresh(chat.scroll_end, animate=False, immediate=True, x_axis=False)


    def _clear_stream(self, *, remove: bool = False) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        if remove and self._active_stream is not None:
            chat = self.query_one("#chat", ChatLog)
            self._chat_blocks.pop(self._active_stream)
            if self._stream_start is not None:
                chat.truncate_tail(self._stream_start)
        self._active_stream = None
        self._stream_start = self._stream_tail_start = None
        self._stream_lines = None
        self._stream_width = None
        self._stream_rendered = ""


    def _finish_answer(self, answer: str) -> None:
        if self._active_stream is None:
            self._show_message("assistant", answer)
            return
        chat = self.query_one("#chat", ChatLog)
        self._chat_blocks[self._active_stream] = {"kind":"message", "role":"assistant", "content":answer}
        chat.replace_tail(self._stream_start, Group(Text("DianStar", style="bold green"),
                                                  ChatMarkdown(answer), Text("")))
        self._clear_stream()


    def _receive_thinking(self, content: str) -> None:
        if not content:
            self._new_thinking_part = True
            return
        if self._new_thinking_part:
            self._thinking_parts.append(content)
            self._new_thinking_part = False
        else:
            self._thinking_parts[-1] = content
        self._stream_thinking("\n\n".join(self._thinking_parts))


    def _stream_thinking(self, content: str) -> None:
        if not content:
            self._reset_active_thinking()
            return
        if self._active_thinking is None:
            self._active_thinking = len(self._chat_blocks)
            self._chat_blocks.append({"kind": "thinking", "content": content})
            self._thinking_tail_start = len(self.query_one("#chat", RichLog).lines)
            self._paint_thinking()
        else:
            self._chat_blocks[self._active_thinking]["content"] = content
            if self._thinking_timer is None:
                self._thinking_timer = self.set_timer(0.1, self._paint_thinking)


    def _reset_active_thinking(self) -> None:
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        self._active_thinking = None
        self._thinking_tail_start = None
        self._thinking_rendered = None
        self._thinking_parts = []
        self._new_thinking_part = True


    def _thinking_panel(self, index: int) -> Panel:
        expanded = index in self._expanded_thinking
        content = self._chat_blocks[index]["content"]
        title = Text("模型思考 · 收起" if expanded else "模型思考 · 展开完整内容",
                     style=Style(color="#6b9cbb", meta={"@click": f"app.toggle_thinking({index})"}))
        visible = content if expanded else "\n".join(content.splitlines()[-4:])
        return Panel(Text(visible, style="#aeb7c6", no_wrap=not expanded, overflow="ellipsis"),
                     title=title, title_align="left", border_style="#365f7d", padding=(0, 1))


    def _paint_thinking(self) -> None:
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        if self._active_thinking is None:
            return
        signature = (self._active_thinking, self._chat_blocks[self._active_thinking]["content"],
                     self._active_thinking in self._expanded_thinking)
        if signature == self._thinking_rendered:
            return
        chat = self.query_one("#chat", ChatLog)
        if self._active_thinking == len(self._chat_blocks) - 1 and self._thinking_tail_start is not None:
            chat.replace_tail(self._thinking_tail_start, self._thinking_panel(self._active_thinking))
        else:
            self._redraw_chat()
        self._thinking_rendered = signature


    def action_toggle_thinking(self, index: int) -> None:
        if not 0 <= index < len(self._chat_blocks) or self._chat_blocks[index]["kind"] != "thinking":
            return
        if index in self._expanded_thinking:
            self._expanded_thinking.remove(index)
        else:
            self._expanded_thinking.add(index)
        chat = self.query_one("#chat", RichLog)
        scroll_y = chat.scroll_y
        self._redraw_chat()
        chat.call_after_refresh(chat.scroll_to, y=scroll_y, animate=False)
