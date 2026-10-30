/* Bout en bout OpenTV Canvas (tests seulement, jamais installé) : VRAIE page /tv du dépôt (tv.html, tv.css, canvas.js,
   keys.js, tv.js, qrcode.js) dans Chromium 1920×1080 sous la CSP du service ; l'API /api/tv/* est simulée ici pour
   piloter retards, pertes et désordre des réponses. Aucune requête ne sort de l'origine simulée (comptées, refusées).
   argv[2] = module playwright ; argv[3] = JSON {chromium, ui, qrcode, csp, shots, presets}. Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const ORIGIN = "http://opentv.test";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (o) => JSON.parse(JSON.stringify(o));
const P = cfg.presets;
const ROW_TITLES = { tonight: "Le choix de ce soir", watchlist: "À voir plus tard", "new": "Nouveautés disponibles",
                     top: "Les mieux notés que tu n'as pas vus", more: "Autres suggestions" };
const GENRES = [["Science-fiction", "Aventure"], ["Thriller"], ["Comédie"], ["Drame"], ["Animation", "Famille"], ["Policier"]];

function item(n, title) {
  return { id: "films/" + n, title: title || ("Film " + n), year: 2014 + (n % 12), genres: GENRES[n % GENRES.length],
           description: "Résumé du film " + n + ". Une histoire qui se regarde à deux.",
           imdb: { rating: 6 + (n % 30) / 10, votes: 1000 + n }, reasons: [{ code: "genre", text: "Parce que tu aimes " +
           GENRES[n % GENRES.length][0].toLowerCase() }] };
}
function fyPopulated(longTitle, because) {
  const rows = [], counts = because ? { tonight: 3, watchlist: 5, because: 4, "new": 9, top: 10, more: 8 } :
    { tonight: 3, watchlist: 5, "new": 9, top: 10, more: 8 };
  let n = 1;
  ROW_TITLES.because = "Parce que tu as aimé « Film 1 »";
  Object.keys(counts).forEach((id) => {
    const items = [];
    for (let i = 0; i < counts[id]; i++) {
      items.push(item(n, longTitle && n === 1 ? "Le Jardin des Possibles, une très longue odyssée familiale au bout du monde" : null));
      n += 1;
    }
    rows.push({ id: id, title: ROW_TITLES[id], items: items });
  });
  return { note: "", marks: {}, rows: rows };
}
const FY_SPARSE = { note: "", marks: {}, rows: [] };
function view(scene, extra) {
  return Object.assign({ status: "ok", state: "default", revision: 0, hash: "0".repeat(64), scene: clone(scene),
                         preset: "canvas", can_rollback: false, preview: null, sheet: null }, extra || {});
}
function baseState() {
  return { device: "Salon", configured: true, version: "test", network: { mode: "wifi", ip: "192.168.2.40" },
           setup: { active: false }, message: null, agents: [], cec: { adapters: 1 },
           assistants: { authorized: 1, connected: 0, list: [], manage: { active: false } }, muse: {}, foryou: true,
           remote: { enabled: true, url: "http://192.168.2.40:8097/r/pair?k=0123456789abcdef0123456789abcdef", phones: 0 },
           playback: { state: "idle", seq: 0, available: true }, canvas: view(P.canvas), actions: { pending: [] } };
}
function changes(from, to) {
  return ["wallpaper", "motion", "accent", "shade", "hero_focus", "row_order"].filter(
    (f) => JSON.stringify(from[f]) !== JSON.stringify(to[f])).map((f) => ({ field: f, from: from[f], to: to[f] }));
}
function setPreview(st, pid, scene, opts) {
  opts = opts || {};
  const exp = opts.exp === undefined ? 25 : opts.exp;
  st.canvas.preview = { id: pid, proposed_by: "muse", hash: "a".repeat(64), preset: "cinema", scene: clone(scene),
                        base_revision: st.canvas.revision, expires_in_s: exp };
  st.canvas.sheet = opts.sheet === false ? null : {
    preview_id: pid, proposed_by: "muse", agent_name: "Muse", preset: "cinema", expires_in_s: exp,
    changes: changes(st.canvas.scene, scene), focus: "no", choices: ["no", "yes"], timeout_action: "reject" };
}
function clearPreview(st) { st.canvas.preview = null; st.canvas.sheet = null; }
const pid = (n) => ("p" + n + "0".repeat(32)).slice(0, 32).replace(/p/, "a");

// ---------------------------------------------------------------- API simulée
class Fake {
  constructor(opts) {
    this.state = baseState();
    this.fy = opts.fy || fyPopulated(false);
    this.log = []; this.seq = 0; this.external = [];
    this.busy = null; this.inflightUi = 0; this.maxInflightUi = 0; this.uiDelays = []; this.uiFail = 0;
    this.uiResponses = []; this.eventDelays = []; this.eventFail = 0; this.keepPreview = false;
    this.stateFail = 0; this.sheetServed = 0; this.remoteQ = []; this.rseq = 0; this.hooks = {};
    this.play = { state: "idle", seq: 0 };
    this.noTransitions = !!opts.noTransitions;
  }
  file(name) {
    const p = name === "qrcode.js" ? cfg.qrcode : path.join(cfg.ui, name);
    return fs.readFileSync(p);
  }
  async handle(route) {
    const req = route.request(), url = new URL(req.url());
    if (url.origin !== ORIGIN) { this.external.push(req.url()); return route.abort(); }
    const p = url.pathname, m = req.method();
    const H = { "content-security-policy": cfg.csp, "cache-control": "no-cache" };
    if (p === "/tv") {
      const html = this.file("tv.html").toString("utf8").replace("{{TV_KEY}}", "k-test");
      return route.fulfill({ status: 200, headers: Object.assign({ "content-type": "text/html; charset=utf-8" }, H), body: html });
    }
    if (p.indexOf("/static/") === 0) {
      const name = p.slice(8), types = { "tv.js": "application/javascript", "keys.js": "application/javascript",
        "canvas.js": "application/javascript", "qrcode.js": "application/javascript", "tv.css": "text/css" };
      if (!types[name]) { return route.fulfill({ status: 404, body: "introuvable" }); }
      let body = this.file(name);
      if (name === "tv.css" && this.noTransitions) {   // mesures géométriques déterministes (feuille du test seulement)
        body = Buffer.concat([body, Buffer.from("\n*,*::before,*::after{transition:none!important}\n")]);
      }
      return route.fulfill({ status: 200, headers: Object.assign({ "content-type": types[name] + "; charset=utf-8" }, H), body: body });
    }
    let body = null;
    try { body = req.postDataJSON(); } catch (e) { body = null; }
    const e = { i: this.seq++, t: Date.now(), m: m, p: p, q: url.search, b: body };
    this.log.push(e);
    const json = (obj, status) => route.fulfill({ status: status || 200, headers: Object.assign(
      { "content-type": "application/json" }, H), body: JSON.stringify(obj) });
    const hook = this.hooks[m + " " + p];
    if (hook) { const r = await hook(e, this); if (r === "abort") { return route.abort(); } if (r) { return json(r.json, r.status); } }
    if (m === "GET" && p === "/api/tv/state") {
      if (this.stateFail > 0) { this.stateFail -= 1; return route.abort(); }
      if (this.state.canvas.sheet) { this.sheetServed += 1; }
      return json(clone(this.state));
    }
    if (m === "GET" && p === "/api/tv/foryou") { return json(this.fy); }
    if (m === "GET" && p === "/api/tv/play") { return json(this.play); }
    if (m === "GET" && p === "/api/tv/remote") {
      const first = url.searchParams.get("after") === "-1";
      for (let i = 0; i < 16 && !first && !this.remoteQ.length; i++) { await sleep(50); }
      const ev = first ? [] : this.remoteQ.splice(0);
      if (ev.length) { this.rseq += 1; }
      return json({ seq: this.rseq, events: ev, idle: false });
    }
    if (m === "POST" && p === "/api/tv/canvas/event") {
      const d = this.eventDelays.length ? this.eventDelays.shift() : 0;
      if (d) { await sleep(d); }
      if (this.eventFail > 0) { this.eventFail -= 1; return json({ ok: false, error: "invalid_event" }, 400); }
      const had = !!this.state.canvas.preview;
      if (!this.keepPreview) { clearPreview(this.state); }
      return json({ ok: true, preview_cancelled: had });
    }
    if (m === "POST" && p === "/api/tv/canvas/ui") {
      this.inflightUi += 1;
      this.maxInflightUi = Math.max(this.maxInflightUi, this.inflightUi);
      const d = this.uiDelays.length ? this.uiDelays.shift() : 0;
      if (d) { await sleep(d); }
      this.inflightUi -= 1;
      if (this.uiFail > 0) { this.uiFail -= 1; return json({ ok: false, error: "invalid_event" }, 400); }
      this.busy = body && body.busy;
      this.uiResponses.push({ t: Date.now(), busy: this.busy });
      return json({ ok: true, error: null });
    }
    if (m === "POST" && p.indexOf("/api/tv/canvas/") === 0) {
      return json({ ok: true, error: null, outcome: "applied", result: {}, canvas: clone(this.state.canvas) });
    }
    if (m === "POST" && p === "/api/tv/action") { return json({ ok: false, error: "gone" }, 409); }
    if (m === "POST" && p === "/api/tv/play") { this.play = { state: "loading", seq: 1 }; return json({ ok: true, playback: this.play }); }
    if (m === "POST") { return json({ ok: true, state: { opinion: null }, message: "ok" }); }
    return route.fulfill({ status: 404, body: "introuvable" });
  }
  posts(prefix, since) { return this.log.filter((x) => x.m === "POST" && x.p.indexOf(prefix) === 0 && x.i >= (since || 0)); }
}

const INIT = `
window.__ev = []; window.__hidden = false;
Object.defineProperty(document, "hidden", { configurable: true, get: function () { return window.__hidden; } });
(function () {
  var of = window.fetch;
  window.fetch = function (p, o) {
    var b = null;
    try { b = o && o.body ? JSON.parse(o.body) : null; } catch (e) { b = null; }
    window.__ev.push({ k: "fetch", t: performance.now(), m: (o && o.method) || "GET", p: String(p), b: b });
    return of.apply(this, arguments);
  };
  var d = Object.getOwnPropertyDescriptor(Element.prototype, "className");
  Object.defineProperty(Element.prototype, "className", { configurable: true,
    get: function () { return d.get.call(this); },
    set: function (v) {
      var old = d.get.call(this);
      if (/\\bfocused\\b/.test(v) && !/\\bfocused\\b/.test(old)) {
        window.__ev.push({ k: "focus", t: performance.now(), txt: (this.textContent || "").slice(0, 40) });
      }
      d.set.call(this, v);
    } });
  var h = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "hidden");
  Object.defineProperty(HTMLElement.prototype, "hidden", { configurable: true,
    get: function () { return h.get.call(this); },
    set: function (v) {
      if ((this.id === "panel" || this.id === "playov") && !!v !== h.get.call(this)) {
        window.__ev.push({ k: "focus", t: performance.now(), txt: this.id + (v ? ":hide" : ":show") });
      }
      h.set.call(this, v);
    } });
})();`;

async function open(browser, opts) {
  opts = opts || {};
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1,
                                         reducedMotion: "no-preference" });
  const page = await ctx.newPage();
  const F = new Fake(opts);
  if (opts.state) { opts.state(F.state); }
  if (opts.before) { opts.before(F); }
  await page.addInitScript(INIT);
  await ctx.route("**/*", (route) => F.handle(route));
  if (opts.clock) { await page.clock.install(); }
  await page.goto(ORIGIN + "/tv");
  if (!opts.clock) {
    await page.waitForFunction(() => document.body.getAttribute("data-cv-mode") === "run" ||
                                      document.body.getAttribute("data-cv-mode") === "static" ||
                                      document.body.className === "cv-setup", null, { timeout: 15000 });
    if (!opts.sparse && !opts.setupFirst) { await page.waitForSelector(".card.focused", { timeout: 15000 }); }
  }
  return { ctx: ctx, page: page, F: F };
}
const focusOf = (page) => page.evaluate(() => {
  const f = document.querySelector(".card.focused .t");
  if (f) { return f.textContent; }
  const b = document.querySelector("#pbtns .focused, #cvbtns .focused");
  if (b && !b.closest("[hidden]")) { return "btn:" + (b.getAttribute("data-label") || b.textContent); }
  return document.getElementById("gear").className.indexOf("focused") >= 0 ? "gear" : null;
});
const bodyCls = (page) => page.evaluate(() => document.body.className);
const sheetShown = (page) => page.evaluate(() => !document.getElementById("cvsheet").hidden);
const sheetFocus = (page) => page.evaluate(() => { const b = document.querySelector("#cvbtns .focused"); return b ? b.textContent : null; });
const toastText = (page) => page.evaluate(() => { const t = document.getElementById("toast"); return t.hidden ? "" : t.textContent; });
const rowTitles = (page) => page.evaluate(() => Array.prototype.map.call(document.querySelectorAll("#rowsin .row h2"), (h) => h.textContent));
const probeRows = (page) => page.evaluate(() => { const r = document.querySelector("#rowsin .row"); if (r) { r.__probe = 1; } });
const probed = (page) => page.evaluate(() => { const r = document.querySelector("#rowsin .row"); return !!(r && r.__probe === 1); });
const drain = (page) => page.evaluate(() => window.__ev.splice(0));
async function until(fn, ms) {
  const end = Date.now() + (ms || 5000);
  while (Date.now() < end) { if (await fn()) { return true; } await sleep(50); }
  return false;
}
const anims = (page) => page.evaluate(() => {
  const all = document.getAnimations().filter((a) => a.animationName !== undefined);
  const running = all.filter((a) => a.playState === "running");
  return { total: all.length, running: running.length, mode: document.body.getAttribute("data-cv-mode"),
           degraded: document.body.getAttribute("data-cv-degraded"), body: document.body.className,
           anims: running.map((a) => {
             const props = {};
             a.effect.getKeyframes().forEach((k) => Object.keys(k).forEach((x) => {
               if (["offset", "easing", "composite", "computedOffset"].indexOf(x) < 0) { props[x] = 1; } }));
             const t = a.effect.target;
             return { name: a.animationName, target: t.id || t.className, props: Object.keys(props) };
           }) };
});
async function waitMode(page, want, ms) {
  await until(async () => (await page.evaluate(() => document.body.getAttribute("data-cv-mode"))) === want, ms || 4000);
  await sleep(120);
  return anims(page);
}
const press = async (page, key, wait) => { await page.keyboard.press(key); await sleep(wait === undefined ? 150 : wait); };

