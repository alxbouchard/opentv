"""Suiveur HDMI-CEC exclusif minimal (0.7.6, opentv-cec-follower.service, module cecfollower) : ABI ioctl, politique
pure (octets exacts), garde d'émission (liste blanche), transport, moteur sur faux noyau (TV éteinte/rallumée,
retrait, verrou, débit, fraîcheur, arrêt), configuration/CLI/santé, unité systemd, installation et documentation."""
import configparser
import contextlib
import errno
import io
import json
import os
import re
import select
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._common import PI4, ROOT
from tests._cecfake import Clock, FakeKernel
from opentv_pi import VERSION, cecboot, cecfollower as cf, cecreturn, cli, config, health, ratings

UNIT = "opentv-cec-follower.service"
INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
ID4 = cf.Identity(pa=0x1000, las=(4,), mask=1 << 4, primary=4, flags=2)
ID8 = cf.Identity(pa=0x2100, las=(8,), mask=1 << 8, primary=8, flags=2)
ID11 = cf.Identity(pa=0x3400, las=(11,), mask=1 << 11, primary=11, flags=2)
ALLOWED_TX_OPS = {0x00, 0x82, 0x90}


def offsets(fmt, n):
    """Décalage de chacun des n premiers champs d'un format struct « = » (sans remplissage implicite)."""
    codes = re.findall(r"(\d*)([a-zA-Z])", fmt[1:])
    out, pos = [], 0
    for count, code in codes:
        if code == "x":
            pos += int(count or 1)
            continue
        out.append(pos)
        pos += struct.calcsize("=" + (count or "") + code)
    return out[:n]


# --------------------------------------------------------------------------- ABI (uapi linux/cec.h)
class TestAbi(unittest.TestCase):
    def test_struct_sizes_and_offsets_match_uapi(self):
        # tailles/décalages prouvés par _Static_assert contre les en-têtes uapi v6.6 et v6.12 (aarch64, armhf, x86_64)
        self.assertEqual(cf.abi_errors(), [])
        self.assertEqual({k: struct.calcsize(v) for k, v in cf.FORMATS.items()},
                         {"cec_msg": 56, "cec_caps": 76, "cec_log_addrs": 92, "cec_event": 80})
        self.assertEqual(offsets(cf.MSG_FMT, 14), [0, 8, 16, 20, 24, 28, 32, 48, 49, 50, 51, 52, 53, 54])
        self.assertEqual(offsets(cf.CAPS_FMT, 5), [0, 32, 64, 68, 72])
        self.assertEqual(offsets(cf.LA_FMT, 11), [0, 4, 6, 7, 8, 12, 16, 31, 35, 39, 43])
        self.assertEqual(offsets(cf.EVENT_FMT, 4), [0, 8, 12, 16])

    def test_ioctl_numbers_exact_and_no_setter(self):
        self.assertEqual(cf.IOCTL, {"CEC_ADAP_G_CAPS": 0xC04C6100, "CEC_ADAP_G_PHYS_ADDR": 0x80026101,
                                    "CEC_ADAP_G_LOG_ADDRS": 0x805C6103, "CEC_TRANSMIT": 0xC0386105,
                                    "CEC_RECEIVE": 0xC0386106, "CEC_DQEVENT": 0xC0506107, "CEC_G_MODE": 0x80046108,
                                    "CEC_S_MODE": 0x40046109})
        # jamais de réclamation, d'adresse physique écrite ni de connecteur : S_LOG_ADDRS/S_PHYS_ADDR absents
        for bad in (0xC05C6104, 0x40026102):
            self.assertNotIn(bad, cf.IOCTL.values())
        self.assertFalse(hasattr(cf.Adapter, "set_log_addrs"))
        self.assertFalse(hasattr(cf.Adapter, "set_phys_addr"))

    def test_modes(self):
        self.assertEqual((cf.MODE_FOLLOWER, cf.MODE_PLAIN, cf.MODE_PASSTHRU), (0x21, 0x01, 0x31))

    def test_abi_errors_detect_drift(self):
        with mock.patch.object(cf, "MSG_FMT", "=QQIIII16sBBBBBBB"):
            self.assertTrue(cf.abi_errors())
        with mock.patch.dict(cf.IOCTL, {"CEC_TRANSMIT": 0xC0376105}):
            self.assertTrue(cf.abi_errors())
        with mock.patch.dict(cf.IOCTL, {"CEC_ADAP_S_LOG_ADDRS": 0xC05C6104}):
            self.assertTrue(cf.abi_errors())


# --------------------------------------------------------------------------- identité de l'adaptateur
def la_dict(las=(4,), mask=None, num=None, osd="OpenTV", prim=4, flags=2, configured=True):
    las = list(las)
    return {"log_addr": (las if configured else [0xFF] * len(las)) + [0xFF] * (4 - len(las)),
            "log_addr_mask": (sum(1 << a for a in las) if mask is None else mask) if configured else 0,
            "num_log_addrs": len(las) if num is None else num, "osd_name": osd,
            "primary_device_type": [prim] * len(las) + [0] * (4 - len(las)), "flags": flags}


CAPS = {"driver": "vc4_hdmi", "name": "vc4-hdmi-0", "capabilities": 0x7E, "available_log_addrs": 1, "version": 0}


class TestIdentity(unittest.TestCase):
    def test_ready_identity_is_dynamic(self):
        st, ident = cf.identity_from(CAPS, 0x1000, la_dict())
        self.assertEqual((st, ident), ("ready", cf.Identity(0x1000, (4,), 0x10, 4, 2)))
        st, ident = cf.identity_from(CAPS, 0x2100, la_dict((8,)))
        self.assertEqual((st, ident.pa, ident.las, ident.primary), ("ready", 0x2100, (8,), 8))

    def test_refusals(self):
        cases = [
            (dict(CAPS, driver="cec-gpio"), 0x1000, la_dict(), "not_vc4"),
            (dict(CAPS, capabilities=0x7E & ~cf.CEC_CAP_TRANSMIT), 0x1000, la_dict(), "no_transmit"),
            (CAPS, 0x1000, la_dict(osd="Kodi"), "not_ours"),
            (CAPS, 0x1000, la_dict(prim=0), "not_ours"),
            (CAPS, 0x1000, la_dict(num=0, osd=""), "not_ours"),
            (CAPS, 0xFFFF, la_dict(configured=False), "disconnected"),
            (CAPS, 0x0000, la_dict(), "bad_physical_address"),
            (CAPS, 0x1000, la_dict(configured=False), "unconfigured"),
            (CAPS, 0x1000, la_dict(mask=0x30), "bad_logical_address"),
            (CAPS, 0x1000, la_dict((0,)), "bad_logical_address"),
            (CAPS, 0x1000, la_dict((5,)), "bad_logical_address"),
        ]
        for caps, pa, la, want in cases:
            self.assertEqual(cf.identity_from(caps, pa, la), (want, None), (caps, pa, la))
        self.assertTrue(cf.config_ours(la_dict(configured=False)))       # TV éteinte : configuration conservée
        self.assertFalse(cf.config_ours(la_dict(osd="Kodi")))


