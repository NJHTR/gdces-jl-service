#!/usr/bin/env bash
# 服务自愈：BFF 不响应就拉起来。由 cron 每 5 分钟调一次。
#
# 启动统一走 scripts/start-bff.sh —— 以前这里硬写 `python3 runtime/server.py`
# （旧版单体文件）且不带环境变量，自愈后会静默降级成 CLI 模式、没有复核模型。
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

echo "$(ts) BFF 无响应，重启（走 start-bff.sh）" >> "$LOG"
if "$R/scripts/start-bff.sh" >> "$LOG" 2>&1; then
  echo "$(ts) 重启成功" >> "$LOG"
  exit 0
fi

echo "$(ts) 重启失败，需人工" >> "$LOG"
exit 1
