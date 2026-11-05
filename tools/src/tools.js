// 四个工具的实际实现
import { q, one } from "./db.js";
import {
  readProject, readMemories, safeName, writeWorkspaceFile, gitCommit, excerpt, wsPath, listFiles,
} from "./workspace.js";

/* ---------------- get_project ---------------- */
export async function getProject({ projectId }) {
  const row = await one(
    `SELECT id, name, code, addr, meta, ws_path, status FROM project WHERE id = ?`,
    [projectId]
  );
  const disk = readProject(projectId);
  if (!row) {
    // 库里没有但磁盘有：仍可回答（工作区是事实来源）
    if (!disk.meta || !disk.meta.id) throw new Error(`项目不存在: ${projectId}`);
    return { id: disk.meta.id, name: disk.meta.name, code: disk.meta.code, meta: disk.meta, source: "workspace" };
  }
  return {
    id: row.id,
    name: row.name,
    code: row.code,
    addr: row.addr,
    meta: typeof row.meta === "string" ? JSON.parse(row.meta) : row.meta,
    wsPath: row.ws_path,
    source: "db",
  };
}

/* ---------------- search_records ---------------- */
export async function searchRecords({ projectId, keyword, place, kind, dateFrom, dateTo, limit = 10 }) {
  const where = ["project_id = ?"];
  const args = [projectId];

  if (kind) { where.push("kind = ?"); args.push(kind); }
  if (place) { where.push("obj LIKE ?"); args.push(`%${place}%`); }
  if (dateFrom) { where.push("created_at >= ?"); args.push(dateFrom); }
  if (dateTo) { where.push("created_at <= ?"); args.push(dateTo); }
  if (keyword) {
    where.push("(obj LIKE ? OR kind LIKE ?)");
    args.push(`%${keyword}%`, `%${keyword}%`);
  }

  const rows = await q(
    `SELECT id, kind, obj, state, path, git_commit, created_at, bases
       FROM doc WHERE ${where.join(" AND ")}
      ORDER BY created_at DESC LIMIT ?`,
    [...args, Math.min(Number(limit) || 10, 50)]
  );

  return rows.map((r) => ({
    id: r.id,
    kind: r.kind,
    obj: r.obj,
    state: r.state,
    path: r.path,
    commit: r.git_commit,
    at: r.created_at,
    excerpt: r.path ? excerpt(projectId, r.path) : "",
    bases: typeof r.bases === "string" ? JSON.parse(r.bases || "[]") : r.bases || [],
  }));
}

/* ---------------- search_standards ---------------- */
export async function searchStandards({ query: text, limit = 5 }) {
  if (!text || !String(text).trim()) return [];
  const rows = await q(
    `SELECT code, clause, title, body, version, effective_at,
            ROUND(MATCH(title, body) AGAINST (? IN NATURAL LANGUAGE MODE), 3) AS score
       FROM standard
      HAVING score > 0
      ORDER BY score DESC
      LIMIT ?`,
    [text, Math.min(Number(limit) || 5, 20)]
  );
  return rows;
}

/* ---------------- create_doc_draft ---------------- */
const TEMPLATES = {
  旁站记录: ["部位", "时间", "施工内容", "结论"],
  监理通知单: ["受检单位", "部位", "事由", "整改期限", "签发人"],
  监理日志: ["日期", "天气", "部位", "施工内容", "结论"],
};

function renderDoc({ kind, obj, fields, bases }) {
  const req = TEMPLATES[kind] || Object.keys(fields || {});
  const lines = [
    `# ${kind}${obj ? " · " + obj : ""}`,
    "",
    `> 生成时间：${new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`,
    "",
  ];

  const missing = [];
  for (const k of req) {
    const v = fields?.[k];
    if (v === undefined || v === null || String(v).trim() === "") {
      missing.push(k);
      lines.push(`- **${k}**：待补`);
    } else {
      lines.push(`- **${k}**：${v}`);
    }
  }
  for (const [k, v] of Object.entries(fields || {})) {
    if (!req.includes(k)) lines.push(`- **${k}**：${v}`);
  }

  if (bases && bases.length) {
    lines.push("", "## 依据", "");
    for (const b of bases) {
      const ref = b.ref ? `（${b.ref}）` : "";
      lines.push(`- ${b.text || b.kind}${ref}`);
    }
  }
  return { md: lines.join("\n") + "\n", missing };
}

