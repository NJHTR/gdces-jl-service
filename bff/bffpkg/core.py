import base64
import sys
sys.path.insert(0, "/home/baize/.local/lib/python3.14/site-packages")

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

"""公共底座：配置、数据库、文本清洗、分段。谁都可以 import。"""


# ---------------------------------------------------------------- stmt_2
"""监理 AI 服务 · BFF

职责：
  1. 鉴权占位 / 多租户（project_id + user_id）
  2. 契约翻译：CHAT-1 → easyCode app-server 的 thread/start + turn/start
  3. 落库：task / message
  4. 断网续跑的基础：任务状态机（这里先落 queued/running/done/failed）

只用标准库 + pymysql。
"""


# ---------------------------------------------------------------- ROOT
ROOT = "/home/baize/jl-service"


# ---------------------------------------------------------------- ENGINE
ENGINE = f"{ROOT}/repos/easycode/.target/release/easycode"


# ---------------------------------------------------------------- WS_ROOT
WS_ROOT = f"{ROOT}/data/workspaces"
# 每个用户一个引擎家目录（记忆/配置/技能都在里面）
HOMES_ROOT = f"{ROOT}/data/homes"


# ---------------------------------------------------------------- PORT
PORT = int(os.environ.get("BFF_PORT", "8081"))


# ---------------------------------------------------------------- stmt_34
sys.path.insert(0, "/home/baize/.local/lib/python3.14/site-packages")


# ---------------------------------------------------------------- load_env
def load_env():
    cfg = {}
    with open(f"{ROOT}/.env", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


# ---------------------------------------------------------------- ENV
ENV = load_env()


# ---------------------------------------------------------------- ENGINE_ENV
ENGINE_ENV = dict(
    os.environ,
    HOME="/home/baize",
    PATH="/home/baize/bin:" + os.environ.get("PATH", ""),
)


# ---------------------------------------------------------------- stmt_57
# 旧中转的 key 不再注入（线路已统一到百炼，注入它只会让引擎能误连旧线路）

# 备选线路（阿里云百炼）：env_key 里只写变量名，值在这里注入
for _k in ("DASHSCOPE_API_KEY", "HUAGONG_VLM_TOKEN"):
    if ENV.get(_k):
        ENGINE_ENV[_k] = ENV[_k]


# ---------------------------------------------------------------- db
def db():
    return pymysql.connect(
        host="127.0.0.1",
        port=3309,
        user="jl",
        password=ENV["MYSQL_PASSWORD"],
        database="jl",
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ---------------------------------------------------------------- q1
def q1(params, key, default=None):
    """parse_qs 的值是列表，这里取第一个"""
    v = params.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return default if v is None or v == "" else v


# ---------------------------------------------------------------- q
def q(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


# ---------------------------------------------------------------- exec_sql
def exec_sql(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


# ---------------------------------------------------------------- exec_rowcount
def exec_rowcount(sql, args=()):
    """执行写语句并返回**受影响行数**。

    为什么单独一个：exec_sql 返回 cur.lastrowid，对 UPDATE 恒为 0。
    claim_due 用 `n == 1` 判断"这次认领成功"，拿 lastrowid 就永远领不到
    —— 整个 service_job 调度器等于没启动（实测 2026-11-06）。
    """
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


# ---------------------------------------------------------------- NOTIFY
NOTIFY = queue.Queue()


# ---------------------------------------------------------------- MAX_CONCURRENT
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))


# ---------------------------------------------------------------- TURN_SLOTS
TURN_SLOTS = threading.Semaphore(MAX_CONCURRENT)


# ---------------------------------------------------------------- ENGINE_CLIENT
ENGINE_CLIENT = None


# ---------------------------------------------------------------- AUTH_REQUIRED
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0") == "1"


# ---------------------------------------------------------------- TOKEN_TTL_HOURS
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "720"))  # 30 天


# ---------------------------------------------------------------- BLOB_ROOT
BLOB_ROOT = f"{ROOT}/data/blobs"


# ---------------------------------------------------------------- WEEK
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- STATE_TO_ST
STATE_TO_ST = {"待补": "wait", "待签": "sign", "已签": "ok"}


# ---------------------------------------------------------------- KIND_TO_ICON
KIND_TO_ICON = {"旁站记录": "doc", "监理通知单": "warn", "监理日志": "pen"}


# ---------------------------------------------------------------- KIND_TO_TINT
KIND_TO_TINT = {"旁站记录": "b", "监理通知单": "y", "监理日志": "g"}


