-- M1b-1 · 0002：memory_item 增加 legacy_memory_id 映射列
-- 用途：把 legacy `memory` 表（39 行）幂等映射进 v2 `memory_item`；
--       UNIQUE KEY 保证同一 legacy id 只导入一次（可重复执行）。
-- 注意：只 ALTER memory_item，不改 legacy `memory` 表。

ALTER TABLE memory_item
  ADD COLUMN legacy_memory_id BIGINT UNSIGNED NULL
    COMMENT '来源 legacy memory.id；NULL=非导入行（幂等回填唯一键）',
  ADD UNIQUE KEY uk_legacy_memory (legacy_memory_id);
