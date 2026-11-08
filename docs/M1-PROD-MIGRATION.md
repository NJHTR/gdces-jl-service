# M1 · 生产迁移受控路径（runbook）

> 关联：`docs/M1-DESIGN.md`（表设计）、`db/migrations/0001_memory_v2.*`。
> 本文件只描述**受控路径**。M1 本身不部署、不在本仓库执行生产 DDL；
> 生产 apply 由 Lead 在独立验证后按本 runbook 执行一次。

## 1. 默认安全 vs 受控生产

| 场景 | 命令 | 行为 |
|---|---|---|
| scratch（默认） | `migrate.py` / `--check` / `--rollback` / `--dry-run` | 只操作 `jl_m1` |
| 误指生产 | `migrate.py --database jl` | **exit 2**（默认拒绝） |
| 生产只读检查 | `migrate.py --database jl --allow-production --check` | 只读，需环境门禁 |
| 生产 apply | `migrate.py --database jl --allow-production --backup <tgz>` | 全部门禁通过才执行 |
| 生产 rollback | 同上 + `--rollback` | 只 DROP 5 张 v2 表 |

生产路径需要**同时**满足：

1. `--allow-production`（显式意图；只能搭配 `--database jl`，配 jl_m1 会 exit 2）；
2. 进程环境 `JL_ALLOW_PROD_MIGRATION=1`（`export`，**不读 .env**）；
3. `--backup <path>`：存在、非空、`gzip -t` 通过；输出备份绝对路径、sha256、字节数；
4. pending SQL 只允许以下三类，其它一律 exit 2：
   - `CREATE TABLE IF NOT EXISTS`：目标表在库中必须**不存在**（拒绝“IF NOT EXISTS 静默跳过”
     造成的状态不一致）；
   - `SET NAMES`；
   - 对 **`memory_item`** 的窄 ALTER（M1b-1b，对应 0002）：仅允许
     `ADD COLUMN legacy_memory_id BIGINT UNSIGNED NULL [COMMENT ...]` 与
     `ADD UNIQUE KEY uk_legacy_memory (legacy_memory_id)`；目标表必须是 `memory_item`
     且已存在；列/键已存在或同一计划内重复 → exit 2；任何其它 ALTER
     （`DROP`/`MODIFY`/`ADD` 其它列/其它表/legacy `memory`）→ exit 2；
     含 MySQL 可执行注释 `/*! ... */` → exit 2（可能绕过 allowlist）。
     允许的 ALTER 会在 `PROD PLAN` 的 `narrow ALTER` 行逐条打印。
5. 执行前后 `memory` 不变量：行数不变、`SHOW CREATE TABLE memory` 逐字节一致；
6. 备份/门禁校验在**任何 DDL 之前**完成；备份缺失/损坏直接 exit 2。

当前生产 `jl` 状态：0001 已应用、v2 空、legacy memory 39 行；pending 只有
`0002_memory_v2_legacy_map`（上述窄 ALTER）。`--dry-run` 应打印
`narrow ALTER : memory_item (ADD COLUMN legacy_memory_id, ADD UNIQUE KEY uk_legacy_memory)`
并输出 `PROD_DRY_RUN_OK`，**不执行 DDL**。真正 apply 的命令与 0001 相同
（migrations 目录含 0001+0002，0001 会 SKIP）。

`migrate.py --check` 与 `check-prod-migration.py` 都是只读（后者额外
`SET SESSION transaction_read_only=1`），不需要备份；`--dry-run` 需要备份并打印完整
`PROD PLAN`，但**不执行任何 DDL**。

退出码：`0` 成功；`1` 状态/校验失败（含不变量失败）；`2` 安全门禁拒绝。

## 2. 备份路径约定

沿用 `scripts/backup.sh` 的约定（`/home/baize/jl-service/backups/`，mysqldump + gzip）：

```bash
# 在服务器上（只读导出，--single-transaction 不锁业务写）
cd /home/baize/jl-service
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p backups
docker exec jl-mysql mysqldump -uroot -p"$(grep '^MYSQL_ROOT_PASSWORD=' .env | cut -d= -f2-)" \
  --default-character-set=utf8mb4 --single-transaction --routines jl \
  | gzip > "backups/db-pre-m1-$STAMP.sql.gz"
ls -l "backups/db-pre-m1-$STAMP.sql.gz"
```

路径约定：`/home/baize/jl-service/backups/db-pre-m1-YYYYMMDD-HHMM.sql.gz`。
恢复方式：`zcat db-pre-m1-*.sql.gz | docker exec -i jl-mysql mysql -uroot -p*** jl`。

## 3. 执行步骤（Lead 用；脚本放 /tmp，**不部署**）

```bash
# 0) 只读代码副本（本地 -> 服务器 /tmp；不动服务器仓库/deploy）
ssh rokid-r640 'rm -rf /tmp/m1-prod && mkdir -p /tmp/m1-prod/scripts /tmp/m1-prod/db/migrations'
scp scripts/migrate.py scripts/check-prod-migration.py scripts/check-memory-v2.py \
    rokid-r640:/tmp/m1-prod/scripts/
scp db/migrations/0001_memory_v2.sql db/migrations/0001_memory_v2.down.sql \
    rokid-r640:/tmp/m1-prod/db/migrations/
ENV=/home/baize/jl-service/.env
BK=/home/baize/jl-service/backups/db-pre-m1-<STAMP>.sql.gz   # §2 生成

# 1) pre 基线（只读；记录 memory 行数 + SHOW CREATE sha256）
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/check-prod-migration.py \
  --env-file $ENV --database jl --allow-production --phase pre \
  --write-baseline /tmp/m1-prod-baseline.json"

# 2) dry-run（打印 PROD PLAN；不执行 DDL）
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/migrate.py \
  --env-file $ENV --database jl --allow-production --backup $BK --dry-run"

# 3) 正式 apply（唯一一次写操作）
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/migrate.py \
  --env-file $ENV --database jl --allow-production --backup $BK"

# 4) 事后独立校验（只读）
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/migrate.py \
  --env-file $ENV --database jl --allow-production --check"
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/check-prod-migration.py \
  --env-file $ENV --database jl --allow-production --phase post \
  --baseline /tmp/m1-prod-baseline.json"
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/check-memory-v2.py \
  --env-file $ENV --database jl --allow-production"
```

期望：pre/post 均打印 `PROD_CHECK_OK`；apply 打印 `PROD_APPLY_OK`
（含 `memory rows=... create_sha256=... unchanged`）；`--check` 打印 `MIGRATIONS_OK`；
`check-memory-v2.py` 打印 `M1_OK`。

## 4. 回滚（同样受控）

```bash
ssh rokid-r640 "JL_ALLOW_PROD_MIGRATION=1 python3 /tmp/m1-prod/scripts/migrate.py \
  --env-file $ENV --database jl --allow-production --backup $BK --rollback"
```

仅执行 `0001_memory_v2.down.sql`（DROP 5 张 v2 表），删除 `schema_migrations` 行；
`memory` 表不变量再次校验。期望 `PROD_ROLLBACK_OK`。

## 5. 明确不做

- 不在本仓库/本轮执行生产 DDL（Lead 独立验证后按 §3 执行）；
- 不修改 `bff/*`、迁移 SQL、BACKLOG、`loop.json`、回执、部署脚本；
- 不删除/改写 legacy `memory` 表；v2 与旧表并存，可回滚。
