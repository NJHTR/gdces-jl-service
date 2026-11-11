-- 0005 执行 trace 事件账本 + task 补列（最终态）· up
-- 生产以 BFF 启动时 trace.ensure_trace_schema() 为准（部署不传 db/）；本文件供全新环境。
-- 注意：task.status 仍保持旧 ENUM('queued','running','done','failed','cancelled')，
--       新契约的 run.status=succeeded/partial 在投影层映射，不改旧字段（旧客户端不变）。
-- task.event_seq / task.trace_version 两列由运行时 ensure 幂等补（MySQL 8 无 ADD COLUMN
-- IF NOT EXISTS，且 scripts/migrate.py 的生产 allowlist 不允许 ALTER；schema.sql 已含这两列）。
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS task_event (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id        BIGINT UNSIGNED NOT NULL,
  seq            BIGINT UNSIGNED NOT NULL,
  parent_seq     BIGINT UNSIGNED NULL,
  step_id        VARCHAR(40) NULL,
  kind           ENUM('plan','tool','file_read','file_write','image_read','subagent',
                      'artifact','validation','note','error','status') NOT NULL,
  phase          VARCHAR(16) NULL,
  name           VARCHAR(128) NOT NULL,
  status         ENUM('pending','running','succeeded','failed','retrying','skipped',
                      'cancelled') NOT NULL DEFAULT 'pending',
  summary        VARCHAR(512) NULL,
  input_preview  TEXT NULL,
  output_preview TEXT NULL,
  output_ref     VARCHAR(255) NULL,
  output_bytes   BIGINT NULL,
  truncated      TINYINT NOT NULL DEFAULT 0,
  error_detail   TEXT NULL,
  visibility     ENUM('project','private','admin') NOT NULL DEFAULT 'project',
  attempt        INT NOT NULL DEFAULT 1,
  retry_of_seq   BIGINT UNSIGNED NULL,
  started_at     DATETIME(3) NULL,
  ended_at       DATETIME(3) NULL,
  meta           JSON NULL COMMENT '内部扩展：旧 trace 投影用 tool/icon/args',
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_seq (task_id, seq),
  KEY idx_task_status (task_id, status),
  KEY idx_task_kind (task_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='执行 trace 事件账本（append-only）';
