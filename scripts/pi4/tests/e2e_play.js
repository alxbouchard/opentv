/* Bout en bout « Regarder » (tests seulement, jamais installé) : vrai service OpenTV + vraie page /tv 1920×1080 dans
   Chromium headless ; contrôleur du lecteur simulé côté Python (révélation, Retour CEC/HID simulé, aucun lecteur).
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, shots}. Sortie : une ligne JSON. */
"use strict";
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const out = { steps: [] };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const snap = async (label, shot) => {
      const s = await page.evaluate(() => {
        const q = (id) => document.getElementById(id);
        const f = document.querySelector(".card.focused .t");
        const btns = Array.prototype.map.call(document.querySelectorAll("#pbtns button"), (b) => ({
          t: b.textContent, focused: b.className.indexOf("focused") >= 0 }));
        const ov = q("playov"), r = ov.getBoundingClientRect();
        return { focus: f ? f.textContent : null, panel: !q("panel").hidden, ptitle: q("ptitle").textContent,
                 btns: btns, overlay: !ov.hidden, ovRect: [r.left, r.top, r.width, r.height],
                 ovTitle: q("playtitle").textContent, ovMsg: q("playmsg").textContent,
                 toast: q("toast").hidden ? null : q("toast").textContent,
                 url: location.pathname, iframes: document.querySelectorAll("iframe").length,
                 html: document.documentElement.outerHTML.length };
      });
      s.s = label;
      if (shot && cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, label + ".png") }); }
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
    const overlayHidden = () => page.waitForFunction(() => document.getElementById("playov").hidden,
                                                     null, { timeout: 15000 });
    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.keyboard.press("ArrowRight");
    await sleep(200);
    await snap("card", true);
    await page.keyboard.press("Enter");
    await sleep(250);
    await snap("item", true);
    await page.keyboard.press("Enter");                       // « Regarder »
    await sleep(120);
    await snap("loading", true);
    out.playing_seen = await waitApi(["playing"], 5000);       // le contrôleur simulé révèle le lecteur
    await overlayHidden();                                     // fiche rouverte SOUS la fenêtre du lecteur
    await snap("playing", true);
    // le contrôleur simulé reçoit Retour (CEC/HID) ; l'utilisateur garde la touche enfoncée : la télécommande répète
    // l'appui (nouveaux keydown sans repeat) puis des répétitions auto, pendant ~0,8 s après la fermeture
    out.closed_seen = await waitApi(["closed", "idle"], 15000);
    for (let i = 0; i < 6; i++) { await page.keyboard.press("Escape"); await sleep(120); }
    await page.keyboard.down("Escape");
    await page.keyboard.down("Escape");                        // repeat=true
    await page.keyboard.up("Escape");
    await snap("held", true);
    out.after_return = await waitApi(["idle"], 5000);          // repeint puis acquitté
    await sleep(700);                                          // touche relâchée : la garde expire
    await snap("returned", true);
    await page.keyboard.press("Escape");                       // nouvel appui délibéré : Retour ferme la fiche
    await sleep(200);
    await snap("released", false);
    await page.keyboard.press("Enter");                        // même carte : la fiche se rouvre sur « Regarder »
    await sleep(250);
    await snap("reopened", false);
    await page.keyboard.press("Enter");                        // cette fois aucun lecteur
    await sleep(100);
    await snap("loading2", false);
    await overlayHidden();
    await sleep(300);
    await snap("unavailable", true);
    await sleep(500);
    let v = await api("/api/tv/play");
    out.after_unavailable = v.state;
    await page.keyboard.press("Enter");                        // troisième lecture : Retour sur /tv pendant le chargement
    await sleep(150);
    await page.keyboard.press("Escape");
    await overlayHidden();
    out.after_cancel = await waitApi(["idle"], 15000);
    await sleep(300);
    await snap("cancelled", false);
    out.state_text = JSON.stringify(await api("/api/tv/state"));
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
