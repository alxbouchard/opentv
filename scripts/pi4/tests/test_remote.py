"""Télécommande : amorçage HDMI-CEC borné/idempotent/réversible (opentv-cec), table de touches du noyau, unités,
installation/désinstallation, normalisation des touches (keys.js) et comportement réel de tv.js dans Chromium."""
import configparser
import contextlib
import html
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

from tests._common import PI4
from tests.test_install import INSTALL, UNINSTALL, sh, tree
from tests.test_service import find_chromium, make_app, Servers
from opentv_pi import app as appmod, cecboot, cli, config, health, phoneremote

# sorties réelles de cec-ctl 1.22.1 (Raspberry Pi 4, noyau 6.12, TV Samsung sur HDMI0)
OURS_TMPL = """Driver Info:
\tDriver Name                : {driver}
\tAdapter Name               : {adapter}
\tCapabilities               : 0x0000031e
\t\tLogical Addresses
\t\tTransmit
\t\tPassthrough
\t\tRemote Control Support
\t\tConnector Info
\tDriver version             : 6.12.109
\tAvailable Logical Addresses: 1
\tDRM Connector Info         : card 1, connector 33
\tPhysical Address           : {pa}
\tLogical Address Mask       : {mask}
\tCEC Version                : 2.0
\tVendor ID                  : 0x000c03 (HDMI)
\tOSD Name                   : '{osd}'
\tLogical Addresses          : 1 (Allow RC Passthrough)

\t  Logical Address          : 4 (Playback Device 1)
\t    Primary Device Type    : {ptype}
\t    Logical Address Type   : {ptype}
\t    All Device Types       : {ptype}
\t    RC TV Profile          : None
\t    Device Features        :
\t\tNone
"""
EMPTY_TMPL = """Driver Info:
\tDriver Name                : {driver}
\tAdapter Name               : {adapter}
\tCapabilities               : 0x0000031e
\t\tLogical Addresses
\t\tTransmit
\t\tPassthrough
\t\tRemote Control Support
\t\tConnector Info
\tDriver version             : 6.12.109
\tAvailable Logical Addresses: 1
\tDRM Connector Info         : card 1, connector 42
\tPhysical Address           : {pa}
\tLogical Address Mask       : 0x0000
\tCEC Version                : 2.0
\tOSD Name                   : ''
\tLogical Addresses          : 0 
"""
POWER_TMPL = """
Transmit from Playback Device 1 to TV (4 to 0):
GIVE_DEVICE_POWER_STATUS (0x8f)
    Received from TV (0):
    REPORT_POWER_STATUS (0x90):
\tpwr-state: {state} (0x00)
\tSequence: 49 Tx Timestamp: 7752.116s Rx Timestamp: 7752.204s
"""
BOOT_A = "11111111-2222-3333-4444-555555555555"
BOOT_B = "99999999-2222-3333-4444-555555555555"
TRANSMITS = ("--playback", "--image-view-on", "--active-source", "--menu-status", "--give-device-power-status",
             "--clear")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeBus:
    """cec-ctl simulé : état par adaptateur, chaque appel enregistré (argv, délai) ; avance l'horloge d'un pas."""

    def __init__(self, clock, adapters, power="on", fail=()):
        self.clock, self.ad, self.power, self.fail = clock, adapters, power, set(fail)
        self.calls = []

    def render(self, dev):
        a = self.ad[dev]
        n = dev[-1]
        base = dict(driver=a.get("driver", "vc4_hdmi"), adapter="vc4-hdmi-" + n, pa=a["pa"])
        if a["state"] == "empty":
            return EMPTY_TMPL.format(**base)
        if a["state"] == "ours":
            mask = "0x0010" if a["pa"] != "f.f.f.f" else "0x0000"
            return OURS_TMPL.format(mask=mask, osd="OpenTV", ptype="Playback", **base)
        return OURS_TMPL.format(mask="0x0002", osd="SecretBox", ptype="Recorder", **base)

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        self.clock.t += 0.05
        dev = argv[2]
        if dev not in self.ad or self.ad[dev].get("unreadable"):
            return 1, "open failed"
        opt = next((o for o in TRANSMITS if o in argv), None)
        if opt in self.fail:
            return 1, ""
        if opt is None:
            return 0, self.render(dev)
        if opt == "--playback":
            self.ad[dev]["state"] = "ours"
        elif opt == "--clear":
            self.ad[dev]["state"] = "empty"
        elif opt == "--give-device-power-status":
            return (0, POWER_TMPL.format(state=self.power)) if self.power != "silent" else (0, "Timed out\n")
        return 0, ""

    def transmits(self):
        return [a for a, _ in self.calls if any(o in a for o in TRANSMITS)]

    def opts(self):
        return [next(o for o in TRANSMITS if o in a) for a in self.transmits()]


class FakeKeymap:
    def __init__(self, table=None, broken=()):
        self.table = table if table is not None else {}
        self.broken = set(broken)
        self.sets = []

    def get(self, dev, sc):
        if dev in self.broken:
            raise OSError(13, "denied")
        return self.table.setdefault(dev, {0x00: cecboot.KEY_OK}).get(sc, 0)

    def set(self, dev, sc, code):
        if dev in self.broken:
            raise OSError(13, "denied")
        self.sets.append((dev, sc, code))
        self.table.setdefault(dev, {})[sc] = code


