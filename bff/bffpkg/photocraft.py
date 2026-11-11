# -*- coding: utf-8 -*-
"""真实照片的派生图片：标注图 / 整改前后对比拼版（task-9）

硬约束（与 task-8 同一口径）：
  · 只 **读** photo 表 + data/blobs 里的现场原图；绝不写 photo/doc/records/memory/knowledge。
  · 派生结果只落 data/assets/<pid>/<assetId>（asset 表 origin=derived，status=committed）。
  · 必须带 sourceRefs=["photo:<id>"]、producer、model、prompt_hash；禁用外链/base64/本地路径。
  · 不自动生成无源图片：照片不存在/不属于本项目 → 4xx，不产生资产。
  · 角标「派生图·基于现场照片」+ 来源水印，避免把派生图当现场原图。
"""
import hashlib
import io
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import assetsvc
from .core import BLOB_ROOT, q
from .biz import guard_project

MAX_SRC_PIXELS = 60_000_000        # 防解压炸弹
MAX_OUT_SIDE = 3000                # 输出长边上限
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_BOXES = 20
BADGE_TEXT = "派生图·基于现场照片"

_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FONT_CACHE = {}

BOX_COLORS = [(230, 60, 60), (245, 170, 30), (50, 140, 230), (60, 180, 120),
              (200, 80, 200), (90, 90, 90)]


def _font(size, bold=False):
    size = max(10, int(size))
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    cands = _FONT_CANDIDATES if not bold else (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ) + _FONT_CANDIDATES
    for path in cands:
        if os.path.isfile(path):
            try:
                f = ImageFont.truetype(path, size, index=0)
                _FONT_CACHE[key] = f
                return f
            except Exception as e:
                sys.stderr.write("[photo] 字体加载失败 %s: %r\n" % (path, e))
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _safe_text(s, limit=60):
    """水印/标题文本：去控制字符，拒绝外链/本地路径/base64。"""
    if not isinstance(s, str):
        return ""
    s = "".join(ch for ch in s if ch >= " " or ch == "\n").strip()
    if assetsvc._unsafe_text(s):
        return ""
    return assetsvc._clean_text(s, limit)


def _prompt_hash(payload):
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(("photo-craft-v1|" + blob).encode("utf-8")).hexdigest()


def _load_photo(project_id, photo_id):
    """读现场原图（只读）。返回 (PIL.Image, photo_row)。"""
    rows = q("SELECT id, project_id, sha256, blob_path, place, shot_at, created_at "
             "FROM photo WHERE id=%s", (photo_id,))
    if not rows or str(rows[0].get("project_id")) != str(project_id):
        raise assetsvc.AssetError(404, "照片不存在或不属于该项目")
    row = rows[0]
    sha = row.get("sha256") or ""
    path = row.get("blob_path") or os.path.join(BLOB_ROOT, sha[:2], sha)
    real = os.path.realpath(path)
    blob_root = os.path.realpath(BLOB_ROOT)
    if not real.startswith(blob_root + os.sep) or not os.path.isfile(real):
        raise assetsvc.AssetError(404, "照片原图缺失")
    try:
        with open(real, "rb") as f:
            data = f.read()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise assetsvc.AssetError(400, "照片无法解析: %r" % (e,))
    if img.width * img.height > MAX_SRC_PIXELS:
        raise assetsvc.AssetError(413, "原图像素过大")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img, row


def _source_line(row):
    """来源水印用：现场照片 #id · 日期 · 部位。"""
    bits = ["现场照片 #%s" % row["id"]]
    at = str(row.get("shot_at") or row.get("created_at") or "")[:16]
    if at:
        bits.append(at)
    place = (row.get("place") or "").strip()
    if place:
        bits.append(_safe_text(place, 30))
    return " · ".join([b for b in bits if b])


