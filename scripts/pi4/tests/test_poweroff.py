"""Réglages → « Éteindre la boîte » (0.7.23) : tests ciblés. AUCUN arrêt réel : le code OpenTV n'exécute aucune
commande (l'arrêt est OnSuccess=poweroff.target de l'unité) ; ici le « systemd » est un faux exécuteur qui lance la
vraie commande root sans privilège, PATH piégé (systemctl/shutdown/poweroff/halt/reboot/sudo enregistrent et échouent).
"""
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests._common import PI4
from tests.test_install import INSTALL, UNINSTALL
from tests.test_service import Servers, find_chromium, fixture_root, make_app
from tests import test_remote as tr
from opentv_pi import health, poweroff

BOOT_A = "0badc0de-0000-4000-8000-000000000001"   # celui de make_app
BOOT_B = "0badc0de-0000-4000-8000-0000000000b2"
CLI = os.path.join(PI4, "bin", "opentv-pi")
TRAPPED = ("systemctl", "shutdown", "poweroff", "halt", "reboot", "sudo", "init", "telinit")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def power_dir(path):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, poweroff.DIR_MODE)
    os.chown(path, -1, os.getgid())
    return path


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def trap_path(tc):
    """PATH piégé : toute tentative d'arrêt/commande système est notée puis échoue."""
    d = tempfile.mkdtemp(prefix="otvtrap", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    log = os.path.join(d, "calls.log")
    for name in TRAPPED:
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho \"%s $*\" >> '%s'\nexit 1\n" % (name, log))
        os.chmod(p, 0o755)
    return d, log