// géométrie 1920×1080 (mêmes règles que le harnais v23 de test_remote)
const GEOM = () => {
  const $ = (id) => document.getElementById(id);
  const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
  const vis = (e) => !!e && !e.hidden && e.getClientRects().length > 0 && getComputedStyle(e).display !== "none";
  const inter = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5;
  const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
  const out = { ringClip: [], cardQrOverlap: [], outside: [] };
  const fe = document.querySelector(".card.focused");
  if (fe) {
    const cs = getComputedStyle(fe), m = /matrix\(([-\d.e]+)/.exec(cs.transform || "");
    const ext = [0, 0, 0, 0];   // débordement réel de chaque ombre extérieure : gauche, haut, droite, bas
    (cs.boxShadow || "").replace(/rgba?\([^)]*\)/g, "").split(",").forEach((s) => {
      const n = s.trim().split(/\s+/).map(parseFloat).filter((x) => !isNaN(x));
      if (n.length >= 4 && !/inset/.test(s)) {
        const b = n[2] + n[3];
        ext[0] = Math.max(ext[0], b - n[0]); ext[1] = Math.max(ext[1], b - n[1]);
        ext[2] = Math.max(ext[2], b + n[0]); ext[3] = Math.max(ext[3], b + n[1]);
      }
    });
    const k = m ? parseFloat(m[1]) : 1, r = R(fe);
    const ring = [r[0] - ext[0] * k, r[1] - ext[1] * k, r[2] + ext[2] * k, r[3] + ext[3] * k];
    for (let a = fe.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const ac = getComputedStyle(a);
      if ((ac.overflowX !== "visible" || ac.overflowY !== "visible") && !inside(ring, R(a))) {
        out.ringClip.push((a.id || a.className) + " ring=" + ring.map(Math.round) + " clip=" + R(a).map(Math.round));
      }
    }
    if (!inside(ring, [0, 0, 1920, 1080])) { out.ringClip.push("viewport"); }
  }
  const box = $("remotebox"), main = $("main");
  if (vis(box)) {
    Array.prototype.forEach.call(document.querySelectorAll(".card"), (c) => {
      const r = R(c), mm = R(main), p = [Math.max(r[0], mm[0]), Math.max(r[1], mm[1]), Math.min(r[2], mm[2]), Math.min(r[3], mm[3])];
      if (inter(r, mm) && inter(p, [0, 0, 1920, 1080]) && inter(p, R(box))) { out.cardQrOverlap.push(c.textContent.slice(0, 30)); }
    });
  }
  const pan = vis($("panel")) ? $("panel") : (vis($("cvsheet")) ? $("cvsheet") : null);
  out.panelVisible = !!pan;
  out.panelQrOverlap = !!(pan && vis(box) && inter(R(pan), R(box)));
  out.panelHiddenBy = [];
  if (pan) {
    const btns = pan.id === "panel" ? $("pbtns").children : $("cvbtns").children;
    [pan.querySelector("h2")].concat(Array.prototype.slice.call(btns)).forEach((e) => {
      const r = R(e), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
      if (!h || !pan.contains(h)) { out.panelHiddenBy.push(e.textContent.slice(0, 30)); }
    });
  }
  const help = $("help");
  out.footerOverlap = vis(help) && [main, $("hero"), box].some((e) => vis(e) && inter(R(e), R(help)));
  ["clock", "date", "cvpill", "gear", "glabel", "device", "net", "hint", "remotebox", "panel", "cvsheet", "hero", "htitle",
   "hmeta", "toast", "help"].forEach((id) => {
    const e = $(id);
    if (vis(e) && !inside(R(e), [0, 0, 1920, 1080])) { out.outside.push(id + " " + R(e).map(Math.round)); }
  });
  const q = $("rqr");
  out.qrVisible = vis(box) && q.getAttribute("data-qr") === "ok" && (() => {
    const r = R(q), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
    return h === q;
  })();
  out.heroInMain = inside(R($("hero")), [R(main)[0] - 0.5, R(main)[1], R(main)[2] + 0.5, R(main)[3]]);
  out.mainRight = R(main)[2];
  out.qrLeft = vis(box) ? R(box)[0] : 1920;
  const b = $("banner");
  if (vis(b)) {
    const r = R(b), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
    out.bannerTop = !!h && (h === b || b.contains(h)) && !inter(R(b), R($("htitle")));
  }
  out.text = document.body.innerText;
  const f = document.querySelector(".card.focused .t");
  out.focus = f ? f.textContent : ($("gear").className.indexOf("focused") >= 0 ? "gear" : null);
  return out;
};
async function geom(page, name) {
  await sleep(250);
  const g = await page.evaluate(GEOM);
  g.shot = path.join(cfg.shots, name + ".png");
  await page.screenshot({ path: g.shot });
  return g;
}

