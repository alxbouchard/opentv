/* OpenTV Pi — écran TV (Chromium Raspberry Pi OS, loopback). Aucun script en ligne, aucune URL externe sauf affiches https. */
(function () {
  "use strict";
  var KEY = document.querySelector('meta[name="opentv-key"]').content;
  var $ = function (id) { return document.getElementById(id); };
  var state = null, fy = null, fyAt = 0, fails = 0, lastQr = "", lastApQr = "";
  var focus = { row: 0, col: 0 }, panel = null, toastTimer = null;
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
    var n = q.getModuleCount(), size = Number(canvas.getAttribute("data-size") || 400);
    var cell = Math.max(2, Math.floor(size / (n + 8))), pad = cell * 4;
    canvas.width = canvas.height = cell * n + pad * 2;
    var g = canvas.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = "#111";
    for (var r = 0; r < n; r++) { for (var c = 0; c < n; c++) { if (q.isDark(r, c)) { g.fillRect(pad + c * cell, pad + r * cell, cell, cell); } } }
    canvas.setAttribute("data-qr", "ok");
  }
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4000);
  }
  function clock() {
    var d = new Date();
    $("clock").textContent = d.toLocaleTimeString("fr-CA", { hour: "2-digit", minute: "2-digit" });
  }

  // ---- configuration (QR)
  function renderSetup(s) {
    var steps = $("steps");
    steps.textContent = "";
    var list = s.hotspot ? ["Sur ton téléphone, rejoins le Wi-Fi « " + s.hotspot.ssid + " » (QR de gauche).",
                            "Scanne le QR de droite.", "Choisis ton Wi-Fi, le nom de la TV et les agents."]
                         : ["Scanne le QR avec l'appareil photo de ton téléphone.",
                            "Remplis le formulaire : Wi-Fi, nom de la TV, agents.", "La TV affiche ensuite OpenTV."];
    list.forEach(function (x) { steps.appendChild(el("li", null, x)); });
    $("setupmsg").textContent = s.message || (s.expires_in ? "Ce code est valable " + Math.max(1, Math.round(s.expires_in / 60)) + " min, une seule fois." : "");
    $("apfig").hidden = !s.hotspot;
    if (s.hotspot) {
      $("apssid").textContent = s.hotspot.ssid;
      if (s.hotspot.wifi_qr !== lastApQr) { lastApQr = s.hotspot.wifi_qr; drawQR($("apqr"), lastApQr); }
    }
    $("qrfig").hidden = !s.url;
    $("url").textContent = s.url ? s.url.split("?")[0] : "";
    if (s.url && s.url !== lastQr) { lastQr = s.url; drawQR($("qr"), s.url); }
  }

  // ---- Pour toi
  function loadForYou(force) {
    if (!force && fy && Date.now() - fyAt < 120000) { return; }
    fyAt = Date.now();
    api("GET", "/api/tv/foryou").then(function (d) { fy = d; renderRows(); }).catch(function (e) { fyAt = 0; onApiError(e); });
  }
  function rowsWithSettings() {
    var rows = (fy && fy.rows) ? fy.rows.slice() : [];
    rows.push({ id: "settings", title: "Réglages", items: [{ id: "settings/setup", title: "Reconfigurer (nouveau QR)", settings: true }] });
    return rows;
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
  function renderRows() {
    var box = $("rows"), rows = rowsWithSettings();
    $("note").textContent = (fy && fy.note) || "";
    box.textContent = "";
    focus.row = Math.min(focus.row, rows.length - 1);
    rows.forEach(function (r, ri) {
      var row = el("div", "row"), strip = el("div", "strip");
      row.appendChild(el("h2", null, r.title));
      focus.col = ri === focus.row ? Math.min(focus.col, r.items.length - 1) : focus.col;
      r.items.forEach(function (it, ci) {
        var card = el("div", "card" + (it.settings ? " settings" : "") + (ri === focus.row && ci === focus.col ? " focused" : ""));
        if (it.poster && Math.abs(ri - focus.row) <= 1) {
          var img = el("img"); img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
          img.addEventListener("error", function () { img.replaceWith(el("div", "ph", (it.title || "?").charAt(0))); });
          img.src = it.poster;
          card.appendChild(img);
        } else { card.appendChild(el("div", "ph", it.settings ? "⚙" : (it.title || "?").charAt(0))); }
        var info = el("div", "info");
        info.appendChild(el("div", "t", it.title));
        if (it.year) { info.appendChild(el("div", "y", it.year + (it.genres && it.genres.length ? " · " + it.genres.join(", ") : ""))); }
        if (it.reasons && it.reasons[0]) { info.appendChild(el("div", "r", it.reasons[0].text)); }
        badges((fy.marks || {})[it.id]).forEach(function (b) { info.appendChild(el("span", "mark", b)); });
        card.appendChild(info);
        strip.appendChild(card);
      });
      row.appendChild(strip);
      box.appendChild(row);
    });
    var f = box.querySelector(".card.focused");
    if (f) { f.scrollIntoView({ block: "nearest", inline: "nearest" }); }
  }
  function current() {
    var rows = rowsWithSettings(), r = rows[focus.row];
    return r ? r.items[focus.col] : null;
  }
  function openPanel(it) {
    var btns = $("pbtns");
    btns.textContent = "";
    if (it.settings) {
      $("ptitle").textContent = "Afficher un nouveau QR de configuration ?";
      $("pmeta").textContent = "Le Wi-Fi actuel reste actif tant que tu ne le changes pas.";
      panel = { it: it, btn: 1, actions: ["setup", "close"] };
      ["Oui, reconfigurer", "Non"].forEach(function (t) { btns.appendChild(el("button", null, t)); });
    } else {
      $("ptitle").textContent = it.title;
      $("pmeta").textContent = (it.description || "") ;
      panel = { it: it, btn: 0, actions: Object.keys(LABELS) };
      var m = (fy.marks || {})[it.id] || {};
      panel.actions.forEach(function (a) {
        var on = m[TOGGLE[a][0]] === TOGGLE[a][1];
        btns.appendChild(el("button", on ? "on" : null, (on ? "✓ " : "") + LABELS[a]));
      });
    }
    $("panel").hidden = false;
    paintPanel();
  }
  function paintPanel() {
    var b = $("pbtns").children;
    for (var i = 0; i < b.length; i++) { b[i].className = (b[i].className.indexOf("on") >= 0 ? "on " : "") + (i === panel.btn ? "focused" : ""); }
  }
  function closePanel() { panel = null; $("panel").hidden = true; }
  function act() {
    var a = panel.actions[panel.btn], it = panel.it;
    if (a === "close") { closePanel(); return; }
    if (a === "setup") { closePanel(); api("POST", "/api/tv/setup", {}).then(function () { toast("Nouveau QR en préparation…"); }).catch(onApiError); return; }
    var m = (fy.marks || {})[it.id] || {}, t = TOGGLE[a];
    var action = m[t[0]] === t[1] ? t[2] : a;
    api("POST", "/api/tv/feedback", { id: it.id, action: action }).then(function (r) {
      if (r.ok && r.state) { fy.marks = fy.marks || {}; fy.marks[it.id] = r.state; }
      toast(r.message || "Action non enregistrée.");
      closePanel(); renderRows();
    }).catch(function (e) { if (!onApiError(e)) { toast("Action non enregistrée."); } });
  }
  document.addEventListener("keydown", function (e) {
    if (!state || state.setup.active) { return; }
    var k = e.key;
    if (panel) {
      if (k === "ArrowLeft") { panel.btn = Math.max(0, panel.btn - 1); paintPanel(); }
      else if (k === "ArrowRight") { panel.btn = Math.min(panel.actions.length - 1, panel.btn + 1); paintPanel(); }
      else if (k === "Enter") { act(); }
      else if (k === "Escape" || k === "Backspace" || k === "BrowserBack") { closePanel(); }
      e.preventDefault(); return;
    }
    var rows = rowsWithSettings();
    if (k === "ArrowRight") { focus.col = Math.min(rows[focus.row].items.length - 1, focus.col + 1); }
    else if (k === "ArrowLeft") { focus.col = Math.max(0, focus.col - 1); }
    else if (k === "ArrowDown") { focus.row = Math.min(rows.length - 1, focus.row + 1); focus.col = 0; }
    else if (k === "ArrowUp") { focus.row = Math.max(0, focus.row - 1); focus.col = 0; }
    else if (k === "Enter") { var it = current(); if (it) { openPanel(it); } }
    else { return; }
    e.preventDefault(); renderRows();
  });

  // ---- état global
  function render(s) {
    state = s;
    $("device").textContent = s.device;
    $("net").textContent = NET[s.network.mode] || s.network.mode;
    $("net").className = "badge " + s.network.mode;
    $("agents").textContent = s.agents.length ? "Agents : " + s.agents.map(function (a) { return AGENTS[a] || a; }).join(", ") : "Aucun agent";
    var b = $("banner");
    if (s.message) { b.textContent = "Message de " + (AGENTS[s.message.from] || s.message.from) + " : " + s.message.text; b.hidden = false; }
    else { b.hidden = true; }
    $("setup").hidden = !s.setup.active;
    $("home").hidden = s.setup.active;
    $("help").hidden = s.setup.active;
    if (s.setup.active) { closePanel(); renderSetup(s.setup); } else { loadForYou(false); }
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
})();
