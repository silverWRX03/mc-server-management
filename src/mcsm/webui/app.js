"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel) => document.querySelector(sel);

// Build DOM nodes. Text is always inserted as text, never HTML: mod names,
// descriptions, player names and console output all come from outside.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

class Unauthorized extends Error {}

async function api(path, { method = "GET", body, raw } = {}) {
  const headers = { "X-MCSM": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: raw !== undefined ? raw : body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Unauthorized(); }
  if (res.status === 428) { showNotice(); throw new Unauthorized(); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// A toast that stays until the user picks an action.
function stickyToast(id, children) {
  if (document.getElementById(id)) return;
  $("#toasts").append(h("div", { class: "toast sticky", id }, children));
}
function closeToast(id) { const el = document.getElementById(id); if (el) el.remove(); }

function toast(message, bad = false) {
  const el = h("div", { class: "toast" + (bad ? " bad" : "") }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 9000 : 5000);
}

async function act(fn, okMessage) {
  try {
    const r = await fn();
    if (okMessage) toast(okMessage);
    await refreshStatus();
    return r;
  } catch (e) {
    if (!(e instanceof Unauthorized)) toast(e.message, true);
  }
}

const fmtBytes = (n) => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(1) + " MB";
const fmtTime = (t) => new Date(t * 1000).toLocaleString();
const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function fmtDuration(s) {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), hr = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${hr}h` : hr ? `${hr}h ${m}m` : `${m}m ${s % 60}s`;
}
function ago(t) {
  const s = Date.now() / 1000 - t;
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : fmtTime(t);
}
// Like el.replaceChildren(), but skips null/false (which would otherwise render as "null").
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}
function card(title, ...children) { return h("div", { class: "card" }, title ? h("h3", {}, title) : null, ...children); }

// -------------------------------------------------------------------- state
let status = null;
let lastJobSeen = null;
let current = null;           // current view
let timers = [];

function every(ms, fn) { fn(); timers.push(setInterval(fn, ms)); }
function clearTimers() { timers.forEach(clearInterval); timers = []; }

// -------------------------------------------------------------------- login
const PROMPT_KEY = "mcsm-password-prompt-dismissed";
async function showLogin() {
  clearTimers();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  let a = null;
  try { a = await (await fetch("/api/auth", { credentials: "same-origin" })).json(); } catch (_) { /* offline */ }
  const input = $("#login-password");
  const pin = a && a.mode === "pin";
  $("#login-label").textContent = pin ? "PIN" : "Password";
  input.setAttribute("inputmode", pin ? "numeric" : "text");
  input.setAttribute("autocomplete", pin ? "off" : "current-password");
  $("#login-fields").classList.toggle("hidden", !!(a && a.mode === "none"));
  $("#login-hint").replaceChildren(...(
    !a ? [] :
    a.mode === "none" ? ["This control panel has no password, so it only opens on the server's own computer. To use it from here, set a PIN or password there (Settings → Sign-in)."] :
    a.managed ? ["The password is set in mcsm.toml under ", h("code", {}, "[web] password"), "."] :
    a.default ? ["First time? The password is ", h("strong", {}, "PASSWORD"), ". You'll choose your own next."] :
    ["Forgot it? Run ", h("code", {}, "mcsm web-password --reset"), " on the server to go back to PASSWORD."]));
  input.focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    $("#login-password").value = "";
    try { sessionStorage.removeItem(PROMPT_KEY); } catch (_) {}
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ------------------------------------------------------------ first-run notice
let noticeOpening = false;
async function showNotice() {
  // Guard synchronously: several status polls can call this before the fetch returns.
  if (noticeOpening || $("#notice")) return;
  noticeOpening = true;
  let n;
  try { n = await (await fetch("/api/notice", { credentials: "same-origin" })).json(); } catch (_) { n = null; }
  noticeOpening = false;
  if (!n || n.accepted || $("#notice")) return;
  clearTimers();
  const accept = h("button", { class: "btn primary", onclick: async () => {
    try {
      await api("/api/notice/accept", { method: "POST", body: { version: n.version } });
      $("#notice").remove();
      route();
    } catch (e) { if (!(e instanceof Unauthorized)) toast(e.message, true); }
  } }, "I understand and accept");
  const decline = h("button", { class: "btn ghost", onclick: async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    $("#notice").remove();
    showLogin();
  } }, "Decline and sign out");
  document.body.append(h("div", { class: "modal-backdrop", id: "notice", role: "dialog", "aria-modal": "true", "aria-labelledby": "notice-title" },
    h("div", { class: "modal" },
      h("h2", { id: "notice-title" }, n.title),
      h("ul", { class: "notice-points" }, n.points.map((p) => h("li", {}, p))),
      h("p", { class: "muted small" }, "The Minecraft server won't start until this is accepted. You can read it again under Settings → About."),
      h("div", { class: "row" }, accept, decline))));
  accept.focus();
}

// ------------------------------------------------------------ sign-in settings
const AUTH_MODES = [
  ["password", "Password", "At least 4 characters."],
  ["pin", "PIN", "4 to 8 digits. Quick to type on a phone."],
  ["none", "No password", "Opens without signing in, but only on the server's own computer."],
];
async function showSecurity(firstTime = false) {
  if ($("#security")) return;
  let local = false;
  try { local = !!(await (await fetch("/api/auth", { credentials: "same-origin" })).json()).local; } catch (_) {}
  if ($("#security")) return;
  let mode = status && status.auth && !status.auth.default ? status.auth.mode : "password";
  if (mode === "none" && !local) mode = "pin";
  const close = () => { const m = $("#security"); if (m) m.remove(); };
  const box = h("div", { class: "modal compact" });
  const render = (error) => {
    const pin = mode === "pin";
    const kind = pin ? "PIN" : "password";
    const extra = pin ? { inputmode: "numeric", maxlength: 8, autocomplete: "off" } : { autocomplete: "new-password" };
    const secret = h("input", { type: "password", ...extra });
    const again = h("input", { type: "password", ...extra });
    const save = async (e) => {
      e.preventDefault();
      if (mode !== "none" && secret.value !== again.value) return render(`The two ${kind}s don't match.`);
      try {
        await api("/api/auth/change", { method: "POST", body: { mode, secret: mode === "none" ? "" : secret.value } });
        close();
        toast(mode === "none" ? "Password turned off for this computer" : `Your new ${kind} is saved`);
        refreshStatus();
      } catch (err) { if (!(err instanceof Unauthorized)) render(err.message); }
    };
    fill(box,
      h("h2", { id: "security-title" }, firstTime ? "Choose your own password" : "Sign-in"),
      firstTime ? h("p", {}, "You're signed in with the default password, PASSWORD, which anyone could guess. Pick how you'd like to protect this control panel.") : null,
      h("div", { class: "choices" }, AUTH_MODES.map(([m, label, desc]) => h("button", {
        type: "button", class: "choice" + (mode === m ? " selected" : ""), disabled: m === "none" && !local,
        onclick: () => { mode = m; render(); },
      }, h("strong", {}, label), h("span", { class: "small muted" }, m === "none" && !local ? "Only available on the server's own computer." : desc)))),
      h("form", { class: "mt", onsubmit: save },
        mode === "none"
          ? h("p", { class: "muted" }, "Anyone using this computer can open the panel. Other devices won't be able to use it at all until you set a password or PIN again.")
          : h("div", { class: "grid" }, h("label", {}, `New ${kind}`, secret), h("label", {}, `Type it again`, again)),
        h("p", { class: "error" }, error || ""),
        h("div", { class: "row" },
          h("button", { class: "btn primary", type: "submit" }, "Save"),
          h("button", { class: "btn ghost", type: "button", onclick: () => {
            if (firstTime) { try { sessionStorage.setItem(PROMPT_KEY, "1"); } catch (_) {} }
            close();
          } }, firstTime ? "Not now" : "Cancel")),
        h("p", { class: "muted small" }, "Everyone else is signed out when this changes. Forgot it later? Run ",
          h("code", {}, "mcsm web-password --reset"), " on the server.")));
    const first = box.querySelector("input");
    if (first) first.focus();
  };
  render();
  document.body.append(h("div", { class: "modal-backdrop", id: "security", role: "dialog", "aria-modal": "true", "aria-labelledby": "security-title" }, box));
}
function promptDismissed() { try { return !!sessionStorage.getItem(PROMPT_KEY); } catch (_) { return false; } }

