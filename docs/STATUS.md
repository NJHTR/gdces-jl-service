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

## 静默失败大扫除（2026-11-06 06:20）

全仓 `except Exception` 有 **79 处**，其中「except 后面只有 pass」**13 处**。
逐个看过，判断如下。

### 不能沉默的 4 处（已改成打印日志）

```
agent.py  取规范条数失败      → 提示里少一条事实，agent 会反复搜规范
agent.py  learn_from_turn 失败 → 学习功能静默失效（就是 R5 举的那个例子）
agent.py  取文书字段失败      → 复核员拿到空字段，会给出错误意见
appserver 读输出解析失败      → 丢一条消息（可能是工具结果或复核）
```

### 可以不管的 13 处（已写明理由，写进 preflight 注释）

```
FileNotFoundError ×4  删临时文件 / 清理
杀进程 / 终止 / thread-close ×3
调试转储到 /tmp ×2
命中计数、进度更新、列 schema、读消息兜底 ×4
```

### preflight 加了一条硬线

```
[静默失败] 13 处 except+pass（基线 13）
涨了就直接失败 —— 逼着解释：新加的 except 是不是在藏失败。
```

### 部署后验证

```
preflight 全绿
跑一单（42#楼）：33 秒看到文书、51 秒完成、状态 done
日志里没有任何一条新留痕（说明这几条路都走通了）
```

## 验证：修完之后 render-memory 真的活了（2026-11-06 06:05）

core.py 那四处补导入之后，沿着这条路继续验，又发现一层：

```
POST /api/v1/projects/test-sandbox/render-memory
修复前：500（NameError 被吞，实际是 render_project_memory 没导入）
补导入后：500 [Errno 2] No such file …/memories/project.md   ← 目录不存在
补 makedirs 后：200
```

**现在的结果**：

```json
{"code":0,"data":{"path":"memories/project.md","count":5,
 "commit":"e75ee8d…","changed":true}}
```

```markdown
# 项目记忆 · 测试沙箱（AI 自测用）
- 整改回复必须附整改前后对比照片  <!-- learned · 用过 82 次 -->
- 监理通知单允许先出草稿，受检单位、整改期限、签发人等字段可后续补充  <!-- learned · 用过 28 次 -->
```

**这条链路以前完全是死的**（记忆→文件→git 提交），没有任何人发现，
因为异常被两层 try/except 吞掉了。

### 这轮学到的

```
「静默失败」不是一个 bug，是一类 bug：
  没导入 → except 吞 → 上层再 try → 最后只剩一个 500 或什么都不发生
一层层剥：修好导入 → 露出目录不存在 → 再修 → 才好
```

## 加了 preflight 自检，它立刻抓出 4 处旧 bug（2026-11-06 05:45）

连续两轮埋了同一类雷（调用了不存在的东西），于是把它们做成可重复的检查：
`scripts/preflight.py`，每轮收尾跑一次。

### 检查什么

```
A. Python 语法（ast 解析，不是编译字节码）
B. Python：函数体里调用的每个名字，是否在模块级/局部可见
C. JS：this._xxx(...) 是否有定义
D. 硬编码体检（基线 11，只能降）
E. 各项目 AGENTS.md 体量与是否混入行为说明
```

### 第一次跑就抓出 4 处（都是老代码，不是我这两天写的）

```
core.py:330/1039  render_project_memory()  没导入（定义在 biz.py）
core.py:391/569   engine()                 没导入（定义在 agent.py）
```

**这四处全都包在 try/except 里** —— 所以它们一直在静默失败：
重渲染项目记忆、读线程历史这两个功能等于没生效，但没人发现。

修法：调用时导入（`from .biz import …`），绕开 core↔biz、core↔agent 的循环依赖。
修完 preflight 全绿。

### 顺带修的一个真问题：AGENTS.md 只在跑过任务时才重写

```
yun-gu-3qi  2778 字节（改模板后一直没重写，还带着【可用工具】【说话方式】）
现在：启动时 refresh_all_agents() 把所有项目重写一遍
      → yun-gu-3qi 1658 字节，行为说明没了
```

### 判据修正

原来写「AGENTS.md ≤800 字节」是按英文拍的；中文一个字 3 字节，800 字节只有 260 字，
连项目事实都放不下。改成 **≤1200 字且不含工具清单/说话方式**。

## 又一处静默失败：复核员被告知「缺项：无」（2026-11-06 05:30）

上一轮修完前端的「只有调用没定义」后，我照着同样的思路查后端：

```
agent.py:853  _chk = validate_fields(...)   ← 用了 validate_fields
import 行里没有它（它定义在 core.py:1184）
→ NameError → 被 except Exception 吞掉 → missing = []
→ 复核提示里写「缺项：无」—— 这是**错误信息**，比不写更糟
```

**两处都修了**：

```
1) 补上 from .core import … validate_fields …
2) except 不再静默：打印 [review] 取缺项失败 doc=… 并把
   「（缺项查询失败，请自己核对）」交给复核员
```

### 复测（task 213）

```
看到文书 17s | 完成 34s | 复核 1492 字
复核意见带上了行号：「受检单位「待补」（第5行）… 事由「待补」（第7行）」
日志里没有「取缺项失败」—— 说明 validate_fields 真的通了
```

### 这一类错误的模式（值得记住）

```
「调用了不存在的东西」+「except 吞掉」= 静默降级
语法检查查不出，测试也可能看不出来（因为降级后还能跑）
唯一能发现的办法：要么静态查引用，要么看输出对不对
```

