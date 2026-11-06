# -*- coding: utf-8 -*-
"""知识包同步：data/knowledge/standards/*.md → standard 表

知识包是文件（可 review、可 git 回溯），库只是索引。这里的规矩：
- **standard 表只有一个写者：这个模块**
- 按 (code, clause) upsert，重复执行不会产生重复行
- 文件删了不自动删库里的行（删数据太危险，要人工确认）
"""
import glob
import io
import os
import re
import sys

from .core import exec_sql, q, ROOT

STD_DIR = os.path.join(ROOT, "data", "knowledge", "standards")

H1 = re.compile(r"^#\s+(.+?)\s*$")
H2 = re.compile(r"^##\s+([0-9]+(?:\.[0-9]+)*)\s*(.*?)\s*$")
VER = re.compile(r"^version:\s*(\S+)\s*$", re.I)
SEP = re.compile(r"^·\s*|\s+·\s+")


def parse_file(path):
    """解析一个规范文件 → (code, version, std_title, [(clause, title, body), ...])"""
    code, version, std_title = None, None, None
    items, cur, body = [], None, []

    def flush():
        if cur:
            items.append((cur[0], cur[1], "\n".join(body).strip()))

    with io.open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = VER.match(line.strip())
            if m:
                version = m.group(1)
                continue
            m = H1.match(line)
            if m and code is None:
                parts = SEP.split(m.group(1))
                code = parts[0].strip()
                # 一级标题形如「GB 50204-2015 · 混凝土结构工程施工质量验收规范」
                if len(parts) > 1:
                    std_title = parts[1].strip()
                continue
            m = H2.match(line)
            if m:
                flush()
                cur, body = (m.group(1), m.group(2) or ""), []
                continue
            if cur is not None:
                body.append(line)
    flush()
    return code, version, std_title, items


def sync_standards(dry_run=False, verbose=True):
    """把知识包同步进标准库，返回 (新增, 更新, 跳过)"""
    files = sorted(glob.glob(os.path.join(STD_DIR, "*.md")))
    new = upd = skip = 0
    for path in files:
        code, version, std_title, items = parse_file(path)
        if not code or not items:
            if verbose:
                sys.stderr.write("[knowledge] 跳过格式不对的文件：%s\n" % os.path.basename(path))
            continue
        for clause, ctitle, body in items:
            if not body:
                skip += 1
                continue
            # 正文末尾常带一句运维备注（"（导入自原数据库记录，条号与原文未经…复核。）"）。
            # 它不是条文，模型会把它当正文抄进回答 —— 剥出来单独存。
            _note = ""
            _i = body.rfind("（导入自")
            if _i > 0 and body.rstrip().endswith("）"):
                _note = body[_i:].strip()
                body = body[:_i].rstrip()

            exists = q("SELECT id FROM standard WHERE code=%s AND clause=%s", (code, clause))
            if dry_run:
                if verbose:
                    print("  %s %s %s → %s" % (code, clause, ctitle, "更新" if exists else "新增"))
                continue
            if exists:
                exec_sql("UPDATE standard SET title=%s, std_title=%s, body=%s, version=%s, "
                         "review_note=%s WHERE id=%s",
                         (ctitle, std_title, body, version, _note or None, exists[0]["id"]))
                upd += 1
            else:
                exec_sql("INSERT INTO standard (code, clause, title, std_title, body, version, "
                         "review_note) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (code, clause, ctitle, std_title, body, version, _note or None))
                new += 1
    if verbose and not dry_run:
        total = q("SELECT COUNT(*) AS n FROM standard")[0]["n"]
        sys.stderr.write("[knowledge] 规范同步：新增 %d、更新 %d、跳过 %d，库里共 %s 条\n"
                         % (new, upd, skip, total))
    return new, upd, skip


# ---------------------------------------------------------------- install_skills
SKILLS_SRC = os.path.join(ROOT, "data", "knowledge", "skills")
HOMES_ROOT = os.environ.get("JL_HOMES_ROOT", os.path.join(ROOT, "data", "homes"))


def skill_names():
    """组织技能有哪些（data/knowledge/skills/<name>/SKILL.md）"""
    out = []
    try:
        entries = sorted(os.listdir(SKILLS_SRC))
    except FileNotFoundError:
        return out          # 没有技能目录就是没有，调用方会看到空列表
    for name in entries:
        if os.path.isfile(os.path.join(SKILLS_SRC, name, "SKILL.md")):
            out.append(name)
    return out


def install_skills(project_ids=None, user_ids=None, verbose=False):
    """把组织技能装到引擎真正会扫的两个作用域。

    组织技能（data/knowledge/skills）是唯一源；这里只是安装，不是写知识。
      Repo scope: <ws>/.easycode/skills/<name>/SKILL.md
      User scope: <homes>/<uid>/skills/<name>/SKILL.md

    为什么必须装：引擎只在上述目录里找 SKILL.md（ext/skills/src/host_roots.rs）。
    文件放在 data/knowledge 下它是看不到的。
    """
    names = skill_names()
    if not names:
        return {"installed": 0, "names": [], "note": "组织技能目录为空"}

    targets = []
    for pid in (project_ids or []):
        targets.append(os.path.join(ROOT, "data", "workspaces", str(pid), ".easycode", "skills"))
    for uid in (user_ids or []):
        targets.append(os.path.join(HOMES_ROOT, str(uid), "skills"))

    changed = 0
    for root in targets:
        for name in names:
            src = os.path.join(SKILLS_SRC, name, "SKILL.md")
            dst_dir = os.path.join(root, name)
            dst = os.path.join(dst_dir, "SKILL.md")
            try:
                with io.open(src, encoding="utf-8") as f:
                    want = f.read()
            except OSError as e:
                sys.stderr.write("[skills] 读源失败 %s: %s\n" % (src, e))
                continue
            have = ""
            try:
                with io.open(dst, encoding="utf-8") as f:
                    have = f.read()
            except OSError:
                have = ""       # 目标不存在 = 需要安装
            if have == want:
                continue
            try:
                os.makedirs(dst_dir, exist_ok=True)
                io.open(dst, "w", encoding="utf-8", newline="\n").write(want)
                changed += 1
                if verbose:
                    print("  [skills] %s ← %s" % (dst.replace(ROOT + os.sep, ""), name))
            except OSError as e:
                sys.stderr.write("[skills] 写失败 %s: %s\n" % (dst, e))

    return {"installed": changed, "names": names, "targets": len(targets)}
