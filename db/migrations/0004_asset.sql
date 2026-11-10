-- 0004 多模态资产（Phase1 MVP）· up
-- 全新环境用迁移工具建表；生产以 BFF 启动时 ensure_asset_table() 为准（部署不传 db/）。
-- 资产字节只落 data/assets/<project_id>/<asset_id>；本表只存元数据。
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS asset (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  asset_id     CHAR(26) NOT NULL COMMENT 'ULID；对外 id，也是 data/assets/<pid>/ 下的文件名',
  project_id   VARCHAR(32) NOT NULL,
  user_id      BIGINT UNSIGNED NULL COMMENT '创建者；uploaded 引用是引用发起人',
  task_id      BIGINT UNSIGNED NULL,
  message_id   BIGINT UNSIGNED NULL,
  kind         ENUM('image','audio','file') NOT NULL,
  mime         VARCHAR(96) NOT NULL,
  size         INT UNSIGNED NOT NULL,
  sha256       CHAR(64) NOT NULL COMMENT '去元数据后的字节 sha',
  origin       ENUM('uploaded','derived','generated') NOT NULL
               COMMENT 'uploaded=引用现场照片/用户上传；derived=加工；generated=生成',
  producer     VARCHAR(32) NOT NULL DEFAULT 'bff' COMMENT 'photo_ref / annotate / compose / tool 名',
  model        VARCHAR(64) NULL,
  prompt_hash  CHAR(64) NULL,
  source_refs  JSON NULL COMMENT '来源引用，如 ["photo:55","asset:<ulid>"]',
  caption      VARCHAR(300) NULL,
  is_stored    TINYINT NOT NULL DEFAULT 1 COMMENT '1=字节在 data/assets',
  path         VARCHAR(255) NULL COMMENT 'data/assets 下的相对路径',
  status       ENUM('staged','committed','quarantined','deleted') NOT NULL DEFAULT 'staged',
  expires_at   DATETIME NULL,
  deleted_at   DATETIME NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_asset_id (asset_id),
  KEY idx_asset_project_status (project_id, status),
  KEY idx_asset_message (message_id),
  KEY idx_asset_expires (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='多模态资产（独立 data/assets；staged 不对外当最终产物）';
