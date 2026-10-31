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
from tests.test_remote import BOOT_A, Base, Clock, FakeBus, make_root, tmpdir
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
    """Sortie de `cec-ctl -m` simulée : lignes horodatées sur l'horloge partagée, puis fin (ou silence)."""

    def __init__(self, clock, script, end="eof"):
        self.clock, self.script, self.end = clock, list(script), end
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

    def close(self):
        self.closed = True


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

    def watch(self, script, dev="/dev/cec0", pa="1.0.0.0", mask=0x0010, mon=None, end="eof"):
        self.src = FakeLines(self.clock, script, end)
        mon = mon or self.monitor([self.src])
        return mon, mon.watch(dev, pa, mask)

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
                         ("recovered", "/dev/cec0", "1.0.0.0"))
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
        mon.watch("/dev/cec1", "2.1.0.0", 0x0010)
        self.assertIn(["cec-ctl", "-d", "/dev/cec1", "-s", "--active-source", "phys-addr=2.1.0.0"], bus.transmits())
        # la séquence visant 1.0.0.0 n'appartient pas à une boîte en 2.1.0.0
        bus2 = RecBus(self.clock, {"/dev/cec1": {"pa": "2.1.0.0", "state": "ours"}})
        mon2 = self.monitor([captured("1.0.0.0", own="2.1.0.0")], bus=bus2)
        self.assertEqual(mon2.watch("/dev/cec1", "2.1.0.0", 0x0010), "eof")
        self.assertEqual(bus2.transmits(), [])
        src = module_source()
        for literal in ("1.0.0.0", "/dev/cec0", "event3", "/dev/input"):
            self.assertNotIn(literal, src)

    def test_restart_after_youtube_never_reannounces(self):
        """Service (ou cec-ctl) redémarré pendant YouTube : la première moitié n'a pas été vue — ambiguïté, rien."""
        mon = self.monitor([at(0, state_event()) + at(1, active_source()) + at(1, routing_change()),
                            at(0, state_event()) + at(0.05, stream_path()) + at(5, power_poll())])
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), "eof")
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), "eof")
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
                self.assertEqual(self.bus.transmits(), [], name)
                self.assertEqual(self.recoveries(), [], name)

    def test_oversize_or_unknown_output_ends_watch_without_emission(self):
        mon = self.monitor([FakeLines(self.clock, at(0, active_source()) + at(60, routing_change()), end="oversize")])
        self.assertEqual(mon.watch("/dev/cec0", "1.0.0.0", 0x10), "oversize")
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
                self.assertEqual(why, "adapter_state_changed")
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
        fd2 = cecboot._lock(self.state)                              # libre ensuite : la reprise passe
        os.close(fd2)
        self.assertEqual(self.monitor().recover("/dev/cec0", "1.0.0.0")["result"], "recovered")

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

    def test_cooldown_and_hourly_cap(self):
        two = captured(youtube_s=10) + captured(youtube_s=10)          # deux retours en < COOLDOWN_S
        self.watch(two)
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual(res.count("recovered"), 1, res)
        self.assertIn("cooldown", res)
        self.bus.calls.clear()
        self.logs.clear()
        mon = self.monitor([FakeLines(self.clock, sum((captured(youtube_s=cecreturn.COOLDOWN_S + 5)
                                                       for _ in range(cecreturn.MAX_PER_HOUR + 3)), []))])
        mon.watch("/dev/cec0", "1.0.0.0", 0x10)
        res = [e["result"] for e in self.recoveries()]
        self.assertEqual(res.count("recovered"), cecreturn.MAX_PER_HOUR, res)
        self.assertIn("rate_limited", res)
        self.assertEqual(self.bus.opts().count("--active-source"), cecreturn.MAX_PER_HOUR)

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
        for ad in ({}, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"}},
                   {"/dev/cec0": {"pa": "1.0.0.0", "state": "foreign"}},
                   {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"}}):
            with self.subTest(ad):
                self.spawned.clear()
                self.sleeps.clear()
                bus = RecBus(self.clock, ad)
                self.monitor([], bus=bus).serve(max_cycles=3)
                self.assertEqual(self.spawned, [])
                self.assertEqual(bus.transmits(), [])
                self.assertEqual(self.sleeps, [cecreturn.DISCOVERY_RETRY_S] * 3)

    def test_monitor_failures_restart_with_bounded_backoff(self):
        n = len(cecreturn.RESTART_BACKOFF_S) + 3
        scripts = [OSError("cec-ctl absent")] + [[] for _ in range(n - 1)]   # échec de lancement puis fins immédiates
        self.monitor(scripts).serve(max_cycles=n)
        self.assertEqual(len(self.spawned), n)
        self.assertEqual(self.sleeps, [cecreturn.RESTART_BACKOFF_S[min(i, len(cecreturn.RESTART_BACKOFF_S) - 1)]
                                       for i in range(n)])
        self.assertTrue(all(s >= 5 for s in self.sleeps))
        self.assertLessEqual(max(self.sleeps), 300)
        self.assertEqual(self.bus.transmits(), [])
        ends = [e["reason"] for e in self.logs if e.get("event") == "cec_return_watch_end"]
        self.assertEqual(ends[0], "spawn_failed")

    def test_long_healthy_watch_resets_backoff(self):
        long_ok = [(cecreturn.HEALTHY_RUN_S + 1, "")]                   # liste : horloge prise au lancement
        self.monitor([[], [], long_ok, []]).serve(max_cycles=4)
        b = cecreturn.RESTART_BACKOFF_S
        self.assertEqual(self.sleeps, [b[0], b[1], b[0], b[1]])

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
        # mode moniteur du noyau (CAP_NET_ADMIN, passif) : seule capacité, confinée à un réseau privé
        self.assertEqual((s["CapabilityBoundingSet"], s["AmbientCapabilities"]), ("CAP_NET_ADMIN", "CAP_NET_ADMIN"))
        self.assertEqual((s["NoNewPrivileges"], s["PrivateNetwork"], s["RestrictAddressFamilies"]),
                         ("yes", "yes", "AF_UNIX"))
        self.assertEqual(sorted(re.findall(r"^DeviceAllow=(.+)$", text, re.M)), ["char-cec rw"])
        self.assertEqual((s["DevicePolicy"], s["ProtectSystem"], s["ProtectHome"]), ("closed", "strict", "yes"))
        self.assertEqual((s["RuntimeDirectory"], s["RuntimeDirectoryPreserve"]), ("opentv-cec", "yes"))
        self.assertEqual(s["Restart"], "on-failure")
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
                         "OPENTV_CEC_RETURN_RECOVERY", "CAP_NET_ADMIN", "Samsung Home"):
                self.assertTrue(need in text, (name, need))
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            self.assertIn("0.7.3", f.read())
        with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
            self.assertIn("cecreturn", f.read())


if __name__ == "__main__":
    unittest.main()