def _draw_badge(img, text=BADGE_TEXT):
    draw = ImageDraw.Draw(img, "RGBA")
    size = max(16, min(44, img.width // 32))
    f = _font(size, bold=True)
    bbox = draw.textbbox((0, 0), text, font=f)
    pad = max(6, size // 3)
    w = bbox[2] - bbox[0] + pad * 2
    h = bbox[3] - bbox[1] + pad * 2
    draw.rectangle([pad, pad, pad + w, pad + h], fill=(0, 0, 0, 165))
    draw.text((pad + pad // 2, pad + pad // 2 - bbox[1]), text, font=f,
              fill=(255, 255, 255, 245))
    return img


def _draw_bottom_text(img, text):
    if not text:
        return img
    draw = ImageDraw.Draw(img, "RGBA")
    size = max(14, min(36, img.width // 42))
    f = _font(size)
    bbox = draw.textbbox((0, 0), text, font=f)
    pad = max(6, size // 2)
    h = bbox[3] - bbox[1] + pad * 2
    draw.rectangle([0, img.height - h, img.width, img.height], fill=(0, 0, 0, 150))
    draw.text((pad, img.height - h + pad - bbox[1]), text, font=f,
              fill=(255, 255, 255, 250))
    return img


def _fit(img):
    """输出长边上限（只缩不放）。"""
    if max(img.width, img.height) <= MAX_OUT_SIDE:
        return img
    ratio = MAX_OUT_SIDE / float(max(img.width, img.height))
    return img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                      Image.LANCZOS)


def _save_and_commit(project_id, data, user, origin, producer, caption, source_refs,
                     task_id, message_id, prompt_hash):
    """派生图落独立资产并立即 committed；返回 (code, payload)。"""
    code, payload = assetsvc.create_asset(
        data, project_id, user=user, mime="image/png", origin=origin,
        producer=producer, caption=caption, source_refs=source_refs,
        task_id=task_id, message_id=message_id, model="pillow",
        prompt_hash=prompt_hash)
    if code != 200:
        return code, payload
    aid = ((payload.get("data") or {}) or {}).get("assetId")
    if not aid:
        return 500, {"code": 500, "message": "派生图未签发"}
    code2, payload2 = assetsvc.commit_asset(aid, user=user, task_id=task_id,
                                            message_id=message_id)
    if code2 != 200:
        return code2, payload2
    row = assetsvc._row(aid)
    block = assetsvc.block_for_asset(aid, "image_ref", caption=caption)
    return 200, {"code": 0, "data": {
        "asset": assetsvc.public_row(row),
        "block": block,
        "fallbackText": caption or ("派生图已生成" if origin != "uploaded" else "图片"),
    }}


# ---------------------------------------------------------------- annotate_photo
def _norm_boxes(boxes, w, h):
    """校验/裁剪标注框；返回 [(x1,y1,x2,y2,label,raw_index)]；全无效抛 400。

    兼容两类模型输出：
      · boxes: [{x,y,w,h}]，像素坐标
      · annotations: [{x1,y1,x2,y2}]，可为 0..1 归一化
    """
    if boxes in (None, ""):
        return []
    if not isinstance(boxes, list):
        raise assetsvc.AssetError(400, "boxes 必须是数组")
    if len(boxes) > MAX_BOXES:
        raise assetsvc.AssetError(400, "标注框最多 %d 个" % MAX_BOXES)
    out = []
    for i, b in enumerate(boxes):
        if not isinstance(b, dict):
            continue
        try:
            if any(k in b for k in ("x1", "y1", "x2", "y2")):
                x1 = float(b.get("x1")); y1 = float(b.get("y1"))
                x2 = float(b.get("x2")); y2 = float(b.get("y2"))
                unit = bool(b.get("normalized")) or (
                    all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2))
                )
                if unit:
                    x1, y1, x2, y2 = x1 * w, y1 * h, x2 * w, y2 * h
                if x2 <= x1 or y2 <= y1:
                    continue
            else:
                x = float(b.get("x")); y = float(b.get("y"))
                bw = float(b.get("w")); bh = float(b.get("h"))
                if bw <= 0 or bh <= 0:
                    continue
                unit = bool(b.get("normalized")) or (
                    all(0.0 <= v <= 1.0 for v in (x, y, bw, bh)) and
                    x + bw <= 1.0 and y + bh <= 1.0
                )
                if unit:
                    x, y, bw, bh = x * w, y * h, bw * w, bh * h
                x1, y1, x2, y2 = x, y, x + bw, y + bh
        except (TypeError, ValueError):
            continue
        x1 = max(0.0, min(float(w), x1))
        y1 = max(0.0, min(float(h), y1))
        x2 = max(0.0, min(float(w), x2))
        y2 = max(0.0, min(float(h), y2))
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        label = _safe_text(b.get("label") or "", 24)
        out.append((int(x1), int(y1), int(x2), int(y2), label, i))
    if boxes and not out:
        raise assetsvc.AssetError(400, "标注框全部超出图片范围")
    return out


def annotate_photo(project_id, photo_id, boxes, caption=None, user=None,
                   task_id=None, message_id=None):
    """在真实照片上画框/编号/标签 → 派生图资产 + image_ref block。"""
    deny = guard_project(project_id, user)
    if deny:
        return deny
    try:
        img, row = _load_photo(project_id, photo_id)
        w, h = img.size
        norm = _norm_boxes(boxes, w, h)
        draw = ImageDraw.Draw(img, "RGBA")
        line_w = max(2, min(10, int(min(w, h) / 220)))
        num_size = max(16, min(40, int(min(w, h) / 28)))
        nf = _font(num_size, bold=True)
        lf = _font(max(13, min(28, int(min(w, h) / 38))))
        for j, (x1, y1, x2, y2, label, raw_i) in enumerate(norm):
            color = BOX_COLORS[j % len(BOX_COLORS)]
            draw.rectangle([x1, y1, x2, y2], outline=color + (255,), width=line_w)
            # 编号角标
            r = num_size // 2 + 2
            cx, cy = x1 + r, y1 + r
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (235,))
            draw.text((cx - num_size // 4, cy - num_size // 2 - 1), str(j + 1),
                      font=nf, fill=(255, 255, 255, 255))
            # 标签
            if label:
                tb = draw.textbbox((0, 0), label, font=lf)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                ty = y1 - th - 8 if y1 - th - 8 >= 0 else y2 + 4
                tx = min(max(0, x1), w - tw - 6)
                draw.rectangle([tx - 3, ty - 2, tx + tw + 3, ty + th + 2],
                               fill=(0, 0, 0, 150))
                draw.text((tx, ty), label, font=lf, fill=(255, 255, 255, 250))
        text = _safe_text(caption, 80) or ("标注图 · 现场照片 #%s" % row["id"])
        img = _draw_bottom_text(img, text)
        img = _draw_badge(img)
        img = _fit(img)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        if len(data) > MAX_OUTPUT_BYTES:
            raise assetsvc.AssetError(413, "派生图超过 %dMB" % (MAX_OUTPUT_BYTES // (1024 * 1024)))
        ph = _prompt_hash({"op": "annotate", "photoId": photo_id, "boxes": boxes,
                           "caption": caption or ""})
        return _save_and_commit(project_id, data, user, "derived", "annotate",
                                text, ["photo:%s" % row["id"]], task_id, message_id, ph)
    except assetsvc.AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    except Exception as e:
        sys.stderr.write("[photo] annotate 失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "标注图生成失败"}


# ---------------------------------------------------------------- compare_photos
def _label_block(draw, x, y, w, text_lines, size):
    f = _font(size)
    lh = size + 6
    total = lh * len(text_lines) + 8
    draw.rectangle([x, y, x + w, y + total], fill=(255, 255, 255, 255),
                   outline=(200, 200, 200, 255))
    for i, line in enumerate(text_lines):
        draw.text((x + 6, y + 4 + i * lh - 1), line, font=f, fill=(30, 30, 30, 255))
    return total


def compare_photos(project_id, before_photo_id, after_photo_id, title=None,
                   before_label="整改前", after_label="整改后", user=None,
                   task_id=None, message_id=None):
    """整改前后对比拼版（两张真实照片）→ 派生图 + image_ref block。"""
    deny = guard_project(project_id, user)
    if deny:
        return deny
    if str(before_photo_id) == str(after_photo_id):
        return 400, {"code": 400, "message": "不能拿同一张照片做前后对比"}
    try:
        before, brow = _load_photo(project_id, before_photo_id)
        after, arow = _load_photo(project_id, after_photo_id)
        # 统一高度（只缩不放），再拼
        target_h = min(1200, max(before.height, after.height))
        def _scale(im):
            if im.height <= target_h:
                return im
            r = target_h / float(im.height)
            return im.resize((max(1, int(im.width * r)), target_h), Image.LANCZOS)
        before, after = _scale(before), _scale(after)
        gap, margin = 16, 20
        title_txt = _safe_text(title, 80) or "整改前后对比"
        fs = 26
        label_h = fs + 6
        top = fs * 2 + 16
        canvas_w = margin * 2 + before.width + gap + after.width
        canvas_h = top + max(before.height, after.height) + label_h * 3 + margin
        canvas = Image.new("RGB", (canvas_w, canvas_h), (245, 245, 245))
        draw = ImageDraw.Draw(canvas, "RGBA")
        tf = _font(fs, bold=True)
        draw.text((margin, 8), title_txt, font=tf, fill=(20, 20, 20, 255))
        xb, xa = margin, margin + before.width + gap
        yb = top
        canvas.paste(before, (xb, yb))
        canvas.paste(after, (xa, yb))
        # 两侧来源水印（画在图上）
        wf = _font(max(12, min(24, min(before.width, after.width) // 28)))
        for im, x, row in ((before, xb, brow), (after, xa, arow)):
            line = _source_line(row)
            tb = draw.textbbox((0, 0), line, font=wf)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            pad = 6
            px = x + im.width - tw - pad * 2 - 4
            py = yb + im.height - th - pad * 2 - 4
            draw.rectangle([px, py, px + tw + pad * 2, py + th + pad * 2],
                           fill=(0, 0, 0, 150))
            draw.text((px + pad, py + pad - tb[1]), line, font=wf,
                      fill=(255, 255, 255, 245))
        # 下方标签：整改前/后 + 日期/部位
        fs2 = 20
        bl = _safe_text(before_label, 12) or "整改前"
        al = _safe_text(after_label, 12) or "整改后"
        b2 = [bl] + ([str(brow.get("shot_at") or "")[:16]] if brow.get("shot_at") else [])
        a2 = [al] + ([str(arow.get("shot_at") or "")[:16]] if arow.get("shot_at") else [])
        _label_block(draw, xb, yb + before.height + 6, before.width, b2, fs2)
        _label_block(draw, xa, yb + after.height + 6, after.width, a2, fs2)
        canvas = _draw_badge(canvas)
        canvas = _fit(canvas)
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        if len(data) > MAX_OUTPUT_BYTES:
            raise assetsvc.AssetError(413, "对比图超过 %dMB" % (MAX_OUTPUT_BYTES // (1024 * 1024)))
        ph = _prompt_hash({"op": "compare", "before": before_photo_id,
                           "after": after_photo_id, "title": title_txt,
                           "beforeLabel": bl, "afterLabel": al})
        caption = title_txt
        return _save_and_commit(project_id, data, user, "derived", "compare",
                                caption, ["photo:%s" % brow["id"],
                                          "photo:%s" % arow["id"]],
                                task_id, message_id, ph)
    except assetsvc.AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    except Exception as e:
        sys.stderr.write("[photo] compare 失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "对比图生成失败"}


# ---------------------------------------------------------------- HTTP handlers
def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def handle_annotate(body, user=None):
    pid = (body.get("projectId") or "").strip()
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    photo_id = _int_or_none(body.get("photoId"))
    if not photo_id:
        return 400, {"code": 400, "message": "photoId 必填"}
    return annotate_photo(pid, photo_id, body.get("boxes") or [], caption=body.get("caption"),
                          user=user, task_id=_int_or_none(body.get("taskId")),
                          message_id=_int_or_none(body.get("messageId")))


def handle_compare(body, user=None):
    pid = (body.get("projectId") or "").strip()
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    before = _int_or_none(body.get("beforePhotoId"))
    after = _int_or_none(body.get("afterPhotoId"))
    if not before or not after:
        return 400, {"code": 400, "message": "beforePhotoId / afterPhotoId 必填"}
    return compare_photos(pid, before, after, title=body.get("title"),
                          before_label=body.get("beforeLabel") or "整改前",
                          after_label=body.get("afterLabel") or "整改后",
                          user=user, task_id=_int_or_none(body.get("taskId")),
                          message_id=_int_or_none(body.get("messageId")))
