"""Git hook bindings and adapters for exact index, tree and history objects."""
import re
from dianstar.security.content_guard import MAX_FILE, MAX_TOTAL, MAX_FILES, SecurityViolation, check_content
from .registry import HookRegistration


def check_publishable_content(context, path, data):
    return check_content(context.scanner, path, data)


def registrations():
    return (HookRegistration("git.content", ("before_stage", "before_commit", "before_push", "after_clone"),
                             check_publishable_content, priority=0),)


class GitSecurityHooks:
    def __init__(self, workspace):
        from .runner import HookRunner
        self.workspace = workspace
        self.hooks = HookRunner()

    def _blobs(self, context, oids, cache):
        missing = list(dict.fromkeys(oid for oid in oids if oid not in cache))
        if not missing:
            return
        if len(missing) > MAX_FILES or any(not re.fullmatch(r"[a-f0-9]{40,64}", oid) for oid in missing):
            raise SecurityViolation(incomplete=True)
        request = ("\n".join(missing) + "\n").encode("ascii")
        checked = self.workspace._query(context, ["cat-file", "--batch-check"], input_data=request).splitlines()
        sizes = []
        if len(checked) != len(missing):
            raise SecurityViolation(incomplete=True)
        for oid, header in zip(missing, checked):
            fields = header.split()
            if len(fields) != 3 or fields[0].decode() != oid or fields[1] != b"blob":
                raise SecurityViolation(incomplete=True)
            size = int(fields[2])
            if size > MAX_FILE or size < 0:
                raise SecurityViolation(incomplete=True)
            sizes.append(size)
        if sum(sizes) + sum(map(len, cache.values())) > MAX_TOTAL:
            raise SecurityViolation(incomplete=True)
        raw = self.workspace._query(context, ["cat-file", "--batch"], input_data=request,
                                    output_limit=MAX_TOTAL + MAX_FILES * 100)
        offset = 0
        for oid, size in zip(missing, sizes):
            end = raw.find(b"\n", offset)
            if end < 0 or raw[offset:end] != f"{oid} blob {size}".encode():
                raise SecurityViolation(incomplete=True)
            offset = end + 1
            data = raw[offset:offset + size]
            if len(data) != size or raw[offset + size:offset + size + 1] != b"\n":
                raise SecurityViolation(incomplete=True)
            cache[oid] = data
            offset += size + 1
        if offset != len(raw):
            raise SecurityViolation(incomplete=True)

    def index(self, context):
        def files():
            raw = self.workspace._query(context, ["ls-files", "--stage", "-z"])
            entries = []
            for entry in raw.split(b"\0"):
                if not entry:
                    continue
                metadata, path = entry.split(b"\t", 1)
                mode, oid, stage = metadata.split()
                if stage != b"0":
                    raise SecurityViolation(incomplete=True)
                if mode != b"160000":
                    entries.append((path.decode("utf-8"), oid.decode("ascii")))
            cache = {}
            self._blobs(context, [oid for _, oid in entries], cache)
            for path, oid in entries:
                yield path, cache[oid]
        try:
            return self.hooks.run("before_commit", files())
        except SecurityViolation as error:
            raise SecurityViolation(error.findings, incomplete=error.code == "security_scan_incomplete",
                                    source="git_index") from None

    def tree_files(self, context, tree, seen=None, cache=None):
        seen = seen if seen is not None else set()
        cache = cache if cache is not None else {}
        raw = self.workspace._query(context, ["ls-tree", "-r", "-z", "--full-tree", tree])
        entries = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind != b"blob":
                continue
            binding = (path, oid)
            if binding not in seen:
                seen.add(binding)
                entries.append((path.decode("utf-8"), oid.decode("ascii")))
        self._blobs(context, [oid for _, oid in entries], cache)
        for path, oid in entries:
            yield path, cache[oid]

    def tree(self, context, tree):
        return self.hooks.run("before_commit", self.tree_files(context, tree))

    def remote_head(self, context, url, target):
        network = self.workspace._network_context(context, url)
        raw = self.workspace._query(network, ["ls-remote", "--heads", "--", url, "refs/heads/" + target], network=True)
        rows = raw.decode("ascii").splitlines()
        if not rows:
            return ""
        if len(rows) != 1:
            raise SecurityViolation(incomplete=True)
        oid, ref = rows[0].split()
        if ref != "refs/heads/" + target or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", oid):
            raise SecurityViolation(incomplete=True)
        return oid

    def push(self, context, head, remote_head):
        def files():
            # Missing ancestors must never be interpreted as clean history.
            shallow = self.workspace._query(context, ["rev-parse", "--is-shallow-repository"]).strip()
            if shallow != b"false":
                raise SecurityViolation(incomplete=True)
            exclusions = []
            if remote_head and self.workspace._invoke(context, ["cat-file", "-e", remote_head + "^{commit}"], optional=True)[0] == 0:
                exclusions = ["^" + remote_head]
            # If the advertised remote commit is not local, scan the full local history.
            commits = self.workspace._query(context, ["rev-list", "--max-count=501", head, *exclusions]).decode("ascii").splitlines()
            if len(commits) > 500:
                raise SecurityViolation(incomplete=True)
            seen, cache = set(), {}
            for commit in commits:
                yield "[提交说明]", self.workspace._query(context, ["show", "-s", "--format=%B", commit])
                yield from self.tree_files(context, commit, seen, cache)
        return self.hooks.run("before_push", files())
