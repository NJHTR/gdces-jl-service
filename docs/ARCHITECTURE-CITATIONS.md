# 引用与权威化（设计稿 v2 · 定稿）

> 上一版有两个错：① 说联网不可用（错，是配置没开）② 设计了两套引用通道。
> 这一版重新看了一遍现状，改成**一套机制**，并把结论落到可执行。

---

## 一、现状体检（2026-11-06 11:35 实测）

| 项 | 事实 | 判断 |
|---|---|---|
多用户隔离 | 52 个 `EASYCODE_HOME`，记忆/会话/技能各自独立 | ✅ 成立 |
项目隔离 | 3 个 workspace，各有 git、AGENTS.md、.easycode/skills | ✅ 成立 |
组织知识 | `data/knowledge/kb/`：模板 27 / 示例 16 / 验收要点 124 / 参考 4 | ✅ 有货 |
规范条文 | `standard` 表 2 条（历史数据，未复核） | ⚠️ 太少 |
AGENTS.md | 942 / 1207 / 809 字节（yun-gu-3qi 略超 1200） | ⚠️ 略胖 |
技能 | 4 个 SKILL.md 装进 3 个项目 + 60 个 home；模型能报出名字 | ✅ 生效 |
记忆 | `memory_summary` 只有 **1 个** home 有 → 其余 51 个用户拿不到记忆指令 | ⚠️ 未启用 |
项目规矩 | `records/rules.md` 只有 test-sandbox 有 | ⚠️ 空跑 |
联网 | 52 个 home 开了 `web_search = "live"`；task 292 实测会搜 | ✅ 可用 |
引用 | `message.cites` 有 20 条（全部来自工具侧 cites.jsonl） | ⚠️ 单通道 |
架构债 | `cites.jsonl`（工具写）+ 引擎 `memoryCitation` 两套 | 🔴 要合并 |

---

## 二、关键发现：引擎已经能做"角标引用"，只是没人用

实测（user 971，真实跑过）：

```
模型在回复末尾写：
<oai-mem-citation>
<citation_entries>
knowledge/checkpoints/主体结构-·-混凝土结构.md:1-30|note=[读了前30行…]
</citation_entries>
<rollout_ids></rollout_ids>
</oai-mem-citation>

引擎返回的事件里：
"memoryCitation": {"entries":[{"path":"knowledge/checkpoints/主体结构-·-混凝土结构.md",
  "lineStart":1,"lineEnd":30,"note":"…"}],"threadIds":[]}
```

正文里的这个块**会被剥掉**（`utils/stream-parser/src/citation.rs`），客户端拿到的是
干净正文 + 结构化 entries。这就是"论文式脚注"的天然实现。

**硬约束（源码 `memories/read/src/citations.rs:53`）**：

```rust
let (path, line_range) = location.rsplit_once(':')?;   // 必须有冒号
let (start, end) = line_range.split_once('-')?;        // 后半必须是 数字-数字
let line_start: u32 = start.parse().ok()?;             // 两个 u32
```

所以**所有来源都必须编码成「路径:起始行-结束行」**。这一条决定了下面六类怎么写。

---

## 三、六类来源，一种编码

| 来源 | 编码 | 例 |
|---|---|---|
文件/知识 | 相对工作区路径 + 真行号 | `knowledge/checkpoints/主体结构-·-混凝土结构.md:88-90` |
影像 | 照片索引行 | `records/photos.md:12-12` |
记忆 | 记忆文件行 | `homes/1/memories/MEMORY.md:12-14` |
规范条号 | 伪路径 + `:1-1` 占位（行号无意义） | `standard://JGJ 130-2011#6.4.3:1-1` |
用户原话 | 会话伪路径 + `:1-1` | `thread://<threadId>/message/512:1-1` |
网络资料 | 域名伪路径 + `:1-1` | `web://m.nmc.cn/weather:1-1` |

**为什么规范用伪路径**：工程建设标准没有官方免费全文（openstd 明确不含），
下载镜像站有版权风险。我们只存「条号 + 摘录」，引用指向条号本身。

