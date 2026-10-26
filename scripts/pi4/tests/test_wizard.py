"""Assistant QR en 3 étapes (téléphone) : sémantique des étapes, échappement, CSP, secret du jeton et du mot de passe,
liste des Wi-Fi proches (argv fixe, jeton exigé, sortie validée, cache/débit bornés)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from tests._common import PI4, ROOT, SECRET_PSK
from tests.test_service import AP_DEV, AP_IP, Net, Servers, make_app, token_of
from opentv_pi import app as appmod, nethelper, web, wifiscan

EVIL = "\"><svg onload=alert(1)>"
WIFI_LIST = ("Maison:82:WPA2\n"
             "Maison:40:WPA2\n"                       # doublon (autre borne) : fusionné, meilleur signal
             "Voisin\\:Bas:55:WPA1 WPA2\n"            # « : » échappé par nmcli -t
             "%s:30:WPA2\n"                           # SSID piégé : affiché échappé
             "CafeOuvert:90:\n"                       # ouvert : exclu (le formulaire exige un mot de passe WPA)
             "Bureau:70:WPA2 802.1X\n"                # entreprise : exclu
             "Vieux:60:WEP\n"                         # WEP : exclu
             "Moderne:65:WPA3\n"                      # WPA3 seul : exclu (profil wpa-psk)
             "OpenTV-AB12:99:WPA2\n"                  # notre propre Wi-Fi temporaire : exclu
             "a;b:50:WPA2\n"                          # « ; » refusé par validate.ssid
             ":45:WPA2\n"                             # masqué (sans nom) : exclu
             "Bizarre:45:WPA9\n"                      # sécurité inconnue : exclue
             "Fort:101:WPA2\n"                        # signal hors bornes : exclu
             "Tronq" % EVIL)                          # dernière ligne non terminée (sortie tronquée) : ignorée


class WifiNet(Net):
    """Faux nmcli : état réseau (Net) + liste Wi-Fi pour l'argv exact de wifiscan."""

    def __init__(self, listing=WIFI_LIST, rc=0, gate=None, **kw):
        super().__init__(**kw)
        self.listing, self.rc, self.gate, self.scans = listing, rc, gate, []

    def __call__(self, argv, timeout):
        if list(argv) == wifiscan.NMCLI_WIFI_LIST:
            self.scans.append((list(argv), timeout))
            if self.gate is not None:
                self.gate.wait(5)
            return self.rc, self.listing
        return super().__call__(argv, timeout)


def q(path, **params):
    return path + "?" + urllib.parse.urlencode(params)


def finish_form(body):
    m = re.search(r"<form method=\"post\" action=\"/setup\" id=\"finish\".*?</form>", body, re.S)
    assert m, body
    return m.group(0)


class TestScanParsing(unittest.TestCase):
    def test_parse_validates_dedupes_sorts_and_bounds(self):
        nets = wifiscan.parse(WIFI_LIST)
        self.assertEqual([n["ssid"] for n in nets], ["Maison", "Voisin:Bas", EVIL])
        self.assertEqual(nets[0]["signal"], 82)
        many = "".join("Reseau%02d:%d:WPA2\n" % (i, i) for i in range(40))
        out = wifiscan.parse(many)
        self.assertEqual(len(out), wifiscan.MAX_NETWORKS)
        self.assertEqual(out[0]["ssid"], "Reseau39")
        for junk in ("", None, 42, "\n\n", "x" * 5000, "Maison:82\n", "Maison:82:WPA2:extra\n", "\x00:1:WPA2\n",
                     " Maison:50:WPA2\n", ("é" * 17) + ":50:WPA2\n"):
            self.assertEqual(wifiscan.parse(junk), [], repr(junk)[:40])

    def test_bars(self):
        self.assertEqual([wifiscan.bars(x) for x in (0, 24, 25, 50, 74, 75, 100)], [1, 1, 2, 3, 3, 4, 4])