# --------------------------------------------------------------------------- politique pure
class TestPolicy(unittest.TestCase):
    def test_power_status_reply_exact_bytes(self):
        a = cf.decide(bytes([0x04, 0x8F]), ID4)
        self.assertEqual((a.kind, a.frame), ("reply_power", bytes([0x40, 0x90, 0x00])))
        self.assertEqual(cf.decide(bytes([0x58, 0x8F]), ID8).frame, bytes([0x85, 0x90, 0x00]))
        self.assertEqual(cf.decide(bytes([0x0B, 0x8F]), ID11).frame, bytes([0xB0, 0x90, 0x00]))
        self.assertEqual(cf.decide(bytes([0x0B, 0x8F]), ID4).kind, "ignore")      # pas pour nous

    def test_set_stream_path_exact_gives_one_active_source(self):
        a = cf.decide(bytes([0x0F, 0x86, 0x10, 0x00]), ID4)
        self.assertEqual((a.kind, a.frame), ("active_source", bytes([0x4F, 0x82, 0x10, 0x00])))
        self.assertEqual(cf.decide(bytes([0x0F, 0x86, 0x21, 0x00]), ID8).frame, bytes([0x8F, 0x82, 0x21, 0x00]))
        self.assertEqual(cf.decide(bytes([0x0F, 0x86, 0x34, 0x00]), ID11).frame, bytes([0xBF, 0x82, 0x34, 0x00]))

    def test_set_stream_path_refusals(self):
        for frame, why in ((bytes([0x0F, 0x86, 0x20, 0x00]), "stream_path_other"),
                           (bytes([0x0F, 0x86, 0x00, 0x00]), "stream_path_other"),
                           (bytes([0x0F, 0x86, 0x10]), "stream_path_malformed"),
                           (bytes([0x0F, 0x86, 0x10, 0x00, 0x00]), "stream_path_malformed"),
                           (bytes([0x5F, 0x86, 0x10, 0x00]), "stream_path_not_from_tv"),
                           (bytes([0xFF, 0x86, 0x10, 0x00]), "stream_path_not_from_tv"),
                           (bytes([0x04, 0x86, 0x10, 0x00]), "broadcast_only_directed")):
            a = cf.decide(frame, ID4)
            self.assertEqual((a.kind, a.reason, a.frame), ("ignore", why, None), frame.hex())

    def test_broadcasts_never_answered_except_exact_stream_path(self):
        for op in range(256):
            for tail in (b"", b"\x10\x00", b"\x00\x00\x10\x00", b"\x10\x00\x04"):
                for init in (0, 5, 15):
                    frame = bytes([(init << 4) | 15, op]) + tail
                    a = cf.decide(frame, ID4)
                    if (op, init, tail) == (0x86, 0, b"\x10\x00"):
                        self.assertEqual(a.kind, "active_source")
                    else:
                        self.assertIsNone(a.frame, frame.hex())

    def test_unknown_directed_feature_abort_unrecognized(self):
        for op in (0x8D, 0x1A, 0x89, 0xA0, 0x36, 0x04, 0x0D, 0x90, 0x9E, 0x47, 0x91, 0x70, 0x9D):
            a = cf.decide(bytes([0x04, op, 0x01, 0x02, 0x03, 0x04]), ID4)
            self.assertEqual((a.kind, a.frame), ("feature_abort", bytes([0x40, 0x00, op, 0x00])), hex(op))

    def test_never_aborted_directed(self):
        for op in (0x00, 0x44, 0x45, 0x84, 0x82, 0x85, 0x80, 0x81, 0x86, 0x32, 0x87, 0xA6, 0xA7, 0xA8, 0xF8):
            a = cf.decide(bytes([0x04, op, 0x01, 0x02, 0x03, 0x04]), ID4)
            self.assertEqual((a.kind, a.frame), ("ignore", None), hex(op))
        # UCP/UCR : jamais d'abandon ni d'injection (le noyau les transforme en touches)
        self.assertEqual(cf.decide(bytes([0x04, 0x44, 0x01]), ID4).reason, "user_control")
        self.assertEqual(cf.decide(bytes([0x04, 0x45]), ID4).reason, "user_control")

    def test_kernel_core_opcodes_are_an_anomaly(self):
        for op in (0x9F, 0xFF, 0x46, 0x8C, 0xA5, 0x83):
            self.assertEqual(cf.decide(bytes([0x04, op]), ID4).kind, "anomaly", hex(op))
        self.assertEqual(cf.KERNEL_CORE_OPCODES, frozenset({0x9F, 0xFF, 0x46, 0x8C, 0xA5, 0x83}))

    def test_addressing_ignores(self):
        self.assertEqual(cf.decide(b"\x04", ID4).reason, "poll")
        self.assertEqual(cf.decide(b"", ID4).reason, "poll")
        self.assertEqual(cf.decide(bytes([0x08, 0x8F]), ID4).reason, "not_for_us")
        self.assertEqual(cf.decide(bytes([0x44, 0x8F]), ID4).reason, "from_self")
        self.assertEqual(cf.decide(bytes([0xF4, 0x8F]), ID4).reason, "from_unregistered")
        self.assertEqual(cf.decide(bytes([0xF4, 0x8D, 0x00]), ID4).reason, "from_unregistered")

    def test_exhaustive_sweep_only_whitelisted_output(self):
        """256 en-têtes × 256 opcodes × 6 longueurs × 3 identités : la politique n'émet que la liste blanche, et
        seulement pour les deux demandes prévues (8f dirigé vers nous, 86 exact de la TV) ou un abandon dirigé."""
        tails = (b"", b"\x10", b"\x10\x00", b"\x21\x00", b"\x34\x00", b"\x10\x00\x00\x00")
        counts = {"reply_power": 0, "active_source": 0, "feature_abort": 0}
        for ident in (ID4, ID8, ID11):
            for hdr in range(256):
                for op in range(256):
                    for tail in tails:
                        frame = bytes([hdr, op]) + tail
                        a = cf.decide(frame, ident)
                        if a.frame is None:
                            continue
                        counts[a.kind] += 1
                        self.assertEqual(cf.guard_tx(a.frame, ident), a.frame)
                        self.assertIn(a.frame[1], ALLOWED_TX_OPS)
                        init, dest = hdr >> 4, hdr & 15
                        if a.kind == "active_source":
                            self.assertEqual((init, dest, op, frame[2:]), (0, 15, 0x86, struct.pack(">H", ident.pa)))
                        elif a.kind == "reply_power":
                            self.assertEqual((op, dest in ident.las, init in ident.las or init == 15),
                                             (0x8F, True, False))
                        else:
                            self.assertTrue(dest in ident.las and init not in ident.las and init != 15)
                            self.assertNotIn(op, cf.NEVER_ABORT)
        self.assertEqual(counts["active_source"], 3)
        self.assertEqual(counts["reply_power"], 3 * 14 * 6)          # 14 initiateurs × 6 longueurs ≥ 2 octets


