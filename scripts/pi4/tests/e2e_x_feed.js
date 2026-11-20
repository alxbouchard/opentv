/* Bout en bout du FIL X à la TV (0.7.14, tests seulement, jamais installé) : Chromium COMPLET réel, extension telle
   qu'installée (ou, pour le témoin « avant », celle de 0.7.13), pages x.com SYNTHÉTIQUES et ANONYMES servies par
   Playwright (aucun réseau, aucun compte, aucune session). Rien n'est lu d'un vrai X.

   Fixture du fil : chaîne d'ancêtres façon react-native-web de X (vues flex colonne, flex-shrink 0, conteneur de 990 px,
   colonne principale max-width 600 px, ligne flex) et, par défaut, un plafond SUPPLÉMENTAIRE de 600 px sous la colonne
   (?plain : sans ce plafond). Ce plafond est une HYPOTHÈSE qui reproduit la photo terrain (post ≈ 1/3 de l'écran en
   0.7.13) ; le DOM réel de X n'a pas pu être mesuré (x.com refuse le navigateur sans tête déconnecté).
   Posts : p1 court (J'aime + Enregistrer + barres annexes de X), p2 déjà aimé/enregistré (X REMPLACE le bouton au
   changement d'état), p3 LONG + vraie vidéo H.264 par MSE dans un enveloppant [role=group] (hypothèse « vidéos
   invisibles ») avec commandes [role=group], p4 post cité/imbriqué (rangée du post cité), p5 sans Enregistrer + vidéo
   H.264 progressive, p6 aucune action + photo, p7 vidéo VP9 progressive. Les vidéos (640×360, 3 s, première image
   rouge) ne sont JAMAIS lancées : preload seulement, aucun autoplay.
   Instrumentation de la page : chaque clic sur un bouton d'action (fiable ou non) est journalisé (__acts) ; X simulé
   bascule l'état comme le vrai (like <-> unlike, bookmark <-> removeBookmark).
   argv[2] = module playwright ; argv[3] = JSON {chrome, ext, ext_id, mode: "geom"|"full", label, shots, video_dir}.
   Sortie : une ligne JSON. */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const pw = require(process.argv[2]);
