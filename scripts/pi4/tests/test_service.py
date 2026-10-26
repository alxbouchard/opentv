"""Service OpenTV Pi : premier démarrage (QR, jeton, formulaire), hotspot borné, routes TV/CSP, préflight, CEC."""
import glob
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
import urllib.parse

from tests._common import PI4, NANOPI, SECRET_PSK, write_catalogue
from opentv_pi import app as appmod, config, web, preflight, cec, health

ETH_DEV = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:\n"
ETH_IP = "2: eth0    inet 192.168.2.40/24 brd 192.168.2.255 scope global eth0\n"
OFF_DEV = "eth0:ethernet:unavailable:\nwlan0:wifi:disconnected:\n"
AP_DEV = "eth0:ethernet:unavailable:\nwlan0:wifi:connected:opentv-setup-ap\n"
AP_IP = "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\n"
WIFI_DEV = "eth0:ethernet:unavailable:\nwlan0:wifi:connected:opentv-wifi\n"
WIFI_IP = "3: wlan0    inet 192.168.2.41/24 brd 192.168.2.255 scope global wlan0\n"


class Net:
    def __init__(self, dev=ETH_DEV, ip=ETH_IP):
        self.dev, self.ip, self.calls = dev, ip, []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[:2] == ["nmcli", "-t"]:
            return 0, self.dev
        if argv[:2] == ["ip", "-4"]:
            return 0, self.ip
        return 1, ""


class Mono:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def make_app(tc, net=None, proc_root=None, **env):
    d = tempfile.mkdtemp(prefix="otv", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    cat = write_catalogue(os.path.join(d, "catalogue.json"))
    e = {"OPENTV_STATE_DIR": os.path.join(d, "state"), "OPENTV_RUN_DIR": os.path.join(d, "run"),
         "OPENTV_SHARE_DIR": os.path.join(PI4, "ui"), "OPENTV_FORYOU_ENGINE_DIR": NANOPI,
         "OPENTV_CATALOGUE_FILE": cat, "OPENTV_QRCODE_JS": os.path.join(os.path.dirname(os.path.dirname(PI4)),
                                                                          "native", "app", "qrcode.js"),
         "OPENTV_TV_PORT": "0", "OPENTV_SETUP_PORT": "0", "OPENTV_HOTSPOT_GRACE_S": "60"}
    e.update(env)
    cfg = config.Config(e)
    cfg.ensure_dirs()
    logs = []
    mono = Mono()
    if proc_root is None:   # démarrage identifiable par défaut (sinon le hotspot est fermé, fail-closed)
        proc_root = os.path.join(d, "proc")
        os.makedirs(os.path.join(proc_root, "sys", "kernel", "random"))
        with open(os.path.join(proc_root, "sys", "kernel", "random", "boot_id"), "w") as f:
            f.write("0badc0de-0000-4000-8000-000000000001\n")
    a = appmod.App(cfg, runner=net or Net(), mono=mono, log=lambda ev, **f: logs.append(dict(f, event=ev)),
                   proc_root=proc_root)
    tc.addCleanup(a.close)
    a.mono_clock = mono
    a.test_logs = logs
    return a


class Servers:
    def __init__(self, tc, app):
        self.web = web.WebServers(app)
        self.web.start_tv()
        app.tick()
        self.web.sync_lan()
        tc.addCleanup(self.web.stop)
        self.app = app

    def req(self, method, path, body=None, headers=None, lan=False):
        port = self.web.lan_port() if lan else self.web.tv_port()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % port}
        h.update(headers or {})
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body)
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def tv_headers(self):
        return {"X-OpenTV-TV": self.app.tv_key}


def token_of(app):
    return app.tv_state()["setup"]["token"]


