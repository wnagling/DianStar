"""GitHub development workflows, official arguments, scope and exact approvals."""
from dataclasses import asdict, replace
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp.types import CallToolResult, TextContent, Tool

from dianstar.config.mcp import MCPSettings, normalize, save_mcp_settings, load_mcp_settings
from dianstar.app.runtime import run_turn
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway, ToolPreparationError
from dianstar.tools.mcp.client import MCPTools
from dianstar.tools.mcp.connection import MCPUnavailable
from dianstar.tools.mcp.github_adapter import GitHubAdapter
from dianstar.tools.mcp.github_contract import READ_TOOLS, WRITE_TOOLS, METHODS
from dianstar.tools.mcp.github_search import search_repositories
from dianstar.tools.mcp.launch import server_parameters
from dianstar.tools.mcp.results import normalize_result
from dianstar.tools.mcp.schemas import adapted_schema
from dianstar.security.validation import valid_arguments


def result(value):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))])


def tool(name, extra=None):
    properties = {"owner": {"type": "string"}, "repo": {"type": "string"}, **(extra or {})}
    if name == "search_code":
        properties = {"query": {"type": "string"}, "perPage": {"type": "integer"}}
    if name in METHODS:
        properties["method"] = {"type": "string", "enum": sorted(METHODS[name]) + ["not_allowed"]}
    return Tool(name=name, inputSchema={"type": "object", "properties": properties,
                "required": ["query"] if name == "search_code" else ["owner", "repo"]})


def official_tools():
    fixture = json.loads((Path(__file__).parent / "fixtures/github_mcp_v2_0_3.json").read_text(encoding="utf-8"))
    return [Tool(name=name, inputSchema=schema) for name, schema in fixture["schemas"].items()]


class OfficialGitHubSchemaTests(unittest.TestCase):
    def test_all_captured_official_tools_preserve_parameter_names_types_and_required_fields(self):
        for original in official_tools():
            with self.subTest(name=original.name):
                adapted = adapted_schema("github", original)["function"]["parameters"]
                self.assertEqual(set(adapted["properties"]), set(original.inputSchema["properties"]))
                self.assertEqual(set(adapted.get("required", [])), set(original.inputSchema.get("required", [])))
                for name, field in original.inputSchema["properties"].items():
                    self.assertEqual(adapted["properties"][name].get("type"), field.get("type"))

    def test_pr_title_empty_body_and_official_pending_review_methods_work(self):
        schemas = {tool.name: adapted_schema("github", tool) for tool in official_tools()}
        self.assertTrue(valid_arguments(schemas["create_pull_request"], {
            "owner": "owner", "repo": "repo", "head": "feature", "base": "main", "title": "Fix", "body": "", "draft": True}))
        for method in ("create", "submit_pending", "delete_pending", "resolve_thread", "unresolve_thread"):
            self.assertTrue(valid_arguments(schemas["pull_request_review_write"], {
                "owner": "owner", "repo": "repo", "pullNumber": 1, "method": method}))


