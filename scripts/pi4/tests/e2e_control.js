/* Bout en bout du pilotage par assistant (tests seulement, jamais installé) : vrai service OpenTV + vraie page /tv
   1920×1080 dans Chromium headless. Les commandes d'assistant passent par l'Agent Port réel (AgentService.handle)
   avec l'identité LIÉE « muse-device » du pont Muse (canal fichier ligne à ligne : cfg.ctl -> cfg.res) ; contrôleur
   du lecteur simulé côté Python. Tout libellé visible doit dire « Muse », jamais « muse-device » ni « un assistant ».
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, shots, ctl, res}. Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let sent = 0;
async function agent(tool, args, extra) {
  fs.appendFileSync(cfg.ctl, JSON.stringify(Object.assign({ tool: tool, args: args || {} }, extra || {})) + "\n");
  sent += 1;
  for (let i = 0; i < 1000; i++) {
    let lines = [];
    try { lines = fs.readFileSync(cfg.res, "utf8").split("\n").filter((l) => l.trim()); } catch (e) { lines = []; }
    if (lines.length >= sent) { return JSON.parse(lines[sent - 1]); }
    await sleep(20);
  }
  throw new Error("agent timeout " + tool);
}
async function finalState(seq, ms) {   // état autoritaire d'une action TV (status de l'agent)
  for (let i = 0; i < (ms || 8000) / 100; i++) {
    const r = await agent("status");
    const a = (r.status.actions || []).filter((x) => x.seq === seq)[0];
    if (a && a.state !== "queued" && a.state !== "claimed") { return a; }
    await sleep(100);
  }
  return null;
}

(async () => {
  const out = { steps: [], applied: {} };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const snap = async (label, shot) => {
      const s = await page.evaluate(() => {
        const q = (id) => document.getElementById(id);
        const f = document.querySelector(".card.focused .t");
        const btns = Array.prototype.map.call(document.querySelectorAll("#pbtns button"), (b) => ({
          t: b.textContent, focused: b.className.indexOf("focused") >= 0 }));
        const ov = q("playov");
        const bn = q("banner");
        return { banner: bn.hidden ? null : bn.textContent, bodyText: document.body.innerText,
                 focus: f ? f.textContent : null, panel: !q("panel").hidden, ptitle: q("ptitle").textContent,
                 kicker: q("pkicker").textContent, pmeta: q("pmeta").textContent, btns: btns,
                 overlay: !ov.hidden, ovTitle: q("playtitle").textContent, ovMsg: q("playmsg").textContent,
                 gear: q("gear").className, url: location.pathname, iframes: document.querySelectorAll("iframe").length,
                 html: document.documentElement.outerHTML };
      });
      s.s = label;
      for (const bad of ["muse-device", "un assistant"]) {   // nom brut ou anonyme : jamais à l'écran
        if (s.bodyText.indexOf(bad) >= 0 || s.kicker.indexOf(bad) >= 0) {
          throw new Error("libellé interdit : " + bad);
        }
      }
      delete s.bodyText;
      for (const bad of ["https://", "http://", "spam.example", "films-mirror", "/b/fakrov"]) {
        // seule l'affiche https approuvée peut figurer dans le DOM, en attribut src d'une image de carte
        if (bad !== "https://" && s.html.indexOf(bad) >= 0) { throw new Error("adresse dans le DOM : " + bad); }
      }
      delete s.html;
      if (shot && cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, label + ".png") }); }
      out.steps.push(s);
      return s;
    };
    const api = (p) => page.evaluate((p) => fetch(p, { headers: { "X-OpenTV-TV":
      document.querySelector('meta[name="opentv-key"]').content }, cache: "no-store" }).then((r) => r.json()), p);
    const waitPanel = (title) => page.waitForFunction((t) => !document.getElementById("panel").hidden &&
      document.getElementById("ptitle").textContent === t, title, { timeout: 15000 });

    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });

    // 1. proposition d'un film hors des rangées (marqué « Déjà vu ») : fiche « Proposé par Muse » par-dessus
    let r = await agent("present_title", { id: "films/103", text: "Une comédie pour finir la semaine." });
    if (!r.ok) { throw new Error("present: " + JSON.stringify(r)); }
    await waitPanel("Rire Encore");
    await sleep(150);
    await snap("proposed", true);
    out.applied.present1 = (await finalState(r.result.action.seq)).state;

    // 2a. OK d'assistant sur « Regarder » : refusé (lancement seulement par play_title)
    r = await agent("remote_control", { key: "ok" });
    let f = await finalState(r.result.action.seq);
    out.applied.ok_on_watch = [f.state, f.reason];
    // 2b. touche humaine puis touche d'assistant aussitôt : la télécommande d'Alex est prioritaire
    await page.keyboard.press("ArrowRight");
    r = await agent("remote_control", { key: "left" });
    f = await finalState(r.result.action.seq);
    out.applied.human_first = [f.state, f.reason];
    await page.keyboard.press("Escape");                         // Alex ferme la fiche
    await sleep(3500);                                           // fin de la priorité humaine (2 s + marge)
    // 2c. assistant : ↑ depuis la rangée 0 atteint le cercle des Réglages, mais OK y est interdit
    r = await agent("remote_control", { key: "up" });
    f = await finalState(r.result.action.seq);
    out.applied.up = [f.state, f.reason];
    r = await agent("remote_control", { key: "ok" });
    f = await finalState(r.result.action.seq);
    out.applied.settings_ok = [f.state, f.reason];
    await sleep(200);
    await snap("after_settings_try", false);
    r = await agent("remote_control", { key: "down" });
    await finalState(r.result.action.seq);

    // 3. proposition d'un film des rangées : sa carte prend le focus, puis la fiche s'ouvre
    r = await agent("present_title", { id: "films/106", text: "Science-fiction très bien notée." });
    await waitPanel("Les Étoiles");
    await sleep(150);
    await snap("proposed_in_rows", true);

    // 3b. Alex vient d'agir (→ dans la fiche) : une nouvelle proposition de l'assistant ne remplace pas sa fiche
    await page.keyboard.press("ArrowRight");
    r = await agent("present_title", { id: "films/101", text: "Autre idée." });
    f = await finalState(r.result.action.seq);
    out.applied.present_human_first = [f.state, f.reason];
    await sleep(150);
    await snap("present_after_human", false);

    // 4. Alex demande de le regarder : play_title (portes officielles) -> écran OpenTV, lecteur, puis close_player
    r = await agent("play_title", { id: "films/106" });
    if (!r.ok) { throw new Error("play: " + JSON.stringify(r)); }
    const playSeq = r.result.action.seq;
    await page.waitForFunction(() => !document.getElementById("playov").hidden, null, { timeout: 15000 });
    await snap("agent_loading", true);
    const w = await agent("wait", {}, { states: ["playing"] });
    out.playing_seen = w.result.state;
    await page.waitForFunction(() => document.getElementById("playov").hidden, null, { timeout: 15000 });
    await snap("playing_under", false);
    await page.keyboard.press("ArrowDown");                      // touche humaine : fermer reste toujours permis
    r = await agent("close_player");
    if (!r.ok || !r.result.closed) { throw new Error("close: " + JSON.stringify(r)); }
    for (let i = 0; i < 250; i++) {
      const v = await api("/api/tv/play");
      if (v.state === "idle") { out.after_return = "idle"; break; }
      await sleep(40);
    }
    await sleep(400);
    await snap("returned", true);
    out.applied.play = (await finalState(playSeq)).state;

    // 5. rechargement : aucune action déjà appliquée n'est rejouée
    const st = await api("/api/tv/state");
    out.reload_pending = (st.actions && st.actions.pending || []).length;
    await page.reload();
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await sleep(2600);
    await snap("reloaded", false);

    // 6. bannière d'un message envoyé par l'identité liée : « Message de Muse », jamais l'identifiant brut
    r = await agent("display_message", { text: "Bonne soirée, Alex !", seconds: 30 });
    if (!r.ok) { throw new Error("message: " + JSON.stringify(r)); }
    await page.waitForFunction(() => !document.getElementById("banner").hidden, null, { timeout: 15000 });
    await snap("message", true);
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
