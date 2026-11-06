/* Harnais Node (sans navigateur) de la sonde canari X : exécute le VRAI nav.js / tvkey.js / background.js de
 * l'extension dans un contexte vm minimal (DOM factice, minuteries et horloge manuelles, chrome.* espionné) et rejoue
 * un scénario de touches. Sortie : UNE ligne JSON (messages envoyés au service de l'extension / corps POSTés).
 * Usage : node x_probe_harness.js <dossier extension> <nav|nav_long|nav_pagehide|tvkey|background|background_stale|background_restarts> */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [extDir, which] = process.argv.slice(2);

function makeClock() {
  const c = { t: 1000000, seq: 0, timers: [] };
  c.setTimeout = (fn, ms) => { const id = ++c.seq; c.timers.push({ id, at: c.t + Math.max(0, ms || 0), fn, every: 0 }); return id; };
  c.setInterval = (fn, ms) => { const id = ++c.seq; c.timers.push({ id, at: c.t + ms, fn, every: ms }); return id; };
  c.clearTimeout = (id) => { c.timers = c.timers.filter((x) => x.id !== id); };
  c.advance = (ms) => {
    const end = c.t + ms;
    for (;;) {
      c.timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = c.timers[0];
      if (!next || next.at > end) { break; }
      c.t = next.at;
      if (next.every) { next.at += next.every; } else { c.timers.shift(); }
      next.fn();
    }
    c.t = end;
  };
  return c;
}

function el(tag, type) {
  return { tagName: tag, isContentEditable: false, isConnected: true, getAttribute: (k) => (k === "type" ? type : null),
           setAttribute() {}, removeAttribute() {}, closest: () => null, focus() {}, scrollIntoView() {},
           style: { setProperty() {}, removeProperty() {}, getPropertyValue: () => "", getPropertyPriority: () => "" } };
}

function baseContext(clock) {
  const sent = [];
  const times = [];
  const listeners = {};
  const body = el("BODY");
  const doc = {
    readyState: "complete", body, documentElement: el("HTML"), activeElement: body, visibilityState: "visible",
    focused: true, metas: {},
    hasFocus() { return this.focused; },
    querySelector(sel) { return sel.indexOf("primaryColumn") >= 0 ? el("DIV") : (this.metas[sel] || null); },
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement: (t) => el(t.toUpperCase())
  };
  const win = {
    innerWidth: 1920, scrollY: 0,
    addEventListener(type, fn, capture) { (listeners[type] = listeners[type] || []).push({ fn, capture: !!capture }); }
  };
  const chrome = { runtime: { id: "ext", lastError: undefined, sendMessage(msg, cb) { sent.push(JSON.parse(JSON.stringify(msg))); times.push(clock.t); if (cb) { cb(); } } } };
  const ctx = {
    window: win, document: doc, chrome, console,
    location: { protocol: "https:", hostname: "x.com", pathname: "/home", href: "https://x.com/home" },
    history: { back() {} }, navigator: {},
    getComputedStyle: () => ({ visibility: "visible", display: "block", opacity: "1", position: "fixed", outlineStyle: "solid", outlineWidth: "6px" }),
    MutationObserver: class { observe() {} },
    setTimeout: clock.setTimeout, setInterval: clock.setInterval, clearTimeout: clock.clearTimeout,
    innerWidth: 1920, scrollY: 0
  };
  win.top = win;
  ctx.self = win;
  vm.createContext(ctx);
  ctx.window = ctx;   // window === globalThis du contexte (window.top === window)
  Object.assign(ctx, { addEventListener: win.addEventListener, top: ctx });
  vm.runInContext("Date.now = () => __now();", Object.assign(ctx, { __now: () => clock.t }));
  return { ctx, doc, sent, times, listeners };
}

function key(k, opts) {
  return Object.assign({ key: k, keyCode: 0, code: "", isTrusted: true, repeat: false, altKey: false, ctrlKey: false,
                         metaKey: false, isComposing: false, defaultPrevented: false,
                         preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} }, opts || {});
}

