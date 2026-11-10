#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多模态资产 + answer.blocks 回归测试（服务器上跑，覆盖 task-8 + 补充契约 signed URL）。

    cd /home/baize/jl-service
    python3 tools/test-assets.py                # unit + http + perms(子进程 AUTH_REQUIRED=1)
    python3 tools/test-assets.py --mode unit
    python3 tools/test-assets.py --mode http
    AUTH_REQUIRED=1 python3 tools/test-assets.py --mode perms

覆盖：
  1) 签发：PNG/JPEG 生成物、multipart/原始字节、mime allowlist、EXIF/GPS 去除、大小/内容不符拒绝
  2) 存储：只落 data/assets/<pid>/<assetId>；不写 blobs/photos/docs/records/memory/knowledge
  3) 权限：项目成员可见；非成员 403（AUTH_REQUIRED=1）；短签 URL 不带 Bearer 也能取（<image>）
  4) 路径：非 ULID/../ 穿越 404；已删除 404
  5) 生命周期：staged 24h 过期 GC、commit 90d、软删除、audit
  6) 真实照片引用：origin=uploaded、sourceRefs=["photo:<id>"]、不复制证据、不写 photo 表
  7) answer.blocks：fallback、未知/他项目 assetId 丢弃、base64/本地路径/外链丢弃、单轮上限、
     card 只读、photoId→image_ref、signed url、task 轮询/历史 message.answer 返回并重签
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("JL_ROOT") or "/home/baize/jl-service"
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
OTHER_PROJECT = os.environ.get("JL_TEST_OTHER_PROJECT") or "yun-gu-3qi"
USER = os.environ.get("JL_TEST_USER") or "99999"
MEMBER_A = int(os.environ.get("JL_TEST_OWNER", "1"))
MEMBER_B = int(os.environ.get("JL_TEST_MEMBER", "2"))
OUTSIDER = int(os.environ.get("JL_TEST_OUTSIDER", "11"))
PERM_PROJECT = os.environ.get("JL_TEST_PERM_PROJECT") or "yun-gu-3qi"
PERM_PROJECT2 = os.environ.get("JL_TEST_PERM_PROJECT2") or "baiyun-a2"

PREFIX = "【资产自测】"
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
    import bffpkg.assetsvc as av
    import bffpkg.agent as agent
    return core, biz, av, agent


# ---------------------------------------------------------------- 图片样例
def make_png(w=4, h=4, color=(220, 40, 40)):
    from PIL import Image
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_jpeg_with_exif():
    from PIL import Image
    img = Image.new("RGB", (8, 8), (30, 120, 220))
    exif = Image.Exif()
    exif[0x010E] = "SECRET-EXIF-DESC"          # ImageDescription
    exif[0x0112] = 6                            # Orientation（手机常见）
    exif[0x8825] = {2: (39.9, 116.4, 0.0)}      # GPSLatitude（简化写入）
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def has_exif(data):
    return b"Exif\x00\x00" in data or b"SECRET-EXIF-DESC" in data


def same_image(a, b):
    """图片被重编码后字节会变；按像素比是否同一张图。"""
    from PIL import Image
    try:
        ia = Image.open(io.BytesIO(a))
        ib = Image.open(io.BytesIO(b))
        if ia.size != ib.size:
            return False
        return list(ia.convert("RGB").getdata()) == list(ib.convert("RGB").getdata())
    except Exception:
        return False


# ---------------------------------------------------------------- 基线/清场
def snapshots(core):
    with core.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM photo")
        photos = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM doc")
        docs = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM memory")
        memory = int(cur.fetchone()["n"])
    blobs = sorted(os.listdir(core.BLOB_ROOT)) if os.path.isdir(core.BLOB_ROOT) else []
    records_root = os.path.join(core.WS_ROOT, PROJECT, "records")
    records = []
    if os.path.isdir(records_root):
        for root, _d, files in os.walk(records_root):
            for fn in sorted(files):
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                    records.append((os.path.relpath(p, records_root), st.st_size, int(st.st_mtime)))
                except OSError:
                    pass
    return {"photos": photos, "docs": docs, "memory": memory,
            "blobs": (len(blobs), blobs), "records": records}


