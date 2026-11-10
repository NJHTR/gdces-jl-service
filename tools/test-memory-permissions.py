#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA F1-F3 / R1 权限回归（成员 vs owner，演示模式 AUTH_REQUIRED=0 也要拦）。

    cd /home/baize/jl-service
    python3 tools/test-memory-permissions.py            # 单元(进程内) + HTTP 端到端
    python3 tools/test-memory-permissions.py --mode unit
    python3 tools/test-memory-permissions.py --mode http

背景：QA 发现 AUTH_REQUIRED=0 时 can_confirm_project_rule 直接 True，导致
登录的普通成员（project_member.role=member/engineer）也能：
  F1 accept rule_proposal、F2 PATCH active 项目记忆、F3 DELETE active 项目记忆。
修复口径：登录用户且在该项目有角色行 → 只看角色（owner/admin/总监）；只有匿名/无角色行才演示放行。
另外 R1：旧 /memories 的 GET/POST/recall 要按 token 收敛个人域（他人 ownerId/userId → 403）。

测试项目用 yun-gu-3qi（真实成员：user1=owner, user2=engineer），临时数据全部清理。
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "yun-gu-3qi"
OWNER_ID = int(os.environ.get("JL_TEST_OWNER", "1"))     # yun-gu-3qi owner
MEMBER_ID = int(os.environ.get("JL_TEST_MEMBER", "2"))   # yun-gu-3qi engineer
PREFIX = "【权限自测】"

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append((name, detail))
        print("  FAIL  %s  %s" % (name, detail))


def eq(name, got, want, detail=""):
    check(name, got == want, detail or ("got=%r want=%r" % (got, want)))


def load():
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.core as core
    import bffpkg.biz as biz
    import bffpkg.agent as agent
    import bffpkg.memcandidate as mc
    return core, biz, agent, mc


def add_project_memory(core, pid, text):
    return core.exec_sql(
        "INSERT INTO memory (scope, owner_id, project_id, text, source, applies_to, status, updated_at) "
        "VALUES ('project',%s,NULL,%s,'manual',%s,1,NOW(3))",
        (pid, text, "project:%s" % pid))


def _test_notes(core):
    """handle_memory_create 写个人笔记的落点：homes/<uid>/memories/extensions/ad_hoc/notes/。
    返回内容里带测试前缀的 note 文件（QA O1：之前 cleanup 只清 DB，note 会残留）。"""
    out = []
    pattern = os.path.join(core.HOMES_ROOT, "*", "memories", "extensions",
                           "ad_hoc", "notes", "*.md")
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            continue
        if PREFIX in body:
            out.append(path)
    return out


