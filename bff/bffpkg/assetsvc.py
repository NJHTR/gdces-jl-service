# -*- coding: utf-8 -*-
"""多模态资产服务（Phase1 MVP 基座）

职责边界（硬约束，别在这里长歪）：
  · 资产只存 `data/assets/<project_id>/<asset_id>`；**绝不**写 data/blobs、photos、docs、
    records、memory、knowledge 等证据/知识位置。
  · 生成物不进驻 photo 表；真实照片只做「引用」（origin=uploaded, sourceRefs=['photo:<id>']），
    资产里存的是去 EXIF 后的展示副本，现场原图仍在 blobs/photo，不动它。
  · 模型不输出字节/base64/本地路径/外链：块只带 assetId/photoId 这类引用，url 由服务端生成。

表 `asset` 由 ensure_asset_table() 幂等创建（服务启动时调；部署不传 db/）。
生命周期：staged(24h) → committed；generated/derived 90 天后过期回收；软删除保留审计。
"""
import hashlib
import hmac
import io
import json
import os
import re
import sys
import time

from .core import AUTH_REQUIRED, BLOB_ROOT, ENV, ROOT, exec_sql, q, q1, similar
from .biz import audit, guard_project

ASSET_ROOT = os.path.join(ROOT, "data", "assets")

# 白名单：只收这些 mime；svg/html/js 不进白名单，天然拒绝。
IMAGE_MIMES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
AUDIO_MIMES = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav"}
FILE_MIMES = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
ALLOWED_MIMES = {}
ALLOWED_MIMES.update(IMAGE_MIMES)
ALLOWED_MIMES.update(AUDIO_MIMES)
ALLOWED_MIMES.update(FILE_MIMES)

ORIGINS = ("uploaded", "derived", "generated")
STATUSES = ("staged", "committed", "quarantined", "deleted")

MAX_IMAGE = 15 * 1024 * 1024
MAX_AUDIO = 20 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
STAGED_TTL = 24 * 3600           # 24h
GENERATED_TTL = 90 * 24 * 3600   # 90 天

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 短签 URL：小程序 <image> 不能自定义 header，只能把签名放查询参数。
# 默认 1 小时有效；读取历史时用 resign_answer() 重新签，库里存的是旧签名也不怕。
ASSET_URL_TTL = int(os.environ.get("ASSET_URL_TTL", "3600"))

# 单轮块上限（task-8 §4）
BLOCK_CAPS = {"text": 6, "image_ref": 4, "file_ref": 2, "card": 3, "audio_ref": 1}
BLOCK_TYPES = ("text", "image_ref", "file_ref", "card")


_MARKUP_HINTS = (b"<svg", b"<html", b"<script", b"<?xml", b"<!doctype html",
                 b"javascript:")
_GENERIC_MIMES = ("", "application/octet-stream", "binary/octet-stream",
                  "multipart/form-data", "application/x-www-form-urlencoded")


def _looks_markup(data):
    head = (data or b"")[:2048].lower()
    return any(h in head for h in _MARKUP_HINTS)


class AssetError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- DDL / ensure
ASSET_DDL = """
CREATE TABLE IF NOT EXISTS asset (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  asset_id     CHAR(26) NOT NULL COMMENT 'ULID；对外 id，也是 data/assets/<pid>/ 下的文件名',
  project_id   VARCHAR(32) NOT NULL,
  user_id      BIGINT UNSIGNED NULL COMMENT '创建者；uploaded 引用是引用发起人',
  task_id      BIGINT UNSIGNED NULL,
  message_id   BIGINT UNSIGNED NULL,
  kind         ENUM('image','audio','file') NOT NULL,
  mime         VARCHAR(96) NOT NULL,
  size         INT UNSIGNED NOT NULL,
  sha256       CHAR(64) NOT NULL COMMENT '去元数据后的字节 sha',
  origin       ENUM('uploaded','derived','generated') NOT NULL
               COMMENT 'uploaded=引用现场照片/用户上传；derived=加工；generated=生成',
  producer     VARCHAR(32) NOT NULL DEFAULT 'bff' COMMENT 'photo_ref / annotate / compose / tool 名',
  model        VARCHAR(64) NULL,
  prompt_hash  CHAR(64) NULL,
  source_refs  JSON NULL COMMENT '来源引用，如 ["photo:55","asset:<ulid>"]',
  caption      VARCHAR(300) NULL,
  is_stored    TINYINT NOT NULL DEFAULT 1 COMMENT '1=字节在 data/assets；0=仅外部引用（保留）',
  path         VARCHAR(255) NULL COMMENT 'data/assets 下的相对路径',
  status       ENUM('staged','committed','quarantined','deleted') NOT NULL DEFAULT 'staged',
  expires_at   DATETIME NULL,
  deleted_at   DATETIME NULL,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_asset_id (asset_id),
  KEY idx_asset_project_status (project_id, status),
  KEY idx_asset_message (message_id),
  KEY idx_asset_expires (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='多模态资产（独立 data/assets；pending 阶段 staged）'
"""

