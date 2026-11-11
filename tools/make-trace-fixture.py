#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""造 trace 联调样例任务（给 client/QA 的 L1/L2/L3 与折叠测试）。

- 普通样例：30~40 个事件，含五阶段、失败命令（有 error 详情）、重试树
  （attempt/retryOfSeq）、子智能体 parentSeq、长输出截断、图片/文书产物。
- 长样例：120+ 事件，专门测「更早 N 步」折叠。

事件不是手写 JSON 硬灌：工具步骤真的执行 scripts/jl-tool（真实输出/退出码），
图片产物真的调 BFF /api/v1/photos/annotate 生成资产；只是这些步骤由脚本驱动，
不是一次模型 turn（任务 payload 里标了 fixture=true）。数据默认保留。

    cd /home/baize/jl-service && python3 tools/make-trace-fixture.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PROJECT = os.environ.get("JL_FIXTURE_PROJECT") or "clean-demo-01"
UID = int(os.environ.get("JL_TEST_USER", "1"))
PHOTO = int(os.environ.get("JL_FIXTURE_PHOTO", "46"))
PHOTO2 = int(os.environ.get("JL_FIXTURE_PHOTO2", "53"))
JL_TOOL = os.path.join(ROOT, "scripts", "jl-tool")
WS = os.path.join(ROOT, "data", "workspaces", PROJECT)

sys.path.insert(0, os.path.join(ROOT, "bff"))
import bffpkg.core as core          # noqa: E402
import bffpkg.trace as tm           # noqa: E402


def http(method, path, body=None, timeout=120):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def new_fixture_task(label):
    cid = "trace-fixture-%s-%d" % (label, int(time.time() * 1000))
    tid = core.exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status, "
        "trace_version) VALUES (%s,%s,%s,'chat',%s,'queued',2)",
        (PROJECT, UID, cid, json.dumps({"text": "trace 联调样例 %s" % label,
                                        "fixture": True, "label": label},
                                       ensure_ascii=False)))
    tm.start_run(tid)
    return tid, cid


def tool_step(task_id, name, args, *, phase="act", prefix="", fail=False):
    """真跑一次 jl-tool，把真实输出/退出码写进事件。"""
    cmd = [JL_TOOL, name, json.dumps(args, ensure_ascii=False)]
    seq = tm.append_event(task_id, _kind_of(name), (prefix or name) + "·" + name,
                          phase=phase, status="running", started=True,
                          step_id="fx-%d-%d" % (task_id, time.time() * 1000),
                          input_text=json.dumps(args, ensure_ascii=False)[:2000],
                          meta={"tool": name, "label": prefix or name, "icon": "run",
                                "args": json.dumps(args, ensure_ascii=False)[:300],
                                "legacy_kind": "cmd"})
    p = subprocess.run(cmd, cwd=WS, capture_output=True, text=True, timeout=180)
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    ok = (p.returncode == 0) and not fail
    cs = tm.finish_event(task_id, seq, "succeeded" if ok else "failed",
                         output_text=out or "(no output)",
                         error=(None if ok else
                                (p.stderr or p.stdout or "exit code %r" % p.returncode)[:2000]),
                         meta_extra={"secs": 0.2})
    return seq, cs, ok, out


def _kind_of(name):
    if name in ("search_photos", "caption_photo", "annotate_photo", "compare_photos",
                "send_photo"):
        return "image_read"
    if name in ("create_doc_draft", "propose_rule"):
        return "artifact"
    if name in ("validate_doc", "record_review"):
        return "validation"
    return "tool"


