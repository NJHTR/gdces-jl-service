#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口。业务/Agent/路由已经拆到 bffpkg/ 里，这里只负责启动。

    python3 runtime/server.py

分包结构（对应 contracts/openapi.yaml 的 x-owner）：
    bffpkg/core.py   公共底座：配置、数据库、文本清洗
    bffpkg/biz.py    业务域：文书/项目/成员/审计/规范/统计  ← 未来抽 Java
    bffpkg/agent.py  Agent 运行时：任务编排/引擎/轨迹/记忆   ← 留 Python
    bffpkg/api.py    HTTP 路由
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 让 server.py 旁边能 import 到 bffpkg
for p in (ROOT, os.path.join(ROOT, "bff")):
    if p not in sys.path:
        sys.path.insert(0, p)

from bffpkg.api import main  # noqa: E402

if __name__ == "__main__":
    main()
