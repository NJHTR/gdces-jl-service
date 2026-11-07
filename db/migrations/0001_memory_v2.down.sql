-- M1 记忆 v2 · 回滚（down）
-- 与 0001_memory_v2.sql 对应：按外键依赖倒序删表。
-- 只动 v2 新表，不碰旧 memory 表。

SET NAMES utf8mb4;

DROP TABLE IF EXISTS memory_audit;
DROP TABLE IF EXISTS memory_review;
DROP TABLE IF EXISTS memory_use;
DROP TABLE IF EXISTS memory_variant;
DROP TABLE IF EXISTS memory_item;
