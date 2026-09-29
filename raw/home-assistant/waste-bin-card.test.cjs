// Run: node raw/home-assistant/waste-bin-card.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({
  HTMLElement: class {},
  customElements: { define() {} },
  window: {},
});
vm.runInContext(fs.readFileSync(`${__dirname}/waste-bin-card.js`, "utf8"), context);
const state = (date, value = "collection") => ({ state: value, attributes: { Sort_date: date } });
context.streams = [{ entity: "sensor.paper" }, { entity: "sensor.gft" }, { entity: "sensor.pmd" }];
context.states = {
  "sensor.paper": state(20261113),
  "sensor.gft": state(20261006),
  "sensor.pmd": state(20261006),
};
let rows = vm.runInContext(
  'collectionRows(streams, states, new Date("2026-09-29T12:00:00Z"), "Europe/Amsterdam")',
  context,
);
assert.deepEqual(
  Array.from(rows, (row) => row.days),
  [7, 7, 45],
);
rows = vm.runInContext(
  'collectionRows(streams, states, new Date("2026-10-05T22:30:00Z"), "Europe/Amsterdam")',
  context,
);
assert.equal(rows[0].days, 0, "Amsterdam midnight must mean today, even if browser is elsewhere");
context.states["sensor.gft"] = state(20261006, "unavailable");
context.states["sensor.pmd"] = state(20260230);
rows = vm.runInContext(
  'collectionRows(streams, states, new Date("2026-11-14T12:00:00Z"), "Europe/Amsterdam")',
  context,
);
assert.ok(
  rows.every((row) => row.days === null),
  "Missing, invalid, unavailable and past dates cannot be upcoming",
);
context.unsafe = '<img onerror="x">&';
assert.equal(
  vm.runInContext("escapeHtml(unsafe)", context),
  "&lt;img onerror=&quot;x&quot;&gt;&amp;",
);
console.log("Waste bin card: sorting, ties, timezone, unavailable/stale dates and escaping passed");
