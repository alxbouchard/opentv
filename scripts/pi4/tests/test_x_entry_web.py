"""X TV — saisie de l'identifiant X par téléphone (0.7.10) : extension, page du téléphone, installation, bout en bout.

- statique : xentry.js ne LIT jamais la valeur d'un champ, n'appelle jamais click/submit, exactement UN « input » et UN
  « change », clé privée non exportable ; background.js relaie des champs FERMÉS ; xphone.js efface le fragment avant
  tout réseau, n'a aucun stockage, aucun repli en clair ; pages et scripts en ES5/ES2020 (Acorn) ;
- harnais Node (tests/x_entry_harness.js) : VRAIS muse.js + xphone.js et bloc <gate> de xentry.js ; mutations des
  contrôles de sécurité (empreinte, e, taille de n, secret, séance, libellés, autocomplete, URL…) : chacune DÉTECTÉE ;
- installation : fichiers manifestés, 0644, identiques au dépôt ; retirés par uninstall.sh ;
- bout en bout : Chromium complet réel + extension installée + vrai service + téléphones séparés ; QR décodé sur capture.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest

from tests._common import PI4
from tests.test_remote import MDNS, remote_app, remote_servers
from tests.test_x_tv import EXT, EXT_DST, INSTALL, UNINSTALL, NODE, code_only, full_chrome, read, sh
from opentv_pi import VERSION, config, xcontext, xentry, xescape, xprobe

XENTRY_JS = os.path.join(EXT, "xentry.js")


def no_comments(js):
    """Source JavaScript sans commentaires (blocs /* */ et lignes //) : les contrôles portent sur le CODE seul."""
    return code_only(re.sub(r"/\*.*?\*/", "", js, flags=re.S))


XPHONE_JS = os.path.join(PI4, "ui", "xphone.js")
HARNESS = os.path.join(PI4, "tests", "x_entry_harness.js")
try:
    import cv2   # décodage du QR sur la capture (facultatif : bout en bout sauté sans lui)
    import numpy
except ImportError:   # pragma: no cover - selon l'hôte
    cv2 = numpy = None


class TestStatic(unittest.TestCase):
    def test_extension_never_reads_value_clicks_or_submits(self):
        src = no_comments(read(XENTRY_JS))
        self.assertIsNone(re.search(r"\.value\b", src))       # valeur jamais lue (setter natif seulement)
        self.assertEqual(src.count('Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call('), 1)
        self.assertEqual(re.findall(r'dispatchEvent\(new Event\("(\w+)", \{ bubbles: true \}\)\)', src),
                         ["input", "change"])
        for bad in (".click(", ".submit(", "requestSubmit", "localStorage", "sessionStorage", "indexedDB",
                    "storage.local", "storage.session", "console.", "innerHTML", "eval(", "new Function",
                    "postMessage", "navigator.credentials", 'mode: "open"', "fetch("):
            self.assertFalse(bad in src, bad)
        self.assertIn('hash: "SHA-256" }, false, ["encrypt", "decrypt"])', src)   # clé privée non exportable
        self.assertIn("if (pair.privateKey.extractable !== false)", src)
        self.assertIn('attachShadow({ mode: "closed" })', src)                     # QR invisible pour la page
        # messages vers background.js : champs FERMÉS, jamais l'identifiant, le secret ni l'empreinte
        for m in re.findall(r"send\(\{([^}]*)\}\)", src):
            keys = sorted(k.strip().split(":")[0] for k in m.split(",") if k.strip())
            self.assertTrue(set(keys) <= {"type", "op", "n", "e", "sid", "result"}, keys)
        self.assertIn("if (!e || e.isTrusted !== true) { return false; }", src)
        nav = code_only(read(os.path.join(EXT, "nav.js")))
        # armement : seulement après la porte isTrusted de nav.js, et par une flèche RÉELLE (navFocus dans loginKey)
        self.assertLess(nav.index("if (!e.isTrusted) { return; }"), nav.index("eh.key(e) === true"))
        self.assertEqual(nav.count("h.navFocus(document.activeElement)"), 1)

    def test_background_relay_closed_and_tv_flag(self):
        bg = code_only(read(os.path.join(EXT, "background.js")))
        body = bg[bg.index("function xentryBody(msg)"):bg.index("async function xentry(msg)")]
        self.assertEqual(sorted(set(re.findall(r"\{ ((?:\w+: [^,}]+(?:, )?)+) \}", body))),
                         ['n: msg.n, e: "AQAB"', "sid: msg.sid", "sid: msg.sid, result: msg.result"])
        self.assertIn("if (s.xentry !== true) { return { ok: false }; }", bg)
        self.assertIn("xentry: msg.entry === true", bg)
        self.assertIn('if (msg.type === "xentry" && fromX(sender)) {', bg)
        tk = code_only(read(os.path.join(EXT, "tvkey.js")))
        self.assertIn('entry.content === "1"', tk)

    def test_phone_script_scrubs_fragment_first_no_storage_no_plaintext_fallback(self):
        js = read(XPHONE_JS)
        code = no_comments(js)
        self.assertLess(code.index("replaceState"), code.index("fetch("))     # fragment effacé avant tout réseau
        for bad in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "console.", "innerHTML",
                    "eval(", "sendBeacon", "XMLHttpRequest", "crypto.subtle"):
            self.assertFalse(bad in code, bad)
        posts = re.findall(r'post\("([^"]+)", \{([^}]*)\}', code)
        self.assertEqual(sorted(set(p for p, _ in posts)), ["/r/x/cancel", "/r/x/claim", "/r/x/submit"])
        self.assertEqual([b for p, b in posts if p == "/r/x/submit"], [" s: S, c: C, ct: ct "])   # jamais « t: »
        self.assertIsNone(re.search(r"=>|\blet\b|\bconst\b|`", code))               # ES5 (téléphones anciens)
        self.assertIn('"X-OpenTV-Remote": "x"', code)
        self.assertIn('inp.value = "";', code)

    @unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_ACORN"), "acorn absent (OPENTV_TEST_ACORN)")
    def test_scripts_parse(self):
        for f, ver in ((XENTRY_JS, 2020), (XPHONE_JS, 5), (os.path.join(EXT, "background.js"), 2020)):
            js = ("const a=require(process.argv[1]);a.parse(require('fs').readFileSync(process.argv[2],'utf8'),"
                  "{ecmaVersion:%d,sourceType:'script'});" % ver)
            p = subprocess.run([NODE, "-e", js, os.environ["OPENTV_TEST_ACORN"], f], capture_output=True, timeout=30)
            self.assertEqual(p.returncode, 0, f + p.stderr.decode())

    def test_docs_flag_and_honest_limits(self):
        readme = read(os.path.join(PI4, "README.md"))
        agents_md = read(os.path.join(os.path.dirname(os.path.dirname(PI4)), "AGENTS.md"))
        for doc in (readme, agents_md):
            for need in ("OPENTV_X_PHONE_ENTRY", "Saisir avec le téléphone", "attaquant ACTIF",
                         "Identifiant seulement — jamais mot de passe ou code"):
                self.assertIn(need, doc)
        self.assertEqual(VERSION, "0.7.10")
        self.assertEqual(json.loads(read(os.path.join(EXT, "manifest.json")))["version"], VERSION)

    def test_line_caps(self):
        caps = {"opentv_pi/xentry.py": 400, "x-tv-extension/xentry.js": 420, "ui/xphone.js": 200,
                "opentv_pi/web.py": 1538, "opentv_pi/app.py": 1400, "opentv_pi/xprobe.py": 280,
                "x-tv-extension/nav.js": 560, "x-tv-extension/background.js": 320}
        over = []
        for f, cap in caps.items():
            n = len(read(os.path.join(PI4, f)).splitlines())
            if n > cap:
                over.append("%s : %d > %d" % (f, n, cap))
        self.assertEqual(over, [])


