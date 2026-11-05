# Agent 架构 · 设计稿

> 这份文档回答一个问题：**怎么让 AI 真正"自己决定怎么做"，而不是后端替它决定。**
> 状态：设计稿，未实施。实施前先读完 `DECISIONS.md` 的 D1~D9。

---

## 一、现在的问题：Agent 被关在笼子里

当前的"后端"其实在替模型做决定：

| 现在后端干的事 | 本该谁干 | 后果 |
|---|---|---|
拼提示词（塞格式要求、工具说明） | 项目工作区的 `AGENTS.md` | 改一次话术要改代码 |
用正则抠规范条号 | 检索工具直接返回结构化的条号 | 条号格式一变就挂 |
解析「结论/依据/待补」三行 | 模型自己按模板写 | 模型不听话就得写兜底 |
`clean_reply()` 清洗路径/markdown | 模型不该产生这些；产生就重试 | 一堆正则维护成本 |
判断"这一轮有没有生成文书" | 模型自己调工具登记 | `doc_id` 靠时间窗猜（踩过坑） |
把进度拆成 steps 给前端 | 事件流本身就是进度 | 又一套状态机 |

**根因**：我们把"理解输入 → 决定动作 → 产出"这条链路，切成了
「模型只负责说话，后端负责一切结构」。这就是工作流，不是 agent。

**判断标准（写下来防止自欺）**：
> 如果一个需求来了，你的第一反应是"后端加个 if/正则/字段"，
> 那就说明设计错了，应该是"给 agent 一个能力"。

---

## 二、四条设计原则

### P1 · 后端只提供能力，不编排步骤

后端的职责收缩为四件事：

1. **身份与权限**：你是谁，能看哪些项目
2. **存储与转发**：图片/音频/消息/事件的落盘与推送
3. **工具网关**：把项目数据、规范库、文件系统、git 暴露成工具（MCP）
4. **追溯**：每一步都记事件，可回放

**不做的**：意图识别、格式约束、回复清洗、结构化拆解、条号提取。

### P2 · 多模态是一等公民，不是附件字段

现在：`message.photos = ["blobid", ...]`，`text` 是主角。
目标：**消息 = parts 数组**，任何模态都是平级的 part。

```json
{
  "parts": [
    {"type": "text",  "text": "这个地方连墙件少了两根"},
    {"type": "image", "blob": "sha256:ab12…", "w": 1080, "h": 1440,
     "exif": {"takenAt": "2026-11-05T09:12:03+08:00", "gps": [113.9, 22.5]}},
    {"type": "image", "blob": "sha256:cd34…", "w": 1080, "h": 1440},
    {"type": "audio", "blob": "sha256:ef56…", "dur": 6.4,
     "transcript": "四号楼东侧外架连墙件还差两根"},
    {"type": "location", "lat": 22.53, "lng": 113.94, "addr": "科技园北区"}
  ]
}
```

规则：
- **任意组合**：纯图、纯语音、图文、多图、语音+图+定位，都是合法的 parts
- **数量不设业务上限**（只有物理上限，比如单次 20 张）
- 语音的 `transcript` 只是**提示**，agent 仍然可以重新调 `transcribe` 校验
- 图片 agent 默认看到缩略图；要看细节就调 `crop_image` / `zoom_image`
- **后端不判断"这条是语音还是文字"** —— 它只是把 parts 递给 agent

### P3 · 产物是文件，不是字符串

文书不是后端拼出来的 JSON，而是 **agent 在项目工作区里写的一份 `.md`**：

```
workspace/yun-gu-3qi/
  AGENTS.md                      ← 项目作业指导（agent 每次读）
  templates/监理通知单.md          ← 带 {{字段}} 占位
  templates/监理通知单.schema.json  ← 字段定义、必填、枚举、状态机
  docs/2026-11-05/监理通知单-4#楼东侧外架-01.md
  memory/项目记忆.md
```

