/* OpenTV Canvas — laboratoire (prototype phase 1, jamais installé). ES5 strict (Chromium 56 / Acorn ecmaVersion 5).
   Coque fixe : seules des classes issues d'énumérations fermées sont appliquées ; aucune valeur de scène n'est jamais
   injectée comme HTML, CSS, URL ou nom de fichier. Le texte affiché vient d'un catalogue de démonstration local et
   passe uniquement par textContent. Aperçu temporaire → confirmation littérale (true) → publication ; historique de
   cinq scènes ; retour arrière ou défaut immuable. Les animations sont figées pendant panneau, lecture,
   configuration, hors-ligne, page cachée ou performance dégradée.
   Temps : échéances d'aperçu et fenêtre de silence sur horloge MONOTONE (performance.now) ; l'heure murale ne sert
   qu'à l'affichage (horloge, salutation), un saut d'heure système ne prolonge ni ne tue donc rien.
   Annuler-sur-humain/occupé : toute touche de navigation et toute transition fiche, Réglages, lecture, chargement,
   configuration ou hors-ligne annulent l'aperçu vivant AVANT la transition ; la scène publiée revient avec le focus
   ré-ancré par rangée et titre ; si le titre au focus a changé, la touche annule seulement. Les rangées ne sont
   jamais reconstruites sous un panneau, une lecture, un chargement, la configuration ou le hors-ligne.
   API : OTVCanvas.agent (aperçu, confirmation littérale, annulation) ; OTVCanvas.owner (PROPRIÉTAIRE SEULEMENT :
   choix de variante, modification directe, Annuler, défaut) ; OTVCanvas.lab (états simulés, instantané). */
