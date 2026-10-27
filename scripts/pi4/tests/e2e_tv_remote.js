/* Bout en bout (tests seulement, jamais installé) : vrai service OpenTV + vraie page /tv dans Chromium headless (temps
   réel) ; clavier HID (Playwright) puis téléphone (POST /r/cmd avec cookie + CSRF). Sortie : une ligne JSON.
   Puis Réglages (un seul ↑ depuis plusieurs rangées, OK, ← →, Retour) avec mesures 1920×1080 et captures réelles
   (si cfg.shots) : accueil, Réglages au focus, panneau Réglages, confirmation, fiche titre, catalogue vide.
   argv[2] = chemin du module playwright ; argv[3] = JSON {chromium, tv, port, host, cookie, csrf, shots?}.
   Le pavé est joint sur 127.0.0.1:<port> avec l'en-tête Host = nom mDNS de la boîte (seul Host admis avec l'adresse
   liée). */
"use strict";
const http = require("http");
const path = require("path");
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
        const g = document.getElementById("gear");
        return { focus: f ? f.textContent : null, panel: !document.getElementById("panel").hidden,
                 settings: g.className.indexOf("focused") >= 0,
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

    // Réglages : un seul ↑ depuis plusieurs rangées, puis OK / ← → / Retour (vraies touches, vraie page)
    const measure = () => page.evaluate(() => {
      const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
      const inter = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5;
      const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
      const VP = [0, 0, window.innerWidth, window.innerHeight];
      const gear = document.getElementById("gear"), box = document.getElementById("remotebox");
      const main = document.getElementById("main"), panel = document.getElementById("panel");
      const fe = document.querySelector(".card.focused") || (gear.className.indexOf("focused") >= 0 ? gear : null);
      const over = [];
      document.querySelectorAll(".card").forEach((c) => {
        const r = R(c);
        if (!box.hidden && inter(r, R(main)) && inter(r, VP) && inter(r, R(box))) { over.push(c.textContent.slice(0, 30)); }
      });
      const cs = fe ? getComputedStyle(fe) : null;
      // anneau complet (box-shadow × échelle) : aucun ancêtre overflow≠visible ne le rogne
      const ring_clip = [];
      if (fe) {
        let spread = 0;
        (cs.boxShadow || "").replace(/rgba?\([^)]*\)/g, "").split(",").forEach((s) => {
          const n = s.trim().split(/\s+/).map(parseFloat).filter((x) => !isNaN(x));
          if (n.length >= 4 && !/inset/.test(s)) { spread = Math.max(spread, Math.abs(n[0]) + n[2] + n[3]); }
        });
        const m = /matrix\(([-\d.e]+)/.exec(cs.transform || ""), d = spread * (m ? parseFloat(m[1]) : 1), r = R(fe);
        const ring = [r[0] - d, r[1] - d, r[2] + d, r[3] + d];
        for (let a = fe.parentElement; a && a !== document.documentElement; a = a.parentElement) {
          const ac = getComputedStyle(a);
          if ((ac.overflowX !== "visible" || ac.overflowY !== "visible") && !inside(ring, R(a))) { ring_clip.push(a.id || a.className); }
        }
        if (!inside(ring, VP)) { ring_clip.push("viewport"); }
      }
      const hidden_by = [];   // titre et boutons du panneau réellement au premier plan
      if (!panel.hidden) {
        [document.getElementById("ptitle")].concat(Array.from(document.querySelectorAll("#pbtns button"))).forEach((e) => {
          const r = R(e), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
          if (!h || !panel.contains(h)) { hidden_by.push((e.getAttribute("data-label") || e.textContent).slice(0, 30)); }
        });
      }
      const banner = document.getElementById("banner"), bvis = !banner.hidden && banner.getClientRects().length > 0;
      const rqr = document.getElementById("rqr"), qr_css = R(rqr);
      return { ring_clip, panel_hidden_by: hidden_by, banner_visible: bvis,
               banner_panel_overlap: bvis && !panel.hidden && inter(R(banner), R(panel)),
               qr_crisp: rqr.width === rqr.height && Math.abs(qr_css[2] - qr_css[0] - rqr.width) < 0.01 &&
                         getComputedStyle(rqr).imageRendering === "pixelated",
               hint: document.getElementById("hint").innerText, viewport: [window.innerWidth, window.innerHeight], gear: R(gear),
               gear_top: !gear.hidden && inside(R(gear), VP) && R(gear)[3] <= 150,
               focus_in_view: !!fe && inside(R(fe), VP) && (box.hidden || !inter(R(fe), R(box))),
               focus_ring: !!cs && cs.boxShadow !== "none", card_overlaps: over,
               panel_box_overlap: !panel.hidden && !box.hidden && inter(R(panel), R(box)),
               qr: document.getElementById("rqr").getAttribute("data-qr"), box_visible: !box.hidden,
               // identité v23 : photo plein écran embarquée, Réglages = cercle en haut à droite, feuille latérale verticale
               bg_full: (() => { const b = document.getElementById("bg"); const r = R(b);
                 return r.join() === "0,0,1920,1080" && getComputedStyle(b).backgroundImage.indexOf("data:image/jpeg") >= 0; })(),
               gear_circle: gear.offsetWidth === gear.offsetHeight && getComputedStyle(gear).borderTopLeftRadius === "50%" &&
                            R(gear)[2] >= 1780,
               sheet_vertical: panel.hidden || panel.className.indexOf("sheet") < 0 || Array.from(document.querySelectorAll("#pbtns button"))
                 .every((b, i, a) => i === 0 || R(b)[1] >= R(a[i - 1])[3] - 0.5),
               panel_cls: panel.hidden ? null : panel.className };
    });
    const shot = async (name) => { if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, name) }); } };
    const key = async (k, label) => { await page.keyboard.press(k); await sleep(350); const s = await snap(label); s.layout = await measure(); return s; };
    out.settings = [];
    await key("Home", "set:home");
    await shot("tv-1920x1080-home.png");
    out.settings.push(await key("ArrowUp", "set:up-row0"));
    await shot("tv-1920x1080-settings-focus.png");
    await key("ArrowDown", "set:down");
    await key("ArrowDown", "set:down2");
    await key("ArrowDown", "set:down3");
    out.settings.push(await key("ArrowUp", "set:up-row2"));
    out.settings.push(await key("Enter", "set:open"));
    out.settings_buttons = await page.evaluate(() => Array.prototype.map.call(document.querySelectorAll("#pbtns button"),
      (b) => b.getAttribute("data-label") || b.textContent));
    await shot("tv-1920x1080-settings-panel.png");
    out.settings.push(await key("ArrowRight", "set:right"));
    out.settings.push(await key("ArrowLeft", "set:left"));
    out.settings.push(await key("Enter", "set:confirm"));
    out.confirm_btn = await page.evaluate(() => document.querySelector("#pbtns .focused").textContent);
    await shot("tv-1920x1080-settings-confirm.png");
    out.settings.push(await key("Escape", "set:back"));
    out.settings.push(await key("Escape", "set:close"));
    out.settings.push(await key("Escape", "set:content"));
    await key("ArrowDown", "set:row1");
    out.settings.push(await key("Enter", "set:title"));
    await shot("tv-1920x1080-title-panel.png");
    out.settings.push(await key("ArrowUp", "set:up-from-title"));
    await key("Escape", "set:end");
    // Retour dans les rangées : une seule rangée à la fois (jamais de saut à la rangée 0), ↑ reste un geste
    out.climb = [];
    for (const [k, label] of [["Home", "climb:0"], ["ArrowDown", "climb:down1"], ["ArrowRight", "climb:right1"],
                              ["ArrowDown", "climb:down2"], ["Escape", "climb:back1"], ["Escape", "climb:back0"],
                              ["Escape", "climb:stay0"], ["ArrowDown", "climb:again1"], ["ArrowUp", "climb:up"],
                              ["Escape", "climb:ret"]]) {
      out.climb.push(await key(k, label));
    }
    out.rows_count = await page.evaluate(() => document.querySelectorAll(".row").length);
    out.setup_posts = await page.evaluate(() => performance.getEntriesByType("resource")
      .filter((e) => /\/api\/tv\/(setup|remote\/revoke)/.test(e.name)).length);

    // message d'assistant + nom d'appareil long + 3 assistants (état réel du service, enrichi) : le bandeau ne
    // recouvre jamais Réglages/confirmation/fiche, la barre du haut tient sur une ligne
    const msg = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await msg.route("**/api/tv/state", async (r) => {
      const resp = await r.fetch();
      const j = await resp.json();
      j.device = "Salon du sous-sol chez grand-maman Gertrude, télé principale";
      j.agents = ["muse", "hermit", "hermes"];
      j.message = { from: "muse", text: "Ce soir, je te propose un film calme. Bonne soirée ! ".repeat(2) };
      await r.fulfill({ response: resp, body: JSON.stringify(j) });
    });
    await msg.goto(cfg.tv);
    await msg.waitForSelector(".card.focused", { timeout: 10000 });
    await msg.waitForSelector("#banner:not([hidden])", { timeout: 10000 });
    await sleep(400);
    const header = () => msg.evaluate(() => {
      const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
      const lines = (e) => { const rg = document.createRange(); rg.selectNodeContents(e);
        const rs = Array.from(rg.getClientRects()).filter((x) => x.width > 0 && x.height > 0);
        return rs.some((a) => rs.some((b) => b.top >= a.bottom - 1)) ? 2 : 1; };
      const ids = { clock: "#clock", date: "#date", gear: "#gear", glabel: "#glabel", device: "#device", net: "#net",
                    agents: "#agents" };
      const kids = Object.keys(ids).map((k) => { const e = document.querySelector(ids[k]);
        return { id: k, r: R(e), lines: lines(e), clipped: e.scrollWidth > e.clientWidth + 1 }; });
      return { h: R(document.querySelector("header")), kids };
    });
    out.message = { header: await header() };
    const mkey = async (k, label) => { await msg.keyboard.press(k); await sleep(350);
      const l = await msg.evaluate(() => {
        const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
        const inter = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5;
        const panel = document.getElementById("panel"), banner = document.getElementById("banner");
        const bvis = !banner.hidden && banner.getClientRects().length > 0, hidden_by = [];
        if (!panel.hidden) {
          [document.getElementById("ptitle")].concat(Array.from(document.querySelectorAll("#pbtns button"))).forEach((e) => {
            const r = R(e), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
            if (!h || !panel.contains(h)) { hidden_by.push(e.textContent.slice(0, 30)); }
          });
        }
        const f = document.querySelector("#pbtns .focused");
        // bandeau : emplacement réservé dans le héros, jamais sur le titre, les puces ni un QR
        const bover = [];
        if (bvis) {
          ["htitle", "hkicker", "hmeta", "hchips", "remotebox", "rqr"].forEach((id) => {
            const e = document.getElementById(id);
            if (e && !e.hidden && e.getClientRects().length && inter(R(banner), R(e))) { bover.push(id); }
          });
        }
        const hero = document.getElementById("hero");
        const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
        return { s: "", panel: !panel.hidden, ptitle: document.getElementById("ptitle").textContent,
                 btn: f ? (f.getAttribute("data-label") || f.textContent) : null, banner_visible: bvis,
                 banner_panel_overlap: bvis && !panel.hidden && inter(R(banner), R(panel)), panel_hidden_by: hidden_by,
                 banner_overlaps: bover, banner_in_hero: bvis && inside(R(banner), R(hero)),
                 banner_lines_ok: !bvis || banner.clientHeight <= 2 * parseFloat(getComputedStyle(banner).lineHeight) + 0.5 };
      });
      l.s = label; return l; };
    out.message.steps = [await mkey("ArrowRight", "msg:home")];
    if (cfg.shots) { await msg.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-message-header.png") }); }
    for (const [k, label] of [["ArrowUp", "msg:top"], ["Enter", "msg:settings"], ["Enter", "msg:confirm"]]) {
      out.message.steps.push(await mkey(k, label));
    }
    if (cfg.shots) { await msg.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-message-confirm.png") }); }
    for (const [k, label] of [["Escape", "msg:back"], ["Escape", "msg:closed"], ["Escape", "msg:rows"],
                              ["Enter", "msg:title"], ["Escape", "msg:end"]]) {
      out.message.steps.push(await mkey(k, label));
    }
    out.message.posts = await msg.evaluate(() => performance.getEntriesByType("resource")
      .filter((e) => /\/api\/tv\/(setup|remote\/revoke|feedback)/.test(e.name)).length);

    // configuration (normale puis hotspot) avec un message en attente : jamais de bandeau, rien sur les QR ni le titre,
    // la ligne « Tout se règle… » entière dans l'écran (état réel du service, configuration simulée côté réponse)
    out.setup = [];
    for (const hot of [false, true]) {
      const sp = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
      await sp.route("**/api/tv/state", async (r) => {
        const resp = await r.fetch();
        const j = await resp.json();
        j.agents = ["muse", "hermit", "hermes"];
        j.message = { from: "muse", text: "Ce soir, je te propose un film calme. Bonne soirée ! ".repeat(2) };
        j.setup = { active: true, url: "http://opentv-salon.local:8081/setup?t=" + "x".repeat(43), message: "",
                    expires_in: 600, hotspot: hot ? { ssid: "OpenTV-AB12", state: "active",
                                                      wifi_qr: "WIFI:T:WPA;S:OpenTV-AB12;P:abcdefghijkm;;" } : null };
        await r.fulfill({ response: resp, body: JSON.stringify(j) });
      });
      await sp.goto(cfg.tv);
      await sp.waitForSelector("#setup:not([hidden]) #qr[data-qr=ok]", { timeout: 10000 });
      if (hot) { await sp.waitForSelector("#apqr[data-qr=ok]", { timeout: 10000 }); }
      await sleep(400);
      out.setup.push(await sp.evaluate((hotspot) => {
        const R = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
        const vis = (e) => !!e && !e.hidden && e.getClientRects().length > 0;
        const inter = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5;
        const inside = (a, b) => a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5;
        const VP = [0, 0, window.innerWidth, window.innerHeight], setup = document.getElementById("setup");
        const qrs = ["qr"].concat(hotspot ? ["apqr"] : []).map((id) => document.getElementById(id));
        const figs = qrs.map((q) => q.parentNode);
        const covered = [];
        qrs.forEach((q) => { const r = R(q);
          [[(r[0] + r[2]) / 2, (r[1] + r[3]) / 2], [r[0] + 12, r[1] + 12], [r[2] - 12, r[3] - 12]].forEach((p) => {
            const h = document.elementFromPoint(p[0], p[1]); if (h !== q) { covered.push(q.id); } }); });
        const texts = [document.querySelector("#setup .kicker"), document.querySelector("#setup h1"),
                       document.getElementById("steps"), document.getElementById("setupmsg"), document.getElementById("phoneonly")];
        const bad = [];
        texts.forEach((e) => { if (!inside(R(e), VP) || !inside(R(e), R(setup))) { bad.push(e.id || e.tagName); }
                               figs.forEach((f) => { if (inter(R(e), R(f))) { bad.push((e.id || e.tagName) + "/" + f.id); } }); });
        const po = document.getElementById("phoneonly"), pr = R(po);
        const ph = document.elementFromPoint((pr[0] + pr[2]) / 2, pr[3] - 6);
        return { hotspot, setup_visible: vis(setup), banner_visible: vis(document.getElementById("banner")),
                 text_has_message: document.body.innerText.indexOf("Message de") >= 0, qr_covered: covered, text_bad: bad,
                 phoneonly_bottom: pr[3], phoneonly_visible: !!ph && (ph === po || po.contains(ph)),
                 qr_in_view: qrs.every((q) => inside(R(q), VP)), figs_overlap: figs.length > 1 && inter(R(figs[0]), R(figs[1])) };
      }, hot));
      if (cfg.shots) { await sp.screenshot({ path: path.join(cfg.shots, hot ? "tv-1920x1080-setup-hotspot.png" : "tv-1920x1080-setup.png") }); }
      await sp.close();
    }

    // catalogue vide (réponse « Pour toi » sans rangée) : écran intentionnel, Réglages au focus
    const empty = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await empty.route("**/api/tv/foryou", (r) => r.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ rows: [], note: "", marks: {} }) }));
    await empty.goto(cfg.tv);
    await empty.waitForSelector("#gear.focused", { timeout: 10000 });
    await sleep(400);
    out.empty = await empty.evaluate(() => ({ text: document.body.innerText, cards: document.querySelectorAll(".card").length,
                                              settings: document.getElementById("gear").className.indexOf("focused") >= 0 }));
    await empty.keyboard.press("Enter");
    await sleep(300);
    out.empty.ptitle = await empty.evaluate(() => document.getElementById("panel").hidden ? null :
                                                  document.getElementById("ptitle").textContent);
    if (cfg.shots) { await empty.keyboard.press("Escape"); await sleep(300);
                     await empty.screenshot({ path: path.join(cfg.shots, "tv-1920x1080-empty.png") }); }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
