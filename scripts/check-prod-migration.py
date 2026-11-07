#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 生产迁移前后只读校验（默认库 jl；本脚本从不写库）。

用途：给生产迁移（0001_memory_v2）做独立的前/后状态检查。
  pre  : 断言 jl 尚无 v2 表、无 0001 迁移记录；记录 legacy memory 表的
         行数与 SHOW CREATE sha256（可用 --write-baseline 落盘给 post 用）。
  post : 断言 5 张 v2 表 + schema_migrations 已存在且 0001 checksum 与仓库文件一致；
         legacy memory 表的行数与 SHOW CREATE sha256 与 pre 基线一致（未被改动）；
         v2 表全为空（迁移只建结构，不写数据）。

安全：
  · 只读连接（额外设 SESSION transaction_read_only=1），不执行任何 DDL/DML；
  · 访问生产库 `jl` 需要 --allow-production + 进程环境 JL_ALLOW_PROD_MIGRATION=1；
  · 非法/未授权访问 exit 2；状态不达标打印 PROD_CHECK_FAIL 并 exit 1。

用法：
    # pre（记录基线）
    JL_ALLOW_PROD_MIGRATION=1 python3 scripts/check-prod-migration.py \
        --database jl --allow-production --phase pre \
        --write-baseline /tmp/m1-prod-baseline.json
    # post（对比基线）
    JL_ALLOW_PROD_MIGRATION=1 python3 scripts/check-prod-migration.py \
        --database jl --allow-production --phase post \
        --baseline /tmp/m1-prod-baseline.json
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("FATAL: pymysql 未安装", file=sys.stderr)
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATABASE = "jl"
PROD_DATABASE = "jl"
DB_HOST = "127.0.0.1"
DB_PORT = 3309
DB_USER = "jl"
PROD_ENV_VAR = "JL_ALLOW_PROD_MIGRATION"
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
MIGRATION_VERSION = "0001_memory_v2"

V2_TABLES = [
    "memory_item",
    "memory_variant",
    "memory_use",
    "memory_review",
    "memory_audit",
]


def die(message, code=1):
    print("FATAL: " + message, file=sys.stderr)
    sys.exit(code)