class TestScanner(unittest.TestCase):
    def test_real_runner_preserves_utf8_ssids(self):
        proc = mock.Mock()
        proc.communicate.return_value = ("Café:88:WPA2\n".encode("utf-8"), None)
        proc.returncode = 0
        with mock.patch.object(nethelper.subprocess, "Popen", return_value=proc) as popen:
            nets = wifiscan.Scanner(nethelper.default_runner).networks()
        self.assertEqual(nets, [{"ssid": "Café", "signal": 88}])
        self.assertEqual(popen.call_args.kwargs["env"]["LC_ALL"], "C.UTF-8")

    def test_fixed_argv_cache_and_throttle(self):
        clock = [1000.0]
        calls = []

        def runner(argv, timeout):
            calls.append((argv, timeout))
            return 0, "Maison:80:WPA2\n"
        sc = wifiscan.Scanner(runner, mono=lambda: clock[0])
        self.assertEqual(sc.networks(), [{"ssid": "Maison", "signal": 80}])
        for _ in range(50):
            sc.networks()
        self.assertEqual(len(calls), 1)
        argv, timeout = calls[0]
        self.assertEqual(argv, ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                "--rescan", "no"])
        self.assertLessEqual(timeout, 3)
        clock[0] += wifiscan.CACHE_TTL_S + 1
        sc.networks()
        self.assertEqual(len(calls), 2)
        sc.networks()[0]["ssid"] = "modifié"   # copie : le cache n'est pas altérable par l'appelant
        self.assertEqual(sc.networks()[0]["ssid"], "Maison")

    def test_failure_is_negatively_cached_and_never_raises(self):
        clock = [0.0]
        calls = []

        def runner(argv, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return 4, "Error: NetworkManager is not running."
        sc = wifiscan.Scanner(runner, mono=lambda: clock[0])
        self.assertEqual(sc.networks(), [])
        self.assertEqual(sc.networks(), [])
        self.assertEqual(len(calls), 1)
        clock[0] += wifiscan.CACHE_TTL_S + 1
        self.assertEqual(sc.networks(), [])
        self.assertEqual(len(calls), 2)

    def test_single_flight_concurrent_callers_never_wait(self):
        gate, started = threading.Event(), threading.Event()
        calls = []

        def runner(argv, timeout):
            calls.append(1)
            started.set()
            gate.wait(5)
            return 0, "Maison:80:WPA2\n"
        sc = wifiscan.Scanner(runner)
        t = threading.Thread(target=sc.networks)
        t.start()
        self.assertTrue(started.wait(5))
        others = [sc.networks() for _ in range(20)]   # pendant le balayage : réponse immédiate (cache vide)
        gate.set()
        t.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(others, [[]] * 20)
        self.assertEqual(sc.networks(), [{"ssid": "Maison", "signal": 80}])


class TestScanEndpointSecurity(unittest.TestCase):
    def test_bad_token_never_runs_nmcli(self):
        net = WifiNet()
        a = make_app(self, net=net)
        s = Servers(self, a)
        for path in ("/setup", "/setup?t=faux-jeton-xxxxxxxxxxxx", q("/setup", t="x" * 43, net="other"),
                     "/setup?t=" + "%00" * 20):
            code, _, body = s.req("GET", path, lan=True)
            self.assertEqual(code, 403, path)
            self.assertNotIn("Maison", body)
        self.assertEqual(net.scans, [])
        self.assertIsNone(a.setup_networks("faux-jeton-xxxxxxxxxxxx"))
        self.assertEqual(net.scans, [])

    def test_valid_token_lists_networks_escaped_cached_and_not_consumed(self):
        net = WifiNet()
        a = make_app(self, net=net)
        s = Servers(self, a)
        tok = token_of(a)
        for _ in range(30):
            code, h, body = s.req("GET", q("/setup", t=tok), lan=True)
            self.assertEqual(code, 200)
        self.assertEqual(len(net.scans), 1)                                  # cache : un seul nmcli
        self.assertIn("value=\"Maison\"", body)
        self.assertIn("Voisin:Bas", body)
        self.assertNotIn("<svg", body)                                       # SSID piégé échappé
        self.assertIn("&quot;&gt;&lt;svg onload=alert(1)&gt;", body)
        for gone in ("CafeOuvert", "Bureau", "Vieux", "Moderne", "OpenTV-AB12", "a;b", "Bizarre", "Fort", "Tronq"):
            self.assertNotIn(gone, body)
        self.assertEqual(token_of(a), tok)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertNotIn(tok, json.dumps(a.test_logs))

    def test_no_scan_while_applying_or_rate_limited(self):
        net = WifiNet()
        a = make_app(self, net=net)
        s = Servers(self, a)
        tok = token_of(a)
        for i in range(appmod.BAD_TOKEN_PER_IP):
            s.req("POST", "/setup", {"t": "faux-jeton-%03d-xxxxxxxxxxxx" % i, "net": "keep"}, lan=True)
        self.assertEqual(s.req("GET", q("/setup", t=tok), lan=True)[0], 403)   # adresse limitée : pas de nmcli
        self.assertEqual(net.scans, [])
        a.bad_token_ips.clear()
        code, _, _ = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "wifi", "ssid": "Maison",
                                              "psk": SECRET_PSK}, lan=True)
        self.assertEqual(code, 200)
        self.assertEqual(a.tv_state()["setup"]["phase"], "applying")
        self.assertIsNone(a.setup_networks(tok))
        self.assertEqual(net.scans, [])

    def test_slow_scan_never_holds_the_service_lock(self):
        gate = threading.Event()
        net = WifiNet(gate=gate)
        a = make_app(self, net=net)
        a.tick()
        tok = token_of(a)
        t = threading.Thread(target=a.setup_networks, args=(tok,))
        t.start()
        try:
            for _ in range(100):
                if net.scans:
                    break
                threading.Event().wait(0.01)
            self.assertTrue(net.scans)
            got = []
            w = threading.Thread(target=lambda: got.append(a.tv_state()))   # la boucle/TV ne sont pas bloquées
            w.start()
            w.join(2)
            self.assertTrue(got)
        finally:
            gate.set()
            t.join(5)


