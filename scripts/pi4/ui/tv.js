/* OpenTV Pi — écran TV (Chromium Raspberry Pi OS, loopback). Aucun script en ligne, aucune URL externe sauf affiches https. */
(function () {
  "use strict";
  var KEY = document.querySelector('meta[name="opentv-key"]').content;
  var $ = function (id) { return document.getElementById(id); };
  var state = null, fy = null, fyAt = 0, fails = 0, lastQr = "", lastApQr = "", lastRemoteQr = "";
  var REMOTE_ACTIONS = { up: 1, down: 1, left: 1, right: 1, ok: 1, back: 1, home: 1 };
  var remoteSeq = -1, remoteFails = 0;
  var focus = { zone: "rows", row: 0, cols: [], auto: false }, panel = null, toastTimer = null;
  var LABELS = { like: "J'aime", dislike: "Pas pour moi", seen: "Déjà vu", watchlist: "À voir plus tard",
                 "not-tonight": "Pas ce soir" };
  var TOGGLE = { like: ["opinion", "like", "clear-opinion"], dislike: ["opinion", "dislike", "clear-opinion"],
                 seen: ["seen", true, "unseen"], watchlist: ["watchlist", true, "unwatchlist"],
                 "not-tonight": ["not_tonight", true, "clear-not-tonight"] };
  var AGENTS = { muse: "Muse", hermit: "Hermit", hermes: "Hermes" };
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
  function clock() {
    var d = new Date();
    $("clock").textContent = d.toLocaleTimeString("fr-CA", { hour: "2-digit", minute: "2-digit" });
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
    $("apfig").hidden = !s.hotspot;
    if (s.hotspot) {
      $("apssid").textContent = s.hotspot.ssid;
      if (s.hotspot.wifi_qr !== lastApQr) { lastApQr = s.hotspot.wifi_qr; drawQR($("apqr"), lastApQr); }
    }
    $("qrfig").hidden = !s.url;
    $("url").textContent = "";   // jamais d'adresse technique à l'écran : le QR suffit
    if (s.url && s.url !== lastQr) { lastQr = s.url; drawQR($("qr"), s.url); }
  }

  // ---- Pour toi : héros (titre au focus) + rangées. Réglages vivent dans la barre du haut (zone « top ») :
  // un seul ↑ y mène depuis n'importe quelle rangée ou fiche, OK ouvre, Retour ferme — jamais une rangée cachée.
  // Rangées : ↓ descend, Retour remonte d'une seule rangée (colonne mémorisée par rangée), Accueil = tout début.
  var ROWS = [], grid = [], heroTimer = null, heroId = null;
  function loadForYou(force) {
    if (!force && fy && Date.now() - fyAt < 120000) { return; }
    fyAt = Date.now();
    api("GET", "/api/tv/foryou").then(function (d) { fy = d; renderRows(); }).catch(function (e) { fyAt = 0; onApiError(e); });
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
    lines.push(["⚙", "Réglages toujours en haut de l'écran : ↑ puis OK."]);
    lines.forEach(function (x) {
      var li = el("li");
      li.appendChild(el("span", "ico", x[0])); li.appendChild(el("span", null, x[1]));
      ul.appendChild(li);
    });
    d.appendChild(ul);
    box.appendChild(d);
  }
  function renderRows() {
    var box = $("rowsin");
    ROWS = rowsData(); grid = [];
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
    $("gear").className = "gear" + (top ? (panel ? " active" : " focused") : "");
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
      var last = g.cards[g.cards.length - 1], lw = g.strip.parentNode.clientWidth - 64, x = 0;
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
    rows: [["← →", "Parcourir"], ["↓", "Rangée suivante"], ["Retour", "Rangée précédente"], ["↑", "Réglages"],
           ["OK", "Ouvrir"]],
    top: [["OK", "Ouvrir les Réglages"], ["↓ ou Retour", "Revenir aux titres"]],
    topEmpty: [["OK", "Ouvrir les Réglages"]],
    item: [["← →", "Choisir"], ["OK", "Valider"], ["Retour", "Fermer"], ["↑", "Réglages"]],
    panel: [["← →", "Choisir"], ["OK", "Valider"], ["Retour", "Revenir"]]
  }, helpKey = "";
  function help() {
    var k = panel ? (panel.kind === "item" ? "item" : "panel") : (focus.zone === "top" ? (ROWS.length ? "top" : "topEmpty") : "rows");
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
      $("hkicker").textContent = "Pour toi";
      $("htitle").textContent = "Ta soirée se prépare";
      $("htitle").className = "htitle";
      $("hmeta").textContent = "OpenTV choisit pour toi dans le catalogue disponible.";
      $("hdesc").textContent = (fy && fy.note) || "Tes suggestions apparaîtront ici dès que le catalogue sera prêt : rien à faire de ton côté.";
    } else {
      $("hkicker").textContent = r.title;
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
    clearTimeout(heroTimer);   // image différée : rien à décoder pendant la répétition d'une flèche
    heroTimer = setTimeout(function () { heroImage(it); }, 160);
  }
  function heroImage(it) {
    var hp = $("hposter"), bg = $("himg");
    hp.textContent = ""; hp.removeAttribute("data-img");
    hp.hidden = !it; $("hlogo").hidden = !!it;
    if (!it) { bg.hidden = true; bg.removeAttribute("src"); $("hart").style.background = ""; return; }
    hp.appendChild(artFor(it)); poster(hp, it);
    $("hart").style.background = "radial-gradient(1000px 560px at 78% 30%, hsla(" + hue(it.id || it.title) +
                                 ", 60%, 42%, .5), rgba(0, 0, 0, 0) 70%)";
    bg.hidden = true;   // montré seulement une fois chargé (jamais d'icône d'image cassée)
    if (isHttps(it.poster)) { bg.src = it.poster; } else { bg.removeAttribute("src"); }
  }
  // ---- télécommande sur téléphone (repli de la télécommande CEC) : QR d'appairage toujours visible sur l'accueil,
  // dans sa propre colonne (jamais sur les cartes), même après un premier appairage : un téléphone perdu se remplace
  // sans aucune touche. Rien à recopier : ni adresse, ni code.
  function renderRemote() {
    var r = state && state.remote, box = $("remotebox");
    var show = !!(r && r.enabled && r.url && !state.setup.active);
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
          d.events.forEach(function (ev) { if (ev && REMOTE_ACTIONS[ev.a] === 1) { handle(ev.a); } });
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
    $("panel").className = "panel" + (kind === "item" ? "" : " top");
    $("panel").hidden = false;
    paintPanel(); paint(); renderRemote();
  }
  function button(label, base) {
    var b = el("button", base || null, label);
    b.setAttribute("data-base", base || "");
    return b;
  }
  function openItem(it, row) {
    var m = ((fy && fy.marks) || {})[it.id] || {}, actions = Object.keys(LABELS);
    showPanel("item", row ? row.title : "", it.title, it.description || meta(it, false), actions.map(function (a) {
      var on = m[TOGGLE[a][0]] === TOGGLE[a][1];
      return button((on ? "✓ " : "") + LABELS[a], on ? "on" : "");
    }), 0, { it: it, actions: actions });
  }
  function openSettings(sel) {
    var list = [["setup", "⟳", "Reconfigurer", "Nouveau QR : Wi-Fi, nom, assistants"]];
    if (state && state.remote && state.remote.enabled && state.remote.phones > 0) {
      list.push(["remote-forget", "▣", "Oublier les téléphones", "Ils devront rescanner le QR"]);
    }
    list.push(["close", "✕", "Fermer", "Revenir à l'accueil"]);
    var idx = 0;
    showPanel("settings", (state && state.device) || "OpenTV", "Réglages",
              "← → choisir · OK ouvrir · Retour fermer. Rien ne change sans ta confirmation.",
              list.map(function (x, i) {
                var b = button("", "tile");
                b.setAttribute("data-label", x[2]);
                b.appendChild(el("span", "ti", x[1])); b.appendChild(el("span", "tl", x[2])); b.appendChild(el("span", "ts", x[3]));
                if (x[0] === sel) { idx = i; }
                return b;
              }), idx, { actions: list.map(function (x) { return x[0]; }) });
  }
  function openConfirm(what) {   // choix sûr (« Non ») au focus par défaut
    var forget = what === "remote-forget";
    showPanel("confirm", "Réglages", forget ? "Oublier tous les téléphones ?" : "Afficher un nouveau QR de configuration ?",
              forget ? "Ils ne pourront plus servir de télécommande avant d'avoir rescanné le QR."
                     : "Le Wi-Fi actuel reste actif tant que tu ne le changes pas.",
              [button(forget ? "Oui, oublier" : "Oui, reconfigurer", forget ? "danger" : ""), button("Non")], 1,
              { what: what, actions: [what, "no"] });
  }
  function paintPanel() {
    var b = $("pbtns").children;
    for (var i = 0; i < b.length; i++) {
      var base = b[i].getAttribute("data-base") || "";
      b[i].className = base + (i === panel.btn ? (base ? " " : "") + "focused" : "");
    }
  }
  function closePanel() { panel = null; $("panel").hidden = true; paint(); if (state) { renderRemote(); } }
  function panelBack() {
    if (panel.kind === "confirm") { openSettings(panel.what); } else { closePanel(); }
  }
  function goStart() {
    focus.row = 0; focus.cols = [];
    focus.zone = ROWS.length ? "rows" : "top";
    paint();
  }
  function act() {
    var a = panel.actions[panel.btn], it = panel.it;
    if (panel.kind === "settings") { if (a === "close") { closePanel(); } else { openConfirm(a); } return; }
    if (panel.kind === "confirm") {
      if (a === "no") { openSettings(panel.what); return; }
      closePanel();
      if (a === "setup") { api("POST", "/api/tv/setup", {}).then(function () { toast("Nouveau QR en préparation…"); }).catch(onApiError); }
      if (a === "remote-forget") { api("POST", "/api/tv/remote/revoke", {}).then(function () { toast("Téléphones oubliés."); }).catch(onApiError); }
      return;
    }
    var m = (fy.marks || {})[it.id] || {}, t = TOGGLE[a];
    var action = m[t[0]] === t[1] ? t[2] : a;
    api("POST", "/api/tv/feedback", { id: it.id, action: action }).then(function (r) {
      if (r.ok && r.state) { fy.marks = fy.marks || {}; fy.marks[it.id] = r.state; }
      toast(r.message || "Action non enregistrée.");
      closePanel(); renderRows();
    }).catch(function (e) { if (!onApiError(e)) { toast("Action non enregistrée."); } });
  }
  // ---- télécommande : actions normalisées par keys.js (clavier, télécommande USB 2,4 GHz, HDMI-CEC)
  var KEYS = window.OpenTVKeys || { normalize: function () { return null; }, isBrowserNav: function () { return false; } };
  function handle(a) {   // -> true si l'action a été consommée
    if (!state || state.setup.active) { return false; }   // configuration : pilotée par le téléphone seulement
    if (REMOTE_ACTIONS[a] !== 1) { return false; }
    if (panel) {
      if (a === "left") { panel.btn = Math.max(0, panel.btn - 1); paintPanel(); }
      else if (a === "right") { panel.btn = Math.min(panel.actions.length - 1, panel.btn + 1); paintPanel(); }
      else if (a === "ok") { act(); }
      else if (a === "back") { panelBack(); }
      else if (a === "home") { closePanel(); goStart(); }
      else if (a === "up" && panel.kind === "item") { closePanel(); focus.zone = "top"; focus.auto = false; paint(); }
      return true;                                         // ↑/↓ dans Réglages : sans effet, mais consommés
    }
    if (focus.zone === "top") {
      if (a === "ok") { openSettings(); return true; }
      if ((a === "down" || a === "back") && ROWS.length) { focus.zone = "rows"; focus.auto = false; }
      else if (a === "home") { goStart(); return true; }
      paint();
      return true;
    }
    if (a === "up") { focus.zone = "top"; focus.auto = false; }   // un seul ↑, depuis n'importe quelle rangée
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
    if (e.repeat && (a === "ok" || a === "back" || a === "home")) { e.preventDefault(); return; }   // pas de rafale
    if (handle(a)) { e.preventDefault(); }
  });
  // boutons « précédent/suivant » d'une télécommande souris (air mouse) : jamais d'historique ; précédent = Retour
  document.addEventListener("mouseup", function (e) {
    if (e.button === 3 || e.button === 4) { e.preventDefault(); if (e.button === 3) { handle("back"); } }
  });

  // ---- état global
  function render(s) {
    var wasSetup = !!(state && state.setup.active);
    state = s;
    if (wasSetup && !s.setup.active) {   // succès confirmé sur la TV, sans terminal ni jargon
      toast("C'est prêt ! OpenTV est configuré." + (s.remote && s.remote.enabled && !s.remote.phones ?
            " Télécommande sur téléphone : mets-le sur le même Wi-Fi que la TV, puis scanne le QR à droite." : ""), true);
    }
    $("device").textContent = s.device;
    $("net").textContent = NET[s.network.mode] || s.network.mode;
    $("net").className = "badge " + s.network.mode;
    $("agents").textContent = s.agents.length ? "Assistants : " + s.agents.map(function (a) { return AGENTS[a] || a; }).join(", ") : "";
    $("agents").hidden = !s.agents.length;
    // message d'assistant : accueil seulement, emplacement réservé dans le héros (jamais sur le titre ni un QR)
    var b = $("banner"), showMsg = !!s.message && !s.setup.active;
    if (showMsg) { b.textContent = "Message de " + (AGENTS[s.message.from] || s.message.from) + " : " + s.message.text; }
    b.hidden = !showMsg;
    $("hero").className = "hero" + (showMsg ? " msg" : "");
    $("setup").hidden = !s.setup.active;
    $("home").hidden = s.setup.active;
    $("help").hidden = s.setup.active;
    $("gear").hidden = s.setup.active;
    if (s.setup.active) { closePanel(); renderSetup(s.setup); }
    else {
      if (!fy && !heroId) { renderRows(); }   // avant « Pour toi » : écran d'attente écrit, Réglages au focus
      loadForYou(false);
    }
    renderRemote();
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
  $("himg").addEventListener("load", function () { $("himg").hidden = !$("himg").getAttribute("src"); });
  $("himg").addEventListener("error", function () { $("himg").hidden = true; });   // affiche absente : fond dessiné seul
  clock(); setInterval(clock, 10000);
  poll();
  remoteLoop();
})();
