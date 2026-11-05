# easyCode 能力清单（源码核实版）

> **怎么读这份文档**
>
> 每一项都标了**置信度**，因为我们踩过"文档说有、实际跑不通"的坑：
>
> - 🟢 **代码已核实**：读过实现，能指出文件和行号
> - 🟡 **符号已见**：看到类型/定义，但调用链没走完
> - 🔴 **未跑通**：还没在真机上验证
>
> **改这份文档的规矩**：只写你亲眼在代码里看到的。看到文档说"支持 X"不算，
> 要找到实现再写进来。跑通的补一句实测输出。
>
> 仓库位置：`/home/baize/jl-service/repos/easycode/easycode-rs`（Codex 的 fork）

---

## 一、多 Agent（决定整个架构的部分）

### 1.1 工具集 🟢

名字来自 `core/src/tools/handlers/multi_agents_spec.rs`：

| 工具 | 作用 | 关键参数 |
|---|---|---|
`spawn_agent` | 派一个子 agent | `agent_type`、`message` 或 `items`、`fork_context`、`fork_turns`、`model`、`reasoning_effort`、`task_name` |
`send_message` | 给已有 agent 发消息（不触发新 turn） | `target`、`message`、`interrupt` |
`wait_agent` | 等一个或多个 agent 结束 | `targets[]`、`timeout_ms` |
`interrupt_agent` | 打断当前 turn，agent 仍可继续用 | `target` |
`close_agent` | 关掉 agent | `target` |
`list_agents` | 列出所有 agent 和状态 | — |

**`items` 支持的类型**（`create_collab_input_items_schema`）：

```
text · image_url · audio_url · path（local_image / local_audio / skill / mention）
```

**这一条最重要**：子 agent 可以直接接收**图片和音频**，不需要我们先下载再转述。

### 1.2 限制与默认值 🟢

`core/src/config/mod.rs`：

```rust
DEFAULT_AGENT_MAX_DEPTH = 1;                       // 默认只能派一层
DEFAULT_AGENT_MAX_THREADS = Some(6);               // 一个会话最多 6 个 agent 线程
DEFAULT_MULTI_AGENT_V2_MAX_CONCURRENT_THREADS_PER_SESSION = 4;
DEFAULT_MULTI_AGENT_V2_MIN_WAIT_TIMEOUT_MS = 10_000;
DEFAULT_MULTI_AGENT_V2_MAX_WAIT_TIMEOUT_MS = 3_600_000;
DEFAULT_MULTI_AGENT_V2_DEFAULT_WAIT_TIMEOUT_MS = 30_000;
```

**含义**：默认是"一层多 agent"（总监派 6 个下属，下属不能再派）。
要做"层级 agent"（总监 → 组长 → 组员），必须把 `agent_max_depth` 调到 2 或 3。

配置键：`agent_max_depth`、`agent_max_threads`（`config/src/config_toml.rs` 里有字段）。

### 1.3 角色（agent_types）🟢

`agent-roles/src/agent_role_config.rs` + `core/src/agent/role.rs`：

```rust
pub struct AgentRoleConfig {
    pub role_name: String,
    pub description: Option<String>,
    pub nickname_candidates: Option<Vec<String>>,
    pub config_file: Option<PathBuf>,
    pub config: TomlValue,          // ← 任意配置项都能按角色覆盖
}
```

内置角色只有两个（`core/assets/agent/builtins/`）：

- `explorer.toml`（空文件，靠默认）
- `awaiter.toml` —— 一个"只等待、不改动"的角色，示范了怎么用 `developer_instructions`

**角色能覆盖什么**：`config` 是 `TomlValue`，会**合并进基础配置**，
所以模型、推理强度、超时、沙箱、各类开关都能按角色改。

在 `~/.easycode/config.toml` 里加：

```toml
[agent_roles.reviewer]
description = "监理文书复核员：只找问题，不改文件"
developer_instructions = """
你是监理文书复核员……
"""
```

> ⚠️ **注意**：我**没有**在源码里找到"角色级工具白名单"这个字段。
> 权限隔离要么靠配置项（`config` 里能设的开关）、要么靠沙箱、
> 要么靠提示词 —— 这一点需要实测确认，别假设它有白名单机制。

### 1.4 特性开关 🟢

`features/src/lib.rs` 里的键：

