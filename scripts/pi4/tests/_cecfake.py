"""Faux noyau CEC (tests seulement) : modèle borné de drivers/media/cec/core (cec-adap.c / cec-api.c, v6.6–v6.12)
utilisé derrière le VRAI transport ioctl de opentv_pi.cecfollower (structures empaquetées, numéros d'ioctl, poll).

Fidèle aux règles qui comptent pour le suiveur :
- descripteurs ouverts en mode INITIATOR (0x01) ; un seul EXCL_FOLLOWER (EBUSY sinon) ; événement initial
  STATE_CHANGE (FL_INITIAL_STATE) à l'ouverture ; G_LOG_ADDRS rend des adresses logiques invalides si non configuré ;
- réception : rien sans masque ; adressage/longueur (cec_msg_size) ; opcodes « cœur » (9f ff 46 8c a5 83) répondus
  par le noyau lui-même hors passthrough et jamais remis au suiveur ; <Give Device Power Status>/… depuis
  l'adresse 15 ignorés ; UCP/UCR transformés en touches (ALLOW_RC_PASSTHRU) puis remis au suiveur sans abandon ;
  tout autre message dirigé non traité ⇒ FEATURE_ABORT automatique du noyau SAUF si un suiveur existe ;
- émission : EPERM sans configuration, ENONET si non configuré, EINVAL si l'initiateur n'est pas à nous ; résultat
  rendu au descripteur émetteur par CEC_RECEIVE (séquence, tx_ts) ; jamais renvoyé aux suiveurs ;
- HPD bas/haut : STATE_CHANGE (PA invalide, masque 0) puis (PA, 0) et (PA, masque) après la réclamation, suivie de
  <Report Physical Address> diffusé par le noyau ; retrait du périphérique : ENODEV, POLLERR|POLLHUP.
Horloge simulée : poll/sleep avancent le temps jusqu'au prochain événement scripté (aucune attente réelle).
"""
import collections
import errno
import select
import struct

from opentv_pi import cecfollower as cf

CORE = {0x9F, 0xFF, 0x46, 0x8C, 0xA5, 0x83}
BCAST, DIRECTED, BOTH = 1, 2, 3
# sous-ensemble de cec_msg_size[] (longueur minimale, direction)
MSG_SIZE = {0x82: (4, BCAST), 0x04: (2, DIRECTED), 0x0D: (2, DIRECTED), 0x9D: (4, DIRECTED), 0x85: (2, BCAST),
            0x80: (6, BCAST), 0x81: (4, BCAST), 0x86: (4, BCAST), 0x36: (2, BOTH), 0x9F: (2, DIRECTED),
            0x83: (2, DIRECTED), 0x91: (2, DIRECTED), 0x84: (5, BCAST), 0x32: (5, BCAST), 0xA6: (6, BCAST),
            0xA5: (2, DIRECTED), 0x87: (5, BCAST), 0x8C: (2, DIRECTED), 0x89: (2, DIRECTED), 0xA0: (5, BOTH),
            0x46: (2, DIRECTED), 0x47: (2, DIRECTED), 0x8D: (3, DIRECTED), 0x8E: (3, DIRECTED),
            0x44: (3, DIRECTED), 0x45: (2, DIRECTED), 0x8F: (2, DIRECTED), 0x90: (3, DIRECTED), 0x00: (4, DIRECTED),
            0xFF: (2, DIRECTED), 0x1A: (3, DIRECTED), 0xA7: (4, BCAST), 0xA8: (6, BCAST)}


class Clock:
    def __init__(self, start_s=1000.0):
        self.ns = int(start_s * 1e9)

    def __call__(self):
        return self.ns

    @property
    def s(self):
        return self.ns / 1e9


class Adap:
    def __init__(self, path, pa, las, configured, ours, driver, caps, rc_passthru, osd, prim):
        self.path, self.pa, self.las = path, pa, tuple(las)
        self.configured = configured and pa != 0xFFFF
        self.num_log_addrs = len(las) if ours or osd else 0
        self.osd = osd if osd is not None else ("OpenTV" if ours else "")
        self.prim = prim if prim is not None else (4 if ours else 0)
        self.driver, self.caps, self.rc_passthru = driver, caps, rc_passthru
        self.removed = False
        self.leak_core = False          # anomalie simulée : opcodes cœur remis au suiveur (passthrough)

    @property
    def mask(self):
        return sum(1 << a for a in self.las) if self.configured else 0


