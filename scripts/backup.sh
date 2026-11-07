#!/usr/bin/env bash
# 备份：数据库 + 项目工作区 + 现场影像 + 记忆 + 组织知识。保留 7 天。
#
# 2026-11-06 补漏：原本只备份 db 和 workspaces，
#   data/blobs（照片/语音，现场的原始证据）没备 —— 5MB，丢了不可再生
#   data/homes 里的记忆（MEMORY.md / memory_summary.md / config.toml）没备 —— 184KB
#   data/knowledge（规范/模板/技能）没备 —— 虽然进 git，但推送通道当时不通
# 现在都补上；homes 只备记忆和配置，**不备 sessions**（2GB，可重建）。
#
# 恢复方式（灾难时）：
#   1) 起 mysql 容器 → zcat db-*.sql.gz | docker exec -i jl-mysql mysql -uroot -p*** jl
#   2) tar -xzf ws-*.tar.gz  -C data/          # 工作区（含 .git 历史）
#   3) tar -xzf blobs-*.tar.gz -C data/        # 照片/语音
#   4) tar -xzf mem-*.tar.gz  -C data/         # homes 里的记忆与配置
#   5) tar -xzf know-*.tar.gz -C data/         # 规范/模板/技能
#   6) tar -xzf exports-*.tar.gz -C data/      # 已生成的巡视记录/表式 docx
#   7) tar -xzf quarantine-*.tar.gz -C data/   # 隔离区（旧记忆产物，不删）
#   8) tar -xzf memview-*.tar.gz  -C data/      # 个人×项目记忆视图
set -uo pipefail

R=/home/baize/jl-service
B=$R/backups
mkdir -p "$B"
STAMP=$(date '+%Y%m%d-%H%M')
LOG=$R/runtime/backup.log
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

PW=$(grep '^MYSQL_ROOT_PASSWORD=' "$R/.env" | cut -d= -f2)

# 1) 数据库
if docker exec jl-mysql mysqldump -uroot -p"$PW" --default-character-set=utf8mb4 \
     --single-transaction --routines jl 2>/dev/null \
     | gzip > "$B/db-$STAMP.sql.gz"; then
  say "db 备份 $(du -h "$B/db-$STAMP.sql.gz" | cut -f1)"
else
  say "db 备份失败"
fi

# 2) 项目工作区（含 .git，能完整还原历史）
if tar -czf "$B/ws-$STAMP.tar.gz" -C "$R/data" workspaces 2>/dev/null; then
  say "工作区备份 $(du -h "$B/ws-$STAMP.tar.gz" | cut -f1)"
else
  say "工作区备份失败"
fi

# 3) 现场影像（照片/语音原始证据）
if [ -d "$R/data/blobs" ] && tar -czf "$B/blobs-$STAMP.tar.gz" -C "$R/data" blobs 2>/dev/null; then
  say "影像备份 $(du -h "$B/blobs-$STAMP.tar.gz" | cut -f1)"
else
  say "影像备份失败（或目录不存在）"
fi

# 4) 记忆与配置（homes 里只挑小的、不可再生的；不备 sessions）
# 用相对路径打包（cd 进 data 再找），这样恢复时统一 -C data/ 即可
if ( cd "$R/data" && find homes -maxdepth 3 \( -name 'MEMORY.md' -o -name 'memory_summary.md' \
      -o -name 'raw_memories.md' -o -name 'config.toml' \) -type f -print0 2>/dev/null \
     | tar -czf "$B/mem-$STAMP.tar.gz" --null -T - ) 2>/dev/null; then
  say "记忆备份 $(du -h "$B/mem-$STAMP.tar.gz" | cut -f1)"
else
  say "记忆备份失败（可能还没有任何记忆）"
fi

# 5) 组织知识（规范/模板/技能）
if [ -d "$R/data/knowledge" ] && tar -czf "$B/know-$STAMP.tar.gz" -C "$R/data" knowledge 2>/dev/null; then
  say "知识备份 $(du -h "$B/know-$STAMP.tar.gz" | cut -f1)"
else
  say "知识备份失败"
fi

# 6) 导出产物（生成的巡视记录/表式 docx：丢了要重新跑，且当时的字段可能已变）
if [ -d "$R/data/exports" ] && tar -czf "$B/exports-$STAMP.tar.gz" -C "$R/data" exports 2>/dev/null; then
  say "导出备份 $(du -h "$B/exports-$STAMP.tar.gz" | cut -f1)"
else
  say "导出备份失败（或目录不存在）"
fi

# 6.5) 个人×项目记忆的渲染视图（data/memory；可从 memory 表重建，但一并备）
if [ -d "$R/data/memory" ] && tar -czf "$B/memview-$STAMP.tar.gz" -C "$R/data" memory 2>/dev/null; then
  say "记忆视图备份 $(du -h "$B/memview-$STAMP.tar.gz" | cut -f1)"
else
  say "记忆视图备份失败（或目录不存在）"
fi

# 7) 隔离区（不删的旧记忆产物：磁盘坏了也要能找回）
if [ -d "$R/data/quarantine" ] && tar -czf "$B/quarantine-$STAMP.tar.gz" -C "$R/data" quarantine 2>/dev/null; then
  say "隔离区备份 $(du -h "$B/quarantine-$STAMP.tar.gz" | cut -f1)"
else
  say "隔离区备份失败（或目录不存在）"
fi

# 8) 只留 7 天
find "$B" -name '*.gz' -type f -mtime +7 -delete

say "完成，当前备份 $(ls -1 "$B" | wc -l) 个"
