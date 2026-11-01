"""Régression terrain (Pi réel + Samsung, 2026-10-30) : après l'application native YouTube puis retour visible sur
OpenTV, plus aucune touche de la télécommande Samsung n'atteignait OpenTV.

Capture réelle (`cec-ctl -m`) : avant YouTube, TV → Playback Device 1 USER_CONTROL down/up (devenus événements Linux).
Entrée dans YouTube : la TV diffuse ACTIVE_SOURCE phys-addr 0.0.0.0. Sortie (21:49:35) : la TV diffuse ROUTING_CHANGE
0.0.0.0 → 1.0.0.0 puis SET_STREAM_PATH 1.0.0.0, puis GIVE_DEVICE_POWER_STATUS toutes les 5 s ; plus aucun USER_CONTROL.
`sudo opentv-pi cec --activate` (question d'alimentation, <Active Source>, <Menu Status>, TV allumée seulement) a
rétabli la télécommande (confirmé physiquement par Alex).

Contrat (cecreturn.py, opentv-cec-return.service) : après la séquence EXACTE émise par la TV et visant cette boîte
(ACTIVE_SOURCE 0.0.0.0 de la TV, puis ROUTING_CHANGE 0.0.0.0 → notre adresse physique, puis SET_STREAM_PATH notre
adresse, fenêtres bornées), la même annonce sûre est rejouée une fois, sous le verrou existant, en quelques secondes.
Jamais de réclamation périodique, de réveil, de <Image View On>/<Text View On>, de réaction à une autre source, à
Samsung Home seul, ni à un redémarrage ; jamais de touche lue, consommée ou synthétisée ; témoin de démarrage intact.
"""
import configparser
import contextlib
import functools
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests._common import PI4, ROOT
from tests.test_install import INSTALL, UNINSTALL, sh, tree
from tests.test_remote import ACTIVE_SOURCE_TX, BOOT_A, MENU_STATUS_TX, OURS_TMPL, Base, FakeBus, make_root, tmpdir
from opentv_pi import VERSION, cecboot, cecreturn, cli, config, health, ratings

WAKE = ("--image-view-on", "--text-view-on", "--standby", "--set-power-state", "--user-control-pressed",
        "--user-control-released", "--wake")
ANNOUNCE_SEQ = ["--give-device-power-status", "--active-source", "--menu-status"]
UNIT = "opentv-cec-return.service"
NAMES = {0: "TV", 4: "Playback Device 1", 5: "Audio System", 8: "Playback Device 2", 15: "all"}


# --------------------------------------------------------------------------- sortie cec-ctl 1.22.1 (-s -r -m)
def raw_line(data):
    """`\\tRaw:` exactement comme print_bytes() de cec-ctl : octets hexadécimaux puis ASCII imprimable."""
    return "\tRaw:" + "".join(" 0x%02x" % b for b in data) + " (" + "".join(
        chr(b) if 32 <= b <= 127 else " " for b in data) + ")"


def pa_bytes(pa):
    a, b, c, d = (int(x, 16) for x in pa.split("."))
    return [(a << 4) | b, (c << 4) | d]


def frame(op, name, args=(), params=(), src=0, dst=15, tx=False):
    head = "%s %s to %s (%d to %d): %s (0x%02x)%s" % ("Transmitted by" if tx else "Received from", NAMES[src],
                                                      NAMES[dst], src, dst, name, op, ":" if args else "")
    return [head] + ["\t%s: %s" % kv for kv in args] + [raw_line([(src << 4) | dst, op] + list(params))]


def active_source(pa="0.0.0.0", src=0, dst=15, tx=False):
    return frame(0x82, "ACTIVE_SOURCE", [("phys-addr", pa)], pa_bytes(pa), src, dst, tx)


def routing_change(orig="0.0.0.0", new="1.0.0.0", src=0):
    return frame(0x80, "ROUTING_CHANGE", [("orig-phys-addr", orig), ("new-phys-addr", new)],
                 pa_bytes(orig) + pa_bytes(new), src)


def stream_path(pa="1.0.0.0", src=0):
    return frame(0x86, "SET_STREAM_PATH", [("phys-addr", pa)], pa_bytes(pa), src)


def power_poll(dst=4):
    return frame(0x8f, "GIVE_DEVICE_POWER_STATUS", (), (), 0, dst)


def key(cmd=0x02, name="down", dst=4):
    return frame(0x44, "USER_CONTROL_PRESSED", [("ui-cmd", "%s (0x%02x)" % (name, cmd))], [cmd], 0, dst) + \
        frame(0x45, "USER_CONTROL_RELEASED", (), (), 0, dst)


def state_event(pa="1.0.0.0", mask=0x0010, initial=True):
    return ["", "%sEvent: State Change: PA: %s, LA mask: 0x%04x, Conn Info: yes" % (
        "Initial " if initial else "1234.567890s: ", pa, mask)]


def at(dt, lines):
    """Lignes émises `dt` secondes après la précédente (la première porte le délai, les suivantes arrivent ensemble)."""
    return [(dt, lines[0])] + [(0.0, line) for line in lines[1:]]


def captured(pa="1.0.0.0", youtube_s=600.0, own=None):
    """Séquence réelle du 2026-10-30 : touches, YouTube (ACTIVE_SOURCE 0.0.0.0), retour 21:49:35, sondages 5 s."""
    s = at(0.0, state_event(own or pa)) + at(1.0, key()) + at(0.4, key(0x01, "up"))
    s += at(5.0, active_source("0.0.0.0"))
    for _ in range(3):
        s += at(5.0, power_poll())
    s += at(youtube_s, routing_change("0.0.0.0", pa)) + at(0.05, stream_path(pa))
    for _ in range(4):
        s += at(5.0, power_poll())
    return s


class FakeLines:
    """Sortie de `cec-ctl -m` simulée : lignes horodatées sur l'horloge partagée, puis fin (ou silence) ; `error` /
    `exit_status` : diagnostic de fin (stderr classé, code de sortie) comme PopenLines.diagnosis()."""

    def __init__(self, clock, script, end="eof", error=None, exit_status=0):
        self.clock, self.script, self.end = clock, list(script), end
        self.error, self.exit_status = error, exit_status
        self.due = clock.t + (self.script[0][0] if self.script else 0.0)
        self.closed = False
        self.reads = 0
        self.delivered = []

    def readline(self, timeout):
        self.reads += 1
        assert self.reads < 100000, "boucle de lecture non bornée"
        if not self.script:
            if self.end == "silence" and timeout > 0:
                self.clock.t += timeout
                return "timeout", None
            return self.end if self.end != "silence" else "eof", None
        if self.due > self.clock.t + timeout:
            self.clock.t += timeout
            return "timeout", None
        self.clock.t = max(self.clock.t, self.due)
        _, line = self.script.pop(0)
        self.delivered.append((self.clock.t, line))
        if self.script:
            self.due = self.clock.t + self.script[0][0]
        return "line", line

    def diagnosis(self):
        return {"error": self.error, "exit_status": self.exit_status}

    def close(self):
        self.closed = True


def ready_first(script, pa="1.0.0.0", mask=0x0010):
    """Préfixe l'événement initial attendu (moniteur prêt) sauf s'il est déjà là."""
    if any("Initial Event" in line for _, line in script[:2]):
        return list(script)
    return at(0, state_event(pa, mask)) + list(script)


# `cec-ctl -d <dev> -s -t 0 -P` (cec-ctl 1.22.1, sans -v) : acquitté par la TV = en-tête, POLL, ligne Sequence et
# rien d'autre ; non acquitté = même sortie + statut (tx_status2s), code de sortie 0 dans les deux cas.
POLL_OK = "\nTransmit from Playback Device 1 to TV (4 to 0):\nPOLL\n\tSequence: 117 Tx Timestamp: 8123.456s\n"
POLL_NACK = POLL_OK + "\tTx, Not Acknowledged (1), Max Retries\n"
POLL_ARGV_OPTS = ["-s", "-t", "0", "-P"]
POLL_CAP = 2000                       # > 1 h de maintien (≈ 487 sondes) : au-delà, boucle de POLL non bornée


@contextlib.contextmanager
def fail_fast(seconds):
    """Garde-fou réel (SIGALRM) : un mutant ou une régression qui boucle sans avancer l'horloge simulée échoue en
    quelques secondes au lieu de pendre la suite."""
    def boom(*_):
        raise AssertionError("test bloqué > %.0f s : boucle de POLL/attente non bornée ?" % seconds)
    prev = signal.signal(signal.SIGALRM, boom)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def bounded(seconds=60.0):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            with fail_fast(seconds):
                return fn(self, *a, **kw)
        return wrapper
    return deco


class RecBus(FakeBus):
    """cec-ctl simulé + instant de chaque émission (pour mesurer le délai de reprise) ; <Poll> (`-P`) répondu par
    `poll_reply` (code, sortie) ou une fonction (argv) -> (code, sortie) ; `mask` facultatif par adaptateur."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.at = []
        self.poll_reply = (0, POLL_OK)
        self.poll_spans = []          # (début, fin) de chaque cec-ctl POLL : l'émission sur le bus est entre les deux

    def render(self, dev):
        a = self.ad[dev]
        if a["state"] == "ours" and ("osd" in a or "ptype" in a):    # même PA/masque, identité différente
            out = OURS_TMPL.format(driver=a.get("driver", "vc4_hdmi"), adapter="vc4-hdmi-" + dev[-1], pa=a["pa"],
                                   mask="0x0010", osd=a.get("osd", "OpenTV"), ptype=a.get("ptype", "Playback"))
        else:
            out = super().render(dev)
        m = a.get("mask")
        return out.replace("Logical Address Mask       : 0x0010", "Logical Address Mask       : " + m) if m else out

    def __call__(self, argv, timeout):
        self.at.append((self.clock.t, list(argv)))
        if "-P" in argv:
            # fail-fast : un mutant qui sonderait sans fin échoue au lieu de pendre
            assert len(self.poll_spans) < POLL_CAP, "POLL non borné"
            self.calls.append((list(argv), timeout))
            t0 = self.clock.t
            self.clock.t += 0.05
            self.poll_spans.append((t0, self.clock.t))
            if argv[2] not in self.ad or self.ad[argv[2]].get("unreadable"):
                return 1, "open failed"
            r = self.poll_reply
            return r(argv) if callable(r) else r
        return super().__call__(argv, timeout)

    def polls(self):
        return [t for t, a in self.at if "-P" in a]


def module_source():
    with open(os.path.join(PI4, "opentv_pi", "cecreturn.py"), encoding="utf-8") as f:
        return f.read()


def wake_args(bus):
    return [o for a, _ in bus.calls for o in a if o in WAKE]


class RBase(Base):
    def setUp(self):
        super().setUp()
        self.root = make_root(self)
        self.bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"},
                                       "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        self.logs, self.spawned, self.sleeps = [], [], []

    def sleep(self, s):
        self.sleeps.append(s)
        self.clock.t += s

    def monitor(self, scripts=(), bus=None, **kw):
        it = iter(scripts)

        def spawn(argv):
            self.spawned.append(list(argv))
            src = next(it)
            if isinstance(src, Exception):
                raise src
            if isinstance(src, FakeLines):
                return src
            return FakeLines(self.clock, src)
        kw.setdefault("state_dir", self.state)
        return cecreturn.Monitor(self.root, runner=bus or self.bus, spawn=spawn, mono=self.clock,
                                 sleep=self.sleep, log=self.logs.append, **kw)

    def solo(self, **kw):
        """Moniteur appelé hors de watch() (tests unitaires de recover/poll) : relecture finale neutre installée à la
        main (watch() l'installe en production ; sans elle, recover/poll refusent : `drain_unavailable`)."""
        mon = self.monitor(**kw)
        mon._before_tx = lambda: None
        return mon

    def watch(self, script, dev="/dev/cec0", pa="1.0.0.0", mask=0x0010, mon=None, end="eof", ready=True):
        """Un moniteur ; `ready` : l'événement initial attendu est préfixé. -> (moniteur, raison) ; self.ready."""
        self.src = FakeLines(self.clock, ready_first(script, pa, mask) if ready else script, end)
        mon = mon or self.monitor([self.src])
        why, self.ready = mon.watch(dev, pa, mask)
        return mon, why

    def recoveries(self):
        return [e for e in self.logs if e.get("event") == "cec_return_recovery"]

    def emitted(self, bus=None):
        return [o for o in (bus or self.bus).opts() if o in ANNOUNCE_SEQ + list(WAKE)]


# --------------------------------------------------------------------------- séquence exacte
class TestCapturedReturn(RBase):
    def test_exact_captured_sequence_recovers_once_within_seconds(self):
        mon, why = self.watch(captured())
        self.assertEqual(why, "eof")
        rec = self.recoveries()
        self.assertEqual(len(rec), 1, self.logs)
        self.assertEqual((rec[0]["result"], rec[0]["device"], rec[0]["physical_address"]),
                         ("announced", "/dev/cec0", "1.0.0.0"))
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)          # même annonce sûre que `cec --activate`
        self.assertEqual(self.bus.transmits(), [
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--give-device-power-status"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "--active-source", "phys-addr=1.0.0.0"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--menu-status", "menu-state=activated"]])
        self.assertEqual(wake_args(self.bus), [])
        self.assertEqual(self.spawned, [["cec-ctl", "-d", "/dev/cec0", "-s", "-r", "-m"]])
        # « en quelques secondes » : l'<Active Source> part ≤ 5 s après SET_STREAM_PATH
        t_stream = next(t for t, line in self.src.delivered if line == stream_path()[0])
        t_as = next(t for t, a in self.bus.at if "--active-source" in a)
        self.assertGreaterEqual(t_as - t_stream, cecreturn.SETTLE_S)
        self.assertLessEqual(t_as - t_stream, 5.0)

    def test_dynamic_adapter_and_physical_address(self):
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                  "/dev/cec1": {"pa": "2.1.0.0", "state": "ours"}})
        mon = self.monitor([captured("2.1.0.0")], bus=bus)
        self.assertEqual(mon.discover(), ("/dev/cec1", "2.1.0.0", 0x0010))
        self.assertEqual(mon.watch("/dev/cec1", "2.1.0.0", 0x0010), ("eof", True))
        self.assertIn(["cec-ctl", "-d", "/dev/cec1", "-s", "--active-source", "phys-addr=2.1.0.0"], bus.transmits())
        # la séquence visant 1.0.0.0 n'appartient pas à une boîte en 2.1.0.0
        bus2 = RecBus(self.clock, {"/dev/cec1": {"pa": "2.1.0.0", "state": "ours"}})
        mon2 = self.monitor([captured("1.0.0.0", own="2.1.0.0")], bus=bus2)
        self.assertEqual(mon2.watch("/dev/cec1", "2.1.0.0", 0x0010), ("eof", True))
        self.assertEqual(bus2.transmits(), [])
        src = module_source()
        for literal in ("1.0.0.0", "/dev/cec0", "event3", "/dev/input"):
            self.assertNotIn(literal, src)

    def test_restart_after_youtube_never_reannounces(self):
        """Service (ou cec-ctl) redémarré pendant YouTube : la première moitié n'a pas été vue — ambiguïté, rien."""
        mon = self.monitor([at(0, state_event()) + at(1, active_source()) + at(1, routing_change()),
                            at(0, state_event()) + at(0.05, stream_path()) + at(5, power_poll())])
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual(self.bus.transmits(), [])
        fresh = self.monitor([at(0, state_event()) + at(1, routing_change()) + at(0.05, stream_path())])
        fresh.watch("/dev/cec0", "1.0.0.0", 0x10)
        self.assertEqual(self.bus.transmits(), [])


# --------------------------------------------------------------------------- tout le reste ne déclenche rien
NEG = {
    "wrong_pa_in_routing": lambda: at(0, active_source()) + at(60, routing_change(new="2.0.0.0")) +
    at(0.05, stream_path()),
    "wrong_pa_in_stream_path": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.05, stream_path("2.0.0.0")),
    "routing_not_from_native_root": lambda: at(0, active_source()) + at(60, routing_change(orig="2.0.0.0")) +
    at(0.05, stream_path()),
    "other_source_active": lambda: at(0, active_source("3.0.0.0", src=8)) + at(60, routing_change()) +
    at(0.05, stream_path()),
    "other_source_between": lambda: at(0, active_source()) + at(30, active_source("3.0.0.0", src=8)) +
    at(30, routing_change()) + at(0.05, stream_path()),
    # la TV diffuse <Active Source> mais pas l'adresse racine 0.0.0.0 : ce n'est pas une application native
    "tv_active_source_not_root": lambda: at(0, active_source("2.0.0.0")) + at(60, routing_change()) +
    at(0.05, stream_path()),
    "tv_active_source_our_pa": lambda: at(0, active_source("1.0.0.0")) + at(60, routing_change()) +
    at(0.05, stream_path()),
    "tv_non_root_active_source_after_native": lambda: at(0, active_source()) + at(30, active_source("3.0.0.0")) +
    at(30, routing_change()) + at(0.05, stream_path()),
    "no_prior_native_active_source": lambda: at(60, routing_change()) + at(0.05, stream_path()),
    "native_active_source_not_broadcast": lambda: at(0, active_source(dst=4)) + at(60, routing_change()) +
    at(0.05, stream_path()),
    "missing_routing_change": lambda: at(0, active_source()) + at(60, stream_path()),
    "missing_stream_path": lambda: at(0, active_source()) + at(60, routing_change()) + at(5, power_poll()) +
    at(5, power_poll()),
    "reversed_order": lambda: at(0, active_source()) + at(60, stream_path()) + at(0.05, routing_change()),
    "stream_path_too_late": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(cecreturn.ROUTE_TO_STREAM_MAX_S + 1, stream_path()),
    "native_too_old": lambda: at(0, active_source()) + at(cecreturn.NATIVE_MAX_S + 1, routing_change()) +
    at(0.05, stream_path()),
    "routing_from_other_device": lambda: at(0, active_source()) + at(60, routing_change(src=5)) +
    at(0.05, stream_path()),
    "stream_path_from_other_device": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.05, stream_path(src=5)),
    "standby_between": lambda: at(0, active_source()) + at(30, frame(0x36, "STANDBY")) +
    at(30, routing_change()) + at(0.05, stream_path()),
    "samsung_home_overlay_only": lambda: at(0, key()) + sum((at(5, power_poll()) for _ in range(40)), []) +
    at(1, key(0x0d, "back")),
    "lost_messages_between": lambda: at(0, active_source()) + at(30, ["", "1.0s: Event: Lost 3 messages"]) +
    at(30, routing_change()) + at(0.05, stream_path()),
    "malformed_raw_disagrees_with_args": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.05, stream_path("2.0.0.0")[:2] + [raw_line([0x0f, 0x86, 0x10, 0x00])]),
    "partial_frame_without_raw": lambda: at(0, active_source()) + at(60, routing_change()[:-1]) +
    at(0.05, stream_path()),
    "garbage_line_between": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.01, ["\x1b[2Jbogus output"]) + at(0.05, stream_path()),
    "orphan_argument_line": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.01, ["\tphys-addr: 1.0.0.0"]) + at(0.05, stream_path()),
    "wrong_length_stream_path": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.05, stream_path()[:2] + [raw_line([0x0f, 0x86, 0x10])]),
    "duplicate_stream_path": lambda: at(0, active_source()) + at(60, routing_change()) + at(0.05, stream_path()) +
    at(0.05, stream_path()),
    "header_disagrees_with_raw": lambda: at(0, active_source()) + at(60, routing_change()) +
    at(0.05, ["Received from Playback Device 1 to all (4 to 15): SET_STREAM_PATH (0x86):", "\tphys-addr: 1.0.0.0",
              raw_line([0x0f, 0x86, 0x10, 0x00])]),
}


class TestNoTrigger(RBase):
    def test_every_non_exact_sequence_emits_nothing(self):
        for name, script in sorted(NEG.items()):
            with self.subTest(name):
                self.bus.calls.clear()
                self.logs.clear()
                self.watch(at(0, state_event()) + script() + at(10, power_poll()))
                self.assertTrue(self.ready, name)                       # moniteur prêt : refus par la séquence
                self.assertEqual(self.bus.transmits(), [], name)
                self.assertEqual(self.recoveries(), [], name)

    def test_oversize_or_unknown_output_ends_watch_without_emission(self):
        mon = self.monitor([FakeLines(self.clock, ready_first(at(0, active_source()) + at(60, routing_change())),
                                      end="oversize")])
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("oversize", True))
        self.assertEqual(self.bus.transmits(), [])

    def test_pending_recovery_is_dropped_when_monitor_dies_before_settle(self):
        script = at(0, active_source()) + at(60, routing_change()) + at(0.05, stream_path())
        self.watch(script, end="eof")
        self.assertEqual(self.bus.transmits(), [])                 # fin avant l'échéance : rien (redémarrage ambigu)

    def test_keys_or_other_announcement_during_settle_cancel_recovery(self):
        base = at(0, active_source()) + at(60, routing_change()) + at(0.05, stream_path())
        for extra in (key(), active_source("1.0.0.0", src=4, tx=True), active_source("3.0.0.0", src=8)):
            with self.subTest(extra[0]):
                self.bus.calls.clear()
                self.watch(base + at(0.2, extra) + at(10, power_poll()))
                self.assertEqual(self.bus.transmits(), [])

    def test_adapter_state_change_ends_watch(self):
        for ev in (state_event("2.0.0.0", initial=False), state_event("1.0.0.0", 0x0000, initial=False),
                   state_event("f.f.f.f", 0x0000, initial=False)):
            with self.subTest(ev[1]):
                _, why = self.watch(at(0, active_source()) + at(1, ev) + at(60, routing_change()) +
                                    at(0.05, stream_path()))
                self.assertEqual((why, self.ready), ("adapter_state_changed", True))
                self.assertEqual(self.bus.transmits(), [])


