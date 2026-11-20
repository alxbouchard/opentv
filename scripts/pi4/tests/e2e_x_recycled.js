/* Offline Chromium counterexamples. argv: Playwright module, Chrome, installed extension.
 * Identical observations for historical builds and the candidate; assertions in test_x_empty.py. */
'use strict';
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('node:assert/strict');
const [pw, chrome, ext] = process.argv.slice(2);
const home = 'https://x.com/home';
const shell = '<article id="shell" aria-busy="true" data-testid="tweet"><div role="progressbar">…</div></article>';
const post = '<article id="post" data-testid="tweet"><a id="link" href="/fixture/status/1"><time>1h</time></a>' +
  '<div data-testid="tweetText">Fixture A</div><div role="group"><button id="action" data-testid="like">Like</button></div></article>';
const retry = '<button id="retry">Retry</button>';
(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'otvx-recycle-'));
  let ctx;
  try {
    ctx = await require(pw).chromium.launchPersistentContext(profile, {executablePath: chrome, headless: true,
      viewport: {width: 1920, height: 1080}, ignoreDefaultArgs: ['--disable-extensions'],
      args: ['--disable-extensions-except=' + ext, '--load-extension=' + ext]});
    if (!ctx.serviceWorkers().length) await ctx.waitForEvent('serviceworker');
    const p = ctx.pages()[0], out = {recycle: {}, unmarked: {}, buttonUnmarked: {}, staleEnter: {}, errors: []};
    p.on('pageerror', e => out.errors.push(e.message));
    let column, navs = 0;
    await ctx.route('**/*', r => {
      if (r.request().url() !== home || !r.request().isNavigationRequest()) return r.abort();
      navs++;
      return r.fulfill({contentType: 'text/html', body: '<!doctype html><style>article{min-height:180px}button{padding:20px}</style>' +
        '<main><div data-testid="primaryColumn">' + column + '</div></main><script>window.acts=[];' +
        'window.recycle=()=>{post.style.opacity="0";link.href="/fixture/status/2";if(window.unmark)document.getElementById(window.unmark).removeAttribute("data-testid")};' +
        'window.addEventListener("keydown",e=>{if(window.armed && e.key==="Enter"){window.armed=false;recycle()}},true);' +
        'document.onclick=e=>acts.push([e.target.id,e.isTrusted,document.querySelector("#link")?.pathname])</script>'});
    });
    const open = async html => { column = html; await p.goto(home); await p.waitForSelector('html.otvx-tv'); };
    const key = k => p.keyboard.press(k);
    const state = () => p.evaluate(() => ({selected: document.querySelector('.otvx-focus')?.id || '',
      focus: document.activeElement.id || '', reload: document.activeElement.matches('.otvx-empty button'),
      recovery: !!document.querySelector('.otvx-empty button')?.checkVisibility(), feed: !!document.querySelector('.otvx-help')}));
    // The page registers capture before document_idle (x.js). Recycle synchronously there so
    // no scheduled refresh can run between the identity change and the feed's key handler.
    async function recycled(phase, held, unmark, finalKey = 'Enter') {
      await open(post); await key('ArrowDown'); await key('ArrowRight');
      assert.equal((await state()).focus, 'action');
      await p.evaluate(value => { window.unmark=value; }, unmark);
      if (held) await p.keyboard.down('Enter'); // first valid OK on A
      if (phase === 'after') {
        await p.evaluate(() => window.recycle());
        await p.waitForFunction(() => !document.querySelector('[data-otvx-act]'));
        await p.evaluate(() => document.querySelector('#action').focus()); // site restores native focus, not consent
        assert.equal((await state()).focus, 'action');
      } else await p.evaluate(() => { window.armed=true; });
      await p.evaluate(() => { window.staleEnter = 0; document.querySelector('#action').onkeydown = e => {
        if (e.key === 'Enter' && link.pathname === '/fixture/status/2') window.staleEnter++;
      }; });
      await p.keyboard.down(finalKey); await p.keyboard.up(finalKey);
      assert.equal(await p.locator('#link').getAttribute('href'), '/fixture/status/2');
      assert.equal(await p.locator('#post').getAttribute('data-testid'), unmark === 'post' ? null : 'tweet');
      assert.equal(await p.locator('#action').getAttribute('data-testid'), unmark === 'action' ? null : 'like');
      assert.equal(p.url(), home);
      return p.evaluate(() => window.acts);
    }
    for (const phase of ['before','after']) for (const held of [false,true]) {
      const name = phase + (held ? '-repeat' : '-single');
      out.recycle[name] = await recycled(phase, held, null);
      out.unmarked[name] = await recycled(phase, held, 'post');
      out.buttonUnmarked[name] = await recycled(phase, held, 'action');
      out.staleEnter[name] = await p.evaluate(() => window.staleEnter);
    }
    // Space is not an OpenTV key: isolate Chromium's trusted keyboard click from the OK guard.
    out.unmarkedKeyboardClick = await recycled('after', false, 'action', 'Space');
    await open(post); await key('ArrowDown'); await key('ArrowRight');
    await p.evaluate(() => { document.querySelector('#action').onkeydown = () => window.recycle(); });
    await key('Enter'); out.lateNative = await p.evaluate(() => window.acts);
    assert.equal(await p.locator('#link').getAttribute('href'), '/fixture/status/2');
    await open(shell + retry); await p.waitForSelector('.otvx-empty button, .otvx-help');
    await key('ArrowUp'); out.shellUp = await state(); await key('ArrowUp'); out.shellRetry = await state();
    const loads = navs;
    // Hydrate the SAME article; media alone (no author, text, permalink or actions) counts.
    await p.evaluate(() => { const s=document.querySelector('#shell'); // aria-busy may outlive hydration
      s.innerHTML='<video width="200" height="120"></video>'; });
    // Feed help and recovery panel have separate observers. Require both transitions.
    const hydratedFeed = async () => {
      await p.waitForSelector('.otvx-help');
      await p.locator('.otvx-empty button').waitFor({state:'hidden', timeout:3000});
    };
    await hydratedFeed(); await key('ArrowDown'); out.media = await state();
    out.autoLoads = navs - loads;
    // Keep the sized box AND aria-busy through hydration. Only the contents change.
    out.reserved = {};
    const progress = '<div role="progressbar">…</div>';
    for (const [name, marker, initial, hydrated] of [
      ['emptyText', 'tweetText', '', '<span>Fixture</span>'],
      ['photoSpinner', 'tweetPhoto', progress, '<img width="100" height="60" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'],
      ['hiddenText', 'tweetText', '<span hidden>Fixture</span>' + progress, '<span>Fixture</span>'],
      ['videoShell', 'videoPlayer', progress, '<video width="200" height="120"></video>']
    ]) {
      await open(shell.replace(progress, `<div id="box" style="height:80px" data-testid="${marker}">${initial}</div>` + progress) + retry);
      await p.waitForSelector('.otvx-empty button, .otvx-help');
      const before = navs;
      await key('ArrowUp'); const up = await state(); await key('ArrowUp'); const next = await state();
      await p.locator('#box').evaluate((box, html) => { box.innerHTML = html; }, hydrated);
      await hydratedFeed(); await key('ArrowDown');
      out.reserved[name] = {up, next, hydrated: await state(), extraLoads: navs - before};
    }
    await open(shell + post); await p.waitForSelector('.otvx-help'); await key('ArrowDown'); out.mixed = await state();
    await p.evaluate(() => document.body.insertAdjacentHTML('beforeend','<div role="dialog" aria-modal="true"><button id="dismiss">Dismiss</button></div>'));
    await key('ArrowDown'); out.modal = await state(); await key('Enter'); out.modalActs = await p.evaluate(() => window.acts);
    // Observe the *sender* boundary. An unpaired worker's refusal toast alone says
    // nothing about which message it received. Never return a reply or journal payloads.
    const worker = ctx.serviceWorkers()[0];
    await worker.evaluate(() => {
      self.fixtureIds = [];
      chrome.runtime.onMessage.addListener(m => {
        if (m.type === 'share') self.fixtureIds.push(m.text === 'Fixture A' ? 'A' : m.text === 'Fixture B' ? 'B' : '?');
      });
    });
    const ids = () => worker.evaluate(() => self.fixtureIds.slice());
    const share = async () => {
      // Only the extension reveals its toast: wait until the refusal or the real
      // worker reply has been processed (and thus the passive listener ran).
      await p.locator('.otvx-toast').evaluateAll(nodes => nodes.forEach(n => { n.hidden = true; }));
      await key('ArrowLeft'); await p.waitForSelector('.otvx-toast:visible');
      return {ids: await ids(), pick: (await p.locator('.otvx-toast').innerText()).includes('↑ ↓')};
    };
    const neighbor = '<article id="neighbor" data-testid="tweet"><a href="/fixture/status/3"><time>1h</time></a></article>';
    await open(neighbor + post); await key('ArrowDown'); await key('ArrowDown');
    assert.equal((await state()).selected, 'post');
    out.shareA = await share();
    await p.evaluate(() => {
      document.querySelector('#link').href = '/fixture/status/2';
      document.querySelector('#post [data-testid=tweetText]').textContent = 'Fixture B';
    });
    out.shareStale = await share();
    await key('ArrowUp'); assert.equal((await state()).selected, 'neighbor');
    await key('ArrowDown'); assert.equal((await state()).selected, 'post');
    out.shareReselected = await share();
    // Move the still visible, still identically keyed article outside the feed.
    // The neighboring article keeps feed navigation active during the check.
    await p.evaluate(() => document.body.appendChild(document.querySelector('#post')));
    out.shareOutside = await share();
    await open(neighbor + post.replace('/fixture/status/1', '/fixture/unknown'));
    await key('ArrowDown'); await key('ArrowDown'); assert.equal((await state()).selected, 'post');
    out.shareNoKey = await share();
    console.log(JSON.stringify(out));
  } finally { if (ctx) await ctx.close(); fs.rmSync(profile, {recursive:true, force:true}); }
})().catch(e => { console.error(e); process.exitCode=1; });
