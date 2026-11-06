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
  F. 静默失败（except 后只有 pass 的地方）只能降不能升
  E. 各项目 AGENTS.md 的体量（判据：≤1200 字且不含工具清单/说话方式）

用法：
    python3 scripts/preflight.py            # 全跑
    python3 scripts/preflight.py --quiet    # 只报问题
"""
import ast
import builtins
import io
import json
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



# ---------------------------------------------------------------- F: 静默失败
def check_silent_except():
    """数「except 后面只有 pass」的地方。

    不是每一处都错（清理文件、杀进程时可以不管），
    但这个数只能降不能升 —— 涨了就说明有人又在藏失败。
    """
    # 13 处已逐个看过，都是可以不管的：
    #   FileNotFoundError ×4  删临时文件/清理
    #   杀进程/终止进程 ×3    p.kill() / proc.terminate() / thread/close
    #   调试转储 ×2           写 /tmp 日志
    #   其余 ×4               命中计数、进度更新、列 schema、读消息兜底
    # 涨了就要解释：新加的 except+pass 是不是在藏失败。
    base = 13
    n = 0
    spots = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "bff", "bffpkg")):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            lines = io.open(p, encoding="utf-8", errors="replace").read().splitlines()
            for i, line in enumerate(lines):
                if re.match(r"\s*except[^:]*:\s*$", line):
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if nxt == "pass":
                        n += 1
                        spots.append("%s:%d" % (os.path.relpath(p, ROOT), i + 1))
    if n > base:
        add("静默失败：%d 处 except+pass > 基线 %d（%s）" % (n, base, ", ".join(spots)))
    say("  [静默失败] %d 处 except+pass（基线 %d）" % (n, base))
    return n

def check_imports():
    """真 import 一次 bffpkg.api。

    为什么加这条：删掉一个函数后忘了改 api.py 的 import 列表，
    语法检查、未定义检查都过得去，但服务起不来（实测踩过）。
    真 import 一次能把这类错误一次抓完。
    """
    import subprocess

    # core.py 的 ROOT 是服务器绝对路径（读 .env）；不在服务器上就没法真 import，
    # 这条检查交给服务器跑（devloop 每天 09:00 会跑 preflight）。
    if not os.path.isdir("/home/baize/jl-service"):
        say("  [导入] 跳过（不在服务器上；服务器 devloop 每天会跑）")
        return 0

    # 本机可能没装 pymysql（只有服务器装）：插个桩，让 import 能走完。
    # 我们抓的是「删了函数忘改 import」，不是数据库能不能连。
    code = (
        "import sys, types\n"
        "sys.path.insert(0, %r)\n"
        "try:\n"
        "    import pymysql\n"
        "except ImportError:\n"
        "    m = types.ModuleType('pymysql')\n"
        "    c = types.ModuleType('pymysql.cursors')\n"
        "    c.DictCursor = object\n"
        "    m.cursors = c\n"
        "    m.connect = lambda *a, **k: None\n"
        "    sys.modules['pymysql'] = m\n"
        "    sys.modules['pymysql.cursors'] = c\n"
        "import bffpkg.api\n"
    ) % os.path.join(ROOT, "bff")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else "import 失败"
        add("导入失败：%s" % detail)
        say("  [导入] 失败：%s" % detail)
        return 1
    say("  [导入] bffpkg.api 可导入")
    return 0


def check_templates():
    """表式模板与映射必须自洽：文件在、标签在模板里找得到。

    这是"格式保真"的地基：模板被换掉/字段名改了，生成时才发现就太晚了。
    """
    tdir = os.path.join(ROOT, "data", "knowledge", "templates", "docx")
    if not os.path.isdir(tdir):
        return [("模板目录不存在: %s" % tdir, "templates")]
    try:
        from docx import Document
    except Exception:
        return []          # 没装 python-docx 就跳过（服务器上装了）
    out = []
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".fields.json"):
            continue
        fp = os.path.join(tdir, fn)
        try:
            spec = json.load(io.open(fp, encoding="utf-8"))
        except (OSError, ValueError) as e:
            out.append(("%s 解析失败: %r" % (fn, e), "模板映射"))
            continue
        tpl = os.path.join(tdir, spec.get("template") or "")
        if not os.path.isfile(tpl):
            out.append(("%s 指向的模板不存在: %s" % (fn, spec.get("template")), "模板映射"))
            continue
        doc = Document(tpl)
        text = "\n".join([p.text for p in doc.paragraphs] +
                          [c.text for t in doc.tables for r in t.rows for c in r.cells])
        for lb in spec.get("fixed_labels") or []:
            if lb not in text:
                out.append(("%s 的固定标签在模板里找不到: %s" % (fn, lb), "模板映射"))
        for slot in spec.get("slots") or []:
            lb = slot.get("label")
            if lb and slot.get("target") != "para" and lb not in text:
                out.append(("%s 的槽位标签找不到: %s" % (fn, lb), "模板映射"))
    return out


def check_memory_single_writer():
    """rules.md 只能由 render_rules_md 渲染，不许再"追加一行"。

    以前创建记忆时"往文件追加一行 + 同时写库"，两处各写各的（改库文件不动）。
    """
    fp = os.path.join(ROOT, "bff", "bffpkg", "biz.py")
    if not os.path.isfile(fp):
        return []
    src = io.open(fp, encoding="utf-8").read()
    bad = []
    for line in src.splitlines():
        if "rules.md" in line and ('"a"' in line or "'a'" in line or "\"a\"" in line):
            bad.append(line.strip())
    # 允许 render_rules_md 内部写文件（它是渲染器）
    if bad:
        return [("rules.md 被追加写入: %s" % bad[0], "记忆写者")]
    return []


def check_exports_dir():
    """导出目录必须存在且可写（报告/表式都落在这里）。"""
    if not os.path.isdir(os.path.join(ROOT, "data", "workspaces")):
        return []           # 本地仓库没有运行数据（工作区/导出只在服务器上）
    d = os.path.join(ROOT, "data", "exports")
    out = []
    if not os.path.isdir(d):
        out.append(("data/exports 不存在", "导出"))
        return out
    probe = os.path.join(d, ".preflight-write-test")
    try:
        with io.open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        out.append(("data/exports 不可写: %r" % (e,), "导出"))
    return out


def check_ws_gitignore_code():
    """工作区 .gitignore 必须由代码保证（不是手工在服务器上加的）。"""
    fp = os.path.join(ROOT, "bff", "bffpkg", "biz.py")
    if not os.path.isfile(fp):
        return []
    src = io.open(fp, encoding="utf-8").read()
    if "def ensure_ws_gitignore(" in src and "ensure_ws_gitignore(project_id)" in src:
        return []
    return [("工作区 .gitignore 没有代码保证", "biz.py")]


def check_memories_path():
    """readMemories 不能只读旧的 memories/project.md（那个文件早就不写了）。"""
    fp = os.path.join(ROOT, "tools", "src", "workspace.js")
    if not os.path.isfile(fp):
        return []
    src = io.open(fp, encoding="utf-8").read()
    if 'records", "rules.md"' in src or "records/rules.md" in src:
        return []
    return [("readMemories 还只读 memories/project.md", "workspace.js")]


def check_duplicate_constants():
    """core.py 的模块级常量不许定义两次。

    实测：7 组常量在文件里写了两遍（AUTH_REQUIRED/TOKEN_TTL_HOURS/BLOB_ROOT/
    WEEK/STATE_TO_ST/KIND_TO_ICON/KIND_TO_TINT），改一处漏一处。
    """
    fp = os.path.join(ROOT, "bff", "bffpkg", "core.py")
    if not os.path.isfile(fp):
        return []
    src = io.open(fp, encoding="utf-8").read()
    names = re.findall(r"(?m)^([A-Z][A-Z0-9_]{2,})\s*=", src)
    dup = sorted({n for n in names if names.count(n) > 1})
    return [("重复定义: %s" % ", ".join(dup), "core.py")] if dup else []


def check_dead_endpoints():
    """禁回退：E2 删掉的两个端点与三个 handler 不许再出现。

    它们要么没人调用（/members），要么功能已被自动渲染取代（render-memory），
    留着只会让人误以为还有这条链路。
    """
    bad = ["/api/v1/members", "/render-memory",
           "handle_members", "handle_member_add", "handle_render_memory"]
    hits = []
    for rel in ("bff/bffpkg/api.py", "bff/bffpkg/biz.py",
                "bff/bffpkg/core.py", "bff/bffpkg/agent.py"):
        fp = os.path.join(ROOT, rel)
        if not os.path.isfile(fp):
            continue
        src = io.open(fp, encoding="utf-8").read()
        for b in bad:
            if b in src:
                hits.append("%s 里还有 %s" % (rel, b))
    return [(h, "死端点") for h in hits]


def check_tools_wiring():
    """server.js 的 import 名字必须覆盖 HANDLERS 用到的函数。

    实测踩过：HANDLERS 加了 proposeRule，import 没加 → jl-tool 一调用就崩
    "proposeRule is not defined"，模型只能空跑（出稿 doc=None）。
    """
    fp = os.path.join(ROOT, "tools", "src", "server.js")
    if not os.path.isfile(fp):
        return []
    src = io.open(fp, encoding="utf-8").read()
    m = re.search(r'import \{([^}]+)\} from "\./tools\.js"', src)
    imported = {x.strip() for x in (m.group(1).split(",") if m else []) if x.strip()}
    h = re.search(r"const HANDLERS = \{(.*?)\};", src, re.S)
    # HANDLERS 形如 `get_project: getProject,` —— 要比的是**值**（函数名）
    used = set(re.findall(r"\w+\s*:\s*(\w+)", h.group(1))) if h else set()
    missing = sorted(used - imported)
    return [("tools 接线缺失: %s" % ", ".join(missing), "server.js")] if missing else []


def check_architecture():
    """架构不变量（docs/ARCHITECTURE-FINAL.md）：一件事只能有一个实现。

    这几条都是真出过问题的地方：
      · 两套建文书路径（老的那条绕过守卫）→ 现在只允许 register 一条
      · doc 表多个写者 → 只允许 register 一处 INSERT
    """
    import glob
    import io as _io
    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    pkg = _os.path.join(root, "bff", "bffpkg")
    problems = []

    files = sorted(glob.glob(_os.path.join(pkg, "*.py")))
    text = {}
    for f in files:
        text[f] = _io.open(f, encoding="utf-8", errors="replace").read()

    # 1) 老的单体/老路径不许回来
    for f, t in text.items():
        if "def handle_doc_create" in t:
            problems.append("%s 里又出现了 handle_doc_create（POST /docs 已下线）" % _os.path.basename(f))

    # 2) doc 表只有一个 INSERT
    hits = [( _os.path.basename(f), t.count("INSERT INTO doc ")) for f, t in text.items()]
    total = sum(n for _, n in hits)
    if total != 1:
        problems.append("INSERT INTO doc 出现 %d 次（应为 1 次）：%s"
                        % (total, [h for h in hits if h[1]]))

    # 3) 解析点不许再用下划线字段（引擎协议是驼峰）
    # 只查"读字段"的写法：it.get("aggregated_output") / it.get("exit_code")
    for f, t in text.items():
        for bad, good in (('it.get("aggregated_output")', "it.get(\"aggregatedOutput\")"),
                          ('it.get("exit_code")', "it.get(\"exitCode\")")):
            if bad in t:
                problems.append("%s 用了 %s，应为 %s"
                                % (_os.path.basename(f), bad, good))

    return problems


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
    check_silent_except()
    total += check_agents()
    total += check_imports()

    print("")
    # 这些检查以前写了却没挂进来（main 只调 check_architecture）——
    # 等于白写。统一在这里逐个跑。
    checks = [
        check_architecture, check_tools_wiring, check_dead_endpoints,
        check_duplicate_constants, check_memories_path, check_ws_gitignore_code,
        check_templates, check_memory_single_writer, check_exports_dir,
    ]
    for fn in checks:
        try:
            problems.extend(fn())
        except Exception as e:
            problems.append("检查 %s 自己崩了: %r" % (fn.__name__, e))

    if problems:
        print("发现 %d 个问题：" % len(problems))
        for p in problems:
            if isinstance(p, tuple):
                msg = p[0]
                where = p[1] if len(p) > 1 else ""
                print("  X %s%s" % (msg, ("（%s）" % where) if where else ""))
            else:
                print("  X " + str(p))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
