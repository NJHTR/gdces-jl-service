import contextlib
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
from .core import (ADMIN_HTML, HOMES_ROOT, ALLOWED_TOOLS, AUTH_REQUIRED, BLOB_ROOT, BOLD_RE, ENGINE, ENGINE_CLIENT, ENGINE_ENV, ENGINE_MODE, ENV, GAP_INLINE_RE, GAP_SPLIT_RE, JL_TOOL_BIN, KIND_TO_ICON, KIND_TO_TINT, LEARN_PROMPT, LLM_MODEL, LLM_URL, MAX_CONCURRENT, NOTIFY, NUM_RE, PORT, REF_RE, ROOT, STATE_TO_ST, SYSTEM_PROMPT, TOKEN_TTL_HOURS, TURN_SLOTS, WEEK, WS_ROOT, _USER_LOCKS, _USER_LOCKS_GUARD, _bigrams, _day_label, _hash_token, _rerender_if_project, _split_num, allowed_tools, bearer, build_answer, clean_reply, db, describe_tool, exec_sql, extract_from_items, get_project, handle_login, handle_logout, issue_token, llm, load_env, ms_label, notify_worker, on_notification, parse_step, q, q1, resolve_token, rich_reply, rich_segments, run_tool, similar, split_gaps, transcribe_audio, validate_fields, materialize_images, tool_manual)
from .biz import (append_manifest, audit, update_doc_file, default_project_id, guard_project, handle_admin, handle_audit, handle_blob, handle_doc_get, handle_doc_patch, handle_docs_list, handle_geo_reverse, handle_photo_list, handle_photo_patch, handle_project_create, handle_project_get, handle_project_list, handle_project_save, handle_standard, handle_standards_search, handle_stats, handle_upload, handle_weather, member_of, render_project_memory, render_user_memory, store_blob)
from .memcandidate import extract_memory_candidates


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
def _photos_from_parts(parts):
    """从消息 parts 里还原图片 URL。聊天历史只存 blob sha，不还原就永远显示 [图片]。"""
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except Exception:
            parts = []
    out = []
    for p in (parts or []):
        if not isinstance(p, dict):
            continue
        if (p.get("type") or "").lower() != "image":
            continue
        blob = (p.get("blob") or p.get("sha") or "").strip()
        if blob:
            out.append("/api/v1/blobs/" + blob)
    return out


def _normalize_message_photos(m):
    photos = m.get("photos")
    if isinstance(photos, str):
        try:
            photos = json.loads(photos)
        except Exception:
            photos = []
    photos = photos or []
    if not photos:
        photos = _photos_from_parts(m.pop("parts", None))
    else:
        m.pop("parts", None)
    m["photos"] = photos
    return m