def proc_root(tc, boot):
    d = tempfile.mkdtemp(prefix="otvproc", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    os.makedirs(os.path.join(d, "sys", "kernel", "random"))
    with open(os.path.join(d, "sys", "kernel", "random", "boot_id"), "w") as f:
        f.write(boot + "\n")
    return d


class TestServiceSide(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvpw", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.dir = power_dir(os.path.join(self.d, "run-poweroff"))
        self.mono, self.clock, self.logs = Clock(), Clock(5000.0), []
        self.p = self.make()

    def make(self, **kw):
        return poweroff.PowerOff(self.dir, kw.pop("enabled", True), boot_id=kw.pop("boot_id", BOOT_A), mono=self.mono,
                                 clock=self.clock, log=lambda ev, **f: self.logs.append(ev), **kw)

    def req(self):
        return os.path.join(self.dir, poweroff.REQUEST)

    def armed(self):
        code, j = self.p.arm()
        self.assertEqual(code, 200)
        return j["challenge"]

    def test_unavailable_without_exact_dir_or_when_disabled(self):
        self.assertTrue(self.p.view()["available"])
        self.assertFalse(self.make(enabled=False).available())
        os.chmod(self.dir, 0o750)
        self.assertFalse(self.p.available())
        self.assertEqual(self.p.arm()[1]["error"], "unavailable")
        self.assertFalse(poweroff.PowerOff(os.path.join(self.d, "absent")).available())

    def test_ok_held_or_double_is_never_a_confirmation(self):
        ch = self.armed()
        self.mono.t += 0.2                                              # double OK / OK tenu : trop tôt
        code, j = self.p.confirm(ch, True)
        self.assertEqual((code, j["error"]), (409, "too_soon"))
        self.assertTrue(j["message"])                                   # la TV affiche le rappel
        self.assertFalse(os.path.exists(self.req()))
        self.assertEqual(self.p.confirm(ch, "yes")[0], 400)             # confirm: true exactement
        self.mono.t += 0.5
        code, j = self.p.confirm(ch, True)                               # OK délibéré : demande déposée
        self.assertEqual((code, j["power"]["state"]), (200, "requested"))
        doc = json.loads(read(self.req()))
        self.assertTrue(poweroff.request_ok(doc))
        self.assertEqual((doc["boot_id"], doc["at"]), (BOOT_A, 5000.0))
        self.assertEqual(stat.S_IMODE(os.stat(self.req()).st_mode), 0o644)
        self.assertEqual(self.p.confirm(ch, True)[1]["error"], "in_progress")   # jamais deux fois
        self.assertEqual(self.p.arm()[1]["error"], "in_progress")

    def test_cancel_and_wrong_or_expired_challenge(self):
        ch = self.armed()
        self.p.cancel()
        self.mono.t += 1
        self.assertEqual(self.p.confirm(ch, True)[1]["error"], "not_armed")
        ch = self.armed()
        self.mono.t += 1
        self.assertEqual(self.p.confirm("x" * 32, True)[1]["error"], "not_armed")
        self.mono.t += poweroff.ARM_TTL_S
        self.assertEqual(self.p.confirm(ch, True)[1]["error"], "expired")
        self.assertFalse(os.path.exists(self.req()))

    def confirmed(self):
        ch = self.armed()
        self.mono.t += 1
        self.assertEqual(self.p.confirm(ch, True)[0], 200)
        return json.loads(read(self.req()))

    def test_never_picked_up_is_withdrawn_recoverable_failure(self):
        self.confirmed()
        self.mono.t += poweroff.PICKUP_S
        v = self.p.view()
        self.assertEqual((v["state"], v["reason"]), ("failed", "not_started"))
        self.assertFalse(os.path.exists(self.req()))                     # retiré : root ne le prendra jamais
        self.armed()                                                      # nouvel essai possible

    def test_taken_then_result(self):
        doc = self.confirmed()
        os.unlink(self.req())                                            # pris par root
        self.assertEqual(self.p.view()["state"], "requested")
        with open(os.path.join(self.dir, poweroff.RESULT), "w") as f:
            json.dump({"v": 1, "id": doc["id"], "outcome": "accepted", "reason": None}, f)
        self.assertEqual(self.p.view()["state"], "accepted")
        self.assertFalse(os.path.exists(os.path.join(self.dir, poweroff.RESULT)))

    def test_taken_without_result_is_unknown_failure(self):
        self.confirmed()
        os.unlink(self.req())
        self.mono.t += poweroff.PICKUP_S
        self.assertEqual(self.p.view()["state"], "requested")
        self.mono.t += poweroff.RESULT_WAIT_S
        self.assertEqual(self.p.view()["reason"], "unknown")

    def test_new_service_process_never_replays_a_leftover_request(self):
        self.confirmed()
        self.make()                                                      # redémarrage du service
        self.assertFalse(os.path.exists(self.req()))
        self.assertIn("poweroff_stale_removed", self.logs)

    def test_unknown_boot_writes_nothing(self):
        p = self.make(boot_id="")
        ch = p.arm()[1]["challenge"]
        self.mono.t += 1
        self.assertEqual(p.confirm(ch, True)[1]["error"], "boot_unknown")
        self.assertFalse(os.path.exists(self.req()))


class TestRootAction(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvpwr", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.dir = power_dir(os.path.join(self.d, "run-poweroff"))
        self.uid, self.gid = os.getuid(), os.getgid()

    def put(self, **over):
        doc = dict({"v": 1, "op": "poweroff", "boot_id": BOOT_A, "at": 1000.0, "id": "a" * 32}, **over)
        p = os.path.join(self.dir, poweroff.REQUEST)
        with open(p, "w") as f:
            json.dump(doc, f)
        return p

    def run_(self, now=1001.0, boot=BOOT_A, **kw):
        return poweroff.run_action(self.dir, self.uid, self.gid, boot, now, **kw)

    def result(self):
        return json.loads(read(os.path.join(self.dir, poweroff.RESULT)))

    def test_valid_request_only_then_nothing(self):
        p = self.put()
        self.assertEqual(self.run_()["action"], "poweroff")
        self.assertFalse(os.path.exists(p))
        self.assertEqual(self.result()["outcome"], "accepted")
        self.assertEqual(self.run_()["action"], "none")                   # consommée : aucun rejeu

    def test_refusals_consume_the_request(self):
        cases = [({"at": 900.0}, {}, "stale"), ({}, {"boot": BOOT_B}, "boot"), ({"op": "reboot"}, {}, "schema"),
                 ({"cmd": "rm -rf /"}, {}, "schema"), ({}, {"enabled": False}, "disabled")]
        for over, kw, why in cases:
            with self.subTest(why=why):
                p = self.put(**over)
                self.assertEqual(self.run_(**kw), {"action": "refused", "reason": why})
                self.assertFalse(os.path.exists(p))
        marker = os.path.join(self.d, "opentv-uninstalling")
        open(marker, "w").close()
        self.put()
        self.assertEqual(self.run_(uninstalling=marker)["reason"], "uninstalling")

    def test_wrong_owner_symlink_or_dir_mode(self):
        self.put()
        self.assertEqual(poweroff.run_action(self.dir, self.uid + 1, self.gid, BOOT_A, 1001.0)["reason"], "request")
        os.symlink("/etc/passwd", os.path.join(self.dir, poweroff.REQUEST))
        self.assertEqual(self.run_()["reason"], "request")
        self.put()
        os.chmod(self.dir, 0o777)
        self.assertEqual(self.run_()["reason"], "dir")
        self.assertTrue(os.path.exists(os.path.join(self.dir, poweroff.REQUEST)))   # dossier douteux : rien touché


class TestFullChainFakeExecutor(unittest.TestCase):
    """Service réel (HTTP /tv, portes) -> demande -> faux systemd : vraie commande root (sous-processus, sans
    privilège) -> « poweroff.target » noté seulement si sortie 0. Puis rejeu après « redémarrage » refusé."""

    def setUp(self):
        self.a = make_app(self, OPENTV_X_TV="0")
        power_dir(self.a.cfg.poweroff_dir)
        self.s = Servers(self, self.a)
        self.trap, self.trap_log = trap_path(self)

    def post(self, path, body, origin=True, key=True):
        port = self.s.web.tv_port()
        h = {"Content-Type": "application/json"}
        if key:
            h["X-OpenTV-TV"] = self.a.tv_key
        if origin:
            h["Origin"] = origin if isinstance(origin, str) else "http://127.0.0.1:%d" % port
        code, _, data = self.s.req("POST", path, json.dumps(body), h)
        return code, json.loads(data) if data.startswith("{") else data

    def executor(self, boot=BOOT_A):
        """Faux systemd : PathExists -> ExecStart (même commande, sans root) ; OnSuccess -> noté, jamais exécuté."""
        if not os.path.exists(os.path.join(self.a.cfg.poweroff_dir, poweroff.REQUEST)):
            return None, []
        env = {"PATH": self.trap, "OPENTV_RUN_DIR": self.a.cfg.run_dir, "OPENTV_STATE_DIR": self.a.cfg.state_dir,
               "OPENTV_SERVICE_USER": getpass.getuser(), "LC_ALL": "C.UTF-8"}
        p = subprocess.run([sys.executable, "-I", CLI, "--env", "/nonexistent", "poweroff-action", "--proc-root",
                            tr_proc(self, boot)], env=env, capture_output=True, text=True, timeout=30)
        return p.returncode, (["poweroff.target"] if p.returncode == 0 else [])

    def test_gates_then_deliberate_ok_then_fake_systemd(self):
        st = self.a.tv_state()["power"]
        self.assertEqual((st["available"], st["state"]), (True, "idle"))
        self.assertEqual(self.post(poweroff.ARM_PATH, {}, key=False)[0], 403)
        self.assertEqual(self.post(poweroff.ARM_PATH, {}, origin=False)[0], 403)
        self.assertEqual(self.post(poweroff.ARM_PATH, {}, origin="chrome-extension://abc")[0], 403)
        code, j = self.post(poweroff.ARM_PATH, {})
        self.assertEqual(code, 200)
        self.assertEqual(self.post(poweroff.CONFIRM_PATH, {"challenge": j["challenge"], "confirm": True})[1]["error"],
                         "too_soon")
        self.assertEqual(self.executor(), (None, []))                    # rien déposé : le faux systemd ne fait rien
        self.a.power.arm_tok = (j["challenge"], self.a.power.arm_tok[1] - 1)   # OK délibéré (≥ 0,6 s plus tard)
        code, j2 = self.post(poweroff.CONFIRM_PATH, {"challenge": j["challenge"], "confirm": True})
        self.assertEqual((code, j2["power"]["state"]), (200, "requested"))
        saved = read(os.path.join(self.a.cfg.poweroff_dir, poweroff.REQUEST))
        code, jobs = self.executor()
        self.assertEqual((code, jobs), (0, ["poweroff.target"]))
        self.assertEqual(self.a.tv_state()["power"]["state"], "accepted")
        self.assertEqual(self.executor(), (None, []))                    # consommée
        # rejeu de la MÊME demande après un « redémarrage » (autre boot_id) : refus, retirée, aucun arrêt
        with open(os.path.join(self.a.cfg.poweroff_dir, poweroff.REQUEST), "w") as f:
            f.write(saved)
        self.assertEqual(self.executor(boot=BOOT_B), (2, []))
        self.assertFalse(os.path.exists(os.path.join(self.a.cfg.poweroff_dir, poweroff.REQUEST)))
        self.assertFalse(os.path.exists(self.trap_log))                  # aucune commande système tentée
        self.assertIn("poweroff_accepted", [x["event"] for x in self.a.test_logs])


def tr_proc(tc, boot):
    return proc_root(tc, boot)


class TestUnitsAndPackaging(unittest.TestCase):
    def test_root_unit_has_no_capability_command_or_install(self):
        t = read(os.path.join(PI4, "systemd", "opentv-poweroff.service"))
        for want in ("OnSuccess=poweroff.target\n", "OnSuccessJobMode=replace-irreversibly\n", "RefuseManualStart=yes",
                     "Type=oneshot", "CapabilityBoundingSet=\n", "PrivateNetwork=yes", "PrivateDevices=yes",
                     "NoNewPrivileges=yes", "ReadWritePaths=-/run/opentv-poweroff\n", "opentv-pi poweroff-action\n",
                     "InaccessiblePaths=-/etc/opentv/tmdb", "InaccessiblePaths=-/var/lib/opentv/canvas"):
            self.assertIn(want, t)
        self.assertNotIn("[Install]", t)
        self.assertNotIn("User=", t)
        execs = re.findall(r"^Exec\w*=(.*)$", t, re.M)
        self.assertEqual(execs, ["/usr/bin/python3 -I /opt/opentv/bin/opentv-pi poweroff-action"])
        p = read(os.path.join(PI4, "systemd", "opentv-poweroff.path"))
        self.assertIn("PathExists=/run/opentv-poweroff/request\n", p)
        self.assertIn("Unit=opentv-poweroff.service\n", p)
        self.assertIn("d /run/opentv-poweroff 0730 root opentv -", read(os.path.join(PI4, "system",
                                                                                     "tmpfiles-opentv.conf")))
        self.assertIn("-/run/opentv-poweroff", read(os.path.join(PI4, "systemd", "opentv.service")))
        self.assertIn("opentv-poweroff.path", health.UNITS_OPTIONAL)

    def test_no_command_cec_kiosk_or_agent_surface(self):
        src = read(os.path.join(PI4, "opentv_pi", "poweroff.py"))
        for bad in ("subprocess", "os.system", "exec", "cec", "shell"):
            self.assertNotIn(bad, src.split('"""', 2)[2].lower() if bad == "cec" else src.split('"""', 2)[2])
        for rel in ("systemd/opentv-kiosk.service", "bin/opentv-kiosk-browser", "opentv_pi/tools.py",
                    "opentv_pi/mcp.py", "opentv_pi/agentport.py", "opentv_pi/musebridge.py", "ui/remote.js",
                    "system/chromium-policy-opentv-kiosk.json"):
            self.assertNotIn("poweroff", read(os.path.join(PI4, rel)).lower(), rel)
        self.assertIn("HandlePowerKey=ignore", read(os.path.join(PI4, "system", "logind-opentv-kiosk.conf")))

    def test_install_and_uninstall_in_temp_rootfs_without_any_shutdown(self):
        trap, log = trap_path(self)
        r = fixture_root(self)
        env = dict(os.environ, LC_ALL="C", PATH=trap + ":" + os.environ.get("PATH", ""))
        env.pop("OPENTV_ROOT", None)
        p = subprocess.run(["bash", INSTALL, "--root", r, "--skip-preflight"], capture_output=True, env=env, timeout=180)
        out = p.stdout.decode() + p.stderr.decode()
        self.assertEqual(p.returncode, 0, out[-3000:])
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        for u in ("opentv-poweroff.path", "opentv-poweroff.service"):
            self.assertIn("  /etc/systemd/system/%s\n" % u, man)
        self.assertRegex(out, r"non exécuté\] systemctl enable [^\n]*opentv-poweroff\.path")
        self.assertNotRegex(out, r"(systemctl (start|restart)[^\n]*opentv-poweroff\.service|poweroff\.target)")
        self.assertFalse(os.path.exists(os.path.join(r, "run/opentv-poweroff/request")))
        p = subprocess.run(["bash", UNINSTALL, "--root", r, "--purge"], capture_output=True, env=env, timeout=180)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr).decode()[-3000:])
        for u in ("opentv-poweroff.path", "opentv-poweroff.service"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))
        self.assertFalse(os.path.exists(log), read(log) if os.path.exists(log) else "")


