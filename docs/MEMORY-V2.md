# 记忆 v2 · 设计稿（2026-11-06）

> 状态：设计稿，待拍板 ⑦⑧⑨⑩ 后进入实现（BACKLOG Wave 3.5 / M0~M8）
> 目标：让“记住”这件事**可解释、可撤销、可度量**；切断三个正反馈
> 证据：BACKLOG「2026-11-06 17:30 记忆审计」（4 条近义、smoke 值、全局人设、project_id 全 NULL、179 vs 0）

---

## 0. 一句话

把记忆从「日志 + 点赞数」改成「**有作用域、有证据、有确认、有衰减的规则库**」；
排序看**有没有用**，不看**被搜到几次**。

---

## 1. 现状的五个病

| # | 病 | 现在的机制 | 后果 |
|---|---|---|---|
| 1 | 检索即强化 | `recall` 一次 `hits+1`，`ORDER BY hits DESC` | 被搜到 ≠ 有用；越搜越前，自我强化 |
| 2 | 学自己的输出 | 学习输入含 `assistant_text` + rollout 摘要 | 模型的错误/口头禅被固化成“规则” |
| 3 | 全量注入 + 自我模仿 | `memory_summary.md` 每轮整份进上下文 | 风格单调收敛，越用越像自己 |
| 4 | 老知识垄断 | 无时间窗口、无衰减、无上限、无探索 | 179 次的旧条永远压 0 次的新条，新规则进不了 top6 |
| 5 | 交互怪癖升人格 | 无 scope、无确认、置信度 = 重复次数 | 测试里一句“只回：记住”变成全局 User Profile |

三个正反馈的共同点：**系统把“出现/被搜到”当成了“有用”**。这是反直觉的根源。

---

## 2. 设计原则（不可协商）

- **P1 三分**：事实（project 表）/ 规则（记忆）/ 片段（会话）。
  只有“规则”进提示；事实用工具查；片段按需检索。**不复述事实，不记忆片段。**
- **P2 两阶段**：抽取 → 候选；确认 → 生效。**自动抽取永远不直接 active。**
- **P3 作用域显式**：默认 `project`；`global` 必须显式提升；**禁止 NULL = 全局**。
- **P4 来源必填**：每条记忆带 `source_task_id / source_message_id` + 用户原话摘录 + `created_by`。
- **P5 排序看效用**：`relevance × utility × decay + exploration`；**`hits` 退役**（改名 `injected_count`，只统计不排序）。
- **P6 注入预算**：top-k、token 上限、带作用域与来源；每轮写 `memory_use` 记账。
- **P7 三不学**：不从模型输出学；不从一次性内容学；不从“重复次数”推置信度。

---

## 3. 数据模型

```sql
memory_item (
  id BIGINT PK,
  kind ENUM('rule','style','fact_ref'),      -- rule=业务规矩；style=交互偏好；fact_ref=指向项目事实
  scope ENUM('global','project','project_user','user'),
  scope_id VARCHAR(64),                       -- project_id / user_id / 组合键
  project_id VARCHAR(32) NULL,
  canonical_text VARCHAR(512),
  status ENUM('candidate','active','superseded','archived','expired'),
  confidence DECIMAL(4,3) DEFAULT 0.5,
  created_by ENUM('user','model','import'),
  confirmed_by VARCHAR(64) NULL,
  source_task_id BIGINT NULL,
  source_message_id BIGINT NULL,
  evidence VARCHAR(1024) NULL,                -- 用户原话/编辑 diff 摘录
  supersedes_id BIGINT NULL,
  expires_at DATETIME NULL,                   -- style 默认 90 天；candidate 默认 14 天
  created_at / updated_at
)

memory_variant (id, item_id, text, embedding_ref)   -- 同一规则的不同说法
memory_use     (id, item_id, task_id, injected_at, rank, relevance, utility,
                outcome ENUM('helpful','harmful','ignored','unknown'))
memory_review  (id, item_id, action ENUM('accept','reject','merge'), by, at, note)
memory_audit   (id, item_id, action, before_json, after_json, by, at)
```

