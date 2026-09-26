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
  $("#version").textContent = "v" + hb.version;
  $("#logout").classList.toggle("hidden", hb.auth.mode === "none");
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
  let info = {};
  const load = async () => {
    const r = await api("/api/mods").catch(() => null);
    if (!r) return;
    info = r;
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
      "🔎 Download mods", h("span", { class: "small muted" }, "Browse Modrinth and CurseForge")),
    hubInfo && hubInfo.single ? null : h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "modpack", target: "setup" }) },
      "📦 Modpacks", h("span", { class: "small muted" }, "Start a new server from a pack")),
    picker);

  fill($("#main"), 
    h("h2", { class: "view-title" }, "Mods"),
    card("Add mods", sources, h("h3", { class: "mt" }, "Quick add"), q, results,
      h("div", { class: "row mt-s" }, cfId,
        h("button", { class: "btn", onclick: () => cfId.value.trim() && add(cfId.value.trim(), true, "curseforge") }, "Add from CurseForge"))),
    h("div", { class: "grid mt" }, card("Configured (mcsm.toml)", configured),
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
        chk("auto_upgrade", "Apply updates automatically"),
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
  fill($("#main"), h("h2", { class: "view-title" }, "Server settings"), form, exportCard, danger);
  load();
  loadExports();
  renderDanger();
  return { onStatus: renderDanger, onJobDone: loadExports };
};

// ------------------------------------------------------------------ friends
// A download friends run to set up their Minecraft for this server (mods and all).
views.friends = () => {
  const body = h("div");
  const results = h("div");
  const q = h("input", { type: "search", placeholder: "Search Modrinth for mods players can add, e.g. minimap, JEI, Sodium" });
  let data = null;
  let timer;
  const save = async (changes, message) => {
    const r = await act(() => api("/api/client", { method: "POST", body: changes }), message);
    if (r) { data = r; render(); }
  };
  const search = async () => {
    if (!data || !data.enabled) return;
    const term = q.value.trim();
    const r = await api(`/api/client/search?${term ? "q=" + encodeURIComponent(term) : "top=1"}`).catch((e) => { toast(e.message, true); return null; });
    if (!r || q.value.trim() !== term) return;
    fill(results, term ? null : h("h3", { class: "mt-s" }, "Popular mods for players"),
      r.results.length ? r.results.slice(0, term ? 10 : 20).map((m) => h("div", { class: "mod" },
        m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "info" }, h("div", { class: "name" }, m.name), h("div", { class: "desc" }, m.description)),
        (data.pack && data.pack.mods.some((x) => x.project === "modrinth:" + m.id)) && !data.mods.includes(m.slug)
          ? h("span", { class: "tag" }, "included")
        : data.mods.includes(m.slug) || data.mods.includes(m.id) ? h("span", { class: "tag ok" }, "added")
          : h("button", { class: "btn small primary", onclick: () => save({ mods: [...data.mods, m.slug] }, `${m.name} added for players`).then(search) }, "Add"),
      )) : [h("p", { class: "empty" }, "No player mods found.")]);
  };
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });

  const render = () => {
    const d = data;
    if (!d.available) {
      fill(body, card(null, h("p", {}, "Friend downloads are part of mcsm's server list. Start mcsm by double-clicking it (or `mcsm start`) to use them.")));
      return;
    }
    const toggle = h("input", { type: "checkbox", checked: d.enabled, onchange: (e) => save({ enabled: e.target.checked },
      e.target.checked ? "Friend download switched on" : "Friend download switched off").then(search) });
    const intro = card("Let friends set up their Minecraft",
      h("p", {}, "Share a link. Your friends download a small file that adds a ", h("strong", {}, (status && status.motd) || "server"),
        " instance to their launcher (Minecraft Launcher, Prism Launcher, Modrinth App or CurseForge: they choose) with the right Minecraft version, mod loader and mods, and puts this server in their multiplayer list. They sign in with their own Minecraft account as usual."),
      h("label", { class: "row mt-s" }, toggle, h("span", {}, "Make a download for friends")));
    if (!d.enabled) { fill(body, intro); return; }
    const s = d.share || {};
    const link = h("input", { readonly: true, value: d.link || "", class: "grow mono", "aria-label": "Invite link" });
    const copy = h("button", { class: "btn primary", onclick: async () => {
      try { await navigator.clipboard.writeText(link.value); toast("Invite link copied"); }
      catch (_) { link.select(); document.execCommand("copy"); toast("Invite link copied"); }
    } }, "Copy");
    const pack = d.pack;
    const sideTag = (m) => h("span", { class: "tag" }, m.side === "client" ? "players only" : "server + players");
    fill(body,
      intro,
      h("div", { class: "mt" }, card("Invite link",
        h("div", { class: "row" }, link, copy,
          h("button", { class: "btn ghost", onclick: () => {
            if (confirm("Make a new link? The old one stops working (friends who already set up keep playing, but can't update until they get the new link).")) {
              act(() => api("/api/client/new-link", { method: "POST", body: {} }), "New link made").then((r) => { if (r) { data = r; render(); } });
            }
          } }, "New link")),
        s.error ? h("div", { class: "notice bad mt-s" }, s.error)
          : h("p", { class: "muted small" }, s.running ? `Sharing on port ${s.port}.` : "Sharing starts in a few seconds."),
        h("p", { class: "muted small" },
          s.address ? `Friends connect to ${s.address}. ` : `The link uses this computer's address on your network (${s.lan_ip || "unknown"}), which works for friends on the same Wi-Fi. `,
          "For friends elsewhere, forward TCP ports ", h("strong", {}, String(s.port)), " (the download) and your Minecraft port on your router, and set your public address under ",
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
            [2, 3, 4, 6, 8, 10, 12].map((g) => h("option", { value: String(g) }, `${g} GB`))); sel.value = String(d.memory_gb); return sel; })()))),
      d.loader === "vanilla" ? null : h("div", { class: "mt" }, card("Add mods just for players",
        h("p", { class: "muted small" }, "Client-side mods like minimaps, recipe viewers or performance mods. The server's own mods that players need are included automatically."),
        q, results)),
    );
    if (!results.childElementCount) search();
  };
  fill($("#main"), h("h2", { class: "view-title" }, "Friends"), body);
  api("/api/client").then((r) => { data = r; render(); }).catch((e) => { if (!(e instanceof Unauthorized)) toast(e.message, true); });
  return {};
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

