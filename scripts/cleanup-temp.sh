#!/bin/sh
# 清理服务产生的临时产物。
#
# 背景：出稿结果、复核目录、语音 wav 都改成了「按任务隔离」（防串单），
# 代价是会随使用量增长。正常由 service_job 的 cleanup_temp 每天 04:00 清，
# 这个脚本是给手工排查用的同一套逻辑。
#
# 只删「超过 1 天」的 —— 正在跑的不会被误删。
set -u

find /tmp -maxdepth 1 -name 'jl-review-*' -type d -mtime +0 -exec rm -rf {} + 2>/dev/null || true
find /tmp -maxdepth 1 -name 'jl-asr-*.wav' -mtime +0 -delete 2>/dev/null || true
find /home/baize/jl-service/data/workspaces -path '*/.agent/last-*.md' -mtime +0 -delete 2>/dev/null || true

echo "[cleanup] 完成 $(date '+%F %T')"
