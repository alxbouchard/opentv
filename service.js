// Service Node.js exécuté par TizenBrew (Node 4.4 : ES5 uniquement, new Buffer, pas de Buffer.from).
// Écoute sur toutes les interfaces : le téléphone (même Wi-Fi) sert de télécommande complète.
//   GET /apps /launch?id=  -> applis
//   GET /info              -> { ip, port, version }
//   GET /remote            -> page HTML de télécommande (téléphone)
//   GET /sys?key=KEY_VOLUP -> relaie une touche système à la TV (API Samsung, contrôle TOUT)
//   GET /cmd?cmd= /poll    -> ancien canal navigation OpenTV (conservé)
//   GET /rcstatus /status /version /shutdown
var http = require('http');
var url = require('url');
var os = require('os');
var tls = require('tls');
var crypto = require('crypto');
var fs = require('fs');

var PORT = 8096;
var VERSION = 6;
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

// ---------------------------------------------------------------- relais télécommande TV (API Samsung, wss:8002)
var RC_NAME = new Buffer('OpenTV').toString('base64');
var TOKEN_FILE = os.tmpdir() + '/opentv_rc_token';
var rcToken = '';
try { rcToken = (fs.readFileSync(TOKEN_FILE, 'utf8') || '').replace(/\s/g, ''); } catch (e) {}
var rcSock = null, rcReady = false, rcConnecting = false, rcHandshook = false, rcQueue = [], rcBuf = new Buffer(0);

function rcFrame(opcode, payload) {
  var len = payload.length, mask = crypto.randomBytes(4), header;
  if (len < 126) { header = new Buffer(2); header[1] = 0x80 | len; }
  else if (len < 65536) { header = new Buffer(4); header[1] = 0x80 | 126; header.writeUInt16BE(len, 2); }
  else { header = new Buffer(10); header[1] = 0x80 | 127; header.writeUInt32BE(0, 2); header.writeUInt32BE(len, 6); }
  header[0] = 0x80 | opcode;
  var out = new Buffer(len);
  for (var i = 0; i < len; i++) out[i] = payload[i] ^ mask[i % 4];
  return Buffer.concat([header, mask, out]);
}
function rcText(str) { return rcFrame(0x1, new Buffer(str, 'utf8')); }
function rcDown() { rcReady = false; rcConnecting = false; rcHandshook = false; try { if (rcSock) rcSock.destroy(); } catch (e) {} rcSock = null; }

