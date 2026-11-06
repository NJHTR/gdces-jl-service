# 待办清单（唯一列表 · 2026-11-06 17:50 更新）

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
next = Wave5 / E5  工作区 .gitignore（test-sandbox 缺，防 .agent/ 进仓库）
```

## 本轮踩的坑（记下来，避免重犯）

```
· lxml 元素不能用 id() 判"是否同一个格子"：代理对象回收后地址复用，
  两个不同格子被判成同一个 → 报「标签右边没有可填的单元格」。
  正确做法：`el is not anchor_el`（两个引用都活着）。
· report.py 用了 io.open 却没 import io → 线上 500 NameError。
  B5 已把报告接口纳入 smoke（现在这类问题部署时就会红）。
· smoke 的 sql() 只执行不返回 → 清理读不到行；已改成 return stdout 并在失败时抛错。
· /docs/register 的返回键是 docId（不是 id）；测试脚本里写 id 会拿到 None → 400。
· API 路由顺序：`/api/v1/memories/recall` 必须排在 `startswith("/api/v1/memories")` 之前，
  否则会被列表接口截走（这次就踩了，返回「ownerId 必填」）。
· smoke 不能依赖"模型这次缺几项"：模型只缺 1 项时"补一半仍待补"会假失败。
  状态机改用 register 的受控文书（字段集合固定）。
```

## 开发 loop 协议 v2（每轮照做，不许跳步）

> 目标已写入会话（24 轮上限）。轮次用完就停下汇报，不自己加轮。

```
1. 读「当前指针」→ 只做这一项
2. 动手前核实现状（代码/数据/线上），不凭印象
3. preflight → deploy.ps1（含 smoke）；失败先修绿
   同一原因连挂两次 → 记「本轮踩的坑」并换下一个可做项
4. 实测证据写回本文件对应条目 → 标 [x] → 指针前移
5. 一项一提交（类型(模块): 中文说明）；小程序与后端分开
6. 要人拍板的 → 停，写「待拍板」，不自行决定
7. 每完成 5 项对账一次：BACKLOG 指针 / git 提交数 / 线上 commit / smoke 全绿
   四条对齐才算数
```

## 旧版协议（保留备查）

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

2026-11-06 已全部拍板：
  ①        → D-表式-02
  ②~⑥     → D-记忆-07~11
  ⑦~⑩     → D-记忆-03~06
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
[x] B5 smoke 断言（15 条，已进部署门禁）：表数=天数、每表 4×4、合并 4/2/1/1、
      7 个固定标签都在、巡视内容清单未动、部位已填写；校验后删掉导出文件
      顺手修 smoke 两个 bug：sql() 没 return stdout（按任务清理永远删不掉）；
      清理改按 task_id（模型有时不写 obj，前缀判断失效，垃圾越攒越多）
[x] B6.0 单表机制：POST /api/v1/reports/form {projectId, docId}
      一份已入库文书 → 套对应模板；未接模板返回 400 并说明；越项目 400
      表式差异全在 <表名>.fields.json（block 槽支持 lines_key/sig_key/date_key）
[x] B6.1 旁站记录（GD-B-212）接入
      实测 doc 173：部位「西楼A区十层墙柱十一层梁板 · 混凝土浇筑」、
      时间「2026-11-06 09:10 → 10:40」、施工情况/问题/签名/日期都写进去了
      修了三处映射：问题块行来源用全字段名、部位与工序合并、只有时分的时间用创建日补全
[x] B6.2 平行检查记录（GD-B-213）接入
      实测 doc 310（登记后生成）：6×4、四块正文齐、结论行「经检查☑是/□否」按结论勾选
      修 formfill：没有 sig_label 的块传空串，以前 `is not None` 会把尾部复选框行清空
[x] B6.3 监理日志（GD-B-220 房建新）接入 + 按天出表接口
      实测 yun-gu 11-05..11-06：2 张 11 行 × 9 列、表头四格、
      施工进展/监理工作/问题处理/其他、记录人（签名）都写进去了
      气温/天气从影像里取，没有就留空（不编）
[ ] B6.4 监理通知单（GD-B-217）
[ ] B6.3 监理日志（GD-B-220）
[x] B6.4 监理通知单（GD-B-215/216）接入
      同一 docx 两张表：通知单（1×1 长文本）+ 回复单（8×2 空白留给施工单位）
      实测 doc 315：致/事由/内容（部位·问题·整改要求·期限·依据）/署名/日期都对，
      回复单原样未动
      新增 cell_para 槽（rewrite / block_after / last_rewrite）+ 双表模板校验修正
[x] B6.5 决策：不做省统表表式（省统表 1~25 无此表，复用 15 会伪造表名/编号）；
      系统保留文书类型；需要出稿走「非正式导出」（普通文档 + 页眉非省统表水印）—— D-表式-02
[x] B6.6 smoke 覆盖单表/按天接口（已进门禁）
      /reports/form：双表 (1×1)+(8×2)、致/事由/内容/项目章/总监都在、回复单未被代填
      /reports/daily：表数=天数、每张 11×9、四块内容 + 记录人 + 审阅日期都在
      导出文件用完即删
[ ] B6.6 smoke 覆盖单表接口（用一张 smoke 造的文书生成并校验结构）
```

