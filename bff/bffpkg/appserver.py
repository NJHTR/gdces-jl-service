# -*- coding: utf-8 -*-
"""常驻 agent 服务客户端（easyCode app-server）

为什么要有它：现在每轮任务用 `easycode exec` 起一个子进程，启动 + 扫仓库要好几秒，
首次响应 8~14 秒。app-server 常驻后实测：
    简单一轮 2.6 / 2.3 秒
    带工具一轮 4.8 / 4.1 秒

设计要点：
  - 每个项目一条常驻进程 + 一个线程（会话可以复用，上下文不丢）
  - 对外只暴露 `run_turn()`，返回和 exec 模式一样的"事件列表"，
    这样 agent.py 的解析代码不用改（同一份解析，两个后端）
  - 进程挂了自动重启一次，别让一个坏掉的进程拖死整个服务

踩过的坑（写在这里省得再踩）：
  - `thread/start` 的 cwd 必须是绝对路径，相对路径会让 exec_command 起不来，
    报 "Failed to create unified exec process: No such file or directory"
  - 子进程环境要带完整 PATH，否则 shell 工具找不到命令
"""
import json
import os
import sys
import subprocess
import threading
import time

ENGINE = os.environ.get("ENGINE_BIN", "/home/baize/jl-service/repos/easycode/.target/release/easycode")
WS_ROOT = os.environ.get("WS_ROOT", "/home/baize/jl-service/data/workspaces")

_LOCK = threading.Lock()
_CLIENTS = {}

# app-server 的 item 类型是驼峰，exec 的是下划线 —— 统一成 exec 的样子
_ITEM_MAP = {
    # 驼峰 → 下划线。这些名字是实测出来的，见文件头的说明
    "agentMessage": "agent_message",
    "userMessage": "user_message",
    "commandExecution": "command_execution",
    "reasoning": "reasoning",
    "error": "error",
    "functionCall": "function_call",
    "functionCallOutput": "function_call_output",
    # 多 agent 的活动：子 agent 启动/交互，以及协作工具调用
    "subAgentActivity": "sub_agent_activity",
    "collabAgentToolCall": "collab_tool_call",
    "mcpToolCall": "mcp_tool_call",
    "webSearch": "web_search",
}


class AppServerError(Exception):
    pass


HOMES_ROOT = os.environ.get("JL_HOMES_ROOT", "/home/baize/jl-service/data/homes")
_MAIN_CONFIG = "/home/baize/.easycode/config.toml"


