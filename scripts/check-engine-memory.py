# -*- coding: utf-8 -*-
"""核对引擎的"记忆整理"到底有没有在工作（可重复的取证脚本）。

引擎规则（读自源码，不是猜的）：
- 每个用户一个 EASYCODE_HOME，记忆根 = $EASYCODE_HOME/memories
  （memories/read/src/lib.rs: memory_root）
- 整理发生在**引擎启动时**：phase1 领 rollout（默认最多 2 条/次），
  条件是 rollout 闲置 >= 6 小时、且不超过 10 天
  （config/src/types.rs: DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP / MIN_ROLLOUT_IDLE_HOURS）
- phase1 产出 raw_memories，phase2 合并成 MEMORY.md + memory_summary.md
- 每轮对话写的是 rollout（sessions/ 下的 jsonl），不是每个用户都马上有 MEMORY.md

用法：python3 scripts/check-engine-memory.py
"""
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOMES = os.path.join(ROOT, "data", "homes")
IDLE_HOURS = 6
MAX_AGE_DAYS = 10


def cfg_has_memories(home):
    fp = os.path.join(home, "config.toml")
    if not os.path.isfile(fp):
        return None
    in_features = False
    for line in io.open(fp, encoding="utf-8", errors="replace"):
        t = line.strip()
        if t.startswith("["):
            in_features = t == "[features]"
            continue
        if in_features and t.replace(" ", "").startswith("memories="):
            return t.split("=", 1)[1].strip().lower() in ("true", "1")
    return False


def rollouts(home):
    base = os.path.join(home, "sessions")
    out = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".jsonl"):
                fp = os.path.join(root, fn)
                out.append((fp, os.path.getmtime(fp)))
    return out


def size_of(path):
    return os.path.getsize(path) if os.path.isfile(path) else 0


def lines_of(path):
    if not os.path.isfile(path):
        return 0
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for ln in f if ln.strip() and not ln.startswith("#"))


def main():
    now = time.time()
    rows = []
    for name in sorted(os.listdir(HOMES), key=lambda x: (not x.isdigit(), x)):
        home = os.path.join(HOMES, name)
        mem = os.path.join(home, "memories")
        if not os.path.isdir(home):
            continue
        rs = rollouts(home)
        eligible = [r for r in rs
                    if (now - r[1]) >= IDLE_HOURS * 3600
                    and (now - r[1]) <= MAX_AGE_DAYS * 86400]
        rows.append({
            "user": name,
            "cfg": cfg_has_memories(home),
            "rollouts": len(rs),
            "eligible": len(eligible),
            "raw": lines_of(os.path.join(mem, "raw_memories.md")),
            "memory": size_of(os.path.join(mem, "MEMORY.md")),
            "summary": size_of(os.path.join(mem, "memory_summary.md")),
            "mem_mtime": (time.strftime("%m-%d %H:%M",
                          time.localtime(os.path.getmtime(os.path.join(mem, "MEMORY.md"))))
                          if os.path.isfile(os.path.join(mem, "MEMORY.md")) else ""),
        })

    print("  user      cfg   rollouts  可整理  raw行  MEMORY   summary  最近整理")
    for r in rows:
        if not (r["rollouts"] or r["memory"] or r["raw"]):
            continue
        print("  %-8s  %-4s  %7d  %5d  %5d  %6d  %8d  %s"
              % (r["user"], {True: "开", False: "关", None: "无"}[r["cfg"]],
                 r["rollouts"], r["eligible"], r["raw"], r["memory"],
                 r["summary"], r["mem_mtime"]))

    consolidated = [r for r in rows if r["memory"] or r["summary"]]
    enabled = [r for r in rows if r["cfg"]]
    print("")
    print("  已整理出 MEMORY/summary 的用户: %s"
          % (", ".join(r["user"] for r in consolidated) or "无"))
    print("  记忆开关打开的用户: %d/%d" % (len(enabled), len(rows)))
    print("  有可整理 rollout（>=%dh、<=%dd）的用户: %s"
          % (IDLE_HOURS, MAX_AGE_DAYS,
             ", ".join("%s(%d)" % (r["user"], r["eligible"])
                       for r in rows if r["eligible"]) or "无"))
    return 0 if consolidated and enabled else 1


if __name__ == "__main__":
    sys.exit(main())
