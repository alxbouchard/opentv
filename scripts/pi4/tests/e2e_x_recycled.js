/* Offline Chromium counterexamples. argv: Playwright module, Chrome, installed extension.
 * Identical observations for both historical builds and the candidate; assertions in test_x_empty.py. */
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
    const p = ctx.pages()[0], out = {recycle: {}, errors: []};
    p.on('pageerror', e => out.errors.push(e.message));
    let column, navs = 0;
    await ctx.route('**/*', r => {
      if (r.request().url() !== home || !r.request().isNavigationRequest()) return r.abort();
      navs++;
      return r.fulfill({contentType: 'text/html', body: '<!doctype html><style>article{min-height:180px}button{padding:20px}</style>' +
        '<main><div data-testid="primaryColumn">' + column + '</div></main><script>window.acts=[];' +
        'window.recycle=()=>{document.querySelector("article").style.opacity="0";link.href="/fixture/status/2"};' +
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
    for (const phase of ['before','after']) {
      for (const held of [false, true]) {
        await open(post); await key('ArrowDown'); await key('ArrowRight');
        assert.equal((await state()).focus, 'action');
        if (held) await p.keyboard.down('Enter'); // first valid OK on A
        if (phase === 'after') {
          await p.evaluate(() => window.recycle());
          await p.waitForFunction(() => !document.querySelector('[data-otvx-act]'));
          await p.evaluate(() => document.querySelector('#action').focus()); // site restores native focus, not consent
        } else await p.evaluate(() => { window.armed=true; });
        await p.keyboard.down('Enter'); await p.keyboard.up('Enter');
        assert.equal(await p.locator('#link').getAttribute('href'), '/fixture/status/2');
        const acts = await p.evaluate(() => window.acts);
        assert.equal(p.url(), home);
        out.recycle[phase + (held ? '-repeat' : '-single')] = acts;
      }
    }
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
    await p.waitForSelector('.otvx-help'); await key('ArrowDown'); out.media = await state();
    out.autoLoads = navs - loads;
    await open(shell + post); await p.waitForSelector('.otvx-help'); await key('ArrowDown'); out.mixed = await state();
    await p.evaluate(() => document.body.insertAdjacentHTML('beforeend','<div role="dialog" aria-modal="true"><button id="dismiss">Dismiss</button></div>'));
    await key('ArrowDown'); out.modal = await state(); await key('Enter'); out.modalActs = await p.evaluate(() => window.acts);
    console.log(JSON.stringify(out));
  } finally { if (ctx) await ctx.close(); fs.rmSync(profile, {recursive:true, force:true}); }
})().catch(e => { console.error(e); process.exitCode=1; });
