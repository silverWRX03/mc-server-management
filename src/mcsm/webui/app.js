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

// ------------------------------------------------------------------ day / night
// Remembered per browser; the first time, it follows the computer's own light/dark setting.
const THEME_KEY = "mcsm-theme";
function currentTheme() {
  try { const t = localStorage.getItem(THEME_KEY); if (t === "day" || t === "night") return t; } catch (_) { /* private mode */ }
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "day" : "night";
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  for (const b of document.querySelectorAll("[data-theme-toggle]")) {
    b.setAttribute("aria-pressed", String(theme === "day"));
    b.title = theme === "day" ? "Switch to night" : "Switch to day";
  }
}
applyTheme(currentTheme());
document.addEventListener("click", (e) => {
  const b = e.target.closest && e.target.closest("[data-theme-toggle]");
  if (!b) return;
  const next = document.documentElement.dataset.theme === "day" ? "night" : "day";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* private mode */ }
  document.documentElement.classList.add("theme-switching");
  applyTheme(next);
  setTimeout(() => document.documentElement.classList.remove("theme-switching"), 700);
});

// With several servers, a server's calls go to /api/servers/<id>/...; these are about mcsm itself.
const GLOBAL_API = /^\/api\/(login|logout|auth|notice|licenses|self-update|hub|servers)(\/|\?|$)/;
let server = null;            // the server being looked at (null on the server list)
const scoped = (path) => server && path.startsWith("/api/") && !GLOBAL_API.test(path)
  ? `/api/servers/${server}/${path.slice(5)}` : path;
const link = (view) => `#s/${server}/${view}`;

async function api(path, { method = "GET", body, raw } = {}) {
  const headers = { "X-MCSM": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(scoped(path), {
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

// Uploads a big file with progress (fetch can't report upload progress).
function upload(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", scoped(path));
    xhr.setRequestHeader("X-MCSM", "1");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* empty */ }
      if (xhr.status === 401) { showLogin(); reject(new Unauthorized()); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(data.error || xhr.statusText));
    };
    xhr.onerror = () => reject(new Error("the upload failed; check the connection and try again"));
    xhr.send(file);
  });
}

// "Open folder" buttons: shown only in a browser on the server's own computer, where
// mcsm can open the file manager. ``sid`` picks a server other than the current one.
function folderBtn(what, label, sid, cls = "btn ghost small") {
  if (!hubInfo || !hubInfo.local) return null;
  const path = what === "home" ? "/api/hub/open" : sid ? `/api/servers/${encodeURIComponent(sid)}/open` : "/api/open";
  return h("button", { type: "button", class: cls, title: "Open in your file manager",
    onclick: () => api(path, { method: "POST", body: { what } }).catch((e) => { if (!(e instanceof Unauthorized)) toast(e.message, true); }) },
  "📂 ", label);
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
let status = null;            // the current server's status
let hubInfo = null;           // mcsm itself: servers, sign-in, notice, updates
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
  const reset = h("button", { type: "button", class: "link-btn", onclick: async () => {
    if (!confirm("Go back to the default password, PASSWORD? Anyone signed in elsewhere is signed out, and you'll choose a new one after signing in.")) return;
    try {
      await api("/api/auth/reset-local", { method: "POST", body: {} });
      toast("The password is PASSWORD again");
      showLogin();
    } catch (err) { $("#login-error").textContent = err.message; }
  } }, "Reset it to PASSWORD");
  $("#login-hint").replaceChildren(...(
    !a ? [] :
    a.mode === "none" ? ["This control panel has no password, so it only opens on the server's own computer. To use it from here, set a PIN or password there (mcsm settings → Sign-in)."] :
    a.managed ? ["The password is set in mcsm.toml under ", h("code", {}, "[web] password"), "."] :
    a.default ? ["First time? The password is ", h("strong", {}, "PASSWORD"), " (in capitals). You'll choose your own next."] :
    a.local ? ["Forgot it? ", reset, " (this works on the server's own computer)."] :
    ["Forgot it? On the server's own computer, open this page and choose \"Reset it to PASSWORD\", or run ",
      h("code", {}, "mcsm web-password --reset"), "."]));
  input.focus();
}

// A show/hide "eye" for password inputs.
function eyeToggle(input, button) {
  button.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.setAttribute("aria-pressed", String(show));
    button.setAttribute("aria-label", show ? "Hide password" : "Show password");
    button.title = show ? "Hide password" : "Show password";
    button.querySelector(".eye-open").classList.toggle("hidden", show);
    button.querySelector(".eye-shut").classList.toggle("hidden", !show);
    input.focus();
  });
}
eyeToggle($("#login-password"), $("#login-eye"));
function pwField(input) {
  const svg = (cls, d) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.setAttribute("class", cls);
    for (const part of d) {
      const el = document.createElementNS("http://www.w3.org/2000/svg", part.circle ? "circle" : "path");
      for (const [k, v] of Object.entries(part.circle || { d: part })) el.setAttribute(k, v);
      s.append(el);
    }
    return s;
  };
  const btn = h("button", { type: "button", class: "eye", "aria-label": "Show password", "aria-pressed": "false", title: "Show password" },
    svg("eye-open", ["M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12Z", { circle: { cx: 12, cy: 12, r: 3.2 } }]),
    svg("eye-shut hidden", ["M3 3l18 18M10.6 5.1A10.8 10.8 0 0 1 12 4.5C18.5 4.5 22.5 12 22.5 12a18 18 0 0 1-3.3 4.3M6.6 6.6C3.4 8.6 1.5 12 1.5 12S5.5 19.5 12 19.5a10 10 0 0 0 5.4-1.6M9.9 9.9a3.2 3.2 0 0 0 4.2 4.2"]));
  eyeToggle(input, btn);
  return h("div", { class: "pw-field" }, input, btn);
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
// There's no command window to close, so mcsm is quit from here.
$("#quit").addEventListener("click", async () => {
  const running = ((hubInfo && hubInfo.servers) || []).filter((s) => s.state === "running" || s.state === "starting");
  if (!confirm(running.length ? `Quit mcsm? ${running.map((s) => s.name).join(", ")} will be stopped (players get disconnected).`
    : "Quit mcsm? Open it again from its icon when you want it back.")) return;
  try { await api("/api/hub/quit", { method: "POST", body: {} }); } catch (e) { if (!(e instanceof Unauthorized)) { toast(e.message, true); return; } }
  clearTimers();
  document.body.replaceChildren(h("div", { class: "login" }, h("div", { class: "login-card" },
    h("div", { class: "brand big" }, h("span", { class: "logo" }), "mcsm"),
    h("p", {}, "mcsm is shutting down" + (running.length ? " and stopping your servers" : "") + "."),
    h("p", { class: "muted small" }, "You can close this tab. To use mcsm again, open it from its icon."))));
});

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
  let mode = hubInfo && hubInfo.auth && !hubInfo.auth.default ? hubInfo.auth.mode : "password";
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
          : h("div", { class: "grid" }, h("label", {}, `New ${kind}`, pwField(secret)), h("label", {}, `Type it again`, pwField(again))),
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
  try { hubInfo = await api("/api/hub"); } catch (e) {
    if (!(e instanceof Unauthorized)) { $("#state-pill").textContent = "reconnecting"; $("#state-pill").className = "pill"; }
    return;
  }
  const hb = hubInfo;
  $("#version").textContent = "v" + hb.version + " beta";
  $("#version").title = "mcsm is in beta: expect some rough edges, and keep backups.";
  $("#logout").classList.toggle("hidden", hb.auth.mode === "none");
  $("#quit").classList.toggle("hidden", !!hb.single);
  if (!hb.notice_accepted) { showNotice(); return; }
  offerSelfUpdate(hb.self_update);
  if (hb.auth.default && !hb.auth.managed && !promptDismissed()) showSecurity(true);
  if (hb.single && !server && hb.servers.length === 1) { location.hash = `#s/${hb.servers[0].id}/dashboard`; return; }
  renderNav();
  if (!server) {
    if (current && current.onHub) current.onHub(hb);
    return;
  }
  const want = server;
  let s;
  try { s = await api("/api/status"); } catch (e) {
    if (e instanceof Unauthorized) return;
    if (/no server with that id/.test(e.message)) { toast(e.message, true); location.hash = "#servers"; }
    return;
  }
  if (want !== server) return;  // switched servers meanwhile
  status = s;
  const pill = $("#state-pill");
  pill.textContent = s.state;
  pill.className = "pill " + s.state;
  $("#server-title").textContent = (s.motd || s.id) + (s.minecraft
    ? ` · Minecraft ${s.minecraft} · ${s.loader}` : " · not installed yet");
  const busy = !!s.job;
  $("#job").classList.toggle("hidden", !busy);
  $("#job-name").textContent = busy ? s.job.name + "…" : "";
  $("#btn-start").disabled = busy || s.state !== "stopped" || s.setup_pending;
  $("#btn-stop").disabled = busy || s.state === "stopped";
  $("#btn-restart").disabled = busy || s.state !== "running";
  const dot = $("#nav-update-dot");
  if (dot) dot.classList.toggle("hidden", !(s.update && !s.update.up_to_date && s.update.target));

  if (s.last_job && s.last_job.finished !== lastJobSeen) {
    if (lastJobSeen !== null) toast(`${s.last_job.name}: ${s.last_job.message}`, !s.last_job.ok);
    lastJobSeen = s.last_job.finished;
    if (current && current.onJobDone) current.onJobDone();
  } else if (lastJobSeen === null) {
    lastJobSeen = s.last_job ? s.last_job.finished : 0;
  }
  document.body.classList.toggle("setup-mode", !!s.setup_pending);
  if (s.setup_pending && currentName !== "setup") { location.hash = link("setup"); return; }
  if (!s.setup_pending && currentName === "setup" && !s.job) { location.hash = link("dashboard"); return; }
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
  img.src = scoped(`/api/players/skin?name=${encodeURIComponent(name)}`);
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
  const lagBanner = h("div");
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
        h("a", { class: "btn small ghost", href: link("players") }, "More…")) : null);
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
    fill(lagBanner, u ? laggingNotice(u.lagging, s.minecraft) : null);
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
        h("a", { href: link("updates"), class: "btn ghost" }, "Details →")),
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
    lagBanner,
    h("div", { class: "meters" }, cpu.el, mem.el),
    h("div", { class: "mt" }, playerCard),
    h("div", { class: "card mt" }, h("h3", {}, "Console"), con.el),
    h("div", { class: "grid mt" }, card("Server", statusBody,
      h("div", { class: "row mt-s" }, folderBtn("server", "Server folder"), folderBtn("world", "World folder"))), card("Updates", update)),
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
  const folders = hubInfo && hubInfo.local ? h("div", { class: "row mb" }, folderBtn("logs", "Logs folder"), folderBtn("crash", "Crash reports")) : null;
  fill($("#main"), folders, con.el);
  con.input.focus();
  every(1000, con.poll);
  return {};
};

// Mods holding back a newer Minecraft. After a month (and every month after) the admin is
// asked whether to remove them and update; "Keep waiting" hides it until the next reminder.
function laggingNotice(lag, installed, always = false) {
  if (!lag || !lag.mods.length) return null;
  const round = Math.floor(lag.days / lag.remind_days);
  const key = `mcsm-lag-${server}-${lag.version}-${round}`;
  let hidden = false;
  try { hidden = localStorage.getItem(key) === "1"; } catch (_) { /* private mode */ }
  if (!always && (!lag.due || hidden)) return null;
  const removable = lag.mods.filter((m) => m.config);
  const el = h("div", { class: "notice " + (lag.due ? "warn" : "") + " lagging" },
    h("strong", {}, lag.due ? `Minecraft ${lag.version} came out ${lag.days} days ago, and ${lag.mods.length === 1 ? "a mod hasn't" : `${lag.mods.length} mods haven't`} caught up`
      : `Minecraft ${lag.version} is out; waiting for ${lag.mods.length === 1 ? "a mod" : `${lag.mods.length} mods`} to support it`),
    h("p", { class: "small" }, `The server stays on Minecraft ${installed} until every mod supports ${lag.version}, so nothing breaks. ` +
      (lag.due ? "You can wait longer, or remove these mods and update now:" : `mcsm reminds you ${lag.remind_days} days after the release if they still haven't. These are:`)),
    h("ul", { class: "small" }, lag.mods.map((m) => h("li", {}, h("strong", {}, m.name), m.required ? h("span", { class: "tag" }, "required") : null,
      h("span", { class: "muted" }, ` — ${m.reason}`)))),
    h("div", { class: "row" },
      removable.length ? h("button", { class: "btn " + (lag.due ? "danger" : "small"), onclick: () => {
        const names = removable.map((m) => m.name).join(", ");
        if (!confirm(`Remove ${names} from this server and update to Minecraft ${lag.version}?\n\n` +
          "Their blocks and items disappear from the world. A backup is made first, and the update rolls back if the new version fails to start. " +
          "You can add the mods back later, once they support the new version.")) return;
        act(() => api("/api/updates/remove-and-upgrade", { method: "POST", body: { version: lag.version, mods: removable.map((m) => m.config) } }),
          `Removing ${removable.length} mod(s) and updating…`);
      } }, `Remove ${removable.length === 1 ? "it" : "them"} and update to ${lag.version}`) : null,
      lag.due && !always ? h("button", { class: "btn ghost", onclick: () => { try { localStorage.setItem(key, "1"); } catch (_) {} el.remove(); } }, "Keep waiting") : null));
  return el;
}

