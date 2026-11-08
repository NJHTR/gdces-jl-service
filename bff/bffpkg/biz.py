import json
import os
import queue
import re
import subprocess
import urllib.error
import urllib.request
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import pymysql

"""业务域：文书 · 项目 · 成员 · 审计 · 规范 · 统计。

这一层是「真值和规则」：状态机、字段校验、权限。
未来抽成 Java 服务时，把这里的实现换掉即可，接口不变。
"""
from .core import (ADMIN_HTML, HOMES_ROOT, ALLOWED_TOOLS, AUTH_REQUIRED, BLOB_ROOT, BOLD_RE, ENGINE, ENGINE_CLIENT, ENGINE_ENV, ENGINE_MODE, ENV, GAP_INLINE_RE, GAP_SPLIT_RE, JL_TOOL_BIN, KIND_TO_ICON, KIND_TO_TINT, LEARN_PROMPT, LLM_MODEL, LLM_URL, MAX_CONCURRENT, NOTIFY, NUM_RE, PORT, REF_RE, ROOT, STATE_TO_ST, SYSTEM_PROMPT, TOKEN_TTL_HOURS, TURN_SLOTS, WEEK, WS_ROOT, _USER_LOCKS, _USER_LOCKS_GUARD, _bigrams, _day_label, _hash_token, _rerender_if_project, _split_num, allowed_tools, bearer, build_answer, clean_reply, db, describe_tool, exec_sql, extract_from_items, get_project, handle_login, handle_logout, issue_token, llm, load_env, ms_label, notify_worker, on_notification, parse_step, q, q1, resolve_token, rich_reply, rich_segments, run_tool, similar, split_gaps, transcribe_audio, materialize_images, validate_fields, load_schema, list_schemas)


# ---------------------------------------------------------------- handle_project_list
def handle_project_list(params, user=None):
    """项目列表：带上建设单位、消息数，给切换面板用"""
    rows = q("SELECT id, meta, name, code, addr, status, created_at FROM project "
             "ORDER BY created_at DESC")
    out = []
    for r in rows:
        if not AUTH_REQUIRED or member_of(r["id"], user):
            meta = r.get("meta") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            cnt = q("SELECT COUNT(*) AS n FROM message WHERE project_id=%s", (r["id"],))
            out.append({
                "id": r["id"], "name": r["name"], "code": r["code"], "addr": r["addr"],
                "builder": meta.get("builder") or "",
                "msgs": cnt[0]["n"] if cnt else 0,
            })
    return 200, {"code": 0, "data": out, "list": out}


# ---------------------------------------------------------------- handle_project_create
def handle_project_create(body):
    pid = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    code = (body.get("code") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", pid):
        return 400, {"code": 400, "message": "id 只允许字母数字和短横线（1-32 位）"}
    if not name:
        return 400, {"code": 400, "message": "name 必填"}
    if q("SELECT id FROM project WHERE id=%s", (pid,)):
        return 409, {"code": 409, "message": "项目已存在"}

    script = f"{ROOT}/repos/jl-service/scripts/init-project.sh"
    r = subprocess.run(["bash", script, pid, name, code], capture_output=True, text=True)
    if r.returncode != 0:
        return 500, {"code": 500, "message": f"建工作区失败: {r.stderr[:400]}"}

    meta = {"id": pid, "name": name, "code": code}
    exec_sql("INSERT INTO project (id, tenant_id, name, code, addr, meta, ws_path) "
             "VALUES (%s,1,%s,%s,%s,%s,%s)",
             (pid, name, code or None, body.get("addr"),
              json.dumps(meta, ensure_ascii=False), f"{WS_ROOT}/{pid}"))
    return 200, {"code": 0, "data": {"id": pid, "ws": f"{WS_ROOT}/{pid}"}}


# ---------------------------------------------------------------- handle_admin
def handle_admin():
    return 200, ADMIN_HTML, "text/html; charset=utf-8"


# ---------------------------------------------------------------- render_project_memory
WS_GITIGNORE = """# 每轮临时产物：引用账本、轮次标记、最后输出、临时文件
.agent/
*.tmp
tmp/
"""


def ensure_ws_gitignore(project_id):
    """给工作区补 .gitignore，并把已经提交过的 .agent/ 从索引里移除（幂等）。

    两个坑：
    · .gitignore 只影响未跟踪文件——以前提交过的 .agent/*.json 仍在索引里
    · 不能"文件内容一样就早退"：那样永远走不到取消跟踪那一步（实测踩过）
    """
    ws = f"{WS_ROOT}/{project_id}"
    path = os.path.join(ws, ".gitignore")
    try:
        os.makedirs(ws, exist_ok=True)
        old = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old != WS_GITIGNORE:
            with open(path, "w", encoding="utf-8") as f:
                f.write(WS_GITIGNORE)
    except OSError as e:
        sys.stderr.write("[ws] 写 .gitignore 失败 %s: %r\n" % (ws, e))
        return None
    try:
        # 无论 .gitignore 有没有变，都确保索引里没有 .agent/
        subprocess.run(["git", "rm", "-r", "--cached", "--quiet", ".agent"],
                       cwd=ws, capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "add", "--", ".gitignore"], cwd=ws,
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "commit", "-m",
                        "chore(工作区): 忽略并取消跟踪每轮临时产物"],
                       cwd=ws, capture_output=True, text=True, timeout=60)
    except Exception as e:
        sys.stderr.write("[ws] 提交 .gitignore 失败: %r\n" % (e,))
    return ".gitignore"


# ---------------------------------------------------------------- render_project_memory
WS_GITIGNORE = """# 每轮临时产物：引用账本、轮次标记、最后输出、临时文件
.agent/
*.tmp
tmp/
"""


def ensure_ws_gitignore(project_id):
    """给工作区补 .gitignore（幂等）。

    每轮都会在 .agent/ 下写 cites.jsonl、turn-*.json、last-*.md；
    没有 ignore 时 `git add -A` 会把这些每轮都变的临时文件提交进去。
    """
    ws = f"{WS_ROOT}/{project_id}"
    path = os.path.join(ws, ".gitignore")
    try:
        os.makedirs(ws, exist_ok=True)
        old = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old == WS_GITIGNORE:
            return ".gitignore"
        with open(path, "w", encoding="utf-8") as f:
            f.write(WS_GITIGNORE)
    except OSError as e:
        sys.stderr.write("[ws] 写 .gitignore 失败 %s: %r\n" % (ws, e))
        return None
    try:
        # 只加 .gitignore 还不够：以前已经提交过的 .agent/ 文件仍被跟踪
        # （gitignore 只影响未跟踪文件），要显式从索引里移除
        subprocess.run(["git", "rm", "-r", "--cached", "--quiet", ".agent"],
                       cwd=ws, capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "add", "--", ".gitignore"], cwd=ws,
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "commit", "-m", "chore(工作区): 忽略并取消跟踪每轮临时产物"],
                       cwd=ws, capture_output=True, text=True, timeout=60)
    except Exception as e:
        sys.stderr.write("[ws] 提交 .gitignore 失败: %r\n" % (e,))
    return ".gitignore"


def render_project_memory(project_id):
    """重刷项目工作区的 AGENTS.md（项目规矩只有一个文件：records/rules.md）。

    以前这里把 DB 记忆渲染成 memories/project.md —— 那是同一批知识的第三份副本。
    现在删掉那条路径：DB 只当索引（谁在什么时候说过），知识在文件里、由 git 版本化。
    """
    from .agent import render_agents_md          # biz ← agent 只在调用时导入，避免环

    ws = f"{WS_ROOT}/{project_id}"
    ensure_ws_gitignore(project_id)
    removed = None
    stale = f"{ws}/memories/project.md"
    if os.path.exists(stale):
        try:
            os.remove(stale)
            removed = "memories/project.md"
        except OSError as e:
            sys.stderr.write("[agents] 删旧副本失败 %s: %r\n" % (stale, e))

    ctx = render_agents_md(project_id, 1)

    subprocess.run(["git", "add", "--", "AGENTS.md", "records/rules.md"],
                   cwd=ws, capture_output=True, text=True)
    msg = "docs(项目): 刷新 AGENTS.md 与项目规矩\n\n来源: project 表（事实） + records/rules.md"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ws, capture_output=True, text=True)
    commit = None
    if r.returncode == 0:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws,
                                capture_output=True, text=True).stdout.strip()
    elif "nothing to commit" not in (r.stdout + r.stderr):
        sys.stderr.write("[agents] 提交失败 %s: %s\n"
                         % (project_id, (r.stdout + r.stderr)[:200]))

    return {"path": "AGENTS.md", "bytes": len(ctx.encode("utf-8")),
            "commit": (commit or "")[:10], "removed": removed}


# ---------------------------------------------------------------- store_blob
def store_blob(data):
    """内容寻址：sha256 前两位做目录，天然去重"""
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    sub = sha[:2]
    d = os.path.join(BLOB_ROOT, sub)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sha)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return sha, path, len(data)


# ---------------------------------------------------------------- append_manifest
def append_manifest(project_id, entry):
    """把照片写进工作区的 manifest.json，并提交"""
    rel = "photos/manifest.json"
    abs_path = f"{WS_ROOT}/{project_id}/{rel}"
    try:
        with open(abs_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        manifest = {"projectId": project_id, "photos": []}

    manifest.setdefault("photos", []).append(entry)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)   # 新项目第一张照片会缺 photos/ 目录
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    ws = f"{WS_ROOT}/{project_id}"
    subprocess.run(["git", "add", "--", rel], cwd=ws, capture_output=True, text=True)
    msg = (f"record(photo): 新增现场影像 {entry['sha256'][:10]}\n\n"
           f"依据: photo_{entry['sha256'][:10]}\n项目: {project_id}")
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ws, capture_output=True, text=True)
    commit = None
    if r.returncode == 0:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws,
                                capture_output=True, text=True).stdout.strip()
    return commit


