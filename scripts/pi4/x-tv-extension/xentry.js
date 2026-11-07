/* OpenTV X TV Mode — « Saisir avec le téléphone » (0.7.10, OPENTV_X_PHONE_ENTRY ; défaut : absent).
 *
 * But UNIQUE : placer UN identifiant X (nom d'utilisateur, courriel, téléphone) dans le champ de connexion ÉLIGIBLE au
 * focus, sans clavier physique ; Alex continue ensuite lui-même (Suivant, passkey native). Jamais un mot de passe, un
 * code, un NIP, une passkey, un code de secours ni un paiement ; jamais de clic, d'envoi de formulaire ni d'action.
 *
 * Monde ISOLÉ de l'extension (la page X ne voit ni ces variables, ni la clé, ni le QR : racines fantômes FERMÉES).
 * - Activation : pastille « Saisir avec le téléphone » insérée juste après le champ éligible dans le parcours ↑ ↓ de
 *   nav.js, atteinte par une flèche RÉELLE (nav.js signale navFocus) puis UN OK réel ; un focus posé par la page, une
 *   touche ou un clic synthétiques n'arment jamais rien. Drapeau du service relu sur /tv (tvkey.js -> background.js).
 * - Champ éligible (porte, bloc <gate> exécuté tel quel par tests/x_entry_harness.js) : cadre principal, x.com /
 *   twitter.com en https, URL de connexion seulement (/i/flow/login, /login, /i/jf/onboarding/web?mode=login), champ
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
 *   « input » et UN « change », rien d'autre. Puis tout est effacé.
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
    refused: "Rien n'a été saisi : le champ a changé.",
    cancelled: "Saisie par téléphone annulée.",
    expired: "Saisie par téléphone expirée (2 minutes).",
    fail: "Saisie par téléphone indisponible pour l'instant.",
    ineligible: "Ce champ n'accepte pas la saisie par téléphone."
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
  function loginUrl(href) {
    let u = null;
    try { u = new URL(href); } catch (err) { return false; }
    if (u.protocol !== "https:" || !X_HOST_RE.test(u.hostname) || u.port || u.username || u.password) { return false; }
    const path = u.pathname.replace(/\/+$/, "") || "/";
    const modes = u.searchParams.getAll("mode");
    if (u.searchParams.has("start")) { return false; }   // « start=phone… » : inscription, jamais une connexion
    if (LOGIN_PATHS.has(path)) { return modes.every((m) => m === "login"); }
    return path === ONBOARDING_PATH && modes.length === 1 && modes[0] === "login";
  }
  // f : faits relevés sur le champ (attributs seulement, JAMAIS la valeur) -> éligible ?
  function factsOk(f) {
    if (!f || f.top !== true || f.focused !== true || f.visible !== true || f.tag !== "INPUT") { return false; }
    if (f.disabled || f.readonly || !loginUrl(f.url) || !TYPE_OK.has(f.type)) { return false; }
    const toks = String(f.ac).toLowerCase().trim().split(/\s+/).filter(Boolean);
    if (toks.length === 2 && toks[1] === "webauthn") { toks.pop(); }
    if (toks.length !== 1 || !AC_OK.has(toks[0])) { return false; }
    if (/^(numeric|decimal)$/i.test(String(f.inputmode).trim())) { return false; }
    if (f.maxlength > 0 && f.maxlength <= 8) { return false; }   // code court
    if (f.pattern || f.danger) { return false; }                  // motif imposé, mot de passe/code à côté
    return Array.isArray(f.texts) && !f.texts.some((t) => DENY_RE.test(String(t)));
  }
  // document déchiffré -> identifiant nettoyé et borné, ou null (séance, secret, champs exacts)
  function identOf(doc, sid, secret) {
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) { return null; }
    if (Object.keys(doc).sort().join(",") !== "f,s,t,v" || doc.v !== 1 || typeof doc.t !== "string") { return null; }
    if (typeof sid !== "string" || doc.s !== sid || typeof secret !== "string" || !sameText(doc.f, secret)) { return null; }
    const t = doc.t.replace(STRIP_RE, "").trim();
    return t && Array.from(t).length <= ID_MAX ? t : null;
  }
  function sameText(a, b) {   // comparaison sans sortie anticipée
    if (typeof a !== "string" || a.length !== b.length) { return false; }
    let d = 0;
    for (let i = 0; i < a.length; i++) { d |= a.charCodeAt(i) ^ b.charCodeAt(i); }
    return d === 0;
  }
  // </gate>

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
    return {
      top: window.top === window, focused: document.activeElement === el, visible: visible(el), tag: el.tagName,
      disabled: el.disabled, readonly: el.readOnly, url: location.href,
      type: (el.getAttribute("type") || "text").trim().toLowerCase(), ac: el.getAttribute("autocomplete") || "",
      inputmode: el.getAttribute("inputmode") || "", maxlength: el.maxLength, pattern: el.getAttribute("pattern") || "",
      danger: !!scope.querySelector(DANGER_SEL), texts: texts
    };
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
  function placeNear(el, w) {   // à gauche de la fenêtre de X (jamais sur le champ, ni sur « Suivant »)
    const box = (el.closest(DIALOG_SEL) || el).getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const left = box.left - w - 32 >= 16 ? box.left - w - 32 : 16;
    return { left: Math.round(left) + "px", top: Math.round(Math.max(16, Math.min(r.top, innerHeight - 120))) + "px" };
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
    css(pill, placeNear(anchor, 380));
    if (!pill.isConnected) { document.documentElement.appendChild(pill); }
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
  function showQr(text, field) {
    if (typeof qrcode !== "function") { return false; }
    const q = qrcode(0, "M");
    q.addData(text);
    q.make();
    const sh = shell("qr", Object.assign({ background: "#000", border: "3px solid #ffd400", "border-radius": "20px",
                                           padding: "22px", width: "460px", "box-sizing": "border-box",
                                           "pointer-events": "none" }, placeNear(field, 460)));
    sh.host.setAttribute("aria-hidden", "true");
    line(sh.root, PILL_TEXT, { font: "700 28px/1.3 system-ui, sans-serif" });
    const n = q.getModuleCount();
    const cell = Math.max(3, Math.floor(400 / (n + 8)));
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
    document.documentElement.appendChild(sh.host);
    panel = { host: sh.host, canvas: c };
    return true;
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
      const m = r && r.ok === true && typeof r.url === "string" ? URL_RE.exec(r.url) : null;
      if (session !== s) {
        if (m && SID_RE.test(r.sid)) { send({ type: "xentry", op: "cancel", sid: r.sid }); }
        return;
      }
      if (!m || m[1] !== r.sid) { throw new Error("start"); }
      s.sid = r.sid;
      if (!showQr(r.url + "#" + s.secret + "." + fp, field)) { throw new Error("qr"); }
      s.poll = setTimeout(poll, POLL_MS);
    } catch (err) {
      if (s.sid) { send({ type: "xentry", op: "cancel", sid: s.sid }); }
      end(s, MSG.fail);
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
      end(s, r.state === "expired" ? MSG.expired : MSG.cancelled);
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
    const ok = ident !== null && document.activeElement === field && document.hasFocus() && location.href === s.href &&
      eligible(field);
    if (ok) {   // setter natif, UN « input », UN « change » ; jamais de clic ni d'envoi
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(field, ident);
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    }
    ident = null;
    send({ type: "xentry", op: "done", sid: s.sid, result: ok ? "filled" : "refused" });
    end(s, ok ? MSG.filled : MSG.refused);
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