好处：
- 可以 `git diff`、可以 code review、可以回滚
- agent 之间**用文件交接**，不靠自然语言传话
- 提交钩子天然是规则的执行点（钩子校验失败 = 这次产出不合格）

### P4 · 循环和分支由 agent 决定，不由代码决定

"写完了要不要复核"、"要不要再查一次规范"、"要不要问用户" ——
这些判断**必须**在模型手里。后端只提供 `spawn_agent` / `delegate` 这类工具。

---

## 三、目标架构

```
┌──────────────────────────────────────────────────────────────┐
│                          小程序 / 后台                         │
└───────────────────────────┬──────────────────────────────────┘
                            │ parts（text/image/audio/location）
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  网关 BFF（Python）—— 只做四件事                                │
│  鉴权 · 存储转发 · 工具网关(MCP) · 事件追溯                      │
│                                                              │
│  不做：意图判断 / 格式约束 / 回复清洗 / 结构拆解                  │
└───────────────────────────┬──────────────────────────────────┘
                            │ 事件流（SSE）
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                 Agent 运行时（常驻，不是每次起子进程）             │
│                                                              │
│   Orchestrator（总监）                                         │
│     ├─ 并行 → Field Agent（现场：看图/听语音/整理观察）           │
│     ├─ 并行 → Reg Agent（查规范，返回带条号的引用）               │
│     ├─ 串行 → Draft Agent（按模板写文书文件）                     │
│     └─ 循环 → Review Agent（挑毛病）⇄ Draft Agent（改）          │
│                                                              │
│   每个 agent：独立的系统提示 + 工具白名单 + 独立上下文窗口          │
│   产物：工作区文件；交接：文件路径而不是文本                        │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  工具层（MCP）：项目数据 / 规范库 / 文件系统 / git / 语音 / 视觉   │
│  规则层：schema 校验 + 提交钩子（规则的唯一执行点）                │
└──────────────────────────────────────────────────────────────┘
```

---

## 四、多 Agent 怎么组织

### 4.1 角色与职责

| Agent | 输入 | 产出 | 工具白名单 |
|---|---|---|---|
**Orchestrator** | 用户 parts + 项目状态 | 决定派谁、汇总、对用户说话 | spawn/wait/read_file/list_dir |
**Field** | 图片、语音、定位 | `observations.md`（结构化观察：部位/问题/数量/照片引用） | transcribe / describe_image / crop_image / read_file / write_file |
**Reg** | 查询意图 | `cites.json`（标准号+条号+原文摘要） | search_standards / read_file / write_file |
**Draft** | 观察 + 依据 + 模板 | 文书 `.md` 文件 | read_file / write_file / list_dir |
**Review** | 草稿 + 依据 + 模板 schema | 问题清单（不通过就退回 Draft） | read_file / validate_doc / search_standards |
**Verify** | 文件 | 通过 / 缺什么 | validate_doc（只读，无写权限） |

关键：**Review 不能改文件**，只能提意见；**Draft 不能查规范原文以外的库**。
权限靠工具白名单实现，不靠提示词自觉。

### 4.2 拓扑：层级 + 分支 + 循环

```
用户消息
   │
   ▼
Orchestrator ──┬── 并行 ──▶ Field ──▶ observations.md
               ├── 并行 ──▶ Reg   ──▶ cites.json
               │
               ▼ （两者都回来才继续）
            Draft ──▶ 文书.md
               │
               ▼
            Review ──不通过──▶ 带着意见回到 Draft（最多 3 轮）
               │ 通过
               ▼
            Verify ──▶ 钩子提交 → git commit
               │
               ▼
Orchestrator 汇总 → 对用户说一句话
```

**这些箭头不是后端代码里的 if，是 Orchestrator 通过工具调用表达的意图。**
它可以在任何一步决定"不必查规范，直接问用户"，或者"这个任务简单，我自己写"。

### 4.3 Agent 之间怎么交接

**用文件，不用自然语言**：