_ENSURE_LOCK = None
_ENSURED = False
_last_gc = 0.0


def _lock():
    global _ENSURE_LOCK
    if _ENSURE_LOCK is None:
        import threading
        _ENSURE_LOCK = threading.Lock()
    return _ENSURE_LOCK


def ensure_asset_table(force=False):
    """幂等建表（服务启动时调；handler 兜底）；失败不抛，由调用方决定 500。"""
    global _ENSURED
    if _ENSURED and not force:
        return True
    with _lock():
        if _ENSURED and not force:
            return True
        try:
            q("SELECT 1")
            exec_sql(ASSET_DDL)
            _ENSURED = True
            try:
                gc_assets()
            except Exception as e:
                sys.stderr.write("[asset] 启动 GC 失败: %r\n" % (e,))
            return True
        except Exception as e:
            sys.stderr.write("[asset] 建表失败: %r\n" % (e,))
            return False


def maybe_gc(interval=600):
    """定时惰性 GC（每次请求最多 10 分钟一次），避免生成物/临时资产无限堆。"""
    global _last_gc
    now = time.time()
    if now - _last_gc < interval:
        return 0
    _last_gc = now
    return gc_assets()


# ---------------------------------------------------------------- id / mime
def new_ulid():
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    n = (ts << 80) | rnd
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 31])
        n >>= 5
    return "".join(reversed(out))


# ---------------------------------------------------------------- 短签 URL
def _url_secret():
    """签名密钥：优先 ASSET_URL_SECRET；没有就用 MYSQL_PASSWORD 派生，避免新增必填配置。"""
    base = ENV.get("ASSET_URL_SECRET") or ENV.get("MYSQL_PASSWORD") or "jl-asset-dev"
    return hashlib.sha256(("asset-url-v1:" + str(base)).encode("utf-8")).digest()


def _sign(asset_id, exp, dl):
    msg = "%s|%d|%d" % (asset_id, int(exp), 1 if dl else 0)
    return hmac.new(_url_secret(), msg.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def sign_asset_url(asset_id, dl=False, ttl=None, now=None):
    """给小程序 <image>/下载用的短签 URL；不需要也不接受 Bearer header。"""
    ttl = ASSET_URL_TTL if ttl is None else int(ttl)
    exp = int(now if now is not None else time.time()) + ttl
    sig = _sign(asset_id, exp, dl)
    return ("/api/v1/assets/%s/content?e=%d&s=%s%s"
            % (asset_id, exp, sig, "&dl=1" if dl else ""))


def verify_asset_sig(asset_id, exp, sig, dl=False, now=None):
    if not ULID_RE.match(str(asset_id or "")) or not sig:
        return False
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    now = int(now if now is not None else time.time())
    if exp < now:
        return False
    try:
        return hmac.compare_digest(_sign(asset_id, exp, dl), str(sig))
    except Exception:
        return False


def is_content_path(path):
    return bool(re.match(r"^/api/v1/assets/[0-9A-HJKMNP-TV-Z]{26}/content$", path or ""))


def resign_blocks(blocks):
    """读取历史回答时重签 URL（库里存的是签发时的旧签名，短 TTL 会过期）。"""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        aid = b.get("assetId")
        if not aid or b.get("type") not in ("image_ref", "file_ref"):
            continue
        b["url"] = sign_asset_url(aid, False)
        if b.get("type") == "file_ref":
            b["downloadUrl"] = sign_asset_url(aid, True)
    return blocks


def resign_answer(answer):
    if isinstance(answer, dict) and isinstance(answer.get("blocks"), list):
        resign_blocks(answer["blocks"])
    return answer


def detect_mime(data):
    """按文件头认类型；认不出返回 None（不信任 Content-Type）。"""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "audio/mpeg"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    try:
        text = data.decode("utf-8")
        if "\x00" not in text:
            return "text/plain"
    except UnicodeDecodeError:
        return None
    return None


def _kind_of(mime):
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def _size_limit(kind):
    return {"image": MAX_IMAGE, "audio": MAX_AUDIO, "file": MAX_FILE}.get(kind, MAX_FILE)


# ---------------------------------------------------------------- 去元数据
def _sanitize_image(data):
    """用 Pillow 重编码：EXIF/GPS/XMP/IPTC 全部丢掉；方向先摆正。"""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise AssetError(400, "图片无法解析: %r" % (e,))
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)                       # 动图只保留首帧（MVP）
    fmt = (img.format or "").upper()
    img = ImageOps.exif_transpose(img)
    out = io.BytesIO()
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue(), "image/jpeg"
    if fmt == "PNG":
        img.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    if fmt == "WEBP":
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.save(out, "WEBP", quality=90, method=4)
        return out.getvalue(), "image/webp"
    raise AssetError(400, "不支持的图片格式: %s" % (fmt or "?"))


