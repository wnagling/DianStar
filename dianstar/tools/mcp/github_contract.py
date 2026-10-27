"""GitHub tool and method policy; parameter names remain the official schema's."""

READ_TOOLS = {
    "get_file_contents", "get_repository_tree", "list_issues", "issue_read",
    "list_pull_requests", "pull_request_read", "list_commits", "get_commit",
    "list_releases", "get_latest_release", "get_release_by_tag", "list_branches", "list_tags",
    "search_code", "search_issues", "search_pull_requests", "actions_list", "actions_get", "get_job_logs",
}
WRITE_TOOLS = {
    "create_pull_request", "update_pull_request", "issue_write", "add_issue_comment",
    "pull_request_review_write", "add_comment_to_pending_review", "add_reply_to_pull_request_comment",
}
SEARCH_TOOLS = {"search_code", "search_issues", "search_pull_requests"}
METHODS = {
    "issue_read": {"get", "get_comments", "get_labels", "get_sub_issues", "get_parent"},
    "pull_request_read": {"get", "get_diff", "get_files", "get_commits", "get_comments",
                          "get_reviews", "get_review_comments", "get_status", "get_check_runs"},
    "actions_list": {"list_workflows", "list_workflow_runs", "list_workflow_jobs", "list_workflow_run_artifacts"},
    "actions_get": {"get_workflow", "get_workflow_run", "get_workflow_run_usage",
                    "get_workflow_run_logs_url", "get_workflow_job", "download_workflow_run_artifact"},
    "issue_write": {"create", "update"},
    "pull_request_review_write": {"create", "submit_pending", "delete_pending", "submit", "delete", "resolve_thread", "unresolve_thread"},
}
