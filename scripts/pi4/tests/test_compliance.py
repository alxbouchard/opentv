"""Régressions de la revue de conformité Pi 4 : reprise de la page TV après redémarrage du service, hotspot borné et
fail-closed, politique d'agents explicite, préparation stricte avant apt, mise à jour/rollback d'install.sh, jobs
réseau cachés, watchdog borné, jeton QR non consommé par GET, verrouillage Agent Port par UID."""
import configparser
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests._common import PI4
from tests.test_service import (make_app, Servers, Net, ETH_DEV, ETH_IP, OFF_DEV, AP_DEV, AP_IP, fixture_root,
                                find_chromium, token_of)
from tests.test_install import INSTALL, UNINSTALL, PREPARE, sh, tree
from opentv_pi import agents, agentport, cli, config, netjobs, nethelper, netstate

ETH_AP_DEV = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:connected:opentv-setup-ap\n"
ETH_AP_IP = ETH_IP + AP_IP


def proc_root(tc, boot_id="11111111-2222-3333-4444-555555555555"):
    d = tempfile.mkdtemp(prefix="otvproc", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    set_boot_id(d, boot_id)
    return d


def set_boot_id(d, boot_id):
    p = os.path.join(d, "sys", "kernel", "random")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "boot_id"), "w") as f:
        f.write(boot_id + "\n")


def drain(a, net=None, ok=True):
    """Simule le helper root : consomme les jobs, dépose un résultat, met à jour l'état NetworkManager simulé."""
    actions = []
    for n in sorted(os.listdir(a.cfg.jobs_dir)):
        p = os.path.join(a.cfg.jobs_dir, n)
        with open(p, encoding="utf-8") as f:
            job = json.load(f)
        os.remove(p)
        actions.append(job["action"])
        with open(os.path.join(a.cfg.results_dir, job["id"] + ".json"), "w") as f:
            json.dump({"v": 1, "id": job["id"], "action": job["action"], "ok": ok, "error": None}, f)
        if net is not None and ok:
            if job["action"] == "ap-start":
                if net.dev.startswith("eth0:ethernet:connected"):
                    net.dev, net.ip = ETH_AP_DEV, ETH_AP_IP
                else:
                    net.dev, net.ip = AP_DEV, AP_IP
            elif job["action"] == "ap-stop":
                net.dev = net.dev.replace("wlan0:wifi:connected:opentv-setup-ap", "wlan0:wifi:disconnected:")
                net.ip = net.ip.replace(AP_IP, "")
    return actions


