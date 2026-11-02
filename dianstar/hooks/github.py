"""Content checks for outgoing GitHub PR, issue, comment and review arguments."""
import json

from dianstar.security.content_guard import ContentScanner, MAX_FILE, SecurityViolation, check_content, known_credentials
from .registry import HookRegistration


def check_outgoing(context, path, data):
    return check_content(context.scanner, path, data)


def registrations():
    return (HookRegistration("github.content", ("before_github_write",), check_outgoing, priority=0),)


class GitHubWriteHooks:
    def __init__(self, env_file, token_provider, *, additional_hooks=()):
        self.env_file, self.token_provider = env_file, token_provider
        self.additional_hooks = additional_hooks

    def check(self, name, arguments):
        from .runner import HookRunner
        scanner = ContentScanner(known=known_credentials(self.env_file, extra=(self.token_provider(),)))
        runner = HookRunner(scanner, additional_hooks=self.additional_hooks)

        def files():
            payload = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
            if len(payload) > MAX_FILE:
                raise SecurityViolation(incomplete=True)
            yield "github/" + name + "/arguments.json", payload

            def strings(value, path):
                if isinstance(value, dict):
                    for key, item in value.items():
                        yield from strings(item, path + "/" + str(key))
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        yield from strings(item, path + "/" + str(index))
                elif isinstance(value, str):
                    # Scan actual text as well as JSON: multiline assignments in
                    # bodies must not disappear behind JSON's escaped newlines.
                    yield path + ".txt", value.encode("utf-8")

            yield from strings(arguments, "github/" + name)

        return runner.run("before_github_write", files())
