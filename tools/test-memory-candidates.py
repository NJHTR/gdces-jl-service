#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase1 记忆候选（memory_candidate）回归测试。

在服务器上跑（BFF 已启动）：
    cd /home/baize/jl-service
    python3 tools/test-memory-candidates.py                  # http + perm
    python3 tools/test-memory-candidates.py --mode http      # 只打运行中的 BFF
    AUTH_REQUIRED=1 python3 tools/test-memory-candidates.py --mode perm

覆盖（对应 task-1 验收）：
  1. 候选创建（user / project_user / project）、重复不新增、非法参数拒绝
  2. 列表：pending / accepted 过滤、个人候选只归本人
  3. pending 项目候选**不改变** records/rules.md、不写 memory
  4. 接受 user/project_user → memory(active=status=1) + 个人文件刷新
  5. 拒绝 → 只改状态，memory/文件都不动
  6. 项目候选接受 → 只有总监/owner/admin（perm 模式验 403）+ rules.md 重渲染 + audit
  7. 幂等：重复确认 409、不存在 404

测试数据用 test-sandbox 项目 + 测试用户 99999，结束时删除候选/记忆并重渲染文件。
不碰真实项目规矩与真实用户记忆。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
DEFAULT_BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
DEFAULT_PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
DEFAULT_USER = os.environ.get("JL_TEST_USER") or "99999"
PREFIX_TS = time.strftime("%m%d%H%M%S")
PREFIX = "【自测%s】" % PREFIX_TS

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append((name, detail))
        print("  FAIL  %s  %s" % (name, detail))


def eq(name, got, want):
    check(name, got == want, "got=%r want=%r" % (got, want))


def load_bff():
    bff_dir = os.path.join(ROOT, "bff")
    if bff_dir not in sys.path:
        sys.path.insert(0, bff_dir)
    import bffpkg.core as core  # noqa: E402
    import bffpkg.biz as biz    # noqa: E402
    import bffpkg.memcandidate as mc  # noqa: E402
    return core, biz, mc


