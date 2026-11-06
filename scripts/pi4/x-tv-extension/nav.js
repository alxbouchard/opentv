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
 *           réel, le lien générique « Sign in » / « Se connecter » (href /i/jf/onboarding/web?mode=login analysé)
 *           reçoit le focus UNE fois, même si X a autofocusé le champ identifiant ; à défaut la passkey native de X
 *           (« Continue with passkey ») ; JAMAIS Google/Apple ; JAMAIS « Continue with phone » (inscription, mode=signup :
 *           retirée de ↑ ↓ ; href d'inscription strict ou libellé exact connu, jamais « Use your phone » d'une
 *           vérification) ; jamais un sens négatif (« without / instead / different / other passkey », « sans », « au
 *           lieu de », « autre », mot de passe) ; hôtes de href acceptés EXACTEMENT : x.com, www.x.com, mobile.x.com,
 *           twitter.com, www.twitter.com, mobile.twitter.com (https, sans port ni identifiants) ; rien de reconnu :
 *           rien n'est déplacé. Ensuite le focus n'est jamais déplacé seul (sinon ↓ : même préférence, ou premier
 *           contrôle permis), sauf UN réarmement : après un clic de confiance sur ce « Sign in », la MEILLEURE passkey
 *           sûre, visible et ABSENTE du document au clic, de la fenêtre que X ouvre dans la page, reçoit le focus une
 *           fois (15 s monotones, minuterie explicite, annulé par toute touche/geste réel). Inscription au focus posée
 *           par X : focus vers l'entrée préférée, sinon retiré (blur) ; jamais un clic.
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
  // Préférence (0.7.9, DOM RÉEL de x.com déconnecté, SSR 2026-11-06) : « Continue with phone » ->
  // /i/jf/onboarding/web?mode=signup (INSCRIPTION : jamais préférée, jamais atteinte par ↑ ↓) ; Google/Apple ->
  // ?mode=login (fournisseurs : atteignables à la main, jamais préférés) ; lien générique « Sign in » / « Se connecter »
  // -> ?mode=login. Rang RANK_LOGIN : ce lien générique SEUL (href analysé + libellé ancré en entier) ; RANK_PASSKEY :
  // passkey native de X (bouton, verbe d'action en tête) ; sinon -1 (rien de préféré, fail closed). Préférer = focus
  // seulement, jamais une activation.
  const X_HOST_RE = /^(www\.|mobile\.)?(x|twitter)\.com$/;
  const ONBOARDING_PATH = "/i/jf/onboarding/web";
  const LEGACY_LOGIN_PATHS = new Set(["/login", "/i/flow/login"]);
  const SIGNUP_PATHS = new Set(["/signup", "/i/flow/signup"]);
  const RANK_LOGIN = 0;
  const RANK_PASSKEY = 1;
  const GENERIC_LOGIN_RE = /^(sign ?in|log ?in|se connecter|connexion|connecte-toi|iniciar sesi[oó]n|anmelden|einloggen|accedi|entrar)$/i;
  const PASSKEY_RE = /\bpass ?keys?\b|cl[ée]s? d['’](acc[eè]s|identification)|llaves? de acceso|chiavi? d['’]accesso/i;
  const PASSKEY_VERB_RE = /^(continue|use|sign ?in|log ?in|continuer|utiliser|se connecter|connexion|continuar|usar|iniciar sesi[oó]n|weiter|verwenden|mit|anmelden|continua|usa|accedi)\b/i;
  const SIGNUP_RE = /\bsign ?up\b|\bcreate (an |your |a new )?account\b|s['’]inscrire|\binscri(s|ption)\b|cr[ée]er (un |ton |votre )?compte/i;
  // libellés EXACTS (ancrés en entier) de l'inscription par téléphone de X (« Continue with phone » : mode=signup) ;
  // jamais une tournure large (« Use your phone », « Use phone to verify » : vérifications légitimes, atteignables)
  const SIGNUP_PHONE_RE = /^(continue with phone( number)?|sign up with phone( number)?|continuer avec (le |un |ton |votre )?(num[ée]ro (de )?)?t[ée]l[ée]phone|continuar con (el |tu )?(n[uú]mero de )?tel[ée]fono|weiter mit (der |deiner )?telefon(nummer)?|mit telefon(nummer)? fortfahren|continua con (il )?(numero di )?telefono)$/i;
  // sens NÉGATIF ou détourné : « sans », « au lieu de », « autre / différente », mot de passe (libellé seulement,
  // jamais un champ) — jamais préféré
  const NEGATED_RE = /\b(without|instead|different|another|other|not|no)\b|\bsans\b|au lieu|\bplut[ôo]t\b|\bautres?\b|diff[ée]rente?s?\b|\bpas\b|\bsin\b|en (lugar|vez)|\botr[oa]s?\b|\bohne\b|\bstatt\b|\bandere[nmrs]?\b|\bsenza\b|invece|\baltr[oa]\b|\bdivers[oa]\b|pass ?wor[dt]|mot de passe|contrase[ñn]a/i;
  // passkey CANONIQUE de X (libellé entier : verbe + « with/avec » + passkey) : préférée aux variantes plus libres
  const PASSKEY_EXACT_RE = /^((continue|sign ?in|log ?in|continuer|se connecter|connexion|continuar|iniciar sesi[oó]n|weiter|anmelden|continua|accedi) (with|avec|con|mit) |(use|utiliser|usar|verwenden|usa) )(a |an |your |une |ta |votre |una |tu |il |la )?(pass ?key|cl[ée] d['’](acc[eè]s|identification)|llave de acceso|chiave d['’]accesso)$/i;
  const NOT_LOGIN_RE = /learn|about|en savoir|[àa] propos|\badd\b|ajouter|create|cr[ée]er|regist|reg[ií]str|d[ée]connex|d[ée]connect|log ?out|sign ?out/i;
  // href analysé comme URL (jamais une sous-chaîne) : « login » | « signup » | « other » ; null sans href
  function hrefKind(href) {
    if (typeof href !== "string") { return null; }
    let u = null;
    try { u = new URL(href, "https://x.com/"); } catch (err) { return "other"; }
    // https, hôte X exact, sans port ; URL recomposée identique (sinon informations d'utilisateur dans l'URL : refusée)
    if (u.protocol !== "https:" || !X_HOST_RE.test(u.hostname) || u.port ||
        u.href !== u.origin + u.pathname + u.search + u.hash) { return "other"; }
    const path = u.pathname.replace(/\/+$/, "") || "/";
    const q = u.searchParams;
    const modes = q.getAll("mode");
    const starts = q.getAll("start");
    if (SIGNUP_PATHS.has(path)) { return "signup"; }
    if (path !== ONBOARDING_PATH && !LEGACY_LOGIN_PATHS.has(path)) { return "other"; }
    if (modes.indexOf("signup") >= 0 || starts.some((v) => /phone/i.test(v))) { return "signup"; }
    if (starts.length) { return "other"; }
    if (path === ONBOARDING_PATH) { return modes.length === 1 && modes[0] === "login" ? "login" : "other"; }
    return modes.length === 0 || (modes.length === 1 && modes[0] === "login") ? "login" : "other";
  }
  function signupLike(text, href) {   // inscription (href strict ou libellé exact connu) : retirée des flèches
    const t = (typeof text === "string" ? text : "").trim();
    return hrefKind(href) === "signup" || SIGNUP_RE.test(t) || SIGNUP_PHONE_RE.test(t);
  }
  function preferRank(text, href) {
    const t = (typeof text === "string" ? text : "").trim();
    const k = hrefKind(href);
    if (signupLike(t, href) || NOT_LOGIN_RE.test(t) || NEGATED_RE.test(t)) { return -1; }
    if (k === "login" && GENERIC_LOGIN_RE.test(t)) { return RANK_LOGIN; }   // « Sign in » générique, jamais un fournisseur
    if (k === null && PASSKEY_RE.test(t) && PASSKEY_VERB_RE.test(t)) { return RANK_PASSKEY; }
    return -1;
  }
  // score de préférence (plus grand = meilleur ; 0 = jamais) : « Sign in » générique > passkey canonique > autre passkey
  function preferScore(text, href) {
    const r = preferRank(text, href);
    if (r === RANK_LOGIN) { return 3; }
    if (r === RANK_PASSKEY) { return PASSKEY_EXACT_RE.test(String(text).trim()) ? 2 : 1; }
    return 0;
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
  // Réarmement UNIQUE (0.7.9) : X ouvre sa fenêtre de connexion DANS la page (preventDefault + routeur). Seul un clic
  // de confiance (OK réel ou pointeur) sur l'entrée générique reconnue l'arme, une fois par document, REARM_MS au plus
  // (horloge MONOTONE performance.now : un recalage NTP de la Pi ne l'allonge ni ne l'abrège ; minuterie explicite :
  // références libérées même sans aucune mutation) ; toute touche/geste réel suivant l'annule ; consommé par la
  // MEILLEURE passkey sûre et visible ABSENTE du document au moment du clic.
  const REARM_MS = 15000;
  const SEEN_MAX = 2000;   // contrôles instantanés au clic ; au-delà : rien d'armé (fail closed)
  let loginArm = null;   // { until, seen, timer } : seen = TOUS les contrôles présents au clic, visibles ou non
  let armSpent = false;

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
    const dropped = [];
    for (const el of all) {
      if (out.length >= CONTROLS_MAX) { break; }
      if (!usable(el)) { continue; }
      if (out.some((o) => o.contains(el)) || dropped.some((o) => o.contains(el))) { continue; }   // le plus externe
      if (signupLike(label(el), hrefOf(el))) { dropped.push(el); continue; }   // inscription : jamais atteinte par ↑ ↓
      out.push(el);
    }
    return out;
  }
  function label(el) {   // libellé VISIBLE d'un bouton/lien seulement (jamais la valeur d'un champ)
    if (editable(el)) { return ""; }
    const t = (el.getAttribute("aria-label") || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.slice(0, LABEL_MAX);
  }
  function hrefOf(el) { return el && el.tagName === "A" ? el.getAttribute("href") : null; }
  function preferred(list) {   // meilleur score, puis ordre du document ; null si rien (fail closed)
    let best = null;
    let score = 0;
    for (const el of list) {
      const sc = preferScore(label(el), hrefOf(el));
      if (sc > score) { best = el; score = sc; }
    }
    return best;
  }
  // réarmement : MEILLEURE passkey sûre (score) parmi les contrôles ABSENTS au clic ; null sinon
  function newPasskey(list, seen) {
    let best = null;
    let score = 0;
    for (const el of list) {
      if (seen.has(el) || preferRank(label(el), hrefOf(el)) !== RANK_PASSKEY) { continue; }
      const sc = preferScore(label(el), hrefOf(el));
      if (sc > score) { best = el; score = sc; }
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
  // clic de confiance sur l'entrée générique « Sign in » reconnue (visible, dans la portée) : arme UNE fois
  function onActivate(e) {
    if (!e.isTrusted || armSpent || e.button !== 0 || !loginMode()) { return; }
    const t = e.target instanceof Element ? e.target.closest(CONTROL_SEL) : null;
    if (!t || preferRank(label(t), hrefOf(t)) !== RANK_LOGIN) { return; }
    if (!controls().includes(t)) { return; }
    armSpent = true;
    // TOUS les contrôles présents au clic (une passkey masquée puis révélée n'est jamais « nouvelle ») ; aucune valeur
    const all = document.querySelectorAll(CONTROL_SEL);
    if (all.length > SEEN_MAX) { return; }
    disarm();
    loginArm = { until: performance.now() + REARM_MS, seen: new Set(all), timer: setTimeout(disarm, REARM_MS) };
  }
  function disarm() {   // expiration, consommation ou entrée réelle : minuterie et références libérées
    if (!loginArm) { return; }
    clearTimeout(loginArm.timer);
    loginArm = null;
  }
  // X a mis le focus sur une INSCRIPTION connue (jamais atteinte par ↑ ↓) : un OK réel l'ouvrirait. Focus déplacé vers
  // l'entrée de connexion préférée ; à défaut retiré (blur). Jamais un clic.
  function leaveSignup(ae) {
    autoFocused = true;
    const p = preferred(controls());
    if (p) { setFocus(p); return; }
    if (tvFocus === ae) { dropRing(ae); tvFocus = null; }
    try { ae.blur(); } catch (err) { /* élément retiré */ }
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
    if (loginArm && performance.now() >= loginArm.until) { disarm(); }
    if (loginArm) {   // fenêtre de connexion ouverte DANS la page après le clic humain : passkey nouvelle et visible
      const pk = newPasskey(controls(), loginArm.seen);
      if (pk) { disarm(); autoFocused = true; setFocus(pk); return; }   // UNE fois, focus seulement
    }
    if (!none && signupLike(label(ae), hrefOf(ae))) { leaveSignup(ae); return; }   // inscription au focus : jamais
    if (loginArm) { return; }
    if (!autoFocused && !userActed && (none || editable(ae) || signupLike(label(ae), hrefOf(ae)))) {   // champ de X
      const p = preferred(controls());
      if (p) { autoFocused = true; setFocus(p); }   // « Sign in » générique (ou passkey) : OK direct, UNE fois
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
    disarm();   // touche réelle après le clic : le réarmement ne déplace plus rien
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
  window.addEventListener("pointerdown", (e) => { if (e.isTrusted) { userActed = true; disarm(); } }, true);
  window.addEventListener("click", onActivate, true);   // clic de confiance seulement (OK réel ou pointeur) : arme
  window.addEventListener("focusin", schedule, true);   // focus posé par X (inscription comprise) : revérifié
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", schedule); } else { schedule(); }
  window.addEventListener("load", schedule);
  window.addEventListener("pagehide", probeLeave, true);
  setInterval(() => probeSend("beat"), BEAT_MS);   // toujours (< TTL 120 s) : doc_focus/visible expliquent un silence
})();
