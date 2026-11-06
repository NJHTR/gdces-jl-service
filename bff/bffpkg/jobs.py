# -*- coding: utf-8 -*-
"""服务侧定时任务（这些 loop 属于「服务」，不属于开发环境）

为什么单独一个模块：
  以前只有两种 loop —— crontab 里的运维脚本（healthcheck/backup）和
  agent.py 里的 sweeper 线程。要加一个「业务级」的定时任务（比如每天统计待补文书），
  只能改代码 + 加线程，没有登记处、没有历史、没法手动触发。

这个模块提供：
  1. 一张表 service_job —— 登记、开关、上次结果、下次时间，看得见
  2. 一个 5 字段 cron 解析（分钟级）—— 够用且是标准写法，不用记自定义格式
  3. 一个 worker 线程 —— 到点的任务领走执行，单写者（乐观锁认领）
  4. 一个 handler 注册表 —— 加任务 = 加一个函数 + 一行注册
  5. 管理接口 —— 列任务、立刻跑一次（在 api.py 里接）

边界（重要）：
  - 这里只放「服务自己要的」loop：业务数据、通知、整理、统计
  - 开发用的 loop（开工检查、收工归档）挂在开发环境，不在这里
"""
import contextlib
import io
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

from .core import ROOT, exec_sql, q

# ---------------------------------------------------------------- cron 解析


def _parse_field(field, lo, hi):
    """解析一个 cron 字段，返回允许的取值集合

    支持：*  */n  a  a-b  a-b/n  以及逗号列表
    """
    out = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("步长必须 > 0: " + field)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start < lo or end > hi or start > end:
            raise ValueError("越界: " + field)
        out.update(range(start, end + 1, step))
    return out


