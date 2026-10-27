const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const source = readFileSync(`${__dirname}/monitor-review.js`, "utf8");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.disabled = false;
    this.text = "";
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(" "); }
  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name === "data-index") this.dataset.index = value;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  querySelectorAll(tag) {
    return this.children.flatMap((child) => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]);
  }
  click() { return this.disabled ? undefined : this.listeners.click?.(); }
}

async function page({ paused = false, expired = false, failure = false, authFailure = false } = {}) {
  const root = new Element("div");
  const submitted = [];
  const draft = {
    id: "observation", hash: "reviewed-hash", status: "pending",
    expires_at: Date.now() / 1000 + (expired ? -1 : 3600),
    payload: { title: "Storage growth", description: '<img src=x onerror="alert(1)">Evidence shown to reviewer' },
  };
  let args;
  const response = (value) => ({ ok: true, text: async () => JSON.stringify(value) });
  const fetch = async (url, options) => {
    assert.ok(url.startsWith("https://windmill.example.test/api/w/cluster/"));
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.redirect, "error");
    if (authFailure) return { ok: false, status: 401 };
    if (url.includes("/jobs/run/p/")) {
      args = JSON.parse(options.body);
      submitted.push(args);
      return response("00000000-0000-0000-0000-000000000001");
    }
    if (args.action === "drafts") return response({ success: true, result: { paused, drafts: [draft] } });
    if (failure) return response({ success: false, result: { error: { message: "Draft changed. Review it again." } } });
    return response({ success: true, result: args.action === "approve" ?
      { status: "published", task_id: 101 } : { status: "rejected" } });
  };
  vm.runInNewContext(source, {
    document: { getElementById: () => root, createElement: (tag) => new Element(tag), head: new Element("head") },
    window: { ctx: { workspace: "cluster" }, location: { origin: "https://windmill.example.test" } },
    fetch, URL, AbortSignal, Date, setTimeout,
  });
  await new Promise(setImmediate);
  return { root, submitted, draft, buttons: () => root.querySelectorAll("button") };
}

test("Approve carries the displayed ID/hash, prevents duplicate clicks, and shows task link", async () => {
  const p = await page();
  assert.equal(p.root.querySelectorAll("input").length, 0);
  assert.equal(p.root.querySelectorAll("img").length, 0);
  assert.ok(p.root.textContent.includes("Evidence shown to reviewer"));
  p.draft.hash = "changed-after-review";
  const approve = p.buttons().find((b) => b.attributes.class === "approve");
  await Promise.all([approve.click(), approve.click()]);
  assert.deepEqual(p.submitted, [{ action: "drafts" }, {
    action: "approve", draft_id: "observation", draft_hash: "reviewed-hash", approve: true,
  }]);
  assert.equal(p.root.querySelectorAll("a")[0].attributes.href, "https://vikunja.aumer.dev/tasks/101");
  assert.ok(p.root.textContent.includes("Task confirmed"));
  assert.ok(p.buttons().filter((b) => b.dataset.index !== undefined).every((b) => b.disabled));
});

test("Reject uses the same displayed snapshot and reports rejection", async () => {
  const p = await page();
  await p.buttons().find((b) => b.attributes.class === "reject").click();
  assert.deepEqual(p.submitted[1], {
    action: "reject", draft_id: "observation", draft_hash: "reviewed-hash", approve: false,
  });
  assert.equal(p.root.querySelectorAll("a").length, 0);
  assert.ok(p.root.textContent.includes("Draft rejected"));
});

test("Paused and expired drafts cannot submit publication", async () => {
  for (const options of [{ paused: true }, { expired: true }]) {
    const p = await page(options);
    const approve = p.buttons().find((b) => b.attributes.class === "approve");
    assert.equal(approve.disabled, true);
    await approve.click();
    assert.equal(p.submitted.length, 1);
  }
});

test("Changed evidence errors are visible and require refresh without automatic retry", async () => {
  const p = await page({ failure: true });
  await p.buttons().find((b) => b.attributes.class === "approve").click();
  assert.ok(p.root.textContent.includes("Draft changed. Review it again."));
  assert.ok(p.root.textContent.includes("Action not confirmed"));
  assert.equal(p.submitted.length, 2);
  assert.ok(p.buttons().filter((b) => b.dataset.index !== undefined).every((b) => b.disabled));
  assert.equal(p.root.querySelectorAll("a").length, 0);
});

test("Missing login is explained and never submits an approval", async () => {
  const p = await page({ authFailure: true });
  assert.ok(p.root.textContent.includes("Sign in with the configured Windmill administrator"));
  assert.equal(p.submitted.length, 0);
});