状态机：

```
candidate ──确认/显式记住──> active ──被新规则取代──> superseded
   │                          │
   └─14 天未确认─> expired     ├─低效用/超期─> archived
                               └─用户删除─> archived（保留审计）
```

**硬规则：`candidate` 永远不进提示。**

---

## 4. 写入路径

### 4.1 触发白名单

| 来源 | 动作 | 状态 |
|---|---|---|
| 用户显式「记住：…」 | 直接 active，`confirmed_by=user` | 唯一可绕过确认的入口 |
| 用户编辑文书字段（diff） | 规则候选（“把 X 改成 Y” → 规则） | candidate |
| 用户驳回/纠正（“不对，应该…”） | 规则候选 | candidate |
| 用户确认提案（C2） | active | confirmed |
| 模型自评 / rollout 摘要 | **禁止** | — |

### 4.2 过滤器（宁缺毋滥）

- 一次性内容：日期、时间、编号、数量、部位（`#楼`/`层`/`区`）、人名、电话 → **拒绝**
- 测试串：`smoke-`、`test-`、示例值 → 拒绝
- 长度 `<6` 或 `>200` 字 → 拒绝/截断
- 与 `project.meta` 事实重复 → 不记，改指向事实

### 4.3 归一化 + 去重合并

```
normalize(去标点/空格/语气词)
  → 编辑距离 + 分词 Jaccard + embedding cosine
  → cosine ≥ 0.86 或 Jaccard ≥ 0.7：合并进 canonical，variants 保留旧说法
  → 同 scope 同 topic 只留一条 canonical；旧条 superseded_by 指向新条
```

### 4.4 置信度

只来自**确认**（+1）与**结果**（见 §6），**不来自出现次数**。
“重复 4 次”不是证据，只是重复。

---

## 5. 读取与排序

### 5.1 召回

`scope` 过滤（project + project_user + 显式 global，**不再兜底 NULL**）
→ BM25（FULLTEXT / ES）+ 向量 top-50 → 合并去重。

### 5.2 重排（核心改动）

```
rank_i = rel_i × U_i × D_i + c·sqrt(ln N / n_i)

U_i = (helpful_i + 1) / (helpful_i + harmful_i + 2)        # Beta 后验均值
D_i = exp(−Δdays(last_helpful) / τ)                       # τ=30(rule) / 90(style)
c·sqrt(ln N / n_i)                                        # UCB 探索项；n_i=0 时强制给 1 个探索位
```

- `rel` 是**本轮 query** 的相关性，不是历史。
- `U` 是“用了之后有没有用”，不是“被搜到几次”。
- `D` 让**长期没帮上忙的旧条自然退场**；有用的旧条仍能留下。
- 探索项保证**新规则有出头的机会**，不然还是富者愈富。

### 5.3 注入

- `top-k = 3`（可配），总预算 ≤ 400 tokens
- 同一 topic 只取 1 条
- 每条带 `[scope/source]`，模型能判断“这是这个项目的，还是我个人跨项目的”
- **不注入 summary 全文**；summary 只是 UI 视图

### 5.4 记账

每轮写 `memory_use`（task_id、rank、rel、U、outcome=unknown），给 §6 用。

---

## 6. 反馈路径（回合结束后，异步）

| 信号 | outcome |
|---|---|
| 用户改掉/删掉与某条记忆相关的字段 | `harmful`（强） |
| 用户明确确认、接受提案、不改直接签发 | `helpful` |
| 注入了但用户没碰、后续也没引用 | `unknown`（不计分，但计入 n_i） |
| 用户删除/停用记忆 | `harmful`（强） |

