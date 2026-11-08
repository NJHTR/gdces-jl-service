# -*- coding: utf-8 -*-
"""巡视记录导出（服务端生成 docx，供小程序下载打开）

设计要点：
- 内容只来自数据库里当天真实存在的文书与影像，不编造天气/实测值/签发人
- 表式由省统表原件模板（GD-B-214）保证；这里只整理数据，不排版
- 生成物落在 data/exports/<project>/，用 GET /api/v1/exports/<名称> 取
"""
import io
import json
import os
import re
import time

from .core import ROOT, q, validate_fields

EXPORT_ROOT = os.path.join(ROOT, "data", "exports")

ISSUE_KEYS = ("事由", "隐患描述", "问题描述", "存在问题", "检查内容", "检查事项")
DEADLINE_KEYS = ("整改期限", "期限", "deadline")
CHECK_KINDS = ("监理检查记录", "平行检查记录", "旁站记录", "监理日志")
# 这些词出现在对象名里，说明是自测/压测产物，不是现场记录
NOISE_WORDS = ("测试", "自测", "复现", "接口", "容器", "压力", "CLI", "校验",
               "参数形状", "Java骨架", "工具状态")


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
        r["time"] = str(r.get("created_at") or "")[11:16]
        obj = (r.get("obj") or "")
        # 自测/压测产物不是现场记录，不进巡视记录
        if any(w in obj for w in NOISE_WORDS):
            continue
        out.append(r)
    return out


def _photos_between(project_id, start, end):
    rows = q(
        "SELECT id, sha256, place, caption, note, weather, shot_at, created_at "
        "FROM photo WHERE project_id=%s "
        "AND DATE(COALESCE(shot_at, created_at)) BETWEEN %s AND %s ORDER BY id",
        (project_id, start, end))
    out = []
    for r in rows:
        r = dict(r)
        stamp = str(r.get("shot_at") or r.get("created_at") or "")
        r["date"], r["time"] = stamp[:10], stamp[11:16]
        out.append(r)
    return out


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

    times = sorted([d.get("time") for d in docs if d.get("time")] +
                   [p.get("time") for p in photos if p.get("time")])
    signed = sum(1 for d in docs if d.get("state") == "已签")
    todo = sum(1 for d in docs if d.get("state") == "待补")
    return {
        "date": day,
        "weather": weather,
        "parts": objs,
        "start": times[0] if times else "",
        "end": times[-1] if times else "",
        "times": times,
        "objs": objs,
        "issues": issues,
        "quality": quality,
        "deadlines": deadlines,
        "docs": docs,
        "photos": photos,
        "signed": signed,
        "todo": todo,
    }


# ---------------------------------------------------------------- collect
def collect(project_id, start, end):
    """按天汇总：返回 (项目, 每天一块, 全部文书, 全部影像)。"""
    proj_rows = q("SELECT id, name, code, addr FROM project WHERE id=%s", (project_id,))
    proj = (dict(proj_rows[0]) if proj_rows else {}) or {"id": project_id}
    docs = _docs_between(project_id, start, end)
    photos = _photos_between(project_id, start, end)
    days = sorted({d["date"] for d in docs if d.get("date")} |
                  {p["date"] for p in photos if p.get("date")})
    blocks = []
    for day in days:
        blocks.append(_day_block(
            day,
            [d for d in docs if d.get("date") == day],
            [p for p in photos if p.get("date") == day],
        ))
    return proj, blocks, docs, photos


# ---------------------------------------------------------------- 生成（模板填充）
def _org_of(docs, default="中建八局华南公司"):
    """施工单位：优先取文书里的「受检单位 / 施工单位」，取不到用默认。"""
    counts = {}
    for d in docs:
        f = d.get("fields") or {}
        for k in ("受检单位", "施工单位"):
            v = (f.get(k) or "").strip()
            if v and "待补" not in v:
                counts[v] = counts.get(v, 0) + 1
    if not counts:
        return default
    return max(counts.items(), key=lambda kv: kv[1])[0][:40]


