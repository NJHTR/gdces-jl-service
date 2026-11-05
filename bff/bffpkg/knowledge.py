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
    """解析一个规范文件 → (code, version, [(clause, title, body), ...])"""
    code, version = None, None
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
                continue
            m = H2.match(line)
            if m:
                flush()
                cur, body = (m.group(1), m.group(2) or ""), []
                continue
            if cur is not None:
                body.append(line)
    flush()
    return code, version, items


def sync_standards(dry_run=False, verbose=True):
    """把知识包同步进标准库，返回 (新增, 更新, 跳过)"""
    files = sorted(glob.glob(os.path.join(STD_DIR, "*.md")))
    new = upd = skip = 0
    for path in files:
        code, version, items = parse_file(path)
        if not code or not items:
            if verbose:
                sys.stderr.write("[knowledge] 跳过格式不对的文件：%s\n" % os.path.basename(path))
            continue
        for clause, ctitle, body in items:
            if not body:
                skip += 1
                continue
            exists = q("SELECT id FROM standard WHERE code=%s AND clause=%s", (code, clause))
            if dry_run:
                if verbose:
                    print("  %s %s %s → %s" % (code, clause, ctitle, "更新" if exists else "新增"))
                continue
            if exists:
                exec_sql("UPDATE standard SET title=%s, body=%s, version=%s WHERE id=%s",
                         (ctitle, body, version, exists[0]["id"]))
                upd += 1
            else:
                exec_sql("INSERT INTO standard (code, clause, title, body, version) "
                         "VALUES (%s,%s,%s,%s,%s)", (code, clause, ctitle, body, version))
                new += 1
    if verbose and not dry_run:
        total = q("SELECT COUNT(*) AS n FROM standard")[0]["n"]
        sys.stderr.write("[knowledge] 规范同步：新增 %d、更新 %d、跳过 %d，库里共 %s 条\n"
                         % (new, upd, skip, total))
    return new, upd, skip