class TestFirstBootSetup(unittest.TestCase):
    def test_unconfigured_ethernet_shows_qr_and_lan_form(self):
        a = make_app(self)
        s = Servers(self, a)
        st = a.tv_state()
        self.assertTrue(st["setup"]["active"])
        self.assertEqual(st["network"]["mode"], "ethernet")
        self.assertTrue(st["setup"]["url"].startswith("http://192.168.2.40:%d/setup?t=" % s.web.lan_port()))
        tok = token_of(a)
        code, h, body = s.req("GET", "/setup?t=" + urllib.parse.quote(tok), lan=True)
        self.assertEqual(code, 200)
        self.assertIn("default-src 'none'", h["content-security-policy"])
        csp = h["content-security-policy"]   # un seul script externe de même origine, jamais en ligne ni connexion
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("unsafe", csp)
        self.assertNotIn("connect-src", csp)
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=\"/static/setup\.js\")", body))
        self.assertIn("Quel est ton Wi-Fi", body)
        for bad in ("/setup", "/setup?t=nope", "/tv", "/api/tv/state", "/static/tv.js"):
            self.assertIn(s.req("GET", bad, lan=True)[0], (403, 404), bad)
        self.assertNotIn(tok, json.dumps(a.test_logs))

    def test_submit_validation_then_one_shot(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "wifi", "ssid": "a;b",
                                                 "psk": "12345678"}, lan=True)
        self.assertEqual(code, 400)
        self.assertIn("Nom du Wi-Fi invalide", body)
        self.assertNotIn("12345678", body)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon d'Alex", "net": "keep",
                                                 "agent_muse": "feedback", "agent_hermit": "read"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertIn("C'est prêt", body)
        dev = config.load_device(a.cfg)
        self.assertEqual(dev["name"], "Salon d'Alex")
        self.assertEqual(stat.S_IMODE(os.stat(a.cfg.device_file).st_mode), 0o600)
        self.assertEqual(a.registry.policy(), {"hermes": [], "hermit": ["read"], "muse": ["feedback", "read"]})
        self.assertEqual(s.req("POST", "/setup", {"t": tok, "name": "Autre", "net": "keep"}, lan=True)[0], 403)
        a.tick()
        s.web.sync_lan()
        self.assertIsNone(s.web.lan_port())                      # le port LAN se ferme après la configuration
        self.assertFalse(a.tv_state()["setup"]["active"])

    def test_attempts_burn_token(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        for _ in range(5):
            s.req("POST", "/setup", {"t": tok, "name": "<b>", "net": "keep"}, lan=True)
        self.assertEqual(s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)[0], 403)
        a.tick()
        self.assertNotEqual(token_of(a), tok)                    # nouveau QR sur la TV

    def test_wifi_job_success_and_failure(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "wifi", "ssid": "Maison",
                                                 "psk": SECRET_PSK, "agent_muse": "none"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertNotIn("Tr0p", body)
        jobs = os.listdir(a.cfg.jobs_dir)
        self.assertEqual(len(jobs), 1)
        p = os.path.join(a.cfg.jobs_dir, jobs[0])
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(json.loads(_read(p))["payload"]["restore_ap"], False)
        self.assertEqual(a.tv_state()["setup"]["phase"], "applying")
        self.assertNotIn("Tr0p", json.dumps(a.tv_state()) + json.dumps(a.test_logs))
        # le helper échoue : nouveau jeton, message d'erreur, toujours en configuration
        jid = jobs[0][:-5]
        os.remove(p)
        self._result(a, jid, "wifi-apply", False, "wifi_connect_failed")
        a.tick()
        st = a.tv_state()
        self.assertEqual(st["setup"]["phase"], "error")
        self.assertIn("mot de passe", st["setup"]["message"])
        tok2 = st["setup"]["token"]
        self.assertNotEqual(tok2, tok)
        s.req("POST", "/setup", {"t": tok2, "name": "Salon", "net": "wifi", "ssid": "Maison", "psk": "autremotdepasse"},
              lan=True)
        jid = os.listdir(a.cfg.jobs_dir)[0][:-5]
        self._result(a, jid, "wifi-apply", True, None)
        a.tick()
        self.assertFalse(a.tv_state()["setup"]["active"])

    def _result(self, a, jid, action, ok, err):
        with open(os.path.join(a.cfg.results_dir, jid + ".json"), "w") as f:
            json.dump({"v": 1, "id": jid, "action": action, "ok": ok, "error": err}, f)

    def test_hotspot_after_grace_and_qr(self):
        net = Net(OFF_DEV, "")
        a = make_app(self, net)
        a.tick()
        self.assertEqual(os.listdir(a.cfg.jobs_dir), [])
        self.assertIn("Ethernet", a.tv_state()["setup"]["message"])
        a.mono_clock.t += 61
        a.tick()
        jobs = os.listdir(a.cfg.jobs_dir)
        self.assertEqual(len(jobs), 1)
        job = json.loads(_read(os.path.join(a.cfg.jobs_dir, jobs[0])))
        self.assertEqual(job["action"], "ap-start")
        ap = a.tv_state()["setup"]["hotspot"]
        self.assertEqual(ap["ssid"], job["payload"]["ssid"])
        self.assertEqual(ap["wifi_qr"], "WIFI:T:WPA;S:%s;P:%s;;" % (job["payload"]["ssid"], job["payload"]["psk"]))
        a.tick()
        self.assertEqual(len(os.listdir(a.cfg.jobs_dir)), 1)   # pas de second job
        net.dev, net.ip = AP_DEV, AP_IP
        a.tick()
        st = a.tv_state()
        self.assertEqual(st["network"]["mode"], "hotspot")
        self.assertTrue(st["setup"]["url"].startswith("http://10.42.0.1:"))
        # le formulaire en hotspot exige un Wi-Fi et demande la restauration du point d'accès en cas d'échec
        s = Servers(self, a)
        tok = token_of(a)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 400)
        self.assertIn("Wi-Fi", body)
        os.remove(os.path.join(a.cfg.jobs_dir, jobs[0]))
        s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "wifi", "ssid": "Maison", "psk": "12345678"}, lan=True)
        job = json.loads(_read(os.path.join(a.cfg.jobs_dir, os.listdir(a.cfg.jobs_dir)[0])))
        self.assertEqual((job["action"], job["payload"]["restore_ap"], job["payload"]["delay_s"]), ("wifi-apply", True, 3))

    def test_hotspot_disabled_is_bounded(self):
        a = make_app(self, Net(OFF_DEV, ""), OPENTV_HOTSPOT_ENABLED="0")
        a.mono_clock.t += 1000
        a.tick()
        self.assertEqual(os.listdir(a.cfg.jobs_dir), [])
        self.assertIsNone(a.tv_state()["setup"]["url"])
        self.assertIn("Ethernet", a.tv_state()["setup"]["message"])


