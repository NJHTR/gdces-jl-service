# -*- coding: utf-8 -*-
"""Phase 1 · 记忆候选（memory_candidate）

设计口径（对应任务 Phase 1，别在这里自作主张扩张）：
  · 候选只是"待确认"，pending 永远不写 memory、不渲染 records/rules.md、不进提示。
  · 三种 scope：
      user          跨项目个人偏好/书写习惯  → memory(scope=user, project_id=NULL)
      project_user  个人 × 本项目习惯        → memory(scope=user, project_id=<pid>)
      project       项目全体规矩             → memory(scope=project)，且只能总监/owner/admin 接受
  · 候选由服务端/Agent 写入，前端只能看和接受/拒绝；接受才落 memory，再重渲染文件视图。
  · 表由 ensure_memory_candidate_table() 幂等创建（服务启动时调一次），
    不依赖 migrations/scripts 先跑；db/ 里另有 DDL 便于全新环境与审计。

为什么单独一个模块：Phase 2（个人域摘要 / 项目域只重渲染）也读 memory_candidate 的
accepted 状态；把候选生命周期集中在一处，避免又散回 agent.py/biz.py 各处。
"""
import os
import sys
import threading

from .core import AUTH_REQUIRED, exec_sql, llm, parse_step, q, q1, similar
from .biz import (
    audit,
    guard_project,
    render_rules_md,
    render_user_memory,
    render_user_project_md,
)
from .memorysum import render_user_summary

SCOPES = ("user", "project_user", "project")
SCOPE_LABELS = {
    "user": "我的习惯",
    "project_user": "本项目习惯",
    "project": "项目规矩",
}
STATUSES = ("pending", "accepted", "rejected")

MAX_TEXT = 512
MAX_EVIDENCE = 4000

# 线上已有 memory 表的 source 是 ENUM('manual','auto','learned','confirmed','correction')。
# 人点了"接受"，语义就是 confirmed（也算"人写的"，记忆指标里 humanWrites 就是这么算的）。
ACCEPT_SOURCE = "confirmed"


