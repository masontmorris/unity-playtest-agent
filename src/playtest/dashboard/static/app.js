"use strict";

// Playtest Agent dashboard. Plain DOM, no build step. Every value shown comes from the run's own files through
// the local API; a value nobody recorded is shown as unknown, never filled in.

const view = document.getElementById("view");
let dispose = () => {};

// --- helpers -----------------------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "html") el.innerHTML = value;  // only for trusted static markup
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (options.body !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

const q = (params) => new URLSearchParams(params).toString();
const unknown = (value, fallback = "unknown") => (value === null || value === undefined || value === "" ? fallback : value);

function usd(value, digits = 2) {
  return typeof value === "number" ? `$${value.toFixed(digits)}` : "unknown";
}

function ago(iso) {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 0) {
    const ahead = -s;
    return ahead < 3600 ? `in ${Math.ceil(ahead / 60)}m` : ahead < 172800 ? `in ${Math.floor(ahead / 3600)}h ${Math.floor((ahead % 3600) / 60)}m` : `in ${Math.floor(ahead / 86400)}d`;
  }
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function duration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "unknown";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
                 : `${m}:${String(s % 60).padStart(2, "0")}`;
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function statusPill(status) {
  const s = String(status || "unknown");
  const kind = s === "live" || s === "running" || s === "starting" ? "accent live"
    : s === "complete" ? "ok" : s.startsWith("failed") || s === "inconclusive" ? "bad"
    : s === "stopped" || s === "no result" ? "warn" : "";
  return h("span", { class: `pill ${kind}` }, h("span", { class: "dot" }), s);
}

function severityPill(severity) {
  const kind = { blocker: "bad", major: "warn", minor: "warn", cosmetic: "info" }[severity] || "";
  return h("span", { class: `pill ${kind}` }, unknown(severity, "severity unknown"));
}

function setNav(name) {
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
}

function fileUrl(run, path) {
  return `/api/file?${q({ run, path })}`;
}

// --- running indicator -------------------------------------------------------------------------------------

async function refreshIndicator() {
  const el = document.getElementById("running-indicator");
  try {
    const data = await api("/api/runs");
    const job = data.running;
    if (job) {
      el.replaceChildren(statusPill("running"), h("a", { href: `#/live/${encodeURIComponent(job.run)}` }, job.name));
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  } catch { el.hidden = true; }
}
setInterval(refreshIndicator, 5000);

// --- runs list ---------------------------------------------------------------------------------------------

function runsView() {
  setNav("runs");
  const body = h("tbody");
  const rootNote = h("div", { class: "sub" });
  view.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Runs"), rootNote),
      h("a", { class: "btn primary", href: "#/new" }, "New run")),
    h("div", { class: "panel" },
      h("table", { class: "table" },
        h("thead", {}, h("tr", {},
          h("th", {}, "Run"), h("th", {}, "Game"), h("th", {}, "Status"), h("th", {}, "Started"),
          h("th", { class: "num" }, "Sessions"), h("th", { class: "num" }, "Findings"),
          h("th", { class: "num" }, "Spend"), h("th", {}, ""))),
        body)));

  async function load() {
    try {
      const data = await api("/api/runs");
      rootNote.textContent = data.runs_root;
      if (!data.runs.length) {
        body.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty" }, "No runs found under this folder yet.")));
        return;
      }
      body.replaceChildren(...data.runs.map((run) => {
        const id = encodeURIComponent(run.id);
        const findings = run.kind === "hunt"
          ? (run.findings === null ? "—" : `${run.findings}${run.reproduced ? ` · ${run.reproduced} reproduced` : ""}`)
          : (run.suspicions !== null && run.suspicions !== undefined ? `${run.suspicions} suspicions` : "—");
        return h("tr", {},
          h("td", {}, h("div", { class: "run-name" }, run.name),
            h("div", { class: "run-kind" }, run.kind === "hunt" ? "Hunt" : "Session", " · ", run.id)),
          h("td", {}, unknown(run.game, "—")),
          h("td", {}, statusPill(run.status)),
          h("td", { class: "muted" }, ago(run.started_utc)),
          h("td", { class: "num" }, run.kind === "hunt"
            ? `${run.sessions}${run.sessions_planned ? ` / ${run.sessions_planned}` : ""}` : "—"),
          h("td", { class: "num" }, findings),
          h("td", { class: "num" }, run.cost_usd === undefined || run.cost_usd === null ? "—" : usd(run.cost_usd)),
          h("td", {}, h("div", { class: "row-actions" },
            h("a", { class: "btn small", href: `#/live/${id}` }, "Live"),
            h("a", { class: "btn small", href: `#/findings/${id}` }, "Findings"))));
      }));
    } catch (err) {
      body.replaceChildren(h("tr", {}, h("td", { colspan: 8 }, h("div", { class: "error-box" }, err.message))));
    }
  }
  load();
  const timer = setInterval(load, 5000);
  return () => clearInterval(timer);
}

// --- launch ------------------------------------------------------------------------------------------------

