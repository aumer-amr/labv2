const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  class NativeStack {
    style = {};
    setConfig(config) {
      this.config = config;
    }
  }
  global.window = {
    loadCardHelpers: async () => ({ createCardElement: () => new NativeStack() }),
  };
  global.HTMLElement = class {
    attachShadow() {
      const details = { open: false, appendChild() {} };
      return (this.shadowRoot = { querySelector: () => details });
    }
  };
  const elements = {};
  global.customElements = {
    whenDefined: async () => {},
    get: () => NativeStack,
    define: (name, cls) => (elements[name] = cls),
  };
  const source = fs.readFileSync(`${__dirname}/front-door-layout.js`, "utf8");
  await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  const layout = new elements["front-door-layout"]();
  const cards = [{ type: "tile", entity: "person.sem" }];
  layout.setConfig({ type: "custom:front-door-layout", cards });
  assert.equal(layout.config.type, "vertical-stack");
  assert.equal(layout.config.cards, cards);
  assert.equal(layout.style.padding, "24px");
  assert.equal(layout.style.boxSizing, "border-box");
  const lights = new elements["front-door-lights"]();
  const hass = { states: {} };
  lights.hass = hass;
  lights.setConfig({ cards });
  const details = lights.shadowRoot.querySelector("details");
  assert.equal(details.open, false, "Lights must start collapsed");
  assert.equal(lights.getCardSize(), 1);
  assert.equal(lights.stack.config.cards, cards);
  assert.equal(lights.stack.hass, hass);
  details.open = true;
  const updated = { states: { "light.grote_lamp": { state: "on" } } };
  lights.hass = updated;
  lights.setConfig({ cards });
  assert.equal(details.open, true, "Updates must preserve expansion");
  assert.equal(lights.stack.hass, updated);
})();
