const { chromium } = require('playwright');
const { spawn } = require('child_process');
const http = require('http'); const fs = require('fs'); const path = require('path');
const ROOT = path.join(__dirname, 'wgt');
const mime = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.jpg':'image/jpeg', '.png':'image/png' };
const web = http.createServer((q, r) => { const f = path.join(ROOT, decodeURIComponent(q.url.split('?')[0])); fs.readFile(f, (e, d) => { if (e) { r.writeHead(404); return r.end(); } r.writeHead(200, {'Content-Type': mime[path.extname(f)] || 'application/octet-stream'}); r.end(d); }); }).listen(8099);
const get = u => new Promise(res => http.get(u, r => { let b=''; r.on('data', c => b+=c); r.on('end', () => res(b)); }).on('error', () => res(null)));
const results = []; const ok = (name, cond, extra='') => { results.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? '  [' + extra + ']' : '')); };
(async () => {
  let svc = null; const launched = []; let svcStartCalls = 0;
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  const errs = []; p.on('pageerror', e => errs.push(e.message)); p.on('console', m => { if (/LAC|initscript/.test(m.text())) console.log('[page]', m.text()); });
  await p.exposeFunction('__startSvc', () => { svcStartCalls++; if (!svc) { svc = spawn('node', ['svc-wrapper.js'], { cwd: __dirname }); svc.stdout.on('data', d => process.stdout.write('[svc] ' + d)); } });
  await p.exposeFunction('__launched', id => launched.push(id));
  const apps = fs.readFileSync(path.join(__dirname, 'apps.json'), 'utf8');
  await p.addInitScript(appsJson => {
    const APPS = JSON.parse(appsJson);
    localStorage.setItem('opentv.apps.v1', JSON.stringify(APPS.map(x => ({ id:x.id, name:x.name }))));
    window.tizen = {
      application: {
        getAppsInfo: (ok) => setTimeout(() => ok(APPS), 1200),
        launch: (id, ok) => { window.__launched(id); ok && ok(); },
        getCurrentApplication: () => ({ appInfo: { packageId: 'OpnTV00001' } }),
        launchAppControl: (ctrl, id, ok) => { console.log('LAC '+location.href+' '+new Error().stack.split('\n').slice(2,4).join(' <- ')); window.__lastCtrl = { op: ctrl.operation, id }; window.__startSvc(); ok && ok(); }
      },
      ApplicationControl: function (op) { this.operation = op; }
    };
  }, apps);
  await p.goto('http://127.0.0.1:8099/app/index.html');
  await p.waitForTimeout(200);
  const early = await p.$$eval('#tiles .tile', els => els.map(e => e.textContent.trim()).join(' | '));
  ok('accueil depuis le cache avant getAppsInfo', /Netflix/.test(early), early);
  await p.waitForTimeout(3300);
  // 1. démarrage du service
  const ctrl = await p.evaluate(() => window.__lastCtrl);
  ok('service démarré via launchAppControl', ctrl && ctrl.id === 'OpnTV00001.OpenTVService' && ctrl.op === 'http://tizen.org/appcontrol/operation/service', JSON.stringify(ctrl));
  const ver = await get('http://127.0.0.1:8096/version'); ok('service répond /version', ver && /"version":17/.test(ver), ver);
  // 2. tuiles
  const tiles = await p.$$eval('#tiles .tile, #tiles [id^="app-"]', els => els.map(e => e.textContent.trim()));
  const names = tiles.join(' | ');
  ok('Netflix présent', /Netflix/.test(names)); ok('YouTube présent une seule fois', (names.match(/YouTube/g) || []).length === 1, names);
  ok('applis système cachées absentes', !/Paramètres|Apps\b/.test(names));
  ok('OpenTV/TizenBrew pas dans les tuiles', !/OpenTV|TizenBrew/.test(names));
  const build = await p.textContent('#build'); ok('build tag natif', /v22 native/.test(build) && !/aperçu/.test(build), build);
  // 3. télécommande iPhone -> focus
  const before = await p.evaluate(() => (document.querySelector('.focused')||{}).id || (document.querySelector('.focused')||{}).textContent);
  await get('http://127.0.0.1:8096/cmd?cmd=right'); await p.waitForTimeout(1200);
  const after = await p.evaluate(() => (document.querySelector('.focused')||{}).id || (document.querySelector('.focused')||{}).textContent);
  ok('télécommande iPhone déplace le focus', before !== after, before + ' -> ' + after);
  // 4. OK -> lance l'appli focalisée directement via tizen.application.launch
  await get('http://127.0.0.1:8096/cmd?cmd=ok'); await p.waitForTimeout(1200);
  ok('OK lance une appli via API Tizen', launched.length === 1, launched.join(','));
  // 5. touche Retour Samsung (10009) gérée
  await p.evaluate(() => document.dispatchEvent(Object.assign(new KeyboardEvent('keydown', { bubbles: true }), {})));
  // 6. page /remote servie
  const remote = await get('http://127.0.0.1:8096/remote'); ok('page télécommande servie', remote && /TÉLÉCOMMANDE OPENTV/.test(remote));
  const st = JSON.parse(await get('http://127.0.0.1:8096/status'));
  ok('/status rapporte l\'écran', st.client && /v22 native/.test(st.client.build) && st.client.screen === 'home' && st.client.apps === 8 && /^app-/.test(st.client.focus), JSON.stringify(st.client));
  await get('http://127.0.0.1:8096/cmd?cmd=right'); await get('http://127.0.0.1:8096/cmd?cmd=up'); await p.waitForTimeout(1200);
  const moved = await p.evaluate(() => (document.querySelector('.focused')||{}).textContent);
  await p.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => false }); document.dispatchEvent(new Event('visibilitychange')); });
  await p.waitForTimeout(500);
  const back = await p.evaluate(() => ({ f: (document.querySelector('.focused')||{}).textContent, home: document.getElementById('home').classList.contains('active') }));
  ok('retour au premier plan -> accueil, 1re tuile', back.home && /Netflix/.test(back.f), (moved||'').trim() + ' -> ' + (back.f||'').trim());
  ok('aucune erreur JS', errs.length === 0, errs.join(' / '));
  ok('service démarré une seule fois', svcStartCalls === 1, String(svcStartCalls));
  await p.screenshot({ path: require('path').join(process.env.OPENTV_SHOTS||require('os').tmpdir(),'native-mock-tv.png') });
  console.log('\n' + results.join('\n'));
  await b.close(); web.close(); if (svc) svc.kill();
  process.exit(results.some(r => r.startsWith('FAIL')) ? 1 : 0);
})();