function launchView() {
  setNav("new");
  let options = null;
  let selected = null;
  let preset = "quick";
  const overrides = {};
  let name = "";
  let quoteResult = null;
  let ready = null;
  let quoteSeq = 0;
  let quoteTimer = null;

  const prompt = h("textarea", { class: "prompt-input", rows: 4, maxlength: 2000, autofocus: true,
    placeholder: "What should the agent playtest? For example: try every control and tell me anything that doesn't do what the game says it should." });
  const gameRow = h("div", { class: "config-cards" });
  const presetRow = h("div", { class: "segmented" });
  const presetNote = h("div", { class: "hint" });
  const customBody = h("div", { class: "form-grid", style: "margin-top: 14px" });
  const footer = h("div", { class: "launch-footer" });

  const examples = [
    "Try every control and report anything that doesn't do what the game says it should.",
    "Focus on interacting with things: picking up, talking, pressing buttons.",
    "Walk up every structure, then check movement still behaves the same way.",
  ];

  view.replaceChildren(h("div", { class: "composer" },
    h("h1", { class: "composer-title" }, "New playtest"),
    h("div", { class: "composer-sub" }, "Describe what to test, pick a game, and start. S1 plays; the frontier helps when S1 asks; findings are replayed to confirm them."),
    h("div", { class: "prompt-box" }, prompt,
      h("div", { class: "examples" }, ...examples.map((text) =>
        h("button", { class: "example", onclick: () => { prompt.value = text; prompt.dispatchEvent(new Event("input")); prompt.focus(); } }, text)))),
    h("div", { class: "composer-section" }, h("div", { class: "section-title" }, "Game"), gameRow),
    h("div", { class: "composer-section" }, h("div", { class: "section-title" }, "How long"), presetRow, presetNote,
      h("details", { class: "customize" }, h("summary", {}, "Customize"), customBody)),
    footer));
  setTimeout(() => prompt.focus(), 0);

  api("/api/launch/options").then((data) => {
    options = data;
    if (!data.configs.length) {
      footer.replaceChildren(h("div", { class: "note-box" }, "No hunt configs found in runs/hunts/*.json."));
      return;
    }
    renderPresets();
    select(data.configs[0]);
  }).catch((err) => footer.replaceChildren(h("div", { class: "error-box" }, err.message)));

  // Typing a game's name picks it, so a prompt alone can say what to test and where.
  prompt.addEventListener("input", () => {
    if (!options) return;
    const text = prompt.value.toLowerCase();
    const family = (config) => config.game.toLowerCase().split(/[\s(]/)[0];
    if (selected && text.includes(family(selected))) return;
    const match = options.configs.find((config) => text.includes(family(config)));
    if (match) select(match);
  });

  function settings() {
    const base = { ...options.presets[preset] };
    delete base.label;
    return { ...base, frontier_model: selected.frontier_model, ...overrides };
  }

  function select(config) {
    if (selected === config) return;
    selected = config;
    name = config.suggested_name;
    if (!config.measured_models.includes(overrides.frontier_model)) delete overrides.frontier_model;
    ready = null;
    renderGames();
    renderCustom();
    requote();
    api(`/api/launch/check?${q({ config: config.id })}`)
      .then((data) => { if (selected === config) { ready = data; renderFooter(); } })
      .catch((err) => { if (selected === config) { ready = { blockers: [err.message], prepare: [] }; renderFooter(); } });
  }

  function requote() {
    clearTimeout(quoteTimer);
    quoteResult = null;
    renderFooter();
    quoteTimer = setTimeout(() => {
      const seq = ++quoteSeq;
      api("/api/launch/quote", { body: { config: selected.id, preset, settings: overrides } })
        .then((data) => { if (seq === quoteSeq) { quoteResult = data.quote; renderFooter(); } })
        .catch((err) => { if (seq === quoteSeq) { quoteResult = { usd: null, problems: [err.message] }; renderFooter(); } });
    }, 150);
  }

  function renderGames() {
    gameRow.replaceChildren(...options.configs.map((config) =>
      h("button", { class: `config-card ${config === selected ? "selected" : ""}`, onclick: () => select(config) },
        h("div", { class: "title" }, config.game),
        h("div", { class: "meta" }, config.ref),
        config.missing_files.length ? h("div", { class: "meta", style: "color: var(--bad)" }, `missing: ${config.missing_files.join(", ")}`) : null)));
  }

  function renderPresets() {
    presetRow.replaceChildren(...Object.entries(options.presets).map(([key, p]) =>
      h("button", { class: `segment ${key === preset ? "active" : ""}`, onclick: () => {
        preset = key;
        for (const k of Object.keys(options.limits)) delete overrides[k];
        renderPresets(); renderCustom(); requote();
      } }, h("div", { class: "segment-title" }, p.label),
        h("div", { class: "segment-sub" }, `${p.sessions} session${p.sessions > 1 ? "s" : ""} · ${duration(p.wall_limit_s)} each`))));
    const p = options.presets[preset];
    presetNote.textContent = `Up to ${p.max_frontier_calls} frontier calls per session; each finding replayed ${p.attempts} times.`;
  }

  function renderCustom() {
    if (!selected) return;
    const current = settings();
    const numberField = (key, label) => {
      const [low, high] = options.limits[key];
      const input = h("input", { type: "number", min: low, max: high, value: current[key] });
      input.addEventListener("input", () => { overrides[key] = Number(input.value); requote(); });
      return h("div", { class: "field" }, h("label", {}, label), input);
    };
    const nameInput = h("input", { type: "text", value: name, spellcheck: "false" });
    nameInput.addEventListener("input", () => { name = nameInput.value.trim(); renderFooter(); });
    const models = h("div", { class: "radio-group" }, ...options.frontier_models.map((model) => {
      const ok = selected.measured_models.includes(model);
      const input = h("input", { type: "radio", name: "frontier_model", checked: current.frontier_model === model, disabled: !ok });
      input.addEventListener("change", () => { overrides.frontier_model = model; requote(); });
      const tier = model.endsWith("-contributor") ? "Contributor tier" : model.startsWith("muse-spark") ? "Standard tier" : "";
      return h("label", { class: `radio ${ok ? "" : "disabled"}` }, input,
        h("span", {}, h("span", { class: "mono" }, model), tier && h("span", { class: "faint" }, ` · ${tier}`)),
        !ok && h("span", { class: "why" }, "no measured calls to bound it"));
    }));
    customBody.replaceChildren(
      numberField("sessions", "Live sessions"), numberField("wall_limit_s", "Seconds per session"),
      numberField("max_frontier_calls", "Frontier calls per session"), numberField("attempts", "Replay attempts"),
      numberField("max_calls", "S1 calls per session"),
      h("div", { class: "field" }, h("label", {}, "Run name"), nameInput),
      h("div", { class: "field", style: "grid-column: 1 / -1" }, h("label", {}, "Frontier model"), models,
        h("div", { class: "hint" }, "Standard and Contributor tiers differ in price and data sharing.")));
  }

  function renderFooter() {
    if (!selected) return;
    const quote = quoteResult;
    const amount = quote && typeof quote.usd === "number" ? usd(quote.usd) : "…";
    const problems = [...((quote && quote.problems) || []), ...((ready && ready.blockers) || [])];
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(name || "")) problems.push("Run name: lower-case letters, digits and dashes.");
    if (options.running) problems.push(`Hunt ${options.running.name} is running; one at a time.`);
    const blocked = !quote || !ready || problems.length > 0;
    const confirm = h("input", { type: "checkbox" });
    const button = h("button", { class: "btn primary start", disabled: true }, "Start playtest");
    const status = h("div");
    confirm.addEventListener("change", () => { button.disabled = blocked || !confirm.checked; });
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = ready.prepare.length ? "Preparing the project…" : "Starting…";
      try {
        const data = await api("/api/launch", { body: { config: selected.id, preset, settings: overrides, name,
                                                          prompt: prompt.value, approved_usd: quote.usd } });
        location.hash = `#/live/${encodeURIComponent(data.run)}`;
      } catch (err) {
        status.replaceChildren(h("div", { class: "error-box", style: "margin-top: 12px" }, err.message));
        button.textContent = "Start playtest";
        button.disabled = !confirm.checked;
      }
    });
    footer.replaceChildren(h("div", { class: "panel launch-panel" },
      h("div", { class: "launch-summary" },
        h("div", {},
          h("div", { class: "eyebrow" }, "Most this can spend"),
          h("div", { class: "budget-total" }, h("span", { class: "amount" }, amount),
            quote && quote.lines ? h("span", { class: "faint" }, "every allowed call at its measured per-call bound") : null)),
        h("div", { class: "launch-actions" },
          h("label", { class: "confirm" }, confirm, h("span", {}, "I approve up to ", h("strong", {}, amount))),
          button)),
      quote && quote.lines ? h("details", { class: "breakdown" }, h("summary", {}, "Breakdown"),
        h("ul", { class: "budget-lines" }, ...quote.lines.map((line) => h("li", {}, h("span", {}, line.what), h("span", {}, usd(line.usd, 4)))),
          h("li", {}, h("span", {}, "Per-session cap written to this run's approval"), h("span", {}, usd(quote.session_cap_usd))))) : null,
      ready && ready.prepare.length ? h("div", { class: "prepare" }, h("div", { class: "eyebrow" }, "Start will first"),
        h("ul", {}, ...ready.prepare.map((step) => h("li", {}, step.text)))) : null,
      !ready ? h("div", { class: "faint", style: "margin-top: 12px" }, "Checking the project…") : null,
      problems.length ? h("div", { class: "error-box", style: "margin-top: 12px" }, ...problems.map((p) => h("div", {}, p))) : null,
      status));
  }

  return () => clearTimeout(quoteTimer);
}

