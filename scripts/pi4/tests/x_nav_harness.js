/* Harnais Node (sans navigateur) du mode « contrôles » de connexion X : exécute le VRAI nav.js dans un contexte vm
 * (DOM factice minimal : arbre, sélecteurs simples de nav.js, visibilité par display ; horloge, minuteries,
 * performance.now ET Date.now manuels et INDÉPENDANTS) et rejoue des scénarios DÉTERMINISTES de réarmement et de
 * focus : expiration sans mutation, recalage d'horloge murale, clic au pointeur, annulation par pointerdown, passkey
 * masquée préexistante, meilleure passkey sûre, inscription focalisée par X. Aucun réseau.
 * Usage : node x_nav_harness.js <dossier extension>   Sortie : UNE ligne JSON { <scénario>: observations }. */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const extDir = process.argv[2];
const NAV = fs.readFileSync(path.join(extDir, "nav.js"), "utf8");
const ONB = "/i/jf/onboarding/web";

function parseSel(sel) {
  return sel.split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
    const m = /^([a-z]+)?((?:\[[^\]]+\]|:not\(\[[^\]]+\]\))*)$/i.exec(s);
    if (!m) { throw new Error("sélecteur non géré : " + s); }
    const parts = [];
    const re = /(:not\()?\[([a-z-]+)(?:=["']?([^"'\]]*)["']?)?\]\)?/gi;
    let p;
    while ((p = re.exec(m[2]))) { parts.push({ not: !!p[1], name: p[2], value: p[3] }); }
    return { tag: m[1] ? m[1].toUpperCase() : null, parts };
  });
}

