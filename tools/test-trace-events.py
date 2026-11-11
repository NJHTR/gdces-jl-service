#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终态 trace 后端回归：事件账本 / sequencer / 状态机 / 增量 API / 大输出 / 旧投影。

    cd /home/baize/jl-service
    python3 tools/test-trace-events.py

只动临时 task（client_id 前缀 trace-test-），测试后清事件/文件/任务/消息。
"""
import json
import os
import shutil
import sys
import threading
import time
import traceback

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
UID = int(os.environ.get("JL_TEST_USER", "99999"))
PREFIX = "trace-test-%d" % int(time.time() * 1000)
FAILS, PASSES = [], []


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


def new_task(core, client=None, payload=None):
    cid = client or "%s-%d" % (PREFIX, int(time.time() * 1000) % 10_000_000)
    tid = core.exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status, "
        "trace_version) VALUES (%s,%s,%s,'chat',%s,'queued',2)",
        (PROJECT, UID, cid, json.dumps(payload or {"text": "trace test"})))
    return tid


def main():
    core, agent, tm = load()
    trace_base = os.path.dirname(os.path.dirname(tm.TRACE_ROOT))   # core.ROOT（部署时=仓库根）
    print("=== trace 基座回归 project=%s prefix=%s ===" % (PROJECT, PREFIX))
    check("ensure_trace_schema 幂等建表/补列", tm.ensure_trace_schema(force=True))
    task_ids = []
    try:
        check("task_event 表已建", bool(core.q("SHOW TABLES LIKE 'task_event'")))
        cols = {r["Field"] for r in core.q("SHOW COLUMNS FROM task")}
        check("task 有 event_seq/trace_version", {"event_seq", "trace_version"} <= cols,
              repr(sorted(cols)))

        # ---- 1) start_run + seq 单调无空洞 ----
        t1 = new_task(core)
        task_ids.append(t1)
        eq("start_run queued→running", tm.start_run(t1), True)
        eq("重复 start_run 同一 run 仍允许(运行中)", tm.start_run(t1), True)
        for i in range(5):
            tm.append_event(t1, "tool", "步骤%d" % i, phase="act", status="succeeded",
                            summary="ok%d" % i)
        evs = tm.events_after(t1, 0)
        seqs = [e["seq"] for e in evs]
        eq("seq 从 1 连续无空洞", seqs, list(range(1, len(evs) + 1)))
        eq("last_seq = 最大 seq", tm.last_seq(t1), max(seqs))

        # ---- 2) 并发 append 不串线 ----
        t2 = new_task(core)
        task_ids.append(t2)
        tm.start_run(t2)
        errors = []

        def worker(k):
            try:
                for i in range(10):
                    tm.append_event(t2, "tool", "并发-%d-%d" % (k, i), phase="act",
                                    status="succeeded")
            except Exception as e:
                errors.append(e)

        ths = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        [th.start() for th in ths]
        [th.join() for th in ths]
        seqs2 = [e["seq"] for e in tm.events_after(t2, 0)]
        check("并发写无异常", not errors, repr(errors[:2]))
        eq("并发写 seq 1..N 连续且唯一", seqs2, list(range(1, len(seqs2) + 1)))
        eq("并发写条数=1(start)+80", len(seqs2), 81)

        # ---- 3) step 状态机：running→终态一次；终态不可回写 ----
        t3 = new_task(core)
        task_ids.append(t3)
        tm.start_run(t3)
        s = tm.append_event(t3, "tool", "长任务", phase="act", status="running",
                            step_id="step-t3", started=True, output_text="x" * 100)
        n_start = len(tm.events_after(t3, 0))
        c = tm.finish_event(t3, s, "succeeded", output_text="done")
        check("完成是追加新事件（new seq>起始 seq）", isinstance(c, int) and c > s, c)
        c_ev = [e for e in tm.events_after(t3, 0) if e["seq"] == c][0]
        eq("完成事件 parentSeq=起始 seq", c_ev["parentSeq"], s)
        eq("完成事件同 stepId", c_ev["stepId"], "step-t3")
        start_ev = [e for e in tm.events_after(t3, 0) if e["seq"] == s][0]
        eq("起始事件仍 running（append-only，不原地改）", start_ev["status"], "running")
        eq("重复 finish 幂等返回同一完成 seq", tm.finish_event(t3, s, "failed"), c)
        eq("幂等不新增事件", len(tm.events_after(t3, 0)), n_start + 1)
        eq("已终态事件不能再 finish", tm.finish_event(t3, 1, "succeeded"), None)

        # ---- 4) run 状态机：succeeded/cancelled/failed 终态 ----
        ok = tm.finish_run(t3, "succeeded", summary="完成")
        eq("finish_run succeeded", ok, True)
        eq("run_view 映射旧 done→succeeded", tm.run_view(t3)["status"], "succeeded")
        eq("旧 task.status 仍是 done（旧客户端不变）",
           core.q("SELECT status FROM task WHERE id=%s", (t3,))[0]["status"], "done")
        eq("终态 run 不可再次 finish", tm.finish_run(t3, "failed"), False)

        t4 = new_task(core)
        task_ids.append(t4)
        tm.start_run(t4)
        eq("cancel_run running→cancelled", tm.cancel_run(t4, reason="测试取消"), True)
        eq("取消后 DB=cancelled",
           core.q("SELECT status FROM task WHERE id=%s", (t4,))[0]["status"], "cancelled")
        eq("取消后不能 finish_run（迟到 done 不覆盖）", tm.finish_run(t4, "succeeded"), False)
        n_late = core.exec_rowcount("UPDATE task SET status='done' WHERE id=%s AND status='running'",
                                    (t4,))
        eq("迟到 done CAS 命中 0 行", n_late, 0)

        t5 = new_task(core)
        task_ids.append(t5)
        tm.start_run(t5)
        s5 = tm.append_event(t5, "error", "工具失败", phase="act", status="running",
                             started=True)
        tm.finish_event(t5, s5, "failed", error="exit code 2", output_text="stderr")
        eq("失败 run → failed", tm.finish_run(t5, "failed", summary="失败",
                                              error="exit code 2"), True)
        eq("run_view failed", tm.run_view(t5)["status"], "failed")
        # 终态独立于最后一条事件：失败后再追加 note，run 仍是 failed
        tm.append_event(t5, "note", "收尾备注", phase="finish", status="succeeded")
        eq("run 终态独立于最后一条事件", tm.run_view(t5)["status"], "failed")

        # ---- 5) 增量 after_seq ----
        t6 = new_task(core)
        task_ids.append(t6)
        tm.start_run(t6)
        for i in range(20):
            tm.append_event(t6, "tool", "增量%d" % i, phase="act", status="succeeded")
        evs_all = tm.events_after(t6, 0)
        evs_inc = tm.events_after(t6, 10)
        eq("after_seq 只返回更大 seq", [e["seq"] for e in evs_inc],
           [e["seq"] for e in evs_all if e["seq"] > 10])
        eq("after_seq 升序", [e["seq"] for e in evs_inc],
           sorted(e["seq"] for e in evs_inc))

        # ---- 6) 大输出 / 截断 / 详情 / 脱敏 ----
        t7 = new_task(core)
        task_ids.append(t7)
        tm.start_run(t7)
        big = ("line-%d\n" % 1) * 900
        big += ("password=MYSQL_SECRET_123 token=abc123def456 phone=13800138000 "
                "path=/tmp/secret-file.png\n")
        s7 = tm.append_event(t7, "tool", "大输出工具", phase="act", status="running",
                             step_id="big-tool", started=True)
        s7c = tm.finish_event(t7, s7, "succeeded", output_text=big, error=None)
        ev = [e for e in tm.events_after(t7, 0) if e["seq"] == s7c][0]
        eq("完成事件 status 终态", ev["status"], "succeeded")
        check("preview <= 4KB+截断标记", len(ev["outputPreview"] or "") <= 4300,
              len(ev["outputPreview"] or ""))
        eq("大输出标记 truncated", ev["truncated"], True)
        check("outputRef 已落盘", bool(ev["outputRef"]), repr(ev["outputRef"]))
        check("outputBytes 有记录", (ev["outputBytes"] or 0) > len(big) - 100,
              repr(ev.get("outputBytes")))
        raw = open(os.path.join(trace_base, ev["outputRef"]), "rb").read()
        check("落盘文件是 gzip", raw[:2] == b"\x1f\x8b")
        detail = tm.event_detail(t7, s7c)
        eq("detail source=file", detail["source"], "file")
        check("detail 输出完整长度（脱敏后仍完整）", len(detail["output"] or "") >= len(big) - 200,
              (len(detail["output"] or ""), len(big)))
        blob = json.dumps(ev, ensure_ascii=False) + (detail["output"] or "")
        check("预览/详情已脱敏（无明文密码/token/手机号/本地路径）",
              "MYSQL_SECRET_123" not in blob and "abc123def456" not in blob
              and "13800138000" not in blob and "/tmp/secret-file.png" not in blob,
              blob[:200])
        check("手机号/密码被替换", "****" in (detail["output"] or "")
              and "已脱敏" in (detail["output"] or ""))

        # 权限：private 详情只有 creator/管理员；这里只校验 visibility 字段落库
        s8 = tm.append_event(t7, "note", "私密备注", phase="finish", status="succeeded",
                             visibility="private")
        eq("visibility 落库 private", tm.events_after(t7, s8 - 1)[0]["visibility"], "private")

        # 产物引用（L3）：artifact 事件 + detail 都带 artifacts，图片短链由后端现签
        s_art = tm.append_event(t7, "artifact", "生成产物", phase="finish", status="succeeded",
                                meta={"artifacts": [
                                    {"type": "doc", "docId": 123, "name": "监理通知单"},
                                    {"type": "image",
                                     "assetId": "01J00000000000000000000000",
                                     "name": "现场标注图"}]})
        art_ev = [e for e in tm.events_after(t7, s_art - 1) if e["seq"] == s_art][0]
        check("事件带 artifacts", isinstance(art_ev.get("artifacts"), list)
              and len(art_ev["artifacts"]) == 2, repr(art_ev.get("artifacts")))
        eq("artifact docId", art_ev["artifacts"][0].get("docId"), "123")
        art_detail = tm.event_detail(t7, s_art)
        check("detail 带 artifacts", len(art_detail.get("artifacts") or []) == 2,
              repr(art_detail.get("artifacts")))

        # ---- 7) 旧投影 ----
        prog = tm.project_progress(t7)
        check("task.progress 投影是数组", isinstance(prog, list) and len(prog) > 0, repr(prog))
        tr = tm.project_message_trace(t7)
        check("message.trace 投影有条目", isinstance(tr, list) and len(tr) > 0, repr(tr)[:200])
        check("旧 trace 条目含 tool/ok", bool(tr[0].get("tool")) and "ok" in tr[0],
              repr(tr[0]))

        # ---- 8) handle_task 兼容 + 增量 + 旧任务回退 ----
        core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, task_id, sync) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,1)",
            (PROJECT, UID, PREFIX + "-m-" + str(t7), "带 trace 的回答",
             json.dumps(tr, ensure_ascii=False), t7))
        code, payload = agent.handle_task(t7, None, 0)
        data = payload.get("data") or {}
        eq("handle_task 200", code, 200)
        eq("handle_task traceVersion=2", data.get("traceVersion"), 2, repr(data.get("traceVersion")))
        check("handle_task 有 run/lastSeq/events",
              bool(data.get("run")) and data.get("lastSeq") == tm.last_seq(t7)
              and len(data.get("events") or []) == data.get("lastSeq"), repr(data.get("events"))[:200])
        check("handle_task 旧 progress 保留", "progress" in data)
        check("handle_task 旧 messages.trace 保留",
              bool((data.get("messages") or [{}])[0].get("trace")))
        code, payload = agent.handle_task(t7, None, 3)
        data2 = payload.get("data") or {}
        check("after_seq 增量只回新事件",
              all(e["seq"] > 3 for e in (data2.get("events") or []))
              and len(data2.get("events") or []) < len(data.get("events") or []),
              len(data2.get("events") or []))
        # 历史消息：message.trace 保持旧数组；v2 走 message.events
        mid = core.q("SELECT id FROM message WHERE task_id=%s ORDER BY id DESC LIMIT 1",
                     (t7,))[0]["id"]
        code, payload = agent.handle_messages({"projectId": PROJECT, "after": mid - 1})
        hist = [m for m in (payload.get("data") or {}).get("list") or [] if m["id"] == mid]
        check("handle_messages 找到历史消息", bool(hist), repr(payload)[:200])
        if hist:
            hm = hist[0]
            eq("历史消息 traceVersion=2", hm.get("traceVersion"), 2)
            check("历史消息带 events", len(hm.get("events") or []) > 0)
            check("历史 message.trace 仍是旧数组",
                  isinstance(hm.get("trace"), list)
                  and (hm["trace"][0].get("tool") if hm["trace"] else False),
                  repr(hm.get("trace"))[:200])
        # 旧任务（列默认 trace_version=2 但无事件）必须回 v1，避免客户端以为 v2 空
        old = new_task(core, client=PREFIX + "-old", payload={"text": "旧任务"})
        task_ids.append(old)
        core.exec_sql("UPDATE task SET status='done', progress=%s WHERE id=%s",
                      (json.dumps([{"label": "旧步骤", "state": "done"}]), old))
        code, payload = agent.handle_task(old, None, 0)
        od = payload.get("data") or {}
        eq("无事件历史任务 traceVersion=1（不是 v2 空）", od.get("traceVersion"), 1)
        eq("旧任务无 events", od.get("events"), [])
        check("旧任务 progress 回退旧值",
              (od.get("progress") or [{}])[0].get("label") == "旧步骤", repr(od.get("progress")))

        # ---- 9) trace 接口 & detail 接口 ----
        code, payload = agent.handle_task_trace(t7, {"after_seq": "0"}, None)
        eq("trace 接口 200", code, 200)
        check("trace 接口全量事件", len((payload.get("data") or {}).get("events") or [])
              == tm.last_seq(t7))
        code, payload = agent.handle_task_event_detail(t7, s7c, None)
        eq("detail 接口 200", code, 200)
        eq("detail 接口 source", (payload.get("data") or {}).get("source"), "file")

        # ---- 10) GC ----
        t8 = new_task(core)
        task_ids.append(t8)
        tm.start_run(t8)
        s9 = tm.append_event(t8, "tool", "过期事件", phase="act", status="succeeded",
                             output_text="old")
        ref = [e for e in tm.events_after(t8, 0) if e["seq"] == s9][0]["outputRef"]
        core.exec_sql("UPDATE task_event SET created_at=DATE_SUB(NOW(), INTERVAL 90 DAY) "
                      "WHERE task_id=%s", (t8,))
        n = tm.gc(30)
        check("GC 清理超期事件", n >= 2, n)
        eq("GC 后事件清空", tm.events_after(t8, 0), [])
        check("GC 后 gz 文件删除", not (ref and os.path.exists(os.path.join(trace_base, ref))))

        # ---- 11) purge ----
        t9 = new_task(core)
        task_ids.append(t9)
        tm.start_run(t9)
        s10 = tm.append_event(t9, "tool", "待删除", phase="act", status="succeeded",
                              output_text="bye")
        ref10 = [e for e in tm.events_after(t9, 0) if e["seq"] == s10][0]["outputRef"]
        tm.purge_task(t9)
        eq("purge 清事件", tm.events_after(t9, 0), [])
        check("purge 清文件", not os.path.exists(os.path.join(trace_base, ref10)))

    except Exception:
        traceback.print_exc()
        check("测试脚本无异常", False, "见 traceback")
    finally:
        # 清理：事件/消息/任务/trace 目录
        for tid in task_ids:
            try:
                tm.purge_task(tid)
            except Exception:
                traceback.print_exc()
        try:
            core.exec_sql("DELETE FROM message WHERE client_id LIKE %s", (PREFIX + "%",))
            core.exec_sql("DELETE FROM task WHERE client_id LIKE %s", (PREFIX + "%",))
        except Exception:
            traceback.print_exc()
        try:
            for tid in task_ids:
                shutil.rmtree(os.path.join(tm.TRACE_ROOT, str(tid)), ignore_errors=True)
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