def cleanup(core, biz, pid):
    core.exec_sql("DELETE FROM memory WHERE text LIKE %s", ("%" + PREFIX + "%",))
    core.exec_sql("DELETE FROM memory_candidate WHERE text LIKE %s", ("%" + PREFIX + "%",))
    core.exec_sql("DELETE FROM rule_proposal WHERE text LIKE %s", ("%" + PREFIX + "%",))
    # QA O1：清 个人笔记 ad_hoc/notes 里的测试 note（只删内容含测试前缀的，不碰真人笔记）
    for path in _test_notes(core):
        try:
            os.remove(path)
        except OSError:
            traceback.print_exc()
    try:
        biz.render_rules_md(pid)
        # 个人记忆文件也要把测试行刷掉（测试会写 owner 的 MEMORY.md）
        for uid in (OWNER_ID, MEMBER_ID):
            biz.render_user_memory(str(uid))
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- unit（进程内）
def run_unit():
    core, biz, agent, mc = load()
    pid = PROJECT
    owner = {"id": OWNER_ID, "name": "owner", "role": "监理员"}
    member = {"id": MEMBER_ID, "name": "member", "role": "监理员"}
    print("\n=== unit 模式（进程内，AUTH_REQUIRED=%s）project=%s owner=%s member=%s ==="
          % (os.environ.get("AUTH_REQUIRED", "0"), pid, OWNER_ID, MEMBER_ID))
    rules_before = _read_rules(core, pid)
    try:
        # ---- 门禁同口径 ----
        eq("登录 owner 可确认项目规矩", biz.can_confirm_project_rule(pid, owner), True)
        eq("登录普通成员不可确认项目规矩", biz.can_confirm_project_rule(pid, member), False)
        eq("候选确认门禁与规矩同口径(member)",
           mc.can_confirm_project_candidate(pid, member), False)
        eq("候选确认门禁与规矩同口径(owner)",
           mc.can_confirm_project_candidate(pid, owner), True)
        eq("无角色行的登录用户演示放行（F1 口径）",
           biz.can_confirm_project_rule("test-sandbox", member), True)

        # ---- F2 PATCH ----
        mid = add_project_memory(core, pid, PREFIX + "PATCH 用项目记忆")
        code, payload = agent.handle_memory_patch(mid, {"status": 1}, member)
        eq("F2 普通成员 PATCH 项目记忆 403", code, 403, repr(payload)[:160])
        code, payload = agent.handle_memory_patch(mid, {"status": 1}, owner)
        eq("F2 owner PATCH 项目记忆 200", code, 200, repr(payload)[:160])
        check("F2 成员 403 后正文未被改",
              _mem_text(core, mid) == PREFIX + "PATCH 用项目记忆")

        # ---- F3 DELETE ----
        mid2 = add_project_memory(core, pid, PREFIX + "DELETE 用项目记忆")
        code, payload = agent.handle_memory_delete(mid2, member)
        eq("F3 普通成员 DELETE 项目记忆 403", code, 403, repr(payload)[:160])
        check("F3 成员 403 后记忆仍在", _mem_exists(core, mid2))
        code, payload = agent.handle_memory_delete(mid2, owner)
        eq("F3 owner DELETE 项目记忆 200", code, 200, repr(payload)[:160])
        check("F3 owner 删除后记忆消失", not _mem_exists(core, mid2))

        # ---- F1 accept proposal ----
        rid = core.exec_sql(
            "INSERT INTO rule_proposal (project_id, user_id, text, source) "
            "VALUES (%s,%s,%s,'manual')", (pid, str(MEMBER_ID), PREFIX + "成员提案"))
        code, payload = biz.handle_rule_decide(rid, {"action": "accept"}, member)
        eq("F1 普通成员接受项目规矩提案 403", code, 403, repr(payload)[:160])
        check("F1 403 后提案仍 pending", _proposal_status(core, rid) == "pending")
        code, payload = biz.handle_rule_decide(rid, {"action": "accept"}, owner)
        eq("F1 owner 接受项目规矩提案 200", code, 200, repr(payload)[:160])
        check("F1 owner 接受后提案 accepted", _proposal_status(core, rid) == "accepted")
        check("F1 owner 接受后 rules.md 出现该条", PREFIX + "成员提案" in _read_rules(core, pid))

        # ---- R1 旧 /memories 个人域 ----
        code, payload = agent.handle_memory_list({"scope": "user", "ownerId": str(OWNER_ID)}, member)
        eq("R1 member 查他人个人记忆 403", code, 403, repr(payload)[:160])
        code, payload = agent.handle_memory_list({"scope": "user", "ownerId": str(OWNER_ID)}, owner)
        eq("R1 owner 查自己个人记忆 200", code, 200)
        code, payload = agent.handle_memory_create(
            {"scope": "user", "ownerId": str(OWNER_ID), "text": PREFIX + "冒名写个人记忆"}, member)
        eq("R1 member 替他人写个人记忆 403", code, 403, repr(payload)[:160])
        code, payload = agent.handle_memory_create(
            {"scope": "user", "ownerId": str(OWNER_ID), "text": PREFIX + "本人写个人记忆"}, owner)
        eq("R1 owner 写自己个人记忆 200", code, 200, repr(payload)[:160])
        code, payload = agent.handle_memory_recall(
            {"projectId": pid, "userId": str(OWNER_ID), "q": "自测"}, member)
        eq("R1 member recall 他人 userId 403", code, 403, repr(payload)[:160])
        code, payload = agent.handle_memory_recall(
            {"projectId": pid, "userId": str(OWNER_ID), "q": "自测"}, owner)
        eq("R1 owner recall 自己 userId 200", code, 200, repr(payload)[:160])

        # ---- 个人记忆仍只能本人改 ----
        umid = core.exec_sql(
            "INSERT INTO memory (scope, owner_id, project_id, text, source, applies_to, status, updated_at) "
            "VALUES ('user',%s,NULL,%s,'manual',%s,1,NOW(3))",
            (str(OWNER_ID), PREFIX + "个人记忆归属测试", "user:%s" % OWNER_ID))
        code, _ = agent.handle_memory_patch(umid, {"status": 1}, member)
        eq("他人不能 PATCH 别人个人记忆 403", code, 403)
        code, _ = agent.handle_memory_delete(umid, member)
        eq("他人不能 DELETE 别人个人记忆 403", code, 403)

        # ---- R3：build_context/AGENTS 指针不重复 ----
        ctx = agent.build_context(pid, OWNER_ID)
        if "【在哪找】" in ctx:
            sec = ctx.split("【在哪找】", 1)[1].split("\n\n", 1)[0]
            eq("R3 项目规矩指针只出现一次", sec.count("项目规矩 `records/rules.md`"), 1,
               repr(sec)[:300])
            photos_exists = os.path.isfile(os.path.join(core.WS_ROOT, pid, "records", "photos.md"))
            eq("R3 影像索引指针不重复", sec.count("records/photos.md"),
               1 if photos_exists else 0, repr(sec)[:300])
        else:
            check("R3 【在哪找】段存在", False, repr(ctx)[:300])
        eq("R3 全文项目规矩指针不重复", ctx.count("项目规矩 `records/rules.md`"), 1)
    finally:
        cleanup(core, biz, pid)
        check("unit 测试后 rules.md 恢复基线", _read_rules(core, pid) == rules_before)
        check("O1 unit 测试 ad_hoc note 已清理", not _test_notes(core))


