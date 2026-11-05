# 运维手册

## 根目录

```
/home/baize/jl-service/
  .env                  密钥（600，不进 git）
  compose/compose.yml   服务编排
  compose/mysql-init/   首次启动自动执行的 SQL
  data/                 持久化（mysql / workspaces / blobs / logs）
  repos/jl-service/     代码
```

## 启停

```bash
cd /home/baize/jl-service/compose

docker compose -p jl -f compose.yml up -d      # 启动全部
docker compose -p jl -f compose.yml ps         # 状态
docker compose -p jl -f compose.yml logs -f    # 日志
docker compose -p jl -f compose.yml stop       # 停止（保留数据）
```

## MySQL

| 项 | 值 |
|---|---|
容器 | `jl-mysql` |
端口 | `127.0.0.1:3309`（只绑本机，不对外） |
库 | `jl` |
账号 | `jl` / 密码在 `/home/baize/jl-service/.env` |
字符集 | utf8mb4 / utf8mb4_0900_ai_ci |
时区 | +08:00 |

进入命令行：

```bash
PW=$(grep MYSQL_ROOT_PASSWORD /home/baize/jl-service/.env | cut -d= -f2)
docker exec -it jl-mysql mysql -uroot -p"$PW" jl
```

## 备份

```bash
# 结构 + 数据
PW=$(grep MYSQL_ROOT_PASSWORD /home/baize/jl-service/.env | cut -d= -f2)
docker exec jl-mysql mysqldump -uroot -p"$PW" --single-transaction jl \
  | gzip > /home/baize/jl-service/backups/jl-$(date +%F-%H%M).sql.gz
```

## 项目工作区

```bash
# 新建一个项目
bash /home/baize/jl-service/repos/jl-service/scripts/init-project.sh \
  <project_id> "<项目名称>" "[工程编号]"
```

生成 `data/workspaces/<project_id>/`，已 git init 并装好钩子：

- `commit-msg`：record/doc 类型必须带 `依据:` 行
- `pre-commit`：扫密钥，命中直接拒

## 踩过的坑

| 坑 | 现象 | 解决 |
|---|---|---|
`before` / `after` | MySQL 8 保留字，建 `audit_log` 报 1064，后续建表全被跳过 | 列名加反引号 |
`docker exec` 传 SQL | 少 `-i` 时 stdin 不接，SQL 静默不执行 | 用 `docker cp` + `mysql < file` |
ngram 检索 | `WHERE MATCH(...)` 可能带回 score=0 的行 | 用 `SELECT 分数 ... HAVING score > 0` 并排序 |

## 工具服务（MCP）

镜像：`jl-tools:local`（node:20-alpine + git + bash + mysql2）

**stdin/stdout 走 JSON-RPC，所以用 `docker run -i`**，日志在 stderr：

```bash
R=/home/baize/jl-service
PW=$(grep MYSQL_PASSWORD $R/.env | cut -d= -f2)

docker run --rm -i --network jl-net \
  -e MYSQL_PASSWORD="$PW" \
  -v $R/data/workspaces:/ws \
  -v $R/data/blobs:/blobs \
  jl-tools:local
```

引擎（easyCode）的 MCP 配置就指向这条命令。

### 五个工具

| 工具 | 干什么 |
|---|---|
`get_project` | 读工程概况 |
`search_records` | 查历史文书/记录 |
`search_standards` | 自然语言查规范（MySQL ngram） |
`create_doc_draft` | 写文书 + git 提交（强制依据） |
`get_context` | 一次拿项目信息 + 记忆 + 工作区路径 |

### 手工试一下

```bash
cat > /tmp/t.jsonl <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
EOF
docker run --rm -i --network jl-net -e MYSQL_PASSWORD="$PW" \
  -v $R/data/workspaces:/ws jl-tools:local < /tmp/t.jsonl
```

### 工具层踩过的坑

| 坑 | 现象 | 解决 |
|---|---|---|
镜像里没 git | 文书提交失败 | `apk add git` |
钩子用 `#!/usr/bin/env bash` | alpine 没 bash，报 `can't execute bash` | `apk add bash` |
`addgroup -g 1000` | gid 已被 node 占用 | 直接用镜像自带 `node` 用户（uid 1000） |
stdin 关闭就退出 | 小文件一次读完后进程立即死，异步工具调用被截断 | 计数在途请求，全部回完再退 |
同名文书 | 直接覆盖，丢历史 | 自动加序号 `-02.md` |
`docker exec` 灌 SQL 中文乱码 | 存成双重编码，检索为 0 | `--default-character-set=utf8mb4` 或 SQL 里 `SET NAMES utf8mb4` |
## 引擎（easyCode）

