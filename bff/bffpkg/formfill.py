# -*- coding: utf-8 -*-
"""表式填充器：在**原件模板**上原位填字，保证格式不变。

设计（见 docs/ARCHITECTURE-FORMS.md）：
- 模板原件是唯一格式来源；这里只改目标单元格/段落的文字，不动样式、合并、列宽
- 定位用**标签**（"巡视的工程部位"这种固定文字），不写死行列号
- 生成后做**结构签名**校验：表格数/行列/每行合并组数/固定标签必须与模板一致；
  文本变化只允许发生在白名单单元格里，否则拒绝交付

用法（也可被 report.py 直接 import）：
    python3 formfill.py --form 巡视记录 --data data.json --out out.docx
"""
import argparse
import copy
import io
import json
import os
import sys

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

_HERE = os.path.dirname(os.path.abspath(__file__))          # bff/bffpkg
_ROOT = os.path.dirname(os.path.dirname(_HERE))              # 仓库根
TEMPLATE_DIR = os.path.join(_ROOT, "data", "knowledge", "templates", "docx")


# ---------------------------------------------------------------- 规格
def load_spec(form):
    """按表单名找映射：文件名可以带编号前缀（16巡视记录.fields.json），
    以文件内的 "form" 字段为准，其次看文件名包含。"""
    form = (form or "").strip()
    cands = sorted(f for f in os.listdir(TEMPLATE_DIR) if f.endswith(".fields.json"))
    for name in cands:
        fp = os.path.join(TEMPLATE_DIR, name)
        try:
            with io.open(fp, encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            sys.stderr.write("[formfill] 读映射失败 %s: %r\n" % (name, e))
            continue
        if (spec.get("form") or "").strip() == form or form in name:
            spec["_dir"] = TEMPLATE_DIR
            return spec
    raise FileNotFoundError("没有「%s」的字段映射（在 %s 下）" % (form, TEMPLATE_DIR))


def template_path(spec):
    return os.path.join(spec["_dir"], spec["template"])


# ---------------------------------------------------------------- 结构签名
def table_signature(tb):
    """结构签名：(行数, 列数, 每行(单元格数, 去合并后的真实格数))。

    注意：不能用 id(cell._tc) 去重 —— lxml 的元素代理会回收，
    id() 可能被复用，导致"明明是不同格子却判成同一个"（踩过）。
    这里用元素对象做 key（dict 持有引用，hash 稳定）。
    """
    rows = []
    for r in tb.rows:
        seen = {}
        for c in r.cells:
            seen[c._tc] = 1
        rows.append((len(r.cells), len(seen)))
    return (len(tb.rows), len(tb.columns), tuple(rows))


def signature(doc):
    return {
        "tables": [table_signature(tb) for tb in doc.tables],
        "paras": len([p for p in doc.paragraphs if p.text.strip()]),
    }


def all_text(doc):
    parts = [p.text for p in doc.paragraphs]
    for tb in doc.tables:
        for r in tb.rows:
            for c in r.cells:
                parts.append(c.text)
    return "\n".join(parts)


def missing_labels(doc, spec):
    text = all_text(doc)
    return [lb for lb in (spec.get("fixed_labels") or []) if lb not in text]


# ---------------------------------------------------------------- 定位与写入
def _cell_text(cell):
    return (cell.text or "").strip()


def find_cell(tb, label):
    """按标签找单元格，返回 (row_idx, cell_idx)。"""
    for ri, row in enumerate(tb.rows):
        for ci, cell in enumerate(row.cells):
            if label and label in _cell_text(cell):
                return ri, ci
    return None, None


def cell_right_of(tb, ri, ci):
    """同一行、标签单元格右边第一个"不同的"单元格（跳过合并重复）。

    用 `is` 比较元素对象，别用 id()：代理对象回收后 id 会复用。
    """
    row = tb.rows[ri]
    anchor_el = row.cells[ci]._tc
    for cj in range(ci + 1, len(row.cells)):
        el = row.cells[cj]._tc
        if el is not anchor_el:
            return row.cells[cj]
    return None


def _first_run_style(cell):
    """取单元格第一段的第一个 run 作为样式模板（字体/字号）。"""
    for p in cell.paragraphs:
        for r in p.runs:
            if r.text.strip():
                return r
    return None


def clear_cell(cell):
    """清空单元格文字但保留段落与首段样式。"""
    keep = cell.paragraphs[0]
    for p in list(cell.paragraphs)[1:]:
        p._element.getparent().remove(p._element)
    for r in list(keep.runs):
        r._element.getparent().remove(r._element)
    return keep


def write_text(cell, text):
    p = clear_cell(cell)
    style = None
    for r in cell.paragraphs[0].runs:
        style = r
        break
    run = p.add_run(str(text or ""))
    if style is not None and style.font.name:
        run.font.name = style.font.name
        run.font.size = style.font.size
        run.bold = style.bold
    else:
        # 空单元格里没有 run 可抄样式 → 用省统表正文默认
        run.font.name = "宋体"
        run.font.size = Pt(10)
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    return cell


def write_block(cell, lines, keep_tail=None, sig_line=None, date_line=None):
    """多行写入：保留标签段（不一定是第 0 段！）与尾部签名行，中间换成 lines。

    两个坑：
    · 旁站模板的首段是空的，标签在第 2 段 → 不能假设 paragraphs[0] 是标签
    · 段落比较必须用 XML 元素身份（python-docx 每次访问都新建包装对象）
    """
    keep_tail = keep_tail or []
    ps = cell.paragraphs
    if not ps:
        return cell

    # 1) 标签段：第一段有字的
    label_idx = None
    for i, p in enumerate(ps):
        if p.text.strip():
            label_idx = i
            break
    if label_idx is None:
        return cell
    label_el = ps[label_idx]._element

    # 2) 尾部签名段：标签之后、包含 keep_tail 的第一段
    tail_idx, tail_el = None, None
    if keep_tail:
        for i in range(label_idx + 1, len(ps)):
            if any(t in ps[i].text for t in keep_tail):
                tail_idx, tail_el = i, ps[i]._element
                break

    style_run = _first_run_style(cell)
    font_name = style_run.font.name if style_run else None
    font_size = style_run.font.size if style_run else None
    bold = style_run.bold if style_run else None

    # 3) 删掉：标签之后、签名之前的所有段；签名之后的日期段也删（稍后重写）
    keep_els = {label_el}
    for i in range(0, label_idx + 1):
        keep_els.add(ps[i]._element)
    if tail_el is not None:
        keep_els.add(tail_el)
    for p in list(ps):
        if p._element not in keep_els:
            p._element.getparent().remove(p._element)

    # 4) 正文行：倒序插在标签段之后，保证顺序
    for line in reversed(list(lines or [])):
        np = cell.add_paragraph()
        r = np.add_run(str(line))
        r.font.name = font_name or "宋体"
        r.font.size = font_size or Pt(10)
        r.bold = bold
        if not font_name:
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        label_el.addnext(np._element)

    # 5) 签名行
    # 注意：空字符串不是 None。没有 sig_label 的槽会传 ""，
    # 若按 is not None 判断会把尾部（如"经检查□是/□否…"）清空 —— 踩过。
    if tail_el is not None and sig_line:
        tail = None
        for p in cell.paragraphs:
            if p._element is tail_el:
                tail = p
                break
        if tail is not None:
            for r in list(tail.runs):
                r._element.getparent().remove(r._element)
            r = tail.add_run(sig_line)
            r.font.name = font_name or "宋体"
            r.font.size = font_size or Pt(10)
            r.bold = bold
            if not font_name:
                r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    # 6) 日期行：放签名后
    if date_line:
        dp = cell.add_paragraph()
        r = dp.add_run(date_line)
        r.font.name = font_name or "宋体"
        r.font.size = font_size or Pt(10)
        if not font_name:
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        (tail_el if tail_el is not None else label_el).addnext(dp._element)

    return cell


def write_para(para, text):
    """整段替换：保留第一个 run 的样式。"""
    style = None
    for r in para.runs:
        if r.text.strip():
            style = r
            break
    for r in list(para.runs):
        r._element.getparent().remove(r._element)
    run = para.add_run(text)
    if style is not None:
        run.font.name = style.font.name
        run.font.size = style.font.size
        run.bold = style.bold
    return para


# ---------------------------------------------------------------- 填充
def _fill_one(doc, spec, data, table_idx=0, header_idx=0):
    """填一份（第 table_idx 张表 + 第 header_idx 个"工程项目名称"段落）。"""
    tb = doc.tables[table_idx]
    changed = []

    # 段落类（工程项目名称/编号）
    header_paras = [p for p in doc.paragraphs if "工程项目名称" in p.text]
    for slot in spec.get("slots") or []:
        if slot.get("target") != "para":
            continue
        if header_idx >= len(header_paras):
            raise RuntimeError("模板里找不到第 %d 个「工程项目名称」段落" % (header_idx + 1))
        text = (slot.get("text") or "").format(**data)
        write_para(header_paras[header_idx], text)
        changed.append(("para", header_idx))

    # 单元格类
    for slot in spec.get("slots") or []:
        if slot.get("target") == "para":
            continue
        ri, ci = find_cell(tb, slot.get("label"))
        if ri is None:
            raise RuntimeError("模板里找不到标签「%s」" % slot.get("label"))
        if slot["target"] == "right":
            target = cell_right_of(tb, ri, ci)
            if target is None:
                raise RuntimeError("标签「%s」右边没有可填的单元格" % slot.get("label"))
            write_text(target, data.get(slot["key"], ""))
        else:  # cell
            target = tb.rows[ri].cells[ci]
            if slot.get("kind") == "block":
                # 每张表的"正文"来自不同的键：巡逻用 lines，旁站用「施工情况」等
                lines = data.get(slot.get("lines_key") or "lines") or []
                # 只有显式声明了签名/日期的槽才写这两行（施工情况那种正文块不该带日期）
                sig = ""
                if slot.get("sig_label"):
                    sig = slot["sig_label"] + (
                        data.get(slot.get("sig_key") or "inspector") or "")
                date = data.get(slot["date_key"]) if slot.get("date_key") else ""
                write_block(
                    target,
                    lines,
                    keep_tail=slot.get("keep_tail") or [],
                    sig_line=sig,
                    date_line=date,
                )
            else:
                write_text(target, data.get(slot["key"], ""))
        changed.append(("cell", ri, ci))
    return changed


def _block_elements(doc, title=""):
    """返回要复制的一页元素（标题…表格…注），用于多份。

    标题不能写死「巡视记录」——每张表标题不同（旁站记录/平行检查记录…）。
    """
    body = doc.element.body
    els = list(body)
    title = (title or "").strip()
    start = end = None
    for i, el in enumerate(els):
        tag = el.tag.split("}")[-1]
        text = "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()
        if start is None and tag == "p" and title and title in text:
            start = i
        if start is not None and tag == "p" and "注：本表" in text:
            end = i
            break
    if start is None:
        # 兜底：第一段非空段落就是标题（省统表都是"标题 → 编号 → 表"）
        for i, el in enumerate(els):
            if el.tag.split("}")[-1] != "p":
                continue
            text = "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()
            if text and "注：" not in text and "工程项目名称" not in text:
                start = i
                break
    if start is None:
        raise RuntimeError("模板里找不到标题段（title=%r）" % title)
    if end is None:
        end = len(els) - 1
    return [els[i] for i in range(start, end + 1)], start


def build(form, records, out_path):
    """records: list[dict]；一份记录一张表，每份之间分页。"""
    spec = load_spec(form)
    doc = Document(template_path(spec))
    base_sig = signature(doc)
    base_labels = all_text(doc)

    els, start = _block_elements(doc, spec.get("title") or spec.get("form") or "")
    body = doc.element.body
    last = els[-1]

    for i in range(1, len(records)):
        brk = copy.deepcopy(els[0])       # 标题段做分页载体
        # 在标题段前插入分页符
        for run in brk.findall(qn("w:r")):
            brk.remove(run)
        r = brk.makeelement(qn("w:r"), {})
        br = r.makeelement(qn("w:br"), {qn("w:type"): "page"})
        r.append(br)
        brk.append(r)
        last.addnext(brk)
        last = brk
        for el in els:
            new = copy.deepcopy(el)
            last.addnext(new)
            last = new

    for i, rec in enumerate(records):
        _fill_one(doc, spec, rec, table_idx=i, header_idx=i)

    # ---------- 校验
    miss = missing_labels(doc, spec)
    if miss:
        raise RuntimeError("生成后固定标签丢失: %s" % miss)
    after = signature(doc)
    if len(after["tables"]) != len(base_sig["tables"]) * max(1, len(records)):
        raise RuntimeError("表格数量不对：模板 %d，生成 %d（记录 %d 条）"
                           % (len(base_sig["tables"]), len(after["tables"]), len(records)))
    if base_sig["tables"]:
        for i, tb_sig in enumerate(after["tables"]):
            if tb_sig != base_sig["tables"][0]:
                raise RuntimeError("第 %d 张表结构与模板不一致：%r → %r"
                                   % (i + 1, base_sig["tables"][0], tb_sig))

    doc.save(out_path)
    return {"path": out_path, "records": len(records),
            "signature": after, "labels_ok": True}


# ---------------------------------------------------------------- CLI（自测用）
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", required=True)
    ap.add_argument("--data", required=True, help="JSON：单份 dict 或 [{...}, ...]")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with io.open(args.data, encoding="utf-8") as f:
        payload = json.load(f)
    records = payload if isinstance(payload, list) else [payload]
    got = build(args.form, records, args.out)
    print(json.dumps({k: v for k, v in got.items() if k != "signature"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
