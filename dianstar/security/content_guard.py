"""Bounded credential checks. Findings never contain matched values or snippets."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import PurePosixPath
import re

from dotenv import dotenv_values

FINGERPRINT_ENV = "DIANSTAR_SECRET_DIGESTS"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 5000
TEMPLATES = {".env.example", ".env.sample", ".env.template"}
# A source module can manage credentials without being a credential document.
# This only qualifies the final component; private directories remain protected.
SOURCE_SUFFIXES = frozenset({".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                             ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
                             ".cs", ".rb", ".php", ".swift"})


def template(path):
    return PurePosixPath(path.replace("\\", "/").lower()).name in TEMPLATES


def protected_path(path):
    parts = PurePosixPath(path.replace("\\", "/").lower()).parts
    for index, part in enumerate(parts):
        if (part in {".git", ".local", ".aws", ".ssh"}
                or ((part.startswith(".env") or part.endswith(".env")) and part not in TEMPLATES)
                or part.endswith((".pem", ".key"))):
            return True
        if part.startswith(("credentials", "secrets")):
            source_file = index == len(parts) - 1 and PurePosixPath(part).suffix in SOURCE_SUFFIXES
            if not source_file:
                return True
    return False


def local_configuration(path):
    parts = PurePosixPath(path.replace("\\", "/").lower()).parts
    if not parts or any(protected_path(part) for part in parts[:-1]):
        return False
    name = parts[-1]
    environment = (name.startswith(".env") or name.endswith(".env")) and name not in TEMPLATES
    if protected_path(name) and not environment:
        return False
    return environment or PurePosixPath(name).suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}


def placeholder(value):
    value = value.strip()
    lowered = value.lower()
    return (not value or lowered in {"null", "none", "true", "false", "changeme", "change-me", "replace-me", "redacted", "[已遮挡密钥]"}
            or lowered.startswith(("your-", "your_", "example-", "example_", "placeholder", "fake-", "fixture-", "test-", "test_"))
            or bool(re.search(r"(?:^|[-_])(?:fake|fixture|example|placeholder|dummy)(?:[-_]|$)", lowered))
            or value.startswith(("${", "{{", "<", "$env:", "os.environ", "os.getenv", "process.env", "env(")))


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    confidence: str = "high"


def check_content(scanner, path, data, *, allow_local=False):
    """Shared safety policy; event bindings decide whether local storage applies."""
    findings = []
    if protected_path(path) and not allow_local:
        findings.append(Finding(scanner.redact(path), 0, "受保护的本地配置或私钥路径"))
    for item in scanner.scan(path, data):
        if not allow_local or item.kind == "应用已知凭据" or template(path):
            findings.append(item)
    return findings


class SecurityViolation(ValueError):
    code = "sensitive_content_denied"

    def __init__(self, findings=(), *, incomplete=False, source=None):
        self.findings = tuple(findings)
        self.source = source
        self.code = "security_scan_incomplete" if incomplete else "sensitive_content_denied"
        self.message = ("安全检查未能完整完成，已停止操作；请检查文件大小、仓库历史或工具状态。" if incomplete else
                        "检测到受保护配置或凭据，已停止操作。请使用环境变量或不含凭据的模板；本地配置可在明确仅本地保存后写入。")
        if source == "git_index":
            self.message += ("检查对象是完整暂存快照，包含本次未修改的已跟踪文件；不是工作区文件或差异中的路径引用。"
                             "工作区已修正的文件需要重新暂存才会进入提交；测试假值请使用明确的 fixture-/fake- 占位值。")
        super().__init__(self.message)

    def result(self):
        result = {"error": self.code, "message": self.message,
                  "findings": [asdict(item) for item in self.findings[:30]]}
        if self.source is not None:
            result["source"] = self.source
        return result


def known_credentials(env_file=None, extra=()):
    values = dotenv_values(env_file) if env_file and env_file.is_file() else {}
    found = [value for key, value in {**os.environ, **values}.items()
             if any(word in key.upper() for word in ("API_KEY", "TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY"))
             and isinstance(value, str) and len(value) >= 8]
    return tuple(dict.fromkeys([*found, *(v for v in extra if isinstance(v, str) and len(v) >= 8)]))


def fingerprint_payload(values):
    return json.dumps(sorted({hashlib.sha256(v.encode()).hexdigest() for v in values}))


class ContentScanner:
    # Provider prefixes: GitHub's documented authentication formats. No network validity check.
    provider = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_.-]{20,}|github_pat_[A-Za-z0-9_.-]{20,})\b")
    private_key = re.compile(r"-----BEGIN (?:(?:[A-Z0-9]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----(?=\r?\n|\\n|\\r\\n)")
    private_header = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----")
    assignment = re.compile(r'''(?im)(?<![a-z0-9_.-])["']?([a-z0-9_.-]{0,100}(?:api[_-]?key|private[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|authorization|token)[a-z0-9_.-]{0,100})["']?\s*[:=]\s*(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\s,;}#]+))''')
    candidates = re.compile(r'''[A-Za-z0-9_./+=:@-]{8,}|"([^"\r\n]{8,})"|'([^'\r\n]{8,})' ''', re.X)

    def __init__(self, known=(), fingerprints=None):
        self.known = tuple(known)
        if fingerprints is None:
            try:
                fingerprints = json.loads(os.environ.get(FINGERPRINT_ENV, "[]"))
                if not isinstance(fingerprints, list) or len(fingerprints) > 512 or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in fingerprints):
                    raise ValueError()
            except (ValueError, TypeError):
                raise SecurityViolation(incomplete=True) from None
        self.fingerprints = set(fingerprints)

    def matches(self, text, *, env_style=True):
        matches = []
        for secret in self.known:
            start = 0
            while (offset := text.find(secret, start)) >= 0:
                matches.append((offset, offset + len(secret), "应用已知凭据", "high"))
                start = offset + len(secret)
        for match in self.candidates.finditer(text):
            value = next((g for g in match.groups() if g is not None), match.group())
            if hashlib.sha256(value.encode()).hexdigest() in self.fingerprints:
                matches.append((match.start(), match.end(), "应用已知凭据", "high"))
        for pattern, kind in ((self.private_key, "私钥"), (self.provider, "GitHub 访问令牌")):
            matches.extend((m.start(), m.end(), kind, "high") for m in pattern.finditer(text))
        for match in self.assignment.finditer(text):
            group = next(i for i in (2, 3, 4) if match.group(i) is not None)
            if group == 4 and not env_style:
                continue
            value = match.group(group)
            if len(value) >= 8 and not placeholder(value):
                matches.append((match.start(group), match.end(group), "疑似硬编码凭据", "suspected"))
        return matches

    def scan(self, path, data):
        if len(data) > MAX_FILE:
            raise SecurityViolation(incomplete=True)
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
        name = PurePosixPath(path.replace("\\", "/").lower()).name
        env_style = name.startswith(".env") or PurePosixPath(name).suffix in {".ini", ".cfg", ".toml", ".yaml", ".yml"}
        findings, cursor, line = set(), 0, 1
        for start, _, kind, confidence in sorted(self.matches(text, env_style=env_style)):
            line += text.count("\n", cursor, start)
            cursor = start
            findings.add((line, kind, confidence))
        return [Finding(self.redact(path), line, kind, confidence) for line, kind, confidence in sorted(findings)]

    def redact(self, text):
        # A private-key finding covers the whole key block in output/previews.
        parts, cursor = [], 0
        while match := self.private_header.search(text, cursor):
            marker = "-----END " + match.group(1) + "-----"
            close = text.find(marker, match.end())
            end = len(text) if close < 0 else close + len(marker)
            parts.extend((text[cursor:match.start()], "[已遮挡私钥]"))
            cursor = end
        parts.append(text[cursor:])
        text = "".join(parts)
        ranges = []
        for start, end, _, _ in sorted(self.matches(text)):
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
            else:
                ranges.append((start, end))
        parts, cursor = [], 0
        for start, end in ranges:
            parts.extend((text[cursor:start], "[已遮挡凭据]"))
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)
