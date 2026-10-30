"""Explicit typed interfaces for repository lifecycle and engineering tools."""
import asyncio
import inspect

DEFINITIONS = {
    "git_publish": {"files": list[str], "message": str, "branch": str,
                    "remote": (str, "origin"), "set_upstream": (bool, False)},
    "git_clone": {"url": str, "branch": (str, ""), "depth": (int, 0)},
    "git_init": {"branch": (str, "main")},
    "git_remote_set": {"remote": str, "url": str, "push": (bool, False)},
    "git_remote_remove": {"remote": str},
    "git_set_upstream": {"branch": str, "remote_branch": str, "remote": (str, "origin")},
    "git_checkout_remote": {"branch": str, "remote_branch": str, "remote": (str, "origin")},
    "git_config_set": {"key": str, "value": (str, ""), "remove": (bool, False)},
    "git_show": {"ref": (str, "HEAD"), "path": (str, "")},
    "git_diff_refs": {"base": str, "head": (str, "HEAD")},
    "git_config_read": {}, "git_worktree_list": {}, "git_stash_list": {}, "git_conflicts": {},
    "git_pull": {"branch": str, "remote": (str, "origin")},
    "git_restore": {"files": list[str], "source": (str, "HEAD")},
    "git_revert": {"ref": str}, "git_merge": {"ref": str}, "git_rebase": {"ref": str}, "git_cherry_pick": {"ref": str},
    "git_conflict_continue": {}, "git_conflict_abort": {},
    "git_stash_save": {"message": (str, "DianStar stash")},
    "git_stash_apply": {"stash": (str, "stash@{0}")}, "git_stash_drop": {"stash": (str, "stash@{0}")},
    "git_checkpoint": {"message": (str, "DianStar checkpoint")},
    "git_worktree_add": {"path": str, "branch": str, "start_point": (str, "HEAD"), "create_branch": (bool, True)},
    "git_worktree_remove": {"path": str},
    "git_branch_rename": {"branch": str, "new_branch": str},
    "git_branch_delete": {"branch": str, "force": (bool, False)},
    "git_remote_branch_delete": {"branch": str, "remote": (str, "origin")},
}

def register_tools(server, workspace, writes, *, allow_clone=False):
    from dianstar.tools.mcp.git_contract import READ_TOOLS
    for name, fields in DEFINITIONS.items():
        read = name in READ_TOOLS
        if not read and not writes and not (name == "git_clone" and allow_clone):
            continue
        def build(name=name, fields=fields, read=read):
            async def invoke(**kwargs):
                repository = kwargs.pop("repository")
                return await asyncio.to_thread(workspace.read if read else workspace.prepare, name, repository, **kwargs)
            parameters = [inspect.Parameter("repository", inspect.Parameter.KEYWORD_ONLY, annotation=str)]
            annotations = {"repository": str, "return": dict}
            for field, definition in fields.items():
                annotation, default = definition if isinstance(definition, tuple) else (definition, inspect.Parameter.empty)
                annotations[field] = annotation
                parameters.append(inspect.Parameter(field, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default))
            invoke.__annotations__ = annotations
            invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict)
            invoke.__name__ = name
            invoke.__doc__ = "查询工程状态。" if read else "预览指定工程操作；实际执行须经应用展示并批准。"
            return invoke
        server.tool(name=name)(build())
