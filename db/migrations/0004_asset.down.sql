-- 0004 多模态资产（Phase1 MVP）· down
-- 只回滚本迁移新建的表；data/assets 下的字节由 BFF GC/运维手工清理。
SET NAMES utf8mb4;

DROP TABLE IF EXISTS asset;