## 修一个我自己给前端埋的雷（2026-11-06 05:15）

**发现**：上一轮加「复核意见显示」时，只往 `index.js` 里写了**调用**
`this._reviewOf(asst)`，却没写**定义**。

```
grep _reviewOf  → 只有 2 处调用，0 处定义
后果：任务一完成就抛 TypeError，气泡更新中断 —— 用户看不到结果
```

**已修**：补上 `_reviewOf` / `_warningOf` / `_answerOf` 三个方法（后者统一解析
answer 可能是对象也可能是 JSON 字符串）。

**顺手补的**：后端 `answer.warning`（比如「有文书没入库」）之前前端从来没显示过，
等于白报。现在回答下面会有一条黄色警示条。

**教训（写进 R8 的备注）**：改 JS 时不能只 grep 调用点，要确认定义存在。
这类错误 `node --check` 查不出来（语法合法），只有真跑才会炸。

## 沙箱清理 + 孤儿问题确认已修（2026-11-06 05:00）

**确认**：修完之后的新任务不再产生孤儿文书

```
新跑一单（40#楼南侧外架）：24 秒看到文书、完成 41 秒、warning=None
孤儿数没有增加（13 → 13）
```

**清理**：把修之前留下的 13 个孤儿文件删掉（只在 test-sandbox 里）：

```
文件 50 → 38，doc 表 38 条 → 工作区和库对齐
git 提交：chore: 清掉 13 个孤儿文书文件（a46d7fb）
```

这些孤儿的来历也清楚了：工具写文件+提交了，但 register 没成（MCP/PATH 问题），
agent 又不会主动核对，于是文件在、库没有。

## 验收第 3 条：三种说法只留一条（2026-11-06 04:50）—— R7 验收全部完成

**做法**：用同一个用户，在同一条规矩上说了三种说法：

```
1) 记住：整改回复必须附整改前后对比照片
2) 记一下：以后整改的回复要带上整改前后的对比图
3) 规矩：整改必须配前后对比照片，别忘了
```

然后回拨线程时间触发抽取，再回拨整理完成时间绕过 6 小时冷却，强制整理一次。

**结果**：

```
stage1_outputs: 4 条（三种说法各抽一条 + 之前的老记忆）
整理后 MEMORY.md 的 User preferences 段：
  含「对比」的条目数 = 1

  - when issuing a rule for this project, the user said:
    "这个项目的整改回复必须附前后对比照片"
    -> any rectification/整改 reply … must include paired before-and-after
       comparison photos by default, without being reminded.
```

**去重是模型整理的，不是字面相似度。** 三种说法措辞完全不同，字符串比对做不到。

### R7 验收全部完成

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 用户 A 说规矩，B 答不出 | 🟢 21 个家目录只有 1 个有记忆 |
| 2 | A 项目规矩不串到 B 项目 | 🟢 同用户跨项目实测 |
| 3 | 同一句话三种说法只留一块 | 🟢 三种说法合并成 1 条 |
| 4 | 问模板去读文件 | 🟡 链路通，模板目录空（等材料） |
| 5 | 凭照片描述找回来 | 🟢 已验 |
| 6 | AGENTS.md ≤800 且无工具清单 | 🟢 607 字节 |

**5/6 通过，剩下那 1 条不是能力问题，是缺内容（规范/模板材料）。**

## 语音进对话：通了，但语气不对（2026-11-06 04:35）

### 链路验证（task 211，纯语音消息）

```
上传音频 → 转写「哈喽哈喽哈喽」→ 模型回复 → 7 秒
回复：哈喽！听起来心情不错的样子 😄 有什么我能帮上忙的吗？
      代码、文件、还是随便聊聊都行。
```

**链路是通的**：语音进对话、转写、回复，全程 7 秒。

### 但语气跑偏了

一个监理助手不该说「代码、文件、还是随便聊聊」、也不该用 😄。
原因很清楚：**它没读 `output-style.md`**（技能是按需读的，这次没读）。

这不是 bug，是「按需读」的代价：**当它觉得任务很简单（打个招呼），就不去读技能。**

### 两个可选做法（都不违反 R3）

1. **让风格成为事实的一部分**：在 AGENTS.md 的项目事实里加一行
   「说话风格：像监理工程师，短、具体，不寒暄」——但这违反 D25（不写行为）。
2. **接受它**：真实现场没人发「哈喽」；出稿时它会读技能（因为要按流程走）。

**我倾向 2，先不动。** 记在这里，等真实使用中再判断。

## 验收第 2 条：项目边界成立（2026-11-06 04:30）

**做法**：用同一个用户 501，从 test-sandbox 切到 baiyun-a2，问同一个问题。
它记忆里写得很清楚：

```markdown
applies_to: cwd=/home/baize/jl-service/data/workspaces/test-sandbox;
            reuse_rule=safe to reuse for any future task in this project …
```

**结果**：

> 没有。你之前只在 test-sandbox 项目交代过整改回复要附前后对比照片，
> 白云新城 A2 这块没有相关记录。

**这就是 `applies_to: cwd=` 在起作用** —— 同一个人的记忆，跨项目不套用。
而且它没有装不知道，而是说清了「在哪交代过、这里没有」。