class GitHubConfigurationTests(unittest.TestCase):
    def test_old_configuration_stays_public_read_only_and_new_scopes_round_trip(self):
        old = normalize({"github_repositories": ["Owner/Repo"]})
        self.assertEqual(old.github_private_repositories, ())
        self.assertEqual(old.github_write_repositories, ())
        configured = normalize({**asdict(old), "github_private_repositories": ["OWNER/REPO"],
                                "github_write_repositories": ["owner/repo"]})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mcp.json"
            self.assertEqual(save_mcp_settings(configured, path), load_mcp_settings(path))
        for field in ("github_private_repositories", "github_write_repositories"):
            for invalid in (["other/repo"], "owner/repo", [None]):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    normalize({**asdict(old), field: invalid})

    def test_server_write_mode_is_explicit_and_only_selects_the_approved_tool_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "server.exe"
            executable.touch()
            settings = MCPSettings(github_enabled=True, github_executable=str(executable),
                                   github_repositories=("owner/repo",))
            read = server_parameters(settings, root / ".env")["github"]
            self.assertIn("--read-only", read.args)
            self.assertEqual(set(read.env["GITHUB_TOOLS"].split(",")), READ_TOOLS)
            write = server_parameters(replace(settings, github_write_repositories=("owner/repo",)), root / ".env")["github"]
            self.assertNotIn("--read-only", write.args)
            self.assertEqual(write.env["GITHUB_READ_ONLY"], "0")
            self.assertEqual(set(write.env["GITHUB_TOOLS"].split(",")), READ_TOOLS | WRITE_TOOLS)
            self.assertNotIn("merge_pull_request", write.env["GITHUB_TOOLS"])

    def test_official_search_schema_has_no_invented_fields_and_optional_fields_stay_optional(self):
        original = tool("search_code")
        schema = adapted_schema("github", original)
        self.assertEqual(set(schema["function"]["parameters"]["properties"]), {"query", "perPage"})
        self.assertTrue(valid_arguments(schema, {"query": "repo:owner/repo ToolGateway", "perPage": 5}))
        self.assertFalse(valid_arguments(schema, {"query": "repo:owner/repo x", "owner": "owner"}))
        update = adapted_schema("github", tool("update_pull_request", {"body": {"type": "string"}}))
        self.assertTrue(valid_arguments(update, {"owner": "owner", "repo": "repo", "body": ""}))
        self.assertNotIn("owner", original.inputSchema["properties"])

    def test_both_batches_are_registered_with_method_intersections(self):
        for name in READ_TOOLS | WRITE_TOOLS:
            extra = {"query": {"type": "string"}} if name.startswith("search_") else {}
            schema = adapted_schema("github", tool(name, extra))
            with self.subTest(name=name):
                self.assertIsNotNone(schema)
                if name in METHODS:
                    self.assertEqual(set(schema["function"]["parameters"]["properties"]["method"]["enum"]), METHODS[name])
        self.assertIsNone(adapted_schema("github", tool("delete_repository")))


