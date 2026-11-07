#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""负向验收：复核失败时，done 仍然写，且 reviewStatus=failed（客户端据此停轮询）。

做法：直接调用 agent._review_async，用一个不存在的 doc_id 逼复核抛错。
断言：task.status=done 且 task.result.review=failed。
输出最后一行 REVIEWFAIL_OK / REVIEWFAIL_FAIL。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bff"))

from bffpkg import agent  # noqa: E402

PROJECT = "test-sandbox"
USER = 980


def main():
    client_id = "revfail-%d" % int(time.time())
    tid = agent.exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
        "VALUES (%s,%s,%s,'chat',%s,'running')",
        (PROJECT, USER, client_id, json.dumps({"text": "neg"}, ensure_ascii=False)))
    mid = agent.exec_sql(
        "INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync) "
        "VALUES (%s,%s,%s,'assistant','负向用例',%s,1)",
        (PROJECT, USER, "m-%s" % client_id, tid))

    # doc_id 指向不存在文书：_review_pass 取字段就会失败
    try:
        agent._review_async(tid, PROJECT, USER, "neg", [], 999999999, "final",
                            mid, {}, [], 0, None, [], time.time())
    except Exception as e:
        print("  直接抛错（也算失败路径）: %r" % e)

    row = agent.q("SELECT status, result FROM task WHERE id=%s", (tid,))
    st = row[0]["status"] if row else None
    raw = row[0]["result"] if row else None
    try:
        res = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        res = {}
    review = (res or {}).get("review")

    agent.exec_sql("DELETE FROM message WHERE task_id=%s", (tid,))
    agent.exec_sql("DELETE FROM task WHERE id=%s", (tid,))

    ok = st == "done" and review == "failed"
    print("  status=%s review=%s" % (st, review))
    print("REVIEWFAIL_OK" if ok else "REVIEWFAIL_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
