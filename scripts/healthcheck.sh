#!/usr/bin/env bash
# 服务自愈：BFF 不响应就拉起来。由 cron 每 5 分钟调一次
set -uo pipefail

R=/home/baize/jl-service
LOG=$R/runtime/healthcheck.log
ts() { date '+%F %T'; }

# mysql 容器没起就先起（宿主重启后）
if ! docker ps --format '{{.Names}}' | grep -qx jl-mysql; then
  echo "$(ts) mysql 未运行，启动" >> "$LOG"
  (cd $R/compose && docker compose -p jl -f compose.yml up -d mysql) >> "$LOG" 2>&1
  sleep 15
fi

# BFF 健康检查
if curl -sf -m 5 http://127.0.0.1:8081/healthz > /dev/null 2>&1; then
  exit 0
fi

echo "$(ts) BFF 无响应，重启" >> "$LOG"
pkill -f 'runtime/server.py' > /dev/null 2>&1
sleep 1
cd $R && nohup python3 runtime/server.py >> "$R/runtime/bff.log" 2>&1 &

for i in $(seq 1 15); do
  sleep 2
  if curl -sf -m 5 http://127.0.0.1:8081/healthz > /dev/null 2>&1; then
    echo "$(ts) 重启成功" >> "$LOG"
    exit 0
  fi
done

echo "$(ts) 重启失败，需人工" >> "$LOG"
exit 1