验收：生成物与 GD-B-214 原件「同表数、同行列、同合并」；多天多张。

---

## Wave 3 · 记忆变“懂我”（C+ 模式，2~3 天）

设计已定（`[x] 决策`），实现待做：

```
[x] 决策：不选“纯人写”（负担），不选“全自动写项目规矩”（失控）
[x] 决策：个人层自动积累；项目层自动提案 + 一键确认（可配超时生效）
[x] C0 记忆条目元数据：source_ref（来源对话/任务）、applies_to（作用域）、updated_at
      表已加列并回填 applies_to=scope:owner；create 收 sourceRef、重复条目也更新 updated_at；
      list 带出全部元数据；smoke 断言 applies_to=project:<项目> 与 updated_at
      实测：新建条目 applies_to=project:test-sandbox、source_ref=task:0、
            updated_at=2026-11-06 16:52:43
      **核实**：引擎整理确实在跑（homes/501 的 MEMORY.md 4409B + summary 2060B，
      02:53 生成）；notes 通道 0 文件（用户级记忆还没人写过）
[x] C1 个人×项目记忆：homes/<uid>/memories/projects/<pid>.md（同一记忆根，不新建第三处）
      实测：user 980 写 test-sandbox / yun-gu-3qi 两条 → 各进各的文件，互不串；
      删除后文件重渲染成（空）；user 981 没有该目录（按家目录隔离）
      memory 表加 project_id；applies_to=user:<uid>@project:<pid>；list 支持按项目过滤
      顺带修：agent.py 用了 HOMES_ROOT 却没导入（用户级记忆一写就 500）；
             create 先渲染后插入 → 文件写成（空）；delete 没重渲染 → 文件留着条目
[x] C2 项目规矩提案 + 一键确认（rule_proposal 表 + 4 个接口 + jl-tool propose_rule）
      实测：提案 pending → rules.md 里没有；accept → 写 memory(source=confirmed)
      并渲染进 rules.md；reject 始终不写；清理后文件也不留
      smoke：确认前/确认后/否决后 三个时点都断言规矩文件内容
      坑：memory.source 是 ENUM('manual','auto','learned')，写 confirmed 直接 500
          （1265 Data truncated）→ 线上 ALTER + schema 同步
      没复用 observation 表：它是「同类复核问题计数」聚合表（project+doc_kind+tag 唯一），
      和「一条候选规矩」不是一回事
[x] C3 记忆引用可见：[m:<id>] 标记 + 检索命中记次数
      jl-tool recall_memory → GET /api/v1/memories/recall（项目规矩/我的记忆/我在本项目的习惯）
      模型用到哪条就标 [m:<id>]；BFF 机械解析：去标记 + 加一条「你记过的」引用 + hits+1
      没标就当没用过（不虚报"依据你的习惯"）
      实测：同一查询召回两次 → hits 1 → 2
      smoke：strip_mem_refs 解析（去标记、保序去重）+ 召回命中且 hits>=1
[x] C4 「我的记忆」页：来源 / 使用次数 / 生效状态 / 停用恢复 / 删除
      后端 bootstrap 的 memos/projMemos 返回 source/hits/status/appliesTo，
      且停用的也返回（否则没有恢复入口）
      小程序：列表显示「手动记的 · 用过 N 次」「✓ 生效中 / 已停用」；
      点条目真调 PATCH/DELETE 后重拉 bootstrap
      修了两处：onMemoTap 原来只改本地数组（假写）；request.js 没有 PATCH
      实测：条目 p158 带全部元数据；停用后 status=0 且仍在列表
[x] C5 「待确认」分段（规矩提案的确认入口）
      bootstrap 带出 pendingRules；小程序第三段「待确认 N」，
      每条显示正文 + 「模型提议 · 还没生效」，采纳/否决两个按钮走 C2 接口
      实测：提案 id=22 → bootstrap pendingRules 命中 → 采纳 200 accepted →
            清理后 rules.md 里不残留
      坑：给 jl-tool 加 recall_memory/propose_rule 时，name 插进了 record_review 对象里
          （重复键），且 import 行没同步 → "proposeRule is not defined"，
          模型空跑、出稿 doc=None、部署连红两次
          → preflight 新增「import 是否覆盖 HANDLERS」检查，以后这类直接报错
[x] C6 指标可查：GET /api/v1/metrics/memory?projectId=
      口径：主动写=source in (manual,confirmed) · 复用=SUM(hits) ·
            撤销率=停用/总数 · 确认率=accepted/(accepted+rejected)
      实测 test-sandbox：entries=12 active=12 revokeRate=0.0
            recallUses=524 humanWrites=3；byWeek 202645；
            proposals pending/accepted/rejected=0 → acceptRate=null
      smoke：字段齐全 + byWeek 是数组
[ ] C6 指标可查：每人每周主动写记忆次数、记忆复用次数、撤销率、确认率
[x] C7 近义归并：脚本出清单（只提议，不改库）
      scripts/propose_rule_merge.py：归一文本 + 编辑相似度/二元组 Jaccard 双口径，
      >=0.72 聚组，输出 runtime/rules-merge-proposal.md
      同时修 smoke：清理时用「原文 PATCH text」触发重渲染，不再 PATCH status=1
      —— 之前正是它把归并停用的 6 条近义规则又启用了（rules.md 从 6 条涨回 12 条）
      已恢复 test-sandbox 的归并状态（保留 17/21/22/31/40/94，其余 status=0），
      rules.md 回到 6 条
      剩余待拍板：yun-gu-3qi 的 #1（本单位体积写 m³，不写「方」）与
      #3（本单位体积一律写 m³，禁止写「方」）建议合并
[x] C8 引擎整理端到端跑通（原来的"从没验证过"变成"有证据、且修好了"）
      机制（读源码）：整理只在引擎进程启动时跑；phase1 领 rollout（闲置>=6h、<=10d），
      phase2 合并成 MEMORY.md + summary；每用户一个家目录
      发现 3 个真问题并修掉：
      ① 整理只在启动时跑，而我们的 app-server 常驻 → 永远不整理
         → 新增 appserver.recycle_idle() + jobs.engine_memory（每天 04:10 回收闲置 6h+ 引擎）
      ② [memories] 没配 extract_model → 回落到 provider 的 gpt-* 抽取模型，
         dashscope 报 "Model not found: gpt…" → phase1 全失败
         → ensure_home 写 extract_model=主模型；migrate-homes 补 77 个老家目录
      ③ consolidation_model 用主模型时 phase2 写不出合法 apply_patch
         （"invalid hunk"）→ 换成复核模型 qwen3.8-flash 后成功
      实测（user 980）：raw_memories 37B → 73899B/814 行；
      MEMORY.md 21997B + summary 8505B（含 Task Group/scope/applies_to）
      脚本：scripts/check-engine-memory.py（每用户 rollout/可整理/raw/MEMORY/summary）
      坑：TOML 字符串必须加引号——不加引号整份配置被引擎丢弃（出稿 1 秒空跑）
      2026-11-06 17:30 部分核实：homes/501 的 MEMORY.md 4409B + summary 2060B（02:53 生成），
      内容已有 User Profile / 「只回：记住」画像；phase 的时序（闲置/6h）还没验。
```

