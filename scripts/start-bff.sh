#!/usr/bin/env bash
# 唯一的启动入口。cron @reboot、healthcheck、手工运维都用它。
#
# 为什么要有这个脚本：
#   以前 healthcheck 里写的是 `python3 runtime/server.py`（旧版单体文件），
#   而且不带任何环境变量 —— 一旦自愈重启，服务会回到旧代码、且退化成 CLI 模式，
#   复核模型也没有了。**看起来"恢复了"，其实是静默降级。**
set -uo pipefail

R=/home/baize/jl-service
LOG=$R/runtime/bff.log

# ---- 运行参数（集中在这里，别再散落在各脚本里）----
export ENGINE_MODE=${ENGINE_MODE:-appserver}
export ENGINE_MODEL=${ENGINE_MODEL:-qwen3.7-plus}
export REVIEW_MODEL=${REVIEW_MODEL:-qwen3.8-flash}
export JL_APPSERVER_LOG=${JL_APPSERVER_LOG:-1}
export JL_ENGINE_LOG=${JL_ENGINE_LOG:-warn}
export BIND_HOST=${BIND_HOST:-0.0.0.0}

cd "$R" || exit 1

# 已经在跑就不重复起（避免端口冲突后反复失败）
if curl -sf -m 5 http://127.0.0.1:8081/healthz > /dev/null 2>&1; then
  echo "$(date '+%F %T') 已在运行，跳过"
  exit 0
fi

# 清掉旧进程（两种入口都覆盖，包括误起的旧版 server.py）
pkill -f 'runtime/entry.py' > /dev/null 2>&1
pkill -f 'runtime/server.py' > /dev/null 2>&1
sleep 2

rm -rf "$R/bff/bffpkg/__pycache__"
setsid nohup python3 runtime/entry.py >> "$LOG" 2>&1 < /dev/null &
echo "$(date '+%F %T') 启动 entry.py（$ENGINE_MODE / 引擎 $ENGINE_MODEL / 复核 $REVIEW_MODEL）" | tee -a "$LOG"

for _ in $(seq 1 20); do
  sleep 2
  if curl -sf -m 5 http://127.0.0.1:8081/healthz > /dev/null 2>&1; then
    echo "$(date '+%F %T') 启动成功"
    exit 0
  fi
done

echo "$(date '+%F %T') 启动失败，需人工" 
exit 1
