import net from "net";
const HOST = process.argv[2] || "192.168.2.14", PORT = 26101;
const CMDS = process.argv.slice(3);
const u32 = n => { const b = Buffer.alloc(4); b.writeUInt32LE(n >>> 0, 0); return b; };
const sum = buf => { let s = 0; for (const c of buf) s = (s + c) >>> 0; return s; };
function msg(cmd, a0, a1, payload = Buffer.alloc(0)) {
  const c = Buffer.from(cmd, "ascii");
  const magic = Buffer.alloc(4); for (let i=0;i<4;i++) magic[i] = c[i]^0xff;
  return Buffer.concat([c, u32(a0), u32(a1), u32(payload.length), u32(sum(payload)), magic, payload]);
}
const s = net.connect(PORT, HOST);
let buf = Buffer.alloc(0), localId = 1, queue = [...CMDS], out = [], curCmd = null, acc = "";
const done = (code=0) => { try{s.destroy();}catch{}; process.stdout.write(out.join("\n")+"\n"); process.exit(code); };
setTimeout(()=>{ out.push("[timeout]"); done(2); }, 25000);
s.on("connect", () => s.write(msg("CNXN", 0x01000000, 0x1000, Buffer.from("host::\0"))));
s.on("error", e => { out.push("[err] "+e.code); done(1); });
function openNext() {
  if (!queue.length) return done(0);
  curCmd = queue.shift(); acc = "";
  const svc = Buffer.from("shell:"+curCmd+"\0");
  s.write(msg("OPEN", localId, 0, svc));
}
s.on("data", d => {
  buf = Buffer.concat([buf, d]);
  while (buf.length >= 24) {
    const cmd = buf.slice(0,4).toString("ascii");
    const a0 = buf.readUInt32LE(4), a1 = buf.readUInt32LE(8), len = buf.readUInt32LE(12);
    if (buf.length < 24+len) break;
    const payload = buf.slice(24, 24+len); buf = buf.slice(24+len);
    if (cmd === "CNXN") { out.push("=== BANNER ==="); out.push(payload.toString("latin1")); out.push(""); openNext(); }
    else if (cmd === "OKAY") { /* stream ready */ }
    else if (cmd === "WRTE") { acc += payload.toString("latin1"); s.write(msg("OKAY", a1, a0)); }
    else if (cmd === "CLSE") {
      if (curCmd !== null) { out.push("### $ "+curCmd); out.push(acc.replace(/\s+$/,"")); out.push(""); }
      s.write(msg("CLSE", a1, a0)); localId++; openNext();
    }
  }
});
