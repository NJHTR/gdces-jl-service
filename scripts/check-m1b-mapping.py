#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1b-1 只读验收：0002 映射列 + legacy memory 回填结果（默认库 jl_m2）。

断言：
  · 0002 已应用：memory_item.legacy_memory_id 列存在（BIGINT UNSIGNED NULL），
    且有单列 UNIQUE 索引（uk_legacy_memory）；
  · legacy `memory` 行数 = 期望（默认 39，--expect-legacy-rows 可调）且结构未被改；
  · 每条 legacy 行都已映射：mapped 数 = legacy 数；无遗漏、无孤儿、无重复；
  · 映射字段逐条正确：kind/status/created_by/confidence/canonical_text/evidence/
    scope/scope_id/project_id/expires_at；
  · 幂等证据：再跑一次回填将插入 0 行（LEFT JOIN 缺口 = 0，唯一键存在）。

只读（额外 SET SESSION transaction_read_only=1），不写任何表。
安全：默认拒绝生产库 jl；--database jl 需 --allow-production + 进程环境
      JL_ALLOW_PROD_MIGRATION=1。通过打印 M1B_MAPPING_OK，否则 M1B_MAPPING_FAIL。
"""

import argparse
import hashlib
import io
import os
import re
import sys

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("FATAL: pymysql 未安装", file=sys.stderr)
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATABASE = "jl_m2"
PROD_DATABASE = "jl"
DB_HOST = "127.0.0.1"
DB_PORT = 3309
DB_USER = "jl"
PROD_ENV_VAR = "JL_ALLOW_PROD_MIGRATION"
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
MIGRATION_VERSION = "0002_memory_v2_legacy_map"
EXPECT_LEGACY_ROWS = 39

LEGACY_TABLE = "memory"
V2_TABLE = "memory_item"
LEGACY_COLUMNS = [
    "id", "scope", "owner_id", "project_id", "text", "source_ref",
    "status", "hits", "created_at", "updated_at",
]

SCOPE_EXPR = (
    "CASE "
    "WHEN m.scope = 'project' THEN 'project' "
    "WHEN m.scope = 'user' AND m.project_id IS NOT NULL THEN 'project_user' "
    "WHEN m.scope = 'user' THEN 'user' "
    "ELSE NULL END"
)
PROJECT_EXPR = (
    "CASE "
    "WHEN m.scope = 'project' THEN m.owner_id "
    "WHEN m.scope = 'user' AND m.project_id IS NOT NULL THEN m.project_id "
    "ELSE NULL END"
)

CONTENT_MISMATCH_SQL = """
SELECT COUNT(*)
FROM memory_item mi
JOIN memory m ON m.id = mi.legacy_memory_id
WHERE mi.kind <> 'rule'
   OR mi.status <> 'candidate'
   OR mi.created_by <> 'import'
   OR mi.confidence <> 0.500
   OR mi.canonical_text <> m.text
   OR NOT (mi.evidence <=> m.source_ref)
   OR NOT (mi.scope <=> {scope_expr})
   OR NOT (mi.scope_id <=> m.owner_id)
   OR NOT (mi.project_id <=> {project_expr})
""".format(scope_expr=SCOPE_EXPR, project_expr=PROJECT_EXPR)

EXPIRED_BAD_SQL = """
SELECT COUNT(*)
FROM memory_item
WHERE legacy_memory_id IS NOT NULL
  AND (expires_at IS NULL
       OR expires_at <= NOW()
       OR expires_at > DATE_ADD(NOW(), INTERVAL 15 DAY))