# ---------------------------------------------------------------- handle_upload
def handle_upload(project_id, params, data, user=None):
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    if not data:
        return 400, {"code": 400, "message": "空文件"}
    if len(data) > 20 * 1024 * 1024:
        return 413, {"code": 413, "message": "超过 20MB"}

    deny = guard_project(project_id, user)
    if deny:
        return deny

    sha, blob_path, size = store_blob(data)
    # params 里已经合并了 query + 表单字段（见 api.py）
    place = q1(params, "place")
    weather = q1(params, "weather")
    shot_at = q1(params, "shotAt")
    src = q1(params, "src", "camera")
    # 证据来源：谁拍的、在哪、地址怎么来的、天气怎么来的、哪些字段是人工改的
    lat = q1(params, "lat")
    lng = q1(params, "lng")
    address = q1(params, "address")
    address_source = q1(params, "addressSource", "gps")
    weather_source = q1(params, "weatherSource", "api")
    note = q1(params, "note")
    work = q1(params, "work")
    edited = q1(params, "edited")
    captured_by = (user or {}).get("id") or q1(params, "capturedBy")
    device_time = q1(params, "deviceTime")

    exists = q("SELECT id, blob_path FROM photo WHERE project_id=%s AND sha256=%s", (project_id, sha))
    if exists:
        return 200, {"code": 0, "data": {"duplicated": True, "photoId": exists[0]["id"],
                                        "sha256": sha, "size": size}}

    def _num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    photo_id = exec_sql(
        "INSERT INTO photo (project_id, sha256, blob_path, place, weather, shot_at, src, "
        "lat, lng, address, address_source, weather_source, note, work, edited, "
        "captured_by, device_time) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (project_id, sha, blob_path, place, weather, shot_at, src,
         _num(lat), _num(lng), address, address_source, weather_source, note, work,
         edited, captured_by, device_time))

    entry = {"sha256": sha, "path": f"blobs/{sha[:2]}/{sha}", "size": size,
             "takenAt": shot_at or str(time.strftime("%Y-%m-%d %H:%M:%S")),
             "place": place, "weather": weather, "src": src,
             "address": address, "addressSource": address_source,
             "weatherSource": weather_source, "note": note, "work": work,
             "edited": edited, "capturedBy": captured_by}
    commit = append_manifest(project_id, entry)
    if commit:
        exec_sql("UPDATE photo SET git_commit=%s WHERE id=%s", (commit, photo_id))

    # 上传即刷新影像索引：否则 AI 读 records/photos.md 会以为项目里没有照片
    try:
        render_photos_md(project_id)
    except Exception as e:
        sys.stderr.write("[photos] 上传后刷新索引失败: %r\n" % (e,))

    return 200, {"code": 0, "data": {"photoId": photo_id, "sha256": sha, "size": size,
                                    "commit": commit, "path": entry["path"]}}


# ---------------------------------------------------------------- handle_photo_list
def handle_photo_list(params):
    project_id = q1(params, "projectId")
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    limit = min(int(q1(params, "limit", 20)), 100)
    rows = q("SELECT id, sha256, blob_path, place, weather, shot_at, src, git_commit, caption "
             "FROM photo WHERE project_id=%s ORDER BY id DESC LIMIT %s", (project_id, limit))
    for r in rows:
        r["url"] = f"/api/v1/blobs/{r['sha256']}"
    return 200, {"code": 0, "data": rows}


# ---------------------------------------------------------------- handle_blob
def handle_blob(sha):
    import re as _re
    if not _re.match(r"^[a-f0-9]{64}$", sha or ""):
        return 404, {"code": 404, "message": "bad sha"}
    path = os.path.join(BLOB_ROOT, sha[:2], sha)
    if not os.path.exists(path):
        return 404, {"code": 404, "message": "not found"}
    with open(path, "rb") as f:
        data = f.read()
    return 200, data, "application/octet-stream"


# ---------------------------------------------------------------- default_project_id
def default_project_id():
    rows = q("SELECT id FROM project ORDER BY created_at LIMIT 1")
    return rows[0]["id"] if rows else None


def render_photos_md(project_id):
    """把影像索引渲染成 <ws>/records/photos.md —— 按部位分组，一图一行。

    以前是"写完描述追加一行"：同一张图换个描述就多一行、部位没标的堆一堆，
    根本没法回答"这个部位以前查过什么"。现在 DB 是账本，文件是视图（同 rules.md）。
    """
    ws = f"{WS_ROOT}/{project_id}"
    rows = q("SELECT sha256, place, caption, note, work, shot_at, created_at FROM photo "
             "WHERE project_id=%s ORDER BY COALESCE(shot_at, created_at)", (project_id,))
    latest = {}
    for r in rows:
        latest[r["sha256"]] = r
    groups = {}
    for sha, r in latest.items():
        place = (r.get("place") or "").strip() or "未标部位"
        groups.setdefault(place, []).append((sha, r))
    lines = ["# 现场影像索引（按部位）", "",
             "> 一条一张图，按部位分组；原图取 /api/v1/blobs/<sha>。",
             "> 由服务从数据库渲染，不要手改。", ""]
    for place in sorted(groups):
        items = sorted(groups[place],
                       key=lambda x: str(x[1].get("shot_at") or x[1].get("created_at") or ""))
        lines.append("## %s（%d 张）" % (place, len(items)))
        for sha, r in items:
            at = str(r.get("shot_at") or r.get("created_at") or "")[:16].replace("T", " ")
            cap = (r.get("caption") or r.get("note") or r.get("work") or "").strip().replace("\n", " ")[:160]
            lines.append("- %s · %s · sha %s" % (at, cap or "（未描述）", sha[:12]))
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    path = os.path.join(ws, "records", "photos.md")
    rel = "records/photos.md"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old == body:
            return rel
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError as e:
        sys.stderr.write("[photos] 写索引失败 %s: %r\n" % (path, e))
        return None
    try:
        subprocess.run(["git", "add", "--", rel], cwd=ws,
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "commit", "-m",
                        "docs(证据): 重渲染影像索引（%d 张）" % len(latest)],
                       cwd=ws, capture_output=True, text=True, timeout=60)
    except Exception as e:
        sys.stderr.write("[photos] git 提交失败: %r\n" % (e,))
    return rel


def index_photo(project_id, sha, caption, place=None):
    """兼容旧调用：写完描述后重渲染整份索引（不再逐行追加）。"""
    if not project_id:
        return None
    return render_photos_md(project_id)


def handle_photo_index(params, user=None):
    """按部位分组的影像索引：这个部位拍过几张、最近一次、描述样例。"""
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    rows = q("SELECT sha256, place, caption, shot_at, created_at FROM photo "
             "WHERE project_id=%s AND (COALESCE(caption,'')<>'' OR COALESCE(place,'')<>'') "
             "ORDER BY COALESCE(shot_at, created_at)", (pid,))
    groups = {}
    for r in rows:
        place = (r.get("place") or "").strip() or "未标部位"
        g = groups.setdefault(place, {"place": place, "count": 0,
                                      "firstAt": None, "lastAt": None, "samples": []})
        g["count"] += 1
        at = str(r.get("shot_at") or r.get("created_at") or "")[:19].replace("T", " ")
        if not g["firstAt"] or at < g["firstAt"]:
            g["firstAt"] = at
        if not g["lastAt"] or at > g["lastAt"]:
            g["lastAt"] = at
        if len(g["samples"]) < 3:
            g["samples"].append({"sha": (r.get("sha256") or "")[:12], "at": at,
                                 "caption": (r.get("caption") or "")[:120]})
    out = sorted(groups.values(), key=lambda x: -x["count"])
    return 200, {"code": 0, "data": {"places": out, "total": len(rows)}}


# ---------------------------------------------------------------- parse_multipart_full
def parse_multipart_full(body, content_type):
    """解析 multipart/form-data，返回 (文件字节, 字段字典)。

    wx.uploadFile 的 projectId 之类是放在 formData 里的（不在 query 上），
    所以字段必须解析出来；文件取第一个带 filename 的 part。
    """
    import re as _re

    m = _re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return b"", {}
    boundary = (m.group(1) or m.group(2)).strip()
    sep = b"--" + boundary.encode()
    data_file, fields = b"", {}
    for part in body.split(sep):
        idx = part.find(b"\r\n\r\n")
        if idx < 0:
            continue
        head = part[:idx].decode("utf-8", "replace")
        value = part[idx + 4:]
        if value.endswith(b"\r\n"):
            value = value[:-2]
        name = ""
        for chunk in head.split(";"):
            chunk = chunk.strip()
            if chunk.startswith('name="'):
                name = chunk[6:].rstrip('"')
                break
        if not name:
            continue
        if "filename=" in head:
            if name == "file" or b"filename=" in head.encode():
                data_file = value
        else:
            try:
                fields[name] = value.decode("utf-8")
            except Exception:
                fields[name] = ""
    return data_file, fields


# ---------------------------------------------------------------- handle_project_get
def handle_project_get(project_id):
    pid = project_id or default_project_id()
    if not pid:
        return 404, {"code": 404, "message": "还没有项目"}
    proj = get_project(pid)
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    view = dict(meta)
    view.update({"id": proj["id"], "name": proj["name"],
                 "code": proj.get("code") or "", "addr": proj.get("addr") or ""})
    return 200, {"code": 0, "data": view}


