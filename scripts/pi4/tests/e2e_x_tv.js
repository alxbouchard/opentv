/* Bout en bout X TV Mode (tests seulement, jamais installé) : Chromium COMPLET réel (extensions), extension telle
   qu'installée par install.sh dans une racine temporaire, vrai service OpenTV (page /tv réelle, route de partage
   réelle) et pages X SYNTHÉTIQUES (aucun accès réseau : https://x.com/* est servi par Playwright depuis des fixtures
   qui imitent les data-testid de X). Instrumentation de la page : chaque clic et chaque « J'aime » est compté.
   Les pages synthétiques suivent prefers-color-scheme comme X (claires par défaut, sombres si l'appareil l'est) :
   le kiosque lance Chromium avec les arguments X de opentv-kiosk-browser (cfg.args : --force-dark-mode).
   argv[2] = chemin du module playwright ; argv[3] = JSON {chrome, chrome_light, ext, ext_id, tv, port, ctl, shots, args}.
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
  "article{display:block;min-height:420px;border:1px solid #ccc;margin:4px}" +
  "@media (prefers-color-scheme: dark){body{background:#000;color:#e7e9ea}}</style>";
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
  `<body><div role="dialog" aria-modal="true"><h1>Connexion à X</h1><input id="user" name="text" autofocus ` +
  `autocomplete="username"><button id="next">Suivant</button></div><div data-testid="sidebarColumn" id="sb">Pied</div>` +
  `${INSTR}</body></html>`;
// accueil déconnecté RÉALISTE (photo terrain : connexion X sombre, bouton « Continue with phone ») : modale de X,
// iframe Google (jamais un contrôle), boutons natifs, champ sans autofocus, rôles « button » non natifs (div), un
// bouton désactivé et un masqué (jamais focalisés). Chaque activation est comptée par id (window.__hits).
const LANDING = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>X. It’s what’s happening / X</title>` +
  `${STYLE}<style>button,[role=button],a,input{display:block;margin:12px auto;width:300px;height:40px}</style></head>` +
  `<body><div id="react-root"><div role="dialog" aria-modal="true" aria-labelledby="h"><h1 id="h">Sign in to X</h1>` +
  `<iframe title="Sign in with Google" srcdoc="<p>Google</p>" style="width:300px;height:40px"></iframe>` +
  `<button id="apple" type="button" onclick="__hits.push('apple')">Sign in with Apple</button>` +
  `<button id="phone" type="button" onclick="__hits.push('phone')"><span>Continue with phone</span></button>` +
  `<button id="gone" type="button" style="display:none" onclick="__hits.push('gone')">Hidden</button>` +
  `<div>or</div><label><span>Phone, email, or username</span>` +
  `<input id="user" name="text" autocomplete="username" type="text"></label>` +
  `<button id="off" type="button" disabled onclick="__hits.push('off')">Disabled</button>` +
  `<div id="next" role="button" tabindex="0" onclick="__hits.push('next')"><span>Next</span></div>` +
  `<div id="forgot" role="button" tabindex="0" onclick="__hits.push('forgot')"><span>Forgot password?</span></div>` +
  `<span>Don't have an account? <a id="signup" href="/i/flow/signup">Sign up</a></span></div></div>` +
  `<script>window.__hits=[];</script>${INSTR}</body></html>`;
// connexion bloquée en chargement (script de tête jamais servi) : le script du fil (document_idle) n'y tourne jamais
const STALL = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Connexion / X</title>${STYLE}` +
  `<script src="https://x.com/stall.js"></script></head><body>Connexion…</body></html>`;

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
    executablePath: cfg.chrome, headless: true, viewport: { width: 1920, height: 1080 }, colorScheme: null,   // kiosque : aucune émulation
    ignoreDefaultArgs: ["--disable-extensions"],
    args: ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext].concat(cfg.args || [])
  });
  try {
    let sw = ctx.serviceWorkers()[0];
    if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
    if (sw.url().indexOf("chrome-extension://" + cfg.ext_id + "/") !== 0) { throw new Error("ID inattendu " + sw.url()); }
    await ctx.route(/^https:\/\/(x|twitter)\.com\//, (route) => {
      const u = new URL(route.request().url());
      if (u.pathname === "/stall.js") { return undefined; }              // jamais servi : page en chargement
      const body = u.pathname === "/i/flow/stall" ? STALL : u.pathname === "/" ? LANDING :
        (u.pathname.indexOf("/i/flow/login") === 0 || u.pathname.indexOf("/account/") === 0) ? LOGIN :
        /\/status\/\d+$/.test(u.pathname) ? STATUS : FEED;
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: body });
    });
    const page = ctx.pages()[0] || await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    // touche RÉELLE (fiable) sans équivalent Playwright : Close (Exit CEC -> XF86Close), BrowserBack, BrowserHome
    const rawKey = async (key, code) => {
      await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: key, windowsVirtualKeyCode: code || 0 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: key, windowsVirtualKeyCode: code || 0 });
    };
    const look = () => page.evaluate(() => ({
      dark: matchMedia("(prefers-color-scheme: dark)").matches, bg: getComputedStyle(document.body).backgroundColor }));
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
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    out.steps.tv.initial = await page.evaluate(() => {   // accueil : X au focus, rangée de tête, tuile entière à l'écran
      const f = document.querySelector(".card.focused"), r = f.getBoundingClientRect();
      return { focus: f.querySelector(".t").textContent, row: document.querySelector("#rowsin .row h2").textContent,
               in_view: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth };
    });
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-home.png") }); }
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
    out.steps.feed_look = await look();
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

    // 7) connexion : page intacte et SOMBRE (prefers-color-scheme), saisie native ; Retour (même depuis le champ
    //    au focus) revient DIRECTEMENT à OpenTV ; Close (CEC), BrowserBack, Alt+←, BrowserHome aussi
    await page.goto("https://x.com/i/flow/login");
    await sleep(700);
    const lg = await page.evaluate(() => ({
      tv_class: document.documentElement.classList.contains("otvx-tv"), help: !!document.querySelector(".otvx-help"),
      sidebar: getComputedStyle(document.getElementById("sb")).display,
      autofocus: document.activeElement && document.activeElement.id }));
    Object.assign(lg, await look());
    await page.keyboard.type("alice");
    await page.keyboard.press("Backspace");
    await page.keyboard.press("ArrowLeft");                               // ← dans le champ : curseur natif
    await page.keyboard.press("Home");                                    // Début dans le champ : natif, on reste
    await sleep(300);
    lg.typed = await page.evaluate(() => document.getElementById("user").value);
    lg.url_after_text_keys = page.url();
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login.png") }); }
    await page.keyboard.press("Escape");
    lg.url_after_back = await waitUrl((u) => u === cfg.tv);
    const viaKey = async (url, press) => {
      await page.waitForSelector(".card.focused", { timeout: 10000 });
      await page.goto(url, { waitUntil: url.indexOf("/stall") > 0 ? "commit" : "load" });
      await sleep(700);
      const ready = await page.evaluate(() => document.readyState);
      await press();
      return { url: await waitUrl((u) => u === cfg.tv), ready: ready };
    };
    lg.close = await viaKey("https://x.com/i/flow/login", () => rawKey("Close"));
    lg.browser_back = await viaKey("https://x.com/account/access", () => rawKey("BrowserBack", 166));
    lg.alt_left = await viaKey("https://x.com/i/flow/login", () => page.keyboard.press("Alt+ArrowLeft"));
    lg.browser_home = await viaKey("https://x.com/i/flow/login", () => rawKey("BrowserHome", 172));
    lg.stalled = await viaKey("https://x.com/i/flow/stall", () => page.keyboard.press("Escape"));
    // post ouvert directement (aucune page x.com avant lui) : rien à fermer de détectable -> OpenTV
    lg.thread_direct = await viaKey("https://x.com/alice_m/status/2", () => page.keyboard.press("Escape"));
    out.steps.login = lg;

    // 7 bis) accueil déconnecté (photo terrain) : focus TV initial sur « Continue with phone », OK = UN clic exact sur
    //         ce bouton (activation native, geste réel) ; ↑ ↓ parcourent les contrôles visibles/activables dans
    //         l'ordre ; champ : saisie native, aucune valeur lue par l'extension ; rôle « button » non natif activé
    //         par OK réel seulement ; touche OK synthétique : rien
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/");
    await sleep(900);
    const ph = {};
    const st = () => page.evaluate(() => {
      const ae = document.activeElement, f = document.querySelector("[data-otvx-focus]");
      const cs = f ? getComputedStyle(f) : null, h = document.querySelector(".otvx-login-help");
      return { active: ae && ae.id || null, ring: f ? f.id : null, outline: cs ? cs.outlineStyle + " " + cs.outlineWidth : "",
               help: h ? h.textContent : "", hits: window.__hits.slice(), clicks: window.__clicks };
    });
    ph.initial = await st();
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login-phone.png") }); }
    await page.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    await sleep(150);
    ph.synthetic = await st();
    await page.keyboard.press("Enter");
    await sleep(200);
    ph.ok = await st();
    ph.url_after_ok = page.url();
    const order = [];
    await page.keyboard.press("ArrowUp"); await sleep(60); order.push((await st()).active);
    await page.keyboard.press("ArrowUp"); await sleep(60); order.push((await st()).active);   // bord : on reste
    for (let i = 0; i < 6; i++) { await page.keyboard.press("ArrowDown"); await sleep(60); order.push((await st()).active); }
    ph.seq = order;
    await page.keyboard.press("ArrowUp"); await sleep(60);   // signup -> forgot
    await page.keyboard.press("ArrowUp"); await sleep(60);   // forgot -> next
    await page.keyboard.press("ArrowUp"); await sleep(60);   // next -> user (champ)
    ph.field = (await st()).active;
    await page.keyboard.type("alice@example.com");
    await page.keyboard.press("ArrowLeft");                  // natif dans le champ
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Backspace");
    ph.after_typing = await st();
    ph.typed = await page.evaluate(() => document.getElementById("user").value);
    await page.keyboard.press("ArrowDown"); await sleep(60);   // champ d'une ligne : ↓ en sort vers Next
    await page.keyboard.press("ArrowDown"); await sleep(60);   // Forgot password? (div role=button)
    ph.on_forgot = (await st()).active;
    await page.keyboard.press("Enter"); await sleep(200);      // OK réel : repli sur CE contrôle seulement
    ph.forgot = await st();
    ph.url_end = page.url();
    await page.keyboard.press("Escape");
    ph.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.phone_login = ph;

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
  // témoin A/B : même Chromium en apparence claire (cfg.chrome_light), même page de connexion, SANS puis AVEC les
  // arguments X du kiosque -> claire puis sombre : le sombre vient du signal natif, pas de l'hôte ni d'un recoloriage
  out.steps.control = [];
  for (const args of [[], cfg.args || []]) {
    const bare = await pw.chromium.launch({ executablePath: cfg.chrome_light || cfg.chrome, headless: true, args: args });
    try {
      const c = await bare.newContext({ colorScheme: null });
      await c.route(/^https:\/\/x\.com\//, (r) => r.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: LOGIN }));
      const pg = await c.newPage();
      await pg.goto("https://x.com/i/flow/login");
      out.steps.control.push(await pg.evaluate(() => ({
        dark: matchMedia("(prefers-color-scheme: dark)").matches, bg: getComputedStyle(document.body).backgroundColor })));
    } finally {
      await bare.close();
    }
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
