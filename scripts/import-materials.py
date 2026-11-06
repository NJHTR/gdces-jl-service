#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「材料」导进组织知识库（data/knowledge）

为什么要有它：用户的材料目录很乱（同文件多份、有密钥、有垃圾）。
直接拷进去等于把乱带进仓库。这个脚本做四件事：

  1. 只挑有知识价值的类型：省统表(docx)、填写示例(docx/xlsx)、验收要点库(json/md)、旁站范本(md)
  2. 按 md5 去重（同一份内容只导一次，报告里说明哪份被跳过）
  3. 跳过密钥/垃圾（含 key/secret/token/password 字样、<200 字节）
  4. 每个导入文件生成 md，头部带 source/md5/原文件名 —— 可追溯，可 git diff

用法（本地）：
  python3 scripts/import-materials.py --src "C:\\...\\新" --dry
  python3 scripts/import-materials.py --src "C:\\...\\新"
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "knowledge")
SECRET_RE = re.compile(r"key|secret|token|password|apikey|密钥", re.I)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def docx_to_md(path, title):
    from docx import Document

    d = Document(path)
    lines = ["# %s" % title, ""]
    for p in d.paragraphs:
        t = p.text.strip()
        if t:
            lines.append(t)
            lines.append("")
    for ti, tb in enumerate(d.tables):
        lines.append("## 表格 %d" % (ti + 1))
        lines.append("")
        for r in tb.rows:
            cells = [c.text.strip().replace("\n", " ").replace("|", "/") for c in r.cells]
            uniq, seen = [], set()
            for c in cells:
                if c not in seen:
                    uniq.append(c)
                    seen.add(c)
            lines.append("| " + " | ".join(uniq) + " |")
        lines.append("")
    return "\n".join(lines)


def _xlsx_without_styles(path):
    """把 xlsx 复制一份并去掉 xl/styles.xml。

    为什么：省统表那批 xlsx 里有 openpyxl 解析不了的样式节点
    （裸 <color> / 缺属性），普通读会 TypeError；我们只要单元格文本，不要样式。
    不改源文件，只在临时副本上操作。
    """
    import shutil
    import tempfile
    import zipfile

    tmp = os.path.join(tempfile.gettempdir(), "jl-xlsx-%d.xlsx" % (abs(hash(path)) % 10 ** 8))
    with zipfile.ZipFile(path) as z:
        items = [(n, z.read(n)) for n in z.namelist()]
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in items:
            if n == "xl/styles.xml":
                continue
            z.writestr(n, b)
    return tmp


def xlsx_to_md(path, title):
    import openpyxl

    # read_only=True：省统表 xlsx 的样式对象会让普通模式报错
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except TypeError:
        wb = openpyxl.load_workbook(_xlsx_without_styles(path), data_only=True, read_only=True)
    lines = ["# %s" % title, ""]
    for ws in wb.worksheets:
        lines.append("## 工作表 %s" % ws.title)
        lines.append("")
        for row in ws.iter_rows(values_only=True):
            vals = ["" if v is None else str(v).strip().replace("|", "/") for v in row]
            if not any(vals):
                continue
            uniq, seen = [], set()
            for c in vals:
                if c not in seen:
                    uniq.append(c)
                    seen.add(c)
            lines.append("| " + " | ".join(uniq) + " |")
        lines.append("")
    return "\n".join(lines)