# --------------------------------------------------------------------------- recovery : mêmes gardes que --activate
class TestRecoveryGates(RBase):
    def test_tv_not_on_refuses_without_wake(self):
        for power in ("standby", "to-on", "to-standby", "silent"):
            with self.subTest(power):
                self.bus.calls.clear()
                self.bus.power = power
                res = self.solo().recover("/dev/cec0", "1.0.0.0")
                self.assertEqual(res["result"], "refused_tv_not_on")
                self.assertEqual(self.bus.opts(), ["--give-device-power-status"])
                self.assertEqual(wake_args(self.bus), [])

    def test_adapter_changed_or_foreign_refuses_before_any_emission(self):
        for state, pa in (("foreign", "1.0.0.0"), ("empty", "1.0.0.0"), ("ours", "2.0.0.0"), ("ours", "f.f.f.f")):
            with self.subTest((state, pa)):
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": pa, "state": state}})
                res = self.solo(bus=bus).recover("/dev/cec0", "1.0.0.0")
                self.assertEqual(res["result"], "adapter_changed")
                self.assertEqual(bus.transmits(), [])
                if state == "foreign":
                    self.assertNotIn("SecretBox", json.dumps(res))

    def test_uses_existing_lock_and_waits_bounded(self):
        fd = cecboot._lock(self.state)
        try:
            t0 = self.clock.t
            res = self.solo().recover("/dev/cec0", "1.0.0.0")
            self.assertEqual(res["result"], "busy")
            self.assertLessEqual(self.clock.t - t0, cecreturn.LOCK_WAIT_S + 0.01)
            self.assertEqual(self.bus.calls, [])
        finally:
            os.close(fd)
        # attente réelle et bornée : sondage toutes les LOCK_POLL_S jusqu'à LOCK_WAIT_S, jamais au-delà
        self.assertGreaterEqual(self.clock.t - t0, cecreturn.LOCK_WAIT_S - cecreturn.LOCK_POLL_S - 0.01)
        self.assertTrue(self.sleeps and set(self.sleeps) == {cecreturn.LOCK_POLL_S}, self.sleeps)
        fd2 = cecboot._lock(self.state)                              # libre ensuite : la reprise passe
        os.close(fd2)
        self.assertEqual(self.solo().recover("/dev/cec0", "1.0.0.0")["result"], "announced")

    def test_lock_released_during_bounded_wait_then_announces(self):
        fd = [cecboot._lock(self.state)]
        t0 = self.clock.t

        def sleep(s):                                                 # le passage de la minuterie finit après ~3 s
            self.sleep(s)
            if fd[0] is not None and self.clock.t - t0 >= 3.0:
                os.close(fd[0])
                fd[0] = None
        mon = self.solo()
        mon.sleep = sleep
        res = mon.recover("/dev/cec0", "1.0.0.0")
        self.assertEqual(res["result"], "announced")
        self.assertIsNone(fd[0])
        self.assertLess(self.clock.t - t0, cecreturn.LOCK_WAIT_S)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)

    def test_lock_released_after_recovery_so_timer_and_activate_coexist(self):
        self.solo().recover("/dev/cec0", "1.0.0.0")
        fd = cecboot._lock(self.state)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_failed_active_source_reports_failure_and_no_menu_status(self):
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, fail={"--active-source"})
        res = self.solo(bus=bus).recover("/dev/cec0", "1.0.0.0")
        self.assertEqual(res["result"], "failed")
        self.assertNotIn("--menu-status", bus.opts())

    def test_budget_bounded_when_bus_hangs(self):
        def hang(argv, timeout):
            self.clock.t += timeout + cecboot.KILL_GRACE_S
            return 124, ""
        t0 = self.clock.t
        res = self.solo(bus=hang).recover("/dev/cec0", "1.0.0.0")
        self.assertIn(res["result"], ("adapter_changed", "budget_exceeded"))
        self.assertLessEqual(self.clock.t - t0, cecreturn.RECOVER_BUDGET_S + cecreturn.LOCK_WAIT_S)

    def test_rapid_back_and_forth_announces_every_exact_return(self):
        """Usage normal : YouTube → OpenTV → YouTube → OpenTV … en quelques secondes ; chaque retour exact rend les
        touches (aucune reprise perdue par un délai d'une minute)."""
        script = at(0, state_event())
        for _ in range(6):
            script += at(3.0, active_source()) + at(4.0, routing_change()) + at(0.05, stream_path())
        self.watch(script + at(10, power_poll()))
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual(res, ["announced"] * 6, res)
        self.assertEqual(self.bus.opts().count("--active-source"), 6)
        self.assertEqual(wake_args(self.bus), [])

    def test_return_inside_anti_duplicate_cooldown_is_deferred_not_dropped(self):
        first = at(0, state_event()) + at(5, active_source()) + at(60, routing_change()) + at(0.05, stream_path())
        quick = at(2.0, active_source()) + at(0.3, routing_change()) + at(0.05, stream_path())   # ~2 s après l'annonce
        self.watch(first + quick + at(10, power_poll()))
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual([r for r in res if r == "announced"], ["announced", "announced"], res)
        self.assertIn("cooldown_deferred", res)
        t = [t for t, a in self.bus.at if "--active-source" in a]
        self.assertGreaterEqual(t[1] - t[0], cecreturn.COOLDOWN_S - 0.01)
        self.assertLessEqual(t[1] - t[0], cecreturn.COOLDOWN_S + 1.0)
        # touches reçues pendant le report : plus rien à faire, la reprise reportée est annulée
        self.bus.calls.clear()
        self.logs.clear()
        self.watch(first + quick + at(2.0, key()) + at(10, power_poll()))
        self.assertEqual([e["result"] for e in self.recoveries()].count("announced"), 1)

    def test_busy_refused_or_failed_never_suppress_next_exact_return(self):
        """Seule une annonce réellement transmise consomme le délai et le quota ; busy / refus / échec non."""
        seq = at(2.0, active_source()) + at(2.0, routing_change()) + at(0.05, stream_path())
        for first in ("busy", "refused_tv_not_on", "failed"):
            with self.subTest(first):
                self.bus.calls.clear()
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                held = [None]
                if first == "busy":
                    held[0] = cecboot._lock(self.state)
                elif first == "refused_tv_not_on":
                    bus.power = "standby"
                else:
                    bus.fail = {"--active-source"}
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + seq + seq + at(10, power_poll()))],
                                   bus=bus)

                orig = mon.recover

                def recover(dev, pa, _orig=orig, _bus=bus):
                    r = _orig(dev, pa)
                    if held[0] is not None:                       # la minuterie libère le verrou ensuite
                        os.close(held[0])
                        held[0] = None
                    _bus.power, _bus.fail = "on", set()             # la TV est allumée au retour suivant
                    return r
                mon.recover = recover
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                res = [e["result"] for e in self.recoveries()]
                self.assertEqual(res, [first, "announced"], res)

    def test_hourly_cap_on_announcements_is_practical_and_bounded(self):
        script = at(0, state_event())
        for _ in range(cecreturn.MAX_PER_HOUR + 5):
            script += at(cecreturn.COOLDOWN_S + 1, active_source()) + at(1.0, routing_change()) + \
                at(0.05, stream_path())
        # une heure plus tard (même moniteur), le quota est rendu
        script += at(3600, active_source()) + at(1.0, routing_change()) + at(0.05, stream_path()) + \
            at(10, power_poll())
        self.watch(script)
        res = [e["result"] for e in self.recoveries()]
        self.assertGreaterEqual(cecreturn.MAX_PER_HOUR, 20)
        self.assertLessEqual(cecreturn.MAX_PER_HOUR, 60)
        self.assertLessEqual(cecreturn.COOLDOWN_S, 10)
        self.assertEqual(res[:cecreturn.MAX_PER_HOUR], ["announced"] * cecreturn.MAX_PER_HOUR, res)
        self.assertEqual(res[cecreturn.MAX_PER_HOUR:], ["rate_limited"] * 5 + ["announced"], res)
        self.assertEqual(self.bus.opts().count("--active-source"), cecreturn.MAX_PER_HOUR + 1)

    def test_all_attempts_have_a_flood_bound(self):
        """Une séquence exacte répétée sans fin (TV « pas allumée ») reste bornée en question d'alimentation."""
        self.bus.power = "standby"
        script = at(0, state_event())
        for _ in range(cecreturn.MAX_ATTEMPTS_PER_HOUR + 10):
            script += at(2.0, active_source()) + at(1.0, routing_change()) + at(0.05, stream_path())
        self.watch(script + at(10, power_poll()))
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual(res.count("refused_tv_not_on"), cecreturn.MAX_ATTEMPTS_PER_HOUR, res)
        self.assertEqual(res.count("rate_limited"), 10)
        self.assertEqual(self.bus.opts(), ["--give-device-power-status"] * cecreturn.MAX_ATTEMPTS_PER_HOUR)
        self.assertGreaterEqual(cecreturn.MAX_ATTEMPTS_PER_HOUR, cecreturn.MAX_PER_HOUR)

    def test_boot_marker_never_read_written_or_satisfied_by_recovery(self):
        st0 = cecboot.marker_stamp(self.state, BOOT_A)
        self.watch(captured())
        self.assertEqual(len(self.recoveries()), 1)
        self.assertEqual(cecboot.marker_stamp(self.state, BOOT_A), st0)
        self.assertFalse(os.path.exists(os.path.join(self.state, cecboot.STATE_FILE)))
        cecboot.save_announced(self.state, BOOT_A, {"/dev/cec0": "1.0.0.0"})
        stamp = cecboot.marker_stamp(self.state, BOOT_A)
        self.bus.calls.clear()
        self.watch(captured())
        self.assertEqual(cecboot.marker_stamp(self.state, BOOT_A), stamp)   # aucune réécriture : --activate intact
        src = module_source()
        for forbidden in ("save_announced", "load_announced", "marker_stamp", "boot_gate", "STATE_FILE", "bootstrap("):
            self.assertNotIn(forbidden, src)


# --------------------------------------------------------------------------- argv : jamais d'émission interdite
class TestArgv(RBase):
    def test_monitor_argv_is_passive_and_validated(self):
        argv = cecreturn.monitor_argv("/dev/cec3")
        self.assertEqual(argv, ["cec-ctl", "-d", "/dev/cec3", "-s", "-r", "-m"])
        for bad in ("--playback", "--clear", "-M", "--monitor-all", "--follower", "--exclusive-follower", "-t",
                    "--to", "-W", "--wait-for-msgs") + cecboot.NEVER_EMIT:
            self.assertNotIn(bad, argv)
        for dev in ("/dev/cec", "/dev/cecx", "cec0", "/dev/../dev/cec0", "/dev/cec0;rm"):
            with self.assertRaises(ValueError):
                cecreturn.monitor_argv(dev)

    def test_no_forbidden_option_on_any_path(self):
        self.watch(captured() + captured(youtube_s=cecreturn.COOLDOWN_S + 30))
        self.assertEqual(wake_args(self.bus), [])
        self.assertTrue(self.bus.polls())                       # le maintien neutre a bien tourné entre les deux
        for argv, _ in self.bus.calls:
            opts = [o for o in argv if o.startswith("-")]
            self.assertTrue(set(opts) <= {"-d", "-s", "-t", "--give-device-power-status", "--active-source",
                                          "--menu-status", "-P"}, argv)
            if "-P" in argv:                                    # <Poll> : argv exact, rien d'autre
                self.assertEqual(argv, ["cec-ctl", "-d", "/dev/cec0"] + POLL_ARGV_OPTS)
        src = module_source()
        for opt in WAKE + ("--playback", "--clear", "--osd-name", "shell=True", "os.system", "/dev/input",
                           "evdev", "uinput", "EVIOC"):
            self.assertNotIn(opt, src, opt)

    def test_never_emit_guard_still_applies(self):
        run = cecboot._Run(self.bus, self.clock, 30)
        for opt in cecboot.NEVER_EMIT:
            with self.assertRaises(ValueError):
                run(["cec-ctl", "-d", "/dev/cec0", opt], 5)
        self.assertEqual(self.bus.calls, [])