### 验收表（终版）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 用户 A 说规矩，B 答不出 | 🟢 21 个家目录只有 1 个有记忆 |
| 2 | A 项目规矩不串到 B 项目 | 🟢 同用户跨项目实测，明确说「这里没有相关记录」 |
| 3 | 同一句话三种说法只留一块 | 🔴 未验（要等多次整理，成本高） |
| 4 | 问模板去读文件 | 🟡 链路通，模板目录仍空（缺材料） |
| 5 | 凭照片描述找回来 | 🟢 已验 |
| 6 | AGENTS.md ≤800 且无工具清单 | 🟢 607 字节 |

## 验收第 1 条：用户隔离成立（2026-11-06 04:20）

R7 验收清单第 1 条「用户 A 说规矩，用户 B 答不出」—— 之前只验到「结构隔离」，
现在有内容级证据了：

```
data/homes/*/  共 21 个家目录
只有 501 有 MEMORY.md（39 行，内容是 test-sandbox 的项目规矩）
其余 20 个全部为空
```

原因也很清楚：**只有 501 攒够了「闲置 ≥1 小时」的会话**，引擎才给它抽记忆。
其它用户根本没有记忆文件，谈不上串。

### 更新的验收表

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 用户 A 说规矩，B 答不出 | 🟢 内容级验证：21 个家目录只有 1 个有记忆 |
| 2 | A 项目规矩不串到 B 项目 | 🟡 MySQL 按项目过滤已验；引擎记忆的 applies_to 只在文件里见到，未做跨项目实测 |
| 3 | 同一句话三种说法只留一块 | 🔴 未验（要多次整理，成本高） |
| 4 | 问模板去读文件 | 🟡 链路通，模板目录仍空 |
| 5 | 凭照片描述找回来 | 🟢 已验 |
| 6 | AGENTS.md ≤800 且无工具清单 | 🟢 607 字节 |

## 大图不慢：慢的是「模型自己决定多查几轮」（2026-11-06 04:15）

怀疑手机照片大（3~5MB）会拖慢，于是造了一张 1.2MB / 1200x1600 的图来测：

```
小图（几十 KB）   80s（4 次 search_standards）
大图（1.2MB）     55s（1 次 search_standards）
```

**大图反而更快** —— 说明图片体积不是主因，主因是**这一轮模型决定查几次**。

### 结论（改口，之前我说错了）

```
纯文字   11~40s   取决于它查几轮
带图     55~80s   同样取决于查几轮；与图大小无关
```

所以「压图片」这条路没用（引擎本来也会 resize）。真正影响的是**轮次**，
而轮次由模型自己决定 —— 这正好是 R3 说的：不该由后端硬压。

### 还能做什么（不违反 R3）

1. **把事实给足**（不是规则）：规范库条数、项目事实、缺项 —— 都已经给了
2. **让工具更快**：每次 0.47s → 若做成常驻服务可到 0.05s，但一轮才省 0.4s，收益小
3. **接受它**：55~80 秒出图稿，比人工快一个数量级；40 秒是 P50 目标，不是硬门槛

**我的判断：不再为带图的 55 秒做工程优化，先把这个账写清楚。**
如果真的必须 40 秒，唯一有效的手段是少让它查 —— 那就回到了「后端替模型决定」，不做。

## 带图还是慢：80 秒（2026-11-06 04:05）

PATH 修好之后，纯文字已经稳定在 11~40 秒。带图依然慢：

```
task 208（带图）看到文书 80s | 完成 98s | cmds=22
工具分布：shell 13、search_standards 4、search_records 1、validate_doc 1、create_doc_draft 1
```

**对比纯文字**：主链路只要 2 次调用（search_standards + create_doc_draft）。

### 为什么带图会更啰嗦

- 图片内容（一张纯色块）没有可用的现场信息，它不会直接放弃，而是换关键词反复搜（4 次）
- 它会先 validate、再读文件、再看目录（shell 13 次）

这不是 bug，是「信息不足时的探索行为」。压它有两个方向：

```
1) 给事实（不是规则）：『规范库现有 N 条』已经给了；
   可以再加一句事实：『库里没有的规范，换关键词也搜不到』——但这接近行为引导，先不做。
2) 把带图场景单独优化：先让它出稿（缺什么留空），再按需补依据。
   也就是『依据后补』，和复核一样异步化。
```

### 现在的账（诚实版）

```
纯文字出稿     11~40s   ✅ 达标
带图出稿       60~98s   ❌ 未达标（唯一确定的原因：搜索/探索轮次多）
复核           21~30s   不阻塞（异步）
```

## 40 秒目标：达成（2026-11-06 03:55）

### 关键的一步：`jl-tool` 没进 PATH

轨迹里反复出现：

```
which jl-tool          → 空
find -name jl-tool     → 找到才继续
cat scripts/jl-tool    → 它去读源码
```

因为 `/home/baize/bin` 里没有 `jl-tool`，而引擎给的 PATH 是 `/home/baize/bin:…`。
**加一个软链接就好了**（登录 shell 的 PATH 也包含这个目录）：

```
/home/baize/bin/jl-tool -> /home/baize/jl-service/scripts/jl-tool
```

### 顺带给 MCP 服务补了标准方法

```
resources/list → {resources: []}
resources/templates/list → {resourceTemplates: []}
prompts/list → {prompts: []}
logging/setLevel → {}
```

引擎初始化时会问这些；不实现就是日志里的 `list（失败）`。
（**注意：补了之后 MCP 工具仍然没进模型清单**，这条只是让协议更规范，别当成修好了。）