function makeEnv() {
  const clock = { t: 1000000, seq: 0, timers: [], wall: 0 };
  clock.setTimeout = (fn, ms) => { const id = ++clock.seq; clock.timers.push({ id, at: clock.t + Math.max(0, ms || 0), fn, every: 0 }); return id; };
  clock.setInterval = (fn, ms) => { const id = ++clock.seq; clock.timers.push({ id, at: clock.t + ms, fn, every: ms }); return id; };
  clock.clearTimeout = (id) => { clock.timers = clock.timers.filter((x) => x.id !== id); };
  clock.advance = (ms) => {
    const end = clock.t + ms;
    for (;;) {
      clock.timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = clock.timers[0];
      if (!next || next.at > end) { break; }
      clock.t = next.at;
      if (next.every) { next.at += next.every; } else { clock.timers.shift(); }
      next.fn();
    }
    clock.t = end;
  };
  const listeners = {};
  const observers = [];
  const fire = (type, ev) => { for (const l of (listeners[type] || []).slice()) { l.fn(ev); } };

  class FakeEl {
    constructor(doc, tag, attrs, text) {
      this.ownerDocument = doc;
      this.tagName = tag.toUpperCase();
      this.attrs = new Map(Object.entries(attrs || {}));
      this.text = text || "";
      this.children = [];
      this.parentNode = null;
      this.display = "block";
      this.isContentEditable = false;
      this.focusCount = 0;
      this.clicks = 0;
      this.className = "";
      const st = new Map();
      this.style = { setProperty: (k, v) => st.set(k, v), removeProperty: (k) => st.delete(k),
                     getPropertyValue: (k) => st.get(k) || "", getPropertyPriority: () => "" };
    }
    getAttribute(k) { return this.attrs.has(k) ? String(this.attrs.get(k)) : null; }
    setAttribute(k, v) { this.attrs.set(k, String(v)); }
    removeAttribute(k) { this.attrs.delete(k); }
    get id() { return this.getAttribute("id") || ""; }
    get disabled() { return this.attrs.has("disabled"); }
    get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
    set textContent(v) { this.text = String(v); this.children = []; }
    get isConnected() { let n = this; while (n.parentNode) { n = n.parentNode; } return n === this.ownerDocument.documentElement; }
    appendChild(c) { c.remove(); c.parentNode = this; this.children.push(c); return c; }
    remove() {
      if (!this.parentNode) { return; }
      this.parentNode.children = this.parentNode.children.filter((x) => x !== this);
      this.parentNode = null;
    }
    contains(o) { for (let n = o; n; n = n.parentNode) { if (n === this) { return true; } } return false; }
    matches(sel) {
      return parseSel(sel).some((s) => (!s.tag || s.tag === this.tagName) && s.parts.every((p) => {
        const v = this.getAttribute(p.name);
        const ok = p.value === undefined ? v !== null : v === p.value;
        return p.not ? !ok : ok;
      }));
    }
    closest(sel) { for (let n = this; n instanceof FakeEl; n = n.parentNode) { if (n.matches(sel)) { return n; } } return null; }
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => { for (const c of n.children) { if (c.matches(sel)) { out.push(c); } walk(c); } };
      walk(this);
      return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    shown() { for (let n = this; n instanceof FakeEl; n = n.parentNode) { if (n.display === "none") { return false; } } return true; }
    getBoundingClientRect() {
      return this.shown() ? { left: 800, top: 100, right: 1100, bottom: 140, width: 300, height: 40 }
        : { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    }
    checkVisibility() { return this.shown(); }
    focus() {
      if (!this.isConnected) { return; }
      this.ownerDocument.activeElement = this;
      this.focusCount++;
      fire("focusin", { target: this, isTrusted: false });
    }
    blur() { const d = this.ownerDocument; if (d.activeElement === this) { d.activeElement = d.body; d.blurs.push(this.id); } }
    click() { this.clicks++; }
    scrollIntoView() {}
  }
  const doc = {
    readyState: "complete", visibilityState: "visible", blurs: [], activeElement: null,
    hasFocus: () => true, addEventListener() {},
    createElement: (t) => new FakeEl(doc, t),
    querySelectorAll: (s) => doc.documentElement.querySelectorAll(s),
    querySelector: (s) => doc.documentElement.querySelector(s)
  };
  doc.documentElement = new FakeEl(doc, "HTML");
  doc.body = doc.documentElement.appendChild(new FakeEl(doc, "BODY"));
  doc.activeElement = doc.body;
  const sent = [];
  const ctx = {
    document: doc, console, URL, Element: FakeEl,
    chrome: { runtime: { id: "ext", lastError: undefined, sendMessage(m, cb) { sent.push(m.type); if (cb) { cb(); } } } },
    location: { protocol: "https:", hostname: "x.com", pathname: "/", href: "https://x.com/" },
    history: { back() {} }, navigator: {},
    getComputedStyle: (el) => ({ visibility: "visible", display: el.shown && !el.shown() ? "none" : "block", opacity: "1",
                                 position: "fixed", outlineStyle: "solid", outlineWidth: "6px" }),
    MutationObserver: class { constructor(cb) { observers.push(cb); } observe() {} },
    setTimeout: clock.setTimeout, setInterval: clock.setInterval, clearTimeout: clock.clearTimeout,
    performance: { now: () => clock.t - 1000000 },   // monotone : seule l'horloge manuelle l'avance
    innerWidth: 1920, innerHeight: 1080, scrollY: 0,
    addEventListener(type, fn, capture) { (listeners[type] = listeners[type] || []).push({ fn, capture: !!capture }); }
  };
  vm.createContext(ctx);
  ctx.window = ctx;
  ctx.top = ctx;
  ctx.self = ctx;
  // horloge MURALE indépendante (recalage NTP simulé par clock.wall) : jamais celle du réarmement
  vm.runInContext("Date.now = () => __wall();", Object.assign(ctx, { __wall: () => clock.t + clock.wall }));
  const add = (parent, tag, attrs, text) => (parent || doc.body).appendChild(new FakeEl(doc, tag, attrs, text));
  const env = {
    clock, doc, add, fire, sent,
    load() { vm.runInContext(NAV, ctx); clock.advance(1000); },
    mutate() { for (const cb of observers) { cb([]); } },
    settle(ms) { env.mutate(); clock.advance(ms || 400); },
    key(k, target) {
      const e = { key: k, keyCode: 0, code: "", isTrusted: true, repeat: false, altKey: false, ctrlKey: false,
                  metaKey: false, isComposing: false, defaultPrevented: false, target: target || doc.activeElement,
                  preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} };
      fire("keydown", e);
    },
    pointerdown(el) { fire("pointerdown", { isTrusted: true, button: 0, target: el }); },
    click(el, trusted) { fire("click", { isTrusted: trusted !== false, button: 0, target: el }); },
    okClick(el) { env.key("Enter", el); env.click(el); },   // OK réel sur un lien natif : clic de confiance de Chromium
    active() { const a = doc.activeElement; return a && a !== doc.body ? a.id || a.tagName : null; },
    pending() { return clock.timers.filter((x) => !x.every).map((x) => ({ id: x.id, in: x.at - clock.t })); }
  };
  return env;
}