def make_short():
    tid, cid = new_fixture_task("short")
    tm.append_event(tid, "plan", "任务理解", phase="plan", status="succeeded",
                    started=True, finished=True,
                    summary="联调样例：查规范/照片 → 标注/对比 → 生成文书")
    tool_step(tid, "get_context", {"projectId": PROJECT}, phase="understand",
              prefix="读上下文")
    tool_step(tid, "search_standards", {"query": "连墙件 间距"}, phase="gather")
    tool_step(tid, "search_standards", {"query": "临边防护 栏杆"}, phase="gather")
    tool_step(tid, "search_photos", {"projectId": PROJECT, "q": "外架"}, phase="gather")
    tool_step(tid, "search_photos", {"projectId": PROJECT, "q": "配电箱"}, phase="gather")

    # 图片产物：真调 BFF 生成派生图资产
    try:
        r = http("POST", "/api/v1/photos/annotate", {
            "projectId": PROJECT, "photoId": PHOTO,
            "boxes": [{"x": 200, "y": 160, "w": 320, "h": 240, "label": "联调框"}],
            "caption": "trace 联调标注图"})
        block = (r.get("data") or {}).get("block") or {}
        s = tm.append_event(tid, "image_read", "生成标注图", phase="act",
                            status="succeeded", started=True, finished=True,
                            summary="标注图已生成",
                            meta={"tool": "annotate_photo", "label": "生成标注图",
                                  "artifacts": [{"type": "image",
                                                 "assetId": block.get("assetId"),
                                                 "name": "trace 联调标注图"}]})
        artifact_seq = s
    except Exception as e:
        artifact_seq = tm.append_event(tid, "error", "标注图生成失败", phase="act",
                                       status="failed", finished=True,
                                       error="%r" % (e,))
        block = {}

    # 失败命令 + 重试（真实错误：照片不存在 / 照片不属于本项目）
    s_fail, c_fail, _, _ = tool_step(
        tid, "send_photo", {"projectId": PROJECT, "photoId": 99999999},
        phase="act", prefix="发送照片", fail=True)
    tool_step(tid, "send_photo", {"projectId": PROJECT, "photoId": 99999999},
              phase="act", prefix="失败重试")
    # 重试真实成功：发送 photo 46
    try:
        r = http("POST", "/api/v1/assets", {"projectId": PROJECT, "photoId": PHOTO,
                                            "caption": "联调照片"})
        a = r.get("data") or {}
        http("POST", "/api/v1/assets/%s/commit" % a.get("assetId"), {})
        s_retry = tm.append_event(tid, "image_read", "重试·发送照片·send_photo",
                                  phase="act", status="running", started=True,
                                  attempt=2, retry_of_seq=c_fail,
                                  step_id="fx-retry-%d" % tid,
                                  meta={"tool": "send_photo", "label": "发送照片",
                                        "artifacts": [{"type": "image",
                                                       "assetId": a.get("assetId"),
                                                       "name": "联调照片"}]})
        cs_retry = tm.finish_event(tid, s_retry, "succeeded",
                                   output_text=json.dumps({"ok": True,
                                                           "assetId": a.get("assetId")},
                                                          ensure_ascii=False),
                                   meta_extra={"secs": 0.2})
        retry_cs = cs_retry
    except Exception as e:
        print("重试成功样例失败：", e)
        retry_cs = None

    # 子智能体：单行摘要，parentSeq 指向最近一个 act step
    sub_parent = retry_cs or artifact_seq
    tm.append_event(tid, "subagent", "派子 agent（reviewer）", phase="act",
                    status="succeeded", started=True, finished=True,
                    parent_seq=sub_parent,
                    summary="子 agent 复核标注结论：未发现明显问题")
    tm.append_event(tid, "subagent", "子 agent 回报", phase="act",
                    status="succeeded", started=True, finished=True,
                    parent_seq=sub_parent, summary="复核通过（1 条建议）")

    # 长输出：get_context 全文通常较长，落 gz + truncated
    tool_step(tid, "get_context", {"projectId": PROJECT}, phase="act",
              prefix="长输出上下文")

    # 文书产物（引用真实 doc，不复制证据）
    docs = core.q("SELECT id, kind, obj FROM doc WHERE project_id=%s ORDER BY id DESC LIMIT 1",
                  (PROJECT,))
    if docs:
        d = docs[0]
        tm.append_event(tid, "artifact", "生成文书", phase="finish", status="succeeded",
                        started=True, finished=True, summary="%s" % d["kind"],
                        meta={"artifacts": [{"type": "doc", "docId": d["id"],
                                             "name": "%s · %s" % (d["kind"], d["obj"] or "")}]})

    tool_step(tid, "validate_doc", {"kind": "监理通知单",
                                    "fields": {"部位": "4#楼东侧外架",
                                               "问题": "连墙件缺失"}},
              phase="finish", prefix="校验文书")
    tm.finish_run(tid, "succeeded", summary="联调样例完成")
    return tid, cid


def make_long(n=124):
    tid, cid = new_fixture_task("long")
    tm.append_event(tid, "plan", "任务理解", phase="plan", status="succeeded",
                    started=True, finished=True, summary="长链路折叠样例（%d 步）" % n)
    last = None
    for i in range(n):
        kind = ("tool", "image_read", "file_read", "file_write", "validation")[i % 5]
        seq = tm.append_event(tid, kind, "步骤 %03d·%s" % (i + 1, kind), phase="act",
                            status="running", started=True,
                            step_id="long-%d-%d" % (tid, i),
                            summary="折叠测试步骤 %d" % (i + 1),
                            meta={"tool": "search_standards" if kind == "tool" else kind,
                                  "label": "步骤 %03d" % (i + 1)})
        cs = tm.finish_event(tid, seq, "succeeded",
                             output_text="第 %d 步输出\n%s" % (i + 1, "结果 " * 30),
                             meta_extra={"secs": 0.1})
        last = cs
        if i and i % 17 == 0:
            tm.append_event(tid, "note", "阶段小结", phase="finish", status="succeeded",
                            started=True, finished=True, summary="已完成 %d 步" % (i + 1))
    tm.finish_run(tid, "succeeded", summary="长链路样例完成")
    return tid, cid


def main():
    tm.ensure_trace_schema()
    short_tid, short_cid = make_short()
    long_tid, long_cid = make_long()
    for tid, label in ((short_tid, "short"), (long_tid, "long")):
        rows = core.q("SELECT COUNT(*) n FROM task_event WHERE task_id=%s", (tid,))
        msgs = core.q("SELECT id FROM message WHERE task_id=%s", (tid,))
        print("%s taskId=%s events=%s messageId=%s projectId=%s"
              % (label, tid, rows[0]["n"], (msgs[0]["id"] if msgs else None), PROJECT))
    print("SHORT_TASK=%s LONG_TASK=%s" % (short_tid, long_tid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
