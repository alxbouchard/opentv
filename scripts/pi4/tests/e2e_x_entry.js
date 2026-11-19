/* Bout en bout — saisie de l'identifiant X par téléphone (tests seulement, jamais installé). Chromium COMPLET réel :
   kiosque = contexte persistant avec l'extension TELLE QU'INSTALLÉE par install.sh ; téléphones = navigateurs séparés
   (cookies séparés), nom mDNS de test résolu vers 127.0.0.1 ; vrai service OpenTV (TV + télécommande) ; pages X
   SYNTHÉTIQUES servies par Playwright. Le QR est lu sur une CAPTURE D'ÉCRAN (décodage par le canal de contrôle).
   argv[2] = module playwright ; argv[3] = JSON {chrome, ext, ext_id, tv, port, ctl, map, shots, only?}.
   only (mutations) : null = tout ; "late" = champ rendu inéligible après le QR seulement ; "badstart" = réponse de
   start écartée seulement ; "root" = accueil déconnecté réel (x.com/ : champ landing-identifier, QR natif de X
   « Scan to get the app », géométrie 1920×1080 mesurée sur le vrai x.com, colonne aux quatre bords de l'écran, champ
   d'inscription rendu sur place) et URL voisines refusées seulement.
   Sortie : une ligne JSON {steps}. */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STYLE = "<style>body{margin:0;font:16px sans-serif;background:#000;color:#fff}" +
  "[role=dialog]{position:fixed;top:40px;left:660px;width:600px;height:900px;background:#000;border:1px solid #333;" +
  "padding:20px}input{display:block;width:500px;height:48px;font-size:20px;margin:20px 0}" +
  "#next,#forgot{display:block;width:300px;height:44px;margin:12px 0;background:#eee;color:#000}</style>";
// instrumentation (monde principal) : chaque événement compté ; la VALEUR n'est lue que par le test
const INSTR = "<script>window.__ev={input:0,change:0,click:0,submit:0};" +
  "['input','change','click','submit'].forEach(function(t){document.addEventListener(t,function(){window.__ev[t]++;},true);});" +
  "</script>";
const USER = `<label for="user">Phone, email, or username</label><input id="user" name="text" type="text" ` +
  `autocomplete="username" autocapitalize="sentences" autofocus>`;
function page(title, field) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${title} / X</title>${STYLE}</head><body>` +
    `<div id="layers"><div role="dialog" aria-modal="true" aria-labelledby="h"><h1 id="h">${title}</h1>` +
    `<form id="f" onsubmit="return false">${field}<div id="next" role="button" tabindex="0">Next</div>` +
    `<button id="forgot" type="button">Forgot password?</button></form></div></div>${INSTR}</body></html>`;
}
// accueil déconnecté RÉEL (HTML de https://x.com/ relevé le 2026-11-06 : balisage du formulaire et attributs EXACTS du
// champ, classes Tailwind remplacées par une CSS ÉQUIVALENTE minimale) + QR NATIF de X « Scan to get the app » en bas à
// droite (rendu côté client par X : sa charge exacte n'est pas relevée ; NATIVE_QR est un substitut d'adresse X de
// téléchargement — seul compte qu'il n'est PAS le QR d'OpenTV). Géométrie 1920×1080 = celle MESURÉE sur le vrai x.com le
// 2026-11-07 (Chromium) : fournisseurs [356,386..566,740], formulaire [356,627,740,757], champ landing-identifier
// [366,659,730,679], Continuer [356,709,740,757], logo X [1053,271,1533,761], pied de page y 1044..1068, calque fixe
// plein écran pointer-events:none. Variantes (même URL exacte « / », choisies par le test avant chaque chargement) :
// colonne collée à gauche, à droite, en haut, en bas ; champ d'inscription rendu SUR PLACE (jamais éligible).
const ONB = "/i/jf/onboarding/web";
const NATIVE_QR = "https://x.com/download?src=landing";
const QRLIB = fs.readFileSync(path.join(cfg.ext, "qrcode.js"), "utf8");
const LAYOUTS = { real: [356, 226], left: [16, 226], right: [1520, 16], top: [768, -385], bottom: [768, 527] };
let rootLayout = "real";
function landing(layout) {
  const pos = LAYOUTS[layout] || LAYOUTS.real;
  const mode = layout === "signup_mode" ? "signup" : "login";
  const fid = layout === "signup_field" ? "signup-identifier" : "landing-identifier";
  const fname = layout === "signup_field" ? "email" : "username";
  const formOpen = layout === "no_form" ? `<div id="f">` : `<form action="${ONB}" method="get">`;
  const formClose = layout === "no_form" ? `</div>` : `</form>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>X. It’s what’s happening / X</title>` +
  `<style>body{margin:0;font:15px/20px sans-serif;background:#fff;color:#0f1419;overflow:hidden}` +
  `#col{position:absolute;left:${pos[0]}px;top:${pos[1]}px;width:384px;display:flex;flex-direction:column;gap:20px}` +
  `h1{margin:0;height:140px;font:700 60px/70px sans-serif}.prov{display:flex;flex-direction:column;gap:20px}` +
  `.prov a{display:flex;align-items:center;justify-content:center;height:46px;box-sizing:border-box;` +
  `border:1px solid #cfd9de;border-radius:9999px;color:#0f1419;text-decoration:none}#phone{background:#000;color:#fff!important}` +
  `#apple{height:48px!important}.or{height:21px;text-align:center;font-size:14px}` +
  `form,#f{display:flex;flex-direction:column;gap:20px;margin:0}` +
  `label{display:block;position:relative;height:62px;box-sizing:border-box;border:1px solid #cfd9de;border-radius:4px;` +
  `padding:31px 9px 0}label>span{display:block}label span span{position:absolute;left:9px;top:8px;font-size:13px;` +
  `color:#536471}label input{display:block;width:100%;height:20px;border:0;padding:0;margin:0;outline:none;` +
  `background:transparent;font-size:17px}#continue{height:48px;border:0;border-radius:9999px;margin:0;background:#aaa}` +
  `p{margin:2px 0 0;font-size:12px;line-height:18px;text-align:center}svg{width:20px;height:20px}` +
  `#logo{position:absolute;left:1053px;top:271px;width:480px;height:490px}` +
  `footer{position:absolute;left:491px;top:1044px;display:flex;gap:11px;font-size:13px;line-height:24px}` +
  `#overlay{pointer-events:none;position:fixed;top:20px;left:20px;right:20px;bottom:20px;z-index:40}` +
  `#getapp{position:fixed;right:40px;bottom:40px;background:#fff;color:#000;padding:16px;text-align:center;` +
  `border:1px solid #cfd9de;border-radius:16px;z-index:30}#appqr{display:block;margin:8px auto 0}</style>` +
  `</head><body><div id="col"><h1>Happening now<!-- -->.</h1><div class="prov">` +
  `<a id="phone" href="${ONB}?mode=signup&amp;redirect_after_login=%2F"><span><svg></svg></span><span>Continue with phone</span></a>` +
  `<a id="google" href="${ONB}?mode=login&amp;redirect_after_login=%2F"><span><svg></svg></span>` +
  `<span>Continue with Google</span></a><a id="apple" href="${ONB}?mode=login&amp;redirect_after_login=%2F">` +
  `<span><svg></svg></span><span>Continue with Apple</span></a></div><div class="or"><span></span> or <span></span></div>` +
  `${formOpen}<input type="hidden" name="mode" value="${mode}"/>` +
  `<label for="${fid}" aria-label="Email or username"><span><span>Email or username</span>` +
  `<span>Email or username</span><input id="${fid}" type="text" autoComplete="username" ` +
  `autoCapitalize="none" autoCorrect="off" spellCheck="false" required="" placeholder=" " ` +
  `class="h-5 w-full bg-transparent outline-none" name="${fname}"/></span></label>` +
  `<button id="continue" type="submit"><span>Continue</span></button>${formClose}<p>By continuing, you agree to our ` +
  `<a id="tos" href="https://x.com/tos" target="_blank" rel="noopener noreferrer nofollow">Terms of Service</a></p>` +
  `</div><svg id="logo" viewBox="0 0 24 24" aria-label="X" role="img"><path d="M2 2h5l15 20h-5z"></path></svg>` +
  `<footer><a href="https://about.x.com">About</a><a href="https://help.x.com/using-x/download-the-x-app">Get App</a>` +
  `<a href="https://x.com/tos">Terms</a><a href="https://x.com/privacy">Privacy</a></footer><div id="overlay"></div>` +
  `<div id="getapp"><div>Scan to get the app</div><canvas id="appqr" width="232" height="232"></canvas>` +
  `</div><script>${QRLIB}</script><script>(function(){var q=qrcode(0,"M");q.addData(${JSON.stringify(NATIVE_QR)});` +
  `q.make();var n=q.getModuleCount(),c=document.getElementById("appqr"),k=Math.floor(232/(n+8)),g=c.getContext("2d");` +
  `g.fillStyle="#fff";g.fillRect(0,0,232,232);g.fillStyle="#000";for(var r=0;r<n;r++){for(var j=0;j<n;j++){` +
  `if(q.isDark(r,j)){g.fillRect((j+4)*k,(r+4)*k,k,k);}}}` +
  `document.addEventListener("submit",function(e){e.preventDefault();},true);` +
  `document.getElementById(${JSON.stringify(fid)}).focus();})();</script>${INSTR}</body></html>`;
}
// 0.7.12 — champ « jf » RÉEL (relevé le 2026-11-07 sur x.com, Chromium 1920×1080, profil neuf, rien saisi) : ~1 à 2 s
// après le chargement, le script de X REMPLACE SUR PLACE (URL inchangée « / ») les fournisseurs et le formulaire servis
// par un <form> SANS action ni « mode » : boutons « Continue with phone » / Google / Apple, puis
// <label for="jf-input-username_or_email"><div><span>…</span><span>…</span><input autocomplete="username webauthn"
// inputmode="text" id="jf-input-username_or_email" pattern="^[a-zA-Z0-9@._+\-]+$" type="text" value=""
// name="username_or_email"><input inert="" type="password" value="" name="password" style="position: absolute; …
// opacity: 0; pointer-events: none;"></div></label>, « Continue » (div), CGU. Même champ sur /i/flow/login et
// /i/jf/onboarding/web?mode=login, dans un role="dialog" sans aria-modal. Classes Tailwind remplacées par une CSS
// équivalente : le champ reste à [366,659,730,679]. Variantes (jamais éligibles) : leurre sans inert, étape mot de passe
// sur place (mot de passe visible), deux leurres, code à usage unique caché, leurre hors du conteneur du champ,
// inscription jf (phone_number), formulaire avec action, autre motif.
const DECOY = `<input inert="" type="password" value="" name="password" style="position: absolute; top: 0px; left: 0px; ` +
  `width: 100%; height: 100%; opacity: 0; pointer-events: none;">`;
