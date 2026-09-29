// Uses HA recorder statistics and Energy settings; no extra helpers or dependencies.
const energyNumber = (value) =>
  value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
    ? Number(value)
    : null;
const energyEscape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
const energyDay = (time, zone) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(time));

function energyDays(data, ids, zone) {
  const days = new Map();
  for (const id of ids)
    for (const row of data[id] || []) {
      const day = energyDay(row.start, zone);
      if (!days.has(day)) days.set(day, { day, start: row.start, values: {} });
      days.get(day).values[id] = energyNumber(row.change);
    }
  return [...days.values()]
    .sort((a, b) => a.start - b.start)
    .slice(-7)
    .map((row) => ({
      ...row,
      total: ids.every((id) => row.values[id] !== undefined && row.values[id] !== null)
        ? ids.reduce((sum, id) => sum + row.values[id], 0)
        : null,
    }));
}

function energyCost(row, ids, sources, states) {
  if (!row || row.total === null) return null;
  let total = 0;
  for (const id of ids) {
    const source = sources.find((item) => item.stat_energy_from === id);
    const rate = energyNumber(
      source?.entity_energy_price
        ? states[source.entity_energy_price]?.state
        : source?.number_energy_price,
    );
    if (rate === null) return null;
    total += row.values[id] * rate;
  }
  return total;
}