- 更新 Beta 的 α/β；**不更新裸 hits**。
- 每周衰减 job：`D` 自然下降；每月 compaction：合并近义、归档低效用。
- 所有反馈写 `memory_audit`，可回放。

---

## 7. 作用域与「人格防火墙」

- 层级：`global < project < project_user < user`；**默认 project**；`user` 级要显式提升。
- `style`（交互偏好）**单独 kind**：
  - 每 scope 最多 2 条
  - 必须用户确认
  - 默认 90 天过期
  - **禁止从重复次数推断**；禁止把测试里的交互约定写进 User Profile
- 跨项目复用：只有用户显式「应用到所有项目」才提升为 `global`。

---

## 8. 引擎记忆的处置

现状：`homes/<uid>/memories/MEMORY.md` + `memory_summary.md` 自动注入摘要，用户不可见、不可删。

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | 关闭自动注入；引擎只产「候选摘要」，BFF 摄入 `memory_item(candidate, source=engine)`，用户确认后生效 | 切断“全量注入 + 自我模仿”，可审计 |
| B | 保留注入，但只读展示 + 删除，且限定 project cwd | 成本低，但“黑盒注入”还在 |

**无论哪种：`MEMORY.md` 不是权威，`memory_item` 才是。**
迁移时把 summary 拆成候选（`evidence=原文`），**不自动 active**。

---

## 9. 迁移（现有 38 条 + 501 的引擎记忆）

1. 建表（新旧并存，可回滚）
2. 全量降级 `candidate`，`created_by=import`
3. 回填 scope：`project→project`；`user+project_id→project_user`；`NULL→` 待定（人工）
4. 去重合并（脚本 + 人工过 top 50）
5. 清测试污染：`smoke-*`、`只回：记住`、4 条「依据条号」近义
6. **影子读**：新旧排序并行 1 周，比较注入差异
7. 切读；8. 开写入门禁；9. 旧字段保留一个版本再删

---

## 10. 指标（必须能回答“记忆到底有没有用”）

| 指标 | 定义 | 目标 |
|---|---|---|
| 注入精度 | 注入后 7 天内被删/停用比例 | < 10% |
| 规则应用率 | 含该规则的文书比例（抽样 + LLM judge） | 逐条可查 |
| 纠正率 | 每 100 轮用户纠正次数（v2 vs 10% 无记忆 holdout） | 下降 |
| 重复率 | 近义簇 / active 条数 | < 10% |
| 漂移 | 20 条固定 prompt，记忆开/关输出差异 | 可控 |
| 成本 | 记忆 tokens / 总 tokens | < 10% |

---

## 11. 实施顺序（对应 BACKLOG M0~M8）

| 步骤 | 内容 | 验收 |
|---|---|---|
| M0 | 设计定稿 + 拍板 ⑦⑧⑨ | 本文档签字 |
| M1 | 表 + 状态机 + 迁移脚本 | 双写不丢、可回滚 |
| M2 | 写入闸门（候选/白名单/过滤/去重） | 4 条近义只进 1 条 |
| M3 | 读取改造（scope/召回/重排/记账，影子模式） | 新旧排序差异可解释 |
| M4 | 反馈 + 衰减 + compaction | useful 的升、没用的降 |
| M5 | style 防火墙 | 测试串/交互怪癖进不了 active |
| M6 | 引擎记忆处置（A 或 B） | summary 不再黑盒注入 |
| M7 | 迁移 + 评测（50 条对照） | 指标达标 |
| M8 | 清理测试污染 | 审计里 0 条 smoke/近义 |

---

## 12. 风险与取舍

- **候选堆积**：用户不确认 → 14 天自动过期，不打扰。
- **冷启动**：允许“本轮无记忆”，不硬凑。
- **旧数据脏**：迁移只升 20 条高置信，其余归档，不追求全量。
- **过度工程**：M1~M3 是核心；M4/M7 可以后置，但 M2 不做，后面全是脏数据。
