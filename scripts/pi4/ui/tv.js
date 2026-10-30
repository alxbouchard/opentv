/* OpenTV Pi — écran TV (Chromium Raspberry Pi OS, loopback). Aucun script en ligne, aucune URL externe sauf affiches https. */
(function () {
  "use strict";
  var KEY = document.querySelector('meta[name="opentv-key"]').content;
  var $ = function (id) { return document.getElementById(id); };
  var state = null, fy = null, fyAt = 0, fails = 0, lastQr = "", lastApQr = "", lastRemoteQr = "";
  var REMOTE_ACTIONS = { up: 1, down: 1, left: 1, right: 1, ok: 1, back: 1, home: 1 };
  var remoteSeq = -1, remoteFails = 0;
  // « Gérer les assistants » : dernier résultat déjà vu (aucune annonce d'un résultat antérieur au chargement)
  var agentsSeq = null, lastAgentsQr = "", skipCancelToast = false;
  // QR des assistants absent (écoute impossible, adresse perdue) : rien pendant AGENTS_QR_WAIT_MS (le QR arrive
  // normalement au premier état), puis « QR indisponible, réessaie » ; à AGENTS_QR_GIVEUP_MS la feuille se ferme et
  // le mode est annulé — une seule fois, jamais de nouvel essai automatique (pas de boucle, jamais 10 min de vide)
  var AGENTS_QR_WAIT_MS = 2500, AGENTS_QR_GIVEUP_MS = 8000, MSG_AGENTS_QR = "QR indisponible, réessaie.";
  // « Connecter Muse » (Gérer les assistants → Muse) : consentement explicite sur la TV (« Autoriser et connecter »,
  // jamais au focus par défaut), puis QR de séance courte et avancement réel. Dernières issues déjà vues : jamais
  // annoncées de nouveau (rechargement compris) ; la TV ne voit jamais de jeton, de clé ni de commande.
  var museSeq = null, museDiscSeq = null, lastMuseQr = "", museSkipToast = false, museHidden = false;
  var MSG_MUSE_FOREIGN = "Une autre installation de Muse existe déjà sur cette boîte : rien n'est modifié d'ici.";
  var MSG_MUSE_PENDING = "Annulation demandée : la boîte termine l'opération en cours. Le résultat s'affichera ici.";
  var MUSE_CAPS = ["Te proposer des films à l'écran", "Apprendre tes goûts et gérer tes listes", "Naviguer dans OpenTV",
                   "Lancer ou fermer un film quand tu le lui demandes"];
  var MUSE_PHASES = [["preparing", "Préparation"], ["waiting_muse", "En attente de Muse"], ["connecting", "Connexion"],
                     ["connected", "Muse est connectée"]];
  var MUSE_RANK = { submitted: 1, preparing: 1, waiting_muse: 2, connecting: 3, connected: 4 };
  var MUSE_PATH = "Sur l'iPhone : Muse → Réglages → Appareils → Mode développeur → + → OpenTV";
  var focus = { zone: "rows", row: 0, cols: [], auto: false }, panel = null, toastTimer = null;
  // « Regarder » : contexte de la lecture en cours (carte d'origine), sondage rapide de l'état typé du service
  var play = null, playEndSeq = -1, PLAY_POLL_MS = 250, MSG_PLAY_KO = "Lecture indisponible";
  // Retour/Accueil encore tenus quand le lecteur se ferme (la télécommande CEC répète l'appui) : ignorés au moins
  // RETURN_GUARD_MS après le retour, puis tant que les répétitions continuent (fenêtre glissante) — la fiche et la
  // carte d'origine restent en place
  var RETURN_GUARD_MS = 600, guardUntil = 0;
  var MSG_PLAY_LOADING = "Chargement…", MSG_PLAY_RETURN = "Retour à OpenTV…";
  // Pilotage par un assistant (scope « control ») : actions typées numérotées du service (fiche proposée, film lancé
  // par le lecteur officiel, touche de navigation). Chacune est RÉCLAMÉE (claim, atomique côté service) avant d'être
  // appliquée, puis son résultat est rendu (done) : jamais appliquée deux fois, même page rechargée. La télécommande
  // d'Alex (CEC/USB/clavier) et son téléphone restent prioritaires : une touche d'assistant arrivée moins de
  // HUMAN_PRIORITY_MS après une touche humaine est refusée ; Réglages, confirmation et « Regarder » par OK sont
  // interdits à l'assistant (lancement : seulement play_title, mêmes portes que « Regarder »).
  var actSeq = 0, actBusy = false, lastHumanAt = 0, HUMAN_PRIORITY_MS = 2000;
  var LABELS = { watch: "Regarder", like: "J'aime", dislike: "Pas pour moi", seen: "Déjà vu", watchlist: "À voir plus tard",
                 "not-tonight": "Pas ce soir" };
  var TOGGLE = { like: ["opinion", "like", "clear-opinion"], dislike: ["opinion", "dislike", "clear-opinion"],
                 seen: ["seen", true, "unseen"], watchlist: ["watchlist", true, "unwatchlist"],
                 "not-tonight": ["not_tonight", true, "clear-not-tonight"] };
  var AGENTS = { muse: "Muse", hermit: "Hermit", hermes: "Hermes" };
  // nom affiché d'un assistant : le service envoie l'agent PROPRIÉTAIRE (« muse-device » -> « muse ») ; jamais un
  // identifiant technique brut ni une propriété héritée d'Object (« constructor »…)
  function agentName(id) { return typeof id === "string" && AGENTS.hasOwnProperty(id) ? AGENTS[id] : "un assistant"; }
  var NET = { ethernet: "Ethernet", wifi: "Wi-Fi", hotspot: "Wi-Fi temporaire", offline: "Hors ligne" };
  // Reprise après redémarrage du service : la clé de page change à chaque démarrage. Un 403 explicite « tv_key »
  // recharge /tv (qui porte la nouvelle clé) après une attente croissante, au plus RELOAD_MAX fois par fenêtre,
  // journal tenu dans sessionStorage (survit au rechargement). Sans stockage : aucun rechargement (pas de boucle).
  // Sonde /tv en échec (service pas encore prêt) : attente doublée à chaque échec, plafonnée à PROBE_MAX_MS
  // (2, 4, 8, 16, 32, 60, 60… s) — jamais une sonde par tick de 2 s.
  var RELOAD_STORE = "opentv-reloads", RELOAD_WINDOW_MS = 600000, RELOAD_MAX = 3, RELOAD_BASE_MS = 2000;
  var PROBE_MAX_MS = 60000;
  var reloadTimer = null, probeFails = 0;
  var MSG_OFFLINE = "Reconnexion à OpenTV…";
  var MSG_RELOAD = "OpenTV a redémarré : rechargement de l'écran…";
  var MSG_STUCK = "L'écran OpenTV ne se reconnecte pas. Redémarre la boîte OpenTV si cela dure.";

  function api(method, path, body) {
    var h = { "X-OpenTV-TV": KEY };
    if (body) { h["Content-Type"] = "application/json"; }
    return fetch(path, { method: method, headers: h, body: body ? JSON.stringify(body) : undefined, cache: "no-store" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (r.status === 403 && j && j.error === "tv_key") { var e = new Error("tv_key"); e.tvKey = true; throw e; }
          if (!r.ok && !j.message) { throw new Error(r.status); }
          return j;
        });
      });
  }
  function reloadStore() { try { return window.sessionStorage || null; } catch (e) { return null; } }
  function recentReloads(st) {
    var now = Date.now(), list = [];
    try { list = JSON.parse(st.getItem(RELOAD_STORE) || "[]"); } catch (e) { list = []; }
    if (!Array.isArray(list)) { list = []; }
    return list.filter(function (t) { return typeof t === "number" && t <= now && now - t < RELOAD_WINDOW_MS; });
  }
  // voile « Reconnexion… » : il recouvre tout, donc une feuille Canvas dessous serait un consentement invisible.
  // Sans « Oui » en vol, elle est refusée tout de suite (chemin sûr « Non », annulation bornée) ; avec un « Oui » en
  // vol, ses touches restent bloquées (sheetKey) jusqu'à la réponse ou l'échéance CV_ROUTE_MS.
  function showOffline(text) {
    $("offline").textContent = text; $("offline").hidden = false;
    if (cvSheet && !cvSheet.busy) { sheetChoose("no", "offline"); }
    cvPaint();
  }
  function offlineShown() { return !$("offline").hidden; }
  function scheduleReload() {
    if (reloadTimer) { return; }
    var st = reloadStore();
    if (!st) { showOffline(MSG_STUCK); return; }
    var list = recentReloads(st);
    if (list.length >= RELOAD_MAX) {   // plafond atteint : message honnête, nouvel essai à l'expiration de la fenêtre
      showOffline(MSG_STUCK);
      reloadTimer = setTimeout(function () { reloadTimer = null; }, RELOAD_WINDOW_MS - (Date.now() - list[0]) + 1000);
      return;
    }
    showOffline(probeFails ? MSG_OFFLINE : MSG_RELOAD);
    var wait = Math.min(PROBE_MAX_MS, RELOAD_BASE_MS * Math.pow(2, list.length + probeFails));
    reloadTimer = setTimeout(function () {
      fetch("/tv", { cache: "no-store" }).then(function (r) {   // le service répond-il de nouveau avec une page ?
        if (!r.ok) { throw new Error(r.status); }
        var fresh = recentReloads(st);
        fresh.push(Date.now());
        st.setItem(RELOAD_STORE, JSON.stringify(fresh));
        if (JSON.parse(st.getItem(RELOAD_STORE) || "[]").length !== fresh.length) { throw new Error("store"); }
        location.replace("/tv");
      }).catch(function () { probeFails += 1; reloadTimer = null; });
    }, wait);
  }
  function onApiError(e) { if (e && e.tvKey) { scheduleReload(); return true; } return false; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }
  function drawQR(canvas, text) {
    if (typeof qrcode !== "function") { canvas.setAttribute("data-qr", "missing"); return; }
    var q = qrcode(0, "M");
    q.addData(text); q.make();
    // net sur la TV : taille fixe (data-size), modules entiers centrés, zone calme ≥ 4 modules, et 1 pixel du canvas
    // = 1 pixel CSS (style imposé) : jamais de rééchantillonnage flou, quelle que soit la longueur de l'adresse
    var n = q.getModuleCount(), size = Number(canvas.getAttribute("data-size") || 400);
    var cell = Math.max(2, Math.floor(size / (n + 8))), dim = Math.max(size, cell * (n + 8));
    var pad = Math.floor((dim - cell * n) / 2);
    canvas.width = canvas.height = dim;
    canvas.style.width = canvas.style.height = dim + "px";
    canvas.setAttribute("data-cell", String(cell)); canvas.setAttribute("data-quiet", String(pad));
    var g = canvas.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = "#111";
    for (var r = 0; r < n; r++) { for (var c = 0; c < n; c++) { if (q.isDark(r, c)) { g.fillRect(pad + c * cell, pad + r * cell, cell, cell); } } }
    canvas.setAttribute("data-qr", "ok");
  }
  function toast(msg, big) {
    var t = $("toast");
    t.textContent = msg; t.hidden = false; t.className = "toast" + (big ? " big" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, big ? 10000 : 4000);
  }
  // horloge et date d'OpenTV v23 : « 21:05 » et « mardi 27 octobre » (majuscule par le CSS)
  var DAYS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"];
  var MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
                "novembre", "décembre"];
  function two(n) { return (n < 10 ? "0" : "") + n; }
  // « eyebrow » du héros v23 : salut + contexte ; seul le salut suit l'horloge (aucun nouveau rendu du héros/des cartes)
  var kickerHi = null, kickerRest = null;
  function greetingFor(h) { return h < 12 ? "Bonjour" : h < 18 ? "Bon après-midi" : "Bonsoir"; }
  function greeting() { return greetingFor(new Date().getHours()); }
  function setKicker(rest) {
    kickerHi = greeting();
    kickerRest = rest;
    $("hkicker").textContent = kickerHi + " · " + rest;
  }
  function clock() {
    var d = new Date(), hi = greetingFor(d.getHours());
    $("clock").textContent = two(d.getHours()) + ":" + two(d.getMinutes());
    $("date").textContent = DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()];
    if (kickerRest !== null && hi !== kickerHi) {   // changement de tranche (12 h, 18 h, minuit) : texte seul
      kickerHi = hi;
      $("hkicker").textContent = hi + " · " + kickerRest;
    }
  }

  // ---- configuration (QR)
  function renderSetup(s) {
    var steps = $("steps");
    steps.textContent = "";
    // mêmes 3 gestes que l'assistant du téléphone (Wi-Fi, mot de passe, c'est prêt) ; hotspot : rejoindre d'abord
    var list = s.hotspot ? ["Sur ton téléphone, rejoins le Wi-Fi « " + s.hotspot.ssid + " » (QR de gauche).",
                            "Scanne le QR de droite avec l'appareil photo.",
                            "Choisis ton Wi-Fi et tape son mot de passe : la TV affiche ensuite OpenTV."]
                         : ["Scanne le QR avec l'appareil photo de ton téléphone.",
                            "Choisis ton Wi-Fi (ou garde le câble).",
                            "Tape le mot de passe : la TV affiche ensuite OpenTV."];
    list.forEach(function (x) { steps.appendChild(el("li", null, x)); });
    $("setupmsg").textContent = s.message || (s.expires_in ? "Ce QR est valable " + Math.max(1, Math.round(s.expires_in / 60)) + " min, une seule fois." : "");
    // connexion Wi-Fi en cours mais assistants non enregistrés : dit tout de suite, en clair (aucun détail technique)
    $("agentsnote").hidden = !(s.phase === "applying" && s.agents_saved === false && s.agents_message);
    $("agentsnote").textContent = $("agentsnote").hidden ? "" : s.agents_message;
    $("apfig").hidden = !s.hotspot;
    if (s.hotspot) {
      $("apssid").textContent = s.hotspot.ssid;
      if (s.hotspot.wifi_qr !== lastApQr) { lastApQr = s.hotspot.wifi_qr; drawQR($("apqr"), lastApQr); }
    }
    $("qrfig").hidden = !s.url;
    $("url").textContent = "";   // jamais d'adresse technique à l'écran : le QR suffit
    if (s.url && s.url !== lastQr) { lastQr = s.url; drawQR($("qr"), s.url); }
  }

  // ---- Pour toi : héros (titre au focus) + rangées. Navigation verticale normale : ↑ remonte d'une rangée
  // (colonne mémorisée par rangée), ↓ descend ; seul ↑ depuis la première rangée (ou depuis une fiche) mène aux
  // Réglages = cercle en haut à droite (zone « top »), OK ouvre, Retour ferme — jamais une rangée cachée.
  // Retour remonte aussi d'une seule rangée, Accueil = tout début.
  // Relecture « Pour toi » toutes les FY_REFRESH_MS (≤ 60 s) : le service recalcule dès que le catalogue est remplacé,
  // donc un rafraîchissement quotidien réussi est visible à l'accueil en ≤ FY_REFRESH_MS + une période de poll (2 s).
  // Réponse identique à celle affichée : rien n'est redessiné (focus, défilement et affiches intacts). Réponse
  // différente pendant une fiche/feuille ou une lecture (chargement, lecture, fermeture) : gardée en attente et
  // appliquée dès la fermeture/le retour ; le focus est alors réancré par identifiant stable du film (jamais par
  // rangée/colonne), avec repli sur la même rangée si le film a disparu.
  var FY_REFRESH_MS = 60000;
  var ROWS = [], grid = [], heroId = null, fySig = null, fyPending = null;
  function loadForYou(force) {
    if (!force && fy && Date.now() - fyAt < FY_REFRESH_MS) { return; }
    fyAt = Date.now();
    api("GET", "/api/tv/foryou").then(offerForYou).catch(function (e) { fyAt = 0; onApiError(e); });
  }
  function sigOf(d) { try { return JSON.stringify(d); } catch (e) { return null; } }
  function fyBusy() { return !!(play || panel || cvSheet); }   // fiche, feuille Canvas ou lecture : rangées gelées
  function offerForYou(d) {
    var sig = sigOf(d);
    if (fy && sig !== null && sig === fySig) { fyPending = null; return; }   // inchangé : aucun nouveau rendu
    if (fyBusy()) { fyPending = { d: d, sig: sig }; return; }               // fiche ou lecture : plus tard
    applyForYou(d, sig);
  }
  function applyForYou(d, sig) { fyPending = null; fy = d; fySig = sig; renderRows(); }
  function flushForYou() {   // -> true si une sélection en attente vient d'être appliquée (fiche/lecture fermée)
    if (!fyPending || fyBusy()) { return false; }
    var p = fyPending;
    applyForYou(p.d, p.sig);
    return true;
  }
  // fiche, feuille ou lecture terminée : ordre des rangées de la scène publiée PUIS sélection en attente ; un seul
  // nouveau rendu, focus réancré par rangée + film (identifiants stables). -> true si les rangées ont été redessinées
  function flushPending() {
    var ro = takeRowOrder();
    if (flushForYou()) { return true; }
    if (ro && fy) { renderRows(); return true; }
    return false;
  }
  function badges(m) {
    var out = [];
    if (!m) { return out; }
    if (m.opinion === "like") { out.push("J'aime"); }
    if (m.opinion === "dislike") { out.push("Pas pour moi"); }
    if (m.seen) { out.push("Déjà vu"); }
    if (m.watchlist) { out.push("À voir plus tard"); }
    if (m.not_tonight) { out.push("Pas ce soir"); }
    return out;
  }
  function hue(x) {
    var h = 0, t = String(x || "");
    for (var i = 0; i < t.length; i++) { h = (h * 31 + t.charCodeAt(i)) % 360; }
    return h;
  }
  function artFor(it) {   // visuel dessiné (aucune ressource) : jamais un bloc sombre « en chargement »
    var a = el("div", "art"), h = hue(it.id || it.title);
    a.style.background = "linear-gradient(160deg, hsl(" + h + ", 52%, 36%) 0%, hsl(" + ((h + 48) % 360) +
                         ", 58%, 17%) 68%, #0a0c16 100%)";
    a.appendChild(el("span", "letter", (it.title || "?").charAt(0).toUpperCase()));
    return a;
  }
  function isHttps(u) { return typeof u === "string" && u.indexOf("https://") === 0; }
  function poster(box, it) {   // affiche https au-dessus du visuel dessiné ; échec : l'image disparaît, le visuel reste
    if (box.getAttribute("data-img") || !isHttps(it.poster)) { return; }
    box.setAttribute("data-img", "1");
    var img = el("img"), art = box.querySelector(".art");
    img.alt = ""; img.referrerPolicy = "no-referrer"; img.decoding = "async";
    img.addEventListener("error", function () { if (img.parentNode) { img.parentNode.removeChild(img); } });
    img.src = it.poster;
    box.insertBefore(img, art ? art.nextSibling : null);
  }
  function meta(it, short) {
    var g = (it.genres || []).slice(0, short ? 2 : 3).join(", "), parts = [];
    if (it.year) { parts.push(String(it.year)); }
    if (g) { parts.push(g); }
    return parts.join(" · ");
  }
  function scores(it) {   // fournisseurs étiquetés séparément ; donnée absente = rien (jamais 0)
    var out = [];
    [["imdb", "IMDb"], ["tmdb", "TMDb"]].forEach(function (p) {
      var v = it[p[0]] && it[p[0]].rating;
      if (typeof v === "number" && isFinite(v) && v > 0) { out.push(p[1] + " " + String(Math.round(v * 10) / 10).replace(".", ",")); }
    });
    return out;
  }
  function rowsData() {
    var out = [];
    ((fy && fy.rows) || []).forEach(function (r) { if (r && r.items && r.items.length) { out.push(r); } });
    return orderRows(out);
  }
  function renderEmpty(box) {
    var d = el("div", "empty"), ul = el("ul");
    d.appendChild(el("h2", null, "En attendant"));
    var lines = [["★", "Des choix qui apprennent de tes goûts : « J'aime », « Pas pour moi », « À voir plus tard »."]];
    if (state && state.remote && state.remote.enabled) {
      lines.push(["▣", "Ton téléphone peut aussi servir de télécommande : vise le QR à droite."]);
    }
    lines.push(["⚙", "Réglages : le cercle en haut à droite de l'écran (↑ puis OK)."]);
    lines.forEach(function (x) {
      var li = el("li");
      li.appendChild(el("span", "ico", x[0])); li.appendChild(el("span", null, x[1]));
      ul.appendChild(li);
    });
    d.appendChild(ul);
    box.appendChild(d);
  }
  // ---- focus par identifiant stable : relevé avant un nouveau rendu, réappliqué après (rangées réordonnées, titres
  // ajoutés ou retirés) ; rangée identifiée par son id (« tonight », « top »…), sinon par sa position
  function rowKey(r, i) { return r && r.id ? "r:" + r.id : "i:" + i; }
  function anchor() {
    var a = { id: null, key: null, row: focus.row, col: col(), cols: {}, colIdx: {} };
    ROWS.forEach(function (r, ri) {
      var c = focus.cols[ri] || 0, k = rowKey(r, ri);
      a.colIdx[k] = c;
      if (r.items[c]) { a.cols[k] = r.items[c].id; }
      if (ri === focus.row) { a.key = k; a.id = r.items[c] ? r.items[c].id : null; }
    });
    return a;
  }
  function findItem(id, key) {   // -> {row, col} : même rangée d'abord, sinon la première qui contient ce film
    var hit = null;
    if (!id) { return null; }
    ROWS.forEach(function (r, ri) {
      r.items.forEach(function (it, ci) {
        if (it.id === id && (!hit || (rowKey(r, ri) === key && hit.key !== key))) { hit = { row: ri, col: ci, key: rowKey(r, ri) }; }
      });
    });
    return hit;
  }
  function reanchor(a) {
    var cols = [];
    ROWS.forEach(function (r, ri) {
      var k = rowKey(r, ri), want = a.cols[k], c = -1;
      if (want) { r.items.forEach(function (it, ci) { if (c < 0 && it.id === want) { c = ci; } }); }
      cols[ri] = c >= 0 ? c : Math.min(a.colIdx[k] || 0, r.items.length - 1);
    });
    focus.cols = cols;
    if (!ROWS.length) { return; }
    var hit = findItem(a.id, a.key);
    if (hit) { focus.row = hit.row; focus.cols[hit.row] = hit.col; return; }
    // film retiré : même rangée (par id) à la même colonne bornée, sinon la position la plus proche
    var same = -1;
    ROWS.forEach(function (r, ri) { if (same < 0 && rowKey(r, ri) === a.key) { same = ri; } });
    focus.row = same >= 0 ? same : Math.min(a.row, ROWS.length - 1);
    focus.cols[focus.row] = Math.max(0, Math.min(a.col, ROWS[focus.row].items.length - 1));
  }
  function renderRows() {
    var box = $("rowsin"), a = anchor();
    ROWS = rowsData(); grid = [];
    reanchor(a);
    box.textContent = "";
    if (!ROWS.length) { renderEmpty(box); }
    ROWS.forEach(function (r) {
      var few = r.items.length <= 2, row = el("div", "row"), lane = el("div", "lane"), strip = el("div", "strip"), cards = [];
      row.appendChild(el("h2", null, r.title));
      r.items.forEach(function (it) {
        var card = el("div", "card"), info = el("div", "info"), marks = badges(((fy && fy.marks) || {})[it.id]);
        card.appendChild(artFor(it));
        if (marks.length) {
          var mk = el("div", "marks");
          marks.forEach(function (b) { mk.appendChild(el("span", null, b)); });
          card.appendChild(mk);
        }
        info.appendChild(el("div", "t", it.title));
        if (meta(it, true)) { info.appendChild(el("div", "y", meta(it, true))); }
        if (it.reasons && it.reasons[0] && it.reasons[0].text) { info.appendChild(el("div", "r", it.reasons[0].text)); }
        card.appendChild(info);
        strip.appendChild(card);
        cards.push(card);
      });
      lane.appendChild(strip); row.appendChild(lane); box.appendChild(row);
      grid.push({ row: row, strip: strip, cards: cards, few: few });
    });
    if (!ROWS.length) { focus.zone = "top"; focus.auto = true; }            // seul choix possible : Réglages
    else if (focus.auto && !panel) { focus.zone = "rows"; focus.auto = false; }
    heroId = null;
    paint();
    renderRemote();
  }
  function col() { return focus.cols[focus.row] || 0; }
  function paint() {
    if (!ROWS.length) { focus.zone = "top"; }
    else {
      focus.row = Math.max(0, Math.min(focus.row, ROWS.length - 1));
      focus.cols[focus.row] = Math.max(0, Math.min(col(), ROWS[focus.row].items.length - 1));
    }
    // feuille Canvas : son choix est le SEUL focus visible ; la carte/Réglages choisis restent mémorisés (focus.*)
    // et retrouvent leur anneau à la fermeture
    var top = focus.zone === "top", ring = !cvSheet;
    $("gear").className = "qbtn" + (top ? (panel ? " active" : ring ? " focused" : "") : "");
    // panneau ouvert : le message d'assistant s'efface (couche dédiée, jamais sur Réglages/confirmation/fiche) ;
    // feuille latérale (Réglages, confirmation, Canvas) : la mention Samsung du pied de page ne passe jamais dessous
    $("app").className = (top ? "zone-top" : "") + (panel || cvSheet ? " has-panel" : "") +
      (cvSheet || (panel && panel.kind !== "item") ? " has-sheet" : "");
    $("main").className = "main" + (panel ? " dim" : "");
    grid.forEach(function (g, ri) {
      var cur = ri === focus.row, c = focus.cols[ri] || 0, fc = g.cards[c] || g.cards[0];
      g.row.className = "row" + (g.few ? " few" : "") + (cur ? " current" : "");
      g.cards.forEach(function (card, ci) {
        card.className = "card" + (ring && !top && cur && ci === c ? " focused" : "");
        if (Math.abs(ri - focus.row) <= 1 && ci <= c + 8) { poster(card, ROWS[ri].items[ci]); }   // affiches proches seulement
      });
      // défilement horizontal par transform (pas de scrollIntoView) : la carte au focus garde une marge à droite
      var last = g.cards[g.cards.length - 1], lw = g.strip.parentNode.clientWidth - 76, x = 0;
      var need = fc.offsetLeft + fc.offsetWidth + 120 - lw, max = Math.max(0, last.offsetLeft + last.offsetWidth - lw);
      if (need > 0) { x = Math.min(need, max); }
      g.strip.style.transform = x ? "translateX(" + (-x) + "px)" : "";
    });
    var g0 = grid[focus.row];
    $("rowsin").style.transform = g0 && g0.row.offsetTop ? "translateY(" + (-g0.row.offsetTop) + "px)" : "";
    hero();
    help();
    cvPaint();
  }
  // aide du bas : toujours celle du contexte courant (rangées, Réglages en haut, panneau)
  var HELP = {
    rows: [["← →", "Parcourir"], ["↑", "Rangée précédente"], ["↓", "Rangée suivante"], ["Retour", "Rangée précédente"],
           ["OK", "Ouvrir"]],
    rows0: [["← →", "Parcourir"], ["↓", "Rangée suivante"], ["↑", "Réglages"], ["OK", "Ouvrir"]],
    top: [["OK", "Ouvrir les Réglages"], ["↓ ou Retour", "Revenir aux titres"]],
    topEmpty: [["OK", "Ouvrir les Réglages"]],
    item: [["← →", "Choisir"], ["OK", "Valider"], ["Retour", "Fermer"]],
    agents: [["Téléphone", "Scanne le QR"], ["OK ou Retour", "Annuler"]],
    musesetup: [["Téléphone", "Suis les étapes"], ["OK ou Retour", "Annuler"]],
    panel: [["↑ ↓", "Choisir"], ["OK", "Valider"], ["Retour", "Revenir"]],
    // feuille Canvas : boutons empilés (↑ = « Non » en haut, ↓ = « Oui » ; ← → identiques), OK = choix au focus
    sheetNo: [["↑ ↓", "Choisir"], ["OK", "Non, garder l'ambiance"], ["Retour", "Non"]],
    sheetYes: [["↑ ↓", "Choisir"], ["OK", "Oui, appliquer"], ["Retour", "Non"]]
  }, helpKey = "";
  function help() {
    var k = cvSheet ? (cvSheet.btn === 1 ? "sheetYes" : "sheetNo") :
            panel ? (panel.kind === "item" ? "item" : panel.kind === "agents" ? "agents" :
                     panel.kind === "musesetup" ? "musesetup" : "panel") : (focus.zone === "top" ? (ROWS.length ? "top" : "topEmpty") :
                                                                       (focus.row > 0 ? "rows" : "rows0"));
    if (k === helpKey) { return; }
    helpKey = k;
    var h = $("hint");
    h.textContent = "";
    HELP[k].forEach(function (x, i) {
      if (i) { h.appendChild(document.createTextNode(" · ")); }
      h.appendChild(el("span", "hk", x[0])); h.appendChild(document.createTextNode(" " + x[1]));
    });
  }
  function hero() {
    var r = ROWS[focus.row] || null, it = r ? r.items[col()] : null, key = it ? "i:" + it.id : (fy ? "empty" : "wait");
    if (key === heroId) { return; }
    heroId = key;
    if (CV) { CV.focus(it ? hue(it.id || it.title) : -1); }   // halo du héros : teinte du visuel dessiné au focus
    var chips = $("hchips");
    chips.textContent = "";
    if (!it) {
      setKicker("Pour toi");
      $("htitle").textContent = "Ta soirée se prépare";
      $("htitle").className = "htitle";
      $("hmeta").textContent = "OpenTV choisit pour toi dans le catalogue disponible.";
      $("hdesc").textContent = (fy && fy.note) || "Tes suggestions apparaîtront ici dès que le catalogue sera prêt : rien à faire de ton côté.";
    } else {
      setKicker(r.title);
      $("htitle").textContent = it.title;
      $("htitle").className = "htitle" + (String(it.title || "").length > 24 ? " long" : "");
      $("hmeta").textContent = meta(it, false);
      scores(it).forEach(function (c) { chips.appendChild(el("span", "chip score", c)); });
      var why = (it.reasons || []).filter(function (x) { return x && x.code !== "quality" && x.text; })[0];
      if (why) { chips.appendChild(el("span", "chip why", why.text)); }
      else if (!scores(it).length && it.reasons && it.reasons[0]) { chips.appendChild(el("span", "chip", it.reasons[0].text)); }
      badges(((fy && fy.marks) || {})[it.id]).forEach(function (b) { chips.appendChild(el("span", "chip mark", b)); });
      $("hdesc").textContent = it.description || "";
    }
    // v23 : le héros est du texte posé sur la photo ; l'affiche vit dans la carte au focus (aucune image ici)
  }
  // ---- télécommande sur téléphone (repli de la télécommande CEC) : QR d'appairage toujours visible sur l'accueil,
  // dans sa propre colonne (jamais sur les cartes), même après un premier appairage : un téléphone perdu se remplace
  // sans aucune touche. Rien à recopier : ni adresse, ni code.
  function renderRemote() {
    var r = state && state.remote, box = $("remotebox");
    // QR des assistants affiché : un seul QR à l'écran (jamais deux codes à confondre)
    var show = !!(r && r.enabled && r.url && !state.setup.active) &&
      !(panel && (panel.kind === "agents" || panel.kind === "musesetup"));
    box.hidden = !show;
    if (!show) { return; }
    if (r.url !== lastRemoteQr) { lastRemoteQr = r.url; drawQR($("rqr"), r.url); }
    $("rtitle").textContent = r.phones > 0 ? "Ajouter un téléphone" : "Télécommande sur ton téléphone";
    $("rnote").textContent = r.phones > 0 ? "Téléphone perdu ? Scanne ce QR avec un autre, puis Réglages : « Oublier les téléphones »."
                                          : "Facultatif. Rien à taper : le QR suffit.";
  }
  function remoteLoop() {   // longue requête bornée côté service ; aucun téléphone : réponse immédiate « idle »
    api("GET", "/api/tv/remote?after=" + remoteSeq).then(function (d) {
      remoteFails = 0;
      if (d && typeof d.seq === "number") {
        if (remoteSeq >= 0 && d.events) {
          d.events.forEach(function (ev) { if (ev && REMOTE_ACTIONS[ev.a] === 1) { remoteKey(ev.a); } });
        }
        remoteSeq = d.seq;
      }
      setTimeout(remoteLoop, d && (d.idle || d.busy) ? 3000 : 30);
    }).catch(function (e) {
      onApiError(e);
      remoteFails += 1;
      setTimeout(remoteLoop, Math.min(30000, 1000 * Math.pow(2, remoteFails)));
    });
  }
  function current() {
    if (focus.zone !== "rows" || !ROWS.length) { return null; }
    return ROWS[focus.row].items[col()] || null;
  }
  // ---- panneau : fiche d'un titre, Réglages, confirmation (Gauche/Droite choisit, OK valide, Retour revient)
  function showPanel(kind, kicker, title, text, buttons, btn, extra) {
    var box = $("pbtns");
    box.textContent = "";
    buttons.forEach(function (b) { box.appendChild(b); });
    $("pkicker").textContent = kicker || "";
    $("ptitle").textContent = title;
    $("pmeta").textContent = text || "";
    panel = extra || {};
    panel.kind = kind; panel.btn = btn;
    $("panel").className = "panel" + (kind === "item" ? "" : " sheet");   // Réglages : feuille latérale v23
    $("pqrfig").hidden = kind !== "agents" && kind !== "musesetup";
    $("pcaps").hidden = true; $("pphases").hidden = true;
    $("panel").hidden = false;
    paintPanel(); paint(); renderRemote();
  }
  function button(label, base) {
    var b = el("button", base || null, label);
    b.setAttribute("data-base", base || "");
    return b;
  }
  function canWatch() { return !!(state && state.playback && state.playback.available); }
  function openItem(it, row, prop) {   // « Regarder » en premier, seulement si une lecture réelle est possible
    var m = ((fy && fy.marks) || {})[it.id] || {}, actions = Object.keys(LABELS).filter(function (a) {
      return a !== "watch" || canWatch();
    });
    var kicker = row ? row.title : "", text = it.description || meta(it, false);
    if (prop) {   // fiche proposée par un assistant : son nom et sa phrase (texte seul, sans adresse) en tête
      kicker = "Proposé par " + agentName(prop.from);
      if (prop.text) { text = "« " + prop.text + " »" + (text ? " — " + text : ""); }
    }
    showPanel("item", kicker, it.title, text, actions.map(function (a) {
      if (a === "watch") { return button(LABELS[a], "play"); }
      var on = m[TOGGLE[a][0]] === TOGGLE[a][1];
      return button((on ? "✓ " : "") + LABELS[a], on ? "on" : "");
    }), 0, { it: it, actions: actions, prop: prop || null });
  }
  // ---- « Regarder » : écran OpenTV pendant la préparation cachée ; retour sur la même carte, fiche rouverte
  function showPlayOverlay(title, msg) {
    $("playtitle").textContent = title || "";
    $("playmsg").textContent = msg || MSG_PLAY_LOADING;
    $("playov").hidden = false;
  }
  function restoreCard(p) {   // même film (identifiant stable), fiche rouverte sur « Regarder »
    var hit = findItem(p.it.id, p.key);
    focus.zone = ROWS.length ? "rows" : "top";
    if (!hit) {   // film absent des rangées affichées : position d'origine bornée
      if (ROWS.length) { focus.row = Math.min(p.row, ROWS.length - 1); focus.cols[focus.row] = p.col; }
      paint();
      // fiche proposée par un assistant (film du catalogue hors des rangées) : la même fiche est rouverte
      if (p.prop) { openItem(p.it, null, p.prop); }
      return;
    }
    focus.row = hit.row; focus.cols[hit.row] = hit.col;
    paint();
    openItem(ROWS[hit.row].items[hit.col], p.prop ? null : ROWS[hit.row], p.prop || null);
  }
  // lecteur révélé ou fermeture demandée : la page /tv (sous la fenêtre du lecteur) montre DÉJÀ la destination du
  // retour — jamais un « Chargement… » périmé quand le lecteur disparaît
  function restoreUnder() {
    if (!play || play.restored) { return; }
    play.restored = true;
    if (play.it) { $("playov").hidden = true; restoreCard(play); }
    else { showPlayOverlay("", MSG_PLAY_RETURN); }   // page rechargée en cours de lecture : aucune carte à rouvrir
  }
  function guardReturn() { guardUntil = Date.now() + RETURN_GUARD_MS; }
  function guarded(a) {   // -> true : appui Retour/Accueil tenu depuis la fermeture du lecteur, ignoré
    if ((a !== "back" && a !== "home") || Date.now() >= guardUntil) { return false; }
    guardUntil = Date.now() + RETURN_GUARD_MS;
    return true;
  }
  function startPlay(it) {
    play = { it: it, row: focus.row, col: col(), key: ROWS[focus.row] ? rowKey(ROWS[focus.row], focus.row) : null,
             seq: -1, done: false, restored: false, prop: (panel && panel.prop) || null };
    closePanel();
    showPlayOverlay(it.title);
    api("POST", "/api/tv/play", { id: it.id }).then(function (r) {
      if (!play) { return; }
      if (!r.ok) { endPlay({ state: "unavailable", seq: r.playback ? r.playback.seq : -1 }, true); return; }
      pollPlay();
    }).catch(function (e) { if (!onApiError(e) && play) { endPlay({ state: "unavailable", seq: -1 }, true); } });
  }
  function pollPlay() {
    if (!play) { return; }
    api("GET", "/api/tv/play").then(function (v) {
      if (!play) { return; }
      if (v.state === "closed" || v.state === "unavailable") { endPlay(v, false); return; }
      // lecture lancée par un assistant : un « idle » antérieur à son démarrage (minSeq) n'est jamais une fin
      if (v.state === "idle" && v.seq !== play.seq && !(play.minSeq && v.seq < play.minSeq)) { endPlay(v, false); return; }
      if (v.state === "playing" || v.state === "closing") { restoreUnder(); }
      setTimeout(pollPlay, PLAY_POLL_MS);
    }).catch(function (e) { if (!onApiError(e)) { setTimeout(pollPlay, 1000); } });
  }
  function closePlay() {   // Retour/Accueil vus par /tv (avant l'ouverture de la fenêtre du lecteur)
    api("POST", "/api/tv/play/close", {}).catch(onApiError);
  }
  function endPlay(v, local) {
    var p = play;
    play = null;
    guardReturn();
    if (typeof v.seq === "number" && v.seq > playEndSeq) { playEndSeq = v.seq; }
    $("playov").hidden = true;
    if (p && p.it && !p.restored) { restoreCard(p); }
    if (v.state === "unavailable") { toast(MSG_PLAY_KO, true); }   // grand message centré : jamais sur la fiche
    flushPending();   // sans fiche rouverte (page rechargée en cours de lecture) : ordre et sélection en attente
    if (!local && typeof v.seq === "number" && v.seq >= 0) {   // repeint d'abord, puis acquittement au service
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        api("POST", "/api/tv/play/ack", { seq: v.seq }).catch(onApiError);
      }); });
    }
  }
  function agentPlayPending(s) {   // lecture lancée par un assistant dont l'action « play » n'est pas encore appliquée
    var l = (s && s.actions && s.actions.pending) || [], i;
    for (i = 0; i < l.length; i++) { if (l[i] && l[i].kind === "play" && l[i].seq > actSeq) { return true; } }
    return actBusy;
  }
  function resumePlay(v) {   // page /tv rechargée pendant une lecture : écran de chargement ou acquittement
    if (play || !v || (typeof v.seq === "number" && v.seq <= playEndSeq)) { return; }
    if ((v.state === "loading" || v.state === "playing") && agentPlayPending(state)) { return; }   // l'action s'en charge
    if (v.state === "loading" || v.state === "playing" || v.state === "closing") {
      play = { it: null, row: focus.row, col: col(), seq: -1, restored: false };
      showPlayOverlay("", v.state === "loading" ? MSG_PLAY_LOADING : MSG_PLAY_RETURN);
      if (v.state !== "loading") { play.restored = true; }
      pollPlay();
    } else if (v.state === "closed" || v.state === "unavailable") {
      endPlay(v, false);
    }
  }
  // assistants : « autorisés » (ce que la TV permet) et « connectés » (vraiment installés sur la boîte) restent
  // deux nombres distincts ; une autorisation n'est jamais présentée comme une connexion
  // en-tête « Connectés : … » : la même liste canonique que les compteurs (assistants connus vraiment connectés),
  // jamais une entrée étrangère du registre ; « agents » seulement pour un service plus ancien sans cette liste
  function connectedAgents(s) {
    var l = s.assistants && s.assistants.list, out = [], i;
    if (!(l instanceof Array)) { return s.agents || []; }
    for (i = 0; i < l.length; i++) { if (l[i] && l[i].connected && AGENTS.hasOwnProperty(l[i].id)) { out.push(l[i].id); } }
    return out;
  }
  function agentsCounts() {
    var a = state && state.assistants;
    if (!a || typeof a.authorized !== "number") { return "Muse, Hermit, Hermes"; }
    if (a.unreadable) { return "État illisible : rien ne sera changé"; }
    return "Autorisés : " + a.authorized + " · Connectés : " + a.connected;
  }
  function openSettings(sel) {
    var list = [["setup", "⟳", "Reconfigurer", "Nouveau QR : Wi-Fi, nom, assistants"],
                ["agents", "◎", "Gérer les assistants", agentsCounts()]];
    if (cvOn() && CV) { list.push(["ambiance", "✦", "Ambiance OpenTV", ambianceSub()]); }
    if (state && state.remote && state.remote.enabled && state.remote.phones > 0) {
      list.push(["remote-forget", "▣", "Oublier les téléphones", "Ils devront rescanner le QR"]);
    }
    list.push(["close", "✕", "Fermer", "Revenir à l'accueil"]);
    var idx = 0;
    showPanel("settings", (state && state.device) || "OpenTV", "Réglages",
              "↑ ↓ choisir · OK ouvrir · Retour fermer. Rien ne change sans ta confirmation.",
              list.map(function (x, i) {
                var b = button("", "lrow"), t = el("span", "lt");
                b.setAttribute("data-label", x[2]);
                t.appendChild(el("span", "tl", x[2])); t.appendChild(el("span", "ts", x[3]));
                b.appendChild(el("span", "ic", x[1])); b.appendChild(t);
                if (x[0] === sel) { idx = i; }
                return b;
              }), idx, { actions: list.map(function (x) { return x[0]; }) });
  }
  function openConfirm(what) {   // choix sûr (« Non ») au focus par défaut
    if (what === "amb-undo" || what === "amb-reset") {
      var undo = what === "amb-undo";
      showPanel("confirm", "Ambiance OpenTV", undo ? "Revenir à l'ambiance précédente ?" : "Revenir à l'ambiance d'origine ?",
                undo ? "La dernière ambiance enregistrée est annulée." : "Canvas, mouvement calme. Les versions précédentes sont oubliées.",
                [button(undo ? "Oui, revenir" : "Oui, ambiance d'origine", "lrow"), button("Non", "lrow")], 1,
                { what: what, actions: [what, "no"] });
      return;
    }
    if (what === "agents") {
      showPanel("confirm", "Réglages", "Gérer les assistants ?",
                "Un QR s'affiche : choisis sur ton téléphone ce que Muse, Hermit et Hermes peuvent faire. Le Wi-Fi et le nom de la TV ne changent pas.",
                [button("Oui, afficher le QR", "lrow"), button("Non", "lrow")], 1, { what: what, actions: [what, "no"] });
      return;
    }
    var forget = what === "remote-forget";
    showPanel("confirm", "Réglages", forget ? "Oublier tous les téléphones ?" : "Afficher un nouveau QR de configuration ?",
              forget ? "Ils ne pourront plus servir de télécommande avant d'avoir rescanné le QR."
                     : "Le Wi-Fi actuel reste actif tant que tu ne le changes pas.",
              [button(forget ? "Oui, oublier" : "Oui, reconfigurer", forget ? "lrow danger" : "lrow"),
               button("Non", "lrow")], 1,
              { what: what, actions: [what, "no"] });
  }
  function paintPanel() {
    var b = $("pbtns").children;
    for (var i = 0; i < b.length; i++) {
      var base = b[i].getAttribute("data-base") || "";
      b[i].className = base + (i === panel.btn ? (base ? " " : "") + "focused" : "");
    }
  }
  function closePanel() {   // fiche/feuille fermée : sélection « Pour toi » en attente appliquée maintenant
    panel = null; $("panel").hidden = true; $("pqrfig").hidden = true;
    $("pcaps").hidden = true; $("pphases").hidden = true;
    if (!flushPending()) { paint(); }
    if (state) { renderRemote(); }
  }
  function panelBack() {
    if (panel.kind === "agents") { cancelAgents(); }
    else if (panel.kind === "musesetup") { cancelMuse(); }
    else if (panel.kind === "confirm" && (panel.what === "amb-undo" || panel.what === "amb-reset")) { openAmbiance(panel.what.slice(4)); }
    else if (panel.kind === "confirm") { if (panel.what === "agents") { openAssistants("rights"); } else { openSettings(panel.what); } }
    else if (panel.kind === "ambiance") { openSettings("ambiance"); }
    else if (panel.kind === "ambstyle") { openAmbiance("style"); }
    else if (panel.kind === "ambmotion") { openAmbiance("motion"); }
    else if (panel.kind === "assistants") { openSettings("agents"); }
    else if (panel.kind === "muse") { openAssistants("muse"); }
    else if (panel.kind === "museconfirm") { openMuse(); }
    else { closePanel(); }
  }
  // ---- « Gérer les assistants » : Muse (connexion guidée) ou droits des assistants (QR existant)
  function museTv() { return (state && state.muse) || {}; }
  function museConnected() { return museTv().bridge === "ready"; }
  // « Déconnecter Muse » : proposé dès que la boîte POSSÈDE Muse (appariement, jeton local ou identité à elle :
  // owned exactement true, publié par le helper root), pont prêt, en pause ou en échec ; jamais pour une identité
  // étrangère ni tant que la propriété n'est pas établie (owned null)
  function museOwned() { return museTv().owned === true; }
  function museSig() { var m = museTv(); return [m.owned, !!m.foreign, m.bridge || ""].join("|"); }
  function museSub() {
    var m = museTv();
    if (m.active) { return "Connexion en cours…"; }
    if (m.disconnect && m.disconnect.state === "running") { return "Déconnexion en cours…"; }
    if (m.foreign) { return "Installée autrement"; }
    if (museOwned()) {
      return m.bridge === "ready" ? "Connectée · Déconnecter" : m.bridge === "paused" ?
        "En pause · Reconnecter ou déconnecter" : "Pas connectée · Reconnecter ou déconnecter";
    }
    if (m.bridge === "ready") { return "Connectée"; }
    if (m.bridge === "paused") { return "En pause · Reconnecter"; }
    return "Connecter";
  }
  function openAssistants(sel) {
    var list = [["muse", "M", "Muse", museSub()], ["rights", "◎", "Droits des assistants", agentsCounts()],
                ["back", "←", "Retour", "Revenir aux Réglages"]];
    var idx = 0;
    showPanel("assistants", "Réglages", "Gérer les assistants",
              "Choisis un assistant. Rien ne change sans ta confirmation.",
              list.map(function (x, i) {
                var b = button("", "lrow"), t = el("span", "lt");
                b.setAttribute("data-label", x[2]);
                t.appendChild(el("span", "tl", x[2])); t.appendChild(el("span", "ts", x[3]));
                b.appendChild(el("span", "ic", x[1])); b.appendChild(t);
                if (x[0] === sel) { idx = i; }
                return b;
              }), idx, { actions: list.map(function (x) { return x[0]; }) });
  }
  function openMuse(refresh) {   // « Autoriser et connecter » n'est JAMAIS au focus par défaut : choisir Muse n'accorde rien
    var m = museTv(), sig = museSig();
    if (m.active) { openMuseSetup(); return; }
    if (!refresh) {   // état public de la boîte relu par le helper root (borné côté service) : « Déconnecter » proposé
      api("POST", "/api/tv/muse/probe", {}).catch(onApiError);   // seulement s'il est établi que la boîte possède Muse
    }
    if (m.foreign) {   // identité « muse-device » qui ne vient pas de cette boîte : jamais de déconnexion d'ici
      showPanel("muse", "Gérer les assistants", "Muse est installée autrement", MSG_MUSE_FOREIGN,
                [button("Retour", "lrow")], 0, { actions: ["back"], sig: sig });
      return;
    }
    if (museOwned() && museConnected()) {
      showPanel("muse", "Gérer les assistants", "Muse est connectée",
                "Muse peut proposer des films, apprendre tes goûts, naviguer et lancer un film quand tu le lui demandes.",
                [button("Déconnecter Muse", "lrow danger"), button("Retour", "lrow")], 1,
                { actions: ["disconnect", "back"], sig: sig });
      return;
    }
    if (museOwned()) {   // appariée à cette boîte mais en pause ou sans connexion (reconnexion échouée…) : « Retour » au focus
      var paused = m.bridge === "paused";
      showPanel("muse", "Gérer les assistants", paused ? "Muse est en pause" : "Muse n'est pas connectée",
                paused ? "Muse est installée sur cette boîte mais ne peut plus piloter la TV." :
                  "Muse est appairée à cette boîte mais ne répond pas pour l'instant.",
                [button("Reconnecter", "lrow"), button("Déconnecter Muse", "lrow danger"), button("Retour", "lrow")], 2,
                { actions: ["reconnect", "disconnect", "back"], sig: sig });
      return;
    }
    if (museConnected()) {   // pont prêt mais propriété pas encore établie : rien à déconnecter d'ici pour l'instant
      showPanel("muse", "Gérer les assistants", "Muse est connectée", "Vérification de la boîte…",
                [button("Retour", "lrow")], 0, { actions: ["back"], sig: sig });
      return;
    }
    openMuseConsent();
  }
  function openMuseConsent() {
    showPanel("muse", "Gérer les assistants", "Connecter Muse",
              "Avec ton accord, Muse pourra :", [button("Autoriser et connecter", "lrow"), button("Pas maintenant", "lrow")],
              1, { actions: ["connect", "back"], sig: museSig() });
    var ul = $("pcaps");
    ul.textContent = "";
    MUSE_CAPS.forEach(function (x) { ul.appendChild(el("li", null, x)); });
    ul.appendChild(el("li", "note", "Ton compte reste chez Muse. Retrait à tout moment ici."));
    ul.hidden = false;
  }
  function openMuseConfirm() {
    showPanel("museconfirm", "Muse", "Déconnecter Muse ?",
              "Muse ne pourra plus rien faire sur la TV. Ton jeton et tes autres gadgets Muse ne changent pas.",
              [button("Oui, déconnecter", "lrow danger"), button("Non", "lrow")], 1, { actions: ["yes", "no"] });
  }
  function startMuse() {   // consentement explicite du propriétaire : seul chemin qui envoie consent: true
    api("POST", "/api/tv/muse/start", { consent: true }).then(function (j) {
      if (!j || !j.ok) {
        closePanel();
        toast(j && j.error === "busy" ? "Muse est déjà en cours de connexion." : j && j.message ? j.message :
              "Impossible pour l'instant : la TV doit être connectée au réseau.", !!(j && j.message));
        return;
      }
      if (j.muse && j.muse.last && typeof j.muse.last.seq === "number") { museSeq = j.muse.last.seq; }
      museSkipToast = false;
      openMuseSetup();
      readState().then(function (r) { render(r.s, null, r.fresh); }).catch(onApiError);   // QR sans attendre le poll
    }).catch(function (e) {
      if (!onApiError(e)) { closePanel(); toast("Impossible pour l'instant : la TV doit être connectée au réseau."); }
    });
  }
  function openMuseSetup() {
    lastMuseQr = "";
    $("psteps").removeAttribute("data-muse");
    showPanel("musesetup", "Gérer les assistants", "Connecter Muse", "", [button("Annuler", "lrow")], 0,
              { actions: ["cancel"], openedAt: Date.now() });
    renderMuse();
  }
  function renderMuse() {
    if (!panel || panel.kind !== "musesetup") { return; }
    var m = museTv(), c = $("aqr"), ol = $("psteps"), ph = $("pphases");
    var r = MUSE_RANK[m.state] || 0;
    if (m.cancelling) { $("pmeta").textContent = m.cancel_pending ? MSG_MUSE_PENDING : "Annulation en cours…"; }
    if (!m.active) { return; }
    if (m.state === "keygen" || m.state === "await_token") {
      $("pqrfig").hidden = false; ph.hidden = true;
      if (m.url) {
        if (m.url !== lastMuseQr) { lastMuseQr = m.url; drawQR(c, m.url); }
        c.style.visibility = ""; $("aqrmsg").hidden = true;
      } else {   // clé de séance en préparation : jamais un ancien QR à l'écran
        c.style.visibility = "hidden"; $("aqrmsg").textContent = "Préparation du QR…"; $("aqrmsg").hidden = false;
      }
      if (!m.cancelling) {
        $("pmeta").textContent = m.phone ? "Téléphone connecté : suis les étapes affichées dessus." :
          "Ton téléphone doit être sur le même Wi-Fi que la TV.";
      }
      if (ol.getAttribute("data-muse") !== "1") {
        ol.textContent = "";
        ["Scanne ce QR avec l'appareil photo du téléphone.", "Colle ton jeton Muse : le même pour tous tes appareils.",
         "Termine dans l'app Muse sur l'iPhone."].forEach(function (x) { ol.appendChild(el("li", null, x)); });
        ol.setAttribute("data-muse", "1");
      }
      return;
    }
    $("pqrfig").hidden = true; ph.hidden = false;   // jeton reçu : plus de QR, l'avancement réel seulement
    ph.textContent = "";
    MUSE_PHASES.forEach(function (x, i) {
      ph.appendChild(el("li", m.state === "connected" || r > i + 1 ? "done" : r === i + 1 ? "on" : "", x[1]));
    });
    if (!m.cancelling) {
      $("pmeta").textContent = m.state === "waiting_muse" ? MUSE_PATH + (m.ble_name ? " (" + m.ble_name + ")" : "") :
        m.state === "connecting" ? "Muse est appairée : connexion…" : "La boîte prépare la connexion…";
    }
  }
  function cancelMuse() {
    var m = museTv();
    if (m.active && m.cancel_pending) {   // annulation demandée, issue pas encore confirmée : la feuille peut se fermer
      closePanel();                        // mais rien n'est dit « annulé » ; l'issue réelle sera annoncée
      museHidden = true;
      toast("Annulation demandée : le résultat s'affichera sur la TV.");
      return;
    }
    if (m.active && (MUSE_RANK[m.state] || 0) >= 1) {   // appariement lancé : la boîte ferme la fenêtre, issue annoncée
      api("POST", "/api/tv/muse/cancel", {}).then(function (j) { if (j && j.muse) { state.muse = j.muse; renderMuse(); } })
        .catch(onApiError);
      $("pmeta").textContent = "Annulation en cours…";
      return;
    }
    closePanel();
    museSkipToast = true;
    toast("Annulé : rien n'a changé.");
    api("POST", "/api/tv/muse/cancel", {}).catch(onApiError);
  }
  function onMuseResult(m) {
    var st = m.last && m.last.state, msg = (m.last && m.last.message) || m.message;
    museHidden = false;
    if (st === "cancelled" && museSkipToast) { museSkipToast = false; return; }
    museSkipToast = false;
    if (panel && (panel.kind === "musesetup" || panel.kind === "muse")) { closePanel(); }
    if (st === "connected") {
      goStart();
      toast("Muse est connectée !\nDemande-lui par exemple : « Propose-moi un film ».", true);
    } else if (st === "cancelled") { toast("Annulé : rien n'a changé."); }
    else if (st === "expired") { toast("QR de Muse expiré : rien n'a changé."); }
    else if (st === "failed") {
      if (play || (panel && panel.kind === "item")) { toast(msg || "Muse n'est pas connectée.", true); return; }
      if (m.last && m.last.error === "paired_incomplete") { openMusePaired(msg); return; }
      showPanel("museresult", "Gérer les assistants", "Muse n'est pas connectée", msg || "La connexion n'a pas abouti.",
                [button("Réessayer", "lrow"), button("Fermer", "lrow")], 0, { actions: ["retry", "close"] });
    }
  }
  // appariement réussi, puis interruption (plantage du helper pendant le démarrage du pont…) : Muse EST appairée,
  // jamais « annulé » ni « connectée » ; reprise sûre (« Reconnecter » = nouveau consentement, même jeton) ou
  // « Déconnecter Muse » (confirmation, seulement si la boîte possède Muse) ; « Fermer » au focus
  function openMusePaired(msg) {
    var acts = ["reconnect"], btns = [button("Reconnecter", "lrow")];
    if (museOwned() && !museTv().foreign) { acts.push("disconnect"); btns.push(button("Déconnecter Muse", "lrow danger")); }
    acts.push("close"); btns.push(button("Fermer", "lrow"));
    showPanel("museresult", "Gérer les assistants", "Muse est appairée",
              msg || "Muse est appairée, mais la connexion doit être reprise.", btns, btns.length - 1,
              { actions: acts, paired: msg || "", sig: museSig() });
  }
  function disconnectMuse() {
    api("POST", "/api/tv/muse/disconnect", { confirm: true }).then(function (j) {
      closePanel();
      toast(j && j.ok ? "Déconnexion de Muse…" : "Impossible pour l'instant : réessaie dans un moment.");
    }).catch(function (e) { if (!onApiError(e)) { closePanel(); toast("Impossible pour l'instant : réessaie dans un moment."); } });
  }
  // ---- « Gérer les assistants » : QR dédié, téléphone seulement ; Retour/OK (« Annuler ») ferme sans rien changer
  function openAgents(seq) {
    lastAgentsQr = "";
    // nouveau parcours : une annulation TV précédente (POST perdu) ne doit jamais masquer une annulation téléphone ;
    // tout résultat déjà émis par le service avant cette ouverture (seq de la réponse) n'est jamais annoncé
    skipCancelToast = false;
    if (typeof seq === "number" && (agentsSeq === null || seq > agentsSeq)) { agentsSeq = seq; }
    var now = Date.now();
    showPanel("agents", "Réglages", "Gérer les assistants",
              "Sur ton téléphone, même Wi-Fi que la TV. Le Wi-Fi et le nom de la TV ne changent pas.",
              [button("Annuler", "lrow")], 0, { actions: ["cancel"], openedAt: now, noQrSince: now });
    $("aqrmsg").hidden = true; $("aqrmsg").textContent = MSG_AGENTS_QR;
    var ol = $("psteps");
    ol.textContent = ""; ol.removeAttribute("data-muse");
    ["Scanne ce QR avec l'appareil photo.", "Choisis pour Muse, Hermit et Hermes.", "Confirme sur le téléphone."]
      .forEach(function (x) { ol.appendChild(el("li", null, x)); });
    renderAgents();
  }
  function renderAgents() {
    if (!panel || panel.kind !== "agents") { return; }
    var m = state && state.assistants && state.assistants.manage, c = $("aqr"), now = Date.now();
    if (m && m.active && m.url) {
      if (m.url !== lastAgentsQr) { lastAgentsQr = m.url; drawQR(c, m.url); }
      c.style.visibility = "";
      $("aqrmsg").hidden = true;
      panel.noQrSince = null; panel.noQrTimers = false;
      return;
    }
    c.style.visibility = "hidden";   // jamais un ancien QR (adresse périmée) laissé à l'écran
    if (panel.noQrSince === null || panel.noQrSince === undefined) { panel.noQrSince = now; }
    if (!panel.noQrTimers) {   // 2 contrôles différés par absence de QR (pas de boucle) : échéances tenues sans poll
      panel.noQrTimers = true;
      var p = panel;
      [AGENTS_QR_WAIT_MS, AGENTS_QR_GIVEUP_MS].forEach(function (ms) {
        setTimeout(function () { if (panel === p) { renderAgents(); } }, Math.max(0, p.noQrSince + ms - now) + 20);
      });
    }
    var waited = now - panel.noQrSince;
    $("aqrmsg").hidden = waited < AGENTS_QR_WAIT_MS;
    if (waited >= AGENTS_QR_GIVEUP_MS) { giveUpAgents(); }
  }
  function giveUpAgents() {   // feuille fermée + mode annulé côté service ; annonce honnête, aucune relance
    closePanel();
    skipCancelToast = true;
    toast(MSG_AGENTS_QR + "\nRien n'a changé.");
    api("POST", "/api/tv/agents/cancel", {}).catch(onApiError);
  }
  function cancelAgents() {
    closePanel();
    skipCancelToast = true;
    toast("Annulé : rien n'a changé.");
    api("POST", "/api/tv/agents/cancel", {}).catch(onApiError);
  }
  function onAgentsResult(kind) {
    var cancelled = kind === "cancelled";
    if (cancelled && skipCancelToast) { skipCancelToast = false; return; }
    skipCancelToast = false;
    if (panel && panel.kind !== "item") { closePanel(); }
    if (kind === "saved") {
      goStart();   // retour à l'accueil, confirmation claire et chiffrée (autorisés ≠ connectés)
      var n = agentsCounts();
      toast("C'est enregistré !\n" + (n.indexOf("Autorisés") === 0 ?
            "Assistants a" + n.slice(1).replace("Connectés", "connectés") : n), true);
    } else if (kind === "expired") { toast("QR des assistants expiré : rien n'a changé."); }
    else if (kind === "unavailable") { toast(MSG_AGENTS_QR + "\nRien n'a changé."); }
    else if (cancelled) { toast("Annulé sur le téléphone : rien n'a changé."); }
  }
  function goStart() {
    focus.row = 0; focus.cols = [];
    focus.zone = ROWS.length ? "rows" : "top";
    paint();
  }
  function act() {
    var a = panel.actions[panel.btn], it = panel.it;
    if (panel.pending) { return; }   // écriture de l'ambiance en cours : jamais deux fois
    if (a === "watch") { startPlay(it); return; }
    if (panel.kind === "settings") {
      if (a === "close") { closePanel(); } else if (a === "agents") { openAssistants(); }
      else if (a === "ambiance") { openAmbiance(); } else { openConfirm(a); }
      return;
    }
    if (ambianceAct(a)) { return; }
    if (panel.kind === "agents") { cancelAgents(); return; }
    if (panel.kind === "assistants") {
      if (a === "muse") { openMuse(); } else if (a === "rights") { openConfirm("agents"); } else { openSettings("agents"); }
      return;
    }
    if (panel.kind === "muse") {
      if (a === "connect") { startMuse(); } else if (a === "disconnect") { openMuseConfirm(); }
      else if (a === "reconnect") { openMuseConsent(); } else { openAssistants("muse"); }
      return;
    }
    if (panel.kind === "museconfirm") { if (a === "yes") { disconnectMuse(); } else { openMuse(); } return; }
    if (panel.kind === "musesetup") { cancelMuse(); return; }
    if (panel.kind === "museresult") {
      if (a === "retry") { openMuse(); } else if (a === "reconnect") { openMuseConsent(); }
      else if (a === "disconnect") { openMuseConfirm(); } else { closePanel(); }
      return;
    }
    if (panel.kind === "confirm") {
      if (a === "no") { if (panel.what === "agents") { openAssistants("rights"); } else { openSettings(panel.what); } return; }
      if (a === "agents") {
        api("POST", "/api/tv/agents", {}).then(function (j) {
          var base = j && typeof j.seq === "number" ? j.seq : null, last = j && j.last;
          // l'issue de CETTE ouverture est déjà lue (un poll arrivé avant la réponse l'a annoncée) : jamais rouvrir la
          // feuille, jamais d'annulation inutile ni de seconde annonce
          if (base !== null && agentsSeq !== null && agentsSeq > base) {
            if (panel && panel.kind === "confirm") { closePanel(); }
            return;
          }
          // la boîte a déjà fermé le mode pendant l'ouverture (écoute impossible…) : une annonce, aucune feuille
          if (base !== null && last && typeof last.seq === "number" && last.seq > base) {
            agentsSeq = last.seq;
            skipCancelToast = false;
            if (panel && panel.kind === "confirm") { closePanel(); }
            onAgentsResult(last.kind);
            return;
          }
          openAgents(base);
          readState().then(function (r) { render(r.s, null, r.fresh); }).catch(onApiError);   // QR tout de suite
        }).catch(function (e) {
          if (!onApiError(e)) { closePanel(); toast("Impossible pour l'instant : la TV doit être connectée au réseau."); }
        });
        return;
      }
      closePanel();
      if (a === "setup") { api("POST", "/api/tv/setup", {}).then(function () { toast("Nouveau QR en préparation…"); }).catch(onApiError); }
      if (a === "remote-forget") { api("POST", "/api/tv/remote/revoke", {}).then(function () { toast("Téléphones oubliés."); }).catch(onApiError); }
      return;
    }
    var m = (fy.marks || {})[it.id] || {}, t = TOGGLE[a];
    var action = m[t[0]] === t[1] ? t[2] : a;
    api("POST", "/api/tv/feedback", { id: it.id, action: action }).then(function (r) {
      if (r.ok && r.state) {
        fy.marks = fy.marks || {}; fy.marks[it.id] = r.state;
        // sélection en attente (reçue AVANT ce choix) : porte aussi le nouvel état, jamais un badge perdu
        if (fyPending && fyPending.d) { fyPending.d.marks = fyPending.d.marks || {}; fyPending.d.marks[it.id] = r.state; }
      }
      toast(r.message || "Action non enregistrée.");
      closePanel(); renderRows();
      if (r.ok) { loadForYou(true); }   // état du service relu (redessiné seulement s'il diffère, focus gardé)
    }).catch(function (e) { if (!onApiError(e)) { toast("Action non enregistrée."); } });
  }
  // ---- pilotage par assistant : une action à la fois, réclamée puis appliquée une seule fois, résultat rendu
  function runActions(s) {
    // feuille Canvas affichée : seul le propriétaire décide, aucune action d'assistant n'est même réclamée
    if (actBusy || !s || s.setup.active || cvSheet) { return; }
    var l = (s.actions && s.actions.pending) || [], next = null, i;
    for (i = 0; i < l.length; i++) { if (l[i] && typeof l[i].seq === "number" && l[i].seq > actSeq) { next = l[i]; break; } }
    if (!next) { return; }
    actBusy = true;
    // heure d'ENVOI de la réclamation : l'âge mesuré par le service s'y retranche, jamais la latence de la réponse
    // (une page lente ne peut donc que reculer l'heure estimée de mise en file — en faveur de la touche humaine)
    var seq = next.seq, sentAt = Date.now();
    api("POST", "/api/tv/action", { seq: seq, phase: "claim" }).then(function (r) {
      actSeq = Math.max(actSeq, seq);
      if (!r || !r.ok || !r.action || r.action.seq !== seq) { return null; }   // déjà prise, expirée, remplacée
      var why = null;
      try { why = applyAction(r.action, sentAt); } catch (e) { why = "error"; }
      // fiche ouverte par l'action : le service connaît l'occupation « panel » AVANT d'apprendre qu'elle est faite
      return syncBusy().then(function () {
        return api("POST", "/api/tv/action", { seq: seq, phase: "done", ok: !why, reason: why || null });
      });
    }).catch(function (e) { onApiError(e); actSeq = Math.max(actSeq, seq); })
      .then(function () { actBusy = false; if (state) { runActions(state); } });
  }
  function validItem(it, id) {
    return !!(it && typeof it === "object" && it.id === id && typeof it.title === "string" && it.title);
  }
  // touche humaine (télécommande, clavier, téléphone) moins de HUMAN_PRIORITY_MS avant la mise en file d'une action
  // d'assistant, ou depuis : l'humain gagne (jamais de focus ni de fiche volés à Alex pendant qu'il navigue)
  function humanRecent(queuedAt) { return !!(lastHumanAt && lastHumanAt > queuedAt - HUMAN_PRIORITY_MS); }
  function applyAction(x, sentAt) {   // -> null (appliquée) | code de refus
    if (!state || state.setup.active) { return "tv_setup_active"; }
    if (cvSheet) { return "tv_busy"; }   // réclamée juste avant la feuille Canvas : jamais appliquée par-dessus
    // heure de mise en file estimée sur l'horloge de la page (au plus tôt : la latence favorise l'humain)
    var queuedAt = (sentAt || Date.now()) - (typeof x.age_ms === "number" ? Math.max(0, x.age_ms) : 0);
    if (x.kind === "key") { return agentKey(x.key, queuedAt); }
    if (!validItem(x.item, x.id)) { return "bad_item"; }
    var prop = { from: x.from, text: typeof x.text === "string" ? x.text : null };
    if (x.kind === "present") {   // change le focus et ouvre une fiche : même priorité humaine que les touches
      if (play) { return "player_active"; }                          // jamais de vol du focus du lecteur
      if (panel && panel.kind !== "item") { return "tv_busy"; }      // Réglages / assistants : l'humain d'abord
      if (humanRecent(queuedAt)) { return "human_active"; }          // fiche/carte qu'Alex vient d'ouvrir : gardée
      presentItem(x.item, prop);
      return null;
    }
    // « play » : la lecture est DÉJÀ acceptée côté service (play_title = demande explicite d'Alex, portes de
    // « Regarder ») et le lecteur va apparaître quoi qu'il arrive ; la page montre donc honnêtement l'écran de
    // chargement puis revient sur la fiche du film. Fermer (close_player) reste toujours permis.
    if (x.kind === "play") { return agentPlay(x, prop); }
    return "bad_kind";
  }
  function presentItem(it, prop) {   // fiche du film : sa carte au focus si elle est dans les rangées, sinon par-dessus
    if (it.marks && fy) { fy.marks = fy.marks || {}; if (!fy.marks[it.id]) { fy.marks[it.id] = it.marks; } }
    if (panel) { panel = null; $("panel").hidden = true; $("pqrfig").hidden = true; }
    var hit = findItem(it.id, null);
    if (hit) { focus.zone = "rows"; focus.auto = false; focus.row = hit.row; focus.cols[hit.row] = hit.col; }
    paint();
    openItem(hit ? ROWS[hit.row].items[hit.col] : it, null, prop);
  }
  function agentPlay(x, prop) {   // lecture DÉJÀ acceptée côté service (Playback.start + portes) : écran OpenTV
    // même film que la fiche proposée ouverte : sa phrase est gardée pour la fiche rouverte au retour
    if (!prop.text && panel && panel.prop && panel.it && panel.it.id === x.id) { prop = panel.prop; }
    if (panel && panel.kind !== "item") { panel = null; $("panel").hidden = true; $("pqrfig").hidden = true; }
    var pb = state && state.playback, minSeq = typeof x.playback_seq === "number" ? x.playback_seq : 0;
    if (pb && typeof pb.seq === "number" && pb.seq <= playEndSeq && pb.state !== "loading" && pb.state !== "playing") {
      if (!play) { presentItem(x.item, prop); }   // déjà terminée et acquittée : la fiche seulement
      return null;
    }
    if (!play) {
      presentItem(x.item, prop);
      play = { it: panel && panel.it ? panel.it : x.item, row: focus.row, col: col(),
               key: ROWS[focus.row] ? rowKey(ROWS[focus.row], focus.row) : null, seq: -1, done: false,
               restored: false, minSeq: minSeq, prop: prop };
      closePanel();
      showPlayOverlay(x.item.title);
      pollPlay();
    } else if (!play.it) {   // page rechargée / état vu avant l'action : la carte d'origine est complétée
      play.it = x.item; play.prop = prop; play.minSeq = minSeq;
      if (!play.restored) { showPlayOverlay(x.item.title); }
    }
    return null;
  }
  function agentKey(a, queuedAt) {   // touche d'un assistant : jamais prioritaire, jamais Réglages ni « Regarder »
    if (REMOTE_ACTIONS[a] !== 1) { return "bad_key"; }
    if (play) { return "player_active"; }   // Retour/Accueil pendant une lecture : fermés côté service
    if (humanRecent(queuedAt)) { return "human_active"; }
    if (panel && panel.kind !== "item") {
      // QR d'assistants, connexion de Muse (consentement, QR, confirmation) : jamais une touche d'assistant
      if ({ agents: 1, assistants: 1, muse: 1, museconfirm: 1, musesetup: 1, museresult: 1 }[panel.kind]) { return "tv_busy"; }
      if (a !== "back" && a !== "home") { return "settings_forbidden"; }
    } else if (!panel && focus.zone === "top" && a === "ok") { return "settings_forbidden"; }
    else if (panel && a === "ok" && panel.actions[panel.btn] === "watch") { return "use_play_title"; }
    handle(a);
    return null;
  }
  // ---- OpenTV Canvas : scène d'ambiance publiée, aperçu d'un assistant à confirmer, Réglages « Ambiance OpenTV ».
  // Le rendu appartient à canvas.js (classes fermées, aucune requête) ; ici, le cycle de vie avec le service :
  // - chaque touche ordinaire (télécommande, clavier, téléphone, souris) envoie POST /api/tv/canvas/event AVANT d'agir
  //   (« remote » pour le téléphone : le service ne la voit pas autrement). Aperçu vivant connu (sans feuille) : la
  //   touche n'agit qu'après la confirmation de son annulation (CV_GATE_MS au plus), sinon elle est seulement consommée ;
  // - l'ensemble occupé de la page (panneau, page cachée, hors ligne) est déclaré COMPLET par POST
  //   /api/tv/canvas/ui {busy, page, seq}, une requête à la fois, le plus récent gagnant : `page` identifie CE
  //   chargement (32 hex aléatoires, aucun secret), `seq` croît à chaque envoi ; le service n'applique qu'un numéro
  //   plus récent de la page active (une liste vide tardive ou une ancienne page n'écrase jamais l'état courant).
  //   Échec, échéance ou réponse « stale » : plus aucun accusé, l'ensemble courant repart ; et il est ré-envoyé
  //   tel quel au moins toutes les CV_BUSY_LEASE_MS (bail : répare toute perte, même sans changement) ;
  // - la feuille de confirmation (« Non » au focus) n'est ni un panneau ni un geste ordinaire : ses touches ←/→/↑/↓,
  //   OK et Retour n'envoient ni événement ni occupation. L'aperçu n'est peint QUE pendant qu'elle est affichée, ses
  //   rangées jamais (mémoire spatiale) ; l'ordre de la scène publiée attend la fin de toute fiche/feuille/lecture.
  //   Pendant la feuille : ambiance figée (raison de pause locale « sheet »), son choix est le seul focus visible
  //   (carte/Réglages mémorisés, sans anneau) et l'aide du bas nomme ses touches (OK = choix au focus, Retour = Non).
  // - aucune action d'assistant n'est réclamée ni appliquée pendant la feuille ; « restore_failed » (see_state) montre
  //   l'état réel relu par le service, jamais « rien n'a changé ».
  // - réponse d'une route qui fait foi (confirm, cancel, apply, rollback, reset) : adoptée seulement si elle est la plus
  //   récente ENVOYÉE, et chaque adoption incrémente la génération cvGen. Un état (/api/tv/state) n'apporte sa vue
  //   Canvas que si cvGen n'a pas bougé depuis son ENVOI (et s'il est plus récent que le dernier état adopté) : un poll
  //   parti avant une confirmation et arrivé après ne repeint jamais l'ancienne ambiance ni ne reconstruit les rangées.
  //   Jamais la révision du magasin pour en juger (« Ambiance d'origine » la fait reculer).
  // - toute requête Canvas est bornée (CV_ROUTE_MS) : un « Oui » sans réponse ne piège jamais la télécommande ; issue
  //   ambiguë => état réel relu, jamais « rien n'a changé ».
  var CV = window.OpenTVCanvas || null;
  var CV_GATE_MS = 800, CV_SHEET_MARGIN_S = 3, CV_ROUTE_MS = 5000;
  var cvGen = 0, cvTicket = 0, cvRouteAt = 0, cvGetAt = 0;
  var cvPub = null, cvSheet = null, cvSheetTimer = null, cvDone = {}, cvGate = null;
  var humanGen = 0, evPending = 0, rowOrder = null, rowOrderSig = "";
  var CV_BUSY_LEASE_MS = 9000, CV_BUSY_RETRY_MS = 2000;
  var busyPage = newPageId(), busySeq = 0, busyAcked = null, busyFailed = null, busySentAt = 0;
  var busyFlight = false, busyWait = [], busyTimer = null, cvOwnerLock = false;
  var MSG_CV_SEE = "L'ambiance n'a peut-être pas été enregistrée comme prévu.\nL'écran montre l'ambiance réellement active.";
  var MSG_CV_LOST = "Réponse perdue : vérifie l'ambiance affichée (Réglages → Ambiance OpenTV).";
  var MSG_CV_CHECK = "Vérification de l'ambiance en cours : réessaie dans un instant.";
  var MSG_CV_UNAVAILABLE = "Ambiance indisponible pour l'instant : rien n'a changé.";
  var MSG_CV_UNAVAILABLE_LONG = "Ambiance indisponible sur cette boîte : l'ambiance de base reste affichée et ne peut pas " +
    "être modifiée pour l'instant. Si cela dure, redémarre la boîte OpenTV.";
  var CV_ERR = { no_change: "Déjà active : rien n'a changé.", nothing_to_rollback: "Aucune version précédente : rien n'a changé.",
                 store_corrupt: "Ambiance illisible : choisis « Ambiance d'origine ». Rien n'a changé.",
                 store_busy: "OpenTV est occupé : réessaie. Rien n'a changé." };
  var STYLES = [["canvas", "Canvas", "Aurore et halo du titre · d'origine"], ["mist", "Brume", "La côte brumeuse d'OpenTV, en mouvement"],
                ["cinema", "Cinéma", "Salle sombre et halo de l'affiche"]];
  var STYLE_WP = { canvas: "aurore", mist: "brume", cinema: "affiche" };
  var MOTIONS = [["off", "Coupé", "Fond fixe, aucune animation"], ["calm", "Calme", "Mouvement lent · d'origine"],
                 ["alive", "Vivant", "Plus d'ambiance"]];
  // horloge monotone (échéance de la feuille) : performance.now(), sinon Date.now rendu monotone (recul ignoré)
  var perf = window.performance && typeof window.performance.now === "function" ? window.performance : null;
  var monoWall = null, monoAcc = 0;
  function mono() {
    if (perf) { return perf.now(); }
    var w = Date.now(), d = monoWall === null ? 0 : w - monoWall;
    monoWall = w;
    monoAcc += d > 0 ? Math.min(d, 1000) : 0;
    return monoAcc;
  }
  function cvOn() { return !!(state && state.canvas); }   // service Canvas (sinon : OpenTV v23 inchangé)
  function capi(path, body) {   // POST Canvas : la réponse JSON fait foi même en 409/503 (outcome, canvas relu)
    // échéance CV_ROUTE_MS : requête abandonnée (AbortController si présent) et promesse rejetée « timeout » ; une
    // réponse plus tardive n'est jamais lue
    var ctl = typeof AbortController === "function" ? new AbortController() : null, timer = null;
    var req = fetch(path, { method: "POST", headers: { "X-OpenTV-TV": KEY, "Content-Type": "application/json" },
                            body: JSON.stringify(body), cache: "no-store", signal: ctl ? ctl.signal : undefined })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (r.status === 403 && j && j.error === "tv_key") { var e = new Error("tv_key"); e.tvKey = true; throw e; }
          if (!j || typeof j !== "object") { throw new Error(r.status); }
          return j;
        });
      });
    var limit = new Promise(function (res, rej) {
      timer = setTimeout(function () {
        var e = new Error("timeout");
        e.timeout = true;
        if (ctl) { try { ctl.abort(); } catch (x) { /* déjà terminée */ } }
        rej(e);
      }, CV_ROUTE_MS);
    });
    return Promise.race([req, limit]).then(function (j) { clearTimeout(timer); return j; },
                                           function (e) { clearTimeout(timer); throw e; });
  }
  // route Canvas qui fait foi : sa vue est adoptée seulement si aucune route envoyée APRÈS elle n'a déjà été adoptée ;
  // l'adoption change la génération (tout état lu avant n'apporte plus sa vue Canvas)
  function cvRoute(path, body) {
    var t = ++cvTicket;
    return capi(path, body).then(function (j) {
      var c = j.canvas;
      if (t > cvRouteAt && c && typeof c === "object") {
        cvRouteAt = t;
        cvGen += 1;
        cvAdopt(c);
      }
      return j;
    });
  }
  // seule lecture de /api/tv/state : génération capturée à l'ENVOI -> {s, fresh} ; fresh = aucune route adoptée
  // depuis l'envoi et aucun état plus récemment envoyé déjà adopté
  function readState() {
    var g = cvGen, t = ++cvTicket;
    return api("GET", "/api/tv/state").then(function (s) {
      var fresh = g === cvGen && t > cvGetAt;
      if (fresh) { cvGetAt = t; }
      return { s: s, fresh: fresh };
    });
  }
  // ---- occupation de la page : ensemble complet daté, une seule requête en vol, toujours le plus récent en dernier
  function newPageId() {   // 32 hex minuscules : aléa cryptographique si présent (jamais un secret, seulement unique)
    var b = [], i, h = "", c = window.crypto;
    try { var u = new Uint8Array(16); c.getRandomValues(u); for (i = 0; i < 16; i++) { b.push(u[i]); } }
    catch (e) { b = []; for (i = 0; i < 16; i++) { b.push(Math.floor(Math.random() * 256)); } }
    for (i = 0; i < 16; i++) { h += (b[i] < 16 ? "0" : "") + b[i].toString(16); }
    return h;
  }
  function pageBusy() {
    var b = [];
    if (panel) { b.push("panel"); }
    if (document.hidden) { b.push("hidden"); }
    if (!$("offline").hidden) { b.push("offline"); }
    return b;
  }
  function busySettle(ok) { var w = busyWait; busyWait = []; w.forEach(function (f) { f(ok); }); }
  function busyLater(ms) {
    clearTimeout(busyTimer);
    busyTimer = setTimeout(function () { busyTimer = null; pumpBusy(); }, Math.max(50, ms));
  }
  function pumpBusy() {
    if (busyFlight || !cvOn()) { return; }
    var want = pageBusy(), sig = want.join(","), age = mono() - busySentAt;
    // accusé valable et bail encore frais : réglé, prochain ré-envoi à l'échéance du bail
    if (sig === busyAcked && age < CV_BUSY_LEASE_MS) { busySettle(true); busyLater(CV_BUSY_LEASE_MS - age); return; }
    clearTimeout(busyTimer);
    busyTimer = null;
    busyFlight = true;
    busySeq += 1;
    busySentAt = mono();
    capi("/api/tv/canvas/ui", { busy: want, page: busyPage, seq: busySeq })
      .then(function (j) {
        // « stale » sur l'envoi le plus récent : cette page a été remplacée (une ancienne requête d'un autre
        // chargement a pris la main) — nouvel identifiant, la page courante reprend la main au prochain envoi
        if (j.ok && j.stale) { busyPage = newPageId(); busySeq = 0; }
        return !!(j.ok && !j.stale);
      }, function (e) { onApiError(e); return false; })
      .then(function (ok) {
        busyFlight = false;
        busyAcked = ok ? sig : null;    // échec, échéance, périmé : jamais d'accusé gardé, l'ensemble courant repart
        busyFailed = ok ? null : sig;
        if (!ok) {
          busySettle(false);            // personne n'attend indéfiniment
          // ensemble changé pendant l'envoi : le plus récent part tout de suite ; sinon nouvel essai borné
          if (pageBusy().join(",") !== sig) { pumpBusy(); } else { busyLater(CV_BUSY_RETRY_MS); }
          return;
        }
        pumpBusy();   // l'ensemble a changé pendant l'envoi : le plus récent part maintenant (sinon : réglé + bail)
      });
  }
  function syncBusy() {   // -> promesse : true quand le service connaît l'ensemble courant (false : échec)
    if (!cvOn()) { return Promise.resolve(true); }
    return new Promise(function (res) { busyWait.push(res); pumpBusy(); });
  }
  function noteBusy() {   // changement visible : envoi immédiat, sauf l'ensemble qui vient d'échouer (essai borné)
    var sig = cvOn() && !busyFlight ? pageBusy().join(",") : null;
    if (sig !== null && sig !== busyAcked && sig !== busyFailed) { pumpBusy(); }
  }
  // ---- gestes humains ordinaires
  function humanEvent(kind) {
    humanGen += 1;   // tout état demandé avant ce geste ne peut plus ouvrir de feuille
    if (!cvOn()) { return Promise.resolve(true); }
    evPending += 1;
    return capi("/api/tv/canvas/event", { kind: kind }).then(function (j) { return !!j.ok; },
                                                          function (e) { onApiError(e); return false; })
      .then(function (ok) { evPending -= 1; return ok; });
  }
  function previewKnown() {
    var p = cvOn() && state.canvas.preview;
    return p && typeof p.id === "string" && !cvDone[p.id] ? p.id : null;
  }
  function humanKey(kind, a, repeat) {   // -> true si la touche est consommée
    lastHumanAt = Date.now();
    if (cvGate) { return true; }   // annulation d'un aperçu en cours d'établissement : touche consommée
    var pid = previewKnown();
    if (!pid) {
      // une répétition tenue suit l'événement déjà en vol (borné : une requête à la fois pour une touche tenue)
      if (!(repeat && evPending)) { humanEvent(kind); } else { humanGen += 1; }
      return handle(a);
    }
    // aperçu vivant connu : jamais d'action avant que le service confirme son annulation
    var gate = cvGate = { a: a };
    cvDone[pid] = true;
    renderCanvas();
    var t = setTimeout(function () { if (cvGate === gate) { cvGate = null; } }, CV_GATE_MS);
    humanEvent(kind).then(function (ok) {
      if (cvGate !== gate) { return; }   // délai dépassé : la touche reste consommée, l'interface jamais figée
      cvGate = null;
      clearTimeout(t);
      if (ok) { handle(a); }
    });
    return true;
  }
  function remoteKey(a) {   // téléphone appairé : même règle, événement « remote » (le service ne le voit pas)
    if (offlineShown()) { lastHumanAt = Date.now(); return; }   // voile « Reconnexion… » : rien d'invisible
    if (cvSheet) { lastHumanAt = Date.now(); sheetKey(a); return; }
    humanKey("remote", a, false);
  }
  // ---- scène publiée et rendu
  function cvValid(sc) { return !!(CV && CV.valid(sc)); }
  function cvAdopt(c) {   // vue Canvas du service (état, réponse d'une route) : scène publiée et ordre des rangées
    if (!c || typeof c !== "object") { return; }
    if (state) { state.canvas = c; }
    cvPub = cvValid(c.scene) ? c.scene : null;
    if (takeRowOrder() && fy) { renderRows(); }
    renderCanvas();
  }
  function wantRowOrder() {
    var o = cvPub && cvPub.row_order;
    return o instanceof Array && o.length ? o.join(",") : "";
  }
  function takeRowOrder() {   // nouvel ordre publié adopté seulement hors fiche/feuille/lecture -> true si changé
    var sig = wantRowOrder();
    if (sig === rowOrderSig || fyBusy()) { return false; }
    rowOrderSig = sig;
    rowOrder = sig ? sig.split(",") : null;
    return true;
  }
  // rangées connues de la scène réordonnées ENTRE ELLES dans leurs emplacements ; une rangée hors schéma (« because »…)
  // garde exactement sa place : l'ordre par défaut rend donc l'ordre du service à l'identique
  function orderRows(rows) {
    if (!rowOrder) { return rows; }
    var rank = {}, known = [], k = 0, i;
    for (i = 0; i < rowOrder.length; i++) { rank[rowOrder[i]] = i; }
    function isKnown(r) { return typeof r.id === "string" && rank.hasOwnProperty(r.id); }
    rows.forEach(function (r, n) { if (isKnown(r)) { known.push({ r: r, n: n }); } });
    known.sort(function (x, y) { return rank[x.r.id] - rank[y.r.id] || x.n - y.n; });
    return rows.map(function (r) { return isKnown(r) ? known[k++].r : r; });
  }
  function pbState() { return (state && state.playback && state.playback.state) || "idle"; }
  function cvPauses() {
    var r = [];
    if (state && state.setup && state.setup.active) { r.push("setup"); }
    if (panel) { r.push("panel"); }
    if (cvSheet) { r.push("sheet"); }   // feuille Canvas : aperçu figé (jamais déclaré au service comme occupation)
    if (play || { loading: 1, playing: 1, closing: 1, closed: 1, unavailable: 1 }[pbState()] === 1) { r.push("playback"); }
    if (!$("offline").hidden) { r.push("offline"); }
    if (document.hidden) { r.push("hidden"); }
    return r;
  }
  function renderCanvas() {
    if (!CV) { return; }
    CV.apply(cvSheet ? cvSheet.scene : cvPub, { on: cvOn(), preview: !!cvSheet,
                                               setup: !!(state && state.setup && state.setup.active) });
    CV.pause(cvPauses());
  }
  function cvPaint() {   // après chaque changement d'état visible : occupation déclarée, animation figée ou non
    noteBusy();
    if (CV && cvOn()) { CV.pause(cvPauses()); }
  }
  // ---- feuille de confirmation du propriétaire (aperçu proposé par un assistant)
  function sheetBlocked() {
    return !!(panel || play || cvGate || (state && state.setup && state.setup.active) || !$("offline").hidden ||
              document.hidden || { loading: 1, playing: 1, closing: 1, closed: 1, unavailable: 1 }[pbState()] === 1);
  }
  function cvState(c, gen) {
    if (!c) { if (cvSheet) { closeSheet(); } return; }
    var pv = c.preview, sh = c.sheet;
    if (cvSheet) {
      if (cvSheet.busy) { return; }   // « Oui » en vol : sa réponse décide
      if (!pv || pv.id !== cvSheet.id) {   // retiré ailleurs (agent, échéance, état occupé) : rien n'a été publié
        cvDone[cvSheet.id] = true;
        closeSheet();
        toast("Proposition d'ambiance retirée : rien n'a changé.");
      } else if (!sh || sheetBlocked()) {
        sheetChoose("no", "timeout");
      }
      return;
    }
    if (!sh || !pv || sh.preview_id !== pv.id || cvDone[pv.id] || gen !== humanGen || evPending || sheetBlocked() ||
        !cvValid(pv.scene) || typeof sh.expires_in_s !== "number" || sh.expires_in_s - CV_SHEET_MARGIN_S < 1) {
      return;
    }
    openSheet(sh, pv);
  }
  function openSheet(sh, pv) {
    cvSheet = { id: pv.id, scene: pv.scene, btn: 0, busy: false,
                deadline: mono() + (sh.expires_in_s - CV_SHEET_MARGIN_S) * 1000 };
    $("cvkicker").textContent = "Proposé par " + (AGENTS.hasOwnProperty(sh.proposed_by) ? AGENTS[sh.proposed_by] : "un assistant");
    var ul = $("cvchanges"), rows = CV.describe(sh.changes);
    ul.textContent = "";
    rows.forEach(function (x) {
      var li = el("li");
      li.appendChild(el("b", null, x.label + " : "));
      li.appendChild(document.createTextNode(x.from ? x.from + " → " + x.to : x.to));
      ul.appendChild(li);
    });
    var box = $("cvbtns");
    box.textContent = "";
    [["no", "Non, garder l'ambiance actuelle"], ["yes", "Oui, appliquer"]].forEach(function (b) {
      var x = button(b[1], "lrow");
      x.setAttribute("data-choice", b[0]);
      box.appendChild(x);
    });
    $("cvsheet").hidden = false;
    paintSheet();
    tickSheet();
    renderCanvas();
    paint();
  }
  function paintSheet() {
    var b = $("cvbtns").children;
    for (var i = 0; i < b.length; i++) { b[i].className = "lrow" + (cvSheet && i === cvSheet.btn ? " focused" : ""); }
    help();   // aide du bas : OK nomme le choix au focus
  }
  function tickSheet() {
    clearTimeout(cvSheetTimer);
    cvSheetTimer = null;
    if (!cvSheet) { return; }
    var left = cvSheet.deadline - mono();
    if (left <= 0) { if (!cvSheet.busy) { sheetChoose("no", "timeout"); } return; }
    $("cvcount").textContent = "Sans réponse dans " + Math.ceil(left / 1000) + " s : « Non ».";
    cvSheetTimer = setTimeout(tickSheet, Math.max(50, Math.min(1000, left)));
  }
  function closeSheet() {
    if (!cvSheet) { return; }
    cvSheet = null;
    clearTimeout(cvSheetTimer);
    cvSheetTimer = null;
    $("cvsheet").hidden = true;
    renderCanvas();
    if (!flushPending()) { paint(); }
    if (state) { runActions(state); }
  }
  function sheetKey(a) {   // touches de consentement : jamais d'événement ordinaire, jamais d'occupation
    // voile hors-ligne visible : la feuille n'est plus vue, aucune de ses touches n'agit (quelle que soit la requête)
    if (!cvSheet || cvSheet.busy || offlineShown()) { return true; }
    if (a === "left" || a === "up") { cvSheet.btn = 0; paintSheet(); }
    else if (a === "right" || a === "down") { cvSheet.btn = 1; paintSheet(); }
    else if (a === "ok") { sheetChoose(cvSheet.btn === 1 ? "yes" : "no", "owner"); }
    else if (a === "back" || a === "home") { sheetChoose("no", "owner"); }
    return true;
  }
  function sheetChoose(choice, why) {
    var sh = cvSheet;
    if (!sh || sh.busy) { return; }
    cvDone[sh.id] = true;
    if (choice !== "yes") {   // « Non », Retour, échéance ou hors-ligne : aperçu retiré, scène publiée inchangée
      closeSheet();
      cvRoute("/api/tv/canvas/cancel", { preview_id: sh.id }).catch(onApiError);   // borné, jamais bloquant
      toast(why === "timeout" ? "Sans réponse : l'ambiance ne change pas." :
            why === "offline" ? "Connexion perdue : proposition d'ambiance refusée, l'ambiance ne change pas." :
            "Ambiance refusée : rien n'a changé.");
      return;
    }
    sh.busy = true;
    cvRoute("/api/tv/canvas/confirm", { preview_id: sh.id, confirm: true }).then(function (j) {
      closeSheet();   // vue de la réponse déjà adoptée (cvRoute) : rangées réordonnées une seule fois, ici
      if (j.ok) { toast("Nouvelle ambiance appliquée.\nRéglages → Ambiance OpenTV → Version précédente pour revenir.", true); }
      else if (j.outcome === "see_state") { toast(MSG_CV_SEE, true); }
      else if (j.error === "unknown_preview" || j.error === "preview_expired") { toast("Proposition expirée : rien n'a changé."); }
      else { toast("Ambiance non appliquée : rien n'a changé."); }
    }).catch(function (e) {
      if (onApiError(e)) { return; }
      closeSheet();
      toast(MSG_CV_LOST, true);
      refreshState();
    });
  }
  function refreshState() { readState().then(function (r) { render(r.s, null, r.fresh); }).catch(onApiError); }
  // ---- Réglages « Ambiance OpenTV » (propriétaire seulement : jamais une touche d'assistant, écritures confirmées)
  function cvView() { return (state && state.canvas) || {}; }
  function styleOf(sc) { var w = sc && sc.wallpaper; return w === "brume" ? "mist" : w === "affiche" ? "cinema" : "canvas"; }
  function styleName() { var s = styleOf(cvPub); return STYLES.filter(function (x) { return x[0] === s; })[0][1]; }
  function motionName() {
    var m = (cvPub && cvPub.motion) || "calm";
    return (MOTIONS.filter(function (x) { return x[0] === m; })[0] || MOTIONS[1])[1];
  }
  function ambianceSub() {
    var c = cvView();
    if (c.status === "corrupt") { return "Illisible : revenir à l'ambiance d'origine"; }
    if (c.status === "unavailable") { return "Indisponible : ambiance d'origine"; }
    return styleName() + " · mouvement " + motionName().toLowerCase();
  }
  function listButtons(list, sel) {
    var idx = 0, btns = list.map(function (x, i) {
      var b = button("", "lrow"), t = el("span", "lt");
      b.setAttribute("data-label", x[2]);
      t.appendChild(el("span", "tl", x[2])); t.appendChild(el("span", "ts", x[3]));
      b.appendChild(el("span", "ic", x[1])); b.appendChild(t);
      if (x[0] === sel) { idx = i; }
      return b;
    });
    return { btns: btns, idx: idx, actions: list.map(function (x) { return x[0]; }) };
  }
  function openAmbiance(sel) {
    var c = cvView(), list = [];
    // magasin indisponible (toute écriture échouerait, « Ambiance d'origine » comprise) : aucune écriture proposée
    if (c.status !== "unavailable") {
      list.push(["style", "◐", "Style", "Actuel : " + styleName()]);
      list.push(["motion", "≈", "Mouvement du fond", "Actuel : " + motionName()]);
      if (c.can_rollback) { list.push(["undo", "↩", "Version précédente", "Revenir à l'ambiance d'avant"]); }
      list.push(["reset", "↺", "Ambiance d'origine", "Canvas, mouvement calme"]);
    }
    list.push(["back", "←", "Retour", "Revenir aux Réglages"]);
    var text = c.status === "corrupt" ? "Ambiance enregistrée illisible : l'écran montre l'ambiance d'origine. « Ambiance d'origine » la répare." :
      c.status === "unavailable" ? MSG_CV_UNAVAILABLE_LONG :
      "Fond et couleurs de l'accueil. Rien ne change sans ton choix ici ou ton « Oui » sur la TV.";
    var l = listButtons(list, sel);
    showPanel("ambiance", "Réglages", "Ambiance OpenTV", text, l.btns, l.idx, { actions: l.actions });
  }
  function openAmbStyle() {
    var cur = styleOf(cvPub), list = STYLES.map(function (x) {
      return [x[0], x[0] === cur ? "✓" : "◐", x[1], x[2]];
    });
    list.push(["back", "←", "Retour", "Revenir à l'ambiance"]);
    var l = listButtons(list, cur);
    showPanel("ambstyle", "Ambiance OpenTV", "Style", "OK applique le style choisi ; le mouvement du fond est gardé.",
              l.btns, l.idx, { actions: l.actions });
  }
  function openAmbMotion() {
    var cur = (cvPub && cvPub.motion) || "calm", list = MOTIONS.map(function (x) {
      return [x[0], x[0] === cur ? "✓" : "≈", x[1], x[2]];
    });
    list.push(["back", "←", "Retour", "Revenir à l'ambiance"]);
    var l = listButtons(list, cur);
    showPanel("ambmotion", "Ambiance OpenTV", "Mouvement du fond",
              "« Réduire les animations » du système et une lecture fluide passent toujours avant.", l.btns, l.idx,
              { actions: l.actions });
  }
  // écriture du propriétaire (confirmation littérale de l'API) ; la réponse porte l'état relu du service. Une seule
  // à la fois, toutes feuilles confondues : réponse perdue (échéance) => issue inconnue, aucune nouvelle écriture
  // tant qu'un état demandé APRÈS n'a pas été relu (jamais un doublon, ex. deux « Version précédente »)
  function ownerCanvas(action, body, okMsg, sel) {
    if (!panel || panel.pending) { return; }
    if (cvOwnerLock) { toast(MSG_CV_CHECK); return; }
    panel.pending = true;
    cvOwnerLock = true;
    body.confirm = true;
    var p = panel;
    cvRoute("/api/tv/canvas/" + action, body).then(function (j) {   // vue de la réponse adoptée par cvRoute
      cvOwnerLock = false;
      if (panel === p) { openAmbiance(sel); }   // feuille Ambiance à jour (l'ordre des rangées attend sa fermeture)
      if (j.ok) { toast(okMsg); }
      else if (j.outcome === "see_state") { toast(MSG_CV_SEE, true); }
      else if (cvView().status === "unavailable") { toast(MSG_CV_UNAVAILABLE); }
      else { toast(CV_ERR[j.error] || "Impossible pour l'instant : rien n'a changé."); }
    }).catch(function (e) {
      if (onApiError(e)) { return; }   // clé périmée : la page se recharge (verrou gardé jusque-là)
      toast(MSG_CV_LOST, true);
      ownerResolve(p, sel);
    });
  }
  function ownerResolve(p, sel) {   // issue inconnue : verrou levé seulement après un état réel relu
    readState().then(function (r) {
      render(r.s, null, r.fresh);
      cvOwnerLock = false;
      if (panel === p) { openAmbiance(sel); }   // l'ambiance réellement active, jamais une issue devinée
    }).catch(function (e) { if (!onApiError(e)) { setTimeout(function () { ownerResolve(p, sel); }, 2000); } });
  }
  function ambianceAct(a) {   // -> true si l'action relève de l'ambiance
    if (panel.kind === "ambiance") {
      if (a === "style") { openAmbStyle(); } else if (a === "motion") { openAmbMotion(); }
      else if (a === "undo" || a === "reset") { openConfirm("amb-" + a); } else { openSettings("ambiance"); }
      return true;
    }
    if (panel.kind === "ambstyle") {
      if (a === "back") { openAmbiance("style"); return true; }
      if (styleOf(cvPub) === a) { toast(CV_ERR.no_change); return true; }
      ownerCanvas("apply", { changes: { preset: a, motion: (cvPub && cvPub.motion) || "calm" } },
                  "Style « " + STYLES.filter(function (x) { return x[0] === a; })[0][1] + " » appliqué.", "style");
      return true;
    }
    if (panel.kind === "ambmotion") {
      if (a === "back") { openAmbiance("motion"); return true; }
      ownerCanvas("apply", { changes: { motion: a } }, "Mouvement du fond : " +
                  MOTIONS.filter(function (x) { return x[0] === a; })[0][1].toLowerCase() + ".", "motion");
      return true;
    }
    if (panel.kind === "confirm" && (panel.what === "amb-undo" || panel.what === "amb-reset")) {
      if (a === "no") { openAmbiance(panel.what.slice(4)); return true; }
      if (a === "amb-undo") { ownerCanvas("rollback", {}, "Ambiance précédente rétablie.", "style"); }
      else { ownerCanvas("reset", {}, "Ambiance d'origine rétablie.", "style"); }
      return true;
    }
    return false;
  }
  // ---- télécommande : actions normalisées par keys.js (clavier, télécommande USB 2,4 GHz, HDMI-CEC)
  var KEYS = window.OpenTVKeys || { normalize: function () { return null; }, isBrowserNav: function () { return false; } };
  function handle(a) {   // -> true si l'action a été consommée
    if (!state || state.setup.active) { return false; }   // configuration : pilotée par le téléphone seulement
    if (REMOTE_ACTIONS[a] !== 1) { return false; }
    if (play) { if (a === "back" || a === "home") { closePlay(); } return true; }   // lecture : rien d'autre
    if (panel) {
      var sheet = panel.kind !== "item";   // feuille v23 (Réglages, confirmation) : lignes verticales, ↑ ↓ aussi
      if (a === "left" || (sheet && a === "up")) { panel.btn = Math.max(0, panel.btn - 1); paintPanel(); }
      else if (a === "right" || (sheet && a === "down")) { panel.btn = Math.min(panel.actions.length - 1, panel.btn + 1); paintPanel(); }
      else if (a === "ok") { act(); }
      else if (a === "back") { panelBack(); }
      else if (a === "home") {
        if (panel.kind === "agents") { cancelAgents(); } else if (panel.kind === "musesetup") { cancelMuse(); }
        else { closePanel(); }
        goStart();
      }
      // ↑ ↓ sur une fiche : sans effet, mais consommés — jamais de saut vers Réglages depuis un film ouvert
      return true;
    }
    if (focus.zone === "top") {
      if (a === "ok") { openSettings(); return true; }
      if ((a === "down" || a === "back") && ROWS.length) { focus.zone = "rows"; focus.auto = false; }
      else if (a === "home") { goStart(); return true; }
      paint();
      return true;
    }
    if (a === "up") {   // rangée N > 0 : rangée N-1 (sa colonne mémorisée) ; rangée 0 seulement : Réglages
      if (focus.row > 0) { focus.row -= 1; } else { focus.zone = "top"; focus.auto = false; }
    }
    else if (a === "down") { focus.row = Math.min(ROWS.length - 1, focus.row + 1); }
    else if (a === "right") { focus.cols[focus.row] = Math.min(ROWS[focus.row].items.length - 1, col() + 1); }
    else if (a === "left") { focus.cols[focus.row] = Math.max(0, col() - 1); }
    else if (a === "ok") { var it = current(); if (it) { openItem(it, ROWS[focus.row]); } return true; }
    else if (a === "home") { goStart(); return true; }   // jamais quitter /tv : retour au début
    else if (a === "back") {   // Retour remonte d'UNE rangée (colonne gardée) ; en rangée 0 : première carte
      if (focus.row > 0) { focus.row -= 1; } else { focus.cols[0] = 0; }
    }
    paint();
    return true;
  }
  document.addEventListener("keydown", function (e) {
    var a = KEYS.normalize(e);
    // navigation du navigateur (Retour, Accueil…) : toujours empêchée, configuration comprise — le kiosque reste sur /tv
    if (KEYS.isBrowserNav(e) || a === "back" || a === "home") { e.preventDefault(); }
    if (!a) { return; }
    lastHumanAt = Date.now();   // télécommande d'Alex : prioritaire sur toute touche d'assistant
    // voile plein écran « Reconnexion… » : l'écran dessous n'est plus vu, aucune touche n'y agit (ni titre, ni
    // « Regarder », ni avis, ni feuille) ; la reprise automatique (poll, rechargement) n'en dépend pas
    if (offlineShown()) { e.preventDefault(); return; }
    if (cvSheet) {   // feuille Canvas : touches de consentement seulement (aucun geste ordinaire, aucune rafale)
      if (!(e.repeat && (a === "ok" || a === "back" || a === "home"))) { sheetKey(a); }
      e.preventDefault();
      return;
    }
    if (!play && guarded(a)) { humanEvent("key"); e.preventDefault(); return; }   // Retour/Accueil tenus après le lecteur
    if (e.repeat && (a === "ok" || a === "back" || a === "home")) { e.preventDefault(); return; }   // pas de rafale
    if (humanKey("key", a, !!e.repeat)) { e.preventDefault(); }
  });
  // boutons « précédent/suivant » d'une télécommande souris (air mouse) : jamais d'historique ; précédent = Retour
  document.addEventListener("mouseup", function (e) {
    if (e.button === 3 || e.button === 4) {
      e.preventDefault();
      if (offlineShown()) { return; }   // voile hors-ligne : rien d'invisible
      if (e.button === 3 && cvSheet) { sheetKey("back"); }
      else if (e.button === 3 && (play || !guarded("back"))) { humanKey("pointer", "back", false); }
    }
  });

  // ---- état global
  // fresh = false (état lu avant l'adoption d'une réponse de route, ou dépassé par un état plus récent) : tout l'état
  // est rendu SAUF sa vue Canvas, qui reste celle déjà adoptée (jamais d'ancienne ambiance ni de feuille rouverte)
  function render(s, gen, fresh) {
    var wasSetup = !!(state && state.setup.active);
    if (!fresh && state) { s.canvas = state.canvas; }
    state = s;
    if (wasSetup && !s.setup.active) {   // succès confirmé sur la TV, sans terminal ni jargon
      // succès partiel (Wi-Fi/nom appliqués, assistants non enregistrés) : jamais un « tout est prêt » à tort
      var agentsNote = s.setup.agents_saved === false && s.setup.agents_message;
      toast("C'est prêt ! OpenTV est configuré." + (agentsNote ? "\n" + agentsNote :
            (s.remote && s.remote.enabled && !s.remote.phones ?
             " Télécommande sur téléphone : mets-le sur le même Wi-Fi que la TV, puis scanne le QR à droite." : "")), true);
    }
    $("device").textContent = s.device;
    $("net").textContent = NET[s.network.mode] || s.network.mode;
    // préfixe « net- » : la classe « offline » est celle du voile plein écran « Reconnexion… » (jamais sur le badge)
    $("net").className = "badge net-" + s.network.mode;
    var conn = connectedAgents(s);
    $("agents").textContent = conn.length ? "Connectés : " + conn.map(function (a) { return AGENTS[a] || a; }).join(", ") : "";
    $("agents").hidden = !conn.length;
    // message d'assistant : accueil seulement, emplacement réservé dans le héros (jamais sur le titre ni un QR)
    var b = $("banner"), showMsg = !!s.message && !s.setup.active;
    if (showMsg) { b.textContent = "Message de " + agentName(s.message.from) + " : " + s.message.text; }
    b.hidden = !showMsg;
    $("hero").className = "hero" + (showMsg ? " msg" : "");
    $("setup").hidden = !s.setup.active;
    $("home").hidden = s.setup.active;
    $("help").hidden = s.setup.active;
    $("gear").hidden = s.setup.active;
    $("glabel").hidden = s.setup.active;
    var am = s.assistants && s.assistants.manage;
    if (am && am.last && typeof am.last.seq === "number") {
      if (agentsSeq === null) { agentsSeq = am.last.seq; }
      // seulement un résultat plus récent : un état lu avant l'ouverture (seq plus ancien) n'est jamais annoncé
      else if (am.last.seq > agentsSeq) { agentsSeq = am.last.seq; if (!s.setup.active) { onAgentsResult(am.last.kind); } }
    }
    // fin du mode : annoncée par le changement de résultat ci-dessus ; un état sans QR (poll parti avant
    // l'ouverture) est toléré quelques secondes, au-delà la feuille se ferme d'elle-même
    if (panel && panel.kind === "agents") {
      if ((am && am.active) || Date.now() - panel.openedAt < 6000) { renderAgents(); } else { closePanel(); }
    }
    var mu = s.muse;
    if (mu && mu.last && typeof mu.last.seq === "number") {   // issue de la connexion de Muse : une annonce, jamais rejouée
      if (museSeq === null) { museSeq = mu.last.seq; }
      else if (mu.last.seq > museSeq) { museSeq = mu.last.seq; if (!s.setup.active) { onMuseResult(mu); } }
    }
    if (mu && mu.disconnect_last && typeof mu.disconnect_last.seq === "number") {
      if (museDiscSeq === null) { museDiscSeq = mu.disconnect_last.seq; }
      else if (mu.disconnect_last.seq > museDiscSeq) {
        museDiscSeq = mu.disconnect_last.seq;
        toast(mu.disconnect_last.state === "done" ? "Muse est déconnectée." :
              "Muse n'a pas pu être déconnectée. Redémarre la boîte, puis réessaie.", true);
      }
    }
    if (mu && !mu.active) { museHidden = false; }
    if (mu && mu.active && !museHidden && !s.setup.active && !play && (!panel || panel.kind !== "musesetup") &&
        (!panel || panel.kind !== "item")) {
      openMuseSetup();   // page rechargée pendant la connexion : la feuille revient (séance déjà confirmée sur la TV)
    } else if (panel && panel.kind === "musesetup") {
      if (mu && mu.active) { renderMuse(); } else if (Date.now() - panel.openedAt > 6000) { closePanel(); }
    }
    if (panel && panel.kind === "muse" && panel.sig !== undefined && panel.sig !== museSig() && !(mu && mu.active)) {
      openMuse(true);   // propriété / pont établis entre-temps : choix à jour, focus de nouveau sur l'option sûre
    }
    if (panel && panel.kind === "museresult" && panel.paired !== undefined && panel.sig !== museSig() &&
        !(mu && mu.active)) {
      openMusePaired(panel.paired);   // propriété publiée après l'issue : « Déconnecter Muse » à jour, focus sûr
    }
    if (panel && panel.kind === "assistants") {   // sous-titres à jour (Muse connectée, compteurs)
      var subs = document.querySelectorAll("#pbtns .ts");
      if (subs.length === 3) { subs[0].textContent = museSub(); subs[1].textContent = agentsCounts(); }
    }
    if (s.setup.active) { closePanel(); renderSetup(s.setup); }
    else {
      if (!fy && !heroId) { renderRows(); }   // avant « Pour toi » : écran d'attente écrit, Réglages au focus
      loadForYou(false);
    }
    renderRemote();
    if (!fresh) { renderCanvas(); }   // vue Canvas périmée : celle déjà adoptée reste peinte, aucune feuille décidée
    else if (s.canvas) { cvAdopt(s.canvas); cvState(s.canvas, gen); }
    else { cvPub = null; cvState(null); renderCanvas(); }
    cvPaint();
    runActions(s);
    resumePlay(s.playback);
  }
  function poll() {
    var gen = humanGen;   // feuille Canvas : seulement d'un état demandé APRÈS le dernier geste humain
    readState().then(function (r) { fails = 0; $("offline").hidden = true; render(r.s, gen, r.fresh); })
      .catch(function (e) {
        if (onApiError(e)) { return; }
        fails += 1;
        if (fails >= 2 && !reloadTimer) { showOffline(MSG_OFFLINE); }
      })
      .then(function () { setTimeout(poll, 2000); });
  }
  (function () {   // témoin lisible (tests, diagnostic) : rechargements automatiques dans la fenêtre courante
    var st = reloadStore();
    document.documentElement.setAttribute("data-reloads", st ? String(recentReloads(st).length) : "na");
  })();
  document.addEventListener("visibilitychange", function () {   // page cachée : occupation déclarée, feuille retirée
    if (document.hidden && cvSheet && !cvSheet.busy) { sheetChoose("no", "timeout"); }
    cvPaint();
  });
  clock(); setInterval(clock, 10000);
  poll();
  remoteLoop();
})();
