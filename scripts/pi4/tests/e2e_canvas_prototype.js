/* Bout en bout (tests seulement, jamais installé) : laboratoire OpenTV Canvas dans un vrai Chromium 1920×1080.
   argv[2] = chemin du module playwright ; argv[3] = JSON {chromium, url, shots, acorn?}.
   Vérifie pour mist, cinema et canvas : géométrie v23 (horloge/date, Réglages 72 px, colonne QR 300 px / QR 232 px,
   pied de page, aucune carte ni panneau sur le QR, anneau de focus jamais rogné), plancher de taille de texte,
   animations limitées à transform/opacity sur ≤ 4 couches, aucun filtre ; puis pause (panneau, feuille Réglages,
   lecture, configuration, hors-ligne, page cachée, dégradé), mouvement réduit/coupé, refus des scènes hostiles,
   aperçu qui revient seul, confirmation littérale, historique de cinq, retour arrière et défaut immuable.
   Capture mist.png, cinema.png, canvas.png. Sortie : une ligne JSON {variants, failures}. */
"use strict";
const fs = require("fs");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const failures = [];
const check = (cond, msg, extra) => { if (!cond) { failures.push(msg + (extra !== undefined ? " " + JSON.stringify(extra) : "")); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (cfg.acorn) {   // ES5 au niveau source (Acorn), en plus de la recherche de jetons du test Python
  const acorn = require(cfg.acorn);
  acorn.parse(fs.readFileSync(path.join(__dirname, "..", "prototypes", "canvas", "canvas.js"), "utf8"), { ecmaVersion: 5 });
}

// ---- mesures dans la page
function measure() {
  const r = (el) => { const b = el.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height }; };
  const $ = (id) => document.getElementById(id);
  const inter = (a, b) => Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l)) * Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t));
  const tv = $("tv");
  const out = { clock: r($("clock")), date: r($("date")), gear: r($("gear")), glabel: r($("glabel")),
                remote: r($("remotebox")), qr: r($("rqr")), main: r($("main")), help: r($("help")), hero: r($("hero")),
                htext: r(document.querySelector(".htext")), qrAttr: [$("rqr").width, $("rqr").height] };
  // cartes : partie peinte (∩ #main) jamais sur la colonne QR
  out.cardQrOverlap = 0;
  out.cardHelpOverlap = 0;
  document.querySelectorAll(".card").forEach((c) => {
    const cr = r(c), m = out.main;
    const painted = { l: Math.max(cr.l, m.l), t: Math.max(cr.t, m.t), r: Math.min(cr.r, m.r), b: Math.min(cr.b, m.b) };
    if (painted.r > painted.l && painted.b > painted.t) {
      out.cardQrOverlap += inter(painted, out.remote);
    }
  });
  // anneau de focus : rect agrandi (spread × échelle) contenu dans chaque ancêtre qui rogne
  const f = document.querySelector(".card.focused");
  out.ringClipped = null;
  if (f) {
    const fr = r(f), cs = getComputedStyle(f);
    let spread = 0;
    (cs.boxShadow.match(/0px 0px 0px (\d+)px/g) || []).forEach((m) => { spread = Math.max(spread, +m.split(" ")[3].replace("px", "")); });
    const ring = { l: fr.l - spread * 1.08, t: fr.t - spread * 1.08, r: fr.r + spread * 1.08, b: fr.b + spread * 1.08 };
    out.ringClipped = [];
    for (let a = f.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).overflow !== "visible") {
        const ar = r(a);
        if (ring.l < ar.l - 0.5 || ring.t < ar.t - 0.5 || ring.r > ar.r + 0.5 || ring.b > ar.b + 0.5) {
          out.ringClipped.push(a.id || a.className);
        }
      }
    }
    out.spread = spread;
  }
  // plancher de texte : tout élément visible portant du texte dans le cadre TV
  let minFont = 1e9, minWho = null;
  tv.querySelectorAll("*").forEach((el) => {
    const own = Array.prototype.some.call(el.childNodes, (n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) { return; }
    const b = el.getBoundingClientRect();
    if (!b.width || !b.height || b.bottom <= 0 || b.top >= 1080 || b.left >= 1920) { return; }
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || +cs.opacity === 0) { return; }
    let hidden = false;
    for (let a = el; a && a !== tv; a = a.parentElement) { if (a.hidden || getComputedStyle(a).display === "none") { hidden = true; } }
    if (hidden) { return; }
    const fs = parseFloat(cs.fontSize);
    if (fs < minFont) { minFont = fs; minWho = el.className || el.tagName; }
  });
  out.minFont = minFont;
  out.minWho = minWho;
  // animations : couches en cours (cibles uniques), propriétés animées, transitions, filtres
  const running = new Set(), props = new Set();
  document.getAnimations().forEach((a) => {
    if (a.playState === "running" && a.effect && a.effect.target) {
      if (a.constructor.name === "CSSAnimation") { running.add(a.effect.target); }
      a.effect.getKeyframes().forEach((k) => Object.keys(k).forEach((p) => {
        if (["offset", "easing", "composite", "computedOffset"].indexOf(p) < 0) { props.add(p); }
      }));
    }
  });
  out.runningLayers = running.size;
  out.runningAnimated = Array.from(running).every((e) => e.classList.contains("anim"));
  out.animatedProps = Array.from(props);
  const kf = new Set();
  Array.from(document.styleSheets).forEach((s) => Array.from(s.cssRules).forEach((rule) => {
    if (rule.type === CSSRule.KEYFRAMES_RULE) {
      Array.from(rule.cssRules).forEach((k) => { for (let i = 0; i < k.style.length; i++) { kf.add(k.style[i]); } });
    }
  }));
  out.keyframeProps = Array.from(kf);
  const badTrans = [], filters = [];
  document.querySelectorAll("*").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.transitionDuration.split(",").some((d) => parseFloat(d) > 0)) {
      cs.transitionProperty.split(",").map((s) => s.trim()).forEach((p) => {
        if (["transform", "opacity"].indexOf(p) < 0) { badTrans.push((el.id || el.className) + ":" + p); }
      });
    }
    if (cs.filter !== "none" || (cs.backdropFilter && cs.backdropFilter !== "none")) { filters.push(el.id || el.className); }
  });
  out.badTransitions = badTrans.slice(0, 5);
  out.filters = filters;
  out.externalRequests = performance.getEntriesByType("resource").map((e) => e.name)
    .filter((n) => n.indexOf(location.origin + "/") !== 0);
  out.snap = window.OTVCanvas.snapshot();
  out.cls = tv.className;
  return out;
}

