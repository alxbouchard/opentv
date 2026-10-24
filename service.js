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
var VERSION = 8;
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
'*{box-sizing:border-box;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent;-webkit-touch-callout:none}',
'html,body{margin:0;min-height:100%;background:#000;color:#fff;font-family:-apple-system,system-ui,sans-serif}',
'body{display:flex;flex-direction:column;align-items:center;padding:16px 18px 40px;gap:20px}',
'h1{font-size:15px;font-weight:600;opacity:.5;margin:6px 0 0;letter-spacing:.3px}',
'.top{display:flex;gap:18px;width:320px;max-width:86vw;justify-content:center}',
'.circle{flex:1;height:60px;border:none;border-radius:16px;background:#17191e;color:#fff;font:600 15px inherit;cursor:pointer}',
'.circle:active{background:#2a2d35}.pw{color:#ff5b52}',
// trackpad
'.pad{width:320px;max-width:86vw;height:320px;max-height:86vw;border-radius:42px;position:relative;',
'background:radial-gradient(130% 130% at 50% 20%,#2b2f37 0%,#15171c 60%,#101216 100%);',
'box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 10px 30px rgba(0,0,0,.5);overflow:hidden;touch-action:none}',
'.pad .hint{position:absolute;left:0;right:0;bottom:22px;text-align:center;font-size:13px;color:rgba(255,255,255,.32)}',
'.pad .okl{position:absolute;left:0;right:0;top:46%;text-align:center;font-size:17px;font-weight:600;color:rgba(255,255,255,.5)}',
'.rip{position:absolute;width:120px;height:120px;margin:-60px 0 0 -60px;border-radius:50%;background:rgba(255,255,255,.14);opacity:0;pointer-events:none;transition:opacity .35s}',
// boutons
'.row{display:flex;gap:16px;width:320px;max-width:86vw}',
'.key{flex:1;height:62px;border:none;border-radius:16px;background:#17191e;color:#fff;font:600 16px inherit;cursor:pointer;display:flex;align-items:center;justify-content:center}',
'.key:active{background:#2a2d35}',
'.rock{flex:1;display:flex;background:#17191e;border-radius:16px;overflow:hidden}',
'.rock button{flex:1;border:none;background:transparent;color:#fff;font-size:24px;cursor:pointer}',
'.rock button:active{background:#2a2d35}.rock .sep{width:1px;background:rgba(255,255,255,.08)}',
'#flash{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#2a2d35;color:#fff;font-size:13px;padding:6px 16px;border-radius:14px;opacity:0;transition:opacity .15s;pointer-events:none}',
'</style></head><body>',
'<div id="flash"></div>',
'<h1>TÉLÉCOMMANDE OPENTV</h1>',
'<div id="st" style="font-size:13px;margin-top:-10px;min-height:18px"></div>',
'<div class="top">',
'<button class="circle pw" data-k="KEY_POWER">⏻ Power</button>',
'<button class="circle" data-k="KEY_MUTE">🔇 Muet</button>',
'<button class="circle" data-k="KEY_SOURCE">⇄ Source</button>',
'</div>',
'<div class="pad" id="pad"><div class="okl">OK</div><div class="hint">Glisse pour naviguer · touche pour OK</div><div class="rip" id="rip"></div></div>',
'<div class="row">',
'<button class="key" data-k="KEY_RETURN">‹ Retour</button>',
'<button class="key" data-k="KEY_HOME">⌂ Accueil</button>',
'<button class="key" data-k="KEY_PLAY">⏯</button>',
'</div>',
'<div class="row">',
'<div class="rock"><button data-k="KEY_VOLDOWN">VOL −</button><div class="sep"></div><button data-k="KEY_VOLUP">VOL +</button></div>',
'<div class="rock"><button data-k="KEY_CHDOWN">CH −</button><div class="sep"></div><button data-k="KEY_CHUP">CH +</button></div>',
'</div>',
'<script>',
'var f=document.getElementById("flash"),ft,stEl=document.getElementById("st");',
'function flash(t){f.textContent=t;f.style.opacity=1;clearTimeout(ft);ft=setTimeout(function(){f.style.opacity=0;},600);}',
'function poll(){try{var x=new XMLHttpRequest();x.open("GET","rcstatus",true);x.onreadystatechange=function(){if(x.readyState===4){var r={};try{r=JSON.parse(x.responseText);}catch(e){}',
'if(r.ready){stEl.textContent="● TV connectée";stEl.style.color="#34d399";}',
'else{stEl.textContent="○ Autorise OpenTV sur ta TV (popup à l\'écran)";stEl.style.color="#fbbf24";}}};x.send();}catch(e){}}',
'poll();setInterval(poll,2500);',
'function send(k){try{var x=new XMLHttpRequest();x.open("GET","sys?key="+k,true);x.send();}catch(e){}}',
'var LB={KEY_ENTER:"OK",KEY_UP:"▲",KEY_DOWN:"▼",KEY_LEFT:"◀",KEY_RIGHT:"▶",KEY_RETURN:"Retour",KEY_HOME:"Accueil",KEY_PLAY:"Lecture",KEY_POWER:"Power",KEY_MUTE:"Muet",KEY_SOURCE:"Source",KEY_VOLUP:"Vol +",KEY_VOLDOWN:"Vol −",KEY_CHUP:"Ch +",KEY_CHDOWN:"Ch −"};',
'function tap(k){send(k);flash(LB[k]||k);}',
// trackpad : glisser = direction, toucher = OK
'var pad=document.getElementById("pad"),rip=document.getElementById("rip");',
'var sx,sy,st,active=false;',
'function ripple(x,y){var r=pad.getBoundingClientRect();rip.style.left=(x-r.left)+"px";rip.style.top=(y-r.top)+"px";rip.style.transition="none";rip.style.opacity=.5;setTimeout(function(){rip.style.transition="opacity .35s";rip.style.opacity=0;},10);}',
'pad.addEventListener("touchstart",function(e){e.preventDefault();var t=e.touches[0];sx=t.clientX;sy=t.clientY;st=Date.now();active=true;},{passive:false});',
'pad.addEventListener("touchmove",function(e){e.preventDefault();},{passive:false});',
'pad.addEventListener("touchend",function(e){if(!active)return;active=false;var t=e.changedTouches[0];ripple(t.clientX,t.clientY);',
'var dx=t.clientX-sx,dy=t.clientY-sy,ax=Math.abs(dx),ay=Math.abs(dy);',
'if(ax<24&&ay<24){tap("KEY_ENTER");return;}',
'var k=ax>ay?(dx>0?"KEY_RIGHT":"KEY_LEFT"):(dy>0?"KEY_DOWN":"KEY_UP");',
'var dist=Math.max(ax,ay),reps=dist>190?3:(dist>110?2:1);',
'for(var i=0;i<reps;i++)(function(d){setTimeout(function(){send(k);},d*90);})(i);',
'flash((LB[k]||k)+(reps>1?" ×"+reps:""));',
'},{passive:false});',
// souris (test bureau) : clic = OK
'pad.addEventListener("click",function(e){ripple(e.clientX,e.clientY);tap("KEY_ENTER");});',
// boutons
'var bs=document.querySelectorAll("[data-k]");',
'for(var i=0;i<bs.length;i++){(function(b){var k=b.getAttribute("data-k");',
'b.addEventListener("touchstart",function(e){e.preventDefault();tap(k);},{passive:false});',
'b.addEventListener("click",function(){tap(k);});})(bs[i]);}',
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
