(() => {
  "use strict";

  const root = document.getElementById("root");
  const workspace = window.ctx?.workspace;
  let busy = false;
  let needsRefresh = false;
  let listing = { paused: true, drafts: [] };

  const style = document.createElement("style");
  style.textContent = `
    :root { color-scheme: dark; font: 16px/1.5 system-ui, sans-serif; color: #d4d7dd; background: #2e3441; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    main { max-width: 960px; margin: auto; padding: 24px; }
    h1 { margin: 0; font-size: 26px; } h2 { margin: 0; font-size: 18px; overflow-wrap: anywhere; }
    p { margin: 8px 0 16px; } header, .actions { display: flex; align-items: center; gap: 12px; }
    header { justify-content: space-between; margin-bottom: 20px; }
    article { background: #252b36; border: 1px solid #4b5563; border-radius: 10px; padding: 20px; margin: 16px 0; }
    button, a { font: inherit; } button { cursor: pointer; padding: 9px 14px; border-radius: 6px; border: 1px solid #667085; background: #384152; color: #e5e7eb; }
    button.approve { background: #166534; border-color: #166534; color: white; }
    button:disabled { opacity: .5; cursor: default; } button:focus-visible, a:focus-visible, summary:focus-visible { outline: 3px solid #93c5fd; outline-offset: 3px; }
    summary { cursor: pointer; padding: 8px 0; font-weight: 600; }
    pre { font: 14px/1.6 system-ui, sans-serif; white-space: pre-wrap; overflow-wrap: anywhere; margin: 12px 0 20px; }
    .meta { color: #aab4c3; font-size: 14px; margin: 8px 0; } [role="alert"] { color: #fda4af; }
    a { color: #93c5fd; } .notice { background: #393522; border-radius: 6px; padding: 12px; }
    @media (max-width: 600px) { main { padding: 16px; } header { align-items: flex-start; } article { padding: 16px; } }
  `;
  document.head.append(style);
  document.title = "Monitoring review";

  function element(tag, text, attributes = {}) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    return node;
  }

  const main = element("main");
  const header = element("header");
  const refresh = element("button", "Refresh drafts", { type: "button" });
  header.append(element("h1", "Monitoring review"), refresh);
  const notice = element("p", "", { class: "notice" });
  const status = element("p", "Loading drafts…", { role: "status", "aria-live": "polite" });
  const error = element("p", "", { role: "alert" });
  const cards = element("section", undefined, { "aria-label": "Monitoring drafts" });
  main.append(header, element("p", "Review the evidence, then approve publication to LabV2 or reject the draft."), notice, status, error, cards);
  root.replaceChildren(main);

  async function request(path, body) {
    const response = await fetch(`${window.location.origin}/api/w/${encodeURIComponent(workspace)}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      redirect: "error",
      signal: AbortSignal.timeout(20000),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) throw new Error("Sign in with the configured Windmill administrator.");
      throw new Error(`Windmill returned HTTP ${response.status}. Refresh before retrying.`);
    }
    const text = await response.text();
    try { return JSON.parse(text); } catch { return text.trim(); }
  }

  async function run(args) {
    const job = await request("/jobs/run/p/f/cluster/monitor", args);
    if (typeof job !== "string" || !/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/i.test(job)) {
      throw new Error("Windmill did not return a run ID. Sign in and refresh before retrying.");
    }
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      const result = await request(`/jobs_u/get/${job}?no_logs=true&no_code=true`);
      if (result.success === true) return result.result;
      if (result.success === false) throw new Error(result.result?.error?.message?.slice(0, 500) || "Run failed. Refresh before retrying.");
      await new Promise((resolve) => setTimeout(resolve, 750));
    }
    throw new Error(`Run ${job} is still in progress. Check it in Windmill before retrying publication.`);
  }

  function updateButtons() {
    refresh.disabled = busy;
    for (const button of cards.querySelectorAll("button")) {
      const draft = listing.drafts[Number(button.dataset.index)];
      button.disabled = busy || needsRefresh || listing.paused !== false ||
        !["pending", "approved"].includes(draft.status) || draft.expires_at * 1000 <= Date.now();
    }
  }

  function render() {
    cards.replaceChildren();
    notice.textContent = listing.paused ? "Publication is paused. You can still review drafts; monitoring continues." :
      "Only the configured administrator can publish. Approval applies to the evidence shown here.";
    listing.drafts.forEach((draft, index) => {
      const card = element("article");
      const meta = element("p", `${draft.status} · Expires ${new Date(draft.expires_at * 1000).toLocaleString()}`, { class: "meta" });
      const details = element("details");
      details.append(element("summary", "Review evidence"), element("pre", draft.payload.description));
      const actions = element("div", undefined, { class: "actions" });
      for (const [action, label] of [["approve", "Approve and publish"], ["reject", "Reject draft"]]) {
        const button = element("button", label, { type: "button", class: action, "data-index": String(index) });
        button.addEventListener("click", () => decide(draft, action));
        actions.append(button);
      }
      details.append(actions);
      card.append(element("h2", draft.payload.title), meta, details);
      if (draft.status === "approved") card.append(element("p", "Publication is not confirmed. Review this draft and retry to recover an existing task or finish publication."));
      if (draft.task_url) {
        const task = new URL(draft.task_url);
        if (task.origin === "https://vikunja.aumer.dev" && /^\/tasks\/\d+$/.test(task.pathname)) {
          card.append(element("a", "Open Vikunja task", { href: task.href, target: "_blank", rel: "noopener noreferrer" }));
        }
      }
      cards.append(card);
    });
    if (!listing.drafts.length) cards.append(element("p", "No eligible drafts. Monitoring continues."));
    updateButtons();
  }

  async function load() {
    if (busy) return;
    busy = true;
    error.textContent = "";
    status.textContent = "Loading drafts…";
    updateButtons();
    try {
      if (workspace !== "cluster") throw new Error("Open this app in the cluster workspace after signing in to Windmill.");
      const result = await run({ action: "drafts" });
      if (!Array.isArray(result?.drafts)) throw new Error("Windmill returned an invalid draft list.");
      listing = result;
      needsRefresh = false;
      render();
      status.textContent = `${listing.drafts.filter((d) => d.status === "pending").length} pending draft(s).`;
    } catch (failure) {
      needsRefresh = true;
      error.textContent = failure.message;
      status.textContent = "Drafts could not be refreshed. No approval was sent.";
    } finally {
      busy = false;
      updateButtons();
    }
  }

  async function decide(draft, action) {
    if (busy || needsRefresh || listing.paused !== false) return;
    if (draft.expires_at * 1000 <= Date.now()) {
      error.textContent = "This draft expired. Refresh to review current evidence.";
      needsRefresh = true;
      updateButtons();
      return;
    }
    busy = true;
    error.textContent = "";
    status.textContent = action === "approve" ? "Publishing the reviewed draft…" : "Rejecting the reviewed draft…";
    updateButtons();
    try {
      const result = await run({ action, draft_id: draft.id, draft_hash: draft.hash, approve: action === "approve" });
      if (result.status === "published" && Number.isInteger(result.task_id) && result.task_id > 0) {
        draft.status = "published";
        draft.task_url = `https://vikunja.aumer.dev/tasks/${result.task_id}`;
        status.textContent = "Task confirmed in Vikunja. Open it below.";
      } else if (result.status === "rejected") {
        draft.status = "rejected";
        status.textContent = "Draft rejected. No task was published by this action.";
      } else throw new Error("Publication was not confirmed. Refresh and check the draft status before retrying.");
      render();
    } catch (failure) {
      needsRefresh = true;
      error.textContent = failure.message;
      status.textContent = "Action not confirmed. Refresh and check its status before retrying.";
    } finally {
      busy = false;
      updateButtons();
    }
  }

  refresh.addEventListener("click", load);
  load();
})();
