import "../dist/utils/http-timeout.js";
import * as api from "@actual-app/api";
import { createHash, timingSafeEqual } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { registerAllTools } from "../dist/tools/index.js";
import { actualApiVersion } from "../dist/utils/version.js";

export function readTools() {
  const tools = [];
  process.env.ACTUAL_READ_ONLY = "1";
  registerAllTools({
    tool(name, description, schema, annotations) {
      // Upstream keeps repair_sync in read-only mode; never expose maintenance.
      if (annotations?.readOnlyHint === true)
        tools.push({ name, description, schema, annotations });
    },
  });
  return tools;
}

export function remoteBudgets(files) {
  return [
    ...new Map(
      files
        .filter((file) => file.cloudFileId && !file.deleted)
        .map((file) => [file.cloudFileId, { budget_id: file.cloudFileId, name: file.name }]),
    ).values(),
  ];
}

export function authorize(header, token) {
  const supplied = Buffer.from(header ?? "");
  const expected = Buffer.from("Bearer " + token);
  return (
    Boolean(token) && supplied.length === expected.length && timingSafeEqual(supplied, expected)
  );
}

export function redact(value, secrets) {
  let text = JSON.stringify(value);
  for (const secret of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) {
    text = text.split(JSON.stringify(secret).slice(1, -1)).join("[redacted]");
  }
  return JSON.parse(text);
}

let listing = Promise.resolve();
export function assertCompatibleVersion(version) {
  // Opening a budget with a newer API can migrate it even with write tools disabled.
  if (
    version?.split(".").slice(0, 2).join(".") !==
    actualApiVersion().split(".").slice(0, 2).join(".")
  ) {
    throw new Error("Actual server and MCP API versions must match before opening budgets.");
  }
}

export function listBudgets() {
  const result = listing.then(async () => {
    await mkdir("/data/catalog", { recursive: true });
    try {
      await api.init({
        dataDir: "/data/catalog",
        serverURL: process.env.ACTUAL_SERVER_URL,
        sessionToken: process.env.ACTUAL_SESSION_TOKEN,
      });
      const { version } = await api.getServerVersion();
      assertCompatibleVersion(version);
      return remoteBudgets(await api.getBudgets());
    } finally {
      await api.shutdown();
    }
  });
  listing = result.catch(() => {});
  return result;
}

const budgetReads = new Map();
export async function queryBudget(...args) {
  const budgetId = args[0];
  const previous = budgetReads.get(budgetId) ?? Promise.resolve();
  const result = previous.catch(() => {}).then(() => queryBudgetIsolated(...args));
  budgetReads.set(budgetId, result);
  try {
    return await result;
  } finally {
    if (budgetReads.get(budgetId) === result) budgetReads.delete(budgetId);
  }
}

async function queryBudgetIsolated(
  budgetId,
  name,
  args,
  passwords,
  makeClient = () => new Client({ name: "actual-http", version: "1" }),
) {
  const cache = createHash("sha256").update(budgetId).digest("hex");
  const client = makeClient();
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["/app/dist/index.js"],
    env: {
      ...process.env,
      ACTUAL_READ_ONLY: "1",
      ACTUAL_BUDGET_ID: budgetId,
      ACTUAL_DATA_DIR: "/data/budgets/" + cache,
      ACTUAL_ENCRYPTION_PASSWORD: passwords[budgetId] ?? "",
    },
    stderr: "ignore",
  });
  // shortcut: one isolated process per read, reuse per-budget workers if startup latency becomes material.
  try {
    await client.connect(transport);
    return await client.callTool({ name, arguments: args }, undefined, { timeout: 120_000 });
  } finally {
    await client.close();
  }
}

export function createServer({ list = listBudgets, query = queryBudget, passwords = {} } = {}) {
  const server = new McpServer({ name: "actual-budget", version: "1" });
  const secrets = [
    process.env.ACTUAL_SESSION_TOKEN,
    process.env.ACTUAL_MCP_TOKEN,
    ...Object.values(passwords),
  ];
  const guarded = async (operation) => {
    try {
      return redact(await operation(), secrets);
    } catch {
      return {
        isError: true,
        content: [
          {
            type: "text",
            text: "Actual read failed. Check credentials, budget encryption, and matching server/API versions.",
          },
        ],
      };
    }
  };
  server.registerTool(
    "list_budgets",
    {
      description:
        "List every budget accessible to the configured Actual user. Use its budget_id on subsequent reads.",
      inputSchema: {},
      annotations: { readOnlyHint: true },
    },
    () =>
      guarded(async () => ({ content: [{ type: "text", text: JSON.stringify(await list()) }] })),
  );
  for (const { name, description, schema, annotations } of readTools()) {
    server.registerTool(
      name,
      {
        description,
        inputSchema: {
          ...schema,
          budget_id: z.string().min(1).describe("ID returned by list_budgets"),
        },
        annotations,
      },
      ({ budget_id, ...args }) =>
        guarded(async () => {
          if (!(await list()).some((budget) => budget.budget_id === budget_id)) {
            return {
              isError: true,
              content: [
                {
                  type: "text",
                  text: "Budget is unavailable. Call list_budgets to select an accessible budget.",
                },
              ],
            };
          }
          return query(budget_id, name, args, passwords);
        }),
    );
  }
  return server;
}

export function start() {
  const token = process.env.ACTUAL_MCP_TOKEN;
  if (!process.env.ACTUAL_SESSION_TOKEN || !token || token.length < 32)
    throw new Error("Actual session token and MCP token (at least 32 characters) are required.");
  const passwords = JSON.parse(process.env.ACTUAL_ENCRYPTION_PASSWORDS ?? "{}");
  if (
    !passwords ||
    Array.isArray(passwords) ||
    typeof passwords !== "object" ||
    Object.values(passwords).some((value) => typeof value !== "string")
  ) {
    throw new Error("ACTUAL_ENCRYPTION_PASSWORDS must map budget IDs to encryption passwords.");
  }
  // Actual's SDK can log upstream responses; never send those to workload logs.
  console.log = console.info = console.warn = console.error = () => {};
  process.on("unhandledRejection", () => {});
  const app = createMcpExpressApp({
    host: "0.0.0.0",
    allowedHosts: [
      "actual-mcp",
      "actual-mcp.ai",
      "actual-mcp.ai.svc",
      "actual-mcp.ai.svc.cluster.local",
      "localhost",
      "127.0.0.1",
    ],
  });
  app.use((req, res, next) => {
    if (!authorize(req.headers.authorization, token)) return res.sendStatus(401);
    if (req.headers.origin) return res.sendStatus(403);
    next();
  });
  app.post("/mcp", async (req, res) => {
    const server = createServer({ passwords });
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    res.on("close", () => {
      void server.close();
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch {
      if (!res.headersSent) res.sendStatus(500);
    }
  });
  app.listen(8080, "0.0.0.0");
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) start();
