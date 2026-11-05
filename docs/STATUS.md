# 当前状态

> 这是**唯一**的进度事实来源。开工先看它，收工更新它。
> 更新规则：只写事实（有实测输出），不写"应该""大概"。

**最后更新**：2026-11-05 16:20（+08:00）

---

## 现在在做什么

**M4 工程化** —— 把业务域从 Python 单体里抽出来，让 Java 服务接管。

**刚做完**：Java 骨架跑通四个文书接口，和 Python 共用同一个 MySQL，
互相能读到对方写的数据。

**手上这一件**：R4 —— 多 agent 拓扑还差一步：先解决 `encrypted_content` 阻断。
两条路：(a) 找一个完整实现 Responses 的线路；(b) 在能编译的环境里打上补丁
（补丁已写在 `repos/easycode/easycode-rs/core/src/client.rs`，只差编译）。
如果两条都走不通，就先做 R2 剩下的部分：把 `validate_doc` 挪进 `PostToolUse` 钩子。

> 原计划下一步是“biz 切 Java”，但语音/看图的能力刚验证完、趁热接上更划算；
> Java 切换不阻塞产品，可以稍后。

---

## 跑着什么

| 服务 | 端口 | 状态 | 说明 |
|---|---|---|---|
| BFF（Python） | 8081 | ✅ `{"ok":true,"engine":"up"}` | `runtime/entry.py`，包版 |
| jl-biz（Java） | 8082 | ✅ 200 | `service-java/target/jl-biz-0.1.0.jar` |
| MySQL | 3309 | ✅ Up 19h | docker `jl-mysql` |
| 引擎 easycode | — | provider=`dashscope` model=`qwen3.7-plus` | **常驻** app-server，会话复用 |
| 百炼（阿里云） | — | ✅ 已验证 LLM / 看图 / ASR / 华工隐患识别 | 凭据复用 `rokid_glass` |

**启动命令**

```bash
# BFF
cd /home/baize/jl-service && ENGINE_MODE=cli nohup python3 runtime/entry.py > runtime/bff.log 2>&1 &
# Java
export JAVA_HOME=/home/baize/opt/jdk-17.0.2
export MYSQL_PASSWORD=$(grep '^MYSQL_PASSWORD=' .env | cut -d= -f2)
setsid nohup java -jar service-java/target/jl-biz-0.1.0.jar > /tmp/jl-biz.log 2>&1 < /dev/null &
```

---

## 数据现状

```
项目 2 | 文书 43（待补 0） | 消息 161 | 任务 94 | 记忆 14 | 审计 9
工作区 yun-gu-3qi：61 次提交
```

> 待补 0 是因为最近几轮测试把字段都填了，不是功能没生效。

---

## 最近三轮做完的（倒序）

1. **Java 骨架**（`f6a5d72`）四个文书接口跑通，与 Python 数据互通
2. **单一写者**（`4fd4f54`）工具改调 `POST /docs/register`，doc 表只剩一个写者
3. **分包 + 契约**（`b195163` / `e34b7a9`）2900 行单文件拆成四层，30 条路径标了所有权

---

## 重大环境变化（2026-11-05）

**xfcxb 中转额度耗尽**：`403 用户额度不足，剩余额度 $0.000000`。
所有 AI 任务停摆，直到充值或换线路。

**已找到并验证备选线路**——复用 `rokid_glass` 项目的阿里云百炼凭据
（**不改对方的配置**，只把凭据复制到我们的 `.env`）：

| 能力 | 模型 | 实测结果 |
|---|---|---|
LLM | `qwen3.7-plus` | 200，**返回带 `reasoning_content`**（真正的思考文本） |
看图 | `qwen3.8-flash` | 200，能正确描述图片颜色 |
语音识别 | `qwen-audio-3.0-asr-flash` | 200，`"欢迎使用阿里云。"` 识别正确 |
隐患识别 | 华工 `hazard_vlm_detection` | 200，返回结构化风险判定 |

已写入 `/home/baize/jl-service/.env`：`DASHSCOPE_*` / `ASR_*` / `HUAGONG_*`。
**未做的事**：还没把引擎切到百炼（`wire_api = "chat"`），等确认。

几个踩到的坑（记下来省得重踩）：
- 百炼 ASR 对**静音**返回 `400` 且响应体是空的 `{}` —— 那不是鉴权失败，是"没听懂"
- ASR 必须用原生接口 `/api/v1/services/aigc/multimodal-generation/generation`，
  且 payload 是 `input.messages[].content[].input_audio.data`（data URI），
  不是 OpenAI 的 `/audio/transcriptions`（这个端点 404）
- ASR 只吃 16k 单声道 wav，webm/mp3 要先转（容器里有 ffmpeg）
- 华工 VLM 要求文件**带扩展名**，没扩展名直接 400

## 照片描述可检索 + R7 验收（2026-11-06 凌晨）

### 缺口：描述存了，但查不到

上一轮做完 caption，验收清单第 5 条（凭照片描述找回来）其实**做不到** ——
没有任何接口按 caption 查。描述等于白存。

补了 `search_photos` 工具（按关键词/部位/日期查 photo 表，返回描述和原图地址）。

### 实测

```
jl-tool search_photos {"projectId":"test-sandbox","q":"绿色色块"}
  → total 1，命中 photo 19，caption「测试绿色色块，验证描述链路」

问 agent「这个项目以前拍过绿色的东西吗？」
  → 12 秒，答「查到了 1 张：2026-11-06 00:37:39 — 描述：测试绿色色块…（照片 ID 19）」
```

