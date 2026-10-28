"""Assistance de lecture (page parente seulement), portée du pont NanoPi (ASSIST_*_JS) et durcie pour la révélation.

Scripts statiques (aucune donnée du catalogue ni de la page interpolée), exécutés dans un monde isolé CDP recréé à
chaque document : les scripts de la page ne voient ni ne falsifient leurs prototypes. Ils lisent uniquement la
géométrie/visibilité/styles calculés des éléments du document principal ; jamais d'entrée dans une iframe, jamais de
lecture de src/currentSrc/srcObject, rien n'est retiré ni masqué. Le lecteur dominant reçoit un style fixe plein écran
sur la page parente et ses ANCÊTRES sont neutralisés (aucun contexte d'empilement : z-index, transform, filter,
opacity, isolation, will-change, contain…, comme le permet la règle « styler l'iframe et ses ancêtres ») ; la
révélation exige ensuite qu'il couvre toute la fenêtre ET qu'une grille de points (centre, intérieur, coins) touche ce
seul lecteur : aucun en-tête, fiche ou publicité de la page parente ne peut être visible.

Invariant de peinture (fail-closed) : le lecteur est dans le contexte d'empilement RACINE à z-index maximal. CHECK
refuse donc (« obstructed ») :
- tout ancêtre (racines fantômes et slots traversés, <html> excepté) qui crée un contexte d'empilement — z-index sur un
  élément positionné ou un élément de flex/grid, position fixed/sticky, opacity < 1, transform/scale/rotate/translate,
  perspective, filter, backdrop-filter, clip-path, mask, mix-blend-mode, isolation, will-change, contain
  (layout/paint/strict/content), container-type, content-visibility, view-transition-name, box-reflect : la page
  pourrait alors peindre un calque de z-index ordinaire au-dessus du lecteur, même avec pointer-events:none ;
- tout lecteur ou ancêtre translucide, filtré, découpé, masqué ou fondu ;
- toute couche supérieure (:modal, :popover-open, :fullscreen étranger) ;
- tout élément ou pseudo-élément à z-index maximal hors du lecteur qui peut peindre : sa boîte visible dans la
  fenêtre, une ombre/un contour, du texte, ou N'IMPORTE QUEL descendant rendu (un conteneur de taille nulle à z-index
  maximal dont l'enfant déborde est un calque) ;
- DOM trop grand pour être validé dans le budget (MAX_SCAN éléments, racines fantômes ouvertes comprises).
`elementFromPoint` ignore les éléments `pointer-events: none` : la grille seule ne suffit jamais.

Cadence (playerd) : CHECK_FAST_JS (tout sauf le balayage complet du DOM) à chaque tick après la révélation ;
CHECK_JS complet avant la révélation, avant le clic, puis seulement si le DOM a changé (MutationObserver posé par
MAXIMIZE, `dirty`) et au moins toutes les quelques secondes.
"""

OUTCOMES = ("revealed", "no-player", "ambiguous", "foreign", "obstructed", "not_covering", "gone", "timeout",
            "cancelled", "error", "navigated", "crashed")

PROBE_JS = r"""(function () {
  var W = window.innerWidth || 0, H = window.innerHeight || 0;
  var out = {origin: String(location.origin), state: "none", n: 0, kind: ""};
  if (document.readyState === "loading" || !W || !H) { out.state = "loading"; return out; }
  var els = document.querySelectorAll("iframe,video"), c = [], i, e, r, cs;
  for (i = 0; i < els.length && i < 200; i++) {
    e = els[i]; r = e.getBoundingClientRect(); cs = window.getComputedStyle(e);
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) < 0.1) continue;
    if (r.width < 480 || r.height < 270 || r.width * r.height < 0.15 * W * H) continue;
    c.push({e: e, a: r.width * r.height});
  }
  c.sort(function (x, y) { return y.a - x.a; });
  out.n = c.length;
  if (!c.length) return out;
  if (c.length > 1 && c[0].a < 2 * c[1].a) { out.state = "ambiguous"; return out; }
  window.__opentvPlayer = c[0].e;
  out.state = "found"; out.kind = c[0].e.tagName.toLowerCase();
  return out;
})()"""