```
multi_agent          （= collab 的别名）
multi_agent_v2
multi_agent_v2_dynamic_tools
multi_agent_mode
```

配置写法（两种都支持）：

```toml
[features]
multi_agent = true

# 或
[features.multi_agent_v2]
enabled = true
```

> 🟡 **已知坑**：`[features]` 表必须写在**所有顶层键之后**。
> 我第一次插在文件开头，把 `model_provider` 等键吞进了 features 表，引擎直接不工作。

### 1.5 运行时实测（2026-11-05，R0）🟡

三种配置各跑了一次，结论是**协议决定成败**：

| 配置 | 模型发来的调用 | 结果 |
|---|---|---|
| `wire_api = "chat"` | `tool_name="spawn_agent" tool_namespace="functions"` | ❌ `unsupported call: spawn_agent` |
| `wire_api = "chat"` + `tool_namespace="collab"` | 仍然是 `functions` | ❌ 同样失败 |
| `wire_api = "responses"` | `tool_name="spawn_agent" tool_namespace="collaboration"` | ✅ 派发成功，子线程被创建 |

**根因 1（chat 协议）**：`DEFAULT_FUNCTION_NAMESPACE = "functions"`，
chat 协议下所有工具调用都落在 `functions` 命名空间；
而多 agent 工具注册在 `collaboration`（`DEFAULT_MULTI_AGENT_V2_TOOL_NAMESPACE`），
注册表按"命名空间 + 名字"精确匹配 → 查不到 → `unsupported call`。
`functions` 是保留命名空间，`tool_namespace` 改不进去
（见 `validate_multi_agent_v2_tool_namespace`，`protocol/src/tool_name.rs:7`）。

**根因 2（responses 协议）**：工具派发通了，子线程也真的启动了
（日志里出现 `op="inter_agent_communication"` 和新的 `thread_id`，
父 agent 收到来自 `/root/list_docs` 的 `FINAL_ANSWER` 消息），
但子 agent 的 turn 报错，父 agent 转述为 **`encrypted_content` 类型不兼容**。

即：easyCode 的多 agent 交接需要转发**加密推理内容**，
第三方 Responses 实现（阿里云百炼）不支持，子 agent 起不来。
把 `model_reasoning_summary` 改成 `none` 也没绕过。

**结论（已推翻，见下）**：当时以为是模型服务兼容性；实际是引擎没清 `AgentMessage` 里的加密内容块。
要跑通需要"完整实现 Responses（含 encrypted_content 透传）"的服务，
比如官方 OpenAI 或同类中转 —— 我们现在的中转额度为 0。

---

## 二、单 agent 的能力（工具层）

`core/src/tools/handlers/` 下每个文件基本对应一个工具。🟢

| 工具 | 用途 | 对我们意味着什么 |
|---|---|---|
`apply_patch` | 按 patch 改文件 | agent 自己能改文书 md，不用后端拼 |
`unified_exec` / `exec_command` | 执行命令 | 已经在用（`jl-tool`） |
`view_image` | 看图，`detail: high / original` | 照片理解，**不用自己写 VLM 调用** |
`update_plan` | 更新计划（`step` / `status`） | 多步任务显式化，前端可直接展示 |
`request_user_input` | 中途问用户 | 信息不足时**反问**，而不是编 |
`tool_search` | 动态发现工具 | 工具多时按需检索，省上下文 |
`new_context_window` / `get_context_remaining` | 上下文压缩与剩余量 | 长任务不爆窗口 |
`mcp` / `mcp_resource` | MCP 工具与资源 | 我们的 `jl-tool` 其实可以走 MCP |
`request_plugin_install` / `list_available_plugins_to_install` | 插件 | 暂不需要 |
`sleep` / `wait_for_environment` | 等待 | 轮询类任务的正确姿势（别用死循环） |

### 2.1 `view_image` 🟢

`view_image_spec.rs` 参数：`path` 或 `image_url`，`detail` ∈ `{high, original}`。

**结论**：看图能力框架自带。我们需要做的是把上传的照片落到工作区，
然后让 agent 自己 `view_image`。

### 2.2 `update_plan` 🟢

`plan_spec.rs`：

```
update_plan(plan: [{step, status: pending|in_progress|completed}], explanation)
```

**用途**：多 agent 任务里，总监可以显式维护计划，前端读出来就是进度条 ——
**不用我们再在后端拼 progress 数组**。

