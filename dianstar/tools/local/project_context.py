"""Application-owned inputs shared by local tools; no MCP or Git client."""
from pathlib import Path

from dianstar.config.projects import ProjectSettings
from dianstar.security.system_paths import check_host_path, HostSafetyError


class ProjectContext:
    def __init__(self, authorization, settings=None, *, roots=(), credentials=None):
        self.authorization = authorization
        self.settings = settings if settings is not None else ProjectSettings()
        self.roots = set(map(str, roots)) | set(self.settings.roots)
        self.credentials = credentials if credentials is not None else lambda: ()

    def include(self, directory):
        root = check_host_path(Path(directory), project=True)
        if not root.is_dir():
            raise ValueError("Project directory missing")
        self.roots.add(str(root))
        return root

    def require(self, *, write=False):
        if not self.settings.enabled:
            raise HostSafetyError("本地项目工具已被后台停用。", "local_project_disabled")
        if write and not self.settings.write_enabled:
            raise HostSafetyError("本地项目当前为只读，未执行修改或命令。", "local_project_readonly")
