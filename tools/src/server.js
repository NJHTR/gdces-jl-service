#!/usr/bin/env node
// MCP stdio 服务（JSON-RPC 2.0），零依赖协议实现，业务在 tools.js
// stdout 只走协议，日志一律走 stderr
import { createInterface } from "node:readline";
import { getProject, searchRecords, searchStandards, createDocDraft, validateDoc, getContext, captionPhoto, searchPhotos, recordReview } from "./tools.js";
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
    name: "search_photos",
    description:
      "按描述/部位/日期查现场影像。描述是看过图后写的一句话，" +
      "用来回答「这个部位以前查过什么」。返回 id、时间、部位、描述和原图地址。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        q: { type: "string", description: "关键词，匹配描述和部位" },
        place: { type: "string", description: "部位，如 4#楼东侧外架" },
        dateFrom: { type: "string", description: "起始日期 YYYY-MM-DD" },
        dateTo: { type: "string", description: "结束日期 YYYY-MM-DD" },
        limit: { type: "number", description: "最多返回条数，默认 20" },
      },
      required: ["projectId"],
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
    name: "record_review",
    description: "复核判定落库（verdict: must_fix/suggest/ok）。findings 与 counter 每条必须带 locator（出处定位符），没有 locator 的意见会被拒收。",
    inputSchema: {
      type: "object",
      properties: {
        docId: { type: "number", description: "被复核的文书 id" },
        verdict: { type: "string", enum: ["must_fix", "suggest", "ok"] },
        findings: {
          type: "array",
          items: {
            type: "object",
            properties: {
              why: { type: "string", description: "哪一句站不住、为什么" },
              locator: { type: "string", description: "出处定位符，如 standard://JGJ 130-2011#6.4.3:1-1 或 knowledge/...:12-12" },
              tag: { type: "string", description: "短标签（2-6 字），同类问题用同一个词，如 依据缺失/事实不足/条号不符/期限不合理/责任越界" },
            },
            required: ["why", "locator", "tag"],
          },
        },
        counter: {
          type: "array",
          description: "替代解释/反证（可能还有别的读法）",
          items: {
            type: "object",
            properties: {
              reading: { type: "string" },
              locator: { type: "string" },
            },
            required: ["reading", "locator"],
          },
        },
        alternatives: { type: "array", items: { type: "string" }, description: "被排除的解释" },
        uncertainty: { type: "string", description: "还有哪些不确定" },
      },
      required: ["docId", "verdict"],
    },
  },
  {
    name: "recall_memory",
    description: "检索记忆（项目规矩 / 我的跨项目记忆 / 我在这个项目的习惯）。"
      + "用到哪一条，就在回答里写 [m:<id>]；系统据此记使用次数并在出处里显示。"
      + "不写标记 = 当作没用过。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        query: { type: "string", description: "要找回什么（关键词，别写整句）" },
        userId: { type: "string", description: "查谁的个人记忆，默认当前用户" },
      },
      required: ["projectId", "query"],
    },
  },
  {
    name: "propose_rule",
    description: "提议一条项目规矩（只是候选）。人确认后才会写进项目规矩；"
      + "不要直接改规矩文件。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        text: { type: "string", description: "一条规矩，独立可执行，别写成长段" },
        source: { type: "string", enum: ["agent", "correction", "manual"] },
        sourceRef: { type: "string", description: "哪次对话/哪个文书（可追溯）" },
        evidence: { type: "string", description: "为什么提这条：原文或字段改动" },
      },
      required: ["projectId", "text"],
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
  search_photos: searchPhotos,
  record_review: recordReview,
  propose_rule: proposeRule,
  recall_memory: recallMemory,
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
        capabilities: { tools: {}, resources: {}, prompts: {}, logging: {} },
        serverInfo: SERVER,
      });

    case "ping":
      return ok(id, {});

    case "tools/list":
      return ok(id, { tools: TOOLS });

    // MCP 标准里这几项是可选的，但引擎初始化时会问一遍。
    // 不实现的话，日志里就是 `list（失败）`，而且工具可能整个不注册 —— 实测过。
    case "resources/list":
      return ok(id, { resources: [] });

    case "resources/templates/list":
      return ok(id, { resourceTemplates: [] });

    case "prompts/list":
      return ok(id, { prompts: [] });

    case "logging/setLevel":
      return ok(id, {});

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
