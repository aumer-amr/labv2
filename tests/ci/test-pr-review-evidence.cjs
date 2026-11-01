#!/usr/bin/env node
// Run after compiling the pinned Action with tsc -p tsconfig.test.json.
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { execFileSync } = require("node:child_process");
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
    await checkHeadReads();
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
