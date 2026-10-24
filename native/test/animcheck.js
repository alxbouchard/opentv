// vérifie le mouvement de la rangée à l'apparition du menu, avec le comportement Chromium 56 simulé
const { chromium } = require('playwright'); const http=require('http'),fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname,process.argv[2]||'wgt'); const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png'};
const web=http.createServer((q,r)=>{const f=path.join(ROOT,decodeURIComponent(q.url.split('?')[0]));fs.readFile(f,(e,d)=>{if(e){r.writeHead(404);return r.end();}r.writeHead(200,{'Content-Type':mime[path.extname(f)]||'x'});r.end(d);});}).listen(8087);
(async()=>{const b=await chromium.launch();let fail=0;
for (const mode of ['moderne','chromium56']) {
 const p=await b.newPage({viewport:{width:1920,height:1080}});
 await p.addInitScript(m=>{const A=[['org.tizen.netflix-app','Netflix'],['y','YouTube'],['p','Prime Video']].map(x=>({id:x[0],name:x[1],show:true}));
  if (m==='chromium56') { const orig=Element.prototype.scrollIntoView; Element.prototype.scrollIntoView=function(){ orig.call(this,true); }; }  // Chrome 56 : objet ignoré -> alignement en haut
  window.tizen={application:{getAppsInfo:ok=>ok(A),launch:()=>{},getCurrentApplication:()=>({appInfo:{packageId:'OpnTV00001'}}),launchAppControl:()=>{}},ApplicationControl:function(){},systeminfo:{getPropertyValue:(k,ok)=>ok({connected:[{type:'HDMI',number:1}],disconnected:[]})},tvwindow:{setSource:(s,ok)=>ok(s),show:ok=>ok(),hide:ok=>ok&&ok()}};},mode);
 await p.goto('http://127.0.0.1:8087/app/index.html');await p.waitForTimeout(3000);
 await p.keyboard.press('Escape');await p.waitForTimeout(1500);
 const s = await p.evaluate(() => new Promise(res => { const out=[], t0=performance.now(), t=document.getElementById('tiles');
   function f(){ out.push({ms:Math.round(performance.now()-t0), top:Math.round(t.getBoundingClientRect().top), scroll:document.documentElement.scrollTop+document.body.scrollTop});
     if (performance.now()-t0<800) requestAnimationFrame(f); else res(out); }
   document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})); f(); }));
 let mono=true; for (let i=1;i<s.length;i++) if (s[i].top > s[i-1].top + 1) mono=false;
 const maxScroll=Math.max(...s.map(x=>x.scroll)), first=s[0].top, last=s[s.length-1].top, minTop=Math.min(...s.map(x=>x.top));
 const ok = mono && maxScroll===0 && first>1080 && minTop>=last-1;
 if(!ok) fail++;
 console.log((ok?'PASS':'FAIL')+' ['+mode+'] départ='+first+'px (sous l\'écran) -> arrivée='+last+'px | monte seulement='+mono+' | plus haut atteint='+minTop+' | défilement page max='+maxScroll);
 await p.close(); }
await b.close();web.close();process.exit(fail?1:0);})();