# ---------------------------------------------------------------- handle_project_save
def handle_project_save(body, user=None):
    deny = guard_project((body.get("id") or "").strip(), user)
    if deny:
        return deny
    pid = body.get("id") or body.get("projectId") or default_project_id()
    if not pid:
        return 404, {"code": 404, "message": "还没有项目"}
    # 合并而不是覆盖：客户端只传改动字段时，不能把其它字段抹掉
    old = get_project(pid).get("meta") or {}
    if isinstance(old, str):
        try:
            old = json.loads(old)
        except Exception:
            old = {}
    meta = dict(old)
    meta.update({k: v for k, v in body.items() if k not in ("id", "projectId")})
    proj = get_project(pid)
    exec_sql("UPDATE project SET name=%s, code=%s, addr=%s, meta=%s WHERE id=%s",
             (body.get("name") or proj["name"],
              body.get("code") or proj.get("code"),
              body.get("addr") or proj.get("addr"),
              json.dumps(meta, ensure_ascii=False), pid))
    # meta.json 也同步进工作区并提交
    try:
        import pathlib
        ws = f"{WS_ROOT}/{pid}"
        pathlib.Path(f"{ws}/meta.json").write_text(
            json.dumps(dict(meta, id=pid), ensure_ascii=False, indent=2), encoding="utf-8")
        subprocess.run(["git", "add", "--", "meta.json"], cwd=ws, capture_output=True)
        subprocess.run(["git", "commit", "-m",
                        f"meta(project): 更新工程概况\n\n依据: project_form\n项目: {pid}"],
                       cwd=ws, capture_output=True)
    except Exception:
        traceback.print_exc()
    return 200, {"code": 0, "data": {"ok": True, "id": pid}}


# ---------------------------------------------------------------- handle_docs_list
def handle_docs_list(project_id, user=None):
    pid = project_id or default_project_id()
    if not pid:
        return 200, {"code": 0, "data": {"list": [], "total": 0}}
    deny = guard_project(pid, user)
    if deny:
        return deny
    rows = q("SELECT id, kind, obj, state, path, git_commit, created_at FROM doc "
             "WHERE project_id=%s ORDER BY created_at DESC LIMIT 100", (pid,))
    for r in rows:
        r["createdAt"] = str(r.pop("created_at", ""))
    return 200, {"code": 0, "data": {"list": rows, "total": len(rows)}}


# ---------------------------------------------------------------- handle_geo_reverse
_GEO_CACHE = {}


def _project_addr(pid):
    proj = get_project(pid) if pid else {}
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return proj.get("addr") or meta.get("addr") or ""


def handle_geo_reverse(params):
    """坐标 → 中文地址（BigDataCloud 逆地理，无 key）。

    取不到就回项目地址，并明确 source=project，绝不把项目地址伪装成 GPS 地址。
    """
    import urllib.parse
    import urllib.request

    pid = q1(params, "projectId") or default_project_id()
    lat = (q1(params, "lat") or "").strip()
    lng = (q1(params, "lng") or "").strip()
    fallback = _project_addr(pid) or "当前位置"
    if not lat or not lng:
        return 200, {"code": 0, "data": {"address": fallback, "source": "project"}}

    key = lat + "," + lng
    hit = _GEO_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return 200, {"code": 0, "data": hit[1]}

    url = ("https://api.bigdatacloud.net/data/reverse-geocode-client?"
           + urllib.parse.urlencode({
               "latitude": lat, "longitude": lng, "localityLanguage": "zh",
           }))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jl-service/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = json.loads(r.read().decode("utf-8"))
        parts = []
        for k in ("principalSubdivision", "city", "locality"):
            v = (raw.get(k) or "").strip()
            if v and v not in "".join(parts):
                parts.append(v)
        address = "".join(parts) or fallback
        data = {
            "address": address,
            "province": raw.get("principalSubdivision") or "",
            "city": raw.get("city") or "",
            "district": raw.get("locality") or "",
            "postcode": raw.get("postcode") or "",
            "plusCode": raw.get("plusCode") or "",
            "source": "bigdatacloud" if parts else "project",
        }
        _GEO_CACHE[key] = (time.time(), data)
        return 200, {"code": 0, "data": data}
    except Exception as exc:
        sys.stderr.write("[geo] 逆地理失败 %s,%s: %r\n" % (lat, lng, exc))
        return 200, {"code": 0, "data": {"address": fallback, "source": "project"}}


# ---------------------------------------------------------------- handle_weather
# WMO 天气代码 → 中文（Open-Meteo 用的就是这套码）
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷雨伴冰雹",
}
_WIND_DIR = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
_WX_CACHE = {}


