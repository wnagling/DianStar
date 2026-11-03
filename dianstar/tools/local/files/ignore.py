"""Bounded project-local ignore rules; no Git executable or user config."""
import fnmatch
from pathlib import Path
import os

from .text import FileError


class IgnoreRules:
    def __init__(self, root, match):
        self.root, self.match, self.cache = Path(root), match, {}
        self.count, self.total = 0, 0

    def load(self, path, base):
        if path in self.cache:
            return self.cache[path]
        rules = []
        if path.exists():
            if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
                    or path.stat().st_nlink != 1 or path.stat().st_size > 131072):
                raise FileError("file_scan_incomplete", "忽略规则文件不安全或过大，未继续搜索。")
            try:
                with path.open("rb") as stream:
                    data = stream.read(131073)
                self.total += len(data)
                if len(data) > 131072 or self.total > 1024 * 1024:
                    raise ValueError()
                lines = data.decode("utf-8-sig").splitlines()
            except (ValueError, OSError):
                raise FileError("file_scan_incomplete", "无法完整读取项目忽略规则。") from None
            for line in lines:
                while line.endswith(" ") and not line.endswith("\\ "):
                    line = line[:-1]
                if not line or line.startswith("#"):
                    continue
                negate = line.startswith("!")
                line = line[1:] if negate else line
                directory = line.endswith("/")
                pattern = line.rstrip("/")
                anchored = pattern.startswith("/") or "/" in pattern
                pattern = pattern.lstrip("/")
                result, index = [], 0
                while index < len(pattern):
                    char = pattern[index]
                    if char == "\\" and index+1 < len(pattern):
                        index += 1
                        char = pattern[index]
                        char = {"*": "[*]", "?": "[?]", "[": "[[]", "]": "[]]"}.get(char, char)
                    result.append(char)
                    index += 1
                pattern = "".join(result)
                if not pattern:
                    continue
                self.count += 1
                if self.count > 5000 or len(pattern) > 500:
                    raise FileError("file_scan_incomplete", "项目忽略规则超过检查上限。")
                rules.append((base, pattern, anchored, directory, negate))
        self.cache[path] = rules
        return rules

    def rules_for(self, relative):
        # Project-local excludes have lower priority than .gitignore. Never
        # follow linked Git metadata to another directory for search settings.
        rules = []
        metadata = self.root / ".git"
        if metadata.is_dir() and metadata.resolve() == metadata.absolute():
            rules.extend(self.load(metadata / "info/exclude", ""))
        parent = self.root
        rules.extend(self.load(parent / ".gitignore", ""))
        for part in Path(relative).parts[:-1]:
            parent /= part
            rules.extend(self.load(parent / ".gitignore", parent.relative_to(self.root).as_posix()))
        return rules

    def ignored(self, relative, is_directory=False):
        rules = self.rules_for(relative)
        parts = relative.split("/")
        for length in range(1, len(parts)+1):
            target = "/".join(parts[:length])
            directory = length < len(parts) or is_directory
            ignored = False
            for base, pattern, anchored, only_directory, negate in rules:
                prefix = base + "/" if base else ""
                if not target.startswith(prefix) or only_directory and not directory:
                    continue
                value = target[len(prefix):]
                if not value or pattern.endswith("/**") and value == pattern[:-3]:
                    continue
                candidate = value if anchored else value.rsplit("/", 1)[-1]
                if os.name == "nt":
                    candidate, rule = candidate.casefold(), pattern.casefold()
                else:
                    rule = pattern
                matches = self.match(candidate, rule) if anchored else fnmatch.fnmatchcase(candidate, rule)
                if matches:
                    ignored = not negate
            # A negated child cannot resurrect a directory whose parent remains
            # ignored. This also holds for explicit listing of such directories.
            if ignored:
                return True
        return False
