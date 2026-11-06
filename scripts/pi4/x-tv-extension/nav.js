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
 *           fenêtre modale de X s'il y en a une ; anneau TV très visible ; au chargement, AVANT toute touche ou geste
 *           réel, l'entrée de connexion réelle (« Log in with username or email ») reçoit le focus UNE fois, même si
 *           X a autofocusé le champ identifiant ; à défaut le lien de connexion, puis la passkey native de X
 *           (« Continue with passkey ») ; JAMAIS « Continue with phone » (inscription chez X) ; rien de reconnu :
 *           rien n'est déplacé. Ensuite le focus n'est jamais déplacé seul (sinon ↓ : même préférence, ou premier
 *           contrôle).
 *           OK/Entrée RÉELLE : activation native du bouton/lien au focus (geste humain) ; repli pour un rôle
 *           « button » non natif seulement. Champ au focus :
 *           saisie au clavier physique 100 % native (← → Entrée Retour arrière natifs) ; aucune valeur n'est lue,
 *           copiée ni transmise ; ↑ ↓ quittent un champ d'une ligne. Aucun clic automatique, aucun relais d'identifiants.
 *  Fenêtre modale VISIBLE de X (0.7.8) : elle gagne TOUJOURS, même au-dessus du fil (colonne principale présente) —
 *           mode « contrôles » limité à la fenêtre ; x.js ne déplace jamais le fil caché derrière.
 *  Champ autofocusé par X (0.7.8) : ↑ ↓ en sortent tant qu'Alex n'y a rien tapé (zone multiligne ou éditable
 *           comprise) ; champ d'une ligne : ↑ ↓ en sortent toujours ; après une saisie réelle, zone multiligne native.
 *  Anneau et consigne : feuille de l'extension nav.css (hors CSP de la page) ; repli CSSOM en ligne (jamais bloqué
 *           par la CSP) seulement si la feuille manquait ; jamais de <style> injecté.
 *  Sonde canari (0.7.8) : au chargement, à chaque changement de mode, à chaque APPUI réel et toutes les BEAT_MS
 *           (page visible ou non : l'état est dans l'instantané), instantané borné (≥ PROBE_MIN_MS entre deux envois ;
 *           AUCUN plafond par document : le battement ne meurt jamais sur une page ouverte longtemps — débit tenu
 *           par l'intervalle puis par background.js et le service) au service de l'extension : mode, document au
 *           focus, page visible, compteurs
 *           d'APPUIS par classe move/ok/other (répétitions d'une touche tenue à part dans « repeat », jamais
 *           envoyées seules : move/ok se comparent 1:1 aux appuis evdev), compteur de touches synthétiques, et
 *           booléens du dernier appui réel (fiable, champ au focus, document au focus, traitée par OpenTV). Champ
 *           éditable au focus : une touche de saisie (« other ») n'est NI comptée NI envoyée (longueur et rythme d'un
 *           mot de passe jamais observables). Jamais une valeur ni un code de touche, un texte, une URL ni la valeur
 *           d'un champ. Départ de la page (pagehide) : l'instantané DÉJÀ en attente part tout de suite (au mieux),
 *           sans rien capturer de plus (aucun événement, aucune touche, aucun compteur nouveaux).
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
  // Partagé avec x.js (MÊME monde isolé de l'extension, invisible pour la page) : fenêtre modale visible, touches
  // traitées par OpenTV, champs où Alex a réellement tapé. Objet figé, non réinscriptible.
  const handledKeys = new WeakSet();
  const typedFields = new WeakSet();

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
  function markHandled(e) { handledKeys.add(e); }
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
  // Préférence (0.7.9) — « Continue with phone » de X est l'INSCRIPTION (mode signup, start phone) : jamais préféré.
  // Rang 0 : entrée de connexion réelle (« Log in with username or email » : verbe de connexion + identifiant) ;
  // 1 : lien de connexion (href /login ou /i/flow/login de x.com, indépendant de la langue) ; 2 : passkey native de X
  // (« Continue with passkey » / « Use passkey » : verbe d'action en tête) ; inscription, création, aide : -1 (rien de
  // préféré, fail closed : l'autofocus de X reste). Préférer = focus seulement, jamais une activation.
  const LOGIN_VERB_RE = /\b(log ?in|sign ?in)\b|connexion|se connecter|connecte-toi|iniciar sesi[oó]n|anmelden|accedi/i;
  const LOGIN_ID_RE = /user ?name|e-?mail|nom d['’]utilisateur|identifiant|courriel|usuario|correo|benutzername|nome utente/i;
  const LOGIN_HREF_RE = /^(https:\/\/(www\.|mobile\.)?(x|twitter)\.com)?\/(login|i\/flow\/login)\/?([?#]|$)/i;
  const PASSKEY_RE = /\bpass ?keys?\b|cl[ée]s? d['’](acc[eè]s|identification)|llaves? de acceso|chiavi? d['’]accesso/i;
  const PASSKEY_VERB_RE = /^(continue|use|sign ?in|log ?in|continuer|utiliser|se connecter|connexion|continuar|usar|iniciar sesi[oó]n|weiter|verwenden|mit|anmelden|continua|usa|accedi)\b/i;
  const NOT_LOGIN_RE = /sign ?up|create|regist|reg[ií]str|inscri|cr[ée]er|ajouter|\badd\b|learn|about|en savoir|[àa] propos|^continue with phone|^continuer avec (le |ton |votre |un )?(t[ée]l[ée]phone|num[ée]ro)|^use phone/i;
  function preferRank(text, href) {
    const t = typeof text === "string" ? text : "";
    if (NOT_LOGIN_RE.test(t)) { return -1; }
    if (LOGIN_VERB_RE.test(t) && LOGIN_ID_RE.test(t)) { return 0; }
    if (typeof href === "string" && LOGIN_HREF_RE.test(href)) { return 1; }
    if (PASSKEY_RE.test(t) && PASSKEY_VERB_RE.test(t)) { return 2; }
    return -1;
  }
  const MOVE_KEYS = { ArrowUp: -1, Up: -1, ArrowDown: 1, Down: 1, ArrowLeft: -1, Left: -1, ArrowRight: 1, Right: 1 };
  const MOVE_CODES = { 38: -1, 40: 1, 37: -1, 39: 1 };
  const VERTICAL = new Set(["ArrowUp", "Up", "ArrowDown", "Down"]);
  const OK_KEYS = new Set(["Enter", "Select", "Accept"]);
  const NATIVE_RE = /^(A|BUTTON|SUMMARY)$/;
  const LINE_INPUT_RE = /^(text|email|tel|password|search|url|number|)$/;
  const FOCUS_ATTR = "data-otvx-focus";
  const DIALOG_SEL = '[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"], dialog[open]';
  // repli seulement si nav.css manquait (jamais attendu) : CSSOM en ligne, que la CSP de la page ne bloque pas
  const RING_FALLBACK = [["outline", "6px solid #ffd400"], ["outline-offset", "4px"]];
  const HELP_FALLBACK = [["position", "fixed"], ["bottom", "24px"], ["left", "50%"], ["transform", "translateX(-50%)"],
    ["z-index", "2147483647"], ["pointer-events", "none"], ["background", "rgba(0,0,0,.88)"], ["color", "#fff"],
    ["border", "2px solid #ffd400"], ["border-radius", "14px"], ["padding", "10px 22px"],
    ["font", "600 22px/1.3 system-ui,sans-serif"], ["white-space", "nowrap"]];
  const HELP_TEXT = "↑ ↓ choisir · OK valider · Retour : OpenTV · Identifiant et mot de passe : au clavier";
  let tvFocus = null;
  let inlineRing = null;   // { el, prev } : repli en ligne posé (rétabli tel quel au départ du focus)
  let loginHelp = null;
  let refreshTimer = 0;
  let autoFocused = false;
  let userActed = false;   // touche ou geste RÉEL sur cette page : le focus n'est plus jamais déplacé seul

  // fenêtre modale VISIBLE la plus récente (X l'ajoute à la fin de ses calques) ; null sinon
  function modalDialog() {
    if (!document.body) { return null; }
    const dialogs = Array.from(document.querySelectorAll(DIALOG_SEL)).slice(-20);
    for (let i = dialogs.length - 1; i >= 0; i--) { if (visible(dialogs[i])) { return dialogs[i]; } }
    return null;
  }
  // « dialog » (fenêtre modale visible, même au-dessus du fil) > « feed » (colonne principale) > « controls »
  function pageMode() {
    if (!document.body) { return "loading"; }
    if (modalDialog()) { return "dialog"; }
    return document.querySelector(COLUMN_SEL) ? "feed" : "controls";
  }
  function loginMode() { const m = pageMode(); return m === "dialog" || m === "controls"; }
  function scope() { return modalDialog() || document.body; }
  // champ quitté par ↑ ↓ : champ d'une ligne toujours ; autre champ (zone multiligne, éditable) tant qu'Alex n'y a
  // rien tapé (autofocus de X) — jamais une valeur lue, seulement « une touche de saisie réelle a eu lieu ici »
  function leaveField(el) { return lineInput(el) || (el.tagName !== "SELECT" && !typedFields.has(el)); }
  function visible(el) {
    if (!el || !el.isConnected || el.closest("[hidden], [inert], [aria-hidden='true']")) { return false; }
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) { return false; }
    if (r.right <= 0 || r.left >= innerWidth || r.bottom + scrollY <= 0) { return false; }   // hors écran (lien d'évitement)
    if (typeof el.checkVisibility === "function" &&
        !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) { return false; }   // parent opacity:0
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
  function preferred(list) {   // meilleur rang, puis ordre du document ; null si rien (fail closed)
    let best = null;
    let rank = 3;
    for (const el of list) {
      const r = preferRank(label(el), el.tagName === "A" ? el.getAttribute("href") : null);
      if (r >= 0 && r < rank) { best = el; rank = r; }
    }
    return best;
  }
  function applyInline(el, props) {
    for (const [k, v] of props) { el.style.setProperty(k, v, "important"); }
  }
  function dropRing(el) {
    if (!el) { return; }
    el.removeAttribute(FOCUS_ATTR);
    if (inlineRing && inlineRing.el === el) {   // repli : valeurs en ligne d'origine de la page rétablies
      for (const [k, v, prio] of inlineRing.prev) {
        if (v) { el.style.setProperty(k, v, prio); } else { el.style.removeProperty(k); }
      }
      inlineRing = null;
    }
  }
  function ringFallback(el) {
    let cs = null;
    try { cs = getComputedStyle(el); } catch (err) { return; }
    if (cs.outlineStyle === "solid" && parseFloat(cs.outlineWidth) >= 4) { return; }   // nav.css appliquée
    inlineRing = { el: el, prev: RING_FALLBACK.map(([k]) => [k, el.style.getPropertyValue(k), el.style.getPropertyPriority(k)]) };
    applyInline(el, RING_FALLBACK);
  }
  function ensureHelp(on) {
    if (!on) {
      if (loginHelp) { loginHelp.remove(); loginHelp = null; }
      return;
    }
    if (loginHelp && loginHelp.isConnected) { return; }
    loginHelp = document.createElement("div");
    loginHelp.className = "otvx-login-help";
    loginHelp.setAttribute("aria-hidden", "true");
    loginHelp.textContent = HELP_TEXT;
    document.documentElement.appendChild(loginHelp);
    if (getComputedStyle(loginHelp).position !== "fixed") { applyInline(loginHelp, HELP_FALLBACK); }   // repli
  }
  function setFocus(el) {
    if (tvFocus && tvFocus !== el) { dropRing(tvFocus); }
    tvFocus = el;
    if (!el) { return; }
    el.setAttribute(FOCUS_ATTR, "");
    ringFallback(el);
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
    markHandled(e);
    p.el.click();
  }
  function loginKey(e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.isComposing || !loginMode()) { return false; }
    const ae = document.activeElement;
    const m = moveDir(e);
    if (m) {
      if (editable(ae) && !(m.vertical && leaveField(ae))) { return false; }   // ← →, zone où Alex tape : natifs
      e.preventDefault();
      e.stopImmediatePropagation();
      markHandled(e);
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
    const mode = pageMode();
    probeMode(mode);
    const on = mode === "dialog" || mode === "controls";
    ensureHelp(on);
    if (!on) {
      if (tvFocus) { dropRing(tvFocus); tvFocus = null; }
      return;
    }
    if (tvFocus && !tvFocus.isConnected) { tvFocus = null; }
    const ae = document.activeElement;
    const none = !ae || ae === document.body || ae === document.documentElement;
    if (!autoFocused && !userActed && (none || editable(ae))) {   // rien, ou champ autofocusé par X
      const p = preferred(controls());
      if (p) { autoFocused = true; setFocus(p); }   // connexion réelle (ou passkey) : OK direct, zéro flèche, UNE fois
    } else if (!none) {
      autoFocused = true;   // contrôle placé par la page, ou touche/geste réel : jamais volé ensuite
    }
  }
  function schedule() { if (!refreshTimer) { refreshTimer = setTimeout(refresh, 200); } }

  // ---------------------------------------------------------------- sonde canari (0.7.8) : preuve, jamais une action
  const PROBE_MIN_MS = 400;
  const BEAT_MS = 30000;
  const COUNT_MAX = 1000000;
  const PROBE_RANK = { load: 0, mode: 1, key: 2, beat: 3 };
  const probe = { mode: "loading", seq: 0, move: 0, ok: 0, other: 0, repeat: 0, synthetic: 0, last: null, at: 0,
                  pending: null, timer: 0, loaded: false };
  function bump(k) { probe[k] = Math.min(probe[k] + 1, COUNT_MAX); }
  function probeFlush() {
    probe.timer = 0;
    const ev = probe.pending;
    probe.pending = null;
    if (!ev) { return; }
    probe.at = Date.now();
    probe.seq = Math.min(probe.seq + 1, COUNT_MAX);
    const msg = { type: "probe", mode: probe.mode, event: ev, seq: probe.seq, move: probe.move, ok: probe.ok,
                  other: probe.other, repeat: probe.repeat, synthetic: probe.synthetic, last: probe.last,
                  doc_focus: document.hasFocus(), visible: document.visibilityState === "visible" };
    try { chrome.runtime.sendMessage(msg, () => void chrome.runtime.lastError); } catch (err) { /* rechargée */ }
  }
  function probeSend(ev) {
    if (probe.pending === null || PROBE_RANK[ev] < PROBE_RANK[probe.pending]) { probe.pending = ev; }
    if (probe.timer) { return; }
    probe.timer = setTimeout(probeFlush, Math.max(0, PROBE_MIN_MS - (Date.now() - probe.at)));
  }
  // départ de la page : l'instantané EN ATTENTE part sans attendre PROBE_MIN_MS (au mieux) ; rien de nouveau capturé
  function probeLeave() {
    if (!probe.timer) { return; }
    clearTimeout(probe.timer);
    probeFlush();
  }
  function probeMode(mode) {
    if (!probe.loaded) { probe.loaded = true; probe.mode = mode; probeSend("load"); return; }
    if (mode !== probe.mode) { probe.mode = mode; probeSend("mode"); }
  }
  function keyClass(e) { return moveDir(e) ? "move" : (okKey(e) || e.code === "NumpadEnter") ? "ok" : "other"; }
  function probeKey(e) {
    if (!e.isTrusted) { bump("synthetic"); return; }   // compté seulement : jamais un envoi déclenché par la page
    const cls = keyClass(e);
    const ed = editable(document.activeElement);
    if (ed && cls === "other") { return; }   // saisie dans un champ : ni comptée ni envoyée (mot de passe : ni longueur ni rythme)
    if (e.repeat) { bump("repeat"); return; }   // touche tenue : à part, jamais envoyée seule ; move/ok = appuis (1:1 evdev)
    bump(cls);
    setTimeout(() => {   // après la distribution complète : la touche a-t-elle été traitée par OpenTV ?
      probe.last = { cls: cls, trusted: true, editable: ed, doc_focus: document.hasFocus(), handled: handledKeys.has(e) };
      probeSend("key");
    }, 0);
  }
  function noteTyping(e) {   // saisie réelle dans le champ au focus : « tapé ici » (jamais la valeur ni la touche)
    const ae = document.activeElement;
    if (!editable(ae) || e.altKey || e.ctrlKey || e.metaKey) { return; }
    const k = typeof e.key === "string" ? e.key : "";
    if (k.length === 1 || k === "Backspace" || k === "Delete" || e.isComposing) { typedFields.add(ae); }
  }

  function onKey(e) {
    pendingOk = null;
    probeKey(e);
    if (!e.isTrusted) { return; }   // touche synthétique d'une page (script de X ou injecté) : jamais d'action OpenTV
    userActed = true;
    noteTyping(e);
    const a = keyAction(e);
    if (!a) { loginKey(e); return; }
    if (editable(document.activeElement) && textKey(e)) { return; }   // Retour arrière / Début : édition native
    e.preventDefault();
    e.stopImmediatePropagation();
    markHandled(e);
    if (e.repeat) { return; }
    if (a === "home") { goHome(); } else { back(); }
  }
  try {
    Object.defineProperty(window, "__otvxNav", {   // monde isolé de l'extension seulement (x.js) ; figé
      value: Object.freeze({
        dialog: () => !!modalDialog(),
        leaveField: (el) => leaveField(el),
        markHandled: (e) => markHandled(e)
      }),
      writable: false, configurable: false, enumerable: false
    });
  } catch (err) { /* déjà posé (réinjection) : les écouteurs ci-dessous restent installés */ }
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("keydown", onKeyLate, false);   // phase de remontée : après les gestionnaires de X
  window.addEventListener("pointerdown", (e) => { if (e.isTrusted) { userActed = true; } }, true);   // geste réel
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", schedule); } else { schedule(); }
  window.addEventListener("load", schedule);
  window.addEventListener("pagehide", probeLeave, true);
  setInterval(() => probeSend("beat"), BEAT_MS);   // toujours (< TTL 120 s) : doc_focus/visible expliquent un silence
})();
