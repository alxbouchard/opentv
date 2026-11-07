/* OpenTV — identifiant X depuis le téléphone DÉJÀ appairé (xentry, 0.7.10). Même origine seulement (CSP), aucun
   stockage. Le QR de la TV porte, dans le FRAGMENT d'URL (jamais envoyé au service), un secret de séance et l'empreinte
   SHA-256 de la clé publique tirée par l'extension du kiosque. Ce script :
   1. lit puis EFFACE le fragment de la barre d'adresse et de l'historique (history.replaceState) ;
   2. réclame la séance (POST /r/x/claim : cookie appairé, Origin, CSRF, en-tête dédié) et relit la clé publique ;
   3. refuse toute clé dont l'empreinte diffère du QR, dont e ≠ 65537 ou qui n'est pas un module de 2048 bits exactement ;
   4. chiffre {v, s, f, t} en RSA-OAEP SHA-256 (ui/muse.js : JavaScript pur, crypto.subtle n'existe pas en HTTP LAN ;
      aléa de crypto.getRandomValues) : chiffré de 256 octets, quelle que soit la longueur de l'identifiant ;
   5. envoie UNE fois, vide le champ aussitôt, puis suit seulement l'issue (livré / refusé / rejeté / annulé / inconnue).
   Annuler : seulement AVANT l'envoi accepté (caché et désactivé dès le 200). Textes tirés de la réponse du service,
   jamais d'un « rien n'a été saisi » présumé : réponse perdue, 410 générique, 5xx -> « incertain », issue suivie.
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
  // « Rien n'a été saisi » SEULEMENT quand le service confirme une fin AVANT la remise du chiffré à la TV
  var FINAL = {
    delivered: "C'est fait : l'identifiant est dans le champ de la TV. Vérifie-le à l'écran, puis continue sur la " +
               "TV (Suivant, puis ta clé d'accès).",
    refused: "La TV signale n'avoir rien saisi : le champ n'acceptait plus la saisie (il a changé ou n'était plus " +
             "sélectionné). Recommence depuis la TV.",
    rejected: "La TV a rejeté l'envoi (illisible, altéré, QR d'une autre séance, ou ressemble à un code) et signale " +
              "n'avoir rien saisi. Recommence depuis la TV.",
    cancelled: "Séance annulée avant la remise à la TV. Rien n'a été saisi.",
    expired: "Séance expirée (2 minutes) avant la remise à la TV. Rien n'a été saisi.",
    unknown: "Issue incertaine : la séance s'est fermée après la remise à la TV ; l'identifiant a pu être saisi. " +
             "Vérifie le champ sur la TV.",
    gone: "Issue incertaine (séance terminée) : vérifie le champ sur la TV."
  };
  function fin(state) { return Object.prototype.hasOwnProperty.call(FINAL, state) ? FINAL[state] : null; }
  var TOO_LATE = "Trop tard pour annuler : l'identifiant chiffré est déjà parti vers la TV. Regarde la TV ; l'issue " +
                 "s'affiche ici.";
  // réponse de POST /r/x/cancel -> { end, text } : « rien n'a été saisi » SEULEMENT si le service le confirme
  function cancelOutcome(status, j, cancelled) {
    j = j || {};
    if (status === 200 && j.ok === true && j.state === "cancelled") { return { end: true, text: cancelled }; }
    if (j.error === "over" && fin(j.state)) { return { end: true, text: fin(j.state) }; }
    if (j.error === "too_late") { return { end: false, text: TOO_LATE }; }
    if (j.error === "claimed") { return { end: true, text: "Un autre téléphone a déjà pris cette saisie." }; }
    return { end: false, text: "Annulation non confirmée (la boîte OpenTV ne répond pas) : regarde la TV ; l'issue " +
                               "s'affiche ici." };
  }
  var UNSURE = "Envoi incertain (pas de réponse claire de la boîte OpenTV) : l'identifiant a pu partir vers la TV. " +
               "Vérifie le champ sur la TV ; l'issue s'affiche ici dès qu'elle est connue.";
  var PRE = /^(bad_ct|claimed|not_claimed|csrf|origin|not_paired|bad_request|bad_json|content_type|too_large)$/;
  // réponse de POST /r/x/submit (status 0 : perdue) -> { sent | end, text } ; « rien n'a été saisi » SEULEMENT si le
  // service refuse CET envoi avant toute remise ET qu'aucun envoi précédent n'est incertain (unsure)
  function submitOutcome(status, j, unsure) {
    j = j || {};
    if (status === 200 && j.ok === true) { return { sent: true, text: "Envoyé chiffré. Regarde la TV…" }; }
    if (status === 409 && j.error === "already_submitted") { return { sent: true, text: "Déjà envoyé. Regarde la TV…" }; }
    if (j.error === "over" && fin(j.state)) { return { end: true, text: fin(j.state) }; }
    if (!unsure && status >= 400 && status < 500 && PRE.test(j.error)) {
      return { end: true, text: "Envoi refusé par la boîte OpenTV avant la TV. Rien n'a été saisi." };
    }
    return { text: UNSURE };   // perte réseau, 410 générique, 5xx, réponse illisible : jamais présumé
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
                            makeCt: makeCt, cancelOutcome: cancelOutcome, submitOutcome: submitOutcome };
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
  var sent = false, stopping = false, unsure = false;   // sent : envoi accepté — Annuler n'est plus jamais offert
  var seal = window.OpenTVMuseSeal;
  var form = $("xform"), inp = $("xid"), send = $("xsend"), cancel = $("xcancel"), err = $("xerr"), st = $("xstatus");
  function say(t) { st.textContent = t; }
  function showErr(t) { err.textContent = t; err.hidden = !t; }
  function finish(t) {   // fin de séance : plus aucun champ, plus aucun envoi possible
    over = true; N = null; Q = null;
    form.hidden = true; inp.value = ""; cancel.hidden = true; cancel.disabled = true;
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
  function settle(o) {   // issue d'une demande d'annulation, telle que rendue par le service
    if (over) { return; }
    if (o.end) { finish(o.text); return; }
    say(o.text);
    if (!sent) { cancel.disabled = false; }
    startPoll();
  }
  function stop(cancelled) {   // jamais après l'envoi accepté ; le texte final vient de la réponse du service
    if (over || sent || stopping) { return; }
    stopping = true; cancel.disabled = true;
    say("Annulation…");
    post("/r/x/cancel", { s: S, c: C }).then(function (r) {
      stopping = false; settle(cancelOutcome(r.status, r.j, cancelled));
    }, function () { stopping = false; settle(cancelOutcome(0, null, cancelled)); });
  }
  cancel.hidden = false;
  cancel.addEventListener("click", function () { stop("Annulé. Rien n'a été saisi sur la TV."); });
  var polls = 0, polling = false;
  function poll() {   // issue de la séance (TV : annulée, expirée, livrée…) — un seul suivi à la fois
    if (over) { return; }
    polls += 1;
    if (polls > 90) { finish(FINAL.gone); return; }
    fetch("/r/x/status?s=" + encodeURIComponent(S), { cache: "no-store", credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && fin(j.state)) { finish(fin(j.state)); return; }
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
      stop("Séance annulée par sécurité. Rien n'a été envoyé.");
      return;
    }
    N = r.j.n;
    form.hidden = false;
    startPoll();
    try { inp.focus(); } catch (x) { /* clavier ouvert par un toucher */ }
  }, function () { showErr("La boîte OpenTV ne répond pas : ce téléphone doit être sur le même Wi-Fi que la TV."); });
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (busy || over || stopping || !N) { return; }
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
      ct = null; submitted(submitOutcome(r.status, r.j, unsure));
    }, function () { ct = null; submitted(submitOutcome(0, null, unsure)); });
  });
  function submitted(o) {
    busy = false;
    if (over) { return; }   // issue déjà connue (réponse d'Annuler arrivée d'abord) : jamais écrasée
    if (o.end) { finish(o.text); return; }
    if (o.sent) {   // envoi accepté (ou précédent accepté, réponse perdue) : issue suivie, jamais présumée
      sent = true; cancel.hidden = true; cancel.disabled = true;   // plus aucune annulation : le chiffré est parti
      form.hidden = true; N = null;
    } else {   // issue inconnue : suivie au service ; un renvoi apprendra l'issue réelle (410 over / 409)
      unsure = true; send.disabled = false;
    }
    say(o.text);
    startPoll();
  }
})();
