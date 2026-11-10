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
-- 注意：线上 memory 表比旧版 schema 多了 project_id / source_ref / applies_to / updated_at，
-- 这里按线上实际结构同步（2026-11-10）。改动线上表前先 SHOW CREATE TABLE 对一遍。
CREATE TABLE IF NOT EXISTS memory (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  scope      ENUM('user','project') NOT NULL,
  owner_id   VARCHAR(32) NOT NULL COMMENT 'scope=user 时是 user_id，等于 project 时是 project_id',
  project_id VARCHAR(32) NULL COMMENT 'scope=user 且只在本项目生效时=项目 id；跨项目/项目共享为空',
  text       VARCHAR(512) NOT NULL,
  source     ENUM('manual','auto','learned','confirmed','correction') NOT NULL DEFAULT 'manual',
  source_ref VARCHAR(64) NULL COMMENT '来源引用：proposal:<id> / candidate:<id>',
  applies_to VARCHAR(64) NULL COMMENT 'user:<uid> / user:<uid>@project:<pid> / project:<pid>',
  hits       INT NOT NULL DEFAULT 0 COMMENT '被引用次数',
  status     TINYINT NOT NULL DEFAULT 1 COMMENT '1=生效 0=停用',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME(3) NULL,
  KEY idx_owner (scope, owner_id, status),
  KEY idx_mem_proj (scope, owner_id, project_id)
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

-- ============ 复核判定（对抗性复核的落库） ============
CREATE TABLE IF NOT EXISTS review (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  doc_id       BIGINT UNSIGNED NOT NULL,
  task_id      BIGINT UNSIGNED NULL,
  project_id   VARCHAR(64) NOT NULL,
  verdict      VARCHAR(16) NOT NULL COMMENT 'must_fix / suggest / ok',
  findings     JSON NULL COMMENT '[{why, locator}]',
  counter      JSON NULL COMMENT '替代解释/反证',
  alternatives JSON NULL COMMENT '被排除的解释',
  uncertainty  VARCHAR(512) NULL,
  model        VARCHAR(64) NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_doc (doc_id), KEY idx_project (project_id), KEY idx_verdict (verdict)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复核判定';

-- ============ 引用账本（用得越久越权威） ============
CREATE TABLE IF NOT EXISTS citation (
  id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  source_kind    VARCHAR(16) NOT NULL COMMENT 'standard / file / photo / memory / thread / web',
  ref            VARCHAR(512) NOT NULL,
  quote          VARCHAR(600) NULL,
  digest         CHAR(32) NOT NULL COMMENT 'md5(kind+ref+quote)',
  used_count     INT NOT NULL DEFAULT 0,
  verified_count INT NOT NULL DEFAULT 0,
  disputed       TINYINT NOT NULL DEFAULT 0,
  status         VARCHAR(16) NOT NULL DEFAULT 'raw' COMMENT 'raw / trusted / authoritative',
  last_used_at   DATETIME NULL,
  created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_digest (digest),
  KEY idx_kind (source_kind), KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='引用账本';

-- ============ 观察队列（同类问题反复出现） ============
CREATE TABLE IF NOT EXISTS observation (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id  VARCHAR(64) NOT NULL,
  doc_kind    VARCHAR(64) NOT NULL DEFAULT '',
  tag         VARCHAR(32) NOT NULL COMMENT '复核员打的短标签',
  count       INT NOT NULL DEFAULT 1,
  status      VARCHAR(16) NOT NULL DEFAULT 'open' COMMENT 'open / ack / fixed',
  samples     JSON NULL COMMENT '最近几条原话',
  first_seen  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_obs (project_id, doc_kind, tag),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='观察队列：重复出现的问题';

-- 项目规矩提案：模型/纠正只能"提议"，人确认后才进 memory（再渲染 rules.md）
CREATE TABLE IF NOT EXISTS rule_proposal (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id  VARCHAR(64) NOT NULL,
  user_id     VARCHAR(32) NULL,
  text        VARCHAR(512) NOT NULL,
  source      VARCHAR(32) NOT NULL DEFAULT 'agent' COMMENT 'agent / correction / manual',
  source_ref  VARCHAR(64) NULL COMMENT '哪次对话/哪个文书',
  evidence    TEXT NULL COMMENT '为什么提这条（原文/字段改动）',
  status      ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'pending',
  decided_by  VARCHAR(32) NULL,
  decided_at  DATETIME(3) NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_pending (project_id, status),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目规矩提案（待确认）';

-- 记忆候选（Phase1）：服务端/Agent 只能"提"，人确认后才进 memory（再重渲染文件视图）。
-- pending 永远不进 memory / records/rules.md / 提示；表由 BFF 启动时 ensure 幂等创建。
CREATE TABLE IF NOT EXISTS memory_candidate (
  id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  project_id        VARCHAR(32) NOT NULL COMMENT '发起/归属项目（user 候选也记来源项目）',
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
  KEY idx_cand_pending (project_id, scope, status),
  KEY idx_cand_user (user_id, status),
  KEY idx_cand_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆候选（pending 不生效；接受后才写 memory 并重渲染文件）';

-- ============ 多模态资产（Phase1 MVP） ============
-- 资产只存 data/assets/<pid>/<asset_id>；绝不写 blobs/photos/docs/records/memory/knowledge。
-- 由 BFF 启动时 ensure_asset_table() 幂等创建；staged 24h，generated/derived 90d，软删除。
CREATE TABLE IF NOT EXISTS asset (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
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
  UNIQUE KEY uk_asset_id (asset_id),
  KEY idx_asset_project_status (project_id, status),
  KEY idx_asset_message (message_id),
  KEY idx_asset_expires (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='多模态资产（独立 data/assets；staged 不对外当最终产物）';

-- 复核复议：一次、有界（同一条复核只能被申请一次；结果落库可追溯）
CREATE TABLE IF NOT EXISTS review_appeal (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  review_id    BIGINT UNSIGNED NOT NULL,
  doc_id       BIGINT UNSIGNED NOT NULL,
  project_id   VARCHAR(64) NOT NULL,
  user_id      VARCHAR(32) NULL,
  reason       VARCHAR(500) NOT NULL,
  status       ENUM('pending','upheld','overturned') NOT NULL DEFAULT 'pending',
  decision_note VARCHAR(500) NULL,
  decided_by   VARCHAR(32) NULL,
  decided_at   DATETIME(3) NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_review (review_id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复核复议（一次、有界）';