# --------------------------------------------------------------------------- BLOCANT 2 : hotspot borné
class TestHotspotBounded(unittest.TestCase):
    def test_netstate_sees_ap_behind_ethernet(self):
        s = netstate.snapshot(Net(ETH_AP_DEV, ETH_AP_IP))
        self.assertEqual(s["mode"], "ethernet")
        self.assertTrue(s["ap_active"])
        self.assertFalse(netstate.snapshot(Net(ETH_DEV, ETH_IP))["ap_active"])

    def test_ap_plus_ethernet_keep_setup_stops_ap(self):
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=proc_root(self))
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        self.assertEqual(drain(a, net), ["ap-start"])
        a.tick()
        net.dev, net.ip = ETH_AP_DEV, ETH_AP_IP          # câble branché pendant que le hotspot diffuse
        a.tick()
        self.assertEqual(a.tv_state()["network"]["mode"], "ethernet")
        self.assertEqual(os.listdir(a.cfg.jobs_dir), [])   # en configuration : le hotspot reste disponible
        code, kind, _ = a.setup_submit({"t": token_of(a), "name": "Salon", "net": "keep"})
        self.assertEqual((code, kind), (200, "done"))
        self.assertEqual(drain(a, net), ["ap-stop"])       # « garder la connexion » arrête quand même l'AP
        for _ in range(5):
            a.mono_clock.t += 2
            a.tick()
        self.assertEqual(drain(a, net), [])
        self.assertNotIn("opentv-setup-ap", net.dev)

    def test_orphan_ap_behind_ethernet_is_stopped_when_configured(self):
        net = Net(ETH_AP_DEV, ETH_AP_IP)
        a = make_app(self, net, proc_root=proc_root(self))
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.tick()
        self.assertEqual(drain(a, net), ["ap-stop"])

    def test_ttl_expiry_stops_once_and_stays_closed_for_days(self):
        pr = proc_root(self)
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr, OPENTV_HOTSPOT_MAX_S="1200")
        self.assertEqual(a.cfg.hotspot_max_s, 1200)
        t0 = a.mono_clock.t
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        seen = [(a.mono_clock.t, x) for x in drain(a, net)]
        self.assertEqual([x for _, x in seen], ["ap-start"])
        stop_at = None
        while a.mono_clock.t < t0 + 5 * 86400:
            a.mono_clock.t += 120
            a.tick()
            for x in drain(a, net):
                seen.append((a.mono_clock.t, x))
                if x == "ap-stop":
                    stop_at = a.mono_clock.t
        self.assertEqual([x for _, x in seen], ["ap-start", "ap-stop"], seen)
        self.assertGreaterEqual(stop_at - seen[0][0], 1200)
        self.assertLess(stop_at - seen[0][0], 1200 + 240)
        self.assertNotIn("opentv-setup-ap", net.dev)          # aucun hotspot résiduel
        st = a.tv_state()["setup"]
        self.assertTrue(st["active"])
        self.assertIsNone(st["hotspot"])
        self.assertIsNone(st["url"])
        self.assertIn("Ethernet", st["message"])
        self.assertIn("redémarre", st["message"])
        # redémarrage du service (même démarrage de la boîte) : toujours fermé
        a2 = make_app(self, net, proc_root=pr, OPENTV_STATE_DIR=a.cfg.state_dir)
        a2.mono_clock.t = a.mono_clock.t
        for _ in range(100):
            a2.mono_clock.t += 120
            a2.tick()
        self.assertEqual(drain(a2, net), [])
        self.assertIn("redémarre", a2.tv_state()["setup"]["message"])

    def test_reboot_or_explicit_reset_rearms(self):
        pr = proc_root(self)
        net = Net(OFF_DEV, "")
        a = make_app(self, net, proc_root=pr, OPENTV_HOTSPOT_MAX_S="300")
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        drain(a, net)
        a.mono_clock.t += 400
        a.tick()
        self.assertEqual(drain(a, net), ["ap-stop"])
        a.mono_clock.t += 120
        a.tick()
        self.assertEqual(drain(a, net), [])
        # réinitialisation explicite (CLI root) : réarme le hotspot
        env = os.path.join(a.cfg.state_dir, "..", "otv.env")
        with open(env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\n" % (a.cfg.state_dir, a.cfg.run_dir))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--env", env, "setup", "reset"]), 0)
        self.assertEqual(json.loads(out.getvalue())["hotspot"], "rearmed")
        a.tick()
        a.mono_clock.t += 61
        a.tick()
        self.assertEqual(drain(a, net), ["ap-start"])
        # nouveau démarrage de la boîte (boot_id différent) : budget neuf
        a.mono_clock.t += 400
        a.tick()
        self.assertEqual(drain(a, net), ["ap-stop"])
        set_boot_id(pr, "99999999-2222-3333-4444-555555555555")
        a3 = make_app(self, net, proc_root=pr, OPENTV_STATE_DIR=a.cfg.state_dir, OPENTV_HOTSPOT_MAX_S="300")
        a3.tick()
        a3.mono_clock.t += 61
        a3.tick()
        self.assertEqual(drain(a3, net), ["ap-start"])

    def test_hotspot_max_bounds(self):
        self.assertEqual(config.Config({}).hotspot_max_s, 1200)
        for bad in ("0", "30", "99999", "abc"):
            with self.assertRaises(ValueError):
                config.Config({"OPENTV_HOTSPOT_MAX_S": bad})