(function () {
  "use strict";

  // miroir exact de canvas_model.py (vérifié par tests/test_canvas_prototype.py)
  var ENUMS = {"variant": ["mist", "cinema", "canvas"], "wallpaper": ["brume", "affiche", "aurore"], "motion": ["full", "reduced", "off"], "accent": ["ice", "ember", "jade", "violet"], "shade": ["soft", "balanced", "deep"], "hero_focus": ["title", "artwork", "calm"]};
  var ROWS = ["tonight", "new", "top", "watchlist"];
  var PRESETS = {
    "mist": {"schema_version": 1, "variant": "mist", "wallpaper": "brume", "motion": "full", "accent": "ice", "shade": "balanced", "hero_focus": "title", "row_order": ["tonight", "new", "top", "watchlist"]},
    "cinema": {"schema_version": 1, "variant": "cinema", "wallpaper": "affiche", "motion": "full", "accent": "ember", "shade": "deep", "hero_focus": "artwork", "row_order": ["tonight", "top", "new", "watchlist"]},
    "canvas": {"schema_version": 1, "variant": "canvas", "wallpaper": "aurore", "motion": "full", "accent": "ice", "shade": "balanced", "hero_focus": "title", "row_order": ["tonight", "new", "top", "watchlist"]}
  };
  var KEYS = ["schema_version", "variant", "wallpaper", "motion", "accent", "shade", "hero_focus", "row_order"];
  var ENUM_KEYS = ["variant", "wallpaper", "motion", "accent", "shade", "hero_focus"];
  var REQUIRED_ROW = "tonight";
  var DEFAULT_NAME = "canvas";
  var MAX_HISTORY = 5;
  var PREVIEW_MIN_MS = 1000, PREVIEW_MAX_MS = 60000, PREVIEW_DEFAULT_MS = 10000;
  var NAV_QUIET_MS = 1500;          // un changement d'agent attend que la télécommande se taise
  var BUSY_FLAGS = ["panel", "playback", "loading", "setup", "offline"];      // aperçu/confirmation refusés, rangées gelées
  var CANCEL_FLAGS = ["playback", "loading", "setup", "offline"];            // leur début annule l'aperçu vivant
  var PAUSE_FLAGS = ["panel", "playback", "loading", "setup", "offline", "hidden", "degraded"];

  var ROW_LABELS = { tonight: "Le choix de ce soir", "new": "Nouveautés disponibles", top: "Les mieux notés",
                     watchlist: "À voir plus tard" };
  // catalogue de démonstration (aucune adresse, aucune image distante) : palette = classe fermée pal-0..pal-7
  var TITLES = [
    { id: "t1", t: "Les Étoiles", y: 2026, g: "Science-fiction, Aventure", s: "8,2", v: "22 000", p: 0, isNew: true,
      reason: "Parce que tu aimes la science-fiction",
      d: "Une astronome et son fils traversent un ciel qui s'éteint pour rallumer la dernière balise du système." },
    { id: "t2", t: "Le Grand Voyage", y: 2025, g: "Aventure, Drame", s: "7,9", v: "12 000", p: 2, isNew: true,
      reason: "Tu as aimé d'autres grandes aventures",
      d: "Trois sœurs rejoignent la côte à pied pour honorer une promesse faite vingt ans plus tôt." },
    { id: "t3", t: "Marée Haute", y: 2024, g: "Thriller", s: "7,4", v: "8 300", p: 6, isNew: false,
      reason: "Ambiance proche de « Nuit Noire »",
      d: "Une nuit de tempête, un phare isolé et une disparition que personne ne veut expliquer." },
    { id: "t4", t: "Nuit Noire", y: 2019, g: "Thriller", s: "7,1", v: "3 400", p: 1, isNew: false,
      reason: "Ajouté à ta liste « À voir plus tard »",
      d: "Un chauffeur de taxi accepte une dernière course qui va bouleverser sa nuit." },
    { id: "t5", t: "Rire Encore", y: 2021, g: "Comédie", s: "6,4", v: "900", p: 3, isNew: false,
      reason: "Une comédie courte pour finir la soirée",
      d: "Une troupe d'improvisation en faillite tente un dernier spectacle impossible." },
    { id: "t6", t: "Mission Horizon", y: 2018, g: "Action", s: "7,7", v: "450 000", p: 4, isNew: false,
      reason: "Très bien noté par les spectateurs",
      d: "Une équipe d'élite doit empêcher la chute d'une station orbitale." },
    { id: "t7", t: "Forêt d'Hiver", y: 2023, g: "Drame", s: "7,6", v: "5 100", p: 5, isNew: true,
      reason: "Nouveau, dans tes genres préférés",
      d: "Un garde forestier retrouve, sous la neige, la trace d'un ami disparu." },
    { id: "t8", t: "Ligne Rouge", y: 2022, g: "Policier", s: "7,0", v: "15 800", p: 7, isNew: false,
      reason: "Suite logique de ta dernière soirée polar",
      d: "Deux inspecteurs rivaux remontent la piste d'un braquage parfait." },
    { id: "t9", t: "Le Jardin des Possibles, une très longue odyssée familiale", y: 2026, g: "Animation, Famille",
      s: "8,0", v: "2 700", p: 0, isNew: true, reason: "À partager en famille",
      d: "Une petite fille découvre que chaque graine de son jardin ouvre un monde différent." },
    { id: "t10", t: "Sable", y: 2020, g: "Science-fiction", s: "7,8", v: "61 000", p: 2, isNew: false,
      reason: "Très bien noté par les spectateurs",
      d: "Sur une planète désertique, une cartographe découvre une ville enfouie." }
  ];
  var ROW_ITEMS = { tonight: ["t1", "t2", "t3"], "new": ["t9", "t7", "t1", "t2", "t10", "t3", "t8"],
                    top: ["t6", "t1", "t10", "t2", "t7", "t3", "t4", "t8"], watchlist: ["t4", "t5", "t8", "t3", "t6"] };

  // ------------------------------------------------------------------ validation fermée
  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function isPlain(o) { return o !== null && typeof o === "object" && Object.prototype.toString.call(o) === "[object Object]"; }
  function inList(v, list) {
    for (var i = 0; i < list.length; i++) { if (list[i] === v) { return true; } }
    return false;
  }
  function fail(code, key) { return { ok: false, error: code, key: key === undefined ? null : key }; }

  function validateScene(d) {
    var k, i, out = {};
    if (!isPlain(d)) { return fail("invalid_shape"); }
    for (k in d) { if (own(d, k) && !inList(k, KEYS)) { return fail("unknown_key", String(k)); } }
    for (i = 0; i < KEYS.length; i++) { if (!own(d, KEYS[i])) { return fail("missing_key", KEYS[i]); } }
    if (d.schema_version !== 1) { return fail("invalid_value", "schema_version"); }
    out.schema_version = 1;
    for (i = 0; i < ENUM_KEYS.length; i++) {
      k = ENUM_KEYS[i];
      if (typeof d[k] !== "string" || !inList(d[k], ENUMS[k])) { return fail("invalid_value", k); }
      out[k] = d[k];
    }
    var rows = d.row_order, seen = {};
    if (Object.prototype.toString.call(rows) !== "[object Array]" || rows.length < 1 || rows.length > ROWS.length) {
      return fail("invalid_value", "row_order");
    }
    out.row_order = [];
    for (i = 0; i < rows.length; i++) {
      if (typeof rows[i] !== "string" || !inList(rows[i], ROWS) || own(seen, rows[i])) {
        return fail("invalid_value", "row_order");
      }
      seen[rows[i]] = true;
      out.row_order.push(rows[i]);
    }
    if (!own(seen, REQUIRED_ROW)) { return fail("invalid_value", "row_order"); }
    return { ok: true, scene: out };
  }

  function clone(sc) {
    var o = {}, i;
    for (i = 0; i < KEYS.length; i++) { o[KEYS[i]] = KEYS[i] === "row_order" ? sc.row_order.slice() : sc[KEYS[i]]; }
    return o;
  }

  function applyPatch(base, patch) {
    var k, d, n = 0;
    if (!isPlain(patch)) { return fail("invalid_patch"); }
    for (k in patch) {
      if (!own(patch, k)) { continue; }
      if (k === "schema_version" || !inList(k, KEYS)) { return fail("unknown_key", String(k)); }
      n++;
    }
    if (!n) { return fail("invalid_patch"); }
    d = clone(base);
    for (k in patch) { if (own(patch, k)) { d[k] = patch[k]; } }
    return validateScene(d);
  }

  function canonical(sc) {
    var keys = KEYS.slice().sort(), parts = [], i;
    for (i = 0; i < keys.length; i++) { parts.push(JSON.stringify(keys[i]) + ":" + JSON.stringify(sc[keys[i]])); }
    return "{" + parts.join(",") + "}";
  }

  // ------------------------------------------------------------------ état
  var $ = function (id) { return document.getElementById(id); };
  var DEFAULT = clone(PRESETS[DEFAULT_NAME]);
  var S = {
    history: [], revision: 0, preview: null, bootError: null, lastKey: null, rendered: null,
    anchorRow: null, endedBy: null,
    flags: { panel: false, playback: false, loading: false, setup: false, offline: false, hidden: false,
             degraded: false },
    reducedMQ: false
  };
  var F = { zone: "rows", r: 0, cols: {}, panelKind: null, btn: 0 };
  var mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  // horloge monotone : performance.now() (Chromium 56) ; repli : Date.now() rendu monotone, sauts bornés à 1 s
  var perf = window.performance && typeof window.performance.now === "function" ? window.performance : null;
  var monoWall = null, monoAcc = 0;
  function mono() {
    if (perf) { return perf.now(); }
    var w = Date.now(), d = monoWall === null ? 0 : w - monoWall;
    monoWall = w;
    monoAcc += d > 0 ? Math.min(d, 1000) : 0;     // un recul est ignoré, une avance (NTP, réglage) plafonnée
    return monoAcc;
  }
  function wallNow() { return Date.now(); }       // affichage seulement (horloge, salutation), jamais une échéance
  function published() { return S.history.length ? S.history[S.history.length - 1] : DEFAULT; }
  function previewLive() { return S.preview && mono() < S.preview.until ? S.preview : null; }
  function visible() { var p = previewLive(); return p ? p.scene : published(); }
  function layoutFrozen() {
    for (var i = 0; i < BUSY_FLAGS.length; i++) { if (S.flags[BUSY_FLAGS[i]]) { return BUSY_FLAGS[i]; } }
    return null;
  }
  function busyReason() {
    var f = layoutFrozen();
    if (f) { return f; }
    if (S.lastKey !== null && mono() - S.lastKey < NAV_QUIET_MS) { return "navigating"; }
    return null;
  }
  function pauseReasons() {
    var out = [], i;
    for (i = 0; i < PAUSE_FLAGS.length; i++) { if (S.flags[PAUSE_FLAGS[i]]) { out.push(PAUSE_FLAGS[i]); } }
    return out;
  }
  function effectiveMotion(sc) {
    if (sc.motion === "off") { return "off"; }
    if (mq) { S.reducedMQ = !!mq.matches; }          // préférence lue en direct (l'événement peut arriver plus tard)
    if (sc.motion === "reduced" || S.reducedMQ) { return "reduced"; }
    return "full";
  }

  function publish(sc) {
    S.history.push(clone(sc));
    if (S.history.length > MAX_HISTORY) { S.history.shift(); }
    S.revision++;
  }

  // ------------------------------------------------------------------ rendu
  function itemById(id) {
    for (var i = 0; i < TITLES.length; i++) { if (TITLES[i].id === id) { return TITLES[i]; } }
    return null;
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function clear(e) { while (e.firstChild) { e.removeChild(e.firstChild); } }

  var rowsData = [];
  function buildRows(order, prefs) {
    var keepId = currentItem() ? currentItem().id : null, keepRow = rowsData[F.r] ? rowsData[F.r].id : null;
    var box = $("rowsin"), i, j, k, chosen = null;
    clear(box);
    rowsData = [];
    for (i = 0; i < order.length; i++) {
      var id = order[i], ids = ROW_ITEMS[id], row = el("div", "row" + (ids.length <= 3 ? " few" : ""));
      var h = el("h2"), strip = el("div", "strip"), lane = el("div", "lane");
      h.appendChild(el("span", "", ROW_LABELS[id]));
      h.appendChild(el("span", "cnt", ids.length + " titres"));
      row.appendChild(h);
      for (j = 0; j < ids.length; j++) {
        var it = itemById(ids[j]), card = el("div", "card"), art = el("div", "cart pal-" + it.p), info = el("div", "info");
        art.appendChild(el("span", "letter", it.t.charAt(0)));
        card.appendChild(art);
        info.appendChild(el("div", "t", it.t));
        info.appendChild(el("div", "y", it.y + " · " + it.g));
        info.appendChild(el("div", "r", "IMDb " + it.s + "/10 · " + it.v + " votes"));
        card.appendChild(info);
        card.setAttribute("data-id", it.id);
        strip.appendChild(card);
      }
      lane.appendChild(strip);
      row.appendChild(lane);
      box.appendChild(row);
      rowsData.push({ id: id, el: row, strip: strip, ids: ids, few: ids.length <= 3 });
    }
    // mémoire spatiale : rangée par identifiant (préférences dans l'ordre, sinon la première), colonne par rangée
    F.r = 0;
    prefs = prefs || [keepRow];
    for (k = 0; k < prefs.length && chosen === null; k++) {
      for (i = 0; i < rowsData.length; i++) { if (prefs[k] !== null && rowsData[i].id === prefs[k]) { F.r = i; chosen = i; } }
    }
    if (keepId && rowsData[F.r].id === keepRow) {
      var c = rowsData[F.r].ids.indexOf(keepId);
      if (c >= 0) { F.cols[rowsData[F.r].id] = c; }
    }
    drawFocus(true);
  }

  function col(r) {
    var rd = rowsData[r], c = F.cols[rd.id] || 0;
    return Math.max(0, Math.min(c, rd.ids.length - 1));
  }
  function currentItem() {
    if (!rowsData.length || !rowsData[F.r]) { return null; }
    return itemById(rowsData[F.r].ids[col(F.r)]);
  }

  var artTimer = null, artOn = "artA", artPal = -1;
  function setArt(p) {
    var nextId = artOn === "artA" ? "artB" : "artA", next = $(nextId), cur = $(artOn);
    if (p === artPal) { return; }
    artPal = p;
    next.className = "art pal-" + p + " on";          // fondu enchaîné (opacité seulement)
    cur.className = cur.className.replace(" on", "");
    artOn = nextId;
  }

  function greeting() {
    var h = new Date(wallNow()).getHours();
    return h >= 18 || h < 4 ? "Bonsoir" : (h >= 12 ? "Bon après-midi" : "Bonjour");
  }

  function drawHero(deferArt) {
    var it = currentItem(), chips = $("hchips");
    if (!it) { return; }
    $("hkicker").textContent = greeting() + " · " + ROW_LABELS[rowsData[F.r].id];
    $("htitle").textContent = it.t;
    $("hmeta").textContent = it.y + " · " + it.g;
    clear(chips);
    chips.appendChild(el("span", "chip score", "IMDb " + it.s));
    if (it.isNew) { chips.appendChild(el("span", "chip", "Sorti en " + it.y)); }
    $("hreason").textContent = it.reason;
    $("hdesc").textContent = it.d;
    if (artTimer) { clearTimeout(artTimer); artTimer = null; }
    if (deferArt) {
      artTimer = setTimeout(function () { artTimer = null; setArt(it.p); }, 220);   // répétition de touche
    } else {
      setArt(it.p);
    }
  }

  function drawFocus(immediate, deferArt) {
    var i, j, rd, cards, tv = $("tv");
    for (i = 0; i < rowsData.length; i++) {
      rd = rowsData[i];
      rd.el.className = "row" + (rd.few ? " few" : "") + (i === F.r ? " current" : "");
      cards = rd.strip.children;
      var c = col(i);
      for (j = 0; j < cards.length; j++) {
        cards[j].className = "card" + (F.zone === "rows" && i === F.r && j === c ? " focused" : "");
      }
      // géométrie mesurée (largeur de contenu de la rangée, positions de mise en page des cartes, sans l'échelle)
      var laneW = laneWidth(rd), last = cards[cards.length - 1];
      var total = last ? last.offsetLeft + last.offsetWidth : 0, off = c <= 1 || !cards[c - 1] ? 0 : cards[c - 1].offsetLeft;
      off = Math.max(0, Math.min(off, Math.max(0, total - laneW)));
      rd.strip.style.transform = "translate3d(" + (-off) + "px,0,0)";
    }
    var top = rowsData[F.r] ? rowsData[F.r].el.offsetTop : 0;
    $("rowsin").style.transform = "translate3d(0," + (-top) + "px,0)";
    $("gear").className = "qbtn" + (F.zone === "top" && !S.flags.panel ? " focused" : "");
    tv.className = tv.className.replace(/ ?zone-top/g, "") + (F.zone === "top" ? " zone-top" : "");
    drawHint();
    drawHero(deferArt);
  }

  function laneWidth(rd) {
    var lane = rd.strip.parentNode, cs = window.getComputedStyle(lane);
    var w = lane.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    return w > 0 ? w : 0;
  }

  function drawHint() {
    var hint = $("hint"), parts;
    clear(hint);
    if (S.flags.panel) {
      parts = F.panelKind === "sheet" ? [["↑ ↓", "Choisir"], ["OK", "Valider"], ["Retour", "Fermer"]]
                                      : [["← →", "Choisir"], ["OK", "Valider"], ["Retour", "Fermer"]];
    } else if (F.zone === "top") {
      parts = [["OK", "Ouvrir les Réglages"], ["↓", "Revenir aux titres"]];
    } else {
      parts = [["← →", "Parcourir"], ["↓", "Rangée suivante"]];
      parts.push(F.r === 0 ? ["↑", "Réglages"] : ["↑", "Rangée précédente"]);
      parts.push(["OK", "Ouvrir"]);
    }
    for (var i = 0; i < parts.length; i++) {
      if (i) { hint.appendChild(document.createTextNode(" · ")); }
      hint.appendChild(el("span", "hk", parts[i][0]));
      hint.appendChild(document.createTextNode(" " + parts[i][1]));
    }
  }

  function render() {
    var sc = visible(), tv = $("tv"), motion = effectiveMotion(sc), reasons = pauseReasons();
    var cls = ["tv", "v-" + sc.variant, "wp-" + sc.wallpaper, "acc-" + sc.accent, "shade-" + sc.shade,
               "hero-" + sc.hero_focus, "motion-" + sc.motion];
    if (motion === "reduced" && sc.motion === "full") { cls.push("motion-reduced"); }
    if (reasons.length) { cls.push("paused"); }
    if (S.flags.panel) { cls.push("has-panel"); }
    if (F.zone === "top") { cls.push("zone-top"); }
    tv.className = cls.join(" ");
    tv.setAttribute("data-variant", sc.variant);
    var rows = sc.row_order.join(","), cur = rowsData[F.r] ? rowsData[F.r].id : null, prefs;
    // jamais de reconstruction sous un panneau, une lecture, un chargement, la configuration ou le hors-ligne :
    // l'ordre en attente s'applique à la fin de l'état
    if (S.rendered !== rows && !layoutFrozen()) {
      prefs = S.preview ? [cur] : (S.endedBy === "human" ? [cur, S.anchorRow] : [S.anchorRow, cur]);
      S.rendered = rows;
      buildRows(sc.row_order, prefs);
    }
    if (!S.preview && S.rendered === rows) { S.anchorRow = null; S.endedBy = null; }
    $("scenepill").textContent = sc.hero_focus === "calm" ? "Soirée calme" : (sc.variant === "canvas" ? "Ce soir pour toi" : "Scène");
    drawPreviewChip();
    drawLabState();
    monitor();
  }

  // ------------------------------------------------------------------ aperçu
  var pvTimer = null;
  function drawPreviewChip() {
    var p = previewLive(), pv = $("pv");
    pv.hidden = !p;
    if (p) { $("pvtext").textContent = "Aperçu de scène · revient dans " + Math.ceil((p.until - mono()) / 1000) + " s"; }
  }
  // fin d'aperçu sans rendu ; `by` = "human" (touche) ou autre (échéance, état occupé, agent, propriétaire)
  function dropPreview(by) {
    var had = !!S.preview;
    if (pvTimer) { clearTimeout(pvTimer); pvTimer = null; }
    if (had) { S.preview = null; S.endedBy = by; }
    return had;
  }
  // annuler-sur-humain/occupé : appelé AVANT toute transition, rendu immédiat (rangées encore libres)
  function cancelPreview(by) {
    if (!dropPreview(by)) { return false; }
    render();
    return true;
  }
  function tickPreview() {
    pvTimer = null;
    if (!S.preview) { return; }
    if (mono() >= S.preview.until) { dropPreview("expiry"); render(); return; }   // retour automatique
    drawPreviewChip();
    pvTimer = setTimeout(tickPreview, Math.min(1000, Math.max(16, S.preview.until - mono())));
  }

  // ---- chemin agent : aperçu temporaire, puis confirmation littérale ; rien d'autre ne publie
  function preview(patch, ms) {
    var busy = busyReason(), res;
    if (busy) { return fail("ui_busy", busy); }
    res = applyPatch(published(), patch);
    if (!res.ok) { return res; }
    ms = typeof ms === "number" && isFinite(ms) ? Math.max(PREVIEW_MIN_MS, Math.min(PREVIEW_MAX_MS, ms)) : PREVIEW_DEFAULT_MS;
    if (!S.preview) { S.anchorRow = rowsData[F.r] ? rowsData[F.r].id : null; }   // focus d'avant l'aperçu
    dropPreview("agent");
    S.preview = { scene: res.scene, until: mono() + ms, base: S.revision };
    S.endedBy = null;
    pvTimer = setTimeout(tickPreview, Math.min(1000, ms));
    render();
    return { ok: true, expiresInMs: ms, scene: clone(res.scene) };
  }

  function confirm(flag) {
    var p, busy;
    if (flag !== true) { return fail("confirmation_required"); }
    busy = busyReason();
    if (busy) { return fail("ui_busy", busy); }            // jamais sous un panneau, une lecture… ou une touche
    p = previewLive();
    if (!p) { cancelPreview("expiry"); return fail("no_preview"); }
    if (p.base !== S.revision) { cancelPreview("agent"); return fail("stale_preview"); }
    publish(p.scene);
    dropPreview("agent");
    render();
    return { ok: true, revision: S.revision };
  }

  function cancel() { cancelPreview("agent"); return { ok: true }; }

  // ---- PROPRIÉTAIRE SEULEMENT (Réglages, commandes du laboratoire) : jamais exposé au chemin agent
  function undo() {
    if (!S.history.length) { return fail("nothing_to_rollback"); }
    S.history.pop();
    S.revision++;
    dropPreview("owner");
    render();
    return { ok: true, revision: S.revision };
  }

  function reset() {
    S.history = [];
    S.revision++;
    dropPreview("owner");
    render();
    return { ok: true, revision: S.revision };
  }

  function select(name) {
    var busy;
    if (typeof name !== "string" || !own(PRESETS, name)) { return fail("invalid_value", "variant"); }
    busy = busyReason();
    if (busy && busy !== "navigating") { return fail("ui_busy", busy); }
    publish(PRESETS[name]);
    dropPreview("owner");
    render();
    return { ok: true, revision: S.revision };
  }

  function ownerPatch(patch) {
    var res = applyPatch(published(), patch);
    if (!res.ok) { return res; }
    publish(res.scene);
    dropPreview("owner");
    render();
    return { ok: true, revision: S.revision };
  }

  // ------------------------------------------------------------------ pause et performance
  var raf = null, gaps = [], lastFrame = 0;
  function frame(t) {
    raf = null;
    if (lastFrame) {
      gaps.push(t - lastFrame);
      if (gaps.length > 90) { gaps.shift(); }
      var bad = 0;
      for (var i = 0; i < gaps.length; i++) { if (gaps[i] > 70) { bad++; } }
      if (gaps.length >= 60 && bad >= 45) { setFlag("degraded", true); return; }   // fond statique, dur
    }
    lastFrame = t;
    monitor();
  }
  function monitor() {
    var running = !pauseReasons().length && effectiveMotion(visible()) === "full";
    if (running && raf === null && window.requestAnimationFrame) {
      raf = window.requestAnimationFrame(frame);
    } else if (!running) {
      if (raf !== null && window.cancelAnimationFrame) { window.cancelAnimationFrame(raf); }
      raf = null;
      lastFrame = 0;
      gaps = [];
    }
  }

  function setFlag(name, on) {
    if (!own(S.flags, name) || name === "panel") { return fail("invalid_state"); }
    on = !!on;
    if (on && inList(name, CANCEL_FLAGS)) { cancelPreview("busy"); }    // avant la transition
    S.flags[name] = on;
    $("setup").hidden = !S.flags.setup;
    $("offline").hidden = !S.flags.offline;
    $("playov").hidden = !(S.flags.playback || S.flags.loading);
    if (S.flags.playback || S.flags.loading) { $("playtitle").textContent = currentItem() ? currentItem().t : ""; }
    render();
    return { ok: true };
  }

  // ------------------------------------------------------------------ panneaux et navigation (v23)
  function openPanel(kind) {
    var it, btns = $("pbtns"), labels, i;
    cancelPreview("busy");                                             // avant la transition
    it = currentItem();
    F.panelKind = kind;
    F.btn = 0;
    S.flags.panel = true;
    $("panel").className = "panel" + (kind === "sheet" ? " sheet" : "");
    clear(btns);
    if (kind === "sheet") {
      $("pkicker").textContent = "OpenTV";
      $("ptitle").textContent = "Réglages";
      $("pmeta").textContent = "Scène d'accueil : " + visible().variant + " · mouvement " + visible().motion + ".";
      labels = ["Annuler la dernière scène", "Scène OpenTV d'origine",
                "Mouvement du fond : " + ({ full: "complet", reduced: "réduit", off: "coupé" })[published().motion],
                "Fermer"];
    } else {
      $("pkicker").textContent = ROW_LABELS[rowsData[F.r].id];
      $("ptitle").textContent = it.t;
      $("pmeta").textContent = it.y + " · " + it.g + " · IMDb " + it.s + "/10";
      labels = ["Regarder", "J'aime", "À voir plus tard", "Fermer"];
    }
    for (i = 0; i < labels.length; i++) { btns.appendChild(el("button", "", labels[i])); }
    $("panel").hidden = false;
    drawButtons();
    drawFocus();
    render();
  }
  function closePanel() {
    S.flags.panel = false;
    F.panelKind = null;
    $("panel").hidden = true;
    drawFocus();
    render();
  }
  function drawButtons() {
    var b = $("pbtns").children;
    for (var i = 0; i < b.length; i++) {
      b[i].className = (i === F.btn ? "focused" : "") + (b[i].getAttribute("data-on") ? " on" : "");
    }
  }
  function activate() {
    var b = $("pbtns").children[F.btn], label = b ? b.textContent : "";
    if (F.panelKind === "sheet") {
      closePanel();
      if (F.btn === 0) { undo(); } else if (F.btn === 1) { reset(); } else if (F.btn === 2) {
        ownerPatch({ motion: { full: "reduced", reduced: "off", off: "full" }[published().motion] });
      }
      return;
    }
    if (label === "Regarder") { closePanel(); setFlag("playback", true); return; }
    if (label === "Fermer") { closePanel(); return; }
    if (b.getAttribute("data-on")) { b.removeAttribute("data-on"); } else { b.setAttribute("data-on", "1"); }
    drawButtons();
  }

  function onKey(e) {
    var k = e.key, back = k === "Escape" || k === "Backspace" || k === "BrowserBack" || k === "GoBack" || k === "Close";
    var nav = k === "ArrowUp" || k === "ArrowDown" || k === "ArrowLeft" || k === "ArrowRight" || k === "Enter" || back;
    if (!nav) { return; }
    e.preventDefault();
    S.lastKey = mono();
    if (S.preview) {                     // la main humaine gagne : l'aperçu tombe AVANT que la touche n'agisse
      var seen = focusKey();
      cancelPreview("human");
      if (focusKey() !== seen) { drawFocus(); render(); return; }    // titre déplacé : la touche annule seulement
    }
    if (S.flags.playback || S.flags.loading) {
      if (back) { setFlag("loading", false); setFlag("playback", false); }
      return;
    }
    if (S.flags.setup || S.flags.offline) { return; }
    if (S.flags.panel) {
      var n = $("pbtns").children.length;
      if (back) { closePanel(); return; }
      if (k === "Enter") { activate(); return; }
      var fwd = F.panelKind === "sheet" ? (k === "ArrowDown" || k === "ArrowRight") : k === "ArrowRight";
      var bwd = F.panelKind === "sheet" ? (k === "ArrowUp" || k === "ArrowLeft") : k === "ArrowLeft";
      if (fwd) { F.btn = Math.min(n - 1, F.btn + 1); } else if (bwd) { F.btn = Math.max(0, F.btn - 1); }
      drawButtons();
      return;
    }
    if (F.zone === "top") {
      if (k === "Enter") { openPanel("sheet"); } else if (k === "ArrowDown" || back) { F.zone = "rows"; drawFocus(); render(); }
      return;
    }
    var rd = rowsData[F.r];
    if (k === "ArrowLeft") { F.cols[rd.id] = Math.max(0, col(F.r) - 1); }
    else if (k === "ArrowRight") { F.cols[rd.id] = Math.min(rd.ids.length - 1, col(F.r) + 1); }
    else if (k === "ArrowDown") { F.r = Math.min(rowsData.length - 1, F.r + 1); }
    else if (k === "ArrowUp" || back) {
      if (F.r > 0) { F.r--; } else if (k === "ArrowUp") { F.zone = "top"; }
    } else if (k === "Enter") { openPanel("card"); return; }
    drawFocus(false, !!e.repeat);
    render();
  }

  function focusKey() {
    var it = currentItem();
    return F.zone + "|" + (rowsData[F.r] ? rowsData[F.r].id : "") + "|" + (it ? it.id : "");
  }

  // ------------------------------------------------------------------ horloge, QR de démonstration
  var DAYS = ["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"];
  var MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
                "novembre", "décembre"];
  function drawClock() {
    var d = new Date(wallNow()), h = d.getHours(), m = d.getMinutes();
    $("clock").textContent = (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
    $("date").textContent = DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()];
    setTimeout(drawClock, 15000);
  }

  function drawDemoQr() {
    var c = $("rqr"), g = c.getContext ? c.getContext("2d") : null, n = 29, px = 8, x, y, seed = 20261030;
    if (!g) { return; }
    g.fillStyle = "#fff";
    g.fillRect(0, 0, 232, 232);
    g.fillStyle = "#000";
    function rnd() { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }
    function finder(ox, oy) {
      g.fillRect(ox * px, oy * px, 7 * px, 7 * px);
      g.fillStyle = "#fff"; g.fillRect((ox + 1) * px, (oy + 1) * px, 5 * px, 5 * px);
      g.fillStyle = "#000"; g.fillRect((ox + 2) * px, (oy + 2) * px, 3 * px, 3 * px);
    }
    for (y = 4; y < n - 4; y++) {
      for (x = 4; x < n - 4; x++) {
        var inF = (x < 12 && y < 12) || (x > n - 13 && y < 12) || (x < 12 && y > n - 13);
        if (!inF && rnd() < 0.48) { g.fillRect(x * px, y * px, px, px); }
      }
    }
    finder(4, 4); finder(n - 11, 4); finder(4, n - 11);
  }

  // ------------------------------------------------------------------ laboratoire
  function drawLabState() {
    var box = $("labstate");
    if (box) { box.textContent = JSON.stringify(snapshot(), null, 1); }
  }
  function snapshot() {
    var p = previewLive();
    return { published: clone(published()), visible: clone(visible()), publishedCanon: canonical(published()),
             visibleCanon: canonical(visible()), revision: S.revision, history: S.history.length,
             previewLeftMs: p ? Math.max(0, p.until - mono()) : 0, paused: pauseReasons().length > 0,
             pauseReasons: pauseReasons(), motion: effectiveMotion(visible()), bootError: S.bootError,
             zone: F.zone, row: rowsData[F.r] ? rowsData[F.r].id : null, focus: currentItem() ? currentItem().t : null,
             panel: S.flags.panel ? F.panelKind : null, rowsOrder: rowIds() };
  }
  function rowIds() {
    var out = [], i;
    for (i = 0; i < rowsData.length; i++) { out.push(rowsData[i].id); }
    return out;
  }
  function onLabClick(e) {
    var t = e.target, act = t && t.getAttribute ? t.getAttribute("data-lab") : null, arg, patch;
    if (!act) { return; }
    arg = t.getAttribute("data-arg") || "";
    if (act === "select") { select(arg); }
    else if (act === "preview") { patch = {}; patch[arg.split(":")[0]] = arg.split(":")[1]; preview(patch, 10000); }
    else if (act === "confirm") { confirm(true); }
    else if (act === "cancel") { cancel(); }
    else if (act === "undo") { undo(); }
    else if (act === "reset") { reset(); }
    else if (act === "toggle") { setFlag(arg, !S.flags[arg]); }
  }

  // requête du laboratoire : seulement « variant » et « motion » ; un encodage invalide (URIError) ⇒ tout ignoré
  function parseQuery() {
    var q = (location.search || "").replace(/^\?/, ""), out = { variant: null, motion: null }, parts, i, kv, k, v;
    if (!q) { return out; }
    parts = q.split("&");
    try {
      for (i = 0; i < parts.length; i++) {
        kv = parts[i].split("=");
        k = decodeURIComponent(kv[0] || "");
        v = decodeURIComponent(kv.slice(1).join("="));
        if ((k === "variant" || k === "motion") && out[k] === null) { out[k] = v; }
      }
    } catch (err) {
      return null;
    }
    return out;
  }

  function boot() {
    var q = parseQuery(), v = q ? q.variant : null, m = q ? q.motion : null;
    if (!q) { S.bootError = "invalid_query"; }
    S.reducedMQ = !!(mq && mq.matches);
    if (mq && mq.addListener) { mq.addListener(function (ev) { S.reducedMQ = !!ev.matches; render(); }); }
    if (v !== null) { if (own(PRESETS, v) && inList(v, ENUMS.variant)) { publish(PRESETS[v]); } else { S.bootError = "invalid_variant"; } }
    if (m !== null) { if (!ownerPatch({ motion: m }).ok) { S.bootError = "invalid_motion"; } }
    S.flags.hidden = !!document.hidden;
    document.addEventListener("visibilitychange", function () { S.flags.hidden = !!document.hidden; render(); });
    document.addEventListener("keydown", onKey);
    var lab = $("lab");
    if (lab) { lab.addEventListener("click", onLabClick); }
    drawClock();
    drawDemoQr();
    render();
    $("tv").setAttribute("data-ready", "1");
  }

  window.OTVCanvas = Object.freeze({
    // chemin agent : un aperçu temporaire seulement ; publier exige confirm(true) littéral (consentement du
    // propriétaire relayé), refusé tant que l'interface est occupée. Aucun autre accès en écriture.
    agent: Object.freeze({ preview: preview, confirm: confirm, cancel: cancel }),
    // PROPRIÉTAIRE SEULEMENT (Réglages et commandes du laboratoire) : jamais exposé à un agent
    owner: Object.freeze({ select: select, patch: ownerPatch, undo: undo, reset: reset }),
    // laboratoire : états simulés, lecture seule
    lab: Object.freeze({
      setState: setFlag, snapshot: snapshot,
      validate: function (d) { var r = validateScene(d); return r.ok ? { ok: true, scene: clone(r.scene) } : r; },
      presets: function () { var o = {}, k; for (k in PRESETS) { if (own(PRESETS, k)) { o[k] = clone(PRESETS[k]); } } return o; }
    })
  });
  boot();
}());
