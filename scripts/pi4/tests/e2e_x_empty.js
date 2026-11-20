/* Local anonymous blank-feed reproduction. No X request leaves this context. One Chromium at a time.
 * node e2e_x_empty.js <playwright-module> <config.json>; config: chrome, ext (null = disabled), shots, label, expect.
 * The fixture models the reported For you + spinner, NOT a captured live DOM or a proven remote failure cause. */
"use strict";
const fs = require("fs"), os = require("os"), path = require("path"), assert = require("assert/strict");
const pw = require(process.argv[2]), cfg = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const style = `<style>body{margin:0;background:#000;color:#eee;font:28px system-ui}main{display:flex;align-items:center}
[data-testid=primaryColumn]{width:600px;min-height:900px}#tabs{height:80px;border-bottom:1px solid #555}
#pending{height:600px;display:flex;align-items:center;justify-content:center}#spinner{width:36px;height:36px;border:5px solid #234;border-top-color:#1d9bf0;border-radius:50%}
button,a{font:inherit;color:inherit;background:#18232c;padding:18px;margin:16px}article{min-height:200px}
#dlg{position:fixed;top:200px;left:500px;background:#333;padding:40px;z-index:100}
#late{min-height:200px}</style>`;
const post = `<article data-testid="tweet"><div data-testid="User-Name">Fixture @fixture</div><a href="/fixture/status/1"><time>1h</time></a><div data-testid="tweetText">Publication locale différée — aucune donnée de compte.</div></article>`;
const body = `<!doctype html><html lang="fr"><meta charset="utf-8">${style}<body><main role="main"><div data-testid="primaryColumn">
<div id="tabs"><a role="tab" href="/home" id="foryou">For you</a></div><div id="pending"><div id="spinner" role="progressbar" aria-label="Loading"></div></div><div id="result"></div></div></main>
<script>window.acts=[];window.finish=()=>{document.getElementById('pending').remove();document.getElementById('result').innerHTML=${JSON.stringify(post)}};
window.fail=()=>{document.getElementById('pending').innerHTML='<div role="alert">Something went wrong.<button id="retry" type="button">Retry</button></div>';document.getElementById('retry').addEventListener('click',e=>{window.acts.push({id:'retry',trusted:e.isTrusted});window.finish()})};
window.lazy=()=>{document.getElementById('pending').innerHTML='<div id="late"></div>';new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting))window.finish()}).observe(document.getElementById('late'))};
window.dialogue=()=>{document.body.insertAdjacentHTML('beforeend','<div role="dialog" aria-modal="true" id="dlg"><button id="dismiss">Not now</button></div>');document.getElementById('dismiss').onclick=e=>{window.acts.push({id:'dismiss',trusted:e.isTrusted});document.getElementById('dlg').remove()}};
</script></body></html>`;
(async()=>{
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'otvxempty-'));
 let ctx;
 const out={label:cfg.label,cases:{},errors:[],requests:[]};
 try {
  ctx=await pw.chromium.launchPersistentContext(profile,{executablePath:cfg.chrome,headless:true,viewport:{width:1920,height:1080},
   ignoreDefaultArgs:cfg.ext?["--disable-extensions"]:[],args:cfg.ext?["--disable-extensions-except="+cfg.ext,"--load-extension="+cfg.ext]:[]});
  if(cfg.ext){if(!ctx.serviceWorkers().length)await ctx.waitForEvent('serviceworker',{timeout:12000});}
  let loads=0;
  await ctx.route('**/*',async r=>{
   const u=new URL(r.request().url());
   if(u.origin==='https://x.com' && r.request().isNavigationRequest()){
    loads++;out.requests.push(u.pathname);
    return r.fulfill({contentType:'text/html',body:u.pathname==='/login'?`<!doctype html>${style}<body><a href='/i/jf/onboarding/web?mode=login' id='signin'>Sign in</a><input autocomplete='username'></body>`:body});
   }
   if(u.origin==='http://127.0.0.1:8080'&&u.pathname==='/tv')return r.fulfill({contentType:'text/html',body:`<a id="tile" href="https://x.com/home">X</a>`});
   return r.abort();
  });
  const page=ctx.pages()[0];page.on('pageerror',e=>out.errors.push(e.message));
  const settle=()=>page.waitForTimeout(600); // bounded observation window, NOT navigation readiness
  const open=async()=>{await page.goto('https://x.com/home');if(cfg.ext)await page.waitForSelector('html.otvx-tv');await settle()};
  const snap=()=>page.evaluate(()=>({posts:document.querySelectorAll('article').length,spinner:!!document.getElementById('spinner'),
   help:document.querySelector('.otvx-help')?.textContent||'',recovery:!!document.querySelector('.otvx-empty'),focus:document.activeElement.id,
   visible:Array.from(document.querySelectorAll('#tabs,#pending,[role=alert]')).map(e=>({id:e.id,visible:e.checkVisibility({visibilityProperty:true,opacityProperty:true}),rect:[e.offsetWidth,e.offsetHeight]}))}));
  const shot=async name=>{if(cfg.shots){fs.mkdirSync(cfg.shots,{recursive:true});await page.screenshot({path:path.join(cfg.shots,cfg.label+'-'+name+'.png')})}};
  await open();out.cases.first=await snap();await shot('spinner');
  await settle();assert.equal(loads,1,'no automatic reload');assert.equal((await snap()).posts,0,'persistent failure must not invent feed');
  if(cfg.expect){
   await page.evaluate(()=>{const b=document.createElement('button');b.id='low-retry';b.textContent='Retry';b.style.cssText='position:fixed;bottom:130px;left:800px';document.querySelector('[data-testid=primaryColumn]').appendChild(b)});await settle();
   const overlap=await page.evaluate(()=>{const a=document.querySelector('.otvx-empty').getBoundingClientRect(),b=document.getElementById('low-retry').getBoundingClientRect();return a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top});
   assert.equal(overlap,false,'recovery must not cover a native Retry');
   await page.evaluate(()=>document.getElementById('low-retry').remove());await settle();
   await page.evaluate(()=>{const a=document.createElement('div');a.id='full-error';a.role='alert';a.style.cssText='position:fixed;top:0;left:0;width:100vw;height:100vh';a.textContent='Native error';document.body.appendChild(a)});await settle();
   assert.equal(await page.locator('.otvx-empty').isVisible(),false,'no free box: do not cover native state');
   assert.equal(await page.evaluate(()=>document.querySelector('.otvx-login-help').textContent.includes('Recharger')),false,'hidden recovery must not be advertised');
   await page.evaluate(()=>document.getElementById('full-error').remove());await settle();
  }
  if(cfg.expect){
   // Recovery is focus-only: ARIA roles do not acquire a synthetic click fallback.
   out.cases.roleOk=[];
   for(const variant of ['plain','site-handler','modal-during-key','post-during-key']){
    await open();
    await page.evaluate(({variant,post})=>{
     const b=document.createElement('div');b.id='role-retry';b.role='button';b.tabIndex=0;b.textContent='Retry';
     document.getElementById('pending').replaceChildren(b);
     b.addEventListener('click',e=>window.acts.push({kind:'click',trusted:e.isTrusted}));
     b.addEventListener('keydown',e=>{
      if(e.key!=='Enter')return;
      window.acts.push({kind:'key',trusted:e.isTrusted});
      if(variant==='site-handler'){window.acts.push({kind:'site-action',trusted:e.isTrusted});}
      if(variant==='modal-during-key'){window.dialogue();}
      if(variant==='post-during-key'){document.getElementById('result').innerHTML=post;}
     });
    },{variant,post});await settle();
    await page.keyboard.press('ArrowDown');await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(()=>document.activeElement.id),'role-retry');
    await page.keyboard.press('Enter');await settle();
    const acts=await page.evaluate(()=>window.acts);out.cases.roleOk.push({variant,acts});
    assert.deepEqual(acts,variant==='site-handler'?[{kind:'key',trusted:true},{kind:'site-action',trusted:true}]:
     [{kind:'key',trusted:true}],variant+': one native Enter, no synthetic click');
    if(variant==='post-during-key')assert.equal((await snap()).recovery,false);
    if(variant==='modal-during-key'){
     assert.equal((await snap()).recovery,false);await page.keyboard.press('ArrowDown');
     assert.equal(await page.evaluate(()=>document.activeElement.id),'dismiss');
    }
   }
   // Login/modal capture must not leave a pending synthetic activation when the same key reveals a blank feed.
   await open();await page.evaluate(()=>{
    window.dialogue();const dlg=document.getElementById('dlg');
    dlg.innerHTML='<div role="button" tabindex="0" id="modal-role">Retry</div>';
    const b=document.getElementById('modal-role');
    b.onclick=e=>window.acts.push({kind:'click',trusted:e.isTrusted});
    b.onkeydown=e=>{if(e.key==='Enter'){window.acts.push({kind:'key',trusted:e.isTrusted});dlg.removeAttribute('aria-modal')}};
   });await settle();await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');await settle();
   out.cases.modalToRecovery=await page.evaluate(()=>window.acts);
   assert.deepEqual(out.cases.modalToRecovery,[{kind:'key',trusted:true}],'no pending login click after mode transition');
   // The advertised upward gesture must still reach Reload beyond both control-list caps.
   await open();await page.evaluate(()=>{
    const col=document.querySelector('[data-testid=primaryColumn]');
    for(let i=0;i<405;i++){const b=document.createElement('button');b.textContent='X control '+i;b.style.cssText='position:absolute;left:20px;top:100px';col.appendChild(b)}
   });await settle();
   assert(await page.locator('.otvx-login-help').textContent().then(t=>t.includes('Recharger X')));
   await page.keyboard.press('ArrowUp');
   out.cases.cappedRecoveryFocus=await page.evaluate(()=>document.activeElement.textContent);
   assert.equal(out.cases.cappedRecoveryFocus,'Recharger X','advertised Reload remains reachable after capped native controls');
  }
  await open();await page.evaluate(()=>window.fail());await settle();
  const focuses=[];for(let i=0;i<4;i++){await page.keyboard.press('ArrowDown');focuses.push(await page.evaluate(()=>document.activeElement.id));if(focuses.at(-1)==='retry')break;}
  out.cases.retry={focuses,before:await snap()};
  if(focuses.includes('retry')){await page.keyboard.press('Enter');await page.waitForSelector('article');out.cases.retry.acts=await page.evaluate(()=>window.acts);}
  if(cfg.expect)assert(focuses.includes('retry'),'RED: primaryColumn without posts must allow remote focus of native Retry');
  await open();await page.evaluate(()=>window.lazy());await page.waitForSelector('article');await settle();out.cases.lazy=await snap();
  if(cfg.expect){assert.equal(out.cases.lazy.recovery,false);assert(out.cases.lazy.help.includes('Post précédent'));}
  // Virtualization removes the last article, then replaces it, without a navigation.
  await page.evaluate(()=>document.getElementById('result').replaceChildren());await settle();out.cases.virtualEmpty=await snap();
  await page.evaluate(()=>document.getElementById('result').innerHTML=`<article data-testid="tweet" style="display:none">hidden placeholder</article>`);await settle();out.cases.hidden=await snap();
  if(cfg.expect){
   await page.evaluate(()=>document.querySelector('article').style.display='block');await settle();
   assert.equal((await snap()).recovery,false,'rendered in-place article must clear recovery');
  }
  await page.evaluate(()=>document.getElementById('result').innerHTML=`<div id="deferred"></div>`);await settle();
  await page.evaluate(p=>document.getElementById('deferred').innerHTML=p,post);await page.waitForSelector('article');await settle();out.cases.deferred=await snap();
  if(cfg.expect){assert(out.cases.virtualEmpty.recovery);assert(out.cases.hidden.recovery);assert(!out.cases.deferred.recovery);}
  await open();await page.evaluate(()=>window.dialogue());await settle();await page.keyboard.press('ArrowDown');out.cases.dialog={focus:await page.evaluate(()=>document.activeElement.id),snap:await snap()};
  if(cfg.ext)assert.equal(out.cases.dialog.focus,'dismiss','dialog always wins');
  if(cfg.expect)assert.equal(out.cases.dialog.snap.recovery,false);
  await page.goto('https://x.com/login');await settle();out.cases.login=await snap();if(cfg.ext)assert.equal(out.cases.login.focus,'signin');
  // Appliance Return restarts the kiosk on /tv; model its destination, NOT evdev or service execution.
  await page.goto('http://127.0.0.1:8080/tv');await page.focus('#tile');await page.keyboard.press('Enter');await page.waitForURL('https://x.com/home');await settle();out.cases.reopen=await snap();
  if(cfg.expect){
   assert(out.cases.reopen.recovery);assert(!out.cases.reopen.help.includes('Post précédent'));
   // Reload is an explicit native button activation, never a timer, synthetic click or key.
   const n=loads;
   await page.keyboard.press('ArrowUp');assert.equal(await page.evaluate(()=>document.activeElement.textContent),'Recharger X');await shot('recovery-focus');
   await page.evaluate(()=>{const b=document.querySelector('.otvx-empty button');b.click();window.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp'}))});await settle();assert.equal(loads,n,'synthetic input on focused button must not reload');
   await Promise.all([page.waitForEvent('domcontentloaded'),page.keyboard.press('Enter')]);await settle();assert.equal(loads,n+1,'one human OK reloads once');out.cases.reload=await snap();assert(out.cases.reload.recovery);assert.equal(out.cases.reload.posts,0);
   await page.evaluate(()=>window.finish());await settle();assert.equal((await snap()).recovery,false,'late success removes recovery');
   // First key after replacement is feed navigation, never a reload.
   const before=loads;await page.keyboard.press('ArrowDown');assert.equal(loads,before);assert.equal(await page.locator('article.otvx-focus').count(),1);
  }
  assert.deepEqual(out.errors,[]);out.ok=true;
 }catch(e){out.ok=false;out.failure=e.stack;process.exitCode=1;}
 finally{if(ctx)await ctx.close();fs.rmSync(profile,{recursive:true,force:true});fs.writeFileSync(cfg.result,JSON.stringify(out,null,2));console.log(JSON.stringify(out));}
})();