二进制：`repos/easycode/.target/release/easycode`（345MB，已 strip）

**服务器访问不到 github.com**，依赖全部 vendor 到 `repos/easycode/vendor`（1313 个 crate，1.8G），
配合 `.cargo-home/config.toml` 的 source 替换实现完全离线编译。

重新编译：

```bash
cd /home/baize/jl-service/repos/easycode
docker run --rm --user 1000:1000 \
  -v $PWD:/src -w /src/easycode-rs \
  -e CARGO_HOME=/src/.cargo-home -e CARGO_TARGET_DIR=/src/.target \
  -e CARGO_NET_OFFLINE=true \
  jl-easycode-build:local \
  cargo build -p easycode-cli --bin easycode --release
```

编译约 12 分钟（96 核）。

## BFF

`runtime/server.py`（Python 3.14 + pymysql，只绑 127.0.0.1:8081）

```bash
cd /home/baize/jl-service
nohup python3 runtime/server.py > runtime/bff.log 2>&1 &
curl -s http://127.0.0.1:8081/healthz
```

| 接口 | 作用 |
|---|---|
`GET /healthz` | 健康检查（含引擎状态） |
`POST /api/v1/chat` | 发一条 → 建 task/message → thread/start → turn/start |
`GET /api/v1/tasks/:id` | 查任务状态与消息 |

**已处理的两个坑**：

1. **引擎重启后 thread 失效** —— `thread/start` 拿到的 id 是进程内的。turn/start 报
   `thread not found` 时，自动删映射、重建 thread、重试一次。
2. **幂等** —— 同一 `clientId` 重复提交只处理一次（先查 message 表）。

## 待配

模型 provider（DeepSeek）。配好后 `easycode doctor` 的 auth 与 reachability 会转绿，
turn 才能真正产出回复。
## 记忆层

Source of truth 是 MySQL（`memory` 表），渲染成工作区文件给 agent 读。

| 接口 | 作用 |
|---|---|
`GET /api/v1/memories?scope=user\|project&ownerId=` | 列记忆 |
`POST /api/v1/memories` | 新增 `{scope, ownerId, text, source}` |
`PATCH /api/v1/memories/:id` | 改文本 / 启停 `{text?, status?}` |
`DELETE /api/v1/memories/:id` | 删除 |
`POST /api/v1/projects/:id/render-memory` | 渲染成 `memories/project.md` 并 git 提交 |

**三层**：个人（跨项目）、项目（项目内共享）、会话（引擎 thread 内，自动压缩）。

**拼装顺序**（每轮 turn 注入，项目 > 个人）：

```
【项目】名称（编号）
【工程概况】meta.json 摘要
【项目规矩】（优先级高于个人习惯）
【我的书写习惯】
【当前】项目 id · 用户 id
```

**踩过的坑**：`turn/start` 的 `additionalContext` 是 experimental 字段，
`initialize` 时必须声明 `capabilities.experimentalApi = true`，否则报
`turn/start.additionalContext requires experimentalApi capability`。
## 接口一览（BFF :8081，只绑 127.0.0.1）

| 方法 | 路径 | 作用 |
|---|---|---|
GET | `/healthz` | 健康检查（含引擎状态） |
GET | `/admin` | **后台管理页**（项目 / 记忆 / 任务） |
POST | `/api/v1/chat` | 发消息 → 建任务 → 起 turn |
GET | `/api/v1/tasks?limit=` | 任务列表 |
GET | `/api/v1/tasks/:id` | 任务详情与消息 |
GET | `/api/v1/messages?projectId=&after=&userId=` | **增量拉取**（断网续跑用） |
GET | `/api/v1/projects` | 项目列表 |
POST | `/api/v1/projects` | 新建项目（自动建工作区 + git init + 钩子） |
GET | `/api/v1/memories?scope=&ownerId=` | 列记忆 |
POST | `/api/v1/memories` | 新增记忆 |
PATCH | `/api/v1/memories/:id` | 改文本 / 启停 |
DELETE | `/api/v1/memories/:id` | 删除 |
POST | `/api/v1/projects/:id/render-memory` | 渲染记忆到工作区并提交 |