def _norm_part(s):
    """部位名归一：去掉括号说明与空白，用于去重（4#楼东侧外架 / 4#楼东侧外架（待补）算一条）。"""
    t = re.sub(r"[（(][^）)]*[）)]", "", str(s or ""))
    return re.sub(r"\s+", "", t)


def _parts(b):
    seen, out = set(), []
    for p in b.get("parts") or []:
        k = _norm_part(p)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(str(p).strip())
    return out[:8]


def _work_times(b):
    """只取工作时段内的时间（07:00–19:00）；取不到就返回空。
    原始时间是文书创建时间，含凌晨自测稿，直接取 min/max 会出现 00:50 至 23:05。
    """
    ts = [t for t in (b.get("times") or []) if t and "07:00" <= t <= "19:00"]
    if not ts:
        return "", ""
    return ts[0], ts[-1]


def _period(b):
    st, en = _work_times(b)
    span = "%s %s 至 %s" % (b["date"], st or "—", en or "—")
    if b.get("weather"):
        span += "（天气：%s）" % b["weather"]
    return span


def _issue_lines(b, limit=10):
    lines = []
    items = b.get("issues") or []
    for i, it in enumerate(items[:limit], 1):
        lines.append("%d. %s" % (i, str(it)[:180]))
    if len(items) > limit:
        lines.append("另有 %d 项问题，见当日《监理通知单》。" % (len(items) - limit))
    if b.get("deadlines"):
        lines.append("整改期限：%s。" % "、".join(b["deadlines"][:5]))
    if b.get("docs"):
        lines.append("以上问题已向施工单位指出，并下发《监理通知单》%d 份；"
                     "整改完成后报监理复查，整改回复须附整改前后对比影像。" % len(b["docs"]))
    elif lines:
        lines.append("以上问题已向施工单位指出，整改完成后报监理复查。")
    else:
        lines.append("本日巡视记录中未发现需下发《监理通知单》的问题。")
    return lines


def _date_text(day):
    y, m, d = day.split("-")
    return "%s 年 %s 月 %s 日" % (y, m.lstrip("0"), d.lstrip("0"))


def build_patrol_docx(project_id, start, end, inspector="", org=None,
                      operator="广东建设工程监理有限公司"):
    """按省统表 GD-B-214 生成巡视记录：一条记录一张表（模板原件填充）。

    格式由 data/knowledge/templates/docx/16巡视记录.docx 保证；
    这里只负责把数据整理成 slots 需要的字段（project/serial/parts/org/period/
    lines/inspector/date_text），不参与排版。
    """
    from .formfill import build as fill_forms

    proj, blocks, docs, photos = collect(project_id, start, end)
    proj_name = proj.get("name") or project_id

    records = []
    for i, b in enumerate(blocks, 1):
        records.append({
            "project": proj_name,
            "serial": "GD-B-214 %04d" % i,
            "parts": "、".join(_parts(b)) or "（本日无新增文书）",
            "org": org or _org_of(b["docs"]),
            "period": _period(b),
            "lines": _issue_lines(b),
            "inspector": inspector or "",
            "date_text": _date_text(b["date"]),
        })

    if not records:
        # 没有记录也要出一张表，并如实写明"本期间没有记录"，不编内容
        records = [{
            "project": proj_name,
            "serial": "GD-B-214 0001",
            "parts": "（本期间没有可用于巡视记录的文书或影像）",
            "org": org or "—",
            "period": "%s 至 %s" % (start, end),
            "lines": ["本期间没有可汇总的现场记录；出稿或拍照后重新生成。"],
            "inspector": inspector or "",
            "date_text": "—",
        }]

    display = "%s-巡视记录-%s_%s.docx" % (proj_name, start, end)
    url_name = "patrol-%s_%s.docx" % (start.replace("-", ""), end.replace("-", ""))
    dest = os.path.join(EXPORT_ROOT, _safe_name(project_id))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, url_name)

    got = fill_forms("巡视记录", records, path)
    if not got.get("labels_ok"):
        raise RuntimeError("模板校验未通过，拒绝交付")

    with io.open(path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"name": display, "from": start, "to": end,
                   "forms": len(records), "operator": operator}, f, ensure_ascii=False)
    return {"path": path, "name": display, "url_name": url_name,
            "bytes": os.path.getsize(path), "days": len(records),
            "docs": len(docs), "photos": len(photos)}


