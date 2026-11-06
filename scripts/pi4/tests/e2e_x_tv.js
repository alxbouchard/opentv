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
// Accueil déconnecté RÉEL de x.com, MINIMISÉ (SSR de https://x.com/ relevé le 2026-11-06, en et ?lang=fr ;
// sha256 54858e48…99730 / 7ba854cd…73268) : ordre, balises, href, id et libellés de X conservés ; classes, SVG, styles
// et pied de page réduits. « Continue with phone » = INSCRIPTION (mode=signup, premier lien), Google/Apple = mode=login,
// formulaire identifiant + « Continue », puis « Already have an account? » + lien générique « Sign in » (mode=login).
// Seules simulations (X rend le reste côté client, rien dans le SSR) : l'autofocus du champ vu sur la photo terrain
// (sauf ?nofocus) et la fenêtre de connexion DANS la page (clic intercepté : preventDefault + pushState, fenêtre
// modale ajoutée 300 ms plus tard — 1500 ms avec ?slow, options lues AU CHARGEMENT (avant pushState) — après un indicateur de chargement, champ autofocusé) ;
// ?oldpk : leurre SYNTHÉTIQUE, une passkey déjà présente avant le clic. Chaque activation est comptée par id (__hits).
const ONB = "/i/jf/onboarding/web";
const SIGNIN_A = `<a id="signin" href="${ONB}?mode=login&amp;redirect_after_login=%2F">Sign in</a>`;
// fenêtre de connexion de X (rendue côté client, SYNTHÉTIQUE) : leurres jamais focalisés (iframe Google, passkey sous
// parent opacity:0, lien hors écran, masqué, désactivé), passkey native, rôles « button » non natifs, inscription
const LOGIN_WINDOW = `<div id="layers"><div role="dialog" aria-modal="true" id="logindlg" style="position:fixed;top:0;` +
  `left:560px;width:800px;height:1060px;background:#000;color:#fff;z-index:5"><h1>Sign in to X</h1>` +
  `<iframe title="Sign in with Google" srcdoc="<p>Google</p>" style="width:300px;height:40px"></iframe>` +
  `<div style="opacity:0"><button id="ghost" type="button" onclick="__hits.push('ghost')">Continue with passkey</button></div>` +
  `<a id="skip" href="#main" style="position:absolute;left:-9999px;top:0">Skip to content</a>` +
  `<input id="dlguser" name="text" autocomplete="username webauthn" type="text">` +
  `<button id="passkey" type="button" onclick="__hits.push('passkey')"><span>Continue with passkey</span></button>` +
  `<button id="gone" type="button" style="display:none" onclick="__hits.push('gone')">Hidden</button>` +
  `<button id="off" type="button" disabled onclick="__hits.push('off')">Disabled</button>` +
  `<div id="next" role="button" tabindex="0" onclick="__hits.push('next')"><span>Next</span></div>` +
  `<div id="forgot" role="button" tabindex="0" onclick="__hits.push('forgot')"><span>Forgot password?</span></div>` +
  `<span>Don't have an account? <a id="dlgsignup" href="${ONB}?mode=signup&amp;redirect_after_login=%2F">Sign up</a>` +
  `</span></div></div>`;
// ?negpk : leurres SYNTHÉTIQUES de sens négatif (sans / au lieu de / autre / différente, EN + FR) et passkey libre,
// visibles et NOUVEAUX, placés AVANT la passkey canonique de la fenêtre : seule la canonique reçoit le focus
const NEG_DECOYS = [["neg1", "Continue without passkey"], ["neg2", "Sign in without a passkey"],
  ["neg3", "Use password instead of passkey"], ["neg4", "Continue sans clé d'accès"],
  ["neg5", "Utiliser le mot de passe au lieu de la clé d'accès"], ["neg6", "Use a different passkey"],
  ["neg7", "Utiliser une autre clé d'accès"], ["loose", "Use passkey on this device"]]
  .map(([id, t]) => `<button id="${id}" type="button" onclick="__hits.push('${id}')">${t}</button>`).join("");