# --------------------------------------------------------------------------- surveillance : échec, redémarrage, bornes
class TestServeLifecycle(RBase):
    def test_no_adapter_or_unready_adapter_never_spawns(self):
        for devs, ad in (((), {}), (("cec0", "cec1"), {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                                      "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}}),
                         (("cec0",), {"/dev/cec0": {"pa": "1.0.0.0", "state": "foreign"}}),
                         (("cec0",), {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"}})):
            with self.subTest(ad):
                self.spawned.clear()
                self.sleeps.clear()
                bus = RecBus(self.clock, ad)
                mon = self.monitor([], bus=bus)
                mon.root = make_root(self, devs=devs)
                self.assertIsNone(mon.serve(max_cycles=5))            # attente légitime (TV éteinte…), pas un échec
                self.assertEqual(self.spawned, [])
                self.assertEqual(bus.transmits(), [])
                self.assertEqual(self.sleeps, [cecreturn.DISCOVERY_RETRY_S] * 5)

    def test_unexpected_post_ready_failures_restart_with_bounded_backoff(self):
        n = len(cecreturn.RESTART_BACKOFF_S) + 3
        scripts = [at(0, state_event()) for _ in range(n)]              # prêt, puis fin inattendue (eof)
        self.assertIsNone(self.monitor(scripts).serve(max_cycles=n))
        self.assertEqual(len(self.spawned), n)
        self.assertEqual(self.sleeps, [cecreturn.RESTART_BACKOFF_S[min(i, len(cecreturn.RESTART_BACKOFF_S) - 1)]
                                       for i in range(n)])
        self.assertTrue(all(s >= 5 for s in self.sleeps))
        self.assertLessEqual(max(self.sleeps), 300)
        self.assertEqual(self.bus.transmits(), [])
        ready = [e for e in self.logs if e.get("event") == "cec_return_ready"]
        self.assertEqual(len(ready), n)                                  # une fois par moniteur prêt
        ends = [e for e in self.logs if e.get("event") == "cec_return_watch_end"]
        self.assertEqual({(e["reason"], e["ready"]) for e in ends}, {("eof", True)})

    def test_long_healthy_watch_resets_backoff(self):
        long_ok = at(0, state_event()) + [(cecreturn.HEALTHY_RUN_S + 1, "")]
        ok = at(0, state_event())
        self.monitor([ok, ok, long_ok, ok]).serve(max_cycles=4)
        b = cecreturn.RESTART_BACKOFF_S
        self.assertEqual(self.sleeps, [b[0], b[1], b[0], b[1]])

    def test_expected_adapter_state_change_rediscovers_promptly_without_backoff(self):
        """TV éteinte/rallumée, HDMI rebranché : changement d'état attendu ⇒ relecture rapide, jamais 300 s aveugles."""
        change = at(0, state_event()) + at(1, state_event("f.f.f.f", 0x0000, initial=False))
        n = 12
        self.assertIsNone(self.monitor([change] * n).serve(max_cycles=n))
        self.assertEqual(self.sleeps, [cecreturn.STATE_CHANGE_RETRY_S] * n)
        self.assertLessEqual(cecreturn.STATE_CHANGE_RETRY_S, 5)
        # une fin inattendue après plusieurs changements d'état repart du premier palier
        self.sleeps.clear()
        self.monitor([at(0, state_event()), change, at(0, state_event()), change,
                      at(0, state_event())]).serve(max_cycles=5)
        b = cecreturn.RESTART_BACKOFF_S
        self.assertEqual(self.sleeps, [b[0], cecreturn.STATE_CHANGE_RETRY_S, b[0], cecreturn.STATE_CHANGE_RETRY_S,
                                       b[0]])

    def test_monitor_is_closed_on_every_exit(self):
        srcs = [FakeLines(self.clock, captured()), FakeLines(self.clock, at(0, ["x" * 10]), end="oversize")]
        mon = self.monitor(srcs)
        mon.serve(max_cycles=2)
        self.assertTrue(all(s.closed for s in srcs))

    def test_real_subprocess_lines_oversize_eof_and_kill(self):
        py = sys.executable
        src = cecreturn.PopenLines([py, "-c", "print('Received from TV to all (0 to 15): STANDBY (0x36)');"
                                    "print('\\tRaw: 0x0f 0x36 (  )')"])
        got = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            kind, line = src.readline(1.0)
            if kind == "line":
                got.append(line)
            elif kind != "timeout":
                break
        src.close()
        self.assertEqual((kind, got), ("eof", ["Received from TV to all (0 to 15): STANDBY (0x36)",
                                              "\tRaw: 0x0f 0x36 (  )"]))
        src = cecreturn.PopenLines([py, "-c", "import sys,time; sys.stdout.write('A'*%d); sys.stdout.flush(); "
                                    "time.sleep(30)" % (cecreturn.MAX_LINE_BYTES + 10)])
        kinds = set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "oversize" not in kinds:
            kinds.add(src.readline(1.0)[0])
        t0 = time.monotonic()
        src.close()
        self.assertIn("oversize", kinds)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIsNotNone(src.p.returncode)                        # processus réellement terminé
        hang = cecreturn.PopenLines([py, "-c", "import time; time.sleep(60)"])
        self.assertEqual(hang.readline(0.2), ("timeout", None))
        hang.close()
        self.assertIsNotNone(hang.p.returncode)
        with self.assertRaises(OSError):
            cecreturn.PopenLines(["/nonexistent/cec-ctl", "-m"])


# --------------------------------------------------------------------------- prêt / échec réel (faux vert interdit)
REFUSAL = "Selecting monitor mode failed, you may have to run this as root.\n"


def fake_cec_ctl(tc, body):
    """Faux `cec-ctl` exécutable (Python) : `body` reçoit argv ; lancé par PopenLines comme le vrai."""
    d = tmpdir(tc, "otvfakecec")
    path = os.path.join(d, "cec-ctl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("import os, sys, time\n" + body)
    return path


class TestReadiness(RBase):
    def test_ready_only_after_matching_initial_state_and_logged_once(self):
        mon, why = self.watch(at(0, state_event()) + at(1, power_poll()) + at(1, state_event(initial=True)))
        self.assertEqual((why, self.ready), ("eof", True))
        ready = [e for e in self.logs if e.get("event") == "cec_return_ready"]
        self.assertEqual(ready, [{"event": "cec_return_ready", "device": "/dev/cec0", "physical_address": "1.0.0.0"}])

    def test_wrong_or_missing_initial_state_is_never_ready(self):
        cases = {"other_pa": at(0, state_event("2.0.0.0")), "other_mask": at(0, state_event(mask=0x0100)),
                 "disconnected": at(0, state_event("f.f.f.f", 0x0000)),
                 "not_initial": at(0, state_event(initial=False)),
                 "frames_only": captured()[2:]}
        for name, script in sorted(cases.items()):
            with self.subTest(name):
                self.logs.clear()
                self.bus.calls.clear()
                _, why = self.watch(script, ready=False)
                self.assertFalse(self.ready, name)
                self.assertNotIn("cec_return_ready", [e.get("event") for e in self.logs])
                self.assertEqual(self.bus.transmits(), [], name)          # rien n'est armé avant « prêt »
                self.assertIn(why, ("adapter_state_changed", "no_initial_state", "eof", "not_ready"), name)
                if name == "not_initial":                    # même PA/masque sans événement initial : malformé
                    self.assertEqual(why, "no_initial_state")

    def test_sequence_seen_before_ready_never_arms_recovery(self):
        """Trames reçues avant l'événement initial (moniteur pas encore prêt) : jamais comptées dans la séquence."""
        script = at(0, active_source()) + at(1, routing_change()) + at(0.05, stream_path()) + \
            at(0.1, state_event()) + at(10, power_poll())
        _, why = self.watch(script, ready=False)
        self.assertEqual((why, self.ready), ("eof", True))
        self.assertEqual(self.bus.transmits(), [])
        self.assertEqual(self.recoveries(), [])

    def test_silent_monitor_without_initial_state_times_out(self):
        _, why = self.watch([], ready=False, end="silence")
        self.assertEqual((why, self.ready), ("not_ready", False))
        self.assertTrue(self.src.closed)

    def test_detector_failure_cannot_leak_the_monitor(self):
        """(d) Detector construit DANS le try/finally : une adresse invalide ferme quand même cec-ctl."""
        src = FakeLines(self.clock, captured())
        mon = self.monitor([src])
        self.assertEqual(mon.watch("/dev/cec0", "zz", 0x10), ("invalid_startup", False))
        self.assertTrue(src.closed)
        self.assertEqual(self.bus.transmits(), [])


class TestFatal(RBase):
    def ends(self):
        return [e for e in self.logs if e.get("event") in ("cec_return_watch_end", "cec_return_failed")]

    def test_monitor_refusal_on_stderr_with_exit_zero_is_fatal_78(self):
        src = FakeLines(self.clock, [], error="monitor_refused", exit_status=0)
        mon = self.monitor([src])
        self.assertEqual(mon.serve(max_cycles=50), cecreturn.EXIT_FATAL)
        self.assertEqual(cecreturn.EXIT_FATAL, 78)
        self.assertEqual(len(self.spawned), 1)                          # aucune boucle de relance interne
        self.assertTrue(src.closed)
        failed = [e for e in self.logs if e.get("event") == "cec_return_failed"]
        self.assertEqual(failed, [{"event": "cec_return_failed", "reason": "monitor_refused", "exit": 78}])
        self.assertNotIn("cec_return_ready", [e.get("event") for e in self.logs])
        self.assertEqual(self.bus.transmits(), [])

    def test_missing_cec_ctl_is_fatal(self):
        for exc in (FileNotFoundError("cec-ctl"), PermissionError("cec-ctl")):
            with self.subTest(type(exc).__name__):
                self.logs.clear()
                self.spawned.clear()
                self.assertEqual(self.monitor([exc]).serve(max_cycles=50), 78)
                self.assertEqual(len(self.spawned), 1)
                self.assertEqual(self.ends()[-1]["reason"], "cec_ctl_missing")

    def test_repeated_pre_ready_failures_are_fatal_not_endless_active(self):
        for label, mk in (("eof", lambda: FakeLines(self.clock, [])),
                          ("open_failed", lambda: FakeLines(self.clock, [], error="open_failed", exit_status=1)),
                          ("exit_nonzero", lambda: FakeLines(self.clock, [], exit_status=1)),
                          ("not_ready", lambda: FakeLines(self.clock, [], end="silence")),
                          # PA/masque attendus mais sans événement INITIAL : malformé, pas un changement d'état
                          ("no_initial_state", lambda: FakeLines(self.clock, at(0, state_event(initial=False)))),
                          ("spawn_failed", lambda: OSError("EMFILE"))):
            with self.subTest(label):
                self.logs.clear()
                self.spawned.clear()
                self.sleeps.clear()
                srcs = [mk() for _ in range(20)]
                self.assertEqual(self.monitor(srcs).serve(max_cycles=50), 78)
                self.assertEqual(len(self.spawned), cecreturn.PRE_READY_FAILURES_MAX)
                self.assertEqual(self.sleeps, [cecreturn.PRE_READY_RETRY_S] * (cecreturn.PRE_READY_FAILURES_MAX - 1))
                self.assertEqual(self.ends()[-1]["reason"], label)
                self.assertTrue(all(s.closed for s in srcs[:len(self.spawned)] if isinstance(s, FakeLines)))

    def test_unreadable_adapter_is_fatal_not_waiting_forever(self):
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours", "unreadable": True},
                                  "/dev/cec1": {"pa": "f.f.f.f", "state": "empty", "unreadable": True}})
        self.assertEqual(self.monitor([], bus=bus).serve(max_cycles=50), 78)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.ends()[-1]["reason"], "adapter_unreadable")

    def test_ready_monitor_resets_pre_ready_failure_count(self):
        n = cecreturn.PRE_READY_FAILURES_MAX - 1
        scripts = []
        for _ in range(4):
            scripts += [FakeLines(self.clock, []) for _ in range(n)] + [FakeLines(self.clock, at(0, state_event()))]
        self.assertIsNone(self.monitor(scripts).serve(max_cycles=len(scripts)))
        self.assertEqual(len(self.spawned), len(scripts))

    def test_real_fake_cec_ctl_stderr_exit0_fails_service_and_never_logs_raw_text(self):
        """Faux cec-ctl RÉEL : refus du mode moniteur sur stderr puis exit 0 ⇒ code 78, aucun texte brut journalisé."""
        exe = fake_cec_ctl(self, "sys.stderr.write(%r + 'SECRET-raw-detail\\n'); sys.exit(0)\n" % REFUSAL)
        mon = self.monitor([])
        spawned = []

        def spawn(argv):
            spawned.append(argv)
            self.assertLess(len(spawned), 10, "boucle de relance interne : jamais d'échec véridique")
            return cecreturn.PopenLines([sys.executable, "-I", exe] + list(argv[1:]))
        mon.spawn = spawn
        mon.mono = time.monotonic
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cecreturn.run(mon)
        self.assertEqual(code, 78)
        blob = json.dumps(self.logs) + out.getvalue()
        self.assertIn("monitor_refused", blob)
        for raw in ("SECRET", "Selecting", "root"):
            self.assertNotIn(raw, blob)

    def test_real_fake_cec_ctl_ready_then_exit_is_not_fatal(self):
        exe = fake_cec_ctl(self, "print(); print('Initial Event: State Change: PA: 1.0.0.0, LA mask: 0x0010, "
                                 "Conn Info: yes', flush=True); time.sleep(0.3)\n")
        mon = self.monitor([])
        mon.spawn = lambda argv: cecreturn.PopenLines([sys.executable, "-I", exe] + list(argv[1:]))
        mon.mono = time.monotonic
        self.assertIsNone(mon.serve(max_cycles=1))
        self.assertIn("cec_return_ready", [e.get("event") for e in self.logs])
        self.assertEqual(self.ends()[-1]["reason"], "eof")

    def test_cli_propagates_fatal_exit(self):
        d = tmpdir(self, "otvcecret")
        env = os.path.join(d, "env")
        open(env, "w").close()
        real = cecreturn.Monitor
        src = FakeLines(self.clock, [], error="monitor_refused")

        def factory(root, state_dir, **kw):
            return real(self.root, runner=self.bus, spawn=lambda argv: src, mono=self.clock, sleep=self.sleep,
                        state_dir=state_dir, log=self.logs.append, **kw)
        with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cecreturn, "Monitor", factory), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--env", env, "cec-return-monitor", "--state-dir", self.state])
        self.assertEqual(code, 78)


# --------------------------------------------------------------------------- changement d'état attendu : ni angle mort ni 78
class TestAdapterChurn(RBase):
    """Revue (étape 1) : après un changement d'état attendu de l'adaptateur (TV éteinte, HDMI, réclamation en cours),
    une découverte vide ne doit jamais retomber sur l'attente aveugle de DISCOVERY_RETRY_S, et un événement initial
    discordant avant « prêt » (état qui bouge) ne compte jamais vers la sortie 78. Les vrais échecs restent 78 et
    aucune reprise n'est possible avant un NOUVEL événement initial conforme."""

    def setUp(self):
        super().setUp()
        self.plan = {}                                  # n-ième sommeil -> changement de l'adaptateur simulé
        self.found_at = []                              # instants de chaque découverte (vide ou non)

    def sleep(self, s):
        super().sleep(s)
        f = self.plan.get(len(self.sleeps))
        if f:
            f()

    def tv(self, pa):
        return lambda: self.bus.ad["/dev/cec0"].__setitem__("pa", pa)

    def track(self, mon):
        real = mon.discover

        def discover():
            self.found_at.append(self.clock.t)
            return real()
        mon.discover = discover
        mon.log = lambda e: self.logs.append(dict(e, _t=self.clock.t))
        return mon

    def events(self, name):
        return [e for e in self.logs if e.get("event") == name]

    def test_ready_state_change_then_absent_adapter_polls_short_until_ready_again(self):
        """(1) prêt → adapter_state_changed → découverte vide plusieurs fois : sommeils courts seulement (2 s puis
        5 s, jamais 30 s), puis reprise « prêt » et retour YouTube exact annoncé."""
        off = at(0, state_event()) + at(1, state_event("f.f.f.f", 0x0000, initial=False))
        absent = 40                                     # lectures vides successives, au-delà de la fenêtre rapide
        self.plan = {1: self.tv("f.f.f.f"), 1 + absent: self.tv("1.0.0.0")}
        mon = self.track(self.monitor([off, captured()]))
        self.assertIsNone(mon.serve(max_cycles=absent + 2))
        self.assertEqual(len(self.spawned), 2)          # aucun moniteur pendant l'absence
        waits = self.sleeps[:-1]                        # dernier : fin inattendue du 2e moniteur (palier normal)
        self.assertEqual(len(waits), absent + 1)
        self.assertNotIn(cecreturn.DISCOVERY_RETRY_S, waits)
        self.assertTrue(all(2 <= s <= 5 for s in waits), waits)
        self.assertEqual(set(waits), {cecreturn.STATE_CHANGE_RETRY_S, cecreturn.REDISCOVER_SLOW_S})
        self.assertEqual(waits, sorted(waits))          # rapide d'abord, puis basse fréquence (bornée, ≤ 5 s)
        fast = sum(s for s in waits if s == cecreturn.STATE_CHANGE_RETRY_S)
        self.assertGreaterEqual(fast, cecreturn.REDISCOVER_FAST_WINDOW_S - cecreturn.STATE_CHANGE_RETRY_S)
        gaps = [b - a for a, b in zip(self.found_at[1:], self.found_at[2:])]
        self.assertLessEqual(max(gaps), cecreturn.REDISCOVER_SLOW_S + 1, gaps)   # jamais d'angle mort de 30 s
        # attente explicite et véridique : jamais « prêt » pendant l'absence
        waiting = self.events("cec_return_waiting")
        self.assertEqual([(e["reason"], e["rediscover"]) for e in waiting], [("no_adapter", True)])
        ready = self.events("cec_return_ready")
        self.assertEqual(len(ready), 2)
        self.assertGreater(ready[1]["_t"], self.found_at[-1])
        rec = self.recoveries()
        self.assertEqual([r["result"] for r in rec], ["announced"])
        self.assertTrue(all(t > ready[1]["_t"] for t, a in self.bus.at if "--active-source" in a))
        self.assertEqual(wake_args(self.bus), [])

    def test_pre_ready_initial_state_mismatches_never_exit_78_and_rediscover_promptly(self):
        """(2) événements initiaux discordants répétés avant « prêt » (réclamation/HDMI qui rebondit) : jamais 78,
        relecture rapide à chaque fois, puis prêt et reprise."""
        churn = [("f.f.f.f", 0x0000), ("1.0.0.0", 0x0000), ("2.0.0.0", 0x0010), ("1.0.0.0", 0x0100)] * 2
        self.assertGreater(len(churn), cecreturn.PRE_READY_FAILURES_MAX)
        scripts = [at(0, state_event(pa, m)) + at(30, power_poll()) for pa, m in churn] + [captured()]
        mon = self.track(self.monitor(scripts))
        self.assertIsNone(mon.serve(max_cycles=len(scripts)))
        self.assertEqual(len(self.spawned), len(scripts))
        self.assertEqual(self.events("cec_return_failed"), [])
        self.assertEqual(self.sleeps[:-1], [cecreturn.STATE_CHANGE_RETRY_S] * len(churn))
        ends = [(e["reason"], e["ready"]) for e in self.events("cec_return_watch_end")]
        self.assertEqual(ends[:len(churn)], [("adapter_state_changed", False)] * len(churn))
        self.assertEqual(len(self.events("cec_return_ready")), 1)
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])

    def test_pre_ready_mismatch_then_absent_adapter_keeps_short_polling(self):
        """Discordance avant « prêt » puis adaptateur absent (HDMI débranché) : toujours ≤ 5 s, jamais 30 s ni 78."""
        self.plan = {1: self.tv("f.f.f.f"), 6: self.tv("1.0.0.0")}
        mon = self.track(self.monitor([at(0, state_event("1.0.0.0", 0x0000)), captured()]))
        self.assertIsNone(mon.serve(max_cycles=7))
        self.assertEqual(self.sleeps[:-1], [cecreturn.STATE_CHANGE_RETRY_S] * 6)
        self.assertEqual(self.events("cec_return_failed"), [])
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])

    def test_true_failures_still_exit_78_despite_churn(self):
        """(3) refus du moniteur, cec-ctl absent, fins génériques répétées avant « prêt » : toujours 78 ; une
        discordance intercalée ne blanchit pas les échecs génériques ; discordance identique persistante = incohérence."""
        change = at(0, state_event()) + at(1, state_event("f.f.f.f", 0x0000, initial=False))
        mismatch = at(0, state_event("1.0.0.0", 0x0000))
        for label, scripts, reason in (
                ("refusal", [change, mismatch, FakeLines(self.clock, [], error="monitor_refused")], "monitor_refused"),
                ("missing", [change, mismatch, FileNotFoundError("cec-ctl")], "cec_ctl_missing"),
                ("eof_x3", [[], [], []], "eof"),
                ("eof_interleaved", [[], mismatch, [], mismatch, []], "eof")):
            with self.subTest(label):
                self.logs.clear()
                self.spawned.clear()
                self.assertEqual(self.monitor(scripts).serve(max_cycles=50), cecreturn.EXIT_FATAL)
                self.assertEqual(len(self.spawned), len(scripts))
                self.assertEqual(self.events("cec_return_failed")[-1]["reason"], reason)
                self.assertEqual(self.bus.transmits(), [])
        # même discordance, même adaptateur lu, encore et encore : jamais un état qui bouge -> échec véridique borné
        self.logs.clear()
        self.spawned.clear()
        self.sleeps.clear()
        self.assertGreater(cecreturn.MISMATCH_STUCK_MAX, cecreturn.PRE_READY_FAILURES_MAX)
        stuck = [at(0, state_event("2.0.0.0")) for _ in range(50)]
        self.assertEqual(self.monitor(stuck).serve(max_cycles=200), cecreturn.EXIT_FATAL)
        self.assertEqual(len(self.spawned), cecreturn.MISMATCH_STUCK_MAX)
        self.assertEqual(self.sleeps, [cecreturn.STATE_CHANGE_RETRY_S] * (cecreturn.MISMATCH_STUCK_MAX - 1))
        self.assertEqual(self.events("cec_return_failed")[-1]["reason"], "adapter_state_inconsistent")

    def test_no_recovery_before_a_new_matching_ready_event(self):
        """(4) retour détecté puis changement d'état pendant le calme, séquence complète vue avant l'événement initial
        d'un nouveau moniteur, fin de séquence avant « prêt » : rien n'est émis. Seul un retour exact APRÈS le nouvel
        événement initial conforme est annoncé (jamais une séquence vue avant lui, même suivie du calme exigé)."""
        m1 = at(0, state_event()) + at(5, active_source()) + at(60, routing_change()) + at(0.05, stream_path()) + \
            at(0.5, state_event("f.f.f.f", 0x0000, initial=False))
        m2 = at(0, active_source()) + at(1, routing_change()) + at(0.05, stream_path()) + \
            at(0.1, state_event("1.0.0.0", 0x0000))
        m3 = at(0, active_source()) + at(1, routing_change()) + at(0.05, stream_path()) + at(0.1, state_event()) + \
            at(5, power_poll()) + at(5, power_poll())                      # calme bien au-delà de SETTLE_S
        self.assertIsNone(self.track(self.monitor([m1, m2, m3])).serve(max_cycles=3))
        self.assertEqual(self.bus.transmits(), [])
        self.assertEqual(self.recoveries(), [])
        self.assertEqual(len(self.events("cec_return_ready")), 2)          # m1 et m3 seulement
        # même début, puis un retour exact complet après le nouvel événement initial conforme : une seule annonce
        self.logs.clear()
        self.spawned.clear()
        m4 = m3 + at(5, active_source()) + at(30, routing_change()) + at(0.05, stream_path()) + at(10, power_poll())
        self.assertIsNone(self.track(self.monitor([m1, m2, m4])).serve(max_cycles=3))
        ready = self.events("cec_return_ready")
        self.assertEqual(len(ready), 2)
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])
        self.assertTrue(all(t > ready[-1]["_t"] for t, a in self.bus.at if "--active-source" in a))
        self.assertEqual(wake_args(self.bus), [])

    # ----------------------------------------------------------------- revue finale (étape 2) : discriminants bloquants
    def test_varied_mismatches_beyond_stuck_max_never_exit_78(self):
        """(P1) L'identité d'une discordance inclut l'état OBSERVÉ (adresse physique, masque), pas seulement
        l'adaptateur lu : plus de MISMATCH_STUCK_MAX discordances légitimes mais DIFFÉRENTES à la suite sur le même
        adaptateur (l'état bouge vraiment) ne sont jamais `adapter_state_inconsistent`."""
        states = [("f.f.f.f", 0x0000), ("1.0.0.0", 0x0000), ("2.0.0.0", 0x0010)]
        churn = [states[i % len(states)] for i in range(cecreturn.MISMATCH_STUCK_MAX + 3)]
        self.assertGreater(len(churn), cecreturn.MISMATCH_STUCK_MAX)
        self.assertTrue(all(a != b for a, b in zip(churn, churn[1:])))   # jamais deux fois la même à la suite
        scripts = [at(0, state_event(pa, m)) for pa, m in churn] + [captured()]
        mon = self.track(self.monitor(scripts))
        self.assertIsNone(mon.serve(max_cycles=len(scripts)))
        self.assertEqual(len(self.spawned), len(scripts))
        self.assertEqual(self.events("cec_return_failed"), [])
        ends = [(e["reason"], e["ready"]) for e in self.events("cec_return_watch_end")]
        self.assertEqual(ends[:len(churn)], [("adapter_state_changed", False)] * len(churn))
        self.assertEqual(self.sleeps[:-1], [cecreturn.STATE_CHANGE_RETRY_S] * len(churn))
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])

    def test_absent_adapter_resets_identical_mismatch_count(self):
        """(P2) Une découverte sans adaptateur (HDMI débranché, TV éteinte) interrompt la série : la même discordance
        avant puis après l'absence ne s'additionne pas vers MISMATCH_STUCK_MAX (jamais 78), l'attente reste
        explicite (`rediscover`), puis prêt et retour annoncé."""
        n = cecreturn.MISMATCH_STUCK_MAX - 1
        self.assertGreaterEqual(2 * n, cecreturn.MISMATCH_STUCK_MAX)     # sans remise à zéro : 78 assuré
        same = [at(0, state_event("2.0.0.0")) for _ in range(n)]
        self.plan = {n: self.tv("f.f.f.f"), n + 1: self.tv("1.0.0.0")}   # une seule découverte vide entre les séries
        scripts = same + same + [captured()]
        mon = self.track(self.monitor(scripts))
        self.assertIsNone(mon.serve(max_cycles=len(scripts) + 1))
        self.assertEqual(len(self.spawned), len(scripts))
        self.assertEqual(self.events("cec_return_failed"), [])
        waiting = self.events("cec_return_waiting")
        self.assertEqual([(e["reason"], e["rediscover"]) for e in waiting], [("no_adapter", True)])
        self.assertEqual(self.sleeps[:-1], [cecreturn.STATE_CHANGE_RETRY_S] * (2 * n + 1))
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])
        # témoin : la même série ininterrompue atteint bien l'incohérence véridique
        self.logs.clear()
        self.spawned.clear()
        self.sleeps.clear()
        self.plan = {}
        self.assertEqual(self.monitor(same + same).serve(max_cycles=50), cecreturn.EXIT_FATAL)
        self.assertEqual(len(self.spawned), cecreturn.MISMATCH_STUCK_MAX)
        self.assertEqual(self.events("cec_return_failed")[-1]["reason"], "adapter_state_inconsistent")

    def test_generic_pre_ready_failure_during_churn_keeps_short_rediscovery(self):
        """(P3) Prêt → changement d'état attendu, puis fin générique du moniteur suivant avant « prêt » et adaptateur
        absent : l'échec générique compte vers 78 mais n'efface pas l'état de redécouverte — attentes ≤ 5 s,
        jamais DISCOVERY_RETRY_S (30 s), attente journalisée `rediscover: true`, puis reprise."""
        change = at(0, state_event()) + at(1, state_event("f.f.f.f", 0x0000, initial=False))
        absent = 6
        self.plan = {2: self.tv("f.f.f.f"), 2 + absent: self.tv("1.0.0.0")}
        mon = self.track(self.monitor([change, [], captured()]))
        self.assertIsNone(mon.serve(max_cycles=3 + absent))
        self.assertEqual(len(self.spawned), 3)
        ends = [(e["reason"], e["ready"]) for e in self.events("cec_return_watch_end")]
        self.assertEqual(ends[:2], [("adapter_state_changed", True), ("eof", False)])
        waits = self.sleeps[:-1]                        # dernier : fin inattendue du moniteur prêt (palier normal)
        self.assertEqual(len(waits), 2 + absent)
        self.assertNotIn(cecreturn.DISCOVERY_RETRY_S, waits)
        self.assertTrue(all(s <= cecreturn.REDISCOVER_SLOW_S for s in waits), waits)
        self.assertEqual(waits[2:], [cecreturn.STATE_CHANGE_RETRY_S] * absent)
        waiting = self.events("cec_return_waiting")
        self.assertEqual([(e["reason"], e["rediscover"]) for e in waiting], [("no_adapter", True)])
        self.assertEqual(self.events("cec_return_failed"), [])
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])

    def test_ready_state_change_resets_generic_pre_ready_failures(self):
        """(P4) Un moniteur « prêt » qui se termine par `adapter_state_changed` prouve que cec-ctl fonctionne : le
        compteur d'échecs génériques repart de zéro. Il faut ensuite PRE_READY_FAILURES_MAX NOUVEAUX échecs (pas
        moins) pour la sortie 78."""
        k = cecreturn.PRE_READY_FAILURES_MAX
        self.assertGreaterEqual(k, 2)
        change = at(0, state_event()) + at(1, state_event("f.f.f.f", 0x0000, initial=False))
        before, after = [[] for _ in range(k - 1)], [[] for _ in range(k - 1)]
        scripts = before + [change] + after + [captured()]
        self.assertIsNone(self.track(self.monitor(scripts)).serve(max_cycles=len(scripts)))
        self.assertEqual(len(self.spawned), len(scripts))
        self.assertEqual(self.events("cec_return_failed"), [])
        self.assertEqual([r["result"] for r in self.recoveries()], ["announced"])
        # exactement PRE_READY_FAILURES_MAX nouveaux échecs après la remise à zéro : 78 au k-ième, pas avant
        self.logs.clear()
        self.spawned.clear()
        self.bus.calls.clear()
        scripts = before + [change] + [[] for _ in range(k)] + [captured()]
        self.assertEqual(self.track(self.monitor(scripts)).serve(max_cycles=50), cecreturn.EXIT_FATAL)
        self.assertEqual(len(self.spawned), (k - 1) + 1 + k)
        failed = self.events("cec_return_failed")
        self.assertEqual([(e["reason"], e["failures"]) for e in failed], [("eof", k)])
        self.assertEqual(self.bus.transmits(), [])