// ---------------------------------------------------------- mod browser window
// Opened from setup and the Mods page: search, filters and sort at the top left, results
// with checkboxes below, "Add selected" at the bottom, and the mod's page on the right.
function openBrowser(params) {
  const url = `${location.pathname}#browse?${new URLSearchParams(params)}`;
  const win = window.open(url, "mcsm-browse", "width=1400,height=900");
  if (!win) location.hash = `#browse?${new URLSearchParams(params)}`;  // popups blocked: open it here
  else win.focus();
}
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || typeof e.data !== "object") return;
  const d = e.data;
  if (d.type === "mcsm-add-mods" && Array.isArray(d.mods)) {
    for (const m of d.mods) setupState.mods.set(m.source === "curseforge" ? `curseforge:${m.id}` : m.slug || m.id, { name: m.name, required: true });
    toast(`${d.mods.length} mod(s) added`);
    if (current && current.refresh) current.refresh();
  } else if (d.type === "mcsm-modpack" && d.pack) {
    Object.assign(setupState, { modpack: d.pack, loader: d.pack.loader, minecraft: d.pack.minecraft });
    toast(`Modpack chosen: ${d.pack.name}`);
    if ((currentName === "new" || currentName === "setup") && current && current.refresh) current.refresh();
    else location.hash = "#new";
  } else if (d.type === "mcsm-mods-changed" && current && current.refresh) current.refresh();
});

