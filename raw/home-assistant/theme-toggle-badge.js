// Registered as an inline Lovelace module through the Home Assistant resource API.
class ThemeToggleBadge extends HTMLElement {
  setConfig() {
    if (this.shadowRoot) return;
    this.attachShadow({ mode: "open" }).innerHTML = `
      <style>
        :host { position: fixed; right: calc(20px + env(safe-area-inset-right, 0px));
          bottom: calc(20px + env(safe-area-inset-bottom, 0px)); z-index: 5; }
        button { width: 52px; height: 52px; border-radius: 50%; cursor: pointer;
          display: grid; place-items: center; border: 1px solid var(--divider-color);
          background: var(--card-background-color); color: var(--primary-text-color);
          box-shadow: 0 3px 12px #0004; }
        button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 3px; }
        button:disabled { opacity: .6; cursor: wait; }
      </style>
      <button type="button"><ha-icon></ha-icon></button>`;
    this.button = this.shadowRoot.querySelector("button");
    this.icon = this.shadowRoot.querySelector("ha-icon");
    this.button.onclick = () => this.toggle();
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  render() {
    if (!this.button || !this._hass) return;
    const dark = this._hass.themes.default_theme === "Graphite";
    this.nextTheme = dark ? "Graphite Light" : "Graphite";
    this.icon.setAttribute("icon", dark ? "mdi:weather-sunny" : "mdi:weather-night");
    const label = `Switch to ${dark ? "light" : "dark"} theme until the next sun automation run`;
    this.button.setAttribute("aria-label", label);
    this.button.title = label;
  }

  async toggle() {
    if (!this._hass || !this.nextTheme || this.button.disabled) return;
    this.button.disabled = true;
    try {
      await this._hass.callService("frontend", "set_theme", {
        name: this.nextTheme,
        name_dark: this.nextTheme,
      });
    } catch {
      this.dispatchEvent(
        new CustomEvent("hass-notification", {
          detail: { message: "Could not change the theme. Please try again." },
          bubbles: true,
          composed: true,
        }),
      );
    } finally {
      this.button.disabled = false;
    }
  }
}

customElements.define("theme-toggle-badge", ThemeToggleBadge);
