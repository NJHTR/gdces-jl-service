# 监理 AI 服务 · 最终架构

> 本文是**最终形态**的设计，不是过渡方案。开发从这份设计开始，不先做玩具。

## 一句话

小程序是遥控器，服务器是施工现场的"云端电脑"：发一句话，它在项目工作区里查资料、写文书、留痕，产出可追溯到每一条依据、每一次工具调用、每一次文件改动。

## 部署环境

| 项 | 实际情况 |
|---|---|
| 机器 | Dell R640（`baize-R640`）· Ubuntu 26.04 · 96 核 · 247 GB · 3.3 TB（用 2%） |
| GPU | 无 → 模型走云端 API |
| Docker | 29.1.3 + Compose 2.40.3，镜像加速已配 |
| sudo | **无免密** → 一切服务容器化或用户态 |
| 根目录 | `/home/baize/jl-service/`（唯一入口，不散落） |
| 共享性 | 机器上跑着 `rokid-*` 生产容器 → 严格隔离，独立网络与数据 |

## 总结构

```
小程序（遥控器）
   │ CHAT-1  POST /api/v1/chat
   ▼
BFF（Node）· 8081
   ├ 鉴权 / 多租户 / 契约翻译
   ├ 任务队列（异步，断网续跑）
   └ 落库
   │ thread/start(cwd=项目工作区) + turn/start
   ▼
Agent 引擎（easyCode 改造）· 8082 常驻
   ├ 通用 loop（模型自主决定调什么工具）
   ├ 沙箱内执行 shell / 文件 / MCP 工具
   └ 上下文压缩、turn 打断、fork
   │
   ├── /home/baize/jl-service/data/workspaces/{pid}/   项目 git 仓库
   ├── /home/baize/jl-service/data/blobs/{sha256}      图片音频（内容寻址）
   └── MySQL :3309                                     记录/文书/记忆/任务/审计
```

## 五个支柱

### 1. 项目 = 一个 git 仓库

每次产出 = 一次提交，commit message 必带 `依据:`。
**钩子强制**：`record`/`doc` 类型没有依据行直接拒绝提交。

详见 [WORKSPACE.md](./WORKSPACE.md)。

### 2. 记忆三层

| 层 | 归属 | 例子 |
|---|---|---|
个人记忆 | 用户 × 跨项目 | 「结论写经检查符合要求」 |
项目记忆 | 项目 × 全体 | 「本单位叫 m³ 不叫方」 |
会话记忆 | thread 上下文 | 本次对话，自动压缩 |

A 的会话 = A 的个人记忆 + 项目记忆
B 的会话 = B 的个人记忆 + 同一个项目记忆

**冲突优先级：项目 > 个人**，且必须在 trace 里说明原因。

Source of truth 是 MySQL，渲染成 `memories/project.md` 给 agent 读。

### 3. 异步任务（云电脑的关键）

```
queued → running → done / failed / cancelled
```

- 用户发完就关小程序 → 任务继续跑
- 断网重发靠 `client_id` 幂等
- 回来拉增量 + 订阅消息

### 4. 可追溯四件套

| 件 | 存哪 | 回答什么 |
|---|---|---|
`trace` | messages.trace | 它读了什么、查了什么、多久 |
`bases` | docs.bases | 文书引用了什么（带 ref） |
`commit` | git | 改了哪个文件、为什么 |
`audit` | audit_log | 谁触发的、谁改的 |

从文书反查：`docs.git_commit` → commit → 依据行 → msg/photo/规范 → 原文
从消息正查：`messages.trace` → 工具调用 → 产出文件 → commit

### 5. 工具层独立于引擎

11 个工具走 MCP：

```
search_standards / get_standard / search_records / get_record
get_photos / get_memories / get_project / get_weather
web_search / create_doc_draft / ask_user
```

**换引擎不用改工具**。这是分层最关键的一条。

## 安全边界

放开 shell 之后，边界写成规则而不是靠自觉：

| 面 | 规则 |
|---|---|
目录 | 只可写本项目的 `data/workspaces/{pid}`；共享目录只读；跨项目不可见 |
网络 | 默认禁；白名单（模型 API / 规范库 / 地图天气）；联网需申请并留痕 |
资源 | 不设硬配额，留安全上限（防异常膨胀挤占他人）；单任务 120s |
凭证 | 不落盘、不进 git；环境变量注入；pre-commit 扫密钥 |
分支 | AI 只提交到 `agent/*`，永不直接改 `main` |
数据 | 图片文本不外发；模型调用留 prompt 摘要供审计 |

## 里程碑

| # | 里程碑 | 交付物 | 状态 |
|---|---|---|---|
1 | 工作区与版本 | 目录规范 · 初始化脚本 · git 钩子 · 演示项目 | ✅ 已完成 |
2 | 数据与工具 | MySQL 建表 · MCP 工具服务（11 个） | 待做 |
3 | 链路打通 | BFF + 一个 agent daemon，跑通"一句话 → 工具 → 回复 + trace" | 待做 |
4 | 多租户与记忆 | 项目/用户隔离 · 记忆三层 · 渲染成文件 | 待做 |
5 | 异步与后台 | 任务队列 · 断网续跑 · 订阅通知 · 管理后台 | 待做 |

每个里程碑都是**生产形态**，不留"以后再改"的坑。

## 端口约定

| 端口 | 用途 |
|---|---|
3309 | 本项目 MySQL |
8081 | BFF |
8082 | agent 引擎（仅本机） |
8083 | 管理后台（后续） |

## 不做的事

- 本地大模型（无 GPU）
- 多租户资源共享（公司内部，一家一个 workspace）
- 流式打字效果（异步任务 + 通知才是正确形态）
