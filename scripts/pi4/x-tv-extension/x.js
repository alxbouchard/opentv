/* OpenTV X TV Mode — script de contenu de x.com (cadre principal seulement).
 *
 * Fil X lisible à la télécommande, sans API X ni action automatique :
 *  ↑ ↓      post précédent / suivant (anneau très visible, post centré) ; en fin de liste, défilement pour que X
 *           charge la suite (aucun clic) ;
 *  OK       ouvre le post au focus (son permalien x.com) ; un bouton, un lien ou un champ au focus garde son
 *           action native ;
 *  ←        partage le post au focus à « Hermit ou Muse » : SEULEMENT son texte visible (≤ 2400) et son auteur visible
 *           (≤ 120) — jamais une URL, un lien, un média, un cookie ni du HTML ; geste humain uniquement ;
 *  Retour   champ de saisie au focus : il le quitte ; vue de post/modale : page précédente ; racine (accueil, connexion) :
 *           retour explicite à OpenTV /tv ;
 *  Accueil  retour à OpenTV /tv.
 * Mode fil seulement (colonne principale présente) : classe html.otvx-tv (CSS TV, x.css) + bandeau d'aide. Page de
 * connexion ou sans fil : rien n'est stylé ni rempli (Alex se connecte lui-même) ; seuls Retour/Accueil ramènent à
 * OpenTV. DOM dynamique de X : MutationObserver regroupé (une passe au plus toutes les REFRESH_MS). Jamais de clic,
 * de « J'aime », de repartage, de réponse ni de publication.
 */
