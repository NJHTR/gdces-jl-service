# 文档索引（从这里开始）

> 接手这个项目的人（或 agent），**先读这一页**，再按下面的顺序看。
> 十分钟能知道：为什么做、现在到哪、下一步做什么、哪些坑别踩。

---

## 一、按顺序读这五份

| 顺序 | 文档 | 读它是为了知道 | 长度 |
|---|---|---|---|
1 | `GOAL.md` | 为什么做、什么算成功、**不做什么**、记忆的时序边界 | 短 |
2 | `STATUS.md` | 现在跑着什么、最近做完什么、**踩过哪些坑** | 长，看最后三节就够 |
3 | `PLAN.md` | 路线 R0~R8，哪些打勾了、哪些卡住 | 中 |
4 | `DECISIONS.md` | 每个取舍的**理由和推翻条件**（D1~D27） | 长，按需查 |
5 | `LOOP-PROMPT.md` | 每轮怎么干活（连续建造版） | 中 |

## 二、按主题查

### architecture（怎么拼的）

| 文档 | 内容 |
|---|---|
`ARCHITECTURE.md` | 总图：小程序 → BFF → 引擎 → 工作区/数据库 |
`ARCHITECTURE-AGENT.md` | agent 怎么被驱动（exec / app-server 两条路、事件、工具） |
`ARCHITECTURE-KNOWLEDGE.md` | **四层 scope**：组织/项目/个人/会话，知识怎么隔离 |
`ARCHITECTURE-LOOPS.md` | 定时任务的归属：哪些在服务器、哪些在开发环境 |

### 能力与坑

| 文档 | 内容 |
|---|---|
`EASYCODE-CAPABILITIES.md` | 引擎能力清单，🟢已核实 / 🟡只见符号 / 🔴未跑通 |
`STATUS.md` 的“踩过的坑”各节 | 每一条都是真踩过的，附错误原文 |

### 运行与运维

| 位置 | 内容 |
|---|---|
`scripts/progress.sh` | 一条命令看服务/数据/提交状态 |
`scripts/import-knowledge.py` | 把 `data/knowledge/standards/*.md` 同步进库 |
`docs/OPS.md` | 部署、备份、重启、排障 |

---

## 三、五个最容易踩的坑（先记住，省得重踩）

1. **`[features]` 必须放在 config.toml 所有顶层键之后**，否则吞掉 `model_provider`
2. **`memories = true` 必须写在 `[features]` 表里面**，追加到文件末尾会变成别的表的键
3. **引擎二进制有两个位置**：构建产物在 `easycode-rs/target/release/`，运行的是 `.target/release/`，别拷错
4. **MCP 工具容器在桥接网络**，BFF 必须监听 `0.0.0.0`，否则容器里 `fetch failed`（文书会静默不入库）
5. **记忆不是即时的**：会话闲置 ≥1 小时才抽取，整理还有 6 小时冷却

---

## 四、当前状态一句话

```
R0~R6 主线通（小程序 / BFF / 引擎 / 文书 / 多模态）
R7 知识架构 全通（四层隔离、提示词瘦身、知识包、图文语音沉淀）
R8 引擎记忆 全通（抽取 → 整理 → 注入，端到端验过）
最大短板：规范库只有 2 条条文，文书“依据”仍然空着
```

---

## 五、目录速查

```
/home/baize/jl-service
├── bff/bffpkg/          BFF（Python 标准库 + pymysql）
│   ├── api.py           路由
│   ├── biz.py           业务（文书/影像/规范/统计）
│   ├── agent.py         会话与出稿编排
│   ├── appserver.py     常驻引擎客户端
│   ├── jobs.py          服务侧定时任务
│   └── knowledge.py     知识包同步（standard 表唯一写者）
├── tools/src/           MCP 工具（node）
├── schemas/             文书字段定义（校验规则的唯一来源）
├── data/
│   ├── knowledge/       组织知识（规范/模板/技能，git 版本化）
│   ├── homes/<user>/    个人知识（引擎记忆、会话、技能）
│   └── workspaces/<project>/  项目知识（文书、影像、git）
├── patches/             引擎补丁 0001~0004
├── repos/easycode/      引擎源码 + 构建产物
└── docs/                本目录
```

## 查事实的工具

```
python3 scripts/whatison.py          # 路由 + 表列 + 路径权限
python3 scripts/preflight.py         # 提交前自检（引用/静默/硬编码）
```

**别猜路由名、列名、路径 —— 先跑这两个。**
