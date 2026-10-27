"""Régression terrain (Pi réel + Samsung, après redémarrage) : la télécommande Samsung ne pilotait plus OpenTV.

Cause : l'amorçage de la minuterie (opentv-cec.service) ne faisait jamais d'<Active Source> ; seule l'unité manuelle
opentv-cec-announce.service rétablissait le routage des touches, d'où `announced_this_boot=false` après chaque
démarrage. Contrat corrigé :

1. une seule activation CEC par démarrage de la boîte, faite par la minuterie dans une fenêtre bornée après le boot,
   dès que HDMI est branché et l'adaptateur prêt — quelle que soit la source active que la TV annonce (autre/inconnue) ;
2. le témoin de démarrage n'est écrit qu'après une annonce réussie (TV pas allumée ou émission ratée ⇒ aucun témoin,
   nouvel essai au prochain passage tant que la fenêtre est ouverte) ;
3. les passages suivants du même démarrage (minuterie, redémarrage de service) n'émettent plus rien ;
4. la réactivation explicite reste possible (`sudo opentv-pi cec --activate`, `--force-announce`) ;
5. après Samsung Home, aucun redémarrage de service ni passage de minuterie ne vole le focus ;
6. ni l'annonce de démarrage ni `cec --activate` n'émettent jamais <Image View On> ni commande de réveil :
   question d'alimentation, puis <Active Source> et <Menu Status> seulement, TV déjà allumée.
"""
import ast
import contextlib
import fcntl
import io
import json
import os
import re
import unittest
from unittest import mock

from tests._common import PI4
from tests.test_remote import BOOT_A, BOOT_B, Base, FakeBus, make_root, tmpdir, unit
from opentv_pi import cecboot, cli, config, nethelper

EMITS = ("--image-view-on", "--active-source", "--menu-status", "--give-device-power-status")
# jamais <Image View On> : la TV est vue allumée avant l'annonce, et aucun chemin ne la réveille
ANNOUNCE_SEQ = ["--give-device-power-status", "--active-source", "--menu-status"]
WAKE = ("--image-view-on", "--text-view-on", "--standby", "--set-power-state", "--user-control-pressed")


def wake_args(bus):
    """Toute option de réveil/alimentation présente dans un argv réellement passé au faux cec-ctl."""
    return [o for a, _ in bus.calls for o in a if o in WAKE]


def make_proc(tc, boot, uptime=30.0):
    d = tmpdir(tc, "otvproc")
    p = os.path.join(d, "sys", "kernel", "random")
    os.makedirs(p)
    with open(os.path.join(p, "boot_id"), "w") as f:
        f.write(boot + "\n")
    set_uptime(d, uptime)
    return d


def set_uptime(proc, s):
    with open(os.path.join(proc, "uptime"), "w") as f:
        f.write("%.2f 12.34\n" % s)


class OtherSourceBus(FakeBus):
    """La TV répond qu'une AUTRE source est active (ou ne répond pas) : ne doit jamais empêcher l'annonce de boot."""

    def __init__(self, *a, active="3.0.0.0", **kw):
        super().__init__(*a, **kw)
        self.active = active

    def __call__(self, argv, timeout):
        if "--request-active-source" in argv:
            self.calls.append((list(argv), timeout))
            return (0, "ACTIVE_SOURCE (0x82):\n\tphys-addr: %s\n" % self.active) if self.active else (0, "Timed out\n")
        return super().__call__(argv, timeout)


class BootBase(Base):
    def setUp(self):
        super().setUp()
        self.proc = make_proc(self, BOOT_A, uptime=25.0)

    def tick(self, root, bus, **kw):
        kw.setdefault("boot_announce", True)
        return self.boot(root, bus, **kw)

    def marker(self):
        p = os.path.join(self.state, cecboot.STATE_FILE)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def emitted(self, bus, since=0):
        return [o for o in bus.opts()[since:] if o in EMITS]


