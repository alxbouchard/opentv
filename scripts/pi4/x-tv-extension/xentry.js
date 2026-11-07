/* OpenTV X TV Mode — « Saisir avec le téléphone » (0.7.10 ; placement 0.7.11 ; champ « jf » 0.7.12 ; OPENTV_X_PHONE_ENTRY).
 *
 * But UNIQUE : placer UN identifiant X (nom d'utilisateur, courriel, téléphone) dans le champ de connexion ÉLIGIBLE au
 * focus, sans clavier physique ; Alex continue ensuite lui-même (Suivant, passkey native). Jamais un mot de passe, un
 * code, un NIP, une passkey, un code de secours ni un paiement ; jamais de clic, d'envoi de formulaire ni d'action.
 *
 * Monde ISOLÉ de l'extension (la page X ne voit ni ces variables, ni la clé, ni le QR : racines fantômes FERMÉES).
 * - Activation : pastille « Saisir avec le téléphone » insérée juste après le champ éligible dans le parcours ↑ ↓ de
 *   nav.js, atteinte par une flèche RÉELLE (nav.js signale navFocus) puis UN OK réel ; un focus posé par la page, une
 *   touche ou un clic synthétiques n'arment jamais rien. Drapeau du service relu sur /tv (tvkey.js -> background.js).
 * - Placement (0.7.11, bloc <place> pur, exécuté tel quel par le harnais) : pastille et panneau QR VERTICAL mesurés
 *   APRÈS insertion, posés dans une case libre ENTIÈREMENT à l'écran (≥ 16 px des bords), à ≥ 16 px du champ, du
 *   formulaire/de la fenêtre, de chaque contrôle visible (Continuer…) et du QR natif de X ; d'abord à DROITE du
 *   formulaire ; QR réduit (jusqu'à 5 px/module) s'il ne tient pas ; sinon aucune pastille / aucune séance.
 * - Champ éligible (porte, bloc <gate> exécuté tel quel par tests/x_entry_harness.js) : cadre principal, https ;
 *   script INJECTÉ seulement sur x.com et twitter.com (manifest.json : jamais www./mobile., que la porte accepterait
 *   pourtant) ; URL de connexion seulement (/i/flow/login, /login, /i/jf/onboarding/web?mode=login, et l'accueil
 *   déconnecté « / » en forme canonique EXACTE — href === origin + "/", sans « ? » ni « # », même nus — AVEC la
 *   signature statique de son vrai champ : id landing-identifier, name username, formulaire action EXACTE
 *   /i/jf/onboarding/web et UN contrôle « mode » caché d'attribut value « login » ; jamais une valeur saisie ; OU, à
 *   « / » comme sur les routes de connexion, la signature EXACTE du champ « jf » que le script de X met à sa place :
 *   jfSigOk, seul cas où le motif de X et son leurre de mot de passe inert, invisible, frère du champ, sont admis ;
 *   formulaire sans aucun « mode » ; invisibilité PROPRE du leurre prouvée par son rendu, jamais par inert), champ
 *   INPUT au focus, visible, actif, modifiable, d'une ligne, type text/email/tel, autocomplete username / email /
 *   tel / tel-national / tel-local (± webauthn) ; refusé : inputmode numérique, maxlength ≤ 8, pattern, et tout
 *   libellé/nom/placeholder du champ ou titre de la fenêtre évoquant mot de passe, code, OTP, NIP, vérification, 2FA,
 *   passkey, secours, récupération, jeton, carte, paiement (EN/FR) ; ou un champ mot de passe / code à usage unique dans
 *   la même fenêtre ou le même formulaire. La VALEUR du champ n'est jamais lue.
 * - Séance : paire RSA-OAEP-2048/SHA-256 NEUVE, clé privée NON EXPORTABLE (WebCrypto) gardée ici seulement ; la clé
 *   publique part au service (background.js), qui rend `sid` + adresse de la télécommande ; QR = adresse +
 *   `#<secret>.<empreinte SHA-256 de n>` (fragment : jamais vu par le service). Chiffré relevé UNE fois, déchiffré ici,
 *   s/f vérifiés, identifiant nettoyé (contrôles, bidi, largeur nulle, sauts de ligne) et borné (ID_MAX) ; rempli
 *   seulement si le MÊME champ est encore au focus, éligible, document au focus et même URL : setter natif, UN
 *   « input » et UN « change », rien d'autre. Puis : tampon déchiffré mis à zéro ; clé, secret, identifiant (chaînes
 *   JS) et champ : références lâchées (ramasse-miettes), jamais réécrits en place. Code de 4 à 9 chiffres refusé ICI.
 * - Fin : livraison, refus, TTL_MS (horloge monotone), toute touche réelle, focus ailleurs, navigation (pagehide,
 *   navigate, popstate), annulation du téléphone, Retour (kiosque relancé : page détruite, /tv efface le service).
 * Limite (README) : la page du téléphone est servie en HTTP LAN ; un attaquant ACTIF du LAN pourrait la remplacer.
 */
