/* OpenTV X TV Mode — Retour / Accueil sur TOUTE page x.com (cadre principal), connexion et vérifications comprises,
 * et navigation à la télécommande des pages SANS fil (connexion, accueil déconnecté, vérifications).
 *
 * Script autonome, injecté dès document_start : il ne dépend ni du fil (x.js), ni du chargement complet de la page
 * (une page de connexion blanche ou lente ramène quand même à OpenTV). Touches réelles du kiosque (même table que
 * ui/keys.js) : Échap, BrowserBack/GoBack, Close/Exit (Exit CEC -> XF86Close), Retour arrière hors champ de saisie,
 * et Alt+← (raccourci « page précédente » de Chromium, intercepté pour suivre le même contrat).
 * NB terrain : la touche Retour Samsung (KEY_EXIT) n'atteint JAMAIS la page (Chromium/Wayland) ; sa sortie vers /tv est
 * assurée au niveau de l'appareil par opentv-x-escape (témoin « X ouvert » posé par /tv), jamais par ce script.
 *  Accueil  (BrowserHome/GoHome partout ; Home/Début hors champ de saisie) : retour à OpenTV /tv.
 *  Retour   racine (accueil, explorer…), connexion, vérification de compte : retour DIRECT à OpenTV /tv, même depuis
 *           un champ de saisie (Retour arrière y efface toujours un caractère) ; autre page (post, fil de réponses,
 *           modale routée de X) : page précédente SEULEMENT si une entrée x.com précédente existe réellement
 *           (API Navigation), sinon OpenTV /tv ; filet : si rien ne change en BACK_FAILSAFE_MS, OpenTV /tv.
 *  Connexion (mode « contrôles », SEULEMENT hors fil : aucune colonne principale) : ↑ ↓ (et ← → hors champ) parcourent
 *           les contrôles VISIBLES et activables (boutons, liens, champs) dans l'ordre du document, limités à la
 *           fenêtre modale de X s'il y en a une ; anneau TV très visible ; au chargement, le bouton « Continue with
 *           phone » (ou le premier contrôle à ↓) reçoit le focus si rien n'en a. OK/Entrée RÉELLE : activation native
 *           du bouton/lien au focus (geste humain) ; repli pour un rôle « button » non natif seulement. Champ au focus :
 *           saisie au clavier physique 100 % native (← → Entrée Retour arrière natifs) ; aucune valeur n'est lue,
 *           copiée ni transmise ; ↑ ↓ quittent un champ d'une ligne. Aucun clic automatique, aucun relais d'identifiants.
 * Touches synthétiques (non fiables) : jamais d'action. Aucune lecture de contenu hors libellés des contrôles.
 */
