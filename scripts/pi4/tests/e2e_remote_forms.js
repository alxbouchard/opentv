/* Bout en bout (tests seulement, jamais installé) : vrai service OpenTV, vrai Chromium.
   1) écran TV 1920×1080 (JS actif) : mesure que la colonne du QR « Télécommande » ne chevauche aucune carte visible,
      capture d'écran ;
   2) téléphone avec JavaScript DÉSACTIVÉ : QR -> « Oui, utiliser ce téléphone » (vrai formulaire POST) -> pavé ->
      deux touches (formulaires POST) -> formulaire posté depuis une origine opaque (data:, Origin: null) -> refusé ->
      « Oublier ce téléphone » (POST). Les en-têtes Origin réellement envoyés par le navigateur sont relevés.
   Le nom mDNS de test est résolu vers 127.0.0.1 par --host-resolver-rules (aucun DNS réel).
   argv[2] = chemin du module playwright ; argv[3] = JSON {chromium, tv, pair_url, origin, map, shots}.
   Sortie : une ligne JSON. */
"use strict";
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);

async function measure(tv) {
  return tv.evaluate(() => {
    const R = (e) => e.getBoundingClientRect();
    const inter = (a, b) => ({ left: Math.max(a.left, b.left), top: Math.max(a.top, b.top),
                               right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom) });
    const ne = (r) => r.right - r.left > 0.5 && r.bottom - r.top > 0.5;
    const vp = { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
    const boxEl = document.getElementById("remotebox");
    const box = R(boxEl), main = R(document.getElementById("main"));
    const over = [];
    let vis = 0;
    document.querySelectorAll(".card").forEach((c) => {
      const v = inter(inter(R(c), main), vp);   // partie réellement peinte (#main coupe le débordement)
      if (ne(v)) { vis += 1; if (ne(inter(v, box))) { over.push(c.textContent.slice(0, 40)); } }
    });
    const arr = (r) => [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)];
    return { viewport: [window.innerWidth, window.innerHeight],
             qr: document.getElementById("rqr").getAttribute("data-qr"),
             box_visible: !boxEl.hidden && ne(box) && box.left >= 0 && box.top >= 0 && box.right <= vp.right &&
                          box.bottom <= vp.bottom,
             visible_cards: vis, card_overlaps: over, main_overlaps_box: ne(inter(main, box)),
             box: arr(box), main: arr(main), title: document.getElementById("rtitle").textContent,
             text: document.body.innerText };
  });
}

(async () => {
  const out = { posts: [] };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true,
                                             args: ["--host-resolver-rules=" + cfg.map] });
  try {
    const tv = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await tv.goto(cfg.tv);
    await tv.waitForSelector(".card.focused", { timeout: 10000 });
    await tv.waitForSelector("#remotebox:not([hidden]) canvas[data-qr=ok]", { timeout: 10000 });
    out.layout0 = await measure(tv);
    await tv.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-remote-unpaired.png") });
    // rangée pleine (catalogue réel plus grand) : mêmes cartes clonées dans la première rangée, même CSS
    await tv.evaluate(() => {
      const strip = document.querySelector(".strip"), card = strip.querySelector(".card:not(.focused)") ||
        strip.querySelector(".card");
      for (let i = 0; i < 8; i++) { const c = card.cloneNode(true); c.classList.remove("focused"); strip.appendChild(c); }
    });
    out.layout_full = await measure(tv);
    await tv.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-remote-fullrow.png") });

    const ctx = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
    const ph = await ctx.newPage();
    const reqs = [];
    ph.on("request", (r) => { if (r.method() === "POST") { reqs.push(r); } });
    const submit = async (sel) => { await Promise.all([ph.waitForNavigation(), ph.click(sel)]); };
    await ph.goto(cfg.pair_url);
    out.confirm = await ph.textContent("main");
    await submit("button.primary");                                       // « Oui, utiliser ce téléphone »
    out.pad_after_pair = (await ph.$("#pad")) !== null;
    // remote.js (amélioration progressive) marque la page s'il s'exécute : ici il ne doit pas s'exécuter
    out.js = (await ph.getAttribute("html", "data-remote-js")) !== null;
    const ck = (await ctx.cookies()).find((c) => c.name === "otv_remote");
    out.cookie = ck ? { name: ck.name, domain: ck.domain, httpOnly: ck.httpOnly, sameSite: ck.sameSite } : null;
    out.pad_after_cmds = [];
    for (const a of ["right", "down"]) {
      await submit("#pad button[value=" + a + "]");
      out.pad_after_cmds.push((await ph.$("#pad")) !== null);
    }
    await ph.screenshot({ path: path.join(cfg.shots, "phone-pad-nojs.png"), fullPage: true });
    // écran TV : un téléphone appairé, QR toujours là (titre « Ajouter un téléphone »), aucune carte dessous
    await tv.waitForFunction(() => document.getElementById("rtitle").textContent.indexOf("Ajouter") === 0,
                             null, { timeout: 8000 });
    await tv.waitForSelector("#remotebox:not([hidden]) canvas[data-qr=ok]", { timeout: 10000 });
    out.layout1 = await measure(tv);
    await tv.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-remote-paired.png") });
    // formulaire d'une origine opaque (data:) avec le QR courant : le navigateur envoie « Origin: null » -> 403
    const cur = await tv.evaluate(() => fetch("/api/tv/state", { headers: {
      "X-OpenTV-TV": document.querySelector('meta[name="opentv-key"]').content } }).then((r) => r.json()));
    const secret = cur.remote.url.split("?p=")[1];
    const evil = "<form method=post action='" + cfg.origin + "/r/pair'><input type=hidden name=p value='" + secret +
                 "'><button id=go>go</button></form>";
    await ph.goto("data:text/html," + encodeURIComponent(evil));
    await submit("#go");
    await ph.goto(cfg.origin + "/r");
    await submit(".forget button");                                       // « Oublier ce téléphone »
    out.forgotten = ((await ph.textContent("main")) || "").indexOf("appareil photo") >= 0;
    out.cookie_after_forget = (await ctx.cookies()).some((c) => c.name === "otv_remote");
    for (const r of reqs) {
      const h = await r.allHeaders();
      const resp = await r.response();
      out.posts.push({ path: new URL(r.url()).pathname, origin: h.origin || null, status: resp ? resp.status() : null,
                       opaque: (r.headers().referer || "").indexOf("data:") === 0 || h.origin === "null" });
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