def ensure_home(user_id):
    """给每个用户准备一个 EASYCODE_HOME。

    引擎的记忆根是 $EASYCODE_HOME/memories（memories/read/src/lib.rs:13），
    所以「一个用户一个家目录」= 记忆、会话、技能天然隔离，
    不用自己写一套隔离逻辑（也就不会写错）。
    """
    home = os.path.join(HOMES_ROOT, str(user_id))
    cfg = os.path.join(home, "config.toml")
    try:
        os.makedirs(home, exist_ok=True)
        # 注意：不能只在"文件不存在"时写。已有家目录会永远缺 keys
        #（实测 950 的 memories 就是关的，因为 ensure_home 只在创建时写一次）
        if os.path.exists(cfg) or True:
            base = ""
            if os.path.exists(cfg):
                try:
                    with open(cfg, encoding="utf-8") as f:
                        base = f.read()
                except OSError:
                    base = ""
            if not base:
                try:
                    with open(_MAIN_CONFIG, encoding="utf-8") as f:
                        base = f.read()
                except Exception:
                    base = ""
            # 注意：必须插在 [features] 表**里面**。
            # 追加到文件末尾的话，会变成最后一个表的键（之前就踩了：
            # memories 落进 [agent_roles.reviewer]，引擎根本没启用记忆）。
            lines = base.splitlines()
            if "[features]" in lines:
                at = lines.index("[features]")
                if not any(l.strip().startswith("memories") for l in lines[at:]):
                    lines.insert(at + 1, "memories = true")
                # 关键：不加这两行的话，MCP 工具会被"推迟"而模型看不见，
                # 于是它改用 shell 调 jl-tool CLI —— 每次调用都要起一个容器（2~4 秒）。
                # 实测：一次出稿 13~17 次 shell 调用，时间全耗在这。
                # 联网搜索：不开这个键，引擎的 resolve_web_search_mode 返回 None，
                # web_search 工具根本不出现（不是线路不支持，实测百炼能用）
                if not any(l.strip().startswith("web_search") for l in lines):
                    first_table = next((i for i, l in enumerate(lines) if l.startswith("[")), len(lines))
                    lines.insert(first_table, 'web_search = "live"')
                if not any(l.strip().startswith("tool_search_always_defer_mcp_tools") for l in lines):
                    lines.insert(at + 1, "tool_search_always_defer_mcp_tools = false")
                base = "\n".join(lines) + "\n"
            else:
                base = base.rstrip() + "\n\n[features]\nmemories = true\n"
            # 记忆整理用它自己的模型（memories.extract_model / consolidation_model）。
            # 不配的话回落到 provider 的"首选抽取模型"，我们的 dashscope 不支持，
            # 日志里就是 "Phase 1 job failed ... stream disconnected before completion"
            #（实测 980 有 157 条可整理 rollout，raw 一直是 "No raw memories yet"）。
            try:
                from .core import LLM_MODEL as _mem_model, ENV as _env
            except Exception:
                _mem_model = os.environ.get("LLM_MODEL") or "qwen3.7-plus"
                _env = os.environ
            # 两个阶段用不同模型（实测出来的组合）：
            # · 抽取（phase1）：主模型即可，产出 raw_memories
            # · 合并（phase2）：要按 apply_patch 写 MEMORY.md，主模型写不出合法补丁
            #   （报 "apply_patch verification failed: invalid hunk"），
            #   复核那个 flash 模型反而守格式 —— 实测换过去就产出了 22KB MEMORY.md
            _cons_model = (_env.get("REVIEW_MODEL")
                           or _env.get("LLM_FALLBACK_MODEL")
                           or "qwen3.8-flash")
            want = {
                "max_rollouts_per_startup": "20",
                "extract_model": _mem_model,
                "consolidation_model": _cons_model,
            }
            if "[memories]" not in base:
                base = base.rstrip() + "\n\n[memories]\n" + "".join(
                    '%s = "%s"\n' % (k, v) for k, v in want.items())
            else:
                head, tail = base.split("[memories]", 1)
                # 只补缺的键，不覆盖人改过的值
                add = "".join('%s = "%s"\n' % (k, v) for k, v in want.items()
                              if ("\n%s " % k) not in ("\n" + tail) and
                              ("\n%s=" % k) not in ("\n" + tail))
                base = head + "[memories]\n" + add + tail.lstrip("\n")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(base)
    except Exception as exc:
        sys.stderr.write("[home] 准备 %s 失败：%s\n" % (home, exc))
    return home


