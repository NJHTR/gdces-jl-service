# 表式模板（原件，唯一格式来源）

> 交付以这里的 **docx 原件**为准；`data/knowledge/kb/**` 里的 md 是压平文本，只做检索，不能还原排版。

## 目录

```
<表名>.docx            省统表原件（人维护，git 版本化）
<表名>.fields.json     字段映射：哪个格子填什么（标签定位，不写死行列号）
```

## 加一张新表

1. 放原件：`<表名>.docx`
2. 写映射：`<表名>.fields.json`（slots: label/target/kind；fixed_labels 用于校验）
3. 跑 `scripts/check-templates.py` 校验标签都能定位到
4. 在 report/formfill 里接数据源（只写映射，不改填充器）

## 约定

- `target`：`para`（段落里按模板串替换）、`right`（同行标签右侧第一个单元格）、`cell`（该单元格整块）
- `kind`：`text`（单值）、`block`（多行，保留 `keep_tail` 里的尾部行，如签名/日期）
- `fixed_labels`：生成后必须仍在原位，否则视为模板漂移，拒绝交付
- 不要改原件里的样式、合并、列宽；要改格式就换新版本并升 `version`