MAXIMIZE_JS = r"""(function () {
  var e = window.__opentvPlayer;
  if (!e || !e.isConnected) return "gone";
  var p = [["position", "fixed"], ["top", "0"], ["left", "0"], ["right", "0"], ["bottom", "0"],
           ["width", "100vw"], ["height", "100vh"], ["max-width", "none"], ["max-height", "none"],
           ["min-width", "0"], ["min-height", "0"], ["margin", "0"], ["padding", "0"], ["border", "0"],
           ["transform", "none"], ["z-index", "2147483647"], ["visibility", "visible"], ["opacity", "1"],
           ["pointer-events", "auto"], ["filter", "none"], ["clip-path", "none"], ["mask", "none"],
           ["-webkit-mask", "none"], ["mix-blend-mode", "normal"]], i;
  for (i = 0; i < p.length; i++) e.style.setProperty(p[i][0], p[i][1], "important");
  // ancêtres : aucun contexte d'empilement (le lecteur rejoint le contexte racine à z-index maximal) ; jamais <html>
  var q = [["z-index", "auto"], ["transform", "none"], ["scale", "none"], ["rotate", "none"], ["translate", "none"],
           ["perspective", "none"], ["filter", "none"], ["backdrop-filter", "none"], ["-webkit-backdrop-filter", "none"],
           ["opacity", "1"], ["mix-blend-mode", "normal"], ["isolation", "auto"], ["will-change", "auto"],
           ["contain", "none"], ["container-type", "normal"], ["content-visibility", "visible"], ["clip-path", "none"],
           ["mask", "none"], ["-webkit-mask", "none"], ["view-transition-name", "none"],
           ["-webkit-box-reflect", "none"]], a = e, h, st, k;
  for (k = 0; k < 256; k++) {
    h = a.parentNode;
    a = a.assignedSlot || (h && h.nodeType === 11 ? h.host : a.parentElement);
    if (!a || a.nodeType !== 1 || a === document.documentElement) break;
    for (i = 0; i < q.length; i++) a.style.setProperty(q[i][0], q[i][1], "important");
    st = window.getComputedStyle(a);
    if (st.position === "fixed" || st.position === "sticky") a.style.setProperty("position", "static", "important");
  }
  var roots = [document.documentElement, document.body];
  for (i = 0; i < roots.length; i++) if (roots[i]) roots[i].style.setProperty("overflow", "hidden", "important");
  window.scrollTo(0, 0);
  // toute mutation du DOM (calque inséré, style modifié) déclenche un balayage complet au contrôle suivant
  if (!window.__opentvMO && window.MutationObserver) {
    window.__opentvDirty = true;
    window.__opentvMO = new MutationObserver(function () { window.__opentvDirty = true; });
    window.__opentvMO.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  }
  // raccourcis DevTools/code source bloqués tant que la PAGE PARENTE a le focus (monde isolé, même DOM) ; une fois le
  // focus dans l'iframe du lecteur, c'est la fermeture côté contrôleur qui s'applique (playerd, aide des touches)
  if (!window.__opentvKeyGuard) {
    window.__opentvKeyGuard = true;
    window.addEventListener("keydown", function (ev) {
      var c = ev.code || "", m = ev.ctrlKey || ev.metaKey;
      if (c === "F12" || (m && ev.shiftKey && (c === "KeyI" || c === "KeyJ" || c === "KeyC")) ||
          (ev.metaKey && ev.altKey && (c === "KeyI" || c === "KeyJ" || c === "KeyC")) || (m && c === "KeyU")) {
        ev.preventDefault(); ev.stopImmediatePropagation();
      }
    }, true);
  }
  return "ok";
})()"""