```json
// observations.md 里的结构化部分
{
  "site": "4#楼东侧外架",
  "issues": [{"what": "连墙件缺失", "count": 2, "evidence": ["img:ab12", "img:cd34"]}],
  "confidence": 0.8,
  "unknown": ["具体轴线", "整改期限"]
}
```

好处：Draft 拿到的是确定的数据，不用从对话里猜；Review 能逐条对照。

---

## 五、数据模型改动

### 5.1 消息

```sql
-- 旧：text + photos(json)
-- 新：
ALTER TABLE message ADD COLUMN parts JSON NULL COMMENT '多模态内容项';
```

`parts` 是唯一的内容来源；`text` 保留为"纯文字摘要"，方便搜索和列表展示。

### 5.2 文件与文书

文书不再由后端生成：
- `doc` 表保留（索引、状态、权限），但**由 agent 调工具登记**，字段从文件解析
- 文件是真值，数据库是索引（对应 D4）

### 5.3 事件（追溯）

```sql
CREATE TABLE agent_event (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id BIGINT NOT NULL,
  agent_id VARCHAR(32) NOT NULL,        -- orchestrator / field-1 / draft-2
  parent_id VARCHAR(32) NULL,           -- 谁派生的
  kind VARCHAR(16) NOT NULL,            -- spawn / tool_call / message / finish
  payload JSON NOT NULL,
  ms INT NULL,
  created_at DATETIME(3) NOT NULL
);
```

**前端的过程折叠直接读这张表**，不再靠后端拼 progress。

---

## 六、工具目录（后端唯一的"业务暴露"）

| 工具 | 作用 | 谁能用 |
|---|---|---|
`get_project()` | 项目档案（名称/建设单位/地址/成员） | 全部 |
`read_file(path)` / `write_file(path, content)` / `list_dir(path)` | 工作区文件（沙箱限制在项目目录内） | 按角色 |
`search_records(query)` | 历史记录检索 | Field / Reg / Draft |
`search_standards(query)` | 规范检索，**返回 code+clause+原文** | Reg / Review |
`transcribe(blob)` | 语音转文字 | Field |
`describe_image(blob, question)` / `crop_image(blob, box)` | 看图 | Field |
`validate_doc(path)` | 按 schema 校验文书：必填、枚举、依据是否存在 | Review / Verify |
`commit(paths, message)` | git 提交（钩子在这里生效：没有依据行就拒） | Draft（经 Verify 后） |
`spawn_agent(role, task, inputs, budget)` / `wait(agent_id)` | 派生子 agent | Orchestrator |

**注意 `validate_doc` 和钩子用的是同一份 schema**：
规则只写一次，人和机器执行同一条。

---

## 七、循环与失败怎么处理（不靠后端 if）

| 情况 | 谁处理 | 怎么处理 |
|---|---|---|
文书缺字段 | Draft 自己判断 | 按模板留「待补」，不编造 |
Review 不通过 | Orchestrator | 带意见回 Draft，最多 3 轮 |
3 轮还不过 | Orchestrator | 降级：把草稿 + 问题清单交给用户，问要不要改 |
规范查不到 | Reg | 返回空并说明；Orchestrator 决定是否继续出单 |
照片看不清 | Field | 调 `crop_image` 再看一次；仍不行就在 observations 里标 `unknown` |
工具超时 | 运行时 | 返回错误给 agent，由它决定重试或换路 |
预算耗尽 | 运行时 | 强制收敛：让 Orchestrator 用现有信息产出并说明缺什么 |

**兜底的关键**：运行时给每个 agent 一个"预算"（token/时间/工具调用次数），
超了就强制收口。这不是 if，是资源约束。

---

## 八、实施路径（每一步都是"加能力"，不是"加分支"）

### P0 · 拆掉后端的硬编码（1 天，收益最大）

