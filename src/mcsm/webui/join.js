"use strict";
// The friend's page ("mcfui"): pick launchers, add the server to them, watch progress.
const $ = (sel) => document.querySelector(sel);
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-MCSM": "1" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.error || res.statusText); e.data = data; e.status = res.status; throw e; }
  return data;
}
function toast(message, bad = false) {
  const el = h("div", { class: "toast" + (bad ? " bad" : "") }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 7000);
}

const LOADERS = { fabric: "Fabric", quilt: "Quilt", neoforge: "NeoForge", forge: "Forge", vanilla: "no mod loader" };
const OPEN_LABEL = { minecraft: "Open the Minecraft Launcher", prism: "Launch in Prism", modrinth: "Open the file", curseforge: "Show the file" };
let info = null;
let seen = 0;
let extras = { items: [], kinds: {} };
const announced = new Set();  // mods that came along with an extra, already mentioned

// ---------------------------------------------------------- your extras
// Shaders, resource packs and more mods on top of the server's own, picked from Modrinth in a
// panel that slides in (like mcsm's mod browser); mods bring what they need along.
const KIND = { shader: ["Shaders", "✨", "shaders"], resourcepack: ["Resource packs", "🎨", "resource packs"], mod: ["More mods", "🧩", "mods"] };
function extrasCard() {
  const list = h("div", { id: "extras-list" });
  const renderList = () => {
    list.replaceChildren(extras.items.length ? h("ul", { class: "list" }, extras.items.map((i) => h("li", {},
      h("span", { class: "grow" }, h("strong", {}, i.name), " ", h("span", { class: "tag" }, KIND[i.kind][2].replace(/s$/, "")),
        i.enabled === false ? h("span", { class: "tag warn" }, "switched off") : null),
      i.kind !== "mod" ? h("label", { class: "row small", title: i.enabled === false ? "It may not work on this Minecraft version; switching it on may crash the game" : "" },
        h("input", { type: "checkbox", checked: i.enabled !== false, onchange: async (e) => {
          if (e.target.checked && i.enabled === false && !confirm(`${i.name} may not work on Minecraft ${extras.minecraft}. Switching it back on may crash the game. Switch it on anyway?`)) { e.target.checked = false; return; }
          extras = await api("api/extras/enable", { id: i.id, enabled: e.target.checked }).catch((err) => { toast(err.message, true); return extras; });
          renderList();
        } }), "on") : null,
      h("button", { class: "btn small ghost", onclick: async () => {
        extras = await api("api/extras/remove", { id: i.id }).catch((err) => { toast(err.message, true); return extras; });
        renderList();
      } }, "Remove"))))
      : h("p", { class: "muted small" }, "Nothing added: you'll get exactly what the server needs."));
  };
  renderList();
  extrasCard.refresh = renderList;
  const buttons = Object.entries(KIND).filter(([k]) => extras.kinds[k]).map(([k, [label, icon]]) =>
    h("button", { type: "button", class: "btn", onclick: () => openPicker(k) }, `${icon} ${label}`));
  return h("div", { class: "card" },
    h("h2", {}, "Make it yours (optional)"),
    h("p", { class: "muted small" }, "Add shaders, resource packs or other mods that only run on your computer. They're installed with the server's mods, " +
      "and mods bring what they need along. The server doesn't need them."),
    h("div", { class: "row wrap" }, buttons),
    list);
}
function openPicker(kind) {
  closePicker();
  const [label, , noun] = KIND[kind];
  const q = h("input", { type: "search", placeholder: `Search ${noun}…`, "aria-label": `Search ${noun}` });
  const results = h("div", { class: "jresults" }, h("p", { class: "muted" }, "Loading…"));
  let timer, seq = 0;
  const search = async () => {
    const mine = ++seq;
    let r;
    try { r = await api(`api/extras/search?kind=${kind}&q=${encodeURIComponent(q.value.trim())}`); }
    catch (e) { results.replaceChildren(h("div", { class: "notice bad" }, e.message)); return; }
    if (mine !== seq) return;
    const have = new Set(extras.items.map((i) => i.id));
    results.replaceChildren(...(r.results.length ? r.results.map((m) => h("div", { class: "mod" },
      m.icon ? h("img", { src: m.icon, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: "noicon" }),
      h("div", { class: "info" }, h("div", { class: "name" }, m.name), h("div", { class: "desc" }, m.summary)),
      have.has(m.id) ? h("span", { class: "tag ok" }, "added") : h("button", { class: "btn small primary", onclick: async (e) => {
        e.target.disabled = true;
        try {
          const res = await api("api/extras/add", { kind, id: m.id, slug: m.slug, name: m.name });
          extras = res;
          const adds = res.adds.filter((a) => !announced.has(a.name));  // what came along this time
          adds.forEach((a) => announced.add(a.name));
          toast(`Added ${m.name}` + (adds.length ? `, with ${adds.map((a) => `${a.name} (needed by ${a.needed_by})`).join(", ")}` : ""));
          extrasCard.refresh && extrasCard.refresh();
          search();
        } catch (err) { toast(err.message, true); e.target.disabled = false; }
      } }, "Add"))) : [h("p", { class: "muted" }, `No ${noun} found for Minecraft ${info.pack.minecraft}.`)]));
  };
  q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });
  const rail = h("button", { class: "jrail", type: "button", "aria-label": "Back", onclick: closePicker }, "‹ Back");
  const panel = h("section", { class: "jpanel", "aria-label": label },
    h("div", { class: "row" }, h("h2", { class: "grow" }, `${label} for Minecraft ${info.pack.minecraft}`), h("button", { class: "btn ghost small", onclick: closePicker }, "Done")),
    kind === "shader" ? h("p", { class: "muted small" }, "Shaders need a shader loader (Iris, or Oculus on Forge); it's added for you. They need a good graphics card.") : null,
    q, results);
  document.body.append(rail, panel);
  document.body.classList.add("picking");
  q.focus();
  search();
}
function closePicker() {
  document.body.classList.remove("picking");
  document.querySelectorAll(".jrail, .jpanel").forEach((x) => x.remove());
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePicker(); });

