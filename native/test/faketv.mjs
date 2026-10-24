// fausse TV : port sdb qui s'ouvre/se ferme, répond à was_execute ; "OpenTV" (port status) démarre après lancement
import net from "net";
const u32=n=>{const b=Buffer.alloc(4);b.writeUInt32LE(n>>>0,0);return b;};const sum=b=>{let s=0;for(const c of b)s=(s+c)>>>0;return s;};
const msg=(cmd,a0,a1,p=Buffer.alloc(0))=>{const c=Buffer.from(cmd),m=Buffer.alloc(4);for(let i=0;i<4;i++)m[i]=c[i]^0xff;return Buffer.concat([c,u32(a0),u32(a1),u32(p.length),u32(sum(p)),m,p]);};
let sdb=null, status=null, launches=0;
function tvOn(){ sdb=net.createServer(s=>{let buf=Buffer.alloc(0);s.on('data',d=>{buf=Buffer.concat([buf,d]);while(buf.length>=24){const c=buf.slice(0,4).toString(),len=buf.readUInt32LE(12);if(buf.length<24+len)break;const p=buf.slice(24,24+len);buf=buf.slice(24+len);
 if(c==='CNXN')s.write(msg('CNXN',0x01000000,4096,Buffer.from('device::FAKE::0')));
 if(c==='OPEN'){launches++;console.log('[faketv] commande recue:',p.toString().replace('\0',''));s.write(msg('OKAY',9,1));s.write(msg('WRTE',9,1,Buffer.from('app_id[OpnTV00001] launched\n')));s.write(msg('CLSE',9,1));
   if(!status) status=net.createServer(x=>x.end()).listen(18096);}}});s.on('error',()=>{});}).listen(36101); console.log('[faketv] ALLUMEE'); }
function tvOff(){ sdb&&sdb.close(); sdb=null; status&&status.close(); status=null; console.log('[faketv] ETEINTE'); }
tvOn(); setTimeout(tvOff, 14000); setTimeout(tvOn, 20000); setTimeout(()=>{console.log('[faketv] total lancements:',launches);process.exit(0);}, 34000);