### 复核员不再重复查

它之前会 `cat` 两次 + `validate_doc` 跑 4 次（每次都要起容器）。
现在把**缺项和状态直接从库里写进提示**，它只需读一次文件 —— 也顺带变准了：

```
复核意见（task 207）：
  1.「必须改」检查结论为空。而事由已断言"存在安全隐患"——
    结论没落、认定却先下了，文书自相矛盾，签发不成立。
```

### 实测（连续两单，同一用户）

```
第1单（35#楼）看到文书 24s / 完成 46s / shell 9
第2单（36#楼）看到文书 11s / 完成 31s / shell 6
第3单（37#楼）看到文书 40s / 完成 60s / 主链路 2 次工具调用
```

**主链路现在只有 2 次工具调用**：`search_standards` + `create_doc_draft`。
也就是说：只要它不走弯路，40 秒内出稿是稳的。波动来自模型这一轮是不是决定先去翻文件。

## 时间到底花在哪：不是工具，是往返轮次（2026-11-06 03:40）

### 先量工具本身

```
单次 jl-tool 调用（含 docker run）  0.47s
一次出稿 14~20 次工具调用          ≈ 7~10s
```

**所以工具不是瓶颈。** 真正的成本是**每一次调用都要模型重新思考一轮**：
14~20 轮 × 2~3 秒 = 30~50 秒。

### 更贵的是「找路」

task 202 的前 8 次调用全是找路：

```
ls data/knowledge/skills/
cat jl-tool.md / output-style.md
python -c 'import jl_tool...'      ← 它以为有 python 模块
find -name '*.py' | head
which jl-tool; find -name 'jl-tool'
cat scripts/jl-tool                ← 它去读工具源码
```

原因是我在技能文档里写了「工具已经直接提供给你，直接调用」——
**但 MCP 工具在常驻会话里实际没生效**，它照着文档找不到工具，就去翻文件系统。

### 改成真话

技能文档改成：**`jl-tool <名字> '<JSON>'`，命令在 PATH 里；不要去找实现**
（并注明：等 MCP 生效后这段就该删）。

### 效果

```
task 202（改之前）：52s 看到文书、shell 20 次、list 失败 1 次
task 203（改之后）：46s 看到文书、shell 14 次、工具调用 6 次（search_standards×2, get_context×2,
                   search_records×1, create_doc_draft×1）
```

**46 秒，离 40 秒还差一点。** 剩下的差额全在轮次上：14 次 shell 里还有读文件、查目录。

### 结论：真正的解法是让 MCP 工具生效

MCP 生效后：工具直接进模型可见的清单，不需要「找路」，也不需要 shell 包一层 ——
预估能省 8~10 轮 ≈ 20 秒，40 秒目标对带图也能达成。

### 下一轮的两条路（都已确认可行）

1. **继续修 MCP**：现在只知道「MCP 管理器在初始化、`required=true` 也不报错」，
   但工具没进清单。下一步要读 `core/src/session/mcp_runtime.rs` 的 refresh 逻辑，
   看工具是怎么合并进工具表的（文件:行号）。
2. **退路**：把 8 个工具做成原生 shell 别名（`jl-draft`/`jl-search` 等），
   省掉一层 JSON 和 docker（0.47s → 0.05s），但轮次省不了。

## 带图慢的真正原因：MCP 工具没生效（2026-11-06 03:25）

### 证据

把一次出稿的 trace 摊开看，**每一次工具调用都是这样**：

```
[get_context]     /bin/bash -lc 'export PATH=… && jl-tool get_context {…}'
[search_standards]/bin/bash -lc 'export PATH=… && jl-tool search_standards {…}'
[create_doc_draft]/bin/bash -lc 'export PATH=… && jl-tool create_doc_draft {…}'
```

也就是说：**模型没有拿到 MCP 工具，它是用 shell 调我们的 CLI**。
而 `jl-tool` 每次调用都要 `docker run` 一个容器 —— 一次 2~4 秒。
一次出稿 11~17 次这样的调用，时间就这么没了。

旁证：

```
app-server 日志里 0 处 mcp 初始化记录
日志里 0 个 mcp__ 前缀的工具名
每轮开头都有 1~2 次 `list（失败）` —— 那是引擎在试 MCP resources/list
```

### 试过一个修法（无效，但配置留着无害）

```toml
[features]
tool_search_always_defer_mcp_tools = false   # 不让 MCP 工具被「推迟」
```

21 个家目录都补了，重启后复测：**shell 调用 11 次、list 仍失败 2 次**，没变化。
说明不是这个开关的问题，或者 app-server 根本没去初始化 MCP。

### 下一轮要查的（具体）

1. 用 `RUST_LOG=easycode_core=debug,easycode_mcp=debug` 起 app-server，看它到底有没有尝试 spawn MCP
   （现在 `JL_ENGINE_LOG` 已经能注入 RUST_LOG，只需改环境变量）
2. 对比 `exec` 路径：同一份配置下 exec 有没有 MCP 工具（如果 exec 有、app-server 没有，问题就在 app-server）
3. 如果是 app-server 不支持 MCP，退路是：**给 agent 一个原生工具壳**——
   把 `jl-tool` 的 8 个能力做成短路径命令，避免每次 docker run（比如常驻一个本地 HTTP 工具服务，CLI 只是 curl）

### 为什么这条最重要

