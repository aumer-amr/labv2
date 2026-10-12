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
    // Graphite uses dark colors without declaring HA native dark mode.
    const dark = this._hass.themes.theme === "Graphite";
    this.nextTheme = dark ? "Graphite Light" : "Graphite";
    this.icon.setAttribute("icon", dark ? "mdi:weather-sunny" : "mdi:weather-night");
    const label = `Switch to ${dark ? "light" : "dark"} theme for this account`;
    this.button.setAttribute("aria-label", label);
    this.button.title = label;
  }

  toggle() {
    if (!this._hass || !this.nextTheme || this.button.disabled) return;
    this.button.disabled = true;
    try {
      this.dispatchEvent(
        new CustomEvent("settheme", {
          detail: { theme: this.nextTheme, dark: this.nextTheme === "Graphite" },
          bubbles: true,
          composed: true,
        }),
      );
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
