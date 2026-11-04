"""Real isolated PowerShell behavior through the public tool gateway."""
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from dianstar.core.types import ToolAction
from dianstar.security.authorization import Authorization
from dianstar.security.gateway import ToolGateway
from dianstar.tools.local.project_context import ProjectContext
from dianstar.tools.local.workspace import WorkspaceTools
from dianstar.platform.shells import find_powershell


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell isolation')
class PowerShellToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        self.root=self.base/"中文 project's [1]"
        self.root.mkdir()
        state=patch.dict(os.environ,DIANSTAR_FILE_STATE_ROOT=str(self.base/'state'))
        state.start();self.addCleanup(state.stop)
        self.auth=Authorization()
        self.addCleanup(self.auth.close)
        self.workspace=WorkspaceTools(ProjectContext(self.auth,roots=(self.root,)),self.base/'unused.env')
        self.addCleanup(self.workspace.storage.manager.close)
        self.approve=AsyncMock(return_value=True)
        self.gateway=ToolGateway(self.auth,self.workspace.registrations(),self.approve)

    async def call(self,name,**args):
        return (await self.gateway.execute(ToolAction(name,{'repository':str(self.root),**args}))).result

    async def test_explicit_exit_preserves_preceding_output_and_exit_code(self):
        result=await self.call('shell',command="Write-Output 'before-failure'; exit 7")
        self.assertEqual(result.get('exit_code'),7,result)
        self.assertFalse(result['success'])
        self.assertIn('before-failure',result['content'])

    async def test_shell_uses_powershell7_core(self):
        result=await self.call('shell',command="Write-Output ('major='+$PSVersionTable.PSVersion.Major); Write-Output $PSVersionTable.PSEdition")
        self.assertTrue(result.get('success'),result)
        self.assertIn('major=7',result['content'])
        self.assertIn('Core',result['content'])

    async def test_bare_and_legacy_names_route_to_selected_powershell7(self):
        legacy=Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        for executable in ('pwsh','powershell',str(legacy)):
            with self.subTest(executable=executable):
                result=await self.call('workspace_run',argv=[executable,'-Command',"Write-Output ('major='+$PSVersionTable.PSVersion.Major)"])
                self.assertTrue(result.get('success'),result)
                self.assertIn('major=7',result['content'])
                self.assertEqual(Path(result['executable']),find_powershell())

    async def test_argv_powershell_uses_project_directory_and_unicode_output(self):
        (self.root/'输入.txt').write_text('中文 marker',encoding='utf-8')
        command="(Get-Location).ProviderPath; Get-ChildItem -File | Select-Object -ExpandProperty Name; Get-Content -LiteralPath '输入.txt' -Encoding UTF8"
        result=await self.call('workspace_run',argv=[str(find_powershell()),'-NoProfile','-NonInteractive','-Command',command])
        self.assertEqual(result.get('exit_code'),0,result)
        self.assertIn(str(self.root),result['content'])
        self.assertIn('输入.txt',result['content'])
        self.assertIn('中文 marker',result['content'])
        self.assertEqual(result['isolation_profile'],'lpac')

    async def test_file_invocation_is_rejected_before_approval_or_execution(self):
        (self.root/'script.ps1').write_text("Write-Output 'must-not-run'",encoding='utf-8')
        result=await self.call('workspace_run',argv=[str(find_powershell()),'-NoProfile','-File','script.ps1'])
        self.assertEqual(result.get('error'),'powershell_arguments_unsupported',result)
        self.approve.assert_not_awaited()

    async def test_unknown_or_hidden_runtime_options_never_reach_approval(self):
        for options in (['-NoExit','-Command','Write-Output fixture'],
                        ['-ExecutionPolicy','Bypass','-Command','Write-Output fixture'],
                        ['-Command','-'],['-EncodedCommand','ZgBpAHgAdAB1AHIAZQA='],
                        ['-Command','Write-Output','fixture']):
            with self.subTest(options=options):
                result=await self.call('workspace_run',argv=[str(find_powershell()),*options])
                self.assertIn(result.get('error'),{'powershell_arguments_unsupported','dangerous_command_denied'},result)
        self.approve.assert_not_awaited()

    async def test_native_failure_output_and_exit_are_preserved_through_both_entries(self):
        executable=str(Path(sys.executable)).replace("'","''")
        command="& '"+executable+"' -I -S -c \"print('native-before');raise SystemExit(9)\""
        for name,args in (('shell',{'command':command}),
                          ('workspace_run',{'argv':[str(find_powershell()),'-c',command]})):
            with self.subTest(entry=name):
                result=await self.call(name,**args)
                self.assertEqual(result.get('exit_code'),9,result)
                self.assertIn('native-before',result['content'])

    async def test_timeout_keeps_output_already_delivered_to_the_pipe(self):
        # Exceed the child's serializer buffer so bytes reach the OS pipe.
        # A killed runtime's unflushed in-memory output cannot be recovered.
        result=await self.call('shell',command="Write-Output ('before-timeout ' * 1000); Start-Sleep -Seconds 20",timeout_seconds=3)
        self.assertTrue(result.get('timed_out'),result)
        self.assertIn('before-timeout',result['content'])

    async def test_normalized_entry_cannot_read_sibling_fixture(self):
        outside=self.base/'outside.txt'
        outside.write_text('fixture-outside-private-marker',encoding='utf-8')
        command="Get-Content -LiteralPath '"+str(outside).replace("'","''")+"'"
        result=await self.call('workspace_run',argv=[str(find_powershell()),'-Command',command])
        self.assertFalse(result.get('success'),result)
        self.assertNotIn('fixture-outside-private-marker',result['content'])
        self.assertEqual(result['isolation_profile'],'lpac')

    async def test_explicit_network_request_still_requires_approval(self):
        self.approve.return_value=False
        result=await self.call('workspace_run',argv=[str(find_powershell()),'-Command',"Write-Output 'must-not-run'"],network=True)
        self.assertEqual(result.get('error'),'authorization_required',result)
        self.assertIn('互联网',self.approve.await_args.args[0]['summary'])

    async def test_revocation_during_approval_prevents_normalized_execution(self):
        async def revoke(request):
            self.auth.deny('workspace.write')
            return True
        self.gateway.request_approval=revoke
        result=await self.call('workspace_run',argv=[str(find_powershell()),'-Command',"Write-Output 'must-not-run'"])
        self.assertEqual(result.get('error'),'authorization_changed',result)