class TestPopenStderr(unittest.TestCase):
    def drain(self, src, limit=10):
        got, kind = [], None
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            kind, line = src.readline(1.0)
            if kind == "line":
                got.append(line)
            elif kind != "timeout":
                break
        return kind, got

    def test_stderr_refusal_with_exit_zero_is_classified(self):
        py = sys.executable
        for text, rc, want in ((REFUSAL, 0, "monitor_refused"),
                               ("Failed to open /dev/cec0: Permission denied\n", 1, "open_failed"),
                               ("\nDevice was disconnected.\n", 0, "device_disconnected"),
                               ("something else entirely\n", 0, "stderr_unclassified"),
                               ("", 0, None), ("", 3, None)):
            with self.subTest(want):
                src = cecreturn.PopenLines([py, "-c", "import sys; sys.stderr.write(%r); sys.exit(%d)" % (text, rc)])
                try:
                    self.assertEqual(self.drain(src), ("eof", []))
                    self.assertEqual(src.diagnosis(), {"error": want, "exit_status": rc})
                finally:
                    src.close()

    def test_stderr_flood_cannot_deadlock_and_is_byte_bounded(self):
        py = sys.executable
        src = cecreturn.PopenLines([py, "-c", "import sys; sys.stderr.write('E' * 1000000); sys.stderr.flush(); "
                                    "print('Received from TV to all (0 to 15): STANDBY (0x36)', flush=True)"])
        try:
            kind, got = self.drain(src, limit=15)
            self.assertEqual((kind, got), ("eof", ["Received from TV to all (0 to 15): STANDBY (0x36)"]))
            self.assertLessEqual(len(src.err), cecreturn.STDERR_MAX_BYTES)
            self.assertEqual(src.diagnosis(), {"error": "stderr_unclassified", "exit_status": 0})
        finally:
            src.close()

    def test_classification_is_line_and_byte_bounded(self):
        c = cecreturn.classify_stderr
        self.assertEqual(c(REFUSAL.encode()), "monitor_refused")
        self.assertEqual(c(b"x" * (cecreturn.MAX_LINE_BYTES + 1) + REFUSAL.encode()), "stderr_unclassified")
        self.assertEqual(c(b"noise\n" * cecreturn.STDERR_MAX_LINES + REFUSAL.encode()), "stderr_unclassified")
        self.assertEqual(c(b"\xff\xfe" + REFUSAL.encode()), "monitor_refused")
        self.assertIsNone(c(b"  \n\n"))
        self.assertEqual(set(cecreturn.STDERR_CODES_ALL), {"monitor_refused", "open_failed", "device_disconnected",
                                                           "stderr_unclassified"})

    def test_grandchild_holding_pipes_is_killed_with_the_group(self):
        py = sys.executable
        src = cecreturn.PopenLines([py, "-c", "import subprocess, sys; "
                                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                                    "print(p.pid, flush=True); sys.stderr.write('Device was disconnected.\\n'); "
                                    "sys.stderr.flush()"])
        kind, line = src.readline(10.0)
        self.assertEqual(kind, "line")
        gpid = int(line)
        t0 = time.monotonic()
        diag = src.diagnosis()                               # le petit-enfant garde stderr : attente bornée
        self.assertLessEqual(time.monotonic() - t0, cecreturn.FINISH_WAIT_S + 1.0)
        self.assertEqual(diag, {"error": "device_disconnected", "exit_status": 0})
        t0 = time.monotonic()
        src.close()
        self.assertLess(time.monotonic() - t0, 5)
        deadline = time.monotonic() + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(gpid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = False
        if alive:                                            # zombie pas encore récolté : vérifier l'état
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(gpid)], capture_output=True, text=True).stdout
            self.assertTrue(st.strip() == "" or st.strip().startswith("Z"), st)
        self.assertTrue(src.p.stdout.closed and src.p.stderr.closed)


class TestHealthWarning(unittest.TestCase):
    def run_health(self, state, env=None, manifest=None):
        cfg = config.Config(env or {})

        def runner(argv, timeout):
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, (state if argv[2] == UNIT else "active") + "\n"
            return 1, ""
        with mock.patch.object(health, "verify_manifest", return_value=manifest or {"ok": True, "error": None}), \
                mock.patch.object(health.netstate, "snapshot", return_value={"mode": "wifi"}), \
                mock.patch.object(health.ratings, "health_view", return_value={}):
            return health.run(cfg, runner=runner, check_http=False)

    def test_failed_or_inactive_unit_warns_when_enabled(self):
        for state in ("failed", "inactive"):
            with self.subTest(state):
                rep = self.run_health(state)
                self.assertIn("cec_return_failed", rep["warnings"])
                self.assertTrue(rep["ok"])                            # facultatif : avertit sans renverser « ok »
        self.assertNotIn("cec_return_failed", self.run_health("active")["warnings"])
        for env in ({"OPENTV_CEC_RETURN_RECOVERY": "0"}, {"OPENTV_CEC_BOOTSTRAP": "0"}):
            self.assertNotIn("cec_return_failed", self.run_health("inactive", env)["warnings"])
        self.assertNotIn("cec_return_failed",
                         self.run_health("inactive", manifest={"ok": False, "error": "manifest_missing"})["warnings"])


# --------------------------------------------------------------------------- maintien neutre après la reprise (0.7.4)
# Terrain (Samsung, 2026-10-31) : la séquence exacte est détectée, `announced` rend les touches… quelques secondes à
# minutes seulement. Un répondeur <Report Power Status>, une libération/réclamation + annonce : sans effet. Un
# `cec-follower` gardait la télécommande vivante ; la capture a isolé son seul apport utile : un <Poll> neutre vers la
# TV toutes les ~15 s. Canari `cec-ctl -d /dev/cec0 -s -t 0 -P` toutes les 15 s, sans suiveur ni autre réponse :
# six touches > 5 min (Alex). <Poll> n'annonce rien, ne réveille rien, ne change ni alimentation ni source.
KA = "cec_return_keepalive"


def own_poll(nack=False):
    """Notre <Poll> tel que le moniteur `-s -r -m` le montre (statut imprimé seulement s'il n'est pas OK)."""
    return ["Transmitted by Playback Device 1 to TV (4 to 0): POLL"] + \
        (["\tTx, Not Acknowledged (1), Max Retries"] if nack else []) + [raw_line([0x40])]


def tv_poll():
    return ["Received from TV to Playback Device 1 (0 to 4): POLL", raw_line([0x04])]


def returned(pa="1.0.0.0", youtube_s=60.0):
    return at(5, active_source()) + at(youtube_s, routing_change("0.0.0.0", pa)) + at(0.05, stream_path(pa))


def traffic(seconds, every=5.0, lines=None):
    """Trafic ordinaire de la TV (question d'alimentation toutes les 5 s, comme la capture)."""
    out = []
    for _ in range(int(seconds // every)):
        out += at(every, (lines or power_poll)())
    return out


class TestKeepalive(RBase):
    def ka_logs(self):
        return [e for e in self.logs if e.get("event") == KA]

    def states(self):
        return [(e["state"], e.get("reason")) for e in self.ka_logs()]

    def t_announced(self, bus=None):
        return [t for t, a in (bus or self.bus).at if "--menu-status" in a]

    # ----------------------------------------------------------------- jamais armé sans retour exact annoncé
    def test_no_poll_without_exact_sequence_and_announcement(self):
        for name, script in sorted(NEG.items()):
            with self.subTest(name):
                self.bus.calls.clear()
                self.bus.at.clear()
                self.logs.clear()
                self.watch(at(0, state_event()) + script() + traffic(300))
                self.assertEqual(self.bus.polls(), [], name)
                self.assertEqual(self.ka_logs(), [], name)
        self.bus.at.clear()
        self.watch(at(0, state_event()) + traffic(900, lines=key))     # touches seules, 15 min : rien
        self.assertEqual(self.bus.polls(), [])
        self.assertEqual(self.bus.calls, [])

    def test_failed_refused_busy_or_changed_announcement_never_arms(self):
        for label in ("refused_tv_not_on", "failed", "busy", "adapter_changed", "rate_limited"):
            with self.subTest(label):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                held = None
                if label == "refused_tv_not_on":
                    bus.power = "standby"
                elif label == "failed":
                    bus.fail = {"--active-source"}
                elif label == "busy":
                    held = cecboot._lock(self.state)
                elif label == "adapter_changed":
                    bus.ad["/dev/cec0"]["state"] = "foreign"
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(300))],
                                   bus=bus)
                if label == "rate_limited":
                    mon.announced.extend([self.clock.t] * cecreturn.MAX_PER_HOUR)
                try:
                    self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                finally:
                    if held is not None:
                        os.close(held)
                self.assertEqual([e["result"] for e in self.recoveries()], [label])
                self.assertEqual(bus.polls(), [], label)
                self.assertEqual(self.ka_logs(), [], label)

    def test_service_or_monitor_restart_never_arms(self):
        """Retour exact coupé par un redémarrage : rien ; keepalive armé puis redémarrage : jamais réarmé ensuite."""
        m1 = at(0, state_event()) + at(5, active_source()) + at(60, routing_change())
        m2 = at(0, state_event()) + at(0.05, stream_path()) + traffic(300)
        self.assertIsNone(self.monitor([m1, m2]).serve(max_cycles=2))
        self.assertEqual(self.bus.polls(), [])
        self.assertEqual(self.ka_logs(), [])
        # armé (mémoire seulement) puis fin du moniteur : désarmé ; le moniteur suivant ne sonde jamais
        self.logs.clear()
        armed = at(0, state_event()) + returned() + traffic(60)
        after = at(0, state_event()) + traffic(600)
        self.assertIsNone(self.monitor([armed, after]).serve(max_cycles=2))
        t_end = max(self.bus.polls())
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])
        restart = [e for e in self.logs if e.get("event") == "cec_return_watch_end"][0]
        self.assertEqual(restart["reason"], "eof")
        self.assertLess(t_end, self.t_announced()[0] + 61)           # aucune sonde pendant les 600 s suivantes
        self.assertEqual([n for n in os.listdir(self.state) if n != "lock"], [])   # mémoire seulement

    # ----------------------------------------------------------------- armé : cadence fixe 15 s, <Poll> seul
    def test_exact_return_announced_arms_first_poll_at_15s_then_fixed_cadence(self):
        self.watch(at(0, state_event()) + returned() + traffic(300) + traffic(120, every=7, lines=key))
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
        t_ann = self.t_announced()[0]
        polls = self.bus.polls()
        self.assertEqual(cecreturn.KEEPALIVE_INTERVAL_S, 15.0)
        self.assertGreaterEqual(polls[0] - t_ann, cecreturn.KEEPALIVE_INTERVAL_S)       # jamais avant 15 s
        self.assertLessEqual(polls[0] - t_ann, cecreturn.KEEPALIVE_INTERVAL_S + 0.5)
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertTrue(all(cecreturn.KEEPALIVE_INTERVAL_S - 1e-6 <= g <= cecreturn.KEEPALIVE_INTERVAL_S + 0.5
                            for g in gaps), gaps)
        self.assertGreaterEqual(len(polls), 27)                         # ~420 s / 15 s, touches comprises
        # rien d'autre que relecture + <Poll> après l'annonce : jamais d'<Active Source>/<Menu Status>/question
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        after = [a for t, a in self.bus.at if t > t_ann]
        self.assertEqual(after, [["cec-ctl", "-d", "/dev/cec0"], ["cec-ctl", "-d", "/dev/cec0"] + POLL_ARGV_OPTS]
                         * len(polls))
        self.assertEqual(wake_args(self.bus), [])
        # journal sobre : armé, actif (1re sonde), puis désarmé à la fin du moniteur — jamais une ligne par sonde
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])
        armed = self.ka_logs()[0]
        self.assertEqual(armed, {"event": KA, "state": "armed", "device": "/dev/cec0", "physical_address": "1.0.0.0",
                                 "interval_s": 15.0})
        self.assertEqual(self.ka_logs()[-1]["polls"], len(polls))

    def test_poll_argv_is_exact_dynamic_and_never_an_announcement(self):
        self.assertEqual(cecreturn.poll_argv("/dev/cec3"), ["cec-ctl", "-d", "/dev/cec3"] + POLL_ARGV_OPTS)
        for dev in ("/dev/cec", "/dev/cecx", "cec0", "/dev/../dev/cec0", "/dev/cec0;rm", None):
            with self.assertRaises(ValueError):
                cecreturn.poll_argv(dev)
        argv = cecreturn.poll_argv("/dev/cec0")
        for bad in cecboot.NEVER_EMIT + cecboot.ANNOUNCE_OPTS + ("--give-device-power-status", "-m", "-M",
                                                                 "--follower", "--from", "-f", "-v"):
            self.assertNotIn(bad, argv)
        # garde explicite : toute dérive de l'argv (annonce, réveil, alimentation, autre cible) est refusée
        for opts in (("-s", "-t", "0", "-P", "--active-source"), ("-s", "-t", "0", "--image-view-on"),
                     ("-s", "-t", "0", "--give-device-power-status"), ("-s", "-t", "0", "--menu-status"),
                     ("-s", "-t", "4", "-P"), ("-s", "-P"), ("-s", "-t", "0", "-P", "--wake"),
                     ("-s", "-t", "0", "--standby")):
            with self.subTest(opts), mock.patch.object(cecreturn, "POLL_OPTS", opts):
                with self.assertRaises(ValueError):
                    cecreturn.poll_argv("/dev/cec0")
        # adaptateur et adresse dynamiques : relecture puis <Poll> sur l'adaptateur découvert
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                  "/dev/cec1": {"pa": "2.1.0.0", "state": "ours"}})
        mon = self.monitor([at(0, state_event("2.1.0.0")) + returned("2.1.0.0") + traffic(40)], bus=bus)
        self.assertEqual(mon.discover(), ("/dev/cec1", "2.1.0.0", 0x0010))
        self.assertEqual(mon.watch("/dev/cec1", "2.1.0.0", 0x0010), ("eof", True))
        polls = [a for _, a in bus.at if "-P" in a]
        self.assertEqual(polls, [["cec-ctl", "-d", "/dev/cec1"] + POLL_ARGV_OPTS] * 2)
        i = next(i for i, (_, a) in enumerate(bus.at) if "-P" in a)
        self.assertEqual(bus.at[i - 1][1], ["cec-ctl", "-d", "/dev/cec1"])     # relue juste avant chaque sonde

    def test_poll_output_parser_is_exact_and_fail_closed(self):
        p = cecreturn.poll_acked_by
        self.assertEqual(p(POLL_OK), 4)
        self.assertEqual(p(POLL_OK.replace("Playback Device 1", "Playback Device 2").replace("4 to 0", "8 to 0")), 8)
        for bad in (POLL_NACK, "", None, "Timed out\n", "\nAdapter is unconfigured, please configure it first.\n",
                    POLL_OK.replace("\tSequence: 117 Tx Timestamp: 8123.456s\n", ""),
                    POLL_OK + "\tTx, Arbitration Lost (1), Error (1)\n", POLL_OK + "garbage\n",
                    POLL_OK.replace("to TV (4 to 0)", "to Audio System (4 to 5)"),
                    POLL_OK.replace("4 to 0", "0 to 0"), POLL_OK.replace("4 to 0", "15 to 0"),
                    POLL_OK.replace("POLL", "ACTIVE_SOURCE (0x82):"), POLL_OK * 2,
                    "CEC_TRANSMIT returned -1 (No such device)\n" + POLL_OK):
            with self.subTest(bad):
                self.assertIsNone(p(bad))

    @bounded()
    def test_poll_uses_the_existing_lock_and_waits_bounded(self):
        held = [None]

        def reply(argv):
            fd = cecboot._lock(self.state)                          # pendant la sonde, le verrou CEC est tenu
            self.assertIsNone(fd)
            return 0, POLL_OK
        self.bus.poll_reply = reply
        mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(40) + traffic(60))])
        orig = mon.poll

        def poll(dev, pa, mask, _orig=orig):
            if len(self.bus.polls()) == 2 and held[0] is None:      # 3e sonde : minuterie/--activate en cours
                held[0] = cecboot._lock(self.state)
                self.sleeps.clear()
                held.append(self.clock.t)
            return _orig(dev, pa, mask)
        mon.poll = poll
        try:
            self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        finally:
            os.close(held[0])
        self.assertEqual(len(self.bus.polls()), 2)                      # aucune sonde sans le verrou
        self.assertEqual(self.states()[-1], ("disarmed", "lock_busy"))
        self.assertTrue(self.sleeps and set(self.sleeps) == {cecreturn.LOCK_POLL_S}, self.sleeps)
        self.assertLessEqual(sum(self.sleeps), cecreturn.KEEPALIVE_LOCK_WAIT_S)
        self.assertLess(cecreturn.KEEPALIVE_LOCK_WAIT_S + cecreturn.KEEPALIVE_BUDGET_S,
                        cecreturn.KEEPALIVE_INTERVAL_S)                 # une sonde finit avant la suivante
        fd = cecboot._lock(self.state)                                  # libéré après chaque sonde
        self.assertIsNotNone(fd)
        os.close(fd)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)

    @bounded()
    def test_failed_or_unacknowledged_poll_disarms_immediately(self):
        def hang(argv):
            self.clock.t += cecboot.TX_TIMEOUT_S + cecboot.KILL_GRACE_S
            return 124, ""
        cases = {"poll_nack": (0, POLL_NACK), "poll_failed": (1, ""), "poll_failed_timeout": hang,
                 "poll_failed_rc_with_ok_text": (1, POLL_OK),
                 "poll_failed_empty": (0, ""), "poll_failed_garbage": (0, "Timed out\n"),
                 "la_changed": (0, POLL_OK.replace("Playback Device 1", "Playback Device 2").replace("4 to 0",
                                                                                                    "8 to 0"))}
        for label, reply in sorted(cases.items()):
            with self.subTest(label):
                self.logs.clear()
                self.bus.at.clear()
                self.bus.calls.clear()
                n = [0]

                def r(argv, _reply=reply):
                    n[0] += 1
                    if n[0] == 1:
                        return 0, POLL_OK
                    return _reply(argv) if callable(_reply) else _reply
                self.bus.poll_reply = r
                self.watch(at(0, state_event()) + returned() + traffic(300))
                self.assertEqual(len(self.bus.polls()), 2, label)       # la sonde en échec est la dernière
                want = {"poll_nack": "poll_nack", "la_changed": "adapter_changed"}.get(label, "poll_failed")
                self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", want)])
                self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)            # aucune annonce de « secours »
                self.assertEqual(wake_args(self.bus), [])

    @bounded()
    def test_adapter_or_logical_address_change_fails_closed_before_polling(self):
        changes = {"pa": ("pa", "2.0.0.0"), "mask": ("mask", "0x0100"), "foreign": ("state", "foreign"),
                   "empty": ("state", "empty"), "unplugged": ("pa", "f.f.f.f"), "unreadable": ("unreadable", True)}
        for label, (k, v) in sorted(changes.items()):
            with self.subTest(label):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})

                def r(argv, _bus=bus, _k=k, _v=v):
                    _bus.ad["/dev/cec0"][_k] = _v                     # change juste après la 1re sonde
                    return 0, POLL_OK
                bus.poll_reply = r
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(300))],
                                   bus=bus)
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                self.assertEqual(len(bus.polls()), 1, label)
                self.assertEqual(self.states()[-1], ("disarmed", "adapter_changed"))
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)

    # ----------------------------------------------------------------- arrêts : la TV ou une autre source reprend la main
    @bounded()
    def test_every_stop_condition_disarms_and_never_reclaims(self):
        stops = {
            "native_app": (active_source("0.0.0.0"), "native_app"),
            "other_hdmi_source": (active_source("3.0.0.0", src=8), "other_source"),
            "tv_other_active_source": (active_source("2.0.0.0"), "other_source"),
            "foreign_claims_our_pa": (active_source("1.0.0.0", src=8), "other_source"),
            "own_active_source_other_pa": (active_source("2.0.0.0", src=4, tx=True), "own_unexpected"),
            "routing_away": (routing_change("1.0.0.0", "2.0.0.0"), "routed_away"),
            "routing_tv_root_to_other": (routing_change("0.0.0.0", "2.0.0.0"), "routed_away"),
            "routing_other_to_other": (routing_change("2.0.0.0", "3.0.0.0"), "routed_away"),
            "routing_information_away": (frame(0x81, "ROUTING_INFORMATION", [("phys-addr", "2.0.0.0")],
                                               pa_bytes("2.0.0.0")), "routed_away"),
            "stream_path_other": (stream_path("3.0.0.0"), "stream_path_away"),
            "standby_broadcast": (frame(0x36, "STANDBY"), "standby"),
            "standby_to_us": (frame(0x36, "STANDBY", dst=4), "standby"),
            "inactive_source": (frame(0x9d, "INACTIVE_SOURCE", [("phys-addr", "3.0.0.0")], pa_bytes("3.0.0.0"),
                                      src=8, dst=0), "inactive_source"),
            "lost_messages": (["", "1.0s: Event: Lost 3 messages"], "lost"),
            "garbage_line": (["\x1b[2Jbogus"], "invalid_unknown_line"),
            "partial_frame": (stream_path()[:-1] + key()[:1], "invalid_partial_frame"),
            "malformed_watched": (stream_path("2.0.0.0")[:2] + [raw_line([0x0f, 0x86, 0x10, 0x00])],
                                  "invalid_argument_mismatch"),
            "same_state_event": (state_event(initial=False), "adapter_state"),
            "adapter_state_change": (state_event("f.f.f.f", 0x0000, initial=False), "adapter_state"),
        }
        for name, (lines, reason) in sorted(stops.items()):
            with self.subTest(name):
                self.logs.clear()
                self.bus.at.clear()
                self.bus.calls.clear()
                self.watch(at(0, state_event()) + returned() + traffic(20) + at(1, lines) + traffic(600))
                polls = self.bus.polls()
                self.assertEqual(len(polls), 1, name)                    # 1re sonde à 15 s, plus rien ensuite
                st = self.states()
                self.assertEqual(st[:2], [("armed", None), ("active", None)], name)
                self.assertEqual(st[2:], [("disarmed", reason)], name)
                self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ, name)      # jamais de réclamation ni de réveil
                self.assertEqual(wake_args(self.bus), [])

    def test_own_and_tv_polls_and_ordinary_traffic_keep_it_armed(self):
        parser = cecreturn.Parser()
        items = [i for line in own_poll() + own_poll(nack=True) + tv_poll() for i in parser.feed(line)]
        self.assertEqual([i[0] for i in items], ["frame"] * 3)
        ka = cecreturn.Keepalive("1.0.0.0", 0x10)
        for _, fr in items:
            self.assertIsNone(ka.stop_reason(fr))
        keep = [own_poll, tv_poll, power_poll, key, lambda: key(0x0d, "back"), lambda: routing_change("0.0.0.0"),
                lambda: stream_path("1.0.0.0"), lambda: active_source("1.0.0.0", src=4, tx=True),
                lambda: frame(0x81, "ROUTING_INFORMATION", [("phys-addr", "1.0.0.0")], pa_bytes("1.0.0.0")),
                lambda: frame(0x89, "VENDOR_COMMAND_WITH_ID", [("vendor-id", "0x0000f0 (Samsung)")],
                              [0x00, 0x00, 0xf0, 0x01], 0, 4)]
        script = at(0, state_event()) + returned()
        for i in range(60):
            script += at(4, keep[i % len(keep)]())
        self.watch(script)
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])
        self.assertGreaterEqual(len(self.bus.polls()), 15)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)

    def test_samsung_home_or_silence_neither_announces_nor_disarms(self):
        """Home Samsung n'émet aucune trame : le maintien continue, jamais d'annonce qui fermerait Home."""
        self.watch(at(0, state_event()) + returned() + at(600, power_poll()))
        polls = self.bus.polls()
        self.assertGreaterEqual(len(polls), 39)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])

    def test_user_keys_never_disarm_an_armed_keepalive(self):
        self.watch(at(0, state_event()) + returned() + traffic(180, every=3, lines=key))
        self.assertGreaterEqual(len(self.bus.polls()), 11)
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])

    def test_other_hdmi_source_is_never_reclaimed(self):
        script = at(0, state_event()) + returned() + traffic(35) + at(1, active_source("3.0.0.0", src=8)) + \
            at(0.2, routing_change("1.0.0.0", "3.0.0.0")) + traffic(1800)
        self.watch(script)
        self.assertEqual(len(self.bus.polls()), 2)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
        self.assertEqual(self.states()[-1], ("disarmed", "other_source"))

    def test_tv_standby_is_never_woken(self):
        script = at(0, state_event()) + returned() + traffic(20) + at(1, frame(0x36, "STANDBY")) + \
            at(2, state_event("f.f.f.f", 0x0000, initial=False))
        self.watch(script)
        self.assertEqual(len(self.bus.polls()), 1)
        self.assertEqual(wake_args(self.bus), [])
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        self.assertEqual(self.states()[-1], ("disarmed", "standby"))

    def test_no_periodic_announcement_over_an_hour_and_bounded_logs(self):
        self.watch(at(0, state_event()) + returned() + traffic(7300, every=60))
        polls = self.bus.polls()
        self.assertGreaterEqual(len(polls), 480)
        self.assertLessEqual(len(polls), 7300 / cecreturn.KEEPALIVE_INTERVAL_S + 1)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)                  # une seule annonce, jamais périodique
        self.assertEqual([o for o in self.bus.opts() if o == "--give-device-power-status"], ANNOUNCE_SEQ[:1])
        st = [s for s, _ in self.states()]
        self.assertEqual(st, ["armed", "active", "active", "active", "disarmed"])   # 1re sonde puis 1/h
        self.assertEqual(cecreturn.KEEPALIVE_STATUS_POLLS * cecreturn.KEEPALIVE_INTERVAL_S, 3600.0)
        allowed = {"event", "state", "device", "physical_address", "interval_s", "polls", "reason"}
        for e in self.ka_logs():
            self.assertTrue(set(e) <= allowed, e)
        self.assertNotIn("Raw", json.dumps(self.logs))

    def test_next_exact_return_rearms_from_zero(self):
        script = at(0, state_event()) + returned() + traffic(40) + returned(youtube_s=120) + traffic(40)
        self.watch(script)
        t1, t2 = self.t_announced()
        polls = self.bus.polls()
        first = [t for t in polls if t < t2]
        self.assertGreaterEqual(first[0] - t1, cecreturn.KEEPALIVE_INTERVAL_S)
        youtube = [t for t in polls if t1 + 41 < t < t2]
        self.assertEqual(youtube, [])                                    # désarmé pendant l'application native
        second = [t for t in polls if t > t2]
        self.assertGreaterEqual(second[0] - t2, cecreturn.KEEPALIVE_INTERVAL_S)
        self.assertEqual([s for s, _ in self.states()], ["armed", "active", "disarmed", "armed", "active",
                                                          "disarmed"])
        self.assertEqual(self.states()[2], ("disarmed", "native_app"))

    def test_refused_return_after_an_earlier_keepalive_stays_disarmed(self):
        mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(40) +
                                      returned(youtube_s=30) + traffic(300))])
        orig, n = mon.recover, [0]

        def recover(dev, pa, _orig=orig):
            n[0] += 1
            if n[0] == 2:
                self.bus.power = "standby"                               # TV éteinte entre-temps
            return _orig(dev, pa)
        mon.recover = recover
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced", "refused_tv_not_on"])
        t2 = [t for t, a in self.bus.at if "--give-device-power-status" in a][1]
        self.assertEqual([t for t in self.bus.polls() if t > t2], [])
        self.assertEqual(wake_args(self.bus), [])

    def test_any_non_announced_attempt_disarms_an_armed_keepalive(self):
        """Défense en profondeur : même si une tentative non annoncée survenait pendant le maintien (busy, refus,
        échec, report, quota…), le maintien est désarmé, jamais prolongé."""
        for result in ("busy", "refused_tv_not_on", "failed", "adapter_changed", "budget_exceeded",
                       "cooldown_deferred", "rate_limited"):
            with self.subTest(result):
                self.logs.clear()
                mon = self.monitor()
                ka = cecreturn.Keepalive("1.0.0.0", 0x10)
                mon._ka_after_attempt(ka, "announced", "/dev/cec0", "1.0.0.0")
                self.assertTrue(ka.armed)
                mon._ka_after_attempt(ka, result, "/dev/cec0", "1.0.0.0")
                self.assertFalse(ka.armed)
                self.assertEqual(self.states(), [("armed", None), ("disarmed", "announce_" + result)])

    def test_opt_out_never_polls(self):
        mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(300))],
                           keepalive=False)
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
        self.assertEqual(self.bus.polls(), [])
        self.assertEqual(self.ka_logs(), [])

    def test_field_trace_replay_keeps_forwarding_transport_alive(self):
        """Rejoue la séquence terrain du 2026-10-31 : retour exact, `announced`, puis > 5 min de trafic Samsung
        ordinaire (question d'alimentation toutes les 5 s, touches), nos <Poll> vus par le moniteur."""
        s = at(0, state_event()) + at(1, key()) + at(5, active_source("0.0.0.0"))
        for _ in range(24):                                              # 2 min de YouTube
            s += at(5, power_poll())
        s += at(5, routing_change("0.0.0.0", "1.0.0.0")) + at(0.05, stream_path("1.0.0.0"))
        for i in range(80):                                              # 6 min 40 après le retour
            s += at(5, power_poll())
            if i % 3 == 2:
                s += at(0.1, own_poll())
            if i % 4 == 1:
                s += at(0.3, key((0x01, 0x02, 0x03, 0x04, 0x00, 0x0d)[i % 6], "k"))
        self.watch(s)
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
        polls = self.bus.polls()
        self.assertGreaterEqual(len(polls), 25)
        gaps = [b - a for a, b in zip(polls, polls[1:])]
        self.assertTrue(all(15 - 1e-6 <= g <= 15.5 for g in gaps), gaps)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])

    def test_detector_semantics_and_monitor_responsiveness_unchanged(self):
        """Pendant le maintien, une nouvelle séquence exacte est toujours détectée et annoncée en quelques secondes."""
        script = at(0, state_event()) + returned() + traffic(100) + returned(youtube_s=20) + traffic(10)
        self.watch(script)
        t_stream = [t for t, line in self.src.delivered if line == stream_path()[0]][-1]
        t_as = [t for t, a in self.bus.at if "--active-source" in a][-1]
        self.assertLessEqual(t_as - t_stream, 5.0)
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced", "announced"])

    def test_module_contract_text(self):
        src = module_source()
        for need in ("KEEPALIVE_INTERVAL_S", "poll_argv", "cecboot._lock", "NEVER_EMIT", "cecboot.tx_acked"):
            self.assertIn(need, src)
        for forbidden in ("follower", "--wait-for-msgs"):
            self.assertNotIn(forbidden, re.sub(r"(?m)#.*$|\"\"\"[\s\S]*?\"\"\"", "", src), forbidden)