# ---------------------------------------------------------------- 单份文书 → 表式
def _wrap1(text, width=38):
    return (str(text or "").strip()[:width]) or "（待补）"


def _wrap(text, width=38, limit=14):
    """把长文本折成若干行（表格里一行一段，太长会压到签名行）。"""
    out = []
    for para in str(text or "").split("\n"):
        seg = para.strip()
        while seg and len(out) < limit:
            out.append(seg[:width])
            seg = seg[width:]
        if len(out) >= limit:
            break
    return out or ["（待补）"]


def _cn_date(v):
    """把 2026-11-06 / 2026-11-06 09:30 变成「2026 年 11 月 6 日」。"""
    t = str(v or "").strip()
    if len(t) >= 10 and t[4] == "-" and t[7] == "-":
        y, m, d = t[:4], t[5:7], t[8:10]
        return "%s 年 %s 月 %s 日" % (y, m.lstrip("0"), d.lstrip("0"))
    return t or "—"


def _mark_checkbox(path, conclusion):
    """平行检查记录里「经检查□是/□否」按结论勾选。

    只在结论里明确出现「符合/合格」或「不符合/不合格」时才勾；
    含糊就不勾（宁可留空让人手勾，也别替我判断）。
    """
    text = str(conclusion or "")
    if "不符合" in text or "不合格" in text:
        mark, other = "☑否", "□是"
    elif "符合" in text or "合格" in text:
        mark, other = "☑是", "□否"
    else:
        return False
    from docx import Document

    doc = Document(path)
    hit = False
    for tb in doc.tables:
        for row in tb.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if "经检查" not in p.text:
                        continue
                    for r in p.runs:
                        if "□是" in r.text or "□否" in r.text:
                            r.text = r.text.replace("□是", mark if mark.endswith("是") else "□是")
                            r.text = r.text.replace("□否", mark if mark.endswith("否") else "□否")
                            hit = True
    if hit:
        doc.save(path)
    return hit


def _hhmm(v, day):
    """把「9:10」补成「2026-11-06 09:10」；已经带日期的原样返回。"""
    t = str(v or "").strip()
    if not t:
        return ""
    if len(t) >= 10 and t[4] == "-" and t[7] == "-":
        return t
    hh, _, rest = t.partition(":")
    if hh.strip().isdigit():
        return "%s %02d:%s" % (day or "", int(hh.strip()), (rest or "00").strip().zfill(2)[:2])
    return t


def _compose_part(*vals):
    """把「部位」和「关键部位工序」拼起来（模板那一栏就叫"关键部位、关键工序"）。"""
    out = []
    for v in vals:
        t = str(v or "").strip()
        if t and "待补" not in t and t not in out:
            out.append(t)
    return " · ".join(out)


