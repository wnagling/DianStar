"""Narrow defaults from the current user's explicit project creation request.

This is an approval UX classifier, not an OS sandbox or a permission grant.
Unrecognized wording keeps the ordinary approval path.
"""
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class TaskScope:
    root: Path | None = None
    run_go: bool = False

    @classmethod
    def from_question(cls, question, current_directory, *, desktop=None):
        # Never interpret examples, quoted instructions or explicit negatives
        # as permission. Only the current user message is supplied by runtime.
        if re.search(r"不要|不许|禁止|不实际|别(?:运行|创建|新建|修改)|解释|举例|例如|假设|翻译|怎么|如何|是否|能否|能不能|可以吗", question):
            return cls()
        prefix = r"^\s*(?:(?:请|帮我|麻烦)\s*)*"
        match = re.match(prefix + r"在桌面(?:上)?\s*(?:新建|创建)\s*[`\"“]?([\w.-]{1,80})[`\"”]?\s*项目(?:[，。,；;\s]|$)", question)
        if match and match[1] not in {".", ".."} and not match[1].endswith((".", " ")):
            parent = Path(desktop) if desktop is not None else next((path for path in
                (Path(current_directory), *Path(current_directory).parents) if path.name.casefold() == "desktop"), Path.home() / "Desktop")
            root = parent / match[1]
        elif re.match(prefix + r"(?:用|使用)\s*Go\s*(?:写|编写|创建|实现)", question, re.I):
            root = Path(current_directory)
        else:
            return cls()
        return cls(root.absolute(), bool(re.search(r"\bGo\b", question, re.I) and re.search(r"运行|执行|跑", question)))

    def contains(self, root):
        return self.root is not None and os.path.normcase(str(Path(root).absolute())) == os.path.normcase(str(self.root))

    def new_files(self, root, entries):
        return bool(self.contains(root) and entries and all(entry.get("before") is None
            and entry.get("after") is not None and not entry.get("local_only") for entry in entries))

    def go_command(self, argv, root):
        from dianstar.security.content_guard import protected_path
        if (not self.run_go or not self.contains(root) or not Path(argv[0]).is_absolute()
                or Path(argv[0]).is_relative_to(root) or Path(argv[0]).name.lower() not in {"go", "go.exe"}):
            return False
        if len(argv) < 3 or argv[1] not in {"run", "build"}:
            return False
        args = list(argv[2:])
        if argv[1] == "build" and len(args) >= 3 and args[0] == "-o":
            target = Path(args[1])
            target = target if target.is_absolute() else Path(root) / target
            if (target.resolve() != target.absolute() or not target.is_relative_to(root)
                    or protected_path(target.relative_to(root).as_posix())):
                return False
            args = args[2:]
        if args == ["."]:
            sources = list(Path(root).glob("*.go"))
            return bool(sources and all(path.is_file() and path.resolve() == path.absolute() for path in sources))
        for value in args:
            path = Path(value)
            path = path if path.is_absolute() else Path(root) / path
            if (value.startswith("-") or path.suffix != ".go" or not path.is_file()
                    or path.resolve() != path.absolute() or not path.is_relative_to(root)
                    or protected_path(path.relative_to(root).as_posix())):
                return False
        return bool(args)


def task_git_defaults(registry, scope):
    """Keep Git's preflight/state checks, waive only repeated create consent."""
    result = dict(registry)
    name = "mcp_git__git_init"
    if name in result and scope.root is not None:
        entry = result[name]
        previous = entry.auto_approve
        def automatic(args):
            try:
                empty = not scope.root.exists() or not any(scope.root.iterdir())
            except OSError:
                empty = False
            return bool(scope.contains(args["repository"]) and empty or previous is not None and previous(args))
        result[name] = replace(entry, auto_approve=automatic)
    return result
