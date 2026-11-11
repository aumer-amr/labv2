#!/usr/bin/env node
// Run after compiling the pinned Action with tsc -p tsconfig.test.json.
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
const { runToolHarness, resolveLoopLimits } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/harness.js"),
);
const { resolveToolMaxRequests } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/budget.js"),
);
const { adaptiveLoopBudgets } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/loop.js"),
);

for (const [changedFiles, changedLines, requests, rounds] of [
  [3, 120, 24, 24],
  [16, 2000, 24, 24],
  [54, 4527, 26, 26],
  [400, 40000, 50, 32],
]) {
  const size = { changedFiles, changedLines, specialistLeads: 0 };
  const env = { TOOL_LOOP_WALL_CLOCK_SEC: "1200" };
  const resolved = resolveToolMaxRequests("primary", env, size);
  const [configuredRounds, wallClock, explicit] = resolveLoopLimits(env, "primary");
  const limits = adaptiveLoopBudgets(configuredRounds, resolved.budget, wallClock, explicit);
  assert.equal(resolved.budget, requests);
  assert.equal(resolved.configured, null);
  assert.equal(limits.maxRounds, rounds);
  assert.equal(limits.wallClockSec, 1200);
  // The old override blocks scaling even when a PR needs 16 reads plus another call.
  assert.equal(resolveToolMaxRequests("primary", { TOOL_MAX_REQUESTS: "16" }, size).budget, 16);
}
assert.equal(adaptiveLoopBudgets(4, 24, 900, true).maxRounds, 8);

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
    await checkHeadReads();
    await checkRuntimeLimits();
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