// ------------------------------------------------------------ mcsm self-update
const DISMISS_KEY = "mcsm-dismissed-update";
function dismissed() { try { return localStorage.getItem(DISMISS_KEY); } catch (_) { return null; } }
function offerSelfUpdate(u, force = false) {
  if (!u || (!force && dismissed() === u.version)) return;
  const later = () => { try { localStorage.setItem(DISMISS_KEY, u.version); } catch (_) {} closeToast("self-update"); };
  const install = () => {
    if (!confirm(`Update mcsm ${u.current} → ${u.version}?\n\nmcsm installs the update, stops the Minecraft server cleanly (with a 1-minute warning if players are online), and restarts on the new version. You'll need to sign in again afterwards.`)) return;
    closeToast("self-update");
    act(() => api("/api/self-update/apply", { method: "POST", body: { version: u.version } }), "Updating mcsm… this page reconnects when it's back.");
  };
  stickyToast("self-update", [
    h("strong", {}, `mcsm ${u.version} is available`),
    h("div", { class: "small muted" }, `You have ${u.current}. `, u.url ? h("a", { href: u.url, target: "_blank", rel: "noopener noreferrer" }, "What's new ↗") : null),
    u.can_install ? null : h("div", { class: "small" }, u.reason),
    h("div", { class: "row mt-s" },
      u.can_install ? h("button", { class: "btn primary small", onclick: install }, "Update now") : null,
      h("button", { class: "btn small", onclick: later }, "Later")),
  ]);
}

// ------------------------------------------------------------------- status
async function refreshStatus() {
  try { status = await api("/api/status"); } catch (e) {
    if (!(e instanceof Unauthorized)) { $("#state-pill").textContent = "reconnecting"; $("#state-pill").className = "pill"; }
    return;
  }
  const s = status;
  const pill = $("#state-pill");
  pill.textContent = s.state;
  pill.className = "pill " + s.state;
  $("#server-title").textContent = s.minecraft
    ? `Minecraft ${s.minecraft} · ${s.loader}${s.loader_version ? " " + s.loader_version : ""}`
    : "No server installed yet";
  $("#version").textContent = "v" + s.version;
  const busy = !!s.job;
  $("#job").classList.toggle("hidden", !busy);
  $("#job-name").textContent = busy ? s.job.name + "…" : "";
  $("#btn-start").disabled = busy || s.state !== "stopped";
  $("#btn-stop").disabled = busy || s.state === "stopped";
  $("#btn-restart").disabled = busy || s.state !== "running";
  $("#nav-update-dot").classList.toggle("hidden", !(s.update && !s.update.up_to_date && s.update.target));

  if (s.last_job && s.last_job.finished !== lastJobSeen) {
    if (lastJobSeen !== null) toast(`${s.last_job.name}: ${s.last_job.message}`, !s.last_job.ok);
    lastJobSeen = s.last_job.finished;
    if (current && current.onJobDone) current.onJobDone();
  } else if (lastJobSeen === null) {
    lastJobSeen = s.last_job ? s.last_job.finished : 0;
  }
  document.body.classList.toggle("setup-mode", !!s.setup_pending);
  if (s.notice_accepted && s.setup_pending && currentName !== "setup") { location.hash = "#setup"; return; }
  if (!s.setup_pending && currentName === "setup") { location.hash = "#dashboard"; return; }
  $("#logout").classList.toggle("hidden", !!(s.auth && s.auth.mode === "none"));
  if (!s.notice_accepted) showNotice();
  else {
    offerSelfUpdate(s.self_update);
    if (s.auth && s.auth.default && !s.auth.managed && !promptDismissed()) showSecurity(true);
  }
  if (current && current.onStatus) current.onStatus(s);
}

$("#btn-start").addEventListener("click", () => act(() => api("/api/server/start", { method: "POST" })));
$("#btn-stop").addEventListener("click", () => {
  if (confirm("Stop the server? Players will be disconnected.")) act(() => api("/api/server/stop", { method: "POST" }));
});
$("#btn-restart").addEventListener("click", () => act(() => api("/api/server/restart", { method: "POST" })));

// -------------------------------------------------------------------- views
const views = {};

// A live server console: output plus a command box. Used on the Console page and the dashboard.
function consolePanel({ compact = false } = {}) {
  const out = h("div", { class: "console" + (compact ? " compact" : "") });
  const input = h("input", { placeholder: "Type a server command, e.g. say hello  (↑/↓ for history)", autocomplete: "off",
    "aria-label": "Server command" });
  const history = []; let hi = 0; let seq = 0;

  const cls = (line) => line.user ? "l-user" : /\/(ERROR|FATAL)\]|Exception/.test(line.text) ? "l-error" : /\/WARN\]/.test(line.text) ? "l-warn" : "";
  const poll = async () => {
    const r = await api(`/api/console?since=${seq}`).catch(() => null);
    if (!r || !r.lines.length) return;
    const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    seq = r.last;
    const frag = document.createDocumentFragment();
    for (const l of r.lines) frag.append(h("div", { class: cls(l) }, l.text));
    out.append(frag);
    while (out.childElementCount > (compact ? 500 : 3000)) out.firstChild.remove();
    if (stick) out.scrollTop = out.scrollHeight;
  };
  const send = async () => {
    const command = input.value.trim();
    if (!command) return;
    history.push(command); hi = history.length;
    input.value = "";
    await act(() => api("/api/command", { method: "POST", body: { command } }));
    poll();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send();
    else if (e.key === "ArrowUp" && hi > 0) { input.value = history[--hi]; e.preventDefault(); }
    else if (e.key === "ArrowDown") { hi = Math.min(history.length, hi + 1); input.value = history[hi] || ""; }
  });
  const el = h("div", { class: compact ? "console-panel" : "console-wrap" },
    out, h("div", { class: "console-input" }, input, h("button", { class: "btn primary", onclick: send }, "Send")));
  return { el, input, poll };
}

