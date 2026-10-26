"""Validation, jeton QR, profils NetworkManager, helper privilégié, état réseau.

Depuis la racine : python3 -m unittest discover -s scripts/pi4/tests -t scripts/pi4
"""
import json
import os
import stat
import sys
import tempfile
import unittest

from tests._common import SECRET_PSK  # noqa: F401  (ajoute aussi scripts/pi4 au chemin)
from opentv_pi import validate, setup_token, nmkeyfile, nethelper, netjobs, netstate


def gkeyfile_unescape(v):
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[v[i + 1]])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def kf_value(text, section, key):
    cur = None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
        elif cur == section and line.startswith(key + "="):
            return gkeyfile_unescape(line[len(key) + 1:])
    return None


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestValidate(unittest.TestCase):
    def test_ssid_bounds(self):
        self.assertEqual(validate.ssid("Maison 2.4G"), "Maison 2.4G")
        self.assertEqual(validate.ssid("Café-Wi-Fi"), "Café-Wi-Fi")
        for bad in ("", "x" * 33, "é" * 17, " lead", "trail ", "a\nb", "a\x00b", "a;b", 123, None, "\x7f"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate.ssid(bad)

    def test_psk_bounds(self):
        self.assertEqual(validate.psk("12345678"), "12345678")
        self.assertEqual(validate.psk(SECRET_PSK), SECRET_PSK)
        self.assertEqual(validate.psk("a" * 63), "a" * 63)
        self.assertEqual(validate.psk("0123456789abcdef" * 4), "0123456789abcdef" * 4)
        for bad in ("1234567", "z" * 64, "g" * 64, "a" * 65, "motdepasse\n", "motdepasse\t", "é" * 10, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate.psk(bad)

    def test_device_name(self):
        self.assertEqual(validate.device_name("  Salon d'Alex  "), "Salon d'Alex")
        self.assertEqual(validate.device_name("Chambre-2"), "Chambre-2")
        for bad in ("", " ", "x" * 33, "<script>", "a/b", "tv.example.com/x", "nom\n", "a_b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate.device_name(bad)

    def test_agent_id_and_scopes(self):
        self.assertEqual(validate.agent_id("muse"), "muse")
        for bad in ("Muse", "m", "1abc", "a" * 25, "mu se", "../x"):
            with self.assertRaises(ValueError):
                validate.agent_id(bad)
        self.assertEqual(validate.scopes(["feedback", "read", "read"]), ["feedback", "read"])
        with self.assertRaises(ValueError):
            validate.scopes(["read", "shell"])

    def test_message_text_rejects_urls(self):
        self.assertEqual(validate.message_text("  Le souper est prêt ! "), "Le souper est prêt !")
        for bad in ("", "x" * 141, "va sur https://a.b", "www.exemple.com", "voir exemple.com/x", "javascript:alert(1)",
                    "ligne\nligne", "192.168.2.1/admin"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate.message_text(bad)


class TestSetupToken(unittest.TestCase):
    def test_ttl_one_shot_and_attempts(self):
        clk = Clock()
        st = setup_token.SetupToken(clock=clk, ttl=600, max_attempts=3)
        tok = st.issue()
        self.assertGreaterEqual(len(tok), 32)
        self.assertNotIn(tok, repr(st))
        self.assertTrue(st.check(tok))
        self.assertFalse(st.check(tok + "x"))
        self.assertFalse(st.check(""))
        self.assertFalse(st.check(None))
        self.assertTrue(st.consume(tok))
        self.assertFalse(st.consume(tok))       # usage unique
        self.assertFalse(st.check(tok))
        self.assertIsNone(st.current())
        tok2 = st.issue()
        self.assertNotEqual(tok, tok2)
        clk.t += 601
        self.assertFalse(st.check(tok2))        # expiré
        self.assertTrue(st.needs_rotation())
        tok3 = st.issue()
        for _ in range(3):
            st.fail_attempt()
        self.assertFalse(st.check(tok3))        # brûlé après 3 essais
        self.assertTrue(st.needs_rotation())

    def test_current_reports_remaining(self):
        clk = Clock()
        st = setup_token.SetupToken(clock=clk, ttl=600)
        tok = st.issue()
        clk.t += 100
        cur = st.current()
        self.assertEqual(cur[0], tok)
        self.assertEqual(cur[1], 500)


class TestKeyfile(unittest.TestCase):
    def test_wifi_profile_escaping_roundtrip(self):
        txt = nmkeyfile.wifi_profile("Mon Wi-Fi", SECRET_PSK, hidden=True, uuid="11111111-2222-4333-8444-555555555555")
        self.assertEqual(kf_value(txt, "wifi", "ssid"), "Mon Wi-Fi")
        self.assertEqual(kf_value(txt, "wifi-security", "psk"), SECRET_PSK)
        self.assertEqual(kf_value(txt, "wifi", "hidden"), "true")
        self.assertEqual(kf_value(txt, "connection", "id"), "opentv-wifi")
        self.assertEqual(kf_value(txt, "wifi-security", "key-mgmt"), "wpa-psk")
        self.assertEqual(kf_value(txt, "connection", "autoconnect"), "true")
        for line in txt.splitlines():   # aucune ligne ne commence par un espace, aucune section injectée
            self.assertFalse(line.startswith(" "))
        self.assertEqual(sum(1 for l in txt.splitlines() if l.startswith("[")), 5)

    def test_injection_rejected(self):
        with self.assertRaises(ValueError):
            nmkeyfile.wifi_profile("x\n[connection]\nid=evil", "12345678")
        with self.assertRaises(ValueError):
            nmkeyfile.wifi_profile("ok", "12345678\n[ipv4]\nmethod=disabled")

    def test_ap_profile(self):
        txt = nmkeyfile.ap_profile("OpenTV-AB12", "abcdefgh2345")
        self.assertEqual(kf_value(txt, "wifi", "mode"), "ap")
        self.assertEqual(kf_value(txt, "ipv4", "method"), "shared")
        self.assertEqual(kf_value(txt, "connection", "autoconnect"), "false")
        self.assertEqual(kf_value(txt, "connection", "id"), "opentv-setup-ap")
        with self.assertRaises(ValueError):
            nmkeyfile.ap_profile("Evil-AP", "abcdefgh2345")

    def test_write_profile_mode_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            p = nmkeyfile.write_profile(d, "opentv-wifi", "[connection]\nid=opentv-wifi\n")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertTrue(p.endswith("opentv-wifi.nmconnection"))
            with self.assertRaises(ValueError):
                nmkeyfile.write_profile(d, "../../etc/passwd", "x")
            # un lien symbolique pré-placé n'est jamais suivi
            target = os.path.join(d, "victim")
            open(target, "w").close()
            os.remove(p)
            os.symlink(target, p)
            nmkeyfile.write_profile(d, "opentv-wifi", "[connection]\nid=opentv-wifi\n")
            self.assertFalse(os.path.islink(p))
            self.assertEqual(_read(target), "")


class FakeRunner:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, argv, timeout):
        self.assertable = True
        self.calls.append(list(argv))
        key = " ".join(argv)
        for f in self.fail:
            if f in key:
                return 4, "Error"
        return 0, ""


class HelperEnv:
    def __init__(self, tc):
        self.tmp = tempfile.TemporaryDirectory()
        b = self.tmp.name
        self.jobs, self.tmpdir, self.results, self.nm = (os.path.join(b, n) for n in ("jobs", "jobs-tmp", "results", "nm"))
        for d in (self.jobs, self.tmpdir, self.results, self.nm):
            os.makedirs(d, mode=0o700)
        self.clock = Clock(5000.0)
        self.logs = []
        self.runner = FakeRunner()
        tc.addCleanup(self.tmp.cleanup)

    def helper(self, runner=None, owner=None):
        return nethelper.Helper(self.jobs, self.results, self.nm, owner_uid=os.getuid() if owner is None else owner,
                                runner=runner or self.runner, clock=self.clock, sleep=lambda s: None,
                                log=lambda ev, **f: self.logs.append(dict(f, event=ev)), chown=False)

    def submit(self, action, payload):
        return netjobs.submit(self.jobs, self.tmpdir, action, payload, clock=self.clock)


class TestNetJobsAndHelper(unittest.TestCase):
    def test_submit_is_private_and_atomic(self):
        e = HelperEnv(self)
        jid = e.submit("wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK, "hidden": False, "delay_s": 0})
        p = os.path.join(e.jobs, jid + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(os.listdir(e.tmpdir), [])
        with self.assertRaises(ValueError):
            e.submit("reboot", {})
        with self.assertRaises(ValueError):
            e.submit("wifi-apply", {"ssid": "x;y", "psk": "12345678"})

    def test_wifi_apply_consumes_once_and_never_leaks_secret(self):
        e = HelperEnv(self)
        jid = e.submit("wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK, "hidden": False, "delay_s": 0})
        res = e.helper().run_once()
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"], res)
        self.assertEqual(os.listdir(e.jobs), [])                       # consommé
        self.assertEqual(e.helper().run_once(), [])                    # aucune seconde exécution
        prof = os.path.join(e.nm, "opentv-wifi.nmconnection")
        self.assertEqual(stat.S_IMODE(os.stat(prof).st_mode), 0o600)
        self.assertEqual(kf_value(_read(prof), "wifi-security", "psk"), SECRET_PSK)
        for argv in e.runner.calls:
            self.assertEqual(argv[0], "nmcli")
            for a in argv:
                self.assertNotIn("Tr0p", a)
                self.assertNotIn("Maison", a)                          # le SSID ne va que dans le keyfile
        cand = os.path.join(e.nm, "opentv-wifi-candidate.nmconnection")
        self.assertFalse(os.path.lexists(cand))                        # candidat promu puis retiré
        self.assertEqual(kf_value(_read(prof), "connection", "id"), "opentv-wifi")
        ups = [c for c in e.runner.calls if c[3:6] == ["connection", "up", "uuid"]]
        self.assertEqual([c[:3] for c in ups], [["nmcli", "--wait", str(nethelper.WIFI_UP_WAIT_S)]])
        self.assertEqual(kf_value(_read(prof), "connection", "uuid"), ups[0][-1])
        self.assertEqual(e.runner.calls[0], ["nmcli", "connection", "load", cand])
        self.assertIn(["nmcli", "connection", "load", prof, cand], e.runner.calls)
        rfile = os.path.join(e.results, jid + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(rfile).st_mode), 0o600)
        blob = _read(rfile) + json.dumps(e.logs)
        self.assertNotIn("Tr0p", blob)
        self.assertNotIn("Maison", blob)

    def test_failed_wifi_restores_ap_and_removes_profile(self):
        e = HelperEnv(self)
        e.submit("wifi-apply", {"ssid": "Maison", "psk": "12345678", "hidden": False, "delay_s": 2, "restore_ap": True})
        runner = FakeRunner(fail=("connection up uuid",))           # le candidat est refusé
        res = e.helper(runner=runner).run_once()
        self.assertFalse(res[0]["ok"])
        self.assertEqual(res[0]["error"], "wifi_connect_failed")
        self.assertFalse(os.path.exists(os.path.join(e.nm, "opentv-wifi.nmconnection")))
        self.assertFalse(os.path.exists(os.path.join(e.nm, "opentv-wifi-candidate.nmconnection")))
        up = next(c for c in runner.calls if c[3:6] == ["connection", "up", "uuid"])
        self.assertIn(["nmcli", "connection", "delete", "uuid", up[-1]], runner.calls)
        self.assertEqual(runner.calls[-1], ["nmcli", "--wait", str(nethelper.AP_UP_WAIT_S), "connection", "up", "id",
                                            "opentv-setup-ap"])

    def test_ap_start_and_stop(self):
        e = HelperEnv(self)
        e.submit("ap-start", {"ssid": "OpenTV-AB12", "psk": "abcdefgh2345"})
        self.assertTrue(e.helper().run_once()[0]["ok"])
        self.assertTrue(os.path.exists(os.path.join(e.nm, "opentv-setup-ap.nmconnection")))
        e.submit("ap-stop", {})
        self.assertTrue(e.helper().run_once()[0]["ok"])
        self.assertFalse(os.path.exists(os.path.join(e.nm, "opentv-setup-ap.nmconnection")))
        self.assertIn(["nmcli", "connection", "delete", "id", "opentv-setup-ap"], e.runner.calls)

    def _raw_job(self, e, name, body, mode=0o600):
        p = os.path.join(e.jobs, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.write(fd, body.encode())
        os.close(fd)
        os.chmod(p, mode)
        return p

    def test_rejects_bad_jobs_and_still_consumes(self):
        e = HelperEnv(self)
        good = {"v": 1, "id": "abcdefgh12", "action": "ap-stop", "created": e.clock.t, "payload": {}}
        self._raw_job(e, "abcdefgh12.json", json.dumps(good), mode=0o644)                     # mode trop ouvert
        self._raw_job(e, "zzzzzzzz12.json", json.dumps(dict(good, id="yyyyyyyy12")))             # id ≠ nom
        self._raw_job(e, "oldoldold1.json", json.dumps(dict(good, id="oldoldold1", created=e.clock.t - 3600)))
        self._raw_job(e, "badact0001.json", json.dumps(dict(good, id="badact0001", action="shell")))
        self._raw_job(e, "notjson001.json", "{nope")
        self._raw_job(e, "huge000001.json", "x" * 10000)
        target = os.path.join(e.tmp.name, "outside.json")
        with open(target, "w") as f:
            json.dump(dict(good, id="symlink001"), f)
        os.symlink(target, os.path.join(e.jobs, "symlink001.json"))
        res = e.helper().run_once()
        self.assertEqual(len(res), 6)                             # le lien est retiré comme résidu, sans résultat
        self.assertTrue(all(not r["ok"] for r in res), res)
        self.assertEqual(os.listdir(e.jobs), [])
        self.assertTrue(os.path.exists(target))                   # la cible du lien n'est ni lue ni supprimée
        self.assertEqual(e.runner.calls, [])                      # aucune commande exécutée

    def test_wrong_owner_rejected(self):
        e = HelperEnv(self)
        e.submit("ap-stop", {})
        res = e.helper(owner=os.getuid() + 12345).run_once()
        self.assertEqual(res[0]["error"], "bad_owner")
        self.assertEqual(e.runner.calls, [])

    def test_default_runner_never_uses_shell(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "pwned")
            rc, out = nethelper.default_runner([sys.executable, "-c", "import sys; print(sys.argv[1])",
                                                "$(touch %s); `touch %s`" % (marker, marker)], 10)
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(marker))
            self.assertIn("$(touch", out)

    def test_read_results_consumes(self):
        e = HelperEnv(self)
        e.submit("ap-stop", {})
        e.helper().run_once()
        r = netjobs.read_results(e.results)
        self.assertEqual(len(r), 1)
        self.assertEqual(netjobs.read_results(e.results), [])


NMCLI_DEV = """eth0:ethernet:connected:Wired connection 1
wlan0:wifi:disconnected:
lo:loopback:connected (externally):lo
p2p-dev-wlan0:wifi-p2p:disconnected:
"""
IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.2.40/24 brd 192.168.2.255 scope global dynamic noprefixroute eth0\\       valid_lft 86000sec
"""


class TestNetState(unittest.TestCase):
    def runner(self, dev, ip):
        def run(argv, timeout):
            if argv[:2] == ["nmcli", "-t"]:
                return 0, dev
            if argv[:2] == ["ip", "-4"]:
                return 0, ip
            return 1, ""
        return run

    def test_parse_escaped_colons(self):
        rows = netstate.parse_device_status("wlan0:wifi:connected:Café\\:Maison\n")
        self.assertEqual(rows[0]["connection"], "Café:Maison")

    def test_ethernet(self):
        s = netstate.snapshot(self.runner(NMCLI_DEV, IP_ADDR))
        self.assertEqual(s["mode"], "ethernet")
        self.assertEqual(s["ip"], "192.168.2.40")
        self.assertTrue(s["wifi_present"])

    def test_wifi_hotspot_offline(self):
        dev = "eth0:ethernet:unavailable:\nwlan0:wifi:connected:opentv-wifi\n"
        ip = "3: wlan0    inet 192.168.2.41/24 brd 192.168.2.255 scope global wlan0\n"
        self.assertEqual(netstate.snapshot(self.runner(dev, ip))["mode"], "wifi")
        dev = "eth0:ethernet:unavailable:\nwlan0:wifi:connected:opentv-setup-ap\n"
        ip = "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\n"
        s = netstate.snapshot(self.runner(dev, ip))
        self.assertEqual((s["mode"], s["ip"]), ("hotspot", "10.42.0.1"))
        dev = "eth0:ethernet:connected:Wired\n"
        ip = "2: eth0    inet 169.254.3.4/16 scope link eth0\n"
        self.assertEqual(netstate.snapshot(self.runner(dev, ip))["mode"], "offline")

    def test_runner_failure_is_offline(self):
        s = netstate.snapshot(lambda argv, timeout: (127, ""))
        self.assertEqual(s["mode"], "offline")
        self.assertFalse(s["wifi_present"])


if __name__ == "__main__":
    unittest.main()
