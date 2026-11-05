# 当前状态

> 这是**唯一**的进度事实来源。开工先看它，收工更新它。
> 更新规则：只写事实（有实测输出），不写"应该""大概"。

**最后更新**：2026-11-05 13:40（+08:00）

---

## 现在在做什么

**M4 工程化** —— 把业务域从 Python 单体里抽出来，让 Java 服务接管。

**刚做完**：Java 骨架跑通四个文书接口，和 Python 共用同一个 MySQL，
互相能读到对方写的数据。

**手上这一件**：Python 的 biz 调用切到 Java（加 `BIZ_BASE_URL` 开关，可灰度回滚）。

---

## 跑着什么

| 服务 | 端口 | 状态 | 说明 |
|---|---|---|---|
| BFF（Python） | 8081 | ✅ `{"ok":true,"engine":"up"}` | `runtime/entry.py`，包版 |
| jl-biz（Java） | 8082 | ✅ 200 | `service-java/target/jl-biz-0.1.0.jar` |
| MySQL | 3309 | ✅ Up 19h | docker `jl-mysql` |
| 引擎 easycode | — | 编译产物在 `repos/easycode/.target/release/` | 每轮任务拉起子进程 |

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
项目 2 | 文书 41（待补 0） | 消息 157 | 任务 92 | 记忆 14 | 审计 7
工作区 yun-gu-3qi：59 次提交
```

> 待补 0 是因为最近几轮测试把字段都填了，不是功能没生效。

---

## 最近三轮做完的（倒序）

1. **Java 骨架**（`f6a5d72`）四个文书接口跑通，与 Python 数据互通
2. **单一写者**（`4fd4f54`）工具改调 `POST /docs/register`，doc 表只剩一个写者
3. **分包 + 契约**（`b195163` / `e34b7a9`）2900 行单文件拆成四层，30 条路径标了所有权

---

## 已知问题（按严重程度）

| # | 问题 | 影响 | 想怎么解 |
|---|---|---|---|
| 1 | 首次响应要 8 秒才出第一步 | 用户以为卡住 | 流式输出（M6），或先把 3 步压成 1 步 |
| 2 | 语音只能录不能转写 | 语音路径走不通 | 待定 ASR 路线 |
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