// A player's face, cut from their skin (served by mcsm), or a lettered tile if there's none.
const skinFails = new Set();
function playerHead(name, size = 32) {
  const c = h("canvas", { width: size, height: size, class: "head", "aria-hidden": "true" });
  const ctx = c.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const tile = () => {
    let hash = 0;
    for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    ctx.fillStyle = `hsl(${hash % 360} 45% 38%)`;
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#fff";
    ctx.font = `bold ${Math.round(size * 0.55)}px system-ui, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(name.charAt(0).toUpperCase(), size / 2, size / 2 + 1);
  };
  if (skinFails.has(name)) { tile(); return c; }
  const img = new Image();
  img.addEventListener("load", () => {
    ctx.drawImage(img, 8, 8, 8, 8, 0, 0, size, size);        // face
    if (img.height >= 64) ctx.drawImage(img, 40, 8, 8, 8, 0, 0, size, size);  // hat layer
  });
  img.addEventListener("error", () => { skinFails.add(name); tile(); });
  img.src = `/api/players/skin?name=${encodeURIComponent(name)}`;
  return c;
}

function meter(label) {
  const value = h("strong", {});
  const fillBar = h("div", { class: "bar-fill" });
  const note = h("div", { class: "muted small" });
  const el = h("div", { class: "card meter" },
    h("div", { class: "meter-head" }, h("span", {}, label), value), h("div", { class: "bar" }, fillBar), note);
  return {
    el,
    set(pct, text, sub) {
      value.textContent = text;
      note.textContent = sub || "";
      const p = pct === null || pct === undefined ? 0 : Math.max(0, Math.min(100, pct));
      fillBar.style.width = p + "%";  // CSSOM, allowed by the CSP (unlike style attributes)
      fillBar.className = "bar-fill" + (p >= 90 ? " bad" : p >= 75 ? " warn" : "");
    },
  };
}

views.dashboard = () => {
  const statusBody = h("dl", { class: "kv" });
  const update = h("div");
  const events = h("div", { class: "events" });
  const online = h("div", { class: "online" });
  const onlineCount = h("span", { class: "muted" });
  const playerCard = h("div", { class: "card" }, h("h3", {}, "Online now ", onlineCount), online);
  const cpu = meter("CPU"), mem = meter("Memory");
  const con = consolePanel({ compact: true });
  let evSeq = 0;
  let selected = null;      // player whose actions are open
  let ops = new Set();
  let shown = "";           // online list last rendered, to keep head icons from flickering

  const gb = (n) => n < 1024 ** 3 ? Math.round(n / 1024 ** 2) + " MB" : (n / 1024 ** 3).toFixed(n >= 10 * 1024 ** 3 ? 0 : 1) + " GB";
  const renderMeters = (s) => {
    const r = s.resources;
    if (!r) {
      const idle = s.state === "starting" ? "starting…" : "server stopped";
      cpu.set(0, "—", idle); mem.set(0, "—", idle);
      return;
    }
    cpu.set(r.cpu_percent, r.cpu_percent === null ? "…" : `${Math.round(r.cpu_percent)}%`,
      `of ${r.cpus} CPU core${r.cpus === 1 ? "" : "s"}`);
    mem.set(100 * r.memory_bytes / r.memory_max_bytes, `${gb(r.memory_bytes)} / ${gb(r.memory_max_bytes)}`,
      "used / allowed" + (r.system_memory_bytes ? ` · this computer has ${gb(r.system_memory_bytes)}` : ""));
  };

  const run = async (action, name) => {
    const CONFIRM = { kick: `Kick ${name}?`, ban: `Ban ${name}? They won't be able to join until pardoned.`,
      op: `Make ${name} an operator? Operators can run any command, including /stop and /op.` };
    if (CONFIRM[action] && !confirm(CONFIRM[action])) return;
    const r = await act(() => api("/api/players/action", { method: "POST", body: { action, name } }));
    if (r) { toast(r.message); setTimeout(loadPlayers, 800); }
  };
  const message = async (name) => {
    const text = prompt(`Message to ${name}:`);
    if (!text || !text.trim()) return;
    await act(() => api("/api/command", { method: "POST", body: { command: `tell ${name} ${text.trim().replace(/\s+/g, " ")}` } }), `Sent to ${name}`);
    con.poll();
  };
  const renderOnline = (names, max, force = false) => {
    onlineCount.textContent = `${names.length} / ${max}`;
    const key = names.join(",") + "|" + selected + "|" + [...ops].join(",");
    if (key === shown && !force) return;
    shown = key;
    if (selected && !names.includes(selected)) selected = null;
    if (!names.length) { fill(online, h("p", { class: "empty" }, "Nobody online right now.")); return; }
    fill(online,
      h("div", { class: "chips" }, names.map((n) => h("button", {
        type: "button", class: "chip" + (n === selected ? " selected" : ""), title: `Manage ${n}`,
        "aria-expanded": n === selected ? "true" : "false",
        onclick: () => { selected = selected === n ? null : n; renderOnline(names, max, true); },
      }, playerHead(n, 28), h("span", {}, n), ops.has(n.toLowerCase()) ? h("span", { class: "badge" }, "op") : null))),
      selected ? h("div", { class: "row mt-s player-actions" },
        h("strong", { class: "grow" }, selected),
        h("button", { class: "btn small", onclick: () => message(selected) }, "Message"),
        ops.has(selected.toLowerCase())
          ? h("button", { class: "btn small", onclick: () => run("deop", selected) }, "Remove op")
          : h("button", { class: "btn small", onclick: () => run("op", selected) }, "Make op"),
        h("button", { class: "btn small", onclick: () => run("kick", selected) }, "Kick"),
        h("button", { class: "btn small danger", onclick: () => run("ban", selected) }, "Ban"),
        h("a", { class: "btn small ghost", href: "#players" }, "More…")) : null);
  };
  const loadPlayers = async () => {
    const r = await api("/api/players").catch(() => null);
    if (!r) return;
    ops = new Set(r.ops.map((o) => (o.name || "").toLowerCase()));
    if (status) renderOnline(status.players, status.max_players);
  };

  const render = (s) => {
    renderMeters(s);
    renderOnline(s.players, s.max_players);
    fill(statusBody,
      h("dt", {}, "State"), h("dd", {}, h("span", { class: "pill " + s.state }, s.state)),
      h("dt", {}, "Uptime"), h("dd", {}, s.uptime ? fmtDuration(s.uptime) : "—"),
      h("dt", {}, "Minecraft"), h("dd", {}, s.minecraft || "not installed"),
      h("dt", {}, "Loader"), h("dd", {}, `${s.loader} ${s.loader_version || ""}`),
      h("dt", {}, "Java"), h("dd", {}, s.java_major ? `Java ${s.java_major}${s.java_forced ? ` (forced ${s.java_forced})` : ""}` : "—"),
      h("dt", {}, "Mods"), h("dd", {}, String(s.mods)),
      h("dt", {}, "Port"), h("dd", {}, s.port),
    );
    const u = s.update;
    fill(update,
      !u ? h("p", { class: "empty" }, "Not checked yet.")
        : u.up_to_date && u.latest !== s.minecraft
          ? h("div", { class: "notice warn" }, `On the newest version your mods support. Minecraft ${u.latest} is out; waiting on mods (see Updates).`)
        : u.up_to_date ? h("div", { class: "notice ok" }, `Up to date on the latest release (${u.latest}).`)
        : u.target ? h("div", { class: "notice warn" }, `Update ready: Minecraft ${u.target}`,
            u.manual ? h("div", { class: "small" }, `${u.manual} manual download(s) needed`) : null)
        : h("div", { class: "notice bad" }, `Minecraft ${u.latest} is out but nothing installable yet.`),
      u ? h("p", { class: "muted small" }, `Checked ${ago(u.checked_at)} · strategy ${s.strategy} · auto-upgrade ${s.auto_upgrade ? "on" : "off"}`) : null,
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: () => act(() => api("/api/updates/check", { method: "POST", body: {} }), "Checking for updates…") }, "Check now"),
        h("a", { href: "#updates", class: "btn ghost" }, "Details →")),
    );
  };

  const pollEvents = async () => {
    const r = await api(`/api/events?since=${evSeq}`).catch(() => null);
    if (!r) return;
    evSeq = r.last;
    for (const e of r.events) {
      events.prepend(h("div", { class: "ev " + e.level }, h("time", {}, fmtClock(e.time)), h("span", {}, e.message)));
    }
    while (events.childElementCount > 200) events.lastChild.remove();
  };

  fill($("#main"),
    h("h2", { class: "view-title" }, "Dashboard"),
    h("div", { class: "meters" }, cpu.el, mem.el),
    h("div", { class: "mt" }, playerCard),
    h("div", { class: "card mt" }, h("h3", {}, "Console"), con.el),
    h("div", { class: "grid mt" }, card("Server", statusBody), card("Updates", update)),
    h("div", { class: "card mt" }, h("h3", {}, "Activity"), events),
  );
  if (status) render(status);
  every(3000, pollEvents);
  every(1000, con.poll);
  every(5000, loadPlayers);
  return { onStatus: render };
};