views.updates = () => {
  const body = h("div");
  const load = async () => {
    const r = await api("/api/updates").catch(() => null);
    if (!r) return;
    const c = r.check;
    const s = status || {};
    const checkBtn = h("button", { class: "btn", onclick: () => act(() => api("/api/updates/check", { method: "POST", body: {} }), "Checking…") }, "Check now");
    // Betas are tried on a copy, so the real world never meets one.
    const betaCard = h("div");
    api("/api/beta").then((r) => {
      if (!r.betas.length || !r.copies) return;
      const pick = h("select", { "aria-label": "Beta version" }, r.betas.map((v) => h("option", { value: v }, `Minecraft ${v}`)));
      fill(betaCard, card("Test a beta version",
        h("p", { class: "muted small" }, "Try the next Minecraft before it's released. mcsm makes a separate copy of this server " +
          "(world, mods and settings) on the beta, so this server and its world aren't touched. In the copy, mods that don't support " +
          "the beta yet are left out. Delete the copy when you're done."),
        h("div", { class: "row" }, pick, h("button", { class: "btn", onclick: () => {
          if (!confirm(`Make a copy of this server on Minecraft ${pick.value}? It appears in your server list as a separate server.`)) return;
          act(() => api("/api/beta/test", { method: "POST", body: { version: pick.value } }), "Copying the server…");
        } }, "Make a test copy"))));
    }).catch(() => {});
    if (!c) {
      fill(body, card(null, h("p", {}, "No update check has run yet."), checkBtn), h("div", { class: "mt" }, betaCard));
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
      folderBtn("manual", "Manual-downloads folder"),
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
            b.blockers.map((x) => h("li", {}, h("div", {}, h("strong", {}, x.name), x.waiting ? h("span", { class: "tag" }, "optional") : null,
              h("div", { class: "muted small" }, x.reason)))))),
        ))))) : null;

    const dropped = c.dropped.length ? card("Left out (optional or client-only)",
      h("ul", { class: "list" }, c.dropped.map((x) => h("li", {}, h("div", {}, h("strong", {}, x.name), h("div", { class: "muted small" }, x.reason)))))) : null;


    fill(body, 
      summary,
      laggingNotice(c.lagging, c.installed, true),
      h("div", { class: "row mb mt-s" }, applyBtn, checkBtn, h("span", { class: "muted small" }, `Last checked ${ago(c.checked_at)}`)),
      manual, changes, blocked, dropped, h("div", { class: "mt" }, betaCard));
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
  const me = hubInfo && hubInfo.servers ? hubInfo.servers.find((x) => x.id === server) : null;
  const plugins = !!me && me.loader === "paper";  // Paper runs plugins
  const results = h("div");
  const configured = h("div");
  const installed = h("div");
  const q = h("input", { placeholder: plugins ? "Search Modrinth for plugins…" : "Search Modrinth for server mods…", type: "search" });
  let searchTimer;

  let early = false;
  const earlyBox = h("input", { type: "checkbox", onchange: (e) => { early = e.target.checked; search(); } });
  const earlyRow = h("label", { class: "row small early-opt", title: EARLY_WARNING }, earlyBox,
    h("span", {}, `Also show ${plugins ? "plugins" : "mods"} with only alpha/beta builds (less stable)`));
  const search = async () => {
    const term = q.value.trim();
    if (!term) { fill(results, ); return; }
    fill(results, h("p", { class: "empty" }, "Searching…"));
    const r = await api(`/api/mods/search?q=${encodeURIComponent(term)}` + (early ? "&early=1" : "")).catch((e) => { toast(e.message, true); return null; });
    if (!r) return;
    const note = r.hidden || r.early_hidden ? h("p", { class: "muted small" },
      r.hidden ? `${r.hidden} result(s) hidden: no build for this server's Minecraft ${info.minecraft || ""}. ` : "",
      r.early_hidden ? [`${r.early_hidden} only ${r.early_hidden === 1 ? "has" : "have"} alpha/beta builds. `,
        h("button", { class: "link-btn", onclick: () => { earlyBox.checked = early = true; search(); } }, "Show them")] : null) : null;
    fill(results, note, ...(r.results.length ? r.results.map((m) => h("div", { class: "mod" },
      m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
      h("div", { class: "info" },
        h("div", { class: "name" }, m.name, h("span", { class: "tag" }, `${(m.downloads / 1e6).toFixed(1)}M downloads`),
          m.server_side === "optional" ? h("span", { class: "tag" }, "server optional") : null, channelTag(m.channel)),
        h("div", { class: "desc" }, m.description)),
      m.listed ? h("span", { class: "tag ok" }, "added") : h("div", { class: "row" },
        h("button", { class: "btn primary small", onclick: () => confirmEarly([m]) && add(m.slug, true, "modrinth", m.channel) }, "Add"),
        h("button", { class: "btn small", title: "Won't hold back Minecraft upgrades", onclick: () => confirmEarly([m]) && add(m.slug, false, "modrinth", m.channel) }, "Add optional")),
    )) : [h("p", { class: "empty" }, "No server mods found" + (info.minecraft ? ` that work on Minecraft ${info.minecraft}.` : "."))]));
  };
  const add = async (id, required, source = "modrinth", channel = null) => {
    const r = await act(() => api("/api/mods/add", { method: "POST", body: { source, id, required, channel: channel !== "release" ? channel : null } }));
    if (r) {
      toast(`Added ${r.name}` + (r.deps && r.deps.length ? `, with the mods it needs: ${r.deps.join(", ")}` : "") + ". Run an update check to install it.");
      load(); search();
    }
  };
  q.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 350); });

  const cfId = h("input", { placeholder: "CurseForge project id or slug" });
  let info = {};
  const configsCard = h("div");
  const load = async () => {
    const [r, cfg] = await Promise.all([api("/api/mods").catch(() => null), api("/api/configs").catch(() => null)]);
    if (!r) return;
    info = r;
    // Config files, matched to mods by the ids inside their jars.
    const groupByJar = new Map(((cfg && cfg.mods) || []).map((g) => [g.jar, g]));
    const groupFor = (key) => { const x = r.installed.find((m) => m.key === key); return x ? groupByJar.get(x.filename) : null; };
    const cfgBtn = (g) => g ? h("button", { class: "btn small", title: g.files.join("\n"), onclick: () => openConfigEditor(`${g.name} config`, g.files) },
      `⚙ Config${g.files.length > 1 ? ` (${g.files.length})` : ""}`) : null;
    fill(configsCard, cfg && (cfg.mods.length || cfg.other.length) ? card("Mod config files",
      h("p", { class: "muted small" }, "Change how mods behave. Most changes apply when the server restarts; the previous version of a file is kept each time you save."),
      h("ul", { class: "list" }, cfg.mods.map((g) => h("li", {},
        h("div", { class: "grow" }, h("strong", {}, g.name), h("div", { class: "small muted" }, g.files.join(", "))), cfgBtn(g))),
        cfg.other.length ? h("li", {}, h("div", { class: "grow" }, h("strong", {}, "Other config files"),
          h("div", { class: "small muted" }, `${cfg.other.length} file(s) not matched to an installed mod`)),
          h("button", { class: "btn small", onclick: () => openConfigEditor("Other config files", cfg.other) }, "⚙ Open")) : null))
      : null);
    // Each mod you added, with the mods installed because it needs them: they go together.
    const removeMods = async (specs, what) => {
      for (const x of specs) await api("/api/mods/remove", { method: "POST", body: { source: x.source, id: x.id } }).catch((e) => toast(e.message, true));
      toast(`Removed ${what}. ${specs.length === 1 ? "It's" : "They're"} uninstalled at the next update, with dependencies nothing else needs.`);
      load();
    };
    const needersOf = (depKey) => r.configured.filter((c) => c.deps.some((d) => d.key === depKey));
    fill(configured, r.configured.length ? h("ul", { class: "list" }, r.configured.flatMap((s) => [h("li", {},
      h("div", { class: "grow" }, h("strong", {}, s.name), h("span", { class: "tag" }, s.source), channelTag(s.channel)),
      h("label", { class: "row", title: "Every mod holds back Minecraft upgrades until it supports the new version. Required ones also decide the Minecraft version a new server starts on." },
        h("input", { type: "checkbox", checked: s.required, onchange: (e) => act(() => api("/api/mods/required", { method: "POST", body: { source: s.source, id: s.id, required: e.target.checked } })) }),
        "required"),
      cfgBtn(groupFor(s.key)),
      h("button", { class: "btn danger small", onclick: () => {
        // Dependencies another mod also needs stay; say so before and after.
        const shared = s.deps.map((d) => [d, needersOf(d.key).filter((c) => c.key !== s.key)]).filter(([, others]) => others.length);
        const going = s.deps.filter((d) => !shared.some(([x]) => x.key === d.key));
        if (!confirm(`Remove ${s.name}?` +
          (going.length ? ` The mods it needs (${going.map((d) => d.name).join(", ")}) go too.` : "") +
          (shared.length ? ` ${shared.map(([d]) => d.name).join(", ")} ${shared.length === 1 ? "stays" : "stay"}, because other mods need ${shared.length === 1 ? "it" : "them"}.` : "") +
          " It's uninstalled at the next update.")) return;
        removeMods([s], s.name);
        for (const [d, others] of shared) toast(`${d.name} wasn't removed: ${others.map((c) => c.name).join(" and ")} ${others.length === 1 ? "needs" : "need"} it too.`);
      } }, "Remove")),
      ...s.deps.map((d) => h("li", { class: "dep" },
        h("div", { class: "grow" }, "↳ ", h("strong", {}, d.name), h("span", { class: "tag" }, `needed by ${needersOf(d.key).map((c) => c.name).join(", ")}`)),
        h("button", { class: "btn ghost small", onclick: () => {
          const needers = needersOf(d.key);
          if (confirm(`${d.name} is needed by ${needers.map((c) => c.name).join(", ")}, so removing it removes ${needers.length === 1 ? "that mod" : "those mods"} too. Continue?`))
            removeMods(needers, needers.map((c) => c.name).join(", "));
        } }, "Remove")))])) : h("p", { class: "empty" }, "No mods configured."));

    fill(installed, 
      r.installed.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Mod"), h("th", {}, "Version"), h("th", {}, "File"))),
        h("tbody", {}, r.installed.map((m) => h("tr", {},
          h("td", {}, m.name, m.dependency_of ? h("span", { class: "tag" }, `needed by ${(r.installed.find((x) => x.key === m.dependency_of) || {}).name || "another mod"}`) : null, m.manual ? h("span", { class: "tag warn" }, "manual") : null),
          h("td", {}, m.version), h("td", {}, h("code", {}, m.filename)))))) : h("p", { class: "empty" }, "Nothing installed yet."),
      r.skipped.length ? h("div", { class: "notice warn mt-s" }, h("strong", {}, "Not installed: "),
        r.skipped.map((x) => h("div", { class: "small" }, `${x.key}: ${x.reason}`))) : null,
      r.unmanaged.length ? h("div", { class: "notice mt-s" }, h("strong", {}, "Unmanaged jars (not updated by mcsm): "),
        r.unmanaged.map((x) => h("div", {}, h("code", {}, x)))) : null,
    );
  };

  const picker = h("input", { type: "file", multiple: true, accept: ".jar", class: "hidden" });
  picker.addEventListener("change", async () => {
    for (const f of [...picker.files]) {
      const r = await api(`/api/mods/local?filename=${encodeURIComponent(f.name)}`, { method: "POST", raw: f })
        .catch((e) => { toast(`${f.name}: ${e.message}`, true); return null; });
      if (r) toast(r.managed ? `${r.name}: found on Modrinth, so mcsm will keep it up to date` : `${r.name} added as your own file (mcsm won't update it)`);
    }
    picker.value = "";
    load();
  });
  const sources = h("div", { class: "source-buttons" },
    h("button", { type: "button", class: "btn", onclick: () => picker.click() }, "📁 Local files",
      h("span", { class: "small muted" }, ".jar files on this computer")),
    h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "mod", target: server, loader: info.loader || "", version: info.minecraft || "" }) },
      plugins ? "🔎 Download plugins" : "🔎 Download mods", h("span", { class: "small muted" }, plugins ? "Browse Modrinth" : "Browse Modrinth and CurseForge")),
    hubInfo && hubInfo.single ? null : h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "modpack", target: "setup" }) },
      "📦 Modpacks", h("span", { class: "small muted" }, "Start a new server from a pack")),
    picker);

  fill($("#main"), 
    h("h2", { class: "view-title" }, plugins ? "Plugins" : "Mods"),
    card(plugins ? "Add plugins" : "Add mods", sources, h("h3", { class: "mt" }, "Quick add"), q, earlyRow, results,
      plugins ? h("p", { class: "muted small mt-s" }, "Paper runs Paper, Spigot and Bukkit plugins from its plugins folder. Players don't need them.")
        : h("div", { class: "row mt-s" }, cfId,
          h("button", { class: "btn", onclick: () => cfId.value.trim() && add(cfId.value.trim(), true, "curseforge") }, "Add from CurseForge"))),
    h("div", { class: "mt" }, configsCard),
    h("div", { class: "grid mt" }, card("Configured (mcsm.toml)", configured,
      h("div", { class: "row mt-s" }, testButton({
        check: ["/api/mods/check", {}],
        trial: { server },
        keepWorking: async (res) => {
          for (const o of res.outliers) await api("/api/mods/remove", { method: "POST", body: { source: o.source, id: o.id } }).catch((e) => toast(e.message, true));
          toast(`Removed ${res.outliers.map((o) => o.id).join(", ")}. They're uninstalled at the next update.`);
          load();
        },
      }))),
      card("Installed", hubInfo && hubInfo.local ? h("div", { class: "row mb" }, folderBtn("mods", "Mods folder"), folderBtn("config", "Config folder")) : null, installed)),
  );
  load();
  return { onJobDone: load, refresh: load };
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
    card("Backups", h("div", { class: "row" }, h("p", { class: "muted small grow" }, "Restoring needs the server to be stopped."), folderBtn("backups", "Backups folder")), list),
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
        card("Download Temurin", h("p", { class: "muted" }, "Downloads Eclipse Temurin into .mcsm/java/. ", folderBtn("java", "Open it")),
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
    const advanced = { ...s.properties };
    const advancedEl = h("details", { class: "advanced mt-l" },
      h("summary", {}, "Advanced server settings"),
      h("p", { class: "muted small" }, "The rest of Minecraft's server.properties. These apply at the next restart."),
      propsEditor(s.properties_schema, advanced));
    fill(form,
      h("h3", {}, "Updates"),
      h("div", { class: "grid" },
        h("label", {}, "Strategy", sel("strategy", s.choices.strategy)),
        h("label", {}, "Lowest mod release channel", sel("mod_channel", s.choices.mod_channel)),
        h("label", {}, "Check every (e.g. 6h, 30m)", txt("check_interval")),
        h("label", {}, "In-game warnings (minutes, comma separated)", txt("warn_minutes", { value: s.warn_minutes.join(", ") }))),
      h("div", { class: "grid mt-s" },
        chk("auto_upgrade", "Apply updates automatically (a new Minecraft only once every mod supports it)"),
        chk("wait_for_empty", "Wait until nobody is online"),
        chk("verify_boot", "Test-boot and roll back on failure")),
      h("h3", { class: "mt-l" }, "Server"),
      h("div", { class: "grid" },
        h("label", {}, "Memory (e.g. 6G)", txt("memory")),
        h("label", {}, "Port players connect to", txt("port", { type: "number", min: 1024, max: 65535 })),
        h("label", {}, "Backups to keep", txt("backups_keep", { type: "number", min: 1 })),
        h("label", {}, "Discord webhook URL", txt("discord_webhook", { type: "url", placeholder: "https://discord.com/api/webhooks/…" }))),
      h("div", { class: "grid mt-s" }, chk("restart_on_crash", "Restart after crashes")),
      advancedEl,
      h("div", { class: "row mt" }, h("button", { class: "btn primary", type: "submit" }, "Save settings"),
        h("span", { class: "muted small" }, "Memory, port and advanced changes apply at the next restart.")),
    );
    form.onsubmit = (e) => {
      e.preventDefault();
      const body = {
        strategy: f.strategy.value, mod_channel: f.mod_channel.value, check_interval: f.check_interval.value.trim(),
        warn_minutes: f.warn_minutes.value.split(",").map((x) => x.trim()).filter(Boolean).map(Number),
        auto_upgrade: f.auto_upgrade.checked, wait_for_empty: f.wait_for_empty.checked, verify_boot: f.verify_boot.checked,
        memory: f.memory.value.trim(), backups_keep: Number(f.backups_keep.value), discord_webhook: f.discord_webhook.value.trim(),
        port: Number(f.port.value),
        restart_on_crash: f.restart_on_crash.checked,
        properties: changedProps(advanced, s.properties),
      };
      act(() => api("/api/settings", { method: "POST", body }), "Settings saved").then(load);
    };
  };
  const danger = h("div", { class: "card danger-zone mt" });
  const renderDanger = () => {
    const me = hubInfo && hubInfo.servers ? hubInfo.servers.find((x) => x.id === server) : null;
    if (!me || hubInfo.single) { fill(danger); danger.classList.add("hidden"); return; }
    danger.classList.remove("hidden");
    fill(danger, h("h3", {}, "Delete this server"),
      h("div", { class: "row" },
        h("span", { class: "grow muted" }, "Take it off your list, and choose whether to also erase its world, mods and backups."),
        h("button", { class: "btn danger", onclick: () => deleteServer(me, () => { location.hash = "#servers"; }) }, "Delete server…")));
  };
  // Move to another computer: export everything to one file, import it there.
  const exportCard = h("div", { class: "card mt" });
  const withBackups = h("input", { type: "checkbox" });
  const loadExports = async () => {
    const r = await api("/api/export").catch(() => null);
    if (!r) return;
    fill(exportCard, h("h3", {}, "Move to another computer"),
      h("p", { class: "muted small" }, "Export saves this server (worlds, mods, configs, settings, player lists) in one .zip. " +
        "On the other computer, install mcsm, then choose Import a server on the server list. The world is saved first, " +
        "so this works while the server runs."),
      h("div", { class: "row" },
        h("button", { class: "btn primary", disabled: !!(status && status.job), onclick: () => act(() => api("/api/export", { method: "POST", body: { backups: withBackups.checked } }), "Exporting…") }, "Export server"),
        h("label", { class: "row" }, withBackups, h("span", {}, "Include backups (bigger file)"))),
      r.exports.length ? h("table", { class: "mt-s" },
        h("thead", {}, h("tr", {}, h("th", {}, "Export"), h("th", {}, "Made"), h("th", {}, "Size"), h("th", {}))),
        h("tbody", {}, r.exports.map((x) => h("tr", {},
          h("td", {}, h("code", {}, x.name)), h("td", {}, fmtTime(x.created)), h("td", {}, fmtBytes(x.size)),
          h("td", { class: "row" },
            h("a", { class: "btn small", href: scoped(`/api/export/download?name=${encodeURIComponent(x.name)}`), download: x.name }, "Download"),
            h("button", { class: "btn small danger", onclick: () => confirm(`Delete ${x.name}?`) && act(() => api("/api/export/delete", { method: "POST", body: { name: x.name } }), "Deleted").then(loadExports) }, "Delete"))))))
        : null,
      h("div", { class: "row mt-s" }, h("p", { class: "muted small grow" }, "Exports are kept in ", h("code", {}, r.folder)),
        folderBtn("exports", "Exports folder"), folderBtn("server", "Server folder")));
  };
  const worldCard = card("World",
    h("p", { class: "muted small" }, "Put a different world on this server: a singleplayer world or a world .zip. " +
      "The current world is backed up first (see Backups), so you can go back."),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => pickWorld(async (w) => {
        if (!confirm(`Replace this server's world with ${w.name}? The current world is backed up first.`)) return;
        await act(() => api("/api/world/replace", { method: "POST", body: { world: w.world } }), "Replacing the world…");
      }) }, "Replace the world…"),
      folderBtn("world", "World folder")));
  worldCard.classList.add("mt");
  fill($("#main"), h("h2", { class: "view-title" }, "Server settings"), form, worldCard, exportCard, danger);
  load();
  loadExports();
  renderDanger();
  return { onStatus: renderDanger, onJobDone: loadExports };
};