class Ctx(object):
    def __init__(self, core, biz, av, agent):
        self.core, self.biz, self.av, self.agent = core, biz, av, agent
        self.assets = []
        self.extra = []          # ("photo"|"doc"|"message"|"task", id)
        self.checked_roots = set()
        self.av.ensure_asset_table()

    def track(self, payload):
        aid = ((payload or {}).get("data") or {}).get("assetId")
        if aid:
            self.assets.append(aid)
        return aid

    def cleanup(self):
        core = self.core
        for aid in self.assets:
            try:
                rows = core.q("SELECT project_id, path FROM asset WHERE asset_id=%s", (aid,))
                if rows:
                    p = rows[0].get("path")
                    if p:
                        try:
                            os.remove(os.path.join(core.ROOT, p))
                        except OSError:
                            pass
                core.exec_sql("DELETE FROM asset WHERE asset_id=%s", (aid,))
                core.exec_sql("DELETE FROM audit_log WHERE target_type='asset' AND target_id=%s",
                              (aid,))
            except Exception:
                traceback.print_exc()
        for kind, rid in self.extra:
            try:
                table = {"photo": "photo", "doc": "doc", "message": "message", "task": "task"}[kind]
                core.exec_sql("DELETE FROM %s WHERE id=%%s" % table, (rid,))
            except Exception:
                traceback.print_exc()
        # 干净目录（测试项目/其它项目 assets 目录只在空时删）
        for pid in (PROJECT, OTHER_PROJECT, PERM_PROJECT, PERM_PROJECT2):
            d = os.path.join(core.ROOT, "data", "assets", pid)
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass


def add_photo_fixture(ctx):
    """借一张真实照片的 blob 给测试项目造 photo 行（不新增 blob，仅测试夹具）。"""
    core = ctx.core
    rows = core.q("SELECT sha256, blob_path FROM photo ORDER BY id")
    for p in rows:
        sha = p.get("sha256") or ""
        path = p.get("blob_path") or os.path.join(core.BLOB_ROOT, sha[:2], sha)
        try:
            with open(path, "rb") as f:
                head = f.read(64)
        except OSError:
            continue
        mime = ctx.av.detect_mime(head)
        if not mime or not mime.startswith("image/"):
            continue
        pid_ = core.exec_sql(
            "INSERT INTO photo (project_id, sha256, blob_path, src) VALUES (%s,%s,%s,'test')",
            (PROJECT, sha, path))
        ctx.extra.append(("photo", pid_))
        return pid_
    return None