function jfForm(v) {
  const id = v === "jf_signup" ? "jf-input-phone_number" : "jf-input-username_or_email";
  const name = v === "jf_signup" ? "phone_number" : "username_or_email";
  const pat = v === "jf_pattern" ? "[0-9]*" : "^[a-zA-Z0-9@._+\\-]+$";
  // jf_rendered (revue 0.7.12) : leurre inert GARDÉ mais RENDU (style retiré : mot de passe visible, pleine taille)
  const decoy = v === "jf_noinert" ? DECOY.replace('inert="" ', "") : v === "jf_rendered" ? DECOY.replace(/ style="[^"]*"/, "") :
    v === "jf_pwstep" ? `<input type="password" value="" name="password" autocomplete="current-password">` : DECOY;
  const field = `<input autocomplete="username webauthn" inputmode="text" id="${id}" pattern="${pat}" type="text" ` +
    `value="" name="${name}">`;
  return `<form${v === "jf_action" ? ` action="${ONB}"` : ""}><button type="button" id="jfphone" class="jfprov">` +
    `<div><p>Continue with phone</p></div></button><div role="button" tabindex="0" id="jfgoogle" class="jfprov">` +
    `<span>Continue with Google</span></div><button type="button" id="jfapple" class="jfprov"><div><p>Continue with ` +
    `Apple</p></div></button><div class="or"><p>or</p></div><label for="${id}"><div class="jfbox"><span>Email or ` +
    `username</span><span>Email or username</span>${field}${v === "jf_nonsibling" ? "" : decoy}</div></label>` +
    (v === "jf_nonsibling" ? decoy : "") + (v === "jf_two" ? DECOY : "") +
    (v === "jf_otp" ? `<input type="text" autocomplete="one-time-code" name="code" hidden>` : "") +
    (/^jf_mode_/.test(v) ? `<input type="hidden" name="mode" value="${v.slice(8)}">` : "") +   // « mode » caché : jamais jf
    `<div id="jfcontinue"><div><p>Continue</p></div></div><p>By continuing, you agree to our ` +
    `<a id="jftos" href="https://x.com/tos">Terms of Service</a></p></form>`;
}
const JF_CSS = `<style>.jfprov{display:flex;align-items:center;justify-content:center;height:46px;box-sizing:border-box;` +
  `border:1px solid #cfd9de;border-radius:9999px;background:#fff;color:#0f1419;font:inherit;margin:0;padding:0}` +
  `#jfapple{height:48px}.jfprov p,.or p{margin:0}.or{height:21px;text-align:center;font-size:14px}` +
  `.jfbox{position:relative}.jfbox>span:first-child{position:absolute;left:0;top:-23px;font-size:13px;color:#536471}` +
  `.jfbox>span+span{display:none}#jfcontinue{height:48px;border-radius:9999px;background:#aaa;display:flex;` +
  `align-items:center;justify-content:center}</style>`;
