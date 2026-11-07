/* Lutron To DMX Bridge web GUI - vanilla JS, no external assets.
 * All user/device-provided strings are inserted with textContent (never innerHTML).
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- utils
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** Create an element. attrs: props/attributes; children: nodes or strings (as text). */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k === "style") el.style.cssText = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (k in el && typeof v !== "string") el[k] = v;
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  const store = {
    // "lutron-dmx:" was the key prefix before the app was renamed; read it as a fallback so saved view settings survive.
    get(k, d) { try { let v = localStorage.getItem("lutron-to-dmx-bridge:" + k); if (v === null) v = localStorage.getItem("lutron-dmx:" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("lutron-to-dmx-bridge:" + k, JSON.stringify(v)); } catch (e) { /* ignore */ } },
  };

  let toastTimer = null;
  function toast(msg, bad, ms) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.toggle("bad", !!bad);
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), ms || (bad ? 6000 : 2500));
  }

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch("api/" + path, opts);
    } catch (e) {
      throw new Error("Cannot reach the server");
    }
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      if (data && data.detail) {
        msg = Array.isArray(data.detail)
          ? data.detail.map((d) => (d.loc ? d.loc.slice(1).join(".") + ": " : "") + d.msg).join("; ")
          : String(data.detail);
      }
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function fmtPct(v) { return Math.round(Number(v) || 0) + "%"; }

  function throttle(fn, ms) {
    let last = 0, timer = null, lastArgs = null;
    return function (...args) {
      lastArgs = args;
      const now = Date.now();
      const run = () => { last = Date.now(); timer = null; fn(...lastArgs); };
      if (now - last >= ms) run();
      else if (!timer) timer = setTimeout(run, ms - (now - last));
    };
  }

  function setFill(range) { range.style.setProperty("--fill", ((range.value - range.min) / (range.max - range.min)) * 100 + "%"); }

  // ---------------------------------------------------------------- state
  const state = {
    status: null,
    inv: { areas: [], zones: [], keypads: [], schedules: [] },
    zonesById: new Map(),
    tiles: new Map(),        // zone_id -> {root, toggle, slider, label}
    dragging: new Set(),     // zone ids being dragged by this user
    scenes: [],
    editor: null,            // {id, rows: Map(zone_id -> {checked, level})}
    dmx: null,
    dmxMapped: new Set(),    // zone ids with an enabled saved DMX mapping
    serialPorts: [],
    monitorTimer: null,
    monitorCells: [],
    monitorSelected: null,
    ws: null,
    wsOk: false,
    wsFailed: false,         // the websocket closed at least once
    msgAt: 0,                // when the last websocket message arrived
    statusAt: 0,             // when the last status arrived (websocket or polling)
    hist: { fps: [], cps: [], latency: [] },  // [ms timestamp, value|null] pairs, last HISTORY_S seconds (latency comes from the app)
    pop: false,              // status popover open
    tab: "devices",
  };

  // ---------------------------------------------------------------- tabs
  function showTab(name) {
    state.tab = name;
    store.set("tab", name);
    $$("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    $$("main > section").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + name));
    if (name === "devices") loadDmxMapped();
    if (name === "scenes") loadScenes();
    if (name === "schedules") { renderSchedules(); loadScheduleSets(); }
    if (name === "dmx") enterDmx(); else stopMonitor();
    if (name === "status") renderStatusTab();
    if (name === "settings") { loadDriverSettings(); loadUpdateSettings(); renderUpdateFacts(); }
    renderPop();
  }

  // ---------------------------------------------------------------- status
  const KIND_NAMES = { simulator: "Simulator", ra2: "RadioRA 2", leap: "LEAP" };
  const SLOW_LUTRON_MS = 500;    // p95 round-trip latency above this turns the Lutron chip amber
  const STALE_DMX_S = 2;         // no DMX packet for this long turns the DMX chip amber
  const STALE_MSG_MS = 4000;     // the app pushes status every second; this many ms of silence is "stale"
  const HISTORY_S = 120;
  const LATENCY_GAP_MS = 8000;   // latency samples further apart than this are drawn as separate lines

  function fmtDur(s) {
    s = Math.max(0, Math.round(s));
    const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + " d " + hr + " h";
    if (hr) return hr + " h " + m + " m";
    if (m) return m + " m " + String(s % 60).padStart(2, "0") + " s";
    return s + " s";
  }
  const fmtMs = (v) => (v === null || v === undefined ? "-" : Math.round(v) + " ms");
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  /** The Server chip: is this browser still hearing from the app? Judged in the browser, not by the app. */
  function serverModel() {
    const now = Date.now();
    if (state.wsOk) {
      const age = now - state.msgAt;
      if (age <= STALE_MSG_MS) return { cls: "ok", value: "live", title: "Live updates from the app are connected" };
      return { cls: "warn", value: "no data " + Math.round(age / 1000) + " s",
        title: "Connected, but the app has sent nothing for " + Math.round(age / 1000) + " s. It may be busy or stuck." };
    }
    if (!state.wsFailed) return { cls: "", value: "connecting", title: "Connecting to the app" };
    if (now - state.statusAt <= 8000) {
      return { cls: "warn", value: "polling", title: "Live updates are down; the page is asking the app for status every 5 s" };
    }
    return { cls: "bad", value: "offline", title: "Cannot reach the app. Is it still running? Retrying." };
  }

  const PROTECT_TEXT = {
    ok: { chip: "Full speed", row: "Full speed" },
    backoff: { chip: "Slowed down: the Lutron repeater is behind", row: "Slowed down because the repeater is behind" },
    hold: { chip: "Held: the repeater is seconds behind", row: "Holding commands until the repeater catches up" },
    paused: { chip: "Paused by you", row: "Paused by you" },
    tripped: { chip: "Paused automatically", row: "Paused automatically" },
  };

  /** Everything the chips, popover and Status tab show, derived from one status payload. */
  function statusModel(st) {
    const L = st.lutron || {}, lat = L.latency || {}, D = st.dmx || {}, rx = D.receiver || {};
    const A = st.alerts || { warnings: 0, errors: 0, recent: [] }, app = st.app || {};
    const d = st.driver || {};
    const srv = serverModel();
    const rate = Math.round(L.commands_per_s || 0);
    const Pr = D.protect || {};
    const protectOn = !!(D.enabled && Pr.enabled);
    const cfgBudget = D.enabled ? Number(D.max_commands_per_s) || 0 : 0;
    // with protection on, the limit shown is what the app allows right now
    const budget = protectOn && Pr.rate ? (cfgBudget ? Math.min(cfgBudget, Math.round(Pr.rate)) : Math.round(Pr.rate)) : cfgBudget;
    const held = D.held_back || 0;
    const Q = st.queue || {}, waiting = Q.waiting || 0;
    const reconnecting = !st.connected && (L.reconnect_attempts || 0) > 0;
    const m = { chips: {}, sections: [], alerts: A, app, srv };

    // Lutron
    {
      let cls = "ok", value = lat.count ? lat.median + " ms" : "connected";
      let title = "Connected to the Lutron system" + (lat.count ? ". Median round-trip latency over the last minute." : ". No latency measured yet.");
      if (!st.connected) { cls = "bad"; value = "offline"; title = reconnecting ? "Reconnecting to the Lutron system" : "Not connected to the Lutron system"; }
      else if (lat.p95 >= SLOW_LUTRON_MS) { cls = "warn"; title = "The repeater is slow to answer (95th percentile " + lat.p95 + " ms)"; }
      m.chips.lutron = { cls, value, title };
      const rows = [
        ["State", st.connected ? "Connected" : reconnecting
          ? "Reconnecting (attempt " + L.reconnect_attempts + (L.retry_in_s != null ? ", next try in " + L.retry_in_s + " s" : "") + ")"
          : "Not connected", st.connected ? "ok" : "bad"],
        ["System", (KIND_NAMES[d.kind] || d.kind || "-") + (d.host ? " · " + d.host : "")],
      ];
      if (st.connected && L.connected_for_s != null) rows.push(["Connected for", fmtDur(L.connected_for_s)]);
      if (L.reconnects) rows.push(["Reconnects since start", String(L.reconnects), "warn"]);
      rows.push(["Lutron latency",
        lat.count ? fmtMs(lat.last) + " (median " + fmtMs(lat.median) + ", p95 " + fmtMs(lat.p95) + ")" : "no replies in the last minute",
        lat.p95 >= SLOW_LUTRON_MS ? "warn" : ""]);
      rows.push(["Commands in flight", String(L.inflight || 0), (L.inflight || 0) > 10 ? "warn" : ""]);
      m.sections.push({ id: "lutron", title: "Lutron link", cls, rows });
    }

    // DMX in
    {
      let cls = "ok", value = "-", title = "";
      const age = D.last_packet_age;
      if (!D.enabled) { cls = "off"; value = "off"; title = "DMX input is switched off"; }
      else if (!D.running) { cls = "bad"; value = "stopped"; title = D.error || "DMX is enabled but not running"; }
      else if (D.error) { cls = "warn"; value = "error"; title = D.error; }
      else if (age === null || age === undefined) { cls = "warn"; value = "waiting"; title = "Listening, but no DMX packet has arrived yet"; }
      else if (age >= STALE_DMX_S) { cls = "warn"; value = "no data " + Math.round(age) + " s"; title = "No DMX packet for " + Math.round(age) + " s"; }
      else {
        value = Math.round(D.fps || 0) + " fps" + (D.source === "usb" ? "" : " · U" + D.universe);
        title = "Receiving " + (D.source_label || D.source) + " at " + (D.fps || 0) + " frames/s";
      }
      m.chips.dmx = { cls, value, title };
      const rows = [];
      if (!D.enabled) rows.push(["Input", "Switched off (DMX tab)", ""]);
      else {
        rows.push(["Source", (D.source_label || D.source || "-") + (D.source === "usb" ? "" : " · universe " + D.universe)]);
        if (rx.last_sender) rows.push(["Sender", rx.last_sender]);
        else if (Array.isArray(rx.sources)) rows.push(["Sources", String(rx.sources.length)]);
        rows.push(["Frames/s", (D.fps || 0).toFixed(1), cls === "warn" || cls === "bad" ? cls : ""]);
        rows.push(["Last packet", age === null || age === undefined ? "none yet" : age < 1 ? Math.round(age * 1000) + " ms ago" : age.toFixed(1) + " s ago",
          age >= STALE_DMX_S ? "warn" : ""]);
        rows.push(["Packets total", Number(D.packets || 0).toLocaleString()]);
        rows.push(["Mapped lights", String(D.mapped_zones || 0)]);
        if ((D.warnings || []).length) rows.push(["Patch warnings", String(D.warnings.length), "warn"]);
        if ((rx.other_universes || []).length) rows.push(["Also seeing universe", rx.other_universes.join(", ") + " (listening on " + D.universe + ")", "warn"]);
        if (D.error) rows.push(["Error", D.error, "warn"]);
      }
      m.sections.push({ id: "dmx", title: "DMX input", cls, rows });
    }

    // Output
    {
      let cls = held > 0 || (L.inflight || 0) > 10 ? "warn" : "ok";
      let title = "Commands sent to the Lutron system per second" + (budget ? ", against the limit set on the DMX tab" : "");
      if (held > 0) title += ". " + plural(held, "light") + " waiting for the limit.";
      let value = budget ? rate + "/" + budget + " cmd/s" : rate + " cmd/s";
      const prot = PROTECT_TEXT[Pr.state];
      if (protectOn && (Pr.state === "backoff" || Pr.state === "hold")) { cls = "warn"; title = prot.chip + ". " + title; }
      if (D.paused) { cls = "bad"; value = "paused"; title = Pr.reason || "DMX output is paused"; }
      if (!st.connected) cls = "off";
      m.chips.out = { cls, value, title, pct: budget && !D.paused ? Math.min(100, (rate / budget) * 100) : null, held };
      const rows = [
        ["Commands/s", rate + (budget ? " of " + budget + " allowed" : ""), held > 0 ? "warn" : ""],
        ["Held back by the limit", held ? plural(held, "light") : "none", held ? "warn" : ""],
        ["Waiting in the app", waiting ? plural(waiting, "update") : "none", waiting ? "warn" : ""],
        ["Sent since start", Number(L.commands_total || 0).toLocaleString()],
      ];
      if (D.enabled) {
        const text = prot ? prot.row : "";  // the banner carries the reason
        if (!protectOn && !D.paused) rows.push(["Repeater protection", "Off (DMX tab)", "warn"]);
        else if (text) rows.push(["Repeater protection", text, Pr.state === "ok" ? "ok" : Pr.state === "backoff" || Pr.state === "hold" ? "warn" : "bad"]);
        if (protectOn && Pr.slowdowns) rows.push(["Slowed down since start", plural(Pr.slowdowns, "time"), "warn"]);
      }
      m.sections.push({ id: "out", title: "Output to Lutron", cls, rows });
    }

    // Alerts
    {
      const e = A.errors || 0, w = A.warnings || 0;
      const cls = e ? "bad" : w ? "warn" : "ok";
      const value = e ? e + " err" + (w ? " · " + w + " warn" : "") : w ? w + " warn" : "none";
      m.chips.alerts = { cls, value, title: e || w ? "Warnings and errors since the app started or since cleared. Open the Status tab for details." : "No warnings or errors" };
    }

    // Server (this browser) and app
    m.chips.server = { cls: srv.cls, value: srv.value, title: srv.title };
    const setRow = st.schedule_set ? st.schedule_set.set_name + " (" + plural(st.schedule_set.count, "schedule") + " held off)" : "None active";
    m.sections.push({ id: "app", title: "App", cls: srv.cls === "bad" ? "bad" : (A.errors ? "bad" : A.warnings ? "warn" : "ok"), rows: [
      ["Schedule set", setRow],
      ["Warnings / errors", (A.warnings || 0) + " / " + (A.errors || 0), A.errors ? "bad" : A.warnings ? "warn" : ""],
      ["Version", app.version || "-"],
      ["Uptime", app.uptime_s != null ? fmtDur(app.uptime_s) : "-"],
      ["Browsers connected", app.clients != null ? String(app.clients) : "-"],
      ["This browser", srv.cls === "ok" ? "Live updates connected" : srv.value, srv.cls === "ok" ? "ok" : srv.cls],
    ] });
    return m;
  }

  function paintChip(id, c) {
    const el = $("#chip-" + id);
    el.classList.remove("ok", "warn", "bad", "off");
    if (c.cls) el.classList.add(c.cls);
    $(".v", el).textContent = c.value;
    el.title = c.title;
    if (id === "out") {
      const meter = $(".meter", el), held = $(".held", el);
      meter.classList.toggle("hidden", c.pct === null);
      $("i", meter).style.width = (c.pct || 0).toFixed(0) + "%";
      held.classList.toggle("hidden", !c.held);
      held.textContent = c.held ? c.held + " held" : "";
    }
  }

  function renderStatus(st) {
    // The app was replaced by an update while this page stayed open: load the new page and script.
    const ver = st.app && st.app.version;
    if (ver) {
      if (state.appVersion && state.appVersion !== ver) { location.reload(); return; }
      state.appVersion = ver;
    }
    state.status = st;
    state.statusAt = Date.now();
    $("#system-name").textContent = st.system_name || "Lutron";
    document.title = (st.system_name || "Lutron") + " · Lutron To DMX Bridge";
    const d = st.driver || {};
    $("#driver-label").textContent = (KIND_NAMES[d.kind] || d.kind || "") + (d.host ? " · " + d.host : "") +
      (st.connected ? " · connected" : " · not connected");
    renderSetBanner(st.schedule_set);
    renderProtect(st.dmx);
    renderUpdate(st.update);
    const banner = $("#error-banner");
    if (st.last_error) { banner.textContent = st.last_error; banner.classList.remove("hidden"); }
    else banner.classList.add("hidden");
    pushHistory(st);
    renderStatusArea();
  }

  /** The pause switch in the header, and the banner shown while DMX output is paused. */
  function renderProtect(D) {
    D = D || {};
    const Pr = D.protect || {}, live = !!D.running, btn = $("#btn-dmx-pause"), bn = $("#protect-banner");
    btn.classList.toggle("hidden", !live);
    btn.textContent = D.paused ? "Resume DMX" : "Pause DMX";
    btn.classList.toggle("danger", !D.paused);
    btn.classList.toggle("primary", !!D.paused);
    bn.classList.toggle("hidden", !(live && D.paused));
    bn.classList.toggle("alert", !!Pr.auto);
    if (live && D.paused) {
      const text = $("#protect-banner-text");
      clear(text);
      text.appendChild(document.createTextNode(Pr.auto ? "DMX output was paused automatically: " + (Pr.reason || "storm detected")
        : "DMX output is paused. Nothing from the DMX input reaches the lights."));
      text.appendChild(h("small", { text: Pr.auto
        ? "The lights keep their last levels. The Lutron repeater may still be working through commands it had already accepted: give it a minute, look at the latency on the Status tab, make sure the source is calm, then resume."
        : "The input is still tracked. When you resume, the lights catch up to the current DMX values, starting slowly." }));
    }
  }

  async function toggleDmxPause() {
    const paused = !!(state.status && state.status.dmx && state.status.dmx.paused);
    try {
      await api("POST", "dmx/pause", { paused: !paused });
      toast(paused ? "DMX resumed" : "DMX paused");
      await loadStatus();
    } catch (e) { toast(e.message, true); }
  }

  /** Repaint the header chips and, when visible, the popover and Status tab. */
  function renderStatusArea() {
    if (!state.status) return;
    const m = statusModel(state.status);
    for (const id of Object.keys(m.chips)) paintChip(id, m.chips[id]);
    const n = (m.alerts.errors || 0) + (m.alerts.warnings || 0);
    const badge = $("#status-badge");
    badge.textContent = String(n);
    badge.classList.toggle("hidden", n === 0);
    badge.classList.toggle("bad", (m.alerts.errors || 0) > 0);
    renderPop(m);
    if (state.tab === "status") renderStatusTab(m);
  }

  function pushHistory(st) {
    const now = Date.now();
    if (now - (state.histAt || 0) < 700) return;  // a manual refresh next to a push must not double up
    state.histAt = now;
    const L = st.lutron || {}, D = st.dmx || {};
    const add = (k, v) => {
      const a = state.hist[k];
      a.push([now, v]);
      while (a.length && now - a[0][0] > HISTORY_S * 1000) a.shift();
    };
    add("fps", D.running ? D.fps || 0 : null);
    add("cps", st.connected ? L.commands_per_s || 0 : null);
    // Round-trip samples are timed by the app (one every couple of seconds, busy or idle), so take its
    // history as it is instead of sampling the latest value once a second.
    state.hist.latency = ((L.latency || {}).history || []).map(([age, ms]) => [now - age * 1000, ms]);
  }

  function dlRows(rows) {
    const dl = h("dl", { class: "kv" });
    for (const [k, v, cls] of rows) { dl.appendChild(h("dt", { text: k })); dl.appendChild(h("dd", { class: cls || "", text: v })); }
    return dl;
  }
  function sectionHead(sec) { return h("i", { class: "dot " + (sec.cls === "off" ? "" : sec.cls) }); }

  // -- popover
  function renderPop(m) {
    const pop = $("#status-pop");
    pop.classList.toggle("hidden", !state.pop);
    $$("#status-chips .chip").forEach((c) => c.setAttribute("aria-expanded", String(state.pop)));
    if (!state.pop || !state.status) return;
    m = m || statusModel(state.status);
    const top = pop.scrollTop;
    clear(pop);
    for (const sec of m.sections) {
      pop.appendChild(h("section", { class: "status-section" }, h("h4", null, sectionHead(sec), sec.title), dlRows(sec.rows)));
    }
    pop.appendChild(h("div", { class: "status-pop-foot" },
      state.tab === "status" ? null : h("button", { class: "btn", text: "Open Status tab", onclick: () => { closePop(); showTab("status"); } }),
      h("button", { class: "btn ghost", text: "Close", onclick: () => closePop(true) })));
    pop.scrollTop = top;
  }
  function togglePop() { state.pop = !state.pop; renderPop(); }
  function closePop(refocus) {
    if (!state.pop) return;
    state.pop = false;
    renderPop();
    if (refocus) $("#chip-lutron").focus();
  }

  // -- Status tab
  function niceMax(v, min) {
    const target = Math.max(v * 1.1, min);
    const mag = Math.pow(10, Math.floor(Math.log10(target)));
    for (const step of [1, 2, 5, 10]) if (step * mag >= target) return step * mag;
    return 10 * mag;
  }

  /** One sparkline: samples are [ms, value|null]; x is time over the last HISTORY_S seconds, y is 0..max. */
  function sparkSvg(samples, max, color, limit, gap) {
    const W = 300, H = 72, padT = 6, padB = 4, now = Date.now(), span = HISTORY_S * 1000;
    const X = (t) => ((t - (now - span)) / span) * W;
    const Y = (v) => padT + (1 - Math.min(v, max) / max) * (H - padT - padB);
    let svg = "";
    for (const f of [0, 0.5, 1]) {
      const y = padT + f * (H - padT - padB);
      svg += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" style="stroke:var(--line)" stroke-width="1"' + (f < 1 ? ' stroke-dasharray="3 4"' : "") + "/>";
    }
    if (limit && limit <= max) {
      const y = Y(limit).toFixed(1);
      svg += '<line x1="0" x2="' + W + '" y1="' + y + '" y2="' + y + '" style="stroke:var(--warn)" stroke-width="1" stroke-dasharray="6 4" opacity=".7"/>';
    }
    const segs = [];
    let prev = null, cur = null;
    for (const [t, v] of samples) {
      if (v === null || v === undefined) { prev = null; cur = null; continue; }
      if (prev === null || t - prev > (gap || 3000)) { cur = []; segs.push(cur); }
      cur.push([X(t), Y(v)]);
      prev = t;
    }
    for (const seg of segs) {
      if (seg.length === 1) { svg += '<circle cx="' + seg[0][0].toFixed(1) + '" cy="' + seg[0][1].toFixed(1) + '" r="2" style="fill:' + color + '"/>'; continue; }
      const pts = seg.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1));
      const last = seg[seg.length - 1], first = seg[0];
      svg += '<path d="M' + pts.join(" L") + " L" + last[0].toFixed(1) + " " + (H - padB) + " L" + first[0].toFixed(1) + " " + (H - padB) +
        ' Z" style="fill:' + color + '" fill-opacity=".14"/>';
      svg += '<path d="M' + pts.join(" L") + '" fill="none" style="stroke:' + color + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>';
    }
    const lastSample = samples[samples.length - 1];
    if (lastSample && lastSample[1] !== null && lastSample[1] !== undefined && now - lastSample[0] <= (gap || 3000)) {
      svg += '<circle cx="' + X(lastSample[0]).toFixed(1) + '" cy="' + Y(lastSample[1]).toFixed(1) + '" r="3.2" style="fill:' + color + '"/>';
    }
    const holder = h("div");
    holder.innerHTML = '<svg class="spark" viewBox="0 0 ' + W + " " + H + '" role="img">' + svg + "</svg>";  // numbers only, no user text
    return holder.firstChild;
  }

  function graphCard(title, big, unit, samples, max, color, maxUnit, limit, gap) {
    const svg = sparkSvg(samples, max, color, limit, gap);
    svg.setAttribute("aria-label", title + " over the last 2 minutes");
    return h("div", { class: "card" }, h("h3", { text: title }),
      h("div", { class: "big" }, big, h("small", { text: unit })), svg,
      h("div", { class: "spark-foot" }, h("span", { text: "2 min ago" }), h("span", { text: "0 to " + max + " " + maxUnit }), h("span", { text: "now" })));
  }

  function renderStatusTab(m) {
    if (!state.status) return;
    m = m || statusModel(state.status);
    const st = state.status, L = st.lutron || {}, lat = L.latency || {}, D = st.dmx || {};
    const budget = D.enabled ? Number(D.max_commands_per_s) || 0 : 0;

    const secs = $("#status-sections");
    clear(secs);
    for (const sec of m.sections) {
      secs.appendChild(h("div", { class: "card" }, h("h3", null, sectionHead(sec), sec.title), dlRows(sec.rows)));
    }

    const peak = (k) => state.hist[k].reduce((a, p) => (p[1] > a ? p[1] : a), 0);
    const g = $("#status-graphs");
    clear(g);
    g.appendChild(graphCard("DMX frames in", D.running ? String(Math.round(D.fps || 0)) : "-", "frames/s",
      state.hist.fps, niceMax(peak("fps"), 50), "var(--dmx)", "frames/s"));
    g.appendChild(graphCard("Commands to Lutron", String(Math.round(L.commands_per_s || 0)), budget ? "cmd/s, limit " + budget : "cmd/s",
      state.hist.cps, niceMax(Math.max(peak("cps"), budget * 1.25), 10), "var(--accent)", "cmd/s", budget || null));
    // the app times a round trip to the Lutron system every couple of seconds; a few missed ones still join up
    g.appendChild(graphCard("Lutron latency", lat.last === null || lat.last === undefined ? "-" : String(lat.last), "ms" + (lat.count ? " · median " + lat.median + ", p95 " + lat.p95 : ""),
      state.hist.latency, niceMax(peak("latency"), 200), "var(--ok)", "ms", SLOW_LUTRON_MS, LATENCY_GAP_MS));

    const A = m.alerts, list = $("#status-log");
    clear(list);
    for (const r of A.recent || []) {
      list.appendChild(h("li", null, h("span", { class: "t", text: new Date(r.time * 1000).toLocaleTimeString() }),
        h("span", { class: "lvl " + r.level, text: r.level }), h("span", { text: r.message })));
    }
    $("#status-log-empty").classList.toggle("hidden", (A.recent || []).length > 0);
    $("#status-log-dot").className = "dot " + (A.errors ? "bad" : A.warnings ? "warn" : "ok");
    $("#btn-alerts-clear").disabled = !(A.errors || A.warnings);
    const waiting = (st.queue || {}).waiting || 0;
    $("#btn-queue-clear").textContent = waiting ? "Clear Lutron queue (" + waiting + ")" : "Clear Lutron queue";
    $("#btn-queue-clear").disabled = !st.connected;
  }

  /** Say what a clear did and, as important, what it could not undo. */
  function queueClearMessage(r) {
    const parts = [r.dropped ? "Dropped " + plural(r.dropped, "waiting update") + "." : "Nothing was waiting in the app."];
    if (r.inflight) parts.push(plural(r.inflight, "command") + " already sent to the repeater can't be recalled.");
    if (r.fading) parts.push(plural(r.fading, "light") + " will finish the fade already started.");
    return parts.join(" ");
  }

  async function clearQueue() {
    const btn = $("#btn-queue-clear");
    btn.disabled = true;
    try {
      const r = await api("POST", "queue/clear");
      toast(queueClearMessage(r), false, 7000);
      if (r.status) renderStatus(r.status); else await loadStatus();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = !(state.status && state.status.connected); }
  }

  async function clearAlerts() {
    try { await api("POST", "alerts/clear"); await loadStatus(); }
    catch (e) { toast(e.message, true); }
  }

  /** Keep the sticky tab bar and the popover just under the header, whatever height it wraps to. */
  function trackTopbarHeight() {
    const bar = $(".topbar");
    const set = () => document.documentElement.style.setProperty("--topbar-h", bar.offsetHeight + "px");
    set();
    if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
    else window.addEventListener("resize", set);
  }

  async function loadStatus() {
    try { renderStatus(await api("GET", "status")); } catch (e) { renderStatusArea(); /* the Server chip shows the problem */ }
  }

  // ---------------------------------------------------------------- inventory / devices
  async function loadInventory() {
    try {
      const inv = await api("GET", "inventory");
      setInventory(inv);
    } catch (e) {
      toast("Could not load devices: " + e.message, true);
    }
  }

  function setInventory(inv) {
    const oldKey = invKey(state.inv);
    state.inv = inv;
    state.zonesById = new Map(inv.zones.map((z) => [z.id, z]));
    if (invKey(inv) !== oldKey || state.tiles.size === 0) renderDevices();
    else inv.zones.forEach((z) => updateTile(z.id, z.level));
    if (state.tab === "schedules") renderSchedules();
  }

  function invKey(inv) {
    return JSON.stringify([inv.areas, inv.zones.map((z) => [z.id, z.name, z.area_id, z.type]), inv.keypads]);
  }

  function groupByArea() {
    const areaIds = new Set(state.inv.areas.map((a) => a.id));
    const groups = state.inv.areas.map((a) => ({ id: a.id, name: a.name, zones: [], keypads: [] }));
    const byId = new Map(groups.map((g) => [g.id, g]));
    const other = { id: "__other__", name: "Other", zones: [], keypads: [] };
    for (const z of state.inv.zones) (areaIds.has(z.area_id) ? byId.get(z.area_id) : other).zones.push(z);
    for (const k of state.inv.keypads || []) (areaIds.has(k.area_id) ? byId.get(k.area_id) : other).keypads.push(k);
    groups.push(other);
    return groups.filter((g) => g.zones.length || g.keypads.length);
  }

  function renderDevices() {
    const root = $("#areas");
    clear(root);
    state.tiles.clear();
    const groups = groupByArea();
    $("#devices-empty").classList.toggle("hidden", state.inv.zones.length > 0);
    const closed = new Set(store.get("closedAreas", []));
    for (const g of groups) root.appendChild(renderArea(g, !closed.has(g.id)));
  }

  function renderArea(g, open) {
    const det = h("details", { class: "area", open: open, dataset: { area: g.id } });
    det.addEventListener("toggle", () => {
      const closed = new Set(store.get("closedAreas", []));
      if (det.open) closed.delete(g.id); else closed.add(g.id);
      store.set("closedAreas", Array.from(closed));
    });
    const onCount = h("span", { class: "area-count" });
    det.appendChild(h("summary", null, h("span", { class: "area-name", text: g.name }), onCount));
    const body = h("div", { class: "area-body" });

    if (g.zones.length && g.id !== "__other__") {
      const master = h("input", { type: "range", min: 0, max: 100, step: 1, value: 0, "aria-label": "All zones in " + g.name });
      setFill(master);
      master.addEventListener("input", () => setFill(master));
      master.addEventListener("change", async () => {
        try { await api("POST", "areas/" + encodeURIComponent(g.id) + "/level", { level: Number(master.value) }); }
        catch (e) { toast(e.message, true); }
      });
      const allBtn = (label, lvl) => h("button", {
        class: "btn ghost", text: label, onclick: async () => {
          try {
            await api("POST", "areas/" + encodeURIComponent(g.id) + "/level", { level: lvl });
            master.value = lvl; setFill(master);
          } catch (e) { toast(e.message, true); }
        },
      });
      body.appendChild(h("div", { class: "area-master" }, h("span", { class: "lbl", text: "Whole area" }), master, allBtn("All on", 100), allBtn("All off", 0)));
    }

    const zones = h("div", { class: "zones" });
    for (const z of g.zones) zones.appendChild(renderZone(z));
    body.appendChild(zones);

    if (g.keypads.length) {
      const kp = h("div", { class: "keypads" }, h("h4", { text: "Keypads (read-only)" }));
      for (const k of g.keypads) {
        kp.appendChild(h("div", { class: "keypad" },
          h("div", { class: "keypad-name", text: k.name }),
          (k.buttons && k.buttons.length) ? h("div", { class: "chips" }, k.buttons.map((b) => h("span", { class: "chip", text: b }))) : null));
      }
      body.appendChild(kp);
    }
    det.appendChild(body);
    det._updateCount = () => {
      const n = g.zones.filter((z) => (state.zonesById.get(z.id) || z).level > 0).length;
      onCount.textContent = g.zones.length ? n + " / " + g.zones.length + " on" : g.keypads.length + " keypad(s)";
    };
    det._updateCount();
    g.zones.forEach((z) => { state.tiles.get(z.id).area = det; });
    return det;
  }

  function renderZone(z) {
    const isSwitch = z.type === "switch" || z.type === "fan";
    const isShade = z.type === "shade";
    const toggle = h("button", { class: "toggle", role: "switch", "aria-checked": "false", "aria-label": (isShade ? "Open " : "Turn on ") + z.name });
    const label = h("span", { class: "zone-level" });
    const slider = h("input", { type: "range", min: 0, max: 100, step: 1, value: z.level, "aria-label": z.name + " level" });
    const root = h("div", { class: "zone", dataset: { zone: z.id } },
      h("div", { class: "zone-name", text: z.name, title: z.name }),
      h("div", { class: "zone-tag hidden" }, h("span", { class: "dmx-badge", text: "DMX Controlled", title: "Mapped to a DMX address - level follows DMX input" })),
      h("div", { class: "zone-head" },
        h("span", { class: "zone-type", text: z.type !== "dimmer" ? z.type : "" }),
        label, toggle),
      isSwitch ? null : slider);

    toggle.addEventListener("click", async () => {
      const turnOn = toggle.getAttribute("aria-checked") !== "true";
      const prev = (state.zonesById.get(z.id) || z).level;
      updateTile(z.id, turnOn ? 100 : 0, true);
      try { await api("POST", "zones/" + encodeURIComponent(z.id) + (turnOn ? "/on" : "/off")); }
      catch (e) { updateTile(z.id, prev, true); toast(e.message, true); }
    });

    const send = (v) => api("POST", "zones/" + encodeURIComponent(z.id) + "/level", { level: v }).catch((e) => toast(e.message, true));
    const sendThrottled = throttle(send, 250);
    const startDrag = () => state.dragging.add(z.id);
    const endDrag = () => setTimeout(() => state.dragging.delete(z.id), 600);
    slider.addEventListener("pointerdown", startDrag);
    slider.addEventListener("touchstart", startDrag, { passive: true });
    slider.addEventListener("pointerup", endDrag);
    slider.addEventListener("touchend", endDrag);
    slider.addEventListener("input", () => { updateTile(z.id, Number(slider.value), true, true); sendThrottled(Number(slider.value)); });
    slider.addEventListener("change", () => { send(Number(slider.value)); endDrag(); });

    state.tiles.set(z.id, { root, toggle, slider, label, isSwitch, tag: root.querySelector(".zone-tag") });
    paintTile(state.tiles.get(z.id), z.level);
    paintDmxTag(state.tiles.get(z.id), z.id);
    return root;
  }

  function paintDmxTag(t, zoneId) {
    const mapped = state.dmxMapped.has(zoneId);
    t.root.classList.toggle("dmx", mapped);
    t.tag.classList.toggle("hidden", !mapped);
  }

  /** Recompute which zones are DMX controlled from a saved mappings list and restyle the cards. */
  function setDmxMapped(mappings) {
    state.dmxMapped = new Set((mappings || []).filter((m) => m.enabled !== false && (m.params || []).length).map((m) => m.zone_id));
    state.tiles.forEach((t, id) => paintDmxTag(t, id));
  }

  async function loadDmxMapped() {
    try { setDmxMapped((await api("GET", "settings/dmx")).mappings); } catch (e) { /* styling only; leave as is */ }
  }

  function paintTile(t, level, skipSlider) {
    const on = level > 0;
    t.root.classList.toggle("on", on);
    t.root.style.setProperty("--glow", (level / 100).toFixed(2));
    t.toggle.setAttribute("aria-checked", String(on));
    t.label.textContent = t.isSwitch ? (on ? "On" : "Off") : fmtPct(level);
    if (!skipSlider) { t.slider.value = level; }
    setFill(t.slider);
  }

  /** Update a zone's displayed level. local=true for optimistic user changes. */
  function updateTile(zoneId, level, local, fromSlider) {
    const z = state.zonesById.get(zoneId);
    if (z) { z.level = level; z.is_on = level > 0; }
    const t = state.tiles.get(zoneId);
    if (!t) return;
    if (!local && state.dragging.has(zoneId)) return; // don't fight the user's finger
    paintTile(t, level, fromSlider);
    if (t.area && t.area._updateCount) t.area._updateCount();
    if (state.editor && state.tab === "scenes") { /* editor uses its own values */ }
  }

  // ---------------------------------------------------------------- websocket
  let wsRetry = 1000, pollTimer = null;
  function connectWs() {
    const base = location.pathname.replace(/[^/]*$/, "");
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + base + "ws";
    let ws;
    try { ws = new WebSocket(url); } catch (e) { scheduleWs(); return; }
    state.ws = ws;
    ws.onopen = () => {
      state.wsOk = true; state.msgAt = Date.now(); wsRetry = 1000;
      stopPolling();
      renderStatusArea();
      loadInventory();
    };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      state.msgAt = Date.now();
      if (msg.type === "level") updateTile(msg.zone_id, Number(msg.level));
      else if (msg.type === "status") {
        const wasConnected = state.status && state.status.connected;
        renderStatus(msg);
        if (msg.connected && !wasConnected) loadInventory();
      } else if (msg.type === "inventory_changed") loadInventory();
    };
    ws.onclose = () => {
      state.wsOk = false; state.wsFailed = true;
      renderStatusArea();
      startPolling();
      scheduleWs();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
  }
  function scheduleWs() { setTimeout(connectWs, wsRetry); wsRetry = Math.min(wsRetry * 2, 15000); }
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => { loadStatus(); loadInventory(); }, 5000);
  }
  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

  // ---------------------------------------------------------------- scenes
  async function loadScenes() {
    try { state.scenes = await api("GET", "scenes"); renderScenes(); }
    catch (e) { toast("Could not load scenes: " + e.message, true); }
  }

  function zoneName(id) { const z = state.zonesById.get(id); return z ? z.name : id + " (unknown)"; }

  function renderScenes() {
    const list = $("#scene-list");
    clear(list);
    $("#scenes-empty").classList.toggle("hidden", state.scenes.length > 0);
    for (const s of state.scenes) {
      const n = Object.keys(s.levels || {}).length;
      const names = Object.keys(s.levels || {}).slice(0, 4).map(zoneName).join(", ") + (n > 4 ? ", …" : "");
      list.appendChild(h("div", { class: "item" },
        h("div", { class: "item-head" }, h("span", { class: "item-title", text: s.name })),
        h("div", { class: "item-sub", text: n + " zone" + (n === 1 ? "" : "s") + (s.fade != null ? " · fade " + s.fade + " s" : "") }),
        n ? h("div", { class: "item-sub", text: names }) : null,
        h("div", { class: "item-actions" },
          h("button", { class: "btn primary", text: "Activate", onclick: () => activateScene(s) }),
          h("button", { class: "btn", text: "Edit", onclick: () => openEditor(s) }),
          h("button", { class: "btn danger", text: "Delete", onclick: () => deleteScene(s) }))));
    }
  }

  async function activateScene(s) {
    try { await api("POST", "scenes/" + encodeURIComponent(s.id) + "/activate"); toast("Activated " + s.name); }
    catch (e) { toast(e.message, true); }
  }

  async function deleteScene(s) {
    if (!confirm("Delete scene \"" + s.name + "\"?")) return;
    try {
      await api("DELETE", "scenes/" + encodeURIComponent(s.id));
      if (state.editor && state.editor.id === s.id) closeEditor();
      toast("Deleted " + s.name);
      loadScenes();
    } catch (e) { toast(e.message, true); }
  }

  async function snapshotAll() {
    const name = prompt("Name for the new scene (captures every zone's current level):", "Snapshot " + new Date().toLocaleString());
    if (!name || !name.trim()) return;
    try { await api("POST", "scenes/capture", { name: name.trim(), zone_ids: [] }); toast("Saved " + name.trim()); loadScenes(); }
    catch (e) { toast(e.message, true); }
  }

  function openEditor(scene) {
    const rows = new Map();
    for (const z of state.inv.zones) rows.set(z.id, { checked: false, level: Math.round(z.level) });
    if (scene) {
      for (const [zid, lvl] of Object.entries(scene.levels || {})) rows.set(zid, { checked: true, level: Number(lvl) });
    }
    state.editor = { id: scene ? scene.id : null, rows };
    $("#scene-editor-title").textContent = scene ? "Edit scene" : "New scene";
    $("#scene-name").value = scene ? scene.name : "";
    $("#scene-fade").value = scene && scene.fade != null ? scene.fade : "";
    renderEditorRows();
    $("#scene-editor").classList.remove("hidden");
    $("#scene-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeEditor() { state.editor = null; $("#scene-editor").classList.add("hidden"); }

  function renderEditorRows() {
    const root = $("#scene-zones");
    clear(root);
    const ed = state.editor;
    const groups = groupByArea().filter((g) => g.zones.length);
    const known = new Set(state.inv.zones.map((z) => z.id));
    const unknown = Array.from(ed.rows.keys()).filter((id) => !known.has(id));
    if (unknown.length) groups.push({ id: "__unknown__", name: "Not in current system", zones: unknown.map((id) => ({ id, name: id })) });
    for (const g of groups) {
      root.appendChild(h("div", { class: "scene-area", text: g.name }));
      for (const z of g.zones) {
        const r = ed.rows.get(z.id);
        const cb = h("input", { type: "checkbox", checked: r.checked, "aria-label": "Include " + z.name });
        const rng = h("input", { type: "range", min: 0, max: 100, step: 1, value: r.level, "aria-label": z.name + " level" });
        const num = h("input", { type: "number", min: 0, max: 100, step: 1, value: r.level, "aria-label": z.name + " level percent" });
        const row = h("label", { class: "scene-row" + (r.checked ? "" : " off") }, cb, h("span", { class: "nm", text: z.name, title: z.name }), rng, num);
        setFill(rng);
        cb.addEventListener("change", () => { r.checked = cb.checked; row.classList.toggle("off", !cb.checked); });
        const setVal = (v) => {
          v = Math.max(0, Math.min(100, Number(v) || 0));
          r.level = v; rng.value = v; num.value = v; setFill(rng);
          if (!r.checked) { r.checked = true; cb.checked = true; row.classList.remove("off"); }
        };
        rng.addEventListener("input", () => setVal(rng.value));
        num.addEventListener("change", () => setVal(num.value));
        r.setVal = setVal;
        r.setChecked = (c) => { r.checked = c; cb.checked = c; row.classList.toggle("off", !c); };
        root.appendChild(row);
      }
    }
  }

  function editorCapture() {
    const ed = state.editor;
    if (!ed) return;
    let any = false;
    for (const r of ed.rows.values()) if (r.checked) any = true;
    for (const [zid, r] of ed.rows) {
      const z = state.zonesById.get(zid);
      if (!z) continue;
      if (!any) r.setChecked && r.setChecked(true);
      if (r.checked) r.setVal ? r.setVal(Math.round(z.level)) : (r.level = Math.round(z.level));
    }
    toast(any ? "Captured current levels of selected zones" : "Captured current levels of all zones");
  }

  function editorLevels() {
    const levels = {};
    for (const [zid, r] of state.editor.rows) if (r.checked) levels[zid] = Number(r.level);
    return levels;
  }

  async function editorPreview() {
    const levels = editorLevels();
    const entries = Object.entries(levels).filter(([zid]) => state.zonesById.has(zid));
    if (!entries.length) { toast("Select some zones first", true); return; }
    try {
      await Promise.all(entries.map(([zid, lvl]) => api("POST", "zones/" + encodeURIComponent(zid) + "/level", { level: lvl })));
      toast("Previewing on the lights");
    } catch (e) { toast(e.message, true); }
  }

  async function editorSave() {
    const ed = state.editor;
    const name = $("#scene-name").value.trim();
    if (!name) { toast("Give the scene a name", true); $("#scene-name").focus(); return; }
    const levels = editorLevels();
    if (!Object.keys(levels).length) { toast("Select at least one zone", true); return; }
    const fadeRaw = $("#scene-fade").value.trim();
    const body = { name, levels, fade: fadeRaw === "" ? null : Number(fadeRaw) };
    try {
      if (ed.id) await api("PUT", "scenes/" + encodeURIComponent(ed.id), body);
      else await api("POST", "scenes", body);
      toast("Saved " + name);
      closeEditor();
      loadScenes();
    } catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- schedules
  function renderSchedules() {
    const list = $("#schedule-list");
    clear(list);
    const scheds = state.inv.schedules || [];
    $("#schedules-empty").classList.toggle("hidden", scheds.length > 0);
    for (const s of scheds) {
      const tg = h("button", { class: "toggle", role: "switch", "aria-checked": String(!!s.enabled), "aria-label": "Enable " + s.name });
      const badge = h("span", { class: "badge" + (s.enabled ? " ok" : ""), text: s.enabled ? "enabled" : "disabled" });
      const item = h("div", { class: "item" + (s.enabled ? "" : " disabled") },
        h("div", { class: "item-head" }, h("span", { class: "item-title", text: s.name }), badge, tg),
        s.description ? h("div", { class: "item-sub", text: s.description }) : null);
      tg.addEventListener("click", async () => {
        const want = !s.enabled;
        tg.disabled = true;
        try {
          await api("POST", "schedules/" + encodeURIComponent(s.id) + "/enabled", { enabled: want });
          s.enabled = want;
          toast((want ? "Enabled " : "Disabled ") + s.name);
        } catch (e) { toast(e.message, true); }
        tg.disabled = false;
        tg.setAttribute("aria-checked", String(!!s.enabled));
        badge.textContent = s.enabled ? "enabled" : "disabled";
        badge.classList.toggle("ok", !!s.enabled);
        item.classList.toggle("disabled", !s.enabled);
      });
      list.appendChild(item);
    }
  }

  // ---------------------------------------------------------------- schedule sets
  function fmtWhen(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }

  function renderSetBanner(active) {
    const b = $("#set-banner");
    b.classList.toggle("hidden", !active);
    const key = JSON.stringify(active || null);
    if (!active) {
      if (state.setKey !== key) { state.setKey = key; if (state.tab === "schedules") loadScheduleSets(); }
      return;
    }
    const t = $("#set-banner-text");
    clear(t);
    t.appendChild(document.createTextNode(active.set_name + " ACTIVE"));
    const n = active.count;
    const detail = n + " schedule" + (n === 1 ? "" : "s") + " changed" + (active.activated_at ? " since " + fmtWhen(active.activated_at) : "") +
      (active.warnings.length ? " · " + active.warnings.length + " problem" + (active.warnings.length === 1 ? "" : "s") : "");
    t.appendChild(h("small", { text: detail }));
    if (state.setKey !== key) { state.setKey = key; if (state.tab === "schedules") loadScheduleSets(); }
  }

  async function loadScheduleSets() {
    try { state.sets = await api("GET", "schedule-sets"); renderScheduleSets(); }
    catch (e) { toast("Could not load schedule sets: " + e.message, true); }
  }

  function nameList(refs) {
    return refs.map((r) => r.name || r.id);
  }

  function renderScheduleSets() {
    const list = $("#set-list");
    clear(list);
    const { sets, active } = state.sets;
    $("#sets-empty").classList.toggle("hidden", sets.length > 0);
    for (const s of sets) {
      const isActive = !!s.active;
      const missing = [...s.disable, ...s.enable].filter((r) => r.missing);
      const item = h("div", { class: "item" + (isActive ? " active" : "") },
        h("div", { class: "item-head" },
          h("span", { class: "item-title", text: s.name }),
          isActive ? h("span", { class: "badge active", text: "active" }) : null),
        s.disable.length ? h("div", { class: "item-sub", text: "Turns off: " + nameList(s.disable).join(", ") }) : null,
        s.enable.length ? h("div", { class: "item-sub", text: "Turns on: " + nameList(s.enable).join(", ") }) : null,
        missing.length ? h("div", { class: "item-sub missing", text: "Not found in the Lutron system now: " + nameList(missing).join(", ") }) : null,
        h("div", { class: "item-actions" },
          isActive
            ? h("button", { class: "btn primary", text: "Restore schedules", onclick: restoreSet })
            : h("button", { class: "btn primary", text: "Activate", disabled: !!active,
                title: active ? "Restore \"" + active.set_name + "\" first" : "", onclick: () => activateSet(s) }),
          h("button", { class: "btn", text: "Edit", onclick: () => openSetEditor(s) }),
          h("button", { class: "btn danger", text: "Delete", disabled: isActive,
            title: isActive ? "Restore the schedules before deleting" : "", onclick: () => deleteSet(s) })));
      list.appendChild(item);
    }
    if (active && active.warnings.length && $("#set-result").classList.contains("hidden")) {
      showSetResult("bad", active.set_name + " needs attention", active.warnings);
    }
  }

  function showSetResult(kind, title, lines) {
    const box = $("#set-result");
    clear(box);
    box.className = "card result " + kind;
    box.appendChild(h("h3", { text: title }));
    if (lines.length) box.appendChild(h("ul", null, lines.map((l) => h("li", { text: l }))));
    box.appendChild(h("button", { class: "btn ghost", text: "Dismiss", onclick: () => box.classList.add("hidden") }));
    box.classList.remove("hidden");
  }

  function resultLines(res) {
    return [
      ...res.failures.map((f) => "Could not change " + f),
      ...res.missing.map((m) => "Skipped: " + m),
      ...res.unconfirmed.map((n) => "No confirmation from the system for \"" + n + "\" - check the list below"),
    ];
  }

  async function afterSetChange() {
    await Promise.all([loadScheduleSets(), loadInventory(), loadStatus()]);
  }

  async function activateSet(s) {
    const lines = ["Activate \"" + s.name + "\"?", ""];
    if (s.disable.length) lines.push("These schedules will be turned OFF:", ...nameList(s.disable).map((n) => "  • " + n), "");
    if (s.enable.length) lines.push("These schedules will be turned ON:", ...nameList(s.enable).map((n) => "  • " + n), "");
    lines.push("Their current on/off state is remembered. Press \"Restore schedules\" afterwards to put it back exactly.");
    if (!confirm(lines.join("\n"))) return;
    $("#set-result").classList.add("hidden");
    try {
      const res = await api("POST", "schedule-sets/" + encodeURIComponent(s.id) + "/activate");
      const probs = resultLines(res);
      if (probs.length) {
        showSetResult("bad", s.name + " is active, but not everything went through", probs);
        toast(s.name + " active with problems", true);
      } else {
        const n = res.changed.length;
        showSetResult("ok", s.name + " is active", [
          n + " schedule" + (n === 1 ? "" : "s") + " changed" +
            (res.unchanged.length ? ", " + res.unchanged.length + " already in the wanted state" : "") + ".",
        ]);
        toast(s.name + " active");
      }
    } catch (e) {
      showSetResult("bad", "Could not activate " + s.name, [e.message]);
      toast(e.message, true);
    }
    afterSetChange();
  }

  async function restoreSet() {
    const name = state.sets.active ? state.sets.active.set_name : "schedules";
    $("#set-result").classList.add("hidden");
    try {
      const res = await api("POST", "schedule-sets/active/deactivate");
      const probs = resultLines(res);
      if (res.active) {
        showSetResult("bad", "Some schedules are still not restored", [...probs, "Press \"Restore schedules\" again to retry."]);
        toast("Restore incomplete", true);
      } else if (probs.length) {
        showSetResult("bad", name + " restored, with notes", probs);
        toast("Restored with notes");
      } else {
        const n = res.restored.length;
        showSetResult("ok", name + " restored", [n + " schedule" + (n === 1 ? "" : "s") + " put back to how " + (n === 1 ? "it was" : "they were") + "."]);
        toast("Schedules restored");
      }
    } catch (e) {
      showSetResult("bad", "Could not restore", [e.message, "The remembered states are kept. Try again once the Lutron system is reachable."]);
      toast(e.message, true);
    }
    afterSetChange();
  }

  async function deleteSet(s) {
    if (!confirm("Delete the schedule set \"" + s.name + "\"? The schedules themselves are not touched.")) return;
    try {
      await api("DELETE", "schedule-sets/" + encodeURIComponent(s.id));
      if (state.setEditor && state.setEditor.id === s.id) closeSetEditor();
      toast("Deleted " + s.name);
      loadScheduleSets();
    } catch (e) { toast(e.message, true); }
  }

  function openSetEditor(set) {
    const picks = new Map(), missing = new Map();
    if (set) {
      for (const [list, mode] of [[set.disable, "disable"], [set.enable, "enable"]]) {
        for (const r of list) { picks.set(r.id, mode); if (r.missing) missing.set(r.id, r.name); }
      }
    }
    state.setEditor = { id: set ? set.id : null, picks, missing };
    $("#set-editor-title").textContent = set ? "Edit schedule set" : "New schedule set";
    $("#set-name").value = set ? set.name : "";
    renderSetRows();
    $("#set-editor").classList.remove("hidden");
    $("#set-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeSetEditor() { state.setEditor = null; $("#set-editor").classList.add("hidden"); }

  function renderSetRows() {
    const root = $("#set-rows");
    clear(root);
    const ed = state.setEditor;
    const rows = state.inv.schedules.map((s) => ({ id: s.id, name: s.name, sub: s.description, missing: false }));
    for (const [id, name] of ed.missing) rows.push({ id, name: name || id, sub: "Not found in the Lutron system now", missing: true });
    for (const r of rows) {
      const sel = h("select", { "aria-label": "What this set does to " + r.name },
        h("option", { value: "", text: "Leave alone" }),
        h("option", { value: "disable", text: "Turn off while active" }),
        h("option", { value: "enable", text: "Turn on while active" }));
      const row = h("div", { class: "set-row" },
        h("div", { class: "nm" }, r.name, r.sub ? h("small", { class: r.missing ? "missing" : "", text: r.sub }) : null),
        sel);
      const paint = () => {
        const v = ed.picks.get(r.id) || "";
        sel.value = v;
        row.classList.toggle("pick-disable", v === "disable");
        row.classList.toggle("pick-enable", v === "enable");
      };
      sel.addEventListener("change", () => { if (sel.value) ed.picks.set(r.id, sel.value); else ed.picks.delete(r.id); paint(); });
      paint();
      root.appendChild(row);
    }
    if (!rows.length) root.appendChild(h("p", { class: "empty", text: "This system reports no schedules to pick from." }));
  }

  async function saveSet() {
    const ed = state.setEditor;
    if (!ed) return;
    const name = $("#set-name").value.trim();
    if (!name) { toast("Give the set a name", true); $("#set-name").focus(); return; }
    const pick = (mode) => Array.from(ed.picks).filter(([, m]) => m === mode).map(([id]) => id);
    const body = { name, disable: pick("disable"), enable: pick("enable") };
    if (!body.disable.length && !body.enable.length) { toast("Pick at least one schedule", true); return; }
    try {
      if (ed.id) await api("PUT", "schedule-sets/" + encodeURIComponent(ed.id), body);
      else await api("POST", "schedule-sets", body);
      toast("Saved " + name);
      closeSetEditor();
      loadScheduleSets();
    } catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- DMX
  async function enterDmx() {
    buildMonitorGrid();
    if (!state.dmx) await loadDmx();
    startMonitor();
  }

  async function loadDmx() {
    try {
      state.dmx = await api("GET", "settings/dmx");
      setDmxMapped(state.dmx.mappings);
      fillDmxForm();
      loadSerialPorts();
    } catch (e) { toast("Could not load DMX settings: " + e.message, true); }
  }

  function fillDmxForm() {
    const d = state.dmx;
    $("#dmx-enabled").checked = !!d.enabled;
    $("#dmx-source").value = d.source;
    $("#dmx-universe").value = d.universe;
    $("#dmx-bind").value = d.bind_ip || "";
    $("#dmx-rate").value = d.max_rate_hz;
    $("#dmx-total-rate").value = d.max_commands_per_s ?? 20;
    $("#dmx-protect").checked = d.protect !== false;
    $("#dmx-latency-limit").value = d.latency_limit_ms ?? 500;
    $("#dmx-max-inflight").value = d.max_inflight ?? 40;
    updateSourceVisibility();
    renderMappings();
  }

  function updateSourceVisibility() {
    const usb = $("#dmx-source").value === "usb";
    $$(".usb-only").forEach((e) => e.classList.toggle("hidden", !usb));
    $$(".net-only").forEach((e) => e.classList.toggle("hidden", usb));
  }

  async function loadSerialPorts() {
    const sel = $("#dmx-serial");
    let res = { ports: [] };
    try { res = await api("GET", "dmx/serial-ports"); } catch (e) { res = { ports: [], error: e.message }; }
    state.serialPorts = res.ports || [];
    const current = (state.dmx && state.dmx.serial_port) || "";
    clear(sel);
    sel.appendChild(h("option", { value: "", text: state.serialPorts.length ? "Choose a port…" : "No serial ports found" }));
    for (const p of state.serialPorts) {
      const extra = p.can_receive === false ? " (cannot receive DMX)" : "";
      sel.appendChild(h("option", { value: p.device, text: p.device + (p.description ? " - " + p.description : "") + extra, title: p.note || "" }));
    }
    if (current && !state.serialPorts.some((p) => p.device === current)) sel.appendChild(h("option", { value: current, text: current + " (not detected)" }));
    sel.value = current;
    $("#serial-note").textContent = res.error ? res.error :
      "Windows ports look like COM3; macOS ports look like /dev/tty.usbserial-XXXX. Plug in the adapter, then press Rescan.";
  }

  function mappingEnd(m) { return Number(m.address) + (m.params || []).length - 1; }

  function renderMappings() {
    const body = $("#map-body");
    clear(body);
    const maps = state.dmx.mappings;
    $("#map-empty").classList.toggle("hidden", maps.length > 0);
    // channel usage for conflict detection
    const use = new Map();
    maps.forEach((m, i) => { if (!m.enabled) return; for (let c = m.address; c <= mappingEnd(m); c++) use.set(c, (use.get(c) || []).concat(i)); });
    const presets = ["intensity", "on_off", "intensity,on_off"];
    maps.forEach((m, i) => {
      const zoneSel = h("select", { "aria-label": "Zone" });
      for (const g of groupByArea().filter((g) => g.zones.length)) {
        const og = h("optgroup", { label: g.name });
        for (const z of g.zones) og.appendChild(h("option", { value: z.id, text: z.name }));
        zoneSel.appendChild(og);
      }
      if (!state.zonesById.has(m.zone_id)) zoneSel.appendChild(h("option", { value: m.zone_id, text: m.zone_id + " (unknown)" }));
      zoneSel.value = m.zone_id;
      zoneSel.addEventListener("change", () => { m.zone_id = zoneSel.value; });

      const addr = h("input", { type: "number", min: 1, max: 512, step: 1, value: m.address, "aria-label": "Start address" });
      addr.addEventListener("change", () => { m.address = Math.max(1, Math.min(512, parseInt(addr.value, 10) || 1)); renderMappings(); });

      const pSel = h("select", { "aria-label": "Parameters" });
      const key = (m.params || []).join(",");
      for (const p of presets) pSel.appendChild(h("option", { value: p, text: p.replace(",", " + ") }));
      if (!presets.includes(key)) pSel.appendChild(h("option", { value: key, text: key }));
      pSel.value = key;
      pSel.addEventListener("change", () => { m.params = pSel.value.split(","); renderMappings(); });

      const en = h("input", { type: "checkbox", checked: !!m.enabled, "aria-label": "Enabled" });
      en.addEventListener("change", () => { m.enabled = en.checked; renderMappings(); });

      const end = mappingEnd(m);
      let conflict = end > 512;
      for (let c = m.address; c <= end; c++) if ((use.get(c) || []).length > 1) conflict = true;
      const chText = (end > m.address ? m.address + "–" + end : String(m.address)) + (conflict ? " ⚠ overlap" : "");
      body.appendChild(h("tr", { class: conflict && m.enabled ? "conflict" : "" },
        h("td", null, en), h("td", null, zoneSel), h("td", null, addr), h("td", null, pSel),
        h("td", { class: "ch", text: chText, title: conflict ? "Channels overlap another mapping or run past 512" : "" }),
        h("td", null, h("button", { class: "btn ghost", text: "Remove", "aria-label": "Remove mapping", onclick: () => { maps.splice(i, 1); renderMappings(); } }))));
    });
  }

  function addMappingRow() {
    const maps = state.dmx.mappings;
    const next = maps.reduce((mx, m) => Math.max(mx, mappingEnd(m)), 0) + 1;
    const mapped = new Set(maps.map((m) => m.zone_id));
    const z = state.inv.zones.find((z) => !mapped.has(z.id)) || state.inv.zones[0];
    if (!z) { toast("No zones available", true); return; }
    maps.push({ zone_id: z.id, address: Math.min(next, 512), params: ["intensity"], enabled: true });
    renderMappings();
  }

  async function autoAssign(replace) {
    const params = $("#auto-params").value.split(",");
    let start = Math.max(1, Math.min(512, parseInt($("#auto-start").value, 10) || 1));
    const maps = state.dmx.mappings;
    let zoneIds;
    if (replace) {
      if (maps.length && !confirm("Replace all existing mappings?")) return;
      zoneIds = groupByArea().flatMap((g) => g.zones.map((z) => z.id));
    } else {
      const mapped = new Set(maps.map((m) => m.zone_id));
      zoneIds = groupByArea().flatMap((g) => g.zones.map((z) => z.id)).filter((id) => !mapped.has(id));
      const nextFree = maps.reduce((mx, m) => Math.max(mx, mappingEnd(m)), 0) + 1;
      start = Math.max(start, nextFree);
    }
    if (!zoneIds.length) { toast("Every zone is already mapped"); return; }
    try {
      const res = await api("POST", "dmx/auto-assign", { zone_ids: zoneIds, params, start });
      const got = res.mappings || [];
      state.dmx.mappings = replace ? got : maps.concat(got);
      renderMappings();
      const skipped = zoneIds.length - got.length;
      toast("Assigned " + got.length + " zone(s)" + (skipped > 0 ? ", " + skipped + " did not fit in 512 channels" : "") + " - press Save & apply");
    } catch (e) { toast(e.message, true); }
  }

  async function saveDmx() {
    const d = state.dmx;
    const body = {
      enabled: $("#dmx-enabled").checked,
      source: $("#dmx-source").value,
      universe: parseInt($("#dmx-universe").value, 10) || 0,
      bind_ip: $("#dmx-bind").value.trim() || "0.0.0.0",
      serial_port: $("#dmx-serial").value || "",
      max_rate_hz: Number($("#dmx-rate").value) || 5,
      max_commands_per_s: Math.max(0, Number($("#dmx-total-rate").value) || 0),
      protect: $("#dmx-protect").checked,
      latency_limit_ms: Math.min(10000, Math.max(50, Number($("#dmx-latency-limit").value) || 500)),
      max_inflight: Math.max(0, Math.round(Number($("#dmx-max-inflight").value) || 0)),
      mappings: d.mappings.map((m) => ({ zone_id: m.zone_id, address: Number(m.address), params: m.params, enabled: !!m.enabled })),
    };
    if (body.source === "usb" && body.enabled && !body.serial_port) { toast("Choose a serial port for the USB adapter", true); return; }
    const btn = $("#btn-dmx-save");
    btn.disabled = true;
    try {
      const res = await api("PUT", "settings/dmx", body);
      state.dmx = res;
      setDmxMapped(res.mappings);
      fillDmxForm();
      if (res.warning) toast("Saved, but: " + res.warning, true); else toast("DMX settings saved");
      loadStatus();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
  }

  function buildMonitorGrid() {
    if (state.monitorCells.length) return;
    const grid = $("#monitor-grid");
    for (let ch = 1; ch <= 512; ch++) {
      const fill = h("div", { class: "fill" });
      const num = h("span", { text: String(ch) });
      const cell = h("div", { class: "cell", title: "Channel " + ch }, fill, num);
      cell.addEventListener("click", () => {
        if (state.monitorSelected) state.monitorSelected.classList.remove("selected");
        state.monitorSelected = cell; cell.classList.add("selected");
        describeCell(ch);
      });
      grid.appendChild(cell);
      state.monitorCells.push({ cell, fill, num, value: -1, info: null });
    }
    const mo = $("#monitor-mapped-only");
    mo.checked = !!store.get("mappedOnly", false);
    grid.classList.toggle("mapped-only", mo.checked);
    mo.addEventListener("change", () => { grid.classList.toggle("mapped-only", mo.checked); store.set("mappedOnly", mo.checked); });
  }

  function describeCell(ch) {
    const c = state.monitorCells[ch - 1];
    const v = Math.max(0, c.value);
    const txt = "Channel " + ch + ": value " + v + " (" + Math.round((v / 255) * 100) + "%)" +
      (c.info ? " → " + zoneName(c.info.zone_id) + " · " + c.info.param : " → not mapped");
    $("#monitor-detail").textContent = txt;
  }

  async function pollMonitor() {
    if (document.hidden) return;
    let res;
    try { res = await api("GET", "dmx/monitor"); } catch (e) { $("#monitor-state").textContent = "monitor unavailable"; return; }
    const st = res.status || {};
    let stateTxt = "DMX input is not running";
    if (res.running) {
      stateTxt = st.packets ? "receiving" + (st.fps != null ? " · " + st.fps + " fps" : "") + (st.packets != null ? " · " + st.packets + " packets" : "") : "running, waiting for data";
      if (st.error) stateTxt += " · " + st.error;
    }
    $("#monitor-state").textContent = stateTxt;
    const warn = [].concat(st.warnings || [], st.note ? [st.note] : []).join(" ");
    $("#dmx-warnings").textContent = warn;
    $("#dmx-warnings").classList.toggle("hidden", !warn);
    const map = res.channel_map || {};
    for (let i = 0; i < 512; i++) {
      const c = state.monitorCells[i];
      const v = res.frame[i] || 0;
      const info = map[String(i + 1)] || null;
      if (v !== c.value) {
        c.value = v;
        c.fill.style.height = ((v / 255) * 100).toFixed(1) + "%";
      }
      const key = info ? info.zone_id + "|" + info.param : "";
      if (key !== c.key) {
        c.key = key; c.info = info;
        c.cell.classList.toggle("mapped", !!info);
        c.cell.classList.toggle("p-on_off", !!info && info.param === "on_off");
        c.cell.title = "Channel " + (i + 1) + (info ? " - " + zoneName(info.zone_id) + " (" + info.param + ")" : "");
      }
    }
    if (state.monitorSelected) describeCell(state.monitorCells.findIndex((c) => c.cell === state.monitorSelected) + 1);
  }

  function startMonitor() { stopMonitor(); pollMonitor(); state.monitorTimer = setInterval(pollMonitor, 500); }
  function stopMonitor() { clearInterval(state.monitorTimer); state.monitorTimer = null; }

  // ---------------------------------------------------------------- settings
  async function loadDriverSettings() {
    try {
      const d = await api("GET", "settings/driver");
      $("#drv-kind").value = d.kind;
      $("#drv-host").value = d.host || "";
      state.hostname = d.hostname || "";
      clear($("#discover-results")); $("#discover-state").textContent = "";
      $("#drv-port").value = d.port == null ? "" : d.port;
      $("#drv-user").value = d.username || "";
      $("#drv-pass").value = d.password || "";
      $("#drv-report").value = d.integration_report || "";
      $("#drv-certdir").value = d.leap_cert_dir || "";
      updateKindVisibility();
    } catch (e) { toast("Could not load settings: " + e.message, true); }
  }

  function updateKindVisibility() {
    const kind = $("#drv-kind").value;
    $$(".kind-help").forEach((e) => e.classList.toggle("hidden", e.dataset.kind !== kind));
    $$(".for-ra2, .for-leap").forEach((e) => {
      const show = (kind === "ra2" && e.classList.contains("for-ra2")) || (kind === "leap" && e.classList.contains("for-leap"));
      e.classList.toggle("hidden", !show);
    });
    $("#drv-host").placeholder = kind === "ra2" ? "Main Repeater IP, e.g. 192.168.1.50" : "Processor / bridge IP, e.g. 192.168.1.60";
  }

  async function saveDriver() {
    const kind = $("#drv-kind").value;
    const portRaw = $("#drv-port").value.trim();
    const body = {
      kind,
      host: $("#drv-host").value.trim(),
      hostname: state.hostname || "",
      username: $("#drv-user").value,
      password: $("#drv-pass").value,
      port: portRaw === "" ? null : parseInt(portRaw, 10),
      integration_report: $("#drv-report").value.trim(),
      leap_cert_dir: $("#drv-certdir").value.trim(),
    };
    if (kind !== "simulator" && !body.host) { toast("Enter the host / IP address", true); $("#drv-host").focus(); return; }
    const btn = $("#btn-drv-save");
    btn.disabled = true; btn.textContent = "Connecting…";
    try {
      const res = await api("PUT", "settings/driver", body);
      if (res.status) renderStatus(res.status);
      toast(res.status && res.status.connected ? "Connected" : "Saved, but not connected - see the error above", !(res.status && res.status.connected));
      state.tiles.clear();
      await loadInventory();
      state.dmx = null; // zone list may have changed
      loadDriverSettings();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false; btn.textContent = "Save & reconnect";
  }

  // ---------------------------------------------------------------- discovery
  state.hostname = "";

  async function discoverControllers() {
    const btn = $("#btn-discover"), st = $("#discover-state"), box = $("#discover-results");
    btn.disabled = true; clear(box);
    st.textContent = "Looking for Lutron controllers on your network…";
    try {
      const res = await api("POST", "discover", { scan: $("#discover-deep").checked ? true : "auto" });
      const devices = res.devices || [];
      if (!devices.length) {
        st.textContent = "Nothing found. Make sure this computer is on the same network as the controller, or type its IP address below.";
        btn.disabled = false;
        return;
      }
      st.textContent = devices.length === 1 ? "Found 1 controller:" : "Found " + devices.length + " controllers:";
      for (const d of devices) {
        const kindName = d.kind === "ra2" ? "RadioRA 2 (telnet)" : "LEAP (RA3 / RA2 Select / Caseta)";
        box.appendChild(h("div", { class: "discover-row" },
          h("span", { class: "discover-main" },
            h("b", { text: d.host }),
            " ", h("span", { class: "muted", text: [d.system, kindName, d.name, d.mac].filter(Boolean).join(" · ") })),
          h("button", { class: "btn", text: "Use this one", onclick: () => useDiscovered(d) })));
      }
    } catch (e) {
      st.textContent = "Search failed: " + e.message;
    }
    btn.disabled = false;
  }

  function useDiscovered(d) {
    if (d.kind === "ra2" || d.kind === "leap") $("#drv-kind").value = d.kind;
    $("#drv-host").value = d.host;
    $("#drv-port").value = d.kind === "ra2" && d.port !== 23 ? d.port : "";
    state.hostname = d.name || "";
    updateKindVisibility();
    toast(d.simulated ? "Demo result filled in" : "Address filled in - now " + (d.kind === "leap" ? "pair, then " : "") + "save");
  }

  async function pairLeap(trust) {
    const host = $("#drv-host").value.trim();
    if (!host) { toast("Enter the processor / bridge IP address first", true); $("#drv-host").focus(); return; }
    const btn = $("#btn-pair"), st = $("#pair-state");
    btn.disabled = true;
    st.textContent = "Now press the pairing button on the back of the processor/bridge…";
    try {
      const res = await api("POST", "pair/leap", { host, trust_presented_cert: trust === true });
      $("#drv-certdir").value = res.cert_dir || "";
      st.textContent = "Paired. Press “Save & reconnect”.";
      toast("Pairing successful");
    } catch (e) {
      if (e.status === 409 && !trust && confirm(e.message + "\n\nTrust this certificate for this one pairing?")) {
        btn.disabled = false;
        return pairLeap(true);
      }
      st.textContent = "Pairing failed: " + e.message;
      toast(e.message, true);
    }
    btn.disabled = false;
  }

  // ---------------------------------------------------------------- updates
  const UPDATING = ["downloading", "verifying", "staging", "testing"];

  function renderUpdate(u) {
    state.update = u || null;
    renderUpdateFacts();
    const b = $("#update-banner");
    if (!u || u.disabled) { b.classList.add("hidden"); state.updateKey = null; return; }
    const lr = u.last_result;
    const resultSeen = lr && store.get("update-result-seen", "") === lr.at;
    const errorHidden = u.phase === "error" && state.hiddenUpdateError === u.message;
    let mode = null;
    if (UPDATING.includes(u.phase)) mode = "busy";
    else if (u.phase === "restarting") mode = "restarting";
    else if (u.phase === "installed") mode = "installed";
    else if (u.phase === "error" && !errorHidden) mode = "error";
    else if (u.available && !u.dismissed) mode = "available";
    else if (lr && !resultSeen) mode = lr.ok ? "done" : "failed";
    if (!mode) { b.classList.add("hidden"); state.updateKey = null; return; }

    const key = JSON.stringify([mode, u.latest, u.message, u.install && u.install.can_auto, lr && lr.at]);
    if (state.updateKey !== key) {
      state.updateKey = key;
      const text = $("#update-banner-text");
      clear(text);
      const sub = (msg) => text.appendChild(h("small", { text: msg }));
      const notes = $("#update-notes"), now = $("#btn-update-now"), dismiss = $("#btn-update-dismiss");
      notes.classList.add("hidden"); now.classList.add("hidden"); dismiss.classList.add("hidden");
      now.disabled = false;
      b.classList.toggle("bad", mode === "error" || mode === "failed");
      if (mode === "available") {
        text.appendChild(document.createTextNode("Version " + u.latest + " is available"));
        sub("You have " + u.current + "." + (u.install && !u.install.can_auto && u.install.manual ? " " + u.install.manual : ""));
        if (u.notes_url) { notes.href = u.notes_url; notes.classList.remove("hidden"); }
        if (u.install && u.install.can_auto) now.classList.remove("hidden");
        dismiss.classList.remove("hidden");
      } else if (mode === "busy") {
        text.appendChild(document.createTextNode(u.message || "Updating…"));
        text.appendChild(h("div", { class: "update-bar" }, h("i", { id: "update-bar-fill" })));
      } else if (mode === "restarting") {
        text.appendChild(document.createTextNode(u.message || "Installing the update…"));
        sub("This page reloads by itself when the app is back (about 15 seconds).");
      } else if (mode === "installed") {
        text.appendChild(document.createTextNode(u.message));
      } else if (mode === "error") {
        text.appendChild(document.createTextNode("The update did not finish"));
        sub(u.message + " Nothing was changed.");
        if (u.install && u.install.can_auto && u.available) { now.textContent = "Try again"; now.classList.remove("hidden"); }
        dismiss.classList.remove("hidden");
      } else if (mode === "done") {
        text.appendChild(document.createTextNode("Updated to version " + lr.to_version));
        dismiss.classList.remove("hidden");
      } else if (mode === "failed") {
        text.appendChild(document.createTextNode("The update to " + lr.to_version + " did not complete"));
        sub((lr.error || "unknown error") + ". The previous version (" + lr.from_version + ") was restored.");
        dismiss.classList.remove("hidden");
      }
      if (mode === "available") now.textContent = "Update now";
      state.updateMode = mode;
    }
    const fill = $("#update-bar-fill");
    if (fill) fill.style.width = Math.round(100 * (u.progress || 0)) + "%";
    b.classList.remove("hidden");
  }

  function renderUpdateFacts() {
    const dl = $("#update-facts");
    if (!dl || state.tab !== "settings") return;
    const u = state.update;
    clear(dl);
    if (!u || u.disabled) return;
    const row = (k, v, cls) => { dl.appendChild(h("dt", { text: k })); dl.appendChild(h("dd", { class: cls || "", text: v })); };
    row("Installed version", u.current);
    row("Installed as", (u.install && u.install.label) || "-");
    row("Latest release", u.available ? u.latest + " (newer)" : "none newer than yours", u.available ? "warn" : "ok");
    row("Last checked", u.checked_at ? new Date(u.checked_at).toLocaleString() : "not yet");
    if (u.check_error) row("Last check", u.check_error, "warn");
  }

  async function loadUpdateSettings() {
    try {
      const c = await api("GET", "settings/updates");
      $("#upd-check").checked = !!c.check;
      $("#upd-token").value = c.token || "";
    } catch (e) { /* no updater in this mode: leave the card as is */ }
  }

  async function saveUpdateSettings() {
    try {
      const res = await api("PUT", "settings/updates", { check: $("#upd-check").checked, token: $("#upd-token").value });
      $("#upd-token").value = res.token || "";
      if (res.update) renderUpdate(res.update);
      toast("Update settings saved");
    } catch (e) { toast(e.message, true); }
  }

  // ---------------------------------------------------------------- export / import of settings
  const xfer = { doc: null, preview: null, fileName: "" };

  function updateExportLink() {
    const creds = $("#xfer-creds").checked;
    $("#btn-export").setAttribute("href", "api/config/export" + (creds ? "?credentials=true" : ""));
    $("#xfer-creds-warn").classList.toggle("hidden", !creds);
  }

  function closeImportPanel() {
    xfer.doc = xfer.preview = null;
    const p = $("#import-panel");
    clear(p);
    p.classList.add("hidden");
  }

  function showImportMessage(kind, title, lines) {
    const p = $("#import-panel");
    clear(p);
    p.classList.remove("hidden");
    p.appendChild(h("div", { class: "xfer-note " + kind, role: kind === "bad" ? "alert" : "status" },
      h("b", { text: title }), ...lines.map((l) => h("div", { text: l }))));
    p.appendChild(h("div", { class: "toolbar" }, h("button", { class: "btn", onclick: closeImportPanel, text: "Close" })));
  }

  async function pickImportFile(ev) {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = ""; // so choosing the same file again works
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { showImportMessage("bad", "That file is too big to be a settings export.", []); return; }
    let doc;
    try { doc = JSON.parse(await file.text()); }
    catch (e) { showImportMessage("bad", "Cannot read " + file.name, ["It is not valid JSON."]); return; }
    try {
      xfer.preview = await api("POST", "config/import/preview", { document: doc });
      xfer.doc = doc;
      xfer.fileName = file.name;
      renderImportPanel();
    } catch (e) { showImportMessage("bad", "Cannot import " + file.name, [e.message]); }
  }

  function renderImportPanel() {
    const pv = xfer.preview, f = pv.file;
    const p = $("#import-panel");
    clear(p);
    p.classList.remove("hidden");
    const when = f.created ? " on " + new Date(f.created).toLocaleString() : "";
    p.appendChild(h("h4", { text: "Import " + xfer.fileName }));
    p.appendChild(h("p", { class: "hint", text:
      "Made by app version " + (f.app_version || "unknown") + when + ". Integration login, update token and pairing certificates: " +
      (f.includes_credentials ? "included in the file." : "not in the file (the ones this app has now are kept).") }));
    pv.warnings.filter((w) => !w.section).forEach((w) => p.appendChild(h("div", { class: "xfer-note warn", text: w.text })));

    const boxes = [];
    const importBtn = h("button", { class: "btn primary", text: "Import selected" });
    const refresh = () => {
      boxes.forEach(({ box, row }) => row.classList.toggle("off", !box.checked));
      importBtn.disabled = !boxes.some(({ box }) => box.checked);
    };
    pv.sections.forEach((s) => {
      const box = h("input", { type: "checkbox", dataset: { section: s.id }, checked: pv.selected.includes(s.id) && !s.blocked, disabled: !!s.blocked, onchange: refresh });
      const row = h("div", { class: "import-sec" },
        h("label", { class: "check" }, box, h("b", { text: s.label }), h("span", { class: "muted", text: s.changed ? "" : "no changes" })));
      const items = [...s.changes.map((c) => h("li", { text: c })),
        ...pv.warnings.filter((w) => w.section === s.id).map((w) => h("li", { class: "warn", text: w.text }))];
      if (items.length) row.appendChild(h("ul", null, ...items));
      if (s.blocked) row.appendChild(h("div", { class: "blocked", text: s.blocked }));
      boxes.push({ box, row });
      p.appendChild(row);
    });

    let restoreBox = null;
    if (pv.can_restore_active_set) {
      restoreBox = h("input", { type: "checkbox", id: "import-restore-active" });
      p.appendChild(h("div", { class: "import-sec" }, h("label", { class: "check" }, restoreBox,
        h("span", { text: "Also bring back the saved schedule states of “" + f.active_set + "”, which was active when the file was made" }))));
    }
    p.appendChild(h("p", { class: "hint", text:
      "Each ticked section is replaced as a whole: anything in it that is not in the file is removed. The current settings are saved to the app’s backups folder first, and that file can be imported to undo this. Changing the Lutron connection reconnects the app." }));
    importBtn.addEventListener("click", () => runImport(importBtn, restoreBox));
    p.appendChild(h("div", { class: "toolbar" }, importBtn, h("button", { class: "btn ghost", onclick: closeImportPanel, text: "Cancel" })));
    refresh();
  }

  async function runImport(btn, restoreBox) {
    const sections = $$("#import-panel input[data-section]").filter((i) => i.checked).map((i) => i.dataset.section);
    if (!sections.length) return;
    btn.disabled = true;
    try {
      const res = await api("POST", "config/import", {
        document: xfer.doc, sections, restore_active_set: !!(restoreBox && restoreBox.checked), confirm: true });
      const names = new Map(xfer.preview.sections.map((s) => [s.id, s.label]));
      const lines = ["Imported: " + res.applied.map((s) => names.get(s) || s).join(", ") + ".",
        "The previous settings were saved to " + res.backup + " (import that file to undo this).", ...res.warnings];
      if (res.restart_required) lines.push("The web address or port changes the next time the app starts.");
      showImportMessage("ok", "Settings imported", lines);
      xfer.doc = xfer.preview = null;
      state.dmx = null; // the DMX tab re-reads its settings
      loadDriverSettings(); loadUpdateSettings(); renderUpdateFacts(); loadInventory(); loadDmxMapped(); loadStatus();
    } catch (e) {
      btn.disabled = false;
      toast(e.message, true);
    }
  }

  async function checkUpdateNow() {
    const btn = $("#btn-update-check"), st = $("#update-check-state");
    btn.disabled = true; st.textContent = "Asking GitHub…";
    try {
      const u = await api("POST", "update/check");
      renderUpdate(u);
      st.textContent = u.check_error || (u.available ? "Version " + u.latest + " is available." : "You are up to date.");
    } catch (e) { st.textContent = e.message; }
    btn.disabled = false;
  }

  async function updateNow(confirmed) {
    const u = state.update;
    if (!u || !u.latest) return;
    const btn = $("#btn-update-now");
    btn.disabled = true;
    try {
      const res = await api("POST", "update/apply", { version: u.latest, confirm: !!confirmed });
      if (res.needs_confirm) {
        const ok = confirm("Update to " + u.latest + " now?\n\n" + res.reasons.join("\n\n") +
          "\n\nLutron To DMX Bridge will close and reopen. Your settings and scenes are kept.");
        btn.disabled = false;
        if (ok) updateNow(true);
        return;
      }
      if (res.update) renderUpdate(res.update);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  }

  async function dismissUpdate() {
    const u = state.update;
    if (!u) return;
    if (state.updateMode === "available") {
      try { renderUpdate(await api("POST", "update/dismiss", { version: u.latest })); }
      catch (e) { toast(e.message, true); }
    } else if (state.updateMode === "error") {
      state.hiddenUpdateError = u.message; renderUpdate(u);
    } else if (u.last_result) {
      store.set("update-result-seen", u.last_result.at); renderUpdate(u);
    }
  }

  // ---------------------------------------------------------------- wiring
  function wire() {
    $$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    $("#btn-expand-all").addEventListener("click", () => { $$("details.area").forEach((d) => { d.open = true; }); });
    $("#btn-collapse-all").addEventListener("click", () => { $$("details.area").forEach((d) => { d.open = false; }); });

    $$("#status-chips .chip").forEach((c) => c.addEventListener("click", togglePop));
    $("#btn-alerts-clear").addEventListener("click", clearAlerts);
    $("#btn-queue-clear").addEventListener("click", clearQueue);
    document.addEventListener("click", (e) => { if (state.pop && !e.target.closest("#status-pop, #status-chips")) closePop(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(true); });
    trackTopbarHeight();
    setInterval(renderStatusArea, 1000);  // the Server chip must notice silence even when nothing arrives

    $("#btn-new-scene").addEventListener("click", () => openEditor(null));
    $("#btn-snapshot").addEventListener("click", snapshotAll);
    $("#btn-scene-close").addEventListener("click", closeEditor);
    $("#btn-scene-capture").addEventListener("click", editorCapture);
    $("#btn-scene-all").addEventListener("click", () => { for (const r of state.editor.rows.values()) r.setChecked && r.setChecked(true); });
    $("#btn-scene-none").addEventListener("click", () => { for (const r of state.editor.rows.values()) r.setChecked && r.setChecked(false); });
    $("#btn-scene-preview").addEventListener("click", editorPreview);
    $("#btn-scene-save").addEventListener("click", editorSave);

    $("#btn-new-set").addEventListener("click", () => openSetEditor(null));
    $("#btn-set-close").addEventListener("click", closeSetEditor);
    $("#btn-set-save").addEventListener("click", saveSet);
    $("#btn-set-restore").addEventListener("click", restoreSet);
    $("#btn-dmx-pause").addEventListener("click", toggleDmxPause);
    $("#btn-protect-resume").addEventListener("click", toggleDmxPause);
    $("#btn-set-details").addEventListener("click", () => showTab("schedules"));

    $("#dmx-source").addEventListener("change", updateSourceVisibility);
    $("#btn-serial-refresh").addEventListener("click", (e) => { e.preventDefault(); loadSerialPorts(); });
    $("#btn-map-add").addEventListener("click", addMappingRow);
    $("#btn-auto-unmapped").addEventListener("click", () => autoAssign(false));
    $("#btn-auto-all").addEventListener("click", () => autoAssign(true));
    $("#btn-dmx-save").addEventListener("click", saveDmx);

    $("#drv-kind").addEventListener("change", updateKindVisibility);
    $("#btn-drv-save").addEventListener("click", saveDriver);
    $("#btn-update-now").addEventListener("click", () => updateNow(false));
    $("#btn-update-dismiss").addEventListener("click", dismissUpdate);
    $("#btn-update-check").addEventListener("click", checkUpdateNow);
    $("#btn-upd-save").addEventListener("click", saveUpdateSettings);
    $("#xfer-creds").addEventListener("change", updateExportLink);
    $("#import-file").addEventListener("change", pickImportFile);
    $("#upd-token").addEventListener("focus", (e) => { if (e.target.value === "********") e.target.select(); });
    $("#btn-pair").addEventListener("click", () => pairLeap());
    $("#btn-discover").addEventListener("click", discoverControllers);
    $("#drv-pass").addEventListener("focus", (e) => { if (e.target.value === "********") e.target.select(); });

    document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.wsOk) { loadStatus(); loadInventory(); } });
  }

  async function init() {
    wire();
    const validTabs = ["devices", "scenes", "schedules", "dmx", "status", "settings"];
    // The desktop launcher opens "#settings" on first run; otherwise restore the last tab.
    const hashTab = location.hash.slice(1);
    const tab = validTabs.includes(hashTab) ? hashTab : store.get("tab", "devices");
    await Promise.all([loadStatus(), loadInventory(), loadDmxMapped()]);
    showTab(validTabs.includes(tab) ? tab : "devices");
    connectWs();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
