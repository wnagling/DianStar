"""Inline, independently expandable tool receipts in the chat log."""
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from dianstar.sessions.tool_activity import ToolActivity
from .widgets import ChatLog


LABELS = {"running": "处理中", "success": "完成", "failed": "失败", "not_executed": "未执行",
          "cancelled": "已取消", "interrupted": "结果待核对", "warning": "需核对"}
COLORS = {"running": "cyan", "success": "green", "failed": "red", "not_executed": "yellow",
          "cancelled": "yellow", "interrupted": "yellow", "warning": "yellow"}


class ToolView:
    def _record_tool_event(self, event):
        if self._active_tools is None:
            self._active_tools = len(self._chat_blocks)
            self._tool_activity = ToolActivity()
            self._chat_blocks.append({"kind": "tools", "calls": self._tool_activity.records})
            self._tool_tail_start = len(self.query_one("#chat", ChatLog).lines)
        self._tool_activity.consume(event)
        if self._tool_timer is None:
            self._tool_timer = self.set_timer(.1, self._paint_tools)

    def _reset_active_tools(self):
        if self._tool_timer is not None:
            self._tool_timer.stop()
        self._tool_timer = None
        self._active_tools = self._tool_tail_start = self._tool_activity = None

    def _finish_tools(self):
        if self._tool_activity is not None:
            self._tool_activity.interrupt()
        self._paint_tools()
        self._reset_active_tools()

    def _paint_tools(self):
        if self._tool_timer is not None:
            self._tool_timer.stop()
            self._tool_timer = None
        if self._active_tools is None:
            return
        chat = self.query_one("#chat", ChatLog)
        if self._active_tools == len(self._chat_blocks) - 1 and self._tool_tail_start is not None:
            chat.replace_tail(self._tool_tail_start, self._tools_panel(self._active_tools))
        else:
            self._redraw_chat()

    def _tools_panel(self, index):
        calls = self._chat_blocks[index]["calls"]
        expanded = index in self._expanded_tools
        pending = sum(item["status"] == "running" for item in calls)
        failures = sum(item["status"] == "failed" for item in calls)
        counts = f"{len(calls)} 项" + (f" · {pending} 处理中" if pending else "") + (f" · {failures} 失败" if failures else "")
        title = Text("工具执行 · " + counts + (" · 收起" if expanded else " · 展开详情"),
            style=Style(color="#e7a04d", meta={"@click": f"app.toggle_tools({index})"}))
        pending_indices = [i for i, item in enumerate(calls) if item["status"] == "running"][-4:]
        recent = [i for i in reversed(range(len(calls))) if i not in pending_indices][:4-len(pending_indices)]
        selected = list(range(len(calls))) if expanded else sorted(pending_indices + recent)
        rows = []
        if not expanded and len(selected) < len(calls):
            rows.append(Text(f"其余 {len(calls)-len(selected)} 项可展开查看", style="dim"))
        for item_index in selected:
            item = calls[item_index]
            color = COLORS[item["status"]]
            duration = f" · {item['duration_ms']/1000:.1f} 秒" if item.get("duration_ms") is not None else ""
            label = Text(f"{LABELS[item['status']]}  ", style=color)
            label.append(item["title"], style="bold")
            if item.get("target"):
                label.append(" · " + item["target"].replace("\n", " "))
            label.append(duration, style="dim")
            if expanded:
                label.append("  [详情]", style=Style(color="#e7a04d", meta={"@click": f"app.toggle_tool_item({index},{item_index})"}))
            else:
                label.no_wrap, label.overflow = True, "ellipsis"
            rows.append(label)
            if item.get("summary"):
                rows.append(Text("  " + item["summary"], style="dim", no_wrap=not expanded, overflow="ellipsis"))
            if expanded and (index, item_index) in self._expanded_tool_items:
                rows.append(Text("工具：" + item["tool"], style="#e7a04d"))
                if item["call_id"]:
                    rows.append(Text("调用记录：" + item["call_id"], style="dim"))
                if item["parameters"]:
                    rows.append(Text("参数：\n" + item["parameters"], style="#aeb7c6"))
                if item["output"]:
                    rows.append(Text("结果：\n" + item["output"], style="#aeb7c6"))
        return Panel(Group(*(rows or [Text("正在准备工具调用…")])), title=title, title_align="left",
                     border_style="#b87832", padding=(0, 1))

    def action_toggle_tools(self, index):
        if not 0 <= index < len(self._chat_blocks) or self._chat_blocks[index]["kind"] != "tools":
            return
        if index in self._expanded_tools:
            self._expanded_tools.remove(index)
        else:
            self._expanded_tools.add(index)
        self._redraw_chat()

    def action_toggle_tool_item(self, index, item_index):
        if (not 0 <= index < len(self._chat_blocks) or self._chat_blocks[index]["kind"] != "tools"
                or not 0 <= item_index < len(self._chat_blocks[index]["calls"])):
            return
        key = (index, item_index)
        if key in self._expanded_tool_items:
            self._expanded_tool_items.remove(key)
        else:
            self._expanded_tool_items.add(key)
        self._redraw_chat()
