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
  if (!res.ok) throw new Error(data.error || res.statusText);
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
    h("form", { class: "card", onsubmit: async (e) => {
      e.preventDefault();
      const launchers = [...document.querySelectorAll("input[name=launcher]:checked")].map((x) => x.value);
      if (!launchers.length) { toast("Tick at least one launcher.", true); return; }
      go.disabled = true;
      try { await api("api/setup", { launchers, memory_gb: Number(memory.value) }); poll(); } catch (err) { toast(err.message, true); go.disabled = false; }
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
  render();
}
setInterval(() => fetch(`api/progress?since=${seen}`).catch(() => null), 30000);  // "still open"
load();
