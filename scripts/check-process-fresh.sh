#!/usr/bin/env bash
# 断言：entry.py 进程必须比本次上传的代码新（否则就是"文件更新了但服务没重启"）。
#
# 为什么不用 ps -o lstart：中文 locale 下输出是「六 11月 7 01:13:09 2026」，
# date -d 解析成空 → SE=0 → 断言永远判失败（或永远判通过，取决于比较方向）。
# 这里用 /proc/<pid>/stat 的 starttime + /proc/stat 的 btime 算 epoch，与 locale 无关。
#
# 用法：
#   bash scripts/check-process-fresh.sh            # 正常：与 runtime/UPLOADED_AT 比
#   bash scripts/check-process-fresh.sh <epoch>    # 负向测试：传一个未来的 epoch，预期退出 3
set -uo pipefail

R=${JL_ROOT:-/home/baize/jl-service}
UA=${1:-$(cat "$R/runtime/UPLOADED_AT" 2>/dev/null || echo 0)}

PID=$(pgrep -f 'runtime/entry.py' | head -1 || true)
if [ -z "${PID:-}" ]; then
  echo "STALE: 没有 entry.py 进程"
  exit 3
fi

HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
BOOT=$(awk '/^btime/{print $2}' /proc/stat)
TICKS=$(awk '{print $22}' "/proc/$PID/stat" 2>/dev/null || echo "")
if [ -z "$TICKS" ] || [ -z "$BOOT" ]; then
  echo "STALE: 读不到进程启动信息（pid=$PID）"
  exit 3
fi
SE=$(( BOOT + TICKS / HZ ))
AGE=$(( $(date +%s) - SE ))

echo "进程 $PID 启动于 $(date -d "@$SE" '+%F %T')（${AGE}s 前）"
echo "代码上传于 $(date -d "@$UA" '+%F %T')"

if [ "$SE" -lt "$UA" ]; then
  echo "STALE: 进程启动时间早于代码上传时间（服务没真的重启）"
  exit 3
fi
echo "进程比代码新（OK）"
exit 0
