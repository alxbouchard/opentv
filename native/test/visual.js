const { chromium } = require('playwright'); const http=require('http'),fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname, 'wgt'); const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png'};
const web=http.createServer((q,r)=>{const f=path.join(ROOT,decodeURIComponent(q.url.split('?')[0]));fs.readFile(f,(e,d)=>{if(e){r.writeHead(404);return r.end();}r.writeHead(200,{'Content-Type':mime[path.extname(f)]||'x'});r.end(d);});}).listen(8092);
const SETS={six:[['org.tizen.netflix-app','Netflix'],['com.samsung.tv.cobalt-yt','YouTube'],['evKhCgZelL.AmazonIgnitionLauncher2','Prime Video'],['rJeHak5zRg.Spotify','Spotify'],['org.tizen.channel-list','Live TV'],['org.tizen.browser','Internet']],
 twelve:[['n','Netflix'],['y','YouTube'],['p','Prime Video'],['d','Disney+'],['a','Apple TV'],['s','Spotify'],['x','Plex'],['t','Tubi'],['c','Crunchyroll'],['pp','Paramount+'],['am','Apple Music'],['org.tizen.browser','Internet']]};
(async()=>{const b=await chromium.launch();
for (const [name,set] of Object.entries(SETS)) {
 const p=await b.newPage({viewport:{width:1920,height:1080}});const errs=[];p.on('pageerror',e=>errs.push(e.message));
 await p.addInitScript(a=>{const APPS=a.map(x=>({id:x[0],name:x[1],show:true}));try{localStorage.clear();}catch(e){}
  window.tizen={application:{getAppsInfo:ok=>ok(APPS),launch:()=>{},getCurrentApplication:()=>({appInfo:{packageId:'OpnTV00001'}}),launchAppControl:()=>{}},ApplicationControl:function(){},
  systeminfo:{getPropertyValue:(k,ok)=>ok({connected:[{type:'HDMI',number:1}],disconnected:[]})},tvwindow:{setSource:(s,ok)=>ok(s),show:ok=>ok(),hide:ok=>ok&&ok()}};
  // fausse image HDMI 1 derrière la page transparente
  document.addEventListener('DOMContentLoaded',()=>{const v=document.createElement('div');v.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;background:url(backgrounds/city-dusk.jpg) center/cover';
   v.innerHTML='<div style="position:absolute;top:60px;left:80px;font:600 54px sans-serif;color:#fff;text-shadow:0 2px 8px #000">HDMI 1 — image en direct (simulée)</div>';document.body.insertBefore(v,document.body.firstChild);});
 },set);
 await p.goto('http://127.0.0.1:8092/app/index.html');await p.waitForTimeout(3000);
 await p.keyboard.press('Escape');await p.waitForTimeout(900);
 await p.screenshot({path:require('path').join(process.env.OPENTV_SHOTS||require('os').tmpdir(),`peek-${name}-0-full.png`)});
 await p.keyboard.press('ArrowRight');await p.waitForTimeout(900);
 await p.screenshot({path:require('path').join(process.env.OPENTV_SHOTS||require('os').tmpdir(),`peek-${name}-1.png`)});
 const n=set.length; for(let i=0;i<n;i++){await p.keyboard.press('ArrowRight');await p.waitForTimeout(250);}
 await p.waitForTimeout(500);
 const vis=await p.evaluate(()=>{const f=document.querySelector('.focused'),t=document.getElementById('tiles').getBoundingClientRect(),r=f.getBoundingClientRect();return {f:f.textContent.trim().slice(0,30),inRow:r.left>=t.left-2&&r.right<=t.right+40,top:Math.round(r.top),bottom:Math.round(r.bottom),hero:getComputedStyle(document.getElementById('hero')).display,clock:getComputedStyle(document.getElementById('clockBox')).display};});
 await p.screenshot({path:require('path').join(process.env.OPENTV_SHOTS||require('os').tmpdir(),`peek-${name}-2-end.png`)});
 console.log(name, JSON.stringify(vis), 'erreurs:', errs.length);
 await p.close();}
await b.close();web.close();})();