// ------------------------------------------------------------------ friends
// A download friends run to set up their Minecraft for this server (mods and all).
views.friends = () => {
  const body = h("div");
  let data = null;
  const save = async (changes, message) => {
    const r = await act(() => api("/api/client", { method: "POST", body: changes }), message);
    if (r) { data = r; render(); }
  };
  const reload = async () => { const r = await api("/api/client").catch(() => null); if (r) { data = r; render(); } };
  // Your own mod files for players (e.g. ones that aren't on Modrinth).
  const picker = h("input", { type: "file", multiple: true, accept: ".jar", class: "hidden" });
  picker.addEventListener("change", async () => {
    for (const f of [...picker.files]) {
      const r = await api(`/api/client/local?filename=${encodeURIComponent(f.name)}`, { method: "POST", raw: f })
        .catch((e) => { toast(`${f.name}: ${e.message}`, true); return null; });
      if (r) toast(`${f.name} added for players`);
    }
    picker.value = "";
    reload();
  });
  // Mods the server's mods need on players' computers are added by themselves; say so once.
  const announceCompanions = (d) => {
    const key = `mcsm-companions-${server}`;
    let seen = [];
    try { seen = JSON.parse(localStorage.getItem(key) || "[]"); } catch (_) { /* private mode */ }
    const fresh = ((d.pack && d.pack.mods) || []).filter((m) => m.needed_by && !seen.includes(m.project));
    if (!fresh.length) return;
    const byMod = new Map();
    for (const m of fresh) byMod.set(m.needed_by, [...(byMod.get(m.needed_by) || []), m.name]);
    for (const [by, names] of byMod) toast(`Added ${names.join(", ")} for players, because ${by} needs ${names.length === 1 ? "it" : "them"} on their computers.`);
    try { localStorage.setItem(key, JSON.stringify([...seen, ...fresh.map((m) => m.project)])); } catch (_) { /* private mode */ }
  };

  const render = () => {
    const d = data;
    if (!d.available) {
      fill(body, card(null, h("p", {}, "Friend downloads are part of mcsm's server list. Start mcsm by double-clicking it (or `mcsm start`) to use them.")));
      return;
    }
    const toggle = h("input", { type: "checkbox", checked: d.enabled, onchange: (e) => save({ enabled: e.target.checked },
      e.target.checked ? "Friend download switched on" : "Friend download switched off") });
    const intro = card("Let friends set up their Minecraft",
      h("p", {}, "Share a link. Your friends download a small file that adds a ", h("strong", {}, (status && status.motd) || "server"),
        " instance to their launcher (Minecraft Launcher, Prism Launcher, Modrinth App or CurseForge: they choose) with the right Minecraft version, mod loader and mods, and puts this server in their multiplayer list. They sign in with their own Minecraft account as usual."),
      h("label", { class: "row mt-s" }, toggle, h("span", {}, "Make a download for friends")));
    if (!d.enabled) { fill(body, intro); return; }
    const s = d.share || {};
    const links = d.links || {};
    const linkRow = (label, hint, url) => {
      const input = h("input", { readonly: true, value: url, class: "grow mono", "aria-label": label });
      return h("div", { class: "invite" }, h("strong", {}, label), h("div", { class: "muted small" }, hint),
        h("div", { class: "row" }, input, h("button", { class: "btn primary", onclick: async () => {
          try { await navigator.clipboard.writeText(input.value); toast(`${label} copied`); }
          catch (_) { input.select(); document.execCommand("copy"); toast(`${label} copied`); }
        } }, "Copy")));
    };
    const findIp = h("button", { class: "btn", onclick: async () => {
      findIp.disabled = true;
      const r = await act(() => api("/api/hub/share/public-ip", { method: "POST", body: {} }));
      findIp.disabled = false;
      if (r) { toast(`Your public address is ${r.ip}`); data = await api("/api/client"); render(); }
    } }, links.internet ? "Check my public IP again" : "🌐 Use my public IP");
    const pack = d.pack;
    const companions = ((pack && pack.mods) || []).filter((m) => m.needed_by);
    const sideTag = (m) => h("span", { class: "tag" }, m.side === "client" ? "players only" : "server + players");
    fill(body,
      intro,
      h("div", { class: "mt" }, card("Invite links",
        links.local ? linkRow("Local link", `For friends on the same Wi-Fi or network as this computer (${s.lan_ip}).`, links.local) : null,
        links.internet ? linkRow("Internet link", `For friends anywhere else, through your public address (${s.address}).`, links.internet)
          : h("div", { class: "invite" }, h("strong", {}, "Internet link"),
            h("div", { class: "muted small" }, "For friends elsewhere, mcsm needs your public address. It can find it for you.")),
        h("div", { class: "row mt-s" }, findIp,
          links.internet || links.local ? h("button", { class: "btn", onclick: () => openDiscord(links) }, "💬 Post to Discord") : null,
          h("button", { class: "btn ghost", onclick: () => {
            if (confirm("Make a new link? The old one stops working (friends who already set up keep playing, but can't update until they get the new link).")) {
              act(() => api("/api/client/new-link", { method: "POST", body: {} }), "New link made").then((r) => { if (r) { data = r; render(); } });
            }
          } }, "New link")),
        s.error ? h("div", { class: "notice bad mt-s" }, s.error)
          : h("p", { class: "muted small" }, s.running ? `Sharing on port ${s.port}.` : "Sharing starts in a few seconds."),
        h("p", { class: "muted small" },
          "For the internet link to work, forward two TCP ports on your router to this computer: ", h("strong", {}, String(s.port)),
          " (the download) and ", h("strong", {}, String((status && status.port) || 25565)), " (Minecraft). Your public address can change; ",
          "press the button again if friends can't connect. You can also type an address (e.g. a domain) under ",
          h("a", { href: "#mcsm" }, "mcsm settings → Sharing"), "."))),
      h("div", { class: "mt" }, card("What friends get",
        d.pack_error ? h("div", { class: "notice warn" }, d.pack_error)
          : !pack ? h("p", { class: "empty" }, "Install the server first; the list appears once it's set up.")
          : [h("p", {}, `Minecraft ${pack.minecraft} with ${pack.loader === "vanilla" ? "no mod loader" : pack.loader + " " + pack.loader_version}, ${pack.mods.length} mod(s), ${pack.memory_gb} GB of memory.`),
             pack.mods.length ? h("ul", { class: "list" }, pack.mods.map((m) => h("li", {}, h("span", { class: "grow" }, m.name), sideTag(m)))) : null,
             pack.manual.length ? h("div", { class: "notice warn mt-s" }, "Players have to download these themselves (their authors block automatic downloads): ",
               pack.manual.map((m) => m.name).join(", ")) : null,
             pack.skipped.length ? h("div", { class: "notice warn mt-s" }, pack.skipped.map((x) => `${x.name}: ${x.reason}`).join("; ")) : null],
        d.mods.length ? h("div", { class: "mt-s" }, h("strong", {}, "Mods you added for players: "),
          d.mods.map((x) => h("span", { class: "tag" }, x, " ", h("button", { class: "link-btn", "aria-label": `Remove ${x}`,
            onclick: () => save({ mods: d.mods.filter((y) => y !== x) }, `${x} removed`) }, "✕")))) : null,
        h("label", { class: "mt" }, "Memory for friends' Minecraft",
          (() => { const sel = h("select", { onchange: (e) => save({ memory_gb: Number(e.target.value) }, "Saved") },
            [2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32].map((g) => h("option", { value: String(g) }, `${g} GB`))); sel.value = String(d.memory_gb); return sel; })()))),
      d.loader === "vanilla" || d.loader === "paper" ? null : h("div", { class: "mt" }, card("Mods for players",
        h("p", { class: "muted small" }, "Client-side mods like minimaps, recipe viewers or performance mods. The server's own mods that players need are included automatically, and so are the client-side mods they need."),
        h("div", { class: "source-buttons" },
          h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "mod", target: server, side: "client", loader: d.loader, version: d.minecraft || "" }) },
            "🔎 Set up now", h("span", { class: "small muted" }, "Browse mods that run on players' computers")),
          h("button", { type: "button", class: "btn", onclick: () => picker.click() }, "📁 Local files",
            h("span", { class: "small muted" }, ".jar files on this computer, for players")),
          picker),
        h("h3", { class: "mt" }, "Your players' mods"),
        d.mods.length || d.local_mods.length || companions.length ? h("ul", { class: "list" },
          d.mods.map((x) => h("li", {}, h("strong", { class: "grow" }, x),
            h("button", { class: "btn small danger", onclick: () => save({ mods: d.mods.filter((y) => y !== x) }, `${x} removed`) }, "Remove"))),
          d.local_mods.map((x) => h("li", {}, h("div", { class: "grow" }, h("strong", {}, x), h("span", { class: "tag" }, "local file")),
            h("button", { class: "btn small danger", onclick: () => confirm(`Remove ${x} from the players' download?`) &&
              act(() => api("/api/client/local/remove", { method: "POST", body: { name: x } }), `${x} removed`).then(reload) }, "Remove"))),
          companions.map((m) => h("li", { class: "dep" }, h("div", { class: "grow" }, "↳ ", h("strong", {}, m.name),
            h("span", { class: "tag" }, `added automatically: ${m.needed_by} needs it`)))))
          : h("p", { class: "empty" }, "None yet. Leave it empty if you like: players get the server's mods either way."),
        h("div", { class: "row mt-s" }, testButton({ check: ["/api/client/check", {}], trial: null }),
          h("span", { class: "muted small" }, "Checks the server's mods and these together.")))),
    );
    announceCompanions(d);
  };
  // Reached from a new server's setup: it's still installing (see the bar at the bottom).
  const installing = dock && dock.sid === server && !dock.done;
  fill($("#main"), h("h2", { class: "view-title" }, installing ? `Friends for ${dock.name}` : "Friends"),
    installing ? h("div", { class: "notice mb" }, h("strong", {}, "Your server is still installing. "),
      "Meanwhile, pick the mods your friends' Minecraft gets. The invite link works once it's ready.",
      h("div", { class: "row mt-s" }, h("a", { class: "btn small", href: `#s/${server}/setup` }, "Back to the progress"))) : null,
    body);
  api("/api/client").then((r) => { data = r; render(); }).catch((e) => { if (!(e instanceof Unauthorized)) toast(e.message, true); });
  return { refresh: reload };
};

// ---------------------------------------------------------- rich text (mod pages)
// Modrinth descriptions are Markdown with bits of HTML; CurseForge's are HTML. Both are
// turned into DOM through an allowlist, so nothing from outside can run script here.
const RICH_TAGS = new Set(["A", "P", "BR", "HR", "H1", "H2", "H3", "H4", "H5", "H6", "UL", "OL", "LI", "STRONG", "B",
  "EM", "I", "U", "S", "DEL", "CODE", "PRE", "BLOCKQUOTE", "IMG", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD",
  "DETAILS", "SUMMARY", "DIV", "SPAN", "CENTER", "SUB", "SUP", "KBD", "FIGURE", "FIGCAPTION"]);
const RICH_DROP = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "NOSCRIPT", "TEMPLATE", "SVG", "MATH",
  "FORM", "INPUT", "BUTTON", "TEXTAREA", "SELECT", "LINK", "META", "BASE", "VIDEO", "AUDIO"]);
function richFromHtml(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");  // inert: nothing runs or loads
  const out = document.createDocumentFragment();
  const walk = (node, into) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { into.append(child.textContent); continue; }
      if (child.nodeType !== 1) continue;
      const tag = child.tagName;
      if (RICH_DROP.has(tag)) continue;
      if (!RICH_TAGS.has(tag)) { walk(child, into); continue; }
      const el = document.createElement(tag === "CENTER" ? "div" : tag.toLowerCase());
      if (tag === "A") {
        const href = child.getAttribute("href") || "";
        if (/^https?:\/\//i.test(href)) { el.href = href; el.target = "_blank"; el.rel = "noopener noreferrer"; }
      } else if (tag === "IMG") {
        const src = child.getAttribute("src") || "";
        if (!/^https:\/\//i.test(src)) continue;
        el.src = src; el.alt = child.getAttribute("alt") || ""; el.loading = "lazy"; el.referrerPolicy = "no-referrer";
        for (const a of ["width", "height"]) if (/^\d{1,4}$/.test(child.getAttribute(a) || "")) el.setAttribute(a, child.getAttribute(a));
      } else if (tag === "TD" || tag === "TH") {
        if (/^\d{1,2}$/.test(child.getAttribute("colspan") || "")) el.setAttribute("colspan", child.getAttribute("colspan"));
      }
      walk(child, el);
      into.append(el);
    }
  };
  walk(doc.body, out);
  return out;
}
function mdToHtml(md) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => s
    .replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`)
    .replace(/!\[([^\]]*)\]\((\S+?)(?:\s+"[^"]*")?\)/g, '<img alt="$1" src="$2">')
    .replace(/\[([^\]]+)\]\((\S+?)(?:\s+"[^"]*")?\)/g, '<a href="$2">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2">$2</a>');
  const out = [];
  let list = null, para = [];
  const flush = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
    if (list) { out.push(`</${list}>`); list = null; }
  };
  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    let m;
    if (!line.trim()) { flush(); continue; }
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/))) { flush(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); continue; }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    if ((m = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/))) {
      if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
      const kind = /\d/.test(m[1]) ? "ol" : "ul";
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2])}</li>`);
      continue;
    }
    if (/^\s*<\/?[a-zA-Z][^>]*>\s*$/.test(line) || /^\s*<(div|center|p|img|details|summary|table|h\d|br|a)\b/i.test(line)) {
      flush(); out.push(line); continue;  // raw HTML block (sanitised below)
    }
    if (list) { out.push(`</${list}>`); list = null; }
    para.push(line.trim());
  }
  flush();
  return out.join("\n");
}
function richText(text, format) {
  return h("div", { class: "rich" }, richFromHtml(format === "html" ? text : mdToHtml(text || "")));
}

// ------------------------------------------------------------------ mod browser
// Opened from setup, the Mods page and Friends: the page slides left into a narrow rail
// (click it or press Escape to go back) and the browser takes the screen, with search,
// filters and sort at the top left, results with checkboxes below, "Add selected" at the
// bottom, and the mod's page on the right.
let browserOpen = null;
function openBrowser(params) {
  closeBrowser(true);
  const stage = $("#stage");
  const back = { setup: "setup", new: "setup", mods: "Mods", friends: "Friends" }[currentName] || "the page";
  const rail = h("button", { type: "button", class: "browse-rail", title: `Back to ${back} (Esc)`, "aria-label": `Back to ${back}`,
    onclick: () => closeBrowser() }, h("span", { class: "rail-arrow" }, "‹"), h("span", { class: "rail-label" }, `Back to ${back}`));
  const refresh = () => { if (current && current.refresh) current.refresh(); };
  const b = browserPanel(new URLSearchParams(params), {
    close: () => closeBrowser(),
    addMods: (mods) => {
      for (const m of mods) setupAddMod(setupModKey(m), m.name, m.channel);
      toast(`${mods.length} mod(s) added`);
      closeBrowser();
      refresh();
    },
    pickPack: (pack) => {
      Object.assign(setupState, { modpack: pack, loader: pack.loader, minecraft: pack.minecraft });
      toast(`Modpack chosen: ${pack.name}`);
      closeBrowser();
      if (currentName === "new" || currentName === "setup") refresh(); else location.hash = "#new";
    },
    changed: () => { closeBrowser(); refresh(); },
  });
  const panel = h("section", { class: "inpage-browser", "aria-label": "Mod browser" }, b.el);
  stage.append(rail, panel);
  stage.classList.add("browsing");
  browserOpen = { rail, panel };
  b.start();
}
function closeBrowser(instant = false) {
  if (!browserOpen) return;
  const { rail, panel } = browserOpen;
  browserOpen = null;
  const stage = $("#stage");
  stage.classList.remove("browsing");
  if (instant || matchMedia("(prefers-reduced-motion: reduce)").matches) { rail.remove(); panel.remove(); return; }
  panel.classList.add("leaving");
  rail.remove();
  setTimeout(() => panel.remove(), 260);
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && browserOpen && !document.querySelector(".modal")) closeBrowser();
});

views.browse = (params) => {  // a direct #browse link: the browser on its own
  document.body.classList.add("browse-mode");
  const b = browserPanel(params, null);
  fill($("#main"), b.el);
  b.start();
  return {};
};

