-- 0005 执行 trace 事件账本 · down
-- 只删 task_event；task.event_seq / task.trace_version 两列保留（旧客户端/回滚安全，
-- 删除列会造成不可逆数据丢失，故 down 不删列）。data/trace 下的 gz 由 GC/运维清理。
SET NAMES utf8mb4;

DROP TABLE IF EXISTS task_event;