// --- live --------------------------------------------------------------------------------------------------

function liveView(runId) {
  setNav("");
  let detail = null;
  let followed = null;      // session id being followed
  let source = null;        // EventSource
  let state = null;
  let renderQueued = false;
  let disposed = false;

  const title = h("h1", {}, runId.split("/").pop());
  const statusSlot = h("span");
  const findingsLink = h("a", { class: "btn small", href: `#/findings/${encodeURIComponent(runId)}` }, "Findings");
  const stepsBar = h("div", { class: "steps" });
  const promptLine = h("div", { class: "prompt-line", hidden: true });
  const img = h("img", { alt: "Game view" });
  const placeholder = h("div", { class: "placeholder" });
  const overlayTL = h("div", { class: "overlay tl" });
  const overlayTR = h("div", { class: "overlay tr" });
  const overlayBL = h("div", { class: "overlay bl" });
  const viewport = h("div", { class: "viewport" }, img, placeholder, overlayTL, overlayTR, overlayBL);
  const stats = h("div", { class: "stats" });
  const actionsList = h("ul", { class: "feed" });
  const suspicionsList = h("ul", { class: "feed" });
  const frontierList = h("ul", { class: "feed" });
  const actionsCount = h("span", { class: "count" });
  const suspicionsCount = h("span", { class: "count" });
  const frontierCount = h("span", { class: "count" });
  const sessionLabel = h("span", { class: "chip" });

  view.replaceChildren(
    h("div", { class: "live-head" }, h("a", { href: "#/", class: "faint" }, "Runs /"), title, statusSlot, sessionLabel,
      h("span", { class: "spacer" }), findingsLink),
    promptLine,
    stepsBar,
    h("div", { class: "live-grid" },
      h("div", { class: "live-left" }, viewport, stats),
      h("div", { class: "live-right" },
        h("div", { class: "panel actions" },
          h("div", { class: "panel-head" }, h("h2", {}, "S1 actions"), actionsCount, h("span", { class: "spacer" }),
            h("span", { class: "faint", style: "font-size: 12px" }, "chosen by S1 · executed")),
          h("div", { class: "scroll" }, actionsList)),
        h("div", { class: "panel suspicions" },
          h("div", { class: "panel-head" }, h("h2", {}, "Suspicions"), suspicionsCount, h("span", { class: "spacer" }),
            h("span", { class: "faint", style: "font-size: 12px" }, "measured facts, not verdicts")),
          h("div", { class: "scroll" }, suspicionsList)),
        h("div", { class: "panel frontier" },
          h("div", { class: "panel-head" }, h("h2", {}, "Frontier & findings"), frontierCount, h("span", { class: "spacer" }),
            h("span", { class: "faint", style: "font-size: 12px" }, "help S1 asked for · what was filed")),
          h("div", { class: "scroll" }, frontierList)))));

  function freshState() {
    return { run: null, turns: 0, lastTurn: null, gameS: null, decisions: new Map(), actions: [], suspicions: [],
             frontier: [], findings: new Map(), s1Calls: 0, s1Cost: 0, s1CostKnown: false, latencies: [],
             ended: null, corrupt: 0, lastAction: null };
  }

  function follow(sessionId, live) {
    if (followed === sessionId) return;
    followed = sessionId;
    if (source) source.close();
    state = freshState();
    actionsList.replaceChildren(); suspicionsList.replaceChildren(); frontierList.replaceChildren();
    sessionLabel.textContent = sessionId.split("/").pop();
    placeholder.hidden = false;
    placeholder.replaceChildren(h("div", {}, h("div", { class: "stage" }, live ? "Waiting for the game view…" : "No recorded frame"),
      h("div", {}, live ? "Frames appear once the Editor enters Play Mode." : "This session left no frames to show.")));
    img.style.visibility = "hidden";
    frameLoop(sessionId);
    source = new EventSource(`/api/events?${q({ run: sessionId })}`);
    source.onmessage = (message) => { apply(JSON.parse(message.data)); queueRender(); };
    source.addEventListener("closed", () => { source.close(); });
  }

  // The newest frame, fetched again as soon as the last one is shown: about as fast as the bridge writes them
  // while the host is up, once for a finished session. The server answers 204 when nothing newer exists.
  let frameToken = 0;
  let frameInfo = null;
  async function frameLoop(sessionId) {
    const token = ++frameToken;
    let shown = "";
    let objectUrl = null;
    while (!disposed && token === frameToken) {
      let wait = 120;
      try {
        const response = await fetch(`/api/frame.jpg?${q({ run: sessionId, after: shown })}`, { cache: "no-store" });
        if (response.status === 200) {
          const blob = await response.blob();
          if (token !== frameToken) return;
          const url = URL.createObjectURL(blob);
          await new Promise((resolve) => { img.onload = resolve; img.onerror = resolve; img.src = url; });
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          objectUrl = url;
          shown = `${response.headers.get("X-Frame-Source")}/${response.headers.get("X-Frame-Name")}`;
          frameInfo = { source: response.headers.get("X-Frame-Source"), written: response.headers.get("X-Frame-Written") };
          img.style.visibility = "visible";
          placeholder.hidden = true;
        } else if (response.status === 404) {
          wait = 1000;
        }
        if (frameInfo && frameInfo.source !== "live") wait = 3000;
      } catch { wait = 1500; }
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }

  function apply(event) {
    const s = state;
    switch (event.type) {
      case "run": s.run = event; break;
      case "observation": s.gameS = event.game_s; break;
      case "decision": s.decisions.set(event.turn, event); break;
      case "turn": {
        s.turns += 1; s.lastTurn = event.turn;
        const decision = s.decisions.get(event.turn);
        if (typeof event.latency_ms === "number") s.latencies.push(event.latency_ms);
        s.actions.push({ ...event, decision, fresh: true });
        s.lastAction = event;
        break;
      }
      case "option": s.actions.push({ ...event, fresh: true }); break;
      case "s1_call":
        s.s1Calls += 1;
        if (typeof event.cost === "number") { s.s1Cost += event.cost; s.s1CostKnown = true; }
        break;
      case "suspicion": s.suspicions.push({ ...event, fresh: true }); break;
      case "finding": s.findings.set(event.finding_id, event); s.frontier.push({ ...event, fresh: true }); break;
      case "help":
        if (event.event !== "repeat") s.frontier.push({ ...event, fresh: true });
        break;
      case "investigation":
        if (event.event === "directive" || event.event === "hypothesis_directive" || event.event === "outcome")
          s.frontier.push({ ...event, fresh: true });
        break;
      case "restart": s.frontier.push({ ...event, fresh: true }); break;
      case "end": s.ended = event; break;
      case "corrupt": s.corrupt += 1; break;
    }
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; renderFeeds(); });
  }

  const MAX_ROWS = 150;

  function actionRow(a) {
    if (a.type === "option") {
      return h("li", {},
        h("div", { class: "row" }, h("span", { class: "turn" }, a.turn !== null ? `T${a.turn}` : ""),
          h("span", { class: "grow title" }, a.label || a.option_id),
          h("span", { class: "pill info" }, a.template || "option")),
        h("div", { class: "meta" }, h("span", {}, `ended: ${unknown(a.terminated_by)}`),
          typeof a.duration_s === "number" ? h("span", {}, `${a.duration_s.toFixed(1)} s`) : null));
    }
    const audit = a.raw_choice !== a.effective_choice || (a.executed_action && a.effective_choice !== a.executed_action);
    const outcomeKind = a.outcome === "executed" ? "ok" : a.outcome ? "warn" : "";
    const d = a.decision || {};
    return h("li", { class: audit ? "audit" : "" },
      h("div", { class: "row" },
        h("span", { class: "turn" }, `T${a.turn}`),
        h("span", { class: "grow title", title: a.effective_choice || "" }, a.label || a.effective_choice || "unknown"),
        h("span", { class: `pill ${outcomeKind}` }, unknown(a.outcome)),
        h("span", { class: "latency" }, typeof a.latency_ms === "number" ? `${Math.round(a.latency_ms)} ms` : "")),
      h("div", { class: "meta" },
        h("span", { class: "mono" }, a.effective_choice),
        d.source ? h("span", {}, `${d.source}${d.condition ? ` (${d.condition})` : ""}`) : null,
        typeof d.probability === "number" ? h("span", {}, `p ${d.probability.toFixed(2)}`) : null,
        a.policy_default ? h("span", { style: "color: var(--warn)" }, "policy default") : null,
        a.while_pending ? h("span", {}, "while help pending") : null,
        audit ? h("span", { style: "color: var(--warn)" }, `raw ${a.raw_choice} → effective ${a.effective_choice} → executed ${unknown(a.executed_action, "none")}`) : null),
      d.reason ? h("div", { class: "text" }, d.reason) : null);
  }

  function suspicionRow(s) {
    return h("li", {},
      h("div", { class: "row" }, h("span", { class: "turn" }, s.turn !== null ? `T${s.turn}` : ""),
        h("span", { class: "chip" }, s.basis), h("span", { class: "grow mono faint" }, s.subject)),
      h("div", { class: "text" }, s.statement));
  }

  function frontierRow(f) {
    if (f.type === "finding") {
      return h("li", { class: "finding" },
        h("div", { class: "row" }, h("span", { class: "pill bad" }, "Finding filed"), severityPill(f.severity),
          h("span", { class: "chip" }, f.finding_id), h("span", { class: "grow" }), h("span", { class: "faint" }, f.state)),
        h("div", { class: "title", style: "margin-top: 6px" }, f.title),
        f.actual ? h("div", { class: "text" }, f.actual) : null);
    }
    if (f.type === "help") {
      const text = f.event === "opened" ? `S1 asked the frontier for help (${unknown(f.help_kind)})`
        : f.event === "adopted" ? `Frontier answered${f.hypotheses.length ? ` with ${f.hypotheses.length} hypotheses` : ""}`
        : f.event === "cancelled" ? "Help request cancelled" : `Help: ${f.event}`;
      return h("li", { class: "help" },
        h("div", { class: "row" }, h("span", { class: "turn" }, f.turn !== null ? `T${f.turn}` : ""), h("span", { class: "grow title" }, text)),
        f.event === "adopted" && f.hypotheses.length ? h("div", { class: "text" },
          h("ul", { style: "margin: 4px 0 0; padding-left: 18px" }, ...f.hypotheses.slice(0, 4).map((x) => h("li", { style: "border: 0; padding: 2px 0; animation: none" }, x.statement))))
          : null,
        f.reason ? h("div", { class: "text" }, f.reason) : null);
    }
    if (f.type === "investigation") {
      const label = f.event === "outcome" ? "Directive outcome" : "Frontier directive";
      return h("li", { class: "help" },
        h("div", { class: "row" }, h("span", { class: "pill info" }, label),
          f.directive && f.directive.id ? h("span", { class: "chip" }, f.directive.id) : null),
        f.directive && f.directive.text ? h("div", { class: "text" }, f.directive.text) : null,
        f.event === "outcome" && f.outcome ? h("div", { class: "text mono" }, f.outcome) : null);
    }
    if (f.type === "restart") {
      return h("li", {}, h("div", { class: "row" }, h("span", { class: "pill warn" }, "Play mode restarted")));
    }
    return h("li", {}, f.type);
  }

  function renderList(list, items, row) {
    const shown = items.slice(-MAX_ROWS).reverse();
    list.replaceChildren(...shown.map((item) => {
      const el = row(item);
      if (!item.fresh) el.style.animation = "none";
      item.fresh = false;
      return el;
    }));
  }

  function renderFeeds() {
    if (!state) return;
    const s = state;
    renderList(actionsList, s.actions, actionRow);
    renderList(suspicionsList, s.suspicions, suspicionRow);
    renderList(frontierList, s.frontier, frontierRow);
    actionsCount.textContent = s.turns ? `${s.turns} turns` : "";
    suspicionsCount.textContent = String(s.suspicions.length || "");
    frontierCount.textContent = s.findings.size ? `${s.findings.size} filed` : "";
    if (!s.actions.length) actionsList.replaceChildren(h("li", { class: "empty", style: "animation: none" }, "Waiting for S1's first decision…"));
    if (!s.suspicions.length) suspicionsList.replaceChildren(h("li", { class: "empty", style: "animation: none" }, "None raised."));
    if (!s.frontier.length) frontierList.replaceChildren(h("li", { class: "empty", style: "animation: none" }, "No help requests or findings yet."));
    renderOverlay();
    renderStats();
  }

  function renderOverlay() {
    const s = state;
    const live = !s.ended && detail && (detail.status === "live" || detail.status === "running");
    const age = frameInfo && frameInfo.written ? (Date.now() - new Date(frameInfo.written).getTime()) / 1000 : null;
    overlayTL.replaceChildren(...[live ? h("span", { class: "pill live live-badge" }, h("span", { class: "dot" }), "LIVE")
      : h("span", { class: "pill" }, s.ended ? `ENDED · ${unknown(s.ended.status)}` : "RECORDED"),
      live && frameInfo && frameInfo.source === "live" && Number.isFinite(age)
        ? h("span", { class: "pill mono" }, `frame ${Math.max(0, age).toFixed(1)} s old`) : null].filter(Boolean));
    overlayTR.replaceChildren(...[
      s.run && s.run.scene ? h("span", { class: "pill" }, s.run.scene) : null,
      typeof s.gameS === "number" ? h("span", { class: "pill mono" }, `game ${duration(s.gameS)}`) : null].filter(Boolean));
    const a = s.lastAction;
    if (a && (!overlayBL.dataset.turn || overlayBL.dataset.turn !== String(a.turn))) {
      overlayBL.dataset.turn = String(a.turn);
      overlayBL.replaceChildren(h("div", { class: "action-toast" }, h("span", { class: "who" }, "S1"),
        h("span", { class: "what" }, a.label || a.effective_choice), h("span", { class: "chip" }, `T${a.turn}`)));
    }
  }

  function renderStats() {
    const s = state;
    const cost = detail && typeof detail.cost_usd === "number" ? detail.cost_usd : null;
    const med = median(s.latencies.slice(-30));
    const tile = (label, value, small) => h("div", { class: "stat" }, h("div", { class: "label" }, label),
      h("div", { class: "value" }, value, small ? h("small", {}, ` ${small}`) : null));
    stats.replaceChildren(
      tile("Turn", s.lastTurn ?? "—"),
      tile("S1 calls", s.s1Calls),
      tile("S1 latency", med === null ? "—" : Math.round(med), med === null ? "" : "ms med"),
      tile("Spend", cost === null ? "unknown" : usd(cost), detail && detail.kind === "hunt" ? "hunt" : "session"),
      tile("Suspicions", s.suspicions.length),
      tile("Findings", s.findings.size));
  }

  function renderSteps() {
    if (!detail || detail.kind !== "hunt" || !detail.steps || !detail.steps.length) { stepsBar.hidden = true; return; }
    stepsBar.hidden = false;
    const parts = [];
    detail.steps.forEach((step, i) => {
      if (i) parts.push(h("span", { class: `step-line ${step.state === "done" ? "done" : ""}` }));
      const cls = step.state === "no result" ? "failed" : step.state;
      parts.push(h("span", { class: `step ${cls}`, title: step.state },
        h("span", { class: "bullet" }, step.state === "done" ? "✓" : step.state === "no result" ? "!" : ""),
        step.label + (step.key === "replays" && step.count ? ` ${step.finished}/${step.count}` : "")));
    });
    stepsBar.replaceChildren(...parts);
  }

  function showStage(text, lines) {
    placeholder.hidden = false;
    frameToken += 1;
    img.style.visibility = "hidden";
    placeholder.replaceChildren(h("div", {}, h("div", { class: "stage" }, text),
      lines && lines.length ? h("pre", {}, lines.slice(-8).join("\n")) : null));
  }

  async function poll() {
    if (disposed) return;
    try {
      detail = await api(`/api/run?${q({ run: runId })}`);
      statusSlot.replaceChildren(statusPill(detail.status));
      title.textContent = detail.name || runId;
      const asked = (detail.config && detail.config.prompt) || (detail.job && detail.job.prompt);
      promptLine.hidden = !asked;
      if (asked) promptLine.replaceChildren(h("span", { class: "eyebrow" }, "Asked"), h("span", {}, `“${asked}”`));
      renderSteps();
      if (detail.kind === "session") {
        follow(detail.id, detail.status === "live");
      } else if (detail.pending) {
        showStage("Starting the hunt…", detail.log_tail);
      } else {
        const active = detail.active;
        const sessionStep = active && active.session ? active : null;
        if (sessionStep) {
          follow(`${detail.id}/${sessionStep.session}`, true);
        } else if (active) {
          followed = null; if (source) source.close();
          showStage(`${active.label}${active.key.startsWith("predict") ? " · one frontier call" : active.key === "replays" ? " · no model calls" : ""}`, detail.log_tail);
        } else if (!followed) {
          const sessions = detail.steps.filter((s) => s.session && s.state !== "pending");
          const last = sessions.filter((s) => s.key.startsWith("session")).pop() || sessions.pop();
          if (last) follow(`${detail.id}/${last.session}`, false);
          else showStage(detail.status === "complete" ? "Hunt complete" : `Hunt ${detail.status}`, detail.log_tail);
        }
      }
      if (state) renderStats(), renderOverlay();
    } catch (err) {
      showStage(err.message);
    }
  }

  poll();
  const timer = setInterval(poll, 2000);
  return () => { disposed = true; frameToken += 1; clearInterval(timer); if (source) source.close(); };
}