views.console = () => {
  const con = consolePanel();
  fill($("#main"), con.el);
  con.input.focus();
  every(1000, con.poll);
  return {};
};

views.updates = () => {
  const body = h("div");
  const load = async () => {
    const r = await api("/api/updates").catch(() => null);
    if (!r) return;
    const c = r.check;
    const s = status || {};
    const checkBtn = h("button", { class: "btn", onclick: () => act(() => api("/api/updates/check", { method: "POST", body: {} }), "Checking…") }, "Check now");
    if (!c) {
      fill(body, card(null, h("p", {}, "No update check has run yet."), checkBtn));
      return;
    }
    const applyBtn = h("button", {
      class: "btn primary", disabled: c.up_to_date || !c.target || c.manual.length > 0,
      onclick: () => {
        const running = s.state === "running";
        if (confirm(`Update to Minecraft ${c.target}?` + (running ? "\n\nPlayers get an in-game countdown, then the server restarts. A backup is made first and it rolls back automatically if the new version fails to start." : "")))
          act(() => api("/api/updates/apply", { method: "POST", body: { target: c.target } }), "Update started");
      },
    }, c.installed ? "Apply update" : "Install server");

    const summary = c.up_to_date
      ? h("div", { class: c.latest === c.installed ? "notice ok" : "notice warn" },
          c.latest === c.installed ? `Up to date on the latest release, Minecraft ${c.installed}.`
            : `Up to date on Minecraft ${c.installed}, the newest version your mods support. ${c.latest} is blocked; see below.`)
      : c.target
        ? h("div", { class: "notice warn" }, h("strong", {}, `Ready: Minecraft ${c.installed || "(new install)"} → ${c.target}`),
            c.loader_version ? h("span", { class: "muted" }, `  (loader ${c.loader_version})`) : null)
        : h("div", { class: "notice bad" }, "No installable combination of Minecraft, loader and required mods was found.");

    const manual = c.manual.length ? card("Manual downloads needed",
      h("p", { class: "muted" }, "These mod authors don't allow automatic downloads. Download each file from its link, then upload it here (or copy it into the manual-downloads folder)."),
      h("ul", { class: "list" }, c.manual.map((m) => {
        const file = h("input", { type: "file", accept: ".jar", class: "hidden" });
        file.addEventListener("change", async () => {
          const f = file.files[0];
          if (!f) return;
          if (f.name !== m.filename && !confirm(`The selected file is "${f.name}", expected "${m.filename}". Upload anyway?`)) return;
          await act(() => api(`/api/manual/upload?filename=${encodeURIComponent(m.filename)}`, { method: "POST", raw: f }), `Uploaded ${m.filename}`);
          load();
        });
        return h("li", {},
          h("div", { class: "grow" }, h("div", {}, h("strong", {}, m.name)), h("code", {}, m.filename)),
          h("a", { class: "btn", href: m.url, target: "_blank", rel: "noopener noreferrer" }, "Download ↗"),
          h("button", { class: "btn primary", onclick: () => file.click() }, "Upload"), file);
      }))) : null;

    const changes = c.changes.length ? card("Changes", h("ul", { class: "list" }, c.changes.map((line) =>
      h("li", { class: line.startsWith("+") ? "change-add" : line.startsWith("-") ? "change-rm" : "" }, line)))) : null;

    const blocked = c.blocked.length ? card("Newer versions that are blocked",
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Minecraft"), h("th", {}, "Waiting on"))),
        h("tbody", {}, c.blocked.map((b) => h("tr", {},
          h("td", {}, b.minecraft),
          h("td", {}, h("ul", { class: "list" },
            b.loader_missing ? h("li", {}, "Loader has no build for this version yet") : null,
            b.blockers.map((x) => h("li", {}, h("div", {}, h("strong", {}, x.name), h("div", { class: "muted small" }, x.reason)))))),
        ))))) : null;

    const dropped = c.dropped.length ? card("Left out (optional or client-only)",
      h("ul", { class: "list" }, c.dropped.map((x) => h("li", {}, h("div", {}, h("strong", {}, x.name), h("div", { class: "muted small" }, x.reason)))))) : null;

    fill(body, 
      summary,
      h("div", { class: "row mb" }, applyBtn, checkBtn, h("span", { class: "muted small" }, `Last checked ${ago(c.checked_at)}`)),
      manual, changes, blocked, dropped);
  };
  fill($("#main"), h("h2", { class: "view-title" }, "Updates"), body);
  load();
  return { onJobDone: load };
};