function runNav() {
  const clock = makeClock();
  const { ctx, doc, sent, listeners } = baseContext(clock);
  vm.runInContext(fs.readFileSync(path.join(extDir, "nav.js"), "utf8"), ctx);
  const press = (e) => { for (const l of listeners.keydown || []) { l.fn(e); } };
  const steps = {};
  clock.advance(1000);
  steps.load = sent.length;
  // 1) champ éditable (mot de passe) au focus : lettres, Retour arrière, lettre tenue -> rien compté ni envoyé
  doc.activeElement = el("INPUT", "password");
  for (const k of ["s", "e", "c", "r", "e", "t", "Backspace"]) { press(key(k)); clock.advance(50); }
  press(key("x", { repeat: true })); press(key("x", { repeat: true }));
  clock.advance(2000);
  steps.after_password = sent.length;
  // 2) navigation dans le champ : ↓ appui + 3 répétitions -> move 1, repeat 3, UN envoi
  press(key("ArrowDown")); press(key("ArrowDown", { repeat: true })); press(key("ArrowDown", { repeat: true }));
  press(key("ArrowDown", { repeat: true }));
  clock.advance(2000);
  steps.after_hold = sent.length;
  // 3) hors champ : OK, lettre (other), synthétique
  doc.activeElement = doc.body;
  press(key("Enter")); clock.advance(500);
  press(key("a")); clock.advance(500);
  press(key("ArrowUp", { isTrusted: false }));
  clock.advance(2000);
  // 4) page cachée, document sans focus : le battement part quand même et le dit
  doc.visibilityState = "hidden"; doc.focused = false;
  clock.advance(30000);
  return { sent, steps };
}

// document x.com ouvert TRÈS longtemps : bien plus d'envois que l'ancien plafond par document (3000), puis le
// battement doit continuer ; jamais deux envois à moins de PROBE_MIN_MS
function runNavLong() {
  const clock = makeClock();
  const { ctx, sent, times, listeners } = baseContext(clock);
  vm.runInContext(fs.readFileSync(path.join(extDir, "nav.js"), "utf8"), ctx);
  const press = (e) => { for (const l of listeners.keydown || []) { l.fn(e); } };
  clock.advance(1000);
  for (let i = 0; i < 4000; i++) { press(key(i % 2 ? "ArrowDown" : "Enter")); clock.advance(450); }
  const after_keys = sent.length;
  clock.advance(12 * 3600 * 1000);   // 12 h sans touche : battements toutes les 30 s
  let min_gap = Infinity;
  for (let i = 1; i < times.length; i++) { min_gap = Math.min(min_gap, times[i] - times[i - 1]); }
  const beats = sent.slice(after_keys).filter((m) => m.event === "beat").length;
  return { total: sent.length, after_keys, beats, min_gap, last: sent[sent.length - 1] };
}

// pagehide : l'instantané EN ATTENTE part tout de suite ; rien en attente : aucun envoi ; jamais de donnée nouvelle
function runNavPagehide() {
  const clock = makeClock();
  const { ctx, sent, times, listeners } = baseContext(clock);
  vm.runInContext(fs.readFileSync(path.join(extDir, "nav.js"), "utf8"), ctx);
  const press = (e) => { for (const l of listeners.keydown || []) { l.fn(e); } };
  const hide = () => { for (const l of listeners.pagehide || []) { l.fn({ persisted: false }); } };
  clock.advance(1000);
  const steps = { load: sent.length };
  hide();                                     // rien en attente
  steps.idle_hide = sent.length;
  press(key("ArrowDown")); clock.advance(1);  // envoi de la touche programmé (≥ 400 ms après le précédent)
  press(key("Enter")); clock.advance(1);
  steps.before_hide = sent.length;
  const t0 = clock.t;
  hide();
  steps.after_hide = sent.length;
  steps.hide_at = times[times.length - 1] - t0;
  clock.advance(5000);                        // plus rien d'autre (minuterie annulée, pas de doublon)
  steps.end = sent.length;
  hide();
  steps.second_hide = sent.length;
  // page restaurée (bfcache) : appui juste après un envoi au pagehide ; la minuterie annulée ne doit jamais
  // vider ce NOUVEL instantané avant PROBE_MIN_MS
  press(key("ArrowUp")); clock.advance(1);
  press(key("ArrowUp")); clock.advance(1);    // envoi programmé (≥ 400 ms après le précédent)
  const t1 = clock.t;
  hide();
  press(key("ArrowDown")); clock.advance(1);  // restaurée : nouvel instantané en attente
  clock.advance(2000);
  steps.restored_gap = times[times.length - 1] - t1;
  steps.restored_sends = sent.length;
  return { sent, steps, listeners: Object.keys(listeners).sort() };
}

