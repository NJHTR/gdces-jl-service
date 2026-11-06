# -*- coding: utf-8 -*-
"""运行数据体检（只读）：磁盘、备份、日志、文书健康度。

不修任何东西，只回答"现在哪里在漏"：
  · 磁盘还剩多少；data/ 各目录占地
  · 备份有几个包、最老的多少天（保留 7 天是否生效）
  · 日志多大、有没有异常增长
  · 文书：待补分布、孤儿文件（磁盘有库没有）、丢失文件（库有磁盘没有）
  · 影像：无描述的张数（索引里查不到）
  · 记忆：停用/重复/近义候选

用法：python3 scripts/health-check.py
"""
import io
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def sql(query):
    pw = ""
    for line in io.open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
            break
    r = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw,
                        "--default-character-set=utf8mb4", "-N", "-e", query, "jl"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[:200])
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def du(path):
    if not os.path.isdir(path):
        return 0
    total = 0
    for base, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
    return total


def mb(n):
    return "%.1fMB" % (n / 1024.0 / 1024.0)


def main():
    print("=== 磁盘 ===")
    t = shutil.disk_usage(ROOT)
    print("  根分区 总 %.0fG 已用 %.0fG 剩 %.0fG（%d%%）"
          % (t.total / 2**30, t.used / 2**30, t.free / 2**30,
             100 * t.used / t.total))
    for sub in ("workspaces", "blobs", "homes", "exports", "knowledge"):
        print("  data/%-11s %s" % (sub, mb(du(os.path.join(DATA, sub)))))

    print("=== 备份 ===")
    bdir = os.path.join(ROOT, "backups")
    if os.path.isdir(bdir):
        files = sorted(os.listdir(bdir))
        if files:
            newest = max(os.path.getmtime(os.path.join(bdir, f)) for f in files)
            oldest = min(os.path.getmtime(os.path.join(bdir, f)) for f in files)
            print("  %d 个包，总 %s" % (len(files), mb(du(bdir))))
            print("  最新 %.1f 小时前 · 最老 %.1f 天前（保留 7 天）"
                  % ((time.time() - newest) / 3600, (time.time() - oldest) / 86400))
            old = [f for f in files
                   if time.time() - os.path.getmtime(os.path.join(bdir, f)) > 8 * 86400]
            print("  超过 8 天未清: %d 个" % len(old))
    else:
        print("  backups/ 不存在")

    print("=== 日志 ===")
    rt = os.path.join(ROOT, "runtime")
    if os.path.isdir(rt):
        for fn in sorted(os.listdir(rt)):
            fp = os.path.join(rt, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) > 5 * 1024 * 1024:
                print("  偏大: %-24s %s" % (fn, mb(os.path.getsize(fp))))

    print("=== 文书 ===")
    states = sql("SELECT COALESCE(state,'?'), COUNT(*) FROM doc GROUP BY state")
    print("  状态分布: " + " · ".join(states))
    known = set()
    proj_docs = {}
    for pid, path in (ln.split("\t") for ln in sql(
            "SELECT project_id, COALESCE(path,'') FROM doc") if "\t" in ln):
        if path:
            known.add("%s/%s" % (pid, path))
            proj_docs.setdefault(pid, set()).add(path)
    orphan = lost = 0
    for pid in os.listdir(os.path.join(DATA, "workspaces")) if os.path.isdir(
            os.path.join(DATA, "workspaces")) else []:
        base = os.path.join(DATA, "workspaces", pid, "docs")
        if not os.path.isdir(base):
            continue
        for root, _d, files in os.walk(base):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn),
                                      os.path.join(DATA, "workspaces", pid))
                if rel not in proj_docs.get(pid, set()):
                    orphan += 1
        for rel in proj_docs.get(pid, set()):
            if not os.path.isfile(os.path.join(DATA, "workspaces", pid, rel)):
                lost += 1
    print("  孤儿文件（磁盘有、库没有）: %d" % orphan)
    print("  丢失文件（库有、磁盘没有）: %d" % lost)

    print("=== 影像 ===")
    rows = sql("SELECT COUNT(*), COALESCE(SUM(COALESCE(caption,'')=''),0), "
               "COALESCE(SUM(COALESCE(place,'')=''),0) FROM photo")
    print("  照片 %s 张 · 无描述 %s · 无部位 %s" % tuple(rows[0].split("\t")))

    print("=== 记忆 ===")
    print("  " + " · ".join(sql(
        "SELECT CONCAT('active=', SUM(status=1), ' disabled=', SUM(status=0), "
        "' total=', COUNT(*)) FROM memory WHERE scope='project'")))
    dup = sql("SELECT COUNT(*) FROM (SELECT project_id, text FROM memory "
              "WHERE scope='project' GROUP BY project_id, text HAVING COUNT(*)>1) x")
    print("  完全重复条目组: %s" % (dup[0] if dup else "0"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
