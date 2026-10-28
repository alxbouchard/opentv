/* Validation réelle (Chromium headless) des scripts d'assistance du lecteur : PROBE/MAXIMIZE/CHECK(_FAST) d'assist.py
   exécutés sur des pages de test (tests seulement, jamais installé). argv[2] = module playwright ; argv[3] = chemin
   d'un JSON {chromium, probe, maximize, check, fast, cases:[{name, html, after}]}. Sortie : une ligne JSON par cas réunis.

   Preuve PIXEL : le contenu du lecteur de test est un aplat vert pur ; après CHECK, une capture 1920×1080 est
   échantillonnée sur une grille dense (33×19 points + bandes de bord). « ready » doit impliquer 100 % de pixels verts
   (rien de la page parente n'est peint au-dessus) ; un calque réel doit laisser des pixels non verts (le cas de test
   est une vraie obstruction, que la grille elementFromPoint seule ne voit pas). */
"use strict";
const fs = require("fs");
const pw = require(process.argv[2]);
const cfg = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const GRID_ONLY = `(function () {
  var e = window.__opentvPlayer, W = innerWidth, H = innerHeight, i, j;
  var xs = [4, Math.floor(W / 4), Math.floor(W / 2), Math.floor(3 * W / 4), W - 5];
  var ys = [4, Math.floor(H / 4), Math.floor(H / 2), Math.floor(3 * H / 4), H - 5];
  for (i = 0; i < 5; i++) for (j = 0; j < 5; j++) if (document.elementFromPoint(xs[i], ys[j]) !== e) return "obstructed";
  return "ready";
})()`;

function points(W, H) {
  const pts = [];
  for (let i = 0; i <= 32; i++) {
    for (let j = 0; j <= 18; j++) {
      pts.push([Math.min(W - 1, Math.round(i * (W - 1) / 32)), Math.min(H - 1, Math.round(j * (H - 1) / 18))]);
    }
  }
  for (let x = 2; x < W; x += 60) { pts.push([x, 2], [x, H - 3]); }
  for (let y = 2; y < H; y += 60) { pts.push([2, y], [W - 3, y]); }
  return pts;
}

(async () => {
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  const out = {};
  try {
    const decoder = await browser.newPage({ viewport: { width: 64, height: 64 } });
    for (const c of cfg.cases) {
      const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
      await page.setContent(c.html, { waitUntil: "load" });
      const probe = await page.evaluate(cfg.probe);
      const max = await page.evaluate(cfg.maximize);
      if (c.after) { await page.evaluate(c.after); }
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
      const check = await page.evaluate(cfg.check);
      const fast = await page.evaluate(cfg.fast);
      const grid = await page.evaluate(GRID_ONLY);
      const png = (await page.screenshot({ type: "png" })).toString("base64");
      const px = await decoder.evaluate(async ({ b64, pts }) => {
        const img = new Image();
        img.src = "data:image/png;base64," + b64;
        await img.decode();
        const cv = document.createElement("canvas");
        cv.width = img.width; cv.height = img.height;
        const g = cv.getContext("2d");
        g.drawImage(img, 0, 0);
        const d = g.getImageData(0, 0, img.width, img.height).data;
        let green = 0;
        const bad = [];
        for (const [x, y] of pts) {
          const o = (y * img.width + x) * 4;
          const ok = d[o] <= 8 && d[o + 1] >= 247 && d[o + 2] <= 8;
          if (ok) { green++; } else if (bad.length < 5) { bad.push([x, y, d[o], d[o + 1], d[o + 2]]); }
        }
        return { total: pts.length, green: green, bad: bad, w: img.width, h: img.height };
      }, { b64: png, pts: points(1920, 1080) });
      out[c.name] = { probe: probe.state, max: max, check: check.state, why: check.why || null, how: check.how || null,
                      fast: fast.state, fastWhy: fast.why || null, grid: grid, px: px };
      await page.close();
    }
    await decoder.close();
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
