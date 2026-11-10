-- 0003 记忆候选（Phase1）· up
-- 用途：全新环境（如 jl_m1 影子库 / 重建库）用迁移工具一次建好 memory_candidate。
--
-- 生产环境以 BFF 启动时的 ensure_memory_candidate_table() 为准（幂等 CREATE TABLE IF NOT EXISTS）：
--   · scripts/deploy.ps1 只传 bff/ scripts/ schemas/ tools/，不传 db/，线上也不许手改；
--   · 服务重启会再次 ensure，表不会丢；
--   · 因此线上不需要、也不应该再跑本文件（scripts/migrate.py 的生产路径会因"表已存在"拒绝）。
--
-- 语义硬规则：
--   pending 永远是候选：不进 memory、不渲染 records/rules.md、不进提示；
--   accepted 才写 memory（user/project_user 由本人确认，project 只能总监/owner/admin 确认）。

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS memory_candidate (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  project_id        VARCHAR(32) NOT NULL COMMENT '发起/归属项目（user 候选也记来源项目，便于项目内待确认列表）',
  user_id           VARCHAR(32) NOT NULL COMMENT '候选归属人（个人域）；project 候选是提交人',
  scope             ENUM('user','project_user','project') NOT NULL
                    COMMENT 'user=跨项目个人 / project_user=个人×本项目 / project=项目共享',
  text              VARCHAR(512) NOT NULL,
  evidence          TEXT NULL COMMENT '为什么提这条（用户原话/字段改动摘录）',
  source_task_id    BIGINT UNSIGNED NULL,
  source_message_id BIGINT UNSIGNED NULL,
  status            ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'pending'
                    COMMENT 'pending 永远不进 memory / 文件',
  decided_by        VARCHAR(32) NULL,
  decided_at        DATETIME(3) NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cand_pending (project_id, scope, status),
  KEY idx_cand_user (user_id, status),
  KEY idx_cand_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='记忆候选（pending 不生效；接受后才写 memory 并重渲染文件）';
