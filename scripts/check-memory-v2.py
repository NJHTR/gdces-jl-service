#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 记忆 v2 结构校验（只读，默认库 jl_m1）。

断言：
  · 6 张表存在：memory_item / memory_variant / memory_use / memory_review /
    memory_audit / schema_migrations，且 InnoDB + utf8mb4；
  · 关键列的类型（含 ENUM 取值）、NOT NULL/默认值；
  · 关键索引（列顺序）与 3 条外键（ON DELETE CASCADE）；
  · 0001_memory_v2 已应用，且库里的 checksum 与本仓库迁移文件一致。
全部通过打印 M1_OK；任何一项失败打印 M1_FAIL 并 exit 1。
硬拒绝生产库 jl：--database jl 直接 exit 2。
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
DEFAULT_DATABASE = "jl_m1"
PROD_DATABASE = "jl"
DB_HOST = "127.0.0.1"
DB_PORT = 3309
DB_USER = "jl"
PROD_ENV_VAR = "JL_ALLOW_PROD_MIGRATION"
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")

V2_TABLES = [
    "memory_item",
    "memory_variant",
    "memory_use",
    "memory_review",
    "memory_audit",
]
ALL_TABLES = V2_TABLES + ["schema_migrations"]

EXPECTED_COLUMNS = {
    "memory_item": {
        "id": "bigint unsigned",
        "kind": "enum('rule','style','fact_ref')",
        "scope": "enum('global','project','project_user','user')",
        "scope_id": "varchar(64)",
        "project_id": "varchar(32)",
        "canonical_text": "varchar(512)",
        "status": "enum('candidate','active','superseded','archived','expired')",
        "confidence": "decimal(4,3)",
        "created_by": "enum('user','model','import')",
        "confirmed_by": "varchar(64)",
        "source_task_id": "bigint unsigned",
        "source_message_id": "bigint unsigned",
        "evidence": "varchar(1024)",
        "supersedes_id": "bigint unsigned",
        "expires_at": "datetime",
        "created_at": "datetime",
        "updated_at": "datetime",
    },
    "memory_variant": {
        "id": "bigint unsigned",
        "item_id": "bigint unsigned",
        "text": "varchar(1024)",
        "embedding_ref": "varchar(255)",
        "created_at": "datetime",
    },
    "memory_use": {
        "id": "bigint unsigned",
        "item_id": "bigint unsigned",
        "task_id": "bigint unsigned",
        "injected_at": "datetime",
        "rank": "int",
        "relevance": "decimal(4,3)",
        "utility": "decimal(4,3)",
        "outcome": "enum('helpful','harmful','ignored','unknown')",
        "created_at": "datetime",
    },
    "memory_review": {
        "id": "bigint unsigned",
        "item_id": "bigint unsigned",
        "action": "enum('accept','reject','merge')",
        "by": "varchar(64)",
        "at": "datetime",
        "note": "varchar(512)",
    },
    "memory_audit": {
        "id": "bigint unsigned",
        "item_id": "bigint unsigned",
        "action": "varchar(32)",
        "before_json": "json",
        "after_json": "json",
        "by": "varchar(64)",
        "at": "datetime",
    },
    "schema_migrations": {
        "version": "varchar(64)",
        "checksum": "char(64)",
        "applied_at": "datetime",
    },
}

EXPECTED_INDEXES = {
    "memory_item": [
        ("id",),
        ("scope", "scope_id", "status"),
        ("project_id", "status"),
        ("kind", "status"),
        ("expires_at",),
    ],
    "memory_variant": [("id",), ("item_id",)],
    "memory_use": [("id",), ("item_id", "injected_at"), ("task_id",)],
    "memory_review": [("id",), ("item_id",)],
    "memory_audit": [("id",), ("item_id",), ("at",)],
    "schema_migrations": [("version",)],
}

EXPECTED_FKS = {
    ("memory_variant", "item_id", "memory_item", "id", "CASCADE"),
    ("memory_use", "item_id", "memory_item", "id", "CASCADE"),
    ("memory_review", "item_id", "memory_item", "id", "CASCADE"),
}

