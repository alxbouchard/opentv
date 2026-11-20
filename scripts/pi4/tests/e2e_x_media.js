/* Bout en bout 0.7.15 (tests seulement, jamais installé) : aperçus de médias du fil X et du détail d'un post, rangée
   d'actions et bandeau d'aide. Chromium COMPLET réel ; extension telle qu'installée (0.7.15), celle d'un commit témoin
   (0.7.14) ou AUCUNE extension (ext = "") ; pages x.com SYNTHÉTIQUES et ANONYMES servies par Playwright, toute autre
   requête annulée (aucun réseau, aucun compte, aucune session). Rien n'est lu d'un vrai X.

   Médias portrait 9:16 (boîte d'aspect proportionnelle à la LARGEUR, comme X), poster ROUGE plein (pixel discriminant),
   dans cinq états : poster (lecteur non initialisé : fond + ▶, AUCUN data-testid média), card (card.wrapper > a > img),
   lazy (img dont src est posée par IntersectionObserver), defer (bouton vide remplacé par le lecteur quand il devient
   visible : IntersectionObserver), init (videoPlayer/videoComponent/video poster + ▶). Quatre structures : sib (rangée
   d'actions sœur du média), grp (rangée [role=group] DANS un groupe ancêtre mixte qui contient aussi le média), deep
   (idem, un niveau plus bas), flat (boutons directement dans le groupe mixte, sans groupe de rangée propre). Plus :
   post citant un post dont média + rangée sont dans un groupe mixte (qm), post citant un post à rangée pure (qp), post
   LONG texte seul (aide), post plat dont X REMPLACE le bouton au clic (rp). Chaque post a « Show more ».
   Le groupe ancêtre mixte est une HYPOTHÈSE de pire cas (non attestée pour le DOM réel de X).

   Preuves : pixel au cœur du cadre lu sur la CAPTURE (PNG décodé ici) ; chargement réel (img chargée, lecteur différé
   initialisé) ; contrôles natifs du média visibles ; puis D-pad réel (↓ → OK ←) : J'aime / Enregistrer de CE post,
   activation native fiable, jamais un bouton du post cité ; bande d'aide : aucun glyphe de texte sous l'aide (pixels),
   ↓ révèle la ligne cachée, aucun saut de défilement après une passe de rafraîchissement.
   argv[2] = module playwright ; argv[3] = JSON {chrome, ext, ext_id, label, shots, pages: ["home", "status"]}.
   Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function decodePng(buf) {   // 8 bits, RVB/RVBA, non entrelacé (captures de Chromium) -> px(x, y) = [r, g, b]
  const zlib = require("zlib");
  let pos = 8, w = 0, h = 0, ct = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString("ascii", pos + 4, pos + 8), data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") {
      w = data.readUInt32BE(0); h = data.readUInt32BE(4); ct = data[9];
      if (data[8] !== 8 || data[12] !== 0 || (ct !== 2 && ct !== 6)) { throw new Error("PNG non pris en charge"); }
    } else if (type === "IDAT") { idat.push(data); }
    pos += 12 + len;
  }
  const bpp = ct === 6 ? 4 : 3, stride = w * bpp, raw = zlib.inflateSync(Buffer.concat(idat));
  const px = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], src = y * (stride + 1) + 1, row = y * stride;
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? px[row + i - bpp] : 0, b = y ? px[row - stride + i] : 0, c = i >= bpp && y ? px[row - stride + i - bpp] : 0;
      let v = raw[src + i];
      if (f === 1) { v += a; } else if (f === 2) { v += b; } else if (f === 3) { v += (a + b) >> 1; }
      else if (f === 4) { const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      px[row + i] = v & 255;
    }
  }
  return { w: w, h: h, px: (x, y) => { const o = y * stride + x * bpp; return [px[o], px[o + 1], px[o + 2]]; } };
}

const POSTER = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' width='720' height='1280'>" +
  "<rect width='720' height='1280' fill='#e01010'/></svg>");
const TEXT = "Texte fictif de démonstration pour un post avec média. ".repeat(3).trim();
const LONG = ("Ceci est un très long post fictif, écrit seulement pour vérifier la lecture à distance sur un téléviseur : " +
  "aucune ligne ne doit passer sous le bandeau d'aide ni à côté de lui. ").repeat(16).trim();
const ICON = "<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"9\"/></svg>";
const STYLE = `<style>html,body{margin:0;background:#000;color:#e7e9ea;font:15px/1.3 system-ui,sans-serif}
:where(#react-root) div,section,header,main,nav{display:flex;flex-direction:column;flex-shrink:0;min-width:0;min-height:0;position:relative;box-sizing:border-box;align-items:stretch}
:where(#react-root) .row{flex-direction:row}#app{min-height:100vh;width:100%}header[role=banner]{flex-grow:1;align-items:flex-end}header>div{width:275px;height:600px}
main[role=main]{flex-grow:1;flex-shrink:1;align-items:flex-start}.w990{width:990px}.sb{justify-content:space-between;width:100%}
[data-testid=primaryColumn]{max-width:600px;width:100%;flex-grow:1;flex-shrink:1;border-left:1px solid #2f3336;border-right:1px solid #2f3336}
[data-testid=sidebarColumn]{width:350px}.cap{max-width:600px}
article{display:flex;flex-direction:column;flex-shrink:0;min-width:0;padding:12px 16px;border-bottom:1px solid #2f3336}
.av{width:40px;height:40px;margin-right:8px;border-radius:50%;background:#556}.cnt{flex:1 1 0px}
[data-testid=tweetText]{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:40;overflow:hidden}
:where(#react-root) .bar{flex-direction:row;justify-content:space-between;max-width:425px;margin-top:12px}
:where(#react-root) .bar>div{flex-direction:row}.bar button,.bar a{background:none;border:0;color:#71767b;display:flex;align-items:center;height:34px;padding:0 8px;font:13px system-ui;text-decoration:none}
.bar svg{width:19px;height:19px;fill:currentColor}
.frame{margin-top:12px;border-radius:16px;overflow:hidden;border:1px solid #8b98a5}
.fill{position:absolute;top:0;left:0;right:0;bottom:0}
.play{position:absolute;left:50%;top:50%;width:60px;height:60px;margin:-30px;border-radius:50%;background:#1d9bf0;color:#fff;align-items:center;justify-content:center}
.quote{margin-top:12px;border:1px solid #2f3336;border-radius:16px}</style>`;

function slots(id) {
  const b = (t, label, n) => `<div><button data-testid="${t}" type="button" aria-label="${label}" data-otv-btn="${id}-${t}">` +
    `${ICON}<span>${n}</span></button></div>`;
  return b("reply", "Répondre", 2) + b("retweet", "Republier", 3) + b("like", "J'aime", 5) +
    `<div><a href="/fix_${id}/status/1/analytics" aria-label="40 vues" data-otv-btn="${id}-views">${ICON}<span>40</span></a></div>` +
    `<div class="row"><div><button data-testid="bookmark" type="button" aria-label="Enregistrer" data-otv-btn="${id}-bookmark">` +
    `${ICON}</button></div><div><button type="button" aria-label="Partager le post" data-otv-btn="${id}-share">${ICON}</button></div></div>`;
}
const bar = (id) => `<div role="group" class="bar" id="bar-${id}">${slots(id)}</div>`;
const SP = `<div style="padding-bottom:177.78%"></div>`;   // aspect portrait 9:16 (proportionnel à la LARGEUR)
function media(id, st) {
  let inner;
  if (st === "poster") {
    inner = `<div class="fill" role="button" tabindex="0" aria-label="Lire" id="hit-${id}"><div class="fill" id="img-${id}" ` +
      `style="background-image:url(&quot;${POSTER}&quot;);background-size:cover"></div><div class="play" id="play-${id}">▶</div></div>`;
  } else if (st === "card") {
    inner = `<div data-testid="card.wrapper" class="fill"><a href="https://t.co/fixture" role="link" class="fill" id="hit-${id}">` +
      `<img id="img-${id}" alt="" src="${POSTER}" style="width:100%;height:100%;object-fit:cover"></a></div>`;
  } else if (st === "lazy") {
    inner = `<a href="/fix_${id}/status/1/photo/1" role="link" class="fill" id="hit-${id}"><div class="fill">` +
      `<img id="img-${id}" alt="Image" data-lazy="1" style="width:100%;height:100%;object-fit:cover"></div></a>`;
  } else if (st === "defer") {
    inner = `<div class="fill" role="button" tabindex="0" aria-label="Lire" id="hit-${id}" data-defer="1"></div>`;
  } else {
    inner = `<div data-testid="videoPlayer" class="fill" id="hit-${id}"><div data-testid="videoComponent" class="fill">` +
      `<video id="img-${id}" preload="none" muted playsinline poster="${POSTER}" style="width:100%;height:100%;object-fit:cover"></video>` +
      `<div role="button" tabindex="0" aria-label="Lire" class="play" id="play-${id}">▶</div></div></div>`;
  }
  return `<div class="frame" id="frame-${id}">${SP}${inner}</div>`;
}
function head(id) {
  return `<div data-testid="User-Name" class="row"><span>Fixture</span>&nbsp;<a href="/fix_${id}"><span>@fix_${id}</span></a>` +
    `&nbsp;<a href="/fix_${id}/status/1"><time datetime="2026-11-05T10:00:00Z">2 h</time></a></div>`;
}
function text(id, t) {
  return `<div data-testid="tweetText" lang="fr" id="text-${id}">${t}</div><a href="/fix_${id}/status/1" ` +
    `data-testid="tweet-text-show-more-link" id="more-${id}">Show more</a>`;
}
function body(id, st, s) {
  if (s === "grp") { return `<div role="group" id="outer-${id}">${media(id, st)}${bar(id)}</div>`; }
  if (s === "deep") { return `<div role="group" id="outer-${id}"><div>${media(id, st)}${bar(id)}</div></div>`; }
  if (s === "flat") { return `<div role="group" id="outer-${id}">${media(id, st)}<div class="bar" id="bar-${id}">${slots(id)}</div></div>`; }
  return media(id, st) + bar(id);
}
function article(id, inner, attrs) {
  return `<div data-testid="cellInnerDiv"><div><div><article data-testid="tweet" role="article" tabindex="0" data-otv-id="${id}" ` +
    `${attrs || ""}><div class="row"><div class="av"></div><div class="cnt">${inner}</div></div></article></div></div></div>`;
}
const STATES = ["poster", "card", "lazy", "defer", "init"];
const STRUCTS = ["sib", "grp", "deep", "flat"];
const MEDIA_IDS = [];
let POSTS = "";
for (const st of STATES) {
  for (const s of STRUCTS) { const id = st + "_" + s; MEDIA_IDS.push(id); POSTS += article(id, head(id) + text(id, TEXT) + body(id, st, s)); }
}
// post cité à média + rangée dans un groupe mixte (qm) ; post cité à rangée pure (qp)
const quoted = (q, inner) => `<div class="quote" role="link" tabindex="0"><article data-testid="tweet" data-otv-id="${q}" tabindex="-1">` +
  `<div class="row"><div class="cnt">${head(q)}<div data-testid="tweetText" lang="fr">Post cité.</div>${inner}</div></div></article></div>`;
POSTS += article("pqm", head("pqm") + text("pqm", "Je cite un post avec média.") +
  quoted("qm", `<div role="group" id="outer-qm">${media("qm", "poster")}${bar("qm")}</div>`) + bar("pqm"));
MEDIA_IDS.push("qm");
POSTS += article("pqp", head("pqp") + text("pqp", "Je cite un post sans média.") + quoted("qp", bar("qp")) + bar("pqp"));
POSTS += article("rp", head("rp") + text("rp", "Post plat : X remplace le bouton au clic.") + body("rp", "poster", "flat"), 'data-replace="1"');
MEDIA_IDS.push("rp");
POSTS += article("long", head("long") + text("long", LONG) + bar("long"));
POSTS += article("tail", head("tail") + text("tail", "Dernier post.") + bar("tail"));

const SCRIPT = `<script>(function(){var P=${JSON.stringify(POSTER)};window.__acts=[];
var io=new IntersectionObserver(function(es){es.forEach(function(e){if(!e.isIntersecting){return;}io.unobserve(e.target);var t=e.target;
if(t.tagName==="IMG"){t.src=P;return;}var id=t.id.slice(4);var d=document.createElement("div");d.setAttribute("data-testid","videoPlayer");
d.className="fill";d.id="hit-"+id;var c=document.createElement("div");c.setAttribute("data-testid","videoComponent");c.className="fill";
var v=document.createElement("video");v.id="img-"+id;v.preload="none";v.muted=true;v.poster=P;v.style.cssText="width:100%;height:100%;object-fit:cover";
var b=document.createElement("div");b.setAttribute("role","button");b.tabIndex=0;b.className="play";b.id="play-"+id;b.textContent="\\u25b6";
c.appendChild(v);c.appendChild(b);d.appendChild(c);t.replaceWith(d);});});
document.querySelectorAll("img[data-lazy],[data-defer]").forEach(function(e){io.observe(e);});
var NEXT={like:"unlike",unlike:"like",bookmark:"removeBookmark",removeBookmark:"bookmark"};
document.addEventListener("click",function(e){var b=e.target.closest&&e.target.closest("[data-otv-btn]");if(!b){return;}
var art=b.closest("article");window.__acts.push({post:art.getAttribute("data-otv-id"),btn:b.getAttribute("data-otv-btn"),
action:b.getAttribute("data-testid"),trusted:e.isTrusted});var n=NEXT[b.getAttribute("data-testid")];if(!n){return;}
if(art.getAttribute("data-replace")==="1"){var c=b.cloneNode(true);c.setAttribute("data-testid",n);b.replaceWith(c);}
else{b.setAttribute("data-testid",n);}},false);})();</script>`;
function page(kind) {
  const title = kind === "status" ? "Post / X" : "Accueil / X";
  const tl = kind === "status" ? `<div aria-label="Fil de la conversation"><div>${POSTS}</div></div>` :
    `<div aria-label="Fil d'actualités"><div>${POSTS}</div></div>`;
  return `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>${title}</title>${STYLE}</head><body>` +
    `<div id="react-root"><div id="app"><div class="row" style="width:100%"><header role="banner"><div>Nav</div></header>` +
    `<main role="main"><div class="w990"><div class="row sb"><div data-testid="primaryColumn"><div class="cap"><section>` +
    `${tl}</section></div></div><div data-testid="sidebarColumn">T</div></div></div></main></div></div></div>${SCRIPT}</body></html>`;
}
const URLS = { home: "https://x.com/home", status: "https://x.com/fix_poster_sib/status/1" };

(async () => {
  const out = { label: cfg.label, ext: !!cfg.ext, pages: {} };
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "otvxmedia"));
  const args = cfg.ext ? ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext] : ["--disable-extensions"];
  const ctx = await pw.chromium.launchPersistentContext(prof, { executablePath: cfg.chrome, headless: true,
    viewport: { width: 1920, height: 1080 }, colorScheme: "dark", ignoreDefaultArgs: ["--disable-extensions"], args: args });
  out.requests = { served: 0, aborted: [] };
  try {
    if (cfg.ext) {
      let sw = ctx.serviceWorkers()[0];
      if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
      if (sw.url().indexOf("chrome-extension://" + cfg.ext_id + "/") !== 0) { throw new Error("ID inattendu " + sw.url()); }
    }
    await ctx.route("**/*", (r) => {
      const u = new URL(r.request().url());
      if (u.protocol === "https:" && u.hostname === "x.com" && (u.pathname === "/home" || u.pathname === "/fix_poster_sib/status/1")) {
        out.requests.served++;
        return r.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: page(u.pathname === "/home" ? "home" : "status") });
      }
      if (out.requests.aborted.length < 20) { out.requests.aborted.push(u.origin + u.pathname); }
      return r.abort();   // aucun réseau (127.0.0.1 compris)
    });
    const pg = ctx.pages()[0] || await ctx.newPage();
    for (const kind of cfg.pages || ["home", "status"]) {
      const res = { posts: {} };
      out.pages[kind] = res;
      await pg.goto(URLS[kind]);
      if (cfg.ext) {
        await pg.waitForFunction(() => document.documentElement.classList.contains("otvx-tv") &&
          !!document.querySelector(".otvx-help"), null, { timeout: 8000 });
      }
      await sleep(700);
      // ---- 1. aperçus : chaque cadre amené en haut de l'écran (défilement de la fenêtre, sans touche)
      for (const id of MEDIA_IDS) {
        await pg.evaluate((id) => { const f = document.getElementById("frame-" + id);
          window.scrollBy(0, Math.round(f.getBoundingClientRect().top - 140)); }, id);
        await sleep(350);   // IntersectionObserver + passe de rafraîchissement de l'extension
        const m = await pg.evaluate((id) => {
          const R = (e) => { if (!e) { return null; } const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round); };
          const vis = (e) => !!e && e.getBoundingClientRect().width > 0 && e.checkVisibility({ opacityProperty: true, visibilityProperty: true });
          const fr = document.getElementById("frame-" + id), img = document.getElementById("img-" + id),
            hit = document.getElementById("hit-" + id), play = document.getElementById("play-" + id);
          const art = fr.closest("article");
          const hidden = []; for (const e of [fr].concat(Array.from(fr.querySelectorAll("*")))) {
            if (getComputedStyle(e).display === "none") { hidden.push(e.id || e.tagName); } }
          let loaded = false;
          if (img && img.tagName === "IMG") { loaded = img.complete && img.naturalWidth > 0 && !!img.currentSrc; }
          else if (img && img.tagName === "VIDEO") { loaded = !!img.poster; }
          else if (img) { loaded = getComputedStyle(img).backgroundImage.indexOf("data:image") >= 0 && vis(img); }
          const btn = (k) => document.querySelector('[data-otv-btn="' + id + "-" + k + '"]');
          const grp = (e) => e ? e.getAttribute("data-otvx-bar") : null;
          return { frame: R(fr), frame_vis: vis(fr), hidden_in_frame: hidden, hit_vis: vis(hit), play_vis: play ? vis(play) : null,
            media_tag: img ? img.tagName : null, loaded: loaded, media_vis: vis(img),
            more_vis: vis(art.querySelector('[data-testid="tweet-text-show-more-link"]')),
            bar: grp(document.getElementById("bar-" + id)), outer: grp(document.getElementById("outer-" + id)),
            like_vis: vis(btn("like") || btn("unlike")), bookmark_vis: vis(btn("bookmark") || btn("removeBookmark")),
            reply_vis: vis(btn("reply")), share_vis: vis(btn("share")), views_vis: vis(btn("views")) };
        }, id);
        const buf = await pg.screenshot(cfg.shots ? { path: path.join(cfg.shots, `media-${cfg.label}-${kind}-${id}.png`) } : {});
        const img = decodePng(buf);
        // 4 points du cadre VISIBLE (hors ▶ central et hors bandeau d'aide), lus sur la capture
        const f = m.frame, top = Math.max(f[1], 0), bot = Math.min(f[1] + f[3], 1080 - 110);
        const pts = [];
        if (f[2] > 40 && bot - top > 40) {
          for (const fx of [0.2, 0.8]) { for (const fy of [0.2, 0.8]) {
            const x = Math.round(f[0] + f[2] * fx), y = Math.round(top + (bot - top) * fy);
            pts.push({ xy: [x, y], rgb: img.px(x, y), hit: null });
          } }
          for (const p of pts) {
            p.hit = await pg.evaluate(([x, y]) => { const e = document.elementFromPoint(x, y);
              const h = e && e.closest('[id^="hit-"], [id^="img-"]'); return h ? h.id : (e ? e.tagName : null); }, p.xy);
          }
        }
        m.points = pts;
        m.red = pts.length === 4 && pts.every((p) => p.rgb[0] >= 200 && p.rgb[1] <= 60 && p.rgb[2] <= 60);
        res.posts[id] = m;
      }
      // ---- 2. D-pad réel : ↓ jusqu'au post, → (J'aime), → (Enregistrer), ← (sortie sans partage)
      await pg.evaluate(() => window.scrollTo(0, 0));
      await sleep(400);
      const st = () => pg.evaluate(() => {
        const f = document.querySelector("article.otvx-focus"), ae = document.activeElement, act = document.querySelector("[data-otvx-act]");
        const toast = document.querySelector(".otvx-toast");
        return { post: f ? f.getAttribute("data-otv-id") : null, active: ae && ae.getAttribute && ae.getAttribute("data-otv-btn"),
          act: act ? act.getAttribute("data-otv-btn") : null, act_testid: act ? act.getAttribute("data-testid") : null,
          act_is_active: !!act && act === ae, toast: toast && !toast.hidden ? toast.textContent : "",
          acts: window.__acts.slice(), url: location.pathname };
      });
      const key = async (k, ms) => { await pg.keyboard.press(k); await sleep(ms || 130); return st(); };
      res.dpad = {};
      if (cfg.ext) {
        const order = MEDIA_IDS.filter((i) => i !== "qm" && i !== "rp").concat(["pqm", "pqp", "rp"]);
        const OK_ON = new Set(["poster_grp", "defer_deep", "init_flat", "card_flat", "rp", "pqm"]);
        for (const id of order) {
          let s = await st();
          for (let i = 0; i < 16 && s.post !== id; i++) { s = await key("ArrowDown", 110); }
          const d = { reached: s.post === id };
          const r1 = await key("ArrowRight");
          const r2 = await key("ArrowRight");
          d.seq = [r1.act, r2.act];
          d.active_ok = r1.act_is_active && r2.act_is_active;
          d.toast = r1.toast;
          if (OK_ON.has(id) && r2.act) {
            const r3 = await key("ArrowRight");   // retour sur J'aime
            const n0 = r3.acts.length;
            await pg.evaluate(() => { if (document.activeElement) { document.activeElement.__orig = 1; } });
            const ok = await key("Enter", 750);
            d.ok_acts = ok.acts.slice(n0);
            d.after_ok = { act: ok.act, act_testid: ok.act_testid, act_is_active: ok.act_is_active, url: ok.url,
              new_node: await pg.evaluate(() => !!document.activeElement && document.activeElement.__orig === undefined) };
          }
          await key("ArrowLeft", 160);
          const after = await st();
          d.left_toast = after.toast;
          d.left_act = after.act;
          res.dpad[id] = d;
        }
        // ---- 3. bande d'aide : post LONG au focus (début en haut), aucun glyphe sous l'aide, ↓ révèle la suite
        let s = await st();
        for (let i = 0; i < 20 && s.post !== "long"; i++) { s = await key("ArrowDown", 110); }
        await sleep(400);
        const geo = () => pg.evaluate(() => {
          const h = document.querySelector(".otvx-help"), hr = h.getBoundingClientRect();
          const t = document.getElementById("text-long"), art = t.closest("article"), ar = art.getBoundingClientRect();
          const rg = document.createRange(); rg.selectNodeContents(t);
          const tops = []; for (const r of rg.getClientRects()) { if (r.width > 2 && !tops.some((x) => Math.abs(x[0] - r.top) < 3)) {
            tops.push([Math.round(r.top), Math.round(r.bottom)]); } }
          const hrg = document.createRange(); hrg.selectNodeContents(h); const tb = hrg.getBoundingClientRect();
          return { help: [hr.left, hr.top, hr.right, hr.bottom].map(Math.round), help_text: [tb.left, tb.top, tb.right, tb.bottom].map(Math.round),
            post: [ar.left, ar.top, ar.right, ar.bottom].map(Math.round), lines: tops, scroll: Math.round(scrollY),
            focus: (document.querySelector("article.otvx-focus") || {}).getAttribute ? document.querySelector("article.otvx-focus").getAttribute("data-otv-id") : null };
        });
        const g0 = await geo();
        const buf = await pg.screenshot(cfg.shots ? { path: path.join(cfg.shots, `help-${cfg.label}-${kind}.png`) } : {});
        const img = decodePng(buf);
        const lum = (p) => (p[0] + p[1] + p[2]) / 3;
        // bande du bas [1080-56, 1080-6] sur toute la largeur du post, hors boîte du texte de l'aide (± 6 px)
        let maxUnder = 0, nUnder = 0;
        for (let y = 1080 - 56; y <= 1080 - 6; y += 2) {
          for (let x = g0.post[0] + 4; x <= g0.post[2] - 4; x += 2) {
            const tb = g0.help_text;
            if (x >= tb[0] - 6 && x <= tb[2] + 6 && y >= tb[1] - 6 && y <= tb[3] + 6) { continue; }
            maxUnder = Math.max(maxUnder, lum(img.px(x, y))); nUnder++;
          }
        }
        // témoin de lecture du pixel : une ligne de texte AU-DESSUS de l'aide est bien rendue (glyphes clairs)
        const lim = g0.help[1] - 20;
        const above = g0.lines.filter((l) => l[0] >= 96 && l[1] <= lim);
        let maxAbove = 0;
        if (above.length) { const l = above[0]; for (let y = l[0]; y < l[1]; y += 1) {
          for (let x = g0.post[0] + 4; x <= g0.post[2] - 4; x += 2) { maxAbove = Math.max(maxAbove, lum(img.px(x, y))); } } }
        const hiddenIdx = g0.lines.findIndex((l) => l[1] > lim);
        // aucun saut : une passe de rafraîchissement (mutation ailleurs) ne déplace pas la lecture
        await pg.evaluate(() => { const d = document.createElement("div"); d.style.height = "40px";
          document.getElementById("text-tail").after(d); });
        await sleep(700);
        const g1 = await geo();
        const down = await key("ArrowDown", 400);
        const g2 = await geo();
        const line = hiddenIdx >= 0 ? g2.lines[hiddenIdx] : null;
        res.help = { focus: g0.focus, help: g0.help, help_text: g0.help_text, post: g0.post, lines: g0.lines.length,
          lines_behind_help: g0.lines.filter((l) => l[1] > g0.help[1]).length, sampled: nUnder, max_lum_under_help: Math.round(maxUnder),
          max_lum_text_above: Math.round(maxAbove), scroll0: g0.scroll, scroll_after_refresh: g1.scroll, down_post: down.post,
          scroll_after_down: g2.scroll, hidden_line_before: hiddenIdx >= 0 ? g0.lines[hiddenIdx] : null, hidden_line_after: line,
          revealed: !!line && line[0] >= 96 && line[1] <= g2.help[1] - 20 };
      }
    }
  } finally {
    await ctx.close();
    fs.rmSync(prof, { recursive: true, force: true });
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
