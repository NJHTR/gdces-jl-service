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

import io as _io
import os as _os
import re as _re

HOMES = os.path.join(ROOT, "data", "homes")


def main():
    n = 0
    was_on = 0
    for name in sorted(os.listdir(HOMES)):
        if not name.isdigit():
            continue
        cfg = os.path.join(HOMES, name, "config.toml")
        if os.path.isfile(cfg) and "memories = true" in io.open(cfg, encoding="utf-8").read():
            was_on += 1
        ensure_home(int(name))
        n += 1
    print("已处理 %d 个家目录（其中原本 memories=true 的 %d 个 → 现在默认 false）"
          % (n, was_on))


if __name__ == "__main__":
    main()