views.players = () => {
  const body = h("div");
  const name = h("input", { placeholder: "Player name", autocomplete: "off", maxlength: 16 });
  const reason = h("input", { placeholder: "Reason (optional, shown when kicked/banned)", maxlength: 200 });
  let data = null;

  const CONFIRM = {
    kick: (n) => `Kick ${n}?`, ban: (n) => `Ban ${n}? They won't be able to join until pardoned.`,
    "ban-ip": (n) => `Ban the IP address of ${n}? Everyone on that connection will be blocked.`,
    op: (n) => `Make ${n} an operator? Operators can run any command, including /stop and /op.`,
  };
  const run = async (action, target, why) => {
    if (CONFIRM[action] && !confirm(CONFIRM[action](target))) return;
    const r = await act(() => api("/api/players/action", { method: "POST", body: { action, name: target, reason: why || reason.value } }));
    if (r) { toast(r.message); setTimeout(load, 800); }
  };
  const btn = (label, action, target, cls = "") => h("button", { class: `btn small ${cls}`, onclick: () => run(action, target) }, label);

  const load = async () => {
    const r = await api("/api/players").catch(() => null);
    if (!r) return;
    data = r;
    const lc = (list) => new Set(list.map((x) => (x.name || "").toLowerCase()));
    const ops = lc(r.ops), wl = lc(r.whitelist), banned = lc(r.bans);
    const actionsFor = (n) => {
      const k = n.toLowerCase();
      return h("div", { class: "row" },
        r.running && r.online.includes(n) ? btn("Kick", "kick", n) : null,
        ops.has(k) ? btn("De-op", "deop", n) : btn("Op", "op", n),
        r.whitelist_enabled || wl.has(k) ? (wl.has(k) ? btn("Un-whitelist", "whitelist-remove", n) : btn("Whitelist", "whitelist-add", n)) : null,
        banned.has(k) ? btn("Pardon", "pardon", n) : btn("Ban", "ban", n, "danger"),
        r.running && r.online.includes(n) ? btn("Ban IP", "ban-ip", n, "danger") : null);
    };
    const tags = (n) => {
      const k = n.toLowerCase();
      return [ops.has(k) ? h("span", { class: "tag ok" }, "op") : null, wl.has(k) ? h("span", { class: "tag" }, "whitelisted") : null,
              banned.has(k) ? h("span", { class: "tag bad" }, "banned") : null];
    };
    const playerList = (names, empty) => names.length
      ? h("ul", { class: "list" }, names.map((n) => h("li", {}, h("div", { class: "grow" }, h("strong", {}, n), tags(n)), actionsFor(n))))
      : h("p", { class: "empty" }, empty);

    const onlineSet = new Set(r.online.map((n) => n.toLowerCase()));
    const known = r.known.map((k) => k.name).filter((n) => n && !onlineSet.has(n.toLowerCase()));

    fill(body,
      r.running ? null : h("div", { class: "notice" }, "The server is stopped. Changes are written to its player files and apply when it starts. Kicking needs the server running."),
      !r.online_mode ? h("div", { class: "notice warn" }, "online-mode is off: anyone can join with any name, so bans and the whitelist only match names, not accounts.") : null,
      h("div", { class: "grid mt" },
        card(`Online now (${r.online.length})`, playerList(r.online, r.running ? "Nobody online" : "Server is stopped")),
        card(`Operators (${r.ops.length})`, r.ops.length ? h("ul", { class: "list" }, r.ops.map((o) => h("li", {},
          h("div", { class: "grow" }, h("strong", {}, o.name), h("span", { class: "tag" }, `level ${o.level}`)), btn("De-op", "deop", o.name)))) : h("p", { class: "empty" }, "No operators"))),
      h("div", { class: "grid mt" },
        card("Whitelist",
          h("div", { class: "row mb" },
            h("span", { class: "grow" }, "Whitelist is ", h("strong", {}, r.whitelist_enabled ? "on" : "off"),
              h("span", { class: "muted small" }, r.whitelist_enabled ? ": only listed players can join" : ": anyone can join")),
            h("button", { class: "btn small", onclick: () => run(r.whitelist_enabled ? "whitelist-off" : "whitelist-on", "") }, r.whitelist_enabled ? "Turn off" : "Turn on")),
          r.whitelist.length ? h("ul", { class: "list" }, r.whitelist.map((w) => h("li", {}, h("div", { class: "grow" }, w.name), btn("Remove", "whitelist-remove", w.name))))
            : h("p", { class: "empty" }, "Nobody whitelisted")),
        card("Banned",
          r.bans.length || r.ip_bans.length ? h("ul", { class: "list" },
            r.bans.map((b) => h("li", {}, h("div", { class: "grow" }, h("strong", {}, b.name),
              h("div", { class: "muted small" }, [b.reason, b.created && `since ${b.created}`].filter(Boolean).join(" · "))), btn("Pardon", "pardon", b.name))),
            r.ip_bans.map((b) => h("li", {}, h("div", { class: "grow" }, h("code", {}, b.ip),
              h("div", { class: "muted small" }, [b.reason, b.created && `since ${b.created}`].filter(Boolean).join(" · "))), btn("Pardon", "pardon-ip", b.ip))))
            : h("p", { class: "empty" }, "Nobody banned"))),
      h("div", { class: "mt" }, card("Players who have joined before", playerList(known, "Nobody else has joined yet"))),
    );
  };

  // Built once, outside the refreshed area, so typing isn't interrupted.
  const manage = card("Add or manage a player",
        h("div", { class: "row" }, name, reason),
        h("div", { class: "row mt-s" },
          ...[["Op", "op"], ["De-op", "deop"], ["Whitelist", "whitelist-add"], ["Un-whitelist", "whitelist-remove"], ["Kick", "kick"], ["Pardon", "pardon"]]
            .map(([label, action]) => h("button", { class: "btn", onclick: () => name.value.trim() && run(action, name.value.trim()) }, label)),
          h("button", { class: "btn danger", onclick: () => name.value.trim() && run("ban", name.value.trim()) }, "Ban"),
          h("button", { class: "btn danger", title: "Enter an IP address, or the name of an online player",
                        onclick: () => name.value.trim() && run("ban-ip", name.value.trim()) }, "Ban IP")));
  fill($("#main"), h("h2", { class: "view-title" }, "Players"), manage, h("div", { class: "mt" }, body));
  load();
  every(5000, load);
  return {};
};