### 看后台（本机转发）

```bash
ssh -L 8081:127.0.0.1:8081 rokid-r640
# 然后浏览器打开 http://127.0.0.1:8081/admin
```

后台能做的：新建项目、增删改记忆、一键渲染到工作区、看任务状态（5 秒自动刷新）。

### 超时清理

`sweeper` 线程每 60 秒扫一次：跑了 15 分钟还没结束的任务判 `failed`（记「超时」），
避免任务永远挂在 `running`。
## 鉴权

默认**不强制**（`AUTH_REQUIRED=0`），接小程序前改成 1。

| 接口 | 作用 |
|---|---|
`POST /api/v1/auth/login` | `{openid, name?, device?}` → `{token, expiresIn, user}` |
`POST /api/v1/auth/logout` | 吊销当前 token |
`GET /api/v1/me` | 当前用户 |

- token 是随机 32 字节，**库里只存 sha256 哈希**，明文只返回一次
- 有效期默认 720 小时（30 天），可 `TOKEN_TTL_HOURS` 调
- `AUTH_REQUIRED=1` 时除 `/healthz`、`/admin`、`/auth/login` 外全部要 `Authorization: Bearer <token>`
- 首次登录带 `name` 会自动建用户

开关：

```bash
AUTH_REQUIRED=1 nohup python3 runtime/server.py > runtime/bff.log 2>&1 &
```

**实测**（8082 端口起了个强制鉴权的实例）：

```
无 token   → 401
带 token   → 200
登出后再用 → 401
healthz    → 200（不受影响）
```

## 记忆命中计数

每轮对话注入记忆时，被用到的记忆 `hits + 1`。后台「用过 N 次」就是它——
**用来看哪条记忆真在起作用，哪些是噪音。**

实测：发一条消息后三条记忆的 hits 从 0 变成 1。
## 小程序对接

小程序的 `utils/config.js` 里 `API_BASE` 从空改成 `http://127.0.0.1:8081`。
`request.js` 的 `MOCK = !API_BASE` 会自动把整条请求链切到真后端。

**隧道**（后端只绑 127.0.0.1）：

```bash
ssh -N -L 8081:127.0.0.1:8081 rokid-r640
```

微信开发者工具需勾选「不校验合法域名」。要回演示模式，把 `API_BASE` 改回空。

### 为小程序补的接口

小程序原来的调用路径和后端命名不完全一致，BFF 做了兼容层：

| 小程序调用 | 后端实现 |
|---|---|
`GET /api/v1/bootstrap` | 首屏聚合：msgs / records / docs / memos / projMemos / project |
`POST /api/v1/auth/wx-login` | `{code}` 当 openid 用（开发期），签 token |
`POST /api/v1/auth/wx-phone` | 占位 |
`GET / PUT /api/v1/project` | 21 字段工程概况（**合并式保存，不会丢字段**） |
`GET / POST /api/v1/docs` | 文书列表 / 生成（写文件 + git 提交 + 落库） |
`GET /api/v1/geo/reverse` | 占位（先返回项目地址，接第三方后替换） |
`GET /api/v1/weather` | 占位 |
`POST /api/v1/upload` | **同时支持原始字节和 multipart**（wx.uploadFile 是后者），返回 `{url}` |

### bootstrap 的数据映射

| 前端要的 | 后端从哪来 |
|---|---|
`msgs[]` | `message` 表，按天插入 stamp，role 映射成 me / agent |
`records[]` | `doc` 表按天分组，缺项算成 tags，state 映射成 st |
`docs[]` | `doc` 表 + `photo` 表（影像行） |
`memos[]` | `memory` scope=user |
`projMemos[]` | `memory` scope=project |
`project` | `project` 表 + meta JSON |

### 踩过的坑

| 坑 | 现象 | 解决 |
|---|---|---|
`PUT /project` 覆盖式保存 | 只传一个字段，其它 20 个全被抹掉 | 先读旧 meta，合并后再写 |
`PUT /project` 抹掉 name | 局部更新把 name 置空 | name/code/addr 缺省取原值 |
wx.uploadFile 是 multipart | 直传原始字节解析失败 | 加 multipart 解析，按 boundary 抠 `name="file"` |
## 接真模型（Responses API 中转）

```toml
# ~/.easycode/config.toml
model_provider = "xfcxb"
model = "gpt-5.6-sol"

[model_providers.xfcxb]
name = "xfcxb relay"
base_url = "https://llmapi.xfcxb.com/v1"
wire_api = "responses"
env_key = "JL_LLM_KEY"
```