async function checkRuntimeLimits() {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "review-limits-"));
  fs.writeFileSync(path.join(cwd, "evidence.txt"), "preserved evidence\n");
  const call = (id) => ({
    choices: [
      {
        message: {
          tool_calls: [
            {
              id,
              type: "function",
              function: {
                name: "read_file",
                arguments: JSON.stringify({ path: "evidence.txt" }),
              },
            },
          ],
        },
      },
    ],
  });
  const verdict = text(
    JSON.stringify({ verdict: "approve", review_markdown: "Fixture", findings: [] }),
  );
  async function run({
    env = {},
    corpus = "# PR Diff\n+ change\n",
    respond,
    tier = "primary",
    staleResponse = false,
    files,
  } = {}) {
    let clock = 0;
    const logs = [];
    const requests = [];
    const artifacts = new Map([
      [`review-corpus${tier === "smart" ? ".smart" : ""}.truncated.md`, corpus],
    ]);
    if (staleResponse) artifacts.set(`ai-response.${tier}.json`, "stale verdict");
    if (files)
      artifacts.set(
        "pr-files.json",
        JSON.stringify(files.map((filename) => ({ filename, status: "modified" }))),
      );
    const { result } = await runToolHarness({
      env: {
        REPO: repo,
        AI_BASE_URL: "http://model.test/v1",
        AI_MODEL: "fixture",
        AI_API_KEY: secret,
        GH_TOKEN: secret,
        AI_STREAM: "false",
        SYSTEM_PROMPT: "Review fixture",
        MODEL_CONTEXT_TOKENS: "32768",
        AI_MAX_TOKENS: "4096",
        TOOL_MAX_TOKENS_PER_TURN: "1024",
        TOOL_LOOP_WALL_CLOCK_SEC: "1200",
        TOOL_TURN_TIMEOUT_SEC: "600",
        AI_REQUEST_TIMEOUT_SEC: "1800",
        TOOL_HARNESS_TIER: tier,
        SMART_BASE_URL: "http://model.test/v1",
        SMART_MODEL: "fixture",
        SMART_API_KEY: secret,
        ...env,
      },
      cwd,
      readText: (name) => artifacts.get(name) ?? null,
      exists: (name) => artifacts.has(name),
      writeArtifact: (name, value) => artifacts.set(name, value),
      deleteArtifact: (name) => artifacts.delete(name),
      timeFn: () => clock,
      log: (line) => logs.push(line),
      transport: async (_base, _format, payload, _key, timeout) => {
        requests.push({ payload, timeout });
        const n = requests.length;
        if (respond)
          return respond({
            n,
            payload,
            timeout,
            advance: (seconds) => {
              clock += seconds;
            },
          });
        return [call("c1"), text("done"), verdict][n - 1];
      },
    });
    return { result, requests, artifacts, logs };
  }
  try {
    const normal = await run();
    assert.deepEqual(
      normal.requests.map((r) => r.timeout),
      [600, 600, 1800],
    );
    assert.equal(normal.result.native_loop_verdict_produced, true);
    assert.equal(normal.result.executed_request_count, 1);
    const context = normal.result.native_loop_verdict_context;
    assert.equal(
      context.request_bytes,
      Buffer.byteLength(JSON.stringify(normal.requests[2].payload)),
    );
    assert.equal(context.input_token_budget, 32768 - 4096 - 2000);
    assert.ok(normal.logs.some((line) => line.includes('"stage":"verdict"')));
    assert.ok(!normal.logs.join("\n").includes("preserved evidence"));

    const alternateTokens = await run({ env: { AI_TOKENS_PARAM: "max_completion_tokens" } });
    assert.equal(alternateTokens.requests[2].payload.max_completion_tokens, 4096);
    assert.equal(alternateTokens.result.native_loop_verdict_context.output_tokens, 4096);
    const undeclared = await run({ env: { MODEL_CONTEXT_TOKENS: "" } });
    assert.equal(undeclared.result.native_loop_verdict_produced, true);
    assert.equal(undeclared.result.native_loop_verdict_context.input_token_budget, null);

    // Budget the rendered request, including system text and multibyte content.
    for (const options of [
      { corpus: "# PR Diff\n" + "界".repeat(40000) },
      { env: { SYSTEM_PROMPT: "界".repeat(30000) } },
      { env: { PRIMARY_MODEL_CONTEXT_TOKENS: "11000", AI_MAX_TOKENS: "8192" } },
    ]) {
      const oversized = await run({ ...options, staleResponse: true });
      assert.equal(oversized.requests.length, 2, "Do not send an oversized native verdict");
      assert.equal(oversized.result.native_loop_verdict_reason, "context-budget");
      assert.equal(oversized.result.native_loop_verdict_produced, undefined);
      assert.equal(oversized.result.executed_request_count, 1);
      assert.match(oversized.artifacts.get("tool-harness.md"), /preserved evidence/);
      assert.equal(oversized.artifacts.has("ai-response.primary.json"), false);
      assert.ok(oversized.logs.some((line) => line.includes("context-budget")));
    }

    const boundaryEnv = { SYSTEM_PROMPT: "Review fixture " + "x".repeat(24000) };
    const measured = await run({ env: boundaryEnv });
    const window =
      Math.ceil(measured.result.native_loop_verdict_context.request_bytes / 3) + 4096 + 2000;
    const atBoundary = await run({ env: { ...boundaryEnv, MODEL_CONTEXT_TOKENS: String(window) } });
    assert.equal(atBoundary.result.native_loop_verdict_produced, true);
    const overBoundary = await run({
      env: { ...boundaryEnv, MODEL_CONTEXT_TOKENS: String(window - 1) },
    });
    assert.equal(overBoundary.result.native_loop_verdict_reason, "context-budget");

    const exhausted = await run({
      files: ["evidence.txt", "unread.txt"],
      respond: ({ n, timeout, advance }) => {
        if (n === 1) {
          advance(590);
          return call("c1");
        }
        if (n === 2) {
          advance(590);
          return call("c2");
        }
        if (n === 3) {
          assert.equal(timeout, 20);
          advance(20);
          throw new Error("model request timed out");
        }
        assert.equal(n, 4, "The evidence loop must stop at its deadline");
        assert.equal(timeout, 1800, "Primary synthesis gets its own review deadline");
        advance(700);
        return verdict;
      },
    });
    assert.equal(exhausted.result.stop_reason, "wall-clock-exceeded");
    assert.equal(exhausted.result.native_loop_verdict_produced, true);
    assert.deepEqual(exhausted.result.partial_coverage.unread_files, ["unread.txt"]);
    assert.ok(exhausted.logs.some((line) => line.includes("model request timed out")));
    assert.ok(exhausted.logs.some((line) => line.includes('"timeout_sec":20')));

    // A response arriving at the deadline cannot start another evidence request.
    const late = await run({
      env: { TOOL_LOOP_WALL_CLOCK_SEC: "10" },
      respond: ({ n, advance }) => {
        if (n === 1) {
          advance(10);
          return call("c1");
        }
        return verdict;
      },
    });
    assert.equal(late.result.stop_reason, "wall-clock-exceeded");
    assert.equal(late.result.executed_request_count, 0);

    const failed = await run({
      respond: ({ n }) => {
        if (n === 1) return call("c1");
        if (n === 2) throw new Error(`transport failed ${secret}`);
        return verdict;
      },
    });
    assert.equal(failed.result.stop_reason, "request-error");
    assert.ok(failed.logs.some((line) => line.includes("transport failed")));
    assert.ok(!JSON.stringify(failed.logs).includes(secret));
    assert.ok(!JSON.stringify(failed.result).includes(secret));
    assert.ok(![...failed.artifacts.values()].join("\n").includes(secret));

    const smart = await run({
      tier: "smart",
      env: { SMART_TOOL_LOOP_WALL_CLOCK_SEC: "10" },
      respond: ({ timeout, advance }) => {
        assert.equal(timeout, 10);
        advance(10);
        throw new Error("model request timed out");
      },
    });
    assert.equal(smart.requests.length, 1, "Smart tier must retain its total deadline");
    assert.equal(smart.result.stop_reason, "wall-clock-exceeded");
    assert.equal(smart.result.native_loop_verdict_produced, undefined);

    const streamed = await run({
      env: { AI_STREAM: "true" },
      respond: ({ n, timeout, payload, advance }) => {
        if (n === 1) return call("c1");
        if (n === 2) return text("done");
        if (n === 3) {
          assert.equal(timeout, 1800);
          assert.equal(payload.stream, true);
          advance(1700);
          throw new Error(`stream failed ${secret}`);
        }
        assert.equal(n, 4);
        assert.equal(timeout, 100, "The verdict retry shares the review deadline");
        assert.equal(payload.stream, false);
        return verdict;
      },
    });
    assert.equal(streamed.result.native_loop_verdict_produced, true);
    assert.equal(streamed.result.native_loop_verdict_retried, true);
    assert.ok(!streamed.logs.join("\n").includes(secret));

    const anthropic = await run({
      env: { AI_API_FORMAT: "anthropic" },
      respond: ({ n, payload, timeout }) => {
        if (n === 1)
          return {
            content: [
              { type: "tool_use", id: "c1", name: "read_file", input: { path: "evidence.txt" } },
            ],
          };
        if (n === 2) return { content: [{ type: "text", text: "done" }] };
        assert.equal(timeout, 1800);
        assert.equal(payload.max_tokens, 4096);
        assert.equal(payload.tools, undefined);
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                verdict: "approve",
                review_markdown: "Fixture",
                findings: [],
              }),
            },
          ],
        };
      },
    });
    assert.equal(anthropic.result.native_loop_verdict_produced, true);
    assert.equal(anthropic.result.executed_request_count, 1);
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
}

