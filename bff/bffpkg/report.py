# -*- coding: utf-8 -*-
"""巡视记录导出（服务端生成 docx，供小程序下载打开）

设计要点：
- 内容只来自数据库里当天真实存在的文书与影像，不编造天气/实测值/签发人
- 表式按《建设工程监理规范》GB/T 50319 巡视要求 + 常用监表 37 的字段
- 生成物落在 data/exports/<project>/，用 GET /api/v1/exports/<名称> 取
"""
import json
import os
import re
import time

from .core import ROOT, q, validate_fields

EXPORT_ROOT = os.path.join(ROOT, "data", "exports")

ISSUE_KEYS = ("事由", "隐患描述", "问题描述", "存在问题", "检查内容", "检查事项")
DEADLINE_KEYS = ("整改期限", "期限", "deadline")
CHECK_KINDS = ("监理检查记录", "平行检查记录", "旁站记录", "监理日志")


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "-", str(s or "")).strip("-")[:60] or "导出"


def _pick(fields, keys):
    for k in keys:
        v = (fields or {}).get(k)
        if v and str(v).strip() and "待补" not in str(v):
            return str(v).strip()
    return ""


def _loads(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}


def _docs_between(project_id, start, end):
    rows = q(
        "SELECT id, kind, obj, state, fields, bases, path, created_at FROM doc "
        "WHERE project_id=%s AND DATE(created_at) BETWEEN %s AND %s ORDER BY id",
        (project_id, start, end))
    out = []
    for r in rows:
        r = dict(r)
        r["fields"] = _loads(r.get("fields"))
        r["date"] = str(r.get("created_at") or "")[:10]
        out.append(r)
    return out


def _photos_between(project_id, start, end):
    rows = q(
        "SELECT id, sha256, place, caption, note, weather, shot_at, created_at "
        "FROM photo WHERE project_id=%s "
        "AND DATE(COALESCE(shot_at, created_at)) BETWEEN %s AND %s ORDER BY id",
        (project_id, start, end))
    return [dict(r) for r in rows]


def _day_block(day, docs, photos):
    """把一天的数据整理成表里那几行。"""
    objs, issues, quality, deadlines = [], [], [], []
    for d in docs:
        obj = (d.get("obj") or "").strip()
        if obj and obj not in objs:
            objs.append(obj)
        f = d.get("fields") or {}
        if d.get("kind") in CHECK_KINDS:
            text = _pick(f, ("检查内容", "检查事项", "巡视内容"))
            concl = _pick(f, ("检查结论", "巡视结论"))
            if text or concl:
                quality.append((text + ("：" if text and concl else "") + concl).strip("："))
        else:
            it = _pick(f, ISSUE_KEYS)
            if it:
                issues.append(it)
        dl = _pick(f, DEADLINE_KEYS)
        if dl and dl not in deadlines:
            deadlines.append(dl)

    weather = ""
    for p in photos:
        w = (p.get("weather") or "").strip()
        if w:
            weather = w
            break
        cap = (p.get("caption") or "") + " " + (p.get("note") or "")
        for word in ("晴", "多云", "阴", "小雨", "中雨", "大雨", "阵雨", "雷阵雨", "雪"):
            if word in cap:
                weather = word
                break
        if weather:
            break

    signed = sum(1 for d in docs if d.get("state") == "已签")
    todo = sum(1 for d in docs if d.get("state") == "待补")
    return {
        "date": day,
        "weather": weather,
        "objs": objs,
        "issues": issues,
        "quality": quality,
        "deadlines": deadlines,
        "docs": docs,
        "photos": photos,
        "signed": signed,
        "todo": todo,
    }


# ---------------------------------------------------------------- docx 排版
def _cn(run, font, size, bold=False, color=None):
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = font
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def _para(doc, text="", size=10.5, bold=False, font="宋体", space_after=4, align=None):
    from docx.shared import Pt

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.35
    if align is not None:
        p.alignment = align
    if text:
        _cn(p.add_run(text), font, size, bold)
    return p


def _cell(cell, text, bold=False, size=9.5):
    from docx.shared import Pt

    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.25
    _cn(p.add_run(str(text or "")), "宋体", size, bold)