class FH:
    def __init__(self, adap, fd):
        self.adap, self.fd = adap, fd
        self.mode = 0x01
        self.msgs = collections.deque()
        self.events = collections.deque()
        self.closed = False


class FakeKernel:
    def __init__(self, clock=None, pa=0x1000, las=(4,), configured=True, ours=True, driver="vc4_hdmi",
                 caps=0x7E, rc_passthru=True, path="/dev/cec0", osd=None, prim=None):
        self.clock = clock or Clock()
        self.adaps = {}
        self.add_adapter(path, pa=pa, las=las, configured=configured, ours=ours, driver=driver, caps=caps,
                         rc_passthru=rc_passthru, osd=osd, prim=prim)
        self.fhs = {}
        self.next_fd = 100
        self.seq = 0
        self.bus = []            # (t_ns, origin, frame) : tout ce qui passe sur le fil
        self.rc = []             # (t_ns, "down"/"up", code) : touches remises à l'entrée Linux
        self.script = []         # [(t_ns, action)] trié
        self.opens = []          # (t_ns, path)
        self.scans = []          # t_ns de chaque énumération de /dev/cecN
        self.ioctls = []         # noms
        self.nack = set()        # destinations absentes (émission non acquittée)
        self.tx_delay_ns = int(0.03e9)
        self.drop_tx_results = False   # résultats d'émission perdus (file de réception pleine, LOST_MSGS)

    # ---------------------------------------------------------------- topologie
    def add_adapter(self, path, pa=0x1000, las=(4,), configured=True, ours=True, driver="vc4_hdmi", caps=0x7E,
                    rc_passthru=True, osd=None, prim=None):
        self.adaps[path] = Adap(path, pa, las, configured, ours, driver, caps, rc_passthru, osd, prim)
        return self.adaps[path]

    def adap(self, path="/dev/cec0"):
        return self.adaps[path]

    def devices(self):
        self.scans.append(self.clock.ns)
        return sorted(p for p, a in self.adaps.items() if not a.removed)

    def at(self, t_s, action):
        self.script.append((int(t_s * 1e9), action))
        self.script.sort(key=lambda x: x[0])

    def _run_due(self, until_ns):
        while self.script and self.script[0][0] <= until_ns:
            t, act = self.script.pop(0)
            self.clock.ns = max(self.clock.ns, t)
            act()

    def advance(self, seconds):
        end = self.clock.ns + int(seconds * 1e9)
        self._run_due(end)
        self.clock.ns = max(self.clock.ns, end)

    # ---------------------------------------------------------------- descripteurs
    def open(self, path):
        a = self.adaps.get(path)
        self.opens.append((self.clock.ns, path))
        if a is None or a.removed:
            raise FileNotFoundError(errno.ENOENT, path)
        fd = self.next_fd
        self.next_fd += 1
        fh = FH(a, fd)
        self.fhs[fd] = fh
        fh.events.append((cf.CEC_EVENT_STATE_CHANGE, cf.CEC_EVENT_FL_INITIAL_STATE, a.pa, a.mask, 0))
        return fd

    def close(self, fd):
        fh = self.fhs.get(fd)
        if fh is not None:
            fh.closed = True
            del self.fhs[fd]

    def open_fds(self, path="/dev/cec0"):
        return [fd for fd, fh in self.fhs.items() if fh.adap.path == path]

    def excl(self, a):
        return next((fh for fh in self.fhs.values() if fh.adap is a and fh.mode & 0xF0 in (0x20, 0x30)), None)

    def followers(self, a):
        return [fh for fh in self.fhs.values() if fh.adap is a and fh.mode & 0xF0 == 0x10]

    # ---------------------------------------------------------------- ioctl (même signature que fcntl.ioctl)
    def ioctl(self, fd, req, buf, mutate=True):
        name = {v: k for k, v in cf.IOCTL.items()}.get(req)
        self.ioctls.append(name or "0x%08x" % req)
        fh = self.fhs.get(fd)
        if fh is None:
            raise OSError(errno.EBADF, "bad fd")
        a = fh.adap
        if a.removed:
            raise OSError(errno.ENODEV, "gone")
        if name == "CEC_ADAP_G_CAPS":
            struct.pack_into(cf.CAPS_FMT, buf, 0, a.driver.encode(), (a.driver.replace("_", "-") + "-0").encode(),
                             1, a.caps, 0x060c00)
        elif name == "CEC_ADAP_G_PHYS_ADDR":
            struct.pack_into("=H", buf, 0, a.pa)
        elif name == "CEC_ADAP_G_LOG_ADDRS":
            la = bytes([x if a.configured else 0xFF for x in a.las] + [0xFF] * (4 - len(a.las)))
            prim = bytes([a.prim] * a.num_log_addrs + [0] * (4 - a.num_log_addrs))
            ltype = bytes([3] * a.num_log_addrs + [0] * (4 - a.num_log_addrs))
            struct.pack_into(cf.LA_FMT, buf, 0, la, a.mask, 5, a.num_log_addrs, 0xFFFFFF,
                             2 if a.rc_passthru else 0, a.osd.encode()[:14], prim, ltype, prim, b"\0" * 48)
        elif name == "CEC_G_MODE":
            struct.pack_into("=I", buf, 0, fh.mode)
        elif name == "CEC_S_MODE":
            mode = struct.unpack_from("=I", buf, 0)[0]
            if mode & 0xF0 in (0x20, 0x30):
                other = self.excl(a)
                if other is not None and other is not fh:
                    raise OSError(errno.EBUSY, "busy")
            fh.mode = mode
        elif name == "CEC_RECEIVE":
            if not fh.msgs:
                raise OSError(errno.EAGAIN, "empty")
            m = fh.msgs.popleft()
            frame = m["frame"]
            struct.pack_into(cf.MSG_FMT, buf, 0, m.get("tx_ts", 0), m.get("rx_ts", 0), len(frame), 0,
                             m.get("sequence", 0), 0, bytes(frame).ljust(16, b"\0"), 0, m.get("rx_status", 0),
                             m.get("tx_status", 0), 0, m.get("nack", 0), 0, 0)
        elif name == "CEC_DQEVENT":
            if not fh.events:
                raise OSError(errno.EAGAIN, "empty")
            ev, flags, pa, mask, lost = fh.events.popleft()
            raw = struct.pack("=HHH", pa, mask, 0) if ev == cf.CEC_EVENT_STATE_CHANGE else struct.pack("=I", lost)
            struct.pack_into(cf.EVENT_FMT, buf, 0, self.clock.ns, ev, flags, raw.ljust(64, b"\0"))
        elif name == "CEC_TRANSMIT":
            return self._transmit(fh, buf)
        else:
            raise OSError(errno.ENOTTY, "not supported by the fake (never a setter)")
        return 0

    def _transmit(self, fh, buf):
        f = struct.unpack_from(cf.MSG_FMT, buf, 0)
        n = f[2]
        frame = bytes(f[6][:n])
        a = fh.adap
        if a.num_log_addrs == 0:
            raise OSError(errno.EPERM, "unconfigured")
        if not a.configured:
            raise OSError(getattr(errno, "ENONET", 64), "not configured")
        if fh.mode & 0x0F == 0:
            raise OSError(errno.EBUSY, "no initiator")
        if not 1 <= n <= 16 or (frame[0] >> 4) not in a.las:
            raise OSError(errno.EINVAL, "bad initiator")
        self.seq += 1
        t = self.clock.ns + self.tx_delay_ns
        self.bus.append((t, "follower", frame))
        dest = frame[0] & 0xF
        ok = dest == 15 or dest not in self.nack
        if not self.drop_tx_results:
            fh.msgs.append({"frame": frame, "sequence": self.seq, "tx_ts": t, "tx_status": 0x01 if ok else 0x04,
                            "nack": 0 if ok else 1})
        struct.pack_into("=I", buf, 24, self.seq)
        return 0

    # ---------------------------------------------------------------- poll
    def readiness(self, fd):
        fh = self.fhs.get(fd)
        if fh is None:
            return select.POLLNVAL
        if fh.adap.removed:
            return select.POLLERR | select.POLLHUP
        return (select.POLLIN if fh.msgs else 0) | (select.POLLPRI if fh.events else 0)

    def poll(self, fds, timeout_ms):
        deadline = self.clock.ns + int(max(0, timeout_ms) * 1e6)
        while True:
            ready = [(fd, self.readiness(fd)) for fd in fds if self.readiness(fd)]
            if ready:
                return ready
            if self.script and self.script[0][0] <= deadline:
                self._run_due(self.script[0][0])
                continue
            self.clock.ns = max(self.clock.ns, deadline)
            return []

    def poller(self):
        k = self

        class P:
            def __init__(self):
                self.fds = []

            def register(self, fd, _mask):
                self.fds.append(fd)

            def poll(self, timeout_ms):
                return k.poll(self.fds, timeout_ms)
        return P()

    def sleep(self, s):
        self.advance(s)

    # ---------------------------------------------------------------- bus : la TV et les autres appareils
    def send(self, frame, path="/dev/cec0", age_s=0.0):
        """Un appareil du bus émet `frame` : routage du noyau vers l'adaptateur `path`."""
        frame = bytes(frame)
        a = self.adaps[path]
        self.bus.append((self.clock.ns, "remote", frame))
        if a.removed or not a.configured or len(frame) < 2:
            return
        init, dest, op = frame[0] >> 4, frame[0] & 0xF, frame[1]
        if init in a.las:
            return                                       # notre propre émission : filtrée
        bcast = dest == 15
        if not bcast and dest not in a.las:
            return
        if op in MSG_SIZE:
            mn, d = MSG_SIZE[op]
            if len(frame) < mn or (bcast and not d & BCAST) or (not bcast and not d & DIRECTED):
                return
        from_unreg = init == 15
        rx = {"frame": frame, "rx_ts": self.clock.ns - int(age_s * 1e9), "rx_status": 0x01}
        excl = self.excl(a)
        passthru = excl is not None and excl.mode & 0xF0 == 0x30
        if op in (0x9F, 0xFF, 0x8F, 0x46) and from_unreg and not passthru:
            return
        if op in CORE and not passthru and not a.leak_core:
            if not bcast:
                self.bus.append((self.clock.ns, "kernel", bytes([(dest << 4) | init, 0x9E if op == 0x9F else 0x00])))
            return
        if op in (0x44, 0x45):
            if bcast or from_unreg:
                self._deliver(a, rx)
                return
            if a.rc_passthru and a.caps & cf.CEC_CAP_RC:
                self.rc.append((self.clock.ns, "down" if op == 0x44 else "up", frame[2] if len(frame) > 2 else None))
        elif op == 0x84:
            pass
        elif not bcast and excl is None and not self.followers(a) and op != 0x00:
            self.bus.append((self.clock.ns, "kernel", bytes([(dest << 4) | init, 0x00, op, 0x00])))
            return
        self._deliver(a, rx)

    def _deliver(self, a, rx):
        excl = self.excl(a)
        for fh in ([excl] if excl is not None else self.followers(a)):
            fh.msgs.append(dict(rx))

    def _event(self, a, pa, mask, flags=0):
        for fh in self.fhs.values():
            if fh.adap is a:
                fh.events.append((cf.CEC_EVENT_STATE_CHANGE, flags, pa, mask, 0))

    def hpd_low(self, path="/dev/cec0"):
        """TV éteinte / HDMI débranché : adresse physique invalide, adaptateur déconfiguré."""
        a = self.adaps[path]
        a.pa, a.configured = 0xFFFF, False
        self._event(a, 0xFFFF, 0)

    def hpd_high(self, pa=0x1000, path="/dev/cec0", claim_s=0.15):
        """TV rallumée : nouvelle adresse physique, puis réclamation automatique des adresses logiques par le noyau."""
        a = self.adaps[path]
        a.pa = pa
        self._event(a, pa, 0)

        def claimed():
            a.configured = True
            self._event(a, a.pa, a.mask)
            self.bus.append((self.clock.ns, "kernel", bytes([(a.las[0] << 4) | 15, 0x84, pa >> 8, pa & 0xFF, 4])))
        self.at(self.clock.s + claim_s, claimed)

    def lost(self, n=3, path="/dev/cec0"):
        a = self.adaps[path]
        for fh in self.fhs.values():
            if fh.adap is a:
                fh.events.append((cf.CEC_EVENT_LOST_MSGS, 0, 0, 0, n))

    def remove(self, path="/dev/cec0"):
        self.adaps[path].removed = True

    def restore(self, path="/dev/cec0"):
        self.adaps[path].removed = False

    # ---------------------------------------------------------------- lecture des résultats
    def ours(self):
        return [(t, f) for t, o, f in self.bus if o == "follower"]

    def kernel_aborts(self, op=None):
        return [(t, f) for t, o, f in self.bus if o == "kernel" and len(f) == 4 and f[1] == 0x00 and
                (op is None or f[2] == op)]