class Cron:
    """5 字段 cron：分 时 日 月 周"""

    def __init__(self, expr):
        parts = str(expr).split()
        if len(parts) != 5:
            raise ValueError("cron 要 5 段（分 时 日 月 周）: " + str(expr))
        self.minutes = _parse_field(parts[0], 0, 59)
        self.hours = _parse_field(parts[1], 0, 23)
        self.days = _parse_field(parts[2], 1, 31)
        self.months = _parse_field(parts[3], 1, 12)
        self.weekdays = _parse_field(parts[4], 0, 6)

    def next_after(self, when):
        """下一次触发时间（分钟精度），最多往后找 366 天"""
        t = (when + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = when + timedelta(days=366)
        while t < limit:
            if (
                t.minute in self.minutes
                and t.hour in self.hours
                and t.day in self.days
                and t.month in self.months
                and (t.weekday() + 1) % 7 in self.weekdays
            ):
                return t
            t += timedelta(minutes=1)
        raise ValueError("一年内找不到下次触发时间: %s" % when)


# ---------------------------------------------------------------- handler 注册
_HANDLERS = {}


def handler(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn

    return deco


def run_handler(name, payload):
    fn = _HANDLERS.get(name)
    if not fn:
        raise RuntimeError("没注册的 handler: " + name)
    return fn(payload or {})


# ---------------------------------------------------------------- 内置任务

@handler("pending_docs_digest")
def pending_docs_digest(payload):
    """每天统计「待补」文书，写审计。

    为什么要它：项目里缺字段的文书不会自己冒出来，人不会天天去翻列表。
    这个 loop 只做统计和留痕，不替人做决定（不发通知、不改状态）。
    """
    rows = q("SELECT project_id, COUNT(*) AS n FROM doc WHERE state='待补' GROUP BY project_id")
    total = sum(int(r["n"] or 0) for r in rows)
    for r in rows:
        exec_sql(
            "INSERT INTO audit_log (project_id, actor_id, action, target_type, target_id, after) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (r["project_id"], 0, "job.pending_docs", "doc", "",
             json.dumps({"pending": int(r["n"] or 0)}, ensure_ascii=False)),
        )
    sys.stderr.write("[job] 待补文书统计：%d 份 / %d 个项目\n" % (total, len(rows)))
    return {"projects": len(rows), "pending": total}


@handler("cleanup_temp")
def cleanup_temp(payload):
    """每天清一次临时产物。

    为什么要有它：出稿结果、复核目录、语音 wav 都按任务隔离了（防串单），
    代价是会随使用量增长 —— 没人清就会一直堆。
    只删「超过 1 天」的，正在跑的不会被误删。
    """
    import glob
    import os as _os
    import shutil
    import time as _time

    cutoff = _time.time() - 24 * 3600
    removed = {"dirs": 0, "files": 0}

    for path in glob.glob("/tmp/jl-review-*"):
        with contextlib.suppress(OSError):
            if _os.path.isdir(path) and _os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed["dirs"] += 1

    for pattern in ("/tmp/jl-asr-*.wav",
                    "/tmp/jl-appserver-*.log",          # 每用户一个，会随用户数累积
                    "/home/baize/jl-service/data/workspaces/*/.agent/last-*.md"):
        for path in glob.glob(pattern):
            with contextlib.suppress(OSError):
                if _os.path.isfile(path) and _os.path.getmtime(path) < cutoff:
                    _os.remove(path)
                    removed["files"] += 1

    # 主日志加体积上限：超过 50MB 就留一份旧的，别让它无限长
    log = "/home/baize/jl-service/runtime/bff.log"
    try:
        if _os.path.isfile(log) and _os.path.getsize(log) > 50 * 1024 * 1024:
            _os.replace(log, log + ".1")
            removed["rotated"] = 1
    except OSError as exc:
        print("[job] 日志轮转失败: %r" % (exc,), flush=True)

    sys.stderr.write("[job] 清理临时产物：目录 %d 个、文件 %d 个、轮转 %d\n"
                     % (removed["dirs"], removed["files"], removed.get("rotated", 0)))
    return removed


@handler("export_authoritative")
def export_authoritative(payload):
    """把账本里够权威的条目导出成知识文件（幂等：内容没变就不重写）。

    判定「够权威」只看账本，不看我们的主观：verified_count 与 used_count 达标、
    且 disputed=0 —— 也就是「被反复引用、且经人确认、摘录前后一致」。
    """
    import hashlib
    import os as _os

    dest = _os.path.join(ROOT, "data", "knowledge", "kb", "authoritative")
    _os.makedirs(dest, exist_ok=True)

    # disputed 的一律不导出：摘录前后不一致的东西，不能当权威卖出去
    rows = q("SELECT ref, quote, used_count, verified_count, disputed, status, "
             "created_at, last_used_at FROM citation "
             "WHERE status='authoritative' AND disputed=0 "
             "ORDER BY verified_count DESC, used_count DESC")
    written, skipped, errors = [], 0, []

    for r in rows:
        ref = r.get("ref") or ""
        if not ref.startswith("standard://"):
            continue
        code, _, clause = ref[len("standard://"):].partition("#")
        code, clause = code.strip(), clause.strip()
        try:
            srows = q("SELECT title, std_title, body, review_note FROM standard WHERE "
                      "REPLACE(code,' ','')=REPLACE(%s,' ','') AND clause=%s",
                      (code, clause))
        except Exception as exc:
            errors.append("%s: %r" % (ref, exc))
            continue
        if not srows:
            errors.append("%s: 库里查不到条文" % ref)
            continue
        std = srows[0]
        body = (std.get("body") or "").strip()
        std_title = (std.get("std_title") or "").strip()
        slug = ("%s-%s" % (code, clause)).replace(" ", "-").replace("/", "-")
        fp = _os.path.join(dest, slug + ".md")

        text = "\n".join([
            "# %s 第 %s 条 · %s" % (code, clause, (std.get("title") or "").strip()),
            "",
            "> 标准：%s（%s）" % (std_title or "（标准名缺失）", code),
            "> 出处：条文库 standard %s#%s" % (code, clause),
            "> 确认：被引用 %s 次，经确认 %s 次%s" % (
                r.get("used_count"), r.get("verified_count"),
                "；**存在摘录不一致，待人工核对**" if r.get("disputed") else ""),
            "> 首次：%s；最近：%s" % (str(r.get("created_at") or "")[:10],
                                      str(r.get("last_used_at") or "")[:10]),
            "> 状态：authoritative（账本自动导出，勿手改；与原文不符以库为准）",
            "",
            "## 条文摘录",
            "",
            body or "（库里没有正文，只有条号）",
            ("" if not std.get("review_note") else "\n> 复核状态：%s" % std.get("review_note")),
            "",
        ])
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        try:
            if _os.path.exists(fp):
                old = io.open(fp, encoding="utf-8").read()
                if hashlib.md5(old.encode("utf-8")).hexdigest() == digest:
                    skipped += 1
                    continue
            io.open(fp, "w", encoding="utf-8", newline="\n").write(text)
            written.append(slug + ".md")
        except OSError as exc:
            errors.append("%s: %r" % (fp, exc))

    if written or skipped or errors:
        sys.stderr.write("[job] 权威依据导出：写 %d / 未变 %d / 失败 %d\n"
                         % (len(written), skipped, len(errors)))
    for e in errors[:5]:
        sys.stderr.write("[job] 导出失败 %s\n" % e)
    return {"authoritative": len(rows), "written": written, "skipped": skipped,
            "errors": errors[:5]}


# ---------------------------------------------------------------- 表与调度

DDL = """
CREATE TABLE IF NOT EXISTS service_job (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE,
  cron_expr VARCHAR(64) NOT NULL,
  handler VARCHAR(64) NOT NULL,
  payload JSON NULL,
  enabled TINYINT NOT NULL DEFAULT 1,
  last_run_at DATETIME NULL,
  last_status VARCHAR(16) NULL,
  last_error VARCHAR(500) NULL,
  next_run_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 服务自己要的 loop。加任务：写 handler + 在这里登记一行。
DEFAULTS = [
    ("pending_docs_digest", "0 18 * * *", "pending_docs_digest", {},
     "每天 18:00 统计待补文书并留痕"),
    ("cleanup_temp", "0 4 * * *", "cleanup_temp", {},
     "每天 04:00 清掉超过 1 天的临时产物（复核目录、语音 wav、出稿临时文件）"),
    ("export_authoritative", "40 3 * * 1", "export_authoritative", {},
     "每周一 03:40 把被反复确认的依据导出到 knowledge/kb/authoritative/"),
]


def ensure_table():
    exec_sql(DDL)
    for name, expr, hname, payload, _desc in DEFAULTS:
        exists = q("SELECT id FROM service_job WHERE name=%s", (name,))
        if exists:
            continue
        nxt = Cron(expr).next_after(datetime.now())
        exec_sql(
            "INSERT INTO service_job (name, cron_expr, handler, payload, next_run_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (name, expr, hname, json.dumps(payload, ensure_ascii=False), nxt),
        )
        sys.stderr.write("[job] 注册服务任务 %s（%s，下次 %s）\n" % (name, expr, nxt))


def claim_due(limit=5):
    """认领到点的任务（乐观锁：只有把 next_run_at 推后的那次算领到）"""
    due = q("SELECT id, name, cron_expr, handler, payload, next_run_at "
            "FROM service_job WHERE enabled=1 AND next_run_at<=NOW() "
            "ORDER BY next_run_at LIMIT %s", (limit,))
    got = []
    for row in due:
        try:
            nxt = Cron(row["cron_expr"]).next_after(datetime.now())
        except Exception:
            nxt = datetime.now() + timedelta(minutes=10)
        n = exec_sql(
            "UPDATE service_job SET next_run_at=%s WHERE id=%s AND next_run_at=%s",
            (nxt, row["id"], row["next_run_at"]),
        )
        if n == 1:
            got.append(row)
    return got


def run_one(row):
    name = row["name"]
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    try:
        result = run_handler(row["handler"], payload)
        exec_sql(
            "UPDATE service_job SET last_run_at=NOW(), last_status='ok', last_error=NULL WHERE id=%s",
            (row["id"],),
        )
        sys.stderr.write("[job] %s 完成：%s\n" % (name, json.dumps(result, ensure_ascii=False)[:200]))
        return True, result
    except Exception as exc:
        traceback.print_exc()
        exec_sql(
            "UPDATE service_job SET last_run_at=NOW(), last_status='failed', last_error=%s WHERE id=%s",
            (str(exc)[:500], row["id"]),
        )
        sys.stderr.write("[job] %s 失败：%s\n" % (name, exc))
        return False, str(exc)


def run_now(name):
    """手动触发一次（管理接口用），不动 next_run_at"""
    rows = q("SELECT id, name, cron_expr, handler, payload FROM service_job WHERE name=%s", (name,))
    if not rows:
        return None
    return run_one(rows[0])


def list_jobs():
    rows = q("SELECT name, cron_expr, handler, enabled, last_run_at, last_status, "
             "last_error, next_run_at FROM service_job ORDER BY name")
    return rows


def worker():
    """每 20 秒看一次有没有到点的任务"""
    while True:
        try:
            for row in claim_due():
                threading.Thread(target=run_one, args=(row,), daemon=True).start()
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def start():
    try:
        ensure_table()
    except Exception:
        traceback.print_exc()
        return
    threading.Thread(target=worker, daemon=True, name="service-jobs").start()
    sys.stderr.write("[job] 服务侧调度已启动\n")
