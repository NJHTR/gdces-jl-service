#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证三件事：
1. 引擎能不能发现我们的 MCP 工具
2. thread/start 能不能把会话绑到某个项目工作区（cwd）
3. 不带模型 key 时 turn/start 报什么错（确认只差这一样）
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

BIN = "/home/baize/jl-service/repos/easycode/.target/release/easycode"
ENV = dict(os.environ, HOME="/home/baize", PATH="/home/baize/bin:" + os.environ.get("PATH", ""))
WS = "/home/baize/jl-service/data/workspaces/yun-gu-3qi"


class AppServer:
    def __init__(self):
        self.proc = subprocess.Popen(
            [BIN, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=ENV, text=True, bufsize=1,
        )
        self.q = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()
        self._errlog = []
        threading.Thread(target=self._err_reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def _err_reader(self):
        for line in self.proc.stderr:
            self._errlog.append(line.rstrip())

    def send(self, method, params=None):
        rid = self.next_id
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return rid

    def call(self, method, params=None, timeout=60):
        rid = self.send(method, params)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.2, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg
        return None

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    s = AppServer()
    try:
        r = s.call("initialize", {"clientInfo": {"name": "jl-probe", "version": "0.1"}}, 30)
        print("① initialize:", "OK" if r and "result" in r else (r or "超时"))

        # ---- MCP ----
        r = s.call("mcpServerStatus/list", {}, 120)
        if r and "result" in r:
            for srv in r["result"].get("data", []):
                tools = srv.get("tools") or {}
                print(f"② MCP `{srv['name']}` → {srv['serverInfo']['name']} v{srv['serverInfo']['version']}，工具 {len(tools)} 个")
                print("   " + ", ".join(tools.keys()))
        else:
            print("② MCP 状态取不到:", r)

        # ---- thread/start 绑工作区 ----
        r = s.call("thread/start", {"cwd": WS, "mcpServers": ["jl"]}, 90)
        if r is None:
            print("③ thread/start: 超时")
        elif "error" in r:
            print("③ thread/start 错误:", json.dumps(r["error"], ensure_ascii=False)[:400])
        else:
            th = r["result"].get("thread", r["result"])
            tid = th.get("id") or th.get("threadId")
            print("③ thread/start OK  thread =", tid)
            print("   cwd =", th.get("cwd") or th.get("workingDirectory") or "（未返回）")

            # ---- turn/start：没 key 时应报鉴权错 ----
            r2 = s.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "4#楼东侧外架连墙件少了两根"}]}, 60)
            if r2 is None:
                print("④ turn/start: 超时")
            elif "error" in r2:
                err = r2["error"]
                print("④ turn/start 报错（预期：缺 key）:", err.get("code"), str(err.get("message"))[:220])
            else:
                print("④ turn/start 接受:", json.dumps(r2["result"], ensure_ascii=False)[:200])
    finally:
        errs = [l for l in s._errlog if "ERROR" in l or "error" in l.lower()][:4]
        if errs:
            print("--- 引擎日志（错误行）---")
            for l in errs:
                print("  ", l[:180])
        s.close()


if __name__ == "__main__":
    main()