class TestHotspotPrime(unittest.TestCase):
    def test_offline_prime_then_hotspot_offers_last_list(self):
        from tests.test_service import OFF_DEV
        net = WifiNet(dev=OFF_DEV, ip="")
        a = make_app(self, net=net)
        a.tick()                                                   # hors ligne : relevé en thread (hors boucle)
        a._scan_thread.join(5)
        self.assertEqual(len(net.scans), 1)
        a.tick()
        self.assertEqual(len(net.scans), 1)                        # au plus un relevé par SCAN_PRIME_S
        # le point d'accès démarre : wlan0 ne voit plus rien, la dernière liste non vide est proposée
        net.dev, net.ip, net.listing = AP_DEV, AP_IP, ""
        a.mono_clock.t += wifiscan.CACHE_TTL_S + 1
        a.tick()
        self.assertEqual(a.net["mode"], "hotspot")
        nets = a.setup_networks(token_of(a))
        self.assertEqual([n["ssid"] for n in nets], ["Maison", "Voisin:Bas", EVIL])
        self.assertEqual(len(net.scans), 2)
        a.mono_clock.t += wifiscan.STALE_MAX_S + 1                 # trop vieille : plus proposée
        a.tick()                                                   # nouveau QR (l'ancien a expiré entre-temps)
        self.assertEqual(a.setup_networks(token_of(a)), [])

    def test_no_prime_when_online_or_configured(self):
        net = WifiNet()
        a = make_app(self, net=net)
        a.tick()
        self.assertIsNone(a._scan_thread)                          # Ethernet : balayage seulement à la demande
        self.assertEqual(net.scans, [])

    def test_stale_list_never_offered_outside_hotspot(self):
        clock = [0.0]
        listing = ["Maison:80:WPA2\n"]
        sc = wifiscan.Scanner(lambda argv, t: (0, listing[0]), mono=lambda: clock[0])
        self.assertTrue(sc.networks())
        listing[0] = ""
        clock[0] += wifiscan.CACHE_TTL_S + 1
        self.assertEqual(sc.networks(), [])
        self.assertEqual(sc.networks(allow_stale=True), [{"ssid": "Maison", "signal": 80}])