function rcConnect() {
  if (rcConnecting || rcReady) return;
  rcConnecting = true; rcHandshook = false; rcBuf = new Buffer(0);
  var host = lanIp();
  try {
    rcSock = tls.connect({ host: host, port: 8002, rejectUnauthorized: false }, function () {
      var k = crypto.randomBytes(16).toString('base64');
      var path = '/api/v2/channels/samsung.remote.control?name=' + RC_NAME + (rcToken ? '&token=' + rcToken : '');
      rcSock.write('GET ' + path + ' HTTP/1.1\r\nHost: ' + host + ':8002\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n' +
        'Sec-WebSocket-Key: ' + k + '\r\nSec-WebSocket-Version: 13\r\n\r\n');
    });
    rcSock.on('data', rcData);
    rcSock.on('error', rcDown);
    rcSock.on('close', rcDown);
  } catch (e) { rcDown(); }
}
function rcData(chunk) {
  rcBuf = Buffer.concat([rcBuf, chunk]);
  if (!rcHandshook) {
    var s = rcBuf.toString('binary'), idx = s.indexOf('\r\n\r\n');
    if (idx < 0) return;
    if (s.slice(0, idx).indexOf('101') < 0) { rcDown(); return; }
    rcBuf = rcBuf.slice(idx + 4);
    rcHandshook = true; rcReady = true; rcConnecting = false;
    while (rcQueue.length) { try { rcSock.write(rcText(rcQueue.shift())); } catch (e) {} }
  }
  while (rcBuf.length >= 2) {
    var b1 = rcBuf[1], op = rcBuf[0] & 0x0f, masked = b1 & 0x80, len = b1 & 0x7f, off = 2;
    if (len === 126) { if (rcBuf.length < 4) break; len = rcBuf.readUInt16BE(2); off = 4; }
    else if (len === 127) { if (rcBuf.length < 10) break; len = rcBuf.readUInt32BE(6); off = 10; }
    if (masked) off += 4;
    if (rcBuf.length < off + len) break;
    var payload = rcBuf.slice(off, off + len);
    rcBuf = rcBuf.slice(off + len);
    if (op === 0x8) { rcDown(); return; }
    if (op === 0x9) { try { rcSock.write(rcFrame(0xA, payload)); } catch (e) {} continue; }
    if (op === 0x1) rcMsg(payload.toString('utf8'));
  }
}
function rcMsg(str) {
  var m; try { m = JSON.parse(str); } catch (e) { return; }
  if (m.event === 'ms.channel.connect' && m.data && m.data.token && String(m.data.token) !== rcToken) {
    rcToken = String(m.data.token); try { fs.writeFileSync(TOKEN_FILE, rcToken); } catch (e) {}
  }
  if (m.event === 'ms.channel.unauthorized') { rcToken = ''; try { fs.unlinkSync(TOKEN_FILE); } catch (e) {} rcDown(); }
}
function rcSend(key) {
  var msg = JSON.stringify({ method: 'ms.remote.control', params: { Cmd: 'Click', DataOfCmd: key, Option: 'false', TypeOfRemote: 'SendRemoteKey' } });
  if (rcReady && rcSock) { try { rcSock.write(rcText(msg)); return true; } catch (e) { rcDown(); } }
  rcQueue.push(msg); if (rcQueue.length > 30) rcQueue.shift();
  rcConnect();
  return false;
}
rcConnect();   // établit la liaison dès le démarrage (et persiste le token)

