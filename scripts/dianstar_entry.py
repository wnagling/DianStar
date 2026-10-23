"""PyInstaller 入口：启动与源码模式相同的 DianStar 命令行。"""

from dianstar.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