class _Client:
    def __init__(self, project_id, user_id=1):
        self.project_id = project_id
        self.user_id = user_id
        self.ws = os.path.join(WS_ROOT, project_id)
        self.proc = None
        self.thread_id = None
        self.seq = 0
        self.pending = {}
        self.notifications = []
        self._lock = threading.Lock()
        self._dead = False
        self.last_used = time.time()

    # ---------------------------------------------------------------- 进程
    def _spawn(self):
        # 必须用 ENGINE_ENV：BFF 把模型 key 注入在那儿，
        # 只继承 os.environ 会报 "Missing environment variable: DASHSCOPE_API_KEY"
        try:
            from .core import ENGINE_ENV as _base
            env = dict(_base)
        except Exception:
            env = dict(os.environ)
        env["HOME"] = env.get("HOME", "/home/baize")
        env["PATH"] = "/home/baize/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        # 每个用户一个家目录：记忆/会话/技能自动隔离
        env["EASYCODE_HOME"] = ensure_home(self.user_id)
        # 排障用：JL_ENGINE_LOG 可以打开引擎侧模块日志（比如 memories=debug）
        if os.environ.get("JL_ENGINE_LOG"):
            env["RUST_LOG"] = os.environ["JL_ENGINE_LOG"]
        self.proc = subprocess.Popen(
            [ENGINE, "app-server"],
            cwd=self.ws,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=(open(os.path.join("/tmp", "jl-appserver-%s.log" % self.user_id), "ab")
                    if os.environ.get("JL_APPSERVER_LOG") == "1" else subprocess.DEVNULL),
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.call("initialize", {
            "clientInfo": {"name": "jl-bff", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=60)
        self.thread_id = None

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._lock:
                        self.pending[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
        except Exception as e:
            # 解析失败会丢掉一条消息（可能是工具结果或复核），必须留痕
            print("[appserver] 读输出失败: %r | raw=%r" % (e, line[:200]), flush=True)
        self._dead = True

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self._dead

    # ---------------------------------------------------------------- 调用
    def call(self, method, params=None, timeout=180):
        if not self.alive():
            raise AppServerError("引擎进程不可用")
        self.seq += 1
        rid = self.seq
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except Exception as exc:
            raise AppServerError("写不进去：%s" % exc)
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if rid in self.pending:
                    msg = self.pending.pop(rid)
                    if "error" in msg:
                        raise AppServerError(json.dumps(msg["error"], ensure_ascii=False)[:300])
                    return msg.get("result") or {}
            if not self.alive():
                raise AppServerError("引擎进程退出了")
            time.sleep(0.02)
        raise AppServerError("等 %s 超时" % method)

    def open_thread(self, model=None, developer_instructions=None, cwd=None):
        """开一条独立线程，不占用主线程。

        复核用它：换一个模型、换一份角色说明，互不干扰。
        比让主 agent 自己调 spawn_agent 可靠 —— 那个工具偶尔会报 unsupported。
        """
        params = {
            # 复核线程要给干净 cwd：项目 AGENTS.md 里的多 agent 说明会把复核员带偏
            "cwd": cwd or self.ws,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        res = self.call("thread/start", params, timeout=120)
        return res.get("threadId") or (res.get("thread") or {}).get("id")

    def run_on_thread(self, thread_id, prompt, timeout=600, images=None):
        """在指定线程上跑一轮，返回 (文本, 事件列表)"""
        start = len(self.notifications)
        inputs = [{"type": "text", "text": prompt}]
        for img in images or []:
            inputs.append({"type": "localImage", "path": img})
        self.call("turn/start", {"threadId": thread_id, "input": inputs}, timeout=120)
        text_parts, events = [], []
        t0 = time.time()
        cursor = start
        while time.time() - t0 < timeout:
            ready = self.notifications[cursor:]
            cursor = len(self.notifications)
            for msg in ready:
                # 只认本线程的通知：同一个 app-server 里可能有别的线程在跑，
                # 不过滤会把别人的消息、甚至子 agent 的信封混进来
                p_params = msg.get("params") or {}
                tid = p_params.get("threadId") or (p_params.get("thread") or {}).get("id")
                if tid and tid != thread_id:
                    continue
                events.append(msg)
                if msg.get("method") == "item/agentMessage/delta":
                    piece = (msg.get("params") or {}).get("delta") or ""
                    if piece:
                        text_parts.append(piece)
                    continue
                it = (msg.get("params") or {}).get("item") or {}
                if msg.get("method") == "item/completed" and it.get("type") == "agentMessage":
                    t = (it.get("text") or "").strip()
                    if t:
                        text_parts.append(t)
                if msg.get("method") == "turn/completed":
                    return "".join(text_parts).strip(), events
            if not self.alive():
                raise AppServerError("引擎进程中途退出")
            time.sleep(0.05)
        raise AppServerError("复核超过 %d 秒还没结束" % timeout)

    def ensure_thread(self, model=None):
        if self.thread_id:
            return self.thread_id
        params = {
            "cwd": self.ws,          # 必须绝对路径，否则工具起不来
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        res = self.call("thread/start", params, timeout=120)
        self.thread_id = res.get("threadId") or (res.get("thread") or {}).get("id")
        if not self.thread_id:
            raise AppServerError("没拿到 threadId")
        return self.thread_id

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


def recycle_idle(idle_seconds=6 * 3600):
    """回收空闲很久的引擎进程。

    为什么要它：引擎的记忆整理（phase1/phase2）只在**进程启动**时跑，
    一次最多领 2 条 rollout（且要求闲置 >=6h）。我们的 app-server 是常驻的，
    等于永远不启动 → 记忆永远不整理（实测：78 个用户里只有 501 有 MEMORY.md）。
    回收后下一次对话会重启引擎，顺带把这批已闲置的 rollout 整理掉。
    """
    now = time.time()
    out = []
    for key in list(_CLIENTS.keys()):
        c = _CLIENTS.get(key)
        if not c:
            continue
        idle = now - getattr(c, "last_used", now)
        if idle < idle_seconds:
            continue
        try:
            c.close()
        except Exception as e:
            sys.stderr.write("[engine] 回收 %s 失败: %r\n" % (key, e))
            continue
        _CLIENTS.pop(key, None)
        out.append((key, round(idle / 3600.0, 1)))
    if out:
        sys.stderr.write("[engine] 回收空闲引擎 %d 个: %s\n"
                         % (len(out), ", ".join("%s(%.1fh)" % x for x in out)))
    return out


def _client(project_id, user_id=1):
    """会话按「项目 + 用户」隔离。

    只按项目复用的话，同一个项目里的两个用户会共享一条线程 ——
    看到对方的上下文，甚至接着对方的话说，那是串话不是记忆。
    """
    key = "%s#%s" % (project_id, user_id or 1)
    with _LOCK:
        c = _CLIENTS.get(key)
        if c is None or not c.alive():
            if c is not None:
                c.close()
            c = _Client(project_id, user_id)
            c._spawn()
            _CLIENTS[key] = c
        c.last_used = time.time()
        return c


def run_stream(project_id, prompt, images=None, timeout=600, user_id=1):
    """跑一轮，边跑边吐事件。

    事件形状和 `exec --json` 对齐（{"type":"item.completed","item":{...}}），
    这样 agent.py 里那套解析两个后端共用。
    """
    c = _client(project_id, user_id)
    c.ensure_thread(model=os.environ.get("ENGINE_MODEL") or None)

    inputs = [{"type": "text", "text": prompt}]
    for img in images or []:
        inputs.append({"type": "localImage", "path": img})

    start = len(c.notifications)
    res = c.call("turn/start", {"threadId": c.thread_id, "input": inputs}, timeout=120)
    if os.environ.get("JL_APPSERVER_DEBUG"):
        try:
            with open("/tmp/jl-appserver-last.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"turn_start_res": res}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    t0 = time.time()
    cursor = start
    deltas = {}          # item_id -> 拼起来的文本
    while time.time() - t0 < timeout:
        ready = c.notifications[cursor:]
        cursor = len(c.notifications)
        for msg in ready:
            method = msg.get("method") or ""
            if os.environ.get("JL_APPSERVER_DEBUG"):
                try:
                    with open("/tmp/jl-appserver-last.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps(msg, ensure_ascii=False)[:2000] + "\n")
                except Exception:
                    pass
            params = msg.get("params") or {}

            # 流式文本：agentMessage 的正文基本都走这里
            if method == "item/agentMessage/delta":
                iid = params.get("itemId") or params.get("item_id") or ""
                piece = params.get("delta") or params.get("text") or ""
                if iid and piece:
                    deltas[iid] = deltas.get(iid, "") + piece
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                kind = item.get("type") or ""
                body = dict(item)
                body["type"] = _ITEM_MAP.get(kind, kind)
                if body["type"] == "agent_message" and not (body.get("text") or "").strip():
                    body["text"] = deltas.get(item.get("id") or "", "")
                if "exitCode" in body and "exit_code" not in body:
                    body["exit_code"] = body.pop("exitCode")
                yield {"type": "item.completed", "item": body}
                continue

            if method == "error":
                err = params.get("error") or {}
                yield {"type": "item.completed",
                       "item": {"type": "error",
                                "message": err.get("message") or "引擎报错"}}

            if method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") == "failed":
                    err = turn.get("error") or {}
                    yield {"type": "item.completed",
                           "item": {"type": "error",
                                    "message": "这一轮失败：" + (err.get("message") or "未知原因")}}
                yield {"type": "turn.completed"}
                return
        if not c.alive():
            raise AppServerError("引擎进程中途退出")
        time.sleep(0.05)
    raise AppServerError("这一轮超过 %d 秒还没结束" % timeout)


def run_isolated(project_id, prompt, model=None, developer_instructions=None,
                 timeout=600, cwd=None, images=None, user_id=1):
    """开一条独立线程跑一轮，跑完就关。

    给复核用：独立模型、独立角色、独立上下文，不污染主对话。
    """
    c = _client(project_id, user_id)
    thread_id = c.open_thread(model=model, developer_instructions=developer_instructions,
                              cwd=cwd)
    try:
        return c.run_on_thread(thread_id, prompt, timeout=timeout, images=images)
    finally:
        try:
            c.call("thread/close", {"threadId": thread_id}, timeout=30)
        except Exception:
            pass
