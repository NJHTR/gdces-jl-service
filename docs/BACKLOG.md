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
[x] G3 消息 756/758 的 docId 已改 267·重建 / 268·重建（库里已核对）
[x] G4 110 份孤儿文书归档到 records/archive/2026-11-06/ + records/orphans.md（git 留痕）
[x] G5 规矩归并：12 条 → 6 条（6 条「整改回复附对比图」合成 1 条，2 条「依据留空」合成 1 条）
[x] G6 caption sha-only 补 project_id；索引没写成回 indexed=null
[x] G7 handle_asr 补 guard_project（projectId 或按 sha 反查）
[x] G8 noSource 遇 cites 非空不再报「未溯源」
[x] G9 ENGINE_MODE 默认 appserver；复核开关去掉 mode 依赖
[x] G10 删 scripts/make_patrol_docx.py
[x] G11 删 scripts/inspect_docx.py（保留 dump_docx.py）
[x] G12 kb 里两份巡视记录 md 已加注「表结构以 docx 原件为准」
[x] G13 ARCHITECTURE-LEARNING 加口径更新
```

---

## 当前指针（每轮开发从这里开始）

```
next = Wave2 / B5  smoke 断言（模板结构签名 + 固定标签 + 白名单单元格）
```

## 本轮踩的坑（记下来，避免重犯）

```
· lxml 元素不能用 id() 判"是否同一个格子"：代理对象回收后地址复用，
  两个不同格子被判成同一个 → 报「标签右边没有可填的单元格」。
  正确做法：`el is not anchor_el`（两个引用都活着）。
· report.py 用了 io.open 却没 import io → 线上 500 NameError。
  这类"改完没跑过"的问题，靠 deploy 的 smoke 抓不住（报告接口没被 smoke 覆盖）→ B5 要补。
```

## 开发 loop 协议（每轮照做，不许跳步）

```
1. 读本文件「当前指针」，只做那一项（不顺手改别的）
2. 实现 → 本地 preflight → deploy.ps1（含 smoke）
3. 失败先修到绿，再继续；修不好就停下来写清楚卡在哪
4. 把证据（实测输出/数字）写回本文件对应项，状态改 [x]
5. 更新「当前指针」到下一项，commit（一项一提交）
6. 需要人拍板的（策略/取舍）→ 停下，写进「待拍板」一节，不自己决定
```

## 待拍板

```
（暂无）
```

---

## Wave 2 · 表式文书（下一步，2~3 天）

```
[x] B1 模板原件入库（6 张：旁站/平行检查/巡视/通知单/监理日志×2）+ README
      16巡视记录.fields.json：标签定位（header/parts/org/period/issues）+ fixed_labels
[x] B2 bffpkg/formfill.py：load → 标签定位 → 只改 run.text → 结构签名校验 → save
      多份：复制模板一页元素 + 分页；空单元格回落 宋体 10pt
      实测：本地 2 条记录 → 2 张 4×4 表、合并一致、标签齐全
[x] B3 /reports/patrol 改为模板填充（实测 2 张 4×4、部位/时间/问题/签名/日期都在）
      质量修正：部位去重限 8、时间只取 07:00–19:00、问题限 10 行
[x] B4 多份：一记录一张表 + 分页 + 连续编号（GD-B-214 0001/0002，已实测）
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