def _sanitize_audio(data):
    """mp3 去掉 ID3v2（可能含歌手/定位/自定义标签）；其它音频保持原样。"""
    if data[:3] == b"ID3" and len(data) > 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
               ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        body = data[10 + size:]
        if body:
            return body
    return data


def sanitize_bytes(mime, data):
    """返回 (干净字节, 实际 mime)。图片必过 Pillow；音频去 ID3；其它原样。"""
    if mime in IMAGE_MIMES:
        clean, real_mime = _sanitize_image(data)
        return clean, real_mime
    if mime in AUDIO_MIMES:
        return _sanitize_audio(data), mime
    return data, mime


# ---------------------------------------------------------------- 存储
def _project_dir(project_id):
    pid = str(project_id or "")
    if not PID_RE.match(pid):
        raise AssetError(400, "projectId 不合法")
    d = os.path.join(ASSET_ROOT, pid)
    real = os.path.realpath(d)
    if not real.startswith(os.path.realpath(ASSET_ROOT) + os.sep):
        raise AssetError(400, "资产路径越界")
    return real


def _asset_path(project_id, asset_id):
    if not ULID_RE.match(str(asset_id or "")):
        raise AssetError(404, "资产不存在")
    d = _project_dir(project_id)
    p = os.path.join(d, asset_id)
    real = os.path.realpath(p)
    if not real.startswith(d + os.sep):
        raise AssetError(404, "资产不存在")
    return p


