# -*- coding: utf-8 -*-
"""完整导出 docx 结构（含合并信息、列宽、完整文本），写到 UTF-8 文件。"""
import io
import sys

from docx import Document

path = sys.argv[1]
out = sys.argv[2]
buf = io.StringIO()


def say(*a):
    buf.write(" ".join(str(x) for x in a) + "\n")


doc = Document(path)

say("=== 段落（含空段，标序号）===")
for i, p in enumerate(doc.paragraphs):
    al = p.alignment
    say("  [%02d] align=%s style=%s | %r" % (i, al, p.style.name, p.text))

for ti, tb in enumerate(doc.tables):
    say("\n=== 表 %d：%d 行 × %d 列 ===" % (ti, len(tb.rows), len(tb.columns)))
    # 列宽
    try:
        widths = [round(c.width.cm, 2) if c.width else None for c in tb.columns]
        say("  列宽: %s" % widths)
    except Exception as e:
        say("  列宽取不到: %r" % (e,))
    for ri, row in enumerate(tb.rows):
        say("  --- 行 %d ---" % ri)
        seen = {}
        for ci, cell in enumerate(row.cells):
            tc = id(cell._tc)
            if tc in seen:
                say("    c%d → 与 c%d 合并" % (ci, seen[tc]))
                continue
            seen[tc] = ci
            txt = cell.text
            say("    c%d (%d 段) %r" % (ci, len(cell.paragraphs), txt[:400]))
            # 字体信息
            for p in cell.paragraphs[:2]:
                for r in p.runs[:2]:
                    if r.text.strip():
                        say("        字体 %s / %s / bold=%s" % (
                            r.font.name, r.font.size.pt if r.font.size else None, r.bold))
                        break
                break

sec = doc.sections[0]
say("\n=== 页面 ===")
say("  %.1f x %.1f cm, 边距 左%.1f 右%.1f 上%.1f 下%.1f" % (
    sec.page_width.cm, sec.page_height.cm,
    sec.left_margin.cm, sec.right_margin.cm, sec.top_margin.cm, sec.bottom_margin.cm))

io.open(out, "w", encoding="utf-8").write(buf.getvalue())
print("written:", out)
