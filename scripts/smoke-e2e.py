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
    # 标题必须带 smoke- 前缀：清理时只删自己造的，绝不碰真文书
    # （14:30 出过事：同项目并发时 BFF 把用户的文书当成本轮产物，smoke 清理把它删了）
    text = "smoke-%d#楼东侧外架连墙件缺失，出监理通知单，标题用 smoke-%d#楼" % (
        random.randint(60, 99), random.randint(60, 99))
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
    st, b = call("PUT", "/api/v1/docs/%s" % doc_id,
                 {"fields": {"受检单位": "中建八局华南公司",
                             "依据": "JGJ 130-2011 第 6.4.3 条"}})
    data = b.get("data") or {}
    check("PUT 一个字段 200", st == 200, "state=%s" % data.get("state"))
    check("字段真的存进去了", (doc_row(doc_id) or {}).get("fields", {}).get("受检单位") == "中建八局华南公司")
    check("仍缺项 → 状态还是待补", (doc_row(doc_id) or {}).get("state") == "待补")

    # 3) 补全部缺项
    need = {
        # 依据现在是 schema 必填（文书必须写清依据），这里一起补上
        "依据": "JGJ 130-2011 第 6.4.3 条",
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

    # 4.5) 带图链路：上传 → parts 带 image → 出稿（这是相机按钮走的路）
    try:
        import struct, zlib, tempfile
        w = h = 64
        raw = b"".join(b"\x00" + bytes((120, 160, 90)) * w for _ in range(h))
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(png); tmp.close()
        up = subprocess.run(
            ["curl", "-s", "-m", "60", "-X", "POST", BASE + "/api/v1/upload",
             "-F", "projectId=" + PROJECT, "-F", "userId=980", "-F", "kind=photo",
             "-F", "file=@" + tmp.name + ";type=image/png"],
            capture_output=True, text=True, timeout=90)
        sha = ""
        try:
            sha = (json.loads(up.stdout).get("data") or {}).get("sha256") or ""
        except Exception:
            pass
        check("上传图片拿到 sha", bool(sha), sha[:12])
        if sha:
            st, body = call("POST", "/api/v1/chat", {
                "projectId": PROJECT, "userId": 980,
                "clientId": "smoke-img-" + uuid.uuid4().hex[:8],
                "text": "这张照片是 smoke-%d#楼南侧外架，出监理通知单，标题用 smoke-%d#楼" % (
            random.randint(60, 99), random.randint(60, 99)),
                "parts": [{"type": "image", "blob": sha}],
            })
            tid2 = (body.get("data") or {}).get("taskId")
            d2, seen2, total2 = wait_done(tid2) if tid2 else ({}, None, 0)
            art2 = d2.get("artifact") or {}
            msgs2 = [m for m in (d2.get("messages") or []) if m.get("role") == "assistant"]
            reply = (msgs2[-1].get("text") or "") if msgs2 else ""
            # 这里只验「图确实进了模型」：纯色图没有可用的现场信息，
            # 模型正确拒绝凭它编通知单（实测回复「一片纯绿色，看不出内容」）。
            # 所以断言的是「有回复、没报错」，不是「一定出稿」。
            ok2 = bool(reply) and d2.get("status") == "done"
            check("带图能进模型", ok2,
                  "看到 %.0fs 完成 %.0fs 回复=%s" % (seen2 or -1, total2, reply[:24].replace("\n", " ")))
            if art2.get("id"):
                created.append(art2["id"])
        os.unlink(tmp.name)
    except Exception as exc:
        check("带图能进模型", False, repr(exc)[:80])

    # 4.55) 照片沉淀：批注 → 能按描述搜回来（多模态沉淀的验收点）
    try:
        st, b = call("GET", "/api/v1/photos?projectId=" + PROJECT)
        photos = b.get("data") or []
        if isinstance(photos, dict):
            photos = photos.get("items") or []
        target = next((x for x in photos if x.get("sha256") == sha), None)
        if target:
            pid = target.get("id")
            kw = "smoke批注%d" % random.randint(100, 999)
            # 描述走 POST /photos/caption（参数是 sha256）；PATCH /photos/<id> 只改 place
            # 必须带 overwrite=1：有描述时不覆盖（保护人工改过的描述），但仍返回 200。
            # 第一次没带 → 存的是旧描述 → 后面按新关键词搜不到，白折腾一轮。
            st, b = call("POST", "/api/v1/photos/caption",
                         {"projectId": PROJECT, "sha256": sha, "caption": kw, "overwrite": 1})
            saved = ((b.get("data") or {}).get("skipped") is not True)
            check("照片批注", st == 200 and saved,
                  "HTTP %s skipped=%s" % (st, (b.get("data") or {}).get("skipped")))
            # 搜索参数名是 q（不是 query）；中文必须 urlencode，
            # 否则请求本身失败，而下面会把失败当成「0 条」—— 差点又误判成功能坏了。
            from urllib.parse import quote
            st, b = call("GET", "/api/v1/photos/search?projectId=%s&q=%s"
                         % (PROJECT, quote(kw)))
            hits = b.get("data") or []
            if isinstance(hits, dict):
                hits = hits.get("items") or []
            ok = st == 200 and any(x.get("id") == pid for x in hits)
            check("按描述搜回照片", ok,
                  "HTTP %s 命中 %d 条" % (st, len(hits)))
            if st != 200:
                check("搜索接口可用", False, "错误：%s" % str(b)[:80])
        else:
            check("照片批注", False, "列表里找不到刚上传的那张")
    except Exception as exc:
        check("照片批注", False, repr(exc)[:80])

    # 4.6) 记忆 CRUD：小程序「记忆」页在用（新增 → 列表 → 停用 → 删除）
    st, b = call("POST", "/api/v1/memories", {
        "projectId": PROJECT, "scope": "project", "ownerId": PROJECT,
        "text": "smoke 测试用记忆（自动删除）",
    })
    mid = (b.get("data") or {}).get("id")
    check("建记忆", st in (200, 201) and bool(mid), "id=%s" % mid)
    if mid:
        st, b = call("GET", "/api/v1/memories?projectId=%s&ownerId=%s" % (PROJECT, PROJECT))
        items = (b.get("data") or {}).get("list") if isinstance(b.get("data"), dict) else b.get("data")
        found = any(str(x.get("id")) == str(mid) for x in (items or []))
        check("记忆出现在列表里", found, "共 %d 条" % len(items or []))
        st, b = call("PATCH", "/api/v1/memories/%s" % mid, {"status": 0})
        check("停用记忆", st == 200, "HTTP %s" % st)
        st, b = call("DELETE", "/api/v1/memories/%s" % mid)
        check("删除记忆", st in (200, 204), "HTTP %s" % st)

    # 5) 反向：缺字段不许签
    # POST /api/v1/docs 已下线（它绕过守卫、自己排版）。反向用例改走 register 这条唯一入口。
    st, b = call("POST", "/api/v1/docs/register", {
        "projectId": PROJECT, "kind": "监理通知单",
        "obj": "smoke-反向用例-自动删除", "fields": {"部位": "测试"},
    })
    bad_id = (b.get("data") or {}).get("docId")
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
            try:
                _obj = sql("SELECT COALESCE(obj,'') FROM doc WHERE id=%s" % did).strip()
            except Exception:
                _obj = ""
            if not _obj.startswith("smoke-"):
                print("  跳过删除 doc %s（%s，不是 smoke 造的）" % (did, _obj or "无标题"))
                continue
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