EXPECTED_DEFAULTS = {
    ("memory_item", "status"): "candidate",
    ("memory_item", "confidence"): "0.500",
    ("memory_use", "outcome"): "unknown",
}


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
        return pymysql.connect(**kwargs)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else None
        if code == 1049:
            die("数据库 %s 不存在（先运行 scripts/migrate.py）" % database)
        if code in (1044, 1045, 2003):
            die("无法连接 MySQL %s:%s（权限/服务/密码错误，code=%s）" % (
                kwargs["host"], kwargs["port"], code))
        raise


def guard_database(name, allow_production=False):
    name = (name or "").strip()
    if not IDENT_RE.match(name):
        die("非法数据库名（只允许字母/数字/下划线）：%r，exit 2" % name, 2)
    if name.lower() == PROD_DATABASE:
        if not allow_production:
            die("REFUSED: 拒绝检查生产库 'jl'。默认只允许 scratch 库（默认 jl_m1）；"
                "生产结构检查需 --allow-production + %s=1，exit 2" % PROD_ENV_VAR, 2)
        if os.environ.get(PROD_ENV_VAR) != "1":
            die("REFUSED: 生产结构检查需要进程环境 %s=1（export，不是 .env），exit 2"
                % PROD_ENV_VAR, 2)
    elif allow_production:
        die("REFUSED: --allow-production 只允许搭配 --database jl（当前 %s），exit 2"
            % name, 2)
    return name


