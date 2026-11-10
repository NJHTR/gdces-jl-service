# 记忆架构验收文档（MEMORY-ACCEPTANCE）

> 适用范围：jl-service BFF 的记忆候选（Phase 1）与个人域摘要 / 项目域重渲染（Phase 2），
> 以及旧 `memory` 表、项目规矩提案、文件视图、权限与回滚。
> 契约源：`contracts/openapi.yaml`（记忆与规矩相关路由已补齐）。
> 基线：Phase1 `777ddba` → Phase2 `a254c9c` → 权限修复 `5c271f9` → 收口文档 `fe8331d`；本文档随 task-7 收口提交。
> 线上：`rokid-r640:/home/baize/jl-service`，BFF `0.0.0.0:8081`，MySQL `127.0.0.1:3309/jl`。

---

## 0. 一句话结论

- **能**：在"候选 → 人工确认 → 生效"的前提下，让项目规矩、个人习惯分层落账，并渲染成 agent 可读文件；
  个人域可确定性去重/摘要；项目域可幂等重渲染；所有确认动作留 audit。
- **不能**（本阶段刻意不做）：自动把模型输出当事实；自动合并/裁决冲突；auto promote 到项目或全局；
  用 LLM 做个人摘要 consolidate（仅预留）。
- 硬规则：**pending 永远无效**——不进 `memory`、不渲染文件、不进提示；会话（task/message）只作 provenance。

---

## 1. 分层与作用域（L0~L4）

后端口径里，长期记忆只有 **L1 / L3 / L4** 三类落 `memory` 表；**L0 组织知识**和 **L2 项目证据**不落记忆表。会话/任务只作 provenance，不单独建记忆作用域：

| 层 | 名称 | 语义 | 存储 | 谁能确认 | 现状 |
|---|---|---|---|---|---|
| L0 | 组织知识 | 模板、规范、公司制度、验收要点；最高权威 | `knowledge/`（只读软链）+ `standard` 表 | 组织知识管理员 / git | 已存在 |
| L1 | 项目共享 | 这个项目所有人生效的规矩 | `memory(scope='project', owner_id=<project_id>, project_id=NULL)` | 总监 / owner / admin | **已实现** |
| L2 | 项目事实/证据 | 项目信息、照片、文书、记录、图纸 | `project` / `photo` / `doc` + `records/photos`、`docs/` | 现场采集 / 项目管理员 | 已存在，不是记忆 |
| L3 | 个人 × 本项目 | 这个人在这个项目的习惯 | `memory(scope='user', owner_id=<uid>, project_id=<pid>)` | 本人 | **已实现** |
| L4 | 个人跨项目 | 这个人所有项目通用的偏好 | `memory(scope='user', owner_id=<uid>, project_id=NULL)` | 本人 | **已实现** |

会话/任务不是 L0，也不是记忆层：`task` / `message` 只作为候选/提案的 provenance（`source_task_id` / `source_message_id`）。

关键编码事实（读代码时别被 ENUM 骗了）：

- `memory.scope` 只有两个值 `user|project`；**三档语义**用 `project_id` 是否为空区分：
  - `scope='project'` → L1；
  - `scope='user' && project_id=<pid>` → L3；
  - `scope='user' && project_id IS NULL` → L4。
- 候选表 `memory_candidate.scope` 允许 `user|project_user|project`，与上述对应关系：
  `project_user` 确认后写 `memory(scope='user', project_id=<pid>)`。
- `applies_to` 列存人读的作用域串：`project:<pid>` / `user:<uid>` / `user:<uid>@project:<pid>`。
- 会话仅 provenance：候选/提案的 `source_ref`（`candidate:<id>` / `proposal:<id>`）指向来源；
  任何情况下**不会因为出现/重复/被搜到而把内容变成规则**。

---

## 2. 单一写者与文件布局

### 2.1 账本（唯一真值）

| 表 | 作用 | 写入方 |
|---|---|---|
| `memory` | 生效记忆账本（L1/L3/L4） | 候选 accept、规矩提案 accept、旧 `/memories` 手动写（个人域） |
| `memory_candidate` | 待确认候选（Phase1） | Agent/服务端抽取、API POST；**accept/reject 只改状态** |
| `rule_proposal` | 项目规矩提案（旧路径，仍在用） | 旧 `/memories scope=project`、agent 工具、`POST /rule-proposals` |
| `audit_log` | 谁在哪个项目对哪条做了什么 | 创建/接受/拒绝/修改/删除 |