key 存在 `/home/baize/jl-service/.env`（600），BFF 启动时注入 `ENGINE_ENV`。
**不要写进 config.toml，也不要进 git。**

实测：`easycode exec --dangerously-bypass-approvals-and-sandbox "只回答两个字：收到"`
→ 返回「收到」，tokens used 12,937（系统提示 + 工具定义占大头，短问答也要这么多）。

## 一个必须记住的坑：code-mode

app-server 模式下 `exec_command` 走 code-mode，需要
`easycode-code-mode-host` 这个二进制，它依赖 **rusty_v8 预编译包（GitHub 下载）**，
服务器上拿不到，于是所有 shell 工具都报：

```
code-mode host is disabled
failed to spawn code-mode host ... No such file or directory
```

试过 `--disable code_mode_host`、`[features.code_mode] enabled = false` 都不生效。

**但 CLI 路径（`easycode exec`）不走 code-mode，工具能正常跑**——
实测通过 CLI 让模型调 `jl-tool create_doc_draft`，真的生成了文书并提交
（`d42461a doc(监理通知单): 4#楼东侧外架`）。

结论：BFF 的执行方式要从 app-server turn 换成 CLI exec（下轮改）。

## 工具为什么做成命令行

引擎的 MCP 工具路由在这套配置下同样不通（裸名、`mcp__jl__`、`mcp__jl-tools__` 都报
`unsupported call`）。所以把 5 个工具包成 `scripts/jl-tool`，让 agent 用 shell 调：

```bash
jl-tool list
jl-tool search_standards '{"query":"连墙件 间距"}'
jl-tool create_doc_draft '{"projectId":"yun-gu-3qi","kind":"监理通知单",...}'
```

好处：trace 里能看到具体执行的命令，可追溯性更强。
## 记忆自动学习

每轮对话结束后，BFF 再问模型一次：**这轮有没有值得长期记住的东西**。

值得记：个人书写习惯（「结论写经检查符合要求」）、项目规矩（「整改回复必须附前后对比图」）
不值得记：这次的具体内容（部位、数量、日期）

模型只回一个 JSON：

```json
{"memories":[{"scope":"user","text":"..."},{"scope":"project","text":"..."}]}
```

BFF 的处理：

1. **跨作用域去重** —— 同一条习惯模型时而判 user 时而判 project，只按同 scope 比会记两遍。
   所以拿 `(scope=user, owner=uid)` 和 `(scope=project, owner=pid)` 一起比。
2. **相似度判断**（>0.8 视为重复）—— 「…的结论统一写」和「…结论统一写」算同一条。
   命中就只 `hits+1`，不新增。
3. 学到 `project` 级 → **自动重渲染 `memories/project.md` 并 git 提交**。

实测：

```
[learn] 学到 1 条: user:旁站记录的结论统一写「经检查符合要求」。
[learn] 学到 1 条: project:整改回复必须附整改前后对比图

6bbe27d memory(render): 同步项目记忆（3 条）
```

再说一遍同样的习惯 → 识别为重复，不新增，只加 hits。

**踩过的坑**：一开始只按「同 scope + LIKE 前 12 字」去重，
结果「…的结论统一写」和「…结论统一写」被判成两条，库里出现重复记忆。
改成归一化 + `difflib` 相似度，并把 user/project 一起比，才治住。
## 并发与排队

`MAX_CONCURRENT`（默认 3）控制同时跑几个 turn。超出的任务状态停在 `queued`，
拿到槽位才变 `running` —— 20 个人同时发消息也不会把机器打满。

实测（`MAX_CONCURRENT=1`，连发 3 条）：

```
38 queued
37 queued
36 running     ← 只有 1 个在跑
…
36 done / 37 done / 38 done
```

## 记忆按相关性注入

记忆多了不能全塞进上下文。注入前按**当前这句话**算相关度：

- 字符 bigram 的 Jaccard 相似度（中文不需要分词）
- 有相关的取 top 6；全都不相关时退回最常用的 3 条
- 只有被注入的记忆才 `hits+1`，所以「用过 N 次」反映的是**真正被用上**

实测日志：

```
[ctx] 记忆候选 project=8 user=2 → 注入 3+2      ← 话题无关，走兜底
[ctx] 记忆候选 project=8 user=2 → 注入 2+1      ← 提到「外架步距」，命中相关的那条
```

