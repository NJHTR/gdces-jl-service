# -*- coding: utf-8 -*-
"""最终态执行 trace：事件账本（task_event）+ 状态机 + 大输出 + 旧字段投影。

设计口径（对应 task-16/17，字段是前后端共享契约，别随手改）：
  · run = task（一轮 turn），不另建 run 表。
  · task_event 是 append-only 账本；BFF 是唯一 sequencer。
  · seq 由 `UPDATE task SET event_seq=LAST_INSERT_ID(event_seq+1)` 原子取号，
    同一 task 内单调、无空洞；events 按 seq 升序读。
  · step 的 running→终态只允许一次 CAS 更新；终态不可回写；纠正/重试追加新事件。
  · 事件行只存 ≤4KB preview；完整 output 落 data/trace/<task>/<seq>.json.gz，
    详情端点按需读；落库/落盘前做脱敏。
  · 旧客户端字段（message.trace / task.progress）从事件投影生成；无事件回退旧值。
"""
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time

from .core import ROOT, db, exec_rowcount, exec_sql, q

TRACE_VERSION = 2
PREVIEW_LIMIT = 4096
TRACE_ROOT = os.path.join(ROOT, "data", "trace")
TRACE_TTL_DAYS = int(os.environ.get("TRACE_TTL_DAYS", "30"))

EVENT_KINDS = ("plan", "tool", "file_read", "file_write", "image_read", "subagent",
               "artifact", "validation", "note", "error", "status")
PHASES = ("understand", "plan", "gather", "act", "finish")
STEP_STATUSES = ("pending", "running", "succeeded", "failed", "retrying", "skipped",
                 "cancelled")
TERMINAL_STEP = ("succeeded", "failed", "skipped", "cancelled")
RUN_STATUSES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
TERMINAL_RUN = ("succeeded", "partial", "failed", "cancelled")
VISIBILITIES = ("project", "private", "admin")

