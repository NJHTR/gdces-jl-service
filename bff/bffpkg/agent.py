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

"""Agent 运行时：引擎、任务编排、工具轨迹、记忆。

留 Python：它跟子进程、文件、git 打交道。
"""
from .appserver import _client, run_isolated, run_stream
from .core import (ADMIN_HTML, ALLOWED_TOOLS, AUTH_REQUIRED, BLOB_ROOT, BOLD_RE, ENGINE, ENGINE_CLIENT, ENGINE_ENV, ENGINE_MODE, ENV, GAP_INLINE_RE, GAP_SPLIT_RE, JL_TOOL_BIN, KIND_TO_ICON, KIND_TO_TINT, LEARN_PROMPT, LLM_MODEL, LLM_URL, MAX_CONCURRENT, NOTIFY, NUM_RE, PORT, REF_RE, ROOT, STATE_TO_ST, STD_RE, SYSTEM_PROMPT, TOKEN_TTL_HOURS, TURN_SLOTS, WEEK, WS_ROOT, _USER_LOCKS, _USER_LOCKS_GUARD, _bigrams, _day_label, _hash_token, _rerender_if_project, _split_num, allowed_tools, bearer, build_answer, clean_reply, db, describe_tool, exec_sql, extract_from_items, get_or_create_thread, get_project, handle_login, handle_logout, handle_render_memory, issue_token, llm, load_env, ms_label, notify_worker, on_notification, parse_step, q, q1, relevance, resolve_token, rich_reply, rich_segments, run_tool, similar, split_gaps, transcribe_audio, validate_fields, materialize_images, tool_manual)
from .biz import (append_manifest, audit, commit_doc, default_project_id, guard_project, handle_admin, handle_audit, handle_blob, handle_doc_create, handle_doc_get, handle_doc_patch, handle_docs_list, handle_geo_reverse, handle_member_add, handle_members, handle_photo_list, handle_photo_patch, handle_project_create, handle_project_get, handle_project_list, handle_project_save, handle_standard, handle_standards_search, handle_stats, handle_upload, handle_weather, member_of, parse_multipart, render_project_memory, store_blob)


