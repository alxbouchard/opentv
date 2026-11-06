/* OpenTV X TV Mode — Retour / Accueil sur TOUTE page x.com (cadre principal), connexion et vérifications comprises.
 *
 * Script autonome, injecté dès document_start : il ne dépend ni du fil (x.js), ni du chargement complet de la page
 * (une page de connexion blanche ou lente ramène quand même à OpenTV). Touches réelles du kiosque (même table que
 * ui/keys.js) : Échap, BrowserBack/GoBack, Close/Exit (Exit CEC -> XF86Close), Retour arrière hors champ de saisie,
 * et Alt+← (raccourci « page précédente » de Chromium, intercepté pour suivre le même contrat).
 *  Accueil  (BrowserHome/GoHome partout ; Home/Début hors champ de saisie) : retour à OpenTV /tv.
 *  Retour   racine (accueil, explorer…), connexion, vérification de compte : retour DIRECT à OpenTV /tv, même depuis
 *           un champ de saisie (Retour arrière y efface toujours un caractère) ; autre page (post, fil de réponses,
 *           modale routée de X) : page précédente SEULEMENT si une entrée x.com précédente existe réellement
 *           (API Navigation), sinon OpenTV /tv ; filet : si rien ne change en BACK_FAILSAFE_MS, OpenTV /tv.
 * Touches synthétiques (non fiables) : jamais d'action. Aucun clic, aucune lecture de contenu.
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
  function onKey(e) {
    if (!e.isTrusted) { return; }   // touche synthétique d'une page (script de X ou injecté) : jamais d'action OpenTV
    const a = keyAction(e);
    if (!a) { return; }
    if (editable(document.activeElement) && textKey(e)) { return; }   // Retour arrière / Début : édition native
    e.preventDefault();
    e.stopImmediatePropagation();
    if (e.repeat) { return; }
    if (a === "home") { goHome(); } else { back(); }
  }
  window.addEventListener("keydown", onKey, true);
})();
