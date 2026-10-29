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
  function showOffline(text) { $("offline").textContent = text; $("offline").hidden = false; }
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
  function fyBusy() { return !!(play || panel); }
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
    return out;
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
    var top = focus.zone === "top";
    $("gear").className = "qbtn" + (top ? (panel ? " active" : " focused") : "");
    // panneau ouvert : le message d'assistant s'efface (couche dédiée, jamais sur Réglages/confirmation/fiche)
    $("app").className = (top ? "zone-top" : "") + (panel ? " has-panel" : "");
    $("main").className = "main" + (panel ? " dim" : "");
    grid.forEach(function (g, ri) {
      var cur = ri === focus.row, c = focus.cols[ri] || 0, fc = g.cards[c] || g.cards[0];
      g.row.className = "row" + (g.few ? " few" : "") + (cur ? " current" : "");
      g.cards.forEach(function (card, ci) {
        card.className = "card" + (!top && cur && ci === c ? " focused" : "");
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
    panel: [["↑ ↓", "Choisir"], ["OK", "Valider"], ["Retour", "Revenir"]]
  }, helpKey = "";
  function help() {
    var k = panel ? (panel.kind === "item" ? "item" : panel.kind === "agents" ? "agents" : "panel") : (focus.zone === "top" ? (ROWS.length ? "top" : "topEmpty") :
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
      if (why) { chips.appendChild(el("span", "chip", why.text)); }
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
    var show = !!(r && r.enabled && r.url && !state.setup.active) && !(panel && panel.kind === "agents");
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
          d.events.forEach(function (ev) { if (ev && REMOTE_ACTIONS[ev.a] === 1) { lastHumanAt = Date.now(); handle(ev.a); } });
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
    $("pqrfig").hidden = kind !== "agents";
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
    flushForYou();   // sans fiche rouverte (page rechargée en cours de lecture) : sélection en attente appliquée
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
    if (!flushForYou()) { paint(); }
    if (state) { renderRemote(); }
  }
  function panelBack() {
    if (panel.kind === "agents") { cancelAgents(); }
    else if (panel.kind === "confirm") { openSettings(panel.what); } else { closePanel(); }
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
    $("aqrmsg").hidden = true;
    var ol = $("psteps");
    ol.textContent = "";
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
    if (a === "watch") { startPlay(it); return; }
    if (panel.kind === "settings") { if (a === "close") { closePanel(); } else { openConfirm(a); } return; }
    if (panel.kind === "agents") { cancelAgents(); return; }
    if (panel.kind === "confirm") {
      if (a === "no") { openSettings(panel.what); return; }
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
          api("GET", "/api/tv/state").then(render).catch(onApiError);   // QR tout de suite, sans attendre le poll
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
    if (actBusy || !s || s.setup.active) { return; }
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
      return api("POST", "/api/tv/action", { seq: seq, phase: "done", ok: !why, reason: why || null });
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
      if (panel.kind === "agents") { return "tv_busy"; }
      if (a !== "back" && a !== "home") { return "settings_forbidden"; }
    } else if (!panel && focus.zone === "top" && a === "ok") { return "settings_forbidden"; }
    else if (panel && a === "ok" && panel.actions[panel.btn] === "watch") { return "use_play_title"; }
    handle(a);
    return null;
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
      else if (a === "home") { if (panel.kind === "agents") { cancelAgents(); } else { closePanel(); } goStart(); }
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
    if (!play && guarded(a)) { e.preventDefault(); return; }   // Retour/Accueil tenus depuis la fermeture du lecteur
    if (e.repeat && (a === "ok" || a === "back" || a === "home")) { e.preventDefault(); return; }   // pas de rafale
    if (handle(a)) { e.preventDefault(); }
  });
  // boutons « précédent/suivant » d'une télécommande souris (air mouse) : jamais d'historique ; précédent = Retour
  document.addEventListener("mouseup", function (e) {
    if (e.button === 3 || e.button === 4) {
      e.preventDefault();
      if (e.button === 3 && (play || !guarded("back"))) { handle("back"); }
    }
  });

  // ---- état global
  function render(s) {
    var wasSetup = !!(state && state.setup.active);
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
    if (s.setup.active) { closePanel(); renderSetup(s.setup); }
    else {
      if (!fy && !heroId) { renderRows(); }   // avant « Pour toi » : écran d'attente écrit, Réglages au focus
      loadForYou(false);
    }
    renderRemote();
    runActions(s);
    resumePlay(s.playback);
  }
  function poll() {
    api("GET", "/api/tv/state").then(function (s) { fails = 0; $("offline").hidden = true; render(s); })
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
  clock(); setInterval(clock, 10000);
  poll();
  remoteLoop();
})();