// ---------------------------------------------------------------- scénarios
const S = {};
S.load = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  const first = F.log.filter((x) => x.m === "POST" && x.p.indexOf("/api/tv/canvas/") === 0)[0];
  const why = await page.evaluate(() => { const c = document.querySelector("#hchips .chip.why"); return c ? { cls: c.className } : null; });
  const r = { firstCanvasPost: first ? { p: first.p, b: first.b } : null, mode: await page.evaluate(() => document.body.getAttribute("data-cv-mode")),
              body: await bodyCls(page), external: F.external, pill: await page.evaluate(() => document.getElementById("cvpill").textContent),
              whyChip: why };
  await ctx.close();
  return r;
};

S.order = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  await sleep(300);
  const keys = [];
  let busyAfterPanel = null, busyAfterClose = null;
  for (const k of ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Enter", "ArrowRight", "Escape", "ArrowDown"]) {
    await drain(page);
    await press(page, k, 250);
    const ev = await drain(page);
    const ei = ev.findIndex((x) => x.k === "fetch" && x.p === "/api/tv/canvas/event");
    const fi = ev.findIndex((x) => x.k === "focus");
    keys.push({ key: k, event_first: ei >= 0 && (fi < 0 || ei < fi), acted: fi >= 0, kind: ei >= 0 ? ev[ei].b.kind : null,
                ei: ei, fi: fi });
    if (k === "Enter") { await until(async () => JSON.stringify(F.busy) === '["panel"]', 3000); busyAfterPanel = F.busy; }
    if (k === "Escape") { await until(async () => JSON.stringify(F.busy) === "[]", 3000); busyAfterClose = F.busy; }
  }
  // événement lent, aucun aperçu connu : la touche agit tout de suite (jamais d'interface figée)
  const before = await focusOf(page);
  F.eventDelays = [700];
  const t0 = Date.now();
  await page.keyboard.press("ArrowRight");
  await until(async () => (await focusOf(page)) !== before, 2000);
  const delayed = Date.now() - t0;
  await ctx.close();
  return { keys: keys, busy_after_panel: busyAfterPanel, busy_after_close: busyAfterClose, delayed_focus_ms: delayed };
};

