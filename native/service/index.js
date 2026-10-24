// Service Node.js exécuté par TizenBrew (Node 4.4 : ES5).
// Écoute sur toutes les interfaces : le téléphone (même Wi-Fi) sert de télécommande pour OpenTV.
//   GET /apps /launch?id=  -> applis
//   GET /info              -> { ip, port, version }
//   GET /remote            -> page HTML de télécommande (trackpad -> navigation OpenTV)
//   GET /cmd?cmd=up        -> le téléphone envoie une touche de navigation à OpenTV
//   GET /poll              -> OpenTV récupère la dernière touche { n, cmd }
//   GET /status /version /shutdown
//
// NOTE : contrôler toute la TV (volume, power, apps natives) depuis une page web n'est PAS possible.
// Samsung ferme les connexions à son API télécommande (8002) qui viennent de la TV elle-même
// (self-origin bloqué, diagnostic « closed_early »), et Safari refuse le wss auto-signé en direct.
// Pour le contrôle système complet -> app SmartThings.
var http = require('http');
var url = require('url');
var os = require('os');

var PORT = 8096;
var VERSION = 13;
var REMOTE = { n: 0, cmd: '' };

function reply(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(body));
}
function lanIp() {
  var ifs = os.networkInterfaces(), ip = '127.0.0.1';
  Object.keys(ifs).forEach(function (n) {
    ifs[n].forEach(function (a) { if (a.family === 'IPv4' && !a.internal) ip = a.address; });
  });
  return ip;
}

var REMOTE_HTML = [
'<!doctype html><html lang="fr"><head><meta charset="utf-8">',
'<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">',
'<title>Télécommande OpenTV</title><style>',
'*{box-sizing:border-box;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}',
'html,body{margin:0;min-height:100%;background:#000;color:#fff;font-family:-apple-system,system-ui,sans-serif}',
'body{display:flex;flex-direction:column;align-items:center;padding:22px 18px 40px;gap:22px}',
'h1{font-size:15px;font-weight:600;opacity:.5;margin:8px 0 0;letter-spacing:.3px}',
'.pad{width:320px;max-width:86vw;height:320px;max-height:86vw;border-radius:44px;position:relative;',
'background:radial-gradient(130% 130% at 50% 20%,#2b2f37 0%,#15171c 60%,#101216 100%);',
'box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 10px 30px rgba(0,0,0,.5);overflow:hidden;touch-action:none}',
'.pad .okl{position:absolute;left:0;right:0;top:46%;text-align:center;font-size:18px;font-weight:600;color:rgba(255,255,255,.5)}',
'.pad .hint{position:absolute;left:0;right:0;bottom:22px;text-align:center;font-size:13px;color:rgba(255,255,255,.32)}',
'.rip{position:absolute;width:120px;height:120px;margin:-60px 0 0 -60px;border-radius:50%;background:rgba(255,255,255,.14);opacity:0;pointer-events:none;transition:opacity .35s}',
'.row{display:flex;gap:16px;width:320px;max-width:86vw}',
'.key{flex:1;height:62px;border:none;border-radius:16px;background:#17191e;color:#fff;font:600 16px inherit;cursor:pointer}',
'.key:active{background:#2a2d35}',
'.note{width:320px;max-width:86vw;font-size:12px;color:rgba(255,255,255,.4);text-align:center;line-height:1.5}',
'#flash{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#2a2d35;color:#fff;font-size:13px;padding:6px 16px;border-radius:14px;opacity:0;transition:opacity .15s;pointer-events:none}',
'</style></head><body>',
'<div id="flash"></div>',
'<h1>TÉLÉCOMMANDE OPENTV</h1>',
'<div class="pad" id="pad"><div class="okl">OK</div><div class="hint">Glisse pour naviguer · touche pour OK</div><div class="rip" id="rip"></div></div>',
'<div class="row">',
'<button class="key" data-k="back">‹ Retour</button>',
'<button class="key" data-k="home">⌂ Accueil</button>',
'</div>',
'<div class="note">Navigue OpenTV et lance tes apps.<br>Volume, allumage et contrôle dans Netflix/YouTube : app <b>SmartThings</b> (Samsung bloque le contrôle système depuis le web).</div>',
'<script>',
'var f=document.getElementById("flash"),ft;',
'function flash(t){f.textContent=t;f.style.opacity=1;clearTimeout(ft);ft=setTimeout(function(){f.style.opacity=0;},600);}',
'function send(c){try{var x=new XMLHttpRequest();x.open("GET","cmd?cmd="+c,true);x.send();}catch(e){}}',
'var LB={ok:"OK",up:"▲",down:"▼",left:"◀",right:"▶",back:"Retour",home:"Accueil"};',
'function tap(c){send(c);flash(LB[c]||c);}',
'var pad=document.getElementById("pad"),rip=document.getElementById("rip"),sx,sy,active=false;',
'function ripple(x,y){var r=pad.getBoundingClientRect();rip.style.left=(x-r.left)+"px";rip.style.top=(y-r.top)+"px";rip.style.transition="none";rip.style.opacity=.5;setTimeout(function(){rip.style.transition="opacity .35s";rip.style.opacity=0;},10);}',
'pad.addEventListener("touchstart",function(e){e.preventDefault();var t=e.touches[0];sx=t.clientX;sy=t.clientY;active=true;},{passive:false});',
'pad.addEventListener("touchmove",function(e){e.preventDefault();},{passive:false});',
'pad.addEventListener("touchend",function(e){if(!active)return;active=false;var t=e.changedTouches[0];ripple(t.clientX,t.clientY);',
'var dx=t.clientX-sx,dy=t.clientY-sy,ax=Math.abs(dx),ay=Math.abs(dy);',
'if(ax<24&&ay<24){tap("ok");return;}',
'var c=ax>ay?(dx>0?"right":"left"):(dy>0?"down":"up");tap(c);},{passive:false});',
'pad.addEventListener("click",function(e){ripple(e.clientX,e.clientY);tap("ok");});',
'var bs=document.querySelectorAll("[data-k]");',
'for(var i=0;i<bs.length;i++){(function(b){var k=b.getAttribute("data-k");',
'b.addEventListener("touchstart",function(e){e.preventDefault();tap(k);},{passive:false});',
'b.addEventListener("click",function(){tap(k);});})(bs[i]);}',
'</script></body></html>'
].join('\n');

