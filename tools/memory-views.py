#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase2 记忆视图刷新工具（幂等；服务器上跑）。

    cd /home/baize/jl-service
    python3 tools/memory-views.py --user 1          # 只刷某个人的 memory_summary.md
    python3 tools/memory-views.py --all-users       # 所有有 active 个人记忆的人
    python3 tools/memory-views.py --project baiyun-a2   # 只重渲染某项目 records/rules.md
    python3 tools/memory-views.py --all-projects
    python3 tools/memory-views.py --all             # 全部（部署后回填/巡检用）

只读 memory 表；memory_candidate 的 pending 不参与。写文件幂等：内容没变不动。
不写 memory，不提升候选，不改项目规则内容。
"""
import argparse
import os
import sys

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"


def load():
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.memorysum as ms  # noqa: E402
    return ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", action="append", default=[], help="user_id，可多次")
    ap.add_argument("--project", action="append", default=[], help="project_id，可多次")
    ap.add_argument("--all-users", action="store_true")
    ap.add_argument("--all-projects", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    ms = load()

    users = list(args.user)
    projects = list(args.project)
    if args.all_users or args.all:
        users = None
    if args.all_projects or args.all:
        projects = None
    if not users and not projects and not (args.all or args.all_users or args.all_projects):
        ap.error("至少给 --user/--project，或 --all-users/--all-projects/--all")

    if users is None:
        paths = ms.refresh_all_user_summaries()
        print("[summary] 刷新 %d 份" % len(paths))
        for p in paths:
            print("  " + p)
    else:
        for uid in users:
            rel = ms.render_user_summary(uid)
            print("[summary] user=%s -> %s" % (uid, rel or "失败"))

    if projects is None:
        got = ms.refresh_all_project_rules()
        print("[rules] 刷新 %d 个项目" % len(got))
        for p in got:
            print("  " + p)
    else:
        for pid in projects:
            rel = ms.render_project_rules(pid)
            print("[rules] project=%s -> %s" % (pid, rel or "失败"))


if __name__ == "__main__":
    main()
