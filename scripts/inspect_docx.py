# -*- coding: utf-8 -*-
"""把模板 docx 的结构打印出来：段落、表格、单元格合并情况。"""
import io
import sys

from docx import Document

path = sys.argv[1]
out_path = sys.argv[2] if len(sys.argv) > 2 else "docx_dump.txt"
buf = io.StringIO()

def say(*a):
    buf.write(" ".join(str(x) for x in a) + "\n")

doc = Document(path)

say("=== 段落（非空）===")
for i, p in enumerate(doc.paragraphs):
    t = p.text.strip()
    if t:
        say("  [%02d] %s" % (i, t[:120]))

say("\n=== 表格 %d 个 ===" % len(doc.tables))
for ti, tb in enumerate(doc.tables):
    say("\n-- 表 %d：%d 行 × %d 列（style=%s）" % (ti, len(tb.rows), len(tb.columns), tb.style.name if tb.style else "?"))
    for ri, row in enumerate(tb.rows):
        cells = []
        seen = set()
        for ci, cell in enumerate(row.cells):
            key = id(cell._tc)
            txt = cell.text.replace("\n", "⏎").strip()
            if key in seen:
                cells.append("«合并»")
                continue
            seen.add(key)
            cells.append(txt[:60] if txt else "·")
        say("  r%02d | %s" % (ri, " | ".join(cells)))

sec = doc.sections[0]
say("\n=== 页面 ===")
say("  宽 %.1fcm 高 %.1fcm 边距 左%.1f 右%.1f 上%.1f 下%.1f" % (
    sec.page_width.cm, sec.page_height.cm,
    sec.left_margin.cm, sec.right_margin.cm, sec.top_margin.cm, sec.bottom_margin.cm))

io.open(out_path, "w", encoding="utf-8").write(buf.getvalue())
print("written:", out_path)