function runTvkey() {
  const clock = makeClock();
  const { ctx, doc, sent } = baseContext(clock);
  ctx.location = { protocol: "http:", hostname: "127.0.0.1", pathname: "/tv", href: "http://127.0.0.1:8080/tv" };
  doc.metas['meta[name="opentv-key"]'] = { content: "k".repeat(43) };
  vm.runInContext(fs.readFileSync(path.join(extDir, "tvkey.js"), "utf8"), ctx);
  clock.advance(30000 * 3 + 10);
  doc.visibilityState = "hidden"; doc.focused = false;
  clock.advance(30000);
  return { sent };
}

async function runBackground() {
  const clock = makeClock();
  const posts = [];
  let onMessage = null;
  const store = {};
  const ctx = {
    console, URL, AbortController, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, Promise,
    chrome: {
      runtime: { id: "ext", getManifest: () => ({ version: "0.7.8" }), onMessage: { addListener(f) { onMessage = f; } } },
      storage: { session: {
        get: async (keys) => { const o = {}; for (const k of keys) { if (k in store) { o[k] = store[k]; } } return o; },
        set: async (o) => { Object.assign(store, o); }, remove: async (ks) => { for (const k of ks) { delete store[k]; } } } },
      tabs: { update: async () => undefined }
    },
    fetch: async (url, init) => { posts.push({ url, body: JSON.parse(init.body), headers: init.headers }); return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
  };
  vm.createContext(ctx);
  vm.runInContext("Date.now = () => __now();", Object.assign(ctx, { __now: () => clock.t }));
  vm.runInContext(fs.readFileSync(path.join(extDir, "background.js"), "utf8"), ctx);
  const tv = { id: "ext", frameId: 0, url: "http://127.0.0.1:8080/tv" };
  const x = { id: "ext", frameId: 0, url: "https://x.com/home" };
  const flush = () => new Promise((r) => setImmediate(r));
  const settle = async () => { for (let i = 0; i < 20; i++) { await flush(); } };
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});   // avant la clé : rien
  await settle();
  const before_key = posts.length;
  onMessage({ type: "tvkey", key: "k".repeat(43), doc_focus: true, visible: true }, tv, () => {});
  await settle();
  onMessage({ type: "tvbeat", doc_focus: false, visible: false }, tv, () => {});
  await settle();
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, x, () => {});      // « tvbeat » depuis x.com : refusé
  await settle();
  onMessage({ type: "probe", mode: "feed", event: "key", seq: 1, move: 2, ok: 1, other: 0, repeat: 5, synthetic: 0,
              doc_focus: true, visible: true, key: "ArrowUp", url: "https://x.com/secret",
              last: { cls: "move", trusted: true, editable: false, doc_focus: true, handled: true, code: 38 } }, x, () => {});
  await settle();
  onMessage({ type: "probe", mode: "feed", event: "beat", seq: 2, move: 2, ok: 1, other: 0, repeat: 5, synthetic: 0,
              doc_focus: "yes", visible: 1, last: null }, tv, () => {});   // « probe » depuis /tv : refusé
  await settle();
  onMessage({ type: "probe", mode: "feed", event: "beat", seq: 3, move: 2, ok: 1, other: 0, repeat: 5, synthetic: 0,
              doc_focus: "yes", visible: 1, last: null }, x, () => {});    // non booléens : jamais « vrai »
  await settle();
  onMessage({ type: "tvbeat", doc_focus: "true", visible: [true] }, tv, () => {});
  await settle();
  return { before_key, posts };
}

