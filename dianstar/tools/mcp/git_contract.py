"""Shared Git MCP operation names; no server implementation or transport imports."""

READ_TOOLS = {"git_status", "git_diff", "git_log", "git_branches", "git_remotes"}
READ_TOOLS |= {"git_show", "git_diff_refs", "git_config_read", "git_worktree_list", "git_stash_list", "git_conflicts"}
WRITE_TOOLS = {"git_stage", "git_unstage", "git_create_branch", "git_switch_branch",
               "git_commit", "git_fetch", "git_push"}
WRITE_TOOLS |= {"git_clone", "git_init", "git_remote_set", "git_remote_remove", "git_set_upstream", "git_config_set",
                "git_checkout_remote", "git_pull", "git_restore", "git_revert", "git_merge", "git_rebase", "git_cherry_pick",
                "git_conflict_continue", "git_conflict_abort", "git_stash_save", "git_stash_apply", "git_stash_drop",
                "git_checkpoint", "git_worktree_add", "git_worktree_remove", "git_branch_rename", "git_branch_delete"}
WRITE_TOOLS |= {"git_remote_branch_delete", "git_publish"}

BOOTSTRAP_TOOLS = {"git_clone", "git_init"}