const LOGIN_WINDOW_NEG = LOGIN_WINDOW.replace('<button id="passkey"', NEG_DECOYS + '<button id="passkey"');
const LANDING_BODY = `<div id="react-root"><div><h1>Happening now.</h1><div>` +
  `<a id="phone" href="${ONB}?mode=signup&amp;redirect_after_login=%2F"><span><svg width="20" height="20"></svg></span>` +
  `<span>Continue with phone</span></a><div><div><a id="google" href="${ONB}?mode=login&amp;redirect_after_login=%2F">` +
  `<span><svg width="20" height="20"></svg></span><span>Continue with Google</span></a></div></div>` +
  `<a id="apple" href="${ONB}?mode=login&amp;redirect_after_login=%2F"><span><svg width="20" height="20"></svg></span>` +
  `<span>Continue with Apple</span></a><div><span></span> or <span></span></div>` +
  `<form action="${ONB}" method="get"><input type="hidden" name="mode" value="login">` +
  `<label for="landing-identifier" aria-label="Email or username"><span><span>Email or username</span>` +
  `<input id="landing-identifier" type="text" autocomplete="username" autocapitalize="none" autocorrect="off" ` +
  `spellcheck="false" required="" placeholder=" " name="username"></span></label>` +
  `<button id="continue" type="submit"><span>Continue</span></button></form><p>By continuing, you agree to our ` +
  `<a id="tos" href="https://x.com/tos" target="_blank" rel="noopener noreferrer nofollow">Terms of Service</a>.</p>` +
  `</div></div><div><h1>See what’s happening</h1><p>Already have an account?</p>${SIGNIN_A}</div>` +
  `<footer><nav><a id="about" target="_blank" rel="noopener noreferrer nofollow" href="https://about.x.com">About</a>` +
  `</nav></footer></div>`;
const LANDING_JS = `<script>window.__hits=[];window.__pkFocus=0;window.__doc=String(Math.random());(function(){var Q=location.search;` +
  `var W=/negpk/.test(Q)?${JSON.stringify(LOGIN_WINDOW_NEG)}:${JSON.stringify(LOGIN_WINDOW)};` +
  `window.__f={};document.addEventListener('focusin',function(e){var i=e.target&&e.target.id;if(i){__f[i]=(__f[i]||0)+1;}},true);` +
  `document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href^="${ONB}"]');` +
  `if(!a){return;}e.preventDefault();__hits.push(a.id);var u=new URL(a.href);` +
  `if(u.searchParams.get('mode')!=='login'){return;}` +
  `if(location.pathname!==u.pathname){history.pushState({otv:1},'',u.pathname+u.search);}` +
  `if(!document.getElementById('spinner')){document.body.insertAdjacentHTML('beforeend','<div id="spinner" role="progressbar">Loading…</div>');}` +
  `setTimeout(function(){var sp=document.getElementById('spinner');if(sp){sp.remove();}` +
  `if(/hidpk/.test(Q)){if(!document.getElementById('newpk')){document.getElementById('hidpk').style.display='block';` +
  `document.body.insertAdjacentHTML('beforeend','<div id="inlinepk"><button id="newpk" type="button">Continue with passkey</button></div>');}return;}` +
  `if(document.getElementById('logindlg')){return;}document.body.insertAdjacentHTML('beforeend',W);` +
  `document.getElementById('passkey').addEventListener('focus',function(){__pkFocus++;});` +
  `document.getElementById('dlguser').focus();},/slow/.test(Q)?1500:300);});` +
  `addEventListener('popstate',function(){var d=document.getElementById('layers');if(d){d.remove();}});` +
  `if(/oldpk/.test(Q)){document.getElementById('signin').insertAdjacentHTML('afterend',` +
  `'<button id="oldpk" type="button">Continue with passkey</button>');window.__oldFocus=0;` +
  `document.getElementById('oldpk').addEventListener('focus',function(){__oldFocus++;});}` +
  `if(/hidpk/.test(Q)){document.getElementById('signin').insertAdjacentHTML('afterend',` +
  `'<button id="hidpk" type="button" style="display:none">Continue with passkey</button>');}` +
  `if(/phonefocus/.test(Q)){document.getElementById('phone').focus();window.__phonefocused=true;}` +
  `else if(!/nofocus/.test(Q)){document.getElementById('landing-identifier').focus();window.__autofocused=true;}` +
  `})();</script>`;
const LANDING = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>X. It’s what’s happening / X</title>` +
  `${STYLE}<style>a,button,[role=button],input{display:block;margin:10px auto;width:300px;height:40px}</style></head>` +
  `<body>${LANDING_BODY}${LANDING_JS}${INSTR}</body></html>`;
// même accueil réel SANS l'entrée générique « Sign in » : rien n'est préféré (fail closed), ↓ jamais l'inscription
const LANDING_NOSIGNIN = LANDING.replace(SIGNIN_A, "");
// 0.7.9 — étape identifiant avec passkey native de X (libellés du bundle web : « Continue with passkey ») : aucune
// entrée « Log in with username or email » ici ; « Continue with phone » (inscription) présent mais jamais préféré
const PASSKEY_LOGIN = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Log in to X / X</title>` +
  `${STYLE}<style>button,[role=button],input{display:block;margin:12px auto;width:300px;height:40px}</style></head>` +
  `<body><div role="dialog" aria-modal="true"><h1>Sign in to X</h1>` +
  `<button id="phone" type="button" onclick="__hits.push('phone')">Continue with phone</button>` +
  `<input id="user" name="text" autofocus autocomplete="username webauthn" type="text">` +
  `<button id="passkey" type="button" onclick="__hits.push('passkey')"><span>Continue with passkey</span></button>` +
  `<div id="next" role="button" tabindex="0" onclick="__hits.push('next')"><span>Next</span></div></div>` +
  `<script>window.__hits=[];</script>${INSTR}</body></html>`;
