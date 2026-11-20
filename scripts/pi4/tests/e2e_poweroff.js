/* Bout en bout (tests seulement, jamais installé) : Réglages → « Éteindre la boîte » sur la VRAIE page /tv (Chromium
   headless, temps réel) pilotée par le clavier (= télécommande HID/CEC normalisée par keys.js) et par le pavé du
   téléphone appairé (POST /r/cmd, cookie + CSRF). Le service OpenTV réel dépose la demande ; ce script joue aussi le
   « faux systemd » (exécuteur) : quand cfg.dir/request apparaît (PathExists de opentv-poweroff.path), il lance la VRAIE
   commande root `opentv-pi poweroff-action` (sans privilège, PATH piégé) et note « poweroff.target » seulement si elle
   sort 0 (OnSuccess= de l'unité). Rien n'éteint jamais rien.
   argv[2] = module playwright ; argv[3] = JSON {chromium, tv, port, host, cookie, csrf, dir, python, cli, env, envfile,
   proc, shots?}. Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawnSync } = require("child_process");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const REQ = path.join(cfg.dir, "request");

const executor = { enabled: false, runs: [], jobs: [], timer: null };
function tick() {
  if (!executor.enabled || !fs.existsSync(REQ)) { return; }
  const p = spawnSync(cfg.python, ["-I", cfg.cli, "--env", cfg.envfile, "poweroff-action", "--proc-root", cfg.proc],
                      { env: cfg.env, encoding: "utf8", timeout: 20000 });
  let rep = null;
  try { rep = JSON.parse(p.stdout); } catch (e) { rep = { raw: (p.stdout || "") + (p.stderr || "") }; }
  executor.runs.push({ code: p.status, rep: rep });
  if (p.status === 0) { executor.jobs.push("poweroff.target (simulé : OnSuccess=, jamais exécuté)"); }
}

(async () => {
  const out = { steps: [], shots: [] };
  executor.timer = setInterval(tick, 50);
  const browser = await pw.chromium.launch({ executablePath: cfg.chromium, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    const posts = [];
    page.on("request", (r) => { if (r.url().indexOf("/api/tv/power/") >= 0) { posts.push(r.url().split("/api/tv/power/")[1]); } });
    await page.goto(cfg.tv);
    await page.waitForSelector(".card.focused", { timeout: 10000 });
    const snap = async (label) => {
      const s = await page.evaluate(() => {
        const btns = Array.prototype.slice.call(document.querySelectorAll("#pbtns button"));
        const f = btns.filter((b) => b.className.indexOf("focused") >= 0)[0];
        const ov = document.getElementById("powerov"), off = document.getElementById("offline");
        const t = document.getElementById("toast");
        return { panel: !document.getElementById("panel").hidden, ptitle: document.getElementById("ptitle").textContent,
                 pmeta: document.getElementById("pmeta").textContent,
                 labels: btns.map((b) => b.getAttribute("data-label") || b.textContent),
                 focused: f ? (f.getAttribute("data-label") || f.textContent) : null,
                 overlay: !ov.hidden, ovmsg: document.getElementById("powermsg").textContent,
                 ovtitle: document.getElementById("powertitle").textContent,
                 ovtop: ov.hidden ? null : document.elementFromPoint(960, 540) &&
                   ov.contains(document.elementFromPoint(960, 540)),
                 offline: !off.hidden, toast: t.hidden ? null : t.textContent,
                 hint: document.getElementById("hint").textContent, url: location.pathname };
      });
      s.s = label;
      s.request = fs.existsSync(REQ);
      s.posts = posts.slice();
      out.steps.push(s);
      return s;
    };
    const shot = async (name) => {
      if (!cfg.shots) { return; }
      await page.screenshot({ path: path.join(cfg.shots, name) });
      out.shots.push(name);
    };
    const press = async (k, ms) => { await page.keyboard.press(k); await sleep(ms === undefined ? 150 : ms); };
    const phone = (a) => new Promise((resolve, reject) => {
      const body = "a=" + a + "&c=" + cfg.csrf;
      const req = http.request({ host: "127.0.0.1", port: cfg.port, path: "/r/cmd", method: "POST", headers: {
        Host: cfg.host, Origin: "http://" + cfg.host, Cookie: "otv_remote=" + cfg.cookie, Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(body) } }, (r) => {
        r.resume(); r.on("end", () => resolve(r.statusCode));
      });
      req.on("error", reject);
      req.end(body);
    });
    const powerRow = async () => {   // Réglages ouverts : descend jusqu'à « Éteindre la boîte » (index lu dans la page)
      const s = await snap("tmp");
      out.steps.pop();
      return s.labels.indexOf("Éteindre la boîte");
    };

    // --- télécommande (clavier) : ↑ (Réglages) puis OK
    await press("ArrowUp");
    await press("Enter", 300);
    const st = await snap("settings");
    out.settings_buttons = st.labels;
    const idx = await powerRow();
    for (let i = 0; i < idx; i++) { await press("ArrowDown", 80); }
    await snap("settings:power-row");
    await shot("power-settings-row.png");

    // 1) double OK rapide : la feuille s'ouvre (« Annuler » au focus), le 2e OK ANNULE
    await page.keyboard.press("Enter");
    await sleep(80);
    await press("Enter", 400);
    await snap("double-ok");

    // 2) OK tenu (répétitions du navigateur) : la feuille s'ouvre, les répétitions sont ignorées
    await page.keyboard.down("Enter");
    for (let i = 0; i < 6; i++) { await sleep(60); await page.keyboard.down("Enter"); }
    await page.keyboard.up("Enter");
    await sleep(400);
    await snap("held-ok");
    await shot("power-confirm-sheet.png");

    // 3) Retour sur la feuille : Annuler (retour aux Réglages, même ligne)
    await press("Escape", 400);
    await snap("back");

    // 4) ↓ « Éteindre » puis OK IMMÉDIAT (< 500 ms) : seulement le rappel, rien n'est demandé
    await press("Enter", 300);
    await press("ArrowDown", 60);
    await press("Enter", 400);
    await snap("ok-too-fast");
    await shot("power-confirm-hint.png");

    // 5) Accueil sur la feuille : fermée, défi oublié
    await press("Home", 400);
    await snap("home");

    // --- téléphone appairé (pavé existant) : chemin d'ÉCHEC récupérable (aucun exécuteur : demande jamais prise)
    await sleep(1200);   // la page a repris sa longue requête du téléphone
    out.phone = [];
    for (const a of ["up", "ok"]) { out.phone.push(await phone(a)); await sleep(350); }
    for (let i = 0; i < idx; i++) { out.phone.push(await phone("down")); await sleep(200); }
    out.phone.push(await phone("ok")); await sleep(400);
    await snap("phone:sheet");
    out.phone.push(await phone("down")); await sleep(800);
    out.phone.push(await phone("ok")); await sleep(600);
    await snap("phone:requested");
    await shot("power-overlay-requested.png");
    out.phone.push(await phone("back")); await press("Escape", 300);   // écran d'arrêt : touches sans effet
    await snap("phone:keys-ignored");
    await page.waitForSelector("#powerov[hidden]", { state: "attached", timeout: 15000 });
    await sleep(300);
    await snap("phone:failed");
    await shot("power-failed-toast.png");

    // --- télécommande : chemin COMPLET avec le faux systemd (exécuteur) actif
    executor.enabled = true;
    await sleep(4500);   // toast d'échec retiré ; Réglages restent ouverts sur la ligne
    const re = await snap("remote:settings");
    if (!re.panel || re.focused !== "Éteindre la boîte") { throw new Error("Réglages attendus : " + JSON.stringify(re)); }
    await press("Enter", 300);
    await press("ArrowDown", 700);   // ≥ POWER_ARM_MS sur « Éteindre »
    await press("Enter", 100);
    await snap("remote:requested");
    await page.waitForFunction(() => document.getElementById("powermsg").textContent.indexOf("Arrêt en cours") === 0,
                               null, { timeout: 8000 });
    await snap("remote:accepted");
    await shot("power-overlay-accepted.png");
    // le service s'arrête aussi (simulé : état injoignable) : l'écran d'arrêt reste, jamais « Reconnexion… »
    await page.route("**/api/tv/**", (r) => r.abort());
    await sleep(6500);
    await press("Escape", 200); await press("Enter", 200);
    await snap("remote:service-gone");
  } finally {
    clearInterval(executor.timer);
    await browser.close();
  }
  out.executor = { runs: executor.runs, jobs: executor.jobs };
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
