const { chromium } = require('playwright'); const http=require('http'),fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname,'wgt'); const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png'};
const web=http.createServer((q,r)=>{const f=path.join(ROOT,decodeURIComponent(q.url.split('?')[0]));fs.readFile(f,(e,d)=>{if(e){r.writeHead(404);return r.end();}r.writeHead(200,{'Content-Type':mime[path.extname(f)]||'x'});r.end(d);});}).listen(8091);
(async()=>{const b=await chromium.launch();const p=await b.newPage({viewport:{width:1920,height:1080}});
await p.addInitScript(()=>{const A=[['n','Netflix'],['y','YouTube'],['p','Prime Video'],['d','Disney+'],['a','Apple TV'],['s','Spotify'],['x','Plex'],['t','Tubi'],['c','Crunchyroll'],['pp','Paramount+'],['am','Apple Music'],['org.tizen.browser','Internet']].map(x=>({id:x[0],name:x[1],show:true}));
 window.tizen={application:{getAppsInfo:ok=>ok(A),launch:()=>{},getCurrentApplication:()=>({appInfo:{packageId:'OpnTV00001'}}),launchAppControl:()=>{}},ApplicationControl:function(){},systeminfo:{getPropertyValue:(k,ok)=>ok({connected:[{type:'HDMI',number:1}],disconnected:[]})},tvwindow:{setSource:(s,ok)=>ok(s),show:ok=>ok(),hide:ok=>ok&&ok()}};});
await p.goto('http://127.0.0.1:8091/app/index.html');await p.waitForTimeout(3000);await p.keyboard.press('Escape');await p.waitForTimeout(800);await p.keyboard.press('ArrowRight');await p.waitForTimeout(800);
const n=await p.$$eval('#tiles .tile',e=>e.length);let bad=[],seq=[];
for(let i=1;i<n;i++){await p.keyboard.press('ArrowRight');await p.waitForTimeout(350);
 const v=await p.evaluate(()=>{const f=document.querySelector('.focused'),t=document.getElementById('tiles').getBoundingClientRect(),r=f.getBoundingClientRect();return {f:f.textContent.trim(),ok:r.left>=t.left-1&&r.right<=t.right+1};});
 if(!v.ok)bad.push(v.f); seq.push(v.f||'(bouton)');}
const last=await p.evaluate(()=>document.querySelector('.focused').textContent.trim());
await p.screenshot({path:'/home/claude/e2e/peek-twelve-scrolled.png'});
for(let i=1;i<n;i++){await p.keyboard.press('ArrowLeft');await p.waitForTimeout(200);}
const first=await p.evaluate(()=>{const f=document.querySelector('.focused'),t=document.getElementById('tiles').getBoundingClientRect(),r=f.getBoundingClientRect();return f.textContent.trim()+' visible='+(r.left>=t.left-1);});
console.log(seq.join(' > '));console.log(n+' tuiles; dernière atteinte: '+last+'; tuiles hors champ pendant le défilement: '+(bad.length?bad.join(','):'aucune')+'; retour au début: '+first);
await b.close();web.close();})();