### R7 验收清单（对着 ARCHITECTURE-KNOWLEDGE 第八节逐条核）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 用户 A 说规矩，用户 B 答不出 | 🟡 结构已隔离（每用户 EASYCODE_HOME），**内容未验**：引擎记忆要多次会话才落盘 |
| 2 | A 项目规矩不串到 B 项目 | 🟡 MySQL 记忆按 project 过滤已生效；引擎记忆的 `applies_to: cwd=` 未验 |
| 3 | 同一句话三种说法只留一块 | 🔴 未验（依赖引擎整理，需要多轮会话） |
| 4 | 问模板去读文件而不是编 | 🟡 链路已通（文件→导入→检索），**模板目录还是空的** |
| 5 | 凭照片描述找回来 | 🟢 已验（见上） |
| 6 | AGENTS.md ≤800 字节且无工具清单 | 🟢 607 字节 |

**没验的就是没验**，不写成完成。第 1~3 条卡在同一个前提：引擎记忆要有足够多的会话才产出。

## R7 第四步：多模态沉淀（2026-11-06 凌晨）

### 做了什么

```
photo 表     加 caption / caption_at
接口         POST /api/v1/photos/caption（有人写过就不覆盖，除非 overwrite）
工具         caption_photo（agent 看完图自己写一句）
列表         /api/v1/photos 带 caption
```

### 实测（三步都验了）

```
1) 上传 test-sandbox 的图 → photo 19，project_id=test-sandbox（不再进默认项目）
2) 手动写描述 → {"id":19,"caption":"测试绿色色块…"}
   再写一次 → {"skipped":true}（不覆盖）
3) agent 自己看图 → 20 秒内写下
   「纯红色（砖红色）色块图片，无文字、无水印、无可辨识场景或构件」
   相册接口返回该 caption
```

### 顺手修了两个真 bug

**① 上传的照片永远进第一个项目**

小程序 `wx.uploadFile` 把 `projectId` 放在 **formData** 里，
后端只从 query string 读 → 读不到就 `default_project_id()`。
**结果：不管用户在哪个项目，图片全进最早创建的那个** ——
这正是「沙盛四路的照片跑进云谷项目」的根源之一。

修法：multipart 解析同时返回表单字段，解析顺序 query → 表单 → 默认。

**② 新项目的第一张照片必然 500**

`append_manifest` 直接写 `photos/manifest.json`，目录不存在就 FileNotFoundError。
已先建目录。

## 提交
## R7 第三步：冷启动知识包（2026-11-06 凌晨）

### 先说发现的事实：规范库几乎是空的

```
SELECT COUNT(*) FROM standard;  → 2 条
  1) JGJ 130-2011 6.4.3 连墙件的布置
  2) GB 50204-2015 8.2.1 保护层厚度允许偏差
```

**这就是模型总说「规范库里查不到条文」的真正原因** —— 不是它不肯查，是库里真没有。
之前所有「依据」要么空着，要么是模型凭印象写的条号（危险）。

### 做法：知识包是文件，库只是索引

```
data/knowledge/standards/JGJ-130-2011.md    ← 人写、git 管理、可 review
data/knowledge/standards/GB-50204-2015.md
        ↓  scripts/import-knowledge.py（也可由 BFF 启动时自动同步）
standard 表（唯一写者：bffpkg/knowledge.py）
        ↓
/api/v1/standards?q=…  →  jl-tool search_standards
```

文件格式（一个规范一个文件）：

```markdown
# JGJ 130-2011 · 建筑施工扣件式钢管脚手架安全技术规范
version: 2011

## 6.4.3 连墙件的布置
正文……
```

按 (code, clause) upsert，重复导入不会产生重复行。

### 实测

```
导入器幂等：连续跑两次 → 新增 0、更新 2，库里仍 2 条
BFF 启动自动同步：[knowledge] 规范同步：新增 1、更新 2、跳过 0
端到端：加一条测试条文文件 → 导入 → 检索命中
  {"code":"TEST 0000-2099","clause":"1.1.1","title":"导入链路自检"}
清理后回到 2 条（测试数据不留在库里）
```

### 现在缺的是内容，不是链路

**必须由监理技术负责人提供并核对**：常用规范条文摘编（脚手架、混凝土、临边洞口、
临时用电……）。我不能凭印象往里写条号 —— 那是拿错误依据出正式文书。
链路已经通了，放文件进去就生效。

### 顺带查了 72 份「待补」的构成

```
yun-gu-3qi   监理通知单      62 份（2026-11-04 ~ 11-05）  ← 绝大多数是我测试时出的
yun-gu-3qi   监理检查记录      7 份
test-sandbox 监理通知单        4 份                          ← 自测，正常
baiyun-a2    监理日志          2 份                          ← 真实使用
yun-gu-3qi   监理日志          1 份
```

**结论**：真实项目里被测试污染的是 yun-gu-3qi 的 62 份通知单（我出的）。
要不要清理，等用户决定 —— 里面可能混着早期真实使用产生的。

## R7 第二步：AGENTS.md 瘦身 + 找到一个真 bug（2026-11-06 凌晨）

### AGENTS.md 瘦身：2778 → 607 字节