S.gate = async (browser) => {
  const { ctx, page, F } = await open(browser, { state: (st) => setPreview(st, pid(1), P.cinema, { sheet: false }) });
  await waitMode(page, "run");
  await sleep(2300);
  let seen = false;
  const watch = setInterval(async () => { try { if (await sheetShown(page)) { seen = true; } } catch (e) { /* fermé */ } }, 100);
  const out = { bodyNoSheet: await bodyCls(page) };   // aperçu connu SANS feuille : scène publiée peinte
  out.before = await focusOf(page);
  F.eventDelays = [350];
  await page.keyboard.press("ArrowRight");
  await sleep(100);
  out.at100 = await focusOf(page);
  await sleep(700);
  out.after = await focusOf(page);
  // échec de l'annulation : touche consommée
  setPreview(F.state, pid(2), P.cinema, { sheet: false });
  await sleep(2400);
  F.eventFail = 1;
  out.failed_before = await focusOf(page);
  await page.keyboard.press("ArrowRight");
  await sleep(1000);
  out.failed_after = await focusOf(page);
  // annulation trop lente (> 800 ms) : touche consommée, la suivante agit normalement
  setPreview(F.state, pid(3), P.cinema, { sheet: false });
  await sleep(2400);
  F.eventDelays = [2000];
  out.slow_before = await focusOf(page);
  await page.keyboard.press("ArrowRight");
  await sleep(1300);
  out.slow_after = await focusOf(page);
  await page.keyboard.press("ArrowRight");
  await sleep(300);
  out.slow_next = await focusOf(page);
  clearInterval(watch);
  out.sheet_seen = seen;
  await ctx.close();
  return out;
};

S.stale = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  let seen = false;
  const watch = setInterval(async () => { try { if (await sheetShown(page)) { seen = true; } } catch (e) { /* fermé */ } }, 100);
  F.eventDelays = [3500];
  await page.keyboard.press("ArrowRight");
  setPreview(F.state, pid(4), P.cinema);   // créé AVANT que l'événement n'atteigne le service
  await sleep(6000);
  clearInterval(watch);
  const r = { sheet_seen: seen || await sheetShown(page), preview_was_served: F.sheetServed > 0 };
  await ctx.close();
  return r;
};

S.sheet = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  const out = { pid: pid(5) };
  out.rowsBefore = await rowTitles(page);
  await probeRows(page);
  setPreview(F.state, out.pid, P.cinema);
  out.shown = await until(() => sheetShown(page), 4000);
  out.focus = await sheetFocus(page);
  out.kicker = await page.evaluate(() => document.getElementById("cvkicker").textContent);
  out.changes = await page.evaluate(() => Array.prototype.map.call(document.querySelectorAll("#cvchanges li"), (l) => l.textContent));
  out.bodyShown = await bodyCls(page);
  out.rowsDuring = await rowTitles(page);
  out.rowsSameNodes = await probed(page);
  const g = await page.evaluate(GEOM);
  out.sheetPanelOverlapQr = g.panelQrOverlap;
  out.sheetInView = g.outside.length === 0 && g.panelHiddenBy.length === 0;
  await page.screenshot({ path: path.join(cfg.shots, "sheet.png") });
  F.state.actions.pending = [{ seq: 7, kind: "key", key: "down", age_ms: 0 }];
  const mark = F.seq;
  await sleep(2500);
  out.claimsDuringSheet = F.posts("/api/tv/action", mark).length;
  const m2 = F.seq;
  await press(page, "ArrowRight");
  out.focusAfterRight = await sheetFocus(page);
  await press(page, "ArrowLeft");
  out.focusAfterLeft = await sheetFocus(page);
  out.sheetPosts = F.posts("/api/tv/canvas/", m2).map((x) => x.p);
  const m3 = F.seq;
  await press(page, "Escape", 400);
  const c = F.posts("/api/tv/canvas/cancel", m3)[0];
  out.cancelPost = c ? c.b : null;
  out.shownAfter = await sheetShown(page);
  out.bodyAfter = await bodyCls(page);
  out.toast = await toastText(page);
  await sleep(2500);
  out.reshown = await sheetShown(page);
  out.claimsAfter = F.posts("/api/tv/action", m3).length;
  await ctx.close();
  return out;
};