function browserPanel(params, host) {
  const kind = params.get("type") === "modpack" ? "modpack" : "mod";
  const target = params.get("target") || "setup";
  const loader = params.get("loader") || "";
  const noun = loader === "paper" ? "plugin" : kind;  // Paper runs plugins (from Modrinth)
  const forPlayers = params.get("side") === "client";  // the Friends page: mods for players' computers
  const base = target === "setup" ? "/api/hub/browse" : `/api/servers/${encodeURIComponent(target)}/browse`;
  const st = { q: "", source: "modrinth", sort: "relevance", category: "", version: params.get("version") || "",
    offset: 0, total: 0, results: [], selected: new Map(), active: null, early: false, hidden: 0, earlyHidden: 0 };
  const earlyBox = h("input", { type: "checkbox", onchange: (e) => { st.early = e.target.checked; search(); } });
  const earlyRow = kind === "mod" && !forPlayers ? h("label", { class: "row small early-opt", title: EARLY_WARNING }, earlyBox,
    h("span", {}, "Also show mods with only alpha/beta builds (less stable)")) : null;
  const list = h("div", { class: "browse-results" });
  const details = h("div", { class: "browse-right" }, h("p", { class: "empty" }, `Pick a ${noun} on the left to read about it here.`));
  const count = h("span", { class: "grow muted small" });
  const addBtn = h("button", { class: "btn primary" + (kind === "modpack" ? " hidden" : ""), disabled: true }, `Add selected ${noun}s`);
  const q = h("input", { type: "search", placeholder: `Search ${noun}s…`, "aria-label": "Search" });
  const sort = h("select", { "aria-label": "Sort by" }, [["relevance", "Best match"], ["downloads", "Most downloaded"],
    ["follows", "Most followed"], ["newest", "Newest"], ["updated", "Recently updated"]].map(([v, l]) => h("option", { value: v }, l)));
  const source = h("select", { "aria-label": "Source" }, h("option", { value: "modrinth" }, "Modrinth"),
    kind === "mod" && noun !== "plugin" && !forPlayers ? h("option", { value: "curseforge" }, "CurseForge") : null);
  let cfKey = null;  // whether a CurseForge API key is set (asked once)
  const category = h("select", { "aria-label": "Category" }, h("option", { value: "" }, "All categories"));
  const version = h("input", { value: st.version, placeholder: "Any version", "aria-label": "Minecraft version", class: "narrow" });
  let timer, seq = 0;

  const fmtNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
  // What a ticked mod brings along (the page adds those too).
  const needs = async (m) => {
    if (m.source !== "modrinth" || m.deps || forPlayers) return;
    const reqBase = target === "setup" ? "/api/hub/mods/requires" : `/api/servers/${encodeURIComponent(target)}/mods/requires`;
    const p = new URLSearchParams({ id: m.id });
    if (m.channel && m.channel !== "release") p.set("channel", m.channel);
    if (target === "setup" && loader) p.set("loader", loader);
    if (st.version || target === "setup") p.set("version", st.version);
    const r = await api(`${reqBase}?${p}`).catch(() => null);
    if (!r) return;
    m.deps = r.deps.map((d) => d.name);
    m.bad = r.compatible ? "" : r.reason;
    updateFooter();
  };
  const updateFooter = () => {
    const n = st.selected.size;
    const extra = [...new Set([...st.selected.values()].flatMap((m) => m.deps || []))];
    const bad = [...st.selected.values()].filter((m) => m.bad);
    count.textContent = kind === "modpack" ? (st.active ? "" : "Pick a modpack to see its versions.")
      : n ? `${n} selected: ${[...st.selected.values()].map((m) => m.name).slice(0, 3).join(", ")}${n > 3 ? "…" : ""}` +
        (extra.length ? ` · also adds ${extra.join(", ")} (needed)` : "") + (bad.length ? ` · ⚠ ${bad.map((m) => m.bad).join("; ")}` : "")
        : `Tick the ${noun}s you want.`;
    addBtn.disabled = kind === "modpack" ? true : n === 0;
  };
  // CurseForge only answers apps with an API key (free); explain and take one here.
  const keyPanel = () => {
    const input = h("input", { type: "password", placeholder: "Paste your CurseForge API key", autocomplete: "off", "aria-label": "CurseForge API key" });
    const save = h("button", { class: "btn primary", onclick: async () => {
      save.disabled = true;
      try {
        await api("/api/hub/curseforge", { method: "POST", body: { key: input.value } });
        cfKey = true;
        toast("CurseForge key saved. It works for all your servers.");
        loadCategories();
        search();
      } catch (e) { if (!(e instanceof Unauthorized)) toast(e.message, true); save.disabled = false; }
    } }, "Save key");
    fill(list, h("div", { class: "notice key-panel" },
      h("strong", {}, "CurseForge needs an API key"),
      h("p", { class: "small" }, "CurseForge only lets apps search it with a key. It's free and takes a minute:"),
      h("ol", { class: "small" },
        h("li", {}, "Open ", h("a", { href: "https://console.curseforge.com/", target: "_blank", rel: "noopener noreferrer" }, "console.curseforge.com ↗"), " and sign in (a CurseForge or Google account works)."),
        h("li", {}, "Go to ", h("strong", {}, "API keys"), " and copy your key."),
        h("li", {}, "Paste it here. mcsm checks it with CurseForge and keeps it in mcsm settings.")),
      h("div", { class: "row" }, input, save)));
  };
  const search = async (more = false) => {
    if (st.source === "curseforge") {
      if (cfKey === null) cfKey = (await api("/api/hub/curseforge").catch(() => ({ set: false }))).set;
      if (!cfKey) { keyPanel(); st.results = []; updateFooter(); return; }
    }
    const mine = ++seq;
    if (!more) { st.offset = 0; list.scrollTop = 0; }
    const p = new URLSearchParams({ type: kind, q: st.q, source: st.source, sort: st.sort, offset: String(st.offset) });
    if (st.category) p.set("category", st.category);
    if (st.early) p.set("early", "1");
    if (forPlayers) p.set("side", "client");
    p.set("version", st.version);
    if (loader) p.set("loader", loader);
    if (!more) fill(list, h("p", { class: "empty" }, "Searching…"));
    const r = await api(`${base}/search?${p}`).catch((e) => { if (!(e instanceof Unauthorized)) fill(list, h("div", { class: "notice bad" }, e.message)); return null; });
    if (!r || mine !== seq) return;
    st.results = more ? st.results.concat(r.results) : r.results;
    st.total = r.total;
    st.hidden = (more ? st.hidden : 0) + (r.hidden || 0);
    st.earlyHidden = (more ? st.earlyHidden : 0) + (r.early_hidden || 0);
    renderList();
  };
  const renderList = () => {
    // Search results the chosen version can't run are left out; say so.
    const where = `${loader ? loader + " " : ""}Minecraft ${st.version}`;
    const note = st.version && (st.hidden || st.earlyHidden) ? h("p", { class: "muted small hidden-note" },
      st.hidden ? `${st.hidden} result(s) hidden: no build for ${where}. ` : "",
      st.earlyHidden ? [`${st.earlyHidden} only ${st.earlyHidden === 1 ? "has" : "have"} alpha/beta builds. `,
        h("button", { class: "link-btn", onclick: () => { earlyBox.checked = st.early = true; search(); } }, "Show them")] : null) : null;
    fill(list, note, st.results.length ? st.results.map((m) => {
      const key = `${m.source}:${m.id}`;
      const box = kind === "mod" ? h("input", { type: "checkbox", checked: st.selected.has(key), "aria-label": `Select ${m.name}`,
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => { if (e.target.checked) { st.selected.set(key, m); needs(m); } else st.selected.delete(key); updateFooter(); } }) : null;
      return h("div", { class: "result" + (st.active === key ? " active" : ""), tabindex: "0", role: "button",
        onclick: () => showDetails(m), onkeydown: (e) => { if (e.key === "Enter") showDetails(m); } },
        box || h("span"),
        m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "info" },
          h("div", { class: "name" }, m.name, m.author ? h("span", { class: "muted small" }, ` by ${m.author}`) : null, " ", channelTag(m.channel)),
          h("div", { class: "desc" }, m.summary),
          h("div", { class: "muted small" }, `⬇ ${fmtNum(m.downloads)}`, m.follows ? ` · ♥ ${fmtNum(m.follows)}` : "",
            m.updated ? ` · updated ${new Date(m.updated).toLocaleDateString()}` : "")));
    }).concat(st.results.length < st.total ? [h("div", { class: "row mt-s" }, h("button", { class: "btn small", onclick: () => { st.offset += 20; search(true); } }, "Load more"))] : [])
      : [h("p", { class: "empty" }, "Nothing found. Try other words or fewer filters.")]);
    updateFooter();
  };
  const showDetails = async (m) => {
    st.active = `${m.source}:${m.id}`;
    renderList();
    fill(details, h("p", { class: "empty" }, `Loading ${m.name}…`));
    const p = await api(`${base}/project?source=${m.source}&id=${encodeURIComponent(m.id)}`).catch((e) => { toast(e.message, true); return null; });
    if (!p || st.active !== `${m.source}:${m.id}`) return;
    const key = `${p.source}:${p.id}`;
    const pick = kind === "mod" ? h("button", { class: "btn" + (st.selected.has(key) ? "" : " primary"), onclick: () => {
      if (st.selected.has(key)) st.selected.delete(key); else st.selected.set(key, m);
      renderList(); showDetails(m);
    } }, st.selected.has(key) ? "✓ Selected" : "Select") : null;
    const versionSel = kind === "modpack" && p.versions.length ? h("select", { "aria-label": "Modpack version" },
      p.versions.map((v) => h("option", { value: v.id }, `${v.name} · Minecraft ${v.minecraft.join(", ")} · ${v.loaders.join(", ")}`))) : null;
    const usePack = kind === "modpack" ? h("button", { class: "btn primary", disabled: !versionSel, onclick: () => {
      const v = p.versions.find((x) => x.id === versionSel.value);
      const packLoader = (v.loaders.find((l) => ["fabric", "neoforge", "forge", "quilt"].includes(l)) || "vanilla");
      const pack = { project: p.id, version_id: v.id, name: p.name, version: v.name, minecraft: v.minecraft[0], loader: packLoader, icon: p.icon };
      if (host) host.pickPack(pack);
      else { Object.assign(setupState, { modpack: pack, loader: pack.loader, minecraft: pack.minecraft }); location.hash = "#new"; }
    } }, "Use this modpack") : null;
    fill(details,
      h("div", { class: "browse-head" },
        p.icon ? h("img", { src: p.icon, alt: "", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "grow" }, h("h2", {}, p.name), h("div", { class: "muted" }, p.summary),
          h("div", { class: "muted small" }, `⬇ ${fmtNum(p.downloads)}`, p.follows ? ` · ♥ ${fmtNum(p.follows)}` : "",
            p.license ? ` · ${p.license}` : "", p.updated ? ` · updated ${new Date(p.updated).toLocaleDateString()}` : "")),
        h("div", { class: "row" }, pick,
          h("a", { class: "btn ghost", href: p.url, target: "_blank", rel: "noopener noreferrer" }, `Open on ${p.source === "curseforge" ? "CurseForge" : "Modrinth"} ↗`))),
      kind === "modpack" ? h("div", { class: "card mt-s" }, h("label", {}, "Version", versionSel || h("p", { class: "empty" }, "No versions.")),
        h("p", { class: "muted small" }, "The new server is set up with this pack's Minecraft version, mod loader, server mods and configs, and stays on that Minecraft version."),
        h("div", { class: "row mt-s" }, usePack)) : null,
      p.categories.length ? h("div", { class: "mt-s" }, p.categories.map((c) => h("span", { class: "tag" }, c))) : null,
      p.gallery.length ? h("div", { class: "gallery mt" }, p.gallery.map((g) => h("a", { href: g.url, target: "_blank", rel: "noopener noreferrer" },
        h("img", { src: g.url, alt: g.title || "", loading: "lazy", referrerpolicy: "no-referrer" })))) : null,
      Object.keys(p.links).length ? h("div", { class: "row mt-s small" }, Object.entries(p.links).map(([k, v]) =>
        h("a", { href: v, target: "_blank", rel: "noopener noreferrer" }, k.replace("_url", "").replace(/^./, (x) => x.toUpperCase()) + " ↗"))) : null,
      h("div", { class: "mt" }, richText(p.body, p.body_format)));
    details.scrollTop = 0;
  };

  addBtn.addEventListener("click", async () => {
    const mods = [...st.selected.values()].map((m) => ({ source: m.source, id: m.id, slug: m.slug, name: m.name,
      channel: m.channel && m.channel !== "release" ? m.channel : null }));
    if (!confirmEarly(mods)) return;
    if (forPlayers) {  // extras in the friends' download (their dependencies come along there)
      const cur = await api(`/api/servers/${encodeURIComponent(target)}/client`).catch(() => null);
      if (!cur) return;
      const r = await act(() => api(`/api/servers/${encodeURIComponent(target)}/client`, { method: "POST",
        body: { mods: [...new Set([...cur.mods, ...mods.map((m) => m.slug || m.id)])] } }), `Added ${mods.map((m) => m.name).join(", ")} for players`);
      if (r && host) host.changed();
      return;
    }
    if (target === "setup") {
      if (host) { host.addMods(mods); return; }
      for (const m of mods) setupAddMod(setupModKey(m), m.name, m.channel);
      location.hash = "#new";
      return;
    }
    const r = await act(() => api(`/api/servers/${encodeURIComponent(target)}/mods/add-many`, { method: "POST", body: { mods } }));
    if (!r) return;
    toast(`Added ${r.added.length} mod(s)` + (r.skipped.length ? `; skipped ${r.skipped.map((x) => `${x.name} (${x.reason})`).join(", ")}` : ""), r.skipped.length > 0);
    st.selected.clear(); renderList();
    if (host) host.changed();
  });
  q.addEventListener("input", () => { st.q = q.value.trim(); clearTimeout(timer); timer = setTimeout(() => search(), 350); });
  sort.addEventListener("change", () => { st.sort = sort.value; search(); });
  source.addEventListener("change", () => { st.source = source.value; st.category = ""; loadCategories(); search(); });
  category.addEventListener("change", () => { st.category = category.value; search(); });
  version.addEventListener("change", () => { st.version = version.value.trim(); search(); });
  const loadCategories = async () => {
    const r = await api(`${base}/categories?type=${kind}&source=${st.source}`).catch(() => null);
    fill(category, h("option", { value: "" }, "All categories"), r ? r.categories.map((c) => h("option", { value: c.id }, c.name)) : []);
  };

  const el = h("div", { class: "browse" },
    h("div", { class: "browse-left" },
      h("div", { class: "browse-filters" },
        h("div", { class: "row" }, h("strong", { class: "grow" }, forPlayers ? "Mods for players" : { modpack: "Modpacks", plugin: "Plugins", mod: "Mods" }[noun]),
          loader ? h("span", { class: "tag" }, loader) : null,
          host ? h("button", { class: "btn ghost small", onclick: () => host.close() }, "Close") : h("a", { class: "btn ghost small", href: target === "setup" ? "#new" : `#s/${target}/mods` }, "Back")),
        q,
        h("div", { class: "row" }, source, sort),
        h("div", { class: "row" }, category, version),
        earlyRow),
      list,
      h("div", { class: "browse-footer" }, count, addBtn)),
    details);
  return { el, start: () => { q.focus(); loadCategories(); search(); } };
}

// ------------------------------------------------------------ advanced settings
// Every other server.properties setting, grouped; edits `values` (key -> string) in place.
const WORLD_CARD_PROPS = ["level-seed", "level-type", "generate-structures", "hardcore"];
function propsEditor(schema, values) {
  const pretty = (c) => c.replace(/^minecraft:/, "").replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase());
  const field = (p) => {
    const set = (v) => { values[p.key] = String(v); };
    const hint = p.help ? h("span", { class: "muted small" }, p.help) : null;
    if (p.kind === "bool") {
      return h("label", { class: "row prop-bool" },
        h("input", { type: "checkbox", checked: values[p.key] === "true", onchange: (e) => set(e.target.checked) }),
        h("span", {}, p.label, hint ? h("br") : null, hint));
    }
    let input;
    if (p.kind === "int") input = h("input", { type: "number", min: p.min, max: p.max, value: values[p.key], oninput: (e) => set(e.target.value) });
    else if (p.kind === "choice") {
      input = h("select", { onchange: (e) => set(e.target.value) }, p.choices.map((c) => h("option", { value: c }, pretty(c))));
      input.value = values[p.key];
    } else input = h("input", { value: values[p.key], maxlength: p.max_len || null, oninput: (e) => set(e.target.value) });
    return h("label", {}, p.label, input, hint);
  };
  const groups = [...new Set(schema.map((p) => p.group))];
  return h("div", { class: "props" }, groups.map((g) => h("fieldset", {},
    h("legend", {}, g), h("div", { class: "grid" }, schema.filter((p) => p.group === g).map(field)))));
}
// Only the settings that differ from `base`, so untouched ones keep Minecraft's own defaults.
function changedProps(values, base) {
  return Object.fromEntries(Object.entries(values).filter(([k, v]) => v !== base[k]));
}

// ------------------------------------------------------------------ Discord
// Posting the invite to a channel, through the user's own bot (a webhook only reaches one
// channel). The first time, it walks through making the bot and adding it to a server.
function openDiscord(links) {
  if ($("#discord")) return;
  const body = h("div", {});
  const close = () => $("#discord").remove();
  document.body.append(h("div", { class: "modal-backdrop", id: "discord", role: "dialog", "aria-modal": "true", "aria-labelledby": "discord-title" },
    h("div", { class: "modal discord" },
      h("div", { class: "row" }, h("h2", { id: "discord-title", class: "grow" }, "Post the invite to Discord"),
        h("button", { class: "btn ghost small", onclick: close }, "Close")),
      body)));
  const ext = (href, text) => h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);

  const askToken = (info) => {
    const input = h("input", { type: "password", autocomplete: "off", placeholder: "Paste the bot token", "aria-label": "Bot token", class: "grow" });
    const save = h("button", { class: "btn primary", onclick: async () => {
      save.disabled = true;
      try {
        const r = await api("/api/hub/discord", { method: "POST", body: { token: input.value } });
        toast(`Connected as ${r.bot.name}`);
        load();
      } catch (e) { if (!(e instanceof Unauthorized)) toast(e.message, true); save.disabled = false; }
    } }, "Connect");
    fill(body,
      h("p", {}, "mcsm posts through a Discord bot that belongs to you. Setting one up takes a couple of minutes, once:"),
      h("ol", { class: "steps" },
        h("li", {}, "Open the ", ext(info.portal, "Discord Developer Portal ↗"), " and press ", h("strong", {}, "New Application"), ". Name it (e.g. “Minecraft server”)."),
        h("li", {}, "Open the ", h("strong", {}, "Bot"), " tab, press ", h("strong", {}, "Reset Token"), ", then ", h("strong", {}, "Copy"), "."),
        h("li", {}, "Paste the token here. mcsm checks it with Discord and keeps it in mcsm settings; it never leaves this computer otherwise.")),
      h("div", { class: "row" }, input, save),
      h("p", { class: "muted small" }, "The bot only needs to see channels and send messages. mcsm never reads messages, and its posts can't ping @everyone."));
    input.focus();
  };

  const pick = async (info) => {
    let guilds;
    try { guilds = (await api("/api/hub/discord/guilds")).guilds; }
    catch (e) { fill(body, h("div", { class: "notice bad" }, e.message), h("button", { class: "btn mt-s", onclick: () => askToken(info) }, "Use another bot token")); return; }
    const addBot = h("p", { class: "small" }, ext(info.invite_url, `Add ${info.bot.name} to a Discord server ↗`),
      " (you need “Manage Server” there), then ", h("button", { class: "link-btn", onclick: load }, "refresh the list"), ".");
    if (!guilds.length) {
      fill(body, h("div", { class: "notice" }, h("strong", {}, `${info.bot.name} isn't in any Discord server yet. `), "Add it to the one you want to post in:"), addBot);
      return;
    }
    const guildSel = h("select", { "aria-label": "Discord server" }, guilds.map((g) => h("option", { value: g.id }, g.name)));
    const chanSel = h("select", { "aria-label": "Channel" });
    const loadChannels = async () => {
      fill(chanSel, h("option", { value: "" }, "Loading…"));
      const r = await api(`/api/hub/discord/channels?guild=${guildSel.value}`).catch((e) => { toast(e.message, true); return null; });
      const chans = r ? r.channels : [];
      fill(chanSel, chans.length ? chans.map((c) => h("option", { value: c.id }, (c.category ? `${c.category} / ` : "") + "#" + c.name + (c.kind === "announcements" ? " (announcements)" : "")))
        : h("option", { value: "" }, "No text channels the bot can see"));
      if (chans.some((c) => c.id === info.channel)) chanSel.value = info.channel;
    };
    guildSel.addEventListener("change", loadChannels);
    if (guilds.some((g) => g.id === info.guild)) guildSel.value = info.guild;
    const name = (hubInfo && hubInfo.servers && (hubInfo.servers.find((x) => x.id === server) || {}).name) || "our Minecraft server";
    const message = h("textarea", { rows: 3, maxlength: 1800, "aria-label": "Message" },
      `${name} is up! Open the link, run the download, and it sets up Minecraft with everything you need to join.`);
    const useInternet = h("input", { type: "checkbox", checked: !!links.internet, disabled: !links.internet });
    const useLocal = h("input", { type: "checkbox", checked: !links.internet && !!links.local, disabled: !links.local });
    const post = h("button", { class: "btn primary", onclick: async () => {
      const chosen = [useInternet.checked ? "internet" : null, useLocal.checked ? "local" : null].filter(Boolean);
      if (!chanSel.value) { toast("Pick a channel", true); return; }
      if (!chosen.length) { toast("Pick at least one link to post", true); return; }
      post.disabled = true;
      try {
        await api("/api/client/discord", { method: "POST", body: { guild: guildSel.value, channel: chanSel.value, message: message.value, links: chosen } });
        toast(`Posted to #${chanSel.selectedOptions[0].textContent.split("#").pop().replace(/ \(.*$/, "")}`);
        close();
      } catch (e) { if (!(e instanceof Unauthorized)) toast(e.message, true); post.disabled = false; }
    } }, "Post");
    fill(body,
      h("div", { class: "grid" }, h("label", {}, "Discord server", guildSel), h("label", {}, "Channel", chanSel)),
      h("label", { class: "mt-s" }, "Message", message),
      h("div", { class: "mt-s" },
        h("label", { class: "row" }, useInternet, h("span", {}, "Internet link", links.internet ? "" : " (use your public IP on the Friends page first)")),
        h("label", { class: "row" }, useLocal, h("span", {}, "Local link (only works on this computer's network)"))),
      h("div", { class: "row mt" }, post, h("span", { class: "muted small grow" }, `Posting as ${info.bot.name}.`)),
      addBot);
    loadChannels();
  };

  const load = async () => {
    fill(body, h("p", { class: "muted" }, "Loading…"));
    const info = await api("/api/hub/discord").catch((e) => { fill(body, h("div", { class: "notice bad" }, e.message)); return null; });
    if (!info) return;
    if (info.set) pick(info); else askToken(info);
  };
  load();
}

