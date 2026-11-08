#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1b-1 · legacy `memory` → v2 `memory_item` 幂等回填（只读 legacy 表）。

映射（见 docs/M1B-1.md）：
  kind='rule', status='candidate', created_by='import', confidence=0.5,
  canonical_text = memory.text, evidence = memory.source_ref,
  expires_at = NOW() + 14 天, legacy_memory_id = memory.id

  scope 映射：
    memory.scope='project'                          -> scope='project',
                                                       scope_id=owner_id, project_id=owner_id
    memory.scope='user' AND project_id IS NOT NULL  -> scope='project_user',
                                                       scope_id=owner_id(user_id), project_id=project_id
    memory.scope='user' AND project_id IS NULL      -> scope='user',
                                                       scope_id=owner_id(user_id), project_id=NULL

幂等：UNIQUE KEY uk_legacy_memory(legacy_memory_id) + LEFT JOIN 过滤已映射行；
      重复 --execute 插入 0。绝不 UPDATE / DELETE legacy `memory` 表。

安全：默认拒绝生产库 jl；--database jl 需 --allow-production + 进程环境
      JL_ALLOW_PROD_MIGRATION=1（--execute 还需有效 gzip --backup）。
      默认 database=jl_m2（生产 jl 的临时克隆，测试用）。

用法（服务器 /tmp 副本）：
    python3 scripts/backfill-memory-v2.py --env-file /path/.env --database jl_m2 --dry-run
    python3 scripts/backfill-memory-v2.py --env-file /path/.env --database jl_m2 --execute
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
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

LEGACY_TABLE = "memory"
V2_TABLE = "memory_item"
EXPIRE_DAYS = 14

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

INSERT_SQL = """
INSERT INTO memory_item
  (kind, scope, scope_id, project_id, canonical_text, status, confidence,
   created_by, evidence, expires_at, legacy_memory_id)
SELECT
  'rule',
  {scope_expr},
  m.owner_id,
  {project_expr},
  m.text,
  'candidate',
  0.500,
  'import',
  m.source_ref,
  DATE_ADD(NOW(), INTERVAL {days} DAY),
  m.id
FROM memory m
LEFT JOIN memory_item mi ON mi.legacy_memory_id = m.id
WHERE mi.id IS NULL
ORDER BY m.id
""".format(scope_expr=SCOPE_EXPR, project_expr=PROJECT_EXPR, days=EXPIRE_DAYS)

SCOPE_COUNTS_SQL = """
SELECT {scope_expr} AS mapped_scope, COUNT(*) AS n
FROM memory m
GROUP BY mapped_scope
ORDER BY mapped_scope
""".format(scope_expr=SCOPE_EXPR)