(() => {
  "use strict";
  if (window.top !== window) { return; }
  const TEXT_MAX = 2400;
  const AUTHOR_MAX = 120;
  const POSTS_MAX = 200;
  const REFRESH_MS = 250;
  const BACK_FAILSAFE_MS = 1200;
  const TOAST_MS = 6000;
  const POST_SEL = 'article[data-testid="tweet"]';
  const COLUMN_SEL = '[data-testid="primaryColumn"]';
  const ROOT_PATHS = new Set(["/", "/home", "/login", "/logout", "/explore", "/notifications", "/messages"]);
  const KEYS = {
    ArrowUp: "up", Up: "up", ArrowDown: "down", Down: "down", ArrowLeft: "left", Left: "left",
    ArrowRight: "right", Right: "right", Enter: "ok", Accept: "ok", Select: "ok",
    Escape: "back", Esc: "back", Backspace: "back", BrowserBack: "back", GoBack: "back", Close: "back", Exit: "back",
    BrowserHome: "home", Home: "home", GoHome: "home"
  };
  const KEYCODES = { 38: "up", 40: "down", 37: "left", 39: "right", 13: "ok", 27: "back", 8: "back", 166: "back",
                     172: "home", 36: "home" };
  const STRIP_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
  const HANDLE_RE = /@[A-Za-z0-9_]{1,15}/;
  const PERMALINK_RE = /^\/[A-Za-z0-9_]{1,15}\/status\/[0-9]{1,25}$/;
  const HELP = [["↑ ↓", "Post précédent / suivant"], ["OK", "Ouvrir"], ["←", "Partager à Hermit ou Muse"],
                ["Retour", "Revenir"], ["Accueil", "OpenTV"]];
  const MSG_PICK = "Choisis d'abord un post avec ↑ ↓.";
  const MSG_FAIL = "Partage impossible pour l'instant. Rien n'a été envoyé.";

  let focused = null;
  let help = null;
  let toastEl = null;
  let toastTimer = 0;
  let refreshTimer = 0;
  let backTimer = 0;
  let sharing = false;

  function keyAction(e) {
    if (!e || e.isComposing || e.altKey || e.ctrlKey || e.metaKey) { return null; }
    const k = typeof e.key === "string" && e.key !== "Unidentified" ? e.key : "";
    if (k) { return Object.prototype.hasOwnProperty.call(KEYS, k) ? KEYS[k] : (e.code === "NumpadEnter" ? "ok" : null); }
    return KEYCODES[e.keyCode] || null;
  }
  function editable(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  function control(el) {
    return !!el && (/^(A|BUTTON|INPUT|TEXTAREA|SELECT|SUMMARY)$/.test(el.tagName) || el.isContentEditable ||
                    /^(button|link|menuitem|tab|checkbox|switch|option)$/.test(el.getAttribute("role") || ""));
  }
  function feedMode() { return !!document.querySelector(COLUMN_SEL); }
  function shown(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function posts() {
    const col = document.querySelector(COLUMN_SEL);
    if (!col) { return []; }
    return Array.from(col.querySelectorAll(POST_SEL)).slice(0, POSTS_MAX).filter(shown);
  }
  function clean(s, cap) {
    return Array.from(String(s || "").replace(STRIP_RE, "")).slice(0, cap).join("").trim();
  }

  // ---- bandeau d'aide et message (textContent seulement)
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function ensureHelp(on) {
    if (on && (!help || !help.isConnected)) {
      help = el("div", "otvx-help");
      help.setAttribute("aria-hidden", "true");
      HELP.forEach((h, i) => {
        if (i) { help.appendChild(document.createTextNode(" · ")); }
        help.appendChild(el("b", null, h[0]));
        help.appendChild(document.createTextNode(" " + h[1]));
      });
      document.documentElement.appendChild(help);
    } else if (!on && help) {
      help.remove();
      help = null;
    }
  }
  function toast(text) {
    if (!toastEl || !toastEl.isConnected) {
      toastEl = el("div", "otvx-toast");
      toastEl.setAttribute("role", "status");
      document.documentElement.appendChild(toastEl);
    }
    toastEl.textContent = String(text || "").slice(0, 200);
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { if (toastEl) { toastEl.hidden = true; } }, TOAST_MS);
  }

  // ---- focus des posts
  function setFocus(post) {
    if (focused && focused !== post) { focused.classList.remove("otvx-focus"); }
    focused = post;
    if (post) {
      post.classList.add("otvx-focus");
      post.scrollIntoView({ block: "center", inline: "nearest" });
    }
  }
  function firstInView(list) {
    return list.find((p) => p.getBoundingClientRect().bottom > 80) || list[0] || null;
  }
  function move(dir) {
    const list = posts();
    if (!list.length) { return; }
    const i = focused && focused.isConnected ? list.indexOf(focused) : -1;
    if (i < 0) { setFocus(firstInView(list)); return; }
    const j = i + dir;
    if (j >= 0 && j < list.length) { setFocus(list[j]); }
    else if (dir > 0) { window.scrollBy({ top: Math.round(window.innerHeight * 0.6) }); }   // X charge la suite
  }

  // ---- OK : permalien du post au focus (même origine, format exact)
  function permalink(post) {
    const links = Array.from(post.querySelectorAll('a[href*="/status/"]'));
    const a = links.find((x) => x.querySelector("time")) || null;
    if (!a) { return null; }
    let u = null;
    try { u = new URL(a.getAttribute("href"), location.origin); } catch (e) { return null; }
    return u.origin === location.origin && PERMALINK_RE.test(u.pathname) ? u.pathname : null;
  }
  function open() {
    const ae = document.activeElement;
    const onPost = !ae || ae === document.body || ae === document.documentElement || ae === focused ||
                   (focused && focused.contains(ae) && !control(ae));
    if (!focused || !focused.isConnected || !onPost) { return false; }   // bouton/lien/champ au focus : natif
    const p = permalink(focused);
    if (!p) { return false; }
    location.assign(p);
    return true;
  }

  // ---- ← : partage du post au focus (texte et auteur VISIBLES, rien d'autre)
  function extract(post) {
    const t = post.querySelector('[data-testid="tweetText"]');
    const u = post.querySelector('[data-testid="User-Name"]');
    const text = clean(t ? t.innerText : "", TEXT_MAX);
    let author = "";
    if (u) {
      const raw = u.innerText || "";
      const name = (raw.split("\n").map((s) => s.trim()).filter(Boolean)[0] || "").replace(HANDLE_RE, "").replace(/[·•]/g, "").trim();
      const handle = (raw.match(HANDLE_RE) || [""])[0];
      author = clean([name, handle].filter(Boolean).join(" "), AUTHOR_MAX);
    }
    return { author: author, text: text };
  }
  function shareFocused() {
    if (!focused || !focused.isConnected) { toast(MSG_PICK); return; }
    if (sharing) { return; }
    const p = extract(focused);
    sharing = true;
    try {
      chrome.runtime.sendMessage({ type: "share", author: p.author, text: p.text }, (r) => {
        sharing = false;
        void chrome.runtime.lastError;
        toast(r && typeof r.message === "string" ? r.message : MSG_FAIL);
      });
    } catch (e) {
      sharing = false;
      toast(MSG_FAIL);
    }
  }

  // ---- Retour / Accueil
  function goHome() {
    try { chrome.runtime.sendMessage({ type: "home" }); } catch (e) { /* extension rechargée : rien */ }
  }
  function isRoot() {
    const p = location.pathname.replace(/\/+$/, "") || "/";
    return ROOT_PATHS.has(p) || p.indexOf("/i/flow/") === 0;
  }
  function back() {
    if (isRoot()) { goHome(); return; }
    const before = location.href;
    clearTimeout(backTimer);
    history.back();   // ferme la vue de post / la modale routée de X
    backTimer = setTimeout(() => { if (location.href === before) { goHome(); } }, BACK_FAILSAFE_MS);
  }

  function onKey(e) {
    if (!e.isTrusted) { return; }   // touche synthétique d'une page (script de X ou injecté) : jamais d'action OpenTV
    const a = keyAction(e);
    if (!a) { return; }
    const ae = document.activeElement;
    if (editable(ae)) {   // saisie (connexion, recherche) : touches natives ; Retour (hors Effacement) quitte le champ
      if (a === "back" && e.key !== "Backspace" && e.keyCode !== 8) {
        e.preventDefault();
        e.stopImmediatePropagation();
        ae.blur();
      }
      return;
    }
    const feed = feedMode();
    let used = false;
    if (a === "home") { if (!e.repeat) { goHome(); } used = true; }
    else if (a === "back") { if (!e.repeat) { back(); } used = true; }
    else if (feed && a === "up") { move(-1); used = true; }
    else if (feed && a === "down") { move(1); used = true; }
    else if (feed && a === "left") { if (!e.repeat) { shareFocused(); } used = true; }
    else if (feed && a === "ok") { used = !e.repeat && open(); }
    if (used) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }

  function refresh() {
    refreshTimer = 0;
    const on = feedMode();
    document.documentElement.classList.toggle("otvx-tv", on);
    ensureHelp(on);
    if (focused && !focused.isConnected) { focused = null; }   // post retiré par X : prochain ↑ ↓ repart de l'écran
  }
  const observer = new MutationObserver(() => {
    if (!refreshTimer) { refreshTimer = setTimeout(refresh, REFRESH_MS); }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("keydown", onKey, true);
  refresh();
})();
