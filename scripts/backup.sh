#!/usr/bin/env bash
# 备份：数据库 + 项目工作区。保留 7 天。
set -uo pipefail

R=/home/baize/jl-service
B=$R/backups
mkdir -p "$B"
STAMP=$(date '+%Y%m%d-%H%M')
LOG=$R/runtime/backup.log

PW=$(grep '^MYSQL_ROOT_PASSWORD=' "$R/.env" | cut -d= -f2)

# 1) 数据库
if docker exec jl-mysql mysqldump -uroot -p"$PW" --single-transaction --routines jl 2>/dev/null \
     | gzip > "$B/db-$STAMP.sql.gz"; then
  echo "$(date '+%F %T') db 备份 $(du -h "$B/db-$STAMP.sql.gz" | cut -f1)" >> "$LOG"
else
  echo "$(date '+%F %T') db 备份失败" >> "$LOG"
fi

# 2) 工作区（含 .git，能完整还原历史）
if tar -czf "$B/ws-$STAMP.tar.gz" -C "$R/data" workspaces 2>/dev/null; then
  echo "$(date '+%F %T') 工作区备份 $(du -h "$B/ws-$STAMP.tar.gz" | cut -f1)" >> "$LOG"
else
  echo "$(date '+%F %T') 工作区备份失败" >> "$LOG"
fi

# 3) 只留 7 天
find "$B" -name '*.gz' -type f -mtime +7 -delete

echo "$(date '+%F %T') 完成，当前备份 $(ls -1 "$B" | wc -l) 个" >> "$LOG"