# ---------------------------------------------------------------- HTTP
def api(method, path, body=None, token=None, base=None):
    base = base or DEFAULT_BASE
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads((r.read() or b"{}").decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, {"raw": raw[:200]}


def http_data(payload):
    d = payload.get("data") if isinstance(payload, dict) else None
    return d


def delete_candidates_prefix(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memory_candidate WHERE text LIKE %s", (PREFIX + "%",))
        n1 = cur.rowcount
        cur.execute("DELETE FROM memory WHERE text LIKE %s", (PREFIX + "%",))
        n2 = cur.rowcount
    return n1, n2


def run_http(args):
    core, biz, mc = load_bff()
    conn = core.db()
    print("\n=== HTTP 模式：%s  project=%s user=%s ===" % (DEFAULT_BASE, args.project, args.user))
    token = args.token or ""
    base = args.base
    # 判断服务端是否开鉴权：无 token 拿列表，401 就是开了
    code, _ = api("GET", "/api/v1/memory-candidates?projectId=%s" % urllib.parse.quote(args.project),
                  token=token or None, base=base)
    auth_on = (code == 401)
    if auth_on and not token:
        print("  SKIP HTTP：服务端开了 AUTH_REQUIRED，请带 --token（总监）再跑")
        return
    print("  (服务端鉴权: %s)" % ("on" if auth_on else "off/demo"))
    if auth_on:
        print("  SKIP HTTP：本项目测试需要在演示模式跑 accept 成功路径；鉴权模式请用 --mode perm")
        return

    pid = args.project
    uid = args.user
    rules_path = os.path.join(core.WS_ROOT, pid, "records", "rules.md")

    def rules_text():
        try:
            with open(rules_path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def memory_rows(text_like=None):
        with conn.cursor() as cur:
            if text_like:
                cur.execute("SELECT id, scope, owner_id, project_id, source, status, text "
                            "FROM memory WHERE text LIKE %s", (text_like,))
            else:
                cur.execute("SELECT id FROM memory")
            return cur.fetchall()

    created_ids = []
    memory_ids = []
    rules_before = rules_text()
    try:
        # ---- 1) 创建 ----
        texts = {
            "user": PREFIX + "依据必须写到条号",
            "project_user": PREFIX + "本项目通知单抬头固定写广东监理",
            "project": PREFIX + "整改回复必须附前后对比照片",
        }
        body = {"projectId": pid, "userId": uid, "scope": "user", "text": texts["user"],
                "evidence": "测试：用户说以后都这么写"}
        code, p = api("POST", "/api/v1/memory-candidates", body, token or None, base)
        u = http_data(p) or {}
        eq("创建 user 候选 200", code, 200)
        check("创建返回 id/status=pending", bool(u.get("id")) and u.get("status") == "pending",
              repr(p)[:200])
        if u.get("id"):
            created_ids.append(u["id"])
        code2, p2 = api("POST", "/api/v1/memory-candidates", body, token or None, base)
        check("重复创建不新增", (http_data(p2) or {}).get("id") == u.get("id")
              and (http_data(p2) or {}).get("duplicated") is True, repr(p2)[:200])

        code, p = api("POST", "/api/v1/memory-candidates",
                      {"projectId": pid, "userId": uid, "scope": "project_user",
                       "text": texts["project_user"]}, token or None, base)
        pu = http_data(p) or {}
        check("创建 project_user 候选 200", code == 200 and bool(pu.get("id")), repr(p)[:200])
        if pu.get("id"):
            created_ids.append(pu["id"])

        code, p = api("POST", "/api/v1/memory-candidates",
                      {"projectId": pid, "userId": uid, "scope": "project",
                       "text": texts["project"]}, token or None, base)
        pr = http_data(p) or {}
        check("创建 project 候选 200", code == 200 and bool(pr.get("id")), repr(p)[:200])
        if pr.get("id"):
            created_ids.append(pr["id"])

        code, _ = api("POST", "/api/v1/memory-candidates",
                      {"projectId": pid, "userId": uid, "scope": "bad", "text": "x"},
                      token or None, base)
        eq("非法 scope 400", code, 400)
        code, _ = api("POST", "/api/v1/memory-candidates",
                      {"projectId": pid, "userId": uid, "scope": "user", "text": "   "},
                      token or None, base)
        eq("空 text 400", code, 400)

        # ---- 2) 列表 / 可见性 ----
        code, p = api("GET", "/api/v1/memory-candidates?projectId=%s&status=pending"
                      % urllib.parse.quote(pid), token=token or None, base=base)
        rows = http_data(p) or []
        ids = [r.get("id") for r in rows]
        check("pending 列表含三条候选", all(i in ids for i in created_ids),
              "ids=%r want=%r" % (ids, created_ids))
        personal = [r for r in rows if r.get("scope") in ("user", "project_user")
                    and str(r.get("userId")) == str(uid)]
        check("个人候选可见且带 scopeLabel", all(r.get("scopeLabel") for r in personal)
              and len(personal) >= 2, repr([r.get("scopeLabel") for r in personal]))
        check("列表不带已决状态", all(r.get("status") == "pending" for r in rows))

        # ---- 3) pending 不改变 rules.md / memory ----
        check("pending 项目候选不改 records/rules.md", rules_text() == rules_before)
        check("pending 不写 memory", memory_rows(PREFIX + "%") == [])

        # ---- 4) 接受 user / project_user ----
        code, p = api("POST", "/api/v1/memory-candidates/%s/accept" % u["id"],
                      None, token or None, base)
        d = http_data(p) or {}
        eq("accept user 候选 200", code, 200)
        eq("accept user status", d.get("status"), "accepted")
        memory_ids.append(d.get("memoryId"))
        with conn.cursor() as cur:
            cur.execute("SELECT scope, owner_id, project_id, source, status FROM memory "
                        "WHERE id=%s", (d.get("memoryId"),))
            m = cur.fetchone()
        check("user 候选写 memory(scope=user,project_id=NULL,source=confirmed,status=1)",
              bool(m) and m["scope"] == "user" and str(m["owner_id"]) == str(uid)
              and m["project_id"] is None and m["source"] == "confirmed"
              and int(m["status"]) == 1, repr(m))
        mem_path = os.path.join(core.WS_ROOT, "..", "memory", "users", str(uid), "MEMORY.md")
        mem_text = ""
        try:
            with open(mem_path, encoding="utf-8") as f:
                mem_text = f.read()
        except OSError:
            pass
        check("个人 MEMORY.md 已刷新", texts["user"] in mem_text, mem_path)

        code, p = api("POST", "/api/v1/memory-candidates/%s/accept" % pu["id"],
                      None, token or None, base)
        d2 = http_data(p) or {}
        eq("accept project_user 候选 200", code, 200)
        memory_ids.append(d2.get("memoryId"))
        with conn.cursor() as cur:
            cur.execute("SELECT scope, owner_id, project_id, source, status FROM memory "
                        "WHERE id=%s", (d2.get("memoryId"),))
            m2 = cur.fetchone()
        check("project_user 写 memory(project_id=项目)",
              bool(m2) and m2["scope"] == "user" and str(m2["project_id"]) == str(pid),
              repr(m2))
        pm_path = os.path.join(core.WS_ROOT, "..", "memory", "projects", str(uid),
                               "%s.md" % pid)
        pm_text = ""
        try:
            with open(pm_path, encoding="utf-8") as f:
                pm_text = f.read()
        except OSError:
            pass
        check("个人×项目文件已刷新", texts["project_user"] in pm_text, pm_path)

        # ---- 5) 拒绝 project 候选：不写 memory / 不改 rules ----
        code, p = api("POST", "/api/v1/memory-candidates/%s/reject" % pr["id"],
                      None, token or None, base)
        eq("reject 200", code, 200)
        eq("reject status", (http_data(p) or {}).get("status"), "rejected")
        check("reject 不改 rules.md", rules_text() == rules_before)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM memory WHERE text=%s", (texts["project"],))
            n = int(cur.fetchone()["n"])
        eq("reject 不写 memory", n, 0)

        # ---- 6) 项目候选接受（演示模式/无鉴权时允许）→ rules.md 重渲染 ----
        code, p = api("POST", "/api/v1/memory-candidates",
                      {"projectId": pid, "userId": uid, "scope": "project",
                       "text": texts["project"] + "（接受版）"}, token or None, base)
        pr2 = http_data(p) or {}
        if pr2.get("id"):
            created_ids.append(pr2["id"])
        code, p = api("POST", "/api/v1/memory-candidates/%s/accept" % pr2.get("id"),
                      None, token or None, base)
        d3 = http_data(p) or {}
        eq("accept project 候选 200", code, 200)
        memory_ids.append(d3.get("memoryId"))
        with conn.cursor() as cur:
            cur.execute("SELECT scope, owner_id, project_id, source, status FROM memory "
                        "WHERE id=%s", (d3.get("memoryId"),))
            m3 = cur.fetchone()
        check("项目候选写 memory(scope=project,source=confirmed)",
              bool(m3) and m3["scope"] == "project" and str(m3["owner_id"]) == str(pid)
              and m3["source"] == "confirmed" and int(m3["status"]) == 1, repr(m3))
        check("accept 项目候选后 rules.md 重渲染",
              texts["project"] + "（接受版）" in (rules_text() or ""))
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='memory_candidate.accept'")
            eq("accept 写 audit_log", int(cur.fetchone()["n"]) >= 2, True)

        # ---- 7) 幂等 / 404 ----
        code, _ = api("POST", "/api/v1/memory-candidates/%s/accept" % u["id"],
                      None, token or None, base)
        eq("重复 accept 409", code, 409)
        code, _ = api("POST", "/api/v1/memory-candidates/999999999/accept",
                      None, token or None, base)
        eq("不存在候选 404", code, 404)
        code, p = api("GET", "/api/v1/memory-candidates?projectId=%s&status=accepted"
                      % urllib.parse.quote(pid), token=token or None, base=base)
        acc_ids = [r.get("id") for r in (http_data(p) or [])]
        check("accepted 列表含已接受候选", u["id"] in acc_ids and pu["id"] in acc_ids)
    finally:
        # ---- 清理：候选 / 记忆 / 渲染文件 ----
        try:
            n1, n2 = delete_candidates_prefix(conn)
            for mid in memory_ids:
                if mid:
                    try:
                        core.exec_sql("DELETE FROM memory WHERE id=%s", (mid,))
                    except Exception:
                        pass
            biz.render_user_memory(uid)
            biz.render_user_project_md(uid, pid)
            biz.render_rules_md(pid)
            shutil.rmtree(os.path.join(core.WS_ROOT, "..", "memory", "users", str(uid)),
                          ignore_errors=True)
            shutil.rmtree(os.path.join(core.WS_ROOT, "..", "memory", "projects", str(uid)),
                          ignore_errors=True)
            print("  (清理候选 %d 条 / 记忆 %d 条)" % (n1, n2))
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- perm
def run_perm(args):
    core, biz, mc = load_bff()
    if not core.AUTH_REQUIRED:
        print("\n=== perm 模式需要 AUTH_REQUIRED=1（当前 0），跳过 ===")
        return
    print("\n=== perm 模式（进程内 AUTH_REQUIRED=1）：项目 yun-gu-3qi / 用户 1(owner) / 2(engineer) ===")
    conn = core.db()
    pid = "yun-gu-3qi"
    owner = {"id": 1, "name": "李工", "role": "监理员"}
    engineer = {"id": 2, "name": "王工", "role": "监理员"}
    ids = []
    try:
        mc.ensure_memory_candidate_table()
        # 造：user1 的个人候选、项目共享候选（yun-gu-3qi）、test-sandbox 项目候选
        c_personal = core.exec_sql(
            "INSERT INTO memory_candidate (project_id, user_id, scope, text, evidence) "
            "VALUES (%s,%s,'user',%s,'perm test')",
            (pid, "1", PREFIX + "个人候选-归属用户1"))
        c_proj = core.exec_sql(
            "INSERT INTO memory_candidate (project_id, user_id, scope, text) "
            "VALUES (%s,%s,'project',%s)",
            (pid, "2", PREFIX + "项目候选-权限测试"))
        c_other = core.exec_sql(
            "INSERT INTO memory_candidate (project_id, user_id, scope, text) "
            "VALUES ('test-sandbox','1','project',%s)", (PREFIX + "别的项目候选",))
        ids = [c_personal, c_proj, c_other]

        # 1) 个人候选：别的用户不能看不能接受
        code, payload = mc.handle_candidate_decide(c_personal, "accept", engineer)
        eq("普通用户接受他人个人候选 403", code, 403)
        code, payload = mc.handle_candidate_decide(c_personal, "reject", engineer)
        eq("普通用户拒绝他人个人候选 403", code, 403)
        code, payload = mc.handle_candidate_list({"projectId": pid}, engineer)
        rows = payload.get("data") or []
        check("列表不含他人个人候选",
              all(r.get("id") != c_personal for r in rows), repr([r.get("id") for r in rows]))
        code, payload = mc.handle_candidate_list({"projectId": pid}, owner)
        rows = payload.get("data") or []
        check("本人能看到自己的个人候选",
              any(r.get("id") == c_personal for r in rows))

        # 2) 项目候选：总监/owner 可确认，普通成员 403
        eq("owner 可确认项目候选", mc.can_confirm_project_candidate(pid, owner), True)
        eq("普通工程师不可确认项目候选", mc.can_confirm_project_candidate(pid, engineer), False)
        code, payload = mc.handle_candidate_decide(c_proj, "accept", engineer)
        eq("普通成员接受项目候选 403", code, 403)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM memory_candidate WHERE id=%s", (c_proj,))
            eq("403 后项目候选仍 pending", cur.fetchone()["status"], "pending")

        # 3) 项目候选可见性：成员可见，非成员看不到别的项目
        code, payload = mc.handle_candidate_list({"projectId": pid}, engineer)
        rows = payload.get("data") or []
        check("项目成员能看到项目候选", any(r.get("id") == c_proj for r in rows))
        check("非成员的别的项目候选不可见",
              all(r.get("id") != c_other for r in rows))
        code, payload = mc.handle_candidate_list({"projectId": "test-sandbox"}, engineer)
        eq("非成员访问别项目列表 403", code, 403)

        # 4) 登录用户不能替别人提交候选
        code, payload = mc.handle_candidate_create(
            {"projectId": pid, "userId": "1", "scope": "user", "text": PREFIX + "冒名"}, engineer)
        eq("登录用户冒名提交 403", code, 403)

        # 5) 项目候选拒绝：成员可以（只改状态）
        code, payload = mc.handle_candidate_decide(c_proj, "reject", engineer)
        eq("普通成员可忽略项目候选", code, 200)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM memory_candidate WHERE id=%s", (c_proj,))
            eq("忽略后 status=rejected", cur.fetchone()["status"], "rejected")
    finally:
        try:
            with conn.cursor() as cur:
                for cid in ids:
                    cur.execute("DELETE FROM memory_candidate WHERE id=%s", (cid,))
                cur.execute("DELETE FROM memory_candidate WHERE text LIKE %s", (PREFIX + "%",))
                cur.execute("DELETE FROM memory WHERE text LIKE %s", (PREFIX + "%",))
        except Exception:
            traceback.print_exc()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("http", "perm", "all"), default="all")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    if args.mode == "perm":
        os.environ["AUTH_REQUIRED"] = "1"
    if args.mode == "all":
        run_http(args)
        env = dict(os.environ, AUTH_REQUIRED="1")
        rc = subprocess.run([sys.executable, os.path.abspath(__file__),
                             "--mode", "perm", "--project", args.project],
                            env=env).returncode
        if rc != 0:
            FAILS.append(("perm 子进程", "exit=%d" % rc))
    elif args.mode == "http":
        run_http(args)
    else:
        run_perm(args)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
