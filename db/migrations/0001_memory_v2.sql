-- M1 记忆 v2 · 基础表（up）
-- 依据 docs/MEMORY-V2.md §3 / docs/M1-DESIGN.md；只建 v2 新表，不动旧 memory 表。
-- 目标库：影子库 jl_m1（utf8mb4 / InnoDB）。状态机：
--   candidate --确认/显式记住--> active --被取代--> superseded
--   candidate --14 天未确认--> expired；active --低效用/超期/删除--> archived
-- 硬规则：candidate 永远不进提示。

SET NAMES utf8mb4;

-- ---------------------------------------------------------------- 记忆主表
CREATE TABLE IF NOT EXISTS memory_item (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  kind              ENUM('rule','style','fact_ref') NOT NULL
                    COMMENT 'rule=业务规矩；style=交互偏好；fact_ref=指向项目事实',
  scope             ENUM('global','project','project_user','user') NOT NULL
                    COMMENT '层级：global < project < project_user < user',
  scope_id          VARCHAR(64) NOT NULL
                    COMMENT 'project_id / user_id / 组合键；禁止 NULL=全局（P3）',
  project_id        VARCHAR(32) NULL COMMENT '项目定位冗余列，便于按项目过滤',
  canonical_text    VARCHAR(512) NOT NULL COMMENT '归一化后的唯一说法',
  status            ENUM('candidate','active','superseded','archived','expired')
                    NOT NULL DEFAULT 'candidate' COMMENT 'candidate 永远不进提示',
  confidence        DECIMAL(4,3) NOT NULL DEFAULT 0.500
                    COMMENT '只来自确认与结果，不来自出现次数（D-记忆-09）',
  created_by        ENUM('user','model','import') NOT NULL COMMENT '写入来源',
  confirmed_by      VARCHAR(64) NULL COMMENT '确认人 user_id / system',
  source_task_id    BIGINT UNSIGNED NULL COMMENT '来源 task（P4 来源必填）',
  source_message_id BIGINT UNSIGNED NULL COMMENT '来源 message',
  evidence          VARCHAR(1024) NULL COMMENT '用户原话 / 编辑 diff 摘录',
  supersedes_id     BIGINT UNSIGNED NULL COMMENT '被本条目取代的旧 memory_item.id',
  expires_at        DATETIME NULL COMMENT 'candidate 默认 14 天；style 默认 90 天',
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_scope_status (scope, scope_id, status),
  KEY idx_project_status (project_id, status),
  KEY idx_kind_status (kind, status),
  KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆规则主表（v2）';

-- ---------------------------------------------------------------- 同义说法
CREATE TABLE IF NOT EXISTS memory_variant (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id       BIGINT UNSIGNED NOT NULL,
  text          VARCHAR(1024) NOT NULL COMMENT '同一规则的不同说法',
  embedding_ref VARCHAR(255) NULL COMMENT '向量引用（外部向量存储的 key）',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_variant_item (item_id),
  CONSTRAINT fk_variant_item FOREIGN KEY (item_id)
    REFERENCES memory_item (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆变体（归一化/近义合并时保留旧说法）';

-- ---------------------------------------------------------------- 注入记账
CREATE TABLE IF NOT EXISTS memory_use (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id     BIGINT UNSIGNED NOT NULL,
  task_id     BIGINT UNSIGNED NOT NULL,
  injected_at DATETIME NOT NULL,
  `rank`      INT NULL COMMENT '本轮重排名次（1-based）',
  relevance   DECIMAL(4,3) NULL COMMENT '本轮 query 相关性 rel',
  utility     DECIMAL(4,3) NULL COMMENT 'Beta 后验均值 U',
  outcome     ENUM('helpful','harmful','ignored','unknown') NOT NULL DEFAULT 'unknown'
              COMMENT '回合结束后异步回填',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_use_item_time (item_id, injected_at),
  KEY idx_use_task (task_id),
  CONSTRAINT fk_use_item FOREIGN KEY (item_id)
    REFERENCES memory_item (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆注入记账（U 的分子/分母来源）';

-- ---------------------------------------------------------------- 候选评审
CREATE TABLE IF NOT EXISTS memory_review (
  id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id  BIGINT UNSIGNED NOT NULL,
  action   ENUM('accept','reject','merge') NOT NULL,
  `by`     VARCHAR(64) NOT NULL COMMENT '评审人 user_id / system',
  `at`     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note     VARCHAR(512) NULL,
  PRIMARY KEY (id),
  KEY idx_review_item (item_id),
  CONSTRAINT fk_review_item FOREIGN KEY (item_id)
    REFERENCES memory_item (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='候选评审记录（accept/reject/merge）';

-- ---------------------------------------------------------------- 审计
CREATE TABLE IF NOT EXISTS memory_audit (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id     BIGINT UNSIGNED NULL COMMENT '可为 NULL：条目删除后审计仍保留',
  action      VARCHAR(32) NOT NULL,
  before_json JSON NULL,
  after_json  JSON NULL,
  `by`        VARCHAR(64) NOT NULL,
  `at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_audit_item (item_id),
  KEY idx_audit_at (`at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆变更审计（可回放）';
