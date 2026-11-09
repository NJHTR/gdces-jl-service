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

"""HTTP 路由层：只负责解析请求、调 handler、写回响应。"""
from .core import (ADMIN_HTML, ALLOWED_TOOLS, AUTH_REQUIRED, BLOB_ROOT, BOLD_RE, ENGINE, ENGINE_CLIENT, ENGINE_ENV, ENGINE_MODE, ENV, GAP_INLINE_RE, GAP_SPLIT_RE, JL_TOOL_BIN, KIND_TO_ICON, KIND_TO_TINT, LEARN_PROMPT, LLM_MODEL, LLM_URL, MAX_CONCURRENT, NOTIFY, NUM_RE, PORT, REF_RE, ROOT, STATE_TO_ST, SYSTEM_PROMPT, TOKEN_TTL_HOURS, TURN_SLOTS, WEEK, WS_ROOT, _USER_LOCKS, _USER_LOCKS_GUARD, _bigrams, _day_label, _hash_token, _rerender_if_project, _split_num, allowed_tools, bearer, build_answer, clean_reply, db, describe_tool, exec_sql, extract_from_items, get_project, handle_login, handle_logout, issue_token, llm, load_env, ms_label, notify_worker, on_notification, parse_step, q, q1, resolve_token, rich_reply, rich_segments, run_tool, similar, split_gaps, validate_fields, list_schemas)
from .jobs import list_jobs, run_now, start as start_jobs
from .knowledge import sync_standards
from .biz import (
    handle_review_record, handle_observations, handle_rule_propose, handle_rule_proposals, handle_rule_decide, handle_review_appeal, handle_review_appeals, handle_appeal_decide, handle_memory_metrics, handle_asr, append_manifest, audit, default_project_id, guard_project, handle_admin, handle_audit, handle_blob, handle_doc_get, handle_doc_register, handle_doc_patch, handle_docs_list, handle_geo_reverse, handle_photo_list, handle_photo_patch, handle_photo_index, handle_photo_index, handle_photo_caption, handle_photo_search, parse_multipart_full, handle_project_create, handle_project_get, handle_project_list, handle_project_save, handle_standard, handle_standards_search, handle_stats, handle_upload, handle_weather, member_of, render_project_memory, store_blob, handle_doc_validate, handle_doc_schema)
from .agent import (Engine, _cites_from_output, _parse_cli_jsonl, _run_agent_loop_inner, _run_cli_turn, _set_progress, _user_lock, build_context, engine, handle_bootstrap, handle_chat, handle_memory_create, handle_memory_delete, handle_memory_list, handle_memory_patch, handle_messages, handle_memory_recall, handle_task, handle_task_cancel, handle_task_list, handle_task_retry, learn_from_turn, render_agents_md, run_agent_loop, run_turn, sweeper)


