"use strict";
// The store's page: the list of apps, an app's page and its build log, all
// rendered here from the JSON API. Every string from the API is untrusted
// (names, commit subjects and logs come from repositories), so it only ever
// goes in through textContent.

const $ = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const elapsed = seconds => {
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
};
const enc = encodeURIComponent;

let apps = [];             // from /api/apps
let loaded = false, loadError = null;
let sort = "date", tab = "all";
let armed = null;          // the one armed button: an app id, or "<id>/<key>" for a stored build
const ui = new Map();      // app id → {state: "building" | "failed" | "ready", text}
const following = new Set();
const rows = new Map();    // app id → its <li>, kept across refreshes so taps on it aren't lost
let view = null;           // the route on screen: {id, log} or null for the list
let page = null;           // what the detail view shows: {id, data, signature}
let listScroll = 0;

// MARK: - Elements

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function icon(name, url) {
  const box = el("div", "icon");
  if (url) {
    const img = el("img");
    img.alt = "";
    img.src = url;
    // A missing icon falls back to the letter, as an app without one has.
    img.onerror = () => img.replaceWith(el("div", "letter", (name || "?").slice(0, 1)));
    box.append(img);
  } else {
    box.append(el("div", "letter", (name || "?").slice(0, 1)));
  }
  return box;
}

// The latest build's icon URL has its key in it, so a new icon is never stale.
const iconUrl = app => (app.latest && app.latest.iconUrl) || app.iconUrl;

function chevron(color) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "chevron");
  svg.setAttribute("viewBox", "0 0 9 14");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M1.5 1.5l5.5 5.5-5.5 5.5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", "2.5");
  svg.append(path);
  return svg;
}

const buy = () => $("buy").content.firstElementChild.cloneNode(true);

