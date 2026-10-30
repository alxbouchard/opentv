/* Bout en bout (tests seulement, jamais installé) : « Gérer les assistants » avec le vrai service et le vrai Chromium.
   1) TV 1920×1080 (JS actif) : Réglages -> « Gérer les assistants » -> confirmation (« Non » par défaut) -> « Oui » ->
      QR dédié dans la feuille ; mesures (rien de rogné, un seul QR) et captures ;
   2) téléphone 390×844 avec JavaScript DÉSACTIVÉ : étape 1 (choix) -> étape 2 (confirmer) -> formulaire posté depuis
      une origine opaque (Origin: null, refusé) -> « Oui, enregistrer » (vrai POST) -> étape 3 ;
   3) TV : retour à l'accueil + confirmation chiffrée (autorisés ≠ connectés) ;
   4) page finale stylée (styles calculés) alors que l'écoute est déjà fermée, après « Oui, enregistrer » ET après
      « Annuler » (second QR) ; Hermes, installé au-delà de son autorisation, n'est jamais touché en silence.
   argv[2] = chemin du module playwright ; argv[3] = JSON {chromium, tv, shots}. Sortie : une ligne JSON. */
"use strict";
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const out = { posts: [] };
  // le téléphone passe par un mandataire = le service lui-même : requêtes vers la vraie adresse LAN simulée
  // (Host et Origin = http://<adresse LAN>:<port>, exactement comme un vrai téléphone) ; la TV reste en boucle locale
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true,
                                             proxy: { server: "http://per-context" } });
  try {
    const tvctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
                                             proxy: { server: "http://per-context", bypass: "127.0.0.1" } });
    const tv = await tvctx.newPage();
    await tv.goto(cfg.tv);
    await tv.waitForSelector(".card.focused", { timeout: 10000 });
    // CSP stricte (style-src 'self') : aucun style injecté ; les transitions courtes du produit sont attendues
    const key = async (k) => { await tv.keyboard.press(k); await sleep(300); };
    const shot = (name) => tv.screenshot({ path: path.join(cfg.shots, name) });
    await key("ArrowUp");                                                      // rangée 0 : Réglages
    await key("Enter");
    out.settings_buttons = await tv.$$eval("#pbtns button", (bs) => bs.map((b) => b.getAttribute("data-label") || b.textContent));
    out.settings_text = await tv.textContent("#panel");
    await key("ArrowDown");
    await shot("tv-agents-settings.png");
    await key("Enter");                                                        // Gérer les assistants : Muse | Droits
    out.assistants_buttons = await tv.$$eval("#pbtns button", (bs) => bs.map((b) => b.getAttribute("data-label") || b.textContent));
    await key("ArrowDown");
    await key("Enter");
    out.confirm_focus = await tv.textContent("#pbtns .focused");
    await shot("tv-agents-confirm.png");
    await key("ArrowUp");
    await key("Enter");
    await tv.waitForSelector("#pqrfig:not([hidden]) canvas[data-qr=ok]", { timeout: 10000 });
    out.qr_layout = await tv.evaluate(() => {
      const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
      const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
      const pan = document.getElementById("panel"), pr = R(pan), clipped = [];
      pan.querySelectorAll("*").forEach((e) => {
        if (e.getClientRects().length && !inside(R(e), pr)) { clipped.push((e.id || e.tagName) + " " + R(e).map(Math.round)); }
      });
      return { viewport: [innerWidth, innerHeight], qr: document.getElementById("aqr").getAttribute("data-qr"),
               panel_in_view: inside(pr, [0, 0, innerWidth, innerHeight]),
               remotebox_hidden: document.getElementById("remotebox").hidden, clipped: clipped,
               text: document.body.innerText };
    });
    await shot("tv-agents-qr.png");
    const st = await tv.evaluate(() => fetch("/api/tv/state", { headers: {
      "X-OpenTV-TV": document.querySelector('meta[name="opentv-key"]').content } }).then((r) => r.json()));
    const url = st.assistants.manage.url;
    out.origin = new URL(url).origin;

    const ctx = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 },
                                           proxy: { server: "http://127.0.0.1:" + cfg.lan_port } });
    const ph = await ctx.newPage();
    const reqs = [];
    ph.on("request", (r) => { if (r.method() === "POST") { reqs.push(r); } });
    const submit = async (sel) => { await Promise.all([ph.waitForNavigation(), ph.click(sel)]); };
    // styles réellement appliqués (pas seulement présents dans le HTML) sur la page affichée
    const styled = () => ph.evaluate(() => {
      const cs = (sel, prop) => { const e = document.querySelector(sel); return e ? getComputedStyle(e)[prop] : null; };
      return { body_bg: cs("body", "backgroundColor"), body_color: cs("body", "color"), h1_size: cs("h1", "fontSize"),
               main_max: cs("main", "maxWidth"), lead_size: cs(".lead", "fontSize"),
               recap_border: cs(".recap li", "borderTopColor"), st_bg: cs(".st.yes b", "backgroundColor"),
               links: document.querySelectorAll("link[rel=stylesheet]").length, sheets: document.styleSheets.length };
    });
    await ph.goto(url);
    out.phone_js = (await ph.$("script")) !== null;   // aucune balise <script> : parcours entièrement sans JS
    out.step1 = await ph.textContent("main");
    out.targets = [];
    for (const sel of ["select[name=agent_muse]", "select[name=agent_hermit]",
                       "label.check:has(input[name=canvas_muse])", "#choose button.primary", ".cancel button"]) {
      const b = await (await ph.$(sel)).boundingBox();
      out.targets.push([sel, Math.round(b.height), Math.round(b.width)]);
    }
    out.targets_ok = out.targets.every((t) => t[1] >= 56 && t[2] >= 280);
    out.defaults = await ph.$$eval("select", (ss) => ss.map((s) => [s.name, s.value]));
    // permission facultative « Changer l'ambiance OpenTV » : une case séparée par assistant, décochée par défaut
    out.canvas_boxes = await ph.$$eval("input[type=checkbox]", (bs) => bs.map((b) => [b.name, b.checked,
      b.closest("label").firstChild.nextSibling.firstChild.textContent]));
    await ph.screenshot({ path: path.join(cfg.shots, "phone-agents-step1.png"), fullPage: true });
    await ph.selectOption("select[name=agent_muse]", "feedback");
    await ph.check("input[name=canvas_muse]");
    await submit("#choose button.primary");                                     // « Continuer » (GET)
    out.step2 = await ph.textContent("main");
    const step2Url = ph.url();
    await ph.screenshot({ path: path.join(cfg.shots, "phone-agents-step2.png"), fullPage: true });
    const t = await ph.getAttribute("#save input[name=t]", "value");
    const c = await ph.getAttribute("#save input[name=c]", "value");
    // formulaire d'une origine opaque (data:) avec le bon QR et le bon CSRF : « Origin: null » -> 403
    const evil = "<form method=post action='" + out.origin + "/assistants'><input type=hidden name=t value='" + t +
                 "'><input type=hidden name=c value='" + c + "'><input type=hidden name=action value=save>" +
                 "<input type=hidden name=agent_muse value=full><input type=hidden name=agent_hermit value=full>" +
                 "<input type=hidden name=agent_hermes value=full><button id=go>go</button></form>";
    await ph.goto("data:text/html," + encodeURIComponent(evil));
    await submit("#go");
    await ph.goto(step2Url);
    await submit("#save button.primary");                                       // « Oui, enregistrer » (POST)
    out.step3 = await ph.textContent("main");
    out.step3_style = await styled();
    await ph.screenshot({ path: path.join(cfg.shots, "phone-agents-step3.png"), fullPage: true });

    await tv.waitForFunction(() => !document.getElementById("toast").hidden &&
      document.getElementById("toast").textContent.indexOf("C'est enregistré") === 0, null, { timeout: 10000 });
    out.after = await tv.evaluate(() => ({ panel: !document.getElementById("panel").hidden,
                                           toast: document.getElementById("toast").textContent,
                                           home: !document.getElementById("home").hidden }));
    await shot("tv-agents-saved.png");
    out.header = await tv.textContent("#agents");
    // second QR, puis « Annuler » sur le téléphone : page finale tout aussi stylée, TV informée, rien ne change
    await sleep(500);
    await key("ArrowUp");                                                      // accueil, rangée 0 : Réglages
    await key("Enter");
    await key("ArrowDown");
    await key("Enter");                                                        // Gérer les assistants
    await key("ArrowDown");                                                    // Droits des assistants
    await key("Enter");
    await key("ArrowUp");
    await key("Enter");
    await tv.waitForSelector("#pqrfig:not([hidden]) canvas[data-qr=ok]", { timeout: 10000 });
    const st2 = await tv.evaluate(() => fetch("/api/tv/state", { headers: {
      "X-OpenTV-TV": document.querySelector('meta[name="opentv-key"]').content } }).then((r) => r.json()));
    out.second_url_differs = st2.assistants.manage.url !== url;
    await ph.goto(st2.assistants.manage.url);
    await submit(".cancel button");                                             // « Annuler, rien ne change » (POST)
    out.cancelled = await ph.textContent("main");
    out.cancel_style = await styled();
    await ph.screenshot({ path: path.join(cfg.shots, "phone-agents-cancelled.png"), fullPage: true });
    await tv.waitForFunction(() => !document.getElementById("toast").hidden &&
      document.getElementById("toast").textContent.indexOf("Annulé") === 0, null, { timeout: 10000 });
    out.after_cancel = await tv.evaluate(() => ({ panel: !document.getElementById("panel").hidden,
                                                  toast: document.getElementById("toast").textContent }));
    await shot("tv-agents-cancelled.png");
    for (const r of reqs) {
      const h = await r.allHeaders();
      const resp = await r.response();
      out.posts.push({ path: new URL(r.url()).pathname, origin: h.origin || null, status: resp ? resp.status() : null,
                       opaque: h.origin === "null" });
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
