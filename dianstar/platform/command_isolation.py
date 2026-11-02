"""Mandatory Windows command write scope, with recoverable SID-only ACL leases."""
import json
import os
from pathlib import Path
import uuid

from dianstar.platform.file_state import private_directory, protect, state_directory, atomic_private_record
from dianstar.security.content_guard import protected_path, local_configuration, ContentScanner, MAX_FILE, MAX_TOTAL
from dianstar.security.file_locks import FileLocks, FileLockBusy
from dianstar.security.system_paths import HostSafetyError, check_host_path


class IsolationError(HostSafetyError):
    def __init__(self, message="无法建立命令写入隔离，目标程序未执行。"):
        super().__init__(message, "command_isolation_unavailable")


def file_identity(path):
    path = Path(path)
    info = path.lstat()
    if (path.resolve() != path.absolute() or getattr(info, "st_file_attributes", 0) & 0x400
            or path.is_symlink() or not path.is_dir() and info.st_nlink != 1):
        raise IsolationError("项目包含链接、junction 或多重硬链接，不能安全建立写入范围；未执行命令。")
    return [info.st_dev, info.st_ino]


class WindowsWriteScope:
    def __init__(self, root, *, protected=(), managed=False, scanner=None):
        from .windows_acl import WindowsSecurity
        self.root = check_host_path(root, project=True, managed=managed)
        self.api = WindowsSecurity()
        if self.api.is_elevated():
            raise IsolationError("请以普通用户启动 DianStar；开发命令不会继承管理员权限。")
        self.identifier = uuid.uuid4().hex
        self.profile = "DianStar.Command." + self.identifier
        self.sid = self.api.appcontainer_sid(self.profile)
        self.logon = self.api.logon_sid()
        self.directory = private_directory(state_directory() / "command-security")
        self.journal = self.directory / (self.identifier + ".json")
        self.locks = FileLocks()
        state_root = state_directory().absolute()
        self.protected = [Path(path).absolute() for path in protected]
        if not (managed and self.root.is_relative_to(state_root)):
            self.protected.append(state_root)
        self.paths, self.lease, self.cleanup_pending = [], None, False
        self.scanner = scanner if scanner is not None else ContentScanner()

    def save(self):
        data = protect(json.dumps({"sid": self.sid, "profile": self.profile, "logon": self.logon, "root": str(self.root), "paths": self.paths}, ensure_ascii=False).encode())
        atomic_private_record(self.journal, data)

    def plan(self):
        file_identity(self.root)
        denied, pending, count, inspected = [], [self.root], 0, 0
        def configuration_blocked(path, relative):
            nonlocal inspected
            size = path.stat().st_size
            inspected += size
            if size > MAX_FILE or inspected > MAX_TOTAL:
                return True
            identity = file_identity(path)
            with path.open("rb") as stream:
                data = stream.read(MAX_FILE + 1)
            if file_identity(path) != identity:
                raise IsolationError("配置文件在检查期间被替换，未执行命令。")
            return len(data) > MAX_FILE or bool(self.scanner.scan(relative, data))
        while pending:
            parent = pending.pop()
            for path in parent.iterdir():
                count += 1
                if count > 50000:
                    raise IsolationError("项目超过50000项的隔离检查上限，未执行命令。")
                file_identity(path)
                relative = path.relative_to(self.root).as_posix()
                if protected_path(relative) or any(path == item or path.is_relative_to(item) for item in self.protected):
                    denied.append(path)
                    if path == self.root / ".git" and path.is_dir():
                        configuration = path / "config"
                        if configuration.is_file() and configuration_blocked(configuration, ".git/config"):
                            denied.append(configuration)
                elif path.is_dir():
                    pending.append(path)
                elif local_configuration(relative):
                    # Oversized/uninspected configs stay outside the allowlist;
                    # never let a scan limit turn into credential disclosure.
                    if configuration_blocked(path, relative):
                        denied.append(path)
        return denied

    def revoke(self, paths, sid, logon=None):
        failed = False
        # Remove parent grants first, then this SID's explicit child denies.
        for entry in paths:
            path = Path(entry["path"])
            try:
                if path.exists() and file_identity(path) == entry["identity"]:
                    if "acl_before" in entry:
                        current = self.api.acl_snapshot(path)
                        if current == entry["acl_before"]:
                            continue
                        if current != entry["acl_expected"]:
                            failed = True
                            continue
                        before = entry["acl_before"]
                        self.api.set_acl(path, [bytes.fromhex(item) for item in before["entries"]], protected=before["protected"])
                        continue
                    self.api.edit_acl(path, sid, mode=4, inherit=path.is_dir())
                    if logon and "logon_before" in entry:
                        self.api.restore_sid(path, logon, entry["logon_before"])
                    for group, before in entry.get("app_groups_before", {}).items():
                        self.api.restore_sid(path, group, before)
                elif path.exists():
                    failed = True
            except (ValueError, OSError):
                failed = True
        return not failed

    def recover_pending(self):
        for journal in self.directory.glob("*.json"):
            if journal == self.journal:
                continue
            try:
                record = json.loads(protect(journal.read_bytes(), decrypt=True))
                if record.get("kind") == "read_catalog":
                    try:
                        with self.locks.hold("command-acl-live:" + journal.stem, timeout=0):
                            if not self.revoke(record["paths"], record["sid"]):
                                raise IsolationError("上次工具链只读权限清理未完成，未执行命令。")
                            journal.unlink()
                    except FileLockBusy:
                        pass
                    continue
                other = Path(record["root"])
                if not (self.root.is_relative_to(other) or other.is_relative_to(self.root)):
                    continue
                with self.locks.hold("command-acl-live:" + journal.stem, timeout=0):
                    if not self.revoke(record["paths"], record["sid"], record.get("logon")):
                        raise IsolationError("上次命令的权限范围清理未完成，已停止新命令，请保留状态记录。")
                    if record.get("profile"):
                        self.api.delete_profile(record["profile"])
                    journal.unlink()
            except FileLockBusy:
                # Live leases own distinct SIDs; never revoke another command.
                continue
            except IsolationError:
                raise
            except (ValueError, OSError, KeyError):
                raise IsolationError("无法核对上次命令的权限范围，已停止新命令。") from None

    def start(self, executable=None, environment=None):
        denied = self.plan()  # No host ACL changed until complete preflight.
        self.work = private_directory(state_directory() / "command-work" / self.identifier)
        self.temp = private_directory(self.work / "tmp")
        self.home = private_directory(self.work / "home")
        for path in (self.home / "AppData/Roaming", self.home / "AppData/Local/Packages" / self.profile.lower() / "AC/Temp",
                     self.home / ".cache", self.home / "go-cache"):
            private_directory(path)
        work_paths = [self.work, *self.work.rglob("*")]
        self.lease = self.locks.hold("command-acl-live:" + self.identifier, timeout=0)
        self.lease.__enter__()
        try:
            with self.locks.hold("command-acl-setup", timeout=60):
                self.recover_pending()
                from .command_reads import allow_runtime, runtime_roots
                self.read_sid = allow_runtime(self.api, runtime_roots(executable, self.root, environment, protected=self.protected), executable=executable)
                self.paths = [{"path": str(path), "identity": file_identity(path)} for path in (self.root, *work_paths, *denied)]
                for item in self.paths[:1+len(work_paths)]:
                    item["logon_before"] = self.api.capture_sid(item["path"], self.logon)
                for item in self.paths[1+len(work_paths):]:
                    path = Path(item["path"])
                    item["acl_before"] = self.api.acl_snapshot(path)
                    item["acl_expected"] = self.api.private_acl(item["acl_before"],
                        git_sid=self.sid if path == self.root / ".git" else None)
                self.save()  # Persist ownership and identities before any grant.
                self.api.create_profile(self.profile)
                self.api.restore_sid(self.root, self.sid, [self.api.allow_ace(self.sid, 0x1201BF).hex(),
                    self.api.allow_ace(self.sid, 0x1301BF, 0xB).hex()])
                for path in work_paths:
                    self.api.edit_acl(path, self.sid)
                # OWNER RIGHTS-only directories need an enabled normal SID as
                # well as the synthetic SID used by the restricted check.
                for path in (self.root, *work_paths):
                    self.api.edit_acl(path, self.logon)
                # Apply explicit exclusions after parent inheritance updates.
                for path in denied:
                    item = next(item for item in self.paths if item["path"] == str(path))
                    if file_identity(path) != item["identity"]:
                        raise IsolationError("保护路径在准备期间被替换，未执行命令。")
                    expected = item["acl_expected"]
                    self.api.set_acl(path, [bytes.fromhex(item) for item in expected["entries"]], protected=True)
            return self
        except BaseException:
            self.close()
            raise

    def launch(self, argv, environment, *, network=False):
        environment = dict(environment)
        # GetTempPath2 may redirect TEMP to the packaged directory prepared in
        # start(). All grants stay within this command's private work tree.
        environment.update(TEMP=str(self.temp), TMP=str(self.temp), HOME=str(self.home), USERPROFILE=str(self.home),
            APPDATA=str(self.home / "AppData/Roaming"), LOCALAPPDATA=str(self.home / "AppData/Local"),
            XDG_CACHE_HOME=str(self.home / ".cache"), GOCACHE=str(self.home / "go-cache"))
        import sys
        api_source = Path(__file__).with_name("windows_acl.py").read_text(encoding="utf-8")
        worker = Path(__file__).with_name("windows_command_worker.py").read_text(encoding="utf-8")
        self.launch_nonce = uuid.uuid4().hex
        config = {"sid": self.sid, "desktop": "DianStar-" + self.identifier, "root": str(self.root),
            "read_sid": self.read_sid,
            "powershell_runtime": Path(argv[0]).resolve() == Path(os.environ["SYSTEMROOT"], "System32/WindowsPowerShell/v1.0/powershell.exe").resolve(),
            "argv": list(map(str, argv)), "environment": environment, "network": network, "nonce": self.launch_nonce}
        return [sys.executable, "-I", "-S", "-c", api_source + "\n" + worker], json.dumps(config, ensure_ascii=False).encode()

    def close(self):
        try:
            if self.paths:
                with self.locks.hold("command-acl-setup", timeout=60):
                    self.cleanup_pending = not self.revoke(self.paths, self.sid, self.logon)
                    if not self.cleanup_pending:
                        self.api.delete_profile(self.profile)
                        self.journal.unlink(missing_ok=True)
                        self.paths = []
        except (ValueError, OSError):
            self.cleanup_pending = True
        finally:
            if self.lease is not None:
                self.lease.__exit__(None, None, None)
                self.lease = None


def write_scope(root, *, protected=(), executable=None, environment=None, managed=False, scanner=None):
    if os.name != "nt":
        raise IsolationError("当前平台还没有经过验收的命令隔离后端，已停止执行；不会降级为无限制运行。")
    return WindowsWriteScope(root, protected=protected, managed=managed, scanner=scanner).start(executable, environment)