**没有这一步，记忆攒到几百条时上下文会爆**，而且越用越慢、越用越贵。
## 记忆改动自动重渲染

`POST/PATCH/DELETE /api/v1/memories` 只要动的是 **project** 级记忆，
就会自动重渲染 `memories/project.md` 并 git 提交，不用人工点「渲染」。

实测：

```
PATCH 改文本 → rendered: {count: 8, commit: 1a131d8}
POST  新增   → rendered: {count: 9, commit: 6179d77}
```

## 任务重试

```
POST /api/v1/tasks/:id/retry  →  {taskId: 新任务, retriedFrom: 原任务}
```

按原任务的 payload 重新入队，走正常排队流程。失败的任务不用让人重新打字。

## 轨迹文案

trace 里的 `text` 不再是「调用 search_standards」，而是人话：

```
查规范：现浇混凝土楼板 钢筋保护层厚度 实测 允许偏差 检查
写监理检查记录 · 3#楼5F顶板钢筋保护层
```

小程序轨迹条直接显示这个，不用再翻译。
## 三项验证结果

### 断网续跑（实测通过）

发完消息立刻断开客户端，70 秒后拉增量：

```
发之前 lastId = 60
POST /api/v1/chat  → 202，客户端不再轮询
70 秒后 GET /api/v1/messages?after=60
→ 新增 2 条：user + assistant
```

**任务在服务器跑完，手机回来看得到。**

### 并发（实测通过）

`MAX_CONCURRENT=3`，同时发 6 条：

```
4 秒后：  done 2 / running 1 / queued 3
20 秒后：全部 done，平均 3 秒
```

### 小程序字段契约（静态核对）

WXML 用到 `item.trace / traceSum / ms / traceOpen`，bootstrap 返回 `trace / traceSum / ms / msLabel`，
`traceOpen` 由页面 `refreshViews` 计算 —— **对得上**。

真实数据渲染后修了两处文案：

| 之前 | 之后 |
|---|---|
`调了 search_standards、create_doc_draft` | `调了 查规范、写文书`（且只统计成功的步骤） |
`用时 19786ms` | `用时 19.8 秒` |

### 格式纠正（新增）

模型偶尔不按 JSON 回（实测 1/6 概率），直接吐出散文。
现在会先纠正一次再接受：

```
[loop] 格式不对，纠正一次
[loop] task 51 done (23688ms, 2 次工具)   ← 纠正后正常调工具
```
## 引擎真正接管：code-mode host 编出来了

easyCode 的 `exec_command` 依赖 `easycode-code-mode-host`，而它要 **rusty_v8 预编译包**，
服务器连不上 GitHub。解决过程：

1. 在 Windows 上下载 `librusty_v8_ptrcomp_release_x86_64-unknown-linux-gnu.a.gz`（38MB）
   和 `src_binding_release_x86_64-unknown-linux-gnu.rs`（40KB）
2. scp 到服务器（211MB 解压后）
3. 编译时指定：
   ```
   -e RUSTY_V8_ARCHIVE=/src/v8lib.a
   -e RUSTY_V8_SRC_BINDING_PATH=/src/v8binding.rs
   ```
4. `cargo build -p easycode-code-mode-host --release` → 35 秒出二进制（199MB）

**注意**：release 里没有 `ptrcomp_sandbox` 变体，用 `ptrcomp` 的静态库 + 普通 binding 能过。

## 执行模式（ENGINE_MODE）

| 值 | 谁执行 | 说明 |
|---|---|---|
`cli`（默认） | **easyCode 自己** | 它读 AGENTS.md、用 shell 调 jl-tool、产出文书 |
`loop` | BFF 驱动 | 模型只回 JSON，BFF 执行工具（引擎 shell 坏掉时的退路） |

CLI 模式下 trace 从 `exec --json` 的 `command_execution` 事件解析出来的：

```
{"type":"command_execution","command":"/bin/bash -lc \".../jl-tool search_standards '{\\\"query\\\":\\\"保护层\\\"}'\"",
 "aggregated_output":"[...]","exit_code":0,"status":"completed"}
```

实测一次完整任务（43 秒，3 步）：

```
查历史记录 → 查规范 → 写文书
提交 320e38d doc(监理通知单): 4#楼东侧外架连墙件
```

**引擎自己决定查什么、写什么，BFF 只负责收发和落库。**
## 同一个人串行 + 项目隔离