**2026-11-06 17:30 记忆审计（服务器实测，结论进 C9~C15）**

```
· memory 表 38 条生效：manual 11（733 次命中）/ learned 28（828 次命中）
· 58/60/62/64 四条「依据要写到具体条号」在 12:32–12:45 内重复入库，hits 全 0
· 89「监理通知单标题需包含楼栋号（如 smoke-78#楼）」source=learned —— 测试值被当长期习惯
· 38 条 project_id 全 NULL → 个人记忆跨项目生效；render_user_project_md 永远「（空）」
· homes/501/memories：MEMORY.md 4409B、summary 2060B（02:53），
  summary 的 User Profile 已把「记住：…只回：记住」写成 high confidence 全局画像
· records/rules.md 只有 test-sandbox 有；yun-gu-3qi/records/ 为空 → 23 条项目记忆模型看不到
```

```
[ ] C9 写入去重升级：create 路径也跑 similar()，命中合并文本 + hits+1（不只比完全相等）
      验收：近义四条（「依据写到/要写/写明 具体条号」）不再新增；存量并进 C7 的人工合并清单
[ ] C10 一次性内容过滤：日期/编号/部位（#楼）/测试串（smoke-）不进 user 记忆
      验收：learn 与 create 共用黑名单，命中拒绝并记日志；项目规矩必须带 source_ref
[ ] C11 user 记忆加项目边界：project_id 必填或显式 global
      验收：新建默认带当前 project_id；存量按确认过的项目回填；recall 不再无条件兜底 NULL
[ ] C12 排序加窗口与容量：hits 按 30 天窗口 + 时间衰减；每 owner 上限 50；月度 compaction
      验收：30 天未用的记忆降权；超限按「低分 + 最久未用」淘汰并写审计
[ ] C13 引擎记忆可见化：「我的」页能看/删 homes/<uid>/memories 的 MEMORY.md / summary
      验收：GET /api/v1/memories?scope=engine 返回摘要；删除后重渲染 summary
      没有这一项，C4 的「撤销/删除」只覆盖 mysql 层，自动注入那层仍是黑盒
[ ] C14 注入预算与可审计：每轮 top-k 注入 + trace 记录用了哪几条 memory id
      验收：注入 ≤N 条（可配）；task 的 trace 里能查出「本轮引用了哪几条记忆」
[ ] C15 记忆效果评测：50 条「记忆开/关」对照（离线跑，不阻塞 C3~C7）
      指标：纠正率、重复率、输出漂移（同输入两次差异）、引用命中率
```