// Service redémarré pendant que X est ouvert : premier 403 « tv_key » -> clé retirée, PLUS AUCUN envoi (instantané
// ni battement) ; l'ancienne clé n'est jamais reprise ; une clé NEUVE de /tv rouvre la sonde. Course : 403 tardif de
// l'ancienne clé pendant qu'une clé neuve arrive : la neuve reste.
async function runBackgroundStale() {
  const clock = makeClock();
  const posts = [];
  const server = { key: "a".repeat(43), defer: false };
  const held = [];
  let onMessage = null;
  const store = {};
  const later = (v) => new Promise((r) => setImmediate(() => r(v)));
  const reply = (key) => (key === server.key ?
    { ok: true, status: 200, json: async () => ({ ok: true }) } :
    { ok: false, status: 403, json: async () => ({ ok: false, error: "tv_key" }) });
  const ctx = {
    console, URL, AbortController, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, Promise,
    chrome: {
      runtime: { id: "ext", getManifest: () => ({ version: "0.7.8" }), onMessage: { addListener(f) { onMessage = f; } } },
      storage: { session: {
        get: (keys) => { const o = {}; for (const k of keys) { if (k in store) { o[k] = store[k]; } } return later(o); },
        set: (o) => { Object.assign(store, o); return later(undefined); },
        remove: (ks) => { for (const k of ks) { delete store[k]; } return later(undefined); } } },
      tabs: { update: async () => undefined }
    },
    fetch: (url, init) => {
      const key = init.headers["X-OpenTV-TV"];
      posts.push({ path: new URL(url).pathname, key, body: JSON.parse(init.body) });
      if (server.defer) { return new Promise((r) => held.push(() => r(reply(key)))); }
      return Promise.resolve(reply(key));
    }
  };
  vm.createContext(ctx);
  vm.runInContext("Date.now = () => __now();", Object.assign(ctx, { __now: () => clock.t }));
  vm.runInContext(fs.readFileSync(path.join(extDir, "background.js"), "utf8"), ctx);
  const tv = { id: "ext", frameId: 0, url: "http://127.0.0.1:8080/tv" };
  const x = { id: "ext", frameId: 0, url: "https://x.com/home" };
  const flush = () => new Promise((r) => setImmediate(r));
  const settle = async () => { for (let i = 0; i < 80; i++) { await flush(); } };
  const xprobe = (seq) => ({ type: "probe", mode: "feed", event: "key", seq, move: seq, ok: 0, other: 0, repeat: 0,
                             synthetic: 0, doc_focus: true, visible: true, last: null });
  const marks = {};
  const K1 = "a".repeat(43), K2 = "b".repeat(43), K3 = "c".repeat(43);
  onMessage({ type: "tvkey", key: K1, doc_focus: true, visible: true }, tv, () => {});
  await settle();
  onMessage(xprobe(1), x, () => {});
  await settle();
  marks.fresh = posts.length;                         // 2 envois acceptés avec K1
  server.key = K2;                                    // redémarrage du service : K1 morte
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
  await settle();
  marks.first_403 = posts.length;                     // UN envoi refusé (403)
  marks.key_after_403 = store.tvkey === undefined ? null : store.tvkey;
  for (let i = 0; i < 20; i++) {                      // touches, battements x.com et /tv : plus rien ne part
    clock.advance(500);
    onMessage(xprobe(10 + i), x, () => {});
    onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
    await settle();
  }
  marks.after_stale = posts.length;
  let shareMsg = null;
  onMessage({ type: "share", author: "a", text: "t" }, x, (r) => { shareMsg = r; });
  await settle();
  marks.share = shareMsg;
  marks.after_share = posts.length;                   // partage : aucun appel réseau sans clé
  onMessage({ type: "tvkey", key: K1, doc_focus: true, visible: true }, tv, () => {});   // ancienne page /tv
  await settle();
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
  await settle();
  marks.dead_key_refused = posts.length;
  marks.key_after_dead = store.tvkey === undefined ? null : store.tvkey;
  onMessage({ type: "tvkey", key: K2, doc_focus: true, visible: true }, tv, () => {});   // /tv de CE démarrage
  await settle();
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
  await settle();
  marks.reopened = posts.length;
  // course : redémarrage (K3) ; battement envoyé avec K2 et retenu ; K3 capturée ; puis 403 tardif de K2
  server.key = K3;
  server.defer = true;
  clock.advance(1000);
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
  await settle();
  server.defer = false;
  marks.held = held.length;
  // MÊME tour : clé neuve K3 remise par /tv ET 403 tardif de K2 (écritures de clé concurrentes)
  onMessage({ type: "tvkey", key: K3, doc_focus: true, visible: true }, tv, () => {});
  while (held.length) { held.shift()(); }
  await settle();
  marks.key_after_race = store.tvkey === undefined ? null : store.tvkey;
  clock.advance(1000);
  onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {});
  await settle();
  marks.end = posts.length;
  return { marks, posts, K1, K2, K3 };
}