@unittest.skipUnless(find_chromium() and tr.NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestRealTvPage(unittest.TestCase):
    def test_remote_and_phone_drive_power_sheet(self):
        a = tr.remote_app(self, OPENTV_X_TV="0")
        from opentv_pi import config
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = tr.remote_servers(self, a, [])
        a._mdns_check()
        h = tr.TestPhoneRemoteHttp("setUp")
        h.binds = []
        host = h.host(s, tr.MDNS)
        tok = h.pair(a, s, host=host)
        d = power_dir(a.cfg.poweroff_dir)
        trap, log = trap_path(self)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvshots", dir="/tmp")
        os.makedirs(shots, exist_ok=True)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(),
               "port": s.web.remote_port(), "host": host, "cookie": tok, "csrf": h.csrf(s, tok, host=host),
               "dir": d, "python": sys.executable, "cli": CLI, "envfile": "/nonexistent",
               "proc": proc_root(self, BOOT_A), "shots": shots,
               "env": {"PATH": trap, "OPENTV_RUN_DIR": a.cfg.run_dir, "OPENTV_STATE_DIR": a.cfg.state_dir,
                       "OPENTV_SERVICE_USER": getpass.getuser(), "LC_ALL": "C.UTF-8"}}
        with mock.patch.object(poweroff, "PICKUP_S", 2.0):
            p = subprocess.run([tr.NODE, os.path.join(PI4, "tests", "e2e_poweroff.js"),
                                os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True,
                               timeout=240)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        st = {x["s"]: x for x in res["steps"]}
        self.assertEqual(res["settings_buttons"][-2:], ["Éteindre la boîte", "Fermer"])
        self.assertEqual(st["settings:power-row"]["focused"], "Éteindre la boîte")
        dbl = st["double-ok"]                                            # double OK : Annuler -> Réglages
        self.assertEqual((dbl["ptitle"], dbl["focused"], dbl["request"]), ("Réglages", "Éteindre la boîte", False))
        held = st["held-ok"]                                             # OK tenu : feuille, « Annuler » au focus
        self.assertEqual((held["ptitle"], held["focused"], held["request"]), ("Éteindre la boîte ?", "Annuler", False))
        self.assertEqual(held["labels"], ["Annuler", "Éteindre la boîte"])
        self.assertIn("Retour Annuler", held["hint"])
        self.assertEqual((st["back"]["ptitle"], st["back"]["focused"]), ("Réglages", "Éteindre la boîte"))
        fast = st["ok-too-fast"]                                         # ↓ puis OK immédiat : rappel seulement
        self.assertEqual((fast["focused"], fast["request"]), ("Éteindre la boîte", False))
        self.assertEqual(fast["pmeta"], "Pour confirmer, appuie de nouveau sur OK.")
        self.assertFalse(st["home"]["panel"])
        self.assertEqual(st["home"]["posts"].count("confirm"), 0)       # aucune confirmation envoyée jusque-là
        self.assertTrue(all(c == 200 for c in res["phone"]), res["phone"])
        self.assertEqual(st["phone:sheet"]["focused"], "Annuler")
        rq = st["phone:requested"]                                       # téléphone : OK délibéré
        self.assertTrue(rq["overlay"] and rq["ovtop"] and not rq["offline"], rq)
        self.assertIn("Ne débranche pas encore", rq["ovmsg"])
        self.assertTrue(st["phone:keys-ignored"]["overlay"])
        fl = st["phone:failed"]                                          # jamais prise : échec récupérable
        self.assertFalse(fl["overlay"])
        self.assertIn("la boîte reste allumée", fl["toast"])
        self.assertEqual((fl["ptitle"], fl["focused"], fl["request"]), ("Réglages", "Éteindre la boîte", False))
        acc = st["remote:accepted"]                                      # télécommande + faux systemd
        self.assertTrue(acc["ovmsg"].startswith("Arrêt en cours. Ne débranche pas encore"), acc)
        gone = st["remote:service-gone"]
        self.assertTrue(gone["overlay"] and not gone["offline"] and not gone["panel"], gone)
        self.assertEqual([r["code"] for r in res["executor"]["runs"]], [0])
        self.assertEqual(len(res["executor"]["jobs"]), 1)
        self.assertEqual(a.power.view()["state"], "accepted")
        self.assertFalse(os.path.exists(log))
        for name in res["shots"]:
            self.assertGreater(os.path.getsize(os.path.join(shots, name)), 10000, name)


if __name__ == "__main__":
    unittest.main()