# ---------------------------------------------------------------- http（端到端）
def _api(method, path, body=None, token=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    req.add_header("referer", "http://localhost/")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads((r.read() or b"{}").decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, {"raw": raw[:200]}


def run_http():
    core, biz, agent, mc = load()
    pid = PROJECT
    print("\n=== http 模式：%s project=%s ===" % (BASE, pid))
    if not _health_ok():
        print("  SKIP http：服务没起来")
        return
    owner_tok = core.issue_token(OWNER_ID, device="perm-test")
    member_tok = core.issue_token(MEMBER_ID, device="perm-test")
    rules_before = _read_rules(core, pid)
    try:
        # ---- F2 PATCH ----
        mid = add_project_memory(core, pid, PREFIX + "HTTP PATCH 用项目记忆")
        code, p = _api("PATCH", "/api/v1/memories/%s" % mid, {"status": 1}, member_tok)
        eq("HTTP F2 普通成员 PATCH 项目记忆 403", code, 403, repr(p)[:160])
        code, p = _api("PATCH", "/api/v1/memories/%s" % mid, {"status": 1}, owner_tok)
        eq("HTTP F2 owner PATCH 项目记忆 200", code, 200, repr(p)[:160])
        code, p = _api("PATCH", "/api/v1/memories/%s" % mid, {"status": 1})   # 匿名演示
        eq("HTTP F2 匿名演示仍放行（预览不破坏）", code, 200, repr(p)[:160])

        # ---- F3 DELETE ----
        mid2 = add_project_memory(core, pid, PREFIX + "HTTP DELETE 用项目记忆")
        code, p = _api("DELETE", "/api/v1/memories/%s" % mid2, None, member_tok)
        eq("HTTP F3 普通成员 DELETE 项目记忆 403", code, 403, repr(p)[:160])
        code, p = _api("DELETE", "/api/v1/memories/%s" % mid2, None, owner_tok)
        eq("HTTP F3 owner DELETE 项目记忆 200", code, 200, repr(p)[:160])

        # ---- F1 accept proposal ----
        code, p = _api("POST", "/api/v1/rule-proposals",
                       {"projectId": pid, "text": PREFIX + "HTTP 成员提案", "source": "manual"},
                       member_tok)
        rid = ((p.get("data") or {}).get("id"))
        check("HTTP 创建规矩提案 200", code == 200 and bool(rid), repr(p)[:160])
        code, p = _api("POST", "/api/v1/rule-proposals/%s/accept" % rid, None, member_tok)
        eq("HTTP F1 普通成员接受规矩提案 403", code, 403, repr(p)[:160])
        code, p = _api("POST", "/api/v1/rule-proposals/%s/accept" % rid, None, owner_tok)
        eq("HTTP F1 owner 接受规矩提案 200", code, 200, repr(p)[:160])
        check("HTTP F1 owner 接受后 rules.md 出现该条",
              PREFIX + "HTTP 成员提案" in _read_rules(core, pid))

        # ---- R1 ----
        code, p = _api("GET", "/api/v1/memories?scope=user&ownerId=%s" % OWNER_ID,
                       None, member_tok)
        eq("HTTP R1 member 查他人个人记忆 403", code, 403, repr(p)[:160])
        code, p = _api("GET", "/api/v1/memories?scope=user&ownerId=%s" % OWNER_ID,
                       None, owner_tok)
        eq("HTTP R1 owner 查自己个人记忆 200", code, 200, repr(p)[:160])
        code, p = _api("POST", "/api/v1/memories",
                       {"scope": "user", "ownerId": OWNER_ID,
                        "text": PREFIX + "HTTP 冒名个人记忆"}, member_tok)
        eq("HTTP R1 member 替他人写个人记忆 403", code, 403, repr(p)[:160])
        code, p = _api("POST", "/api/v1/memories",
                       {"scope": "user", "ownerId": OWNER_ID,
                        "text": PREFIX + "HTTP 本人个人记忆"}, owner_tok)
        eq("HTTP R1 owner 写自己个人记忆 200", code, 200, repr(p)[:160])
        code, p = _api("GET", "/api/v1/memories/recall?projectId=%s&userId=%s&q=%s"
                       % (urllib.parse.quote(pid), OWNER_ID, urllib.parse.quote("自测")),
                       None, member_tok)
        eq("HTTP R1 member recall 他人 userId 403", code, 403, repr(p)[:160])
        code, p = _api("GET", "/api/v1/memories/recall?projectId=%s&userId=%s&q=%s"
                       % (urllib.parse.quote(pid), OWNER_ID, urllib.parse.quote("自测")),
                       None, owner_tok)
        eq("HTTP R1 owner recall 自己 userId 200", code, 200, repr(p)[:160])
    finally:
        cleanup(core, biz, pid)
        core.exec_sql("DELETE FROM auth_token WHERE device=%s", ("perm-test",))
        check("HTTP 测试后 rules.md 恢复基线", _read_rules(core, pid) == rules_before)
        check("O1 HTTP 测试 ad_hoc note 已清理", not _test_notes(core))


def _health_ok():
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=10) as r:
            return json.loads(r.read() or b"{}").get("ok") is True
    except Exception:
        return False


def _read_rules(core, pid):
    try:
        with open(os.path.join(core.WS_ROOT, pid, "records", "rules.md"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _mem_text(core, mid):
    rows = core.q("SELECT text FROM memory WHERE id=%s", (mid,))
    return rows[0]["text"] if rows else None


def _mem_exists(core, mid):
    return bool(core.q("SELECT id FROM memory WHERE id=%s", (mid,)))


def _proposal_status(core, rid):
    rows = core.q("SELECT status FROM rule_proposal WHERE id=%s", (rid,))
    return rows[0]["status"] if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("unit", "http", "all"), default="all")
    args = ap.parse_args()
    if args.mode in ("unit", "all"):
        run_unit()
    if args.mode == "http":
        run_http()
    elif args.mode == "all":
        env = dict(os.environ)
        rc = subprocess.run([sys.executable, os.path.abspath(__file__), "--mode", "http"],
                            env=env).returncode
        if rc != 0:
            FAILS.append(("http 子进程", "exit=%d" % rc))
    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
