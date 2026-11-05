#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whatison —— 这个服务到底有哪些路由、哪些表、哪些路径

为什么要有它：
  今天我有六次「查错东西」——
    用 POST 打 GET 的路由、用 audit 查 audit_log、把 data 当字典、
    拿不存在的 text 列查 task、用 800 字节判中文文档、用 0.47 秒推断 docker 慢。
  每次都是先猜、后验。这个脚本把「猜」变成「查」。

用法：
    python3 scripts/whatison.py            # 全部
    python3 scripts/whatison.py routes     # 只看路由
    python3 scripts/whatison.py tables     # 只看表结构
    python3 scripts/whatison.py paths      # 只看关键路径
"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def routes():
    """从 api.py 里抽出所有路由（HTTP 方法 + 路径 + 处理函数）"""
    path = os.path.join(ROOT, "bff", "bffpkg", "api.py")
    src = io.open(path, encoding="utf-8").read().splitlines()
    print("## 路由（来源：api.py，按源码顺序）")
    method = "?"
    for i, line in enumerate(src, 1):
        if "def do_GET" in line:
            method = "GET"
        elif "def do_POST" in line:
            method = "POST"
        elif "def do_PATCH" in line:
            method = "PATCH"
        elif "def do_DELETE" in line:
            method = "DELETE"
        m = re.search(r'path\s*==\s*"([^"]+)"', line)
        if m:
            print("  %-6s %s" % (method, m.group(1)))
            continue
        # 元组写法：if path in ("/a", "/b")
        m = re.search(r'path\s+in\s+\(([^)]*)\)', line)
        if m:
            for one in re.findall(r'"([^"]+)"', m.group(1)):
                print("  %-6s %s" % (method, one))
            continue
        m = re.search(r're\.match\(r"\^([^"]+)\$"', line)
        if m:
            print("  %-6s %s   （正则，见 api.py:%d）" % (method, m.group(1), i))


def tables():
    """表 → 列。避免再拿不存在的列去查"""
    env = os.path.join(ROOT, ".env")
    pw = ""
    for line in io.open(env, encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
            break
    sql = ("SELECT TABLE_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) "
           "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='jl' "
           "GROUP BY TABLE_NAME ORDER BY TABLE_NAME")
    try:
        out = subprocess.run(
            ["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw, "-N", "-e", sql],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception as exc:
        print("## 表结构：查询失败 %r" % (exc,))
        return
    print("## 表与列（来源：information_schema，数据库 jl）")
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        name, cols = line.split("\t", 1)
        print("  %-18s %s" % (name, cols))


def paths():
    """关键路径 + 权限：避免再猜文件在哪、谁能读"""
    print("## 关键路径")
    items = [
        ("BFF 入口", "runtime/entry.py"),
        ("旧版单体（勿用）", "runtime/server.py"),
        ("HTTP 模块", "bff/bffpkg/api.py"),
        ("业务域", "bff/bffpkg/biz.py"),
        ("Agent 运行时", "bff/bffpkg/agent.py"),
        ("公共底座", "bff/bffpkg/core.py"),
        ("服务侧任务", "bff/bffpkg/jobs.py"),
        ("知识包", "data/knowledge/"),
        ("项目工作区", "data/workspaces/"),
        ("个人记忆", "data/homes/<uid>/memories/"),
        ("影像", "data/blobs/"),
        ("启动脚本", "scripts/start-bff.sh"),
        ("自愈", "scripts/healthcheck.sh"),
        ("备份", "scripts/backup.sh"),
    ]
    for label, rel in items:
        full = os.path.join(ROOT, rel)
        if os.path.exists(full):
            mode = oct(os.stat(full).st_mode)[-3:]
            print("  %-14s %-28s 权限 %s" % (label, rel, mode))
        else:
            print("  %-14s %-28s 不存在" % (label, rel))


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "routes"):
        routes()
        print()
    if what in ("all", "tables"):
        tables()
        print()
    if what in ("all", "paths"):
        paths()


if __name__ == "__main__":
    main()
