-- 监理 AI 服务 · 数据库结构
-- 字符集 utf8mb4，时间统一 +08:00（容器启动参数里设）

SET NAMES utf8mb4;

-- ============ 租户 / 用户 ============
CREATE TABLE IF NOT EXISTS tenant (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(64) NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='租户（公司内部只有一个，留着扩展）';

CREATE TABLE IF NOT EXISTS app_user (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  tenant_id     BIGINT UNSIGNED NOT NULL,
  openid        VARCHAR(64) NOT NULL COMMENT '微信 openid',
  name          VARCHAR(32) NOT NULL,
  role          VARCHAR(16) NOT NULL DEFAULT '监理员',
  phone         VARCHAR(20) NULL,
  status        TINYINT NOT NULL DEFAULT 1 COMMENT '1=正常 0=停用',
  token_version INT NOT NULL DEFAULT 0 COMMENT '改密/封号时 +1，旧 token 立即失效',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_openid (openid),
  KEY idx_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工';

-- ============ 项目 ============
CREATE TABLE IF NOT EXISTS project (
  id          VARCHAR(32) PRIMARY KEY COMMENT '项目 id，同时是工作区目录名',
  tenant_id   BIGINT UNSIGNED NOT NULL,
  name        VARCHAR(64) NOT NULL,
  code        VARCHAR(32) NULL COMMENT '工程编号',
  addr        VARCHAR(128) NULL,
  meta        JSON NOT NULL COMMENT '工程概况 21 字段',
  ws_path     VARCHAR(255) NOT NULL COMMENT '工作区绝对路径',
  status      TINYINT NOT NULL DEFAULT 1,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_tenant (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='工程项目';

CREATE TABLE IF NOT EXISTS project_member (
  project_id  VARCHAR(32) NOT NULL,
  user_id     BIGINT UNSIGNED NOT NULL,
  role        VARCHAR(16) NOT NULL DEFAULT 'member' COMMENT 'owner/engineer/member',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (project_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目成员';

-- ============ 会话映射 ============
CREATE TABLE IF NOT EXISTS thread_map (
  id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id     VARCHAR(32) NOT NULL,
  user_id        BIGINT UNSIGNED NOT NULL,
  thread_id      VARCHAR(64) NOT NULL COMMENT 'easyCode 的 thread id',
  kind           ENUM('chronic','ephemeral') NOT NULL DEFAULT 'chronic',
  last_active_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_slot (project_id, user_id, kind),
  UNIQUE KEY uk_thread (thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目×用户 → 引擎会话';

-- ============ 消息 ============
CREATE TABLE IF NOT EXISTS message (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id VARCHAR(32) NOT NULL,
  user_id    BIGINT UNSIGNED NOT NULL,
  client_id  VARCHAR(48) NOT NULL COMMENT '前端生成，幂等去重',
  role       ENUM('user','assistant','system') NOT NULL,
  text       MEDIUMTEXT NULL,
  photos     JSON NULL,
  audio_url  VARCHAR(255) NULL,
  dur        VARCHAR(8) NULL,
  quote      JSON NULL COMMENT '{who,text,refId}',
  trace      JSON NULL COMMENT 'AI 操作轨迹 [{tool,icon,text,ms}]',
  trace_sum  VARCHAR(128) NULL,
  ms         INT NULL,
  sync       TINYINT NOT NULL DEFAULT 0 COMMENT '0=待上传 1=已上传',
  task_id    BIGINT UNSIGNED NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_client (client_id),
  KEY idx_proj_time (project_id, created_at),
  KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='现场消息（含 AI 回复）';

-- ============ 文书 ============
CREATE TABLE IF NOT EXISTS doc (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id  VARCHAR(32) NOT NULL,
  kind        VARCHAR(16) NOT NULL COMMENT '旁站记录/监理通知单/监理日志',
  obj         VARCHAR(64) NULL,
  state       VARCHAR(8) NOT NULL DEFAULT '待补' COMMENT '待补/待签/已签',
  fields      JSON NULL,
  bases       JSON NULL COMMENT '依据 [{kind,ref,text,ic}]',
  path        VARCHAR(255) NULL COMMENT '工作区相对路径',
  git_commit  CHAR(40) NULL,
  created_by  BIGINT UNSIGNED NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_proj_time (project_id, created_at),
  KEY idx_state (project_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文书';

-- ============ 影像 ============
CREATE TABLE IF NOT EXISTS photo (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id  VARCHAR(32) NOT NULL,
  sha256      CHAR(64) NOT NULL,
  blob_path   VARCHAR(255) NOT NULL,
  place       VARCHAR(128) NULL,
  weather     VARCHAR(32) NULL,
  shot_at     DATETIME NULL,
  lat         DECIMAL(9,6) NULL,
  lng         DECIMAL(9,6) NULL,
  src         VARCHAR(16) NOT NULL DEFAULT 'camera' COMMENT 'camera/chat/album',
  git_commit  CHAR(40) NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_sha (project_id, sha256),
  KEY idx_proj_time (project_id, shot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='影像元数据（本体在 data/blobs）';

-- ============ 记忆 ============
CREATE TABLE IF NOT EXISTS memory (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  scope      ENUM('user','project') NOT NULL,
  owner_id   VARCHAR(32) NOT NULL COMMENT 'scope=user 时是 user_id，等于 project 时是 project_id',
  text       VARCHAR(512) NOT NULL,
  source     ENUM('manual','auto','learned') NOT NULL DEFAULT 'manual',
  hits       INT NOT NULL DEFAULT 0 COMMENT '被引用次数',
  status     TINYINT NOT NULL DEFAULT 1 COMMENT '1=生效 0=停用',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_owner (scope, owner_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆（三层里的两层，会话层在引擎里）';

-- ============ 任务 ============
CREATE TABLE IF NOT EXISTS task (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id  VARCHAR(32) NOT NULL,
  user_id     BIGINT UNSIGNED NOT NULL,
  client_id   VARCHAR(48) NOT NULL,
  thread_id   VARCHAR(64) NULL,
  turn_id     VARCHAR(64) NULL,
  kind        VARCHAR(16) NOT NULL DEFAULT 'chat',
  payload     JSON NULL,
  status      ENUM('queued','running','done','failed','cancelled') NOT NULL DEFAULT 'queued',
  result      JSON NULL,
  error       TEXT NULL,
  started_at  DATETIME(3) NULL,
  finished_at DATETIME(3) NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_client (client_id),
  KEY idx_status (status, created_at),
  KEY idx_proj (project_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='异步任务';

-- ============ 审计 ============
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  actor_id    BIGINT UNSIGNED NULL,
  project_id  VARCHAR(32) NULL,
  action      VARCHAR(24) NOT NULL COMMENT 'sign/fill/delete/generate/project_edit',
  target_type VARCHAR(16) NULL,
  target_id   VARCHAR(64) NULL,
  `before`      JSON NULL,
  `after`       JSON NULL,
  commit_hash CHAR(40) NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_target (target_type, target_id, created_at),
  KEY idx_proj_time (project_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作留痕';

-- ============ 规范条文 ============
CREATE TABLE IF NOT EXISTS standard (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  code         VARCHAR(32) NOT NULL COMMENT '如 JGJ 130-2011',
  clause       VARCHAR(16) NOT NULL COMMENT '如 6.4.3',
  title        VARCHAR(128) NULL COMMENT '条标题',
  std_title    VARCHAR(128) NULL COMMENT '标准全名，如 混凝土结构工程施工质量验收规范',
  body         MEDIUMTEXT NULL,
  version      VARCHAR(16) NULL,
  effective_at DATE NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_code_clause (code, clause),
  FULLTEXT KEY ft_body (title, body) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='规范条文库';

-- ============ 种子：一个租户 ============
INSERT IGNORE INTO tenant (id, name) VALUES (1, '广东建设工程监理有限公司');
