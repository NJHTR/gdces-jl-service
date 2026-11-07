# LOOP · 双环 + 三闸 + 五件套（可执行版）

> 这份文档是**运行协议**，不是理念。每一轮只做一项，Verifier 独立验，验不过不算完成。
> 配套文件：`docs/state/loop.json`、`docs/receipts/*.json`、`scripts/loop-*.py|ps1`。

---

## 0. 读什么、不读什么

| 内容 | 每轮 | 何时读全文 |
|---|---|---|
`docs/receipts/<最新>.json` | ✅ | — |
`git diff --stat` + 变更文件 | ✅ | — |
门禁退出码（preflight/deploy/smoke/acceptance） | ✅ | 红时读 tail ≤200 行 |
新决策 / 新坑 / 待拍板 | ✅ | — |
失败命令的完整 stdout/stderr | ❌ | 红时读 |
成功路径完整日志 | ❌ | 每 5 轮抽 1 轮 |
模型推理/中间叙述 | ❌ | 每 10 轮审计抽 1 轮 |

Verifier **只读 diff + DoD + 回执**，不读 Builder 的叙述。

---

## 1. 角色（可一人分饰，但 Verifier 必须“新上下文”）

```
Planner  维护指针、写 DoD、只选一项
Builder  实现，不碰无关文件、不改服务器
Verifier 独立跑 DoD + 门禁，只读 diff，给 pass/fail + 证据
Scribe   写回执、BACKLOG 证据、commit message
Gate     preflight → deploy → smoke → acceptance → health
Human    拍板 / 删数据 / 改 schema / 改模型 / 生产变更
```

---

## 2. 状态机

```
INIT → SYNC → PICK → PLAN → BUILD → VERIFY ─┬─ PASS → RECORD → CHECKPOINT(每5轮) → PICK
                                            └─ FAIL → 修复(同因 ≤2 次) → 仍红 → STOP/BLOCKED
```

---

## 3. 每轮输入（固定 6 件）

```
1. docs/state/loop.json          # 指针/轮次/尝试次数
2. BACKLOG 的「当前指针」+「决策记录」
3. 最近 3 条回执
4. git status / git log -5
5. runtime/DEPLOYED + healthz
6. 待拍板 / 新坑（只读标题）
```

---

## 4. 每轮输出：回执（Builder 不能自己签 pass）

`docs/receipts/<YYYYMMDD-HHMM>-<item>.json`

```json
{
  "round": 1,
  "item": "P0-perf",
  "type": "IMPL",
  "commit": "abc1234",
  "files": ["bff/bffpkg/biz.py"],
  "dod": [
    {"check": "复核不再挡 done", "cmd": "python3 scripts/check-review-nonblock.py", "expect": "ok"},
    {"check": "负向：复核失败不影响 done", "cmd": "...", "expect": "0"}
  ],
  "gates": {"preflight": 0, "deploy": 0, "smoke": 0, "acceptance": 0, "negative": "pass"},
  "evidence": [{"cmd": "...", "exit": 0, "out_tail": "..."}],
  "decisions_needed": [],
  "risks": [],
  "next": "M1",
  "duration_s": 540,
  "verified_by": "verifier:independent",
  "verified_at": "2026-11-07T22:40:00+08:00"
}
```

验证器 v2 的硬约束（缺一条 fail）：

```
1. JL_VERIFIER_TOKEN 必须由外部注入（Builder 会话不放）—— 不能靠 --sign 字符串自称 verifier
2. 跨仓库项必须传多个 --commit（后端一个、小程序一个），每个 commit 都要有文件改动
3. DoD 至少一条负向（防止只测 happy path）
4. gates 必须含 preflight/deploy/smoke/acceptance 四个退出码，且 gate.head == 当前 HEAD
5. 回执带 base_commit + diff_hash，把"验的是哪段 diff"钉死
```

生成与签名：

```bash
python3 scripts/loop-verify.py --item P0-perf --commit HEAD \
  --dod dod.json --sign "verifier:independent"
```

`loop-verify.py` 只认 DoD 命令的退出码/期望输出；缺 `commit` 或 `files` 为空直接 fail。

---

## 5. 三闸（每道都要“接线 + 负向”）

| 门禁 | 正向 | 负向 |
|---|---|---|
preflight | 全绿 | 摘掉一项检查必须红；打印实际执行清单 |
deploy | 退出 0 | `-SimulateStale` 无 `JL_ALLOW_TEST_HOOKS=1` → 2；有 opt-in 无 `-Environment test` → 2；齐全 → 3 |
smoke | 全绿 + 残留 0 | `JL_SMOKE_PROJECT=yun-gu-3qi` → 2，且 doc 数不变 |
acceptance | 12 项全绿 | 造一条污染 → 必须红 |

**没有负向测试的门禁，一律算未完成。**

---

## 6. STOP / BLOCKED

```
STOP（写 BACKLOG 并停）：
- 同一原因连续失败 2 次
- 门禁红且一轮内无法解释
- 本项与 D-* 冲突
- 指针连续 2 轮未动
- 需要删/改生产数据、改 schema、改模型/提示词、动钱/账号
- 预算超限

BLOCKED（写「待拍板」）：
- 业务取舍（F3/D1b/B6.5）
- 需要人给文件/账号/域名
- 需要人确认真机/现场
```

---

## 7. 每 5 轮 Checkpoint（必做）

```
1. 指针只前进、无回退
2. 提交数 = 完成项数（无夹带）
3. 线上 DEPLOYED = 本地 HEAD（差距有解释）
4. preflight / smoke / acceptance 全绿
5. 数据健康：待补/孤儿/丢失/无描述 vs 上轮，异常增长要解释
6. 返工率：5 轮里“同一项修两次”>30% → 停下复盘
```

每 10 轮独立审计：Verifier 只看 diff + 回执重建结论，抽 1 项复跑。

---

## 8. 数据安全硬规则

```
· 不在服务器手改文件；只改本地 → deploy
· 测试不碰业务库/业务工作区（T6.5 未定前禁止新增破坏性测试）
· 删除/停用前：备份 + 快照 + 可恢复；写 quarantine，不 rm
· 数据变更在回执里带 before/after count
```

---

## 9. 反模式黑名单（已犯过）

```
✗ 检查写了没接线        ✗ 修已废弃的项（C8 vs D-04）
✗ 服务器手改            ✗ 测试同库 + LIKE 删除
✗ 数字手抄过期          ✗ 一个提交混多件事
✗ 自己总结当验收        ✗ 把 [x] 当完成
✗ 无限重试              ✗ 不读 diff 只读摘要
```

---

## 10. 预算

```
每项：最多 2 次修复，第 3 次停
每 Wave：≤6 项 或 3 天
每轮读取：≤2k tokens 结构化内容
模型/提示词/路由变更：单独一项，必须带 50 条评测
```

---

## 11. 命令

```bash
# 同步（只读）
python3 scripts/loop-sync.py

# 生成决策索引（BACKLOG D-* → JSON）
python3 scripts/loop-decisions.py

# 独立验证 + 写回执
python3 scripts/loop-verify.py --item <item> --commit HEAD --dod dod.json \
        --sign "verifier:independent"

# 门禁（本地，输出 JSON 汇总）
powershell -File scripts/loop-gate.ps1
```