@unittest.skipUnless(NODE, "node absent")
class TestHarnessAndMutations(unittest.TestCase):
    def run_harness(self, xphone=XPHONE_JS, xentry_js=XENTRY_JS):
        p = subprocess.run([NODE, HARNESS, PI4, xphone, xentry_js], capture_output=True, text=True, timeout=120)
        try:
            return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return p.returncode, {"stderr": p.stderr[-2000:]}

    def test_harness_all_expectations(self):
        code, r = self.run_harness()
        self.assertEqual(code, 0, r)
        self.assertTrue(r["ok"], r)
        self.assertGreaterEqual(r["counts"]["phone"], 30)
        self.assertGreaterEqual(r["counts"]["gate"], 120)
        self.assertGreaterEqual(r["counts"]["ident"], 15)

    MUTANTS = (
        ("xphone", "return b64uEnc(seal.sha256(nb)) === fp;", "return true;"),                      # empreinte
        ("xphone", 'if (e !== "AQAB" || ', "if ("),                                                 # exposant
        ("xphone", "if (nb.length !== 256 || nb[0] < 0x80 || b64uEnc(nb) !== n) { return false; }", ""),  # 2048 bits
        ("xphone", 'if (CODE_RE.test(v)) { return "code"; }', ""),                                   # code refusé
        ("xphone", 'if (points(v) > ID_MAX) { return "long"; }', ""),                                 # borne
        ("xentry", "toks.length !== 1 || !AC_OK.has(toks[0])", "false"),                              # autocomplete
        ("xentry", "!f.texts.some((t) => DENY_RE.test(String(t)))", "true"),                         # libellés
        ("xentry", "if (f.maxlength > 0 && f.maxlength <= 8) { return false; }", ""),                # code court
        ("xentry", "if (f.pattern || f.danger) { return false; }", ""),                               # mot de passe voisin
        ("xentry", "|| !loginUrl(f.url) ", ""),                                                       # URL de connexion
        ("xentry", 'if (u.searchParams.has("start")) { return false; }', ""),                        # inscription
        ("xentry", "|| !TYPE_OK.has(f.type)", ""),                                                    # type
        ("xentry", "if (/^(numeric|decimal)$/i.test(String(f.inputmode).trim())) { return false; }", ""),
        ("xentry", "|| !sameText(doc.f, secret)", ""),                                                # secret du QR
        ("xentry", "|| doc.s !== sid ", ""),                                                          # séance
        ("xentry", 'Object.keys(doc).sort().join(",") !== "f,s,t,v" || ', ""),                       # champs fermés
        ("xentry", "return t && Array.from(t).length <= ID_MAX ? t : null;", "return t || null;"),   # borne
        ("xentry", "const t = doc.t.replace(STRIP_RE, \"\").trim();", "const t = doc.t.trim();"),     # contrôles
        ("xentry", "f.top !== true || f.focused !== true || ", ""),                                  # cadre + focus
    )

    def test_every_safety_mutant_is_detected(self):
        d = tempfile.mkdtemp(prefix="otvxmut", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        survived = []
        for i, (which, old, new) in enumerate(self.MUTANTS):
            src = read(XPHONE_JS if which == "xphone" else XENTRY_JS)
            self.assertEqual(src.count(old), 1, (which, old))
            path = os.path.join(d, "%s-%d.js" % (which, i))
            with open(path, "w", encoding="utf-8") as f:
                f.write(src.replace(old, new))
            code, r = self.run_harness(**({"xphone": path} if which == "xphone" else {"xentry_js": path}))
            if code == 0:
                survived.append((which, old))
        self.assertEqual(survived, [])


class TestInstall(unittest.TestCase):
    def test_install_manifests_files_and_uninstall_removes(self):
        r = tempfile.mkdtemp(prefix="otvxent", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        for dst, src in ((EXT_DST + "/xentry.js", XENTRY_JS), (EXT_DST + "/qrcode.js", os.path.join(EXT, "qrcode.js")),
                         ("/opt/opentv/share/ui/xphone.js", XPHONE_JS),
                         ("/opt/opentv/share/ui/muse.js", os.path.join(PI4, "ui", "muse.js"))):
            self.assertIn(dst, man)
            full = os.path.join(r, dst.lstrip("/"))
            self.assertEqual(os.stat(full).st_mode & 0o777, 0o644)
            with open(full, "rb") as a, open(src, "rb") as b:
                self.assertEqual(a.read(), b.read())
        env = read(os.path.join(r, "etc/opentv/opentv.env"))
        self.assertFalse(config.Config(dict(re.findall(r"(?m)^(OPENTV_[A-Z0-9_]+)=(.*)$", env))).x_phone_entry)
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        for dst in (EXT_DST + "/xentry.js", "/opt/opentv/share/ui/xphone.js"):
            self.assertFalse(os.path.exists(os.path.join(r, dst.lstrip("/"))), dst)

    def test_dry_run_lists_new_files(self):
        p = sh([INSTALL, "--dry-run"])
        out = p.stdout.decode() + p.stderr.decode()
        self.assertEqual(p.returncode, 0, out[-2000:])
        for need in ("x-tv-extension/xentry.js", "x-tv-extension/qrcode.js", "ui/xphone.js"):
            self.assertIn(need, out)


# --------------------------------------------------------------------------------------------- bout en bout
class _Control(http.server.BaseHTTPRequestHandler):
    """Canal de test seulement : témoin « X ouvert », état de la séance, décodage du QR (capture), drapeau, appairage."""

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        a = self.server.app
        if self.path == "/state":
            s = a.x_entry._s
            return self._send({"active": a.x_entry.active(), "sid": s["sid"] if s else None,
                               "n": s["n"] if s else None})
        if self.path == "/pair":
            return self._send({"url": a.tv_state()["remote"]["url"]})
        self._send({})

    def do_POST(self):
        a = self.server.app
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        if self.path.startswith("/presence"):
            xescape.set_marker(a.cfg.run_dir, self.path.endswith("on=1"))
        elif self.path == "/flag?on=0":
            a.cfg.x_phone_entry = False
        elif self.path == "/qr":
            img = cv2.imdecode(numpy.frombuffer(data, numpy.uint8), cv2.IMREAD_GRAYSCALE)
            text = ""
            # deux détecteurs d'OpenCV (le standard rate parfois un QR net ; Aruco le relit) : décodage seulement
            for det in [cv2.QRCodeDetector()] + ([cv2.QRCodeDetectorAruco()] if hasattr(cv2, "QRCodeDetectorAruco") else []):
                text = det.detectAndDecode(img)[0]
                if text:
                    break
            self.server.qrs.append(text)
            if not text and os.environ.get("OPENTV_TEST_SHOTS"):   # diagnostic : capture non décodée gardée
                with open(os.path.join(os.environ["OPENTV_TEST_SHOTS"], "qr-miss-%d.png" % len(self.server.qrs)), "wb") as f:
                    f.write(data)
            return self._send({"text": text})
        self._send({})

    def log_message(self, *a):
        pass


@unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT") and full_chrome() and cv2 is not None,
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT + OPENTV_TEST_CHROME_FULL + node + cv2 (QR)")
class TestXEntryEndToEnd(unittest.TestCase):
    def test_real_chromium_extension_phone_service(self):
        root = tempfile.mkdtemp(prefix="otvxee", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        p = sh([INSTALL, "--root", root, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        a = remote_app(self, OPENTV_X_PHONE_ENTRY="1")
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = remote_servers(self, a)
        a._mdns_check()
        ctl = http.server.HTTPServer(("127.0.0.1", 0), _Control)
        ctl.app, ctl.qrs = a, []
        threading.Thread(target=ctl.serve_forever, daemon=True).start()
        self.addCleanup(ctl.server_close)
        self.addCleanup(ctl.shutdown)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvxeshots", dir="/tmp")
        os.makedirs(shots, exist_ok=True)
        rport = s.web.remote_port()
        cfg = {"chrome": full_chrome(), "ext": os.path.join(root, EXT_DST.lstrip("/")), "ext_id": xcontext.EXTENSION_ID,
               "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "ctl": "http://127.0.0.1:%d" % ctl.server_port,
               "map": "MAP %s 127.0.0.1" % MDNS, "shots": shots}
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_x_entry.js"), os.environ["OPENTV_TEST_PLAYWRIGHT"],
                            json.dumps(cfg)], capture_output=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-3000:] + p.stderr.decode()[-3000:])
        st = json.loads(p.stdout.decode().strip().splitlines()[-1])["steps"]
        self.assertIs(st["flag_on"], True)
        # champ éligible au focus : pastille ; rien d'armé par la page (focus programmatique, touches/clic factices)
        self.assertEqual((st["initial"]["active"], st["initial"]["pill"], st["initial"]["qr"]), ("user", True, False))
        self.assertFalse(st["fake"]["tv"]["qr"])
        self.assertFalse(st["fake"]["service"]["active"])
        self.assertEqual(st["synthetic_arrow"]["active"], "user")              # flèche synthétique : rien ne bouge
        # QR : décodé sur la capture ; adresse de la télécommande + fragment (secret, empreinte de la clé publique)
        url_re = r"^http://%s:%d/r/x\?s=([A-Za-z0-9_-]{22})#([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})$" % (
            re.escape(MDNS), rport)
        for k in ("s1", "s4"):
            ss = st[k]
            self.assertEqual(ss["on_pill"], "pill")                               # ↓ réel : champ -> pastille
            self.assertTrue(ss["started"], ss)
            m = re.match(url_re, ss["url"])
            self.assertIsNotNone(m, ss["url"])
            self.assertEqual(m.group(1), ss["svc"]["sid"])
            import base64
            import hashlib
            n = base64.urlsafe_b64decode(ss["svc"]["n"] + "==")
            self.assertEqual(len(n), 256)
            self.assertEqual(m.group(3), base64.urlsafe_b64encode(hashlib.sha256(n).digest()).rstrip(b"=").decode())
        self.assertEqual(len(set(q.split("#")[1] for q in ctl.qrs if "#" in q)), len(ctl.qrs))   # secret neuf
        # empreinte altérée : clé refusée, séance annulée, rien saisi
        wf = st["wrong_fp"]
        self.assertIn("Clé inattendue", wf["phone"]["err"])
        self.assertFalse(wf["service"]["active"])
        self.assertEqual((wf["tv"]["qr"], wf["tv"]["value"]), (False, ""))
        # secret altéré / chiffré altéré : refusés par l'extension, champ intact, aucun événement
        for k in ("wrong_secret", "tampered_ct"):
            self.assertIn("rien saisi", st[k]["phone"]["status"], k)
            self.assertEqual((st[k]["tv"]["value"], st[k]["tv"]["ev"]["input"], st[k]["tv"]["qr"]), ("", 0, False), k)
            self.assertFalse(st[k]["service"]["active"], k)
        # nominal : fragment effacé, champ natif sans nom ni aide à la saisie, second téléphone refusé, UN remplissage
        self.assertEqual((st["scrubbed"]["hash"], "#" in st["scrubbed"]["href"]), ("", False))
        self.assertEqual(st["phone_input"], {"type": "text", "autocomplete": "off", "autocapitalize": "none",
                                             "autocorrect": "off", "spellcheck": "false", "name": None,
                                             "inputmode": "email"})
        self.assertIn("Un autre téléphone", st["second_phone"]["status"])
        f = st["filled"]
        self.assertEqual(f["tv"]["value"], "alex@example.com")                  # nettoyé (espaces, largeur nulle)
        self.assertEqual(f["tv"]["ev"], {"input": 1, "change": 1, "click": 0, "submit": 0})
        self.assertEqual((f["tv"]["qr"], f["tv"]["active"]), (False, "user"))
        self.assertTrue(f["tv"]["href"].startswith("https://x.com/i/flow/login"))
        self.assertIn("C'est fait", f["phone"]["status"])
        self.assertFalse(f["service"]["active"])
        reqs = st["phone_reqs"]
        frag = st["s4"]["url"].split("#")[1]
        for r in reqs:
            self.assertNotIn("#", r["url"])
            for secret in (frag, frag.split(".")[0], frag.split(".")[1], "alex@example.com", "alex%40example.com"):
                self.assertNotIn(secret, r["url"] + r["body"])
        sub = [r for r in reqs if r["path"] == "/r/x/submit"]
        self.assertEqual(len(sub), 1)
        body = json.loads(sub[0]["body"])
        self.assertEqual(sorted(body), ["c", "ct", "s"])
        self.assertEqual(len(body["ct"]), 342)                                  # taille FIXE
        self.assertEqual([r["path"] for r in reqs if r["method"] == "POST"], ["/r/x/claim", "/r/x/submit"])
        # annulations : touche réelle, focus posé par la page, navigation ; Retour (/tv) efface le service
        for k in ("cancel_key", "cancel_focus", "cancel_nav"):
            self.assertFalse(st[k]["service"]["active"], k)
            self.assertFalse(st[k]["tv"]["qr"], k)
        self.assertTrue(st["return_before"])
        self.assertFalse(st["return_after"]["active"])
        self.assertIn("annulée", st["return_phone"]["status"])
        # jamais éligibles : mot de passe, OTP, code de vérification, NIP, mot de passe voisin, passkey, secours,
        # carte ; URL hors connexion (accueil, inscription, accueil déconnecté)
        for k, v in st["ineligible"].items():
            if k == "url_onboarding_login":
                self.assertTrue(v["pill"], k)
                continue
            self.assertFalse(v["pill"], k)
            if "after" in v:
                self.assertNotEqual(v["after"], "pill", k)
                self.assertFalse(v["service"], k)
        # drapeau coupé : aucune UI
        self.assertIs(st["flag_off"], False)
        self.assertFalse(st["off"]["pill"])
        # aucune trace côté service : journaux, disque, vues TV / opérateur / agent
        blob = json.dumps(a.test_logs, ensure_ascii=False)
        for d in (a.cfg.state_dir, a.cfg.run_dir):
            for rt, _, files in os.walk(d):
                for fn in files:
                    try:
                        with open(os.path.join(rt, fn), "rb") as fh:
                            blob += fh.read().decode("utf-8", "replace")
                    except OSError:
                        pass
        blob += json.dumps(a.tv_state(), ensure_ascii=False) + json.dumps(xprobe.status_view(a))
        blob += json.dumps(a.agent_x_context())
        for secret in ["alex@example.com", "mallory@example.com", body["ct"], st["s4"]["svc"]["n"]] + \
                [q.split("#")[1] for q in ctl.qrs if "#" in q]:
            self.assertNotIn(secret, blob)
        self.assertTrue(all(set(x) <= {"event", "state", "method", "path", "status", "count", "port", "phones",
                                       "purpose", "reason", "error", "state_"} for x in a.test_logs if
                            x.get("event") in ("x_entry", "http")))
        for name in ("tv-x-entry-qr.png", "phone-x-entry.png"):
            self.assertGreater(os.path.getsize(os.path.join(shots, name)), 5000, name)


if __name__ == "__main__":
    unittest.main()