# ---------------------------------------------------------------- Engine
class Engine:
    """一个常驻 app-server 进程，多线程复用（按 id 分发响应）"""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.proc = subprocess.Popen(
            # code-mode 依赖 V8 预编译包（GitHub 下载不了），关掉它，
            # exec_command 会退回直接执行 shell——CLI 路径就是这么跑的。
            [ENGINE, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=ENGINE_ENV, text=True, bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is None and msg.get("method"):
                NOTIFY.put(msg)
                continue
            slot = self.pending.get(rid)
            if slot:
                slot["msg"] = msg
                slot["ev"].set()

    def _err(self):
        for line in self.proc.stderr:
            sys.stderr.write("[engine] " + line.rstrip()[:300] + "\n")

    def call(self, method, params=None, timeout=180):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            slot = {"ev": threading.Event(), "msg": None}
            self.pending[rid] = slot
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        slot["ev"].wait(timeout)
        self.pending.pop(rid, None)
        return slot["msg"]


# ---------------------------------------------------------------- engine
def engine():
    global ENGINE_CLIENT
    if ENGINE_CLIENT is None or ENGINE_CLIENT.proc.poll() is not None:
        ENGINE_CLIENT = Engine()
        ENGINE_CLIENT.call("initialize", {"clientInfo": {"name": "jl-bff", "version": "0.1"},
                                              "capabilities": {"experimentalApi": True}}, 30)
    return ENGINE_CLIENT


# ---------------------------------------------------------------- handle_messages
def handle_messages(params):
    project_id = q1(params, "projectId")
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    after = int(q1(params, "after", 0))
    user_id = q1(params, "userId")
    sql = ("SELECT id, user_id, role, text, photos, trace, trace_sum, ms, task_id, created_at "
           "FROM message WHERE project_id=%s AND id > %s")
    args = [project_id, after]
    if user_id:
        sql += " AND (user_id=%s OR role='assistant')"
        args.append(int(user_id))
    sql += " ORDER BY id LIMIT 100"
    rows = q(sql, args)
    for m in rows:
        if m.get("trace") and isinstance(m["trace"], str):
            m["trace"] = json.loads(m["trace"])
        m["createdAt"] = str(m.pop("created_at", ""))
    return 200, {"code": 0, "data": {"list": rows, "lastId": rows[-1]["id"] if rows else after}}


# ---------------------------------------------------------------- handle_task_retry
def handle_task_retry(task_id):
    rows = q("SELECT id, project_id, user_id, payload, status FROM task WHERE id=%s", (task_id,))
    if not rows:
        return 404, {"code": 404, "message": "任务不存在"}
    t = rows[0]
    payload = t["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    text = (payload or {}).get("text", "")
    if not text:
        return 400, {"code": 400, "message": "原任务没有文本，无法重试"}

    new_id = exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
        "VALUES (%s,%s,%s,'chat',%s,'queued')",
        (t["project_id"], t["user_id"], f"retry-{task_id}-{int(time.time())}",
         json.dumps({"text": text}, ensure_ascii=False)))
    threading.Thread(target=run_turn,
                     args=(new_id, t["project_id"], t["user_id"], text), daemon=True).start()
    return 200, {"code": 0, "data": {"taskId": new_id, "retriedFrom": task_id}}


# ---------------------------------------------------------------- handle_task_list
def handle_task_list(params):
    limit = min(int(q1(params, "limit", 20)), 100)
    rows = q("SELECT id, project_id, user_id, status, error, created_at, finished_at "
             "FROM task ORDER BY id DESC LIMIT %s", (limit,))
    return 200, {"code": 0, "data": rows}


# ---------------------------------------------------------------- sweeper
def sweeper():
    """跑太久还挂着的任务判超时，避免永远 running"""
    while True:
        time.sleep(60)
        try:
            n = exec_sql("UPDATE task SET status='failed', error='超时（15 分钟未完成）', "
                         "finished_at=NOW(3) WHERE status='running' "
                         "AND started_at < DATE_SUB(NOW(3), INTERVAL 15 MINUTE)")
            if n:
                sys.stderr.write(f"[sweeper] {n} 个任务判超时\n")
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- pick_memories
def pick_memories(rows, query, limit=6):
    """按相关度挑记忆；都没相关时退回最常用的几条。

    这样记忆攒到几百条也不会把上下文撑爆。
    """
    if not rows:
        return []
    if not query:
        return rows[:limit]
    scored = [(relevance(r["text"], query), r) for r in rows]
    scored.sort(key=lambda x: (-x[0], -(x[1].get("hits") or 0)))
    hit = [r for sc, r in scored if sc > 0][:limit]
    if hit:
        return hit
    return rows[: min(3, len(rows))]


# ---------------------------------------------------------------- build_context
def build_context(project_id, user_id, query=None):
    """拼出这一轮要注入的上下文：项目 + 项目规矩 + 个人习惯"""
    proj = get_project(project_id)
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    # 库里的 meta 可能是字符串 "null"（json.loads → None），不能直接 dict(None)
    if not isinstance(meta, dict):
        meta = {}

    pm = q("SELECT id, text, hits FROM memory WHERE scope='project' AND owner_id=%s AND status=1 "
           "ORDER BY hits DESC, id DESC LIMIT 30", (project_id,))
    um = q("SELECT id, text, hits FROM memory WHERE scope='user' AND owner_id=%s AND status=1 "
           "ORDER BY hits DESC, id DESC LIMIT 30", (str(user_id),))

    parts = [f"【项目】{proj['name']}" + (f"（{proj['code']}）" if proj.get("code") else "")]
    if meta:
        # 不再把 21 个字段的 JSON 全塞进来（agent 需要细节时 get_project 能拿到）。
        # 这里只留每份文书都用得上的事实，且一行说完。
        bits = []
        for key, label in (("addr", ""), ("category", ""), ("structure", ""),
                           ("area", "㎡"), ("floors", "")):
            val = (meta.get(key) or "").strip() if isinstance(meta.get(key), str) else meta.get(key)
            if val:
                bits.append(f"{val}{label}")
        span = ""
        if meta.get("start") and meta.get("finish"):
            span = f"工期 {meta['start']} ~ {meta['finish']}"
        line = " · ".join(x for x in [" · ".join(bits), span] if x)
        if line:
            parts.append("【工程概况】" + line)

        who = []
        for key, label in (("owner", "建设单位"), ("builder", "施工"), ("supervisor", "监理"),
                           ("design", "设计"), ("survey", "勘察")):
            if meta.get(key):
                who.append(f"{label} {meta[key]}")
        if who:
            parts.append("【参建】" + " · ".join(who))
    pm_all, um_all = len(pm), len(um)
    pm, um = pick_memories(pm, query), pick_memories(um, query)
    sys.stderr.write(f"[ctx] 记忆候选 project={pm_all} user={um_all} → 注入 {len(pm)}+{len(um)}\n")
    if pm:
        parts.append("【项目规矩】（优先级高于个人习惯）\n" + "\n".join(f"- {m['text']}" for m in pm))
    if um:
        parts.append("【我的书写习惯】\n" + "\n".join(f"- {m['text']}" for m in um))
    # 注入即计一次命中，供后台看"哪条记忆真在用"
    try:
        ids = [m["id"] for m in pm + um if m.get("id")]
        if ids:
            exec_sql("UPDATE memory SET hits = hits + 1 WHERE id IN (%s)" % ",".join(["%s"] * len(ids)), ids)
    except Exception:
        pass

    # 规范库有多大：这是事实，而且是每轮都变的事实，不能写死在提示词里。
    # 它决定了「搜一次没有就是没有」，省掉反复换关键词的空搜（实测能省 10~30 秒）。
    try:
        n_std = q("SELECT COUNT(*) AS n FROM standard")[0]["n"]
        parts.append("【规范库】现有 %s 条条文（搜一次没有，就是没有；别换关键词反复搜）" % n_std)
    except Exception:
        pass

    # ---- 以下三行是「指针」，不是行为规则 ----
    # 工具清单不写死：跑 `jl-tool list` 永远是最新的（以前写死过，加工具忘了改）。
    parts.append(
        "【交付】出文书必须用 `jl-tool create_doc_draft` —— 它写文件、提交、入库。\n"
        "手写 md 不会出现在系统里，用户看不到（实测过）。工具报错要重试或明说，不许改成手写。\n"
        "**缺的字段留空就行**（validate_doc 会标出来，人可以在手机上补），"
        "不要为了凑齐而追问 —— 监理员在工地上没空回答五连问，先出稿。\n"
        "工具直接可用（不需要先列清单或进 shell 查）；"
        "出稿流程和说话风格见 `data/knowledge/skills/`，需要时再读。"
    )
    parts.append(f"【当前】项目 {project_id} · 用户 {user_id}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- handle_memory_list
def handle_memory_list(params, user=None):
    scope = q1(params, "scope", "project")
    owner = q1(params, "ownerId")
    if not owner:
        return 400, {"code": 400, "message": "ownerId 必填"}
    if scope == "project":
        deny = guard_project(owner, user)
        if deny:
            return deny
    rows = q("SELECT id, scope, owner_id, text, source, hits, status FROM memory "
             "WHERE scope=%s AND owner_id=%s ORDER BY status DESC, hits DESC, id DESC",
             (scope, owner))
    return 200, {"code": 0, "data": rows}


# ---------------------------------------------------------------- handle_memory_create
def handle_memory_create(body):
    scope = body.get("scope")
    owner = body.get("ownerId")
    text = (body.get("text") or "").strip()
    if scope not in ("user", "project") or not owner or not text:
        return 400, {"code": 400, "message": "scope/ownerId/text 必填"}
    mid = exec_sql("INSERT INTO memory (scope, owner_id, text, source) VALUES (%s,%s,%s,%s)",
                   (scope, str(owner), text[:512], body.get("source") or "manual"))
    rendered = _rerender_if_project(scope, str(owner))
    return 200, {"code": 0, "data": {"id": mid, "rendered": rendered}}


# ---------------------------------------------------------------- handle_memory_patch
def handle_memory_patch(mid, body):
    rows = q("SELECT id, scope, owner_id FROM memory WHERE id=%s", (mid,))
    if not rows:
        return 404, {"code": 404, "message": "记忆不存在"}
    if "text" in body:
        exec_sql("UPDATE memory SET text=%s WHERE id=%s", ((body["text"] or "")[:512], mid))
    if "status" in body:
        exec_sql("UPDATE memory SET status=%s WHERE id=%s", (1 if body["status"] else 0, mid))
    rendered = _rerender_if_project(rows[0]["scope"], rows[0]["owner_id"])
    return 200, {"code": 0, "data": {"ok": True, "rendered": rendered}}


# ---------------------------------------------------------------- handle_memory_delete
def handle_memory_delete(mid):
    rows = q("SELECT id, scope, owner_id FROM memory WHERE id=%s", (mid,))
    exec_sql("DELETE FROM memory WHERE id=%s", (mid,))
    rendered = _rerender_if_project(rows[0]["scope"], rows[0]["owner_id"]) if rows else None
    return 200, {"code": 0, "data": {"ok": True, "rendered": rendered}}


# ---------------------------------------------------------------- handle_task_cancel
def handle_task_cancel(task_id, user=None):
    rows = q("SELECT id, project_id, status FROM task WHERE id=%s", (task_id,))
    if not rows:
        return 404, {"code": 404, "message": "任务不存在"}
    t = rows[0]
    deny = guard_project(t["project_id"], user)
    if deny:
        return deny
    if t["status"] in ("done", "failed", "cancelled"):
        return 409, {"code": 409, "message": f"任务已{t['status']}，不能取消"}
    exec_sql("UPDATE task SET status='cancelled', finished_at=NOW(3) WHERE id=%s", (task_id,))
    audit(t["project_id"], user["id"] if user else 1, "task.cancel", f"task:{task_id}", "")
    return 200, {"code": 0, "data": {"id": task_id, "status": "cancelled"}}


# ---------------------------------------------------------------- handle_bootstrap
def handle_bootstrap(params, user):
    project_id = q1(params, "projectId")
    if not project_id:
        rows = q("SELECT id FROM project ORDER BY created_at LIMIT 1")
        if not rows:
            return 404, {"code": 404, "message": "还没有项目"}
        project_id = rows[0]["id"]

    deny = guard_project(project_id, user)
    if deny:
        return deny

    user_id = user["id"] if user else 1
    # 预热：打开小程序就把常驻会话和线程起好。
    # 冷启动第一单实测要 86 秒，预热后第二单只要 21 秒 —— 差别就在线程初始化。
    def _warm():
        try:
            c = _client(project_id, user_id)
            c.ensure_thread(model=os.environ.get("ENGINE_MODEL") or None)
            sys.stderr.write("[warm] %s#%s 会话已预热\n" % (project_id, user_id))
        except Exception as exc:
            sys.stderr.write("[warm] 预热失败 %s：%s\n" % (project_id, exc))
    threading.Thread(target=_warm, daemon=True).start()
    proj = get_project(project_id)
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    # meta 可能是字符串 "null"（json.loads → None），不能直接 dict(None)
    if not isinstance(meta, dict):
        meta = {}

    project_view = dict(meta)
    project_view.update({
        "id": proj["id"], "name": proj["name"],
        "code": proj.get("code") or meta.get("code") or "",
        "addr": proj.get("addr") or meta.get("addr") or "",
        "sub": (meta.get("builder") or proj.get("owner") or "")[:20],
    })

    # ---- 消息（含按天分组的时间戳）----
    msgs = []
    last_day = None
    rows = q("SELECT id, role, text, photos, trace, trace_sum, ms, sync, created_at, cites, answer "
             "FROM message WHERE project_id=%s ORDER BY id", (project_id,))
    for m in rows:
        day = m["created_at"].strftime("%Y-%m-%d")
        if day != last_day:
            last_day = day
            md, week = _day_label(m["created_at"])
            msgs.append({"id": f"s-{day}", "type": "stamp", "text": f"{'今天' if week.startswith('今天') else md} {m['created_at'].strftime('%H:%M')}"})
        if m["role"] == "user":
            msgs.append({
                "id": f"m{m['id']}", "type": "me", "text": m["text"] or "",
                "time": m["created_at"].strftime("%H:%M"), "sync": bool(m["sync"]),
                "photos": json.loads(m["photos"]) if m["photos"] and isinstance(m["photos"], str) else (m["photos"] or []),
                "voice": False,
            })
        elif m["role"] == "assistant":
            trace = m["trace"]
            if isinstance(trace, str):
                trace = json.loads(trace)
            msgs.append({
                "id": f"a{m['id']}", "type": "agent", "text": m["text"] or "",
                "time": m["created_at"].strftime("%H:%M"),
                "trace": trace or [], "traceSum": m["trace_sum"], "ms": m["ms"],
                "msLabel": ms_label(m["ms"]),
                "cites": json.loads(m["cites"]) if isinstance(m["cites"], str) else (m["cites"] or []),
                "answer": json.loads(m["answer"]) if isinstance(m["answer"], str) else (m["answer"] or None),
            })

    # ---- 记录（按天分组）----
    groups = {}
    for d in q("SELECT id, kind, obj, state, fields, created_at, path FROM doc "
               "WHERE project_id=%s ORDER BY created_at DESC", (project_id,)):
        day = d["created_at"].strftime("%Y-%m-%d")
        fields = d["fields"]
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        missing = [k for k, v in (fields or {}).items() if v in (None, "", "待补")]
        tags = []
        if missing:
            tags.append(f"缺 {len(missing)} 项")
            tags += missing[:2]
        st = STATE_TO_ST.get(d["state"], "wait")
        groups.setdefault(day, {"dt": d["created_at"], "items": []})["items"].append({
            "id": f"d{d['id']}", "kind": d["kind"], "obj": d["obj"] or "",
            "time": d["created_at"].strftime("%H:%M"), "state": d["state"], "st": st,
            "icon": KIND_TO_ICON.get(d["kind"], "doc"),
            "tint": KIND_TO_TINT.get(d["kind"], "b"),
            "tags": tags,
        })

    records = []
    for i, (day, g) in enumerate(sorted(groups.items(), reverse=True)):
        md, week = _day_label(g["dt"])
        records.append({"id": f"r{i}", "date": md, "week": week,
                        "count": len(g["items"]), "items": g["items"]})

    # ---- 资料 ----
    docs = []
    for d in q("SELECT id, kind, obj, state, created_at FROM doc WHERE project_id=%s "
               "ORDER BY created_at DESC LIMIT 30", (project_id,)):
        tone = {"待补": "wait", "待签": "wait", "已签": "ok"}.get(d["state"], "wait")
        docs.append({
            "id": f"f{d['id']}", "kind": d["kind"][:3] if d["kind"] else "文书",
            "name": f"{d['kind']} · {d['obj']}" if d["obj"] else d["kind"],
            "meta": d["created_at"].strftime("%m-%d"), "state": d["state"], "tone": tone,
            "icon": KIND_TO_ICON.get(d["kind"], "doc"), "visual": False,
        })
    for ph in q("SELECT id, sha256, place, shot_at FROM photo WHERE project_id=%s "
                "ORDER BY id DESC LIMIT 10", (project_id,)):
        docs.append({
            "id": f"p{ph['id']}", "kind": "影像",
            "name": f"{ph['place'] or '现场'} · 现场影像",
            "meta": ph["shot_at"].strftime("%m-%d %H:%M") if ph["shot_at"] else "",
            "state": "已上传", "tone": "sign", "icon": "cam", "visual": True,
        })

    # ---- 记忆 ----
    memos = [{"id": f"m{m['id']}", "text": m["text"],
              "meta": f"用了 {m['hits']} 次 · 来自我的习惯"} for m in
             q("SELECT id, text, hits FROM memory WHERE scope='user' AND owner_id=%s AND status=1 "
               "ORDER BY hits DESC, id DESC", (str(user_id),))]
    proj_memos = [{"id": f"p{m['id']}", "text": m["text"],
                   "meta": f"{m['source']} · 全项目"} for m in
                  q("SELECT id, text, source FROM memory WHERE scope='project' AND owner_id=%s AND status=1 "
                    "ORDER BY hits DESC, id DESC", (project_id,))]

    return 200, {"code": 0, "data": {
        "msgs": msgs, "records": records, "docs": docs,
        "memos": memos, "projMemos": proj_memos, "project": project_view,
    }}


# ---------------------------------------------------------------- learn_from_turn
def learn_from_turn(project_id, user_id, user_text, assistant_text, trace):
    """让模型自己判断这轮有没有值得记的习惯/规矩，写进 memory 表"""
    convo = (f"用户说：{user_text[:800]}\n\n"
             f"助手回：{assistant_text[:800]}\n\n"
             f"调用的工具：{', '.join(t.get('tool', '') for t in trace) or '无'}")
    try:
        raw = llm([
            {"role": "system", "content": [{"type": "input_text", "text": LEARN_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": convo}]},
        ], max_tokens=400, timeout=90)
    except Exception as e:
        sys.stderr.write(f"[learn] 模型调用失败: {e}\n")
        return []

    obj = parse_step(raw) or {}
    items = obj.get("memories") or []
    added, proj_changed = [], False

    for it in items[:2]:
        scope = it.get("scope")
        text = (it.get("text") or "").strip()
        if scope not in ("user", "project") or len(text) < 4:
            continue
        owner = str(user_id) if scope == "user" else project_id
        # 去重：同 owner 下已有相同或高度相似的，就跳过
        # 去重：跨 user/project 一起比 —— 同一条习惯被模型时而判成 user 时而 project，
        # 只按同 scope 比会重复。宁可少记，不要记两遍。
        existing = q("SELECT id, text FROM memory WHERE status=1 AND "
                     "((scope='user' AND owner_id=%s) OR (scope='project' AND owner_id=%s))",
                     (str(user_id), project_id))
        dup_id = None
        for e in existing:
            if similar(text, e["text"]):
                dup_id = e["id"]
                break
        if dup_id:
            exec_sql("UPDATE memory SET hits=hits+1 WHERE id=%s", (dup_id,))
            continue
        mid = exec_sql("INSERT INTO memory (scope, owner_id, text, source) VALUES (%s,%s,%s,'learned')",
                       (scope, owner, text[:512]))
        added.append({"id": mid, "scope": scope, "text": text})
        if scope == "project":
            proj_changed = True

    if proj_changed:
        try:
            render_project_memory(project_id)
        except Exception:
            traceback.print_exc()

    if added:
        sys.stderr.write(f"[learn] 学到 {len(added)} 条: " +
                         " | ".join(f"{a['scope']}:{a['text'][:20]}" for a in added) + "\n")
    return added


# ---------------------------------------------------------------- run_agent_loop
def run_agent_loop(task_id, project_id, user_id, text):
    """模型决定调哪个工具，BFF 负责执行 —— 不依赖引擎的 shell"""
    # 排队：拿不到槽位就等，任务状态停在 queued
    with TURN_SLOTS:
        exec_sql("UPDATE task SET status='running', started_at=NOW(3) WHERE id=%s", (task_id,))
        _run_agent_loop_inner(task_id, project_id, user_id, text)


# ---------------------------------------------------------------- _run_agent_loop_inner
def _run_agent_loop_inner(task_id, project_id, user_id, text):
    started = time.time()
    trace = []
    try:
        ctx = build_context(project_id, user_id, query=text)
        history = [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text",
                                          "text": ctx + "\n\n【用户说】" + text}]},
        ]

        final_text = ""
        corrected = False
        for step in range(6):
            raw = llm(history)
            sys.stderr.write(f"[loop] step{step} raw: {raw[:300]!r}\n")
            obj = parse_step(raw)

            if not obj:
                # 模型偶尔不按 JSON 回：先纠正一次，别急着把散文当结果
                if not corrected:
                    corrected = True
                    sys.stderr.write("[loop] 格式不对，纠正一次\n")
                    history.append({"role": "user", "content": [{"type": "input_text", "text":
                        "你刚才没有按约定格式输出。请只输出一个 JSON，不要任何解释："
                        '{"action":"tool","tool":"<工具名>","args":{...}} 或 '
                        '{"action":"final","text":"<回复>"}'}]})
                    continue
                # 纠正后还是不按格式：把原话当最终回复，至少别丢内容
                final_text = raw.strip()
                break

            if obj.get("action") == "tool":
                name = obj.get("tool") or ""
                args = obj.get("args") or {}
                ok, res = run_tool(name, args)
                trace.append({
                    "tool": name,
                    "icon": "search" if "search" in name else ("doc" if "create" in name else "run"),
                    "text": describe_tool(name, args) + ("" if ok else "（失败）"),
                    "args": json.dumps(args, ensure_ascii=False)[:300],
                    "ok": ok,
                })
                history.append({"role": "assistant",
                                "content": [{"type": "output_text", "text": raw.strip()}]})
                history.append({"role": "user",
                                "content": [{"type": "input_text",
                                             "text": ("工具结果：" if ok else "工具报错：") + "\n" + res}]})
                continue

            final_text = (obj.get("text") or "").strip()
            break

        if not final_text:
            final_text = "（这次没得出结果，可以再说一次）"

        ms = int((time.time() - started) * 1000)
        short = {"search_standards": "查规范", "search_records": "查记录",
                 "create_doc_draft": "写文书", "get_project": "读项目",
                 "get_context": "读上下文"}
        noise = {"list", "get_context", "get_project"}
        ok_steps = [
            t for t in trace
            if t.get("ok") and t.get("tool") not in noise
            and t.get("kind") in ("cmd", "shell", None)
        ]
        trace_sum = ("调了 " + "、".join(short.get(t["tool"], t["tool"]) for t in ok_steps[:3])) if ok_steps else None

        exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, trace_sum, ms, task_id, sync) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,%s,%s,1)",
            (project_id, user_id, f"r-{task_id}-{int(time.time())}", clean_reply(final_text),
             json.dumps(trace, ensure_ascii=False) if trace else None, trace_sum, ms, task_id))

        exec_sql("UPDATE task SET status='done', finished_at=NOW(3) WHERE id=%s", (task_id,))
        sys.stderr.write(f"[loop] task {task_id} done ({ms}ms, {len(trace)} 次工具)\n")

        # 收尾后学一手：这轮有没有值得记住的习惯/规矩
        learn_from_turn(project_id, user_id, text, final_text, trace)

    except Exception as e:
        traceback.print_exc()
        exec_sql("UPDATE task SET status='failed', error=%s, finished_at=NOW(3) WHERE id=%s",
                 (str(e)[:900], task_id))