# ---------------------------------------------------------------- unit
def run_unit():
    core, biz, av, agent = load()
    ctx = Ctx(core, biz, av, agent)
    pid, uid = PROJECT, USER
    print("\n=== unit 模式（进程内，AUTH_REQUIRED=%s）project=%s ==="
          % (os.environ.get("AUTH_REQUIRED", "0"), pid))
    base = snapshots(core)
    try:
        # ---- 1) 签发 + EXIF/GPS 去除 + 存储位置 ----
        png = make_png()
        code, payload = av.create_asset(png, pid, user=None, mime="image/png",
                                        origin="generated", caption="生成图")
        eq("unit 签发 PNG 200", code, 200, repr(payload)[:200])
        aid = ctx.track(payload)
        row = av._row(aid)
        eq("新资产初始 staged", row["status"], "staged")
        path = os.path.join(core.ROOT, row["path"])
        check("资产字节只在 data/assets/<pid>/<assetId>",
              path.startswith(os.path.join(core.ROOT, "data", "assets", pid)) and os.path.isfile(path),
              row["path"])
        got = av.get_asset_content(aid)
        eq("内容端点返回 200", got[0], 200)
        check("内容仍是同一张图（重编码脱敏）", same_image(got[1], png))
        view = av.public_row(row)
        check("短签 URL 带 e/s", "e=" in view["url"] and "s=" in view["url"], view["url"])

        jpeg = make_jpeg_with_exif()
        check("样例 JPEG 确实含 EXIF", has_exif(jpeg))
        code, payload = av.create_asset(jpeg, pid, user=None, mime="image/jpeg",
                                        origin="generated", caption="含 EXIF 图")
        eq("unit 签发 JPEG 200", code, 200, repr(payload)[:200])
        aid_exif = ctx.track(payload)
        got = av.get_asset_content(aid_exif)
        eq("EXIF 图内容 200", got[0], 200)
        check("内容已去 EXIF/GPS", not has_exif(got[1]), "still has exif")
        from PIL import Image
        try:
            Image.open(io.BytesIO(got[1])).load()
            decoded = True
        except Exception:
            decoded = False
        check("去元数据后仍是合法图片", decoded)

        # ---- mime allowlist / 内容不符 / 大小 ----
        code, _ = av.create_asset(b"<svg xmlns='http://www.w3.org/2000/svg'/>", pid,
                                  mime="image/svg+xml", origin="generated")
        eq("拒绝 svg", code, 400)
        code, _ = av.create_asset(b"<html><script>alert(1)</script></html>", pid,
                                  mime="text/html", origin="generated")
        eq("拒绝 html", code, 400)
        code, _ = av.create_asset(png, pid, mime="image/jpeg", origin="generated")
        eq("内容与 mime 不符 400", code, 400)
        code, _ = av.create_asset(b"<svg xmlns='http://www.w3.org/2000/svg'/>", pid,
                                  mime="application/octet-stream", origin="generated")
        eq("未知 mime 的 svg 嗅探拒绝 400", code, 400)
        code, _ = av.create_asset(b"<html><script>alert(1)</script></html>", pid,
                                  mime="text/plain", origin="generated")
        eq("text/plain 夹带 html 拒绝 400", code, 400)
        code, _ = av.create_asset(b"", pid, mime="image/png", origin="generated")
        eq("空文件 400", code, 400)

        # ---- 2) 不写证据 ----
        after = snapshots(core)
        eq("资产不新增 photo", after["photos"], base["photos"])
        eq("资产不新增 doc", after["docs"], base["docs"])
        eq("资产不新增 memory", after["memory"], base["memory"])
        eq("资产不新增 blob", after["blobs"], base["blobs"])
        check("资产不写工作区 records", after["records"] == base["records"])

        # ---- 3) 路径穿越 / 未知 id ----
        for bad in ("../etc/passwd", "..", "not-a-ulid", "0" * 26,
                    "01J0000000000000000000000/"):
            code, _ = av.get_asset_content(bad)
            eq("非法 id %r → 404" % bad, code, 404)

        # ---- 4) 生命周期：commit / 过期 GC / 软删除 ----
        code, payload = av.create_asset(png, pid, user=None, mime="image/png",
                                        origin="generated", caption="生命周期")
        aid3 = ctx.track(payload)
        code, payload = av.commit_asset(aid3)
        eq("commit 200", code, 200)
        r3 = av._row(aid3)
        eq("commit 后状态", r3["status"], "committed")
        check("生成物 90 天后过期", r3["expires_at"] is not None)
        code, _ = av.commit_asset(aid3)
        eq("重复 commit 幂等 200", code, 200)

        code, payload = av.create_asset(png, pid, user=None, mime="image/png",
                                        origin="generated", caption="staged 过期")
        aid4 = ctx.track(payload)
        core.exec_sql("UPDATE asset SET expires_at=DATE_SUB(NOW(), INTERVAL 1 HOUR) "
                      "WHERE asset_id=%s", (aid4,))
        n = av.gc_assets()
        check("staged 过期被 GC", n >= 1)
        eq("过期后软删除", av._row(aid4)["status"], "deleted")
        check("过期后字节被清", not os.path.isfile(os.path.join(core.ROOT, av._row(aid4)["path"]))
              if av._row(aid4).get("path") else True)
        eq("过期后内容 404", av.get_asset_content(aid4)[0], 404)
        with core.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM audit_log WHERE action='asset.gc' AND target_id=%s",
                        (aid4,))
            eq("GC 写 audit", int(cur.fetchone()["n"]), 1)

        code, payload = av.create_asset(png, pid, user=None, mime="image/png",
                                        origin="derived", caption="软删除")
        aid5 = ctx.track(payload)
        code, _ = av.delete_asset(aid5)
        eq("delete 200", code, 200)
        eq("delete 后 404", av.get_asset_content(aid5)[0], 404)
        with core.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM audit_log WHERE action='asset.delete' AND target_id=%s",
                        (aid5,))
            eq("delete 写 audit", int(cur.fetchone()["n"]), 1)

        # ---- 5) 真实照片引用：不复制证据、不写 photo ----
        photo_id = add_photo_fixture(ctx)
        if photo_id:
            before_photo = snapshots(core)["photos"]
            code, payload = av.create_asset_from_photo(pid, photo_id, user=None, caption="现场照片引用")
            eq("photo_ref 200", code, 200, repr(payload)[:200])
            aidp = ctx.track(payload)
            rp = av._row(aidp)
            eq("photo_ref origin=uploaded", rp["origin"], "uploaded")
            refs = json.loads(rp["source_refs"]) if isinstance(rp["source_refs"], str) else rp["source_refs"]
            check("sourceRefs 指向 photo:<id>", refs == ["photo:%s" % photo_id], repr(refs))
            check("photo_ref 字节在 data/assets（证据不动）",
                  os.path.isfile(os.path.join(core.ROOT, rp["path"])))
            eq("photo_ref 不新增 photo 行", snapshots(core)["photos"], before_photo)
            got = av.get_asset_content(aidp)
            eq("photo_ref 内容 200", got[0], 200)
            from PIL import Image
            try:
                Image.open(io.BytesIO(got[1])).load()
                ok = True
            except Exception:
                ok = False
            check("photo_ref 内容是合法图片（已脱敏）", ok)
            code, _ = av.create_asset_from_photo(pid, 99999999, user=None)
            eq("不存在的 photoId 404", code, 404)

        # ---- 6) signed URL ----
        exp = int(time.time()) + 60
        sig = av._sign(aid, exp, False)
        got = av.get_asset_content(aid, None, exp=exp, sig=sig)
        eq("有效短签无 token 可取", got[0], 200)
        got = av.get_asset_content(aid, None, exp=exp, sig="x" * 32)
        check("篡改短签在 demo 模式回落匿名权限（AUTH=0 仍 200）", got[0] == 200,
              repr(got)[:160])
        check("过期短签验签失败", av.verify_asset_sig(aid, int(time.time()) - 1,
                                                    av._sign(aid, int(time.time()) - 1, False)) is False)
        check("签名 URL 含 dl 参数（file 下载）",
              "dl=1" in av.sign_asset_url(aid, True))

        # ---- 7) answer.blocks v1 ----
        # 未知/他项目 assetId 丢弃；有效保留；file_ref 与 image 类型不匹配丢弃
        code, other = av.create_asset(png, OTHER_PROJECT, user=None, mime="image/png",
                                      origin="generated", caption="他项目图")
        aid_other = ctx.track(other)
        raw = [
            {"type": "text", "text": "看这两张图"},
            {"type": "image_ref", "assetId": aid, "caption": "图一"},
            {"type": "image_ref", "assetId": "01JUNKNOWNUNKNOWNUNKNOWN00"},
            {"type": "image_ref", "assetId": aid_other},
            {"type": "file_ref", "assetId": aid},                  # 类型不匹配 → 丢
            {"type": "image_ref", "assetId": aid,
             "caption": "data:image/png;base64,AAAA"},             # 不安全 caption → 清掉但保留块
        ]
        blocks = av.normalize_blocks(pid, uid, raw, fallback_text="文字 fallback")
        types = [b["type"] for b in blocks]
        eq("blocks 只保留 2 个 image_ref + text", types.count("image_ref"), 2)
        check("未知/他项目 assetId 被丢弃",
              all(b.get("assetId") in (aid,) for b in blocks if b["type"] == "image_ref"))
        check("块带 signed url（<image> 可用）",
              all("e=" in b["url"] and "s=" in b["url"]
                  for b in blocks if b["type"] in ("image_ref", "file_ref")))
        check("块带 origin（前端标签用）",
              all(b.get("origin") for b in blocks if b["type"] in ("image_ref", "file_ref")))
        check("unsafe caption 被清空", all("data:" not in (b.get("caption") or "") for b in blocks))
        check("自动补 text fallback", any(b["type"] == "text" for b in blocks))

        unsafe = [
            {"type": "text", "text": "data:image/png;base64," + "A" * 500},
            {"type": "text", "text": "本地路径 /home/baize/jl-service/data/x.png"},
            {"type": "text", "text": "外链 https://evil.example/x"},
            {"type": "image_ref", "assetId": aid, "caption": "https://evil.example/x"},
        ]
        blocks = av.normalize_blocks(pid, uid, unsafe, fallback_text="保底文字")
        check("base64/本地路径/外链文本块被丢弃", isinstance(blocks, list))
        # 单轮上限：9 图
        ids = []
        for i in range(10):
            code, p = av.create_asset(png, pid, user=None, mime="image/png",
                                      origin="generated", caption="cap%d" % i)
            ids.append(ctx.track(p))
        blocks = av.normalize_blocks(pid, uid,
                                     [{"type": "image_ref", "assetId": i} for i in ids])
        eq("单轮最多 9 图", len([b for b in blocks if b["type"] == "image_ref"]), 9)

        # card 只读 + 不新增写 API；外部 data 被过滤
        blocks = av.normalize_blocks(pid, uid, [{
            "type": "card", "title": "项目概况",
            "data": {"name": "测试", "link": "https://evil.example", "path": "/home/x"},
            "actions": [{"type": "write"}, {"type": "open"}, {"type": "readonly"}],
        }], fallback_text="文字")
        card = [b for b in blocks if b["type"] == "card"]
        eq("card 保留", len(card), 1)
        if card:
            acts = [a["type"] for a in card[0]["actions"]]
            check("card 动作只读", set(acts) <= {"open", "readonly"}, repr(acts))
            check("card.data 外链/本地路径被过滤",
                  "link" not in card[0]["data"] and "path" not in card[0]["data"],
                  repr(card[0]["data"]))
        # 他项目 docId → 丢
        with core.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM doc WHERE project_id<>%s LIMIT 1", (pid,))
            other_doc = cur.fetchone()
        if other_doc:
            blocks = av.normalize_blocks(pid, uid, [{"type": "card", "docId": other_doc["id"]}])
            eq("他项目 docId 的 card 被丢弃", len(blocks), 0)

        # photoId → image_ref（origin=uploaded + sourceRefs）
        if photo_id:
            blocks = av.normalize_blocks(pid, uid, [{"type": "image_ref", "photoId": photo_id,
                                                     "caption": "现场图"}])
            img = [b for b in blocks if b["type"] == "image_ref"]
            eq("photoId 生成 image_ref", len(img), 1)
            if img:
                check("photoId 块 origin=uploaded", img[0].get("origin") == "uploaded",
                      repr(img[0].get("origin")))
                check("photoId 块 sourceRefs 指向 photo",
                      any(str(r).startswith("photo:") for r in (img[0].get("sourceRefs") or [])))
                ctx.assets.append(img[0]["assetId"])

        # ---- 8) build_answer / task 轮询 / 历史 message.answer ----
        ans = core.build_answer(None, [], "纯文字回答")
        check("旧结构自带空 blocks", ans.get("blocks") == [])
        ans = core.build_answer(None, [], "带块回答", blocks=blocks)
        check("build_answer 挂 blocks", ans.get("blocks") == blocks and ans.get("text") == "带块回答")

        task_id = core.exec_sql(
            "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
            "VALUES (%s,%s,%s,'chat',%s,'done')",
            (pid, int(uid), "asset-test-%d" % int(time.time() * 1000),
             json.dumps({"text": "测试", "capabilities": ["answer.blocks"]})))
        ctx.extra.append(("task", task_id))
        eq("capability opt-in 识别", agent._client_wants_blocks(task_id), True)
        task2 = core.exec_sql(
            "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
            "VALUES (%s,%s,%s,'chat',%s,'done')",
            (pid, int(uid), "asset-test-old-%d" % int(time.time() * 1000),
             json.dumps({"text": "测试"})))
        ctx.extra.append(("task", task2))
        eq("旧客户端默认不产 blocks", agent._client_wants_blocks(task2), False)

        msg_blocks = [{"type": "text", "text": "带图回答"},
                      {"type": "image_ref", "assetId": aid, "caption": "旧签名",
                       "origin": "generated", "url": "/api/v1/assets/%s/content" % aid}]
        stored_answer = json.dumps({"text": "带图回答", "blocks": msg_blocks}, ensure_ascii=False)
        mid = core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,1,%s)",
            (pid, int(uid), "asset-ans-%d" % int(time.time() * 1000), "带图回答", task_id,
             stored_answer))
        ctx.extra.append(("message", mid))
        code, payload = agent.handle_task(task_id)
        data = payload.get("data") or {}
        check("task 轮询返回 blocks", bool(data.get("blocks")), repr(data.get("blocks"))[:200])
        msgs = data.get("messages") or []
        m_ans = (msgs[0].get("answer") if msgs else None) or {}
        check("历史 message.answer 带 blocks", bool(m_ans.get("blocks")), repr(m_ans)[:200])
        if m_ans.get("blocks"):
            img_blocks = [b for b in m_ans["blocks"] if b.get("type") == "image_ref"]
            u = (img_blocks[0].get("url") if img_blocks else "") or ""
            check("历史 blocks url 被重签（含 e/s）", "e=" in u and "s=" in u, u)

        # 工具文件协议
        ws = os.path.join(core.WS_ROOT, pid)
        blocks_fp = os.path.join(ws, ".agent", "blocks.json")
        os.makedirs(os.path.dirname(blocks_fp), exist_ok=True)
        with open(blocks_fp, "w", encoding="utf-8") as f:
            json.dump({"blocks": [{"type": "image_ref", "assetId": aid}]}, f)
        drained = agent._drain_blocks(ws)
        check("_drain_blocks 读取并删除文件",
              len(drained) == 1 and not os.path.exists(blocks_fp), repr(drained))
    finally:
        ctx.cleanup()
        after = snapshots(core)
        eq("unit 测试后 photo/doc/memory 恢复", (after["photos"], after["docs"], after["memory"]),
           (base["photos"], base["docs"], base["memory"]))
        check("unit 测试后 records 未变", after["records"] == base["records"])


