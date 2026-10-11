// Reuse the native stack; only the tablet's outer spacing differs.
const helpers = await window.loadCardHelpers();
helpers.createCardElement({
  type: "vertical-stack",
  cards: [],
});
await customElements.whenDefined("hui-vertical-stack-card");
const NativeStack = customElements.get("hui-vertical-stack-card");

class FrontDoorLayout extends NativeStack {
  setConfig(config) {
    super.setConfig({ ...config, type: "vertical-stack" });
    this.style.padding = "24px";
    this.style.boxSizing = "border-box";
  }
}

customElements.define("front-door-layout", FrontDoorLayout);

class FrontDoorLights extends HTMLElement {
  setConfig(config) {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" }).innerHTML = `
        <style>
          details { background: var(--card-background-color); color: var(--primary-text-color);
            border-radius: var(--ha-card-border-radius, 20px); padding: 16px; }
          summary { cursor: pointer; font-weight: 600; min-height: 24px; }
          summary:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 4px; }
          details[open] summary { margin-bottom: 16px; }
        </style>
        <details><summary>Lights</summary></details>`;
      this.stack = new NativeStack();
      this.shadowRoot.querySelector("details").appendChild(this.stack);
    }
    this.stack.setConfig({ type: "vertical-stack", cards: config.cards });
    this.stack.hass = this._hass;
  }

  set hass(hass) {
    this._hass = hass;
    if (this.stack) this.stack.hass = hass;
  }

  getCardSize() {
    return this.shadowRoot?.querySelector("details").open ? 5 : 1;
  }
}

customElements.define("front-door-lights", FrontDoorLights);
