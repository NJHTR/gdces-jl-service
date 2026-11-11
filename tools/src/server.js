#!/usr/bin/env node
// MCP stdio 服务（JSON-RPC 2.0），零依赖协议实现，业务在 tools.js
// stdout 只走协议，日志一律走 stderr
import { createInterface } from "node:readline";
import { getProject, searchRecords, searchStandards, createDocDraft, validateDoc, getSchema, getContext, captionPhoto, searchPhotos, recordReview, recallMemory, proposeRule, annotatePhoto, comparePhotos, sendPhoto } from "./tools.js";
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
      "在工作区生成一份文书草稿并 git 提交。先用 get_schema 拿正式字段名，再用 validate_doc 自查；字段名不在 schema 会被拒绝，不再猜别名。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        kind: { type: "string", description: "文书类型；先调用 get_schema(kind)" },
        obj: { type: "string", description: "对象，如 4#楼东侧外架" },
        fields: { type: "object", description: "字段键值；key 必须来自 get_schema 的 required/optional，不能自造别名" },
        bases: {
          type: "array",
          description:
            "依据列表。照片依据放这里，不要放 fields：kind=photo, ref=照片 sha256, text=部位/时间/画面内容/为什么属于本记录。",
          items: {
            type: "object",
            properties: {
              kind: { type: "string", description: "rec / photo / rule / memo" },
              ref: { type: "string", description: "msg_123 / photo sha256 / JGJ130-2011#6.4.3" },
              text: { type: "string", description: "人话依据；照片要写画面和关联理由" },
            },
          },
        },
        body: { type: "string", description: "额外正文（可选）" },
      },
      required: ["projectId", "kind"],
    },
  },
  {
    name: "get_schema",
    description:
      "拿某类文书的正式字段表：required / optional / placeholders / 来源说明。出稿第一步先调它，再按表去现场记录、照片、知识、记忆里逐项找证据；不要猜字段名。",
    inputSchema: {
      type: "object",
      properties: {
        kind: { type: "string", description: "文书类型，如 旁站记录 / 监理通知单 / 监理检查记录" },
      },
      required: ["kind"],
    },
  },
  {
    name: "validate_doc",
    description:
      "出稿前自查：按文书类型的 schema 检查字段齐不齐。返回 {ok, missing, placeholder, unknown, required, optional}。unknown 表示 key 写错，必须改成 required/optional 里的正式名；占位词也算缺。",
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
      "用来回答「这个部位以前查过什么」。返回 id、时间、部位、描述、原图地址，" +
      "以及可直接传给 view_image 的本地 path——先看图再判断，别只看描述选图。",
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
    name: "annotate_photo",
    description:
      "把一张**真实现场照片**做成标注图并放进本次回答（回答里会出现这张图）。" +
      "photoId 必须是先用 search_photos 查到的真实 id，不能编；它只给原图画框/编号/标签，图上会带「派生图·基于现场照片」角标，**不是现场原图**。" +
      "不会生成或伪造现场照片；没有真实 photoId 就不要调用。" +
      "同一张照片本轮重复调用会自动去重；单轮最多 9 张图。调完在正文里照常写一句话即可。" +
      "要标注必须给标注框：只用 caption 不会画出任何圈，不算完成标注。" +
      "**首选 detectLabels**：用开放词汇检测模型返回真实 bbox；不要让语言模型自己估坐标。" +
      "detectLabels 用英文目标名，如 [\"cardboard box\",\"carton\"]、[\"worker\",\"safety helmet\"]；maxBoxes 控制取前几个。" +
      "只有检测不可用时才用 boxes/annotations 手动画框。" +
      "坐标以完整原图为基准（含底部项目信息条），不要按聊天里裁剪/缩放后的图估算；框要刚好包住目标。" +
      "boxes 为原图像素 x/y/w/h；annotations 为 x1/y1/x2/y2，可为 0..1 归一化。" +
      "若改用命令行调用，格式是 argv JSON：jl-tool annotate_photo '{\"projectId\":\"...\",\"photoId\":<id>,\"boxes\":[...]}'；" +
      "不要用 echo/管道/stdin/heredoc，也禁止用 shell/python/Pillow 自己画图或写 /tmp。工具失败就如实告诉用户失败原因，不要绕过。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        photoId: { type: "number", description: "真实照片 id（先用 search_photos 查）" },
        detectLabels: {
          type: "array",
          description: "首选：英文开放词汇目标名，交给检测模型返回真实 bbox",
          items: { type: "string" },
        },
        maxBoxes: { type: "number", description: "检测后最多取几个框，默认 20" },
        boxes: {
          type: "array",
          description: "要标注的框，坐标是原图像素；最多 20 个",
          items: {
            type: "object",
            properties: {
              x: { type: "number" },
              y: { type: "number" },
              w: { type: "number" },
              h: { type: "number" },
              normalized: { type: "boolean", description: "为 true 时 x/y/w/h 使用 0..1 归一化" },
              label: { type: "string", description: "框旁短标签，如 裂缝 / 整改前后" },
            },
            required: ["x", "y", "w", "h"],
          },
        },
        annotations: {
          type: "array",
          description: "boxes 的兼容别名；使用 x1/y1/x2/y2 四角坐标，可为 0..1 归一化（相对完整原图）",
          items: {
            type: "object",
            properties: {
              x1: { type: "number" },
              y1: { type: "number" },
              x2: { type: "number" },
              y2: { type: "number" },
              normalized: { type: "boolean", description: "为 true 时四角坐标使用 0..1 归一化" },
              label: { type: "string" },
            },
            required: ["x1", "y1", "x2", "y2"],
          },
        },
        caption: { type: "string", description: "图片底部说明（可选，别写外链）" },
      },
      required: ["projectId", "photoId", "detectLabels"],
    },
  },
  {
    name: "compare_photos",
    description:
      "把两张**真实现场照片**拼成「整改前 / 整改后」对比图并放进本次回答。" +
      "两个 id 必须先用 search_photos 查到真实照片，且不能是同一张；图上两侧会带来源水印（现场照片#id/日期/部位）和「派生图」角标，" +
      "不伪造现场。单轮最多 9 张图（含标注图）；同一对照片本轮重复调用会自动去重。调完在正文里照常写一句话。" +
      "命令行格式：jl-tool compare_photos '{\"projectId\":\"...\",\"beforePhotoId\":<id>,\"afterPhotoId\":<id>,\"title\":\"...\"}'（argv JSON，不是 stdin）；" +
      "禁止用 shell/python/Pillow 自绘或写 /tmp；工具失败就如实说明失败，不要绕过。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        beforePhotoId: { type: "number", description: "整改前真实照片 id" },
        afterPhotoId: { type: "number", description: "整改后真实照片 id" },
        title: { type: "string", description: "拼版标题，如 4#楼东侧外架整改前后" },
        beforeLabel: { type: "string", description: "左侧标签，默认 整改前" },
        afterLabel: { type: "string", description: "右侧标签，默认 整改后" },
      },
      required: ["projectId", "beforePhotoId", "afterPhotoId"],
    },
  },
  {
    name: "send_photo",
    description:
      "把一张**真实项目现场照片**直接发给用户看（回答里出现这张照片；只引用，不改图）。" +
      "用户说「发张图片/给我看照片/把照片X发我/随便发张图」时用这个，**不要用文字描述代替图片**。" +
      "photoId 必须先用 search_photos 查到真实 id（可按部位/日期/描述搜），不能编；" +
      "照片不存在、不属于本项目或不是图片就如实告诉用户（例如「没有找到这张照片」），不要伪造、不要用 shell/python 画图。" +
      "同一张照片本轮重复调用会自动去重；单轮最多 9 张图。调完在正文里照常写一句话。" +
      "命令行格式：jl-tool send_photo '{\"projectId\":\"...\",\"photoId\":<id>,\"caption\":\"...\"}'（argv JSON，不是 stdin/管道）。",
    inputSchema: {
      type: "object",
      properties: {
        projectId: { type: "string" },
        photoId: { type: "number", description: "真实照片 id（先用 search_photos 查）" },
        caption: { type: "string", description: "图片说明（可选，如 4#楼东侧外架现场）" },
      },
      required: ["projectId", "photoId"],
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
  get_schema: getSchema,
  get_context: getContext,
  caption_photo: captionPhoto,
  search_photos: searchPhotos,
  record_review: recordReview,
  propose_rule: proposeRule,
  recall_memory: recallMemory,
  annotate_photo: annotatePhoto,
  compare_photos: comparePhotos,
  send_photo: sendPhoto,
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
        let extra = "";
        if (name === "annotate_photo" || name === "compare_photos" || name === "send_photo") {
          extra = `\n用法：jl-tool ${name} '<JSON>'（JSON 作为第二个参数，不是 stdin/管道/heredoc）。` +
            "不要把失败改成用 shell/python 自己画图；如实告诉用户失败原因。";
        }
        return ok(id, {
          content: [{ type: "text", text: `工具执行失败: ${e.message || e}${extra}` }],
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