class TestWizardSemantics(unittest.TestCase):
    def setUp(self):
        self.net = WifiNet()
        self.a = make_app(self, net=self.net)
        self.s = Servers(self, self.a)
        self.tok = token_of(self.a)

    def get(self, **params):
        return self.s.req("GET", q("/setup", t=self.tok, **params), lan=True)

    def test_step1_big_choices_no_primary_and_no_js_needed(self):
        code, _, body = self.get()
        self.assertEqual(code, 200)
        self.assertIn("Étape 1 sur 3", body)
        self.assertNotIn("class=\"primary\"", body)                           # on touche son Wi-Fi, c'est tout
        self.assertNotIn("<script", body)                                     # l'étape 1 n'a aucun script
        self.assertEqual(body.count("class=\"choice\" name=\"ssid\""), 3)
        self.assertIn("name=\"net\" value=\"wifi\"", body)
        self.assertIn("Mon Wi-Fi n'est pas dans la liste", body)
        self.assertIn("Garder le câble Ethernet", body)
        self.assertNotIn("onclick", body)
        self.assertNotIn("type=\"button\"", body)
        self.assertNotIn("psk", body)

    def test_step1_without_networks_offers_manual_entry(self):
        self.net.listing = ""
        self.a.wifi_scan = wifiscan.Scanner(self.net)
        _, _, body = self.get()
        self.assertIn("Aucun Wi-Fi trouvé", body)
        self.assertIn("Écrire le nom de mon Wi-Fi", body)

    def test_step2_scanned_network_password_only(self):
        code, h, body = self.get(net="wifi", ssid="Maison")
        self.assertEqual(code, 200)
        self.assertIn("Étape 2 sur 3", body)
        self.assertIn("<script src=\"/static/setup.js\" defer></script>", body)
        f = finish_form(body)
        self.assertEqual(f.count("class=\"primary\""), 1)
        self.assertIn("Connecter la TV", f)
        self.assertIn("<input type=\"hidden\" name=\"ssid\" value=\"Maison\">", f)
        self.assertIn("<input type=\"hidden\" name=\"net\" value=\"wifi\">", f)
        self.assertRegex(f, r"<input name=\"psk\" id=\"psk\" type=\"password\" minlength=\"8\" "
                            r"maxlength=\"64\" required")
        self.assertNotIn("name=\"hidden\"", f)
        # défauts sûrs : aucun agent, Muse décochée, nom « Salon »
        self.assertRegex(f, r"<input type=\"checkbox\" name=\"agent_muse\" value=\"feedback\">")
        for aid in ("hermit", "hermes"):
            sel = re.search(r"<select name=\"agent_%s\">(.*?)</select>" % aid, f).group(1)
            self.assertIn("<option value=\"none\" selected>", sel)
        self.assertIn("<details class=\"adv\"><summary>", f)                   # options avancées repliées
        self.assertIn("value=\"Salon\"", f)
        self.assertEqual(self.net.scans, [])                                   # pas de balayage à l'étape 2
        self.assertEqual(token_of(self.a), self.tok)

    def test_step2_keep_and_other_and_invalid_choices(self):
        _, _, body = self.get(net="keep")
        f = finish_form(body)
        self.assertIn("Terminer", f)
        self.assertNotIn("psk", f)
        self.assertIn("name=\"net\" value=\"keep\"", f)
        _, _, body = self.get(net="other")
        f = finish_form(body)
        self.assertIn("<input name=\"ssid\" id=\"ssid\" maxlength=\"32\" required", f)
        self.assertIn("name=\"hidden\" value=\"1\"", f)
        _, _, body = self.get(net="wifi", ssid="a;b")
        self.assertIn("Étape 1 sur 3", body)
        self.assertIn("Nom du Wi-Fi invalide", body)
        _, _, body = self.get(net="wifi")
        self.assertIn("Étape 1 sur 3", body)
        _, _, body = self.get(net="nimporte")
        self.assertIn("Étape 1 sur 3", body)
        self.assertEqual(self.s.req("GET", "/setup?t=%s&net=keep&net=wifi" % self.tok, lan=True)[0], 200)
        self.assertEqual(self.s.req("GET", "/setup?t=%s&net=wifi&ssid=x&e=1" % self.tok, lan=True)[0], 200)
        self.assertEqual(self.s.req("GET", "/setup?t=%s&net=wifi&ssid=x&e=1&f=2" % self.tok, lan=True)[0], 400)

    def test_keep_refused_when_offline_hotspot(self):
        net = WifiNet(dev=AP_DEV, ip=AP_IP)
        a = make_app(self, net=net)
        a.tick()
        tok = token_of(a)
        ctx = a.setup_context()
        self.assertEqual(ctx["net_mode"], "hotspot")
        plan = web.wizard_plan(ctx, choice=("keep", None))
        self.assertEqual((plan["step"], plan["error"]), (1, appmod.ERR_FORM_OFFLINE))
        page = web.setup_form(tok, ctx, networks=[], plan=web.wizard_plan(ctx))
        self.assertNotIn("Garder", page)

    def test_escaping_of_query_ssid_and_echoed_name(self):
        _, _, body = self.get(net="wifi", ssid=EVIL)
        self.assertNotIn("<svg", body)
        self.assertIn("value=\"&quot;&gt;&lt;svg onload=alert(1)&gt;\"", body)
        _, _, body = self.get(net="other", ssid=EVIL)
        self.assertNotIn("<svg", body)
        code, _, body = self.s.req("POST", "/setup", {"t": self.tok, "name": "<b onmouseover=x>", "net": "keep"},
                                   lan=True)
        self.assertEqual(code, 400)
        self.assertNotIn("<b onmouseover", body)
        self.assertIn("&lt;b onmouseover=x&gt;", body)
        self.assertIn("<details class=\"adv\" open>", body)                    # erreur de nom : options ouvertes

    def test_full_flow_defaults_to_no_agent(self):
        _, _, body = self.get(net="wifi", ssid="Maison")
        f = finish_form(body)
        fields = dict(re.findall(r"<input type=\"hidden\" name=\"(\w+)\" value=\"([^\"]*)\">", f))
        fields.update(psk=SECRET_PSK, name="Salon", agent_hermit="none", agent_hermes="none")
        self.assertEqual(set(fields), {"t", "net", "ssid", "psk", "name", "agent_hermit", "agent_hermes"})
        code, _, body = self.s.req("POST", "/setup", fields, lan=True)
        self.assertEqual(code, 200, body)
        self.assertIn("Étape 3 sur 3", body)
        self.assertNotIn(self.tok, body)
        self.assertNotIn("Tr0p", body)
        self.assertEqual(self.a.registry.policy(), {"hermes": [], "hermit": [], "muse": []})
        jobs = os.listdir(self.a.cfg.jobs_dir)
        with open(os.path.join(self.a.cfg.jobs_dir, jobs[0])) as fh:
            self.assertEqual(json.load(fh)["payload"]["ssid"], "Maison")
        self.assertNotIn(SECRET_PSK, json.dumps(self.a.test_logs) + json.dumps(self.a.tv_state()))
        self.assertNotIn(self.tok, json.dumps(self.a.test_logs))

    def test_muse_checkbox_grants_feedback_only(self):
        code, _, body = self.s.req("POST", "/setup", {"t": self.tok, "name": "Salon", "net": "keep",
                                                      "agent_muse": "feedback"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.a.registry.policy(), {"hermes": [], "hermit": [], "muse": ["feedback", "read"]})

    def test_reconfigure_preserves_policy_in_defaults(self):
        self.a.registry.set_policy({"muse": ["display", "feedback", "read"], "hermit": ["read"], "hermes": []})
        _, _, body = self.get(net="keep")
        f = finish_form(body)
        self.assertNotIn("type=\"checkbox\" name=\"agent_muse\"", f)            # niveau non binaire : liste
        self.assertIn("<option value=\"full\" selected>", re.search(r"name=\"agent_muse\">(.*?)</select>", f).group(1))
        self.assertIn("<option value=\"read\" selected>",
                      re.search(r"name=\"agent_hermit\">(.*?)</select>", f).group(1))
        self.assertIn("<details class=\"adv\" open>", f)
        self.a.registry.set_policy({"muse": ["feedback", "read"], "hermit": [], "hermes": []})
        f = finish_form(self.get(net="keep")[2])
        self.assertIn("<input type=\"checkbox\" name=\"agent_muse\" value=\"feedback\" checked>", f)
        self.assertIn("<details class=\"adv\">", f)

    def test_password_never_echoed_after_errors(self):
        for form in ({"name": "<b>", "net": "wifi", "ssid": "Maison", "psk": SECRET_PSK},
                     {"name": "Salon", "net": "wifi", "ssid": "Maison", "psk": "court!"},
                     {"name": "Salon", "net": "wifi", "ssid": "a;b", "psk": SECRET_PSK, "hidden": "1"},
                     {"name": "Salon", "net": "wifi", "ssid": "Maison", "psk": SECRET_PSK, "agent_muse": "root"}):
            code, _, body = self.s.req("POST", "/setup", dict(form, t=self.tok), lan=True)
            self.assertEqual(code, 400)
            self.assertNotIn(form["psk"], body)
            self.assertNotIn("Tr0p", body)
            self.assertIsNone(re.search(r"<input[^>]*name=\"psk\"[^>]*value=", body))
            self.assertIn("Étape 2 sur 3", body)
        self.assertNotIn("Tr0p", json.dumps(self.a.test_logs))
        self.assertNotIn("court!", json.dumps(self.a.test_logs))
        # le 4e essai était faux : le 5e (valide) passe encore — les GET intermédiaires n'ont rien consommé
        for _ in range(20):
            self.get(net="wifi", ssid="Maison")
        code, _, body = self.s.req("POST", "/setup", {"t": self.tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200, body)

    def test_error_step_mapping(self):
        ctx = self.a.setup_context()
        plan = web.wizard_plan(ctx, appmod.ERR_FORM_SSID, {"net": "wifi", "ssid": "a;b"})
        self.assertEqual((plan["step"], plan["mode"]), (2, "other"))
        plan = web.wizard_plan(ctx, appmod.ERR_FORM_PSK, {"net": "wifi", "ssid": "Maison"})
        self.assertEqual((plan["step"], plan["mode"], plan["ssid"]), (2, "wifi", "Maison"))
        plan = web.wizard_plan(ctx, appmod.ERR_FORM_PSK, {"net": "wifi", "ssid": "Cache", "hidden": "1"})
        self.assertEqual(plan["mode"], "other")
        plan = web.wizard_plan(ctx, appmod.ERR_FORM_NET, {"net": "x"})
        self.assertEqual(plan["step"], 1)

    def test_five_valid_token_failures_still_burn(self):
        for _ in range(5):
            self.assertEqual(self.s.req("POST", "/setup", {"t": self.tok, "name": "Salon", "net": "wifi",
                                                           "ssid": "Maison", "psk": "x"}, lan=True)[0], 400)
        self.assertEqual(self.s.req("POST", "/setup", {"t": self.tok, "name": "Salon", "net": "keep"},
                                    lan=True)[0], 403)
        self.assertEqual(self.get()[0], 403)


class TestSetupStaticAndCsp(unittest.TestCase):
    def test_lan_static_allowlist_and_csp(self):
        a = make_app(self, net=WifiNet())
        s = Servers(self, a)
        code, h, js = s.req("GET", "/static/setup.js", lan=True)
        self.assertEqual(code, 200)
        self.assertTrue(h["content-type"].startswith("application/javascript"))
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("script-src 'self';", h["content-security-policy"])
        self.assertEqual(s.req("GET", "/static/setup.css", lan=True)[0], 200)
        for bad in ("/static/tv.js", "/static/qrcode.js", "/static/tv.html", "/static/../opentv_pi/web.py",
                    "/static/", "/static/setup.js/"):
            self.assertEqual(s.req("GET", bad, lan=True)[0], 404, bad)
        self.assertNotIn("setup.js", web.TV_STATIC)
        self.assertNotIn("unsafe", web.SETUP_CSP)
        self.assertIn("form-action 'self'", web.SETUP_CSP)
        self.assertIn("default-src 'none'", web.SETUP_CSP)

    def test_setup_js_is_inert_and_parses(self):
        with open(os.path.join(PI4, "ui", "setup.js"), encoding="utf-8") as f:
            src = f.read()
        for bad in ("fetch", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "console",
                    "eval(", "innerHTML", "document.write", "cookie", "location"):
            self.assertNotIn(bad, src)
        node = shutil.which("node")
        if node:
            p = subprocess.run([node, "--check", os.path.join(PI4, "ui", "setup.js")], capture_output=True,
                               timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_installed_with_manifest(self):
        r = tempfile.mkdtemp(prefix="otvw", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        p = subprocess.run(["bash", os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"],
                           capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            self.assertIn("/opt/opentv/share/ui/setup.js", f.read())
        with open(os.path.join(r, "opt/opentv/share/ui/setup.js"), "rb") as f1, \
                open(os.path.join(PI4, "ui", "setup.js"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        self.assertTrue(os.path.isdir(ROOT))