一条内容从候选到生效只经过：`memory_candidate/rule_proposal`（pending）→ 人确认 → `memory`（active）。

### 2.2 渲染视图（文件不是账本，可整份删了重建）

| 文件 | 内容 | 生成函数 | 幂等 |
|---|---|---|---|
| `data/workspaces/<pid>/records/rules.md` | L1 项目规矩（`memory` active 项目行） | `biz.render_rules_md` / `memorysum.render_project_rules` | 内容不变不写 |
| `data/memory/users/<uid>/MEMORY.md` | L4 跨项目个人记忆视图 | `biz.render_user_memory` | 是 |
| `data/memory/users/<uid>/projects/<pid>.md` | L3 个人×项目视图（`JL_ENGINE_MEMORY=0` 时） | `biz.render_user_project_md` | 是 |
| `data/homes/<uid>/memories/projects/<pid>.md` | L3 视图（线上 `JL_ENGINE_MEMORY=1` 时落这里） | 同上 | 是 |
| `data/memory/users/<uid>/memory_summary.md` | 个人域确定性摘要（Phase2） | `memorysum.render_user_summary` | 内容不变不写 |
| `data/workspaces/<pid>/AGENTS.md` | 只放**指针**（rules/photos/MEMORY/summary 路径）+ 人工区 | `agent.render_agents_md`（`build_context`） | 每次重写 generated 区，human 区保留 |
| `data/homes/<uid>/memories/extensions/ad_hoc/notes/*.md` | 旧路径：无 projectId 的个人记忆备注 | `handle_memory_create`（历史行为） | 否（测试需清理） |

`AGENTS.md` 只放指针：实测"记忆摘要"只出现路径，不注入摘要正文（QA 已验）。
同一指针只出现一次（L1 rules.md / photos.md 去重，`5c271f9` 修复）。

---

## 3. 写入路径与审批矩阵

| 入口 | scope/对象 | 谁可以 | 直接写 memory？ | 副作用 |
|---|---|---|---|---|
| `POST /api/v1/memory-candidates` | user / project_user / project | 登录用户只能代表自己；项目成员 | **否**，只写 pending | 无（不渲染、不注入） |
| `POST /api/v1/memory-candidates/<id>/accept` | user / project_user | 本人 | 是（`source='confirmed'`） | 重渲染 MEMORY.md / 个人×项目文件 + 刷新摘要 + audit |
| 同上 | project | 总监/owner/admin（有角色行则严格判，**含演示模式**） | 是（`scope='project'`, `source='confirmed'`） | `render_rules_md` + audit |
| `POST /api/v1/memory-candidates/<id>/reject` | 全部 | 个人=本人；项目=成员 | 否 | 只改状态 + audit |
| `POST /api/v1/rule-proposals` | L1 提案 | 项目成员 | **否** | 只写 pending |
| `POST /api/v1/rule-proposals/<id>/accept` | L1 | 总监/owner/admin | 是（`source='confirmed'`） | `render_rules_md` + audit |
| `POST /api/v1/rule-proposals/<id>/reject` | L1 | 项目成员 | 否 | 只改状态 + audit |
| `POST /api/v1/memories` | scope=user | **本人**（他人 ownerId → 403） | 是（`source=manual`） | 刷新个人文件 + ad_hoc note |
| 同上 | scope=project | 项目成员，但**只创建 rule_proposal(pending)** | 否 | 无 |
| `PATCH/DELETE /api/v1/memories/<id>` | 个人 | 本人 | 是 | 重渲染个人文件 |
| 同上 | 项目 | 总监/owner/admin | 是 | 重渲染 rules.md |
| 内部抽取 `extract_memory_candidates` | 全部（默认 `project_user` 优先） | Agent 任务收尾（flag 控制） | **否**，只写 pending | 无 |

审批硬规则：

1. 候选 pending 不做任何生效动作；accept 才写 `memory`（`status=1`）。
2. L1 必须人确认；个人域（L3/L4）只允许本人确认/修改/删除。
3. 项目域的人工写/改/删/接受，只要调用者是**登录用户且有 project_member 角色行**，就按
   `owner/admin/总监/项目总监` 判——即使 `AUTH_REQUIRED=0` 的演示部署也如此
   （`can_confirm_project_rule` 与 `memcandidate.can_confirm_project_candidate` 同一实现）。
