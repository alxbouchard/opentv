/* OpenTV X TV Mode — script de contenu de x.com (cadre principal seulement).
 *
 * Fil X lisible à la télécommande, sans API X ni action automatique :
 *  ↑ ↓      post précédent / suivant (anneau jaune, post placé dans la zone libre au-dessus du bandeau d'aide) ;
 *           post plus haut que l'écran : ↑ ↓ le font d'abord défiler (lecture), puis passent au post voisin ; en fin
 *           de liste, défilement pour que X charge la suite (aucun clic) ;
 *  →        (0.7.14) mode « actions » : le focus NATIF du navigateur passe au VRAI bouton J'aime de X du post au focus
 *           (sinon Enregistrer) ; → suivant circule entre J'aime et Enregistrer (signet) de CE post seulement (jamais
 *           un post cité/imbriqué, jamais un bouton masqué) ; anneau blanc/bleu distinct, libellé français tiré de
 *           l'état affiché par X (« Aimé · OK retire », « Enregistré · OK retire ») ;
 *  OK       post : ouvre son permalien x.com ; bouton d'action au focus : RIEN ici — Chromium active lui-même le
 *           bouton au focus (geste humain réel). Jamais de click(), d'événement forgé, de raccourci X simulé ni
 *           d'API. Touche OK tenue (répétition) : bloquée (une activation par appui) ; bouton qui n'appartient plus au
 *           post choisi (DOM réutilisé par X) : bloqué, mode actions quitté ;
 *  ←        post : partage à « Hermit ou Muse » SEULEMENT son texte visible (≤ 2400) et son auteur visible (≤ 120) —
 *           jamais une URL, un lien, un média, un cookie ni du HTML ; geste humain uniquement. En mode actions : revient
 *           au post SANS partager (répétition et second ← immédiat absorbés pendant LEFT_GUARD_MS) ;
 *  Retour / Accueil : nav.js (script autonome, toute page x.com dès document_start, connexion comprise) — jamais
 *           ici, pour qu'aucune page ne dépende du fil.
 * Fenêtre modale VISIBLE de X au-dessus du fil (0.7.8) : elle gagne — nav.js la parcourt (mode « contrôles »), ce
 * script n'agit plus (jamais de fil caché déplacé ni de post ouvert derrière) et masque son bandeau d'aide.
 * Champ autofocusé par X dans le fil (0.7.8) : ↑ ↓ en sortent (champ d'une ligne, ou zone où Alex n'a rien tapé) ;
 * ← → OK y restent natifs. Toute touche traitée ici est signalée à la sonde canari de nav.js (jamais sa valeur).
 * Colonne principale présente : classe html.otvx-tv (CSS TV, x.css) + largeur ; aide/touches seulement avec article rendu.
 * Largeur
 * réelle (0.7.14 : chaque ancêtre effectif main -> colonne -> post marqué data-otvx-lane, aucun plafond de X ne
 * survit). Page de connexion ou sans fil : rien n'est stylé ni rempli (Alex se connecte lui-même). DOM dynamique de
 * X : MutationObserver regroupé (une passe au plus toutes les REFRESH_MS). Aucun état de compte mémorisé : l'état
 * J'aime/Enregistré affiché est toujours celui du DOM courant de X. Jamais de clic, de repartage, de réponse ni de
 * publication par ce script.
 * 0.7.15 : ce script ne marque AUCUNE rangée et ne juge aucun groupe : x.css masque seulement, un par un, des
 * contrôles secondaires identifiés par eux-mêmes (data-testid / lien de statistiques), jamais un conteneur, une rangée
 * ni un post cité entier (aperçu, poster, carte, image ou lecteur différé, texte libre : rendu natif intact). → / OK
 * visent les vrais J'aime / Enregistrer de CE post (rangée = [role=group] le plus interne, jamais un post cité).
 * Bandeau d'aide (0.7.15) : bande opaque pleine largeur en bas (x.css) — aucun texte de post ne passe à côté ou
 * au travers ; la zone de lecture reste au-dessus (bottomLimit), aucun défilement ajouté.
 */
