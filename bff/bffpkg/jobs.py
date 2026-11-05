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
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

from .core import exec_sql, q

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