def _write_bytes(project_id, asset_id, data):
    path = _asset_path(project_id, asset_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return os.path.relpath(path, ROOT)


def _row(asset_id):
    rows = q("SELECT * FROM asset WHERE asset_id=%s", (str(asset_id or ""),))
    return rows[0] if rows else None


def _validate_link(project_id, task_id, message_id):
    """task/message 若给了，必须和项目一致，避免跨项目挂资产。"""
    if task_id:
        rows = q("SELECT project_id FROM task WHERE id=%s", (int(task_id),))
        if not rows or str(rows[0]["project_id"]) != str(project_id):
            raise AssetError(400, "taskId 不属于该项目")
    if message_id:
        rows = q("SELECT project_id FROM message WHERE id=%s", (int(message_id),))
        if not rows or str(rows[0]["project_id"]) != str(project_id):
            raise AssetError(400, "messageId 不属于该项目")


# ---------------------------------------------------------------- 创建/签发
def _create_common(project_id, user_id, data, mime, origin, producer, caption=None,
                   source_refs=None, task_id=None, message_id=None, model=None,
                   prompt_hash=None):
    if origin not in ORIGINS:
        raise AssetError(400, "origin 只能是 uploaded/derived/generated")
    mime = (mime or "").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ALLOWED_MIMES:
        # 显式声明了不在白名单的 mime（svg/html/js…）→ 直接拒绝，不做嗅探兜底。
        if mime and mime not in _GENERIC_MIMES:
            raise AssetError(400, "不允许的 mime: %s" % mime)
        # 原始字节上传时 Content-Type 常是 application/octet-stream / multipart；
        # 按文件头嗅探一次，白名单之外仍拒绝。
        sniffed = detect_mime(data)
        if sniffed == "text/plain" and _looks_markup(data):
            raise AssetError(400, "拒绝 svg/html/js 内容")
        mime = sniffed or ""
    if mime not in ALLOWED_MIMES:
        raise AssetError(400, "不允许的 mime: %s" % (mime or "?"))
    if mime == "text/plain" and _looks_markup(data):
        raise AssetError(400, "拒绝 svg/html/js 内容")
    kind = _kind_of(mime)
    if not data:
        raise AssetError(400, "空文件")
    if len(data) > _size_limit(kind):
        raise AssetError(413, "文件超过 %dMB" % (_size_limit(kind) // (1024 * 1024)))
    real = detect_mime(data)
    if real != mime and not (mime == "text/plain" and real == "text/plain"):
        raise AssetError(400, "内容与 mime 不符（声明 %s，实际 %s）" % (mime, real or "无法识别"))
    _validate_link(project_id, task_id, message_id)
    try:
        clean, real_mime = sanitize_bytes(mime, data)
    except AssetError:
        raise
    except Exception as e:
        raise AssetError(400, "资产清洗失败: %r" % (e,))
    if not clean:
        raise AssetError(400, "清洗后为空")
    if len(clean) > _size_limit(kind):
        raise AssetError(413, "清洗后仍超过大小上限")
    sha = hashlib.sha256(clean).hexdigest()
    aid = new_ulid()
    rel = _write_bytes(project_id, aid, clean)
    expires_sql = "DATE_ADD(NOW(), INTERVAL %d HOUR)" % (STAGED_TTL // 3600)
    exec_sql(
        "INSERT INTO asset (asset_id, project_id, user_id, task_id, message_id, kind, mime, "
        "size, sha256, origin, producer, model, prompt_hash, source_refs, caption, is_stored, "
        "path, status, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,"
        "'staged'," + expires_sql + ")",
        (aid, str(project_id), user_id, task_id, message_id, kind, real_mime, len(clean),
         sha, origin, (producer or "bff")[:32], (model or None), (prompt_hash or None),
         json.dumps(source_refs or [], ensure_ascii=False), (caption or None), rel))
    audit(str(project_id), (user_id or 1), "asset.create", "asset:%s" % aid,
          "%s %s %s %dB" % (kind, real_mime, origin, len(clean)))
    return _row(aid)


def create_asset(data, project_id, user=None, mime=None, origin="generated",
                 producer="bff", caption=None, source_refs=None, task_id=None,
                 message_id=None, model=None, prompt_hash=None):
    """签发生成/派生资产。返回 HTTP 风格 (code, payload)。"""
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    deny = guard_project(project_id, user)
    if deny:
        return deny
    try:
        row = _create_common(str(project_id), (user or {}).get("id"), data, mime, origin,
                             producer, caption=caption, source_refs=source_refs,
                             task_id=task_id, message_id=message_id, model=model,
                             prompt_hash=prompt_hash)
    except AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    except Exception as e:
        sys.stderr.write("[asset] 创建失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "资产创建失败"}
    return 200, {"code": 0, "data": public_row(row)}


def create_asset_from_photo(project_id, photo_id, user=None, caption=None,
                            task_id=None, message_id=None, producer="photo_ref"):
    """真实照片引用：读原图 → 去 EXIF 副本 → 独立资产。不动 photot/blob/records。"""
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    deny = guard_project(project_id, user)
    if deny:
        return deny
    rows = q("SELECT id, project_id, sha256, blob_path FROM photo WHERE id=%s", (photo_id,))
    if not rows or str(rows[0]["project_id"]) != str(project_id):
        return 404, {"code": 404, "message": "照片不存在或不属于该项目"}
    photo = rows[0]
    sha = photo.get("sha256") or ""
    path = photo.get("blob_path") or os.path.join(BLOB_ROOT, sha[:2], sha)
    real = os.path.realpath(path)
    blob_root = os.path.realpath(BLOB_ROOT)
    if not real.startswith(blob_root + os.sep) or not os.path.isfile(real):
        return 404, {"code": 404, "message": "照片原图缺失"}
    try:
        with open(real, "rb") as f:
            raw = f.read()
        mime = detect_mime(raw)
        if mime not in IMAGE_MIMES:
            return 400, {"code": 400, "message": "照片不是支持的图片格式"}
        row = _create_common(str(project_id), (user or {}).get("id"), raw, mime,
                             "uploaded", producer, caption=caption,
                             source_refs=["photo:%s" % photo["id"]],
                             task_id=task_id, message_id=message_id)
    except AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    except Exception as e:
        sys.stderr.write("[asset] 照片引用失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "照片引用失败"}
    return 200, {"code": 0, "data": public_row(row)}


# ---------------------------------------------------------------- 读取/提交/删除
def public_row(row):
    if not row:
        return None
    refs = row.get("source_refs")
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except ValueError:
            refs = []
    aid = row["asset_id"]
    view = {
        "assetId": aid,
        "projectId": row["project_id"],
        "kind": row.get("kind"),
        "mime": row.get("mime"),
        "size": int(row.get("size") or 0),
        "sha256": row.get("sha256"),
        "origin": row.get("origin"),
        "status": row.get("status"),
        "caption": row.get("caption"),
        "sourceRefs": refs or [],
        # 短签 URL：小程序 <image> 直接能加载（不需要 custom header）
        "url": sign_asset_url(aid, False),
        "createdAt": str(row.get("created_at") or ""),
        "expiresAt": str(row.get("expires_at") or "") or None,
    }
    if row.get("kind") == "file":
        view["downloadUrl"] = sign_asset_url(aid, True)
    return view


def _guard_row(row, user):
    """项目权限：必须是该项目成员；删除/隔离的资产对外一律 404。"""
    if not row:
        return 404, {"code": 404, "message": "资产不存在"}
    if row.get("status") in ("deleted", "quarantined"):
        return 404, {"code": 404, "message": "资产不存在"}
    deny = guard_project(row.get("project_id"), user)
    if deny:
        return deny
    return None


def get_asset_meta(asset_id, user=None):
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    row = _row(asset_id)
    deny = _guard_row(row, user)
    if deny:
        return deny
    return 200, {"code": 0, "data": public_row(row)}


def get_asset_content(asset_id, user=None, exp=None, sig=None, dl=False):
    """短期访问端点。两条路：
      1) 有效短签（?e=&s=[&dl=1]）→ 放行（小程序 <image> 不能带 header）；
      2) 没有/无效签名 → 走正常项目权限（Bearer token）。
    """
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    maybe_gc()
    row = _row(asset_id)
    if not row or row.get("status") in ("deleted", "quarantined"):
        return 404, {"code": 404, "message": "资产不存在"}
    signed_ok = verify_asset_sig(row["asset_id"], exp, sig, dl=dl)
    if not signed_ok:
        deny = _guard_row(row, user)
        if deny:
            return deny
    if not row.get("path"):
        return 404, {"code": 404, "message": "资产内容不存在"}
    try:
        path = _asset_path(row["project_id"], row["asset_id"])
    except AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    if not os.path.isfile(path):
        return 404, {"code": 404, "message": "资产内容不存在"}
    with open(path, "rb") as f:
        data = f.read()
    return 200, data, row["mime"], bool(dl)


def commit_asset(asset_id, user=None, message_id=None, task_id=None):
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    row = _row(asset_id)
    deny = _guard_row(row, user)
    if deny:
        return deny
    if row.get("status") == "committed":
        return 200, {"code": 0, "data": public_row(row)}
    if row.get("status") != "staged":
        return 409, {"code": 409, "message": "资产状态不可提交（%s）" % row.get("status")}
    try:
        if task_id:
            _validate_link(row["project_id"], task_id, None)
        if message_id:
            _validate_link(row["project_id"], None, message_id)
    except AssetError as e:
        return e.code, {"code": e.code, "message": e.message}
    if row.get("origin") in ("generated", "derived"):
        expires = "DATE_ADD(NOW(), INTERVAL %d DAY)" % (GENERATED_TTL // 86400)
    else:
        expires = "NULL"
    exec_sql(
        "UPDATE asset SET status='committed', expires_at=" + expires + ", "
        "message_id=COALESCE(%s, message_id), task_id=COALESCE(%s, task_id) "
        "WHERE asset_id=%s",
        (message_id, task_id, row["asset_id"]))
    audit(row["project_id"], (user or {}).get("id") or 1, "asset.commit",
          "asset:%s" % row["asset_id"], row.get("origin") or "")
    return 200, {"code": 0, "data": public_row(_row(row["asset_id"]))}


def delete_asset(asset_id, user=None):
    """软删除：status=deleted + 删字节；审计保留。"""
    if not ensure_asset_table():
        return 500, {"code": 500, "message": "资产表还没就绪"}
    row = _row(asset_id)
    deny = _guard_row(row, user)
    if deny:
        return deny
    exec_sql("UPDATE asset SET status='deleted', deleted_at=NOW(3) WHERE asset_id=%s",
             (row["asset_id"],))
    try:
        os.remove(_asset_path(row["project_id"], row["asset_id"]))
    except OSError as e:
        sys.stderr.write("[asset] 删除字节失败（已软删）%s: %r\n" % (row["asset_id"], e))
    audit(row["project_id"], (user or {}).get("id") or 1, "asset.delete",
          "asset:%s" % row["asset_id"], row.get("origin") or "")
    return 200, {"code": 0, "data": {"assetId": row["asset_id"], "status": "deleted"}}


# ---------------------------------------------------------------- GC
def gc_assets(now=None):
    """staged 过期（24h）与 generated/derived 过期（90d）→ 软删除 + 删字节。"""
    try:
        rows = q("SELECT asset_id, project_id, path, status, origin FROM asset "
                 "WHERE (status='staged' AND expires_at IS NOT NULL AND expires_at < NOW()) "
                 "   OR (status='committed' AND origin IN ('generated','derived') "
                 "       AND expires_at IS NOT NULL AND expires_at < NOW()) LIMIT 200")
    except Exception:
        return 0
    n = 0
    for r in rows:
        try:
            exec_sql("UPDATE asset SET status='deleted', deleted_at=NOW(3) WHERE asset_id=%s",
                     (r["asset_id"],))
            try:
                os.remove(_asset_path(r["project_id"], r["asset_id"]))
            except OSError as e:
                sys.stderr.write("[asset] GC 删字节失败 %s: %r\n" % (r["asset_id"], e))
            audit(r["project_id"], 1, "asset.gc", "asset:%s" % r["asset_id"],
                  "%s/%s 过期回收" % (r["status"], r["origin"]))
            n += 1
        except Exception as e:
            sys.stderr.write("[asset] GC 失败 %s: %r\n" % (r.get("asset_id"), e))
    if n:
        sys.stderr.write("[asset] GC 回收 %d 个资产\n" % n)
    return n


# ---------------------------------------------------------------- answer.blocks v1
# 块只带引用（assetId/photoId/docId）与展示信息；url 由服务端生成。
# 模型/工具若写 data:base64、本地绝对路径、外链 → 整块丢弃，保留文字 fallback。
_BASE64_HINT = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,", re.I)
_LOCAL_PATH = re.compile(r"(^|[\s\"'(:])(/home/|/tmp/|/var/|/etc/|file://|[A-Za-z]:[\\/])")
_EXTERNAL_URL = re.compile(r"https?://", re.I)
_LONG_B64 = re.compile(r"^[A-Za-z0-9+/=]{400,}$")
_HAS_SPACE = re.compile(r"\s")


def _unsafe_text(s):
    if not isinstance(s, str):
        return True
    s = s.strip()
    if not s:
        return False
    if _BASE64_HINT.search(s) or _LOCAL_PATH.search(s) or _EXTERNAL_URL.search(s):
        return True
    # 整段疑似 base64（长、无空格）也拒绝
    if len(s) > 400 and not _HAS_SPACE.search(s) and _LONG_B64.match(s):
        return True
    return False


def _clean_text(s, limit=1000):
    if not isinstance(s, str):
        return ""
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s.strip())
    return s[:limit]


def _source_refs(row):
    refs = row.get("source_refs")
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except ValueError:
            refs = []
    return refs or []


def _json_safe(value, depth=0):
    """card.data 只允许 JSON 标量/对象/数组；字符串按 _unsafe_text 过滤。"""
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return None if _unsafe_text(value) else _clean_text(value, 500)
    if isinstance(value, list):
        return [v for v in (_json_safe(x, depth + 1) for x in value) if v is not None][:20]
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:20]:
            if not isinstance(k, str) or _unsafe_text(k) or len(k) > 40:
                continue
            vv = _json_safe(v, depth + 1)
            if vv is not None:
                out[k] = vv
        return out
    return None


def _block_from_asset(row, btype, caption):
    bid = row["asset_id"]
    block = {
        "type": btype,
        "assetId": bid,
        "caption": _clean_text(caption or row.get("caption") or "", 300),
        # origin 一定给；前端缺省时显示中性"附件"
        "origin": row.get("origin") or "generated",
        "mime": row.get("mime"),
        "sourceRefs": _source_refs(row),
        # 短签 URL：小程序 <image> 直接加载，不走 Bearer header
        "url": sign_asset_url(bid, False),
    }
    if btype == "file_ref":
        block["downloadUrl"] = sign_asset_url(bid, True)
        if row.get("caption"):
            block["name"] = _clean_text(row.get("caption"), 120)
    return block


def block_for_asset(asset_id, btype="image_ref", caption=None):
    """给内部工具（如标注/拼版）把已 commit 的资产转成 answer.blocks 里的块。"""
    row = _row(asset_id)
    if not row or row.get("status") in ("deleted", "quarantined"):
        return None
    return _block_from_asset(row, btype, caption=caption)


def normalize_blocks(project_id, user_id, raw_blocks, fallback_text="", task_id=None):
    """把工具/Agent 写出的 blocks 校验成 answer.blocks。

    规则：
      · 未知类型丢弃；assetId 不属于本项目/不存在/已删除 → 丢弃该块（文字 fallback 仍在）；
      · image_ref 可直接带 photoId（真实照片引用）：服务端建 uploaded 资产，不复制证据；
      · url 一律由服务端生成；模型给的外链/base64/本地路径 → 丢弃；
      · 单轮上限 4 图 / 1 音（预留）/ 2 文件 / 3 卡 / 6 文字；
      · staged 资产在挂上回答时自动 commit（90d/上传引用长效）。
    """
    if not raw_blocks:
        return []
    if not ensure_asset_table():
        return []
    if isinstance(raw_blocks, dict):
        raw_blocks = raw_blocks.get("blocks") or []
    out = []
    counts = {k: 0 for k in BLOCK_CAPS}
    for b in list(raw_blocks)[:24]:
        if not isinstance(b, dict):
            continue
        btype = (b.get("type") or "").strip()
        if btype not in BLOCK_TYPES:
            continue
        if counts.get(btype, 0) >= BLOCK_CAPS.get(btype, 0):
            continue
        if btype == "text":
            text = _clean_text(b.get("text"), 1000)
            if not text or _unsafe_text(text):
                continue
            out.append({"type": "text", "text": text})
            counts["text"] += 1
            continue
        if btype in ("image_ref", "file_ref"):
            want_kind = "image" if btype == "image_ref" else "file"
            row = None
            aid = str(b.get("assetId") or "").strip()
            photo_id = b.get("photoId")
            try:
                if aid and ULID_RE.match(aid):
                    row = _row(aid)
                    if row and (str(row.get("project_id")) != str(project_id)
                                or row.get("status") in ("deleted", "quarantined")
                                or row.get("kind") != want_kind):
                        row = None
                elif photo_id and btype == "image_ref":
                    got, payload = create_asset_from_photo(
                        project_id, int(photo_id), user={"id": user_id} if user_id else None,
                        caption=b.get("caption"), producer="photo_ref")
                    row = (payload.get("data") or {}) if got == 200 else None
                    if row:
                        row = _row(row.get("assetId"))
            except (AssetError, ValueError, TypeError) as e:
                sys.stderr.write("[asset] blocks 引用校验失败: %r\n" % (e,))
                row = None
            if not row:
                continue
            if row.get("status") == "staged":
                commit_asset(row["asset_id"],
                             user={"id": user_id} if user_id else None, task_id=task_id)
                row = _row(row["asset_id"]) or row
            caption = b.get("caption")
            if _unsafe_text(caption if isinstance(caption, str) else ""):
                caption = ""
            out.append(_block_from_asset(row, btype, caption))
            counts[btype] += 1
            continue
        if btype == "card":
            doc_id = b.get("docId")
            if doc_id not in (None, ""):
                try:
                    rows = q("SELECT id, project_id, kind, obj FROM doc WHERE id=%s",
                             (int(doc_id),))
                except (ValueError, TypeError):
                    rows = []
                if not rows or str(rows[0]["project_id"]) != str(project_id):
                    continue                  # 他项目 docId → 丢弃
                doc_id = int(doc_id)
            title = _clean_text(b.get("title") or b.get("name") or "", 120)
            if _unsafe_text(title):
                title = ""
            data = _json_safe(b.get("data") or {}) or {}
            # card 只读：actions 只留 open/readonly；deep link 指向本服务资源
            actions = []
            for a in (b.get("actions") or [])[:5]:
                if isinstance(a, dict) and (a.get("type") or "") in ("open", "readonly"):
                    actions.append({"type": a["type"]})
            card = {"type": "card", "docId": doc_id, "title": title,
                    "data": data, "actions": actions or [{"type": "readonly"}]}
            if doc_id:
                card["url"] = "/api/v1/docs/%s" % doc_id
            out.append(card)
            counts["card"] += 1
            continue
    if out and not any(x.get("type") == "text" for x in out):
        # 每条回答至少保留 text fallback
        fb = _clean_text(fallback_text, 1000)
        if fb and not _unsafe_text(fb):
            out.insert(0, {"type": "text", "text": fb})
    return out


def attach_message(blocks, message_id):
    """把块里引用的资产挂到 message 上（便于审计/回查）。"""
    for b in blocks or []:
        aid = b.get("assetId")
        if not aid:
            continue
        try:
            exec_sql("UPDATE asset SET message_id=%s WHERE asset_id=%s", (message_id, aid))
        except Exception as e:
            sys.stderr.write("[asset] 挂 message 失败 %s: %r\n" % (aid, e))


# ---------------------------------------------------------------- HTTP handlers
def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _list_or_none(v):
    if v in (None, ""):
        return None
    if isinstance(v, list):
        return v
    try:
        got = json.loads(v)
        return got if isinstance(got, list) else [str(got)]
    except ValueError:
        return [x.strip() for x in str(v).split(",") if x.strip()]


def handle_asset_issue(params, data, user=None):
    """POST /api/v1/assets（原始字节或 multipart）：签发生成/派生资产。"""
    pid = (q1(params, "projectId") or "").strip()
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    mime = q1(params, "mime") or ""
    if not mime:
        # 原始字节上传时 Content-Type 放在 params["contentType"]（api.py 合并）
        mime = q1(params, "contentType") or ""
    origin = (q1(params, "origin") or "generated").strip()
    return create_asset(
        data, pid, user=user, mime=mime, origin=origin,
        producer=(q1(params, "producer") or "bff").strip(),
        caption=(q1(params, "caption") or "").strip() or None,
        source_refs=_list_or_none(q1(params, "sourceRefs")),
        task_id=_int_or_none(q1(params, "taskId")),
        message_id=_int_or_none(q1(params, "messageId")),
        model=q1(params, "model"), prompt_hash=q1(params, "promptHash"))


def handle_asset_create_json(body, user=None):
    """POST /api/v1/assets（JSON）：目前支持真实照片引用（photoId）。"""
    pid = (body.get("projectId") or "").strip()
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    if body.get("photoId") not in (None, ""):
        return create_asset_from_photo(
            pid, _int_or_none(body.get("photoId")), user=user,
            caption=body.get("caption") or None,
            task_id=_int_or_none(body.get("taskId")),
            message_id=_int_or_none(body.get("messageId")),
            producer=(body.get("producer") or "photo_ref")[:32])
    return 400, {"code": 400, "message": "JSON 签发只支持 photoId 引用；生成物请传字节"}


def handle_asset_commit(asset_id, body=None, user=None):
    body = body or {}
    return commit_asset(asset_id, user=user,
                        message_id=_int_or_none(body.get("messageId")),
                        task_id=_int_or_none(body.get("taskId")))