# --------------------------------------------------------------------------- revue Stage-1 de b5ce6ca (bloquants)
AS_OK = ACTIVE_SOURCE_TX.format(pa="1.0.0.0")
# rc 0 dans tous les cas (cec-ctl 1.22.1) ; sortie fusionnée stdout+stderr (default_runner : stderr=STDOUT)
AS_BAD = {
    "tx_error": AS_OK + "\tTx, Error (1), Max Retries\n",
    "nack": AS_OK + "\tTx, Not Acknowledged (1), Max Retries\n",
    "arbitration_lost": AS_OK + "\tTx, Arbitration Lost (1), Max Retries\n",
    "low_drive": AS_OK + "\tTx, Low Drive (1), Max Retries\n",
    "aborted": AS_OK + "\tTx, Aborted\n",
    "timeout_status": AS_OK + "\tTx, Timeout\n",
    "stderr_merged_before": "CEC_TRANSMIT returned -1 (No such device)\n" + AS_OK,
    "stderr_merged_after": AS_OK + "Device was disconnected\n",
    "unconfigured": "\nAdapter is unconfigured, please configure it first.\n",
    "empty": "",
    "garbage_suffix": AS_OK + "garbage\n",
    "twice": AS_OK * 2,
    "no_sequence": AS_OK.replace("\tSequence: 51 Tx Timestamp: 7752.318s\n", ""),
    "other_pa": ACTIVE_SOURCE_TX.format(pa="2.0.0.0"),
    "directed_not_broadcast": AS_OK.replace("to all (4 to 15)", "to TV (4 to 0)"),
    "foreign_la": AS_OK.replace("Playback Device 1", "Playback Device 2").replace("(4 to 15)", "(8 to 15)"),
    "unregistered_la": AS_OK.replace("Playback Device 1", "Unregistered").replace("(4 to 15)", "(15 to 15)"),
    "other_opcode": AS_OK.replace("ACTIVE_SOURCE (0x82):", "INACTIVE_SOURCE (0x9d):"),
    "oversize": AS_OK + "\t" + "x" * 5000 + "\n",
    "sequence_malformed": AS_OK.replace("Tx Timestamp: 7752.318s", "Tx Timestamp: pending"),
}


class TestStage1Review(RBase):
    """Blocages de la revue Stage-1 de b5ce6ca : cadence réelle du bus sous contention, armement seulement sur une
    annonce réellement transmise, relecture « le nôtre » complète, routage ailleurs réaliste, file déjà lue d'abord."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states
    t_announced = TestKeepalive.t_announced

    def t_active_source_end(self, bus=None):
        return [t + 0.05 for t, a in (bus or self.bus).at if "--active-source" in a]   # FakeBus : 0,05 s par appel

    # ----------------------------------------------------------------- (1) cadence : depuis la fin du POLL émis
    @bounded()
    def test_transient_lock_contention_never_shortens_a_bus_interval(self):
        """Régression b5ce6ca : l'échéance suivante partait de t0 (avant l'attente du verrou) ; une contention de 2 s
        donnait des écarts réels [17, 13, 15]. Exigé : jamais < 15 s entre deux POLL sur le bus, 1er ≥ 15 s après
        l'annonce, contention comprise (attente du verrou, relecture et POLL lui-même)."""
        for hits in ((1,), (2,), (1, 2, 3), (2, 4), (3,)):
            with self.subTest(hits=hits):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(150))],
                                   bus=bus)
                orig, n, held = mon.poll, [0], {}

                def poll(dev, pa, mask, _orig=orig, _n=n, _held=held):
                    _n[0] += 1
                    if _n[0] in hits:                       # minuterie / --activate tient le verrou ~2 s
                        _held["fd"], _held["t"] = cecboot._lock(self.state), self.clock.t
                    return _orig(dev, pa, mask)

                def sleep(s, _held=held):
                    self.sleep(s)
                    if _held.get("fd") is not None and self.clock.t - _held["t"] >= 2.0:
                        os.close(_held["fd"])
                        _held["fd"] = None
                mon.poll, mon.sleep = poll, sleep
                try:
                    self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                finally:
                    if held.get("fd") is not None:
                        os.close(held["fd"])
                spans = bus.poll_spans
                self.assertGreaterEqual(len(spans), 8)
                # pire cas : notre ACTIVE_SOURCE émise à la fin de son cec-ctl, le POLL au début du sien
                self.assertGreaterEqual(spans[0][0] - self.t_active_source_end(bus)[0],
                                        cecreturn.KEEPALIVE_INTERVAL_S)
                gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]   # début du suivant - fin du précédent
                self.assertTrue(all(g >= cecreturn.KEEPALIVE_INTERVAL_S - 1e-9 for g in gaps), gaps)
                starts = [b[0] - a[0] for a, b in zip(spans, spans[1:])]
                self.assertTrue(all(g <= cecreturn.KEEPALIVE_INTERVAL_S + 2.5 for g in starts), starts)
                self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)

    def test_next_due_is_scheduled_after_the_emitted_poll_returns(self):
        mon = self.monitor()
        ka = cecreturn.Keepalive("1.0.0.0", 0x10)
        ka.arm(self.clock.t)

        def slow_poll(dev, pa, mask):
            self.clock.t += 2.0 + 0.05 + 0.05        # attente du verrou, relecture, cec-ctl POLL
            return "ok"
        mon.poll = slow_poll
        mon._ka_poll(ka, "/dev/cec0", "1.0.0.0", 0x10)
        self.assertEqual(ka.next_due, self.clock.t + cecreturn.KEEPALIVE_INTERVAL_S)

    # ----------------------------------------------------------------- (2) armement : annonce réellement transmise
    @bounded()
    def test_rc0_with_tx_error_or_parasite_output_never_announced_armed_or_polled(self):
        for label, out in sorted(AS_BAD.items()):
            with self.subTest(label):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                bus.tx_out["--active-source"] = out
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(120))],
                                   bus=bus)
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                rec = self.recoveries()
                self.assertEqual([e["result"] for e in rec], ["failed"], label)
                self.assertEqual(rec[0]["steps"], {"active_source": False}, label)
                self.assertIn(rec[0]["tx"], cecboot.TX_CODES, label)
                self.assertNotEqual(rec[0]["tx"], "acked", label)
                self.assertEqual(bus.opts(), ["--give-device-power-status", "--active-source"], label)
                self.assertEqual(bus.polls(), [], label)                  # pas armé, aucun POLL
                self.assertEqual(self.ka_logs(), [], label)
                self.assertEqual(wake_args(bus), [])
                dumped = json.dumps(self.logs)                             # jamais de texte brut de cec-ctl
                for raw in ("Tx,", "Transmit from", "CEC_TRANSMIT", "garbage", "Sequence"):
                    self.assertNotIn(raw, dumped, label)
        codes = {k: cecboot.tx_acked_status(0, v, cecboot.active_source_body("1.0.0.0"), cecboot.BROADCAST, 0x10)
                 for k, v in AS_BAD.items()}
        self.assertEqual((codes["tx_error"], codes["nack"]), ("tx_error", "nack"))

    def test_exact_acknowledged_announcement_arms_and_menu_status_kept(self):
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(40))], bus=bus)
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        rec = self.recoveries()
        self.assertEqual([e["result"] for e in rec], ["announced"])
        self.assertEqual(rec[0]["steps"], {"active_source": True, "menu_status": True})
        self.assertEqual(bus.opts(), ANNOUNCE_SEQ)                        # TV allumée, AS puis Menu Status
        self.assertEqual(self.states()[0], ("armed", None))
        self.assertEqual(len(bus.polls()), 2)
        # Menu Status non acquitté : inchangé (non bloquant, véridique), l'annonce AS acquittée arme toujours
        self.logs.clear()
        bus2 = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        bus2.tx_out["--menu-status"] = MENU_STATUS_TX + "\tTx, Not Acknowledged (1), Max Retries\n"
        mon2 = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(40))], bus=bus2)
        mon2.watch("/dev/cec0", "1.0.0.0", 0x10)
        rec = self.recoveries()
        self.assertEqual((rec[0]["result"], rec[0]["steps"]), ("announced", {"active_source": True,
                                                                              "menu_status": False}))
        self.assertEqual(bus2.opts(), ANNOUNCE_SEQ)

    def test_tx_parser_exact_real_format(self):
        body = cecboot.active_source_body("1.0.0.0")
        st = cecboot.tx_acked_status
        self.assertEqual(st(0, AS_OK, body, cecboot.BROADCAST, 0x10), "acked")
        self.assertEqual(cecboot.tx_acked(AS_OK, body, cecboot.BROADCAST), 4)
        self.assertEqual(st(0, AS_OK, body, cecboot.BROADCAST, None), "acked")
        self.assertEqual(st(1, AS_OK, body, cecboot.BROADCAST, 0x10), "exit_nonzero")
        self.assertEqual(st(124, "", body, cecboot.BROADCAST, 0x10), "timeout")
        self.assertEqual(st(0, AS_BAD["foreign_la"], body, cecboot.BROADCAST, 0x10), "foreign_la")
        self.assertEqual(st(0, AS_BAD["foreign_la"], body, cecboot.BROADCAST, 0x100), "acked")
        for label in ("stderr_merged_before", "garbage_suffix", "other_pa", "empty", "oversize", "twice",
                      "sequence_malformed"):
            self.assertEqual(st(0, AS_BAD[label], body, cecboot.BROADCAST, 0x10), "unverified", label)
        # le parseur du POLL est le même (aucune divergence de format)
        self.assertEqual(cecreturn.poll_acked_by(POLL_OK), 4)
        self.assertEqual(st(0, POLL_NACK, cecreturn.POLL_BODY, cecreturn.TV, 0x10), "nack")
        self.assertEqual(st(0, MENU_STATUS_TX, cecboot.MENU_STATUS_BODY, cecreturn.TV, 0x10), "acked")

    def test_recover_direct_result_is_truthful(self):
        self.bus.tx_out["--active-source"] = AS_BAD["tx_error"]
        res = self.solo().recover("/dev/cec0", "1.0.0.0")
        self.assertEqual((res["result"], res["tx"], res["steps"]), ("failed", "tx_error", {"active_source": False}))
        self.assertNotIn("--menu-status", self.bus.opts())

    # ----------------------------------------------------------------- (3) relecture : identité complète « le nôtre »
    @bounded()
    def test_same_pa_and_mask_but_other_identity_refuses_poll_and_disarms(self):
        changes = {"osd_name": {"osd": "SecretBox"}, "device_type_recorder": {"ptype": "Recorder"},
                   "device_type_tuner": {"ptype": "Tuner"}, "osd_and_type": {"osd": "Other", "ptype": "Recorder"},
                   "driver_not_vc4": {"osd": "OpenTV", "driver": "dw_hdmi"}}
        for label, change in sorted(changes.items()):
            with self.subTest(label):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})

                def r(argv, _bus=bus, _c=change):
                    _bus.ad["/dev/cec0"].update(_c)                    # juste après la 1re sonde
                    return 0, POLL_OK
                bus.poll_reply = r
                mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(120))],
                                   bus=bus)
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                info = cecboot.parse_info(bus.render("/dev/cec0"))
                self.assertEqual((info["physical_address"], info["mask"]), ("1.0.0.0", 0x10), label)  # même PA+masque
                self.assertEqual(len(bus.polls()), 1, label)          # 2e sonde refusée avant toute émission
                self.assertEqual(self.states()[-1], ("disarmed", "adapter_changed"), label)
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)

    # ----------------------------------------------------------------- (4) routage ailleurs réaliste
    @bounded()
    def test_realistic_routing_change_away_disarms(self):
        for orig, new in (("0.0.0.0", "2.0.0.0"), ("2.0.0.0", "3.0.0.0"), ("1.0.0.0", "2.0.0.0"),
                          ("0.0.0.0", "1.1.0.0")):
            with self.subTest((orig, new)):
                self.logs.clear()
                self.bus.at.clear()
                self.bus.calls.clear()
                self.watch(at(0, state_event()) + returned() + traffic(20) + at(1, routing_change(orig, new)) +
                           traffic(300))
                self.assertEqual(len(self.bus.polls()), 1)
                self.assertEqual(self.states()[-1], ("disarmed", "routed_away"))
                self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)              # jamais de reprise
        ka = cecreturn.Keepalive("1.0.0.0", 0x10)
        for orig, new, want in (("0.0.0.0", "2.0.0.0", "routed_away"), ("2.0.0.0", "3.0.0.0", "routed_away"),
                                ("2.0.0.0", "1.0.0.0", None), ("0.0.0.0", "1.0.0.0", None)):
            parser = cecreturn.Parser()
            items = [i for line in routing_change(orig, new) for i in parser.feed(line)]
            self.assertEqual([i[0] for i in items], ["frame"])
            self.assertEqual(ka.stop_reason(items[0][1]), want, (orig, new))

    # ----------------------------------------------------------------- non-bloquant strict : file déjà lue d'abord
    @bounded()
    def test_active_source_already_buffered_disarms_before_any_poll(self):
        """La trame <Active Source> de la TV arrive au moment même où l'échéance du POLL expire (select() rend la main
        sur délai, la ligne est déjà dans le tampon). Elle doit être lue et désarmer AVANT tout POLL."""
        for frame_lines, reason in ((active_source("0.0.0.0"), "native_app"),
                                    (active_source("3.0.0.0", src=8), "other_source"),
                                    (routing_change("0.0.0.0", "2.0.0.0"), "routed_away"),
                                    (frame(0x36, "STANDBY"), "standby")):
            with self.subTest(reason):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                src = RaceLines(self.clock, at(0, state_event()) + returned() + at(120, power_poll()),
                                frame_lines, lambda t, _b=bus: self._due_reached(_b, t))
                mon = self.monitor([src], bus=bus)
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                self.assertTrue(src.fired)
                self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
                self.assertEqual(bus.polls(), [], reason)                     # aucun POLL après la trame
                self.assertEqual(self.states(), [("armed", None), ("disarmed", reason)])

    def _due_reached(self, bus, t):
        menu = [x for x, a in bus.at if "--menu-status" in a]
        return bool(menu) and t >= menu[0] + cecreturn.KEEPALIVE_INTERVAL_S

    def test_popen_lines_nonblocking_read_returns_buffered_lines_then_timeout(self):
        code = "import sys,time;sys.stdout.write('a\\nb\\nc\\n');sys.stdout.flush();time.sleep(30)"
        src = cecreturn.PopenLines([sys.executable, "-I", "-c", code])
        try:
            self.assertEqual(src.readline(10.0), ("line", "a"))
            t0 = time.monotonic()
            self.assertEqual(src.readline(0.0), ("line", "b"))            # déjà dans le tampon : immédiat
            self.assertEqual(src.readline(0.0), ("line", "c"))
            self.assertEqual(src.readline(0.0), ("timeout", None))         # rien d'autre : jamais bloquant
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            src.close()


