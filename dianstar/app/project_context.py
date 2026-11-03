"""Assemble local project scope and migrate legacy backend policy once read."""
from pathlib import Path

from dianstar.config.projects import ProjectSettings, load_project_settings
from dianstar.tools.local.project_context import ProjectContext


def selected_directory(directory):
    # Local authorization follows the selected directory, not a Git ancestor.
    # Git discovery may independently find repositories for version control.
    return Path(directory).absolute()


def create_project_context(authorization, directory, config_path, *, legacy=None, legacy_invalid=False, credentials=None):
    # Old explicit local-write denials remain effective until the separate
    # project configuration is saved. Git enablement is not local enablement.
    fallback = ProjectSettings(write_enabled=not legacy_invalid and getattr(legacy, "workspace_write_enabled", None) is not False,
        roots=tuple(getattr(legacy, "git_repositories", ())),
        workspace_roots=tuple(getattr(legacy, "git_workspace_roots", ())))
    status = {}
    try:
        settings = load_project_settings(config_path, fallback=fallback)
    except ValueError as error:
        settings = ProjectSettings(enabled=False, write_enabled=False)
        status = {"state": "invalid", "message": str(error)}
    context = ProjectContext(authorization, settings, credentials=credentials)
    if settings.enabled:
        try:
            context.include(selected_directory(directory))
        except (ValueError, OSError):
            status = {"state": "not_selected", "message": "当前目录不适合作为项目，请打开明确的本地项目目录。"}
    return context, status