# ---------------------------------------------------------------- _cites_from_output
def _cites_from_output(label, output):
    """从工具输出里扣出"引用了什么"，用来做论文式引用"""
    out = []
    if not output:
        return out
    try:
        data = json.loads(output)
    except Exception:
        return out
    if not isinstance(data, list):
        return out
    if label == "search_standards":
        for x in data[:3]:
            if isinstance(x, dict) and x.get("code") and x.get("clause"):
                out.append({"kind": "rule", "code": x["code"], "clause": x["clause"],
                            "title": x.get("title") or ""})
    return out


# ---------------------------------------------------------------- _parse_cli_jsonl
def _parse_cli_jsonl(stdout):
    """从 exec --json 里取：会话 id、最终文本、时间线（命令 + 模型插话）、引用

    时间线用来做"两层折叠"：外层是整轮过程，内层每条都能单独展开看细节。
    """
    thread_id, texts = None, []
    cites = []
    timeline = []          # 顺序排列的条目
    open_cmds = {}         # id -> 在 timeline 里的下标

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started":
            thread_id = ev.get("thread_id") or ev.get("threadId") or thread_id
            continue

        it = ev.get("item") or {}
        kind = (it.get("type") or "").lower()

        # ---- 模型说的话（过程里的插话/最终回复）----
        if kind == "agent_message":
            txt = (it.get("text") or "").strip()
            if txt:
                texts.append(txt)
                timeline.append({"kind": "note", "icon": "think", "text": txt[:400],
                                 "output": txt[:2000]})

        # ---- 子 agent 的活动（多 agent 时才有）----
        elif kind == "sub_agent_activity":
            stage = it.get("kind") or ""
            model = it.get("model") or ""
            if stage == "started":
                label = "派子 agent" + ("（%s）" % model if model else "")
            elif stage == "interacted":
                label = "子 agent 有反馈"
            else:
                label = "子 agent " + (stage or "活动")
            timeline.append({"kind": "note", "icon": "think", "text": label,
                             "output": json.dumps(it, ensure_ascii=False)[:600]})

        # ---- 子 agent 协作工具（spawn / wait / send_message …）----
        elif kind == "collab_tool_call":
            tool = it.get("tool") or ""
            names = {"spawn": "派子 agent", "wait": "等子 agent 回报",
                     "send_message": "给子 agent 发消息", "interrupt": "打断子 agent",
                     "close": "收掉子 agent", "list": "看有哪些子 agent"}
            timeline.append({"kind": "note", "icon": "think",
                             "text": names.get(tool, "子 agent 协作：" + (tool or "")),
                             "output": json.dumps(it, ensure_ascii=False)[:600]})

        # ---- 命令执行 ----
        elif kind == "command_execution":
            cid = it.get("id") or ""
            cmd = (it.get("command") or "").replace("\n", " ").strip()
            if not cmd:
                continue
            status = it.get("status")
            out = it.get("aggregated_output") or ""

            m = re.search(r"jl-tool\s+([a-z_]+)", cmd)
            label = m.group(1) if m else "shell"
            args = {}
            mm = re.search(r"jl-tool\s+[a-z_]+\s+'(.*)'", cmd)
            if mm:
                raw = mm.group(1).replace('\\"', '"').replace("\\\\", "\\")
                try:
                    args = json.loads(raw)
                except Exception:
                    args = {}
            title = describe_tool(label, args) if m else cmd[:60]

            if cid and cid in open_cmds:
                idx = open_cmds[cid]
                item = timeline[idx]
                if status != "in_progress":
                    item["state"] = "done"
                    item["secs"] = round(time.time() - item.pop("_t0", time.time()), 1)
                    item["output"] = out[:2000] or item.get("output") or ""
                    item["ok"] = it.get("exit_code") in (0, None)
            else:
                item = {"kind": "cmd" if m else "shell", "tool": label,
                        "icon": "search" if "search" in label else ("doc" if "create" in label else "run"),
                        "text": title + ("" if it.get("exit_code") in (0, None) else "（失败）"),
                        "cmd": cmd[:500], "output": out[:2000],
                        "args": json.dumps(args, ensure_ascii=False)[:300] if args else "",
                        "state": "done" if status != "in_progress" else "running",
                        "_t0": time.time(), "ok": it.get("exit_code") in (0, None)}
                if item["state"] == "done":
                    item["secs"] = 0
                    item.pop("_t0", None)
                timeline.append(item)
                if cid:
                    open_cmds[cid] = len(timeline) - 1

            for ci in _cites_from_output(label, out):
                if not any((x["code"], x["clause"]) == (ci["code"], ci["clause"]) for x in cites):
                    cites.append(ci)

    # 收尾：没标记完成的按完成算
    for it in timeline:
        if it.get("state") == "running":
            it["state"] = "done"
            it["secs"] = round(time.time() - it.pop("_t0", time.time()), 1)

    # 最终回复只取最后一条：前面那些是过程里的插话，拼起来会变成流水账
    final = texts[-1] if texts else ""
    return thread_id, final, timeline, cites


