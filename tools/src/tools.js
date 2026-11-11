// 四个工具的实际实现
import { q, one } from "./db.js";
import fs from "node:fs";
import path from "node:path";
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
export async function searchStandards(args = {}) {
  // 参数不合格要报错，不能静默返回空：模型传错键名（如 keyword）时会以为"库里没有"。
  // 实测：模型连续用 {"keyword": "连墙件"} 查了 5 次，全空，然后回答"规范库没有"。
  const text = args.query;
  if (text === undefined) {
    throw new Error(
      '参数不对：search_standards 要 {"query": "关键词"}，可带 "limit"（默认 5）。' +
      '收到: ' + JSON.stringify(args)
    );
  }
  const limit = Number(args.limit) || 5;
  if (!String(text).trim()) return [];
  const rows = await q(
    `SELECT code, clause, title, std_title, body, version, effective_at,
            ROUND(MATCH(title, body) AGAINST (? IN NATURAL LANGUAGE MODE), 3) AS score
       FROM standard
      HAVING score > 0
      ORDER BY score DESC
      LIMIT ?`,
    [text, Math.min(Number(limit) || 5, 20)]
  );
  // 记一笔「这一轮查过什么」：引擎的命令输出不回传给客户端，
  // 所以引用只能在工具侧落盘，由 BFF 在一轮结束时取走（见 agent._drain_cites）。
  try {
    // 容器里只有 /ws（= 宿主 data/workspaces），所以用 basename 拼容器内路径
    const hostWs = process.env.JL_WS || "";
    if (!hostWs) throw new Error("没有 JL_WS");
    const ws = path.join("/ws", path.basename(hostWs));
    const dir = path.join(ws, ".agent");
    fs.mkdirSync(dir, { recursive: true });
    const fp = path.join(dir, "cites.jsonl");
    const now = Date.now() / 1000;
    for (const r of rows) {
      fs.appendFileSync(fp, JSON.stringify({
        ts: now, code: r.code, clause: r.clause, title: r.title,
        std: r.std_title, quote: String(r.body || "").replace(/\s+/g, " ").slice(0, 140),
      }) + "\n");
    }
  } catch (e) {
    console.error("[search_standards] 写引用失败:", e && e.message);
  }
  return rows;
}

/** 本轮任务号：BFF 在每轮开始写 <ws>/.agent/turn-<uid>.json，这里读最新的一个。
 *
 * 为什么要它：文书要挂到"是哪一轮产出的"上。以前 BFF 只能按"本轮之后第一条新文书"
 * 认领，同项目并发时会把别人刚建的文书算成自己的（实测删过用户文书）。
 */
function currentTurn(projectId) {
  try {
    const dir = path.join("/ws", projectId, ".agent");
    if (!fs.existsSync(dir)) return null;
    const cands = fs.readdirSync(dir)
      .filter((f) => /^turn-\d+\.json$/.test(f))
      .map((f) => {
        const fp = path.join(dir, f);
        return { fp, mtime: fs.statSync(fp).mtimeMs };
      })
      .sort((a, b) => b.mtime - a.mtime);
    if (!cands.length) return null;
    if (Date.now() - cands[0].mtime > 30 * 60 * 1000) return null;  // 半小时前的不要
    const j = JSON.parse(fs.readFileSync(cands[0].fp, "utf8"));
    return j && j.taskId ? j.taskId : null;
  } catch (e) {
    console.error("[turn] 读本轮标记失败:", e && e.message);
    return null;
  }
}

/* ---------------- create_doc_draft ---------------- */
// 不再在这里写字段表：字段规则只有一个出处 —— 服务端 schemas/<类型>.json。
// 以前这里有一份 TEMPLATES，加字段时两边不同步，算出的状态和数据库对不上。

