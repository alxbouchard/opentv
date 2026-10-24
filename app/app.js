/* OpenTV — lanceur TV. Flèches + OK + Retour, les mêmes touches qu'une télécommande.
   La page n'a pas l'API tizen : elle parle au service Node local pour lister et lancer les applis. */
(function () {
  "use strict";
  var SERVICE = "http://127.0.0.1:8096";
  var $ = function (id) { return document.getElementById(id); };
  var stage = $("stage");

  var BRANDS = window.NOVA_BRANDS || {};
  var ACTIONS = window.NOVA_ACTIONS || [];
  var PRESETS = window.NOVA_PRESETS || [];
  var DESIGNS = window.NOVA_DESIGNS || [];
  var LOOK = window.NOVA_LOOK || [];

  var DEFAULT_PREFS = {
    bg: { type: "preset", id: "misty-coast" },
    tiles: "glass", labels: "both", logo: "white", font: "modern", clock: "show",
    apps: null   // null => calculé au premier démarrage (applis reconnues installées)
  };

  var ICONS = {
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8M4.6 9a1.7 1.7 0 0 0-.3-1.8"/></svg>',
    look: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3a9 9 0 0 0 0 18c1.1 0 1.8-.8 1.8-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.8-.5-1.2 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3z"/><circle cx="7.5" cy="11" r="1.2"/><circle cx="10" cy="7" r="1.2"/><circle cx="14.5" cy="7" r="1.2"/><circle cx="17" cy="11" r="1.2"/></svg>',
    input: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 21h8"/></svg>',
    web: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/></svg>',
    app: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>',
    tv: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/></svg>',
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18h2"/></svg>'
  };
  var IMG_TINT = { white: "brightness(0) invert(1)", black: "brightness(0)", none: "none" };

  // ---- état ----
  var P = loadPrefs();
  var INSTALLED = {};        // id -> name (depuis le service)
  var haveService = false;
  var stack = ["home"];
  var focusEl = null;
  var lookOpen = false;
  var lastKeyByScreen = {};
  var idleAt = Date.now();
  var actions = {};          // data-key -> fn

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function bind(key, fn) { actions[key] = fn; }

  // ---- préférences (localStorage) ----
  function loadPrefs() {
    var p = {}; for (var k in DEFAULT_PREFS) p[k] = DEFAULT_PREFS[k];
    try { var s = window.localStorage.getItem("opentv.prefs"); if (s) { var o = JSON.parse(s); for (var j in o) p[j] = o[j]; } } catch (e) {}
    return p;
  }
  function savePrefs(changes) {
    for (var k in changes) P[k] = changes[k];
    try { window.localStorage.setItem("opentv.prefs", JSON.stringify(P)); } catch (e) {}
    applyPrefs(); renderHome(); if (lookOpen) renderLook();
  }
  function applyPrefs() {
    var keep = stage.className.split(/\s+/).filter(function (c) { return c && !/^(font|tiles|labels|logo|clock)-/.test(c); });
    keep.push("font-" + P.font, "tiles-" + P.tiles, "labels-" + P.labels, "logo-" + P.logo, "clock-" + P.clock);
    stage.className = keep.join(" ");
    setBackground();
  }
  function setBackground() {
    var img = $("bgImg"), bg = P.bg || {}, src;
    src = "backgrounds/" + (bg.type === "preset" ? bg.id : "misty-coast") + ".jpg";
    if (img.getAttribute("data-src") !== src) {
      img.setAttribute("data-src", src);
      img.onload = function () { img.className = "on"; };
      img.src = src;
    } else img.className = "on";
  }

  // ---- horloge ----
  function two(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var d = new Date();
    var t = two(d.getHours()) + ":" + two(d.getMinutes());
    $("time").textContent = t; $("saverClock").textContent = t;
    var days = ["dimanche","lundi","mardi","mercredi","jeudi","vendredi","samedi"];
    var mon = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"];
    $("date").textContent = days[d.getDay()] + " " + d.getDate() + " " + mon[d.getMonth()];
    if (current() === "home" && !lookOpen && !isSaving() && Date.now() - idleAt > 180000) showSaver();
  }

  // ---- catalogue d'applis ----
  // Reconnaît une appli par son NOM (ou par un id exact listé dans brand.ids).
  function brandFor(id) {
    var name = (INSTALLED[id] || id || "").toLowerCase();
    for (var i = 0; i < BRANDS.length; i++) {
      var b = BRANDS[i];
      if (b.ids && indexOf(b.ids, id) >= 0) return b;
      if (b.match) for (var m = 0; m < b.match.length; m++) if (name.indexOf(b.match[m]) >= 0) return b;
    }
    return null;
  }
  function appInfo(id) {
    var b = brandFor(id);
    if (b) return b;
    return { name: INSTALLED[id] || id, icon: "app", bg: ["#2A2F3A", "#141820"], glow: "#5B8DEF", generic: true };
  }
  function brandOrder(id) { var b = brandFor(id); return b ? indexOf(BRANDS, b) : 999; }
  function installedIds() { var a = []; for (var id in INSTALLED) a.push(id); return a; }

  // Masque les composants système (non reconnus) : vrais tiers = id à hash aléatoire,
  // les services Samsung commencent par org.tizen./com.samsung./com.webos. ou portent un nom de service.
  var SYS_WORDS = ["app", "service", "daemon", "bridge", "running", "automation", "hdmicec", "hbbtv", "sso",
    "btaudio", "aging", "dynamichint", "channel-list", "picture-test", "easysetup", "ui-application",
    "add_channels", "acr", "heremaps", "smsource", "photo player", "avcontrol", "registerav", "ignition",
    "mycontents", "ttx", "music tv"];
  function isSystemApp(id) {
    if (brandFor(id)) return false;                 // reconnue = jamais système
    if (/^(org\.tizen\.|com\.samsung\.|com\.webos\.)/.test(id)) return true;
    var n = (INSTALLED[id] || "").toLowerCase();
    if (!n) return true;
    for (var i = 0; i < SYS_WORDS.length; i++) if (n.indexOf(SYS_WORDS[i]) >= 0) return true;
    return false;
  }

  // Parmi plusieurs applis qui collent à la même marque, en choisir UNE (nom exact, sinon le plus court).
  function pickForBrand(brand) {
    var cands = installedIds().filter(function (id) { return brandFor(id) === brand; });
    if (!cands.length) return null;
    cands.sort(function (a, b) {
      var na = (INSTALLED[a] || "").toLowerCase(), nb = (INSTALLED[b] || "").toLowerCase();
      var ea = na === brand.name.toLowerCase() ? 0 : 1, eb = nb === brand.name.toLowerCase() ? 0 : 1;
      if (ea !== eb) return ea - eb;
      return na.length - nb.length;
    });
    return cands[0];
  }

  // Une appli par marque reconnue (dédoublonnée), dans l'ordre du catalogue.
  function recognizedApps() {
    var out = [];
    for (var i = 0; i < BRANDS.length; i++) { var id = pickForBrand(BRANDS[i]); if (id) out.push(id); }
    return out;
  }
  function defaultApps() { return recognizedApps(); }

  function dedupeByBrand(ids) {
    var out = [], seen = {};
    for (var i = 0; i < ids.length; i++) {
      var b = brandFor(ids[i]), key = b ? b.key : ids[i];
      if (seen[key]) continue; seen[key] = 1; out.push(ids[i]);
    }
    return out;
  }
  function shownApps() {
    var list = (P.apps && P.apps.length) ? P.apps.slice() : defaultApps();
    if (haveService) list = list.filter(function (id) { return INSTALLED[id]; });
    return dedupeByBrand(list);
  }
  function allSelectableApps() {
    // marques reconnues (une par marque) + vraies applis tierces non reconnues ; services système masqués
    var others = installedIds().filter(function (id) { return !brandFor(id) && !isSystemApp(id); });
    others.sort(function (a, b) { return (INSTALLED[a] || "").localeCompare(INSTALLED[b] || ""); });
    return recognizedApps().concat(others);
  }

  // ---- rendu d'une tuile ----
  function logoHtml(app) {
    if (app.logo) {
      var src = "logos/" + app.logo + ".svg";
      if (app.mono) return '<span class="mask ' + (app.wide ? "wd" : "sq") + '" style="-webkit-mask-image:url(' + src +
        ');mask-image:url(' + src + ');--brand:' + (app.fg || "#fff") + '"></span>';
      return '<img class="logo' + (app.wide ? "" : " sq") + '" src="' + src + '" style="-webkit-filter:' +
        IMG_TINT[app.img || "none"] + ';filter:' + IMG_TINT[app.img || "none"] + '">';
    }
    return '<span class="ic" style="display:inline-block;width:56px;height:56px;color:#fff">' + (ICONS[app.icon] || ICONS.app) + '</span>';
  }
  function tileHtml(id, key, extra, off) {
    var app = appInfo(id);
    var vars = "--c1:" + app.bg[0] + ";--c2:" + app.bg[1] + ";--glow:" + (app.glow || "#fff") + ";--tfg:" + (app.fg || "#fff");
    return '<div class="tile focusable glow' + (off ? " off" : "") + '" data-key="' + key + '" data-app="' + esc(id) +
      '" style="' + vars + '">' + (extra || "") +
      '<div class="logoWrap">' + logoHtml(app) + '</div><div class="name">' + esc(app.name) + '</div></div>';
  }

  // ---- lancement ----
  function launch(id) {
    if (!haveService) { toast("Service indisponible (aperçu)"); return; }
    xhr("GET", SERVICE + "/launch?id=" + encodeURIComponent(id), function (ok, data) {
      if (!ok || (data && data.error)) toast("Impossible d'ouvrir « " + appInfo(id).name + " »");
    });
  }
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.className = "";
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.className = "hidden"; }, 2600);
  }

  // ---- écrans ----
  function current() { return stack[stack.length - 1]; }
  function isSaving() { return !$("saver").classList.contains("hidden"); }
  function show(name) {
    var ss = document.querySelectorAll(".screen");
    for (var i = 0; i < ss.length; i++) ss[i].classList.toggle("active", ss[i].id === name);
    stage.classList.toggle("inside", name !== "home");
  }
  function open(name) {
    closeLook(true);
    if (current() !== name) stack.push(name);
    show(name); renderScreen(name); restoreFocus(name);
  }
  function back() {
    if (lookOpen) return closeLook();
    if (stack.length > 1) { stack.pop(); show(current()); renderScreen(current()); restoreFocus(current()); }
  }
  function goHome() { stack = ["home"]; closeLook(true); show("home"); renderScreen("home"); restoreFocus("home"); }
  function renderScreen(name) {
    if (name === "home") renderHome();
    else if (name === "apps") renderApps();
    else if (name === "designs") renderDesigns();
    else if (name === "phone") renderPhone();
  }

  // ---- accueil ----
  function renderHome() {
    var h = new Date().getHours();
    $("heroEyebrow").textContent = h < 12 ? "Bonjour" : h < 18 ? "Bon après-midi" : "Bonsoir";
    // raccourcis
    var q = '<button class="qbtn focusable" data-key="q-phone" data-tip="Télécommande">' + ICONS.phone + '</button>';
    q += '<button class="qbtn focusable" data-key="q-look" data-tip="Apparence">' + ICONS.look + '</button>';
    for (var a = 0; a < ACTIONS.length; a++) {
      (function (act) { bind("q-act-" + act.app, function () { launch(act.app); }); })(ACTIONS[a]);
      q += '<button class="qbtn focusable" data-key="q-act-' + ACTIONS[a].app + '" data-tip="' + esc(ACTIONS[a].label) + '">' +
        (ICONS[ACTIONS[a].icon] || ICONS.app) + '</button>';
    }
    $("quick").innerHTML = q;
    bind("q-look", openLook);
    bind("q-phone", function () { open("phone"); });
    // tuiles
    var ids = shownApps();
    $("tiles").innerHTML = '<div class="grid">' + ids.map(function (id) {
      bind("app-" + id, function () { launch(id); });
      return tileHtml(id, "app-" + id);
    }).join("") + "</div>";
    keepFocus();
  }

  // ---- apparence ----
  function openLook() { lookOpen = true; $("look").classList.remove("hidden"); renderLook();
    setFocus($("look").querySelector(".focusable")); }
  function closeLook(silent) { if (!lookOpen) return; lookOpen = false; $("look").classList.add("hidden");
    if (!silent) restoreFocus(current()); }
  function currentDesign() {
    for (var i = 0; i < DESIGNS.length; i++) {
      var d = DESIGNS[i], ok = true;
      for (var k in d.p) if (JSON.stringify(d.p[k]) !== JSON.stringify(P[k])) { ok = false; break; }
      if (ok) return d;
    }
    return null;
  }
  function bgName(bg) {
    if (!bg) return "";
    for (var i = 0; i < PRESETS.length; i++) if (PRESETS[i][0] === bg.id) return PRESETS[i][1];
    return bg.id;
  }
  function renderLook() {
    var d = currentDesign();
    var html = '<button class="lrow focusable" data-key="look-design" data-look="design"><span class="lname">Design</span>' +
      '<span class="lval"><span class="arrow">&#8249;</span>' + (d ? d.name : "Perso") + '<span class="arrow">&#8250;</span></span></button>';
    var th = "backgrounds/" + (P.bg && P.bg.id ? P.bg.id : "misty-coast") + ".jpg";
    html += '<button class="lrow focusable" data-key="look-bg" data-look="bg"><span class="lname">Fond</span>' +
      '<span class="lval"><span class="arrow">&#8249;</span><span class="thumb" style="background-image:url(\'' + th + '\')"></span>' +
      esc(bgName(P.bg)) + '<span class="arrow">&#8250;</span></span></button>';
    for (var i = 0; i < LOOK.length; i++) {
      var row = LOOK[i], val = null;
      for (var v = 0; v < row[2].length; v++) if (row[2][v][0] === P[row[0]]) val = row[2][v];
      val = val || row[2][0];
      html += '<button class="lrow focusable" data-key="look-' + row[0] + '" data-look="' + row[0] + '"><span class="lname">' +
        row[1] + '</span><span class="lval"><span class="arrow">&#8249;</span>' + val[1] + '<span class="arrow">&#8250;</span></span></button>';
    }
    html += '<button class="lrow action focusable" data-key="look-designs"><span class="ic">' + ICONS.app + '</span>Voir tous les designs</button>';
    html += '<button class="lrow action focusable" data-key="look-apps"><span class="ic">' + ICONS.app + '</span>Choisir les applis</button>';
    $("lookRows").innerHTML = html;
    bind("look-designs", function () { open("designs"); });
    bind("look-apps", function () { open("apps"); });
  }
  function cycleLook(key, step) {
    if (key === "design") {
      var cur = currentDesign(), at = cur ? indexOf(DESIGNS, cur) : -1;
      var next = DESIGNS[(at + step + DESIGNS.length) % DESIGNS.length];
      return savePrefs(JSON.parse(JSON.stringify(next.p)));
    }
    if (key === "bg") {
      var i = 0; for (var n = 0; n < PRESETS.length; n++) if (PRESETS[n][0] === (P.bg && P.bg.id)) i = n;
      var np = PRESETS[(i + step + PRESETS.length) % PRESETS.length];
      return savePrefs({ bg: { type: "preset", id: np[0] } });
    }
    var row = null; for (var r = 0; r < LOOK.length; r++) if (LOOK[r][0] === key) row = LOOK[r];
    var vals = row[2].map(function (o) { return o[0]; });
    var j = vals.indexOf(P[key]), ch = {};
    ch[key] = vals[(j + step + vals.length) % vals.length];
    savePrefs(ch);
  }
  function indexOf(arr, x) { for (var i = 0; i < arr.length; i++) if (arr[i] === x) return i; return -1; }

  // ---- designs ----
  function renderDesigns() {
    var cur = currentDesign();
    $("designGrid").innerHTML = DESIGNS.map(function (d) {
      bind("design-" + d.id, function () { savePrefs(JSON.parse(JSON.stringify(d.p))); goHome(); });
      return '<div class="dcard focusable" data-key="design-' + d.id + '"><div class="dthumb" style="background-image:url(\'designs/' +
        d.id + '.jpg\')">' + (cur && cur.id === d.id ? '<div class="badge">Actif</div>' : '') + '</div><div class="dname">' +
        d.name + '</div><div class="ddesc">' + d.desc + '</div></div>';
    }).join("");
  }

  // ---- choisir les applis ----
  function renderApps() {
    var ids = allSelectableApps();
    var chosen = shownApps();
    $("appGrid").innerHTML = ids.map(function (id) {
      var on = indexOf(chosen, id) >= 0;
      bind("pick-" + id, function () {
        var list = (P.apps && P.apps.length ? P.apps.slice() : defaultApps());
        var at = indexOf(list, id);
        if (at >= 0) { if (list.length > 1) list.splice(at, 1); }
        else list.push(id);
        savePrefs({ apps: list });
        renderApps();
        var el = $("appGrid").querySelector('[data-key="pick-' + cssEsc(id) + '"]');
        if (el) setFocus(el);
      });
      var extra = (on ? '<span class="check">&#10003;</span>' : "") + '<span class="appid">' + esc(id) + '</span>';
      return tileHtml(id, "pick-" + id, extra, !on);
    }).join("");
    keepFocus();
  }
  function cssEsc(s) { return String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1"); }

  // ---- télécommande téléphone ----
  var REMOTE_URL = "";
  function drawQR(text) {
    var c = $("qrCanvas");
    if (!c || typeof qrcode !== "function") return;
    var q = qrcode(0, "M");
    q.addData(text); q.make();
    var n = q.getModuleCount(), size = Number(c.getAttribute("data-size") || 340), cell = Math.max(2, Math.floor(size / n));
    c.width = c.height = cell * n;
    var g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = "#111";
    for (var r = 0; r < n; r++) for (var col = 0; col < n; col++) if (q.isDark(r, col)) g.fillRect(col * cell, r * cell, cell, cell);
  }
  function renderPhone() {
    $("remoteUrl").textContent = "…";
    xhr("GET", SERVICE + "/info", function (ok, data) {
      if (ok && data && data.ip && data.ip !== "127.0.0.1") {
        REMOTE_URL = "http://" + data.ip + ":" + data.port + "/remote";
        $("remoteUrl").textContent = REMOTE_URL.replace("http://", "");
        drawQR(REMOTE_URL);
      } else {
        $("remoteUrl").textContent = "Télécommande indisponible (TV non connectée au Wi-Fi ?)";
      }
    });
  }
  var lastPollN = null;
  function pollRemote() {
    xhr("GET", SERVICE + "/poll", function (ok, data) {
      if (!ok || !data) return;
      if (lastPollN === null) { lastPollN = data.n; return; }
      if (data.n !== lastPollN) { lastPollN = data.n; if (data.cmd) command(data.cmd); }
    });
  }

  // ---- moteur de focus ----
  function layer() { return lookOpen ? $("look") : $(current()); }
  function focusables() {
    var root = layer();
    var all = Array.prototype.slice.call(root.querySelectorAll(".focusable"));
    if (!lookOpen && current() !== "home") all = all.concat(Array.prototype.slice.call($("nav").querySelectorAll(".focusable")));
    return all.filter(function (el) { return el.offsetParent !== null; });
  }
  function setFocus(el) {
    if (!el) return;
    if (focusEl) focusEl.classList.remove("focused");
    focusEl = el; el.classList.add("focused");
    var tip = el.getAttribute("data-tip");
    $("tip").textContent = tip || ""; $("tip").className = tip ? "on" : "";
    var k = el.getAttribute("data-key");
    if (lookOpen) lastKeyByScreen["look"] = k;
    else lastKeyByScreen[current()] = k;
    // faire défiler si besoin
    if (el.scrollIntoView) try { el.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (e) {}
  }
  function restoreFocus(screen) {
    var key = lastKeyByScreen[lookOpen ? "look" : screen], list = focusables(), el = null;
    if (key) el = layer().querySelector('[data-key="' + cssEsc(key) + '"]');
    if (!el || indexOf(list, el) < 0) {
      // par défaut sur l'accueil : la première tuile d'appli, pas un raccourci
      if (!lookOpen && screen === "home") el = $("tiles").querySelector(".tile");
      if (!el || indexOf(list, el) < 0) el = list[0];
    }
    setFocus(el);
  }
  function keepFocus() {
    if (!focusEl || !document.body.contains(focusEl) || focusEl.offsetParent === null) restoreFocus(current());
  }
  function center(r) { return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
  function move(dir) {
    var list = focusables();
    if (!list.length) return;
    if (!focusEl || indexOf(list, focusEl) < 0) return setFocus(list[0]);
    // dans le panneau Apparence, gauche/droite change la valeur
    if (lookOpen && focusEl.getAttribute("data-look") && (dir === "left" || dir === "right"))
      return cycleLook(focusEl.getAttribute("data-look"), dir === "right" ? 1 : -1);
    var a = focusEl.getBoundingClientRect(), ac = center(a), best = null, score = Infinity;
    for (var i = 0; i < list.length; i++) {
      var el = list[i]; if (el === focusEl) continue;
      var b = el.getBoundingClientRect(), bc = center(b), main, cross;
      if (dir === "down")  { if (b.top < a.bottom - 4) continue; main = b.top - a.bottom; cross = Math.abs(bc.x - ac.x); }
      if (dir === "up")    { if (b.bottom > a.top + 4) continue; main = a.top - b.bottom; cross = Math.abs(bc.x - ac.x); }
      if (dir === "right") { if (b.left < a.right - 4) continue; main = b.left - a.right; cross = Math.abs(bc.y - ac.y); }
      if (dir === "left")  { if (b.right > a.left + 4) continue; main = a.left - b.right; cross = Math.abs(bc.y - ac.y); }
      var s = main + cross * 2.2;
      if (s < score) { score = s; best = el; }
    }
    if (best) setFocus(best);
  }

  // ---- sélection (OK) ----
  function select() {
    if (!focusEl) return;
    if (lookOpen && focusEl.getAttribute("data-look")) return cycleLook(focusEl.getAttribute("data-look"), 1);
    var key = focusEl.getAttribute("data-key");
    if (key && actions[key]) return actions[key]();
  }
  bind("nav-back", function () { back(); });
  bind("nav-home", function () { goHome(); });

  // ---- économiseur ----
  function showSaver() { $("saver").classList.remove("hidden"); }
  function hideSaver() { if (isSaving()) { $("saver").classList.add("hidden"); return true; } return false; }

  // ---- entrées clavier / télécommande ----
  function command(cmd) {
    idleAt = Date.now();
    if (hideSaver()) return;
    if (cmd === "up" || cmd === "down" || cmd === "left" || cmd === "right") return move(cmd);
    if (cmd === "ok") return select();
    if (cmd === "back") return back();
    if (cmd === "home") return goHome();
  }
  document.addEventListener("keydown", function (e) {
    var map = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", Enter: "ok", Escape: "back" };
    var k = e.key, code = e.keyCode;
    if (map[k]) { e.preventDefault(); return command(map[k]); }
    // codes Samsung : 10009 = Return/Back, 10073/65385 variantes, 38/40/37/39 flèches, 13 OK
    if (code === 10009 || code === 65385 || code === 8) { e.preventDefault(); return command("back"); }
    if (code === 38) return command("up");
    if (code === 40) return command("down");
    if (code === 37) return command("left");
    if (code === 39) return command("right");
    if (code === 13) return command("ok");
    if (code === 10071 || code === 10228) return command("back"); // Exit -> revient à l'accueil
  });

  // ---- XHR simple ----
  function xhr(method, url, done) {
    try {
      var r = new XMLHttpRequest();
      r.open(method, url, true); r.timeout = 6000;
      r.onreadystatechange = function () {
        if (r.readyState === 4) {
          var data = null; try { data = JSON.parse(r.responseText); } catch (e) {}
          done(r.status >= 200 && r.status < 300, data);
        }
      };
      r.ontimeout = function () { done(false, null); };
      r.onerror = function () { done(false, null); };
      r.send();
    } catch (e) { done(false, null); }
  }

  // ---- démarrage ----
  function boot() {
    applyPrefs();
    tick(); setInterval(tick, 1000);
    xhr("GET", SERVICE + "/apps", function (ok, data) {
      if (ok && data && data.length) {
        haveService = true;
        for (var i = 0; i < data.length; i++) INSTALLED[data[i].id] = data[i].name;
      } else {
        // aperçu hors TV : fabriquer des applis fictives à partir du catalogue
        for (var b = 0; b < BRANDS.length; b++) INSTALLED["demo." + BRANDS[b].key] = BRANDS[b].name;
      }
      var el = $("build");
      if (el) el.textContent = "OpenTV v9 · " + (haveService ? (installedIds().length + " applis") : "aperçu");
      goHome();
      if (haveService) { pollRemote(); setInterval(pollRemote, 400); }   // écoute la télécommande téléphone
    });
    // filet de sécurité : si le service tarde, afficher quand même l'accueil
    setTimeout(function () { if (!focusEl) goHome(); }, 3500);
  }
  boot();
})();
