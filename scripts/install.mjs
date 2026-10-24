import net from "net"; import fs from "fs";
const [HOST, FILE, REMOTE, PKG] = process.argv.slice(2);
const u32 = n => { const b=Buffer.alloc(4); b.writeUInt32LE(n>>>0,0); return b; };
const sum = b => { let s=0; for (const c of b) s=(s+c)>>>0; return s; };
const msg=(cmd,a0,a1,p=Buffer.alloc(0))=>{const c=Buffer.from(cmd,"ascii");const m=Buffer.alloc(4);for(let i=0;i<4;i++)m[i]=c[i]^0xff;return Buffer.concat([c,u32(a0),u32(a1),u32(p.length),u32(sum(p)),m,p]);};
const log = (...a) => console.log(...a);
const s = net.connect(26101, HOST);
let buf = Buffer.alloc(0), maxdata = 4096, phase = "cnxn", remote = 0, local = 1;
let out = [], sendq = [], waitingOkay = false, syncReply = Buffer.alloc(0), shellOut = "";
setTimeout(() => { log("[timeout] phase=" + phase); process.exit(2); }, 170000);
s.on("error", e => { log("[err]", e.code); process.exit(1); });
s.on("connect", () => s.write(msg("CNXN", 0x01000000, 0x40000, Buffer.from("host::\0"))));
function pump() { if (waitingOkay || !sendq.length) return; waitingOkay = true; s.write(msg("WRTE", local, remote, sendq.shift())); }
function startSync() {
  phase = "sync-open"; s.write(msg("OPEN", local, 0, Buffer.from("sync:\0")));
}
function queueFile() {
  const data = fs.readFileSync(FILE);
  const spec = Buffer.from(REMOTE + ",33188");
  let stream = [Buffer.concat([Buffer.from("SEND"), u32(spec.length), spec])];
  for (let o = 0; o < data.length; o += 65536) { const ch = data.slice(o, o + 65536); stream.push(Buffer.concat([Buffer.from("DATA"), u32(ch.length), ch])); }
  stream.push(Buffer.concat([Buffer.from("DONE"), u32(Math.floor(Date.now()/1000))]));
  const all = Buffer.concat(stream);
  for (let o = 0; o < all.length; o += maxdata) sendq.push(all.slice(o, o + maxdata));
  log("push", data.length, "octets en", sendq.length, "paquets (maxdata " + maxdata + ")");
  phase = "sync-send"; pump();
}
function startShell() {
  local++; phase = "shell"; shellOut = "";
  s.write(msg("OPEN", local, 0, Buffer.from("shell:0 vd_appinstall " + PKG + " " + REMOTE + "\0")));
}
s.on("data", d => {
  buf = Buffer.concat([buf, d]);
  while (buf.length >= 24) {
    const cmd = buf.slice(0,4).toString("ascii"), a0 = buf.readUInt32LE(4), a1 = buf.readUInt32LE(8), len = buf.readUInt32LE(12);
    if (buf.length < 24 + len) break;
    const p = buf.slice(24, 24 + len); buf = buf.slice(24 + len);
    if (cmd === "CNXN") { maxdata = Math.min(a1 || 4096, 65536); log("BANNER:", p.toString("latin1").trim(), "maxdata=" + a1); startSync(); }
    else if (cmd === "OKAY") {
      if (phase === "sync-open") { remote = a0; queueFile(); }
      else if (phase === "sync-send") { waitingOkay = false; if (sendq.length) pump(); else phase = "sync-wait"; }
      else if (phase === "shell") { remote = a0; }
    }
    else if (cmd === "WRTE") {
      s.write(msg("OKAY", a1, a0));
      if (phase.startsWith("sync")) {
        syncReply = Buffer.concat([syncReply, p]);
        if (syncReply.length >= 8) {
          const id = syncReply.slice(0,4).toString("ascii");
          if (id === "OKAY") { log("PUSH OK ->", REMOTE); s.write(msg("WRTE", local, remote, Buffer.concat([Buffer.from("QUIT"), u32(0)]))); s.write(msg("CLSE", local, remote)); startShell(); }
          else { log("PUSH FAIL:", syncReply.slice(8).toString("latin1")); process.exit(3); }
        }
      } else { shellOut += p.toString("latin1"); process.stdout.write(p.toString("latin1")); }
    }
    else if (cmd === "CLSE") {
      if (phase === "shell") { log("\n[install terminee]"); process.exit(0); }
    }
  }
});
