"""Non-destructive preflight and display checks for command protection."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from dianstar.core.types import ToolAction, Observation
from dianstar.app.tool_events import ToolEvents
from dianstar.platform.powershell_output import decode_output
from dianstar.security.host_commands import inspect_command
from dianstar.security.system_paths import HostSafetyError
from dianstar.platform.toolchains import resolve_launcher
from dianstar.security.content_guard import ContentScanner


class HostPolicyTests(unittest.TestCase):
    def test_administration_wrappers_and_broker_commands_are_denied_without_execution(self):
        for command in ("Start-Service fixture-service", "Set-ExecutionPolicy Bypass", "Unregister-ScheduledTask fixture",
                        "cmd /c diskpart", "& pnputil /delete-driver fixture.inf", "wsl --exec sh", "docker run fixture",
                        "powershell -enc fixture", "Remove-Item -Recurse 'C:\\Windows\\System32\\drivers'"):
            with self.subTest(command=command), self.assertRaises(HostSafetyError):
                inspect_command("powershell", command.encode())

    def test_plain_project_scripts_and_readonly_administration_queries_are_not_blanket_denied(self):
        for command in ("Get-ChildItem .", "Write-Output fixture; Get-Location", "sc query fixture", "reg query HKCU\\fixture"):
            inspect_command("powershell", command.encode())

    def test_clixml_is_text_only_and_escapes_are_decoded_once(self):
        data = b'#< CLIXML\n<Objs><S>hello_x000A_world</S><S>_x005F_x0041_</S><Obj><ToString>failure</ToString><Props><S>hidden-property</S></Props></Obj></Objs>'
        self.assertEqual(decode_output(data), "hello\nworld\n_x0041_\nfailure")
        self.assertIn("未解析", decode_output(b'#< CLIXML\n<!DOCTYPE x><Objs/>'))
        self.assertIn("未解析", decode_output(b'#< CLIXML\n<!ENTITY x "fixture"><Objs/>'))

    def test_normal_and_malformed_shell_output_remain_visible(self):
        self.assertEqual(decode_output("中文".encode()), "中文")
        self.assertEqual(decode_output("Windows 启动错误".encode("utf-16-le")), "Windows 启动错误")
        self.assertEqual(decode_output('#< CLIXML\n<Objs><S>中文</S></Objs>'.encode("utf-16")), "中文")
        self.assertIn("<Objs>", decode_output(b"#< CLIXML\n<Objs>broken"))
        self.assertIn("未完整显示", decode_output(b"#< CLIXML\n<Objs>" + b"<S>x</S>"*10001 + b"</Objs>"))

    def test_executable_resolution_does_not_evaluate_project_shim_files(self):
        # A project-owned shim must remain an ordinary executable candidate;
        # only the known host Scoop launcher directory is interpreted.
        self.assertEqual(resolve_launcher(sys.executable), Path(sys.executable).resolve())

    def test_display_distinguishes_cleanup_warning_execution_uncertainty_and_refusal(self):
        events = ToolEvents(ContentScanner(), "fixture-request", "fixture-conversation")
        action = ToolAction("shell", {"repository": "fixture", "command": "Get-Location"}, "fixture")
        for result, status in (({"success": True, "isolation_cleanup_pending": True}, "warning"),
                               ({"error": "command_execution_incomplete"}, "failed"),
                               ({"error": "command_runtime_inaccessible"}, "not_executed")):
            events.started(action)
            completed = events.completed(Observation(action, result))
            self.assertEqual(completed["status"], status)
