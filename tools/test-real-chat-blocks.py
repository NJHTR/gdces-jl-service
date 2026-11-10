#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实模型 chat 出图复测（task-14）：带 capabilities 提示“圈出照片45里的配重块”。

    cd /home/baize/jl-service
    python3 tools/test-real-chat-blocks.py                 # 默认跑 3 次，统计失败率
    python3 tools/test-real-chat-blocks.py --attempts 1

每轮：
  1) POST /api/v1/chat（capabilities=["answer.blocks"]）→ 轮询 /api/v1/tasks/<id>
  2) 断言：answer.blocks 有 image_ref；asset committed/derived/sourceRefs=photo:45；
     短签 url 无 header GET 200 image/png；正文无本地绝对路径（/tmp、/home、C:\\、file://）
  3) 记录模型是否仍绕过工具（trace 出现 python/PIL/ImageDraw//tmp 自绘）
  4) 清理本轮 task/message/asset/audit、blocks.json、/tmp 残留文件

注意：真实模型有随机性；本脚本不修改代码，只测“提示词约束 + BFF 兜底”后的成功率。
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "yun-gu-3qi"
UID = int(os.environ.get("JL_TEST_USER", "1"))
PHOTO_ID = int(os.environ.get("JL_TEST_PHOTO", "45"))
PROMPT = os.environ.get("JL_TEST_PROMPT") or "圈出照片45里的配重块，用标注图给我看"
EXPECT_ORIGIN = os.environ.get("JL_TEST_EXPECT_ORIGIN") or "derived"
NEGATIVE = False
WS = os.path.join(ROOT, "data", "workspaces", PROJECT)
BLOCKS_FP = os.path.join(WS, ".agent", "blocks.json")

LOCAL_PATH_RE = re.compile(
    r"(file://[^\s]+|/(?:tmp|home|var|etc|opt|usr|root|mnt|media)/[^\s，。；：）)、！？】\"'`]+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)[^\s，。；）)、！？】\"'`]+)")
BYPASS_HINTS = ("python3", "python ", "ImageDraw", "PIL", "/tmp/", "cv2", "matplotlib")


def load():
    bff = os.path.join(ROOT, "bff")
    if bff not in sys.path:
        sys.path.insert(0, bff)
    import bffpkg.core as core
    import bffpkg.assetsvc as av
    return core, av


