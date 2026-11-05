#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监理 AI 服务 · BFF

职责：
  1. 鉴权占位 / 多租户（project_id + user_id）
  2. 契约翻译：CHAT-1 → easyCode app-server 的 thread/start + turn/start
  3. 落库：task / message
  4. 断网续跑的基础：任务状态机（这里先落 queued/running/done/failed）

只用标准库 + pymysql。
"""
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

ROOT = "/home/baize/jl-service"
ENGINE = f"{ROOT}/repos/easycode/.target/release/easycode"
WS_ROOT = f"{ROOT}/data/workspaces"
PORT = int(os.environ.get("BFF_PORT", "8081"))

sys.path.insert(0, "/home/baize/.local/lib/python3.14/site-packages")


# ---------------------------------------------------------------- 配置
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


ENV = load_env()
ENGINE_ENV = dict(
    os.environ,
    HOME="/home/baize",
    PATH="/home/baize/bin:" + os.environ.get("PATH", ""),
)
# 模型 key 从 .env 注入，不写进 config.toml
if ENV.get("JL_LLM_KEY"):
    ENGINE_ENV["JL_LLM_KEY"] = ENV["JL_LLM_KEY"]


# ---------------------------------------------------------------- 数据库
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


def q1(params, key, default=None):
    """parse_qs 的值是列表，这里取第一个"""
    v = params.get(key)
    if isinstance(v, list):
        v = v[0] if v else None
    return default if v is None or v == "" else v


def q(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def exec_sql(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


# ---------------------------------------------------------------- 引擎连接
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


NOTIFY = queue.Queue()
# 同时跑几个 turn（每个都是一次模型循环，别把机器打满）
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
TURN_SLOTS = threading.Semaphore(MAX_CONCURRENT)
ENGINE_CLIENT = None


def engine():
    global ENGINE_CLIENT
    if ENGINE_CLIENT is None or ENGINE_CLIENT.proc.poll() is not None:
        ENGINE_CLIENT = Engine()
        ENGINE_CLIENT.call("initialize", {"clientInfo": {"name": "jl-bff", "version": "0.1"},
                                              "capabilities": {"experimentalApi": True}}, 30)
    return ENGINE_CLIENT


# ---------------------------------------------------------------- 鉴权
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0") == "1"
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "720"))  # 30 天













# ---------------------------------------------------------------- 影像
BLOB_ROOT = f"{ROOT}/data/blobs"











# ---------------------------------------------------------------- 小程序兼容层















# ---------------------------------------------------------------- 首屏聚合
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATE_TO_ST = {"待补": "wait", "待签": "sign", "已签": "ok"}
KIND_TO_ICON = {"旁站记录": "doc", "监理通知单": "warn", "监理日志": "pen"}
KIND_TO_TINT = {"旁站记录": "b", "监理通知单": "y", "监理日志": "g"}







# ---------------------------------------------------------------- 项目
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


# ---------------------------------------------------------------- 增量
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


# ---------------------------------------------------------------- 后台页面
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
    <button onclick="renderMemory()">渲染到工作区</button>
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
async function renderMemory() {
  const owner = document.getElementById('m-owner').value;
  const d = await api(`/api/v1/projects/${owner}/render-memory`, {method:'POST'});
  alert(d.code === 0 ? `已渲染 ${d.data.count} 条到工作区\\ncommit: ${d.data.commit || '（无变化）'}` : d.message);
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


def handle_admin():
    return 200, ADMIN_HTML, "text/html; charset=utf-8"


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

def handle_task_list(params):
    limit = min(int(q1(params, "limit", 20)), 100)
    rows = q("SELECT id, project_id, user_id, status, error, created_at, finished_at "
             "FROM task ORDER BY id DESC LIMIT %s", (limit,))
    return 200, {"code": 0, "data": rows}


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

# ---------------------------------------------------------------- 记忆
def _bigrams(text):
    t = re.sub(r"[\s，。、；：！？「」『』（）()\[\]【】,.;:!?·]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def relevance(memory_text, query):
    """记忆和当前这句话的相关度：字符 bigram 的 Jaccard"""
    a, b = _bigrams(memory_text), _bigrams(query)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


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


def build_context(project_id, user_id, query=None):
    """拼出这一轮要注入的上下文：项目 + 项目规矩 + 个人习惯"""
    proj = get_project(project_id)
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    pm = q("SELECT id, text, hits FROM memory WHERE scope='project' AND owner_id=%s AND status=1 "
           "ORDER BY hits DESC, id DESC LIMIT 30", (project_id,))
    um = q("SELECT id, text, hits FROM memory WHERE scope='user' AND owner_id=%s AND status=1 "
           "ORDER BY hits DESC, id DESC LIMIT 30", (str(user_id),))

    parts = [f"【项目】{proj['name']}" + (f"（{proj['code']}）" if proj.get("code") else "")]
    if meta:
        parts.append("【工程概况】" + json.dumps(meta, ensure_ascii=False))
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

    parts.append(
        "【可用工具】本机已装好命令行工具 jl-tool，**直接用，不需要先 list**。参数必须是 JSON：\n"
        '  /home/baize/jl-service/scripts/jl-tool list                        # 列工具\n'
        '  jl-tool get_project \'{"projectId":"' + project_id + '"}\'\n'
        '  jl-tool search_records \'{"projectId":"' + project_id + '","place":"4#楼东侧外架"}\'\n'
        '  jl-tool search_standards \'{"query":"连墙件 间距"}\'\n'
        '  jl-tool create_doc_draft \'{"projectId":"' + project_id + '","kind":"监理通知单",'
        '"obj":"部位","fields":{...},"bases":[{"kind":"rule","ref":"JGJ130-2011#6.4.3","text":"..."}]}\'\n'
        "规则：\n"
        "1. 用户说「出/写/生成 通知单、记录、日志」时，**必须真的调用 create_doc_draft**，"
        "不能只在回复里描述文书内容。\n"
        "2. 写文书前先用 search_standards 查到依据，把条号写进 bases。\n"
        "3. create_doc_draft 会把文件写进项目工作区并 git 提交。\n"
        "4. 缺的字段留空，会标成「待补」，不要编。"
    )
    parts.append(
        "【回复格式】最后一段话必须严格是下面三行（没有内容写「无」）：\n"
        "结论：一句话说做了什么、结论是什么\n"
        "依据：引用的规范号 + 条号，多条用、隔开；没有就写无\n"
        "待补：还缺哪些信息，用、隔开；没有就写无\n"
        "不要写文件路径、不要写提交号、不要用 Markdown、不要复述你查了哪些工具。"
    )
    parts.append(f"【当前】项目 {project_id} · 用户 {user_id}")
    return "\n\n".join(parts)


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


def _rerender_if_project(scope, owner):
    """项目记忆一变，就把 memories/project.md 重渲染并提交 —— 不用人工点"""
    if scope != "project":
        return None
    try:
        return render_project_memory(owner)
    except Exception:
        traceback.print_exc()
        return None


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


def handle_memory_delete(mid):
    rows = q("SELECT id, scope, owner_id FROM memory WHERE id=%s", (mid,))
    exec_sql("DELETE FROM memory WHERE id=%s", (mid,))
    rendered = _rerender_if_project(rows[0]["scope"], rows[0]["owner_id"]) if rows else None
    return 200, {"code": 0, "data": {"ok": True, "rendered": rendered}}

# ---------------------------------------------------------------- 完成事件
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


def notify_worker():
    while True:
        msg = NOTIFY.get()
        try:
            on_notification(msg)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- 鉴权
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0") == "1"
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "720"))  # 30 天


def _hash_token(t):
    import hashlib
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def issue_token(user_id, device=None):
    import secrets
    raw = secrets.token_hex(32)
    exec_sql("INSERT INTO auth_token (user_id, token_hash, device, expires_at) "
             "VALUES (%s,%s,%s, DATE_ADD(NOW(), INTERVAL %s HOUR))",
             (user_id, _hash_token(raw), device, TOKEN_TTL_HOURS))
    return raw


def resolve_token(raw):
    """返回 user 行，无效则 None"""
    if not raw:
        return None
    rows = q("SELECT u.id, u.name, u.role, u.tenant_id FROM auth_token t "
             "JOIN app_user u ON u.id = t.user_id "
             "WHERE t.token_hash=%s AND t.revoked=0 AND t.expires_at > NOW() "
             "AND u.status=1", (_hash_token(raw),))
    return rows[0] if rows else None


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


def handle_logout(headers):
    raw = bearer(headers)
    if raw:
        exec_sql("UPDATE auth_token SET revoked=1 WHERE token_hash=%s", (_hash_token(raw),))
    return 200, {"code": 0, "data": {"ok": True}}


def bearer(headers):
    v = headers.get("Authorization") or ""
    return v[7:].strip() if v.lower().startswith("bearer ") else None

# ---------------------------------------------------------------- 影像
BLOB_ROOT = f"{ROOT}/data/blobs"


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


def handle_upload(project_id, params, data):
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    if not data:
        return 400, {"code": 400, "message": "空文件"}
    if len(data) > 20 * 1024 * 1024:
        return 413, {"code": 413, "message": "超过 20MB"}

    sha, blob_path, size = store_blob(data)
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


def handle_photo_list(params):
    project_id = q1(params, "projectId")
    if not project_id:
        return 400, {"code": 400, "message": "projectId 必填"}
    limit = min(int(q1(params, "limit", 20)), 100)
    rows = q("SELECT id, sha256, blob_path, place, weather, shot_at, src, git_commit "
             "FROM photo WHERE project_id=%s ORDER BY id DESC LIMIT %s", (project_id, limit))
    for r in rows:
        r["url"] = f"/api/v1/blobs/{r['sha256']}"
    return 200, {"code": 0, "data": rows}


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

# ---------------------------------------------------------------- 小程序兼容层
def parse_multipart(body, content_type):
    """从 multipart/form-data 里抠出文件字节。wx.uploadFile 用的是这种。"""
    import re as _re
    m = _re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        return b""
    boundary = (m.group(1) or m.group(2)).strip()
    sep = b"--" + boundary.encode()
    parts = body.split(sep)
    for part in parts:
        if b'name="file"' not in part and b"filename=" not in part:
            continue
        idx = part.find(b"\r\n\r\n")
        if idx < 0:
            continue
        data = part[idx + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return data
    return b""


def default_project_id():
    rows = q("SELECT id FROM project ORDER BY created_at LIMIT 1")
    return rows[0]["id"] if rows else None


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


def handle_geo_reverse(params):
    """逆地理：先把项目地址回给它，接第三方后替换"""
    pid = q1(params, "projectId") or default_project_id()
    proj = get_project(pid) if pid else {}
    addr = proj.get("addr") or (proj.get("meta") or {}).get("addr") or "当前位置"
    return 200, {"code": 0, "data": {"address": addr, "stub": True}}


def handle_weather(params):
    """天气：先返回占位，接第三方后替换"""
    return 200, {"code": 0, "data": {"text": "多云", "temp": 26, "wind": "东南风 2 级", "stub": True}}

# ---------------------------------------------------------------- 首屏聚合
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATE_TO_ST = {"待补": "wait", "待签": "sign", "已签": "ok"}
KIND_TO_ICON = {"旁站记录": "doc", "监理通知单": "warn", "监理日志": "pen"}
KIND_TO_TINT = {"旁站记录": "b", "监理通知单": "y", "监理日志": "g"}


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


# ---------------------------------------------------------------- 业务接口
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
    missing = [k for k, v in (fields or {}).items() if not str(v or "").strip()]
    return 200, {"code": 0, "data": {
        "id": d["id"], "projectId": d["project_id"], "kind": d["kind"], "obj": d["obj"],
        "state": d["state"], "fields": fields or {}, "missing": missing,
        "bases": bases or [], "path": d["path"], "commit": (d["git_commit"] or "")[:10],
        "createdAt": str(d["created_at"] or ""), "updatedAt": str(d["updated_at"] or ""),
    }}


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
    if not isinstance(patch, dict) or not patch:
        return 400, {"code": 400, "message": "fields 必填"}

    # 只能改本文书已有的字段，避免乱塞
    for k, v in patch.items():
        if k in fields:
            fields[k] = "" if v is None else str(v)

    missing = [k for k, v in fields.items() if not str(v or "").strip()]
    state = body.get("state") or ("待补" if missing else "待签")

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
                                     "fields": fields}}


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
    proj = get_project(project_id)
    meta = proj.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
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

# ---------------------------------------------------------------- 项目 / 会话
def get_project(project_id):
    rows = q("SELECT * FROM project WHERE id=%s", (project_id,))
    if not rows:
        raise ValueError(f"项目不存在: {project_id}")
    return rows[0]


def get_or_create_thread(project_id, user_id, kind="chronic"):
    rows = q("SELECT thread_id FROM thread_map WHERE project_id=%s AND user_id=%s AND kind=%s",
             (project_id, user_id, kind))
    if rows:
        return rows[0]["thread_id"], False

    ws = f"{WS_ROOT}/{project_id}"
    # sandbox 必须放开：工具要调 docker 访问数据库和工作区，
    # 默认沙箱会拦住，表现为 exec_command 失败但 turn 仍显示 done。
    r = engine().call("thread/start", {
        "cwd": ws,
        "sandbox": "danger-full-access",
        "approvalPolicy": "never",
    }, 120)
    if not r or "error" in (r or {}):
        raise RuntimeError(f"thread/start 失败: {r}")
    tid = r["result"]["thread"]["id"]
    exec_sql("INSERT INTO thread_map (project_id, user_id, thread_id, kind) VALUES (%s,%s,%s,%s)",
             (project_id, user_id, tid, kind))
    return tid, True


# ---------------------------------------------------------------- 业务
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

# ---------------------------------------------------------------- 模型
LLM_URL = ENV.get("LLM_BASE", "https://llmapi.xfcxb.com/v1").rstrip("/") + "/responses"
LLM_MODEL = ENV.get("LLM_MODEL", "gpt-5.6-sol")
JL_TOOL_BIN = f"{ROOT}/scripts/jl-tool"

SYSTEM_PROMPT = """你是现场监理助手，帮监理员把现场的情况记成合规的文书。