4. 只有**匿名**（无 token）或登录用户在项目里**没有角色行**时，才走演示放行（保证预览可用）。
5. 所有创建/接受/拒绝/修改/删除写 `audit_log`（`memory_candidate.create|accept|reject|extract`、`rule.*` 等）。

---

## 4. 读取、优先级与冲突

- **Agent 读什么**：AGENTS.md 只给指针，正文按需 `read`：`records/rules.md`、`records/photos.md`、
  个人 `MEMORY.md`、`memory_summary.md`、个人×项目文件。不整份注入摘要（P6 的方向）。
- **接口读什么**：`GET /api/v1/memories`（账本，按 scope/owner/project 过滤）、
  `GET /api/v1/memories/recall`（字面关键词，命中 `hits+1`）、
  `GET /api/v1/metrics/memory`（主动写/复用/撤销/提案确认率）。
- **优先级（概念）**：L0 组织知识/规范最高；本轮明确指令在不违反 L0 的前提下优先；L1 项目规矩作用于本项目；
  L3 覆盖本人在本项目的习惯；L4 是默认个人偏好；L2 是证据，不参与记忆排序。文件视图按层分文件，agent 按问题取用。
- **冲突不静默覆盖**：
  - accept 同文本查重：已存在则复用/重新启用该 `memory` 行，不产生两条。
  - 个人摘要：归一化相同 + 包含关系合并并标注"（同义合并 N 条）"；**相近但不同的说法并列**
    放入 `## 相近但未合并（需人工确认）`，不自动挑一条。
  - 项目规矩：只渲染 `memory` 里的 active 行，不在渲染时改写文本；冲突交给人工编辑/否决。
  - 本阶段**没有**自动裁决/自动 supersede（`hits` 排序也仍在用，v2 的 U×D 排序未实现）。

---

## 5. Phase 1：候选记忆（tasks / QA 目录已验）

流程：

```
任务完成（_post_turn_learn）
  ├─ MEMORY_LEARN=1（默认 0）：旧学习路径（直接写 memory，历史开关，保持关闭）
  └─ MEMORY_EXTRACT=1（默认 0）：LLM 抽 0~3 条候选
        → 只 INSERT memory_candidate(status='pending')；与已有 active/pending 相似则跳过
        → 不写 memory、不渲染文件、不进提示
小程序「待确认」
  → personal(user/project_user)：本人接受/忽略
  → project：只有总监/owner/admin 能接受；普通成员 403 时提示「需总监确认」
接受
  → memory(status=1, source='confirmed', source_ref='candidate:<id>')
  → 重渲染个人文件 / rules.md；个人域另刷 memory_summary.md
  → audit_log
```

要点：

- `MEMORY_EXTRACT` 默认关闭；开启也**只产 pending**，项目候选绝不直接 active（fake-LLM 已验，见 §9）。
- 抽取输入含对话上下文，但产物只是候选；`evidence` 建议保留用户原话；不做「模型自评/rollout 摘要」学习。
- 候选去重：同 project+user+scope+text 的 pending 不重复入队（API 返回 `duplicated:true`）。
- 候选目前**没有** 14 天过期任务、没有条数上限（设计稿有，未实现，见 §11）。

---

## 6. Phase 2：个人域摘要 + 项目域只重渲染

| 域 | 做法 | 保证 |
|---|---|---|
| 个人域 | 只读 `memory(scope='user', status=1)`（L3+L4），确定性去重/归并 → `data/memory/users/<uid>/memory_summary.md`；个人候选 accept 或小程序 bootstrap 时刷新 | 候选不参与；不调 LLM；幂等；不覆盖既有 `MEMORY.md` / `projects/<pid>.md` 拆分 |
| 项目域 | 只调 `render_rules_md`，数据源 `memory(scope='project', status=1)` | **不写 memory**、不新增/提升/改写规则；pending 天然不在表内；不把个人记忆升格为项目 |

有意不按 `source='confirmed'` 过滤项目规矩：线上 L1 多数是 `manual/learned` 的人工写入，
只认 `confirmed` 会把已生效规矩从文件里删掉（等于自动改写项目规则）。"confirmed active" 按
**已生效（status=1）** 落地。