function size(bytes) {
  let value = bytes || 0;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return unit === "B" ? `${value} B` : `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
}

function when(iso, withTime = true) {
  const date = new Date(iso);
  if (!iso || isNaN(date)) return iso || "";
  const options = { month: "short", day: "numeric" };
  if (date.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
  if (withTime) Object.assign(options, { hour: "numeric", minute: "2-digit" });
  return date.toLocaleString(undefined, options);
}

const versionOf = build => `${build.version} (${build.build})`;

// The detail line when nothing is happening.
function describe(app) {
  if (app.problem) return app.problem;
  const build = app.latest;
  const line = build ? [versionOf(build), size(build.size), build.source].filter(Boolean).join(" · ") : "";
  if (app.freshness === "current") return line ? "Up to date · " + line : "Up to date";
  if (app.freshness === "changed") return "Changed since last build";
  if (app.freshness === "unbuilt") return "Not built yet";
  return line || "No builds yet";
}

// Install links are itms-services URLs from the store; never follow anything
// that could run script.
const safe = url => typeof url === "string" && (/^(itms-services|https?):/i.test(url) || /^\/(?!\/)/.test(url));
function go(url) { if (safe(url)) location.href = url; }

// MARK: - API

async function api(path, options) {
  const response = await fetch(path, Object.assign({ cache: "no-store" }, options));
  if (!response.ok) {
    let message = `The store answered ${response.status}`;
    try { message = (await response.json()).error || message; } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response;
}
const json = async (path, options) => (await api(path, options)).json();

// MARK: - Buttons and builds

function appData(id) {
  return apps.find(app => app.id === id) || (page && page.id === id && page.data) || null;
}

// What the app's button shows: this page's own state, else a failure the
// store still remembers (finished jobs stay until the next request).
function stateOf(app) {
  const own = ui.get(app.id);
  if (own) return own;
  if (app.status && app.status.state === "failed")
    return { state: "failed", text: "Build failed: " + (app.status.error || "see the log on the Mac") };
  return null;
}

// Paints an app's buttons and detail line wherever it shows (its row and its page).
function paint(id) {
  const app = appData(id);
  if (!app) return;
  const own = stateOf(app);
  const canInstall = app.buildable || !!app.latest;
  for (const box of document.querySelectorAll("[data-app]")) {
    if (box.dataset.app !== id) continue;
    for (const state of ["building", "failed", "ready"]) box.classList.toggle(state, !!own && own.state === state);
    box.classList.toggle("armed", !own && armed === id);
    const price = box.querySelector(".price");
    price.textContent = app.freshness === "changed" ? "UPDATE" : "FREE";
    price.disabled = !canInstall;
    box.querySelector(".install").textContent = own && own.state === "failed" ? "RETRY" : "INSTALL";
    const line = box.querySelector(".detail");
    line.textContent = own && own.state !== "ready" ? own.text : describe(app);
    line.classList.toggle("problem", !own && !!app.problem);
  }
  const rebuild = document.querySelector(".cell.rebuild");
  if (rebuild && page && page.id === id) rebuild.classList.toggle("disabled", !!own && own.state === "building");
}

function paintAll() {
  for (const app of apps) paint(app.id);
  if (page && page.data && !apps.some(app => app.id === page.id)) paint(page.id);
  for (const cell of document.querySelectorAll("[data-build]")) cell.classList.toggle("armed", armed === cell.dataset.build);
}

// An armed button goes back to its price after this long without a tap on it, as in the store.
const ARMED_FOR = 2000;
let disarmTimer = null;

function arm(key) {
  armed = key;
  clearTimeout(disarmTimer);
  if (key !== null) disarmTimer = setTimeout(() => arm(null), ARMED_FOR);
  paintAll();
}

function set(id, state) {
  ui.set(id, state);
  paint(id);
}

// Always asks the store, even with a finished build at hand: the sources may
// have changed since, or the build been pruned. An unchanged one answers at once.
const tapInstall = id => follow(id, "install", true);

// Follows the build of an app until it's done, then installs it if
// `redirect`. `start` is "install" or "force" to start it with a POST, or
// false to follow one already running.
async function follow(id, start, redirect) {
  if (following.has(id)) return;
  following.add(id);
  if (armed === id) armed = null;
  const app = appData(id);
  const step = app?.status?.step;
  set(id, { state: "building", text: start ? "Starting…" : `${step || "Building"}… ${elapsed(app?.status?.elapsed || 0)}` });
  let status, final = null, failures = 0;
  try {
    while (true) {
      try {
        status = start
          ? await json(`/api/apps/${enc(id)}/install${start === "force" ? "?force=1" : ""}`, { method: "POST" })
          : await json(`/api/apps/${enc(id)}/status`);
        start = false;
        failures = 0;
      } catch (error) {
        // An answer from the store (409: nothing to install, 404: gone) is final.
        if (error.status && error.status < 500) { final = error.message; break; }
        // The phone may have slept or dropped off the tailnet for a moment.
        if (++failures > 30) { final = "Lost the store: " + error.message; break; }
        await sleep(2000);
        continue;
      }
      if (status.state !== "queued" && status.state !== "building") break;
      set(id, { state: "building", text: `${status.step || "Building"}… ${elapsed(status.elapsed || 0)}` });
      await sleep(2000);
    }
  } finally {
    following.delete(id);
  }
  if (final !== null) {
    set(id, { state: "failed", text: final });
  } else if (status.state === "done" && status.build && safe(status.build.installUrl)) {
    set(id, { state: "ready" });
    if (redirect) go(status.build.installUrl);
  } else if (status.state === "failed") {
    set(id, { state: "failed", text: "Build failed: " + (status.error || "see the log on the Mac") });
  } else {
    // Cancelled, or the store restarted under it: back to how the store sees it.
    ui.delete(id);
    paint(id);
  }
  refresh();
}

// MARK: - The list

const signature = app => JSON.stringify(Object.assign({}, app, { status: null }));

function row(app) {
  let li = rows.get(app.id);
  if (!li) {
    li = el("li");
    li.dataset.app = app.id;
    rows.set(app.id, li);
  }
  const sig = signature(app);
  if (li.dataset.sig !== sig) {
    li.dataset.sig = sig;
    const info = el("div", "info");
    info.append(el("div", "maker", app.bundleId || app.id), el("div", "name", app.name), el("div", "detail"));
    const progress = el("div", "progress");
    progress.append(el("div"));
    info.append(progress);
    const buttons = buy();
    buttons.append(chevron("#4c4f54"));
    li.replaceChildren(icon(app.name, iconUrl(app)), info, buttons);
  }
  return li;
}

function compare(a, b) {
  if (sort === "name") return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  if (sort === "size") return ((b.latest && b.latest.size) || 0) - ((a.latest && a.latest.size) || 0);
  // Newest build first, unbuilt last; ties keep the store's order.
  const at = app => (app.latest && app.latest.builtAt) || "";
  return at(b).localeCompare(at(a));
}

function renderList() {
  const list = $("apps");
  const ids = new Set(apps.map(app => app.id));
  for (const [id, li] of rows) if (!ids.has(id)) { li.remove(); rows.delete(id); }
  let shown = 0;
  for (const app of apps.slice().sort(compare)) {
    const li = row(app);
    li.hidden = tab === "updates" && app.freshness !== "changed";
    if (!li.hidden) shown++;
    list.append(li);
  }
  const updates = apps.filter(app => app.freshness === "changed").length;
  $("badge").textContent = updates;
  $("badge").hidden = !updates;
  const empty = $("empty");
  empty.hidden = shown > 0;
  empty.textContent = !loaded ? (loadError ? "Can’t reach the store." : "Loading…")
    : apps.length === 0 ? "No apps yet." : "All apps are up to date.";
  paintAll();
}

$("apps").addEventListener("click", event => {
  const li = event.target.closest("li");
  if (!li) return;
  const id = li.dataset.app;
  if (event.target.closest(".price")) {
    if (!event.target.closest(".price").disabled) arm(id);
  } else if (event.target.closest(".install")) {
    tapInstall(id);
  } else {
    open(`#/apps/${enc(id)}`);
  }
});