class RaceLines(FakeLines):
    """select() expire exactement à l'échéance (délai > 0) au moment où une trame arrive : la trame `race` est alors
    déjà dans le tampon (lisible sans attente), mais aucun délai positif ne la rend avant l'échéance."""

    def __init__(self, clock, script, race, trigger):
        super().__init__(clock, script)
        self.race, self.trigger, self.fired = list(race), trigger, False

    def readline(self, timeout):
        kind, line = super().readline(timeout)
        if kind == "timeout" and not self.fired and self.trigger(self.clock.t):
            self.fired = True
            self.script = [(0.0, x) for x in self.race] + self.script
            self.due = self.clock.t
        return kind, line


# --------------------------------------------------------------------------- revue Stage-2 de 67dbddc (bloquant 1)
class TestStage2LastMomentDrain(RBase):
    """Course du maintien pendant la contention : watch() vidait la file AVANT l'échéance, puis Monitor.poll() pouvait
    attendre le verrou (≤ 3 s) et relire l'adaptateur (≤ budget) puis émettre sans relire le moniteur. Une trame d'arrêt
    arrivée pendant cette attente laissait partir un POLL. Exigé : relecture non bloquante des lignes déjà reçues APRÈS
    le verrou et la relecture de l'adaptateur, juste avant l'émission ; mêmes Parser/Keepalive ; désarmé ⇒ aucun POLL.

    Sonde déterministe : à l'échéance, une autre passe CEC tient le verrou HOLD_S ; la trame arrive ARRIVE_S plus tard
    (pendant l'attente) ou pendant une relecture lente de l'adaptateur."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states
    t_announced = TestKeepalive.t_announced
    HOLD_S, ARRIVE_S = 2.0, 0.5

    def run_case(self, lines, where="lock", tail=None, end_stream=False, partial_line=False):
        """-> (moniteur, résultat de watch, instant d'arrivée de la trame). `where` : « lock » (verrou tenu HOLD_S,
        trame à +ARRIVE_S) ou « read » (relecture de l'adaptateur de 4 s, trame à +1 s)."""
        self.logs.clear()
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.rbus = bus
        src = FakeLines(self.clock, at(0, state_event()) + returned() + (tail if tail is not None else traffic(60)))
        self.src = src
        slow = {"on": False}

        def runner(argv, timeout):
            if slow["on"] and argv == ["cec-ctl", "-d", "/dev/cec0"]:
                slow["on"] = False
                self.clock.t += 4.0                                     # relecture lente (≤ budget)
            return bus(argv, timeout)
        mon = self.monitor([src], bus=runner)
        orig, held, inj = mon.poll, {}, {}

        def poll(dev, pa, mask):
            if "t" not in inj:
                inj["t"] = self.clock.t + (self.ARRIVE_S if where == "lock" else 1.0)
                if where == "lock":
                    held["fd"], held["t"] = cecboot._lock(self.state), self.clock.t
                else:
                    slow["on"] = True
                if partial_line:
                    src.partial = lambda: True                          # octets sans fin de ligne déjà reçus
                if end_stream:
                    src.script = []                                     # fin du moniteur pendant l'attente
                else:
                    src.script = [(0.0, x) for x in lines] + src.script
                    src.due = inj["t"]
            return orig(dev, pa, mask)

        def sleep(s):
            self.sleep(s)
            if held.get("fd") is not None and self.clock.t - held["t"] >= self.HOLD_S:
                os.close(held["fd"])
                held["fd"] = None
        mon.poll, mon.sleep = poll, sleep
        try:
            res = mon.watch("/dev/cec0", "1.0.0.0", 0x10)
        finally:
            if held.get("fd") is not None:
                os.close(held["fd"])
        self.assertIn("t", inj)
        return mon, res, inj["t"]

    STOPS = {
        "native_app": (active_source("0.0.0.0"), "native_app"),
        "other_source": (active_source("3.0.0.0", src=8), "other_source"),
        "foreign_claims_our_pa": (active_source("1.0.0.0", src=8), "other_source"),
        "standby": (frame(0x36, "STANDBY"), "standby"),
        "routed_away": (routing_change("1.0.0.0", "2.0.0.0"), "routed_away"),
        "stream_path_away": (stream_path("3.0.0.0"), "stream_path_away"),
        "inactive_source": (frame(0x9d, "INACTIVE_SOURCE", [("phys-addr", "3.0.0.0")], pa_bytes("3.0.0.0"),
                                  src=8, dst=0), "inactive_source"),
        "lost": (["", "1.0s: Event: Lost 3 messages"], "lost"),
        "garbage": (["\x1b[2Jbogus"], "invalid_unknown_line"),
        "malformed": (stream_path("2.0.0.0")[:2] + [raw_line([0x0f, 0x86, 0x10, 0x00])], "invalid_argument_mismatch"),
        "same_state_event": (state_event(initial=False), "adapter_state"),
        "after_neutral_traffic": (power_poll() + key() + active_source("0.0.0.0"), "native_app"),
    }

    @bounded()
    def test_stop_frame_arriving_during_lock_wait_or_adapter_read_prevents_the_poll(self):
        for where in ("lock", "read"):
            for name, (lines, reason) in sorted(self.STOPS.items()):
                with self.subTest(where=where, case=name):
                    mon, res, t_frame = self.run_case(lines, where)
                    bus = self.rbus
                    self.assertEqual(res, ("eof", True), name)
                    self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
                    self.assertEqual(bus.polls(), [], "POLL émis malgré %s déjà reçu (%s)" % (name, where))
                    self.assertEqual(self.states(), [("armed", None), ("disarmed", reason)], name)
                    # le verrou a bien été pris et l'adaptateur relu AVANT la relecture finale du moniteur
                    t_ann = self.t_announced(bus)[0]
                    reads = [t for t, a in bus.at if a == ["cec-ctl", "-d", "/dev/cec0"] and t > t_ann]
                    self.assertEqual(len(reads), 1, name)
                    self.assertGreaterEqual(self.clock.t, t_frame)
                    self.assertEqual(bus.opts(), ANNOUNCE_SEQ)              # jamais de reprise ni de réveil
                    self.assertEqual(wake_args(bus), [])
                    fd = cecboot._lock(self.state)                          # verrou libéré malgré l'annulation
                    self.assertIsNotNone(fd)
                    os.close(fd)

    @bounded()
    def test_adapter_state_change_or_monitor_end_during_wait_ends_without_poll(self):
        for where in ("lock", "read"):
            with self.subTest(where=where, case="state_changed"):
                _, res, _ = self.run_case(state_event("f.f.f.f", 0x0000, initial=False), where)
                self.assertEqual(res, ("adapter_state_changed", True))
                self.assertEqual(self.rbus.polls(), [])
                self.assertEqual(self.states(), [("armed", None), ("disarmed", "adapter_state")])
            with self.subTest(where=where, case="eof"):
                _, res, _ = self.run_case([], where, end_stream=True)
                self.assertEqual(res, ("eof", True))
                self.assertEqual(self.rbus.polls(), [])
                self.assertEqual(self.states(), [("armed", None), ("disarmed", "monitor_end")])

    @bounded()
    def test_neutral_traffic_during_wait_keeps_the_poll_and_cadence(self):
        """Touches, questions d'alimentation, POLL de la TV/nôtres, routage vers NOUS lus au dernier moment : le POLL
        part (même contention), jamais d'annonce, cadence ≥ 15 s depuis la fin de chaque POLL."""
        neutral = power_poll() + key() + tv_poll() + own_poll() + routing_change("0.0.0.0", "1.0.0.0") + \
            stream_path("1.0.0.0") + active_source("1.0.0.0", src=4, tx=True)
        for where in ("lock", "read"):
            with self.subTest(where=where):
                _, res, t_frame = self.run_case(neutral, where, tail=traffic(120))
                bus = self.rbus
                self.assertEqual(res, ("eof", True))
                spans = bus.poll_spans
                self.assertGreaterEqual(len(spans), 6)
                self.assertGreaterEqual(spans[0][0], t_frame)               # émis après la lecture des lignes
                gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
                self.assertTrue(all(g >= cecreturn.KEEPALIVE_INTERVAL_S - 1e-9 for g in gaps), gaps)
                self.assertEqual(self.states(), [("armed", None), ("active", None), ("disarmed", "monitor_end")])
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)
                delivered = [line for _, line in self.src.delivered]
                self.assertEqual(delivered.count(power_poll()[0]), 1 + 120 // 5)   # chaque ligne lue une fois

    @bounded()
    def test_frame_still_incomplete_at_the_last_moment_cancels_the_poll(self):
        """En-tête d'une trame lu, `Raw:` pas encore : son contenu (peut-être une ACTIVE_SOURCE) est inconnu ⇒ pas
        d'émission (échec fermé) ; jamais d'attente bloquante pour la compléter."""
        for label, lines, partial_line in (("header_without_raw", active_source("0.0.0.0")[:2], False),
                                           ("line_without_newline", [], True)):
            with self.subTest(label):
                _, res, _ = self.run_case(lines, "lock", partial_line=partial_line)
                self.assertEqual(res, ("eof", True))
                self.assertEqual(self.rbus.polls(), [])
                self.assertEqual(self.states(), [("armed", None), ("disarmed", "frame_pending")])

    def test_popen_lines_reports_a_line_still_being_written(self):
        code = "import sys,time;sys.stdout.write('a\\nRecei');sys.stdout.flush();time.sleep(30)"
        src = cecreturn.PopenLines([sys.executable, "-I", "-c", code])
        try:
            self.assertEqual(src.readline(10.0), ("line", "a"))
            self.assertEqual(src.readline(0.0), ("timeout", None))
            self.assertTrue(src.partial())
        finally:
            src.close()
        src = cecreturn.PopenLines([sys.executable, "-I", "-c", code.replace("Recei", "")])
        try:
            self.assertEqual(src.readline(10.0), ("line", "a"))
            self.assertEqual(src.readline(0.0), ("timeout", None))
            self.assertFalse(src.partial())
        finally:
            src.close()

    @bounded()
    def test_last_moment_drain_is_bounded_and_fails_closed(self):
        flood = []
        for _ in range(cecreturn.PRE_TX_DRAIN_MAX // 2 + 1):
            flood += power_poll()                                      # 2 lignes par trame ordinaire
        _, res, _ = self.run_case(flood + active_source("0.0.0.0"), "lock")
        self.assertEqual(res, ("eof", True))
        self.assertEqual(self.rbus.polls(), [])
        self.assertEqual(self.states(), [("armed", None), ("disarmed", "drain_overflow")])
        self.assertGreaterEqual(cecreturn.PRE_TX_DRAIN_MAX, 1024)

    def test_last_moment_hook_reads_only_without_waiting(self):
        """La relecture finale n'appelle readline qu'avec un délai nul (jamais bloquante ni boucle d'attente)."""
        timeouts = []
        src = FakeLines(self.clock, at(0, state_event()) + returned() + traffic(40))
        orig = src.readline

        def readline(timeout):
            timeouts.append((len(self.bus.polls()), timeout))
            return orig(timeout)
        src.readline = readline
        mon = self.monitor([src])
        orig_poll = mon.poll

        def poll(dev, pa, mask):
            timeouts.append(("poll", None))
            return orig_poll(dev, pa, mask)
        mon.poll = poll
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        i = timeouts.index(("poll", None))
        between = [t for n, t in timeouts[i + 1:] if n == 0]           # lectures entre l'appel et l'émission
        self.assertTrue(between, timeouts[i:i + 3])
        self.assertEqual(set(between), {0.0})
        # moniteur fini : sa relecture finale n'est plus jamais appelée (aucune lecture d'un moniteur fermé) et,
        # sans elle, aucun POLL ne part (échec fermé, verrou rendu)
        n, polls = len(timeouts), len(self.bus.polls())
        self.assertEqual(orig_poll("/dev/cec0", "1.0.0.0", 0x10), "drain_unavailable")
        self.assertEqual((len(timeouts), len(self.bus.polls())), (n, polls))

    # ----------------------------------------------------------------- preuve terrain du 2026-10-31 ~16:08
    # Production 0.7.3 + canari POLL manuel : POLL acquittés toutes les 15 s (séquences 711–718), adaptateur sain
    # (ours, PA 1.0.0.0, masque 0x10), mais la télécommande ne pilotait plus OpenTV ; aucun retour exact depuis
    # 14:37:48. `cec --activate` (annonce sûre) a rétabli immédiatement. POLL acquitté = transport seulement : il
    # maintient un renvoi existant, il ne restaure jamais un renvoi déjà perdu.
    @bounded()
    def test_acked_polls_before_any_announcement_never_arm_or_count_as_recovery(self):
        """POLL acquittés visibles sur le bus (canari manuel, autre processus, POLL de la TV) sans retour exact
        annoncé : jamais d'armement, jamais de POLL du service, jamais d'annonce ni de « reprise » ; même après un
        redémarrage du moniteur ou du service au milieu du flux de POLL."""
        canary = []
        for _ in range(40):                                             # 10 min de POLL ACK toutes les 15 s
            canary += at(15, own_poll()) + at(0.2, tv_poll()) + at(0.1, power_poll())
        nack_canary = at(15, own_poll(nack=True)) + at(15, own_poll())
        for label, scripts in (("single_monitor", [at(0, state_event()) + canary]),
                               ("monitor_restart", [at(0, state_event()) + canary[:30],
                                                    at(0, state_event()) + canary[30:]]),
                               ("nack_then_ack", [at(0, state_event()) + nack_canary + canary[:12]])):
            with self.subTest(label):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                self.assertIsNone(self.monitor(scripts, bus=bus).serve(max_cycles=len(scripts)))
                self.assertEqual(bus.polls(), [], label)                     # le service ne sonde jamais
                self.assertEqual(self.ka_logs(), [], label)                   # jamais armé
                self.assertEqual(self.recoveries(), [], label)                # jamais une « reprise »
                self.assertEqual(bus.opts(), [], label)                       # aucune annonce, aucun réveil

    @bounded()
    def test_lost_forwarding_is_restored_only_by_the_exact_return_announcement(self):
        """Renvoi déjà perdu malgré des POLL ACK : seule la séquence exacte de la TV déclenche l'annonce unique ;
        le maintien n'est armé qu'APRÈS cette annonce acquittée, jamais par les POLL qui la précèdent."""
        pre = []
        for _ in range(8):
            pre += at(15, own_poll())                                    # POLL ACK, télécommande inerte
        self.watch(at(0, state_event()) + pre + returned() + traffic(40))
        rec = self.recoveries()
        self.assertEqual([e["result"] for e in rec], ["announced"])
        t_ann = self.t_announced()[0]
        self.assertEqual([t for t in self.bus.polls() if t <= t_ann], [])   # aucun POLL du service avant
        self.assertEqual(self.states()[0], ("armed", None))
        t_armed = [t for t, a in self.bus.at if "--active-source" in a][0]
        self.assertGreater(min(self.bus.polls()), t_armed + cecreturn.KEEPALIVE_INTERVAL_S - 1e-6)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)                  # une seule annonce, jamais périodique
        # annonce NON acquittée après la même séquence : ni armement ni POLL, malgré les POLL ACK antérieurs
        self.logs.clear()
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        bus.tx_out["--active-source"] = AS_BAD["nack"]
        mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + pre + returned() + traffic(120))], bus=bus)
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual([e["result"] for e in self.recoveries()], ["failed"])
        self.assertEqual((bus.polls(), self.ka_logs()), ([], []))
        self.assertEqual(bus.opts(), ["--give-device-power-status", "--active-source"])

    def test_docs_record_poll_is_transport_only_field_evidence(self):
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                flat = re.sub(r"\s+", " ", f.read())
            for need in ("2026-10-31 ~16:08", "711–718", "ne restaure pas un renvoi déjà perdu",
                         "jamais d'annonce périodique"):
                self.assertTrue(need in flat, (name, need))
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            self.assertIn("ne restaure pas un renvoi déjà perdu", re.sub(r"\s+", " ", f.read()))

    def test_docs_last_moment_drain_and_opt_out_restart(self):
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                flat = re.sub(r"\s+", " ", f.read())
            for need in ("juste avant l'émission", "après l'attente du verrou et la relecture de l'adaptateur",
                         "frame_pending", "drain_overflow",
                         "systemctl restart opentv-cec-return.service"):
                self.assertTrue(need in flat, (name, need))
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            env = re.sub(r"\s+", " ", f.read().replace("#", " ")).lower()
        self.assertIn("systemctl restart opentv-cec-return.service", env)
        self.assertIn("lu au démarrage du service", env)
        self.assertIn("juste avant l'émission", module_source())


class OnceEnd(FakeLines):
    """Fin de flux NON collante, comme PopenLines : `oversize` n'est rendu qu'UNE fois (ligne trop longue), puis la
    lecture continuerait (lignes ordinaires, puis EOF). `arm()` : la prochaine lecture rend cette fin ; `after` compte
    les lectures demandées ensuite (un moniteur aveugle qui continue à lire)."""

    def __init__(self, clock, script, kind="oversize"):
        super().__init__(clock, script)
        self.kind, self.armed, self.fired, self.after = kind, False, False, 0

    def arm(self):
        self.armed = True

    def readline(self, timeout):
        if self.fired:
            self.after += 1
        if self.armed:
            self.armed, self.fired = False, True
            return self.kind, None
        return super().readline(timeout)