class GitHubSearchTests(unittest.TestCase):
    def test_scoped_boolean_queries_and_official_owner_repo_arguments(self):
        allowed = {"owner/repo", "other/allowed"}
        for query, expected in (
            ("repo:Owner/Repo ToolGateway", {"owner/repo"}),
            ('repo:"owner/repo" (foo OR bar)', {"owner/repo"}),
            ("(repo:owner/repo foo) OR (repo:other/allowed bar)", allowed),
            ("repo:owner/repo NOT failure -repo:unconfigured/repo", {"owner/repo"}),
        ):
            with self.subTest(query=query):
                self.assertEqual(search_repositories({"query": query}, allowed), expected)
        self.assertEqual(search_repositories({"owner": "owner", "repo": "repo", "query": "fix timeout"}, allowed), {"owner/repo"})

    def test_unbounded_or_negated_quoted_and_conflicting_scopes_are_rejected(self):
        for query in ("foo", "repo:owner/repo OR foo", '"repo:owner/repo" foo',
                      "NOT repo:owner/repo foo", "-repo:owner/repo foo", "repo:evil/repo x",
                      "repo:owner/repo (foo OR bar", "repo:owner/repo OR", 'repo:owner/repo "unterminated'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                search_repositories({"query": query}, {"owner/repo"})
        with self.assertRaises(ValueError):
            search_repositories({"owner": "owner", "repo": "repo", "query": "foo OR bar"}, {"owner/repo"})

    def test_search_result_scope_and_source_attribution(self):
        args = {"query": "repo:owner/repo function"}
        for repository, expected_error in (("owner/repo", None), ("other/repo", "mcp_repository_denied")):
            payload = {"items": [{"html_url": f"https://github.com/{repository}/blob/main/code.py",
                                  "repository": {"full_name": repository}}]}
            normalized = normalize_result(result(payload), "github", "search_code", args,
                                          Path("missing.env"), repositories={"owner/repo"})
            self.assertEqual(normalized.get("error"), expected_error)
            if not expected_error:
                self.assertEqual(normalized["sources"], [payload["items"][0]["html_url"]])


class GitHubWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.env = Path(self.temporary.name) / ".env"
        self.env.write_text("GITHUB_PERSONAL_ACCESS_TOKEN=fixture-github-token\n", encoding="utf-8")
        self.settings = MCPSettings(github_repositories=("owner/repo", "owner/read-only"),
                                   github_write_repositories=("owner/repo",))
        self.authorization = Authorization()
        self.tools = MCPTools(self.settings, self.env, self.authorization)
        self.call = AsyncMock(return_value=result({"html_url": "https://github.com/owner/repo/pull/1", "number": 1}))
        self.connection = SimpleNamespace(call=self.call, tools=[], changed=False, task=SimpleNamespace(done=lambda: False),
                                          start=AsyncMock(), close=AsyncMock())
        self.tools.connections["github"] = self.connection
        self.public_patch = patch.object(self.tools.github_writes, "public_check", AsyncMock(return_value=True))
        self.public_patch.start()
        self.addCleanup(self.public_patch.stop)
        self.tools.github.request_json = AsyncMock(return_value={"number": 1, "title": "old", "body": "before", "state": "open"})

    async def register(self, name, extra):
        self.connection.tools = [tool(name, extra)]
        params = {"github": object()}
        with patch("dianstar.tools.mcp.client.server_parameters", return_value=params), \
                patch("dianstar.tools.mcp.client.StdioConnection", return_value=self.connection):
            await self.tools.__aenter__()
        return self.tools.registry

    async def test_private_repository_access_is_explicit_and_checks_credentials(self):
        adapter = GitHubAdapter(replace(self.settings, github_private_repositories=("owner/repo",)), self.env)
        public = AsyncMock(return_value=False)
        adapter.request_json = AsyncMock(return_value={"full_name": "Owner/Repo", "private": True})
        args = {"owner": "owner", "repo": "repo", "method": "get"}
        self.assertIsNone(await adapter.validate_read("issue_read", args, public))
        public.assert_not_awaited()
        adapter.request_json.assert_awaited_once_with("/repos/owner/repo")
        adapter.settings = replace(adapter.settings, github_private_repositories=())
        self.assertEqual((await adapter.validate_read("issue_read", args, public))["error"], "mcp_public_repository_unverified")
        self.assertEqual((await adapter.validate_read("issue_read", {**args, "repo": "outside"}, public))["error"], "mcp_repository_denied")

    async def test_private_access_needs_token_and_verified_target(self):
        adapter = GitHubAdapter(replace(self.settings, github_private_repositories=("owner/repo",)), self.env)
        args = {"owner": "owner", "repo": "repo", "method": "get"}
        with patch.object(adapter, "token", return_value=None):
            self.assertEqual((await adapter.validate_read("issue_read", args, AsyncMock()))["error"], "github_token_missing")
        adapter.request_json = AsyncMock(return_value={"full_name": "other/repo", "private": True})
        self.assertEqual((await adapter.validate_read("issue_read", args, AsyncMock()))["error"], "mcp_repository_denied")

    async def test_search_tree_ci_and_added_pr_methods_execute_without_write_approval(self):
        self.connection.tools = official_tools()
        with patch("dianstar.tools.mcp.client.server_parameters", return_value={"github": object()}), \
                patch("dianstar.tools.mcp.client.StdioConnection", return_value=self.connection):
            await self.tools.__aenter__()
        approve = AsyncMock()
        gateway = ToolGateway(self.authorization, self.tools.registry, approve)
        common = {"owner": "owner", "repo": "repo"}
        cases = [("get_repository_tree", {**common, "recursive": True}),
                 ("search_code", {"query": "repo:owner/repo code", "perPage": 5}),
                 ("search_issues", {**common, "query": "timeout"}),
                 ("search_pull_requests", {"query": "repo:owner/repo fix"}),
                 ("get_job_logs", {**common, "job_id": 1, "return_content": True}),
                 ("pull_request_read", {**common, "pullNumber": 1, "method": "get_commits"}),
                 ("pull_request_read", {**common, "pullNumber": 1, "method": "get_check_runs"})]
        for method in METHODS["actions_list"]:
            cases.append(("actions_list", {**common, "method": method, "resource_id": "1"}))
        for method in METHODS["actions_get"]:
            cases.append(("actions_get", {**common, "method": method, "resource_id": "1"}))
        with patch.object(self.tools, "_public_repository", AsyncMock(return_value=True)):
            for name, args in cases:
                with self.subTest(name=name, args=args):
                    response = await gateway.execute(ToolAction("mcp_github__" + name, args))
                    self.assertNotIn("error", response.result)
                    self.call.assert_awaited_with(name, args)
        approve.assert_not_awaited()

    async def test_metadata_requests_are_fixed_host_no_redirect_and_do_not_expose_http_errors(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={"full_name": "owner/repo", "private": True})
        original = httpx.AsyncClient
        adapter = GitHubAdapter(self.settings, self.env)
        with patch("dianstar.tools.mcp.github_adapter.httpx.AsyncClient",
                   side_effect=lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)):
            self.assertEqual((await adapter.request_json("/repos/owner/repo"))["full_name"], "owner/repo")
        self.assertEqual(str(seen[0].url), "https://api.github.com/repos/owner/repo")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer fixture-github-token")

    async def test_write_tools_are_hidden_when_not_enabled_and_read_path_cannot_execute_them(self):
        self.tools.settings = replace(self.settings, github_write_repositories=())
        self.assertEqual(await self.register("update_pull_request", {"pullNumber": {"type": "integer"}}), {})
        response = await self.tools.execute("github", "update_pull_request", {"owner": "owner", "repo": "repo", "pullNumber": 1})
        self.assertEqual(response["error"], "mcp_tool_denied")
        self.call.assert_not_awaited()

    async def test_approval_displays_before_and_after_and_every_repeat_needs_confirmation(self):
        registry = await self.register("update_pull_request", {"pullNumber": {"type": "integer"}, "body": {"type": "string"}})
        approve = AsyncMock(side_effect=[True, True])
        gateway = ToolGateway(self.authorization, registry, approve)
        action = ToolAction("mcp_github__update_pull_request", {"owner": "owner", "repo": "repo", "pullNumber": 1, "body": ""})
        for _ in range(2):
            self.assertNotIn("error", (await gateway.execute(action)).result)
        self.assertEqual(approve.await_count, 2)
        self.assertEqual(self.call.await_count, 2)
        preview = approve.await_args.args[0]["description"]
        self.assertIn("before", preview)
        self.assertIn('"body": ""', preview)
        self.call.assert_awaited_with("update_pull_request", action.arguments)

    async def test_decline_missing_ui_wrong_repository_and_revocation_do_not_write(self):
        registry = await self.register("add_issue_comment", {"issue_number": {"type": "integer"}, "body": {"type": "string"}})
        action = ToolAction("mcp_github__add_issue_comment", {"owner": "owner", "repo": "repo", "issue_number": 1, "body": "comment"})
        self.assertIn("error", (await ToolGateway(self.authorization, registry).execute(action)).result)
        approve = AsyncMock(return_value=False)
        self.assertIn("error", (await ToolGateway(self.authorization, registry, approve).execute(action)).result)
        invalid = ToolAction(action.name, {**action.arguments, "repo": "read-only"})
        approve = AsyncMock(return_value=True)
        self.assertEqual((await ToolGateway(self.authorization, registry, approve).execute(invalid)).result["error"], "github_write_disabled")
        approve.assert_not_awaited()
        async def revoke(_):
            self.authorization.revoke("mcp.github.add_issue_comment")
            return True
        self.assertEqual((await ToolGateway(self.authorization, registry, revoke).execute(action)).result["error"], "authorization_changed")
        self.call.assert_not_awaited()

    async def test_changed_target_or_expired_preview_blocks_execution(self):
        args = {"owner": "owner", "repo": "repo", "pullNumber": 1, "body": "new"}
        writes = self.tools.github_writes
        await writes.prepare("update_pull_request", args)
        self.tools.github.request_json.return_value = {"number": 1, "title": "changed"}
        self.assertEqual((await writes.apply("update_pull_request", args))["error"], "github_state_changed")
        self.assertEqual((await writes.apply("update_pull_request", args))["error"], "github_not_prepared")
        await writes.prepare("update_pull_request", args)
        with patch("dianstar.tools.mcp.github_writes.time.monotonic", return_value=10**12):
            self.assertEqual((await writes.apply("update_pull_request", args))["error"], "github_not_prepared")
        self.call.assert_not_awaited()

    async def test_timeout_is_unknown_and_not_automatically_retried(self):
        args = {"owner": "owner", "repo": "repo", "method": "create", "title": "new issue"}
        await self.tools.github_writes.prepare("issue_write", args)
        self.call.side_effect = MCPUnavailable()
        self.assertEqual((await self.tools.github_writes.apply("issue_write", args))["error"], "github_outcome_unknown")
        with self.assertRaises(ToolPreparationError):
            await self.tools.github_writes.prepare("issue_write", args)
        self.call.assert_awaited_once()

    async def test_cross_repository_parent_and_reply_targets_are_checked(self):
        args = {"owner": "owner", "repo": "repo", "method": "create", "parent_issue_number": 2,
                "parent_owner": "other", "parent_repo": "private"}
        with self.assertRaises(ToolPreparationError):
            await self.tools.github_writes.prepare("issue_write", args)
        self.tools.github.request_json.return_value = {"pull_request_url": "https://api.github.com/owner/other/pulls/2"}
        with self.assertRaises(ToolPreparationError):
            await self.tools.github_writes.prepare("add_reply_to_pull_request_comment", {
                "owner": "owner", "repo": "repo", "commentId": 1, "pullNumber": 2, "body": "reply"})
        self.call.assert_not_awaited()

    async def test_create_draft_pr_issue_review_and_inline_comment_reach_official_tools(self):
        cases = [
            ("create_pull_request", {"head": "feature", "base": "main", "title": "fix", "draft": True}),
            ("issue_write", {"method": "create", "title": "bug", "body": "steps"}),
            ("issue_write", {"method": "update", "issue_number": 1, "state": "closed"}),
            ("pull_request_review_write", {"method": "create", "pullNumber": 1, "event": "COMMENT", "body": "review"}),
            ("pull_request_review_write", {"method": "submit_pending", "pullNumber": 1, "event": "REQUEST_CHANGES", "body": "fix"}),
            ("pull_request_review_write", {"method": "delete_pending", "pullNumber": 1}),
            ("add_comment_to_pending_review", {"pullNumber": 1, "path": "app.py", "line": 3, "side": "RIGHT", "subjectType": "LINE", "body": "check"}),
            ("add_reply_to_pull_request_comment", {"commentId": 1, "pullNumber": 1, "body": "fixed"}),
        ]
        async def read(path, params=None):
            if path == "/user":
                return {"login": "fixture"}
            if "/commits/" in path:
                return {"sha": "a" * 40}
            if path.endswith("/reviews"):
                return [{"id": 1, "state": "PENDING", "user": {"login": "fixture"}, "body": "review"}]
            if path.endswith("/pulls") or path.endswith("/comments"):
                return []
            if "/pulls/comments/" in path:
                return {"pull_request_url": "https://api.github.com/repos/owner/repo/pulls/1", "body": "old"}
            return {"number": 1, "title": "current"}
        self.tools.github.request_json.side_effect = read
        for name, supplied in cases:
            with self.subTest(name=name, supplied=supplied):
                args = {"owner": "owner", "repo": "repo", **supplied}
                await self.tools.github_writes.prepare(name, args)
                self.assertNotIn("error", await self.tools.github_writes.apply(name, args))
                self.call.assert_awaited_with(name, args)

    async def test_review_thread_resolution_checks_repository_and_pr(self):
        args = {"owner": "owner", "repo": "repo", "pullNumber": 1, "method": "resolve_thread", "threadId": "PRRT_fixture"}
        self.tools.github.review_thread = AsyncMock(return_value={"id": "PRRT_fixture", "isResolved": False,
            "pullRequest": {"number": 2, "repository": {"nameWithOwner": "owner/repo"}}})
        with self.assertRaises(ToolPreparationError):
            await self.tools.github_writes.prepare("pull_request_review_write", args)
        self.tools.github.review_thread.return_value["pullRequest"]["number"] = 1
        await self.tools.github_writes.prepare("pull_request_review_write", args)
        self.assertNotIn("error", await self.tools.github_writes.apply("pull_request_review_write", args))

    async def test_full_turn_search_ci_pr_issue_and_review_uses_official_arguments_and_history(self):
        self.connection.tools = official_tools()
        self.call.return_value = result({"number": 1, "html_url": "https://github.com/owner/repo/pull/1"})
        common = {"owner": "owner", "repo": "repo"}
        decisions = [
            ("search_code", {"query": "repo:owner/repo ToolGateway", "perPage": 5}),
            ("actions_list", {**common, "method": "list_workflow_runs"}),
            ("actions_list", {**common, "method": "list_workflow_jobs", "resource_id": "10"}),
            ("get_job_logs", {**common, "job_id": 20, "return_content": True, "tail_lines": 50}),
            ("create_pull_request", {**common, "head": "feature", "base": "main", "title": "Fix", "draft": True}),
            ("issue_write", {**common, "method": "create", "title": "Follow up"}),
            ("pull_request_review_write", {**common, "method": "create", "pullNumber": 1}),
            ("add_comment_to_pending_review", {**common, "pullNumber": 1, "path": "app.py", "line": 3,
                "side": "RIGHT", "subjectType": "LINE", "body": "Review comment"}),
            ("add_reply_to_pull_request_comment", {**common, "pullNumber": 1, "commentId": 1, "body": "Fixed"}),
            ("pull_request_review_write", {**common, "method": "submit_pending", "pullNumber": 1, "event": "COMMENT"}),
            ("update_pull_request", {**common, "pullNumber": 1, "body": "Verified"}),
        ]
        responses = [SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=f"call-{index}",
                function=SimpleNamespace(name="mcp_github__" + name, arguments=json.dumps(arguments)))]))])
            for index, (name, arguments) in enumerate(decisions)]
        responses.append(SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content="开发协作流程已完成。"))]))
        async def metadata(path, params=None):
            if path == "/user":
                return {"login": "fixture"}
            if "/commits/" in path:
                return {"sha": "a" * 40}
            if path.endswith("/reviews"):
                return [{"id": 1, "state": "PENDING", "user": {"login": "fixture"}, "body": "review"}]
            if path.endswith("/pulls") or path.endswith("/comments"):
                return []
            if "/pulls/comments/" in path:
                return {"pull_request_url": "https://api.github.com/repos/owner/repo/pulls/1", "body": "old"}
            return {"number": 1, "title": "current"}
        save_mcp_settings(replace(self.settings, github_enabled=True,
            github_executable=str(self.env.parent / "server.exe")), self.env.parent / ".local/mcp.json")
        self.env.write_text(self.env.read_text(encoding="utf-8") + "DEEPSEEK_API_KEY=fixture-model-key\n", encoding="utf-8")
        approve = AsyncMock(return_value=True)
        history = []
        with patch("dianstar.providers.deepseek.AsyncOpenAI") as client_class, \
                patch("dianstar.tools.mcp.client.server_parameters", return_value={"github": object()}), \
                patch("dianstar.tools.mcp.client.StdioConnection", return_value=self.connection), \
                patch.object(MCPTools, "_public_repository", AsyncMock(return_value=True)), \
                patch.object(GitHubAdapter, "request_json", AsyncMock(side_effect=metadata)):
            create = client_class.return_value.chat.completions.create = AsyncMock(side_effect=responses)
            answer = await run_turn("完成开发协作流程", env_file=self.env, history=history, on_permission=approve)
        self.assertEqual(create.await_count, 12)
        messages = [message for message in history if message["role"] == "tool"]
        self.assertEqual(len(messages), len(decisions))
        errors = [(decisions[index][0], json.loads(message["content"])) for index, message in enumerate(messages)
                  if "error" in json.loads(message["content"])]
        self.assertEqual(errors, [])
        self.assertEqual(approve.await_count, 7)
        self.assertEqual([(call.args[0], call.args[1]) for call in self.call.await_args_list], decisions)
        self.assertEqual(history[-1]["content"], answer)

    async def test_cross_fork_pr_preview_resolves_actual_compare_api_head(self):
        args = {"owner": "owner", "repo": "repo", "head": "contributor:feature", "base": "main", "title": "Fix"}
        for total, expected in ((0, "ancestor"), (1, "first"), (2, "last")):
            async def read(path, params=None):
                if "/compare/" in path:
                    commits = [] if total == 0 else [{"sha": "last" if params.get("page") == 2 else "first"}]
                    return {"base_commit": {"sha": "base"}, "merge_base_commit": {"sha": "ancestor"},
                            "total_commits": total, "commits": commits}
                return []
            with self.subTest(total=total):
                self.tools.github.request_json.side_effect = read
                preview = await self.tools.github_writes.prepare("create_pull_request", args)
                self.assertEqual(json.loads(preview)["当前状态"]["commits"], {"base": "base", "head": expected})
                self.assertNotIn("error", await self.tools.github_writes.apply("create_pull_request", args))

    async def test_pending_review_and_comments_are_paginated_and_shown_before_submit(self):
        args = {"owner": "owner", "repo": "repo", "pullNumber": 1, "method": "submit_pending", "event": "COMMENT"}
        async def read(path, params=None):
            if path == "/user":
                return {"login": "fixture"}
            if path.endswith("/reviews"):
                if params["page"] == 1:
                    return [{"id": index, "state": "COMMENTED", "user": {"login": "other"}} for index in range(100)]
                return [{"id": 101, "state": "PENDING", "user": {"login": "fixture"}, "body": "draft review"}]
            if path.endswith("/comments"):
                if params["page"] == 1:
                    return [{"id": index, "body": "pending comment", "path": "app.py", "line": index} for index in range(100)]
                return [{"id": 100, "body": "last pending comment"}]
            return {"number": 1, "head": {"sha": "head"}, "title": "PR"}
        self.tools.github.request_json.side_effect = read
        preview = await self.tools.github_writes.prepare("pull_request_review_write", args)
        self.assertIn("last pending comment", preview)
        self.assertEqual(len(json.loads(preview)["当前状态"]["pending_review"]["comments"]), 101)
        self.assertNotIn("error", await self.tools.github_writes.apply("pull_request_review_write", args))
        self.tools.github.request_json.side_effect = lambda path, params=None: {"login": "fixture"} if path == "/user" else [] if path.endswith("/reviews") else {"number": 1}
        with self.assertRaises(ToolPreparationError):
            await self.tools.github_writes.prepare("pull_request_review_write", args)