# Révélation : le lecteur couvre la fenêtre, n'est ni translucide ni découpé (lui et ses ancêtres), aucun ancêtre ne crée
# de contexte d'empilement, aucun calque de la page parente ne peut être peint au-dessus (styles calculés,
# pointer-events ignoré) et chaque point d'une grille 5×5 (bords inclus, à 4 px) le touche. FULL=false : tout sauf le
# balayage complet du DOM (contrôle rapide de la lecture en cours).
MAX_SCAN = 8000
_CHECK_TMPL = r"""(function () {
  var e = window.__opentvPlayer, W = window.innerWidth, H = window.innerHeight, Z = 2147483647, MAX = %d, FULL = %s;
  if (!e || !e.isConnected) return {state: "gone"};
  var r = e.getBoundingClientRect();
  if (r.left > 1 || r.top > 1 || r.right < W - 1 || r.bottom < H - 1) return {state: "not_covering"};
  var gcs = window.getComputedStyle, n = 0, dirty = window.__opentvDirty === true;
  function num(v, d) { var f = parseFloat(v); return isNaN(f) ? d : f; }
  function none(v) { return !v || v === "none"; }
  function see(st) {
    return st.display !== "none" && st.visibility === "visible" && num(st.opacity, 1) > 0.01;
  }
  function zmax(st) { return st.zIndex !== "auto" && parseInt(st.zIndex, 10) >= Z; }
  function up(a) {
    var h = a.parentNode;
    return a.assignedSlot || (h && h.nodeType === 11 ? h.host : a.parentElement);
  }
  function stacking(st, pst) {
    if (st.position !== "static" && st.zIndex !== "auto") return "z";
    if (st.position === "fixed" || st.position === "sticky") return "position";
    if (pst && /flex|grid/.test(pst.display) && st.zIndex !== "auto") return "flex_grid_z";
    if (num(st.opacity, 1) < 1) return "opacity";
    if (!none(st.transform) || !none(st.scale) || !none(st.rotate) || !none(st.translate) ||
        !none(st.perspective)) return "transform";
    if (!none(st.filter) || !none(st.backdropFilter) || !none(st.webkitBackdropFilter)) return "filter";
    if (!none(st.clipPath) || !none(st.maskImage) || !none(st.webkitMaskImage) || !none(st.maskBorderSource) ||
        !none(st.webkitMaskBoxImageSource)) return "mask";
    if (st.mixBlendMode && st.mixBlendMode !== "normal") return "blend";
    if (st.isolation === "isolate") return "isolation";
    if (st.willChange && st.willChange !== "auto") return "will_change";
    if (st.contain && st.contain !== "none") return "contain";
    if (st.containerType && st.containerType !== "normal") return "container";
    if (st.contentVisibility && st.contentVisibility !== "visible") return "content_visibility";
    if (!none(st.viewTransitionName) || !none(st.webkitBoxReflect)) return "other";
    return null;
  }
  var ps0 = gcs(e);
  if (!see(ps0) || num(ps0.opacity, 1) < 0.99 || ps0.position !== "fixed" || parseInt(ps0.zIndex, 10) !== Z)
    return {state: "obstructed", why: "player_style"};
  var a = e, st, pa, pst, sc, k;
  for (k = 0; a && a.nodeType === 1 && k < 512; k++) {   // lecteur et ancêtres : jamais transparent ni découpé
    st = gcs(a);
    if (num(st.opacity, 1) < 0.99 || !none(st.filter) || !none(st.clipPath) ||
        ((st.maskImage || st.webkitMaskImage || "none") !== "none") ||
        (st.mixBlendMode && st.mixBlendMode !== "normal")) return {state: "obstructed", why: "translucent"};
    pa = up(a);
    if (a !== e && a !== document.documentElement) {   // ancêtre : aucun contexte d'empilement
      pst = pa && pa.nodeType === 1 ? gcs(pa) : null;
      sc = stacking(st, pst);
      if (sc) return {state: "obstructed", why: "stacking", how: sc};
    }
    a = pa;
  }
  if (k >= 512) return {state: "obstructed", why: "too_deep"};
  var tops = [":modal", ":popover-open", ":fullscreen"], i, j, q;
  for (i = 0; i < tops.length; i++) {
    try { q = document.querySelectorAll(tops[i]); } catch (x) { q = []; }
    for (j = 0; j < q.length; j++) if (q[j] !== e && !q[j].contains(e)) return {state: "obstructed", why: "top_layer"};
  }
  if (document.fullscreenElement && document.fullscreenElement !== e) return {state: "obstructed", why: "top_layer"};
  function boxPaints(el, s3) {   // boîte, ombre, contour ou texte direct qui peut apparaître dans la fenêtre
    var rs = el.getClientRects(), t, b, c;
    for (t = 0; t < rs.length; t++) {
      b = rs[t];
      if (b.width > 0 && b.height > 0 && b.right > 0 && b.bottom > 0 && b.left < W && b.top < H) return true;
    }
    if (rs.length && (!none(s3.boxShadow) || (s3.outlineStyle !== "none" && num(s3.outlineWidth, 0) > 0)))
      return true;
    for (c = el.firstChild; c; c = c.nextSibling) if (c.nodeType === 3 && /\S/.test(c.data)) return true;
    return false;
  }
  function pseudoPaints(el, onlyMax) {
    var w = ["::before", "::after"], t, s3;
    for (t = 0; t < w.length; t++) {
      s3 = gcs(el, w[t]);
      if (s3 && s3.content && s3.content !== "none" && s3.content !== "normal" && see(s3) && (!onlyMax || zmax(s3)))
        return true;
    }
    return false;
  }
  function rendered(el, s3) {   // ce calque ou N'IMPORTE QUEL descendant peut-il peindre ? (fail-closed)
    if (s3.display === "none" || num(s3.opacity, 1) <= 0.01) return false;   // sous-arbre entier invisible
    if (s3.visibility === "visible" && (boxPaints(el, s3) || pseudoPaints(el, false))) return true;
    if (el.shadowRoot) return true;
    var d = el.querySelectorAll("*"), t, ds;
    for (t = 0; t < d.length; t++) {
      if (++n > MAX) return true;
      ds = gcs(d[t]);
      if (ds.visibility === "visible" && ds.display !== "none" && (boxPaints(d[t], ds) || pseudoPaints(d[t], false) ||
          d[t].shadowRoot || d[t].tagName === "IFRAME" || d[t].tagName === "VIDEO" || d[t].tagName === "CANVAS" ||
          d[t].tagName === "IMG" || d[t].tagName === "svg")) return true;
    }
    return false;
  }
  function scan(root) {
    var all = root.querySelectorAll("*"), el, s2, res, t;
    for (t = 0; t < all.length; t++) {
      if (++n > MAX) return "too_many";
      el = all[t];
      if (el === e) continue;
      if (!el.contains(e) && !e.contains(el)) {
        s2 = gcs(el);
        if (zmax(s2) && rendered(el, s2)) return "overlay";
        if (n > MAX) return "too_many";
      }
      if (pseudoPaints(el, true)) return "overlay";
      if (el.shadowRoot) { res = scan(el.shadowRoot); if (res) return res; }
    }
    return null;
  }
  if (FULL) {
    window.__opentvDirty = false;   // mutations pendant le balayage : reposées par l'observateur
    if (pseudoPaints(document.documentElement, true)) return {state: "obstructed", why: "overlay"};
    var bad = scan(document);
    if (bad) return {state: "obstructed", why: bad};
  }
  var xs = [4, Math.floor(W / 4), Math.floor(W / 2), Math.floor(3 * W / 4), W - 5];
  var ys = [4, Math.floor(H / 4), Math.floor(H / 2), Math.floor(3 * H / 4), H - 5];
  for (i = 0; i < xs.length; i++) for (j = 0; j < ys.length; j++)
    if (document.elementFromPoint(xs[i], ys[j]) !== e) return {state: "obstructed", why: "grid"};
  return {state: "ready", x: Math.floor(W / 2), y: Math.floor(H / 2), w: W, h: H, dirty: dirty, full: FULL};
})()"""
CHECK_JS = _CHECK_TMPL % (MAX_SCAN, "true")
CHECK_FAST_JS = _CHECK_TMPL % (MAX_SCAN, "false")

# Révélation peinte : deux images d'animation de l'onglet désormais visible (borné à 2 s côté page).
PAINTED_JS = r"""new Promise(function (ok) {
  var done = false, fin = function (v) { if (!done) { done = true; ok(v); } };
  setTimeout(function () { fin(document.visibilityState); }, 2000);
  requestAnimationFrame(function () { requestAnimationFrame(function () { fin(document.visibilityState); }); });
})"""


def valid_point(chk):
    if not isinstance(chk, dict) or chk.get("state") != "ready":
        return False
    return all(isinstance(chk.get(k), int) and not isinstance(chk.get(k), bool) and 0 <= chk[k] <= 8192
               for k in ("x", "y"))
