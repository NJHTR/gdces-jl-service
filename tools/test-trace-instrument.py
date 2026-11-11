#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task-17 埋点/状态/重试/取消/投影回归。

    cd /home/baize/jl-service
    python3 tools/test-trace-instrument.py

只动临时 task（client_id 前缀 trace-instr-），测试后清事件/文件/任务/消息。
"""
import json
import os
import sys
import time
import traceback

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
UID = int(os.environ.get("JL_TEST_USER", "99999"))
PREFIX = "trace-instr-%d" % int(time.time() * 1000)
PASSES, FAILS = [], []


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
    import bffpkg.agent as agent
    import bffpkg.trace as tm
    return core, agent, tm


def new_task(core, label, text="埋点回归"):
    cid = "%s-%s-%d" % (PREFIX, label, int(time.time() * 1000) % 10_000_000)
    tid = core.exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status, "
        "trace_version) VALUES (%s,%s,%s,'chat',%s,'queued',2)",
        (PROJECT, UID, cid, json.dumps({"text": text}, ensure_ascii=False)))
    return tid


def main():
    core, agent, tm = load()
    print("=== task-17 埋点回归 project=%s prefix=%s ===" % (PROJECT, PREFIX))
    tm.ensure_trace_schema(force=True)
    task_ids = []
    try:
        # ---- 1) 工具名 → kind/phase 映射 ----
        eq("search_photos→image_read", agent._tool_event_def("search_photos")[0], "image_read")
        eq("create_doc_draft→artifact", agent._tool_event_def("create_doc_draft")[0], "artifact")
        eq("validate_doc→validation", agent._tool_event_def("validate_doc")[0], "validation")
        eq("caption_photo→file_write", agent._tool_event_def("caption_photo")[0], "file_write")
        eq("shell cat→file_read",
           agent._tool_from_cmd("cat records/rules.md")[2], "file_read")
        eq("shell python 绘图→image_read",
           agent._tool_from_cmd("python3 draw.py")[2], "image_read")
        eq("shell 重定向→file_write",
           agent._tool_from_cmd("echo x > out.txt")[2], "file_write")
        eq("普通 shell→tool", agent._tool_from_cmd("ls -la")[2], None)

        # ---- 2) 10 个失败命令全部可见（append-only 完成事件 + error）----
        t1 = new_task(core, "fail10")
        task_ids.append(t1)
        eq("start_run", tm.start_run(t1), True)
        tm.append_event(t1, "plan", "任务理解", phase="plan", status="succeeded",
                        started=True, finished=True, summary="跑 10 个失败命令")
        comp_seqs = []
        for i in range(10):
            s = tm.append_event(t1, "tool", "失败命令%d" % i, phase="act",
                                status="running", started=True,
                                step_id="fail-%d" % i,
                                meta={"tool": "search_standards", "label": "失败命令%d" % i})
            c = tm.finish_event(t1, s, "failed",
                                output_text="stderr line %d" % i,
                                error="exit code %d" % (i + 1))
            comp_seqs.append(c)
        evs = tm.events_after(t1, 0)
        fails = [e for e in evs if e["status"] == "failed"]
        eq("10 个失败完成事件都可见", len(fails), 10)
        check("失败事件都带 error 详情",
              all(e.get("error") for e in fails), repr([e.get("error") for e in fails[:3]]))
        check("失败完成事件 parentSeq 指起始",
              all(e.get("parentSeq") for e in fails), repr([e.get("parentSeq") for e in fails[:3]]))
        check("失败完成事件 stepId 唯一（不串线）",
              len({e.get("stepId") for e in fails}) == 10,
              repr([e.get("stepId") for e in fails]))
        tr = tm.project_message_trace(t1)
        eq("旧投影：10 条失败命令", sum(1 for x in (tr or []) if not x.get("ok")), 10)
        tm.finish_run(t1, "failed", summary="10 个命令失败", error="失败")
        eq("turn failed → run failed", tm.run_view(t1)["status"], "failed")

        # ---- 3) 重试树：attempt/retryOfSeq ----
        t2 = new_task(core, "retry")
        task_ids.append(t2)
        tm.start_run(t2)
        s0 = tm.append_event(t2, "tool", "第一次", phase="act", status="running",
                             started=True, attempt=1, step_id="r-1")
        c0 = tm.finish_event(t2, s0, "failed", error="boom")
        s1 = tm.append_event(t2, "tool", "重试·第一次", phase="act", status="retrying",
                             started=True, attempt=2, retry_of_seq=c0, step_id="r-2")
        c1 = tm.finish_event(t2, s1, "succeeded", output_text="ok")
        evs = tm.events_after(t2, 0)
        retry_ev = [e for e in evs if e["seq"] == s1][0]
        eq("重试 attempt=2", retry_ev["attempt"], 2)
        eq("重试 retryOfSeq=上一次完成 seq", retry_ev["retryOfSeq"], c0)
        eq("旧 attempt 保留", [e["seq"] for e in evs if e["seq"] == c0], [c0])
        eq("旧 attempt 完成态仍 failed",
           [e for e in evs if e["seq"] == c0][0]["status"], "failed")
        check("完成事件树 parentSeq", any(e["seq"] == c1 and e["parentSeq"] == s1 for e in evs))

        # ---- 4) 子智能体 parentSeq / 单行摘要 ----
        t3 = new_task(core, "subagent")
        task_ids.append(t3)
        tm.start_run(t3)
        s_tool = tm.append_event(t3, "tool", "查规范", phase="gather", status="running",
                                 started=True, step_id="st-1")
        c_tool = tm.finish_event(t3, s_tool, "succeeded", output_text="规范结果")
        s_sub = tm.append_event(t3, "subagent", "派子 agent", phase="act",
                                status="succeeded", started=True, finished=True,
                                parent_seq=c_tool, summary="子 agent 复核通过")
        sub = [e for e in tm.events_after(t3, 0) if e["seq"] == s_sub][0]
        eq("子智能体 parentSeq 指父 step", sub["parentSeq"], c_tool)
        eq("子智能体 kind", sub["kind"], "subagent")
        check("子智能体单行摘要", bool(sub.get("summary")), repr(sub.get("summary")))
        # 回报事件：与派发同 parentSeq → L1 stepCount 合并计 1（不按两条算）
        tm.append_event(t3, "subagent", "子 agent 回报", phase="act",
                        status="succeeded", started=True, finished=True,
                        parent_seq=c_tool, summary="复核通过")
        eq("stepCount 把 subagent 派发/回报合并计 1", tm.step_count(t3), 2)

        # ---- 5) 取消：CAS + 迟到 done 不覆盖 + 未完成 step 收尾 ----
        t4 = new_task(core, "cancel")
        task_ids.append(t4)
        tm.start_run(t4)
        run_seq = tm.append_event(t4, "tool", "长命令", phase="act", status="running",
                                  started=True, step_id="cx-1")
        eq("cancel_run", tm.cancel_run(t4, reason="测试取消"), True)
        eq("取消后 DB cancelled",
           core.q("SELECT status FROM task WHERE id=%s", (t4,))[0]["status"], "cancelled")
        eq("取消后不能 finish_run", tm.finish_run(t4, "succeeded"), False)
        late = core.exec_rowcount(
            "UPDATE task SET status='done' WHERE id=%s AND status='running'", (t4,))
        eq("迟到 done CAS 命中 0", late, 0)
        agent._finish_running_steps(t4, "cancelled", "用户取消")
        cx = [e for e in tm.events_after(t4, 0)
              if e.get("parentSeq") == run_seq and e["status"] == "cancelled"]
        eq("未完成 step 被追加 cancelled 完成事件", len(cx), 1)
        # 已取消任务 run_turn 不再开跑（不产生消息）
        n_msg = core.q("SELECT COUNT(*) n FROM message WHERE task_id=%s", (t4,))[0]["n"]
        agent.run_turn(t4, PROJECT, UID, "取消任务不应再跑")
        eq("取消任务 run_turn 不开跑", core.q("SELECT COUNT(*) n FROM message WHERE task_id=%s",
                                              (t4,))[0]["n"], n_msg)

        # ---- 6) 产物 / 长输出 / 截断 / detail ----
        t5 = new_task(core, "artifact")
        task_ids.append(t5)
        tm.start_run(t5)
        big = ("x" * 120 + "\n") * 120
        s5 = tm.append_event(t5, "artifact", "生成产物", phase="finish", status="running",
                             started=True, step_id="art-1")
        c5 = tm.finish_event(t5, s5, "succeeded", output_text=big,
                             meta_extra={"artifacts": [
                                 {"type": "doc", "docId": 1, "name": "监理通知单"},
                                 {"type": "image", "assetId": "01J00000000000000000000000",
                                  "name": "现场图"}]})
        ev5 = [e for e in tm.events_after(t5, 0) if e["seq"] == c5][0]
        eq("完成事件 truncated", ev5["truncated"], True)
        check("完成事件 artifacts", len(ev5.get("artifacts") or []) == 2,
              repr(ev5.get("artifacts")))
        det = tm.event_detail(t5, c5)
        eq("detail artifacts", len(det.get("artifacts") or []), 2)
        check("detail 输出完整", len(det.get("output") or "") >= len(big) - 50,
              (len(det.get("output") or ""), len(big)))
        code, payload = agent.handle_task_event_detail(t5, c5, None)
        eq("detail 接口 200", code, 200)
        # TTL：把 created_at 改老 → 410
        core.exec_sql("UPDATE task_event SET created_at=DATE_SUB(NOW(), INTERVAL 90 DAY) "
                      "WHERE task_id=%s", (t5,))
        code, payload = agent.handle_task_event_detail(t5, c5, None)
        eq("超期 detail 410", code, 410)
        core.exec_sql("UPDATE task_event SET created_at=NOW() WHERE task_id=%s", (t5,))

        # ---- 7) 实时/历史一致 + 旧客户端不变 ----
        t6 = new_task(core, "consistency")
        task_ids.append(t6)
        tm.start_run(t6)
        for i in range(4):
            s = tm.append_event(t6, "tool", "步骤%d" % i, phase="act", status="running",
                                started=True, step_id="c-%d" % i,
                                meta={"tool": "search_standards", "label": "步骤%d" % i})
            tm.finish_event(t6, s, "succeeded", output_text="结果%d" % i)
        core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, task_id, sync) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,1)",
            (PROJECT, UID, PREFIX + "-msg-" + str(t6), "一致性回答",
             json.dumps(tm.project_message_trace(t6), ensure_ascii=False), t6))
        code, payload = agent.handle_task(t6, None, 0)
        rt = payload.get("data") or {}
        code2, payload2 = agent.handle_task_trace(t6, {"after_seq": "0"}, None)
        full = payload2.get("data") or {}
        eq("实时 events == 历史全量", [e["seq"] for e in rt.get("events") or []],
           [e["seq"] for e in full.get("events") or []])
        a_msgs = [m for m in rt.get("messages") or [] if m.get("role") == "assistant"]
        eq("历史 message.events 一致",
           [e["seq"] for e in (a_msgs[0].get("events") or [])],
           [e["seq"] for e in full.get("events") or []])
        check("旧客户端 message.trace 仍是旧数组",
              isinstance(a_msgs[0].get("trace"), list)
              and all(("tool" in x) for x in (a_msgs[0].get("trace") or [])),
              repr(a_msgs[0].get("trace"))[:200])
        eq("旧客户端顶层 status 仍是 done/failed 旧值", rt.get("status"), "running")
        eq("run.status 投影 running", (rt.get("run") or {}).get("status"), "running")

        # ---- 8) 删除任务清理 ----
        t7 = new_task(core, "purge")
        task_ids.append(t7)
        tm.start_run(t7)
        s7 = tm.append_event(t7, "tool", "待删", phase="act", status="succeeded",
                             output_text="bye")
        ref = [e for e in tm.events_after(t7, 0) if e["seq"] == s7][0].get("outputRef")
        tm.purge_task(t7)
        eq("purge 后无事件", tm.events_after(t7, 0), [])
        check("purge 后 gz 删除",
              not (ref and os.path.exists(
                  os.path.join(os.path.dirname(os.path.dirname(tm.TRACE_ROOT)), ref))))

        # ---- 9) B2：未知 exitCode 不得算成功（_parse_cli_jsonl 旧 trace）----
        def parse_items(*items):
            lines = "\n".join(json.dumps(
                {"type": "item.completed", "item": it}, ensure_ascii=False) for it in items)
            return agent._parse_cli_jsonl(lines, ctx={})[2]

        tl = parse_items({"type": "command_execution", "id": "u1",
                          "command": "jl-tool search_standards '{}'",
                          "status": "failed", "exitCode": None,
                          "aggregatedOutput": ""})
        eq("未知 exitCode+failed → ok=false", tl[0].get("ok"), False)
        check("未知 exitCode 文本标失败", "失败" in (tl[0].get("text") or ""),
              repr(tl[0].get("text")))
        tl = parse_items({"type": "command_execution", "id": "u2",
                          "command": "jl-tool search_standards '{}'",
                          "status": "completed", "exitCode": None,
                          "aggregatedOutput": "out"})
        eq("exitCode 缺失+completed → ok=false（unknown）", tl[0].get("ok"), False)
        tl = parse_items({"type": "command_execution", "id": "u3",
                          "command": "jl-tool search_standards '{}'",
                          "status": "completed", "exitCode": 0,
                          "aggregatedOutput": "ok"})
        eq("exitCode=0 → ok=true", tl[0].get("ok"), True)
        tl = parse_items({"type": "command_execution", "id": "u4",
                          "command": "jl-tool search_standards '{}'",
                          "status": "failed", "exitCode": 2,
                          "aggregatedOutput": "err"})
        eq("exitCode=2 → ok=false", tl[0].get("ok"), False)

        # ---- 10) B1：bootstrap 与 /messages 的 v2 字段同源 ----
        t8 = new_task(core, "bootstrap")
        task_ids.append(t8)
        tm.start_run(t8)
        for i in range(3):
            s = tm.append_event(t8, "tool", "boot 步骤%d" % i, phase="act",
                                status="running", step_id="b-%d" % i,
                                meta={"tool": "search_standards", "label": "boot 步骤%d" % i})
            tm.finish_event(t8, s, "succeeded", output_text="结果%d" % i)
        mid = core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, "
            "trace_sum, ms, task_id, sync, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,100,%s,1,%s)",
            (PROJECT, UID, PREFIX + "-boot-msg", "bootstrap 投影回答",
             json.dumps(tm.project_message_trace(t8), ensure_ascii=False), "调了 查规范",
             t8, json.dumps({"text": "bootstrap 投影回答"}, ensure_ascii=False)))
        # 预热线程会连引擎；单测里替换掉，避免测试期间额外起会话
        _orig_client = getattr(agent, "_client", None)
        agent._client = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("skip warm in test"))
        try:
            code, bpayload = agent.handle_bootstrap({"projectId": PROJECT}, None)
        finally:
            if _orig_client is not None:
                agent._client = _orig_client
        bdata = bpayload.get("data") or {}
        bmsg = [m for m in (bdata.get("msgs") or [])
                if m.get("type") == "agent" and m.get("id") == "a%d" % mid]
        check("bootstrap 返回 assistant 消息", bool(bmsg), repr(bdata.get("msgs"))[:200])
        if bmsg:
            bm = bmsg[0]
            eq("bootstrap 消息 traceVersion=2", bm.get("traceVersion"), 2)
            eq("bootstrap 消息 taskId", bm.get("taskId"), t8)
            eq("bootstrap 消息 events 数", len(bm.get("events") or []), tm.last_seq(t8))
            eq("bootstrap 消息 lastSeq", bm.get("lastSeq"), tm.last_seq(t8))
            eq("bootstrap 消息 run.status", (bm.get("run") or {}).get("status"), "running")
            eq("bootstrap 顶层 traceVersion=2", bdata.get("traceVersion"), 2)
            eq("bootstrap 顶层 lastSeq", bdata.get("lastSeq"), tm.last_seq(t8))
            check("bootstrap 顶层带 run", bool(bdata.get("run")))
            code2, mpayload = agent.handle_messages(
                {"projectId": PROJECT, "after": mid - 1})
            mm = [m for m in (mpayload.get("data") or {}).get("list") or []
                  if m.get("id") == mid]
            check("bootstrap 与 /messages 事件一致",
                  bool(mm) and [e["seq"] for e in (mm[0].get("events") or [])]
                  == [e["seq"] for e in (bm.get("events") or [])],
                  repr([e["seq"] for e in (bm.get("events") or [])]))
            eq("bootstrap 旧 message.trace 保留",
               isinstance(bm.get("trace"), list) and len(bm.get("trace")) > 0, True)

    except Exception:
        traceback.print_exc()
        check("测试脚本无异常", False, "见 traceback")
    finally:
        for tid in task_ids:
            try:
                tm.purge_task(tid)
            except Exception:
                traceback.print_exc()
        try:
            core.exec_sql("DELETE FROM message WHERE client_id LIKE %s", (PREFIX + "%",))
            core.exec_sql("DELETE FROM task WHERE client_id LIKE %s", (PREFIX + "%",))
            # bootstrap 预热会给测试用户写个人摘要（仅 99999），一并清
            import shutil
            shutil.rmtree(os.path.join(ROOT, "data", "memory", "users", str(UID)),
                          ignore_errors=True)
        except Exception:
            traceback.print_exc()
        left = core.q("SELECT COUNT(*) n FROM task WHERE client_id LIKE %s", (PREFIX + "%",))
        eq("测试数据已清理", int(left[0]["n"]), 0)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
