/* Bout en bout (tests seulement, jamais installé) : « Connecter Muse » avec le vrai service, le vrai helper root
   (MuseHelper, administration Muse simulée côté Python) et le vrai Chromium.
   1) TV 1920×1080 : Réglages -> Gérer les assistants -> Muse -> consentement (« Pas maintenant » au focus) ->
      « Autoriser et connecter » -> QR ; mesures (rien de rogné, un seul QR, aucune adresse/jeton à l'écran) ;
   2) téléphone 390×844 SANS JavaScript : étape 1 puis 2 -> champ jamais affiché, rien ne peut partir (échec fermé) ;
   3) téléphone 390×844 AVEC JavaScript, origine http://<nom>.local vérifié (jamais une IP ; contexte NON sécurisé :
      pas de crypto.subtle) :
      « Utiliser mon jeton existant » (un jeton, plusieurs appareils ; page officielle en lien secondaire), étape 2, rechargement (secret du QR gardé), jeton invalide refusé sur place, jeton valide
      chiffré puis envoyé, étape 3 et avancement réel ; Retour du navigateur ne redemande jamais le jeton ;
   4) TV : Préparation -> En attente de Muse -> Connexion -> « Muse est connectée ! ».
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, shots, lan_port, token}. Sortie : une ligne JSON. */
"use strict";
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const out = { requests: [] };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true,
                                             proxy: { server: "http://per-context" } });
  try {
    const tvctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
                                             proxy: { server: "http://per-context", bypass: "127.0.0.1" } });
    const tv = await tvctx.newPage();
    await tv.goto(cfg.tv);
    await tv.waitForSelector(".card.focused", { timeout: 10000 });
    const key = async (k) => { await tv.keyboard.press(k); await sleep(300); };
    const shot = (name) => tv.screenshot({ path: path.join(cfg.shots, name) });
    const panel = () => tv.evaluate(() => {
      const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
      const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
      const pan = document.getElementById("panel"), pr = R(pan), clipped = [];
      pan.querySelectorAll("*").forEach((e) => {
        if (e.getClientRects().length && getComputedStyle(e).visibility !== "hidden" && !inside(R(e), pr)) {
          clipped.push((e.id || e.tagName) + " " + R(e).map(Math.round));
        }
      });
      const f = document.querySelector("#pbtns .focused");
      return { title: document.getElementById("ptitle").textContent, hidden: pan.hidden,
               btns: Array.prototype.map.call(document.querySelectorAll("#pbtns button"),
                                              (b) => b.getAttribute("data-label") || b.textContent),
               focused: f ? (f.getAttribute("data-label") || f.textContent) : null,
               caps: Array.prototype.map.call(document.querySelectorAll("#pcaps:not([hidden]) li"), (x) => x.textContent),
               phases: Array.prototype.map.call(document.querySelectorAll("#pphases:not([hidden]) li"),
                                                (x) => [x.textContent, x.className]),
               meta: document.getElementById("pmeta").textContent,
               qr: document.getElementById("pqrfig").hidden ? null : document.getElementById("aqr").getAttribute("data-qr"),
               panel_in_view: inside(pr, [0, 0, innerWidth, innerHeight]), clipped: clipped,
               remotebox_hidden: document.getElementById("remotebox").hidden, text: document.body.innerText,
               toast: document.getElementById("toast").hidden ? null : document.getElementById("toast").textContent };
    });
    await key("ArrowUp");
    await key("Enter");                     // Réglages
    await key("ArrowDown");
    await key("Enter");                     // Gérer les assistants
    out.assistants = await panel();
    await shot("tv-muse-assistants.png");
    const probed = tv.waitForResponse((r) => r.url().indexOf("/api/tv/muse/probe") >= 0, { timeout: 10000 });
    await key("Enter");                     // Muse
    // propriété relue par la sonde puis publiée : la feuille se met à jour d'elle-même (focus remis sur l'option sûre).
    // Attendre ce premier état APRÈS la sonde, sinon ↑ peut tomber juste avant ce rafraîchissement (course de minutage)
    await probed;
    await tv.waitForResponse((r) => r.url().indexOf("/api/tv/state") >= 0, { timeout: 10000 });
    await sleep(150);
    out.consent = await panel();
    await shot("tv-muse-consent.png");
    // seule requête permise avant le consentement : relire l'état public de la boîte (aucune écriture d'autorisation)
    out.posts_before_consent = await tv.evaluate(() => performance.getEntriesByType("resource")
      .filter((e) => e.name.indexOf("/api/tv/muse/") >= 0 && e.name.indexOf("/api/tv/muse/probe") < 0).length);
    out.probes_before_consent = await tv.evaluate(() => performance.getEntriesByType("resource")
      .filter((e) => e.name.indexOf("/api/tv/muse/probe") >= 0).length);
    await key("ArrowUp");
    await key("Enter");                     // « Autoriser et connecter »
    await tv.waitForSelector("#pqrfig:not([hidden]) canvas[data-qr=ok]", { timeout: 15000 });
    out.qr = await panel();
    await shot("tv-muse-qr.png");
    const st = await tv.evaluate(() => fetch("/api/tv/state", { headers: {
      "X-OpenTV-TV": document.querySelector('meta[name="opentv-key"]').content } }).then((r) => r.json()));
    const url = st.muse.url;
    out.origin = new URL(url).origin;

    // -- téléphone SANS JavaScript : rien ne peut partir
    const nojs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 },
                                            proxy: { server: "http://127.0.0.1:" + cfg.lan_port } });
    const p0 = await nojs.newPage();
    await p0.goto(url);
    out.nojs_step1 = await p0.textContent("main");
    await Promise.all([p0.waitForNavigation(), p0.click("#next")]);
    out.nojs_step2 = { text: await p0.textContent("main"), input_visible: await p0.isVisible("#tok"),
                       form_visible: await p0.isVisible("#seal") };
    await p0.screenshot({ path: path.join(cfg.shots, "phone-muse-nojs.png"), fullPage: true });
    await nojs.close();

    // -- téléphone AVEC JavaScript, origine http://<nom>.local en clair (contexte non sécurisé)
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 },
                                           proxy: { server: "http://127.0.0.1:" + cfg.lan_port } });
    const ph = await ctx.newPage();
    const reqs = [];
    ph.on("request", (r) => { reqs.push(r); });
    await ph.goto(url);
    out.secure_context = await ph.evaluate(() => [window.isSecureContext, !!(window.crypto && window.crypto.subtle)]);
    out.step1 = { text: await ph.textContent("main"), official: await ph.getAttribute("#official", "href"),
                  target: await ph.getAttribute("#official", "target"), rel: await ph.getAttribute("#official", "rel"),
                  next_h: Math.round((await (await ph.$("#next")).boundingBox()).height) };
    await ph.screenshot({ path: path.join(cfg.shots, "phone-muse-step1.png"), fullPage: true });
    await Promise.all([ph.waitForNavigation(), ph.click("#next")]);
    await ph.reload();                                                            // rafraîchir : rien perdu
    await ph.waitForSelector("#seal:not([hidden])", { timeout: 5000 });
    out.step2_url = ph.url();
    out.step2 = { text: await ph.textContent("main"), type: await ph.getAttribute("#tok", "type"),
                  name: await ph.getAttribute("#tok", "name") };
    await ph.fill("#tok", "mgst_pas-un-vrai-jeton");
    await ph.click("#send");
    await ph.waitForSelector("#merr:not([hidden])");
    out.bad = { err: await ph.textContent("#merr"), value: await ph.inputValue("#tok") };
    await ph.screenshot({ path: path.join(cfg.shots, "phone-muse-step2.png"), fullPage: true });
    await ph.fill("#tok", "  " + cfg.token + "\n");
    await Promise.all([ph.waitForURL(/step=3/, { timeout: 15000 }), ph.click("#send")]);
    out.step3_url = ph.url();
    out.storage = await ph.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie]);
    await ph.screenshot({ path: path.join(cfg.shots, "phone-muse-step3.png"), fullPage: true });

    // -- TV : avancement réel, puis connectée
    await tv.waitForFunction(() => {
      const l = document.querySelectorAll("#pphases li.on");
      return l.length && l[0].textContent === "En attente de Muse";
    }, null, { timeout: 20000 });
    out.waiting = await panel();
    await shot("tv-muse-waiting.png");
    await ph.waitForFunction(() => document.getElementById("mstatus").textContent.indexOf("En attente de Muse") === 0,
                             null, { timeout: 10000 });
    out.phone_waiting = await ph.textContent("#mstatus");
    await tv.waitForFunction(() => !document.getElementById("toast").hidden &&
      document.getElementById("toast").textContent.indexOf("Muse est connectée") === 0, null, { timeout: 20000 });
    out.connected = await panel();
    await shot("tv-muse-connected.png");
    await ph.waitForFunction(() => document.getElementById("mstatus").textContent.indexOf("Muse est connectée") === 0,
                             null, { timeout: 10000 });
    out.phone_connected = await ph.textContent("#mstatus");
    await ph.screenshot({ path: path.join(cfg.shots, "phone-muse-connected.png"), fullPage: true });
    await ph.goBack();                                                            // Retour : jamais redemandé
    out.back = { url: ph.url(), text: await ph.textContent("main"), input: await ph.$("#tok") !== null };
    // TV : Gérer les assistants montre Muse connectée, Déconnecter derrière une confirmation (« Non » au focus)
    await sleep(2500);
    await key("ArrowUp");
    await key("Enter");
    await key("ArrowDown");
    await key("Enter");
    out.assistants_after = await panel();
    await key("Enter");
    out.muse_connected = await panel();
    await key("ArrowUp");
    await key("Enter");
    out.disconnect_confirm = await panel();
    await shot("tv-muse-disconnect.png");
    await key("Escape");
    await key("Escape");
    await key("Escape");
    await key("Escape");
    out.after_escape = await panel();
    for (const r of reqs) {   // en-têtes réellement envoyés (Origin comprise)
      out.requests.push({ url: r.url(), method: r.method(), body: r.postData() || "", headers: await r.allHeaders() });
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