(() => {
  "use strict";
  if (window.top !== window) { return; }
  const BACK_FAILSAFE_MS = 1200;
  const ROOT_PATHS = new Set(["/", "/home", "/explore", "/notifications", "/messages"]);
  // connexion, sortie, inscription, vérifications (challenges) et consentements : toujours un retour direct
  const AUTH_RE = /^\/(i\/flow\/|login(\/|$)|logout(\/|$)|signup(\/|$)|account\/|i\/oauth2?\/|i\/web\/login)/;
  const KEYS = {
    Escape: "back", Esc: "back", Backspace: "back", BrowserBack: "back", GoBack: "back", Close: "back", Exit: "back",
    BrowserHome: "home", Home: "home", GoHome: "home"
  };
  const KEYCODES = { 27: "back", 8: "back", 166: "back", 172: "home", 36: "home" };
  const TEXT_KEYS = new Set(["Backspace", "Home"]);   // dans un champ : édition native, jamais une navigation
  let backTimer = 0;

  function keyAction(e) {
    if (!e || e.isComposing) { return null; }
    if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === "ArrowLeft" || e.keyCode === 37)) { return "back"; }
    if (e.altKey || e.ctrlKey || e.metaKey) { return null; }
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    if (k) { return Object.prototype.hasOwnProperty.call(KEYS, k) ? KEYS[k] : null; }
    return KEYCODES[e.keyCode] || null;
  }
  function textKey(e) {
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    return k ? TEXT_KEYS.has(k) : (e.keyCode === 8 || e.keyCode === 36);
  }
  function editable(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  function goHome() {
    try { chrome.runtime.sendMessage({ type: "home" }, () => void chrome.runtime.lastError); } catch (e) { /* rechargée */ }
  }
  function direct() {
    const p = location.pathname.replace(/\/+$/, "") || "/";
    return ROOT_PATHS.has(p) || AUTH_RE.test(p + "/");
  }
  // entrée x.com précédente réellement présente ? (API Navigation : entrées de MÊME origine seulement)
  function canGoBackInX() {
    const n = window.navigation;
    if (!n || typeof n.entries !== "function" || !n.currentEntry) { return null; }   // inconnu : filet seulement
    return n.currentEntry.index > 0 && n.entries().length > 1;
  }
  function back() {
    if (direct()) { goHome(); return; }
    const can = canGoBackInX();
    if (can === false) { goHome(); return; }
    const before = location.href;
    clearTimeout(backTimer);
    history.back();   // ferme la vue de post / la modale routée de X
    backTimer = setTimeout(() => { if (location.href === before) { goHome(); } }, BACK_FAILSAFE_MS);
  }
  // ---------------------------------------------------------------- mode « contrôles » (pages sans fil)
  const COLUMN_SEL = '[data-testid="primaryColumn"]';
  const CONTROL_SEL = 'a[href], button, input:not([type="hidden"]), textarea, select, summary, [role="button"], ' +
    '[role="link"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"], [role="option"]';
  const CONTROLS_MAX = 200;
  const LABEL_MAX = 80;
  const PREFERRED_RE = /^(continue with phone|continuer avec (le |ton |votre |un )?(t[ée]l[ée]phone|num[ée]ro)|use phone)/i;
  const MOVE_KEYS = { ArrowUp: -1, Up: -1, ArrowDown: 1, Down: 1, ArrowLeft: -1, Left: -1, ArrowRight: 1, Right: 1 };
  const MOVE_CODES = { 38: -1, 40: 1, 37: -1, 39: 1 };
  const VERTICAL = new Set(["ArrowUp", "Up", "ArrowDown", "Down"]);
  const OK_KEYS = new Set(["Enter", "Select", "Accept"]);
  const NATIVE_RE = /^(A|BUTTON|SUMMARY)$/;
  const LINE_INPUT_RE = /^(text|email|tel|password|search|url|number|)$/;
  const FOCUS_ATTR = "data-otvx-focus";
  const RING_CSS = "[" + FOCUS_ATTR + "]{outline:6px solid #ffd400!important;outline-offset:4px!important;" +
    "box-shadow:0 0 0 12px rgba(255,212,0,.35)!important}" +
    ".otvx-login-help{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:2147483647;" +
    "pointer-events:none;background:rgba(0,0,0,.88);color:#fff;border:2px solid #ffd400;border-radius:14px;" +
    "padding:10px 22px;font:600 22px/1.3 system-ui,sans-serif;white-space:nowrap}";
  const HELP_TEXT = "↑ ↓ choisir · OK valider · Retour : OpenTV · Identifiant et mot de passe : au clavier";
  let tvFocus = null;
  let ringStyle = null;
  let loginHelp = null;
  let refreshTimer = 0;
  let autoFocused = false;

  function loginMode() { return !!document.body && !document.querySelector(COLUMN_SEL); }
  function scope() {
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]'));
    for (let i = dialogs.length - 1; i >= 0; i--) { if (visible(dialogs[i])) { return dialogs[i]; } }
    return document.body;
  }
  function visible(el) {
    if (!el || !el.isConnected || el.closest("[hidden], [inert], [aria-hidden='true']")) { return false; }
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) { return false; }
    const cs = getComputedStyle(el);
    return cs.visibility === "visible" && cs.display !== "none" && Number(cs.opacity) > 0.05;
  }
  function usable(el) {
    if (el.disabled || el.getAttribute("aria-disabled") === "true") { return false; }
    if (el.tagName === "A" && !el.getAttribute("href") && !el.getAttribute("role")) { return false; }
    return visible(el);
  }
  function controls() {
    const root = scope();
    if (!root) { return []; }
    const all = Array.from(root.querySelectorAll(CONTROL_SEL)).slice(0, CONTROLS_MAX * 2);
    const out = [];
    for (const el of all) {
      if (out.length >= CONTROLS_MAX) { break; }
      if (!usable(el)) { continue; }
      if (out.some((o) => o.contains(el))) { continue; }   // contrôle dans un contrôle : le plus externe seulement
      out.push(el);
    }
    return out;
  }
  function label(el) {   // libellé VISIBLE d'un bouton/lien seulement (jamais la valeur d'un champ)
    if (editable(el)) { return ""; }
    const t = (el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.slice(0, LABEL_MAX);
  }
  function preferred(list) { return list.find((el) => PREFERRED_RE.test(label(el))) || null; }
  function ensureRing() {
    if (ringStyle && ringStyle.isConnected) { return; }
    ringStyle = document.createElement("style");
    ringStyle.textContent = RING_CSS;
    (document.head || document.documentElement).appendChild(ringStyle);
  }
  function ensureHelp(on) {
    if (!on) {
      if (loginHelp) { loginHelp.remove(); loginHelp = null; }
      return;
    }
    if (loginHelp && loginHelp.isConnected) { return; }
    ensureRing();
    loginHelp = document.createElement("div");
    loginHelp.className = "otvx-login-help";
    loginHelp.setAttribute("aria-hidden", "true");
    loginHelp.textContent = HELP_TEXT;
    document.documentElement.appendChild(loginHelp);
  }
  function setFocus(el) {
    if (tvFocus && tvFocus !== el) { tvFocus.removeAttribute(FOCUS_ATTR); }
    tvFocus = el;
    if (!el) { return; }
    ensureRing();
    el.setAttribute(FOCUS_ATTR, "");
    try { el.focus({ preventScroll: true }); } catch (err) { /* élément retiré */ }
    try { el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (err) { /* ancien moteur */ }
  }
  function current(list) {
    const ae = document.activeElement;
    if (ae && list.includes(ae)) { return ae; }
    return tvFocus && list.includes(tvFocus) ? tvFocus : null;
  }
  function move(dir) {
    const list = controls();
    if (!list.length) { return false; }
    const cur = current(list);
    if (!cur) { setFocus(dir > 0 ? (preferred(list) || list[0]) : list[list.length - 1]); return true; }
    const i = list.indexOf(cur) + dir;
    if (i >= 0 && i < list.length) { setFocus(list[i]); }
    return true;   // bord atteint : on reste (jamais de défilement de page imprévu)
  }
  function lineInput(el) {
    return !!el && el.tagName === "INPUT" && LINE_INPUT_RE.test((el.getAttribute("type") || "").toLowerCase());
  }
  function moveDir(e) {
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    if (k) { return Object.prototype.hasOwnProperty.call(MOVE_KEYS, k) ? { dir: MOVE_KEYS[k], vertical: VERTICAL.has(k) } : null; }
    const d = MOVE_CODES[e.keyCode];
    return d ? { dir: d, vertical: e.keyCode === 38 || e.keyCode === 40 } : null;
  }
  function okKey(e) {
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    return k ? OK_KEYS.has(k) : e.keyCode === 13;
  }
  // OK sur un contrôle non natif (div role=button de X) : si la page n'a pas traité l'Entrée réelle, UN clic sur CE
  // contrôle au focus, dans le même geste humain. Bouton/lien natif : activation native de Chromium, jamais ce repli.
  let pendingOk = null;
  function onKeyLate(e) {
    const p = pendingOk;
    pendingOk = null;
    if (!p || p.ev !== e || e.defaultPrevented || !e.isTrusted) { return; }   // même touche réelle seulement
    if (document.activeElement !== p.el || !p.el.isConnected) { return; }
    p.el.click();
  }
  function loginKey(e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.isComposing || !loginMode()) { return false; }
    const ae = document.activeElement;
    const m = moveDir(e);
    if (m) {
      if (editable(ae) && !(m.vertical && lineInput(ae))) { return false; }   // ← → et zones multilignes : natifs
      e.preventDefault();
      e.stopImmediatePropagation();
      move(m.dir);
      return true;
    }
    if (okKey(e) && !e.repeat) {
      if (!ae || editable(ae) || NATIVE_RE.test(ae.tagName)) { return false; }   // natif : Chromium active (humain)
      const role = ae.getAttribute("role") || "";
      if (/^(button|link|checkbox|radio|switch|tab|menuitem|option)$/.test(role)) { pendingOk = { el: ae, ev: e }; }
    }
    return false;
  }
  function refresh() {
    refreshTimer = 0;
    const on = loginMode();
    ensureHelp(on);
    if (!on) {
      if (tvFocus) { tvFocus.removeAttribute(FOCUS_ATTR); tvFocus = null; }
      return;
    }
    if (tvFocus && !tvFocus.isConnected) { tvFocus = null; }
    const ae = document.activeElement;
    if (!autoFocused && (!ae || ae === document.body || ae === document.documentElement)) {
      const list = controls();
      const p = preferred(list);
      if (p) { autoFocused = true; setFocus(p); }   // « Continue with phone » : OK direct, sans aucune flèche
    } else if (ae && ae !== document.body) {
      autoFocused = true;   // la page (ou Alex) a déjà placé le focus : jamais volé ensuite
    }
  }
  function schedule() { if (!refreshTimer) { refreshTimer = setTimeout(refresh, 200); } }

  function onKey(e) {
    pendingOk = null;
    if (!e.isTrusted) { return; }   // touche synthétique d'une page (script de X ou injecté) : jamais d'action OpenTV
    const a = keyAction(e);
    if (!a) { loginKey(e); return; }
    if (editable(document.activeElement) && textKey(e)) { return; }   // Retour arrière / Début : édition native
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.repeat) { return; }
    if (a === "home") { goHome(); } else { back(); }
  }
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("keydown", onKeyLate, false);   // phase de remontée : après les gestionnaires de X
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", schedule); } else { schedule(); }
  window.addEventListener("load", schedule);
})();