(() => {
  "use strict";
  if (window.top !== window) { return; }
  const TTL_MS = 120000;
  const POLL_MS = 1000;
  const TOAST_MS = 7000;
  const SID_RE = /^[A-Za-z0-9_-]{22}$/;
  const CT_RE = /^[A-Za-z0-9_-]{342}$/;
  const URL_RE = /^http:\/\/[a-z0-9.-]{1,253}:[0-9]{2,5}\/r\/x\?s=([A-Za-z0-9_-]{22})$/;
  const DIALOG_SEL = '[role="dialog"], [role="alertdialog"], dialog';
  const DANGER_SEL = 'input[type="password" i], input[autocomplete~="current-password" i], ' +
    'input[autocomplete~="new-password" i], input[autocomplete~="one-time-code" i]';
  const PILL_TEXT = "Saisir avec le téléphone";
  const WARN = "Identifiant seulement — jamais mot de passe ou code";
  const MSG = {
    filled: "Identifiant saisi. Vérifie-le, puis Suivant et ta clé d'accès.",
    refused: "Rien n'a été saisi : ce champ n'accepte plus la saisie (changé, plus sélectionné ou page changée).",
    rejected: "Rien n'a été saisi : envoi rejeté (illisible, altéré, autre QR, ou ressemble à un code).",
    cancelled: "Saisie par téléphone annulée.",
    expired: "Saisie par téléphone expirée (2 minutes).",
    fail: "Saisie par téléphone indisponible pour l'instant.",
    ineligible: "Ce champ n'accepte pas la saisie par téléphone.",
    room: "Rien n'a été lancé : pas de place libre à l'écran pour le QR sans cacher X."
  };
  // <gate> — fonctions PURES (aucun DOM) : exécutées telles quelles par tests/x_entry_harness.js (+ mutations)
  const ID_MAX = 100;
  const X_HOST_RE = /^(www\.|mobile\.)?(x|twitter)\.com$/;
  const LOGIN_PATHS = new Set(["/i/flow/login", "/login"]);
  const ONBOARDING_PATH = "/i/jf/onboarding/web";
  const TYPE_OK = new Set(["text", "email", "tel"]);
  const AC_OK = new Set(["username", "email", "tel", "tel-national", "tel-local"]);
  const DENY_RE = /pass|mot de passe|\bmdp\b|code|\botp\b|one.?time|usage unique|\bpin\b|\bnip\b|v[ée]rif|2fa|\bmfa\b|two.?factor|2.?step|two.?step|deux (facteurs|[ée]tapes)|double authentification|authentifica|authenticat|backup|secours|r[ée]cup[ée]ration|recovery|cl[ée]s? d['’]acc[eè]s|security key|cl[ée] de s[ée]curit[ée]|token|jeton|\bcard\b|carte|\bcvc\b|\bcvv\b|\biban\b|paiement|payment|expir|confirm/i;
  // contrôles, sauts de ligne, bidi, largeur nulle : jamais écrits dans le champ
  const STRIP_RE = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
  // ressemble à un code (OTP, vérification, NIP) : chiffres (Unicode compris), espaces, points, tirets SEULEMENT, de 4 à
  // 9 chiffres. Refusé ICI aussi : la page du téléphone (HTTP LAN) peut être altérée. « +1 514… », ≥ 10 chiffres : ok.
  const CODE_RE = /^[\p{Nd}\s.-]+$/u;
  const DIGIT_RE = /\p{Nd}/gu;
  // URL -> "root" (accueil déconnecté exact), "login" (route de connexion) ou false
  function loginUrl(href) {
    let u = null;
    try { u = new URL(href); } catch (err) { return false; }
    if (u.protocol !== "https:" || !X_HOST_RE.test(u.hostname) || u.port || u.username || u.password) { return false; }
    const path = u.pathname.replace(/\/+$/, "") || "/";
    // accueil déconnecté (champ « landing-identifier ») : forme canonique EXACTE https://x.com/ seulement — aucune
    // requête (mode, start, lang…), aucun fragment, pas même « ? » ou « # » nus ; jamais « // », /home ni l'inscription
    if (u.pathname === "/") { return u.href === u.origin + "/" ? "root" : false; }
    const modes = u.searchParams.getAll("mode");
    if (u.searchParams.has("start")) { return false; }   // « start=phone… » : inscription, jamais une connexion
    if (LOGIN_PATHS.has(path)) { return modes.every((m) => m === "login") ? "login" : false; }
    return path === ONBOARDING_PATH && modes.length === 1 && modes[0] === "login" ? "login" : false;
  }
  // accueil « / » : signature RÉELLE du champ de connexion (attributs STATIQUES seulement, jamais une valeur saisie) :
  // id landing-identifier, name username, formulaire action EXACTE /i/jf/onboarding/web, UN seul contrôle « mode »,
  // caché, attribut value « login ». Un champ d'inscription rendu sur place à « / » n'est jamais éligible.
  function rootSigOk(f) {
    if (f.id !== "landing-identifier" || f.name !== "username" || f.formAction !== ONBOARDING_PATH) { return false; }
    const m = f.formModes;
    return Array.isArray(m) && m.length === 1 && !!m[0] && m[0].type === "hidden" && m[0].valueAttr === "login";
  }
  // 0.7.12 (terrain : pastille absente) : le script de X REMPLACE sur place, sans changer d'URL, le formulaire servi par
  // celui de son moteur « jf » — à « / » comme sur /i/flow/login et /i/jf/onboarding/web?mode=login (relevé réel
  // 2026-11-07, Chromium 1920×1080). Signature EXACTE de cette étape identifiant (attributs STATIQUES seulement) : id
  // jf-input-username_or_email, name username_or_email, motif EXACT de X, formulaire SANS action, et UN SEUL champ
  // sensible dans la portée : le leurre de X pour les gestionnaires de mots de passe (input inert, type password,
  // name password, invisible, frère du champ). Tout autre mot de passe ou code, visible ou non : jamais éligible.
  // AUCUN contrôle « mode » (ni login, ni signup, caché ou non) : le formulaire jf réel n'en porte pas.
  const JF_ID = "jf-input-username_or_email";
  const JF_NAME = "username_or_email";
  const JF_PATTERN = "^[a-zA-Z0-9@._+\\-]+$";
  function jfSigOk(f) {
    if (f.id !== JF_ID || f.name !== JF_NAME || f.pattern !== JF_PATTERN) { return false; }
    if (!Array.isArray(f.formModes) || f.formModes.length !== 0) { return false; }   // AUCUN « mode »
    return f.hasForm === true && f.formAction === null && f.dangerN === 1 && f.decoyN === 1;
  }
  // d : faits d'un champ sensible de la portée (attributs + invisibilité PROPRE prouvée par son rendu) -> leurre exact ?
  // Un mot de passe inert mais RENDU (visible) n'est jamais un leurre : l'attribut inert ne prouve pas l'invisibilité.
  function decoyOk(d) {
    return !!d && d.other === true && d.input === true && d.sibling === true && d.inert === true &&
      d.type === "password" && d.name === "password" && d.ownHidden === true;
  }
  // f : faits relevés sur le champ (attributs seulement, JAMAIS la valeur) -> éligible ?
  function factsOk(f) {
    if (!f || f.top !== true || f.focused !== true || f.visible !== true || f.tag !== "INPUT") { return false; }
    const kind = loginUrl(f.url);
    if (kind !== "root" && kind !== "login") { return false; }
    const jf = jfSigOk(f);
    if (kind === "root" && !rootSigOk(f) && !jf) { return false; }
    if (f.disabled || f.readonly || !TYPE_OK.has(f.type)) { return false; }
    const toks = String(f.ac).toLowerCase().trim().split(/\s+/).filter(Boolean);
    if (toks.length === 2 && toks[1] === "webauthn") { toks.pop(); }
    if (toks.length !== 1 || !AC_OK.has(toks[0])) { return false; }
    if (/^(numeric|decimal)$/i.test(String(f.inputmode).trim())) { return false; }
    if (f.maxlength > 0 && f.maxlength <= 8) { return false; }   // code court
    if (!jf && (f.pattern || f.danger)) { return false; }         // motif imposé, mot de passe/code à côté (hors jf exact)
    return Array.isArray(f.texts) && !f.texts.some((t) => DENY_RE.test(String(t)));
  }
  // document déchiffré -> identifiant nettoyé et borné, ou null (séance, secret, champs exacts)
  function identOf(doc, sid, secret) {
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) { return null; }
    if (Object.keys(doc).sort().join(",") !== "f,s,t,v" || doc.v !== 1 || typeof doc.t !== "string") { return null; }
    if (typeof sid !== "string" || doc.s !== sid || typeof secret !== "string" || !sameText(doc.f, secret)) { return null; }
    const t = doc.t.replace(STRIP_RE, "").trim();
    if (CODE_RE.test(t) && (t.match(DIGIT_RE) || []).length >= 4 && (t.match(DIGIT_RE) || []).length <= 9) { return null; }
    return t && Array.from(t).length <= ID_MAX ? t : null;
  }
  // décision FINALE, juste avant d'écrire : identifiant valide, MÊME champ au focus, document au focus, MÊME URL
  // exacte qu'au départ, champ ENCORE éligible (faits relevés par deliver() sur le DOM réel à cet instant)
  function fillOk(d) {
    if (!d || typeof d.ident !== "string" || !d.ident || d.same !== true || d.docFocus !== true) { return false; }
    return typeof d.href === "string" && d.href === d.startHref && d.eligible === true;
  }
  function sameText(a, b) {   // comparaison sans sortie anticipée
    if (typeof a !== "string" || a.length !== b.length) { return false; }
    let d = 0;
    for (let i = 0; i < a.length; i++) { d |= a.charCodeAt(i) ^ b.charCodeAt(i); }
    return d === 0;
  }
  // </gate>
  // <place> — géométrie PURE (aucun DOM) : exécutée telle quelle par tests/x_entry_harness.js (+ mutations)
  const EDGE = 16;   // marge minimale à chaque bord de l'écran
  const GAP = 16;    // écart minimal avec chaque obstacle (champ, formulaire, Continuer, contrôles, QR natif de X)
  const STEP = 8;
  function hits(a, b, gap) {   // rectangles {left, top, right, bottom} à moins de `gap` px l'un de l'autre ?
    return a.left < b.right + gap && b.left < a.right + gap && a.top < b.bottom + gap && b.top < a.bottom + gap;
  }
  // case w×h ENTIÈREMENT à l'écran (≥ EDGE de chaque bord), à ≥ GAP de chaque obstacle ; d'abord à DROITE de `scope`
  // (formulaire/fenêtre de X), puis au plus près de `pref` ; null si aucune place (jamais par-dessus un obstacle)
  function placeBox(vw, vh, w, h, scope, pref, obstacles) {
    if (!(w > 0 && h > 0) || w > vw - 2 * EDGE || h > vh - 2 * EDGE) { return null; }
    const maxL = vw - w - EDGE;
    const maxT = vh - h - EDGE;
    const pl = Number.isFinite(pref.left) ? pref.left : EDGE;
    const pt = Number.isFinite(pref.top) ? pref.top : EDGE;
    const axis = (max, p) => {   // grille de STEP px + bords exacts + point préféré borné
      const out = [EDGE, max, Math.min(max, Math.max(EDGE, Math.round(p)))];
      for (let v = EDGE; v < max; v += STEP) { out.push(v); }
      return out;
    };
    const tops = axis(maxT, pt);
    let best = null;
    for (const left of axis(maxL, pl)) {
      for (const top of tops) {
        const box = { left: left, top: top, right: left + w, bottom: top + h };
        if (obstacles.some((o) => hits(box, o, GAP))) { continue; }
        const score = (left >= scope.right + GAP ? 0 : 1e6) + Math.abs(left - pl) + Math.abs(top - pt);
        if (!best || score < best.score) { best = { left: left, top: top, score: score }; }
      }
    }
    return best && { left: best.left, top: best.top };
  }
  // </place>

  function b64uEnc(bytes) {
    let s = "";
    for (const b of bytes) { s += String.fromCharCode(b); }
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64uDec(s) {
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) { out[i] = bin.charCodeAt(i); }
    return out;
  }
  function visible(el) {
    if (!el.isConnected || el.closest("[hidden], [inert], [aria-hidden='true']")) { return false; }
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right <= 0 || r.left >= innerWidth || r.bottom <= 0 || r.top >= innerHeight) {
      return false;
    }
    return typeof el.checkVisibility !== "function" ||
      el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  }
  function idsText(v) {
    return String(v || "").split(/\s+/).filter(Boolean).slice(0, 4).map((id) => {
      const n = document.getElementById(id);
      return n ? String(n.textContent || "").slice(0, 200) : "";
    });
  }
  function facts(el) {   // attributs, libellés et titres seulement ; jamais la valeur du champ
    const dlg = el.closest(DIALOG_SEL);
    const scope = dlg || el.form || document;
    const texts = ["name", "id", "placeholder", "aria-label", "title", "data-testid"].map((a) => el.getAttribute(a) || "");
    texts.push(...idsText(el.getAttribute("aria-labelledby")), ...idsText(el.getAttribute("aria-describedby")));
    for (const l of Array.from(el.labels || []).slice(0, 4)) { texts.push(String(l.textContent || "").slice(0, 200)); }
    if (dlg) {
      texts.push(dlg.getAttribute("aria-label") || "", ...idsText(dlg.getAttribute("aria-labelledby")));
      for (const h of Array.from(dlg.querySelectorAll("h1, h2, [role='heading']")).slice(0, 4)) {
        texts.push(String(h.textContent || "").slice(0, 200));
      }
    }
    // signature de l'accueil : attributs STATIQUES (getAttribute) du champ, de son formulaire et du contrôle « mode »
    // caché ; jamais une valeur saisie
    const form = el.form;
    const modes = form ? Array.from(form.elements).filter((c) => c.getAttribute("name") === "mode").slice(0, 4) : [];
    const dangers = Array.from(scope.querySelectorAll(DANGER_SEL)).slice(0, 8);   // mots de passe / codes de la portée
    return {
      top: window.top === window, focused: document.activeElement === el, visible: visible(el), tag: el.tagName,
      disabled: el.disabled, readonly: el.readOnly, url: location.href, hasForm: !!form,
      id: el.getAttribute("id"), name: el.getAttribute("name"), formAction: form ? form.getAttribute("action") : null,
      formModes: modes.map((c) => ({ type: c.tagName === "INPUT" ? (c.getAttribute("type") || "").trim().toLowerCase() : "",
                                     valueAttr: c.getAttribute("value") })),
      type: (el.getAttribute("type") || "text").trim().toLowerCase(), ac: el.getAttribute("autocomplete") || "",
      inputmode: el.getAttribute("inputmode") || "", maxlength: el.maxLength, pattern: el.getAttribute("pattern") || "",
      danger: dangers.length > 0, dangerN: dangers.length, decoyN: dangers.filter((c) => decoyOk(decoyFacts(c, el))).length,
      texts: texts
    };
  }
  // leurre « jf » de X : <input inert type=password name=password> FRÈRE du champ, invisible (attributs + rendu ; jamais
  // une valeur). Invisibilité PROPRE (ownHidden), indépendante d'inert : visible() rend false pour tout élément inert.
  function decoyFacts(c, el) {
    return { other: c !== el, input: c.tagName === "INPUT", sibling: c.parentElement === el.parentElement,
             inert: c.hasAttribute("inert"), type: (c.getAttribute("type") || "").trim().toLowerCase(),
             name: c.getAttribute("name"), ownHidden: ownHidden(c) };
  }
  // PREUVE que l'élément lui-même n'est pas rendu : display none, visibility hidden/collapse, opacité PROPRE nulle, ou
  // checkVisibility (ancêtre display none / opacité nulle / content-visibility hidden ; jamais « auto » : hors écran
  // seulement, défilable). Sans preuve : réputé visible (fail-closed).
  function ownHidden(c) {
    if (!c.isConnected) { return false; }   // style calculé vide hors du document : aucune preuve
    const cs = getComputedStyle(c);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse" || cs.opacity === "0") {
      return true;
    }
    return typeof c.checkVisibility === "function" &&
      !c.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  }
  function eligible(el) { return el instanceof HTMLInputElement && factsOk(facts(el)); }

  // ---------------------------------------------------------------- affichage (racines fantômes FERMÉES, CSSOM)
  function css(el, props) { for (const k of Object.keys(props)) { el.style.setProperty(k, props[k], "important"); } }
  function shell(kind, props) {
    const host = document.createElement("div");
    host.setAttribute("data-otvx-entry", kind);
    css(host, Object.assign({ position: "fixed", "z-index": "2147483647", display: "block" }, props));
    return { host: host, root: host.attachShadow({ mode: "closed" }) };
  }
  function line(root, text, props) {
    const p = document.createElement("div");
    p.textContent = text;
    css(p, Object.assign({ font: "600 24px/1.35 system-ui, sans-serif", color: "#fff", margin: "6px 0" }, props || {}));
    root.appendChild(p);
    return p;
  }
  let enabled = false;
  let pill = null;
  let anchor = null;
  let pillTrusted = false;   // pastille atteinte par une flèche RÉELLE (nav.js) depuis son dernier focus
  let session = null;
  let panel = null;
  let toastBox = null;
  let toastTimer = 0;
  let pillBox = null;   // dernier rectangle de la pastille : le panneau QR ne le recouvre jamais
  // obstacles jamais recouverts : champ, formulaire/fenêtre, consigne de nav.js, chaque contrôle visible (Continuer, liens ;
  // ceux de la fenêtre modale seulement si le champ y est), QR natif de X (« Scan to get the app ») et toute image
  // presque carrée de 48 à 400 px avec son cadre fixe. Calques plein écran (≥ moitié de l'écran) ignorés.
  const CTRL_SEL = 'a[href], button, input:not([type="hidden" i]), select, textarea, [role="button"], [role="link"], ' +
    '[tabindex]:not([tabindex="-1"])';
  const MEDIA_SEL = "canvas, img, svg, video, iframe, [role='img']";
  function rectOf(el) { const r = el.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; }
  function viewport() { return { w: document.documentElement.clientWidth || innerWidth, h: document.documentElement.clientHeight || innerHeight }; }
  function obstacles(field) {
    const vp = viewport();
    const out = [];
    const add = (el) => {
      if (!el || el.closest("[data-otvx-entry]")) { return; }
      const r = rectOf(el);
      const w = r.right - r.left;
      const h = r.bottom - r.top;
      if (w > 0 && h > 0 && r.right > 0 && r.bottom > 0 && r.left < vp.w && r.top < vp.h && w * h < vp.w * vp.h / 2) { out.push(r); }
    };
    add(field);
    add(field.form);
    add(field.closest(DIALOG_SEL));
    for (const l of Array.from(field.labels || []).slice(0, 4)) { add(l); }
    add(document.querySelector(".otvx-login-help"));   // consigne ↑ ↓ OK de nav.js (bas de l'écran)
    const modal = field.closest('[aria-modal="true"], dialog[open]');
    for (const el of Array.from((modal || document).querySelectorAll(CTRL_SEL)).slice(0, 2000)) { add(el); }
    for (const el of Array.from(document.querySelectorAll(MEDIA_SEL)).slice(0, 500)) {
      const r = rectOf(el);
      const w = r.right - r.left;
      const h = r.bottom - r.top;
      if (w < 48 || h < 48 || w > 400 || h > 400 || w > h * 1.25 || h > w * 1.25) { continue; }
      add(el);
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const p = getComputedStyle(a).position;
        if (p === "fixed" || p === "sticky") { add(a); break; }
      }
    }
    return out;
  }
  // pose `host` (déjà inséré) : taille MESURÉE après insertion, case libre (placeBox), bornée à l'écran, puis
  // rectangle RELU et revérifié ; null (host masqué) si aucune place — jamais par-dessus le formulaire ni le QR natif
  function place(host, field, kind, extra) {
    css(host, { visibility: "hidden", left: "0px", top: "0px" });
    const m = host.getBoundingClientRect();
    const w = Math.ceil(m.width);
    const h = Math.ceil(m.height);
    const vp = viewport();
    const scope = rectOf(field.closest(DIALOG_SEL) || field.form || field);
    const f = rectOf(field);
    const obs = obstacles(field).concat(extra || []);
    const pref = { left: scope.right + 32, top: kind === "qr" ? EDGE : (f.top + f.bottom - h) / 2 };
    const p = placeBox(vp.w, vp.h, w, h, scope, pref, obs);
    if (!p) { return null; }
    const left = Math.max(EDGE, Math.min(p.left, vp.w - w - EDGE));
    const top = Math.max(EDGE, Math.min(p.top, vp.h - h - EDGE));
    css(host, { left: left + "px", top: top + "px" });
    const r = rectOf(host);
    if (r.left < EDGE || r.top < EDGE || r.right > vp.w - EDGE || r.bottom > vp.h - EDGE ||
        obs.some((o) => hits(r, o, 0))) { return null; }
    css(host, { visibility: "visible" });
    return r;
  }
  function showPill() {
    if (!pill) {
      const sh = shell("pill", { background: "#1d9bf0", color: "#fff", "border-radius": "999px", padding: "14px 26px",
                                 width: "380px", "box-sizing": "border-box", "text-align": "center" });
      sh.host.setAttribute("role", "button");
      sh.host.setAttribute("tabindex", "-1");
      sh.host.setAttribute("aria-label", PILL_TEXT);
      line(sh.root, "📱 " + PILL_TEXT, { margin: "0" });
      sh.host.addEventListener("focusout", () => { pillTrusted = false; });
      pill = sh.host;
    }
    if (!pill.isConnected) { document.documentElement.appendChild(pill); }
    pillBox = place(pill, anchor, "pill");
    if (!pillBox) { hidePill(); }   // aucune place libre : pas de pastille plutôt qu'un recouvrement
  }
  function hidePill() {
    pillTrusted = false;
    if (pill && pill.isConnected) { pill.remove(); }
  }
  function sync() {
    if (session) { hidePill(); return; }
    const ae = document.activeElement;
    if (pill && ae === pill && anchor && anchor.isConnected) { return; }
    if (enabled && ae instanceof HTMLInputElement && eligible(ae)) { anchor = ae; showPill(); return; }
    anchor = null;
    hidePill();
  }
  function toast(text) {
    if (!toastBox) {
      const sh = shell("toast", { left: "50%", top: "40px", transform: "translateX(-50%)", background: "rgba(0,0,0,.9)",
                                  border: "2px solid #ffd400", "border-radius": "14px", padding: "10px 24px",
                                  "pointer-events": "none" });
      sh.host.setAttribute("aria-hidden", "true");
      toastBox = { host: sh.host, text: line(sh.root, "", { margin: "0" }) };
    }
    toastBox.text.textContent = text;
    if (!toastBox.host.isConnected) { document.documentElement.appendChild(toastBox.host); }
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.host.remove(), TOAST_MS);
  }
  // panneau QR VERTICAL, posé après mesure (place) ; s'il ne tient nulle part, QR réduit module par module jusqu'à
  // QR_CELL_MIN px (≈ 3 mm par module sur une TV de 55 po : encore lisible du canapé), sinon refus (aucun recouvrement)
  const QR_CELL_MAX = 7;
  const QR_CELL_MIN = 5;
  function showQr(text, field) {   // false = aucune place libre (générateur absent : exception, échec générique)
    if (typeof qrcode !== "function") { throw new Error("qr"); }
    const q = qrcode(0, "M");
    q.addData(text);
    q.make();
    const n = q.getModuleCount();
    for (let cell = Math.max(QR_CELL_MIN, Math.min(QR_CELL_MAX, Math.floor(400 / (n + 8)))); cell >= QR_CELL_MIN; cell--) {
      const p = qrPanel(q, n, cell);
      document.documentElement.appendChild(p.host);
      if (place(p.host, field, "qr", pillBox ? [pillBox] : [])) { panel = p; return true; }
      p.canvas.width = p.canvas.height = 0;
      p.host.remove();
    }
    return false;
  }
  function qrPanel(q, n, cell) {
    const side = cell * (n + 8);
    const sh = shell("qr", { background: "#000", border: "3px solid #ffd400", "border-radius": "20px", padding: "22px",
                             width: (Math.max(side, 300) + 50) + "px", "box-sizing": "border-box",
                             "pointer-events": "none" });
    sh.host.setAttribute("aria-hidden", "true");
    line(sh.root, PILL_TEXT, { font: "700 28px/1.3 system-ui, sans-serif" });
    const c = document.createElement("canvas");
    c.width = c.height = cell * (n + 8);
    css(c, { display: "block", margin: "10px auto", width: c.width + "px", height: c.height + "px" });
    const g = c.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = "#000";
    for (let r = 0; r < n; r++) {
      for (let k = 0; k < n; k++) { if (q.isDark(r, k)) { g.fillRect((k + 4) * cell, (r + 4) * cell, cell, cell); } }
    }
    sh.root.appendChild(c);
    line(sh.root, "Scanne avec le téléphone déjà appairé, écris ton identifiant X, puis Envoyer.",
         { font: "500 21px/1.35 system-ui, sans-serif" });
    line(sh.root, WARN, { color: "#ffd400", font: "700 21px/1.35 system-ui, sans-serif" });
    line(sh.root, "2 minutes · une touche de la télécommande annule", { color: "#bbb", font: "500 19px/1.3 system-ui" });
    return { host: sh.host, canvas: c };
  }
  function hideQr() {
    if (!panel) { return; }
    panel.canvas.width = panel.canvas.height = 0;   // pixels du QR effacés
    panel.host.remove();
    panel = null;
  }

  // ---------------------------------------------------------------- séance
  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (r) => { void chrome.runtime.lastError; resolve(r && typeof r === "object" ? r : null); });
      } catch (err) {
        resolve(null);
      }
    });
  }
  function end(s, text) {
    if (session !== s) { return; }
    clearTimeout(s.timer);
    clearTimeout(s.poll);
    s.key = s.secret = s.field = null;
    session = null;
    hideQr();
    if (text) { toast(text); }
    setTimeout(sync, 0);   // pastille de nouveau proposée si le champ reste au focus
  }
  function cancel(why) {
    const s = session;
    if (!s) { return; }
    if (s.sid) { send({ type: "xentry", op: "cancel", sid: s.sid }); }
    end(s, why === "expired" ? MSG.expired : MSG.cancelled);
  }
  async function start() {
    const field = anchor;
    hidePill();
    try { field.focus({ preventScroll: true }); } catch (err) { /* retiré */ }
    if (!field || !eligible(field)) { toast(MSG.ineligible); return; }
    const s = { field: field, href: location.href, key: null, secret: null, sid: null, poll: 0, timer: 0 };
    session = s;
    s.timer = setTimeout(() => cancel("expired"), TTL_MS);   // minuterie monotone (indépendante de l'horloge murale)
    try {
      const pair = await crypto.subtle.generateKey({ name: "RSA-OAEP", modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, false, ["encrypt", "decrypt"]);
      if (pair.privateKey.extractable !== false) { throw new Error("extractable"); }
      const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
      if (session !== s) { return; }
      if (jwk.e !== "AQAB" || !CT_RE.test(jwk.n)) { throw new Error("key"); }
      s.key = pair.privateKey;
      const fp = b64uEnc(new Uint8Array(await crypto.subtle.digest("SHA-256", b64uDec(jwk.n))));
      s.secret = b64uEnc(crypto.getRandomValues(new Uint8Array(16)));
      const r = await send({ type: "xentry", op: "start", n: jwk.n, e: jwk.e });
      const rsid = r && r.ok === true && typeof r.sid === "string" && SID_RE.test(r.sid) ? r.sid : null;
      const m = rsid && typeof r.url === "string" ? URL_RE.exec(r.url) : null;
      if (session !== s || !m || m[1] !== rsid) {   // réponse écartée : la séance créée au service est annulée
        if (rsid) { send({ type: "xentry", op: "cancel", sid: rsid }); }
        if (session !== s) { return; }
        throw new Error("start");
      }
      s.sid = rsid;
      if (!showQr(r.url + "#" + s.secret + "." + fp, field)) { throw new Error("room"); }
      s.poll = setTimeout(poll, POLL_MS);
    } catch (err) {
      if (s.sid) { send({ type: "xentry", op: "cancel", sid: s.sid }); }
      end(s, err && err.message === "room" ? MSG.room : MSG.fail);
    }
  }
  async function poll() {
    const s = session;
    if (!s || !s.sid) { return; }
    if (location.href !== s.href) { cancel("navigation"); return; }
    if (document.activeElement !== s.field) { cancel("focus"); return; }
    const r = await send({ type: "xentry", op: "poll", sid: s.sid });
    if (session !== s) { return; }
    if (r && r.ok === true && r.state === "submitted" && CT_RE.test(r.ct)) { deliver(s, r.ct); return; }
    if (r && r.ok === true && !/^(waiting|claimed|delivering)$/.test(r.state)) {   // annulé/expiré côté téléphone
      end(s, r.state === "expired" ? MSG.expired : r.state === "cancelled" ? MSG.cancelled : MSG.fail);
      return;
    }
    s.poll = setTimeout(poll, POLL_MS);   // en attente (ou service momentanément injoignable : TTL borne tout)
  }
  async function deliver(s, ct) {
    let pt = null;
    try { pt = new Uint8Array(await crypto.subtle.decrypt({ name: "RSA-OAEP" }, s.key, b64uDec(ct))); } catch (err) { pt = null; }
    s.key = null;
    if (session !== s) { if (pt) { pt.fill(0); } return; }
    let ident = null;
    if (pt) {
      try { ident = identOf(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(pt)), s.sid, s.secret); } catch (err) {
        ident = null;
      }
      pt.fill(0);
    }
    const field = s.field;
    const ok = fillOk({ ident: ident, same: document.activeElement === field, docFocus: document.hasFocus(),
                        href: location.href, startHref: s.href, eligible: eligible(field) });
    if (ok) {   // setter natif, UN « input », UN « change » ; jamais de clic ni d'envoi
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, ident);
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const result = ok ? "filled" : ident === null ? "rejected" : "refused";
    ident = null;   // référence lâchée (chaîne JS : récupérée par le ramasse-miettes, jamais réécrite en place)
    send({ type: "xentry", op: "done", sid: s.sid, result: result });
    end(s, MSG[result]);
  }
  function okKey(e) {
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    return k ? (k === "Enter" || k === "Select" || k === "Accept") : e.keyCode === 13;
  }
  // nav.js : touche RÉELLE (isTrusted vérifié par nav.js, et ici) -> true si consommée
  function onKey(e) {
    if (!e || e.isTrusted !== true) { return false; }
    if (session) {   // toute touche réelle annule (Alex reprend la main) ; consommée
      e.preventDefault();
      e.stopImmediatePropagation();
      if (!e.repeat) { cancel("key"); }
      return true;
    }
    if (!pill || document.activeElement !== pill || !okKey(e)) { return false; }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (!e.repeat && pillTrusted && anchor && enabled) { start(); }
    return true;
  }
  function augment(list) {   // parcours ↑ ↓ de nav.js : pastille juste après son champ
    if (!enabled || session || !pill || !pill.isConnected || !anchor) { return list; }
    const i = list.indexOf(anchor);
    return i < 0 ? list : list.slice(0, i + 1).concat([pill], list.slice(i + 1));
  }
  try {
    Object.defineProperty(window, "__otvxEntry", {   // monde isolé de l'extension seulement (nav.js) ; figé
      value: Object.freeze({
        augment: (list) => augment(list),
        navFocus: (el) => { pillTrusted = !!pill && el === pill && document.activeElement === pill; },
        key: (e) => onKey(e)
      }),
      writable: false, configurable: false, enumerable: false
    });
  } catch (err) { return; }   // déjà posé (réinjection) : une seule instance
  document.addEventListener("focusin", () => {
    if (session && document.activeElement !== session.field) { cancel("focus"); }
    setTimeout(sync, 0);
  }, true);
  document.addEventListener("focusout", () => {
    setTimeout(() => {
      if (session && document.activeElement !== session.field) { cancel("focus"); }
      sync();
    }, 0);
  }, true);
  window.addEventListener("pointerdown", (e) => { if (e.isTrusted && session) { cancel("pointer"); } }, true);
  window.addEventListener("pagehide", () => cancel("navigation"), true);
  window.addEventListener("popstate", () => cancel("navigation"), true);
  if (window.navigation && typeof window.navigation.addEventListener === "function") {
    window.navigation.addEventListener("navigate", () => cancel("navigation"));
  }
  send({ type: "xentry", op: "enabled" }).then((r) => { enabled = !!r && r.ok === true; sync(); });
})();
