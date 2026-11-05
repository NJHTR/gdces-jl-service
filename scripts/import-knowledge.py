#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动同步知识包（BFF 启动时也会自动同步一次）

    python3 scripts/import-knowledge.py --dry-run
    python3 scripts/import-knowledge.py
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bff"))

from bffpkg.knowledge import sync_standards  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    new, upd, skip = sync_standards(dry_run=args.dry_run)
    if not args.dry_run:
        print("导入完成：新增 %d、更新 %d、跳过 %d" % (new, upd, skip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
