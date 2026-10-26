"""Durcissement avant revue qualité : wifi-apply jamais bloqué (échéance persistée, résultat tardif ignoré), budget
réel du helper sous TimeoutStartSec, garde root du Wi-Fi temporaire (service mort, configuration invalide, budget,
désinstallation), boot_id illisible fail-closed, GET /setup trop large, sonde /tv à attente croissante, détection
apt sans pipe."""
import configparser
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

from tests._common import PI4, SECRET_PSK
from tests.test_service import make_app, Servers, Net, ETH_DEV, ETH_IP, OFF_DEV, AP_DEV, AP_IP, find_chromium, token_of
from tests.test_compliance import proc_root, set_boot_id, drain
from tests.test_install import INSTALL, UNINSTALL, PREPARE, sh, tree
from opentv_pi import apguard, app as appmod, cli, config, netjobs, nethelper, nmkeyfile

AP_PROFILE = "etc/NetworkManager/system-connections/opentv-setup-ap.nmconnection"


def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def jobs(a):
    out = []
    for n in sorted(os.listdir(a.cfg.jobs_dir)):
        with open(os.path.join(a.cfg.jobs_dir, n), encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def put_result(a, jid, action, ok, err=None):
    with open(os.path.join(a.cfg.results_dir, jid + ".json"), "w") as f:
        json.dump({"v": 1, "id": jid, "action": action, "ok": ok, "error": err}, f)


def submit_wifi(a, tok=None):
    return a.setup_submit({"t": tok or token_of(a), "name": "Salon", "net": "wifi", "ssid": "Maison",
                           "psk": SECRET_PSK})


# --------------------------------------------------------------------------- 1. wifi-apply borné
class TestApplyDeadline(unittest.TestCase):
    def test_pending_apply_without_result_fails_honestly_and_late_result_is_ignored(self):
        a = make_app(self, Net(ETH_DEV, ETH_IP))
        a.tick()
        tok = token_of(a)
        self.assertEqual(submit_wifi(a, tok)[:2], (200, "applying"))
        jid = jobs(a)[0]["id"]
        t0 = a.mono_clock.t
        phases = []
        while a.mono_clock.t < t0 + 2000:                    # le helper ne répond jamais
            a.mono_clock.t += 2
            a.tick()
            phases.append((a.mono_clock.t - t0, a.tv_state()["setup"]["phase"]))
        stuck = [dt for dt, ph in phases if ph == "applying"]
        self.assertTrue(stuck)
        self.assertLessEqual(max(stuck), appmod.APPLY_DEADLINE_S)   # jamais « applying » au-delà de l'échéance
        self.assertLess(appmod.APPLY_DEADLINE_S, 300)
        st = a.tv_state()["setup"]
        self.assertEqual(st["phase"], "error")
        self.assertIn("à temps", st["message"])
        self.assertTrue(st["token"] and st["token"] != tok)        # nouveau QR
        self.assertTrue(st["url"].startswith("http://192.168.2.40:"))
        self.assertEqual(os.listdir(a.cfg.jobs_dir), [])           # job encore en file retiré : jamais exécuté
        self.assertFalse(os.path.exists(a.cfg.apply_state))
        self.assertTrue(any(e["event"] == "apply_timeout" and e["withdrawn"] for e in a.test_logs))
        # résultat tardif (réussi !) de l'ancien job : ignoré, la configuration reste ouverte
        put_result(a, jid, "wifi-apply", True)
        a.tick()
        st = a.tv_state()["setup"]
        self.assertTrue(st["active"])
        self.assertEqual(st["phase"], "error")
        self.assertTrue(any(e["event"] == "net_job_result_ignored" and e["reason"] == "stale" for e in a.test_logs))
        # nouvelle tentative normale : son propre résultat est appliqué
        self.assertEqual(submit_wifi(a)[1], "applying")
        jid2 = jobs(a)[0]["id"]
        os.remove(os.path.join(a.cfg.jobs_dir, jid2 + ".json"))
        put_result(a, jid, "wifi-apply", True)                      # encore l'ancien : ignoré
        a.tick()
        self.assertEqual(a.tv_state()["setup"]["phase"], "applying")
        put_result(a, jid2, "wifi-apply", True)
        a.tick()
        self.assertFalse(a.tv_state()["setup"]["active"])
        self.assertNotIn("Tr0p", json.dumps(a.test_logs) + json.dumps(a.tv_state()))

    def test_hotspot_apply_timeout_rearms_only_within_budget(self):
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=proc_root(self), OPENTV_HOTSPOT_MAX_S="1200")
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        first = a.mono_clock.t
        seen = drain(a, net)
        self.assertEqual(seen, ["ap-start"])
        a.tick()
        self.assertEqual(submit_wifi(a)[1], "applying")
        job = jobs(a)[0]
        self.assertTrue(job["payload"]["restore_ap"])
        jid = job["id"]
        os.remove(os.path.join(a.cfg.jobs_dir, jid + ".json"))      # pris par le helper… qui reste bloqué
        net.dev, net.ip = OFF_DEV, ""                               # AP coupé par le helper
        t0 = a.mono_clock.t
        stop_at = None
        while a.mono_clock.t < t0 + 2000:
            a.mono_clock.t += 10
            a.tick()
            for x in drain(a, net):
                seen.append(x)
                if x == "ap-stop":
                    stop_at = a.mono_clock.t
        # échéance ⇒ hotspot relancé (budget restant), puis fermé à l'échéance du budget, jamais rouvert
        self.assertEqual(seen, ["ap-start", "ap-start", "ap-stop"], seen)
        self.assertLess(stop_at - first, 1200 + 20)
        self.assertNotIn("opentv-setup-ap", net.dev)
        st = a.tv_state()["setup"]
        self.assertEqual((st["phase"], st["hotspot"]), ("error", None))
        self.assertIn("redémarre", st["message"])
        put_result(a, jid, "wifi-apply", False, "wifi_connect_failed")   # échec tardif de l'ancien job : ignoré
        a.tick()
        a.mono_clock.t += 600
        a.tick()
        self.assertEqual(drain(a, net), [])

    def test_service_restart_keeps_deadline_and_other_boot_abandons(self):
        pr = proc_root(self)
        a = make_app(self, Net(ETH_DEV, ETH_IP), proc_root=pr)
        a.tick()
        submit_wifi(a)
        jid = jobs(a)[0]["id"]
        os.remove(os.path.join(a.cfg.jobs_dir, jid + ".json"))
        a.mono_clock.t += 100
        # redémarrage du service, même démarrage de la boîte : reprise avec l'échéance d'origine
        b = make_app(self, Net(ETH_DEV, ETH_IP), proc_root=pr, OPENTV_STATE_DIR=a.cfg.state_dir)
        b.mono_clock.t = a.mono_clock.t
        b._apply_load()                                     # mono de test fixé après construction
        self.assertEqual(b.tv_state()["setup"]["phase"], "applying")
        b.mono_clock.t += appmod.APPLY_DEADLINE_S - 100 + 1
        b.tick()
        self.assertEqual(b.tv_state()["setup"]["phase"], "error")
        # même scénario, autre démarrage : abandonné tout de suite, résultat tardif ignoré
        c = make_app(self, Net(ETH_DEV, ETH_IP), proc_root=pr)
        c.tick()
        submit_wifi(c)
        jid = jobs(c)[0]["id"]
        set_boot_id(pr, "99999999-2222-3333-4444-555555555555")
        d = make_app(self, Net(ETH_DEV, ETH_IP), proc_root=pr, OPENTV_STATE_DIR=c.cfg.state_dir)
        self.assertEqual(d.tv_state()["setup"]["phase"], "error")
        self.assertEqual(os.listdir(d.cfg.jobs_dir), [])
        put_result(d, jid, "wifi-apply", True)
        d.tick()
        self.assertTrue(d.tv_state()["setup"]["active"])

    def test_helper_refuses_wifi_apply_picked_up_too_late(self):
        e = tempfile.mkdtemp(prefix="otvh", dir="/tmp")
        self.addCleanup(shutil.rmtree, e, True)
        dirs = [os.path.join(e, n) for n in ("jobs", "tmp", "res", "nm")]
        for x in dirs:
            os.makedirs(x, mode=0o700)
        now = [10000.0]
        netjobs.submit(dirs[0], dirs[1], "wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK},
                       clock=lambda: now[0] - nethelper.WIFI_APPLY_PICKUP_MAX_S - 1)
        calls = []
        h = nethelper.Helper(dirs[0], dirs[2], dirs[3], os.getuid(), runner=lambda a, t: calls.append(a) or (0, ""),
                             clock=lambda: now[0], sleep=lambda s: None, log=lambda *a, **k: None, chown=False)
        res = h.run_once()
        self.assertEqual((res[0]["ok"], res[0]["error"]), (False, "expired"))
        self.assertEqual(calls, [])
        self.assertLess(nethelper.WIFI_APPLY_PICKUP_MAX_S + nethelper.RUN_BUDGET_S, appmod.APPLY_DEADLINE_S)


