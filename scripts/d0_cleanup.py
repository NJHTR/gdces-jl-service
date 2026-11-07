#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D0=A：清 test-sandbox 的测试文书（只删 state='待补'），带引用清理与归档。

安全规则：
  · 只动 project_id='test-sandbox' AND state='待补'（4 待签 + 4 已签不动）
  · 文件移到 records/archive/2026-11-07/docs/，不 rm
  · 删行前先备份（mysqldump + tar 工作区/导出）
  · task.doc_id 悬空统一置 NULL（含历史悬空）
  · 孤儿只归档 test-sandbox 的；yun-gu 的只报告
  · 丢失文件只报告，不碰

用法：
  python3 scripts/d0_cleanup.py --dry-run
  python3 scripts/d0_cleanup.py --apply
"""
import argparse
import glob
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "data", "workspaces")
QDIR = os.path.join(ROOT, "data", "quarantine", "2026-11-07")
ARCH = os.path.join(WS, "test-sandbox", "records", "archive", "2026-11-07")
PROJECT = "test-sandbox"


def pw():
    for line in io.open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""


def mysql(sql, db="jl"):
    r = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw(),
                        "--default-character-set=utf8mb4", "-N", "-e", sql, db],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[:300])
    return r.stdout.strip()


def rows(sql):
    out = []
    for line in (mysql(sql) or "").splitlines():
        out.append(line.split("\t"))
    return out


def sh(cmd):
    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip()[-400:], (r.stderr or "").strip()[-400:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not (a.dry_run or a.apply):
        print("要 --dry-run 或 --apply")
        return 1

    todo = rows("SELECT id, COALESCE(path,'') FROM doc WHERE project_id='%s' "
                "AND state='待补' ORDER BY id" % PROJECT)
    ids = [int(r[0]) for r in todo]
    keep = rows("SELECT id, state FROM doc WHERE project_id='%s' AND state<>'待补' "
                "ORDER BY id" % PROJECT)
    refs = rows("SELECT t.id, t.doc_id FROM task t JOIN doc d ON t.doc_id=d.id "
                "WHERE d.project_id='%s' AND d.state='待补'" % PROJECT)
    total_before = int(mysql("SELECT COUNT(*) FROM doc"))
    dangling_before = int(mysql(
        "SELECT COUNT(*) FROM task t LEFT JOIN doc d ON t.doc_id=d.id "
        "WHERE t.doc_id IS NOT NULL AND d.id IS NULL"))

    if a.dry_run:
        print("  test-sandbox 待补: %d（计划删除）" % len(ids))
        print("  保留（待签/已签）: %s" % [(int(k[0]), k[1]) for k in keep])
        print("  被 task 引用: %d 个 task" % len(refs))
        print("  总文书: %d · 当前悬空 task.doc_id: %d" % (total_before, dangling_before))
        print("  删除后预期: 总文书 %d · test-sandbox %d" % (total_before - len(ids), len(keep)))
        return 0

    # ---------- 1) 备份
    stamp = time.strftime("%Y%m%d-%H%M")
    bdir = os.path.join(ROOT, "backups")
    os.makedirs(bdir, exist_ok=True)
    dump = os.path.join(bdir, "d0-%s.sql.gz" % stamp)
    rc, out, err = sh("docker exec jl-mysql mysqldump -ujl -p%s --single-transaction jl | gzip > %s"
                      % (pw(), dump))
    if rc != 0 or not os.path.isfile(dump):
        print("备份失败: %s %s" % (out, err))
        return 1
    ws_tar = os.path.join(bdir, "d0-ws-test-sandbox-%s.tar.gz" % stamp)
    sh("tar -czf %s -C %s test-sandbox" % (ws_tar, WS))
    exp_tar = os.path.join(bdir, "d0-exports-%s.tar.gz" % stamp)

    # ---------- 2) manifest
    os.makedirs(QDIR, exist_ok=True)
    manifest = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project": PROJECT,
        "rule": "state='待补' only",
        "deleted_ids": ids,
        "deleted_paths": [r[1] for r in todo],
        "keep_ids": [int(k[0]) for k in keep],
        "task_refs": [[int(x[0]), int(x[1])] for x in refs],
        "total_before": total_before,
        "dangling_task_doc_before": dangling_before,
        "backups": {"db": os.path.basename(dump),
                    "workspace": os.path.basename(ws_tar) if os.path.isfile(ws_tar) else None},
    }

    # ---------- 3) 移文件到 archive（不 rm）
    moved = missing = 0
    for _did, rel in todo:
        if not rel:
            continue
        src = os.path.join(WS, PROJECT, rel)
        if not os.path.isfile(src):
            missing += 1
            continue
        dst = os.path.join(ARCH, "docs", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        moved += 1

    # ---------- 4) 删 doc（只这 81 个）
    idlist = ",".join(str(i) for i in ids) or "0"
    mysql("DELETE FROM doc WHERE id IN (%s) AND project_id='%s' AND state='待补'"
          % (idlist, PROJECT))
    left = int(mysql("SELECT COUNT(*) FROM doc WHERE id IN (%s)" % idlist))

    # ---------- 5) task.doc_id 悬空统一置 NULL
    unlinked = int(mysql(
        "SELECT COUNT(*) FROM task t LEFT JOIN doc d ON t.doc_id=d.id "
        "WHERE t.doc_id IS NOT NULL AND d.id IS NULL"))
    mysql("UPDATE task t LEFT JOIN doc d ON t.doc_id=d.id SET t.doc_id=NULL "
          "WHERE t.doc_id IS NOT NULL AND d.id IS NULL")

    # ---------- 6) 孤儿：只归档 test-sandbox
    known = set()
    for pid, rel in rows("SELECT project_id, COALESCE(path,'') FROM doc"):
        if pid == PROJECT and rel:
            known.add(rel)
    orphan_moved, orphans_other = [], {}
    base = os.path.join(WS, PROJECT, "docs")
    if os.path.isdir(base):
        for root, _d, files in os.walk(base):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, os.path.join(WS, PROJECT))
                if rel in known:
                    continue
                dst = os.path.join(ARCH, "orphans", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(full, dst)
                orphan_moved.append(rel)
    for pid in os.listdir(WS):
        if pid == PROJECT:
            continue
        d = os.path.join(WS, pid, "docs")
        if not os.path.isdir(d):
            continue
        kn = {rel for p, rel in rows("SELECT project_id, COALESCE(path,'') FROM doc")
              if p == pid and rel}
        n = 0
        for root, _d, files in os.walk(d):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), os.path.join(WS, pid))
                if rel not in kn:
                    n += 1
        if n:
            orphans_other[pid] = n

    # ---------- 7) 丢失：只报告
    lost = {}
    for pid, rel in rows("SELECT project_id, COALESCE(path,'') FROM doc"):
        if rel and not os.path.isfile(os.path.join(WS, pid, rel)):
            lost[pid] = lost.get(pid, 0) + 1

    # ---------- 8) 断言
    after_total = int(mysql("SELECT COUNT(*) FROM doc"))
    after_todo = int(mysql("SELECT COUNT(*) FROM doc WHERE project_id='%s' "
                           "AND state='待补'" % PROJECT))
    after_proj = int(mysql("SELECT COUNT(*) FROM doc WHERE project_id='%s'" % PROJECT))
    dangling_after = int(mysql(
        "SELECT COUNT(*) FROM task t LEFT JOIN doc d ON t.doc_id=d.id "
        "WHERE t.doc_id IS NOT NULL AND d.id IS NULL"))

    manifest.update({
        "moved_files": moved, "missing_files": missing,
        "deleted_rows": len(ids) - left,
        "task_unlinked": unlinked,
        "orphans_archived": orphan_moved,
        "orphans_other_projects": orphans_other,
        "lost_files_reported": lost,
        "after": {"total": after_total, "test_sandbox": after_proj,
                  "test_sandbox_todo": after_todo, "dangling_task_doc": dangling_after},
    })
    io.open(os.path.join(QDIR, "d0-manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    ok = (after_todo == 0 and left == 0 and dangling_after == 0
          and after_proj == len(keep))
    print(json.dumps({
        "before_total": total_before, "after_total": after_total,
        "deleted": len(ids), "moved": moved, "missing": missing,
        "unlinked": unlinked, "orphans_test_sandbox": len(orphan_moved),
        "orphans_other": orphans_other, "lost": lost,
        "test_sandbox_todo_after": after_todo, "test_sandbox_after": after_proj,
        "dangling_after": dangling_after,
    }, ensure_ascii=False))
    print("D0_OK" if ok else "D0_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