# ---------------------------------------------------------------- _set_progress
def _set_progress(task_id, steps):
    """把执行步骤写进任务，前端每秒拉一次就能看到进展"""
    try:
        exec_sql("UPDATE task SET progress=%s WHERE id=%s",
                 (json.dumps(steps, ensure_ascii=False), task_id))
    except Exception:
        pass


def _review_async(task_id, project_id, user_id, text, images, doc_id, final_text,
                  message_id, answer, trace, ms, trace_sum, cites, started):
    """后台跑复核，完成后回写消息并把任务置 done。

    为什么要异步：复核要 24~31 秒，而用户要的是文书本身（9~44 秒就有）。
    让复核挡在前面，等于让人干等一件"加分项"。
    """
    try:
        got = _review_pass(task_id, project_id, doc_id, final_text,
                           images=images, user_text=text, user_id=user_id)
        if got:
            review_text, rtrace, rdoc = got
            trace2 = list(trace) + list(rtrace or [])
            trace2.append({"kind": "note", "icon": "check",
                           "text": "复核意见", "output": review_text[:2000]})
            ans2 = dict(answer or {})
            ans2["review"] = review_text
            exec_sql("UPDATE message SET trace=%s, answer=%s WHERE id=%s",
                     (json.dumps(trace2, ensure_ascii=False), json.dumps(ans2, ensure_ascii=False),
                      message_id))
            sys.stderr.write("[review] task %s 复核完成 doc=%s（异步）\n" % (task_id, rdoc))
    except Exception as exc:
        sys.stderr.write("[review] 异步复核失败 task %s：%s\n" % (task_id, exc))
    finally:
        try:
            exec_sql("UPDATE task SET status='done', finished_at=NOW(3) WHERE id=%s", (task_id,))
        except Exception:
            traceback.print_exc()
        try:
            learn_from_turn(project_id, user_id, text, final_text, trace)
        except Exception:
            pass