// accueil déconnecté de x.com (mêmes href/libellés que le SSR réel) ; X autofocuse le champ (ou l'inscription)
function landing(opts) {
  const o = opts || {};
  const env = makeEnv();
  const els = {};
  els.phone = env.add(null, "a", { id: "phone", href: ONB + "?mode=signup&redirect_after_login=%2F" }, "Continue with phone");
  els.google = env.add(null, "a", { id: "google", href: ONB + "?mode=login&redirect_after_login=%2F" }, "Continue with Google");
  els.ident = env.add(null, "input", { id: "ident", type: "text" });
  if (o.hidpk) {   // passkey DÉJÀ dans le document au clic, masquée (display:none)
    els.hidpk = env.add(null, "button", { id: "hidpk", type: "button" }, "Continue with passkey");
    els.hidpk.display = "none";
  }
  if (!o.nosignin) { els.signin = env.add(null, "a", { id: "signin", href: ONB + "?mode=login&redirect_after_login=%2F" }, "Sign in"); }
  env.doc.activeElement = o.focus === "phone" ? els.phone : els.ident;
  env.load();
  return { env, els };
}
function openWindow(env, labels) {   // fenêtre de connexion de X ajoutée DANS la page (nouveaux contrôles)
  const dlg = env.add(null, "div", { id: "logindlg", role: "dialog", "aria-modal": "true" });
  const out = {};
  for (const [id, text] of labels) { out[id] = env.add(dlg, "button", { id: id, type: "button" }, text); }
  return out;
}
const counts = (els) => Object.fromEntries(Object.entries(els).map(([k, e]) => [k, e.focusCount]));
const clicks = (els) => Object.values(els).reduce((n, e) => n + e.clicks, 0);
const R = {};

