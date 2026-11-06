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
    fixed = 0
    for name in sorted(os.listdir(HOMES)):
        if not name.isdigit():
            continue
        # 先修"无引号字符串"：TOML 里 extract_model = qwen3.7-plus 是非法值，
        # 引擎会整份配置回落到默认（MCP 工具、记忆全没了，出稿 1 秒空跑）
        cfg = _os.path.join(HOMES, name, "config.toml")
        if _os.path.isfile(cfg):
            txt = _io.open(cfg, encoding="utf-8").read()
            new = _re.sub(r'(?m)^(extract_model|consolidation_model)\s*=\s*([A-Za-z0-9._-]+)\s*$',
                          r'\1 = "\2"', txt)
            # phase2 的合并模型不能用主模型（写不出合法 apply_patch）→ 统一换成复核模型
            from bffpkg.core import ENV
            cons = ENV.get("REVIEW_MODEL") or ENV.get("LLM_FALLBACK_MODEL") or "qwen3.8-flash"
            new = _re.sub(r'(?m)^consolidation_model\s*=\s*"[^"]*"\s*$',
                          'consolidation_model = "%s"' % cons, new)
            if new != txt:
                _io.open(cfg, "w", encoding="utf-8").write(new)
                fixed += 1
        ensure_home(int(name))
        n += 1
    print("已补齐 %d 个家目录（修好无引号字符串 %d 个）" % (n, fixed))


if __name__ == "__main__":
    main()
