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
from .core import (ADMIN_HTML, ALLOWED_TOOLS, AUTH_REQUIRED, BLOB_ROOT, BOLD_RE, ENGINE, ENGINE_CLIENT, ENGINE_ENV, ENGINE_MODE, ENV, GAP_INLINE_RE, GAP_SPLIT_RE, JL_TOOL_BIN, KIND_TO_ICON, KIND_TO_TINT, LEARN_PROMPT, LLM_MODEL, LLM_URL, MAX_CONCURRENT, NOTIFY, NUM_RE, PORT, REF_RE, ROOT, STATE_TO_ST, STD_RE, SYSTEM_PROMPT, TOKEN_TTL_HOURS, TURN_SLOTS, WEEK, WS_ROOT, _USER_LOCKS, _USER_LOCKS_GUARD, _bigrams, _day_label, _hash_token, _rerender_if_project, _split_num, allowed_tools, bearer, build_answer, clean_reply, db, describe_tool, exec_sql, extract_from_items, get_or_create_thread, get_project, handle_login, handle_logout, handle_render_memory, issue_token, llm, load_env, ms_label, notify_worker, on_notification, parse_step, q, q1, relevance, resolve_token, rich_reply, rich_segments, run_tool, similar, split_gaps, transcribe_audio, materialize_images, validate_fields, load_schema, list_schemas)


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
def render_project_memory(project_id):
    """把库里的记忆渲染成工作区文件并提交（文件是产物，库是事实来源）"""
    proj = get_project(project_id)
    pm = q("SELECT text, source, hits FROM memory WHERE scope='project' AND owner_id=%s AND status=1 "
           "ORDER BY hits DESC, id DESC", (project_id,))

    lines = [f"# 项目记忆 · {proj['name']}", "",
             "<!-- 由数据库渲染生成，勿手改。要改走管理接口。 -->", ""]
    if pm:
        for m in pm:
            lines.append(f"- {m['text']}  <!-- {m['source']} · 用过 {m['hits']} 次 -->")
    else:
        lines.append("暂无。")

    rel = "memories/project.md"
    abs_path = f"{WS_ROOT}/{project_id}/{rel}"
    # 目录可能不存在（项目从没渲染过记忆）—— 之前这里直接抛错，被上层吞成 500
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    ws = f"{WS_ROOT}/{project_id}"
    subprocess.run(["git", "add", "--", rel], cwd=ws, check=True,
                   capture_output=True, text=True)
    msg = f"memory(render): 同步项目记忆（{len(pm)} 条）\n\n依据: memory_table\n项目: {project_id}"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ws, capture_output=True, text=True)
    commit = None
    if r.returncode == 0:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws,
                                capture_output=True, text=True).stdout.strip()
    elif "nothing to commit" not in (r.stdout + r.stderr):
        raise RuntimeError(f"git 提交失败: {r.stderr[:300]}")
    return {"path": rel, "count": len(pm), "commit": commit,
            "changed": commit is not None}


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
def handle_upload(project_id, params, data):
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    if not data:
        return 400, {"code": 400, "message": "空文件"}
    if len(data) > 20 * 1024 * 1024:
        return 413, {"code": 413, "message": "超过 20MB"}

    sha, blob_path, size = store_blob(data)
    # params 里已经合并了 query + 表单字段（见 api.py）
    place = q1(params, "place")
    weather = q1(params, "weather")
    shot_at = q1(params, "shotAt")
    src = q1(params, "src", "camera")

    exists = q("SELECT id, blob_path FROM photo WHERE project_id=%s AND sha256=%s", (project_id, sha))
    if exists:
        return 200, {"code": 0, "data": {"duplicated": True, "photoId": exists[0]["id"],
                                        "sha256": sha, "size": size}}

    photo_id = exec_sql(
        "INSERT INTO photo (project_id, sha256, blob_path, place, weather, shot_at, src) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (project_id, sha, blob_path, place, weather, shot_at, src),
    )

    entry = {"sha256": sha, "path": f"blobs/{sha[:2]}/{sha}", "size": size,
             "takenAt": shot_at or str(time.strftime("%Y-%m-%d %H:%M:%S")),
             "place": place, "weather": weather, "src": src}
    commit = append_manifest(project_id, entry)
    if commit:
        exec_sql("UPDATE photo SET git_commit=%s WHERE id=%s", (commit, photo_id))

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


