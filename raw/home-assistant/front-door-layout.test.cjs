const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  class NativeStack {
    style = {};
    connectedCallback() {}
    disconnectedCallback() {}
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
  let now = 0;
  const originalNow = Date.now;
  Date.now = () => now;
  let closed = 0;
  const audio = new EventTarget();
  const chat = {
    pipeline: { id: "01m6bwx4grbap360r75m0h0n22" },
    _conversation: [{ who: "hass", text: "Saved: get groceries." }],
    _audio: audio,
  };
  const dialog = {
    _open: true,
    shadowRoot: { querySelector: () => chat },
    closeDialog: () => {
      closed++;
      dialog._open = false;
    },
  };
  global.document = { querySelector: () => ({ shadowRoot: { querySelector: () => dialog } }) };
  layout.closeSavedReminder();
  now = 5000;
  chat._audio = undefined;
  layout.closeSavedReminder();
  assert.equal(closed, 0, "Interrupted or failed audio must not close the dialog");
  audio.dispatchEvent(new Event("ended"));
  layout.closeSavedReminder();
  now += 1499;
  layout.closeSavedReminder();
  assert.equal(closed, 0, "Keep confirmation visible briefly after playback");
  now++;
  layout.closeSavedReminder();
  assert.equal(closed, 1, "Close after successful confirmation finishes");
  for (const response of [
    { who: "hass", text: "I could not confirm the save." },
    { who: "hass", text: "Saved: failed", error: true },
    { who: "user", text: "Saved: fake" },
  ]) {
    dialog._open = true;
    chat._conversation = [response];
    layout.closeSavedReminder();
    now += 5000;
    layout.closeSavedReminder();
    assert.equal(closed, 1, "Failures and user messages stay visible");
  }
  chat._conversation = [{ who: "hass", text: "Saved: text reminder." }];
  chat.pipeline.id = "another-pipeline";
  layout.closeSavedReminder();
  now += 5000;
  layout.closeSavedReminder();
  assert.equal(closed, 1, "Other pipelines stay open");
  chat.pipeline.id = "01m6bwx4grbap360r75m0h0n22";
  layout.closeSavedReminder();
  chat._conversation = [{ who: "user", text: "Another request" }];
  now += 5000;
  layout.closeSavedReminder();
  assert.equal(closed, 1, "A follow-up cancels pending close");
  chat._conversation = [{ who: "hass", text: "Saved: text reminder." }];
  layout.closeSavedReminder();
  now += 1500;
  layout.closeSavedReminder();
  assert.equal(closed, 2, "Text confirmations also close");
  layout.connectedCallback();
  layout.disconnectedCallback();
  assert.equal(layout._assistCompletion, undefined);
  Date.now = originalNow;
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