# ---------------------------------------------------------------- _run_cli_turn
# ---------------------------------------------------------------- _review_pass
REVIEW_PROMPT = (
    "刚出完文书（doc id={doc_id}），现在做复核。\n"
    "复核要用子 agent：调 spawn_agent（这是引擎自带的工具，不是 jl-tool 命令行），"
    "agent_type 填 reviewer，把文书路径、你的关键判断、用户的原始要求都告诉它。\n"
    "它给意见后：有「必须改」的用 create_doc_draft 出新版；没有就说无需修改。\n"
    "重要问题原样列出来，别改写成你自己的话。\n"
    "如果 spawn_agent 真的调不通，就明说调不通，然后自己按同样清单审一遍 —— "
    "不要把没试过的说成不可用。"
)


def _review_pass(task_id, project_id, doc_id, first_text, images=None, user_text="", user_id=1):
    """复核：开一条独立会话，用复核员角色 + 独立模型审这份文书。

    为什么不再让主 agent 自己 spawn_agent：
      实测那个工具时不时返回 unsupported call（引擎内部路由问题），
      复核就变成了"自己审自己"，形同虚设。
      独立会话是确定能用的：新开一条线程、指定模型和角色、跑完就关。
    """
    model = (os.environ.get("REVIEW_MODEL") or "").strip() or None
    ws = WS_ROOT + "/" + project_id
    # 复核线程用一个空目录当 cwd：项目 AGENTS.md 里写着多 agent 的用法，
    # 复核员读了会跑去试 spawn_agent，而不是审文书（实测过）。
    clean_cwd = "/tmp/jl-review"
    try:
        os.makedirs(clean_cwd, exist_ok=True)
    except Exception:
        clean_cwd = None

    # 直接把文件路径给复核员。
    # 之前让它"去 docs 下找最新那份"，它只看了 docs/ 根目录，
    # 而文件在 docs/2026-11-05/ 里 —— 于是它得出"文书实质不存在"的错误结论。
    doc_path, doc_fields, doc_state = "", {}, ""
    try:
        rows = q("SELECT path, fields, state FROM doc WHERE id=%s", (doc_id,))
        if rows:
            row = rows[0]
            if row.get("path"):
                doc_path = ws + "/" + row["path"]
            f = row.get("fields")
            if isinstance(f, str):
                try:
                    f = json.loads(f)
                except Exception:
                    f = {}
            doc_fields = f or {}
            doc_state = row.get("state") or ""
    except Exception:
        pass

    # 缺项和依据直接从库里给，别让复核员自己去查/试 —— 它之前重复跑了 4 次 validate_doc
    missing = []
    try:
        if isinstance(doc_fields, dict) and doc_fields.get("kind"):
            _chk = validate_fields(doc_fields.get("kind") or "", doc_fields)
            missing = (_chk or {}).get("missing") or []
    except Exception as e:  # 不许静默：出错要让复核员知道「缺项没查到」
        print("[review] 取缺项失败 doc=%s: %r" % (doc_id, e), flush=True)
        missing = ["（缺项查询失败，请自己核对）"]
    prompt = (
        "你是独立复核会话。给你一份已经写完的监理文书，请审它。\n"
        "不要派子 agent，不要重写文书，也不要改文件 —— 只列问题。\n\n"
        "文书：doc id=" + str(doc_id) + "\n"
        "文件：" + (doc_path or (ws + "/docs（下面按日期分子目录，用 find 找最新那份）")) + "\n"
        "工作区：" + ws + "\n"
        "用户的原始要求：" + ((user_text or "").strip() or "（用户只发了图片/语音，没写文字）")[:300] + "\n"
        "这一轮用户发的图片：" + (("共 %d 张，见附件" % len(images)) if images else "无") + "\n\n"
        "已知（不用再查）：缺项 " + ("、".join(missing[:8]) if missing else "无") + "；"
        "状态 " + (doc_state or "未知") + "\n"
        "怎么审（只用 1 次工具）：\n"
        "1. 读上面这个「文件」路径（它一定存在；不要凭目录列表猜测）；\n"
        "2. 不要跑 validate_doc、也不要检索规范 —— 缺项已给你，依据按文书内容判断。\n"
        "审的重点：结论有没有事实依据、有没有自相矛盾、条号是否与内容对得上。\n"
        "输出格式：直接写审查意见本身，逐条列问题，每条标「必须改 / 建议改 / 可忽略」；"
        "没有问题就写「无需修改」。\n"
        "不要写 FINAL_ANSWER / Sender / Payload 这类信封，也不要描述你是怎么审的。"
    )
    steps = []
    try:
        text, events = run_isolated(
            project_id, prompt,
            user_id=user_id,
            model=model,
            cwd=clean_cwd,
            images=images,
            developer_instructions=(
                "你是监理文书复核员，只挑毛病，不重写文书，也不改文件。"
                "说话要短、要具体，指出哪一句站不住、缺什么。"
                "这个会话不需要派子 agent。"
            ),
        )
    except Exception as exc:
        sys.stderr.write(f"[review] 复核会话失败：{exc}\n")
        return None

    if not text:
        return None

    trace = [{"kind": "note", "icon": "think",
              "text": "复核会话（模型 %s）" % (model or "默认"), "output": ""}]
    for ev in events:
        it = (ev.get("params") or {}).get("item") or {}
        if it.get("type") == "commandExecution":
            trace.append({"kind": "shell", "icon": "run",
                          "text": ((it.get("command") or "")[:60]).replace("\n", " "),
                          "output": (it.get("aggregatedOutput") or "")[:600],
                          "state": "done", "secs": 0})
    return text, trace, doc_id