---

## Wave 3.5 · 记忆 v2 重构（设计：`docs/MEMORY-V2.md`）

> 2026-11-06 17:40 设计稿。结论：现在的 `hits` 排序把“被搜到”当成“有用”，
> 加上学自己的输出、全量注入，三个正反馈一个没断；必须换排序和写入闸门。
> C9~C15 与 M1~M4 重叠，执行以 M 为准，C 编号保留作验收点。
> **不改当前顺序**：C 系列收口后启动；①~⑩ 已全部拍板（D-表式-02 / D-记忆-03~11），M0 只锁指标口径。

```
[ ] M0 设计定稿：按 D-表式-02 / D-记忆-03~11 锁定口径
       （排序三段启用 / 引擎 A / 影子 3 天 / 全降级+20 条 / 单写者 / project 默认）
       + 指标口径（注入精度/应用率/纠正率/重复率/漂移/成本）
[ ] M1 数据模型：memory_item / memory_variant / memory_use / memory_review / memory_audit
      状态机 candidate→active→superseded/archived/expired；新旧并存、可回滚
[ ] M2 写入闸门：候选制（④：自动→candidate；显式「记住」/手动/确认→active）
      + 来源白名单（只认显式「记住」/编辑 diff/驳回/确认）
      一次性内容过滤（日期/编号/部位/#楼/smoke-）；normalize + 近义合并
      验收：4 条「依据条号」只进 1 条；smoke-78#楼 进不了 candidate
[ ] M3 读取改造：scope 过滤（③：默认 project_user，去掉 project_id IS NULL 兜底）→ 召回 → 重排 → top-k=3
      rank = rel × U × D + UCB；U=(helpful+1)/(helpful+harmful+2)；D=exp(−Δ天/30)
      三段启用：M3 先 rel + D + 固定探索位（每 10 轮 1 条），U 用先验 0.5；
      hits 退役（改名 injected_count，只统计不排序）；影子 3 天/50 轮（D-记忆-05）
[ ] M4 反馈闭环：编辑 diff/删除/确认 → α/β；weekly 衰减、monthly compaction；memory_use 记账
      ⑤：每 scope active ≤50、candidate 14 天过期、超限按最低 U×D 淘汰（同分最久未用）
[ ] M5 人格防火墙：kind=style（交互偏好）每 scope ≤2、需确认、90 天过期；
      禁止从重复次数推置信度；测试里的「只回：记住」不得进 User Profile
[ ] M6 引擎记忆处置：A 关闭自动注入（D-记忆-04/07），B 仅作降级
      memory 表是唯一写者；MEMORY.md 不是权威；summary 拆候选，不自动 active
[ ] M7 迁移与评测：38 条全量降级 + 20 条待确认（D-记忆-06）、
      ③：15 条 user 行无来源 → 留 candidate 评审，24 条 project 行回填 owner_id；
      50 条开/关对照、6 个指标看板
[ ] M8 清理测试污染（D-记忆-11）：test-sandbox 记忆不进队列；smoke-* /「只回：记住」/
      4 条近义直接 archived（合并 1 条 canonical）
```

