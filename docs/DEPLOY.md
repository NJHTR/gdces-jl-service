# 部署：唯一入口

本地是**唯一源码**（有 git），服务器只负责**运行**。
本文件说明为什么要有 `scripts/deploy.ps1`，以及它守的规矩。

---

## 三个位置，各管什么

| 位置 | 是什么 | 谁改 | 有 git 吗 |
|---|---|---|---|
**本地** `C:\Users\NJHTR\IdeaProjects\jl-service` | 源码仓库 | 只有它改代码 | ✅ 202+ 提交 |
**服务器** `baize-R640:/home/baize/jl-service` | 唯一在跑的服务 | 只接收部署；**不许手改代码** | ❌ 不是仓库 |
**小程序** `C:\Users\NJHTR\WeChatProjects\jl` | 客户端 | 只有它改客户端 | ✅ 本地仓库 |

服务器上跑着的：`python3 runtime/entry.py`（BFF，0.0.0.0:8081）、
MySQL（127.0.0.1:3309）、两个 `easycode app-server`、五条 cron。

**本机不跑服务。** 本机 8081 没有监听；小程序 `API_BASE` 指向 `127.0.0.1:8081`，
那是从本机到服务器的 SSH 隧道：

```
ssh -N -L 8081:127.0.0.1:8081 rokid-r640
```

---

## 三条铁律

1. **代码只从本地流向服务器**，方向永不可逆。
   服务器上出现的任何手改，下一次部署会被覆盖 —— 所以别手改。
2. **部署只有一个入口**：`scripts/deploy.ps1`。
   它会记录「部署了哪个提交」，并把 preflight + smoke 跑给你看。
3. **数据在服务器上，不在本地**。
   `data/`（工作区、影像、记忆、知识）只存在于服务器；本地仓库里的 `data/knowledge`
   是**知识源**（会同步到服务器），不是运行数据。

---

## 一次部署做什么

```
1. 检查本地工作区干净（有未提交改动就拒绝 —— 免得部署了"不在 git 里的代码"）
2. 记下本地 HEAD
3. 传 bff/ + scripts/ + data/knowledge/ 到服务器
4. 服务器写 runtime/DEPLOYED（提交号 + 时间）
5. 重启 BFF（kill entry.py → start-bff.sh）
6. 跑 preflight + 健康检查
7. 打印：部署了什么、服务是否健康
```

日常巡检（每天 09:00 devloop）会打印 `runtime/DEPLOYED`，
所以你随时能知道「线上跑的是哪个提交」。

---

## 上线还要补的（现在不阻塞开发）

小程序现在靠 SSH 隧道连服务器，这是**只在开发者电脑上成立**的方式。
真机在工地用时，必须：

```
域名 + HTTPS（例：https://api.gdces.cn）
  → 微信公众平台「request 合法域名」加白名单
  → utils/config.js 的 API_BASE 改成该域名
  → 服务器前面加 nginx（TLS 终结）或直接给 BFF 上证书
```

在改成域名之前，监理员只能在连着开发者电脑的网络里用。
