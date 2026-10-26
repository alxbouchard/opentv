/* Assistant de configuration OpenTV (téléphone) : amélioration progressive seulement. Sans ce fichier, la page
   reste entièrement utilisable. Aucune requête réseau, aucun stockage, aucun journal : le mot de passe ne quitte
   jamais le champ du formulaire. ES5 (vieux téléphones). */
(function () {
  "use strict";
  var form = document.getElementById("finish");
  var psk = document.getElementById("psk");
  var iosTextMask = false;

  /* iOS peut remplacer le clavier par son gestionnaire de mots de passe pour un
     champ password servi par la boîte locale. Sur iPhone seulement, demander un
     vrai clavier texte et conserver le masquage visuel WebKit. Sans JS ou sans
     support de cette propriété, le champ reste un password normal. */
  if (psk && /iPhone|iPad|iPod/.test(navigator.userAgent || "") && window.CSS &&
      typeof window.CSS.supports === "function" && window.CSS.supports("-webkit-text-security", "disc")) {
    psk.type = "text";
    if (psk.className.indexOf("ios-text-mask") < 0) {
      psk.className = (psk.className ? psk.className + " " : "") + "ios-text-mask";
    }
    iosTextMask = true;
  }

  if (psk && psk.parentNode) {   // « Afficher » : moins de fautes de frappe dans le mot de passe Wi-Fi
    var b = document.createElement("button");
    b.type = "button";
    b.className = "reveal";
    b.textContent = "Afficher";
    b.setAttribute("aria-controls", "psk");
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", function () {
      var show = iosTextMask ? psk.className.indexOf("ios-text-mask") >= 0 : psk.type === "password";
      if (iosTextMask) {
        psk.className = show ? psk.className.replace(/(^|\s)ios-text-mask(?=\s|$)/, "").replace(/^\s+|\s+$/g, "")
                             : (psk.className ? psk.className + " " : "") + "ios-text-mask";
      } else { psk.type = show ? "text" : "password"; }
      b.textContent = show ? "Masquer" : "Afficher";
      b.setAttribute("aria-pressed", show ? "true" : "false");
      psk.focus();
    });
    psk.parentNode.appendChild(b);
  }

  if (!form) { return; }
  var submit = form.querySelector("button[type=submit]");
  var label = submit ? submit.textContent : "";

  form.addEventListener("invalid", function (ev) {   // champ invalide dans « Plus d'options » : l'ouvrir
    var n = ev.target;
    while (n && n !== form) {
      if (n.tagName === "DETAILS") { n.open = true; }
      n = n.parentNode;
    }
  }, true);

  form.addEventListener("submit", function (ev) {   // un seul envoi (chaque envoi refusé use un essai du QR)
    if (form.getAttribute("data-sent") === "1") { ev.preventDefault(); return; }
    form.setAttribute("data-sent", "1");
    if (psk) { psk.type = "password"; }   // jamais soumis ni mémorisé comme texte par le navigateur
    if (submit) { submit.disabled = true; submit.textContent = "Envoi…"; }
  });

  window.addEventListener("pageshow", function (ev) {   // retour arrière (cache de page) : bouton de nouveau actif
    if (ev.persisted) {
      form.removeAttribute("data-sent");
      if (submit) { submit.disabled = false; submit.textContent = label; }
    }
  });
}());
