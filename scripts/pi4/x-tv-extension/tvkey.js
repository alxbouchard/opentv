/* OpenTV X TV Mode — script de contenu de la page /tv (boucle locale du kiosque).
 * Relit la clé TV de CE démarrage du service (meta « opentv-key », elle change à chaque redémarrage) et la confie au
 * service de l'extension, qui la garde dans son stockage de SESSION (mémoire du navigateur, jamais sur disque). C'est la
 * SEULE voie d'acquisition de la clé : après un redémarrage du service pendant que X est ouvert, la sonde se tait
 * jusqu'au prochain chargement de /tv (par conception). Sonde canari (0.7.8) : battement « tvbeat » toutes les BEAT_MS
 * (< TTL 120 s du service) avec deux booléens seulement, document au focus et page visible. Rien d'autre n'est lu ni
 * modifié sur la page OpenTV. */
(() => {
  "use strict";
  if (location.protocol !== "http:" || location.hostname !== "127.0.0.1" || location.pathname !== "/tv") { return; }
  const meta = document.querySelector('meta[name="opentv-key"]');
  const key = meta && typeof meta.content === "string" ? meta.content : "";
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(key)) { return; }
  const BEAT_MS = 30000;
  function send(msg) {
    msg.doc_focus = document.hasFocus();
    msg.visible = document.visibilityState === "visible";
    try {
      chrome.runtime.sendMessage(msg, () => void chrome.runtime.lastError);
    } catch (e) {
      /* extension rechargée pendant la page : la prochaine visite de /tv recapture la clé */
    }
  }
  // saisie de l'identifiant X par téléphone (0.7.10) : drapeau du service, « 1 » seulement si activée
  const entry = document.querySelector('meta[name="opentv-x-entry"]');
  send({ type: "tvkey", key: key, entry: !!entry && entry.content === "1" });
  setInterval(() => send({ type: "tvbeat" }), BEAT_MS);
})();
