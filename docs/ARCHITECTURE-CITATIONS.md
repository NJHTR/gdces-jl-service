# 引用与权威化（设计稿 · 终态）

> 目标：模型说的每一句有出处的话，都能点开看到来源；用得越久，被确认过的出处越权威。
> 约束：不造轮子、不写死提示词、不做后端语义解析、不下载标准全文。

---

## 一、引擎已经给了什么（核实过，不是猜）

| 能力 | 出处 | 现状 |
|---|---|---|
**引用块剥离** | `utils/stream-parser/src/citation.rs:11` `CITATION_OPEN = "<oai-mem-citation>"` | 从可见正文里摘掉，返回 `(visible_text, extracted)` |
**引用结构化** | `memories/read/src/citations.rs:6` | 解析成 `MemoryCitation{entries:[{location, note}], rollout_ids}` |
**挂到消息上** | app-server `item/completed` 的 `agentMessage.memoryCitation` | 客户端能拿到结构化引用 |
**引用写法已教给模型** | `ext/memories/templates/memories/read_path_v2.md` | 模板里就写着 `path:8-10\|note=[...]` |
**行内样式** | `core.py: rich_segments` 已有 `r`（ref）片段类型 | 前端能把某段渲染成引用样式 |

**结论：不需要新造引用格式。** 引擎的格式是「路径 + 行号 + 一句话备注」，天然适配我们所有来源。

---

## 二、五类来源，一套定位符

所有来源都落成 `path:line` 或 `scheme://...`，模型只写这一种：

| 来源 | 定位符 | 例子 |
|---|---|---|
**记忆** | 记忆文件路径 + 行 | `homes/1/memories/MEMORY.md:12-14` |
**知识文件** | 工作区内路径 + 行 | `knowledge/checkpoints/主体结构-·-混凝土结构.md:88` |
**影像** | 我们已经在写的索引 | `records/photos.md:12`（一行一张图：sha · 部位 · 时间 · 描述） |
**规范条号** | 伪路径（全文不入库） | `standard://JGJ 130-2011#6.4.3` |
**用户原话** | 会话内定位 | `thread://<threadId>/message/<id>` |
**网络资料** | 抓取快照 + 行（不引 URL，URL 会漂） | `webcache/<sha8>.md:10` |

**为什么规范要用伪路径**：工程建设标准没有官方免费全文（openstd 明确不含），
下载镜像站有版权风险。我们只存「条号 + ≤140 字摘录 + 出处」，引用指向条号本身。

**为什么网络资料要落快照**：网页会改、会 404。引用必须指向"我读到的那一版"，
所以抓取时把正文落成 `webcache/<sha>.md`，引用它的行号。这也是可复核的前提。

---

## 三、一个引用长什么样

模型在回复末尾（正文外）写：

```
<oai-mem-citation>
<citation_entries>
knowledge/checkpoints/主体结构-·-混凝土结构.md:88|note=[后植筋抗拔试验要求]
records/photos.md:12|note=[10:30 现场照片：连墙件缺失]
standard://JGJ 130-2011#6.4.3|note=[连墙件布置：偏离主节点≤300mm]
thread://019c6e27/message/512|note=[用户原话：外架连墙件缺失]
</citation_entries>
</oai-mem-citation>
```

引擎负责把它从正文剥离；BFF 负责：
1. 把 `location` 归一成 `{kind, ref, quote, note}`
2. 按出现顺序编号 `[1][2][3]`
3. 落 `message.cites`（已有字段），并累加 `citation` 表计数
4. 前端渲染：正文里的 `[n]` 变角标，底部列出处，点开看摘录

**正文里的 `[n]` 由模型自己写** —— 我们不解析、不猜、不纠正。
它写不写、写对没写对，是它的事；后端只负责把 `[n]` 渲染成角标。

---

## 四、权威化：用得多了才敢叫"依据"

### 数据模型

```sql
CREATE TABLE citation (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  source_kind   VARCHAR(16)  NOT NULL,   -- memory / file / photo / standard / thread / web
  locator       VARCHAR(512) NOT NULL,   -- 归一化后的定位符
  quote         VARCHAR(600) NULL,       -- 引用时抓到的原文片段（≤140 字）
  digest        CHAR(32)     NOT NULL,   -- md5(kind+locator+quote)，防重复
  used_count    INT NOT NULL DEFAULT 0,  -- 被引用次数
  verified_count INT NOT NULL DEFAULT 0, -- 被确认次数（复核通过 / 人签发）
  disputed      TINYINT NOT NULL DEFAULT 0, -- 同一条号出现不一致摘录
  status        VARCHAR(16) NOT NULL DEFAULT 'raw', -- raw / trusted / authoritative
  last_used_at  DATETIME NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_digest (digest),
  KEY idx_kind (source_kind), KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='引用账本';
```

### 三个状态与升级条件

```
raw            刚被引用过（默认）
trusted        verified_count ≥ 2 且 used_count ≥ 3 且 disputed=0
authoritative  verified_count ≥ 5 且 used_count ≥ 10 且 disputed=0
               且被写进 kb/authoritative/ 的一个摘录文件（有出处、有人负责）
```

**"确认"从哪来（不新增人工负担）**：