# --------------------------------------------------------------------------- garde d'émission (dernier rempart)
FORBIDDEN = [
    bytes([0x40]),                              # POLL
    bytes([0x40, 0x04]), bytes([0x40, 0x0D]),   # Image/Text View On
    bytes([0x40, 0x36]), bytes([0x4F, 0x36]),   # Standby
    bytes([0x40, 0x8E, 0x00]),                  # Menu Status
    bytes([0x40, 0x8F]),                        # Give Device Power Status (question d'alimentation)
    bytes([0x4F, 0x85]),                        # Request Active Source
    bytes([0x40, 0x44, 0x01]), bytes([0x40, 0x45]),        # touches
    bytes([0x40, 0x9D, 0x10, 0x00]),            # Inactive Source
    bytes([0x4F, 0x80, 0x00, 0x00, 0x10, 0x00]),           # Routing Change
    bytes([0x4F, 0x86, 0x10, 0x00]),            # Set Stream Path
    bytes([0x4F, 0x82, 0x20, 0x00]),            # Active Source, autre adresse physique
    bytes([0x40, 0x82, 0x10, 0x00]),            # Active Source dirigée
    bytes([0x4F, 0x82, 0x10]), bytes([0x4F, 0x82, 0x10, 0x00, 0x00]),
    bytes([0x40, 0x90, 0x01]), bytes([0x40, 0x90, 0x02]),  # power status ≠ on
    bytes([0x4F, 0x90, 0x00]),                  # power status diffusé
    bytes([0x40, 0x00, 0x8F, 0x00]),            # FEATURE_ABORT de 8f (exigence : jamais)
    bytes([0x40, 0x00, 0x44, 0x00]), bytes([0x40, 0x00, 0x45, 0x00]), bytes([0x40, 0x00, 0x00, 0x00]),
    bytes([0x40, 0x00, 0x86, 0x00]), bytes([0x40, 0x00, 0x84, 0x00]),
    bytes([0x40, 0x00, 0x9F, 0x00]), bytes([0x40, 0x00, 0x83, 0x00]),
    bytes([0x40, 0x00, 0x8D, 0x04]),            # raison ≠ unrecognized
    bytes([0x4F, 0x00, 0x8D, 0x00]),            # abandon diffusé
    bytes([0x80, 0x90, 0x00]), bytes([0x50, 0x00, 0x8D, 0x00]), bytes([0x8F, 0x82, 0x10, 0x00]),  # LA étrangère
    bytes([0x44, 0x90, 0x00]),                  # vers nous-mêmes
    bytes([0x40, 0x90, 0x00, 0x00]), b"", "40:90:00", bytearray(b"\x40\x90"),
]


class TestGuard(unittest.TestCase):
    def test_allowed(self):
        for f in (bytes([0x40, 0x90, 0x00]), bytes([0x45, 0x90, 0x00]), bytes([0x40, 0x00, 0x8D, 0x00]),
                  bytes([0x4F, 0x82, 0x10, 0x00])):
            self.assertEqual(cf.guard_tx(f, ID4), f)
        self.assertEqual(cf.guard_tx(bytearray([0x40, 0x90, 0x00]), ID4), bytes([0x40, 0x90, 0x00]))

    def test_forbidden(self):
        for f in FORBIDDEN:
            with self.assertRaises(cf.ForbiddenTx, msg=repr(f)):
                cf.guard_tx(f, ID4)

    def test_every_other_opcode_is_forbidden_at_every_length(self):
        """Mutants « tout opcode interdit » : quel que soit l'octet d'opcode hors liste blanche, à toute longueur,
        diffusé ou dirigé, la garde refuse."""
        for op in range(256):
            if op in ALLOWED_TX_OPS:
                continue
            for hdr in (0x40, 0x4F, 0x45):
                for n in range(0, 15):
                    with self.assertRaises(cf.ForbiddenTx):
                        cf.guard_tx(bytes([hdr, op]) + bytes(n), ID4)


# --------------------------------------------------------------------------- transport (faux ioctl enregistreur)
class RecIoctl:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda name, buf: None)

    def __call__(self, fd, req, buf, mutate=True):
        name = {v: k for k, v in cf.IOCTL.items()}[req]
        self.calls.append((name, bytes(buf)))
        self.handler(name, buf)
        return 0


class NullPoll:
    def __init__(self, res=()):
        self.res = list(res)

    def register(self, fd, mask):
        self.mask = mask

    def poll(self, ms):
        return self.res


def adapter(handler=None, res=()):
    rec = RecIoctl(handler)
    closed = []
    ad = cf.Adapter("/dev/cec0", ioctl_fn=rec, opener=lambda p: 42, closer=closed.append,
                    poll_factory=lambda: NullPoll(res))
    return ad, rec, closed


class TestTransport(unittest.TestCase):
    def test_transmit_layout(self):
        def h(name, buf):
            struct.pack_into("=I", buf, 24, 77)
        ad, rec, _ = adapter(h)
        self.assertEqual(ad.transmit(bytes([0x40, 0x90, 0x00])), 77)
        name, raw = rec.calls[-1]
        self.assertEqual((name, len(raw)), ("CEC_TRANSMIT", 56))
        self.assertEqual(struct.unpack_from("=I", raw, 16)[0], 3)            # len
        self.assertEqual(struct.unpack_from("=I", raw, 20)[0], 0)            # timeout : non bloquant
        self.assertEqual(struct.unpack_from("=I", raw, 28)[0], 0)            # flags : jamais RAW
        self.assertEqual(raw[32:35], bytes([0x40, 0x90, 0x00]))
        self.assertEqual(raw[48], 0)                                          # reply : aucune attente de réponse
        with self.assertRaises(cf.ForbiddenTx):
            ad.transmit(b"")
        with self.assertRaises(cf.ForbiddenTx):
            ad.transmit(bytes(17))

    def test_receive_parse_and_eagain(self):
        def h(name, buf):
            if name == "CEC_RECEIVE":
                struct.pack_into(cf.MSG_FMT, buf, 0, 5, 9, 2, 0, 3, 0, b"\x04\x8f".ljust(16, b"\0"), 0, 1, 0, 0, 0,
                                 0, 0)
        ad, _, _ = adapter(h)
        m = ad.receive()
        self.assertEqual((m.frame, m.rx_ts, m.tx_ts, m.sequence, m.rx_status), (b"\x04\x8f", 9, 5, 3, 1))
        self.assertFalse(m.is_tx_result)

        def eagain(name, buf):
            raise OSError(errno.EAGAIN, "x")
        ad, _, _ = adapter(eagain)
        self.assertIsNone(ad.receive())
        self.assertIsNone(ad.dqevent())

    def test_enodev_is_adapter_gone_and_poll_error(self):
        def gone(name, buf):
            raise OSError(errno.ENODEV, "x")
        ad, _, _ = adapter(gone)
        for call in (ad.receive, ad.dqevent, ad.phys_addr, ad.mode, lambda: ad.transmit(b"\x40\x90\x00")):
            with self.assertRaises(cf.AdapterGone):
                call()
        ad, _, _ = adapter(res=[(42, select.POLLERR)])
        with self.assertRaises(cf.AdapterGone):
            ad.wait(1.0)
        ad, _, _ = adapter(res=[(42, select.POLLIN | select.POLLPRI)])
        self.assertEqual(ad.wait(1.0), (True, True))

    def test_mode_only_follower_or_plain_never_passthrough(self):
        ad, rec, _ = adapter()
        ad.set_mode(cf.MODE_FOLLOWER)
        ad.set_mode(cf.MODE_PLAIN)
        self.assertEqual([struct.unpack("=I", raw)[0] for n, raw in rec.calls if n == "CEC_S_MODE"], [0x21, 0x01])
        for bad in (0x31, 0x11, 0x22, 0xE0, 0x02, 0x00):
            with self.assertRaises(cf.ForbiddenTx):
                ad.set_mode(bad)
        self.assertEqual(len([n for n, _ in rec.calls if n == "CEC_S_MODE"]), 2)

    def test_device_path_validated_and_closed_once(self):
        for bad in ("/dev/cec", "/dev/cec123", "/dev/../dev/cec0", "/tmp/cec0", "cec0", "/dev/cec0 "):
            with self.assertRaises(ValueError):
                cf.Adapter(bad, ioctl_fn=RecIoctl(), opener=lambda p: 1, closer=lambda fd: None,
                           poll_factory=NullPoll)
        ad, _, closed = adapter()
        ad.close()
        ad.close()
        self.assertEqual(closed, [42])

    def test_real_open_refuses_non_char_device(self):
        d = tempfile.mkdtemp(prefix="otvcecf")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "cec0")
        open(p, "w").close()
        with self.assertRaises(OSError):
            cf.Adapter.default_open(p)