var server = http.createServer(function (req, res) {
  var request = url.parse(req.url, true);
  var p = request.pathname;

  if (p === '/apps') {
    try {
      tizen.application.getAppsInfo(function (apps) {
        var out = [];
        apps.forEach(function (app) { if (app.show === false) return; out.push({ id: app.id, name: app.name || app.id }); });
        reply(res, 200, out);
      }, function (e) { reply(res, 500, { error: e.message }); });
    } catch (e) { reply(res, 500, { error: String(e) }); }
  } else if (p === '/launch' && request.query.id) {
    try {
      tizen.application.launch(request.query.id, function () { reply(res, 200, { ok: true }); },
        function (e) { reply(res, 500, { error: e.message }); });
    } catch (e) { reply(res, 500, { error: String(e) }); }
  } else if (p === '/info') {
    reply(res, 200, { ip: lanIp(), port: PORT, version: VERSION });
  } else if (p === '/remote' || p === '/remote/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    res.end(REMOTE_HTML);
  } else if (p === '/cmd') {
    REMOTE.n++; REMOTE.cmd = String(request.query.cmd || '').slice(0, 20);
    reply(res, 200, { ok: true });
  } else if (p === '/poll') {
    reply(res, 200, { n: REMOTE.n, cmd: REMOTE.cmd });
  } else if (p === '/status') {
    reply(res, 200, { ok: true, version: VERSION });
  } else if (p === '/version') {
    reply(res, 200, { version: VERSION });
  } else if (p === '/shutdown') {
    reply(res, 200, { ok: true }); server.close();
  } else {
    reply(res, 404, { error: 'not found' });
  }
});

var attempts = 0;
function listen() { server.listen(PORT, '0.0.0.0'); }
server.on('error', function (e) {
  if (e.code !== 'EADDRINUSE' || ++attempts > 3) return;
  http.get({ host: '127.0.0.1', port: PORT, path: '/version' }, function (res) {
    var body = '';
    res.on('data', function (chunk) { body += chunk; });
    res.on('end', function () {
      var running = 0;
      try { running = JSON.parse(body).version || 0; } catch (err) {}
      if (running >= VERSION) return;
      http.get({ host: '127.0.0.1', port: PORT, path: '/shutdown' }, function () {}).on('error', function () {});
      setTimeout(listen, 500);
    });
  }).on('error', function () { setTimeout(listen, 500); });
});
listen();
