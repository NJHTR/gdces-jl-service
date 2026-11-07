#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D0 验收：只读 DB/文件，打印不变量；全过输出 D0CHECK_OK。

不变量：
  1. test-sandbox 待补 = 0
  2. 总文书 = 88，test-sandbox = 8
  3. 悬空 task.doc_id = 0
  4. 归档目录有 80 份 docs + 9 份 orphans
  5. 负向：yun-gu 待补 = 71（真实项目未被碰）
  6. manifest 存在且 deleted_ids = 81
"""
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "data", "workspaces")
ARCH = os.path.join(WS, "test-sandbox", "records", "archive", "2026-11-07")


def pw():
    for line in io.open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""


def one(sql):
    r = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw(),
                        "--default-character-set=utf8mb4", "-N", "-e", sql, "jl"],
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def main():
    checks = []
    checks.append(("test-sandbox 待补 = 0",
                   one("SELECT COUNT(*) FROM doc WHERE project_id='test-sandbox' "
                       "AND state='待补'") == "0"))
    total = one("SELECT COUNT(*) FROM doc")
    proj = one("SELECT COUNT(*) FROM doc WHERE project_id='test-sandbox'")
    checks.append(("总文书 = 88", total == "88"))
    checks.append(("test-sandbox = 8", proj == "8"))
    checks.append(("悬空 task.doc_id = 0", one(
        "SELECT COUNT(*) FROM task t LEFT JOIN doc d ON t.doc_id=d.id "
        "WHERE t.doc_id IS NOT NULL AND d.id IS NULL") == "0"))
    checks.append(("yun-gu 待补 = 71（负向：未动真实项目）",
                   one("SELECT COUNT(*) FROM doc WHERE project_id='yun-gu-3qi' "
                       "AND state='待补'") == "71"))

    n_docs = sum(len(f) for _r, _d, f in os.walk(os.path.join(ARCH, "docs")))
    n_orph = sum(len(f) for _r, _d, f in os.walk(os.path.join(ARCH, "orphans")))
    checks.append(("归档 docs = 80", n_docs == 80))
    checks.append(("归档 orphans = 9", n_orph == 9))

    mf = os.path.join(ROOT, "data", "quarantine", "2026-11-07", "d0-manifest.json")
    ok_manifest = False
    if os.path.isfile(mf):
        m = json.load(io.open(mf, encoding="utf-8"))
        ok_manifest = len(m.get("deleted_ids", [])) == 81
    checks.append(("manifest deleted_ids = 81", ok_manifest))

    print(json.dumps({"total": total, "test_sandbox": proj,
                      "arch_docs": n_docs, "arch_orphans": n_orph}, ensure_ascii=False))
    for name, ok in checks:
        print(("  PASS " if ok else "  FAIL ") + name)
    all_ok = all(ok for _n, ok in checks)
    print("D0CHECK_OK" if all_ok else "D0CHECK_FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
