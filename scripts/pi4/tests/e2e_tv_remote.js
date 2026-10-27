/* Bout en bout (tests seulement, jamais installé) : vrai service OpenTV + vraie page /tv dans Chromium headless (temps
   réel) ; clavier HID (Playwright) puis téléphone (POST /r/cmd avec cookie + CSRF). Sortie : une ligne JSON.
   argv[2] = chemin du module playwright ; argv[3] = JSON {chromium, tv, port, host, cookie, csrf}.
   Le pavé est joint sur 127.0.0.1:<port> avec l'en-tête Host = nom mDNS de la boîte (seul Host admis avec l'adresse
   liée). */
"use strict";
const http = require("http");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const out = { steps: [] };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    const snap = async (label) => {
      const s = await page.evaluate(() => {
        const f = document.querySelector(".card.focused .t");
        return { focus: f ? f.textContent : null, panel: !document.getElementById("panel").hidden,
                 ptitle: document.getElementById("ptitle").textContent, url: location.pathname,
                 remotebox: !document.getElementById("remotebox").hidden };
      });
      s.s = label;
      out.steps.push(s);
      return s;
    };
    await snap("start");
    // niveau 3 : télécommande USB HID / clavier (événements clavier réels du navigateur)
    for (const k of ["ArrowDown", "Down2", "ArrowRight", "Enter", "Escape", "Home", "Backspace"]) {
      if (k === "Down2") { await page.keyboard.press("ArrowDown"); await sleep(150); await snap("key:Down2"); continue; }
      await page.keyboard.press(k);
      await sleep(150);
      await snap("key:" + k);
    }
    // niveau 2 : téléphone (même requête que le pavé, cookie HttpOnly + CSRF + Origin)
    await sleep(1500);   // la page TV a vu le téléphone appairé et attend en longue requête
    const post = (a) => new Promise((resolve, reject) => {
      const body = "a=" + a + "&c=" + cfg.csrf;
      const req = http.request({ host: "127.0.0.1", port: cfg.port, path: "/r/cmd", method: "POST", headers: {
        Host: cfg.host, Origin: "http://" + cfg.host, Cookie: "otv_remote=" + cfg.cookie, Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(body) } }, (r) => {
        r.resume();
        r.on("end", () => resolve(r.statusCode));
      });
      req.on("error", reject);
      req.end(body);
    });
    out.posts = [await post("down"), await post("down")];
    await sleep(400);
    await snap("phone:down");
    out.posts.push(await post("right"));
    await sleep(400);
    await snap("phone:right");
    const t0 = Date.now();
    out.posts.push(await post("ok"));
    await page.waitForSelector("#panel:not([hidden])", { timeout: 4000 });
    out.ok_latency_ms = Date.now() - t0;
    await snap("phone:ok");
    out.posts.push(await post("back"));
    await page.waitForSelector("#panel[hidden]", { state: "attached", timeout: 4000 });
    await snap("phone:back");
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
