// Appli externe NanoPi (tuile « Films ») : HDMI 1 + touches relayées + retour au tableau de bord.
// Faux NanoPi local (contrat : native/NANOPI-API.md). Ne remplace pas /status sur la vraie TV.
const { chromium } = require('playwright'); const http = require('http'), fs = require('fs'), path = require('path');
const ROOT = path.join(__dirname, 'wgt'); const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.png': 'image/png' };
const web = http.createServer((q, r) => { const f = path.join(ROOT, decodeURIComponent(q.url.split('?')[0])); fs.readFile(f, (e, d) => { if (e) { r.writeHead(404); return r.end(); } r.writeHead(200, { 'Content-Type': mime[path.extname(f)] || 'x' }); r.end(d); }); }).listen(8094);
// ---- faux NanoPi ----
const NP = { log: [], state: 'dashboard', session: null, mode: 'ok', delay: 0, n: 0 };
function npReply(r, code, body) { r.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' }); r.end(JSON.stringify(body)); }
const nanopi = http.createServer((q, r) => {
  const u = new URL(q.url, 'http://x'), p = u.pathname, s = u.searchParams;
  if (p !== '/v1/health') NP.log.push(q.method + ' ' + p + (u.search || ''));
  const go = () => {
    if (NP.mode === 'down') { q.socket.destroy(); return; }
    if (NP.mode === 'bogus') return npReply(r, 200, { ok: true, session: '<img src=x onerror=alert(1)>', state: 'pwned' });
    if (p === '/v1/session' && q.method === 'POST') {
      if (NP.mode === 'fail') return npReply(r, 503, { ok: false, error: 'discovery_failed' });
      if (s.get('app') !== 'catalogue') return npReply(r, 404, { ok: false, error: 'unknown_app' });
      NP.session = 'sess' + (++NP.n) + 'abcdef'; NP.state = 'catalogue';
      return npReply(r, 200, { ok: true, session: NP.session, state: NP.state });
    }
    if (p === '/v1/key' && q.method === 'POST') {
      if (s.get('session') !== NP.session) return npReply(r, 410, { ok: false, error: 'no_session' });
      const k = s.get('k');
      if (k === 'ok' && NP.state === 'catalogue') NP.state = 'player';
      else if (k === 'back') NP.state = NP.state === 'player' ? 'catalogue' : 'dashboard';
      if (NP.state === 'dashboard') NP.session = null;
      return npReply(r, 200, { ok: true, state: NP.state });
    }
    if (p === '/v1/close' && q.method === 'POST') { NP.session = null; NP.state = 'dashboard'; return npReply(r, 200, { ok: true, state: 'dashboard' }); }
    if (p === '/v1/health') return npReply(r, 200, { ok: true, api: 1, state: NP.state, session: NP.session });
    npReply(r, 404, { ok: false, error: 'not_found' });
  };
  NP.delay ? setTimeout(go, NP.delay) : go();
}).listen(18097);

const R = []; const ok = (n, c, x = '') => R.push((c ? 'PASS ' : 'FAIL ') + n + (x ? '  [' + x + ']' : ''));
const INIT = base => {
  window.LOG = []; window.NOVA_NANOPI = { base };
  window.tizen = { application: { getAppsInfo: ok => ok([{ id: 'org.tizen.netflix-app', name: 'Netflix', show: true }, { id: '111299001912', name: 'YouTube', show: true }]), launch: (id, ok) => { LOG.push('launch ' + id); ok && ok(); }, getCurrentApplication: () => ({ appInfo: { packageId: 'OpnTV00001' } }), launchAppControl: () => {} }, ApplicationControl: function () {},
    tvinputdevice: { registerKey: k => LOG.push('registerKey ' + k) },
    systeminfo: { getPropertyValue: (k, ok) => ok({ connected: [{ type: 'HDMI', number: 1 }], disconnected: [] }) },
    tvwindow: { setSource: (s, ok) => { LOG.push('setSource ' + s.type + s.number); ok(s); }, show: (ok, err, rect, type, z) => { LOG.push('show ' + type + ' ' + z); ok(rect, type); }, hide: ok => { LOG.push('hide'); ok && ok(); } } };
};
(async () => {
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message)); p.on('dialog', d => { errs.push('dialog ' + d.message()); d.dismiss(); });
  await p.addInitScript(INIT, 'http://127.0.0.1:18097');
  await p.goto('http://127.0.0.1:8094/app/index.html'); await p.waitForTimeout(2500);
  const key = async (k, wait = 500) => { await p.keyboard.press(k); await p.waitForTimeout(wait); };
  const code = async (c, wait = 500) => { await p.evaluate(c => document.dispatchEvent(new KeyboardEvent('keydown', { keyCode: c, bubbles: true })), c); await p.waitForTimeout(wait); };
  const st = () => p.evaluate(() => ({ cls: document.getElementById('stage').className, f: (document.querySelector('.focused') || {}).getAttribute && document.querySelector('.focused').getAttribute('data-key'), toast: document.getElementById('toast').className ? '' : document.getElementById('toast').textContent, log: LOG.slice() }));
  const focusExt = async () => { await key('ArrowRight', 150); await key('ArrowRight', 150); };   // accueil : Netflix, YouTube, Films
  const vis = async hidden => { await p.evaluate(h => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => h }); document.dispatchEvent(new Event('visibilitychange')); }, hidden); await p.waitForTimeout(400); };

  // 1. tuile externe distincte
  const tile = await p.evaluate(() => { const t = document.querySelector('#tiles [data-key="ext-catalogue"]'); const all = [].map.call(document.querySelectorAll('#tiles .tile'), e => e.getAttribute('data-key')); return t && { kind: t.getAttribute('data-kind'), ext: t.classList.contains('ext'), via: (t.querySelector('.via') || {}).textContent, name: t.querySelector('.name').textContent, last: all[all.length - 1], all: all.join(',') }; });
  ok('tuile Films = externe NanoPi (badge HDMI 1, en dernier, pas app-)', tile && tile.kind === 'nanopi' && tile.ext && tile.via === 'HDMI 1' && tile.last === 'ext-catalogue', JSON.stringify(tile));
  let s = await st(); ok('touche rouge enregistrée', s.log.includes('registerKey ColorF0Red'));
  ok('focus initial = 1re appli native', s.f === 'app-org.tizen.netflix-app', s.f);

  // 2. ouverture
  await key('ArrowRight', 150); await key('ArrowRight', 150); s = await st();
  ok('navigation jusqu\'à la tuile externe', s.f === 'ext-catalogue', s.f);
  await key('Enter', 900); s = await st();
  ok('OK -> POST /v1/session?app=catalogue (aucune URL envoyée)', NP.log[0] === 'POST /v1/session?app=catalogue', NP.log.join(' | '));
  ok('jamais tizen.application.launch pour la tuile externe', !s.log.some(l => /^launch/.test(l)), s.log.join(' / '));
  ok('HDMI 1 affiché DERRIÈRE, menu descendu', s.log.includes('setSource HDMI1') && s.log.includes('show MAIN BEHIND') && /\blive\b/.test(s.cls) && !/peek/.test(s.cls), s.cls);

  // 3. touches relayées
  NP.log = []; await key('ArrowDown', 300); await key('ArrowRight', 300); await key('Enter', 500); s = await st();
  ok('flèches + OK relayées au NanoPi avec la session', NP.log.join(',') === 'POST /v1/key?session=sess1abcdef&k=down,POST /v1/key?session=sess1abcdef&k=right,POST /v1/key?session=sess1abcdef&k=ok', NP.log.join(' | '));
  ok('focus OpenTV inchangé, menu toujours descendu', s.f === 'ext-catalogue' && !/peek/.test(s.cls), s.f + ' ' + s.cls);
  ok('NanoPi en lecteur (page publique)', NP.state === 'player');

  // 4. Retour : lecteur -> catalogue -> tableau de bord
  NP.log = []; await key('Escape', 500); s = await st();
  ok('Retour dans le lecteur -> relayé, catalogue restauré, session gardée', NP.log[0] === 'POST /v1/key?session=sess1abcdef&k=back' && NP.state === 'catalogue' && /\blive\b/.test(s.cls) && !s.log.includes('hide'), NP.log.join(' | '));
  await key('Escape', 600); s = await st();
  ok('Retour à la racine du catalogue -> tableau de bord (HDMI 1 reste, pas de close en double)', NP.state === 'dashboard' && /\blive\b/.test(s.cls) && !s.log.includes('hide') && !NP.log.some(l => /close/.test(l)), NP.log.join(' | ') + ' ' + s.cls);
  NP.log = []; await key('ArrowRight', 500); s = await st();
  ok('ensuite, comportement direct normal (1re touche = menu, rien relayé)', /peek/.test(s.cls) && NP.log.length === 0, s.cls);
  await key('Escape', 500);

  // 5. bascule de focus (touche rouge)
  await key('ArrowRight', 400); await key('Enter', 900);   // menu remonte puis OK sur Films (focus restauré)
  s = await st(); ok('réouverture', NP.state === 'catalogue' && /\blive\b/.test(s.cls) && !/peek/.test(s.cls), NP.state + ' ' + s.cls);
  NP.log = []; await code(403); s = await st();
  ok('rouge -> focus OpenTV (rangée par-dessus HDMI 1)', /peek/.test(s.cls) && NP.log.length === 0, s.cls);
  await key('ArrowLeft', 300); s = await st();
  ok('flèches gérées par OpenTV, pas relayées', s.f === 'app-111299001912' && NP.log.length === 0, s.f + ' ' + NP.log.join('|'));
  await key('Escape', 400); s = await st();
  ok('Retour -> focus NanoPi (menu redescend, session gardée)', !/peek/.test(s.cls) && /\blive\b/.test(s.cls) && NP.session, s.cls);
  await code(403);
  NP.log = []; await key('Enter', 700); s = await st();
  ok('OK sur appli native en focus OpenTV -> close NanoPi, hide, launch Tizen', NP.log.some(l => /^POST \/v1\/close\?session=sess2abcdef&reason=live-off/.test(l)) && s.log.slice(-2).join(' / ') === 'hide / launch 111299001912', NP.log.join(' | ') + ' // ' + s.log.slice(-2).join(' / '));

  // 6. Exit -> tableau de bord ; visibilité -> close
  await vis(false);
  await focusExt(); await key('Enter', 900); NP.log = []; await code(10071, 500); s = await st();
  ok('Exit -> close + tableau de bord', NP.log.some(l => /close\?session=sess3abcdef&reason=user/.test(l)) && /\blive\b/.test(s.cls) && NP.state === 'dashboard', NP.log.join(' | '));
  await key('ArrowRight', 400); await key('Enter', 900); NP.log = [];
  await vis(true);
  ok('TV quitte OpenTV (Home Samsung) -> close session', NP.log.some(l => /close\?session=sess4abcdef&reason=live-off/.test(l)), NP.log.join(' | '));
  await vis(false);

  // 7. inactivité côté NanoPi (health -> dashboard)
  await focusExt(); await key('Enter', 900); NP.state = 'dashboard'; NP.session = null; await p.waitForTimeout(2600); s = await st();
  NP.log = []; await key('ArrowDown', 400);
  ok('inactivité NanoPi (health=dashboard) -> sortie, plus rien relayé', NP.log.length === 0 && /peek/.test((await st()).cls), NP.log.join('|'));
  await key('Escape', 400);

  // 8. NanoPi injoignable pendant la session
  await key('ArrowRight', 400); await key('Enter', 900); NP.mode = 'down'; await p.waitForTimeout(7000); s = await st();
  ok('NanoPi injoignable (3 échecs health) -> sortie + message', /NanoPi injoignable/.test(s.toast) && /\blive\b/.test(s.cls), s.toast + ' ' + s.cls);
  NP.mode = 'ok'; await key('ArrowRight', 400); await key('Escape', 400);

  // 9. échec d'ouverture et réponse invalide
  await vis(true); await vis(false);          // retour à l'accueil normal (sans direct)
  NP.mode = 'fail'; await p.evaluate(() => LOG.length = 0);
  await focusExt(); await key('Enter', 900); s = await st();
  ok('échec NanoPi (503) -> message, accueil, pas d\'HDMI ni de lancement', /indisponible/.test(s.toast) && !/\blive\b/.test(s.cls) && !s.log.some(l => /^(show|launch)/.test(l)), s.toast + ' / ' + s.log.join(' / '));
  NP.mode = 'bogus'; await p.waitForTimeout(2700); await key('Enter', 900); s = await st();
  const html = await p.evaluate(() => document.body.innerHTML);
  ok('réponse NanoPi invalide -> refusée, rien injecté', /indisponible/.test(s.toast) && !/\blive\b/.test(s.cls) && !/onerror=alert/.test(html), s.toast);
  NP.mode = 'ok';

  // 10. rafale bornée
  NP.delay = 400; await key('Enter', 1500); NP.log = [];
  for (let i = 0; i < 10; i++) await p.keyboard.press('ArrowDown');
  await p.waitForTimeout(4000);
  const keys = NP.log.filter(l => /\/v1\/key/.test(l)).length;
  ok('rafale de 10 touches -> au plus 5 relayées (1 en vol + 4 en file)', keys >= 1 && keys <= 5, String(keys));
  NP.delay = 0; await code(10071, 400);

  // 11. base NanoPi non locale -> aucune tuile externe
  const p2 = await b.newPage(); await p2.addInitScript(INIT, 'http://evil.example.com:8097'); await p2.goto('http://127.0.0.1:8094/app/index.html'); await p2.waitForTimeout(2000);
  ok('base NanoPi hors réseau local -> pas de tuile externe', (await p2.$$('[data-kind="nanopi"]')).length === 0);

  ok('aucune erreur JS', errs.length === 0, errs.join(' / '));
  console.log(R.join('\n')); await b.close(); web.close(); nanopi.close(); process.exit(R.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