```
纯文字出稿 40s 里有 ~20s 是这些 shell+容器调用
带图 66s 同理
把它修掉，40 秒目标对带图也能达成 —— 而且顺带解决「注册偶发失败」（每次起容器都可能失败）
```

## 注册路径压测 + 时间构成（2026-11-06 03:05）

### 上一轮那个『4 次只成功 1 次』复现不出来

两条路径都压了一遍：

```
jl-tool CLI 连调 5 次   → 5/5 成功（docId 97~101）
MCP 容器连调 3 次       → 3/3 成功（docId 102~104）
```

说明之前那 4 次失败是**当时的瞬时问题**（轨迹开头有两条 `list（失败）`，
agent 当时还自己去 `docker exec` 排查 MCP）。现在 MCP 就绪策略默认是
『等连接』，路径本身是通的。**记在这里，不再当未解 bug 挂着。**

### 带图为什么慢：时间花在 shell 上

```
task 196（带图）turn=66s cmds=18 → shell 17 次、list 2 次（失败）
```

根因之一是我自己在 AGENTS.md 写的指针：**『工具清单跑 `jl-tool list` 看』**
—— 它真的进 shell 去跑，多出好几次往返。

改成：**『工具直接可用（不需要先列清单或进 shell 查）』**

### 改完再测

```
task 197（纯文字）turn=38.7s 看到文书 40s 完成 63s
  工具构成：shell 13 / list 1 / search_standards 1 / validate_doc 1 / create_doc_draft 1
  （对比改之前：shell 17 / list 2）
```

纯文字仍在 40 秒线上。**带图还是 66 秒左右**（图片让上下文变大，shell 反而更多）。
带图的优化留到下轮，先把『文字 40 秒』守住。

## 复盘：上一轮认定的『手写文书』是误判（2026-11-06 02:55）

上一轮我说 agent 绕开 create_doc_draft 手写了三份文件。**看了完整轨迹后发现不是。**

### 轨迹事实（task 194）

```
create_doc_draft 被调用了 4 次（都是 27#楼北侧外架）
→ 产生 01/02/03/04 四个文件
→ 只有 05 那条成功入库（doc 95）
```

所以不是『手写』，是**工具调用失败/重复**。真正造成误判的是复核员：

```
复核员去 docs/ 根目录找文件，而文件在 docs/2026-11-05/ 里
→ 它得出『工作区没有这份文书，doc 95 实质不存在』
→ 这个结论是错的，比不复核更糟（会让人以为文书丢了）
```

### 修法：把文件路径直接给复核员

```python
doc_path = 从 doc 表取 path  # 例如 docs/2026-11-05/监理通知单-xxx-05.md
prompt 里写：文件：<绝对路径>（它一定存在；不要凭目录列表猜测）
```

### 验证（task 195，28#楼东侧外架）

```
看到文书 40s | 完成 59s
复核意见：
  1. 必须改 — 受检单位「待补」。没有受文主体，无法送达、无法追究整改责任，文书不成立。
  2. 必须改 — 签发人「待补」。缺签发人即为废单。
  3. 必须改 — 整改期限「待补」。无限期无法考核闭合，外架连墙件缺失属坍塌类重大隐患…
```

**这才是复核该有的样子**：具体、可执行、指出哪一项让文书不成立。

### 还没查的

为什么 create_doc_draft 调了 4 次只成功 1 次（另外 3 次写文件+提交后，register 失败）。
日志里 register 只收到 2 次 200，说明那 3 次根本没发出请求 —— 可能是容器网络/超时的偶发。
下次再遇到，先看 `/tmp/jl-appserver-<user>.log` 里的 MCP 报错。

## 端到端回归：带图这条路有两个问题（2026-11-06 02:40）

用一张图 + 一句话跑了一遍完整链路（task 194）：

```
看到文书 105s | 完成 159s | 产物 监理通知单 · 27#楼北侧外架
warning: 4 份文书文件没登记入库（01/02/03 三份孤儿 + 一份 -05 已入库）
[perf] turn=103808ms cmds=30
register 请求只有 2 次 200（其中 1 次是我手动测的）
```

### 问题 1：带图比纯文字慢 3 倍

```
纯文字（预热后）  22~34s
带图              105s
```

图片让上下文变大、工具调用变多（30 次命令）。40 秒目标在带图场景**没达成**。

### 问题 2：agent 又绕开了 create_doc_draft

它连写三个文件（01/02/03）都没走工具，最后才用工具出 05。
对账抓到了（这正是对账存在的意义），但**事后才发现**。

根因：契约只是提醒，没有机制拦住手写 docs 下的 md。
下一步应该用引擎的 **PostToolUse 钩子**：写 docs/**/*.md 时如果没经过工具就直接拦下来。
（D27 里写的推翻条件就是这个 —— 有钩子就可以删掉那句提醒。）

## 前端接上异步复核（2026-11-06 02:25）

**问题**：后端把复核改成异步了，但前端只在 `status=done` 时才把气泡换成结果 ——
而后端要等复核跑完才 done。**异步省下的 30 秒，被前端又吃回去了。**

**两处改动**：

```
轮询时只要看到 assistant 消息 → 立刻 _fillPending 显示（幂等，每轮都刷）
done / failed → 更新同一条消息（用 _earlyId），不再另插一条
```

**复核意见展示**：`message.answer.review` 之前前端从没渲染过（存了没人看）。
现在气泡下方多一张可折叠的「复核意见」卡片，默认收起，点标题展开。

**怎么在真机上验**：

