// 项目工作区：路径解析、文件读写、git 提交
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

const ROOT = process.env.WS_ROOT || "/ws";

/** 项目 id 只允许字母数字和短横线，防止路径穿越 */
export function safeId(id) {
  if (!/^[a-zA-Z0-9_-]{1,32}$/.test(String(id || ""))) {
    throw new Error(`项目 id 不合法: ${id}`);
  }
  return String(id);
}

export function wsPath(projectId) {
  return path.join(ROOT, safeId(projectId));
}

export function ensureWs(projectId) {
  const p = wsPath(projectId);
  if (!existsSync(p)) throw new Error(`工作区不存在: ${p}`);
  return p;
}

export function readProject(projectId) {
  const p = wsPath(projectId);
  const metaFile = path.join(p, "meta.json");
  const projFile = path.join(p, "PROJECT.md");
  const meta = existsSync(metaFile) ? JSON.parse(readFileSync(metaFile, "utf8")) : {};
  const ctx = existsSync(projFile) ? readFileSync(projFile, "utf8") : "";
  return { meta, ctx };
}

/**
 * 项目记忆：现在有两份
 *  - records/rules.md：项目规矩（由 memory 表渲染，唯一真相）
 *  - memories/project.md：旧位置，若有还读（逐步淘汰，别让人以为记忆丢了）
 * 个人记忆在家目录里（homes/<uid>/memories），容器只挂工作区，读不到 —— 那是故意的。
 */
export function readMemories(projectId) {
  const ws = wsPath(projectId);
  const parts = [];
  const rules = path.join(ws, "records", "rules.md");
  if (existsSync(rules)) parts.push(readFileSync(rules, "utf8").trim());
  const legacy = path.join(ws, "memories", "project.md");
  if (existsSync(legacy)) parts.push(readFileSync(legacy, "utf8").trim());
  return parts.filter(Boolean).join("\n\n");
}

/** 文件名安全化：去掉路径分隔符和非法字符 */
export function safeName(s, max = 40) {
  return String(s || "")
    .replace(/[\\/:*?"<>|\r\n\t]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max) || "未命名";
}

/** 写入工作区内的相对路径文件，自动建目录 */
export function writeWorkspaceFile(projectId, relPath, content) {
  const root = ensureWs(projectId);
  const abs = path.join(root, relPath);
  if (!abs.startsWith(root + path.sep)) throw new Error("拒绝写工作区之外的文件");
  mkdirSync(path.dirname(abs), { recursive: true });
  writeFileSync(abs, content, "utf8");
  return abs;
}

/** git 提交；返回 commit hash。失败时抛错（钩子拒绝会走到这里） */
export function gitCommit(projectId, message, relPaths = []) {
  const cwd = ensureWs(projectId);
  const run = (args) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

  try {
    if (relPaths.length) run(["add", "--", ...relPaths]);
    else run(["add", "-A"]);
    run(["commit", "-m", message]);
    return run(["rev-parse", "HEAD"]);
  } catch (e) {
    const stderr = (e.stderr || e.stdout || "").toString();
    const msg = stderr.includes("提交被拒") ? stderr.split("\n").slice(0, 3).join(" ") : stderr || String(e.message);
    throw new Error(`git 提交失败: ${msg.trim()}`);
  }
}

/** 读文件前 400 字做摘要 */
export function excerpt(projectId, relPath, max = 400) {
  try {
    const abs = path.join(wsPath(projectId), relPath);
    if (!existsSync(abs) || !statSync(abs).isFile()) return "";
    const txt = readFileSync(abs, "utf8").replace(/\s+/g, " ").trim();
    return txt.slice(0, max);
  } catch {
    return "";
  }
}

/** 列工作区某目录下的文件（相对路径） */
export function listFiles(projectId, relDir) {
  const dir = path.join(wsPath(projectId), relDir);
  if (!existsSync(dir)) return [];
  const out = [];
  const walk = (d, base) => {
    for (const name of readdirSync(d)) {
      const p = path.join(d, name);
      const rel = path.posix.join(base, name);
      if (statSync(p).isDirectory()) walk(p, rel);
      else out.push(rel);
    }
  };
  walk(dir, relDir);
  return out;
}