// 1) meilleure passkey sûre : leurres négatifs (sans / au lieu de / autre / différente, EN + FR) et passkey libre AVANT
//    la passkey canonique dans le document ; passkey préexistante masquée puis révélée : jamais « nouvelle »
{
  const { env, els } = landing({ hidpk: true });
  const initial = env.active();
  env.okClick(els.signin);
  env.clock.advance(100);
  els.hidpk.display = "block";
  const w = openWindow(env, [
    ["neg1", "Continue without passkey"], ["neg2", "Sign in without a passkey"], ["neg3", "Use password instead of passkey"],
    ["neg4", "Continue sans clé d'accès"], ["neg5", "Utiliser le mot de passe au lieu de la clé d'accès"],
    ["neg6", "Use a different passkey"], ["neg7", "Use another passkey"], ["neg8", "Utiliser une autre clé d'accès"],
    ["loose", "Use passkey on this device"], ["canon", "Continue with passkey"]]);
  env.settle();
  R.best = { initial, active: env.active(), focus: counts(Object.assign({ hidpk: els.hidpk }, w)),
             clicks: clicks(Object.assign({}, els, w)) };
}
// 1 bis) SEULE nouveauté : la passkey préexistante masquée devient visible -> rien n'est focalisé
{
  const { env, els } = landing({ hidpk: true });
  env.okClick(els.signin);
  els.hidpk.display = "block";
  env.settle();
  R.hidden_only = { active: env.active(), hidpk: els.hidpk.focusCount };
}
// 1 ter) seulement des leurres négatifs dans la fenêtre : rien n'est focalisé
{
  const { env, els } = landing();
  env.okClick(els.signin);
  const w = openWindow(env, [["neg1", "Continue without passkey"], ["neg6", "Use a different passkey"],
                             ["neg8", "Utiliser une autre clé d'accès"]]);
  env.settle();
  R.negatives_only = { active: env.active(), focus: counts(w) };
}
// 2) clic au POINTEUR (pointerdown puis click de confiance, même geste) : arme ; passkey nouvelle focalisée une fois
{
  const { env, els } = landing();
  env.pointerdown(els.signin);
  env.click(els.signin);
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle();
  env.settle();
  R.pointer = { active: env.active(), focus: w.passkey.focusCount, clicks: clicks(Object.assign({}, els, w)) };
}
// 3) pointerdown réel APRÈS le clic d'armement : annulé, la passkey n'est jamais focalisée
{
  const { env, els } = landing();
  env.okClick(els.signin);
  env.clock.advance(100);
  env.pointerdown(env.doc.body);
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle();
  R.pointer_cancel = { active: env.active(), focus: w.passkey.focusCount, timers: env.pending().filter((x) => x.in > 10000) };
}
// 3 bis) clic synthétique (script de page) : jamais armé
{
  const { env, els } = landing();
  env.click(els.signin, false);
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle();
  R.synthetic_click = { active: env.active(), focus: w.passkey.focusCount };
}
// 4) expiration SANS aucune mutation (minuterie explicite) ; horloge murale reculée d'une heure (recalage NTP) :
//    l'échéance reste 15 s monotones
{
  const { env, els } = landing();
  env.okClick(els.signin);
  const armTimers = env.pending().filter((x) => x.in === 15000);
  env.clock.wall -= 3600 * 1000;
  env.clock.advance(15001);
  const left = env.pending().filter((x) => armTimers.some((a) => a.id === x.id));
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle();
  R.expiry = { arm_timers: armTimers.length, left: left.length, active: env.active(), focus: w.passkey.focusCount };
}
// 4 bis) juste AVANT l'échéance (14,7 s sans mutation) : encore armé
{
  const { env, els } = landing();
  env.okClick(els.signin);
  env.clock.advance(14700);
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle(250);
  R.before_expiry = { active: env.active(), focus: w.passkey.focusCount };
}
// 4 ter) horloge murale AVANCÉE d'une heure juste après le clic : le réarmement n'expire pas pour autant
{
  const { env, els } = landing();
  env.okClick(els.signin);
  env.clock.wall += 3600 * 1000;
  env.clock.advance(300);
  const w = openWindow(env, [["passkey", "Continue with passkey"]]);
  env.settle();
  R.wall_jump = { active: env.active(), focus: w.passkey.focusCount };
}
// 5) X focalise l'INSCRIPTION (« Continue with phone ») : vers « Sign in » si présent ; sinon retirée (blur) ; aussi
//    après une touche réelle ; jamais un clic
{
  const { env, els } = landing({ focus: "phone" });
  const initial = env.active();
  env.key("ArrowDown");
  const afterDown = env.active();
  els.phone.focus();
  env.clock.advance(400);
  R.signup_focus_pref = { initial, after_down: afterDown, refocused: env.active(), clicks: clicks(els) };
}
{
  const { env, els } = landing({ nosignin: true, focus: "phone" });
  const initial = env.active();
  env.key("Enter");
  const afterOk = { active: env.active(), clicks: clicks(els) };
  env.key("ArrowDown");
  const afterDown = env.active();
  els.phone.focus();
  env.clock.advance(400);
  R.signup_focus_none = { initial, after_ok: afterOk, after_down: afterDown, refocused: env.active(),
                          blurs: env.doc.blurs.slice(), ring: els.phone.getAttribute("data-otvx-focus"), clicks: clicks(els) };
}
// 6) vérification légitime : « Use your phone » / « Use phone to verify » atteignables à la main ; l'inscription exacte
//    connue (« Continue with phone ») et le href d'inscription restent retirés
{
  const env = makeEnv();
  const dlg = env.add(null, "div", { role: "dialog", "aria-modal": "true" });
  const els = {
    usephone: env.add(dlg, "button", { id: "usephone", type: "button" }, "Use your phone"),
    phoneverify: env.add(dlg, "button", { id: "phoneverify", type: "button" }, "Use phone to verify"),
    vfr: env.add(dlg, "button", { id: "vfr", type: "button" }, "Utiliser votre téléphone pour vérifier"),
    phonesignup: env.add(dlg, "button", { id: "phonesignup", type: "button" }, "Continue with phone"),
    signuplink: env.add(dlg, "a", { id: "signuplink", href: ONB + "?mode=signup" }, "Get started")
  };
  env.load();
  const seq = [env.active()];
  for (let i = 0; i < 4; i++) { env.key("ArrowDown"); seq.push(env.active()); }
  R.verify = { seq, clicks: clicks(els) };
}
// 7) X focalise un CONTENEUR (fenêtre role=dialog tabindex=-1, enveloppe tabindex=-1) dont le texte mentionne
//    « Create account » / « Sign up » : jamais classé inscription par son texte — focus conservé, aucun blur, aucune
//    oscillation, avant ET après une entrée réelle ; le VRAI lien d'inscription focalisé par X reste redirigé
{
  const env = makeEnv();
  const dlg = env.add(null, "div", { id: "dlg", role: "dialog", "aria-modal": "true", tabindex: "-1" },
    "Create account or Sign up. ");
  const els = {
    signin: env.add(dlg, "a", { id: "signin", href: ONB + "?mode=login&redirect_after_login=%2F" }, "Sign in"),
    google: env.add(dlg, "a", { id: "google", href: ONB + "?mode=login&redirect_after_login=%2F" }, "Continue with Google"),
    signup: env.add(dlg, "a", { id: "dlgsignup", href: ONB + "?mode=signup&redirect_after_login=%2F" }, "Sign up")
  };
  const wrap = env.add(dlg, "div", { id: "wrap", tabindex: "-1" }, "Don't have an account? Sign up to create account");
  env.doc.activeElement = dlg;   // X focalise sa fenêtre (conteneur) avant le chargement de nav.js
  env.load();
  const initial = env.active();
  const seq = [];
  for (let i = 0; i < 3; i++) { dlg.focus(); env.clock.advance(400); seq.push(env.active()); }
  const before = { signin: els.signin.focusCount, blurs: env.doc.blurs.slice() };
  env.key("ArrowDown");   // entrée réelle
  const afterKey = env.active();
  const user = [];
  for (const el of [dlg, wrap, dlg]) { el.focus(); env.clock.advance(400); user.push(env.active()); }
  const afterUser = { signin: els.signin.focusCount, blurs: env.doc.blurs.slice() };
  els.signup.focus();   // VRAI contrôle d'inscription focalisé par X : redirigé vers « Sign in »
  env.clock.advance(400);
  R.container_focus = { initial, seq, before, after_key: afterKey, user, after_user: afterUser,
                        signup_redirect: env.active(), clicks: clicks(els) };
}
process.stdout.write(JSON.stringify(R) + "\n");