function geometry(m, v) {
  const ok = [];
  const t = (c, msg) => { ok.push(c); check(c, v + ": " + msg, c ? undefined : m); };
  t(m.clock.l >= 108 && m.clock.l <= 116 && m.clock.t >= 40 && m.clock.b <= 150, "horloge en haut à gauche");
  t(m.date.l >= 108 && m.date.t > m.clock.b && m.date.b <= 200, "date sous l'horloge");
  t(Math.round(m.gear.w) === 72 && Math.round(m.gear.h) === 72, "Réglages 72 px");
  t(Math.abs(m.gear.r - 1810) <= 1 && m.gear.t >= 40 && m.gear.b <= 140, "Réglages en haut à droite");
  t(m.glabel.r < m.gear.l, "libellé Réglages à gauche du cercle");
  t(Math.round(m.remote.w) === 300 && Math.abs(m.remote.r - 1810) <= 1, "colonne QR 300 px alignée à droite");
  t(Math.round(m.qr.w) === 232 && Math.round(m.qr.h) === 232 && m.qrAttr[0] === 232, "QR 232 px net");
  t(m.main.r + 40 <= m.remote.l + 0.5, "colonne principale ≥ 40 px avant le QR");
  t(m.cardQrOverlap === 0, "aucune carte peinte sur le QR");
  t(m.remote.t >= 200 && m.remote.b <= m.help.t, "QR entre le haut et le pied de page");
  t(m.help.b <= 1080 && m.help.t >= 1000, "pied de page en bas");
  t(m.hero.t >= 199 && m.hero.b <= 560, "héros sous l'horloge");
  t(m.htext.r <= m.remote.l, "texte du héros jamais sur le QR");
  t(m.ringClipped && m.ringClipped.length === 0, "anneau de focus jamais rogné");
  t(m.externalRequests.length === 0, "aucune requête externe");
  return ok.every(Boolean);
}

