#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase1 抽取钩子验证：MEMORY_EXTRACT=1 时只产 pending 候选，不改 active / rules（fake LLM，零费用）。

    cd /home/baize/jl-service
    python3 tools/test-memory-extract-fake.py

做法：monkeypatch bffpkg.memcandidate.llm 返回固定 JSON（含 user / project_user / project 三种 scope
+ 一条非法 scope + 一条过短文本），再调 extract_memory_candidates()。断言：
  · 合法候选只写 memory_candidate，status 全 pending；
  · project 候选也绝不直接 active；
  · memory 表行数不变、项目 records/rules.md 不变；
  · 第二次抽取同内容不重复入队（与已有 pending 相似）；
  · flag 关掉时不调用模型、不产候选。
测试项目 test-sandbox / 用户 99999，结束清理候选/审计，不污染正式项目。
"""
import json
import os
import sys
import traceback

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
USER = os.environ.get("JL_TEST_USER") or "99999"
PREFIX = "【抽取自测】"

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
    return core, biz, mc


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


FAKE_JSON = json.dumps({"candidates": [
    {"scope": "user", "text": PREFIX + "依据必须写到条号", "evidence": "用户：以后依据写条号"},
    {"scope": "project_user", "text": PREFIX + "本项目通知单抬头固定写广东监理",
     "evidence": "用户：本项目都这么写"},
    {"scope": "project", "text": PREFIX + "整改回复必须附前后对比照片",
     "evidence": "用户：整改回复要附照片"},
    {"scope": "bad_scope", "text": PREFIX + "不该要的候选", "evidence": "x"},
    {"scope": "user", "text": "太短", "evidence": "x"},
]}, ensure_ascii=False)

CALLS = {"n": 0}


def main():
    core, biz, mc = load()
    pid, uid = PROJECT, USER
    rules_path = os.path.join(core.WS_ROOT, pid, "records", "rules.md")
    print("=== 抽取钩子验证（fake LLM）project=%s user=%s ===" % (pid, uid))

    def cleanup():
        core.exec_sql("DELETE FROM memory_candidate WHERE text LIKE %s", ("%" + PREFIX + "%",))
        core.exec_sql("DELETE FROM audit_log WHERE action=%s AND target_type=%s",
                      ("memory_candidate.extract", "candidate"))
        try:
            mc.render_rules_md(pid)
        except Exception:
            traceback.print_exc()

    rules_before = read(rules_path)
    memory_before = int(mc.q("SELECT COUNT(*) n FROM memory")[0]["n"])
    candidate_before = int(mc.q("SELECT COUNT(*) n FROM memory_candidate")[0]["n"])
    old_llm = mc.llm
    cleanup()
    try:
        # ---- flag 关闭：不调模型、不产候选 ----
        os.environ.pop("MEMORY_EXTRACT", None)
        CALLS["n"] = 0
        mc.llm = lambda *a, **k: (CALLS.__setitem__("n", CALLS["n"] + 1) or FAKE_JSON)
        got = mc.extract_memory_candidates(pid, uid, "用户：以后依据写条号", "助手：好的")
        check("flag 关闭不调用模型", CALLS["n"] == 0)
        check("flag 关闭不产候选", got == [])

        # ---- flag 打开：只写 pending ----
        os.environ["MEMORY_EXTRACT"] = "1"
        CALLS["n"] = 0
        got = mc.extract_memory_candidates(
            pid, uid, "用户：以后整改回复要附前后对比照片，依据要写条号", "助手：明白了",
            trace=[{"tool": "create_doc_draft"}])
        check("flag 打开调用了一次模型", CALLS["n"] == 1)
        check("合法候选 3 条（非法 scope/过短被过滤）", len(got) == 3,
              repr(got)[:300])
        check("三种 scope 都被抽到", sorted(x["scope"] for x in got)
              == ["project", "project_user", "user"], repr(got))
        ids = [x["id"] for x in got]
        rows = []
        for cid in ids:
            rows += mc.q("SELECT id, scope, status, text FROM memory_candidate WHERE id=%s",
                         (cid,))
        check("落库候选全部 pending", rows and all(r["status"] == "pending" for r in rows),
              repr(rows))
        check("project 候选也只是 pending（不 active）",
              any(r["scope"] == "project" and r["status"] == "pending" for r in rows))
        memory_after = int(mc.q("SELECT COUNT(*) n FROM memory")[0]["n"])
        check("memory 表行数不变（候选不当事实）", memory_after == memory_before,
              "%s -> %s" % (memory_before, memory_after))
        check("records/rules.md 不变", read(rules_path) == rules_before)

        # ---- 第二次：同样的模型输出不应重复入队 ----
        got2 = mc.extract_memory_candidates(pid, uid, "用户：再说一次", "助手：好的")
        check("重复抽取不重复入队", len(got2) == 0, repr(got2)[:200])
        candidate_after = int(mc.q("SELECT COUNT(*) n FROM memory_candidate")[0]["n"])
        check("候选数净增 = 3", candidate_after - candidate_before == 3,
              "%s -> %s" % (candidate_before, candidate_after))
    finally:
        mc.llm = old_llm
        os.environ.pop("MEMORY_EXTRACT", None)
        cleanup()
        memory_end = int(mc.q("SELECT COUNT(*) n FROM memory")[0]["n"])
        check("测试后 memory 行数恢复", memory_end == memory_before)
        check("测试后 rules.md 恢复基线", read(rules_path) == rules_before)
        left = mc.q("SELECT COUNT(*) n FROM memory_candidate WHERE text LIKE %s",
                    ("%" + PREFIX + "%",))[0]["n"]
        check("测试候选已清理", int(left) == 0)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
