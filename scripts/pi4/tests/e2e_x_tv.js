/* Bout en bout X TV Mode (tests seulement, jamais installé) : Chromium COMPLET réel (extensions), extension telle
   qu'installée par install.sh dans une racine temporaire, vrai service OpenTV (page /tv réelle, route de partage
   réelle) et pages X SYNTHÉTIQUES (aucun accès réseau : https://x.com/* est servi par Playwright depuis des fixtures
   qui imitent les data-testid de X). Instrumentation de la page : chaque clic et chaque « J'aime » est compté.
   argv[2] = chemin du module playwright ; argv[3] = JSON {chrome, ext, ext_id, tv, port, ctl, shots}.
   Sortie : une ligne JSON {steps, film_api_calls}. */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STYLE = "<style>body{margin:0;font:16px sans-serif;background:#fff;color:#000}main{display:flex}" +
  "header{width:280px}[data-testid=sidebarColumn]{display:block;width:340px}[data-testid=primaryColumn]{width:600px}" +
  "article{display:block;min-height:420px;border:1px solid #ccc;margin:4px}</style>";
const INSTR = "<script>window.__clicks=0;window.__likes=0;" +
  "document.addEventListener('click',function(){window.__clicks++;},true);</script>";

function post(id, name, handle, n, textHtml) {
  return `<article data-testid="tweet" tabindex="0" data-otv-id="${id}">` +
    `<div data-testid="User-Name"><div><span>${name}</span></div><div><a href="/${handle}"><span>@${handle}</span></a>` +
    `<span>·</span><a href="/${handle}/status/${n}"><time datetime="2026-11-05T10:00:00Z">2 h</time></a></div></div>` +
    `<div data-testid="tweetText" lang="fr">${textHtml}</div>` +
    `<div role="group"><button data-testid="reply">Répondre</button><button data-testid="retweet">Repartager</button>` +
    `<button data-testid="like" onclick="window.__likes++">J'aime</button></div></article>`;
}
const P1 = post("p1", "Bob", "bob", 1, "Premier post.");
const P2 = post("p2", "Alice Martin", "alice_m", 2,
  "Deuxième post visible.\u2066<br>Lien : <a href=\"https://t.co/abc\">example.com/page</a> fin." +
  "<span style=\"display:none\">CACHÉ</span>");
const P3 = post("p3", "Chloé", "chloe", 3, "Troisième post.");
const P4 = post("p4", "Dan", "dan", 4, "Quatrième post.");
const FEED = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Accueil / X</title>${STYLE}</head><body>` +
  `<div id="react-root"><header role="banner">Navigation</header><main role="main"><div data-testid="primaryColumn">` +
  `<div id="tl">${P1}${P2}${P3}</div></div><div data-testid="sidebarColumn">Tendances</div></main></div>${INSTR}` +
  `<script>window.__addTweet=function(){var d=document.createElement('div');d.innerHTML=${JSON.stringify(P4)};` +
  `document.getElementById('tl').appendChild(d.firstChild);};</script></body></html>`;
const STATUS = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Post / X</title>${STYLE}</head><body>` +
  `<div id="react-root"><main role="main"><div data-testid="primaryColumn">${P2}</div></main>` +
  `<div role="dialog" aria-modal="true" id="viewer">Photo</div></div>${INSTR}</body></html>`;