views.browse = (params) => {
  document.body.classList.add("browse-mode");
  const kind = params.get("type") === "modpack" ? "modpack" : "mod";
  const target = params.get("target") || "setup";
  const loader = params.get("loader") || "";
  const base = target === "setup" ? "/api/hub/browse" : `/api/servers/${encodeURIComponent(target)}/browse`;
  const st = { q: "", source: "modrinth", sort: "relevance", category: "", version: params.get("version") || "",
    offset: 0, total: 0, results: [], selected: new Map(), active: null };
  const list = h("div", { class: "browse-results" });
  const details = h("div", { class: "browse-right" }, h("p", { class: "empty" }, `Pick a ${kind} on the left to read about it here.`));
  const count = h("span", { class: "grow muted small" });
  const addBtn = h("button", { class: "btn primary" + (kind === "modpack" ? " hidden" : ""), disabled: true }, "Add selected mods");
  const q = h("input", { type: "search", placeholder: kind === "modpack" ? "Search modpacks…" : "Search mods…", "aria-label": "Search" });
  const sort = h("select", { "aria-label": "Sort by" }, [["relevance", "Best match"], ["downloads", "Most downloaded"],
    ["follows", "Most followed"], ["newest", "Newest"], ["updated", "Recently updated"]].map(([v, l]) => h("option", { value: v }, l)));
  const source = h("select", { "aria-label": "Source" }, h("option", { value: "modrinth" }, "Modrinth"));
  const category = h("select", { "aria-label": "Category" }, h("option", { value: "" }, "All categories"));
  const version = h("input", { value: st.version, placeholder: "Any version", "aria-label": "Minecraft version", class: "narrow" });
  let timer, seq = 0;

  const fmtNum = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
  const updateFooter = () => {
    const n = st.selected.size;
    count.textContent = kind === "modpack" ? (st.active ? "" : "Pick a modpack to see its versions.")
      : n ? `${n} selected: ${[...st.selected.values()].map((m) => m.name).slice(0, 3).join(", ")}${n > 3 ? "…" : ""}` : "Tick the mods you want.";
    addBtn.disabled = kind === "modpack" ? true : n === 0;
  };
  const search = async (more = false) => {
    const mine = ++seq;
    if (!more) { st.offset = 0; list.scrollTop = 0; }
    const p = new URLSearchParams({ type: kind, q: st.q, source: st.source, sort: st.sort, offset: String(st.offset) });
    if (st.category) p.set("category", st.category);
    p.set("version", st.version);
    if (loader) p.set("loader", loader);
    if (!more) fill(list, h("p", { class: "empty" }, "Searching…"));
    const r = await api(`${base}/search?${p}`).catch((e) => { if (!(e instanceof Unauthorized)) fill(list, h("div", { class: "notice bad" }, e.message)); return null; });
    if (!r || mine !== seq) return;
    st.results = more ? st.results.concat(r.results) : r.results;
    st.total = r.total;
    renderList();
  };
  const renderList = () => {
    fill(list, st.results.length ? st.results.map((m) => {
      const key = `${m.source}:${m.id}`;
      const box = kind === "mod" ? h("input", { type: "checkbox", checked: st.selected.has(key), "aria-label": `Select ${m.name}`,
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => { if (e.target.checked) st.selected.set(key, m); else st.selected.delete(key); updateFooter(); } }) : null;
      return h("div", { class: "result" + (st.active === key ? " active" : ""), tabindex: "0", role: "button",
        onclick: () => showDetails(m), onkeydown: (e) => { if (e.key === "Enter") showDetails(m); } },
        box || h("span"),
        m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "info" },
          h("div", { class: "name" }, m.name, m.author ? h("span", { class: "muted small" }, ` by ${m.author}`) : null),
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
      if (window.opener) { window.opener.postMessage({ type: "mcsm-modpack", pack }, location.origin); window.close(); }
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
    const mods = [...st.selected.values()].map((m) => ({ source: m.source, id: m.id, slug: m.slug, name: m.name }));
    if (target === "setup") {
      if (window.opener) { window.opener.postMessage({ type: "mcsm-add-mods", mods }, location.origin); window.close(); return; }
      for (const m of mods) setupState.mods.set(m.source === "curseforge" ? `curseforge:${m.id}` : m.slug || m.id, { name: m.name, required: true });
      location.hash = "#new";
      return;
    }
    const r = await act(() => api(`/api/servers/${encodeURIComponent(target)}/mods/add-many`, { method: "POST", body: { mods } }));
    if (!r) return;
    toast(`Added ${r.added.length} mod(s)` + (r.skipped.length ? `; skipped ${r.skipped.map((x) => `${x.name} (${x.reason})`).join(", ")}` : ""), r.skipped.length > 0);
    st.selected.clear(); renderList();
    if (window.opener) window.opener.postMessage({ type: "mcsm-mods-changed" }, location.origin);
  });
  q.addEventListener("input", () => { st.q = q.value.trim(); clearTimeout(timer); timer = setTimeout(() => search(), 350); });
  sort.addEventListener("change", () => { st.sort = sort.value; search(); });
  source.addEventListener("change", () => { st.source = source.value; st.category = ""; loadCategories(); search(); });
  category.addEventListener("change", () => { st.category = category.value; search(); });
  version.addEventListener("change", () => { st.version = version.value.trim(); search(); });
  const loadCategories = async () => {
    const r = await api(`${base}/categories?type=${kind}&source=${st.source}`).catch(() => null);
    if (!r) return;
    fill(category, h("option", { value: "" }, "All categories"), r.categories.map((c) => h("option", { value: c.id }, c.name)));
    if (source.options.length === 1 && r.sources.includes("curseforge") && kind === "mod") source.append(h("option", { value: "curseforge" }, "CurseForge"));
  };

  fill($("#main"), h("div", { class: "browse" },
    h("div", { class: "browse-left" },
      h("div", { class: "browse-filters" },
        h("div", { class: "row" }, h("strong", { class: "grow" }, kind === "modpack" ? "Modpacks" : "Mods"),
          loader ? h("span", { class: "tag" }, loader) : null,
          window.opener ? h("button", { class: "btn ghost small", onclick: () => window.close() }, "Close") : h("a", { class: "btn ghost small", href: target === "setup" ? "#new" : `#s/${target}/mods` }, "Back")),
        q,
        h("div", { class: "row" }, source, sort),
        h("div", { class: "row" }, category, version)),
      list,
      h("div", { class: "browse-footer" }, count, addBtn)),
    details));
  q.focus();
  loadCategories();
  search();
  return {};
};

// ------------------------------------------------------------ advanced settings
// Every other server.properties setting, grouped; edits `values` (key -> string) in place.
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
  fill($("#main"), security, network, sharing, about);
  renderSecurity(hubInfo);
  renderSharing(hubInfo);
  loadAbout();
  return { onHub: (hb) => { renderSecurity(hb); renderSharing(hb); } };
};

// ------------------------------------------------------------------- setup
// Kept outside the view so choices survive re-renders and a failed attempt.
const setupState = { friends: false, loader: null, minecraft: "latest", mods: new Map(), motd: "A Minecraft server", properties: null, advancedOpen: false,
  max_players: 20, difficulty: "normal", gamemode: "survival", port: 25565, memory_gb: null,
  network_access: null, accept_eula: false, submitted: false, prefilled: false, modpack: null, localMods: [] };

views.setup = () => {
  const main = h("div", { class: "setup" });
  let opts = null;
  const st = setupState;
  const isNew = !server;  // #new: a brand-new server; #s/<id>/setup: finish one that exists
  const topMods = new Map();  // loader -> the popular list, fetched once
  let propDefaults = {};

  const field = (label, input, hint) => h("label", {}, label, input, hint ? h("span", { class: "muted small" }, hint) : null);

  const renderForm = (error) => {
    const loaderCards = h("div", { class: "choices" }, opts.loaders.map((l) => h("button", {
      type: "button", class: "choice" + (st.loader === l.name ? " selected" : ""),
      disabled: !!st.modpack && st.loader !== l.name,
      onclick: () => { st.loader = l.name; if (!l.mods) { st.mods.clear(); st.localMods = []; } renderForm(); },
    }, h("strong", {}, l.label), h("span", { class: "small muted" }, l.description))));
    const intro = [
      h("h2", { class: "view-title" }, isNew ? "Create a new server" : "Set up your server"),
      h("p", { class: "muted" }, "Choose what kind of server you want. mcsm downloads everything it needs (Minecraft, the mod loader, mods and Java) and keeps it up to date from then on. " +
        (opts.network_option ? "" : "It won't start until you press Start.")),
      error ? h("div", { class: "notice bad" }, h("strong", {}, "Setup didn't finish: "), error, h("div", { class: "small mt-s" }, "Change your choices below and try again.")) : null,
    ];
    if (!st.loader) {  // one step at a time: the rest depends on the server type
      fill(main, intro, card("1. Server type", loaderCards,
        h("p", { class: "muted small mt-s" }, "Pick a server type to continue. Fabric, NeoForge, Forge and Quilt run mods; Vanilla is plain Minecraft.")));
      return;
    }

    const version = h("select", { onchange: (e) => { st.minecraft = e.target.value; } },
      h("option", { value: "latest" }, st.loader === "vanilla" ? "Newest release (recommended)" : "Newest version your mods support (recommended)"),
      opts.versions.map((v) => h("option", { value: v }, `Minecraft ${v}`)));
    if (st.modpack && ![...version.options].some((o) => o.value === st.minecraft)) version.append(h("option", { value: st.minecraft }, `Minecraft ${st.minecraft}`));
    version.value = st.minecraft;
    version.disabled = !!st.modpack;

    // Mods
    const results = h("div");
    const selected = h("div");
    const renderSelected = () => fill(selected, st.mods.size || st.localMods.length ? h("ul", { class: "list" }, st.localMods.map((m) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, m.name), h("span", { class: "tag" }, "local file")),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.localMods = st.localMods.filter((x) => x !== m); renderSelected(); } }, "Remove"))),
      [...st.mods].map(([slug, m]) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, m.name), h("span", { class: "tag" }, slug.startsWith("curseforge:") ? "CurseForge" : slug)),
      h("label", { class: "row", title: "Required mods hold back Minecraft upgrades until they support the new version" },
        h("input", { type: "checkbox", checked: m.required, onchange: (e) => { m.required = e.target.checked; } }), "required"),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.mods.delete(slug); renderSelected(); search(); } }, "Remove"))))
      : h("p", { class: "empty" }, st.modpack ? "No extra mods. The modpack's own mods are added when the server is created."
        : "No mods yet. Search above, or leave empty for an unmodded server."));
    const q = h("input", { type: "search", placeholder: "Search Modrinth, e.g. lithium, create, farmer's delight" });
    let timer;
    const loaderLabel = (opts.loaders.find((l) => l.name === st.loader) || {}).label || st.loader;
    const search = async () => {
      const term = q.value.trim();
      const url = `${isNew ? "/api/hub" : "/api"}/mods/search?loader=${encodeURIComponent(st.loader)}&` +
        (term ? `q=${encodeURIComponent(term)}` : "top=1");
      const key = st.loader + "|" + term;
      const r = term || !topMods.has(key)
        ? await api(url).catch((e) => { toast(e.message, true); return null; }) : topMods.get(key);
      if (!r || q.value.trim() !== term) return;  // a newer search is on its way
      if (!term) topMods.set(key, r);
      fill(results, term ? null : h("h3", { class: "mt-s" }, `Most popular ${loaderLabel} mods`),
        r.results.length ? r.results.slice(0, term ? 10 : 20).map((m) => h("div", { class: "mod" },
        m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
        h("div", { class: "info" }, h("div", { class: "name" }, m.name), h("div", { class: "desc" }, m.description)),
        st.mods.has(m.slug) ? h("span", { class: "tag ok" }, "added")
          : h("button", { type: "button", class: "btn small primary", onclick: () => { st.mods.set(m.slug, { name: m.name, required: true }); renderSelected(); search(); } }, "Add"),
      )) : [h("p", { class: "empty" }, `No ${loaderLabel} server mods found.`)]);
    };
    q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });
    renderSelected();
    if (st.loader && opts.loaders.find((l) => l.name === st.loader).mods) search();  // the popular list
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
      h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "mod", target: "setup", loader: st.loader, version: st.minecraft === "latest" ? "" : st.minecraft }) },
        "🔎 Download mods", h("span", { class: "small muted" }, "Browse Modrinth and CurseForge")),
      h("button", { type: "button", class: "btn", onclick: () => openBrowser({ type: "modpack", target: "setup", loader: st.modpack ? "" : st.loader }) },
        "📦 Modpacks", h("span", { class: "small muted" }, "A ready-made pack of mods")),
      picker);
    const packCard = st.modpack ? h("div", { class: "notice mt-s pack" },
      st.modpack.icon ? h("img", { src: st.modpack.icon, alt: "", referrerpolicy: "no-referrer" }) : null,
      h("div", { class: "grow" }, h("strong", {}, st.modpack.name), " ", h("span", { class: "tag" }, st.modpack.version || ""),
        h("div", { class: "small muted" }, `Minecraft ${st.minecraft}, ${loaderLabel}. The pack decides the version and server type; its mods are installed and kept up to date.`)),
      h("button", { type: "button", class: "btn small danger", onclick: () => { st.modpack = null; st.minecraft = "latest"; renderForm(); } }, "Remove modpack")) : null;
    const modsCard = st.loader && opts.loaders.find((l) => l.name === st.loader).mods ? card("3. Mods",
      sources, packCard, h("h3", { class: "mt" }, "Quick add"), q, results, h("h3", { class: "mt" }, "Your mods"), selected,
      st.loader === "fabric" || st.loader === "quilt" ? h("p", { class: "muted small" }, "Fabric API is added automatically, since almost every Fabric mod needs it.") : null) : null;

    // Settings
    const inp = (key, attrs = {}) => h("input", { value: st[key], ...attrs, oninput: (e) => { st[key] = attrs.type === "number" ? Number(e.target.value) : e.target.value; } });
    const sel = (key, choices) => { const el = h("select", { onchange: (e) => { st[key] = e.target.value; } }, choices.map((c) => h("option", { value: c }, c[0].toUpperCase() + c.slice(1)))); el.value = st[key]; return el; };
    const maxMem = Math.max(2, Math.floor(opts.total_ram_gb || 16));
    const mem = h("select", { onchange: (e) => { st.memory_gb = Number(e.target.value); } },
      Array.from({ length: Math.min(maxMem, 32) }, (_, i) => i + 1).map((g) => h("option", { value: String(g) }, `${g} GB${g === opts.memory_gb ? " (suggested)" : ""}`)));
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

    const eula = h("input", { type: "checkbox", checked: st.accept_eula, onchange: (e) => { st.accept_eula = e.target.checked; } });
    const lan = h("input", { type: "checkbox", checked: st.network_access, onchange: (e) => { st.network_access = e.target.checked; } });

    const submit = async (e) => {
      e.preventDefault();
      if (!st.accept_eula) { toast("Please read and accept the Minecraft EULA first.", true); return; }
      const mods = [...st.mods].filter(([, m]) => m.required).map(([slug]) => slug);
      const optional = [...st.mods].filter(([, m]) => !m.required).map(([slug]) => slug);
      const body = { loader: st.loader, minecraft: st.minecraft, mods, optional_mods: optional, memory_gb: st.memory_gb,
        motd: st.motd, max_players: st.max_players, difficulty: st.difficulty, gamemode: st.gamemode, port: st.port,
        network_access: st.network_access, accept_eula: true, properties: changedProps(st.properties, propDefaults),
        friends: !!st.friends, local_mods: st.localMods.map((m) => m.id) };
      if (st.modpack) body.modpack_version = st.modpack.version_id;
      if (isNew) {
        const r = await act(() => api("/api/hub/create", { method: "POST", body }));
        if (r) {
          Object.assign(setupState, { friends: false, loader: null, mods: new Map(), motd: "A Minecraft server", accept_eula: false,
            prefilled: false, properties: null, advancedOpen: false, modpack: null, localMods: [], minecraft: "latest" });
          location.hash = `#s/${r.id}/setup`;
        }
        return;
      }
      const r = await act(() => api("/api/setup", { method: "POST", body }));
      if (r) { st.submitted = true; renderProgress(); }
    };

    const advanced = h("details", { class: "card advanced", open: st.advancedOpen },
      h("summary", {}, "Advanced settings (optional)"),
      h("p", { class: "muted small" }, "The rest of Minecraft's server settings: world seed and type, PvP, spawn protection, view distance and more. The defaults suit most servers, and you can change these later in the server's Settings."),
      propsEditor(opts.properties_schema, st.properties));
    advanced.addEventListener("toggle", () => { st.advancedOpen = advanced.open; });

    fill(main, intro,
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
    fill(main,
      h("h2", { class: "view-title" }, "Creating your server…"),
      h("div", { class: "notice" }, h("div", { class: "row" }, h("span", { class: "spinner" }),
        h("span", { class: "grow" }, "Downloading Java, the mod loader, Minecraft and your mods, then checking that the server starts. This usually takes a few minutes."))),
      card("What's happening", events));
    every(1500, async () => {
      const r = await api(`/api/events?since=${seq}`).catch(() => null);
      if (!r) return;
      seq = r.last;
      for (const e of r.events) events.prepend(h("div", { class: "ev " + e.level }, h("time", {}, fmtClock(e.time)), h("span", {}, e.message)));
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
      for (const m of c.mods) st.mods.set(m.slug, { name: m.slug, required: m.required });
      if (c.memory_gb) st.memory_gb = c.memory_gb;
    }
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
