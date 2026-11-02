"""PyInstaller 入口：启动与源码模式相同的 DianStar 命令行。"""

import hashlib
from importlib.resources import files
import sys


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--file-search-worker":
        source = files("dianstar.tools.local.files").joinpath("search_worker.txt").read_text(encoding="utf-8")
        if hashlib.sha256(source.encode()).hexdigest() != sys.argv[2]:
            return 2
        exec(compile(source, "file-search-worker", "exec"), {"__name__": "__main__"})
        return 0
    from dianstar.__main__ import main as application_main
    return application_main()


if __name__ == "__main__":
    raise SystemExit(main())