幂等：内容无变化不写文件（不刷 mtime、不产生无意义 git 提交）；全量重建命令见 §8.

---

## 7. API 摘要

统一响应：`{code, message?, data}`，`code=0` 成功；HTTP 状态码同步语义；需要登录的接口带
`Authorization: Bearer <token>`。完整字段见 `contracts/openapi.yaml`。

| 方法 | 路径 | 说明 | 关键错误 |
|---|---|---|---|
| POST | `/api/v1/memory-candidates` | 新建候选（只 pending） | 400 参数；403 替他人提交/非成员 |
| GET | `/api/v1/memory-candidates?projectId=&userId=&status=` | 列表（默认 pending）；个人只有本人，项目需成员 | 403 |
| POST | `/api/v1/memory-candidates/<id>/accept` | 接受候选 → memory + 文件渲染 + audit | 403/404/409 |
| POST | `/api/v1/memory-candidates/<id>/reject` | 拒绝（只改状态） | 403/404/409 |
| POST | `/api/v1/rule-proposals` | 项目规矩提案（只 pending） | 400/403 |
| GET | `/api/v1/rule-proposals?projectId=&status=` | 提案列表 | 403 |
| POST | `/api/v1/rule-proposals/<id>/accept` | 总监确认 → memory + rules.md + audit | 403/404/409 |
| POST | `/api/v1/rule-proposals/<id>/reject` | 否决（只改状态） | 409 |
| GET | `/api/v1/memories?scope=&ownerId=&projectId=` | 账本列表（个人只本人；项目需成员） | 403 |
| POST | `/api/v1/memories` | 个人记忆（仅本人）/ 项目提案（pending） | 403 |
| PATCH/DELETE | `/api/v1/memories/<id>` | 个人=本人；项目=总监/owner/admin | 403 |
| GET | `/api/v1/memories/recall?projectId=&q=&userId=` | 字面召回，hits+1；他人 userId → 403 | 403 |
| GET | `/api/v1/metrics/memory?projectId=` | 记忆指标 | 403 |

响应示例：

```json
POST /api/v1/memory-candidates → {"code":0,"data":{"id":32,"status":"pending"}}
GET  /api/v1/memory-candidates?status=pending → {"code":0,"data":[{
  "id":32,"projectId":"test-sandbox","userId":"99999","scope":"project_user",
  "scopeLabel":"本项目习惯","text":"…","evidence":"…","sourceTaskId":1,
  "status":"pending","decidedBy":null,"decidedAt":null,"createdAt":"2026-11-10 13:15:36"}]}
POST /api/v1/memory-candidates/32/accept → {"code":0,"data":{
  "id":32,"status":"accepted","memoryId":123,"rendered":"data/memory/...","summary":"…"}}
POST /api/v1/memory-candidates/33/reject → {"code":0,"data":{"id":33,"status":"rejected"}}
```

---

## 8. 配置开关

| 变量 | 默认 | 作用 | 改动方式 |
|---|---|---|---|
| `AUTH_REQUIRED` | `0`（演示） | 0=不强制 token；1=所有非白名单接口要登录 | 线上 `.env`/启动 env；**改前确认所有账号都有 project_member 行** |
| `MEMORY_EXTRACT` | `0` | Phase1 候选抽取总开关；开启也只写 pending | 启动 env（`start-bff.sh` 目前未设置）；改后需重启 |
| `MEMORY_LEARN` | `0` | 旧学习路径（直接写 memory），保持关闭 | 同上 |
| `JL_ENGINE_MEMORY` | `1`（线上仅 clean-demo-01 白名单） | 1=个人×项目文件落 `data/homes/<uid>/memories/projects/`；0=落 `data/memory/projects/<uid>/` | `scripts/start-bff.sh` |
| `JL_MEMORY_PROJECTS` | `clean-demo-01` | 允许开引擎记忆（并注入引擎自家 MEMORY.md/summary）的项目白名单 | 同上 |

---

## 9. 验收记录（可复现）

所有脚本都在服务器 `/home/baize/jl-service` 跑（Windows 本机没有 Python）。结果基于部署版
`fe8331d`（task-7 只改 contracts/docs/tools，服务逻辑与 `5c271f9` 相同）。