# --------------------------------------------------------------------------- revue Stage-2 de c7e1de1 (suivi)
class TestStage2FollowUp(RBase):
    """M34 : une fin de flux non collante vue par la relecture finale du maintien doit finir watch() À L'INSTANT (sinon
    le moniteur continue, aveugle). Durcissement : poll() sans relecture finale installée n'émet jamais."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states

    @bounded()
    def test_non_sticky_stream_end_in_the_last_moment_drain_ends_watch_at_once(self):
        src = OnceEnd(self.clock, at(0, state_event()) + returned() + traffic(120))
        mon = self.monitor([src])
        orig = mon.poll

        def poll(dev, pa, mask):
            src.arm()                         # la relecture finale (après verrou + relecture adaptateur) la verra
            return orig(dev, pa, mask)
        mon.poll = poll
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("oversize", True))
        self.assertTrue(src.fired)
        self.assertEqual(src.after, 0, "lecture du moniteur poursuivie après sa fin")
        self.assertEqual(self.bus.polls(), [])
        self.assertEqual(self.states(), [("armed", None), ("disarmed", "monitor_end")])
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)
        self.assertTrue(src.closed)

    def test_poll_without_installed_drain_never_transmits(self):
        mon = self.monitor()
        self.assertIsNone(mon._before_tx)                     # hors de watch() : aucune relecture finale
        self.assertEqual(mon.poll("/dev/cec0", "1.0.0.0", 0x10), "drain_unavailable")
        self.assertEqual(self.bus.polls(), [])
        self.assertEqual(self.bus.transmits(), [])
        fd = cecboot._lock(self.state)                        # verrou rendu malgré le refus
        self.assertIsNotNone(fd)
        os.close(fd)
        self.assertEqual(self.solo().poll("/dev/cec0", "1.0.0.0", 0x10), "ok")   # relecture installée : émis
        self.assertEqual(len(self.bus.polls()), 1)


class TestRecoveryLastMomentDrain(RBase):
    """Course de la reprise unique (revue Stage-1 de c7e1de1) : recover() pouvait attendre le verrou (≤ 10 s) et la
    question d'alimentation (≤ 5 s) puis émettre <Active Source> alors que la TV était repartie (application native
    0.0.0.0, veille, autre source, routage ailleurs…) pendant l'attente — vol de la source. Exigé : relecture non
    bloquante du moniteur (mêmes Parser/Detector/Keepalive) APRÈS le verrou, la relecture de l'adaptateur ET la
    question d'alimentation, juste avant <Active Source> ; trame d'arrêt, trame incomplète, fin ou débordement ⇒
    reprise annulée (`cancelled`), rien d'autre émis, jamais armé. Boot et `cec --activate` restent sans moniteur."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states
    STOPS = TestStage2LastMomentDrain.STOPS
    HOLD_S, ARRIVE_S, SLOW_POWER_S = 4.0, 1.0, 3.0

    def run_case(self, lines, where="lock", tail=None, end_stream=False, partial_line=False, src=None):
        """-> (résultat de watch, instant d'arrivée de la trame, instant du début de recover). `where` : « lock »
        (verrou tenu HOLD_S, trame à +ARRIVE_S) ou « power » (question d'alimentation de SLOW_POWER_S, trame à +1 s,
        donc après le début de la question)."""
        self.logs.clear()
        self.t_pq = []
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.rbus = bus
        src = src or FakeLines(self.clock, at(0, state_event()) + returned() +
                               (tail if tail is not None else traffic(60)))
        self.src = src
        slow = {"on": False}

        def runner(argv, timeout):
            if "--give-device-power-status" in argv:
                self.t_pq.append(self.clock.t)                         # début de la question d'alimentation
            if slow["on"] and "--give-device-power-status" in argv:
                slow["on"] = False
                self.clock.t += self.SLOW_POWER_S                    # réponse lente (≤ TX_TIMEOUT_S)
            return bus(argv, timeout)
        mon = self.monitor([src], bus=runner)
        orig, held, inj = mon.recover, {}, {}

        def recover(dev, pa):
            if "t" not in inj:
                inj["t0"] = self.clock.t
                inj["t"] = self.clock.t + (self.ARRIVE_S if where == "lock" else 1.0)
                inj["drain"] = mon._before_tx
                if where == "lock":
                    held["fd"], held["t"] = cecboot._lock(self.state), self.clock.t
                else:
                    slow["on"] = True
                if partial_line:
                    src.partial = lambda: True
                if isinstance(src, OnceEnd):
                    src.script = [(0.0, x) for x in lines] + src.script
                    src.due = inj["t"]
                    src.arm()
                elif end_stream:
                    src.script = []
                else:
                    src.script = [(0.0, x) for x in lines] + src.script
                    src.due = inj["t"]
            return orig(dev, pa)

        def sleep(s):
            self.sleep(s)
            if held.get("fd") is not None and self.clock.t - held["t"] >= self.HOLD_S:
                os.close(held["fd"])
                held["fd"] = None
        mon.recover, mon.sleep = recover, sleep
        try:
            res = mon.watch("/dev/cec0", "1.0.0.0", 0x10)
        finally:
            if held.get("fd") is not None:
                os.close(held["fd"])
        self.assertIn("t", inj)
        self.assertIsNotNone(inj["drain"])                            # installée par watch() avant toute reprise
        return res, inj["t"], inj["t0"]

    def assert_cancelled(self, reason, t_frame, t0, where):
        bus = self.rbus
        rec = self.recoveries()
        self.assertEqual([(e["result"], e.get("reason")) for e in rec[:1]], [("cancelled", reason)])
        self.assertEqual(bus.opts()[:1], ["--give-device-power-status"])      # question posée APRÈS le verrou…
        self.assertNotIn("--active-source", bus.opts())                       # … jamais d'annonce ensuite
        self.assertNotIn("--menu-status", bus.opts())
        self.assertEqual(wake_args(bus), [])
        self.assertEqual(bus.polls(), [])
        self.assertEqual(self.ka_logs(), [])                                  # jamais armé
        t_pq = self.t_pq[0]
        if where == "lock":
            self.assertGreaterEqual(t_pq, t0 + self.HOLD_S - 1e-9)            # verrou attendu avant la question
        else:
            self.assertLess(t_pq, t_frame)                                    # trame arrivée PENDANT la question
        fd = cecboot._lock(self.state)                                        # verrou rendu malgré l'annulation
        self.assertIsNotNone(fd)
        os.close(fd)

    @bounded()
    def test_stop_frame_during_lock_wait_or_power_query_cancels_the_announcement(self):
        for where in ("lock", "power"):
            for name, (lines, reason) in sorted(self.STOPS.items()):
                with self.subTest(where=where, case=name):
                    res, t_frame, t0 = self.run_case(lines, where)
                    self.assertEqual(res, ("eof", True), name)
                    self.assert_cancelled(reason, t_frame, t0, where)
                    self.assertEqual(len(self.recoveries()), 1, name)
                    self.assertGreaterEqual(self.clock.t, t_frame)

    @bounded()
    def test_first_stop_frame_wins_and_the_rest_is_left_to_the_monitor_loop(self):
        """La relecture finale s'arrête à la PREMIÈRE trame d'arrêt (raison véridique) ; la suite reste au moniteur."""
        for where in ("lock", "power"):
            with self.subTest(where=where):
                lines = active_source("0.0.0.0") + frame(0x36, "STANDBY") + power_poll()
                res, t_frame, t0 = self.run_case(lines, where)
                self.assertEqual(res, ("eof", True))
                self.assert_cancelled("native_app", t_frame, t0, where)
                self.assertIn(frame(0x36, "STANDBY")[0], [line for _, line in self.src.delivered])

    @bounded()
    def test_adapter_state_change_or_stream_end_during_wait_cancels_and_ends_watch(self):
        for where in ("lock", "power"):
            with self.subTest(where=where, case="state_changed"):
                res, t_frame, t0 = self.run_case(state_event("f.f.f.f", 0x0000, initial=False), where)
                self.assertEqual(res, ("adapter_state_changed", True))
                self.assert_cancelled("adapter_state", t_frame, t0, where)
            with self.subTest(where=where, case="eof"):
                res, t_frame, t0 = self.run_case([], where, end_stream=True)
                self.assertEqual(res, ("eof", True))
                self.assert_cancelled("monitor_end", t_frame, t0, where)
            with self.subTest(where=where, case="oversize_once"):
                src = OnceEnd(self.clock, at(0, state_event()) + returned() + traffic(60))
                res, t_frame, t0 = self.run_case(power_poll(), where, src=src)
                self.assertEqual(res, ("oversize", True))                    # rendu tout de suite, jamais perdu
                self.assertEqual(src.after, 0, "lecture du moniteur poursuivie après sa fin")
                self.assert_cancelled("monitor_end", t_frame, t0, where)

    @bounded()
    def test_incomplete_frame_or_flood_at_the_last_moment_cancels(self):
        flood = []
        for _ in range(cecreturn.PRE_TX_DRAIN_MAX // 2 + 1):
            flood += power_poll()
        for where in ("lock", "power"):
            for label, lines, partial_line, reason in (
                    ("header_without_raw", active_source("0.0.0.0")[:2], False, "frame_pending"),
                    ("line_without_newline", [], True, "frame_pending"),
                    ("flood", flood + active_source("0.0.0.0"), False, "drain_overflow")):
                with self.subTest(where=where, case=label):
                    res, t_frame, t0 = self.run_case(lines, where, partial_line=partial_line)
                    self.assertEqual(res, ("eof", True))
                    self.assert_cancelled(reason, t_frame, t0, where)

    @bounded()
    def test_neutral_traffic_during_wait_keeps_the_single_announcement(self):
        neutral = power_poll() + key() + tv_poll() + own_poll() + routing_change("0.0.0.0", "1.0.0.0") + \
            stream_path("1.0.0.0") + active_source("1.0.0.0", src=4, tx=True)
        for where in ("lock", "power"):
            with self.subTest(where=where):
                res, t_frame, t0 = self.run_case(neutral, where, tail=traffic(60))
                bus = self.rbus
                self.assertEqual(res, ("eof", True))
                self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)                  # une seule annonce, jamais de réveil
                t_as = next(t for t, a in bus.at if "--active-source" in a)
                self.assertGreaterEqual(t_as, t_frame)                       # lignes lues avant l'annonce
                self.assertEqual(self.states()[0], ("armed", None))
                self.assertTrue(bus.polls())
                self.assertGreaterEqual(min(bus.polls()), t_as + cecreturn.KEEPALIVE_INTERVAL_S)

    @bounded()
    def test_cancelled_recovery_never_suppresses_the_next_exact_return(self):
        """YouTube rouvert pendant l'attente (annulé), puis vrai retour : la reprise suivante passe normalement."""
        tail = at(30, routing_change("0.0.0.0", "1.0.0.0")) + at(0.05, stream_path("1.0.0.0")) + traffic(60)
        res, _, _ = self.run_case(active_source("0.0.0.0"), "lock", tail=tail)
        self.assertEqual(res, ("eof", True))
        self.assertEqual([e["result"] for e in self.recoveries()], ["cancelled", "announced"])
        self.assertEqual(self.rbus.opts(), ["--give-device-power-status"] + ANNOUNCE_SEQ)
        self.assertEqual(self.states()[0], ("armed", None))

    def test_recovery_requires_the_monitor_drain(self):
        mon = self.monitor()
        self.assertIsNone(mon._before_tx)
        self.assertEqual(mon.recover("/dev/cec0", "1.0.0.0"), {"result": "drain_unavailable"})
        self.assertEqual(self.bus.calls, [])                                  # ni relecture, ni question, ni annonce
        fd = cecboot._lock(self.state)
        self.assertIsNotNone(fd)
        os.close(fd)
        # la relecture passée à l'annonce est exactement celle installée par watch() (jamais None sur ce chemin)
        seen = []
        orig = cecboot.announce

        def spy(*a, **kw):
            seen.append(kw.get("before_tx"))
            return orig(*a, **kw)
        with mock.patch.object(cecreturn.cecboot, "announce", spy):
            mon = self.monitor([FakeLines(self.clock, at(0, state_event()) + returned() + traffic(20))])
            drains = []
            orig_rec = mon.recover

            def recover(dev, pa):
                drains.append(mon._before_tx)
                return orig_rec(dev, pa)
            mon.recover = recover
            self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertIs(seen[0], drains[0])

    def test_announce_hook_runs_after_power_query_and_is_optional_for_boot_and_activate(self):
        order = []

        def run(argv, want):
            order.append(next((o for o in ("--give-device-power-status", "--active-source", "--menu-status")
                               if o in argv), "read"))
            return self.bus(argv, want)
        for stop in ("native_app", "frame_pending", "monitor_end"):
            with self.subTest(stop=stop):
                order.clear()
                ann, ok = cecboot.announce(run, "/dev/cec0", "1.0.0.0", 0x10,
                                           before_tx=lambda _s=stop: order.append("drain") or _s)
                self.assertFalse(ok)
                self.assertEqual(ann, {"result": "cancelled", "tv_power": "on", "reason": stop})
                self.assertEqual(order, ["--give-device-power-status", "drain"])
        order.clear()
        ann, ok = cecboot.announce(run, "/dev/cec0", "1.0.0.0", 0x10, before_tx=lambda: order.append("drain"))
        self.assertTrue(ok)
        self.assertEqual(order, ["--give-device-power-status", "drain", "--active-source", "--menu-status"])
        self.bus.power = "standby"                                            # TV pas allumée : rien d'autre
        order.clear()
        ann, ok = cecboot.announce(run, "/dev/cec0", "1.0.0.0", 0x10, before_tx=lambda: order.append("drain"))
        self.assertEqual((ann["result"], ok, order), ("refused_tv_not_on", False, ["--give-device-power-status"]))
        self.bus.power = "on"
        order.clear()
        ann, ok = cecboot.announce(run, "/dev/cec0", "1.0.0.0", 0x10)        # boot / --activate : sans moniteur
        self.assertTrue(ok)
        self.assertEqual(order, ["--give-device-power-status", "--active-source", "--menu-status"])
        # `cec --activate` (bootstrap --force-announce) : inchangé, aucune relecture de moniteur exigée
        self.bus.calls.clear()
        res = self.boot(self.root, self.bus, force_announce=True)
        self.assertEqual(res["result"], "ready")
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)

    def test_docs_recovery_last_moment_drain_and_home_keepalive(self):
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                flat = re.sub(r"\s+", " ", f.read())
            for need in ("juste avant l'`ACTIVE_SOURCE` de la reprise", "après le verrou et la question d'alimentation",
                         "\"result\":\"cancelled\"", "drain_unavailable", "fenêtre résiduelle"):
                self.assertTrue(need in flat, (name, need))
        with open(os.path.join(PI4, "CEC_COMPATIBILITY.md"), encoding="utf-8") as f:
            flat = re.sub(r"\s+", " ", f.read())
        self.assertNotIn("Ne pas ajouter de keepalive ou de réannonce périodique", flat)
        for need in ("POLL neutre", "autorisé seulement après", "annonce, réannonce ou reprise périodique",
                     "≥ 3 min"):
            self.assertTrue(need in flat, need)
        src = re.sub(r"\s+", " ", module_source())
        self.assertIn("juste avant l'<Active Source> de la reprise", src)
        self.assertIn("fenêtre résiduelle", src)


# --------------------------------------------------------------------------- revue finale de 4a1b48d (mutants)
class TimedLog(list):
    """Journal de test : chaque événement est copié avec l'instant simulé `_t` de son émission."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def append(self, obj):
        super().append(dict(obj, _t=self.clock.t))


BUDGET_DOC = ("comptent dans le plafond de 60 tentatives/h, mais ni dans le délai anti-doublon de 5 s ni dans le "
              "quota de 30 annonces/h")


class TestFinalReviewBudgetsAndEnds(RBase):
    """Mutants survivants des revues finales de 4a1b48d, tués ici sans changer le code de production :
    1) une reprise `cancelled` qui consommerait le délai anti-doublon (le retour exact suivant serait reporté) ;
    2) une reprise `cancelled` qui consommerait le quota de 30 annonces/h (`rate_limited` après 30 annulations) ;
    3) une fin de moniteur vue par la relecture finale rendue `eof` brut au lieu du diagnostic stderr/code de sortie ;
    4) la relecture préalable de la boucle principale retirée (verrou pris et adaptateur relu pour rien).
    Contrat : `cancelled`, `busy`, refus et échecs comptent dans le plafond séparé MAX_ATTEMPTS_PER_HOUR (60/h), jamais
    dans COOLDOWN_S ni dans MAX_PER_HOUR."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states
    t_announced = TestKeepalive.t_announced
    run_case = TestRecoveryLastMomentDrain.run_case
    assert_cancelled = TestRecoveryLastMomentDrain.assert_cancelled
    HOLD_S, ARRIVE_S, SLOW_POWER_S = (TestRecoveryLastMomentDrain.HOLD_S, TestRecoveryLastMomentDrain.ARRIVE_S,
                                      TestRecoveryLastMomentDrain.SLOW_POWER_S)
    REDO_S = 2.0          # retour exact suivant : ROUTING_CHANGE 2 s après l'annulation (+ 0,05 s + calme 1,5 s)

    # ----------------------------------------------------------------- (1) cancelled ≠ délai anti-doublon
    @bounded()
    def test_fast_exact_return_after_cancelled_is_announced_not_cooldown_deferred(self):
        """YouTube rouvert pendant l'attente du verrou (`cancelled`), puis la TV revient : la reprise suivante part
        ~3,5–4 s après l'annulation (< COOLDOWN_S), dès la fin du calme — jamais `cooldown_deferred`."""
        for where in ("lock", "power"):
            with self.subTest(where=where):
                self.logs = TimedLog(self.clock)
                tail = at(self.REDO_S, routing_change("0.0.0.0", "1.0.0.0")) + at(0.05, stream_path("1.0.0.0")) + \
                    traffic(60)
                res, _, _ = self.run_case(active_source("0.0.0.0"), where, tail=tail)
                self.assertEqual(res, ("eof", True))
                rec = self.recoveries()
                self.assertEqual([(e["result"], e.get("reason")) for e in rec],
                                 [("cancelled", "native_app"), ("announced", None)], rec)
                t_cancel, t_retry = rec[0]["_t"], self.t_pq[1]           # 2e reprise = 2e question d'alimentation
                self.assertGreaterEqual(t_retry - t_cancel, 3.5 - 1e-6)
                self.assertLessEqual(t_retry - t_cancel, 4.0)
                self.assertLess(t_retry - t_cancel, cecreturn.COOLDOWN_S)
                t_sp = [t for t, line in self.src.delivered if line == stream_path("1.0.0.0")[0]][-1]
                # aucun report après le calme (seule la relecture de l'adaptateur, 0,05 s simulés, précède la question)
                self.assertLessEqual(t_retry - t_sp, cecreturn.SETTLE_S + 0.1)
                self.assertEqual(self.rbus.opts(), ["--give-device-power-status"] + ANNOUNCE_SEQ)
                self.assertEqual(self.states()[0], ("armed", None))

    def test_cancelled_attempt_leaves_cooldown_and_announcement_quota_untouched(self):
        mon = self.solo()
        det = cecreturn.Detector("1.0.0.0", 0x10)
        stops = ["native_app"]
        mon._before_tx = lambda: stops.pop(0) if stops else None
        self.assertEqual(mon._attempt("/dev/cec0", "1.0.0.0", det), "cancelled")
        self.assertIsNone(mon.last_announced)                 # ni délai anti-doublon…
        self.assertEqual(len(mon.announced), 0)               # … ni quota de 30 annonces/h
        self.assertEqual(len(mon.attempts), 1)                # plafond séparé des tentatives (60/h) : compté
        self.clock.t += 3.5
        self.assertEqual(mon._attempt("/dev/cec0", "1.0.0.0", det), "announced")
        self.assertIsNone(det.deadline())                     # jamais reportée
        self.assertEqual([e["result"] for e in self.recoveries()], ["cancelled", "announced"])
        self.assertIsNotNone(mon.last_announced)
        self.assertEqual((len(mon.announced), len(mon.attempts)), (1, 2))
        self.assertEqual(self.bus.opts(), ["--give-device-power-status"] + ANNOUNCE_SEQ)

    # ----------------------------------------------------------------- (2) cancelled ≠ quota de 30 annonces/h
    def test_thirty_or_thirty_one_cancelled_never_rate_limit_the_next_announcement(self):
        for n in (cecreturn.MAX_PER_HOUR, cecreturn.MAX_PER_HOUR + 1):
            with self.subTest(cancelled=n):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                mon = self.solo(bus=bus)
                det = cecreturn.Detector("1.0.0.0", 0x10)
                stops = ["native_app"] * n
                mon._before_tx = lambda _s=stops: _s.pop(0) if _s else None
                out = []
                for _ in range(n + 1):
                    out.append(mon._attempt("/dev/cec0", "1.0.0.0", det))
                    self.clock.t += 1.0
                self.assertEqual(out, ["cancelled"] * n + ["announced"], out)
                self.assertNotIn("rate_limited", out)
                self.assertNotIn("cooldown_deferred", out)
                self.assertEqual(len(mon.announced), 1)
                self.assertEqual(len(mon.attempts), n + 1)
                self.assertEqual(bus.opts(), ["--give-device-power-status"] * n + ANNOUNCE_SEQ)

    def test_cancelled_attempts_still_count_toward_the_separate_attempt_cap(self):
        """Plafond de protection du bus : 60 tentatives/h, annulées comprises ; puis plus rien n'est émis jusqu'à ce
        que la fenêtre glissante d'une heure se libère."""
        mon = self.solo()
        det = cecreturn.Detector("1.0.0.0", 0x10)
        mon._before_tx = lambda: "native_app"
        out = []
        for _ in range(cecreturn.MAX_ATTEMPTS_PER_HOUR):
            out.append(mon._attempt("/dev/cec0", "1.0.0.0", det))
            self.clock.t += 1.0
        self.assertEqual(out, ["cancelled"] * cecreturn.MAX_ATTEMPTS_PER_HOUR)
        self.assertEqual(len(mon.announced), 0)
        n_bus = len(self.bus.calls)
        self.assertEqual(mon._attempt("/dev/cec0", "1.0.0.0", det), "rate_limited")
        self.assertEqual(len(self.bus.calls), n_bus)                  # aucune question, aucune émission
        self.clock.t += 3600.0
        mon._before_tx = lambda: None
        self.assertEqual(mon._attempt("/dev/cec0", "1.0.0.0", det), "announced")

    @bounded()
    def test_many_cancelled_exact_returns_in_one_monitor_still_announce_the_next_one(self):
        """Bout en bout (un seul moniteur) : MAX_PER_HOUR + 1 retours exacts, chacun annulé par YouTube rouvert pendant
        l'attente, puis un retour exact normal : exactement une annonce, jamais `rate_limited` ni `cooldown_deferred`."""
        n = cecreturn.MAX_PER_HOUR + 1
        script = at(0, state_event()) + returned()
        for _ in range(n):
            script += at(self.REDO_S, routing_change("0.0.0.0", "1.0.0.0")) + at(0.05, stream_path("1.0.0.0"))
        src = FakeLines(self.clock, script + traffic(60))
        mon = self.monitor([src])
        orig, calls = mon.recover, []

        def recover(dev, pa):
            calls.append(self.clock.t)
            if len(calls) <= n:          # ACTIVE_SOURCE 0.0.0.0 reçue pendant l'attente : relue au dernier moment
                src.script = [(0.0, x) for x in active_source("0.0.0.0")] + src.script
                src.due = self.clock.t
            return orig(dev, pa)
        mon.recover = recover
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual(res, ["cancelled"] * n + ["announced"], res)
        self.assertLess(calls[-1] - calls[0], 3600.0)                 # même heure glissante
        self.assertEqual(self.bus.opts(), ["--give-device-power-status"] * n + ANNOUNCE_SEQ)
        self.assertEqual(self.states()[0], ("armed", None))

    # ----------------------------------------------------------------- (3) fin vue par la relecture finale
    @bounded()
    def test_non_repeated_end_seen_by_the_final_drain_keeps_the_exact_end_reason(self):
        """Fin du moniteur (EOF rendu UNE fois, comme PopenLines) vue par la relecture finale de la reprise ou du POLL :
        watch() rend le diagnostic exact de PopenLines (stderr classé, sinon `exit_nonzero`), pas `eof` ; aucune lecture
        de plus, aucun POLL, aucune annonce."""
        cases = (("device_disconnected", 0, "device_disconnected"), ("device_disconnected", 1, "device_disconnected"),
                 (None, 1, "exit_nonzero"), (None, 0, "eof"))

        def once_end(err, status):
            src = OnceEnd(self.clock, at(0, state_event()) + returned() + traffic(60), kind="eof")
            src.error, src.exit_status, src.diags = err, status, 0
            orig = src.diagnosis

            def diagnosis(_o=orig, _s=src):
                _s.diags += 1
                return _o()
            src.diagnosis = diagnosis
            return src
        for err, status, want in cases:
            for where in ("lock", "power"):
                with self.subTest(path="recovery", where=where, want=want, exit_status=status):
                    src = once_end(err, status)
                    res, t_frame, t0 = self.run_case([], where, src=src)
                    self.assertEqual(res, (want, True))
                    self.assertTrue(src.fired)
                    self.assertEqual(src.after, 0, "lecture du moniteur poursuivie après sa fin")
                    self.assertEqual(src.diags, 1)
                    self.assert_cancelled("monitor_end", t_frame, t0, where)
                    self.assertEqual(self.rbus.opts(), ["--give-device-power-status"])
            with self.subTest(path="poll", want=want, exit_status=status):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
                src = once_end(err, status)
                mon = self.monitor([src], bus=bus)
                orig_poll = mon.poll

                def poll(dev, pa, mask, _o=orig_poll, _s=src):
                    _s.arm()                  # vue par la relecture finale (après verrou + relecture adaptateur)
                    return _o(dev, pa, mask)
                mon.poll = poll
                self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), (want, True))
                self.assertEqual((src.fired, src.after, src.diags), (True, 0, 1))
                self.assertEqual(bus.polls(), [])
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)
                self.assertEqual(self.states(), [("armed", None), ("disarmed", "monitor_end")])

    # ----------------------------------------------------------------- (4) relecture préalable de la boucle
    @bounded()
    def test_stop_frame_buffered_before_a_due_poll_cancels_without_lock_or_adapter_read(self):
        """Trame d'arrêt déjà dans le tampon à l'échéance du POLL : la boucle principale la lit d'abord (délai nul) et
        désarme sans même prendre le verrou CEC ni relire l'adaptateur (économie + échec rapide) ; poll() jamais
        appelé. La relecture finale de poll() reste la seconde barrière, pas la seule."""
        for frame_lines, reason in ((active_source("0.0.0.0"), "native_app"),
                                    (active_source("3.0.0.0", src=8), "other_source"),
                                    (routing_change("0.0.0.0", "2.0.0.0"), "routed_away"),
                                    (frame(0x36, "STANDBY"), "standby"),
                                    (["", "1.0s: Event: Lost 3 messages"], "lost")):
            with self.subTest(reason):
                self.logs.clear()
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})

                def due(t, _b=bus):
                    menu = [x for x, a in _b.at if "--menu-status" in a]
                    return bool(menu) and t >= menu[0] + cecreturn.KEEPALIVE_INTERVAL_S
                src = RaceLines(self.clock, at(0, state_event()) + returned() + at(120, power_poll()), frame_lines, due)
                mon = self.monitor([src], bus=bus)
                polls, locks = [], []
                orig_poll, orig_lock = mon.poll, cecboot._lock
                mon.poll = lambda *a, _o=orig_poll: polls.append(self.clock.t) or _o(*a)

                def lock(state_dir, _o=orig_lock):
                    locks.append(self.clock.t)
                    return _o(state_dir)
                with mock.patch.object(cecreturn.cecboot, "_lock", lock):
                    self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), ("eof", True))
                self.assertTrue(src.fired)
                self.assertEqual([e["result"] for e in self.recoveries()], ["announced"])
                t_ann = self.t_announced(bus)[0]
                self.assertEqual(polls, [], reason)                                    # poll() jamais appelé
                self.assertEqual([t for t in locks if t > t_ann], [], reason)          # verrou jamais repris
                self.assertEqual(len(locks), 1)                                       # celui de la reprise seul
                reads = [t for t, a in bus.at if a == ["cec-ctl", "-d", "/dev/cec0"] and t > t_ann]
                self.assertEqual(reads, [], reason)                                   # adaptateur jamais relu
                self.assertEqual(bus.polls(), [])
                self.assertEqual(bus.opts(), ANNOUNCE_SEQ)
                self.assertEqual(self.states(), [("armed", None), ("disarmed", reason)])

    def test_docs_budget_split_between_attempt_cap_cooldown_and_quota(self):
        for path in (os.path.join(PI4, "README.md"), os.path.join(PI4, "CEC_COMPATIBILITY.md"),
                     os.path.join(ROOT, "AGENTS.md")):
            with open(path, encoding="utf-8") as f:
                flat = re.sub(r"\s+", " ", f.read())
            self.assertIn(BUDGET_DOC, flat, path)
            self.assertIn("`cancelled`", flat, path)
        self.assertEqual((cecreturn.COOLDOWN_S, cecreturn.MAX_PER_HOUR, cecreturn.MAX_ATTEMPTS_PER_HOUR),
                         (5.0, 30, 60))


