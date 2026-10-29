/* OpenTV — « Connecter Muse » sur le téléphone. Même origine seulement (CSP), aucun stockage, aucune trace.
   Le jeton SDK Muse est chiffré ICI (RSA-OAEP SHA-256, clé publique de la séance fournie par la page) avec le secret
   du QR (fragment d'URL, jamais envoyé en clair) avant d'être envoyé en JSON. WebCrypto (SubtleCrypto) n'existe pas
   sur http://<adresse locale> : SHA-256 et RSA sont calculés en JavaScript (BigInt), l'aléa vient de
   crypto.getRandomValues. Au moindre manque (pas de BigInt, pas d'aléa sûr, auto-test raté, secret du QR absent),
   le champ reste caché : rien ne peut partir (échec fermé). Syntaxe ES5 (BigInt appelé comme fonction). */
(function () {
  "use strict";
  var K = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
           0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
           0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
           0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
           0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
           0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
           0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
           0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];
  var TOKEN_RE = /^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/;
  var SECRET_RE = /^[A-Za-z0-9_-]{22}$/;

  function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }
  function sha256(msg) {   // msg : tableau d'octets -> tableau de 32 octets
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var m = msg.slice(), l = msg.length, i, t, w = new Array(64);
    m.push(0x80);
    while (m.length % 64 !== 56) { m.push(0); }
    var hi = Math.floor(l / 0x20000000), lo = (l * 8) >>> 0;
    m.push((hi >>> 24) & 255, (hi >>> 16) & 255, (hi >>> 8) & 255, hi & 255,
           (lo >>> 24) & 255, (lo >>> 16) & 255, (lo >>> 8) & 255, lo & 255);
    for (i = 0; i < m.length; i += 64) {
      for (t = 0; t < 16; t++) {
        w[t] = (m[i + 4 * t] << 24) | (m[i + 4 * t + 1] << 16) | (m[i + 4 * t + 2] << 8) | m[i + 4 * t + 3];
      }
      for (t = 16; t < 64; t++) {
        var s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
        var s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (t = 0; t < 64; t++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25), ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[t] + w[t]) | 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22), maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0; H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
    }
    var out = [];
    for (i = 0; i < 8; i++) { out.push((H[i] >>> 24) & 255, (H[i] >>> 16) & 255, (H[i] >>> 8) & 255, H[i] & 255); }
    return out;
  }
  function hex(bytes) {
    var s = "", i;
    for (i = 0; i < bytes.length; i++) { s += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16); }
    return s;
  }
  function mgf1(seed, len) {
    var out = [], i = 0;
    while (out.length < len) {
      out = out.concat(sha256(seed.concat([(i >>> 24) & 255, (i >>> 16) & 255, (i >>> 8) & 255, i & 255])));
      i += 1;
    }
    return out.slice(0, len);
  }
  function xor(a, b) {
    var out = [], i;
    for (i = 0; i < a.length; i++) { out.push(a[i] ^ b[i]); }
    return out;
  }
  function b64uDec(s) {
    if (typeof s !== "string" || !/^[A-Za-z0-9_-]+$/.test(s)) { throw new Error("b64"); }
    var bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), out = [], i;
    for (i = 0; i < bin.length; i++) { out.push(bin.charCodeAt(i)); }
    return out;
  }
  function b64uEnc(bytes) {
    var s = "", i;
    for (i = 0; i < bytes.length; i++) { s += String.fromCharCode(bytes[i]); }
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function big(bytes) { return BigInt("0x" + (bytes.length ? hex(bytes) : "0")); }
  function modpow(b, e, m) {
    var r = BigInt(1), zero = BigInt(0), two = BigInt(2), one = BigInt(1);
    b = b % m;
    while (e > zero) {
      if (e % two === one) { r = (r * b) % m; }
      e = e / two;
      b = (b * b) % m;
    }
    return r;
  }
  function utf8(s) {
    var bin = unescape(encodeURIComponent(s)), out = [], i;
    for (i = 0; i < bin.length; i++) { out.push(bin.charCodeAt(i)); }
    return out;
  }
  // RSA-OAEP (SHA-256, MGF1-SHA-256, étiquette vide) : même calcul que opentv_pi/museseal.encrypt
  function seal(nB64, eB64, msg, rnd) {
    var n = b64uDec(nB64), e = b64uDec(eB64), k = n.length;
    if (k < 256 || k > 512 || msg.length > k - 66) { throw new Error("size"); }
    var ps = [], i;
    for (i = 0; i < k - msg.length - 66; i++) { ps.push(0); }
    var db = sha256([]).concat(ps, [1], msg), seed = rnd(32);
    if (seed.length !== 32) { throw new Error("rnd"); }
    var maskedDb = xor(db, mgf1(seed, k - 33)), maskedSeed = xor(seed, mgf1(maskedDb, 32));
    var c = modpow(big([0].concat(maskedSeed, maskedDb)), big(e), big(n)).toString(16);
    while (c.length < 2 * k) { c = "0" + c; }
    var out = [];
    for (i = 0; i < 2 * k; i += 2) { out.push(parseInt(c.substr(i, 2), 16)); }
    return b64uEnc(out);
  }
  function secureRandom(n) {
    var c = window.crypto || window.msCrypto;
    if (!c || typeof c.getRandomValues !== "function") { throw new Error("rnd"); }
    var a = new Uint8Array(n), out = [], i;
    c.getRandomValues(a);
    for (i = 0; i < n; i++) { out.push(a[i]); }
    return out;
  }
  function selfTest() {
    try {
      if (typeof BigInt !== "function" || typeof Uint8Array !== "function") { return false; }
      if (hex(sha256(utf8("abc"))) !== "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") { return false; }
      if (modpow(BigInt(4), BigInt(13), BigInt(497)) !== BigInt(445)) { return false; }
      return secureRandom(16).length === 16;
    } catch (e) { return false; }
  }
  if (typeof window !== "undefined") { window.OpenTVMuseSeal = { sha256: sha256, seal: seal, utf8: utf8, selfTest: selfTest }; }
  if (typeof document === "undefined") { return; }

  var $ = function (id) { return document.getElementById(id); };
  var main = $("muse");
  if (!main) { return; }
  var S = main.getAttribute("data-s") || "", C = main.getAttribute("data-c") || "";
  var step = main.getAttribute("data-step");
  var F = (window.location.hash || "").slice(1);
  if (!SECRET_RE.test(F)) { F = null; }
  function stepUrl(n) { return "/muse?s=" + encodeURIComponent(S) + "&step=" + n + (F ? "#" + F : ""); }
  ["next", "back"].forEach(function (id) {   // le secret du QR suit la navigation (fragment : jamais envoyé)
    var a = $(id);
    if (a && F) { a.setAttribute("href", a.getAttribute("href") + "#" + F); }
  });
  function post(path, body) {
    return fetch(path, { method: "POST", cache: "no-store", credentials: "same-origin", referrerPolicy: "same-origin",
                         headers: { "Content-Type": "application/json", "X-OpenTV-Muse": "1" },
                         body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j || {} }; });
      });
  }
  function done(title, text) {
    main.textContent = "";
    var h = document.createElement("h1"), p = document.createElement("p");
    h.textContent = title; p.className = "lead"; p.textContent = text;
    main.appendChild(h); main.appendChild(p);
  }
  var cancel = $("mcancel");
  if (cancel) {
    cancel.hidden = false;
    cancel.addEventListener("click", function () {
      cancel.disabled = true;
      post("/muse/cancel", { s: S, c: C }).then(function () {
        done("Annulé", "Rien n'a changé. Tu peux fermer cette page.");
      }).catch(function () { done("Annulé", "Regarde la TV : elle confirme l'annulation."); });
    });
  }

  if (step === "2") {
    var form = $("seal"), inp = $("tok"), send = $("send"), err = $("merr");
    var n = main.getAttribute("data-n"), e = main.getAttribute("data-e");
    if (!form || !inp || !send) { return; }
    if (!F || !n || !e || !selfTest()) { $("nosecure").hidden = false; return; }
    form.hidden = false;
    var busy = false;
    var showErr = function (t) { err.textContent = t; err.hidden = false; };
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) { return; }
      var v = String(inp.value || "").replace(/^[\s\u00a0]+|[\s\u00a0]+$/g, "");
      inp.value = "";   // le jeton quitte le champ tout de suite (jamais réaffiché)
      err.hidden = true;
      if (!TOKEN_RE.test(v)) {
        // début masqué recopié depuis la page Muse (•, *, …) : il ne peut pas servir ; jamais de conseil d'en créer
        // un autre (révoquer déconnecterait les autres gadgets)
        var masked = /[\u2022\u2026*\u25cf]|\.\.\./.test(v);
        v = null;
        showErr(masked ? "Ceci n'est que le début masqué du jeton : il faut le jeton complet, là où tu l'avais gardé." :
                "Ce n'est pas un jeton Muse complet (il commence par « mgst_ »). Colle le jeton entier.");
        return;
      }
      var ct = null;
      try { ct = seal(n, e, utf8(JSON.stringify({ v: 1, s: S, f: F, t: v })), secureRandom); } catch (x) { ct = null; }
      v = null;
      if (!ct) { showErr("Ce navigateur ne peut pas protéger le jeton : rien n'a été envoyé."); return; }
      busy = true; send.disabled = true; send.textContent = "Envoi…";
      post("/muse/submit", { s: S, c: C, ct: ct }).then(function (r) {
        ct = null;
        if (r.status === 200 || (r.status === 409 && r.j.error === "already_submitted")) {
          window.location.replace(stepUrl(3));
          return;
        }
        busy = false; send.disabled = false; send.textContent = "Envoyer à OpenTV";
        var code = r.j.error;
        if (code === "invalid_token") { showErr("Muse ne reconnaît pas ce jeton. Colle le jeton complet, le même que pour tes autres appareils."); }
        else if (code === "expired" || code === "too_many_attempts") {
          showErr("Ce QR n'est plus valable. Sur la TV : Réglages → Gérer les assistants → Muse.");
          send.disabled = true;
        } else { showErr("L'envoi n'a pas abouti : rescanne le QR de la TV, puis réessaie."); }
      }).catch(function () {
        ct = null; busy = false; send.disabled = false; send.textContent = "Envoyer à OpenTV";
        showErr("La boîte OpenTV ne répond pas : ce téléphone doit être sur le même Wi-Fi que la TV.");
      });
    });
  }

  if (step === "3") {
    var FINAL = { connected: 1, failed: 1, cancelled: 1, expired: 1 }, polls = 0, fails = 0;
    var RANK = { submitted: 1, preparing: 1, waiting_muse: 2, connecting: 3, connected: 4 };
    var paint = function (j) {
      var st = $("mstatus"), r = RANK[j.state] || 0, items = document.querySelectorAll("#mphases li"), i;
      if (j.state === "connected") { st.textContent = "Muse est connectée ! Tu peux fermer cette page."; }
      else if (FINAL[j.state]) { st.textContent = j.message || "La connexion n'a pas abouti. Recommence depuis la TV."; }
      else { st.textContent = (j.label || "Préparation") + "…"; }
      st.className = "lead mstatus" + (FINAL[j.state] && j.state !== "connected" ? " fail" : "");
      for (i = 0; i < items.length; i++) {
        items[i].className = j.state === "connected" || r > i + 1 ? "done" : r === i + 1 ? "on" : "";
      }
      if (j.ble_name && $("mble")) { $("mble").textContent = " (nom affiché : " + j.ble_name + ")"; }
      if (FINAL[j.state] && cancel) { cancel.hidden = true; }
    };
    var gone = function () {
      $("mstatus").textContent = "Regarde la TV pour la suite : cette page n'est plus nécessaire.";
      if (cancel) { cancel.hidden = true; }
    };
    var poll = function () {
      polls += 1;
      if (polls > 700) { gone(); return; }
      fetch("/muse/status?s=" + encodeURIComponent(S), { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
        .then(function (x) {
          fails = 0;
          if (x.status !== 200 || !x.j || !x.j.ok) { gone(); return; }
          paint(x.j);
          if (!FINAL[x.j.state]) { setTimeout(poll, 2000); }
        }).catch(function () {
          fails += 1;
          if (fails > 5) { gone(); } else { setTimeout(poll, 3000); }
        });
    };
    if (!FINAL[main.getAttribute("data-state")]) { poll(); } else if (cancel) { cancel.hidden = true; }
  }
})();
