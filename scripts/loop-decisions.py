#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 BACKLOG 的「决策记录」段生成 docs/decisions.index.json。

决策是硬约束：loop 每轮 PICK 后要检查本项是否与 D-* 冲突。
`forbids` 是"出现这些词就说明在碰被禁止的做法"，由人工维护的小表给出，
不靠模型猜；匹配到了就打印 WARN，由 Planner 判断是否 STOP。
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKLOG = os.path.join(ROOT, "docs", "BACKLOG.md")
OUT = os.path.join(ROOT, "docs", "decisions.index.json")

# 决策 -> 触发词。只放"写成代码就会违规"的关键词，不放语义猜测。
FORBIDS = {
    "D-记忆-04": ["JL_ENGINE_MEMORY=1", "memories = true", "自动注入"],
    "D-记忆-07": ["引擎写 memory", "engine writes memory", "第二写者"],
    "D-记忆-03": ["hits 排序", "按 hits 排序", "hits 作为复用"],
    "D-记忆-09": ["默认 active", "直接写入 active"],
    "D-记忆-11": ["test 数据进业务", "smoke 直写业务表"],
    "D-表式-01": ["手绘表格", "模型生成 ooxml", "程序画表"],
    "D-表式-02": ["监理检查记录 省统表"],
    "D-部署-01": ["服务器手改", "直接改服务器文件"],
}


def main():
    if not os.path.isfile(BACKLOG):
        print("找不到 docs/BACKLOG.md")
        return 1
    text = io.open(BACKLOG, encoding="utf-8").read()
    got = {}
    # 形如：D-记忆-04  引擎记忆走 A：关闭自动注入……
    for m in re.finditer(r"(?m)^(D-[^\s]+)\s+(.+)$", text):
        did, desc = m.group(1), m.group(2).strip()
        if did not in got:
            got[did] = {"id": did, "text": desc[:200],
                        "forbids": FORBIDS.get(did, [])}
    data = {
        "generated_from": "docs/BACKLOG.md",
        "count": len(got),
        "decisions": sorted(got.values(), key=lambda d: d["id"]),
    }
    io.open(OUT, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print("  决策 %d 条 -> %s" % (len(got), os.path.relpath(OUT, ROOT)))
    for d in data["decisions"]:
        tag = ("  禁止词: " + "、".join(d["forbids"])) if d["forbids"] else ""
        print("    %-12s %s%s" % (d["id"], d["text"][:60], tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
