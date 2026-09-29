// Run: node raw/home-assistant/energy-summary-card.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({
  HTMLElement: class {},
  customElements: { define() {} },
  window: {},
});
vm.runInContext(fs.readFileSync(`${__dirname}/energy-summary-card.js`, "utf8"), context);
context.data = {
  a: [{ start: Date.parse("2026-09-28T22:00:00Z"), change: 2 }],
  b: [{ start: Date.parse("2026-09-28T22:00:00Z"), change: 5.31 }],
};
context.sources = [
  { stat_energy_from: "a", number_energy_price: 0.230547 },
  { stat_energy_from: "b", number_energy_price: 0.230547 },
];
const run = (code) => vm.runInContext(code, context);
assert.equal(run('energyDays(data, ["a", "b"], "Europe/Amsterdam")[0].total'), 7.31);
assert.equal(run('energyDays(data, ["a", "b"], "Europe/Amsterdam")[0].day'), "2026-09-29");
assert.equal(run('energyDays(data, ["a", "missing"], "Europe/Amsterdam")[0].total'), null);
assert.ok(
  Math.abs(
    run(
      'energyCost(energyDays(data, ["a", "b"], "Europe/Amsterdam")[0], ["a", "b"], sources, {})',
    ) - 1.68529857,
  ) < 1e-8,
);
assert.equal(
  run('energyCost(energyDays(data, ["a"], "Europe/Amsterdam")[0], ["a"], [], {})'),
  null,
);
assert.equal(run('energyNumber("unavailable")'), null);
assert.equal(run("energyNumber(null)"), null);
assert.equal(run('energyNumber("0")'), 0);
assert.equal(run('energyDay("2026-10-25T23:30:00Z", "Europe/Amsterdam")'), "2026-10-26");
console.log(
  "Energy summary: tariff aggregation, missing data, costs, zero usage and timezone/DST passed",
);