你可以调用工具。**每一步只输出一个 JSON，不要任何多余文字、不要代码块围栏。**

要调工具时：
{"action":"tool","tool":"<工具名>","args":{...}}

要收尾时：
{"action":"final","text":"<给用户看的回复，中文，简洁>"}

规矩：
1. 写文书（create_doc_draft）之前，必须先用 search_standards 查到依据，把条号写进 bases。
2. 部位要写清，例如「4#楼东侧外架」「3#5F 顶板」。
3. 拿不准的字段留空，系统会标成「待补」，不要编。
4. 查不到规范就明说「没查到依据」，不要凭印象写条号。
5. 最多调 4 次工具，够了就 final。
"""

ALLOWED_TOOLS = None


def allowed_tools():
    global ALLOWED_TOOLS
    if ALLOWED_TOOLS is None:
        try:
            p = subprocess.run([JL_TOOL_BIN, "list"], capture_output=True, text=True, timeout=60)
            ALLOWED_TOOLS = set(json.loads(p.stdout.strip() or "[]"))
        except Exception:
            ALLOWED_TOOLS = {"get_project", "search_records", "search_standards",
                             "create_doc_draft", "get_context"}
    return ALLOWED_TOOLS


def llm(messages, max_tokens=1500, timeout=180):
    """直接调中转的 Responses API"""
    payload = {"model": LLM_MODEL, "input": messages, "max_output_tokens": max_tokens}
    req = urllib.request.Request(
        LLM_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ENV.get("JL_LLM_KEY", "")},
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
    if name == "get_project":
        return "读工程概况"
    if name == "get_context":
        return "读项目上下文"
    return f"调用 {name}"

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


def run_agent_loop(task_id, project_id, user_id, text):
    """模型决定调哪个工具，BFF 负责执行 —— 不依赖引擎的 shell"""
    # 排队：拿不到槽位就等，任务状态停在 queued
    with TURN_SLOTS:
        exec_sql("UPDATE task SET status='running', started_at=NOW(3) WHERE id=%s", (task_id,))
        _run_agent_loop_inner(task_id, project_id, user_id, text)


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

# ---------------------------------------------------------------- 业务
# ---------------------------------------------------------------- 结构化回答
# 让模型按固定三行回答，解析成字段，前端就能做"表单式"排版
ANSWER_KEYS = {
    "结论": "conclusion",
    "依据": "basis",
    "待补": "gaps",
    "缺项": "gaps",
    "下一步": "next",
}
ANSWER_RE = re.compile(
    r"^[\s*#\-【\[]*(结论|依据|待补|缺项|下一步)[】\]]*\s*[：:]\s*(.*)$"
)


REF_RE = re.compile(
    r"((?:GB|JGJ|CJJ|DB|JG)\s?[0-9T]+\s?[-–—]\s?[0-9]{2,4})"
    r"(?:\s*第?\s*#?\s*([0-9]+(?:\.[0-9]+)*)\s*条?)?"
)

# "待补：A、B、C" 里 A/B/C 也当成要标的字段
GAP_INLINE_RE = re.compile(r"待补[：:]\s*([^。；\n]+)")


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
        events.sort()

        for start, end, kind, val, clause in events:
            if start < pos:
                continue
            if start > pos:
                out.extend(_split_num(s[pos:start]))
            if kind == "r":
                out.append({"s": "r", "v": val, "code": val.split("第")[0].strip(), "clause": clause or ""})
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


def build_answer(doc, cites, text):
    """只产出"行内标注"所需的数据：富文本片段 + 待补字段名。不做表格。"""
    text = clean_reply(text)

    missing = []
    if doc:
        fields = doc.get("fields") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except Exception:
                fields = {}
        missing = [k for k, v in fields.items() if not str(v or "").strip()]

    return {
        "rich": rich_reply(text, missing),
        "gaps": missing,
        "text": text,
    }


def parse_answer(text):
    """把'结论：…\n依据：…\n待补：…'拆成字段。拆不出来就返回 None。"""
    if not text:
        return None
    out = {}
    loose = []
    for raw in text.splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        m = ANSWER_RE.match(line)
        if m:
            key = ANSWER_KEYS[m.group(1)]
            val = m.group(2).strip()
            if val:
                out[key] = (out.get(key, "") + " " + val).strip() if key in out else val
            else:
                out.setdefault(key, "")
        else:
            loose.append(line)

    if not out.get("conclusion") and not out.get("basis"):
        # 模型没按格式来：退化成启发式拆分，保证界面永远有结构
        return enrich_answer(_split_by_heuristic(text))

    if loose:
        out.setdefault("rest", " ".join(loose))
    return enrich_answer(out)


# ---- 富文本分段：普通 / 数字 / 加粗 ----
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# 只高亮"带单位的量"和"独立的整数"，避免把 JGJ 130-2011 / 6.4.3 拆碎
NUM_RE = re.compile(
    r"[+\-±]?\d+(?:\.\d+)?\s?(?:mm|cm|m|㎡|m3|kg|kN|MPa|℃|%|处|根|个|条|块|层|号|天|日|小时|分钟)"
    r"|(?<![\w.\-])[0-9]+(?![\w.\-])"
)


def rich_segments(text):
    """把一句话切成 [{s,v}]，s ∈ t(普通)/n(数字)/b(加粗)。"""
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

    pos = 0
    for m in BOLD_RE.finditer(text):
        if m.start() > pos:
            push_num(text[pos:m.start()])
        out.append({"s": "b", "v": m.group(1)})
        pos = m.end()
    if pos < len(text):
        push_num(text[pos:])

    return [x for x in out if x["v"]]


GAP_SPLIT_RE = re.compile(r"[、，,；;/]|\s{2,}")


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


def enrich_answer(ans):
    """给结构化回答补上富文本分段和 chips。"""
    if not ans:
        return None
    for key in ("conclusion", "gaps", "rest"):
        if ans.get(key):
            ans[key + "Rich"] = rich_segments(ans[key])
    if ans.get("basis"):
        # 依据里全是规范号，拆数字反而难读
        ans["basisRich"] = [{"s": "t", "v": ans["basis"]}]
    items = split_gaps(ans.get("gaps"))
    if items:
        ans["gapItems"] = items
    return ans


STD_RE = re.compile(r"((?:GB|JGJ|CJJ|DB|JG)\s?[0-9T]+\s?[-–—]\s?[0-9]{2,4})(?:\s*第?\s*([0-9.]+)\s*条)?")


def _split_by_heuristic(text):
    """不要格式也能拆：结论取第一句，依据抠标准号，待补取含'缺/待补/未提供'的句子。"""
    text = (text or "").strip()
    if not text:
        return None
    sents = [x.strip() for x in re.split(r"(?<=[。！？；\n])", text) if x.strip()]
    if not sents:
        return None

    out = {"conclusion": sents[0][:200]}

    codes = []
    for m in STD_RE.finditer(text):
        code = m.group(1).replace(" ", "")
        clause = m.group(2)
        item = code + (" 第" + clause + "条" if clause else "")
        if item not in codes:
            codes.append(item)
    if codes:
        out["basis"] = "、".join(codes[:3])

    gaps = [s for s in sents[1:] if re.search(r"待补|缺|未提供|不详|没有给|留待", s)]
    if gaps:
        out["gaps"] = " ".join(gaps[:2])[:160]

    rest = [s for s in sents[1:] if s not in gaps]
    if rest:
        out["rest"] = "".join(rest)[:600]
    return out


# ---------------------------------------------------------------- 回复清洗
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
    # 3) 服务器绝对路径
    t = re.sub(r"/home/[^\s，。；：）)、]+", "", t)
    t = re.sub(r"data/workspaces/[^\s，。；：）)、]+", "", t)
    t = re.sub(r"docs/\d{4}-\d{2}-\d{2}/[^\s，。；：）)、]*\.md", "", t)
    # 4) URL 百分号编码长串（文件名被编码后的样子）
    t = re.sub(r"(?:%[0-9A-Fa-f]{2}){2,}", "", t)
    # 5) 「提交号 d227936」这类内部线索
    t = re.sub(r"[，,；;]?\s*提交号\s*[0-9a-fA-F]{6,40}", "", t)
    # 6) 去掉开头的过程叙述（"我先核对…""我会先查…"）——轨迹里已经显示了
    t = re.sub(r"^[^。\n]{0,30}(我先|我会先|我将先|我先来)[^。\n]*。\s*", "", t)

    # 7) 句子去重：模型常把同一句换个说法重复一遍
    import difflib as _dl
    parts = [x.strip() for x in re.split(r"(?<=[。！？\n])", t) if x.strip()]
    kept = []
    for seg in parts:
        if any(_dl.SequenceMatcher(None, seg, k).ratio() > 0.72 for k in kept):
            continue
        kept.append(seg)
    t = "".join(kept)

    # 8) 长度上限：一句话能说完的，别写成小作文
    LIMIT = 160
    if len(t) > LIMIT:
        cut = t[:LIMIT]
        pos = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"))
        t = cut[: pos + 1] if pos > 60 else cut.rstrip() + "…"

    # 9) 空括号、多余空白
    t = re.sub(r"[（(]\s*[）)]", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\s+\n", "\n", t)
    return t.strip()

# ---------------------------------------------------------------- 权限
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


def guard_project(project_id, user):
    """返回 None 表示放行，否则返回 403 的响应"""
    if member_of(project_id, user):
        return None
    return 403, {"code": 403, "message": "无权访问该项目"}

# ---------------------------------------------------------------- 执行方式
ENGINE_MODE = os.environ.get("ENGINE_MODE", "cli")   # cli = 引擎自己跑工具；loop = BFF 驱动


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

    return thread_id, "\n".join(texts).strip(), timeline, cites


def _set_progress(task_id, steps):
    """把执行步骤写进任务，前端每秒拉一次就能看到进展"""
    try:
        exec_sql("UPDATE task SET progress=%s WHERE id=%s",
                 (json.dumps(steps, ensure_ascii=False), task_id))
    except Exception:
        pass


def _run_cli_turn(task_id, project_id, user_id, text):
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

        hint = (
            "\n\n[输出要求] 最后一句话请严格写成三行，不要写别的东西：\n"
            "结论：一句话说做了什么、结论是什么\n"
            "依据：规范号 + 条号；没有就写无\n"
            "待补：还缺哪些字段，用、隔开；没有就写无"
        )
        cmd = [ENGINE, "exec", "-C", ws,
               "--dangerously-bypass-approvals-and-sandbox",
               "--json", "-o", out_file, text + hint]

        before = q("SELECT COALESCE(MAX(id),0) AS m FROM doc WHERE project_id=%s", (project_id,))
        doc_before = before[0]["m"] if before else 0

        steps, by_cmd_id, lines = [], {}, []
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1, env=ENGINE_ENV)

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

        _watchdog(proc, 300)
        deadline = time.time() + 600
        for line in proc.stdout:
            lines.append(line)
            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, 600)

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
        answer = build_answer(doc_row, cites, final_text)

        exec_sql(
            "INSERT INTO message (project_id, user_id, client_id, role, text, trace, trace_sum, ms, task_id, sync, cites, answer) "
            "VALUES (%s,%s,%s,'assistant',%s,%s,%s,%s,%s,1,%s,%s)",
            (project_id, user_id, f"r-{task_id}-{int(time.time())}", clean_reply(final_text),
             json.dumps(trace, ensure_ascii=False) if trace else None, trace_sum, ms, task_id,
             json.dumps(cites, ensure_ascii=False) if cites else None,
             json.dumps(answer, ensure_ascii=False) if answer else None))
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


# 每个「项目+用户」一把锁：同一个人的消息排队执行，不同人互不影响
_USER_LOCKS = {}
_USER_LOCKS_GUARD = threading.Lock()


def _user_lock(project_id, user_id):
    key = (str(project_id), str(user_id))
    with _USER_LOCKS_GUARD:
        lk = _USER_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _USER_LOCKS[key] = lk
        return lk


def run_turn(task_id, project_id, user_id, text):
    # 先排自己的队，再抢全局并发槽。
    # 这样同一个人连发三条，后两条会等前一条做完 —— 不会三个 turn 同时改同一个工作区。
    with _user_lock(project_id, user_id):
        with TURN_SLOTS:
            exec_sql("UPDATE task SET status='running', started_at=NOW(3) WHERE id=%s", (task_id,))
            if ENGINE_MODE == "cli":
                _run_cli_turn(task_id, project_id, user_id, text)
            else:
                _run_agent_loop_inner(task_id, project_id, user_id, text)

# ---------------------------------------------------------------- 执行（CLI）

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


# ---------------------------------------------------------------- 业务
def handle_chat(body, headers=None):
    project_id = body.get("projectId")
    user_id = int(body.get("userId") or 1)
    client_id = body.get("clientId") or f"c-{uuid.uuid4().hex[:16]}"
    text = (body.get("text") or "").strip()

    if not project_id or not text:
        return 400, {"code": 400, "message": "projectId 和 text 必填"}

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
        (project_id, user_id, client_id, json.dumps({"text": text}, ensure_ascii=False)))

    exec_sql("INSERT INTO message (project_id, user_id, client_id, role, text, task_id, sync) "
             "VALUES (%s,%s,%s,'user',%s,%s,1)",
             (project_id, user_id, client_id, text, task_id))

    # 立刻返回，任务在后台跑 —— 手机断了也照跑
    threading.Thread(target=run_turn, args=(task_id, project_id, user_id, text),
                     daemon=True).start()

    return 202, {"code": 0, "data": {
        "taskId": task_id, "status": "running", "poll": f"/api/v1/tasks/{task_id}"}}

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


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "jl-bff/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if AUTH_REQUIRED and path not in ("/healthz", "/admin", "/admin/", "/api/v1/bootstrap"):
                if not resolve_token(bearer(self.headers)):
                    return self._send(401, {"code": 401, "message": "未登录或令牌失效"})
            if path == "/healthz":
                eng = engine().proc.poll() is None
                return self._send(200, {"ok": True, "engine": "up" if eng else "down"})
            if path in ("/admin", "/admin/"):
                return self._send(*handle_admin())
            if path == "/api/v1/project":
                return self._send(*handle_project_get(q1(parse_qs(urlparse(self.path).query), "id")))
            m = re.match(r"^/api/v1/docs/(\d+)$", path)
            if m:
                doc_id = int(m.group(1))
                usr = resolve_token(bearer(self.headers))
                return self._send(*handle_doc_get(doc_id, usr))
            if path == "/api/v1/standards":
                return self._send(*handle_standards_search(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/audit":
                return self._send(*handle_audit(parse_qs(urlparse(self.path).query),
                                                resolve_token(bearer(self.headers))))
            if path == "/api/v1/stats":
                return self._send(*handle_stats(parse_qs(urlparse(self.path).query),
                                                resolve_token(bearer(self.headers))))
            if path == "/api/v1/members":
                usr = resolve_token(bearer(self.headers))
                if self.command == "POST":
                    return self._send(*handle_member_add(json.loads(self._body() or "{}"), usr))
                return self._send(*handle_members(parse_qs(urlparse(self.path).query), usr))

            m = re.match(r"^/api/v1/tasks/(\d+)/cancel$", path)
            if m and self.command == "POST":
                return self._send(*handle_task_cancel(int(m.group(1)),
                                                      resolve_token(bearer(self.headers))))
            if path == "/api/v1/docs":
                                return self._send(*handle_docs_list(
                    q1(parse_qs(urlparse(self.path).query), "projectId"),
                    resolve_token(bearer(self.headers)),
                ))
            if path == "/api/v1/geo/reverse":
                return self._send(*handle_geo_reverse(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/weather":
                return self._send(*handle_weather(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/bootstrap":
                return self._send(*handle_bootstrap(parse_qs(urlparse(self.path).query),
                                                    resolve_token(bearer(self.headers))))
            if path == "/api/v1/me":
                u = resolve_token(bearer(self.headers))
                return self._send(200, {"code": 0, "data": u})
            if path == "/api/v1/standard":
                return self._send(*handle_standard(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/projects":
                return self._send(*handle_project_list({}))
            if path == "/api/v1/tasks":
                return self._send(*handle_task_list(parse_qs(urlparse(self.path).query)))
            m = re.match(r"^/api/v1/tasks/(\d+)$", path)
            if m:
                return self._send(*handle_task(int(m.group(1)), resolve_token(bearer(self.headers))))
            if path.startswith("/api/v1/memories"):
                return self._send(*handle_memory_list(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers)),
                ))
            if path.startswith("/api/v1/messages"):
                return self._send(*handle_messages(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/photos":
                return self._send(*handle_photo_list(parse_qs(urlparse(self.path).query)))
            m2 = re.match(r"^/api/v1/blobs/([a-f0-9]{64})$", path)
            if m2:
                return self._send(*handle_blob(m2.group(1)))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})

    def do_DELETE(self):
        try:
            m = re.match(r"^/api/v1/memories/(\d+)$", self.path)
            if m:
                return self._send(*handle_memory_delete(int(m.group(1))))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})
    def do_PUT(self):
        try:
            path = urlparse(self.path).path
            if AUTH_REQUIRED and not resolve_token(bearer(self.headers)):
                return self._send(401, {"code": 401, "message": "未登录或令牌失效"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/v1/project":
                return self._send(*handle_project_save(body))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})

    def do_PATCH(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            usr = resolve_token(bearer(self.headers))

            m = re.match(r"^/api/v1/docs/(\d+)$", path)
            if m:
                return self._send(*handle_doc_patch(int(m.group(1)), body, usr))

            m = re.match(r"^/api/v1/photos/(\d+)$", path)
            if m:
                return self._send(*handle_photo_patch(int(m.group(1)), body, usr))

            m = re.match(r"^/api/v1/memories/(\d+)$", self.path)
            if m:
                return self._send(*handle_memory_patch(int(m.group(1)), body))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if AUTH_REQUIRED and path not in ("/api/v1/auth/login",):
                if not resolve_token(bearer(self.headers)):
                    return self._send(401, {"code": 401, "message": "未登录或令牌失效"})
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

            # 上传走原始字节，不能当 JSON 解
            if path == "/api/v1/upload":
                pid = q1(qs, "projectId") or default_project_id()
                ctype = self.headers.get("Content-Type") or ""
                data = parse_multipart(raw, ctype) if "multipart/form-data" in ctype else raw
                code, payload = handle_upload(pid, qs, data)
                if code == 200 and isinstance(payload, dict):
                    d = payload.get("data", {})
                    if d.get("sha256"):
                        d["url"] = f"/api/v1/blobs/{d['sha256']}"
                return self._send(code, payload)

            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}

            if path == "/api/v1/auth/wx-phone":
                return self._send(200, {"code": 0, "data": {"phone": body.get("phone") or "", "stub": True}})
            if path in ("/api/v1/auth/wx-login", "/api/v1/auth/login"):
                # 小程序传 {code}，开发期把 code 当 openid 用
                if "code" in body and "openid" not in body:
                    body = dict(body, openid="wx-" + str(body.pop("code"))[:40],
                                name=body.get("name") or "监理员")
                return self._send(*handle_login(body))
            m3 = re.match(r"^/api/v1/tasks/(\d+)/retry$", path)
            if m3:
                return self._send(*handle_task_retry(int(m3.group(1))))
            if path == "/api/v1/auth/logout":
                return self._send(*handle_logout(self.headers))
            if path == "/api/v1/chat":
                return self._send(*handle_chat(body, self.headers))
            if path == "/api/v1/project":
                return self._send(*handle_project_save(body))
            if path == "/api/v1/docs":
                return self._send(*handle_doc_create(body))
            if path == "/api/v1/standard":
                return self._send(*handle_standard(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/projects":
                return self._send(*handle_project_create(body))
            if path == "/api/v1/memories":
                return self._send(*handle_memory_create(body))
            m = re.match(r"^/api/v1/projects/([\w-]+)/render-memory$", path)
            if m:
                return self._send(*handle_render_memory(m.group(1)))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})


def handle_render_memory(project_id):
    try:
        return 200, {"code": 0, "data": render_project_memory(project_id)}
    except Exception as e:
        return 500, {"code": 500, "message": str(e)}

def main():
    engine()  # 预热
    threading.Thread(target=notify_worker, daemon=True).start()
    threading.Thread(target=sweeper, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(f"jl-bff 已启动 :{PORT}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
