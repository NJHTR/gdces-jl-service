#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-perf 验收：复核不能挡住 task done。

做三件事：
  1. 发起一轮会出文书的对话，轮询到 done，记 t_done / done 时的 reviewStatus
  2. 等 review 行出现，记 t_review
  3. 断言 t_done < t_review，且 done 时 reviewStatus=pending，复核后变 done

输出最后一行为 NONBLOCK_OK 或 NONBLOCK_FAIL；退出码 0/1。
清理：只删本次创建的 task/message/doc/review + 文件。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("BFF_URL") or "http://127.0.0.1:8081"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = "test-sandbox"


def call(method, path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def sql(query):
    pw = ""
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
            break
    r = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw,
                        "--default-character-set=utf8mb4", "-N", "-e", query, "jl"],
                       capture_output=True, text=True, timeout=60)
    return (r.stdout or "").strip()


def main():
    cid = "nonblock-%d" % int(time.time())
    body = {"projectId": PROJECT, "userId": 980, "clientId": cid,
            "text": "现场巡查发现 4#楼东侧外架连墙件缺失，出监理通知单"}
    t0 = time.time()
    d = call("POST", "/api/v1/chat", body)
    tid = (d.get("data") or {}).get("taskId")
    if not tid:
        print("NONBLOCK_FAIL 没有 taskId: %r" % d)
        return 1

    done_at, status_at_done, doc_id = None, None, None
    while time.time() - t0 < 300:
        t = call("GET", "/api/v1/tasks/%s" % tid).get("data") or {}
        if t.get("status") in ("done", "failed"):
            done_at = time.time()
            status_at_done = t.get("reviewStatus")
            doc_id = (t.get("artifact") or {}).get("id") or t.get("docId")
            break
        time.sleep(1)

    if not done_at:
        print("NONBLOCK_FAIL 任务 300s 未 done")
        return 1
    if not doc_id:
        # artifact 可能晚一步，从 task 表取
        v = sql("SELECT COALESCE(doc_id,0) FROM task WHERE id=%s" % int(tid))
        doc_id = int(v) if v.isdigit() else 0

    review_at, verdict = None, None
    while time.time() - t0 < 420:
        v = sql("SELECT id, verdict FROM review WHERE doc_id=%s ORDER BY id DESC LIMIT 1"
                % int(doc_id)) if doc_id else ""
        if v:
            review_at = time.time()
            verdict = v.split("\t")[-1].strip()
            break
        time.sleep(2)

    final_state = (call("GET", "/api/v1/tasks/%s" % tid).get("data") or {}).get("reviewStatus")

    print("  doc=%s task=%s" % (doc_id, tid))
    print("  t_done   = %.1fs (reviewStatus=%s)" % (done_at - t0, status_at_done))
    print("  t_review = %s" % ("%.1fs (verdict=%s)" % (review_at - t0, verdict) if review_at else "未出现"))
    print("  final reviewStatus = %s" % final_state)

    ok = (status_at_done == "pending" and review_at and done_at < review_at
          and final_state == "done")
    # 清理：只删本次
    if tid:
        sql("DELETE FROM message WHERE task_id=%s" % int(tid))
        sql("DELETE FROM task WHERE id=%s" % int(tid))
    if doc_id:
        rel = sql("SELECT COALESCE(path,'') FROM doc WHERE id=%s" % int(doc_id)).strip()
        sql("DELETE FROM review WHERE doc_id=%s" % int(doc_id))
        sql("DELETE FROM doc WHERE id=%s" % int(doc_id))
        if rel:
            fp = os.path.join(ROOT, "data", "workspaces", PROJECT, rel)
            if os.path.isfile(fp):
                os.remove(fp)
    print("NONBLOCK_OK" if ok else "NONBLOCK_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
