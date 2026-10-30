"""Account commands independent of model configuration and chat history."""
import asyncio
from getpass import getpass, GetPassWarning
import warnings

from dianstar.app.github_account import GitHubAccount
from dianstar.config.paths import ENV_FILE
from dianstar.platform.github_credentials import GitHubLoginError


def run_account_command(operation, *, env_file=ENV_FILE):
    if operation not in {"status", "login", "token", "logout"}:
        print("GitHub 账号操作：status 查看状态、login 浏览器登录、token 输入令牌、logout 退出登录。")
        return 1
    service = GitHubAccount(env_file)
    async def run():
        if operation == "login":
            print("请在浏览器中完成 GitHub 登录；Ctrl+C 取消等待。")
            return await service.login()
        if operation == "token":
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                token = getpass("GitHub 访问令牌（输入隐藏）> ").strip()
            return await service.login(token)
        if operation == "logout":
            return await service.logout()
        return await service.check()
    try:
        status = asyncio.run(run())
        print(("账号：" + status.login + "\n" if status.login else "") + status.message)
        return 0 if status.state in {"connected", "signed_out"} else 1
    except (EOFError, KeyboardInterrupt):
        print("已取消登录操作。")
        return 1
    except GetPassWarning:
        print("当前终端不能隐藏输入，请使用 /github 的专用输入框或支持隐藏输入的交互式终端。")
        return 1
    except (ValueError, OSError) as error:
        print(str(error) if isinstance(error, GitHubLoginError) else "账号操作未完成，请重试。")
        return 1