# ---------------------------------------------------------------- ADMIN_HTML
ADMIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>监理 AI 服务 · 后台</title>
<style>
body{font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;margin:0;background:#f4f7fb;color:#11131a}
header{background:#1f3a5f;color:#fff;padding:14px 20px;font-weight:600}
.wrap{max-width:960px;margin:0 auto;padding:20px}
h2{font-size:16px;margin:24px 0 10px;color:#1f3a5f}
.card{background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 6px 16px -14px rgba(22,38,74,.5)}
table{width:100%;border-collapse:collapse}
td,th{padding:8px 6px;border-bottom:1px solid #eef2f8;text-align:left;font-size:13px;vertical-align:top}
th{color:#6b7789;font-weight:600;font-size:12px}
input,select,button{font:inherit;padding:6px 10px;border:1px solid #d6deea;border-radius:8px;background:#fff}
button{cursor:pointer}.pri{background:#0a6dff;color:#fff;border-color:#0a6dff}
.mut{color:#8a94a6;font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.ok{background:#e7f8ed;color:#0f8a3c}.off{background:#f2f4f8;color:#5b6473}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
</style></head><body>
<header>监理 AI 服务 · 后台</header>
<div class="wrap">

<h2>项目</h2>
<div class="card">
  <table id="projects"></table>
  <div class="row" style="margin-top:12px">
    <input id="p-id" placeholder="项目 id（英文）" size="16">
    <input id="p-name" placeholder="项目名称" size="22">
    <input id="p-code" placeholder="工程编号" size="14">
    <button class="pri" onclick="addProject()">新建项目</button>
  </div>
</div>

<h2>记忆 <span class="mut" id="mem-scope-label"></span></h2>
<div class="card">
  <div class="row" style="margin-bottom:10px">
    <select id="m-scope" onchange="loadMemories()">
      <option value="project">项目记忆</option>
      <option value="user">个人记忆</option>
    </select>
    <select id="m-owner" onchange="loadMemories()"></select>
    <input id="m-text" placeholder="新增一条记忆" size="40">
    <button class="pri" onclick="addMemory()">添加</button>
    
  </div>
  <table id="memories"></table>
</div>

<h2>任务</h2>
<div class="card"><table id="tasks"></table></div>

</div>
<script>
let projects = [];
async function api(path, opt) {
  const r = await fetch(path, opt);
  return r.json();
}
async function loadProjects() {
  const d = await api('/api/v1/projects');
  projects = d.data || [];
  document.getElementById('projects').innerHTML =
    '<tr><th>id</th><th>名称</th><th>编号</th><th>创建时间</th></tr>' +
    projects.map(p => `<tr><td>${p.id}</td><td>${p.name}</td><td>${p.code || '-'}</td><td class="mut">${p.created_at}</td></tr>`).join('');
  const owner = document.getElementById('m-owner');
  const scope = document.getElementById('m-scope').value;
  owner.innerHTML = projects.map(p => `<option value="${p.id}">${p.name}</option>`).join('') +
    (scope === 'user' ? '<option value="1">用户 1（李工）</option><option value="2">用户 2（王工）</option>' : '');
  loadMemories();
}
async function loadMemories() {
  const scope = document.getElementById('m-scope').value;
  let owner = document.getElementById('m-owner').value;
  if (scope === 'project' && projects.length) owner = owner || projects[0].id;
  if (!owner) { document.getElementById('memories').innerHTML = ''; return; }
  const d = await api(`/api/v1/memories?scope=${scope}&ownerId=${owner}`);
  const rows = d.data || [];
  document.getElementById('mem-scope-label').textContent = `（${scope === 'project' ? '项目' : '个人'} · ${owner}）`;
  document.getElementById('memories').innerHTML =
    '<tr><th>内容</th><th>来源</th><th>用过</th><th>状态</th><th></th></tr>' +
    (rows.length ? rows.map(m => `<tr>
      <td><input value="${(m.text||'').replace(/"/g,'&quot;')}" size="46" onchange="editMemory(${m.id}, this.value)"></td>
      <td class="mut">${m.source}</td><td class="mut">${m.hits}</td>
      <td><span class="tag ${m.status ? 'ok' : 'off'}">${m.status ? '生效' : '停用'}</span></td>
      <td><button onclick="toggleMemory(${m.id}, ${m.status ? 0 : 1})">${m.status ? '停用' : '启用'}</button>
          <button onclick="delMemory(${m.id})">删除</button></td></tr>`).join('')
      : '<tr><td class="mut">还没有记忆</td></tr>');
}
async function addProject() {
  const id = document.getElementById('p-id').value.trim();
  const name = document.getElementById('p-name').value.trim();
  const code = document.getElementById('p-code').value.trim();
  const d = await api('/api/v1/projects', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id,name,code})});
  if (d.code !== 0) return alert(d.message);
  loadProjects();
}
async function addMemory() {
  const scope = document.getElementById('m-scope').value;
  const owner = document.getElementById('m-owner').value;
  const text = document.getElementById('m-text').value.trim();
  if (!text) return;
  const d = await api('/api/v1/memories', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({scope, ownerId: owner, text})});
  if (d.code !== 0) return alert(d.message);
  document.getElementById('m-text').value = '';
  loadMemories();
}
async function editMemory(id, text) {
  await api('/api/v1/memories/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
}
async function toggleMemory(id, status) {
  await api('/api/v1/memories/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
  loadMemories();
}
async function delMemory(id) {
  if (!confirm('删除这条记忆？')) return;
  await api('/api/v1/memories/' + id, {method:'DELETE'});
  loadMemories();
}
async function loadTasks() {
  const d = await api('/api/v1/tasks?limit=20');
  const rows = d.data || [];
  document.getElementById('tasks').innerHTML =
    '<tr><th>id</th><th>项目</th><th>用户</th><th>状态</th><th>创建</th><th>错误</th></tr>' +
    rows.map(t => `<tr><td>${t.id}</td><td>${t.project_id}</td><td>${t.user_id}</td>
      <td><span class="tag ${t.status==='done'?'ok':'off'}">${t.status}</span></td>
      <td class="mut">${t.created_at}</td><td class="mut">${(t.error||'').slice(0,60)}</td></tr>`).join('');
}
loadProjects(); loadTasks(); setInterval(loadTasks, 5000);
</script></body></html>"""


# ---------------------------------------------------------------- _bigrams
def _bigrams(text):
    t = re.sub(r"[\s，。、；：！？「」『』（）()\[\]【】,.;:!?·]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


# ---------------------------------------------------------------- _rerender_if_project
def _rerender_if_project(scope, owner):
    """项目记忆一变，就把 memories/project.md 重渲染并提交 —— 不用人工点"""
    if scope != "project":
        return None
    try:
        # biz 依赖 core，core 不能顶层导入 biz（循环）—— 调用时导入
        from .biz import render_project_memory, render_rules_md

        render_rules_md(owner)
        return render_project_memory(owner)
    except Exception:
        traceback.print_exc()
        return None


# ---------------------------------------------------------------- extract_from_items
def extract_from_items(items):
    """从 thread items 里挑出助手文本和工具调用（形状不确定时容忍）"""
    texts, trace = [], []

    def text_of(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for c in content:
                if isinstance(c, dict):
                    out.append(c.get("text") or c.get("content") or "")
                elif isinstance(c, str):
                    out.append(c)
            return "".join(out)
        return ""

    for it in items or []:
        t = (it.get("type") or "").lower()
        role = (it.get("role") or "").lower()

        if "message" in t or role in ("assistant", "user"):
            if role == "assistant" or t in ("assistant_message", "agent_message", "agentmessage"):
                txt = text_of(it.get("content")) or text_of(it.get("text"))
                if txt.strip():
                    texts.append(txt.strip())
        elif "tool" in t or "command" in t or "exec" in t:
            name = (it.get("name") or it.get("toolName") or it.get("command") or "tool")
            if isinstance(name, str) and "/" in name:
                name = name.rsplit("/", 1)[-1]
            trace.append({"tool": str(name)[:60], "icon": "search",
                          "text": str(name)[:80], "ms": it.get("durationMs")})
    return "\n".join(texts).strip(), trace


# ---------------------------------------------------------------- on_notification
def on_notification(msg):
    method = msg.get("method")
    if method not in ("turn/completed",):
        return
    params = msg.get("params") or {}
    turn = params.get("turn") or {}
    turn_id = turn.get("id")
    status = (turn.get("status") or "").lower()
    thread_id = params.get("threadId") or turn.get("threadId")

    rows = q("SELECT id FROM task WHERE turn_id=%s", (turn_id,))
    if not rows:
        return
    task_id = rows[0]["id"]

    text, trace = "", []
    try:
        # includeTurns 必须为 true，否则 items 是 notLoaded，拿不到内容
        from .agent import engine

        r = engine().call("thread/read", {"threadId": thread_id, "includeTurns": True}, 60)
        thread = (r or {}).get("result", {}).get("thread", {}) or {}
        items = list(thread.get("items") or [])
        for t in thread.get("turns") or []:
            items.extend(t.get("items") or [])
        if items and not getattr(on_notification, "_logged", False):
            sys.stderr.write("[items-sample] " + json.dumps(items[-1], ensure_ascii=False)[:700] + "\n")
            on_notification._logged = True
        text, trace = extract_from_items(items)
    except Exception:
        traceback.print_exc()

    if text or trace:
        exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, trace_sum, ms, task_id, sync) "
            "SELECT project_id, user_id, CONCAT('r-', %s), 'assistant', %s, %s, %s, NULL, id, 1 "
            "FROM task WHERE id=%s",
            (turn_id, text or None,
             json.dumps(trace, ensure_ascii=False) if trace else None,
             f"{len(trace)} 次工具调用" if trace else None, task_id),
        )

    new_status = {"completed": "done", "failed": "failed", "interrupted": "cancelled"}.get(status, "failed")
    err = json.dumps(turn.get("error"), ensure_ascii=False)[:900] if turn.get("error") else None
    exec_sql("UPDATE task SET status=%s, error=%s, finished_at=NOW(3) WHERE id=%s",
             (new_status, err, task_id))
    sys.stderr.write(f"[turn] {turn_id[:12]} -> {new_status}\n")


# ---------------------------------------------------------------- notify_worker
def notify_worker():
    while True:
        msg = NOTIFY.get()
        try:
            on_notification(msg)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- _hash_token
def _hash_token(t):
    import hashlib
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- issue_token
def issue_token(user_id, device=None):
    import secrets
    raw = secrets.token_hex(32)
    exec_sql("INSERT INTO auth_token (user_id, token_hash, device, expires_at) "
             "VALUES (%s,%s,%s, DATE_ADD(NOW(), INTERVAL %s HOUR))",
             (user_id, _hash_token(raw), device, TOKEN_TTL_HOURS))
    return raw


# ---------------------------------------------------------------- resolve_token
def resolve_token(raw):
    """返回 user 行，无效则 None"""
    if not raw:
        return None
    rows = q("SELECT u.id, u.name, u.role, u.tenant_id FROM auth_token t "
             "JOIN app_user u ON u.id = t.user_id "
             "WHERE t.token_hash=%s AND t.revoked=0 AND t.expires_at > NOW() "
             "AND u.status=1", (_hash_token(raw),))
    return rows[0] if rows else None


# ---------------------------------------------------------------- handle_login
def handle_login(body):
    openid = (body.get("openid") or "").strip()
    name = (body.get("name") or "").strip()
    if not openid:
        return 400, {"code": 400, "message": "openid 必填"}

    rows = q("SELECT id, name, role FROM app_user WHERE openid=%s", (openid,))
    if rows:
        user = rows[0]
    elif name:
        uid = exec_sql("INSERT INTO app_user (tenant_id, openid, name, role) VALUES (1,%s,%s,%s)",
                       (openid, name, body.get("role") or "监理员"))
        user = {"id": uid, "name": name, "role": body.get("role") or "监理员"}
    else:
        return 404, {"code": 404, "message": "用户不存在，首次登录请带 name"}

    token = issue_token(user["id"], body.get("device"))
    return 200, {"code": 0, "data": {
        "token": token, "expiresIn": TOKEN_TTL_HOURS * 3600,
        "user": {"id": user["id"], "name": user["name"], "role": user["role"]},
    }}


# ---------------------------------------------------------------- handle_logout
def handle_logout(headers):
    raw = bearer(headers)
    if raw:
        exec_sql("UPDATE auth_token SET revoked=1 WHERE token_hash=%s", (_hash_token(raw),))
    return 200, {"code": 0, "data": {"ok": True}}


# ---------------------------------------------------------------- bearer
def bearer(headers):
    v = headers.get("Authorization") or ""
    return v[7:].strip() if v.lower().startswith("bearer ") else None


# ---------------------------------------------------------------- _day_label
def _day_label(dt):
    today = time.strftime("%Y-%m-%d")
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    d = str(dt)[:10]
    md = f"{int(str(dt)[5:7])}月{int(str(dt)[8:10])}日"
    if d == today:
        return md, f"今天 · {WEEK[dt.weekday()]}"
    if d == yesterday:
        return md, f"昨天 · {WEEK[dt.weekday()]}"
    return md, WEEK[dt.weekday()]


# ---------------------------------------------------------------- ms_label
def ms_label(ms):
    """人话时长：58.8 秒 / 2 分 51 秒 / 420 毫秒"""
    if not ms:
        return None
    if ms < 1000:
        return f"{ms} 毫秒"
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.1f} 秒"
    m, s = divmod(int(round(secs)), 60)
    return f"{m} 分 {s} 秒"


# ---------------------------------------------------------------- get_project
def get_project(project_id):
    rows = q("SELECT * FROM project WHERE id=%s", (project_id,))
    if not rows:
        raise ValueError(f"项目不存在: {project_id}")
    return rows[0]


# ---------------------------------------------------------------- LEARN_PROMPT
LEARN_PROMPT = """你刚完成一次监理助手的对话。现在只做一件事：判断有没有值得长期记住的东西。

值得记的（最多 2 条）：
- 用户个人的书写习惯：如「结论写经检查符合要求」「单位写 m³」
- 项目的规矩：如「整改回复必须附前后对比图」「沟槽开挖不出旁站」

不值得记的：
- 这一次的具体内容（部位、数量、日期）
- 一次性的事实

**只输出 JSON，不要任何多余文字**：
{"memories":[{"scope":"user","text":"..."},{"scope":"project","text":"..."}]}

没有可记的就输出：{"memories":[]}
"""


# ---------------------------------------------------------------- similar
def similar(a, b):
    """归一化后算相似度，>0.8 视为重复"""
    import difflib
    norm = lambda x: re.sub(r"[\s，。、；：！？「」『』（）()\[\]【】,.;:!?]", "", x or "")
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if short in long_:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() > 0.8


# ---------------------------------------------------------------- LLM_URL
# 唯一线路：阿里云百炼。旧中转（xfcxb）已停用、额度为 0，代码里不再引用
LLM_URL = (ENV.get("LLM_BASE")
           or ENV.get("DASHSCOPE_BASE_URL")
           or "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1").rstrip("/") + "/responses"


# ---------------------------------------------------------------- LLM_MODEL
LLM_MODEL = ENV.get("LLM_MODEL") or ENV.get("LLM_FALLBACK_MODEL") or "qwen3.7-plus"


# ---------------------------------------------------------------- JL_TOOL_BIN
JL_TOOL_BIN = f"{ROOT}/scripts/jl-tool"


# ---------------------------------------------------------------- SYSTEM_PROMPT
SYSTEM_PROMPT = """你是现场监理助手，帮监理员把现场的情况记成合规的文书。

你可以调用工具。**每一步只输出一个 JSON，不要任何多余文字、不要代码块围栏。**

要调工具时：
{"action":"tool","tool":"<工具名>","args":{...}}

要收尾时：
{"action":"final","text":"<给用户看的回复，中文，简洁>"}

规矩：
1. 出稿前先侦查：用 get_context 看项目，读 records/rules.md、records/photos.md，用 search_records/search_photos 找同类记录，规范用 search_standards 查。至少 2~3 个来源，不要搜一次就写。
2. 能出草稿的只有：监理通知单、监理日志、旁站记录、平行检查记录、监理检查记录。巡视记录是每日汇总导出，不是单份草稿，要引导用户去「资料/记录 → 巡视记录」。
3. 部位、事由、问题这些关键信息缺失时，先问一句；不要生成空壳文书。非关键字段可以留空标「待补」。
4. 查到规范就把条号写进 bases；查不到就明说「没查到依据」，不要凭印象写条号。
5. 写文书（create_doc_draft）之前，必须有 obj 或至少一个非空字段；后端会拒绝空文书。
6. 用户要标注/圈出/对比现场照片时：先 search_photos 拿真实 photoId，再执行
   jl-tool annotate_photo '{"projectId":"<项目>","photoId":<真实id>,"boxes":[{"x":..,"y":..,"w":..,"h":..,"label":".."}],"caption":".."}'
   或 jl-tool compare_photos '{"projectId":"<项目>","beforePhotoId":<id>,"afterPhotoId":<id>,"title":".."}'。
   JSON 必须作为第二个命令行参数（argv），不要用 echo/管道/stdin/heredoc。
   禁止用 shell/python/Pillow 自己绘制或编辑图片、禁止写 /tmp、禁止在回答里输出本地路径/base64；
   工具失败就如实告诉用户失败原因，不要绕过工具自己造图。
7. 最多调 8 次工具；证据够了就 final。
"""


# ---------------------------------------------------------------- ALLOWED_TOOLS
ALLOWED_TOOLS = None


# ---------------------------------------------------------------- allowed_tools
def allowed_tools():
    global ALLOWED_TOOLS
    if ALLOWED_TOOLS is None:
        try:
            p = subprocess.run([JL_TOOL_BIN, "list"], capture_output=True, text=True, timeout=60)
            data = json.loads(p.stdout.strip() or "{}")
            # jl-tool list 返回 {"tools": [...]}，也兼容直接给数组
            items = data.get("tools") if isinstance(data, dict) else data
            ALLOWED_TOOLS = {t.get("name") for t in (items or []) if t.get("name")}
        except Exception:
            ALLOWED_TOOLS = {"get_project", "search_records", "search_standards",
                             "create_doc_draft", "get_context"}
    return ALLOWED_TOOLS


# ---------------------------------------------------------------- tool_manual
# 出图类工具的用法/失败处理要写死：真实模型会试 echo JSON | jl-tool ...（stdin 不通），
# 失败后还可能改用 shell/python 自己画一张写 /tmp —— 这两条都必须在手册里堵死。
_TOOL_EXTRA = {
    "annotate_photo": (
        "用法：jl-tool annotate_photo '{\"projectId\":\"<项目>\",\"photoId\":<真实id>,"
        "\"boxes\":[{\"x\":100,\"y\":100,\"w\":300,\"h\":200,\"label\":\"配重块\"}],"
        "\"caption\":\"...\"}'  ← JSON 是第二个命令行参数（argv），不要用 echo/管道/stdin/heredoc。"
        "先 search_photos 拿真实 photoId；没有 photoId 就如实说，不要自己画图。"
    ),
    "compare_photos": (
        "用法：jl-tool compare_photos '{\"projectId\":\"<项目>\",\"beforePhotoId\":<id>,"
        "\"afterPhotoId\":<id>,\"title\":\"整改前后\"}'  ← JSON 是第二个命令行参数（argv）。"
        "先 search_photos 拿两张真实照片 id；同一张不能做前后对比。"
    ),
}
_TOOL_FOOTER = (
    "出图规则：现场照片的标注/对比只能用 annotate_photo / compare_photos；"
    "禁止用 shell/python/Pillow 自己绘制或编辑图片，禁止写 /tmp，禁止在回答里输出本地路径或 base64；"
    "工具失败（缺 photoId、照片不属于项目、同图对比等）就如实告诉用户失败原因，不要绕过工具自己造图。"
)


def tool_manual():
    """把工具的清单和一句话说明列出来。

    为什么从工具那儿取：手写的话，加了新工具（比如 validate_doc）没人记得改这里，
    agent 就永远不知道有这个东西。
    """
    try:
        p = subprocess.run([JL_TOOL_BIN, "list"], capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout.strip() or "{}")
        items = data.get("tools") if isinstance(data, dict) else data
    except Exception:
        return ""
    lines = []
    for t in items or []:
        name = t.get("name") or ""
        if not name:
            continue
        desc = (t.get("description") or "").strip()
        # 说明太长就取第一句，够 agent 判断该不该用
        short = desc.split("。")[0][:60]
        # 必须带完整路径：agent 的 shell 里没有 PATH 里的 jl-tool
        lines.append("  %s %s  # %s" % (JL_TOOL_BIN, name, short))
        extra = _TOOL_EXTRA.get(name)
        if extra:
            lines.append("      " + extra)
    lines.append("  # " + _TOOL_FOOTER)
    return "\n".join(lines)


# ---------------------------------------------------------------- llm
def llm(messages, max_tokens=1500, timeout=180):
    """直接调中转的 Responses API"""
    payload = {"model": LLM_MODEL, "input": messages, "max_output_tokens": max_tokens}
    req = urllib.request.Request(
        LLM_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (ENV.get("DASHSCOPE_API_KEY") or "")},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        sys.stderr.write(f"[llm] HTTP {e.code}: {body[:600]}\n")
        raise RuntimeError(f"模型返回 {e.code}: {body[:300]}")
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text" and c.get("text"):
                    return c["text"]
    return ""


# ---------------------------------------------------------------- parse_step
def parse_step(raw):
    """从模型输出里抠 JSON，容忍代码块/前后废话"""
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------- describe_tool
def describe_tool(name, args):
    """把工具调用翻译成人话，给小程序里的轨迹条显示"""
    a = args or {}
    if name == "search_standards":
        q = a.get("query") or ""
        return f"查规范：{q}" if q else "查规范"
    if name == "create_doc_draft":
        kind = a.get("kind") or "文书"
        obj = a.get("obj") or ""
        return f"写{kind}" + (f" · {obj}" if obj else "")
    if name == "search_records":
        place = a.get("place") or a.get("keyword") or ""
        return f"查历史记录" + (f" · {place}" if place else "")
    if name == "search_photos":
        q = a.get("q") or a.get("place") or ""
        return f"查现场照片" + (f" · {q}" if q else "")
    if name == "annotate_photo":
        return "标注现场照片 #%s" % (a.get("photoId") or "?")
    if name == "compare_photos":
        return "整改前后对比 #%s ↔ #%s" % (a.get("beforePhotoId") or "?",
                                          a.get("afterPhotoId") or "?")
    if name == "get_project":
        return "读工程概况"
    if name == "get_context":
        return "读项目上下文"
    return f"调用 {name}"


# ---------------------------------------------------------------- run_tool
def run_tool(name, args):
    """工具全部经 jl-tool 执行，白名单 + 超时 + 留痕"""
    if name not in allowed_tools():
        return False, f"工具不在白名单: {name}"
    started = time.time()
    try:
        p = subprocess.run([JL_TOOL_BIN, name, json.dumps(args, ensure_ascii=False)],
                           capture_output=True, text=True, timeout=180)
        ms = int((time.time() - started) * 1000)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "工具执行失败")[:800] + f" ({ms}ms)"
        return True, (p.stdout or "")[:6000]
    except subprocess.TimeoutExpired:
        return False, "工具超时（180 秒）"


# ---------------------------------------------------------------- ANSWER_KEYS

# ---------------------------------------------------------------- ANSWER_RE

# ---------------------------------------------------------------- REF_RE
REF_RE = re.compile(
    r"((?:GB|JGJ|CJJ|DB|JG)\s?[0-9T]+\s?[-–—]\s?[0-9]{2,4})"
    r"(?:\s*第?\s*#?\s*([0-9]+(?:\.[0-9]+)*)\s*条?)?"
)


# ---------------------------------------------------------------- GAP_INLINE_RE
GAP_INLINE_RE = re.compile(r"待补[：:]\s*([^。；\n]+)")


# ---------------------------------------------------------------- rich_reply
def rich_reply(text, gap_items):
    """一段话 + 行内标注。s ∈ t(普通)/b(加粗)/n(数字)/r(规范引用)/g(待补)"""
    text = (text or "").strip()
    if not text:
        return []

    # 模型把"依据：""待补："直接接在上一句后面，这里补成独立一行
    text = re.sub(r"(?<![。；！？\n])\s*(依据|待补)[：:]", r"。\n\1：", text)
    text = text.replace("。。", "。")

    gaps = [g for g in (gap_items or []) if g]
    if not gaps:
        m = GAP_INLINE_RE.search(text)
        if m:
            gaps = [x.strip() for x in re.split(r"[、，,]", m.group(1)) if 2 <= len(x.strip()) <= 10]
    out = []

    def push_plain(s):
        # 处理换行：切成多段，插入 br
        if "\n" in s:
            for i, part in enumerate(s.split("\n")):
                if i:
                    out.append({"s": "br", "v": "\n"})
                if part:
                    push_plain(part)
            return
        pos = 0
        # 在一段普通文字里找：规范号 → 待补字段 → 数字
        events = []
        for m in REF_RE.finditer(s):
            events.append((m.start(), m.end(), "r", m.group(1).replace(" ", ""), m.group(2)))
        for g in gaps:
            for m in re.finditer(re.escape(g), s):
                events.append((m.start(), m.end(), "g", g, None))
        # 引用角标 [n]：模型自己写的序号（顺序 = 引用块 entries 顺序）
        for m in CITE_MARK_RE.finditer(s):
            events.append((m.start(), m.end(), "c", m.group(0), None))
        events.sort()

        for start, end, kind, val, clause in events:
            if start < pos:
                continue
            if start > pos:
                out.extend(_split_num(s[pos:start]))
            if kind == "r":
                out.append({"s": "r", "v": val, "code": val.split("第")[0].strip(), "clause": clause or ""})
            elif kind == "c":
                try:
                    out.append({"s": "c", "v": val, "n": int(val.strip("[]"))})
                except ValueError:
                    out.append({"s": "t", "v": val})
            else:
                out.append({"s": "g", "v": val, "gap": val})
            pos = end
        if pos < len(s):
            out.extend(_split_num(s[pos:]))

    pos = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > pos:
            push_plain(text[pos:m.start()])
        out.append({"s": "b", "v": m.group(1)})
        pos = m.end()
    if pos < len(text):
        push_plain(text[pos:])
    # 换行段没有文字，不能一起滤掉
    return [x for x in out if x.get("v")]


# ---------------------------------------------------------------- _split_num
def _split_num(s):
    out = []
    pos = 0
    for m in NUM_RE.finditer(s):
        if m.start() > pos:
            out.append({"s": "t", "v": s[pos:m.start()]})
        out.append({"s": "n", "v": m.group(0)})
        pos = m.end()
    if pos < len(s):
        out.append({"s": "t", "v": s[pos:]})
    return out


# ---------------------------------------------------------------- build_answer
def build_answer(doc, cites, text, blocks=None):
    """结构一律从数据来，不从模型的话里抠。

    gaps  —— 按 schema 校验文书字段（validate_fields）
    cites —— 工具输出（search_standards 的结果）里带的条号
    text  —— 模型自己的话，原样保留，只做基础清洗
    blocks —— 多模态 answer.blocks v1（可空；旧客户端只读 text/rich，天然兼容）
    """
    text = ensure_image_fallback(clean_reply(text), blocks)

    missing = []
    if doc:
        fields = doc.get("fields") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        check = validate_fields(doc.get("kind") or "", fields)
        missing = list(check["missing"]) + list(check["placeholder"])

    return {
        "rich": rich_reply(text, missing if missing else None),
        "gaps": missing,
        "text": text,
        # 旧客户端读 text/rich 不受影响；新客户端 opt-in 读 blocks（可为空数组）
        "blocks": blocks if isinstance(blocks, list) else [],
        # 卡片必须跟着消息一起落库：否则刷新/重进页面，聊天里只剩文字，
        # 文书卡片消失（前端只从 task 的即时返回里拿 artifact）。
        "artifact": ({
            "id": doc.get("id"), "kind": doc.get("kind") or "文书",
            "obj": doc.get("obj") or "", "state": doc.get("state") or "",
            "path": doc.get("path") or "",
            "commit": (doc.get("git_commit") or "")[:10],
            "name": (doc.get("kind") or "文书") +
                    (" · " + doc["obj"] if doc.get("obj") else ""),
        } if doc else None),
    }


# ---------------------------------------------------------------- BOLD_RE
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


# ---------------------------------------------------------------- NUM_RE
NUM_RE = re.compile(
    r"[+\-±]?\d+(?:\.\d+)?\s?(?:mm|cm|m|㎡|m3|kg|kN|MPa|℃|%|处|根|个|条|块|层|号|天|日|小时|分钟)"
    r"|(?<![\w.\-])[0-9]+(?![\w.\-])"
)


# ---------------------------------------------------------------- rich_segments
# 角标：[1] [2] …（模型自己写的引用序号）
CITE_MARK_RE = re.compile(r"\[(\d{1,2})\]")


def rich_segments(text):
    """把一句话切成 [{s,v}]，s ∈ t(普通)/n(数字)/b(加粗)/c(引用角标)/r(条文号)。"""
    text = text or ""
    if not text:
        return []

    out = []

    def push_num(s):
        pos = 0
        for m in NUM_RE.finditer(s):
            if m.start() > pos:
                out.append({"s": "t", "v": s[pos:m.start()]})
            out.append({"s": "n", "v": m.group(0)})
            pos = m.end()
        if pos < len(s):
            out.append({"s": "t", "v": s[pos:]})

    def push_cites(s):
        """把 [n] 切成引用角标，其余交回 push_num。"""
        pos2 = 0
        for m in CITE_MARK_RE.finditer(s):
            if m.start() > pos2:
                push_num(s[pos2:m.start()])
            out.append({"s": "c", "v": m.group(0), "n": int(m.group(1))})
            pos2 = m.end()
        if pos2 < len(s):
            push_num(s[pos2:])

    pos = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > pos:
            push_cites(text[pos:m.start()])
        out.append({"s": "b", "v": m.group(1)})
        pos = m.end()
    if pos < len(text):
        push_cites(text[pos:])

    return [x for x in out if x["v"]]


# ---------------------------------------------------------------- GAP_SPLIT_RE
GAP_SPLIT_RE = re.compile(r"[、，,；;/]|\s{2,}")


# ---------------------------------------------------------------- split_gaps
def split_gaps(text):
    """把待补那段拆成条目，给界面做 chips。"""
    if not text:
        return []
    # 不是栏位名的词：动作、口语、连接词
    BAD = ("写明", "提供", "采用", "保留", "生成", "核对", "标注", "记录",
           "要求", "建议", "可以", "需要", "都已", "已", "具体", "以及")
    items = []
    for raw in GAP_SPLIT_RE.split(text):
        it = raw.strip().strip("。").strip()
        if not (2 <= len(it) <= 10):
            continue
        if any(b in it for b in BAD):
            continue
        if re.search(r"[的了是在和与或]", it):
            continue
        if it not in items:
            items.append(it)
    return items[:6]


# ---------------------------------------------------------------- 本地路径清洗 / 出图提示
# 模型在 shell 路径里自己画图时，会把 /tmp/xxx.png、/home/...、C:\...、file:// 写进正文。
# 这些路径对用户不可访问，也不是资产；统一清洗掉（相对 docs/、/api/ 正常路径不受影响）。
_FILE_URL = r"file://[^\s，。；：）)、！？】\"'`]+"
_POSIX_LOCAL = r"/(?:tmp|home|var|etc|opt|usr|root|mnt|media)/[^\s，。；：）)、！？】\"'`]*"
_WIN_LOCAL = r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)[^\s，。；：）)、！？】\"'`]*"
_LOCAL_ABS_RE = re.compile("(?:" + "|".join([_FILE_URL, _POSIX_LOCAL, _WIN_LOCAL]) + ")", re.I)
_LOCAL_SAVE_PHRASE_RE = re.compile(
    r"(?:文件)?(?:已)?(?:保存|写入|输出|导出|存放|存|放)(?:在|到)\s*[：:]?\s*"
    + "(?:" + "|".join([_FILE_URL, _POSIX_LOCAL, _WIN_LOCAL]) + ")", re.I)

_IMAGE_CLAIM_RE = re.compile(
    r"(标注图|对比图|拼版|图片已|图像已|图已(?:生成|保存|输出)|已(?:生成|保存|输出|做好).{0,8}图"
    r"|见附图|附图[:：]|图片如下|标注如下)", re.I)


def strip_local_paths(text):
    """把正文里的本地绝对路径 / file:// / 「文件已保存在…」整段替换掉。"""
    if not text:
        return ""
    t = _LOCAL_SAVE_PHRASE_RE.sub("（本地文件不展示）", text)
    t = _LOCAL_ABS_RE.sub("", t)
    return t


def ensure_image_fallback(text, blocks):
    """正文声称有图、但 blocks 为空时，补一句明确提示，避免用户以为能看到图。"""
    t = text or ""
    if blocks:
        return t
    if _IMAGE_CLAIM_RE.search(t) and "未生成可展示的图片" not in t:
        t = t.rstrip() + "\n\n（提示：本次未生成可展示的图片，可以让我重新标注一次。）"
    return t


# ---------------------------------------------------------------- clean_reply
def clean_reply(text):
    """把模型的话清洗成能给用户看的样子。

    模型会不自觉地把内部细节吐出来：绝对路径、Markdown 链接、URL 编码、提交号。
    小程序不渲染 Markdown，用户也不需要知道文件存在服务器的哪个目录。
    """
    if not text:
        return ""
    t = text

    # 1) Markdown 链接 [文字](地址) → 只留文字
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 2) 行内代码 / 代码块围栏
    t = re.sub(r"```[a-zA-Z]*\n?", "", t)
    t = t.replace("`", "")
    # 3) 服务器绝对路径 / 本地临时路径 / file://（/tmp、/home、C:\ 等）
    t = strip_local_paths(t)
    t = re.sub(r"/home/[^\s，。；：）)、]+", "", t)
    t = re.sub(r"data/workspaces/[^\s，。；：）)、]+", "", t)
    t = re.sub(r"docs/\d{4}-\d{2}-\d{2}/[^\s，。；：）)、]*\.md", "", t)
    # 4) URL 百分号编码长串（文件名被编码后的样子）
    t = re.sub(r"(?:%[0-9A-Fa-f]{2}){2,}", "", t)
    # 4.5) 删完路径/链接后常留下「，。」「。。」「；，」这类残句
    # 顺序要紧：先去标点间的空白，再合并连续标点，否则「。 。」会先被并成「。。」
    t = re.sub(r"\s+([，。；：、])", r"\1", t)
    t = re.sub(r"([，。；：、])\s+", r"\1", t)
    t = re.sub(r"([，。；：、])[，。；：、]+", r"\1", t)
    t = re.sub(r"[，、；：]([。！？])", r"\1", t)
    t = t.strip(" ，、；：")

    # 5) 「提交号 d227936」这类内部线索
    t = re.sub(r"[，,；;]?\s*提交号\s*[0-9a-fA-F]{6,40}", "", t)
    # 6) 去掉开头的过程叙述（"我先核对…""我会先查…"）——轨迹里已经显示了
    t = re.sub(r"^[^。\n]{0,30}(我先|我会先|我将先|我先来)[^。\n]*。\s*", "", t)

    # 7) 句子去重：只删"几乎一模一样"的重复句（≥0.95），且只删短的那句
    #    以前阈值 0.72 + 硬截 160 字 —— 那是在替模型删内容（实测把整段回答截没了）
    import difflib as _dl
    parts = [x.strip() for x in re.split(r"(?<=[。！？\n])", t) if x.strip()]
    kept = []
    for seg in parts:
        dup = next((i for i, k in enumerate(kept)
                    if _dl.SequenceMatcher(None, seg, k).ratio() > 0.95), None)
        if dup is None:
            kept.append(seg)
        elif len(seg) > len(kept[dup]):
            kept[dup] = seg          # 留更完整的那句
    t = "".join(kept)

    # 8) 不再截断。内容长短由模型决定，不由后端限制。
    #    只修一种排版事故：** 不成对（被别处切断）时会把星号露出来
    while t.count("**") % 2:
        idx = t.rfind("**")
        t = t[:idx] + t[idx + 2:]

    # 9) 空括号、多余空白
    t = re.sub(r"[（(]\s*[）)]", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\s+\n", "\n", t)
    return t.strip()


# ---------------------------------------------------------------- ENGINE_MODE
# appserver = 常驻 app-server 跑（唯一支持的一等路径）；cli = 一次性 exec（老路径）
# 默认改成 appserver：以前默认 cli 会让"复核开关"静默失效（判断写的是 == appserver）
ENGINE_MODE = os.environ.get("ENGINE_MODE", "appserver")


# ---------------------------------------------------------------- _USER_LOCKS
_USER_LOCKS = {}


# ---------------------------------------------------------------- _USER_LOCKS_GUARD
_USER_LOCKS_GUARD = threading.Lock()


def _blob_file(sha):
    """blob 的磁盘路径：data/blobs/<sha 前两位>/<sha>"""
    if not sha:
        return None
    sha = str(sha).split(":")[-1].strip()
    if len(sha) < 4:
        return None
    p2 = BLOB_ROOT + "/" + sha[:2] + "/" + sha
    return p2 if os.path.exists(p2) else None


def materialize_images(parts, limit=20):
    """把 parts 里的图片落到临时目录，返回真实文件路径。

    引擎的 `exec -i` 要真实路径，而 blob 是纯 sha256 无名无扩展名，
    有的解码器会认不出，所以按文件头补扩展名再落一份。
    """
    out = []
    tmp = "/tmp/jl-img"
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        return out
    for part in parts or []:
        if len(out) >= limit:
            break
        if part.get("type") != "image":
            continue
        src = _blob_file(part.get("blob") or part.get("sha"))
        if not src:
            continue
        try:
            with open(src, "rb") as f:
                head = f.read(16)
            ext = ".bin"
            for magic, e in IMG_MAGIC:
                if head.startswith(magic):
                    ext = e
                    break
            dst = tmp + "/" + os.path.basename(src)[:16] + ext
            if not os.path.exists(dst):
                with open(src, "rb") as a, open(dst, "wb") as b:
                    b.write(a.read())
            out.append(dst)
        except Exception as exc:
            sys.stderr.write("[img] 落盘失败 %s: %s\n" % (src, exc))
    return out


def transcribe_audio(blob_sha, language="zh"):
    """任意音频 → 16k 单声道 wav → 百炼 ASR。返回 (文本, 提示)。"""
    src = _blob_file(blob_sha)
    if not src:
        return None, "找不到音频"

    # 按次隔离：以前是全局共用的 /tmp/jl-asr.wav，
    # 两个人同时发语音会互相覆盖，其中一个人拿到别人的转写。
    wav = "/tmp/jl-asr-%s.wav" % uuid.uuid4().hex[:10]
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ff, "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
                       capture_output=True, timeout=120)
        if not os.path.exists(wav):
            return None, "音频转换失败"
    except Exception as exc:
        return None, "转换异常：%s" % exc

    key = ENV.get("ASR_API_KEY") or ENV.get("DASHSCOPE_API_KEY")
    host = (ENV.get("ASR_HOST") or "").rstrip("/")
    model = ENV.get("ASR_MODEL") or "qwen-audio-3.0-asr-flash"
    if not key or not host:
        return None, "没配置 ASR"

    b64 = base64.b64encode(open(wav, "rb").read()).decode()
    payload = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [
            {"type": "input_audio",
             "input_audio": {"data": "data:audio/wav;base64," + b64}}]}]},
        "parameters": {"format": "wav", "sample_rate": "16000"},
    }
    url = host + "/api/v1/services/aigc/multimodal-generation/generation"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 百炼对静音/过短音频返回 400 且体为空 {}，那不是故障
        if e.code == 400:
            return "", "没识别到语音内容"
        return None, "ASR HTTP %s" % e.code
    except Exception as exc:
        return None, "ASR 异常：%s" % exc

    text = (body.get("text") or "").strip()
    if not text:
        out = body.get("output") or {}
        if isinstance(out, dict):
            text = (out.get("text") or "").strip()
    return text, None

# ================================================================ 文书 schema
# 规则来自 schemas/<类型>.json，不写死在代码里。
# 为什么要它：模型会把"待补"当字段值写进去（"签发人": "待补"），
# 只判空字符串的话，这种文书会被标成"待签"，其实没填。

SCHEMA_DIR = ROOT + "/schemas"


def load_schema(kind):
    """读某个文书类型的 schema；没有就返回 None（不阻挡，只提示）。"""
    if not kind:
        return None
    path = SCHEMA_DIR + "/" + str(kind).strip() + ".json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_schemas():
    out = []
    try:
        for name in sorted(os.listdir(SCHEMA_DIR)):
            if name.endswith(".json"):
                out.append(name[:-5])
    except Exception:
        pass
    return out


def validate_fields(kind, fields):
    """按 schema 查字段是否齐全。

    返回 {ok, missing, placeholder, unknown, note}
      missing     —— 必填但没给
      placeholder —— 给了占位词（待补/待定…），等于没给
      unknown     —— schema 里没定义的字段（只提示，不算错）
    """
    fields = fields or {}
    schema = load_schema(kind)
    if not schema:
        return {"ok": True, "missing": [], "placeholder": [], "unknown": [],
                "note": "没有「%s」的 schema，按不齐处理但不算错" % kind}

    required = schema.get("fields") or []
    optional = schema.get("optional") or []
    holders = set(schema.get("placeholders") or [])

    missing, placeholder = [], []
    for key in required:
        raw = fields.get(key)
        text = str(raw if raw is not None else "").strip()
        if not text:
            missing.append(key)
        elif text in holders:
            placeholder.append(key)

    known = set(required) | set(optional)
    unknown = [k for k in fields.keys() if k not in known]

    return {
        "ok": not missing and not placeholder,
        "missing": missing,
        "placeholder": placeholder,
        "unknown": unknown,
        "note": "",
    }