function animationRules(m, v) {
  check(m.runningLayers <= 4, v + ": ≤ 4 couches animées", m.runningLayers);
  check(m.runningAnimated, v + ": seules les couches .anim sont animées");
  check(m.animatedProps.every((p) => p === "transform" || p === "opacity"), v + ": propriétés animées", m.animatedProps);
  check(m.keyframeProps.every((p) => p === "transform" || p === "opacity"), v + ": @keyframes", m.keyframeProps);
  check(m.badTransitions.length === 0, v + ": transitions transform/opacity seulement", m.badTransitions);
  check(m.filters.length === 0, v + ": aucun filtre", m.filters);
}

(async () => {
  const out = { variants: {}, behaviour: {} };
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1,
                                           reducedMotion: "no-preference" });
    // aucune sortie réseau hors du serveur borné du laboratoire
    const origin = new URL(cfg.url).origin;
    const blocked = [];
    await ctx.route("**/*", (route) => {
      const u = route.request().url();
      if (u.indexOf(origin + "/") === 0) { return route.continue(); }
      blocked.push(u);
      return route.abort();
    });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") { errors.push(m.text()); } });
    await page.clock.install({ time: new Date("2026-10-30T20:41:00") });

    for (const v of ["mist", "cinema", "canvas"]) {
      await page.goto(cfg.url + "?variant=" + v);
      await page.waitForSelector("#tv[data-ready='1']");
      await page.keyboard.press("ArrowRight");          // focus sur une carte au milieu de la rangée
      await sleep(900);
      const m = await page.evaluate(measure);
      check(m.snap.visible.variant === v && m.cls.indexOf("v-" + v) >= 0, v + ": variante appliquée", m.cls);
      check(!m.snap.paused, v + ": non figé au repos", m.snap.pauseReasons);
      const geo = geometry(m, v);
      animationRules(m, v);
      check(m.runningLayers >= 1, v + ": fond vivant", m.runningLayers);
      check(m.minFont >= 17, v + ": texte ≥ 17 px", [m.minFont, m.minWho]);
      await sleep(1600);                                // la télécommande se tait avant la capture
      await page.screenshot({ path: path.join(cfg.shots, v + ".png"), clip: { x: 0, y: 0, width: 1920, height: 1080 } });
      out.variants[v] = { geometry_ok: geo, running_layers: m.runningLayers, min_font_px: m.minFont,
                          spread: m.spread, cls: m.cls };
    }

    // ---- comportement (variante recommandée)
    await page.goto(cfg.url);
    await page.waitForSelector("#tv[data-ready='1']");
    const snap = () => page.evaluate(() => window.OTVCanvas.snapshot());
    const layers = () => page.evaluate(() => {
      const s = new Set();
      document.getAnimations().forEach((a) => { if (a.playState === "running" && a.constructor.name === "CSSAnimation") { s.add(a.effect.target); } });
      return s.size;
    });
    const quiet = () => sleep(1600);
    // les animations CSS relancées peuvent mettre une image à repartir : attendre au plus 2 s le nombre attendu
    const waitLayers = async (n) => {
      let got = -1;
      for (let i = 0; i < 40; i++) { got = await layers(); if (got === n) { return true; } await sleep(50); }
      return false;
    };
    let s = await snap();
    check(s.visible.variant === "canvas" && s.revision === 0, "défaut immuable = canvas, révision 0", s);
    const base = await layers();
    check(base >= 1 && base <= 4, "couches animées au repos", base);

    // panneau titre : figé, géométrie hors QR, Retour reprend
    await page.keyboard.press("Enter");
    await sleep(300);
    let g = await page.evaluate(() => {
      const p = document.getElementById("panel").getBoundingClientRect(), q = document.getElementById("remotebox").getBoundingClientRect();
      return { pr: p.right, ql: q.left, vis: !document.getElementById("panel").hidden };
    });
    s = await snap();
    check(g.vis && s.panel === "card" && s.paused && s.pauseReasons.indexOf("panel") >= 0, "fiche : fond figé", s);
    check(g.pr + 40 <= g.ql + 0.5, "fiche jamais sur le QR", g);
    check(await layers() === 0, "fiche : aucune couche en cours");
    let r = await page.evaluate(() => window.OTVCanvas.preview({ accent: "ember" }));
    check(!r.ok && r.error === "ui_busy", "aperçu refusé pendant un panneau", r);
    await page.keyboard.press("Escape");
    await sleep(200);
    check(await waitLayers(base), "Retour : animation reprise");

    // ↑ depuis la rangée 0 → Réglages ; feuille latérale figée, hors QR
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("Enter");
    await sleep(300);
    g = await page.evaluate(() => {
      const p = document.getElementById("panel").getBoundingClientRect(), q = document.getElementById("remotebox").getBoundingClientRect();
      return { pr: p.right, ql: q.left, sheet: document.getElementById("panel").className };
    });
    s = await snap();
    check(s.panel === "sheet" && /sheet/.test(g.sheet) && s.paused, "feuille Réglages figée", s);
    check(g.pr + 40 <= g.ql + 0.5, "feuille jamais sur le QR", g);
    await page.keyboard.press("Escape");
    await page.keyboard.press("ArrowDown");
    await quiet();

    // états qui figent le fond
    for (const st of ["playback", "setup", "offline", "degraded"]) {
      await page.evaluate((x) => window.OTVCanvas.setState(x, true), st);
      s = await snap();
      check(s.paused && s.pauseReasons.indexOf(st) >= 0 && await layers() === 0, "figé : " + st, s.pauseReasons);
      await page.evaluate((x) => window.OTVCanvas.setState(x, false), st);
      check(await waitLayers(base), "reprise après " + st);
    }
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    s = await snap();
    check(s.pauseReasons.indexOf("hidden") >= 0 && await layers() === 0, "figé : page cachée", s.pauseReasons);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    check(await waitLayers(base), "reprise après page cachée");

    // mouvement réduit (préférence système) et coupé (scène)
    await page.emulateMedia({ reducedMotion: "reduce" });
    check(await waitLayers(0), "prefers-reduced-motion : aucune couche en cours");
    s = await snap();
    check(s.motion === "reduced", "prefers-reduced-motion : fond statique", s.motion);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    check(await waitLayers(base), "reprise sans mouvement réduit", base);
    s = await snap();
    check(s.motion === "full", "mouvement complet rétabli", [s.motion, s.pauseReasons]);

    // scènes hostiles : refus, rien ne change
    const before = await snap();
    const hostile = [{ wallpaper: "https://evil.example/x.jpg" }, { css: "body{display:none}" }, { accent: "red; x:url(a)" },
                     { row_order: ["top"] }, { row_order: ["tonight", "<img src=x onerror=alert(1)>"] },
                     { schema_version: 2 }, { variant: "../../ui/tv" }, {}, null, "accent:ember"];
    for (const h of hostile) {
      r = await page.evaluate((x) => window.OTVCanvas.preview(x), h);
      check(r && r.ok === false, "scène hostile refusée", h);
    }
    r = await page.evaluate(() => window.OTVCanvas.validate({ schema_version: 1, variant: "canvas", wallpaper: "aurore",
      motion: "full", accent: "ice", shade: "balanced", hero_focus: "title", row_order: ["tonight"], html: "<b>" }));
    check(!r.ok && r.error === "unknown_key", "clé inconnue refusée", r);
    s = await snap();
    check(s.visibleCanon === before.visibleCanon && s.publishedCanon === before.publishedCanon && s.revision === before.revision,
          "scène inchangée après refus");
    const cls = await page.evaluate(() => document.getElementById("tv").className);
    check(!/evil|url|img|red/.test(cls), "aucune valeur hostile dans les classes", cls);

    // aperçu : visible, non publié, revient seul (horloge simulée)
    r = await page.evaluate(() => window.OTVCanvas.preview({ accent: "ember", motion: "off" }, 5000));
    check(r.ok, "aperçu accepté au repos", r);
    s = await snap();
    check(s.visible.accent === "ember" && s.published.accent === "ice" && s.previewLeftMs > 0, "aperçu non publié", s);
    check(await layers() === 0 && s.motion === "off", "aperçu mouvement coupé : fond statique");
    check(await page.evaluate(() => !document.getElementById("pv").hidden && /revient dans/.test(document.getElementById("pvtext").textContent)),
          "pastille d'aperçu avec compte à rebours");
    const pvGeo = await page.evaluate(() => {
      const p = document.getElementById("pv").getBoundingClientRect(), c = document.getElementById("clock").getBoundingClientRect(),
        q = document.querySelector(".quick").getBoundingClientRect(), d = document.querySelector(".dateline").getBoundingClientRect();
      return p.left > Math.max(c.right, d.right) && p.right < q.left && p.bottom <= 200;
    });
    check(pvGeo, "pastille d'aperçu entre l'horloge et les Réglages");
    await page.clock.fastForward(2000);
    s = await snap();
    check(s.visible.accent === "ember", "aperçu encore actif à 2 s", s.visible);
    await page.clock.fastForward(3500);
    await sleep(100);
    s = await snap();
    check(s.visible.accent === "ice" && s.previewLeftMs === 0, "aperçu revenu seul à 5 s", s);
    check(await page.evaluate(() => document.getElementById("pv").hidden && /acc-ice/.test(document.getElementById("tv").className)),
          "rendu revenu à la scène publiée");
    check(await waitLayers(base), "animation reprise après l'aperçu");
    r = await page.evaluate(() => window.OTVCanvas.confirm(true));
    check(!r.ok, "confirmation impossible après expiration", r);

    // confirmation littérale
    await page.evaluate(() => window.OTVCanvas.preview({ accent: "ember" }, 20000));
    for (const bad of [false, 1, "true", "yes", null]) {
      r = await page.evaluate((x) => window.OTVCanvas.confirm(x), bad);
      check(!r.ok && r.error === "confirmation_required", "confirmation non littérale refusée", bad);
    }
    r = await page.evaluate(() => window.OTVCanvas.confirm(true));
    s = await snap();
    check(r.ok && s.published.accent === "ember" && s.revision === 1 && s.history === 1, "publication confirmée", s);

    // historique de cinq + retour arrière exact + défaut immuable
    const seq = ["jade", "violet", "ice", "ember", "jade", "violet"];
    for (const a of seq) {
      r = await page.evaluate((x) => window.OTVCanvas.preview({ accent: x }, 20000), a);
      check(r.ok, "aperçu " + a, r);
      await page.evaluate(() => window.OTVCanvas.confirm(true));
    }
    s = await snap();
    check(s.history === 5 && s.published.accent === "violet" && s.revision === 7, "historique borné à cinq", s);
    await page.evaluate(() => window.OTVCanvas.undo());
    s = await snap();
    check(s.published.accent === "jade", "retour arrière exact", s.published);
    for (let i = 0; i < 4; i++) { await page.evaluate(() => window.OTVCanvas.undo()); }
    s = await snap();
    check(s.history === 0 && s.visible.variant === "canvas" && s.visible.accent === "ice", "historique épuisé ⇒ défaut", s);
    r = await page.evaluate(() => window.OTVCanvas.undo());
    check(!r.ok && r.error === "nothing_to_rollback", "rien à annuler", r);
    await page.evaluate(() => window.OTVCanvas.select("cinema"));
    await page.evaluate(() => window.OTVCanvas.reset());
    s = await snap();
    check(s.publishedCanon === before.publishedCanon && s.history === 0, "réinitialisation au défaut immuable", s);

    // navigation récente : un changement attend que la télécommande se taise
    await page.keyboard.press("ArrowRight");
    r = await page.evaluate(() => window.OTVCanvas.preview({ accent: "jade" }));
    check(!r.ok && r.error === "ui_busy" && r.key === "navigating", "aperçu différé pendant la navigation", r);
    await page.keyboard.press("ArrowLeft");

    // ordre des rangées : la mémoire spatiale garde la même rangée et le même titre
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowRight");
    await quiet();
    const keep = await snap();
    r = await page.evaluate(() => window.OTVCanvas.preview({ row_order: ["top", "tonight", "new"] }, 20000));
    s = await snap();
    check(r.ok && s.row === keep.row && s.focus === keep.focus, "ordre des rangées : focus conservé", [keep.row, keep.focus, s.row, s.focus]);
    await page.evaluate(() => window.OTVCanvas.cancel());

    // amorçage hostile par l'adresse du laboratoire
    await page.goto(cfg.url + "?variant=" + encodeURIComponent("https://evil.example") + "&motion=" + encodeURIComponent("<b>"));
    await page.waitForSelector("#tv[data-ready='1']");
    s = await snap();
    check(s.bootError && s.publishedCanon === before.publishedCanon && s.revision === 0, "variante/mouvement hostiles ignorés", s);

    check(errors.length === 0, "aucune erreur de page", errors);
    check(blocked.length === 0, "aucune requête hors laboratoire", blocked);
    out.behaviour = { base_layers: base };
  } finally {
    await browser.close();
  }
  out.failures = failures;
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(2); });
