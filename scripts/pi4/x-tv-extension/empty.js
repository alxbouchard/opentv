/* X TV 0.7.16 : récupération humaine explicite, uniquement /home sans publication rendue.
 * Module sans réseau, stockage, télémétrie ni valeur de champ ; nav.js fournit les prédicats existants.
 * Le bouton est natif : jamais de click(), événement forgé ni recharge automatique. */
(() => {
  "use strict";
  if (window.top !== window) { return; }
  Object.defineProperty(window, "__otvxEmpty", { writable: false, configurable: false, value: (nav) => {
    let panel = null;
    let button = null;
    let reloading = false;
    function empty() {
      return location.pathname === "/home" && !!document.querySelector(nav.column) && !nav.ready() && !nav.dialog();
    }
    function place() {
      // Mesure avant affichage : contrôles, spinner et états d'erreur natifs jamais recouverts.
      // Si aucun espace libre : panneau caché, contrôles natifs/Retour toujours disponibles.
      panel.hidden = false;
      const r = panel.getBoundingClientRect();
      const blocked = Array.from(document.body.querySelectorAll(nav.controls + ', [role="alert"], [role="status"], [role="progressbar"]'))
        .filter((e) => !panel.contains(e) && nav.visible(e)).map((e) => e.getBoundingClientRect())
        .filter((b) => b.left < r.right + 16 && b.right > r.left - 16).sort((a, b) => a.top - b.top);
      let top = 100;
      let chosen = null;
      for (const b of blocked.concat([{ top: innerHeight - 100, bottom: innerHeight - 100 }])) {
        const end = Math.min(b.top - 16, innerHeight - 100);
        if (end - top >= r.height) { chosen = end - r.height; }
        top = Math.max(top, b.bottom + 16);
      }
      if (chosen === null || r.width <= 0) { panel.hidden = true; return; }
      const value = Math.round(chosen) + "px";
      if (panel.style.top !== value) { panel.style.setProperty("top", value, "important"); }
      if (panel.style.bottom !== "auto") { panel.style.setProperty("bottom", "auto", "important"); }
    }
    function refresh() {
      if (!empty()) {
        if (panel) { panel.remove(); panel = null; button = null; }
        return;
      }
      if (panel && panel.isConnected) { place(); return; }
      panel = document.createElement("div");
      panel.className = "otvx-empty";
      const text = document.createElement("span");
      text.textContent = "Aucune publication affichée pour l'instant. Le chargement peut encore aboutir. ";
      button = document.createElement("button");
      button.type = "button";
      button.textContent = "Recharger X";
      button.addEventListener("click", (e) => {
        // Relit l'état : article/modale apparu depuis refresh ⇒ aucune recharge. Profil inchangé.
        if (!e.isTrusted || reloading || document.activeElement !== button || !empty()) { return; }
        reloading = true;
        location.reload();
      });
      panel.appendChild(text);
      panel.appendChild(button);
      document.body.appendChild(panel);
      place();
    }
    return Object.freeze({ empty: empty, refresh: refresh, button: (el) => !!button && el === button,
      // Reserve the last focus slot: ↑ from the page must match the hint, even after 400+ site controls.
      augment: (list, max) => button && empty() && nav.visible(button) ?
        list.filter((el) => el !== button).slice(0, max - 1).concat(button) : list,
      hint: () => panel && !panel.hidden ?
        "↑ ↓ choisir (↑ depuis la page : Recharger X) · OK valider · Retour : OpenTV" :
        "↑ ↓ contrôles de X · OK valider · Retour : OpenTV" });
  } });
})();
