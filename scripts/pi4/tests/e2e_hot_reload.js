/* Bout en bout « catalogue remplacé pendant la navigation, une fiche ou une lecture » (tests seulement, jamais
   installé) : vrai service OpenTV + vraie page /tv 1920×1080 dans Chromium headless, horloge de page de Playwright
   (temps naturel + sauts de 61 s pour atteindre FY_REFRESH_MS sans attendre). Le catalogue est remplacé sur disque
   comme le rafraîchissement quotidien (fichier du même dossier puis rename : nouvel inode) ; le contrôleur du lecteur
   est simulé côté Python, piloté par des fichiers de commande (press / slow / reveal) du dossier `ctl`.
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, catfile, ctl, catalogues:{A,B,C}, shots}.
   Sortie : une ligne JSON {steps:[…]}. */
"use strict";
const fs = require("fs");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const out = { steps: [], fetches: 0 };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    page.on("response", (r) => { if (r.url().indexOf("/api/tv/foryou") >= 0) { out.fetches += 1; } });
    await page.clock.install();
    const swap = (k) => {   // remplacement atomique, même dossier (comme catalogue.py)
      const tmp = path.join(path.dirname(cfg.catfile), ".catalogue.json.e2e.candidate");
      fs.writeFileSync(tmp, cfg.catalogues[k]);
      fs.renameSync(tmp, cfg.catfile);
    };
    const ctl = (name) => fs.writeFileSync(path.join(cfg.ctl, name), "1");
    let probeN = 0;
    const probe = () => page.evaluate((n) => {   // marque les nœuds actuels : un nouveau rendu les remplace
      const r = document.querySelector("#rowsin .row");
      if (r) { r.__probe = n; }
      return !!r;
    }, ++probeN);
    const snap = async (label, shot) => {
      const s = await page.evaluate((n) => {
        const q = (id) => document.getElementById(id);
        const f = document.querySelector(".card.focused");
        const row = f ? f.parentNode.parentNode.parentNode : null;
        const first = document.querySelector("#rowsin .row");
        return { focus: f ? f.querySelector(".t").textContent : null,
                 col: f ? Array.prototype.indexOf.call(f.parentNode.children, f) : -1,
                 rowTitle: row ? row.querySelector("h2").textContent : null,
                 panel: !q("panel").hidden, ptitle: q("ptitle").textContent,
                 btn0: (document.querySelector("#pbtns button") || {}).textContent || null,
                 overlay: !q("playov").hidden, ovMsg: q("playmsg").textContent,
                 htitle: q("htitle").textContent,
                 marks: f ? Array.prototype.map.call(f.querySelectorAll(".marks span"), (e) => e.textContent) : [],
                 probe: !!(first && first.__probe === n),
                 titles: Array.prototype.map.call(document.querySelectorAll("#rowsin .card .t"), (e) => e.textContent),
                 url: location.pathname, iframes: document.querySelectorAll("iframe").length };
      }, probeN);
      s.s = label;
      s.fetches = out.fetches;
      if (shot && cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "hot-" + label + ".png") }); }
      out.steps.push(s);
      return s;
    };
    const api = (p) => page.evaluate((p) => fetch(p, { headers: { "X-OpenTV-TV":
      document.querySelector('meta[name="opentv-key"]').content }, cache: "no-store" }).then((r) => r.json()), p);
    const waitApi = async (states, ms) => {
      for (let i = 0; i < ms / 20; i++) {
        const v = await api("/api/tv/play");
        if (states.indexOf(v.state) >= 0) { return v.state; }
        await sleep(20);
      }
      return null;
    };
    // ≥ FY_REFRESH_MS plus tard : le prochain poll d'état (2 s) redemande « Pour toi » ; attend la réponse servie
    const tick61 = async () => {
      const before = out.fetches;
      await page.clock.fastForward(61000);
      for (let i = 0; i < 250 && out.fetches === before; i++) {
        await sleep(20);
        if (i % 25 === 24) { await page.clock.fastForward(2100); }   // poll suivant si le premier était en vol
      }
      await sleep(250);                                   // réponse traitée (rendu ou mise en attente)
      return out.fetches > before;
    };
    const key = async (k, ms) => { await page.keyboard.press(k); await sleep(ms || 200); };

    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await key("ArrowDown"); await key("ArrowDown"); await key("ArrowRight");
    await snap("start", true);

    // 0) même réponse 61 s plus tard : aucune reconstruction des rangées
    await probe();
    out.f0 = await tick61();
    await snap("unchanged", false);

    // 1) accueil inactif, catalogue A -> B (le film change de colonne, sa colonne d'origine porte un autre film)
    swap("B");
    await probe();
    out.f1 = await tick61();
    await page.waitForFunction((n) => { const r = document.querySelector("#rowsin .row"); return r && r.__probe !== n; },
                               probeN, { timeout: 5000 });
    await snap("idle_swapped", true);

    // 2) fiche ouverte, B -> A : rien n'est redessiné tant que la fiche reste ouverte ; Retour l'applique
    await key("Enter", 250);
    swap("A");
    await probe();
    out.f2 = await tick61();
    await snap("panel_deferred", true);
    await key("Escape", 300);
    await snap("panel_closed", true);

    // 3) lecture en cours, A -> B : différé pendant la lecture et sur la fiche rouverte au retour
    await key("Enter", 250);
    await key("Enter", 150);                               // « Regarder »
    out.playing3 = await waitApi(["playing"], 8000);
    await page.waitForFunction(() => document.getElementById("playov").hidden, null, { timeout: 15000 });
    swap("B");
    await probe();
    out.f3 = await tick61();
    await snap("play_deferred", true);
    ctl("press");                                          // Retour vu par le contrôleur (CEC/HID)
    out.idle3 = await waitApi(["idle"], 15000);
    await sleep(700);                                      // garde du Retour tenu expirée
    await snap("play_returned", true);
    await key("Escape", 300);
    await snap("play_closed", true);

    // 4) chargement en cours, B -> C (le film est RETIRÉ) : différé ; retour sur la fiche du film, puis repli
    ctl("slow");
    await sleep(100);
    await key("Enter", 250);
    await key("Enter", 150);
    out.loading4 = await waitApi(["loading"], 5000);
    swap("C");
    await probe();
    out.f4 = await tick61();
    await snap("loading_deferred", true);
    ctl("reveal");
    out.playing4 = await waitApi(["playing"], 8000);
    await page.waitForFunction(() => document.getElementById("playov").hidden, null, { timeout: 15000 });
    ctl("press");
    out.idle4 = await waitApi(["idle"], 15000);
    await sleep(700);
    await snap("removed_returned", true);
    await key("Escape", 300);
    await snap("removed_closed", true);

    // 5) « J'aime » sur une fiche pendant qu'une sélection attend (C -> B) : appliquée à la fermeture, badge gardé
    await key("Enter", 250);
    swap("B");
    await probe();
    out.f5 = await tick61();
    await snap("like_deferred", false);
    await key("ArrowRight", 150);
    const before5 = out.fetches;
    // relecture forcée retardée : l'état juste après la fermeture vient de la sélection en attente seule
    await page.route("**/api/tv/foryou", async (route) => { await sleep(1500); await route.continue(); });
    await key("Enter", 400);                               // « J'aime »
    await snap("liked_immediate", false);
    for (let i = 0; i < 150 && out.fetches === before5; i++) { await sleep(20); }   // relecture du service
    await sleep(400);
    out.f5b = out.fetches > before5;
    await page.unroute("**/api/tv/foryou");
    await snap("liked_applied", true);
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