**为什么网络来源是 `web://`**：`webSearch` item 的 `results` 为 `null`，
搜索结果只进了模型上下文、不回传客户端 —— 我们**没法核验它读了哪个 URL**。
所以这类型先记 `raw`，靠人工确认升级；这是诚实的做法，不是假装能核验。

---

## 四、角标：模型自己写，后端只做样式

```
模型在正文里写 [1] [2]（顺序与引用块 entries 顺序一致）
后端：rich_segments 增加一条渲染规则把 [n] 变成角标（纯样式，不判断语义）
      —— 正文里没有 [n] 也不报错，底部照样列出出处
前端：底部按 entries 顺序列 [1] [2]，点开看 note + 来源定位
```

**不做**：用正则从正文里找"第 X 条"去反推引用 —— 那是后端替模型判断。

---

## 五、两路证据合并（这是唯一的例外，且有理由）

```
模型声明：<citation_entries>（覆盖六类，但它可能漏、可能编）
工具实测：search_standards 命中（规范条号最可靠的证据，工具用自己的查询换来的）
```

合并规则：

```
按 (kind, ref) 去重；
同一 ref 两路都有 → 取工具的 quote（更可信），note 用模型的（更贴语境）；
只有一路 → 照样记，但标记 source_of_truth = model / tool；
同一 ref 两路 quote 不一致 → disputed=1，降级 raw，进人工队列
```

为什么保留两路：**规范条号的事实来源是数据库**，不是模型记忆；
而知识文件/影像/记忆的路径只有模型知道它读了哪几行。二者不可互相替代。

---

## 六、权威化：一致性 > 次数

```sql
citation(
  id, source_kind, ref, quote, digest UNIQUE,
  used_count, verified_count, disputed, status(raw/trusted/authoritative),
  last_used_at, created_at
)
```

```
raw          默认
trusted      verified_count ≥ 2 且 used_count ≥ 3 且 disputed=0
authoritative verified_count ≥ 5 且 used_count ≥ 10 且 disputed=0
             + 导出到 kb/authoritative/<slug>.md（带出处与确认次数）
```

确认信号（不新增人工负担）：

```
复核员判定「依据成立」   → +1 verified
人在小程序签发该文书      → 该文书 bases 里的条 +1 verified
人点开引用点「对/不对」   → +1 verified / disputed=1（可选）
```

冲突即降级：同 ref 不同 quote → disputed=1 → 回 raw。**攒一致性，不攒次数。**

---

## 七、工作包（一次到位）

| 包 | 内容 | 依赖 |
|---|---|---|
**W1** | BFF 消费 `memoryCitation.entries` → 六类归一 → `message.cites`；空则用工具侧账本兜底 | 无 |
**W2** | `rich_segments` 认 `[n]` → 角标；前端底部列表 + 点开 | W1 |
**W3** | `citation` 账本 + 升级/降级 + 复核/签发作为确认 | W1 |
**W4** | 技能 `jl-cite`：教模型写引用块（按需读，不进 AGENTS.md） | W1 |
**W5** | 规范伪路径的 quote 从库里取（≤140 字）+ `allowed_domains` 可选收紧 | 无 |

**注意 W4 的位置**：引用格式属于"流程"，放技能、按需读；
**不写进 AGENTS.md**（那里的判据是：加工具/换模型会不会失效——引用格式会）。

---

## 八、验收断言

```
1. 一条回答同时引用：知识文件、照片索引、规范条号、记忆 → cites 4 条、编号连续
2. 正文写 [2] → 渲染成角标；点开是第 2 条
3. 模型漏写引用块 → 底部仍有工具实测的规范条号（兜底生效）
4. 同 ref 两种 quote → disputed=1、status 降 raw
5. 复核通过 2 次 + 出稿引用 3 次 → trusted；签发 3 次 → authoritative 且导出文件
6. 引用块被引擎剥掉：正文里看不到 <oai-mem-citation>（引擎已保证）
7. smoke-e2e 全绿；preflight 三条硬线不升
```

---

## 九、这版明确不做

```
✗ 自建 citation 解析器（引擎已有 strip + parse）
✗ 后端正则从正文抠出处
✗ 向量库
✗ 下载标准全文
✗ 用搜索结果"自证"（results 不回传，核验不了就标 raw）
✗ 把引用格式写进 AGENTS.md
```