views.mods = () => {
  const results = h("div");
  const configured = h("div");
  const installed = h("div");
  const q = h("input", { placeholder: "Search Modrinth for server mods…", type: "search" });
  let searchTimer;

  const search = async () => {
    const term = q.value.trim();
    if (!term) { fill(results, ); return; }
    fill(results, h("p", { class: "empty" }, "Searching…"));
    const r = await api(`/api/mods/search?q=${encodeURIComponent(term)}`).catch((e) => { toast(e.message, true); return null; });
    if (!r) return;
    fill(results, ...(r.results.length ? r.results.map((m) => h("div", { class: "mod" },
      m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
      h("div", { class: "info" },
        h("div", { class: "name" }, m.name, h("span", { class: "tag" }, `${(m.downloads / 1e6).toFixed(1)}M downloads`),
          m.server_side === "optional" ? h("span", { class: "tag" }, "server optional") : null),
        h("div", { class: "desc" }, m.description)),
      m.listed ? h("span", { class: "tag ok" }, "added") : h("div", { class: "row" },
        h("button", { class: "btn primary small", onclick: () => add(m.slug, true) }, "Add"),
        h("button", { class: "btn small", title: "Won't hold back Minecraft upgrades", onclick: () => add(m.slug, false) }, "Add optional")),
    )) : [h("p", { class: "empty" }, "No server-compatible mods found.")]));
  };
  const add = async (id, required, source = "modrinth") => {
    const r = await act(() => api("/api/mods/add", { method: "POST", body: { source, id, required } }));
    if (r) { toast(`Added ${r.name}. Run an update check to install it.`); load(); search(); }
  };
  q.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 350); });

  const cfId = h("input", { placeholder: "CurseForge project id or slug" });
  const load = async () => {
    const r = await api("/api/mods").catch(() => null);
    if (!r) return;
    fill(configured, r.configured.length ? h("ul", { class: "list" }, r.configured.map((s) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, s.id), h("span", { class: "tag" }, s.source)),
      h("label", { class: "row", title: "Required mods hold back Minecraft upgrades until they support the new version" },
        h("input", { type: "checkbox", checked: s.required, onchange: (e) => act(() => api("/api/mods/required", { method: "POST", body: { source: s.source, id: s.id, required: e.target.checked } })) }),
        "required"),
      h("button", { class: "btn danger small", onclick: () => confirm(`Remove ${s.id}? It is uninstalled at the next update.`) && act(() => api("/api/mods/remove", { method: "POST", body: { source: s.source, id: s.id } }), `Removed ${s.id}`).then(load) }, "Remove"),
    ))) : h("p", { class: "empty" }, "No mods configured."));

    fill(installed, 
      r.installed.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Mod"), h("th", {}, "Version"), h("th", {}, "File"))),
        h("tbody", {}, r.installed.map((m) => h("tr", {},
          h("td", {}, m.name, m.dependency_of ? h("span", { class: "tag" }, "dependency") : null, m.manual ? h("span", { class: "tag warn" }, "manual") : null),
          h("td", {}, m.version), h("td", {}, h("code", {}, m.filename)))))) : h("p", { class: "empty" }, "Nothing installed yet."),
      r.skipped.length ? h("div", { class: "notice warn mt-s" }, h("strong", {}, "Not installed: "),
        r.skipped.map((x) => h("div", { class: "small" }, `${x.key}: ${x.reason}`))) : null,
      r.unmanaged.length ? h("div", { class: "notice mt-s" }, h("strong", {}, "Unmanaged jars (not updated by mcsm): "),
        r.unmanaged.map((x) => h("div", {}, h("code", {}, x)))) : null,
    );
  };

  fill($("#main"), 
    h("h2", { class: "view-title" }, "Mods"),
    card("Add mods", q, results,
      h("div", { class: "row mt-s" }, cfId,
        h("button", { class: "btn", onclick: () => cfId.value.trim() && add(cfId.value.trim(), true, "curseforge") }, "Add from CurseForge"))),
    h("div", { class: "grid mt" }, card("Configured (mcsm.toml)", configured), card("Installed", installed)),
  );
  load();
  return { onJobDone: load };
};

views.backups = () => {
  const list = h("div");
  const label = h("input", { placeholder: "label (optional)" });
  const load = async () => {
    const r = await api("/api/backups").catch(() => null);
    if (!r) return;
    const stopped = status && status.state === "stopped";
    fill(list, r.backups.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Backup"), h("th", {}, "Created"), h("th", {}, "Size"), h("th", {}))),
      h("tbody", {}, r.backups.map((b) => h("tr", {},
        h("td", {}, h("code", {}, b.name)), h("td", {}, fmtTime(b.time)), h("td", {}, fmtBytes(b.size)),
        h("td", {}, h("button", {
          class: "btn small", disabled: !stopped, title: stopped ? "" : "Stop the server first",
          onclick: () => confirm(`Replace the server directory with ${b.name}? Anything since then is lost.`) &&
            act(() => api("/api/backups/restore", { method: "POST", body: { name: b.name } }), "Restoring…"),
        }, "Restore")))))) : h("p", { class: "empty" }, "No backups yet."));
  };
  fill($("#main"), 
    h("h2", { class: "view-title" }, "Backups"),
    card("Create backup", h("p", { class: "muted" }, "A backup is also made automatically before every update."),
      h("div", { class: "row" }, label, h("button", { class: "btn primary", onclick: () => act(() => api("/api/backups/create", { method: "POST", body: { label: label.value } }), "Backing up…") }, "Back up now"))),
    card("Backups", h("p", { class: "muted small" }, "Restoring needs the server to be stopped."), list),
  );
  load();
  return { onJobDone: load, onStatus: load };
};

views.java = () => {
  const body = h("div");
  const load = async () => {
    const r = await api("/api/java").catch(() => null);
    if (!r) return;
    const use = h("select", {}, h("option", { value: "auto" }, "auto (what Minecraft needs)"),
      [8, 11, 16, 17, 21, 25].map((v) => h("option", { value: String(v) }, `Java ${v}`)));
    use.value = r.forced ? String(r.forced) : "auto";
    const major = h("input", { type: "number", min: 8, max: 99, value: r.required || 21, class: "narrow" });
    fill(body, 
      h("div", { class: "grid" },
        card("Server runtime", h("dl", { class: "kv" },
          h("dt", {}, "Needs"), h("dd", {}, r.required ? `Java ${r.required}` : "—"),
          h("dt", {}, "Using"), h("dd", {}, r.current ? h("code", {}, r.current) : "—"),
          h("dt", {}, "Auto-install"), h("dd", {}, r.auto_install ? "on" : "off")),
          h("label", { class: "mt-s" }, "Run the server on",
            h("div", { class: "row" }, use, h("button", { class: "btn primary", onclick: () => act(() => api("/api/java/use", { method: "POST", body: { version: use.value } }), "Saved. Applies at the next start.").then(load) }, "Save")))),
        card("Download Temurin", h("p", { class: "muted" }, "Downloads Eclipse Temurin into .mcsm/java/."),
          h("div", { class: "row" }, major, h("button", { class: "btn", onclick: () => act(() => api("/api/java/install", { method: "POST", body: { major: Number(major.value) } }), "Downloading…") }, "Install"))),
      ),
      card("Available runtimes", h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Java"), h("th", {}, "Source"), h("th", {}, "Path"))),
        h("tbody", {},
          r.managed.map((j) => h("tr", {}, h("td", {}, String(j.major)), h("td", {}, "managed ", h("span", { class: "tag" }, j.release)), h("td", {}, h("code", {}, j.path)))),
          r.configured.map((j) => h("tr", {}, h("td", {}, String(j.major)), h("td", {}, "[java.versions]"), h("td", {}, h("code", {}, j.path)))),
          h("tr", {}, h("td", {}, "?"), h("td", {}, "default"), h("td", {}, h("code", {}, r.default)))))),
    );
  };
  fill($("#main"), h("h2", { class: "view-title" }, "Java"), body);
  load();
  return { onJobDone: load };
};