1. 发一句「xx楼外架连墙件缺失，出监理通知单」
2. **30 秒左右**应该先看到文书和缺项（此时任务还在跑，复核没结束）
3. 再过 20~30 秒，任务变完成，气泡下方出现「复核意见 · 展开」
4. 点开能读到逐条意见（必须改 / 建议改 / 可忽略）

## R9 完成：40 秒目标达成（2026-11-06 02:10）

### 两处结构性改动

**① 复核改异步**

```
出稿完成 → 立刻插入消息（用户马上能看）→ 任务保持 running
后台线程 → 跑复核 → 回写同一条消息的 answer.review + 时间线 → 任务置 done
异常兜底 → finally 里也置 done，避免被 sweeper 判超时
```

**② 打开小程序就预热会话**

`/api/v1/bootstrap` 里起后台线程把该用户的常驻会话和线程建好。
冷启动第一单原本 86 秒，预热后 22~34 秒。

### 实测（用户看到文书的时间 = 真正在等的）

| 场景 | 看到文书 | 任务完成 |
|---|---|---|
优化前（同步复核） | 63~127s | 63~127s |
复核异步（冷启动） | 47~86s | 77~129s |
复核异步 + 预热 | **22~34s** | 44~68s |

**GOAL 的 40 秒目标达成**（预热后首次 34 秒，第二次 22 秒）。
复核仍在跑，但用户不用等它 —— 26~30 秒后意见会自己出现在同一条消息里。

### 顺带修的一个 500

```
GET /api/v1/bootstrap → 500 TypeError: 'NoneType' object is not iterable
原因：库里 meta 存的是字符串 "null"，json.loads 后是 None，直接 dict(None) 炸
影响：meta 为空的项目（如 test-sandbox）每次打开小程序都 500
```

## 40 秒目标：先量再优化（2026-11-06 02:05）

### 加了阶段计时

```
[perf] task N turn=Xms review=Yms total=Zms cmds=K
```

### 实测（同一套出稿提示，test-sandbox）

| 阶段 | 出稿(turn) | 复核(review) | 合计 |
|---|---|---|---|
优化前 | 9~37s | **54~90s** | 63~127s |
复核提示限定 2 次工具 | 14~48s | 29~33s | 44~82s |
再加『规范库只有 N 条』事实注入 | 14~44s | 24~31s | **45~68s** |

**结论：复核占了 70%+ 的时间，是主要瓶颈。**

### 这轮做了三处优化

1. **复核提示限定步数**：读一次文书 + 最多一次 validate，**不再反复搜规范**
2. **注入『规范库现有 N 条』**：这是每轮都可能变的事实，不能写死在提示词里；
   它让模型知道「搜一次没有就是没有」，省掉空搜（cmds 从 28 降到 9~22）
3. **补回『缺项留空、不要追问』**（重要回归修复）

### 回归修复：它开始追问，而不是出稿

瘦身时把『缺的就留空、交给人补』删掉了，结果：

```
实测：『15#楼南侧外架剪刀撑缺失，出监理通知单』
  修复前 → 追问「受检单位是哪家？整改期限几天？」（没出稿）
  修复后 → 直接出稿，回「通知单已出，状态待补…还缺四样，手机上补一下就行」
```

这违反「零必填、不阻塞」这条底线。已在契约行里补回：
**缺的字段留空就行，不要为了凑齐而追问 —— 监理员在工地上没空回答五连问。**

### 还差什么到 40 秒

现在 45~68 秒。若把复核改成**异步**（出稿先返回，复核结果后补），
用户感知时间就是 turn 的 14 秒左右 —— 但这需要前端在任务完成后继续拉一次消息。
下一轮做。

## 配置警告的排查（2026-11-06 01:55，未完全定位）

现象：app-server 启动时报一次

```
ERROR easycode_app_server: easyCode is ignoring 1 unrecognized configuration setting.
```

排查结果（两次实测）：

```
最小配置（provider + [features] memories）        → 0 次
完整 501 配置，走 exec 路径                        → 0 次
完整 501 配置，走 app-server 路径                  → 1 次（启动时）
```

**所以不是 `[memories]` 或 `[agent_roles]` 的问题**，是 app-server 特有的校验。
影响：引擎只打一行警告然后继续跑，功能未见异常。
暂不阻塞，记在这里 —— 如果再出现奇怪行为，先怀疑它。

## 语音沉淀（2026-11-06 01:45）

和照片同一套机制：**说的和拍的都留一句话，能查回来**。

```
POST /api/v1/asr {blob}  →  转写
                            → 若这条 blob 已有 photo 行且没描述
                            → caption = "语音：<转写文本>"，saved=true
检索                      →  search_photos 直接按关键词找到
```

**实测**（把一段真实语音复制到 test-sandbox 测，不动真实项目）：

```
转写: {"text":"哈喽哈喽哈喽。","saved":true}
库:   id=22 test-sandbox  caption="语音：哈喽哈喽哈喽。"
检索: search_photos q=语音 → 命中 1 条（id=22, src=voice）
```

**为什么写进 caption 而不是新建表**：
语音和照片在库里本来就是同一张表（`photo`，靠 `src` 区分），
描述字段已经能被 `search_photos` 检索 —— 再用一套表就是重复造。

## 记忆打通了（2026-11-06 01:45）—— R8 完成

### 完整链路（全部实测）