# --------------------------------------------------------------------------- moteur sur faux noyau
class FakeLock:
    def __init__(self, k):
        self.k = k
        self.busy_until = None
        self.held = 0
        self.calls = 0

    def acquire(self):
        self.calls += 1
        if self.busy_until is not None and self.k.clock.s < self.busy_until:
            return None
        self.held += 1
        return 7

    def release(self, tok):
        self.held -= 1


def make(k, until, canary=False, installed=None, **kw):
    logs = []
    lock = FakeLock(k)
    f = cf.Follower(devices=k.devices,
                    open_adapter=lambda p: cf.Adapter(p, ioctl_fn=k.ioctl, opener=k.open, closer=k.close,
                                                      poll_factory=k.poller),
                    mono_ns=k.clock, sleep=k.sleep, lock=lock.acquire, unlock=lock.release, log=logs.append,
                    canary=canary, installed=installed or (lambda: True), stop=lambda: k.clock.s >= until, **kw)
    return f, logs, lock


def events(logs, name=None):
    return [e for e in logs if name is None or e["event"] == name]


class EngineBase(unittest.TestCase):
    def setUp(self):
        self.k = FakeKernel(Clock(1000.0))

    def run_until(self, until, **kw):
        f, logs, lock = make(self.k, until, **kw)
        code = f.serve()
        self.logs, self.lock, self.f = logs, lock, f
        return code

    def tv(self, t, frame, age_s=0.0):
        self.k.at(t, lambda: self.k.send(bytes(frame), age_s=age_s))

    def replies(self, op=None):
        return [(t, f) for t, f in self.k.ours() if op is None or f[1] == op]


