#!/usr/bin/env node
// Run after compiling the pinned Action with tsc -p tsconfig.test.json.
const assert = require("node:assert/strict");
const path = require("node:path");
const { runToolHarness } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/harness.js"),
);

const head = "8d566e01e5f8cc98562f29d430f27ce47bd29e95";
const file = "kubernetes/apps/ai/llmkube/models/qwen3-5-9b.yaml";
const repo = "aumer-amr/labv2";
const secret = "dummy-private-credential-for-regression";
const originalFetch = global.fetch;
const text = (content) => ({ choices: [{ message: { content } }] });

async function check(mode) {
  const reads = [];
  const logs = [];
  const artifacts = new Map([["review-corpus.truncated.md", "# PR Diff\n+ image digest update"]]);
  global.fetch = async (url, init) => {
    reads.push(String(url));
    assert.equal(new URL(url).origin, "https://api.github.com");
    assert.equal(init.method, "GET");
    assert.equal(init.redirect, "manual");
    assert.equal(init.headers.Authorization, `Bearer ${secret}`);
    assert.ok(init.signal);
    if (mode === "redirect")
      return new Response("", {
        status: 302,
        headers: { location: `https://evil.test/${secret}` },
      });
    if (mode === "forbidden") return new Response("{}", { status: 403 });
    if (String(url).includes("/git/blobs/"))
      return Response.json({
        encoding: "base64",
        content: Buffer.from("kind: InferenceService").toString("base64"),
      });
    if (String(url).includes(`/contents/${file}?`))
      return Response.json({
        type: "file",
        encoding: "base64",
        path: file,
        content: Buffer.from("kind: InferenceService").toString("base64"),
      });
    return Response.json([{ name: "qwen3-5-9b.yaml", type: "file", sha: "a".repeat(40) }]);
  };
  const requests = [
    ["repo_contents", { repo, path: file, ref: head }],
    ["gh_api", { endpoint: `/repos/${repo}/contents/${file}?ref=${head}` }],
    ["gh_api", { endpoint: "/repos/other/private/contents/file" }],
    ["repo_contents", { repo, path: "age.key", ref: head }],
  ];
  const responses = [
    {
      choices: [
        {
          message: {
            content: null,
            tool_calls: requests.map(([name, args], i) => ({
              id: `c${i}`,
              type: "function",
              function: { name, arguments: JSON.stringify(args) },
            })),
          },
        },
      ],
    },
    text("Evidence collection complete"),
    text(JSON.stringify({ verdict: "approve", review_markdown: "Fixture review", findings: [] })),
  ];
  const { result } = await runToolHarness({
    env: {
      REPO: repo,
      GH_TOKEN: secret,
      AI_BASE_URL: "http://model.test/v1",
      AI_MODEL: "fixture",
      AI_API_KEY: secret,
      AI_STREAM: "false",
      SYSTEM_PROMPT: "Review fixture",
      TOOL_MAX_REQUESTS: "16",
    },
    cwd: "/tmp",
    readText: (name) => artifacts.get(name) ?? null,
    exists: (name) => artifacts.has(name),
    writeArtifact: (name, value) => artifacts.set(name, value),
    deleteArtifact: (name) => artifacts.delete(name),
    transport: async () => responses.shift(),
    timeFn: () => 0,
    log: (line) => logs.push(line),
  });
  assert.equal(result.tool_calls.length, 4);
  assert.equal(result.executed_request_count, mode === "ok" ? 2 : 0);
  assert.deepEqual(
    result.tool_calls.map((call) => call.status),
    mode === "ok" ? ["ok", "ok", "error", "error"] : ["error", "error", "error", "error"],
  );
  assert.equal(reads.length, mode === "ok" ? 3 : 2);
  assert.ok(
    reads.filter((url) => url.includes("/contents/")).every((url) => url.endsWith(`?ref=${head}`)),
  );
  const diagnostic = logs.find((line) => line.includes("evidence outcomes:"));
  assert.ok(diagnostic, "per-call outcomes must survive runner cleanup in the job log");
  assert.ok(!diagnostic.includes(secret));
  assert.ok(!diagnostic.includes("kind: InferenceService"));
  assert.ok(!diagnostic.includes(file));
  const outcomes = JSON.parse(diagnostic.split("evidence outcomes: ")[1]);
  assert.equal(outcomes.issued, 4);
  assert.equal(outcomes.succeeded, mode === "ok" ? 2 : 0);
  assert.equal(outcomes.failed, mode === "ok" ? 2 : 4);
  if (mode === "forbidden") assert.match(outcomes.calls[0].error, /HTTP 403/);
  if (mode === "redirect") assert.match(outcomes.calls[0].error, /redirect-blocked/);
}

(async () => {
  try {
    for (const mode of ["ok", "redirect", "forbidden"]) await check(mode);
    console.log(
      "Evidence transport, exact-head reads, security boundaries, and diagnostics passed",
    );
  } finally {
    global.fetch = originalFetch;
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