S.yes = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  await press(page, "ArrowDown");
  await press(page, "ArrowRight", 300);
  const out = { pid: pid(6), focusIdBefore: await focusOf(page) };
  await sleep(1800);   // fenêtre de silence : la feuille n'apparaît qu'ensuite
  setPreview(F.state, out.pid, P.cinema);
  await until(() => sheetShown(page), 4000);
  const published = view(P.cinema, { state: "applied", revision: 1, preset: "cinema", can_rollback: true });
  F.hooks["POST /api/tv/canvas/confirm"] = async (e, f) => {
    f.state.canvas = clone(published);
    return { json: { ok: true, error: null, outcome: "applied", result: { revision: 1 }, canvas: clone(published) } };
  };
  const mark = F.seq;
  await press(page, "ArrowRight");
  await press(page, "Enter", 600);
  const c = F.posts("/api/tv/canvas/confirm", mark)[0];
  out.confirmPost = c ? c.b : null;
  out.shownAfter = await sheetShown(page);
  out.bodyAfter = await bodyCls(page);
  out.toast = await toastText(page);
  out.rowsAfter = await rowTitles(page);
  out.focusIdAfter = await focusOf(page);
  await page.screenshot({ path: path.join(cfg.shots, "cinema.png") });
  await ctx.close();
  return out;
};

S.errors = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  const out = {};
  const cases = [
    ["expired", 409, { ok: false, error: "preview_expired", outcome: "unchanged" }, null],
    ["unknown", 409, { ok: false, error: "unknown_preview", outcome: "unchanged" }, null],
    ["see_state", 503, { ok: false, error: "restore_failed", outcome: "see_state" }, P.mist],
    ["lost", 0, null, null]];
  let n = 10;
  for (const [name, status, reply, actual] of cases) {
    clearPreview(F.state);
    const id = pid(n++);
    setPreview(F.state, id, P.cinema);
    await until(() => sheetShown(page), 4000);
    F.hooks["POST /api/tv/canvas/confirm"] = async (e, f) => {
      clearPreview(f.state);
      if (!reply) { return "abort"; }
      if (actual) { f.state.canvas = view(actual, { state: "applied", revision: 1, preset: "mist", can_rollback: true }); }
      return { status: status, json: Object.assign({ result: null, canvas: clone(f.state.canvas) }, reply) };
    };
    await press(page, "ArrowRight");
    await press(page, "Enter", 500);
    out[name] = { shown: await sheetShown(page), toast: await toastText(page), body: await bodyCls(page) };
    await sleep(500);
  }
  await ctx.close();
  return out;
};

S.timeout = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  const out = { pid: pid(20) };
  setPreview(F.state, out.pid, P.cinema, { exp: 4.5 });
  F.keepPreview = true;
  out.shown = await until(() => sheetShown(page), 4000);
  const t0 = Date.now(), mark = F.seq;
  await until(async () => F.posts("/api/tv/canvas/cancel", mark).length > 0, 6000);
  out.elapsed_ms = Date.now() - t0;
  const c = F.posts("/api/tv/canvas/cancel", mark)[0];
  out.cancelPost = c ? c.b : null;
  await sleep(200);
  out.shownAfter = await sheetShown(page);
  out.toast = await toastText(page);
  await ctx.close();
  return out;
};

S.phone = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  await sleep(500);
  const out = { focusBefore: await focusOf(page) };
  const mark = F.seq;
  F.remoteQ.push({ a: "right" });
  await until(async () => (await focusOf(page)) !== out.focusBefore, 3000);
  out.focusAfter = await focusOf(page);
  out.kinds = F.posts("/api/tv/canvas/event", mark).map((x) => x.b.kind);
  await sleep(1800);
  setPreview(F.state, pid(21), P.cinema);
  await until(() => sheetShown(page), 4000);
  const m2 = F.seq;
  F.remoteQ.push({ a: "right" });
  await until(async () => (await sheetFocus(page)) === "Oui, appliquer", 3000);
  out.sheetFocus = await sheetFocus(page);
  out.sheetPosts = F.posts("/api/tv/canvas/", m2).map((x) => x.p);
  await ctx.close();
  return out;
};