class TestTVRoutes(unittest.TestCase):
    def configured(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        return a, Servers(self, a)

    def test_tv_page_csp_and_api_guard(self):
        a, s = self.configured()
        code, h, body = s.req("GET", "/tv")
        self.assertEqual(code, 200)
        csp = h["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", body))   # aucun script en ligne
        self.assertIn(a.tv_key, body)
        self.assertEqual(s.req("GET", "/api/tv/state")[0], 403)
        self.assertEqual(s.req("GET", "/api/tv/state", headers={"X-OpenTV-TV": "x" * 43})[0], 403)
        code, _, body = s.req("GET", "/api/tv/state", headers=s.tv_headers())
        st = json.loads(body)
        self.assertEqual((code, st["device"], st["setup"]["active"]), (200, "Salon", False))
        self.assertEqual(s.req("GET", "/api/tv/state", headers=dict(s.tv_headers(), Host="evil.example:80"))[0], 421)
        self.assertEqual(s.req("GET", "/etc/passwd")[0], 404)
        self.assertEqual(s.req("GET", "/static/../opentv_pi/web.py")[0], 404)
        for name in ("tv.js", "tv.css", "qrcode.js"):
            code, h, _ = s.req("GET", "/static/" + name)
            self.assertEqual(code, 200, name)
        self.assertEqual(s.req("GET", "/healthz")[0], 200)

    def test_foryou_rows_and_feedback(self):
        a, s = self.configured()
        code, _, body = s.req("GET", "/api/tv/foryou", headers=s.tv_headers())
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(d["rows"])
        self.assertNotIn("detail_path", body)
        self.assertNotIn("www.spam", body)
        uid = d["rows"][0]["items"][0]["id"]
        hdr = dict(s.tv_headers(), **{"Content-Type": "application/json"})
        code, _, body = s.req("POST", "/api/tv/feedback", json.dumps({"id": uid, "action": "like"}), hdr)
        r = json.loads(body)
        self.assertEqual((code, r["state"]["opinion"]), (200, "like"))
        code, _, _ = s.req("POST", "/api/tv/feedback", json.dumps({"id": uid, "action": "like"}),
                           {"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        code, _, _ = s.req("POST", "/api/tv/feedback", "x" * 5000, hdr)
        self.assertEqual(code, 413)

    def test_agent_status_and_message(self):
        a, s = self.configured()
        st = a.agent_status("muse", ["read"])
        blob = json.dumps(st)
        self.assertNotIn("192.168", blob)
        self.assertEqual(st["relay"], {"status": "not_implemented"})
        self.assertEqual(st["network"], {"mode": "ethernet", "online": True})
        a.show_message("muse", "Bonjour Alex", 10)
        self.assertEqual(a.tv_state()["message"]["text"], "Bonjour Alex")
        self.assertEqual(a.tv_state()["message"]["from"], "muse")
        a.mono_clock.t += 11
        self.assertIsNone(a.tv_state()["message"])

    def test_request_setup_from_tv(self):
        a, s = self.configured()
        hdr = dict(s.tv_headers(), **{"Content-Type": "application/json"})
        code, _, _ = s.req("POST", "/api/tv/setup", "{}", hdr)
        self.assertEqual(code, 200)
        a.tick()
        self.assertTrue(a.tv_state()["setup"]["active"])


def find_chromium():
    for p in (os.environ.get("OPENTV_TEST_CHROMIUM"),) + tuple(sorted(glob.glob(os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell")))[-1:]) + \
            tuple(sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/"
                                                      "chrome-headless-shell")))[-1:]):
        if p and os.path.exists(p):
            return p
    return shutil.which("chromium") or shutil.which("chromium-browser")


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestRealChromium(unittest.TestCase):
    def render(self, a, s):
        prof = tempfile.mkdtemp(prefix="otvchr", dir="/tmp")
        self.addCleanup(shutil.rmtree, prof, True)
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + prof,
                            "--virtual-time-budget=4000", "--window-size=1920,1080", "--dump-dom",
                            "http://127.0.0.1:%d/tv" % s.web.tv_port()], capture_output=True, timeout=60)
        return p.stdout.decode("utf-8", "replace")

    def test_setup_qr_rendered(self):
        a = make_app(self)
        s = Servers(self, a)
        dom = self.render(a, s)
        self.assertIn('data-qr="ok"', dom)
        self.assertIn("Scanne", dom)

    def test_foryou_rendered(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = Servers(self, a)
        dom = self.render(a, s)
        self.assertIn("Le choix de ce soir", dom)
        self.assertIn("Salon", dom)
        self.assertNotIn("CSP", dom)


# --------------------------------------------------------------------------- préflight / CEC / santé
def fixture_root(tc, model="Raspberry Pi 4 Model B Rev 1.5", codename="bookworm", kms=True, cage=True, cecdev=True,
                 regdom=True, rpi=True, drm=True, chromium=True):
    r = tempfile.mkdtemp(prefix="otvroot", dir="/tmp")
    tc.addCleanup(shutil.rmtree, r, True)

    def w(path, text, mode=0o644):
        p = os.path.join(r, path.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, mode)
    w("/proc/device-tree/model", model + "\0")
    w("/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_CODENAME=%s\n' % codename)
    if rpi:
        w("/etc/rpi-issue", "Raspberry Pi reference 2025-05-13\n")
    w("/boot/firmware/config.txt", "[all]\n%sdtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n" % ("" if kms else "#"))
    w("/boot/firmware/cmdline.txt", "console=tty1 root=PARTUUID=abcd-02 rootwait\n")
    if drm:
        w("/sys/class/drm/card1-HDMI-A-1/status", "connected\n")
        w("/sys/module/vc4/refcnt", "1\n")
        w("/dev/dri/card1", "")
    w("/proc/cmdline", "console=tty1 %s\n" % ("cfg80211.ieee80211_regdom=CA" if regdom else ""))
    if cecdev:
        w("/dev/cec0", "")
        w("/dev/cec1", "")
    bins = ["nmcli", "systemctl", "cec-ctl", "wlr-randr", "dnsmasq", "dpkg", "python3"] + (["chromium"] if chromium else [])
    if cage:
        bins.append("cage")
    for b in bins:
        w("/usr/bin/" + b, "#!/bin/sh\n", 0o755)
    return r


def pre_runner(arch="arm64", nm="active"):
    def run(argv, timeout):
        if argv == ["dpkg", "--print-architecture"]:
            return 0, arch + "\n"
        if argv == ["systemctl", "is-active", "NetworkManager"]:
            return (0 if nm == "active" else 3), nm + "\n"
        return 1, ""
    return run


class TestPreflight(unittest.TestCase):
    def status(self, rep, cid):
        return {c["id"]: c["status"] for c in rep["checks"]}[cid]

    def test_pi4_bookworm_ok(self):
        rep = preflight.run(fixture_root(self), runner=pre_runner())
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["platform"], "pi4")
        self.assertEqual(self.status(rep, "cec"), "ok")

    def test_failures(self):
        cases = [
            (dict(model="Raspberry Pi 5 Model B Rev 1.0"), {}, "model"),
            (dict(model="FriendlyElec NanoPi M4B"), {}, "model"),
            (dict(codename="bullseye"), {}, "os"),
            (dict(kms=False), {}, "kms"),
            (dict(cage=False), {}, "bin_cage"),
            ({}, dict(arch="armhf"), "arch"),
            ({}, dict(nm="inactive"), "networkmanager"),
        ]
        for fx, rn, cid in cases:
            rep = preflight.run(fixture_root(self, **fx), runner=pre_runner(**rn))
            self.assertFalse(rep["ok"], (fx, rn))
            self.assertEqual(self.status(rep, cid), "fail", (fx, rn, rep))
        rep = preflight.run(fixture_root(self, model="Raspberry Pi 5 Model B Rev 1.0"), runner=pre_runner())
        self.assertIn("Pi 5", [c for c in rep["checks"] if c["id"] == "model"][0]["detail"])

    def test_warnings_do_not_fail(self):
        rep = preflight.run(fixture_root(self, cecdev=False, regdom=False), runner=pre_runner())
        self.assertTrue(rep["ok"])
        self.assertEqual(self.status(rep, "cec"), "warn")
        self.assertEqual(self.status(rep, "wifi_country"), "warn")

    def test_cli_strict_exit_code(self):
        r = fixture_root(self, kms=False)
        p = subprocess.run([os.path.join(PI4, "bin", "opentv-pi"), "preflight", "--root", r, "--assume-arch", "arm64",
                            "--assume-nm-active"], capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertFalse(json.loads(p.stdout)["ok"])


class TestCec(unittest.TestCase):
    def test_discovery_only(self):
        r = fixture_root(self)
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return 0, ("Driver Info:\n\tDriver Name                : vc4_hdmi\nCEC Adapter Info:\n"
                       "\tPhysical Address           : 1.0.0.0\n\tLogical Address Mask       : 0x0000\n")
        d = cec.discover(r, runner=run)
        self.assertEqual([x["device"] for x in d["adapters"]], ["/dev/cec0", "/dev/cec1"])
        self.assertEqual(d["adapters"][0]["physical_address"], "1.0.0.0")
        self.assertTrue(d["discovery_only"])
        for argv in calls:
            self.assertEqual(argv[:2], ["cec-ctl", "-d"])
            self.assertEqual(len(argv), 3)                        # aucune option d'émission/configuration


class TestHealthManifest(unittest.TestCase):
    def test_manifest_verify(self):
        r = tempfile.mkdtemp(prefix="otvman", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        os.makedirs(os.path.join(r, "opt/opentv"))
        with open(os.path.join(r, "opt/opentv/a.txt"), "w") as f:
            f.write("bonjour")
        import hashlib
        h = hashlib.sha256(b"bonjour").hexdigest()
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), "w") as f:
            f.write("%s  /opt/opentv/a.txt\n%s  /opt/opentv/missing\n" % (h, h))
        res = health.verify_manifest(r)
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"], ["/opt/opentv/missing"])
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), "w") as f:
            f.write("%s  /opt/opentv/a.txt\n" % h)
        self.assertTrue(health.verify_manifest(r)["ok"])
        with open(os.path.join(r, "opt/opentv/a.txt"), "w") as f:
            f.write("modifié")
        self.assertEqual(health.verify_manifest(r)["mismatch"], ["/opt/opentv/a.txt"])


class TestConfig(unittest.TestCase):
    def test_env_file_is_parsed_not_executed(self):
        d = tempfile.mkdtemp(prefix="otvcfg", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "opentv.env")
        marker = os.path.join(d, "pwned")
        with open(p, "w") as f:
            f.write("# commentaire\nOPENTV_TV_PORT=8090\nOPENTV_HOTSPOT_ENABLED=\"0\"\nPATH=/evil\n"
                    "OPENTV_FORYOU_SOURCE_ID=$(touch %s)\n" % marker)
        env = config.read_env_file(p)
        self.assertEqual(env["OPENTV_TV_PORT"], "8090")
        self.assertNotIn("PATH", env)
        self.assertFalse(os.path.exists(marker))
        with self.assertRaises(ValueError):
            config.Config(env)                                     # source invalide : refus, pas de repli
        env.pop("OPENTV_FORYOU_SOURCE_ID")
        c = config.Config(env)
        self.assertEqual((c.tv_port, c.hotspot_enabled), (8090, False))
        with self.assertRaises(ValueError):
            config.Config({"OPENTV_TV_PORT": "80"})


if __name__ == "__main__":
    unittest.main()
