/* OpenTV Pi — touches de télécommande -> actions OpenTV. ES5 strict (compatible Chromium 56), aucune dépendance.
 *
 * Sources couvertes, sans surveillance ni injection : clavier USB, télécommandes USB 2,4 GHz « HID » (air mouse,
 * mini-claviers : flèches, OK = Entrée/Entrée du pavé, Retour = Échap/Retour arrière/BrowserBack, Accueil =
 * BrowserHome/Home) et télécommande de la TV par HDMI-CEC (noyau vc4-hdmi : flèches ; Select -> Entrée grâce à
 * opentv-cec ; Exit -> XF86Close = « Close »). Actions : up, down, left, right, ok, back, home — rien d'autre.
 * Touches média : aucune action OpenTV ne leur correspond aujourd'hui (pas de lecteur) ; elles ne sont pas captées.
 * `isBrowserNav` signale les touches de navigation du navigateur (Retour, Suivant, Accueil, Recherche, Favoris…) :
 * leur action par défaut est toujours empêchée pour que le kiosque ne quitte jamais /tv. */
(function (root) {
  "use strict";
  var has = Object.prototype.hasOwnProperty;
  var BY_KEY = {
    ArrowUp: "up", Up: "up", ArrowDown: "down", Down: "down",
    ArrowLeft: "left", Left: "left", ArrowRight: "right", Right: "right",
    Enter: "ok", Accept: "ok", Select: "ok",
    Escape: "back", Esc: "back", Backspace: "back", BrowserBack: "back", GoBack: "back", Close: "back", Exit: "back",
    BrowserHome: "home", Home: "home", GoHome: "home"
  };
  // repli si `key` est absent ou « Unidentified » (anciens pilotes HID) : codes DOM historiques
  var BY_KEYCODE = { 38: "up", 40: "down", 37: "left", 39: "right", 13: "ok", 27: "back", 8: "back", 166: "back",
                     172: "home", 36: "home" };
  var BROWSER_KEYS = { BrowserBack: 1, BrowserForward: 1, BrowserHome: 1, BrowserSearch: 1, BrowserFavorites: 1,
                       BrowserStop: 1, GoBack: 1, GoHome: 1 };
  var BROWSER_KEYCODES = { 166: 1, 167: 1, 172: 1, 170: 1, 171: 1, 169: 1 };

  function keyName(e) {
    var k = e && typeof e.key === "string" ? e.key : "";
    return k === "Unidentified" ? "" : k;
  }
  function keyCode(e) { return (e && (e.keyCode || e.which)) || 0; }

  function normalize(e) {
    if (!e || e.isComposing || e.altKey || e.ctrlKey || e.metaKey) { return null; }
    var k = keyName(e), kc = keyCode(e);
    if (k) { return has.call(BY_KEY, k) ? BY_KEY[k] : (e.code === "NumpadEnter" ? "ok" : null); }
    if (e.code === "NumpadEnter") { return "ok"; }
    return has.call(BY_KEYCODE, kc) ? BY_KEYCODE[kc] : null;
  }

  function isBrowserNav(e) {
    if (!e) { return false; }
    var k = keyName(e);
    if (k) { return has.call(BROWSER_KEYS, k); }
    return has.call(BROWSER_KEYCODES, keyCode(e));
  }

  root.OpenTVKeys = { normalize: normalize, isBrowserNav: isBrowserNav };
})(typeof window !== "undefined" ? window : this);
