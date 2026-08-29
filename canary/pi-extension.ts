import { readFileSync } from "node:fs";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const endpoint = process.env.BROWSER_WORKER_MCP_URL ?? "http://127.0.0.1:8890/mcp";
const tokenFile = process.env.BROWSER_WORKER_MCP_TOKEN_FILE;

function token(): string {
  if (!tokenFile) throw new Error("BROWSER_WORKER_MCP_TOKEN_FILE is required");
  const value = readFileSync(tokenFile, "utf8").trim();
  if (!value) throw new Error("browser-worker token file is empty");
  return value;
}

async function call(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      authorization: `Bearer ${token()}`,
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: `pi-canary-${Date.now()}`,
      method: "tools/call",
      params: { name, arguments: args },
    }),
    redirect: "error",
    signal,
  });
  if (!response.ok) throw new Error(`browser-worker returned HTTP ${response.status}`);
  const payload = (await response.json()) as {
    result?: { content?: Array<{ type?: string; text?: string }>; isError?: boolean };
    error?: unknown;
  };
  if (payload.error || !payload.result) throw new Error("browser-worker returned an MCP error");
  const text = payload.result.content?.find((part) => part.type === "text")?.text;
  if (!text) throw new Error("browser-worker returned no text content");
  if (payload.result.isError) throw new Error(text);
  return text;
}

const browserFetch = defineTool({
  name: "browser_fetch",
  label: "Browser Fetch",
  description: "Render one public web page in an isolated browser and return its visible text.",
  parameters: Type.Object({
    url: Type.String({ description: "Absolute public HTTP(S) URL" }),
    max_chars: Type.Optional(Type.Number({ default: 20000 })),
    wait_until: Type.Optional(
      Type.Union([
        Type.Literal("load"),
        Type.Literal("domcontentloaded"),
        Type.Literal("networkidle"),
        Type.Literal("commit"),
      ]),
    ),
    timeout_ms: Type.Optional(Type.Number({ default: 30000 })),
    include_links: Type.Optional(Type.Boolean({ default: false })),
    include_screenshot: Type.Optional(Type.Boolean({ default: false })),
  }),
  async execute(_id, params, signal) {
    const text = await call("browser_fetch", params, signal);
    return { content: [{ type: "text" as const, text }], details: { endpoint } };
  },
});

const browserInspect = defineTool({
  name: "browser_inspect",
  label: "Browser Inspect",
  description: "Create or operate a short-lived browser session using one explicit action.",
  parameters: Type.Object({
    action: Type.String({ description: "Worker action such as open, extract_text, click, state, or close" }),
    session_id: Type.Optional(Type.String()),
    scope_id: Type.Optional(Type.String()),
    url: Type.Optional(Type.String()),
    selector: Type.Optional(Type.String()),
    text: Type.Optional(Type.String()),
    tab_index: Type.Optional(Type.Number()),
    timeout_ms: Type.Optional(Type.Number({ default: 30000 })),
    wait_until: Type.Optional(Type.String({ default: "domcontentloaded" })),
    state: Type.Optional(Type.String({ default: "visible" })),
    url_contains: Type.Optional(Type.String()),
    max_chars: Type.Optional(Type.Number({ default: 20000 })),
    limit: Type.Optional(Type.Number({ default: 50 })),
    clear: Type.Optional(Type.Boolean({ default: true })),
    submit: Type.Optional(Type.Boolean({ default: false })),
    full_page: Type.Optional(Type.Boolean({ default: true })),
    format: Type.Optional(Type.String({ default: "A4" })),
    landscape: Type.Optional(Type.Boolean({ default: false })),
    print_background: Type.Optional(Type.Boolean({ default: true })),
    script: Type.Optional(Type.String()),
    arg: Type.Optional(Type.Any()),
  }),
  async execute(_id, params, signal) {
    const text = await call("browser_inspect", params, signal);
    return { content: [{ type: "text" as const, text }], details: { endpoint } };
  },
});

export default function register(pi: ExtensionAPI) {
  pi.registerTool(browserFetch);
  pi.registerTool(browserInspect);
}
