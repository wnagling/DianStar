"""Map trusted tool contracts to actual resource scopes, never model grants."""
import os
from pathlib import Path

from dianstar.core.resources import ResourceClaim
from dianstar.tools.local.files.transforms import transform_paths
from dianstar.tools.local.environments import ENVIRONMENT_TOOLS


GIT_NETWORK_WRITES = {"git_clone", "git_fetch", "git_pull", "git_push", "git_publish", "git_remote_branch_delete"}
FILE_READS = {"read", "glob", "grep", "file_history"}
FILE_WRITES = {"edit", "write", "patch", "move", "workspace_delete", "file_batch", "file_restore"}
STATE_TOOLS = {"open_project", "configure_mcp", "get_mcp_configuration", "review_changes", "record_review", "task_status"} | ENVIRONMENT_TOOLS


def path_claim(path, write=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Resource paths must be absolute")
    # Preserve lexical scope until the gateway validates links and permissions.
    return ResourceClaim("filesystem", tuple(Path(os.path.normcase(os.path.abspath(path))).parts), write)


class ToolResources:
    def __init__(self, registry, workspace, mcp):
        self.registry, self.workspace, self.mcp = registry, workspace, mcp

    def __call__(self, action):
        entry = self.registry.get(action.name)
        if entry is None:
            return (ResourceClaim("runtime", write=True),)
        if entry.resources is not None:
            return (ResourceClaim("runtime"), *entry.resources(action))
        name, args = action.name, action.arguments
        if name in STATE_TOOLS or entry.capability.startswith(("project.", "permissions.write", "mcp.configure")):
            return (ResourceClaim("runtime", write=True),)
        claims = [ResourceClaim("runtime")]
        if name in FILE_READS | FILE_WRITES or name in {"workspace_run", "bash", "shell", "workspace_environment_inspect"} or name.startswith("mcp_git__"):
            root = Path(args["repository"])
            write = name in FILE_WRITES or name in {"workspace_run", "bash", "shell", "workspace_environment_inspect"} or (name.startswith("mcp_git__") and entry.approval_description is not None)
            if name in {"glob", "grep", "file_history", "file_restore", "workspace_run", "bash", "shell", "workspace_environment_inspect"} or name.startswith("mcp_git__"):
                claims.append(path_claim(root, write))
            else:
                paths = (transform_paths(name, args) if name in {"patch", "move"} else
                         [item["path"] for item in args["operations"]] if name == "file_batch" else [args["path"]])
                for relative in paths:
                    path = root / relative
                    claims.append(path_claim(path, write))
                    if write:
                        # Directory creation is also a write; two new files in
                        # the same missing subtree cannot create it concurrently.
                        for parent in path.parents:
                            if parent == root or not parent.is_relative_to(root):
                                break
                            if not parent.exists():
                                claims.append(path_claim(parent, True))
            if name.startswith("mcp_git__"):
                original = name.removeprefix("mcp_git__")
                if original in GIT_NETWORK_WRITES:
                    # Exact remote targets are bound by preflight, after this
                    # plan. Until then unknown remote scopes must overlap.
                    claims.append(ResourceClaim("remote", write=True))
                if original in {"git_worktree_add", "git_worktree_remove"}:
                    claims.append(ResourceClaim("filesystem", write=True))
            return tuple(claims)
        if name.startswith(("mcp_github__", "github_")):
            write = entry.approval_description is not None
            owner, repo = args.get("owner"), args.get("repo")
            if owner and repo:
                key = (str(owner).casefold(), str(repo).casefold())
                claims.append(ResourceClaim("remote", key, write))
                if ":" in str(args.get("head", "")):
                    # The source fork's repository name is not always known
                    # before GitHub resolves the comparison. Reserve its reads
                    # conservatively instead of inventing a repository scope.
                    claims.append(ResourceClaim("remote"))
                if args.get("parent_owner") or args.get("parent_repo"):
                    claims.append(ResourceClaim("remote", (str(args.get("parent_owner", owner)).casefold(),
                        str(args.get("parent_repo", repo)).casefold())))
            else:
                # Search and cross-repository calls may touch multiple scopes.
                claims.append(ResourceClaim("remote", write=write))
            return tuple(claims)
        if entry.parallel_safe or entry.capability == "permissions.read" or name == "mcp_status":
            return tuple(claims)
        # New tools remain usable, but need trusted scopes to overlap safely.
        return (ResourceClaim("runtime", write=True),)
