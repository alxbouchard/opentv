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
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests._common import PI4, ROOT
from tests.test_install import INSTALL, UNINSTALL, sh, tree
from tests.test_remote import BOOT_A, Base, FakeBus, make_root, tmpdir
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


class RecBus(FakeBus):
    """cec-ctl simulé + instant de chaque émission (pour mesurer le délai de reprise)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.at = []

    def __call__(self, argv, timeout):
        self.at.append((self.clock.t, list(argv)))
        return super().__call__(argv, timeout)


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
                res = self.monitor().recover("/dev/cec0", "1.0.0.0")
                self.assertEqual(res["result"], "refused_tv_not_on")
                self.assertEqual(self.bus.opts(), ["--give-device-power-status"])
                self.assertEqual(wake_args(self.bus), [])

    def test_adapter_changed_or_foreign_refuses_before_any_emission(self):
        for state, pa in (("foreign", "1.0.0.0"), ("empty", "1.0.0.0"), ("ours", "2.0.0.0"), ("ours", "f.f.f.f")):
            with self.subTest((state, pa)):
                bus = RecBus(self.clock, {"/dev/cec0": {"pa": pa, "state": state}})
                res = self.monitor(bus=bus).recover("/dev/cec0", "1.0.0.0")
                self.assertEqual(res["result"], "adapter_changed")
                self.assertEqual(bus.transmits(), [])
                if state == "foreign":
                    self.assertNotIn("SecretBox", json.dumps(res))

    def test_uses_existing_lock_and_waits_bounded(self):
        fd = cecboot._lock(self.state)
        try:
            t0 = self.clock.t
            res = self.monitor().recover("/dev/cec0", "1.0.0.0")
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
        self.assertEqual(self.monitor().recover("/dev/cec0", "1.0.0.0")["result"], "announced")

    def test_lock_released_during_bounded_wait_then_announces(self):
        fd = [cecboot._lock(self.state)]
        t0 = self.clock.t

        def sleep(s):                                                 # le passage de la minuterie finit après ~3 s
            self.sleep(s)
            if fd[0] is not None and self.clock.t - t0 >= 3.0:
                os.close(fd[0])
                fd[0] = None
        mon = self.monitor()
        mon.sleep = sleep
        res = mon.recover("/dev/cec0", "1.0.0.0")
        self.assertEqual(res["result"], "announced")
        self.assertIsNone(fd[0])
        self.assertLess(self.clock.t - t0, cecreturn.LOCK_WAIT_S)
        self.assertEqual(self.bus.opts(), ANNOUNCE_SEQ)

    def test_lock_released_after_recovery_so_timer_and_activate_coexist(self):
        self.monitor().recover("/dev/cec0", "1.0.0.0")
        fd = cecboot._lock(self.state)
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_failed_active_source_reports_failure_and_no_menu_status(self):
        bus = RecBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, fail={"--active-source"})
        res = self.monitor(bus=bus).recover("/dev/cec0", "1.0.0.0")
        self.assertEqual(res["result"], "failed")
        self.assertNotIn("--menu-status", bus.opts())

    def test_budget_bounded_when_bus_hangs(self):
        def hang(argv, timeout):
            self.clock.t += timeout + cecboot.KILL_GRACE_S
            return 124, ""
        t0 = self.clock.t
        res = self.monitor(bus=hang).recover("/dev/cec0", "1.0.0.0")
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
        for argv, _ in self.bus.calls:
            opts = [o for o in argv if o.startswith("-")]
            self.assertTrue(set(opts) <= {"-d", "-s", "-t", "--give-device-power-status", "--active-source",
                                          "--menu-status"}, argv)
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

        def factory(root, state_dir):
            return real(self.root, runner=self.bus, spawn=lambda argv: src, mono=self.clock, sleep=self.sleep,
                        state_dir=state_dir, log=self.logs.append)
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
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            self.assertIn("#OPENTV_CEC_RETURN_RECOVERY=1", f.read())

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
            seen.update(root=me.root, state_dir=me.state_dir)
        with mock.patch.object(os, "geteuid", lambda: 1000), mock.patch.object(cecreturn.Monitor, "serve", serve):
            code, _ = self.run_cli("--env", env, "cec-return-monitor", "--root", d, "--state-dir", d)
        self.assertEqual((code, seen), (0, {"root": d, "state_dir": d}))

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
        self.assertEqual(VERSION, "0.7.3")
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
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("0.7.3", readme)
        gate = next(line for line in readme.splitlines() if line.startswith("| **Retour d'application native"))
        for need in ("cec_return_ready", "\"ready\":false", "cec_return_failed", "NRestarts"):
            self.assertIn(need, gate)
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            self.assertIn("cecreturn", f.read())


if __name__ == "__main__":
    unittest.main()
