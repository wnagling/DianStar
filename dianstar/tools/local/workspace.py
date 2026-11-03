"""Shared workspace scope and composition of file and command tools."""
import os
from pathlib import Path
from dotenv import dotenv_values
from dianstar.security.content_guard import ContentScanner, protected_path, known_credentials, local_configuration
from dianstar.hooks import HookRunner
from .files import FileTools
from .commands import CommandTools
from .environments import EnvironmentTools
from .environment_inspection import EnvironmentInspection
from .files.recovery import FileRecovery
from .storage import StorageTools
from dianstar.security.system_paths import check_host_path


class WorkspaceTools:
    def __init__(self, projects, env_file, *, conversation_id=None, task_scope=None, storage_manager=None):
        self.projects, self.env_file = projects, env_file
        self.task_scope = task_scope
        self.security = HookRunner(ContentScanner(known=known_credentials(env_file,
            extra=projects.credentials())))
        self.files = FileRecovery(self)
        self.environments = EnvironmentTools(self, conversation_id)
        self.storage = StorageTools(self, storage_manager)

    def root(self, args):
        self.projects.require()
        root = Path(args["repository"])
        check_host_path(root, project=True, managed=str(root) in self.storage.manager.scratch_roots)
        if (not root.is_absolute() or str(root).startswith(("\\\\", "//"))
                or root not in {Path(value) for value in (*self.projects.roots, *self.storage.manager.scratch_roots)} or not root.is_dir()
                or root.resolve(strict=True) != root.absolute()):
            raise ValueError("Repository not enabled")
        return root

    def path(self, args, *, local_only=False, allow_root=False, internal_temporary=False):
        root = self.root(args)
        relative = args["path"]
        if allow_root and relative == ".":
            return root
        if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                or any(ord(char) < 32 for char in relative) or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))
                or (protected_path(relative) and not (local_only and local_configuration(relative))
                    and not (internal_temporary and all(not protected_path(part) for part in relative.split("/")[:-1])
                             and relative.split("/")[-1].startswith(".dianstar-tmp-")))):
            raise ValueError("Invalid project path")
        if self.security.scanner.matches(relative):
            raise ValueError("Credential in project path")
        if os.name == "nt" and any(part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in
                {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
                for part in relative.split("/")):
            raise ValueError("Windows device or aliased path")
        path = root / relative
        check_host_path(path, managed=str(root) in self.storage.manager.scratch_roots)
        if path.resolve() != path.absolute() or not path.is_relative_to(root):
            raise ValueError("Project path escaped repository")
        if local_only and path.absolute() == self.env_file.absolute():
            raise ValueError("Application credentials require the configuration editor")
        return path

    def mask(self, value):
        values = list(dotenv_values(self.env_file).values()) if self.env_file.is_file() else []
        values.extend(self.projects.credentials())
        values += [value for key, value in os.environ.items() if any(x in key.upper() for x in ("TOKEN", "API_KEY", "PASSWORD"))]
        for secret in values:
            if secret and len(secret) >= 8:
                value = value.replace(secret, "[已遮挡密钥]")
        return self.security.scanner.redact(value)

    def registrations(self):
        return {**FileTools(self).registrations(), **CommandTools(self).registrations(),
                **self.environments.registrations(), **EnvironmentInspection(self).registrations(), **self.storage.registrations()}

    def refresh_credentials(self):
        self.security.scanner = ContentScanner(known=known_credentials(self.env_file, extra=self.projects.credentials()))

    def mask_file(self, text, path):
        scanner = ContentScanner(known=known_credentials(self.env_file, extra=self.projects.credentials()))
        name = Path(path).name.lower()
        env_style = name.startswith(".env") or name.endswith(".env") or Path(path).suffix.lower() in {".ini", ".cfg", ".toml", ".yaml", ".yml"}
        safe = scanner.redact(text, env_style=env_style, preserve_lines=True)
        return self.security.scanner.redact(safe, env_style=env_style, preserve_lines=True)
