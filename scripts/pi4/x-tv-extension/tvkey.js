/* OpenTV X TV Mode — script de contenu de la page /tv (boucle locale du kiosque).
 * Relit la clé TV de CE démarrage du service (meta « opentv-key », elle change à chaque redémarrage) et la confie au
 * service de l'extension, qui la garde dans son stockage de SESSION (mémoire du navigateur, jamais sur disque). Rien
 * d'autre n'est lu ni modifié sur la page OpenTV. */
(() => {
  "use strict";
  if (location.protocol !== "http:" || location.hostname !== "127.0.0.1" || location.pathname !== "/tv") { return; }
  const meta = document.querySelector('meta[name="opentv-key"]');
  const key = meta && typeof meta.content === "string" ? meta.content : "";
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(key)) { return; }
  try {
    chrome.runtime.sendMessage({ type: "tvkey", key: key }, () => void chrome.runtime.lastError);
  } catch (e) {
    /* extension rechargée pendant la page : la prochaine visite de /tv recapture la clé */
  }
})();