async function checkHeadReads() {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "review-base-"));
  try {
    fs.writeFileSync(path.join(cwd, "modified.yaml"), "trusted base version\n");
    fs.writeFileSync(path.join(cwd, "unchanged.yaml"), "trusted unchanged version\n");
    execFileSync("git", ["init", "--quiet", cwd]);
    execFileSync("git", ["-C", cwd, "add", "modified.yaml", "unchanged.yaml"]);
    const cases = [
      { name: "added", file: "added.yaml", status: "added" },
      { name: "modified", file: "modified.yaml", status: "modified" },
      {
        name: "relative-alias",
        file: "modified.yaml",
        status: "modified",
        args: { path: "././modified.yaml" },
      },
      {
        name: "absolute-alias",
        file: "modified.yaml",
        status: "modified",
        args: { path: path.join(cwd, "modified.yaml") },
      },
      {
        name: "cap",
        file: "added.yaml",
        status: "added",
        body: "head first line\n" + "x".repeat(11970) + `\napi_key: ${secret}\n`,
      },
      { name: "range", file: "modified.yaml", status: "modified", args: { offset: 2, limit: 1 } },
      { name: "unchanged", file: "unchanged.yaml", local: true },
      {
        name: "standalone",
        file: "modified.yaml",
        status: "modified",
        local: true,
        standalone: true,
      },
      {
        name: "forbidden",
        file: "modified.yaml",
        status: "modified",
        httpStatus: 403,
        error: true,
      },
      { name: "missing", file: "modified.yaml", status: "modified", httpStatus: 404, error: true },
      { name: "redirect", file: "modified.yaml", status: "modified", httpStatus: 302, error: true },
      { name: "symlink", file: "added.yaml", status: "added", type: "symlink", error: true },
      { name: "binary", file: "added.yaml", status: "added", body: "\0binary", error: true },
      { name: "sensitive", file: "age.key", status: "added", error: true, noNetwork: true },
      { name: "traversal", file: "../outside.yaml", status: "added", error: true, noNetwork: true },
      {
        name: "invalid-head",
        file: "modified.yaml",
        status: "modified",
        sha: "main",
        error: true,
        noNetwork: true,
      },
      {
        name: "missing-head",
        file: "modified.yaml",
        status: "modified",
        sha: "",
        error: true,
        noNetwork: true,
      },
    ];
    for (const fixture of cases) {
      const reads = [];
      const logs = [];
      const sha = fixture.sha ?? head;
      const artifacts = new Map([
        ["review-corpus.truncated.md", "# PR Diff\n+ fixture"],
        [
          "pr-files.json",
          JSON.stringify(
            [{ filename: fixture.file, status: fixture.status ?? "unchanged" }].filter(
              (f) => f.status !== "unchanged",
            ),
          ),
        ],
      ]);
      global.fetch = async (url, init) => {
        reads.push(String(url));
        const parsed = new URL(url);
        assert.equal(parsed.origin, "https://api.github.com");
        assert.ok(parsed.pathname.startsWith(`/repos/${repo}/`));
        assert.equal(init.method, "GET");
        assert.equal(init.redirect, "manual");
        if (fixture.httpStatus) return new Response("{}", { status: fixture.httpStatus });
        if (parsed.pathname.includes("/contents")) {
          assert.equal(parsed.searchParams.get("ref"), head);
          return Response.json([
            { name: fixture.file, type: fixture.type ?? "file", sha: "a".repeat(40) },
          ]);
        }
        assert.equal(parsed.pathname, `/repos/${repo}/git/blobs/${"a".repeat(40)}`);
        return Response.json({
          encoding: "base64",
          content: Buffer.from(
            fixture.body ?? `head first line\nhead second line\napi_key: ${secret}\n`,
          ).toString("base64"),
        });
      };
      const responses = [
        {
          choices: [
            {
              message: {
                content: null,
                tool_calls: [
                  {
                    id: "read-head",
                    type: "function",
                    function: {
                      name: "read_file",
                      arguments: JSON.stringify({ path: fixture.file, ...fixture.args }),
                    },
                  },
                ],
              },
            },
          ],
        },
        text("Evidence collection complete"),
        text(
          JSON.stringify({ verdict: "approve", review_markdown: "Fixture review", findings: [] }),
        ),
      ];
      const { result } = await runToolHarness({
        env: {
          REPO: repo,
          ...(fixture.standalone ? {} : { PR_HEAD_SHA: sha }),
          GH_TOKEN: secret,
          AI_BASE_URL: "http://model.test/v1",
          AI_MODEL: "fixture",
          AI_API_KEY: secret,
          AI_STREAM: "false",
          SYSTEM_PROMPT: "Review fixture",
          TOOL_MAX_REQUESTS: "16",
          TOOL_MAX_RESPONSE_BYTES: "12000",
        },
        cwd,
        readText: (name) => artifacts.get(name) ?? null,
        exists: (name) => artifacts.has(name),
        writeArtifact: (name, value) => artifacts.set(name, value),
        deleteArtifact: (name) => artifacts.delete(name),
        transport: async () => responses.shift(),
        timeFn: () => 0,
        log: (line) => logs.push(line),
      });
      assert.equal(result.tool_results.length, 1, fixture.name);
      const call = result.tool_results[0];
      assert.equal(call.status, fixture.error ? "error" : "ok", fixture.name);
      assert.equal(result.executed_request_count, fixture.error ? 0 : 1, fixture.name);
      assert.ok(!JSON.stringify(result.tool_results).includes(secret), fixture.name);
      assert.ok(!logs.join("\n").includes(secret), fixture.name);
      if (fixture.local || fixture.noNetwork) assert.equal(reads.length, 0, fixture.name);
      if (fixture.local) {
        assert.equal(
          call.result.content,
          fixture.standalone ? "trusted base version\n" : "trusted unchanged version\n",
        );
        assert.equal(call.result.provenance, undefined);
      } else if (!fixture.error) {
        assert.deepEqual(call.result.provenance, {
          source: "github_pr_head",
          ref: head,
          untrusted: true,
        });
        assert.ok(
          call.result.content.includes(
            fixture.args?.offset ? "head second line" : "head first line",
          ),
        );
        assert.ok(!call.result.content.includes("trusted base version"));
        assert.equal(reads.length, 2);
        assert.ok(Buffer.byteLength(call.result.content.replace(/\n\[truncated\]$/, "")) <= 12000);
        assert.ok(
          !fs.existsSync(path.join(cwd, "added.yaml")),
          "Head files must not enter the checkout",
        );
        if (fixture.args?.offset) {
          assert.equal(call.result.content, "head second line\n");
          assert.equal(call.result.range.offset, 2);
          assert.equal(call.result.range.lines, 1);
        }
      }
    }
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
}
