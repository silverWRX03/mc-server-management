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
function showLogin() {
  clearTimers();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-password").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
    $("#login-password").value = "";
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
  if (!s.notice_accepted) showNotice();
  else offerSelfUpdate(s.self_update);
  if (current && current.onStatus) current.onStatus(s);
}

$("#btn-start").addEventListener("click", () => act(() => api("/api/server/start", { method: "POST" })));
$("#btn-stop").addEventListener("click", () => {
  if (confirm("Stop the server? Players will be disconnected.")) act(() => api("/api/server/stop", { method: "POST" }));
});
$("#btn-restart").addEventListener("click", () => act(() => api("/api/server/restart", { method: "POST" })));

// -------------------------------------------------------------------- views
const views = {};

views.dashboard = () => {
  const statusBody = h("dl", { class: "kv" });
  const players = h("div");
  const update = h("div");
  const events = h("div", { class: "events" });
  let evSeq = 0;

  const render = (s) => {
    fill(statusBody, 
      h("dt", {}, "State"), h("dd", {}, h("span", { class: "pill " + s.state }, s.state)),
      h("dt", {}, "Uptime"), h("dd", {}, s.uptime ? fmtDuration(s.uptime) : "—"),
      h("dt", {}, "Minecraft"), h("dd", {}, s.minecraft || "not installed"),
      h("dt", {}, "Loader"), h("dd", {}, `${s.loader} ${s.loader_version || ""}`),
      h("dt", {}, "Java"), h("dd", {}, s.java_major ? `Java ${s.java_major}${s.java_forced ? ` (forced ${s.java_forced})` : ""}` : "—"),
      h("dt", {}, "Mods"), h("dd", {}, String(s.mods)),
      h("dt", {}, "Port"), h("dd", {}, s.port),
    );
    fill(players, 
      h("div", { class: "stat" }, `${s.players.length}`, h("span", { class: "muted small" }, ` / ${s.max_players}`)),
      s.players.length ? h("ul", { class: "list" }, s.players.map((p) => h("li", {}, p))) : h("p", { class: "empty" }, "Nobody online"),
      h("a", { href: "#players", class: "btn ghost" }, "Manage players →"),
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
    h("div", { class: "grid" }, card("Server", statusBody), card("Players", players), card("Updates", update)),
    h("div", { class: "card mt" }, h("h3", {}, "Activity"), events),
  );
  if (status) render(status);
  every(3000, pollEvents);
  return { onStatus: render };
};

views.console = () => {
  const out = h("div", { class: "console" });
  const input = h("input", { placeholder: "Type a command, e.g. say hello  (↑/↓ for history)", autocomplete: "off" });
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
    while (out.childElementCount > 3000) out.firstChild.remove();
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

  fill($("#main"), h("div", { class: "console-wrap" },
    out, h("div", { class: "console-input" }, input, h("button", { class: "btn primary", onclick: send }, "Send"))));
  input.focus();
  every(1000, poll);
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
        table("Used by mcsm", lic.runtime), table("Used only for development", lic.development),
        table("Downloaded for you", lic.downloaded),
        h("h3", { class: "mt-l" }, "Online services"),
        h("ul", { class: "list" }, lic.services.map((x) => h("li", {}, h("span", { class: "grow" }, x.name),
          x.url.startsWith("http") ? h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "terms ↗") : h("span", { class: "muted small" }, x.url)))))),
    );
  };
  fill($("#main"), h("h2", { class: "view-title" }, "Settings"), form, about);
  load();
  loadAbout();
  return {};
};

// ------------------------------------------------------------------- router
function route() {
  const name = (location.hash || "#dashboard").slice(1);
  const view = views[name] ? name : "dashboard";
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
