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
const badge = new Badge();
badge.button = { disabled: false, setAttribute(key, value) { this[key] = value; } };
badge.icon = { setAttribute(key, value) { this[key] = value; } };
let hass = {
  user: { is_admin: false },
  themes: { default_theme: "Graphite", theme: "Graphite Light", darkMode: false },
  callService() { assert.fail("Theme changes must not call an admin service"); },
};
const events = [];
badge.dispatchEvent = (event) => {
  events.push(event);
  if (event.type === "settheme") {
    assert.equal(event.bubbles, true);
    assert.equal(event.composed, true);
    // HA forces native darkMode false for themes without a dark modes entry.
    hass = { ...hass, themes: { ...hass.themes, theme: event.detail.theme, darkMode: false } };
    badge.hass = hass;
  }
  return true;
};
badge.hass = hass;
for (const expected of ["Graphite", "Graphite Light", "Graphite", "Graphite Light"]) {
  badge.toggle();
  assert.equal(events.at(-1).detail.theme, expected);
  assert.equal(events.at(-1).detail.dark, expected === "Graphite");
  assert.equal(badge.icon.icon, expected === "Graphite" ? "mdi:weather-sunny" : "mdi:weather-night");
  assert.equal(badge.button.disabled, false);
}
badge.button.disabled = true;
badge.toggle();
assert.equal(events.length, 4);
badge.button.disabled = false;
badge.dispatchEvent = (event) => {
  if (event.type === "settheme") throw new Error("dispatch failed");
  events.push(event);
};
badge.toggle();
assert.equal(events.at(-1).type, "hass-notification");
assert.equal(badge.button.disabled, false);
console.log("Theme toggle: repeated clicks with native darkMode false, read-only user, disabled guard and errors passed");