# ---------------------------------------------------------------- _run_cli_turn

def _run_cli_turn(task_id, project_id, user_id, text, images=None):
    """让 easyCode 自己跑：它读 AGENTS.md、调 jl-tool、产出文书。

    边跑边把步骤写进 task.progress —— 前端不用等全部结束才有反馈。
    """
    ws = f"{WS_ROOT}/{project_id}"
    os.makedirs(f"{ws}/.agent", exist_ok=True)
    out_file = f"{ws}/.agent/last.md"
    started = time.time()
    try:
        render_agents_md(project_id, user_id)
        try:
            os.remove(out_file)
        except FileNotFoundError:
            pass

        # 不再要求固定格式：模型用自己的话说话，结构由数据和工具给
        hint = ""
        # 把"本轮附件"标清楚：会话是复用的，历史里也有图片，
        # 不说明的话模型会把历史图片也当成这一轮的来复述（踩过）。
        if images:
            hint += ("\n\n【本轮附件】这一条消息带了 %d 张图片（已作为附件传给你）。\n"
                     "请只针对这几张回答；历史对话里出现过的图片不要重复描述，"
                     "除非用户明确问起。" % len(images))
        # 两种后端共用后面的解析：事件来源不同，事件形状一样
        proc = None
        if ENGINE_MODE == "appserver":
            line_iter = (json.dumps(ev, ensure_ascii=False) + "\n"
                         for ev in run_stream(project_id, text + hint, images, user_id=user_id))
        else:
            cmd = [ENGINE, "exec", "-C", ws,
                   "--dangerously-bypass-approvals-and-sandbox",
                   "--json", "-o", out_file]
            # 顺序要紧：`-i <FILE>...` 是变长参数，会把后面的东西全当文件名，
            # 所以 prompt 必须排在 -i 之前。
            cmd.append(text + hint)
            for img in (images or []):
                cmd += ["-i", img]

        before = q("SELECT COALESCE(MAX(id),0) AS m FROM doc WHERE project_id=%s", (project_id,))
        doc_before = before[0]["m"] if before else 0

        steps, by_cmd_id, lines = [], {}, []
        if proc is None and ENGINE_MODE != "appserver":
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1, env=ENGINE_ENV)
            line_iter = proc.stdout

        # 看门狗：进程完全不出声时 for 循环会永远等下去（之前挂满 15 分钟才被兜底扫掉）
        def _watchdog(p, secs):
            def run():
                time.sleep(secs)
                if p.poll() is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            threading.Thread(target=run, daemon=True).start()

        if proc is not None:
            _watchdog(proc, 300)
        deadline = time.time() + 600
        appserver_started = time.time()
        for line in line_iter:
            lines.append(line)
            if time.time() > deadline:
                if proc is not None:
                    proc.kill()
                raise subprocess.TimeoutExpired("easycode", 600)

            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            it = ev.get("item") or {}
            if (it.get("type") or "").lower() != "command_execution":
                continue

            cid = it.get("id") or ""
            cmdstr = (it.get("command") or "").replace("\n", " ")
            m = re.search(r"jl-tool\s+([a-z_]+)", cmdstr)
            if not m:
                continue
            label = m.group(1)
            args = {}
            mm = re.search(r"jl-tool\s+[a-z_]+\s+'(.*)'", cmdstr)
            if mm:
                try:
                    args = json.loads(mm.group(1).replace('\\"', '"'))
                except Exception:
                    args = {}
            text_label = describe_tool(label, args)
            if len(text_label) > 22:
                text_label = text_label[:21] + "…"

            now = time.time()
            if cid in by_cmd_id:
                cur = by_cmd_id[cid]
                if it.get("status") != "in_progress" and cur.get("state") != "done":
                    cur["state"] = "done"
                    cur["secs"] = round(now - cur.get("t0", now), 1)
                    cur.pop("t0", None)
            else:
                step = {"label": text_label, "tool": label,
                        "state": "running" if it.get("status") == "in_progress" else "done",
                        "t0": now}
                if step["state"] == "done":
                    step["secs"] = 0
                    step.pop("t0", None)
                steps.append(step)
                by_cmd_id[cid] = step
            _set_progress(task_id, steps)
        if proc is not None:
            proc.wait()

        now_end = time.time()
        for st in steps:
            if st.get("state") != "done":
                st["state"] = "done"
                st["secs"] = round(now_end - st.get("t0", now_end), 1)
                st.pop("t0", None)
        _set_progress(task_id, steps)

        thread_id, final_text, trace, cites = _parse_cli_jsonl("".join(lines))

        # 耗时以运行时的 progress 为准（事件流里 completed 可能没有 in_progress 配对）
        cmd_items = [t for t in trace if t.get("kind") in ("cmd", "shell")]
        for st, it in zip(steps, cmd_items):
            if st.get("secs"):
                it["secs"] = st["secs"]
        if not final_text:
            try:
                with open(out_file, encoding="utf-8") as f:
                    final_text = f.read().strip()
            except FileNotFoundError:
                pass
        if not final_text:
            final_text = "".join(lines)[-1200:].strip() or "（引擎没有返回内容）"

        ms = int((time.time() - started) * 1000)
        short = {"search_standards": "查规范", "search_records": "查记录",
                 "create_doc_draft": "写文书", "get_project": "读项目", "get_context": "读上下文"}
        noise = {"list", "get_context", "get_project"}
        ok_steps = [
            t for t in trace
            if t.get("ok") and t.get("tool") not in noise
            and t.get("kind") in ("cmd", "shell", None)
        ]
        trace_sum = ("调了 " + "、".join(short.get(t["tool"], t["tool"]) for t in ok_steps[:3])) if ok_steps else None

        # 先看这一轮有没有产出文书 —— 结构化回答以它为真值
        new_doc = q("SELECT id, kind, obj, state, fields FROM doc "
                    "WHERE project_id=%s AND id > %s ORDER BY id LIMIT 1",
                    (project_id, doc_before))
        doc_row = new_doc[0] if new_doc else None

        turn_ms = int((time.time() - started) * 1000)
        sys.stderr.write("[perf] task %s turn=%dms total=%dms cmds=%d (复核转后台)\n"
                         % (task_id, turn_ms, turn_ms, len(trace)))

        answer = build_answer(doc_row, cites, final_text)

        # 对账：文件写了但没入库的，要让人看见（不然用户以为系统里有，其实没有）
        try:
            orphans = _unregistered_docs(project_id, started)
            if orphans:
                trace.append({"kind": "note", "icon": "warn", "text": "没入库的文书",
                              "output": "\n".join(orphans[:5])})
                answer["warning"] = "%d 份文书文件没登记入库：%s" % (
                    len(orphans), "、".join(orphans[:3]))
                sys.stderr.write("[doc] 未入库文书 %d 份：%s\n" % (len(orphans), orphans[:3]))
        except Exception as exc:
            sys.stderr.write("[doc] 对账失败：%s\n" % exc)

        message_id = exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, trace_sum, ms, task_id, sync, cites, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,%s,%s,1,%s,%s)",
            (project_id, user_id, f"r-{task_id}-{int(time.time())}", clean_reply(final_text),
             json.dumps(trace, ensure_ascii=False) if trace else None, trace_sum, ms, task_id,
             json.dumps(cites, ensure_ascii=False) if cites else None,
             json.dumps(answer, ensure_ascii=False) if answer else None))

        # 有文书且开复核 → 交给后台，任务等复核完成后才置 done（前端会一直轮询到那时）
        if (ENGINE_MODE == "appserver" and doc_row
                and (os.environ.get("REVIEW_PASS") or "1") != "0" and message_id):
            threading.Thread(target=_review_async,
                             args=(task_id, project_id, user_id, text, images, doc_row["id"],
                                   final_text, message_id, answer, trace, ms, trace_sum,
                                   cites, started),
                             daemon=True).start()
            sys.stderr.write("[review] task %s 复核已转后台\n" % task_id)
            return
        exec_sql("UPDATE task SET status='done', thread_id=%s, doc_id=%s, finished_at=NOW(3) WHERE id=%s",
                 (thread_id, new_doc[0]["id"] if new_doc else None, task_id))
        sys.stderr.write(f"[cli] task {task_id} done ({ms}ms, {len(trace)} 次命令)\n")

        learn_from_turn(project_id, user_id, text, final_text, trace)

    except subprocess.TimeoutExpired:
        exec_sql("UPDATE task SET status='failed', error='超时（10 分钟）', finished_at=NOW(3) WHERE id=%s",
                 (task_id,))
        sys.stderr.write(f"[cli] task {task_id} 超时\n")
    except Exception as e:
        traceback.print_exc()
        exec_sql("UPDATE task SET status='failed', error=%s, finished_at=NOW(3) WHERE id=%s",
                 (str(e)[:900], task_id))