# --------------------------------------------------------------------------- BLOCANT 3 : politique d'agents
class TestAgentPolicy(unittest.TestCase):
    def reg(self):
        d = tempfile.mkdtemp(prefix="otvreg", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        return agents.Registry(os.path.join(d, "agents.json"))

    def test_no_policy_is_local_admin(self):
        r = self.reg()
        self.assertFalse(r.policy_defined())
        self.assertTrue(r.issue("claude", ["display", "feedback", "read"]))   # avant configuration : admin local

    def test_none_unknown_force_and_exact_ceiling(self):
        r = self.reg()
        r.set_policy({"muse": [], "hermit": ["read"], "hermes": ["feedback", "read"]})
        self.assertTrue(r.policy_defined())
        for agent, scopes, err in (("muse", ["read"], "scope_exceeds_policy"),          # « aucun accès »
                                   ("hermit", ["feedback", "read"], "scope_exceeds_policy"),
                                   ("hermes", ["display", "feedback", "read"], "scope_exceeds_policy"),
                                   ("claude", ["read"], "agent_not_in_policy")):       # inconnu
            with self.assertRaises(ValueError) as cm:
                r.issue(agent, scopes)
            self.assertEqual(str(cm.exception), err, agent)
            self.assertNotIn(agent, r.list())
        self.assertTrue(r.issue("hermes", ["feedback", "read"]))   # plafond exact
        self.assertTrue(r.issue("hermit", ["read"]))
        self.assertEqual(r.list()["hermes"]["scopes"], ["feedback", "read"])
        self.assertTrue(r.issue("muse", ["read"], force=True))     # --force : décision explicite sur la boîte
        self.assertTrue(r.issue("claude", ["read"], force=True))

    def test_setup_registers_every_known_agent_even_none(self):
        a = make_app(self)
        a.tick()
        code, _, _ = a.setup_submit({"t": token_of(a), "name": "Salon", "net": "keep", "agent_muse": "none",
                                     "agent_hermit": "read"})
        self.assertEqual(code, 200)
        self.assertEqual(a.registry.policy(), {"hermes": [], "hermit": ["read"], "muse": []})
        with self.assertRaises(ValueError):
            a.registry.issue("muse", ["read"])

    def test_cli_refuses_beyond_policy_without_force(self):
        d = tempfile.mkdtemp(prefix="otvcli", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        st = os.path.join(d, "state")
        os.makedirs(st)
        env = os.path.join(d, "otv.env")
        with open(env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\n" % (st, os.path.join(d, "run")))
        agents.Registry(os.path.join(st, "agents.json")).set_policy({"muse": [], "hermit": ["read"], "hermes": []})
        exe = os.path.join(PI4, "bin", "opentv-pi")

        def run(*args):
            p = subprocess.run([sys.executable, "-I", exe, "--env", env, "agent", "issue"] + list(args) +
                               ["--out", os.path.join(d, "c-%s.json" % secrets.token_hex(3))],
                               capture_output=True, timeout=30)
            return p.returncode, json.loads(p.stdout or b"{}")
        self.assertEqual(run("--id", "muse", "--scopes", "read"), (2, {"ok": False, "error": "scope_exceeds_policy"}))
        self.assertEqual(run("--id", "claude", "--scopes", "read"), (2, {"ok": False, "error": "agent_not_in_policy"}))
        self.assertEqual(run("--id", "hermit", "--scopes", "read")[0], 0)
        code, out = run("--id", "muse", "--scopes", "read", "--force")
        self.assertEqual((code, out["scopes"]), (0, ["read"]))


# --------------------------------------------------------------------------- MINEUR 5 : GET ne brûle pas le jeton
class TestSetupGetDoS(unittest.TestCase):
    def test_repeated_bad_get_does_not_burn_token(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        for i in range(40):
            self.assertEqual(s.req("GET", "/setup?t=mauvais-jeton-%02d-xxxxxxxx" % i, lan=True)[0], 403)
        self.assertEqual(s.req("GET", "/setup?t=" + tok, lan=True)[0], 200)
        self.assertEqual(token_of(a), tok)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200, body)

    def test_only_valid_token_with_bad_form_burns(self):
        # revue n°2 : un jeton faux (jamais émis) ne compte pas (sinon n'importe quel appareil du LAN tue le QR) ;
        # seul le bon jeton accompagné d'un formulaire invalide use les 5 essais
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        for i in range(5):
            s.req("POST", "/setup", {"t": "mauvais-jeton-%02d-xxxxxxxx" % i, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(token_of(a), tok)
        for _ in range(5):
            self.assertEqual(s.req("POST", "/setup", {"t": tok, "name": "<b>", "net": "keep"}, lan=True)[0], 400)
        self.assertEqual(s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)[0], 403)


# --------------------------------------------------------------------------- MINEUR 6 : verrouillage par UID
class _Reg:
    def __init__(self, good):
        self.good = good

    def authenticate(self, agent, token):
        return ["read"] if token == self.good else None


class _Audit:
    def record(self, *a, **k):
        pass


class _Ctx:
    def agent_status(self, agent, scopes):
        return {"agent": agent}


class TestAgentPortPerUid(unittest.TestCase):
    def svc(self):
        self.clock = [1000.0]
        return agentport.AgentService(_Ctx(), _Reg("g" * 43), _Audit(), clock=lambda: self.clock[0])

    def req(self, tok):
        return {"v": 1, "agent": "muse", "token": tok, "op": "call", "tool": "status", "args": {}}

    def test_malicious_uid_does_not_lock_other_uid(self):
        s = self.svc()
        for _ in range(agentport.AUTH_FAIL_MAX + 5):
            s.handle(self.req("w" * 43), peer_uid=2001)
        self.assertEqual(s.handle(self.req("g" * 43), peer_uid=2001)["error"], "locked")
        self.assertTrue(s.handle(self.req("g" * 43), peer_uid=1001)["ok"])
        self.assertTrue(s.handle(self.req("g" * 43), peer_uid=None)["ok"])     # sans SO_PEERCRED : seau distinct
        self.clock[0] += agentport.AUTH_LOCK_S + 1
        self.assertTrue(s.handle(self.req("g" * 43), peer_uid=2001)["ok"])

    def test_call_rate_limit_is_per_uid(self):
        s = self.svc()
        for _ in range(agentport.CALLS_PER_S):
            self.assertTrue(s.handle(self.req("g" * 43), peer_uid=2001)["ok"])
        self.assertEqual(s.handle(self.req("g" * 43), peer_uid=2001)["error"], "rate_limited")
        self.assertTrue(s.handle(self.req("g" * 43), peer_uid=1001)["ok"])

    def test_peer_uid_read_from_socket(self):
        if not (hasattr(socket, "SO_PEERCRED") or hasattr(socket, "LOCAL_PEERCRED")):
            self.skipTest("aucune lecture d'identité de pair sur cette plateforme")
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(agentport._peer_uid(a), os.getuid())
        finally:
            a.close()
            b.close()

    def test_bucket_table_is_bounded(self):
        s = self.svc()
        for uid in range(5000):
            s.handle(self.req("w" * 43), peer_uid=10000 + uid)
        self.assertLessEqual(len(s.fails), agentport.MAX_TRACKED_UIDS)


# --------------------------------------------------------------------------- MINEUR 3 : jobs cachés / dossiers
class TestNetJobsJunk(unittest.TestCase):
    def test_hidden_files_dirs_and_links_are_removed_without_action(self):
        d = tempfile.mkdtemp(prefix="otvjunk", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        jobs, results, nm = (os.path.join(d, n) for n in ("jobs", "results", "nm"))
        for x in (jobs, results, nm):
            os.makedirs(x, mode=0o700)
        outside = os.path.join(d, "outside")
        os.makedirs(os.path.join(outside, "keep"))
        with open(os.path.join(outside, "keep", "precieux"), "w") as f:
            f.write("ne pas toucher")
        with open(os.path.join(jobs, ".cache"), "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(jobs, ".git", "objects"))
        with open(os.path.join(jobs, ".git", "objects", "x"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(jobs, "abcdefgh12.json", "sub"))          # dossier au nom de job
        os.symlink(outside, os.path.join(jobs, ".lien"))                    # lien caché vers un dossier
        os.symlink(outside, os.path.join(jobs, "lien000001.json"))
        if hasattr(os, "mkfifo"):
            os.mkfifo(os.path.join(jobs, ".fifo"))
        calls = []
        h = nethelper.Helper(jobs, results, nm, owner_uid=os.getuid(), runner=lambda a, t: calls.append(a) or (0, ""),
                             sleep=lambda s: None, log=lambda *a, **k: None, chown=False)
        h.run_once()
        self.assertEqual(os.listdir(jobs), [])          # DirectoryNotEmpty= retombe : aucune boucle du .path
        self.assertEqual(calls, [])
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep", "precieux")))
        self.assertEqual(h.run_once(), [])

    def test_valid_job_beside_junk_still_runs_once(self):
        d = tempfile.mkdtemp(prefix="otvjunk", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        jobs, tmp, results, nm = (os.path.join(d, n) for n in ("jobs", "tmp", "results", "nm"))
        for x in (jobs, tmp, results, nm):
            os.makedirs(x, mode=0o700)
        netjobs.submit(jobs, tmp, "ap-stop", {})
        with open(os.path.join(jobs, ".residuel.tmp"), "w") as f:
            f.write("x")
        calls = []
        h = nethelper.Helper(jobs, results, nm, owner_uid=os.getuid(), runner=lambda a, t: calls.append(a) or (0, ""),
                             sleep=lambda s: None, log=lambda *a, **k: None, chown=False)
        res = h.run_once()
        self.assertEqual([r["action"] for r in res], ["ap-stop"])
        self.assertTrue(res[0]["ok"])
        self.assertEqual(os.listdir(jobs), [])


# --------------------------------------------------------------------------- MINEUR 4 : watchdog
class TestWatchdogBudget(unittest.TestCase):
    def watchdog_s(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(os.path.join(PI4, "systemd", "opentv.service"))
        return float(cp["Service"]["WatchdogSec"])

    def test_tick_worst_case_fits_watchdog(self):
        worst = netstate.NMCLI_TIMEOUT_S + netstate.IP_TIMEOUT_S + nethelper.KILL_GRACE_S * 2 + cli.LOOP_INTERVAL_S
        self.assertLessEqual(worst * 2, self.watchdog_s(), worst)

    def test_runner_kills_whole_process_group_on_timeout(self):
        t0 = time.monotonic()
        rc, _ = nethelper.default_runner(["/bin/sh", "-c", "sleep 30 & sleep 30"], 0.5)
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - t0, 0.5 + nethelper.KILL_GRACE_S + 1.0)

    def test_slow_cec_never_blocks_tick(self):
        r = fixture_root(self)
        release = threading.Event()
        self.addCleanup(release.set)

        class SlowCec(Net):
            def __call__(self, argv, timeout):
                if argv[0] == "cec-ctl":
                    release.wait(10)
                    return 0, "Physical Address : 1.0.0.0\n"
                return Net.__call__(self, argv, timeout)
        a = make_app(self, SlowCec())
        a.cec_root = r
        a.cec_at = -1e9
        t0 = time.monotonic()
        a.tick()
        a.tick()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(a.tv_state()["cec"]["adapters"], 0)          # découverte encore en cours
        release.set()
        for _ in range(100):
            if a.tv_state()["cec"]["adapters"] == 2:
                break
            time.sleep(0.05)
        self.assertEqual(a.tv_state()["cec"]["adapters"], 2)

    def test_slow_foryou_session_never_blocks_tick(self):
        a = make_app(self)
        self.assertIsNotNone(a.foryou)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        orig = a.foryou.tv_session

        def slow():
            started.set()
            release.wait(10)
            return orig()
        a.foryou.tv_session = slow
        th = threading.Thread(target=a.tv_foryou, daemon=True)
        th.start()
        self.assertTrue(started.wait(5))
        t0 = time.monotonic()
        a.tick()
        self.assertLess(time.monotonic() - t0, 1.0)              # la page « Pour toi » lente ne gèle pas la boucle
        release.set()
        th.join(5)
        self.assertFalse(th.is_alive())

    def test_netstate_uses_short_timeouts(self):
        seen = []
        netstate.snapshot(lambda argv, timeout: seen.append(timeout) or (1, ""))
        self.assertEqual(seen, [netstate.NMCLI_TIMEOUT_S, netstate.IP_TIMEOUT_S])


# --------------------------------------------------------------------------- MINEUR 1 : préparation stricte
class TestPrepareStrict(unittest.TestCase):
    def test_hardware_preflight_runs_before_apt(self):
        cases = [dict(kms=False), dict(model="Raspberry Pi 5 Model B Rev 1.0"), dict(codename="trixie"),
                 dict(rpi=False), dict(drm=False)]
        for fx in cases:
            r = fixture_root(self, **fx)
            p = sh([PREPARE, "--root", r, "--yes", "--no-install"])
            out = p.stdout.decode()
            self.assertNotEqual(p.returncode, 0, fx)
            self.assertNotIn("apt-get", out, fx)
            self.assertIn("préflight", p.stderr.decode(), fx)

    def test_missing_dependencies_are_listed_not_fatal(self):
        r = fixture_root(self, cage=False, chromium=False)
        p = sh([PREPARE, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        m = re.search(r"dépendances absentes \(installées par prepare\) : (.*)", out)
        self.assertIsNotNone(m, out)
        self.assertEqual(sorted(m.group(1).split()), ["cage", "chromium"])
        self.assertIn("apt-get install", out)
        self.assertLess(out.index("préflight matériel : OK"), out.index("apt-get install"))

    def test_full_preflight_flags_missing_dependency_with_package(self):
        from opentv_pi import preflight
        from tests.test_service import pre_runner
        rep = preflight.run(fixture_root(self, cage=False), runner=pre_runner())
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["missing_packages"], ["cage"])
        hw = preflight.run(fixture_root(self, cage=False), runner=pre_runner(), stage="hardware")
        self.assertTrue(hw["ok"], hw)
        self.assertEqual(hw["missing_packages"], ["cage"])


# --------------------------------------------------------------------------- MINEURS 1/2/8 : install.sh
def snapshot(root):
    out = {}
    for rel in tree(root):
        if rel.startswith("var/lib/opentv-install/"):
            continue
        p = os.path.join(root, rel)
        if os.path.islink(p):
            out[rel] = "link:" + os.readlink(p)
            continue
        with open(p, "rb") as f:
            out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


CMDLINE = "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootwait cfg80211.ieee80211_regdom=CA\n"
VIDEO = "video=HDMI-A-1:1920x1080@60"


class TestInstallUpgradeRollback(unittest.TestCase):
    def rootfs(self):
        r = fixture_root(self)
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "w") as f:
            f.write(CMDLINE)
        return r

    def install(self, r, ok=True):
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p

    def ro(self, path):
        os.chmod(path, 0o555)
        self.addCleanup(os.chmod, path, 0o755)

    def test_1080p_kernel_arg_is_part_of_install_and_rolled_back(self):
        r = self.rootfs()
        cmd = os.path.join(r, "boot/firmware/cmdline.txt")
        p = self.install(r)
        self.assertIn("redémarrage", p.stdout.decode())
        with open(cmd) as f:
            text = f.read()
        self.assertEqual(text.count(VIDEO), 1)
        self.assertEqual(len(text.splitlines()), 1)
        self.install(r)                                           # idempotent
        with open(cmd) as f:
            self.assertEqual(f.read().count(VIDEO), 1)
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(cmd) as f:
            self.assertEqual(f.read(), CMDLINE)                   # ligne d'origine, octet pour octet

    def test_existing_video_mode_is_respected(self):
        r = self.rootfs()
        cmd = os.path.join(r, "boot/firmware/cmdline.txt")
        with open(cmd, "w") as f:
            f.write(CMDLINE.strip() + " video=HDMI-A-1:1280x720@60\n")
        p = self.install(r)
        self.assertIn("video=HDMI-A-1 déjà présent", p.stdout.decode())
        with open(cmd) as f:
            self.assertNotIn(VIDEO, f.read())

    def test_upgrade_restarts_services_and_removes_obsolete_files(self):
        r = self.rootfs()
        self.install(r)
        obsolete = os.path.join(r, "opt/opentv/lib/opentv_pi/ancien_module.py")
        with open(obsolete, "w") as f:
            f.write("# v0\n")
        victim = os.path.join(r, "etc/hostname")
        with open(victim, "w") as f:
            f.write("opentv-salon\n")
        man = os.path.join(r, "opt/opentv/MANIFEST.sha256")
        with open(man, "a") as f:
            f.write("%s  /opt/opentv/lib/opentv_pi/ancien_module.py\n" % hashlib.sha256(b"# v0\n").hexdigest())
            f.write("%s  /etc/hostname\n" % hashlib.sha256(b"opentv-salon\n").hexdigest())   # hors périmètre
        p = self.install(r)
        out = p.stdout.decode()
        self.assertFalse(os.path.exists(obsolete))
        self.assertTrue(os.path.exists(victim))
        self.assertIn("retiré (absent du nouveau manifeste) : /opt/opentv/lib/opentv_pi/ancien_module.py", out)
        bk = [x for x in tree(r) if x.endswith("opt/opentv/lib/opentv_pi/ancien_module.py")]
        self.assertEqual(len(bk), 1, bk)
        self.assertTrue(bk[0].startswith("var/lib/opentv-install/backup-"))
        self.assertRegex(out, r"systemctl restart opentv-net-helper\.path opentv-ap-guard\.timer opentv\.service "
                              r"opentv-kiosk\.service")
        self.assertLess(out.index("systemctl daemon-reload"), out.index("systemctl restart"))
        with open(man) as f:
            self.assertNotIn("ancien_module", f.read())

    def _prepare_upgrade(self, r):
        self.install(r)
        obsolete = os.path.join(r, "opt/opentv/lib/opentv_pi/ancien_module.py")
        with open(obsolete, "w") as f:
            f.write("# v0\n")
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), "a") as f:
            f.write("%s  /opt/opentv/lib/opentv_pi/ancien_module.py\n" % hashlib.sha256(b"# v0\n").hexdigest())
        with open(os.path.join(r, "opt/opentv/lib/opentv_pi/app.py"), "a") as f:
            f.write("# version précédente\n")
        with open(os.path.join(r, "etc/pam.d/opentv-kiosk"), "a") as f:
            f.write("# version précédente\n")
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "w") as f:
            f.write(CMDLINE)                                       # paramètre 1080p encore absent

    def test_rollback_when_copy_fails_midway(self):
        r = self.rootfs()
        self._prepare_upgrade(r)
        before = snapshot(r)
        self.ro(os.path.join(r, "etc/pam.d"))                      # échec injecté au milieu de la copie
        p = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        os.chmod(os.path.join(r, "etc/pam.d"), 0o755)
        self.assertEqual(snapshot(r), before)
        self.assertNotIn("systemctl restart", p.stdout.decode())

    def test_rollback_when_late_step_fails_restores_removed_and_cmdline(self):
        r = self.rootfs()
        self._prepare_upgrade(r)
        os.remove(os.path.join(r, "etc/opentv/opentv.env"))       # l'étape env (après retrait + 1080p) devra écrire
        before = snapshot(r)
        self.ro(os.path.join(r, "etc/opentv"))                     # échec injecté tard
        p = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        os.chmod(os.path.join(r, "etc/opentv"), 0o755)
        self.assertEqual(snapshot(r), before)
        self.assertTrue(os.path.exists(os.path.join(r, "opt/opentv/lib/opentv_pi/ancien_module.py")))
        with open(os.path.join(r, "boot/firmware/cmdline.txt")) as f:
            self.assertEqual(f.read(), CMDLINE)

    def test_fresh_install_failure_leaves_nothing(self):
        r = self.rootfs()
        before = snapshot(r)
        os.makedirs(os.path.join(r, "etc/pam.d"))
        self.ro(os.path.join(r, "etc/pam.d"))
        p = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        os.chmod(os.path.join(r, "etc/pam.d"), 0o755)
        self.assertEqual(snapshot(r), before)


# --------------------------------------------------------------------------- BLOCANT 1 : reprise de la page TV
@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestTvPageRecovery(unittest.TestCase):
    def render(self, s, budget):
        prof = tempfile.mkdtemp(prefix="otvchr", dir="/tmp")
        self.addCleanup(shutil.rmtree, prof, True)
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + prof,
                            "--virtual-time-budget=%d" % budget, "--window-size=1920,1080", "--dump-dom",
                            "http://127.0.0.1:%d/tv" % s.web.tv_port()], capture_output=True, timeout=120)
        return p.stdout.decode("utf-8", "replace")

    def configured(self, name="Salon"):
        a = make_app(self)
        config.save_device(a.cfg, {"name": name})
        a.reload_device()
        return a, Servers(self, a)

    def page_loads(self, a):
        return sum(1 for e in a.test_logs if e.get("event") == "http" and e.get("path") == "/tv" and e.get("status") == 200)

    def test_rotated_key_reloads_page_and_shows_data_again(self):
        a, s = self.configured()
        orig, n = a.tv_state, [0]

        def tv_state():
            n[0] += 1
            st = orig()
            if n[0] == 1:                          # le service « redémarre » : nouvelle clé, nouvel état
                a.tv_key = "k" + secrets.token_urlsafe(32)
                config.save_device(a.cfg, {"name": "Chambre"})
                a.reload_device()
            return st
        a.tv_state = tv_state
        dom = self.render(s, 20000)
        self.assertIn("Chambre", dom)                      # données relues avec la nouvelle clé
        self.assertIn("Le choix de ce soir", dom)
        self.assertIn('data-reloads="1"', dom)             # exactement un rechargement…
        self.assertEqual(self.page_loads(a), 3)            # … = page initiale + sonde /tv + navigation
        self.assertRegex(dom, r'id="offline"[^>]*hidden')
        self.assertIn("applis Samsung", dom)

    def test_persistent_key_mismatch_is_bounded(self):
        a, s = self.configured()
        orig = a.tv_state

        def tv_state():
            a.tv_key = "k" + secrets.token_urlsafe(32)     # chaque page reçoit une clé déjà périmée
            return orig()
        a.tv_state = tv_state
        dom = self.render(s, 120000)
        self.assertIn('data-reloads="3"', dom)             # plafond : 3 rechargements par fenêtre de 10 min
        self.assertEqual(self.page_loads(a), 1 + 2 * 3)    # puis plus aucune sonde ni navigation
        self.assertIn("Redémarre la boîte", dom)

    def test_non_key_403_never_reloads(self):
        a, s = self.configured()
        orig = a.tv_state

        def tv_state():
            a.tv_client_uids = frozenset({4242})           # le contrôle UID refuse désormais : pas une clé tournée
            a.peer_uid_fn = lambda ip, port, sport: 1
            return orig()
        a.tv_state = tv_state
        dom = self.render(s, 20000)
        self.assertEqual(self.page_loads(a), 1)
        self.assertIn("Reconnexion", dom)


if __name__ == "__main__":
    unittest.main()
