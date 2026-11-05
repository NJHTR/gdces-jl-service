#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小验证：easyCode 的 app-server 能不能常驻复用

要回答三件事（都靠实测，不靠文档）：
  1. 能不能用 stdio JSON-RPC 起来、initialize 通
  2. thread/start + turn/start 能不能跑完一轮
  3. 同一个线程跑第二轮，是不是比第一轮快（省掉进程启动和预热）

用法：
    python3 scripts/appserver_probe.py [工作区目录]
"""
import json
import os
import subprocess
import sys
import threading
import time

ENGINE = "/home/baize/jl-service/repos/easycode/.target/release/easycode"
WS = sys.argv[1] if len(sys.argv) > 1 else "/home/baize/jl-service/data/workspaces/yun-gu-3qi"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else "只回复四个字：会话可用"


class Client:
    def __init__(self):
        env = dict(os.environ)
        env.setdefault("HOME", "/home/baize")
        self.proc = subprocess.Popen(
            [ENGINE, "app-server"],
            cwd=WS,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self.next_id = 0
        self.pending = {}
        self.notifications = []
        self.lock = threading.Lock()
        self.first_event_at = None
        self._stop = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _drain_err(self):
        for line in self.proc.stderr:
            if line.strip():
                sys.stderr.write("[engine] " + line)

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if self.first_event_at is None:
                self.first_event_at = time.time()
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    self.pending[msg["id"]] = msg
            else:
                self.notifications.append(msg)

    def call(self, method, params=None, timeout=180):
        self.next_id += 1
        rid = self.next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                if rid in self.pending:
                    return self.pending.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(method + " 超时")

    def wait_turn(self, timeout=240):
        t0 = time.time()
        seen = len(self.notifications)
        while time.time() - t0 < timeout:
            for n in self.notifications[seen:]:
                if n.get("method") == "turn/completed":
                    return True
            time.sleep(0.05)
        return False

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def main():
    print("工作区:", WS)
    c = Client()
    t_spawn = time.time()

    r = c.call("initialize", {
        "clientInfo": {"name": "jl-probe", "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    })
    print("initialize ->", "ok" if "result" in r else r.get("error"))

    r = c.call("thread/start", {
        "cwd": WS,
        "model": "qwen3.7-plus",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    })
    if "error" in r:
        print("thread/start 失败:", json.dumps(r["error"], ensure_ascii=False)[:300])
        c.close()
        sys.exit(1)
    thread_id = r["result"].get("threadId") or r["result"].get("thread", {}).get("id")
    print("thread/start ->", thread_id, "| 启动耗时 %.1f 秒" % (time.time() - t_spawn))

    for round_no in (1, 2):
        t0 = time.time()
        c.first_event_at = None
        r = c.call("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": PROMPT}],
        })
        if "error" in r:
            print("第 %d 轮 turn/start 失败: %s" % (round_no, json.dumps(r["error"], ensure_ascii=False)[:300]))
            c.close()
            sys.exit(1)
        ok = c.wait_turn()
        dt = time.time() - t0
        first = (c.first_event_at - t0) if c.first_event_at else -1
        print("第 %d 轮：完成=%s 总耗时=%.1f 秒 首个事件=%.1f 秒" % (round_no, ok, dt, first))

    msgs = [n for n in c.notifications if n.get("method") in ("item/completed", "item/started")]
    print("事件数:", len(c.notifications), "| item 事件:", len(msgs))
    for n in c.notifications:
        if n.get("method") == "item/completed":
            it = (n.get("params") or {}).get("item") or {}
            if str(it.get("type", "")).lower().find("message") >= 0:
                text = it.get("text") or ""
                if text:
                    print("  回复:", text[:120].replace("\n", " "))
    tools = []
    for n in c.notifications:
        if n.get("method") == "item/completed":
            it = (n.get("params") or {}).get("item") or {}
            if str(it.get("type", "")).lower().find("command") >= 0:
                tools.append((it.get("command") or "")[:60])
    print("  命令执行次数:", len(tools))
    for t in tools[:3]:
        print("   -", t)

    tails = [n for n in c.notifications if n.get("method") == "turn/completed"]
    print("turn/completed 次数:", len(tails))
    c.close()


if __name__ == "__main__":
    main()