class TestEngine(EngineBase):
    def test_kernel_baseline_aborts_8f_without_follower(self):
        # le faux noyau reproduit la panne : sans suiveur, <Give Device Power Status> reçoit FEATURE_ABORT
        self.k.send(bytes([0x04, 0x8F]))
        self.assertEqual([f for _, f in self.k.kernel_aborts(0x8F)], [bytes([0x40, 0x00, 0x8F, 0x00])])

    def test_power_status_answered_once_in_under_a_second_no_kernel_abort(self):
        for i in range(5):
            self.tv(1010 + 30 * i, [0x04, 0x8F])
        self.assertEqual(self.run_until(1200), 0)
        rp = self.replies(0x90)
        self.assertEqual([f for _, f in rp], [bytes([0x40, 0x90, 0x00])] * 5)
        for i, (t, _) in enumerate(rp):
            self.assertLess(t - int((1010 + 30 * i) * 1e9), int(1e9))
        self.assertEqual(self.k.kernel_aborts(0x8F), [])
        self.assertEqual(self.k.kernel_aborts(), [])
        self.assertEqual(len(self.k.ours()), 5)                    # rien d'autre émis
        self.assertEqual(events(self.logs, "cec_follower_ready")[0]["logical_address"], 4)

    def test_set_stream_path_exact_one_active_source(self):
        self.tv(1010, [0x0F, 0x86, 0x10, 0x00])
        self.tv(1020, [0x0F, 0x86, 0x20, 0x00])                    # autre entrée
        self.tv(1030, [0x0F, 0x86, 0x10, 0x00, 0x00])              # malformé
        self.tv(1040, [0x5F, 0x86, 0x10, 0x00])                    # pas la TV
        self.tv(1050, [0x0F, 0x86, 0x10, 0x00])
        self.tv(1050.5, [0x0F, 0x86, 0x10, 0x00])                  # doublon immédiat : une seule réponse
        self.run_until(1100)
        ours = self.k.ours()
        self.assertEqual([f for _, f in ours], [bytes([0x4F, 0x82, 0x10, 0x00])] * 2)
        self.assertLess(ours[0][0] - int(1010e9), int(1e9))
        self.assertLess(ours[1][0] - int(1050e9), int(1e9))
        rep = events(self.logs, "cec_follower_reply")
        self.assertEqual([e["result"] for e in rep], ["sent", "sent", "duplicate_request"])
        self.assertTrue(all(e["request"] == "set_stream_path" for e in rep))

    def test_unknown_directed_aborted_ucp_never_and_keys_still_mapped(self):
        self.tv(1010, [0x04, 0x8D, 0x01])            # Menu Request
        self.tv(1011, [0x04, 0x1A, 0x01])            # Give Deck Status
        self.tv(1012, [0x04, 0x44, 0x01])            # Haut
        self.tv(1012.1, [0x04, 0x45])
        self.tv(1013, [0x04, 0x00, 0x8F, 0x00])      # FEATURE_ABORT reçu : jamais abandonné
        self.tv(1014, [0x0F, 0x36])                  # Standby diffusé : rien
        self.tv(1015, [0x0F, 0x82, 0x00, 0x00])      # application native : rien
        self.tv(1016, [0x0F, 0x85])                  # Request Active Source : rien (diffusion)
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x00, 0x8D, 0x00]),
                                                          bytes([0x40, 0x00, 0x1A, 0x00])])
        self.assertEqual([(d, c) for _, d, c in self.k.rc], [("down", 0x01), ("up", None)])   # RC conservé
        self.assertEqual(self.k.kernel_aborts(), [])

    def test_no_periodic_emission_ever(self):
        """6 h simulées, trafic de diffusion seulement, extinction/rallumage de la TV : aucune émission, aucune
        annonce sur simple HPD, aucun POLL, aucune réclamation."""
        for i in range(0, 6 * 3600, 600):
            self.tv(1000 + i + 1, [0x0F, 0x87, 0x00, 0x00, 0xF0])          # Device Vendor ID diffusé
        self.k.at(5000, self.k.hpd_low)
        self.k.at(6000, lambda: self.k.hpd_high(0x1000))
        self.assertEqual(self.run_until(1000 + 6 * 3600), 0)
        self.assertEqual(self.k.ours(), [])
        self.assertNotIn("CEC_ADAP_S_LOG_ADDRS", self.k.ioctls)
        self.assertNotIn("CEC_TRANSMIT", self.k.ioctls)

    def test_tv_off_on_three_cycles_ready_immediately_same_fd(self):
        t = 1010.0
        for _ in range(3):
            self.k.at(t, self.k.hpd_low)
            self.tv(t + 1, [0x04, 0x8F])                 # TV éteinte : adaptateur déconfiguré, rien reçu
            self.k.at(t + 300, lambda: self.k.hpd_high(0x1000, claim_s=0.15))
            # la TV demande notre chemin 20 ms après la réclamation : la fenêtre de retour n'est jamais manquée
            self.tv(t + 300.17, [0x0F, 0x86, 0x10, 0x00])
            self.tv(t + 300.5, [0x04, 0x8F])
            t += 600
        self.run_until(t)
        ours = self.k.ours()
        self.assertEqual([f for _, f in ours], [bytes([0x4F, 0x82, 0x10, 0x00]), bytes([0x40, 0x90, 0x00])] * 3)
        for i in range(3):
            self.assertLess(ours[2 * i][0] - int((1010 + 600 * i + 300.17) * 1e9), int(1e9))
        self.assertEqual(len(self.k.opens), 1)                   # même descripteur : pas de réouverture
        waits = [e["reason"] for e in events(self.logs, "cec_follower_waiting")]
        self.assertEqual(waits.count("disconnected"), 3)
        self.assertEqual(len(events(self.logs, "cec_follower_ready")), 4)

    def test_request_during_churn_is_never_answered(self):
        # changement d'état en file AVANT la demande : relecture, adresse changée ⇒ aucune réponse
        def churn_then_request():
            a = self.k.adap()
            a.pa = 0x2000
            self.k._event(a, 0x2000, a.mask)
            self.k.send(bytes([0x0F, 0x86, 0x10, 0x00]))
            self.k.send(bytes([0x04, 0x8F]))
        self.k.at(1010, churn_then_request)
        self.tv(1020, [0x0F, 0x86, 0x20, 0x00])                  # nouvelle adresse : exacte
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x90, 0x00]),
                                                          bytes([0x4F, 0x82, 0x20, 0x00])])

    def test_churn_without_event_detected_before_tx(self):
        # adresse physique changée sans événement (événements perdus) : relecture juste avant l'émission
        def silent_change():
            self.k.adap().pa = 0x2000
            self.k.send(bytes([0x0F, 0x86, 0x10, 0x00]))
        self.k.at(1010, silent_change)
        self.run_until(1100)
        self.assertEqual(self.k.ours(), [])
        self.assertIn("adapter_churn", [e["result"] for e in events(self.logs, "cec_follower_reply")])

    def test_stale_request_not_answered(self):
        self.tv(1010, [0x04, 0x8F], age_s=0.9)
        self.tv(1011, [0x0F, 0x86, 0x10, 0x00], age_s=2.0)
        self.tv(1012, [0x04, 0x8F], age_s=0.1)
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x90, 0x00])])
        self.assertEqual(events(self.logs, "cec_follower_reply")[0]["result"], "stale")

    def test_lock_busy_bounded_wait_then_skip(self):
        self.tv(1010, [0x04, 0x8F])
        self.tv(1020, [0x04, 0x8F])
        f, logs, lock = make(self.k, 1100)
        lock.busy_until = 1015.0
        f.serve()
        ours = self.k.ours()
        self.assertEqual(len(ours), 1)
        self.assertLess(ours[0][0] - int(1020e9), int(1e9))
        self.assertLessEqual(lock.calls, 2 + int(cf.LOCK_WAIT_S / cf.LOCK_POLL_S) + 2)
        self.assertEqual(lock.held, 0)
        # attente bornée : le passage suivant n'a pas été retardé au-delà de LOCK_WAIT_S
        self.assertEqual(f.counters["lock_busy"], 1)

    def test_lock_released_after_wait_is_used(self):
        self.tv(1010, [0x04, 0x8F])
        f, logs, lock = make(self.k, 1100)
        lock.busy_until = 1010.1
        f.serve()
        self.assertEqual(len(self.k.ours()), 1)
        self.assertLess(self.k.ours()[0][0] - int(1010e9), int(0.5e9))

    def test_request_gone_stale_during_lock_wait_not_answered(self):
        # fraîche à la réception (0,5 s) mais périmée après l'attente du verrou (0,35 s) : rien émis
        self.tv(1010, [0x04, 0x8F], age_s=0.5)
        f, logs, lock = make(self.k, 1100)
        lock.busy_until = 1010.35
        f.serve()
        self.assertEqual(self.k.ours(), [])
        self.assertEqual(f.counters["stale"], 1)

    def test_adapter_change_during_lock_wait_cancels_reply(self):
        # la TV change notre adresse pendant que le verrou est tenu : la demande pour l'ancienne adresse n'est pas
        # servie, la suivante (nouvelle adresse) l'est
        self.tv(1010, [0x0F, 0x86, 0x10, 0x00])

        def move():
            a = self.k.adap()
            a.pa = 0x2000
            self.k._event(a, 0x2000, a.mask)
        self.k.at(1010.1, move)
        self.tv(1020, [0x0F, 0x86, 0x20, 0x00])
        f, logs, lock = make(self.k, 1100)
        lock.busy_until = 1010.2
        f.serve()
        self.assertEqual([fr for _, fr in self.k.ours()], [bytes([0x4F, 0x82, 0x20, 0x00])])
        self.assertEqual([e["result"] for e in events(logs, "cec_follower_reply")], ["adapter_churn", "sent"])

    def test_rate_limit_bounds_bursts(self):
        for i in range(50):
            self.tv(1010 + i * 0.02, [0x04, 0x8D, 0x00])
        self.run_until(1100)
        self.assertLessEqual(len(self.k.ours()), cf.TX_RATE_MAX)
        self.assertTrue(events(self.logs, "cec_follower_rate_limited"))
        self.assertEqual(len(events(self.logs, "cec_follower_rate_limited")), 1)

    def test_active_source_hourly_cap(self):
        for i in range(cf.AS_PER_HOUR_MAX + 5):
            self.tv(1010 + i * 10, [0x0F, 0x86, 0x10, 0x00])
        self.run_until(1010 + 3000)
        self.assertEqual(len(self.k.ours()), cf.AS_PER_HOUR_MAX)

    def test_core_opcode_delivered_is_fatal_and_released(self):
        self.k.adap().leak_core = True
        self.tv(1010, [0x04, 0x9F])
        self.tv(1020, [0x04, 0x8F])
        self.assertEqual(self.run_until(1100), cf.EXIT_FATAL)
        self.assertEqual(self.k.ours(), [])
        self.assertEqual(self.k.open_fds(), [])
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["reason"], "mode_anomaly")
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["exit"], 78)

    def test_exclusive_follower_busy_never_stolen_then_fatal(self):
        other = self.k.open("/dev/cec0")
        self.k.fhs[other].mode = 0x21
        code = self.run_until(2000)
        self.assertEqual(code, cf.EXIT_FATAL)
        self.assertEqual(self.k.fhs[other].mode, 0x21)
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["reason"], "exclusive_follower_busy")
        self.assertEqual(self.k.open_fds(), [other])

    def test_brief_adapter_glitch_reopens_immediately(self):
        # descripteur perdu un instant (rebind du pilote) : réouverture en ~0,2 s, la demande suivante est servie
        self.k.at(1010, self.k.remove)
        self.k.at(1010.05, self.k.restore)
        self.tv(1011, [0x0F, 0x86, 0x10, 0x00])
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x4F, 0x82, 0x10, 0x00])])
        self.assertTrue(any(int(1010e9) < t <= int(1010.5e9) for t, _ in self.k.opens))

    def test_transient_exclusive_owner_tolerated_then_held(self):
        other = self.k.open("/dev/cec0")
        self.k.fhs[other].mode = 0x21
        self.k.at(1007, lambda: self.k.close(other))         # cec-follower de diagnostic arrêté
        self.tv(1030, [0x04, 0x8F])
        self.assertEqual(self.run_until(1100), 0)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x90, 0x00])])
        self.assertEqual(events(self.logs, "cec_follower_failed"), [])

    def test_mode_not_applied_fails_closed(self):
        orig = self.k.ioctl

        def ioctl(fd, req, buf, mutate=True):
            if req == cf.IOCTL["CEC_S_MODE"]:
                return 0                                      # mode silencieusement non appliqué
            return orig(fd, req, buf, mutate)
        self.k.ioctl = ioctl
        self.tv(1010, [0x04, 0x8F])
        self.assertEqual(self.run_until(1100), cf.EXIT_FATAL)
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["reason"], "mode_readback_mismatch")
        self.assertEqual(self.k.ours(), [])

    def test_policy_bug_caught_by_guard_at_transmit(self):
        # dernier rempart : même si la politique produisait une trame interdite, rien ne part et le service échoue
        self.tv(1010, [0x04, 0x8F])
        bad = cf.Action("reply_power", bytes([0x40, 0x04]), "mutant")
        with mock.patch.object(cf, "decide", lambda frame, ident: bad):
            self.assertEqual(self.run_until(1100), cf.EXIT_FATAL)
        self.assertEqual(self.k.ours(), [])
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["reason"], "forbidden_tx")
        self.assertEqual(self.k.open_fds(), [])

    def test_adapter_removed_reopens_bounded_without_spin(self):
        self.k.at(1010, self.k.remove)
        self.k.at(1070, self.k.restore)
        self.tv(1080, [0x04, 0x8F])
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x90, 0x00])])
        absent = [t for t in self.k.scans if int(1010e9) <= t < int(1070e9)]
        self.assertLessEqual(len(absent), 20)                          # 60 s d'absence : quelques relectures
        self.assertGreaterEqual(len(absent), 5)                        # mais jamais une attente aveugle
        self.assertTrue(any(int(1070e9) <= t < int(1076e9) for t, _ in self.k.opens))
        self.assertEqual(len([t for t, _ in self.k.opens if t >= int(1070e9)]), 1)   # rouvert une fois, tenu
        self.assertEqual(self.k.open_fds(), [])                                        # arrêt : fermé

    def test_config_not_ours_releases_follower_kernel_default_back(self):
        def foreign():
            a = self.k.adap()
            a.osd = "Kodi"
            self.k._event(a, a.pa, a.mask)
        self.k.at(1010, foreign)
        self.tv(1020, [0x04, 0x8F])
        self.run_until(1100)
        self.assertEqual(self.k.ours(), [])
        self.assertEqual([f for _, f in self.k.kernel_aborts(0x8F)], [bytes([0x40, 0x00, 0x8F, 0x00])])
        self.assertEqual(self.k.open_fds(), [])
        self.assertIn("not_ours", [e["reason"] for e in events(self.logs, "cec_follower_waiting")])

    def test_not_ours_at_start_waits_then_holds_after_claim(self):
        a = self.k.adap()
        a.osd, a.prim, a.num_log_addrs, a.configured = "", 0, 0, False

        def claim():
            a.osd, a.prim, a.num_log_addrs, a.configured = "OpenTV", 4, 1, True
            self.k._event(a, a.pa, a.mask)
        self.k.at(1020, claim)
        self.tv(1030, [0x04, 0x8F])
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x40, 0x90, 0x00])])
        before = [t for t, _ in self.k.opens if t < int(1020e9)]
        self.assertLessEqual(len(before), 12)

    def test_tv_off_at_start_holds_and_waits(self):
        a = self.k.adap()
        a.pa, a.configured = 0xFFFF, False
        self.k.at(1050, lambda: self.k.hpd_high(0x1000))
        self.tv(1050.3, [0x0F, 0x86, 0x10, 0x00])
        self.run_until(1100)
        self.assertEqual([f for _, f in self.k.ours()], [bytes([0x4F, 0x82, 0x10, 0x00])])
        self.assertEqual(len(self.k.opens), 1)

    def test_lost_messages_logged_and_continue(self):
        self.k.at(1010, self.k.lost)
        self.tv(1020, [0x04, 0x8F])
        self.run_until(1100)
        self.assertEqual(len(self.k.ours()), 1)
        self.assertEqual(len(events(self.logs, "cec_follower_lost")), 1)

    def test_nack_never_retransmitted(self):
        self.k.nack.add(0)
        for i in range(10):
            self.tv(1010 + i, [0x04, 0x8F])
        self.assertEqual(self.run_until(1100), 0)
        self.assertEqual(len(self.k.ours()), 10)
        self.assertEqual(self.f.counters["tx_failed"], 10)

    def test_stop_releases_mode_and_closes(self):
        seen = {}
        orig = self.k.close

        def close(fd):
            seen["mode"] = self.k.fhs[fd].mode
            orig(fd)
        self.k.close = close
        self.assertEqual(self.run_until(1050), 0)
        self.assertEqual(seen["mode"], cf.MODE_PLAIN)
        self.assertEqual(self.k.open_fds(), [])
        self.assertEqual(events(self.logs)[-1]["event"], "cec_follower_released")

    def test_uninstalled_stops_cleanly(self):
        self.tv(1100, [0x04, 0x8F])
        code = self.run_until(2000, installed=lambda: self.k.clock.s < 1050)
        self.assertEqual(code, 0)
        self.assertEqual(self.k.ours(), [])
        self.assertEqual(self.k.open_fds(), [])
        self.assertIn("cec_follower_uninstalled", [e["event"] for e in self.logs])
        self.assertLess(self.k.clock.s, 1060)

    def test_mode_drift_is_fatal(self):
        def drift():
            fd = self.k.open_fds()[0]
            self.k.fhs[fd].mode = 0x31
        self.k.at(1010, drift)
        self.assertEqual(self.run_until(1100), cf.EXIT_FATAL)
        self.assertEqual(events(self.logs, "cec_follower_failed")[0]["reason"], "mode_changed")

    def test_unexpected_error_releases_and_raises(self):
        self.tv(1010, [0x04, 0x8F])
        f, logs, lock = make(self.k, 1100)
        with mock.patch.object(cf, "decide", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                f.serve()
        self.assertEqual(self.k.open_fds(), [])

    def test_unreadable_adapters_fail_truthfully(self):
        def refuse(path):
            raise PermissionError(errno.EACCES, path)
        logs = []
        f = cf.Follower(devices=self.k.devices, open_adapter=refuse, mono_ns=self.k.clock, sleep=self.k.sleep,
                        log=logs.append, stop=lambda: self.k.clock.s >= 2000)
        self.assertEqual(f.serve(), cf.EXIT_FATAL)
        self.assertEqual(events(logs, "cec_follower_failed")[0]["reason"], "adapter_unreadable")
        self.assertLess(self.k.clock.s, 1030)

    def test_rc_passthru_off_warns_once(self):
        self.k.adap().rc_passthru = False
        self.run_until(1020)
        self.assertEqual([e.get("warning") for e in events(self.logs, "cec_follower_warning")], ["rc_passthru_off"])

    def test_abi_mismatch_fatal_before_any_open(self):
        with mock.patch.object(cf, "abi_errors", lambda: ["drift"]):
            self.assertEqual(self.run_until(1100), cf.EXIT_FATAL)
        self.assertEqual(self.k.opens, [])

    def test_production_logs_are_sober_canary_bounded(self):
        for i in range(200):
            self.tv(1010 + i * 15, [0x04, 0x8F])
        self.run_until(1010 + 200 * 15 + 5)
        self.assertEqual(len(self.k.ours()), 200)
        self.assertLessEqual(len(self.logs), 8)
        self.assertNotIn("cec_follower_rx", [e["event"] for e in self.logs])
        k2 = FakeKernel(Clock(1000.0))
        for i in range(2000):
            k2.at(1010 + i * 1.5, lambda: k2.send(bytes([0x04, 0x8F])))
        f, logs, _ = make(k2, 1010 + 2000 * 1.5 + 5, canary=True)
        f.serve()
        rx = [e for e in logs if e["event"] in ("cec_follower_rx", "cec_follower_tx")]
        self.assertEqual(len(rx), cf.CANARY_LOG_MAX_PER_HOUR)
        tx = [e for e in logs if e["event"] == "cec_follower_tx"]
        self.assertTrue(all(e["e2e_ms"] < 1000 for e in tx))
        self.assertTrue(all(set(e) <= {"event", "opcode", "initiator", "action", "reason", "kind", "ok",
                                       "e2e_ms", "submit_ms"} for e in rx))

    def test_stats_hourly_only_when_changed(self):
        self.tv(1010, [0x04, 0x8F])
        self.run_until(1000 + 4 * 3600)
        st = events(self.logs, "cec_follower_stats")
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["counters"]["power_replies"], 1)


