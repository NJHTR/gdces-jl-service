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
