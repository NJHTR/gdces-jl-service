#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每轮 SYNC：只读，输出一份 ≤60 行的结构化简报。

它读 6 件东西（见 docs/LOOP.md §3），并做三件事：
  1. 指针是否与决策冲突（用 decisions.index.json 的 forbids 关键词）
  2. 上一轮回执是否签名、是否 pass
  3. 健康与线上版本是否对得上

退出码：0 = 可以 PICK；2 = 有冲突/需要人（BLOCKED）；3 = 状态文件坏了
"""
import glob
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "docs", "state", "loop.json")
BACKLOG = os.path.join(ROOT, "docs", "BACKLOG.md")
DECISIONS = os.path.join(ROOT, "docs", "decisions.index.json")
RECEIPTS = os.path.join(ROOT, "docs", "receipts")


def sh(cmd):
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                           text=True, timeout=60)
        return (r.stdout or "").strip()
    except Exception as e:
        return "ERR:%r" % e


def ssh_run(remote_cmd, timeout=20):
    """用参数列表调 ssh：Windows 的 cmd 不认单引号，字符串拼接会静默失败。"""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "rokid-r640", remote_cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def main():
    if not os.path.isfile(STATE):
        print("状态文件不存在: docs/state/loop.json")
        return 3
    st = json.load(io.open(STATE, encoding="utf-8"))
    pointer = (st.get("pointer") or "").strip()
    print("=== LOOP SYNC @ %s ===" % time.strftime("%Y-%m-%d %H:%M"))
    print("  round=%s  wave=%s  pointer=%s" % (st.get("round"), st.get("wave"), pointer))
    print("  尝试次数: %s" % (st.get("attempts") or {}))

    # 1) 决策冲突
    conflict = []
    if os.path.isfile(DECISIONS):
        dec = json.load(io.open(DECISIONS, encoding="utf-8"))
        hay = pointer.lower()
        for d in dec.get("decisions", []):
            for w in d.get("forbids", []):
                if w.lower() in hay:
                    conflict.append((d["id"], w))
    print("  决策: %s" % ("无冲突" if not conflict else "冲突 %s" % conflict))

    # 2) 最近回执
    files = sorted(glob.glob(os.path.join(RECEIPTS, "*.json")))[-3:]
    print("  最近回执: %d 条" % len(files))
    for f in files:
        try:
            r = json.load(io.open(f, encoding="utf-8"))
            print("    %-28s %-8s %s verified_by=%s" % (
                os.path.basename(f), r.get("item"), r.get("verdict", "-"),
                r.get("verified_by", "未签名")))
        except Exception as e:
            print("    %s 解析失败 %r" % (os.path.basename(f), e))

    # 3) 代码与线上
    print("  git: %s 处未提交 · HEAD %s · 共 %s 提交" % (
        len([x for x in sh("git status --porcelain").splitlines() if x.strip()]),
        sh("git rev-parse --short HEAD"), sh("git rev-list --count HEAD")))
    dep = ssh_run("head -1 /home/baize/jl-service/runtime/DEPLOYED")
    print("  线上 DEPLOYED: %s" % (dep or "取不到"))
    health = ssh_run("curl -s -m 4 http://127.0.0.1:8081/healthz")
    print("  healthz: %s" % (health or "取不到"))

    # 4) 待拍板（只标题）
    if os.path.isfile(BACKLOG):
        txt = io.open(BACKLOG, encoding="utf-8").read()
        seg = txt.split("## 卡在这里", 1)
        if len(seg) > 1:
            heads = [l.strip() for l in seg[1].splitlines()
                     if l.strip().startswith(("F3", "D1b", "B6.5", "T6.5", "新", "真机"))]
            print("  待拍板: %d 条" % len(heads))
            for h in heads[:6]:
                print("    " + h[:70])

    print("  结论: %s" % ("BLOCKED（有冲突，停）" if conflict else "可以 PICK"))
    return 2 if conflict else 0


if __name__ == "__main__":
    sys.exit(main())
