#!/usr/bin/env node
// MCP stdio 服务（JSON-RPC 2.0），零依赖协议实现，业务在 tools.js
// stdout 只走协议，日志一律走 stderr
import { createInterface } from "node:readline";
import { getProject, searchRecords, searchStandards, createDocDraft, validateDoc, getContext, captionPhoto } from "./tools.js";
import { health } from "./db.js";

const SERVER = { name: "jl-tools", version: "0.1.0" };
const PROTOCOL = "2024-11-05";

const TOOLS = [
  {
    name: "get_project",
    description: "读取工程项目信息（名称、编号、地址、工程概况 21 字段）。填文书抬头、判断部位归属时用。",
    inputSchema: {
      type: "object",
      properties: { projectId: { type: "string", description: "项目 id" } },
      required: ["projectId"],
    },
  },
  {
    name: "search_records",
    description:
      "检索该项目的文书与现场记录（旁站记录、监理通知单、监理日志）。用于查历史问题、判断是否重复发生。返回摘要，需要全文用 path 再取。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        keyword: { type: "string", description: "关键词，匹配类型与部位" },
        place: { type: "string", description: "部位，如 4#楼东侧外架" },
        kind: { type: "string", description: "文书类型：旁站记录 / 监理通知单 / 监理日志" },
        dateFrom: { type: "string", description: "起始日期 YYYY-MM-DD" },
        dateTo: { type: "string", description: "结束日期 YYYY-MM-DD" },
        limit: { type: "number", description: "最多返回条数，默认 10" },
      },
      required: ["projectId"],
    },
  },
  {
    name: "search_standards",
    description:
      "用自然语言检索规范条文（如「连墙件 间距」「保护层厚度允许偏差」）。返回条文全文、标准号、条号、版本。写得靠不靠得住，靠它。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "自然语言或关键词" },
        limit: { type: "number", description: "最多返回条数，默认 5" },
      },
      required: ["query"],
    },
  },
  {
    name: "create_doc_draft",
    description:
      "在工作区生成一份文书草稿并 git 提交。提交信息必须先写依据，没有依据会被钩子拒绝。缺的字段会标「待补」，不影响生成。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        kind: { type: "string", description: "旁站记录 / 监理通知单 / 监理日志" },
        obj: { type: "string", description: "对象，如 4#楼东侧外架" },
        fields: { type: "object", description: "字段键值，如 {受检单位, 部位, 事由}" },
        bases: {
          type: "array",
          description: "依据列表",
          items: {
            type: "object",
            properties: {
              kind: { type: "string", description: "rec / photo / rule / memo" },
              ref: { type: "string", description: "msg_123 / photo_9 / JGJ130-2011#6.4.3" },
              text: { type: "string" },
            },
          },
        },
        body: { type: "string", description: "额外正文（可选）" },
      },
      required: ["projectId", "kind"],
    },
  },
  {
    name: "validate_doc",
    description:
      "出稿前自查：按文书类型的 schema 检查字段齐不齐。占位词（待补/待定/未知…）会被当成缺。返回 {ok, missing, placeholder, unknown}。缺就继续打听/补齐，别硬写。",
    inputSchema: {
      type: "object",
      properties: {
        kind: { type: "string", description: "文书类型，如 监理通知单 / 监理检查记录" },
        fields: { type: "object", description: "当前字段键值" },
      },
      required: ["kind"],
    },
  },
  {
    name: "caption_photo",
    description:
      "给一张现场照片写一句描述（部位、工序、看到的问题、可见的量测/水印信息）。" +
      "你刚看过图片时调用，让'看过'变成'记得'：下次检索能凭这句话找回这张图。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        sha256: { type: "string", description: "照片的 sha256（上传接口返回过）" },
        caption: { type: "string", description: "一句话描述，写清楚部位+工序+问题" },
        overwrite: { type: "boolean", description: "已有人写过描述时是否覆盖，默认 false" },
      },
      required: ["projectId", "caption"],
    },
  },
  {
    name: "get_context",
    description: "一次性拿到项目信息 + 项目记忆 + 工作区路径。会话开始时用，省得连调几个工具。",
    inputSchema: {
      type: "object",
      properties: { projectId: { type: "string" } },
      required: ["projectId"],
    },
  },
];

const HANDLERS = {
  get_project: getProject,
  search_records: searchRecords,
  search_standards: searchStandards,
  create_doc_draft: createDocDraft,
  validate_doc: validateDoc,
  get_context: getContext,
  caption_photo: captionPhoto,
};

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function ok(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handle(msg) {
  const { id, method, params } = msg;

  // 通知（无 id）不需要回
  if (id === undefined) return;

  switch (method) {
    case "initialize":
      return ok(id, {
        protocolVersion: PROTOCOL,
        capabilities: { tools: {} },
        serverInfo: SERVER,
      });

    case "ping":
      return ok(id, {});

    case "tools/list":
      return ok(id, { tools: TOOLS });

    case "tools/call": {
      const name = params?.name;
      const args = params?.arguments || {};
      const fn = HANDLERS[name];
      if (!fn) return fail(id, -32602, `未知工具: ${name}`);
      try {
        const data = await fn(args);
        return ok(id, { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] });
      } catch (e) {
        return ok(id, {
          content: [{ type: "text", text: `工具执行失败: ${e.message || e}` }],
          isError: true,
        });
      }
    }

    case "health":
      return ok(id, await health());

    default:
      return fail(id, -32601, `不支持的方法: ${method}`);
  }
}

let inflight = 0;
let closed = false;

function maybeExit() {
  if (closed && inflight === 0) setTimeout(() => process.exit(0), 30);
}

const rl = createInterface({ input: process.stdin });

rl.on("line", (line) => {
  const s = line.trim();
  if (!s) return;
  let msg;
  try {
    msg = JSON.parse(s);
  } catch {
    return fail(null, -32700, "解析失败");
  }
  inflight++;
  handle(msg)
    .catch((e) => {
      if (msg && msg.id !== undefined) fail(msg.id, -32603, String(e.message || e));
    })
    .finally(() => {
      inflight--;
      maybeExit();
    });
});

// stdin 关闭后不能立刻退：还有未完成的工具调用。
// 等它们都回完再退，否则客户端会收到半截。
rl.on("close", () => {
  closed = true;
  maybeExit();
});

process.on("uncaughtException", (e) => process.stderr.write(`uncaught: ${e.stack}\n`));
process.stderr.write(`jl-tools 已启动（stdio MCP，${TOOLS.length} 个工具）\n`);
