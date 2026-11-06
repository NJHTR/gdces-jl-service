# -*- coding: utf-8 -*-
"""把 data/homes/* 的 config.toml 补齐（幂等）。

为什么要单独跑：ensure_home 只在用户下次对话时执行，老用户永远不会被补到。
用法：python3 scripts/migrate-homes.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bff"))

from bffpkg.appserver import ensure_home  # noqa: E402

HOMES = os.path.join(ROOT, "data", "homes")


def main():
    n = 0
    for name in sorted(os.listdir(HOMES)):
        if not name.isdigit():
            continue
        ensure_home(int(name))
        n += 1
    print("已补齐 %d 个家目录" % n)


if __name__ == "__main__":
    main()