```bash
# 1) 语法/契约
python3 -m py_compile bff/bffpkg/{memcandidate,memorysum,agent,biz,api}.py \
    tools/test-memory-{candidates,summary,permissions,extract-fake}.py
python3 -c "import yaml,io;d=yaml.safe_load(io.open('contracts/openapi.yaml',encoding='utf-8'));\
print('YAML_OK paths=%d' % len(d['paths']))"   # → YAML_OK paths=40（16 schemas）
# 注：contracts/ 不在 deploy 清单里，线上验证需把文件临时放服务器（如 /tmp）解析。

# 2) Phase1（候选 API / 权限 / 文件刷新 / pending 不改 rules）
python3 tools/test-memory-candidates.py --mode all    # HTTP 30/30 + 权限 14/14

# 3) Phase2（摘要去重 / pending 不影响 / accept 刷新 / AGENTS 仅指针）
python3 tools/test-memory-summary.py                  # 24/24

# 4) QA F1-F3/R1/R3 权限（member vs owner，含演示模式）
python3 tools/test-memory-permissions.py --mode all   # HTTP 17/17 + unit 30/30

# 5) 抽取钩子 flag（fake LLM，零费用；test-sandbox 隔离）
python3 tools/test-memory-extract-fake.py             # 14/14

# 6) 线上健康
curl -s http://127.0.0.1:8081/healthz                # {"ok":true,"engine":"up"}
head -1 runtime/DEPLOYED                             # fe8331d（task-7 提交后随部署更新）
```

实际记录（2026-11-10）：

| 项 | 结果 |
|---|---|
| Phase1 候选 | HTTP 30/30；权限 14/14 |
| Phase2 摘要/项目域 | 24/24 |
| QA 权限（F1-F3/R1/R3） | HTTP 17/17；unit 30/30 |
| 抽取 fake-LLM | 14/14（含 project scope 只 pending、memory 行数不变、rules.md 不变） |
| OpenAPI YAML | 解析通过，40 paths / 16 schemas，新增 8 条记忆/规矩路由 |
| 线上 health / preflight | `{"ok":true,"engine":"up"}` / 全部通过 |
| 数据基线 | `memory` 40 行；`memory_candidate` 仅 client 的 1 条历史 rejected；无测试残留 |

---

## 10. 回滚

> 原则：代码/配置/数据分开回滚；先备份再动库。线上唯一部署入口是本地 `scripts/deploy.ps1`
> （服务器不许手改代码）。

### 10.1 代码回滚（推荐 revert 出新提交）

```powershell
# 本地仓库
git revert --no-edit <bad-commit>        # 或 checkout 旧 tag 后重部署
.\scripts\deploy.ps1 -SkipSmoke          # 会拒脏工作区；部署会记录 runtime/DEPLOYED
ssh rokid-r640 'cat /home/baize/jl-service/runtime/DEPLOYED; curl -s http://127.0.0.1:8081/healthz'
```

回滚到 Phase1 只有 / Phase2 之前的点：revert `a254c9c`（Phase2）；回滚权限修复：revert `5c271f9`
（注意回滚后普通成员又能改项目规矩，仅演示模式可接受）。

### 10.2 数据库迁移回滚

```bash
# 备份（务必先做）
ssh rokid-r640 'cd /home/baize/jl-service && bash scripts/backup.sh'

# memory_candidate 是 Phase1 新表，down 脚本只删这一张表：
#   db/migrations/0003_memory_candidate.down.sql  →  DROP TABLE IF EXISTS memory_candidate;
# 手工执行（脚本不会自动跑；scripts/migrate.py 的生产路径只认 M1 影子表）：
ssh rokid-r640 'cd /home/baize/jl-service && python3 - <<PY
import pymysql
env = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
c = pymysql.connect(host="127.0.0.1", port=3309, user="jl",
                    password=env["MYSQL_PASSWORD"], database="jl", autocommit=True)
c.cursor().execute("DROP TABLE IF EXISTS memory_candidate")
print("memory_candidate dropped")
PY'
```

**顺序**：先 revert Phase1 代码并部署（否则服务重启时 `ensure_memory_candidate_table()` 会把表再建回来），
再决定是否 DROP 表。删表只是丢候选（pending），不影响 `memory` / 文件。

### 10.3 数据回滚（只回滚内容，不换代码）

```sql
-- 候选：直接删（本来就不生效）
DELETE FROM memory_candidate WHERE id %s;
-- 某次误接受的项目规矩：停用而不是删（保留审计），再重渲染
UPDATE memory SET status=0, updated_at=NOW(3) WHERE id=%s;
-- 误接受后想删除：DELETE FROM memory WHERE id=%s; 然后重渲染
```

