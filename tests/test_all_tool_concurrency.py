"""All tool kinds participate; resource conflicts, not tool kinds, order work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.app.tool_resources import ToolResources
from dianstar.config.mcp import MCPSettings
from dianstar.config.search import SearchSettings
from dianstar.core.registry import ToolRegistration
from dianstar.core.resources import ResourceClaim
from dianstar.core.scheduler import BatchExecutor
from dianstar.core.types import Observation, ToolAction
from dianstar.security.tool_policy import ToolPolicy
from dianstar.tools.local.files.journal import FileJournal
from dianstar.tools.local.registry import build_local_registry
from dianstar.tools.local.web import WebTools
from dianstar.tools.mcp.servers.git.workspace import GitWorkspace
from tests.test_orchestration import calls
from tests.test_permissions import reply, with_review, workflow_results


class ResourceSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def registration(self, *, query=False):
        return ToolRegistration({}, "fixture", AsyncMock(), None if query else lambda _: "confirm", parallel_safe=query)

    async def test_writes_commands_and_mcp_mutations_all_overlap_independent_resources(self):
        names = ("write", "workspace_run", "mcp_git__git_create_branch", "mcp_github__create_issue")
        registry = {name: self.registration() for name in names}
        active, peak, entered = 0, 0, asyncio.Event()
        async def execute(action):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                entered.set()
            try:
                await asyncio.wait_for(entered.wait(), 3)
                return Observation(action, {"success": True})
            finally:
                active -= 1
        scheduler = BatchExecutor(registry, execute, resources=lambda action: (
            ResourceClaim("runtime"), ResourceClaim("fixture", (action.call_id,), True)))
        result = await scheduler([ToolAction(name, {}, str(index)) for index, name in enumerate(names)])
        self.assertEqual(peak, 4)
        self.assertTrue(all(item.result.get("success") for item in result), result)

    async def test_conflicts_preserve_call_order_without_holding_worker_slots(self):
        events, independent = [], asyncio.Event()
        async def execute(action):
            events.append(action.call_id)
            if action.call_id == "first":
                await asyncio.wait_for(independent.wait(), 3)
            if action.call_id == "independent":
                independent.set()
            return Observation(action, {"success": True})
        def resources(action):
            key = "other" if action.call_id == "independent" else "same"
            return (ResourceClaim("files", (key,), True),)
        scheduler = BatchExecutor({"write": self.registration()}, execute, concurrency=2, resources=resources)
        result = await scheduler([ToolAction("write", {}, identifier) for identifier in ("first", "second", "independent")])
        self.assertTrue(all(item.result.get("success") for item in result), result)
        self.assertEqual(events, ["first", "independent", "second"])

    def test_trusted_planner_covers_files_repositories_remotes_and_shared_state(self):
        root = Path(tempfile.gettempdir()).resolve()
        registry = {name: self.registration(query=name in {"read", "mcp_git__git_status"}) for name in
            ("write", "read", "workspace_run", "mcp_git__git_status", "mcp_github__create_issue", "open_project", "configure_mcp")}
        planner = ToolResources(registry, None, None)
        from dianstar.core.resources import conflicts
        def plan(name, **args):
            return planner(ToolAction(name, args))
        a = plan("write", repository=str(root), path="a.txt")
        b = plan("write", repository=str(root), path="b.txt")
        self.assertFalse(conflicts(a, b))
        self.assertTrue(conflicts(a, plan("read", repository=str(root), path="a.txt")))
        self.assertTrue(conflicts(a, plan("workspace_run", repository=str(root))))
        self.assertFalse(conflicts(a, plan("workspace_run", repository=str(root / "elsewhere"))))
        self.assertTrue(conflicts(a, plan("open_project")))
        self.assertTrue(conflicts(a, plan("configure_mcp")))
        remote = plan("mcp_github__create_issue", owner="Owner", repo="Repo")
        self.assertFalse(conflicts(remote, plan("mcp_github__create_issue", owner="owner", repo="another")))
        self.assertTrue(conflicts(remote, plan("mcp_github__create_issue", owner="owner", repo="repo")))
        fork = plan("mcp_github__create_issue", owner="owner", repo="base", head="fork:feature")
        self.assertTrue(conflicts(fork, plan("mcp_github__create_issue", owner="fork", repo="source")))

    async def test_reversed_multi_resource_declarations_cannot_create_a_scheduler_cycle(self):
        order = []
        async def execute(action):
            order.append(action.call_id)
            await asyncio.sleep(0)
            return Observation(action, {"success": True})
        def claims(action):
            keys = ("a", "b") if action.call_id == "first" else ("b", "a")
            return tuple(ResourceClaim("files", (key,), True) for key in keys)
        scheduler = BatchExecutor({"write": self.registration()}, execute, resources=claims)
        await asyncio.wait_for(scheduler([ToolAction("write", {}, value) for value in ("first", "second")]), 2)
        self.assertEqual(order, ["first", "second"])

    async def test_web_search_and_read_share_the_same_scheduler_as_writes(self):
        entered, all_entered = set(), asyncio.Event()
        async def web_call(name, arguments):
            entered.add(name)
            if len(entered) == 3:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), 3)
            return {"content": "fixture"}
        evidence = SimpleNamespace(observe=lambda name, args, result: result)
        registry, _ = build_local_registry(SimpleNamespace(execute=web_call), evidence, Path("fixture.env"), SearchSettings())
        async def write(action):
            return Observation(action, await web_call("write", {}))
        registry["write"] = ToolRegistration({}, "workspace.write", write, lambda _: "confirm")
        scheduler = BatchExecutor(registry, lambda action: registry[action.name].handler(action),
                                  resources=ToolResources(registry, None, None))
        actions = [ToolAction("search_web", {"query": "fixture"}, "search"),
                   ToolAction("read_webpage", {"url": "https://example.test/known"}, "read"),
                   ToolAction("write", {"repository": str(Path.cwd()), "path": "fixture.txt"}, "write")]
        result = await scheduler(actions)
        self.assertEqual(entered, {"search_web", "read_webpage", "write"})
        self.assertTrue(all(not item.result.get("error") for item in result), result)


class ActualToolConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.roots = [self.base / name for name in ("project-a", "project-b")]
        for root in self.roots:
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
        self.env = self.base / ".env"
        self.env.write_text("DEEPSEEK_API_KEY=fixture-model-key\n")
        settings = MCPSettings(git_enabled=True, git_repositories=tuple(map(str, self.roots)), git_write_enabled=True,
                               github_enabled=False, workspace_write_enabled=True)
        for context in (patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")}),
                        patch("dianstar.app.runtime.load_mcp_settings", return_value=settings)):
            context.start()
            self.addCleanup(context.stop)

    async def run_calls(self, items, approve=None):
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=with_review([calls(*items), reply("已执行。")]))
            await asyncio.wait_for(run_turn("执行指定的独立操作并核对结果", env_file=self.env, project_directory=self.roots[0],
                on_permission=approve, tool_policies={"workspace.write": ToolPolicy(requires_approval=False)}, history=history), 30)
        return workflow_results(history)

    async def test_actual_writes_overlap_without_rolling_back_a_live_transaction(self):
        barrier = threading.Barrier(2, timeout=8)
        original = FileJournal.save
        first, mutex = set(), threading.Lock()
        def save(journal, record):
            result = original(journal, record)
            with mutex:
                wait = record["status"] == "prepared" and record["id"] not in first
                if wait:
                    first.add(record["id"])
            if wait:
                barrier.wait()
            return result
        root = self.roots[0]
        with patch.object(FileJournal, "save", save):
            results = await self.run_calls([(name, "write", {"repository": str(root), "path": name + ".txt", "content": name})
                                           for name in ("one", "two")])
        self.assertEqual(len(first), 2)
        self.assertTrue(all(item.get("success") for item in results), results)
        self.assertEqual((root / "one.txt").read_text(), "one")
        self.assertEqual((root / "two.txt").read_text(), "two")

    async def test_real_commands_in_different_projects_overlap(self):
        # Each confined process accesses only its own project. The trusted
        # test host releases both barriers only after both have started.
        ready = [root / "fixture.ready" for root in self.roots]
        program = ("import pathlib,time; pathlib.Path('fixture.ready').touch(); deadline=time.monotonic()+15; "
                   "\nwhile not pathlib.Path('fixture.release').exists():\n if time.monotonic()>deadline: raise RuntimeError('commands did not overlap')\n time.sleep(.01)\nprint('overlapped')")
        items = [(str(index), "workspace_run", {"repository": str(root), "argv": [sys.executable, "-c", program,
                  ]}) for index, root in enumerate(self.roots)]
        async def release():
            async with asyncio.timeout(25):
                while not all(path.exists() for path in ready):
                    await asyncio.sleep(.02)
                for root in self.roots:
                    (root / "fixture.release").touch()
        results, _ = await asyncio.gather(self.run_calls(items), release())
        self.assertTrue(all(item.get("success") for item in results), results)
        self.assertTrue(all("overlapped" in item["content"] for item in results))

    async def test_approval_dialog_does_not_stop_independent_tool(self):
        root = self.roots[0]
        approved = asyncio.Event()
        entered = asyncio.Event()
        async def approve(request):
            entered.set()
            await asyncio.wait_for(approved.wait(), 8)
            return True
        def progress(event):
            if event["call_id"] == "query" and event["phase"] == "completed":
                approved.set()
        history = []
        def registry(*args):
            entries, act_web = build_local_registry(*args)
            original = entries["calculate_number"]
            async def calculate(action):
                await asyncio.wait_for(entered.wait(), 8)
                return await original.handler(action)
            entries["calculate_number"] = ToolRegistration(original.schema, original.capability, calculate, parallel_safe=True)
            return entries, act_web
        with patch("dianstar.app.runtime.build_local_registry", side_effect=registry), patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=with_review([calls(
                ("git", "mcp_git__git_create_branch", {"repository": str(root), "branch": "fixture"}),
                ("query", "calculate_number", {"expression": "1+1"})), reply("完成。")]))
            # Create an initial commit so branch preflight reaches its approval.
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                            "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
            await asyncio.wait_for(run_turn("创建分支并独立计算", env_file=self.env, project_directory=root,
                on_permission=approve, on_tool=progress, history=history), 20)
        self.assertTrue(entered.is_set())
        self.assertTrue(approved.is_set())
        self.assertTrue(all(not result.get("error") for result in workflow_results(history)), history)

    async def test_model_sees_complete_search_batch_before_generating_dependent_read(self):
        returned, progress, step = set(), [], 0
        slow_ready, fast_ready = asyncio.Event(), asyncio.Event()
        url = "https://example.test/returned-by-search"
        async def web_call(service, name, arguments):
            if name == "search_web":
                if arguments["query"] == "slow":
                    slow_ready.set()
                    await fast_ready.wait()
                else:
                    await slow_ready.wait()
                    fast_ready.set()
                returned.add(arguments["query"])
                return {"results": [{"url": url}], "content_kind": "search_summaries"}
            self.assertEqual(returned, {"fast", "slow"})
            self.assertEqual(arguments["url"], url)
            return {"url": url, "content": "fixture page", "content_kind": "page_body"}
        async def respond(**request):
            nonlocal step
            step += 1
            if step == 1:
                return calls(("slow", "search_web", {"query": "slow"}), ("fast", "search_web", {"query": "fast"}))
            self.assertEqual(returned, {"fast", "slow"})
            if step == 2:
                messages = [item for item in request["messages"] if item["role"] == "tool"]
                self.assertEqual({item["tool_call_id"] for item in messages}, {"fast", "slow"})
                target = json.loads(messages[0]["content"])["results"][0]["url"]
                return calls(("dependent", "read_webpage", {"url": target}))
            return reply("已读取搜索结果。")
        history = []
        with patch.object(WebTools, "execute", web_call), patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=respond)
            await asyncio.wait_for(run_turn("先搜索再读取结果", env_file=self.env, project_directory=self.roots[0],
                on_tool=progress.append, history=history), 20)
        self.assertEqual(step, 3)
        events = [(event["phase"], event["call_id"]) for event in progress]
        self.assertLess(events.index(("completed", "slow")), events.index(("started", "dependent")))
        self.assertLess(events.index(("completed", "fast")), events.index(("started", "dependent")))


class GitPlanConcurrencyTests(unittest.TestCase):
    def test_independent_git_previews_and_mutations_overlap_and_preserve_one_use_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            roots = [base / name for name in ("a", "b")]
            for root in roots:
                subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                                "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
            with patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(base / "state")}):
                workspace = GitWorkspace(roots, allow_writes=True)
                barrier, store = threading.Barrier(2, timeout=8), workspace.store_plan
                def save(plan):
                    barrier.wait()
                    return store(plan)
                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(workspace, "store_plan", save):
                    plans = list(pool.map(lambda root: workspace.prepare("git_create_branch", str(root), branch="fixture"), roots))
                self.assertTrue(all(item.get("prepared") for item in plans), plans)
                self.assertEqual(len(workspace.plans), 2)
                original = workspace._apply
                def apply(context, plan):
                    barrier.wait()
                    return original(context, plan)
                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(workspace, "_apply", apply):
                    results = list(pool.map(lambda plan: workspace.apply(plan["token"]), plans))
                self.assertTrue(all(item.get("success") for item in results), results)
                for root, plan in zip(roots, plans):
                    check = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "refs/heads/fixture"], capture_output=True)
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertIn("error", workspace.apply(plan["token"]))

    def test_repository_initialization_does_not_hold_global_metadata_lock_during_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            with patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(base / "state")}):
                workspace = GitWorkspace([], allow_writes=True, workspace_roots=[base])
                roots = [base / name for name in ("a", "b")]
                plans = [workspace.prepare("git_init", str(root), branch="main") for root in roots]
                self.assertTrue(all(item.get("prepared") for item in plans), plans)
                barrier, original = threading.Barrier(2, timeout=8), workspace._apply_bootstrap
                def apply(plan, root):
                    barrier.wait()
                    return original(plan, root)
                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(workspace, "_apply_bootstrap", apply):
                    results = list(pool.map(lambda plan: workspace.apply(plan["token"]), plans))
                self.assertTrue(all(item.get("success") for item in results), results)
                self.assertEqual(set(workspace.repositories), set(roots))