class TestReturnInterplay(unittest.TestCase):
    def test_follower_active_source_cancels_return_recovery(self):
        """Avec le suiveur, la TV qui revient (ROUTING_CHANGE + SET_STREAM_PATH) reçoit l'<Active Source> du suiveur
        en quelques ms ; le moniteur de reprise voit notre propre émission et n'annonce pas une seconde fois."""
        det = cecreturn.Detector("1.0.0.0", 0x10)
        F = cecreturn.Frame
        det.frame(F(True, 0, 15, 0x82, b"\x00\x00"), 0.0)
        det.frame(F(True, 0, 15, 0x80, b"\x00\x00\x10\x00"), 10.0)
        det.frame(F(True, 0, 15, 0x86, b"\x10\x00"), 10.1)
        self.assertIsNotNone(det.deadline())
        det.frame(F(False, 4, 15, 0x82, b"\x10\x00"), 10.13)
        self.assertIsNone(det.deadline())
        self.assertFalse(det.take(20.0))

    def test_follower_traffic_parses_in_monitor_and_never_disarms(self):
        p = cecreturn.Parser()
        ka = cecreturn.Keepalive("1.0.0.0", 0x10)
        lines = ["Transmitted by Playback Device 1 to TV (4 to 0): REPORT_POWER_STATUS (0x90):",
                 "\tpwr-state: on (0x00)", "\tRaw: 0x40 0x90 0x00 (3)",
                 "Transmitted by Playback Device 1 to TV (4 to 0): FEATURE_ABORT (0x00):",
                 "\tabort-msg: MENU_REQUEST (0x8d)", "\treason: Unrecognized Op (0x00)",
                 "\tRaw: 0x40 0x00 0x8d 0x00 (4)",
                 "Transmitted by Playback Device 1 to all (4 to 15): ACTIVE_SOURCE (0x82):",
                 "\tphys-addr: 1.0.0.0", "\tRaw: 0x4f 0x82 0x10 0x00 (4)"]
        items = [it for line in lines for it in p.feed(line)]
        self.assertEqual([it[0] for it in items], ["frame"] * 3)
        self.assertEqual([ka.stop_reason(it[1]) for it in items], [None, None, None])


