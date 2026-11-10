# -*- coding: utf-8 -*-
"""Phase 2 · 记忆的两个域：个人域摘要 / 项目域只重渲染

分工（别再长成第三个"记忆写者"）：
  · memory 表是唯一账本；memory_candidate 是候选，pending 永远不算 active。
  · 个人域：把 user + project_user 的 **active** 记忆做确定性去重/归并，
    生成 data/memory/users/<uid>/memory_summary.md（只读视图，可整份删了重建）。
  · 项目域：只把 **active** 的 memory(scope=project) 重渲染到 records/rules.md；
    本模块绝不 INSERT/UPDATE memory，也不把个人记忆/候选提升成项目规矩。
  · 幂等：内容没变就不写文件（不产生无意义的 mtime/ git 提交）。

为什么先做确定性摘要：先要能解释"这行为什么是它"，再谈 LLM 归并。
去重规则写死、可复现；相近但不同的说法**并列保留**，不静默合并掉（冲突不静默覆盖）。
"""
import os
import re
import sys

from .core import ROOT, q, similar
from .biz import render_rules_md

# 去重时忽略的标点/空白（与 core.similar 的归一化同一套口径）
_NORM_RE = re.compile(r"[\s，。、；：！？「」『』（）()\[\]【】,.;:!?]")


def user_summary_path(user_id):
    """个人域摘要的固定落点（任务要求 data/memory 下，不混进引擎 home）。"""
    return os.path.join(ROOT, "data", "memory", "users", str(user_id), "memory_summary.md")


def _norm(text):
    return _NORM_RE.sub("", (text or "").strip())


def _active_personal_rows(user_id):
    """只读 memory 里 status=1 的个人记忆（user scope：跨项目 + 个人×项目）。
    candidate 表根本不在这里出现 —— 候选不是 active。"""
    return q("SELECT id, text, project_id, source, applies_to, created_at FROM memory "
             "WHERE scope='user' AND owner_id=%s AND status=1 ORDER BY id", (str(user_id),))


def _merge_entries(rows):
    """确定性去重/归并。

    规则（顺序固定，结果可复现）：
      1) 归一化后完全相同 → 合并成一条，记合并条数；
      2) 一条是另一条的子串（说法更完整）→ 并到更长的那条，记合并条数；
      3) 其余相似（similar()）→ **不合并**，两条都留在正文，并记一组"相近待确认"。
    这样同义重复被压掉，但措辞不同的规则不会被悄悄丢掉。
    """
    entries = []          # [{text, norm, merged}]
    by_norm = {}
    conflicts = []        # [(text_a, text_b)]
    for r in rows:
        text = (r.get("text") or "").strip()
        norm = _norm(text)
        if not norm:
            continue
        if norm in by_norm:
            ent = by_norm[norm]
            ent["merged"] += 1
            continue
        contained = None
        for ent in entries:
            if norm in ent["norm"] or ent["norm"] in norm:
                contained = ent
                break
        if contained is not None:
            contained["merged"] += 1
            if len(norm) > len(contained["norm"]):      # 用更完整的说法做正文
                contained["text"] = text
                contained["norm"] = norm
                by_norm[norm] = contained
            continue
        for ent in entries:
            if similar(text, ent["text"]):
                pair = (ent["text"], text)
                if pair not in conflicts:
                    conflicts.append(pair)
                break
        ent = {"text": text, "norm": norm, "merged": 0}
        entries.append(ent)
        by_norm[norm] = ent
    return entries, conflicts