// 0.7.9 — seule l'inscription par téléphone est proposée : rien n'est préféré (fail closed)
const PHONE_ONLY = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>X</title>` +
  `${STYLE}<style>button,[role=button],input{display:block;margin:12px auto;width:300px;height:40px}</style></head>` +
  `<body><div role="dialog" aria-modal="true"><h1>Sign in to X</h1>` +
  `<button id="phone" type="button" onclick="__hits.push('phone')">Continue with phone</button>` +
  `<input id="user" name="text" autofocus autocomplete="username" type="text">` +
  `<div id="next" role="button" tabindex="0" onclick="__hits.push('next')"><span>Next</span></div></div>` +
  `<script>window.__hits=[];</script>${INSTR}</body></html>`;
// vérification de compte (SYNTHÉTIQUE) : options légitimes par téléphone atteignables à la main ; l'inscription exacte
// connue (« Continue with phone ») et le href d'inscription restent retirés de ↑ ↓
const VERIFY = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Verify / X</title>` +
  `${STYLE}<style>a,button,[role=button],input{display:block;margin:12px auto;width:300px;height:40px}</style></head>` +
  `<body><div role="dialog" aria-modal="true"><h1>Verify your identity</h1>` +
  `<button id="usephone" type="button" onclick="__hits.push('usephone')">Use your phone</button>` +
  `<button id="phoneverify" type="button" onclick="__hits.push('phoneverify')">Use phone to verify</button>` +
  `<button id="vfr" type="button" onclick="__hits.push('vfr')">Utiliser votre téléphone pour vérifier</button>` +
  `<button id="phonesignup" type="button" onclick="__hits.push('phonesignup')">Continue with phone</button>` +
  `<a id="signuplink" href="${ONB}?mode=signup" onclick="__hits.push('signuplink');return false">Get started</a></div>` +
  `<script>window.__hits=[];</script>${INSTR}</body></html>`;
// 0.7.8 — fenêtre modale VISIBLE de X au-dessus du fil (colonne principale PRÉSENTE) : invite de notifications réaliste
// (calque fixe, bouton natif <button role=button> comme X + rôle « button » non natif qui ferme la fenêtre) ; le fil derrière ne doit jamais bouger
const DIALOG = `<div id="layers"><div role="dialog" aria-modal="true" id="dlg" style="position:fixed;top:220px;` +
  `left:610px;width:700px;height:360px;background:#202327;color:#fff;z-index:10;padding:20px">` +
  `<h2>Turn on notifications</h2><button id="allow" type="button" role="button" onclick="__hits.push('allow')">Allow</button>` +
  `<div id="notnow" role="button" tabindex="0" style="display:block;width:300px;height:40px" ` +
  `onclick="__hits.push('notnow');document.getElementById('dlg').remove()"><span>Not now</span></div></div></div>`;
const FEED_DIALOG = FEED.replace("</body>", `${DIALOG}<script>window.__hits=[];</script></body>`);
// 0.7.8 — X autofocuse sa zone de rédaction (éditable multiligne) dans le fil : ↑ ↓ ne doivent pas être morts
const FEED_COMPOSE = FEED.replace('<div id="tl">', '<div id="compose" contenteditable="true" role="textbox" ' +
  'style="min-height:60px;border:1px solid #888"></div><div id="tl">')
  .replace("</body>", "<script>document.getElementById('compose').focus();</script></body>");