def _wind_dir_text(deg):
    try:
        d = float(deg) % 360
        return _WIND_DIR[int((d + 22.5) // 45) % 8] + "风"
    except Exception:
        return ""


def _wind_level(kmh):
    """km/h → 大致风力级（只用于水印，不用于气象结论）。"""
    try:
        v = float(kmh)
    except Exception:
        return ""
    for i, top in enumerate([1, 5, 11, 19, 28, 38, 49, 61, 74, 88, 102, 117], 0):
        if v <= top:
            return str(i) + "级"
    return "12级"


def handle_weather(params):
    """真实天气：Open-Meteo（无需 key）。

    为什么不再返回假数据：这个天气会被烧进照片水印，而照片是证据材料。
    宁可不显示，也不能编一个「多云 26°C」。
    取不到就回空字符串，前端自然不画这一行。
    """
    import urllib.parse
    import urllib.request

    lat = (q1(params, "lat") or "").strip()
    lng = (q1(params, "lng") or "").strip()
    if not lat or not lng:
        return 200, {"code": 0, "data": {"text": "", "temp": None,
                                         "source": "no-coords"}}
    key = lat + "," + lng
    hit = _WX_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:          # 10 分钟缓存
        return 200, {"code": 0, "data": hit[1]}

    url = ("https://api.open-meteo.com/v1/forecast?"
           + urllib.parse.urlencode({
               "latitude": lat, "longitude": lng,
               "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                          "precipitation,weather_code,cloud_cover,pressure_msl,"
                          "wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility",
               "timezone": "Asia/Shanghai",
           }))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "jl-service/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = json.loads(r.read().decode("utf-8"))
        cur = raw.get("current") or {}
        code = cur.get("weather_code")
        temp = cur.get("temperature_2m")
        num = lambda v: (int(round(v)) if isinstance(v, (int, float)) else None)
        data = {
            "text": WMO_TEXT.get(int(code), "") if code is not None else "",
            "temp": num(temp),
            "humidity": num(cur.get("relative_humidity_2m")),
            "apparent": num(cur.get("apparent_temperature")),
            "precipitation": cur.get("precipitation"),
            "wind_speed": cur.get("wind_speed_10m"),
            "wind_dir": cur.get("wind_direction_10m"),
            "wind_dir_text": _wind_dir_text(cur.get("wind_direction_10m")),
            "wind_level": _wind_level(cur.get("wind_speed_10m")),
            "cloud": num(cur.get("cloud_cover")),
            "pressure": cur.get("pressure_msl"),
            "visibility": cur.get("visibility"),
            "source": "open-meteo",
            "at": cur.get("time") or "",
        }
        _WX_CACHE[key] = (time.time(), data)
        return 200, {"code": 0, "data": data}
    except Exception as exc:
        sys.stderr.write("[weather] 取天气失败 %s,%s: %r\n" % (lat, lng, exc))
        return 200, {"code": 0, "data": {"text": "", "temp": None, "source": "error"}}


# ---------------------------------------------------------------- audit
def audit(project_id, actor_id, action, target, detail=""):
    """写一条审计：谁、在哪个项目、做了什么、对哪条记录"""
    ttype, _, tid = (target or "").partition(":")
    try:
        exec_sql(
            "INSERT INTO audit_log (actor_id, project_id, action, target_type, target_id, after) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (int(actor_id or 1), project_id, action, ttype or "misc", tid[:64] or None,
             json.dumps({"detail": detail}, ensure_ascii=False) if detail else None))
    except Exception as e:
        sys.stderr.write(f"[audit] 写失败 {action}: {e}\n")




# ---------------------------------------------------------------- handle_asr
def handle_asr(body, user=None):
    """把已上传的音频转成文字。

    前端流程：先 /upload 拿 sha，再调这里换文字。
    """
    sha = (body.get("blob") or body.get("sha") or "").strip()
    if not sha:
        return 400, {"code": 400, "message": "blob 必填"}

    # 项目归属：优先用传上来的 projectId，没有就按 sha 反查；都查不到就拒绝。
    # 以前完全没有守卫 —— 知道 sha 就能跨项目转写并写入别人项目的影像索引。
    pid = (body.get("projectId") or "").strip()
    if not pid:
        try:
            _r = q("SELECT project_id FROM photo WHERE sha256=%s ORDER BY id DESC LIMIT 1", (sha,))
            pid = (_r[0].get("project_id") if _r else "") or ""
        except Exception as e:
            sys.stderr.write("[asr] 反查项目失败 %s: %r\n" % (sha[:8], e))
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填（按 sha 找不到所属项目）"}
    deny = guard_project(pid, user)
    if deny:
        return deny

    text, err = transcribe_audio(sha)
    if err and text is None:
        return 503, {"code": 503, "message": err}

    # 沉淀：转写结果写进这条音频的描述里，否则"说过的"跟"拍过的"一样，读完就没了。
    # 描述是数据不是判断 —— 后端只负责存，怎么用由 agent 决定。
    saved = False
    if (text or "").strip():
        rows = q("SELECT id, caption, project_id, place FROM photo "
                 "WHERE project_id=%s AND sha256=%s ORDER BY id DESC LIMIT 1", (pid, sha))
        if rows and not (rows[0].get("caption") or "").strip():
            cap = "语音：" + text.strip()[:480]
            exec_sql("UPDATE photo SET caption=%s, caption_at=NOW(3) WHERE id=%s",
                     (cap, rows[0]["id"]))
            index_photo(pid, sha, cap)
            saved = True

    return 200, {"code": 0, "data": {"text": text or "", "note": err or "",
                                     "saved": saved}}

# ---------------------------------------------------------------- 复核复议（F4）
def handle_review_appeal(review_id, body, user=None):
    """对一条 must_fix 复核申请复议。**一次、有界**：同一条复核只能申请一次。"""
    rows = q("SELECT id, doc_id, project_id, verdict FROM review WHERE id=%s", (review_id,))
    if not rows:
        return 404, {"code": 404, "message": "复核记录不存在"}
    r = rows[0]
    deny = guard_project(r["project_id"], user)
    if deny:
        return deny
    if r["verdict"] != "must_fix":
        return 400, {"code": 400,
                     "message": "只有 must_fix 的复核可以复议（当前 %s）" % r["verdict"]}
    reason = (body.get("reason") or "").strip()
    if not reason:
        return 400, {"code": 400, "message": "reason 必填（要说清为什么不同意）"}
    old = q("SELECT id, status FROM review_appeal WHERE review_id=%s", (review_id,))
    if old:
        return 409, {"code": 409,
                     "message": "这条复核已经复议过一次了（%s）" % old[0]["status"]}
    aid = exec_sql(
        "INSERT INTO review_appeal (review_id, doc_id, project_id, user_id, reason) "
        "VALUES (%s,%s,%s,%s,%s)",
        (review_id, r["doc_id"], r["project_id"],
         str((user or {}).get("id") or ""), reason[:500]))
    return 200, {"code": 0, "data": {"id": aid, "status": "pending"}}


def handle_review_appeals(review_id, user=None):
    rows = q("SELECT id, doc_id, project_id FROM review WHERE id=%s", (review_id,))
    if not rows:
        return 404, {"code": 404, "message": "复核记录不存在"}
    deny = guard_project(rows[0]["project_id"], user)
    if deny:
        return deny
    out = q("SELECT id, review_id, doc_id, reason, status, decision_note, "
            "decided_by, decided_at, created_at FROM review_appeal "
            "WHERE review_id=%s ORDER BY id", (review_id,))
    return 200, {"code": 0, "data": out}


def handle_appeal_decide(appeal_id, body, user=None):
    """裁定复议：维持（uphold）或推翻（overturn）。裁定后不可再改。"""
    rows = q("SELECT id, review_id, project_id, status FROM review_appeal WHERE id=%s",
             (appeal_id,))
    if not rows:
        return 404, {"code": 404, "message": "复议不存在"}
    a = rows[0]
    deny = guard_project(a["project_id"], user)
    if deny:
        return deny
    if a["status"] != "pending":
        return 409, {"code": 409, "message": "这条复议已经裁定过了（%s）" % a["status"]}
    action = (body.get("action") or "").strip()
    if action not in ("uphold", "overturn"):
        return 400, {"code": 400, "message": "action 只能是 uphold / overturn"}
    status = "upheld" if action == "uphold" else "overturned"
    note = (body.get("note") or "").strip()[:500] or None
    exec_sql("UPDATE review_appeal SET status=%s, decision_note=%s, decided_by=%s, "
             "decided_at=NOW(3) WHERE id=%s",
             (status, note, str((user or {}).get("id") or ""), appeal_id))
    return 200, {"code": 0, "data": {"id": appeal_id, "status": status}}


# ---------------------------------------------------------------- 记忆指标（C6）
def handle_memory_metrics(params, user=None):
    """记忆用起来没有：主动写 / 复用 / 撤销率 / 提案确认率。

    口径写在这（免得以后各说各话）：
    · 主动写 = source in ('manual','confirmed')（人写的、人确认的）
    · 复用 = SUM(hits)（检索命中与被引用都记 hits）
    · 撤销率 = 停用条数 / 总条数
    · 确认率 = accepted / (accepted + rejected)；没有已决提案时返回 null
    """
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny

    t = q("SELECT COUNT(*) AS n, COALESCE(SUM(status=1),0) AS active, "
          "COALESCE(SUM(hits),0) AS uses, "
          "COALESCE(SUM(source IN ('manual','confirmed')),0) AS human "
          "FROM memory WHERE scope='project' AND owner_id=%s", (pid,))[0]
    n = int(t["n"] or 0)
    active = int(t["active"] or 0)
    by_week = q("SELECT YEARWEEK(created_at,3) AS yw, COUNT(*) AS entries, "
                "COALESCE(SUM(source IN ('manual','confirmed')),0) AS human, "
                "COALESCE(SUM(hits),0) AS uses "
                "FROM memory WHERE scope='project' AND owner_id=%s "
                "AND created_at >= DATE_SUB(NOW(), INTERVAL 8 WEEK) "
                "GROUP BY yw ORDER BY yw", (pid,))
    pr = q("SELECT COALESCE(SUM(status='accepted'),0) AS a, "
           "COALESCE(SUM(status='rejected'),0) AS r, "
           "COALESCE(SUM(status='pending'),0) AS p "
           "FROM rule_proposal WHERE project_id=%s", (pid,))[0]
    a, r, p = int(pr["a"] or 0), int(pr["r"] or 0), int(pr["p"] or 0)
    return 200, {"code": 0, "data": {
        "projectId": pid,
        "totals": {
            "entries": n, "active": active, "disabled": n - active,
            "revokeRate": round((n - active) / n, 3) if n else 0.0,
            "recallUses": int(t["uses"] or 0),
            "humanWrites": int(t["human"] or 0),
        },
        "byWeek": [{"week": str(w["yw"]), "entries": int(w["entries"]),
                    "humanWrites": int(w["human"]), "uses": int(w["uses"])}
                   for w in by_week],
        "proposals": {"pending": p, "accepted": a, "rejected": r,
                      "acceptRate": round(a / (a + r), 3) if (a + r) else None},
    }}


# ---------------------------------------------------------------- 规矩提案（C2）
def handle_rule_propose(body, user=None):
    """收下一条候选规矩。**不写 memory、不动 rules.md** —— 只是排队等人确认。"""
    pid = (body.get("projectId") or "").strip()
    text = (body.get("text") or "").strip()
    if not pid or not text:
        return 400, {"code": 400, "message": "projectId 和 text 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    kept = text[:512]
    src = (body.get("source") or "agent").strip()[:32]
    ref = (body.get("sourceRef") or "").strip()[:64] or None
    ev = (body.get("evidence") or "").strip()[:4000] or None
    dup = q("SELECT id, status FROM rule_proposal WHERE project_id=%s AND text=%s LIMIT 1",
            (pid, kept))
    if dup:
        return 200, {"code": 0, "data": {"id": dup[0]["id"], "status": dup[0]["status"],
                                        "duplicated": True}}
    rid = exec_sql(
        "INSERT INTO rule_proposal (project_id, user_id, text, source, source_ref, evidence) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (pid, str((user or {}).get("id") or ""), kept, src, ref, ev))
    return 200, {"code": 0, "data": {"id": rid, "status": "pending"}}


def handle_rule_proposals(params, user=None):
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    status = q1(params, "status", "pending")
    rows = q("SELECT id, project_id, user_id, text, source, source_ref, evidence, "
             "status, decided_by, decided_at, created_at FROM rule_proposal "
             "WHERE project_id=%s AND status=%s ORDER BY id DESC", (pid, status))
    return 200, {"code": 0, "data": rows}


def handle_rule_decide(rid, body, user=None):
    """确认或否决一条候选。确认 = 写 memory（scope=project）并渲染 rules.md。"""
    rows = q("SELECT id, project_id, text, status FROM rule_proposal WHERE id=%s", (rid,))
    if not rows:
        return 404, {"code": 404, "message": "提案不存在"}
    row = rows[0]
    deny = guard_project(row["project_id"], user)
    if deny:
        return deny
    if row["status"] != "pending":
        return 409, {"code": 409, "message": "这条已经处理过了（%s）" % row["status"]}
    action = (body.get("action") or "").strip()
    if action not in ("accept", "reject"):
        return 400, {"code": 400, "message": "action 只能是 accept / reject"}
    who = str((user or {}).get("id") or "")

    if action == "reject":
        exec_sql("UPDATE rule_proposal SET status='rejected', decided_by=%s, decided_at=NOW(3) "
                 "WHERE id=%s", (who, rid))
        return 200, {"code": 0, "data": {"id": rid, "status": "rejected"}}

    # 确认：同一个项目里已有同样内容的规矩就不重复写
    dup = q("SELECT id FROM memory WHERE scope='project' AND owner_id=%s AND text=%s LIMIT 1",
            (row["project_id"], row["text"]))
    if dup:
        mid = dup[0]["id"]
        exec_sql("UPDATE memory SET status=1, updated_at=NOW(3) WHERE id=%s", (mid,))
    else:
        mid = exec_sql(
            "INSERT INTO memory (scope, owner_id, text, source, source_ref, applies_to, updated_at) "
            "VALUES ('project',%s,%s,'confirmed',%s,%s,NOW(3))",
            (row["project_id"], row["text"], "proposal:%s" % rid,
             "project:%s" % row["project_id"]))
    exec_sql("UPDATE rule_proposal SET status='accepted', decided_by=%s, decided_at=NOW(3) "
             "WHERE id=%s", (who, rid))
    rel = render_rules_md(row["project_id"])
    return 200, {"code": 0, "data": {"id": rid, "status": "accepted",
                                     "memoryId": mid, "rendered": rel}}


# ---------------------------------------------------------------- render_user_project_md
def render_user_project_md(user_id, project_id):
    """个人 × 项目记忆：<homes>/<uid>/memories/projects/<pid>.md

    引擎只有"个人跨项目"（MEMORY.md），缺"这个人在这一个项目里的习惯"。
    文件是 memory 表的渲染视图；只有本人能看到（按家目录隔离）。
    """
    # 引擎记忆关闭时（D-记忆-04），不要往 homes/<uid>/memories 里写：
    # 那样隔离区会被"重新长出来"（空壳 projects/ 也算）。改写到 data/memory/projects/。
    if os.environ.get("JL_ENGINE_MEMORY", "0") == "1":
        d = os.path.join(HOMES_ROOT, str(user_id), "memories", "projects")
    else:
        d = os.path.join(WS_ROOT, "..", "memory", "projects", str(user_id))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        sys.stderr.write("[mem] 建目录失败 %s: %r\n" % (d, e))
        return None
    rows = q("SELECT text FROM memory WHERE scope='user' AND owner_id=%s "
             "AND project_id=%s AND status=1 ORDER BY id", (str(user_id), project_id))
    lines = ["# %s 在 %s 的项目记忆" % (user_id, project_id), "",
             "> 只在这个项目生效的个人习惯；跨项目偏好见 ../MEMORY.md。", ""]
    for r in rows:
        t = (r.get("text") or "").strip()
        if t:
            lines.append("- %s" % t)
    if len(lines) == 4:
        lines.append("- （空）")
    body = "\n".join(lines).rstrip() + "\n"
    path = os.path.join(d, "%s.md" % project_id)
    try:
        old = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old != body:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
    except OSError as e:
        sys.stderr.write("[mem] 写文件失败 %s: %r\n" % (path, e))
        return None
    return path


# ---------------------------------------------------------------- render_rules_md
def render_rules_md(project_id):
    """把项目规矩渲染成 <ws>/records/rules.md（memory 表是条目账本，这里是视图）。

    以前是"创建时往文件追加一行 + 同时写库"——两处各写各的，改了库文件不动、
    删了库文件还在。现在只有 memory 表写，文件每次从表重渲染。
    """
    ws = f"{WS_ROOT}/{project_id}"
    try:
        os.makedirs(f"{ws}/records", exist_ok=True)
    except OSError as e:
        sys.stderr.write("[rules] 建目录失败 %s: %r\n" % (ws, e))
        return None
    rows = q("SELECT text FROM memory WHERE scope='project' AND owner_id=%s "
             "AND status=1 ORDER BY id", (project_id,))
    lines = ["# 项目规矩", "",
             "> 由小程序/接口写入（条目来源见记忆账本）。AGENTS.md 只放指针，正文在这里。",
             ""]
    for r in rows:
        t = (r.get("text") or "").strip()
        if t:
            lines.append("- %s" % t)
    body = "\n".join(lines).rstrip() + "\n"
    rel = "records/rules.md"
    full = os.path.join(ws, rel)
    try:
        old = ""
        if os.path.exists(full):
            with open(full, encoding="utf-8") as f:
                old = f.read()
        if old == body:
            return rel
        with open(full, "w", encoding="utf-8") as f:
            f.write(body)
        subprocess.run(["/usr/bin/git", "-C", ws, "add", "--", rel], check=False)
        subprocess.run(["/usr/bin/git", "-C", ws,
                        "-c", "user.name=jl-service", "-c", "user.email=jl@local",
                        "commit", "-m", "docs(规矩): 重渲染项目规矩 %d 条" % len(rows)],
                       check=False)
    except OSError as e:
        sys.stderr.write("[rules] 写文件失败 %s: %r\n" % (full, e))
        return None
    return rel


# ---------------------------------------------------------------- 引用账本
def _base_ref(b):
    """把一条依据归一成 (kind, ref)。支持两种形态：
       对象 {"kind":"rule","ref":"JGJ130-2011#6.4.3"}
       字符串 "standard://JGJ 130-2011#6.4.3:1-1"（模型按引用格式写的）
    """
    if isinstance(b, str):
        loc = b.strip()
        if loc.startswith("standard://"):
            body = loc[len("standard://"):]
            # 去掉结尾的 :1-1
            if ":" in body:
                body = body.rsplit(":", 1)[0]
            return "rule", body
        return "file", loc
    if isinstance(b, dict):
        return (b.get("kind") or ""), (b.get("ref") or "")
    return "", ""


CODE_RE = r"(?:GB/T|GB|JGJ/T|JGJ|JG|DBJ|CJJ|CECS)[\s/]*[\d.]+(?:-\s*\d{4})?"
CLAUSE_CN_RE = r"第\s*([\d]+(?:\.[\d]+)*)\s*条"
CLAUSE_NUM_RE = r"(\d+(?:\.\d+)+)"


def _parse_ref(kind, ref):
    """规范类依据 → (canon, code, clause)；认不出返回 None。

    模型会混用三种写法，都得认（否则「依据」写对了却不记账）：
      standard://JGJ 130-2011#6.4.3:1-1
      JGJ130-2011#6.4.3
      JGJ 130-2011 第 6.4.3 条
    """
    if not ref:
        return None
    r = ref.strip()

    # ① 带 # 的两种写法
    if "#" in r:
        body = r[len("standard://"):] if r.startswith("standard://") else r
        if ":" in body.rsplit("#", 1)[-1]:
            body = body.rsplit(":", 1)[0]
        code, _, clause = body.partition("#")
        code, clause = code.strip(), clause.strip()
        if code and clause:
            return ("standard://%s#%s" % (code, clause)), code, clause

    # ② 「JGJ 130-2011 第 6.4.3 条」
    codes = re.findall(CODE_RE, r, re.I)
    if not codes:
        return None
    code = "".join(str(codes[0]).split()).upper()
    # 标准号里字母段与编号之间要留空格（GB50204-2015 → GB 50204-2015）
    cut = next((i for i, ch in enumerate(code) if ch.isdigit()), None)
    if cut:
        code = code[:cut] + " " + code[cut:]
    clauses = re.findall(CLAUSE_CN_RE, r) or re.findall(CLAUSE_NUM_RE, r)
    if not clauses:
        return None
    clause = clauses[-1]
    return ("standard://%s#%s" % (code, clause)), code, clause



def _cite_digest(kind, ref, quote=""):
    """一条 ref 一行。quote 不进 digest —— 否则同一法条引用十次会拆成十行，
    计数失效。摘录不一致用 disputed 标记（见 record_citations）。"""
    import hashlib
    return hashlib.md5(("%s|%s" % (kind, ref)).encode("utf-8")).hexdigest()


def record_citations(bases):
    """出稿时：把 rule 类依据记进账本（used_count+1）。不判断对错，只记账。"""
    n = 0
    for b in (bases or []):
        kind, ref = _base_ref(b)
        parsed = _parse_ref(kind, ref)
        if not parsed:
            continue
        canon, code, clause = parsed
        quote = ""
        if isinstance(b, dict):
            quote = (b.get("quote") or b.get("text") or "")[:120]
        dg = _cite_digest("standard", canon, quote)
        try:
            old = q("SELECT id, quote FROM citation WHERE digest=%s", (dg,))
            if old:
                prev = (old[0].get("quote") or "")[:120]
                # 同一 ref 出现不同摘录 = 有冲突，降级回 raw，等人工核对
                if prev and quote and prev != quote:
                    exec_sql("UPDATE citation SET used_count=used_count+1, disputed=1, "
                             "status='raw', last_used_at=NOW() WHERE digest=%s", (dg,))
                else:
                    exec_sql("UPDATE citation SET used_count=used_count+1, "
                             "last_used_at=NOW(), quote=COALESCE(NULLIF(quote,''), %s) "
                             "WHERE digest=%s", (quote, dg))
            else:
                exec_sql(
                    "INSERT INTO citation (source_kind, ref, quote, digest, used_count, last_used_at) "
                    "VALUES ('standard', %s, %s, %s, 1, NOW())",
                    (canon, quote, dg))
            n += 1
        except Exception as exc:
            sys.stderr.write("[cite] 记账失败 %s: %r\n" % (canon, exc))
    return n


def verify_citations_for_doc(doc_id):
    """签发 / 复核通过：把这份文书依据过的条各 +1 verified（同一份文书只算一次）。"""
    try:
        rows = q("SELECT bases FROM doc WHERE id=%s", (doc_id,))
        if not rows:
            return 0
        bases = rows[0].get("bases")
        if isinstance(bases, str):
            bases = json.loads(bases or "[]")
    except Exception as exc:
        sys.stderr.write("[cite] 取依据失败 doc=%s: %r\n" % (doc_id, exc))
        return 0
    n = 0
    for b in (bases or []):
        kind, ref = _base_ref(b)
        parsed = _parse_ref(kind, ref)
        if not parsed:
            continue
        canon, code, clause = parsed
        try:
            # 只更新已存在的账本行（不凭空造）
            exec_sql("UPDATE citation SET verified_count=verified_count+1 "
                     "WHERE ref=%s AND verified_count < 999", (canon,))
            n += 1
        except Exception as exc:
            sys.stderr.write("[cite] 核验失败 %s: %r\n" % (canon, exc))
    if n:
        try:
            exec_sql("UPDATE citation SET status = CASE "
                     "WHEN disputed=1 THEN 'raw' "
                     "WHEN verified_count>=5 AND used_count>=10 THEN 'authoritative' "
                     "WHEN verified_count>=2 AND used_count>=3 THEN 'trusted' "
                     "ELSE 'raw' END")
        except Exception as exc:
            sys.stderr.write("[cite] 升级状态失败: %r\n" % (exc,))
    return n


def prepare_bases(fields, bases):
    """依据归一（两条创建路径共用，避免两份实现）：

    ① 模型把依据写进「依据」字段、没传 bases → 反哺成结构化依据
    ② bases 有值但字段空 → 由 bases 派生字段
    ③ 记账（used_count+1）
    返回 (fields, bases)。
    """
    bases = list(bases or [])
    if not bases and isinstance(fields, dict) and str(fields.get("依据") or "").strip():
        for _it in [x.strip() for x in re.split(r"[；;、,，]", str(fields["依据"])) if x.strip()]:
            _p = _parse_ref("rule", _it)
            if _p:
                bases.append({"kind": "rule", "ref": _p[0], "text": _it[:120]})

    if bases and isinstance(fields, dict) and not str(fields.get("依据") or "").strip():
        _refs = []
        for _b in bases:
            _k, _r = _base_ref(_b)
            _p = _parse_ref(_k, _r)
            if _p:
                _refs.append(_p[0])
            elif isinstance(_b, dict) and (_b.get("ref") or _b.get("text")):
                _refs.append((_b.get("ref") or _b.get("text"))[:60])
        if _refs:
            fields["依据"] = "；".join(_refs)[:200]

    record_citations(bases)
    return fields, bases


def record_observations(project_id, doc_kind, findings):
    """把带 tag 的意见累加成观察条目。tag 由复核员给，后端只计数。"""
    n = 0
    seen = {}
    for f in (findings or []):
        tag = (f.get("tag") or "").strip()[:32]
        if not tag:
            continue
        seen.setdefault(tag, []).append((f.get("why") or "")[:120])
    for tag, samples in seen.items():
        try:
            rows = q("SELECT id, count, samples FROM observation "
                     "WHERE project_id=%s AND doc_kind=%s AND tag=%s",
                     (project_id, doc_kind or "", tag))
            if rows:
                prev = rows[0].get("samples")
                if isinstance(prev, str):
                    try:
                        prev = json.loads(prev)
                    except Exception:
                        prev = []
                merged = ([x for x in (prev or [])] + samples)[-5:]
                exec_sql("UPDATE observation SET count=count+%s, last_seen=NOW(), samples=%s "
                         "WHERE id=%s",
                         (len(samples), json.dumps(merged, ensure_ascii=False), rows[0]["id"]))
            else:
                exec_sql("INSERT INTO observation (project_id, doc_kind, tag, count, samples) "
                         "VALUES (%s,%s,%s,%s,%s)",
                         (project_id, doc_kind or "", tag, len(samples),
                          json.dumps(samples[-5:], ensure_ascii=False)))
            n += 1
        except Exception as exc:
            sys.stderr.write("[obs] 记账失败 %s/%s: %r\n" % (doc_kind, tag, exc))
    return n


def handle_observations(project_id, status="open", user=None):
    """观察队列查询。达到 3 次的才算"反复出现"，一次的不算现象。"""
    deny = guard_project(project_id, user)
    if deny:
        return deny
    rows = q("SELECT id, doc_kind, tag, count, status, samples, first_seen, last_seen "
             "FROM observation WHERE project_id=%s AND status=%s AND count >= 3 "
             "ORDER BY count DESC, last_seen DESC LIMIT 50",
             (project_id, status or "open"))
    out = []
    for r in rows:
        sm = r.get("samples")
        if isinstance(sm, str):
            try:
                sm = json.loads(sm)
            except Exception:
                sm = []
        out.append({"id": r["id"], "docKind": r.get("doc_kind"), "tag": r.get("tag"),
                    "count": r.get("count"), "status": r.get("status"),
                    "samples": sm or [], "firstSeen": str(r.get("first_seen") or ""),
                    "lastSeen": str(r.get("last_seen") or "")})
    return 200, {"code": 0, "data": out}


def handle_review_record(body, user=None):
    """复核判定落库。verdict 只能是枚举；findings/counter 每条必须带 locator。"""
    doc_id = body.get("docId") or body.get("doc_id")
    verdict = (body.get("verdict") or "").strip()
    if not doc_id:
        return 400, {"code": 400, "message": "docId 必填"}
    if verdict not in ("must_fix", "suggest", "ok"):
        return 400, {"code": 400,
                     "message": "verdict 只能是 must_fix / suggest / ok，收到：%r" % (verdict,)}

    rows = q("SELECT project_id FROM doc WHERE id=%s", (doc_id,))
    if not rows:
        return 404, {"code": 404, "message": "文书不存在"}
    pid = rows[0]["project_id"]
    deny = guard_project(pid, user)
    if deny:
        return deny

    def _clean(items, name):
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            loc = (it.get("locator") or "").strip()
            if not loc:
                # 没有出处的意见不算数：拒收，让复核员补 locator
                return None, "%s 里有一条没有 locator：%r" % (name, (it.get("why") or it.get("reading") or "")[:40])
            out.append({"why": (it.get("why") or "").strip()[:300],
                        "locator": loc[:400],
                        "tag": (it.get("tag") or "").strip()[:32]})
        return out, None

    findings, err = _clean(body.get("findings"), "findings")
    if err:
        return 400, {"code": 400, "message": err}
    counter, err = _clean(body.get("counter"), "counter")
    if err:
        return 400, {"code": 400, "message": err}

    # 去重：复核员有时会把同一条判定调两次（实测会重复）
    try:
        dup = q("SELECT id, verdict FROM review WHERE doc_id=%s AND verdict=%s "
                "AND findings=%s AND created_at > DATE_SUB(NOW(), INTERVAL 1 HOUR) "
                "ORDER BY id DESC LIMIT 1",
                (doc_id, verdict, json.dumps(findings or [], ensure_ascii=False)))
        if dup:
            return 200, {"code": 0, "data": {"id": dup[0]["id"], "verdict": verdict,
                                             "duplicated": True, "verifiedBumped": 0}}
    except Exception as exc:
        sys.stderr.write("[review] 去重查询失败: %r\n" % (exc,))

    rid = exec_sql(
        "INSERT INTO review (doc_id, task_id, project_id, verdict, findings, counter, "
        "alternatives, uncertainty, model) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (doc_id, body.get("taskId"), pid, verdict,
         json.dumps(findings or [], ensure_ascii=False),
         json.dumps(counter or [], ensure_ascii=False),
         json.dumps(body.get("alternatives") or [], ensure_ascii=False),
         (body.get("uncertainty") or "")[:512],
         (body.get("model") or "")[:64]))

    # 观察队列：同一类问题反复出现 → 进人工队列（不自动改技能）
    try:
        krow = q("SELECT kind FROM doc WHERE id=%s", (doc_id,))
        doc_kind = (krow[0].get("kind") if krow else "") or ""
        record_observations(pid, doc_kind, findings)
    except Exception as exc:
        sys.stderr.write("[obs] 累加失败 doc=%s: %r\n" % (doc_id, exc))

    bumped = verify_citations_for_doc(doc_id) if verdict == "ok" else 0
    return 200, {"code": 0, "data": {"id": rid, "verdict": verdict,
                                     "findings": len(findings or []),
                                     "counter": len(counter or []),
                                     "verifiedBumped": bumped}}


# ---------------------------------------------------------------- handle_doc_register
def handle_doc_validate(body, user=None):
    """只校验，不落库。agent 出稿前自己先查一遍，别等 register 被打回。"""
    kind = (body.get("kind") or "").strip()
    if not kind:
        return 400, {"code": 400, "message": "kind 必填"}
    if load_schema(kind) is None:
        return 400, {"code": 400, "message": "不支持生成「%s」草稿；可出的文书：%s。巡视记录请走每日巡视记录导出，不要当单份文书生成。"
                     % (kind, "、".join(list_schemas()))}
    fields = body.get("fields") or {}
    if not any(str(v or "").strip() for v in fields.values()):
        return 400, {"code": 400, "message": "这份文书没有任何字段内容；先向用户问清部位/事由，不要生成空文书"}
    check = validate_fields(kind, fields)
    return 200, {"code": 0, "data": dict(check, kind=kind, schemas=list_schemas())}


def handle_doc_register(body, user=None):
    """登记文书：只写数据库，不写文件。

    分工：工作区里的文件和 git 提交由 Agent 侧的工具负责（它懂仓库和钩子），
    数据库这一行由 biz 负责 —— 这样 doc 表只有一个写者。
    """
    pid = (body.get("projectId") or "").strip()
    kind = (body.get("kind") or "").strip()
    if not pid or not kind:
        return 400, {"code": 400, "message": "projectId 和 kind 必填"}
    if load_schema(kind) is None:
        return 400, {"code": 400, "message": "不支持生成「%s」草稿；可出的文书：%s。巡视记录请走每日巡视记录导出。"
                     % (kind, "、".join(list_schemas()))}

    deny = guard_project(pid, user)
    if deny:
        return deny

    obj = (body.get("obj") or "").strip()
    fields = body.get("fields") or {}
    if not obj and not any(str(v or "").strip() for v in fields.values()):
        return 400, {"code": 400, "message": "这份文书没有任何内容；先向用户问清部位/事由，不要登记空文书"}
    bases = body.get("bases") or []
    path = (body.get("path") or "").strip()
    commit = (body.get("commit") or "").strip()
    # 这一步由哪条任务产出（工具从 .agent/turn-<uid>.json 带上来）
    task_id = body.get("taskId") or None

    fields, bases = prepare_bases(fields, bases)

    # 齐不齐由 schema 说了算（依据也算必填；占位词「待补」也算缺）
    check = validate_fields(kind, fields)
    missing = list(check["missing"]) + list(check["placeholder"])
    # 状态只由 schema 决定，不接受客户端传的 state。
    state = "待补" if missing else "待签"

    # 用 lastrowid：以前 SELECT ... ORDER BY id DESC LIMIT 1 回头捞，
    # 同项目并发登记会捞到别人的行（返回错误的 docId）。
    doc_id = exec_sql(
        "INSERT INTO doc (project_id, kind, obj, state, fields, bases, path, git_commit, task_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (pid, kind, obj or None, state, json.dumps(fields, ensure_ascii=False),
         json.dumps(bases, ensure_ascii=False), path or None, commit or None,
         task_id))

    audit(pid, (user or {}).get("id") or 1, "doc.create", f"doc:{doc_id}", commit)
    return 200, {"code": 0, "data": {
        "docId": doc_id, "state": state, "missing": missing,
        "placeholder": check["placeholder"], "unknown": check["unknown"]}}


# ---------------------------------------------------------------- handle_doc_get
_FIELD_ALIASES = {
    "监理日志": {
        "当日施工进展情况": "当日施工进展",
        "当日监理工作情况": "当日监理工作",
        "当日存在问题及处理情况": "当日存在问题及处理",
        "其他有关事项": "其他事项",
    },
}


def normalize_fields(kind, fields):
    """模型/旧模板有时写旧字段名，旧键会和 schema 的新键并存，页面就出现
    “明明填了，下面还有一堆待补”。读文书时把旧键并到新键并删掉旧键。"""
    f = dict(fields or {})
    for old, new in (_FIELD_ALIASES.get(kind) or {}).items():
        if old not in f:
            continue
        val = str(f.get(old) or "").strip()
        cur = str(f.get(new) or "").strip()
        if val and (not cur or cur in ("待补", "待定", "待填写", "待确认")):
            f[new] = f[old]
        del f[old]
    return f
def handle_doc_get(doc_id, user=None):
    rows = q("SELECT id, project_id, kind, obj, state, fields, bases, path, git_commit, "
             "created_at, updated_at FROM doc WHERE id=%s", (doc_id,))
    if not rows:
        return 404, {"code": 404, "message": "文书不存在"}
    d = rows[0]
    deny = guard_project(d["project_id"], user)
    if deny:
        return deny
    fields = d["fields"]
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = {}
    bases = d["bases"]
    if isinstance(bases, str):
        try:
            bases = json.loads(bases)
        except Exception:
            bases = []
    fields = normalize_fields(d["kind"], fields)
    check = validate_fields(d["kind"], fields)
    missing = list(check["missing"]) + list(check["placeholder"])

    # 依据补全：库里的 bases 只存了 ref+text，这里补标准全名与原文摘录，
    # 前端才好点开看。规范号空格不一致（"JGJ130-2011" vs "JGJ 130-2011"），
    # 用 SQL 的 REPLACE 归一，不在 Python 里做正则拆分。
    norm_bases = []
    for b in (bases or []):
        kind, ref = _base_ref(b)
        parsed = _parse_ref(kind, ref)
        if parsed:
            canon, code, clause = parsed
            nb = dict(b) if isinstance(b, dict) else {
                "kind": "rule", "ref": ref, "text": ""}
            nb["kind"], nb["ref"] = "rule", canon
            norm_bases.append((nb, code, clause))
        else:
            nb = dict(b) if isinstance(b, dict) else {"kind": kind, "ref": ref, "text": ""}
            norm_bases.append((nb, None, None))

    for nb, code, clause in norm_bases:
        b = nb
        if not code:
            continue
        try:
            rows2 = q("SELECT title, std_title, body, clause FROM standard "
                      "WHERE REPLACE(code,' ','')=REPLACE(%s,' ','') AND clause=%s",
                      (code.strip(), clause.strip()))
        except Exception as exc:
            sys.stderr.write("[doc] 补依据失败 %s: %r\n" % (nb.get("ref"), exc))
            continue
        if not rows2:
            continue
        r2 = rows2[0]
        b["std"] = (r2.get("std_title") or "").strip()
        b["clause"] = r2.get("clause")
        b["title"] = (r2.get("title") or "").strip()
        # 摘录优先用库里的原文（≤160 字），没有就保留它自己写的 text
        body = (r2.get("body") or "").strip().replace("\n", " ")
        if body:
            b["quote"] = body[:160]

    # 老前端只看 text，新前端看 quote；两边都给，别让依据点开是空白。
    for nb, _code, _clause in norm_bases:
        if not nb.get("text") and nb.get("quote"):
            nb["text"] = nb["quote"]
        if not nb.get("quote") and nb.get("text"):
            nb["quote"] = nb["text"]

    bases = [nb for nb, _, _ in norm_bases]

    return 200, {"code": 0, "data": {
        "id": d["id"], "projectId": d["project_id"], "kind": d["kind"], "obj": d["obj"],
        "state": d["state"], "fields": fields or {}, "missing": missing,
        "bases": bases or [], "path": d["path"], "commit": (d["git_commit"] or "")[:10],
        "createdAt": str(d["created_at"] or ""), "updatedAt": str(d["updated_at"] or ""),
    }}


# ---------------------------------------------------------------- handle_doc_patch
def handle_doc_patch(doc_id, body, user=None):
    rows = q("SELECT id, project_id, kind, obj, fields, state, bases FROM doc WHERE id=%s", (doc_id,))
    if not rows:
        return 404, {"code": 404, "message": "文书不存在"}
    d = rows[0]
    deny = guard_project(d["project_id"], user)
    if deny:
        return deny

    fields = d["fields"]
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except Exception:
            fields = {}
    fields = dict(fields or {})

    patch = body.get("fields") or {}
    if not isinstance(patch, dict):
        return 400, {"code": 400, "message": "fields 必须是对象"}
    # 允许「只改状态」（比如签发）：不传 fields 就只动 state。
    # 原来强制 fields 必填，导致签发时还得把全部字段重发一遍。
    if not patch and not (body.get("state") or "").strip():
        return 400, {"code": 400, "message": "fields 或 state 至少给一个"}

    # 允许补 schema 里的字段（不只是已有字段）。
    #
    # 原来的写法是「只能改已有字段」，导致：文书创建时只写了「部位/问题描述」，
    # 监理员在手机上补「受检单位/整改期限/签发人」时，这些键不在 fields 里 →
    # 被静默丢掉，还报 missing=[] 并把状态改成「待签」。
    # 等于「说补好了，其实没补」——实测 doc 139 复现。
    allowed = set(fields.keys())
    try:
        schema = load_schema(d["kind"]) or {}
        # schema 里的必填 + 可选都能补
        allowed |= set(schema.get("fields") or [])
        allowed |= set(schema.get("optional") or [])
    except Exception as exc:
        sys.stderr.write(f"[doc] 取 schema 失败 {d['kind']}: {exc}\n")

    dropped = []
    for k, v in patch.items():
        if k in allowed:
            fields[k] = "" if v is None else str(v)
        else:
            dropped.append(k)

    # 缺项以 schema 为准（不是「当前 fields 里为空的键」）
    chk = validate_fields(d["kind"], fields)
    missing = list(chk.get("missing") or []) + list(chk.get("placeholder") or [])

    wanted = (body.get("state") or "").strip()
    # 新建时缺字段会被降级成「待补」；这里必须一致 ——
    # 否则客户端一句 state=已签 就能把缺项的文书标成签发。
    if missing and wanted in ("待签", "已签"):
        return 400, {"code": 400,
                     "message": "%s 还缺 %d 项，不能标成「%s」" % (d["kind"], len(missing), wanted),
                     "missing": missing}
    state = wanted or ("待补" if missing else "待签")

    prev_state = d.get("state")
    exec_sql("UPDATE doc SET fields=%s, state=%s, updated_at=NOW(3) WHERE id=%s",
             (json.dumps(fields, ensure_ascii=False), state, doc_id))
    # 人签发 = 依据被确认：引用账本 verified +1。
    # 只在「从未签 → 已签」这一次生效，否则重复 PUT 会把权威度刷上去。
    if state == "已签" and prev_state != "已签":
        verify_citations_for_doc(doc_id)

    # 写回工作区并提交，保证"文档=仓库里的文件"
    try:
        update_doc_file(d["project_id"], doc_id, d["kind"], d["obj"], fields, d.get("bases"))
    except Exception as e:
        sys.stderr.write(f"[doc] 回写失败 {doc_id}: {e}\n")

    audit(d["project_id"], user["id"] if user else 1, "doc.update", f"doc:{doc_id}",
          json.dumps({"fields": list(patch.keys()), "state": state}, ensure_ascii=False))
    return 200, {"code": 0, "data": {"id": doc_id, "state": state, "missing": missing,
                                       "dropped": dropped,
                                     "fields": fields}}


# ---------------------------------------------------------------- 文书渲染（唯一实现）
GEN_TIME_RE = re.compile(r"^> 生成时间：(.*)$", re.M)


def render_doc_md(kind, obj, fields, bases, missing=None, gen_time=None):
    """文书 md 的唯一渲染器 —— 版式必须与 tools/src/tools.js renderDoc 一致。

    以前有两份：工具的（有生成时间+待补+依据段）和 commit_doc 的（都没有）。
    补一次字段就多一个文件、还把"待补"抹掉 —— 归档看不出当时缺什么。
    """
    plac = set(schema_placeholders(kind))
    lines = [
        "# " + str(kind) + (" · " + str(obj) if obj else ""),
        "",
        "> 生成时间：" + (gen_time or time.strftime("%Y-%m-%d %H:%M:%S")),
        "",
    ]
    miss = list(missing or [])
    for k in miss:
        lines.append("- **%s**：待补" % k)
    for k, v in (fields or {}).items():
        txt = "" if v is None else str(v).strip()
        if not txt or txt in plac or k in miss:
            continue
        lines.append("- **%s**：%s" % (k, txt))
    if bases:
        lines += ["", "## 依据", ""]
        for b in bases:
            _k, ref = _base_ref(b)
            text = (b.get("text") or "") if isinstance(b, dict) else ""
            lines.append("- %s（%s）" % (text or _k or "依据", ref))
    return "\n".join(lines) + "\n"


def schema_placeholders(kind):
    sch = load_schema(kind) or {}
    return sch.get("placeholders") or []


def _doc_rel_path(project_id, kind, obj, doc_id):
    """沿用工具的文件名规则：docs/<date>/<kind>-<obj>-NN.md；不换新名字。"""
    import pathlib
    stamp = time.strftime("%Y-%m-%d")
    safe = "".join(ch for ch in ("%s-%s" % (kind, obj or "")) if ch not in '\\/:*?"<>|')
    safe = safe.replace(" ", "")[:40] or "doc"
    d = pathlib.Path("%s/%s/docs/%s" % (WS_ROOT, project_id, stamp))
    d.mkdir(parents=True, exist_ok=True)
    seq = 1
    while (d / ("%s-%02d.md" % (safe, seq))).exists():
        seq += 1
    return "docs/%s/%s-%02d.md" % (stamp, safe, seq)


def update_doc_file(project_id, doc_id, kind, obj, fields, bases=None):
    """把字段回写进**同一个文件**并提交。

    档案要求：文件跟着字段走，历史里只有一条线。
    - 文件已存在 → 就地重写（保留原「生成时间」）
    - 文件不存在 → 按工具同一命名规则新建
    """
    ws = "%s/%s" % (WS_ROOT, project_id)
    if not os.path.isdir(ws):
        return None
    rows = q("SELECT path FROM doc WHERE id=%s", (doc_id,))
    rel = (rows[0].get("path") if rows else "") or ""
    if not rel:
        rel = _doc_rel_path(project_id, kind, obj, doc_id)

    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)

    gen_time = None
    try:
        with open(full, encoding="utf-8") as f:
            m = GEN_TIME_RE.search(f.read())
        if m:
            gen_time = m.group(1).strip()
    except OSError as exc:
        sys.stderr.write("[doc] 读不到原文件（按新建处理）%s: %r\n" % (full, exc))

    chk = validate_fields(kind, fields)
    missing = list(chk.get("missing") or [])
    md = render_doc_md(kind, obj, fields, bases, missing=missing, gen_time=gen_time)
    with open(full, "w", encoding="utf-8") as f:
        f.write(md)

    refs = []
    for b in (bases or []):
        _k, ref = _base_ref(b)
        if ref:
            refs.append(ref)
    basis = "、".join(refs[:5]) or ("doc:%s" % doc_id)
    subprocess.run(["/usr/bin/git", "-C", ws, "add", "--", rel], check=False)
    subprocess.run(["/usr/bin/git", "-C", ws,
                    "-c", "user.name=jl-service", "-c", "user.email=jl@local",
                    "commit", "-m",
                    "doc(%s): %s 更新字段\n\n依据: %s\n项目: %s"
                    % (kind[:8], obj or doc_id, basis, project_id)], check=False)
    commit = subprocess.run(["/usr/bin/git", "-C", ws, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    exec_sql("UPDATE doc SET path=%s, git_commit=%s WHERE id=%s", (rel, commit, doc_id))
    return commit


# ---------------------------------------------------------------- handle_standards_search
def handle_standards_search(params):
    kw = (q1(params, "q") or "").strip()
    limit = min(int(q1(params, "limit") or 20), 50)
    if kw:
        like = f"%{kw}%"
        rows = q("SELECT code, clause, title, body, version FROM standard "
                 "WHERE code LIKE %s OR title LIKE %s OR body LIKE %s "
                 "ORDER BY code, clause LIMIT %s", (like, like, like, limit))
    else:
        rows = q("SELECT code, clause, title, body, version FROM standard "
                 "ORDER BY code, clause LIMIT %s", (limit,))
    return 200, {"code": 0, "data": rows, "total": len(rows)}


# ---------------------------------------------------------------- handle_audit
def handle_audit(params, user=None):
    pid = q1(params, "projectId")
    limit = min(int(q1(params, "limit") or 50), 200)
    if pid:
        deny = guard_project(pid, user)
        if deny:
            return deny
        rows = q("SELECT id, project_id, actor_id, action, target_type, target_id, after, "
                 "created_at FROM audit_log WHERE project_id=%s ORDER BY id DESC LIMIT %s",
                 (pid, limit))
    else:
        rows = q("SELECT id, project_id, actor_id, action, target_type, target_id, after, "
                 "created_at FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
    out = []
    for r in rows:
        after = r.get("after")
        if isinstance(after, str):
            try:
                after = json.loads(after)
            except Exception:
                after = {}
        out.append({
            "id": r["id"], "projectId": r["project_id"], "actorId": r["actor_id"],
            "action": r["action"],
            "target": f"{r['target_type']}:{r['target_id']}" if r.get("target_id") else r.get("target_type"),
            "detail": (after or {}).get("detail", ""),
            "at": str(r["created_at"] or ""),
        })
    return 200, {"code": 0, "data": out}


# ---------------------------------------------------------------- handle_photo_patch
def handle_photo_patch(photo_id, body, user=None):
    rows = q("SELECT id, project_id, place FROM photo WHERE id=%s", (photo_id,))
    if not rows:
        return 404, {"code": 404, "message": "影像不存在"}
    ph = rows[0]
    deny = guard_project(ph["project_id"], user)
    if deny:
        return deny
    place = (body.get("place") or "").strip()
    if not place:
        return 400, {"code": 400, "message": "place 必填"}
    exec_sql("UPDATE photo SET place=%s WHERE id=%s", (place, photo_id))
    return 200, {"code": 0, "data": {"id": photo_id, "place": place}}


# ---------------------------------------------------------------- handle_photo_caption
def handle_photo_caption(body, user=None):
    """给照片补一句描述（agent 看完图写的那句话）。

    不覆盖已有描述除非显式传 overwrite=1 —— 描述是人能改的，别被后来的一轮冲掉。
    """
    pid = (body.get("projectId") or "").strip()
    sha = (body.get("sha256") or "").strip()
    caption = (body.get("caption") or "").strip()
    if not caption:
        return 400, {"code": 400, "message": "caption 必填"}
    if pid:
        deny = guard_project(pid, user)
        if deny:
            return deny
        rows = q("SELECT id, caption, project_id, place FROM photo "
                 "WHERE project_id=%s AND sha256=%s ORDER BY id DESC LIMIT 1", (pid, sha))
    elif sha:
        rows = q("SELECT id, caption, project_id, place FROM photo "
                 "WHERE sha256=%s ORDER BY id DESC LIMIT 1", (sha,))
    else:
        return 400, {"code": 400, "message": "projectId 或 sha256 必填"}
    if not rows:
        return 404, {"code": 404, "message": "没找到这张照片"}
    row = rows[0]
    if (row.get("caption") or "").strip() and not body.get("overwrite"):
        return 200, {"code": 0, "data": {"id": row["id"], "caption": row["caption"],
                                         "skipped": True}}
    exec_sql("UPDATE photo SET caption=%s, caption_at=NOW(3) WHERE id=%s", (caption[:500], row["id"]))
    owner = pid or (row.get("project_id") or "")
    idx = index_photo(owner, sha, caption[:500], row.get("place"))
    # 没写成索引就如实说 None：以前不管写没写成都回 records/photos.md
    return 200, {"code": 0, "data": {"id": row["id"], "caption": caption[:500],
                                     "indexed": idx or None}}


# ---------------------------------------------------------------- handle_photo_search
def handle_photo_search(params, user=None):
    """按描述/部位/日期查现场影像（caption 是 agent 看完图写的，最有用）"""
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    kw = (q1(params, "q") or "").strip()
    place = (q1(params, "place") or "").strip()
    date_from = (q1(params, "dateFrom") or "").strip()
    date_to = (q1(params, "dateTo") or "").strip()
    limit = min(int(q1(params, "limit") or 20), 100)

    sql = ("SELECT id, sha256, place, weather, shot_at, src, caption, created_at "
           "FROM photo WHERE project_id=%s")
    argv = [pid]
    if kw:
        like = "%" + kw + "%"
        sql += " AND (caption LIKE %s OR place LIKE %s)"
        argv += [like, like]
    if place:
        sql += " AND place LIKE %s"
        argv.append("%" + place + "%")
    if date_from:
        sql += " AND created_at >= %s"
        argv.append(date_from + " 00:00:00")
    if date_to:
        sql += " AND created_at <= %s"
        argv.append(date_to + " 23:59:59")
    sql += " ORDER BY created_at DESC LIMIT %s"
    argv.append(limit)

    rows = q(sql, tuple(argv))
    for r in rows:
        r["url"] = "/api/v1/blobs/" + r["sha256"]
        r["shot_at"] = str(r["shot_at"]) if r.get("shot_at") else None
        r["created_at"] = str(r["created_at"]) if r.get("created_at") else None
    return 200, {"code": 0, "data": rows, "total": len(rows)}


# ---------------------------------------------------------------- handle_stats
def handle_stats(params, user=None):
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    docs = q("SELECT COUNT(*) AS n FROM doc WHERE project_id=%s", (pid,))
    pending = q("SELECT COUNT(*) AS n FROM doc WHERE project_id=%s AND state='待补'", (pid,))
    msgs = q("SELECT COUNT(*) AS n FROM message WHERE project_id=%s", (pid,))
    photos = q("SELECT COUNT(*) AS n FROM photo WHERE project_id=%s", (pid,))
    tasks = q("SELECT COUNT(*) AS n FROM task WHERE project_id=%s AND status='done'", (pid,))
    return 200, {"code": 0, "data": {
        "docs": docs[0]["n"], "pending": pending[0]["n"], "messages": msgs[0]["n"],
        "photos": photos[0]["n"], "tasksDone": tasks[0]["n"],
    }}


# ---------------------------------------------------------------- handle_standard
def handle_standard(params):
    """按标准号+条号取条文原文 —— 点回复里的引用时用"""
    code = q1(params, "code")
    clause = q1(params, "clause")
    if not code or not clause:
        return 400, {"code": 400, "message": "code 和 clause 必填"}
    rows = q("SELECT code, clause, title, body, version, effective_at FROM standard "
             "WHERE code=%s AND clause=%s LIMIT 1", (code, clause))
    if not rows:
        return 404, {"code": 404, "message": "条文不存在"}
    r = rows[0]
    r["effectiveAt"] = str(r.pop("effective_at") or "")
    return 200, {"code": 0, "data": r}


# ---------------------------------------------------------------- member_of
def member_of(project_id, user):
    """这个人能不能看这个项目。

    演示模式（AUTH_REQUIRED=0）默认放行；
    开了鉴权之后，必须是 project_member 里的人（或者项目创建者）。
    """
    if not AUTH_REQUIRED:
        return True
    if not user:
        return False
    rows = q("SELECT 1 FROM project_member WHERE project_id=%s AND user_id=%s",
             (project_id, user["id"]))
    return bool(rows)


# ---------------------------------------------------------------- guard_project
def guard_project(project_id, user):
    """返回 None 表示放行，否则返回 403 的响应"""
    if member_of(project_id, user):
        return None
    return 403, {"code": 403, "message": "无权访问该项目"}


