/* OpenTV Pi — OpenTV Canvas : rendu de la scène d'ambiance sur la page /tv (Chromium Raspberry Pi OS, ES5 strict).
   Rôle : PEINDRE seulement. Aucune requête réseau, aucun minuteur d'API, aucun texte ni style venus du service :
   la page (tv.js) passe la scène publiée — ou l'aperçu d'un agent, uniquement pendant la feuille de confirmation du
   propriétaire — et ce module n'en lit que les champs FERMÉS du schéma v1 (wallpaper, motion, accent, shade,
   hero_focus ; row_order est géré par tv.js). Toute scène invalide ⇒ ambiance d'origine. Seules des classes issues de
   ces énumérations (préfixe « cv- ») sont posées sur <body> ; les couches du fond (#cvwall dans #bg, sous le voile)
   sont fixes dans tv.html. La coque (configuration, Réglages, confirmations, boutons, QR, lecteur, hors-ligne) n'est
   jamais stylée par la scène.
   Mouvement : au plus quatre couches continues (.cv-l1, .cv-l2, .cv-l3, #cvglow), transform/opacity seulement, aucun
   filtre, flou, vidéo ni worker. Figées (pause) pendant un panneau, la feuille de confirmation Canvas (aperçu figé),
   la lecture et son retour, le hors-ligne ou une page cachée ; fond fixe (aucune animation) en configuration,
   mouvement « off », préférence « réduire les animations » (relue en direct à chaque rendu) ou performance dégradée
   (cadence d'images mesurée par rafales bornées, définitif pour la session).
   Halo du héros : teinte du visuel dessiné de la carte au focus (même empreinte que tv.js), quantifiée en douze
   classes fermées ; jamais une adresse d'image, jamais le fond entier qui change de carte en carte. */
