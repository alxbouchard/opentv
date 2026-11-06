/* OpenTV X TV Mode — service de l'extension locale du kiosque (Manifest V3).
 *
 * Trois messages, chacun vérifié (expéditeur = CETTE extension, cadre principal, page attendue) :
 *  - tvkey : envoyé par tvkey.js depuis la page /tv de la boucle locale ; clé TV + port gardés dans
 *    chrome.storage.session (mémoire, jamais sur disque, jamais lisible par les pages ni les scripts de contenu) ;
 *  - share : envoyé par x.js depuis x.com quand Alex appuie sur ← ; auteur et texte VISIBLES du post au focus, déjà
 *    bornés, re-bornés ici ; un seul appel réseau : POST /api/tv/x/context sur 127.0.0.1 avec la clé TV ;
 *  - home : envoyé par nav.js (toute page x.com) : retour explicite à la page OpenTV /tv (Accueil, ou Retour) ;
 *  - probe (canari 0.7.8) : envoyé par nav.js (x.com) ; instantané reconstruit sur une liste FERMÉE (mode, compteurs
 *    move/ok/other/repeat/synthetic, doc_focus/visible, booléens de la dernière touche réelle), page source DÉDUITE de
 *    l'expéditeur (jamais du message) ; + instantané « tv » quand tvkey.js remet la clé, puis à chaque « tvbeat »
 *    (tvkey.js, toutes les 30 s < TTL 120 s du service). Au plus un envoi en vol (coalescé, le dernier instantané
 *    gagne), ≤ PROBE_MAX par PROBE_WINDOW_MS ; POST PROBE_PATH sur 127.0.0.1 avec la clé TV ; échec ignoré (jamais la
 *    clé effacée : le partage garde son message « OpenTV a redémarré »). Service OpenTV redémarré pendant que X est
 *    ouvert : clé périmée, sonde refusée (403) jusqu'au prochain /tv — par conception, la clé n'est reprise QUE sur
 *    /tv (tvkey.js), gardée en stockage de SESSION seulement, jamais persistée ni obtenue autrement.
 * Une page x.com ne peut jamais forger ces messages : aucun externally_connectable, aucun écouteur postMessage ;
 * seuls les scripts de contenu de CETTE extension (monde isolé) atteignent ce service.
 * Aucune API X, aucun cookie, aucune lecture d'onglet, aucun autre hôte contacté, aucun envoi vers un agent.
 */
"use strict";

const SHARE_PATH = "/api/tv/x/context";
const PROBE_PATH = "/api/tv/x/probe";
const PROBE_TIMEOUT_MS = 2000;
const PROBE_WINDOW_MS = 10000;
const PROBE_MAX = 30;
const COUNT_MAX = 1000000;
const PROBE_MODES = new Set(["feed", "controls", "dialog", "loading"]);
const PROBE_EVENTS = new Set(["load", "mode", "key", "beat"]);
const PROBE_CLASSES = new Set(["move", "ok", "other"]);
const STARTED = Date.now();
const TEXT_MAX = 2400;
const AUTHOR_MAX = 120;
const DEFAULT_PORT = 8080;
const FETCH_TIMEOUT_MS = 5000;
const KEY_RE = /^[A-Za-z0-9_-]{32,128}$/;
const X_HOSTS = new Set(["x.com", "twitter.com"]);
// contrôles, bidi et largeur nulle : jamais transmis (le service OpenTV les retire aussi)
const STRIP_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
const MSG = {
  nokey: "Reviens une fois à l'accueil OpenTV (touche Accueil), puis réessaie.",
  stale: "OpenTV a redémarré : reviens à l'accueil OpenTV (touche Accueil), puis réessaie.",
  empty: "Ce post n'a pas de texte à partager.",
  fail: "Partage impossible pour l'instant. Rien n'a été envoyé."
};

function senderUrl(sender) {
  try { return new URL(sender && sender.url); } catch (e) { return null; }
}

function fromTv(sender) {
  const u = senderUrl(sender);
  return !!u && sender.frameId === 0 && u.protocol === "http:" && u.hostname === "127.0.0.1" && u.pathname === "/tv";
}

function fromX(sender) {
  const u = senderUrl(sender);
  return !!u && sender.frameId === 0 && u.protocol === "https:" && X_HOSTS.has(u.hostname);
}

function portOf(u) {
  const p = Number(u.port);
  return Number.isInteger(p) && p >= 1024 && p <= 65535 ? p : null;
}

function clean(s, cap) {
  if (typeof s !== "string") { return ""; }
  return Array.from(s.replace(STRIP_RE, "")).slice(0, cap).join("").trim();
}

async function tvSession() {
  const s = await chrome.storage.session.get(["tvkey", "tvport"]);
  const port = Number.isInteger(s.tvport) && s.tvport >= 1024 && s.tvport <= 65535 ? s.tvport : DEFAULT_PORT;
  return { key: typeof s.tvkey === "string" && KEY_RE.test(s.tvkey) ? s.tvkey : null, port: port };
}

