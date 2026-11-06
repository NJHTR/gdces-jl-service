# 终态架构（v1 · 定稿）

> 这份文档的作用：以后每加一个功能，先问「它归谁管」。归不清，就别加。
> 它不是重构计划——是**所有权契约**。现有代码按契约收敛，不推倒重来。

---

## 一、五条原则（不是口号，是可检查的规则）

```
1. 单一写者   一张表、一个文件、一份 git 历史，只能有一个模块写
2. 单一渲染   同一份内容只有一种排版实现（其他一律调用它）
3. 单一解析   同一个事件源只有一套解析（字段名以引擎实际协议为准）
4. 单一寻址   任何"出处"只有一种定位符写法（path:行-行 / scheme://...:1-1）
5. 删除优先   新实现上线时，旧实现当次删除；不允许"先留着"
```

违反任意一条，就是技术债的产生点。**巡检脚本（preflight）负责守住 1–4。**

---

## 二、所有权表（谁写什么，写清楚）

| 数据 | 唯一写者 | 位置 | 其他模块只能 |
|---|---|---|---|
文书文件（.md） | agent 工具 `create_doc_draft`（`tools/src/tools.js`） | `<ws>/docs/<date>/` | 读 |
文书元数据（doc 表） | `biz.handle_doc_register`（只此一条 INSERT） | MySQL `doc` | 读 |
字段补全/签发 | `biz.handle_doc_patch` | MySQL `doc` | 读 |
**文书渲染** | `tools/src/tools.js renderDoc()` | — | 不得二次排版 |
git 提交 | 工具 `workspace.gitCommit()`（文件类） / `biz.commit_doc` 仅用于**回写同一文件** | `<ws>/.git` | 不得另起新文件 |
照片索引 | `biz.index_photo()` | `<ws>/records/photos.md` | 读 |
项目规矩 | `biz.render_project_memory()` → `records/rules.md` + AGENTS.md | `<ws>` | 读 |
AGENTS.md | `biz.render_project_memory()` + `agent.render_agents_md()`（保留 human 段） | `<ws>/AGENTS.md` | 读 |
个人记忆 | 引擎（`homes/<uid>/memories/`）+ `handle_memory_create` 落笔记 | `<home>` | 读 |
引用账本 | `biz.record_citations` / `verify_citations_for_doc` | `citation` | 读 |
复核判定 | `handle_review_record`（经 `jl-tool record_review`） | `review` | 读 |
观察队列 | `biz.record_observations` | `observation` | 读（人改 status） |
权威导出 | `jobs.export_authoritative` | `kb/authoritative/` | 读 |
组织知识（kb） | 人（`scripts/import-materials.py` 导入） | `data/knowledge/kb/` | 只读挂载 |
规范库 | `knowledge.sync_standards()` | MySQL `standard` | 读 |
定时任务 | `jobs`（`service_job` 表 + `claim_due`） | MySQL | 只通过 `/jobs/<name>/run` 手动触发 |

**关键约束**：`doc` 表只有 `handle_doc_register`（INSERT）与 `handle_doc_patch`（UPDATE）两个写者；
`handle_doc_create` 必须下线（它绕过了守卫，还自己排版）。

---

## 三、租户模型（隔离靠目录，不靠 if）

```
user_id  →  homes/<uid>/            记忆、会话、技能、config.toml（引擎原生隔离）
project  →  data/workspaces/<pid>/   git、AGENTS.md、records/、docs/、.easycode/skills/
交叉     →  thread_map(project_id, user_id) → thread_id（一条线程一个人一个项目）
```

规则：

```
1. user 只从 token 取，永不接受 body 里的 userId
2. 任何带 projectId 的写操作必须过 guard_project(project_id, user)
3. 不允许 default_project_id() 兜底写操作（读可以用兜底，写不行）
4. 文件路径一律相对工作区；容器内用 /ws/<pid>，宿主用 <ROOT>/data/workspaces/<pid>
```

---

## 四、知识模型（四类，各有生命周期）

| 类 | 存什么 | 存哪 | 谁写 | 生命周期 |
|---|---|---|---|---|
组织知识 | 模板、示例、验收要点、参考 | `kb/`（只读挂载进工作区 `knowledge/`） | 人 | 长期 |
项目知识 | 文书、照片索引、项目规矩 | `<ws>/records/`、`<ws>/docs/`、`<ws>/AGENTS.md` | 工具 + biz | 项目期 |
个人记忆 | 偏好、纠正过的事 | `homes/<uid>/memories/` | 引擎 | 跨项目，几年 |
引用账本 | 谁引用了什么、确认几次 | `citation` | biz | 几年，会升权威 |

