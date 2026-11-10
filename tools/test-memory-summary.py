#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase2 回归测试：个人域摘要 + 项目域只重渲染（服务器上跑）。

    cd /home/baize/jl-service
    python3 tools/test-memory-summary.py

覆盖（对应 task-3 验收）：
  1. 个人域摘要只来自 active：pending 候选不出现、不进文件
  2. 去重/归并：完全相同/包含关系合并并标注条数
  3. 冲突不静默覆盖：相近但不同的说法并列保留在"相近但未合并"
  4. 幂等：重复渲染内容不变；pending 不影响 summary/rules
  5. 接受候选后自动刷新 summary
  6. 项目域只读 active project memory：停用/候选不出现，个人记忆不升格为项目
  7. AGENTS.md 只放指针（memory_summary.md 路径），不整份注入摘要
  8. 不覆盖既有个人文件拆分（MEMORY.md / projects/<uid>/<pid>.md 仍各自存在）

测试数据用 test-sandbox + 测试用户 99999，结束时全部清理并恢复 rules.md。
"""
import hashlib
import os
import shutil
import sys
import traceback

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
USER = os.environ.get("JL_TEST_USER") or "99999"
PREFIX = "【P2自测】"

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("  PASS  %s" % name)
    else:
        FAILS.append((name, detail))
        print("  FAIL  %s  %s" % (name, detail))


def load():
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.core as core
    import bffpkg.biz as biz
    import bffpkg.memcandidate as mc
    import bffpkg.memorysum as ms
    return core, biz, mc, ms


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def main():
    core, biz, mc, ms = load()
    pid, uid = PROJECT, USER
    summary_path = ms.user_summary_path(uid)
    rules_path = os.path.join(core.WS_ROOT, pid, "records", "rules.md")
    print("=== Phase2 测试 project=%s user=%s ===" % (pid, uid))

    def cleanup():
        core.exec_sql("DELETE FROM memory WHERE text LIKE %s", ("%" + PREFIX + "%",))
        core.exec_sql("DELETE FROM memory_candidate WHERE text LIKE %s", ("%" + PREFIX + "%",))
        try:
            ms.render_user_summary(uid)
            ms.render_project_rules(pid)
        except Exception:
            traceback.print_exc()
        if str(uid).startswith("9999"):
            shutil.rmtree(os.path.join(core.ROOT, "data", "memory", "users", str(uid)),
                          ignore_errors=True)
            shutil.rmtree(os.path.join(core.ROOT, "data", "memory", "projects", str(uid)),
                          ignore_errors=True)
            shutil.rmtree(os.path.join(core.HOMES_ROOT, str(uid)), ignore_errors=True)

    def add_personal(text, project_id=None):
        applies = ("user:%s@project:%s" % (uid, project_id)) if project_id else ("user:%s" % uid)
        return core.exec_sql(
            "INSERT INTO memory (scope, owner_id, project_id, text, source, applies_to, updated_at) "
            "VALUES ('user',%s,%s,%s,'manual',%s,NOW(3))",
            (uid, project_id, text, applies))

    def add_project(text, status=1, source="confirmed"):
        return core.exec_sql(
            "INSERT INTO memory (scope, owner_id, project_id, text, source, applies_to, status, updated_at) "
            "VALUES ('project',%s,NULL,%s,%s,%s,%s,NOW(3))",
            (pid, text, source, "project:%s" % pid, status))

    cleanup()
    rules_baseline = read(rules_path)
    try:
        # ---- 造 active 个人记忆：重复 / 包含 / 相近 ----
        add_personal(PREFIX + "依据必须写到条号")
        add_personal(PREFIX + "依据必须写到条号。")            # 归一化后完全重复
        add_personal("结论写经检查符合要求" + PREFIX)
        add_personal("监理日志结论写经检查符合要求" + PREFIX)   # 包含关系 → 并到更长
        add_personal(PREFIX + "通知单抬头写广东监理")
        add_personal(PREFIX + "通知单抬头写广东建设监理")       # 相近但不同 → 并列待确认
        add_personal(PREFIX + "本项目编号写楼栋", project_id=pid)

        # ---- 1) 摘要只来自 active ----
        check("候选表无本测试 pending 时摘要可生成", ms.render_user_summary(uid) is not None)
        text = read(summary_path)
        check("摘要文件已生成", bool(text), summary_path)
        check("摘要不含候选（pending 不存在于摘要）", "不应出现" not in text)
        check("个人域摘要标题/说明", "我的记忆摘要" in text and "候选（pending）不计入" in text)

        # ---- 2) 去重/归并 ----
        check("完全重复合并为一条", text.count(PREFIX + "依据必须写到条号") == 1,
              "count=%d" % text.count(PREFIX + "依据必须写到条号"))
        check("合并标注条数", "（同义合并 2 条）" in text)
        check("包含关系并到更完整说法",
              "- 监理日志结论写经检查符合要求" + PREFIX + "（同义合并 2 条）" in text)

        # ---- 3) 冲突不静默覆盖 ----
        conflict_ok = False
        if "相近但未合并" in text:
            tail = text.split("相近但未合并", 1)[1]
            conflict_ok = ("广东监理" in tail) and ("广东建设监理" in tail)
        check("相近不同说法并列保留（不静默合并）", conflict_ok)

        # ---- 4) 幂等 ----
        h1 = sha1(read(summary_path))
        ms.render_user_summary(uid)
        h2 = sha1(read(summary_path))
        check("个人摘要重复渲染幂等", h1 == h2)

        # pending 项目候选不影响 summary / rules
        code, payload = mc.handle_candidate_create(
            {"projectId": pid, "userId": uid, "scope": "project",
             "text": PREFIX + "pending 项目规矩不应出现"})
        cid = (payload.get("data") or {}).get("id")
        h_sum = sha1(read(summary_path))
        ms.render_user_summary(uid)
        check("pending 候选不影响 summary", sha1(read(summary_path)) == h_sum)
        ms.render_project_rules(pid)
        check("pending 候选不影响 rules.md", read(rules_path) == rules_baseline)

        # ---- 5) 接受后刷新 ----
        text_accept = PREFIX + "accept 后摘要要刷新"
        code, payload = mc.handle_candidate_create(
            {"projectId": pid, "userId": uid, "scope": "user", "text": text_accept})
        cand_id = (payload.get("data") or {}).get("id")
        code, payload = mc.handle_candidate_decide(cand_id, "accept", None)
        check("accept 个人候选 200", code == 200, repr(payload)[:200])
        check("accept 返回 summary 路径", bool((payload.get("data") or {}).get("summary")),
              repr(payload)[:200])
        check("接受后摘要已刷新", text_accept in read(summary_path))

        # ---- 6) 项目域：只读 active project memory ----
        add_project(PREFIX + "项目规矩-生效")
        add_project(PREFIX + "项目规矩-停用", status=0)
        ms.render_project_rules(pid)
        rules = read(rules_path)
        check("生效项目规矩进 rules.md", PREFIX + "项目规矩-生效" in rules)
        check("停用项目规矩不进 rules.md", PREFIX + "项目规矩-停用" not in rules)
        check("个人记忆不升格为项目规矩", text_accept not in rules
              and PREFIX + "依据必须写到条号" not in rules)
        check("候选不升格为项目规矩", PREFIX + "pending 项目规矩不应出现" not in rules)

        # ---- 7) AGENTS.md 只放指针 ----
        import bffpkg.agent as agent
        ctx = agent.build_context(pid, uid)
        check("AGENTS 上下文含 memory_summary 指针", "memory_summary.md" in ctx)
        check("AGENTS 不注入摘要正文", text_accept not in ctx)

        # ---- 8) 不覆盖既有个人文件拆分 ----
        import bffpkg.biz as _biz
        _biz.render_user_memory(uid)
        _biz.render_user_project_md(uid, pid)
        cross = read(os.path.join(core.ROOT, "data", "memory", "users", str(uid), "MEMORY.md"))
        per = read(os.path.join(core.ROOT, "data", "memory", "projects", str(uid),
                                pid + ".md"))
        check("跨项目 MEMORY.md 仍独立存在", "跨项目" in cross)
        check("个人×项目文件仍独立存在（不合并进摘要）", "项目记忆" in per)
        check("摘要文件与既有拆分并存", os.path.isfile(summary_path))
    finally:
        cleanup()
        # rules.md 恢复到测试前
        after = read(rules_path)
        check("项目 rules.md 恢复基线", after == rules_baseline,
              "before=%d after=%d" % (len(rules_baseline), len(after)))
        print("  (已清理测试数据)")

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