# ---------------------------------------------------------------- DDL / ensure
TASK_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS task_event (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id        BIGINT UNSIGNED NOT NULL,
  seq            BIGINT UNSIGNED NOT NULL COMMENT 'per-task 单调序号（BFF sequencer 发号）',
  parent_seq     BIGINT UNSIGNED NULL COMMENT '父 step 的 seq（子智能体/嵌套步骤）',
  step_id        VARCHAR(40) NULL COMMENT '工具 command id / 内部 step id，用于同一步的起止配对',
  kind           ENUM('plan','tool','file_read','file_write','image_read','subagent',
                      'artifact','validation','note','error','status') NOT NULL,
  phase          VARCHAR(16) NULL COMMENT 'understand/plan/gather/act/finish',
  name           VARCHAR(128) NOT NULL COMMENT '中文动作名（前端展示）',
  status         ENUM('pending','running','succeeded','failed','retrying','skipped',
                      'cancelled') NOT NULL DEFAULT 'pending',
  summary        VARCHAR(512) NULL,
  input_preview  TEXT NULL COMMENT '参数摘要，≤4KB，已脱敏',
  output_preview TEXT NULL COMMENT '输出前 4KB，已脱敏',
  output_ref     VARCHAR(255) NULL COMMENT 'data/trace/<task>/<seq>.json.gz',
  output_bytes   BIGINT NULL,
  truncated      TINYINT NOT NULL DEFAULT 0,
  error_detail   TEXT NULL,
  visibility     ENUM('project','private','admin') NOT NULL DEFAULT 'project',
  attempt        INT NOT NULL DEFAULT 1,
  retry_of_seq   BIGINT UNSIGNED NULL,
  started_at     DATETIME(3) NULL,
  ended_at       DATETIME(3) NULL,
  meta           JSON NULL COMMENT '内部扩展：旧 trace 投影需要的 tool/icon/args 等',
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_seq (task_id, seq),
  KEY idx_task_status (task_id, status),
  KEY idx_task_kind (task_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='执行 trace 事件账本（append-only）'
"""

_ENSURE_LOCK = threading.Lock()
_ENSURED = False


def _column_exists(table, column):
    rows = q("SELECT COUNT(*) AS n FROM information_schema.columns "
             "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
             (table, column))
    return int(rows[0]["n"] or 0) > 0


def ensure_trace_schema(force=False):
    """幂等建表 + task 补列。服务启动调一次；写事件前也会兜底。"""
    global _ENSURED
    if _ENSURED and not force:
        return True
    with _ENSURE_LOCK:
        if _ENSURED and not force:
            return True
        try:
            q("SELECT 1")
            exec_sql(TASK_EVENT_DDL)
            if not _column_exists("task", "event_seq"):
                exec_sql("ALTER TABLE task ADD COLUMN event_seq BIGINT UNSIGNED NOT NULL "
                         "DEFAULT 0 COMMENT 'per-task 事件序号（BFF sequencer）'")
            if not _column_exists("task", "trace_version"):
                exec_sql("ALTER TABLE task ADD COLUMN trace_version TINYINT NOT NULL "
                         "DEFAULT 2 COMMENT '1=旧扁平 trace / 2=task_event'")
            _ENSURED = True
            return True
        except Exception as e:
            sys.stderr.write("[trace] ensure schema 失败: %r\n" % (e,))
            return False


# ---------------------------------------------------------------- 脱敏 / 大输出
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|mysql_password|"
    r"authorization)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LOCAL_PATH_RE = re.compile(
    r"(?:file://[^\s，。；：）)、！？】\"'`]+"
    r"|/(?:tmp|home|var|etc|opt|usr|root|mnt|media)/[^\s，。；：）)、！？】\"'`]*"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)[^\s，。；：）)、！？】\"'`]*)")


def redact(text):
    """落库/落盘前脱敏：token/密码/手机号/本地绝对路径/长 hex。"""
    if not text:
        return ""
    t = str(text)
    t = _SECRET_RE.sub(lambda m: "%s=[已脱敏]" % m.group(1), t)
    t = _BEARER_RE.sub("Bearer [已脱敏]", t)
    t = _PHONE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], t)
    t = _LOCAL_PATH_RE.sub("[本地路径]", t)
    t = _LONG_HEX_RE.sub("[hash]", t)
    return t


def preview(text, limit=PREVIEW_LIMIT):
    """预览：脱敏 + 截断到 limit。返回 (preview, truncated, full_bytes)。"""
    t = redact(text or "")
    raw = len((text or "").encode("utf-8"))
    if len(t) <= limit:
        return t, False, raw
    return t[:limit] + "\n…（已截断，完整内容见 detail）", True, raw


def _safe_task_dir(task_id):
    tid = str(int(task_id))
    d = os.path.join(TRACE_ROOT, tid)
    real = os.path.realpath(d)
    if not real.startswith(os.path.realpath(TRACE_ROOT) + os.sep):
        raise ValueError("trace 路径越界")
    return real


def store_output(task_id, seq, text):
    """完整输出落 data/trace/<task>/<seq>.json.gz；返回 (rel_ref, bytes)。"""
    d = _safe_task_dir(task_id)
    os.makedirs(d, exist_ok=True)
    payload = {"taskId": int(task_id), "seq": int(seq),
               "text": redact(text or ""),
               "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S")}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = os.path.join(d, "%s.json.gz" % int(seq))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
            gz.write(raw)
    os.replace(tmp, path)
    return os.path.relpath(path, ROOT), len((text or "").encode("utf-8"))


def load_output(task_id, seq):
    """读完整输出；不存在返回 None。"""
    try:
        d = _safe_task_dir(task_id)
    except ValueError:
        return None
    path = os.path.join(d, "%s.json.gz" % int(seq))
    if not os.path.isfile(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("[trace] 读 output 失败 %s/%s: %r\n" % (task_id, seq, e))
        return None


# ---------------------------------------------------------------- sequencer
def _task_row(task_id):
    rows = q("SELECT id, project_id, user_id, status, started_at, finished_at, "
             "event_seq, trace_version, error FROM task WHERE id=%s", (task_id,))
    return rows[0] if rows else None


def next_seq(task_id):
    """原子取号：同一连接里 UPDATE event_seq=LAST_INSERT_ID(event_seq+1) 再取 LAST_INSERT_ID。

    不能靠 exec_sql 的 lastrowid：pymysql 对 UPDATE 的 lastrowid 不保证是
    LAST_INSERT_ID(expr) 的值（实测依赖 OK 包），必须在同一连接里 SELECT 出来。
    """
    ensure_trace_schema()
    task_id = int(task_id)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE task SET event_seq=LAST_INSERT_ID(event_seq+1) WHERE id=%s",
                    (task_id,))
        if cur.rowcount != 1:
            raise ValueError("task 不存在或取号失败: %s" % task_id)
        cur.execute("SELECT LAST_INSERT_ID() AS s")
        row = cur.fetchone() or {}
        seq = int(row.get("s") or 0)
    if seq <= 0:
        raise ValueError("seq 取号失败: %s" % task_id)
    return seq


def _normalize_kind(kind):
    kind = (kind or "note").strip()
    if kind not in EVENT_KINDS:
        raise ValueError("非法 kind: %s" % kind)
    return kind


def append_event(task_id, kind, name, *, phase=None, status="pending", summary=None,
                 input_text=None, output_text=None, error=None, parent_seq=None,
                 step_id=None, visibility="project", attempt=1, retry_of_seq=None,
                 meta=None, started=False, finished=False, output_ref=None):
    """追加一条事件并返回 seq。

    - started=True 时 status 置 running、写 started_at；finished=True 时补 ended_at。
    - output_text 非空：预览进事件行，完整内容落 gz（output_ref/output_bytes/truncated）。
    - 单条事件一旦是终态，本函数不会再改它（纠正用新事件/retry_of_seq）。
    """
    ensure_trace_schema()
    task_id = int(task_id)
    kind = _normalize_kind(kind)
    status = (status or "pending").strip()
    if status not in STEP_STATUSES:
        raise ValueError("非法 status: %s" % status)
    visibility = visibility if visibility in VISIBILITIES else "project"
    phase = phase if phase in PHASES else None
    if started and status in ("pending", "running"):
        status = "running"
    preview_txt, truncated, out_bytes = "", False, None
    if output_text is not None:
        preview_txt, truncated, out_bytes = preview(output_text)
    inp = ""
    if input_text is not None:
        inp = preview(input_text)[0]
    seq = next_seq(task_id)
    if output_text is not None and output_ref is None:
        rel, out_bytes = store_output(task_id, seq, output_text)
        output_ref = rel
    elif output_text is None and output_ref is None:
        out_bytes = None
    err = (redact(error)[:4000] if error else None)
    sql = (
        "INSERT INTO task_event (task_id, seq, parent_seq, step_id, kind, phase, name, "
        "status, summary, input_preview, output_preview, output_ref, output_bytes, "
        "truncated, error_detail, visibility, attempt, retry_of_seq, started_at, ended_at, "
        "meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        + ("NOW(3)" if (started or status in TERMINAL_STEP or finished) else "NULL") + ","
        + ("NOW(3)" if (finished or status in TERMINAL_STEP) else "NULL") + ",%s)")
    exec_sql(sql, (
        task_id, seq, int(parent_seq) if parent_seq else None,
        (str(step_id)[:40] if step_id else None), kind, phase,
        redact(str(name or ""))[:128] or kind, status,
        (redact(str(summary))[:512] if summary else None),
        inp or None, (preview_txt or None), output_ref, out_bytes,
        1 if truncated else 0, err, visibility, int(attempt or 1),
        int(retry_of_seq) if retry_of_seq else None,
        json.dumps(meta or {}, ensure_ascii=False) if meta else None))
    return seq


def finish_event(task_id, seq, status="succeeded", *, summary=None, output_text=None,
                 error=None, output_ref=None, meta_extra=None):
    """步骤完成：**追加**一条新事件（同 stepId、parent_seq=起始事件 seq），不原地改已有行。

    客户端按 stepId upsert：后到的终态事件覆盖先前的 running 事件。
    返回完成事件的 seq；若起始事件已是终态/不存在返回 None；重复完成幂等返回已有 seq。
    """
    ensure_trace_schema()
    task_id, seq = int(task_id), int(seq)
    status = (status or "").strip()
    if status not in TERMINAL_STEP:
        raise ValueError("终态只能是 %r" % (TERMINAL_STEP,))
    rows = q("SELECT * FROM task_event WHERE task_id=%s AND seq=%s", (task_id, seq))
    if not rows:
        return None
    start = rows[0]
    if start.get("status") in TERMINAL_STEP:
        return None
    done = q("SELECT seq FROM task_event WHERE task_id=%s AND parent_seq=%s "
             "AND status IN ('succeeded','failed','skipped','cancelled') "
             "ORDER BY seq LIMIT 1", (task_id, seq))
    if done:
        return int(done[0]["seq"])
    meta = {}
    raw_meta = start.get("meta")
    if isinstance(raw_meta, str):
        try:
            meta = json.loads(raw_meta or "{}")
        except ValueError:
            meta = {}
    elif isinstance(raw_meta, dict):
        meta = dict(raw_meta)
    if meta_extra:
        meta.update(meta_extra)
    meta["startSeq"] = seq
    return append_event(
        task_id, start.get("kind"), start.get("name"),
        phase=start.get("phase"), status=status,
        summary=(summary if summary is not None else start.get("summary")),
        output_text=output_text, error=error, parent_seq=seq,
        step_id=start.get("step_id"), visibility=start.get("visibility") or "project",
        attempt=int(start.get("attempt") or 1),
        retry_of_seq=(start.get("retry_of_seq") or None),
        meta=meta, started=True, finished=True, output_ref=output_ref)


def finish_event_by_step(task_id, step_id, status="succeeded", **kw):
    """按 step_id 找未完成的起始事件并追加终态事件（工具 command id 配对用）。"""
    rows = q("SELECT seq FROM task_event WHERE task_id=%s AND step_id=%s "
             "AND status IN ('running','retrying','pending') ORDER BY seq DESC LIMIT 1",
             (int(task_id), str(step_id)))
    if not rows:
        return None
    return finish_event(task_id, rows[0]["seq"], status=status, **kw)


# ---------------------------------------------------------------- run 状态机
def start_run(task_id):
    """queued→running（CAS）；返回 True 表示确实是本进程开跑，False=已被取消/终态。

    已在 running（重复调用/恢复）返回 True 且不重复追加"开始执行"事件。
    """
    ensure_trace_schema()
    task_id = int(task_id)
    rows = q("SELECT status FROM task WHERE id=%s", (task_id,))
    if not rows or rows[0].get("status") not in ("queued", "running"):
        return False
    if rows[0].get("status") == "running":
        return True
    n = exec_rowcount(
        "UPDATE task SET status='running', started_at=COALESCE(started_at, NOW(3)), "
        "trace_version=%s WHERE id=%s AND status='queued'",
        (TRACE_VERSION, task_id))
    if n == 1:
        append_event(task_id, "status", "开始执行", phase="understand",
                     status="succeeded", summary="任务开始", started=True, finished=True)
    return n == 1


# DB 层 task.status 是旧 ENUM('queued','running','done','failed','cancelled')：
# 旧客户端只认 done，所以新契约的 succeeded/partial 在投影层映射，不改旧字段值。
_DB_RUN_STATUS = {"succeeded": "done", "partial": "done", "failed": "failed",
                  "cancelled": "cancelled"}
_VIEW_RUN_STATUS = {"done": "succeeded"}


def _view_run_status(db_status):
    return _VIEW_RUN_STATUS.get(db_status, db_status)


def finish_run(task_id, status, *, summary=None, error=None):
    """run 终态：只允许从 queued/running 进入；已被取消则不改写。"""
    ensure_trace_schema()
    status = status if status in RUN_STATUSES else "failed"
    if status not in TERMINAL_RUN:
        raise ValueError("run 终态只能是 %r" % (TERMINAL_RUN,))
    db_status = _DB_RUN_STATUS.get(status, "failed")
    n = exec_rowcount(
        "UPDATE task SET status=%s, finished_at=NOW(3), "
        "error=COALESCE(%s, error), trace_version=%s "
        "WHERE id=%s AND status IN ('queued','running')",
        (db_status, (str(error)[:900] if error else None), TRACE_VERSION, int(task_id)))
    if n == 1:
        step_status = ("cancelled" if status == "cancelled" else
                       ("failed" if status == "failed" else "succeeded"))
        append_event(task_id, "status", run_status_label(status), phase="finish",
                     status=step_status, summary=summary or run_status_label(status),
                     error=error, finished=True)
    return n == 1


def cancel_run(task_id, *, reason=None):
    """取消：CAS queued/running→cancelled；返回是否真的取消成功。"""
    ensure_trace_schema()
    n = exec_rowcount(
        "UPDATE task SET status='cancelled', finished_at=NOW(3), "
        "error=COALESCE(%s, error) WHERE id=%s AND status IN ('queued','running')",
        ((str(reason)[:900] if reason else None), int(task_id)))
    if n == 1:
        append_event(task_id, "status", "已取消", phase="finish", status="cancelled",
                     summary=reason or "用户取消", finished=True)
    return n == 1


def run_status_label(status):
    return {"queued": "排队中", "running": "执行中", "succeeded": "已完成",
            "partial": "部分完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)


def finished_run_state(status):
    return status in TERMINAL_RUN


# ---------------------------------------------------------------- 读取 / 契约
def _event_view(row):
    meta = row.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    return {
        "seq": int(row["seq"]),
        "stepId": row.get("step_id"),
        "parentSeq": int(row["parent_seq"]) if row.get("parent_seq") else None,
        "kind": row.get("kind"),
        "phase": row.get("phase"),
        "status": row.get("status"),
        "name": row.get("name"),
        "summary": row.get("summary"),
        "inputPreview": row.get("input_preview"),
        "outputPreview": row.get("output_preview"),
        "outputRef": row.get("output_ref"),
        "outputBytes": int(row["output_bytes"]) if row.get("output_bytes") is not None else None,
        "truncated": bool(row.get("truncated")),
        "error": row.get("error_detail"),
        "attempt": int(row.get("attempt") or 1),
        "retryOfSeq": int(row["retry_of_seq"]) if row.get("retry_of_seq") else None,
        "startedAt": str(row["started_at"]) if row.get("started_at") else None,
        "endedAt": str(row["ended_at"]) if row.get("ended_at") else None,
        "visibility": row.get("visibility") or "project",
        # 只读产物引用（图片现签短链；不复制证据）
        "artifacts": _artifacts_of(meta),
        # 内部字段不对外（meta 里的 tool/icon 给旧投影用）
    }


def events_after(task_id, after_seq=0, limit=500):
    ensure_trace_schema()
    rows = q("SELECT * FROM task_event WHERE task_id=%s AND seq>%s ORDER BY seq ASC LIMIT %s",
             (int(task_id), int(after_seq or 0), int(limit)))
    return [_event_view(r) for r in rows]


def last_seq(task_id):
    rows = q("SELECT COALESCE(MAX(seq),0) AS s FROM task_event WHERE task_id=%s",
             (int(task_id),))
    return int(rows[0]["s"] or 0)


# ---------------------------------------------------------------- 产物引用（只读）
def _artifacts_of(meta):
    """事件 meta.artifacts → 契约 artifacts[]；图片 asset 现签短链，不复制证据。"""
    arts = (meta or {}).get("artifacts") if isinstance(meta, dict) else None
    out = []
    if not isinstance(arts, list):
        return out
    for a in arts[:8]:
        if not isinstance(a, dict):
            continue
        item = {"type": str(a.get("type") or "asset")[:16]}
        for k in ("assetId", "docId", "photoId", "name", "caption", "mime"):
            if a.get(k) not in (None, ""):
                item[k] = str(a[k])[:255]
        if item.get("assetId"):
            try:
                from . import assetsvc
                row = assetsvc._row(item["assetId"])
                if row:
                    item["url"] = assetsvc.sign_asset_url(item["assetId"])
                    item["mime"] = row.get("mime") or item.get("mime")
                    item["bytes"] = int(row.get("size") or 0)
            except Exception as e:
                sys.stderr.write("[trace] 签 artifact 失败 %s: %r\n" % (item.get("assetId"), e))
        out.append(item)
    return out


def _events_collapsed(task_id, kinds=None, limit=None):
    """按 step 折叠事件：同一 stepId（无则按 seq）以最大 seq 为准。

    返回 [(seq, kind, name, status, started_at, ended_at, meta, output_preview,
            error_detail, parent_seq)]，按首个事件出现顺序。
    """
    sql = "SELECT * FROM task_event WHERE task_id=%s"
    args = [int(task_id)]
    if kinds:
        sql += " AND kind IN (" + ",".join(["%s"] * len(kinds)) + ")"
        args.extend(kinds)
    sql += " ORDER BY seq ASC"
    rows = q(sql, tuple(args))
    latest, order = {}, []
    for r in rows:
        key = r.get("step_id") or ("seq:%s" % r["seq"])
        if key not in latest:
            order.append(key)
        latest[key] = r
    out = [latest[k] for k in order]
    if limit:
        out = out[-int(limit):]
    return out


def _step_items(task_id):
    """折叠后的「可见工作项」：同 stepId 合并起止；subagent 按 parentSeq 合并派发/回报。

    引擎会收到“派子 agent”(seq20) 与“子 agent 回报”(seq21) 两条；L2 把这对显示成一行，
    L1 步数必须与之一致（否则 stepCount=22 / totalSteps=21）。
    """
    rows = _events_collapsed(task_id)
    out, sub_idx = [], {}
    for r in rows:
        if r.get("kind") == "status":
            continue
        if r.get("kind") == "subagent":
            key = ("subagent", r.get("parent_seq") or ("seq:%s" % r["seq"]))
            if key in sub_idx:
                out[sub_idx[key]] = r          # 保留最新一条（rows 已按 seq 升序）
            else:
                sub_idx[key] = len(out)
                out.append(r)
        else:
            out.append(r)
    return out


def step_count(task_id):
    """已完成工作项数：按 stepId 折叠；subagent 派发/回报按 parentSeq 合并计 1。

    L1 步数必须 == L2 可见步骤（client 把 subagent 对归并成一行），所以这里不能
    把派发和回报各算一步。
    """
    n = 0
    for r in _step_items(task_id):
        if r.get("status") in ("succeeded", "failed", "cancelled", "skipped", "retrying"):
            n += 1
    return n


def run_view(task_id, task_row=None):
    t = task_row or _task_row(task_id)
    if not t:
        return None
    summary = ""
    try:
        rows = q("SELECT name, summary FROM task_event WHERE task_id=%s "
                 "ORDER BY seq DESC LIMIT 1", (int(task_id),))
        summary = (rows[0].get("summary") or rows[0].get("name") or "") if rows else ""
    except Exception:
        summary = ""
    return {
        "taskId": int(t["id"]),
        "status": _view_run_status(t.get("status")),
        "startedAt": str(t["started_at"]) if t.get("started_at") else None,
        "endedAt": str(t["finished_at"]) if t.get("finished_at") else None,
        "summary": (summary or t.get("error") or "")[:512],
        "stepCount": step_count(task_id),
    }


def event_detail(task_id, seq):
    """完整 output（详情端点用）。不存在事件返回 None。"""
    rows = q("SELECT * FROM task_event WHERE task_id=%s AND seq=%s",
             (int(task_id), int(seq)))
    if not rows:
        return None
    row = rows[0]
    meta = row.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    elif not isinstance(meta, dict):
        meta = {}
    out = {"seq": int(seq), "kind": row.get("kind"), "name": row.get("name"),
           "status": row.get("status"), "error": row.get("error_detail"),
           "truncated": bool(row.get("truncated")),
           "outputBytes": int(row["output_bytes"]) if row.get("output_bytes") is not None
           else None,
           "output": None, "source": "none",
           # L3 产物/图片：只读引用，短签由后端现签
           "artifacts": _artifacts_of(meta)}
    data = load_output(task_id, seq) if row.get("output_ref") else None
    if data and data.get("text") is not None:
        out["output"] = data["text"]
        out["source"] = "file"
    elif row.get("output_preview"):
        out["output"] = row["output_preview"]
        out["source"] = "preview"
    elif row.get("error_detail"):
        out["output"] = row["error_detail"]
        out["source"] = "error"
    return out


# ---------------------------------------------------------------- 旧字段投影
def project_progress(task_id, limit=12):
    """把事件投影成旧 task.progress: [{label,tool,state,secs}]（按 stepId 折叠）。"""
    try:
        rows = [r for r in _step_items(task_id)
                if r.get("kind") in ("tool", "file_read", "file_write", "image_read",
                                     "artifact", "validation", "subagent", "plan")]
    except Exception:
        return None
    if not rows:
        return None
    steps = []
    for r in rows[-limit:]:
        meta = r.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except ValueError:
                meta = {}
        meta = meta or {}
        secs = None
        if r.get("started_at") and r.get("ended_at"):
            try:
                secs = round((r["ended_at"] - r["started_at"]).total_seconds(), 1)
            except Exception:
                secs = None
        state = ("running" if r.get("status") in ("running", "retrying", "pending")
                 else "done")
        steps.append({"label": meta.get("label") or r.get("name"),
                      "tool": meta.get("tool") or r.get("kind"),
                      "state": state, "secs": secs or 0})
    return steps


def project_message_trace(task_id, fallback=None, limit=120):
    """把事件投影成旧 message.trace（按 stepId 折叠，一条工具一项）；无事件回退 fallback。"""
    try:
        rows = _events_collapsed(
            task_id, kinds=("tool", "file_read", "file_write", "image_read",
                            "artifact", "validation", "subagent", "note"))
    except Exception:
        return fallback
    if not rows:
        return fallback
    out = []
    for r in rows[-int(limit):]:
        meta = r.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except ValueError:
                meta = {}
        meta = meta or {}
        tool = meta.get("tool") or "tool"
        item = {
            "kind": meta.get("legacy_kind") or "cmd",
            "tool": tool,
            "icon": meta.get("icon") or "run",
            "text": meta.get("label") or r.get("name"),
            "args": meta.get("args") or "",
            "ok": r.get("status") in ("succeeded", "retrying"),
        }
        if r.get("output_preview"):
            item["output"] = r["output_preview"]
        if r.get("error_detail"):
            item["output"] = (item.get("output") + "\n" if item.get("output") else "") + \
                str(r["error_detail"])[:400]
        if meta.get("secs") is not None:
            item["secs"] = meta["secs"]
        out.append(item)
    return out or fallback


# ---------------------------------------------------------------- GC
def gc(ttl_days=None):
    """清理超期 trace：先删事件行，再清理空的 task 目录（含孤儿文件）。"""
    ensure_trace_schema()
    ttl = int(ttl_days or TRACE_TTL_DAYS)
    n = 0
    try:
        rows = q("SELECT task_id, COUNT(*) AS n FROM task_event "
                 "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY task_id",
                 (ttl,))
    except Exception as e:
        sys.stderr.write("[trace] GC 查询失败: %r\n" % (e,))
        return 0
    for r in rows:
        tid = int(r["task_id"])
        try:
            n += exec_rowcount("DELETE FROM task_event WHERE task_id=%s "
                               "AND created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
                               (tid, ttl))
            d = _safe_task_dir(tid)
            if os.path.isdir(d):
                keep = {str(x["seq"]) for x in q(
                    "SELECT seq FROM task_event WHERE task_id=%s", (tid,))}
                for fn in os.listdir(d):
                    if fn.endswith(".json.gz") and fn[:-8] not in keep:
                        try:
                            os.remove(os.path.join(d, fn))
                        except OSError as e:
                            sys.stderr.write("[trace] GC 删文件失败 %s: %r\n" % (fn, e))
                try:
                    if not os.listdir(d):
                        os.rmdir(d)
                except OSError as e:
                    sys.stderr.write("[trace] GC 删目录失败 %s: %r\n" % (d, e))
        except Exception as e:
            sys.stderr.write("[trace] GC %s 失败: %r\n" % (tid, e))
    if n:
        sys.stderr.write("[trace] GC 清理 %d 条事件\n" % n)
    # 已删除任务留下的孤儿目录/文件也清掉（任务删除时 purge_task 是首选，这里兜底）
    try:
        if os.path.isdir(TRACE_ROOT):
            alive = {str(r["id"]) for r in q("SELECT id FROM task")}
            for fn in os.listdir(TRACE_ROOT):
                d = os.path.join(TRACE_ROOT, fn)
                if not os.path.isdir(d) or fn in alive or not fn.isdigit():
                    continue
                for sub in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, sub))
                    except OSError as e:
                        sys.stderr.write("[trace] GC 删孤儿文件失败 %s: %r\n" % (sub, e))
                try:
                    os.rmdir(d)
                except OSError as e:
                    sys.stderr.write("[trace] GC 删孤儿目录失败 %s: %r\n" % (d, e))
    except Exception as e:
        sys.stderr.write("[trace] GC 孤儿目录扫描失败: %r\n" % (e,))
    return n


def purge_task(task_id):
    """任务删除时清理它的 trace 文件和事件（软保留由调用方决定）。"""
    try:
        d = _safe_task_dir(task_id)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                try:
                    os.remove(os.path.join(d, fn))
                except OSError as e:
                    sys.stderr.write("[trace] 删 trace 文件失败 %s: %r\n" % (fn, e))
            try:
                os.rmdir(d)
            except OSError as e:
                sys.stderr.write("[trace] 删 trace 目录失败 %s: %r\n" % (d, e))
    except Exception as e:
        sys.stderr.write("[trace] purge 文件失败 %s: %r\n" % (task_id, e))
    try:
        exec_sql("DELETE FROM task_event WHERE task_id=%s", (int(task_id),))
    except Exception as e:
        sys.stderr.write("[trace] purge 事件失败 %s: %r\n" % (task_id, e))