document.querySelectorAll("[data-sort]").forEach(button => button.onclick = () => {
  sort = button.dataset.sort;
  document.querySelectorAll("[data-sort]").forEach(b => b.setAttribute("aria-pressed", b === button));
  renderList();
});

document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => {
  tab = button.dataset.tab;
  document.querySelectorAll("[data-tab]").forEach(b => b.setAttribute("aria-pressed", b === button));
  // As in the store, a tab takes you back to its root.
  const depth = (history.state && history.state.depth) || 0;
  if (depth) history.go(-depth);
  else if (view) { history.replaceState(null, "", "#/"); route(); }
  renderList();
  if (!view) scrollTo(0, 0);
});

// Touching anywhere but the armed button disarms it, as in the store, and
// that tap does nothing else. It listens for pointerdown because iOS Safari
// sends no click for a tap on something that isn't clickable.
let swallowClick = false;
document.addEventListener("pointerdown", event => {
  swallowClick = armed !== null && !event.target.closest(".price, .install");
  if (swallowClick) arm(null);
}, true);
document.addEventListener("click", event => {
  if (!swallowClick) return;
  swallowClick = false;
  event.preventDefault();
  event.stopPropagation();
}, true);

// MARK: - Routing

// In-app navigation pushes history entries with their depth, so the back
// button (and Safari's swipe back) pop them; a page opened from a link has
// none, and its back button replaces it with its parent instead.
function open(hash) {
  const depth = ((history.state && history.state.depth) || 0) + 1;
  history.pushState({ depth }, "", hash);
  route();
}

$("back").onclick = () => {
  if (history.state && history.state.depth) history.back();
  else { history.replaceState(null, "", view && view.log ? `#/apps/${enc(view.id)}` : "#/"); route(); }
};

function parse() {
  const match = /^#\/apps\/([^/]+)(\/log)?$/.exec(location.hash);
  if (!match) return null;
  try { return { id: decodeURIComponent(match[1]), log: !!match[2] }; } catch (_) { return null; }
}