class EnergySummaryCard extends HTMLElement {
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
    const ids = [
      config.power,
      config.gas,
      config.tariff,
      config.export,
      ...(config.electricity || []),
    ];
    if (!config.electricity?.length || ids.some((id) => !/^sensor\.[a-z0-9_]+$/.test(id)))
      throw new Error("Define power, gas, tariff, export and electricity sensor IDs");
    this.config = config;
    this.data = {};
    this.sources = [];
    this.loaded = false;
    this.refresh();
  }
  set hass(hass) {
    this._hass = hass;
    this.render();
    if (!this.loaded) this.refresh();
  }
  connectedCallback() {
    this.refresh();
    this.timer = setInterval(() => this.refresh(), 300000);
  }
  disconnectedCallback() {
    clearInterval(this.timer);
  }
  getCardSize() {
    return 4;
  }
  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  async refresh() {
    if (!this.config || !this._hass || this.loading || !this.isConnected) return;
    this.loading = true;
    const config = this.config;
    try {
      const [data, prefs] = await Promise.all([
        this._hass.callWS({
          type: "recorder/statistics_during_period",
          start_time: new Date(Date.now() - 7 * 86400000).toISOString(),
          statistic_ids: [...config.electricity, config.gas, config.power],
          period: "day",
          types: ["change", "max"],
          units: { energy: "kWh", power: "kW", volume: "m³" },
        }),
        this._hass.callWS({ type: "energy/get_prefs" }).catch(() => ({ energy_sources: [] })),
      ]);
      if (config !== this.config) return;
      this.data = data;
      this.sources = prefs.energy_sources || [];
      this.error = false;
    } catch {
      this.error = true;
      this.data = {};
    } finally {
      this.loading = false;
      this.loaded = true;
      this.render();
    }
  }

  render() {
    if (!this.config || !this._hass) return;
    const c = this.config,
      states = this._hass.states;
    const zone = this._hass.config.time_zone || "Europe/Amsterdam";
    const today = energyDay(Date.now(), zone);
    const electricity = energyDays(this.data || {}, c.electricity, zone);
    const gas = energyDays(this.data || {}, [c.gas], zone);
    const eToday = electricity.find((row) => row.day === today);
    const gToday = gas.find((row) => row.day === today);
    const powerValue = (id) => {
      const value = energyNumber(states[id]?.state),
        unit = states[id]?.attributes.unit_of_measurement;
      return value === null ? null : unit === "W" ? value / 1000 : unit === "kW" ? value : null;
    };
    const power = powerValue(c.power),
      exported = powerValue(c.export);
    const recordedPeak = energyNumber(
      (this.data?.[c.power] || []).find((row) => energyDay(row.start, zone) === today)?.max,
    );
    const peak = recordedPeak === null ? null : Math.max(recordedPeak, power || 0);
    const percent =
      power !== null && peak > 0 ? Math.min(100, Math.round((power / peak) * 100)) : 0;
    const format = (value, digits = 2) =>
      value === null || value === undefined
        ? "—"
        : value.toLocaleString("en-GB", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
          });
    const cost = (row, ids) => {
      const value = energyCost(row, ids, this.sources || [], states);
      return value === null
        ? ""
        : ` · ≈ ${energyEscape(new Intl.NumberFormat("en-GB", { style: "currency", currency: this._hass.config.currency || "EUR" }).format(value))}`;
    };
    const bars = (rows, unit) => {
      const max = Math.max(...rows.map((row) => row.total || 0), 0.001);
      return `<div class="bars" role="img" aria-label="Daily ${unit} usage, last seven days">${rows.map((row) => `<span class="bar ${row.day === today ? "current" : ""}" style="height:${row.total === null ? 2 : Math.max(2, (row.total / max) * 28)}px" title="${row.day}: ${format(row.total)} ${unit}${row.day === today ? " (today so far)" : ""}"></span>`).join("")}</div>`;
    };
    const tariff = states[c.tariff]?.state;
    const tariffLabel =
      tariff === "normal"
        ? "Normal tariff"
        : tariff === "low"
          ? "Low tariff"
          : "Tariff unavailable";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; border-radius: 16px; color: var(--primary-text-color); }
        button { border: 0; background: none; color: inherit; font: inherit; text-align: left; padding: 0; cursor: pointer; }
        button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 4px; border-radius: 4px; }
        .row { display: flex; align-items: center; gap: 12px; min-height: 44px; width: 100%; }
        ha-icon { flex: none; color: #448aff; --mdc-icon-size: 24px; }
        .gas ha-icon { color: #f59e0b; }
        .main { flex: 1; min-width: 0; }
        strong { font-size: 18px; font-weight: 600; }
        .daily { text-align: right; flex: none; }
        .daily strong { font-size: 16px; }
        .muted { color: var(--secondary-text-color); font-size: 12px; line-height: 1.6; }
        .meter { height: 6px; margin: 12px 0 8px; border-radius: 5px; background: var(--divider-color); overflow: hidden; }
        .fill { height: 100%; width: ${percent}%; background: #448aff; border-radius: inherit; }
        .gas strong { font-size: 15px; }
        .bars { display: flex; align-items: flex-end; gap: 4px; height: 30px; margin-left: auto; }
        .bar { width: 7px; border-radius: 2px; background: #f59e0b; opacity: .6; }
        .current { background: #448aff; opacity: 1; }
        .footer { margin-top: 7px; display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
      </style>
      <ha-card>
        <button class="row" data-entity="${c.power}" aria-label="Electricity consumption details">
          <ha-icon icon="mdi:lightning-bolt" aria-hidden="true"></ha-icon>
          <div class="main"><strong>${format(power)} kW</strong><div class="muted">${tariffLabel}</div></div>
          <div class="daily"><strong>${format(eToday?.total)} kWh</strong><div class="muted">today${cost(eToday, c.electricity)}</div></div>
        </button>
        <div class="meter" role="meter" aria-label="Current power as percentage of today's measured peak" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="fill"></div></div>
        <div class="muted">${peak === null || power === null ? "Peak unavailable" : `${percent}% of today's peak · ${format(peak)} kW`}</div>
        <button class="row gas" data-entity="${c.gas}" aria-label="Gas consumption details">
          <ha-icon icon="mdi:fire" aria-hidden="true"></ha-icon>
          <div class="main"><strong>Gas · ${format(gToday?.total, 3)} m³</strong><div class="muted">today${cost(gToday, [c.gas])}</div></div>
          ${bars(gas, "m³")}
        </button>
        <div class="footer muted"><span>${this.error ? "Usage history unavailable" : !this.loaded ? "Loading usage…" : "Recorded usage · 7-day gas trend"}</span>
          <button data-entity="${c.export}" aria-label="Electricity export details">Export ${format(exported)} kW</button></div>
      </ha-card>`;
  }
}
customElements.define("energy-summary-card", EnergySummaryCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-summary-card",
  name: "Electricity & gas",
  description: "Live power, daily usage, estimated cost and gas trend",
});