# ---------------------------------------------------------------- _user_lock
def _user_lock(project_id, user_id):
    key = (str(project_id), str(user_id))
    with _USER_LOCKS_GUARD:
        lk = _USER_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _USER_LOCKS[key] = lk
        return lk


# ---------------------------------------------------------------- run_turn
def run_turn(task_id, project_id, user_id, text, images=None):
    # 先排自己的队，再抢全局并发槽。
    # 这样同一个人连发三条，后两条会等前一条做完 —— 不会三个 turn 同时改同一个工作区。
    with _user_lock(project_id, user_id):
        with TURN_SLOTS:
            exec_sql("UPDATE task SET status='running', started_at=NOW(3) WHERE id=%s", (task_id,))
            # appserver 和 cli 用的是同一套"引擎跑一轮"，区别只在事件从哪来
            if ENGINE_MODE in ("cli", "appserver"):
                _run_cli_turn(task_id, project_id, user_id, text, images)
            else:
                _run_agent_loop_inner(task_id, project_id, user_id, text)


# ---------------------------------------------------------------- render_agents_md
def render_agents_md(project_id, user_id):
    """把上下文写成项目工作区里的 AGENTS.md —— 引擎每次会自动读它。

    比 additionalContext 更稳：CLI 路径没有那个参数，但 AGENTS.md 是原生机制。
    """
    ctx = build_context(project_id, user_id)
    ws = f"{WS_ROOT}/{project_id}"
    os.makedirs(f"{ws}/.agent", exist_ok=True)
    with open(f"{ws}/AGENTS.md", "w", encoding="utf-8") as f:
        f.write("# 项目上下文（由服务自动生成，勿手改）\n\n" + ctx + "\n")
    return ctx


def _unregistered_docs(project_id, started_at):
    """本轮新增但没入库的文书文件（按文件名对账，不看模型输出）"""
    ws = f"{WS_ROOT}/{project_id}"
    docs_dir = os.path.join(ws, "docs")
    if not os.path.isdir(docs_dir):
        return []
    rows = q("SELECT path FROM doc WHERE project_id=%s", (project_id,))
    known = {(r.get("path") or "").split("/")[-1] for r in rows}
    out = []
    for root, _dirs, files in os.walk(docs_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            try:
                if os.path.getmtime(full) < started_at:
                    continue
            except OSError:
                continue
            if fn not in known:
                out.append(fn)
    return out


# ---------------------------------------------------------------- _parse_jsonl
def _parse_jsonl(stdout):
    """从 exec --json 的输出里捞出：会话 id、助手文本、工具调用"""
    thread_id, texts, trace = None, [], []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id") or ev.get("threadId") or thread_id
            continue
        it = ev.get("item") or {}
        it_type = (it.get("type") or "").lower()
        if it_type in ("agent_message", "agentmessage"):
            txt = it.get("text") or ""
            if txt:
                texts.append(txt)
        elif "command" in it_type or "tool" in it_type or "exec" in it_type:
            name = it.get("command") or it.get("name") or it_type
            if isinstance(name, list):
                name = " ".join(str(x) for x in name)
            name = str(name).replace("\n", " ").strip()
            # 只留关键信息：jl-tool 的哪个子命令
            m = re.search(r"jl-tool\s+([a-z_]+)", name)
            label = m.group(1) if m else name[:60]
            icon = "search" if "search" in label else ("doc" if "create" in label else "run")
            trace.append({"tool": label, "icon": icon, "text": label,
                          "cmd": name[:200], "ms": it.get("duration_ms")})
    return thread_id, "\n".join(t2 for t2 in texts if t2.strip()).strip(), trace


# ---------------------------------------------------------------- run_cli_turn
def run_cli_turn(task_id, project_id, user_id, text):
    """一个任务 = 一次 easycode exec 子进程（不走 app-server，避开 code-mode）"""
    ws = f"{WS_ROOT}/{project_id}"
    out_file = f"{ws}/.agent/last.md"
    started = time.time()
    try:
        render_agents_md(project_id, user_id)
        try:
            os.remove(out_file)
        except FileNotFoundError:
            pass

        hint = (
            "\n\n[输出要求] 最后一句话请严格写成三行，不要写别的东西：\n"
            "结论：一句话说做了什么、结论是什么\n"
            "依据：规范号 + 条号；没有就写无\n"
            "待补：还缺哪些字段，用、隔开；没有就写无"
        )
        cmd = [ENGINE, "exec", "-C", ws,
               "--dangerously-bypass-approvals-and-sandbox",
               "--json", "-o", out_file, text + hint]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=ENGINE_ENV)

        thread_id, text_out, trace = _parse_jsonl(p.stdout)
        if not text_out:
            try:
                with open(out_file, encoding="utf-8") as f:
                    text_out = f.read().strip()
            except FileNotFoundError:
                pass
        if not text_out:
            text_out = (p.stdout or "")[-1500:].strip() or "（模型没有返回内容）"

        ms = int((time.time() - started) * 1000)
        short = {"search_standards": "查规范", "search_records": "查记录",
                 "create_doc_draft": "写文书", "get_project": "读项目",
                 "get_context": "读上下文"}
        noise = {"list", "get_context", "get_project"}
        ok_steps = [
            t for t in trace
            if t.get("ok") and t.get("tool") not in noise
            and t.get("kind") in ("cmd", "shell", None)
        ]
        trace_sum = ("调了 " + "、".join(short.get(t["tool"], t["tool"]) for t in ok_steps[:3])) if ok_steps else None

        exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, trace_sum, ms, task_id, sync, cites) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,%s,%s,1,%s)",
            (project_id, user_id, f"r-{task_id}-{int(time.time())}", clean_reply(text_out),
             json.dumps(trace, ensure_ascii=False) if trace else None, trace_sum, ms, task_id,
             json.dumps(cites, ensure_ascii=False) if cites else None))

        if thread_id and re.match(r"^[a-zA-Z0-9_-]{8,64}$", thread_id):
            exec_sql("UPDATE thread_map SET thread_id=%s, last_active_at=NOW() "
                     "WHERE project_id=%s AND user_id=%s",
                     (thread_id, project_id, user_id))

        failed = p.returncode not in (0,)
        exec_sql("UPDATE task SET status=%s, error=%s, finished_at=NOW(3), thread_id=%s WHERE id=%s",
                 ("failed" if failed else "done",
                  (p.stderr or "")[-500:] if failed else None, thread_id, task_id))
        sys.stderr.write(f"[turn] task {task_id} -> {'failed' if failed else 'done'} "
                         f"({ms}ms, trace {len(trace)})\n")

    except subprocess.TimeoutExpired:
        exec_sql("UPDATE task SET status='failed', error='超时（10 分钟）', finished_at=NOW(3) WHERE id=%s",
                 (task_id,))
        sys.stderr.write(f"[turn] task {task_id} 超时\n")
    except Exception as e:
        traceback.print_exc()
        exec_sql("UPDATE task SET status='failed', error=%s, finished_at=NOW(3) WHERE id=%s",
                 (str(e)[:900], task_id))