def checkpoints_to_md(path):
    """验收要点库 → 按分部工程切片，每片一个 md（可检索、省 token）"""
    d = json.load(io.open(path, encoding="utf-8"))
    entries = d.get("entries") or []
    meta = d.get("meta") or {}
    groups = {}
    for e in entries:
        key = (e.get("分部工程") or "未分类").strip()
        groups.setdefault(key, []).append(e)

    out = {}
    safe = lambda s: re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-")[:60] or "未分类"
    for key, items in groups.items():
        lines = [
            "# 验收要点 · %s" % key,
            "",
            "> 来源：验收要点库（结构化索引）.json，共 %d 条；总库 %d 条 / %d 个分部。"
            % (len(items), meta.get("总条目", len(entries)), len(groups)),
            "> 每条含「原句」（验收时照此核查）与「询问式」（转成向施工方确认的话术）。",
            "",
        ]
        for e in items:
            lines.append("## %s. %s" % (e.get("序号"), (e.get("关键工序_关键部位") or "").strip()))
            lines.append("")
            if e.get("单位工程"):
                lines.append("- 单位工程：%s" % e["单位工程"])
            if e.get("来源"):
                lines.append("- 分区来源：%s" % e["来源"])
            lines.append("")
            lines.append("**验收要点：**")
            for x in e.get("验收要点_原句") or []:
                lines.append("- %s" % x)
            lines.append("")
            if e.get("验收要点_询问式"):
                lines.append("**问法：**")
                for x in e["验收要点_询问式"]:
                    lines.append("- %s" % x)
                lines.append("")
        out["checkpoints/" + safe(key) + ".md"] = "\n".join(lines)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    src = a.src
    if not os.path.isdir(src):
        print("目录不存在：%s" % src)
        return 2

    # 优先级：省统表 > 填写示例 > 安全用表 > 知识库 > 文档
    # 同一份内容（md5 相同）只导一次，所以要按优先级决定「谁赢」。
    def priority(rel):
        r = rel.replace("\\", "/")
        if r.startswith("知识库/省统表word版/"):
            return 0
        if r.startswith("知识库/填写示例/"):
            return 1
        if r.startswith("知识库/第三节安全监理用表/"):
            return 2
        if r.startswith("知识库/"):
            return 3
        if r.startswith("文档/"):
            return 4
        return 5

    def category(rel):
        r = rel.replace("\\", "/")
        if r.startswith("知识库/省统表word版/"):
            return "templates"
        if r.startswith("知识库/第三节安全监理用表/"):
            return "templates/safety"
        if r.startswith("知识库/填写示例/"):
            return "examples"
        if "验收要点库" in r:
            return "checkpoints"
        if r.startswith("文档/"):
            return "examples"
        return "references"

    # 明确不导入（不是知识，或另有归属）
    SKIP_PREFIX = ("知识库/user_kb/", "知识库/user_kb\\", "模型调用文档/",
                   "文档/智能监理系统重构设计/")

    picked = []
    unsupported = []
    for root, _, files in os.walk(src):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, src)
            r = rel.replace("\\", "/")
            size = os.path.getsize(p)
            ext = os.path.splitext(fn)[1].lower()

            if any(r.startswith(x.replace("\\", "/")) for x in SKIP_PREFIX):
                unsupported.append((rel, "按规则跳过（不是组织知识：用户上传/密钥目录/设计稿）"))
                continue
            if SECRET_RE.search(fn):
                unsupported.append((rel, "疑似密钥/凭证"))
                continue
            if size < 200:
                unsupported.append((rel, "空文件或垃圾（%d 字节）" % size))
                continue
            if ext in (".pdf", ".doc", ".jpg", ".png"):
                unsupported.append((rel, "暂不支持自动抽取（%s），列为待处理" % ext))
                continue
            if ext not in (".docx", ".xlsx", ".json", ".md"):
                unsupported.append((rel, "类型不处理（%s）" % (ext or "无扩展名")))
                continue
            picked.append((priority(rel), rel, p, fn, ext, size))

    picked.sort(key=lambda x: (x[0], x[1]))

    seen = {}
    plan = []
    skipped = list(unsupported)
    for pri, rel, p, fn, ext, size in picked:
        h = md5(p)
        if h in seen:
            skipped.append((rel, "与 %s 内容完全相同（保留优先级更高的那份）" % seen[h]))
            continue
        seen[h] = rel

        name = os.path.splitext(fn)[0]
        cat = category(rel)
        safe = re.sub(r"[\\/:*?\"<>|]+", "-", name)[:80]

        if ext == ".docx":
            plan.append(("%s/%s.md" % (cat, safe), (lambda pp, nn: (lambda: docx_to_md(pp, nn)))(p, name), rel, h, size))
        elif ext == ".xlsx":
            plan.append(("%s/%s.md" % (cat, safe), (lambda pp, nn: (lambda: xlsx_to_md(pp, nn)))(p, name), rel, h, size))
        elif ext == ".md":
            plan.append(("%s/%s.md" % (cat, safe), (lambda pp: (lambda: io.open(pp, encoding="utf-8").read()))(p), rel, h, size))
        elif ext == ".json" and cat == "checkpoints":
            for out_rel, body in checkpoints_to_md(p).items():
                plan.append((out_rel, (lambda b=body: b), rel, h, size))
        else:
            skipped.append((rel, "没有对应抽取器（%s）" % ext))

    print("计划导入 %d 个文件 → %s" % (len(plan), OUT))
    print("跳过 %d 个：" % len(skipped))
    for rel, why in skipped[:30]:
        print("  - %s  （%s）" % (rel, why))
    if len(skipped) > 30:
        print("  ... 还有 %d 个" % (len(skipped) - 30))

    if a.dry:
        print("\n--dry：不写文件")
        for out_rel, _, rel, _, size in plan[:40]:
            print("  %-52s ← %s (%.0fKB)" % (out_rel, rel, size / 1024))
        return 0

    manifest = [
        "# 知识库清单（由 scripts/import-materials.py 生成）",
        "",
        "> 每个文件头部都有 source / md5，可追溯回原始材料；重复内容只导一次。",
        "",
        "| 落点 | 源文件 | md5 | 大小 |",
        "|---|---|---|---|",
    ]
    n = 0
    for out_rel, gen, rel, h, size in plan:
        dst = os.path.join(OUT, out_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            body = gen()
        except Exception as exc:                      # noqa: BLE001
            print("  跳过（抽取失败）：%s  %r" % (rel, exc))
            continue
        header = (
            "<!-- source: %s -->\n<!-- md5: %s -->\n<!-- imported_by: scripts/import-materials.py -->\n\n"
            % (rel.replace("\\", "/"), h)
        )
        io.open(dst, "w", encoding="utf-8", newline="\n").write(header + body)
        manifest.append("| %s | %s | %s | %.0fKB |" % (out_rel, rel.replace("\\", "/"), h[:8], size / 1024))
        n += 1

    io.open(os.path.join(OUT, "MANIFEST.md"), "w", encoding="utf-8", newline="\n").write(
        "\n".join(manifest) + "\n")
    print("\n已写入 %d 个文件 + MANIFEST.md" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