function renderDoc({ kind, obj, fields, bases, missingKeys = [] }) {
  // required 由调用方从服务端 validate 拿到，这里只负责排版
  const req = missingKeys.concat(Object.keys(fields || {}));
  const lines = [
    `# ${kind}${obj ? " · " + obj : ""}`,
    "",
    `> 生成时间：${new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`,
    "",
  ];

  const missing = missingKeys.slice();
  for (const k of missingKeys) {
    lines.push(`- **${k}**：待补`);
  }
  for (const [k, v] of Object.entries(fields || {})) {
    if (v === undefined || v === null || String(v).trim() === "") continue;
    lines.push(`- **${k}**：${v}`);
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

  // “自然确认 / 目测 / 经验”不是依据；没有条文号或照片依据时不能拿来过 required。
  const basis = String((fields || {}).依据 || (fields || {}).检查依据 || "").trim();
  const pseudo = ["自然确认", "现场确认", "目测", "经验", "无", "暂无", "N/A", "n/a", "/", "-"];
  if (basis && !(bases || []).length && pseudo.indexOf(basis) >= 0) {
    throw new Error(
      "依据不能只写「" + basis + "」：它不是条文、图纸/方案号，也不是照片依据。" +
      " 查不到规范就留空并问用户，或有现场照片就放进 bases；不要拿历史文书的假依据过关。"
    );
  }

  // schema 是唯一出处：先拿正式字段校验，字段名不在表里就拒绝写文件。
  let chk;
  try {
    chk = await validateDoc({ kind, fields: fields || {} });
  } catch (err) {
    throw new Error("无法校验字段（" + ((err && err.message) || "网络异常") + "），先不写文书；请重试 validate_doc");
  }
  if ((chk.unknown || []).length) {
    const allowed = (chk.required || []).concat(chk.optional || []);
    throw new Error(
      "字段名不在 schema，已拒绝写入：" + chk.unknown.join("、") +
      "；正式字段是：" + allowed.join("、") +
      "；请用 get_schema 重新对照，把事实填到正式 key 后再提交"
    );
  }
  const missingKeys = (chk.missing || []).concat(chk.placeholder || []);
  const { md } = renderDoc({ kind, obj, fields, bases, missingKeys });
  const missing = missingKeys;

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
  // state 由服务端按 schema 决定；这里只在服务端没给时兜底
  const state = missing.length ? "待补" : "待签";

  // 数据库这一行交给 biz 服务登记，工具自己只写文件和提交 —— doc 表只有一个写者
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/docs/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      projectId, kind, obj: obj || "",
      fields: fields || {}, bases, path: relPath, commit,
      taskId: currentTurn(projectId),
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

/* ---------------- get_schema ---------------- */
/**
 * 拿某类文书的正式字段表（required/optional/placeholders）。
 * 模型必须按这张表去理解现场、找证据、逐项填；不要猜字段名。
 */
export async function getSchema({ kind }) {
  if (!kind) throw new Error("缺 kind");
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/docs/schema`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`取 schema 失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
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

/* ---------------- search_photos ---------------- */
/**
 * 按描述/部位/日期查现场影像。
 * caption 是看过图之后写的一句话，所以这里能回答"这个部位以前查过什么"。
 */
export async function searchPhotos({ projectId, q, place, dateFrom, dateTo, limit = 20 }) {
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const u = new URL(`${bff}/api/v1/photos/search`);
  u.searchParams.set("projectId", projectId);
  if (q) u.searchParams.set("q", q);
  if (place) u.searchParams.set("place", place);
  if (dateFrom) u.searchParams.set("dateFrom", dateFrom);
  if (dateTo) u.searchParams.set("dateTo", dateTo);
  u.searchParams.set("limit", String(limit));
  const resp = await fetch(u);
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`查影像失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
  }
  return { total: payload.total, items: payload.data || [] };
}

/* ---------------- record_review ---------------- */
/**
 * 复核判定落库。
 *
 * 为什么做成工具而不是让模型输出 JSON：文本要后端解析（禁止）；
 * 工具是声明式的、可校验的，而且判定直接进账本，成为「核验」信号。
 */
/**
 * 提议一条项目规矩。**只是候选**：人确认后才会写进 records/rules.md。
 * 为什么不让 agent 直接写规矩：规矩影响所有人的出稿，得有人拍板（C2 的设计）。
 */
/**
 * 检索记忆（项目规矩 / 我的跨项目记忆 / 我在这个项目的习惯）。
 * 用到哪条，就在回答里写 [m:<id>] —— 系统据此记使用次数并在出处里显示。
 * 不写标记 = 当作没用过（不虚报"依据你的习惯"）。
 */
export async function recallMemory({ projectId, query, userId = "" }) {
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const qs = new URLSearchParams({ projectId, q: query, userId: String(userId) });
  const resp = await fetch(`${bff}/api/v1/memories/recall?${qs}`);
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`检索记忆失败: ${resp.status} ${JSON.stringify(payload).slice(0, 200)}`);
  }
  return payload.data || [];
}


export async function proposeRule({ projectId, text, source = "agent", sourceRef = "", evidence = "" }) {
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/rule-proposals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projectId, text, source, sourceRef, evidence }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`提交规矩提案失败: ${resp.status} ${JSON.stringify(payload).slice(0, 300)}`);
  }
  return payload.data;
}


export async function recordReview({ docId, verdict, findings = [], counter = [], alternatives = [], uncertainty = "", taskId }) {
  // findings 里带 tag：同类问题反复出现时，观察队列靠它聚合（后端不解析文本）
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}/api/v1/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ docId, taskId, verdict, findings, counter, alternatives, uncertainty }),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    throw new Error(`登记复核失败: ${resp.status} ${JSON.stringify(payload).slice(0, 300)}`);
  }
  return payload.data;
}

/* ================================================================
 * 多模态：真实照片标注 / 整改前后对比（把 image_ref 写进本轮回答）
 *
 * 链路：模型先 search_photos 拿真实 photoId → 调这里 →
 *   这里只调 BFF 的 /api/v1/photos/annotate|compare（不自己画图、不写证据）→
 *   BFF 返回 image_ref block → 合并写入 <ws>/.agent/blocks.json →
 *   本轮结束时 BFF `_turn_blocks` 取走并挂到 message.answer.blocks。
 *
 * 硬边界：
 *   · 只接受真实 photoId；不生成/伪造现场照片；
 *   · 不写 photo/doc/records/memory/knowledge，产物只走 data/assets（BFF asset 服务）；
 *   · blocks.json 只存引用（assetId/sourceRefs/url），不存 base64、本地路径、外链；
 *   · 同一张源照片本轮重复调只保留一条（不重复生成资产）；单轮图片上限 9 张。
 * ================================================================ */
const IMAGE_BLOCK_CAP = 9;
const FILE_BLOCK_CAP = 2;
const CARD_BLOCK_CAP = 3;
const TEXT_BLOCK_CAP = 6;
const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

function blocksFilePath(projectId) {
  return path.join(wsPath(projectId), ".agent", "blocks.json");
}

function looksUnsafe(s) {
  const text = String(s || "");
  if (!text) return false;
  return /data:[a-z0-9.+-]+\/[a-z0-9.+-]+;base64,/i.test(text)
    || /(https?:\/\/|file:\/\/)/i.test(text)
    || /(^|[\s"'(])(\/(home|tmp|var|etc)\/|[A-Za-z]:[\\/])/.test(text);
}

function readBlocks(projectId) {
  try {
    const fp = blocksFilePath(projectId);
    if (!fs.existsSync(fp)) return [];
    const raw = JSON.parse(fs.readFileSync(fp, "utf8"));
    const arr = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.blocks) ? raw.blocks : []);
    return arr.filter((b) => b && typeof b === "object" && b.type);
  } catch (e) {
    console.error("[blocks] 读失败（按空处理）:", e && e.message);
    return [];
  }
}

function writeBlocks(projectId, blocks) {
  const fp = blocksFilePath(projectId);
  fs.mkdirSync(path.dirname(fp), { recursive: true });
  const tmp = fp + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify({ blocks }, null, 2), "utf8");
  fs.renameSync(tmp, fp);
  return fp;
}

function countBlocks(blocks, type) {
  return blocks.filter((b) => b.type === type).length;
}

/** 只保留 answer.blocks 协议内字段；校验引用形态，不合法直接拒绝（不伪造块）。 */
function sanitizeBlock(raw, expectType, expectOrigin = "derived") {
  if (!raw || raw.type !== expectType) {
    throw new Error(`服务端没有返回 ${expectType} block，已放弃（不伪造图片）`);
  }
  const out = {
    type: raw.type,
    assetId: String(raw.assetId || ""),
    caption: String(raw.caption || "").slice(0, 300),
    origin: raw.origin || "",
    mime: raw.mime || "",
    sourceRefs: Array.isArray(raw.sourceRefs) ? raw.sourceRefs.map(String) : [],
    url: String(raw.url || ""),
  };
  if (!ULID_RE.test(out.assetId)) throw new Error("服务端返回的 assetId 不合法，已放弃");
  if (!out.url.startsWith("/api/v1/assets/")) throw new Error("服务端返回的 url 不合法，已放弃");
  if ((out.sourceRefs || []).some((r) => !/^photo:\d+$/.test(r))) {
    throw new Error("image_ref 的 sourceRefs 必须指向真实 photo:<id>，已放弃");
  }
  if (out.origin !== expectOrigin) {
    throw new Error(`图片产物必须是 ${expectOrigin}，已放弃`);
  }
  for (const v of [out.caption, out.url]) {
    if (looksUnsafe(v)) throw new Error("block 含外链/本地路径/base64，已放弃");
  }
  return out;
}

function mergeBlock(projectId, block) {
  const blocks = readBlocks(projectId);
  const caps = { image_ref: IMAGE_BLOCK_CAP, file_ref: FILE_BLOCK_CAP, card: CARD_BLOCK_CAP };
  if (countBlocks(blocks, block.type) >= (caps[block.type] || 99)) {
    throw new Error(`本轮 ${block.type} 已达上限 ${caps[block.type]}，未加入回答`);
  }
  const dup = blocks.some((b) => b.type === block.type &&
    (b.assetId === block.assetId ||
     JSON.stringify((b.sourceRefs || []).slice().sort()) === JSON.stringify((block.sourceRefs || []).slice().sort())));
  if (dup) return { blocks, duplicated: true };
  blocks.push(block);
  writeBlocks(projectId, blocks);
  return { blocks, duplicated: false };
}

function sameSourceBlock(projectId, sourceRefs) {
  const want = JSON.stringify(sourceRefs.slice().sort());
  return readBlocks(projectId).find((b) => b.type === "image_ref" &&
    JSON.stringify((b.sourceRefs || []).slice().sort()) === want) || null;
}

function requirePhotoId(photoId, field = "photoId") {
  const n = Number(photoId);
  if (!Number.isInteger(n) || n <= 0) throw new Error(`${field} 必须是真实照片 id（先用 search_photos 查）`);
  return n;
}

function normalizeBoxes(boxes, annotations) {
  const raw = Array.isArray(boxes) && boxes.length
    ? boxes
    : (Array.isArray(annotations) ? annotations : boxes);
  if (!Array.isArray(raw)) {
    throw new Error("缺少标注框：annotate_photo 必须提供 boxes 或 annotations");
  }
  if (!raw.length) {
    // 保留 caption-only 的兼容路径；只要调用方给了 annotations，就必须真的能解析出框。
    if (Array.isArray(annotations) && annotations.length) {
      throw new Error("annotations 里没有有效标注框（需要 x1/y1/x2/y2）");
    }
    return [];
  }
  if (raw.length > 20) throw new Error("标注框最多 20 个");
  return raw.map((b, i) => {
    if (!b || typeof b !== "object") throw new Error(`boxes[${i}] 不是对象`);
    const out = {};
    const hasCorners = b.x1 !== undefined || b.y1 !== undefined || b.x2 !== undefined || b.y2 !== undefined;
    if (hasCorners) {
      for (const k of ["x1", "y1", "x2", "y2"]) {
        const v = Number(b[k]);
        if (!Number.isFinite(v)) throw new Error(`annotations[${i}].${k} 必须是数字`);
        out[k] = v;
      }
      if (out.x2 <= out.x1 || out.y2 <= out.y1) throw new Error(`annotations[${i}] 右下角必须大于左上角`);
      const unit = b.normalized !== false &&
        [out.x1, out.y1, out.x2, out.y2].every((v) => v >= 0 && v <= 1);
      out.normalized = !!unit;
    } else {
      for (const k of ["x", "y", "w", "h"]) {
        const v = Number(b[k]);
        if (!Number.isFinite(v)) throw new Error(`boxes[${i}].${k} 必须是数字`);
        out[k] = v;
      }
      if (out.w <= 0 || out.h <= 0) throw new Error(`boxes[${i}] 宽高必须大于 0`);
      const unit = b.normalized === true ||
        ([out.x, out.y, out.w, out.h].every((v) => v >= 0 && v <= 1) &&
          out.x + out.w <= 1 && out.y + out.h <= 1);
      out.normalized = !!unit;
    }
    if (b.label !== undefined) out.label = String(b.label).slice(0, 24);
    return out;
  });
}

async function bffPost(pathname, body) {
  const bff = process.env.BFF_URL || "http://host.docker.internal:8081";
  const resp = await fetch(`${bff}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await resp.json().catch(() => ({}));
  if (!resp.ok || payload.code !== 0) {
    const msg = (payload && payload.message) || JSON.stringify(payload).slice(0, 300);
    throw new Error(`${pathname} 失败: HTTP ${resp.status} ${msg}`);
  }
  return payload.data || {};
}

/** 在真实照片上标注 → 派生图 → 加入本轮回答。 */
export async function annotatePhoto({ projectId, photoId, boxes, annotations, caption = "" }) {
  if (!projectId) throw new Error("缺 projectId");
  const pid = requirePhotoId(photoId);
  const normBoxes = normalizeBoxes(boxes, annotations);
  const refs = [`photo:${pid}`];
  // 去重：同一张源照片本轮已加过 → 不重复生成资产
  const dup = sameSourceBlock(projectId, refs);
  if (dup) {
    return { ok: true, duplicated: true, assetId: dup.assetId, caption: dup.caption,
             note: "这张照片已在本次回答里，未重复生成" };
  }
  const existing = readBlocks(projectId);
  if (countBlocks(existing, "image_ref") >= IMAGE_BLOCK_CAP) {
    throw new Error(`本轮图片已达上限 ${IMAGE_BLOCK_CAP} 张，未加入回答；请先回答文字`);
  }
  const data = await bffPost("/api/v1/photos/annotate", {
    projectId, photoId: pid, boxes: normBoxes, caption: String(caption || "").slice(0, 120),
  });
  const block = sanitizeBlock(data.block, "image_ref");
  const merged = mergeBlock(projectId, block);
  return {
    ok: true, duplicated: !!merged.duplicated, assetId: block.assetId,
    caption: block.caption, origin: "derived",
    note: merged.duplicated ? "这张照片已在本次回答里" : "标注图已加入本次回答（派生图，不是现场原图）",
  };
}

/** 整改前后对比拼版 → 派生图 → 加入本轮回答。 */
export async function comparePhotos({ projectId, beforePhotoId, afterPhotoId, title = "",
                                     beforeLabel = "整改前", afterLabel = "整改后" }) {
  if (!projectId) throw new Error("缺 projectId");
  const before = requirePhotoId(beforePhotoId, "beforePhotoId");
  const after = requirePhotoId(afterPhotoId, "afterPhotoId");
  if (before === after) throw new Error("对比需要两张不同照片（同一张不能做前后对比）");
  const refs = [`photo:${before}`, `photo:${after}`];
  const dup = sameSourceBlock(projectId, refs);
  if (dup) {
    return { ok: true, duplicated: true, assetId: dup.assetId, caption: dup.caption,
             note: "这两张照片的对比图已在本次回答里，未重复生成" };
  }
  const existing = readBlocks(projectId);
  if (countBlocks(existing, "image_ref") >= IMAGE_BLOCK_CAP) {
    throw new Error(`本轮图片已达上限 ${IMAGE_BLOCK_CAP} 张，未加入回答；请先回答文字`);
  }
  const data = await bffPost("/api/v1/photos/compare", {
    projectId, beforePhotoId: before, afterPhotoId: after,
    title: String(title || "").slice(0, 80),
    beforeLabel: String(beforeLabel || "整改前").slice(0, 12),
    afterLabel: String(afterLabel || "整改后").slice(0, 12),
  });
  const block = sanitizeBlock(data.block, "image_ref");
  const merged = mergeBlock(projectId, block);
  return {
    ok: true, duplicated: !!merged.duplicated, assetId: block.assetId,
    caption: block.caption, origin: "derived",
    note: merged.duplicated ? "对比图已在本次回答里" : "整改前后对比图已加入本次回答（派生图）",
  };
}

/** 直接把一张**真实项目照片**发给用户（引用已有照片，不改图、不复制证据）。 */
export async function sendPhoto({ projectId, photoId, caption = "" }) {
  if (!projectId) throw new Error("缺 projectId");
  const pid = requirePhotoId(photoId);
  const refs = [`photo:${pid}`];
  const dup = sameSourceBlock(projectId, refs);
  if (dup) {
    return { ok: true, duplicated: true, assetId: dup.assetId, caption: dup.caption,
             origin: dup.origin || "uploaded", note: "这张照片已在本次回答里，未重复发送" };
  }
  const existing = readBlocks(projectId);
  if (countBlocks(existing, "image_ref") >= IMAGE_BLOCK_CAP) {
    throw new Error(`本轮图片已达上限 ${IMAGE_BLOCK_CAP} 张，未加入回答；请先回答文字`);
  }
  // 走资产服务的 JSON photoId 分支：origin=uploaded、sourceRefs=["photo:<id>"]、
  // 只把去 EXIF 的展示副本存进 data/assets，不动 photo 表/原图。
  const data = await bffPost("/api/v1/assets", {
    projectId, photoId: pid, caption: String(caption || "").slice(0, 120),
  });
  if (String(data.kind || "") !== "image") {
    throw new Error("这个 photoId 不是图片（或不存在），无法作为图片发送；请如实告诉用户");
  }
  // 直接发照片：建完就提交（staged → committed），不等本轮结束；
  // 这样工具立刻可用、90d 生命周期正确，_turn_blocks 仍会做幂等校验。
  if (data.status !== "committed") {
    await bffPost(`/api/v1/assets/${data.assetId}/commit`, {});
    data.status = "committed";
  }
  const raw = {
    type: "image_ref",
    assetId: data.assetId,
    caption: data.caption || String(caption || "").slice(0, 120) || "",
    origin: data.origin,
    mime: data.mime,
    sourceRefs: data.sourceRefs,
    url: data.url,
  };
  const block = sanitizeBlock(raw, "image_ref", "uploaded");
  const merged = mergeBlock(projectId, block);
  return {
    ok: true, duplicated: !!merged.duplicated, assetId: block.assetId,
    caption: block.caption, origin: "uploaded",
    note: merged.duplicated ? "这张照片已在本次回答里" : "照片已加入本次回答（项目现场照片引用）",
  };
}
