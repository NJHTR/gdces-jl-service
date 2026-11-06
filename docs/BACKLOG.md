# 待办清单（唯一列表 · 2026-11-06 15:35 更新）

> 规则：这份文件是**唯一**的待办来源。口头讨论出结论后，必须并到这里，否则视为没定。
> 状态：`[x]` 已完成 · `[ ]` 待做 · `[~]` 进行中 · `[?]` 待你拍板

---

## 已完成（不再列细项）

```
[x] 调度器复活（claim_due 用 rowcount）
[x] 权威导出不再误删（keep = written ∪ skipped，有错不删）
[x] 签发幂等、register 用 lastrowid、依据字段由 bases 派生
[x] 下线 POST /docs、删 117KB 遗留单体、deploy 加删除清单
[x] 命令字段改驼峰（输出/退出码/失败标记恢复）
[x] 上传/项目保存/记忆改删 补守卫；handle_chat 用户取 token
[x] 天气接 Open-Meteo 真实数据（取不到就空，不编）
[x] 拍照页：取景实时水印、定位五级兜底、地图选点、信息可编辑、来源落库
[x] 引用：引擎 memoryCitation + 工具账本合并、角标、底部出处、账本 L1-L4
[x] 巡视记录服务端生成 docx + 小程序下载/打开（当前是手绘表，待 Wave 2 换模板）
[x] Wave1-A1 文书归属任务（doc.task_id + 轮次标记）
[x] Wave1-A2 记忆单一写者（rules.md 由表渲染；learn_from_turn 默认关；完全重复只 hits+1）
[x] Wave1-A3 小程序「写一条记忆」接真接口
[x] Wave1-A4 图片消息不再标成「语音」
```

---

## Wave 0 · 收口（先补漏，半天）

> 2026-11-06 15:40 审计“起头又放下 / 已过时 / 信息悬空”的结果。前两项已当场修掉。

```
[x] G1 项目规矩移出 AGENTS 后没指路 → 每轮上下文补 records/rules.md 指针（已修）
[x] G2 任务 386/387 的 doc_id 指向已删的 264/265 → 回填 267/268，artifact 恢复（已修）
[ ] G3 消息 756/758 正文仍写着 docId 264/265（历史记录，改不改要拍板）
[ ] G4 110/198 个文书文件是孤儿（磁盘有、doc 表无）—— 补登记还是清理
[ ] G5 近义规矩 6 条变体（整改回复附对比图）+ 2 条（依据留空）未归并
[ ] G6 照片 sha-only 分支传空 pid：index_photo 返回 None，但响应仍说 indexed
[ ] G7 handle_asr 没有 guard_project（能按 sha 跨项目转写）
[ ] G8 noSource 与 cites 打架：有引用也可能被标「未溯源」
[ ] G9 ENGINE_MODE 三处判断不一致（1201 / 1395 / 1440）—— 默认 cli 时复核静默关闭
[ ] G10 scripts/make_patrol_docx.py 已过时（服务端可生成），留着会成为第二套渲染器
[ ] G11 inspect_docx.py 与 dump_docx.py 功能重复，留一个
[ ] G12 kb/templates/16巡视记录.md 仍是压平版，且无 docx 原件（Wave 2 B1 解决）
[ ] G13 旧文档口径（“文件是唯一真相”）与 D-记忆-01（表是账本、文件是视图）冲突，要改注
```

---

## Wave 2 · 表式文书（下一步，2~3 天）

```
[ ] B1 模板原件入库
      放 data/knowledge/templates/docx/（省统表 5 张 + 危大巡视表）
      每张配 <表名>.fields.json（标签定位，声明式，不写死行列号）
[ ] B2 formfill.py：load → 按标签定位 → 只改 run.text → 校验 → save
[ ] B3 /api/v1/reports/patrol 改为模板填充（替换现在的手绘表）
[ ] B4 多份：一条记录一张表 + 分页 + 连续编号
[ ] B5 smoke 断言：表格数/行列/合并不变、固定标签齐全、只改白名单单元格
[ ] B6 其余表式接入（旁站/平行检查/监理日志/通知单/检查记录）
```

