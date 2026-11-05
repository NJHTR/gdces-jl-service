# 计划

> 只写「要做什么、做到哪一步」。当前状态看 `STATUS.md`，为什么这么定看 `DECISIONS.md`。
> 每完成一项就把 `[ ]` 改成 `[x]`，并在 `STATUS.md` 记一笔。

## 里程碑

### M1 能跑（已完成）

- [x] easyCode 引擎在本机构建（离线 vendor + V8）
- [x] 项目工作区 + git + 提交钩子（文书必须带依据行）
- [x] MCP 工具层（项目/规范/文书），包成 `jl-tool` 命令行
- [x] BFF：鉴权 / bootstrap / chat / 任务 / 上传
- [x] MySQL 13 张表

### M2 能用（已完成）

- [x] 异步任务 + 轮询 + 断线续拉
- [x] 三层记忆（会话/项目/全局）+ 交叉去重
- [x] trace / cites / 文书卡片
- [x] 并发控制 + 每用户串行排队
- [x] 项目隔离（成员校验）

### M3 好用（已完成）

- [x] 执行过程两层折叠（Codex 式：叙述正文 + 命令行 +
      Shell 面板）
- [x] 主次分层：过程是旁注，答案才是气泡
- [x] 行内标注：规范号蓝色、待补暖色、数字加重（只用颜色和字重）
- [x] 回复清洗：去路径 / 去 URL 编码 / 去 markdown / 去过程叙述 / 限 160 字
- [x] 产物卡片按 `task.doc_id` 精确绑定（修掉"你好也挂通知单"）

### M4 工程化（进行中）

- [x] OpenAPI 契约（30 路径 / 36 操作，标 x-owner）
- [x] 后端拆包：core / biz / agent / api（单文件 2900 → 入口 5 行）
- [x] 补齐业务接口：文书详情与保存、任务取消、规范检索、审计、成员、影像、统计
- [x] doc 表收敛为单一写者（工具改调 `POST /docs/register`）
- [x] Java 骨架：Spring Boot + MyBatis，文书四接口，和 Python 共用同一个库
- [ ] **Python 的 biz 调用切到 Java**（`BIZ_BASE_URL` 开关，可灰度）
- [ ] Java 补完 project / member / audit / standard / stats
- [ ] 对象存储（上第二台机器前的硬前提）

### M5 上线前（未开始）

- [ ] `AUTH_REQUIRED=1` 并回归全部流程
- [ ] Redis：跨进程锁 + 任务队列（起第二个实例前必须）
- [ ] 备份演练（现在 cron 有 backup.sh，没验证过恢复）
- [ ] 监控：健康检查 + 错误告警 + 磁盘
- [ ] 压测：并发 10 用户时的排队表现

### M6 增强（未开始）

- [x] 语音转文字**路线已定**：阿里云百炼 `qwen-audio-3.0-asr-flash`（已实测）
      —— 原方案（微信插件 / 本机 whisper）作废
- [ ] 流式输出（首次响应从 8 秒降到 1~2 秒）
- [ ] 实时看板（Kafka 事件流）
- [ ] 电子签章 / PDF 导出

### M8 多模态接入（下一步）

- [ ] `message.parts` 数组：文字 / 图片 / 语音 / 定位，任意组合
- [ ] 上传音频 → 百炼 ASR 转写 → 作为 part 进上下文
- [ ] 看图能力接进工具：通用看图用 `qwen3.8-flash`，隐患识别用华工 `hazard_vlm_detection`
- [ ] 多张照片：只受物理上限约束，不设业务上限
- [ ] 前端：任意组合输入、多图预览


## 路线（为什么是这个顺序）

> 原则：**每一步都朝最终形态走，不做"先糊一版再推翻"。**
> 判断标准：这一步产出的东西，在最终版本里还需要吗？需要才做。

### R0 · 先验证地基（1 天）

**动手写任何东西之前，先回答三个问题**（见 `EASYCODE-CAPABILITIES.md` 第七节）：

- [ ] `spawn_agent` 真能派子 agent 吗（不通 → 多 agent 路线作废，退回单 agent）
- [ ] 子 agent 能收到图片/音频吗（决定多模态给谁）
- [ ] app-server 的 `thread/resume` 跨重启有效吗（决定断线续跑怎么做）

**为什么排第一**：这三个答案会改变后面所有设计。先问路，再修路。

### R1 · 多模态 parts（2 天）

消息模型从 `text + photos` 改成 `parts` 数组 —— **这就是最终形态，不是过渡**。
接百炼 ASR 转写、图片落工作区、前端任意组合输入。

### R2 · 把"后端 if"换成"工具 + 钩子"（3 天）

- 文书模板与 schema 放进项目工作区
- `validate_doc` 工具 = 提交钩子用的同一份 schema
- 删掉 `clean_reply` / `parse_answer` / `_cites_from_output` 这些替模型做判断的代码

**为什么在 R3 之前**：先把规则搬到数据和钩子里，agent 换运行方式时才不会丢东西。

### R3 · 常驻 agent server（3 天）

`thread/start` + `turn/start` + 事件流 SSE。首次响应从 8 秒降到 1~2 秒。
**只换执行方式，不改契约**。

### R4 · 多 agent 拓扑（4 天）

Orchestrator + Field/Reg/Draft/Review/Verify，`agent_max_depth=2`、`threads=6`。
文件交接、预算与强制收敛。

### R5 · 业务域收尾（3 天）

project / member / audit / standard 搬到 Java，Python 通过 HTTP 调。

### R6 · 上线前（3 天）

`AUTH_REQUIRED=1` 全流程回归、Redis、对象存储、监控、压测。

---

## 当前优先级


1. **多模态接入**（M8，能力已验证，趁热接）
2. 文书页接真数据（`GET /docs/:id` 已就绪，前端还在用假数据）
3. Python biz → Java 灰度切换（M4 收尾）
4. Java 补齐其余业务域

## 怎么判断一步做完了

不是"代码写了"，而是：

- 有实测输出（贴在提交信息或 `STATUS.md` 里）
- 契约（`contracts/openapi.yaml`）同步更新
- 已知坑写进 `DECISIONS.md` 或对应 README

### M7 Agent 自由化（设计完成，待实施）

> 详细设计见 `docs/ARCHITECTURE-AGENT.md`。
> 核心：后端只提供能力和工具，不做意图识别、不做正则、不做结构拆解。

- [ ] **P0 拆掉后端硬编码**：提示词搬进 AGENTS.md、删 clean_reply/parse_answer/
      _cites_from_output、删"按时间窗猜 doc_id"
- [ ] **P1 多模态 parts**：消息改成 parts 数组（文字/图片/语音/定位任意组合），
      ASR 与视觉作为工具
- [ ] **P2 常驻 Agent 运行时**：会话复用、SSE 直推、agent_event 表
- [ ] **P3 多 Agent**：spawn_agent / wait，角色与工具白名单，文件交接
- [ ] **P4 循环与校验**：validate_doc 与提交钩子共用 schema，Review⇄Draft 至多 3 轮
- [ ] **P5 观测与成本**：每 agent 的 token/耗时/工具次数，任务事件回放