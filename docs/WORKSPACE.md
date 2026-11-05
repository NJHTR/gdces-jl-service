# 项目工作区规范

> 每个监理项目 = 一个 git 仓库。AI 的每一次产出都是一次可回溯的提交。

## 目录

```
workspaces/{project_id}/
  PROJECT.md                项目上下文（给 agent 读的"这是什么项目"）
  meta.json                 工程概况（21 个字段，机器可读）
  memories/
    project.md              项目记忆（由 MySQL 渲染，不手改）
  records/
    2026-11-04/
      0805-外架连墙件.md
      0940-顶板钢筋验收.md
  docs/
    监理通知单-NO-018.md
    旁站记录-3F5F-顶板.md
  photos/
    manifest.json           照片索引（sha256 + 元数据），图片本体在 data/blobs
  .agent/
    thread.md               会话摘要（长期记忆沉淀）
  .gitignore
```

## 三条规则

**① 图片/音频不进 git**

```
图片本体  → data/blobs/{sha256}
git 里只存 photos/manifest.json：
  { "sha256": "...", "path": "blobs/ab/abcd...", "takenAt": "...", "place": "...", "weather": "..." }
```

理由：几百张现场照片进 git，clone/diff 全垮。内容寻址天然去重。

**② 文件即事实，数据库即索引**

- 文书/记录的**内容**在 git 里（可 diff、可回滚）
- 状态、关联、检索在 MySQL（`docs`、`records`、`memories` 表）
- 两边用 `git_commit` 与 `path` 关联

**③ 记忆文件是渲染产物**

`memories/project.md` 由 MySQL 生成，**不手改**。要改记忆走管理后台 → 下次会话启动时重新渲染 → 提交一次 commit。

---

## 提交规范

```
<type>(<scope>): <一句话说清改了什么>

依据: <refs>
任务: <task_id>
```

| type | 用途 |
|---|---|
`record` | 新增/修改现场记录 |
`doc` | 生成/修改文书 |
`memory` | 记忆渲染更新 |
`meta` | 工程概况变更 |

`依据` 必填（record/doc），格式：

```
依据: msg_8837, photo_221, JGJ130-2011#6.4.3, NO-014
```

四类引用前缀：

| 前缀 | 指向 |
|---|---|
`msg_` | 现场消息 id |
`photo_` | 照片 id（或 sha256） |
`规范号#条号` | 规范条文 |
`NO-` | 历史文书编号 |

**这条是硬约束**：没有依据的文书提交会被 `commit-msg` 钩子拒绝。

---

## 钩子

| 钩子 | 作用 |
|---|---|
`commit-msg` | record/doc 类型必须带 `依据:` 行 |
`pre-commit` | 扫密钥（`sk-`、`api_key`、`password`），命中直接拒绝 |

---

## 分支

- `main`：正式内容
- `agent/{task_id}`：AI 的试验性改动（生成草稿、批量整理），确认后合并

**AI 永远不直接改 `main`**，只往 `agent/*` 提交，由 BFF 或人工合并。这样 AI 犯错不会污染正式台账。
