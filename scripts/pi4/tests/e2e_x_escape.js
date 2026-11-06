/* Bout en bout X TV — sortie « appareil » KEY_EXIT (tests seulement, jamais installé). Chromium COMPLET réel +
   extension installée + vrai service OpenTV ; pages X SYNTHÉTIQUES (aucun réseau). Piloté ligne à ligne par le test
   Python (test_x_escape.TestXEscapeEndToEnd), qui joue l'aide evdev et l'action root réelles :
     node -> "X_READY"      X ouvert depuis /tv par OK humain (le service a posé le témoin) ; aucune touche Retour
                            n'est envoyée à la page (terrain : Chromium/Wayland ne délivre pas KEY_EXIT)
     py   -> "RESTART"      l'action root a demandé `systemctl restart opentv-kiosk.service` : simulée ici comme le
                            vrai kiosque (onglet fermé, Chromium repart sur l'URL de démarrage /tv, même profil)
     node -> "TV <json>"    /tv rechargée (le service a retiré le témoin)
     py   -> "END"          fin ; dernière ligne JSON.
   argv[2] = module playwright ; argv[3] = JSON {chrome, ext, ext_id, tv}. */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const FEED = "<!doctype html><html><head><meta charset=utf-8><title>Home / X</title></head><body>" +
  "<main><div data-testid=primaryColumn><article data-testid=tweet tabindex=0>Post</article></div></main></body></html>";
const LANDING = "<!doctype html><html><head><meta charset=utf-8><title>X</title></head><body>" +
  "<div role=dialog aria-modal=true><button id=phone type=button>Continue with phone</button>" +
  "<input id=user type=text></div></body></html>";

const lines = readline.createInterface({ input: process.stdin });
const queue = [];
let waiter = null;
lines.on("line", (l) => { if (waiter) { const w = waiter; waiter = null; w(l.trim()); } else { queue.push(l.trim()); } });
const next = () => new Promise((r) => { if (queue.length) { r(queue.shift()); } else { waiter = r; } });
const say = (s) => process.stdout.write(s + "\n");

(async () => {
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "otvxesc"));
  const ctx = await pw.chromium.launchPersistentContext(prof, {
    executablePath: cfg.chrome, headless: true, viewport: { width: 1920, height: 1080 },
    ignoreDefaultArgs: ["--disable-extensions"],
    args: ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext]
  });
  const out = {};
  try {
    let sw = ctx.serviceWorkers()[0];
    if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
    await ctx.route(/^https:\/\/(x|twitter)\.com\//, (route) => {
      const u = new URL(route.request().url());
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: u.pathname === "/" ? LANDING : FEED });
    });
    let page = ctx.pages()[0] || await ctx.newPage();
    const focusTitle = () => page.evaluate(() => { const f = document.querySelector(".card.focused .t"); return f ? f.textContent : null; });
    const waitUrl = async (pred, ms) => {
      for (let i = 0; i < (ms || 8000) / 100; i++) { if (pred(page.url())) { return page.url(); } await sleep(100); }
      return page.url();
    };
    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    for (let i = 0; i < 14 && (await focusTitle()) !== "X"; i++) { await page.keyboard.press("ArrowDown"); await sleep(150); }
    out.tile = await focusTitle();
    await page.keyboard.press("Enter");                       // OK humain : /tv pose le témoin PUIS ouvre X
    out.opened = await waitUrl((u) => u.indexOf("https://x.com/home") === 0);
    await page.goto("https://x.com/");                        // page de connexion (photo terrain)
    await sleep(600);
    out.login_focus = await page.evaluate(() => document.activeElement && document.activeElement.id);
    say("X_READY " + JSON.stringify({ url: page.url() }));
    const cmd = await next();
    if (cmd !== "RESTART") { throw new Error("commande inattendue " + cmd); }
    out.before_restart = page.url();
    await page.close();                                        // kiosque relancé : nouveau Chromium, URL de démarrage
    page = await ctx.newPage();
    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    out.after_restart = page.url();
    say("TV " + JSON.stringify({ url: page.url(), focus: await focusTitle() }));
    const end = await next();
    if (end !== "END") { throw new Error("commande inattendue " + end); }
  } finally {
    lines.close();
    await ctx.close();
    fs.rmSync(prof, { recursive: true, force: true });
  }
  say(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