**冷启动** = `kb/` 预置（已有 186 个文件）+ `schemas/` + 技能；
**个性化积累** = 记忆（引擎整理）+ 账本（引用/确认）+ 观察（重复问题）。
三者不互相覆盖 —— 这决定了「越用越聪明」能不能跨年。

---

## 五、唯一生成流水线（禁止旁路）

```
小程序 → POST /chat（token 定用户）
  → guard_project → task(queued) → run_turn（ENGINE_MODE=appserver）
  → appserver.run_stream → 引擎读 AGENTS.md/技能/知识 → 调 jl-tool
  → 工具写文件 + git 提交 → BFF 登记 doc（唯一 INSERT，state 由 schema 决定）
  → 解析 memoryCitation + 工具账本 → message.cites
  → 复核（独立会话，must_fix/suggest/ok，经 record_review 落库）
  → task done（doc_id 必写）
```

任何绕过这条链的入口（`POST /docs`、直接改文件、客户端 mock）都应删掉。

---

## 六、引用与学习（已建成，别再改结构）

```
L1 记账：出稿 → used_count+1
L2 核验：复核 ok / 人签发 → verified_count+1（必须幂等：同 doc 只算一次）
L3 权威：verified≥5 且 used≥10 且 disputed=0 → 导出 kb/authoritative/（降级即删文件）
L4 观察：同 tag 反复出现 ≥3 次 → 人工队列（不自动改技能）
```

引用定位符统一：`knowledge/...:行-行`、`records/photos.md:行-行`、
`standard://CODE#CLAUSE:1-1`、`thread://...:1-1`、`web://域名:1-1`。

---

## 七、要删的（收敛第一步）

```
删：run_cli_turn / _parse_jsonl / relevance / get_or_create_thread /
    parse_multipart / STD_RE / core 里 7 个重复常量 / _cites_from_output
删：POST /api/v1/docs（handle_doc_create）、GET /members、/render-memory
删：do_GET 里永不成立的 POST 分支（且它调了不存在的 self._body）
改：readMemories() 改读 records/rules.md（memories/project.md 已废）
加：<ws>/.gitignore（.agent/）
```

---

## 八、收敛计划（4 批，一次到位，不重写）

```
批 1 止血（当天）
  · claim_due 用 rowcount 判断（调度器现在是死的）
  · export_authoritative 清理只删"不再权威"的文件（skipped 不能删）
  · do_DELETE/do_PATCH 补鉴权、路径统一 urlparse
  · handle_chat 用 token 用户
  · project_save / upload / photo_caption 补 guard_project，去掉写操作兜底
  · register 用 lastrowid
  · 签发幂等

批 2 单一化（一两天）
  · 文书渲染只留 tools.js 一份；commit_doc 改成"读原文件、替换字段行"，或改为不回写
  · 解析只留 _parse_cli_jsonl，且字段名用引擎实际协议（camelCase）
  · patch 也走 prepare_bases（依据派生一致）

批 3 删除（半天）
  · 第七节整段

批 4 产品
  · 天气接真实数据源（Open-Meteo，无 key），失败就不显示，绝不编
  · 照片水印去掉"编造的天气/地址"
```

---

## 九、防回归（这次是检查体系的空档）

**preflight 新检查（缺一不可）**：

```
A. 调度器活着：临时把某 job 的 next_run_at 拨到过去，claim_due 必须能领到
B. 单一写者：grep 全库，INSERT INTO doc 只允许出现在 handle_doc_register
C. 单一渲染：渲染函数只允许一处（tools/src/tools.js renderDoc）
D. 协议字段：所有解析点不得出现 aggregated_output / exit_code（下划线）
E. 端点守卫：每个 handler 首行必须有 guard_project（白名单：/healthz、/auth/*）
```

**smoke 新断言**：

```
· 一张照片上传 → 水印数据 source 必须为真实数据源或空，不得是 stub
· 一份文书改字段后，磁盘上同名文件数量不变（不能多出一份）
· 已签文书再 POST 一次 → verified_count 不变
```

---

## 十、我做的决定

**做，但不重写。** 理由是：现有链路（appserver + 工具 + 账本 + 复核 + 观察）是对的，
坏的只是「同一件事多做了一遍」。收敛比推倒便宜一个数量级，而且能保住已有数据与在跑的业务。

顺序：批 1 → 批 4 → 批 2 → 批 3。
（批 4 提前，因为**假天气写进证据照片**是唯一会伤到对外信用的问题。）
