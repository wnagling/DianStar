"""Inspect local project metadata without running Git or connecting a service."""
from pathlib import Path


def project_repositories(directory):
    """Find the opened project and, for a verified worktree, its main repo."""
    directory = Path(directory).absolute()
    try:
        if str(directory).startswith(("\\\\", "//")) or directory.resolve(strict=True) != directory:
            return ()
        for root in (directory, *directory.parents):
            marker = root / ".git"
            if marker.resolve() != marker.absolute():
                return ()
            if not marker.exists():
                continue
            if marker.is_dir():
                return (str(root),)
            if not marker.is_file() or marker.stat().st_size > 4096:
                return ()
            text = marker.read_text().strip()
            if not text.startswith("gitdir: "):
                return ()
            metadata = Path(text[8:])
            metadata = (root / metadata).absolute() if not metadata.is_absolute() else metadata.absolute()
            if metadata.resolve(strict=True) != metadata:
                return ()
            for name in ("commondir", "gitdir"):
                item = metadata / name
                if item.resolve() != item.absolute() or item.stat().st_size > 4096:
                    return ()
            common = (metadata / (metadata / "commondir").read_text().strip()).resolve(strict=True)
            main = common.parent
            if (common.name != ".git" or metadata.parent != common / "worktrees"
                    or common.resolve() != common.absolute()
                    or (metadata / "gitdir").read_text().strip().replace("\\", "/") != str(marker).replace("\\", "/")):
                return ()
            return (str(main), str(root))
    except (OSError, ValueError, UnicodeError):
        pass
    return ()