def load_env_file(path):
    env = {}
    if not os.path.isfile(path):
        return env
    for raw in io.open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def connect(env, database):
    from urllib.parse import unquote, urlsplit

    kwargs = dict(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=env.get("MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    dsn = env.get("MYSQL_DSN", "").strip()
    if dsn:
        if "://" not in dsn:
            dsn = "mysql://" + dsn
        parts = urlsplit(dsn)
        kwargs["host"] = parts.hostname or DB_HOST
        kwargs["port"] = parts.port or DB_PORT
        kwargs["user"] = unquote(parts.username) if parts.username else DB_USER
        kwargs["password"] = unquote(parts.password) if parts.password else ""
    try:
        conn = pymysql.connect(**kwargs)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if code == 1049:
            die("数据库 %s 不存在" % database)
        if code in (1044, 1045, 2003):
            die("无法连接 MySQL %s:%s（权限/服务/密码错误，code=%s）" % (
                kwargs["host"], kwargs["port"], code))
        raise
    # 只读自证：本脚本不允许任何写操作
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION transaction_read_only = 1")
    except Exception:  # pragma: no cover - 老版本 MySQL 不支持时仍保持只读代码路径
        pass
    return conn


def guard_database(name, allow_production=False):
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        die("非法数据库名（只允许字母/数字/下划线）：%r，exit 2" % name, 2)
    if name.lower() == PROD_DATABASE:
        if not allow_production:
            die("REFUSED: 拒绝检查生产库 'jl'。默认只允许 scratch 库；"
                "生产检查需 --allow-production + %s=1，exit 2" % PROD_ENV_VAR, 2)
        if os.environ.get(PROD_ENV_VAR) != "1":
            die("REFUSED: 生产检查需要进程环境 %s=1（export，不是 .env），exit 2"
                % PROD_ENV_VAR, 2)
    elif allow_production:
        die("REFUSED: --allow-production 只允许搭配 --database jl（当前 %s），exit 2"
            % name, 2)
    return name


def table_exists(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (name,),
        )
        return cur.fetchone()[0] > 0


def v2_tables_present(conn):
    return [table for table in V2_TABLES if table_exists(conn, table)]


def memory_snapshot(conn):
    if not table_exists(conn, "memory"):
        return {"present": False, "rows": None, "create_sha256": None}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM memory")
        rows = cur.fetchone()[0]
        cur.execute("SHOW CREATE TABLE memory")
        create_sql = cur.fetchone()[1]
    return {
        "present": True,
        "rows": rows,
        "create_sha256": hashlib.sha256(create_sql.encode("utf-8")).hexdigest(),
    }


def migration_file_checksum():
    path = os.path.join(ROOT, "db", "migrations", MIGRATION_VERSION + ".sql")
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_migration_row(conn, version):
    if not table_exists(conn, "schema_migrations"):
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT checksum FROM schema_migrations WHERE version = %s",
                    (version,))
        row = cur.fetchone()
    return row[0] if row else None


class ProdChecker(object):
    def __init__(self, conn, database):
        self.conn = conn
        self.database = database
        self.checks = []
        self.snapshot = None

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    def report(self, phase):
        ok_all = True
        for name, ok, detail in self.checks:
            if ok:
                print("PASS  " + name)
            else:
                suffix = ("  -- " + detail) if detail else ""
                print("FAIL  " + name + suffix)
                ok_all = False
        if ok_all:
            print("PROD_CHECK_OK phase=%s database=%s" % (phase, self.database))
            return 0
        print("PROD_CHECK_FAIL phase=%s database=%s" % (phase, self.database))
        return 1

    # ---------------------------------------------------------------- pre
    def run_pre(self, require_memory):
        present = v2_tables_present(self.conn)
        self.check(
            "pre: 尚无 v2 表（5 张都不存在）",
            not present,
            "已存在 " + ", ".join(present) if present else "",
        )
        if table_exists(self.conn, "schema_migrations"):
            recorded = read_migration_row(self.conn, MIGRATION_VERSION)
            self.check(
                "pre: schema_migrations 无 %s 记录" % MIGRATION_VERSION,
                recorded is None,
                "已有记录 checksum=%s" % recorded,
            )
        else:
            self.check("pre: schema_migrations 尚不存在（或为空表）", True)
        self.snapshot = memory_snapshot(self.conn)
        if require_memory:
            self.check(
                "pre: legacy memory 表存在",
                self.snapshot["present"],
                "memory 表不存在",
            )
        else:
            self.check(
                "pre: 记录 legacy memory 表状态（该库无 memory 时允许缺失）",
                True,
                "",
            )
        if self.snapshot["present"]:
            self.check(
                "pre: 记录 memory 基线 rows=%s create_sha256=%s" % (
                    self.snapshot["rows"], self.snapshot["create_sha256"]),
                True,
            )
        else:
            self.check("pre: memory 表不存在（基线 present=False）", True)
        return self.report("pre")

    # ---------------------------------------------------------------- post
    def run_post(self, baseline):
        present = v2_tables_present(self.conn)
        self.check(
            "post: 5 张 v2 表都在",
            len(present) == len(V2_TABLES),
            "缺少 " + ", ".join(t for t in V2_TABLES if t not in present),
        )
        self.check(
            "post: schema_migrations 存在",
            table_exists(self.conn, "schema_migrations"),
            "schema_migrations 表不存在",
        )
        expected_checksum = migration_file_checksum()
        recorded = read_migration_row(self.conn, MIGRATION_VERSION)
        self.check(
            "post: %s 已记录且 checksum 与仓库文件一致" % MIGRATION_VERSION,
            expected_checksum is not None and recorded == expected_checksum,
            "库中 %r / 文件 %r" % (recorded, expected_checksum),
        )

        current = memory_snapshot(self.conn)
        base = baseline.get("memory") or {}
        self.check(
            "post: legacy memory 表存在性与 pre 一致",
            current["present"] == base.get("present"),
            "pre=%r post=%r" % (base.get("present"), current["present"]),
        )
        self.check(
            "post: memory 行数不变（%r -> %r）" % (
                base.get("rows"), current["rows"]),
            base.get("rows") == current["rows"],
            "行数变化",
        )
        self.check(
            "post: memory SHOW CREATE 与 pre 逐字节一致（sha256）",
            base.get("create_sha256") == current["create_sha256"],
            "pre=%r post=%r" % (base.get("create_sha256"),
                                current["create_sha256"]),
        )
        # 迁移只建结构：v2 表必须为空
        empty_ok = True
        counts = {}
        with self.conn.cursor() as cur:
            for table in V2_TABLES:
                if table not in present:
                    continue
                cur.execute("SELECT COUNT(*) FROM `%s`" % table)
                counts[table] = cur.fetchone()[0]
                if counts[table] != 0:
                    empty_ok = False
        self.check(
            "post: v2 表无数据（迁移只建结构）",
            empty_ok,
            "非空: " + ", ".join("%s=%d" % (k, v) for k, v in counts.items()
                                  if v) if not empty_ok else "",
        )
        return self.report("post")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1 生产迁移前后只读校验（默认 jl；从不写库）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl；'jl' 需 --allow-production + 环境门禁）")
    parser.add_argument("--allow-production", action="store_true",
                        help="显式放行生产库 jl（还需 JL_ALLOW_PROD_MIGRATION=1）")
    parser.add_argument("--phase", choices=("pre", "post"), default="pre",
                        help="pre=迁移前基线；post=迁移后对比基线")
    parser.add_argument("--baseline", default=None,
                        help="post 必填：pre 阶段 --write-baseline 产出的 JSON")
    parser.add_argument("--write-baseline", default=None,
                        help="pre 阶段可选：把基线写入该 JSON 路径")
    parser.add_argument("--env-file", default=None,
                        help=".env 路径（默认 <repo>/.env，可用 M1_ENV_FILE 覆盖）")
    args = parser.parse_args(argv)

    database = guard_database(args.database, args.allow_production)
    if args.phase == "post" and not args.baseline:
        die("post 阶段必须提供 --baseline <pre 基线 JSON>，exit 2", 2)
    if args.phase == "pre" and args.baseline:
        die("pre 阶段不接受 --baseline（用 --write-baseline），exit 2", 2)

    env_file = args.env_file or os.environ.get("M1_ENV_FILE") or os.path.join(ROOT, ".env")
    if not os.path.isfile(env_file):
        die("找不到 .env：%s（可用 --env-file 指定）" % env_file)
    env = load_env_file(env_file)
    if not env.get("MYSQL_PASSWORD") and not env.get("MYSQL_DSN"):
        die(".env 中缺少 MYSQL_PASSWORD / MYSQL_DSN：%s" % env_file)

    baseline = None
    if args.phase == "post":
        if not os.path.isfile(args.baseline):
            die("基线文件不存在：%s，exit 2" % args.baseline, 2)
        try:
            baseline = json.load(io.open(args.baseline, encoding="utf-8"))
        except ValueError as exc:
            die("基线不是合法 JSON：%s（%s），exit 2" % (args.baseline, exc), 2)
        if baseline.get("database") != database:
            die("基线数据库 %r 与当前 --database %r 不一致，exit 2"
                % (baseline.get("database"), database), 2)
        if baseline.get("phase") != "pre":
            die("基线 phase=%r 不是 pre，exit 2" % baseline.get("phase"), 2)
        if "memory" not in baseline:
            die("基线缺少 memory 字段，exit 2", 2)

    print("phase=%s database=%s host=%s:%s user=%s (read-only)" % (
        args.phase, database, DB_HOST, DB_PORT, DB_USER))
    conn = connect(env, database)
    try:
        checker = ProdChecker(conn, database)
        if args.phase == "pre":
            rc = checker.run_pre(require_memory=(database.lower() == PROD_DATABASE))
        else:
            rc = checker.run_post(baseline)
        if rc == 0 and args.phase == "pre" and args.write_baseline:
            payload = {
                "database": database,
                "phase": "pre",
                "checked_at": datetime.now().astimezone().isoformat(),
                "memory": checker.snapshot,
                "v2_tables_present": False,
                "migration_version": MIGRATION_VERSION,
            }
            with io.open(args.write_baseline, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            print("BASELINE_WRITTEN %s" % os.path.abspath(args.write_baseline))
        return rc
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