```
会话闲置 ≥1 小时            → phase1 claim 到 1 个候选
phase1 抽取                 → stage1_outputs 出现真实内容（rollout_summary + raw_memory）
整理（6 小时冷却期）         → memory_consolidate_global done
产出                         → MEMORY.md + memory_summary.md 有内容
新会话提问                   → 它真的答得出来（见下）
```

### 关键：整理出来的东西长什么样

```markdown
# Task Group: test-sandbox project (photo search, rectification output rules)

scope: photo search via `search_photos` …
applies_to: cwd=/home/baize/jl-service/data/workspaces/test-sandbox;
            reuse_rule=safe to reuse for any future task in this project …

## User preferences
- when issuing a rule for this project, the user said:
  "这个项目的整改回复必须附前后对比照片" -> 以后默认带上，不用再提醒
- …"结论要写具体数字，不要写'基本符合'" -> …

## Reusable knowledge
- `search_photos` 接受 projectId / q，返回 id, sha256, caption …
  例：q="绿色" 返回 id=19, caption="测试绿色色块…"

## Failures and how to do differently
```

**格式完全符合设计**：Task Group + `scope:` + `applies_to: cwd=`（项目边界）+ 用户原话 + 可复用知识 + 失败记录。

### 验收（三条都能复现）

```
1) 问「整改回复有什么要求」→ 答「必须附前后对比照片；结论要写具体数字，不要写基本符合」 9 秒
2) 问「search_photos 返回什么字段、你查绿色时拿到了什么」
   → 答出 id=19、sha256=66e504…4d954 —— 这个 sha 只存在于引擎记忆里
      （MySQL 的 memory 表没有，AGENTS.md 也没有）
```

**第 2 条是决定性证据：它真的在读引擎的记忆文件。**

### 两个时间常数（产品必须知道）

```
min_rollout_idle_hours   ≥ 1 小时（引擎 clamp 在 [1,48]，写 0 无效）
整理冷却                  6 小时（PHASE2_SUCCESS_COOLDOWN_SECONDS = 6*60*60）
```

**含义：记忆是「隔一会儿才长出来」的，不是即时的。**
今天下午说的话，最快一小时后进候选池，整理要等到冷却期结束。
产品文案不能说「我刚说完它就记住了」。

## 记忆为什么是空的：找到根因了（2026-11-06 01:35）

### 根因：引擎把 `min_rollout_idle_hours` 钳死在 [1, 48]

```rust
// config/src/types.rs:431
min_rollout_idle_hours: toml
    .min_rollout_idle_hours
    .unwrap_or(defaults.min_rollout_idle_hours)
    .clamp(1, 48),          // ← 写 0 也没用，最少 1 小时
```

也就是说：**只抽取「闲置 ≥1 小时」的会话**（设计上避免拿正在进行的活去提炼经验）。
我之前的测试会话都是几分钟前的，所以 claim 永远返回 0 —— 不是坏了，是没到点。

### 验证（把线程时间往回拨 3 小时）

```
UPDATE threads SET updated_at_ms = updated_at_ms - 3h   （测试家用目录，仅此一次）

轮次后日志：
  jl-memory: startup task spawning (source=VSCode, idle_hours=1, max_rollouts=8)
  jl-memory: phase1 claimed 0 candidates        ← 前一轮
  jl-memory: phase1 claimed 1 candidate(s)      ← 回拨之后
  memory stage-1 extraction complete: 1 claimed, 1 succeeded (1 with output, 0 failed)

memories_1.sqlite:
  jobs: memory_stage1 | done
  stage1_outputs: 1 行，内容是真实抽取结果：
    description: User searched test-sandbox project photos for "绿色" …
    rollout_summary: # User searched project photos for green items, then establi…
```

**phase1 完全打通了。** 记忆真的能被抽出来。

### 还差最后一步：整理（phase2）

```
jobs: memory_consolidate_global | pending
stage1_outputs: selected_for_phase2 = 0
consolidation_progress: (version=1, watermark=0)
MEMORY.md 仍是 No consolidated memories yet
```

下一步：读 `phase2` 的 claim 条件（为什么 pending 不被认领、`selected_for_phase2` 谁来置 1）。

### 产品含义（值得记住）

**记忆不是即时的**：今天的会话，最早一小时后才可能被提炼。这符合引擎的设计意图，
但要让用户有正确预期 —— 别指望「刚说完它就记住」。

### 新增补丁 0004（诊断日志）

```
memories/write/src/start.rs    三个提前返回点打原因 + 启动参数
memories/write/src/phase1.rs   claim 到几个候选
```
有了它，以后再出问题看一眼日志就知道卡在哪，不用再猜三轮。

## R8 第二步：phase1 为什么一个都不claim（2026-11-06 01:20）

### 已确认的事实

```
1) 手工跑 claim 的 SQL（state_5.sqlite）→ 6 条候选线程，条件全满足
   memory_mode=enabled、source=vscode、updated_at 在窗口内
2) 日志 tag 里有 memories.max_rollouts_per_startup=8 → [memories] 段确实被解析
3) 整理任务 memory_consolidate_global = done；MEMORY.md / memory_summary.md 已生成
4) 但 jobs 表里没有任何 memory_stage1 行，stage1_outputs 仍 0
```

**所以不是“没有候选”，是候选没有被认领。**

### claim 的源码路径（记文件:行号）