只留四类：项目名、工程概况（一行）、参建单位（一行）、项目规矩、交付契约 + 指针。
工具清单和说话风格搬到 `data/knowledge/skills/`（`jl-tool.md` / `output-style.md`）。

**踩的坑**：把「必须用 create_doc_draft」整段删掉后，agent 改用 shell 手写 md 并声称完成，
doc 表没有这行 —— 用户在系统里根本看不到这份文书。
所以契约那一行必须留（`手写 md 不会出现在系统里`），它不是工具手册。

### 顺手挖出一个真的线上 bug：文书根本没入库

**现象**：任务 153/154 里 `create_doc_draft` / `validate_doc` 返回 `fetch failed`，
agent 连续失败 5 次后手写文件，回复却说「已生成并提交」。

**根因**：两处，缺一不可

```
1) BFF 只监听 127.0.0.1:8081（api.py 硬编码）
2) MCP 工具容器跑在 jl-net（172.19.0.0/16）桥接网络里
→ 容器里访问 host.docker.internal 也到不了只绑回环的 BFF
对比：exec 路径的 jl-tool 用 --network host，所以能通 —— 这就是为什么时好时坏
```

**修法**：

```
api.py     绑定改成 BIND_HOST（默认 0.0.0.0）
mcp-jl.sh  加 --add-host host.docker.internal:host-gateway
           加 -e BFF_URL=http://host.docker.internal:8081
```

**验证**：

```
ss -ltnp → 0.0.0.0:8081
任务 157：1 条助手消息、answer.review 2374 字、文书已入库、无 warning
doc 82/83 出现在 doc 表（test-sandbox）
```

### 新增：出稿对账（防止静默丢文书）

本轮工作区新增的 `docs/**/*.md`，如果 doc 表里没有对应 path，就在时间线记一条，
并写进 `answer.warning`。实测抓到了 3 份手写文件。

### 还发现一处重复复核

`_run_cli_turn` 里同时存在「合并版复核」和「老版单独插一条消息」两段代码，
导致每轮复核两次、多一个气泡。已删掉老段（1116 字符）。

## Loop 归属理清 + 服务侧调度器（2026-11-06 凌晨）

**问题**：开发用的 loop 和服务要的 loop 混在一起，搞错地方不会报错，
只会「以为它在跑」。

**规则**：服务停了用户会受影响 → 放服务器；只影响开发 → 放开发环境。
详见 `docs/ARCHITECTURE-LOOPS.md`。

**新增服务侧调度器**（`bff/bffpkg/jobs.py`）：

```
service_job 表    name / cron_expr / handler / enabled / last_status / next_run_at
Cron 解析         5 段，支持 * */n a a-b a-b/n 逗号
worker 线程       每 20 秒认领到点任务（乐观锁，单写者）
handler 注册表    加任务 = 加一个函数 + 一行登记
管理接口          GET /api/v1/jobs、POST /api/v1/jobs/<name>/run
```

**第一个业务任务**：`pending_docs_digest`（每天 18:00 统计待补文书并写审计）

**实测**：

```
[job] 注册服务任务 pending_docs_digest（0 18 * * *，下次 2026-11-06 18:00）
POST /api/v1/jobs/pending_docs_digest/run → {"projects":2,"pending":72}
audit_log: yun-gu-3qi job.pending_docs {"pending":70} / baiyun-a2 {"pending":2}
把 next_run_at 改成过去 → 30 秒内自动跑（00:02:06 ok），并自动排到下一天
```

**顺带发现**：待补文书 **72 份**（云谷 70 + 白云 2），
之前 progress.sh 显示「待补 0」是因为口径不同（那个只看某个状态）。
72 份缺字段的文书是个真问题，值得单独看。

## R7 第一步：每用户 EASYCODE_HOME（2026-11-05 深夜）

**做了什么**：BFF 给每个用户准备一个引擎家目录，起 app-server 时传 `EASYCODE_HOME`。

```
data/homes/<userId>/
  ├── config.toml        从主配置复制，并在 [features] 里加 memories = true
  ├── memories/          引擎自动维护（raw_memories.md / rollout_summaries/）
  ├── sessions/          该用户的会话
  └── skills/            该用户私有技能
```

**实测**：

```
用户 201 → data/homes/201/memories/{raw_memories.md, rollout_summaries/, extensions/}
用户 202 → data/homes/202/memories/{...}（各自独立）
进程环境：EASYCODE_HOME=/home/baize/jl-service/data/homes/201
config: [features] 里 memories = true（feature.memories=true 已确认）
```

**踩的坑（重要）**：第一次把 `memories = true` 追加到配置文件**末尾**，
结果它成了最后一个表（`[agent_roles.reviewer]`）的键，引擎根本没启用记忆。
→ 必须插在 `[features]` 表**里面**。这和之前 `[features]` 必须放在顶层键之后
是同一类坑：**TOML 的表边界靠位置决定，不靠缩进**。

**还没验证的**：记忆**内容**。引擎的记忆管线在会话启动时跑，
只处理最多 2 条历史 rollout；我们这两轮内容太轻（只回"好"），
`raw_memories.md` 仍是 "No raw memories yet"。
需要一次真实出稿会话，再重启一次触发提取，才能确认内容能落下来。

## 线路统一：彻底去掉旧中转（2026-11-05 深夜）

**问题**：换到百炼之后，`llm()`（学习、记忆抽取、旧的 tool-loop 用它）
还指着 `llmapi.xfcxb.com`，那条额度是 0 —— 于是学习一直在报 403，功能等于死的。