def _table(doc, rows, widths=None, header=False):
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Cm

    t = doc.add_table(rows=0, cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for ci, val in enumerate(row):
            _cell(cells[ci], val, bold=(header and ri == 0) or (ci == 0 and not header))
    if widths:
        for ci, w in enumerate(widths):
            for row in t.rows:
                row.cells[ci].width = Cm(w)
    return t


def build_patrol_docx(project_id, start, end, inspector="", org="广东建设工程监理有限公司"):
    """生成巡视记录 docx，返回 {path, name, bytes}。"""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    proj = (q("SELECT id, name, code, addr FROM project WHERE id=%s", (project_id,)) or [{}])[0]
    docs = _docs_between(project_id, start, end)
    photos = _photos_between(project_id, start, end)

    days = sorted({d["date"] for d in docs} | {str(p.get("shot_at") or p.get("created_at") or "")[:10]
                                               for p in photos})
    blocks = [_day_block(day, [d for d in docs if d["date"] == day],
                         [p for p in photos
                          if str(p.get("shot_at") or p.get("created_at") or "")[:10] == day])
              for day in days]

    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)

    _para(doc, "监 理 巡 视 记 录", size=20, bold=True, font="黑体",
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _para(doc, org, size=10.5, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    _para(doc, "记录期间：%s — %s      编号：JL-XS-%s" % (start, end, start.replace("-", "")),
          size=9, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)

    _table(doc, [
        ["工程名称", "%s（%s）" % (proj.get("name") or project_id, proj.get("code") or "—"),
         "监理单位", org],
        ["项目编号", proj.get("code") or "—", "记录人", inspector or "—"],
        ["记录日期", "%s 至 %s" % (start, end), "数据来源", "本项目已出具的文书与现场影像"],
        ["表式依据", "《建设工程监理规范》GB/T 50319 巡视要求；常用《巡视记录》表式（监表 37）", "", ""],
    ], widths=[2.4, 6.4, 2.2, 5.6])

    if not blocks:
        _para(doc, "", space_after=8)
        _para(doc, "本期间内没有可用于巡视记录的文书或影像。", size=11, bold=True)
        _para(doc, "说明：生成器只汇总已存在的记录，不会凭空补写现场情况。"
                   "请先在小程序里出稿或拍照，再重新生成。", size=9.5, color=(0x66, 0x66, 0x66))

    for idx, b in enumerate(blocks, 1):
        _para(doc, "", space_after=6)
        _para(doc, "%s、%s" % ("一二三四五六七八九十"[idx - 1] if idx <= 10 else idx,
                               b["date"]), size=12, bold=True, font="黑体")
        _para(doc, "天气：%s　|　当日文书 %d 份（已签 %d、待补 %d）　|　现场影像 %d 张"
              % (b["weather"] or "未记录", len(b["docs"]), b["signed"], b["todo"], len(b["photos"])),
              size=9, space_after=4)

        objs = b["objs"]
        issues = b["issues"]
        quality = b["quality"]
        _table(doc, [
            ["巡视范围\n及主要部位",
             "、".join(objs[:14]) + ("等" if len(objs) > 14 else "") if objs else "—"],
            ["主要施工情况",
             "当日依据项目记录：%s" % ("；".join(objs[:8]) if objs else "无新增文书")],
            ["质量、安全\n等情况",
             "质量：%s\n安全及其他问题：%s" % (
                 "；".join(quality[:6]) if quality else "未记录质量检查结论",
                 "；".join(issues[:10]) if issues else "未记录问题")],
            ["发现问题及\n处理情况",
             "共记录问题 %d 项。%s%s" % (
                 len(issues),
                 ("已下发文书 %d 份（已签 %d 份）。" % (len(b["docs"]), b["signed"])) if b["docs"] else "",
                 ("整改期限：%s。" % "、".join(b["deadlines"][:5])) if b["deadlines"] else "")],
            ["巡视人", inspector or "—"],
        ], widths=[3.0, 13.4])

    # 汇总
    if blocks:
        _para(doc, "", space_after=8)
        _para(doc, "%s、本期汇总" % ("一二三四五六七八九十"[len(blocks)] if len(blocks) <= 10 else ""),
              size=12, bold=True, font="黑体")
        rows = [["日期", "巡视部位", "问题数", "文书数", "状态"]]
        for b in blocks:
            rows.append([b["date"],
                         "、".join(b["objs"][:4]) + ("等" if len(b["objs"]) > 4 else ""),
                         str(len(b["issues"])), str(len(b["docs"])),
                         "已签 %d / 待补 %d" % (b["signed"], b["todo"])])
        _table(doc, rows, widths=[2.6, 7.6, 2.0, 2.0, 3.0], header=True)

    # 附表
    if docs:
        _para(doc, "", space_after=6)
        _para(doc, "附表　本期文书索引", size=11, bold=True, font="黑体")
        rows = [["文书号", "类型", "对象", "状态"]]
        for d in docs[:40]:
            rows.append([str(d["id"]), d.get("kind") or "", (d.get("obj") or "—")[:28],
                         d.get("state") or ""])
        _table(doc, rows, widths=[2.2, 3.6, 9.0, 2.4], header=True)

    _para(doc, "", space_after=4)
    for line in [
        "说明：",
        "1. 本表依据 GB/T 50319 巡视要求编制；记录内容取自本项目当日已出具的文书与现场影像，可逐条追溯。",
        "2. 天气、实测值、签发人等现场未取得的数据按“未记录 / 待补”保留，不回填补写。",
        "3. 整改要求以已下发《监理通知单》为准，整改回复须附整改前后对比影像。",
    ]:
        _para(doc, line, size=9, space_after=2)

    name = "%s-巡视记录-%s_%s.docx" % (_safe_name(proj.get("name") or project_id), start, end)
    dest = os.path.join(EXPORT_ROOT, _safe_name(project_id))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, name)
    doc.save(path)
    return {"path": path, "name": name, "bytes": os.path.getsize(path),
            "days": len(blocks), "docs": len(docs), "photos": len(photos)}
