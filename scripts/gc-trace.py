#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理超期执行 trace（task_event + data/trace/<task>/*.json.gz）。

默认保留 30 天（TRACE_TTL_DAYS 可覆盖）。建议 cron 每天跑一次：
    0 4 * * * cd /home/baize/jl-service && python3 scripts/gc-trace.py >> runtime/gc-trace.log 2>&1
"""
import argparse
import os
import sys

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl-days", type=int, default=0, help="保留天数；0=用 TRACE_TTL_DAYS/默认30")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.trace as trace
    if args.dry_run:
        print("DRY-RUN：将清理 %d 天前的 task_event 与 data/trace 文件"
              % (args.ttl_days or trace.TRACE_TTL_DAYS))
        return 0
    n = trace.gc(args.ttl_days or trace.TRACE_TTL_DAYS)
    print("TRACE_GC_OK removed_events=%d ttl_days=%d"
          % (n, args.ttl_days or trace.TRACE_TTL_DAYS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
