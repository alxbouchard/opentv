/* OpenTV X TV Mode — service de l'extension locale du kiosque (Manifest V3).
 *
 * Trois messages, chacun vérifié (expéditeur = CETTE extension, cadre principal, page attendue) :
 *  - tvkey : envoyé par tvkey.js depuis la page /tv de la boucle locale ; clé TV + port gardés dans
 *    chrome.storage.session (mémoire, jamais sur disque, jamais lisible par les pages ni les scripts de contenu) ;
 *  - share : envoyé par x.js depuis x.com quand Alex appuie sur ← ; auteur et texte VISIBLES du post au focus, déjà
 *    bornés, re-bornés ici ; un seul appel réseau : POST /api/tv/x/context sur 127.0.0.1 avec la clé TV ;
 *  - home : envoyé par nav.js (toute page x.com) : retour explicite à la page OpenTV /tv (Accueil, ou Retour).
 * Aucune API X, aucun cookie, aucune lecture d'onglet, aucun autre hôte contacté, aucun envoi vers un agent.
 */
"use strict";

const SHARE_PATH = "/api/tv/x/context";
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
      }).catch(() => undefined);
    }
    return false;
  }
  if (msg.type === "share" && fromX(sender)) {
    share(msg).then(respond, () => respond({ ok: false, message: MSG.fail }));
    return true;
  }
  if (msg.type === "home" && fromX(sender) && sender.tab && Number.isInteger(sender.tab.id)) {
    goTv(sender.tab.id).catch(() => undefined);
    return false;
  }
  return false;
});
