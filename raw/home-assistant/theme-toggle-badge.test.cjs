// Run: node raw/home-assistant/theme-toggle-badge.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
let Badge;
const context = vm.createContext({
  HTMLElement: class {},
  CustomEvent: class {
    constructor(type, options) {
      Object.assign(this, { type }, options);
    }
  },
  customElements: {
    define(name, cls) {
      Badge = cls;
    },
  },
});
vm.runInContext(fs.readFileSync(`${__dirname}/theme-toggle-badge.js`, "utf8"), context);
(async () => {
  const badge = new Badge();
  badge.button = {
    setAttribute(key, value) {
      this[key] = value;
    },
  };
  badge.icon = {
    setAttribute(key, value) {
      this[key] = value;
    },
  };
  const calls = [];
  const hass = {
    themes: { default_theme: "Graphite" },
    async callService(...args) {
      calls.push(args);
    },
  };
  badge.hass = hass;
  assert.equal(badge.icon.icon, "mdi:weather-sunny");
  await Promise.all([badge.toggle(), badge.toggle()]);
  assert.equal(calls.length, 1, "Double clicks must not send duplicate requests");
  assert.equal(
    JSON.stringify(calls[0]),
    JSON.stringify([
      "frontend",
      "set_theme",
      { name: "Graphite Light", name_dark: "Graphite Light" },
    ]),
  );
  badge.hass = { ...hass, themes: { default_theme: "Graphite Light" } };
  assert.equal(badge.icon.icon, "mdi:weather-night");
  await badge.toggle();
  assert.equal(calls[1][2].name, "Graphite");
  badge.hass = hass; // A subsequent sun automation update must update the button again.
  assert.equal(badge.nextTheme, "Graphite Light");
  let error;
  badge.dispatchEvent = (event) => {
    error = event;
  };
  badge.hass = {
    ...hass,
    async callService() {
      throw new Error("offline");
    },
  };
  await badge.toggle();
  assert.equal(error.type, "hass-notification");
  assert.equal(badge.button.disabled, false);
  console.log("Theme toggle: both modes, automation updates, duplicate clicks and errors passed");
})();
