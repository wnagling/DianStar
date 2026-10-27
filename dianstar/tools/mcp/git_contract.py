"""Shared Git MCP operation names; no server implementation or transport imports."""

READ_TOOLS = {"git_status", "git_diff", "git_log", "git_branches", "git_remotes"}
WRITE_TOOLS = {"git_stage", "git_unstage", "git_create_branch", "git_switch_branch",
               "git_commit", "git_fetch", "git_push"}
