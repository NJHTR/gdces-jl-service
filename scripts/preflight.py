#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight —— 每轮收尾前自检

起因：连续两轮都埋了同一类雷 —— 调用了不存在的东西。

  1. 前端 index.js 里写了 this._reviewOf(...) 却没定义 → 任务一完成就 TypeError
  2. 后端 agent.py 里调了 validate_fields(...)，但 import 行没有它
     → NameError 被 except 吞掉 → 复核员被告知「缺项：无」

两者语法都合法、服务都能起来，**只有真跑到那一步才炸**。
这个脚本把这类问题在提交前抓出来。

检查项：
  A. Python 语法（import 级别，不是编译字节码）
  B. Python：函数体里调用的名字，是否在「模块级可见」的集合里
  C. JS：this._xxx(...) 的每个 xxx 是否有定义
  D. 硬编码体检（只能降不能升，基线记在 docs/STATUS.md）
  E. 各项目 AGENTS.md 的体量（判据：≤1200 字且不含工具清单/说话方式）

用法：
    python3 scripts/preflight.py            # 全跑
    python3 scripts/preflight.py --quiet    # 只报问题
"""
import ast
import builtins
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUIET = "--quiet" in sys.argv

problems = []
notes = []

# Windows 控制台默认 GBK，输出中文/符号会炸 —— 强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def say(line):
    if not QUIET:
        print(line)


def add(msg):
    problems.append(msg)


# ---------------------------------------------------------------- A + B: Python
def module_visible_names(tree, path):
    """模块级可见的名字：import、def、class、赋值、以及 __all__ 里的"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def local_names(func):
    """函数内可见的局部名：参数、赋值、for、with、except、comprehension"""
    names = set()
    args = func.args
    for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.comprehension):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def check_python(root_dir):
    bad = 0
    for dirpath, _dirs, files in os.walk(root_dir):
        if "__pycache__" in dirpath or "/repos/" in dirpath.replace("\\", "/"):
            continue
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            raw = io.open(path, encoding="utf-8", errors="replace").read()
            try:
                tree = ast.parse(raw, filename=path)
            except SyntaxError as e:
                add("%s:%s 语法错误：%s" % (rel, e.lineno, e.msg))
                bad += 1
                continue

            module_names = module_visible_names(tree, path)
            builtin_names = set(dir(builtins))

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                visible = module_names | builtin_names | local_names(node)
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    fn_node = sub.func
                    if isinstance(fn_node, ast.Name):
                        name = fn_node.id
                        if name not in visible:
                            add("%s:%s 调用了未定义/未导入的 %s()" % (rel, sub.lineno, name))
                            bad += 1
    say("  [Python] 检查完成，问题 %d 处" % bad)
    return bad


# ---------------------------------------------------------------- C: JS
def check_js(js_root):
    bad = 0
    for dirpath, _dirs, files in os.walk(js_root):
        if "node_modules" in dirpath or "miniprogram_npm" in dirpath:
            continue
        for fn in sorted(files):
            if not fn.endswith(".js"):
                continue
            path = os.path.join(dirpath, fn)
            raw = io.open(path, encoding="utf-8", errors="replace").read()
            calls = set(re.findall(r"this\.(_[A-Za-z0-9_]+)\s*\(", raw))
            if not calls:
                continue
            defs = set(re.findall(r"(?m)^\s{2,6}(_[A-Za-z0-9_]+)\s*\(", raw))
            for c in sorted(calls - defs):
                add("%s: this.%s() 没有定义" % (os.path.relpath(path, js_root), c))
                bad += 1
    say("  [JS] 检查完成，问题 %d 处" % bad)
    return bad


# ---------------------------------------------------------------- D: 硬编码
def check_hardcode():
    base = 11
    pat = re.compile(r"re\.search|re\.match")
    skip = re.compile(r"urlparse|re\.match\(r\"\^/api")
    hits = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "bff", "bffpkg")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            for i, line in enumerate(io.open(p, encoding="utf-8", errors="replace"), 1):
                if pat.search(line) and not skip.search(line):
                    hits.append((os.path.relpath(p, ROOT), i, line.strip()[:80]))
    n = len(hits)
    if n > base:
        add("硬编码体检：%d 处 > 基线 %d（新增的正则/意图判断）" % (n, base))
    say("  [硬编码] %d 处（基线 %d）" % (n, base))
    return n


# ---------------------------------------------------------------- E: AGENTS.md
def check_agents():
    ws = os.path.join(ROOT, "data", "workspaces")
    if not os.path.isdir(ws):
        return 0
    bad = 0
    for proj in sorted(os.listdir(ws)):
        p = os.path.join(ws, proj, "AGENTS.md")
        if not os.path.isfile(p):
            continue
        size = os.path.getsize(p)
        raw = io.open(p, encoding="utf-8", errors="replace").read()
        flag = ""
        chars = len(raw)
        if chars > 1200:
            flag += " 超长(%d 字)" % chars
            add("AGENTS.md %s：%d 字 > 1200（%d 字节）" % (proj, chars, size))
            bad += 1
        for marker in ("【可用工具】", "【说话方式】", "jl-tool list"):
            if marker in raw:
                flag += " 含行为说明(%s)" % marker
                add("AGENTS.md %s：含行为说明 %s（应该搬进 skills）" % (proj, marker))
                bad += 1
        say("  [AGENTS] %s：%d 字节 / %d 字%s" % (proj, size, len(raw), flag))
    return bad


def main():
    say("preflight —— 提交前自检")
    total = 0
    total += check_python(os.path.join(ROOT, "bff"))
    total += check_python(os.path.join(ROOT, "scripts"))
    js_root = os.environ.get("JL_MINIAPP") or r"C:\Users\NJHTR\WeChatProjects\jl"
    if os.path.isdir(js_root):
        total += check_js(js_root)
    else:
        say("  [JS] 跳过（找不到小程序目录）")
    check_hardcode()
    total += check_agents()

    print("")
    if problems:
        print("发现 %d 个问题：" % len(problems))
        for p in problems:
            print("  X " + p)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