"""


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
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=env.get("MYSQL_PASSWORD", ""), database=database,
        charset="utf8mb4", autocommit=True, connect_timeout=10,
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
            die("数据库 %s 不存在（先克隆 jl -> jl_m2 并应用 0001+0002）" % database)
        if code in (1044, 1045, 2003):
            die("无法连接 MySQL %s:%s（权限/服务/密码错误，code=%s）"
                % (kwargs["host"], kwargs["port"], code))
        raise
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION transaction_read_only = 1")
    except Exception:  # pragma: no cover
        pass
    return conn


def guard_database(name, allow_production=False):
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        die("非法数据库名（只允许字母/数字/下划线）：%r，exit 2" % name, 2)
    if name.lower() == PROD_DATABASE:
        if not allow_production:
            die("REFUSED: 拒绝检查生产库 'jl'。默认只允许克隆库（默认 jl_m2）；"
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
            "WHERE table_schema = DATABASE() AND table_name = %s", (name,))
        return cur.fetchone()[0] > 0


def scalar(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def migration_file_checksum():
    path = os.path.join(ROOT, "db", "migrations", MIGRATION_VERSION + ".sql")
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Checker(object):
    def __init__(self, conn, expect_legacy_rows):
        self.conn = conn
        self.expect_legacy_rows = expect_legacy_rows
        self.checks = []
        self.scope_actual = {}
        self.scope_expected = {}

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    def run(self):
        self.check_mapping_column()
        self.check_unique_key()
        self.check_migration_row()
        self.check_legacy_table()
        self.check_mapping_coverage()
        self.check_scope_counts()
        self.check_content()
        self.check_expiry()
        self.check_idempotency()
        ok_all = True
        for name, ok, detail in self.checks:
            if ok:
                print("PASS  " + name)
            else:
                suffix = ("  -- " + detail) if detail else ""
                print("FAIL  " + name + suffix)
                ok_all = False
        print("scope counts actual   : %s" % (
            ", ".join("%s=%d" % (k, v) for k, v in sorted(self.scope_actual.items()))
            or "(none)"))
        print("scope counts expected : %s" % (
            ", ".join("%s=%d" % (k, v) for k, v in sorted(self.scope_expected.items()))
            or "(none)"))
        print("M1B_MAPPING_OK" if ok_all else "M1B_MAPPING_FAIL")
        return 0 if ok_all else 1

    # ---------------------------------------------------------------- schema
    def check_mapping_column(self):
        exists = table_exists(self.conn, V2_TABLE)
        self.check("memory_item 表存在", exists, "表不存在")
        if not exists:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT column_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "  AND column_name = 'legacy_memory_id'", (V2_TABLE,))
            row = cur.fetchone()
        self.check("0002: memory_item.legacy_memory_id 列存在", row is not None,
                   "列缺失（0002 未应用？）")
        if row:
            self.check("0002: legacy_memory_id 类型 = bigint unsigned",
                       row[0] == "bigint unsigned", "实际 %s" % row[0])
            self.check("0002: legacy_memory_id 可空（NULL=非导入行）",
                       row[1] == "YES", "实际 IS_NULLABLE=%s" % row[1])

    def check_unique_key(self):
        if not table_exists(self.conn, V2_TABLE):
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT index_name, non_unique, seq_in_index, column_name "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "  AND column_name = 'legacy_memory_id' "
                "ORDER BY index_name, seq_in_index", (V2_TABLE,))
            rows = cur.fetchall()
        unique_single = any(
            non_unique == 0 and seq == 1
            for _name, non_unique, seq, _col in rows)
        self.check("0002: legacy_memory_id 上有单列 UNIQUE 索引（幂等键）",
                   unique_single,
                   "实际索引 %r" % [(r[0], r[1], r[2], r[3]) for r in rows])

    def check_migration_row(self):
        expected = migration_file_checksum()
        if not table_exists(self.conn, "schema_migrations"):
            self.check("0002 已应用（schema_migrations 记录）", False,
                       "schema_migrations 不存在")
            return
        with self.conn.cursor() as cur:
            cur.execute("SELECT checksum FROM schema_migrations WHERE version = %s",
                        (MIGRATION_VERSION,))
            row = cur.fetchone()
        self.check("0002 已应用且 checksum 与仓库文件一致",
                   expected is not None and row is not None and row[0] == expected,
                   "库中 %r / 文件 %r" % (row[0] if row else None, expected))

    # ---------------------------------------------------------------- legacy
    def check_legacy_table(self):
        if not table_exists(self.conn, LEGACY_TABLE):
            self.check("legacy memory 表存在（未被改名/删除）", False, "表不存在")
            return
        self.check("legacy memory 表存在（未被改名/删除）", True)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s", (LEGACY_TABLE,))
            columns = {row[0] for row in cur.fetchall()}
        missing = [c for c in LEGACY_COLUMNS if c not in columns]
        self.check("legacy memory 结构未变（原列都在，且未加 v2 列）",
                   not missing and "legacy_memory_id" not in columns,
                   "缺少 %r / 多出 legacy_memory_id=%r"
                   % (missing, "legacy_memory_id" in columns))
        rows = scalar(self.conn, "SELECT COUNT(*) FROM memory")
        self.check("legacy memory 行数 = %d（未被增删）" % self.expect_legacy_rows,
                   int(rows) == self.expect_legacy_rows, "实际 %s" % rows)

    # ---------------------------------------------------------------- mapping
    def check_mapping_coverage(self):
        if not table_exists(self.conn, V2_TABLE) or not table_exists(self.conn, LEGACY_TABLE):
            return
        legacy = int(scalar(self.conn, "SELECT COUNT(*) FROM memory"))
        total = int(scalar(self.conn, "SELECT COUNT(*) FROM memory_item"))
        mapped = int(scalar(self.conn, "SELECT COUNT(*) FROM memory_item "
                                     "WHERE legacy_memory_id IS NOT NULL"))
        distinct = int(scalar(self.conn, "SELECT COUNT(DISTINCT legacy_memory_id) "
                                        "FROM memory_item "
                                        "WHERE legacy_memory_id IS NOT NULL"))
        missing = int(scalar(
            self.conn,
            "SELECT COUNT(*) FROM memory m "
            "LEFT JOIN memory_item mi ON mi.legacy_memory_id = m.id "
            "WHERE mi.id IS NULL"))
        orphan = int(scalar(
            self.conn,
            "SELECT COUNT(*) FROM memory_item mi "
            "LEFT JOIN memory m ON m.id = mi.legacy_memory_id "
            "WHERE mi.legacy_memory_id IS NOT NULL AND m.id IS NULL"))
        self.check("每条 legacy 行都已映射（mapped=%d = legacy=%d）" % (mapped, legacy),
                   mapped == legacy, "mapped=%d legacy=%d" % (mapped, legacy))
        self.check("无遗漏：回填 LEFT JOIN 缺口 = 0", missing == 0, "缺口 %d" % missing)
        self.check("无孤儿：v2 的 legacy_memory_id 都能在 memory 找到",
                   orphan == 0, "孤儿 %d" % orphan)
        self.check("无重复：mapped 行数 = distinct legacy id",
                   mapped == distinct, "mapped=%d distinct=%d" % (mapped, distinct))
        self.check("所有 memory_item 行都来自 legacy 导入（本项只做映射）",
                   total == mapped, "total=%d mapped=%d" % (total, mapped))

    def check_scope_counts(self):
        if not table_exists(self.conn, V2_TABLE) or not table_exists(self.conn, LEGACY_TABLE):
            return
        with self.conn.cursor() as cur:
            cur.execute("SELECT scope, COUNT(*) FROM memory_item GROUP BY scope")
            self.scope_actual = {row[0]: int(row[1]) for row in cur.fetchall()}
            cur.execute(
                "SELECT {scope_expr} AS mapped_scope, COUNT(*) FROM memory m "
                "GROUP BY mapped_scope".format(scope_expr=SCOPE_EXPR))
            self.scope_expected = {
                row[0]: int(row[1]) for row in cur.fetchall() if row[0] is not None}
        self.check("scope 计数与 legacy 映射规则一致",
                   self.scope_actual == self.scope_expected,
                   "actual=%r expected=%r"
                   % (self.scope_actual, self.scope_expected))

    def check_content(self):
        if not table_exists(self.conn, V2_TABLE) or not table_exists(self.conn, LEGACY_TABLE):
            return
        mismatch = int(scalar(self.conn, CONTENT_MISMATCH_SQL))
        self.check(
            "逐条映射字段正确（kind=rule/status=candidate/created_by=import/"
            "confidence=0.5/canonical_text/evidence/scope/scope_id/project_id）",
            mismatch == 0, "不一致 %d 行" % mismatch)

    def check_expiry(self):
        if not table_exists(self.conn, V2_TABLE):
            return
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM memory_item WHERE legacy_memory_id IS NOT NULL")
            mapped = int(cur.fetchone()[0])
        if mapped == 0:
            self.check("expires_at = 回填时刻 + 14 天（无导入行，跳过）", True)
            return
        bad = int(scalar(self.conn, EXPIRED_BAD_SQL))
        self.check("expires_at 全部非空且落在 [now, now+15 天]（回填=now+14 天）",
                   bad == 0, "异常 %d 行" % bad)

    def check_idempotency(self):
        if not table_exists(self.conn, V2_TABLE) or not table_exists(self.conn, LEGACY_TABLE):
            return
        would_insert = int(scalar(
            self.conn,
            "SELECT COUNT(*) FROM memory m "
            "LEFT JOIN memory_item mi ON mi.legacy_memory_id = m.id "
            "WHERE mi.id IS NULL"))
        self.check("幂等证据：再执行 --execute 将插入 0 行",
                   would_insert == 0, "would_insert=%d" % would_insert)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1b-1 只读验收：0002 映射列 + legacy 回填结果（默认 jl_m2）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl_m2；'jl' 需 --allow-production + 环境门禁）")
    parser.add_argument("--allow-production", action="store_true",
                        help="显式放行生产库 jl（还需 JL_ALLOW_PROD_MIGRATION=1）")
    parser.add_argument("--expect-legacy-rows", type=int, default=EXPECT_LEGACY_ROWS,
                        help="legacy memory 期望行数（默认 39）")
    parser.add_argument("--env-file", default=None,
                        help=".env 路径（默认 <repo>/.env，可用 M1_ENV_FILE 覆盖）")
    args = parser.parse_args(argv)

    database = guard_database(args.database, args.allow_production)
    env_file = args.env_file or os.environ.get("M1_ENV_FILE") or os.path.join(ROOT, ".env")
    if not os.path.isfile(env_file):
        die("找不到 .env：%s（可用 --env-file 指定）" % env_file)
    env = load_env_file(env_file)
    if not env.get("MYSQL_PASSWORD") and not env.get("MYSQL_DSN"):
        die(".env 中缺少 MYSQL_PASSWORD / MYSQL_DSN：%s" % env_file)

    print("database=%s host=%s:%s user=%s expect_legacy_rows=%d (read-only)" % (
        database, DB_HOST, DB_PORT, DB_USER, args.expect_legacy_rows))
    conn = connect(env, database)
    try:
        return Checker(conn, args.expect_legacy_rows).run()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
