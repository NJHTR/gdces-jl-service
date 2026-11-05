#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假模型：OpenAI Chat Completions 兼容，专供联调。

第 1 轮 exec_command 查规范，第 2 轮 exec_command 写文书，第 3 轮出最终回复。
接真模型时把 provider 指回 DeepSeek，这里停掉即可。
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
MODEL = "stub-1"
JL_TOOL = "/home/baize/jl-service/scripts/jl-tool"


def plan(messages, tools):
    n = len([m for m in messages if m.get("role") == "tool"])
    names = {(t.get("function") or {}).get("name") or t.get("name") or "" for t in (tools or [])}

    if n == 0 and "exec_command" in names:
        return {"type": "tool", "name": "exec_command",
                "args": {"cmd": JL_TOOL + " search_standards '{\"query\":\"连墙件 间距\"}'",
                         "yield_time_ms": 30000},
                "say": "我先查一下规范。"}

    if n == 1 and "exec_command" in names:
        draft = {
            "projectId": "yun-gu-3qi",
            "kind": "监理通知单",
            "obj": "4#楼东侧外架",
            "fields": {"受检单位": "中建八局 · 外架班组", "部位": "4#楼东侧外架",
                       "事由": "连墙件缺失 2 处", "整改期限": "待补", "签发人": "待补"},
            "bases": [{"kind": "rule", "ref": "JGJ130-2011#6.4.3", "text": "连墙件设置间距"},
                      {"kind": "rec", "ref": "msg_demo", "text": "08:05 现场语音记录"}],
        }
        cmd = JL_TOOL + " create_doc_draft '" + json.dumps(draft, ensure_ascii=False) + "'"
        return {"type": "tool", "name": "exec_command",
                "args": {"cmd": cmd, "yield_time_ms": 30000},
                "say": "规范查到了，我把通知单草稿写出来。"}

    return {"type": "text",
            "text": ("记下了。4#楼东侧外架连墙件缺失 2 处，隐患已建档。\n\n"
                     "我按 JGJ 130-2011 §6.4.3 写了监理通知单草稿，已存进项目工作区并提交，"
                     "整改期限和签发人留了待补，晚上补齐就能签发。")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[stub] %s\n" % (fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "created": int(time.time()), "owned_by": "stub"}]})
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except Exception:
            return self._json(400, {"error": {"message": "bad json"}})

        messages = req.get("messages") or []
        tools = req.get("tools") or []
        stream = bool(req.get("stream"))
        step = plan(messages, tools)

        base = {"id": "chatcmpl-%d" % int(time.time() * 1000), "created": int(time.time()), "model": MODEL}
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

        if step["type"] == "tool":
            call_id = "call_%d" % int(time.time() * 1000)
            args = json.dumps(step["args"], ensure_ascii=False)
            sys.stderr.write("[stub] -> exec_command: %s\n" % step["args"]["cmd"][:90])
            call = {"index": 0, "id": call_id, "type": "function",
                    "function": {"name": step["name"], "arguments": args}}

            if not stream:
                return self._json(200, dict(base, object="chat.completion", usage=usage,
                    choices=[{"index": 0, "finish_reason": "tool_calls",
                              "message": {"role": "assistant", "content": step.get("say", ""),
                                          "tool_calls": [call]}}]))

            self._sse_start()
            self._emit(dict(base, object="chat.completion.chunk", usage=usage,
                            choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]))
            self._emit(dict(base, object="chat.completion.chunk",
                            choices=[{"index": 0, "delta": {"content": step.get("say", "")},
                                      "finish_reason": None}]))
            half = len(args) // 2
            self._emit(dict(base, object="chat.completion.chunk",
                            choices=[{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                                {"index": 0, "id": call_id, "type": "function",
                                 "function": {"name": step["name"], "arguments": args[:half]}}]}}]))
            self._emit(dict(base, object="chat.completion.chunk",
                            choices=[{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                                {"index": 0, "function": {"arguments": args[half:]}}]}}]))
            self._emit(dict(base, object="chat.completion.chunk",
                            choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
            self._sse_end()
            return

        text = step["text"]
        sys.stderr.write("[stub] -> final text (%d chars)\n" % len(text))
        if not stream:
            return self._json(200, dict(base, object="chat.completion", usage=usage,
                choices=[{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": text}}]))

        self._sse_start()
        self._emit(dict(base, object="chat.completion.chunk", usage=usage,
                        choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]))
        i = 0
        while i < len(text):
            self._emit(dict(base, object="chat.completion.chunk",
                            choices=[{"index": 0, "delta": {"content": text[i:i + 24]},
                                      "finish_reason": None}]))
            i += 24
        self._emit(dict(base, object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))
        self._sse_end()

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _emit(self, obj):
        payload = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        self.wfile.write(("%X\r\n" % len(payload)).encode() + payload + b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        done = b"data: [DONE]\n\n"
        self.wfile.write(("%X\r\n" % len(done)).encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    sys.stderr.write("[stub] 假模型已启动 :%d\n" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()