const template = document.createElement("template");
template.innerHTML = `
<style>
  :host {
    color-scheme: dark;
    --page: #282828;
    --ink: #fff;
    --muted: #e3e3e3;
    --glass: rgb(30 30 30 / 57%);
    --line: rgb(255 255 255 / 24%);
    --shade-top: rgb(16 16 16 / 67%);
    --shade-bottom: rgb(16 16 16 / 63%);
  }
  :host([data-mode="light"]) {
    color-scheme: light;
    --page: #e7e7e3;
    --ink: #202020;
    --muted: #414141;
    --glass: rgb(255 255 255 / 73%);
    --line: rgb(0 0 0 / 20%);
    --shade-top: rgb(255 255 255 / 68%);
    --shade-bottom: rgb(255 255 255 / 50%);
  }
  * { box-sizing: border-box; }
  :host { display: block; background: var(--page); color: var(--ink); font: 500 16px/1.4 "Hanken Grotesk", system-ui, sans-serif; }
  .screen {
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 100svh;
    isolation: isolate;
    padding: clamp(24px, 6vw, 90px) clamp(24px, 8vw, 135px);
  }
  .screen::before {
    content: "";
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: -1;
    background: linear-gradient(135deg, var(--shade-top), var(--shade-bottom));
  }
  .photo { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; object-position: center 52%; z-index: -2; }
  header, .utilities { display: flex; align-items: center; }
  header { justify-content: space-between; flex-wrap: wrap; gap: 14px 28px; }
  .utilities { gap: 5px; }
  button {
    font: inherit;
    color: inherit;
    background: transparent;
    border: 0;
    cursor: pointer;
  }
  button:focus-visible, summary:focus-visible { outline: 2px solid currentColor; outline-offset: 4px; }
  .utilities button { display: grid; place-items: center; width: 44px; height: 44px; }
  .utilities button:hover { background: var(--glass); }
  svg { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.35; stroke-linecap: round; stroke-linejoin: round; }
  .hero { margin-top: auto; padding: clamp(72px, 18vh, 180px) 0 clamp(40px, 10vh, 92px); }
  .date { letter-spacing: .12em; text-transform: uppercase; font-size: 14px; font-weight: 600; }
  .clock { font-size: clamp(24px, 3vw, 38px); line-height: 1; font-weight: 700; font-variant-numeric: tabular-nums; }
  .greeting { margin: 10px 0 0; font-size: clamp(48px, 7vw, 106px); line-height: 1.02; letter-spacing: -.045em; }
  .weather { margin: 12px 0 0; color: var(--muted); font-size: clamp(16px, 2vw, 23px); }
  .tiles { display: grid; grid-template-columns: 1.35fr 1fr 1.1fr .8fr; gap: 7px; align-items: start; }
  .tile { position: relative; min-width: 0; }
  .tile[open] { z-index: 1; }
  .tile-face {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    min-height: 176px;
    padding: 18px clamp(16px, 2.5vw, 35px);
    background: var(--glass);
    backdrop-filter: blur(19px);
  }
  summary { cursor: pointer; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  details[open] > summary { background: color-mix(in srgb, var(--glass), var(--ink) 8%); }
  .tile-face svg { width: 36px; height: 36px; stroke-width: .85; }
  .tile-name, .tile-value { display: block; }
  .tile-name { font-size: 18px; font-weight: 700; }
  .tile-value { color: var(--muted); font-size: 15px; margin-top: 4px; }
  .tile-face-with-rows { grid-template-columns: minmax(0, 1fr) auto; gap: 6px 10px; align-content: center; }
  .tile-face-with-rows svg { width: 22px; height: 22px; }
  .tile-rows { grid-column: 1 / -1; display: grid; gap: 4px; color: var(--muted); font-size: 15px; }
  .tile-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0 8px; }
  .tile-row > :first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tile-row > :last-child { white-space: nowrap; }
  .readings {
    position: absolute;
    inset: auto 0 calc(100% + 7px);
    margin: 0;
    padding: 6px 16px;
    list-style: none;
    background: var(--glass);
    backdrop-filter: blur(19px);
  }
  .readings li { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 2px 8px; padding: 9px 0; }
  .readings li + li { border-top: 1px solid var(--line); }
  .readings small { color: var(--muted); font-size: 13px; }
  .lights li { flex-wrap: nowrap; }
  .lights label { min-height: 44px; display: flex; align-items: center; justify-content: space-between; gap: 10px; flex: 1; cursor: pointer; }
  .lights li { padding-block: 0; }
  .lights input:disabled { opacity: .55; cursor: default; }
  .lights input {
    appearance: none;
    display: grid;
    align-items: center;
    flex: none;
    width: 38px;
    height: 22px;
    margin: 0;
    padding: 3px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: color-mix(in srgb, var(--ink) 9%, transparent);
    color: var(--ink);
    cursor: pointer;
  }
  .lights input::before {
    content: "";
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: currentColor;
  }
  .lights input:checked {
    justify-items: end;
    border-color: transparent;
    background: var(--ink);
    color: var(--page);
  }
  .lights input:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }
  @media (forced-colors: active) {
    .lights input { appearance: auto; display: inline-block; }
    .lights input::before { content: none; }
  }
  @media (max-width: 950px) { .tile-face:not(.tile-face-with-rows) svg { width: 28px; height: 28px; } }
  @media (max-width: 1000px) {
    .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 600px) and (max-height: 780px) {
    .screen { padding-block: 24px; }
    .hero { padding-block: 36px 24px; }
    .tile-face { min-height: 150px; padding-block: 12px; }
  }
  @media (max-width: 480px) {
    .screen { padding-inline: 16px; }
    .tiles { grid-template-columns: minmax(0, 1fr); }
    .tile-face { gap: 6px; padding-inline: 12px; }
    .tile-face svg { width: 20px; height: 20px; }
  }
  .light-name { min-width: 36px; min-height: 44px; font-size: 19px; }
  .light-name:hover { text-decoration: underline; }
  .readings { z-index: 1; }
  .calendar-list { max-block-size: min(31vh, 280px); overflow: auto; }
</style>
<main class="screen" aria-label="Front Door dashboard">
  <header>
    <time class="clock" id="clock"></time>
    <div class="utilities">
      <button id="refresh" type="button" title="Refresh" aria-label="Refresh">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2 6M20 5v6h-6"/></svg>
      </button>
      <button id="theme" type="button" title="Switch to light mode" aria-label="Switch to light mode">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path id="theme-icon" d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z"/></svg>
      </button>
    </div>
  </header>
  <section class="hero" aria-labelledby="greeting">
    <span class="date" id="date"></span>
    <h1 id="greeting" class="greeting">Welcome home</h1>
    <p class="weather" id="weather" hidden></p>
  </section>
  <div class="tiles">
    <details class="tile" id="calendar" name="dashboard-tile">
      <summary class="tile-face tile-face-with-rows">
        <span class="tile-name">Calendar</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h18v16H3zM3 9h18M8 3v4m8-4v4m-9 6h2m3 0h2m3 0h1m-11 4h2m3 0h2"/></svg>
        <span class="tile-rows" id="event-summary">Loading events…</span>
      </summary>
      <ul class="readings calendar-list" id="event-details" aria-label="Calendar for the next seven days"></ul>
    </details>
    <details class="tile" id="bins" name="dashboard-tile">
      <summary class="tile-face"><span><span class="tile-name">Bins</span><span class="tile-value" id="bin-summary">Loading pickups…</span></span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16m-10 0V3h4v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/></svg>
      </summary>
      <ul class="readings" id="bin-details"></ul>
    </details>
    <section class="tile tile-face tile-face-with-rows" id="rooms" aria-labelledby="rooms-title">
      <span class="tile-name" id="rooms-title">Rooms</span>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 14.7V5a2 2 0 0 0-4 0v9.7a4 4 0 1 0 4 0ZM12 8v10"/></svg>
      <div class="tile-rows" id="room-readings"></div>
    </section>
    <details class="tile" id="lights" name="dashboard-tile">
      <summary class="tile-face"><span><span class="tile-name">Lights</span><span class="tile-value" id="light-count">Loading lights…</span></span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3h8l5 11H3L8 3Zm4 11v7m-4 0h8m2-7v4"/></svg>
      </summary>
      <ul class="readings lights" id="light-controls"></ul>
    </details>
  </div>
</main>
`;

