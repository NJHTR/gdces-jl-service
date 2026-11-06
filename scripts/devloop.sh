#!/usr/bin/env bash
# 开发用 loop（跑在服务器上，每天 09:00）
#
# 为什么放服务器：开发机上的定时任务对「服务」没有意义 ——
# 服务在服务器上跑，检查也得在同一台机器上看事实。
#
# 为什么只读：这个 loop 不改代码、不改数据。
# 它只做三件事：
#   1. 跑 preflight（引用 / 静默失败 / 硬编码 三条硬线）
#   2. 跑 whatison 拿真实路由和表数量，和上次比（结构变了要人知道）
#   3. 查数据不变量（孤儿文书、待补数量、备份新鲜度、磁盘）
# 任何一条不过，退出码非 0，并把结论写进 runtime/loop-report.log。
set -uo pipefail

R=/home/baize/jl-service
LOG=$R/runtime/loop-report.log
STAMP=$(date '+%F %T')
say() { echo "$STAMP  $*" | tee -a "$LOG" > /dev/null; echo "  $*"; }

cd "$R" || exit 1
FAIL=0

echo "=== 开发 loop $STAMP ==="

# 线上跑的是哪个提交（部署入口会写 runtime/DEPLOYED）
if [ -f "$R/runtime/DEPLOYED" ]; then
  say "[线上] $(head -1 "$R/runtime/DEPLOYED")"
else
  say "[线上] 没有 runtime/DEPLOYED —— 说明不是经 deploy.ps1 部署的"
fi

# 1) preflight 三条硬线
if OUT=$(python3 scripts/preflight.py 2>&1); then
  say "[preflight] 通过"
else
  say "[preflight] 未通过："
  echo "$OUT" | grep -E '^\s+X ' | sed 's/^/    /'
  FAIL=1
fi

# 2) 结构快照（路由数 / 表数），和上次比
ROUTES=$(python3 scripts/whatison.py routes 2>/dev/null | grep -cE '^\s+(GET|POST|PATCH|DELETE)')
TABLES=$(python3 scripts/whatison.py tables 2>/dev/null | grep -cE '^\s+[a-z_]+\s+[a-z_]+')
STATE=$R/runtime/loop-state.txt
PREV=$(cat "$STATE" 2>/dev/null || echo "routes=0 tables=0")
echo "routes=$ROUTES tables=$TABLES" > "$STATE"
if [ "$PREV" != "routes=$ROUTES tables=$TABLES" ]; then
  say "[结构] 变了：上次 [$PREV] → 现在 [routes=$ROUTES tables=$TABLES]（确认是有意改动）"
else
  say "[结构] 未变（路由 $ROUTES / 表 $TABLES）"
fi

# 3) 数据不变量
PW=$(grep '^MYSQL_PASSWORD=' "$R/.env" | cut -d= -f2)
# 必须带 utf8mb4：不带的话中文条件匹配不到（实测 state='待补' 返回 0，实际 132 条）
Q() {
  local out rc
  out=$(docker exec jl-mysql mysql -ujl -p"$PW" --default-character-set=utf8mb4 -N -e "$1" jl 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then
    say "[查库失败] $1 → $out"
    echo "ERR"
    return 1
  fi
  echo "$out" | grep -v '^mysql: \[Warning\]'
}

ORPHAN=$(python3 - <<'PY'
import os, glob, subprocess
pw = [l.split('=',1)[1].strip() for l in open('/home/baize/jl-service/.env') if l.startswith('MYSQL_PASSWORD=')][0]
try:
    out = subprocess.run(["docker","exec","jl-mysql","mysql","-ujl","-p"+pw,
                          "--default-character-set=utf8mb4","-N","-e",
                          "select path from doc where project_id='test-sandbox'","jl"],
                         capture_output=True, text=True, timeout=60).stdout
except Exception:
    print("?"); raise SystemExit
known = {(l or '').split('/')[-1] for l in out.strip().splitlines()}
root = '/home/baize/jl-service/data/workspaces/test-sandbox/docs'
files = [f for p,_,fs in os.walk(root) for f in fs if f.endswith('.md')]
print(len([f for f in files if f not in known]))
PY
)
say "[不变量] test-sandbox 孤儿文书 $ORPHAN 份（应为 0）"
[ "$ORPHAN" != "0" ] && FAIL=1

PENDING=$(Q "select count(*) from doc where state='待补'")
if [ "$PENDING" = "ERR" ] || [ -z "$PENDING" ]; then
  say "[不变量] 待补文书：查询失败"
  FAIL=1
else
  say "[不变量] 待补文书 $PENDING 份"
fi

NEWEST=$(ls -1t "$R"/backups/db-*.gz 2>/dev/null | head -1)
if [ -n "$NEWEST" ]; then
  AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$NEWEST") ) / 3600 ))
  say "[不变量] 最新 db 备份 ${AGE_H} 小时前（应 < 48）"
  [ "$AGE_H" -ge 48 ] && FAIL=1
else
  say "[不变量] 找不到 db 备份"
  FAIL=1
fi

USE=$(df --output=pcent /home | tail -1 | tr -dc '0-9')
say "[不变量] 磁盘使用 ${USE}% （应 < 85）"
[ "$USE" -ge 85 ] && FAIL=1

echo
if [ "$FAIL" -eq 0 ]; then
  say "结论：全部通过"
  exit 0
fi
say "结论：有项目未通过，需人看"
exit 1
