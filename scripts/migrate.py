#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 记忆 v2 迁移运行器（scratch 库专用）。

设计要点：
  · 只读 db/migrations/*.sql（up）与同名 *.down.sql（down），按版本号排序；
  · schema_migrations 记录 version + sha256 校验和，重复执行为 no-op；
    已应用版本的文件一旦被改动（校验和不一致）立即失败，防止“改历史”；
  · 硬拒绝生产库 jl：--database jl 直接 exit 2；
  · 只连 127.0.0.1:3309 的 jl 账号，密码取自 .env（MYSQL_PASSWORD）或 MYSQL_DSN。

用法（在服务器 /tmp 测试副本上运行，--env-file 指向部署仓库的 .env）：
    python3 scripts/migrate.py                     # 应用到 jl_m1（默认）
    python3 scripts/migrate.py --check             # 只检查，不写库
    python3 scripts/migrate.py --dry-run           # 只打印计划，不写库
    python3 scripts/migrate.py --rollback          # 回滚到空（幂等）
    python3 scripts/migrate.py --database jl       # exit 2（生产库拒绝）
"""

import argparse
import hashlib
import io
import os
import re
import sys
from urllib.parse import unquote, urlsplit

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("FATAL: pymysql 未安装", file=sys.stderr)
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATABASE = "jl_m1"
PROD_DATABASE = "jl"
DB_HOST = "127.0.0.1"
DB_PORT = 3309
DB_USER = "jl"
MIGRATION_FILE_RE = re.compile(r"^([0-9A-Za-z_.-]+)\.sql$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(64) NOT NULL,
  checksum   CHAR(64) NOT NULL,
  applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='迁移版本与 sha256 校验和'
"""


class Migration(object):
    def __init__(self, version, path, down_path, checksum, sql_text):
        self.version = version
        self.path = path
        self.down_path = down_path
        self.checksum = checksum
        self.sql_text = sql_text


def die(message, code=1):
    print("FATAL: " + message, file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ env
def load_env_file(path):
    """极简 .env 解析：KEY=VALUE，忽略注释/空行，去掉成对引号。不打印任何值。"""
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


def connection_kwargs(env, database):
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
    return kwargs


def guard_database(name):
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        die("非法数据库名（只允许字母/数字/下划线）：%r，exit 2" % name, 2)
    if name.lower() == PROD_DATABASE:
        die(
            "REFUSED: 拒绝操作生产库 'jl'。本工具只允许 scratch 库（默认 jl_m1），exit 2",
            2,
        )
    return name


# ------------------------------------------------------------------ fs
def checksum_of(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(migrations_dir):
    if not os.path.isdir(migrations_dir):
        die("迁移目录不存在：%s" % migrations_dir)
    migrations = []
    for filename in sorted(os.listdir(migrations_dir)):
        match = MIGRATION_FILE_RE.match(filename)
        if not match or filename.endswith(".down.sql"):
            continue
        version = match.group(1)
        path = os.path.join(migrations_dir, filename)
        down_path = os.path.join(migrations_dir, version + ".down.sql")
        migrations.append(
            Migration(
                version=version,
                path=path,
                down_path=down_path,
                checksum=checksum_of(path),
                sql_text=io.open(path, encoding="utf-8").read(),
            )
        )
    if not migrations:
        die("没有发现任何 up 迁移文件：%s" % migrations_dir)
    return migrations


def split_sql(sql):
    """把 SQL 文件切成单条语句：跳过 -- / # / 块注释，正确处理引号与转义。"""
    statements = []
    buf = []
    i, size = 0, len(sql)
    quote = None
    while i < size:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < size else ""
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"') and nxt:
                buf.append(nxt)
                i += 2
                continue
            if ch == quote:
                if nxt == quote:  # '' 或 "" 转义，仍处于字符串内
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "#":
            while i < size and sql[i] != "\n":
                i += 1
            continue
        if ch == "-" and nxt == "-":
            while i < size and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = size if end == -1 else end + 2
            continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


# ------------------------------------------------------------------ db
def connect(env, database):
    kwargs = connection_kwargs(env, database)
    try:
        return pymysql.connect(**kwargs)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if code == 1049:  # unknown database
            die(
                "数据库 %s 不存在。先创建（不要在 jl 上执行）："
                " CREATE DATABASE %s CHARACTER SET utf8mb4;" % (database, database)
            )
        if code in (1044, 1045, 2003):
            die("无法连接 MySQL %s:%s（权限/服务/密码错误，code=%s）" % (
                kwargs["host"], kwargs["port"], code))
        raise


def table_exists(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (name,),
        )
        return cur.fetchone()[0] > 0


def ensure_schema_migrations(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_MIGRATIONS_DDL)


def read_applied(conn):
    if not table_exists(conn, "schema_migrations"):
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


# ------------------------------------------------------------------ commands
def cmd_apply(conn, migrations):
    ensure_schema_migrations(conn)
    applied = read_applied(conn)
    applied_count = 0
    for migration in migrations:
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                die(
                    "CHECKSUM MISMATCH %s：已应用=%s 文件=%s（迁移文件被改动过，拒绝继续）"
                    % (migration.version, applied[migration.version], migration.checksum)
                )
            print("SKIP  %s (already applied, checksum ok)" % migration.version)
            continue
        print("APPLY %s  <- %s" % (migration.version, os.path.basename(migration.path)))
        statements = split_sql(migration.sql_text)
        if not statements:
            die("迁移 %s 中没有可执行语句" % migration.version)
        with conn.cursor() as cur:
            for statement in statements:
                try:
                    cur.execute(statement)
                except pymysql.err.MySQLError as exc:
                    die("FAILED %s: %s\n-- 语句：\n%s" % (
                        migration.version, exc, statement[:800]))
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
        print("  OK  %d statement(s)" % len(statements))
        applied_count += 1

    known = {migration.version for migration in migrations}
    orphans = sorted(set(applied) - known)
    if orphans:
        die("schema_migrations 中存在无对应文件的版本：%s" % ", ".join(orphans))

    if applied_count:
        print("MIGRATE_OK (%d migration(s) applied)" % applied_count)
    else:
        print("MIGRATE_OK (no-op: all migrations already applied)")
    return 0


def cmd_check(conn, migrations):
    if not table_exists(conn, "schema_migrations"):
        print("PENDING  (schema_migrations 不存在，所有迁移未应用)")
        print("MIGRATIONS_NOT_APPLIED")
        return 1
    applied = read_applied(conn)
    ok = True
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is None:
            print("PENDING  %s" % migration.version)
            ok = False
            continue
        if recorded != migration.checksum:
            print("MISMATCH %s：已应用=%s 文件=%s" % (
                migration.version, recorded, migration.checksum))
            ok = False
            continue
        print("APPLIED  %s (checksum ok)" % migration.version)
    known = {migration.version for migration in migrations}
    orphans = sorted(set(applied) - known)
    if orphans:
        print("ORPHAN   %s" % ", ".join(orphans))
        ok = False
    print("MIGRATIONS_OK" if ok else "MIGRATIONS_NOT_APPLIED")
    return 0 if ok else 1


def cmd_dry_run(env, database, migrations):
    conn = connect(env, database)
    try:
        applied = read_applied(conn)
    finally:
        conn.close()
    print("DRY-RUN database=%s host=%s:%s user=%s" % (
        database, DB_HOST, DB_PORT, DB_USER))
    pending = 0
    for migration in migrations:
        if migration.version in applied:
            print("  SKIP     %s" % migration.version)
        else:
            count = len(split_sql(migration.sql_text))
            print("  WOULD-APPLY %s (%d statement(s))" % (migration.version, count))
            pending += 1
    print("DRY_RUN_OK (no changes made; pending=%d)" % pending)
    return 0


def cmd_rollback(conn, migrations):
    if not table_exists(conn, "schema_migrations"):
        print("ROLLBACK_NOOP (schema_migrations 不存在)")
        return 0
    applied = read_applied(conn)
    if not applied:
        print("ROLLBACK_NOOP (没有已应用的迁移)")
        return 0
    by_version = {migration.version: migration for migration in migrations}
    rolled = 0
    for version in sorted(applied, reverse=True):
        migration = by_version.get(version)
        if migration is None or not os.path.isfile(migration.down_path):
            die("缺少 %s 的 down 文件：%s" % (
                version, migration.down_path if migration else "?"))
        print("ROLLBACK %s  <- %s" % (
            version, os.path.basename(migration.down_path)))
        statements = split_sql(io.open(migration.down_path, encoding="utf-8").read())
        if not statements:
            die("down 迁移 %s 中没有可执行语句" % version)
        with conn.cursor() as cur:
            for statement in statements:
                try:
                    cur.execute(statement)
                except pymysql.err.MySQLError as exc:
                    die("ROLLBACK FAILED %s: %s\n-- 语句：\n%s" % (
                        version, exc, statement[:800]))
            cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
        print("  OK  %d statement(s), schema_migrations row deleted" % len(statements))
        rolled += 1
    print("ROLLBACK_OK (%d migration(s) rolled back)" % rolled)
    return 0


# ------------------------------------------------------------------ main
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1 记忆 v2 迁移运行器（仅 scratch 库，默认 jl_m1）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl_m1；'jl' 会被硬拒绝）")
    parser.add_argument("--rollback", action="store_true", help="按 down.sql 回滚已应用迁移")
    parser.add_argument("--check", action="store_true", help="只检查迁移状态，不写库")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的迁移，不写库")
    parser.add_argument("--env-file", default=None,
                        help=".env 路径（默认 <repo>/.env，可用 M1_ENV_FILE 覆盖）")
    parser.add_argument("--migrations-dir", default=None,
                        help="迁移目录（默认 <repo>/db/migrations）")
    args = parser.parse_args(argv)

    database = guard_database(args.database)
    if args.dry_run and (args.check or args.rollback):
        die("--dry-run 不能与 --check / --rollback 同时使用，exit 2", 2)

    migrations_dir = args.migrations_dir or os.path.join(ROOT, "db", "migrations")
    env_file = args.env_file or os.environ.get("M1_ENV_FILE") or os.path.join(ROOT, ".env")
    if not os.path.isfile(env_file):
        die("找不到 .env：%s（可用 --env-file 指定）" % env_file)
    env = load_env_file(env_file)
    if not env.get("MYSQL_PASSWORD") and not env.get("MYSQL_DSN"):
        die(".env 中缺少 MYSQL_PASSWORD / MYSQL_DSN：%s" % env_file)
    migrations = discover(migrations_dir)
    print("database=%s env_file=%s migrations=%d" % (
        database, os.path.basename(env_file), len(migrations)))

    if args.dry_run:
        return cmd_dry_run(env, database, migrations)

    conn = connect(env, database)
    try:
        if args.check:
            return cmd_check(conn, migrations)
        if args.rollback:
            return cmd_rollback(conn, migrations)
        return cmd_apply(conn, migrations)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
