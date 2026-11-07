#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 记忆 v2 迁移运行器（默认只操作 scratch 库；生产库需显式受控路径）。

默认安全：
  · 只连 127.0.0.1:3309 的 jl 账号，密码取自 .env（MYSQL_PASSWORD）或 MYSQL_DSN；
  · 只读 db/migrations/*.sql（up）与同名 *.down.sql（down），按版本号排序；
  · schema_migrations 记录 version + sha256 校验和，重复执行为 no-op；
    已应用版本的文件一旦被改动（校验和不一致）立即失败；
  · 默认拒绝生产库 `jl`（exit 2）。

受控生产路径（--database jl --allow-production），额外要求：
  · 进程环境 JL_ALLOW_PROD_MIGRATION=1（不是 .env 里的值）；
  · apply / rollback / dry-run 必须带 --backup <gzip 备份>，gzip -t 通过且非空，
    输出备份 sha256 与字节数；
  · 只允许“不改既有表”的迁移：pending SQL 仅 CREATE TABLE IF NOT EXISTS / SET NAMES，
    且目标表在库中不存在；rollback 仅 DROP TABLE IF EXISTS 5 张 v2 表；
  · 执行前后校验 memory 表不变量：行数不变、SHOW CREATE 逐字节一致；
    任一项不满足即失败（DDL 前 abort，异常时提示从备份恢复）。

用法（服务器 /tmp 测试副本上运行，--env-file 指向部署仓库的 .env）：
    python3 scripts/migrate.py                     # 应用到 jl_m1（默认）
    python3 scripts/migrate.py --check             # 只检查，不写库
    python3 scripts/migrate.py --dry-run           # 只打印计划，不写库
    python3 scripts/migrate.py --rollback          # 回滚到空（幂等）
    python3 scripts/migrate.py --database jl       # exit 2（生产库默认拒绝）
    JL_ALLOW_PROD_MIGRATION=1 python3 scripts/migrate.py --database jl \
        --allow-production --backup /path/jl-pre-m1.sql.gz          # 受控生产 apply
    JL_ALLOW_PROD_MIGRATION=1 python3 scripts/migrate.py --database jl \
        --allow-production --backup /path/jl-pre-m1.sql.gz --dry-run
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
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
PROD_ENV_VAR = "JL_ALLOW_PROD_MIGRATION"
MIGRATION_FILE_RE = re.compile(r"^([0-9A-Za-z_.-]+)\.sql$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")

V2_TABLES = [
    "memory_item",
    "memory_variant",
    "memory_use",
    "memory_review",
    "memory_audit",
]

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


def validate_database_name(name):
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        die("非法数据库名（只允许字母/数字/下划线）：%r，exit 2" % name, 2)
    return name


def require_prod_env():
    """生产路径必须由进程环境显式放行；.env 里的同名键不算。"""
    if os.environ.get(PROD_ENV_VAR) != "1":
        die(
            "REFUSED: 生产路径需要进程环境 %s=1（export，不是 .env），exit 2"
            % PROD_ENV_VAR,
            2,
        )


def validate_backup(path):
    """生产备份门禁：存在、非空、gzip -t 通过；返回绝对路径/sha256/字节数。"""
    if not path:
        die("REFUSED: 生产路径必须提供 --backup <gzip 备份文件>，exit 2", 2)
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        die("REFUSED: --backup 文件不存在：%s，exit 2" % abs_path, 2)
    size = os.path.getsize(abs_path)
    if size <= 0:
        die("REFUSED: --backup 文件为空：%s，exit 2" % abs_path, 2)
    try:
        checked = subprocess.run(
            ["gzip", "-t", abs_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        die("REFUSED: 找不到 gzip 命令，无法校验备份，exit 2", 2)
    if checked.returncode != 0:
        detail = (checked.stderr or b"").decode("utf-8", "replace").strip()
        die("REFUSED: --backup 不是有效 gzip（gzip -t 失败）：%s %s，exit 2"
            % (abs_path, detail), 2)
    digest = hashlib.sha256()
    with io.open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return {
        "path": abs_path,
        "sha256": digest.hexdigest(),
        "size": size,
    }


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


def memory_snapshot(conn, require=False):
    """jl 旧 memory 表的不变量快照：行数 + SHOW CREATE 原文。"""
    if not table_exists(conn, "memory"):
        if require:
            die(
                "REFUSED: 目标库缺少 legacy memory 表；生产路径只允许新建 v2 表，"
                "拒绝继续，exit 2",
                2,
            )
        return {"present": False, "rows": None, "create_sql": None,
                "create_sha256": None}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM memory")
        rows = cur.fetchone()[0]
        cur.execute("SHOW CREATE TABLE memory")
        create_sql = cur.fetchone()[1]
    return {
        "present": True,
        "rows": rows,
        "create_sql": create_sql,
        "create_sha256": hashlib.sha256(create_sql.encode("utf-8")).hexdigest(),
    }


def memory_invariant(before, after):
    if before["rows"] != after["rows"]:
        return False, "memory 行数 %s -> %s" % (before["rows"], after["rows"])
    if before["create_sql"] != after["create_sql"]:
        return False, "memory SHOW CREATE 不再逐字节一致"
    return True, ""


def analyze_pending_safety(conn, migrations, applied, pending):
    """生产 apply 前置：已应用 checksum 必须一致；pending 只允许建新表。"""
    for migration in migrations:
        if migration.version in applied and applied[migration.version] != migration.checksum:
            die(
                "CHECKSUM MISMATCH %s：已应用=%s 文件=%s（迁移文件被改动过）"
                % (migration.version, applied[migration.version], migration.checksum)
            )
    created = []
    for migration in pending:
        statements = split_sql(migration.sql_text)
        if not statements:
            die("REFUSED: 迁移 %s 中没有可执行语句，exit 2" % migration.version, 2)
        for statement in statements:
            stripped = statement.strip()
            if re.match(r"^SET\s+NAMES\b", stripped, re.I):
                continue
            match = re.match(
                r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?([A-Za-z0-9_]+)`?",
                stripped, re.I,
            )
            if not match:
                die(
                    "REFUSED: 生产路径只允许 'CREATE TABLE IF NOT EXISTS' / 'SET NAMES'；"
                    "迁移 %s 含不安全语句，exit 2\n-- 语句：\n%s"
                    % (migration.version, stripped[:800]),
                    2,
                )
            table = match.group(1)
            if table_exists(conn, table):
                die(
                    "REFUSED: 待应用迁移要创建的表已存在：%s（IF NOT EXISTS 会静默跳过；"
                    "拒绝制造 schema_migrations 与实际结构不一致），exit 2" % table,
                    2,
                )
            created.append((migration.version, table))
    return created


def analyze_down_safety(migrations, applied):
    """生产 rollback 前置：down 只允许 DROP TABLE IF EXISTS <v2 表>。"""
    by_version = {migration.version: migration for migration in migrations}
    drops = []
    for version in sorted(applied, reverse=True):
        migration = by_version.get(version)
        if migration is None or not os.path.isfile(migration.down_path):
            die("生产回滚缺少 %s 的 down 文件，exit 2" % version, 2)
        statements = split_sql(io.open(migration.down_path, encoding="utf-8").read())
        if not statements:
            die("REFUSED: down 迁移 %s 中没有可执行语句，exit 2" % version, 2)
        for statement in statements:
            stripped = statement.strip()
            if re.match(r"^SET\s+NAMES\b", stripped, re.I):
                continue
            match = re.match(
                r"^DROP\s+TABLE\s+IF\s+EXISTS\s+`?([A-Za-z0-9_]+)`?\s*$",
                stripped, re.I,
            )
            if not match or match.group(1) not in V2_TABLES:
                die(
                    "REFUSED: 生产回滚只允许 DROP TABLE IF EXISTS <v2 表>；"
                    "迁移 %s 含不安全语句，exit 2\n-- 语句：\n%s"
                    % (version, stripped[:800]),
                    2,
                )
            drops.append((version, match.group(1)))
    return drops


# ------------------------------------------------------------------ plan
def print_prod_plan(mode, database, pending, created, backup, before):
    print("PROD PLAN (%s)" % mode)
    print("  database        : %s (production)" % database)
    print("  migrations      : %s" % (
        ", ".join(m.version for m in pending) if pending else "(none)"))
    print("  new tables      : %s" % (
        ", ".join(table for _version, table in created) if created else "(none)"))
    print("  backup          : %s" % backup["path"])
    print("  backup sha256   : %s" % backup["sha256"])
    print("  backup size     : %d bytes (gzip -t ok)" % backup["size"])
    print("  env guard       : %s=1 (process env)" % PROD_ENV_VAR)
    print("  invariants pre  : memory present=%s rows=%s create_sha256=%s" % (
        before["present"], before["rows"], before["create_sha256"]))
    print("  safety          : CREATE TABLE IF NOT EXISTS only; target tables absent; "
          "legacy memory table untouched")


def print_prod_rollback_plan(database, applied, drops, backup, before):
    print("PROD PLAN (rollback)")
    print("  database        : %s (production)" % database)
    print("  migrations      : %s" % ", ".join(sorted(applied, reverse=True)))
    print("  drops           : %s" % ", ".join(table for _version, table in drops))
    print("  backup          : %s" % backup["path"])
    print("  backup sha256   : %s" % backup["sha256"])
    print("  backup size     : %d bytes (gzip -t ok)" % backup["size"])
    print("  env guard       : %s=1 (process env)" % PROD_ENV_VAR)
    print("  invariants pre  : memory present=%s rows=%s create_sha256=%s" % (
        before["present"], before["rows"], before["create_sha256"]))
    print("  safety          : DROP TABLE IF EXISTS <v2 tables> only; "
          "legacy memory table untouched")


# ------------------------------------------------------------------ commands
def apply_migrations(conn, migrations):
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
    return applied_count


def rollback_migrations(conn, migrations):
    applied = read_applied(conn)
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
    return rolled


def cmd_apply(conn, migrations):
    applied_count = apply_migrations(conn, migrations)
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
    rolled = rollback_migrations(conn, migrations)
    print("ROLLBACK_OK (%d migration(s) rolled back)" % rolled)
    return 0


def cmd_prod_apply(conn, migrations, database, backup, dry_run):
    mode = "dry-run apply" if dry_run else "apply"
    applied = read_applied(conn)
    pending = [m for m in migrations if m.version not in applied]
    created = analyze_pending_safety(conn, migrations, applied, pending)
    before = memory_snapshot(conn, require=True)
    print_prod_plan(mode, database, pending, created, backup, before)
    if dry_run:
        print("PROD_DRY_RUN_OK (no DDL executed; %s unchanged)" % database)
        return 0

    print("PROD EXECUTE: applying %d migration(s) to %s ..." % (len(pending), database))
    applied_count = apply_migrations(conn, migrations)
    after = memory_snapshot(conn, require=True)
    ok, detail = memory_invariant(before, after)
    if not ok:
        die(
            "PROD INVARIANT FAIL: %s。立即从备份恢复：%s" % (detail, backup["path"]),
            1,
        )
    missing = [table for table in V2_TABLES if not table_exists(conn, table)]
    if missing:
        die("PROD APPLY FAIL: 建表后仍缺少 %s，从备份恢复：%s" % (
            ", ".join(missing), backup["path"]), 1)
    print(
        "PROD_APPLY_OK (%d migration(s) applied; memory rows=%s create_sha256=%s unchanged)"
        % (applied_count, after["rows"], after["create_sha256"])
    )
    return 0


def cmd_prod_rollback(conn, migrations, database, backup):
    applied = read_applied(conn)
    if not applied:
        print("ROLLBACK_NOOP (没有已应用的迁移)")
        print("PROD_ROLLBACK_OK (no-op)")
        return 0
    drops = analyze_down_safety(migrations, applied)
    before = memory_snapshot(conn, require=True)
    print_prod_rollback_plan(database, applied, drops, backup, before)
    print("PROD EXECUTE: rolling back %d migration(s) on %s ..." % (len(applied), database))
    rolled = rollback_migrations(conn, migrations)
    after = memory_snapshot(conn, require=True)
    ok, detail = memory_invariant(before, after)
    if not ok:
        die("PROD INVARIANT FAIL: %s。立即从备份恢复：%s" % (detail, backup["path"]), 1)
    left = [table for table in V2_TABLES if table_exists(conn, table)]
    if left:
        die("PROD ROLLBACK FAIL: v2 表仍存在 %s，从备份恢复：%s" % (
            ", ".join(left), backup["path"]), 1)
    print("PROD_ROLLBACK_OK (%d migration(s) rolled back; memory untouched)" % rolled)
    return 0


# ------------------------------------------------------------------ main
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1 记忆 v2 迁移运行器（默认 scratch 库；生产库走受控路径）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl_m1；'jl' 需 --allow-production + 通道门禁）")
    parser.add_argument("--rollback", action="store_true", help="按 down.sql 回滚已应用迁移")
    parser.add_argument("--check", action="store_true", help="只检查迁移状态，不写库")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的迁移，不写库")
    parser.add_argument("--allow-production", action="store_true",
                        help="显式放行生产库 jl（还需 JL_ALLOW_PROD_MIGRATION=1 与 --backup）")
    parser.add_argument("--backup", default=None,
                        help="生产备份文件（gzip；apply/rollback/dry-run 必填）")
    parser.add_argument("--env-file", default=None,
                        help=".env 路径（默认 <repo>/.env，可用 M1_ENV_FILE 覆盖）")
    parser.add_argument("--migrations-dir", default=None,
                        help="迁移目录（默认 <repo>/db/migrations）")
    args = parser.parse_args(argv)

    database = validate_database_name(args.database)
    prod = database.lower() == PROD_DATABASE
    if prod:
        if not args.allow_production:
            die(
                "REFUSED: 拒绝操作生产库 'jl'。默认只允许 scratch 库（默认 jl_m1）；"
                "生产迁移必须显式 --allow-production + JL_ALLOW_PROD_MIGRATION=1 + --backup，"
                "exit 2",
                2,
            )
        require_prod_env()
    elif args.allow_production:
        die(
            "REFUSED: --allow-production 只允许搭配 --database jl（当前 %s），exit 2"
            % database,
            2,
        )
    if args.backup and not prod:
        die("REFUSED: --backup 只用于生产路径（--database jl --allow-production），exit 2", 2)
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

    if args.check:
        conn = connect(env, database)
        try:
            if prod:
                print("PROD CHECK (read-only) database=%s" % database)
            return cmd_check(conn, migrations)
        finally:
            conn.close()

    if args.dry_run and not prod:
        return cmd_dry_run(env, database, migrations)

    backup = None
    if prod:
        # 备份门禁在建立连接 / 执行任何 DDL 之前完成
        backup = validate_backup(args.backup)

    conn = connect(env, database)
    try:
        if args.rollback:
            if prod:
                return cmd_prod_rollback(conn, migrations, database, backup)
            return cmd_rollback(conn, migrations)
        if prod:
            return cmd_prod_apply(conn, migrations, database, backup,
                                  dry_run=args.dry_run)
        return cmd_apply(conn, migrations)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
