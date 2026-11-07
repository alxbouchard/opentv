/* OpenTV — identifiant X depuis le téléphone DÉJÀ appairé (xentry, 0.7.10). Même origine seulement (CSP), aucun
   stockage. Le QR de la TV porte, dans le FRAGMENT d'URL (jamais envoyé au service), un secret de séance et l'empreinte
   SHA-256 de la clé publique tirée par l'extension du kiosque. Ce script :
   1. lit puis EFFACE le fragment de la barre d'adresse et de l'historique (history.replaceState) ;
   2. réclame la séance (POST /r/x/claim : cookie appairé, Origin, CSRF, en-tête dédié) et relit la clé publique ;
   3. refuse toute clé dont l'empreinte diffère du QR, dont e ≠ 65537 ou qui n'est pas un module de 2048 bits exactement ;
   4. chiffre {v, s, f, t} en RSA-OAEP SHA-256 (ui/muse.js : JavaScript pur, crypto.subtle n'existe pas en HTTP LAN ;
      aléa de crypto.getRandomValues) : chiffré de 256 octets, quelle que soit la longueur de l'identifiant ;
   5. envoie UNE fois, vide le champ aussitôt, puis suit seulement l'issue (livré / refusé / annulé).
   Aucun repli en clair : au moindre manque (BigInt, aléa, auto-test, fragment, clé), rien ne part. La page ne clique
   jamais rien sur X et n'ouvre rien : la suite (Suivant, passkey) se fait sur la TV. Syntaxe ES5. */
