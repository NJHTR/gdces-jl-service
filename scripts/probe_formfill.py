# -*- coding: utf-8 -*-
"""可行性试验：在官方模板上原位填字，检查结构（行列/合并/固定标签）有没有被破坏。

只改指定单元格的文字，保存副本，再读回来比对结构签名。
"""
import io
import sys

from docx import Document

TPL = sys.argv[1]
OUT = sys.argv[2]


def signature(doc):
    sig = []
    for tb in doc.tables:
        rows = []
        for r in tb.rows:
            seen, cells = {}, []
            for c in r.cells:
                key = id(c._tc)
                if key in seen:
                    cells.append("«m%d»" % seen[key])
                    continue
                seen[key] = len(cells)
                cells.append(c.text.replace("\n", "⏎").strip()[:40])
            rows.append(cells)
        sig.append((len(tb.rows), len(tb.columns),
                    [len({id(c._tc) for c in r.cells}) for r in tb.rows],
                    rows))
    return sig


doc = Document(TPL)
before = signature(doc)

# 找"巡视的工程部位"所在行，填它右边的单元格；再填巡视时间、问题及处理
tb = doc.tables[0]
filled = []

for ri, row in enumerate(tb.rows):
    cells = row.cells
    for ci, c in enumerate(cells):
        if "巡视的工程部位" in c.text:
            # 右边第一个不同的单元格就是值
            for cj in range(ci + 1, len(cells)):
                if id(cells[cj]._tc) != id(c._tc):
                    target = cells[cj]
                    target.text = "4#楼东侧外架、5#楼南侧外架、28#楼西侧模板支撑"
                    filled.append(("部位", ri, cj))
                    break
        if "巡视时间" in c.text:
            for cj in range(ci + 1, len(cells)):
                if id(cells[cj]._tc) != id(c._tc):
                    cells[cj].text = "2026-11-05 08:30 至 2026-11-05 17:30"
                    filled.append(("时间", ri, cj))
                    break
        if "巡视发现的问题" in c.text:
            c.text = ("巡视发现的问题及处理情况：\n"
                      "1. 4#楼东侧外架连墙件缺失 2 处（已下发监理通知单，期限 2026-11-08）\n"
                      "2. 28#楼西侧模板支撑扫地杆缺失（期限 2026-11-07）\n"
                      "巡视监理人员（签字）：李工\n2026 年 11 月 5 日")
            filled.append(("问题及处理", ri, ci))

doc.save(OUT)

after_doc = Document(OUT)
after = signature(after_doc)

print("写入单元格:", filled)
print("表格数 before/after:", len(before), len(after))
same_shape = all(b[0:3] == a[0:3] for b, a in zip(before, after))
print("行列/合并结构一致:", same_shape)

# 固定标签是否还在原位
labels = ["巡视的工程部位", "施工单位", "巡视时间", "巡视内容", "注：本表一式一份"]
text_all = "\n".join(p.text for p in after_doc.paragraphs) + "\n" + "\n".join(
    c.text for t in after_doc.tables for r in t.rows for c in r.cells)
for lb in labels:
    print("  仍包含 %-12s %s" % (lb, lb in text_all))

# 只允许值单元格变化
changed = []
for b, a in zip(before, after):
    for rb, ra in zip(b[3], a[3]):
        for cb, ca in zip(rb, ra):
            if cb != ca:
                changed.append((cb[:18], ca[:24]))
print("变化单元格数:", len(changed))
for x in changed[:6]:
    print("   ", x)