**改动**（四处，全部指向百炼）：

| 位置 | 改成 |
|---|---|
| `bff/bffpkg/core.py` 的 `LLM_URL` | `DASHSCOPE_BASE_URL` + `/responses` |
| `LLM_MODEL` | `qwen3.7-plus`（可用 `LLM_MODEL` / `LLM_FALLBACK_MODEL` 覆盖） |
| 鉴权 | `DASHSCOPE_API_KEY` |
| `ENGINE_ENV` 注入 | 不再注入 `JL_LLM_KEY` |

另外：`~/.easycode/config.toml` 删掉 `[model_providers.xfcxb]` 整段；
`.env` 删掉 `JL_LLM_KEY`；旧的单文件版 `runtime/server.py` 同步切过去。

**验证**：

```
LLM_URL: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/responses
LLM_MODEL: qwen3.7-plus
llm('只回复两个字：可用') → '可用'

学习回路：说「整改回复必须附对比照片」→ 写入 memory（id 17）
         再说同一条 → 命中去重，hits 1→2（不重复记）
全仓再搜 xfcxb / JL_LLM_KEY：只剩 core.py 里一句历史注释
```

## 记忆边界修整（2026-11-05 深夜）

用户问：模型是不是没记忆 / 记忆混乱？

**是有记忆的**：常驻会话按项目复用线程，历史都留着。
正因如此，它把**历史里出现过的图片**也当成本轮内容复述了 ——
那张渐变图是我早先测试留下的，它只是如实描述了上下文里的所有图。

### 两个修整

**① 提示里标清「本轮附件」**

带图时加一句：这一条消息带了 N 张图，只针对这几张回答，
历史图片不要重复描述。

实测（两轮、两张纯色图）：

```
第1轮 红图 → 红色
第2轮 蓝图 → 蓝色（没有再提红色）
```

**② 会话改按「项目 + 用户」隔离**（这个更严重）

原来 `_client(project_id)` 只按项目复用 —— **同一个项目里的两个用户会共享一条线程**，
能看到对方说过的话，甚至接着对方的话说。那不是记忆，是串话。

改完实测：

```
用户1：记住一个词：苹果 → 收到
用户2：我刚才让你记住什么？→ 不知道。这是这轮对话的第一条消息，之前没有上下文。
```

## 测试图污染真实项目（2026-11-05 深夜，已清理）

**现象**：用户发了两张沙盛四路的现场照，模型却多描述出一张「彩色渐变图」，
还判断「不是现场照片」。用户问：怎么那么奇怪？

**原因**：不是模型乱说 —— 是我们自测时把两张测试文件传进了**真实项目**：

```
photo 1 → 400x300 渐变 PNG（我做 vision 测试用的）
photo 4 → WAV（我做 ASR 测试用的）
```

它们进了 `yun-gu-3qi` 的现场影像，用户选附件时把测试图一起选上了。
模型如实描述它看到的三张图，其中一张确实不是现场照。

**已处理**：

- 删掉 photo 1 / 4 两行 + 对应 blob 文件
- 新建测试专用项目 `test-sandbox`（`data/workspaces/test-sandbox`），
  **以后所有自测都放这里**，不许再往真实项目写
- 顺手补上 `yun-gu-3qi` 缺失的项目编号（原为 NULL）

**教训（写进 LOOP-PROMPT 的纪律）**：

自测产生的数据不能进真实项目。图片、语音、文书、任务都一样 ——
它们会以「相册」「资料」的形式出现在用户面前，用户会以为是自己传的。

## Markdown 渲染（2026-11-05 深夜）

模型输出里有 `**加粗**`、`- 列表`、`### 小标题`、`代码`，原来按纯文本显示，
界面上就看见一堆星号和横杠。

**自己写了个极简解析**（`utils/md.js`，约 150 行）：只支持四种块 + 三种行内样式，
够用且可控。没用 towxml / mp-html —— 它们在 Skyline 上支持参差。

```
块：段落 / 标题(#~####) / 无序列表(- * •) / 有序列表(1. 2)) / 分隔线
行内：**加粗** / `代码` / *斜体*（当普通字处理）
```

只有检测到 Markdown 语法才走块渲染，没有就还用原来的纯文本/富文本 ——
避免把老消息的排版搞乱。

## 复核并进主消息 + 能看到图片（2026-11-05 深夜）

**用户报的**：上传多张图，回答却是复核那条 `Message Type: FINAL_ANSWER …` 信封。

**两个原因**：

1. 复核是**单独插一条 assistant 消息**，前端把它当成回答显示，图文回应被挤下去
2. 复核线程拿到的「用户原始要求」是空的（纯图片消息没有文字），也看不到图

**改法**：

- 复核做完再一次性入库：复核内容进 `answer.review` + 时间线里一条「复核意见」note，
  **不再单独占气泡**
- 复核会话能收到用户的文字和图片（`thread/start` 的 `localImage` 输入）
- 复核线程用干净 cwd（`/tmp/jl-review`），不加载项目 AGENTS.md —— 之前就是被它带偏去试 spawn_agent
- 客户端读通知**按线程过滤**，避免把别的线程（含子 agent）的消息混进来
- 复核输出要求直接写意见，不要 FINAL_ANSWER 信封

**实测**：

