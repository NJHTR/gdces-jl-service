# -*- coding: utf-8 -*-
"""找出「近义重复」的规矩，生成归并清单（只提议，不删）。

为什么需要它：
- 规矩是一句句攒起来的，同一个意思被不同措辞写过好几遍，
  出稿时全塞进提示里既吵又互相打架
- 但"合并哪几条"是语义判断，不能靠相似度算法自动删
  → 这个脚本只输出清单（哪条留、哪条并进去、为什么），合并由人确认

用法：
  python3 scripts/propose_rule_merge.py            # 所有项目
  python3 scripts/propose_rule_merge.py yun-gu-3qi # 单个项目
输出：runtime/rules-merge-proposal.md（也打印摘要）
"""
import io
import os
import sys
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bff"))

from bffpkg.core import q  # noqa: E402

THRESHOLD = 0.72


def norm(t):
    t = (t or "").strip()
    for ch in "。；;，,、 ：:（）()「」“”\"'":
        t = t.replace(ch, "")
    return t


def bigrams(t):
    t = norm(t)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def similar(a, b):
    """两种口径取大的：编辑相似度 + 二元组 Jaccard。

    单用 SequenceMatcher 会把「整改必须配前后对比照片」和
    「整改回复须附对比图」判成不像（措辞差太多、意思一样）；
    单用 Jaccard 又容易把短句判得太像。取大的，宁可多提几条让人筛。
    """
    r1 = SequenceMatcher(None, norm(a), norm(b)).ratio()
    ba, bb = bigrams(a), bigrams(b)
    r2 = len(ba & bb) / max(1, len(ba | bb))
    return max(r1, r2)


def clusters(rows, threshold=THRESHOLD):
    """把相似的条目聚成组；每组第一条（id 最小）当候选保留。"""
    used = set()
    out = []
    for i, a in enumerate(rows):
        if i in used:
            continue
        group = [i]
        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            if similar(a["text"], rows[j]["text"]) >= threshold:
                group.append(j)
                used.add(j)
        used.add(i)
        if len(group) > 1:
            out.append([rows[k] for k in group])
    return out


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    where = "WHERE scope='project' AND status=1"
    args = ()
    if only:
        where += " AND owner_id=%s"
        args = (only,)
    rows = q("SELECT id, owner_id, text, hits, source FROM memory " + where +
             " ORDER BY owner_id, id", args)
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r["owner_id"], []).append(r)

    lines = ["# 规矩归并清单（只提议，未执行）", ""]
    total = 0
    for pid, items in by_proj.items():
        groups = clusters(items)
        lines.append("## %s（%d 条 → 建议保留 %d 条）" % (pid, len(items), len(items) - sum(len(g) - 1 for g in groups)))
        if not groups:
            lines.append("")
            lines.append("没有发现近义重复。")
            lines.append("")
            continue
        for g in groups:
            total += len(g) - 1
            keep = g[0]
            lines.append("")
            lines.append("- **保留** #%s：%s" % (keep["id"], keep["text"]))
            for other in g[1:]:
                lines.append("  - 并入 #%s：%s（相似度 %.2f）"
                             % (other["id"], other["text"],
                                similar(keep["text"], other["text"])))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("说明：确认后执行「保留 id 置 status=1，其余置 status=0」，"
                 "原文保留可追溯，再重渲染 rules.md。")
    out_dir = os.path.join(ROOT, "runtime")
    os.makedirs(out_dir, exist_ok=True)
    fp = os.path.join(out_dir, "rules-merge-proposal.md")
    io.open(fp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("写出: %s" % fp)
    print("可归并 %d 条（%d 组）" % (total, sum(len(clusters(v)) for v in by_proj.values())))
    for pid, items in by_proj.items():
        for g in clusters(items):
            print("  [%s] 保留 #%s ← 并入 %s" % (pid, g[0]["id"],
                                                ",".join("#%s" % x["id"] for x in g[1:])))


if __name__ == "__main__":
    main()