(function () {
  "use strict";
  var FRAG_RE = /^([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$/;
  var SID_RE = /^[A-Za-z0-9_-]{22}$/;
  var N_RE = /^[A-Za-z0-9_-]{342}$/;
  var ID_MAX = 100;       // points de code (nom d'utilisateur, courriel ou téléphone)
  var DOC_MAX = 190;      // octets : plafond RSA-OAEP-2048/SHA-256 (256 - 2·32 - 2)
  var CT_LEN = 342;       // 256 octets en base64url : taille FIXE du chiffré
  // contrôles, sauts de ligne, bidi et largeur nulle : jamais envoyés (l'extension les retire aussi après déchiffrement)
  var STRIP_RE = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
  var CODE_RE = /^[0-9\s.-]{4,9}$/;   // ressemble à un code de vérification : jamais ici

  function b64uEnc(bytes) {
    var s = "", i;
    for (i = 0; i < bytes.length; i++) { s += String.fromCharCode(bytes[i]); }
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64uDec(s) {
    var bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), out = [], i;
    for (i = 0; i < bin.length; i++) { out.push(bin.charCodeAt(i)); }
    return out;
  }
  function parseFragment(h) {
    var m = FRAG_RE.exec(typeof h === "string" ? h : "");
    return m ? { f: m[1], fp: m[2] } : null;
  }
  // clé de la séance acceptée SEULEMENT si : e = 65537, n = 256 octets (bit fort à 1), empreinte = celle du QR
  function verifyKey(seal, n, e, fp) {
    if (e !== "AQAB" || typeof n !== "string" || !N_RE.test(n) || typeof fp !== "string") { return false; }
    var nb;
    try { nb = b64uDec(n); } catch (x) { return false; }
    if (nb.length !== 256 || nb[0] < 0x80 || b64uEnc(nb) !== n) { return false; }
    return b64uEnc(seal.sha256(nb)) === fp;
  }
  function clean(v) {
    return String(v === undefined || v === null ? "" : v).replace(STRIP_RE, "").replace(/^[\s\u00a0]+|[\s\u00a0]+$/g, "");
  }
  function points(v) { return v.replace(/[\ud800-\udbff][\udc00-\udfff]/g, "_").length; }
  // "" si acceptable ; sinon le motif du refus (jamais envoyé)
  function refusal(v) {
    if (!v) { return "empty"; }
    if (points(v) > ID_MAX) { return "long"; }
    if (CODE_RE.test(v)) { return "code"; }
    return "";
  }
  function makeCt(seal, n, sid, f, ident, rnd) {
    var doc = seal.utf8(JSON.stringify({ v: 1, s: sid, f: f, t: ident }));
    if (doc.length > DOC_MAX) { throw new Error("size"); }
    var ct = seal.seal(n, "AQAB", doc, rnd);
    if (typeof ct !== "string" || ct.length !== CT_LEN) { throw new Error("size"); }
    return ct;
  }
  function secureRandom(n) {
    var c = window.crypto || window.msCrypto;
    if (!c || typeof c.getRandomValues !== "function") { throw new Error("rnd"); }
    var a = new Uint8Array(n), out = [], i;
    c.getRandomValues(a);
    for (i = 0; i < n; i++) { out.push(a[i]); }
    return out;
  }
  if (typeof window !== "undefined") {
    window.OpenTVXPhone = { parseFragment: parseFragment, verifyKey: verifyKey, clean: clean, refusal: refusal,
                            makeCt: makeCt };
  }
  if (typeof document === "undefined") { return; }

  var $ = function (id) { return document.getElementById(id); };
  var main = $("xentry");
  if (!main || !main.getAttribute("data-s")) { return; }
  var loc = window.location;
  var raw = (loc.hash || "").slice(1);
  if (loc.hash) {   // fragment effacé tout de suite : ni barre d'adresse, ni historique, ni capture d'écran
    try { window.history.replaceState(null, "", loc.pathname + loc.search); } catch (x) { loc.hash = ""; }
  }
  var Q = parseFragment(raw);
  raw = null;
  var S = main.getAttribute("data-s"), C = main.getAttribute("data-c") || "", N = null, busy = false, over = false;
  var seal = window.OpenTVMuseSeal;
  var form = $("xform"), inp = $("xid"), send = $("xsend"), cancel = $("xcancel"), err = $("xerr"), st = $("xstatus");
  function say(t) { st.textContent = t; }
  function showErr(t) { err.textContent = t; err.hidden = !t; }
  function finish(t) {   // fin de séance : plus aucun champ, plus aucun envoi possible
    over = true; N = null; Q = null;
    form.hidden = true; inp.value = ""; cancel.hidden = true;
    say(t);
  }
  function post(path, body) {
    return fetch(path, { method: "POST", cache: "no-store", credentials: "same-origin", referrerPolicy: "same-origin",
                         headers: { "Content-Type": "application/json", "X-OpenTV-Remote": "x" },
                         body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j || {} }; });
      });
  }
  if (!Q || !SID_RE.test(S) || !seal || typeof seal.selfTest !== "function" || !seal.selfTest()) {
    showErr("Ce lien n'est pas complet ou ce navigateur ne peut pas chiffrer : rescanne le QR affiché sur la TV avec " +
            "l'appareil photo. Rien n'a été envoyé.");
    return;
  }
  function stop(why) {
    post("/r/x/cancel", { s: S, c: C }).then(function () { return null; }, function () { return null; });
    finish(why);
  }
  cancel.hidden = false;
  cancel.addEventListener("click", function () { stop("Annulé. Rien n'a été saisi sur la TV."); });
  var FINAL = {
    delivered: "C'est fait : l'identifiant est dans le champ de la TV. Vérifie-le à l'écran, puis continue sur la " +
               "TV (Suivant, puis ta clé d'accès).",
    refused: "La TV n'a rien saisi : le champ n'était plus sélectionné. Recommence depuis la TV.",
    cancelled: "Séance annulée sur la TV. Rien n'a été saisi.",
    expired: "Séance expirée (2 minutes). Rien n'a été saisi.",
    gone: "Séance terminée. Rien n'a été saisi."
  };
  var polls = 0, polling = false;
  function poll() {   // issue de la séance (TV : annulée, expirée, livrée…) — un seul suivi à la fois
    if (over) { return; }
    polls += 1;
    if (polls > 90) { finish(FINAL.gone); return; }
    fetch("/r/x/status?s=" + encodeURIComponent(S), { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && FINAL[j.state]) { finish(FINAL[j.state]); return; }
        setTimeout(poll, 1500);
      }, function () { setTimeout(poll, 3000); });
  }
  function startPoll() {
    if (!polling) { polling = true; setTimeout(poll, 1500); }
  }
  post("/r/x/claim", { s: S, c: C }).then(function (r) {
    if (over) { return; }
    if (r.status !== 200 || !r.j.ok) {
      finish(r.j.error === "claimed" ? "Un autre téléphone a déjà pris cette saisie." :
             "Ce QR n'est plus valable : sur la TV, choisis de nouveau « Saisir avec le téléphone ».");
      return;
    }
    if (!verifyKey(seal, r.j.n, r.j.e, Q.fp)) {   // la clé ne vient pas de la TV qui a affiché ce QR
      showErr("Clé inattendue : ce QR ne correspond pas à la clé reçue. Rien n'a été envoyé.");
      stop("Séance annulée par sécurité.");
      return;
    }
    N = r.j.n;
    form.hidden = false;
    startPoll();
    try { inp.focus(); } catch (x) { /* clavier ouvert par un toucher */ }
  }, function () { showErr("La boîte OpenTV ne répond pas : ce téléphone doit être sur le même Wi-Fi que la TV."); });
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (busy || over || !N) { return; }
    var v = clean(inp.value);
    inp.value = "";   // l'identifiant quitte le champ tout de suite
    var why = refusal(v);
    if (why) {
      v = null;
      showErr(why === "code" ? "Ceci ressemble à un code : jamais de code ici, seulement ton identifiant." :
              why === "long" ? "Trop long pour un identifiant X (100 caractères au plus)." :
              "Écris ton nom d'utilisateur, ton courriel ou ton numéro de téléphone.");
      return;
    }
    var ct = null;
    try { ct = makeCt(seal, N, S, Q.f, v, secureRandom); } catch (x) { ct = null; }
    v = null;
    if (!ct) { showErr("Ce navigateur ne peut pas chiffrer l'identifiant : rien n'a été envoyé."); return; }
    busy = true; send.disabled = true; showErr("");
    post("/r/x/submit", { s: S, c: C, ct: ct }).then(function (r) {
      ct = null;
      if (r.status === 200) {
        form.hidden = true; N = null;
        say("Envoyé chiffré. Regarde la TV…");
        startPoll();
        return;
      }
      finish(r.j.error === "already_submitted" ? "Déjà envoyé." : "L'envoi n'a pas abouti. Rien n'a été saisi.");
    }, function () {
      ct = null; busy = false; send.disabled = false;
      showErr("La boîte OpenTV ne répond pas : ce téléphone doit être sur le même Wi-Fi que la TV.");
    });
  });
})();
