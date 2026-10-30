/* Bout en bout OpenTV Canvas avec le VRAI service (tests seulement, jamais installé) : API TV, magasin Canvas réel sur
   disque, horloge monotone réelle ; la page /tv réelle 1920×1080. Les gestes d'agent (aperçu), du téléphone et les
   pannes injectées passent par un canal fichier ligne à ligne exécuté côté Python (cfg.ctl -> cfg.res).
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, shots, ctl, res, sparse?, stall?}. Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const QUIET_MS = 1800;   // fenêtre de silence humaine du service (1,5 s) + marge

let sent = 0;
async function ctl(op, extra) {
  fs.appendFileSync(cfg.ctl, JSON.stringify(Object.assign({ op: op }, extra || {})) + "\n");
  sent += 1;
  for (let i = 0; i < 1000; i++) {
    let lines = [];
    try { lines = fs.readFileSync(cfg.res, "utf8").split("\n").filter((l) => l.trim()); } catch (e) { lines = []; }
    if (lines.length >= sent) { return JSON.parse(lines[sent - 1]); }
    await sleep(20);
  }
  throw new Error("ctl timeout " + op);
}
const status = async () => (await ctl("status")).result;
async function until(fn, ms) {
  const end = Date.now() + (ms || 5000);
  while (Date.now() < end) { if (await fn()) { return true; } await sleep(60); }
  return false;
}

(async () => {
  const out = { steps: {} };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    await ctx.route(/^https?:\/\/(?!127\.0\.0\.1)/, (r) => r.abort());   // affiches distantes : jamais chargées ici
    const page = await ctx.newPage();
    const q = {
      focus: () => page.evaluate(() => { const f = document.querySelector(".card.focused .t"); return f ? f.textContent :
        (document.getElementById("gear").className.indexOf("focused") >= 0 ? "gear" : null); }),
      body: () => page.evaluate(() => document.body.className),
      mode: () => page.evaluate(() => document.body.getAttribute("data-cv-mode")),
      sheet: () => page.evaluate(() => !document.getElementById("cvsheet").hidden),
      toast: () => page.evaluate(() => { const t = document.getElementById("toast"); return t.hidden ? "" : t.textContent; }),
      rows: () => page.evaluate(() => Array.prototype.map.call(document.querySelectorAll("#rowsin .row h2"), (h) => h.textContent)),
      kicker: () => page.evaluate(() => document.getElementById("cvkicker").textContent)
    };
    const key = async (k, wait) => { await page.keyboard.press(k); await sleep(wait === undefined ? 200 : wait); };
    const shot = (name) => page.screenshot({ path: path.join(cfg.shots, name + ".png") });

    await page.goto(cfg.tv);
    if (cfg.sparse) {
      await until(() => page.evaluate(() => document.body.innerText.indexOf("En attendant") >= 0), 10000);
      await until(async () => (await q.mode()) === "run", 5000);
      await sleep(600);
      const st = await status();
      out.steps.sparse = { text: await page.evaluate(() => document.body.innerText), busy: st.busy };
      await shot("sparse");
      return;
    }
    await page.waitForSelector(".card.focused", { timeout: 15000 });
    await until(async () => (await q.mode()) === "run", 5000);
    await sleep(700);
    if (cfg.stall) {
      // Réglages ouverts (occupation « panel » acquittée), puis verrou du magasin tenu au-delà de l'échéance de la
      // page : fermer / rouvrir Réglages pendant ce temps ; la liste vide est livrée en retard (cfg.ctl « stall »)
      await key("ArrowUp");
      await key("Enter", QUIET_MS);
      const x = { before: (await status()).busy };
      x.held = (await ctl("stall", { hold_s: cfg.stall })).result;
      const t0 = Date.now();
      await key("Escape", 400);
      await key("Enter", 300);
      x.panelOpen = await page.evaluate(() => !document.getElementById("panel").hidden);
      let st = null, late = null;
      x.repaired = await until(async () => {
        late = (await ctl("late")).result;
        st = await status();
        return !!late.result && st.busy.indexOf("panel") >= 0;
      }, 20000);
      x.elapsedMs = Date.now() - t0;
      x.busy = st.busy;
      x.late = late.result;
      const pv = await ctl("preview", { changes: { preset: "cinema" } });
      x.preview = { ok: pv.ok, error: pv.error };
      await key("Escape", 300);
      await until(async () => (await status()).busy.length === 0, 12000);
      x.closed = (await status()).busy;
      await sleep(QUIET_MS);
      x.previewAfter = await ctl("preview", { changes: { preset: "cinema" } });
      out.steps.stall = x;
      return;
    }
    out.steps.load = { busy: (await status()).busy };
    await shot("populated");

    // panneau : l'occupation « panel » est déclarée au service, puis retirée
    await key("ArrowUp");
    await key("Enter", QUIET_MS);
    out.steps.panel = { busy: (await status()).busy };
    await key("Escape");
    await key("ArrowDown", QUIET_MS);
    out.steps.panel_closed = { busy: (await status()).busy };

    // aperçu d'un agent (identité liée « muse-device ») -> feuille, « Non » au focus ; Retour = Non
    let r = await ctl("preview", { changes: { preset: "cinema" } });
    if (!r.ok) { throw new Error("preview: " + JSON.stringify(r)); }
    const shown = await until(q.sheet, 5000);
    let st = await status();
    await sleep(400);   // transitions du focus terminées avant mesure et capture
    const probe = await page.evaluate(() => {
      const sheet = document.getElementById("cvsheet");
      const shownEl = (e) => !e.closest("[hidden]") && e.getClientRects().length > 0;
      const f = Array.prototype.filter.call(document.querySelectorAll(".focused"), shownEl);
      const L = [".cv-l1", ".cv-l2", ".cv-l3", "#cvglow"].map((s) => document.querySelector("#cvwall " + s));
      const an = document.getAnimations().filter((a) => a.animationName !== undefined && L.indexOf(a.effect.target) >= 0);
      return { hint: document.getElementById("hint").innerText, mode: document.body.getAttribute("data-cv-mode"),
               running: an.filter((a) => a.playState === "running").length,
               outside: f.filter((e) => !sheet.contains(e)).length, inside: f.filter((e) => sheet.contains(e)).map((e) => e.textContent) };
    });
    out.steps.sheet = Object.assign({ shown: shown, kicker: await q.kicker(), body: await q.body(), revision: st.revision }, probe);
    await shot("sheet");
    await key("Escape", 600);
    st = await status();
    out.steps.no = { revision: st.revision, preview: st.preview, shown: await q.sheet() };

    // « Oui » : publication réelle, rangées réordonnées après la feuille, focus réancré sur le même film
    await key("ArrowDown");
    await key("ArrowRight", QUIET_MS);
    const focusBefore = await q.focus();
    r = await ctl("preview", { changes: { preset: "cinema" } });
    if (!r.ok) { throw new Error("preview2: " + JSON.stringify(r)); }
    await until(q.sheet, 5000);
    await key("ArrowRight");
    await key("Enter", 900);
    st = await status();
    out.steps.yes = { revision: st.revision, scene: st.scene, body: await q.body(), rows: await q.rows(),
                      focus: await q.focus(), focusBefore: focusBefore, toast: await q.toast() };
    await shot("cinema");

    // geste humain AVANT que la feuille n'apparaisse : l'aperçu est annulé par le service, aucune feuille
    let seen = false;
    const watch = setInterval(async () => { try { if (await q.sheet()) { seen = true; } } catch (e) { /* fin */ } }, 80);
    await page.route("**/api/tv/state", (rt) => setTimeout(() => rt.continue().catch(() => {}), 1500));
    r = await ctl("preview", { changes: { preset: "mist" } });
    if (!r.ok) { throw new Error("preview3: " + JSON.stringify(r)); }
    await key("ArrowRight", 2500);
    await page.unroute("**/api/tv/state");
    await sleep(2500);
    clearInterval(watch);
    st = await status();
    out.steps.human_cancel = { preview: st.preview, sheet_seen: seen || await q.sheet(), human: st.human };

    // échéance : aucune réponse ⇒ « Non » automatique (rejet audité), rien publié
    await sleep(QUIET_MS);
    r = await ctl("preview", { changes: { preset: "mist" } });
    if (!r.ok) { throw new Error("preview4: " + JSON.stringify(r)); }
    await until(q.sheet, 5000);
    await until(async () => !(await q.sheet()), 14000);
    st = await status();
    out.steps.timeout = { shown: await q.sheet(), preview: st.preview, revision: st.revision, toast: await q.toast() };

    // télécommande du téléphone : événement « remote » envoyé par la page, puis la touche agit
    const fb = await q.focus();
    r = await ctl("phone", { key: "left" });
    await until(async () => (await q.focus()) !== fb, 8000);
    await until(async () => { st = await status(); return st.human[st.human.length - 1] === "remote"; }, 3000);
    out.steps.phone = { focusBefore: fb, focus: await q.focus(), human: st.human, cmd: r };

    // Réglages → Ambiance OpenTV (propriétaire) : style, mouvement, version précédente, origine, restore_failed
    await key("Home");
    await key("ArrowUp");
    await key("Enter", 300);
    await key("ArrowDown");
    await key("ArrowDown");
    await key("Enter", 300);                 // Ambiance OpenTV
    await key("Enter", 300);                 // Style (actuel : Cinéma)
    await key("ArrowUp");                    // Brume
    await key("Enter", 900);
    st = await status();
    out.steps.settings_style = { scene: st.scene, toast: await q.toast() };
    await shot("settings");
    await key("ArrowDown");                  // Mouvement du fond
    await key("Enter", 300);
    await key("ArrowUp");                    // Coupé
    await key("Enter", 900);
    st = await status();
    out.steps.settings_motion = { scene: st.scene, mode: await q.mode() };
    await key("ArrowDown");                  // Version précédente
    await key("Enter", 300);
    await key("ArrowUp");                    // « Oui, revenir » (Non au focus par défaut)
    await key("Enter", 900);
    st = await status();
    out.steps.settings_undo = { scene: st.scene };
    for (let i = 0; i < 3; i++) { await key("ArrowDown"); }   // Ambiance d'origine
    await key("Enter", 300);
    await key("ArrowUp");
    await key("Enter", 900);
    st = await status();
    out.steps.settings_reset = { scene: st.scene };
    await ctl("fail_apply");
    await key("Enter", 300);                 // Style (actuel : Canvas)
    await key("ArrowDown");                  // Brume
    await key("Enter", 900);
    out.steps.see_state = { toast: await q.toast(), body: await q.body(), scene: (await status()).scene };
    out.steps.audit = (await ctl("audit")).result;
  } finally {
    await browser.close();
    process.stdout.write(JSON.stringify(out) + "\n");
  }
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