### 每用户排队

原来只有全局限流（`MAX_CONCURRENT=3`），同一个人连发 3 条会**同时**开 3 个 turn，
三个进程同时改同一个项目工作区 —— 会互相踩。

现在每个人一把锁：

```python
with _user_lock(project_id, user_id):      # 自己排队
    with TURN_SLOTS:                        # 再抢全局并发
        run(...)
```

实测（连发 3 条，限制 3 并发）：

```
5s   57 running / 58 queued / 59 queued
30s  57 done    / 58 queued / 59 queued
45s  57 done    / 58 done   / 59 queued
60s  57 done    / 58 done   / 59 running
75s  全部 done
```

**同一个人一次只跑一个；不同用户之间并行不受影响。**

任务详情多了 `queueAhead`，小程序显示「前面还有 N 条在跑，稍等」。

### 项目隔离

之前的状态：**数据是按 project_id 查的，但没有「你能不能看这个项目」的鉴权**。
只要知道 projectId 就能读。`AUTH_REQUIRED=0` 时完全开放。

现在补了成员校验：`project_member` 表 + `guard_project()`，
开了鉴权后非成员返回 403。

实测（8082 端口起强制鉴权实例）：

| 请求 | 李工（成员） | 外人（非成员） |
|---|---|---|
`bootstrap?projectId=yun-gu-3qi` | 200 | **403** |
`docs?projectId=yun-gu-3qi` | 200 | **403** |
`tasks/57` | 200 | **403** |

**注意**：默认还是 `AUTH_REQUIRED=0`（开发方便）。上线前必须改成 1，否则隔离不生效。

### 顺手清理：22 个函数被重复定义

之前用「插到锚点前」的方式打补丁，锚点在文件里出现两次，整块插了两遍。
运行时后者覆盖前者，功能没坏，但文件里躺着 400 多行死代码。

用 ast 找出重复定义、只保留最后一个：

```
2455 行 → 2048 行，重复定义 0 个
```

之后冒烟测试六个接口全 200，无异常日志。
## 发送后的可见性（用户体验修复）

问题：反馈只存在于消息列表里。列表没渲染出来、或没滚到底，用户就完全看不到
「正在处理」——只能切走再回来，靠 `onShow` 重新拉数据才发现已经完成。

改动：

1. **底部胶囊上方常驻状态条**：转圈 +「正在处理 · 12 秒」+ 当前步骤
   ```
   ◜ 正在处理 · 12 秒
     查规范：连墙件 间距 缺失 外脚手架
   ```
2. **每秒跳秒** —— 这是"它还活着"的最强信号
3. **任务 id 落盘**（`jl_inflight`），切走/切回 `onShow` 自动续上同一个任务
4. **三处 toast**：发出「已发送，正在处理」、完成「已完成」、失败「处理失败」
5. 完成时轻震

实测（真机逻辑跑真实后端）：

```
发出后 busy = {taskId:60, elapsed:0, step:"正在发送…"}
 3s  状态条：3秒 · 正在读项目资料…
 9s  状态条：9秒 · 查规范：连墙件 间距 缺失 外脚手架
15s  状态条：15秒 · 查历史记录 · 4#楼东侧外架
69s  状态条：68秒 · 写监理通知单 · 4#楼东侧外架
结束 busy = null
回复: 我先核查"连墙件设置"相关规范条款…
产物: 监理通知单 · 4#楼东侧外架
toast: 已发送，正在处理 | 已完成
```

## 提示词补强

发现模型有时"只查资料不写文书"（用户明明说了"出通知单"）。
在上下文里把规则写死：**用户说「出/写/生成 通知单、记录、日志」时，必须真的调用
create_doc_draft，不能只在回复里描述**。

补强后同一句话能稳定产出文书卡片。
## 回复不该露出内部细节

现象（改造前）：

```
我先核查"连墙件设置"相关规范条款…已生成监理通知单：[监理通知单-4#楼东侧外架-03.md]
(/home/baize/jl-service/data/workspaces/yun-gu-3qi/docs/2026-11-05/%E7%9B%91%E7%90%86
%E9%80%9A%E7%9F%A5%E5%8D%95-4%23%E6%A5%BC%E4%B8%9C%E4%BE%A7%E5%A4%96%E6%9E%B6-03.md)。
…文书已由工具提交，提交号 d227936。
```

四个问题：

