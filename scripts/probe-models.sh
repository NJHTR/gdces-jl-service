#!/usr/bin/env bash
# 探测阿里云百炼上哪些模型适合做 agent（尤其是多 agent）
#
# 背景：多 agent 的子 agent 需要转发"加密推理内容"（encrypted_content），
#       百炼不支持 → 子 agent 起不来。
#       假设：如果主模型**不产生推理内容**，就没有 encrypted_content 要转发，
#             子 agent 也许能起来。
#
# 这个脚本做两件事：
#   1. 拉上游模型列表
#   2. 逐个模型问一句废话，看它是否返回 reasoning 内容、消耗多少 reasoning token
#
# 用法：bash scripts/probe-models.sh
# 输出：一张表，标出"无推理内容"的模型（这些是多 agent 的候选）

set -u
cd "$(dirname "$0")/.." || exit 1

KEY=$(grep -m1 '^DASHSCOPE_API_KEY=' .env | cut -d= -f2)
BASE=$(grep -m1 '^DASHSCOPE_BASE_URL=' .env | cut -d= -f2)
[ -z "${KEY:-}" ] && { echo "没读到 DASHSCOPE_API_KEY"; exit 1; }

echo "上游: $BASE"
echo

echo "=== 1. 模型清单 ==="
curl -s -m 30 "$BASE/models" -H "Authorization: Bearer $KEY" -o /tmp/models.json -w 'HTTP %{http_code}\n'
python3 - <<'PY'
import json
try:
    d = json.load(open('/tmp/models.json'))
except Exception:
    print("  （解析失败）"); raise SystemExit
ids = [m.get('id') for m in d.get('data', []) if isinstance(m, dict)]
print(f"  共 {len(ids)} 个")
for i in ids:
    print("   ", i)
PY

echo
echo "=== 2. 逐个探测推理行为 ==="
echo "   （判断标准：响应里有没有 reasoning 块 / reasoning_tokens 是否 > 0）"
echo

CANDIDATES=$(python3 -c "
import json
d=json.load(open('/tmp/models.json'))
ids=[m.get('id') or '' for m in d.get('data',[])]
# 只挑对话类模型，跳过图像/视频/音频
skip=('image','video','tts','asr','realtime','audio')
print(' '.join(i for i in ids if i and not any(s in i for s in skip)))
")

printf '  %-28s %-6s %-10s %-9s %s\n' "模型" "HTTP" "推理块" "推理token" "是否多agent候选"
printf '  %-28s %-6s %-10s %-9s %s\n' "----------------------------" "------" "----------" "---------" "----------------"

for m in $CANDIDATES; do
  code=$(curl -s -m 60 -o /tmp/probe.json -w '%{http_code}' \
    "$BASE/responses" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$m\",\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"说一个字：好\"}]}]}" 2>/dev/null)

  read -r has_reason r_tokens <<<"$(python3 - <<'PY'
import json
try:
    d = json.load(open('/tmp/probe.json'))
except Exception:
    print("? 0"); raise SystemExit
out = d.get('output') or []
has = any((it or {}).get('type') == 'reasoning' for it in out)
u = (d.get('usage') or {}).get('output_tokens_details') or {}
print(("有" if has else "无"), int(u.get('reasoning_tokens') or 0))
PY
)"

  if [ "$has_reason" = "无" ] && [ "$r_tokens" = "0" ] && [ "$code" = "200" ]; then
    verdict="✅ 候选"
  elif [ "$code" != "200" ]; then
    verdict="（不可用）"
  else
    verdict="有推理，跳过"
  fi
  printf '  %-28s %-6s %-10s %-9s %s\n' "$m" "$code" "$has_reason" "$r_tokens" "$verdict"
done

echo
echo "下一步：拿标 ✅ 的模型改 ~/.easycode/config.toml 的 model，然后跑："
echo "  bash scripts/probe-spawn.sh <模型名>"