// accueil « / » : HTML SERVI (landing-identifier) puis remplacement SUR PLACE par le formulaire jf, comme X
function jfLanding(v) {
  const swap = `<script>setTimeout(function(){var c=document.getElementById("col"),h=c.querySelector("h1");` +
    `while(h.nextSibling){c.removeChild(h.nextSibling);}c.insertAdjacentHTML("beforeend",${JSON.stringify(jfForm(v))});},400);` +
    `</script>`;
  return landing("real").replace("</head>", JF_CSS + "</head>").replace("</body>", swap + "</body>");
}
// routes de connexion : même formulaire jf dans un role="dialog" (sans aria-modal, comme X)
function jfDialog(v) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign in to X / X</title>${STYLE}${JF_CSS}` +
    `<style>#jf-input-username_or_email,#jf-input-phone_number{margin:30px 0 0}</style></head><body><div id="layers">` +
    `<div role="dialog" aria-labelledby="h"><h1 id="h">Sign in to X</h1>${jfForm(v)}</div></div>${INSTR}</body></html>`;
}
const CASES = {
  login: page("Sign in to X", USER),
  jf: jfDialog("jf"), jf_rendered: jfDialog("jf_rendered"), jf_mode_login: jfDialog("jf_mode_login"),
  jf_mode_signup: jfDialog("jf_mode_signup"),
  password: page("Enter your password", `<input id="user" name="password" type="password" autocomplete="current-password" autofocus>`),
  otp: page("Enter your verification code", `<input id="user" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" autofocus>`),
  verifyuser: page("Enter your verification code", `<input id="user" name="text" type="text" autocomplete="username" autofocus>`),
  pin: page("Sign in to X", `<input id="user" name="text" type="tel" autocomplete="tel" maxlength="6" autofocus>`),
  withpw: page("Sign in to X", USER + `<input id="pw" name="password" type="password" autocomplete="current-password">`),
  passkey: page("Sign in to X", `<input id="user" name="text" type="text" autocomplete="username webauthn" aria-label="Passkey" autofocus>`),
  backup: page("Entre ton code de secours", `<input id="user" name="text" type="text" autocomplete="username" autofocus>`),
  card: page("Sign in to X", `<input id="user" name="cardnumber" type="text" autocomplete="cc-number" autofocus>`)
};

function ctl(method, p, body) {
  return new Promise((res, rej) => {
    const u = new URL(cfg.ctl + p);
    const r = http.request({ host: u.hostname, port: u.port, path: u.pathname + u.search, method: method }, (x) => {
      const parts = [];
      x.on("data", (d) => parts.push(d));
      x.on("end", () => { const t = Buffer.concat(parts).toString("utf8"); res(t ? JSON.parse(t) : null); });
    });
    r.on("error", rej);
    r.end(body || undefined);
  });
}