1. **泄露服务器绝对路径** —— 用户不需要知道文件在 `/home/baize/...`
2. **URL 百分号编码** —— 中文文件名被编码成一串 `%E7%9B%91`，小程序里原样显示
3. **Markdown 不渲染** —— `[文字](链接)` 在小程序里就是一堆字符
4. **自述过程** —— 四句话在汇报"我查了…我又补查…"，用户要的是结果

### 三层治理

| 层 | 做法 |
|---|---|
提示词 | 上下文加【回复要求】：不写路径/提交号/Markdown；先结论后依据；最多三句；不复述过程 |
后端清洗 | `clean_reply()`：剥 Markdown 链接、删绝对路径、删 `%XX` 串、删「提交号 xxx」、去掉开头的"我先…"叙述 |
界面载体 | 文件名/提交号/状态只出现在**卡片**里，正文不再重复 |

改造后：

```
已生成监理检查记录，当前为待补。依据 GB 50204-2015 第 8.2.1 条，
板类构件保护层厚度允许偏差为 +8mm、-5mm。
```

### 顺带修的 bug：产物卡片张冠李戴

原来按"任务开始时间之后新建的第一份文书"匹配。结果是**说一句「你好」也挂一张通知单**——
因为同一时间窗里有别的任务在建文书。

改成按 `task.doc_id` 精确绑定：开跑前记下当前最大 doc id，跑完取新增的那份写回任务。
## 项目切换从"假"改成"真"

之前的三层假：

1. **列表是硬编码的**：`["云谷智造园 · 3期", "前海金融中心 · 2期", "深湾文化中心 · 1期"]`
   —— 后两个项目在数据库里根本不存在
2. **切换只改标题**：`setData({project:{name,sub}})`，不重新拉数据
3. **切完数据还串着**：`loadAll()` 不带 projectId，服务端永远给默认项目；
   `project.id` 被覆盖成 undefined 后，chat 也回落到 `yun-gu-3qi`

**结果是：标题写着"前海金融中心"，消息其实记在"云谷智造园"里。**

现在：

```js
onProjectTap → GET /api/v1/projects（真实列表 + 建设单位 + 消息数）
             → showActionSheet
             → switchProject(picked)
                ├ 存 storage（下次打开还在这个项目）
                ├ 清空 msgs/records/docs/memos/busy
                └ loadAll(picked.id) → bootstrap?projectId=...
```

实测：

```
1) 首屏
   项目: yun-gu-3qi | 云谷智造园 · 3期 | 消息: 118 | 文书: 26
2) 切项目
   sheet[白云新城 A2 地块 / 云谷智造园 · 3期 · 中建八局华南公司]
   GET /api/v1/bootstrap?projectId=baiyun-a2
   项目: baiyun-a2 | 白云新城 A2 地块 | 消息: 0 | 文书: 0
   存储: baiyun-a2
```

顺带补了 `/api/v1/projects`：原来不返回建设单位，也没有消息数，切换面板里没法区分项目。
## 分包与契约（M6 起点）

### 之前的问题

`server.py` 一个文件 2900 行，同时干了四件事：对外 API、业务规则、Agent 调度、数据写入。
两个后果：

1. **同一个业务对象有两个写者**：Agent 的 `create_doc_draft` 直接 INSERT doc 表，
   将来人工编辑也写 doc 表 —— 并发就打架，而且没法做事务
2. **换语言等于重写整个服务**：业务规则和调度器缠在一起

### 现在

```
contracts/openapi.yaml      30 条路径 / 36 个操作，每个标了 x-owner
bff/bffpkg/
  core.py    公共底座：配置、数据库、文本清洗、分段   (1092 行)
  biz.py     文书·项目·成员·审计·规范·统计          ( 676 行)  ← 未来抽 Java
  agent.py   引擎·任务编排·工具轨迹·记忆             (1110 行)  ← 留 Python
  api.py     HTTP 路由                              ( 237 行)
bff/entry.py  入口（5 行有效代码）
runtime/entry.py  部署位置；runtime/server.py 保留为单文件备份
```

### 分开的依据

| 层 | 归属 | 为什么 |
|---|---|---|
`doc/project/photo/audit/standard` | biz | 真值和规则（状态机、字段校验、权限）。将来接电子签章、PDF、国密，生态在 Java |
`task/message.trace/thread_map` | agent | 跟子进程、文件、git 打交道，Python 更顺手 |
鉴权/上传/blob/地理 | infra | 纯基础设施 |