# --------------------------------------------------------------------------- configuration, CLI, santé
class TestConfigCli(unittest.TestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def env(self, content=""):
        d = tempfile.mkdtemp(prefix="otvcecf")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "env")
        with open(p, "w") as f:
            f.write(content)
        return d, p

    def test_config_values(self):
        self.assertEqual(config.Config({}).cec_follower, "on")
        self.assertEqual(config.Config({"OPENTV_CEC_FOLLOWER": "1"}).cec_follower, "on")
        self.assertEqual(config.Config({"OPENTV_CEC_FOLLOWER": "0"}).cec_follower, "off")
        self.assertEqual(config.Config({"OPENTV_CEC_FOLLOWER": "canary"}).cec_follower, "canary")
        for bad in ("oui", "2", "CANARY", ""):
            with self.assertRaises(ValueError):
                config.Config({"OPENTV_CEC_FOLLOWER": bad})

    def test_poll_keepalive_off_while_follower_on_and_back_on_rollback(self):
        self.assertFalse(config.Config({}).cec_return_keepalive)
        self.assertFalse(config.Config({"OPENTV_CEC_FOLLOWER": "canary"}).cec_return_keepalive)
        self.assertFalse(config.Config({"OPENTV_CEC_FOLLOWER": "1", "OPENTV_CEC_RETURN_KEEPALIVE": "1"})
                         .cec_return_keepalive)
        self.assertTrue(config.Config({"OPENTV_CEC_FOLLOWER": "0"}).cec_return_keepalive)
        self.assertFalse(config.Config({"OPENTV_CEC_FOLLOWER": "0", "OPENTV_CEC_RETURN_KEEPALIVE": "0"})
                         .cec_return_keepalive)
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("#OPENTV_CEC_FOLLOWER=1", text)

    def test_root_refused_disabled_and_enabled(self):
        d, env = self.env()
        with mock.patch.object(os, "geteuid", lambda: 0):
            code, out = self.run_cli("--env", env, "cec-follower")
        self.assertEqual((code, out[0]["error"]), (2, "run_via_systemd"))
        for content in ("OPENTV_CEC_FOLLOWER=0\n", "OPENTV_CEC_BOOTSTRAP=0\n"):
            with open(env, "w") as f:
                f.write(content)
            with mock.patch.object(os, "geteuid", lambda: 1000), \
                    mock.patch.object(cf.Follower, "serve", side_effect=AssertionError("started")):
                code, out = self.run_cli("--env", env, "cec-follower")
            self.assertEqual((code, out[0]["result"]), (0, "disabled"))
        seen = {}

        def serve(me, max_cycles=None):
            seen["canary"] = me.canary
            return None
        for content, want in (("", False), ("OPENTV_CEC_FOLLOWER=canary\n", True)):
            with open(env, "w") as f:
                f.write(content)
            with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cf.Follower, "serve", serve):
                code, _ = self.run_cli("--env", env, "cec-follower", "--root", d, "--state-dir", d)
            self.assertEqual((code, seen["canary"]), (0, want))

    def test_return_monitor_gets_effective_keepalive(self):
        d, env = self.env()
        seen = {}

        def serve(me, max_cycles=None):
            seen["keepalive"] = me.keepalive
        for content, want in (("", False), ("OPENTV_CEC_FOLLOWER=0\n", True)):
            with open(env, "w") as f:
                f.write(content)
            with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cecreturn.Monitor, "serve", serve):
                code, _ = self.run_cli("--env", env, "cec-return-monitor", "--root", d, "--state-dir", d)
            self.assertEqual((code, seen["keepalive"]), (0, want), content)

    def test_manual_activate_and_boot_announce_untouched(self):
        import inspect
        src = inspect.getsource(cli.cmd_cec) + inspect.getsource(cli.cmd_cec_bootstrap) + \
            inspect.getsource(cecboot)
        self.assertNotIn("cecfollower", src)
        self.assertEqual(cli.CEC_ANNOUNCE_UNIT, "opentv-cec-announce.service")
        self.assertNotIn("cecfollower", inspect.getsource(cecreturn))

    def test_health_warns_when_follower_absent_or_failed(self):
        d = tempfile.mkdtemp(prefix="otvcecf")
        self.addCleanup(shutil.rmtree, d, True)

        def runner_for(state):
            def runner(argv, timeout):
                if argv[:2] == ["systemctl", "is-active"]:
                    return 0, (state if argv[2] == UNIT else "active") + "\n"
                return 1, ""
            return runner
        with mock.patch.object(health, "verify_manifest", lambda root: {"ok": True, "error": None}), \
                mock.patch.object(health.netstate, "snapshot", lambda r: {"mode": "client"}), \
                mock.patch.object(health.ratings, "health_view", lambda cfg, runner=None: {}):
            for state, cfgd, want in (("failed", {}, True), ("inactive", {}, True), ("active", {}, False),
                                      ("failed", {"OPENTV_CEC_FOLLOWER": "0"}, False),
                                      ("inactive", {"OPENTV_CEC_BOOTSTRAP": "0"}, False),
                                      ("failed", {"OPENTV_CEC_FOLLOWER": "canary"}, True)):
                rep = health.run(config.Config(cfgd), root=d, runner=runner_for(state), check_http=False)
                self.assertEqual("cec_follower_failed" in rep["warnings"], want, (state, cfgd))
        self.assertIn(UNIT, health.UNITS_OPTIONAL)
        self.assertNotIn(UNIT, health.UNITS_REQUIRED)