# ---------------------------------------------------------------- DDL / ensure
MEMORY_CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS memory_candidate (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  project_id        VARCHAR(32) NOT NULL COMMENT '发起/归属项目（user 候选也记来源项目，便于项目内待确认列表）',
  user_id           VARCHAR(32) NOT NULL COMMENT '候选归属人（个人域）；project 候选是提交人',
  scope             ENUM('user','project_user','project') NOT NULL
                    COMMENT 'user=跨项目个人 / project_user=个人×本项目 / project=项目共享',
  text              VARCHAR(512) NOT NULL,
  evidence          TEXT NULL COMMENT '为什么提这条（用户原话/字段改动摘录）',
  source_task_id    BIGINT UNSIGNED NULL,
  source_message_id BIGINT UNSIGNED NULL,
  status            ENUM('pending','accepted','rejected') NOT NULL DEFAULT 'pending'
                    COMMENT 'pending 永远不进 memory / 文件',
  decided_by        VARCHAR(32) NULL,
  decided_at        DATETIME(3) NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_cand_pending (project_id, scope, status),
  KEY idx_cand_user (user_id, status),
  KEY idx_cand_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='记忆候选（pending 不生效；接受后才写 memory 并重渲染文件）'
"""

_ENSURE_LOCK = threading.Lock()
_ENSURED = False


def ensure_memory_candidate_table(force=False):
    """幂等建表。服务启动调用一次；handler 里也会兜底调。

    为什么不用 migrations 脚本建：部署脚本只传 bff/scripts/schemas/tools，不传 db/，
    线上也不能手改；放在服务自己身上才能保证"重启/换机都不丢表、不漏建"。
    失败不抛（不能让建表把 BFF 启动搞挂），由调用方决定是否 500。
    """
    global _ENSURED
    if _ENSURED and not force:
        return True
    with _ENSURE_LOCK:
        if _ENSURED and not force:
            return True
        try:
            q("SELECT 1")  # 连接可用性先探一下，错误信息更直白
            exec_sql(MEMORY_CANDIDATE_DDL)
            _ENSURED = True
            return True
        except Exception as e:  # pragma: no cover - 依赖线上 DB
            sys.stderr.write("[memcand] 建表失败: %r\n" % (e,))
            return False


# ---------------------------------------------------------------- 小工具
def _actor_id(user, fallback_text=""):
    """audit_log.actor_id 是 BIGINT；没有登录用户时沿用旧约定记 1。"""
    uid = (user or {}).get("id")
    if uid is not None:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    try:
        return int(str(fallback_text))
    except (TypeError, ValueError):
        return 1


def _who(user):
    return str((user or {}).get("id") or "")


def _candidate_view(r):
    """返回给前端的字段统一 camelCase（跟其它接口一致）。"""
    decided_at = r.get("decided_at")
    created_at = r.get("created_at")
    return {
        "id": r["id"],
        "projectId": r.get("project_id"),
        "userId": r.get("user_id"),
        "scope": r.get("scope"),
        "scopeLabel": SCOPE_LABELS.get(r.get("scope") or "", r.get("scope") or ""),
        "text": r.get("text") or "",
        "evidence": r.get("evidence"),
        "sourceTaskId": r.get("source_task_id"),
        "sourceMessageId": r.get("source_message_id"),
        "status": r.get("status"),
        "decidedBy": r.get("decided_by"),
        "decidedAt": str(decided_at) if decided_at else None,
        "createdAt": str(created_at) if created_at else None,
    }


def _member_project_ids(user):
    rows = q("SELECT project_id FROM project_member WHERE user_id=%s", (user["id"],))
    return [r["project_id"] for r in rows]


def can_confirm_project_candidate(project_id, user):
    """项目全域候选能不能被这个人接受。

    口径与既有 rule_proposal 一致：只有项目 owner/admin/总监可以确认；普通成员只能提。
    演示模式（AUTH_REQUIRED=0）没有登录用户时放行，让本地/预览能点通；
    **登录用户存在时仍按项目角色判**，所以普通工程师点接受会拿到 403（小程序提示需总监确认）。
    这是对"项目候选 accepted 只能总监确认"的落地，同时不破坏现有演示模式。
    """
    if not user:
        return not AUTH_REQUIRED
    rows = q("SELECT role FROM project_member WHERE project_id=%s AND user_id=%s",
             (project_id, user["id"]))
    role = (rows[0].get("role") or "").strip().lower() if rows else ""
    if role:
        return role in ("owner", "admin", "总监", "项目总监")
    # 演示模式下没有成员记录：放行（否则预览环境没人能确认）；鉴权模式下 guard_project 已挡掉非成员
    return not AUTH_REQUIRED


def _can_decide_personal(candidate, user):
    """user / project_user 候选只有本人能接受或拒绝。"""
    if not user:
        return not AUTH_REQUIRED          # 演示模式/服务端内部调用
    return str(candidate.get("user_id") or "") == str(user.get("id"))


# ---------------------------------------------------------------- POST /memory-candidates
def handle_candidate_create(body, user=None):
    """收下一条候选。只写 memory_candidate（status=pending），绝不写 memory/rules.md。"""
    if not ensure_memory_candidate_table():
        return 500, {"code": 500, "message": "记忆候选表还没就绪，请稍后再试"}

    pid = (body.get("projectId") or "").strip()
    scope = (body.get("scope") or "").strip()
    text = (body.get("text") or "").strip()
    if not pid:
        return 400, {"code": 400, "message": "projectId 必填"}
    if scope not in SCOPES:
        return 400, {"code": 400, "message": "scope 只能是 user / project_user / project"}
    if not text:
        return 400, {"code": 400, "message": "text 必填"}
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]

    body_uid = (str(body.get("userId") or "")).strip()
    if not body_uid:
        body_uid = _who(user)
    if not body_uid:
        return 400, {"code": 400, "message": "userId 必填"}
    # 登录用户只能代表自己提交候选（防止替别人写"个人习惯"）
    if user and str(user.get("id")) != body_uid:
        return 403, {"code": 403, "message": "只能以自己的身份提交记忆候选"}

    deny = guard_project(pid, user)
    if deny:
        return deny

    evidence = (body.get("evidence") or "").strip()
    evidence = evidence[:MAX_EVIDENCE] or None
    task_id = body.get("sourceTaskId")
    msg_id = body.get("sourceMessageId")
    try:
        task_id = int(task_id) if task_id not in (None, "") else None
    except (TypeError, ValueError):
        task_id = None
    try:
        msg_id = int(msg_id) if msg_id not in (None, "") else None
    except (TypeError, ValueError):
        msg_id = None

    # 同一条待确认不重复入队（scope 决定去重维度：个人域按人，项目域按项目）
    try:
        if scope == "project":
            dup = q("SELECT id, status FROM memory_candidate "
                    "WHERE project_id=%s AND scope='project' AND status='pending' AND text=%s "
                    "LIMIT 1", (pid, text))
        else:
            dup = q("SELECT id, status FROM memory_candidate "
                    "WHERE project_id=%s AND user_id=%s AND scope=%s AND status='pending' "
                    "AND text=%s LIMIT 1", (pid, body_uid, scope, text))
        if dup:
            return 200, {"code": 0, "data": {
                "id": dup[0]["id"], "status": dup[0]["status"], "duplicated": True}}
    except Exception as e:
        sys.stderr.write("[memcand] 去重查询失败: %r\n" % (e,))

    try:
        cid = exec_sql(
            "INSERT INTO memory_candidate (project_id, user_id, scope, text, evidence, "
            "source_task_id, source_message_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (pid, body_uid, scope, text, evidence, task_id, msg_id))
    except Exception as e:
        sys.stderr.write("[memcand] 写候选失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "记忆候选没写进去，请重试"}

    audit(pid, _actor_id(user, body_uid), "memory_candidate.create", "candidate:%s" % cid,
          "%s %s" % (scope, text[:80]))
    return 200, {"code": 0, "data": {"id": cid, "status": "pending"}}


# ---------------------------------------------------------------- GET /memory-candidates
def handle_candidate_list(params, user=None):
    """待确认/已决列表。

    可见性（任务里的硬要求）：
      · user / project_user：只能看本人；登录用户传别人的 userId 也看不到
      · project：项目成员可见（鉴权模式下按 project_member；演示模式下项目接口本就放行）
    """
    if not ensure_memory_candidate_table():
        return 500, {"code": 500, "message": "记忆候选表还没就绪，请稍后再试"}

    pid = (q1(params, "projectId") or "").strip()
    status = (q1(params, "status") or "pending").strip()
    uid_param = (q1(params, "userId") or "").strip()
    if status not in STATUSES:
        return 400, {"code": 400, "message": "status 只能是 pending / accepted / rejected"}
    if pid:
        deny = guard_project(pid, user)
        if deny:
            return deny

    where = ["status=%s"]
    args = [status]
    if pid:
        where.append("project_id=%s")
        args.append(pid)

    if user:
        own = str(user.get("id"))
        if uid_param and uid_param != own:
            personal = "1=0"                     # 不能看别人的个人候选
        else:
            personal = "user_id=%s"
            args.append(own)
        if AUTH_REQUIRED:
            project = ("project_id IN (SELECT project_id FROM project_member "
                       "WHERE user_id=%s)")
            args.append(user["id"])
        else:
            project = "1=1"                      # 演示模式项目接口本就放行
    else:
        personal = "user_id=%s" if uid_param else "1=1"
        if uid_param:
            args.append(uid_param)
        project = "1=1"

    where.append("((scope IN ('user','project_user') AND %s) OR (scope='project' AND %s))"
                 % (personal, project))
    sql = ("SELECT id, project_id, user_id, scope, text, evidence, source_task_id, "
           "source_message_id, status, decided_by, decided_at, created_at "
           "FROM memory_candidate WHERE " + " AND ".join(where) +
           " ORDER BY id DESC LIMIT 200")
    try:
        rows = q(sql, tuple(args))
    except Exception as e:
        sys.stderr.write("[memcand] 查候选失败: %r\n" % (e,))
        return 500, {"code": 500, "message": "查询记忆候选失败"}
    return 200, {"code": 0, "data": [_candidate_view(r) for r in rows]}


# ---------------------------------------------------------------- 接受 / 拒绝
def _write_personal_memory(candidate, memory_id_hint=None):
    """user / project_user 候选接受后写 memory（scope=user）。返回 memory id。"""
    uid = str(candidate.get("user_id") or "")
    pid = str(candidate.get("project_id") or "")
    text = (candidate.get("text") or "").strip()[:MAX_TEXT]
    cid = candidate.get("id")
    if candidate["scope"] == "project_user":
        applies_to = "user:%s@project:%s" % (uid, pid)
        memory_project = pid
    else:
        applies_to = "user:%s" % uid
        memory_project = None
    dup = q("SELECT id FROM memory WHERE scope='user' AND owner_id=%s "
            "AND COALESCE(project_id,'')=%s AND text=%s LIMIT 1",
            (uid, memory_project or "", text))
    if dup:
        mid = dup[0]["id"]
        exec_sql("UPDATE memory SET status=1, updated_at=NOW(3) WHERE id=%s", (mid,))
        return mid
    return exec_sql(
        "INSERT INTO memory (scope, owner_id, project_id, text, source, source_ref, "
        "applies_to, updated_at) VALUES ('user',%s,%s,%s,%s,%s,%s,NOW(3))",
        (uid, memory_project, text, ACCEPT_SOURCE,
         "candidate:%s" % cid, applies_to))


def _write_project_memory(candidate):
    """project 候选接受后写 memory（scope=project）。返回 memory id。"""
    pid = str(candidate.get("project_id") or "")
    text = (candidate.get("text") or "").strip()[:MAX_TEXT]
    cid = candidate.get("id")
    dup = q("SELECT id FROM memory WHERE scope='project' AND owner_id=%s AND text=%s LIMIT 1",
            (pid, text))
    if dup:
        mid = dup[0]["id"]
        exec_sql("UPDATE memory SET status=1, updated_at=NOW(3) WHERE id=%s", (mid,))
        return mid
    return exec_sql(
        "INSERT INTO memory (scope, owner_id, project_id, text, source, source_ref, "
        "applies_to, updated_at) VALUES ('project',%s,NULL,%s,%s,%s,%s,NOW(3))",
        (pid, text, ACCEPT_SOURCE, "candidate:%s" % cid, "project:%s" % pid))


def handle_candidate_decide(cid, action, user=None):
    """接受/拒绝一条候选。

    accept：
      · user/project_user → 写 memory(active) + 重渲染个人文件视图
      · project → 只有总监/owner/admin；写 memory(scope=project, source=confirmed) +
        重渲染 records/rules.md + audit
    reject：只改状态，任何文件/记忆都不动；同样写 audit。
    """
    if action not in ("accept", "reject"):
        return 400, {"code": 400, "message": "action 只能是 accept / reject"}
    if not ensure_memory_candidate_table():
        return 500, {"code": 500, "message": "记忆候选表还没就绪，请稍后再试"}

    rows = q("SELECT id, project_id, user_id, scope, text, evidence, status "
             "FROM memory_candidate WHERE id=%s", (cid,))
    if not rows:
        return 404, {"code": 404, "message": "候选不存在"}
    cand = rows[0]
    pid = str(cand.get("project_id") or "")
    deny = guard_project(pid, user)
    if deny:
        return deny
    if cand["status"] != "pending":
        return 409, {"code": 409, "message": "这条候选已经处理过了（%s）" % cand["status"]}

    who = _who(user)
    if action == "reject":
        # 项目候选：项目成员都可以忽略；个人候选：只有本人
        if cand["scope"] in ("user", "project_user") and not _can_decide_personal(cand, user):
            return 403, {"code": 403, "message": "只能处理自己的记忆候选"}
        exec_sql("UPDATE memory_candidate SET status='rejected', decided_by=%s, "
                 "decided_at=NOW(3) WHERE id=%s", (who, cid))
        audit(pid, _actor_id(user, who), "memory_candidate.reject", "candidate:%s" % cid,
              "%s %s" % (cand["scope"], (cand.get("text") or "")[:80]))
        return 200, {"code": 0, "data": {"id": cid, "status": "rejected"}}

    # ---- accept ----
    if cand["scope"] in ("user", "project_user"):
        if not _can_decide_personal(cand, user):
            return 403, {"code": 403, "message": "只能接受自己的记忆候选"}
        mid = _write_personal_memory(cand)
        # 先写库再重渲染：渲染是查表生成的视图
        rendered = render_user_memory(cand["user_id"]) or ""
        note = ""
        if cand["scope"] == "project_user":
            rel = render_user_project_md(cand["user_id"], pid) or ""
            if rel:
                note = rel
        exec_sql("UPDATE memory_candidate SET status='accepted', decided_by=%s, "
                 "decided_at=NOW(3) WHERE id=%s", (who, cid))
        audit(pid, _actor_id(user, who), "memory_candidate.accept", "candidate:%s" % cid,
              "memory:%s %s %s" % (mid, cand["scope"], (cand.get("text") or "")[:80]))
        # Phase2：个人域摘要跟着刷新（确定性、幂等；不写 memory）
        summary = ""
        try:
            summary = render_user_summary(cand["user_id"]) or ""
        except Exception as e:
            sys.stderr.write("[memcand] 刷新个人摘要失败: %r\n" % (e,))
        data = {"id": cid, "status": "accepted", "memoryId": mid, "rendered": rendered}
        if note:
            data["renderedProject"] = note
        if summary:
            data["summary"] = summary
        return 200, {"code": 0, "data": data}

    # project 候选：只能总监/owner/admin 接受
    if not can_confirm_project_candidate(pid, user):
        return 403, {"code": 403,
                     "message": "项目规矩需要总监/项目管理员确认；你提交的会留在待确认里"}
    mid = _write_project_memory(cand)
    exec_sql("UPDATE memory_candidate SET status='accepted', decided_by=%s, "
             "decided_at=NOW(3) WHERE id=%s", (who, cid))
    rel = render_rules_md(pid) or ""
    audit(pid, _actor_id(user, who), "memory_candidate.accept", "candidate:%s" % cid,
          "memory:%s project %s" % (mid, (cand.get("text") or "")[:80]))
    return 200, {"code": 0, "data": {"id": cid, "status": "accepted",
                                     "memoryId": mid, "rendered": rel}}


# ---------------------------------------------------------------- Phase1 抽取钩子
CANDIDATE_EXTRACT_PROMPT = """你在帮监理助手做「记忆候选」抽取。只判断这轮对话里有没有值得以后继续用的东西。

候选只有三种 scope：
- user：这个人跨项目的稳定偏好/书写习惯（例：依据必须写到条号；结论常写“经检查符合要求”）
- project_user：这个人只在这个项目里的习惯或本项目固定写法（例：本项目通知单抬头固定写某公司）
- project：项目全体都要遵守的规矩（例：整改回复必须附整改前后对比照片）。这类只能总监确认，更不能乱提。

不要提：
- 一次性事实（部位、数量、日期、当天天气、这一份文书的具体内容）
- 已有记忆/待确认里已经有的内容
- 推测、常识、规范原文

最多 3 条；没有就返回空数组。**只输出一个 JSON，不要任何多余文字**：
{"candidates":[{"scope":"project_user","text":"以后怎么做的稳定说法","evidence":"用户原话摘录"}]}

【已有生效记忆】
%s

【已有待确认候选】
%s
"""


def memory_extract_enabled():
    """抽取总开关，默认关闭。开着也只写 pending 候选，不会把模型输出当事实。"""
    return os.environ.get("MEMORY_EXTRACT", "0") == "1"


def _existing_texts(project_id, user_id):
    """给抽取做去重参考：这个人/这个项目已有生效记忆。"""
    try:
        rows = q("SELECT text FROM memory WHERE status=1 AND "
                 "((scope='user' AND owner_id=%s) OR (scope='project' AND owner_id=%s)) "
                 "ORDER BY id DESC LIMIT 40", (str(user_id), str(project_id)))
        return [(r.get("text") or "").strip() for r in rows if (r.get("text") or "").strip()]
    except Exception as e:
        sys.stderr.write("[memcand] 读已有记忆失败: %r\n" % (e,))
        return []


def _pending_texts(project_id, user_id):
    try:
        rows = q("SELECT text FROM memory_candidate WHERE status='pending' AND project_id=%s "
                 "AND (scope='project' OR user_id=%s) ORDER BY id DESC LIMIT 40",
                 (str(project_id), str(user_id)))
        return [(r.get("text") or "").strip() for r in rows if (r.get("text") or "").strip()]
    except Exception as e:
        sys.stderr.write("[memcand] 读待确认失败: %r\n" % (e,))
        return []


def extract_memory_candidates(project_id, user_id, user_text, assistant_text, trace=None):
    """任务完成后调用：让模型抽 0~3 条候选，**只写 memory_candidate(pending)**。

    · flag: MEMORY_EXTRACT=1 才跑，默认关闭（线上不打电话、不花钱、不产生候选）。
    · 项目候选也只会 pending，绝不直接 active；接受仍要总监点确认。
    返回本次新增的候选 [{"id","scope","text"}]，用于日志/测试。
    """
    if not memory_extract_enabled():
        return []
    if not ensure_memory_candidate_table():
        return []

    pid = str(project_id or "")
    uid = str(user_id or "")
    if not pid or not uid:
        return []

    tools = "、".join((t or {}).get("tool", "") for t in (trace or []) if isinstance(t, dict)) or "无"
    active = _existing_texts(pid, uid)
    pending = _pending_texts(pid, uid)
    prompt = CANDIDATE_EXTRACT_PROMPT % (
        "\n".join("- %s" % t for t in active[:30]) or "- （无）",
        "\n".join("- %s" % t for t in pending[:30]) or "- （无）",
    )
    convo = ("用户说：%s\n\n助手回：%s\n\n调用的工具：%s"
             % ((user_text or "")[:1500], (assistant_text or "")[:1500], tools))
    try:
        raw = llm([
            {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": convo}]},
        ], max_tokens=500, timeout=60)
    except Exception as e:
        sys.stderr.write("[memcand] 抽取模型调用失败: %r\n" % (e,))
        return []

    obj = parse_step(raw) or {}
    items = obj.get("candidates") or []
    added = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        scope = (it.get("scope") or "").strip()
        text = (it.get("text") or "").strip()
        if scope not in SCOPES or len(text) < 4:
            continue
        text = text[:MAX_TEXT]
        # 模型输出不当事实：与已有生效/待确认相似的直接丢
        pool = active + pending
        if any(similar(text, t) for t in pool):
            continue
        evidence = (it.get("evidence") or "").strip()[:MAX_EVIDENCE] or None
        try:
            cid = exec_sql(
                "INSERT INTO memory_candidate (project_id, user_id, scope, text, evidence) "
                "VALUES (%s,%s,%s,%s,%s)", (pid, uid, scope, text, evidence))
        except Exception as e:
            sys.stderr.write("[memcand] 抽取写候选失败: %r\n" % (e,))
            continue
        pending.append(text)
        added.append({"id": cid, "scope": scope, "text": text})
        audit(pid, _actor_id(None, uid), "memory_candidate.extract", "candidate:%s" % cid,
              "%s %s" % (scope, text[:80]))
    return added