def build_form_docx(project_id, doc_id):
    """把一份已入库的文书套进对应省统表模板，生成单份 docx。

    表式差异全部由 <表名>.fields.json 描述，这里只做字段搬运：
      · 普通槽：data[key] = fields[key]
      · block 槽：lines_key="施工情况lines" → 从 fields["施工情况"] 折行
    """
    from .formfill import build as fill_forms, load_spec

    rows = q("SELECT id, kind, obj, fields, bases, project_id, created_at FROM doc WHERE id=%s",
             (doc_id,))
    if not rows:
        raise RuntimeError("文书不存在: %s" % doc_id)
    d = dict(rows[0])
    if d["project_id"] != project_id:
        raise PermissionError("文书不属于该项目")
    fields = _loads(d.get("fields"))
    kind = d.get("kind") or ""
    # 「监理检查记录」在库里是独立 kind，但省统表对应的是 15 平行检查记录。
    # 这里做字段搬运，不新建另一套模板；否则用户点导出永远提示"没接模板"。
    form_kind = kind
    if kind == "监理检查记录":
        raw_bases = _loads(d.get("bases"))
        basis_parts = []
        for b in (raw_bases or []):
            if isinstance(b, dict):
                v = (b.get("quote") or b.get("text") or b.get("ref") or "").strip()
            else:
                v = str(b or "").strip()
            if v:
                basis_parts.append(v.replace("standard://", ""))
        mapped = dict(fields)
        mapped.update({
            "检查时间": fields.get("检查日期") or fields.get("检查时间") or "",
            "检查部位": fields.get("检查部位") or fields.get("部位") or "",
            "检查项目": fields.get("检查事项") or fields.get("检查内容") or "",
            "检查方法": fields.get("检查方法") or "",
            "检查依据": "；".join(basis_parts) or fields.get("依据") or fields.get("检查依据") or "",
            "检查记录": fields.get("检查情况") or fields.get("检查记录") or "",
            "检查结论": fields.get("检查结论") or "",
            "处理记录": fields.get("处理意见") or fields.get("处理记录") or fields.get("备注") or "",
            "检查人": fields.get("检查人") or fields.get("监理人员") or "",
            "复核人": fields.get("复核人") or "",
        })
        fields = mapped
        form_kind = "平行检查记录"
    spec = load_spec(form_kind)          # 没有 fields.json 会抛错（这也是"未接入"的明确信号）
    proj_rows = q("SELECT name FROM project WHERE id=%s", (project_id,))
    proj_name = (proj_rows[0]["name"] if proj_rows else project_id)

    created = str(d.get("created_at") or "")[:10]
    data = dict(fields)
    data["project"] = proj_name
    data["serial"] = "%s %04d" % (spec.get("code") or "", doc_id)

    # 部位 + 关键部位工序 拼成一栏（模板就叫"关键部位、关键工序"）
    if "关键部位工序" in (fields or {}):
        data["关键部位工序"] = _compose_part(fields.get("部位"), fields.get("关键部位工序"))

    # 时间：字段里可能只有「9:10」，用文书创建日期补全
    day = created or (_cn_date(fields.get("日期") or "") or "")
    if fields.get("开始时间"):
        data["开始时间"] = _hhmm(fields.get("开始时间"), created)
    if fields.get("结束时间"):
        data["结束时间"] = _hhmm(fields.get("结束时间"), created)
    data["date_text"] = _cn_date(fields.get("日期") or created or fields.get("开始时间") or "")

    # 监理通知单：把「内容」那一段按条目拼出来（模板里是一整格文字）
    if kind == "监理通知单":
        body = []
        if (fields.get("部位") or "").strip():
            body.append("工程部位：%s" % fields["部位"].strip())
        for key in ("问题描述", "事由", "整改要求"):
            v = (fields.get(key) or "").strip()
            if v and v not in body:
                body.append(("%s：%s" % (key, v)) if key == "整改要求" else v)
        if (fields.get("整改期限") or "").strip():
            body.append("整改期限：%s" % fields["整改期限"].strip())
        if (fields.get("依据") or "").strip():
            body.append("依据：%s" % fields["依据"].strip())
        data["内容lines"] = [_wrap1(x) for x in body][:12] or ["（待补）"]

    # block 槽：把对应字段折行
    for slot in spec.get("slots") or []:
        if slot.get("kind") != "block":
            continue
        key = slot.get("lines_key") or "lines"
        src = key[:-5] if key.endswith("lines") else key
        data[key] = _wrap(fields.get(src) or "")

    # 模板里可能引用库里没有的字段（签发人/受检单位/事由……）。不编内容，
    # 但必须给空串，否则 str.format 直接 KeyError，整张表导不出来。
    for slot in spec.get("slots") or []:
        for tpl in ("text",):
            for name in re.findall(r"\{([^{}]+)\}", slot.get(tpl) or ""):
                if not isinstance(data.get(name), str):
                    data[name] = ""

    display = "%s-%s-%s.docx" % (proj_name, kind, d.get("obj") or doc_id)
    # URL 必须纯 ASCII：带中文时 urllib / wx.downloadFile 都会在请求行编码失败（踩过两次）
    url_name = "form-doc%s.docx" % doc_id
    dest = os.path.join(EXPORT_ROOT, _safe_name(project_id))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, url_name)

    got = fill_forms(form_kind, [data], path)
    if not got.get("labels_ok"):
        raise RuntimeError("模板校验未通过，拒绝交付")
    # 勾选框：只有结论明确时才勾（不做模糊判断）
    if form_kind == "平行检查记录":
        _mark_checkbox(path, fields.get("检查结论"))
    with io.open(path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"name": display, "docId": doc_id, "kind": kind}, f, ensure_ascii=False)
    return {"path": path, "name": display, "url_name": url_name,
            "bytes": os.path.getsize(path), "docId": doc_id, "kind": kind}


