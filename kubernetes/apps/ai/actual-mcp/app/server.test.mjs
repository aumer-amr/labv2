import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { get } from "node:http";
import { copyFile, mkdtemp, rmdir, symlink, unlink } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import {
  assertCompatibleVersion,
  authorize,
  createServer,
  queryBudget,
  readTools,
  redact,
  remoteBudgets,
} from "./server.mjs";

assert.equal(authorize(undefined, "token"), false);
assert.equal(authorize("Bearer wrong", "token"), false);
assert.equal(authorize("Bearer token", "token"), true);
assert.equal(authorize("Bearer ", ""), false);
assert.doesNotThrow(() => assertCompatibleVersion("26.10.0"));
assert.doesNotThrow(() => assertCompatibleVersion("26.10.1"));
for (const version of [undefined, "26.9.0", "26.11.0", "27.10.0"]) {
  assert.throws(() => assertCompatibleVersion(version));
}
assert.deepEqual(
  remoteBudgets([
    { id: "local-only", name: "Local" },
    { cloudFileId: "file-one", groupId: "one", name: "One" },
    { cloudFileId: "file-one", groupId: "one", name: "One" },
    { cloudFileId: "missing-sync-id", name: "Incomplete" },
    { cloudFileId: "file-deleted", groupId: "deleted", name: "Deleted", deleted: true },
  ]),
  [{ budget_id: "one", name: "One" }],
);
assert.deepEqual(redact({ text: 'quoted "token" and password' }, ['"token"', "password"]), {
  text: "quoted [redacted] and [redacted]",
});
const tools = readTools();
assert.equal(tools.length, 15);
assert.ok(tools.every((tool) => tool.annotations.readOnlyHint === true));
assert.ok(!tools.some((tool) => tool.name === "repair_sync"));

const calls = [];
const server = createServer({
  list: async () => [
    { budget_id: "one", name: "One" },
    { budget_id: "two", name: "Two" },
  ],
  query: async (id, name, args) => {
    calls.push({ id, name, args });
    await new Promise((resolve) => setTimeout(resolve, id === "one" ? 10 : 1));
    return { content: [{ type: "text", text: id }] };
  },
});
const client = new Client({ name: "test", version: "1" });
const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
await server.connect(serverTransport);
await client.connect(clientTransport);
try {
  const { tools: exposed } = await client.listTools();
  assert.equal(exposed.length, 16);
  assert.ok(
    exposed.every(
      (tool) => tool.name === "list_budgets" || tool.inputSchema.required.includes("budget_id"),
    ),
  );
  const results = await Promise.all(
    ["one", "two"].map((budget_id) =>
      client.callTool({ name: "list_accounts", arguments: { budget_id } }),
    ),
  );
  assert.deepEqual(
    results.map((result) => result.content[0].text),
    ["one", "two"],
  );
  assert.deepEqual(calls, [
    { id: "one", name: "list_accounts", args: {} },
    { id: "two", name: "list_accounts", args: {} },
  ]);
  assert.equal(
    (await client.callTool({ name: "list_accounts", arguments: { budget_id: "unknown" } })).isError,
    true,
  );
  assert.equal((await client.callTool({ name: "list_accounts", arguments: {} })).isError, true);
  assert.equal(
    (await client.callTool({ name: "create_transaction", arguments: {} })).isError,
    true,
  );
  assert.equal((await client.callTool({ name: "repair_sync", arguments: {} })).isError, true);
  assert.equal(calls.length, 2);
} finally {
  await client.close();
  await server.close();
}

for (const id of ["one", "../../two"]) {
  let transport;
  let closed = false;
  await queryBudget(id, "list_accounts", {}, { [id]: "encryption" }, () => ({
    connect: async (value) => {
      transport = value;
    },
    callTool: async (request) => {
      assert.equal(transport._serverParams.env.ACTUAL_BUDGET_ID, id);
      assert.equal(transport._serverParams.env.ACTUAL_READ_ONLY, "1");
      assert.equal(transport._serverParams.env.ACTUAL_ENCRYPTION_PASSWORD, "encryption");
      assert.match(transport._serverParams.env.ACTUAL_DATA_DIR, /^\/data\/budgets\/[a-f0-9]{64}$/);
      assert.deepEqual(request, { name: "list_accounts", arguments: {} });
      return { content: [] };
    },
    close: async () => {
      closed = true;
    },
  }));
  assert.equal(closed, true);
}
let closed = false;
const active = new Map();
let maximumParallel = 0;
await Promise.all(
  ["one", "one", "two"].map((id) =>
    queryBudget(id, "list_accounts", {}, {}, () => ({
      connect: async () => {
        active.set(id, (active.get(id) ?? 0) + 1);
        assert.equal(active.get(id), 1, "same-budget reads must serialize");
        maximumParallel = Math.max(
          maximumParallel,
          [...active.values()].reduce((sum, value) => sum + value, 0),
        );
      },
      callTool: async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        return { content: [] };
      },
      close: async () => {
        active.set(id, active.get(id) - 1);
      },
    })),
  ),
);
assert.equal(maximumParallel, 2, "different-budget reads may run in parallel");
await assert.rejects(
  queryBudget("one", "list_accounts", {}, {}, () => ({
    connect: async () => {
      throw new Error("Startup failed");
    },
    close: async () => {
      closed = true;
    },
  })),
);
assert.equal(closed, true);

const token = "synthetic-token-for-offline-tests-only";
const projection = await mkdtemp("/app/bridge/..projected-");
const projectedEntry = "/app/bridge/projected-server.mjs";
await copyFile(fileURLToPath(new URL("./server.mjs", import.meta.url)), projection + "/server.mjs");
await symlink(projection.split("/").pop() + "/server.mjs", projectedEntry);
const child = spawn(process.execPath, ["--preserve-symlinks-main", projectedEntry], {
  env: { ...process.env, ACTUAL_SESSION_TOKEN: "synthetic-session", ACTUAL_MCP_TOKEN: token },
  stdio: "ignore",
});
const httpClient = new Client({ name: "http-test", version: "1" });
const endpoint = new URL("http://127.0.0.1:8080/mcp");
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      const response = await fetch(endpoint);
      assert.equal(response.status, 401);
      ready = true;
      break;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
  assert.equal(ready, true, "HTTP listener did not start");
  assert.equal(
    (await fetch(endpoint, { headers: { Authorization: "Bearer incorrect" } })).status,
    401,
  );
  const headers = { Authorization: "Bearer " + token };
  assert.equal(
    await new Promise((resolve, reject) => {
      get(endpoint, { headers: { ...headers, Host: "foreign.example" } }, (response) => {
        response.resume();
        resolve(response.statusCode);
      }).on("error", reject);
    }),
    403,
  );
  assert.equal(
    (await fetch(endpoint, { headers: { ...headers, Origin: "https://foreign.example" } })).status,
    403,
  );
  await httpClient.connect(
    new StreamableHTTPClientTransport(endpoint, { requestInit: { headers } }),
  );
  assert.equal((await httpClient.listTools()).tools.length, 16);
} finally {
  await httpClient.close();
  if (child.exitCode === null) {
    const stopped = new Promise((resolve) => child.once("exit", resolve));
    child.kill("SIGTERM");
    await stopped;
  }
  await unlink(projectedEntry);
  await unlink(projection + "/server.mjs");
  await rmdir(projection);
}
console.log(
  "Actual MCP checks passed: read-only discovery, authorization, all-budget selection, concurrent isolation, and child cleanup.",
);
