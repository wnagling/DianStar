"""Host administration preflight and shared credential rules; OS isolation is separate."""
from dianstar.security.content_guard import check_content
from .registry import HookRegistration
from dianstar.security.host_commands import inspect_command


def before_command(context, path, data):
    inspect_command(path, data)
    return check_content(context.scanner, path, data)


def registrations():
    return (HookRegistration("command.content", ("before_command",), before_command, priority=0),)