# --------------------------------------------------------------------------- 2. budget réel du helper
class HangingNM:
    """Chaque nmcli « bloque » jusqu'à son échéance : l'horloge simulée avance du délai + mise à mort du groupe."""

    def __init__(self):
        self.t, self.calls = 1000.0, []

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        self.t += timeout + 2 * nethelper.KILL_GRACE_S
        return 124, ""


class TestHelperBudget(unittest.TestCase):
    def env(self):
        e = tempfile.mkdtemp(prefix="otvh", dir="/tmp")
        self.addCleanup(shutil.rmtree, e, True)
        dirs = [os.path.join(e, n) for n in ("jobs", "tmp", "res", "nm")]
        for x in dirs:
            os.makedirs(x, mode=0o700)
        nmkeyfile.write_profile(dirs[3], nmkeyfile.AP_ID, nmkeyfile.ap_profile("OpenTV-AB12", "abcdefgh2345"))
        return dirs

    def test_constants_fit_systemd_timeout(self):
        ts = float(unit("opentv-net-helper.service")["Service"]["TimeoutStartSec"])
        self.assertEqual(ts, nethelper.UNIT_TIMEOUT_START_S)
        self.assertLessEqual(nethelper.RUN_BUDGET_S + 60, ts)             # marge nette (démarrage Python, journal)
        self.assertLessEqual(nethelper.WIFI_APPLY_WORST_S, nethelper.RUN_BUDGET_S + nethelper.MIN_JOB_BUDGET_S)

    def test_every_nmcli_hanging_stays_within_budget_and_fails_cleanly(self):
        jd, tmp, rd, nm = self.env()
        clock = time.time
        for _ in range(16):
            netjobs.submit(jd, tmp, "wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK, "delay_s": 10,
                                                   "restore_ap": True})
        sim, logs = HangingNM(), []
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=sim, clock=clock, sleep=sim.sleep, mono=sim.mono,
                             log=lambda ev, **f: logs.append(dict(f, event=ev)), chown=False)
        t0 = sim.t
        res = h.run_once()
        elapsed = sim.t - t0
        self.assertLessEqual(elapsed, nethelper.RUN_BUDGET_S)
        self.assertLess(elapsed + 30, float(unit("opentv-net-helper.service")["Service"]["TimeoutStartSec"]))
        self.assertGreaterEqual(len(res), 1)
        self.assertTrue(all(r["ok"] is False and r["error"] == "wifi_connect_failed" for r in res), res)
        self.assertGreater(len(os.listdir(jd)), 0)                     # le reste attend l'activation suivante
        self.assertTrue(any(e["event"] == "net_job_deferred" for e in logs))
        for argv, timeout in sim.calls:
            self.assertEqual(argv[0], "nmcli")
            if "--wait" in argv:
                self.assertLessEqual(int(argv[argv.index("--wait") + 1]) + nethelper.NM_GRACE_S, timeout + 1e-9, argv)
        blob = json.dumps(res) + json.dumps(logs)
        for n in os.listdir(rd):
            with open(os.path.join(rd, n)) as f:
                blob += f.read()
        self.assertNotIn("Tr0p", blob)
        self.assertNotIn("Maison", blob)
        self.assertFalse(os.path.exists(os.path.join(nm, "opentv-wifi.nmconnection")))   # identifiants non conservés

    def test_argv_timeouts_sum_below_budget(self):
        jd, tmp, rd, nm = self.env()
        netjobs.submit(jd, tmp, "wifi-apply", {"ssid": "Maison", "psk": "12345678", "delay_s": 10, "restore_ap": True})
        seen = []

        def runner(argv, timeout):
            seen.append((argv, timeout))
            return (4, "") if "uuid" in argv and "up" in argv else (0, "")   # candidat refusé
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=runner, sleep=lambda s: None, log=lambda *a, **k: None,
                             chown=False)
        self.assertFalse(h.run_once()[0]["ok"])
        waits = {a[a.index("connection") + 1] + ":" + (a[-1] if a[-2] == "id" else "candidate"): int(a[2])
                 for a, _ in seen if a[1] == "--wait"}
        self.assertEqual(waits, {"down:opentv-setup-ap": nethelper.AP_DOWN_WAIT_S,
                                 "up:candidate": nethelper.WIFI_UP_WAIT_S,
                                 "up:opentv-setup-ap": nethelper.AP_UP_WAIT_S})
        worst = 10 + sum(t + 2 * nethelper.KILL_GRACE_S for _, t in seen)
        self.assertLessEqual(worst, nethelper.WIFI_APPLY_WORST_S)
        self.assertLess(nethelper.WIFI_APPLY_WORST_S, nethelper.UNIT_TIMEOUT_START_S - 30)

    def test_real_runner_kills_hung_children_within_budget(self):
        jd, tmp, rd, nm = self.env()
        netjobs.submit(jd, tmp, "wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK, "restore_ap": True})

        def runner(argv, timeout):   # vrai runner (groupe tué) sur une commande qui ne rend jamais la main
            return nethelper.default_runner(["/bin/sh", "-c", "sleep 30 & sleep 30"], timeout)
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=runner, log=lambda *a, **k: None, chown=False,
                             budget_s=4.0, min_job_s=1.0)
        t0 = time.monotonic()
        res = h.run_once()
        self.assertLess(time.monotonic() - t0, 4.0 + 2 * nethelper.KILL_GRACE_S + 1.0)
        self.assertEqual((res[0]["ok"], res[0]["error"]), (False, "wifi_connect_failed"))