def api(method, path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, {"raw": raw[:300]}, dict(e.headers)


def get_bytes(url):
    req = urllib.request.Request(BASE + url)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read(), dict(r.headers)


class Attempt(object):
    def __init__(self):
        self.task_id = None
        self.message_ids = []
        self.asset_ids = []
        self.bypass = []
        self.path_leaks = []
        self.errors = []

    def check(self, name, cond, detail=""):
        if cond:
            print("    PASS  %s" % name)
            return True
        self.errors.append((name, detail))
        print("    FAIL  %s  %s" % (name, detail))
        return False

    def cleanup(self, core, av):
        for aid in self.asset_ids:
            try:
                row = core.q("SELECT project_id, path FROM asset WHERE asset_id=%s", (aid,))
                if row and row[0].get("path"):
                    try:
                        os.remove(os.path.join(ROOT, row[0]["path"]))
                    except OSError:
                        pass
                core.exec_sql("DELETE FROM asset WHERE asset_id=%s", (aid,))
                core.exec_sql("DELETE FROM audit_log WHERE target_type='asset' AND target_id=%s",
                              (aid,))
            except Exception:
                traceback.print_exc()
        for mid in self.message_ids:
            try:
                core.exec_sql("DELETE FROM message WHERE id=%s", (mid,))
            except Exception:
                traceback.print_exc()
        if self.task_id:
            try:
                # 连用户消息一起清（之前只删 assistant 会留 user 行）
                core.exec_sql("DELETE FROM message WHERE task_id=%s", (self.task_id,))
                core.exec_sql("DELETE FROM task WHERE id=%s", (self.task_id,))
            except Exception:
                traceback.print_exc()
        try:
            os.remove(BLOCKS_FP)
        except OSError:
            pass
        # 模型偶尔仍会写 /tmp：只清明显的测试残留
        for pat in ("/tmp/photo*annot*", "/tmp/photo*mark*", "/tmp/jl-*annot*"):
            import glob
            for p in glob.glob(pat):
                try:
                    os.remove(p)
                except OSError:
                    pass


def run_attempt(idx, core, av, timeout=420):
    print("\n=== 第 %d 轮：%s ===" % (idx, PROMPT))
    att = Attempt()
    client_id = "realchat-%d-%d" % (int(time.time() * 1000), idx)
    st, payload, _h = api("POST", "/api/v1/chat", {
        "projectId": PROJECT, "userId": UID, "text": PROMPT,
        "capabilities": ["answer.blocks"], "clientId": client_id})
    if st not in (200, 202):
        att.check("chat 发起", False, "%s %r" % (st, payload))
        return att
    att.task_id = ((payload.get("data") or {}).get("taskId"))
    print("    taskId=%s" % att.task_id)
    started = time.time()
    data = {}
    while time.time() - started < timeout:
        time.sleep(3)
        st, payload, _h = api("GET", "/api/v1/tasks/%d" % att.task_id)
        if st != 200:
            continue
        data = payload.get("data") or {}
        if data.get("status") in ("done", "failed", "cancelled"):
            break
    print("    status=%s  elapsed=%.0fs" % (data.get("status"), time.time() - started))
    msgs = data.get("messages") or []
    att.message_ids = [m.get("id") for m in msgs if m.get("role") == "assistant"]
    blocks = data.get("blocks") or []
    if not blocks:
        for m in msgs:
            a = m.get("answer") or {}
            if isinstance(a, dict) and a.get("blocks"):
                blocks = a["blocks"]
                break
    texts = [m.get("text") or "" for m in msgs if m.get("role") == "assistant"]
    body = "\n".join(texts)
    traces = []
    for m in msgs:
        traces.append(json.dumps(m.get("trace") or [], ensure_ascii=False))
    trace_text = "\n".join(traces)

    att.check("任务 done", data.get("status") == "done", repr(data.get("status")))
    img = next((b for b in blocks if isinstance(b, dict) and b.get("type") == "image_ref"), None)
    leaks = LOCAL_PATH_RE.findall(body)
    att.path_leaks = leaks

    if NEGATIVE:
        # “照片 45 不存在”场景：不能伪造 image_ref，正文必须明确说没有
        att.check("否定场景不伪造 image_ref", not img, repr(blocks)[:300])
        said_no = any(k in body for k in ("没有找到", "没有这张", "不存在", "未找到",
                                          "暂无", "没有照片", "没有图", "查无"))
        att.check("正文明确说没有这张照片", said_no, body[:300])
        att.check("正文无本地绝对路径", not leaks, repr(leaks))
        for hint in BYPASS_HINTS:
            if hint in trace_text:
                att.bypass.append(hint)
        if att.bypass:
            print("    RISK  否定场景 trace 出现自绘痕迹: %r" % att.bypass)
        return att

    att.check("answer.blocks 有 image_ref", bool(img), repr(blocks)[:300])
    if img:
        att.asset_ids.append(img.get("assetId"))
        refs = img.get("sourceRefs") or []
        att.check("image_ref sourceRefs 含 photo:%d" % PHOTO_ID,
                  "photo:%s" % PHOTO_ID in refs, repr(refs))
        att.check("image_ref origin=%s" % EXPECT_ORIGIN,
                  img.get("origin") == EXPECT_ORIGIN, repr(img.get("origin")))
        row = core.q("SELECT asset_id, status, origin, producer, source_refs FROM asset "
                     "WHERE asset_id=%s", (img.get("assetId"),))
        att.check("asset committed", bool(row) and row[0]["status"] == "committed",
                  repr(row))
        try:
            st, b, h = get_bytes(img.get("url") or "")
            att.check("短签 GET 200", st == 200, st)
            att.check("短签是图片", (h.get("Content-Type") or "").startswith("image/"),
                      h.get("Content-Type"))
            att.check("图片字节非空", len(b) > 1000, len(b))
        except Exception as e:
            att.check("短签 GET", False, repr(e))
    att.check("正文无本地绝对路径", not leaks, repr(leaks))
    for hint in BYPASS_HINTS:
        if hint in trace_text:
            att.bypass.append(hint)
    if att.bypass:
        print("    RISK  模型 trace 里出现疑似自绘痕迹: %r（兜底已清正文路径）" % att.bypass)
    else:
        print("    PASS  未发现 shell/python 自绘痕迹")
    return att


def main():
    global PROJECT, PHOTO_ID, PROMPT, EXPECT_ORIGIN, NEGATIVE, WS, BLOCKS_FP
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--photo", type=int, default=PHOTO_ID)
    ap.add_argument("--prompt", default=PROMPT)
    ap.add_argument("--expect-origin", default=EXPECT_ORIGIN,
                    choices=("derived", "uploaded"))
    ap.add_argument("--negative", action="store_true",
                    help="照片不存在场景：断言不伪造 image_ref 且正文明确说没有")
    args = ap.parse_args()
    PROJECT = args.project
    PHOTO_ID = args.photo
    PROMPT = args.prompt
    EXPECT_ORIGIN = args.expect_origin
    NEGATIVE = args.negative
    WS = os.path.join(ROOT, "data", "workspaces", PROJECT)
    BLOCKS_FP = os.path.join(WS, ".agent", "blocks.json")
    core, av = load()
    print("=== 真实 chat 出图复测 project=%s user=%s photo=%s attempts=%d negative=%s ==="
          % (PROJECT, UID, PHOTO_ID, args.attempts, NEGATIVE))
    photo_before = int(core.q("SELECT COUNT(*) n FROM photo")[0]["n"])
    results = []
    try:
        for i in range(1, args.attempts + 1):
            att = run_attempt(i, core, av, timeout=args.timeout)
            ok = not att.errors
            results.append(ok)
            att.cleanup(core, av)
            print("    本轮结果: %s（errors=%d）" % ("PASS" if ok else "FAIL", len(att.errors)))
    finally:
        photo_after = int(core.q("SELECT COUNT(*) n FROM photo")[0]["n"])
        print("\nphoto 行数: %s -> %s（应不变）" % (photo_before, photo_after))
        left_assets = int(core.q("SELECT COUNT(*) n FROM asset")[0]["n"])
        print("清理后 asset 行数: %d" % left_assets)
    passed = sum(1 for x in results if x)
    print("\n==== 结果：%d/%d 轮成功（失败率 %.0f%%） ===="
          % (passed, len(results), 100.0 * (len(results) - passed) / max(1, len(results))))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