function route() {
  const next = parse();
  if (JSON.stringify(next) === JSON.stringify(view)) return;
  if (!view) listScroll = scrollY;
  view = next;
  armed = null;
  $("list-view").hidden = !!view;
  $("detail-view").hidden = !view;
  document.body.classList.toggle("in-detail", !!view);
  if (!view) {
    page = null;
    $("detail").replaceChildren();
    paintAll();
    requestAnimationFrame(() => scrollTo(0, listScroll));
    return;
  }
  if (!page || page.id !== view.id) page = { id: view.id, data: null, signature: null };
  page.signature = null;
  $("detail").replaceChildren();
  header();
  scrollTo(0, 0);
  loadPage();
}

addEventListener("popstate", route);
addEventListener("hashchange", route);
history.scrollRestoration = "manual";

function header() {
  const data = page && page.data;
  const name = (data && data.name) || (view && view.id) || "";
  $("detail-title").textContent = view.log ? "Build Log" : name;
  $("back").textContent = view.log ? name : tab === "updates" ? "Updates" : "Featured";
  // Keeps the title centred on the bar, as UIKit does, whatever the back button's width.
  const bar = $("back").parentElement;
  bar.style.setProperty("--back-width", $("back").offsetWidth + "px");
}

// MARK: - The app's page

async function loadPage() {
  if (!view) return;
  const shown = view;
  let data, log = null, missing = false;
  try {
    data = await json(`/api/apps/${enc(shown.id)}`);
    if (shown.log) {
      try { log = await (await api(`/api/apps/${enc(shown.id)}/log`)).text(); }
      catch (error) { if (error.status !== 404) throw error; }
    }
  } catch (error) {
    if (error.status === 404) missing = true;
    else if (page.data) return; // Keep what's on screen; the next refresh tries again.
    else data = null;
  }
  if (view !== shown) return;
  const sig = JSON.stringify([missing, Object.assign({}, data, { status: null }), log]);
  page.data = data || null;
  header();
  if (sig === page.signature) { paint(shown.id); return; }
  page.signature = sig;
  const content = $("detail");
  if (missing || !data) {
    content.replaceChildren(el("p", "empty", missing ? "This app isn’t in the store anymore." : "Can’t reach the store."));
    return;
  }
  if (shown.log) renderLog(content, log);
  else renderPage(content, data);
  paintAll();
}

function group(title, cells, note) {
  const section = el("section", "group");
  if (title) section.append(el("h2", "", title));
  const box = el("div", "cells");
  box.append(...cells);
  section.append(box);
  if (note) section.append(el("p", "note", note));
  return section;
}

function pair(label, value) {
  const cell = el("div", "cell");
  cell.append(el("span", "label", label), el("span", "value", value));
  return cell;
}

