// Bout en bout OpenTV v22 (page réelle, API tizen simulée) <-> VRAI pont NanoPi (scripts/nanopi/opentv_bridge.py)
// lancé en sous-processus, affichage nul (aucun Chromium, aucun réseau externe, aucune TV).
// Depuis native/test/ : node bridge_e2e.js   (exige le .wgt décompressé dans ./wgt et python3)
const { chromium } = require('playwright'); const http = require('http'), fs = require('fs'), path = require('path'), os = require('os');
const { spawn } = require('child_process'), net = require('net');
const ROOT = path.join(__dirname, 'wgt'); const BRIDGE = path.join(__dirname, '..', '..', 'scripts', 'nanopi', 'opentv_bridge.py');
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.png': 'image/png' };
const freePort = () => new Promise(r => { const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
const R = []; const ok = (n, c, x = '') => R.push((c ? 'PASS ' : 'FAIL ') + n + (x ? '  [' + x + ']' : ''));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const [webPort, port, uiPort] = [await freePort(), await freePort(), await freePort()];
  const web = http.createServer((q, r) => { const f = path.join(ROOT, decodeURIComponent(q.url.split('?')[0])); fs.readFile(f, (e, d) => { if (e) { r.writeHead(404); return r.end(); } r.writeHead(200, { 'Content-Type': mime[path.extname(f)] || 'x' }); r.end(d); }); }).listen(webPort);
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'opentv-bridge-e2e-'));
  const now = Date.now() / 1000;
  fs.writeFileSync(path.join(state, 'prefix.json'), JSON.stringify({ prefix: 'aab2ae3', discovered_at: now, validated_at: now, listing_path: '/aab2ae3/home/fakrov' }));
  fs.writeFileSync(path.join(state, 'catalogue.json'), JSON.stringify({ items: Array.from({ length: 14 }, (_, i) => ({ title: 'Film ' + i, detail_path: 'b/fakrov/' + (1000 + i) })) }));
  let lines = [], bridge;
  const start = () => {
    bridge = spawn('python3', ['-I', BRIDGE, 'serve'], { env: Object.assign({}, process.env, { STATE_DIR: state, PORT: String(port), UI_PORT: String(uiPort), BIND: '127.0.0.1', ALLOW_IPS: '127.0.0.1', DISPLAY_BACKEND: 'null', SOURCE_APPROVED: '1', CATALOGUE_REFRESH_S: '0', DASHBOARD_UNIT: '', CATALOGUE_IDLE_S: '6', HEARTBEAT_S: '5' }) });
    bridge.stdout.on('data', d => String(d).split('\n').filter(Boolean).forEach(l => { try { lines.push(JSON.parse(l)); } catch (e) {} }));
    return sleep(800);
  };
  const ev = (name, k) => lines.filter(l => l.event === name).map(l => k ? l[k] : l);
  const uiState = () => new Promise(res => http.get('http://127.0.0.1:' + uiPort + '/ui/state.json', r => { let b = ''; r.on('data', c => b += c); r.on('end', () => res(JSON.parse(b))); }).on('error', () => res(null)));
  await start();

  const INIT = base => {
    window.LOG = []; window.NOVA_NANOPI = { base };
    window.tizen = { application: { getAppsInfo: ok => ok([{ id: 'org.tizen.netflix-app', name: 'Netflix', show: true }, { id: '111299001912', name: 'YouTube', show: true }]), launch: (id, ok) => { LOG.push('launch ' + id); ok && ok(); }, getCurrentApplication: () => ({ appInfo: { packageId: 'OpnTV00001' } }), launchAppControl: () => {} }, ApplicationControl: function () {},
      tvinputdevice: { registerKey: k => LOG.push('registerKey ' + k) },
      systeminfo: { getPropertyValue: (k, ok) => ok({ connected: [{ type: 'HDMI', number: 1 }], disconnected: [] }) },
      tvwindow: { setSource: (s, ok) => { LOG.push('setSource ' + s.type + s.number); ok(s); }, show: (ok, err, rect, type, z) => { LOG.push('show ' + type + ' ' + z); ok(rect, type); }, hide: ok => { LOG.push('hide'); ok && ok(); } } };
  };
  const b = await chromium.launch(); let p;
  const errs = [];
  const openPage = async () => {
    p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
    p.on('pageerror', e => errs.push(e.message));
    await p.addInitScript(INIT, 'http://127.0.0.1:' + port);
    await p.goto('http://127.0.0.1:' + webPort + '/app/index.html'); await p.waitForTimeout(2500);
  };
  await openPage();
  const key = async (k, wait = 500) => { await p.keyboard.press(k); await p.waitForTimeout(wait); };
  const code = async (c, wait = 500) => { await p.evaluate(c => document.dispatchEvent(new KeyboardEvent('keydown', { keyCode: c, bubbles: true })), c); await p.waitForTimeout(wait); };
  const st = () => p.evaluate(() => ({ cls: document.getElementById('stage').className, f: (document.querySelector('.focused') || { getAttribute: () => '' }).getAttribute('data-key'), toast: document.getElementById('toast').textContent, log: LOG.slice() }));
  const openFilms = async () => { const f = (await st()).f; if (f !== 'ext-catalogue') { await key('ArrowRight', 150); await key('ArrowRight', 150); } await key('Enter', 1200); };

  // 1. ouverture réelle
  await openFilms(); let s = await st(); let u = await uiState();
  ok('tuile Films -> session ouverte par le vrai pont', ev('session_open').length === 1 && u.state === 'catalogue', JSON.stringify(u));
  ok('HDMI 1 BEHIND côté OpenTV', s.log.includes('show MAIN BEHIND') && /\blive\b/.test(s.cls) && !/peek/.test(s.cls), s.cls);

  // 2. navigation relayée (curseur appartenant au pont)
  await key('ArrowDown', 300); await key('ArrowRight', 300); u = await uiState();
  ok('Bas + Droite -> curseur 7 dans le pont', u.cursor === 7, JSON.stringify(u));
  await key('Enter', 600); u = await uiState();
  ok('OK -> page publique du film (state player, fiche 1007)', u.state === 'player' && ev('state', 'item').includes('1007'), JSON.stringify(ev('state')));
  await key('Escape', 600); u = await uiState();
  ok('Retour -> catalogue, position conservée', u.state === 'catalogue' && u.cursor === 7, JSON.stringify(u));
  await key('Escape', 800); u = await uiState(); s = await st();
  ok('Retour racine -> tableau de bord (pont), HDMI 1 reste affiché', u.state === 'dashboard' && ev('session_end', 'reason').slice(-1)[0] === 'back' && /\blive\b/.test(s.cls) && !s.log.includes('hide'), ev('session_end', 'reason').join(','));
  await key('ArrowRight', 500); s = await st();
  ok('OpenTV sorti du mode externe (1re touche = menu)', /peek/.test(s.cls), s.cls);
  await key('Escape', 400);

  // 3. Exit -> close(user)
  await key('ArrowRight', 400); await key('Enter', 1200); u = await uiState();
  ok('réouverture', u.state === 'catalogue', JSON.stringify(u));
  await code(10071, 800);
  ok('Exit -> close reason tv-user', ev('session_end', 'reason').slice(-1)[0] === 'tv-user' && (await uiState()).state === 'dashboard', ev('session_end', 'reason').join(','));

  // 4. inactivité catalogue (6 s) -> tableau de bord ; OpenTV le voit par /v1/health
  await key('ArrowRight', 400); await key('Enter', 1200);
  await p.waitForTimeout(9000);
  ok('inactivité -> fin idle côté pont', ev('session_end', 'reason').slice(-1)[0] === 'idle', ev('session_end', 'reason').join(','));
  const before = (await uiState()).cursor; await key('ArrowDown', 500); s = await st();
  ok('OpenTV a quitté le mode externe (touche non relayée)', /peek/.test(s.cls) && (await uiState()).state === 'dashboard', s.cls);
  await key('Escape', 400);

  // 5. panne du pont (SIGKILL) pendant la session -> OpenTV « NanoPi injoignable »
  await key('ArrowRight', 400); await key('Enter', 1200);
  bridge.kill('SIGKILL'); await p.waitForTimeout(7500); s = await st();
  ok('pont tué -> OpenTV sort avec message', /NanoPi injoignable/.test(s.toast) && /\blive\b/.test(s.cls), s.toast + ' ' + s.cls);
  await start(); lines = lines.filter(l => l.event === 'start');
  ok('pont redémarré : aucune session fantôme', (await uiState()).state === 'dashboard');
  await key('ArrowRight', 400); await key('Escape', 400);

  // 6. TV perdue (page OpenTV fermée sans close) -> fin tv-lost (HEARTBEAT_S=5)
  await key('ArrowRight', 400); await key('Enter', 1200);
  ok('session rouverte après redémarrage', (await uiState()).state === 'catalogue');
  await p.close(); await sleep(7500);
  ok('plus de /v1/health -> fin tv-lost', ev('session_end', 'reason').slice(-1)[0] === 'tv-lost', ev('session_end', 'reason').join(','));

  ok('aucune erreur JS', errs.length === 0, errs.join(' / '));
  ok('aucune réponse 5xx/erreur interne du pont', ev('api_error').length === 0);
  console.log(R.join('\n'));
  bridge.kill('SIGTERM'); await b.close(); web.close(); fs.rmSync(state, { recursive: true, force: true });
  process.exit(R.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