# ---------------------------------------------------------------- Handler
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

    def _send_file(self, path, name, ctype):
        """下发文件（docx 等），带 Content-Disposition 让小程序能存成文件名。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._send(404, {"code": 404, "message": "文件不存在: %r" % (e,)})
        from urllib.parse import quote as _q
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % _q(name))
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
            if path.startswith("/api/v1/exports/"):
                from urllib.parse import unquote
                name = unquote(path[len("/api/v1/exports/"):])
                # 允许 <project>/<file>，但每段都要干净（防路径穿越）
                parts = [x for x in name.split("/") if x]
                if not parts or len(parts) > 2 or any(
                        x in (".", "..") or "\\" in x for x in parts):
                    return self._send(400, {"code": 400, "message": "文件名不合法"})
                fp = os.path.join(ROOT, "data", "exports", *parts)
                if not os.path.isfile(fp):
                    import glob
                    hits = glob.glob(os.path.join(ROOT, "data", "exports", "*", parts[-1]))
                    fp = hits[0] if hits else fp
                ctype = ("application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document")
                show = os.path.basename(fp)
                try:
                    with open(fp + ".meta.json", encoding="utf-8") as mf:
                        show = json.load(mf).get("name") or show
                except (OSError, ValueError) as e:
                    sys.stderr.write("[export] 读不到显示名（用文件名）%s: %r\n" % (fp, e))
                return self._send_file(fp, show, ctype)
            if path == "/api/v1/jobs":
                return self._send(200, {"code": 0, "data": {"list": list_jobs()}})
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
            if path == "/api/v1/metrics/memory":
                return self._send(*handle_memory_metrics(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers))))
            m = re.match(r"^/api/v1/reviews/(\d+)/appeals$", path)
            if m:
                return self._send(*handle_review_appeals(
                    int(m.group(1)), resolve_token(bearer(self.headers))))
            if path == "/api/v1/rule-proposals":
                return self._send(*handle_rule_proposals(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers))))
            if path == "/api/v1/observations":
                usr = resolve_token(bearer(self.headers))
                qs2 = parse_qs(urlparse(self.path).query)
                pid = q1(qs2, "projectId") or default_project_id()
                status = q1(qs2, "status") or "open"
                return self._send(*handle_observations(pid, status, usr))
            if path == "/api/v1/tasks":
                return self._send(*handle_task_list(parse_qs(urlparse(self.path).query)))
            m = re.match(r"^/api/v1/tasks/(\d+)$", path)
            if m:
                return self._send(*handle_task(int(m.group(1)), resolve_token(bearer(self.headers))))
            if path == "/api/v1/memories/recall":
                return self._send(*handle_memory_recall(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers))))
            if path.startswith("/api/v1/memories"):
                return self._send(*handle_memory_list(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers)),
                ))
            if path.startswith("/api/v1/messages"):
                return self._send(*handle_messages(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/photos/search":
                return self._send(*handle_photo_search(
                    parse_qs(urlparse(self.path).query), resolve_token(bearer(self.headers))))
            if path == "/api/v1/photos/index":
                return self._send(*handle_photo_index(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers))))
            if path == "/api/v1/photos/index":
                return self._send(*handle_photo_index(
                    parse_qs(urlparse(self.path).query),
                    resolve_token(bearer(self.headers))))
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
            path = urlparse(self.path).path
            if AUTH_REQUIRED and not resolve_token(bearer(self.headers)):
                return self._send(401, {"code": 401, "message": "未登录或令牌失效"})
            m = re.match(r"^/api/v1/memories/(\d+)$", path)
            if m:
                return self._send(*handle_memory_delete(int(m.group(1)),
                                                        resolve_token(bearer(self.headers))))
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
            # PUT 当 PATCH 用：微信小程序的 wx.request 不支持 PATCH 方法，
            # 小程序端补字段/改状态只能走 PUT。
            m = re.match(r"^/api/v1/docs/(\d+)$", path)
            if m:
                usr = resolve_token(bearer(self.headers))
                return self._send(*handle_doc_patch(int(m.group(1)), body, usr))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})

    def do_PATCH(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if AUTH_REQUIRED and not resolve_token(bearer(self.headers)):
                return self._send(401, {"code": 401, "message": "未登录或令牌失效"})
            usr = resolve_token(bearer(self.headers))

            m = re.match(r"^/api/v1/docs/(\d+)$", path)
            if m:
                return self._send(*handle_doc_patch(int(m.group(1)), body, usr))

            m = re.match(r"^/api/v1/photos/(\d+)$", path)
            if m:
                return self._send(*handle_photo_patch(int(m.group(1)), body, usr))

            m = re.match(r"^/api/v1/memories/(\d+)$", path)
            if m:
                return self._send(*handle_memory_patch(int(m.group(1)), body, usr))
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
                ctype = self.headers.get("Content-Type") or ""
                if "multipart/form-data" in ctype:
                    data, fields = parse_multipart_full(raw, ctype)
                    # 表单字段补进 qs（query 优先），小程序把 projectId 放在 formData 里
                    for k, v in (fields or {}).items():
                        qs.setdefault(k, [v])
                else:
                    data = raw
                pid = q1(qs, "projectId")
                if not pid:
                    return self._send(400, {"code": 400, "message": "projectId 必填"})
                code, payload = handle_upload(pid, qs, data,
                                              resolve_token(bearer(self.headers)))
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
            if path == "/api/v1/jobs":
                return self._send(200, {"code": 0, "data": {"list": list_jobs()}})
            if path.startswith("/api/v1/jobs/") and path.endswith("/run"):
                name = path[len("/api/v1/jobs/"):-len("/run")]
                got = run_now(name)
                if got is None:
                    return self._send(404, {"code": 404, "message": "没有这个任务"})
                ok, payload = got
                return self._send(200 if ok else 500,
                                  {"code": 0 if ok else 500, "data": payload})
            if path == "/api/v1/photos/caption":
                return self._send(*handle_photo_caption(body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/asr":
                return self._send(*handle_asr(body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/docs/schema":
                return self._send(*handle_doc_schema(body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/docs/validate":
                return self._send(*handle_doc_validate(body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/reports/patrol":
                from .report import build_patrol_docx
                usr = resolve_token(bearer(self.headers))
                pid = (body.get("projectId") or "").strip()
                deny = guard_project(pid, usr)
                if deny:
                    return self._send(*deny)
                end = (body.get("to") or time.strftime("%Y-%m-%d")).strip()
                start = (body.get("from") or "").strip()
                if not start:
                    import datetime as _dt
                    d = _dt.date.today() - _dt.timedelta(days=6)
                    start = d.isoformat()
                try:
                    got = build_patrol_docx(pid, start, end,
                                            inspector=(body.get("inspector") or "").strip())
                except Exception as exc:
                    traceback.print_exc()
                    return self._send(500, {"code": 500, "message": "生成失败: %r" % (exc,)})
                sub = os.path.basename(os.path.dirname(got["path"]))
                return self._send(200, {"code": 0, "data": {
                    "name": got["name"],
                    "url": "/api/v1/exports/%s/%s" % (sub, got.get("url_name") or got["name"]),
                    "bytes": got["bytes"], "days": got["days"],
                    "docs": got["docs"], "photos": got["photos"],
                    "from": start, "to": end}})
            if path == "/api/v1/reports/daily":
                from .report import build_daily_docx
                usr = resolve_token(bearer(self.headers))
                pid = (body.get("projectId") or "").strip()
                kind = (body.get("kind") or "监理日志").strip()
                start = (body.get("from") or "").strip()
                end = (body.get("to") or start).strip()
                if not pid or not start:
                    return self._send(400, {"code": 400, "message": "projectId 和 from 必填"})
                deny = guard_project(pid, usr)
                if deny:
                    return self._send(*deny)
                try:
                    got = build_daily_docx(kind, pid, start, end,
                                           (body.get("inspector") or "").strip())
                except FileNotFoundError as exc:
                    return self._send(400, {"code": 400,
                                            "message": "这张表还没接模板：%s" % (exc,)})
                except Exception as exc:
                    traceback.print_exc()
                    return self._send(500, {"code": 500, "message": "生成失败: %r" % (exc,)})
                sub = os.path.basename(os.path.dirname(got["path"]))
                return self._send(200, {"code": 0, "data": {
                    "name": got["name"], "url": "/api/v1/exports/%s/%s" % (sub, got["url_name"]),
                    "bytes": got["bytes"], "days": got["days"], "kind": kind}})
            if path == "/api/v1/reports/form":
                from .report import build_form_docx
                usr = resolve_token(bearer(self.headers))
                pid = (body.get("projectId") or "").strip()
                did = body.get("docId")
                if not pid or not did:
                    return self._send(400, {"code": 400, "message": "projectId 和 docId 必填"})
                deny = guard_project(pid, usr)
                if deny:
                    return self._send(*deny)
                try:
                    got = build_form_docx(pid, int(did))
                except FileNotFoundError as exc:
                    return self._send(400, {"code": 400,
                                            "message": "这张表还没接模板：%s" % (exc,)})
                except PermissionError as exc:
                    return self._send(400, {"code": 400, "message": str(exc)})
                except Exception as exc:
                    traceback.print_exc()
                    return self._send(500, {"code": 500, "message": "生成失败: %r" % (exc,)})
                sub = os.path.basename(os.path.dirname(got["path"]))
                return self._send(200, {"code": 0, "data": {
                    "name": got["name"],
                    "url": "/api/v1/exports/%s/%s" % (sub, got["url_name"]),
                    "bytes": got["bytes"], "docId": got["docId"], "kind": got["kind"]}})
            if path == "/api/v1/rule-proposals":
                return self._send(*handle_rule_propose(
                    body, resolve_token(bearer(self.headers))))
            m = re.match(r"^/api/v1/rule-proposals/(\d+)/(accept|reject)$", path)
            if m:
                return self._send(*handle_rule_decide(
                    int(m.group(1)), {"action": m.group(2)},
                    resolve_token(bearer(self.headers))))
            m = re.match(r"^/api/v1/reviews/(\d+)/appeals$", path)
            if m:
                return self._send(*handle_review_appeal(
                    int(m.group(1)), body, resolve_token(bearer(self.headers))))
            m = re.match(r"^/api/v1/appeals/(\d+)/decide$", path)
            if m:
                return self._send(*handle_appeal_decide(
                    int(m.group(1)), body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/reviews":
                usr = resolve_token(bearer(self.headers))
                return self._send(*handle_review_record(body, usr))
            if path == "/api/v1/docs/register":
                return self._send(*handle_doc_register(body, resolve_token(bearer(self.headers))))
            if path == "/api/v1/standard":
                return self._send(*handle_standard(parse_qs(urlparse(self.path).query)))
            if path == "/api/v1/projects":
                return self._send(*handle_project_create(body))
            if path == "/api/v1/memories":
                return self._send(*handle_memory_create(body))
            return self._send(404, {"code": 404, "message": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"code": 500, "message": str(e)})


# ---------------------------------------------------------------- main
def main():
    engine()  # 预热
    threading.Thread(target=notify_worker, daemon=True).start()
    threading.Thread(target=sweeper, daemon=True).start()
    start_jobs()
    try:
        from .agent import refresh_all_agents
        refresh_all_agents()
    except Exception as _e:
        print('[agents] 启动刷新失败:', _e, flush=True)
    try:
        sync_standards()
    except Exception:
        traceback.print_exc()
    # 监听 0.0.0.0：MCP 工具容器在 jl-net（172.19.0.0/16）里，
    # 只绑 127.0.0.1 的话容器永远 fetch failed，文书就入不了库（踩过）。
    # 要收回只给本机访问，设 BIND_HOST=127.0.0.1。
    bind_host = os.environ.get("BIND_HOST") or "0.0.0.0"
    srv = ThreadingHTTPServer((bind_host, PORT), Handler)
    sys.stderr.write(f"jl-bff 已启动 :{PORT}\n")
    srv.serve_forever()