def die(message, code=1):
    print("FATAL: " + message, file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ env
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
        from urllib.parse import unquote, urlsplit
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
    if os.environ.get(PROD_ENV_VAR) != "1":
        die("REFUSED: 生产路径需要进程环境 %s=1（export，不是 .env），exit 2"
            % PROD_ENV_VAR, 2)


def validate_backup(path):
    """生产 execute 的备份门禁：存在、非空、gzip -t 通过；输出 sha256/size。"""
    if not path:
        die("REFUSED: 生产 execute 必须提供 --backup <gzip 备份文件>，exit 2", 2)
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        die("REFUSED: --backup 文件不存在：%s，exit 2" % abs_path, 2)
    size = os.path.getsize(abs_path)
    if size <= 0:
        die("REFUSED: --backup 文件为空：%s，exit 2" % abs_path, 2)
    try:
        checked = subprocess.run(["gzip", "-t", abs_path],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        die("REFUSED: 找不到 gzip 命令，无法校验备份，exit 2", 2)
    if checked.returncode != 0:
        die("REFUSED: --backup 不是有效 gzip：%s（gzip -t 失败），exit 2"
            % abs_path, 2)
    digest = hashlib.sha256()
    with io.open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return {"path": abs_path, "sha256": digest.hexdigest(), "size": size}


# ------------------------------------------------------------------ db
def connect(env, database):
    kwargs = connection_kwargs(env, database)
    try:
        return pymysql.connect(**kwargs)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if code == 1049:
            die("数据库 %s 不存在（先克隆 jl -> jl_m2）" % database)
        if code in (1044, 1045, 2003):
            die("无法连接 MySQL %s:%s（权限/服务/密码错误，code=%s）"
                % (kwargs["host"], kwargs["port"], code))
        raise


def table_exists(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s", (name,))
        return cur.fetchone()[0] > 0


def column_exists(conn, table, column):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "  AND column_name = %s", (table, column))
        return cur.fetchone()[0] > 0


def scalar(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def legacy_fingerprint(conn):
    """legacy memory 全表指纹（行数 + 关键字段 sha256），用于证明只读。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, scope, owner_id, project_id, text, source_ref, status, "
            "       hits, created_at, updated_at FROM memory ORDER BY id")
        rows = cur.fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(("\x1f".join("" if v is None else str(v) for v in row)
                       + "\x1e").encode("utf-8"))
    return {"rows": len(rows), "sha256": digest.hexdigest()}


def scope_counts(conn):
    with conn.cursor() as cur:
        cur.execute(SCOPE_COUNTS_SQL)
        return {row[0]: int(row[1]) for row in cur.fetchall() if row[0] is not None}


def preflight(conn):
    if not table_exists(conn, LEGACY_TABLE):
        die("目标库缺少 legacy memory 表，无法回填", 1)
    if not table_exists(conn, V2_TABLE):
        die("目标库缺少 memory_item 表（先应用 0001_memory_v2）", 1)
    if not column_exists(conn, V2_TABLE, "legacy_memory_id"):
        die("memory_item 缺少 legacy_memory_id 列（先应用 0002_memory_v2_legacy_map）", 1)
    unknown = scalar(
        conn, "SELECT COUNT(*) FROM memory WHERE scope NOT IN ('project','user')")
    if unknown:
        die("legacy memory 含未知 scope 的行 %d 条，拒绝回填" % unknown, 1)


def insert_stats(conn):
    legacy = scalar(conn, "SELECT COUNT(*) FROM memory")
    mapped = scalar(conn, "SELECT COUNT(*) FROM memory_item "
                          "WHERE legacy_memory_id IS NOT NULL")
    missing = scalar(
        conn,
        "SELECT COUNT(*) FROM memory m "
        "LEFT JOIN memory_item mi ON mi.legacy_memory_id = m.id "
        "WHERE mi.id IS NULL")
    return {"legacy": int(legacy), "mapped": int(mapped), "missing": int(missing)}


def print_plan(database, mode, stats, scopes, fingerprint):
    print("BACKFILL PLAN (%s)" % mode)
    print("  database            : %s" % database)
    print("  legacy table        : memory (%d rows, read-only)" % fingerprint["rows"])
    print("  legacy fingerprint  : %s" % fingerprint["sha256"])
    print("  v2 table            : memory_item")
    print("  already mapped      : %d" % stats["mapped"])
    print("  would insert        : %d" % stats["missing"])
    print("  mapping             : kind='rule' status='candidate' "
          "created_by='import' confidence=0.5 expires_at=NOW()+%d days" % EXPIRE_DAYS)
    print("  scope mapping       : project -> project(scope_id=owner_id, project_id=owner_id); "
          "user+project_id -> project_user; user -> user(project_id=NULL)")
    print("  scope counts (legacy): %s" % (
        ", ".join("%s=%d" % (k, v) for k, v in sorted(scopes.items())) or "(none)"))
    print("  writes legacy memory: NO (only SELECT)")


# ------------------------------------------------------------------ main
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1b-1 legacy memory -> memory_item 幂等回填（默认 jl_m2，只读 legacy）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl_m2；'jl' 需 --allow-production + 环境门禁）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印映射计数，不写库（默认行为）")
    parser.add_argument("--execute", action="store_true",
                        help="真正写入 memory_item（幂等，重复执行插入 0）")
    parser.add_argument("--allow-production", action="store_true",
                        help="显式放行生产库 jl（还需 JL_ALLOW_PROD_MIGRATION=1）")
    parser.add_argument("--backup", default=None,
                        help="生产 execute 必填：gzip 备份文件")
    parser.add_argument("--env-file", default=None,
                        help=".env 路径（默认 <repo>/.env，可用 M1_ENV_FILE 覆盖）")
    args = parser.parse_args(argv)

    database = validate_database_name(args.database)
    prod = database.lower() == PROD_DATABASE
    if prod:
        if not args.allow_production:
            die("REFUSED: 拒绝回填生产库 'jl'。默认只允许克隆库（默认 jl_m2）；"
                "生产回填需 --allow-production + %s=1（+ execute 时 --backup），exit 2"
                % PROD_ENV_VAR, 2)
        require_prod_env()
    elif args.allow_production:
        die("REFUSED: --allow-production 只允许搭配 --database jl（当前 %s），exit 2"
            % database, 2)
    if args.backup and not prod:
        die("REFUSED: --backup 只用于生产路径（--database jl --allow-production），exit 2", 2)
    if args.dry_run and args.execute:
        die("--dry-run 与 --execute 互斥，exit 2", 2)

    execute = bool(args.execute)
    mode = "execute" if execute else "dry-run"
    backup = None
    if prod and execute:
        backup = validate_backup(args.backup)  # 连接/写入前完成门禁
    elif prod:
        backup = {"path": None, "sha256": None, "size": None}

    env_file = args.env_file or os.environ.get("M1_ENV_FILE") or os.path.join(ROOT, ".env")
    if not os.path.isfile(env_file):
        die("找不到 .env：%s（可用 --env-file 指定）" % env_file)
    env = load_env_file(env_file)
    if not env.get("MYSQL_PASSWORD") and not env.get("MYSQL_DSN"):
        die(".env 中缺少 MYSQL_PASSWORD / MYSQL_DSN：%s" % env_file)

    print("database=%s mode=%s env_file=%s" % (
        database, mode, os.path.basename(env_file)))
    conn = connect(env, database)
    try:
        preflight(conn)
        before_fp = legacy_fingerprint(conn)
        stats = insert_stats(conn)
        scopes = scope_counts(conn)
        if backup and backup.get("path"):
            print("  backup              : %s" % backup["path"])
            print("  backup sha256       : %s" % backup["sha256"])
            print("  backup size         : %d bytes (gzip -t ok)" % backup["size"])
        print_plan(database, mode, stats, scopes, before_fp)

        if not execute:
            print("BACKFILL_DRY_RUN_OK (no writes; would insert=%d)" % stats["missing"])
            return 0

        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(INSERT_SQL)
                inserted = cur.rowcount
            after_fp = legacy_fingerprint(conn)
            if after_fp != before_fp:
                raise RuntimeError(
                    "legacy memory 指纹变化（行数/内容），已回滚：before=%r after=%r"
                    % (before_fp, after_fp))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        final = insert_stats(conn)
        if final["missing"] != 0:
            die("回填后仍有 %d 条 legacy 行未映射" % final["missing"], 1)
        print("BACKFILL_OK (inserted=%d, already_mapped=%d, legacy_rows=%d unchanged, "
              "re-run would insert 0)" % (
                  inserted, stats["mapped"], final["legacy"]))
        return 0
    except pymysql.err.MySQLError as exc:
        die("数据库错误：%s" % exc)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
