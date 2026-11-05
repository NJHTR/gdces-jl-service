#!/usr/bin/env bash
# 初始化一个项目工作区：目录 + git + 钩子 + 首次提交
# 用法：init-project.sh <project_id> <项目名称> [工程编号]
set -euo pipefail

ROOT="${JL_ROOT:-/home/baize/jl-service}"
PID="${1:?用法: init-project.sh <project_id> <项目名称> [工程编号]}"
PNAME="${2:?缺项目名称}"
PCODE="${3:-}"

WS="$ROOT/data/workspaces/$PID"
[ -d "$WS" ] && { echo "已存在: $WS"; exit 1; }

mkdir -p "$WS"/{memories,records,docs,photos,.agent}

cat > "$WS/PROJECT.md" <<EOF
# $PNAME

> 这是本项目的上下文说明，agent 每次会话会读它。

## 基本信息

- 项目 ID：\`$PID\`
- 工程编号：${PCODE:-待补}
- 监理单位：广东建设工程监理有限公司

## 现场约定

<!-- 部位怎么叫、状态怎么写、验收口径 —— 越具体越好 -->

- 部位写法：楼号 + 楼层 + 构件，例：\`4#楼东侧外架\`、\`3#5F 顶板\`
- 结论写法：待补（写入 \`memories/project.md\` 后以那边为准）

## 目录约定

| 目录 | 放什么 |
|---|---|
| \`records/\` | 现场记录，按日期分子目录 |
| \`docs/\` | 生成的文书 |
| \`memories/\` | 项目记忆（自动渲染，勿手改） |
| \`photos/\` | 照片索引（图片本体在 data/blobs） |
EOF

cat > "$WS/meta.json" <<EOF
{
  "id": "$PID",
  "name": "$PNAME",
  "code": "${PCODE}",
  "addr": "",
  "owner": "",
  "builder": "",
  "supervisor": "广东建设工程监理有限公司",
  "design": "",
  "survey": "",
  "category": "房屋建筑",
  "area": "",
  "floors": "",
  "structure": "",
  "cost": "",
  "start": "",
  "finish": "",
  "license": "",
  "bureau": "",
  "chief": "",
  "reps": "",
  "contract": ""
}
EOF

cat > "$WS/memories/project.md" <<EOF
# 项目记忆 · $PNAME

<!-- 由 MySQL 渲染生成，勿手改。要改请走管理后台。 -->

暂无。
EOF

cat > "$WS/photos/manifest.json" <<EOF
{ "projectId": "$PID", "photos": [] }
EOF

cat > "$WS/.gitignore" <<'EOF'
# 图片/音频本体不进 git（在 data/blobs，内容寻址）
blobs/
*.jpg
*.jpeg
*.png
*.mp3
*.m4a
*.wav
# 会话临时文件
.agent/tmp/
EOF

cd "$WS"
git init -q -b main
git config user.name "jl-agent"
git config user.email "agent@jl.local"
git config commit.gpgsign false

# ---- 钩子 ----
HOOKS="$WS/.git/hooks"

cat > "$HOOKS/commit-msg" <<'EOF'
#!/usr/bin/env bash
# record/doc 的提交必须写明依据
MSG_FILE="$1"
TYPE=$(head -1 "$MSG_FILE" | sed -n 's/^\([a-z]*\)(.*/\1/p')

case "$TYPE" in
  record|doc)
    if ! grep -q '^依据:.*[^ ]' "$MSG_FILE"; then
      echo "❌ 提交被拒：$TYPE 类型必须写明「依据:」行" >&2
      echo "   例：依据: msg_8837, photo_221, JGJ130-2011#6.4.3" >&2
      exit 1
    fi
    ;;
esac
exit 0
EOF

cat > "$HOOKS/pre-commit" <<'EOF'
#!/usr/bin/env bash
# 禁止把密钥写进项目仓库
PATTERNS='sk-[A-Za-z0-9]{16,}|api[_-]?key[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9]{12,}|password[[:space:]]*[:=][[:space:]]*["'"'"']?[^"'"'"'[:space:]]{6,}'

HIT=$(git diff --cached -U0 | grep -Ei "$PATTERNS" || true)
if [ -n "$HIT" ]; then
  echo "❌ 提交被拒：检测到疑似密钥" >&2
  echo "$HIT" | head -3 >&2
  exit 1
fi
exit 0
EOF

chmod +x "$HOOKS/commit-msg" "$HOOKS/pre-commit"

git add -A
git commit -q -m "meta(init): 初始化项目工作区 · $PNAME

工程编号: ${PCODE:-待补}
项目 ID: $PID"

echo "✅ 工作区已建: $WS"
echo "   分支: $(git -C "$WS" branch --show-current)"
echo "   提交: $(git -C "$WS" log --oneline -1)"
echo "   钩子: commit-msg（依据校验） pre-commit（密钥扫描）"
