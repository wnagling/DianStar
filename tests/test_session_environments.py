"""Durable environment isolation, CAS, approvals and real command reuse."""
import asyncio
from copy import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.app.tool_resources import ToolResources
from dianstar.app.turn_log import TurnLog
from dianstar.core.resources import conflicts
from dianstar.core.types import ToolAction
from dianstar.platform.command_environment import profile_environment
from dianstar.security.authorization import Authorization
from dianstar.security.content_guard import ContentScanner
from dianstar.security.gateway import ToolGateway
from dianstar.security.tool_policy import ToolPolicy
from dianstar.sessions.environments import EnvironmentProfileError, EnvironmentStore
from dianstar.tools.local.workspace import WorkspaceTools
from tests import test_file_tools as fixtures


class SessionEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.FileToolTests.setUp(self)
        environment = patch.dict(os.environ, {"DIANSTAR_FILE_STATE_ROOT": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.bind("fixture-session-a")

    def bind(self, identifier):
        self.mcp.authorization = Authorization()
        self.tools = WorkspaceTools(self.mcp, self.env, conversation_id=identifier)
        self.registry = self.tools.registrations()
        self.gateway = ToolGateway(self.mcp.authorization, self.registry, self.approve)

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {"repository": str(self.root), **args}))).result

    async def save(self, revision=0, **values):
        return await self.call("workspace_environment_set", expected_revision=revision,
            environment=values.get("environment", {"JAVA_HOME": "fixture-saved-jdk"}), path_prepend=values.get("path_prepend", []))

    async def native(self, **extra):
        return await self.call("workspace_run", argv=[sys.executable, "-c", "import os;print(os.environ.get('JAVA_HOME', 'unset'))"], **extra)

    async def test_persistence_across_turn_objects_and_conversation_and_project_isolation(self):
        self.assertEqual((await self.save())["revision"], 1)
        self.bind("fixture-session-a")  # Re-created workspace/authorization, like the next turn.
        self.assertEqual((await self.native())["content"].strip(), "fixture-saved-jdk")
        self.bind("fixture-session-b")
        self.assertFalse((await self.call("workspace_environment_get"))["configured"])
        other = self.base / "other"
        other.mkdir()
        self.mcp.git.repositories.add(str(other))
        self.bind("fixture-session-a")
        result = await self.gateway.execute(ToolAction("workspace_environment_get", {"repository": str(other)}))
        self.assertFalse(result.result["configured"])
        self.assertTrue((await self.call("workspace_environment_get"))["configured"])

    async def test_clear_uses_tombstone_revision_and_does_not_restore_old_value(self):
        await self.save()
        with patch.dict(os.environ, {"JAVA_HOME": "fixture-host-jdk"}):
            self.assertEqual((await self.native(environment={"JAVA_HOME": "fixture-once-jdk"}))["content"].strip(), "fixture-once-jdk")
            self.assertEqual((await self.native())["content"].strip(), "fixture-saved-jdk")
            cleared = await self.call("workspace_environment_clear", expected_revision=1)
            self.assertEqual(cleared["revision"], 2)
            self.assertEqual((await self.native())["content"].strip(), "fixture-host-jdk")
            self.assertEqual(os.environ["JAVA_HOME"], "fixture-host-jdk")
        stale = await self.save(revision=0)
        self.assertEqual(stale["error"], "project_environment_changed")
        self.assertEqual((await self.save(revision=2))["revision"], 3)

    async def test_no_anonymous_global_profile_and_no_model_supplied_conversation_identity(self):
        self.bind(None)
        self.assertNotIn("workspace_environment_set", self.registry)
        self.assertTrue((await self.native())["success"])
        self.bind("fixture-session-a")
        result = await self.call("workspace_environment_get", conversation_id="fixture-session-b")
        self.assertEqual(result["error"], "invalid_tool_arguments")

    async def test_new_process_reopens_same_database_and_no_per_conversation_config_file(self):
        await self.save()
        program = ("import sys,json;from pathlib import Path;from dianstar.sessions.environments import EnvironmentStore;"
            "print(json.dumps(EnvironmentStore('fixture-session-a').load(Path(sys.argv[1]))))")
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, str(self.root)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["environment"], {"JAVA_HOME": "fixture-saved-jdk"})
        self.assertFalse(list((self.base / "state").glob("*.json")))

    async def test_read_only_retains_query_but_removes_environment_mutations(self):
        await self.save()
        self.mcp.settings = replace(self.mcp.settings, workspace_write_enabled=False)
        self.bind("fixture-session-a")
        self.assertIn("workspace_environment_get", self.registry)
        self.assertNotIn("workspace_environment_set", self.registry)
        self.assertNotIn("workspace_environment_clear", self.registry)
        self.assertTrue((await self.call("workspace_environment_get"))["configured"])

    async def test_sensitive_and_startup_variables_and_full_path_are_rejected_before_approval(self):
        for environment in ({"JAVA_HOME": "fixture-application-key"}, {"TOKEN": "fixture-fake-value"},
                            {"BASH_ENV": "startup.sh"}, {"PATH": str(self.root)}):
            result = await self.save(environment=environment)
            self.assertIn("error", result)
            self.assertNotIn("fixture-application-key", json.dumps(result))
        self.approve.assert_not_awaited()
        self.assertFalse((await self.call("workspace_environment_get"))["configured"])

    async def test_missing_search_directory_fails_but_profile_can_be_queried_and_cleared(self):
        sdk = self.base / "sdk"
        sdk.mkdir()
        self.assertTrue((await self.save(path_prepend=[str(sdk)]))["success"])
        sdk.rmdir()
        self.assertEqual((await self.native())["error"], "project_environment_stale")
        profile = await self.call("workspace_environment_get")
        self.assertFalse(profile["valid"])
        self.assertTrue((await self.call("workspace_environment_clear", expected_revision=profile["revision"]))["success"])

    async def test_denial_disabled_policy_and_revocation_cannot_write(self):
        self.approve.return_value = False
        self.assertEqual((await self.save())["error"], "authorization_required")
        for policy in ({"workspace.write": ToolPolicy(enabled=False)}, {"workspace_run": ToolPolicy(enabled=False)}):
            self.gateway = ToolGateway(self.mcp.authorization, self.registry, self.approve, policies=policy)
            self.assertEqual((await self.save())["error"], "tool_disabled")
        async def revoke(request):
            self.mcp.authorization.deny("workspace.write")
            return True
        self.gateway = ToolGateway(self.mcp.authorization, self.registry, revoke)
        self.assertEqual((await self.save())["error"], "authorization_changed")
        self.assertEqual(self.tools.environments.store.load(self.root)["revision"], 0)

    async def test_changed_while_waiting_approval_does_not_overwrite_and_commands_do_not_run(self):
        store = self.tools.environments.store
        async def change(request):
            store.replace(self.root, 0, {"environment": {"JAVA_HOME": "fixture-newer-jdk"}, "path_prepend": []})
            return True
        self.gateway = ToolGateway(self.mcp.authorization, self.registry, change)
        self.assertEqual((await self.save())["error"], "project_environment_changed")
        async def clear(request):
            store.replace(self.root, 1, None)
            return True
        self.gateway = ToolGateway(self.mcp.authorization, self.registry, clear)
        result = await self.call("workspace_run", argv=[sys.executable, "-c", "from pathlib import Path;Path('must-not-exist').touch()"])
        self.assertEqual(result["error"], "workspace_command_changed")
        self.assertFalse((self.root / "must-not-exist").exists())

    async def test_saved_profile_is_not_a_command_grant_and_approval_text_explains_scope(self):
        await self.save()
        self.assertIn("当前会话", self.approve.call_args.args[0]["summary"])
        self.assertIn("fixture-saved-jdk", self.approve.call_args.args[0]["summary"])
        self.approve.reset_mock()
        self.approve.return_value = False
        result = await self.native()
        self.assertEqual(result["error"], "authorization_required")
        self.approve.assert_awaited_once()
        self.assertIn("会话为此项目保存", self.approve.call_args.args[0]["summary"])

    async def test_profile_is_rechecked_after_waiting_for_repository_lock(self):
        await self.save()
        original = self.tools.files.exclusive_operation
        @asynccontextmanager
        async def changed_after_lock(root):
            async with original(root):
                self.tools.environments.store.replace(root, 1, None)
                yield
        with patch.object(self.tools.files, "exclusive_operation", side_effect=changed_after_lock):
            result = await self.native()
        self.assertEqual(result["error"], "workspace_command_changed")

    async def test_bash_uses_saved_profile_and_shell_export_does_not_modify_it(self):
        from dianstar.platform.shells import find_bash, find_powershell
        try:
            find_powershell() if os.name == "nt" else find_bash()
        except ValueError:
            self.skipTest("Actual Bash unavailable")
        await self.save()
        first = await self.call("shell" if os.name == "nt" else "bash", command="Write-Output $env:JAVA_HOME" if os.name == "nt" else "printf '%s' \"$JAVA_HOME\"")
        self.assertTrue(first.get("success"), first)
        self.assertEqual(first["content"].strip(), "fixture-saved-jdk")
        second = await self.call("shell" if os.name == "nt" else "bash", command="$env:JAVA_HOME='fixture-shell-only'; Write-Output $env:JAVA_HOME" if os.name == "nt" else "export JAVA_HOME=fixture-shell-only; printf '%s' \"$JAVA_HOME\"")
        self.assertEqual(second["content"].strip(), "fixture-shell-only")
        self.assertEqual((await self.call("workspace_environment_get"))["environment"]["JAVA_HOME"], "fixture-saved-jdk")

    async def test_existing_directory_path_prefix_is_not_a_host_path_snapshot(self):
        sdk = self.base / "sdk"
        sdk.mkdir()
        await self.save(path_prepend=[str(sdk), str(sdk)])
        profile = await self.call("workspace_environment_get")
        self.assertEqual(profile["path_prepend"], [str(sdk)])
        self.assertNotIn("PATH", profile["environment"])
        scanner = ContentScanner()
        with patch.dict(os.environ, {"PATH": str(self.root)}):
            env = profile_environment(scanner, profile, {})
            self.assertEqual(env["PATH"].split(os.pathsep), [str(sdk), str(self.root)])
            explicit = profile_environment(scanner, profile, {"PATH": str(self.base)})
            self.assertEqual(explicit["PATH"], str(self.base))
        with patch.dict(os.environ, {"PATH": str(self.base)}):
            self.assertTrue(profile_environment(scanner, profile, {})["PATH"].endswith(str(self.base)))

    async def test_unsafe_paths_rejected_and_project_cannot_escape_scope(self):
        for value in ("relative/sdk", str(self.base / "missing"), str(self.base) + os.pathsep + str(self.root)):
            self.assertIn("error", await self.save(path_prepend=[value]))
        result = await self.gateway.execute(ToolAction("workspace_environment_get", {"repository": str(self.base)}))
        self.assertIn("error", result.result)
        self.approve.assert_not_awaited()

    async def test_corrupt_database_record_fails_closed_without_leaking_payload(self):
        await self.save()
        with closing(sqlite3.connect(self.base / "state/tasks.sqlite3")) as connection, connection:
            connection.execute("UPDATE command_environments SET payload=?", (b"fixture-damaged-payload",))
        self.assertEqual((await self.native())["error"], "project_environment_unavailable")
        self.assertEqual((await self.call("workspace_environment_get"))["error"], "project_environment_unavailable")

    async def test_profile_with_newly_known_credential_is_not_reused_or_returned(self):
        await self.save()
        self.env.write_text("API_KEY=fixture-saved-jdk\n", encoding="utf-8")
        self.bind("fixture-session-a")
        for name in ("workspace_environment_get", "workspace_run"):
            args = {} if name.endswith("get") else {"argv": [sys.executable, "-c", "print('must not execute')"]}
            result = await self.call(name, **args)
            self.assertEqual(result["error"], "sensitive_content_denied")
            self.assertNotIn("fixture-saved-jdk", json.dumps(result))
        self.assertTrue((await self.call("workspace_environment_clear", expected_revision=1))["success"])

    async def test_concurrent_first_open_logging_and_environment_cas_have_one_winner(self):
        barrier = threading.Barrier(6)
        store = self.tools.environments.store
        def write(index):
            barrier.wait(timeout=5)
            if index % 2:
                log = TurnLog(f"fixture-task-{index}", ContentScanner(), conversation_id="fixture-session-a")
                log.append({"event": "fixture"})
                return "logged"
            try:
                store.replace(self.root, 0, {"environment": {"JAVA_HOME": f"fixture-jdk-{index}"}, "path_prepend": []})
                return "saved"
            except EnvironmentProfileError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=6) as executor:
            result = list(executor.map(write, range(6)))
        self.assertEqual(result.count("saved"), 1, result)
        self.assertEqual(result.count("project_environment_changed"), 2, result)
        self.assertEqual(result.count("logged"), 3, result)
        self.assertEqual(store.load(self.root)["revision"], 1)

    async def test_saved_environment_tools_are_ordered_against_commands_in_one_batch(self):
        resources = ToolResources(self.registry, lambda: self.tools, self.mcp)
        command = resources(ToolAction("workspace_run", {"repository": str(self.root)}))
        for name in ("workspace_environment_get", "workspace_environment_set", "workspace_environment_clear"):
            self.assertTrue(conflicts(command, resources(ToolAction(name, {"repository": str(self.root)}))))

    async def test_concurrent_conversations_use_their_own_saved_environment(self):
        gateways = []
        for identifier, value in (("fixture-session-a", "fixture-jdk-a"), ("fixture-session-b", "fixture-jdk-b")):
            mcp = copy(self.mcp)
            mcp.authorization = Authorization()
            tools = WorkspaceTools(mcp, self.env, conversation_id=identifier)
            gateway = ToolGateway(mcp.authorization, tools.registrations(), AsyncMock(return_value=True))
            saved = await gateway.execute(ToolAction("workspace_environment_set", {"repository": str(self.root),
                "environment": {"JAVA_HOME": value}, "path_prepend": [], "expected_revision": 0}))
            self.assertTrue(saved.result.get("success"), saved.result)
            gateways.append(gateway)
        action = ToolAction("workspace_run", {"repository": str(self.root), "argv": [sys.executable, "-c",
            "import os,time;time.sleep(.1);print(os.environ['JAVA_HOME'])"]})
        results = await asyncio.gather(*(gateway.execute(action) for gateway in gateways))
        self.assertEqual([result.result["content"].strip() for result in results], ["fixture-jdk-a", "fixture-jdk-b"])

    async def test_real_jdk_saved_once_reused_without_command_environment_then_clear(self):
        compiler = shutil.which("javac")
        if not compiler:
            self.skipTest("Actual JDK unavailable")
        sdk = Path(compiler).resolve().parent
        (self.root / "Main.java").write_text('public class Main { public static void main(String[] a) { System.out.println(42); }}')
        self.assertTrue((await self.save(environment={"JAVA_HOME": str(sdk.parent)}, path_prepend=[str(sdk)]))["success"])
        with patch.dict(os.environ, {"PATH": str(self.root)}):
            self.bind("fixture-session-a")
            compiled = await self.call("workspace_run", argv=["javac", "Main.java"])
            if os.name == "nt" and compiled.get("error") == "command_runtime_inaccessible":
                from dianstar.platform.windows_acl import WindowsSecurity
                self.assertFalse(WindowsSecurity().can_edit(sdk.parent))
                self.assertIn("未执行", compiled["message"])
                self.assertFalse((self.root / "Main.class").exists())
                self.assertEqual((await self.call("workspace_environment_get"))["revision"], 1)
            else:
                self.assertTrue(compiled.get("success"), compiled)
                self.assertEqual(Path(compiled["executable"]), Path(compiler).resolve())
                self.assertEqual(compiled["environment_scope"], "conversation_project")
                ran = await self.call("workspace_run", argv=["java", "-cp", ".", "Main"])
                self.assertEqual(ran["content"].strip(), "42", ran)
            self.bind("fixture-session-b")
            self.assertEqual((await self.call("workspace_run", argv=["javac", "-version"]))["error"], "executable_not_found")
            self.bind("fixture-session-a")
            await self.call("workspace_environment_clear", expected_revision=1)
            self.assertEqual((await self.call("workspace_run", argv=["javac", "-version"]))["error"], "executable_not_found")


class RuntimeEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_passes_same_conversation_id_across_two_requests(self):
        from tests import test_orchestration as orchestration
        from tests.test_permissions import reply
        from dianstar.app.runtime import run_turn
        orchestration.OrchestrationTests.setUp(self)
        responses = [
            orchestration.calls(("save", "workspace_environment_set", {"repository": str(self.root),
                "expected_revision": 0, "environment": {"JAVA_HOME": "fixture-runtime-jdk"}, "path_prepend": []})),
            reply("开发环境已保存。"),
            orchestration.calls(("run", "workspace_run", {"repository": str(self.root), "argv": [sys.executable,
                "-c", "import os;print(os.environ['JAVA_HOME'])"]})),
            reply("环境复用完成。")]
        first, second = [], []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as factory:
            factory.return_value.chat.completions.create = AsyncMock(side_effect=responses)
            for history in (first, second):
                await run_turn("保存或复用本会话项目环境", env_file=self.env, project_directory=self.root,
                    conversation_id="fixture-runtime-session", history=history, on_permission=AsyncMock(return_value=True))
        tools = [json.loads(message["content"]) for message in second if message["role"] == "tool"]
        self.assertTrue(any(item.get("content", "").strip() == "fixture-runtime-jdk" for item in tools), tools)