async function share(msg) {
  const text = clean(msg.text, TEXT_MAX);
  const author = clean(typeof msg.author === "string" ? msg.author.replace(/\s+/g, " ") : "", AUTHOR_MAX);
  if (!text) { return { ok: false, message: MSG.empty }; }
  const tv = await tvSession();
  if (!tv.key) { return { ok: false, message: MSG.nokey }; }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
  let r = null;
  let j = null;
  try {
    r = await fetch(`http://127.0.0.1:${tv.port}${SHARE_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OpenTV-TV": tv.key },
      body: JSON.stringify({ author: author, text: text }),
      credentials: "omit",
      referrerPolicy: "no-referrer",
      cache: "no-store",
      redirect: "error",
      signal: ctl.signal
    });
    try { j = await r.json(); } catch (e) { j = null; }
  } catch (e) {
    return { ok: false, message: MSG.fail };
  } finally {
    clearTimeout(timer);
  }
  if (r.ok && j && j.ok === true && typeof j.message === "string") { return { ok: true, message: j.message.slice(0, 200) }; }
  if (r.status === 403 && j && j.error === "tv_key") {   // service redémarré : clé périmée, jamais réessayée
    await chrome.storage.session.remove(["tvkey"]);
    return { ok: false, message: MSG.stale };
  }
  if (j && j.error === "empty_text") { return { ok: false, message: MSG.empty }; }
  return { ok: false, message: MSG.fail };
}

// ---------------------------------------------------------------- sonde canari : liste fermée, débit borné
function count(v) { return Number.isInteger(v) && v >= 0 ? Math.min(v, COUNT_MAX) : 0; }
function probeBody(page, msg) {
  const mode = page === "tv" ? "tv" : (PROBE_MODES.has(msg.mode) ? msg.mode : null);
  const event = PROBE_EVENTS.has(msg.event) ? msg.event : null;
  if (!mode || !event) { return null; }
  const l = msg.last;
  const last = l && typeof l === "object" && PROBE_CLASSES.has(l.cls) ?
    { cls: l.cls, trusted: l.trusted === true, editable: l.editable === true, doc_focus: l.doc_focus === true,
      handled: l.handled === true } : null;
  return { v: 1, page: page, mode: mode, event: event, seq: count(msg.seq), move: count(msg.move), ok: count(msg.ok),
           other: count(msg.other), repeat: count(msg.repeat), synthetic: count(msg.synthetic),
           doc_focus: msg.doc_focus === true, visible: msg.visible === true, last: last,
           ext: chrome.runtime.getManifest().version, sw_s: count(Math.floor((Date.now() - STARTED) / 1000)) };
}
const probeQ = { pending: null, busy: false, sent: [] };
function queueProbe(body) {
  if (!body) { return; }
  probeQ.pending = body;   // coalescé : seul le dernier instantané attend (compteurs cumulatifs)
  if (!probeQ.busy) { pumpProbe().catch(() => undefined); }
}
async function pumpProbe() {
  probeQ.busy = true;
  try {
    while (probeQ.pending) {
      const now = Date.now();
      probeQ.sent = probeQ.sent.filter((t) => now - t < PROBE_WINDOW_MS);
      if (probeQ.sent.length >= PROBE_MAX) { probeQ.pending = null; break; }   // débit dépassé : abandonné
      const body = probeQ.pending;
      probeQ.pending = null;
      probeQ.sent.push(now);
      const tv = await tvSession();
      if (!tv.key) { continue; }
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
      try {
        await fetch(`http://127.0.0.1:${tv.port}${PROBE_PATH}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-OpenTV-TV": tv.key },
          body: JSON.stringify(body),
          credentials: "omit",
          referrerPolicy: "no-referrer",
          cache: "no-store",
          redirect: "error",
          signal: ctl.signal
        });
      } catch (e) {
        /* service absent ou clé périmée : la sonde se tait, rien d'autre ne change */
      } finally {
        clearTimeout(timer);
      }
    }
  } finally {
    probeQ.busy = false;
  }
}

async function goTv(tabId) {
  const tv = await tvSession();
  await chrome.tabs.update(tabId, { url: `http://127.0.0.1:${tv.port}/tv` });
}

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  if (!sender || sender.id !== chrome.runtime.id || !msg || typeof msg !== "object") { return false; }
  if (msg.type === "tvkey" && fromTv(sender)) {
    const port = portOf(senderUrl(sender));
    if (typeof msg.key === "string" && KEY_RE.test(msg.key) && port) {
      // port épinglé pour la session du navigateur : le kiosque démarre toujours sur le vrai /tv ; une autre page
      // « /tv » d'un autre port local ne peut jamais détourner la clé ni le retour à OpenTV
      chrome.storage.session.get(["tvport"]).then((s) => {
        if (s.tvport === undefined || s.tvport === port) { return chrome.storage.session.set({ tvkey: msg.key, tvport: port }); }
        return undefined;
      }).then(() => queueProbe(probeBody("tv", { event: "load", doc_focus: msg.doc_focus, visible: msg.visible })))
        .catch(() => undefined);
    }
    return false;
  }
  if (msg.type === "tvbeat" && fromTv(sender)) {   // battement /tv : seulement avec la clé déjà capturée (pumpProbe)
    queueProbe(probeBody("tv", { event: "beat", doc_focus: msg.doc_focus, visible: msg.visible }));
    return false;
  }
  if (msg.type === "share" && fromX(sender)) {
    share(msg).then(respond, () => respond({ ok: false, message: MSG.fail }));
    return true;
  }
  if (msg.type === "probe" && fromX(sender)) {
    queueProbe(probeBody("x", msg));
    return false;
  }
  if (msg.type === "home" && fromX(sender) && sender.tab && Number.isInteger(sender.tab.id)) {
    goTv(sender.tab.id).catch(() => undefined);
    return false;
  }
  return false;
});