# --------------------------------------------------------------------------- revue de c0e20ae (mutants survivants)
class TestCooldownEndAndPredrainRecheck(RBase):
    """Délai anti-doublon compté depuis la FIN de l'annonce transmise (pas le début de la tentative) ; <Poll> dû avant
    la lecture sans attente du maintien : rien n'est émis si l'échéance n'est plus due après cette lecture."""
    ka_logs = TestKeepalive.ka_logs
    states = TestKeepalive.states

    def slow_runner(self, slow_s, ends, on=None):
        """Bus dont la question d'alimentation de l'annonce dure `slow_s` (horloge simulée) ; fin de chaque
        <Menu Status> (fin de l'annonce) notée dans `ends`."""
        def runner(argv, timeout):
            if "--give-device-power-status" in argv and (on is None or on["on"]):
                self.clock.t += slow_s
            out = self.bus(argv, timeout)
            if "--menu-status" in argv:
                ends.append(self.clock.t)
            return out
        return runner

    @bounded()
    def test_cooldown_starts_when_a_slow_announcement_ends_not_when_it_starts(self):
        ends = []
        first = at(0, state_event()) + at(5, active_source()) + at(60, routing_change()) + at(0.05, stream_path())
        # retour exact suivant : SET_STREAM_PATH 5,85 s après le précédent ⇒ dû 7,35 s après (SETTLE_S compris), soit
        # ≥ 5 s après le DÉBUT de la 1re tentative mais < 5 s après sa FIN (annonce lente de 3 s)
        quick = at(5.5, active_source()) + at(0.3, routing_change()) + at(0.05, stream_path())
        self.src = FakeLines(self.clock, first + quick + at(20, power_poll()))
        mon = self.monitor([self.src], bus=self.slow_runner(3.0, ends))
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10)[0], "eof")
        rec = self.recoveries()
        self.assertEqual([e["result"] for e in rec], ["announced", "cooldown_deferred", "announced"], rec)
        t_as = [t for t, a in self.bus.at if "--active-source" in a]
        reads = [t for t, a in self.bus.at if a == ["cec-ctl", "-d", "/dev/cec0"]]      # début de chaque tentative
        t_start = reads[0]
        self.assertEqual(len(t_as), 2)
        self.assertGreater(ends[0] - t_start, 3.0)                          # annonce réellement lente
        t_stream2 = [t for t, line in self.src.delivered if line == stream_path()[0]][1]
        t_due2 = t_stream2 + cecreturn.SETTLE_S
        self.assertGreaterEqual(t_due2 - t_start, cecreturn.COOLDOWN_S)     # permis si compté depuis le début…
        self.assertLess(t_due2 - ends[0], cecreturn.COOLDOWN_S)             # … mais reporté depuis la fin
        # 2e tentative : reportée exactement à fin de la 1re annonce + COOLDOWN_S, jamais avant
        self.assertGreaterEqual(reads[1] - ends[0], cecreturn.COOLDOWN_S - 1e-6)
        self.assertLessEqual(reads[1] - ends[0], cecreturn.COOLDOWN_S + 0.5)
        self.assertGreater(t_as[1], reads[1])
        self.assertEqual(wake_args(self.bus), [])

    @bounded()
    def test_poll_due_before_the_predrain_is_not_sent_once_an_announcement_rearmed_it(self):
        """Le <Poll> était dû au début du tour ; pendant la lecture sans attente qui précède l'émission, une reprise
        exacte est annoncée (ACK, lente) et réarme l'échéance à fin d'annonce + 15 s. Aucun <Poll> juste après ce
        tour : le premier part ≥ 15 s après la fin de cette annonce (cadence contrôlée, aucune attente réelle).
        Sonde : la reprise est rejouée depuis cette lecture (seul moment où l'échéance peut bouger entre le test
        `due` du tour et l'émission)."""
        cap, hook, slow, ends = {}, {"busy": False, "t": None}, {"on": False}, []

        class KA(cecreturn.Keepalive):
            def __init__(s, *a):
                super().__init__(*a)
                cap["ka"] = s

        class DET(cecreturn.Detector):
            def __init__(s, *a):
                super().__init__(*a)
                cap["det"] = s

        clock = self.clock

        class PredrainLines(FakeLines):
            def readline(s, timeout):
                kind, line = super().readline(timeout)
                ka = cap.get("ka")
                if kind == "timeout" and timeout == 0.0 and not hook["busy"] and hook["t"] is None and \
                        ka is not None and ka.due(clock.t):
                    hook["busy"], hook["t"], slow["on"] = True, clock.t, True
                    res = mon._attempt("/dev/cec0", "1.0.0.0", cap["det"])
                    mon._ka_after_attempt(ka, res, "/dev/cec0", "1.0.0.0")
                    hook["busy"] = False
                return kind, line

        src = PredrainLines(self.clock, at(0, state_event()) + returned() + traffic(90))
        mon = self.monitor([src], bus=self.slow_runner(2.0, ends, slow))
        with mock.patch.object(cecreturn, "Keepalive", KA), mock.patch.object(cecreturn, "Detector", DET):
            why, _ = mon.watch("/dev/cec0", "1.0.0.0", 0x10)
        self.assertEqual(why, "eof")
        self.assertEqual([e["result"] for e in self.recoveries()], ["announced", "announced"])
        self.assertEqual(len(ends), 2)
        self.assertIsNotNone(hook["t"])
        self.assertGreaterEqual(hook["t"] - ends[0], cecreturn.KEEPALIVE_INTERVAL_S)    # le <Poll> était dû
        self.assertGreater(ends[1] - hook["t"], 2.0)                                     # annonce lente, ACK
        polls = self.bus.polls()
        self.assertTrue(polls)
        self.assertEqual([t for t in polls if t < ends[1] + cecreturn.KEEPALIVE_INTERVAL_S - 1e-6], [], polls)
        self.assertLessEqual(polls[0] - ends[1], cecreturn.KEEPALIVE_INTERVAL_S + 0.5)
        self.assertEqual(self.states()[0], ("armed", None))
        self.assertNotIn("disarmed", [s for s, _ in self.states()[:-1]])
        self.assertEqual(wake_args(self.bus), [])


# --------------------------------------------------------------------------- CLI, configuration
class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(list(argv))
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_config_default_and_opt_out(self):
        self.assertTrue(config.Config({}).cec_return_recovery)
        self.assertFalse(config.Config({"OPENTV_CEC_RETURN_RECOVERY": "0"}).cec_return_recovery)
        with self.assertRaises(ValueError):
            config.Config({"OPENTV_CEC_RETURN_RECOVERY": "oui"})
        # maintien neutre (0.7.4) : actif par défaut, opt-out indépendant
        self.assertTrue(config.Config({}).cec_return_keepalive)
        self.assertFalse(config.Config({"OPENTV_CEC_RETURN_KEEPALIVE": "0"}).cec_return_keepalive)
        with self.assertRaises(ValueError):
            config.Config({"OPENTV_CEC_RETURN_KEEPALIVE": "oui"})
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("#OPENTV_CEC_RETURN_RECOVERY=1", text)
        self.assertIn("#OPENTV_CEC_RETURN_KEEPALIVE=1", text)

    def test_root_refused_and_disabled_exit_cleanly(self):
        d = tmpdir(self, "otvcecret")
        env = os.path.join(d, "env")
        with mock.patch.object(os, "geteuid", lambda: 0):
            code, out = self.run_cli("--env", env, "cec-return-monitor")
        self.assertEqual((code, out[0]["error"]), (2, "run_via_systemd"))
        for content in ("OPENTV_CEC_RETURN_RECOVERY=0\n", "OPENTV_CEC_BOOTSTRAP=0\n"):
            with open(env, "w") as f:
                f.write(content)
            with mock.patch.object(os, "geteuid", lambda: 1000), \
                    mock.patch.object(cecreturn.Monitor, "serve", side_effect=AssertionError("started")):
                code, out = self.run_cli("--env", env, "cec-return-monitor")
            self.assertEqual((code, out[0]["result"]), (0, "disabled"))

    def test_enabled_runs_monitor_with_state_dir(self):
        d = tmpdir(self, "otvcecret")
        env = os.path.join(d, "env")
        open(env, "w").close()
        seen = {}

        def serve(me, max_cycles=None):
            seen.update(root=me.root, state_dir=me.state_dir, keepalive=me.keepalive)
        with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cecreturn.Monitor, "serve", serve):
            code, _ = self.run_cli("--env", env, "cec-return-monitor", "--root", d, "--state-dir", d)
        self.assertEqual((code, seen), (0, {"root": d, "state_dir": d, "keepalive": True}))
        with open(env, "w") as f:                                   # opt-out du maintien seul : reprise conservée
            f.write("OPENTV_CEC_RETURN_KEEPALIVE=0\n")
        with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cecreturn.Monitor, "serve", serve):
            code, _ = self.run_cli("--env", env, "cec-return-monitor", "--root", d, "--state-dir", d)
        self.assertEqual((code, seen["keepalive"]), (0, False))
        self.assertIs(cecreturn.Monitor(d).keepalive, True)          # défaut du module : actif

    def test_manual_activate_path_unchanged(self):
        self.assertEqual(cli.CEC_ANNOUNCE_UNIT, "opentv-cec-announce.service")
        import inspect
        src = inspect.getsource(cli.cmd_cec) + inspect.getsource(cli.cmd_cec_bootstrap)
        self.assertNotIn("cecreturn", src)
        self.assertNotIn("cec-return", src)


# --------------------------------------------------------------------------- unité, installation, manifeste
def unit(name=UNIT):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        text = f.read()
    cp.read_string(text)
    return cp, text


class TestUnit(unittest.TestCase):
    def test_unit_security(self):
        cp, text = unit()
        s = cp["Service"]
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi cec-return-monitor")
        self.assertEqual((s["Type"], s["User"], s["Group"]), ("simple", "opentv", "opentv"))
        self.assertEqual(s["SupplementaryGroups"].split(), ["video"])            # jamais input : aucune touche lue
        # mode moniteur du noyau (CAP_NET_ADMIN, passif) : seule capacité. Le noyau la vérifie dans l'espace de noms
        # utilisateur initial (capable()) : PrivateNetwork ne la confine pas ; confinement réel = AF_UNIX seulement,
        # filtre d'appels système, périphériques CEC seulement (le commentaire de l'unité doit le dire)
        self.assertEqual((s["CapabilityBoundingSet"], s["AmbientCapabilities"]), ("CAP_NET_ADMIN", "CAP_NET_ADMIN"))
        lines = text.splitlines()
        i = lines.index("CapabilityBoundingSet=CAP_NET_ADMIN")
        j = i
        while j > 0 and lines[j - 1].startswith("#"):
            j -= 1
        why = " ".join(line.lstrip("# ") for line in lines[j:i])
        for need in ("CAP_NET_ADMIN", "espace de noms utilisateur initial", "PrivateNetwork ne la borne pas",
                     "RestrictAddressFamilies", "SystemCallFilter", "DevicePolicy=closed", "DeviceAllow"):
            self.assertIn(need, why)
        self.assertNotIn("sans effet hors du reseau", why)
        self.assertEqual(s["SystemCallFilter"], "@system-service")
        self.assertEqual((s["NoNewPrivileges"], s["PrivateNetwork"], s["RestrictAddressFamilies"]),
                         ("yes", "yes", "AF_UNIX"))
        self.assertEqual(sorted(re.findall(r"^DeviceAllow=(.+)$", text, re.M)), ["char-cec rw"])
        self.assertEqual((s["DevicePolicy"], s["ProtectSystem"], s["ProtectHome"]), ("closed", "strict", "yes"))
        self.assertEqual((s["RuntimeDirectory"], s["RuntimeDirectoryPreserve"]), ("opentv-cec", "yes"))
        self.assertEqual(s["Restart"], "on-failure")
        # échec fatal (refus du mode moniteur, échecs répétés avant « prêt ») : état failed véridique, sans relance
        self.assertEqual(s["RestartPreventExitStatus"].split(), [str(cecreturn.EXIT_FATAL)])
        self.assertGreaterEqual(int(s["RestartSec"]), 10)
        self.assertLessEqual(int(cp["Unit"]["StartLimitBurst"]), 10)
        self.assertIn("MemoryMax", s)
        self.assertIn("TasksMax", s)
        inacc = re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)
        self.assertIn("-/etc/opentv/tmdb", inacc)
        self.assertIn("-/var/lib/opentv/canvas", inacc)
        self.assertNotIn("ConditionPathExistsGlob", text)                       # attend lui-même l'adaptateur
        self.assertEqual(cp["Install"]["WantedBy"], "multi-user.target")
        for bad in ("--force-announce", "cec-bootstrap", "input", "Image", "ExecStartPre", "ExecStartPost",
                    "EnvironmentFile"):
            self.assertNotIn(bad, re.sub(r"^#.*$", "", text, flags=re.M), bad)
        same = unit("opentv-cec.service")[0]["Service"]
        self.assertEqual((s["RuntimeDirectoryMode"], s["UMask"]), (same["RuntimeDirectoryMode"], same["UMask"]))
        self.assertIn(UNIT, ratings.MASKED_UNITS)
        self.assertIn(UNIT, health.UNITS_OPTIONAL)
        self.assertNotIn(UNIT, health.UNITS_REQUIRED)

    def test_install_manifest_enable_and_uninstall(self):
        r = tmpdir(self, "otvrfs")
        p = sh([INSTALL, "--root", r, "--dry-run", "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(tree(r), [])
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), encoding="utf-8") as f:
            man = f.read()
        for path in ("/etc/systemd/system/" + UNIT, "/opt/opentv/lib/opentv_pi/cecreturn.py"):
            self.assertIn("  " + path + "\n", man)
        self.assertEqual(health.verify_manifest(r)["mismatch"], [])
        self.assertRegex(out, r"systemctl enable [^\n]*%s" % re.escape(UNIT))
        self.assertRegex(out, r"systemctl restart [^\n]*%s" % re.escape(UNIT))
        # mise à jour : un état failed (78) précédent est effacé AVANT le redémarrage
        i_reset = out.index("systemctl reset-failed " + UNIT)
        i_restart = re.search(r"systemctl restart [^\n]*%s" % re.escape(UNIT), out).start()
        self.assertLess(i_reset, i_restart)
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        i_disable = out.index("systemctl disable --now")
        block = out[i_disable:out.index("\n", out.index(UNIT, i_disable))]
        self.assertIn(UNIT, block)
        self.assertLess(i_disable, out.index("cec-bootstrap --release"))         # arrêtée avant le retour arrière
        self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", UNIT)))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))


# --------------------------------------------------------------------------- version et documentation
class TestReleaseDocs(unittest.TestCase):
    def test_version_and_docs(self):
        self.assertEqual(VERSION, "0.7.4")
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                text = f.read()
            for need in ("opentv-cec-return.service", "ROUTING_CHANGE", "SET_STREAM_PATH", "0.0.0.0",
                         "OPENTV_CEC_RETURN_RECOVERY", "CAP_NET_ADMIN", "Samsung Home", "cec_return_ready",
                         "cec_return_failed", "\"result\":\"announced\"", "transmission"):
                self.assertTrue(need in text, (name, need))
            self.assertNotIn("\"recovered\"", text)
            # redécouverte bornée après un changement d'état attendu (plus de relecture unique de 2 s puis 30 s)
            for need in ("\"rediscover\":true", "adapter_state_inconsistent", "30 s"):
                self.assertTrue(need in text, (name, need))
            self.assertNotIn("relecture en 2 s ;", text)
            # 0.7.4 : `announced` rétablit, le <Poll> neutre maintient ; transport seulement, touches = seule preuve
            for need in ("cec_return_keepalive", "OPENTV_CEC_RETURN_KEEPALIVE", "POLL", "15 s", "-s -t 0 -P",
                         "maintien", "transport", "six touches"):
                self.assertTrue(need in text, (name, need))
            # revue Stage-1 : cadence réelle, annonce prouvée par la sortie exacte, Home ≠ « aucune émission »
            for need in ("jamais deux POLL à moins de 15 s", "Tx, Error", "stdout+stderr"):
                self.assertTrue(need in text, (name, need))
            flat = re.sub(r"\s+", " ", text)
            self.assertIsNone(re.search(r"Home[^.;|]{0,40}(aucune|sans) émission", flat), name)
            self.assertNotIn("toutes les 15 s exactement", flat)
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("0.7.4", readme)
        gate = next(line for line in readme.splitlines() if line.startswith("| **Retour d'application native"))
        for need in ("cec_return_ready", "\"ready\":false", "cec_return_failed", "NRestarts"):
            self.assertIn(need, gate)
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            agents = f.read()
        for need in ("cecreturn", "0.7.4", "POLL", "cec_return_keepalive", "transport"):
            self.assertIn(need, agents)


if __name__ == "__main__":
    unittest.main()