// 0.7.8 — CSP stricte (style-src par nonce, sans 'unsafe-inline') : un <style> injecté serait bloqué ; la feuille de
// l'extension (nav.css) ne l'est pas. Chaque violation CSP de la page est comptée (window.__csp).
const CSP = "style-src 'nonce-n0nce'; script-src 'unsafe-inline'";
// (attributs style de la fixture convertis en classes d'une feuille à nonce : la page elle-même ne viole rien)
const CSP_RULES = [];
const LANDING_CSP = LANDING.replace(/ style="([^"]*)"/g, (m, css) => {
  CSP_RULES.push(`.otvcsp${CSP_RULES.length}{${css}}`);
  return ` class="otvcsp${CSP_RULES.length - 1}"`;
}).split("<style>").join('<style nonce="n0nce">')
  .replace("</head>", `<style nonce="n0nce">${CSP_RULES.join("")}</style></head>`)
  .replace("<body>", "<body><script>window.__csp=0;document.addEventListener('securitypolicyviolation'," +
    "function(){window.__csp++;});</script>");
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
      if (u.pathname === "/csp/landing") {
        return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: LANDING_CSP,
                               headers: { "Content-Security-Policy": CSP } });
      }
      const body = u.pathname === "/i/flow/stall" ? STALL :
        u.pathname === "/" ? (u.searchParams.has("nosignin") ? LANDING_NOSIGNIN : LANDING) :
        u.pathname === "/i/flow/passkey" ? PASSKEY_LOGIN : u.pathname === "/i/flow/phoneonly" ? PHONE_ONLY :
        u.pathname === "/i/flow/verify" ? VERIFY :
        u.pathname === "/settings/dialog" ? FEED_DIALOG : u.pathname === "/compose/home" ? FEED_COMPOSE :
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

    // 7 bis) accueil déconnecté RÉEL (minimisé) : focus TV initial sur le lien générique « Sign in » (jamais
    //         « Continue with phone » : inscription ; jamais Google/Apple) ; OK synthétique : rien ; ↑ ↓ : contrôles
    //         visibles dans l'ordre, SANS l'inscription ; OK RÉEL : UN clic natif sur « Sign in », X ouvre sa fenêtre de
    //         connexion DANS la page (aucun page.goto) et la passkey reçoit le focus UNE fois (réarmement) ; ensuite
    //         saisie native, focus jamais volé, aucun clic automatique
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/");
    const ph = {};
    ph.first_ms = await page.evaluate(() => new Promise((res) => {
      const t0 = performance.now();
      (function poll() {
        const ae = document.activeElement;
        if (ae && ae.id === "signin") { res(Math.round(performance.now() - t0)); return; }
        if (performance.now() - t0 > 3000) { res(-1); return; }
        setTimeout(poll, 20);
      })();
    }));
    ph.autofocus_seen = await page.evaluate(() => window.__autofocused === true);
    await sleep(600);
    const st = () => page.evaluate(() => {
      const ae = document.activeElement, f = document.querySelector("[data-otvx-focus]");
      const cs = f ? getComputedStyle(f) : null, h = document.querySelector(".otvx-login-help");
      return { active: ae && ae.id || null, ring: f ? f.id : null, outline: cs ? cs.outlineStyle + " " + cs.outlineWidth : "",
               help: h ? h.textContent : "", hits: window.__hits.slice(), clicks: window.__clicks,
               pk_focus: window.__pkFocus, doc: window.__doc, dialog: !!document.getElementById("logindlg"),
               url: location.href };
    });
    ph.initial = await st();
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login-landing.png") }); }
    await page.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    await sleep(400);
    ph.synthetic = await st();
    const order = [];
    for (let i = 0; i < 7; i++) { await page.keyboard.press("ArrowUp"); await sleep(60); order.push((await st()).active); }   // bord : on reste
    for (let i = 0; i < 8; i++) { await page.keyboard.press("ArrowDown"); await sleep(60); order.push((await st()).active); }
    ph.seq = order;
    await page.keyboard.press("ArrowUp"); await sleep(60);   // about -> signin
    ph.before_ok = await st();
    await page.keyboard.press("Enter");                       // OK RÉEL : activation native du lien (geste humain)
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    ph.ok = await st();
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login-window-passkey.png") }); }
    // X refait un rendu SANS geste : il remet son autofocus au champ et ajoute une AUTRE passkey — le réarmement est
    // consommé, le focus n'est plus jamais déplacé seul
    await page.evaluate(() => {
      document.getElementById("dlguser").focus();
      document.getElementById("logindlg").insertAdjacentHTML("beforeend",
        '<button id="passkey2" type="button" style="display:block;width:300px;height:40px">Use passkey</button>');
    });
    await sleep(700);
    ph.rerender = await st();
    await page.keyboard.press("ArrowUp"); await sleep(60);   // champ identifiant (premier contrôle) : on y reste
    ph.field = (await st()).active;
    await page.keyboard.type("alice@example.com");
    await page.keyboard.press("ArrowLeft");                  // natif dans le champ
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("Backspace");
    // nouveau rendu de X après une saisie réelle, avec une passkey de plus : le focus n'est JAMAIS repris au champ
    await page.evaluate(() => document.getElementById("logindlg").insertAdjacentHTML("beforeend",
      '<button id="passkey3" type="button" style="display:block;width:300px;height:40px">Continue with passkey</button>'));
    await sleep(700);
    ph.after_typing = await st();
    ph.typed = await page.evaluate(() => document.getElementById("dlguser").value);
    const dseq = [];
    for (let i = 0; i < 7; i++) { await page.keyboard.press("ArrowDown"); await sleep(60); dseq.push((await st()).active); }
    ph.dialog_seq = dseq;
    for (let i = 0; i < 2; i++) { await page.keyboard.press("ArrowUp"); await sleep(60); }   // passkey3 -> passkey2 -> forgot
    ph.on_forgot = (await st()).active;
    await page.keyboard.press("Enter"); await sleep(200);      // OK réel : repli sur CE contrôle seulement
    ph.forgot = await st();
    await page.keyboard.press("Escape");                       // Retour : inchangé (page précédente de X : l'accueil)
    ph.url_after_back1 = await waitUrl((u) => u === "https://x.com/" || u === cfg.tv);
    await sleep(300);
    await page.keyboard.press("Escape");
    ph.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.phone_login = ph;

    // 7 bis-r) activation SYNTHÉTIQUE de « Sign in » (script de page : el.click()) : X ouvre quand même sa fenêtre,
    //          mais rien n'est réarmé — la passkey n'est jamais focalisée, l'autofocus de X reste
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const fk = {};
    await page.evaluate(() => document.getElementById("signin").click());
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    fk.after = await st();
    await page.evaluate(() => document.getElementById("signin").dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })));
    await sleep(700);
    fk.after_event = await st();
    await page.keyboard.press("Escape");
    await waitUrl((u) => u === "https://x.com/" || u === cfg.tv);
    await sleep(300);
    if (page.url() !== cfg.tv) { await page.keyboard.press("Escape"); }
    fk.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.fake_activation = fk;

    // 7 bis-c) réarmement annulé : OK réel sur « Sign in », puis une touche réelle AVANT que la fenêtre (lente)
    //          n'apparaisse — la passkey n'est jamais focalisée ensuite (l'humain a repris la main)
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?slow=1");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const cn = {};
    await page.keyboard.press("Enter");
    await page.keyboard.press("ArrowDown");
    cn.before = await st();
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    cn.after = await st();
    await page.keyboard.press("Escape");
    await waitUrl((u) => u === "https://x.com/?slow=1" || u === cfg.tv);
    await sleep(300);
    if (page.url() !== cfg.tv) { await page.keyboard.press("Escape"); }
    cn.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.cancelled = cn;

    // 7 bis-d) passkey DÉJÀ présente avant le clic (leurre) + indicateur de chargement (mutation) : seule la passkey
    //          NOUVELLE de la fenêtre ouverte par X reçoit le focus, jamais l'ancienne
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?oldpk=1");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const op = {};
    await page.keyboard.press("Enter");
    await sleep(150);
    op.loading = Object.assign(await st(), { old_focus: await page.evaluate(() => window.__oldFocus) });
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    op.after = Object.assign(await st(), { old_focus: await page.evaluate(() => window.__oldFocus) });
    await page.keyboard.press("Escape");
    await waitUrl((u) => u === "https://x.com/?oldpk=1" || u === cfg.tv);
    await sleep(300);
    if (page.url() !== cfg.tv) { await page.keyboard.press("Escape"); }
    op.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.old_passkey = op;

    const backToTv = async (here) => {
      await page.keyboard.press("Escape");
      await waitUrl((u) => u === here || u === cfg.tv);
      await sleep(300);
      if (page.url() !== cfg.tv) { await page.keyboard.press("Escape"); }
      return waitUrl((u) => u === cfg.tv);
    };
    const focusCounts = () => page.evaluate(() => Object.assign({}, window.__f || {}));
    // 7 bis-e) leurres négatifs (sans / au lieu de / autre / différente) et passkey libre AVANT la canonique : le
    //          réarmement choisit la MEILLEURE passkey sûre, jamais la première du document
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?negpk=1");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const ng = {};
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    ng.after = Object.assign(await st(), { f: await focusCounts() });
    ng.url_after_back = await backToTv("https://x.com/?negpk=1");
    out.steps.negative_passkeys = ng;

    // 7 bis-f) passkey DÉJÀ dans le document au clic mais MASQUÉE, révélée par X avec une passkey réellement nouvelle
    //          (fenêtre en ligne, sans modale) : seule la nouvelle reçoit le focus
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?hidpk=1");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const hp = {};
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.getElementById("newpk"), null, { timeout: 5000 });
    await sleep(900);
    hp.after = Object.assign(await st(), { f: await focusCounts(),
      hid_visible: await page.evaluate(() => getComputedStyle(document.getElementById("hidpk")).display) });
    hp.url_after_back = await backToTv("https://x.com/?hidpk=1");
    out.steps.hidden_passkey = hp;

    // 7 bis-g) clic au POINTEUR (souris réelle, pointerdown + click de confiance) sur « Sign in » : arme ; puis
    //          pointerdown réel AVANT la fenêtre (lente) : annule
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    const pt = {};
    const center = (id) => page.evaluate((i) => {
      const r = document.getElementById(i).getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }, id);
    let c0 = await center("signin");
    await page.mouse.click(c0.x, c0.y);
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    pt.click = await st();
    pt.click_back = await backToTv("https://x.com/");
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?slow=1");
    await page.waitForFunction(() => document.activeElement && document.activeElement.id === "signin", null, { timeout: 5000 });
    c0 = await center("signin");
    await page.mouse.click(c0.x, c0.y);
    await sleep(200);
    await page.mouse.click(5, 5);                              // geste réel hors contrôle, avant la fenêtre
    await page.waitForFunction(() => document.getElementById("logindlg"), null, { timeout: 5000 });
    await sleep(900);
    pt.cancel = await st();
    pt.cancel_back = await backToTv("https://x.com/?slow=1");
    out.steps.pointer = pt;

    // 7 bis-h) X focalise l'INSCRIPTION (« Continue with phone ») : sans « Sign in », focus retiré (blur) — un OK réel
    //          n'ouvre rien ; ↓ : Google ; X la refocalise après une touche réelle : retirée encore ; avec « Sign in » :
    //          focus vers « Sign in », même après une touche réelle ; jamais un clic
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?nosignin=1&phonefocus=1");
    await page.waitForFunction(() => window.__phonefocused === true, null, { timeout: 5000 });
    await sleep(900);
    const sf = { initial: await st() };
    await page.keyboard.press("Enter"); await sleep(400);
    sf.ok = await st();
    await page.keyboard.press("ArrowDown"); await sleep(100);
    sf.down = await st();
    await page.evaluate(() => document.getElementById("phone").focus());
    await sleep(700);
    sf.refocus = await st();
    await page.keyboard.press("Enter"); await sleep(400);
    sf.refocus_ok = await st();
    await page.keyboard.press("Escape");
    sf.url_after_back = await waitUrl((u) => u === cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?phonefocus=1");
    await page.waitForFunction(() => window.__phonefocused === true, null, { timeout: 5000 });
    await sleep(900);
    sf.pref_initial = await st();
    await page.keyboard.press("ArrowDown"); await sleep(100);
    sf.pref_down = await st();
    await page.evaluate(() => document.getElementById("phone").focus());
    await sleep(700);
    sf.pref_refocus = await st();
    await page.keyboard.press("Escape");
    sf.pref_url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.signup_focus = sf;

    // 7 bis-i) vérification : « Use your phone » / « Use phone to verify » atteignables, OK réel : UNE activation ;
    //          « Continue with phone » (inscription exacte connue) et le href d'inscription jamais atteints
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/i/flow/verify");
    await page.waitForSelector("#usephone");
    await sleep(900);
    const vf = { initial: await st() };
    const vseq = [];
    for (let i = 0; i < 4; i++) { await page.keyboard.press("ArrowDown"); await sleep(60); vseq.push((await st()).active); }
    vf.seq = vseq;
    await page.keyboard.press("Enter"); await sleep(300);
    vf.ok = await st();
    await page.keyboard.press("Escape");
    vf.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.verify = vf;

    // 7 bis-n) même accueil réel SANS « Sign in » ni autofocus : rien n'est préféré ni déplacé (fail closed) ; le
    //          premier ↓ réel va au premier contrôle permis (Google), JAMAIS à « Continue with phone » (inscription)
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/?nosignin=1&nofocus=1");
    await page.waitForSelector("#landing-identifier");
    await sleep(900);
    const ns = { initial: await st() };
    await page.keyboard.press("ArrowDown"); await sleep(100);
    ns.down = await st();
    await page.keyboard.press("ArrowUp"); await sleep(100);
    ns.up = await st();
    await page.keyboard.press("Escape");
    ns.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.no_signin = ns;

    // 7 bis-b) étape identifiant avec passkey native de X : focus UNE fois sur « Continue with passkey », aucun clic
    //          automatique ; OK synthétique : rien ; OK réel : UNE activation ; repli manuel : ↑ rejoint le champ
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/i/flow/passkey");
    const pk = {};
    await page.evaluate(() => new Promise((res) => {
      const t0 = performance.now();
      (function poll() {
        const ae = document.activeElement;
        if ((ae && ae.id === "passkey") || performance.now() - t0 > 3000) { res(); return; }
        setTimeout(poll, 20);
      })();
    }));
    await sleep(500);
    pk.initial = await st();
    await page.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
    await sleep(150);
    pk.synthetic = await st();
    await page.keyboard.press("Enter"); await sleep(200);
    pk.ok = await st();
    await page.keyboard.press("ArrowUp"); await sleep(60);
    pk.field = (await st()).active;
    await page.keyboard.type("alice");
    pk.typed = await page.evaluate(() => document.getElementById("user").value);
    pk.field_hits = (await st()).hits;
    await page.keyboard.press("Escape");
    pk.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.passkey_login = pk;

    // 7 bis-c) seule l'inscription par téléphone : rien de préféré (fail closed), l'autofocus de X reste
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/i/flow/phoneonly");
    await page.waitForSelector("#user");
    await sleep(900);
    const po = { initial: await st() };
    await page.keyboard.press("ArrowDown"); await sleep(100);
    po.down = await st();
    await page.keyboard.press("Escape");
    po.url_after_back = await waitUrl((u) => u === cfg.tv);
    out.steps.phone_only = po;

    // 7 ter) fenêtre modale VISIBLE au-dessus du fil : elle gagne (mode « contrôles » limité à elle), le fil caché ne
    //         bouge jamais ; OK réel sur le rôle « button » : UN clic ; fenêtre fermée : ↓ reprend le fil
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await page.goto("https://x.com/settings/dialog");
    await waitFeed();
    await sleep(700);
    const dstate = () => page.evaluate(() => {
      const ae = document.activeElement, f = document.querySelector("[data-otvx-focus]"), cs = f ? getComputedStyle(f) : null;
      const post = document.querySelector("article.otvx-focus");
      return { active: ae && ae.id || null, ring: f ? f.id : null, outline: cs ? cs.outlineStyle + " " + cs.outlineWidth : "",
               post: post ? post.getAttribute("data-otv-id") : null, scroll: Math.round(scrollY),
               dialog: !!document.getElementById("dlg"), feed_help: !!document.querySelector(".otvx-help"),
               login_help: !!document.querySelector(".otvx-login-help"), hits: (window.__hits || []).slice(),
               clicks: window.__clicks };
    });
    const dg = { initial: await dstate() };
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-dialog-over-feed.png") }); }
    await page.keyboard.press("ArrowDown"); await sleep(150); dg.down1 = await dstate();
    await page.keyboard.press("ArrowDown"); await sleep(150); dg.down2 = await dstate();
    await page.keyboard.press("ArrowDown"); await sleep(150); dg.edge = await dstate();
    await page.keyboard.press("Enter"); await sleep(700); dg.ok = await dstate();
    await page.keyboard.press("ArrowDown"); await sleep(200); dg.after = await dstate();
    // X rouvre une fenêtre modale PAR-DESSUS un post au focus : OK n'ouvre jamais le post caché, ↓ va à la fenêtre
    await page.evaluate((html) => { document.body.insertAdjacentHTML("beforeend", html); }, DIALOG);
    await sleep(500);
    dg.late = await dstate();
    dg.late.url = page.url();
    await page.keyboard.press("Enter"); await sleep(700);
    dg.late_ok = await dstate();
    dg.late_ok.url = page.url();
    await page.keyboard.press("ArrowDown"); await sleep(150); dg.late_down = await dstate();
    await page.keyboard.press("Enter"); await sleep(400); dg.late_enter = await dstate();   // bouton natif : UNE fois
    out.steps.dialog = dg;

    // 7 bis-forge) une page x.com HOSTILE tente de forger la sonde et les actions : chrome.runtime vers l'extension,
    //   postMessage, objet partagé du monde isolé, fetch direct vers la route (clé devinée), touches synthétiques.
    //   Encadré par deux repères (ctl /mark) ; puis UNE touche réelle ↓ : comptée une fois, synthétiques à part.
    await page.goto("https://x.com/home");
    await waitFeed();
    await sleep(900);                                        // instantané « load » parti
    await ctl("/mark");
    const fg = await page.evaluate(async (a) => {
      const r = { runtime: typeof (window.chrome && window.chrome.runtime && window.chrome.runtime.sendMessage),
                  shared: typeof window.__otvxNav, threw: 0, fetch: "" };
      const forged = { type: "probe", mode: "feed", event: "key", seq: 777777, move: 4242, ok: 4242, other: 0,
                       synthetic: 0, last: { cls: "ok", trusted: true, editable: false, doc_focus: true, handled: true } };
      try { window.chrome.runtime.sendMessage(a.ext, forged, () => undefined); } catch (e) { r.threw += 1; }
      try { window.chrome.runtime.sendMessage(forged); } catch (e) { r.threw += 1; }
      window.postMessage(forged, "*");
      window.postMessage({ type: "home" }, "*");
      try {
        const x = await fetch("http://127.0.0.1:" + a.port + "/api/tv/x/probe", { method: "POST",
          headers: { "Content-Type": "application/json", "X-OpenTV-TV": "A".repeat(43) },
          body: JSON.stringify(Object.assign({ v: 1, page: "x", ext: "0.7.8", sw_s: 1 }, forged)) });
        r.fetch = "status " + x.status;
      } catch (e) { r.fetch = "blocked"; }
      for (let i = 0; i < 6; i++) {
        document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", keyCode: 40, bubbles: true, cancelable: true }));
      }
      for (let i = 0; i < 3; i++) {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, bubbles: true, cancelable: true }));
      }
      return r;
    }, { ext: cfg.ext_id, port: cfg.port });
    await sleep(900);
    fg.after_forge = { url: page.url(), post: await focusedPost(),
                       clicks: await page.evaluate(() => window.__clicks) };
    await page.keyboard.press("ArrowDown");
    await sleep(1200);                                       // ≥ PROBE_MIN_MS + aller-retour
    fg.after_real = { url: page.url(), post: await focusedPost() };
    await ctl("/mark");
    out.steps.forge = fg;
    await page.keyboard.press("Home");
    await waitUrl((u) => u === cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    await waitKey();

    // 7 quater) zone de rédaction autofocusée par X dans le fil : ↓ en sort et parcourt le fil ; après une saisie
    //            réelle d'Alex dans la zone, ↓ y reste natif
    await page.goto("https://x.com/compose/home");
    await waitFeed();
    await sleep(500);
    const cstate = () => page.evaluate(() => {
      const post = document.querySelector("article.otvx-focus");
      return { active: document.activeElement && document.activeElement.id || null,
               post: post ? post.getAttribute("data-otv-id") : null };
    });
    const cp = { initial: await cstate() };
    await page.keyboard.press("ArrowDown"); await sleep(150); cp.leave = await cstate();
    await page.keyboard.press("ArrowDown"); await sleep(150); cp.next = await cstate();
    await page.focus("#compose");
    await page.keyboard.type("abc");
    await page.keyboard.press("ArrowDown"); await sleep(150); cp.typed = await cstate();
    cp.typed.text = await page.evaluate(() => document.getElementById("compose").textContent);
    out.steps.compose = cp;

    // 7 quinquies) CSP stricte de la page : anneau et consigne visibles par la feuille de l'extension (aucun style en
    //              ligne, aucune violation CSP causée par l'extension) ; témoin : un <style> de page EST bloqué
    await page.goto("https://x.com/csp/landing");
    await page.evaluate(() => new Promise((res) => {
      const t0 = performance.now();
      (function poll() {
        const ae = document.activeElement;
        if ((ae && ae.id === "signin") || performance.now() - t0 > 3000) { res(); return; }
        setTimeout(poll, 20);
      })();
    }));
    await sleep(500);
    const csp = await page.evaluate(() => {
      const f = document.querySelector("[data-otvx-focus]"), c = f ? getComputedStyle(f) : null;
      const h = document.querySelector(".otvx-login-help");
      return { active: document.activeElement && document.activeElement.id || null, ring: f ? f.id : null,
               outline: c ? c.outlineStyle + " " + c.outlineWidth : "", inline: f ? f.getAttribute("style") || "" : "?",
               help_pos: h ? getComputedStyle(h).position : "", help_inline: h ? h.getAttribute("style") || "" : "?",
               violations: window.__csp };
    });
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-login-csp.png") }); }
    await page.keyboard.press("ArrowDown"); await sleep(150);
    csp.moved = await page.evaluate(() => {
      const f = document.querySelector("[data-otvx-focus]"), c = getComputedStyle(f);
      return { ring: f.id, outline: c.outlineStyle + " " + c.outlineWidth, inline: f.getAttribute("style") || "" };
    });
    csp.violations_after_keys = await page.evaluate(() => window.__csp);
    csp.control_blocked = await page.evaluate(() => new Promise((res) => {
      const s = document.createElement("style");
      s.textContent = "#apple{color:rgb(1, 2, 3) !important}";
      document.head.appendChild(s);
      setTimeout(() => res({ applied: getComputedStyle(document.getElementById("apple")).color === "rgb(1, 2, 3)",
                             violations: window.__csp }), 200);
    }));
    out.steps.csp = csp;
    await rawKey("BrowserHome", 172);                         // champ au focus : Début y reste natif, Accueil non
    await waitUrl((u) => u === cfg.tv);

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