(() => {
  "use strict";
  if (window.top !== window) { return; }
  const TEXT_MAX = 2400;
  const AUTHOR_MAX = 120;
  const POSTS_MAX = 200;
  const ACTIONS_MAX = 4;
  const LANE_DEPTH_MAX = 24;
  const REFRESH_MS = 250;
  const TOAST_MS = 6000;
  const LEFT_GUARD_MS = 700;   // ← juste après la sortie du mode actions : jamais un partage accidentel
  const TOP_GAP = 96;          // en-tête collant de X au-dessus du fil
  const HELP_GAP = 20;         // jamais un post/bouton au focus à moins de 20 px du bandeau d'aide
  const POST_SEL = 'article[data-testid="tweet"]';
  const COLUMN_SEL = '[data-testid="primaryColumn"]';
  // VRAIS boutons de X restaurés : J'aime / Je n'aime plus / Enregistrer (signet) / Retirer des signets — focus seulement
  const ACTION_TESTIDS = ["like", "unlike", "bookmark", "removeBookmark"];
  const ACTION_SEL = ACTION_TESTIDS.map((t) => '[data-testid="' + t + '"]').join(", ");
  const LANE_ATTR = "data-otvx-lane";
  const ACT_ATTR = "data-otvx-act";
  const ACTING_ATTR = "data-otvx-acting";
  const KEYS = {
    ArrowUp: "up", Up: "up", ArrowDown: "down", Down: "down", ArrowLeft: "left", Left: "left",
    ArrowRight: "right", Right: "right", Enter: "ok", Accept: "ok", Select: "ok"
  };
  const KEYCODES = { 38: "up", 40: "down", 37: "left", 39: "right", 13: "ok" };   // Retour/Accueil : nav.js
  const STRIP_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
  const HANDLE_RE = /@[A-Za-z0-9_]{1,15}/;
  const PERMALINK_RE = /^\/[A-Za-z0-9_]{1,15}\/status\/[0-9]{1,25}$/;
  const HELP_POST = [["↑ ↓", "Post précédent / suivant"], ["→", "J'aime / Enregistrer"], ["OK", "Ouvrir"],
                     ["←", "Partager à Hermit ou Muse"], ["Retour", "Revenir"]];
  const HELP_ACT = [["→", "Bouton suivant"], ["OK", "Activer"], ["←", "Revenir au post"], ["↑ ↓", "Post voisin"],
                    ["Retour", "Revenir"]];
  const MSG_PICK = "Choisis d'abord un post avec ↑ ↓.";
  const MSG_FAIL = "Partage impossible pour l'instant. Rien n'a été envoyé.";
  const MSG_NOACT = "Ce post n'a pas de bouton J'aime ou Enregistrer visible.";
  const MSG_STALE = "Le fil a changé : choisis de nouveau le post avec ↑ ↓, puis →.";

  let focused = null;
  let focusedKey = null;   // snapshot on selection, not on OK after a recycled cell has lost action focus
  let help = null;
  let helpMode = "";
  let toastEl = null;
  let toastTimer = 0;
  let refreshTimer = 0;
  let sharing = false;
  // mode actions : bouton au focus, permalien du post au moment du choix (DOM réutilisé par X = refus), rang du bouton
  let actEl = null;
  let actKey = null;
  let actIdx = 0;
  let leftGuardUntil = 0;
  // nav.js (document_start, MÊME monde isolé de l'extension) : fenêtre modale, champs quittables, sonde
  const NAV = window.__otvxNav || null;
  function dialogOpen() { return !!NAV && NAV.dialog(); }

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
  // post propriétaire d'un élément (le plus proche) ; post de premier niveau = jamais cité/imbriqué dans un autre
  function ownerPost(el) { return el && el.closest ? el.closest(POST_SEL) : null; }
  function topLevel(p) { return !p.parentElement || !p.parentElement.closest(POST_SEL); }
  function own(post, sel) { return Array.from(post.querySelectorAll(sel)).filter((x) => ownerPost(x) === post); }
  function allPosts() {
    const col = document.querySelector(COLUMN_SEL);
    if (!col) { return []; }
    return Array.from(col.querySelectorAll(POST_SEL)).filter(topLevel).slice(0, POSTS_MAX);
  }
  function posts() { return NAV ? allPosts().filter(NAV.renderedPost) : []; }
  function clean(s, cap) {
    return Array.from(String(s || "").replace(STRIP_RE, "")).slice(0, cap).join("").trim();
  }

  // ---- largeur réelle : ancêtres EFFECTIFS marqués (aucune supposition sur la profondeur ou les classes de X)
  function markLanes(on) {
    const want = new Map();
    const col = on ? document.querySelector(COLUMN_SEL) : null;
    if (col) {
      let d = 0;
      for (let e = col.parentElement; e && e !== document.body && e.tagName !== "MAIN" && d < LANE_DEPTH_MAX;
           e = e.parentElement, d++) { want.set(e, "shell"); }
      for (const p of allPosts()) {
        d = 0;
        for (let e = p.parentElement; e && e !== col && d < LANE_DEPTH_MAX; e = e.parentElement, d++) {
          if (!want.has(e)) { want.set(e, "post"); }
        }
      }
    }
    for (const e of Array.from(document.querySelectorAll("[" + LANE_ATTR + "]"))) {
      if (!want.has(e)) { e.removeAttribute(LANE_ATTR); }   // nœud réutilisé par X hors du chemin : rendu tel quel
    }
    for (const [e, v] of want) { if (e.getAttribute(LANE_ATTR) !== v) { e.setAttribute(LANE_ATTR, v); } }
  }

  // ---- rangée d'actions : [role=group] LE PLUS INTERNE de ce post qui porte un de SES boutons (jamais ceux d'un
  // post cité). Lecture seule : aucun attribut posé, rien masqué ici (x.css ne vise que des contrôles identifiés)
  function rowOf(post) {
    const gs = own(post, '[role="group"]').filter((g) =>
      Array.from(g.querySelectorAll(ACTION_SEL)).some((b) => ownerPost(b) === post));
    return gs.find((g) => !gs.some((h) => h !== g && g.contains(h))) || null;
  }

  // ---- bandeau d'aide et message (textContent seulement)
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function ensureHelp(on) {
    const mode = on ? (actEl ? "act" : "post") : "";
    if (on && (!help || !help.isConnected)) {
      help = el("div", "otvx-help");
      help.setAttribute("aria-hidden", "true");
      document.documentElement.appendChild(help);
      helpMode = "";
    } else if (!on && help) {
      help.remove();
      help = null;
    }
    if (!help || helpMode === mode) { return; }
    helpMode = mode;
    help.textContent = "";
    (mode === "act" ? HELP_ACT : HELP_POST).forEach((h, i) => {
      if (i) { help.appendChild(document.createTextNode(" · ")); }
      help.appendChild(el("b", null, h[0]));
      help.appendChild(document.createTextNode(" " + h[1]));
    });
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

  // ---- placement : zone libre = sous l'en-tête de X, au-dessus du bandeau d'aide (jamais un focus caché dessous)
  function bottomLimit() {
    const h = help && help.isConnected ? help.getBoundingClientRect() : null;
    return h && h.height > 0 ? h.top - HELP_GAP : window.innerHeight - HELP_GAP;
  }
  function place(post) {
    const r = post.getBoundingClientRect();
    const room = bottomLimit() - TOP_GAP;
    const target = r.height <= room ? TOP_GAP + (room - r.height) / 2 : TOP_GAP;   // trop haut : son début visible
    window.scrollBy(0, Math.round(r.top - target));
  }
  function reveal(node) {   // bouton d'un post long hors champ : ramené dans la zone libre
    const r = node.getBoundingClientRect();
    const lim = bottomLimit();
    if (r.bottom > lim) { window.scrollBy(0, Math.ceil(r.bottom - lim)); }
    else if (r.top < TOP_GAP) { window.scrollBy(0, Math.floor(r.top - TOP_GAP)); }
  }

  // ---- focus des posts
  function setFocus(post) {
    exitActions();
    if (focused && focused !== post) { focused.classList.remove("otvx-focus"); }
    focused = post;
    focusedKey = post ? permalink(post) : null;
    if (post) {
      post.classList.add("otvx-focus");
      place(post);
    }
  }
  function firstInView(list) {
    return list.find((p) => p.getBoundingClientRect().bottom > 80) || list[0] || null;
  }
  // post long : la touche fait d'abord défiler sa partie cachée (au plus 60 % de la zone libre), puis change de post
  function scrollInside(post, dir) {
    const r = post.getBoundingClientRect();
    const lim = bottomLimit();
    const step = Math.round((lim - TOP_GAP) * 0.6);
    if (dir > 0 && r.bottom > lim + 4) { window.scrollBy(0, Math.min(step, Math.ceil(r.bottom - lim))); return true; }
    if (dir < 0 && r.top < TOP_GAP - 4) { window.scrollBy(0, -Math.min(step, Math.ceil(TOP_GAP - r.top))); return true; }
    return false;
  }
  function move(dir) {
    const list = posts();
    if (!list.length) { return; }
    const i = focused && focused.isConnected ? list.indexOf(focused) : -1;
    if (i < 0) { setFocus(firstInView(list)); return; }
    if (scrollInside(focused, dir)) { return; }
    const j = i + dir;
    if (j >= 0 && j < list.length) { setFocus(list[j]); }
    else if (dir > 0) { window.scrollBy({ top: Math.round(window.innerHeight * 0.6) }); }   // X charge la suite
  }

  // ---- mode actions (→) : focus NATIF sur les vrais boutons J'aime / Enregistrer du post au focus
  function usable(b) {
    if (b.disabled || b.getAttribute("aria-disabled") === "true" || b.closest("[hidden], [inert], [aria-hidden='true']")) {
      return false;
    }
    if (!shown(b)) { return false; }
    return typeof b.checkVisibility !== "function" || b.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  }
  function actionBar(post) {
    return rowOf(post);   // même groupe mixte : seuls les boutons de CE post sont retenus (actions)
  }
  function actions(post) {
    const bar = post && post.isConnected ? actionBar(post) : null;
    if (!bar) { return []; }
    return Array.from(bar.querySelectorAll(ACTION_SEL)).filter((b) => ownerPost(b) === post && usable(b))
      .slice(0, ACTIONS_MAX);
  }
  function actionButton(node) {   // un bouton d'action du fil (quel que soit son post)
    return !!node && node.nodeType === 1 && node.matches(ACTION_SEL) && !!ownerPost(node);
  }
  function clearActMarks(keep) {   // anneau d'action : jamais sur un autre nœud (copie ou remplaçant posé par X)
    for (const n of Array.from(document.querySelectorAll("[" + ACT_ATTR + "]"))) {
      if (n !== keep) { n.removeAttribute(ACT_ATTR); }
    }
  }
  function focusAction(btn, idx) {
    clearActMarks(btn);
    actEl = btn;
    actIdx = idx;
    btn.setAttribute(ACT_ATTR, "");
    focused.setAttribute(ACTING_ATTR, "");
    try { btn.focus({ preventScroll: true }); } catch (err) { /* retiré */ }
    reveal(btn);
    ensureHelp(true);
  }
  function exitActions() {
    if (actEl && document.activeElement === actEl) { actEl.blur(); }
    if (actEl) { clearActMarks(null); }
    if (focused) { focused.removeAttribute(ACTING_ATTR); }
    const was = !!actEl;
    actEl = null;
    actKey = null;
    actIdx = 0;
    if (was) { ensureHelp(!!help); }
  }
  function leaveButton(node) {   // bouton d'action au focus (choisi ou non) : focus rendu au post, rien d'activé
    if (node !== actEl) { try { node.blur(); } catch (err) { /* retiré */ } }
    exitActions();
  }
  function quitActions() {   // sortie SANS cible vérifiable : ← immédiat absorbé (jamais un partage accidentel)
    exitActions();
    leftGuardUntil = performance.now() + LEFT_GUARD_MS;
  }
  // X a remplacé le bouton choisi (J'aime -> Aimé) : focus remis au MÊME rang du MÊME post (permalien inchangé) ;
  // focus seulement, jamais une activation ; faux si la cible n'est plus vérifiable
  function restoreAct() {
    if (!actEl || actEl.isConnected || !focused || !focused.isConnected || actKey === null ||
        permalink(focused) !== actKey) { return false; }
    const list = actions(focused);
    if (!list[actIdx]) { return false; }
    focusAction(list[actIdx], actIdx);
    return true;
  }
  function enterActions() {
    if (!focused || !focused.isConnected) {
      const list = posts();
      if (!list.length) { toast(MSG_PICK); return; }
      setFocus(firstInView(list));
    }
    const key = permalink(focused);
    const list = actions(focused);
    if (!key || key !== focusedKey || !list.length) { toast(MSG_NOACT); return; }
    actKey = key;
    focusAction(list[0], 0);
  }
  function cycleAction() {
    if (!focused || !focused.isConnected || permalink(focused) !== actKey) { exitActions(); toast(MSG_STALE); return; }
    const list = actions(focused);
    if (!list.length) { exitActions(); toast(MSG_NOACT); return; }
    const i = list.indexOf(actEl);
    const j = i < 0 ? 0 : (i + 1) % list.length;
    focusAction(list[j], j);
  }
  // OK sur un bouton d'action : laissé à Chromium SEULEMENT si c'est le bouton choisi, au focus réel, du post choisi,
  // toujours du même permalien et toujours dans sa rangée visible
  function actionValid(node) {
    return !!actEl && node === actEl && actEl.isConnected && !!focused && focused.isConnected &&
           !!NAV && !!focused.closest(COLUMN_SEL) && NAV.renderedPost(focused) &&
           ownerPost(actEl) === focused && actKey !== null && permalink(focused) === actKey &&
           actions(focused).indexOf(actEl) >= 0;
  }

  // ---- OK : permalien du post au focus (même origine, format exact)
  function permalink(post) {
    const a = own(post, 'a[href*="/status/"]').find((x) => x.querySelector("time")) || null;
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
    if (!p || p !== focusedKey || !NAV || !NAV.renderedPost(focused)) { return true; }   // consume stale OK
    location.assign(p);
    return true;
  }

  // ---- ← : partage du post au focus (texte et auteur VISIBLES du post lui-même, jamais du post cité)
  function extract(post) {
    const t = own(post, '[data-testid="tweetText"]')[0] || null;
    const u = own(post, '[data-testid="User-Name"]')[0] || null;
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

  function onKey(e) {
    if (!e.isTrusted) { return; }   // touche synthétique d'une page (script de X ou injecté) : jamais d'action OpenTV
    const a = keyAction(e);
    if (!a) { return; }
    const ae = document.activeElement;
    const acting = actionButton(ae) || (!!actEl && ae === actEl);
    // Chromium retains native focus through recycling, even at opacity:0. Validate OK before
    // yielding to recovery (including a focus restored by the site AFTER refresh). Controls
    // inside the current modal keep their own navigation; the feed behind it cannot activate.
    if (a === "ok" && acting) {
      if (e.repeat || !actionValid(ae) || dialogOpen()) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (NAV) { NAV.markHandled(e); }
        if (!e.repeat) { leaveButton(ae); toast(MSG_STALE); }
      }
      return;   // verified native button: no substitute action
    }
    const feed = feedMode();
    if (!feed || dialogOpen() || (NAV && !NAV.feedReady())) { return; }   // vide : contrôles natifs, nav.js
    if (editable(ae)) {   // saisie : touches natives, sauf ↑ ↓ qui quittent un champ autofocusé / d'une ligne
      if (!((a === "up" || a === "down") && NAV && NAV.leaveField(ae))) { return; }
      ae.blur();
    }
    // Intention « actions » sans cible vérifiable (X a remplacé le bouton juste après OK : focus retombé sur la page ou
    // le post AVANT le prochain refresh) : ← sort SANS partager, OK n'ouvre ni n'active rien (focus remis au même rang
    // s'il est vérifiable), → idem, ↑ ↓ normaux. Contrôle DISTINCT au focus : mode quitté, sa commande native reste.
    if (actEl && ae !== actEl && !actionButton(ae)) {
      const onPage = !ae || ae === document.body || ae === document.documentElement || (!!focused && focused.contains(ae));
      if (onPage && a !== "up" && a !== "down") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (NAV) { NAV.markHandled(e); }
        if (a === "left" || e.repeat || !restoreAct()) { quitActions(); }
        return;
      }
      quitActions();
    }
    let used = false;
    if (a === "ok") {
      used = !e.repeat && open();
    } else if (a === "right") {
      used = true;
      if (!e.repeat) { if (acting && actEl === ae) { cycleAction(); } else { enterActions(); } }
    } else if (a === "left") {
      used = true;
      if (acting) {   // retour au post, JAMAIS un partage (répétitions et ← immédiat absorbés ensuite)
        leaveButton(ae);
        leftGuardUntil = performance.now() + LEFT_GUARD_MS;
      } else if (!e.repeat && performance.now() >= leftGuardUntil) { shareFocused(); }
    } else {   // ↑ ↓ : navigation normale entre posts (le mode actions est quitté)
      used = true;
      if (acting) { leaveButton(ae); }
      move(a === "up" ? -1 : 1);
    }
    if (used) {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (NAV) { NAV.markHandled(e); }
    }
  }

  function refresh() {
    refreshTimer = 0;
    const on = feedMode();
    document.documentElement.classList.toggle("otvx-tv", on);
    markLanes(on);
    if (focused && !focused.isConnected) { focused = null; focusedKey = null; }
    if (actEl && document.activeElement !== actEl) {
      // X a remplacé le bouton (J'aime -> Aimé) dans le MÊME post : focus remis au même rang ; sinon mode quitté,
      // ← immédiat absorbé (garde de sortie)
      const ae = document.activeElement;
      const keep = on && !dialogOpen() && (!ae || ae === document.body || ae === document.documentElement);
      if (!(keep && restoreAct())) { quitActions(); }
    } else if (actEl && (!focused || permalink(focused) !== actKey)) {
      quitActions();   // DOM réutilisé par X pour un autre post : jamais d'action sur un post non choisi
      toast(MSG_STALE);
    }
    ensureHelp(on && !dialogOpen() && (!NAV || NAV.feedReady()));
  }
  const observer = new MutationObserver((records) => {
    if (records.length && records.every((r) => r.target.nodeType === 1 &&
        r.target.closest('.otvx-empty, .otvx-login-help, .otvx-help, .otvx-toast'))) { return; }
    if (!refreshTimer) { refreshTimer = setTimeout(refresh, REFRESH_MS); }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
    attributeFilter: ["hidden", "inert", "aria-hidden", "aria-busy", "data-testid", "role", "style", "class"] });
  window.addEventListener("keydown", onKey, true);
  // A site keydown handler can recycle the cell AFTER our capture and BEFORE Chromium's
  // native keyboard click. Veto that default action too; never dispatch or replace it.
  window.addEventListener("click", (e) => {
    if (!e.isTrusted || e.detail !== 0 || !(actionButton(e.target) || e.target === actEl)) { return; }
    if (e.target !== document.activeElement || !actionValid(e.target) || dialogOpen()) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);
  refresh();
})();