# --------------------------------------------------------------------------- unité systemd
def unit(name=UNIT):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        text = f.read()
    cp.read_string(text)
    return cp, text


class TestUnit(unittest.TestCase):
    def test_unit_is_narrow(self):
        cp, text = unit()
        s = cp["Service"]
        code = re.sub(r"^#.*$", "", text, flags=re.M)
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi cec-follower")
        self.assertEqual((s["Type"], s["User"], s["Group"]), ("simple", "opentv", "opentv"))
        self.assertEqual(s["SupplementaryGroups"].split(), ["video"])
        # EXCL_FOLLOWER/TRANSMIT n'exigent aucune capacité (seuls les modes moniteur exigent CAP_NET_ADMIN)
        self.assertEqual((s["CapabilityBoundingSet"], s["AmbientCapabilities"]), ("", ""))
        self.assertNotIn("CAP_", code)
        self.assertEqual(sorted(re.findall(r"^DeviceAllow=(.+)$", text, re.M)), ["char-cec rw"])
        self.assertEqual((s["DevicePolicy"], s["ProtectSystem"], s["ProtectHome"], s["PrivateTmp"]),
                         ("closed", "strict", "yes", "yes"))
        self.assertEqual((s["NoNewPrivileges"], s["PrivateNetwork"], s["IPAddressDeny"], s["RestrictAddressFamilies"]),
                         ("yes", "yes", "any", "AF_UNIX"))
        self.assertEqual(s["SystemCallFilter"], "@system-service")
        self.assertEqual(s["MemoryDenyWriteExecute"], "yes")
        self.assertEqual((s["RuntimeDirectory"], s["RuntimeDirectoryPreserve"]), ("opentv-cec", "yes"))
        same = unit("opentv-cec.service")[0]["Service"]
        self.assertEqual((s["RuntimeDirectoryMode"], s["UMask"]), (same["RuntimeDirectoryMode"], same["UMask"]))
        self.assertEqual(s["Restart"], "on-failure")
        self.assertEqual(s["RestartPreventExitStatus"].split(), [str(cf.EXIT_FATAL)])
        self.assertLessEqual(int(s["RestartSec"]), 10)
        self.assertLessEqual(int(cp["Unit"]["StartLimitBurst"]), 10)
        self.assertLessEqual(int(s["TimeoutStopSec"]), 10)
        self.assertIn("MemoryMax", s)
        self.assertLessEqual(int(s["TasksMax"]), 8)
        inacc = re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)
        self.assertIn("-/etc/opentv/tmdb", inacc)
        self.assertIn("-/var/lib/opentv/canvas", inacc)
        self.assertEqual(cp["Install"]["WantedBy"], "multi-user.target")
        for bad in ("EnvironmentFile", "ExecStartPre", "ExecStartPost", "input", "--force-announce", "cec-ctl",
                    "ReadWritePaths"):
            self.assertNotIn(bad, code, bad)
        self.assertIn(UNIT, ratings.MASKED_UNITS)

    def test_return_unit_keeps_its_contract(self):
        s = unit("opentv-cec-return.service")[0]["Service"]
        self.assertEqual(s["CapabilityBoundingSet"], "CAP_NET_ADMIN")


# --------------------------------------------------------------------------- installation, retour arrière, docs
def sh(args):
    env = dict(os.environ, LC_ALL="C")
    env.pop("OPENTV_ROOT", None)
    return subprocess.run(["bash"] + list(args), capture_output=True, timeout=180, env=env)


class TestInstall(unittest.TestCase):
    def test_install_enable_restart_and_uninstall(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), encoding="utf-8") as f:
            man = f.read()
        for path in ("/etc/systemd/system/" + UNIT, "/opt/opentv/lib/opentv_pi/cecfollower.py"):
            self.assertIn("  " + path + "\n", man)
        self.assertEqual(health.verify_manifest(r)["mismatch"], [])
        self.assertRegex(out, r"systemctl enable [^\n]*%s" % re.escape(UNIT))
        i_reset = out.index("systemctl reset-failed")
        self.assertIn(UNIT, out[i_reset:out.index("\n", i_reset)])
        m = re.search(r"systemctl restart [^\n]*%s" % re.escape(UNIT), out)
        self.assertIsNotNone(m)
        self.assertLess(i_reset, m.start())
        # le moniteur de reprise redémarre dans la même transaction : l'ancien POLL 0.7.4 cesse à la mise à jour
        self.assertIn("opentv-cec-return.service", m.group(0))
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        i_disable = out.index("systemctl disable --now")
        block = out[i_disable:out.index("\n", out.index(UNIT, i_disable))]
        self.assertIn(UNIT, block)
        self.assertLess(i_disable, out.index("cec-bootstrap --release"))      # libéré avant l'effacement
        self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", UNIT)))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))

    def test_downgrade_self_stop_uses_module_file(self):
        files = cf.follower_files()
        self.assertIn(os.path.abspath(cf.__file__), files)
        self.assertTrue(cf.installed_check(files)())
        self.assertFalse(cf.installed_check(files + ["/nonexistent/opentv-follower"])())


class TestReleaseDocs(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "0.7.6")

    def test_docs(self):
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                text = f.read()
            for need in ("opentv-cec-follower.service", "EXCL_FOLLOWER", "GIVE_DEVICE_POWER_STATUS",
                         "REPORT_POWER_STATUS", "SET_STREAM_PATH", "FEATURE_ABORT", "OPENTV_CEC_FOLLOWER=0",
                         "OPENTV_CEC_FOLLOWER=canary", "cec_follower_failed", "0.7.6", "cec-follower",
                         "aucun POLL", "rollback"):
                self.assertTrue(need in text, (name, need))
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        gate = next(line for line in readme.splitlines() if line.startswith("| **Suiveur CEC (0.7.6)"))
        for need in ("FEATURE_ABORT", "8f", "< 1 s", "YouTube", "15 min", "éteinte", "autre entrée HDMI",
                     "réveil", "cec_follower_ready"):
            self.assertIn(need, gate)
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            agents = f.read()
        for need in ("cecfollower", "0.7.6", "EXCL_FOLLOWER"):
            self.assertIn(need, agents)


if __name__ == "__main__":
    unittest.main()