// ------------------------------------------------------------ try before you buy
// "Test these mods": an instant check (builds for this version, declared conflicts), then,
// where a server can be started, a test boot in a throwaway server; if that fails, an
// offer to find the culprits by adding the mods back a group at a time.
//   opts.quick()      -> Promise of /check's result
//   opts.trial        -> body for POST /api/hub/trial (without bisect), or null (friends: no boot)
//   opts.keepWorking  -> called with the report, to drop the mods that don't work
function testButton(opts) {
  return h("button", { type: "button", class: "btn", onclick: () => openTester(opts) }, "🧪 Test these mods");
}
function openTester(opts) {
  if ($("#tester")) { $("#tester").classList.remove("hidden"); return; }
  const body = h("div", {});
  let poll = null, running = null, quickResult = null;
  const clock = (sec) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  // What it's doing right now: in the dialog, and in a toast that stays while the dialog is hidden.
  const stepText = h("span", { class: "grow" }, "Starting…");
  const bar = h("div", { class: "bar" }, h("span", { class: "bar-fill" }));
  const toastText = h("span", { class: "small" }, "Starting…");
  const toastBar = h("div", { class: "bar" }, h("span", { class: "bar-fill" }));
  const setStep = (text, fraction = null) => {
    stepText.textContent = toastText.textContent = text;
    for (const b of [bar, toastBar]) {
      b.classList.toggle("indeterminate", fraction === null);
      b.firstChild.style.width = fraction === null ? "" : `${Math.round(fraction * 100)}%`;
    }
  };
  const hide = () => $("#tester").classList.add("hidden");
  const show = () => $("#tester").classList.remove("hidden");
  const close = () => {
    if (running && !confirm("Stop the test?")) return;
    if (running) api("/api/hub/trial/cancel", { method: "POST", body: { id: running } }).catch(() => {});
    clearInterval(poll);
    $("#tester").remove();
    closeToast("tester-toast");
  };
  const finished = (text) => {
    closeToast("tester-toast");
    if (!$("#tester") || !$("#tester").classList.contains("hidden")) return;
    stickyToast("tester-toast", [h("strong", {}, "Mod test finished"), h("span", { class: "small" }, text),
      h("div", { class: "row mt-s" }, h("button", { class: "btn small primary", onclick: () => { closeToast("tester-toast"); show(); } }, "See the results"),
        h("button", { class: "btn small ghost", onclick: close }, "Close"))]);
  };
  const box = h("div", { class: "modal tester" },
    h("div", { class: "row" }, h("h2", { id: "tester-title", class: "grow" }, "Test these mods"),
      h("button", { class: "btn ghost small", title: "The test keeps going; its progress stays in a message at the bottom", onclick: hide }, "Keep working"),
      h("button", { class: "btn ghost small", onclick: close }, "Close")),
    h("div", { class: "tester-step" }, h("div", { class: "row" }, h("span", { class: "spinner" }), stepText,
      h("span", { class: "muted small tester-time" })), bar),
    body);
  document.body.append(h("div", { class: "modal-backdrop", id: "tester", role: "dialog", "aria-modal": "true", "aria-labelledby": "tester-title" }, box));
  const stepBox = box.querySelector(".tester-step");
  const idle = () => stepBox.classList.add("hidden");
  const busy = () => stepBox.classList.remove("hidden");
  stickyToast("tester-toast", [h("strong", {}, "Testing mods"),
    h("span", { class: "small" }, "This can take a long time: looking the mods up takes seconds, but a test boot takes a few minutes, and finding which mods break it can take much longer. You can keep using mcsm meanwhile."),
    toastText, toastBar,
    h("div", { class: "row mt-s" }, h("button", { class: "btn small", onclick: show }, "Show"),
      h("button", { class: "btn small ghost", onclick: hide }, "Hide the dialog"))]);

  const issues = (r) => [
    ...r.conflicts.map((c) => h("li", {}, h("strong", {}, c.mods.join(" + ")), h("div", { class: "small muted" }, c.reason))),
    ...r.problems.map((p) => h("li", {}, h("strong", {}, p.mod), h("div", { class: "small muted" }, p.reason)))];

  const runTrial = async (bisect) => {
    const log = h("pre", { class: "log" });
    busy();
    setStep(bisect ? "Finding which mods don't work together…" : "Test boot: installing the mods in a throwaway server and starting it…");
    fill(body, log, h("p", { class: "muted small" }, "Your servers aren't touched. The test server is deleted afterwards."));
    if (!$("#tester-toast")) stickyToast("tester-toast", [h("strong", {}, "Testing mods"), toastText, toastBar,
      h("div", { class: "row mt-s" }, h("button", { class: "btn small", onclick: show }, "Show"))]);
    let r;
    try { r = await api("/api/hub/trial", { method: "POST", body: { ...opts.trial, bisect } }); }
    catch (e) { idle(); fill(body, h("div", { class: "notice bad" }, e.message)); finished(e.message); return; }
    running = r.id;
    let seen = 0;
    poll = setInterval(async () => {
      const t = await api(`/api/hub/trial?id=${running}&since=${seen}`).catch(() => null);
      if (!t) return;
      seen = t.next;
      log.textContent += t.log.map((x) => x + "\n").join("");
      log.scrollTop = log.scrollHeight;
      box.querySelector(".tester-time").textContent = clock(t.elapsed);
      const last = t.log.filter((x) => x.startsWith("Test ")).pop();
      if (last) setStep(last + (bisect ? ` (${t.tests} test${t.tests === 1 ? "" : "s"} so far)` : ""));
      if (t.state === "running") return;
      clearInterval(poll);
      running = null;
      idle();
      report(t, log.textContent);
      const res = t.result || {};
      finished(t.state === "cancelled" ? "The test was stopped." : res.ok ? "✓ The server started with these mods."
        : res.bisected ? `${(res.outliers || []).length} mod(s) don't work.` : "✗ The server didn't start.");
    }, 1500);
  };

  const report = (t, logText) => {
    const res = t.result || {};
    const details = h("details", { class: "mt-s" }, h("summary", {}, "What was tested"), h("pre", { class: "log" }, logText));
    if (t.state === "cancelled") { fill(body, h("div", { class: "notice" }, "The test was stopped."), details); return; }
    if (res.ok) {
      fill(body, h("div", { class: "notice ok" }, h("strong", {}, "✓ It works. "),
        `The server started with ${res.working.length ? "all these mods" : "these settings"}` + (res.minecraft ? ` on Minecraft ${res.minecraft}.` : ".")), details);
      return;
    }
    const diag = res.diagnosis && res.diagnosis.summary;
    if (!res.bisected) {
      fill(body,
        h("div", { class: "notice bad" }, h("strong", {}, "✗ The server didn't start. "), diag || res.reason),
        h("p", {}, "mcsm can find which mods are the problem: it starts test servers with the mods added back a group at a time, splitting any group that fails, until it knows which mods work together."),
        h("div", { class: "notice warn" }, "This can take a while: each test starts a server (usually 1 to 3 minutes each), and a long mod list can need a dozen tests or more."),
        h("div", { class: "row mt-s" },
          h("button", { class: "btn primary", onclick: () => runTrial(true) }, "Find the culprits"),
          h("button", { class: "btn ghost", onclick: close }, "Not now")),
        details);
      return;
    }
    fill(body,
      res.outliers.length ? h("div", { class: "notice warn" }, h("strong", {}, `${res.working.length} of ${res.working.length + res.outliers.length} mods work together. `),
        "These don't:") : h("div", { class: "notice bad" }, res.reason),
      res.outliers.length ? h("ul", { class: "list" }, res.outliers.map((o) => h("li", {},
        h("div", { class: "grow" }, h("strong", {}, o.id), h("div", { class: "small muted" }, o.reason))))) : null,
      res.working.length ? h("div", { class: "mt-s" }, h("strong", {}, "Working together: "), res.working.join(", ")) : null,
      quickResult && (quickResult.conflicts.length || quickResult.problems.length) ? h("div", { class: "mt-s" },
        h("strong", {}, "Known problems (from the mods' own information):"), h("ul", { class: "list" }, issues(quickResult))) : null,
      opts.keepWorking && res.outliers.length ? h("div", { class: "row mt" },
        h("button", { class: "btn primary", onclick: async () => { await opts.keepWorking(res); clearInterval(poll); $("#tester").remove(); } },
          `Remove the ${res.outliers.length === 1 ? "mod that doesn't work" : `${res.outliers.length} mods that don't work`}`),
        h("button", { class: "btn ghost", onclick: close }, "Keep them for now")) : null,
      details);
  };

  // The quick check runs in the background too, so it can say which mod it's looking at.
  const quick = async () => {
    const [path, payload] = opts.check;
    setStep("Looking the mods up on Modrinth…");
    const started = await api(path, { method: "POST", body: { ...payload, background: true } });
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 500));
      if (!$("#tester")) throw new Error("closed");
      const j = await api(`/api/hub/mods/check?id=${started.id}`);
      box.querySelector(".tester-time").textContent = clock(j.elapsed);
      if (j.total) setStep(`Checked ${j.current} (${j.done} of ${j.total})`, j.done / j.total);
      if (j.state === "done") return j.result;
      if (j.state === "failed") throw new Error(j.error || "the check failed");
    }
  };

  (async () => {
    let r;
    try { r = await quick(); } catch (e) { idle(); fill(body, h("div", { class: "notice bad" }, e.message)); finished(e.message); return; }
    idle();
    quickResult = r;
    const found = issues(r);
    fill(body,
      found.length ? [h("div", { class: "notice warn" }, h("strong", {}, "Found problems before starting anything:")), h("ul", { class: "list" }, found)]
        : h("div", { class: "notice ok" }, h("strong", {}, "✓ No known problems. "),
          `${r.mods.length} mod(s) have builds for ${r.minecraft ? `Minecraft ${r.minecraft}` : "this Minecraft"}, and none say they conflict with another.`),
      opts.trial ? [
        h("p", { class: "mt" }, "To be sure, mcsm can start a throwaway server with these mods and see if Minecraft loads. It takes a few minutes; your servers aren't touched."),
        h("div", { class: "row" }, h("button", { class: "btn primary", onclick: () => runTrial(false) }, "Start a test boot"))]
        : h("p", { class: "muted small mt" }, "These mods run on players' computers, and a game can't be started here to try them, so this checks versions and known conflicts. " +
          "The server's own mods can be test-booted on its Mods page."));
    const summary = found.length ? `Found ${found.length} problem(s).` : "✓ No known problems.";
    if (opts.trial) setStep(`${summary} Next: a test boot, which takes a few minutes.`, 1);  // the toast stays till you're done
    else finished(summary);
  })();
}