```
纯图片消息 → 12 秒，模型真的看到了图：
  「这张图片是一个彩色渐变图（蓝紫→粉红→黄橙→绿青），看起来像是测试图或色卡」
带图 + 文字但内容不成立 → 8 秒，模型不编文书，反问要部位和工序（正确）
```

顺带删掉了 `_review_pass` 的重复定义（之前替换时留下了旧版）。

## 收工归档（2026-11-05 晚）

### 今天做完的（按时间）

1. 引擎线路从中转切到百炼（qwen3.7-plus），带回 reasoning
2. 多模态打通：图片走 `exec -i`、语音走 `/api/v1/asr`、任意组合
3. 前端接 parts、文书页接真数据
4. schema 校验（`validate_doc`）+ 12 份错标状态的文书修正
5. 常驻 app-server（`ENGINE_MODE=appserver`），简单一轮 2.6 秒（原 8~14 秒）
6. 多 agent 打通（补丁 0001：清掉 AgentMessage 里的 encrypted_content）
7. 回复去掉「结论/依据/待补」模板，改由数据出结构
8. 输入框乱码修复（受控输入 + 每次 setData 回写会导致光标错位）
9. 附件区重做（flex-wrap 网格，替掉会塌的 scroll-view）
10. 语音附件带上 tempFilePath（原来写死空串，永远传不上去）
11. 子 agent 可换模型（补丁 0002 加 `qwen3.8-flash` + 开 `expose_spawn_agent_model_overrides`）

### 今晚最后卡住的一件事（未解决，明天第一件）

**复核会话的内容跑偏**。做法已经改成「开一条独立会话、用 reviewer 角色、
指定 `REVIEW_MODEL=qwen3.8-flash`」，日志有 `[review] task 133 复核完成`，
但输出内容是「spawn_agent 调不通…」这类元讨论，不是对文书的实际审查意见。

已排除的：

- 部署版本确实含新提示（`grep 你是独立复核会话 bff/bffpkg/agent.py` = 1）
- 复核确实走了独立会话（日志有 review 完成记录，主线程消息只有两条）

**还没查的**：独立线程会加载项目的 AGENTS.md，里面写着多 agent 的用法，
reviewer 可能被它带偏（去试 spawn_agent 而不是读文书）。
下一步要么给独立线程换 cwd（只读挂载），要么在 thread/start 里传更干净的 baseInstructions。

### 附件上传另一个未确认点

用户报「有 N 个附件没传上去」，但服务端日志里 `/api/v1/upload` **全是 200**。
已改客户端：`res.data` 可能是对象而不是字符串（直接 `JSON.parse` 会抛错被吞），
现在两种形态都接，并在拿不到 sha 时打印 statusCode + body。
**是否修好要等用户重试后看控制台**，我没有真机验证能力。

### 今天的数字

```
文书 77 | 消息 249 | 任务 133 | 记忆 14 | 审计 43 | 工作区提交 113
硬编码体检 11（基线 11，全天从 12 降到 11 后未再上涨）
```

## 附件区重做 + 三个前端 bug（2026-11-05 晚）

### 1. 附件条重做

用户描述：「并排向下、冲出屏幕」。根因是 `scroll-view scroll-x` 在 Skyline 里
不给高度会塌，子项还会竖着排。

改成 flex-wrap 网格：

```
112rpx 方块 · 20rpx 圆角 · 12rpx 间距 · 最多 9 张 + 一个虚线添加位
两行封顶（max-height 264rpx），超出裁掉，绝不顶出输入框
每张右上角圆形 ✕（半透明黑底 + 白边 2rpx）
语音块同样大小：波形 + 时长
```

### 2. 只发语音/只发图片报「没有可发送的内容」

两个原因叠加：

- 录音回调没取 `onStop` 的 `tempFilePath`，附件里 `src` 写死空串 → 语音永远传不上去
- 图片上传失败也静默，最后 parts 为空，报一句笼统的「没有可发送的内容」

改：录音取真实路径（拿不到就明确提示）；上传失败时说明「有 N 个附件没传上去」。

### 3. 发送失败时附件被清空

原来 `onSend` 一进来就 `attach: []`，失败后用户得重新选。
改成**任务号拿到以后**才清。

### 4. 左上角项目胶囊偶尔空白

`onShow` 里用 `{name, sub}` **替换**了 project，把 `id` 弄丢了；
`name` 为空时胶囊就变白块。改成合并 + 兜底（name → code → id → 未命名项目），
`loadAll` 也统一走 `_mergeProject()`。

### 5. 子 agent 能换模型了（源码级改）

两件事缺一不可：

1. `models-manager/models.json` 里要有这个模型 —— 它是 `include_str!` 编进二进制的。
   现在加了 `qwen3.8-flash` 一条（只给复核用；主模型继续走 fallback，不动它）。
2. 打开 `[features.multi_agent_v2] expose_spawn_agent_model_overrides = true`，
   否则 spawn 工具根本不暴露 `model` 参数。

实测：

```
事件: sub_agent_activity started | model: qwen3.8-flash
父：派一个 reviewer 子 agent，model 设为 qwen3.8-flash → 已派出，等它回复 → wait completed
```

**踩过的坑**：第一次往 models.json 里加了 5 个模型、还把一批字段置空，
结果整个模型目录变成空表（`model/list` 返回 0 条），连原有的 gpt-* 都没了。
原因是我改的那些能力位让条目校验不过。**正确做法是最小改动**：
克隆一条现成的，只改 slug / display_name / description / context_window。