### 拆分时踩的坑（都修了）

1. `from module import *` **不会导入下划线开头的名字**（`_day_label`、`_split_num` 全丢）
   → 改成显式列出名字
2. 顶层 `if` 语句被漏掉，导致 `ENGINE_ENV["JL_LLM_KEY"]` 注入丢失，
   引擎报 `Missing environment variable: JL_LLM_KEY`
   → 把模块级 if/try 也搬过去
3. `pymysql` 装在用户目录，必须 `sys.path.insert` 在 import 之前 → 放在 core 第一行
4. 按环境变量杀进程杀不掉（`BFF_PORT=8087` 不在命令行里）→ 按脚本名 `pkill -f runtime/entry.py`

### 验证

```
healthz: {"ok": true, "engine": "up"}
bootstrap / docs / tasks / stats / audit / members  全部 200
真实任务：45.5 秒完成，7 条时间线、1 条引用、7 个片段、产物正确
```

### 中间件的上线顺序（不上的后果排序）

| 顺序 | 组件 | 触发条件 | 不上的后果 |
|---|---|---|---|
1 | 对象存储 | 上第二台机器前 | 照片只在一台机器，换台 404 |
2 | Redis | 起第二个 BFF 实例 | 每用户锁失效，两人同时改工作区 |
3 | 队列 | 任务量超单机 | 只能同步排队，重启丢任务 |
4 | Kafka | 要做实时看板/多下游 | 只能轮询数据库 |


---

## 推送不通的真相与解法（2026-11-06 08:50 实测）

### 现象

```
git push → fatal: unable to access ...: OpenSSL SSL_connect: SSL_ERROR_SYSCALL
```

### 排查（不要停在「网络坏了」）

```
TCP 443 连通      → True
TCP 22  连通      → True
HTTPS 握手        → 失败（OpenSSL 与 schannel 都失败：failed to receive handshake）
SSH 握手          → 成功（GitHub 回 Permission denied (publickey)，说明连上了，只是没授权）
```

**结论：不是断网，是 HTTPS 的 TLS 握手被中途重置；SSH 通道是好的。**

### 解法（一次性）

1. 本机公钥（把整行贴到 GitHub → Settings → SSH and GPG keys → New SSH key）：

```
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCcKiGu1icjlwvoRoDZPRfjy7IAyMo+I2dxUW1mc3qWssI85qDQaRHkDGgEiU/yTvIeXQE5kZbF81LqYEtI333wXoc219qRGOfRWyDobwAnFG77k5h4qZvquHiKz1PNO6MhMsh34VdahfYf46UXv7GCVcpGitzpyuBXmAGdQqXQm9KgsrA2bE2vootFG5j5EE9MNiPGiX3hBoHwTCZtOPGXwAM17M9xqZYCfNT/O1i4I0WLCUX3cp7u7vg9M/SeZNL0ZUH/Mbgsw/m724Jjh6SrDQ5692iwZS14KrkMCDClsDWVWd4YrF7F7KMnFBwMxvy5PlrQVat7ar+qILd9dGxV csm@DESKTOP-7DT3AT7
```

指纹：`SHA256:S3CdKRsYZpG83+z0A/v4oUVIukyyiRTc12jTzzQE8E0`

2. 已经配好 SSH 远端（没动 origin）：

```bash
cd /c/Users/NJHTR/IdeaProjects/jl-service
git push ssh main            # 后端

cd /c/Users/NJHTR/WeChatProjects/jl
git push ssh main            # 小程序
```

### 为什么不用换 origin

origin 走 HTTPS，在别的网络环境（比如公司网）可能是通的。
保留它，SSH 作为备用通道 —— 两条都能用，不互相影响。

---

## 部署/改核心代码后的固定动作

```bash
python3 scripts/preflight.py     # 引用 / 静默失败 / 硬编码 三条硬线
python3 scripts/smoke-e2e.py     # 核心链路 13 项断言（约 60 秒，自清理）
```

**为什么要养成这个习惯**：2026-11-06 一天里，光靠「能跑」的冒烟测出了三个用户可见的 bug
（补字段被丢、缺字段能标已签、task.doc_id 没写导致 artifact 永远为空），
每一个都是「不报错、不崩、界面说成功」的类型。

smoke-e2e 会在 test-sandbox 里建文书、验完自动删（库 + 文件）。
要排查就加 `--keep` 保留现场。