# --------------------------------------------------------------------------- 3. garde root du Wi-Fi temporaire
class FakeNM:
    """NetworkManager + systemctl simulés : `opentv-setup-ap` actif et/ou profil keyfile présent."""

    def __init__(self, nm_dir, service="active", ap=True, hang=False):
        self.nm_dir, self.service, self.ap, self.hang = nm_dir, service, ap, hang
        self.calls, self.t = [], 500.0

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.hang:
            self.t += timeout + 2 * nethelper.KILL_GRACE_S
            return 124, ""
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if self.service == "active" else 3), self.service + "\n"
        if argv[:2] == ["nmcli", "-t"]:
            return 0, AP_DEV if self.ap else OFF_DEV
        if argv[:2] == ["ip", "-4"]:
            return 0, AP_IP if self.ap else ""
        if argv[0] == "nmcli" and "down" in argv and argv[-1] == "opentv-setup-ap":
            self.ap = False
        return 0, ""

    def mutations(self):
        return [c for c in self.calls if c[0] == "nmcli" and c[1] != "-t"]


class TestApGuard(unittest.TestCase):
    def setup_env(self, boot="11111111-2222-3333-4444-555555555555", env_extra="", profile=True):
        d = tempfile.mkdtemp(prefix="otvg", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        state, nm = os.path.join(d, "state"), os.path.join(d, "nm")
        os.makedirs(state)
        os.makedirs(nm)
        pr = os.path.join(d, "proc")
        if boot is not None:
            set_boot_id(pr, boot)
        else:
            os.makedirs(pr)
        envf = os.path.join(d, "opentv.env")
        with open(envf, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_NM_DIR=%s\nOPENTV_HOTSPOT_MAX_S=1200\n%s" % (state, nm, env_extra))
        if profile:
            nmkeyfile.write_profile(nm, nmkeyfile.AP_ID, nmkeyfile.ap_profile("OpenTV-AB12", "abcdefgh2345"))
        nmkeyfile.write_profile(nm, nmkeyfile.WIFI_ID, nmkeyfile.wifi_profile("Maison", "12345678"))
        return {"env": envf, "state": state, "nm": nm, "proc": pr}

    def budget(self, e, first_start, expired=False, boot="11111111-2222-3333-4444-555555555555"):
        with open(os.path.join(e["state"], "hotspot.json"), "w") as f:
            json.dump({"v": 1, "boot_id": boot, "first_start": first_start, "expired": expired}, f)

    def guard(self, e, nm, now=1000.0, force=False):
        logs = []
        r = apguard.run(e["env"], runner=nm, mono=lambda: now, proc_root=e["proc"], force=force,
                        log=lambda ev, **f: logs.append(dict(f, event=ev)), environ={})
        return r, logs

    def assert_stopped(self, e, nm, r, reason):
        self.assertEqual((r["action"], r.get("reason")), ("stopped", reason), r)
        self.assertFalse(nm.ap)
        self.assertFalse(os.path.exists(os.path.join(e["nm"], "opentv-setup-ap.nmconnection")))
        self.assertTrue(os.path.exists(os.path.join(e["nm"], "opentv-wifi.nmconnection")))   # jamais touché
        self.assertIn(["nmcli", "connection", "delete", "id", "opentv-setup-ap"], nm.mutations())
        for c in nm.calls:
            self.assertIn(c[0], ("nmcli", "ip", "systemctl"))

    def test_service_not_active_stops_ap(self):
        for state in ("inactive", "failed", "activating", "deactivating", "garbage"):
            e = self.setup_env()
            self.budget(e, 900.0)
            nm = FakeNM(e["nm"], service=state)
            r, _ = self.guard(e, nm)
            self.assert_stopped(e, nm, r, "service_" + (state if state != "garbage" else "unknown"))

    def test_active_service_within_budget_keeps_ap(self):
        e = self.setup_env()
        self.budget(e, 900.0)
        nm = FakeNM(e["nm"])
        r, _ = self.guard(e, nm, now=900.0 + 1200 + apguard.GUARD_SLACK_S - 1)
        self.assertEqual(r["action"], "none")
        self.assertEqual(nm.mutations(), [])
        self.assertTrue(nm.ap)

    def test_fail_closed_reasons(self):
        cases = [
            ("ttl", dict(), lambda e: self.budget(e, 900.0), 900.0 + 1200 + apguard.GUARD_SLACK_S),
            ("expired", dict(), lambda e: self.budget(e, 900.0, expired=True), 950.0),
            ("clock", dict(), lambda e: self.budget(e, 5000.0), 1000.0),           # durée négative
            ("no_budget", dict(), lambda e: None, 1000.0),
            ("no_budget", dict(), lambda e: self.budget(e, 900.0, boot="99999999-2222-3333-4444-555555555555"), 950.0),
            ("boot_id_unknown", dict(boot=None), lambda e: self.budget(e, 900.0, boot=""), 950.0),
            ("boot_id_unknown", dict(boot="../../etc/passwd"), lambda e: self.budget(e, 900.0, boot=""), 950.0),
            ("config_invalid", dict(env_extra="OPENTV_HOTSPOT_MAX_S=abc\n"), lambda e: self.budget(e, 900.0), 950.0),
        ]
        for reason, kw, prep, now in cases:
            with self.subTest(reason=reason, kw=kw):
                e = self.setup_env(**kw)
                prep(e)
                nm = FakeNM(e["nm"])
                r, logs = self.guard(e, nm, now=now)
                if reason == "config_invalid":   # chemins par défaut (configuration illisible) : AP actif arrêté
                    self.assertEqual((r["action"], r["reason"]), ("stopped", reason))
                    self.assertFalse(nm.ap)
                else:
                    self.assert_stopped(e, nm, r, reason)
                self.assertEqual(logs[-1]["event"], "ap_guard_stop")

    def test_leftover_profile_without_active_ap_is_removed(self):
        e = self.setup_env()
        nm = FakeNM(e["nm"], service="inactive", ap=False)
        r, _ = self.guard(e, nm)
        self.assertEqual(r["action"], "stopped")
        self.assertFalse(os.path.exists(os.path.join(e["nm"], "opentv-setup-ap.nmconnection")))

    def test_no_ap_means_no_action_at_all(self):
        e = self.setup_env(profile=False)
        nm = FakeNM(e["nm"], service="failed", ap=False)
        r, _ = self.guard(e, nm)
        self.assertEqual(r, {"ok": True, "ap": False, "action": "none"})
        self.assertEqual(nm.mutations(), [])
        self.assertFalse(any(c[0] == "systemctl" for c in nm.calls))

    def test_force_stop_and_budget_of_hung_nmcli(self):
        e = self.setup_env(profile=False)
        nm = FakeNM(e["nm"], ap=False, hang=True)
        t0 = nm.t
        r = apguard.run(e["env"], runner=nm, mono=lambda: nm.t, proc_root=e["proc"], force=True,
                        log=lambda *a, **k: None, environ={})
        self.assertEqual((r["action"], r["reason"]), ("stopped", "forced"))
        self.assertLessEqual(nm.t - t0, apguard.GUARD_BUDGET_S + netjobs_snapshot_budget())
        ts = float(unit("opentv-ap-guard.service")["Service"]["TimeoutStartSec"])
        self.assertEqual(ts, apguard.UNIT_TIMEOUT_START_S)
        self.assertLessEqual(apguard.GUARD_BUDGET_S + netjobs_snapshot_budget() + apguard.SYSTEMCTL_TIMEOUT_S + 5, ts)

    def test_service_death_simulation_no_ap_left(self):
        """App réelle : AP lancé, puis le service meurt (plus aucun tick). La garde seule le ferme, une seule fois,
        et aucun AP ne réapparaît pendant 5 jours de passages toutes les 30 s."""
        pr = proc_root(self)
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr)
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        self.assertEqual(drain(a, net), ["ap-start"])
        a.tick()
        envf = os.path.join(a.cfg.state_dir, "..", "g.env")
        nmd = os.path.join(a.cfg.state_dir, "..", "nm")
        os.makedirs(nmd)
        nmkeyfile.write_profile(nmd, nmkeyfile.AP_ID, nmkeyfile.ap_profile("OpenTV-AB12", "abcdefgh2345"))
        with open(envf, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_NM_DIR=%s\n" % (a.cfg.state_dir, nmd))
        fake = FakeNM(nmd, service="active")
        e = {"env": envf, "proc": pr}
        r, _ = self.guard(e, fake, now=a.mono_clock.t)
        self.assertEqual(r["action"], "none")                 # service vivant, budget valide : AP légitime
        fake.service = "failed"                                # opentv.service meurt
        stops = 0
        for k in range(5 * 86400 // 30):
            r, _ = self.guard(e, fake, now=a.mono_clock.t + 30 * k)
            stops += r["action"] == "stopped"
        self.assertEqual(stops, 1)
        self.assertFalse(fake.ap)
        self.assertFalse(os.path.exists(os.path.join(nmd, "opentv-setup-ap.nmconnection")))

    def test_rearm_while_ap_broadcasts_gives_guard_a_fresh_budget(self):
        pr = proc_root(self)
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr)
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        drain(a, net)
        a.tick()
        a.mono_clock.t += 500
        a.request_setup()                                     # « Reconfigurer » pendant que l'AP diffuse
        with open(a.cfg.hotspot_state) as f:
            hs = json.load(f)
        self.assertEqual((hs["first_start"], hs["expired"]), (a.mono_clock.t, False))
        self.assertIsNone(apguard.decide("active", True, a.boot_id, hs, a.mono_clock.t + 1, a.cfg.hotspot_max_s))

    def test_cli_requires_root(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["ap-guard"]), 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "root_required")

    def test_units_are_strict_oneshot_and_timer(self):
        svc = unit("opentv-ap-guard.service")["Service"]
        self.assertEqual(svc["Type"], "oneshot")
        self.assertEqual(svc["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi ap-guard")
        self.assertNotIn("User", svc)                          # root (nmcli) ; durci, sans shell
        self.assertEqual(svc["NoNewPrivileges"], "yes")
        self.assertEqual(svc["ReadWritePaths"], "/etc/NetworkManager/system-connections")
        tm = unit("opentv-ap-guard.timer")
        self.assertEqual(tm["Timer"]["Unit"], "opentv-ap-guard.service")
        self.assertLessEqual(int(tm["Timer"]["OnUnitActiveSec"].rstrip("s")), 60)
        self.assertLessEqual(int(tm["Timer"]["OnBootSec"].rstrip("s")), 60)
        self.assertEqual(tm["Install"]["WantedBy"], "timers.target")


def netjobs_snapshot_budget():
    from opentv_pi import netstate
    return netstate.NMCLI_TIMEOUT_S + netstate.IP_TIMEOUT_S + 4 * nethelper.KILL_GRACE_S


class TestGuardInstallUninstall(unittest.TestCase):
    def root(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        return r

    def with_ap(self, r):
        nm = os.path.join(r, "etc/NetworkManager/system-connections")
        os.makedirs(nm, exist_ok=True)
        for n in ("opentv-setup-ap.nmconnection", "opentv-wifi.nmconnection", "Maison.nmconnection",
                  "opentv-wifi-candidate.nmconnection"):   # candidat d'un wifi-apply interrompu : retiré aussi
            with open(os.path.join(nm, n), "w") as f:
                f.write("[connection]\n")
        return nm

    def test_install_manifest_and_enable_include_guard(self):
        r = self.root()
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            m = f.read()
        for u in ("opentv-ap-guard.service", "opentv-ap-guard.timer"):
            self.assertIn("/etc/systemd/system/" + u, m)
        self.assertIn("/opt/opentv/lib/opentv_pi/apguard.py", m)
        self.assertRegex(p.stdout.decode(), r"systemctl enable [^\n]*opentv-ap-guard\.timer")

    def test_normal_uninstall_stops_ap_before_removing_files(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        nm = self.with_ap(r)
        p = sh([UNINSTALL, "--root", r])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(nm)), ["Maison.nmconnection", "opentv-wifi.nmconnection"])
        i_disable = out.index("systemctl disable --now")
        self.assertIn("opentv-ap-guard.timer", out[i_disable:out.index("\n", i_disable)])
        i_down = out.index("nmcli --wait 10 connection down id opentv-setup-ap")
        i_del = out.index("nmcli connection delete id opentv-setup-ap")
        i_done = out.index("Wi-Fi temporaire OpenTV (opentv-setup-ap) arrêté et supprimé")
        self.assertLess(i_disable, i_down)
        self.assertLess(i_down, i_del)
        self.assertLess(i_done, out.index("systemctl daemon-reload"))
        for u in ("opentv-ap-guard.service", "opentv-ap-guard.timer"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))

    def test_purge_also_stops_ap_and_dry_run_touches_nothing(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        nm = self.with_ap(r)
        before = tree(r)
        p = sh([UNINSTALL, "--root", r, "--purge", "--dry-run"])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(tree(r), before)
        self.assertIn("nmcli --wait 10 connection down id opentv-setup-ap", p.stdout.decode())
        p = sh([UNINSTALL, "--root", r, "--purge"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("nmcli --wait 10 connection down id opentv-setup-ap", p.stdout.decode())
        self.assertEqual(sorted(os.listdir(nm)), ["Maison.nmconnection", "opentv-wifi.nmconnection"])

    def test_failed_install_rolls_back_guard_units(self):
        r = self.root()
        os.makedirs(os.path.join(r, "etc/pam.d"))
        os.chmod(os.path.join(r, "etc/pam.d"), 0o555)
        self.addCleanup(os.chmod, os.path.join(r, "etc/pam.d"), 0o755)
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        for u in ("opentv-ap-guard.service", "opentv-ap-guard.timer"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))


# --------------------------------------------------------------------------- 4. boot_id illisible
class TestBootIdUnknown(unittest.TestCase):
    def empty_proc(self):
        d = tempfile.mkdtemp(prefix="otvproc", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_no_automatic_hotspot_and_honest_message(self):
        pr = self.empty_proc()
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr)
        # état d'un démarrage précédent (horloge monotone plus grande) : ne doit jamais servir de budget
        with open(a.cfg.hotspot_state, "w") as f:
            json.dump({"v": 1, "boot_id": "", "first_start": 1e9, "expired": False}, f)
        a = make_app(self, net, proc_root=pr, OPENTV_STATE_DIR=a.cfg.state_dir)
        for _ in range(100):
            a.mono_clock.t += 60
            a.tick()
        self.assertEqual(drain(a, net), [])
        st = a.tv_state()["setup"]
        self.assertIsNone(st["hotspot"])
        self.assertIn("non identifiable", st["message"])
        self.assertIn("Ethernet", st["message"])
        a.request_setup()                                    # « Reconfigurer » ne rouvre pas d'AP non bornable
        a.mono_clock.t += 120
        a.tick()
        self.assertEqual(drain(a, net), [])

    def test_malformed_boot_id_is_unknown_and_orphan_ap_is_stopped(self):
        for bad in ("", "../../x", "11111111-2222-3333-4444-55555555555Z", "a" * 64):
            pr = self.empty_proc()
            set_boot_id(pr, bad)
            self.assertEqual(appmod._boot_id(pr), "")
        net = Net(AP_DEV, AP_IP)
        a = make_app(self, net, proc_root=pr)
        a.tick()
        self.assertEqual(drain(a, net), ["ap-stop"])

    def test_negative_elapsed_expires_never_extends(self):
        pr = proc_root(self)
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr)
        with open(a.cfg.hotspot_state, "w") as f:   # même boot_id mais premier démarrage « dans le futur »
            json.dump({"v": 1, "boot_id": "11111111-2222-3333-4444-555555555555", "first_start": 1e6,
                       "expired": False}, f)
        a = make_app(self, net, proc_root=pr, OPENTV_STATE_DIR=a.cfg.state_dir)
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        self.assertEqual(drain(a, net), [])
        self.assertTrue(a.hs["expired"])


# --------------------------------------------------------------------------- 5. GET /setup trop large
class TestSetupQueryTooWide(unittest.TestCase):
    def test_bounded_400_without_trace_and_token_kept(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        q = "&".join("x%d=1" % i for i in range(50)) + "&t=" + tok
        for path in ("/setup?" + q, "/setup?a=1&b=2&c=3&d=4&e=5", "/setup?" + "&" * 10):
            code, h, body = s.req("GET", path, lan=True)
            self.assertEqual(code, 400, path)
            self.assertIn("Adresse invalide", body)
            self.assertNotIn("Traceback", body)
            self.assertLess(len(body), 2048)
            self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertEqual(s.req("GET", "/setup?t=" + tok, lan=True)[0], 200)
        code, _, _ = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200)
        self.assertNotIn(tok, json.dumps(a.test_logs))


# --------------------------------------------------------------------------- 6. sonde /tv à attente croissante
@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestTvProbeBackoff(unittest.TestCase):
    def test_failing_probe_backs_off(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = Servers(self, a)
        orig = a.tv_state

        def tv_state():   # le service « redémarre » : clé tournée, et /tv n'est pas encore servi (500)
            a.tv_key = "k" * 43
            a.cfg.share_dir = os.path.join(a.cfg.state_dir, "absent")
            return orig()
        a.tv_state = tv_state
        prof = tempfile.mkdtemp(prefix="otvchr", dir="/tmp")
        self.addCleanup(shutil.rmtree, prof, True)
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + prof,
                            "--virtual-time-budget=60000", "--window-size=1920,1080", "--dump-dom",
                            "http://127.0.0.1:%d/tv" % s.web.tv_port()], capture_output=True, timeout=180)
        dom = p.stdout.decode("utf-8", "replace")
        probes = [e for e in a.test_logs if e.get("event") == "http" and e.get("path") == "/tv"
                  and e.get("status") == 500]
        api = [e for e in a.test_logs if e.get("event") == "http" and e.get("path") == "/api/tv/state"]
        self.assertGreaterEqual(len(probes), 2, dom[-400:])     # la reprise continue…
        self.assertLessEqual(len(probes), 6, len(probes))      # … à attente croissante (2, 4, 8, 16, 32 s)
        self.assertGreater(len(api), len(probes))
        self.assertIn('data-reloads="0"', dom)
        self.assertIn("Reconnexion", dom)


# --------------------------------------------------------------------------- 7. détection apt sans pipe
class TestPrepareCandidate(unittest.TestCase):
    def run_with(self, script):
        d = tempfile.mkdtemp(prefix="otvapt", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "apt-cache")
        with open(fake, "w") as f:
            f.write("#!/bin/bash\n" + script)
        os.chmod(fake, 0o755)
        code = ('set -Eeuo pipefail; source "$1"; if has_candidate chromium; then echo yes; else echo no; fi; '
                'type has_candidate >/dev/null')
        p = subprocess.run(["bash", "-c", code, "x", PREPARE], capture_output=True, timeout=30,
                           env=dict(os.environ, PATH=d + ":" + os.environ["PATH"]))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode().strip()

    def test_detection(self):
        ok = 'printf "chromium:\\n  Installed: (none)\\n  Candidate: 1:130.0-1\\n"\n'
        self.assertEqual(self.run_with(ok), "yes")
        self.assertEqual(self.run_with('printf "chromium:\\n  Installed: (none)\\n  Candidate: (none)\\n"\n'), "no")
        self.assertEqual(self.run_with("exit 0\n"), "no")
        self.assertEqual(self.run_with('echo "E: boom" >&2; exit 100\n'), "no")
        # sortie volumineuse : l'ancien « | grep -q » sous pipefail pouvait échouer par SIGPIPE (faux négatif)
        big = ok + 'for i in $(seq 1 20000); do echo "     500 http://archive.raspberrypi.com/debian bookworm/main"; done\n'
        self.assertEqual(self.run_with(big), "yes")

    def test_sourcing_runs_nothing_and_no_pipe_grep(self):
        p = subprocess.run(["bash", "-c", 'source "$1"; echo sourced', "x", PREPARE], capture_output=True, timeout=30)
        self.assertEqual(p.stdout.decode().strip(), "sourced")
        with open(PREPARE) as f:
            text = f.read()
        code = "\n".join(re.sub(r"(^|\s)#.*$", "", l) for l in text.splitlines())   # commentaires retirés
        self.assertIn("apt-cache policy", code)
        self.assertNotRegex(code, r"apt-cache[^\n]*[^|]\|[^|]")      # aucun pipe (« || » autorisé)


if __name__ == "__main__":
    unittest.main()