---

## Wave 4 · 多模态沉淀（1~2 天）

```
[x] D1 影像索引按部位分组（从"平铺追加"改成"按部位可查"）
      · records/photos.md 改为从 DB 渲染的视图：按部位分组、同一 sha 只留最新描述
      · GET /api/v1/photos/index?projectId= → 每个部位的张数/首末时间/描述样例
      · 每轮上下文加指针「records/photos.md（问这个部位以前查过什么先看这里）」
      · 实测 test-sandbox：共 7 张 / 2 个部位（未标部位 6 张、3号楼2层 1 张）；
        /photos/search?q=连墙件 能取回对应原图
      · **数据缺口**：yun-gu 的 14 张历史照片 place 和 caption 都是空（早期上传没走描述链路）
        → 索引里查不到它们，需要补描述（见待拍板）
[ ] D2 真机验收（相机/定位/水印/下载）—— **需你配合**：得在微信里真机打开
      暂不阻塞其它项，先跳过
[ ] D2 真机验证：camera+cover-view 层级、chooseLocation、取景与出图一致
```

---

## Wave 5 · 清理与防回归（1 天）

```
[x] E1 删死代码：run_cli_turn / _parse_jsonl / parse_multipart / relevance / get_or_create_thread
      删 195 行（agent 114 / biz 46 / core 35），import 名单同步清理
      五个函数在线上已确认 0 处残留；preflight 硬编码 11→8、静默失败 13→11
      踩坑两个（都已修）：
      ① api.py 的 import 名单里漏删 run_cli_turn → 服务起不来（ImportError）
      ② 删 parse_multipart 时把 parse_multipart_full 一起删了（前者只是后者的死包装），
         而上传路由用的是 full 那个 → 又 ImportError；已恢复 full，只删包装
[x] E2 删死端点：/members、/render-memory
      证据：/members 无人调用；render-memory 只有后台一个按钮在用（AGENTS.md 早已自动渲染）
      删除：两条路由 + handle_members/member_add + handle_render_memory + 后台按钮/JS + 三处 import
      preflight 新增「死端点禁回退」检查（五个名字再出现就红）
      实测（重启后）：/members → 404、render-memory → 404、后台页 renderMemory 出现 0 次
      坑：最后一次部署被中断，文件已传但进程没重启 → 端点还 200；
          重跑一次 deploy（幂等）后恢复，说明"部署成功"要看进程启动时间不能只看文件
[x] E3 core.py 7 个重复常量、STD_RE、clean_reply 残句
      · 7 组常量（AUTH_REQUIRED/TOKEN_TTL_HOURS/BLOB_ROOT/WEEK/STATE_TO_ST/
        KIND_TO_ICON/KIND_TO_TINT）各定义两遍 → 删第二组，保留第一组
      · STD_RE 无人使用（三个文件只 import）→ 删定义 + 三处 import
      · clean_reply 加标点清洗：先去掉标点间空白，再合并连续标点，再逗号+句号归一
      · 实测：「好了。。 。 请看 /home/a/b.md」→「好了。请看」；
              「已出稿 /home/x/a.md，。 请查收。」→「已出稿，请查收。」
      · 线上复核：重复常量 0、STD_RE 残留 0
      门禁：preflight 新增「常量不许重复定义」；smoke 新增「清洗后无残句标点」
[x] E4 tools/workspace.js readMemories 改读 records/rules.md
      旧路径 memories/project.md 早就不写了 → get_context 的记忆永远是空
      现在先读 records/rules.md，旧文件若还在也带上（逐步淘汰）
      实测（在工作区里直接调 jl-tool get_context）：
        memoriesFile 返回「# 项目规矩 … - 整改回复必须附整改前后对比图…」✓
        wsPath=/ws/test-sandbox
      preflight 新增：readMemories 必须读 records/rules.md
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
Wave 3   记忆 C+（C0~C15）
Wave 3.5 记忆 v2（M0~M8，设计已出；C 系列收口后启动）
Wave 4   多模态（D1~D2）
Wave 5   清理（E1~E6）
Wave 6   合规/保险（F1 暂缓，F2 可随时做）
```