// --- findings ----------------------------------------------------------------------------------------------

function findingsView(runId) {
  setNav("");
  const root = h("div");
  view.replaceChildren(root);
  let timer = null;

  async function load() {
    try {
      const [detail, data] = await Promise.all([api(`/api/run?${q({ run: runId })}`), api(`/api/findings?${q({ run: runId })}`)]);
      render(detail, data);
      if (!data.final && !timer) timer = setInterval(load, 8000);
      if (data.final && timer) { clearInterval(timer); timer = null; }
    } catch (err) {
      root.replaceChildren(h("div", { class: "error-box" }, err.message));
    }
  }

  function replayOf(f) {
    const r = f.replay;
    if (!r) return { label: "Not replayed yet", k: null };
    if (typeof r.k === "number" && typeof r.n === "number") return { label: `Reproduced ${r.k} of ${r.n}`, k: r.k, n: r.n, unknown: r.unknown || 0 };
    return { label: r.status === "no repro_check" ? "Not replayable (no repro check)" : `Replay ${unknown(r.status)}`, k: null, reason: r.reason };
  }

  function reproCheck(check) {
    if (!check) return null;
    let text = `${check.field} ${check.op}`;
    if (check.value !== null && check.value !== undefined) text += ` ${JSON.stringify(check.value)}`;
    if (check.tolerance) text += ` ± ${check.tolerance}`;
    if (check.after_action) text += ` after ${check.after_action}`;
    if (check.within_region) text += ` within ${check.within_region}`;
    return text;
  }

  function evidence(f) {
    const ev = f.evidence || {};
    const box = h("div", { class: "evidence" });
    const player = h("div", { style: "width: 100%" });
    const sessionId = f.session_id;
    if (ev.video) {
      const at = ev.video.offset_s;
      box.append(h("button", { class: "btn small primary", onclick: () => {
        const video = h("video", { controls: true, autoplay: true, muted: true, preload: "metadata",
                                   src: `${fileUrl(sessionId, ev.video.file)}#t=${Math.max(0, at - 3)}` });
        video.addEventListener("loadedmetadata", () => { if (video.currentTime < 0.5) video.currentTime = Math.max(0, at - 3); });
        player.replaceChildren(video, h("div", { class: "faint", style: "font-size: 12px; margin-top: 6px" },
          `Session recording at ≈${duration(at)} (starts 3 s before the cited entry; frame times are JPEG write times, so the offset is approximate).`));
      } }, `▶ Watch evidence at ≈${duration(at)}`));
    } else {
      box.append(h("span", { class: "faint", style: "font-size: 12px" }, ev.cited_utc ? "No session recording covers the cited moment." : "The finding cites no timed entry."));
    }
    for (const clip of ev.clips || []) {
      box.append(h("button", { class: "btn small", onclick: () => {
        player.replaceChildren(h("video", { controls: true, autoplay: true, muted: true, src: fileUrl(sessionId, clip.file) }),
          h("div", { class: "faint", style: "font-size: 12px; margin-top: 6px" }, `The clip the frontier was shown (${clip.turn_folder}).`));
      } }, `Frontier clip ${clip.turn_folder}`));
    }
    box.append(player);
    return box;
  }

  function card(f) {
    const r = replayOf(f);
    const dots = r.k !== null ? h("div", { class: "dots" }, ...Array.from({ length: r.n }, (_, i) =>
      h("span", { class: i < r.k ? "hit" : i >= r.n - r.unknown ? "unknown" : "" }))) : null;
    const check = reproCheck(f.repro_check);
    return h("div", { class: `finding-card sev-${f.severity || "unknown"}` },
      h("div", { class: "finding-head" },
        h("div", { style: "flex: 1; min-width: 0" },
          h("h3", {}, f.title),
          h("div", { class: "finding-chips" }, severityPill(f.severity), h("span", { class: "pill" }, f.state),
            h("span", { class: "chip" }, f.finding_id), h("span", { class: "chip" }, f.session || ""))),
        h("div", { class: "replay" }, dots,
          h("div", { class: "label", style: r.k ? "color: var(--ok)" : "" }, r.label),
          r.reason && r.k !== null ? null : r.reason ? h("div", { class: "faint", style: "font-size: 12px; max-width: 260px; text-align: right" }, r.reason) : null)),
      h("div", { class: "ea" },
        h("div", {}, h("h4", {}, "Expected", h("span", { class: "chip" }, f.expected_source)), h("p", {}, f.expected)),
        h("div", {}, h("h4", {}, "Actual"), h("p", {}, f.actual))),
      h("dl", { class: "finding-meta" },
        f.conditions ? [h("dt", {}, "Conditions"), h("dd", {}, f.conditions)] : null,
        f.steps && f.steps.length ? [h("dt", {}, "Steps"), h("dd", {}, h("ol", {}, ...f.steps.map((s) => h("li", {}, s))))] : null,
        check ? [h("dt", {}, "Repro check"), h("dd", {}, h("code", {}, check))] : null,
        f.severity_reason ? [h("dt", {}, "Severity reason"), h("dd", {}, f.severity_reason)] : null,
        f.uncertainty ? [h("dt", {}, "Uncertainty"), h("dd", {}, f.uncertainty)] : null,
        f.evidence_seq && f.evidence_seq.length ? [h("dt", {}, "Cites trace"), h("dd", { class: "mono" }, f.evidence_seq.join(", "))] : null),
      evidence(f));
  }

  function render(detail, data) {
    const items = [...data.findings].sort((a, b) => {
      const ka = (a.replay && a.replay.k) || 0, kb = (b.replay && b.replay.k) || 0;
      if (kb !== ka) return kb - ka;
      const rank = { blocker: 0, major: 1, minor: 2, cosmetic: 3 };
      return (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9);
    });
    const reproduced = items.filter((f) => f.replay && f.replay.k > 0).length;
    const replayed = items.filter((f) => f.replay && typeof f.replay.n === "number").length;
    const notReplayable = items.filter((f) => f.replay && f.replay.status === "no repro_check").length;
    const tile = (label, value) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));
    root.replaceChildren(...[
      h("div", { class: "page-head" },
        h("div", {}, h("div", { class: "eyebrow" }, detail.kind === "hunt" ? "Hunt findings" : "Session findings"),
          h("h1", {}, detail.name, " ", statusPill(detail.status)),
          h("div", { class: "sub" }, unknown(detail.game || (detail.config && detail.config.game), "")),
          detail.config && detail.config.prompt ? h("div", { class: "prompt-line", style: "margin: 10px 0 0" },
            h("span", { class: "eyebrow" }, "Asked"), h("span", {}, `“${detail.config.prompt}”`)) : null),
        h("div", { style: "display: flex; gap: 8px" },
          h("a", { class: "btn", href: `#/live/${encodeURIComponent(runId)}` }, "Replay live view"),
          detail.report_md ? h("a", { class: "btn", href: fileUrl(runId, "hunt.md"), target: "_blank" }, "hunt.md") : null)),
      !data.final ? h("div", { class: "note-box", style: "margin-bottom: 16px" },
        "This run has no final report yet: these are the findings its sessions filed so far. Replays are added when the hunt writes its report.") : null,
      h("div", { class: "summary-tiles" },
        tile("Filed", items.length), tile("Reproduced", reproduced), tile("Replayed", replayed),
        tile("Not replayable", notReplayable), tile("Sessions", (data.sessions || []).length),
        tile("Spend", usd(data.cost_usd))),
      items.length ? h("div", { class: "findings-list" }, ...items.map(card))
        : h("div", { class: "panel" }, h("div", { class: "empty" }, "No findings filed.")),
      data.unmet && data.unmet.length ? h("div", { class: "panel", style: "margin-top: 20px" },
        h("div", { class: "panel-head" }, h("h2", {}, "Expectations not met"), h("span", { class: "count" }, data.unmet.length),
          h("span", { class: "spacer" }), h("span", { class: "faint", style: "font-size: 12px" }, "scored by code against the predictions")),
        h("ul", { class: "unmet" }, ...data.unmet.map((line) => h("li", {}, line)))) : null].filter(Boolean));
  }

  load();
  return () => { if (timer) clearInterval(timer); };
}

// --- router ------------------------------------------------------------------------------------------------

function route() {
  dispose();
  const hash = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = hash.split("/");
  const arg = decodeURIComponent(rest.join("/"));
  if (name === "new") dispose = launchView();
  else if (name === "live" && arg) dispose = liveView(arg);
  else if (name === "findings" && arg) dispose = findingsView(arg);
  else dispose = runsView();
  window.scrollTo(0, 0);
}

function showFault(message) {
  view.prepend(h("div", { class: "error-box", style: "margin-bottom: 16px" }, `Dashboard error: ${message}`));
}
window.addEventListener("error", (event) => showFault(event.message));
window.addEventListener("unhandledrejection", (event) => showFault(event.reason && event.reason.message || String(event.reason)));

window.addEventListener("hashchange", route);
route();
refreshIndicator();