(function () {
  "use strict";

  // miroir exact de opentv_pi/canvas_schema.py (vérifié par tests/test_canvas_ui.py)
  var ENUMS = {"wallpaper": ["brume", "affiche", "aurore"], "motion": ["off", "calm", "alive"], "accent": ["ice", "ember", "jade", "violet"], "shade": ["soft", "balanced", "deep"], "hero_focus": ["title", "artwork", "calm"]};
  var ROW_IDS = ["tonight", "watchlist", "new", "top", "more"];
  var DEFAULT = {"schema_version": 1, "wallpaper": "aurore", "motion": "calm", "accent": "ice", "shade": "balanced", "hero_focus": "title", "row_order": ["tonight", "watchlist", "new", "top", "more"]};
  var KEYS = ["schema_version", "wallpaper", "motion", "accent", "shade", "hero_focus", "row_order"];
  var ENUM_KEYS = ["wallpaper", "motion", "accent", "shade", "hero_focus"];
  var FIRST_ROW = "tonight";
  // « sheet » : feuille de confirmation Canvas (l'aperçu est peint figé, jamais en mouvement sous le choix)
  var PAUSE_REASONS = ["setup", "panel", "sheet", "playback", "offline", "hidden"];
  // sonde de cadence par RAFALES (budget CPU du Pi) : les couches tournent sur le compositeur ; une boucle rAF
  // permanente forcerait à chaque vsync une image du fil principal (style, commit). Une rafale mesure au plus WIN_MAX
  // intervalles consécutifs (≥ BAD_MIN intervalles > GAP_MS dès WIN_MIN mesurés ⇒ dégradé), puis dort PROBE_SLEEP_MS
  // sans aucun rAF. Rafale immédiate quand le mouvement démarre ou reprend ; rAF et minuteur annulés hors mouvement.
  var GAP_MS = 70, WIN_MIN = 60, WIN_MAX = 90, BAD_MIN = 45, PROBE_SLEEP_MS = 12000;
  var GLOW_DELAY_MS = 180;          // répétition de touche : le halo suit la carte quand la télécommande se pose
  var HUES = 12;                    // teintes fermées du halo (cv-h0 … cv-h11), cv-hn = neutre (accent)

  // libellés fermés (français d'abord) : seuls textes que ce module produit
  var LABELS = {
    wallpaper: { aurore: "Aurore", brume: "Brume", affiche: "Affiche" },
    motion: { off: "Coupé", calm: "Calme", alive: "Vivant" },
    accent: { ice: "Glacier", ember: "Braise", jade: "Jade", violet: "Violet" },
    shade: { soft: "Léger", balanced: "Équilibré", deep: "Profond" },
    hero_focus: { title: "Titre", artwork: "Affiche", calm: "Calme" },
    row: { tonight: "Le choix de ce soir", watchlist: "À voir plus tard", "new": "Nouveautés", top: "Les mieux notés",
           more: "Autres suggestions" }
  };
  var FIELD_LABELS = { wallpaper: "Fond", motion: "Mouvement", accent: "Couleur", shade: "Voile", hero_focus: "Héros",
                       row_order: "Ordre des rangées" };
  var STYLE_OF = { aurore: "Canvas", brume: "Brume", affiche: "Cinéma" };
  var PILL = { aurore: "Ce soir pour toi", brume: "Brume du soir", affiche: "Soirée cinéma" };

  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function inList(v, list) {
    for (var i = 0; i < list.length; i++) { if (list[i] === v) { return true; } }
    return false;
  }
  function isPlain(o) { return o !== null && typeof o === "object" && Object.prototype.toString.call(o) === "[object Object]"; }

  // ------------------------------------------------------------------ validation fermée (échec : ambiance d'origine)
  function valid(sc) {
    var k, i;
    if (!isPlain(sc)) { return false; }
    for (k in sc) { if (own(sc, k) && !inList(k, KEYS)) { return false; } }
    for (i = 0; i < KEYS.length; i++) { if (!own(sc, KEYS[i])) { return false; } }
    if (sc.schema_version !== 1) { return false; }
    for (i = 0; i < ENUM_KEYS.length; i++) {
      k = ENUM_KEYS[i];
      if (typeof sc[k] !== "string" || !inList(sc[k], ENUMS[k])) { return false; }
    }
    var rows = sc.row_order, seen = {};
    if (Object.prototype.toString.call(rows) !== "[object Array]" || rows.length !== ROW_IDS.length) { return false; }
    for (i = 0; i < rows.length; i++) {
      if (typeof rows[i] !== "string" || !inList(rows[i], ROW_IDS) || own(seen, rows[i])) { return false; }
      seen[rows[i]] = true;
    }
    return rows[0] === FIRST_ROW;
  }
  function copy(sc) {
    var o = {}, i;
    for (i = 0; i < KEYS.length; i++) { o[KEYS[i]] = KEYS[i] === "row_order" ? sc.row_order.slice() : sc[KEYS[i]]; }
    return o;
  }

  function label(field, value) {
    var t = own(LABELS, field) ? LABELS[field] : null;
    return t && typeof value === "string" && own(t, value) ? t[value] : null;
  }
  // feuille de confirmation : description FERMÉE des changements [{field, from, to}] -> [{label, from, to}]
  function describe(changes) {
    var out = [], i, c, f, t;
    if (Object.prototype.toString.call(changes) !== "[object Array]") { return out; }
    for (i = 0; i < changes.length && i < 8; i++) {
      c = changes[i];
      if (!isPlain(c) || typeof c.field !== "string" || !own(FIELD_LABELS, c.field)) { continue; }
      if (c.field === "row_order") {
        if (Object.prototype.toString.call(c.to) !== "[object Array]") { continue; }
        t = [];
        for (f = 0; f < c.to.length && t.length < 3; f++) { if (label("row", c.to[f])) { t.push(label("row", c.to[f])); } }
        if (t.length) { out.push({ label: FIELD_LABELS.row_order, from: null, to: t.join(", ") + "…" }); }
        continue;
      }
      if (label(c.field, c.from) && label(c.field, c.to)) {
        out.push({ label: FIELD_LABELS[c.field], from: label(c.field, c.from), to: label(c.field, c.to) });
      }
    }
    return out;
  }

  // ------------------------------------------------------------------ état
  var $ = function (id) { return document.getElementById(id); };
  var mq = null;
  try { mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null; } catch (e) { mq = null; }
  // éteint jusqu'au premier état du service (tv.js appelle apply) : fond OpenTV v23 fixe, jamais d'aurore avant de
  // savoir si la configuration (fond v23) ou un service sans Canvas doit s'afficher
  var S = { on: false, scene: copy(DEFAULT), invalid: false, preview: false, setup: false, reasons: [], degraded: false,
            want: -1, hue: null, glow: "cvga", glowTimer: null, mode: "",
            raf: null, sleep: null, gen: 0, last: 0, n: 0, bad: 0, probes: 0 };   // sonde : n intervalles, bad lents

  function reduced() {   // préférence lue EN DIRECT (l'événement « change » peut arriver après le rendu)
    try { return !!(mq && mq.matches); } catch (e) { return false; }
  }
  function mode() {
    if (!S.on) { return "off"; }
    if (S.setup || S.scene.motion === "off" || S.degraded || reduced()) { return "static"; }
    if (S.reasons.length || document.hidden) { return "paused"; }
    return "run";
  }

  function draw() {
    var sc = S.scene, m = mode(), cls;
    if (!S.on) { cls = []; }
    else if (S.setup) { cls = ["cv-setup"]; }   // configuration : fond OpenTV v23 inchangé, aucune couche
    else {
      cls = ["cv", "cv-wp-" + sc.wallpaper, "cv-acc-" + sc.accent, "cv-shade-" + sc.shade, "cv-hero-" + sc.hero_focus,
             "cv-m-" + sc.motion, m === "static" ? "cv-static" : "cv-anim"];
      if (m === "paused") { cls.push("cv-paused"); }
      if (S.preview) { cls.push("cv-preview"); }
    }
    document.body.className = cls.join(" ");
    document.body.setAttribute("data-cv-mode", m);
    document.body.setAttribute("data-cv-degraded", S.degraded ? "1" : "0");
    var pill = $("cvpill");
    if (pill) {
      pill.hidden = !(S.on && !S.setup);
      pill.textContent = S.preview ? "Aperçu de l'ambiance" : (sc.hero_focus === "calm" ? "Soirée calme" : PILL[sc.wallpaper]);
    }
    S.mode = m;
    monitor();
  }

  // ------------------------------------------------------------------ halo du héros (teinte du visuel au focus)
  function focus(h) {
    var q = typeof h === "number" && isFinite(h) && h >= 0 ? Math.floor((Math.floor(h) % 360) / (360 / HUES)) : -1;
    if (q === S.want && (S.glowTimer || S.hue === q)) { return; }
    S.want = q;
    if (S.glowTimer) { clearTimeout(S.glowTimer); }
    S.glowTimer = setTimeout(swapGlow, GLOW_DELAY_MS);
  }
  function swapGlow() {
    S.glowTimer = null;
    if (S.want === S.hue) { return; }
    var next = S.glow === "cvga" ? "cvgb" : "cvga", n = $(next), c = $(S.glow);
    if (!n || !c) { return; }
    n.className = "cv-g " + (S.want >= 0 ? "cv-h" + S.want : "cv-hn") + " on";   // fondu enchaîné : opacité seulement
    c.className = c.className.replace(/ on$/, "");
    S.glow = next;
    S.hue = S.want;
  }

  // ------------------------------------------------------------------ cadence d'images (repli statique)
  // Chaque rappel porte la génération de sa sonde : un rappel rAF ou un réveil périmé (sonde arrêtée ou remplacée
  // entre-temps) ne fait rien, jamais une seconde chaîne ni une relance hors mouvement.
  function stopProbe() {
    S.gen++;
    if (S.raf !== null && window.cancelAnimationFrame) { window.cancelAnimationFrame(S.raf); }
    if (S.sleep !== null) { clearTimeout(S.sleep); }
    S.raf = null; S.sleep = null; S.last = 0; S.n = 0; S.bad = 0;
  }
  function nextFrame(g) {
    S.raf = window.requestAnimationFrame(function (t) { frame(t, g); });
  }
  function startProbe() {
    var g = S.gen;
    S.sleep = null; S.last = 0; S.n = 0; S.bad = 0;
    S.probes++;
    nextFrame(g);
  }
  function wake(g) {
    if (g !== S.gen) { return; }
    S.sleep = null;
    if (mode() !== "run") { stopProbe(); return; }
    startProbe();
  }
  function frame(t, g) {   // temps constant : deux compteurs, aucune liste d'intervalles
    if (g !== S.gen) { return; }
    S.raf = null;
    if (mode() !== "run") { stopProbe(); return; }
    if (S.last) {
      S.n++;
      if (t - S.last > GAP_MS) { S.bad++; }
      if (S.n >= WIN_MIN && S.bad >= BAD_MIN) { S.degraded = true; stopProbe(); draw(); return; }
      if (S.n >= WIN_MAX) {
        S.last = 0;
        S.sleep = setTimeout(function () { wake(g); }, PROBE_SLEEP_MS);
        return;
      }
    }
    S.last = t;
    nextFrame(g);
  }
  function monitor() {
    if (S.mode !== "run") {
      if (S.raf !== null || S.sleep !== null) { stopProbe(); }
    } else if (S.raf === null && S.sleep === null && window.requestAnimationFrame) {
      startProbe();
    }
  }

  // ------------------------------------------------------------------ API de la page
  // scène publiée (ou aperçu pendant la feuille) ; opts : {on, preview, setup}
  function apply(sc, opts) {
    opts = opts || {};
    S.on = opts.on !== false;
    S.invalid = sc !== null && sc !== undefined && !valid(sc);
    S.scene = valid(sc) ? copy(sc) : copy(DEFAULT);
    S.preview = !!opts.preview && !S.invalid;
    S.setup = !!opts.setup;
    draw();
    return !S.invalid;
  }
  function pause(list) {
    var out = [], i;
    if (Object.prototype.toString.call(list) === "[object Array]") {
      for (i = 0; i < list.length; i++) { if (inList(list[i], PAUSE_REASONS) && !inList(list[i], out)) { out.push(list[i]); } }
    }
    out.sort();
    if (out.join(",") === S.reasons.join(",") && mode() === S.mode) { return; }
    S.reasons = out;
    draw();
  }
  function snapshot() {
    return { on: S.on, mode: mode(), scene: copy(S.scene), invalid: S.invalid, preview: S.preview, setup: S.setup,
             reasons: S.reasons.slice(), degraded: S.degraded, reduced: reduced(), hue: S.hue, gaps: S.n,
             probing: S.raf !== null, sleeping: S.sleep !== null, probes: S.probes };
  }

  if (mq) {
    var onPref = function () { draw(); };
    if (mq.addEventListener) { mq.addEventListener("change", onPref); } else if (mq.addListener) { mq.addListener(onPref); }
  }
  document.addEventListener("visibilitychange", function () { draw(); });
  S.reasons = ["boot"];   // premier rendu (on: false) : fond v23 fixe jusqu'au premier état (jamais un écran vide)
  draw();

  window.OpenTVCanvas = Object.freeze({
    apply: apply, pause: pause, focus: focus, valid: valid, describe: describe, label: label,
    styleName: function (wallpaper) { return typeof wallpaper === "string" && own(STYLE_OF, wallpaper) ? STYLE_OF[wallpaper] : null; },
    state: snapshot
  });
}());
