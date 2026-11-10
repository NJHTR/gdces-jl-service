#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 工具链路测试：jl-tool annotate_photo / compare_photos → .agent/blocks.json → chat 出图。

    cd /home/baize/jl-service
    python3 tools/test-agent-tools.py

前置：jl-tools:local 镜像存在（已有）；BFF AUTH_REQUIRED=0 演示模式；使用 yun-gu-3qi
真实照片 #44/#45（只读，不复制证据）。测试结束清理 blocks 文件与测试资产。

覆盖：
  · list 里出现两个新工具；node --check 语法
  · annotate_photo 生成 derived 资产 + blocks.json image_ref（sourceRefs=photo:45）
  · 重复调去重、4 图上限、compare 正常/同图报错、他项目照片/不存在照片报错
  · BFF `_turn_blocks` 读取并挂到 message.answer.blocks；GET /tasks/<id> 能拿到
  · photo 表/records/photos/memory/doc/blobs 不变（不写证据）
"""
import io
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
HOST_ROOT = os.environ.get("JL_HOST_ROOT") or "/home/baize/jl-service"
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PID = os.environ.get("JL_TEST_PROJECT") or "yun-gu-3qi"
UID = int(os.environ.get("JL_TEST_USER", "1"))
WS = os.path.join(HOST_ROOT, "data", "workspaces", PID)
JL_TOOL = os.environ.get("JL_TOOL_BIN") or os.path.join(HOST_ROOT, "scripts", "jl-tool")
PHOTO_A = int(os.environ.get("JL_TEST_PHOTO_A", "45"))
PHOTO_B = int(os.environ.get("JL_TEST_PHOTO_B", "44"))
DOCKER_IMAGE = os.environ.get("JL_TOOL_IMAGE") or "jl-tools:local"

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
    import bffpkg.biz as biz
    import bffpkg.assetsvc as av
    import bffpkg.agent as agent
    return core, biz, av, agent


BLOCKS_FP = os.path.join(WS, ".agent", "blocks.json")


def rm_blocks():
    try:
        os.remove(BLOCKS_FP)
    except OSError:
        pass


def read_blocks_raw():
    try:
        with open(BLOCKS_FP, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, list) else (raw.get("blocks") or [])
    except (OSError, ValueError):
        return []


def run_tool(tool, args, cwd=None, timeout=180):
    p = subprocess.run([JL_TOOL, tool, json.dumps(args, ensure_ascii=False)],
                       cwd=cwd or WS, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def parse_out(out):
    """jl-tool 输出工具结果的 JSON 文本；失败时是「工具执行失败: ...」。"""
    s = (out or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except ValueError:
            pass
    return None


def is_err(out):
    return "工具执行失败" in (out or "")


def snapshots(core):
    with core.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM photo")
        photos = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM doc")
        docs = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM memory")
        memory = int(cur.fetchone()["n"])
    blobs = sorted(os.listdir(core.BLOB_ROOT)) if os.path.isdir(core.BLOB_ROOT) else []
    rec_root = os.path.join(WS, "records")
    records = []
    if os.path.isdir(rec_root):
        for root, _d, files in os.walk(rec_root):
            for fn in sorted(files):
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                    records.append((os.path.relpath(p, rec_root), st.st_size, int(st.st_mtime)))
                except OSError:
                    pass
    return {"photos": photos, "docs": docs, "memory": memory, "blobs": blobs,
            "records": records}


def fetch(url):
    req = urllib.request.Request(BASE + url)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read(), dict(r.headers)


def main():
    core, biz, av, agent = load()
    print("=== Agent 工具链路测试 project=%s cwd=%s ===" % (PID, WS))
    created_assets = []
    created_rows = []
    base = snapshots(core)
    rm_blocks()
    try:
        # ---- 0) 输出侧清洗（task-14）：本地绝对路径 / 假图提示 ----
        leaked = ("标注图已生成，文件保存在 /tmp/photo45_annotated.jpg，"
                  "也可看 file:///home/baize/x.png 或 C:\\tmp\\a.png")
        cleaned = core.clean_reply(leaked)
        check("清洗 /tmp 路径", "/tmp/" not in cleaned and "photo45_annotated" not in cleaned,
              cleaned)
        check("清洗 file:// 与 C:\\ 路径", "file://" not in cleaned and "C:\\" not in cleaned,
              cleaned)
        check("「文件已保存在」被替换/清掉", "文件已保存在" not in cleaned, cleaned)
        keep = core.clean_reply("见 /api/v1/assets/x/content 与 records/rules.md")
        check("新清洗不影响 /api/ 与相对路径",
              "/api/v1/assets/x/content" in keep and "records/rules.md" in keep, keep)
        raw_keep = core.strip_local_paths("docs/2026-01-01/a.md /api/v1/x records/y.md")
        check("strip_local_paths 不影响 docs//api/相对路径",
              "docs/2026-01-01/a.md" in raw_keep and "/api/v1/x" in raw_keep
              and "records/y.md" in raw_keep, raw_keep)
        hint = core.ensure_image_fallback("标注图已生成，请看。", [])
        check("有图声明但 blocks 空 → 补提示", "未生成可展示的图片" in hint, hint)
        no_hint = core.ensure_image_fallback("标注图已生成。", [{"type": "image_ref"}])
        check("有 blocks 不补提示", "未生成可展示的图片" not in no_hint, no_hint)
        manual = core.tool_manual()
        check("tool_manual 有 argv JSON 用法", "第二个命令行参数" in manual, manual[:200])
        check("tool_manual 禁止 shell/python 自绘",
              "shell/python" in manual and "/tmp" in manual, manual[-200:])

        # ---- 0b) 工具已注册 + JS 语法 ----
        rc, out, err = run_tool("list", {})
        names, descs = [], {}
        try:
            data = json.loads(out[out.find("{"):out.rfind("}") + 1])
            defs = data.get("tools") or []
            names = [t.get("name") for t in defs]
            descs = {t.get("name"): (t.get("description") or "") for t in defs}
        except ValueError:
            pass
        check("list 含 annotate_photo", "annotate_photo" in names, repr(names))
        check("list 含 compare_photos", "compare_photos" in names, repr(names))
        check("annotate_photo 描述有真实照片/派生图约束",
              "真实" in descs.get("annotate_photo", "")
              and "派生图" in descs.get("annotate_photo", ""),
              descs.get("annotate_photo", "")[:120])
        check("compare_photos 描述有整改前后语义",
              "整改前" in descs.get("compare_photos", ""),
              descs.get("compare_photos", "")[:120])
        manual = core.tool_manual()
        check("tool_manual 列出 annotate_photo", "annotate_photo" in manual)
        check("tool_manual 列出 compare_photos", "compare_photos" in manual)
        for f in ("tools.js", "server.js"):
            p = subprocess.run(
                ["docker", "run", "--rm", "-v", os.path.join(ROOT, "tools", "src") + ":/app/src:ro",
                 "--entrypoint", "node", DOCKER_IMAGE, "--check", "/app/src/" + f],
                capture_output=True, text=True, timeout=120)
            eq("node --check %s" % f, p.returncode, 0, (p.stderr or "")[:200])

        # ---- 1) annotate happy path ----
        rc, out, err = run_tool("annotate_photo", {
            "projectId": PID, "photoId": PHOTO_A,
            "boxes": [{"x": 200, "y": 200, "w": 320, "h": 240, "label": "工具链路"},
                      {"x": 700, "y": 500, "w": 240, "h": 180}],
            "caption": "工具链路自测标注图"})
        obj = parse_out(out)
        check("annotate 返回 JSON", isinstance(obj, dict), (out or "")[:300] + (err or "")[:200])
        eq("annotate ok", (obj or {}).get("ok"), True)
        aid = (obj or {}).get("assetId")
        check("annotate 返回 assetId(ULID)", bool(aid) and len(str(aid)) == 26, repr(aid))
        if aid:
            created_assets.append(aid)
        blocks = read_blocks_raw()
        eq("blocks.json 写入 1 条", len(blocks), 1)
        b = blocks[0] if blocks else {}
        eq("block 类型 image_ref", b.get("type"), "image_ref")
        eq("block origin derived", b.get("origin"), "derived")
        check("block sourceRefs 指向 photo", b.get("sourceRefs") == ["photo:%s" % PHOTO_A],
              repr(b.get("sourceRefs")))
        check("block url 是短签相对路径", "/api/v1/assets/" in (b.get("url") or ""),
              repr(b.get("url")))
        raw_text = json.dumps(blocks, ensure_ascii=False)
        check("blocks 无 base64/本地路径/外链",
              "base64," not in raw_text and "/home/" not in raw_text and "http" not in raw_text)
        row = av._row(aid) if aid else None
        check("资产 committed", bool(row) and row["status"] == "committed")
        check("资产 derived/annotate", bool(row) and row["origin"] == "derived"
              and row["producer"] == "annotate")
        refs = json.loads(row["source_refs"]) if isinstance(row.get("source_refs"), str) else row.get("source_refs")
        eq("资产 sourceRefs", refs, ["photo:%s" % PHOTO_A])
        st, body, h = fetch(b.get("url") or "")
        eq("短签取图 200", st, 200)
        eq("短签 Content-Type", h.get("Content-Type"), "image/png")
        check("短签取到 PNG", body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) > 1000, len(body))
        # 不写证据
        snap1 = snapshots(core)
        eq("photo 表行数不变", snap1["photos"], base["photos"])
        eq("doc/memory 不变", (snap1["docs"], snap1["memory"]), (base["docs"], base["memory"]))
        eq("blob 不新增", snap1["blobs"], base["blobs"])
        check("records/photos 无新文件", snap1["records"] == base["records"])

        # ---- 2) 去重：同照片再调不重复生成 ----
        assets_before = int(core.q("SELECT COUNT(*) n FROM asset")[0]["n"])
        rc, out, err = run_tool("annotate_photo", {
            "projectId": PID, "photoId": PHOTO_A,
            "boxes": [{"x": 10, "y": 10, "w": 50, "h": 50}]})
        obj = parse_out(out)
        eq("重复调 annotate duplicated", (obj or {}).get("duplicated"), True, (out or "")[:200])
        eq("blocks 仍 1 条", len(read_blocks_raw()), 1)
        et("重复调不新建资产", int(core.q("SELECT COUNT(*) n FROM asset")[0]["n"]), assets_before)

        # ---- 3) 4 图上限 ----
        fake = [{"type": "image_ref", "assetId": "01J%020d" % i,
                 "caption": "占位 %d" % i, "origin": "derived", "mime": "image/png",
                 "sourceRefs": ["photo:%d" % (900 + i)],
                 "url": "/api/v1/assets/01J%020d/content?e=1&s=x" % i} for i in range(4)]
        os.makedirs(os.path.dirname(BLOCKS_FP), exist_ok=True)
        with open(BLOCKS_FP, "w", encoding="utf-8") as f:
            json.dump({"blocks": fake}, f)
        rc, out, err = run_tool("annotate_photo", {
            "projectId": PID, "photoId": PHOTO_B, "boxes": []})
        check("4 图上限报错", is_err(out), (out or "")[:200])
        eq("上限时 blocks 不变", len(read_blocks_raw()), 4)
        et("上限时不新建资产", int(core.q("SELECT COUNT(*) n FROM asset")[0]["n"]), assets_before)

        # ---- 4) compare 正常 / 同图报错 ----
        rm_blocks()
        rc, out, err = run_tool("compare_photos", {
            "projectId": PID, "beforePhotoId": PHOTO_B, "afterPhotoId": PHOTO_A,
            "title": "工具链路对比图"})
        obj = parse_out(out)
        eq("compare ok", (obj or {}).get("ok"), True, (out or "")[:300])
        caid = (obj or {}).get("assetId")
        if caid:
            created_assets.append(caid)
        blocks = read_blocks_raw()
        eq("compare 写入 1 条", len(blocks), 1)
        eq("compare sourceRefs 两张",
           sorted((blocks[0].get("sourceRefs") or []) if blocks else []),
           sorted(["photo:%s" % PHOTO_A, "photo:%s" % PHOTO_B]))
        crow = av._row(caid) if caid else None
        check("compare 资产 committed/derived",
              bool(crow) and crow["status"] == "committed" and crow["producer"] == "compare")
        rc, out, err = run_tool("compare_photos", {
            "projectId": PID, "beforePhotoId": PHOTO_A, "afterPhotoId": PHOTO_A})
        check("同图对比报错", is_err(out), (out or "")[:200])
        eq("同图报错不改 blocks", len(read_blocks_raw()), 1)

        # ---- 5) 错误：他项目照片 / 不存在 / 参数 ----
        other_ws = os.path.join(HOST_ROOT, "data", "workspaces", "test-sandbox")
        rc, out, err = run_tool("annotate_photo", {
            "projectId": "test-sandbox", "photoId": PHOTO_A, "boxes": []}, cwd=other_ws)
        check("他项目照片报错（不伪造）", is_err(out), (out or "")[:200])
        rc, out, err = run_tool("annotate_photo", {
            "projectId": PID, "photoId": 99999999, "boxes": []})
        check("不存在 photoId 报错", is_err(out), (out or "")[:200])
        rc, out, err = run_tool("annotate_photo", {"projectId": PID, "boxes": []})
        check("缺 photoId 报错", is_err(out), (out or "")[:200])

        # ---- 6) BFF 集成：_turn_blocks → answer.blocks → task 轮询 ----
        rm_blocks()
        rc, out, err = run_tool("annotate_photo", {
            "projectId": PID, "photoId": PHOTO_A,
            "boxes": [{"x": 300, "y": 300, "w": 300, "h": 200, "label": "联调"}],
            "caption": "chat 出图自测"})
        it_obj = parse_out(out)
        if it_obj and it_obj.get("assetId"):
            created_assets.append(it_obj["assetId"])
        check("集成前 blocks 文件存在", os.path.isfile(BLOCKS_FP))
        task_id = core.exec_sql(
            "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
            "VALUES (%s,%s,%s,'chat',%s,'done')",
            (PID, UID, "tool-itg-%d" % int(time.time() * 1000),
             json.dumps({"text": "看图", "capabilities": ["answer.blocks"]})))
        created_rows.append(("task", task_id))
        got_blocks = agent._turn_blocks(PID, UID, task_id)
        check("_turn_blocks 取到 image_ref", bool(got_blocks) and got_blocks[0].get("type") == "image_ref",
              repr(got_blocks)[:300])
        check("_turn_blocks 读完删文件", not os.path.isfile(BLOCKS_FP))
        answer = core.build_answer(None, [], "这是现场照片的标注图。", got_blocks)
        mid = core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,1,%s)",
            (PID, UID, "tool-itg-ans-%d" % int(time.time() * 1000),
             "这是现场照片的标注图。", task_id, json.dumps(answer, ensure_ascii=False)))
        created_rows.append(("message", mid))
        av.attach_message(got_blocks, mid)
        st, body, h = fetch("/api/v1/tasks/%d" % task_id)
        payload = json.loads(body or b"{}")
        data = payload.get("data") or {}
        check("GET /tasks 顶层 blocks 有图",
              any(x.get("type") == "image_ref" for x in (data.get("blocks") or [])),
              repr(data.get("blocks"))[:300])
        msgs = data.get("messages") or []
        m_ans = (msgs[0].get("answer") if msgs else None) or {}
        check("message.answer.blocks 有图",
              any(x.get("type") == "image_ref" for x in (m_ans.get("blocks") or [])))
        img = next((x for x in (m_ans.get("blocks") or []) if x.get("type") == "image_ref"), {})
        st, body, h = fetch(img.get("url") or "")
        eq("回答里的短签图 200", st, 200)
        eq("回答里的短签 Content-Type", h.get("Content-Type"), "image/png")

        # ---- 7) 证据/记忆/知识没被写 ----
        snap2 = snapshots(core)
        eq("最终 photo 行数不变", snap2["photos"], base["photos"])
        eq("最终 doc/memory 不变", (snap2["docs"], snap2["memory"]),
           (base["docs"], base["memory"]))
        eq("最终 blob 不新增", snap2["blobs"], base["blobs"])
        check("最终 records/photos 无新文件", snap2["records"] == base["records"])
    finally:
        rm_blocks()
        for aid in created_assets:
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
        for table, rid in created_rows:
            try:
                core.exec_sql("DELETE FROM %s WHERE id=%%s" % table, (rid,))
            except Exception:
                traceback.print_exc()

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


def et(name, got, want, detail=""):
    eq(name, got, want, detail)


if __name__ == "__main__":
    sys.exit(main())