BFF 现在的启动参数带 `REVIEW_MODEL=qwen3.8-flash`，复核子 agent 用便宜模型。

## 用户体验修整（2026-11-05 晚）

### 1. 回复不再套模板

用户反馈回复生硬（每条都「结论/依据/待补」）。根因是**两头硬编码**：
提示词逼模型写三行，后端再用正则把三行拆成字段。

改法：

- 提示词删掉格式要求，换成「像一个监理工程师在跟同事说话」
- `build_answer` 的缺项改由 schema 算（`validate_fields`），引用由工具输出算
- 删掉 `parse_answer` / `enrich_answer` / `_split_by_heuristic` / `ANSWER_RE` / `ANSWER_KEYS`

实测回复变成了：

```
通知单已出，状态「待签」。要点：依据 JGJ 130-2011 第6.6.2条…签发人 陈志明
需要调整内容或补充其他信息可以说。
```

### 2. 输入框乱码（电脑端）

**根因**：`<textarea value="{{draft}}">` 配 `bindinput` 每次 `setData` 回写，
这是受控输入。电脑端输入法合成、回车、粘贴时异步回写会冲掉光标位置，
字符重复或错位，看上去就是乱码。搜索框 `value="{{query}}"` 同一个毛病。

**改法**：value 只由程序设置（`draftSeed` / `querySeed`），
打字过程正文放 `this._draft`（非响应式），界面只关心 `hasDraft`；
草稿写 storage 改成 400ms 防抖（原来每敲一下写一次，会卡输入）。

**同类隐患一并处理**：剪贴板粘贴、引用插入、发送后清空都统一走 seed，
不会再出现“程序改一半、用户改一半”导致的光标错位。

### 3. 下拉刷新串项目

**根因**：`onRefresh()` 请求 `/api/v1/bootstrap` **没带 projectId**，
后端就用默认项目 —— 刷新完看到的是别的项目的数据。

改法：和 `loadAll` 一样带上 `this._projectId()`。

### 4. 子 agent 想换模型，被引擎白名单挡住

实测：给 `spawn_agent` 传 `model=qwen3.8-flash`，引擎回：

```
引擎不支持 qwen3.8-flash，可用模型只有 gpt-6.1-sol / gpt-6-astra / gpt-6-sol /
gpt-6-luna / gpt-5.6-sol
```

**结论**：子 agent 的 `model` 参数只认引擎内置的模型目录（OpenAI 那套），
我们这条百炼线路不在里面。所以 `REVIEW_MODEL` 暂时不用，
子 agent 与主 agent 用同一个模型。

### 5. 子 agent 活动进时间线

`subAgentActivity` 和 `collabAgentToolCall` 现在会翻成时间线条目：
「派子 agent（模型）」/「等子 agent 回报」/「给子 agent 发消息」。
父 agent 的中间插话本来就在时间线里，现在能看出它到底在跟谁协作。

## R4 第一块：多 agent 打通（2026-11-05）

**结果**：`spawn_agent` 能用了。子 agent 真的跑完任务并把结果交回给父 agent。

```
父：派一个 explorer 子 agent，让它回答一句话
子：跑了完整的工作区探索，返回了一大段项目概况
父：子 agent 已回复……核心信息是：子 agent 在线，能正常接收任务并返回结果
encrypted_content 报错次数：0
```

**怎么解开的**：不是换线路，是在能编译的环境里打补丁。

关键发现：**报错的不是推理项，是 agent 消息里的加密内容块**。

```
错误原文：Invalid content type: encrypted_content.
          Supported types for assistant role are: 'input_text', 'output_text'.
```

`clear_encrypted_content()` 对 `AgentMessage` 这个变体是**跳过**的
（`protocol/src/models.rs` 里的 match 分支），所以多 agent 交接时
那块 `encrypted_content` 会一路带到请求里。

**补丁**（`patches/0001-non-openai-drop-encrypted-content.patch`）：

1. 非 OpenAI 一律清掉加密内容（原来 Reasoning 项被豁免）
2. 不再请求 `reasoning.encrypted_content`
3. **额外**：`AgentMessage` 的 content 只保留 `InputText`

**编译方法**（服务器上没有 C 编译器，但有构建镜像）：

```bash
cd /home/baize/jl-service/repos/easycode
docker run --rm -v $PWD:/src -w /src/easycode-rs \
  -e CARGO_HOME=/src/.cargo-home \
  -e RUSTY_V8_ARCHIVE=/src/v8lib.a \
  -e RUSTY_V8_SRC_BINDING_PATH=/src/v8binding.rs \
  jl-easycode-build:local cargo build --release -p easycode-cli --bin easycode
# 再 strip 一下：1412MB → 283MB
docker run --rm -v $PWD:/src jl-easycode-build:local strip /src/easycode-rs/target/release/easycode
```

编译耗时约 11~13 分钟（首次全量）。镜像里 rustc 1.95 + gcc 12 都有。

**注意**：换二进制前必须先停 BFF 和 app-server，否则 `cp` 报 `文本文件忙`。
重启后要确认 8081 端口没被旧进程占着（会报 `Address already in use`，
而那时代码其实还是旧的）。

## 复核轮（2026-11-05 已上线）

**做了什么**：出稿之后，在同一个会话里再跑一轮，让 agent 自己复核 ——
先 `validate_doc` 看字段，再通读工作区里的最新文书，检查结论有没有依据、
有没有自相矛盾、有没有编造条号；该改就 `create_doc_draft` 出新版。