class TestFirstBootAnnounce(BootBase):
    def test_first_boot_announces_even_if_tv_reports_other_or_unknown_active_source(self):
        for active in ("3.0.0.0", "0.0.0.0", None):
            with self.subTest(active=active):
                self.state = tmpdir(self, "otvcecst")
                r = make_root(self)
                bus = OtherSourceBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                                  "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}}, active=active)
                res = self.tick(r, bus)
                self.assertTrue(res["ok"], res)
                self.assertEqual((res["result"], res["selected"], res["registration"]),
                                 ("ready", "/dev/cec0", "registered"))
                self.assertEqual(res["announce"]["result"], "boot_done")
                self.assertEqual(bus.opts(), ["--playback"] + ANNOUNCE_SEQ)
                self.assertIn(["cec-ctl", "-d", "/dev/cec0", "-s", "--active-source", "phys-addr=1.0.0.0"],
                              bus.transmits())
                self.assertEqual(self.marker()["done"], {"/dev/cec0": "1.0.0.0"})

    def test_existing_registration_still_gets_one_boot_announce(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        res = self.tick(r, bus)
        self.assertEqual((res["ok"], res["registration"], res["announce"]["result"]), (True, "existing", "boot_done"))
        self.assertEqual(bus.opts(), ANNOUNCE_SEQ)

    def test_disconnected_claim_pending_or_foreign_never_announce(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        self.assertEqual(self.tick(r, bus)["result"], "disconnected")
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "foreign"}})
        self.assertEqual(self.tick(r, bus)["result"], "foreign_config_untouched")
        self.assertEqual(bus.transmits(), [])
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        orig = bus.render
        bus.render = lambda dev: orig(dev).replace("0x0010", "0x0000")
        self.assertEqual(self.tick(r, bus)["result"], "claim_pending")
        self.assertEqual(bus.transmits(), [])
        self.assertIsNone(self.marker())

    def test_hdmi_plugged_later_in_window_announces_once(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "f.f.f.f", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        self.assertEqual(self.tick(r, bus)["result"], "disconnected")
        set_uptime(self.proc, 85.0)
        bus.ad["/dev/cec0"]["pa"] = "1.0.0.0"
        res = self.tick(r, bus)
        self.assertEqual(res["announce"]["result"], "boot_done")
        self.assertEqual(self.emitted(bus), ANNOUNCE_SEQ)

    def test_tv_not_on_never_wakes_and_retries_inside_window(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        for power in ("standby", "to-on", "silent"):
            bus.power = power
            res = self.tick(r, bus)
            # TV en veille / inconnue : passage normal (pas un échec d'unité), rien d'autre que la question
            self.assertEqual((res["ok"], res["result"], res["announce"]["result"]),
                             (True, "ready", "refused_tv_not_on"))
        self.assertEqual(set(bus.opts()), {"--give-device-power-status"})
        self.assertIsNone(self.marker())                                    # (2) aucun témoin sans annonce
        bus.power = "on"
        set_uptime(self.proc, 140.0)
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "boot_done")
        self.assertEqual(self.marker()["boot_id"], BOOT_A)


class TestMarkerAfterSuccessOnly(BootBase):
    def test_failed_active_source_writes_no_marker_then_success_writes_it(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}}, fail={"--active-source"})
        res = self.tick(r, bus)
        self.assertEqual((res["ok"], res["result"]), (False, "announce_failed"))
        self.assertNotIn("--menu-status", bus.opts())
        self.assertIsNone(self.marker())
        bus.fail.clear()
        res = self.tick(r, bus)
        self.assertEqual((res["ok"], res["announce"]["result"]), (True, "boot_done"))
        self.assertEqual(self.marker(), {"v": 1, "boot_id": BOOT_A, "done": {"/dev/cec0": "1.0.0.0"}})

    def test_budget_exceeded_mid_announce_writes_no_marker(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        inner = bus.__call__

        def slow(argv, timeout):
            if "--active-source" in argv:
                self.clock.t += 1000
            return inner(argv, timeout)
        res = self.tick(r, slow)
        self.assertEqual((res["ok"], res["result"]), (False, "budget_exceeded"))
        self.assertIsNone(self.marker())

    def test_unwritable_state_dir_refuses_auto_announce_before_any_emission(self):
        """Sans témoin possible, chaque passage réannoncerait : refus avant toute émission."""
        if os.geteuid() == 0:
            self.skipTest("root ignore les droits")
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        open(os.path.join(self.state, cecboot.LOCK_FILE), "w").close()   # verrou déjà présent (passage précédent)
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        res = self.tick(r, bus)
        self.assertEqual(res["announce"]["result"], "state_not_writable")
        self.assertEqual(self.emitted(bus), [])


class TestOncePerBoot(BootBase):
    def test_subsequent_ticks_and_service_restarts_never_announce_again(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"}})
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "boot_done")
        n = len(bus.opts())
        for up in (85.0, 145.0, 205.0, 3600.0, 86400.0):          # minuterie 60 s, puis bien plus tard
            set_uptime(self.proc, up)
            res = self.tick(r, bus)
            self.assertEqual((res["ok"], res["result"]), (True, "ready"))
            self.assertIn(res["announce"]["result"], ("already_this_boot", "boot_window_passed"))
        self.assertEqual(bus.opts()[n:], [])                       # aucune émission, même pas de question

    def test_new_boot_announces_again_exactly_once(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.tick(r, bus)
        self.tick(r, bus)
        self.proc = make_proc(self, BOOT_B, uptime=22.0)           # « sudo reboot » : nouveau boot_id
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "boot_done")
        self.tick(r, bus)
        self.assertEqual(bus.opts().count("--active-source"), 2)
        self.assertEqual(self.marker()["boot_id"], BOOT_B)

    def test_lost_marker_after_window_never_announces(self):
        """/run/opentv-cec recréé (désinstallation partielle, nettoyage) : la fenêtre bornée garde le focus."""
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        set_uptime(self.proc, cecboot.BOOT_ANNOUNCE_WINDOW_S + 1)
        res = self.tick(r, bus)
        self.assertEqual(res["announce"]["result"], "boot_window_passed")
        self.assertEqual(bus.transmits(), [])

    def test_unknown_boot_or_uptime_fails_closed(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        os.unlink(os.path.join(self.proc, "uptime"))
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "uptime_unknown")
        self.proc = make_proc(self, "pas-un-uuid", uptime=20.0)
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "boot_unknown")
        self.assertEqual(bus.transmits(), [])

    def test_manual_announce_counts_as_this_boot(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.assertEqual(self.boot(r, bus, force_announce=True)["announce"]["result"], "done")
        n = len(bus.opts())
        self.assertEqual(self.tick(r, bus)["announce"]["result"], "already_this_boot")
        self.assertEqual(bus.opts()[n:], [])

    def test_boot_announce_disabled_by_config_is_never_attempted(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.assertEqual(self.tick(r, bus, boot_announce=False)["announce"], {"result": "not_requested"})
        self.assertEqual(bus.transmits(), [])
        self.assertTrue(config.Config({}).cec_boot_announce)
        self.assertFalse(config.Config({"OPENTV_CEC_BOOT_ANNOUNCE": "0"}).cec_boot_announce)
        with open(os.path.join(PI4, "opentv-pi.env.example")) as f:
            self.assertIn("#OPENTV_CEC_BOOT_ANNOUNCE=1", f.read())


class TestNoFocusStealAfterSamsungHome(BootBase):
    def test_home_overlay_then_service_restarts_and_ticks_emit_nothing(self):
        """Boot → annonce unique ; Samsung Home ouvert (aucun message CEC vers le Pi) ; opentv-cec relancé par la
        minuterie toutes les 60 s et redémarré à la main pendant la fenêtre : jamais d'Active Source qui fermerait le
        menu Samsung ou volerait une application native."""
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})
        self.tick(r, bus)
        self.assertEqual(bus.opts().count("--active-source"), 1)
        n = len(bus.opts())
        up = 25.0
        for _ in range(12):                    # Home ouvert / fermé, 12 passages dont plusieurs dans la fenêtre
            up += 15.0
            set_uptime(self.proc, up)
            self.assertEqual(self.tick(r, bus)["ok"], True)
        self.assertEqual(self.emitted(bus, n), [])
        self.assertEqual(bus.opts().count("--active-source"), 1)

    def test_only_the_timer_bootstrap_can_auto_announce(self):
        """Seules les unités opentv-cec (minuterie) et opentv-cec-announce (manuelle) lancent l'amorçage ; dans le code,
        seul `cli.cmd_cec_bootstrap` appelle `cecboot.bootstrap`, seul `cecboot.bootstrap` appelle `announce`, et le
        service principal (app.py) n'importe pas cecboot (sa découverte passe par cec.py, lecture seule)."""
        for name in os.listdir(os.path.join(PI4, "systemd")):
            with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
                text = f.read()
            if name == "opentv-cec-announce.service":
                self.assertIn("--force-announce", text)
                self.assertNotIn("[Install]", text)
                continue
            self.assertNotIn("--force-announce", text, name)
            if name != "opentv-cec.service":
                self.assertNotIn("cec-bootstrap", text, name)
        svc = unit("opentv-cec.service")["Service"]
        self.assertNotIn("Restart", svc)
        self.assertEqual(svc["RuntimeDirectoryPreserve"], "yes")    # témoin conservé entre passages
        uses = {}            # module -> {(fonction englobante, attribut cecboot utilisé)}
        imports = {}
        pkg = os.path.join(PI4, "opentv_pi")
        for fn in sorted(os.listdir(pkg)):
            if not fn.endswith(".py"):
                continue
            mod = fn[:-3]
            with open(os.path.join(pkg, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read(), fn)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("cecboot"):
                    imports.setdefault(mod, set()).update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and any(a.name == "cecboot" for a in node.names):
                    imports.setdefault(mod, set()).add("cecboot")
            for fdef in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                for node in ast.walk(fdef):
                    if isinstance(node, ast.Call):
                        f = node.func
                        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "cecboot":
                            uses.setdefault(mod, set()).add((fdef.name, f.attr))
                        elif isinstance(f, ast.Name) and f.id in ("announce", "bootstrap"):
                            uses.setdefault(mod, set()).add((fdef.name, f.id))
        self.assertNotIn("app", imports)                                      # service principal : jamais cecboot
        self.assertNotIn("app", uses)
        self.assertEqual(imports, {"cec": {"cecboot"}, "cli": {"cecboot"}})   # aucun `from .cecboot import announce`
        self.assertEqual({a for _, a in uses["cec"]}, {"parse_info", "classify"})   # lecture seule
        callers = {(m, fn) for m, s in uses.items() for fn, a in s if a in ("bootstrap", "announce")}
        self.assertEqual(callers, {("cli", "cmd_cec_bootstrap"), ("cecboot", "bootstrap")})
        self.assertEqual({a for fn, a in uses["cecboot"] if fn == "bootstrap" and a == "announce"}, {"announce"})


class TestNeverWakesTv(BootBase):
    """Annonce de démarrage et réactivation explicite : jamais <Image View On> ni aucune commande de réveil."""

    def test_boot_announce_emits_only_power_query_active_source_and_menu_status(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        res = self.tick(r, bus)
        self.assertEqual(res["announce"]["result"], "boot_done")
        self.assertEqual(bus.transmits(), [
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--give-device-power-status"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "--active-source", "phys-addr=1.0.0.0"],
            ["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", "--menu-status", "menu-state=activated"]])
        self.assertEqual(wake_args(bus), [])
        self.assertNotIn("image_view_on", res["announce"]["steps"])

    def test_boot_announce_never_wakes_whatever_the_tv_state(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "empty"}})
        for power in ("standby", "to-on", "to-standby", "silent", "on"):
            bus.power = power
            self.tick(r, bus)
        self.assertEqual(wake_args(bus), [])
        self.assertEqual(bus.opts().count("--active-source"), 1)

    def test_force_announce_is_the_same_sequence_without_wake(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        for power in ("standby", "to-on", "silent", "on"):
            bus.power = power
            self.boot(r, bus, force_announce=True)
        self.assertEqual(wake_args(bus), [])
        self.assertEqual(bus.opts(), ["--give-device-power-status"] * 3 + ANNOUNCE_SEQ)

    def test_run_refuses_any_wake_or_power_option_before_spawning(self):
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        run = cecboot._Run(bus, self.clock, 30)
        for opt in cecboot.NEVER_EMIT:
            with self.assertRaises(ValueError):
                run(["cec-ctl", "-d", "/dev/cec0", "-s", "-t", "0", opt], 5)
        self.assertEqual(bus.calls, [])
        self.assertEqual(cecboot.ANNOUNCE_OPTS, ("--active-source", "--menu-status"))
        self.assertTrue(set(WAKE) <= set(cecboot.NEVER_EMIT))

    def test_cli_activate_end_to_end_never_emits_image_view_on(self):
        """`opentv-pi cec --activate` → systemctl start opentv-cec-announce.service → `cec-bootstrap --force-announce`
        (argv de l'unité) → bus : simulé de bout en bout, jamais de réveil."""
        exec_start = unit("opentv-cec-announce.service")["Service"]["ExecStart"]
        self.assertTrue(exec_start.endswith("cec-bootstrap --force-announce"), exec_start)
        r = make_root(self)
        st = os.path.join(r, cecboot.STATE_DIR.lstrip("/"))
        os.makedirs(st)
        p = os.path.join(r, "proc", "sys", "kernel", "random")
        os.makedirs(p)
        with open(os.path.join(p, "boot_id"), "w") as f:
            f.write(BOOT_A + "\n")
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"},
                                   "/dev/cec1": {"pa": "f.f.f.f", "state": "empty"}})

        def systemd(argv, timeout):
            if argv[:2] == ["systemctl", "start"]:
                res = cecboot.bootstrap(r, runner=bus, mono=self.clock, keymap_io=self.km, state_dir=st,
                                        force_announce="--force-announce" in exec_start.split(),
                                        proc_root=os.path.join(r, "proc"))
                return (0 if res["ok"] else 1), ""
            return bus(argv, timeout)                                   # relecture d'état (cec-ctl -d <dev>)
        for power in ("standby", "on"):
            bus.power = power
            with mock.patch.object(nethelper, "default_runner", systemd), \
                    mock.patch.object(os, "geteuid", lambda: 0), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["cec", "--root", r, "--activate"])
            rep = json.loads(out.getvalue())
            self.assertEqual((code, rep["activation"]["ok"]), (0, True) if power == "on" else (1, False), rep)
        self.assertEqual(wake_args(bus), [])
        self.assertEqual(self.emitted(bus), ["--give-device-power-status"] + ANNOUNCE_SEQ)