// ------------------------------------------------------------- code editor
// A config file editor with IDE-style colours: a transparent <textarea> typed into over a
// highlighted copy of the same text. Each language is a list of [sticky regex, token class]
// tried in order at every position; anything unmatched is plain text.
const STR = /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'/y;
const NUM = /[+-]?(?:0x[0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[dDfFlLbBsS]?)(?![\w.])/y;
const HIGHLIGHT = {
  toml: [[/#.*/y, "c"], [/^[ \t]*\[\[?[^\]\n]*\]\]?/my, "h"], [/"""[\s\S]*?"""|'''[\s\S]*?'''/y, "s"], [STR, "s"],
    [/(?:true|false)(?![\w-])/y, "b"], [NUM, "n"], [/[A-Za-z0-9_.-]+(?=[ \t]*=)/y, "k"], [/[=,{}[\]]/y, "p"]],
  json: [[/\/\/.*|\/\*[\s\S]*?\*\//y, "c"], [/"(?:\\.|[^"\\\n])*"(?=\s*:)/y, "k"], [STR, "s"],
    [/(?:true|false|null)(?!\w)/y, "b"], [NUM, "n"], [/[A-Za-z_$][\w$]*(?=\s*:)/y, "k"], [/[{}[\],:]/y, "p"]],
  snbt: [[STR, "s"], [/[A-Za-z_][\w.+-]*(?=\s*:)/y, "k"], [/(?:true|false)(?!\w)/y, "b"], [NUM, "n"], [/[{}[\],:;]/y, "p"]],
  yaml: [[/#.*/y, "c"], [/^---|^\.\.\./my, "h"], [/^[ \t]*(?:- +)?[^\s#:'"][^:#\n]*?(?=:(?:\s|$))/my, "k"],
    [STR, "s"], [/(?:true|false|yes|no|on|off|null|~)(?![\w-])/iy, "b"], [NUM, "n"], [/[:\-|>[\]{},&*!]/y, "p"]],
  properties: [[/^[ \t]*[#!].*/my, "c"], [/^[ \t]*[^=:\s#!][^=:\n]*?(?=[ \t]*[=:])/my, "k"], [/(?:true|false)(?![\w-])/y, "b"],
    [NUM, "n"], [/[=:]/y, "p"]],
  ini: [[/^[ \t]*[;#].*/my, "c"], [/^[ \t]*\[[^\]\n]*\]/my, "h"], [/^[ \t]*[^=\s;#[][^=\n]*?(?=[ \t]*=)/my, "k"], [STR, "s"],
    [/(?:true|false)(?![\w-])/iy, "b"], [NUM, "n"], [/=/y, "p"]],
  // Forge's old .cfg: "B:name=true", "S:name=text", lists in < >, blocks in { }
  cfg: [[/#.*/y, "c"], [/^[ \t]*[\w. -]+(?=[ \t]*\{)/my, "h"], [/[BISD]:/y, "t"], [/"[^"\n]*"(?=[ \t]*[=<])|[\w.-]+(?=[ \t]*[=<])/y, "k"],
    [STR, "s"], [/(?:true|false)(?![\w-])/y, "b"], [NUM, "n"], [/[=<>{}]/y, "p"]],
  text: [[/^[ \t]*#.*/my, "c"]],
};
function highlight(text, lang) {
  const rules = HIGHLIGHT[lang] || HIGHLIGHT.text;
  const out = [];
  let i = 0, plain = "";
  const flush = () => { if (plain) { out.push(plain); plain = ""; } };
  while (i < text.length) {
    let hit = null;
    for (const [re, cls] of rules) {
      re.lastIndex = i;
      const m = re.exec(text);
      if (m && m[0].length) { hit = [m[0], cls]; break; }
    }
    if (hit) { flush(); out.push(h("span", { class: "tk-" + hit[1] }, hit[0])); i += hit[0].length; continue; }
    // plain text: a whole word at once (a word can't start a token midway), else one character
    const word = /[A-Za-z_]\w*/y;
    word.lastIndex = i;
    const w = /[A-Za-z_]/.test(text[i]) ? word.exec(text) : null;
    const take = w ? w[0] : text[i];
    plain += take;
    i += take.length;
  }
  flush();
  return out;
}

function codeEditor(text, lang, onChange) {
  const pre = h("pre", { class: "code-hl", "aria-hidden": "true" });
  const gutter = h("div", { class: "code-gutter", "aria-hidden": "true" });
  const input = h("textarea", { class: "code-input", spellcheck: "false", autocapitalize: "off", autocomplete: "off", wrap: "off", "aria-label": "File contents" });
  input.value = text;
  let lines = 0, frame = 0;
  const sync = () => { pre.scrollTop = input.scrollTop; pre.scrollLeft = input.scrollLeft; gutter.scrollTop = input.scrollTop; };
  const paint = () => {
    frame = 0;
    fill(pre, highlight(input.value, lang), "\n");  // the extra line keeps the last one visible
    const n = input.value.split("\n").length;
    if (n !== lines) { lines = n; gutter.textContent = Array.from({ length: n }, (_, k) => k + 1).join("\n") + "\n"; }
    sync();
  };
  input.addEventListener("input", () => { if (!frame) frame = requestAnimationFrame(paint); onChange(); });
  input.addEventListener("scroll", sync);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {  // indent instead of leaving the editor
      e.preventDefault();
      input.setRangeText("  ", input.selectionStart, input.selectionEnd, "end");
      input.dispatchEvent(new Event("input"));
    }
  });
  paint();
  const el = h("div", { class: "code-editor" }, gutter, h("div", { class: "code-wrap" }, pre, input));
  return { el, input, get value() { return input.value; }, set value(v) { input.value = v; paint(); } };
}

// The config files of one mod (or all of them), in a big dialog: a file list and the editor.
function openConfigEditor(title, files, first) {
  if ($("#config-editor")) return;
  let current = null;   // { path, text, modified, format }
  let editor = null;
  let dirty = false;
  const list = h("ul", { class: "cfg-files" });
  const pane = h("div", { class: "cfg-pane" }, h("p", { class: "empty" }, "Pick a file."));
  const status = h("span", { class: "muted small grow" });
  const saveBtn = h("button", { class: "btn primary small", disabled: true }, "Save");
  const revertBtn = h("button", { class: "btn small", disabled: true }, "Revert");
  const keys = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    if (e.key === "Escape") close();
  };
  const close = () => {
    if (dirty && !confirm("Close without saving your changes?")) return;
    $("#config-editor").remove();
    document.removeEventListener("keydown", keys);
  };
  const checkJson = () => {
    if (!current || !current.path.endsWith(".json")) return "";
    try { JSON.parse(editor.value); return ""; } catch (e) { return `JSON problem: ${e.message}`; }
  };
  const showPos = () => {
    if (!editor) return;
    const upto = editor.input.value.slice(0, editor.input.selectionStart).split("\n");
    const problem = checkJson();
    status.className = "small grow " + (problem ? "bad-text" : "muted");
    status.textContent = problem || `Line ${upto.length}, column ${upto[upto.length - 1].length + 1} · ${current.format.toUpperCase()}` +
      (dirty ? " · unsaved changes" : "");
  };
  const renderList = () => fill(list, files.map((path) => h("li", {},
    h("button", { type: "button", class: current && current.path === path ? "active" : null, title: path, onclick: () => load(path) },
      path, current && current.path === path && dirty ? " •" : ""))));
  const setDirty = (v) => { dirty = v; saveBtn.disabled = !v; revertBtn.disabled = !v; renderList(); };
  const load = async (path) => {
    if (dirty && !confirm("Switch files without saving your changes?")) return;
    const f = await api(`/api/configs/file?path=${encodeURIComponent(path)}`).catch((e) => { toast(e.message, true); return null; });
    if (!f) return;
    current = f;
    editor = codeEditor(f.text, f.format, () => { setDirty(editor.value !== current.text); showPos(); });
    editor.input.addEventListener("keyup", showPos);
    editor.input.addEventListener("click", showPos);
    fill(pane, editor.el);
    setDirty(false);
    showPos();
    editor.input.focus();
  };
  const save = async () => {
    if (!current || !dirty) return;
    const problem = checkJson();
    if (problem && !confirm(`${problem}\n\nSave anyway?`)) return;
    try {
      const r = await api("/api/configs/file", { method: "POST", body: { path: current.path, text: editor.value, modified: current.modified } });
      current = { ...current, text: editor.value, modified: r.modified };
      setDirty(false);
      showPos();
      toast(r.running ? "Saved. Restart the server for it to take effect (some mods reload their config by themselves)." : "Saved.");
    } catch (e) { if (!(e instanceof Unauthorized)) toast(e.message, true); }
  };
  saveBtn.addEventListener("click", save);
  revertBtn.addEventListener("click", () => { if (current && confirm("Undo your unsaved changes?")) { editor.value = current.text; setDirty(false); showPos(); } });
  renderList();
  const box = h("div", { class: "modal editor-modal" },
    h("div", { class: "row" }, h("h2", { id: "config-editor-title", class: "grow" }, title), folderBtn("config", "Config folder"),
      h("button", { class: "btn ghost small", onclick: close }, "Close")),
    h("div", { class: "cfg-body" + (files.length > 1 ? "" : " single") }, files.length > 1 ? list : null, pane),
    h("div", { class: "row mt-s" }, status, revertBtn, saveBtn));
  document.body.append(h("div", { class: "modal-backdrop", id: "config-editor", role: "dialog", "aria-modal": "true", "aria-labelledby": "config-editor-title" }, box));
  document.addEventListener("keydown", keys);
  if (first || files.length) load(first || files[0]);
}

// ------------------------------------------------------------- pick a world
// An existing world: a .zip uploaded from here, or a singleplayer world on the server's
// computer (Minecraft Launcher, Prism, Modrinth App, CurseForge). Calls onPick with
// { world, name, version } where `world` is what the API takes.
function pickWorld(onPick) {
  if ($("#pick-world")) return;
  const close = () => { const m = $("#pick-world"); if (m) m.remove(); };
  const list = h("div", {}, h("p", { class: "muted" }, "Looking for worlds…"));
  const note = h("p", { class: "muted small" });
  const picker = h("input", { type: "file", accept: ".zip", class: "hidden" });
  const choose = (w) => { close(); onPick(w); };
  picker.addEventListener("change", async () => {
    const f = picker.files[0];
    picker.value = "";
    if (!f) return;
    try {
      const r = await upload(`/api/hub/stage?filename=${encodeURIComponent(f.name.replace(/[^A-Za-z0-9 ()\[\]+_.,'-]/g, "_"))}`, f,
        (done) => { note.textContent = `Uploading ${f.name}: ${Math.round(done * 100)}%`; });
      choose({ world: r.id, name: f.name.replace(/\.zip$/i, ""), version: null });
    } catch (e) { if (!(e instanceof Unauthorized)) { note.textContent = ""; toast(e.message, true); } }
  });
  const box = h("div", { class: "modal" },
    h("h2", { id: "pick-world-title" }, "Choose a world"),
    h("h3", {}, "Upload a world"),
    h("p", { class: "muted small" }, "A .zip of a world folder (the folder with level.dat in it). On Windows: right-click the folder → Send to → Compressed (zipped) folder."),
    h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: () => picker.click() }, "Upload a .zip…"), picker), note,
    h("h3", { class: "mt" }, "Worlds on ", hubInfo && hubInfo.local ? "this computer" : "the server's computer"),
    list,
    h("div", { class: "row mt" }, h("button", { class: "btn ghost", type: "button", onclick: close }, "Cancel")));
  document.body.append(h("div", { class: "modal-backdrop", id: "pick-world", role: "dialog", "aria-modal": "true", "aria-labelledby": "pick-world-title" }, box));
  api("/api/hub/saves").then((r) => {
    fill(list, r.worlds.length ? h("ul", { class: "list worlds" }, r.worlds.map((w) => h("li", {},
      w.icon ? h("img", { src: w.icon, alt: "" }) : h("div", { class: "noicon" }),
      h("div", { class: "grow" }, h("strong", {}, w.name), w.hardcore ? h("span", { class: "tag bad" }, "hardcore") : null,
        h("div", { class: "small muted" }, [w.launcher, w.version ? `Minecraft ${w.version}` : null, `played ${fmtTime(w.played)}`].filter(Boolean).join(" · "))),
      h("button", { class: "btn small primary", type: "button", onclick: () => choose({ world: "save:" + w.id, name: w.name, version: w.version }) }, "Use"))))
      : h("p", { class: "empty" }, "No singleplayer worlds found. Upload one as a .zip instead."));
  }).catch((e) => fill(list, h("p", { class: "empty" }, e.message)));
}

// ------------------------------------------------------------ delete a server
function deleteServer(s, after) {
  if ($("#delete-server")) return;
  let everything = false;
  const box = h("div", { class: "modal compact" });
  const close = () => { const m = $("#delete-server"); if (m) m.remove(); };
  const render = (error) => {
    const typed = h("input", { autocomplete: "off", placeholder: s.name });
    const go = async (e) => {
      e.preventDefault();
      if (everything && typed.value.trim() !== s.name) return render(`Type the server's name, ${s.name}, to delete everything.`);
      try {
        const r = await api("/api/hub/delete", { method: "POST", body: { id: s.id, delete_files: everything } });
        close();
        toast(`${s.name}: ${r.message}`);
        if (after) after();
      } catch (err) { if (!(err instanceof Unauthorized)) render(err.message); }
    };
    const choice = (value, title, desc) => h("button", { type: "button", class: "choice" + (everything === value ? " selected" : "") + (value ? " danger" : ""),
      onclick: () => { everything = value; render(); } }, h("strong", {}, title), h("span", { class: "small muted" }, desc));
    fill(box,
      h("h2", { id: "delete-title" }, `Delete ${s.name}?`),
      s.state === "running" || s.state === "starting" ? h("div", { class: "notice warn" }, "Stop the server first.") : null,
      h("p", {}, "Would you also like to delete its world, mods and everything else that belongs to it?"),
      h("div", { class: "choices" },
        choice(false, "Keep the files", `Take it off the list; the world and mods stay in ${s.folder}.`),
        choice(true, "Delete everything", "The world, mods, backups and settings are erased. This can't be undone.")),
      h("form", { class: "mt", onsubmit: go },
        everything ? h("label", {}, `Type ${s.name} to confirm`, typed) : null,
        h("p", { class: "error" }, error || ""),
        h("div", { class: "row" },
          h("button", { class: "btn danger", type: "submit" }, everything ? "Delete everything" : "Remove from the list"),
          h("button", { class: "btn ghost", type: "button", onclick: close }, "Cancel"))));
    if (everything) typed.focus();
  };
  render();
  document.body.append(h("div", { class: "modal-backdrop", id: "delete-server", role: "dialog", "aria-modal": "true", "aria-labelledby": "delete-title" }, box));
}

// The home page: every server, each started and stopped by hand.
views.servers = () => {
  const list = h("div", { class: "server-list" });
  const busy = new Set();
  const control = async (s, action) => {
    if (action === "stop" && s.players && !confirm(`Stop ${s.name}? ${s.players} player(s) will be disconnected.`)) return;
    busy.add(s.id);
    await act(() => api(`/api/servers/${s.id}/server/${action}`, { method: "POST" }),
      action === "start" ? `Starting ${s.name}…` : `Stopping ${s.name}…`);
    busy.delete(s.id);
  };
  const render = (hb) => {
    if (!hb) return;
    const label = (s) => s.state === "unavailable" ? "unavailable" : s.setup_pending ? "not set up" : s.state;
    fill(list,
      hb.servers.map((s) => h("div", { class: "card server-card" },
        h("div", { class: "row" },
          h("span", { class: "pill " + (s.setup_pending ? "pending" : s.state) }, label(s)),
          h("strong", { class: "grow server-name" }, s.name)),
        h("div", { class: "muted" }, s.problem || (s.minecraft ? `Minecraft ${s.minecraft} · ${s.loader}` : `${s.loader} · not installed yet`)),
        s.state === "unavailable" || s.setup_pending ? null
          : h("div", { class: "muted small" }, `${s.players} / ${s.max_players} players · port ${s.port}`, s.update ? " · update ready" : ""),
        s.job ? h("div", { class: "row small" }, h("span", { class: "spinner" }), `${s.job.name}…`) : null,
        s.state === "unavailable" ? null : h("div", { class: "row mt-s" },
          s.setup_pending ? h("a", { class: "btn primary", href: `#s/${s.id}/setup` }, s.job ? "See progress" : "Finish setup")
            : s.state === "stopped"
              ? h("button", { class: "btn primary", disabled: !!s.job || busy.has(s.id), onclick: () => control(s, "start") }, "Start")
              : h("button", { class: "btn danger", disabled: busy.has(s.id), onclick: () => control(s, "stop") }, "Stop"),
          s.setup_pending ? null : h("a", { class: "btn", href: `#s/${s.id}/dashboard` }, "Open"),
          !hb.single && !s.job ? h("button", { class: "btn ghost", onclick: () => deleteServer(s, refreshStatus) }, "Delete") : null),
        h("div", { class: "row" }, h("div", { class: "muted small folder grow" }, s.folder), folderBtn("server", "Folder", s.id)))),
      hb.single ? null : h("a", { class: "card server-card new", href: "#new" },
        h("strong", {}, "+ New server"), h("span", { class: "muted small" }, "Pick a server type, Minecraft version and mods")));
  };
  // Import a server exported on another computer (Settings → Export).
  const picker = h("input", { type: "file", accept: ".zip", class: "hidden" });
  const importNote = h("span", { class: "muted small" });
  const importBtn = h("button", { class: "btn", onclick: () => picker.click() }, "Import a server…");
  picker.addEventListener("change", async () => {
    const f = picker.files[0];
    picker.value = "";
    if (!f) return;
    importBtn.disabled = true;
    try {
      const staged = await upload(`/api/hub/stage?filename=${encodeURIComponent(f.name.replace(/[^A-Za-z0-9 ()\[\]+_.,'-]/g, "_"))}`, f,
        (done) => { importNote.textContent = `Uploading ${f.name}: ${Math.round(done * 100)}%`; });
      importNote.textContent = "Unpacking…";
      const r = await api("/api/hub/import", { method: "POST", body: { id: staged.id } });
      toast("Imported. Press Start when you're ready.");
      await refreshStatus();
      location.hash = `#s/${r.id}/dashboard`;
    } catch (e) {
      if (!(e instanceof Unauthorized)) toast(e.message, true);
    } finally {
      importBtn.disabled = false;
      importNote.textContent = "";
    }
  });
  fill($("#main"),
    h("div", { class: "row mb" },
      h("p", { class: "muted grow" }, "Servers only run when you start them here, and stop when you press Stop or close mcsm."),
      hubInfo && hubInfo.single ? null : h("div", { class: "row" }, importNote, importBtn, picker)),
    list);
  render(hubInfo);
  return { onHub: render };
};

// mcsm itself: sign-in, network access, and what mcsm is.
views.mcsm = () => {
  const security = h("div", { class: "mb" });
  const network = h("div", { class: "mb" });
  const sharing = h("div", { class: "mb" });
  let sharingDrawn = false;
  const renderSharing = (hb) => {
    if (!hb || hb.single || !hb.share) { fill(sharing); return; }
    if (sharingDrawn) return;  // don't wipe what's being typed on every refresh
    sharingDrawn = true;
    const s = hb.share;
    const address = h("input", { value: s.address, placeholder: s.lan_ip ? `automatic (${s.lan_ip} on your network)` : "automatic" });
    const port = h("input", { type: "number", min: 1024, max: 65535, value: s.port });
    fill(sharing, card("Sharing with friends",
      h("p", { class: "muted small" }, "Used by servers whose friend download is switched on (see each server's Friends page)."),
      h("div", { class: "grid" },
        h("label", {}, "Your public address (host name or IP)", address,
          h("span", { class: "muted small" }, "What friends outside your home network use to reach you. Leave empty to use the address in the link they opened.")),
        h("label", {}, "Download port", port, h("span", { class: "muted small" }, "Forward this TCP port on your router, too."))),
      h("div", { class: "row mt-s" }, h("button", { class: "btn primary", onclick: async () => {
        const r = await act(() => api("/api/hub/share", { method: "POST", body: { address: address.value.trim(), port: Number(port.value) } }), "Saved");
        if (r && r.share.error) toast(r.share.error, true);
      } }, "Save"),
      h("span", { class: "muted small" }, s.running ? `Sharing is on (port ${s.port}).` : s.error || "Sharing is off: no server has a friend download switched on."))));
  };
  const renderSecurity = (hb) => {
    const a = (hb && hb.auth) || {};
    const label = { password: "Password", pin: "PIN", none: "No password (this computer only)" }[a.mode] || "…";
    fill(security, card("Sign-in",
      h("div", { class: "row" },
        h("span", { class: "grow" }, a.managed ? "Password set in mcsm.toml ([web] password)" : a.default ? "Default password (PASSWORD) — please change it" : label),
        a.managed ? null : h("button", { class: "btn", onclick: () => showSecurity(false) }, "Change"))));
    if (!hb || hb.single) { fill(network); return; }
    const box = h("input", { type: "checkbox", checked: hb.network_access, onchange: async (e) => {
      const r = await act(() => api("/api/hub/network", { method: "POST", body: { enabled: e.target.checked } }));
      if (r) toast(r.restart_needed ? "Saved. Close and reopen mcsm for this to take effect." : "Saved");
    } });
    fill(network, card("Network access",
      h("label", { class: "row" }, box, h("span", {}, "Let other devices on my network (like my phone) open this control panel")),
      h("p", { class: "muted small" }, `Servers are kept in ${hb.home}. This applies the next time mcsm starts.`)));
  };
  const about = h("div", { class: "mt" });
  const loadAbout = async () => {
    const [n, lic] = await Promise.all([api("/api/notice").catch(() => null), api("/api/licenses").catch(() => null)]);
    if (!n || !lic) return;
    const s = hubInfo || {};
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
          } }, "Check for mcsm updates"), s.single ? null : folderBtn("home", "mcsm folder", null, "btn"))),
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
  // CurseForge's API key (for CurseForge mods and searching CurseForge)
  const cf = h("div", { class: "mb" });
  const renderCf = async () => {
    if (hubInfo && hubInfo.single) return;
    const r = await api("/api/hub/curseforge").catch(() => null);
    if (!r) return;
    const input = h("input", { type: "password", placeholder: r.own ? "•••••••• (saved)" : "Paste your CurseForge API key", autocomplete: "off", "aria-label": "CurseForge API key" });
    const saveKey = (key, msg) => act(() => api("/api/hub/curseforge", { method: "POST", body: { key } }), msg).then(renderCf);
    fill(cf, card("CurseForge",
      h("p", { class: "muted small" }, "Needed to search CurseForge and to use CurseForge mods (Modrinth works without it). Get a free key at ",
        h("a", { href: "https://console.curseforge.com/", target: "_blank", rel: "noopener noreferrer" }, "console.curseforge.com ↗"), " → API keys."),
      h("div", { class: "row" }, input,
        h("button", { class: "btn primary", onclick: () => input.value.trim() && saveKey(input.value, "CurseForge key saved") }, r.own ? "Replace key" : "Save key"),
        r.own ? h("button", { class: "btn ghost", onclick: () => confirm("Remove the CurseForge key?") && saveKey("", "CurseForge key removed") }, "Remove") : null),
      r.own ? h("p", { class: "small ok-text" }, "✓ Your own key is saved.")
        : r.builtin ? h("p", { class: "small ok-text" }, "✓ This version of mcsm has CurseForge built in. You only need your own key if CurseForge starts refusing requests.")
        : null));
  };
  renderCf();
  // The Discord bot that posts invites (set up from a server's Friends page)
  const dc = h("div", { class: "mb" });
  const renderDc = async () => {
    if (hubInfo && hubInfo.single) return;
    const r = await api("/api/hub/discord").catch(() => null);
    if (!r) return;
    fill(dc, card("Discord",
      h("p", { class: "muted small" }, "Post your friends' invite to a Discord channel from a server's Friends page (“Post to Discord”)."),
      r.set ? h("div", { class: "row" }, h("span", { class: "grow small ok-text" }, `✓ Connected as ${r.bot ? r.bot.name : "your bot"}.`),
        r.invite_url ? h("a", { class: "btn small", href: r.invite_url, target: "_blank", rel: "noopener noreferrer" }, "Add it to another Discord server ↗") : null,
        h("button", { class: "btn ghost small", onclick: () => confirm("Disconnect the Discord bot? (It stays in your Discord servers until you remove it there.)") &&
          act(() => api("/api/hub/discord", { method: "POST", body: { token: "" } }), "Discord bot disconnected").then(renderDc) }, "Disconnect"))
        : h("p", { class: "small" }, "Not set up. Use “Post to Discord” on a server's Friends page to connect a bot.")));
  };
  renderDc();
  fill($("#main"), security, network, sharing, cf, dc, about);
  renderSecurity(hubInfo);
  renderSharing(hubInfo);
  loadAbout();
  return { onHub: (hb) => { renderSecurity(hb); renderSharing(hb); } };
};

// ------------------------------------------------------------------- setup
// Kept outside the view so choices survive re-renders and a failed attempt.
const setupState = { friends: false, loader: null, minecraft: "latest", mods: new Map(), motd: "A Minecraft server", properties: null, advancedOpen: false,
  max_players: 20, difficulty: "normal", gamemode: "survival", port: 25565, memory_gb: null,
  network_access: null, accept_eula: false, submitted: false, prefilled: false, modpack: null, localMods: [], world: null };

// A mod picked in setup brings the mods it needs along (marked "needed by ..."). Entries:
// key -> { name, required, explicit (picked by you), by: Set(keys of mods that need it), bad }.
// Removing a mod removes the dependencies nothing else needs; removing a dependency removes
// the mods that need it (after asking).
const setupModKey = (m) => (m.source === "curseforge" ? `curseforge:${m.id}` : m.slug || m.id);
function setupChanged() { if (setupState.onChange) setupState.onChange(); }
const earlyChannels = () => Object.fromEntries([...setupState.mods].filter(([, m]) => m.channel).map(([k, m]) => [k, m.channel]));
function setupModVersion() {
  const st = setupState;
  return st.minecraft === "latest" ? (st.newest || "") : st.minecraft;
}
// Mods with only alpha/beta builds for this version: shown on request, added after a warning.
const EARLY_WARNING = "Early builds (alpha and beta) are unfinished: they can crash the server, break other mods " +
  "or damage your world. Back up before you rely on them.";
const channelTag = (c) => (c && c !== "release" ? h("span", { class: "tag warn", title: EARLY_WARNING }, `${c} only`) : null);
function confirmEarly(mods) {
  const early = mods.filter((m) => m.channel && m.channel !== "release");
  return !early.length || confirm(`${early.map((m) => m.name).join(", ")} ${early.length === 1 ? "only has" : "only have"} ` +
    `alpha or beta builds for this Minecraft version.\n\n${EARLY_WARNING}\n\nAdd ${early.length === 1 ? "it" : "them"} anyway?`);
}
async function setupAddMod(key, name, channel = null) {
  const st = setupState;
  const e = st.mods.get(key);
  if (e) { e.explicit = true; if (channel && channel !== "release") e.channel = channel; }
  else st.mods.set(key, { name, required: true, explicit: true, by: new Set(), bad: "", channel: channel && channel !== "release" ? channel : null });
  setupChanged();
  await setupCheckMod(key);
}
async function setupCheckMod(key, quiet = false) {
  const st = setupState;
  const e = st.mods.get(key);
  if (!e || !st.loader || key.startsWith("curseforge:")) return;
  const v = setupModVersion();
  const r = await api(`/api/hub/mods/requires?id=${encodeURIComponent(key)}&loader=${encodeURIComponent(st.loader)}` +
    (v ? `&version=${encodeURIComponent(v)}` : "") + (e.channel ? `&channel=${e.channel}` : "")).catch(() => null);
  if (!r || st.mods.get(key) !== e) return;
  e.name = r.project.name;
  e.bad = r.compatible ? "" : r.reason;
  const added = [];
  for (const d of r.deps) {
    const dk = d.slug || d.id;
    if (!st.mods.has(dk)) { st.mods.set(dk, { name: d.name, required: e.required, explicit: false, by: new Set(), bad: "", channel: e.channel }); added.push(d); }
    st.mods.get(dk).by.add(key);
  }
  if (added.length && !quiet) {
    toast(`Added ${added.map((d) => d.name).join(", ")} because ${added.length === 1 ? "it's" : "they're"} needed by ` +
      [...new Set(added.map((d) => d.needed_by))].join(" and ") + ".");
  }
  setupChanged();
}
function setupRemoveMod(key) {
  const st = setupState;
  const e = st.mods.get(key);
  if (!e) return;
  const needers = [...e.by].filter((k) => st.mods.has(k));
  if (needers.length && !confirm(`${e.name} is needed by ${needers.map((k) => st.mods.get(k).name).join(", ")}. ` +
    `Remove ${needers.length === 1 ? "that" : "those"} too?`)) return;
  const stays = new Map();  // dependency name -> the mods that still need it
  const drop = (k) => {
    const x = st.mods.get(k);
    if (!x) return;
    st.mods.delete(k);
    stays.delete(x.name);
    for (const n of x.by) drop(n);  // what needs it goes with it
    for (const [ok, o] of [...st.mods]) {
      if (!o.by.delete(k)) continue;
      if (!o.explicit && o.by.size === 0) drop(ok);  // nothing needs it any more
      else if (!o.explicit) stays.set(o.name, [...o.by].map((b) => (st.mods.get(b) || {}).name).filter(Boolean));
    }
  };
  drop(key);
  for (const [name, needers] of stays) {
    if (needers.length) toast(`${name} stays: ${needers.join(" and ")} ${needers.length === 1 ? "needs" : "need"} it too.`);
  }
  setupChanged();
}
function setupRecheckMods() {
  // The Minecraft version or server type changed: dependencies and compatibility may differ.
  const st = setupState;
  for (const [k, e] of [...st.mods]) { if (!e.explicit) st.mods.delete(k); else e.by.clear(); }
  for (const k of st.mods.keys()) setupCheckMod(k, true);
}

// A failed job's message, with where its details were written (and a button to open that folder).
function failureText(message, sid) {
  const [what, where] = String(message || "").split("\nThe details are in ");
  if (!where) return what;
  return h("span", {}, what, h("div", { class: "small mt-s" }, "The details are in ", h("code", { class: "path" }, where),
    hubInfo && hubInfo.local && sid ? [" ", h("button", { type: "button", class: "link-btn", onclick: () =>
      api(`/api/servers/${sid}/open`, { method: "POST", body: { what: "reports" } }).catch((e) => toast(e.message, true)) }, "Open the folder")] : null));
}

// ------------------------------------------------------ setup progress dock
// While a new server installs you can go elsewhere (e.g. set up its friend download): its
// progress keeps going in a bar docked at the bottom of the window.
let dock = null;  // { sid, name, seq, el, timer }
function dockSetup(sid, name) {
  undock();
  const msg = h("span", { class: "grow dock-msg" }, "Starting…");
  const el = h("div", { class: "dock", id: "dock", role: "status" },
    h("span", { class: "spinner" }), h("strong", {}, `Creating ${name}`), msg,
    h("a", { class: "btn small", href: `#s/${sid}/setup` }, "Show"));
  document.body.append(el);
  requestAnimationFrame(() => el.classList.add("in"));
  dock = { sid, name, seq: 0, el, msg };
  const poll = async () => {
    if (!dock || dock.sid !== sid) return;
    el.classList.toggle("hidden", currentName === "setup" && server === sid);  // the full page shows it already
    const ev = await api(`/api/servers/${sid}/events?since=${dock.seq}`).catch(() => null);
    if (ev && ev.events.length) { dock.seq = ev.last; msg.textContent = ev.events[ev.events.length - 1].message; }
    const st = await api(`/api/servers/${sid}/status`).catch(() => null);
    if (!st || st.job || !st.last_job || st.last_job.name !== "set up server") return;
    clearInterval(dock.timer);
    dock.done = true;
    if (currentName === "friends" && server === sid) route();  // drop the "still installing" note
    el.classList.add(st.last_job.ok ? "done" : "failed");
    fill(el, h("strong", {}, st.last_job.ok ? `✓ ${name} is ready` : `${name}: setup didn't finish`),
      h("span", { class: "grow dock-msg" }, st.last_job.ok ? "Press Start when you want to play." : failureText(st.last_job.message, sid)),
      h("a", { class: "btn small primary", href: `#s/${sid}/${st.last_job.ok ? "dashboard" : "setup"}`, onclick: undock }, st.last_job.ok ? "Open" : "See why"),
      h("button", { class: "btn small ghost", "aria-label": "Close", onclick: undock }, "✕"));
  };
  dock.timer = setInterval(poll, 2000);
  poll();
}
function undock() {
  if (!dock) return;
  clearInterval(dock.timer);
  dock.el.remove();
  dock = null;
}

views.setup = () => {
  const main = h("div", { class: "setup" });
  let opts = null;
  const st = setupState;
  const isNew = !server;  // #new: a brand-new server; #s/<id>/setup: finish one that exists
  let propDefaults = {};

  const field = (label, input, hint) => h("label", {}, label, input, hint ? h("span", { class: "muted small" }, hint) : null);

  const renderForm = (error) => {
    const loaderCards = h("div", { class: "choices" }, opts.loaders.map((l) => h("button", {
      type: "button", class: "choice" + (st.loader === l.name ? " selected" : ""),
      disabled: !!st.modpack && st.loader !== l.name,
      onclick: () => { st.loader = l.name; if (!l.mods) { st.mods.clear(); st.localMods = []; } else setupRecheckMods(); renderForm(); },
    }, h("strong", {}, l.label), h("span", { class: "small muted" }, l.description))));
    const intro = [
      h("h2", { class: "view-title" }, isNew ? "Create a new server" : "Set up your server"),
      h("p", { class: "muted" }, "Choose what kind of server you want. mcsm downloads everything it needs (Minecraft, the mod loader, mods and Java) and keeps it up to date from then on. " +
        (opts.network_option ? "" : "It won't start until you press Start.")),
      error ? h("div", { class: "notice bad" }, h("strong", {}, "Setup didn't finish: "), failureText(error, server),
        h("div", { class: "small mt-s" }, "Change your choices below and try again.")) : null,
    ];
    if (!st.loader) {  // one step at a time: the rest depends on the server type
      fill(main, intro, card("1. Server type", loaderCards,
        h("p", { class: "muted small mt-s" }, "Pick a server type to continue. Fabric, NeoForge, Forge and Quilt run mods; Vanilla is plain Minecraft.")));
      return;
    }

    const betas = opts.betas || [];
    const version = h("select", { onchange: (e) => { st.minecraft = e.target.value; setupRecheckMods(); renderForm(); } },
      h("option", { value: "latest" }, st.loader === "vanilla" ? "Newest release (recommended)" : "Newest version your mods support (recommended)"),
      st.showBetas && betas.length ? h("optgroup", { label: "Beta versions (for testing)" }, betas.map((v) => h("option", { value: v }, `Minecraft ${v} (beta)`))) : null,
      h("optgroup", { label: "Releases" }, opts.versions.map((v) => h("option", { value: v }, `Minecraft ${v}`))));
    const isBeta = betas.includes(st.minecraft);
    const betaToggle = betas.length ? h("label", { class: "row mt-s" },
      h("input", { type: "checkbox", checked: !!st.showBetas || isBeta, onchange: (e) => {
        st.showBetas = e.target.checked;
        if (!st.showBetas && betas.includes(st.minecraft)) st.minecraft = "latest";
        renderForm();
      } }),
      h("span", {}, "Show beta versions (snapshots and pre-releases of the next Minecraft)")) : null;
    const betaNote = isBeta ? h("div", { class: "notice warn mt-s" }, h("strong", {}, `Minecraft ${st.minecraft} is a beta. `),
      "It's for trying what's coming: things may break, a world opened in it can't go back to a release, and most mods " +
      "(and the NeoForge and Forge loaders) don't support betas yet. The server stays on this version until you change it in Settings. " +
      "To try a beta with an existing world, use \"Test a beta version\" on that server's Updates page instead: it works on a copy.") : null;
    if (st.modpack && ![...version.options].some((o) => o.value === st.minecraft)) version.append(h("option", { value: st.minecraft }, `Minecraft ${st.minecraft}`));
    version.value = st.minecraft;
    version.disabled = !!st.modpack;

    // Mods
    const selected = h("div");
    // Each picked mod, with the mods it needs listed under it.
    const modRows = () => {
      const rows = [];
      const shown = new Set();
      const row = (key, depth) => {
        const m = st.mods.get(key);
        if (!m || (depth === 0 && shown.has(key)) || depth > 6) return;  // (mods that need each other)
        shown.add(key);
        const needers = [...m.by].filter((k) => st.mods.has(k)).map((k) => st.mods.get(k).name);
        rows.push(h("li", { class: depth ? "dep" : null },
          h("div", { class: "grow" }, depth ? "↳ " : null, h("strong", {}, m.name), depth ? null : channelTag(m.channel),
            key.startsWith("curseforge:") ? h("span", { class: "tag" }, "CurseForge") : null,
            !m.explicit || needers.length ? h("span", { class: "tag" }, `needed by ${needers.join(", ")}`) : null,
            m.bad ? h("div", { class: "small bad-text" }, m.bad) : null),
          m.explicit ? h("label", { class: "row", title: "Every mod holds back Minecraft upgrades until it supports the new version. Required ones also decide the Minecraft version a new server starts on." },
            h("input", { type: "checkbox", checked: m.required, onchange: (e) => { m.required = e.target.checked; } }), "required") : null,
          h("button", { type: "button", class: "btn small danger", onclick: () => { setupRemoveMod(key); search(); } }, "Remove")));
        for (const [k, o] of st.mods) if (o.by.has(key) && !o.explicit) row(k, depth + 1);
      };
      for (const [k, m] of st.mods) if (m.explicit) row(k, 0);
      for (const k of st.mods.keys()) row(k, 0);  // anything left over
      return rows;
    };
    st.onChange = () => renderSelected();
    const renderSelected = () => fill(selected, st.mods.size || st.localMods.length ? h("ul", { class: "list" }, st.localMods.map((m) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, m.name), h("span", { class: "tag" }, "local file")),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.localMods = st.localMods.filter((x) => x !== m); renderSelected(); } }, "Remove"))),
      modRows())
      : h("p", { class: "empty" }, st.modpack ? "No extra mods. The modpack's own mods are added when the server is created."
        : "Nothing yet. Download mods or add files from this computer above, or leave it empty for an unmodded server."));
    const loaderLabel = (opts.loaders.find((l) => l.name === st.loader) || {}).label || st.loader;
    const plugins = st.loader === "paper";  // Paper: server plugins rather than mods
    renderSelected();
    // Three ways to add mods: files on this computer, the mod browser window, or a whole modpack.
    const picker = h("input", { type: "file", multiple: true, accept: ".jar", class: "hidden" });
    picker.addEventListener("change", async () => {
      for (const f of [...picker.files]) {
        const r = await api(`/api/hub/stage?filename=${encodeURIComponent(f.name)}`, { method: "POST", raw: f })
          .catch((e) => { toast(`${f.name}: ${e.message}`, true); return null; });
        if (r) st.localMods.push({ id: r.id, name: f.name });
      }
      picker.value = "";
      renderSelected();
    });
    const sources = h("div", { class: "source-buttons" },
      h("button", { type: "button", class: "btn", onclick: () => picker.click() }, "📁 Local files",
        h("span", { class: "small muted" }, ".jar files on this computer")),
      h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "mod", target: "setup", loader: st.loader, version: setupModVersion() }) },
        plugins ? "🔎 Download plugins" : "🔎 Download mods", h("span", { class: "small muted" }, plugins ? "Browse Modrinth" : "Browse Modrinth and CurseForge")),
      plugins ? null : h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "modpack", target: "setup", loader: st.modpack ? "" : st.loader }) },
        "📦 Modpacks", h("span", { class: "small muted" }, "A ready-made pack of mods")),
      picker);
    const packCard = st.modpack ? h("div", { class: "notice mt-s pack" },
      st.modpack.icon ? h("img", { src: st.modpack.icon, alt: "", referrerpolicy: "no-referrer" }) : null,
      h("div", { class: "grow" }, h("strong", {}, st.modpack.name), " ", h("span", { class: "tag" }, st.modpack.version || ""),
        h("div", { class: "small muted" }, `Minecraft ${st.minecraft}, ${loaderLabel}. The pack decides the version and server type; its mods are installed and kept up to date.`)),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.modpack = null; st.minecraft = "latest"; renderForm(); } }, "Remove modpack")) : null;
    const modsCard = st.loader && opts.loaders.find((l) => l.name === st.loader).mods ? card(plugins ? "3. Plugins" : "3. Mods",
      sources, packCard, h("h3", { class: "mt" }, plugins ? "Your plugins" : "Your mods"), selected,
      h("div", { class: "row mt-s" }, testButton({
        check: ["/api/hub/mods/check", { loader: st.loader, minecraft: setupModVersion(),
          mods: [...st.mods].filter(([k, m]) => m.explicit && !k.startsWith("curseforge:")).map(([k]) => k), channels: earlyChannels() }],
        trial: { loader: st.loader, minecraft: st.minecraft, mods: [...st.mods].filter(([, m]) => m.explicit).map(([k]) => k), channels: earlyChannels() },
        keepWorking: (res) => { for (const o of res.outliers) setupRemoveMod(o.source === "curseforge" ? `curseforge:${o.id}` : o.id); },
      }), h("span", { class: "muted small" }, "Check that these mods work together before creating the server.")),
      st.loader === "fabric" || st.loader === "quilt" ? h("p", { class: "muted small" }, "Fabric API is added automatically, since almost every Fabric mod needs it.") : null,
      plugins ? h("p", { class: "muted small" }, "Paper runs server plugins (Paper, Spigot and Bukkit ones) from its plugins folder. Players join with plain Minecraft: plugins don't need anything on their side.") : null) : null;

    // Settings
    const inp = (key, attrs = {}) => h("input", { value: st[key], ...attrs, oninput: (e) => { st[key] = attrs.type === "number" ? Number(e.target.value) : e.target.value; } });
    const sel = (key, choices) => { const el = h("select", { onchange: (e) => { st[key] = e.target.value; } }, choices.map((c) => h("option", { value: c }, c[0].toUpperCase() + c.slice(1)))); el.value = st[key]; return el; };
    const ram = opts.total_ram_gb || 0;
    const mem = h("select", { onchange: (e) => { st.memory_gb = Number(e.target.value); renderForm(); } },
      Array.from({ length: 32 }, (_, i) => i + 1).map((g) => h("option", { value: String(g) },
        `${g} GB${g === opts.memory_gb ? " (suggested)" : ""}${ram && g > ram ? " (more than this computer has)" : ""}`)));
    mem.value = String(st.memory_gb);

    // The Minecraft port, checked as you type: other servers here, mcsm itself, other programs.
    const portField = () => {
      const note = h("span", { class: "muted small" }, "25565 is Minecraft's usual port. Friends type the address as host:port when it's not 25565.");
      const input = h("input", { type: "number", min: 1024, max: 65535, value: st.port });
      let timer;
      const check = async () => {
        const port = Number(input.value);
        if (!Number.isInteger(port) || port < 1024 || port > 65535) { note.className = "small bad-text"; note.textContent = "Pick a number between 1024 and 65535."; return; }
        if (opts.network_option) return;  // `mcsm run`: a single server, nothing to compare with
        const r = await api(`/api/hub/port?port=${port}${isNew ? "" : "&exclude=" + encodeURIComponent(server)}`).catch(() => null);
        if (!r || Number(input.value) !== port) return;
        if (r.used_by) { note.className = "small bad-text"; note.textContent = `Already used by ${r.used_by}. Try ${r.suggestion}.`; }
        else if (r.mcsm) { note.className = "small bad-text"; note.textContent = `mcsm itself uses this port. Try ${r.suggestion}.`; }
        else if (r.busy) { note.className = "small warn-text"; note.textContent = `Another program on this computer is using port ${port}; the server won't start until it's free. Try ${r.suggestion}.`; }
        else { note.className = "small ok-text"; note.textContent = `Port ${port} is free.` + (port === 25565 ? "" : " Friends connect with your address followed by :" + port + "."); }
      };
      input.addEventListener("input", () => { st.port = Number(input.value); clearTimeout(timer); timer = setTimeout(check, 300); });
      check();
      return field("Port (players connect to this)", input, note);
    };

    // World: a new one (seed, type, structures, hardcore) or one you already have.
    const P = st.properties;
    const worldTypes = [["minecraft:normal", "Normal", "The usual Minecraft world."], ["minecraft:large_biomes", "Large biomes", "Biomes 4× bigger."],
      ["minecraft:amplified", "Amplified", "Huge mountains (needs a fast computer)."], ["minecraft:flat", "Flat", "Superflat, for building."],
      ["minecraft:single_biome_surface", "Single biome", "One biome everywhere."]];
    const seed = h("input", { value: P["level-seed"] || "", maxlength: 64, placeholder: "Random",
      oninput: (e) => { P["level-seed"] = e.target.value; } });
    const flag = (key, text) => h("label", { class: "row" }, h("input", { type: "checkbox", checked: P[key] === "true",
      onchange: (e) => { P[key] = String(e.target.checked); } }), h("span", {}, text));
    const hasMods = !!(st.loader && opts.loaders.find((l) => l.name === st.loader).mods);
    const worldCard = card(hasMods ? "4. World" : "3. World",
      h("div", { class: "choices two" },
        h("button", { type: "button", class: "choice" + (st.world ? "" : " selected"), onclick: () => { st.world = null; renderForm(); } },
          h("strong", {}, "New world"), h("span", { class: "small muted" }, "Minecraft makes a fresh world the first time the server starts.")),
        h("button", { type: "button", class: "choice" + (st.world ? " selected" : ""), onclick: () => pickWorld((w) => { st.world = w; renderForm(); }) },
          h("strong", {}, "Import a world"), h("span", { class: "small muted" }, "Bring a singleplayer world or a world .zip, from any version of Minecraft Java."))),
      st.world ? h("div", { class: "notice mt-s" }, h("strong", {}, st.world.name),
        st.world.version ? h("span", { class: "muted" }, ` · last played on Minecraft ${st.world.version}`) : null,
        h("div", { class: "small muted" }, "Minecraft upgrades an older world when the server first starts; a world can't go back to an older version, " +
          "and blocks from mods the server doesn't have are lost."),
        h("div", { class: "row mt-s" }, h("button", { type: "button", class: "btn small", onclick: () => pickWorld((w) => { st.world = w; renderForm(); }) }, "Choose another"),
          h("button", { type: "button", class: "btn small ghost", onclick: () => { st.world = null; renderForm(); } }, "Use a new world instead")))
      : h("div", { class: "mt-s" },
        h("div", { class: "grid" }, field("Seed", seed, "A number or any text. The same seed makes the same world.")),
        h("h3", { class: "mt-s" }, "World type"),
        h("div", { class: "choices world-types" }, worldTypes.map(([v, label, desc]) => h("button", { type: "button",
          class: "choice" + ((P["level-type"] || "minecraft:normal") === v ? " selected" : ""),
          onclick: () => { P["level-type"] = v; renderForm(); } }, h("strong", {}, label), h("span", { class: "small muted" }, desc)))),
        h("div", { class: "grid mt-s" }, flag("generate-structures", "Villages, temples and other structures"),
          flag("hardcore", "Hardcore: one life, locked to hard"))));

    const eula = h("input", { type: "checkbox", checked: st.accept_eula, onchange: (e) => { st.accept_eula = e.target.checked; } });
    const lan = h("input", { type: "checkbox", checked: st.network_access, onchange: (e) => { st.network_access = e.target.checked; } });

    const submit = async (e) => {
      e.preventDefault();
      if (!st.accept_eula) { toast("Please read and accept the Minecraft EULA first.", true); return; }
      // Only the mods you picked: their dependencies are installed with them (and go with them).
      const bad = [...st.mods.values()].filter((m) => m.bad);
      if (bad.length && !confirm(`${bad.map((m) => `${m.name}: ${m.bad}`).join("\n")}\n\nCreate the server anyway?`)) return;
      const mods = [...st.mods].filter(([, m]) => m.explicit && m.required).map(([slug]) => slug);
      const optional = [...st.mods].filter(([, m]) => m.explicit && !m.required).map(([slug]) => slug);
      const body = { loader: st.loader, minecraft: st.minecraft, mods, optional_mods: optional, memory_gb: st.memory_gb,
        motd: st.motd, max_players: st.max_players, difficulty: st.difficulty, gamemode: st.gamemode, port: st.port,
        network_access: st.network_access, accept_eula: true, properties: changedProps(st.properties, propDefaults),
        friends: !!st.friends, local_mods: st.localMods.map((m) => m.id), world: st.world ? st.world.world : "",
        mod_channels: Object.fromEntries([...st.mods].filter(([, m]) => m.explicit && m.channel).map(([k, m]) => [k, m.channel])) };
      if (st.modpack) body.modpack_version = st.modpack.version_id;
      if (isNew) {
        const r = await act(() => api("/api/hub/create", { method: "POST", body }));
        if (r) {
          setupState.offerFriends = { sid: r.id, name: body.motd, friends: !!body.friends };  // slide the progress down
          Object.assign(setupState, { friends: false, loader: null, mods: new Map(), motd: "A Minecraft server", accept_eula: false,
            prefilled: false, properties: null, advancedOpen: false, modpack: null, localMods: [], minecraft: "latest", world: null });
          location.hash = `#s/${r.id}/setup`;
        }
        return;
      }
      const r = await act(() => api("/api/setup", { method: "POST", body }));
      if (r) { st.submitted = true; renderProgress(); }
    };

    const advanced = h("details", { class: "card advanced", open: st.advancedOpen },
      h("summary", {}, "Advanced settings (optional)"),
      h("p", { class: "muted small" }, "The rest of Minecraft's server settings: PvP, spawn protection, view distance and more. The defaults suit most servers, and you can change these later in the server's Settings."),
      // The World card above covers the seed, type, structures and hardcore.
      propsEditor(opts.properties_schema.filter((p) => !WORLD_CARD_PROPS.includes(p.key)), st.properties));
    advanced.addEventListener("toggle", () => { st.advancedOpen = advanced.open; });

    fill(main, intro,
      h("form", { onsubmit: submit },
        card("1. Server type", loaderCards),
        h("div", { class: "mt" }, card("2. Minecraft version", field("Version", version,
          "\"Newest\" picks the newest Minecraft your mods work on, and upgrades only once every mod supports the next version: a forever server. " +
          "Picking a specific version keeps the server on that version (mods still update); you can change this later in Settings."),
          betaToggle, betaNote)),
        modsCard ? h("div", { class: "mt" }, modsCard) : null,
        h("div", { class: "mt" }, worldCard),
        h("div", { class: "mt" }, card(modsCard ? "5. Settings" : "4. Settings",
          h("div", { class: "grid" },
            field("Server name (shown in the server list)", inp("motd", { maxlength: 59 })),
            field("Max players", inp("max_players", { type: "number", min: 1, max: 1000 })),
            field("Difficulty", sel("difficulty", opts.difficulties)),
            field("Game mode", sel("gamemode", opts.gamemodes)),
            field("Memory", mem, ram ? (st.memory_gb > ram - 2 ? `This computer has ${ram} GB. Leave some for Windows and other programs, or the server may crash.` : `This computer has ${ram} GB.`) : null),
            portField()),
          opts.network_option ? h("label", { class: "row mt" }, lan, h("span", {}, "Let other devices on my network (like my phone) open this control panel")) : null)),
        h("div", { class: "mt" }, advanced),
        opts.network_option ? null : h("div", { class: "mt" }, card("Friends (optional)",
          h("label", { class: "row check-row" },
            h("input", { type: "checkbox", checked: st.friends, onchange: (e) => { st.friends = e.target.checked; } }),
            h("span", {}, "Make a download for my friends: it sets up their Minecraft with this server's version and mods, and adds the server to their list")),
          h("p", { class: "muted small" }, "You get a link to share on the server's Friends page. You can switch this on or off later."))),
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
    const panel = h("div", { class: "progress-panel" },
      h("h2", { class: "view-title" }, "Creating your server…"),
      h("div", { class: "notice" }, h("div", { class: "row" }, h("span", { class: "spinner" }),
        h("span", { class: "grow" }, "Downloading Java, the mod loader, Minecraft and your mods, then checking that the server starts. This usually takes a few minutes."))),
      card("What's happening", events));
    fill(main, panel);
    if (dock && dock.sid === server) undock();  // the full page is back
    const offer = st.offerFriends && st.offerFriends.sid === server ? st.offerFriends : null;
    if (offer) {
      // A new server: its progress slides down to the bottom of the window and keeps going
      // there, so you can look around meanwhile (and, with friends ticked, set up their side).
      st.offerFriends = null;
      setTimeout(() => {
        if (currentName !== "setup" || server !== offer.sid) return;
        panel.classList.add("slide-away");
        setTimeout(() => {
          dockSetup(offer.sid, offer.name);
          fill(main, h("div", { class: "empty mt-l" }, "Your server is installing: its progress is at the bottom of the window.",
            h("div", { class: "row mt-s center" },
              h("button", { class: "btn small", onclick: () => { closeToast("friends-offer"); undock(); renderProgress(); } }, "Show the progress here"),
              hubInfo && hubInfo.single ? null : h("a", { class: "btn small ghost", href: "#servers" }, "Your servers"))));
          if (offer.friends) stickyToast("friends-offer", [
            h("strong", {}, "Set up your friends' download now?"),
            h("span", { class: "small" }, "While the server installs, choose the mods your friends get (a minimap, JEI, …)."),
            h("div", { class: "row mt-s" },
              h("button", { class: "btn small primary", onclick: () => { closeToast("friends-offer"); location.hash = `#s/${offer.sid}/friends`; } }, "Yes"),
              h("button", { class: "btn small ghost", onclick: () => closeToast("friends-offer") }, "Not now"))]);
        }, 650);
      }, 1800);
    }
    let startedAt = null;  // only this setup's events, not an earlier attempt's
    every(1500, async () => {
      if (!events.isConnected && seq) return;  // slid away (or shown again in a newer panel)
      if (startedAt === null) {
        const s = await api("/api/status").catch(() => null);
        if (!s) return;
        startedAt = s.job && s.job.started ? s.job.started - 1 : Date.now() / 1000 - 5;
      }
      const r = await api(`/api/events?since=${seq}`).catch(() => null);
      if (!r) return;
      seq = r.last;
      for (const e of r.events.filter((x) => x.time >= startedAt)) {
        events.prepend(h("div", { class: "ev " + e.level }, h("time", {}, fmtClock(e.time)), h("span", {}, e.message)));
      }
    });
  };

  (async () => {
    opts = await api(isNew ? "/api/hub/setup" : "/api/setup").catch((e) => { toast(e.message, true); return null; });
    if (!opts) return;
    if (!st.prefilled && opts.current) {  // an existing mcsm.toml: start from its choices
      st.prefilled = true;
      const c = opts.current;
      if (opts.loaders.some((l) => l.name === c.loader)) st.loader = c.loader;
      st.minecraft = c.minecraft;
      for (const m of c.mods) st.mods.set(m.slug, { name: m.slug, required: m.required, explicit: true, by: new Set(), bad: "" });
      if (c.memory_gb) st.memory_gb = c.memory_gb;
    }
    st.newest = opts.versions[0] || "";
    if (st.mods.size) setupRecheckMods();  // names, dependencies and compatibility
    propDefaults = Object.fromEntries(opts.properties_schema.map((p) => [p.key, p.default]));
    if (!st.properties) st.properties = { ...propDefaults };
    if (st.memory_gb === null) st.memory_gb = opts.memory_gb;
    if (isNew && opts.port) st.port = opts.port;  // a port no other server here uses
    if (st.network_access === null) st.network_access = opts.network_access;
    if (opts.versions_error) toast(opts.versions_error, true);
    const s = status || (server ? await api("/api/status").catch(() => ({})) : {});
    if (s.job && s.job.name === "set up server") { st.submitted = true; renderProgress(); } else renderForm();
  })();

  $("#main").replaceChildren(main);
  return {
    refresh: () => { if (opts && !st.submitted) renderForm(); },
    onJobDone: () => {
      const last = status && status.last_job;
      if (!last || last.name !== "set up server") return;
      if (last.ok) location.hash = link("dashboard");  // the job's own toast says it's ready
      else { st.submitted = false; clearTimers(); every(2000, refreshStatus); renderForm(last.message); }
    },
  };
};