function renderPage(content, app) {
  const hero = el("div", "hero");
  hero.dataset.app = app.id;
  const info = el("div", "info");
  info.append(el("div", "name", app.name), el("div", "maker", app.bundleId || app.id), el("div", "detail"));
  const progress = el("div", "progress");
  progress.append(el("div"));
  info.append(progress);
  hero.append(icon(app.name, iconUrl(app)), info, buy());

  const groups = [];
  const commits = (app.commits || []).map(commit => {
    const cell = el("div", "cell");
    const text = el("div", "text");
    text.append(el("div", "commit", commit.subject),
                el("div", "sub", [String(commit.sha || "").slice(0, 7), when(commit.date, false)].filter(Boolean).join(" · ")));
    cell.append(text);
    return cell;
  });
  if (commits.length) groups.push(group("What’s New", commits));

  const latest = app.latest, info_ = app.info || {};
  const facts = [];
  if (latest) {
    facts.push(["Version", versionOf(latest)], ["Size", size(latest.size)],
               ["Configuration", latest.configuration], ["Source", latest.source],
               ["Built", when(latest.builtAt)],
               ["Build time", latest.duration != null ? elapsed(latest.duration) : null]);
    if (latest.expiresAt) facts.push(["Profile expires", when(latest.expiresAt, false)],
                                     ["Devices", latest.devices == null ? "All" : String(latest.devices)]);
  }
  facts.push(["Folder", info_.dir], info_.command ? ["Command", info_.command] : ["Scheme", info_.scheme]);
  const cells = facts.filter(([, value]) => value).map(([label, value]) => pair(label, value));
  if (cells.length) groups.push(group("Information", cells));

  const builds = (app.builds || []).map(build => {
    const cell = el("div", "cell");
    cell.dataset.build = `${app.id}/${build.key}`;
    const text = el("div", "text");
    text.append(el("div", "title", versionOf(build)),
                el("div", "sub", [build.source, size(build.size), when(build.builtAt)].filter(Boolean).join(" · ")));
    cell.append(text);
    if (build.installable && safe(build.installUrl)) {
      const buttons = buy();
      buttons.querySelector(".price").onclick = () => arm(cell.dataset.build);
      buttons.querySelector(".install").onclick = () => go(build.installUrl);
      cell.append(buttons);
    } else {
      cell.append(el("span", "expired", "Expired"));
    }
    return cell;
  });
  if (builds.length) groups.push(group(builds.length === 1 ? "Build" : "Builds", builds));

  const actions = [];
  const logCell = el("div", "cell link");
  logCell.append(el("span", "text", "View Build Log"), chevron("#8e8e93"));
  logCell.onclick = () => open(`#/apps/${enc(app.id)}/log`);
  actions.push(group("", [logCell]));
  if (app.buildable) {
    const rebuild = el("div", "cell action rebuild", "Rebuild");
    rebuild.onclick = () => {
      if (!confirm(`Build ${app.name} again, even though nothing changed?`)) return;
      scrollTo({ top: 0, behavior: "smooth" });
      follow(app.id, "force", true);
    };
    actions.push(group("", [rebuild], "For a phone added to the team since the last build: the stored build won’t install on it."));
  }

  const body = el("div");
  body.append(...groups, ...actions);
  content.replaceChildren(hero, body);
  hero.querySelector(".price").onclick = event => { if (!event.currentTarget.disabled) arm(app.id); };
  hero.querySelector(".install").onclick = () => tapInstall(app.id);
}

function renderLog(content, log) {
  // Stay at the end if the reader was there: the end is where errors are.
  const atEnd = !content.firstChild || innerHeight + scrollY >= document.documentElement.scrollHeight - 40;
  const pre = el("pre", "log", log === null ? "No build log yet." : log || "The log is empty.");
  const section = group("", [pre]);
  section.style.marginTop = "18px";
  content.replaceChildren(section);
  if (atEnd) scrollTo(0, document.documentElement.scrollHeight);
}

// MARK: - Refreshing

let refreshing = false, again = false, first = true;

async function refresh() {
  if (refreshing) { again = true; return; }
  refreshing = true;
  try {
    const data = await json("/api/apps");
    apps = Array.isArray(data.apps) ? data.apps : [];
    // A finished build stops being ready once the sources moved on from it.
    for (const app of apps) {
      if (ui.get(app.id)?.state === "ready" && app.freshness !== "current") ui.delete(app.id);
    }
    if (data.title) document.title = data.title;
    loaded = true;
    loadError = null;
  } catch (error) {
    loadError = error;
  } finally {
    refreshing = false;
  }
  renderList();
  // Follows builds started elsewhere. One already running when the page
  // opened is most likely this phone's own tap from before a reload, so it
  // installs when done, as the old page did; later ones just end up ready.
  for (const app of apps) {
    if (app.status && (app.status.state === "queued" || app.status.state === "building")) follow(app.id, false, first);
  }
  if (loaded) first = false;
  if (view) loadPage();
  if (again) { again = false; refresh(); }
}

setInterval(() => { if (!document.hidden) refresh(); }, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

route();
refresh();
