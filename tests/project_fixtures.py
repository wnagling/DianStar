"""Adapt historical test fixtures to the explicit local project contract."""
from dianstar.config.projects import ProjectSettings
from dianstar.tools.local.project_context import ProjectContext
from dianstar.tools.local.workspace import WorkspaceTools


def project_context(legacy):
    if isinstance(legacy, ProjectContext):
        return legacy
    settings = ProjectSettings(write_enabled=legacy.settings.workspace_write_enabled is not False,
        workspace_roots=legacy.settings.git_workspace_roots,
        allow_external_paths=getattr(legacy, "allow_clone", False))
    github = getattr(legacy, "github", None)
    context = ProjectContext(legacy.authorization, settings, credentials=lambda: (github.token(),) if github else ())
    context.roots = legacy.git.repositories
    return context


def workspace_tools(context, *args, **kwargs):
    return WorkspaceTools(project_context(context), *args, **kwargs)
