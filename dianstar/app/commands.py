"""Shared conversation commands; display and input remain in the interface."""

from dianstar.sessions.conversations import Conversation, list_conversations


COMMANDS = {
    "/help": "显示命令帮助",
    "/config": "配置 API Key、模型、快捷键和 GitHub 账号",
    "/github": "登录、检查或退出 GitHub 账号",
    "/new": "创建新会话",
    "/rename": "重命名当前会话：/rename 名称",
    "/resume": "按名称或 ID 恢复会话：/resume 名称",
    "/switch": "按最近使用时间选择会话（方向键、回车）",
    "/compress": "清空后续请求的上下文，保留聊天记录",
    "/exit": "退出",
}


class AmbiguousConversation(ValueError):
    pass


def resolve_conversation(target, directory):
    matches = [entry for entry in list_conversations(directory) if entry.name == target]
    if len(matches) > 1:
        raise AmbiguousConversation("有多个同名会话，请用 /switch 选择。")
    return matches[0].id if matches else target


def new_conversation(current):
    return Conversation.create(current.directory)


def rename_conversation(current, name):
    current.rename(name)


def compress_conversation(current):
    current.compress()
