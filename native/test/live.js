const { chromium } = require('playwright'); const http=require('http'),fs=require('fs'),path=require('path');
const ROOT=path.join(__dirname,'wgt'); const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png'};
const web=http.createServer((q,r)=>{const f=path.join(ROOT,decodeURIComponent(q.url.split('?')[0]));fs.readFile(f,(e,d)=>{if(e){r.writeHead(404);return r.end();}r.writeHead(200,{'Content-Type':mime[path.extname(f)]||'x'});r.end(d);});}).listen(8093);
const R=[];const ok=(n,c,x='')=>R.push((c?'PASS ':'FAIL ')+n+(x?'  ['+x+']':''));
(async()=>{const b=await chromium.launch();const p=await b.newPage({viewport:{width:1920,height:1080}});const errs=[];p.on('pageerror',e=>errs.push(e.message));
await p.addInitScript(()=>{window.LOG=[];window.tizen={application:{getAppsInfo:ok=>ok([{id:'org.tizen.netflix-app',name:'Netflix',show:true},{id:'111299001912',name:'YouTube',show:true}]),launch:(id,ok)=>{LOG.push('launch '+id);ok&&ok();},getCurrentApplication:()=>({appInfo:{packageId:'OpnTV00001'}}),launchAppControl:()=>{}},ApplicationControl:function(){},
 systeminfo:{getPropertyValue:(k,ok)=>{LOG.push('sysinfo '+k);ok({connected:[{type:'TV',number:1},{type:'HDMI',number:2}],disconnected:[{type:'HDMI',number:1},{type:'HDMI',number:3}]});}},
 tvwindow:{setSource:(s,ok)=>{LOG.push('setSource '+s.type+s.number);ok(s);},show:(ok,err,rect,type,z)=>{LOG.push('show '+rect.join(',')+' '+type+' '+z);ok(rect,type);},hide:(ok)=>{LOG.push('hide');ok&&ok();}}};});
await p.goto('http://127.0.0.1:8093/app/index.html');await p.waitForTimeout(3000);
const cmd=async c=>{await p.keyboard.press(c);await p.waitForTimeout(700);};
const st=()=>p.evaluate(()=>({cls:document.getElementById('stage').className,html:document.documentElement.className,bodyBg:getComputedStyle(document.body).backgroundColor,home:getComputedStyle(document.getElementById('home')).transform,bg:getComputedStyle(document.getElementById('bg')).opacity,f:(document.querySelector('.focused')||{}).textContent,log:LOG.slice()}));
// Retour sur l'accueil -> direct
await cmd('Escape'); let s=await st();
ok('Retour sur accueil -> mode direct', /\blive\b/.test(s.cls)&&!/peek/.test(s.cls), s.cls);
ok('source = HDMI 1 (même si débranché, pas HDMI 2)', s.log.includes('setSource HDMI1'), s.log.join(' / '));
ok('vidéo plein écran DERRIÈRE la page', s.log.some(l=>/^show 0px,0px,1920px,1080px MAIN BEHIND$/.test(l)));
ok('page transparente', /live/.test(s.html) && s.bodyBg==='rgba(0, 0, 0, 0)', s.bodyBg);
ok('menu descendu + fond photo masqué', /1080/.test(s.home) && s.bg==='0', s.home+' bg='+s.bg);
await cmd('ArrowRight'); s=await st();
ok('1re touche -> menu remonte par-dessus HDMI (sans bouger la sélection)', /peek/.test(s.cls) && s.home==='none' && s.bg==='0' && /Netflix/.test(s.f), s.cls+' focus='+s.f);
await cmd('Escape'); s=await st(); ok('Retour -> menu redescend', /live/.test(s.cls)&&!/peek/.test(s.cls));
await cmd('ArrowDown'); await cmd('ArrowRight'); s=await st(); ok('navigation dans le menu en direct', /peek/.test(s.cls) && /YouTube/.test(s.f), s.f);
await cmd('Enter'); s=await st(); ok('lancer une appli -> vidéo cachée puis lancement', s.log.slice(-2).join(' / ')==='hide / launch 111299001912' && !/live/.test(s.cls), s.log.slice(-2).join(' / '));
ok('fond photo revenu', s.bg==='1');
// veille auto après inactivité (horloge truquée)
await p.evaluate(()=>{const n=Date.now;Date.now=()=>n()+200000;}); await p.waitForTimeout(1500); s=await st();
ok('inactivité 3 min -> direct (pas l\'horloge)', /live/.test(s.cls), s.cls);
ok('aucune erreur JS', errs.length===0, errs.join(' / '));
console.log(R.join('\n'));await b.close();web.close();process.exit(R.some(r=>r.startsWith('FAIL'))?1:0);})();