// Before installing: extras that don't fit the server's Minecraft need the friend's say-so.
function confirmChanges(changes) {
  return new Promise((resolve) => {
    const done = (v) => { back.remove(); resolve(v); };
    const back = h("div", { class: "modal-backdrop", role: "dialog", "aria-modal": "true" }, h("div", { class: "modal" },
      h("h2", {}, `The server now runs Minecraft ${info.pack.minecraft}`),
      h("p", {}, "Some of your extras don't have a version for it yet:"),
      h("ul", { class: "list" }, changes.map((c) => h("li", {}, h("span", { class: "grow" }, h("strong", {}, c.name),
        h("div", { class: "small muted" }, c.reason)), h("span", { class: "tag " + (c.action === "removed" ? "bad" : "warn") }, c.action)))),
      h("p", { class: "small" }, "If you continue, mods without a version are removed, and resource packs and shaders are kept but ",
        h("strong", {}, "switched off"), ". You can switch them back on later, but the game may crash."),
      h("p", { class: "small muted" }, "If you cancel, nothing changes, but you can't join the updated server until you continue."),
      h("div", { class: "row mt" }, h("button", { class: "btn primary", onclick: () => done(true) }, "Continue"),
        h("button", { class: "btn ghost", onclick: () => done(false) }, "Cancel"))));
    document.body.append(back);
  });
}