# ---------------------------------------------------------------- 按天出表（监理日志等）
def _supervision_lines(b, limit=8):
    """当日监理工作：按文书类型汇总 + 影像张数（只陈述做了什么，不评价）。"""
    kinds = {}
    for d in b.get("docs") or []:
        k = (d.get("kind") or "文书").strip()
        kinds[k] = kinds.get(k, 0) + 1
    lines = []
    for k, n in kinds.items():
        lines.append("审阅/签发《%s》%d 份。" % (k, n))
    photos = len(b.get("photos") or [])
    if photos:
        lines.append("现场影像 %d 张已归档（含部位/时间/天气水印）。" % photos)
    if not lines:
        lines.append("本日无新增文书或影像；现场情况未记录。")
    return lines[:limit]


def _temp_of(b):
    """气温：影像里若有「29℃」这类就取，取不到留空（不编）。"""
    for p in b.get("photos") or []:
        cap = " " + (p.get("caption") or "") + " " + (p.get("weather") or "")
        for i, ch in enumerate(cap):
            if ch == "℃":
                j = i - 1
                while j >= 0 and (cap[j].isdigit() or cap[j] in "-~. "):
                    j -= 1
                seg = cap[j + 1:i].strip()
                if seg:
                    return seg
    return ""


def build_daily_docx(kind, project_id, start, end, inspector=""):
    """按天出表（监理日志这类"一天一张"的表）。数据与巡视记录同一套 collect。"""
    from .formfill import build as fill_forms, load_spec

    spec = load_spec(kind)
    proj, blocks, docs, photos = collect(project_id, start, end)
    proj_name = proj.get("name") or project_id

    records = []
    for i, b in enumerate(blocks, 1):
        y, m, d = b["date"].split("-")
        records.append({
            "project": proj_name,
            "serial": "%s %04d" % (spec.get("code") or "", i),
            "日期": "%s 年 %s 月 %s 日" % (y, m.lstrip("0"), d.lstrip("0")),
            "气温": _temp_of(b),
            "天气": b.get("weather") or "",
            "施工进展lines": _parts(b) or ["（本日无新增文书）"],
            "监理工作lines": _supervision_lines(b),
            "问题处理lines": _issue_lines(b, limit=8),
            "其他lines": ["现场影像与文书已归档，无其他事项。"] if (b.get("photos") or b.get("docs"))
                          else ["无。"],
            "记录人": inspector or "",
            "审阅人": "",
            "审阅日期": "",
        })

    if not records:
        records = [{
            "project": proj_name, "serial": "%s 0001" % (spec.get("code") or ""),
            "日期": "%s 至 %s" % (start, end), "气温": "", "天气": "",
            "施工进展lines": ["本期间没有可用于监理日志的文书或影像。"],
            "监理工作lines": ["无记录。"], "问题处理lines": ["无记录。"],
            "其他lines": ["无。"], "记录人": inspector or "", "审阅人": "", "审阅日期": "",
        }]

    code = (spec.get("code") or "form").replace("-", "").lower()
    display = "%s-%s-%s_%s.docx" % (proj_name, kind, start, end)
    url_name = "%s-%s_%s.docx" % (code, start.replace("-", ""), end.replace("-", ""))
    dest = os.path.join(EXPORT_ROOT, _safe_name(project_id))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, url_name)

    got = fill_forms(kind, records, path)
    if not got.get("labels_ok"):
        raise RuntimeError("模板校验未通过，拒绝交付")
    with io.open(path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"name": display, "kind": kind, "from": start, "to": end,
                   "forms": len(records)}, f, ensure_ascii=False)
    return {"path": path, "name": display, "url_name": url_name,
            "bytes": os.path.getsize(path), "days": len(records)}