验收：生成物与 GD-B-214 原件「同表数、同行列、同合并」；多天多张。

---

## Wave 3 · 记忆变“懂我”（C+ 模式，2~3 天）

设计已定（`[x] 决策`），实现待做：

```
[x] 决策：不选“纯人写”（负担），不选“全自动写项目规矩”（失控）
[x] 决策：个人层自动积累；项目层自动提案 + 一键确认（可配超时生效）
[ ] C0 记忆条目元数据：source（对话id/纠正/手动）、hits、status、applies_to
[ ] C1 个人×项目记忆：homes/<uid>/memories/projects/<pid>.md（同一记忆根，不新建第三处）
[ ] C2 项目规矩提案：从“人改字段/否定结论/重复要求”捕捉 → 观察队列 → 确认写库
[ ] C3 memory:// 引用：回答里标「依据你上次说的…」+ 命中时 hits+1
[ ] C4 「我的记忆」页补：来源、使用次数、撤销、删除（现在只有真写）
[ ] C5 待确认列表（与 observation 同一套 UI）
[ ] C6 指标可查：每人每周主动写记忆次数、记忆复用次数、撤销率、确认率
[ ] C7 近义归并：现有 5 条「整改回复附对比图」变体，人工或整理员合并（不自动删）
[ ] C8 验证引擎整理真的在工作（phase1 闲置≥1h / phase2 6h）——从没验证过
```

---

## Wave 4 · 多模态沉淀（1~2 天）

```
[ ] D1 图片/语音的“描述 + 指针”接入检索（records/photos.md 已有，缺按部位检索验证）
[ ] D2 真机验证：camera+cover-view 层级、chooseLocation、取景与出图一致
```

---

## Wave 5 · 清理与防回归（1 天）

```
[ ] E1 删死代码：run_cli_turn / _parse_jsonl / parse_multipart / relevance / get_or_create_thread
[ ] E2 删死端点：/members、/render-memory
[ ] E3 core.py 7 个重复常量、STD_RE、clean_reply 残句
[ ] E4 tools/workspace.js readMemories 改读 records/rules.md
[ ] E5 test-sandbox 补 .gitignore
[ ] E6 preflight 加：模板签名、记忆唯一写者、data/exports 存在
```

---

## Wave 6 · 运维与合规（部分待你操作）

```
[~] F1 微信后台：隐私保护指引、downloadFile 合法域名、HTTPS —— **暂缓**（要钱要时间，先开发）
[ ] F2 备份加 data/exports（现在只备 workspaces/blobs/memories/knowledge）
[ ] F3 137 份「待补」文书清理 —— 需你定规则
[ ] F4 复议机制（一次、有界）—— 后置
[ ] F5 离线拍照队列 —— 后置
```

---

## 顺序（一次只推进一个 Wave）

```
Wave 1 ✅ 已完成（A1~A4）
Wave 0 ⏭ 收口（G1~G13，半天，先补漏）
Wave 2   表式（B1~B6）
Wave 3   记忆 C+（C0~C8）
Wave 4   多模态（D1~D2）
Wave 5   清理（E1~E6）
Wave 6   合规/保险（F1 暂缓，F2 可随时做）
```

## 决策记录（避免以后再争）

```
D-记忆-01  记忆只有单一写者：项目规矩 = memory 表（rules.md 是渲染视图）；
           个人记忆 = 引擎整理（homes/<uid>/memories）
D-记忆-02  不做“纯人写”（负担）；不做“模型自动写项目规矩”（失控）；
           个人层自动、项目层提案+确认
D-表式-01  表式几乎不变 → 原件模板原位填字 + 结构签名校验；不手绘、不让模型画
D-部署-01  删文件必须写进 scripts/deploy-clean.txt（scp 不删远端）
D-环境-01  服务器 python-docx 用 pip --user 安装；重装系统要补（写进 DEPLOY 文档）
```