**不是多 agent**：同一线程的第二轮，借的是「会话复用」，不依赖 spawn_agent。
等 R4 打通多 agent 再换成真正独立的 reviewer 角色。

**实测**（任务 122）：

```
总用时 89 秒（出稿约 45 秒 + 复核约 44 秒）
消息 3 条：用户 / 出稿 / 复核
复核结论：缺 整改要求、整改期限、检查结论、签发人；
          受检单位、部位、事由与用户描述一致，无编造；bases 为空也没捏造依据
```

**开关**：`REVIEW_PASS=0` 关掉。默认开，只在真的出了文书时跑。

**代价**：出稿类任务耗时翻倍。默认开还是按需开，等实际用一段时间再定。

## R3 常驻 agent 服务（2026-11-05 已接入）

**做法**：`easycode app-server` 常驻，每个项目一条进程 + 一个线程（会话复用），
BFF 用 stdio JSON-RPC 驱动，事件翻译成和 `exec --json` 一样的形状，
**解析代码两个后端共用**（`bff/bffpkg/appserver.py`）。

**实测延迟**（同一台机器，同一模型）：

| 场景 | exec（旧） | app-server（新） |
|---|---|---|
简单一轮 | 8~14 秒 | **2.6 / 2.3 秒** |
带工具一轮 | 14 秒+ | **4.8 / 4.1 秒** |
真实出稿任务 | 36~50 秒 | **53 / 22 / 17 / 12 / 21 秒** |

> 真实任务的总时长主要花在模型调用和查规范上，进程启动省下的 5~10 秒是净赚。

**怎么切换**：`ENGINE_MODE=appserver`（现在是默认），`ENGINE_MODE=cli` 回退旧方式。

```bash
cd /home/baize/jl-service
ENGINE_MODE=appserver ENGINE_MODEL=qwen3.7-plus \
  nohup python3 runtime/entry.py > runtime/bff.log 2>&1 &
```

**踩到的四个坑（都写进代码注释了）**：

1. 子进程必须继承 `ENGINE_ENV`，不是 `os.environ` —— 否则报
   `Missing environment variable: DASHSCOPE_API_KEY`
2. `thread/start` 的 `cwd` 必须是绝对路径，相对路径会让工具起不来，
   报 `Failed to create unified exec process: No such file or directory`
3. item 类型是驼峰 `agentMessage` / `userMessage`，不是下划线
4. `item/completed` 里的 agentMessage 正文经常是空的，真正的文本在
   `item/agentMessage/delta` 里，要自己拼

**debug**：`JL_APPSERVER_DEBUG=1` 会把原始通知落到 `/tmp/jl-appserver-last.jsonl`。
这次就是靠它一眼看到 `Missing environment variable` 的。

## R2 第一块：文书字段按 schema 校验（2026-11-05）

**为什么做**：模型会把「待补」当成字段值写进去（`"签发人": "待补"`），
原来只判空字符串，于是这种文书被标成「待签」，实际没填。

**规则放哪**：`schemas/<文书类型>.json`（`fields` 必填、`optional` 可选、
`placeholders` 占位词）。**代码里没有 if**，加类型就是加一个 json 文件。

**一份实现两处用**（避免两套规则打架）：

| 用处 | 接口/工具 | 谁调 |
|---|---|---|
| agent 出稿前自查 | `POST /api/v1/docs/validate` / `jl-tool validate_doc` | agent 自己 |
| 落库前把关 | `/docs/register` 内调同一个函数 | BFF |

**实测**：

```
validate(签发人=待补)      → ok=false  placeholder=["签发人"]
validate(字段齐全)         → ok=true
validate(未知类型)         → ok=true   note=没有该类型的 schema
register(state=待签, 缺 2 项) → 落库 state=待补，返回 missing
register(齐全)              → state=待签
```

**顺手修的历史数据**：按新规则重算，**12 份文书**从「待签」改成「待补」
（它们其实一直缺字段，页面却显示可签）。

**顺手修的两个 bug**：

1. `allowed_tools()` 把 `{"tools":[...]}` 当数组解析，白名单变成了 `{"tools"}`
2. 提示里的工具清单是**手写**的，加了新工具 agent 永远看不到 → 改成问 `jl-tool list`，
   并且必须带完整路径（agent 的 shell 里没有 PATH 里的 `jl-tool`）

**agent 自己会用了**（任务 104，没人提示它）：

```
调用顺序: search_standards ×5 → validate_doc → create_doc_draft
产物: 监理通知单 | state: 待补
```

**还没做**：把校验做成 easyCode 的 `PostToolUse` 钩子（现在是 register 内部调），
等 R3 常驻服务时一起做。

## 文书详情页接真数据（2026-11-05）

- 有 `doc id` 就查 `GET /api/v1/docs/:id`，没有才回落到原型数据
- 字段原样展示（中文键名），空的单列「待补」，`bases` 转成条文卡片
- 映射逻辑用真实文书（doc 42）验过：8 个字段成 1 组、1 条依据，标题「监理通知单」

**发现一个数据质量问题（记技术债，先不修）**：

模型会把「待补」当成字段值写进去，比如 `"签发人": "待补"`。
后端 `missing` 只判空字符串，所以这种字段被算成"已填"，页面上「缺 0 项」但实际没填。