# ---------------------------------------------------------------- parse_multipart
def parse_multipart_full(body, content_type):
    """解析 multipart/form-data，返回 (文件字节, 字段字典)

    wx.uploadFile 把 projectId 之类放在 formData 里（不是 query），
    以前只抠文件、丢掉字段，导致上传永远进默认项目。这里把字段也带出来。
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
            if chunk.startswith("name=\""):
                name = chunk[6:].rstrip("\"")
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


def parse_multipart(body, content_type):
    """从 multipart/form-data 里抠出文件字节。wx.uploadFile 用的是这种。"""
    return parse_multipart_full(body, content_type)[0]


# ---------------------------------------------------------------- default_project_id
def default_project_id():
    rows = q("SELECT id FROM project ORDER BY created_at LIMIT 1")
    return rows[0]["id"] if rows else None


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
def handle_project_save(body):
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


# ---------------------------------------------------------------- handle_doc_create
def handle_doc_create(body):
    """小程序详情页点「生成文书」→ 写文件 + 提交 + 落库"""
    pid = body.get("projectId") or default_project_id()
    kind = body.get("kind") or "文书"
    obj = body.get("obj") or ""
    fields = body.get("fields") or {}
    bases = body.get("bases") or []
    if not pid:
        return 404, {"code": 404, "message": "还没有项目"}

    missing = [k for k, v in fields.items() if v in (None, "", "待补")]
    stamp = time.strftime("%Y-%m-%d")
    safe = "".join(ch for ch in f"{kind}-{obj}" if ch not in '\\/:*?"<>|').replace(" ", "")[:40]
    seq = 1
    import pathlib
    d = pathlib.Path(f"{WS_ROOT}/{pid}/docs/{stamp}")
    d.mkdir(parents=True, exist_ok=True)
    while (d / f"{safe}-{seq:02d}.md").exists():
        seq += 1
    rel = f"docs/{stamp}/{safe}-{seq:02d}.md"

    lines = [f"# {kind}" + (f" · {obj}" if obj else ""), "",
             f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for k, v in fields.items():
        lines.append(f"- **{k}**：{v if v not in (None, '', '待补') else '待补'}")
    if bases:
        lines += ["", "## 依据", ""]
        lines += [f"- {b.get('text') or b.get('kind')}（{b.get('ref')}）" for b in bases]
    (d / f"{safe}-{seq:02d}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ws = f"{WS_ROOT}/{pid}"
    refs = ", ".join([b.get("ref") for b in bases if b.get("ref")]) or "无"
    subprocess.run(["git", "add", "--", rel], cwd=ws, capture_output=True)
    r = subprocess.run(["git", "commit", "-m",
                        f"doc({kind[:6]}): {obj or '未命名'}\n\n依据: {refs}\n项目: {pid}"],
                       cwd=ws, capture_output=True, text=True)
    commit = None
    if r.returncode == 0:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws,
                                capture_output=True, text=True).stdout.strip()

    state = "待补" if missing else "待签"
    doc_id = exec_sql(
        "INSERT INTO doc (project_id, kind, obj, state, fields, bases, path, git_commit) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (pid, kind, obj, state, json.dumps(fields, ensure_ascii=False),
         json.dumps(bases, ensure_ascii=False), rel, commit))
    return 200, {"code": 0, "data": {"id": doc_id, "path": rel, "commit": commit,
                                    "state": state, "missing": missing}}


# ---------------------------------------------------------------- handle_geo_reverse
def handle_geo_reverse(params):
    """逆地理：先把项目地址回给它，接第三方后替换"""
    pid = q1(params, "projectId") or default_project_id()
    proj = get_project(pid) if pid else {}
    addr = proj.get("addr") or (proj.get("meta") or {}).get("addr") or "当前位置"
    return 200, {"code": 0, "data": {"address": addr, "stub": True}}


# ---------------------------------------------------------------- handle_weather
def handle_weather(params):
    """天气：先返回占位，接第三方后替换"""
    return 200, {"code": 0, "data": {"text": "多云", "temp": 26, "wind": "东南风 2 级", "stub": True}}


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
    text, err = transcribe_audio(sha)
    if err and text is None:
        return 503, {"code": 503, "message": err}

    # 沉淀：转写结果写进这条音频的描述里，否则"说过的"跟"拍过的"一样，读完就没了。
    # 描述是数据不是判断 —— 后端只负责存，怎么用由 agent 决定。
    saved = False
    if (text or "").strip():
        rows = q("SELECT id, caption FROM photo WHERE sha256=%s ORDER BY id DESC LIMIT 1", (sha,))
        if rows and not (rows[0].get("caption") or "").strip():
            exec_sql("UPDATE photo SET caption=%s, caption_at=NOW(3) WHERE id=%s",
                     ("语音：" + text.strip()[:480], rows[0]["id"]))
            saved = True

    return 200, {"code": 0, "data": {"text": text or "", "note": err or "",
                                     "saved": saved}}

# ---------------------------------------------------------------- handle_doc_register
def handle_doc_validate(body, user=None):
    """只校验，不落库。agent 出稿前自己先查一遍，别等 register 被打回。"""
    kind = (body.get("kind") or "").strip()
    if not kind:
        return 400, {"code": 400, "message": "kind 必填"}
    fields = body.get("fields") or {}
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

    deny = guard_project(pid, user)
    if deny:
        return deny

    obj = (body.get("obj") or "").strip()
    fields = body.get("fields") or {}
    bases = body.get("bases") or []
    path = (body.get("path") or "").strip()
    commit = (body.get("commit") or "").strip()

    # 齐不齐由 schema 说了算（占位词「待补」也算缺）
    check = validate_fields(kind, fields)
    missing = list(check["missing"]) + list(check["placeholder"])
    state = body.get("state") or ("待补" if missing else "待签")
    # 模型说"待签"但字段还缺 —— 以 schema 为准，别让不齐的文书冒充可签
    if missing and state == "待签":
        state = "待补"

    exec_sql(
        "INSERT INTO doc (project_id, kind, obj, state, fields, bases, path, git_commit) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (pid, kind, obj or None, state, json.dumps(fields, ensure_ascii=False),
         json.dumps(bases, ensure_ascii=False), path or None, commit or None))

    rows = q("SELECT id FROM doc WHERE project_id=%s ORDER BY id DESC LIMIT 1", (pid,))
    doc_id = rows[0]["id"] if rows else None

    audit(pid, (user or {}).get("id") or 1, "doc.create", f"doc:{doc_id}", commit)
    return 200, {"code": 0, "data": {
        "docId": doc_id, "state": state, "missing": missing,
        "placeholder": check["placeholder"], "unknown": check["unknown"]}}


# ---------------------------------------------------------------- handle_doc_get
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
    check = validate_fields(d["kind"], fields)
    missing = list(check["missing"]) + list(check["placeholder"])
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

    exec_sql("UPDATE doc SET fields=%s, state=%s, updated_at=NOW(3) WHERE id=%s",
             (json.dumps(fields, ensure_ascii=False), state, doc_id))

    # 写回工作区并提交，保证"文档=仓库里的文件"
    try:
        commit_doc(d["project_id"], doc_id, d["kind"], d["obj"], fields, d.get("bases"))
    except Exception as e:
        sys.stderr.write(f"[doc] 回写失败 {doc_id}: {e}\n")

    audit(d["project_id"], user["id"] if user else 1, "doc.update", f"doc:{doc_id}",
          json.dumps({"fields": list(patch.keys()), "state": state}, ensure_ascii=False))
    return 200, {"code": 0, "data": {"id": doc_id, "state": state, "missing": missing,
                                       "dropped": dropped,
                                     "fields": fields}}


# ---------------------------------------------------------------- commit_doc
def commit_doc(project_id, doc_id, kind, obj, fields, bases=None):
    """把字段渲染成 md 写进工作区并 git 提交"""
    ws = f"{WS_ROOT}/{project_id}"
    if not os.path.isdir(ws):
        return None
    import datetime
    day = datetime.date.today().isoformat()
    name = f"{kind}" + (f"-{obj}" if obj else "") + f"-{doc_id}.md"
    rel = f"docs/{day}/{name}"
    full = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)

    lines = [f"# {kind}" + (f"（{obj}）" if obj else ""), ""]
    for k, v in (fields or {}).items():
        lines.append(f"- **{k}**：{v or '待补'}")
    io_open = open
    with io_open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    subprocess.run(["/usr/bin/git", "-C", ws, "add", rel], check=False)
    # 仓库钩子要求提交信息里必须有「依据:」行，这是产品规则，照做
    basis = ""
    if bases:
        if isinstance(bases, str):
            try:
                bases = json.loads(bases)
            except Exception:
                bases = []
        items = []
        for b in bases:
            if isinstance(b, str):
                items.append(b)
            elif isinstance(b, dict):
                code, clause = b.get("code"), b.get("clause")
                if code and clause:
                    items.append(f"{code}#{clause}")
                elif code:
                    items.append(code)
        basis = "、".join(items[:5])
    if not basis:
        basis = f"doc:{doc_id}"

    msg = f"doc({kind}): {obj or doc_id} 更新字段\n\n依据: {basis}"
    subprocess.run(["/usr/bin/git", "-C", ws,
                    "-c", "user.name=jl-service", "-c", "user.email=jl@local",
                    "commit", "-m", msg], check=False)
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


# ---------------------------------------------------------------- handle_members
def handle_members(params, user=None):
    pid = q1(params, "projectId")
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    rows = q("SELECT m.user_id, m.role, u.name, u.role AS user_role FROM project_member m "
             "LEFT JOIN app_user u ON u.id=m.user_id WHERE m.project_id=%s ORDER BY m.user_id",
             (pid,))
    return 200, {"code": 0, "data": rows}


# ---------------------------------------------------------------- handle_member_add
def handle_member_add(body, user=None):
    pid = (body.get("projectId") or "").strip()
    uid = body.get("userId")
    role = (body.get("role") or "engineer").strip()
    if not pid or not uid:
        return 400, {"code": 400, "message": "projectId 和 userId 必填"}
    deny = guard_project(pid, user)
    if deny:
        return deny
    exec_sql("INSERT IGNORE INTO project_member (project_id, user_id, role) VALUES (%s,%s,%s)",
             (pid, int(uid), role))
    audit(pid, user["id"] if user else 1, "member.add", f"user:{uid}", role)
    return 200, {"code": 0, "data": {"projectId": pid, "userId": int(uid), "role": role}}


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
        rows = q("SELECT id, caption FROM photo WHERE project_id=%s AND sha256=%s "
                 "ORDER BY id DESC LIMIT 1", (pid, sha))
    elif sha:
        rows = q("SELECT id, caption FROM photo WHERE sha256=%s ORDER BY id DESC LIMIT 1", (sha,))
    else:
        return 400, {"code": 400, "message": "projectId 或 sha256 必填"}
    if not rows:
        return 404, {"code": 404, "message": "没找到这张照片"}
    row = rows[0]
    if (row.get("caption") or "").strip() and not body.get("overwrite"):
        return 200, {"code": 0, "data": {"id": row["id"], "caption": row["caption"],
                                         "skipped": True}}
    exec_sql("UPDATE photo SET caption=%s, caption_at=NOW(3) WHERE id=%s", (caption[:500], row["id"]))
    return 200, {"code": 0, "data": {"id": row["id"], "caption": caption[:500]}}


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


