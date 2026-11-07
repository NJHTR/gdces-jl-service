# M1 · 记忆 v2 数据模型（schema only）

> 依据：`docs/MEMORY-V2.md`（2026-11-06 已拍板）+ `docs/BACKLOG.md` Wave 3.5 M1。
> 范围：**只建表 + 迁移运行器 + 结构校验**，在影子库 `jl_m1` 验证；不改旧 `memory` 表、
> 不加运行时读写路径、不开 feature flag、不部署。M2~M8 见 MEMORY-V2.md §11。

## 1. 表映射（MEMORY-V2.md §3 → DDL）

| 设计稿 | 表 | 职责 |
|---|---|---|
| `memory_item` | `memory_item` | 规则/事实引用的唯一权威行；承载 kind、scope、状态机、来源与证据 |
| `memory_variant (id, item_id, text, embedding_ref)` | `memory_variant` | 同一规则的不同说法；归一化/近义合并时保留旧说法（§4.3） |
| `memory_use (id, item_id, task_id, injected_at, rank, relevance, utility, outcome)` | `memory_use` | 每轮注入记账，U 的分子/分母来源（§5.4 / §6） |
| `memory_review (id, item_id, action, by, at, note)` | `memory_review` | 候选评审：accept / reject / merge（§4.1/§11） |
| `memory_audit (id, item_id, action, before_json, after_json, by, at)` | `memory_audit` | 变更审计可回放（§6）；`item_id` 可空、无外键，条目删除后审计保留 |
| 迁移元数据（本方案新增） | `schema_migrations` | version + sha256 checksum + applied_at |

## 2. 关键列与设计决定

### memory_item

| 列 | 类型 | 映射/理由 |
|---|---|---|
| `kind` | ENUM('rule','style','fact_ref') | P1 三分：只有 rule 进提示；style 人格防火墙（§7）；fact_ref 指向项目事实 |
| `scope` | ENUM('global','project','project_user','user') | P3 作用域显式，层级 global < project < project_user < user |
| `scope_id` | VARCHAR(64) NOT NULL | project_id / user_id / 组合键；**禁止 NULL=全局**（P3） |
| `project_id` | VARCHAR(32) NULL | 冗余列，支撑 `(project_id,status)` 与影子读比较 |
| `canonical_text` | VARCHAR(512) NOT NULL | 归一化后的唯一说法（§4.3 cosine≥0.86 / Jaccard≥0.7 合并入口） |
| `status` | ENUM('candidate','active','superseded','archived','expired') NOT NULL DEFAULT 'candidate' | P2 两阶段：自动抽取永远 candidate；**candidate 不进提示** |
| `confidence` | DECIMAL(4,3) NOT NULL DEFAULT 0.500 | D-记忆-09：只来自确认与结果，不来自出现次数（§4.4） |
| `created_by` | ENUM('user','model','import') | P4 来源必填；`import` 用于 M7 全量降级导入 |
| `confirmed_by` | VARCHAR(64) NULL | 显式记住/确认人；candidate 为 NULL |
| `source_task_id` / `source_message_id` | BIGINT UNSIGNED NULL | P4 来源必填 |
| `evidence` | VARCHAR(1024) NULL | 用户原话 / 编辑 diff 摘录 |
| `supersedes_id` | BIGINT UNSIGNED NULL | 状态机 superseded 指向新条目（§4.3） |
| `expires_at` | DATETIME NULL | candidate 默认 14 天、style 默认 90 天（D-记忆-09/10） |
| `created_at` / `updated_at` | DATETIME | 时间窗与衰减 D 的输入 |

索引：`(scope,scope_id,status)` 召回过滤；`(project_id,status)` 影子读/项目视图；
`(kind,status)` style 限额；`(expires_at)` 过期 job。

### 其它表

- `memory_variant.item_id`、`memory_use.item_id`、`memory_review.item_id`：
  FK → `memory_item(id)` **ON DELETE CASCADE**（条目删除时附属记录一并清理）。
- `memory_use`：`(item_id,injected_at)` 时间窗统计，`(task_id)` 按任务回放；`outcome` 默认 `unknown`。
- `memory_audit`：`item_id` 无 FK（审计独立保留）；`(item_id)`、`(at)` 两个索引。
- 全部 `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`，带表/列注释（与 `db/schema.sql` 风格一致）。

## 3. 状态机（MEMORY-V2.md §3）

```
candidate --确认/显式记住--> active --被新规则取代--> superseded
   │                          ├--低效用/超期/删除--> archived
   └--14 天未确认--> expired
```

M1 只落库结构与索引；状态迁移由 M2+ 写入闸门实现，本次不改任何 BFF 代码。

## 4. 迁移与回滚

| 文件 | 作用 |
|---|---|
| `db/migrations/0001_memory_v2.sql` | up：建 5 张 memory_* 表（IF NOT EXISTS） |
| `db/migrations/0001_memory_v2.down.sql` | down：按外键倒序 DROP 5 张表 |
| `scripts/migrate.py` | up/rollback/check/dry-run；`schema_migrations` 记录 sha256，重复执行 no-op |
| `scripts/check-memory-v2.py` | 只读校验表/列/ENUM/索引/FK/checksum，通过打印 `M1_OK` |

```bash
# 服务器 /tmp 测试副本上（--env-file 指向部署仓库 .env；不在服务器手改文件）
python3 scripts/migrate.py --database jl_m1            # apply（幂等）
python3 scripts/migrate.py --database jl_m1 --check    # 应打印 MIGRATIONS_OK
python3 scripts/migrate.py --database jl_m1 --rollback # 应打印 ROLLBACK_OK
python3 scripts/check-memory-v2.py --database jl_m1    # 应打印 M1_OK
```

**安全边界**：两个脚本对 `--database jl` 一律 exit 2；迁移只在 `jl_m1` 应用，
不触碰生产库 `jl` 与旧 `memory` 表。

## 5. 验收（M1 DoD）

1. `jl_m1` 首次 apply → `MIGRATE_OK (1 migration(s) applied)`；
2. 再次 apply → `MIGRATE_OK (no-op: all migrations already applied)`；
3. `--check` → `MIGRATIONS_OK`；`check-memory-v2.py` → `M1_OK`；
4. `--rollback` → 5 张 memory_* 表消失、`schema_migrations` 行删除；
5. 重新 apply + 校验 → 再回绿；`jl` 中不存在 `memory_item`（未被碰）。

## 6. 不在本项范围

写入闸门（M2）、读取/重排（M3）、反馈衰减（M4）、style 防火墙（M5）、
引擎记忆处置（M6）、38 条数据迁移与评测（M7）、测试污染清理（M8）。
本项不接 BFF、不改 `docs/state/loop.json` / `docs/BACKLOG.md` / 回执 / 部署脚本。