class TestActivateWhileTimerRuns(BootBase):
    def hold_lock(self):
        fd = cecboot._lock(self.state)
        self.assertIsNotNone(fd)
        self.addCleanup(lambda: os.close(fd) if fd else None)
        return fd

    def test_force_announce_waits_for_timer_pass_then_announces(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        fd = self.hold_lock()
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            self.clock.t += s
            if len(sleeps) == 3:                     # le passage de la minuterie se termine
                fcntl.flock(fd, fcntl.LOCK_UN)
        res = self.boot(r, bus, force_announce=True, sleep=sleep)
        self.assertEqual((res["ok"], res["result"], res["announce"]["result"]), (True, "ready", "done"))
        self.assertEqual(len(sleeps), 3)

    def test_force_announce_still_busy_is_a_bounded_failure_without_emission(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.hold_lock()
        t0 = self.clock.t

        def sleep(s):
            self.clock.t += s
        res = self.boot(r, bus, force_announce=True, sleep=sleep)
        self.assertEqual((res["ok"], res["result"]), (False, "busy"))
        self.assertLessEqual(self.clock.t - t0, cecboot.LOCK_WAIT_S + 0.01)
        self.assertEqual(bus.calls, [])
        self.assertEqual(self.tick(r, bus)["result"], "busy")               # minuterie : jamais d'attente
        self.assertEqual(self.tick(r, bus)["ok"], True)

    def test_cec_bootstrap_busy_exit_code_and_activate_retry_note(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        r = make_root(self, devs=())
        env = os.path.join(self.state, "env")
        open(env, "w").close()
        with mock.patch.object(cecboot, "bootstrap", lambda *a, **kw: {"ok": False, "result": "busy"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--env", env, "cec-bootstrap", "--root", r, "--state-dir", self.state,
                                       "--force-announce"]), cli.CEC_BUSY_EXIT)
        cr = make_root(self, devs=())
        os.makedirs(os.path.join(cr, cecboot.STATE_DIR.lstrip("/")))
        seen = []

        def systemd(status):
            def runner(argv, timeout):
                seen.append(list(argv))
                if argv[:2] == ["systemctl", "start"]:
                    return 1, "Job for opentv-cec-announce.service failed. See journalctl -xe"
                return 0, status
            return runner
        for status, want in (("75\n", "busy"), ("1\n", None), ("bizarre", None)):
            with mock.patch.object(nethelper, "default_runner", systemd(status)), \
                    mock.patch.object(os, "geteuid", lambda: 0), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["cec", "--root", cr, "--activate"])
            rep = json.loads(out.getvalue())
            self.assertEqual((code, rep["activation"]["ok"], rep["activation"].get("result")), (1, False, want))
            self.assertNotIn("Job for", out.getvalue())                       # jamais de sortie brute
            if want == "busy":
                self.assertIn("réessayer dans une minute", rep["note"])
                self.assertIn("opentv-pi cec --activate", rep["note"])
            else:
                self.assertIn("journalctl", rep["note"])
        self.assertIn(["systemctl", "show", "--property=ExecMainStatus", "--value", "opentv-cec-announce.service"],
                      seen)


class TestExplicitReactivation(BootBase):
    def test_force_announce_ignores_boot_marker(self):
        r = make_root(self)
        bus = FakeBus(self.clock, {"/dev/cec0": {"pa": "1.0.0.0", "state": "ours"}})
        self.tick(r, bus)
        set_uptime(self.proc, 7200.0)
        res = self.boot(r, bus, force_announce=True)
        self.assertEqual((res["ok"], res["announce"]["result"]), (True, "done"))
        self.assertEqual(bus.opts().count("--active-source"), 2)

    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def cli_root(self, boot=BOOT_A):
        """Racine pour `opentv-pi cec --root` : /run/opentv-cec (témoin) et /proc (boot_id) sous la racine de test."""
        r = make_root(self, devs=())
        st = os.path.join(r, cecboot.STATE_DIR.lstrip("/"))
        os.makedirs(st)
        p = os.path.join(r, "proc", "sys", "kernel", "random")
        os.makedirs(p)
        with open(os.path.join(p, "boot_id"), "w") as f:
            f.write(boot + "\n")
        return r, st

    def activate(self, r, runner, euid=0):
        with mock.patch.object(nethelper, "default_runner", runner), mock.patch.object(os, "geteuid", lambda: euid):
            return self.run_cli("cec", "--root", r, "--activate")

    def test_cli_cec_activate_starts_announce_unit_as_root_only(self):
        r, st = self.cli_root()
        calls = []

        def runner(argv, timeout):           # l'unité transmet l'annonce : cecboot réécrit le témoin de ce démarrage
            calls.append((list(argv), timeout))
            cecboot.save_announced(st, BOOT_A, {"/dev/cec0": "1.0.0.0"})
            return 0, ""
        code, rep = self.activate(r, runner)
        self.assertEqual(code, 0, rep)
        self.assertEqual(calls, [(["systemctl", "start", "opentv-cec-announce.service"], cli.CEC_ACTIVATE_TIMEOUT_S)])
        self.assertEqual(rep["activation"], {"ok": True, "unit": "opentv-cec-announce.service", "service_ok": True,
                                             "announced": True})
        self.assertEqual(rep["bootstrap"], {"/dev/cec0": "1.0.0.0"})
        self.assertGreater(cli.CEC_ACTIVATE_TIMEOUT_S, cecboot.UNIT_TIMEOUT_START_S)
        calls.clear()
        code, rep = self.activate(r, lambda a, t: (1, "Job failed"))
        self.assertEqual((code, rep["activation"]["ok"], rep["activation"]["service_ok"]), (1, False, False))
        self.assertNotIn("Job failed", json.dumps(rep))                     # jamais de sortie brute
        code, rep = self.activate(r, runner, euid=1000)
        self.assertEqual((code, rep["error"]), (2, "root_required"))
        self.assertEqual(calls, [])                                         # rien n'est démarré sans root

    def test_cli_activate_service_exit_0_without_marker_is_not_success(self):
        """Unité sautée (pas de /dev/cec*), amorçage désactivé, HDMI débranché, configuration étrangère… : systemctl
        sort en 0 mais aucune <Active Source> n'a été transmise ⇒ jamais « ok »."""
        r, st = self.cli_root()
        code, rep = self.activate(r, lambda a, t: (0, ""))
        self.assertEqual(code, 1, rep)
        self.assertEqual(rep["activation"], {"ok": False, "unit": "opentv-cec-announce.service", "service_ok": True,
                                             "announced": False, "result": "no_announcement"})
        self.assertIn("journalctl -b -u opentv-cec-announce.service", rep["note"])
        self.assertFalse(os.path.exists(os.path.join(st, cecboot.STATE_FILE)))

    def test_cli_activate_stale_boot_marker_is_not_a_new_announcement(self):
        """Le témoin de l'annonce automatique du démarrage existe déjà : sans réécriture pendant --activate (unité
        sautée ou sortie 0 sans annonce), la réactivation n'est pas réussie."""
        r, st = self.cli_root()
        cecboot.save_announced(st, BOOT_A, {"/dev/cec0": "1.0.0.0"})
        code, rep = self.activate(r, lambda a, t: (0, ""))
        self.assertEqual((code, rep["activation"]["ok"], rep["activation"]["result"]), (1, False, "no_announcement"))
        self.assertEqual(rep["bootstrap"], {"/dev/cec0": "1.0.0.0"})        # l'état lu reste rapporté tel quel

        def rewrite(argv, timeout):
            cecboot.save_announced(st, BOOT_A, {"/dev/cec0": "1.0.0.0"})
            return 0, ""
        code, rep = self.activate(r, rewrite)
        self.assertEqual((code, rep["activation"]["ok"]), (0, True))

    def test_cli_activate_marker_of_other_boot_or_unknown_boot_fails(self):
        r, st = self.cli_root()

        def other_boot(argv, timeout):
            cecboot.save_announced(st, BOOT_B, {"/dev/cec0": "1.0.0.0"})
            return 0, ""
        code, rep = self.activate(r, other_boot)
        self.assertEqual((code, rep["activation"]["ok"], rep["activation"]["announced"]), (1, False, False))
        r, st = self.cli_root(boot="pas-un-uuid")

        def any_write(argv, timeout):
            cecboot.save_announced(st, "pas-un-uuid", {"/dev/cec0": "1.0.0.0"})
            return 0, ""
        code, rep = self.activate(r, any_write)
        self.assertEqual((code, rep["activation"]["ok"], rep["activation"]["result"]), (1, False, "boot_unknown"))

    def test_marker_stamp_changes_only_on_rewrite(self):
        st = tmpdir(self, "otvcecst")
        self.assertEqual(cecboot.marker_stamp(st, BOOT_A), ({}, None))
        cecboot.save_announced(st, BOOT_A, {"/dev/cec0": "1.0.0.0"})
        done, a = cecboot.marker_stamp(st, BOOT_A)
        self.assertEqual((done, cecboot.marker_stamp(st, BOOT_A)[1]), ({"/dev/cec0": "1.0.0.0"}, a))
        self.assertEqual(cecboot.marker_stamp(st, BOOT_B), ({}, None))
        self.assertEqual(cecboot.marker_stamp(st, ""), ({}, None))
        cecboot.save_announced(st, BOOT_A, {"/dev/cec0": "1.0.0.0"})
        self.assertNotEqual(cecboot.marker_stamp(st, BOOT_A)[1], a)

    def test_plain_cec_status_stays_read_only(self):
        r = make_root(self, devs=())
        with mock.patch.object(nethelper, "default_runner", lambda a, t: self.fail("émission %r" % a)):
            code, rep = self.run_cli("cec", "--root", r)
        self.assertEqual(code, 0)
        self.assertNotIn("activation", rep)

    def test_cli_passes_boot_announce_flag_from_config(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        r = make_root(self, devs=())
        env = os.path.join(self.state, "env")
        seen = []
        orig = cecboot.bootstrap

        def spy(*a, **kw):
            seen.append(kw.get("boot_announce"))
            return orig(*a, **kw)
        for content, want in (("", True), ("OPENTV_CEC_BOOT_ANNOUNCE=0\n", False)):
            with open(env, "w") as f:
                f.write(content)
            with mock.patch.object(cecboot, "bootstrap", spy):
                self.run_cli("--env", env, "cec-bootstrap", "--root", r, "--state-dir", self.state)
            self.assertEqual(seen[-1], want)


class TestDocs(unittest.TestCase):
    def test_docs_describe_single_boot_activation_and_reactivation(self):
        for name in ("README.md", "CEC_COMPATIBILITY.md"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                text = f.read()
            self.assertTrue("opentv-pi cec --activate" in text, name)
            self.assertTrue(re.search(r"une seule (?:annonce|activation)[^\n]*(?:démarrage|boot)", text, re.I), name)
            # séquence documentée sans réveil ; récupération honnête ; lacunes futures consignées
            self.assertNotRegex(text, r"(?:⇒|:)\s*`?<Image View On>`?,", name)
            self.assertIn("Afficher OpenTV", text, name)
            self.assertIn("<Request Active Source>", text, name)
            self.assertNotIn("choisir l'entrée OpenTV (tuile HDMI) ou", text, name)

    def test_readme_recovery_order_and_brand_policy(self):
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertTrue("ne suffit donc pas forcément" in readme, "README : sélection HDMI présentée comme suffisante")
        i_user, i_admin = readme.index("**utilisateur** : la télécommande téléphone"), readme.index(
            "**administrateur / terrain** : `sudo /opt/opentv/bin/opentv-pi cec --activate`")
        self.assertLess(i_user, i_admin)
        with open(os.path.join(PI4, "CEC_COMPATIBILITY.md"), encoding="utf-8") as f:
            compat = f.read()
        for needle in ("prouvée sur le terrain uniquement sur la Samsung", "Toutes les autres marques sont non testées",
                       "Compromis délibéré de la fenêtre de 240 s", "peut basculer une fois",
                       "jamais après\nle témoin ni après la fenêtre", "Jamais `<Image View On>`"):
            self.assertIn(needle, compat)


if __name__ == "__main__":
    unittest.main()
