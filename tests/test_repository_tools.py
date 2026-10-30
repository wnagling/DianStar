"""Public tool boundaries for project files, configuration and GitHub administration."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import httpx

from dianstar.app.mcp_configuration import registrations
from dianstar.config.mcp import MCPSettings, load_mcp_settings
from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.github_admin import GitHubAdmin
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.app.runtime import run_turn


class ConfigurationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_requires_confirmation_and_reconnects_with_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.local/mcp.json'
            reconnect = AsyncMock()
            auth = Authorization()
            gateway = ToolGateway(auth, registrations(path, reconnect), AsyncMock(return_value=True))
            before = await gateway.execute(ToolAction('get_mcp_configuration', {}))
            self.assertFalse(before.result['exists'])
            result = await gateway.execute(ToolAction('configure_mcp', {'changes': {'git_enabled': True, 'git_workspace_roots': [tmp]}}))
            self.assertTrue(result.result['configuration_saved'])
            self.assertTrue(load_mcp_settings(path).git_enabled)
            reconnect.assert_awaited_once_with(load_mcp_settings(path))

    async def test_declined_configuration_never_saves_or_reconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mcp.json'
            reconnect = AsyncMock()
            gateway = ToolGateway(Authorization(), registrations(path, reconnect), AsyncMock(return_value=False))
            result = await gateway.execute(ToolAction('configure_mcp', {'changes': {'git_repositories': [tmp]}}))
            self.assertEqual(result.result['error'], 'authorization_required')
            self.assertFalse(path.exists())
            reconnect.assert_not_awaited()

    async def test_runtime_can_enable_git_initialize_and_query_repository_in_one_conversation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('dianstar.providers.deepseek.AsyncOpenAI') as client_class:
            root = Path(tmp).resolve()
            target = root / 'new-project'
            env = root / '.env'
            env.write_text('DEEPSEEK_API_KEY=fixture-model-key\n')
            def tool(name, args, index):
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content=None,
                    tool_calls=[SimpleNamespace(id='call-' + str(index), function=SimpleNamespace(name=name, arguments=json.dumps(args)))]))])
            client_class.return_value.chat.completions.create = AsyncMock(side_effect=[
                tool('configure_mcp', {'changes': {'git_enabled': True, 'git_write_enabled': True, 'git_workspace_roots': [str(root)]}}, 1),
                tool('mcp_git__git_init', {'repository': str(target), 'branch': 'main'}, 2),
                tool('mcp_git__git_status', {'repository': str(target)}, 3),
                SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='项目已初始化。'))]),
            ])
            history = []
            result = await run_turn('启用 Git 并初始化项目', env_file=env, history=history, on_permission=AsyncMock(return_value=True), allow_backend_configuration=True)
            self.assertEqual(result, '项目已初始化。')
            self.assertTrue((target / '.git').is_dir())
            self.assertIn(str(target), load_mcp_settings(root / '.local/mcp.json').git_repositories)
            values = [json.loads(x['content']) for x in history if x['role'] == 'tool']
            self.assertTrue(values[0]['reconnected'])
            self.assertTrue(values[1]['configuration_saved'])
            self.assertNotIn('error', values[2])


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.auth = Authorization()
        settings = MCPSettings(git_enabled=True, git_repositories=(str(self.root),), workspace_write_enabled=True)
        self.mcp = SimpleNamespace(settings=settings, authorization=self.auth, git=SimpleNamespace(repositories={str(self.root)}))
        self.tools = WorkspaceTools(self.mcp, self.root / '.env')
        self.gateway = ToolGateway(self.auth, self.tools.registrations(), AsyncMock(return_value=True))

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {'repository': str(self.root), **args}))).result

    async def test_create_read_search_replace_and_empty_replacement(self):
        result = await self.call('workspace_edit', path='src/example.txt', mode='create', new_text='hello\nworld\n')
        self.assertTrue(result['success'])
        value = await self.call('workspace_read', path='src/example.txt')
        self.assertIn('1: hello', value['content'])
        self.assertIn('src/example.txt:2: world', (await self.call('workspace_search', query='world'))['content'])
        self.assertTrue((await self.call('workspace_edit', path='src/example.txt', old_text='world\n', new_text=''))['success'])
        self.assertEqual((self.root / 'src/example.txt').read_text(), 'hello\n')

    async def test_paths_secrets_and_stale_edit_are_rejected(self):
        for path in ('../outside', '.env', '.git/config', 'folder/../../outside'):
            self.assertIn('error', await self.call('workspace_read', path=path))
        target = self.root / 'one.txt'
        target.write_text('before')
        async def edit_during_approval(_):
            target.write_text('user edit')
            return True
        self.gateway.request_approval = edit_during_approval
        result = await self.call('workspace_edit', path='one.txt', old_text='before', new_text='after')
        self.assertEqual(result['error'], 'workspace_file_changed')
        self.assertEqual(target.read_text(), 'user edit')

    async def test_command_exit_timeout_and_secret_environment(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'fixture-model-key'}):
            value = await self.call('workspace_run', argv=[sys.executable, '-c', 'import os;print(os.getenv("DEEPSEEK_API_KEY", "absent"));raise SystemExit(3)'])
        self.assertEqual(value['exit_code'], 3)
        self.assertFalse(value['success'])
        self.assertIn('absent', value['content'])
        value = await self.call('workspace_run', argv=[sys.executable, '-c', 'import time;time.sleep(20)'], timeout_seconds=1)
        self.assertTrue(value['timed_out'])

    async def test_command_timeout_stops_descendants(self):
        import asyncio
        marker = self.root / 'orphan-marker.txt'
        child = 'import time;from pathlib import Path;time.sleep(2);Path(' + repr(str(marker)) + ').write_text("orphan")'
        parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",' + repr(child) + ']);time.sleep(20)'
        result = await self.call('workspace_run', argv=[sys.executable, '-c', parent], timeout_seconds=1)
        self.assertTrue(result['timed_out'])
        await asyncio.sleep(2)
        self.assertFalse(marker.exists())

    async def test_read_only_configuration_does_not_expose_edit_or_command(self):
        self.mcp.settings = replace(self.mcp.settings, workspace_write_enabled=False)
        names = self.tools.registrations()
        self.assertIn('workspace_read', names)
        self.assertNotIn('workspace_edit', names)
        self.assertNotIn('workspace_run', names)

    async def test_delete_requires_confirmation_and_only_deletes_named_file(self):
        target = self.root / 'delete.txt'
        target.write_text('delete this file\n')
        other = self.root / 'keep.txt'
        other.write_text('keep\n')
        self.gateway.request_approval = AsyncMock(return_value=False)
        self.assertIn('error', await self.call('workspace_delete', path='delete.txt'))
        self.assertTrue(target.exists())
        self.gateway = ToolGateway(self.auth, self.tools.registrations(), AsyncMock(return_value=True))
        self.assertTrue((await self.call('workspace_delete', path='delete.txt'))['success'])
        self.assertFalse(target.exists())
        self.assertEqual(other.read_text(), 'keep\n')


class GitHubAdministrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = Authorization()
        self.settings = MCPSettings(github_enabled=True, github_repositories=('owner/repo',), github_admin_repositories=('owner/repo',))
        self.admin = GitHubAdmin(self.settings, Path('missing.env'), self.auth)
        self.meta = {'full_name': 'owner/repo', 'private': False, 'visibility': 'public', 'permissions': {'admin': True}, 'description': 'before', 'default_branch': 'main'}
        self.protection = {'required_status_checks': {'strict': True, 'contexts': ['tests']}, 'enforce_admins': {'enabled': False},
            'required_pull_request_reviews': {'required_approving_review_count': 1, 'dismiss_stale_reviews': True}, 'restrictions': None}
        self.mutations = []
        self.invites = []
        self.fail_write = False
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.respond)
        self.patch_client = patch('httpx.AsyncClient', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs))
        self.patch_client.start()
        self.addCleanup(self.patch_client.stop)
        self.patch_token = patch.object(self.admin.adapter, 'token', return_value='fixture-admin-token')
        self.patch_token.start()
        self.addCleanup(self.patch_token.stop)
        self.gateway = ToolGateway(self.auth, self.admin.registrations(), AsyncMock(return_value=True))

    def respond(self, request):
        path, method = request.url.path, request.method
        if method != 'GET':
            self.mutations.append((method, path, json.loads(request.content) if request.content else None))
        if path == '/repos/owner/repo':
            if method == 'PATCH':
                self.meta.update(json.loads(request.content))
                if self.fail_write:
                    return httpx.Response(500)
            return httpx.Response(200, json=self.meta)
        if path.endswith('/branches/main'):
            return httpx.Response(200, json={'name': 'main'})
        if path.endswith('/protection'):
            if method == 'PUT':
                self.protection = json.loads(request.content)
            return httpx.Response(200, json=self.protection)
        if path.endswith('/invitations'):
            return httpx.Response(200, json=self.invites)
        if '/invitations/' in path and method == 'DELETE':
            self.invites = []
            return httpx.Response(204)
        if path.endswith('/collaborators'):
            return httpx.Response(200, json=[])
        if path.endswith('/permission'):
            return httpx.Response(404)
        if '/collaborators/' in path:
            if method == 'PUT':
                self.invites = [{'id': 123, 'invitee': {'login': 'fixture-user'}, 'permissions': 'write'}]
                return httpx.Response(201, json=self.invites[0])
            self.invites = []
            return httpx.Response(204)
        return httpx.Response(404)

    async def call(self, name, **args):
        return (await self.gateway.execute(ToolAction(name, {'owner': 'owner', 'repo': 'repo', **args}))).result

    async def test_settings_update_clears_description_and_reads_back_actual_state(self):
        result = await self.call('github_repository_settings_update', changes={'description': '', 'delete_branch_on_merge': True})
        self.assertTrue(result['success'], result)
        self.assertEqual(self.meta['description'], '')
        self.assertEqual(self.mutations, [('PATCH', '/repos/owner/repo', {'description': '', 'delete_branch_on_merge': True})])

    async def test_branch_change_preserves_existing_review_and_admin_rules(self):
        result = await self.call('github_branch_protection_update', branch='main', changes={'required_conversation_resolution': True})
        self.assertTrue(result['success'], result)
        body = self.mutations[0][2]
        self.assertFalse(body['enforce_admins'])
        self.assertEqual(body['required_pull_request_reviews']['required_approving_review_count'], 1)
        self.assertEqual(body['required_status_checks']['contexts'], ['tests'])

    async def test_collaborator_invitation_and_removal_use_official_permission_field(self):
        result = await self.call('github_collaborators_set', username='fixture-user', permission='push')
        self.assertTrue(result['success'], result)
        self.assertTrue(result['invitation'])
        self.assertEqual(self.mutations[0][2], {'permission': 'push'})
        self.assertTrue((await self.call('github_collaborators_remove', username='fixture-user'))['success'])

    async def test_decline_and_outside_scope_never_mutate_repository(self):
        self.gateway.request_approval = AsyncMock(return_value=False)
        self.assertIn('error', await self.call('github_repository_settings_update', changes={'description': 'declined'}))
        self.assertEqual(self.mutations, [])
        result = await self.gateway.execute(ToolAction('github_repository_settings_get', {'owner': 'other', 'repo': 'repo'}))
        self.assertIn('error', result.result)

    async def test_state_change_while_approving_is_detected(self):
        async def approve(_):
            self.meta['description'] = 'user changed'
            return True
        self.gateway.request_approval = approve
        result = await self.call('github_repository_settings_update', changes={'description': 'agent proposed'})
        self.assertEqual(result['error'], 'github_admin_state_changed')
        self.assertEqual(self.mutations, [])

    async def test_unknown_write_outcome_is_not_automatically_retried(self):
        self.fail_write = True
        result = await self.call('github_repository_settings_update', changes={'description': 'possibly saved'})
        self.assertEqual(result['error'], 'github_admin_outcome_unknown')
        result = await self.call('github_repository_settings_update', changes={'description': 'possibly saved'})
        self.assertEqual(result['error'], 'github_admin_outcome_unknown')
        self.assertEqual(len(self.mutations), 1)
