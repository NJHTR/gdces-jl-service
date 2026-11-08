-- M1b-1 · 0002 回滚：去掉 legacy_memory_id 映射列与其唯一键
-- 只改 memory_item 结构，不改 legacy `memory` 表。

ALTER TABLE memory_item
  DROP INDEX uk_legacy_memory,
  DROP COLUMN legacy_memory_id;