const LOGIN = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Connexion / X</title>${STYLE}</head>` +
  `<body><div role="dialog" aria-modal="true"><h1>Connexion à X</h1><input id="user" name="text" ` +
  `autocomplete="username"><button id="next">Suivant</button></div><div data-testid="sidebarColumn" id="sb">Pied</div>` +
  `${INSTR}</body></html>`;

function ctl(p) {
  return new Promise((res, rej) => {
    const u = new URL(cfg.ctl + p);
    const r = http.request({ host: u.hostname, port: u.port, path: u.pathname, method: "POST" }, (x) => { x.resume(); x.on("end", res); });
    r.on("error", rej);
    r.end();
  });
}

(async () => {
  const out = { steps: {}, film_api_calls: [] };
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "otvxprof"));
  const ctx = await pw.chromium.launchPersistentContext(prof, {
    executablePath: cfg.chrome, headless: true, viewport: { width: 1920, height: 1080 },
    ignoreDefaultArgs: ["--disable-extensions"],
    args: ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext]
  });
  try {
    let sw = ctx.serviceWorkers()[0];
    if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
    if (sw.url().indexOf("chrome-extension://" + cfg.ext_id + "/") !== 0) { throw new Error("ID inattendu " + sw.url()); }
    await ctx.route(/^https:\/\/(x|twitter)\.com\//, (route) => {
      const u = new URL(route.request().url());
      const body = u.pathname.indexOf("/i/flow/login") === 0 ? LOGIN : /\/status\/\d+$/.test(u.pathname) ? STATUS : FEED;
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: body });
    });
    const page = ctx.pages()[0] || await ctx.newPage();
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.hostname === "127.0.0.1" && /^\/api\/tv\/(play|feedback)/.test(u.pathname) && r.method() === "POST") {
        out.film_api_calls.push(u.pathname);
      }
    });
    const session = () => sw.evaluate(() => chrome.storage.session.get(["tvkey", "tvport"]));
    const waitKey = async () => {
      const want = await page.evaluate(() => document.querySelector('meta[name="opentv-key"]').content);
      for (let i = 0; i < 50; i++) {
        const s = await session();
        if (s.tvkey === want && s.tvport === cfg.port) { return true; }
        await sleep(100);
      }
      return false;
    };
    const focusTitle = () => page.evaluate(() => {
      const f = document.querySelector(".card.focused .t");
      return f ? f.textContent : null;
    });
    const toTile = async () => {
      await page.waitForSelector(".card.focused", { timeout: 10000 });
      for (let i = 0; i < 14 && (await focusTitle()) !== "X"; i++) { await page.keyboard.press("ArrowDown"); await sleep(150); }
      return (await focusTitle()) === "X";
    };
    const waitUrl = async (pred, ms) => {
      for (let i = 0; i < (ms || 8000) / 100; i++) { if (pred(page.url())) { return page.url(); } await sleep(100); }
      return page.url();
    };
    const focusedPost = () => page.evaluate(() => {
      const f = document.querySelector("article.otvx-focus");
      return f ? f.getAttribute("data-otv-id") : null;
    });
    const toastText = async (prev) => {
      for (let i = 0; i < 60; i++) {
        const t = await page.evaluate(() => { const e = document.querySelector(".otvx-toast"); return e && !e.hidden ? e.textContent : ""; });
        if (t && t !== prev) { return t; }
        await sleep(100);
      }
      return "";
    };
    const clearToast = () => page.evaluate(() => { const e = document.querySelector(".otvx-toast"); if (e) { e.textContent = ""; } });
    const waitFeed = () => page.waitForFunction(() => document.documentElement.classList.contains("otvx-tv"), null, { timeout: 8000 });

    // 1) /tv : clé capturée par l'extension ; tuile X ; OK d'assistant refusé ; OK humain ouvre X
    await page.goto(cfg.tv);
    out.steps.tv = { key_captured: await waitKey() };
    const onTile = await toTile();
    const hero = await page.evaluate(() => ({ kicker: document.getElementById("hkicker").textContent,
                                              title: document.getElementById("htitle").textContent,
                                              panel: !document.getElementById("panel").hidden }));
    out.steps.tile = { focus: onTile ? "X" : await focusTitle(), kicker: hero.kicker.split(" · ").pop(), panel: hero.panel };
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-tile.png") }); }
    await sleep(2600);                                       // priorité humaine écoulée : la touche d'assistant est jugée
    await ctl("/agent-ok");
    await sleep(4500);                                       // poll 2 s + réclamation + résultat
    out.steps.tile.agent_ok_url = new URL(page.url()).pathname;
    await page.keyboard.press("Enter");
    out.steps.open = { url: await waitUrl((u) => u.indexOf("https://x.com/") === 0) };

    // 2) fil X : CSS TV, colonnes masquées, aide
    await waitFeed();
    out.steps.feed = await page.evaluate(() => ({
      tv_class: document.documentElement.classList.contains("otvx-tv"),
      sidebar: getComputedStyle(document.querySelector('[data-testid="sidebarColumn"]')).display,
      help: (document.querySelector(".otvx-help") || {}).textContent || ""
    }));

    // 3) ↑ ↓ et ajout dynamique
    const seq = [];
    for (const k of ["ArrowDown", "ArrowDown", "ArrowDown", "ArrowUp", "ArrowDown"]) {
      await page.keyboard.press(k); await sleep(120); seq.push(await focusedPost());
    }
    await page.evaluate(() => window.__addTweet());
    await sleep(500);
    await page.keyboard.press("ArrowDown"); await sleep(150); seq.push(await focusedPost());
    out.steps.nav = { seq: seq };

    // 4) ← : partage du post au focus (p2)
    await page.keyboard.press("ArrowUp"); await sleep(120);
    await page.keyboard.press("ArrowUp"); await sleep(120);
    const shared = await focusedPost();
    await page.keyboard.press("ArrowLeft");
    const t1 = await toastText("");
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-feed-shared.png") }); }
    out.steps.share = Object.assign({ toast: t1, post: shared },
      await page.evaluate(() => ({ clicks: window.__clicks, likes: window.__likes })));

    // 4 bis) touches synthétiques de la page (non fiables) : ni partage, ni Accueil, ni Retour
    await clearToast();
    await page.evaluate(() => {
      for (const k of ["ArrowLeft", "Home", "Escape", "BrowserBack"]) {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
      }
    });
    await sleep(1500);
    out.steps.synthetic = { url: page.url(), toast: await page.evaluate(() => {
      const e = document.querySelector(".otvx-toast"); return e && !e.hidden ? e.textContent : ""; }) };

    // 5) OK : permalien ; Retour : vue précédente ; Retour à la racine : /tv explicitement
    await page.keyboard.press("Enter");
    out.steps.enter = { url: await waitUrl((u) => /\/status\/\d+$/.test(u)) };
    await sleep(300);
    await page.keyboard.press("Escape");
    out.steps.back1 = { url: await waitUrl((u) => u.indexOf("https://x.com/home") === 0) };
    await waitFeed();
    await page.keyboard.press("Escape");
    out.steps.back_root = { url: await waitUrl((u) => u === cfg.tv) };
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await waitKey();

    // 6) Accueil depuis X : /tv
    await page.goto("https://x.com/home");
    await waitFeed();
    await page.keyboard.press("Home");
    out.steps.home = { url: await waitUrl((u) => u === cfg.tv) };

    // 7) connexion : page intacte, saisie native, Retour quitte d'abord le champ, puis revient à OpenTV
    await page.goto("https://x.com/i/flow/login");
    await sleep(700);
    const lg = await page.evaluate(() => ({
      tv_class: document.documentElement.classList.contains("otvx-tv"), help: !!document.querySelector(".otvx-help"),
      sidebar: getComputedStyle(document.getElementById("sb")).display }));
    await page.focus("#user");
    await page.keyboard.type("alice");
    await page.keyboard.press("Backspace");
    await page.keyboard.press("ArrowDown");
    lg.typed = await page.evaluate(() => document.getElementById("user").value);
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login.png") }); }
    await page.keyboard.press("Escape");
    await sleep(500);
    lg.url_after_escape = page.url();
    await page.keyboard.press("Escape");
    lg.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.login = lg;

    // 8) service redémarré (clé tournée) : refus honnête ; /tv recapture la nouvelle clé ; le partage repasse
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await waitKey();
    await page.goto("https://x.com/home");
    await waitFeed();
    await page.keyboard.press("ArrowDown"); await sleep(120);
    await ctl("/rotate");
    await clearToast();
    await page.keyboard.press("ArrowLeft");
    const stale = await toastText("");
    await page.keyboard.press("Home");
    await waitUrl((u) => u === cfg.tv);
    const rekey = await waitKey();
    await toTile();
    await page.keyboard.press("Enter");
    await waitUrl((u) => u.indexOf("https://x.com/") === 0);
    await waitFeed();
    await page.keyboard.press("ArrowDown"); await sleep(120);
    await page.keyboard.press("ArrowLeft");
    out.steps.rotate = { toast_stale: stale, rekey: rekey, toast_after: await toastText("") };
  } finally {
    await ctx.close();
    fs.rmSync(prof, { recursive: true, force: true });
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
