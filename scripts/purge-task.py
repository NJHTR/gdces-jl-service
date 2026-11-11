#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""删除任务时清理它的 trace（事件 + data/trace/<task>/*.json.gz）。

    python3 scripts/purge-task.py 766 [767 ...]
    python3 scripts/purge-task.py --delete-task 766   # 连 task/message 行一起删

注意：默认只清 trace，保留任务与消息（审计可查）；--delete-task 才会删任务行。
"""
import argparse
import os
import sys

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_ids", nargs="+", type=int)
    ap.add_argument("--delete-task", action="store_true",
                    help="同时删除 task 与 message 行（默认只清 trace）")
    args = ap.parse_args()
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.core as core
    import bffpkg.trace as trace
    n = 0
    for tid in args.task_ids:
        trace.purge_task(tid)
        if args.delete_task:
            core.exec_sql("DELETE FROM message WHERE task_id=%s", (tid,))
            core.exec_sql("DELETE FROM task WHERE id=%s", (tid,))
        n += 1
        print("purged task", tid)
    print("TRACE_PURGE_OK tasks=%d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
