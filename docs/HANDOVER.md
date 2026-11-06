# 交接说明（这一版能做什么、不能做什么）

> 生成时间：2026-11-07 00:40　线上代码提交：`7f770df`（后端）
> 小程序提交：`7b59493`　数据库：MySQL `jl` @ 127.0.0.1:3309

---

## 一、这套东西是什么

给监理员用的微信小程序 + 后端服务。核心不是"聊天"，而是**把现场的话/照片
变成能签字归档的监理文书**，并且格式、依据、留痕都能对上省统表。

```
小程序（WeChatProjects/jl）
   └── HTTPS/隧道 → BFF（/home/baize/jl-service/bff，8081）
                        ├── MySQL jl（文书、记忆、复核、任务）
                        ├── 工作区 data/workspaces/<项目>/（git 版本化）
                        ├── 引擎 easyCode app-server（按用户隔离 EASYCODE_HOME）
                        └── 工具容器 jl-tools（MCP：9 个工具，只读挂载源码）
```

---

## 二、现在确实能用的（都有实测）

| 能力 | 现状 | 证据 |
|---|---|---|
**现场拍照** | 取景即带水印（部位/时间/天气），定位五级兜底，信息可改 | 真机待验（D2） |
**断网拍照** | 失败存本地队列，回到页面自动补传 | node 打桩测试：入队 2 → 成功 1/失败 1，队列剩 1 |
**聊天出文书** | 一句话 → 监理通知单/旁站/日志等，带缺项与出处 | smoke 全绿，出稿 8~90s |
**省统表出表** | 巡视/旁站/平行检查/监理日志/通知单 五张，按原件模板填 | 结构断言：4×4、11×9、(1×1)+(8×2) 等 |
**下载打开** | 生成 docx → 小程序下载 → 打开 | 巡视记录 33KB / 通知单 37KB |
**记忆** | 项目规矩/个人×项目/个人跨项目；提案→确认；引用可见 | 980 用户 MEMORY.md 22KB 实测生成 |
**复核与复议** | 复核必须带 locator；must_fix 可复议一次，裁定落库 | 复核 102：申请→409→推翻→409 |
**证据索引** | 影像按部位分组；文书按日期归档 | /photos/index 返回 2 部位 7 张 |
**备份** | db/工作区/影像/记忆/知识/导出，保留 7 天，含恢复演练 | exports 包 172K，解出 13=13 文件 |

---

## 三、不能做 / 还没做的

| 项 | 状态 | 说明 |
|---|---|---|
真机验收 | 待你 | 前置要有 **https 域名 + 微信后台白名单 + 隐私指引**（见 CHECKLIST-DEVICE.md） |
137 份「待补」文书 | 待你定规则 | 补全/归档/删除三条路，删不可逆 |
14 张历史照片无描述 | 待你定 | 补描述要模型看图（耗 token） |
「监理检查记录」表 | 待你定模板 | 省统表里没有这张 |
微信后台配置 | 暂缓（F1） | 域名/隐私指引/白名单都是后台操作 |
多人并发同一项目 | 部分 | 同一用户串行；两个用户同时出稿仍可能抢同一批文件（已用 task_id 认领降低风险） |

---

## 四、几条不能破的规矩（改动前先读）

1. **一个关注点只有一个写者**
   - `doc` 表：只有 `handle_doc_register` 插入、`handle_doc_patch` 更新
   - 项目规矩：`memory` 表是账本，`records/rules.md` 是渲染视图
   - 个人记忆：引擎整理（`homes/<uid>/memories`）；个人×项目：memory 表渲染
2. **表式只填原件**：`data/knowledge/templates/docx/*.docx` + `*.fields.json`；
   不许手绘表格、不许让模型输出 OOXML；生成后必须过结构签名校验。
3. **不编数据**：天气/实测值/签发人没有就留空或「待补」。
4. **不写假实现**：界面上的每个按钮都要真调接口（这轮修掉过两处"只改本地数组"）。
5. **部署只走** `scripts/deploy.ps1`：本地 → 服务器，带 preflight + smoke；
   删文件要登记到 `scripts/deploy-clean.txt`。

---

## 五、出问题先看哪

| 现象 | 先看 |
|---|---|
服务起不来 | `runtime/bff.log` 最近 Traceback；`scripts/preflight.py`（导入检查） |
出稿失败/很慢 | `runtime/bff.log` 的 `[turn]`；`/tmp/jl-appserver-<uid>.log` |
文书状态不对 | `doc.state`、`doc.fields`、schema（`schemas/*.json`） |
表式不对 | 先 `python3 -c "from bffpkg.formfill import load_spec..."`；再看模板是否被换 |
记忆不更新 | `scripts/check-engine-memory.py`；引擎日志里的 `Phase 1` / `apply_patch` |
小程序白屏 | `scripts/check-miniapp.py` |

---

## 六、常用命令

```bash
# 部署（本地）
.\scripts\deploy.ps1

# 自检
python3 scripts/preflight.py
python3 scripts/smoke-e2e.py
python3 scripts/check-miniapp.py
python3 scripts/check-engine-memory.py

# 备份 / 恢复演练
bash scripts/backup.sh
tar -tzf backups/exports-*.tar.gz | head

# 表式单测（本地）
python3 bff/bffpkg/formfill.py --form 巡视记录 --data d.json --out /tmp/x.docx
```
