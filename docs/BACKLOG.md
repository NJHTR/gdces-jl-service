# 待办清单（唯一列表 · 2026-11-06 17:40 更新）

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
next = Wave3 / C4  「我的记忆」页补：来源 / 使用次数 / 撤销 / 删除
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
① B6.5「监理检查记录」用哪个模板？
   省统表 1~25 里没有这张。可选：
   A. 复用 15 平行检查记录（结构最接近，但要改表名/编号）
   B. 另找广东统表里的对应表（需要你给文件）
   C. 不做这张表（系统里仍可有这种文书，只是不能出表式 docx）

② 记忆最终只留哪一层写者？（D-记忆-01 现在写的是「个人=引擎、项目=表」）
   A. 统一到 memory 表（可查可删可审计；引擎 MEMORY.md 要关，501 现有数据要迁）
   B. 统一到引擎（保持自动积累；但要做可读/可删/项目边界，成本高）
   C. 维持两层，但把引擎 summary 暴露到「我的」页（折中，先做 C13）
③ user 记忆默认作用域：必须带 project_id（项目内），还是允许全局？
   现有 38 条 project_id 全 NULL；回填按「来源对话的项目」还是「用户最近项目」？
④ 新记忆是否先「待确认」？只对自动学来的（learned/auto）待确认，还是全部？
   影响「我的」页交互与 C5 的列表口径。
⑤ 保留策略：每 owner 上限多少条？hits 窗口 30 天是否合适？超限淘汰按「低分」还是「最久未用」？
⑥ 测试污染怎么处理：test-sandbox 的「只回：记住」是否停用？smoke-78#楼、4 条近义现在就清吗？
⑦ 记忆 v2 排序是否按 `MEMORY-V2.md` 换成 `rel × U × D + UCB`？
   还是先只做“衰减”，探索项（新人新规则出头）后置？
⑧ 若 ② 选 C（两层保留）：引擎记忆走 A（关自动注入、只产候选）还是 B（保留注入 + 只读展示）？
⑨ 切换方式：影子读 1 周再切，还是直接切（回滚靠旧字段）？
⑩ 迁移范围：38 条全降级重审，还是只升 20 条高置信、其余归档？
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
[?] B6.5 监理检查记录 —— 缺原件模板（省统表 1~25 里没有这张），待拍板用哪个模板
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
[ ] C4 「我的记忆」页补：来源、使用次数、撤销、删除（现在只有真写）
[ ] C5 待确认列表（与 observation 同一套 UI）
[ ] C6 指标可查：每人每周主动写记忆次数、记忆复用次数、撤销率、确认率
[ ] C7 近义归并：现有 5 条「整改回复附对比图」变体，人工或整理员合并（不自动删）
[ ] C8 验证引擎整理真的在工作（phase1 闲置≥1h / phase2 6h）——从没验证过
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
> **不改当前顺序**：C 系列收口后启动；M0 先拍板 ⑦⑧⑨⑩。

```
[ ] M0 设计定稿：拍板 ⑦⑧⑨ + 指标口径（注入精度/应用率/纠正率/重复率/漂移/成本）
[ ] M1 数据模型：memory_item / memory_variant / memory_use / memory_review / memory_audit
      状态机 candidate→active→superseded/archived/expired；新旧并存、可回滚
[ ] M2 写入闸门：候选制 + 来源白名单（只认显式「记住」/编辑 diff/驳回/确认）
      一次性内容过滤（日期/编号/部位/#楼/smoke-）；normalize + 近义合并
      验收：4 条「依据条号」只进 1 条；smoke-78#楼 进不了 candidate
[ ] M3 读取改造：scope 过滤（去掉 project_id IS NULL 兜底）→ 召回 → 重排 → top-k=3
      rank = rel × U × D + UCB；U=(helpful+1)/(helpful+harmful+2)；D=exp(−Δ天/30)
      hits 退役（改名 injected_count，只统计不排序）；先影子读 1 周
[ ] M4 反馈闭环：编辑 diff/删除/确认 → α/β；weekly 衰减、monthly compaction；memory_use 记账
[ ] M5 人格防火墙：kind=style（交互偏好）每 scope ≤2、需确认、90 天过期；
      禁止从重复次数推置信度；测试里的「只回：记住」不得进 User Profile
[ ] M6 引擎记忆处置：A 关闭自动注入，只产候选 / B 保留注入但只读+删除
      MEMORY.md 不是权威，memory_item 才是；summary 拆候选，不自动 active
[ ] M7 迁移与评测：38 条降级 candidate、scope 回填、50 条开/关对照、6 个指标看板
[ ] M8 清理测试污染：smoke-* / 「只回：记住」/ 4 条近义；迁完复核
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
           ⚠ 2026-11-06 审计后进入待拍板②：引擎层不可见/不可删，是否改成单层待定
D-记忆-02  不做“纯人写”（负担）；不做“模型自动写项目规矩”（失控）；
           个人层自动、项目层提案+确认
D-表式-01  表式几乎不变 → 原件模板原位填字 + 结构签名校验；不手绘、不让模型画
D-部署-01  删文件必须写进 scripts/deploy-clean.txt（scp 不删远端）
D-环境-01  服务器 python-docx 用 pip --user 安装；重装系统要补（写进 DEPLOY 文档）
```
