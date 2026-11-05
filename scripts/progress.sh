#!/usr/bin/env bash
# 一条命令看清"现在是什么状态"。
#
#   bash scripts/progress.sh
#
# 目的：不管是隔了三天回来，还是换人接手，跑一下就知道现场。
# 输出只放事实，不放判断。

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

line() { printf '  %-22s %s\n' "$1" "$2"; }
head2() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head2 "服务"
bff=$(curl -s -m 4 http://127.0.0.1:8081/healthz 2>/dev/null || echo "（没响应）")
line "BFF 8081" "$bff"
java_code=$(curl -s -m 4 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8082/api/v1/docs?projectId=yun-gu-3qi" 2>/dev/null || echo "000")
line "Java 8082" "$([ "$java_code" = "200" ] && echo "ok" || echo "（$java_code）")"
line "MySQL 容器" "$(docker ps --filter name=jl-mysql --format '{{.Status}}' 2>/dev/null || echo '—')"
line "引擎二进制" "$([ -x repos/easycode/.target/release/easycode ] && echo '存在' || echo '缺失')"

head2 "数据"
PW=$(grep '^MYSQL_PASSWORD=' .env 2>/dev/null | cut -d= -f2)
if [ -n "${PW:-}" ]; then
  docker exec jl-mysql mysql -ujl -p"$PW" -N -e "
    SELECT CONCAT('项目 ', (SELECT COUNT(*) FROM jl.project),
           ' | 文书 ', (SELECT COUNT(*) FROM jl.doc),
           ' | 待补 ', (SELECT COUNT(*) FROM jl.doc WHERE state='待补'),
           ' | 消息 ', (SELECT COUNT(*) FROM jl.message),
           ' | 任务 ', (SELECT COUNT(*) FROM jl.task),
           ' | 记忆 ', (SELECT COUNT(*) FROM jl.memory),
           ' | 审计 ', (SELECT COUNT(*) FROM jl.audit_log));" 2>/dev/null | sed 's/^/  /'
  docker exec jl-mysql mysql -ujl -p"$PW" -N -e "
    SELECT CONCAT('  最近 3 个任务: ', GROUP_CONCAT(CONCAT(id,' ',status) ORDER BY id DESC SEPARATOR ' | '))
    FROM (SELECT id, status FROM jl.task ORDER BY id DESC LIMIT 3) t;" 2>/dev/null
else
  echo "  （没读到 .env）"
fi

head2 "代码"
if git rev-parse --git-dir >/dev/null 2>&1; then
  line "后端 HEAD" "$(git log --oneline -1 2>/dev/null | cut -c1-72)"
else
  line "后端 HEAD" "（服务器不是 git 仓库，看 Windows 侧副本）"
fi
line "未提交" "$(git status --short 2>/dev/null | wc -l | tr -d ' ') 个文件"
line "工作区提交" "$(git -C data/workspaces/yun-gu-3qi rev-list --count HEAD 2>/dev/null || echo '—') 次"

head2 "计划里还没打勾的"
if [ -f docs/PLAN.md ]; then
  grep -n '^\- \[ \]' docs/PLAN.md | head -12 | sed 's/^/  /'
else
  echo "  （没有 PLAN.md）"
fi

head2 "已知问题"
if [ -f docs/STATUS.md ]; then
  awk '/^## 已知问题/{f=1;next} /^## /{f=0} f' docs/STATUS.md | grep '^| [0-9]' | cut -c1-100 | sed 's/^/  /' | head -8
fi

head2 "下一步"
if [ -f docs/STATUS.md ]; then
  awk '/^## 现在在做什么/{f=1;next} /^---/{f=0} f' docs/STATUS.md | grep -E '^\*\*' | sed 's/^/  /'
fi

printf '\n  文档：docs/GOAL.md 目标 · docs/PLAN.md 计划 · docs/STATUS.md 状态 · docs/DECISIONS.md 决策\n\n'