(async () => {
  const out = { steps: {} };
  const st = out.steps;
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "otvxeprof"));
  const ctx = await pw.chromium.launchPersistentContext(prof, {
    executablePath: cfg.chrome, headless: true, viewport: { width: 1920, height: 1080 }, colorScheme: null,
    ignoreDefaultArgs: ["--disable-extensions"],
    args: ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext]
  });
  const phoneBrowser = await pw.chromium.launch({ executablePath: cfg.chrome, headless: true,
                                                  args: ["--host-resolver-rules=" + cfg.map] });
  try {
    let sw = ctx.serviceWorkers()[0];
    if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
    if (sw.url().indexOf("chrome-extension://" + cfg.ext_id + "/") !== 0) { throw new Error("ID " + sw.url()); }
    await ctx.route(/^(https:\/\/(x|twitter)\.com\/|http:\/\/x\.com\/|https:\/\/x\.com\.evil\.example\/)/, (route) => {
      const u = new URL(route.request().url());
      // pages de connexion synthétiques ; partout ailleurs (accueil, /home, inscription, http, hôte piégé) : l'accueil RÉEL
      const flow = u.hostname === "x.com" && u.protocol === "https:" && /^\/(i\/flow\/login|i\/jf\/onboarding\/web)$/.test(u.pathname);
      const body = flow ? CASES[u.searchParams.get("case") || "login"] || CASES.login :
        /^jf/.test(rootLayout) ? jfLanding(rootLayout) : landing(rootLayout);
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: body });
    });
    const tv = ctx.pages()[0] || await ctx.newPage();
    await tv.bringToFront();
    const flagOf = () => sw.evaluate(() => chrome.storage.session.get(["xentry", "tvkey"]));
    const loadTv = async () => {
      await tv.goto(cfg.tv);
      const want = await tv.evaluate(() => document.querySelector('meta[name="opentv-key"]').content);
      for (let i = 0; i < 50; i++) {
        const s = await flagOf();
        if (s.tvkey === want) { return s.xentry; }
        await sleep(100);
      }
      return null;
    };
    st.flag_on = await loadTv();
    await ctl("POST", "/presence?on=1");   // X ouvert À LA TV (POST presence de /tv avant d'ouvrir X)
    const openX = async (q, sel) => {
      await tv.goto(/^https?:/.test(q) ? q : "https://x.com" + q);
      await tv.waitForSelector(sel || "#user");
      await sleep(700);
    };
    const tvState = () => tv.evaluate(() => {
      const ae = document.activeElement;
      const pill = document.querySelector('[data-otvx-entry="pill"]');
      return { active: ae ? (ae.getAttribute("data-otvx-entry") || ae.id || ae.tagName) : null,
               pill: !!pill && pill.isConnected && pill.getBoundingClientRect().width > 0,
               qr: !!document.querySelector('[data-otvx-entry="qr"]'),
               value: (document.getElementById("user") || document.getElementById("landing-identifier") ||
                       document.getElementById("signup-identifier") || document.getElementById("jf-input-username_or_email") ||
                       document.getElementById("jf-input-phone_number")).value,
               pw: Array.from(document.querySelectorAll('input[type="password"]')).map((i) => i.value),   // leurre : jamais rempli
               ev: Object.assign({}, window.__ev), href: location.href };
    });
    const waitFor = async (fn, ms) => {
      for (let i = 0; i < (ms || 8000) / 100; i++) { if (await fn()) { return true; } await sleep(100); }
      return false;
    };
    const service = () => ctl("GET", "/state");
    // rectangles EXACTS (getBoundingClientRect) : champ, formulaire, Continuer, QR natif, chaque contrôle ; pastille et
    // panneau d'OpenTV (hôtes des racines fantômes fermées : leur rectangle est visible de la page, pas leur contenu)
    const geo = () => tv.evaluate(() => {
      const R = (el) => { if (!el) { return null; } const r = el.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
      const f = document.getElementById("landing-identifier") || document.getElementById("signup-identifier") ||
        document.getElementById("jf-input-username_or_email");
      const pill = document.querySelector('[data-otvx-entry="pill"]');
      return { vw: innerWidth, vh: innerHeight, field: R(f), form: R(f && f.form), cont: R(document.getElementById("continue") || document.getElementById("jfcontinue")),
               getapp: R(document.getElementById("getapp")), appqr: R(document.getElementById("appqr")),
               help: R(document.querySelector(".otvx-login-help")),
               pill: pill && pill.isConnected ? R(pill) : null, panel: R(document.querySelector('[data-otvx-entry="qr"]')),
               controls: Array.from(document.querySelectorAll("a[href], button, input:not([type=hidden])"))
                 .filter((el) => !el.closest("[data-otvx-entry]")).map(R) };
    });
    // ---------------------------------------------------------------- champ éligible : pastille ; faux événements
    await openX("/i/flow/login");
    st.initial = await tvState();
    await tv.evaluate(() => {   // page : focus programmatique + touches/clics synthétiques sur la pastille
      const p = document.querySelector('[data-otvx-entry="pill"]');
      p.focus();
      p.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      p.click();
    });
    await tv.keyboard.press("Enter");   // OK RÉEL, mais pastille focalisée par la PAGE : jamais armé
    await sleep(800);
    st.fake = { tv: await tvState(), service: await service() };
    await tv.evaluate(() => {
      document.getElementById("user").focus();
      document.getElementById("user").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    });
    await sleep(300);
    st.synthetic_arrow = await tvState();
    const startSession = async (fid) => {
      await waitFor(async () => !(await tvState()).qr);   // séance précédente terminée côté TV
      await tv.evaluate((i) => document.getElementById(i).focus(), fid || "user");
      await sleep(250);
      await tv.keyboard.press("ArrowDown");
      const onPill = (await tvState()).active;
      await tv.keyboard.press("Enter");
      const ok = await waitFor(async () => (await tvState()).qr && (await service()).active);
      if (!ok) { throw new Error("séance non ouverte : " + JSON.stringify({ on_pill: onPill, tv: await tvState(),
                                                                          svc: await service() })); }
      const shot = path.join(cfg.shots, "tv-x-entry-qr.png");
      let qr = { text: "" };
      for (let i = 0; i < 5 && qr.text.indexOf("#") < 0; i++) {   // capture relue (décodage parfois manqué sous charge)
        await sleep(200);
        await tv.locator('[data-otvx-entry="qr"]').screenshot({ path: shot });
        qr = await ctl("POST", "/qr", fs.readFileSync(shot));
      }
      return { on_pill: onPill, started: ok, after: await tvState(), url: qr.text, svc: await service() };
    };
    // ---------------------------------------------------------------- téléphones appairés
    const phones = [];
    const pairPhone = async () => {
      const c = await phoneBrowser.newContext({ viewport: { width: 390, height: 844 } });
      const p = await c.newPage();
      const reqs = [];
      p.on("request", (r) => reqs.push({ url: r.url(), method: r.method(), body: r.postData() || "" }));
      await p.goto((await ctl("GET", "/pair")).url);
      await Promise.all([p.waitForNavigation(), p.click("button.primary")]);
      phones.push({ c, p, reqs });
      return phones[phones.length - 1];
    };
    const A = await pairPhone();
    const B = await pairPhone();
    const phoneText = (p) => p.evaluate(() => ({
      err: document.getElementById("xerr") ? document.getElementById("xerr").textContent : null,
      status: document.getElementById("xstatus") ? document.getElementById("xstatus").textContent : null,
      form: !!document.getElementById("xform") && !document.getElementById("xform").hidden, href: location.href,
      hash: location.hash }));
    const flip = (s, i) => s.slice(0, i) + (s[i] === "A" ? "B" : "A") + s.slice(i + 1);
    const want = (k) => !cfg.only || cfg.only === k;
    if (want("main")) {
    // 1) empreinte du QR altérée : clé refusée par le téléphone, séance annulée, rien envoyé
    let s1 = await startSession();
    st.s1 = s1;
    const u1 = s1.url.split("#");
    await A.p.goto(u1[0] + "#" + flip(u1[1], 30));
    await waitFor(async () => !(await service()).active && !(await tvState()).qr);
    st.wrong_fp = { phone: await phoneText(A.p), service: await service(), tv: await tvState() };
    // 2) secret du QR altéré (empreinte juste) : chiffré envoyé, refusé par l'extension, champ intact
    const s2 = await startSession();
    const u2 = s2.url.split("#");
    await A.p.goto(u2[0] + "#" + flip(u2[1], 3));
    await A.p.waitForSelector("#xform:not([hidden])");
    await A.p.fill("#xid", "mallory@example.com");
    await A.p.click("#xsend");
    await waitFor(async () => /rejeté/.test((await phoneText(A.p)).status || "") && !(await tvState()).qr);
    st.wrong_secret = { phone: await phoneText(A.p), tv: await tvState(), service: await service() };
    // 3) chiffré altéré en route : refusé par l'extension (OAEP), champ intact
    const s3 = await startSession();
    await A.p.route("**/r/x/submit", (route) => {
      const b = JSON.parse(route.request().postData());
      b.ct = flip(b.ct, 200);
      return route.continue({ postData: JSON.stringify(b) });
    });
    await A.p.goto(s3.url);
    await A.p.waitForSelector("#xform:not([hidden])");
    await A.p.fill("#xid", "mallory@example.com");
    await A.p.click("#xsend");
    await waitFor(async () => /rejeté/.test((await phoneText(A.p)).status || "") && !(await tvState()).qr);
    await A.p.unroute("**/r/x/submit");
    st.tampered_ct = { phone: await phoneText(A.p), tv: await tvState(), service: await service() };
    // 4) chemin nominal : A réclame, B refusé ; A envoie ; UN input + UN change ; aucun clic ni envoi
    await tv.evaluate(() => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; });
    const s4 = await startSession();
    st.s4 = { on_pill: s4.on_pill, started: s4.started, url: s4.url, svc: s4.svc };
    A.reqs.length = 0;
    await A.p.goto(s4.url);
    await A.p.waitForSelector("#xform:not([hidden])");
    st.scrubbed = await phoneText(A.p);
    st.phone_input = await A.p.evaluate(() => {
      const i = document.getElementById("xid");
      return ["type", "autocomplete", "autocapitalize", "autocorrect", "spellcheck", "name", "inputmode"].reduce(
        (o, k) => { o[k] = i.getAttribute(k); return o; }, {});
    });
    await A.p.screenshot({ path: path.join(cfg.shots, "phone-x-entry.png") });
    await B.p.goto(s4.url);
    await waitFor(async () => /autre téléphone/.test((await phoneText(B.p)).status || ""));
    st.second_phone = await phoneText(B.p);
    let release = null;
    const held = new Promise((r) => { release = r; });
    await A.p.route("**/r/x/status*", async (route) => { await held; await route.continue().catch(() => undefined); });
    await A.p.fill("#xid", "  alex@example.com\u200b ");
    await A.p.click("#xsend");
    await A.p.waitForFunction(() => /Envoyé chiffré/.test(document.getElementById("xstatus").textContent));
    st.after_submit = await A.p.evaluate(() => {   // aussitôt après le 200 : Annuler n'est plus offert
      const b = document.getElementById("xcancel");
      const out = { hidden: b.hidden, disabled: b.disabled, visible: b.getBoundingClientRect().width > 0 };
      b.click();
      b.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      return out;
    });
    await waitFor(async () => (await tvState()).value !== "" && !(await service()).active);
    st.phone_reqs = A.reqs.map((r) => ({ path: new URL(r.url).pathname, method: r.method, url: r.url, body: r.body }));
    // course : Annuler arrivé APRÈS la livraison (requête directe, comme un tap retardé) -> issue « delivered » gardée
    st.late_cancel = await A.p.evaluate(async () => {
      const m = document.getElementById("xentry");
      const r = await fetch("/r/x/cancel", { method: "POST", headers: { "Content-Type": "application/json",
        "X-OpenTV-Remote": "x" }, body: JSON.stringify({ s: m.getAttribute("data-s"), c: m.getAttribute("data-c") }) });
      return { status: r.status, j: await r.json() };
    });
    release();
    await A.p.unroute("**/r/x/status*");
    await waitFor(async () => /C'est fait/.test((await phoneText(A.p)).status || ""));
    st.late_cancel.after = await A.p.evaluate(async () => {   // issue relue APRÈS l'annulation tardive
      const sid = document.getElementById("xentry").getAttribute("data-s");
      return (await (await fetch("/r/x/status?s=" + encodeURIComponent(sid))).json()).state;
    });
    await sleep(300);
    st.filled = { tv: await tvState(), phone: await phoneText(A.p), service: await service() };
    // 5) annulations : touche réelle, focus posé par la page, navigation, Retour (/tv rechargée)
    await startSession();
    await tv.keyboard.press("ArrowDown");
    await waitFor(async () => !(await service()).active && !(await tvState()).qr);
    st.cancel_key = { tv: await tvState(), service: await service() };
    await startSession();
    await tv.evaluate(() => document.getElementById("next").focus());
    await waitFor(async () => !(await service()).active && !(await tvState()).qr);
    st.cancel_focus = { tv: await tvState(), service: await service() };
    await startSession();
    await tv.evaluate(() => history.pushState({}, "", "/i/flow/login?step=2"));
    await waitFor(async () => !(await service()).active && !(await tvState()).qr);
    st.cancel_nav = { tv: await tvState(), service: await service() };
    const s8 = await startSession();
    await A.p.goto(s8.url);
    await A.p.waitForSelector("#xform:not([hidden])");
    st.return_before = (await service()).active;
    await loadTv();   // Retour Samsung : le kiosque relancé recharge /tv
    st.return_after = await service();
    await waitFor(async () => /annulée/.test((await phoneText(A.p)).status || ""));
    st.return_phone = await phoneText(A.p);
    }
    // ---------------------------------------------------------------- dernière vérification avant de remplir
    // MÊME élément au focus de bout en bout ; APRÈS le QR/start et AVANT l'envoi du téléphone, la page X le rend
    // inéligible (mot de passe, code à usage unique, fenêtre « code de vérification », mot de passe voisin, libellé,
    // longueur de code) : l'extension refuse à la livraison ; champ vide ; aucun input/change/click/submit.
    if (want("late")) {
      await ctl("POST", "/presence?on=1");
      st.late = {};
      for (const c of ["password", "otp", "vcode_dialog", "danger", "label", "maxlen", "numeric"]) {
        await openX("/i/flow/login");
        await tv.evaluate(() => { window.__u = document.getElementById("user"); });
        const sl = await startSession();
        await A.p.goto(sl.url);
        await A.p.waitForSelector("#xform:not([hidden])");
        await tv.evaluate((k) => {
          window.__ev = { input: 0, change: 0, click: 0, submit: 0 };
          const u = window.__u;
          if (k === "password") { u.type = "password"; }
          if (k === "otp") { u.setAttribute("autocomplete", "one-time-code"); }
          if (k === "vcode_dialog") { document.getElementById("h").textContent = "Enter your verification code"; }
          if (k === "danger") {
            const pw = document.createElement("input");
            pw.type = "password";
            pw.autocomplete = "current-password";
            document.getElementById("f").appendChild(pw);
          }
          if (k === "label") { u.setAttribute("aria-label", "Verification code"); }
          if (k === "maxlen") { u.setAttribute("maxlength", "6"); }
          if (k === "numeric") { u.setAttribute("inputmode", "numeric"); }
        }, c);
        const before = await tv.evaluate(() => ({ same: document.activeElement === window.__u, qr:
          !!document.querySelector('[data-otvx-entry="qr"]') }));
        await A.p.fill("#xid", "alex@example.com");
        await A.p.click("#xsend");
        await waitFor(async () => /Rien n'a été saisi|rien saisi|C'est fait/.test((await phoneText(A.p)).status || "") &&
                                  !(await tvState()).qr);
        st.late[c] = { before: before, phone: await phoneText(A.p), service: await service(),
                       tv: Object.assign(await tvState(), await tv.evaluate(() => ({
                         same: document.activeElement === window.__u, uvalue: window.__u.value }))) };
      }
    }
    // ---------------------------------------------------------------- réponse d'envoi PERDUE (course réelle)
    // le service accepte l'envoi, le téléphone perd la réponse (réseau) ; la TV livre ; le renvoi reçoit 410 over +
    // issue réelle -> « C'est fait », Annuler caché ; jamais « Rien n'a été saisi » ; UN seul remplissage
    if (want("lost")) {
      await ctl("POST", "/presence?on=1");
      await openX("/i/flow/login");
      await tv.evaluate(() => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; });
      const sx = await startSession();
      let relx = null;
      const heldx = new Promise((r) => { relx = r; });
      await A.p.route("**/r/x/status*", async (route) => { await heldx; await route.continue().catch(() => undefined); });
      let firstSubmit = 0;   // la requête RÉELLE du téléphone (mêmes en-têtes, Host, corps) atteint le service ; sa
      await A.p.route("**/r/x/submit", async (route) => {   // réponse est perdue : le navigateur voit un échec réseau
        const rq = route.request();
        const u = new URL(rq.url());
        firstSubmit = await new Promise((res) => {
          const c = http.request({ host: "127.0.0.1", port: u.port, path: u.pathname, method: "POST",
                                   headers: Object.assign({}, rq.headers(), { host: u.host }) },
                                 (r) => { r.resume(); r.on("end", () => res(r.statusCode)); });
          c.on("error", () => res(-1));
          c.end(rq.postData());
        });
        await route.abort("failed");
      }, { times: 1 });
      await A.p.goto(sx.url);
      await A.p.waitForSelector("#xform:not([hidden])");
      await A.p.fill("#xid", "alex@example.com");
      await A.p.click("#xsend");
      await waitFor(async () => /incertain/i.test((await phoneText(A.p)).status || ""));
      const unsure = await phoneText(A.p);
      await waitFor(async () => (await tvState()).value !== "" && !(await service()).active);
      await A.p.fill("#xid", "alex@example.com");
      await A.p.click("#xsend");
      await waitFor(async () => /C'est fait/.test((await phoneText(A.p)).status || ""));
      st.lost = { first_submit: firstSubmit, unsure: unsure, after: await phoneText(A.p), tv: await tvState(), service: await service(),
                  cancel: await A.p.evaluate(() => { const b = document.getElementById("xcancel");
                    return { hidden: b.hidden, disabled: b.disabled }; }) };
      relx();
      await A.p.unroute("**/r/x/status*");
    }
    // ---------------------------------------------------------------- réponse de start écartée
    // le service a créé la séance, mais sa réponse est écartée (adresse hors forme) : annulation au mieux immédiate
    if (want("badstart")) {
      await ctl("POST", "/presence?on=1");
      await openX("/i/flow/login");
      await ctl("POST", "/badurl?on=1");
      const before = await service();
      await tv.evaluate(() => document.getElementById("user").focus());
      await sleep(250);
      await tv.keyboard.press("ArrowDown");
      const onPill = (await tvState()).active;
      await tv.keyboard.press("Enter");
      await waitFor(async () => (await service()).started > before.started);
      await waitFor(async () => !(await service()).active, 6000);
      st.badstart = { on_pill: onPill, before: before, after: await service(), tv: await tvState() };
      await ctl("POST", "/badurl?on=0");
    }
    // ---------------------------------------------------------------- accueil déconnecté RÉEL (x.com/)
    // terrain 0.7.10 : « Happening now », « Email or username » au focus, QR NATIF de X « Scan to get the app » en bas à
    // droite. Pastille pour landing-identifier SEULEMENT ; QR natif ignoré ; inscription par téléphone jamais atteinte ;
    // flèche RÉELLE + OK RÉEL exigés ; QR d'OpenTV décodé sur capture et DISTINCT du QR natif ; URL voisines refusées.
    if (want("root")) {
      await ctl("POST", "/presence?on=1");
      const R = {};
      st.root = R;
      await openX("/", "#landing-identifier");
      await sleep(300);
      await tv.evaluate(() => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; });
      R.initial = await tvState();
      R.geo_pill = await geo();
      R.native = await tv.evaluate(() => {
        const n = document.getElementById("getapp");
        const r = n.getBoundingClientRect();
        return { text: n.textContent, canvas: !!n.querySelector("canvas"), right: Math.round(innerWidth - r.right),
                 bottom: Math.round(innerHeight - r.bottom), otv: !!n.closest("[data-otvx-entry]") };
      });
      if (R.initial.pill) {
        await tv.evaluate(() => {   // page : focus programmatique + touches/clics synthétiques sur la pastille
          const p = document.querySelector('[data-otvx-entry="pill"]');
          p.focus();
          p.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
          document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
          p.click();
        });
        await tv.keyboard.press("Enter");   // OK RÉEL, mais pastille focalisée par la PAGE : jamais armé
        await sleep(800);
        R.fake = { tv: await tvState(), service: await service() };
        await tv.evaluate(() => {
          const u = document.getElementById("landing-identifier");
          u.focus();
          u.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
        });
        await sleep(300);
        R.synthetic_arrow = await tvState();
        // autres contrôles de l'accueil au focus (inscription, fournisseurs, Continuer, QR natif) : jamais de pastille
        R.others = {};
        for (const id of ["phone", "google", "apple", "continue", "tos"]) {
          await tv.evaluate((i) => document.getElementById(i).focus(), id);
          await sleep(250);
          R.others[id] = await tvState();
        }
        // parcours RÉEL ↑ depuis le champ : « Continue with phone » (inscription) jamais atteint
        await tv.evaluate(() => document.getElementById("landing-identifier").focus());
        await sleep(250);
        R.walk_up = [];
        for (let i = 0; i < 5; i++) { await tv.keyboard.press("ArrowUp"); R.walk_up.push((await tvState()).active); }
        // flèche RÉELLE ↓ -> pastille, OK RÉEL -> QR d'OpenTV (compteurs remis à zéro : le clic factice ci-dessus
        // était celui du TEST ; à partir d'ici, aucun clic ni envoi)
        await tv.evaluate(() => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; });
        const sr = await startSession("landing-identifier");
        R.session = { on_pill: sr.on_pill, started: sr.started, url: sr.url, svc: sr.svc };
        const nshot = path.join(cfg.shots, "tv-x-native-app-qr.png");
        await tv.locator("#appqr").screenshot({ path: nshot });
        R.native_qr = (await ctl("POST", "/qr", fs.readFileSync(nshot))).text;
        const full = path.join(cfg.shots, "tv-x-root-landing.png");
        await tv.screenshot({ path: full });
        R.full_qrs = (await ctl("POST", "/qrmulti", fs.readFileSync(full))).texts;
        R.geo_panel = await geo();
        await A.p.goto(sr.url);
        await A.p.waitForSelector("#xform:not([hidden])");
        await A.p.fill("#xid", "alex@example.com");
        await A.p.click("#xsend");
        await waitFor(async () => (await tvState()).value !== "" && !(await service()).active);
        await waitFor(async () => /C'est fait/.test((await phoneText(A.p)).status || ""));
        await sleep(300);
        R.filled = { tv: await tvState(), phone: await phoneText(A.p), service: await service() };
      }
      // URL voisines : même accueil RÉEL, champ au focus ; jamais de pastille, jamais de séance (OK réel compris)
      R.neg = {};
      for (const [name, q] of [["root_signup", "/?mode=signup"], ["root_mode_login", "/?mode=login"],
                               ["root_start", "/?start=phone"], ["root_lang", "/?lang=en"], ["root_unexpected", "/?foo=1"],
                               ["home", "/home"], ["flow_signup", "/i/flow/signup"], ["http", "http://x.com/"],
                               ["evil", "https://x.com.evil.example/"], ["root_bare_query", "/?"],
                               ["root_bare_hash", "/#"]]) {
        await openX(q, "#landing-identifier");
        await sleep(300);
        const before = await tvState();
        await tv.keyboard.press("ArrowDown");
        await tv.keyboard.press("Enter");
        await sleep(500);
        R.neg[name] = { href: before.href, active: before.active, pill: before.pill, after: (await tvState()).active,
                        qr: (await tvState()).qr, service: (await service()).active };
      }
      // bords de l'écran (même URL exacte) : colonne de X collée à gauche, à droite, en haut, en bas ; pastille mesurée,
      // puis flèche RÉELLE + OK RÉEL -> panneau mesuré et QR d'OpenTV décodé sur capture ; touche réelle : annulée
      R.edges = {};
      for (const lay of R.initial.pill ? ["left", "right", "top", "bottom"] : []) {   // (mutant sans « / » : aucune)
        rootLayout = lay;
        await openX("/", "#landing-identifier");
        await sleep(300);
        const before = await geo();
        const ss = await startSession("landing-identifier");
        const after = await geo();
        await tv.keyboard.press("ArrowDown");
        await waitFor(async () => !(await service()).active && !(await tvState()).qr);
        R.edges[lay] = { before: before, after: after, url: ss.url, sid: ss.svc.sid, on_pill: ss.on_pill };
      }
      // champ d'inscription rendu SUR PLACE à « / » (URL exacte, champ au focus) : jamais de pastille ni de séance
      R.inplace = {};
      for (const lay of ["signup_mode", "signup_field", "no_form"]) {
        rootLayout = lay;
        await openX("/", lay === "signup_field" ? "#signup-identifier" : "#landing-identifier");
        await sleep(300);
        const before = await tvState();
        await tv.keyboard.press("ArrowDown");
        await tv.keyboard.press("Enter");
        await sleep(500);
        R.inplace[lay] = { href: before.href, active: before.active, pill: before.pill, after: (await tvState()).active,
                           qr: (await tvState()).qr, service: (await service()).active };
      }
      rootLayout = "real";
    }
    // ---------------------------------------------------------------- champ « jf » RÉEL (0.7.12, défaut terrain 0.7.11)
    // « / » servi puis remplacé SUR PLACE par le formulaire jf de X (URL inchangée) : pastille, flèche RÉELLE + OK RÉEL,
    // QR décodé, UN input + UN change, leurre jamais rempli, aucun clic ni envoi ; routes de connexion jf (fenêtre) ;
    // variantes jamais éligibles ; étape mot de passe rendue sur place APRÈS le QR : refus à la livraison.
    if (want("jf")) {
      await ctl("POST", "/presence?on=1");
      const J = {};
      st.jf = J;
      const JFID = "jf-input-username_or_email";
      // DISPONIBILITÉ OBSERVABLE (stabilisation du test, produit inchangé) : xentry.js n'arme sa pastille qu'après la
      // réponse « enabled » du service worker, aller-retour ASYNCHRONE refait à chaque document ; une lecture unique à
      // +300 ms pouvait précéder cette réponse (témoin positif manqué, fenêtre négative vide de sens). Journal PASSIF du
      // service worker (écouteur qui ne répond jamais : la réponse reste celle de background.js) : chaque « enabled » du
      // cadre principal (URL, documentId), puis « answered » quand un storage.session.get émis APRÈS celui de
      // background.js est résolu (même file : sa réponse est alors déjà partie). Toute observation attend l'entrée du
      // document COURANT (même URL, postérieure à la navigation) ; service worker relancé (journal perdu) : échec franc.
      await sw.evaluate(() => {
        if (globalThis.__otvxJournal) { return; }
        const j = globalThis.__otvxJournal = [];
        chrome.runtime.onMessage.addListener((msg, sender) => {
          if (!msg || msg.type !== "xentry" || msg.op !== "enabled" || !sender || sender.frameId !== 0) { return undefined; }
          const e = { url: sender.url, doc: sender.documentId || null, answered: false };
          j.push(e);
          chrome.storage.session.get(["xentry"]).then(() => { e.answered = true; }, () => undefined);
          return undefined;
        });
      });
      const mark = () => sw.evaluate(() => globalThis.__otvxJournal ? globalThis.__otvxJournal.length : -1);
      const READY_MS = 8000;
      const POS_MS = 3000;     // témoin positif : pastille attendue au plus POS_MS après la preuve de disponibilité
      const NEG_MIN = 1000;    // fenêtre négative : au moins NEG_MIN et 4 × la pire latence positive mesurée
      J.timing = { ready: [], pos: [], neg: [] };
      const ready = async (n0, what) => {
        const href = await tv.evaluate(() => location.href);
        const t0 = Date.now();
        for (;;) {
          const j = await sw.evaluate((n) => globalThis.__otvxJournal ? globalThis.__otvxJournal.slice(n) : null, n0);
          if (n0 < 0 || !j) { throw new Error("journal du service worker absent (relancé ?) : " + what); }
          const e = j.find((x) => x.url === href && x.answered);
          if (e) { J.timing.ready.push([what, Date.now() - t0]); return e.doc; }
          if (Date.now() - t0 > READY_MS) { throw new Error("document non prêt (« enabled » sans réponse) : " + what); }
          await sleep(25);
        }
      };
      // témoin POSITIF : focus APRÈS la preuve de disponibilité, puis pastille attendue (bornée, latence consignée) ;
      // délai dépassé = pastille absente rapportée telle quelle (aucune reprise)
      const positive = async (n0, fid, what) => {
        await ready(n0, what);
        await tv.evaluate((i) => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; document.getElementById(i).focus(); }, fid);
        const t0 = Date.now();
        let s = await tvState();
        while (!s.pill && Date.now() - t0 < POS_MS) { await sleep(25); s = await tvState(); }
        J.timing.pos.push([what, s.pill ? Date.now() - t0 : null]);
        return s;
      };
      // fenêtre NÉGATIVE : focus APRÈS la preuve de disponibilité, puis échantillons continus pendant toute la fenêtre ;
      // pastille vue UNE fois = pastille (jamais une lecture unique)
      const negative = async (n0, fid, what) => {
        await ready(n0, what);
        await tv.evaluate((i) => document.getElementById(i).focus(), fid);
        const ms = Math.max(NEG_MIN, 4 * Math.max(0, ...J.timing.pos.map((x) => x[1] || 0)));
        const t0 = Date.now();
        let s = await tvState();
        let seen = s.pill;
        let n = 1;
        while (!seen && Date.now() - t0 < ms) { await sleep(25); s = await tvState(); seen = s.pill; n++; }
        J.timing.neg.push([what, ms, n, seen]);
        return Object.assign(s, { pill: seen });
      };
      const openJf = async (lay, sel) => {
        rootLayout = lay;
        const n0 = await mark();
        await openX("/", sel || "#" + JFID);
        return n0;
      };
      const openRoute = async (q) => {
        const n0 = await mark();
        await openX(q, "#" + JFID);
        return n0;
      };
      const n0j = await openJf("jf");
      J.swapped = await tv.evaluate(() => ({ landing: !!document.getElementById("landing-identifier"),
        modes: document.querySelectorAll('input[name="mode"]').length, action: document.querySelector("form").getAttribute("action") }));
      J.initial = await positive(n0j, JFID, "root");
      J.geo_pill = await geo();
      if (J.initial.pill) {   // (mutant sans la branche jf : aucune pastille — défaut terrain 0.7.11 reproduit)
      const sj = await startSession(JFID);
      J.session = { on_pill: sj.on_pill, started: sj.started, url: sj.url, svc: sj.svc };
      J.geo_panel = await geo();
      await A.p.goto(sj.url);
      await A.p.waitForSelector("#xform:not([hidden])");
      await A.p.fill("#xid", "alex@example.com");
      await A.p.click("#xsend");
      await waitFor(async () => (await tvState()).value !== "" && !(await service()).active);
      await waitFor(async () => /C'est fait/.test((await phoneText(A.p)).status || ""));
      await sleep(300);
      J.filled = { tv: await tvState(), phone: await phoneText(A.p), service: await service() };
      }
      // routes de connexion : même champ jf dans la fenêtre de X
      J.routes = {};
      for (const [name, q] of [["flow", "/i/flow/login?case=jf"], ["onboarding", "/i/jf/onboarding/web?mode=login&case=jf"]]) {
        J.routes[name] = await positive(await openRoute(q), JFID, name);
      }
      await tv.evaluate(() => { window.__ev = { input: 0, change: 0, click: 0, submit: 0 }; });
      if (J.routes.onboarding.pill) {
      const sf = await startSession(JFID);
      await A.p.goto(sf.url);
      await A.p.waitForSelector("#xform:not([hidden])");
      await A.p.fill("#xid", "alex@example.com");
      await A.p.click("#xsend");
      await waitFor(async () => (await tvState()).value !== "" && !(await service()).active);
      await sleep(300);
      J.routes.filled = await tvState();
      }
      // variantes : jamais de pastille, jamais de séance (flèche RÉELLE + OK RÉEL compris)
      J.neg = {};
      for (const v of ["jf_noinert", "jf_pwstep", "jf_two", "jf_otp", "jf_nonsibling", "jf_signup", "jf_action", "jf_pattern",
                       "jf_rendered", "jf_mode_login", "jf_mode_signup"]) {
        const fid = v === "jf_signup" ? "jf-input-phone_number" : JFID;
        const before = await negative(await openJf(v, "#" + fid), fid, v);
        await tv.keyboard.press("ArrowDown");
        await tv.keyboard.press("Enter");
        await sleep(500);
        J.neg[v] = { href: before.href, active: before.active, pill: before.pill, after: (await tvState()).active,
                     qr: (await tvState()).qr, service: (await service()).active };
      }
      // revue 0.7.12 : mêmes refus sur les routes de connexion (fenêtre jf) — leurre inert RENDU, « mode » caché
      J.routeNeg = {};
      for (const v of ["jf_rendered", "jf_mode_login", "jf_mode_signup"]) {
        const before = await negative(await openRoute("/i/flow/login?case=" + v), JFID, "route_" + v);
        const shown = await tv.evaluate(() => {   // leurre réellement RENDU (boîte non nulle, opacité 1) mais inert
          const pw = document.querySelector('input[name="password"]');
          const r = pw.getBoundingClientRect();
          return { inert: pw.hasAttribute("inert"), w: r.width, h: r.height, op: getComputedStyle(pw).opacity,
                   modes: Array.from(document.querySelectorAll('input[name="mode"]')).map((m) => m.getAttribute("value")) };
        });
        await tv.keyboard.press("ArrowDown");
        await tv.keyboard.press("Enter");
        await sleep(500);
        J.routeNeg[v] = { href: before.href, active: before.active, pill: before.pill, after: (await tvState()).active,
                          qr: (await tvState()).qr, service: (await service()).active, shown: shown };
      }
      // étape mot de passe rendue SUR PLACE après le QR (leurre rendu actif et visible) : refus à la livraison
      const n0l = await openJf("jf");
      await tv.evaluate((i) => { window.__u = document.getElementById(i); }, JFID);
      if (J.initial.pill) {
      await positive(n0l, JFID, "late");   // pastille observée avant ↓ + OK (startSession), même document
      const sl = await startSession(JFID);
      await A.p.goto(sl.url);
      await A.p.waitForSelector("#xform:not([hidden])");
      await tv.evaluate(() => {
        window.__ev = { input: 0, change: 0, click: 0, submit: 0 };
        const pw = document.querySelector('input[name="password"]');
        pw.removeAttribute("inert");
        pw.removeAttribute("style");
      });
      const before = await tv.evaluate(() => ({ same: document.activeElement === window.__u,
                                                qr: !!document.querySelector('[data-otvx-entry="qr"]') }));
      await A.p.fill("#xid", "alex@example.com");
      await A.p.click("#xsend");
      await waitFor(async () => /Rien n'a été saisi|rien saisi|C'est fait/.test((await phoneText(A.p)).status || "") &&
                                !(await tvState()).qr);
      J.late = { before: before, phone: await phoneText(A.p), service: await service(),
                 tv: Object.assign(await tvState(), await tv.evaluate(() => ({ same: document.activeElement === window.__u,
                                                                            uvalue: window.__u.value }))) };
      }
      rootLayout = "real";
    }
    if (want("main")) {
    // ---------------------------------------------------------------- champs jamais éligibles
    await ctl("POST", "/presence?on=1");
    st.ineligible = {};
    for (const c of ["password", "otp", "verifyuser", "pin", "withpw", "passkey", "backup", "card"]) {
      await openX("/i/flow/login?case=" + c);
      const before = await tvState();
      await tv.keyboard.press("ArrowDown");
      await tv.keyboard.press("Enter");
      await sleep(500);
      st.ineligible[c] = { pill: before.pill, after: (await tvState()).active, service: (await service()).active };
    }
    for (const [name, q] of [["signup", "/i/jf/onboarding/web?mode=signup"],
                             ["onboarding_login", "/i/jf/onboarding/web?mode=login"]]) {
      await openX(q);
      st.ineligible["url_" + name] = { pill: (await tvState()).pill };
    }
    // ---------------------------------------------------------------- drapeau coupé : aucune UI
    await ctl("POST", "/flag?on=0");
    st.flag_off = await loadTv();
    await ctl("POST", "/presence?on=1");
    await openX("/i/flow/login");
    st.off = await tvState();
    }
  } catch (err) {
    out.error = String(err && err.stack || err).slice(0, 1500);
    process.stdout.write(JSON.stringify(out) + "\n");
    process.exitCode = 1;
  } finally {
    await phoneBrowser.close().catch(() => undefined);
    await ctx.close().catch(() => undefined);
  }
  if (!out.error) { process.stdout.write(JSON.stringify(out) + "\n"); }
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
