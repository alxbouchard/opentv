// Service Node.js exécuté par TizenBrew (Node 4 : ES5 uniquement).
// Écoute sur toutes les interfaces pour que le téléphone (même Wi-Fi) puisse servir de télécommande.
//   GET /apps            -> applis visibles [{id, name}]
//   GET /launch?id=...   -> lance une appli native
//   GET /info            -> { ip, port, version } (pour construire l'URL du QR)
//   GET /remote          -> page HTML de télécommande (pour le téléphone)
//   GET /cmd?cmd=up      -> le téléphone envoie une touche (up/down/left/right/ok/back/home)
//   GET /poll            -> OpenTV récupère la dernière touche { n, cmd }
//   GET /status /version /shutdown
var http = require('http');
var url = require('url');
var os = require('os');

var PORT = 8096;
var VERSION = 5;                // bump => remplace l'ancien service
var REMOTE = { n: 0, cmd: '' }; // dernière touche reçue du téléphone

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
'*{box-sizing:border-box;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}',
'html,body{margin:0;height:100%;background:#0b0d12;color:#fff;font-family:-apple-system,system-ui,sans-serif}',
'body{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:22px}',
'h1{font-size:19px;font-weight:600;opacity:.65;margin:0}',
'.pad{position:relative;width:260px;height:260px}',
'.btn{position:absolute;border:none;background:#1b1f29;color:#fff;font:600 18px inherit;border-radius:18px;cursor:pointer}',
'.btn:active{background:#2e3442}',
'.up{top:0;left:90px;width:80px;height:80px}.down{bottom:0;left:90px;width:80px;height:80px}',
'.left{top:90px;left:0;width:80px;height:80px}.right{top:90px;right:0;width:80px;height:80px}',
'.ok{top:90px;left:90px;width:80px;height:80px;background:#3b82f6;border-radius:50%}.ok:active{background:#2f6ad0}',
'.row{display:flex;gap:16px}',
'.key{background:#1b1f29;border:none;color:#fff;font:600 16px inherit;padding:16px 26px;border-radius:14px;cursor:pointer}',
'.key:active{background:#2e3442}',
'.arrow{font-size:30px;line-height:1}',
'</style></head><body>',
'<h1>Télécommande OpenTV</h1>',
'<div class="pad">',
'<button class="btn up" data-k="up"><span class="arrow">&#9650;</span></button>',
'<button class="btn left" data-k="left"><span class="arrow">&#9664;</span></button>',
'<button class="btn ok" data-k="ok">OK</button>',
'<button class="btn right" data-k="right"><span class="arrow">&#9654;</span></button>',
'<button class="btn down" data-k="down"><span class="arrow">&#9660;</span></button>',
'</div>',
'<div class="row"><button class="key" data-k="back">Retour</button><button class="key" data-k="home">Accueil</button></div>',
'<script>',
'function send(k){try{var x=new XMLHttpRequest();x.open("GET","cmd?cmd="+k,true);x.send();}catch(e){}}',
'var bs=document.querySelectorAll("[data-k]");',
'for(var i=0;i<bs.length;i++){(function(b){',
'b.addEventListener("touchstart",function(e){e.preventDefault();send(b.getAttribute("data-k"));},{passive:false});',
'b.addEventListener("click",function(){send(b.getAttribute("data-k"));});',
'})(bs[i]);}',
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
    var cmd = request.query.cmd || '';
    if (!cmd) {
      var body = '';
      req.on('data', function (c) { body += c; });
      req.on('end', function () {
        try { cmd = JSON.parse(body || '{}').cmd || ''; } catch (e) { cmd = ''; }
        REMOTE.n++; REMOTE.cmd = String(cmd).slice(0, 20); reply(res, 200, { ok: true });
      });
      return;
    }
    REMOTE.n++; REMOTE.cmd = String(cmd).slice(0, 20);
    reply(res, 200, { ok: true });
  } else if (p === '/poll') {
    reply(res, 200, { n: REMOTE.n, cmd: REMOTE.cmd });
  } else if (p === '/systest') {
    // Test de faisabilité du relais : le service peut-il joindre l'API Samsung (8002) ?
    var out = { node: process.version, ip: lanIp(), tcp8002: 'pending', tls8002: 'pending' };
    try { require('tls'); out.tls = true; } catch (e) { out.tls = false; }
    try { require('crypto'); out.crypto = true; } catch (e) { out.crypto = false; }
    var net2 = require('net');
    var host = lanIp(), replied = false, finished = 0;
    function maybeDone() { if (++finished >= 2 && !replied) { replied = true; reply(res, 200, out); } }
    setTimeout(function () { if (!replied) { replied = true; reply(res, 200, out); } }, 8000);
    var s = net2.connect(8002, host);
    s.setTimeout(5000);
    s.on('connect', function () { out.tcp8002 = 'ok'; try { s.destroy(); } catch (e) {} maybeDone(); });
    s.on('timeout', function () { out.tcp8002 = 'timeout'; try { s.destroy(); } catch (e) {} maybeDone(); });
    s.on('error', function (e) { out.tcp8002 = 'err:' + (e.code || e.message); maybeDone(); });
    try {
      var tls2 = require('tls');
      var t = tls2.connect({ host: host, port: 8002, rejectUnauthorized: false }, function () { out.tls8002 = 'ok'; try { t.destroy(); } catch (e) {} maybeDone(); });
      t.setTimeout(6000);
      t.on('timeout', function () { out.tls8002 = 'timeout'; try { t.destroy(); } catch (e) {} maybeDone(); });
      t.on('error', function (e) { out.tls8002 = 'err:' + (e.code || e.message); maybeDone(); });
    } catch (e) { out.tls8002 = 'noTLS'; maybeDone(); }
    return;
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
