"""MCP protocol entry point for the local Git workspace."""
import argparse
import asyncio
from mcp.server.fastmcp import FastMCP
from dianstar.tools.mcp.servers.git.operations import GitWorkspace


def make_server(repositories, allow_writes=False):
    workspace = GitWorkspace(repositories, allow_writes)
    server = FastMCP("DianStar Git")

    @server.tool()
    async def git_status(repository: str) -> dict:
        """查看允许仓库的分支、已跟踪文件和未忽略的新文件状态。"""
        return await asyncio.to_thread(workspace.read, "git_status", repository)

    @server.tool()
    async def git_diff(repository: str, staged: bool = False) -> dict:
        """查看允许仓库的工作区或暂存区差异，不运行外部 diff/textconv。"""
        return await asyncio.to_thread(workspace.read, "git_diff", repository, staged=staged)

    @server.tool()
    async def git_log(repository: str, limit: int = 10) -> dict:
        """查看允许仓库最近 1 至 30 条提交的摘要。"""
        return await asyncio.to_thread(workspace.read, "git_log", repository, limit=limit)

    @server.tool()
    async def git_branches(repository: str) -> dict:
        """查看本地分支、上游和领先/落后状态（以本地跟踪引用为准）。"""
        return await asyncio.to_thread(workspace.read, "git_branches", repository)

    @server.tool()
    async def git_remotes(repository: str) -> dict:
        """查看已配置远程的安全地址，不联网、不修改配置。"""
        return await asyncio.to_thread(workspace.read, "git_remotes", repository)

    if allow_writes:
        @server.tool()
        async def git_stage(repository: str, files: list[str]) -> dict:
            """预览精确暂存的文件；实际执行需应用确认和单次 token。"""
            return await asyncio.to_thread(workspace.prepare, "git_stage", repository, files=files)

        @server.tool()
        async def git_unstage(repository: str, files: list[str]) -> dict:
            """预览取消指定文件暂存，不修改工作区内容。"""
            return await asyncio.to_thread(workspace.prepare, "git_unstage", repository, files=files)

        @server.tool()
        async def git_create_branch(repository: str, branch: str, start_point: str = "HEAD", checkout: bool = False) -> dict:
            """预览从当前 HEAD 或已有本地分支创建新分支，可选择切换。"""
            return await asyncio.to_thread(workspace.prepare, "git_create_branch", repository,
                branch=branch, start_point=start_point, checkout=checkout)

        @server.tool()
        async def git_switch_branch(repository: str, branch: str) -> dict:
            """预览切换已有本地分支；要求工作区和暂存区干净。"""
            return await asyncio.to_thread(workspace.prepare, "git_switch_branch", repository, branch=branch)

        @server.tool()
        async def git_commit(repository: str, message: str) -> dict:
            """预览提交已暂存快照，展示完整差异和提交说明。"""
            return await asyncio.to_thread(workspace.prepare, "git_commit", repository, message=message)

        @server.tool()
        async def git_fetch(repository: str, remote: str = "origin") -> dict:
            """预览获取指定远程分支；不合并、不切换、不删除引用。"""
            return await asyncio.to_thread(workspace.prepare, "git_fetch", repository, remote=remote)

        @server.tool()
        async def git_push(repository: str, branch: str, remote: str = "origin", set_upstream: bool = False) -> dict:
            """预览当前分支普通推送；branch 必须明确为远程目标分支，不允许强制推送。"""
            return await asyncio.to_thread(workspace.prepare, "git_push", repository,
                remote=remote, branch=branch, set_upstream=set_upstream)

        @server.tool()
        async def git_apply(token: str) -> dict:
            """应用内部专用：消费已批准预览的 token，不注册给问答模型。"""
            return await asyncio.to_thread(workspace.apply, token)

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args()
    make_server(args.repo, args.allow_writes).run(transport="stdio")


if __name__ == "__main__":
    main()