views.settings = () => {
  const form = h("form", { class: "card" });
  const load = async () => {
    const s = await api("/api/settings").catch(() => null);
    if (!s) return;
    const f = {};
    const sel = (k, opts) => (f[k] = h("select", {}, opts.map((o) => h("option", { value: o }, o))), f[k].value = s[k], f[k]);
    const txt = (k, extra = {}) => (f[k] = h("input", { value: s[k], ...extra }));
    const chk = (k, text) => h("label", { class: "row" }, (f[k] = h("input", { type: "checkbox", checked: s[k] })), h("span", {}, text));
    fill(form, 
      h("h3", {}, "Updates"),
      h("div", { class: "grid" },
        h("label", {}, "Strategy", sel("strategy", s.choices.strategy)),
        h("label", {}, "Lowest mod release channel", sel("mod_channel", s.choices.mod_channel)),
        h("label", {}, "Check every (e.g. 6h, 30m)", txt("check_interval")),
        h("label", {}, "In-game warnings (minutes, comma separated)", txt("warn_minutes", { value: s.warn_minutes.join(", ") }))),
      h("div", { class: "grid mt-s" },
        chk("auto_upgrade", "Apply updates automatically"),
        chk("wait_for_empty", "Wait until nobody is online"),
        chk("verify_boot", "Test-boot and roll back on failure")),
      h("h3", { class: "mt-l" }, "Server"),
      h("div", { class: "grid" },
        h("label", {}, "Memory (e.g. 6G)", txt("memory")),
        h("label", {}, "Backups to keep", txt("backups_keep", { type: "number", min: 1 })),
        h("label", {}, "Discord webhook URL", txt("discord_webhook", { type: "url", placeholder: "https://discord.com/api/webhooks/…" }))),
      h("div", { class: "grid mt-s" }, chk("restart_on_crash", "Restart after crashes")),
      h("div", { class: "row mt" }, h("button", { class: "btn primary", type: "submit" }, "Save settings"),
        h("span", { class: "muted small" }, "Memory changes apply at the next restart.")),
    );
    form.onsubmit = (e) => {
      e.preventDefault();
      const body = {
        strategy: f.strategy.value, mod_channel: f.mod_channel.value, check_interval: f.check_interval.value.trim(),
        warn_minutes: f.warn_minutes.value.split(",").map((x) => x.trim()).filter(Boolean).map(Number),
        auto_upgrade: f.auto_upgrade.checked, wait_for_empty: f.wait_for_empty.checked, verify_boot: f.verify_boot.checked,
        memory: f.memory.value.trim(), backups_keep: Number(f.backups_keep.value), discord_webhook: f.discord_webhook.value.trim(),
        restart_on_crash: f.restart_on_crash.checked,
      };
      act(() => api("/api/settings", { method: "POST", body }), "Settings saved").then(load);
    };
  };
  const about = h("div", { class: "mt" });
  const loadAbout = async () => {
    const [n, lic] = await Promise.all([api("/api/notice").catch(() => null), api("/api/licenses").catch(() => null)]);
    if (!n || !lic) return;
    const s = status || {};
    const row = (x) => h("tr", {}, h("td", {}, x.name), h("td", {}, x.license), h("td", { class: "muted" }, x.use),
      h("td", {}, h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "↗")));
    const table = (title, rows) => [h("h3", { class: "mt-l" }, title), h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "License"), h("th", {}, "Used for"), h("th", {}))),
      h("tbody", {}, rows.map(row)))];
    fill(about,
      card("About mcsm",
        h("dl", { class: "kv" },
          h("dt", {}, "Version"), h("dd", {}, s.version || ""),
          h("dt", {}, "License"), h("dd", {}, h("a", { href: lic.project.url, target: "_blank", rel: "noopener noreferrer" }, lic.project.license))),
        h("div", { class: "row mt-s" },
          h("button", { class: "btn", onclick: async () => {
            try { localStorage.removeItem(DISMISS_KEY); } catch (_) {}
            closeToast("self-update");
            await act(() => api("/api/self-update/check", { method: "POST", body: {} }), "Checking for a new mcsm version…");
          } }, "Check for mcsm updates"))),
      h("div", { class: "mt" }, card("What mcsm does and doesn't do",
        h("ul", { class: "notice-points" }, n.points.map((p) => h("li", {}, p))))),
      h("div", { class: "mt" }, card("Open-source licenses",
        h("p", { class: "muted" }, "mcsm has no third-party runtime dependencies, and the web UI uses no third-party code, fonts or images. Software it downloads for you is never bundled or redistributed by mcsm."),
        table("Used by mcsm", lic.runtime),
        table("Bundled into the downloadable executables", lic.bundled),
        table("Used only for development", lic.development),
        table("Downloaded for you", lic.downloaded),
        h("h3", { class: "mt-l" }, "Online services"),
        h("ul", { class: "list" }, lic.services.map((x) => h("li", {}, h("span", { class: "grow" }, x.name),
          x.url.startsWith("http") ? h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "terms ↗") : h("span", { class: "muted small" }, x.url)))))),
    );
  };
  const security = h("div", { class: "mb" });
  const renderSecurity = () => {
    const a = (status && status.auth) || {};
    const label = { password: "Password", pin: "PIN", none: "No password (this computer only)" }[a.mode] || "…";
    fill(security, card("Sign-in",
      h("div", { class: "row" },
        h("span", { class: "grow" }, a.managed ? "Password set in mcsm.toml ([web] password)" : a.default ? "Default password (PASSWORD) — please change it" : label),
        a.managed ? null : h("button", { class: "btn", onclick: () => showSecurity(false) }, "Change"))));
  };
  renderSecurity();
  fill($("#main"), h("h2", { class: "view-title" }, "Settings"), security, form, about);
  load();
  loadAbout();
  return { onStatus: renderSecurity };
};

// ------------------------------------------------------------------- setup
// Kept outside the view so choices survive re-renders and a failed attempt.
const setupState = { loader: "fabric", minecraft: "latest", mods: new Map(), motd: "A Minecraft server",
  max_players: 20, difficulty: "normal", gamemode: "survival", port: 25565, memory_gb: null,
  network_access: null, accept_eula: false, submitted: false, prefilled: false };

