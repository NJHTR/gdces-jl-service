#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实照片派生图回归测试：标注图 / 整改前后对比拼版（task-9）。

    cd /home/baize/jl-service
    python3 tools/test-photo-craft.py              # unit + http + perms(AUTH_REQUIRED=1)
    python3 tools/test-photo-craft.py --mode unit
    AUTH_REQUIRED=1 python3 tools/test-photo-craft.py --mode perms

覆盖：源照片正确、photo 表行数不变、records/photos 无新文件、权限/项目隔离、
无 EXIF/GPS、asset 状态 committed、sourceRefs/producer/model/prompt_hash、坐标边界、失败回退。
"""
import argparse
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
BASE = os.environ.get("JL_TEST_BASE") or "http://127.0.0.1:8081"
PROJECT = os.environ.get("JL_TEST_PROJECT") or "test-sandbox"
OTHER = os.environ.get("JL_TEST_OTHER_PROJECT") or "clean-demo-01"
PERM_PROJECT = os.environ.get("JL_TEST_PERM_PROJECT") or "yun-gu-3qi"
PERM_PROJECT2 = os.environ.get("JL_TEST_PERM_PROJECT2") or "baiyun-a2"
USER = os.environ.get("JL_TEST_USER") or "99999"
MEMBER_A, MEMBER_B, OUTSIDER = 1, 2, 11

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
    import bffpkg.photocraft as pc
    return core, biz, av, pc


def has_exif(data):
    return b"Exif\x00\x00" in data


class Ctx(object):
    def __init__(self, core, biz, av, pc):
        self.core, self.biz, self.av, self.pc = core, biz, av, pc
        self.assets = []
        self.extra = []
        av.ensure_asset_table()

    def track(self, payload):
        aid = ((payload or {}).get("data") or {}).get("asset") or {}
        aid = (aid or {}).get("assetId") or ((payload or {}).get("data") or {}).get("assetId")
        if aid:
            self.assets.append(aid)
        return aid

    def cleanup(self):
        core = self.core
        for aid in self.assets:
            try:
                rows = core.q("SELECT project_id, path FROM asset WHERE asset_id=%s", (aid,))
                if rows and rows[0].get("path"):
                    try:
                        os.remove(os.path.join(core.ROOT, rows[0]["path"]))
                    except OSError:
                        pass
                core.exec_sql("DELETE FROM asset WHERE asset_id=%s", (aid,))
                core.exec_sql("DELETE FROM audit_log WHERE target_type='asset' AND target_id=%s",
                              (aid,))
            except Exception:
                traceback.print_exc()
        for table, rid in self.extra:
            try:
                core.exec_sql("DELETE FROM %s WHERE id=%%s" % table, (rid,))
            except Exception:
                traceback.print_exc()
        for pid in (PROJECT, PERM_PROJECT, PERM_PROJECT2):
            d = os.path.join(core.ROOT, "data", "assets", pid)
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass


def snapshots(core, pid):
    with core.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM photo")
        photos = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM doc")
        docs = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) n FROM memory")
        memory = int(cur.fetchone()["n"])
    blobs = sorted(os.listdir(core.BLOB_ROOT)) if os.path.isdir(core.BLOB_ROOT) else []
    rec_root = os.path.join(core.WS_ROOT, pid, "records")
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


def add_photo_fixture(ctx, pid, want=2):
    """借真实 image blob 给测试项目造 photo 行（只测试夹具，不写 blob）。"""
    core, av = ctx.core, ctx.av
    used = 0
    seen = set()
    for p in core.q("SELECT sha256, blob_path FROM photo ORDER BY id"):
        sha = p.get("sha256") or ""
        path = p.get("blob_path") or os.path.join(core.BLOB_ROOT, sha[:2], sha)
        if sha in seen:
            continue
        try:
            with open(path, "rb") as f:
                head = f.read(64)
        except OSError:
            continue
        mime = av.detect_mime(head)
        if not mime or not mime.startswith("image/"):
            continue
        seen.add(sha)
        rid = core.exec_sql(
            "INSERT INTO photo (project_id, sha256, blob_path, src) VALUES (%s,%s,%s,'test')",
            (pid, sha, path))
        ctx.extra.append(("photo", rid))
        used += 1
        if used >= want:
            break
    return [rid for t, rid in ctx.extra if t == "photo"][-used:]


# ---------------------------------------------------------------- unit
def run_unit():
    core, biz, av, pc = load()
    ctx = Ctx(core, biz, av, pc)
    pid = PROJECT
    print("\n=== unit 模式（进程内，AUTH_REQUIRED=%s）project=%s ==="
          % (os.environ.get("AUTH_REQUIRED", "0"), pid))
    base = snapshots(core, pid)
    try:
        photos = add_photo_fixture(ctx, pid, want=2)
        check("夹具照片 >= 2 张", len(photos) >= 2, repr(photos))
        if len(photos) < 2:
            return
        p1, p2 = photos[0], photos[1]
        src_before = snapshots(core, pid)

        # ---- annotate ----
        code, payload = pc.annotate_photo(
            pid, p1, boxes=[{"x": 1, "y": 1, "w": 8, "h": 8, "label": "裂缝"},
                            {"x": -5, "y": -5, "w": 6, "h": 6, "label": "边界"}],
            caption="标注图测试", user={"id": int(USER)})
        eq("annotate 200", code, 200, repr(payload)[:300])
        aid = ctx.track(payload)
        row = av._row(aid) if aid else None
        check("asset committed", bool(row) and row["status"] == "committed", repr(row and row["status"]))
        check("origin=derived", bool(row) and row["origin"] == "derived")
        eq("producer=annotate", (row or {}).get("producer"), "annotate")
        check("model 已记录", bool((row or {}).get("model")))
        check("prompt_hash 已记录", len(str((row or {}).get("prompt_hash") or "")) == 64)
        refs = json.loads(row["source_refs"]) if isinstance(row.get("source_refs"), str) else row.get("source_refs")
        check("sourceRefs 指向源照片", refs == ["photo:%s" % p1], repr(refs))
        path = os.path.join(core.ROOT, row["path"])
        check("派生图只在 data/assets", path.startswith(os.path.join(core.ROOT, "data", "assets", pid))
              and os.path.isfile(path), row["path"])
        with open(path, "rb") as f:
            png = f.read()
        from PIL import Image
        im = Image.open(io.BytesIO(png))
        im.load()
        check("派生图是合法 PNG", im.format == "PNG")
        check("派生图无 EXIF", not has_exif(png))
        block = (payload.get("data") or {}).get("block") or {}
        eq("返回 image_ref block", block.get("type"), "image_ref")
        check("block 带短签 url", "e=" in (block.get("url") or "") and "s=" in (block.get("url") or ""))
        eq("block origin=derived", block.get("origin"), "derived")
        check("block sourceRefs 有 photo", any(str(r).startswith("photo:") for r in (block.get("sourceRefs") or [])))
        got = av.get_asset_content(aid)
        eq("派生图内容 200", got[0], 200)

        # ---- 坐标边界 ----
        code, _ = pc.annotate_photo(pid, p1, boxes=[{"x": 99999, "y": 99999, "w": 10, "h": 10}])
        eq("全部越界 → 400", code, 400)
        code, _ = pc.annotate_photo(pid, p1, boxes=[{"x": 1, "y": 1, "w": -3, "h": -3}])
        eq("负宽高 → 400", code, 400)
        code, _ = pc.annotate_photo(pid, p1, boxes=[{"x": 1, "y": 1, "w": 5, "h": 5}] * 21)
        eq("超过 20 个框 → 400", code, 400)
        code, _ = pc.annotate_photo(pid, 99999999, boxes=[])
        eq("照片不存在 → 404", code, 404)
        with core.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM photo WHERE project_id<>%s LIMIT 1", (pid,))
            other_photo = cur.fetchone()
        if other_photo:
            code, _ = pc.annotate_photo(pid, other_photo["id"], boxes=[])
            eq("他项目照片 → 404", code, 404)

        # ---- 无源/外链/失败回退 ----
        code, payload = pc.annotate_photo(pid, p1, boxes=[], caption="https://evil.example/x")
        eq("外链 caption 仍可生成（caption 被清）", code, 200)
        ctx.track(payload)
        cap = ((payload.get("data") or {}).get("block") or {}).get("caption") or ""
        check("外链 caption 被清空/替换", "evil" not in cap, cap)

        # ---- compare ----
        code, payload = pc.compare_photos(pid, p1, p2, title="整改前后对比测试",
                                          before_label="整改前", after_label="整改后",
                                          user={"id": int(USER)})
        eq("compare 200", code, 200, repr(payload)[:300])
        aid2 = ctx.track(payload)
        row2 = av._row(aid2) if aid2 else None
        check("对比图 committed", bool(row2) and row2["status"] == "committed")
        eq("producer=compare", (row2 or {}).get("producer"), "compare")
        refs2 = json.loads(row2["source_refs"]) if isinstance(row2.get("source_refs"), str) else row2.get("source_refs")
        check("对比图 sourceRefs 两张",
              sorted(refs2 or []) == sorted(["photo:%s" % p1, "photo:%s" % p2]), repr(refs2))
        with open(os.path.join(core.ROOT, row2["path"]), "rb") as f:
            cpng = f.read()
        cim = Image.open(io.BytesIO(cpng))
        cim.load()
        check("对比图是合法 PNG 且无 EXIF", cim.format == "PNG" and not has_exif(cpng))
        check("对比图比单图宽", cim.width > max(Image.open(io.BytesIO(png)).width, 1))
        code, _ = pc.compare_photos(pid, p1, p1)
        eq("同一张照片对比 → 400", code, 400)
        code, _ = pc.compare_photos(pid, p1, 99999999)
        eq("对比缺图 → 404", code, 404)

        # ---- 不写证据 ----
        after = snapshots(core, pid)
        eq("photo 行数不变（不含夹具）", after["photos"], src_before["photos"])
        eq("doc/memory 不变", (after["docs"], after["memory"]),
           (src_before["docs"], src_before["memory"]))
        eq("blob 不新增", after["blobs"], src_before["blobs"])
        check("records/photos 无新文件", after["records"] == src_before["records"])
    finally:
        ctx.cleanup()
        after = snapshots(core, pid)
        eq("测试后 photo/doc/memory 恢复", (after["photos"], after["docs"], after["memory"]),
           (base["photos"], base["docs"], base["memory"]))
        check("测试后 records 未变", after["records"] == base["records"])


# ---------------------------------------------------------------- http
def _api(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


def _get_bytes(url):
    req = urllib.request.Request(BASE + url)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read(), dict(r.headers)


def run_http():
    core, biz, av, pc = load()
    ctx = Ctx(core, biz, av, pc)
    pid = PROJECT
    print("\n=== http 模式：%s project=%s ===" % (BASE, pid))
    base = snapshots(core, pid)
    try:
        code, _p, _h = _api("GET", "/healthz")
        if code != 200:
            print("  SKIP http：服务未启动")
            return
        photos = add_photo_fixture(ctx, pid, want=2)
        if len(photos) < 2:
            print("  SKIP http：缺少图片夹具")
            return
        p1, p2 = photos[0], photos[1]
        src_before = snapshots(core, pid)
        code, payload, _h = _api("POST", "/api/v1/photos/annotate", {
            "projectId": pid, "photoId": p1,
            "boxes": [{"x": 1, "y": 1, "w": 8, "h": 8, "label": "裂缝"}],
            "caption": "HTTP 标注图"})
        eq("HTTP annotate 200", code, 200, repr(payload)[:200])
        aid = ctx.track(payload)
        block = ((payload.get("data") or {}).get("block") or {})
        eq("HTTP block image_ref", block.get("type"), "image_ref")
        st, body, h = _get_bytes(block.get("url") or "")
        eq("HTTP 短签取派生图 200", st, 200)
        eq("HTTP 派生图 mime", h.get("Content-Type"), "image/png")
        check("HTTP 派生图无 EXIF", not has_exif(body))
        code, payload, _h = _api("POST", "/api/v1/photos/compare", {
            "projectId": pid, "beforePhotoId": p1, "afterPhotoId": p2,
            "title": "HTTP 对比图"})
        eq("HTTP compare 200", code, 200, repr(payload)[:200])
        ctx.track(payload)
        code, _p, _h = _api("POST", "/api/v1/photos/annotate", {
            "projectId": pid, "photoId": 99999999, "boxes": []})
        eq("HTTP 照片不存在 404", code, 404)
        code, _p, _h = _api("POST", "/api/v1/photos/compare", {
            "projectId": pid, "beforePhotoId": p1, "afterPhotoId": p1})
        eq("HTTP 同图对比 400", code, 400)
        after = snapshots(core, pid)
        eq("HTTP 不写 photo/doc/memory",
           (after["photos"], after["docs"], after["memory"]),
           (src_before["photos"], src_before["docs"], src_before["memory"]))
        eq("HTTP 不新增 blob", after["blobs"], src_before["blobs"])
        check("HTTP records/photos 无新文件", after["records"] == src_before["records"])
    finally:
        ctx.cleanup()
        after = snapshots(core, pid)
        eq("HTTP 测试后 photo/doc/memory 恢复", (after["photos"], after["docs"], after["memory"]),
           (base["photos"], base["docs"], base["memory"]))


# ---------------------------------------------------------------- perms
def run_perms():
    core, biz, av, pc = load()
    ctx = Ctx(core, biz, av, pc)
    pid = PERM_PROJECT
    print("\n=== perms 模式（AUTH_REQUIRED=1）project=%s ===" % pid)
    try:
        # 真实照片：yun-gu-3qi id=45 / 44（本项目真实照片，不复制）
        code, payload = pc.annotate_photo(pid, 45, boxes=[], user={"id": MEMBER_A})
        eq("owner 标注本项目照片 200", code, 200)
        ctx.track(payload)
        if code != 200:
            return
        code, payload = pc.annotate_photo(pid, 45, boxes=[], user={"id": MEMBER_B})
        eq("同项目 engineer 可标注 200", code, 200)
        ctx.track(payload)
        code, _ = pc.annotate_photo(pid, 45, boxes=[], user={"id": OUTSIDER})
        eq("非项目成员 403", code, 403)
        code, _ = pc.annotate_photo(PERM_PROJECT2, 45, boxes=[], user={"id": MEMBER_A})
        eq("跨项目用别人照片 404", code, 404)
        code, payload = pc.compare_photos(pid, 45, 44, title="权限对比",
                                          user={"id": MEMBER_A})
        eq("owner 对比两张本项目照片 200", code, 200, repr(payload)[:200])
        ctx.track(payload)
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