// ---------------------------------------------------------------- page télécommande (téléphone)
var REMOTE_HTML = [
'<!doctype html><html lang="fr"><head><meta charset="utf-8">',
'<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">',
'<title>Télécommande OpenTV</title><style>',
'*{box-sizing:border-box;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}',
'html,body{margin:0;min-height:100%;background:#0b0d12;color:#fff;font-family:-apple-system,system-ui,sans-serif}',
'body{display:flex;flex-direction:column;align-items:center;padding:20px 16px 36px;gap:16px}',
'h1{font-size:17px;font-weight:600;opacity:.6;margin:4px 0 2px}',
'.toprow{display:flex;gap:14px}',
'.circle{width:66px;height:66px;border:none;border-radius:50%;background:#1b1f29;color:#fff;font:600 15px inherit;cursor:pointer}',
'.circle:active{background:#2e3442}.pw{background:#b3261e}.pw:active{background:#8e1e18}',
'.vol{display:flex;flex-direction:column;align-items:center;gap:8px}',
'.rock{display:flex;flex-direction:column;background:#1b1f29;border-radius:30px;overflow:hidden}',
'.rock button{border:none;background:transparent;color:#fff;font-size:26px;width:70px;height:60px;cursor:pointer}',
'.rock button:active{background:#2e3442}.rock .lbl{font-size:12px;opacity:.5;height:16px;line-height:16px}',
'.mid{display:flex;gap:26px;align-items:center;margin:4px 0}',
'.pad{position:relative;width:230px;height:230px}',
'.btn{position:absolute;border:none;background:#1b1f29;color:#fff;font:600 17px inherit;border-radius:16px;cursor:pointer}',
'.btn:active{background:#2e3442}',
'.up{top:0;left:79px;width:72px;height:72px}.down{bottom:0;left:79px;width:72px;height:72px}',
'.left{top:79px;left:0;width:72px;height:72px}.right{top:79px;right:0;width:72px;height:72px}',
'.ok{top:79px;left:79px;width:72px;height:72px;background:#3b82f6;border-radius:50%}.ok:active{background:#2f6ad0}',
'.arrow{font-size:28px;line-height:1}',
'.row{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}',
'.key{background:#1b1f29;border:none;color:#fff;font:600 15px inherit;padding:15px 20px;border-radius:13px;cursor:pointer}',
'.key:active{background:#2e3442}',
'#flash{position:fixed;top:8px;left:50%;transform:translateX(-50%);background:#2e3442;color:#fff;font-size:13px;padding:6px 14px;border-radius:12px;opacity:0;transition:opacity .2s}',
'</style></head><body>',
'<div id="flash"></div>',
'<h1>Télécommande OpenTV</h1>',
'<div class="toprow">',
'<button class="circle pw" data-k="KEY_POWER">⏻</button>',
'<button class="circle" data-k="KEY_SOURCE">Source</button>',
'<button class="circle" data-k="KEY_MUTE">Muet</button>',
'</div>',
'<div class="mid">',
'<div class="rock"><div class="lbl">VOL</div><button data-k="KEY_VOLUP">+</button><button data-k="KEY_VOLDOWN">−</button></div>',
'<div class="pad">',
'<button class="btn up" data-k="KEY_UP"><span class="arrow">▲</span></button>',
'<button class="btn left" data-k="KEY_LEFT"><span class="arrow">◀</span></button>',
'<button class="btn ok" data-k="KEY_ENTER">OK</button>',
'<button class="btn right" data-k="KEY_RIGHT"><span class="arrow">▶</span></button>',
'<button class="btn down" data-k="KEY_DOWN"><span class="arrow">▼</span></button>',
'</div>',
'<div class="rock"><div class="lbl">CH</div><button data-k="KEY_CHUP">+</button><button data-k="KEY_CHDOWN">−</button></div>',
'</div>',
'<div class="row">',
'<button class="key" data-k="KEY_RETURN">Retour</button>',
'<button class="key" data-k="KEY_HOME">Accueil</button>',
'<button class="key" data-k="KEY_EXIT">Quitter</button>',
'</div>',
'<div class="row">',
'<button class="key" data-k="KEY_REWIND">⏪</button>',
'<button class="key" data-k="KEY_PLAY">⏯</button>',
'<button class="key" data-k="KEY_FF">⏩</button>',
'<button class="key" data-k="KEY_MENU">Menu</button>',
'</div>',
'<script>',
'var f=document.getElementById("flash");var ft;',
'function flash(t){f.textContent=t;f.style.opacity=1;clearTimeout(ft);ft=setTimeout(function(){f.style.opacity=0;},700);}',
'function send(k){try{var x=new XMLHttpRequest();x.open("GET","sys?key="+k,true);x.send();}catch(e){}flash(k.replace("KEY_",""));}',
'var bs=document.querySelectorAll("[data-k]");',
'for(var i=0;i<bs.length;i++){(function(b){',
'b.addEventListener("touchstart",function(e){e.preventDefault();send(b.getAttribute("data-k"));},{passive:false});',
'b.addEventListener("click",function(){send(b.getAttribute("data-k"));});',
'})(bs[i]);}',
'</script></body></html>'
].join('\n');

// ---------------------------------------------------------------- HTTP
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
  } else if (p === '/sys') {
    var key = request.query.key || '';
    if (!/^KEY_[A-Z0-9_]+$/.test(key)) { reply(res, 400, { error: 'bad key' }); return; }
    var sent = rcSend(key);
    reply(res, 200, { ok: true, live: sent, ready: rcReady });
  } else if (p === '/rcstatus') {
    reply(res, 200, { ready: rcReady, connecting: rcConnecting, hasToken: !!rcToken });
  } else if (p === '/cmd') {
    var cmd = request.query.cmd || '';
    REMOTE.n++; REMOTE.cmd = String(cmd).slice(0, 20);
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