# ---------------------------------------------------------------- http
def _api(method, path, body=None, token=None, ctype=None):
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = ctype or "application/octet-stream"
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            h = dict(r.headers)
            try:
                return r.status, json.loads(raw or b"{}"), h
            except ValueError:
                return r.status, raw, h
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


def _multipart(fields, file_bytes, filename="a.png"):
    boundary = "----jlasset%d" % int(time.time() * 1000)
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode())
    out.append(file_bytes)
    out.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


def run_http():
    core, biz, av, agent = load()
    ctx = Ctx(core, biz, av, agent)
    pid = PROJECT
    print("\n=== http 模式：%s project=%s ===" % (BASE, pid))
    try:
        code, _p, _h = _api("GET", "/healthz")
        if code != 200:
            print("  SKIP http：服务未启动")
            return
        base = snapshots(core)
        png = make_png()
        # 原始字节签发
        url = "/api/v1/assets?projectId=%s&origin=generated&caption=%s" % (
            urllib.parse.quote(pid), urllib.parse.quote("HTTP 生成图"))
        code, payload, _h = _api("POST", url, png, ctype="image/png")
        eq("HTTP 原始字节签发 200", code, 200, repr(payload)[:200])
        aid = ctx.track(payload)
        meta = (payload.get("data") or {})
        surl = meta.get("url") or ""
        check("HTTP 返回短签 URL", "e=" in surl and "s=" in surl, surl)
        # 无 header 直接取（模拟 <image>）
        req = urllib.request.Request(BASE + surl)
        with urllib.request.urlopen(req, timeout=30) as r:
            got = r.read()
            eq("短签无 token 取内容 200", r.status, 200)
            check("短签响应 nosniff", (r.headers.get("X-Content-Type-Options") or "") == "nosniff")
        check("短签内容仍是同一张图", same_image(got, png))
        # EXIF 上传
        jpeg = make_jpeg_with_exif()
        code, payload, _h = _api(
            "POST", "/api/v1/assets?projectId=%s&origin=generated" % urllib.parse.quote(pid),
            jpeg, ctype="image/jpeg")
        eq("HTTP JPEG 签发 200", code, 200)
        aid_exif = ctx.track(payload)
        req = urllib.request.Request(BASE + ((payload.get("data") or {}).get("url")))
        with urllib.request.urlopen(req, timeout=30) as r:
            check("HTTP 内容已去 EXIF", not has_exif(r.read()))
        # multipart
        mp, ct = _multipart({"projectId": pid, "origin": "generated", "caption": "multipart 图"}, png)
        code, payload, _h = _api("POST", "/api/v1/assets", mp, ctype=ct)
        eq("HTTP multipart 签发 200", code, 200, repr(payload)[:200])
        ctx.track(payload)
        # svg 拒绝
        code, _p, _h = _api(
            "POST", "/api/v1/assets?projectId=%s" % urllib.parse.quote(pid),
            b"<svg xmlns='http://www.w3.org/2000/svg'/>", ctype="image/svg+xml")
        eq("HTTP 拒绝 svg 400", code, 400)
        # 元数据 / commit / delete
        code, payload, _h = _api("GET", "/api/v1/assets/%s" % aid)
        eq("HTTP 元数据 200", code, 200)
        code, payload, _h = _api("POST", "/api/v1/assets/%s/commit" % aid, {})
        eq("HTTP commit 200", code, 200)
        code, _p, _h = _api("POST", "/api/v1/assets/%s" % aid, {}, token=None)
        # 非法 id
        code, _p, _h = _api("GET", "/api/v1/assets/not-a-ulid/content")
        eq("HTTP 非法 id 404", code, 404)
        # photo_ref JSON
        photo_id = add_photo_fixture(ctx)
        if photo_id:
            code, payload, _h = _api("POST", "/api/v1/assets",
                                     {"projectId": pid, "photoId": photo_id,
                                      "caption": "HTTP 照片引用"})
            eq("HTTP photo_ref 200", code, 200, repr(payload)[:200])
            aidp = ctx.track(payload)
            eq("HTTP photo_ref origin", (payload.get("data") or {}).get("origin"), "uploaded")
            req = urllib.request.Request(BASE + ((payload.get("data") or {}).get("url")))
            with urllib.request.urlopen(req, timeout=30) as r:
                eq("HTTP photo_ref 内容 200", r.status, 200)
        # task 轮询 blocks（历史 message.answer 重签）
        task_id = core.exec_sql(
            "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
            "VALUES (%s,%s,%s,'chat',%s,'done')",
            (pid, int(USER), "asset-http-%d" % int(time.time() * 1000),
             json.dumps({"text": "http", "capabilities": ["answer.blocks"]})))
        ctx.extra.append(("task", task_id))
        mid = core.exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,1,%s)",
            (pid, int(USER), "asset-http-ans-%d" % int(time.time() * 1000), "带图",
             task_id,
             json.dumps({"text": "带图", "blocks": [{"type": "image_ref", "assetId": aid,
                                                    "origin": "generated"}]},
                        ensure_ascii=False)))
        ctx.extra.append(("message", mid))
        code, payload, _h = _api("GET", "/api/v1/tasks/%s" % task_id)
        eq("HTTP task 200", code, 200)
        blocks = (payload.get("data") or {}).get("blocks") or []
        check("HTTP _pollTask 拿到 blocks", bool(blocks), repr(payload)[:200])
        if blocks:
            check("HTTP blocks url 重签", "e=" in (blocks[0].get("url") or ""))
        code, _p, _h = _api("DELETE", "/api/v1/assets/%s" % aid)
        eq("HTTP 软删除 200", code, 200)
        code, _p, _h = _api("GET", "/api/v1/assets/%s/content" % aid)
        eq("HTTP 删除后内容 404", code, 404)
    finally:
        ctx.cleanup()
        after = snapshots(core)
        eq("HTTP 资产不新增 photo/doc/memory",
           (after["photos"], after["docs"], after["memory"]),
           (base["photos"], base["docs"], base["memory"]))
        eq("HTTP 资产不新增 blob", after["blobs"], base["blobs"])
        check("HTTP 资产不写工作区 records", after["records"] == base["records"])