export async function createDocDraft({ projectId, kind, obj, fields, bases = [], body }) {
  if (!kind) throw new Error("缺 kind");
  const { md, missing } = renderDoc({ kind, obj, fields, bases });

  const stamp = new Date().toISOString().slice(0, 10);
  const base = `${safeName(kind)}-${safeName(obj, 24)}`;
  // 同名文书不覆盖：自动加序号，每份草稿都是一次独立提交
  const dir = `docs/${stamp}`;
  const existing = listFiles(projectId, dir).filter((f) => f.includes(base));
  const seq = String(existing.length + 1).padStart(2, "0");
  const relPath = `${dir}/${base}-${seq}.md`;
  writeWorkspaceFile(projectId, relPath, body ? `${md}\n${body}\n` : md);

  // 依据行是提交钩子的硬要求
  const refStr = bases.map((b) => b.ref).filter(Boolean).join(", ") || "无";
  const message =
    `doc(${safeName(kind, 8)}): ${obj || "未命名"}${missing.length ? `（缺 ${missing.length} 项）` : ""}\n\n` +
    `依据: ${refStr}\n` +
    `项目: ${projectId}`;

  const commit = gitCommit(projectId, message, [relPath]);
  const state = missing.length ? "待补" : "待签";

  // 数据库这一行交给 biz 服务登记，工具自己只写文件和提交 —— doc 表只有一个写者
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/docs/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      projectId, kind, obj: obj || "", state,
      fields: fields || {}, bases, path: relPath, commit,
    }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`登记文书失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
  }
  // 状态和缺项以服务端为准：schema 在服务端，工具自己算出来的会和它对不上
  const saved = payload.data || {};

  return {
    docId: saved.docId || null,
    path: relPath,
    commit,
    state: saved.state || state,
    missing: saved.missing || missing,
    placeholder: saved.placeholder || [],
    unknown: saved.unknown || [],
    bytes: Buffer.byteLength(md),
  };
}

/* ---------------- 校验文书字段 ---------------- */
/**
 * 出稿前先自查：按文书类型的 schema 看字段齐不齐。
 *
 * 为什么要有这一步：模型会把「待补」当成字段值写进去，写进库里就成了
 * 「已填」，到现场才发现没写。schema 在服务端 schemas/<类型>.json，
 * 工具这边不复制规则 —— 只问、不判。
 */
export async function validateDoc({ kind, fields }) {
  if (!kind) throw new Error("缺 kind");
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/docs/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, fields: fields || {} }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`校验失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
  }
  return payload.data;
}

/* ---------------- 上下文（给引擎注入用，也做成工具） ---------------- */
export async function getContext({ projectId }) {
  const proj = await getProject({ projectId });
  return {
    project: proj,
    memoriesFile: readMemories(projectId),
    wsPath: wsPath(projectId),
  };
}

/* ---------------- caption_photo ---------------- */
/**
 * 给照片写一句描述（看过图之后自己写）。
 * 描述是派生文本：图片本身仍在 blob 里，检索靠这句话。
 */
export async function captionPhoto({ projectId, sha256, caption, overwrite = false }) {
  if (!caption) throw new Error("缺 caption");
  if (!projectId && !sha256) throw new Error("projectId 或 sha256 至少给一个");
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/photos/caption`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projectId, sha256, caption, overwrite: !!overwrite }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`写描述失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
  }
  return payload.data;
}