@unittest.skipUnless(os.name=='nt','Native Windows UI helper')
class PowerShellMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_worker_initializes_on_powershell7_without_showing_popup(self):
        from dianstar.platform.windows.context_menu import WindowsCopyMenuClient
        client=WindowsCopyMenuClient()
        try:
            await client.start()
        finally:
            await client.close()


@unittest.skipUnless(os.name=='nt','Windows runtime discovery')
class PowerShellDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        env=patch.dict(os.environ,LOCALAPPDATA=str(self.base/'local'),ProgramFiles=str(self.base/'programs'),
            USERPROFILE=str(self.base/'home'),PATH=str(self.base/'project'))
        env.start();self.addCleanup(env.stop)

    def install_fixture(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'fixture executable location; never run')
        return path

    def test_discovery_ignores_path_shadow_and_does_not_fall_back_to_51(self):
        from dianstar.platform.shells import PowerShellUnavailable
        self.install_fixture(self.base/'project/pwsh.exe')
        with self.assertRaises(PowerShellUnavailable):find_powershell()

    def test_independent_user_install_is_preferred_over_machine_install(self):
        self.install_fixture(self.base/'programs/PowerShell/7/pwsh.exe')
        user=self.install_fixture(self.base/'local/Programs/PowerShell/7/pwsh.exe')
        self.assertEqual(find_powershell(),user)
