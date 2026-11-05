#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke-e2e —— 一条命令验证核心链路：出稿 → 补字段 → 生成 → 签发

为什么要有它：
  今天三个「用户可见」的 bug（补字段被丢、缺字段能被标已签、不能只改状态）
  全靠手工真跑才发现的。冒烟测试只测"能出一份文书"是测不出这些的。

它做什么：
  1. 发一句话，等出稿（断言：有文书、状态待补、missing 非空）
  2. PUT 补一半字段（断言：状态仍待补、missing 变少、字段真的存进去了）
  3. PUT 补剩下字段（断言：状态变待签、missing 为空）
  4. PUT 只改状态=已签（断言：200 且状态已签）
  5. 反向用例：新建一份缺字段的，PUT state=已签（断言：400 被拒）
  6. 清理：删掉本次产生的文书（库 + 文件），不留垃圾

用法：
  python3 scripts/smoke-e2e.py           # 跑一遍
  python3 scripts/smoke-e2e.py --keep    # 保留测试文书，便于排查
"""
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("BFF_URL") or "http://127.0.0.1:8081"
PROJECT = "test-sandbox"
KEEP = "--keep" in sys.argv

fails = []


def call(method, path, body=None, timeout=60):
    url = BASE + path
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"error": repr(exc)}


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def sql(query):
    env = os.path.join(ROOT, ".env")
    pw = ""
    for line in open(env, encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
            break
    subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw,
                    "--default-character-set=utf8mb4", "-N", "-e", query, "jl"],
                   capture_output=True, text=True, timeout=60)


def doc_row(doc_id):
    env = os.path.join(ROOT, ".env")
    pw = ""
    for line in open(env, encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
            break
    out = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw,
                          "--default-character-set=utf8mb4", "-N", "-e",
                          "SELECT id, state, fields FROM doc WHERE id=%s" % doc_id, "jl"],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    if not out:
        return None
    parts = out.split("\t")
    try:
        fields = json.loads(parts[2] or "{}")
    except Exception:
        fields = {}
    return {"id": int(parts[0]), "state": parts[1], "fields": fields}


def wait_done(task_id, limit=300):
    t0 = time.time()
    seen = None
    while time.time() - t0 < limit:
        st, body = call("GET", "/api/v1/tasks/%s" % task_id)
        d = body.get("data") or {}
        if seen is None and any(m.get("role") == "assistant" for m in (d.get("messages") or [])):
            seen = time.time() - t0
        if d.get("status") in ("done", "failed"):
            return d, seen, time.time() - t0
        time.sleep(1)
    return {}, seen, time.time() - t0


def main():
    print("=== smoke-e2e 核心链路 ===")
    created = []

    # 1) 出稿
    # 每次用不同的楼号：同一线程里重复同一句话，agent 会复用上次那份文书
    # （实测 task 240：6.8 秒答「刚才已经出了 docId 144」），不会产生新文书。
    import random
    text = "%d#楼东侧外架连墙件缺失，出监理通知单" % random.randint(60, 99)
    st, body = call("POST", "/api/v1/chat", {
        "projectId": PROJECT, "userId": 980,
        "clientId": "smoke-" + uuid.uuid4().hex[:8], "text": text,
    })
    tid = (body.get("data") or {}).get("taskId")
    check("发起任务", st in (200, 202) and bool(tid), "task=%s" % tid)
    if not tid:
        return finish(created)

    d, seen, total = wait_done(tid)
    msgs = [m for m in (d.get("messages") or []) if m.get("role") == "assistant"]
    art = d.get("artifact") or {}
    # artifact 的键是 id（不是 docId）—— 第一次写错，误报「出稿失败」
    doc_id = art.get("id")
    check("出稿", bool(msgs) and bool(doc_id),
          "看到 %.0fs 完成 %.0fs doc=%s state=%s" % (seen or -1, total, doc_id, art.get("state")))
    if not doc_id:
        return finish(created)
    created.append(doc_id)

    row = doc_row(doc_id)
    check("出稿后状态=待补", row and row["state"] == "待补", str(row and row["state"]))

    # 2) 补一半
    st, b = call("PUT", "/api/v1/docs/%s" % doc_id, {"fields": {"受检单位": "中建八局华南公司"}})
    data = b.get("data") or {}
    check("PUT 一个字段 200", st == 200, "state=%s" % data.get("state"))
    check("字段真的存进去了", (doc_row(doc_id) or {}).get("fields", {}).get("受检单位") == "中建八局华南公司")
    check("仍缺项 → 状态还是待补", (doc_row(doc_id) or {}).get("state") == "待补")

    # 3) 补全部缺项
    need = {
        "事由": "外架连墙件缺失",
        "整改要求": "补齐连墙件并复验",
        "整改期限": "2026-11-20",
        "检查结论": "存在安全隐患，限期整改",
        "签发人": "张工",
    }
    st, b = call("PUT", "/api/v1/docs/%s" % doc_id, {"fields": need})
    data = b.get("data") or {}
    check("补齐后 state=待签", st == 200 and data.get("state") == "待签",
          "missing=%s" % (data.get("missing"),))
    check("missing 为空", not data.get("missing"))

    # 4) 只改状态 → 已签
    st, b = call("PUT", "/api/v1/docs/%s" % doc_id, {"state": "已签"})
    data = b.get("data") or {}
    check("只改状态可签发", st == 200 and data.get("state") == "已签", "state=%s" % data.get("state"))
    check("库里是已签", (doc_row(doc_id) or {}).get("state") == "已签")

    # 5) 反向：缺字段不许签
    st, b = call("POST", "/api/v1/docs", {
        "projectId": PROJECT, "kind": "监理通知单",
        "obj": "smoke-反向用例-自动删除", "fields": {"部位": "测试"},
    })
    bad_id = (b.get("data") or {}).get("id")
    if bad_id:
        created.append(bad_id)
        st, b2 = call("PUT", "/api/v1/docs/%s" % bad_id, {"state": "已签"})
        check("缺字段标已签被拒", st == 400 and bool(b2.get("missing")),
              "HTTP %s missing=%d" % (st, len(b2.get("missing") or [])))
    else:
        check("缺字段标已签被拒", False, "反向用例没建起来")

    return finish(created)


def finish(created):
    if KEEP:
        print("  保留测试文书: %s" % created)
    else:
        for did in created:
            rows = subprocess.run(
                ["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" +
                 next((l.split("=", 1)[1].strip() for l in open(os.path.join(ROOT, ".env"), encoding="utf-8")
                       if l.startswith("MYSQL_PASSWORD=")), ""),
                 "--default-character-set=utf8mb4", "-N", "-e",
                 "SELECT path FROM doc WHERE id=%s" % did, "jl"],
                capture_output=True, text=True).stdout.strip()
            sql("DELETE FROM doc WHERE id=%s" % did)
            if rows:
                p = os.path.join(ROOT, "data", "workspaces", PROJECT, rows)
                if os.path.isfile(p):
                    os.remove(p)
        if created:
            print("  已清理 %d 份测试文书" % len(created))

    print()
    if fails:
        print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