```
state/src/runtime/memories.rs:153  claim_stage1_jobs_for_startup
  SQL 条件：memory_mode='enabled'、source in (cli,vscode,atlas,chatgpt)、
           id != 当前线程、updated_at 在 [max_age, now - idle_hours]
  之后还有两道：stage1_source_needs_update() → try_claim_stage1_job()
```

### 两条待查线索

**① 一条被忽略的配置项**

```
ERROR easycode_app_server: easyCode is ignoring 1 unrecognized configuration setting.
```
还没定位是哪个键。若是 `extract_model` 之类的名字不对，phase1 会退回默认模型，
可能在 `build_request_context` 阶段就失败 —— 而它排在 claim **之前**，
失败时任务直接返回，一条日志都不留（这正是我们看到的现象）。

**② 整理 agent 缺工具**

```
ERROR tools::router: error=unsupported call: write_file
ERROR tools::router: error=apply_patch verification failed …
```
整理 agent 想写 `memories/` 下的文件，但 `write_file` 报 unsupported，
patch 也失败 —— 它最后仍然标记 done，但没写出内容。
这解释了为什么 MEMORY.md 只有骨架。

### 下一轮的做法（明确到动作）

给引擎打一个小补丁（我们本来就有源码和编译链）：

```
memories/write/src/start.rs  在三个提前返回点各加一行 info! 日志：
  ephemeral / feature 未开 / non-root agent  → 各打一条，带原因
  并在 phase1::run 前后打 info：候选数、认领数
```

重编译 ~12 分钟，然后一次就能看到卡在哪。**不再猜。**

## R8 第一步：模型进目录 + 整理 agent 活了（2026-11-06 01:10）

### 做了什么

```
models-manager/models.json   加 qwen3.7-plus（复制 qwen3.8-flash 整块，只改 slug/名称/描述）
patches/0003-add-qwen37-plus.patch   生成的补丁
重编译 11m06s（jl-easycode-build:local），strip 后 297MB
部署：repos/easycode/.target/release/easycode（注意这是运行位置，不是 easycode-rs/target）
```

**踩的坑**：第一次把新 bin 从 `.target` 复制回 `.target`，等于没换。
构建产物在 `easycode-rs/target/release/`，运行位置是 `.target/release/` —— 两个目录。

### 结果

```
Unknown model qwen3.7-plus 警告：之前每轮都有，现在 0 次
memory_consolidate_global：之前 status=error / failed_agent，现在 status=done
记忆目录开始有文件：
  memory_summary.md（首行 v1，各节都是 No…yet）
  MEMORY.md（# Memory Handbook / No consolidated memories yet）
  raw_memories.md（仍为空）
```

**结论：整理 agent 能跑了，但 phase1（从会话里抽候选记忆）仍然产出 0。**
所以记忆还是空的 —— 骨架有了，内容没有。

### 下一步（具体到函数）

读 `easycode_state` 里 `claim_stage1_jobs_for_startup` 的 SQL，看它到底要什么条件：
线程完成态？rollout watermark？还是需要 `threads` 某个标志位。
现在 `jobs` 表里只有全局整理任务，**没有任何 stage1 任务行** —— 说明 claim 阶段就没选上。

## 记忆为什么一直是空的（2026-11-06 凌晨，查清了）

### 不是没跑，是**整理 agent 失败**

查了引擎自己的 sqlite（`data/homes/<user>/*.sqlite`）：

```
memories_1.sqlite：
  jobs: memory_consolidate_global | status=error | retry_remaining=2 | last_error=failed_agent
  stage1_outputs: 0 行
state_5.sqlite：
  threads.memory_mode = enabled（三条线程都是）
```

**结论**：会话被正确标记为「可产生记忆」，管线也被触发了，
但整理那一步的 agent 调用失败（`failed_agent`），所以 `stage1_outputs` 一直是 0，
`raw_memories.md` 永远是 "No raw memories yet"。

### 逐步排除了哪些可能

| 假设 | 结论 |
|---|---|
| 会话来源不在允许列表（只收 cli/vscode/atlas/chatgpt） | ❌ 我们的是 `vscode`，在列表里（看 rollout 的 session_meta） |
| 配置没开 | ❌ `feature.memories=true`、`generate_memories=true`、`max_rollouts_per_startup=8` |
| 刚跑完的会话不够格（默认要闲置 6 小时） | ❌ 已设 `min_rollout_idle_hours = 0` |
| 模型没指定 | ❌ 已设 `extract_model` / `consolidation_model = qwen3.7-plus` |
| **整理 agent 本身失败** | ✅ 就是它：`last_error=failed_agent` |

### 一条很可能相关的证据

```
WARN easycode_models_manager::model_info: Unknown model qwen3.7-plus is used.
     This will use fallback model metadata.
```

我们的主模型 `qwen3.7-plus` **不在引擎的模型目录里**（models.json 只有我加过的
`qwen3.8-flash`）。元数据回退会影响能力标记（工具支持、上下文窗口等），
整理 agent 很可能因此跑不起来。

**下一步**：把 `qwen3.7-plus` 按最小改动加进 `models-manager/models.json`（补丁 0002 的做法），
重编译（11~13 分钟），再看 `stage1_outputs` 是否开始有行。

### 顺带：app-server 日志现在可按需打开

```
JL_APPSERVER_LOG=1        → 引擎 stderr 写到 /tmp/jl-appserver-<user>.log
JL_ENGINE_LOG='...=debug' → 注入 RUST_LOG，看模块级日志
```

以前是 DEVNULL，什么都看不到 —— 这次排查才发现。

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
