/* Télécommande OpenTV (téléphone) : amélioration progressive. Sans ce script, chaque touche est un formulaire POST
   ordinaire (la page se recharge). Avec : envoi sans rechargement, même POST, même jeton CSRF, même origine ; aucune
   autre requête, aucun stockage. ES5. */
(function () {
  "use strict";
  var pad = document.getElementById("pad"), status = document.getElementById("status");
  document.documentElement.setAttribute("data-remote-js", "1");   // témoin (tests) : le script s'est exécuté
  if (!pad || typeof window.fetch !== "function") { return; }
  var csrf = pad.querySelector("input[name=c]").value, busy = 0, clearTimer = null;
  function say(t) {
    status.textContent = t;
    clearTimeout(clearTimer);
    if (t) { clearTimer = setTimeout(function () { status.textContent = ""; }, 3000); }
  }
  function send(btn) {
    if (busy >= 4) { return; }   // jamais de rafale côté téléphone non plus
    busy += 1;
    btn.className += " sent";
    if (navigator.vibrate) { try { navigator.vibrate(10); } catch (e) { /* facultatif */ } }
    fetch("/r/cmd", { method: "POST", credentials: "same-origin", cache: "no-store",
                      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
                      body: "a=" + encodeURIComponent(btn.value) + "&c=" + encodeURIComponent(csrf) })
      .then(function (r) {
        if (r.status === 401 || r.status === 403 || r.status === 503) { location.replace("/r"); return; }
        say(r.status === 429 ? "Doucement…" : (r.ok ? "" : "Touche non envoyée."));
      })
      .catch(function () { say("TV injoignable : ce téléphone est-il sur le même Wi-Fi que la TV ?"); })
      .then(function () {
        busy -= 1;
        setTimeout(function () { btn.className = btn.className.replace(/ sent/g, ""); }, 120);
      });
  }
  var btns = pad.querySelectorAll("button[name=a]");
  for (var i = 0; i < btns.length; i++) {
    btns[i].addEventListener("click", function (e) { e.preventDefault(); send(this); });
  }
})();
