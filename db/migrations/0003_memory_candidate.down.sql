-- 0003 记忆候选（Phase1）· down
-- 只回滚本迁移新建的表；不动 memory / rule_proposal / 工作区文件。
SET NAMES utf8mb4;

DROP TABLE IF EXISTS memory_candidate;
