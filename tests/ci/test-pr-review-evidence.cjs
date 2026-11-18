#!/usr/bin/env node
// Run after compiling the pinned Action with tsc -p tsconfig.test.json.
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
const { createServer } = require("node:http");
const { runToolHarness, resolveLoopLimits } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/harness.js"),
);
const { resolveToolMaxRequests } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/budget.js"),
);
const { adaptiveLoopBudgets } = require(
  path.resolve(process.argv[2], ".test-build/src/tools/loop.js"),
);
const { applyToolHarnessFailureEnforcement, applyToolMinSuccessfulEnforcement } = require(
  path.resolve(process.argv[2], ".test-build/src/enforcement/enforce.js"),
);
const { actionMain } = require(path.resolve(process.argv[2], ".test-build/src/run/action.js"));

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
  assert.equal(limits.wallClockSec, 900);
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
    for (const mode of ["no-call", "denied", "mixed", "unrecovered", "no-evidence"])
      await checkPlanning(mode);
    await checkHeadReads();
    await checkDispatch();
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

async function checkDispatch() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "review-dispatch-"));
  const workspace = path.join(root, "trusted");
  const modelRequests = [];
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    modelRequests.push(body);
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify({
      verdict: "approve", review_markdown: "Fixture review", findings: [],
    }) }, finish_reason: "stop" }] }));
  });
  try {
    fs.mkdirSync(workspace);
    execFileSync("git", ["init", "--quiet", workspace]);
    execFileSync("git", ["-C", workspace, "-c", "user.name=fixture", "-c", "user.email=test@example.com",
      "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "trusted base"]);
    const base = execFileSync("git", ["-C", workspace, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
    const model = `http://127.0.0.1:${server.address().port}`;
    fs.writeFileSync(path.join(workspace, "providers.json"), JSON.stringify({
      providers: [{ id: "pin-fixture", command: [process.execPath, "evidence.cjs"] }],
    }));
    fs.writeFileSync(path.join(workspace, "evidence.cjs"), `
      const assert = require("node:assert/strict");
      assert.equal(process.env.PR_HEAD_SHA, ${JSON.stringify(head)});
      assert.equal(process.env.PR_REVIEWER_BASE_REF, ${JSON.stringify(base)});
      assert.equal(process.env.PR_NUMBER, "7");
      assert.equal(process.env.AI_API_KEY, undefined);
      console.log(JSON.stringify({ findings: [{ message: "trusted evidence fixture" }] }));
    `);
    const eventPath = path.join(root, "event.json");
    fs.writeFileSync(eventPath, JSON.stringify({ inputs: { pr_number: "7" } }));
    let state = "open";
    let baseBranch = "main";
    global.fetch = async (url, init) => {
      const req = new URL(url);
      assert.equal(req.origin, "https://api.github.com");
      if (req.pathname === "/repos/o/r/pulls/7") {
        const accept = new Headers(init.headers).get("Accept");
        if (accept === "application/vnd.github.v3.diff") {
          return new Response("diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hello\n+world\n");
        }
        return Response.json({
          number: 7, state, draft: false, title: "Fixture", body: "",
          base: { sha: base, ref: baseBranch, repo: { full_name: "o/r" } },
          head: { sha: head, ref: "renovate/chart", repo: { full_name: "o/r" } },
          user: { login: "labv2-bot[bot]" }, changed_files: 1,
        });
      }
      if (req.pathname === "/repos/o/r/pulls/7/files") {
        return Response.json([{ filename: "README.md", status: "modified" }]);
      }
      if (/\/(comments|reviews)$/.test(req.pathname)) return Response.json([]);
      return Response.json({}, { status: 404 });
    };
    const output = path.join(root, "output");
    for (const [basePin, headPin, currentState, currentBranch, skip] of [
      [base, "c".repeat(40), "open", "main", "superseded-head"],
      ["c".repeat(40), head, "open", "main", "superseded-base"],
      [base, head, "closed", "main", "manual-pr-ineligible"],
      [base, head, "open", "release", "manual-pr-ineligible"],
      [base, head, "open", "main", null],
    ]) {
      state = currentState;
      baseBranch = currentBranch;
      fs.rmSync(output, { force: true });
      assert.equal(await actionMain({
        PATH: process.env.PATH,
        HOME: process.env.HOME,
        GITHUB_ACTIONS: "true",
        GITHUB_EVENT_NAME: "workflow_dispatch",
        GITHUB_EVENT_PATH: eventPath,
        GITHUB_REPOSITORY: "o/r",
        GITHUB_WORKSPACE: workspace,
        GITHUB_API_URL: "https://api.github.com",
        GITHUB_SERVER_URL: "https://github.com",
        RUNNER_TEMP: root,
        GITHUB_OUTPUT: output,
        LABV2_REVIEW_HEAD_SHA: headPin,
        LABV2_REVIEW_BASE_SHA: basePin,
        "INPUT_GITHUB-TOKEN": "fixture",
        "INPUT_PR-NUMBER": "7",
        "INPUT_FORCE-REVIEW": "true",
        "INPUT_AI-BASE-URL": model,
        "INPUT_AI-MODEL": "fixture",
        "INPUT_AI-API-KEY": "fixture-key",
        "INPUT_AI-REQUEST-TIMEOUT-SEC": "5",
        "INPUT_AI-PRIMARY-RETRIES": "1",
        "INPUT_AI-STREAM": "false",
        "INPUT_TOOL-MODE": "off",
        "INPUT_DEEP-REVIEW": "false",
        "INPUT_CI-STATUS-CHECK": "false",
        "INPUT_EVIDENCE-PROVIDERS-FILE": "providers.json",
        "INPUT_PUBLISH-MODE": "comment",
      }), 0);
      const result = fs.readFileSync(output, "utf8");
      if (skip) {
        assert.match(result, new RegExp(`skip-reason=${skip}`));
        assert.equal(modelRequests.length, 0);
      } else {
        assert.match(result, /should-review=true/);
        const run = fs.readdirSync(root).find((name) => name.startsWith("v3-review-run-"));
        assert.ok(run);
        assert.match(fs.readFileSync(path.join(root, run, "evidence-providers.md"), "utf8"), /trusted evidence fixture/);
        assert.ok(modelRequests.some((body) => body.includes("trusted evidence fixture")));
      }
    }
  } finally {
    if (server.listening) await new Promise((resolve) => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function checkPlanning(mode) {
  const corpus = "# PR Diff\n+ Update Grafana MCP\n\n# Tool Harness Findings\nPending\n";
  const artifacts = new Map([["review-corpus.truncated.md", corpus]]);
  const releaseUrl = "https://api.github.com/repos/grafana/mcp-grafana/releases/tags/v1.0.0";
  const denied = ["gh_api", { endpoint: new URL(releaseUrl).pathname }];
  const publicRead = ["web_fetch", { url: releaseUrl }];
  const calls = (...requests) => ({
    choices: [
      {
        message: {
          tool_calls: requests.map(([name, args], i) => ({
            id: `${requests.length}-${name}-${i}`,
            type: "function",
            function: { name, arguments: JSON.stringify(args) },
          })),
        },
      },
    ],
  });
  const stop = text("Evidence collection complete");
  const startsWithoutCall = mode === "no-call" || mode === "no-evidence";
  const recovers = mode === "no-call" || mode === "denied";
  const requests = [];
  const responses = [
    startsWithoutCall ? stop : mode === "mixed" ? calls(denied, publicRead) : calls(denied),
    ...(startsWithoutCall ? [] : [stop]),
    ...(recovers ? [calls(publicRead), stop] : []),
    ...(mode === "unrecovered" || mode === "no-evidence" ? [stop] : []),
    ...(mode === "no-evidence"
      ? []
      : [
          text(
            JSON.stringify({ verdict: "approve", review_markdown: "Fixture review", findings: [] }),
          ),
        ]),
  ];
  let fetched = 0;
  global.fetch = async (url, init) => {
    assert.equal(String(url), releaseUrl);
    assert.equal(new Headers(init.headers).has("Authorization"), false);
    fetched++;
    return Response.json({ body: "Grafana MCP release notes" });
  };
  const { result } = await runToolHarness({
    env: {
      REPO: repo,
      GH_TOKEN: secret,
      AI_BASE_URL: "http://model.test/v1",
      AI_MODEL: "fixture",
      AI_API_KEY: secret,
      AI_STREAM: "false",
      SYSTEM_PROMPT: "Review fixture",
      ALLOWED_SOURCE_HOSTS: "github.com,api.github.com",
      TOOL_MIN_SUCCESSFUL_REQUESTS: "1",
    },
    cwd: "/tmp",
    readText: (name) => artifacts.get(name) ?? null,
    exists: (name) => artifacts.has(name),
    writeArtifact: (name, value) => artifacts.set(name, value),
    deleteArtifact: (name) => artifacts.delete(name),
    transport: async (_url, _format, request) => {
      requests.push(JSON.stringify(request));
      assert.ok(responses.length, "Unexpected extra model turn");
      return responses.shift();
    },
    timeFn: () => 0,
  });
  const success = recovers || mode === "mixed" ? 1 : 0;
  const failure = startsWithoutCall ? 0 : 1;
  assert.equal(result.executed_request_count, success, mode);
  assert.equal(fetched, success, mode);
  assert.equal(responses.length, 0, mode);
  assert.match(requests[0], /For public upstream evidence, use web_fetch with a listed host/);
  const harnessMarkdown = String(artifacts.get("tool-harness.md"));
  if (mode === "no-evidence") {
    assert.equal(result.stop_reason, "no-tool-calls");
    assert.match(harnessMarkdown, /no evidence gathered/);
  } else {
    const counts = new RegExp(`${success} succeeded; ${failure} failed`);
    assert.match(requests.at(-1), counts);
    assert.match(harnessMarkdown, counts);
  }
  assert.equal(
    requests.filter((request) => request.includes("No tool request has succeeded")).length,
    mode === "mixed" ? 0 : 1,
  );
  const artifact = { verdict: "approve", review_markdown: "Fixture review" };
  assert.equal(
    applyToolHarnessFailureEnforcement(artifact, result).applied,
    mode === "unrecovered",
  );
  assert.equal(applyToolMinSuccessfulEnforcement(artifact, 1, result).applied, success === 0);
  assert.equal(artifact.verdict, success ? "approve" : "request_changes");
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