// Trois redémarrages (K1 -> K2 -> K3 -> K4), le dernier en course (403 tardif de K3 ET clé neuve K4 au même tour) :
// chaque ancienne page /tv (K1, K2, K3) est refusée SANS aucun envoi ; K4 reste et envoie. Puis 10 redémarrages de
// plus : la liste des clés mortes reste bornée (DEAD_MAX), les plus récentes retenues.
async function runBackgroundRestarts() {
  const clock = makeClock();
  const posts = [];
  const K = "abcdefghijklmnopqrstuvwxyz".split("").map((c) => c.repeat(43));
  const server = { key: K[0], defer: false };
  const held = [];
  let onMessage = null;
  const store = {};
  const logs = [];
  const later = (v) => new Promise((r) => setImmediate(() => r(v)));
  const reply = (key) => (key === server.key ?
    { ok: true, status: 200, json: async () => ({ ok: true }) } :
    { ok: false, status: 403, json: async () => ({ ok: false, error: "tv_key" }) });
  const spy = (...a) => { logs.push(a.map(String).join(" ")); };
  const ctx = {
    console: { log: spy, info: spy, warn: spy, error: spy, debug: spy }, URL, AbortController,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, Promise,
    chrome: {
      runtime: { id: "ext", getManifest: () => ({ version: "0.7.8" }), onMessage: { addListener(f) { onMessage = f; } } },
      storage: { session: {
        get: (keys) => { const o = {}; for (const k of keys) { if (k in store) { o[k] = JSON.parse(JSON.stringify(store[k])); } } return later(o); },
        set: (o) => { Object.assign(store, JSON.parse(JSON.stringify(o))); return later(undefined); },
        remove: (ks) => { for (const k of ks) { delete store[k]; } return later(undefined); } } },
      tabs: { update: async () => undefined }
    },
    fetch: (url, init) => {
      const key = init.headers["X-OpenTV-TV"];
      posts.push({ path: new URL(url).pathname, key, body: JSON.parse(init.body) });
      if (server.defer) { return new Promise((r) => held.push(() => r(reply(key)))); }
      return Promise.resolve(reply(key));
    }
  };
  vm.createContext(ctx);
  vm.runInContext("Date.now = () => __now();", Object.assign(ctx, { __now: () => clock.t }));
  vm.runInContext(fs.readFileSync(path.join(extDir, "background.js"), "utf8"), ctx);
  const tv = { id: "ext", frameId: 0, url: "http://127.0.0.1:8080/tv" };
  const flush = () => new Promise((r) => setImmediate(r));
  const settle = async () => { for (let i = 0; i < 80; i++) { await flush(); } };
  const tvkey = async (k) => { onMessage({ type: "tvkey", key: k, doc_focus: true, visible: true }, tv, () => {}); await settle(); };
  const beat = async () => { clock.advance(1000); onMessage({ type: "tvbeat", doc_focus: true, visible: true }, tv, () => {}); await settle(); };
  const cur = () => (store.tvkey === undefined ? null : store.tvkey);
  const marks = { cycles: [] };
  await tvkey(K[0]);
  marks.start = posts.length;                               // K1 : instantané « load » accepté
  for (let i = 1; i <= 2; i++) {                            // redémarrages 1 et 2 : 403 puis clé neuve
    server.key = K[i];
    await beat();
    const died = posts.length;
    await tvkey(K[i]);
    marks.cycles.push({ died, key_after: cur(), fresh: posts.length });
  }
  // redémarrage 3 en course : battement K3 retenu, clé neuve K4 et 403 tardif de K3 au MÊME tour
  server.key = K[3];
  server.defer = true;
  await beat();
  server.defer = false;
  marks.held = held.length;
  onMessage({ type: "tvkey", key: K[3], doc_focus: true, visible: true }, tv, () => {});
  while (held.length) { held.shift()(); }
  await settle();
  marks.key_after_race = cur();
  marks.before_old_pages = posts.length;
  marks.old_pages = [];
  for (const old of [K[0], K[1], K[2], K[0], K[2]]) {       // anciennes pages /tv, dans le désordre : toutes refusées
    await tvkey(old);
    marks.old_pages.push({ key_after: cur(), posts: posts.length });
  }
  await beat();
  marks.after_beat = posts.length;
  marks.dead_after_3 = (store.tvdead || []).slice();
  // 10 redémarrages de plus : liste bornée
  for (let i = 4; i < 14; i++) {
    server.key = K[i];
    await beat();
    await tvkey(K[i]);
  }
  marks.dead_after_13 = (store.tvdead || []).slice();
  marks.key_final = cur();
  marks.end = posts.length;
  return { marks, posts, K: K.slice(0, 14), logs };
}

(async () => {
  const runs = { nav: runNav, nav_long: runNavLong, nav_pagehide: runNavPagehide, tvkey: runTvkey,
                 background: runBackground, background_stale: runBackgroundStale,
                 background_restarts: runBackgroundRestarts };
  const out = await runs[which]();
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