class Checker(object):
    def __init__(self, conn):
        self.conn = conn
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    def run(self):
        self.check_tables()
        self.check_columns()
        self.check_defaults()
        self.check_indexes()
        self.check_foreign_keys()
        self.check_migration_row()
        ok_all = True
        for name, ok, detail in self.checks:
            if ok:
                print("PASS  " + name)
            else:
                suffix = ("  -- " + detail) if detail else ""
                print("FAIL  " + name + suffix)
                ok_all = False
        print("M1_OK" if ok_all else "M1_FAIL")
        return 0 if ok_all else 1

    # ---------------------------------------------------------------- tables
    def table_meta(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, engine, table_collation, table_comment "
                "FROM information_schema.tables WHERE table_schema = DATABASE()",
            )
            return {row[0]: row for row in cur.fetchall()}

    def check_tables(self):
        meta = self.table_meta()
        missing = [t for t in ALL_TABLES if t not in meta]
        self.check(
            "6 张表存在（5 张 memory_* + schema_migrations）",
            not missing,
            "缺少 " + ", ".join(missing) if missing else "",
        )
        for table in V2_TABLES:
            row = meta.get(table)
            if row is None:
                continue
            _name, engine, collation, comment = row
            self.check(
                "%s 引擎=InnoDB" % table, engine == "InnoDB", "实际 %s" % engine)
            self.check(
                "%s 字符集=utf8mb4" % table,
                (collation or "").startswith("utf8mb4"),
                "实际 %s" % collation,
            )
            self.check(
                "%s 有表注释" % table, bool(comment), "table_comment 为空")

    # ---------------------------------------------------------------- columns
    def check_columns(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, column_type, is_nullable, "
                "column_default FROM information_schema.columns "
                "WHERE table_schema = DATABASE()",
            )
            actual = {}
            for table, column, ctype, nullable, default in cur.fetchall():
                actual.setdefault(table, {})[column] = (ctype, nullable, default)
        for table, columns in EXPECTED_COLUMNS.items():
            got = actual.get(table, {})
            for column, expected_type in columns.items():
                if column not in got:
                    self.check("%s.%s 存在" % (table, column), False, "列缺失")
                    continue
                real_type = got[column][0]
                self.check(
                    "%s.%s 类型 = %s" % (table, column, expected_type),
                    real_type == expected_type,
                    "实际 %s" % real_type,
                )
            if table == "memory_item":
                for column in ("kind", "scope", "scope_id", "canonical_text",
                               "status", "confidence", "created_by"):
                    if column in got:
                        self.check(
                            "memory_item.%s NOT NULL" % column,
                            got[column][1] == "NO",
                            "实际 IS_NULLABLE=%s" % got[column][1],
                        )
                for column in ("project_id", "confirmed_by", "source_task_id",
                               "source_message_id", "evidence", "supersedes_id",
                               "expires_at"):
                    if column in got:
                        self.check(
                            "memory_item.%s NULL 可空（非 global 兜底）" % column,
                            got[column][1] == "YES",
                            "实际 IS_NULLABLE=%s" % got[column][1],
                        )
            if table == "memory_audit" and "item_id" in got:
                self.check(
                    "memory_audit.item_id 可空（审计不随条目删除）",
                    got["item_id"][1] == "YES",
                    "实际 IS_NULLABLE=%s" % got["item_id"][1],
                )

    def check_defaults(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, column_default "
                "FROM information_schema.columns WHERE table_schema = DATABASE()",
            )
            defaults = {(row[0], row[1]): row[2] for row in cur.fetchall()}
        for key, expected in EXPECTED_DEFAULTS.items():
            table, column = key
            actual = defaults.get(key)
            self.check(
                "%s.%s 默认值 = %s" % (table, column, expected),
                actual == expected,
                "实际 %r" % (actual,),
            )

    # ---------------------------------------------------------------- indexes
    def check_indexes(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, index_name, seq_in_index, column_name "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() ORDER BY table_name, index_name, seq_in_index",
            )
            grouped = {}
            for table, index_name, seq, column in cur.fetchall():
                grouped.setdefault(table, {}).setdefault(index_name, []).append(
                    (int(seq), column))
        for table, required in EXPECTED_INDEXES.items():
            indexes = grouped.get(table, {})
            built = {tuple(col for _seq, col in sorted(cols))
                     for cols in indexes.values()}
            for columns in required:
                self.check(
                    "%s 索引 (%s)" % (table, ", ".join(columns)),
                    columns in built,
                    "现有 " + repr(sorted(built)),
                )

    # ---------------------------------------------------------------- FKs
    def check_foreign_keys(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT k.table_name, k.column_name, k.referenced_table_name, "
                "k.referenced_column_name, r.delete_rule "
                "FROM information_schema.key_column_usage k "
                "JOIN information_schema.referential_constraints r "
                "  ON r.constraint_schema = k.constraint_schema "
                " AND r.constraint_name = k.constraint_name "
                "WHERE k.table_schema = DATABASE() "
                "  AND k.referenced_table_name IS NOT NULL",
            )
            actual = {(t, c, rt, rc, rule.upper())
                      for t, c, rt, rc, rule in cur.fetchall()}
        self.check(
            "外键 = 3 条且都指向 memory_item(id) ON DELETE CASCADE",
            actual == EXPECTED_FKS,
            "实际 " + repr(sorted(actual)) if actual != EXPECTED_FKS else "",
        )

    # ---------------------------------------------------------------- migration
    def check_migration_row(self):
        version = "0001_memory_v2"
        path = os.path.join(ROOT, "db", "migrations", version + ".sql")
        if not os.path.isfile(path):
            self.check("迁移文件存在：db/migrations/%s.sql" % version, False,
                       path)
            return
        digest = hashlib.sha256()
        with io.open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        expected = digest.hexdigest()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'schema_migrations'",
            )
            if cur.fetchone()[0] == 0:
                self.check("%s 已应用（schema_migrations 有记录）" % version,
                           False, "schema_migrations 不存在")
                return
            cur.execute(
                "SELECT checksum FROM schema_migrations WHERE version = %s",
                (version,),
            )
            row = cur.fetchone()
        self.check(
            "%s 已应用且 checksum 与仓库文件一致" % version,
            row is not None and row[0] == expected,
            "库中 %r / 文件 %s" % (row[0] if row else None, expected),
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="M1 记忆 v2 结构校验（只读，默认 jl_m1）")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help="目标库（默认 jl_m1；'jl' 需 --allow-production + 环境门禁）")
    parser.add_argument("--allow-production", action="store_true",
                        help="显式放行生产库 jl（还需 JL_ALLOW_PROD_MIGRATION=1）")
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

    print("database=%s host=%s:%s user=%s tables=6" % (
        database, DB_HOST, DB_PORT, DB_USER))
    conn = connect(env, database)
    try:
        return Checker(conn).run()
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