## 决策记录（避免以后再争）

```
D-记忆-01  记忆只有单一写者：项目规矩 = memory 表（rules.md 是渲染视图）；
           个人记忆 = 引擎整理（homes/<uid>/memories）
           2026-11-06 被 D-记忆-07 取代：个人记忆也收归 memory 表，引擎只做抽取
D-记忆-02  不做“纯人写”（负担）；不做“模型自动写项目规矩”（失控）；
           个人层自动、项目层提案+确认
D-记忆-03  排序上全公式 rel×U×D+UCB，但分三段启用：
           M3 先 rel + D + 固定探索位（每 10 轮 1 条），U 用先验 0.5；
           M4 反馈数据到位后打开 U；M5 之后用 2 周样本调 UCB 系数。
           理由：没有结果数据的 UCB 是噪声，固定探索位已够冷启动。
D-记忆-04  引擎记忆走 A：关闭自动注入；MEMORY.md/summary 只作候选来源与审计，
           确认后经 memory_item 生效；若引擎没有关闭开关 → 降级 B（只读展示+删除），
           2 周内替换。理由：黑盒注入是“人格化”最直接的通道。
D-记忆-05  切读：影子并行 3 天或 50 轮（先到为准），比较 top-3 重合率与注入差异；
           回滚保留旧字段一个版本。理由：38 条数据量下，1 周影子的成本大于收益。
D-记忆-06  迁移：38 条全量降级 candidate（不直接 active）；去重后按
           「有 source_ref/confirmed/manual + 非测试 + 非近义」挑 20 条进待确认，
           其余归档可恢复；测试污染直接 archived。
           理由：不把脏数据洗成权威；20 条控制人工评审成本。
D-记忆-07  记忆唯一写者 = memory 表（memory_item）。引擎只做“抽取器”；
           MEMORY.md/summary 仅作候选来源与审计，不直接注入、不作权威。
           理由：引擎层不可见、不可删、无项目边界；D-记忆-01 的“个人=引擎”作废。
D-记忆-08  user 记忆默认 project_user（必须带 project_id）；只有显式
           「应用到所有项目」才升 global。
           存量：15 条 user 行 project_id/source_ref 全 NULL，无法推断来源项目 ——
           一律留 candidate，评审时逐条定 project 或 global，定不了就归档；
           24 条 project 行把 project_id 回填为 owner_id（scope 已含项目）。
D-记忆-09  新记忆默认 candidate；只有“用户显式动作”可直接 active：
           显式「记住：…」、在「我的」里手动添加、确认提案。
           自动抽取（learned/auto/engine）一律 candidate，14 天未确认过期。
D-记忆-10  保留：每 scope active 上限 50；candidate 14 天过期；D 的 τ=30 天；
           超限按“排名分最低（U×D）”淘汰，同分再按 last_used_at 最久未用；
           style 另限每 scope 2 条、90 天过期；每月 compaction 合并近义、归档低效用。
           理由：纯 LRU 会淘汰“一年用一次但很关键”的规矩，按分数更稳。
D-记忆-11  测试污染立即清：test-sandbox 的「只回：记住」、smoke-78#楼、
           4 条「依据条号」近义 —— 全量降级时直接 archived（不删、可恢复）；
           4 条合并为 1 条 canonical（写具体条号），其余 superseded；
           test-sandbox 项目下的记忆不进待确认队列。
D-表式-02  B6.5「监理检查记录」不做省统表表式：省统表 1~25 无此表，
           复用 15 平行检查会伪造表名/编号，不能做。
           系统保留该文书类型；需要出稿时走“非正式导出”（普通文档 + 页眉
           「非省统表格式 · 仅供内部使用」），不盖项目章；
           以后拿到广东统表对应原件 → 另立 B6.5b 接模板。
D-表式-01  表式几乎不变 → 原件模板原位填字 + 结构签名校验；不手绘、不让模型画
D-部署-01  删文件必须写进 scripts/deploy-clean.txt（scp 不删远端）
D-环境-01  服务器 python-docx 用 pip --user 安装；重装系统要补（写进 DEPLOY 文档）
```
