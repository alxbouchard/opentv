/* Harnais Node (sans navigateur) de la sonde canari X : exécute le VRAI nav.js / tvkey.js / background.js de
 * l'extension dans un contexte vm minimal (DOM factice, minuteries et horloge manuelles, chrome.* espionné) et rejoue
 * un scénario de touches. Sortie : UNE ligne JSON (messages envoyés au service de l'extension / corps POSTés).
 * Usage : node x_probe_harness.js <dossier extension> <nav|tvkey|background> */
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
  const chrome = { runtime: { id: "ext", lastError: undefined, sendMessage(msg, cb) { sent.push(JSON.parse(JSON.stringify(msg))); if (cb) { cb(); } } } };
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
  return { ctx, doc, sent, listeners };
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

(async () => {
  const out = which === "nav" ? runNav() : which === "tvkey" ? runTvkey() : await runBackground();
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