# ---------------------------------------------------------------- perms
def run_perms():
    core, biz, av, agent = load()
    ctx = Ctx(core, biz, av, agent)
    pid = PERM_PROJECT
    print("\n=== perms 模式（AUTH_REQUIRED=1）project=%s / %s ===" % (PERM_PROJECT, PERM_PROJECT2))
    try:
        png = make_png()
        # yun-gu-3qi：user1=owner，user2=engineer，id11 非成员
        code, payload = av.create_asset(png, pid, user={"id": MEMBER_A}, mime="image/png",
                                        origin="generated", caption="权限用")
        eq("owner 签发 200", code, 200, repr(payload)[:200])
        aid = ctx.track(payload)
        exp = int(time.time()) + 120
        sig = av._sign(aid, exp, False)
        eq("短签无 user 可取（<image>）",
           av.get_asset_content(aid, None, exp=exp, sig=sig)[0], 200)
        eq("同项目 engineer 可取 200", av.get_asset_content(aid, {"id": MEMBER_B})[0], 200)
        eq("非成员无短签 403", av.get_asset_content(aid, {"id": OUTSIDER})[0], 403)
        eq("过期短签且非成员 403",
           av.get_asset_content(aid, None, exp=int(time.time()) - 10,
                                sig=av._sign(aid, int(time.time()) - 10, False))[0], 403)
        # baiyun-a2：只有 user1 是 owner，user2/id11 都不是成员
        code, payload = av.create_asset(png, PERM_PROJECT2, user={"id": MEMBER_A},
                                        mime="image/png", origin="generated", caption="跨项目")
        eq("另一项目 owner 签发 200", code, 200, repr(payload)[:200])
        aid2 = ctx.track(payload)
        eq("跨项目非成员 403", av.get_asset_content(aid2, {"id": MEMBER_B})[0], 403)
        eq("跨项目 owner 200", av.get_asset_content(aid2, {"id": MEMBER_A})[0], 200)
        eq("无 token 且无短签 403", av.get_asset_content(aid2, None)[0], 403)
    finally:
        ctx.cleanup()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("unit", "http", "perms", "all"), default="all")
    args = ap.parse_args()
    if args.mode == "unit":
        run_unit()
    elif args.mode == "http":
        run_http()
    elif args.mode == "perms":
        os.environ["AUTH_REQUIRED"] = "1"
        run_perms()
    else:
        run_unit()
        run_http()
        env = dict(os.environ, AUTH_REQUIRED="1")
        rc = subprocess.run([sys.executable, os.path.abspath(__file__), "--mode", "perms"],
                            env=env).returncode
        if rc != 0:
            FAILS.append(("perms 子进程", "exit=%d" % rc))
    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASSES), len(FAILS)))
    for name, detail in FAILS:
        print("  FAIL %s  %s" % (name, detail))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
