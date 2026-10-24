// OpenTV autostart : lance OpenTV dès que la TV s'allume.
// À faire tourner en permanence sur l'appareil dont l'IP est réglée comme « Host PC IP » en mode développeur.
// Usage : node autostart.mjs [IP_TV] [ID_APP]
import net from "net";
const TV = process.argv[2] || "192.168.2.14";
const APP = process.argv[3] || "OpnTV00001.OpenTV";
const SDB_PORT = Number(process.env.SDB_PORT || 26101);
const STATUS_PORT = Number(process.env.STATUS_PORT || 8096);
const POLL_MS = Number(process.env.POLL_MS || 3000);
const BOOT_DELAY_MS = Number(process.env.BOOT_DELAY_MS || 6000);   // laisse le temps à la TV de finir son démarrage
const log = (...a) => console.log(new Date().toISOString(), ...a);

const u32 = n => { const b = Buffer.alloc(4); b.writeUInt32LE(n >>> 0, 0); return b; };
const sum = b => { let s = 0; for (const c of b) s = (s + c) >>> 0; return s; };
const msg = (cmd, a0, a1, p = Buffer.alloc(0)) => { const c = Buffer.from(cmd, "ascii"), m = Buffer.alloc(4); for (let i = 0; i < 4; i++) m[i] = c[i] ^ 0xff; return Buffer.concat([c, u32(a0), u32(a1), u32(p.length), u32(sum(p)), m, p]); };

function portOpen(port) {
  return new Promise(res => { const s = net.connect(port, TV); const t = setTimeout(() => { s.destroy(); res(false); }, 1500);
    s.on("connect", () => { clearTimeout(t); s.destroy(); res(true); }); s.on("error", () => { clearTimeout(t); res(false); }); });
}
function sdbShell(cmd) {   // ouvre une session sdb et exécute une commande « shell: »
  return new Promise(res => {
    const s = net.connect(SDB_PORT, TV); let buf = Buffer.alloc(0), out = "", done = false;
    const end = r => { if (done) return; done = true; try { s.destroy(); } catch {} res(r); };
    setTimeout(() => end({ ok: false, out: out || "timeout" }), 20000);
    s.on("error", e => end({ ok: false, out: e.code }));
    s.on("connect", () => s.write(msg("CNXN", 0x01000000, 0x1000, Buffer.from("host::\0"))));
    s.on("data", d => {
      buf = Buffer.concat([buf, d]);
      while (buf.length >= 24) {
        const c = buf.slice(0, 4).toString("ascii"), a0 = buf.readUInt32LE(4), a1 = buf.readUInt32LE(8), len = buf.readUInt32LE(12);
        if (buf.length < 24 + len) break;
        const p = buf.slice(24, 24 + len); buf = buf.slice(24 + len);
        if (c === "CNXN") s.write(msg("OPEN", 1, 0, Buffer.from("shell:" + cmd + "\0")));
        else if (c === "WRTE") { out += p.toString("latin1"); s.write(msg("OKAY", a1, a0)); }
        else if (c === "CLSE") end({ ok: /launched|cmd_ret:0/.test(out), out: out.trim() });
      }
    });
  });
}
function openTvRunning() {
  return new Promise(res => { const s = net.connect(STATUS_PORT, TV); const t = setTimeout(() => { s.destroy(); res(false); }, 1500);
    s.on("connect", () => { clearTimeout(t); s.destroy(); res(true); }); s.on("error", () => { clearTimeout(t); res(false); }); });
}

let wasOn = null, busy = false;
async function tick() {
  if (busy) return; busy = true;
  try {
    const on = await portOpen(SDB_PORT);
    if (on && wasOn !== true) {
      log("TV allumée");
      await new Promise(r => setTimeout(r, BOOT_DELAY_MS));
      for (let i = 1; i <= 3; i++) {
        if (await openTvRunning()) { log("OpenTV tourne déjà"); break; }
        const r = await sdbShell("0 was_execute " + APP);
        log("lancement OpenTV (essai " + i + "):", r.ok ? "OK" : "échec", r.out.replace(/\s+/g, " ").slice(0, 120));
        if (r.ok) break;
        await new Promise(r2 => setTimeout(r2, 4000));
      }
    } else if (!on && wasOn === true) log("TV éteinte");
    wasOn = on;
  } finally { busy = false; }
}
log("surveillance de la TV " + TV + " (lance " + APP + " à l'allumage)");
tick(); setInterval(tick, POLL_MS);
