"""Mode HDMI exact du kiosque après extinction/rallumage de la TV (0.7.5) : cible configurable exacte, lecture de
wlr-randr, décision sans écriture quand tout va bien, réaction aux uevents DRM simulés sans scrutation, budgets
d'écriture et de journal bornés, unité systemd sandboxée, installation/désinstallation, santé et boot-check."""
import configparser
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import partial
from unittest import mock

from tests._common import PI4
from opentv_pi import VERSION, displaymode as dm, health

SYSTEMD = os.path.join(PI4, "systemd")
UNIT = "opentv-display-mode.service"
BIN = os.path.join(PI4, "bin", "opentv-display-mode")
OPENER = os.path.join(PI4, "bin", "opentv-kiosk-browser")
INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
BOOT_CHECK = os.path.join(PI4, "system", "opentv-boot-check")
TARGET = (1920, 1080, 60000)


def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
    cp.optionxform = str
    with open(os.path.join(SYSTEMD, name), encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def code_only(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# --------------------------------------------------------------------------- faux compositeur (Cage + wlroots)
SAMSUNG_MODES = [(3840, 2160, 30000), (1920, 1080, 23976), (1920, 1080, 60000), (1920, 1080, 59940),
                 (1280, 720, 60000)]


def render(heads, v4=False):
    """Même format que print_state() de wlr-randr 0.2.0/0.4.1 (printf « %f » d'un float)."""
    out = []
    for name, h in heads.items():
        out.append('%s "Samsung Electric Company QE55 (%s)"' % (name, name))
        if v4:
            out += ["  Make: Samsung Electric Company", "  Model: QE55", "  Serial: 0x01000E00"]
        out.append("  Physical size: 1210x680 mm")
        out.append("  Enabled: %s" % ("yes" if h["enabled"] else "no"))
        out.append("  Modes:")
        for m in h["modes"]:
            line = "    %dx%d px, %f Hz" % (m[0], m[1], m[2] / 1000.0)
            flags = [f for f, on in (("preferred", m == h.get("preferred")), ("current", m == h.get("current"))) if on]
            if flags:
                line += " (%s)" % ", ".join(flags)
            out.append(line)
        if h["enabled"]:
            out += ["  Position: 0,0", "  Transform: normal", "  Scale: 1.000000"]
            if v4:
                out.append("  Adaptive Sync: disabled")
    return "\n".join(out) + ("\n" if out else "")


# Sortie RÉELLE de wlr-randr 0.2.0 (Raspberry Pi OS, cage 0.2.0) sur la Samsung terrain, relevée le 2026-11-01 en
# lecture seule par le compte opentv-kiosk après le correctif manuel : 47 modes, deux 1920x1080@60.000 identiques, le
# mode préféré 3840x2160@30 listé EN DERNIER.
FIELD_2026_11_01 = """\
HDMI-A-1 "Samsung Electric Company SAMSUNG 0x01000E00 (HDMI-A-1)"
  Physical size: 1210x680 mm
  Enabled: yes
  Modes:
    720x400 px, 70.082001 Hz
    640x480 px, 59.939999 Hz
    640x480 px, 60.000000 Hz
    640x480 px, 66.667000 Hz
    640x480 px, 72.808998 Hz
    640x480 px, 75.000000 Hz
    720x480 px, 59.939999 Hz
    720x480 px, 60.000000 Hz
    720x576 px, 50.000000 Hz
    800x600 px, 60.317001 Hz
    800x600 px, 72.188004 Hz
    800x600 px, 75.000000 Hz
    832x624 px, 74.551003 Hz
    1024x768 px, 60.004002 Hz
    1024x768 px, 70.069000 Hz
    1024x768 px, 75.028999 Hz
    1280x720 px, 50.000000 Hz
    1280x720 px, 59.939999 Hz
    1280x720 px, 60.000000 Hz
    1280x720 px, 60.000000 Hz
    1152x864 px, 75.000000 Hz
    1280x800 px, 59.910000 Hz
    1440x900 px, 59.901001 Hz
    1280x1024 px, 60.020000 Hz
    1280x1024 px, 75.025002 Hz
    1600x900 px, 60.000000 Hz
    1680x1050 px, 59.882999 Hz
    1920x1080 px, 23.976000 Hz
    1920x1080 px, 24.000000 Hz
    1920x1080 px, 25.000000 Hz
    1920x1080 px, 29.969999 Hz
    1920x1080 px, 30.000000 Hz
    1920x1080 px, 50.000000 Hz
    1920x1080 px, 59.939999 Hz
    1920x1080 px, 60.000000 Hz (current)
    1920x1080 px, 60.000000 Hz
    3840x2160 px, 23.976000 Hz
    3840x2160 px, 24.000000 Hz
    3840x2160 px, 25.000000 Hz
    3840x2160 px, 29.969999 Hz
    3840x2160 px, 30.000000 Hz
    4096x2160 px, 23.976000 Hz
    4096x2160 px, 24.000000 Hz
    4096x2160 px, 25.000000 Hz
    4096x2160 px, 29.969999 Hz
    4096x2160 px, 30.000000 Hz
    3840x2160 px, 30.000000 Hz (preferred)
  Position: 0,0
  Transform: normal
  Scale: 1.000000
"""


class FakeCage:
    """Compositeur simulé : sortie présente ou non (TV éteinte), mode courant, refus éventuel d'écrire."""

    def __init__(self, current=(1920, 1080, 60000), connected=True, wayland=True, v4=False):
        self.connected, self.wayland, self.v4 = connected, wayland, v4
        self.current, self.calls, self.ignore_writes, self.write_rc = current, [], False, 0
        self.enabled = True

    def heads(self):
        if not self.connected:
            return {}
        return {"HDMI-A-1": {"enabled": self.enabled, "modes": SAMSUNG_MODES, "preferred": SAMSUNG_MODES[0],
                             "current": self.current}}

    def hotplug_off(self):
        self.connected = False

    def hotplug_on(self):
        """wlroots recrée la sortie ; Cage l'active au mode PRÉFÉRÉ de l'EDID (terrain : 3840x2160@30)."""
        self.connected, self.current = True, SAMSUNG_MODES[0]

    def envfn(self):
        return {"WAYLAND_DISPLAY": "wayland-0"} if self.wayland else None

    def runner(self, args, env):
        self.calls.append(list(args))
        if not args:
            return 0, render(self.heads(), self.v4)
        assert args[:3] == ["--output", "HDMI-A-1", "--mode"], args
        m = re.fullmatch(r"(\d+)x(\d+)@(\d+\.\d{3})Hz", args[3])
        assert m, args
        want = (int(m.group(1)), int(m.group(2)), int(round(float(m.group(3)) * 1000)))
        if self.write_rc:
            return self.write_rc, ""
        if want not in SAMSUNG_MODES:            # wlr-randr : « unknown mode », jamais de mode fabriqué
            return 1, ""
        if not self.ignore_writes:
            self.current = want
        return 0, ""

    def writes(self):
        return [c for c in self.calls if c]


def check_for(cage, target=TARGET):
    return dm.Check("HDMI-A-1", target, runner=cage.runner, envfn=cage.envfn)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def logs():
    lines = []
    return lines, dm.Log(write=lines.append, mono=time.monotonic)


# --------------------------------------------------------------------------- configuration
class TestTarget(unittest.TestCase):
    def test_exact_targets_and_unambiguous_default(self):
        self.assertEqual(dm.parse_target("1920x1080"), (1920, 1080, 60000))    # jamais « le premier 1920x1080 »
        self.assertEqual(dm.parse_target("1920x1080@60"), (1920, 1080, 60000))
        self.assertEqual(dm.parse_target("1920x1080@60Hz"), (1920, 1080, 60000))
        self.assertEqual(dm.parse_target("1920x1080@59.94"), (1920, 1080, 59940))
        self.assertEqual(dm.parse_target("1920x1080@23.976Hz"), (1920, 1080, 23976))
        self.assertEqual(dm.parse_target(" 3840x2160@30 "), (3840, 2160, 30000))
        for bad in ("", "1920", "1920x1080@", "1920x1080@6", "1920x1080@60.0001", "1920x1080 60", "x1080",
                    "1920x1080@60;reboot", "99999x1080", "1920x1080@1000", "--mode"):
            self.assertIsNone(dm.parse_target(bad), bad)
        self.assertEqual(dm.fmt_mode(1920, 1080, 59940), "1920x1080@59.940")

    def test_env_defaults_and_invalid_values_fall_back_safely(self):
        self.assertEqual(dm.config_from_env({}), ("HDMI-A-1", TARGET, []))
        # ancien défaut documenté OPENTV_HDMI_MODE=1920x1080 : désormais exactement 60 Hz
        self.assertEqual(dm.config_from_env({"OPENTV_HDMI_MODE": "1920x1080"})[1], TARGET)
        self.assertEqual(dm.config_from_env({"OPENTV_HDMI_OUTPUT": "HDMI-A-2", "OPENTV_HDMI_MODE": "1280x720@50"}),
                         ("HDMI-A-2", (1280, 720, 50000), []))
        out, tgt, err = dm.config_from_env({"OPENTV_HDMI_OUTPUT": "DSI-1;x", "OPENTV_HDMI_MODE": "4k"})
        self.assertEqual((out, tgt), ("HDMI-A-1", TARGET))
        self.assertEqual(err, ["output_invalid", "mode_invalid"])


# --------------------------------------------------------------------------- lecture et décision
class TestParseAndDecide(unittest.TestCase):
    def field_heads(self, v4=False):
        cage = FakeCage(current=SAMSUNG_MODES[0], v4=v4)
        return dm.parse_wlr_randr(render(cage.heads(), v4))

    def test_parses_wlr_randr_02_and_04_formats_exactly(self):
        for v4 in (False, True):
            h = self.field_heads(v4)
            self.assertEqual(list(h), ["HDMI-A-1"])
            self.assertIs(h["HDMI-A-1"]["enabled"], True)
            self.assertEqual([m[:3] for m in h["HDMI-A-1"]["modes"]], SAMSUNG_MODES)   # mHz exacts (59.939999)
            self.assertEqual([m for m in h["HDMI-A-1"]["modes"] if m[3]], [(3840, 2160, 30000, True, True)])

    def test_field_regression_4k_preferred_is_corrected_to_exact_60hz_never_23976(self):
        res, cur, best = dm.decide(self.field_heads(), "HDMI-A-1", TARGET)
        self.assertEqual(res, "drift")
        self.assertEqual(cur[:3], (3840, 2160, 30000))
        self.assertEqual(best[:3], (1920, 1080, 60000))       # pas 23,976 (premier annoncé), pas 59,94

    def test_nothing_to_do_when_correct_absent_disabled_or_unavailable(self):
        ok = dm.parse_wlr_randr(render(FakeCage().heads()))
        self.assertEqual(dm.decide(ok, "HDMI-A-1", TARGET)[0], "ok")
        self.assertEqual(dm.decide({}, "HDMI-A-1", TARGET), ("absent", None, None))
        self.assertEqual(dm.decide(ok, "HDMI-A-2", TARGET)[0], "absent")
        off = FakeCage()
        off.enabled = False
        self.assertEqual(dm.decide(dm.parse_wlr_randr(render(off.heads())), "HDMI-A-1", TARGET)[0], "disabled")
        self.assertEqual(dm.decide(ok, "HDMI-A-1", (2560, 1440, 60000))[0], "unavailable")
        # 59,94 Hz n'est PAS 60 Hz (60 mHz d'écart) : le courant 59,94 est corrigé vers 60,000 annoncé
        h = dm.parse_wlr_randr(render(FakeCage(current=(1920, 1080, 59940)).heads()))
        res, _, best = dm.decide(h, "HDMI-A-1", TARGET)
        self.assertEqual((res, best[:3]), ("drift", (1920, 1080, 60000)))
        # cible 59,94 configurée : respectée exactement
        self.assertEqual(dm.decide(h, "HDMI-A-1", (1920, 1080, 59940))[0], "ok")

    def test_real_samsung_output_of_2026_11_01(self):
        h = dm.parse_wlr_randr(FIELD_2026_11_01)
        modes = h["HDMI-A-1"]["modes"]
        self.assertEqual(len(modes), 47)
        self.assertEqual([m[:3] for m in modes if m[3]], [(3840, 2160, 30000)])            # préféré : dernier
        self.assertEqual([m[:3] for m in modes if m[4]], [(1920, 1080, 60000)])
        self.assertEqual(dm.decide(h, "HDMI-A-1", TARGET)[0], "ok")                         # état actuel : rien
        # même TV après rallumage : Cage au mode préféré ⇒ cible exacte 60,000 (jamais 23,976/24/…/59,94)
        drifted = FIELD_2026_11_01.replace(" (current)", "").replace("30.000000 Hz (preferred)",
                                                                       "30.000000 Hz (preferred, current)")
        res, cur, best = dm.decide(dm.parse_wlr_randr(drifted), "HDMI-A-1", TARGET)
        self.assertEqual((res, cur[:3], best[:3]), ("drift", (3840, 2160, 30000), (1920, 1080, 60000)))
        # wlr-randr 0.2.0 compare les mHz EXACTEMENT : « 60.000 » -> 60000 ⇒ premier 1920x1080 à 60,000 Hz
        self.assertEqual(int(round(float("%d.%03d" % (best[2] // 1000, best[2] % 1000)) * 1000)), 60000)

    def test_unknown_lines_and_garbage_never_crash(self):
        self.assertEqual(dm.parse_wlr_randr(""), {})
        h = dm.parse_wlr_randr('junk\nHDMI-A-1 "x"\n  Enabled: yes\n  Modes:\n    garbage\n  Scale: 1\n')
        self.assertEqual(h, {"HDMI-A-1": {"enabled": True, "modes": []}})
        self.assertEqual(dm.decide(h, "HDMI-A-1", TARGET)[0], "unavailable")


class TestCheck(unittest.TestCase):
    def test_correct_mode_reads_only(self):
        cage = FakeCage()
        self.assertEqual(check_for(cage)()["result"], "ok")
        self.assertEqual(cage.writes(), [])

    def test_disconnected_disabled_or_no_wayland_never_writes(self):
        for setup in ("off", "disabled", "nowl"):
            cage = FakeCage(current=SAMSUNG_MODES[0])
            if setup == "off":
                cage.hotplug_off()
            elif setup == "disabled":
                cage.enabled = False
            else:
                cage.wayland = False
            r = check_for(cage)()
            self.assertEqual(r["result"], {"off": "absent", "disabled": "disabled", "nowl": "no_wayland"}[setup])
            self.assertEqual(cage.writes(), [], setup)

    def test_drift_applies_the_exact_announced_mode_and_verifies(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        r = check_for(cage)()
        self.assertEqual(r, {"result": "applied", "current": "3840x2160@30.000", "applied": "1920x1080@60.000",
                             "now": "1920x1080@60.000"})
        self.assertEqual(cage.writes(), [["--output", "HDMI-A-1", "--mode", "1920x1080@60.000Hz"]])
        self.assertEqual(cage.current, (1920, 1080, 60000))

    def test_apply_failure_and_unverified_are_reported_not_hidden(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.write_rc = 1
        self.assertEqual(check_for(cage)()["result"], "apply_failed")
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True
        self.assertEqual(check_for(cage)()["result"], "apply_unverified")

    def test_unavailable_target_never_fabricates_a_custom_mode(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        r = check_for(cage, (2560, 1440, 60000))()
        self.assertEqual(r["result"], "unavailable")
        self.assertEqual(cage.writes(), [])

    def test_budget_exhausted_reports_suppressed_without_writing(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        self.assertEqual(check_for(cage)(may_apply=False)["result"], "suppressed")
        self.assertEqual(cage.writes(), [])


class TestRealSubprocessRoundTrip(unittest.TestCase):
    """Vrai sous-processus, argv exact, format réel : un faux wlr-randr stocke l'état dans un fichier."""
    FAKE = r'''import json, sys
state_path = sys.argv[1]
args = sys.argv[2:]
st = json.load(open(state_path))
st["calls"].append(args)
if args:
    w, rest = args[3].split("x"); h, hz = rest.split("@"); hz = hz[:-2]
    st["current"] = [int(w), int(h), int(round(float(hz) * 1000))]
else:
    print('HDMI-A-1 "Samsung"')
    print("  Enabled: yes")
    print("  Modes:")
    for m in st["modes"]:
        flag = " (preferred, current)" if m == st["current"] and m == st["modes"][0] else (
            " (current)" if m == st["current"] else (" (preferred)" if m == st["modes"][0] else ""))
        print("    %dx%d px, %f Hz%s" % (m[0], m[1], m[2] / 1000.0, flag))
    print("  Scale: 1.000000")
json.dump(st, open(state_path, "w"))
'''

    def test_fake_binary_round_trip(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        state = os.path.join(d, "state.json")
        with open(state, "w") as f:
            json.dump({"calls": [], "current": list(SAMSUNG_MODES[0]), "modes": [list(m) for m in SAMSUNG_MODES]}, f)
        fake = os.path.join(d, "wlr-randr")
        with open(fake, "w") as f:
            f.write("#!%s\nimport runpy, sys\nsys.argv = [sys.argv[0], %r] + sys.argv[1:]\n" % (sys.executable, state))
            f.write("exec(%r)\n" % self.FAKE)
        os.chmod(fake, 0o755)
        runner = partial(dm.run_wlr_randr, binary=fake)
        c = dm.Check("HDMI-A-1", TARGET, runner=runner, envfn=lambda: {"PATH": "/usr/bin:/bin"})
        self.assertEqual(c()["result"], "applied")
        self.assertEqual(c()["result"], "ok")
        with open(state) as f:
            st = json.load(f)
        self.assertEqual(st["current"], [1920, 1080, 60000])
        self.assertEqual([a for a in st["calls"] if a], [["--output", "HDMI-A-1", "--mode", "1920x1080@60.000Hz"]])
        self.assertEqual(dm.run_wlr_randr([], {}, binary=os.path.join(d, "absent")), (127, ""))


class TestWaylandSocket(unittest.TestCase):
    def test_finds_only_a_socket_owned_by_the_account(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        env = {"XDG_RUNTIME_DIR": d}
        self.assertIsNone(dm.wayland_env(env))
        with open(os.path.join(d, "wayland-0"), "w"):
            pass                                             # fichier ordinaire : refusé
        self.assertIsNone(dm.wayland_env(env))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(os.path.join(d, "wayland-1"))
        e = dm.wayland_env(env)
        self.assertEqual((e["XDG_RUNTIME_DIR"], e["WAYLAND_DISPLAY"]), (d, "wayland-1"))
        self.assertEqual(set(e), {"XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "PATH", "LC_ALL"})   # rien d'autre hérité
        self.assertIsNone(dm.wayland_env(env, uid=os.geteuid() + 1))                          # autre compte
        self.assertEqual(dm.wayland_env({"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "../x"})["WAYLAND_DISPLAY"],
                         "wayland-1")


# --------------------------------------------------------------------------- uevents simulés
def uevent(action="change", subsystem="drm", hotplug=True, extra=()):
    fields = ["ACTION=" + action, "DEVPATH=/devices/platform/gpu/drm/card1", "SUBSYSTEM=" + subsystem,
              "DEVNAME=dri/card1", "DEVTYPE=drm_minor", "SEQNUM=4242"]
    if hotplug:
        fields.append("HOTPLUG=1")
    fields += list(extra)
    return ("%s@/devices/platform/gpu/drm/card1\0" % action + "\0".join(fields) + "\0").encode()


class TestUevents(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return dm.UeventSource(a), b

    def test_filters_drm_hotplug_only(self):
        self.assertTrue(dm.is_drm_hotplug(dm.parse_uevent(uevent())))
        self.assertTrue(dm.is_drm_hotplug(dm.parse_uevent(uevent(extra=("CONNECTOR=33",)))))
        self.assertTrue(dm.is_drm_hotplug(dm.parse_uevent(uevent(action="add", hotplug=False))))
        self.assertFalse(dm.is_drm_hotplug(dm.parse_uevent(uevent(hotplug=False))))
        self.assertFalse(dm.is_drm_hotplug(dm.parse_uevent(uevent(subsystem="usb"))))
        # uevent synthétique (echo change … > /sys/class/drm/card1/uevent) relevé sur la Pi le 2026-11-01 dans le bac à
        # sable exact de l'unité : l'argument devient SYNTH_ARG_HOTPLUG ; wlroots l'ignore, le moniteur aussi
        synth = uevent(hotplug=False, extra=("SYNTH_UUID=0", "SYNTH_ARG_HOTPLUG=1"))
        self.assertFalse(dm.is_drm_hotplug(dm.parse_uevent(synth)))
        self.assertIsNone(dm.parse_uevent(b"libudev\0\xfe\xed" + uevent()))   # format libudev : ignoré
        self.assertIsNone(dm.parse_uevent(b"garbage"))
        self.assertIsNone(dm.parse_uevent(b""))

    def test_drain_counts_relevant_events_without_blocking(self):
        src, tx = self.pair()
        for data in (uevent(), uevent(subsystem="input"), b"libudev\0junk", uevent(hotplug=False), uevent()):
            tx.send(data)
        self.assertEqual(src.drain(), 2)
        t0 = time.monotonic()
        self.assertEqual(src.drain(), 0)                          # rien : retour immédiat, jamais d'attente
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_drain_treats_overflow_as_possible_hotplug(self):
        class Sock:
            def __init__(self):
                self.n = 0

            def setblocking(self, flag):
                pass

            def recvfrom(self, size):
                self.n += 1
                if self.n == 1:
                    raise OSError(dm.errno.ENOBUFS, "overflow")
                raise BlockingIOError()
        self.assertEqual(dm.UeventSource(Sock()).drain(), 1)

    def test_only_kernel_sender_counts_on_netlink(self):
        class Sock:
            def __init__(self, items):
                self.items = list(items)

            def setblocking(self, flag):
                pass

            def recvfrom(self, size):
                if not self.items:
                    raise BlockingIOError()
                return self.items.pop(0)
        self.assertEqual(dm.UeventSource(Sock([(uevent(), (4242, 1)), (uevent(), (0, 1))])).drain(), 1)


# --------------------------------------------------------------------------- moniteur : rafales, budgets, scrutation
class SimSource:
    """Source d'événements et attente simulées : l'horloge avance jusqu'au délai ou au prochain événement."""

    def __init__(self, clock, script):
        self.clock, self.script = clock, sorted(script, key=lambda x: x[0])   # [(t, action)]
        self.pending, self.waits = 0, []

    def fileno(self):
        return -1

    def drain(self):
        n, self.pending = self.pending, 0
        return n

    def wait(self, r, w, x, timeout):
        self.waits.append(timeout)
        if len(self.waits) > 5000:
            raise AssertionError("boucle de scrutation : plus de 5000 attentes")
        nxt = self.script[0][0] if self.script else None
        if timeout is None and nxt is None:
            raise StopIteration
        deadline = self.clock.t + timeout if timeout is not None else float("inf")
        if nxt is not None and nxt <= deadline:
            self.clock.t = max(self.clock.t, nxt)
            _, action = self.script.pop(0)
            if action():
                self.pending += 1
                return [self], [], []
            return [], [], []
        self.clock.t = deadline
        return [], [], []


def simulate(cage, script, installed=None, log_lines=dm.LOG_LINES):
    """Rejoue un scénario [(secondes depuis le départ, action)] ; action() -> True si un uevent DRM est émis.
    `mon.times` : instants des vérifications ; `mon.write_times` : instants des écritures ; `mon.log_times` : instants
    des lignes de journal (secondes depuis le départ, horloge simulée)."""
    clock = Clock()
    lines, log_times = [], []

    def write(line):
        lines.append(line)
        log_times.append(clock.t - 1000.0)
    log = dm.Log(write=write, mono=clock, lines=log_lines)
    write_times = []

    def runner(args, env):
        if args:
            write_times.append(clock.t - 1000.0)
        return cage.runner(args, env)
    check = dm.Check("HDMI-A-1", TARGET, runner=runner, envfn=cage.envfn)
    times = []

    def timed(**kw):
        times.append(clock.t - 1000.0)
        return check(**kw)
    mon = dm.Monitor(timed, log, "1920x1080@60.000", "HDMI-A-1")
    src = SimSource(clock, [(clock.t + t, a) for t, a in script])
    kw = {} if installed is None else {"installed": lambda: installed(clock.t - 1000.0)}
    mon.returned = False
    try:
        dm.run_monitor(mon, src, mono=clock, wait=src.wait, **kw)
        mon.returned = True
    except StopIteration:                    # plus rien de prévu ni d'événement : fin du scénario
        pass
    mon.times, mon.write_times, mon.log_times = times, write_times, log_times
    return mon, src, [json.loads(l) for l in lines]


def max_writes_per_window(times, window=600.0):
    """Plus grand nombre d'écritures dans une fenêtre glissante [t, t + window)."""
    return max((sum(1 for u in times if t <= u < t + window) for t in times), default=0)


def bounce(cage, on):
    """Action de scénario : rebond HDMI (sortie recréée au mode préféré 4K, ou retirée) + uevent DRM."""
    def act():
        cage.hotplug_on() if on else cage.hotplug_off()
        return True
    return act


class TestMonitorSimulatedHotplug(unittest.TestCase):
    def test_tv_off_then_on_restores_1080p60_once_without_touching_a_correct_mode(self):
        cage = FakeCage()

        def off():
            cage.hotplug_off()
            return True

        def on():
            cage.hotplug_on()
            return True
        mon, src, ev = simulate(cage, [(120.0, off), (8 * 3600.0, on)])
        events = [e["event"] for e in ev]
        # démarrage : mode déjà exact ⇒ lectures seulement ; extinction ⇒ sortie absente, rien ;
        # rallumage ⇒ UNE
        # écriture exacte, vérifiée ; contrôles suivants : rien
        self.assertEqual(cage.writes(), [["--output", "HDMI-A-1", "--mode", "1920x1080@60.000Hz"]])
        self.assertEqual(cage.current, (1920, 1080, 60000))
        self.assertIn("display_mode_ok", events)
        self.assertIn("display_mode_absent", events)
        applied = [e for e in ev if e["event"] == "display_mode_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual((applied[0]["current"], applied[0]["now"]), ("3840x2160@30.000", "1920x1080@60.000"))
        # au repos : attente SANS délai (aucune scrutation) entre les rafales ; rafales bornées
        self.assertIsNone(src.waits[-1])
        self.assertLessEqual(sum(1 for w in src.waits if w is None), 3)
        self.assertLessEqual(mon.checks, 3 * 8)
        self.assertEqual(events.count("display_mode_idle"), 3)                 # démarrage, extinction, rallumage

    def test_first_check_waits_for_wlroots_then_corrects(self):
        cage = FakeCage()

        def on():
            cage.hotplug_on()
            return True
        mon, src, ev = simulate(cage, [(100.0, on)])
        self.assertEqual(mon.times[0], 0.0)                                   # démarrage : contrôle immédiat
        after = [t for t in mon.times if t >= 100.0]
        self.assertEqual(after[0], 100.0 + dm.SETTLE_S)                       # rallumage : 1 s pour wlroots
        self.assertEqual(after, [101.0, 102.0, 104.0, 108.0, 116.0, 124.0])   # rafale bornée (30 s)
        self.assertEqual(len([e for e in ev if e["event"] == "display_mode_applied"]), 1)

    def test_already_correct_or_disconnected_bursts_do_nothing(self):
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(50.0, lambda: True), (500.0, lambda: True)])
        self.assertEqual(cage.writes(), [])
        cage = FakeCage(connected=False)
        mon, src, ev = simulate(cage, [(50.0, lambda: True)])
        self.assertEqual(cage.writes(), [])
        self.assertEqual({e["event"] for e in ev} - {"display_mode_watch", "display_mode_idle"},
                         {"display_mode_absent"})                   # une ligne d'état, pas une par contrôle

    def test_wayland_absent_at_start_is_retried_only_within_the_bounded_startup_burst(self):
        cage = FakeCage(current=SAMSUNG_MODES[0], wayland=False)

        def socket_up():
            cage.wayland = True
            return False                                       # aucun uevent : seul le délai de rafale rattrape
        mon, src, ev = simulate(cage, [(20.0, socket_up)])
        self.assertEqual(len(cage.writes()), 1)
        cage = FakeCage(wayland=False)
        mon, src, ev = simulate(cage, [])
        self.assertLessEqual(mon.checks, 12)                   # 60 s : 1+1+2+4+8+8… jamais plus
        self.assertIsNone(src.waits[-1])
        self.assertEqual([e["event"] for e in ev].count("display_mode_no_wayland"), 1)

    def test_compositor_that_keeps_reverting_is_never_fought_beyond_the_budgets(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True                               # « applique » mais le compositeur ignore
        mon, src, ev = simulate(cage, [])
        self.assertEqual(len(cage.writes()), dm.BURST_APPLIES)
        self.assertIsNone(src.waits[-1])                        # plafond de rafale seul : aucune reprise différée
        # tempête de branchements (HDMI qui rebondit 2×/s pendant 2 min) : jamais plus de 6 écritures / 10 min
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True
        mon, src, ev = simulate(cage, [(1.0 + i * 0.5, lambda: True) for i in range(240)])
        self.assertEqual(max_writes_per_window(mon.write_times), dm.WINDOW_APPLIES)
        # une seule vérification différée quand la fenêtre se libère : une écriture, puis repos (aucun duel)
        late = [t for t in mon.write_times if t > 200]
        self.assertEqual(len(late), 1)
        self.assertGreaterEqual(late[0], mon.write_times[0] + dm.WINDOW_S)
        self.assertEqual(len([t for t in mon.times if t > 200]), 1)
        self.assertIsNone(src.waits[-1])
        self.assertLessEqual(mon.checks, 130 + 10)              # au plus ~1 lecture/s pendant la tempête
        self.assertIn("display_mode_suppressed", [e["event"] for e in ev])
        # journal borné (+ une ligne « muted ») sur toute fenêtre de 10 min
        self.assertLessEqual(max_writes_per_window(mon.log_times, dm.LOG_WINDOW_S), dm.LOG_LINES + 1)

    def assert_converged_and_idle(self, cage, mon, src, ev, logged=True):
        self.assertEqual(cage.current, TARGET)                   # convergence finale exacte
        self.assertLessEqual(max_writes_per_window(mon.write_times), dm.WINDOW_APPLIES)
        self.assertIsNone(src.waits[-1])                         # puis repos sans délai (aucune scrutation)
        if logged:
            self.assertEqual(ev[-1]["event"], "display_mode_idle")

    def test_four_or_more_recreations_in_one_burst_still_end_at_1080p60(self):
        # revue 0.7.5 : 4 recréations HDMI dans une même rafale épuisaient le budget de 3 et laissaient la TV en 4K
        for n in (4, 5, 6):
            cage = FakeCage()
            mon, src, ev = simulate(cage, [(100.0 + 3.0 * i, bounce(cage, True)) for i in range(n)], log_lines=10 ** 6)
            self.assert_converged_and_idle(cage, mon, src, ev)
            self.assertEqual(len(mon.write_times), n, n)          # une écriture par recréation, jamais plus
            self.assertNotIn("display_mode_deferred", [e["event"] for e in ev])
        # 8 recréations en 21 s : 6 écritures (plafond 10 min), puis UNE vérification différée quand la fenêtre se
        # libère (pas avant), qui rétablit 1080p60 ; aucune lecture entre-temps
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(100.0 + 3.0 * i, bounce(cage, True)) for i in range(8)], log_lines=10 ** 6)
        self.assert_converged_and_idle(cage, mon, src, ev)
        self.assertEqual(len(mon.write_times), dm.WINDOW_APPLIES + 1)
        self.assertGreaterEqual(mon.write_times[-1], mon.write_times[0] + dm.WINDOW_S)
        self.assertLessEqual(mon.write_times[-1], mon.write_times[0] + dm.WINDOW_S + 2 * dm.SETTLE_S)
        self.assertEqual([t for t in mon.times if 160.0 < t < mon.write_times[-1]], [])
        deferred = [e for e in ev if e["event"] == "display_mode_deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual([e["event"] for e in ev[-3:]], ["display_mode_deferred", "display_mode_applied",
                                                         "display_mode_idle"])

    def test_hdmi_storm_ending_on_4k_converges_with_bounded_writes(self):
        # tempête : la TV rebondit (off/on) ~3×/s pendant 90 s et finit allumée au mode préféré 4K
        cage = FakeCage()
        script = [(10.0 + 0.37 * i, bounce(cage, i % 2 == 1)) for i in range(240)]
        mon, src, ev = simulate(cage, script)
        self.assert_converged_and_idle(cage, mon, src, ev, logged=False)
        self.assertEqual(max_writes_per_window(mon.write_times), dm.WINDOW_APPLIES)
        self.assertLessEqual(max_writes_per_window(mon.log_times, dm.LOG_WINDOW_S), dm.LOG_LINES + 1)
        self.assertEqual(len([t for t in mon.write_times if t > 200]), 1)     # reprise différée unique
        self.assertEqual(len([t for t in mon.times if t > 200]), 1)
        self.assertLessEqual(mon.checks, 160)
        # tempête de N rebonds quelconques (finie) : toujours 1080p60 à la fin, toujours ≤ 6 écritures / 10 min
        for n, step in ((2, 0.2), (7, 1.0), (13, 5.0), (31, 0.7), (9, 40.0)):
            cage = FakeCage()
            mon, src, ev = simulate(cage, [(5.0 + step * i, bounce(cage, True)) for i in range(n)], log_lines=10 ** 6)
            self.assert_converged_and_idle(cage, mon, src, ev)

    def test_global_cap_spans_separate_bursts_then_one_deferred_check(self):
        # 8 rallumages espacés de 80 s (extinction entre deux) : rafales séparées (chacune revient au repos), mais le
        # plafond global de 6 écritures / 10 min les couvre toutes ; la correction suivante attend que la fenêtre se
        # libère, par UNE vérification différée (pas une rafale de 10 min)
        cage = FakeCage()
        script = [(100.0 + 80.0 * i, bounce(cage, True)) for i in range(8)]
        script += [(140.0 + 80.0 * i, bounce(cage, False)) for i in range(7)]
        mon, src, ev = simulate(cage, script, log_lines=10 ** 6)
        self.assert_converged_and_idle(cage, mon, src, ev)
        w = mon.write_times
        events = [e["event"] for e in ev]
        self.assertGreaterEqual(events.count("display_mode_idle"), 15)                # rafales réellement séparées
        self.assertEqual(len(w), 7)
        self.assertEqual(w[:6], [101.0, 181.0, 261.0, 341.0, 421.0, 501.0])            # une par rallumage
        self.assertEqual(max_writes_per_window(w), dm.WINDOW_APPLIES)
        self.assertGreaterEqual(w[6], w[0] + dm.WINDOW_S)
        self.assertLessEqual(w[6], w[0] + dm.WINDOW_S + 2 * dm.SETTLE_S)
        # extinction (620 s) pendant l'attente différée : rafale ordinaire de 30 s, jamais prolongée jusqu'à l'échéance
        self.assertEqual([t for t in mon.times if 646.0 < t < 660.0], [])
        self.assertEqual([t for t in mon.times if 686.0 < t < w[6]], [])
        self.assertIn("display_mode_deferred", events)

    def test_burst_budget_resets_per_hotplug_and_never_fights_a_reverting_compositor(self):
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True                                # le compositeur n'accepte jamais l'écriture
        mon, src, ev = simulate(cage, [(300.0, lambda: True), (400.0, lambda: True)])
        w = mon.write_times
        self.assertEqual(len([t for t in w if t < 100]), dm.BURST_APPLIES)            # démarrage : 3
        self.assertEqual(len([t for t in w if 300 <= t < 340]), dm.BURST_APPLIES)     # nouveau branchement : 3 neufs
        self.assertEqual([t for t in w if 400 <= t < 600], [])                         # plafond 10 min atteint
        self.assertEqual(len([t for t in w if t >= 600]), 1)                           # UNE reprise différée
        self.assertEqual(max_writes_per_window(w), dm.WINDOW_APPLIES)
        self.assertIsNone(src.waits[-1])                                               # puis repos, aucun duel
        # branchement qui a lui-même épuisé ses 3 essais quand la fenêtre se remplit : aucune reprise différée (le
        # compositeur refuse : jamais de duel), même plus tard
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True
        mon, src, ev = simulate(cage, [(300.0, lambda: True)], log_lines=10 ** 6)
        self.assertEqual(len(mon.write_times), 2 * dm.BURST_APPLIES)
        self.assertNotIn("display_mode_deferred", [e["event"] for e in ev])
        self.assertIsNone(src.waits[-1])
        # budget par branchement, même au milieu d'une rafale : un rebond pendant la rafale rouvre 3 écritures
        cage = FakeCage(current=SAMSUNG_MODES[0])
        cage.ignore_writes = True
        mon, src, ev = simulate(cage, [(5.0, lambda: True)])
        self.assertEqual(len([t for t in mon.write_times if t < 100]), 2 * dm.BURST_APPLIES)

    def test_monitor_stops_itself_when_its_files_are_removed(self):
        # retour à ≤ 0.7.4 : l'ancien installateur retire le binaire mais n'arrête pas l'unité ; au premier réveil le
        # moniteur s'arrête proprement (code 0, Restart=on-failure ne le relance pas) sans plus rien écrire
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(200.0, lambda: True)], installed=lambda t: t < 205.0)
        self.assertTrue(mon.returned)                               # retiré en pleine rafale : arrêt au réveil suivant,
        self.assertFalse([t for t in mon.times if t >= 205.0])      # avant la vérification prévue
        self.assertEqual(ev[-1]["event"], "display_mode_uninstalled")
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(200.0, lambda: True), (300.0, bounce(cage, True))],
                                installed=lambda t: t < 250.0)
        self.assertTrue(mon.returned)
        self.assertEqual(cage.writes(), [])
        self.assertEqual(cage.current, SAMSUNG_MODES[0])            # plus aucune correction après retrait
        self.assertEqual(ev[-1]["event"], "display_mode_uninstalled")
        self.assertFalse([t for t in mon.times if t >= 250.0])
        # moniteur installé : aucun arrêt
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(300.0, bounce(cage, True))], installed=lambda t: True)
        self.assertFalse(mon.returned)
        self.assertEqual(len(cage.writes()), 1)

    def test_burst_extends_on_new_events_and_check_cadence_never_spins(self):
        cage = FakeCage()
        mon, src, ev = simulate(cage, [(5.0 + i * 0.01, lambda: True) for i in range(100)])
        self.assertTrue(all(w is None or w >= 0 for w in src.waits))
        self.assertEqual(cage.writes(), [])
        self.assertLessEqual(mon.checks, 15)                     # 100 événements en 1 s : quelques lectures


class TestLog(unittest.TestCase):
    def test_log_is_bounded_per_window(self):
        out = []
        clock = Clock()
        log = dm.Log(write=out.append, mono=clock, lines=3, window=60)
        for i in range(10):
            log("display_mode_ok", i=i)
        self.assertEqual(len(out), 4)
        self.assertEqual(json.loads(out[-1])["event"], "display_mode_log_muted")
        clock.t += 61
        log("display_mode_ok")
        self.assertEqual(len(out), 5)


class TestRealTimeNoPolling(unittest.TestCase):
    """Vrai `select` sur un vrai socket : au repos, le thread dort sans délai ; un uevent simulé le réveille."""

    def test_idle_blocks_then_reacts_to_simulated_hotplug(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        cage = FakeCage()
        waits = []
        stop = threading.Event()

        def wait(r, w, x, t):
            waits.append(t)
            return dm.select.select(r, w, x, t)
        lines, log = logs()
        mon = dm.Monitor(check_for(cage), log, "1920x1080@60.000", "HDMI-A-1")
        with mock.patch.multiple(dm, SETTLE_S=0.05, BURST_S=0.4, STARTUP_BURST_S=0.4, MAX_INTERVAL_S=0.1):
            th = threading.Thread(target=dm.run_monitor, args=(mon, dm.UeventSource(a)),
                                  kwargs={"wait": wait, "stop": stop.is_set}, daemon=True)
            th.start()
            time.sleep(1.0)
            n_idle = len(waits)
            time.sleep(1.0)
            self.assertLessEqual(len(waits) - n_idle, 0)                 # 1 s de repos : AUCUN tour de boucle
            self.assertIsNone(waits[-1])
            checks_before = mon.checks
            cage.hotplug_on()
            b.send(uevent(subsystem="usb"))                              # bruit : réveil mais aucune rafale
            time.sleep(0.3)
            self.assertEqual(mon.checks, checks_before)
            b.send(uevent())
            deadline = time.monotonic() + 3
            while cage.current != (1920, 1080, 60000) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(cage.current, (1920, 1080, 60000))
            time.sleep(0.8)
            stop.set()
            b.send(uevent(subsystem="usb"))                              # réveil final
            th.join(3)
        self.assertFalse(th.is_alive())
        self.assertEqual(len(cage.writes()), 1)


# --------------------------------------------------------------------------- points d'entrée
class TestEntryPoints(unittest.TestCase):
    def test_apply_is_bounded_and_never_blocks_the_kiosk(self):
        cage = FakeCage(wayland=False)
        slept = []
        out, log = logs()
        self.assertEqual(dm.cmd_apply(check_for(cage), log, "t", "o", sleep=slept.append), 0)
        self.assertEqual(sum(slept), 3.5)
        self.assertEqual(json.loads(out[-1])["event"], "display_mode_no_wayland")
        cage = FakeCage(current=SAMSUNG_MODES[0])
        slept = []
        self.assertEqual(dm.cmd_apply(check_for(cage), log, "t", "o", sleep=slept.append), 0)
        self.assertEqual((slept, len(cage.writes())), ([], 1))
        self.assertEqual(json.loads(out[-1])["phase"], "kiosk_start")

    def test_cli_status_usage_and_installed_launcher(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        env = dict(os.environ, XDG_RUNTIME_DIR=d, OPENTV_HDMI_MODE="1920x1080")
        env.pop("WAYLAND_DISPLAY", None)
        p = subprocess.run([sys.executable, "-I", BIN, "status"], capture_output=True, timeout=30, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"result": "no_wayland", "output": "HDMI-A-1",
                                                "target": "1920x1080@60.000"})
        p = subprocess.run([sys.executable, "-I", BIN, "apply"], capture_output=True, timeout=30,
                           env=dict(env, OPENTV_HDMI_MODE="bogus"))
        self.assertEqual(p.returncode, 0)                                # jamais bloquant
        self.assertIn(b"display_mode_config_mode_invalid", p.stderr)
        for argv in ([], ["nope"], ["status", "x"]):
            self.assertEqual(subprocess.run([sys.executable, "-I", BIN] + argv, capture_output=True,
                                            timeout=30).returncode, 2)

    def test_installed_check_follows_the_module_and_launcher_files(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        a, b = os.path.join(d, "displaymode.py"), os.path.join(d, "opentv-display-mode")
        for f in (a, b):
            with open(f, "w"):
                pass
        alive = dm.installed_check([a, b])
        self.assertTrue(alive())
        os.unlink(b)
        self.assertFalse(alive())
        self.assertIn(os.path.abspath(dm.__file__), dm.monitor_files())
        self.assertIn(os.path.abspath(BIN), dm.monitor_files(BIN))

    def test_main_monitor_wires_the_uninstall_stop(self):
        class Src:
            closed = False

            def close(self):
                self.closed = True
        src = Src()
        seen = {}

        def fake_run(mon, source, **kw):
            seen.update(kw)
            return 0
        with mock.patch.object(dm, "UeventSource", return_value=src), mock.patch.object(dm, "run_monitor", fake_run):
            self.assertEqual(dm.main(["monitor"], env={}), 0)
        self.assertTrue(src.closed)
        self.assertTrue(seen["installed"]())
        with mock.patch.object(dm.os.path, "isfile", lambda p: p != os.path.abspath(dm.__file__)):
            self.assertFalse(seen["installed"]())

    @unittest.skipIf(hasattr(socket, "AF_NETLINK"), "Linux : netlink disponible")
    def test_monitor_without_netlink_is_a_truthful_78(self):
        self.assertEqual(dm.main(["monitor"], env={}), dm.EX_UNAVAILABLE)


# --------------------------------------------------------------------------- unités, ouvreur, installation
class TestUnitAndKiosk(unittest.TestCase):
    def test_dedicated_unit_is_unprivileged_sandboxed_and_event_driven(self):
        cp = unit(UNIT)
        u, s = cp["Unit"], cp["Service"]
        self.assertEqual(cp.sections(), ["Unit", "Service"])           # pas d'[Install] : tirée par le kiosque
        self.assertEqual((u["After"], u["PartOf"]), ("opentv-kiosk.service", "opentv-kiosk.service"))
        for k in ("Requires", "BindsTo", "Before", "OnFailure", "Conflicts"):
            self.assertNotIn(k, u)
        self.assertEqual((s["Type"], s["User"], s["Group"]), ("simple", "opentv-kiosk", "opentv-kiosk"))
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-display-mode monitor")
        for k in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload", "ExecCondition", "SupplementaryGroups",
                  "TTYPath", "TTYReset", "TTYVHangup", "TTYVTDisallocate", "PAMName", "AmbientCapabilities=x",
                  "ReadWritePaths", "PrivateNetwork", "DeviceAllow"):
            self.assertNotIn(k, s, k)
        self.assertEqual(s["StandardInput"], "null")
        self.assertEqual((s["Restart"], s["RestartPreventExitStatus"]), ("on-failure", "78"))
        self.assertEqual(s["CapabilityBoundingSet"], "")
        self.assertEqual(s["RestrictAddressFamilies"].split(), ["AF_UNIX", "AF_NETLINK"])
        self.assertEqual(s["IPAddressDeny"], "any")
        # /run/user (socket Wayland de Cage) doit rester joignable : ProtectHome=yes le masquerait
        self.assertEqual(s["ProtectHome"], "read-only")
        for k in ("NoNewPrivileges", "PrivateTmp", "PrivateDevices", "ProtectKernelTunables",
                  "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups", "ProtectClock",
                  "RestrictNamespaces", "RestrictSUIDSGID", "MemoryDenyWriteExecute", "LockPersonality"):
            self.assertEqual(s[k], "yes", k)
        self.assertEqual((s["ProtectSystem"], s["DevicePolicy"]), ("strict", "closed"))
        text = read(os.path.join(SYSTEMD, UNIT))
        inacc = " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()
        self.assertEqual(sorted(inacc), ["-/etc/opentv/tmdb", "-/var/lib/opentv/canvas"])
        self.assertEqual(dm.EX_UNAVAILABLE, 78)
        # jamais de minuterie de scrutation
        for name in os.listdir(SYSTEMD):
            if name.endswith(".timer"):
                self.assertNotIn("display-mode", read(os.path.join(SYSTEMD, name)), name)

    def test_kiosk_restarts_can_never_leave_the_monitor_start_limited(self):
        # PartOf= : chaque redémarrage du kiosque (Restart=always) redémarre aussi le moniteur ; une limite de démarrages
        # (5/600 s) le laissait « start-limit-hit » après 5 redémarrages du kiosque, donc plus de correction HDMI
        u, s = unit(UNIT)["Unit"], unit(UNIT)["Service"]
        self.assertEqual(u["StartLimitIntervalSec"], "0")
        self.assertNotIn("StartLimitBurst", u)
        self.assertNotIn("StartLimitAction", u)
        self.assertEqual((s["Restart"], s["RestartSec"], s["RestartPreventExitStatus"]), ("on-failure", "10", "78"))
        self.assertEqual(unit("opentv-kiosk.service")["Unit"]["StartLimitIntervalSec"], "0")

    def test_kiosk_pulls_the_monitor_without_side_commands_or_plymouth_changes(self):
        k = unit("opentv-kiosk.service")
        u, s = k["Unit"], k["Service"]
        self.assertIn(UNIT, u["Wants"].split())
        self.assertNotIn(UNIT, u["After"].split())                       # le moniteur suit le kiosque
        for key in ("ExecStartPre", "ExecStartPost"):
            self.assertNotIn(key, s)
        self.assertEqual(s["ExecStart"], "/usr/bin/cage -d -- /opt/opentv/bin/opentv-kiosk-browser")
        self.assertEqual((s["TTYReset"], s["TTYVHangup"], s["TTYVTDisallocate"]), ("no", "no", "no"))
        mu = unit(UNIT)["Unit"]
        for key in ("After", "Wants", "Requires", "Before"):
            self.assertNotIn("plymouth", mu.get(key, ""), key)

    def test_opener_uses_the_exact_helper_never_an_ambiguous_wlr_randr_mode(self):
        code = code_only(read(OPENER))
        self.assertNotIn("wlr-randr", code)
        self.assertNotRegex(code, r"--mode")
        self.assertIn('timeout -k 2 15 /usr/bin/python3 -I "$DISPLAY_MODE" apply', code)
        self.assertLess(code.index("apply"), code.index("exec \"$CHROMIUM_BIN\""))   # avant Chromium
        p = subprocess.run(["bash", "-n", OPENER], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue("#OPENTV_HDMI_MODE=1920x1080@60\n" in read(os.path.join(PI4, "opentv-pi.env.example")))

    def test_install_restart_and_uninstall_wire_the_monitor(self):
        inst, unin = read(INSTALL), read(UNINSTALL)
        loop = re.search(r"for u in (opentv\.service [^;]*); do", inst).group(1)
        self.assertIn(UNIT, loop.split())
        self.assertRegex(inst, r"opentv-kiosk-browser opentv-display-mode ")
        enable = re.search(r"post systemctl enable ((?:[^\n]*\\\n)*[^\n]*)", inst).group(1)
        self.assertNotIn(UNIT, enable)                                    # aucune [Install]
        restart = re.search(r"post systemctl restart ((?:[^\n]*\\\n)*[^\n]*)", inst).group(1)
        self.assertLess(restart.index("opentv-kiosk.service"), restart.index(UNIT))
        # mise à jour depuis un 0.7.5 resté « failed »/start-limit-hit : effacé avant le redémarrage
        reset = re.search(r"post systemctl reset-failed ([^\n]*)", inst)
        self.assertIn(UNIT, reset.group(1).split())
        self.assertLess(reset.start(), inst.index("post systemctl restart "))
        disable = re.search(r"host systemctl disable --now ((?:[^\n]*\\\n)*[^\n]*)", unin).group(1)
        self.assertIn(UNIT, disable)

    def test_rootfs_install_manifest_and_uninstall(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        env = dict(os.environ, LC_ALL="C")
        env.pop("OPENTV_ROOT", None)
        p = subprocess.run(["bash", INSTALL, "--root", r, "--skip-preflight"], capture_output=True, timeout=300,
                           env=env)
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-2000:] + p.stderr.decode())
        for rel, mode in (("opt/opentv/bin/opentv-display-mode", 0o755),
                          ("etc/systemd/system/" + UNIT, 0o644), ("opt/opentv/lib/opentv_pi/displaymode.py", 0o644)):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(r, rel)).st_mode), mode, rel)
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        for rel in ("/opt/opentv/bin/opentv-display-mode", "/etc/systemd/system/" + UNIT,
                    "/opt/opentv/lib/opentv_pi/displaymode.py"):
            self.assertIn("  %s\n" % rel, man)
        self.assertTrue(health.verify_manifest(r)["ok"])
        q = subprocess.run([sys.executable, "-I", os.path.join(r, "opt/opentv/bin/opentv-display-mode"), "status"],
                           capture_output=True, timeout=30, env=dict(os.environ, XDG_RUNTIME_DIR=r))
        self.assertEqual(json.loads(q.stdout)["result"], "no_wayland")        # lanceur installé autonome
        p = subprocess.run(["bash", UNINSTALL, "--root", r], capture_output=True, timeout=300, env=env)
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-2000:] + p.stderr.decode())
        out = p.stdout.decode()
        self.assertIn(UNIT, out[out.index("disable --now"):])
        for rel in ("opt/opentv", "etc/systemd/system/" + UNIT):
            self.assertFalse(os.path.lexists(os.path.join(r, rel)), rel)


class TestHealthAndBootCheck(unittest.TestCase):
    def test_health_warns_when_kiosk_runs_without_the_monitor(self):
        self.assertIn(UNIT, health.UNITS_OPTIONAL)
        cfg = mock.Mock(tv_port=8080, cec_bootstrap=False, cec_return_recovery=False)

        def runner(states):
            def run(argv, timeout):
                if argv[:2] == ["systemctl", "is-active"]:
                    return 0, states.get(argv[2], "active") + "\n"
                return 1, ""
            return run
        with mock.patch.object(health.ratings, "health_view", return_value={}), \
                mock.patch.object(health.netstate, "snapshot", return_value={"mode": "client"}):
            ok = health.run(cfg, root="/nonexistent", runner=runner({}), check_http=False)
            self.assertNotIn("display_mode_inactive", ok["warnings"])
            bad = health.run(cfg, root="/nonexistent", runner=runner({UNIT: "failed"}), check_http=False)
            self.assertIn("display_mode_inactive", bad["warnings"])
            self.assertEqual(bad["units"][UNIT], "failed")
            off = health.run(cfg, root="/nonexistent", runner=runner({UNIT: "inactive",
                                                                       "opentv-kiosk.service": "inactive"}),
                             check_http=False)
            self.assertNotIn("display_mode_inactive", off["warnings"])     # kiosque arrêté : rien à surveiller

    def boot_check(self, active, status):
        from tests.test_install import TestSplashAndBootUnits as T
        t = T("test_boot_check_passes_on_expected_state")
        props = dict(T.GOOD)
        props[(UNIT, "ActiveState")] = active
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        cmd = os.path.join(d, "status")
        with open(cmd, "w") as f:
            f.write("#!/bin/sh\nprintf '%%s\\n' '%s'\n" % json.dumps(status, sort_keys=True))
        os.chmod(cmd, 0o755)
        try:
            with mock.patch.dict(os.environ, {"OPENTV_DISPLAY_STATUS_CMD": cmd}):
                p = t.run_check(props, T.GOOD_LINE)
        finally:
            t.doCleanups()
        return p.returncode, p.stdout.decode()

    def test_boot_check_reads_the_live_mode(self):
        if os.geteuid() == 0:
            self.skipTest("exécuté en root")
        ok = {"result": "ok", "current": "1920x1080@60.000", "target": "1920x1080@60.000", "output": "HDMI-A-1"}
        rc, out = self.boot_check("active", ok)
        self.assertEqual(rc, 0, out)
        self.assertIn("OK     mode HDMI : moniteur des branchements actif", out)
        self.assertIn("OK     mode HDMI : 1920x1080@60.000 effectif sur HDMI-A-1", out)
        rc, out = self.boot_check("active", dict(ok, result="drift", current="3840x2160@30.000"))
        self.assertEqual(rc, 1, out)
        self.assertIn("ÉCHEC  mode HDMI : courant 3840x2160@30.000 au lieu de 1920x1080@60.000", out)
        rc, out = self.boot_check("active", {"result": "absent", "output": "HDMI-A-1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("info   mode HDMI : sortie HDMI-A-1 absente", out)
        rc, out = self.boot_check("failed", ok)
        self.assertEqual(rc, 1, out)
        self.assertIn("ÉCHEC  mode HDMI : opentv-display-mode inactif", out)


class TestBootCheckEnvironment(unittest.TestCase):
    """La cible lue par boot-check suit EnvironmentFile= de systemd (guillemets simples/doubles) et le statut ne part
    jamais avec le XDG_RUNTIME_DIR/WAYLAND_DISPLAY de root (/run/user/0 : aucun socket Cage)."""

    def run_boot_check(self, env_text):
        from tests.test_install import TestSplashAndBootUnits as T
        if os.geteuid() == 0:
            self.skipTest("exécuté en root")
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        envf, seen = os.path.join(d, "opentv.env"), os.path.join(d, "seen")
        with open(envf, "w") as f:
            f.write(env_text)
        cmd = os.path.join(d, "status")
        with open(cmd, "w") as f:
            f.write("#!/bin/sh\nprintf '%%s|%%s|%%s|%%s\\n' \"$OPENTV_HDMI_OUTPUT\" \"$OPENTV_HDMI_MODE\" "
                    "\"${XDG_RUNTIME_DIR-unset}\" \"${WAYLAND_DISPLAY-unset}\" > %s\n"
                    "printf '%%s\\n' '{\"result\": \"ok\", \"current\": \"x\", \"output\": \"o\", \"target\": \"t\"}'\n" % seen)
        os.chmod(cmd, 0o755)
        t = T("test_boot_check_passes_on_expected_state")
        props = dict(T.GOOD)
        props[(UNIT, "ActiveState")] = "active"
        try:
            with mock.patch.dict(os.environ, {"OPENTV_DISPLAY_STATUS_CMD": cmd, "OPENTV_ENV_FILE": envf,
                                              "XDG_RUNTIME_DIR": "/run/user/0", "WAYLAND_DISPLAY": "wayland-0"}):
                p = t.run_check(props, T.GOOD_LINE)
        finally:
            t.doCleanups()
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        return read(seen).strip().split("|")

    def test_env_values_are_unquoted_like_systemd_and_root_session_is_dropped(self):
        got = self.run_boot_check("# OPENTV_HDMI_MODE=3840x2160@30\nOPENTV_HDMI_MODE=1920x1080@60\n"
                                  "OPENTV_HDMI_MODE='1280x720@50'\r\n OPENTV_HDMI_OUTPUT = \"HDMI-A-2\"  \n")
        self.assertEqual(got, ["HDMI-A-2", "1280x720@50", "unset", "unset"])
        self.assertEqual(self.run_boot_check("OPENTV_HDMI_MODE=\"1920x1080@59.94\"\n"),
                         ["", "1920x1080@59.94", "unset", "unset"])
        self.assertEqual(self.run_boot_check(""), ["", "", "unset", "unset"])

    def test_root_branch_drops_the_root_session_before_runuser(self):
        code = code_only(read(BOOT_CHECK))
        self.assertRegex(code, r"/usr/bin/env -u XDG_RUNTIME_DIR -u WAYLAND_DISPLAY \"\$\{dm_cmd\[@\]\}\"")
        root = code[code.index('if [ "${EUID:-0}" -eq 0 ]; then\n  dm_cmd='):]
        root = root[:root.index("elif")]
        self.assertIn("runuser -u opentv-kiosk -- /usr/bin/env -u XDG_RUNTIME_DIR -u WAYLAND_DISPLAY", root)
        self.assertIn("/usr/bin/python3 -I /opt/opentv/bin/opentv-display-mode status", root)
        self.assertNotIn("sudo -u opentv-kiosk", code)
        p = subprocess.run(["bash", "-n", BOOT_CHECK], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)


class TestReleaseDocs(unittest.TestCase):
    def test_version_and_docs(self):
        self.assertGreaterEqual(tuple(int(x) for x in VERSION.split(".")), (0, 7, 5))   # 0.7.6 : suiveur CEC
        readme = read(os.path.join(PI4, "README.md"))
        for need in ("opentv-display-mode.service", "1920x1080@60", "3840x2160@30", "uevent", "OPENTV_HDMI_MODE",
                     "display_mode_applied", "display_mode_inactive", "PartOf=", "0.7.5"):
            self.assertTrue(need in readme, need)
        gate = next(l for l in readme.splitlines() if l.startswith("| **Mode HDMI après rallumage"))
        for need in ("wlr-randr", "display_mode_applied", "NRestarts", "opentv-boot-check"):
            self.assertIn(need, gate)
        # diagnostic manuel : même cible que l'unité (EnvironmentFile=, donc OPENTV_HDMI_MODE), jamais sudo -u (qui
        # perd OPENTV_HDMI_MODE et vérifierait le défaut)
        self.assertFalse(re.search(r"sudo -u opentv-kiosk [^`]*opentv-display-mode", readme))
        diag = ("sudo systemd-run --quiet --wait --pipe -p User=opentv-kiosk -p EnvironmentFile=-/etc/opentv/opentv.env "
                "/opt/opentv/bin/opentv-display-mode status")
        self.assertTrue(diag in readme)
        self.assertIn(diag, gate)
        for need in ("display_mode_deferred", "display_mode_uninstalled", "StartLimitIntervalSec=0"):
            self.assertTrue(need in readme, need)


if __name__ == "__main__":
    unittest.main()
