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
}


class AppServerError(Exception):
    pass


class _Client:
    def __init__(self, project_id):
        self.project_id = project_id
        self.ws = os.path.join(WS_ROOT, project_id)
        self.proc = None
        self.thread_id = None
        self.seq = 0
        self.pending = {}
        self.notifications = []
        self._lock = threading.Lock()
        self._dead = False

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
        self.proc = subprocess.Popen(
            [ENGINE, "app-server"],
            cwd=self.ws,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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
        except Exception:
            pass
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

    def open_thread(self, model=None, developer_instructions=None):
        """开一条独立线程，不占用主线程。

        复核用它：换一个模型、换一份角色说明，互不干扰。
        比让主 agent 自己调 spawn_agent 可靠 —— 那个工具偶尔会报 unsupported。
        """
        params = {
            "cwd": self.ws,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        res = self.call("thread/start", params, timeout=120)
        return res.get("threadId") or (res.get("thread") or {}).get("id")

    def run_on_thread(self, thread_id, prompt, timeout=600):
        """在指定线程上跑一轮，返回 (文本, 事件列表)"""
        start = len(self.notifications)
        self.call("turn/start", {"threadId": thread_id,
                              "input": [{"type": "text", "text": prompt}]}, timeout=120)
        text_parts, events = [], []
        t0 = time.time()
        cursor = start
        while time.time() - t0 < timeout:
            ready = self.notifications[cursor:]
            cursor = len(self.notifications)
            for msg in ready:
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


def _client(project_id):
    with _LOCK:
        c = _CLIENTS.get(project_id)
        if c is None or not c.alive():
            if c is not None:
                c.close()
            c = _Client(project_id)
            c._spawn()
            _CLIENTS[project_id] = c
        return c


def run_stream(project_id, prompt, images=None, timeout=600):
    """跑一轮，边跑边吐事件。

    事件形状和 `exec --json` 对齐（{"type":"item.completed","item":{...}}），
    这样 agent.py 里那套解析两个后端共用。
    """
    c = _client(project_id)
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


def run_isolated(project_id, prompt, model=None, developer_instructions=None, timeout=600):
    """开一条独立线程跑一轮，跑完就关。

    给复核用：独立模型、独立角色、独立上下文，不污染主对话。
    """
    c = _client(project_id)
    thread_id = c.open_thread(model=model, developer_instructions=developer_instructions)
    try:
        return c.run_on_thread(thread_id, prompt, timeout=timeout)
    finally:
        try:
            c.call("thread/close", {"threadId": thread_id}, timeout=30)
        except Exception:
            pass