### 2.3 `request_user_input` 🟡

`protocol/src/user_input.rs` 有 `UserInput` 枚举，handler 在
`core/src/tools/handlers/request_user_input.rs`。

**没验证的**：BFF 怎么接管这个请求并把答案回传（需要走 app-server 的请求/响应，
而不是 `exec` 的一次性管道）。录音/拍照补充信息正好用得上。

---

## 三、事件与钩子

### 3.1 hooks 事件 🟢

`hooks/src/events/` 目录：

```
pre_tool_use.rs · post_tool_use.rs · user_prompt_submit.rs
session_start.rs · session_end.rs · stop.rs · compact.rs
interrupt.rs · model_request.rs · permission_request.rs
```

**这是替代"后端 if"的规则执行点**：

- `PreToolUse` —— 拦掉不该发生的写操作（比如写到项目目录外）
- `PostToolUse` —— 校验产物（文书缺依据就判失败）
- `Stop` —— 整轮结束时做最终检查

> 我们现在的"提交钩子校验依据行"就是同一思路，但那个是 git hook；
> 用 `PostToolUse` 可以在**工具刚写完文件时**就拦住，不必等到 commit。

### 3.2 遥测 🟡

`otel/` crate 存在，`ResponseEvent::ReasoningSummaryDelta` 等事件有定义。
🟡 尚未验证在此配置下是否真的产出（我们实测中转不给 reasoning，百炼给 `reasoning_content`）。

---

## 四、常驻会话（app-server）🟢 已实测可用


### 4.1 实测（2026-11-05）🟢

协议（`app-server generate-json-schema` 产出，不是猜的）：

```
initialize   { clientInfo, capabilities.experimentalApi }
thread/start { cwd（必须绝对路径）, model, approvalPolicy, sandbox }
turn/start   { threadId, input: [{type:"text",text}] | [{type:"localImage",path}] }
```

通知（实测抓到的）：

```
thread/started · turn/started · turn/completed
item/started · item/completed
item/reasoning/textDelta · item/agentMessage/delta
error · warning · mcpServer/startupStatus/updated
```

item 类型：`userMessage` · `reasoning` · `agentMessage` · `commandExecution` · `mcpToolCall`

**延迟对比**：简单一轮 2.6/2.3 秒；带工具一轮 4.8/4.1 秒（exec 是 8~14 秒）。

**三个坑**：

1. 子进程要继承 BFF 的 `ENGINE_ENV`，只继承 `os.environ` 会报缺 key
2. `cwd` 是相对路径时，`exec_command` 报
   `Failed to create unified exec process: No such file or directory`
3. `item/completed` 的 agentMessage 正文常为空，文本在 `item/agentMessage/delta`

`app-server-protocol` 里出现的 JSON-RPC 方法 🟢：

```
thread/start · thread/resume · thread/fork
turn/start  · turn/interrupt
model/list  · account/read · account/logout
```

**这是我们 P2 要的东西**：会话可以常驻、可以续、可以 fork。
现在的做法是**每轮 `exec` 起一个子进程**（启动约 3 秒），换成 app-server 就不必反复启动。

> 🔴 未验证：`thread/resume` 跨进程重启后是否还能续（我们之前在 `exec` 模式下
> 观察到 thread_id 是进程内的，重启就 `thread not found`）。
> 这个必须实测，直接影响"断线续跑"的设计。

---

## 五、其他 crate（只列，未深读）

从 `easycode-rs/` 目录看到的存在性（🟡 仅符号）：

| crate | 猜测用途 | 我们是否需要 |
|---|---|---|
`voice-host` | 音频采集/播放（`capture_worker`、`audio_sink`） | 可能，用于实时语音 |
`computer-use` | 桌面截屏/操作 | 不需要 |
`skills` | `SKILL.md` 技能（含隐式触发检测） | 需要：把监理作业指导写成技能 |
`memories` | 记忆（`write/src/runtime.rs` 里有 reasoning_summary 处理） | 需要：替代我们的三层记忆？ |
`agent-message-board-client` | agent 间消息板 | 可能，多 agent 通信 |
`agent-graph-store` | agent 关系图存储 | 可能，用于追溯 |
`guardian-context` | 审查/评分上下文（安全审查） | 可能，用于高风险文书二次审查 |
`collaboration-mode-templates` | 协作模式模板（Plan / Default） | 需要：Plan 模式 |
`worktree` | git worktree | 可能，多 agent 并行改文件时隔离 |