const DAY = 86400000;
const GRAPHITE = "Graphite";
const GRAPHITE_LIGHT = "Graphite Light";
const SUN =
  "M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z";
const MOON = "M20.2 15.1A8.5 8.5 0 0 1 8.9 3.8a8.5 8.5 0 1 0 11.3 11.3Z";

function dayKey(date, timeZone) {
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

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

function eventRows(events, today, timeZone) {
  const until = new Date(Date.parse(today) + 7 * DAY).toISOString().slice(0, 10);
  const rows = [];
  for (const event of events) {
    const start = event.start?.dateTime ?? event.start?.date;
    const timestamp = Date.parse(start);
    if (event.status?.toLowerCase() === "cancelled" || !Number.isFinite(timestamp)) continue;
    const allDay = Boolean(event.start.date);
    const day = allDay ? start : dayKey(new Date(timestamp), timeZone);
    const end = Date.parse(event.end?.dateTime ?? event.end?.date);
    const lastDay = Number.isFinite(end)
      ? allDay
        ? new Date(end - 1).toISOString().slice(0, 10)
        : dayKey(new Date(end - 1), timeZone)
      : day;
    if (day >= until || (lastDay < today && day < today)) continue;
    rows.push({ name: event.summary || "Untitled event", allDay, day, timestamp, end });
  }
  return rows.sort(
    (a, b) =>
      a.day.localeCompare(b.day) ||
      Number(b.allDay) - Number(a.allDay) ||
      a.timestamp - b.timestamp,
  );
}

function row(name, detail, inTile = false) {
  const line = document.createElement(inTile ? "span" : "li");
  if (inTile) line.className = "tile-row";
  const label = document.createElement("span");
  label.textContent = name;
  const value = document.createElement(inTile ? "span" : "small");
  value.textContent = detail;
  line.append(label, value);
  return line;
}

class FrontDoorTablet extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" }).append(template.content.cloneNode(true));
    this.shadowRoot.getElementById("refresh").onclick = () => location.reload();
    this.shadowRoot.getElementById("theme").onclick = () => {
      const dark = this.dataset.mode === "light";
      this.dispatchEvent(
        new CustomEvent("settheme", {
          detail: { theme: dark ? GRAPHITE : GRAPHITE_LIGHT, dark },
          bubbles: true,
          composed: true,
        }),
      );
    };
  }

  setConfig(config) {
    this.stop();
    this.config = config;
    this.lightInputs = new Map(
      config.lights.map(({ name, entity }) => {
        const item = document.createElement("li");
        const label = document.createElement("label");
        const title = document.createElement("span");
        title.textContent = name;
        const input = document.createElement("input");
        input.type = "checkbox";
        input.setAttribute("role", "switch");
        input.onchange = () => this.setLight(entity, input);
        label.append(title, input);
        const more = document.createElement("button");
        more.className = "light-name";
        more.type = "button";
        more.textContent = "⋯";
        more.setAttribute("aria-label", "Adjust " + name);
        more.onclick = () =>
          this.dispatchEvent(
            new CustomEvent("hass-more-info", {
              detail: { entityId: entity },
              bubbles: true,
              composed: true,
            }),
          );
        item.append(label, more);
        return [entity, { input, item }];
      }),
    );
    this.shadowRoot
      .getElementById("light-controls")
      .replaceChildren(...Array.from(this.lightInputs.values(), ({ item }) => item));
    this.start();
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    if (!this.config || !this.isConnected) return;
    if (
      previous &&
      (previous.connection !== hass.connection ||
        previous.config.time_zone !== hass.config.time_zone)
    )
      this.stop();
    this.start();
    if (
      !previous ||
      previous.themes.theme !== hass.themes.theme ||
      previous.themes.darkMode !== hass.themes.darkMode
    )
      this.renderTheme();
    if (
      !previous ||
      this.config.rooms.some(
        (room) =>
          previous.states[room.temperature] !== hass.states[room.temperature] ||
          previous.states[room.humidity] !== hass.states[room.humidity],
      )
    )
      this.renderRooms();
    if (
      !previous ||
      this.config.lights.some(
        (light) => previous.states[light.entity] !== hass.states[light.entity],
      )
    )
      this.renderLights();
    if (
      !previous ||
      this.config.bins.some((bin) => previous.states[bin.entity] !== hass.states[bin.entity])
    )
      this.renderBins();
    if (previous?.states[this.config.weather] !== hass.states[this.config.weather])
      this.renderWeather();
  }

  connectedCallback() {
    this.start();
  }

  disconnectedCallback() {
    this.stop();
  }

  start() {
    if (!this.isConnected || !this._hass || !this.config || this.session) return;
    const timeZone = this._hass.config.time_zone || "Europe/Amsterdam";
    const session = (this.session = {});
    this.timeZone = timeZone;
    this.timeFormat = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
    this.dateFormat = new Intl.DateTimeFormat("en-GB", {
      timeZone,
      weekday: "long",
      day: "numeric",
      month: "long",
    });
    this.shortDateFormat = new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC",
      weekday: "short",
      day: "numeric",
      month: "short",
    });
    this.renderTheme();
    this.renderRooms();
    this.renderLights();
    this.renderWeather();
    this.tick();
    this.loadCalendar(session);
    this.rotatePhoto(session);
    session.clock = setInterval(() => this.tick(), 1000);
    session.calendar = setInterval(() => this.loadCalendar(session), 5 * 60000);
    session.photos = setInterval(() => this.rotatePhoto(session), 30 * 60000);
    this.subscribeWeather(session);
  }

  stop() {
    const session = this.session;
    if (!session) return;
    this.session = undefined;
    clearInterval(session.clock);
    clearInterval(session.calendar);
    clearInterval(session.photos);
    session.unsubscribeWeather?.();
    this.today = undefined;
    this.minute = undefined;
    this.events = undefined;
    this.forecast = undefined;
    this.currentPhoto = undefined;
    for (const image of this.shadowRoot.querySelectorAll(".photo")) image.remove();
  }

  tick() {
    const now = new Date();
    const time = this.timeFormat.format(now);
    if (time === this.minute) return;
    this.minute = time;
    const clock = this.shadowRoot.getElementById("clock");
    clock.textContent = time;
    clock.dateTime = now.toISOString();
    const hour = Number(time.slice(0, 2));
    const greeting = this.shadowRoot.getElementById("greeting");
    if (hour < 5 || hour >= 22) greeting.textContent = "Good night";
    else if (hour < 12) greeting.textContent = "Good morning";
    else if (hour < 18) greeting.textContent = "Good afternoon";
    else greeting.textContent = "Good evening";
    const today = dayKey(now, this.timeZone);
    if (this.today !== today) {
      if (this.today) this.loadCalendar(this.session);
      this.today = today;
      this.shadowRoot.getElementById("date").textContent = this.dateFormat.format(now);
      this.renderBins();
      this.renderWeather();
    }
    if (this.events) this.renderCalendar(now);
  }

  renderTheme() {
    const themes = this._hass.themes;
    const light =
      themes.theme === GRAPHITE_LIGHT || (themes.theme !== GRAPHITE && !themes.darkMode);
    this.dataset.mode = light ? "light" : "dark";
    const button = this.shadowRoot.getElementById("theme");
    button.title = "Switch to " + (light ? "dark" : "light") + " mode";
    button.setAttribute("aria-label", button.title);
    button.disabled = !themes.themes?.[GRAPHITE] || !themes.themes?.[GRAPHITE_LIGHT];
    this.shadowRoot.getElementById("theme-icon").setAttribute("d", light ? MOON : SUN);
  }

  async loadCalendar(session) {
    const request = (session.calendarRequest = Symbol());
    const today = Date.parse(dayKey(new Date(), this.timeZone));
    // Query one extra UTC day at each edge. The display filters in the HA time zone.
    const query = new URLSearchParams({
      start: new Date(today - DAY).toISOString(),
      end: new Date(today + 8 * DAY).toISOString(),
    });
    const calendars = await Promise.allSettled(
      this.config.calendars.map((entity) =>
        this._hass.callApi("GET", "calendars/" + entity + "?" + query),
      ),
    );
    if (this.session !== session || session.calendarRequest !== request) return;
    const loaded = calendars.filter((result) => result.status === "fulfilled");
    if (loaded.length || !this.events) this.events = loaded.flatMap((result) => result.value);
    this.calendarUnavailable = !loaded.length;
    this.renderCalendar(new Date());
  }

  renderCalendar(now) {
    const events = eventRows(this.events, this.today, this.timeZone);
    const next = events
      .filter(
        (event) =>
          event.allDay || (Number.isFinite(event.end) ? event.end > now : event.timestamp >= now),
      )
      .slice(0, 3);
    const summary = this.shadowRoot.getElementById("event-summary");
    const agenda = this.shadowRoot.getElementById("event-details");
    const label = (event) => {
      const day =
        event.day <= this.today ? "Today" : this.shortDateFormat.format(new Date(event.day));
      return (
        day + " · " + (event.allDay ? "All day" : this.timeFormat.format(new Date(event.timestamp)))
      );
    };
    summary.replaceChildren(...next.map((event) => row(event.name, label(event), true)));
    if (!next.length)
      summary.textContent = this.calendarUnavailable
        ? "Calendar unavailable"
        : "No upcoming events";
    agenda.replaceChildren(...events.map((event) => row(event.name, label(event))));
    if (!events.length)
      agenda.append(
        row(
          this.calendarUnavailable ? "Calendar unavailable" : "No events in the next seven days",
          "",
        ),
      );
  }

  async subscribeWeather(session) {
    try {
      const unsubscribe = await this._hass.connection.subscribeMessage(
        (message) => {
          if (this.session !== session) return;
          this.forecast = message.forecast;
          this.renderWeather();
        },
        {
          type: "weather/subscribe_forecast",
          entity_id: this.config.weather,
          forecast_type: "daily",
        },
      );
      if (this.session === session) session.unsubscribeWeather = unsubscribe;
      else unsubscribe();
    } catch {
      if (this.session === session) this.renderWeather();
    }
  }

  renderWeather() {
    const state = this._hass.states[this.config.weather];
    const weather = this.shadowRoot.getElementById("weather");
    const forecast = this.forecast?.find(
      (item) => dayKey(new Date(item.datetime), this.timeZone) === this.today,
    );
    const condition = forecast?.condition ?? state?.state;
    if (!condition || ["unknown", "unavailable"].includes(condition)) {
      weather.hidden = true;
      return;
    }
    const display = condition[0].toUpperCase() + condition.slice(1).replaceAll("-", " ");
    const unit = state?.attributes.temperature_unit ?? "°C";
    const high = forecast?.temperature ?? state?.attributes.temperature;
    const low = forecast?.templow;
    const temperature =
      high == null ? "" : " · " + (low == null ? high : low + "–" + high) + " " + unit;
    weather.textContent = display + (forecast ? " today" : " now") + temperature;
    weather.hidden = false;
  }

  renderBins() {
    const rows = collectionRows(this.config.bins, this._hass.states, new Date(), this.timeZone);
    const first = rows.find((bin) => bin.days !== null);
    const dateLabel = (bin) => {
      if (bin.days === 0) return "Today";
      if (bin.days === 1) return "Tomorrow";
      return bin.days === null ? "Unavailable" : this.shortDateFormat.format(bin.date);
    };
    this.shadowRoot.getElementById("bin-summary").textContent = first
      ? dateLabel(first) +
        " · " +
        rows
          .filter((bin) => bin.days === first.days)
          .map((bin) => bin.name)
          .join(" + ")
      : "No pickup dates available";
    this.shadowRoot
      .getElementById("bin-details")
      .replaceChildren(...rows.map((bin) => row(bin.name, dateLabel(bin))));
  }

  renderRooms() {
    const reading = (entity) => {
      const state = this._hass.states[entity];
      if (!state || ["unavailable", "unknown"].includes(state.state)) return "—";
      const unit = state.attributes.unit_of_measurement;
      return state.state + (unit === "%" ? unit : unit ? " " + unit : "");
    };
    this.shadowRoot
      .getElementById("room-readings")
      .replaceChildren(
        ...this.config.rooms.map((room) =>
          row(room.name, reading(room.temperature) + " · " + reading(room.humidity), true),
        ),
      );
  }

  renderLights() {
    let on = 0;
    for (const [entity, { input }] of this.lightInputs) {
      const state = this._hass.states[entity]?.state;
      input.disabled = !["on", "off"].includes(state) || input.dataset.pending === "true";
      if (input.dataset.pending !== "true") input.checked = state === "on";
      if (input.checked) on++;
    }
    this.shadowRoot.getElementById("light-count").textContent = on + " on";
  }

  async setLight(entity, input) {
    input.dataset.pending = "true";
    this.renderLights();
    try {
      await this._hass.callService(
        "light",
        input.checked ? "turn_on" : "turn_off",
        {},
        { entity_id: entity },
      );
    } catch {
      this.dispatchEvent(
        new CustomEvent("hass-notification", {
          detail: { message: "Could not switch the light." },
          bubbles: true,
          composed: true,
        }),
      );
    } finally {
      delete input.dataset.pending;
      this.renderLights();
    }
  }

  async findAlbum() {
    const browse = async (media_content_id) =>
      (await this._hass.callWS({ type: "media_source/browse_media", media_content_id })).children;
    const accounts = await browse("media-source://immich");
    const matches = [];
    for (const account of accounts) {
      const albums = (await browse(account.media_content_id)).find(
        (group) => group.title.toLowerCase() === "albums",
      );
      if (!albums) continue;
      matches.push(
        ...(await browse(albums.media_content_id)).filter(
          (album) => album.title === this.config.album,
        ),
      );
    }
    return matches.length === 1 ? matches[0].media_content_id : undefined;
  }

  async rotatePhoto(session) {
    if (session.loadingPhoto) return;
    session.loadingPhoto = true;
    let image;
    try {
      session.albumId ??= await this.findAlbum();
      if (!session.albumId || this.session !== session) return;
      const album = await this._hass.callWS({
        type: "media_source/browse_media",
        media_content_id: session.albumId,
      });
      const photos = album.children.filter(
        (item) => item.media_class === "image" && item.media_content_id !== this.currentPhoto,
      );
      if (!photos.length) return;
      const photo = photos[Math.floor(Math.random() * photos.length)];
      // HA's Immich media view also serves the preview size used by the Immich app.
      const thumbnail = new URL(photo.thumbnail, location.origin);
      const path = thumbnail.pathname.replace(/\/thumbnail\/image\/[^/]+$/, "/preview/image/jpeg");
      if (
        thumbnail.origin !== location.origin ||
        !path.startsWith("/immich/") ||
        path === thumbnail.pathname
      )
        return;
      const signed = await this._hass.callWS({ type: "auth/sign_path", path });
      if (this.session !== session) return;
      image = new Image();
      image.alt = "";
      image.className = "photo";
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Photo load timed out")), 20000);
        const complete = (callback) => () => {
          clearTimeout(timeout);
          callback();
        };
        image.onload = complete(resolve);
        image.onerror = complete(reject);
        image.src = signed.path;
      });
      if (this.session !== session) return;
      const screen = this.shadowRoot.querySelector("main");
      const old = screen.querySelector(".photo");
      screen.append(image);
      this.currentPhoto = photo.media_content_id;
      if (old && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        image
          .animate([{ opacity: 0 }, { opacity: 1 }], 600)
          .finished.catch(() => {})
          .finally(() => old.remove());
      } else if (old) {
        old.remove();
      }
    } catch {
      session.albumId = undefined;
    } finally {
      if (image && !image.isConnected) image.removeAttribute("src");
      session.loadingPhoto = false;
    }
  }
}

customElements.define("front-door-tablet", FrontDoorTablet);