views.setup = () => {
  const main = h("div", { class: "setup" });
  let opts = null;
  const st = setupState;

  const field = (label, input, hint) => h("label", {}, label, input, hint ? h("span", { class: "muted small" }, hint) : null);

  const renderForm = (error) => {
    const loaderCards = h("div", { class: "choices" }, opts.loaders.map((l) => h("button", {
      type: "button", class: "choice" + (st.loader === l.name ? " selected" : ""),
      onclick: () => { st.loader = l.name; if (!l.mods) st.mods.clear(); renderForm(); },
    }, h("strong", {}, l.label), h("span", { class: "small muted" }, l.description))));

    const version = h("select", { onchange: (e) => { st.minecraft = e.target.value; } },
      h("option", { value: "latest" }, st.loader === "vanilla" ? "Newest release (recommended)" : "Newest version your mods support (recommended)"),
      opts.versions.map((v) => h("option", { value: v }, `Minecraft ${v}`)));
    version.value = st.minecraft;

    // Mods
    const results = h("div");
    const selected = h("div");
    const renderSelected = () => fill(selected, st.mods.size ? h("ul", { class: "list" }, [...st.mods].map(([slug, m]) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, m.name), h("span", { class: "tag" }, slug)),
      h("label", { class: "row", title: "Required mods hold back Minecraft upgrades until they support the new version" },
        h("input", { type: "checkbox", checked: m.required, onchange: (e) => { m.required = e.target.checked; } }), "required"),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.mods.delete(slug); renderSelected(); search(); } }, "Remove"))))
      : h("p", { class: "empty" }, "No mods yet. Search above, or leave empty for an unmodded server."));
    const q = h("input", { type: "search", placeholder: "Search Modrinth, e.g. lithium, create, farmer's delight" });
    let timer;
    const search = async () => {
      const term = q.value.trim();
      if (!term) { fill(results); return; }
      const r = await api(`/api/mods/search?loader=${encodeURIComponent(st.loader)}&q=${encodeURIComponent(term)}`).catch((e) => { toast(e.message, true); return null; });
      if (!r) return;
      fill(results, r.results.length ? r.results.slice(0, 8).map((m) => h("div", { class: "mod" },
        m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "info" }, h("div", { class: "name" }, m.name), h("div", { class: "desc" }, m.description)),
        st.mods.has(m.slug) ? h("span", { class: "tag ok" }, "added")
          : h("button", { type: "button", class: "btn small primary", onclick: () => { st.mods.set(m.slug, { name: m.name, required: true }); renderSelected(); search(); } }, "Add"),
      )) : [h("p", { class: "empty" }, `No ${st.loader} server mods found.`)]);
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });
    renderSelected();
    const modsCard = opts.loaders.find((l) => l.name === st.loader).mods ? card("3. Mods",
      q, results, h("h3", { class: "mt" }, "Your mods"), selected,
      st.loader === "fabric" || st.loader === "quilt" ? h("p", { class: "muted small" }, "Fabric API is added automatically, since almost every Fabric mod needs it.") : null) : null;

    // Settings
    const inp = (key, attrs = {}) => h("input", { value: st[key], ...attrs, oninput: (e) => { st[key] = attrs.type === "number" ? Number(e.target.value) : e.target.value; } });
    const sel = (key, choices) => { const el = h("select", { onchange: (e) => { st[key] = e.target.value; } }, choices.map((c) => h("option", { value: c }, c[0].toUpperCase() + c.slice(1)))); el.value = st[key]; return el; };
    const maxMem = Math.max(2, Math.floor(opts.total_ram_gb || 16));
    const mem = h("select", { onchange: (e) => { st.memory_gb = Number(e.target.value); } },
      Array.from({ length: Math.min(maxMem, 32) }, (_, i) => i + 1).map((g) => h("option", { value: String(g) }, `${g} GB${g === opts.memory_gb ? " (suggested)" : ""}`)));
    mem.value = String(st.memory_gb);

    const eula = h("input", { type: "checkbox", checked: st.accept_eula, onchange: (e) => { st.accept_eula = e.target.checked; } });
    const lan = h("input", { type: "checkbox", checked: st.network_access, onchange: (e) => { st.network_access = e.target.checked; } });

    const submit = async (e) => {
      e.preventDefault();
      if (!st.accept_eula) { toast("Please read and accept the Minecraft EULA first.", true); return; }
      const mods = [...st.mods].filter(([, m]) => m.required).map(([slug]) => slug);
      const optional = [...st.mods].filter(([, m]) => !m.required).map(([slug]) => slug);
      const body = { loader: st.loader, minecraft: st.minecraft, mods, optional_mods: optional, memory_gb: st.memory_gb,
        motd: st.motd, max_players: st.max_players, difficulty: st.difficulty, gamemode: st.gamemode, port: st.port,
        network_access: st.network_access, accept_eula: true };
      const r = await act(() => api("/api/setup", { method: "POST", body }));
      if (r) { st.submitted = true; renderProgress(); }
    };

    fill(main,
      h("h2", { class: "view-title" }, "Set up your server"),
      h("p", { class: "muted" }, "Choose what kind of server you want. mcsm downloads everything it needs (Minecraft, the mod loader, mods and Java), starts it, and keeps it up to date from then on."),
      error ? h("div", { class: "notice bad" }, h("strong", {}, "Setup didn't finish: "), error, h("div", { class: "small mt-s" }, "Change your choices below and try again.")) : null,
      h("form", { onsubmit: submit },
        card("1. Server type", loaderCards),
        h("div", { class: "mt" }, card("2. Minecraft version", field("Version", version,
          "\"Newest\" picks the newest Minecraft that all your required mods work on, and keeps upgrading as they catch up: a forever server. " +
          "Picking a specific version keeps the server on that version (mods still update); you can change this later in Settings."))),
        modsCard ? h("div", { class: "mt" }, modsCard) : null,
        h("div", { class: "mt" }, card(modsCard ? "4. Settings" : "3. Settings",
          h("div", { class: "grid" },
            field("Server name (shown in the server list)", inp("motd", { maxlength: 59 })),
            field("Max players", inp("max_players", { type: "number", min: 1, max: 1000 })),
            field("Difficulty", sel("difficulty", opts.difficulties)),
            field("Game mode", sel("gamemode", opts.gamemodes)),
            field("Memory", mem, opts.total_ram_gb ? `This computer has ${opts.total_ram_gb} GB.` : null),
            field("Port", inp("port", { type: "number", min: 1024, max: 65535 }), "25565 is Minecraft's usual port.")),
          h("label", { class: "row mt" }, lan, h("span", {}, "Let other devices on my network (like my phone) open this control panel")))),
        h("div", { class: "mt" }, card("Almost done",
          h("label", { class: "row" }, eula, h("span", {}, "I accept the ",
            h("a", { href: "https://aka.ms/MinecraftEULA", target: "_blank", rel: "noopener noreferrer" }, "Minecraft EULA ↗"),
            ", which every Minecraft server must follow.")),
          h("p", { class: "muted small" }, `Your server will be created in ${opts.server_dir}`),
          h("button", { type: "submit", class: "btn primary big" }, "Create my server")))));
  };

  const renderProgress = () => {
    const events = h("div", { class: "events" });
    let seq = 0;
    fill(main,
      h("h2", { class: "view-title" }, "Creating your server…"),
      h("div", { class: "notice" }, h("div", { class: "row" }, h("span", { class: "spinner" }),
        h("span", { class: "grow" }, "Downloading Java, the mod loader, Minecraft and your mods, then starting the server for the first time. This usually takes a few minutes."))),
      card("What's happening", events));
    every(1500, async () => {
      const r = await api(`/api/events?since=${seq}`).catch(() => null);
      if (!r) return;
      seq = r.last;
      for (const e of r.events) events.prepend(h("div", { class: "ev " + e.level }, h("time", {}, fmtClock(e.time)), h("span", {}, e.message)));
    });
  };

  (async () => {
    opts = await api("/api/setup").catch((e) => { toast(e.message, true); return null; });
    if (!opts) return;
    if (!st.prefilled && opts.current) {  // an existing mcsm.toml: start from its choices
      st.prefilled = true;
      const c = opts.current;
      if (opts.loaders.some((l) => l.name === c.loader)) st.loader = c.loader;
      st.minecraft = c.minecraft;
      for (const m of c.mods) st.mods.set(m.slug, { name: m.slug, required: m.required });
      if (c.memory_gb) st.memory_gb = c.memory_gb;
    }
    if (st.memory_gb === null) st.memory_gb = opts.memory_gb;
    if (st.network_access === null) st.network_access = opts.network_access;
    if (opts.versions_error) toast(opts.versions_error, true);
    const s = status || {};
    if (s.job && s.job.name === "set up server") renderProgress();
    else renderForm();
  })();

  $("#main").replaceChildren(main);
  return {
    onJobDone: () => {
      const last = status && status.last_job;
      if (!last || last.name !== "set up server") return;
      if (last.ok) location.hash = "#dashboard";  // the job's own toast says it's ready
      else { st.submitted = false; clearTimers(); every(2000, refreshStatus); renderForm(last.message); }
    },
  };
};

// ------------------------------------------------------------------- router
let currentName = null;
function route() {
  const name = (location.hash || "#dashboard").slice(1);
  const view = views[name] ? name : "dashboard";
  currentName = view;
  clearTimers();
  every(2000, refreshStatus);
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  current = views[view]();
}
window.addEventListener("hashchange", () => { if (!$("#app").classList.contains("hidden")) route(); });

async function start() {
  try { await api("/api/status"); } catch (_) { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  route();
}
start();