def build_user_summary(user_id):
    """确定性摘要正文 + 统计。不读候选表，不调模型。"""
    rows = _active_personal_rows(user_id)
    cross_rows = [r for r in rows if not (r.get("project_id") or "")]
    by_project = {}
    for r in rows:
        pid = r.get("project_id") or ""
        if pid:
            by_project.setdefault(pid, []).append(r)

    cross_entries, cross_conflicts = _merge_entries(cross_rows)
    total_active = len(rows)
    merged_total = 0
    lines = ["# 我的记忆摘要（跨项目 + 本项目）", "",
             "> 由 memory 表 active 记忆确定性生成；候选（pending）不计入。",
             "> 同义/包含关系的说法已合并（标注条数）；相近但不同的并列在末尾，需人工确认，不会静默覆盖。",
             "> 原始账本：data/memory/users/<uid>/MEMORY.md 与 data/memory/projects/<uid>/<project>.md。",
             ""]

    lines.append("## 跨项目（%d 条）" % len(cross_entries))
    if not cross_entries:
        lines.append("- （暂无）")
    for ent in cross_entries:
        merged_total += ent["merged"]
        note = "（同义合并 %d 条）" % (ent["merged"] + 1) if ent["merged"] else ""
        lines.append("- %s%s" % (ent["text"], note))
    lines.append("")

    conflicts_all = list(cross_conflicts)
    if by_project:
        for pid in sorted(by_project):
            entries, conflicts = _merge_entries(by_project[pid])
            conflicts_all.extend(conflicts)
            lines.append("## 本项目 %s（%d 条）" % (pid, len(entries)))
            for ent in entries:
                merged_total += ent["merged"]
                note = "（同义合并 %d 条）" % (ent["merged"] + 1) if ent["merged"] else ""
                lines.append("- %s%s" % (ent["text"], note))
            lines.append("")

    if conflicts_all:
        lines.append("## 相近但未合并（需人工确认，%d 组）" % len(conflicts_all))
        for a, b in conflicts_all:
            lines.append("- 「%s」 ↔ 「%s」" % (a, b))
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    stats = {
        "userId": str(user_id),
        "activeTotal": total_active,
        "crossEntries": len(cross_entries),
        "projectEntries": sum(len(_merge_entries(v)[0]) for v in by_project.values()),
        "mergedRows": merged_total,
        "conflicts": len(conflicts_all),
    }
    return body, stats


def _write_if_changed(path, body):
    """幂等写：内容一样就不动文件（不刷新 mtime、不产生 git diff）。"""
    old = None
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = None
    if old == body:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return True


def render_user_summary(user_id):
    """重渲染个人域摘要。返回落盘路径（相对仓库根，便于日志/接口回显）。"""
    try:
        body, _stats = build_user_summary(user_id)
    except Exception as e:
        sys.stderr.write("[memv2] 个人摘要生成失败 uid=%s: %r\n" % (user_id, e))
        return None
    path = user_summary_path(user_id)
    try:
        _write_if_changed(path, body)
    except OSError as e:
        sys.stderr.write("[memv2] 个人摘要写失败 %s: %r\n" % (path, e))
        return None
    return os.path.relpath(path, ROOT)


def render_project_rules(project_id):
    """项目域：只重渲染 records/rules.md，绝不写 memory。

    数据源 = memory(scope='project', status=1)。候选 pending 不在表里；接受才写 memory
    （Phase1 的 handle_candidate_decide 已经保证 source='confirmed'）。所以这里天然做到：
      · pending 不影响 rules.md
      · 只反映已生效/已确认的项目规矩
      · 不自动新增、不提升个人记忆、不改写规则
    **不按 source='confirmed' 过滤**：线上项目规矩多数是 manual/learned 的人工写入，
    只认 confirmed 会把已生效规矩从文件里删掉——那才是"自动改写项目规则"。
    """
    rel = render_rules_md(str(project_id))
    return rel


def project_rule_texts(project_id):
    """当前会出现在 rules.md 里的规则文本（只读，供测试/审计对账）。"""
    rows = q("SELECT text FROM memory WHERE scope='project' AND owner_id=%s "
             "AND status=1 ORDER BY id", (str(project_id),))
    return [(r.get("text") or "").strip() for r in rows if (r.get("text") or "").strip()]


def refresh_all_user_summaries():
    """给所有有 active 个人记忆的用户重渲染摘要（幂等，可直接跑）。"""
    rows = q("SELECT DISTINCT owner_id FROM memory WHERE scope='user' AND status=1 "
             "ORDER BY owner_id")
    out = []
    for r in rows:
        uid = r.get("owner_id")
        if not uid:
            continue
        rel = render_user_summary(uid)
        if rel:
            out.append(rel)
    return out


def refresh_all_project_rules():
    """给所有项目重渲染 rules.md（幂等，只读 memory）。"""
    rows = q("SELECT id FROM project ORDER BY id")
    out = []
    for r in rows:
        pid = r.get("id")
        if not pid:
            continue
        rel = render_project_rules(pid)
        if rel:
            out.append("%s:%s" % (pid, rel))
    return out