def handle_messages(params):
    project_id = q1(params, "projectId")
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    after = int(q1(params, "after", 0))
    user_id = q1(params, "userId")
    sql = ("SELECT id, user_id, role, text, photos, parts, trace, trace_sum, ms, task_id, created_at "
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
        _normalize_message_photos(m)
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


# ---------------------------------------------------------------- build_context
def build_context(project_id, user_id, query=None):
    """拼出这一轮的常驻上下文：只有项目事实 + 指针。

    知识一律不进这里 —— 它们都在文件里，模型按需读：
      项目规矩  <ws>/records/rules.md            （人写的，git 版本化）
      影像索引  <ws>/records/photos.md           （看过现场图后追加一句描述）
      流程技能  .easycode/skills/<name>/SKILL.md （引擎自动列目录，正文按需）
      个人记忆  $EASYCODE_HOME/memories/         （引擎自动注入摘要，详情按需）
    判断标准：这句话会不会因为加工具、换模型而失效？会 → 不许写。
    """
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

    # 规范库有多大：这是事实，而且是每轮都变的事实，不能写死在提示词里。
    # 它决定了「搜一次没有就是没有」，省掉反复换关键词的空搜（实测能省 10~30 秒）。
    try:
        n_std = q("SELECT COUNT(*) AS n FROM standard")[0]["n"]
        parts.append("【规范库】现有 %s 条条文（搜一次没有，就是没有；别换关键词反复搜）" % n_std)
    except Exception as e:
        # 不许静默：这条事实缺失会让 agent 反复搜规范
        print("[ctx] 取规范条数失败: %r" % (e,), flush=True)

    # ---- 以下是事实与指针，不是行为规则 ----
    # 工具清单不写死：跑 `jl-tool list` 永远是最新的（以前写死过，加工具忘了改）。
    parts.append("【入库】文书只有经 `jl-tool create_doc_draft` 才会出现在系统里；"
                 "手写 md 用户看不到。")
    # 能力事实（不是行为规则）：告诉它规范在哪、怎么取。
    # 没有这一行，模型会在工作区 rg 十几遍找不到，然后凭记忆背条号（实测 task 278）。
    parts.append("【规范】条文在库里，用 `jl-tool search_standards` 查（工作区没有规范全文）。")

    # 指针：只说「东西在哪」，不描述怎么做。文件不存在就不写，省得模型白找。
    ws = f"{WS_ROOT}/{project_id}"
    here = []
    if os.path.exists(f"{ws}/records/rules.md"):
        here.append("项目规矩 `records/rules.md`（这个项目约定怎么写、怎么签；按需读）")
    if os.path.exists(f"{ws}/records/photos.md"):
        here.append("现场影像索引 `records/photos.md`（按部位分组；问'这个部位以前查过什么'先看这里）")
    try:
        if os.path.isdir(f"{ws}/.easycode/skills") and os.listdir(f"{ws}/.easycode/skills"):
            here.append("项目技能 `.easycode/skills/`（目录见系统技能清单，正文按需读）")
    except OSError:
        pass
    # 引用格式属于流程：放技能、按需读。这里只给指针，不写格式本身。
    try:
        if os.path.isdir(f"{ws}/.easycode/skills/jl-cite"):
            here.append("引用写法 `.easycode/skills/jl-cite/SKILL.md`（要标出处时读）")
    except OSError as e:
        sys.stderr.write("[ctx] 引用技能检查失败: %r\n" % (e,))

    mem_base = os.path.join(HOMES_ROOT, str(user_id), "memories")
    mine = []
    if os.path.isfile(os.path.join(mem_base, "memory_summary.md")):
        mine.append("摘要 `%s`" % os.path.join(mem_base, "memory_summary.md"))
    if os.path.isfile(os.path.join(mem_base, "MEMORY.md")):
        mine.append("跨项目记忆 `%s`" % os.path.join(mem_base, "MEMORY.md"))
    # BFF 渲染的个人记忆视图（memory 表是账本，这个是可读文件）
    user_mem = os.path.abspath(os.path.join(WS_ROOT, "..", "memory", "users",
                                             str(user_id), "MEMORY.md"))
    if os.path.isfile(user_mem):
        mine.append("我的记忆 `%s`" % user_mem)
    # Phase2 个人域摘要（去重/归并后的总览）：AGENTS.md 只放指针，不整份注入
    user_sum = os.path.abspath(os.path.join(WS_ROOT, "..", "memory", "users",
                                             str(user_id), "memory_summary.md"))
    if os.path.isfile(user_sum):
        mine.append("记忆摘要 `%s`（去重后的个人习惯总览，按需读）" % user_sum)
    if os.environ.get("JL_ENGINE_MEMORY", "0") == "1":
        proj_mem = os.path.join(mem_base, "projects", "%s.md" % project_id)
    else:
        proj_mem = os.path.join(WS_ROOT, "..", "memory", "projects",
                                str(user_id), "%s.md" % project_id)
    if os.path.isfile(proj_mem):
        mine.append("本项目个人记忆 `%s`" % proj_mem)
    if mine:
        parts.append("【我的记忆】" + " · ".join(mine) + "（按需读，别全量复述）")

    # 注意：records/rules.md 与 records/photos.md 的指针已经在上面的 here 里加过一次，
    # 这里不要再 append（QA R3：以前同一个指针出现两次，AGENTS.md 里重复）。
    if os.path.isdir(f"{ws}/knowledge"):
        here.insert(0, "组织知识 `knowledge/`（模板、填写示例、验收要点；只读）")
    if here:
        parts.append("【在哪找】" + " · ".join(here))

    parts.append("【侦查纪律】出文书前先自己找证据：get_context 看项目；"
                 "读 records/rules.md 和 records/photos.md；"
                 "search_records/search_photos 查同一部位/同类文书；"
                 "search_standards 查规范；缺关键信息先问，不要空稿。"
                 "历史同类文书就是模板，别等别人喂。")

    parts.append(f"【当前】项目 {project_id} · 用户 {user_id}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- handle_memory_recall
def handle_memory_recall(params, user=None):
    """按关键词找记忆（项目规矩 + 我的记忆 + 我在这个项目的习惯）。

    命中即 hits+1：这条被检索出来，就算"用上了"。
    检索是字面匹配（不引向量），宁可少召回，也不假装懂。
    """
    pid = q1(params, "projectId")
    uid = q1(params, "userId") or (user or {}).get("id")
    query = (q1(params, "q") or "").strip()
    if not pid or not query:
        return 400, {"code": 400, "message": "projectId 和 q 必填"}
    # QA R1：登录用户只能 recall 自己的个人记忆；传别人的 userId 直接 403。
    if user and uid and str(uid) != str(user.get("id")):
        return 403, {"code": 403, "message": "只能查询自己的记忆"}
    deny = guard_project(pid, user)
    if deny:
        return deny

    tokens = [t for t in re.split(r"[\s,，。；;、]+", query) if len(t) >= 2][:6]
    if not tokens:
        tokens = [query[:8]]
    like = " OR ".join(["text LIKE %s"] * len(tokens))
    args = ["%" + t + "%" for t in tokens]
    rows = q("SELECT id, scope, owner_id, project_id, text, hits, applies_to FROM memory "
             "WHERE status=1 AND (" + like + ") AND ("
             "(scope='project' AND owner_id=%s) OR "
             "(scope='user' AND owner_id=%s AND (project_id=%s OR project_id IS NULL))"
             ") ORDER BY hits DESC, id DESC LIMIT 6",
             tuple(args) + (pid, str(uid), pid))
    for r in rows:
        try:
            exec_sql("UPDATE memory SET hits=hits+1, updated_at=NOW(3) WHERE id=%s",
                     (r["id"],))
            r["hits"] = int(r.get("hits") or 0) + 1
        except Exception as e:
            sys.stderr.write("[mem] hits+1 失败 id=%s: %r\n" % (r.get("id"), e))
    return 200, {"code": 0, "data": rows}


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
    # QA R1：个人记忆只有本人可按 ownerId 查；登录用户查他人一律 403（演示匿名保持旧行为）
    if scope == "user" and user and str(owner) != str(user.get("id")):
        return 403, {"code": 403, "message": "只能查看自己的记忆"}
    pid = q1(params, "projectId")
    where = "scope=%s AND owner_id=%s" + (" AND project_id=%s" if pid else "")
    args = (scope, owner, pid) if pid else (scope, owner)
    rows = q("SELECT id, scope, owner_id, project_id, text, source, source_ref, applies_to, "
             "hits, status, created_at, updated_at FROM memory "
             "WHERE " + where + " ORDER BY status DESC, hits DESC, id DESC", args)
    return 200, {"code": 0, "data": rows}


# ---------------------------------------------------------------- handle_memory_create
def handle_memory_create(body, user=None):
    """写记忆。项目规矩只进 proposal；个人记忆只能写自己的（QA R1）。"""
    scope = body.get("scope")
    owner = body.get("ownerId")
    text = (body.get("text") or "").strip()
    if scope not in ("user", "project") or not owner or not text:
        return 400, {"code": 400, "message": "scope/ownerId/text 必填"}
    # QA R1：登录用户不能替别人写个人记忆
    if scope == "user" and user and str(owner) != str(user.get("id")):
        return 403, {"code": 403, "message": "只能写自己的记忆"}
    kept = text[:512]
    # 作用域三档：项目级 / 个人×某项目 / 个人跨项目
    pid = (body.get("projectId") or "").strip()
    if scope == "project":
        pid = str(owner)
        # 项目规矩不能由任何一个成员直接写生效；先进入待确认队列。
        # 确认后由 handle_rule_decide 写 memory + 渲染 records/rules.md。
        deny = guard_project(pid, user)
        if deny:
            return deny
        try:
            dup = q("SELECT id, status FROM rule_proposal "
                    "WHERE project_id=%s AND text=%s LIMIT 1", (pid, kept))
            if dup:
                return 200, {"code": 0, "data": {
                    "id": dup[0]["id"], "status": dup[0]["status"],
                    "pending": dup[0]["status"] == "pending", "duplicated": True}}
            rid = exec_sql(
                "INSERT INTO rule_proposal (project_id, user_id, text, source, source_ref) "
                "VALUES (%s,%s,%s,%s,%s)",
                (pid, None, kept, (body.get("source") or "manual").strip()[:32],
                 (body.get("sourceRef") or "").strip()[:64] or None))
            return 200, {"code": 0, "data": {
                "id": rid, "status": "pending", "pending": True, "proposal": True}}
        except Exception as e:
            sys.stderr.write("[memory] 建项目规矩提案失败: %r\n" % (e,))
            return 500, {"code": 500, "message": "项目规矩提案没写进去，请重试"}
    if scope == "user" and pid:
        applies_to = "user:%s@project:%s" % (owner, pid)
    else:
        applies_to = "%s:%s" % (scope, owner)
    source_ref = (body.get("sourceRef") or "").strip() or None
    # 完全相同的条目只加使用次数，不新增行（近义合并交给整理/人工，不在这里猜）
    try:
        dup = q("SELECT id FROM memory WHERE scope=%s AND owner_id=%s AND text=%s "
                "AND COALESCE(project_id,'')=%s LIMIT 1",
                (scope, str(owner), kept, pid))
        if dup:
            exec_sql("UPDATE memory SET hits=hits+1, status=1, updated_at=NOW(3) WHERE id=%s",
                     (dup[0]["id"],))
            _rerender_if_project(scope, str(owner))
            if scope == "user":
                render_user_memory(owner)
            return 200, {"code": 0, "data": {"id": dup[0]["id"], "duplicated": True,
                                             "saved_to": "", "rendered": None,
                                             "applies_to": applies_to}}
    except Exception as e:
        sys.stderr.write("[memory] 查重失败: %r\n" % (e,))

    # 记忆只有一个写者：memory 表。
    # 项目规矩的 records/rules.md 是**渲染视图**（像 AGENTS.md），由 render_rules_md 生成；
    # 个人笔记写进引擎的 notes 通道（整理时消费）。
    note = ""
    try:
        if scope == "project":
            from .biz import render_rules_md
            note = render_rules_md(str(owner)) or ""
        elif pid:                      # 个人 × 某个项目
            from .biz import render_user_project_md
            note = render_user_project_md(owner, pid) or ""
        else:
            d = os.path.join(HOMES_ROOT, str(owner), "memories", "extensions", "ad_hoc", "notes")
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(d, "%s-%s.md" % (stamp, uuid.uuid4().hex[:6]))
            with open(path, "w", encoding="utf-8") as f:
                f.write("- %s\n" % kept)
            note = path
    except OSError as e:
        sys.stderr.write("[memory] 写文件失败 scope=%s owner=%s: %r\n" % (scope, owner, e))

    mid = exec_sql(
        "INSERT INTO memory (scope, owner_id, project_id, text, source, source_ref, "
        "applies_to, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(3))",
        (scope, str(owner), pid or None, kept, body.get("source") or "manual",
         source_ref, applies_to))
    rendered = _rerender_if_project(scope, str(owner))
    if scope == "user":
        render_user_memory(owner)
    # 个人×项目：必须在 INSERT 之后再渲染（渲染是查表生成的，先渲染会写成"（空）"）
    if scope == "user" and pid:
        from .biz import render_user_project_md
        note = render_user_project_md(owner, pid) or note
    return 200, {"code": 0, "data": {"id": mid, "saved_to": note, "rendered": rendered,
                                     "applies_to": applies_to}}


# ---------------------------------------------------------------- handle_memory_patch
def _memory_guard(rows, user):
    """记忆按 scope 归属：项目记忆校验项目成员，个人记忆只允许本人。"""
    if not rows:
        return None
    m = rows[0]
    if (m.get("scope") or "") == "project":
        deny = guard_project(m.get("owner_id") or "", user)
        if deny:
            return deny
        from .biz import can_confirm_project_rule
        if not can_confirm_project_rule(m.get("owner_id") or "", user):
            return 403, {"code": 403, "message": "项目规矩只能由总监/项目管理员修改"}
        return None
    if user and str(m.get("owner_id") or "") != str(user.get("id")):
        return 403, {"code": 403, "message": "只能改自己的记忆"}
    return None


def handle_memory_patch(mid, body, user=None):
    rows = q("SELECT id, scope, owner_id, project_id FROM memory WHERE id=%s", (mid,))
    if not rows:
        return 404, {"code": 404, "message": "记忆不存在"}
    deny = _memory_guard(rows, user)
    if deny:
        return deny
    if "text" in body:
        exec_sql("UPDATE memory SET text=%s, updated_at=NOW(3) WHERE id=%s",
                 ((body["text"] or "")[:512], mid))
    if "status" in body:
        exec_sql("UPDATE memory SET status=%s, updated_at=NOW(3) WHERE id=%s",
                 (1 if body["status"] else 0, mid))
    rendered = _rerender_if_project(rows[0]["scope"], rows[0]["owner_id"])
    if rows[0].get("scope") == "user":
        render_user_memory(rows[0]["owner_id"])
    if rows[0].get("project_id"):
        from .biz import render_user_project_md
        rendered = render_user_project_md(rows[0]["owner_id"], rows[0]["project_id"]) or rendered
    return 200, {"code": 0, "data": {"ok": True, "rendered": rendered}}


# ---------------------------------------------------------------- handle_memory_delete
def handle_memory_delete(mid, user=None):
    rows = q("SELECT id, scope, owner_id, project_id FROM memory WHERE id=%s", (mid,))
    deny = _memory_guard(rows, user)
    if deny:
        return deny
    exec_sql("DELETE FROM memory WHERE id=%s", (mid,))
    rendered = _rerender_if_project(rows[0]["scope"], rows[0]["owner_id"]) if rows else None
    if rows and rows[0].get("scope") == "user":
        render_user_memory(rows[0]["owner_id"])
    # 个人×项目的行删掉后，那个人的项目记忆文件也要重渲染（否则文件里还留着）
    if rows and rows[0].get("project_id"):
        from .biz import render_user_project_md
        rendered = render_user_project_md(rows[0]["owner_id"],
                                          rows[0]["project_id"]) or rendered
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
    # Phase2：个人域摘要（确定性、幂等；候选不计入）。打开小程序即刷新，
    # 保证接下来 build_context/AGENTS.md 的指针有文件可指。
    try:
        from .memorysum import render_user_summary
        render_user_summary(user_id)
    except Exception:
        traceback.print_exc()
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
    rows = q("SELECT id, role, text, photos, parts, trace, trace_sum, ms, sync, created_at, cites, answer, task_id "
             "FROM message WHERE project_id=%s ORDER BY id", (project_id,))
    # 旧消息的 answer 里没有 artifact，但 task.doc_id 还在；批量补出来，
    # 否则重新进页面后聊天里的文书卡片会消失。
    artifact_by_task = {}
    task_ids = sorted({m["task_id"] for m in rows if m.get("task_id") and m.get("role") == "assistant"})
    if task_ids:
        try:
            ph = ",".join(["%s"] * len(task_ids))
            for r in q("SELECT t.id AS task_id, d.id, d.kind, d.obj, d.state, d.path, d.git_commit "
                       "FROM task t JOIN doc d ON d.id=t.doc_id WHERE t.id IN (" + ph + ")",
                       tuple(task_ids)):
                artifact_by_task[r["task_id"]] = {
                    "id": r["id"], "kind": r["kind"] or "文书", "obj": r["obj"] or "",
                    "state": r["state"] or "", "path": r["path"] or "",
                    "commit": (r["git_commit"] or "")[:10],
                    "name": (r["kind"] or "文书") + (" · " + r["obj"] if r["obj"] else ""),
                }
        except Exception as e:
            sys.stderr.write("[bootstrap] 补历史文书卡失败: %r\n" % (e,))
    for m in rows:
        day = m["created_at"].strftime("%Y-%m-%d")
        if day != last_day:
            last_day = day
            md, week = _day_label(m["created_at"])
            msgs.append({"id": f"s-{day}", "type": "stamp", "text": f"{'今天' if week.startswith('今天') else md} {m['created_at'].strftime('%H:%M')}"})
        if m["role"] == "user":
            photos = m["photos"]
            if isinstance(photos, str):
                try:
                    photos = json.loads(photos)
                except Exception:
                    photos = []
            raw_parts = m.get("parts")
            if isinstance(raw_parts, str):
                try:
                    raw_parts = json.loads(raw_parts)
                except Exception:
                    raw_parts = []
            if not isinstance(raw_parts, list):
                raw_parts = []
            photos = photos or _photos_from_parts(raw_parts)
            audio = next((p for p in raw_parts
                          if isinstance(p, dict) and p.get("type") == "audio"), None)
            msgs.append({
                "id": f"m{m['id']}", "type": "me",
                # 语音消息不把服务端给你看的 [语音]/[空消息] 当成正文
                "text": "" if audio else (m["text"] or ""),
                "time": m["created_at"].strftime("%H:%M"), "sync": bool(m["sync"]),
                "photos": photos,
                "voice": bool(audio),
                "dur": (audio or {}).get("dur") or ("0:00" if audio else ""),
                "audioSha": (audio or {}).get("blob") or "",
                "transcript": (audio or {}).get("transcript") or "",
                "transcriptShown": False,
            })
        elif m["role"] == "assistant":
            trace = m["trace"]
            if isinstance(trace, str):
                trace = json.loads(trace)
            answer = m["answer"]
            if isinstance(answer, str):
                try:
                    answer = json.loads(answer) if answer else None
                except Exception:
                    answer = None
            artifact = (answer or {}).get("artifact") if isinstance(answer, dict) else None
            if not artifact:
                artifact = artifact_by_task.get(m.get("task_id"))
            msgs.append({
                "id": f"a{m['id']}", "type": "agent", "text": m["text"] or "",
                "time": m["created_at"].strftime("%H:%M"),
                "trace": trace or [], "traceSum": m["trace_sum"], "ms": m["ms"],
                "msLabel": ms_label(m["ms"]),
                "cites": json.loads(m["cites"]) if isinstance(m["cites"], str) else (m["cites"] or []),
                "answer": answer,
                "artifact": artifact,
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
               "ORDER BY created_at DESC LIMIT 200", (project_id,)):
        tone = {"待补": "wait", "待签": "wait", "已签": "ok"}.get(d["state"], "wait")
        docs.append({
            "id": f"f{d['id']}", "kind": d["kind"][:3] if d["kind"] else "文书",
            "name": f"{d['kind']} · {d['obj']}" if d["obj"] else d["kind"],
            "meta": d["created_at"].strftime("%m-%d"), "state": d["state"], "tone": tone,
            "icon": KIND_TO_ICON.get(d["kind"], "doc"), "visual": False,
        })
    for ph in q("SELECT id, sha256, place, shot_at FROM photo WHERE project_id=%s "
                "ORDER BY id DESC LIMIT 100", (project_id,)):
        docs.append({
            "id": f"p{ph['id']}", "kind": "影像",
            "name": f"{ph['place'] or '现场'} · 现场影像",
            "meta": ph["shot_at"].strftime("%m-%d %H:%M") if ph["shot_at"] else "",
            "state": "已上传", "tone": "sign", "icon": "cam", "visual": True,
            "sha256": ph.get("sha256") or "",
            "url": "/api/v1/blobs/" + (ph.get("sha256") or ""),
        })

    # ---- 记忆 ----
    # 元数据交给前端展示（来源/次数/是否生效）；停用的也返回，才能"恢复"
    memos = [{"id": f"m{m['id']}", "text": m["text"], "source": m["source"],
              "projectId": m.get("project_id"),
              "hits": m["hits"], "status": m["status"], "appliesTo": m["applies_to"]}
             for m in q("SELECT id, text, source, project_id, hits, status, applies_to FROM memory "
                        "WHERE scope='user' AND owner_id=%s "
                        "ORDER BY status DESC, hits DESC, id DESC", (str(user_id),))]
    proj_memos = [{"id": f"p{m['id']}", "text": m["text"], "source": m["source"],
                   "hits": m["hits"], "status": m["status"], "appliesTo": m["applies_to"]}
                  for m in q("SELECT id, text, source, hits, status, applies_to FROM memory "
                             "WHERE scope='project' AND owner_id=%s "
                             "ORDER BY status DESC, hits DESC, id DESC", (project_id,))]

    # 待确认的规矩提案：模型提议的东西先放这里，人点了才生效
    pending_rules = q("SELECT id, text, source, evidence, created_at FROM rule_proposal "
                      "WHERE project_id=%s AND status='pending' ORDER BY id DESC LIMIT 20",
                      (project_id,))
    for _r in pending_rules:
        _r["createdAt"] = str(_r.get("created_at") or "")

    return 200, {"code": 0, "data": {
        "msgs": msgs, "records": records, "docs": docs,
        "memos": memos, "projMemos": proj_memos, "project": project_view,
        "pendingRules": pending_rules,
    }}


# ---------------------------------------------------------------- learn_from_turn
def learn_from_turn(project_id, user_id, user_text, assistant_text, trace):
    """模型抽取式学习。**默认关闭**。

    为什么关：记忆只能有一个写者。个人记忆交给引擎整理（homes/<uid>/memories），
    项目规矩交给 memory 表（rules.md 是渲染视图）。这里再写一遍 = 三方抢写、
    而且用字面相似度去重，越写越脏。需要临时验证再开 MEMORY_LEARN=1。
    """
    if os.environ.get("MEMORY_LEARN", "0") != "1":
        return []
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
        # 项目级抽取只能进候选，不能直接写生效的 memory。
        if scope == "project":
            try:
                dup = q("SELECT id FROM rule_proposal WHERE project_id=%s AND text=%s LIMIT 1",
                        (project_id, text[:512]))
                if not dup:
                    rid = exec_sql(
                        "INSERT INTO rule_proposal (project_id, user_id, text, source) "
                        "VALUES (%s,%s,%s,'learned')",
                        (project_id, str(user_id), text[:512]))
                    added.append({"id": rid, "scope": "proposal", "text": text[:512]})
            except Exception as e:
                sys.stderr.write("[learn] 写项目候选失败: %r\n" % (e,))
            continue
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


# ---------------------------------------------------------------- _post_turn_learn
def _post_turn_learn(project_id, user_id, user_text, assistant_text, trace):
    """任务收尾的统一学习入口。

    · MEMORY_LEARN=1：旧学习路径（默认关；直接写 memory 的历史开关，别动默认值）
    · MEMORY_EXTRACT=1：Phase1 候选抽取（默认关；只写 memory_candidate 的 pending）
    两条都必须"学习失败不影响任务 done"。
    """
    try:
        learn_from_turn(project_id, user_id, user_text, assistant_text, trace)
    except Exception:
        traceback.print_exc()
    try:
        got = extract_memory_candidates(project_id, user_id, user_text, assistant_text, trace)
        if got:
            sys.stderr.write("[memcand] 抽取候选 %d 条: " % len(got) +
                             " | ".join("%s:%s" % (g["scope"], g["text"][:20]) for g in got) + "\n")
    except Exception:
        traceback.print_exc()


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
        _post_turn_learn(project_id, user_id, text, final_text, trace)

    except Exception as e:
        traceback.print_exc()
        exec_sql("UPDATE task SET status='failed', error=%s, finished_at=NOW(3) WHERE id=%s",
                 (str(e)[:900], task_id))


# ---------------------------------------------------------------- _drain_cites
def _drain_cites(ws, since):
    """把工具这一轮写下的引用取走（读 <ws>/.agent/cites.jsonl 然后删掉）。

    为什么要这样：app-server 的 commandExecution item 里没有输出字段
    （实测 keys 只有 type/id/pluginId/scriptPath/command/cwd/processId/source），
    命令输出不回传给客户端，所以引用只能由工具侧落盘、这里取走。
    """
    fp = os.path.join(ws, ".agent", "cites.jsonl")
    out = []
    try:
        if not os.path.exists(fp):
            return out
        with open(fp, encoding="utf-8") as f:
            lines = f.readlines()
        os.remove(fp)
    except OSError as e:
        sys.stderr.write("[cite] 取引用失败 %s: %r\n" % (fp, e))
        return out

    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if float(d.get("ts") or 0) < since - 1:
            continue
        key = (d.get("code"), d.get("clause"))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append({
            "kind": "standard",
            "ref": "standard://%s#%s" % (d.get("code"), d.get("clause")),
            "std": (d.get("std") or "").strip(),
            "code": d.get("code"),
            "clause": d.get("clause"),
            "title": (d.get("title") or "").strip(),
            "quote": (d.get("quote") or "").strip(),
            "source": "标准原文（公司购买版）",
        })
    return out[:5]


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
        for x in data[:5]:
            if not (isinstance(x, dict) and x.get("code") and x.get("clause")):
                continue
            body = (x.get("body") or "").strip().replace("\n", " ")
            out.append({
                "kind": "rule",
                "std": (x.get("std_title") or "").strip(),
                "code": x["code"],
                "clause": x["clause"],
                "title": (x.get("title") or "").strip(),
                # 摘录：像论文引用那样给一句原文，够读者判断，不是全文
                "quote": body[:140],
                "source": "标准原文（公司购买版）",
            })
    return out


# ---------------------------------------------------------------- _plain_trace
def _plain_trace(t):
    """过程面板里的文字：去掉 markdown 记号（**、反引号、井号），不做排版。"""
    t = (t or "").replace("**", "").replace("`", "")
    t = t.replace("### ", "").replace("## ", "").replace("# ", "")
    return t.strip()


# ---------------------------------------------------------------- _cite_from_entry
CITE_KIND_PREFIX = (
    ("standard://", "standard"),
    ("thread://", "thread"),
    ("web://", "web"),
)


def _read_span(ws, path, start, end, limit=40):
    """按行号读一小段原文当摘录（读不到就返回空，不报错）。

    只读被引用的那几行，不读全文：摘录 ≤140 字，够读者判断。
    """
    if not path or start is None or end is None:
        return ""
    if end < start:
        start, end = end, start
    end = min(end, start + limit)
    cands = []
    if path.startswith("/"):
        cands.append(path)
    elif path.startswith("homes/") or path.startswith("data/"):
        cands.append(os.path.join(ROOT, path))
    else:
        cands.append(os.path.join(ws or "", path))
    for fp in cands:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        seg = [ln.strip() for ln in lines[start - 1:end] if ln.strip()]
        if seg:
            return " ".join(seg)[:140]
    return ""


def _cite_from_entry(ws, e):
    """把引擎的一条 entry（path/lineStart/lineEnd/note）归一成我们的引用结构。"""
    path = (e.get("path") or "").strip()
    note = (e.get("note") or "").strip()
    try:
        start = int(e.get("lineStart") or e.get("line_start") or 0)
        end = int(e.get("lineEnd") or e.get("line_end") or 0)
    except (TypeError, ValueError):
        start = end = 0

    kind, ref = "file", path
    for prefix, k in CITE_KIND_PREFIX:
        if path.startswith(prefix):
            kind, ref = k, path[len(prefix):]
            break
    else:
        base = os.path.basename(path)
        if path.endswith("records/photos.md"):
            kind = "photo"
        elif "/memories/" in path or base in ("MEMORY.md", "memory_summary.md"):
            kind = "memory"

    item = {"kind": kind, "ref": ref, "path": path, "note": note,
            "lineStart": start, "lineEnd": end,
            "source": "模型声明"}

    if kind == "standard":
        # ref 形如 JGJ 130-2011#6.4.3 → 统一成 standard://CODE#CLAUSE
        code, _, clause = ref.partition("#")
        item["code"], item["clause"] = code.strip(), clause.strip()
        item["ref"] = "standard://%s#%s" % (item["code"], item["clause"])
        item["std"], item["quote"] = "", ""
        try:
            rows = q("SELECT title, std_title, body FROM standard WHERE code=%s AND clause=%s",
                     (item["code"], item["clause"]))
            if rows:
                r = rows[0]
                item["std"] = (r.get("std_title") or "").strip()
                item["title"] = (r.get("title") or "").strip()
                item["quote"] = (r.get("body") or "").strip().replace("\n", " ")[:140]
                item["source"] = "标准库"
        except Exception as exc:
            sys.stderr.write("[cite] 查条文失败 %s#%s: %r\n" % (item["code"], item["clause"], exc))
    elif kind in ("file", "photo", "memory"):
        item["quote"] = _read_span(ws, path, start, end)
    elif kind == "web":
        item["quote"] = note      # 搜索结果不回传，只能用它自己的话
        item["source"] = "联网检索（未核验）"
    elif kind == "thread":
        item["quote"] = note
        item["source"] = "本轮对话"
    return item


# ---------------------------------------------------------------- _parse_cli_jsonl
def _parse_cli_jsonl(stdout, ctx=None):
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
                timeline.append({"kind": "note", "icon": "think",
                                 "text": _plain_trace(txt)[:400],
                                 "output": txt[:2000]})
            # 引擎已把 <oai-mem-citation> 从正文剥掉，结构化放在这里
            mc = it.get("memoryCitation") or it.get("memory_citation") or {}
            ws = (ctx or {}).get("ws")
            for _e in (mc.get("entries") or []):
                if not isinstance(_e, dict):
                    continue
                _ci = _cite_from_entry(ws, _e)
                if not any((x.get("kind"), x.get("ref")) == (_ci["kind"], _ci["ref"])
                           for x in cites):
                    cites.append(_ci)

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

        # ---- 联网搜索（引擎自己调，结果不回传，只能看到 query）----
        elif kind == "web_search":
            q = it.get("query") or ""
            action = it.get("action") or {}
            qs = action.get("queries") or ([q] if q else [])
            text_label = "联网搜索：" + "；".join([x for x in qs if x][:3] or ["（未给查询词）"])
            timeline.append({"kind": "note", "icon": "search", "text": text_label,
                             "output": json.dumps(it, ensure_ascii=False)[:600]})

        # ---- 命令执行 ----
        elif kind == "command_execution":
            cid = it.get("id") or ""
            cmd = (it.get("command") or "").replace("\n", " ").strip()
            if not cmd:
                continue
            status = it.get("status")
            # 引擎字段是 camelCase（实测 keys：aggregatedOutput / exitCode / durationMs）。
            # 以前读成下划线 → 输出永远空、退出码永远 None → 失败也显示成功。
            out = it.get("aggregatedOutput") or ""
            exit_code = it.get("exitCode")
            ok_flag = exit_code in (0, None)

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
                    item["ok"] = ok_flag
            else:
                item = {"kind": "cmd" if m else "shell", "tool": label,
                        "icon": "search" if "search" in label else ("doc" if "create" in label else "run"),
                        "text": title + ("" if ok_flag else "（失败）"),
                        "cmd": cmd[:500], "output": out[:2000],
                        "args": json.dumps(args, ensure_ascii=False)[:300] if args else "",
                        "state": "done" if status != "in_progress" else "running",
                        "_t0": time.time(), "ok": ok_flag}
                if item["state"] == "done":
                    item["secs"] = 0
                    item.pop("_t0", None)
                timeline.append(item)
                if cid:
                    open_cmds[cid] = len(timeline) - 1

            for ci in _cites_from_output(label, out):
                if not any((x["code"], x["clause"]) == (ci["code"], ci["clause"]) for x in cites):
                    ci["n"] = len(cites) + 1
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
        # 先确认这条消息真的属于本单 —— 实测出现过复核内容被写进别一单的情况
        owner = q("SELECT task_id FROM message WHERE id=%s", (message_id,))
        if not owner or str(owner[0]["task_id"]) != str(task_id):
            print("[review] 消息归属不符，跳过回写：message=%s 属于 task=%s，本单=%s"
                  % (message_id, (owner[0]["task_id"] if owner else "?"), task_id), flush=True)
            return
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
        # 复核结果写成 task.result，客户端轮询 /tasks/<id> 就能看到"复核中→复核完成"
        state = {"review": "failed"}
        try:
            rv = q("SELECT verdict FROM review WHERE doc_id=%s ORDER BY id DESC LIMIT 1",
                   (doc_id,)) if doc_id else []
            if rv:
                state = {"review": "done", "verdict": rv[0]["verdict"]}
            elif not doc_id:
                state = {"review": "skipped"}
        except Exception:
            traceback.print_exc()
        try:
            # doc_id 必须在这里写：走复核分支时主路径已经 return 了，
            # 那边那条 UPDATE 根本不会执行 → task.doc_id 永远 NULL
            # → /api/v1/tasks/<id> 的 artifact 永远为空（客户端拿不到这一单的文书）。
            exec_sql("UPDATE task SET status='done', doc_id=COALESCE(doc_id, %s), "
                     "result=%s, finished_at=COALESCE(finished_at, NOW(3)) WHERE id=%s",
                     (doc_id, json.dumps(state, ensure_ascii=False), task_id))
        except Exception:
            traceback.print_exc()
        try:
            _post_turn_learn(project_id, user_id, text, final_text, trace)
        except Exception as e:
            # 这条绝对不能静默：学习功能曾 403 几个月没人发现（R5）
            print("[learn] 学习失败 project=%s user=%s: %r" % (project_id, user_id, e), flush=True)


# ---------------------------------------------------------------- _review_pass
# 注：这里曾有一个 REVIEW_PROMPT 常量，让 agent 调 spawn_agent 找子 agent 复核。
# 现在已经改成 BFF 自己开独立会话（_review_pass 里内联提示），那段成了死代码，
# 而且里面还写着「agent_type 填 reviewer」这种与现状矛盾的话 —— 删掉，免得以后被带偏。

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
    # 按任务隔离：以前所有复核共用一个 /tmp/jl-review，并发时会互相踩
    clean_cwd = "/tmp/jl-review-%s" % task_id
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
    except Exception as e:
        print("[review] 取文书字段失败 doc=%s: %r" % (doc_id, e), flush=True)

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
        "你要做的不只是挑毛病，还要反驳它。必须回答三个问题：\n"
        "  ① 还有别的解释吗？（例：构件缺失 —— 是没装、拆了没恢复，还是设计变更后取消？）\n"
        "  ② 依据条文的适用边界在哪？（例：JGJ 130 是扣件式钢管脚手架；本项目若是盘扣式，这条不能直接引）\n"
        "  ③ 反过来读成立吗？（例：结论写「必须停工」—— 现有证据支持停工，还是只支持「限期整改」？）\n"
        "每条意见都必须带 locator（出处定位符），没有出处的意见不算数。locator 形如：\n"
        "  standard://JGJ 130-2011#6.4.3:1-1\n"
        "  knowledge/<文件>:<起>-<止>\n"
        "  records/photos.md:<行>-<行>\n\n"
        "最后必须调用工具把判定落库，这是唯一有效的交付方式：\n"
        "  jl-tool record_review '{\"docId\": <本单id>, \"verdict\": \"must_fix|suggest|ok\", "
        "\"findings\": [{\"why\": \"...\", \"locator\": \"...\", \"tag\": \"短标签\"}], "
        "\"counter\": [{\"reading\": \"...\", \"locator\": \"...\"}], "
        "\"alternatives\": [\"...\"], \"uncertainty\": \"...\"}'\n"
        "verdict 只能是 must_fix（不改不能签）/ suggest（建议改）/ ok（可签）。\n"
        "每条 findings 必带 tag：2-6 个字，同类问题必须用同一个词\n"
        "（例如 依据缺失 / 事实不足 / 条号不符 / 期限不合理 / 责任越界）。\n"
        "同一个 tag 反复出现，就会被攒成一条「疑似缺流程」的观察，交给人决定。\n"
        "工具返回成功后，再用两三句话说明最要紧的一两条；不要复述整份文书。"
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
                "你是监理文书复核员：既要挑毛病，也要反驳。"
                "必须给出替代解释、条文适用边界与反向读法，每条意见都要带 locator。"
                "最后必须调用 record_review 把判定落库（不调用等于没交）。"
                "不重写文书，也不改文件；说话要短、要具体。这个会话不需要派 sub-agent。"
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
    # 按任务隔离：以前是项目级共享的 last.md，同一项目两个人在跑时会互相覆盖
    out_file = f"{ws}/.agent/last-{task_id}.md"
    started = time.time()
    # 本轮标记：工具登记文书时读它，把 doc 挂到这条任务上（不再靠时间窗认领）
    try:
        with open(f"{ws}/.agent/turn-{user_id}.json", "w", encoding="utf-8") as f:
            json.dump({"taskId": task_id, "userId": user_id, "ts": started},
                      f, ensure_ascii=False)
    except OSError as e:
        sys.stderr.write("[doc] 写轮次标记失败 %s: %r\n" % (ws, e))
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
            prev_thread = q("SELECT thread_id FROM task WHERE project_id=%s AND user_id=%s "
                            "AND thread_id IS NOT NULL AND thread_id<>'' "
                            "ORDER BY id DESC LIMIT 1", (project_id, user_id))
            thread_hint = prev_thread[0]["thread_id"] if prev_thread else None
            line_iter = (json.dumps(ev, ensure_ascii=False) + "\n"
                         for ev in run_stream(project_id, text + hint, images,
                                              user_id=user_id, thread_id=thread_hint))
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

        thread_id, final_text, trace, cites = _parse_cli_jsonl("".join(lines), ctx={"ws": ws})

        # 过程面板不该重复一遍最终回复：最后一条 note 和 final 一样就去掉
        if trace and final_text:
            last = trace[-1]
            if last.get("kind") == "note" and \
                    _plain_trace(last.get("text") or "")[:80] == _plain_trace(final_text)[:80]:
                trace.pop()

        # 工具侧落盘的引用（命令输出不回传，只能这么取）
        def _ck(c):
            """去重键：文件类用 ref，规范类用 standard://CODE#CLAUSE"""
            r = c.get("ref")
            if not r and c.get("code") and c.get("clause"):
                r = "standard://%s#%s" % (c["code"], c["clause"])
            return (c.get("kind"), r)

        for _ci in _drain_cites(ws, started):
            if not any(_ck(x) == _ck(_ci) for x in cites):
                cites.append(_ci)

        # 记忆引用：[m:12] 是模型自己标的"这条我用上了"。
        # 只做机械解析；没标的就当没用过，不虚报"依据你的习惯"。
        if final_text:
            from .memref import strip_mem_refs
            final_text, _mids = strip_mem_refs(final_text)
            for _mid in _mids:
                try:
                    _rows = q("SELECT id, text, source, scope FROM memory WHERE id=%s", (_mid,))
                    if not _rows:
                        continue
                    _m = _rows[0]
                    exec_sql("UPDATE memory SET hits=hits+1, updated_at=NOW(3) WHERE id=%s",
                             (_mid,))
                    _q = (_m.get("text") or "")[:80]
                    cites.append({"kind": "memory", "ref": "memory:%s" % _mid,
                                  "quote": _q, "note": "你记过的"})
                except Exception as _e:
                    sys.stderr.write("[mem] 记录记忆引用失败 id=%s: %r\n" % (_mid, _e))

        # 统一编号：模型声明在前、工具实测在后，编号从 1 连续
        for _i, _c in enumerate(cites, 1):
            _c["n"] = _i

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
            # 读完就删：按任务隔离后不清会一直堆在项目目录里
            with contextlib.suppress(OSError):
                os.remove(out_file)
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

        # 先看这一轮有没有产出文书 —— 结构化回答以它为真值。
        # 优先按 task_id 认领（工具登记时带的），避免同项目并发时认错文书；
        # 老工具没带 taskId 时才退回时间窗（并把原因写日志）。
        new_doc = q("SELECT id, kind, obj, state, fields FROM doc "
                    "WHERE project_id=%s AND task_id=%s ORDER BY id DESC LIMIT 1",
                    (project_id, task_id))
        if not new_doc:
            new_doc = q("SELECT id, kind, obj, state, fields FROM doc "
                        "WHERE project_id=%s AND id > %s ORDER BY id LIMIT 1",
                        (project_id, doc_before))
            if new_doc:
                sys.stderr.write("[doc] task %s 未按 task_id 认领，退回时间窗（doc %s）\n"
                                 % (task_id, new_doc[0]["id"]))
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

        # 有文书且开复核 → 复核是加分项，**不能挡 done**：
        # 先落 done（用户马上能拿到文书），再把复核丢后台；结果回写 message.answer.review
        # 和 task.result.review（客户端据此显示"复核中/复核通过/需修改"）。
        review_on = bool(doc_row and (os.environ.get("REVIEW_PASS") or "1") != "0" and message_id)
        done_doc = doc_row["id"] if doc_row else (new_doc[0]["id"] if new_doc else None)
        exec_sql("UPDATE task SET status='done', thread_id=%s, doc_id=%s, result=%s, "
                 "finished_at=NOW(3) WHERE id=%s",
                 (thread_id, done_doc,
                  json.dumps({"review": "pending"}) if review_on else None, task_id))
        sys.stderr.write(f"[cli] task {task_id} done ({ms}ms, {len(trace)} 次命令, "
                         f"review={'pending' if review_on else 'off'})\n")

        if review_on:
            threading.Thread(target=_review_async,
                             args=(task_id, project_id, user_id, text, images, doc_row["id"],
                                   final_text, message_id, answer, trace, ms, trace_sum,
                                   cites, started),
                             daemon=True).start()
            sys.stderr.write("[review] task %s 复核已转后台（message=%s doc=%s）\n"
                             % (task_id, message_id, doc_row["id"]))
            return

        _post_turn_learn(project_id, user_id, text, final_text, trace)

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
def ensure_ws_links(project_id):
    """把组织知识挂进项目工作区：<ws>/knowledge -> data/knowledge（只读）。

    为什么需要：agent 的 cwd 是工作区，`knowledge/...` 相对路径才找得到；
    绝对路径能写但提示词里难看、也容易写错（以前指针写的是 data/knowledge，
    在工作区里根本不存在）。
    """
    ws = f"{WS_ROOT}/{project_id}"
    # 只挂 kb/：规范全文不进工作区（否则模型会去 rg 全文，绕开工具、拿不到条号）
    src = os.path.join(ROOT, "data", "knowledge", "kb")
    dst = os.path.join(ws, "knowledge")
    if os.path.islink(dst):
        # 目标变了要重建（软链从 knowledge 根改到 kb/ 时，旧链会一直指着老地方）
        try:
            if os.path.realpath(dst) != os.path.realpath(src):
                os.unlink(dst)
            else:
                return
        except OSError as e:
            sys.stderr.write("[ws] 检查旧软链失败 %s: %r\n" % (dst, e))
            return
    if os.path.exists(dst):
        return                     # 已经是真目录（有人手动建过），不覆盖
    try:
        os.symlink(src, dst)
    except OSError as e:
        sys.stderr.write("[ws] 建 knowledge 软链失败 %s: %r\n" % (dst, e))


HUMAN_START = "<!-- human:start -->"
HUMAN_END = "<!-- human:end -->"
HUMAN_SEED = "\n（这个项目的人工约定写在这里，服务重写 AGENTS.md 时不会覆盖）\n"


def render_agents_md(project_id, user_id):
    """把上下文写成项目工作区里的 AGENTS.md —— 引擎每次会自动读它。

    分成两个区：
      generated —— 服务生成，每次重写
      human     —— 人写的，永不覆盖

    这样 AGENTS.md 能进 git、能 diff、能 review；机器生成的部分不会被误当成人写的。
    """
    ws = f"{WS_ROOT}/{project_id}"
    os.makedirs(f"{ws}/.agent", exist_ok=True)
    ensure_ws_links(project_id)
    ctx = build_context(project_id, user_id)
    path = f"{ws}/AGENTS.md"

    human = HUMAN_SEED
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
        # 不用正则：拿标记前后切两刀就够了（也给硬编码基线省一条）
        if HUMAN_START in old and HUMAN_END in old:
            human = old.split(HUMAN_START, 1)[1].split(HUMAN_END, 1)[0] or HUMAN_SEED
    except FileNotFoundError:
        human = HUMAN_SEED   # 第一次生成：给人写区留个位

    body = (
        "<!-- generated:start 由服务生成，勿手改 -->\n"
        "# 项目上下文\n\n" + ctx + "\n"
        "<!-- generated:end -->\n\n"
        + HUMAN_START + human + HUMAN_END + "\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return ctx


def refresh_all_agents(user_id=1):
    """把所有项目的 AGENTS.md 重写一遍。

    为什么要这个：AGENTS.md 只在「这个项目跑过任务」时才重写，
    于是改了模板之后，久没用的项目还挂着旧版（yun-gu-3qi 一直是 2778 字节的老版本）。
    """
    done = []
    try:
        rows = q("SELECT id FROM project ORDER BY id")
    except Exception as e:
        print("[agents] 取项目失败: %r" % (e,), flush=True)
        return done
    for row in rows:
        pid = row.get("id") or row.get("project_id")
        if not pid:
            continue
        try:
            render_agents_md(pid, user_id)
            try:
                from .knowledge import install_skills
                install_skills(project_ids=[pid], user_ids=[user_id])
            except Exception as e:
                print("[skills] 安装失败 %s: %r" % (pid, e), flush=True)
            done.append(pid)
        except Exception as e:
            print("[agents] %s 重写失败: %r" % (pid, e), flush=True)
    if done:
        print("[agents] 已重写 %d 个项目: %s" % (len(done), ",".join(done)), flush=True)
    return done


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


# ---------------------------------------------------------------- handle_chat
def handle_chat(body, headers=None):
    project_id = body.get("projectId")
    # 用户只能来自 token。以前用 body.userId —— 伪造一下就能把会话/记忆
    # 写进别人的 home，整套「一人一个 EASYCODE_HOME」的隔离被一个参数绕开。
    _u = resolve_token(bearer(headers)) if headers else None
    raw_uid = (_u or {}).get("id") or body.get("userId") or 1
    try:
        user_id = int(raw_uid)
    except (TypeError, ValueError):
        # 旧演示会话/手机预览可能带 userId="demo"；不能因此 500。
        sys.stderr.write("[chat] 非数字 userId=%r，回落 1\n" % (raw_uid,))
        user_id = 1
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
    audios = [p for p in parts if p.get("type") == "audio"]

    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    # 语音是合法内容：ASR 偶发没识别出文字时，不能把它当成“没内容”拒掉。
    if not (text or transcripts or images or audios):
        return 400, {"code": 400, "message": "内容必填（文字、图片或语音都没有）"}

    # 语音转写要进 prompt，否则模型只知道"有段录音"
    prompt_text = text
    for t in transcripts:
        prompt_text = (prompt_text + "\n\n" if prompt_text else "") + "[语音转写] " + t
    if audios and not transcripts:
        prompt_text = (prompt_text + "\n\n" if prompt_text else "") + \
            "[用户发来一段语音，ASR 暂时没识别出文字；先按收到语音处理，必要时请用户再说一遍或补文字]"

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

    # 按 parts 组装：以前用 or 链，图片消息 transcripts 为空时
    # "[语音] " + "" 是真值 → 图片被标成语音（实测）
    _bits = []
    if text:
        _bits.append(text)
    if transcripts:
        _bits.append("[语音] " + "；".join(transcripts))
    if images:
        _bits.append("[图片]×%d" % len(images))
    display = " ".join(_bits) or "[空消息]"
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

    def _no_source(msgs):
        """答复里提到库里的标准号/标准名，但这一单没调过 search_standards → 标记未溯源。

        做法是拿库里的标准号做字符串包含判断，不是解析模型输出结构。
        """
        called = False
        texts = []
        has_cites = False
        for m in msgs or []:
            if m.get("role") != "assistant":
                continue
            texts.append(m.get("text") or "")
            # 已经有结构化引用（引擎 memoryCitation / 工具账本）就不算"未溯源"
            try:
                _c = m.get("cites")
                if isinstance(_c, str):
                    _c = json.loads(_c or "[]")
                if _c:
                    has_cites = True
            except (ValueError, TypeError) as e:
                sys.stderr.write("[cite] cites 解析失败 消息%s: %r\n" % (m.get("id"), e))
            trace = m.get("trace")
            if isinstance(trace, str):
                try:
                    trace = json.loads(trace)
                except (ValueError, TypeError):
                    trace = []
            for it in trace or []:
                if isinstance(it, dict) and (it.get("tool") or "") == "search_standards":
                    called = True
        if called or has_cites or not texts:
            return False
        blob = "\n".join(texts)
        try:
            rows = q("SELECT code, std_title FROM standard")
        except Exception as e:
            sys.stderr.write("[cite] 取标准号失败: %r\n" % (e,))
            return False
        for r in rows:
            code = (r.get("code") or "").strip()
            std = (r.get("std_title") or "").strip()
            if (code and code in blob) or (std and std in blob):
                return True
        return False

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

    # 复核状态：pending（已转后台）/ done（带 verdict）/ failed / skipped / off（没开复核）
    review_state, review_verdict = "off", None
    raw = t.get("result")
    try:
        rr = json.loads(raw) if isinstance(raw, str) else (raw or {})
        review_state = (rr.get("review") or "off").strip() or "off"
        review_verdict = rr.get("verdict")
    except (ValueError, TypeError):
        review_state = "off"

    return 200, {"code": 0, "data": {
        "progress": progress,
        "reviewStatus": review_state,
        "reviewVerdict": review_verdict,
        "queueAhead": queue_ahead,
        "artifact": artifact,
        "noSource": _no_source(msgs),
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