const cfg = JSON.parse(process.argv[3]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// décodeur PNG minimal (8 bits, RVB/RVBA, non entrelacé : captures de Chromium) -> px(x, y) = [r, g, b]
function decodePng(buf) {
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

const LONG = "Ceci est un long post de démonstration, entièrement fictif, écrit pour mesurer la lecture à distance " +
  "sur un téléviseur : les phrases doivent tenir sur des lignes pleines, sans mots isolés ni retours forcés. ";
const STYLE = "<style>html,body{margin:0;background:#000;color:#e7e9ea;font:15px/1.3 system-ui,sans-serif}" +
  ":where(#react-root) div,section,header,main,nav{display:flex;flex-direction:column;flex-shrink:0;min-width:0;min-height:0;" +
  "position:relative;box-sizing:border-box;align-items:stretch}" +
  ".row{flex-direction:row}#app{min-height:100vh;width:100%}" +
  "header[role=banner]{flex-grow:1;align-items:flex-end}header>div{width:275px;height:600px}" +
  "main[role=main]{flex-grow:1;flex-shrink:1;align-items:flex-start}" +
  ".w990{width:990px}.sb{justify-content:space-between;width:100%}" +
  "[data-testid=primaryColumn]{max-width:600px;width:100%;flex-grow:1;flex-shrink:1;border-left:1px solid #2f3336;" +
  "border-right:1px solid #2f3336}[data-testid=sidebarColumn]{width:350px}.cap{max-width:600px}" +
  ".sticky{position:sticky;top:0;height:53px;z-index:3;background:rgba(0,0,0,.65);flex-direction:row;" +
  "align-items:center;justify-content:space-around}" +
  "article{display:flex;flex-direction:column;flex-shrink:0;min-width:0;padding:12px 16px;border-bottom:1px solid #2f3336}" +
  ".av{width:40px;height:40px;margin-right:8px;border-radius:50%;background:#556}.cnt{flex:1 1 0px}" +
  "[data-testid=tweetText]{display:block;white-space:pre-wrap;word-wrap:break-word}" +
  "[role=group].bar{flex-direction:row;justify-content:space-between;max-width:425px;margin-top:12px}" +
  ".bar>div{flex-direction:row}.bar button,.bar a{background:none;border:0;color:#71767b;font:13px system-ui;" +
  "display:flex;flex-direction:row;align-items:center;height:34px;padding:0 8px;text-decoration:none}" +
  ".bar svg{width:19px;height:19px;fill:currentColor}" +
  ".media{margin-top:12px;border-radius:16px;overflow:hidden;border:1px solid #2f3336}" +
  ".ctl{position:absolute;left:0;right:0;bottom:0;flex-direction:row;align-items:center;height:44px;" +
  "background:linear-gradient(transparent,rgba(0,0,0,.7));padding:0 10px}" +
  ".ctl button{background:#0008;border:0;color:#fff;font:15px system-ui;height:32px;min-width:32px;margin-right:8px}" +
  ".seek{flex:1 1 0px;height:6px;background:#fff6}" +
  ".quote{margin-top:12px;border:1px solid #2f3336;border-radius:16px}</style>";
const ICON = "<svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"9\"/></svg>";

function bar(id, kinds) {
  const b = (t, label, n) => `<div><button data-testid="${t}" type="button" aria-label="${label}" data-otv-btn="${id}-${t}">` +
    `${ICON}<span>${n}</span></button></div>`;
  const parts = [];
  for (const k of kinds) {
    if (k === "reply") { parts.push(b("reply", "2 réponses. Répondre", 2)); }
    if (k === "retweet") { parts.push(b("retweet", "3 republications. Republier", 3)); }
    if (k === "like") { parts.push(b("like", "5 J'aime. J'aime", 5)); }
    if (k === "unlike") { parts.push(b("unlike", "6 J'aime. Aimé", 6)); }
    if (k === "views") { parts.push(`<div><a href="/${id}/status/1/analytics" aria-label="40 vues">${ICON}<span>40</span></a></div>`); }
    if (k === "bookmark" || k === "removeBookmark") {
      parts.push(`<div class="row"><div><button data-testid="${k}" type="button" aria-label="${k === "bookmark" ?
        "Enregistrer" : "Enregistré"}" data-otv-btn="${id}-${k}">${ICON}</button></div>` +
        `<div><button type="button" aria-label="Partager le post" data-otv-btn="${id}-share">${ICON}</button></div></div>`);
    }
  }
  return `<div role="group" class="bar" aria-label="actions" id="bar-${id}">${parts.join("")}</div>`;
}
function video(id, src, mse, wrapGroup) {
  const player = `<div data-testid="videoPlayer"><div style="padding-bottom:56.25%"></div>` +
    `<div data-testid="videoComponent" style="position:absolute;top:0;left:0;right:0;bottom:0">` +
    `<video id="v-${id}" preload="auto" playsinline muted crossorigin="anonymous" ${mse ? `data-mse="${src}"` : `src="${src}"`} ` +
    `style="width:100%;height:100%;object-fit:contain;background:#000"></video></div>` +
    `<div role="group" aria-label="Commandes vidéo" class="ctl" id="ctl-${id}"><button type="button" aria-label="Lire" ` +
    `id="play-${id}">▶</button><button type="button" aria-label="Activer le son" id="mute-${id}">♪</button>` +
    `<div role="slider" aria-label="Position" tabindex="0" class="seek" id="seek-${id}"></div></div></div>`;
  return `<div class="media" id="media-${id}">${wrapGroup ? `<div role="group" aria-label="Vidéo intégrée" id="wrap-${id}">` +
    player + "</div>" : player}</div>`;
}
const PHOTO = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' width='1600' " +
  "height='900'><rect width='1600' height='900' fill='#1d6'/><circle cx='800' cy='450' r='300' fill='#fd0'/></svg>");
function photo(id) {
  return `<div class="media"><div data-testid="tweetPhoto" id="photo-${id}"><div style="padding-bottom:56.25%"></div>` +
    `<div id="photobg-${id}" style="position:absolute;top:0;left:0;right:0;bottom:0;background-image:url(&quot;${PHOTO}&quot;);` +
    `background-size:cover;background-position:center"></div><img alt="Image" src="${PHOTO}" style="position:absolute;` +
    `top:0;left:0;width:100%;height:100%;opacity:0"></div></div>`;
}
function post(id, o) {
  const head = `<div data-testid="User-Name" class="row"><span>${o.name}</span>&nbsp;<a href="/${o.handle}"><span>@${o.handle}` +
    `</span></a>&nbsp;<span>·</span>&nbsp;<a href="/${o.handle}/status/${o.n}" id="time-${id}"><time ` +
    `datetime="2026-11-05T10:00:00Z">2 h</time></a></div>`;
  return `<div data-testid="cellInnerDiv"><div><div><article data-testid="tweet" tabindex="0" role="article" ` +
    `data-otv-id="${id}" ${o.replace ? 'data-replace="1"' : ""}><div><div class="row"><div class="av"></div>` +
    `<div class="cnt">${head}<div data-testid="tweetText" lang="fr" id="text-${id}">${o.text}</div>${o.extra || ""}` +
    `${o.bar ? bar(id, o.bar) : ""}</div></div></div></article></div></div></div>`;
}
const QUOTED = `<div class="quote" role="link" tabindex="0"><article data-testid="tweet" data-otv-id="q4" tabindex="-1">` +
  `<div class="row"><div class="cnt"><div data-testid="User-Name" class="row"><span>Cité</span>&nbsp;<a href="/cite">` +
  `<span>@cite</span></a>&nbsp;<a href="/cite/status/40"><time datetime="2026-11-04T10:00:00Z">1 j</time></a></div>` +
  `<div data-testid="tweetText" lang="fr" id="text-q4">Post cité, avec sa propre rangée d'actions.</div>` +
  `${bar("q4", ["reply", "like", "bookmark"])}</div></div></article></div>`;
const VID = "https://video.twimg.com/otv/";
const POSTS = [
  post("p1", { name: "Bob", handle: "bob", n: 1, text: "Premier post, court.",
               bar: ["reply", "retweet", "like", "views", "bookmark"] }),
  post("p2", { name: "Alice Martin", handle: "alice_m", n: 2, replace: true,
               text: "Deuxième post : déjà aimé et déjà enregistré par le compte (état affiché par X).",
               bar: ["reply", "retweet", "unlike", "views", "removeBookmark"] }),
  post("p3", { name: "Chloé", handle: "chloe", n: 3, text: LONG.repeat(9).trim(),
               extra: video("p3", VID + "h264_frag.mp4", true, true), bar: ["reply", "retweet", "like", "views", "bookmark"] }),
  post("p4", { name: "Dan", handle: "dan", n: 4, text: "Je cite ce post :", extra: QUOTED,
               bar: ["reply", "retweet", "like", "views", "bookmark"] }),
  post("p5", { name: "Eve", handle: "eve", n: 5, text: "Post sans bouton Enregistrer, avec une vidéo H.264.",
               extra: video("p5", VID + "h264_prog.mp4", false, false), bar: ["reply", "retweet", "like", "views"] }),
  post("p6", { name: "Fred", handle: "fred", n: 6, text: "Post sans aucune action (photo seulement).", extra: photo("p6") }),
  post("p7", { name: "Gaëlle", handle: "gaelle", n: 7, text: "Vidéo VP9 progressive.",
               extra: video("p7", VID + "vp9.webm", false, false), bar: ["reply", "retweet", "like", "views", "bookmark"] })
].join("");
const SCRIPT = `<script>(function(){window.__acts=[];window.__focus=0;
window.__kf=[];window.addEventListener("keydown",function(e){var x=document.activeElement;window.__kf.push(e.key+":"+(x&&x.getAttribute&&x.getAttribute("data-otv-btn")||(x?x.tagName:"null")));},true);
var IDS=["like","unlike","bookmark","removeBookmark","reply","retweet"];var NEXT={like:"unlike",unlike:"like",bookmark:"removeBookmark",removeBookmark:"bookmark"};
window.addEventListener('keydown',function(e){if(e.key==='Enter' && window.__recycleAtOK){window.__recycleAtOK=false;window.__reuse();}},true);
document.addEventListener("focusin",function(e){if(e.target.matches&&e.target.matches("[data-otv-btn]")){window.__focus++;}},true);
document.addEventListener("click",function(e){var b=e.target.closest&&e.target.closest("[data-testid]");if(!b){return;}
var t=b.getAttribute("data-testid");if(IDS.indexOf(t)<0){return;}var art=b.closest("article");
window.__acts.push({post:art.getAttribute("data-otv-id"),action:t,trusted:e.isTrusted});var n=NEXT[t];if(!n){return;}
if(art.getAttribute("data-replace")==="1"){var c=b.cloneNode(true);c.setAttribute("data-testid",n);b.replaceWith(c);}
else{b.setAttribute("data-testid",n);}},false);
window.__reuse=function(){var a=document.querySelector('[data-otv-id="p1"]');a.querySelector("#time-p1").setAttribute("href","/zoe/status/99");
a.querySelector("#text-p1").textContent="Autre post (DOM réutilisé par X).";a.setAttribute("data-otv-id","p1b");};
Array.prototype.forEach.call(document.querySelectorAll("video[data-mse]"),function(v){
var T='video/mp4; codecs="avc1.64001e"';if(!window.MediaSource||!MediaSource.isTypeSupported(T)){v.setAttribute("data-mse-err","unsupported");return;}
var ms=new MediaSource();v.src=URL.createObjectURL(ms);ms.addEventListener("sourceopen",function(){var sb=ms.addSourceBuffer(T);
fetch(v.getAttribute("data-mse")).then(function(r){return r.arrayBuffer();}).then(function(buf){sb.addEventListener("updateend",function(){
try{ms.endOfStream();}catch(e){}});sb.appendBuffer(buf);}).catch(function(){v.setAttribute("data-mse-err","fetch");});});});
})();</script>`;
function feed(cap) {
  const tl = `<div class="sticky"><span>Pour vous</span><span>Abonnements</span></div><div${cap ? ' class="cap"' : ""}>` +
    `<section role="region"><div aria-label="Fil d'actualités"><div>${POSTS}</div></div></section></div>`;
  return `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Accueil / X</title>${STYLE}</head><body>` +
    `<div id="react-root"><div id="app"><div class="row" style="width:100%"><header role="banner"><div>Navigation</div>` +
    `</header><main role="main"><div class="w990"><div class="row sb"><div data-testid="primaryColumn">${tl}</div>` +
    `<div data-testid="sidebarColumn">Tendances</div></div></div></main></div></div></div>${SCRIPT}</body></html>`;
}
const STATUS = (n) => `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Post / X</title>${STYLE}</head>` +
  `<body><div id="react-root"><div id="app"><div class="row"><main role="main"><div class="w990"><div class="row sb">` +
  `<div data-testid="primaryColumn">${post("s" + n, { name: "Vue", handle: "vue", n: n, text: "Vue du post " + n + ".",
  bar: ["reply", "like", "bookmark"] })}</div></div></div></main></div></div></div>${SCRIPT}</body></html>`;
// fenêtre modale VISIBLE de X au-dessus du fil : → n'y fait rien pour le fil ; nav.js la parcourt
const DIALOG = `<div id="layers"><div role="dialog" aria-modal="true" id="dlg" style="position:fixed;top:220px;left:610px;` +
  `width:700px;height:300px;background:#202327;color:#fff;z-index:10;padding:20px"><h2>Notifications</h2>` +
  `<button id="allow" type="button">Autoriser</button><button id="notnow" type="button">Plus tard</button></div></div>`;
const LOGIN = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Connexion / X</title>${STYLE}</head>` +
  `<body><div role="dialog" aria-modal="true"><h1>Connexion à X</h1><input id="user" name="text" autofocus ` +
  `autocomplete="username"><button id="next" type="button">Suivant</button></div>` +
  `<div data-testid="sidebarColumn" id="sb">Pied</div></body></html>`;

(async () => {
  const out = { label: cfg.label, mode: cfg.mode };
  const prof = fs.mkdtempSync(path.join(os.tmpdir(), "otvxfeed"));
  const ctx = await pw.chromium.launchPersistentContext(prof, {
    executablePath: cfg.chrome, headless: true, viewport: { width: 1920, height: 1080 }, colorScheme: "dark",
    ignoreDefaultArgs: ["--disable-extensions"],
    args: ["--disable-extensions-except=" + cfg.ext, "--load-extension=" + cfg.ext, "--autoplay-policy=no-user-gesture-required"]
  });
  try {
    let sw = ctx.serviceWorkers()[0];
    if (!sw) { sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 }); }
    if (sw.url().indexOf("chrome-extension://" + cfg.ext_id + "/") !== 0) { throw new Error("ID inattendu " + sw.url()); }
    await ctx.route(/^https:\/\/video\.twimg\.com\/otv\//, (route) => {
      const name = path.basename(new URL(route.request().url()).pathname);
      const body = fs.readFileSync(path.join(cfg.video_dir, name));
      return route.fulfill({ status: 200, body: body, headers: { "Content-Type": name.endsWith(".webm") ? "video/webm" : "video/mp4",
        "Access-Control-Allow-Origin": "*", "Accept-Ranges": "none" } });
    });
    await ctx.route(/^https:\/\/(x|twitter)\.com\//, (route) => {
      const u = new URL(route.request().url());
      const m = /\/status\/(\d+)$/.exec(u.pathname);
      const body = u.pathname.indexOf("/i/flow/login") === 0 ? LOGIN : m ? STATUS(m[1]) :
        u.pathname === "/settings/dialog" ? feed(true).replace("</body>", DIALOG + "</body>") : feed(!u.searchParams.has("plain"));
      return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: body });
    });
    const page = ctx.pages()[0] || await ctx.newPage();
    const waitFeed = () => page.waitForFunction(() => document.documentElement.classList.contains("otvx-tv") &&
      !!document.querySelector(".otvx-help"), null, { timeout: 8000 });
    const waitVideos = () => page.waitForFunction(() => Array.from(document.querySelectorAll("video"))
      .every((v) => v.readyState >= 2 || v.getAttribute("data-mse-err") || v.error), null, { timeout: 15000 }).catch(() => null);

    // ---- géométrie (avant/après) : colonne, post, ligne de texte, médias, commandes vidéo, actions, aide
    const geom = () => page.evaluate(() => {
      const R = (e) => { if (!e) { return null; } const r = e.getBoundingClientRect();
        return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; };
      const vis = (e) => !!e && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 &&
        e.checkVisibility({ opacityProperty: true, visibilityProperty: true });
      const art = (id) => document.querySelector('article[data-otv-id="' + id + '"]');
      const lines = (id) => {   // lignes RÉELLES du texte de p3 (boîtes de ligne) et caractères par ligne
        const t = document.getElementById("text-" + id);
        const rg = document.createRange(); rg.selectNodeContents(t);
        const tops = new Set(Array.from(rg.getClientRects()).filter((r) => r.width > 2).map((r) => Math.round(r.top)));
        const n = tops.size;
        return { lines: n, chars_per_line: n ? Math.round(t.textContent.length / n) : 0, width: R(t)[2],
                 font: getComputedStyle(t).fontSize };
      };
      const vids = {};
      for (const id of ["p3", "p5", "p7"]) {
        const v = document.getElementById("v-" + id);
        const c = document.getElementById("ctl-" + id);
        vids[id] = { rect: R(v), visible: vis(v), ready: v.readyState, vw: v.videoWidth, vh: v.videoHeight,
                     paused: v.paused, t: v.currentTime, autoplay: v.autoplay, err: v.getAttribute("data-mse-err") || (v.error ? v.error.code : null),
                     controls_visible: vis(c), play_visible: vis(document.getElementById("play-" + id)),
                     mute_visible: vis(document.getElementById("mute-" + id)), seek_visible: vis(document.getElementById("seek-" + id)),
                     wrap_display: document.getElementById("wrap-" + id) ? getComputedStyle(document.getElementById("wrap-" + id)).display : null };
      }
      const btn = (k) => document.querySelector('[data-otv-btn="' + k + '"]');
      const help = document.querySelector(".otvx-help");
      return {
        vw: innerWidth, col: R(document.querySelector('[data-testid="primaryColumn"]')),
        p1: R(art("p1")), p3: R(art("p3")), lines: lines("p3"),
        photo: { box: R(document.getElementById("photo-p6")), bg: R(document.getElementById("photobg-p6")) },
        videos: vids,
        actions: { like: vis(btn("p1-like")), bookmark: vis(btn("p1-bookmark")), reply: vis(btn("p1-reply")),
                   retweet: vis(btn("p1-retweet")), share: vis(btn("p1-share")),
                   views: vis(document.querySelector('#bar-p1 a[aria-label="40 vues"]')),
                   unlike_p2: vis(btn("p2-unlike")), remove_p2: vis(btn("p2-removeBookmark")),
                   quote_like: vis(btn("q4-like")), quote_reply: vis(btn("q4-reply")), quote_bookmark: vis(btn("q4-bookmark")),
                   quote_bar: vis(document.getElementById("bar-q4")) },
        help: help ? { rect: R(help), text: help.textContent } : null,
        lanes: document.querySelectorAll("[data-otvx-lane]").length,
        sidebar: getComputedStyle(document.querySelector('[data-testid="sidebarColumn"]')).display
      };
    });
    // pixel au centre de chaque vidéo lu sur la CAPTURE réelle (PNG décodé ici), plus l'image décodée de l'élément
    // vidéo lui-même (canvas) : la première image de la fixture est rouge (252, 0, 0)
    const pixels = async (file) => {
      const buf = await page.screenshot({ path: file });
      const sc = await page.evaluate(() => ["p3", "p5", "p7"].map((id) => {
        const v = document.getElementById("v-" + id), r = v.getBoundingClientRect();
        let frame = null;
        try { const c = document.createElement("canvas"); c.width = 32; c.height = 18; const g = c.getContext("2d");
          g.drawImage(v, 0, 0, 32, 18); frame = Array.from(g.getImageData(16, 9, 1, 1).data.slice(0, 3)); } catch (e) { frame = String(e); }
        return { id: id, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), frame: frame,
                 inview: r.width > 0 && r.top + r.height / 2 > 0 && r.top + r.height / 2 < innerHeight };
      }));
      const img = decodePng(buf);
      for (const p of sc) { p.screen = p.inview ? img.px(p.x, p.y) : null; }
      return { points: sc };
    };

    await page.goto("https://x.com/home");
    await waitFeed();
    await waitVideos();
    await sleep(600);
    out.geom = await geom();
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, `x-feed-${cfg.label}.png`) }); }
    // vidéos amenées à l'écran une à une (sans touche : défilement de la fenêtre), capture et point central
    out.video_shots = {};
    for (const id of ["p3", "p5", "p7"]) {
      await page.evaluate((i) => { const v = document.getElementById("v-" + i); const r = v.getBoundingClientRect();
        window.scrollBy(0, Math.round(r.top + r.height / 2 - innerHeight / 2)); }, id);
      await sleep(250);
      const f = cfg.shots ? path.join(cfg.shots, `x-video-${id}-${cfg.label}.png`) : path.join(prof, "v.png");
      const px = await pixels(f);
      out.video_shots[id] = { file: f, point: px.points.find((p) => p.id === id),
        vis: await page.evaluate((i) => { const v = document.getElementById("v-" + i), r = v.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return { rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                   hit_video: hit === v || (!!hit && !!hit.closest('[data-testid="videoComponent"]')) }; }, id) };
    }
    out.geom_plain = null;
    if (cfg.mode === "geom") {
      await page.goto("https://x.com/home?plain=1");
      await waitFeed();
      await sleep(400);
      out.geom_plain = await page.evaluate(() => { const a = document.querySelector('article[data-otv-id="p1"]').getBoundingClientRect();
        return [Math.round(a.left), Math.round(a.width)]; });
      process.stdout.write(JSON.stringify(out) + "\n");
      return;
    }

    // ---------------------------------------------------------------- interactions (extension 0.7.14)
    await page.goto("https://x.com/home?plain=1");
    await waitFeed();
    await sleep(400);
    out.geom_plain = await page.evaluate(() => { const a = document.querySelector('article[data-otv-id="p1"]').getBoundingClientRect();
      return [Math.round(a.left), Math.round(a.width)]; });
    await page.goto("https://x.com/home");
    await waitFeed();
    await waitVideos();
    await sleep(400);
    const st = () => page.evaluate(() => {
      const ae = document.activeElement, f = document.querySelector("article.otvx-focus");
      const act = document.querySelector("[data-otvx-act]"), help = document.querySelector(".otvx-help");
      const toast = document.querySelector(".otvx-toast");
      const rr = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom].map(Math.round); };
      const inter = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
      const hr = help ? rr(help) : null;
      let label = null, ring = null;
      if (act) { label = getComputedStyle(act, "::after").content; const c = getComputedStyle(act);
        ring = c.outlineStyle + " " + c.outlineWidth + " " + c.outlineColor; }
      return { post: f ? f.getAttribute("data-otv-id") : null, active: ae && ae.getAttribute("data-otv-btn") || (ae && ae.tagName),
               act: act ? act.getAttribute("data-otv-btn") : null, act_testid: act ? act.getAttribute("data-testid") : null,
               act_is_active: !!act && act === ae, acting: !!(f && f.hasAttribute("data-otvx-acting")),
               post_outline: f ? getComputedStyle(f).outlineStyle : null, label: label, ring: ring,
               act_rect: act ? rr(act) : null, help_rect: hr, act_under_help: !!(act && hr && inter(rr(act), hr)),
               act_in_view: !!act && rr(act)[1] >= 0 && rr(act)[3] <= innerHeight,
               post_rect: f ? rr(f) : null, help: help ? help.textContent : "",
               toast: toast && !toast.hidden ? toast.textContent : "", acts: window.__acts.slice(), focus_events: window.__focus,
               url: location.pathname, scroll: Math.round(scrollY) };
    });
    const clearToast = () => page.evaluate(() => { const t = document.querySelector(".otvx-toast"); if (t) { t.hidden = true; t.textContent = ""; } });
    const key = async (k, ms) => { await page.keyboard.press(k); await sleep(ms || 120); return st(); };
    const s = {};
    s.initial = await st();
    s.down1 = await key("ArrowDown");                    // p1
    s.right1 = await key("ArrowRight");                  // mode actions : J'aime de p1 (focus seul)
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-feed-actions.png") }); }
    s.right2 = await key("ArrowRight");                  // Enregistrer de p1
    s.right3 = await key("ArrowRight");                  // circule : J'aime
    // touches SYNTHÉTIQUES de la page : rien (ni déplacement ni activation)
    await page.evaluate(() => { for (const k of ["ArrowRight", "Enter", "ArrowLeft"]) {
      document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })); } });
    await sleep(200);
    s.synthetic = await st();
    s.focus_only_acts = s.synthetic.acts.length;          // zéro activation par le focus seul
    s.ok1 = await key("Enter", 300);                     // UNE activation native : like -> unlike
    // OK tenu : répétitions absorbées (une activation par appui)
    await page.keyboard.down("Enter"); await page.keyboard.down("Enter"); await page.keyboard.down("Enter");
    await page.keyboard.up("Enter"); await sleep(300);
    s.ok_held = await st();
    s.ok2 = await key("Enter", 300);                     // UNE activation de plus : unlike -> like (retrait)
    // ← en mode actions : retour au post SANS partage ; ← tenu et second ← immédiat absorbés ; ← plus tard : partage
    await clearToast();
    s.left_exit = await key("ArrowLeft", 60);
    await page.keyboard.down("ArrowLeft"); await page.keyboard.down("ArrowLeft"); await page.keyboard.up("ArrowLeft");
    await sleep(60);
    s.left_quick = await key("ArrowLeft", 400);
    await sleep(500);
    s.left_share = await key("ArrowLeft", 900);          // partage (sans clé /tv : refus honnête, rien n'est envoyé)
    // ↓ en mode actions : navigation normale (post suivant), mode quitté
    await page.keyboard.press("ArrowRight"); await sleep(120);
    s.down_from_act = await key("ArrowDown", 200);       // p2
    // p2 déjà aimé/enregistré : libellés d'état ; X REMPLACE le bouton après OK : focus remis au même rang
    s.p2_right = await key("ArrowRight");
    s.p2_right2 = await key("ArrowRight");
    await page.evaluate(() => { document.querySelector('[data-otv-btn="p2-removeBookmark"]').__orig = 1; });
    s.p2_ok = await key("Enter", 700);                   // removeBookmark -> bookmark (nouveau nœud), refocalisé
    s.p2_new_node = await page.evaluate(() => document.activeElement.__orig === undefined &&
      document.activeElement.getAttribute("data-otv-btn") === "p2-removeBookmark");
    // fenêtre de remplacement : X remplace le bouton au OK et le focus retombe sur la page AVANT le refresh (250 ms) ;
    // ← immédiat : sortie SANS partage ; OK immédiat : ni ouverture ni activation ; puis navigation normale
    await clearToast();
    const n0 = (await st()).acts.length;
    await page.keyboard.press("Enter"); await page.keyboard.press("ArrowLeft");   // même tâche : aucun refresh entre
    await sleep(100);
    s.race_left = await st();
    s.race_left_kf = await page.evaluate(() => window.__kf.slice(-2));
    s.race_left_acts = s.race_left.acts.length - n0;
    await sleep(900);
    s.race_left_after = await st();
    await page.keyboard.press("ArrowRight"); await sleep(150);                   // de nouveau en mode actions (rang 0)
    const n1 = (await st()).acts.length;
    await page.keyboard.press("Enter"); await page.keyboard.press("Enter");
    await sleep(100);
    s.race_ok = await st();
    s.race_ok_kf = await page.evaluate(() => window.__kf.slice(-2));
    s.race_ok_acts = s.race_ok.acts.length - n1;
    await sleep(500);
    s.race_ok_after = await st();
    if (s.race_ok_after.url !== "/home") {   // (mutant seulement) post ouvert : retour au fil pour finir le parcours
      await page.goto("https://x.com/home"); await waitFeed(); await sleep(400);
      for (let i = 0; i < 3 && (await st()).post !== "p2"; i++) { await page.keyboard.press("ArrowDown"); await sleep(150); }
    }
    // p3 : post LONG (plus haut que l'écran) avec vidéo : début visible, → amène son bouton au-dessus de l'aide
    s.p3 = await key("ArrowDown", 200);
    s.p3_right = await key("ArrowRight", 200);
    if (cfg.shots) { await page.screenshot({ path: path.join(cfg.shots, "x-feed-long-actions.png") }); }
    s.p3_up = await key("ArrowUp", 200);                 // quitte le mode actions ; post long : défile d'abord dedans
    s.p3_up2 = await key("ArrowUp", 200);
    // p4 : post cité/imbriqué : → circule SEULEMENT entre les boutons de p4 (jamais ceux du post cité q4)
    for (let i = 0; i < 6 && (await st()).post !== "p4"; i++) { await page.keyboard.press("ArrowDown"); await sleep(150); }
    s.p4 = await st();
    const p4seq = [];
    for (let i = 0; i < 4; i++) { p4seq.push((await key("ArrowRight")).act); }
    s.p4_seq = p4seq;
    // p5 : pas de bouton Enregistrer : → reste sur J'aime ; p6 : aucune action : message, aucun focus de bouton
    await key("ArrowLeft", 800);
    s.p5 = await key("ArrowDown", 200);
    s.p5_seq = [(await key("ArrowRight")).act, (await key("ArrowRight")).act];
    await key("ArrowLeft", 800);
    await clearToast();
    for (let i = 0; i < 4 && (await st()).post !== "p6"; i++) { await page.keyboard.press("ArrowDown"); await sleep(200); }
    s.p6 = await st();
    s.p6_right = await key("ArrowRight", 200);
    // refresh qui ANNULE (restauration impossible : X retire toute la rangée du post choisi) : ← juste après absorbé
    for (let i = 0; i < 6 && (await st()).post !== "p7"; i++) { await page.keyboard.press("ArrowDown"); await sleep(200); }
    await key("ArrowRight", 150);
    s.p7_act = (await st()).act;
    await clearToast();
    await page.evaluate(() => document.getElementById("bar-p7").remove());
    await page.waitForFunction(() => !document.querySelector('[data-otvx-acting]')); // refresh a bien annulé
    s.cancel_left = await key("ArrowLeft", 300);
    for (let i = 0; i < 4 && (await st()).post === "p7"; i++) { await page.keyboard.press("ArrowUp"); await sleep(200); }
    s.cancel_up = await st();                                                   // navigation normale ensuite
    // DOM réutilisé : J'aime de p1 choisi, puis X recycle l'article pour un AUTRE post : OK immédiat bloqué
    await page.evaluate(() => window.scrollTo(0, 0)); await sleep(150);
    for (let i = 0; i < 8 && (await st()).post !== "p1"; i++) { await page.keyboard.press("ArrowUp"); await sleep(150); }
    await key("ArrowRight");
    const beforeReuse = (await st()).acts.length;
    await page.evaluate(() => {
      window.__reuseKeys = 0;
      document.activeElement.addEventListener('keydown', () => window.__reuseKeys++, {once:true});
      // Recycle at capture, synchronously before x.js, not one timer tick before Enter.
      window.__recycleAtOK = true;
    });
    s.reuse_ok = await key("Enter", 400);
    s.reuse_acts_delta = s.reuse_ok.acts.length - beforeReuse;
    s.reuse_site_keys = await page.evaluate(() => window.__reuseKeys);
    // OK sur le post : ouvre le permalien ; Retour (nav.js) : revient au fil. Chaque touche part seulement après le
    // signal de l'état qu'elle suppose, jamais après un délai fixe : Échap seulement une fois le permalien CHARGÉ et
    // l'extension initialisée (html.otvx-tv posé par x.js à document_idle, donc nav.js — document_start, même
    // injection — écoute déjà) ; mesures seulement une fois le fil revenu, initialisé et ses vidéos prêtes. Retour sans
    // effet, ou filet de nav.js (BACK_FAILSAFE_MS -> /tv) : échec EXPLICITE à cette étape, jamais rattrapé.
    // Échec : URL courante, journal des navigations du cadre principal et capture, pour distinguer « Retour sans effet »
    // (resté sur le permalien) de « filet vers /tv » ou d'une ouverture manquée.
    const navs = [], t0 = Date.now();
    page.on("framenavigated", (f) => { if (f === page.mainFrame()) { navs.push((Date.now() - t0) + "ms " + f.url()); } });
    const reach = (fn, what, ms) => page.waitForFunction(fn, null, { timeout: ms }).catch(async (e) => {
      const shot = cfg.shots ? path.join(cfg.shots, `x-feed-back-fail-${cfg.label}.png`) : "";
      if (shot) { await page.screenshot({ path: shot }).catch(() => null); }
      throw new Error(what + " — page : " + page.url() + " — navigations : " + JSON.stringify(navs.slice(-6)) +
        (shot ? " — capture : " + shot : "") + " — " + String(e && e.message || e).split("\n")[0]); });
    await key("ArrowDown", 150); await key("ArrowUp", 150);
    await page.keyboard.press("Enter");
    await reach(() => location.pathname === "/zoe/status/99" && document.readyState === "complete" &&
      document.documentElement.classList.contains("otvx-tv"), "OK : permalien non ouvert ou non initialisé", 8000);
    s.open = await st();
    await page.keyboard.press("Escape");
    await reach(() => location.pathname === "/home", "Retour : fil /home non atteint", 5000);
    await reach(() => document.readyState === "complete" && document.documentElement.classList.contains("otvx-tv") &&
      !!document.querySelector(".otvx-help") && ["p3", "p5", "p7"].every((id) => {
        const v = document.getElementById("v-" + id); return !!v && (v.readyState >= 2 || v.getAttribute("data-mse-err") || v.error); }),
      "Retour : fil revenu mais non initialisé (aide, vidéos)", 15000);
    s.back = await st();
    // vidéos : jamais lancées par l'extension (aucun autoplay), commandes natives intactes
    s.videos_after = await page.evaluate(() => ["p3", "p5", "p7"].map((id) => { const v = document.getElementById("v-" + id);
      return { id: id, paused: v.paused, t: v.currentTime, ready: v.readyState }; }));
    out.s = s;

    // fenêtre modale au-dessus du fil : → ne focalise aucun bouton du fil ; nav.js garde la fenêtre
    await page.goto("https://x.com/settings/dialog");
    await page.waitForFunction(() => document.documentElement.classList.contains("otvx-tv"), null, { timeout: 8000 });
    await sleep(500);
    await page.keyboard.press("ArrowDown"); await sleep(150);
    await page.keyboard.press("ArrowRight"); await sleep(150);
    out.dialog = await page.evaluate(() => ({ active: document.activeElement && document.activeElement.id || null,
      act: !!document.querySelector("[data-otvx-act]"), post: !!document.querySelector("article.otvx-focus"),
      feed_help: !!document.querySelector(".otvx-help"), dlg_display: getComputedStyle(document.getElementById("dlg")).display,
      acts: window.__acts.length }));
    // connexion : aucune classe, aucune largeur imposée, aucune aide du fil, colonne latérale intacte
    await page.goto("https://x.com/i/flow/login");
    await sleep(800);
    await page.keyboard.press("ArrowRight"); await sleep(150);
    out.login = await page.evaluate(() => ({ tv: document.documentElement.classList.contains("otvx-tv"),
      lanes: document.querySelectorAll("[data-otvx-lane]").length, act: !!document.querySelector("[data-otvx-act]"),
      help: !!document.querySelector(".otvx-help"), sidebar: getComputedStyle(document.getElementById("sb")).display }));
    process.stdout.write(JSON.stringify(out) + "\n");
  } finally {
    await ctx.close();
    fs.rmSync(prof, { recursive: true, force: true });
  }
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(1); });
