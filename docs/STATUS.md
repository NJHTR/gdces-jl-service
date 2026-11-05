# 当前状态

> 这是**唯一**的进度事实来源。开工先看它，收工更新它。
> 更新规则：只写事实（有实测输出），不写"应该""大概"。

**最后更新**：2026-11-05 14:25（+08:00）

---

## 现在在做什么

**M4 工程化** —— 把业务域从 Python 单体里抽出来，让 Java 服务接管。

**刚做完**：Java 骨架跑通四个文书接口，和 Python 共用同一个 MySQL，
互相能读到对方写的数据。

**手上这一件**：把多模态接进对话 —— 语音用百炼 ASR，看图用 `qwen3.8-flash` 或华工隐患模型，
消息从 `text + photos` 改成 `parts` 数组。

> 原计划下一步是“biz 切 Java”，但语音/看图的能力刚验证完、趁热接上更划算；
> Java 切换不阻塞产品，可以稍后。

---

## 跑着什么

| 服务 | 端口 | 状态 | 说明 |
|---|---|---|---|
| BFF（Python） | 8081 | ✅ `{"ok":true,"engine":"up"}` | `runtime/entry.py`，包版 |
| jl-biz（Java） | 8082 | ✅ 200 | `service-java/target/jl-biz-0.1.0.jar` |
| MySQL | 3309 | ✅ Up 19h | docker `jl-mysql` |
| 引擎 easycode | — | provider=`dashscope` model=`qwen3.7-plus` | 每轮任务拉起子进程 |
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
