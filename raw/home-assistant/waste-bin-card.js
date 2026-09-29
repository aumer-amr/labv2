// Registered as an inline Lovelace module through the Home Assistant resource API.
function collectionDate(state) {
  if (!state || ["unknown", "unavailable"].includes(state.state)) return null;
  const value = String(state.attributes?.Sort_date ?? "");
  if (!/^\d{8}$/.test(value)) return null;
  const date = new Date(Date.UTC(+value.slice(0, 4), +value.slice(4, 6) - 1, +value.slice(6, 8)));
  return date.toISOString().slice(0, 10).replaceAll("-", "") === value ? date : null;
}

function collectionRows(streams, states, now, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (type) => +parts.find((item) => item.type === type).value;
  const today = Date.UTC(part("year"), part("month") - 1, part("day"));
  return streams
    .map((stream) => {
      const date = collectionDate(states[stream.entity]);
      const days = date ? Math.round((date.getTime() - today) / 86400000) : null;
      return { ...stream, date, days: days !== null && days >= 0 ? days : null };
    })
    .sort((a, b) => (a.days ?? Infinity) - (b.days ?? Infinity));
}

const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );

class WasteBinCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-entity]");
      if (button)
        this.dispatchEvent(
          new CustomEvent("hass-more-info", {
            detail: { entityId: button.dataset.entity },
            bubbles: true,
            composed: true,
          }),
        );
    });
  }

  setConfig(config) {
    if (
      !Array.isArray(config.streams) ||
      !config.streams.length ||
      config.streams.some((stream) => !/^sensor\.[a-z0-9_]+$/.test(stream.entity))
    ) {
      throw new Error("Define waste streams with sensor entity IDs");
    }
    this.config = config;
    this.signature = null;
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }
  connectedCallback() {
    this.render();
    this.timer = setInterval(() => this.render(), 60000);
  }
  disconnectedCallback() {
    clearInterval(this.timer);
  }
  getCardSize() {
    return 3;
  }
  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  render() {
    if (!this.config || !this._hass) return;
    const rows = collectionRows(
      this.config.streams,
      this._hass.states,
      new Date(),
      this._hass.config.time_zone || "Europe/Amsterdam",
    );
    const signature = JSON.stringify(rows);
    if (signature === this.signature) return;
    this.signature = signature;
    const next = rows.find((row) => row.days !== null);
    const due = next
      ? rows
          .filter((row) => row.days === next.days)
          .map((row) => row.name)
          .join(" + ")
      : "";
    const format = (date, options) =>
      new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(date);
    const dateLabel = next
      ? format(next.date, { weekday: "long", day: "numeric", month: "long" })
      : "No collection dates available";
    const urgent = next && next.days <= 1;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; border-radius: 16px; color: var(--primary-text-color); }
        ha-card.urgent { border-color: var(--warning-color, #ff9800); }
        .top { display: flex; gap: 14px; align-items: center; }
        .bin { color: var(--secondary-text-color); --mdc-icon-size: 28px; flex: none; }
        .urgent .bin { color: var(--warning-color, #ff9800); }
        .summary { flex: 1; min-width: 0; }
        .title { font-size: 16px; font-weight: 600; }
        .date, .due, .unit { color: var(--secondary-text-color); font-size: 13px; line-height: 1.5; }
        .due { margin-top: 2px; }
        .count { text-align: right; flex: none; }
        .number { font-size: 24px; font-weight: 600; line-height: 1.2; }
        .urgent .number { font-size: 19px; color: var(--warning-color, #ff9800); }
        .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 12px; }
        button { display: inline-flex; align-items: center; gap: 5px; padding: 8px 10px;
          min-height: 40px; border: 0; border-radius: 24px; cursor: pointer;
          background: var(--secondary-background-color, #f0f2f5); color: inherit; font: inherit; font-size: 12px; }
        button:hover { background: var(--divider-color); }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        button ha-icon { --mdc-icon-size: 18px; color: var(--bin-color); }
        button b { font-weight: 600; }
        button span { color: var(--secondary-text-color); }
        .notice { font-size: 12px; color: var(--secondary-text-color); margin-top: 10px; }
      </style>
      <ha-card class="${urgent ? "urgent" : ""}">
        <div class="top">
          <ha-icon class="bin" icon="mdi:trash-can" aria-hidden="true"></ha-icon>
          <div class="summary"><div class="title">Bins</div>
            <div class="date">${escapeHtml(dateLabel)}</div>
            <div class="due">${escapeHtml(due)}</div>
          </div>
          <div class="count"><div class="number">${next ? (next.days === 0 ? "Today" : next.days === 1 ? "Tomorrow" : next.days) : "—"}</div>
            <div class="unit">${urgent ? "Put bins out" : next ? "days" : ""}</div>
          </div>
        </div>
        <div class="chips">${rows
          .map((row) => {
            const label =
              row.days === null
                ? "Unavailable"
                : row.days === 0
                  ? "Today"
                  : row.days === 1
                    ? "Tomorrow"
                    : format(row.date, { weekday: "short", day: "numeric", month: "short" });
            const color = /^#[0-9a-f]{6}$/i.test(row.color) ? row.color : "#9e9e9e";
            const icon = /^mdi:[a-z0-9-]+$/.test(row.icon) ? row.icon : "mdi:trash-can";
            return `<button data-entity="${escapeHtml(row.entity)}" style="--bin-color:${color}" aria-label="${escapeHtml(`${row.name}: ${label}. Show details`)}">
            <ha-icon icon="${icon}" aria-hidden="true"></ha-icon><b>${escapeHtml(row.name)}</b><span>${escapeHtml(label)}</span></button>`;
          })
          .join("")}</div>
        ${next && rows.some((row) => row.days === null) ? '<div class="notice">Some collection dates are unavailable.</div>' : ""}
      </ha-card>`;
  }
}

customElements.define("waste-bin-card", WasteBinCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "waste-bin-card",
  name: "Waste bins",
  description: "Next collection and colour-coded waste streams",
});