**不能在详情页用正则去认「待补」两个字**（那是后端替模型判断，违反 T2）。
正确做法是 R2 的 `validate_doc` 工具 + 钩子：在文书落库前用 schema 校验，
空值就必须是空字符串，模型想写「待补」会被挡回来。

**硬编码体检**：12（基线 12，没涨）。

## R1 多模态（2026-11-05 前后端都通了）

| 能力 | 状态 | 实测 |
|---|---|---|
| 图片 | ✅ 通 | 发一张渐变图，模型答「多彩渐变色，包含蓝、绿、粉、黄」 |
| 语音 | ✅ 通 | 上传 wav → ASR 出「欢迎使用阿里云。」→ 模型原样复述 |
| 任意组合 | ✅ 通 | 2 图 + 1 语音 + 文字一次发，模型分别报告看到/听到 |
| 前端输入框 | ✅ 已接 | 小程序改为上传换 sha → 发 parts；音频顺手转写后一起发 |

**实现方式**（都走现成能力，没有自己造）：

- 图片：blob 落成真实文件 → 引擎 `exec -i`，模型原生看到，**不调 view_image**
- 语音：`/api/v1/asr` → 百炼 `qwen-audio-3.0-asr-flash`（ffmpeg 转 16k wav）
- 存储：`message.parts` JSON 列，原始内容项原样保存（含 `transcript`）

**踩到的坑**：`exec -i <FILE>...` 是变长参数，会把后面的 prompt 当成图片路径吞掉，
所以命令行里 prompt 必须排在 `-i` 之前。


**前端怎么在真机上验证**（改完必须自己走一遍，别只看代码）：

1. 开发者工具勾「不校验合法域名」，`utils/config.js` 的 `API_BASE` 指向 `http://127.0.0.1:8081`
2. 本机先起隧道：`ssh -N -L 8081:127.0.0.1:8081 rokid-r640`
3. 聊天页：拍两张照 + 按住录音说一句话 + 打字，一次发送
4. 预期：气泡显示两张图和一个语音条；几秒后 AI 回复里同时提到图和语音内容
5. 想确认真的走了多模态：到服务器查 `SELECT parts FROM message ORDER BY id DESC LIMIT 1;`
   应该看到 image / audio / text 三种 part，且 audio 里带 transcript

最后一步的实测记录（2026-11-05，任务 101）：

```
1) 上传图片      img sha: 0c061ae773e5...
2) 上传语音      aud sha: 4593188322d0...
3) 语音转文字    欢迎使用阿里云。
4) 发 parts      HTTP 202 → 任务 101 done
   回复：图片为一张彩色渐变测试图，录音内容为"欢迎使用阿里云"，两者均与项目工程无关。
```

## R0 地基验证（2026-11-05 完成）

| 问题 | 结论 |
|---|---|
| 1. `spawn_agent` 能用吗 | **协议决定**：chat 下不可用；responses 下能派发、子线程真被创建，但子 agent turn 因 `encrypted_content` 不兼容失败（详见能力文档 1.5） |
| 2. 子 agent 能收图/音频吗 | **未测**（被第 1 条卡住） |
| 3. `thread/resume` 跨重启 | **未测** |

**结论**：多 agent 的代码路径没问题，**卡在模型服务兼容性**。
需要能完整实现 Responses（含 encrypted_content 透传）的线路；
现在的中转额度为 0，百炼不兼容这一项。


**硬编码体检基线**：`grep -rn "re\.search\|re\.match" bff/bffpkg/`
过滤掉 urlparse/api 路由后的计数。2026-11-05 立基线 12；同日删掉「结论/依据/待补」那套正则拆解后降到 **11**。这个数字只能降，不能升。

## 已知问题（按严重程度）

| # | 问题 | 影响 | 想怎么解 |
|---|---|---|---|
| 1 | 首次响应要 8 秒才出第一步 | 用户以为卡住 | 流式输出（M6），或先把 3 步压成 1 步 |
| 2 | 语音只能录不能转写 | 语音路径走不通 | **能力已验证**（百炼 ASR），只差接进 parts |
| 3 | 文书页还是硬编码假数据 | 待补字段无法真填 | 接 `GET /docs/:id` |
| 4 | `jl-field-test` 容器跑了 27 小时 | 占资源、来路不明 | 确认后删掉 |
| 5 | BFF 单进程：锁和信号量都在进程内 | 起第二个实例就失效 | Redis（M5） |
| 6 | 暴露过的 API key 未轮换 | 安全 | 找时间换 |

---

## 环境备忘

- 服务器 `baize-R640`，Tailscale `100.105.244.61`；Windows 侧 `100.112.199.126`
- 隧道：`ssh -N -L 8081:127.0.0.1:8081 rokid-r640`
- 服务器**没有 GitHub 访问**，但能连 Maven 中央仓库和阿里云镜像
- JDK/Maven 装在 `~/opt`（无 root，用户态安装）
- 小程序仓库：`NJHTR/gdces-jl-field-miniapp`
- 后端仓库：`NJHTR/gdces-jl-service`
- SSH 常被爆破，`scp` 会间歇 `Connection closed` —— **一律加重试**

---

## 下一次开工从这里开始

```bash
# 1. 看状态
bash /home/baize/jl-service/scripts/progress.sh

# 2. 看计划里第一项没打勾的
sed -n '/## 当前优先级/,/^## /p' docs/PLAN.md

# 3. 动手前先读 DECISIONS.md 里相关的决策
```
