/* Bout en bout — saisie de l'identifiant X par téléphone (tests seulement, jamais installé). Chromium COMPLET réel :
   kiosque = contexte persistant avec l'extension TELLE QU'INSTALLÉE par install.sh ; téléphones = navigateurs séparés
   (cookies séparés), nom mDNS de test résolu vers 127.0.0.1 ; vrai service OpenTV (TV + télécommande) ; pages X
   SYNTHÉTIQUES servies par Playwright. Le QR est lu sur une CAPTURE D'ÉCRAN (décodage par le canal de contrôle).
   argv[2] = module playwright ; argv[3] = JSON {chrome, ext, ext_id, tv, port, ctl, map, shots, only?}.
   only (mutations) : null = tout ; "late" = champ rendu inéligible après le QR seulement ; "badstart" = réponse de
   start écartée seulement.
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
const CASES = {
  login: page("Sign in to X", USER),
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
    await ctx.route(/^https:\/\/(x|twitter)\.com\//, (route) => {
      const u = new URL(route.request().url());
      const body = CASES[u.searchParams.get("case") || "login"] || CASES.login;
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
    const openX = async (q) => {
      await tv.goto("https://x.com" + q);
      await tv.waitForSelector("#user");
      await sleep(700);
    };
    const tvState = () => tv.evaluate(() => {
      const ae = document.activeElement;
      const pill = document.querySelector('[data-otvx-entry="pill"]');
      return { active: ae ? (ae.getAttribute("data-otvx-entry") || ae.id || ae.tagName) : null,
               pill: !!pill && pill.isConnected && pill.getBoundingClientRect().width > 0,
               qr: !!document.querySelector('[data-otvx-entry="qr"]'), value: document.getElementById("user").value,
               ev: Object.assign({}, window.__ev), href: location.href };
    });
    const waitFor = async (fn, ms) => {
      for (let i = 0; i < (ms || 8000) / 100; i++) { if (await fn()) { return true; } await sleep(100); }
      return false;
    };
    const service = () => ctl("GET", "/state");
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
    const startSession = async () => {
      await waitFor(async () => !(await tvState()).qr);   // séance précédente terminée côté TV
      await tv.evaluate(() => document.getElementById("user").focus());
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
    for (const [name, q] of [["home", "/home"], ["signup", "/i/jf/onboarding/web?mode=signup"],
                             ["landing", "/"], ["onboarding_login", "/i/jf/onboarding/web?mode=login"]]) {
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