// ------------------------------------------------------------------- router
const SERVER_VIEWS = [["dashboard", "Dashboard"], ["console", "Console"], ["players", "Players"], ["updates", "Updates"],
  ["mods", "Mods"], ["friends", "Friends"], ["backups", "Backups"], ["java", "Java"], ["settings", "Settings"]];
let currentName = null;

function renderNav() {
  const hb = hubInfo || {};
  const a = (href, label, active, extra) => h("a", { href, class: active ? "active" : null }, label, extra || null);
  const me = server && hb.servers ? hb.servers.find((x) => x.id === server) : null;
  const pending = me ? me.setup_pending : false;
  fill($("#nav"),
    server ? [
      hb.single ? null : a("#servers", "← All servers", false),
      h("div", { class: "nav-server" }, me ? me.name : server),
      pending ? a(link("setup"), "Setup", currentName === "setup")
        : SERVER_VIEWS.map(([v, label]) => a(link(v), label, currentName === v,
            v === "updates" ? h("span", { id: "nav-update-dot", class: "dot" + (me && me.update ? "" : " hidden") }) : null)),
    ] : [
      a("#servers", "Servers", currentName === "servers"),
      hb.single ? null : a("#new", "New server", currentName === "new"),
    ],
    h("div", { class: "nav-sep" }),
    a("#mcsm", "mcsm settings", currentName === "mcsm"));
  const inServer = !!server;
  $(".server-id").classList.toggle("hidden", !inServer);
  $(".actions").classList.toggle("hidden", !inServer);
  $("#page-title").classList.toggle("hidden", inServer);
  $("#page-title").textContent = { servers: "Your servers", new: "New server", mcsm: "mcsm settings" }[currentName] || "";
  if (!inServer) $("#job").classList.add("hidden");
}

function route() {
  closeBrowser(true);
  const hash = (location.hash || "#servers").slice(1);
  document.body.classList.remove("browse-mode");
  if (hash.startsWith("browse")) {  // the mod browser window: no navigation around it
    clearTimers();
    currentName = "browse";
    current = views.browse(new URLSearchParams(hash.split("?")[1] || ""));
    return;
  }
  const m = hash.match(/^s\/([a-z0-9][a-z0-9-]*)(?:\/(\w+))?$/);
  const before = server;
  let view;
  if (m) {
    server = m[1];
    view = m[2] === "setup" || SERVER_VIEWS.some(([v]) => v === m[2]) ? m[2] : "dashboard";
  } else {
    server = null;
    view = ["servers", "new", "mcsm"].includes(hash) ? hash : "servers";
  }
  if (server !== before) { status = null; lastJobSeen = null; }
  currentName = view;
  clearTimers();
  document.body.classList.remove("setup-mode");
  renderNav();
  every(2000, refreshStatus);
  current = views[view === "new" ? "setup" : view]();
}
window.addEventListener("hashchange", () => { if (!$("#app").classList.contains("hidden")) route(); });

async function start() {
  try { hubInfo = await api("/api/hub"); } catch (_) { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  route();
}
start();