- [ ] 把 `build_context()` 里拼的提示词搬到 `workspace/AGENTS.md`
- [ ] 删掉 `clean_reply()` 的正则清洗，改成"不符合格式就重试一次"
- [ ] 删掉 `parse_answer()` / `rich_reply()` 的后端拆解，改由 Draft 按模板写
- [ ] 删掉 `_cites_from_output()` 的正则抠条号，改由 `search_standards` 返回结构化
- [ ] 删掉"按时间窗猜 doc_id"，改由工具返回并登记

**验收**：BFF 代码里不再有 `re.search(r"JGJ|GB|…")`、不再有 `clean_reply`。

### P1 · 多模态 parts（2 天）

- [ ] `message.parts` schema + 迁移
- [ ] 上传接口返回 blob 元信息（尺寸、时长、EXIF）
- [ ] ASR 工具（先接一个，`transcribe`）
- [ ] 视觉工具（`describe_image`）
- [ ] 前端：任意组合输入、多图

### P2 · 常驻 Agent 运行时（3 天）

- [ ] 用 easyCode 的 app-server 常驻（已有 `probe-appserver.py` 可复用）
- [ ] 会话复用（现在每轮起子进程，3 秒启动成本）
- [ ] 事件流 SSE 直推（前端不再轮询）
- [ ] `agent_event` 表 + 前端折叠改读它

### P3 · 多 Agent（3 天）

- [ ] `spawn_agent` / `wait` 工具
- [ ] 角色定义（系统提示 + 工具白名单）
- [ ] 文件交接协议
- [ ] Orchestrator 的汇总逻辑

### P4 · 循环与校验（2 天）

- [ ] `validate_doc` 与提交钩子共用 schema
- [ ] Review ⇄ Draft 循环（上限 3 轮）
- [ ] 预算与强制收敛

### P5 · 观测与成本（1 天）

- [ ] 每个 agent 的 token/耗时/工具调用次数
- [ ] 回放：一条任务的全部事件按时间轴重放

---

## 九、风险与反模式

**风险 1 · 成本和延迟爆炸**
多 agent 会让 token 翻几倍。对策：
- 分类/整理用便宜模型（Field/Reg），创作和复核用强模型（Draft/Review）
- Field 和 Reg 并行
- 简单任务允许 Orchestrator 不派生，自己干

**风险 2 · agent 自由 = 不可控**
对策：工具白名单 + 沙箱 + 预算 + 钩子校验。**自由在"怎么做"，不在"能不能越权"。**

**风险 3 · 幻觉条号**
对策：条号只能来自 `search_standards` 的返回值；钩子校验该条号在库里存在。
**模型不负责记条号，只负责引用。**

**风险 4 · 供应商锁定**
对策：模型调用走一个薄适配层（现在 `ENGINE_MODE` 已经有两套），
保留"换一个 provider 只改配置"的能力。

**反模式（明确不做）**
- ❌ 后端写 `if 有照片 then ... else ...`
- ❌ 后端用正则从模型输出里抠结构
- ❌ 用"提示词 + 兜底正则"两套规则
- ❌ 把 agent 的循环写成代码里的 for
- ❌ 为了省事把多 agent 降级成一个大 prompt

---

## 十、待定的三个选择（需要你定）

1. **语音在哪转写**
   - 入口预转写（快、简单，但后端多一步"流程"）
   - 作为工具由 Field 调（自由，但多一次往返）
   - 我的建议：**入口预转写 + 保留 `transcribe` 工具**（预转写只是加速，不剥夺 agent 复核的能力）

2. **常驻运行时用哪个**
   - easyCode 的 app-server（现成，但 Rust 侧要改配置）
   - 自己写一个 Python orchestrator 调模型 API（可控，但要重做工具调用/会话）
   - 我的建议：**先用 app-server**，因为它已经支持工具和会话

3. **多 agent 的模型分配**
   - 全部用同一个模型（简单，贵）
   - 分级（便宜做整理、强模型做创作）
   - 我的建议：**分级**，但先跑通再说，别一开始就优化