```
复核员判定「依据成立」     → 该条 +1 verified
人在小程序上签发文书        → 该文书 bases 里的条 +1 verified
人点开引用后点「这条对」     → +1 verified（可选的小交互）
人点「这条不对」            → disputed=1，进人工队列
```

### 冲突检测（这条最重要）

同一个 `locator` 出现**不同 quote**（例如同一条号，两次摘录不一致）→ `disputed=1`，
立即从 `trusted` 降回 `raw`，并记一条待人工核对。**权威化不是攒次数，是攒一致性。**

### 为什么这样算"权威"

- 出处是**文件 + 行号**，不是模型嘴里的一句话 —— 可回到原文核对
- 次数来自**真实使用**（出稿引用、复核确认、人签发），不是我们自己打分
- 冲突会降级 —— 用得再多，前后不一致就不算权威

---

## 五、联网：引擎自带 web_search，已实测可用

**先纠正我自己上一版的错误结论。** 之前写"百炼用不了 web_search"是错的。

实测（2026-11-06）：

```
给 config 加 web_search = "live"
→ 模型调了 web_search（item.type=webSearch，带 query）
→ 结果按 m.nmc.cn / trip.com 引用回答
→ 走服务也通：task 292，12 秒，trace 显示「联网搜索：深圳天气 2026年11月6日」
```

真实原因：`core/src/config/mod.rs:2741 resolve_web_search_mode`
—— 没有 `web_search` 键、也没开 `WebSearchCached` / `WebSearchRequest` 时返回 `None`，
**工具根本不出现**。我们线上 config 从来没写过这个键。

配置（已应用到主配置 + 52 个 home）：

```toml
web_search = "live"              # 顶层键，必须在第一个 [table] 之前

[tools.web_search]
search_context_size = "low"      # 一次搜索实测 22k input tokens，压小控成本
```

**已知限制**：`webSearch` item 的 `results` 为 `null` —— 搜索结果只给了模型，
**不回传客户端**（和命令输出同一个毛病）。所以：

```
网络来源的 URL 只能由模型自己写进引用块
我们无法核验它引的 URL 是否真在结果里 → 这类引用先标 raw，靠人工确认升级
```

不做的事：自己拼一个"搜索引擎"（没有搜索 API，拼出来是假搜索）；
也不抓全网站做爬虫（越界且不可维护）。

## 六、四个工作包（一次到位，不分期重写）

### W1 · 协议与解析（BFF 侧）

```
- 解析 <oai-mem-citation> 块 → [{kind, ref, quote, note, n}]
- 归一化六类 locator；standard:// 从库里取 title/std_title/body 前 140 字
- 落 message.cites；正文剥离交给引擎（已实现），BFF 不碰正文
- 写 citation 账本（used_count+1）；同 locator 不同 quote → disputed
验收：一次带 4 类来源的回答，cites 里 6 条、编号连续、每条有摘录
```

### W2 · 行内角标（前端 + 样式）

```
- rich_segments 增加一条规则：正文里的 [n]（n 是 1..9 的纯数字）渲染成角标片段
  （这是渲染规则，不涉及语义判断）
- 角标点击 = 打开对应引用（滚动/弹窗，复用现有 onOpenCite）
- 无引用的 [n] 不渲染成角标（避免误伤正文）
验收：正文出现 [1][2]，点 [2] 打开第 2 条
```

### W3 · 权威化（数据 + 提升）

```
- citation 表 + 升级/降级规则
- 复核判定「依据成立」→ verified_count+1
- 签发 → 该文书 bases 里的条 verified_count+1
- authoritative 的条目导出到 kb/authoritative/<slug>.md（标题带来源与确认次数）
验收：同一出处被引用 10 次、确认 5 次 → status=authoritative 且文件出现；
     人为制造一次不一致摘录 → disputed=1 且降回 raw
```

### W4 · 联网取用

```
- jl-tool fetch_url + webcache 落盘 + 引用行号
- 抓取失败要明说（不静默），超时/超长要有上限
验收：抓一个真实网页 → webcache 出现文件 → 回答引用 webcache/<sha>.md:行号
```

---

## 七、不做什么（写清楚，避免以后有人"顺手加"）

- ✗ 不下载标准全文（版权；且 openstd 明确不含工程建设标准）
- ✗ 不做向量库（We already have grep + 结构化 locator；Letta 的实测也是文件+grep 更强）
- ✗ 不在后端解析正文抠条号/抠 [n] 语义
- ✗ 不给"权威"打分写死权重（次数来自真实确认）
- ✗ 不把引用内容塞进提示词（引用是输出物，不是输入）

---

## 八、验收（可执行的断言）

```
1. 一次回答里引用记忆、知识文件、照片索引、规范条号、用户原话各 1 条
   → cites 5 条，编号 1..5，每条 locator 可打开
2. 正文里模型写了 [2] → 渲染成角标，点击打开第 2 条
3. 制造冲突：同 locator 两条不同 quote → disputed=1，status=raw
4. 复核通过 3 次 → verified_count=3；签发 2 份带该条 → verified_count=5 → authoritative
5. fetch_url 抓一个网页 → webcache 落盘 → 引用其行号
6. smoke-e2e 全绿；preflight 三条硬线不升
```
