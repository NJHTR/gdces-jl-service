#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审一遍：哪些路由的处理函数里没有权限校验

背景：member_of/guard_project 存在，但只在 20 处被调用。
如果某个写接口（改文书、上传、发消息）没校验，开了鉴权之后就是越权口子。
AUTH_REQUIRED=0 时全部放行，所以现在看不出问题 —— 正是要靠静态检查发现。

注意：第一版脚本按「全局扫 handler 名再和路由 zip」对齐，结果只匹配到 1 条
就宣称「全部做了校验」—— 假阴性。改成「从路由行往后找处理函数」，不再全局对齐。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "bff", "bffpkg")


def load():
    src = {}
    for fn in os.listdir(PKG):
        if fn.endswith(".py"):
            src[fn] = io.open(os.path.join(PKG, fn), encoding="utf-8").read()
    return src


def routes_with_handlers(api_src):
    """从路由行往后找 3 行内的 handle_* 调用"""
    lines = api_src.splitlines()
    out = []
    method = "?"
    for i, line in enumerate(lines):
        if "def do_GET" in line:
            method = "GET"
        elif "def do_POST" in line:
            method = "POST"
        elif "def do_PATCH" in line:
            method = "PATCH"
        elif "def do_DELETE" in line:
            method = "DELETE"

        path = None
        m = re.search(r'path\s*==\s*"([^"]+)"', line)
        if m:
            path = m.group(1)
        else:
            m = re.search(r're\.match\(r"\^([^"]+)\$"', line)
            if m:
                path = m.group(1) + "（正则）"
        if not path:
            continue

        handler = None
        for j in range(i, min(i + 4, len(lines))):
            m2 = re.search(r"\b(handle_[a-zA-Z0-9_]+)\s*\(", lines[j])
            if m2:
                handler = m2.group(1)
                break
        if not handler:
            m3 = re.search(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", line)
            handler = m3.group(1) if m3 else "?"
        out.append((method, path, handler))
    return out


def body_of(src, name):
    for fn, text in src.items():
        m = re.search(r"^def %s\(" % re.escape(name), text, re.M)
        if not m:
            continue
        start = m.start()
        nxt = re.search(r"^def [a-zA-Z_]", text[start + 1:], re.M)
        end = start + 1 + nxt.start() if nxt else len(text)
        return fn, text[start:end]
    return None, None


def main():
    src = load()
    routes = routes_with_handlers(src.get("api.py", ""))
    print("## 路由 / 处理函数 / 权限校验（共 %d 条）" % len(routes))
    print()
    unchecked, unknown = [], []
    for method, path, handler in routes:
        fn, body = body_of(src, handler)
        if not body:
            unknown.append((method, path, handler))
            print("  %-6s %-40s %-22s ? 找不到函数定义" % (method, path, handler))
            continue
        guarded = "guard_project(" in body or "member_of(" in body
        print("  %-6s %-40s %-22s %s" % (method, path, handler, "校验" if guarded else "—"))
        if method in ("POST", "PATCH", "DELETE") and not guarded:
            unchecked.append((method, path, handler))

    print()
    if unchecked:
        print("## 写接口没有项目级权限校验：%d 个" % len(unchecked))
        for method, path, handler in unchecked:
            print("  %-6s %-40s %s" % (method, path, handler))
    else:
        print("## 所有写接口都做了权限校验")

    if unknown:
        print()
        print("## 提取不到处理函数的：%d 个（脚本需改进，不是服务问题）" % len(unknown))


if __name__ == "__main__":
    main()
