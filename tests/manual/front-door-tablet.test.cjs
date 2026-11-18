const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const repo = path.join(__dirname, "../..");
const source = fs.readFileSync(path.join(repo, "raw/home-assistant/front-door-tablet.js"), "utf8");
const config = require("../../raw/home-assistant/front-door-dashboard.json").views[0].cards[0];
const { eventRows, collectionRows } = vm.runInNewContext(
  source + "\n({ eventRows, collectionRows })",
  {
    document: { createElement: () => ({}) },
    HTMLElement: class {},
    customElements: { define() {} },
  },
);

test("the agenda uses the HA day at the DST change and excludes cancelled and expired dates", () => {
  const rows = eventRows(
    [
      {
        summary: "After midnight",
        start: { dateTime: "2026-10-24T22:15:00Z" },
        end: { dateTime: "2026-10-24T23:00:00Z" },
      },
      { summary: "All day", start: { date: "2026-10-25" }, end: { date: "2026-10-26" } },
      { summary: "Still in progress", start: { date: "2026-10-23" }, end: { date: "2026-10-26" } },
      { summary: "Expired", start: { date: "2026-10-24" }, end: { date: "2026-10-25" } },
      { summary: "Next week", start: { date: "2026-11-01" }, end: { date: "2026-11-02" } },
      { summary: "Cancelled", start: { date: "2026-10-25" }, status: "CANCELLED" },
    ],
    "2026-10-25",
    "Europe/Amsterdam",
  );
  assert.deepEqual(
    Array.from(rows, (event) => [event.name, event.day]),
    [
      ["Still in progress", "2026-10-23"],
      ["All day", "2026-10-25"],
      ["After midnight", "2026-10-25"],
    ],
  );
});

test("bins keep today's tied pickups first and ignore invalid and past dates", () => {
  const states = Object.fromEntries(
    config.bins.map(({ entity }, index) => [
      entity,
      {
        state: "active",
        attributes: { Sort_date: ["20260329", "20260329", "20260328", "20260230"][index] },
      },
    ]),
  );
  const rows = collectionRows(
    config.bins,
    states,
    new Date("2026-03-28T23:05:00Z"),
    "Europe/Amsterdam",
  );
  assert.deepEqual(
    Array.from(rows, ({ name, days }) => [name, days]),
    [
      ["GFT", 0],
      ["Paper", 0],
      ["PMD", null],
      ["Residual", null],
    ],
  );
});