**这些都不确定，动手前要先读代码。**

---

## 六、我们已经在用的

| 能力 | 怎么用 | 状态 |
|---|---|---|
`exec --json` | 每轮任务一次子进程 | 🟢 生产在用 |
工具调用（`exec_command`） | agent 调 `jl-tool` | 🟢 已验证（qwen 也准） |
`code-mode`（V8 沙箱） | 命令在沙箱里跑 | 🟢 已构建并在用 |
MCP（`mcp_servers.jl`） | 配置了但路由不通 | 🔴 我们用 `jl-tool` 绕过 |
`--json` 事件流 | 解析 timeline / 引用 | 🟢 生产在用 |

---

## 七、下一步必须先验证的三件事

按依赖顺序：

1. **`spawn_agent` 真能派子 agent 吗**（🔴）
   —— 不通则整个"多 agent"路线作废，得退回单 agent + 更长的提示词。
2. **子 agent 能不能收到图片/音频**（🟡）
   —— 决定多模态是"给总监"还是"给专门的 Field agent"。
3. **app-server 的 `thread/resume` 跨重启有效吗**（🔴）
   —— 决定断线续跑怎么做，以及是否值得常驻。

验证方法：每项都用最小用例，把 `--json` 事件流里的证据贴进 `STATUS.md`。


### 1.8 已验证可用（2026-11-05，打了补丁之后）🟢

打补丁后实测：

```
父 agent 派一个 explorer 子 agent → 子 agent 跑完 → 父 agent 收到结果
encrypted_content 报错次数：0
```

**补丁要点**：`AgentMessage` 的 content 里可能带 `encrypted_content` 块，
`clear_encrypted_content()` 对这个变体是跳过的，所以非 OpenAI 服务端会 400。
补丁对非 OpenAI 只保留 `InputText`。

补丁文件：`patches/0001-non-openai-drop-encrypted-content.patch`
编译方法见 `STATUS.md` 的「R4 第一块」。

### 1.6 第二轮实测：换模型也没用（2026-11-05）

把上游 21 个模型逐个探了一遍（`scripts/probe_models.py`）：

```
模型                        HTTP  推理块  推token  判定
qwen3.7-max                 200   有      255     有推理
qwen3.7-plus                200   有      15      有推理
qwen3.6-flash               200   有      188     有推理
glm-5.2                     200   有      224     有推理
deepseek-v4-pro             200   有      17      有推理
qwen3.8-max                 200   有      35      有推理
deepseek-v4.1-flash         200   有      90      有推理
glm-5.3                     200   无      0       ✅ 唯一无推理
auto                        200   有      16      有推理
decision-model-preview      400   -       -       不可用
```

**拿唯一无推理的 `glm-5.3` 试 `spawn_agent`，仍然失败**：

```
Invalid content type: encrypted_content
```

原因：即便模型本身不产生推理，**引擎在 agent 场景下仍会请求并回放推理项**
（`core/src/client.rs` 里 `include` 无条件带 `reasoning.encrypted_content`，
且对非 OpenAI 服务端只清理其它项的加密内容，**Reasoning 项被豁免保留**）。

### 1.7 补丁已写好，但编译环境凑不齐

改法（两处，`core/src/client.rs`）：

```rust
// 1) 非 OpenAI 一律清掉加密内容（原来 Reasoning 项被豁免）
item.clear_encrypted_content();

// 2) 非 OpenAI 不再请求加密推理内容
let mut include = if is_openai {
    vec!["reasoning.encrypted_content".to_string()]
} else {
    Vec::new()
};
```

补丁已打到服务器源码上，但**编译不起来**：

| 障碍 | 现状 |
|---|---|
服务器没有 C 编译器 | 系统无 cc/gcc/clang，无 root 装不了 |
rust-toolchain 要 1.95.0 | 镜像上没有该版本（stable 是 1.99.1） |
改用 stable | 可以装，但 build script 全挂（缺 cc） |
用 zig 充当 cc | zig 下载 8MB 后卡死，镜像不可用 |

**结论**：多 agent 目前被"编译环境 + 服务端兼容性"双重卡住。
单 agent 走 responses 协议是通的，且能拿到 reasoning。