def tmpdir(tc, prefix):
    d = tempfile.mkdtemp(prefix=prefix, dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def make_root(tc, devs=("cec0", "cec1"), inputs=True):
    r = tmpdir(tc, "otvcec")
    os.makedirs(os.path.join(r, "dev"))
    for d in devs:
        open(os.path.join(r, "dev", d), "w").close()
    if inputs:
        cls = os.path.join(r, "sys", "class", "input")
        os.makedirs(cls)
        for i, (hdmi, ev, jack) in enumerate((("fef00700", 3, 4), ("fef05700", 5, 6))):
            for kind, name, num in (("rc/rc%d/input%d" % (i, ev), "vc4-hdmi-%d" % i, ev),
                                    ("sound/card%d/input%d" % (i + 1, jack), "vc4-hdmi-%d HDMI Jack" % i, jack)):
                d = os.path.join(r, "sys", "devices", "platform", "soc", hdmi + ".hdmi", kind)
                os.makedirs(os.path.join(d, "event%d" % num))
                with open(os.path.join(d, "name"), "w") as f:
                    f.write(name + "\n")
                os.symlink("..", os.path.join(d, "event%d" % num, "device"))
                os.symlink(os.path.join(d, "event%d" % num), os.path.join(cls, "event%d" % num))
    return r


def make_proc(tc, boot):
    d = tmpdir(tc, "otvproc")
    p = os.path.join(d, "sys", "kernel", "random")
    os.makedirs(p)
    with open(os.path.join(p, "boot_id"), "w") as f:
        f.write(boot + "\n")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = tmpdir(self, "otvcecst")
        self.proc = make_proc(self, BOOT_A)
        self.km = FakeKeymap()

    def boot(self, root, bus, **kw):
        kw.setdefault("state_dir", self.state)
        kw.setdefault("proc_root", self.proc)
        return cecboot.bootstrap(root, runner=bus, mono=self.clock, keymap_io=self.km, **kw)


# --------------------------------------------------------------------------- analyse des sorties réelles
class TestParse(unittest.TestCase):
    def test_real_outputs(self):
        ours = cecboot.parse_info(OURS_TMPL.format(driver="vc4_hdmi", adapter="vc4-hdmi-0", pa="1.0.0.0",
                                                    mask="0x0010", osd="OpenTV", ptype="Playback"))
        self.assertEqual((ours["physical_address"], ours["mask"], ours["osd"], ours["logical_addresses"]),
                         ("1.0.0.0", 0x10, "OpenTV", 1))
        self.assertEqual((cecboot.classify(ours), cecboot.connected(ours), cecboot.is_vc4(ours)), ("ours", True, True))
        empty = cecboot.parse_info(EMPTY_TMPL.format(driver="vc4_hdmi", adapter="vc4-hdmi-1", pa="f.f.f.f"))
        # « Available Logical Addresses: 1 » ne doit pas faire passer un adaptateur vide pour configuré
        self.assertEqual(empty["logical_addresses"], 0)
        self.assertEqual((cecboot.classify(empty), cecboot.connected(empty)), ("empty", False))
        foreign = cecboot.parse_info(OURS_TMPL.format(driver="vc4_hdmi", adapter="vc4-hdmi-0", pa="1.0.0.0",
                                                       mask="0x0002", osd="Kodi", ptype="Recorder"))
        self.assertEqual(cecboot.classify(foreign), "foreign")
        self.assertEqual(cecboot.classify(cecboot.parse_info("")), "empty")
        self.assertFalse(cecboot.connected(cecboot.parse_info("")))

    def test_power_state_parse(self):
        clock = Clock()
        for st in ("on", "standby", "to-on", "to-standby"):
            bus = FakeBus(clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, power=st)
            self.assertEqual(cecboot.power_state(cecboot._Run(bus, clock, 30), "/dev/cec0"), st)
        bus = FakeBus(clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, power="silent")
        self.assertEqual(cecboot.power_state(cecboot._Run(bus, clock, 30), "/dev/cec0"), "unknown")


# --------------------------------------------------------------------------- amorçage
class TestBootstrap(Base):
    def test_no_adapter_runs_nothing(self):
        r = make_root(self, devs=())
        bus = FakeBus(self.clock, {})
        res = self.boot(r, bus)
        self.assertEqual((res["ok"], res["result"], res["commands"]), (True, "no_adapter", 0))
        self.assertEqual(bus.calls, [])
        self.assertEqual(os.listdir(self.state), [])

    def test_disconnected_never_transmits(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        res = self.boot(r, bus)
        self.assertEqual((res["ok"], res["result"], res["selected"]), (True, "disconnected", None))
        self.assertEqual(bus.transmits(), [])
        self.assertTrue(all(len(a) == 3 for a, _ in bus.calls))           # lectures seules : cec-ctl -d <dev>
        self.assertEqual([e["connected"] for e in res["adapters"]], [False, False])

    def test_unreadable_and_non_vc4_adapters_are_ignored(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty", "unreadable": True},
                                   "/dev/cec1": {"pa": "2.0.0.0", "state": "empty", "driver": "pulse8-cec"}})
        res = self.boot(r, bus)
        self.assertEqual(res["result"], "no_vc4_adapter")
        self.assertEqual(bus.transmits(), [])

    def test_dynamic_adapter_and_physical_address(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                   "/dev/cec1": {"pa": "2.1.0.0", "state": "empty"}})
        res = self.boot(r, bus)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["result"], res["selected"], res["physical_address"]), ("ready", "/dev/cec1", "2.1.0.0"))
        self.assertEqual(res["registration"], "registered")
        # défaut sûr : seulement la déclaration Playback ; ni réveil, ni source active, ni question d'alimentation
        self.assertEqual(bus.transmits(), [["cec-ctl", "-d", "/dev/cec1", "--playback", "--osd-name", "OpenTV"]])
        self.assertEqual(res["announce"], {"result": "not_requested"})
        self.assertEqual(bus.ad["/dev/cec0"]["state"], "empty")                   # adaptateur débranché intact
        self.assertNotIn("--osd-name", json.dumps(res))
        self.assertFalse(os.path.exists(os.path.join(self.state, cecboot.STATE_FILE)))

    def test_timer_never_announces_on_any_tv_state_boot_or_port_change(self):
        """Minuterie (sans --force-announce) : jamais <Image View On> ni <Active Source>, TV allumée, en veille,
        inconnue, en réveil, après redémarrage de la boîte ou changement de port HDMI."""
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        for power in ("on", "standby", "to-on", "to-standby", "silent", "on"):
            bus.power = power
            res = self.boot(r, bus)
            self.assertEqual((res["ok"], res["result"], res["announce"]["result"]), (True, "ready", "not_requested"))
        bus.ad["/dev/cec0"]["pa"] = "3.0.0.0"                                    # TV branchée sur un autre port
        self.assertEqual(self.boot(r, bus)["result"], "ready")
        self.proc = make_proc(self, BOOT_B)                                      # redémarrage de la boîte
        self.assertEqual(self.boot(r, bus)["result"], "ready")
        self.assertEqual(bus.opts(), ["--playback"])                             # une seule émission en tout
        for o in ("--image-view-on", "--active-source", "--menu-status", "--give-device-power-status"):
            self.assertNotIn(o, bus.opts())

    def test_force_announce_only_when_tv_reports_on(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "2.1.0.0", "state": "ours"}})
        for power in ("standby", "to-standby", "to-on", "silent"):               # jamais de réveil, même forcé
            bus.power = power
            res = self.boot(r, bus, force_announce=True)
            self.assertEqual((res["ok"], res["result"], res["announce"]["result"]),
                             (False, "tv_not_on", "refused_tv_not_on"))
        self.assertEqual(set(bus.opts()), {"--give-device-power-status"})
        self.assertFalse(os.path.exists(os.path.join(self.state, cecboot.STATE_FILE)))
        bus.power = "on"
        n = len(bus.transmits())
        res = self.boot(r, bus, force_announce=True)
        self.assertEqual((res["ok"], res["result"], res["announce"]["result"]), (True, "ready", "done"))
        self.assertEqual(bus.transmits()[n:], [
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--give-device-power-status"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "--active-source", "phys-addr=2.1.0.0"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--menu-status", "menu-state=activated"]])
        with open(os.path.join(self.state, cecboot.STATE_FILE)) as f:            # témoin (lecture d'état seulement)
            self.assertEqual(json.load(f)["done"], {"/dev/cec0": "2.1.0.0"})
        n = len(bus.transmits())
        self.assertEqual(self.boot(r, bus)["announce"]["result"], "not_requested")   # le témoin ne réannonce jamais
        self.assertEqual(len(bus.transmits()), n)

    def test_foreign_configuration_is_never_replaced(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "foreign"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        res = self.boot(r, bus)
        self.assertEqual((res["ok"], res["result"]), (True, "foreign_config_untouched"))
        self.assertEqual(bus.transmits(), [])
        self.assertNotIn("SecretBox", json.dumps(res))                           # jamais de nom OSD étranger

    def test_no_wake_parameter_remains(self):
        import inspect
        self.assertNotIn("wake_tv", inspect.signature(cecboot.bootstrap).parameters)
        self.assertNotIn("wake_tv", inspect.signature(cecboot.announce).parameters)
        self.assertNotIn("OPENTV_CEC_WAKE_TV", config.DEFAULTS)

    def test_failures_are_reported_and_retried(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, fail={"--active-source"})
        res = self.boot(r, bus, force_announce=True)
        self.assertEqual((res["ok"], res["result"]), (False, "announce_failed"))
        self.assertNotIn("--menu-status", bus.opts())
        self.assertFalse(os.path.exists(os.path.join(self.state, cecboot.STATE_FILE)))   # aucun témoin après échec
        bus.fail.clear()
        self.assertEqual(self.boot(r, bus, force_announce=True)["result"], "ready")
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"}}, fail={"--playback"})
        res = self.boot(r, bus, state_dir=tmpdir(self, "otvcecst"))
        self.assertEqual((res["ok"], res["result"]), (False, "register_failed"))
        self.assertNotIn("--active-source", bus.opts())

    def test_claim_pending_never_announces(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        orig = bus.render
        bus.render = lambda dev: orig(dev).replace("0x0010", "0x0000")          # adresse logique pas encore obtenue
        res = self.boot(r, bus)
        self.assertEqual(res["result"], "claim_pending")
        self.assertEqual(bus.transmits(), [])

    def test_state_dir_missing_and_concurrent_run(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        res = self.boot(r, bus, state_dir=os.path.join(self.state, "absent"))
        self.assertEqual((res["ok"], res["result"]), (False, "state_dir_missing"))
        lock = cecboot._lock(self.state)
        self.addCleanup(os.close, lock)
        res = self.boot(r, bus)
        self.assertEqual((res["ok"], res["result"]), (True, "busy"))     # minuterie : passage normal
        self.assertEqual(bus.calls, [])

    def test_corrupt_or_foreign_boot_marker_is_ignored(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        for bad in ("{", "[]", json.dumps({"boot_id": BOOT_B, "done": {"/dev/cec0": "1.0.0.0"}})):
            with open(os.path.join(self.state, cecboot.STATE_FILE), "w") as f:
                f.write(bad)
            self.assertEqual(self.boot(r, bus, force_announce=True)["announce"]["result"], "done")
        with open(os.path.join(self.state, cecboot.STATE_FILE)) as f:
            self.assertEqual(json.load(f), {"v": 1, "boot_id": BOOT_A, "done": {"/dev/cec0": "1.0.0.0"}})

    def test_root_status_state_and_lock_refuse_symlink_fifo_and_hardlink(self):
        """Le dossier appartient à opentv mais l'état peut être lu par une CLI root : aucune traversée ni attente."""
        outside = os.path.join(tmpdir(self, "otvcec-outside"), "private")
        valid = json.dumps({"boot_id": BOOT_A, "done": {"leak": "SECRET"}})
        with open(outside, "w") as f:
            f.write(valid)
        marker = os.path.join(self.state, cecboot.STATE_FILE)
        os.symlink(outside, marker)
        self.assertEqual(cecboot.load_announced(self.state, BOOT_A), {})
        os.unlink(marker)
        os.mkfifo(marker)
        self.assertEqual(cecboot.load_announced(self.state, BOOT_A), {})       # O_NONBLOCK : ne pend jamais
        os.unlink(marker)
        os.link(outside, marker)
        self.assertEqual(cecboot.load_announced(self.state, BOOT_A), {})
        os.unlink(marker)
        with open(marker, "w") as f:
            f.write("[" * 4096)   # Python 3.11 lève RecursionError plutôt que JSONDecodeError
        self.assertEqual(cecboot.load_announced(self.state, BOOT_A), {})
        os.unlink(marker)

        lock = os.path.join(self.state, cecboot.LOCK_FILE)
        os.symlink(outside, lock)
        self.assertIsNone(cecboot._lock(self.state))
        os.unlink(lock)
        os.mkfifo(lock)
        self.assertIsNone(cecboot._lock(self.state))
        os.unlink(lock)
        os.link(outside, lock)
        self.assertIsNone(cecboot._lock(self.state))


# --------------------------------------------------------------------------- budget
class TestBounded(Base):
    def test_every_command_hangs_whole_run_stays_in_budget(self):
        r = make_root(self, devs=("cec0", "cec1", "cec2", "cec3", "cec4", "cec5"))
        calls = []

        def hang(argv, timeout):
            calls.append(timeout)
            self.clock.t += timeout + cecboot.KILL_GRACE_S                     # délai entier + mise à mort
            return 124, ""
        t0 = self.clock.t
        res = self.boot(r, hang)
        self.assertEqual(res["result"], "no_vc4_adapter")                   # lectures abandonnées, rien émis
        self.assertLessEqual(self.clock.t - t0, cecboot.RUN_BUDGET_S)
        self.assertLessEqual(len(calls), cecboot.MAX_ADAPTERS)
        self.assertTrue(all(0 < t <= cecboot.READ_TIMEOUT_S for t in calls))

    def test_slow_bus_truncates_to_remaining_budget(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        inner = bus.__call__

        def slow(argv, timeout):
            self.clock.t += timeout * 0.9 + cecboot.KILL_GRACE_S                # presque l'échéance à chaque fois
            return inner(argv, timeout)
        t0 = self.clock.t
        res = self.boot(r, slow, budget=20)
        self.assertEqual((res["ok"], res["result"]), (False, "budget_exceeded"))
        self.assertLessEqual(self.clock.t - t0, 20 + 1)
        self.assertTrue(all(t <= 20 for _, t in bus.calls))
        self.assertFalse(os.path.exists(os.path.join(self.state, cecboot.STATE_FILE)))

    def test_budget_fits_unit_timeout(self):
        with open(os.path.join(PI4, "systemd", "opentv-cec.service")) as f:
            m = re.search(r"^TimeoutStartSec=(\d+)$", f.read(), re.M)
        self.assertEqual(int(m.group(1)), cecboot.UNIT_TIMEOUT_START_S)
        self.assertLessEqual(cecboot.RUN_BUDGET_S + 15, cecboot.UNIT_TIMEOUT_START_S)   # marge démarrage Python

    def test_default_runner_is_used_with_fixed_argv_no_shell(self):
        import inspect
        src = inspect.getsource(cecboot)
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)


# --------------------------------------------------------------------------- clavier noyau
class TestKeymap(Base):
    def test_find_input_matches_rc_device_not_jack(self):
        r = make_root(self)
        self.assertEqual(cecboot.find_input(r, "vc4-hdmi-0"), "/dev/input/event3")
        self.assertEqual(cecboot.find_input(r, "vc4-hdmi-1"), "/dev/input/event5")
        self.assertIsNone(cecboot.find_input(r, "vc4-hdmi-0 HDMI Jack"))       # pas un récepteur RC
        self.assertIsNone(cecboot.find_input(r, None))
        self.assertIsNone(cecboot.find_input(make_root(self, inputs=False), "vc4-hdmi-0"))

    def test_remap_default_only_idempotent_and_reversible(self):
        km = FakeKeymap({"/dev/input/event3": {0x00: cecboot.KEY_OK}, "/dev/input/event5": {0x00: 999}})
        self.assertEqual(cecboot.apply_keymap(km, "/dev/input/event3"), {"0x00": "remapped"})
        self.assertEqual(km.table["/dev/input/event3"][0x00], cecboot.KEY_ENTER)
        self.assertEqual(cecboot.apply_keymap(km, "/dev/input/event3"), {"0x00": "already"})
        self.assertEqual(cecboot.apply_keymap(km, "/dev/input/event5"), {"0x00": "custom_untouched"})
        self.assertEqual(cecboot.apply_keymap(km, "/dev/input/event3", release=True), {"0x00": "restored"})
        self.assertEqual(km.table["/dev/input/event3"][0x00], cecboot.KEY_OK)
        self.assertEqual(cecboot.apply_keymap(km, "/dev/input/event5", release=True), {"0x00": "custom_untouched"})
        self.assertEqual(len(km.sets), 2)
        self.assertEqual(cecboot.apply_keymap(FakeKeymap(broken={"x"}), "x"), {"0x00": "unavailable"})

    def test_bootstrap_remaps_every_vc4_receiver_even_disconnected(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        res = self.boot(r, bus)
        self.assertEqual([e["keymap"] for e in res["adapters"]], [{"0x00": "remapped"}, {"0x00": "remapped"}])
        res = self.boot(r, bus, keymap=False)
        self.assertNotIn("keymap", res["adapters"][0])

    def test_ioctl_layout(self):
        # _IOC(dir, 'E', 0x04, sizeof(struct input_keymap_entry) = 40)
        size = struct.calcsize(cecboot.KEYMAP_FMT)
        self.assertEqual(size, 40)
        self.assertEqual(cecboot.EVIOCGKEYCODE_V2, (2 << 30) | (size << 16) | (ord("E") << 8) | 0x04)
        self.assertEqual(cecboot.EVIOCSKEYCODE_V2, (1 << 30) | (size << 16) | (ord("E") << 8) | 0x04)
        e = struct.unpack(cecboot.KEYMAP_FMT, bytes(cecboot.KernelKeymap._entry(0x0d, 28)))
        self.assertEqual(e[:4], (0, 4, 0, 28))
        self.assertEqual(e[4][:4], b"\x0d\x00\x00\x00")

    def test_kernel_keymap_refuses_non_device_and_symlink(self):
        r = tmpdir(self, "otvkm")
        os.makedirs(os.path.join(r, "dev", "input"))
        with open(os.path.join(r, "dev", "input", "event3"), "w") as f:
            f.write("x")
        os.symlink(os.path.join(r, "dev", "input", "event3"), os.path.join(r, "dev", "input", "event4"))
        km = cecboot.KernelKeymap(r)
        for dev in ("/dev/input/event3", "/dev/input/event4", "/dev/input/event9"):
            with self.assertRaises(OSError):
                km.get(dev, 0)


# --------------------------------------------------------------------------- retour arrière
class TestRelease(Base):
    def test_release_clears_only_ours_and_restores_keymap(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                   "/dev/cec1": {"pa": "2.0.0.0", "state": "foreign"}})
        self.boot(r, bus)
        self.assertEqual(bus.ad["/dev/cec0"]["state"], "ours")
        before = tree(self.state)
        res = cecboot.release(r, runner=bus, mono=self.clock, keymap_io=self.km)
        self.assertTrue(res["ok"], res)
        self.assertEqual([e["result"] for e in res["adapters"]], ["cleared", "untouched"])
        self.assertEqual((bus.ad["/dev/cec0"]["state"], bus.ad["/dev/cec1"]["state"]), ("empty", "foreign"))
        self.assertEqual(self.km.table["/dev/input/event3"][0x00], cecboot.KEY_OK)
        self.assertEqual(tree(self.state), before)                               # aucune écriture de fichier
        self.assertEqual([a for a in bus.transmits() if "--clear" in a], [["cec-ctl", "-d", "/dev/cec0", "--clear"]])


# --------------------------------------------------------------------------- CLI
class TestCecStatus(Base):
    def test_status_reports_registration_announce_and_key_without_transmitting(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        self.boot(r, bus, force_announce=True)
        n = len(bus.transmits())
        from opentv_pi import cec
        rep = cec.discover(r, runner=bus)
        self.assertEqual(len(bus.transmits()), n)                                # lecture seule
        a0, a1 = rep["adapters"]
        self.assertEqual((a0["configuration"], a0["logical_address_claimed"], a0["adapter"]),
                         ("ours", True, "vc4-hdmi-0"))
        self.assertEqual((a1["configuration"], a1["tv_detected"]), ("empty", False))
        # CLI : témoin d'annonce lu depuis <root>/run/opentv-cec et boot_id de <root>/proc
        os.makedirs(os.path.join(r, "run"))
        shutil.copytree(self.state, os.path.join(r, "run", "opentv-cec"))
        shutil.copytree(os.path.join(self.proc, "sys"), os.path.join(r, "proc", "sys"))
        orig = cecboot.KernelKeymap.get
        cecboot.KernelKeymap.get = lambda me, dev, sc: self.km.get(dev, sc)
        self.addCleanup(setattr, cecboot.KernelKeymap, "get", orig)
        import opentv_pi.cec as cecmod
        orig_disc = cecmod.discover
        cecmod.discover = lambda root: orig_disc(root, runner=bus)
        self.addCleanup(setattr, cecmod, "discover", orig_disc)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["cec", "--root", r]), 0)
        rep = json.loads(out.getvalue())
        self.assertEqual(rep["bootstrap"], {"/dev/cec0": "1.0.0.0"})
        self.assertEqual([(x["announced_this_boot"], x["select_key"]) for x in rep["adapters"]],
                         [(True, "enter"), (False, "enter")])
        self.assertEqual(len(bus.transmits()), n)


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(list(argv))
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)                                   # une ligne JSON par passage
        return code, json.loads(lines[0])

    def test_cli_no_adapter_disabled_and_root_refusal(self):
        r = make_root(self, devs=())
        st = tmpdir(self, "otvcecst")
        env = os.path.join(st, "env")
        open(env, "w").close()
        if os.geteuid() == 0:
            code, out = self.run_cli("--env", env, "cec-bootstrap", "--root", r, "--state-dir", st)
            self.assertEqual((code, out["error"]), (2, "run_via_systemd"))
            return
        code, out = self.run_cli("--env", env, "cec-bootstrap", "--root", r, "--state-dir", st)
        self.assertEqual((code, out["event"], out["result"]), (0, "cec_bootstrap", "no_adapter"))
        with open(env, "w") as f:
            f.write("OPENTV_CEC_BOOTSTRAP=0\n")
        code, out = self.run_cli("--env", env, "cec-bootstrap", "--root", r, "--state-dir", st)
        self.assertEqual((code, out["result"]), (0, "disabled"))
        code, out = self.run_cli("--env", env, "cec-bootstrap", "--release", "--root", r)
        self.assertEqual((code, out["event"], out["adapters"]), (0, "cec_release", []))

    def test_config_flags(self):
        c = config.Config({})
        self.assertEqual((c.cec_bootstrap, c.cec_keymap, c.remote_mdns), (True, True, True))
        self.assertFalse(hasattr(c, "cec_wake_tv"))
        c = config.Config({"OPENTV_CEC_WAKE_TV": "1"})                           # ancienne clé : sans aucun effet
        self.assertFalse(hasattr(c, "cec_wake_tv"))
        with self.assertRaises(ValueError):
            config.Config({"OPENTV_CEC_KEYMAP": "yes"})
        with open(os.path.join(PI4, "opentv-pi.env.example")) as f:
            text = f.read()
        for k in ("OPENTV_CEC_BOOTSTRAP", "OPENTV_CEC_KEYMAP", "OPENTV_REMOTE_MDNS"):
            self.assertIn("#%s=%s" % (k, config.DEFAULTS[k]), text)
        self.assertNotIn("WAKE_TV", text)

    def test_timer_unit_never_forces_announce(self):
        svc = unit("opentv-cec.service")["Service"]
        self.assertEqual(svc["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap")
        self.assertNotIn("Environment", svc)
        with open(os.path.join(PI4, "systemd", "opentv-cec.service"), encoding="utf-8") as f:
            self.assertNotIn("WAKE", f.read())


# --------------------------------------------------------------------------- unités, installation, désinstallation
def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        cp.read_file(f)
    return cp


class TestUnitsInstall(unittest.TestCase):
    def root(self):
        return tmpdir(self, "otvrfs")

    def test_units(self):
        with open(os.path.join(PI4, "systemd", "opentv-cec.service"), encoding="utf-8") as f:
            text = f.read()
        svc = unit("opentv-cec.service")["Service"]
        self.assertEqual(svc["Type"], "oneshot")
        self.assertEqual(svc["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap")
        self.assertEqual((svc["User"], svc["CapabilityBoundingSet"], svc["NoNewPrivileges"]), ("opentv", "", "yes"))
        self.assertEqual((svc["PrivateNetwork"], svc["DevicePolicy"], svc["ProtectSystem"]), ("yes", "closed", "strict"))
        self.assertEqual(svc["RestrictAddressFamilies"], "AF_UNIX")
        self.assertEqual(sorted(re.findall(r"^DeviceAllow=(.+)$", text, re.M)), ["char-cec rw", "char-input rw"])
        self.assertEqual((svc["RuntimeDirectory"], svc["RuntimeDirectoryPreserve"]), ("opentv-cec", "yes"))
        self.assertNotIn("Restart", svc)
        self.assertNotIn("[Install]", text)                                      # déclenché par la minuterie seulement
        tm = unit("opentv-cec.timer")
        self.assertEqual(tm["Timer"]["Unit"], "opentv-cec.service")
        self.assertLessEqual(int(tm["Timer"]["OnBootSec"].rstrip("s")), 60)
        self.assertGreaterEqual(int(tm["Timer"]["OnUnitActiveSec"].rstrip("s")), 30)
        self.assertEqual(tm["Install"]["WantedBy"], "timers.target")
        self.assertIn("opentv-cec.timer", health.UNITS_OPTIONAL)
        self.assertNotIn("opentv-cec.timer", health.UNITS_REQUIRED)               # CEC : amélioration optionnelle
        announce = unit("opentv-cec-announce.service")["Service"]
        self.assertEqual(announce["ExecStart"],
                         "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap --force-announce")
        self.assertEqual((announce["User"], announce["SupplementaryGroups"]), ("opentv", "video input"))
        self.assertEqual((announce["RuntimeDirectory"], announce["PrivateNetwork"]), ("opentv-cec", "yes"))
        with open(os.path.join(PI4, "systemd", "opentv-cec-announce.service"), encoding="utf-8") as f:
            self.assertNotIn("[Install]", f.read())                              # jamais activée automatiquement

    def test_install_manifest_and_enable(self):
        r = self.root()
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            m = f.read()
        for path in ("/etc/systemd/system/opentv-cec.service", "/etc/systemd/system/opentv-cec.timer",
                     "/etc/systemd/system/opentv-cec-announce.service",
                     "/opt/opentv/lib/opentv_pi/cecboot.py", "/opt/opentv/share/ui/keys.js"):
            self.assertIn(path, m)
        out = p.stdout.decode()
        self.assertRegex(out, r"systemctl enable [^\n]*opentv-cec\.timer")
        self.assertRegex(out, r"systemctl restart [^\n]*opentv-cec\.timer")
        self.assertNotRegex(out, r"systemctl enable [^\n]*opentv-cec\.service")

    def test_uninstall_releases_cec_before_removing_files(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        os.makedirs(os.path.join(r, "run/opentv-cec"))
        with open(os.path.join(r, "run/opentv-cec", cecboot.STATE_FILE), "w") as f:
            f.write("{}")
        before = tree(r)
        p = sh([UNINSTALL, "--root", r, "--dry-run"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(tree(r), before)
        p = sh([UNINSTALL, "--root", r])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, p.stderr)
        i_disable = out.index("systemctl disable --now")
        line = out[i_disable:out.index("\n", out.index("opentv-cec.service", i_disable))]
        self.assertIn("opentv-cec.timer", line)
        i_release = out.index("python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap --release")
        self.assertLess(i_disable, i_release)
        self.assertLess(i_release, out.index("systemctl daemon-reload"))
        self.assertFalse(os.path.exists(os.path.join(r, "run/opentv-cec")))
        for u in ("opentv-cec.service", "opentv-cec.timer", "opentv-cec-announce.service"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))

    def test_installed_cli_runs_outside_repo(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        if os.geteuid() == 0:
            self.skipTest("root")
        st = tmpdir(self, "otvcecst")
        q = subprocess.run(["python3", "-I", os.path.join(r, "opt/opentv/bin/opentv-pi"), "--env",
                            os.path.join(st, "none"), "cec-bootstrap", "--root", r, "--state-dir", st],
                           capture_output=True, timeout=60)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(json.loads(q.stdout)["result"], "no_adapter")


# --------------------------------------------------------------------------- keys.js (Node) et ES5
NODE = shutil.which("node")
KEYS_JS = os.path.join(PI4, "ui", "keys.js")
TV_JS = os.path.join(PI4, "ui", "tv.js")
REMOTE_JS = os.path.join(PI4, "ui", "remote.js")

KEY_CASES = [
    # (événement, action attendue)
    ({"key": "ArrowUp"}, "up"), ({"key": "Up"}, "up"), ({"key": "ArrowDown"}, "down"), ({"key": "Down"}, "down"),
    ({"key": "ArrowLeft"}, "left"), ({"key": "Left"}, "left"), ({"key": "ArrowRight"}, "right"),
    ({"key": "Right"}, "right"),
    ({"key": "Enter"}, "ok"), ({"key": "Enter", "code": "NumpadEnter"}, "ok"), ({"key": "Accept"}, "ok"),
    ({"key": "Select"}, "ok"), ({"key": "Unidentified", "code": "NumpadEnter", "keyCode": 13}, "ok"),
    ({"key": "Escape"}, "back"), ({"key": "Esc"}, "back"), ({"key": "Backspace"}, "back"),
    ({"key": "BrowserBack"}, "back"), ({"key": "GoBack"}, "back"), ({"key": "Close"}, "back"),
    ({"key": "Exit"}, "back"),
    ({"key": "BrowserHome"}, "home"), ({"key": "Home"}, "home"), ({"key": "GoHome"}, "home"),
    # anciens pilotes : key absent/Unidentified -> keyCode
    ({"keyCode": 38}, "up"), ({"key": "Unidentified", "keyCode": 40}, "down"), ({"keyCode": 37}, "left"),
    ({"keyCode": 39}, "right"), ({"keyCode": 13}, "ok"), ({"keyCode": 27}, "back"), ({"keyCode": 8}, "back"),
    ({"keyCode": 166}, "back"), ({"keyCode": 172}, "home"), ({"keyCode": 36}, "home"),
    # rien : média (aucune action courante), modificateurs, saisie IME, lettres, inconnus
    ({"key": "MediaPlayPause"}, None), ({"key": "MediaTrackNext"}, None), ({"key": "AudioVolumeUp"}, None),
    ({"key": "ArrowRight", "ctrlKey": True}, None), ({"key": "Enter", "altKey": True}, None),
    ({"key": "Enter", "metaKey": True}, None), ({"key": "Enter", "isComposing": True}, None),
    ({"key": "a", "keyCode": 65}, None), ({"key": "Unidentified"}, None), ({}, None),
    ({"key": "F5", "keyCode": 116}, None), ({"key": "ContextMenu"}, None),
]
NAV_CASES = [({"key": "BrowserBack"}, True), ({"key": "BrowserForward"}, True), ({"key": "BrowserHome"}, True),
             ({"key": "BrowserSearch"}, True), ({"key": "BrowserFavorites"}, True), ({"key": "GoHome"}, True),
             ({"keyCode": 166}, True), ({"key": "Unidentified", "keyCode": 172}, True), ({"keyCode": 167}, True),
             ({"key": "Escape"}, False), ({"key": "Home"}, False), ({"key": "Enter"}, False), ({}, False)]


@unittest.skipUnless(NODE, "node absent")
class TestKeysJs(unittest.TestCase):
    def evaluate(self, cases, fn):
        code = ("var vm=require('vm'),fs=require('fs');var ctx={window:{}};vm.createContext(ctx);"
                "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);"
                "var c=JSON.parse(process.argv[2]);"
                "process.stdout.write(JSON.stringify(c.map(function(e){return ctx.window.OpenTVKeys.%s(e);})));" % fn)
        p = subprocess.run([NODE, "-e", code, KEYS_JS, json.dumps([c for c, _ in cases])], capture_output=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_aliases(self):
        got = self.evaluate(KEY_CASES, "normalize")
        for (ev, want), g in zip(KEY_CASES, got):
            self.assertEqual(g, want, ev)
        self.assertIsNone(self.evaluate([(None, None)], "normalize")[0])

    def test_browser_navigation_keys(self):
        got = self.evaluate(NAV_CASES, "isBrowserNav")
        for (ev, want), g in zip(NAV_CASES, got):
            self.assertEqual(g, want, ev)

    def test_node_syntax(self):
        for f in (KEYS_JS, TV_JS, REMOTE_JS, os.path.join(PI4, "ui", "setup.js")):
            p = subprocess.run([NODE, "--check", f], capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, (f, p.stderr))


def find_acorn():
    p = os.environ.get("OPENTV_TEST_ACORN")
    return p if p and os.path.isfile(os.path.join(p, "package.json")) else None


class TestEs5(unittest.TestCase):
    ES6 = re.compile(r"(?<![\w$.])(let|const|class)\s|=>|`|\.\.\.[A-Za-z_$\[]|\bfor\s*\([^)]*\bof\b")

    def test_no_es2015_tokens(self):
        for f in (KEYS_JS, TV_JS, REMOTE_JS):
            with open(f, encoding="utf-8") as fh:
                code = "\n".join(x for x in fh.read().splitlines() if not x.lstrip().startswith(("//", "*", "/*")))
            m = self.ES6.search(code)
            self.assertIsNone(m, (f, m and code[max(0, m.start() - 40):m.end() + 20]))

    @unittest.skipUnless(NODE and find_acorn(), "acorn absent (OPENTV_TEST_ACORN=<chemin du module>)")
    def test_acorn_ecma5(self):
        code = ("var a=require(process.argv[1]),fs=require('fs');"
                "process.argv.slice(2).forEach(function(f){a.parse(fs.readFileSync(f,'utf8'),{ecmaVersion:5});});")
        p = subprocess.run([NODE, "-e", code, find_acorn(), KEYS_JS, TV_JS, REMOTE_JS], capture_output=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())


# --------------------------------------------------------------------------- tv.js réel dans Chromium (sans réseau)
HARNESS_DRIVER = r"""
<script>
(function () {
  var out = [], $ = function (id) { return document.getElementById(id); };
  function press(key, o) {
    o = o || {};
    var e = new KeyboardEvent("keydown", { key: key, code: o.code || "", repeat: !!o.repeat, ctrlKey: !!o.ctrl,
                                           bubbles: true, cancelable: true });
    if (o.keyCode) { Object.defineProperty(e, "keyCode", { get: function () { return o.keyCode; } }); }
    document.dispatchEvent(e);
    return e.defaultPrevented;
  }
  function mouse(button) {
    var e = new MouseEvent("mouseup", { button: button, bubbles: true, cancelable: true });
    document.dispatchEvent(e);
    return e.defaultPrevented;
  }
  function R(e) { var r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; }
  function vis(e) { return !!e && !e.hidden && e.getClientRects().length > 0; }
  function inter(a, b) { return Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 &&
                                Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5; }
  function inside(a, b) { return a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5; }
  function ringClip(fe) {   // anneau complet (box-shadow × échelle) : aucun ancêtre overflow≠visible ne le rogne
    var cs = getComputedStyle(fe), spread = 0, m = /matrix\(([-\d.e]+)/.exec(cs.transform || ""), out = [];
    (cs.boxShadow || "").replace(/rgba?\([^)]*\)/g, "").split(",").forEach(function (s) {
      var n = s.trim().split(/\s+/).map(parseFloat).filter(function (x) { return !isNaN(x); });
      if (n.length >= 4 && !/inset/.test(s)) { spread = Math.max(spread, Math.abs(n[0]) + n[2] + n[3]); }
    });
    var k = m ? parseFloat(m[1]) : 1, r = R(fe), d = spread * k;
    var ring = [r[0] - d, r[1] - d, r[2] + d, r[3] + d];
    for (var a = fe.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      var ac = getComputedStyle(a);
      if ((ac.overflowX !== "visible" || ac.overflowY !== "visible") && !inside(ring, R(a))) {
        out.push((a.id || a.className) + " ring=" + ring.map(Math.round) + " clip=" + R(a).map(Math.round));
      }
    }
    if (!inside(ring, [0, 0, 1920, 1080])) { out.push("viewport"); }
    return out;
  }
  function lines(e) {   // nombre de lignes de texte réellement rendues (1 = aucun retour à la ligne)
    var rg = document.createRange(), rs, n = 1;
    rg.selectNodeContents(e);
    rs = Array.prototype.filter.call(rg.getClientRects(), function (x) { return x.width > 0 && x.height > 0; });
    for (var i = 0; i < rs.length; i++) { for (var j = 0; j < rs.length; j++) {
      if (rs[j].top >= rs[i].bottom - 1) { n = 2; } } }
    return n;
  }
  function header() {   // haut d'écran v23 1920×1080 : chaque élément sur une ligne, aucun chevauchement, rien hors écran
    var hd = document.querySelector("header"), kids = [], ids = ["clock", "date", "gear", "glabel", "device", "net", "agents"];
    var nodes = [$("clock"), $("date"), $("gear"), $("glabel"), $("device"), $("net"), $("agents")];
    nodes.forEach(function (e, i) {
      if (!vis(e)) { return; }
      kids.push({ id: ids[i], r: R(e), lines: lines(e), clipped: e.scrollWidth > e.clientWidth + 1,
                  ellipsis: getComputedStyle(e).textOverflow, ws: getComputedStyle(e).whiteSpace });
    });
    var over = [];
    for (var i = 0; i < kids.length; i++) { for (var j = i + 1; j < kids.length; j++) {
      if (inter(kids[i].r, kids[j].r)) { over.push(kids[i].id + "/" + kids[j].id); } } }
    return { h: R(hd), kids: kids, overlaps: over };
  }
  function qrs() {   // QR nets : 1 pixel du canvas = 1 pixel CSS, modules entiers, taille fixe (data-size)
    var o = {};
    ["qr", "apqr", "rqr"].forEach(function (id) {
      var c = $(id);
      if (!vis(c) || c.getAttribute("data-qr") !== "ok") { return; }
      var r = c.getBoundingClientRect();
      o[id] = { w: c.width, h: c.height, cssw: r.width, cssh: r.height, size: Number(c.getAttribute("data-size")),
                cell: Number(c.getAttribute("data-cell")), quiet: Number(c.getAttribute("data-quiet")),
                rendering: getComputedStyle(c).imageRendering };
    });
    return o;
  }
  function layout() {   // mesure 1920×1080 : Réglages en haut, focus visible, QR jamais sur une carte/le panneau
    var VP = [0, 0, 1920, 1080], gear = $("gear"), box = $("remotebox"), main = $("main");
    var fe = document.querySelector(".card.focused") || (gear.className.indexOf("focused") >= 0 ? gear : null);
    var over = [], cs = fe ? getComputedStyle(fe) : null, rows = R($("rows"));
    if (vis(box)) {
      Array.prototype.forEach.call(document.querySelectorAll(".card"), function (c) {
        var r = R(c), m = R(main);   // partie réellement peinte : #main coupe le débordement d'une rangée longue
        var p = [Math.max(r[0], m[0]), Math.max(r[1], m[1]), Math.min(r[2], m[2]), Math.min(r[3], m[3])];
        if (inter(r, m) && inter(p, VP) && inter(p, R(box))) { over.push(c.textContent.slice(0, 30)); }
      });
    }
    var fr = fe ? R(fe) : null, pan = $("panel"), ban = $("banner"), hits = [];
    if (vis(pan)) {   // ce que la télécommande valide est réellement visible : rien ne recouvre titre ni boutons
      [$("ptitle")].concat(Array.prototype.slice.call($("pbtns").children)).forEach(function (e) {
        var r = R(e), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
        if (!h || !pan.contains(h)) { hits.push((e.getAttribute("data-label") || e.textContent).slice(0, 30)); }
      });
    }
    return { gear: R(gear), gear_visible: vis(gear) && inside(R(gear), VP) && R(gear)[3] <= 150,
             ring_clip: fe ? ringClip(fe) : [], panel_hidden_by: hits,
             banner_panel_overlap: vis(pan) && vis(ban) && inter(R(pan), R(ban)), banner_visible: vis(ban),
             focus_rect: fr, focus_in_view: !!fr && inside(fr, VP) && (!vis(box) || !inter(fr, R(box))) &&
               (fe === gear || inside([fr[0], fr[1], fr[2], fr[3]], [R(main)[0], rows[1] - 40, R(main)[2], rows[3] + 40])),
             focus_ring: !!cs && (cs.boxShadow !== "none" || (cs.outlineStyle !== "none" && cs.outlineWidth !== "0px")),
             card_box_overlaps: over, box: vis(box) ? R(box) : null, main: R(main),
             panel_box_overlap: vis($("panel")) && vis(box) && inter(R($("panel")), R(box)),
             panel_in_view: !vis($("panel")) || inside(R($("panel")), VP) };
  }
  function geom() {   // 1920×1080 : bandeau d'assistant, héros, écran de configuration (QR) — rectangles réels
    var o = { vp: [0, 0, 1920, 1080], ids: {} };
    ["banner", "hero", "main", "htitle", "hkicker", "hmeta", "hchips", "remotebox", "rqr", "setup", "qr",
     "apqr", "qrfig", "apfig", "steps", "setupmsg", "phoneonly", "url", "panel"].forEach(function (id) {
      var e = $(id);
      o.ids[id] = vis(e) ? R(e) : null;
    });
    var sh = document.querySelector("#setup h1"), sk = document.querySelector("#setup .kicker");
    o.ids.setup_h1 = vis(sh) ? R(sh) : null;
    o.ids.setup_kicker = vis(sk) ? R(sk) : null;
    // ce qui est réellement peint au premier plan sur 5 points de chaque cible (centre + coins rentrés de 12 px)
    o.top = {};
    ["qr", "apqr", "htitle", "banner", "phoneonly", "setupmsg"].forEach(function (id) {
      var e = $(id);
      if (!vis(e)) { return; }
      var r = R(e), pts = [[(r[0] + r[2]) / 2, (r[1] + r[3]) / 2], [r[0] + 12, r[1] + 12], [r[2] - 12, r[1] + 12],
                           [r[0] + 12, r[3] - 12], [r[2] - 12, r[3] - 12]];
      o.top[id] = pts.map(function (p) {
        var h = document.elementFromPoint(p[0], p[1]);
        return !!h && (h === e || e.contains(h));
      });
    });
    var b = $("banner");
    if (vis(b)) {
      var cs = getComputedStyle(b), lh = parseFloat(cs.lineHeight);
      o.banner = { text: b.textContent, clientH: b.clientHeight, lineH: lh, clamp: cs.webkitLineClamp,
                   scrollH: b.scrollHeight, inHtext: b.parentNode.className.indexOf("htext") >= 0 };
    }
    var t = $("htitle");
    o.htitle = { clientH: t.clientHeight, scrollH: t.scrollHeight, lineH: parseFloat(getComputedStyle(t).lineHeight) };
    o.heroClass = $("hero").className;
    // texte de configuration rogné ? (débordement de son conteneur ou hors écran)
    o.setupClip = [];
    Array.prototype.forEach.call(document.querySelectorAll("#setup .steps > *, #setup figure"), function (e) {
      if (!vis(e)) { return; }
      var r = R(e), s = R($("setup"));
      var clips = getComputedStyle(e).overflowY !== "visible" && e.scrollHeight > e.clientHeight + 1;
      if (r[3] > s[3] + 0.5 || r[3] > 1080.5 || r[2] > 1920.5 || clips) {
        o.setupClip.push((e.id || e.tagName) + " r=" + r.map(Math.round) + " setup=" + s.map(Math.round));
      }
    });
    return o;
  }
  function v23() {   // identité OpenTV v23 : photo plein écran, horloge + date, cercle Réglages en haut à droite, verre
    var o = {}, bg = $("bg"), g = $("gear"), gl = $("glabel"), cs = getComputedStyle(g), fc = document.querySelector(".card.focused");
    o.bg = vis(bg) ? R(bg) : null;
    o.bgImage = bg ? getComputedStyle(bg).backgroundImage.slice(0, 40) : "";
    o.time = $("clock").textContent;
    o.date = vis($("date")) ? $("date").textContent : null;
    o.dateTransform = $("date") ? getComputedStyle($("date")).textTransform : null;
    o.gear = { r: R(g), radius: cs.borderTopLeftRadius, w: g.offsetWidth, h: g.offsetHeight, bgc: cs.backgroundColor,
               transform: cs.transform, focused: g.className.indexOf("focused") >= 0 };
    o.glabel = vis(gl) ? { text: gl.textContent, r: R(gl) } : null;
    if (fc) {
      var c = getComputedStyle(fc);
      o.card = { border: c.borderTopColor, bw: parseFloat(c.borderTopWidth), shadow: c.boxShadow, transform: c.transform,
                 radius: parseFloat(c.borderTopLeftRadius) };
    }
    o.kicker = $("hkicker").textContent;
    var pan = $("panel");
    if (vis(pan)) {
      o.panel = { r: R(pan), cls: pan.className, btns: Array.prototype.map.call($("pbtns").children, function (b) {
        var s = getComputedStyle(b);
        return { r: R(b), bgc: s.backgroundColor, focused: b.className.indexOf("focused") >= 0 }; }) };
    }
    var outside = [];   // tout ce qui est visible tient dans 1920×1080 (rien de rogné par l'écran)
    ["clock", "date", "gear", "glabel", "device", "net", "agents", "hint", "remotebox", "panel", "hero", "htitle", "hmeta",
     "hdesc", "toast", "help"].forEach(function (id) {
      var e = $(id);
      if (vis(e) && !inside(R(e), [0, 0, 1920, 1080])) { outside.push(id + " " + R(e).map(Math.round)); }
    });
    o.outside = outside;
    return o;
  }
  function snap(label, prevented) {
    var f = document.querySelector(".card.focused .t");
    out.push({ s: label, focus: f ? f.textContent : null, panel: !$("panel").hidden, prevented: prevented,
               settings: $("gear").className.indexOf("focused") >= 0, ptitle: $("panel").hidden ? null :
                 $("ptitle").textContent,
               btns: $("panel").hidden ? [] : Array.prototype.map.call($("pbtns").children, function (b) {
                 return b.getAttribute("data-label") || b.textContent; }),
               btn: $("panel").hidden ? null : document.querySelector("#pbtns .focused") &&
                    (document.querySelector("#pbtns .focused").getAttribute("data-label") ||
                     document.querySelector("#pbtns .focused").textContent),
               hint: $("help").innerText, layout: layout(), geom: geom(), v23: v23() });
  }
  setTimeout(function () {
    window.__STEPS__.forEach(function (st) {
      var p = st[0] === "mouse" ? mouse(st[1]) : press(st[0], st[1]);
      snap(st[0] + (st[1] && st[1].tag ? ":" + st[1].tag : ""), p);
    });
    var r = document.createElement("pre");
    r.id = "result";
    var f = document.querySelector(".card.focused .t");
    r.textContent = JSON.stringify({ steps: out, calls: window.__CALLS__, setup: !$("setup").hidden,
                                     home: !$("home").hidden, focus: f ? f.textContent : null,
                                     panel: !$("panel").hidden, remotebox: !$("remotebox").hidden,
                                     settings: $("gear").className.indexOf("focused") >= 0, layout: layout(),
                                     hint: $("help").innerText, header: header(), qrs: qrs(), geom: geom(), v23: v23(),
                                     banner: $("banner").hidden ? null : $("banner").textContent,
                                     rqr: $("rqr").getAttribute("data-qr"),
                                     toast: $("toast").hidden ? null : $("toast").textContent,
                                     toastBig: $("toast").className.indexOf("big") >= 0,
                                     text: document.body.innerText,
                                     extra: window.__EXTRA__ ? window.__EXTRA__() : null });
    document.body.appendChild(r);
  }, window.__DELAY__ || 1500);
})();
</script>
"""


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestTvKeysInChromium(unittest.TestCase):
    FY = {"note": "", "marks": {}, "rows": [
        {"id": "r1", "title": "Le choix de ce soir", "items": [{"id": "a", "title": "Alpha"},
                                                              {"id": "b", "title": "Bravo"},
                                                              {"id": "c", "title": "Charlie"}]},
        {"id": "r2", "title": "Autres", "items": [{"id": "d", "title": "Delta"}, {"id": "e", "title": "Echo"}]},
        {"id": "r3", "title": "Encore", "items": [{"id": "f", "title": "Foxtrot"}, {"id": "g", "title": "Golf"},
                                                  {"id": "h", "title": "Hotel"}, {"id": "i", "title": "India"}]}]}

    def render(self, state, steps, remote=None, delay=1500, fy=None, inject=""):
        d = tmpdir(self, "otvkeys")
        with open(os.path.join(PI4, "ui", "tv.html"), encoding="utf-8") as f:
            page = f.read().replace("{{TV_KEY}}", "k")
        fy = self.FY if fy is None else fy
        # mesures déterministes : aucune transition pendant le harnais (le CSS produit garde ses transitions courtes)
        page = page.replace("</head>", "<style>*,*::before,*::after{transition:none!important}</style></head>")
        stub = ("<script>window.__DELAY__=%d;window.__CALLS__=[];window.__STEPS__=%s;var S=%s,F=%s,R=%s;"
                "window.fetch=function(p,o){var m=(o&&o.method)||'GET';__CALLS__.push(m+' '+p);"
                "var d=p.indexOf('/api/tv/state')===0?(Array.isArray(S)?(S.length>1?S.shift():S[0]):S):"
                "(p.indexOf('/api/tv/foryou')===0?F:"
                "(p.indexOf('/api/tv/remote?')===0?(R.length?R.shift():{seq:0,events:[],idle:true}):"
                "{ok:true,state:{opinion:'like'},message:'ok'}));"
                "return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(d);}});};"
                "</script>" % (delay, json.dumps(steps), json.dumps(state), json.dumps(fy), json.dumps(remote or [])))
        stub += inject
        page = page.replace('<link rel="stylesheet" href="/static/tv.css">',
                            '<link rel="stylesheet" href="file://%s">' % os.path.join(PI4, "ui", "tv.css"))
        qr = os.path.join(os.path.dirname(os.path.dirname(PI4)), "native", "app", "qrcode.js")
        page = page.replace('<script src="/static/qrcode.js"></script>', stub + '<script src="file://%s"></script>' % qr)
        page = page.replace('<script src="/static/keys.js"></script>', '<script src="file://%s"></script>' % KEYS_JS)
        page = page.replace('<script src="/static/tv.js"></script>',
                            '<script src="file://%s"></script>' % TV_JS + HARNESS_DRIVER)
        path = os.path.join(d, "harness.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        prof = tmpdir(self, "otvchr")
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + prof,
                            "--allow-file-access-from-files", "--virtual-time-budget=%d" % (delay + 3500), "--window-size=1920,1080",
                            "--dump-dom", "file://" + path], capture_output=True, timeout=120)
        m = re.search(r'<pre id="result">(.*?)</pre>', p.stdout.decode("utf-8", "replace"), re.S)
        self.assertIsNotNone(m, p.stdout.decode("utf-8", "replace")[-2000:] + p.stderr.decode()[-2000:])
        return json.loads(html.unescape(m.group(1)))

    STATE = {"device": "Salon", "network": {"mode": "ethernet"}, "agents": [], "message": None,
             "setup": {"active": False}}

    # heure contrôlée (tests seulement) : getHours() suit window.__HOUR__, que le harnais change à t = __FLIP__ ms
    HOUR_STUB = ("<script>window.__HOUR__=%d;Date.prototype.getHours=function(){return window.__HOUR__;};"
                 "setTimeout(function(){window.__CARD__=document.querySelector('.card.focused');"
                 "window.__TITLE__=document.getElementById('htitle').textContent;"
                 "window.__K0__=document.getElementById('hkicker').textContent;window.__HOUR__=%d;},%d);"
                 "window.__EXTRA__=function(){return {k0:window.__K0__,sameCard:!!window.__CARD__&&"
                 "window.__CARD__===document.querySelector('.card.focused'),"
                 "sameTitle:window.__TITLE__===document.getElementById('htitle').textContent};};</script>")

    def test_greeting_follows_clock_bucket_without_rerender(self):
        """Bonjour → Bon après-midi → Bonsoir au passage de 12 h / 18 h, par le tic de l'horloge (10 s), sans
        recréer le héros ni les cartes (même nœud de carte au focus, même titre) ; jamais d'autre texte modifié."""
        for before, after, k0, k1 in ((11, 12, "Bonjour", "Bon après-midi"), (17, 18, "Bon après-midi", "Bonsoir"),
                                      (23, 0, "Bonsoir", "Bonjour"), (9, 10, "Bonjour", "Bonjour")):
            with self.subTest(before=before, after=after):
                res = self.render(self.STATE, [], delay=12500, inject=self.HOUR_STUB % (before, after, 4000))
                self.assertEqual(res["extra"]["k0"], k0 + " · Le choix de ce soir")
                self.assertEqual(res["steps"], [])
                self.assertEqual(res["focus"], "Alpha")
                self.assertEqual(res["v23"]["kicker"], k1 + " · Le choix de ce soir")
                self.assertEqual((res["extra"]["sameCard"], res["extra"]["sameTitle"]), (True, True))
                self.assertEqual(res["v23"]["time"][:2], "%02d" % after)

    def test_home_navigation_with_remote_aliases(self):
        steps = [["ArrowRight"], ["Right"], ["Unidentified", {"keyCode": 37, "tag": "kc37"}], ["ArrowDown"],
                 ["BrowserHome"], ["Enter"], ["Enter", {"repeat": True, "tag": "repeat"}], ["ArrowRight"],
                 ["Close"], ["Enter", {"code": "NumpadEnter", "tag": "numpad"}], ["GoBack"], ["Select"],
                 ["Escape"], ["BrowserBack"], ["MediaPlayPause"], ["ArrowRight", {"ctrl": True, "tag": "ctrl"}],
                 ["mouse", 3], ["Accept"], ["ArrowRight"], ["Enter", {"tag": "act"}]]
        res = self.render(self.STATE, steps)
        s = res["steps"]
        self.assertTrue(res["home"])
        got = [(x["focus"], x["panel"]) for x in s]
        self.assertEqual(got[:5], [("Bravo", False), ("Charlie", False), ("Bravo", False), ("Delta", False),
                                   ("Alpha", False)])
        self.assertTrue(s[4]["prevented"])                                       # BrowserHome : kiosque reste sur /tv
        self.assertEqual([x["panel"] for x in s[5:13]], [True, True, True, False, True, False, True, False])
        self.assertEqual(s[7]["btn"], "Pas pour moi")                            # → dans le panneau
        self.assertTrue(s[8]["prevented"])                                       # Close (CEC Exit) ferme
        self.assertTrue(s[13]["prevented"] and not s[13]["panel"])               # BrowserBack à l'accueil : bloqué
        self.assertFalse(s[14]["prevented"])                                     # média : non capté
        self.assertEqual((s[15]["focus"], s[15]["prevented"]), ("Alpha", False))  # Ctrl+→ : ignoré
        self.assertTrue(s[16]["prevented"])                                      # bouton souris « précédent »
        posts = [c for c in res["calls"] if c.startswith("POST")]
        self.assertEqual(posts, ["POST /api/tv/feedback"])                       # OK répété : aucune rafale

    REMOTE1 = {"enabled": True, "url": "http://opentv-salon.local:8082/r?p=" + "x" * 43, "expires_in": 300, "phones": 1}

    def assertLayout(self, lay, where):
        self.assertTrue(lay["gear_visible"], (where, lay))                       # Réglages toujours en haut, visibles
        self.assertEqual(lay["card_box_overlaps"], [], (where, lay))             # QR : jamais sur une carte
        self.assertFalse(lay["panel_box_overlap"], (where, lay))                 # ni sur le panneau
        self.assertTrue(lay["panel_in_view"], (where, lay))
        self.assertEqual(lay["ring_clip"], [], (where, lay))                    # anneau de focus jamais rogné
        if lay["box"]:
            self.assertLessEqual(lay["main"][2] + 40, lay["box"][0], (where, lay))   # colonne du QR séparée
        self.assertEqual(lay["panel_hidden_by"], [], (where, lay))              # titre/boutons du panneau visibles
        self.assertFalse(lay["banner_panel_overlap"], (where, lay))             # message d'assistant jamais dessus
        if lay["focus_rect"] is not None:
            self.assertTrue(lay["focus_in_view"], (where, lay))                  # focus entièrement visible
            self.assertTrue(lay["focus_ring"], (where, lay))                     # anneau de focus évident

    def test_up_reaches_settings_from_any_row_in_one_press(self):
        st = dict(self.STATE, remote=self.REMOTE1)
        steps = [["ArrowUp", {"tag": "row0"}], ["ArrowDown", {"tag": "ret0"}],
                 ["ArrowDown"], ["ArrowRight"], ["ArrowUp", {"tag": "row1"}], ["ArrowDown", {"tag": "ret1"}],
                 ["ArrowDown"], ["ArrowRight"], ["ArrowRight"], ["ArrowRight"], ["ArrowRight", {"tag": "end"}],
                 ["ArrowDown", {"tag": "last"}], ["ArrowUp", {"tag": "row2"}], ["Enter", {"tag": "open"}],
                 ["Escape", {"tag": "close"}], ["Escape", {"tag": "leave"}], ["Escape", {"tag": "start"}]]
        res = self.render(st, steps)
        s = {x["s"]: x for x in res["steps"]}
        for tag in ("ArrowUp:row0", "ArrowUp:row1", "ArrowUp:row2"):             # un seul ↑ depuis chaque rangée
            self.assertTrue(s[tag]["settings"] and s[tag]["focus"] is None and not s[tag]["panel"], s[tag])
            self.assertTrue(s[tag]["prevented"])
        self.assertEqual(s["ArrowDown:ret0"]["focus"], "Alpha")                  # ↓ : retour à la même carte
        self.assertEqual(s["ArrowDown:ret1"]["focus"], "Echo")
        self.assertEqual(s["ArrowRight:end"]["focus"], "India")                  # rangée longue : dernière carte
        self.assertEqual(s["ArrowDown:last"]["focus"], "India")                  # dernière rangée : reste en place
        self.assertEqual((s["Enter:open"]["ptitle"], s["Enter:open"]["btn"]), ("Réglages", "Reconfigurer"))
        self.assertFalse(s["Escape:close"]["panel"])                             # Retour ferme…
        self.assertTrue(s["Escape:close"]["settings"])                           # … et le focus reste sur Réglages
        self.assertEqual(s["Escape:leave"]["focus"], "India")                    # Retour en haut : contenu retrouvé
        self.assertEqual(s["Escape:start"]["focus"], "Echo")                     # Retour : une rangée plus haut
        for x in res["steps"]:
            self.assertLayout(x["layout"], x["s"])
        self.assertIn("↑ Réglages", res["hint"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])

    FY4 = {"note": "", "marks": {}, "rows": FY["rows"] + [
        {"id": "r4", "title": "Pour finir", "items": [{"id": "j", "title": "Juliett"}, {"id": "k", "title": "Kilo"},
                                                      {"id": "l", "title": "Lima"}]}]}

    def test_back_climbs_one_row_at_a_time_keeping_each_column(self):
        """Choix produit : sur l'accueil sans panneau, Retour remonte d'UNE rangée (colonne mémorisée par rangée) ;
        rangée 0 : reste en rangée 0 (début) ; ↓ redescend ; ↑ mène toujours aux Réglages en un geste."""
        st = dict(self.STATE, remote=self.REMOTE1)
        steps = [["ArrowRight"], ["ArrowDown"], ["ArrowRight", {"tag": "r1"}], ["ArrowDown"], ["ArrowRight"],
                 ["ArrowRight", {"tag": "r2"}], ["ArrowDown"], ["ArrowRight", {"tag": "r3"}],
                 ["Escape", {"tag": "back3"}], ["Escape", {"tag": "back2"}], ["Escape", {"tag": "back1"}],
                 ["Escape", {"tag": "back0"}], ["GoBack", {"tag": "again0"}],
                 ["ArrowDown", {"tag": "down1"}], ["ArrowDown", {"tag": "down2"}], ["ArrowDown", {"tag": "down3"}],
                 ["ArrowUp", {"tag": "settings"}], ["ArrowDown", {"tag": "ret"}],
                 ["Escape", {"tag": "b"}], ["ArrowUp", {"tag": "settings2"}], ["Escape", {"tag": "ret2"}]]
        res = self.render(st, steps, fy=self.FY4)
        s = {x["s"]: x for x in res["steps"]}
        self.assertEqual([s[t]["focus"] for t in ("ArrowRight:r1", "ArrowRight:r2", "ArrowRight:r3")],
                         ["Echo", "Hotel", "Kilo"])
        # rangée 3 -> Retour -> rangée 2 -> Retour -> rangée 1 -> Retour -> rangée 0 ; jamais de saut à la rangée 0
        self.assertEqual([s[t]["focus"] for t in ("Escape:back3", "Escape:back2", "Escape:back1")],
                         ["Hotel", "Echo", "Bravo"])
        self.assertEqual(s["Escape:back0"]["focus"], "Alpha")                    # rangée 0 : reste en rangée 0
        self.assertEqual(s["GoBack:again0"]["focus"], "Alpha")
        for t in ("Escape:back3", "Escape:back2", "Escape:back1", "Escape:back0", "GoBack:again0"):
            self.assertTrue(s[t]["prevented"] and not s[t]["panel"] and not s[t]["settings"], s[t])
        self.assertEqual([s[t]["focus"] for t in ("ArrowDown:down1", "ArrowDown:down2", "ArrowDown:down3")],
                         ["Echo", "Hotel", "Kilo"])                              # ↓ : colonne de chaque rangée gardée
        self.assertTrue(s["ArrowUp:settings"]["settings"] and s["ArrowUp:settings2"]["settings"])  # ↑ : un geste
        self.assertEqual(s["ArrowDown:ret"]["focus"], "Kilo")
        self.assertEqual(s["Escape:b"]["focus"], "Hotel")
        self.assertEqual(s["Escape:ret2"]["focus"], "Hotel")                     # Retour depuis Réglages : même carte
        for x in res["steps"]:
            self.assertLayout(x["layout"], x["s"])
        rows_hint = s["Escape:back2"]["hint"]                                    # aide explicite, dans les rangées
        for want in ("↓", "Retour", "Rangée précédente", "↑ Réglages", "OK"):
            self.assertIn(want, rows_hint)
        self.assertIn("Rangée suivante", rows_hint)
        self.assertNotIn("Rangée précédente", s["ArrowUp:settings"]["hint"])    # en haut : aide propre aux Réglages
        self.assertIn("OK", s["ArrowUp:settings"]["hint"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])

    MSG = {"from": "muse", "text": "Ce soir, je te propose un film calme : bonne soirée ! " * 2}

    def test_assistant_message_never_covers_any_panel(self):
        st = dict(self.STATE, remote=self.REMOTE1, agents=["muse", "hermit", "hermes"], message=self.MSG)
        steps = [["ArrowRight", {"tag": "home"}], ["ArrowUp", {"tag": "top"}], ["Enter", {"tag": "settings"}],
                 ["Enter", {"tag": "confirm"}], ["Escape", {"tag": "back"}], ["ArrowRight"], ["Enter", {"tag": "forget"}],
                 ["Escape"], ["Escape", {"tag": "closed"}], ["ArrowDown"], ["Enter", {"tag": "title"}],
                 ["Escape", {"tag": "end"}]]
        res = self.render(st, steps)
        s = {x["s"]: x for x in res["steps"]}
        self.assertTrue(s["ArrowRight:home"]["layout"]["banner_visible"])      # message lisible à l'accueil
        for t in ("Enter:settings", "Enter:confirm", "Enter:forget", "Enter:title"):
            lay = s[t]["layout"]
            self.assertTrue(s[t]["panel"], t)
            self.assertFalse(lay["banner_panel_overlap"], (t, lay))
            self.assertEqual(lay["panel_hidden_by"], [], (t, lay))
        self.assertEqual((s["Enter:confirm"]["ptitle"], s["Enter:confirm"]["btn"]),
                         ("Afficher un nouveau QR de configuration ?", "Non"))   # « Non » par défaut, visible
        self.assertEqual(s["Enter:forget"]["btn"], "Non")
        self.assertTrue(s["Escape:closed"]["layout"]["banner_visible"])        # panneau fermé : message revient
        self.assertIn("Message de Muse", res["banner"])
        for x in res["steps"]:
            self.assertLayout(x["layout"], x["s"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])

    def test_header_long_device_name_and_three_assistants_stay_on_one_line(self):
        st = dict(self.STATE, remote=self.REMOTE1, device="Salon du sous-sol chez grand-maman Gertrude, télé principale",
                  agents=["muse", "hermit", "hermes"], network={"mode": "hotspot"})
        res = self.render(st, [["ArrowUp"]])
        hd = res["header"]
        self.assertEqual(hd["overlaps"], [], hd)
        self.assertLessEqual(hd["h"][3] - hd["h"][1], 200.5, hd)               # haut v23 : horloge + date, cercles
        kids = {k["id"]: k for k in hd["kids"]}
        self.assertEqual(set(kids), {"clock", "date", "gear", "glabel", "device", "net", "agents"})
        for k in hd["kids"]:
            self.assertEqual(k["lines"], 1, k)                                   # jamais de retour à la ligne
            self.assertTrue(k["r"][0] >= 0 and k["r"][2] <= 1920 and k["r"][1] >= 0 and k["r"][3] <= hd["h"][3] + 1, k)
        for k in ("clock", "date", "gear", "glabel", "net"):
            self.assertFalse(kids[k]["clipped"], kids[k])                        # Réglages, heure, date : entiers
        for k in ("device", "agents"):
            self.assertTrue(kids[k]["clipped"], kids[k])                         # textes longs : ellipsés
            self.assertEqual((kids[k]["ellipsis"], kids[k]["ws"]), ("ellipsis", "nowrap"), kids[k])
        self.assertTrue(res["settings"])
        self.assertTrue(res["layout"]["gear_visible"])
        self.assertEqual(res["layout"]["ring_clip"], [])
        res = self.render(dict(self.STATE, agents=["muse"]), [])                 # cas court : rien d'ellipsé
        kids = {k["id"]: k for k in res["header"]["kids"]}
        self.assertFalse(kids["agents"]["clipped"] or kids["device"]["clipped"], kids)

    FYLONG = {"note": "", "marks": {}, "rows": [
        {"id": "l", "title": "Beaucoup", "items": [{"id": "n%d" % i, "title": "Titre %d" % i} for i in range(14)]},
        {"id": "w", "title": "Peu", "items": [{"id": "w1", "title": "Large un"}, {"id": "w2", "title": "Large deux"}]},
        {"id": "l2", "title": "Encore", "items": [{"id": "m%d" % i, "title": "Autre %d" % i} for i in range(9)]}]}

    def test_focus_ring_is_never_clipped_on_standard_and_wide_cards(self):
        st = dict(self.STATE, remote=self.REMOTE1)
        steps = [["ArrowLeft", {"tag": "first"}]] + [["ArrowRight"]] * 6 + [["ArrowRight", {"tag": "mid"}]] + \
                [["ArrowRight"]] * 5 + [["ArrowRight", {"tag": "last"}], ["ArrowDown", {"tag": "wide1"}],
                                        ["ArrowRight", {"tag": "wide2"}], ["ArrowDown", {"tag": "std"}],
                                        ["ArrowUp", {"tag": "gear"}]]
        res = self.render(st, steps, fy=self.FYLONG)
        s = {x["s"]: x for x in res["steps"]}
        self.assertEqual([s[t]["focus"] for t in ("ArrowLeft:first", "ArrowRight:mid", "ArrowRight:last",
                                                  "ArrowDown:wide1", "ArrowRight:wide2", "ArrowDown:std")],
                         ["Titre 0", "Titre 7", "Titre 13", "Large un", "Large deux", "Autre 0"])
        for x in res["steps"]:
            self.assertEqual(x["layout"]["ring_clip"], [], (x["s"], x["layout"]))
            self.assertLayout(x["layout"], x["s"])

    def test_qr_canvases_are_crisp_and_fixed_size(self):
        st = dict(self.STATE, remote=self.REMOTE1)
        long_remote = dict(self.REMOTE1, url="http://opentv-salon-du-sous-sol.local:8082/r?p=" + "y" * 64)
        hs = dict(self.STATE, setup={"active": True, "url": "http://opentv-salon.local:8081/setup?t=" + "x" * 43,
                                     "message": "", "expires_in": 600,
                                     "hotspot": {"ssid": "OpenTV-AB12", "state": "active",
                                                 "wifi_qr": "WIFI:T:WPA;S:OpenTV-AB12;P:abcdefghijkm;;"}})
        seen = set()
        for state in (st, dict(st, remote=long_remote), hs):
            for cid, q in self.render(state, [])["qrs"].items():
                seen.add(cid)
                self.assertEqual((q["w"], q["h"]), (q["size"], q["size"]), (cid, q))   # taille fixe, cohérente
                self.assertEqual((q["cssw"], q["cssh"]), (q["w"], q["h"]), (cid, q))   # aucun rééchantillonnage
                self.assertEqual(q["cell"], int(q["cell"]), (cid, q))
                self.assertGreaterEqual(q["cell"], 3, (cid, q))
                self.assertGreaterEqual(q["quiet"], 4 * q["cell"], (cid, q))           # zone calme ≥ 4 modules
                self.assertEqual(q["rendering"], "pixelated", (cid, q))
        self.assertEqual(seen, {"qr", "apqr", "rqr"})

    def test_settings_panel_six_keys_and_explicit_confirmation(self):
        st = dict(self.STATE, remote=self.REMOTE1)
        steps = [["ArrowUp"], ["Enter", {"tag": "open"}], ["ArrowRight", {"tag": "r1"}], ["ArrowRight", {"tag": "r2"}],
                 ["ArrowRight", {"tag": "r3"}], ["ArrowLeft", {"tag": "l1"}], ["ArrowDown", {"tag": "down"}],
                 ["ArrowUp", {"tag": "up"}], ["Enter", {"tag": "ask"}], ["Enter", {"tag": "no"}],
                 ["Enter", {"tag": "ask2"}], ["ArrowLeft", {"tag": "yes"}], ["Escape", {"tag": "back"}],
                 ["Escape", {"tag": "closed"}], ["Enter", {"tag": "reopen"}], ["Enter", {"tag": "setup"}],
                 ["ArrowLeft"], ["Enter", {"tag": "confirm"}]]
        res = self.render(st, steps)
        s = {x["s"]: x for x in res["steps"]}
        self.assertEqual(s["Enter:open"]["btns"], ["Reconfigurer", "Oublier les téléphones", "Fermer"])
        self.assertEqual([s[t]["btn"] for t in ("ArrowRight:r1", "ArrowRight:r2", "ArrowRight:r3", "ArrowLeft:l1",
                                                "ArrowDown:down", "ArrowUp:up")],
                         ["Oublier les téléphones", "Fermer", "Fermer", "Oublier les téléphones",
                          "Fermer", "Oublier les téléphones"])                   # feuille v23 : ↑ ↓ aussi
        self.assertEqual(s["Enter:ask"]["ptitle"], "Oublier tous les téléphones ?")
        self.assertEqual(s["Enter:ask"]["btn"], "Non")                           # choix sûr par défaut
        self.assertEqual((s["Enter:no"]["ptitle"], s["Enter:no"]["btn"]), ("Réglages", "Oublier les téléphones"))
        self.assertEqual(s["ArrowLeft:yes"]["btn"], "Oui, oublier")
        self.assertEqual(s["Escape:back"]["ptitle"], "Réglages")                 # Retour : un niveau, rien révoqué
        self.assertFalse(s["Escape:closed"]["panel"])
        self.assertTrue(s["Escape:closed"]["settings"])
        self.assertEqual((s["Enter:setup"]["ptitle"], s["Enter:setup"]["btn"]),
                         ("Afficher un nouveau QR de configuration ?", "Non"))
        self.assertFalse(s["Enter:confirm"]["panel"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], ["POST /api/tv/setup"])
        for x in res["steps"]:
            self.assertLayout(x["layout"], x["s"])
        res = self.render(st, [["ArrowUp"], ["Enter"], ["ArrowRight"], ["Enter"], ["ArrowLeft"], ["Enter"]])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], ["POST /api/tv/remote/revoke"])

    def test_settings_without_phones_and_from_title_panel(self):
        st = dict(self.STATE, remote=dict(self.REMOTE1, phones=0))
        steps = [["ArrowDown"], ["Enter", {"tag": "title"}], ["ArrowUp", {"tag": "up"}], ["Enter", {"tag": "open"}],
                 ["ArrowRight"], ["Enter", {"tag": "close"}]]
        res = self.render(st, steps)
        s = {x["s"]: x for x in res["steps"]}
        self.assertEqual(s["Enter:title"]["ptitle"], "Delta")
        self.assertTrue(s["ArrowUp:up"]["settings"] and not s["ArrowUp:up"]["panel"])   # ↑ même depuis un titre
        self.assertEqual(s["Enter:open"]["btns"], ["Reconfigurer", "Fermer"])    # aucun téléphone : rien à oublier
        self.assertFalse(s["Enter:close"]["panel"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])

    def test_empty_catalogue_is_intentional_and_settings_reachable(self):
        st = dict(self.STATE, remote=self.REMOTE1)
        empty = {"note": "", "marks": {}, "rows": []}
        res = self.render(st, [["ArrowDown", {"tag": "down"}], ["ArrowUp"], ["Enter", {"tag": "open"}],
                               ["Escape", {"tag": "close"}]], fy=empty)
        s = {x["s"]: x for x in res["steps"]}
        self.assertTrue(s["ArrowDown:down"]["settings"])                          # seul choix : Réglages au focus
        self.assertEqual(s["Enter:open"]["ptitle"], "Réglages")
        self.assertTrue(s["Escape:close"]["settings"] and not s["Escape:close"]["panel"])
        self.assertIn("Pour toi", res["text"])
        self.assertIn("se prépare", res["text"])                                 # état vide écrit, pas des blocs vides
        self.assertEqual(res["layout"]["card_box_overlaps"], [])
        self.assertTrue(res["layout"]["gear_visible"])
        self.assertTrue(res["remotebox"])

    DAYS = ("dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi")

    def assertV23Frame(self, v, where):
        self.assertEqual(v["bg"], [0, 0, 1920, 1080], where)                     # photo plein écran (v23)
        self.assertIn("data:image/jpeg", v["bgImage"], where)                    # embarquée : aucune ressource externe
        self.assertRegex(v["time"], r"^\d\d:\d\d$", where)                       # horloge v23 « 21:05 »
        self.assertIsNotNone(v["date"], where)
        self.assertIn(v["date"].split(" ")[0], self.DAYS, where)                # « mardi 27 octobre »
        self.assertEqual(v["dateTransform"], "capitalize", where)
        g = v["gear"]
        self.assertEqual((g["w"], g["radius"]), (g["h"], "50%"), (where, g))     # Réglages : un vrai cercle
        self.assertGreaterEqual(g["w"], 64, (where, g))
        self.assertGreaterEqual(g["r"][2], 1920 - 140, (where, g))               # en haut à droite
        self.assertLessEqual(g["r"][1], 120, (where, g))
        self.assertIsNotNone(v["glabel"], where)                                 # libellé lisible à côté du cercle
        self.assertEqual(v["glabel"]["text"], "Réglages", where)
        self.assertLessEqual(v["glabel"]["r"][2], g["r"][0] + 0.5, where)
        self.assertEqual(v["outside"], [], where)                                # rien ne déborde de 1920×1080

    @staticmethod
    def _scale(t):
        m = re.match(r"matrix\(([-\d.e]+)", t or "")
        return float(m.group(1)) if m else 1.0

    def test_v23_identity_frame_focus_and_settings_sheet(self):
        """Identité OpenTV v23 portée sur le Pi : photo plein écran, horloge + date en haut à gauche, salutation, cercle
        Réglages en haut à droite (↑ depuis la 1re carte), cartes en verre avec anneau blanc + agrandissement, Réglages en
        feuille latérale verticale (lignes blanches au focus) sans jamais toucher la colonne du QR."""
        st = dict(self.STATE, remote=self.REMOTE1)
        steps = [["ArrowRight", {"tag": "card"}], ["ArrowLeft", {"tag": "first"}], ["ArrowUp", {"tag": "top"}],
                 ["Enter", {"tag": "sheet"}], ["ArrowDown", {"tag": "down"}], ["ArrowDown", {"tag": "down2"}],
                 ["ArrowUp", {"tag": "up"}], ["Escape", {"tag": "closed"}], ["Escape", {"tag": "back"}],
                 ["Enter", {"tag": "title"}], ["Escape", {"tag": "end"}]]
        res = self.render(st, steps)
        s = {x["s"]: x for x in res["steps"]}
        for x in res["steps"]:
            self.assertV23Frame(x["v23"], x["s"])
            self.assertLayout(x["layout"], x["s"])
        self.assertV23Frame(res["v23"], "final")
        self.assertTrue(s["ArrowLeft:first"]["v23"]["kicker"].split(" ")[0] in ("Bonjour", "Bon", "Bonsoir"),
                        s["ArrowLeft:first"]["v23"]["kicker"])                   # salutation v23 au-dessus du titre
        for t in ("ArrowRight:card", "ArrowLeft:first"):                          # carte : anneau blanc + agrandissement
            c = s[t]["v23"]["card"]
            self.assertEqual(c["border"], "rgb(255, 255, 255)", (t, c))
            ring = c["bw"] + max([float(x) for x in re.findall(r"rgb\(255, 255, 255\) 0px 0px 0px ([\d.]+)px",
                                                                 c["shadow"])] or [0])
            self.assertGreaterEqual(ring, 4, (t, c))                              # anneau ≥ 4 px : lisible en 1080p
            self.assertTrue(1.05 <= self._scale(c["transform"]) <= 1.1, (t, c))
            self.assertGreaterEqual(c["radius"], 16, (t, c))
        top = s["ArrowUp:top"]["v23"]["gear"]                                    # ↑ depuis la 1re carte : Réglages
        self.assertTrue(top["focused"] and s["ArrowUp:top"]["settings"], top)
        self.assertEqual(top["bgc"], "rgb(255, 255, 255)", top)                  # focus v23 : cercle blanc agrandi
        self.assertGreaterEqual(self._scale(top["transform"]), 1.05, top)
        sh = s["Enter:sheet"]["v23"]["panel"]                                    # feuille latérale pleine hauteur
        self.assertIn("sheet", sh["cls"])
        self.assertGreaterEqual(sh["r"][3] - sh["r"][1], 900, sh)
        self.assertTrue(560 <= sh["r"][2] - sh["r"][0] <= 680, sh)
        for a, b in zip(sh["btns"], sh["btns"][1:]):
            self.assertGreaterEqual(b["r"][1], a["r"][3] - 0.5, sh)                # lignes empilées verticalement
        self.assertEqual([b["bgc"] for b in sh["btns"] if b["focused"]], ["rgb(255, 255, 255)"], sh)
        self.assertEqual([s[t]["btn"] for t in ("Enter:sheet", "ArrowDown:down", "ArrowDown:down2", "ArrowUp:up")],
                         ["Reconfigurer", "Oublier les téléphones", "Fermer", "Oublier les téléphones"])
        self.assertTrue(s["Escape:closed"]["settings"] and not s["Escape:closed"]["panel"])
        self.assertEqual(s["Escape:back"]["focus"], "Alpha")
        self.assertEqual(s["Enter:title"]["ptitle"], "Alpha")                    # fiche du titre : actions inchangées
        self.assertEqual(s["Enter:title"]["btns"], ["J'aime", "Pas pour moi", "Déjà vu", "À voir plus tard",
                                                    "Pas ce soir"])
        self.assertFalse(s["Escape:end"]["panel"])
        for jargon in ("terminal", "sudo", "http", "opentv-pi", "agent"):
            self.assertNotIn(jargon, res["text"].lower())
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])

    def test_setup_screen_is_phone_driven(self):
        st = dict(self.STATE, setup={"active": True, "url": "http://192.168.2.32:8081/setup?t=x", "hotspot": None,
                                      "message": "", "expires_in": 600})
        steps = [["Enter"], ["ArrowDown"], ["Select"], ["BrowserBack"], ["BrowserHome"], ["Escape"], ["mouse", 3]]
        res = self.render(st, steps)
        self.assertTrue(res["setup"])
        self.assertFalse(res["home"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])  # aucune action à distance
        self.assertTrue(all(not x["panel"] for x in res["steps"]))
        prevented = {x["s"]: x["prevented"] for x in res["steps"]}
        self.assertTrue(prevented["BrowserBack"] and prevented["BrowserHome"] and prevented["mouse"])
        self.assertFalse(prevented["Enter"])


    def test_phone_events_drive_the_tv_and_pairing_box(self):
        st = dict(self.STATE, remote={"enabled": True, "url": "http://opentv-salon.local:8082/r?p=" + "x" * 43,
                                      "expires_in": 300, "phones": 0})
        remote = [{"seq": 5, "events": [{"seq": 5, "a": "down"}]},                 # synchronisation : jamais rejoué
                  {"seq": 8, "events": [{"seq": 6, "a": "right"}, {"seq": 7, "a": "reboot"}, {"seq": 8, "a": "ok"}]}]
        res = self.render(st, [], remote=remote)
        self.assertEqual((res["focus"], res["panel"]), ("Bravo", True))         # → puis OK ; « reboot » ignoré
        self.assertTrue(res["remotebox"])                                        # colonne réservée : reste visible
        self.assertEqual(res["rqr"], "ok")
        self.assertNotIn("code", res["text"].lower())                            # aucun code à recopier
        res = self.render(st, [])
        self.assertTrue(res["remotebox"])                                        # aucun téléphone : QR toujours visible
        self.assertIn("Même Wi-Fi", res["text"])
        self.assertTrue(any(c.startswith("GET /api/tv/remote?after=") for c in res["calls"]))
        st["remote"]["phones"] = 1
        res = self.render(st, [])
        self.assertTrue(res["remotebox"])                                        # téléphone perdu : QR sans naviguer
        self.assertIn("Téléphone perdu", res["text"])
        st_setup = dict(st, setup={"active": True, "url": None, "hotspot": None, "message": "", "expires_in": 600})
        res = self.render(st_setup, [], remote=[{"seq": 1, "events": []}, {"seq": 2, "events": [{"seq": 2, "a": "ok"}]}])
        self.assertFalse(res["remotebox"] or res["panel"])                       # configuration : téléphone de setup


    IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_ux_no_technical_address_on_screen_and_success_confirmed(self):
        setup = dict(self.STATE, setup={"active": True, "url": "http://192.168.2.32:8081/setup?t=" + "x" * 43,
                                        "hotspot": None, "message": "", "expires_in": 600})
        res = self.render(setup, [])
        self.assertTrue(res["setup"])
        self.assertIsNone(self.IPV4.search(res["text"]), res["text"])          # QR seulement, aucune adresse IP
        self.assertNotIn("http", res["text"])
        home = dict(self.STATE, remote={"enabled": True, "url": "http://192.168.2.32:8082/r?p=" + "x" * 43,
                                        "expires_in": 300, "phones": 0})
        res = self.render([setup, home], [], delay=3000)                        # configuration terminée
        self.assertTrue(res["home"])
        self.assertIn("C'est prêt", res["toast"] or "")
        self.assertIn("téléphone", res["toast"])                                 # télécommande découvrable
        self.assertTrue(res["toastBig"])
        self.assertTrue(res["remotebox"])
        self.assertIsNone(self.IPV4.search(res["text"]), res["text"])
        self.assertNotIn("http", res["text"])
        self.assertIn("Appareil photo", res["text"])
        self.assertNotIn("télécommande de la TV", res["text"])                   # jamais affirmé avant validation
        res = self.render(home, [])
        self.assertIsNone(res["toast"])                                          # pas de faux « C'est prêt »

    def test_setup_steps_match_the_three_step_wizard_and_hide_jargon(self):
        st = dict(self.STATE, setup={"active": True, "url": "http://192.168.2.32:8081/setup?t=" + "x" * 43,
                                     "hotspot": None, "message": "", "expires_in": 600})
        text = self.render(st, [])["text"]
        for want in ("Scanne le QR", "Choisis ton Wi-Fi", "mot de passe"):
            self.assertIn(want, text)
        for jargon in ("agent", "formulaire", "terminal", "opentv-pi", "sudo"):
            self.assertNotIn(jargon, text.lower())
        hs = dict(st, setup=dict(st["setup"], hotspot={"ssid": "OpenTV-AB12", "state": "active",
                                                       "wifi_qr": "WIFI:T:WPA;S:OpenTV-AB12;P:abcdefghijkm;;"}))
        text = self.render(hs, [])["text"]
        self.assertIn("rejoins le Wi-Fi « OpenTV-AB12 »", text)
        self.assertNotIn("agent", text.lower())

    # ---- géométrie 1920×1080 : bandeau d'assistant (accueil seulement, emplacement réservé) et écran de configuration
    @staticmethod
    def _inter(a, b):
        return min(a[2], b[2]) - max(a[0], b[0]) > 0.5 and min(a[3], b[3]) - max(a[1], b[1]) > 0.5

    @staticmethod
    def _inside(a, b):
        return a[0] >= b[0] - 0.5 and a[1] >= b[1] - 0.5 and a[2] <= b[2] + 0.5 and a[3] <= b[3] + 0.5

    SETUP_N = {"active": True, "url": "http://opentv-salon.local:8081/setup?t=" + "x" * 43, "hotspot": None,
               "message": "", "expires_in": 600}
    SETUP_HS = dict(SETUP_N, hotspot={"ssid": "OpenTV-AB12", "state": "active",
                                      "wifi_qr": "WIFI:T:WPA;S:OpenTV-AB12;P:abcdefghijkm;;"})

    def assertSetupClean(self, res, hot, where):
        g, ids, vp = res["geom"], res["geom"]["ids"], [0, 0, 1920, 1080]
        self.assertTrue(res["setup"] and not res["home"], where)
        self.assertIsNone(res["banner"], where)                                  # jamais de message en configuration
        self.assertIsNone(ids["banner"], where)
        self.assertNotIn("Message de", res["text"], where)
        qrs = ["qr", "apqr"] if hot else ["qr"]
        self.assertEqual(ids["apqr"] is not None, hot, where)
        figs = [ids["qrfig"]] + ([ids["apfig"]] if hot else [])
        for q in qrs:
            self.assertTrue(self._inside(ids[q], vp) and self._inside(ids[q], ids["setup"]), (where, q, ids))
            self.assertEqual(g["top"][q], [True] * 5, (where, q))               # rien ne recouvre le QR
        if hot:
            self.assertFalse(self._inter(ids["qrfig"], ids["apfig"]), (where, ids))
        for t in ("setup_kicker", "setup_h1", "steps", "setupmsg", "phoneonly"):
            self.assertIsNotNone(ids[t], (where, t))
            self.assertTrue(self._inside(ids[t], vp), (where, t, ids[t]))
            for f in figs:                                                       # titre/étapes jamais sur un QR
                self.assertFalse(self._inter(ids[t], f), (where, t, ids[t], f))
        self.assertEqual(g["setupClip"], [], where)                              # aucun texte rogné en bas
        self.assertLessEqual(ids["phoneonly"][3], ids["setup"][3] + 0.5, (where, ids))
        self.assertLessEqual(ids["phoneonly"][3], 1080, (where, ids))
        self.assertEqual(g["top"]["phoneonly"], [True] * 5, where)               # « Tout se règle… » entier, visible
        self.assertIn("Tout se règle sur le téléphone", res["text"])

    def test_setup_normal_and_hotspot_layout_without_overlap_or_clipping(self):
        """Configuration (normale et hotspot), avec un message d'assistant en attente : le message n'est jamais
        rendu ; QR, titre et étapes ne se recouvrent pas ; la ligne « Tout se règle… » tient dans l'écran."""
        base = dict(self.STATE, agents=["muse", "hermit", "hermes"], message=self.MSG, remote=self.REMOTE1)
        for hot, setup in ((False, self.SETUP_N), (True, self.SETUP_HS)):
            res = self.render(dict(base, setup=setup), [["Enter"], ["ArrowDown"]])
            self.assertSetupClean(res, hot, "hotspot" if hot else "normal")
            for x in res["steps"]:
                self.assertIsNone(x["geom"]["ids"]["banner"], x["s"])
        # accueil avec message puis reconfiguration : le message disparaît dès l'écran de configuration
        res = self.render([dict(base, setup={"active": False}), dict(base, setup=self.SETUP_HS)], [], delay=3000)
        self.assertSetupClean(res, True, "home->hotspot")
        self.assertNotIn("msg", res["geom"]["heroClass"])

    FYMSG = {"note": "", "marks": {"m1": {"opinion": "like", "watchlist": True}}, "rows": [
        {"id": "r1", "title": "Le choix de ce soir", "items": [
            {"id": "m1", "title": "Un titre de film vraiment très long qui tiendrait sur deux lignes au moins",
             "year": 2021, "genres": ["Drame", "Comédie", "Aventure"], "imdb": {"rating": 7.8}, "tmdb": {"rating": 7.1},
             "reasons": [{"code": "taste", "text": "Parce que tu aimes les drames"}], "poster": "https://invalid.test/p.jpg",
             "description": "Une longue description du film. " * 12},
            {"id": "m2", "title": "Bravo"}]}] + FY["rows"][1:]}

    def assertBannerReserved(self, g, where):
        ids = g["ids"]
        b = ids["banner"]
        self.assertIsNotNone(b, where)
        self.assertTrue(g["banner"]["inHtext"] and "msg" in g["heroClass"], (where, g))
        self.assertTrue(self._inside(b, ids["hero"]) and self._inside(b, ids["main"]), (where, b, ids["hero"]))
        for k in ("htitle", "hkicker", "hmeta", "hchips", "remotebox", "rqr", "qr", "apqr", "panel"):
            if ids.get(k):
                self.assertFalse(self._inter(b, ids[k]), (where, k, b, ids[k]))  # jamais sur titre, puces, QR
        self.assertLessEqual(ids["htitle"][3] + 8, b[1], (where, ids["htitle"], b))   # sous le titre, espacé
        self.assertEqual(g["top"]["htitle"], [True] * 5, where)                  # titre réellement au premier plan
        self.assertEqual(g["top"]["banner"], [True] * 5, where)
        lh = g["banner"]["lineH"]
        self.assertLessEqual(g["banner"]["clientH"], 2 * lh + 0.5, (where, g["banner"]))   # au plus 2 lignes
        self.assertGreaterEqual(g["banner"]["clientH"], lh - 0.5, (where, g["banner"]))
        self.assertLessEqual(g["htitle"]["clientH"], g["htitle"]["lineH"] + 0.5, (where, g["htitle"]))  # titre 1 ligne

    def test_home_assistant_message_has_reserved_slot_in_hero(self):
        """Accueil 1920×1080 : le message occupe un emplacement réservé en bas du héros — jamais sur #htitle, l'affiche,
        les puces ni le QR de la télécommande ; au plus 2 lignes ; masqué pendant tout panneau, puis de retour."""
        st = dict(self.STATE, remote=self.REMOTE1, agents=["muse", "hermit", "hermes"], message=self.MSG)
        steps = [["ArrowLeft", {"tag": "long"}], ["ArrowRight", {"tag": "short"}], ["ArrowDown", {"tag": "row1"}],
                 ["ArrowUp", {"tag": "top"}], ["Enter", {"tag": "settings"}], ["Escape", {"tag": "closed"}],
                 ["ArrowDown"], ["Enter", {"tag": "title"}], ["Escape", {"tag": "end"}]]
        res = self.render(st, steps, fy=self.FYMSG)
        s = {x["s"]: x for x in res["steps"]}
        for t in ("ArrowLeft:long", "ArrowRight:short", "ArrowDown:row1", "ArrowUp:top", "Escape:closed",
                  "Escape:end"):
            self.assertBannerReserved(s[t]["geom"], t)
            self.assertLayout(s[t]["layout"], t)
        self.assertTrue(s["ArrowLeft:long"]["geom"]["ids"]["hchips"])            # puces présentes, non recouvertes
        for t in ("Enter:settings", "Enter:title"):                              # panneau : message masqué
            self.assertTrue(s[t]["panel"], t)
            self.assertIsNone(s[t]["geom"]["ids"]["banner"], t)
            self.assertEqual(s[t]["layout"]["panel_hidden_by"], [], t)
        self.assertTrue(res["banner"].startswith("Message de Muse : Ce soir"))
        # catalogue vide (logo du héros) et message court (1 ligne) : même emplacement réservé
        short = dict(st, message={"from": "hermit", "text": "Bonne soirée !"})
        for state, fy, tag in ((st, {"note": "", "marks": {}, "rows": []}, "empty"), (short, None, "short")):
            res = self.render(state, [], fy=fy)
            self.assertBannerReserved(res["geom"], tag)
        self.assertLessEqual(res["geom"]["banner"]["clientH"], res["geom"]["banner"]["lineH"] + 0.5)
        res = self.render(dict(st, message=None), [])                            # sans message : héros normal
        self.assertIsNone(res["geom"]["ids"]["banner"])
        self.assertNotIn("msg", res["geom"]["heroClass"])


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestPhonePadInChromium(unittest.TestCase):
    def test_remote_js_posts_same_form_with_csrf(self):
        from opentv_pi import web
        d = tmpdir(self, "otvpad")
        page = web.remote_pad_page("Salon <b>", "c" * 32)
        page = page.replace('<link rel="stylesheet" href="/static/remote.css">',
                            '<link rel="stylesheet" href="file://%s">' % os.path.join(PI4, "ui", "remote.css"))
        stub = ("<script>window.__POSTS__=[];var codes=[200,429,200];window.fetch=function(u,o){"
                "__POSTS__.push([u,o.method,o.body,o.headers.Accept,o.credentials]);var c=codes.length?codes.shift():200;"
                "return Promise.resolve({status:c,ok:c===200});};</script>")
        page = page.replace('<script src="/static/remote.js" defer></script>',
                            stub + '<script src="file://%s" defer></script>' % REMOTE_JS)
        page = page.replace("</main>", "</main><script>setTimeout(function(){var b=document.querySelectorAll("
                            "'#pad button[name=a]'),i=0;(function next(){if(i<b.length){b[i++].click();"
                            "setTimeout(next,50);return;}var n=__POSTS__.length;"
                            "for(var j=0;j<7;j++){b[0].click();}var burst=__POSTS__.length-n;"   # rafale synchrone
                            "setTimeout(function(){var r=document.createElement('pre');r.id='result';"
                            "r.textContent=JSON.stringify({posts:__POSTS__.slice(0,n),burst:burst,url:location.href});"
                            "document.body.appendChild(r);},300);})();},300);</script>")
        path = os.path.join(d, "pad.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + tmpdir(self, "otvchr"),
                            "--virtual-time-budget=3000", "--window-size=390,844", "--dump-dom", "file://" + path],
                           capture_output=True, timeout=120)
        m = re.search(r'<pre id="result">(.*?)</pre>', p.stdout.decode("utf-8", "replace"), re.S)
        self.assertIsNotNone(m, p.stdout.decode()[-1500:])
        res = json.loads(html.unescape(m.group(1)))
        self.assertTrue(res["url"].startswith("file://"))                        # aucune navigation (pas de rechargement)
        bodies = [x[2] for x in res["posts"]]
        self.assertEqual(sorted(bodies), sorted("a=%s&c=%s" % (a, "c" * 32) for a in phoneremote.ACTIONS))
        self.assertTrue(all(x[0] == "/r/cmd" and x[1] == "POST" and x[3] == "application/json" and
                            x[4] == "same-origin" for x in res["posts"]))
        self.assertEqual(res["burst"], 4)                                        # au plus 4 envois en vol


# --------------------------------------------------------------------------- télécommande téléphone : cœur
class Wall:
    def __init__(self):
        self.t = 1.8e9

    def __call__(self):
        return self.t


class Synced:
    def __init__(self, v=True):
        self.v = v

    def __call__(self):
        return self.v


class TestPhoneRemoteCore(unittest.TestCase):
    def make(self, path=None, synced=True):
        d = tmpdir(self, "otvrem")
        self.wall, self.mono, self.logs, self.synced = Wall(), Clock(), [], Synced(synced)
        path = path or os.path.join(d, "remote.json")
        return self.reopen(path), path

    def reopen(self, path):
        return phoneremote.PhoneRemote(path, wall=self.wall, mono=self.mono, synced=self.synced,
                                       log=lambda ev, **f: self.logs.append(dict(f, event=ev)))

    def test_qr_only_secret_256_bits_single_use_rotating_never_stored(self):
        r, path = self.make()
        pr = r.pairing()
        self.assertEqual(set(pr), {"secret", "expires_in"})                     # aucun code court
        self.assertRegex(pr["secret"], r"^[A-Za-z0-9_-]{43}$")                   # 32 octets aléatoires = 256 bits
        self.assertGreaterEqual(phoneremote.PAIR_SECRET_BYTES * 8, 192)
        for name in ("CODE_ALPHABET", "CODE_LEN", "normalize_code", "PAIR_FAIL_GLOBAL"):
            self.assertFalse(hasattr(phoneremote, name), name)
        self.assertTrue(r.check_pair_secret(pr["secret"]))
        self.assertTrue(r.check_pair_secret(pr["secret"]))                       # GET : jamais consommé
        self.assertEqual(r.pairing()["secret"], pr["secret"])
        tok, err = r.pair_phone("192.168.2.50", pr["secret"])
        self.assertIsNone(err)
        self.assertRegex(tok, r"^[A-Za-z0-9_-]{43}$")
        self.assertFalse(r.check_pair_secret(pr["secret"]))                      # usage unique : nouveau QR
        self.assertEqual(r.pair_phone("192.168.2.51", pr["secret"]), (None, "denied"))
        pr2 = r.pairing()
        self.assertNotEqual(pr2["secret"], pr["secret"])
        self.mono.t += phoneremote.PAIR_TTL_S + 1                                # expiration : rotation
        self.assertFalse(r.check_pair_secret(pr2["secret"]))
        self.assertEqual(r.pair_phone("192.168.2.51", pr2["secret"]), (None, "denied"))
        pr3 = r.pairing()
        self.assertNotEqual(pr3["secret"], pr2["secret"])
        tok2, err = r.pair_phone("192.168.2.52", pr3["secret"])
        self.assertIsNone(err)
        self.assertEqual(r.count(), 2)
        with open(path) as f:
            text = f.read()
        for secret in (tok, tok2, pr["secret"], pr2["secret"], pr3["secret"], r.pairing()["secret"]):
            self.assertNotIn(secret, text)                                       # empreintes seulement
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(tok, json.dumps(self.logs))
        self.assertNotIn(pr["secret"], json.dumps(self.logs))
        for bad in (None, "", "x" * 32, pr3["secret"][:-1], pr3["secret"] + "x", 12345):
            self.assertFalse(r.check_pair_secret(bad))

    def test_wrong_secrets_limited_per_ip_only(self):
        r, _ = self.make()
        good = r.pairing()["secret"]
        wrong = ("A" if good[0] != "A" else "B") + good[1:]
        for _ in range(phoneremote.PAIR_FAIL_PER_IP):
            self.assertEqual(r.pair_phone("192.168.2.60", wrong), (None, "denied"))
        self.assertEqual(r.pair_phone("192.168.2.60", good), (None, "limited"))     # même le bon secret
        self.assertTrue(r.pair_limited("192.168.2.60"))
        self.assertEqual(r.pairing()["secret"], good)                              # pas de rotation par erreurs
        tok, err = r.pair_phone("192.168.2.61", good)                              # autre appareil : non bloqué
        self.assertIsNone(err)
        self.mono.t += phoneremote.PAIR_FAIL_WINDOW_S + 1
        self.assertFalse(r.pair_limited("192.168.2.60"))

    def test_stable_session_token_survives_use_restart_and_lost_responses(self):
        r, path = self.make()
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        phone = r.session(tok)
        csrf = r.csrf(phone)
        for _ in range(20):                                                      # 20 jours d'usage quotidien
            self.wall.t += 86400
            p = r.session(tok)
            self.assertIsNotNone(p)                                              # même jeton, jamais remplacé
            self.assertEqual(r.csrf(p), csrf)
        self.assertFalse(r.check_csrf(p, "0" * 32))                             # CSRF refusé : rien d'autre
        self.assertEqual(r.count(), 1)
        r2 = self.reopen(path)                                                   # redémarrage du service
        self.assertEqual(r2.csrf(r2.session(tok)), csrf)
        self.assertEqual(r2.session(tok)["created"], phone["created"])
        for bad in (None, "", "a" * 43, tok + "x", "../../etc"):
            self.assertIsNone(r2.session(bad))

    def test_idle_expiry_requires_two_synchronised_times(self):
        r, path = self.make()
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        self.wall.t += phoneremote.SESSION_IDLE_S - 60
        self.assertIsNotNone(r.session(tok))
        self.wall.t += phoneremote.SESSION_IDLE_S + 1                            # inactif > 30 jours, heure sûre
        self.assertIsNone(r.session(tok))
        self.assertEqual(r.count(), 0)
        self.assertIn("remote_phone_expired", [x["event"] for x in self.logs])
        self.assertEqual(self.reopen(path).count(), 0)

    def test_clock_going_back_before_ntp_never_forgets_a_phone(self):
        r, path = self.make()
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        seen = r.session(tok)["seen"]
        self.synced.v = False                                                    # redémarrage, pas encore NTP
        for back in (3600, 400 * 86400, 20 * 365 * 86400):                       # heure reculée (jusqu'à 1990…)
            self.wall.t = seen - back
            self.assertIsNotNone(r.session(tok))
            self.assertEqual(r.session(tok)["seen"], seen)                       # « vu » jamais reculé
        self.assertEqual(self.reopen(path).count(), 1)
        self.wall.t = seen + 3 * phoneremote.SESSION_IDLE_S                      # heure fausse en avance, non sûre
        self.assertIsNotNone(r.session(tok))
        self.synced.v = True
        self.wall.t = seen + 3600                                                # NTP : heure vraie
        self.assertIsNotNone(r.session(tok))
        self.assertEqual(r.count(), 1)

    def test_phone_paired_before_ntp_survives_forward_correction(self):
        r, path = self.make(synced=False)
        self.wall.t = 1.6e9                                                      # heure restaurée, très en retard
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        self.synced.v = True
        self.wall.t = 1.8e9                                                      # NTP : +6 ans d'un coup
        p = r.session(tok)
        self.assertIsNotNone(p)                                                  # jamais oublié par la correction
        self.assertEqual((p["seen"], p["ss"]), (1.8e9, True))
        self.assertEqual(self.reopen(path).session(tok)["seen"], 1.8e9)          # référence corrigée persistée
        self.wall.t += phoneremote.SESSION_IDLE_S + 1
        self.assertIsNone(r.session(tok))                                        # puis l'inactivité vraie compte

    def test_disk_write_happens_outside_state_lock(self):
        r, path = self.make()
        held = []
        orig = phoneremote.atomic_write_json

        def spy(p, obj):
            got = r.lock.acquire(blocking=False)                                 # libre pendant le fsync ?
            if got:
                r.lock.release()
            held.append(not got)
            return orig(p, obj)
        phoneremote.atomic_write_json = spy
        self.addCleanup(setattr, phoneremote, "atomic_write_json", orig)
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        self.wall.t += phoneremote.SEEN_SAVE_S + 1
        r.session(tok)
        r.forget(r.session(tok)["id"])
        r.revoke_all()
        self.assertGreaterEqual(len(held), 4)
        self.assertFalse(any(held), held)

    def test_corrupt_state_fails_closed(self):
        r, path = self.make()
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        for bad in ("{", "[]", json.dumps({"v": 2, "secret": "zz", "phones": []}),
                    json.dumps({"v": 2, "secret": "a" * 64, "phones": [{"id": "x", "h": "y"}]}),
                    json.dumps({"v": 9, "secret": "a" * 64, "phones": []})):
            with open(path, "w") as f:
                f.write(bad)
            r2 = self.reopen(path)
            self.assertIsNone(r2.session(tok))
            self.assertEqual(r2.count(), 0)

    def test_max_phones_forget_and_revoke(self):
        r, _ = self.make()
        toks = []
        for i in range(phoneremote.MAX_PHONES + 1):
            self.wall.t += 10
            toks.append(r.pair_phone("192.168.2.%d" % (50 + i), r.pairing()["secret"])[0])
        self.assertEqual(r.count(), phoneremote.MAX_PHONES)
        self.assertIsNone(r.session(toks[0]))                                    # le plus ancien est oublié
        phone = r.session(toks[-1])
        csrf = r.csrf(phone)
        self.assertTrue(r.check_csrf(phone, csrf))
        for bad in (None, "", csrf[:-1] + ("0" if csrf[-1] != "0" else "1"), csrf + "0"):
            self.assertFalse(r.check_csrf(phone, bad))
        r.forget(phone["id"])
        self.assertIsNone(r.session(toks[-1]))
        r.revoke_all()
        self.assertEqual(r.count(), 0)
        self.assertTrue(all(r.session(t) is None for t in toks))

    def test_commands_allowlist_rate_limit_and_queue(self):
        r, _ = self.make()
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        phone = r.session(tok)
        for bad in ("reboot", "volume_up", "power", "http://x", "", None, "UP"):
            self.assertEqual(r.command(phone, "192.168.2.50", bad), "bad_action")
        res = [r.command(phone, "192.168.2.50", "right") for _ in range(int(phoneremote.CMD_BURST) + 5)]
        self.assertEqual(res.count("ok"), int(phoneremote.CMD_BURST))
        self.assertEqual(res[-1], "limited")
        self.mono.t += 1.0
        self.assertEqual(r.command(phone, "192.168.2.50", "ok"), "ok")           # débit rétabli
        self.assertEqual(r.wait_events(-1), {"seq": r.seq, "events": []})       # synchronisation sans rejeu
        evs = r.wait_events(0, timeout=0)["events"]
        self.assertLessEqual(len(evs), phoneremote.QUEUE_MAX)
        self.assertEqual(evs[-1]["a"], "ok")
        self.mono.t += phoneremote.EVENT_TTL_S + 1
        self.assertEqual(r.wait_events(0, timeout=0)["events"], [])             # périmés : jamais rejoués
        self.assertEqual(r.wait_events(r.seq + 50)["seq"], r.seq)               # service redémarré : resync

    def test_long_poll_wakes_and_is_bounded(self):
        import threading
        import time as _t
        r = phoneremote.PhoneRemote(os.path.join(tmpdir(self, "otvrem"), "remote.json"), synced=lambda: True)
        self.assertTrue(r.wait_events(0).get("idle"))                           # aucun téléphone : immédiat
        tok, _ = r.pair_phone("192.168.2.50", r.pairing()["secret"])
        phone = r.session(tok)
        threading.Timer(0.2, lambda: r.command(phone, "192.168.2.50", "left")).start()
        t0 = _t.monotonic()
        res = r.wait_events(r.seq, timeout=5)
        self.assertLess(_t.monotonic() - t0, 2)
        self.assertEqual([e["a"] for e in res["events"]], ["left"])
        t0 = _t.monotonic()
        self.assertEqual(r.wait_events(r.seq, timeout=0.3)["events"], [])
        self.assertLess(_t.monotonic() - t0, 1.5)
        r.waiters = phoneremote.WAITERS_MAX                                      # attentes simultanées plafonnées
        self.assertTrue(r.wait_events(r.seq).get("busy"))
        r.waiters = 0
        threading.Timer(0.2, r.revoke_all).start()                              # révocation : l'attente se termine
        t0 = _t.monotonic()
        r.wait_events(r.seq, timeout=5)
        self.assertLess(_t.monotonic() - t0, 2)


# --------------------------------------------------------------------------- télécommande téléphone : HTTP
import http.client  # noqa: E402
import urllib.parse  # noqa: E402
from opentv_pi import web  # noqa: E402
from tests.test_service import ETH_DEV, ETH_IP, Net  # noqa: E402

HOSTNAME = "opentv-test"
MDNS = HOSTNAME + ".local"
LAN_IP = "192.168.2.40"


class Resolver:
    """Résolution mDNS simulée : par défaut, le nom désigne l'adresse LAN actuelle de l'application."""

    def __init__(self):
        self.answer = None   # None = adresse actuelle ; sinon ensemble fixe
        self.app = None
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        if self.answer is not None:
            return set(self.answer)
        return {self.app.net["ip"]} if name == MDNS and self.app.net["ip"] else set()


def remote_app(tc, net=None, resolver=None, synced=True, **env):
    res = resolver or Resolver()
    a = make_app(tc, net=net, app_kw={"hostname": HOSTNAME + ".lan", "resolver": res,
                                      "clock_synced": lambda: synced}, **env)
    res.app = a
    a.test_resolver = res
    return a


def remote_servers(tc, a, binds=None):
    """Couture de test : l'écoute « sur l'adresse LAN » est faite sur 127.0.0.1 (l'adresse simulée n'existe pas
    ici) ; le pair 127.0.0.1 est admis seulement dans ces tests (lan_peer le refuse en production)."""
    def bind(ip):
        if binds is not None:
            binds.append(ip)
        return "127.0.0.1"
    return Servers(tc, a, remote_bind=bind, remote_peer_ok=lambda ip: ip == "127.0.0.1")


class TestPhoneRemoteHttp(unittest.TestCase):
    def configured(self, **kw):
        a = remote_app(self, **kw)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        self.binds = []
        s = remote_servers(self, a, self.binds)
        a._mdns_check()
        return a, s

    def host(self, s, name=LAN_IP):
        return "%s:%d" % (name, s.web.remote_port())

    def req(self, s, method, path, body=None, headers=None, host=None, cookie=None):
        port = s.web.remote_port()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": host if host is not None else self.host(s)}
        if cookie:
            h["Cookie"] = "otv_remote=" + cookie
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def secret(self, a):
        return a.tv_state()["remote"]["url"].split("?p=")[1]

    def pair(self, a, s, host=None):
        host = host or self.host(s)
        code, h, _ = self.req(s, "POST", "/r/pair", {"p": self.secret(a)}, {"Origin": "http://" + host}, host=host)
        self.assertEqual(code, 303)
        m = re.match(r"otv_remote=([A-Za-z0-9_-]{43}); Path=/; Max-Age=\d+; HttpOnly; SameSite=Lax$", h["set-cookie"])
        self.assertIsNotNone(m, h["set-cookie"])
        return m.group(1)

    def csrf(self, s, tok, host=None):
        code, _, body = self.req(s, "GET", "/r", cookie=tok, host=host)
        self.assertEqual(code, 200)
        return re.search(r'name="c" value="([0-9a-f]{32})"', body).group(1)

    def test_closed_during_setup_open_after_on_lan_address_only(self):
        a = remote_app(self)
        binds = []
        s = remote_servers(self, a, binds)
        self.assertIsNone(s.web.remote_port())
        self.assertEqual(a.tv_state()["remote"], {"enabled": False})
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s.web.sync_lan()
        port = s.web.remote_port()
        self.assertIsNotNone(port)
        self.assertEqual(binds, [LAN_IP])                                        # adresse LAN, jamais 0.0.0.0
        self.assertEqual(s.web.remote.lan_ip, LAN_IP)
        a._mdns_check()
        rv = a.tv_state()["remote"]
        self.assertEqual(set(rv), {"enabled", "url", "expires_in", "phones"})    # aucun code
        self.assertRegex(rv["url"], r"^http://%s:%d/r\?p=[A-Za-z0-9_-]{43}$" % (re.escape(MDNS), port))
        self.assertEqual(rv["phones"], 0)
        a.request_setup()                                                        # « Reconfigurer » : fermé
        a.tick()
        s.web.sync_lan()
        self.assertIsNone(s.web.remote_port())
        self.assertEqual(a.tv_state()["remote"], {"enabled": False})

    def test_real_service_never_listens_on_wildcard_and_retries_bounded(self):
        import inspect
        src = inspect.getsource(web.WebServers.sync_remote)
        self.assertNotIn("bind_lan", src)                                        # 0.0.0.0 réservé à /setup
        a = remote_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.tick()
        clock = Clock()
        ws = web.WebServers(a, mono=clock)                                       # aucune couture : vraie adresse
        self.addCleanup(ws.stop)
        ws.sync_remote()                                                         # 192.168.2.40 n'existe pas ici
        self.assertIsNone(ws.remote_port())
        errs = lambda: [x for x in a.test_logs if x["event"] == "remote_listener_error"]   # noqa: E731
        self.assertEqual(len(errs()), 1)
        for _ in range(5):
            ws.sync_remote()
        self.assertEqual(len(errs()), 1)                                         # pas de rafale d'essais
        clock.t += web.REMOTE_RETRY_S + 1
        ws.sync_remote()
        self.assertEqual(len(errs()), 2)
        self.assertEqual(a.tv_state()["remote"], {"enabled": False})            # rien d'affiché sans écoute

    def test_lan_peer_policy(self):
        for ip, ok in (("192.168.2.50", True), ("10.1.2.3", True), ("172.16.0.9", True), ("127.0.0.1", False),
                       ("8.8.8.8", False), ("169.254.3.4", False), ("0.0.0.0", False), ("::1", False),
                       ("fe80::1", False), ("x", False), ("224.0.0.251", False)):
            self.assertEqual(web.lan_peer(ip), ok, ip)

    def test_host_allows_only_bound_address_and_exact_mdns_name(self):
        a, s = self.configured()
        port = s.web.remote_port()
        for host in (LAN_IP, MDNS, MDNS.upper()):
            self.assertEqual(self.req(s, "GET", "/r", host="%s:%d" % (host, port))[0], 200, host)
        self.assertEqual(self.req(s, "GET", "/static/remote.css", host="evil.example:%d" % port)[0], 421)
        self.assertEqual(self.req(s, "GET", "/static/remote.css", host="%s:%d" % (MDNS, port))[0], 200)
        for host in ("192.168.2.41:%d" % port, "10.0.0.5:%d" % port, "127.0.0.1:%d" % port, "localhost:%d" % port,
                     "8.8.8.8:%d" % port, "evil.example:%d" % port, "%s.:%d" % (MDNS, port),
                     "other-box.local:%d" % port, "%s.evil.example:%d" % (HOSTNAME, port), MDNS, LAN_IP,
                     "%s:%d" % (LAN_IP, port + 1), "%s:%d" % (MDNS, port + 1), "[::1]:%d" % port, "",
                     "%s:%d@evil" % (MDNS, port)):
            self.assertEqual(self.req(s, "GET", "/r", host=host)[0], 421, host)

    def test_address_change_restarts_listener_and_keeps_paired_phone(self):
        net = Net()
        a, s = self.configured(net=net)
        tok = self.pair(a, s, host=self.host(s, MDNS))
        old = s.web.remote
        net.ip = ETH_IP.replace(LAN_IP, "192.168.2.77")                          # nouveau bail DHCP
        a.tick()
        s.web.sync_lan()
        self.assertIsNot(s.web.remote, old)                                      # écoute relancée
        self.assertEqual(s.web.remote.lan_ip, "192.168.2.77")
        self.assertEqual(self.binds, [LAN_IP, "192.168.2.77"])
        self.assertEqual(self.req(s, "GET", "/r", host=self.host(s))[0], 421)   # ancienne adresse refusée
        self.assertEqual(self.req(s, "GET", "/r", host=self.host(s, "192.168.2.77"))[0], 200)
        code, _, body = self.req(s, "GET", "/r", cookie=tok, host=self.host(s, MDNS))
        self.assertIn('id="pad"', body)                                          # même nom : téléphone conservé
        a._mdns_check()
        self.assertTrue(a.tv_state()["remote"]["url"].startswith("http://%s:%d/r?p=" % (MDNS, s.web.remote_port())))
        self.assertIn("address_changed", json.dumps(a.test_logs))

    def test_mdns_unavailable_or_conflicting_falls_back_to_bound_address(self):
        a, s = self.configured()
        port = s.web.remote_port()
        for answer in (set(), {"192.168.2.99"}, {LAN_IP, "192.168.2.99"}):      # absent, conflit, ambigu
            a.test_resolver.answer = answer
            a._mdns_check()
            self.assertTrue(a.tv_state()["remote"]["url"].startswith("http://%s:%d/r?p=" % (LAN_IP, port)), answer)
        self.assertEqual(self.req(s, "GET", "/r", host="%s:%d" % (LAN_IP, port))[0], 200)
        a2 = remote_app(self, OPENTV_REMOTE_MDNS="0")                            # désactivé : jamais le nom
        config.save_device(a2.cfg, {"name": "Salon"})
        a2.reload_device()
        s2 = remote_servers(self, a2)
        a2._mdns_check()
        self.assertTrue(a2.tv_state()["remote"]["url"].startswith("http://%s:" % LAN_IP))
        self.assertEqual(self.req(s2, "GET", "/r", host="%s:%d" % (MDNS, s2.web.remote_port()))[0], 421)
        self.assertEqual(a2.test_resolver.calls, [])

    def test_mdns_check_runs_in_background_and_is_bounded(self):
        a, s = self.configured()
        n = len(a.test_resolver.calls)
        for _ in range(5):
            a.tick()
            if a._mdns_thread is not None:
                a._mdns_thread.join(5)
        self.assertLessEqual(len(a.test_resolver.calls) - n, 1)                 # au plus une par MDNS_REFRESH_S

    def test_pairing_flow_get_never_pairs_and_pages_send_real_origin(self):
        a, s = self.configured()
        p = self.secret(a)
        code, h, body = self.req(s, "GET", "/r?p=" + p)
        self.assertEqual(code, 200)
        self.assertIn("Oui, utiliser ce téléphone", body)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(a.remote.count(), 0)
        csp = h["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe", csp)
        # same-origin (jamais no-referrer) : un vrai navigateur envoie alors l'Origin du formulaire
        self.assertEqual((h["cache-control"], h["referrer-policy"]), ("no-store", "same-origin"))
        self.assertIn('<meta name="referrer" content="same-origin">', body)
        self.assertNotIn("no-referrer", body)
        self.assertNotIn("user-scalable", body)
        self.assertNotIn("maximum-scale", body)
        for _ in range(10):                                                      # GET répétés : rien consommé
            self.assertEqual(self.req(s, "GET", "/r?p=" + p)[0], 200)
        self.assertEqual(self.secret(a), p)
        code, _, body = self.req(s, "GET", "/r?p=" + "A" * 43)
        self.assertEqual(code, 403)
        self.assertIn("expiré", body)
        code, _, body = self.req(s, "GET", "/r")
        self.assertNotIn("<input", body)                                         # rien à taper : QR seulement
        self.assertNotIn("<form", body)
        self.assertIn("même Wi-Fi", body)
        tok = self.pair(a, s)
        self.assertEqual(a.remote.count(), 1)
        self.assertNotEqual(self.secret(a), p)                                   # QR renouvelé après succès
        code, _, body = self.req(s, "GET", "/r", cookie=tok)
        self.assertIn('id="pad"', body)
        self.assertIn("écran d'accueil", body)
        self.assertIn("même Wi-Fi", body)
        self.assertNotIn(a.tv_key, body)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=\"/static/remote\.js\")", body))   # aucun script en ligne
        for act in phoneremote.ACTIONS:
            self.assertIn('value="%s"' % act, body)
        code, _, body = self.req(s, "POST", "/r/pair", {"p": p}, {"Origin": "http://" + self.host(s)})
        self.assertEqual(code, 403)                                              # ancien QR : usage unique

    def test_origin_null_is_always_refused(self):
        a, s = self.configured()
        tok = self.pair(a, s)
        c = self.csrf(s, tok)
        p = self.secret(a)
        seq = a.remote.seq
        for path, form in (("/r/pair", {"p": p}), ("/r/cmd", {"a": "up", "c": c}), ("/r/forget", {"c": c})):
            for accept in ({}, {"Accept": "application/json"}):
                for origin in ("null", "NULL", " null"):
                    st, _, _ = self.req(s, "POST", path, form, dict(accept, Origin=origin), cookie=tok)
                    self.assertEqual(st, 403, (path, origin, accept))
        self.assertEqual((a.remote.count(), a.remote.seq), (1, seq))            # aucun effet
        self.assertTrue(a.remote.check_pair_secret(p))
        host = self.host(s, MDNS)
        for origin in ("http://evil.example", "http://%s" % self.host(s), "https://" + host,
                       "http://%s:1" % MDNS):
            st, _, _ = self.req(s, "POST", "/r/cmd", {"a": "up", "c": c}, {"Origin": origin}, host=host, cookie=tok)
            self.assertEqual(st, 403, origin)
        st, _, _ = self.req(s, "POST", "/r/cmd", {"a": "up", "c": c}, {"Origin": "http://" + host}, host=host,
                            cookie=tok)
        self.assertEqual(st, 303)

    def test_commands_csrf_origin_and_no_js(self):
        a, s = self.configured()
        tok = self.pair(a, s)
        c = self.csrf(s, tok)
        js = {"Accept": "application/json", "Origin": "http://" + self.host(s)}
        st, _, body = self.req(s, "POST", "/r/cmd", {"a": "up", "c": c}, js, cookie=tok)
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        ev = a.remote_events(0)
        self.assertEqual([e["a"] for e in ev["events"]], ["up"])
        st, h, _ = self.req(s, "POST", "/r/cmd", {"a": "down", "c": c}, cookie=tok)   # sans JS : 303 vers le pavé
        self.assertEqual((st, h["location"]), (303, "/r"))
        self.assertNotIn("set-cookie", h)                                        # jeton stable : jamais remplacé
        cases = [({"a": "up"}, js, 403), ({"a": "up", "c": "0" * 32}, js, 403),
                 ({"a": "up", "c": c}, dict(js, Origin="http://evil.example"), 403),
                 ({"a": "up", "c": c}, {"Accept": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
                 ({"a": "up", "c": c}, {"Accept": "application/json", "Referer": "http://evil.example/x"}, 403),
                 ({"a": "reboot", "c": c}, js, 400), ({"a": "http://x", "c": c}, js, 400)]
        for form, hdr, want in cases:
            st, _, _ = self.req(s, "POST", "/r/cmd", form, hdr, cookie=tok)
            self.assertEqual(st, want, (form, hdr))
        st, _, _ = self.req(s, "POST", "/r/cmd", {"a": "up", "c": c}, js)        # sans cookie
        self.assertEqual(st, 401)
        st, _, _ = self.req(s, "POST", "/r/cmd", '{"a":"up"}', dict(js, **{"Content-Type": "application/json"}),
                            cookie=tok)
        self.assertEqual(st, 400)
        self.assertEqual(self.req(s, "GET", "/r/cmd?a=up&c=" + c, cookie=tok)[0], 404)   # POST seulement
        codes = [self.req(s, "POST", "/r/cmd", {"a": "left", "c": c}, js, cookie=tok)[0] for _ in range(25)]
        self.assertIn(429, codes)                                                # débit borné

    def test_remote_state_writes_never_run_under_app_lock(self):
        import threading
        a, s = self.configured()
        held = []
        orig = phoneremote.atomic_write_json

        def spy(p, obj):
            free = []
            t = threading.Thread(target=lambda: free.append(a.lock.acquire(blocking=False) and
                                                            (a.lock.release() or True)))
            t.start()
            t.join(5)
            held.append(free != [True])
            return orig(p, obj)
        phoneremote.atomic_write_json = spy
        self.addCleanup(setattr, phoneremote, "atomic_write_json", orig)
        tok = self.pair(a, s)
        self.req(s, "POST", "/r/forget", {"c": self.csrf(s, tok)}, cookie=tok)
        self.pair(a, s)
        hdr = dict(s.tv_headers(), **{"Content-Type": "application/json"})
        self.assertEqual(s.req("POST", "/api/tv/remote/revoke", "{}", hdr)[0], 200)
        self.assertGreaterEqual(len(held), 4)
        self.assertFalse(any(held), held)

    def test_refused_csrf_or_lost_response_never_disconnects_the_phone(self):
        a, s = self.configured()
        tok = self.pair(a, s)
        c = self.csrf(s, tok)
        for form in ({"a": "up", "c": "f" * 32}, {"a": "up"}, {"c": "f" * 32}):
            self.req(s, "POST", "/r/cmd", form, {"Accept": "application/json"}, cookie=tok)
            self.req(s, "POST", "/r/forget", form, cookie=tok)                   # oubli avec CSRF faux : refusé
        conn = http.client.HTTPConnection("127.0.0.1", s.web.remote_port(), timeout=5)   # réponse perdue
        conn.request("POST", "/r/cmd", body="a=up&c=" + c, headers={
            "Host": self.host(s), "Cookie": "otv_remote=" + tok,
            "Content-Type": "application/x-www-form-urlencoded"})
        conn.sock.close()
        self.assertEqual(a.remote.count(), 1)
        self.assertEqual(self.csrf(s, tok), c)                                   # même jeton, même CSRF
        self.assertIsNotNone(phoneremote.PhoneRemote(a.cfg.remote_file).session(tok))   # et sur disque

    def test_tv_revoke_and_phone_forget(self):
        a, s = self.configured()
        tok = self.pair(a, s)
        c = self.csrf(s, tok)
        hdr = dict(s.tv_headers(), **{"Content-Type": "application/json"})
        code, _, body = s.req("GET", "/api/tv/remote?after=-1", None, s.tv_headers())
        self.assertEqual((code, json.loads(body)["events"]), (200, []))
        self.assertEqual(s.req("POST", "/api/tv/remote/revoke", "{}", {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(s.req("POST", "/api/tv/remote/revoke", "{}", hdr)[0], 200)
        st, _, body = self.req(s, "GET", "/r", cookie=tok)
        self.assertIn("Ouvre l'appareil photo", body)                           # plus appairé : rescanner
        st, _, _ = self.req(s, "POST", "/r/cmd", {"a": "up", "c": c}, {"Accept": "application/json"}, cookie=tok)
        self.assertEqual(st, 401)
        tok = self.pair(a, s)
        c = self.csrf(s, tok)
        st, h, _ = self.req(s, "POST", "/r/forget", {"c": c}, cookie=tok)
        self.assertEqual(st, 303)
        self.assertIn("Max-Age=0", h["set-cookie"])
        self.assertEqual(a.remote.count(), 0)
        logs = json.dumps(a.test_logs)
        self.assertNotIn(tok, logs)
        self.assertNotIn(self.secret(a), logs)

    def test_per_ip_concurrency_cap(self):
        import socket
        import time as _t
        a, s = self.configured()
        port = s.web.remote_port()
        idle = []
        for _ in range(4):                                                       # 4 connexions muettes : slots tenus
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(b"GET /r HTTP/1.1\r\n")
            idle.append(c)
        _t.sleep(0.3)
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(b"GET /r HTTP/1.0\r\nHost: " + self.host(s).encode() + b"\r\n\r\n")
        self.assertIn(b"503", c.recv(64))                                       # 5e de la même IP : refus immédiat
        c.close()
        for c in idle:
            c.close()
        for _ in range(50):
            if not s.web.remote.ip_active:
                break
            _t.sleep(0.05)
        self.assertEqual(s.web.remote.ip_active, {})                             # compteurs libérés
        self.assertEqual(self.req(s, "GET", "/r")[0], 200)

    @unittest.skipUnless(find_chromium() and NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                         "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
    def test_end_to_end_hid_and_phone_drive_real_tv_page(self):
        """Service réel + page /tv réelle (temps réel) : clavier HID puis téléphone appairé (→, OK, Retour)."""
        a, s = self.configured()
        host = self.host(s, MDNS)
        tok = self.pair(a, s, host=host)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tmpdir(self, "otvshots")
        os.makedirs(shots, exist_ok=True)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(),
               "port": s.web.remote_port(), "host": host, "cookie": tok, "csrf": self.csrf(s, tok, host=host),
               "shots": shots}
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_tv_remote.js"), os.environ["OPENTV_TEST_PLAYWRIGHT"],
                            json.dumps(cfg)], capture_output=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        st = {x["s"]: x for x in res["steps"]}
        first = st["start"]["focus"]
        self.assertNotEqual(st["key:ArrowDown"]["focus"], first)                 # HID : ↓
        self.assertNotEqual(st["key:ArrowRight"]["focus"], st["key:Down2"]["focus"])   # HID : → (rangée de 2)
        self.assertTrue(st["key:Enter"]["panel"])                                # HID : OK
        self.assertFalse(st["key:Escape"]["panel"])                              # HID : Retour
        self.assertEqual(st["key:Home"]["focus"], first)                         # HID : Accueil
        self.assertTrue(all(x["url"] == "/tv" for x in res["steps"]))            # jamais quitté /tv
        self.assertTrue(all(x["remotebox"] for x in res["steps"]))               # QR toujours dans sa colonne
        self.assertEqual(res["posts"], [200] * 5)
        self.assertNotEqual(st["phone:down"]["focus"], first)                    # téléphone : ↓
        self.assertNotEqual(st["phone:right"]["focus"], st["phone:down"]["focus"])   # téléphone : →
        self.assertTrue(st["phone:ok"]["panel"])                                 # téléphone : OK
        self.assertEqual(st["phone:ok"]["ptitle"], st["phone:right"]["focus"])
        self.assertFalse(st["phone:back"]["panel"])                              # téléphone : Retour
        self.assertLess(res["ok_latency_ms"], 1500)
        # Réglages : un seul ↑ depuis la 1re et la 3e rangée (et depuis une fiche), OK ouvre, ← → choisit, Retour ferme
        st = {x["s"]: x for x in res["settings"]}
        for tag in ("set:up-row0", "set:up-row2", "set:up-from-title"):
            self.assertTrue(st[tag]["settings"] and not st[tag]["panel"] and st[tag]["focus"] is None, st[tag])
        self.assertEqual((st["set:open"]["panel"], st["set:open"]["ptitle"]), (True, "Réglages"))
        self.assertEqual(res["settings_buttons"], ["Reconfigurer", "Oublier les téléphones", "Fermer"])
        self.assertEqual(st["set:confirm"]["ptitle"], "Afficher un nouveau QR de configuration ?")
        self.assertEqual(res["confirm_btn"], "Non")                              # confirmation : « Non » par défaut
        self.assertEqual(st["set:back"]["ptitle"], "Réglages")                   # Retour : un niveau
        self.assertTrue(st["set:close"]["settings"] and not st["set:close"]["panel"])
        self.assertFalse(st["set:content"]["settings"])
        self.assertTrue(st["set:title"]["panel"])
        self.assertEqual(res["setup_posts"], 0)                                  # rien sans confirmation explicite
        self.assertFalse(a.tv_state()["setup"]["active"])
        self.assertEqual(a.remote.count(), 1)
        for x in res["settings"]:
            lay = x["layout"]
            self.assertEqual(lay["viewport"], [1920, 1080])
            self.assertTrue(lay["gear_top"], (x["s"], lay))                      # Réglages visibles en haut
            self.assertEqual(lay["card_overlaps"], [], (x["s"], lay))            # QR jamais sur une carte
            self.assertFalse(lay["panel_box_overlap"], (x["s"], lay))
            self.assertTrue(lay["box_visible"] and lay["qr"] == "ok", (x["s"], lay))
            if not x["panel"]:
                self.assertTrue(lay["focus_in_view"] and lay["focus_ring"], (x["s"], lay))
            self.assertEqual(lay["ring_clip"], [], (x["s"], lay))                # anneau complet, jamais rogné
            self.assertEqual(lay["panel_hidden_by"], [], (x["s"], lay))
            self.assertTrue(lay["qr_crisp"], (x["s"], lay))                      # QR : 1 px canvas = 1 px CSS
            self.assertTrue(lay["bg_full"] and lay["gear_circle"], (x["s"], lay))  # identité v23 sur la vraie page
            self.assertTrue(lay["sheet_vertical"], (x["s"], lay))
        self.assertIn("sheet", st["set:open"]["layout"]["panel_cls"])            # Réglages : feuille latérale v23
        # Retour : une rangée à la fois, colonne gardée ; rangée 0 reste rangée 0 ; ↑ = Réglages en un geste
        self.assertGreaterEqual(res["rows_count"], 3)
        cl = {x["s"]: x for x in res["climb"]}
        self.assertNotEqual(cl["climb:down2"]["focus"], cl["climb:right1"]["focus"])
        self.assertEqual(cl["climb:back1"]["focus"], cl["climb:right1"]["focus"])
        self.assertEqual(cl["climb:back0"]["focus"], cl["climb:0"]["focus"])
        self.assertEqual(cl["climb:stay0"]["focus"], cl["climb:0"]["focus"])
        self.assertEqual(cl["climb:again1"]["focus"], cl["climb:right1"]["focus"])
        self.assertTrue(cl["climb:up"]["settings"] and not cl["climb:up"]["panel"])
        self.assertEqual(cl["climb:ret"]["focus"], cl["climb:again1"]["focus"])
        self.assertIn("Rangée précédente", cl["climb:back1"]["layout"]["hint"])
        for x in res["climb"]:
            self.assertEqual(x["url"], "/tv")
            self.assertEqual(x["layout"]["ring_clip"], [], (x["s"], x["layout"]))
        # message d'assistant + nom long + 3 assistants : barre d'une ligne, bandeau jamais sur un panneau
        hd = res["message"]["header"]
        self.assertLessEqual(hd["h"][3] - hd["h"][1], 200.5, hd)
        self.assertTrue(all(k["lines"] == 1 for k in hd["kids"]), hd)
        kids = {k["id"]: k for k in hd["kids"]}
        self.assertTrue(kids["device"]["clipped"] and kids["agents"]["clipped"], hd)
        self.assertFalse(kids["gear"]["clipped"] or kids["clock"]["clipped"] or kids["date"]["clipped"] or
                         kids["glabel"]["clipped"], hd)
        ms = {x["s"]: x for x in res["message"]["steps"]}
        self.assertTrue(ms["msg:home"]["banner_visible"] and ms["msg:closed"]["banner_visible"])
        self.assertEqual((ms["msg:confirm"]["ptitle"], ms["msg:confirm"]["btn"]),
                         ("Afficher un nouveau QR de configuration ?", "Non"))
        for t in ("msg:settings", "msg:confirm", "msg:back", "msg:title"):
            self.assertTrue(ms[t]["panel"], ms[t])
            self.assertFalse(ms[t]["banner_panel_overlap"], ms[t])
            self.assertEqual(ms[t]["panel_hidden_by"], [], ms[t])
        self.assertEqual(res["message"]["posts"], 0)
        for t in ("msg:home", "msg:closed", "msg:rows", "msg:end"):              # bandeau : place réservée du héros
            self.assertTrue(ms[t]["banner_visible"] and ms[t]["banner_in_hero"], ms[t])
            self.assertEqual(ms[t]["banner_overlaps"], [], ms[t])                # jamais sur #htitle ni le QR
            self.assertTrue(ms[t]["banner_lines_ok"], ms[t])                     # au plus 2 lignes
        for t in ("msg:settings", "msg:confirm", "msg:back", "msg:title"):
            self.assertFalse(ms[t]["banner_visible"], ms[t])                     # masqué pendant tout panneau
        # configuration normale + hotspot (message en attente) : aucun bandeau, QR et titre jamais recouverts
        self.assertEqual([x["hotspot"] for x in res["setup"]], [False, True])
        for x in res["setup"]:
            self.assertTrue(x["setup_visible"], x)
            self.assertFalse(x["banner_visible"] or x["text_has_message"], x)
            self.assertEqual((x["qr_covered"], x["text_bad"]), ([], []), x)
            self.assertTrue(x["qr_in_view"] and not x["figs_overlap"], x)
            self.assertTrue(x["phoneonly_visible"] and x["phoneonly_bottom"] <= 1080, x)   # « Tout se règle… » entier
        self.assertTrue(res["empty"]["settings"] and res["empty"]["cards"] == 0)  # catalogue vide : Réglages au focus
        self.assertIn("se prépare", res["empty"]["text"])
        self.assertEqual(res["empty"]["ptitle"], "Réglages")
        for name in ("tv-1920x1080-home.png", "tv-1920x1080-settings-focus.png", "tv-1920x1080-settings-panel.png",
                     "tv-1920x1080-settings-confirm.png", "tv-1920x1080-title-panel.png", "tv-1920x1080-empty.png",
                     "tv-1920x1080-message-header.png", "tv-1920x1080-message-confirm.png",
                     "tv-1920x1080-setup.png", "tv-1920x1080-setup-hotspot.png"):
            self.assertGreater(os.path.getsize(os.path.join(shots, name)), 10000, name)

    @unittest.skipUnless(find_chromium() and NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                         "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
    def test_real_browser_forms_without_javascript_and_tv_layout_1920(self):
        """Vrai Chromium, JavaScript désactivé côté téléphone : QR -> « Oui » (POST) -> pavé -> touches (POST) ->
        « Oublier ce téléphone » (POST), Origin réelle envoyée ; formulaire d'une origine opaque (Origin: null)
        refusé ; écran TV 1920×1080 : la colonne du QR ne chevauche aucune carte (captures conservées)."""
        a, s = self.configured()
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tmpdir(self, "otvshots")
        os.makedirs(shots, exist_ok=True)
        port = s.web.remote_port()
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(),
               "pair_url": a.tv_state()["remote"]["url"], "origin": "http://%s:%d" % (MDNS, port),
               "map": "MAP %s 127.0.0.1" % MDNS, "shots": shots}
        self.assertTrue(cfg["pair_url"].startswith(cfg["origin"] + "/r?p="))
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_remote_forms.js"),
                            os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        self.assertFalse(res["js"])                                              # page téléphone sans JavaScript
        self.assertIn("Oui, utiliser ce téléphone", res["confirm"])
        self.assertTrue(res["pad_after_pair"])
        self.assertEqual(res["cookie"], {"name": "otv_remote", "domain": MDNS, "httpOnly": True, "sameSite": "Lax"})
        self.assertEqual(res["pad_after_cmds"], [True, True])
        self.assertEqual([x["path"] for x in res["posts"]], ["/r/pair", "/r/cmd", "/r/cmd", "/r/pair", "/r/forget"])
        legit = [x for x in res["posts"] if not x["opaque"]]
        self.assertTrue(all(x["origin"] == cfg["origin"] for x in legit), res["posts"])   # Origin réelle
        opaque = [x for x in res["posts"] if x["opaque"]]
        self.assertEqual([(x["origin"], x["status"]) for x in opaque], [("null", 403)])   # Origin: null refusé
        self.assertTrue(res["forgotten"])
        self.assertFalse(res["cookie_after_forget"])
        self.assertEqual(a.remote.count(), 0)
        self.assertEqual([x[1] for x in a.remote.events][-2:], ["right", "down"])
        self.assertGreaterEqual(res["layout_full"]["visible_cards"], 5, res["layout_full"])   # rangée pleine
        self.assertLessEqual(res["layout_full"]["main"][2] + 40, res["layout_full"]["box"][0])  # colonne séparée
        for lay in (res["layout0"], res["layout_full"], res["layout1"]):
            self.assertEqual(lay["viewport"], [1920, 1080])
            self.assertEqual(lay["qr"], "ok")
            self.assertTrue(lay["box_visible"], lay)
            self.assertGreaterEqual(lay["visible_cards"], 2, lay)
            self.assertEqual(lay["card_overlaps"], [], lay)                      # aucune carte sous le QR
            self.assertFalse(lay["main_overlaps_box"], lay)
            self.assertIsNone(re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", lay["text"]), lay["text"])
            self.assertNotIn("http", lay["text"])
        self.assertEqual(res["layout0"]["title"], "Télécommande sur ton téléphone")
        self.assertEqual(res["layout1"]["title"], "Ajouter un téléphone")       # récupérable sans naviguer
        for name in ("tv-1920x1080-remote-unpaired.png", "tv-1920x1080-remote-fullrow.png",
                     "tv-1920x1080-remote-paired.png", "phone-pad-nojs.png"):
            self.assertGreater(os.path.getsize(os.path.join(shots, name)), 10000, name)

    def test_setup_done_page_offers_phone_remote_in_one_tap(self):
        a = remote_app(self)
        s = remote_servers(self, a)
        tok = a.tv_state()["setup"]["token"]
        a.test_resolver.answer = set()                                           # mDNS pas encore vérifié : adresse
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertIn("C'est prêt", body)
        self.assertIn("même Wi-Fi", body)
        self.assertNotIn("télécommande de la TV fonctionne", body)               # jamais affirmé sans validation
        port = s.web.remote_port()
        self.assertIsNotNone(port)                                               # ouverte avant même la boucle
        m = re.search(r'<a class="primary" href="http://(%s|%s):%d/r\?p=([A-Za-z0-9_-]{43})">'
                      r'Utiliser ce téléphone comme télécommande</a>' % (re.escape(LAN_IP), re.escape(MDNS), port),
                      body)
        self.assertIsNotNone(m, body)
        host = "%s:%d" % (m.group(1), port)
        code, _, page = self.req(s, "GET", "/r?p=" + m.group(2), host=host)       # le toucher du lien
        self.assertEqual(code, 200)
        self.assertIn("Oui, utiliser ce téléphone", page)
        code, h, _ = self.req(s, "POST", "/r/pair", {"p": m.group(2)}, {"Origin": "http://" + host}, host=host)
        self.assertEqual(code, 303)                                              # un second toucher : appairé
        self.assertIn("otv_remote=", h["set-cookie"])
        self.assertEqual(a.remote.count(), 1)
        self.assertNotIn(m.group(2), a.tv_state()["remote"]["url"])             # QR de la TV renouvelé

    def test_setup_result_pages_explain_same_wifi_and_hotspot_recovery(self):
        page = web.setup_result("applying", via_hotspot=True)
        self.assertIn("Wi-Fi temporaire", page)
        self.assertIn("Wi-Fi habituel", page)
        self.assertIn("le même que la TV", page)
        for kind in ("applying", "done"):
            self.assertIn("même Wi-Fi que la TV", web.setup_result(kind))
        for kind, kw in (("applying", {"via_hotspot": True}), ("applying", {}), ("done", {}),
                         ("done", {"remote_link": "http://x.local:8082/r?p=" + "a" * 43})):
            text = web.setup_result(kind, **kw)
            self.assertNotIn("télécommande de la TV", text)
            for jargon in ("sudo", "opentv-pi", "<code>", "agent"):
                self.assertNotIn(jargon, text)

    def test_wizard_has_no_terminal_command_or_agent_jargon(self):
        ctx = {"net_mode": "ethernet", "name": "Salon", "policy": {}}
        for plan in ({"step": 2, "mode": "keep", "ssid": None, "error": None},
                     {"step": 2, "mode": "wifi", "ssid": "Maison", "error": None},
                     {"step": 2, "mode": "other", "ssid": None, "error": appmod.ERR_FORM_AGENT}):
            body = web.setup_form("t" * 43, ctx, plan=plan)
            text = re.sub(r"<[^>]+>", " ", body)
            for bad in ("opentv-pi", "<code>", "sudo", "agent issue", "terminal", "commande"):
                self.assertNotIn(bad, body)
            self.assertNotRegex(text, r"\b[Aa]gents?\b")

    def test_phone_pages_are_plain_and_repairable(self):
        a, s = self.configured()
        _, _, body = self.req(s, "GET", "/r")
        text = re.sub(r"<[^>]+>", " ", body)
        for jargon in ("token", "jeton", "CSRF", "cookie", "HTTP", "IP", "LAN", "port", "code"):
            self.assertNotRegex(text, r"\b%s\b" % jargon)
        _, _, body = self.req(s, "POST", "/r/pair", {"p": "Z" * 43})
        self.assertIn('class="err"', body)
        self.assertIn("appareil photo", body)                                    # erreur réparable : rescanner
        a.request_setup()
        a.tick()                                                                 # « Reconfigurer » pris en compte
        s.web.sync_lan()
        self.assertIsNone(s.web.remote_port())                                   # écoute fermée

    def test_install_ships_remote_assets(self):
        r = tmpdir(self, "otvrfs")
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            m = f.read()
        for path in ("/opt/opentv/share/ui/remote.css", "/opt/opentv/share/ui/remote.js",
                     "/opt/opentv/lib/opentv_pi/phoneremote.py"):
            self.assertIn(path, m)


# --------------------------------------------------------------------------- kiosque durci (HID, Chromium, logind)
POLICY_DSTS = ("/etc/chromium/policies/managed/opentv-kiosk.json",
               "/etc/chromium-browser/policies/managed/opentv-kiosk.json")
LOGIND_DST = "/etc/systemd/logind.conf.d/opentv-kiosk.conf"


class TestKioskHardening(unittest.TestCase):
    def test_cage_vt_switching_disabled(self):
        svc = unit("opentv-kiosk.service")["Service"]
        argv = svc["ExecStart"].split()
        self.assertEqual(argv[0], "/usr/bin/cage")
        self.assertNotIn("-s", argv)                                             # pas de Ctrl+Alt+Fn vers une console
        self.assertEqual(argv[-2:], ["--", "/opt/opentv/bin/opentv-kiosk-browser"])

    def test_logind_drop_in_ignores_power_keys_only(self):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read(os.path.join(PI4, "system", "logind-opentv-kiosk.conf"), encoding="utf-8")
        login = dict(cp["Login"])
        for k in ("HandlePowerKey", "HandlePowerKeyLongPress", "HandleRebootKey", "HandleSuspendKey",
                  "HandleHibernateKey"):
            self.assertEqual(login.get(k), "ignore", k)
        self.assertTrue(all(k.startswith("Handle") and v == "ignore" for k, v in login.items()), login)
        self.assertEqual(cp.sections(), ["Login"])

    def test_chromium_policy_blocks_devtools_print_downloads_external_navigation(self):
        with open(os.path.join(PI4, "system", "chromium-policy-opentv-kiosk.json"), encoding="utf-8") as f:
            pol = json.load(f)
        self.assertEqual(pol["DeveloperToolsAvailability"], 2)
        self.assertIs(pol["PrintingEnabled"], False)
        self.assertEqual(pol["DownloadRestrictions"], 3)
        self.assertEqual(set(pol["URLBlocklist"]), {"*", "chrome://*", "chrome-untrusted://*", "devtools://*"})
        self.assertEqual(sorted(pol["URLAllowlist"]), sorted(["http://127.0.0.1", "about:blank",
                                                               "file:///opt/opentv/share/ui/fallback.html"]))
        self.assertEqual(pol["IncognitoModeAvailability"], 1)
        self.assertEqual(pol["ExtensionInstallBlocklist"], ["*"])
        self.assertIs(pol["AllowFileSelectionDialogs"], False)
        # aucune politique ne touche au clavier : les 7 touches de navigation restent intactes
        for k in pol:
            self.assertNotRegex(k.lower(), r"key|keyboard|shortcut")
        for f in (os.path.join(PI4, "bin", "opentv-kiosk-browser"),):
            with open(f, encoding="utf-8") as fh:
                self.assertIn("127.0.0.1", fh.read())

    def test_install_uninstall_kiosk_policy_and_power_key_exactly(self):
        r = tmpdir(self, "otvrfs")
        os.makedirs(os.path.join(r, "etc/chromium/policies/managed"))            # livré par le paquet (vide)
        foreign = os.path.join(r, "etc/chromium/policies/managed/opentv-kiosk.json")
        with open(foreign, "w") as f:
            f.write('{"Foreign": true}\n')
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertIn("touche Power", p.stdout.decode())
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            m = f.read()
        for dst in POLICY_DSTS + (LOGIND_DST,):
            self.assertIn(dst, m)
            self.assertTrue(os.path.isfile(os.path.join(r, dst.lstrip("/"))), dst)
            self.assertEqual(os.stat(os.path.join(r, dst.lstrip("/"))).st_mode & 0o777, 0o644)
        with open(os.path.join(r, "var/lib/opentv-install/created-dirs.list")) as f:
            created = f.read().split()
        self.assertIn("/etc/systemd/logind.conf.d", created)
        self.assertIn("/etc/chromium-browser/policies/managed", created)
        self.assertNotIn("/etc/chromium/policies/managed", created)              # existait déjà : jamais noté
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)   # réinstallation
        with open(os.path.join(r, "var/lib/opentv-install/created-dirs.list")) as f:
            self.assertEqual(sorted(f.read().split()), sorted(created))
        before = tree(r)
        self.assertEqual(sh([UNINSTALL, "--root", r, "--dry-run"]).returncode, 0)
        self.assertEqual(tree(r), before)
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(foreign) as f:
            self.assertEqual(f.read(), '{"Foreign": true}\n')                     # fichier d'origine restauré
        for gone in ("etc/systemd/logind.conf.d", "etc/chromium-browser",
                     "etc/chromium-browser/policies/managed/opentv-kiosk.json"):
            self.assertFalse(os.path.exists(os.path.join(r, gone)), gone)
        self.assertTrue(os.path.isdir(os.path.join(r, "etc/chromium/policies/managed")))   # dossier du paquet gardé
        self.assertFalse(os.path.exists(os.path.join(r, "var/lib/opentv-install/created-dirs.list")))

    def test_install_rollback_removes_created_system_dirs(self):
        r = tmpdir(self, "otvrfs")
        os.makedirs(os.path.join(r, "etc/systemd"))
        os.makedirs(os.path.join(r, "etc/chromium-browser/policies"))
        os.chmod(os.path.join(r, "etc/chromium-browser/policies"), 0o555)        # échec de copie injecté
        self.addCleanup(os.chmod, os.path.join(r, "etc/chromium-browser/policies"), 0o755)
        before = tree(r)
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(tree(r), before, p.stderr.decode()[-1500:])             # retour exact à l'état initial


if __name__ == "__main__":
    unittest.main()