S.busy = async (browser) => {
  const out = { sequence: [] };
  // la toute première liste (vide) est retardée : la page ouvre les Réglages pendant ce temps
  const { ctx, page, F } = await open(browser, { before: (f) => { f.uiDelays = [1200]; } });
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await until(async () => F.uiResponses.length >= 2, 5000);
  await sleep(300);
  out.serverBusy = F.busy;
  out.maxInflight = F.maxInflightUi;
  await press(page, "Escape", 300);
  await until(async () => JSON.stringify(F.busy) === "[]", 3000);
  await page.evaluate(() => { window.__hidden = true; document.dispatchEvent(new Event("visibilitychange")); });
  await until(async () => JSON.stringify(F.busy) === '["hidden"]', 3000);
  out.hidden = F.busy;
  await page.evaluate(() => { window.__hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
  await until(async () => JSON.stringify(F.busy) === "[]", 3000);
  F.stateFail = 3;
  await until(async () => JSON.stringify(F.busy) === '["offline"]', 9000);
  out.offline = F.busy;
  await until(async () => JSON.stringify(F.busy) === "[]", 9000);
  out.recovered = F.busy;
  F.uiFail = 1;
  await press(page, "Enter", 200);   // Réglages : premier envoi refusé, nouvel essai borné
  out.retried_after_failure = await until(async () => JSON.stringify(F.busy) === '["panel"]', 5000);
  out.maxInflight = Math.max(out.maxInflight, F.maxInflightUi);
  out.sequence = F.posts("/api/tv/canvas/ui").map((x) => x.b.busy);
  await ctx.close();
  return out;
};

S.present = async (browser) => {
  const { ctx, page, F } = await open(browser);
  await waitMode(page, "run");
  const it = F.fy.rows[2].items[3], out = {};
  const act = { seq: 3, kind: "present", id: it.id, item: it, from: "muse", text: "Une idée pour ce soir.", age_ms: 0 };
  F.hooks["POST /api/tv/action"] = async (e) => {
    if (e.b.phase === "claim" && e.b.seq === 3) { return { json: { ok: true, action: act } }; }
    if (e.b.phase === "done" && e.b.seq === 3) { out.done_at = Date.now(); out.done_body = e.b; return { json: { ok: true } }; }
    if (e.b.phase === "claim" && e.b.seq === 4) {
      await sleep(3000);   // la feuille Canvas apparaît pendant cette réclamation
      return { json: { ok: true, action: { seq: 4, kind: "key", key: "down", age_ms: 0 } } };
    }
    if (e.b.phase === "done" && e.b.seq === 4) { out.late_claim_reason = e.b.reason; return { json: { ok: true } }; }
    return { status: 409, json: { ok: false, error: "gone" } };
  };
  F.uiDelays = [700];
  F.state.actions.pending = [act];
  await until(async () => out.done_at !== undefined, 8000);
  out.panel = await page.evaluate(() => !document.getElementById("panel").hidden);
  const ui = F.uiResponses.filter((x) => JSON.stringify(x.busy) === '["panel"]')[0];
  out.ui_response_at = ui ? ui.t : null;
  out.ui_busy = ui ? ui.busy : null;
  out.max_inflight_ui = F.maxInflightUi;
  F.state.actions.pending = [];
  await press(page, "Escape", 2000);
  const m4 = F.seq;
  F.state.actions.pending = [{ seq: 4, kind: "key", key: "down", age_ms: 0 }];
  await until(async () => F.posts("/api/tv/action", m4).length > 0, 5000);   // réclamée, réponse retardée
  F.state.actions.pending = [];
  setPreview(F.state, pid(30), P.cinema);
  out.sheet_during_claim = await until(() => sheetShown(page), 3000);
  await until(async () => out.late_claim_reason !== undefined, 8000);
  await ctx.close();
  return out;
};

S.rows = async (browser) => {
  // rangée hors schéma (« because ») : jamais déplacée, seules les rangées connues prennent l'ordre de la scène
  const { ctx, page, F } = await open(browser, { fy: fyPopulated(false, true) });
  await waitMode(page, "run");
  const serverOrder = F.fy.rows.map((r) => r.title);
  await press(page, "ArrowDown");
  await press(page, "ArrowRight");
  const out = { focusBefore: await focusOf(page), orderBefore: await rowTitles(page), serverOrder: serverOrder };
  await press(page, "Enter", 300);
  await probeRows(page);
  F.state.canvas = view(P.cinema, { state: "applied", revision: 1, preset: "cinema", can_rollback: true });
  await sleep(2500);
  out.panelSameNodes = await probed(page);
  out.panelOrder = await rowTitles(page);
  await press(page, "Escape", 400);
  out.afterPanelOrder = await rowTitles(page);
  out.afterPanelFocus = await focusOf(page);
  // lecteur : « Regarder » (fiche), ordre publié changé pendant la lecture, retour sur la même carte
  out.playFocusId = await focusOf(page);
  await press(page, "Enter", 300);
  await press(page, "Enter", 400);   // « Regarder » est au focus
  await probeRows(page);
  F.play = { state: "playing", seq: 1 };
  F.state.canvas = view(P.canvas, { state: "applied", revision: 2, preset: "canvas", can_rollback: true });
  await sleep(2500);
  out.playSameNodes = await probed(page);
  out.playOrder = await rowTitles(page);
  const m0 = F.seq;
  F.play = { state: "closed", seq: 1 };
  await until(async () => F.posts("/api/tv/play/ack", m0).length > 0, 4000);   // retour acquitté, fiche rouverte
  F.play = { state: "idle", seq: 1 };
  await sleep(900);                                                           // Retour tenu après le lecteur : ignoré
  await press(page, "Escape", 400);
  out.afterPlayOrder = await rowTitles(page);
  out.afterPlayFocusId = await focusOf(page);
  await ctx.close();
  return out;
};

S.motion = async (browser) => {
  const { ctx, page, F } = await open(browser);
  const out = { combos: {} };
  await waitMode(page, "run");
  await press(page, "ArrowUp");
  await press(page, "Enter", 300);
  out.panel = await waitMode(page, "paused");
  await press(page, "Escape");
  await press(page, "ArrowDown");
  F.state.playback = { state: "playing", seq: 1, available: true };
  out.playback = await waitMode(page, "paused", 4000);
  F.state.playback = { state: "idle", seq: 1, available: true };
  await waitMode(page, "run");
  F.stateFail = 3;
  await until(() => page.evaluate(() => !document.getElementById("offline").hidden), 9000);
  out.offline = await waitMode(page, "paused", 1000);
  await waitMode(page, "run", 9000);
  await page.evaluate(() => { window.__hidden = true; document.dispatchEvent(new Event("visibilitychange")); });
  out.hidden = await waitMode(page, "paused");
  await page.evaluate(() => { window.__hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
  await waitMode(page, "run");
  F.state.setup = { active: true, url: "http://192.168.2.40:8096/setup?t=abc", expires_in: 600, phase: "waiting" };
  out.setup = await waitMode(page, "static", 4000);
  F.state.setup = { active: false };
  await waitMode(page, "run");
  for (const wp of ["aurore", "brume", "affiche"]) {
    for (const mo of ["calm", "alive"]) {
      const sc = Object.assign(clone(P.canvas), { wallpaper: wp, motion: mo });
      F.state.canvas = view(sc, { state: "applied", revision: 3, preset: null });
      await until(async () => (await bodyCls(page)).indexOf("cv-wp-" + wp) >= 0 && (await bodyCls(page)).indexOf("cv-m-" + mo) >= 0, 4000);
      out.combos[wp + "-" + mo] = await waitMode(page, "run");
    }
  }
  F.state.canvas = view(Object.assign(clone(P.canvas), { motion: "off" }), { state: "applied", revision: 4, preset: null });
  out.off = await waitMode(page, "static");
  F.state.canvas = view(Object.assign(clone(P.canvas), { motion: "alive" }), { state: "applied", revision: 5, preset: null });
  await waitMode(page, "run");
  await page.emulateMedia({ reducedMotion: "reduce" });
  out.reduced = await waitMode(page, "static", 4000);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  out.reduced_back = await waitMode(page, "run", 4000);
  out.resume = await anims(page);
  await ctx.close();
  return out;
};

S.watchdog = async (browser) => {
  const { ctx, page } = await open(browser, { clock: true });
  for (let i = 0; i < 80; i++) {
    await page.clock.runFor(250);
    if (await page.evaluate(() => document.body.getAttribute("data-cv-mode") === "run" && !!document.querySelector(".card.focused"))) { break; }
  }
  const now = await page.evaluate(() => Date.now());
  await page.clock.pauseAt(now + 1000);
  await page.clock.runFor(2000);
  const healthy = await anims(page);
  for (let i = 0; i < 70; i++) { await page.clock.fastForward(100); }
  const slow = await anims(page);
  await ctx.close();
  return { healthy: healthy, slow: slow };
};

S.shell = async (browser) => {
  const { ctx, page, F } = await open(browser, { noTransitions: true });
  await waitMode(page, "run");
  await press(page, "ArrowUp");
  await press(page, "Enter", 300);   // Réglages ouverts : feuille, boutons, QR mesurés
  const variants = [null];
  ["aurore", "brume", "affiche"].forEach((wp) => ["ice", "ember", "jade", "violet"].forEach((acc, i) => {
    variants.push(Object.assign(clone(P.canvas), { wallpaper: wp, accent: acc, shade: ["soft", "balanced", "deep"][i % 3],
                                                   hero_focus: ["title", "artwork", "calm"][i % 3], motion: ["off", "calm", "alive"][i % 3] }));
  }));
  const res = await page.evaluate((variants) => {
    const ids = ["gear", "glabel", "remotebox", "rqr", "panel", "ptitle", "pmeta", "pbtns", "cvsheet", "toast", "offline",
                 "playov", "setup", "help", "hint"];
    const props = ["background-color", "background-image", "color", "border-top-color", "border-top-width", "box-shadow",
                   "opacity", "transform", "font-size", "font-weight", "border-top-left-radius", "animation-name", "display",
                   "visibility"];
    const snap = () => {
      const o = {};
      ids.forEach((id) => {
        const e = document.getElementById(id), cs = getComputedStyle(e), r = e.getBoundingClientRect();
        o[id] = props.map((p) => cs.getPropertyValue(p)).join("|") + "|" + [r.left, r.top, r.width, r.height].join(",");
      });
      Array.prototype.forEach.call(document.querySelectorAll("#pbtns button"), (b, i) => {
        const cs = getComputedStyle(b);
        o["btn" + i] = props.map((p) => cs.getPropertyValue(p)).join("|");
      });
      return o;
    };
    const C = window.OpenTVCanvas, out = [];
    C.apply(null, { on: false });
    const base = snap();
    variants.forEach((v, n) => {
      if (v) { C.apply(v, { on: true }); C.pause([]); }
      const s = snap();
      Object.keys(base).forEach((k) => { if (s[k] !== base[k]) { out.push(n + ":" + k + " " + s[k].slice(0, 160) + " != " + base[k].slice(0, 160)); } });
    });
    return { diffs: out, variants: variants.length };
  }, variants);
  await press(page, "Escape");
  await press(page, "ArrowDown", 300);
  // QR : pixels identiques dans le temps (fond animé « alive ») et d'une scène à l'autre (intérieur, hors coins arrondis)
  F.state.canvas = view(Object.assign(clone(P.canvas), { motion: "alive" }), { state: "applied", revision: 6, preset: null });
  await until(async () => (await bodyCls(page)).indexOf("cv-m-alive") >= 0, 4000);
  const box = await page.evaluate(() => { const r = document.getElementById("rqr").getBoundingClientRect(); return { x: r.left + 16, y: r.top + 16, width: r.width - 32, height: r.height - 32 }; });
  const a = await page.screenshot({ clip: box });
  await sleep(1500);
  const b = await page.screenshot({ clip: box });
  const shots = [];
  for (const wp of ["brume", "affiche"]) {
    F.state.canvas = view(Object.assign(clone(P.canvas), { wallpaper: wp, motion: "alive" }), { state: "applied", revision: 7, preset: null });
    await until(async () => (await bodyCls(page)).indexOf("cv-wp-" + wp) >= 0, 4000);
    await sleep(300);
    shots.push(await page.screenshot({ clip: box }));
  }
  await ctx.close();
  return { diffs: res.diffs, variants: res.variants, qrStable: a.equals(b), qrSameAcrossScenes: shots.every((s) => s.equals(a)) };
};

S.geometry = async (browser) => {
  const out = {};
  let o = await open(browser, { noTransitions: true });
  await waitMode(o.page, "run");
  out.populated = await geom(o.page, "populated");
  await press(o.page, "ArrowDown");
  await press(o.page, "ArrowDown");
  await press(o.page, "ArrowRight");
  await press(o.page, "ArrowRight");
  out.populated_row2 = await geom(o.page, "populated-row2");
  await press(o.page, "Enter", 300);
  out.item_panel = await geom(o.page, "item-panel");
  await press(o.page, "Escape");
  await press(o.page, "Home");
  await press(o.page, "ArrowUp");
  await press(o.page, "Enter");
  await press(o.page, "ArrowDown");
  await press(o.page, "ArrowDown");
  await press(o.page, "Enter", 300);
  out.settings_ambiance = await geom(o.page, "settings-ambiance");
  await o.ctx.close();
  o = await open(browser, { noTransitions: true, fy: fyPopulated(true),
                            state: (st) => { st.message = { text: "Bonne soirée ! Je t'ai préparé une sélection calme pour ce soir, avec une longue phrase qui doit rester sur deux lignes au plus.", from: "muse", seconds_left: 60 }; } });
  await waitMode(o.page, "run");
  out.long_title_message = await geom(o.page, "long-title-message");
  await o.ctx.close();
  for (const [name, sc] of [["brume", P.mist], ["cinema-preset", P.cinema]]) {
    o = await open(browser, { noTransitions: true, state: (st) => { st.canvas = view(sc, { state: "applied", revision: 1 }); } });
    await waitMode(o.page, "run");
    out[name] = await geom(o.page, name);
    await o.ctx.close();
  }
  o = await open(browser, { noTransitions: true, fy: FY_SPARSE, sparse: true });
  await waitMode(o.page, "run");
  await until(() => o.page.evaluate(() => document.body.innerText.indexOf("En attendant") >= 0), 4000);
  out.sparse = await geom(o.page, "sparse");
  await o.ctx.close();
  Object.keys(out).forEach((k) => { if (k !== "sparse") { delete out[k].text; } });
  return out;
};

S.settings = async (browser) => {
  const { ctx, page, F } = await open(browser, { state: (st) => { st.canvas.can_rollback = true; } });
  await waitMode(page, "run");
  const out = {};
  const labels = () => page.evaluate(() => Array.prototype.map.call(document.querySelectorAll("#pbtns button"),
                                                                     (b) => b.getAttribute("data-label") || b.textContent));
  const reply = (scene, extra) => async (e, f) => {
    f.state.canvas = view(scene, Object.assign({ state: "applied", revision: 1, preset: null, can_rollback: true }, extra || {}));
    return { json: { ok: true, error: null, outcome: "applied", result: {}, canvas: clone(f.state.canvas) } };
  };
  const rowsBefore = await rowTitles(page);
  await press(page, "ArrowUp");
  await press(page, "Enter", 300);
  out.rows = await labels();
  await press(page, "ArrowDown");
  await press(page, "ArrowDown");
  await press(page, "Enter", 300);
  out.ambRows = await labels();
  await probeRows(page);
  // Style -> Cinéma (mouvement gardé)
  F.hooks["POST /api/tv/canvas/apply"] = reply(P.cinema);
  let mark = F.seq;
  await press(page, "Enter", 300);
  await press(page, "ArrowDown");
  await press(page, "ArrowDown");
  await press(page, "Enter", 600);
  let p = F.posts("/api/tv/canvas/apply", mark)[0];
  out.stylePost = p ? p.b : null;
  out.bodyAfterStyle = await bodyCls(page);
  await sleep(2300);
  out.rowsFrozenInSettings = (await probed(page)) && JSON.stringify(await rowTitles(page)) === JSON.stringify(rowsBefore);
  await page.screenshot({ path: path.join(cfg.shots, "settings-after-style.png") });
  // Mouvement -> Coupé
  F.hooks["POST /api/tv/canvas/apply"] = reply(Object.assign(clone(P.cinema), { motion: "off" }));
  await press(page, "ArrowDown");
  await press(page, "Enter", 300);
  await press(page, "ArrowUp");
  mark = F.seq;
  await press(page, "Enter", 600);
  p = F.posts("/api/tv/canvas/apply", mark)[0];
  out.motionPost = p ? p.b : null;
  out.bodyAfterMotion = await bodyCls(page);
  // Version précédente : « Non » au focus, rien n'est envoyé ; puis « Oui »
  F.hooks["POST /api/tv/canvas/rollback"] = reply(P.cinema);
  await press(page, "ArrowDown", 200);
  await press(page, "Enter", 300);
  out.undoFocusDefault = (await focusOf(page)).replace("btn:", "");
  mark = F.seq;
  await press(page, "Enter", 300);
  out.undoNoPosts = F.posts("/api/tv/canvas/", mark).filter((x) => x.p !== "/api/tv/canvas/event" && x.p !== "/api/tv/canvas/ui").map((x) => x.p);
  await press(page, "Enter", 300);
  await press(page, "ArrowUp");
  mark = F.seq;
  await press(page, "Enter", 600);
  p = F.posts("/api/tv/canvas/rollback", mark)[0];
  out.undoPost = p ? p.b : null;
  // Ambiance d'origine
  F.hooks["POST /api/tv/canvas/reset"] = reply(P.canvas, { state: "default", can_rollback: true });
  for (let i = 0; i < 3; i++) { await press(page, "ArrowDown"); }
  await press(page, "Enter", 300);
  await press(page, "ArrowUp");
  mark = F.seq;
  await press(page, "Enter", 600);
  p = F.posts("/api/tv/canvas/reset", mark)[0];
  out.resetPost = p ? p.b : null;
  // restore_failed : l'état réel relu (Brume) est montré, jamais « rien n'a changé »
  F.hooks["POST /api/tv/canvas/apply"] = async (e, f) => {
    f.state.canvas = view(P.mist, { state: "applied", revision: 9, preset: "mist", can_rollback: true });
    return { status: 503, json: { ok: false, error: "restore_failed", outcome: "see_state", result: null, canvas: clone(f.state.canvas) } };
  };
  await press(page, "Enter", 300);   // Style
  await press(page, "ArrowDown");    // Brume
  await press(page, "Enter", 600);
  out.seeStateToast = await toastText(page);
  out.seeStateBody = await bodyCls(page);
  // mouvement déjà actif : refus « no_change » du service, rien n'a changé
  F.hooks["POST /api/tv/canvas/apply"] = async (e, f) => ({ status: 409, json: { ok: false, error: "no_change", outcome: "unchanged", result: null, canvas: clone(f.state.canvas) } });
  await press(page, "ArrowDown");
  await press(page, "Enter", 300);
  await press(page, "Enter", 600);
  out.noChangeToast = await toastText(page);
  // touche d'assistant « OK » dans l'Ambiance : refusée (réservé au propriétaire)
  await sleep(2600);
  F.hooks["POST /api/tv/action"] = async (e) => {
    if (e.b.phase === "claim") { return { json: { ok: true, action: { seq: 11, kind: "key", key: "ok", age_ms: 0 } } }; }
    if (e.b.phase === "done") { out.agentOkInAmbiance = e.b.reason; F.state.actions.pending = []; return { json: { ok: true } }; }
    return null;
  };
  F.state.actions.pending = [{ seq: 11, kind: "key", key: "ok", age_ms: 0 }];
  await until(async () => out.agentOkInAmbiance !== undefined, 6000);
  await ctx.close();
  return out;
};

(async () => {
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  const out = {};
  try {
    const names = cfg.only ? cfg.only : Object.keys(S);
    for (const name of names) {
      try { out[name] = await S[name](browser); } catch (e) { out[name + "_error"] = String(e && e.stack || e); }
    }
  } finally {
    await browser.close();
  }
  const errs = Object.keys(out).filter((k) => /_error$/.test(k));
  if (errs.length) { process.stderr.write(errs.map((k) => k + ": " + out[k]).join("\n") + "\n"); }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
