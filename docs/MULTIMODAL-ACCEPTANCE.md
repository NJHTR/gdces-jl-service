# 多模态输出 MVP 验收文档（MULTIMODAL-ACCEPTANCE）

> 范围：模型输出 `answer.blocks`（text / image_ref / file_ref / card），独立 asset 资产服务，
> 真实照片标注与整改前后对比拼版，Agent 工具链路；**不改 L0-L4 记忆/知识架构**。
> 线上：`rokid-r640:/home/baize/jl-service`，BFF `0.0.0.0:8081`。

## 0. 一句话结论

- **能**：模型通过工具引用真实照片、生成派生标注/对比图、复用文书 file/card，前端统一渲染资产卡。
- **不能**：生成现场照片、伪造证据、把资产写进 `memory` / `knowledge` / `photo` / `records` / `docs`。
- **读路径**：模型只输出 block 引用，不输出字节、base64、本地路径或外链。

## 1. 分层与隔离

```text
L0 knowledge/ + standard     只读输入，资产永不写入
L1 records/rules.md          只读输入，资产永不写入
L2 records/photos、docs      只读输入，资产永不写入；photo 行数不变
L3/L4 memory                 只读输入，资产永不写入
asset                        data/assets/<pid>/<assetId>，独立分仓
```

- 资产 `origin`：`uploaded`（真实照片引用）/ `derived`（标注、拼版）/ `generated`（示意）；
- asset 的 `user_id` 是 creator/audit，可见性按**项目域共享**（同项目成员可读，非成员 403）；
- 生成/派生资产不参与 Phase 1/2 记忆抽取，不写 `citation` / `standard`。

## 2. answer.blocks

位置：`message.answer.blocks`；`GET /api/v1/tasks/<id>` 顶层 `data.blocks` 同步。

```json
{"type":"text","text":"..."}
{"type":"image_ref","assetId":"...","caption":"...","origin":"derived","sourceRefs":["photo:45"],"url":"/api/v1/assets/.../content?e=&s="}
{"type":"file_ref","assetId":"...","name":"...","mime":"application/pdf","downloadUrl":"...?dl=1"}
{"type":"card","docId":499,"title":"...","url":"/api/v1/docs/499","actions":[{"type":"readonly"}]}
```

- 旧客户端不 opt-in `capabilities:["answer.blocks"]` 时 `blocks=[]`，行为完全不变；
- 未知 type/assetId、他项目资产、base64/本地路径/外链一律丢弃，保留 `answer.text`；
- 单轮上限：4 图 / 1 音（预留）/ 2 文件 / 3 卡；
- 历史/bootstrap 读取时服务端重签短时 URL。

## 3. 资产服务

- 表：`asset`（asset_id、project_id、user_id、task/message、kind、mime、size、sha256、origin、producer、model、prompt_hash、source_refs、status、expires_at）；
- 存储：`data/assets/<pid>/<assetId>`，**不写** `data/blobs`、`records/photos`、`docs`；
- 访问：`GET /api/v1/assets/<id>/content?e=&s=` 短签 URL，`<image>` 可直接加载，无需 Bearer；
- 安全：mime allowlist、路径穿越拒绝、EXIF/GPS 去除、禁止 svg/html/js、staged 24h / 生成物 90d、软删除、audit；
- 文件：`downloadUrl`（`dl=1`）供 `wx.downloadFile`；`showMenu` 仅证据态放开。

## 4. 真实照片派生

- `POST /api/v1/photos/annotate`：真实 photoId + boxes → 画框/编号/标签 +「派生图·基于现场照片」角标；
- `POST /api/v1/photos/compare`：两张真实照片 → 整改前后拼版 + 来源水印；
- 只读 photo/blob；产物 origin=derived、committed、sourceRefs 正确；同图对比 400；他项目照片 404；
- **不生成无源图片，不写 photo/records/docs/memory/knowledge。**

## 5. Agent 工具链路

```text
用户：圈出照片45里的配重块
  → search_photos 拿真实 photoId
  → jl-tool annotate_photo '{"projectId":...}'  (argv JSON)
  → /api/v1/photos/annotate
  → 写 <ws>/.agent/blocks.json
  → BFF _turn_blocks → message.answer.blocks
  → 前端资产卡（派生）
```

- 提示词/工具手册/工具描述禁止 shell/python/Pillow 自绘、禁止 /tmp、禁止本地路径；
- BFF `strip_local_paths` 清洗 `/tmp`、`/home`、`C:\`、`file://` 与“文件已保存在…”；
- 正文声称有图但 blocks 为空时，补“本次未生成可展示图片”提示；
- 工具失败返回用法提示，不伪造 block。

## 6. 验收记录

| 项 | 结果 |
|---|---|
| `tools/test-assets.py` | 99/99 |
| `tools/test-photo-craft.py` | 49/49 |
| `tools/test-agent-tools.py` | 62/62 |
| 真实 chat + capabilities（标注照片45） | 3/3 成功出 image_ref，短签 200 PNG |
| QA 多模态隔离（task-11） | 102 PASS / 0 FAIL，资产零证据/记忆/知识写入 |
| QA Agent 工具（task-13） | 45 PASS；真实 chat 曾绕过工具，task-14 修复后 3/3 |
| 记忆/知识回归 | Phase1 14+30、Phase2 24、权限 17+30 全过；rules/AGENTS 哈希不变 |
| 客户端 | blocks 单测 30/30、node/WXML 校验、preview 构建通过 |

## 7. 提交

后端：

```text
110fb4e  answer.blocks + asset 服务
b5041a5  真实照片标注/对比拼版
5f98d49  Agent 工具 annotate/compare → blocks.json
6874a3f  堵 shell 自绘 + 本地路径清洗
```

小程序：

```text
5b5ee5f  answer.blocks 资产卡渲染（证据/派生/示意）
71dd1e4  资产文件下载使用当前 session token
```

## 8. 已知限制

1. 资产可见性为**项目域共享**，`user_id` 仅 creator/audit；
2. 真实模型行为有随机性；若偶发绕过，BFF 兜底清洗路径并提示无图；
3. `tool_manual` 全量历史未注入 appserver 引擎，当前靠提示守卫/工具描述/清洗；
4. `audio_ref` 仍预留未做；MCP 镜像未重建（shell 链路不受影响）；
5. 未做真机视觉点验，仅预览包构建；
6. 短签 URL 10 分钟过期，历史/bootstrap 会重签；页面停留过久点旧图可能占位/失败。

## 9. 回滚

- 代码：`git revert 6874a3f 5f98d49 b5041a5 110fb4e`（按需）；
- 数据：asset 表/资产目录可清空；不影响 photo/doc/memory/knowledge；
- 客户端：回退 `5b5ee5f`/`71dd1e4`，旧客户端不 opt-in blocks 即可完全回到纯文本；
- 服务：`deploy.ps1` 或线上重启 + health/preflight。
