"""Responsive progress presentation; task state lives in the application layer."""
import asyncio

from rich.text import Text
from textual.widgets import Button, Static

from dianstar.sessions.task_plans import TaskPlanStore


LABELS = {"pending": ("○", "待处理"), "in_progress": ("●", "进行中"),
    "pending_review": ("◐", "待核验"), "completed": ("✓", "已完成"),
    "blocked": ("!", "受阻"), "waiting_user": ("?", "等待你确认")}


class TaskView:
    async def _load_task_plan(self):
        self._task_plan = None
        self._todo_mobile_open = False
        try:
            self._task_plan = await asyncio.to_thread(TaskPlanStore().load, self.conversation.id)
        except (ValueError, OSError):
            self._status("待办记录暂时无法读取，未恢复旧任务。")
        self._render_task_plan()

    def _task_progress(self, event):
        if event.get("conversation_id") != self.conversation.id:
            return
        self._task_plan = event.get("plan")
        self._render_task_plan()

    def _render_task_plan(self):
        if not self.is_mounted:
            return
        plan = self._task_plan
        button = self.query_one("#toggle-todo", Button)
        panel = self.query_one("#todo-pane")
        button.display = bool(plan)
        wide = self.size.width >= 110
        visible = bool(plan) and (not self._todo_hidden if wide else self._todo_mobile_open)
        panel.display = visible
        panel.styles.width = "30%" if wide else "100%"
        panel.styles.max_width = 45 if wide else None
        self.query_one("#chat-pane").display = wide or not visible
        if not plan:
            return
        done = sum(step["status"] == "completed" for step in plan["steps"])
        button.label = ("收起待办" if visible else "待办") + f" {done}/{len(plan['steps'])}"
        self.query_one("#todo-title", Static).update(Text(plan["title"], style="bold"))
        state = {"waiting": "待继续", "completed": "已完成", "cancelled": "已取消",
            "failed": "执行失败", "budget_exhausted": "本轮次数已用尽"}.get(plan.get("run_state"),
            "进行中" if self.busy else "历史进度 · 待核对")
        self.query_one("#todo-summary", Static).update(f"已完成 {done}/{len(plan['steps'])} · {state}")
        text = Text()
        for step in plan["steps"]:
            symbol, label = LABELS[step["status"]]
            if step["status"] == "in_progress" and (not self.busy or plan.get("run_state") != "active"):
                label = "上次进行 · 待继续"
            color = "green" if step["status"] == "completed" else "cyan" if step["status"] == "in_progress" else "yellow" if step["status"] in {"blocked", "waiting_user"} else "default"
            text.append(f"{symbol} {step['title']}\n", style=color)
            text.append(f"  {label}\n\n", style="dim")
        self.query_one("#todo-steps", Static).update(text)

    def _toggle_todo(self):
        if self.size.width >= 110:
            self._todo_hidden = not self._todo_hidden
        else:
            self._todo_mobile_open = not self._todo_mobile_open
        self._render_task_plan()