# ---------------------------------------------------------------- handle_chat
def handle_chat(body, headers=None):
    project_id = body.get("projectId")
    user_id = int(body.get("userId") or 1)
    client_id = body.get("clientId") or f"c-{uuid.uuid4().hex[:16]}"

    # parts 是新的内容模型：文字 / 图片 / 语音 / 定位，任意组合。
    # 老客户端只发 text，这里兼容。
    parts = body.get("parts")
    if not isinstance(parts, list) or not parts:
        parts = [{"type": "text", "text": body.get("text") or ""}]

    # 语音：没带转写就现转
    for part in parts:
        if part.get("type") == "audio" and not part.get("transcript"):
            got, err = transcribe_audio(part.get("blob") or part.get("sha"))
            part["transcript"] = got or ""
            if err:
                part["asrNote"] = err

    text = "".join(p.get("text") or "" for p in parts if p.get("type") == "text").strip()
    transcripts = [p.get("transcript") for p in parts
                   if p.get("type") == "audio" and p.get("transcript")]
    images = [p for p in parts if p.get("type") == "image"]

    if not project_id or not (text or transcripts or images):
        return 400, {"code": 400, "message": "projectId 和 内容 必填"}

    # 语音转写要进 prompt，否则模型只知道"有段录音"
    prompt_text = text
    for t in transcripts:
        prompt_text = (prompt_text + "\n\n" if prompt_text else "") + "[语音转写] " + t

    deny = guard_project(project_id, resolve_token(bearer(headers)))
    if deny:
        return deny

    get_project(project_id)

    dup = q("SELECT id, task_id FROM message WHERE client_id=%s", (client_id,))
    if dup:
        return 200, {"code": 0, "data": {"duplicated": True, "messageId": dup[0]["id"],
                                        "taskId": dup[0].get("task_id")}}

    task_id = exec_sql(
        "INSERT INTO task (project_id, user_id, client_id, kind, payload, status) "
        "VALUES (%s,%s,%s,'chat',%s,'queued')",
        (project_id, user_id, client_id,
         json.dumps({"text": prompt_text, "transcripts": transcripts,
                     "images": [i.get("blob") or i.get("sha") for i in images]},
                    ensure_ascii=False)))

    display = text or ("[语音] " + ("；".join(transcripts) if transcripts else "")) or "[图片]"
    exec_sql("INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync, parts) "
             "VALUES (%s,%s,%s,'user',%s,%s,1,%s)",
             (project_id, user_id, client_id, display, task_id,
              json.dumps(parts, ensure_ascii=False)))

    # 立刻返回，任务在后台跑 —— 手机断了也照跑
    # 图片落成真实文件，交给引擎（exec -i），模型直接看到，不用调工具
    img_files = materialize_images(parts)
    threading.Thread(target=run_turn,
                     args=(task_id, project_id, user_id, prompt_text, img_files),
                     daemon=True).start()

    return 202, {"code": 0, "data": {
        "taskId": task_id, "status": "running", "poll": f"/api/v1/tasks/{task_id}"}}


# ---------------------------------------------------------------- handle_task
def handle_task(task_id, user=None):
    rows = q("SELECT * FROM task WHERE id=%s", (task_id,))
    if not rows:
        return 404, {"code": 404, "message": "任务不存在"}
    t = rows[0]
    deny = guard_project(t["project_id"], user)
    if deny:
        return deny
    msgs = q("SELECT id, role, text, trace, trace_sum, ms, cites, answer FROM message WHERE task_id=%s ORDER BY id",
             (task_id,))
    # 排队位置：同一个人还有几条排在前面
    ahead = q("SELECT COUNT(*) AS n FROM task WHERE project_id=%s AND user_id=%s "
              "AND status IN ('queued','running') AND id < %s",
              (t["project_id"], t["user_id"], task_id))
    queue_ahead = ahead[0]["n"] if ahead else 0

    progress = t.get("progress")
    if isinstance(progress, str):
        try:
            progress = json.loads(progress)
        except Exception:
            progress = None

    # 这一轮产出的文书（按任务开始时间之后新建的那份）
    artifact = None
    if t.get("doc_id"):
        rows = q("SELECT id, kind, obj, state, path, git_commit FROM doc WHERE id=%s",
                 (t["doc_id"],))
        if rows:
            d = rows[0]
            artifact = {"id": d["id"], "kind": d["kind"], "obj": d["obj"], "state": d["state"],
                        "path": d["path"], "commit": (d["git_commit"] or "")[:10],
                        "name": f"{d['kind']}" + (f" · {d['obj']}" if d["obj"] else "")}

    return 200, {"code": 0, "data": {
        "progress": progress,
        "queueAhead": queue_ahead,
        "artifact": artifact,
        "id": t["id"], "status": t["status"], "error": t["error"],
        "threadId": t["thread_id"], "turnId": t["turn_id"],
        "createdAt": str(t["created_at"]), "finishedAt": str(t["finished_at"]),
        "messages": [
            {"id": m["id"], "role": m["role"], "text": m["text"],
             "trace": json.loads(m["trace"]) if m["trace"] else None,
                          "traceSum": m["trace_sum"], "ms": m["ms"], "msLabel": ms_label(m["ms"]),
             "cites": json.loads(m["cites"]) if isinstance(m["cites"], str) else (m["cites"] or []),
             "answer": json.loads(m["answer"]) if isinstance(m["answer"], str) else (m["answer"] or None)}
            for m in msgs
        ],
    }}