```bash
# 重渲染文件视图（幂等；只读 memory，不写规则）
python3 tools/memory-views.py --project <pid>      # rules.md
python3 tools/memory-views.py --user <uid>         # memory_summary.md
python3 tools/memory-views.py --all                # 全量重建
```

### 10.4 开关回滚

- `MEMORY_EXTRACT`：从启动 env 去掉（默认 0）→ 重启 BFF；不产生新候选，已有候选保留。
- 个人摘要/候选表是只读视图 + 索引表；关掉 Phase2 代码不影响记忆账本。

---

## 11. 已知限制与未覆盖（如实写）

1. **演示模式** `AUTH_REQUIRED=0`：匿名或登录但**无 project_member 角色行** → 门禁放行；
   有角色行则严格按角色（`5c271f9` 起）。切 `AUTH_REQUIRED=1` 前要保证所有账号都有角色行，
   否则会出现"预览能点、上线 403"。
2. `MEMORY_EXTRACT` 默认 **0**：线上任务完成不会自动产候选；开启需显式 env + 重启。
   真实任务链路的抽取端到端未在线上跑（QA/验收均用 fake LLM）。
3. 个人域摘要为**确定性**去重（完全重复 + 包含关系合并；相近不同并列），未接 LLM consolidate。
4. **无自动冲突裁决**：不同说法并列，人工确认；`hits` 排序仍在用（MEMORY-V2 的 U×D+UCB 未实现）。
5. 候选**没有** 14 天过期 / 每 scope 上限任务；`memory_item` 影子表（global、memory_use/衰减等）
   只在 `jl_m1` 影子库，**未进生产读写路径**，组织级 global 未实现（L0 走 knowledge，不进 memory）。
6. **引擎记忆仍会注入**：`JL_ENGINE_MEMORY=1` 的项目（线上仅 clean-demo-01）里，引擎自家
   `homes/<uid>/memories/MEMORY.md` / `memory_summary.md` 照旧自动注入；BFF 的 `data/memory` 视图
   只是指针目标，不是注入源（MEMORY-V2 M6 未做）。
7. L3 文件落点受 `JL_ENGINE_MEMORY` 影响（`data/homes/...` vs `data/memory/...`），当前线上=1；
   AGENTS 指针同 flag 生成，未发现断链（QA R2 记为待确认）。
8. `metrics/memory` 的"提案确认率"只统计 `rule_proposal`，**不含** `memory_candidate`。
9. `render_rules_md` 每次变化会在项目工作区产生一条 git 提交；测试会在 test-sandbox 留"重渲染"
   提交（内容会恢复，但历史有痕）。
10. `AGENTS.md` 只在 `build_context`/`render_agents_md` 时生成；新增文件指针要等下一次对话/刷新。
11. 小程序真机未扫码点验（QA 只做 `node --check`、WXML 校验、预览包构建）；小程序不在本仓库。
12. 旧 `/api/v1/memories/recall` 虽然做了 token 收敛，但 agent 工具容器内调用通常无 token；
    鉴权全开时工具侧需要另行注入凭证（现状 `AUTH_REQUIRED=0` 才全通）。

---

## 12. 相关文件

- 代码：`bff/bffpkg/memcandidate.py`（候选/抽取）、`memorysum.py`（Phase2 摘要/项目域）、
  `biz.py`（`can_confirm_project_rule`/`render_rules_md`/`render_user_memory`/`render_user_project_md`）、
  `agent.py`（候选 API 之外的手动记忆/recall/_memory_guard/AGENTS 指针/收尾钩子）、
  `api.py`（路由与 ensure）、`core.py`（DB/llm/similar）。
- 契约：`contracts/openapi.yaml`（记忆/规矩路由已补）。
- 数据库：`db/schema.sql`、`db/migrations/0003_memory_candidate.sql|down.sql`。
- 测试/工具：`tools/test-memory-candidates.py`、`test-memory-summary.py`、`test-memory-permissions.py`、
  `test-memory-extract-fake.py`、`memory-views.py`。
- 相关既有设计：`docs/MEMORY-V2.md`（v2 目标态，未全部落地）、`docs/M1-DESIGN.md`（影子库）。