function render() {
  const root = $("#join");
  if (!info.pack) {
    root.replaceChildren(h("div", { class: "card" }, h("h1", {}, "Couldn't reach the server"),
      h("div", { class: "notice bad" }, info.error || "The server didn't answer."),
      h("p", { class: "muted" }, "Check that the server is running and try again, or ask its owner for a new invite."),
      h("button", { class: "btn primary", onclick: load }, "Try again")));
    return;
  }
  const p = info.pack;
  const boxes = info.launchers.map((l) => {
    const box = h("input", { type: "checkbox", name: "launcher", value: l.key, checked: l.found });
    return h("label", { class: "choice launcher" + (l.found ? "" : " missing") }, box,
      h("span", { class: "grow" }, h("strong", {}, l.label), l.found ? h("span", { class: "tag ok" }, "found") : h("span", { class: "tag" }, "not found"),
        l.note ? h("span", { class: "small muted block" }, l.note) : null));
  });
  if (!info.launchers.some((l) => l.found)) boxes[0].querySelector("input").checked = true;
  const go = h("button", { class: "btn primary big", type: "submit" }, "Add to my launchers");
  // Memory for this Minecraft: the server owner's suggestion, changeable to suit this computer.
  const sys = info.system_gb;
  const choices = [2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 24, 32].filter((g) => !sys || g <= Math.max(2, sys - 2) || g === p.memory_gb);
  if (!choices.includes(p.memory_gb)) choices.push(p.memory_gb);
  const memory = h("select", { name: "memory", "aria-label": "Memory for Minecraft" },
    choices.sort((a, b) => a - b).map((g) => h("option", { value: String(g) }, `${g} GB${g === p.memory_gb ? " (suggested by the server)" : ""}`)));
  memory.value = String(p.memory_gb);
  const memHint = h("span", { class: "small muted block" });
  const updateHint = () => {
    const g = Number(memory.value);
    memHint.textContent = sys ? `This computer has ${sys} GB.` + (g > sys - 3 ? " Leave some for the rest of the computer, or Minecraft may crash." : "")
      + (g < p.memory_gb ? " Less than suggested: big modpacks may run slowly or crash." : "") : "";
  };
  memory.addEventListener("change", updateHint);
  updateHint();
  root.replaceChildren(
    h("div", { class: "join-head" },
      p.icon ? h("img", { src: p.icon, alt: "" }) : h("div", { class: "noicon" }),
      h("div", {}, h("h1", {}, p.name), h("div", { class: "muted" }, p.address))),
    h("div", { class: "card" },
      h("p", {}, `This server runs Minecraft ${p.minecraft} with ${LOADERS[p.loader] || p.loader}` +
        (p.loader_version && p.loader !== "vanilla" ? ` ${p.loader_version}` : "") +
        (p.mods.length ? ` and ${p.mods.length} mod${p.mods.length === 1 ? "" : "s"} you need too.` : ".")),
      p.mods.length ? h("details", {}, h("summary", {}, "Show the mods"), h("ul", { class: "small" }, p.mods.map((m) => h("li", {}, m)))) : null,
      h("p", { class: "muted small" }, "mcsm downloads Minecraft's mods straight from Modrinth and CurseForge, checks every file, " +
        "and keeps them in a folder of their own: your other worlds and installations aren't touched. It never asks for your " +
        "Microsoft password; your launcher signs you in.")),
    extrasCard(),
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      const launchers = [...document.querySelectorAll("input[name=launcher]:checked")].map((x) => x.value);
      if (!launchers.length) { toast("Tick at least one launcher.", true); return; }
      go.disabled = true;
      const body = { launchers, memory_gb: Number(memory.value) };
      try { await api("api/setup", body); poll(); }
      catch (err) {
        if (err.status === 409 && err.data && err.data.changes) {
          if (await confirmChanges(err.data.changes)) {
            try { await api("api/setup", { ...body, accept_changes: true }); extras = await api("api/extras").catch(() => extras); poll(); return; }
            catch (e2) { toast(e2.message, true); }
          } else toast("Nothing was changed. You can't join the updated server until you continue.", true);
        } else toast(err.message, true);
        go.disabled = false;
      }
    } },
      h("h2", {}, "Which launchers should have this server?"),
      h("div", { class: "launchers" }, boxes),
      h("label", { class: "mt memory" }, "Memory for Minecraft", memory, memHint),
      h("div", { class: "row mt" }, go)),
    h("div", { id: "progress", class: "card hidden" }, h("h2", {}, "Progress"), h("pre", { id: "log", class: "log" }), h("div", { id: "results" })),
  );
}

async function poll() {
  $("#progress").classList.remove("hidden");
  const r = await api(`api/progress?since=${seen}`).catch(() => null);
  if (r) {
    seen = r.next;
    const log = $("#log");
    log.textContent += r.lines.map((x) => x + "\n").join("");
    log.scrollTop = log.scrollHeight;
    if (!r.running) { showResults(r.results); return; }
  }
  setTimeout(poll, 800);
}

function showResults(results) {
  const p = info.pack;
  const ok = results.filter((r) => r.ok);
  $("#results").replaceChildren(...[
    h("ul", { class: "list" }, results.map((r) => h("li", {},
      h("div", { class: "grow" }, h("strong", {}, r.label), h("span", { class: "tag " + (r.ok ? "ok" : "bad") }, r.ok ? "ready" : "failed"),
        h("div", { class: "small muted" }, r.message)),
      r.ok ? h("button", { class: "btn small", onclick: () => api("api/open", { launcher: r.launcher }).then((x) => x.ok || toast("Couldn't open it; open it yourself.", true)) },
        OPEN_LABEL[r.launcher]) : null))),
    p.manual.length ? h("div", { class: "notice warn mt" }, h("strong", {}, "Download these yourself: "),
      "their authors don't allow automatic downloads. Put them in the instance's mods folder.",
      h("ul", {}, p.manual.map((m) => h("li", {}, h("a", { href: m.url, target: "_blank", rel: "noopener noreferrer" }, m.name))))) : null,
    ok.length ? h("div", { class: "notice mt" }, h("strong", {}, "Next: "),
      `pick "${p.name}" in your launcher and press Play. ` +
      (p.quick_play ? "Minecraft joins the server by itself." : `Then choose Multiplayer: ${p.name} is in the list.`) +
      " If the server updates later, run the download again to update your mods.") : null,
    h("div", { class: "row mt" }, h("button", { class: "btn", onclick: async () => {
      await api("api/quit", {}).catch(() => null);
      document.body.replaceChildren(h("main", { class: "join" }, h("div", { class: "card" }, h("h1", {}, "All done"),
        h("p", { class: "muted" }, "You can close this tab."))));
    } }, "I'm done"))].filter(Boolean));
}

async function load() {
  try { info = await api("api/info"); } catch (e) { info = { pack: null, error: e.message, launchers: [] }; }
  if (info.pack) extras = await api("api/extras").catch(() => extras);
  render();
}
setInterval(() => fetch(`api/progress?since=${seen}`).catch(() => null), 30000);  // "still open"
load();
