"""X TV — sonde canari 0.7.8 (opentv_pi.xprobe + extension) : preuve terrain de l'injection de l'extension et de son
service sur /tv et x.com, et des touches physiques qui atteignent (ou non) la page, sans jamais une action.

Contrat vérifié ici :
- route POST /api/tv/x/probe : portes TV complètes (loopback, Host, UID kiosque, clé TV éphémère), Origin EXACTEMENT
  celui de l'extension (obligatoire), Referer étranger refusé, JSON ≤ 1 Kio, champs FERMÉS (aucune valeur/code de
  touche, texte, URL ni valeur de champ ne peut passer), débit borné par page (429), TTL court, mémoire seulement
  (rien sur disque, rien journalisé), 404 si X TV désactivé ;
- lecture opérateur GET /admin/x-probe : root seulement (UID du pair TCP), contrôle indisponible = refus, jamais le
  kiosque ; CLI `opentv-pi x-probe` ;
- extension : nav.js n'envoie que des instantanés bornés/limités sur touche RÉELLE, chargement, changement de mode et
  battement ; background.js déduit la page de l'expéditeur, reconstruit la liste fermée, coalesce et borne ; aucune
  voie pour qu'une page X forge un message ; anneau/consigne dans nav.css (hors CSP), plus aucun <style> injecté ;
  fenêtre modale visible prioritaire sur le fil (nav.js/x.js).
"""
import io
import json
import os
import re
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests._common import PI4
from tests.test_service import make_app, Servers
from opentv_pi import VERSION, cli, xcontext, xprobe

EXT = os.path.join(PI4, "x-tv-extension")
ORIGIN = xcontext.EXTENSION_ORIGIN


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def code_only(text):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*"):
            continue
        out.append(line)
    return "\n".join(out)


def body(**kw):
    b = {"v": 1, "page": "x", "mode": "feed", "event": "key", "seq": 3, "move": 2, "ok": 1, "other": 0,
         "synthetic": 0, "last": {"cls": "move", "trusted": True, "editable": False, "doc_focus": True,
                                  "handled": True},
         "ext": VERSION, "sw_s": 12}
    b.update(kw)
    return b


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --------------------------------------------------------------------------------------------- module
class TestProbeStore(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = xprobe.XProbe(mono=self.clock)

    def test_valid_snapshot_view_closed(self):
        self.assertEqual(self.p.record(body()), (200, {"ok": True}))
        v = self.p.view()
        self.assertEqual(set(v), {"ttl_s", "pages", "rejected"})
        x = v["pages"]["x"]
        self.assertEqual(set(x), {"present", "mode", "event", "seq", "keys", "synthetic", "last", "ext", "sw_s",
                                  "age_s", "accepted", "rate_limited"})
        self.assertEqual((x["present"], x["mode"], x["keys"], x["ext"]), (True, "feed", {"move": 2, "ok": 1,
                                                                                         "other": 0}, VERSION))
        self.assertEqual(x["last"], {"cls": "move", "trusted": True, "editable": False, "doc_focus": True,
                                     "handled": True})
        self.assertEqual(v["pages"]["tv"], {"present": False, "accepted": 0, "rate_limited": 0})
        self.assertLessEqual(xprobe.TTL_S, 300)                   # TTL court

    def test_tv_heartbeat(self):
        self.assertEqual(self.p.record(body(page="tv", mode="tv", event="load", move=0, ok=0, last=None))[0], 200)
        self.assertTrue(self.p.view()["pages"]["tv"]["present"])

    def test_closed_fields_types_enums_ranges(self):
        bad = [
            dict(body(), key="ArrowUp"), dict(body(), code=38), dict(body(), url="https://x.com/home"),
            dict(body(), text="secret"), {k: v for k, v in body().items() if k != "seq"},
            body(v=2), body(v=True), body(page="evil"), body(page="tv"), body(page="x", mode="tv"),
            body(mode="ArrowUp"), body(event="click"), body(seq=-1), body(move=xprobe.COUNT_MAX + 1),
            body(ok=True), body(other=1.5), body(synthetic="3"), body(ext="evil"), body(ext="0.7.8-extra"),
            body(sw_s=None), body(last=[1]), body(last={"cls": "ArrowUp", "trusted": True, "editable": False,
                                                       "doc_focus": True, "handled": False}),
            body(last={"cls": "move", "trusted": "yes", "editable": False, "doc_focus": True, "handled": False}),
            body(last={"cls": "move", "trusted": True, "editable": False, "doc_focus": True, "handled": False,
                       "key": "a"}),
            body(last={"cls": "move", "trusted": True, "editable": False, "doc_focus": True}),
            [body()], "x", None,
        ]
        for b in bad:
            with self.subTest(b=b):
                self.assertEqual(self.p.record(b), (400, {"ok": False, "error": "bad_fields"}))
        v = self.p.view()
        self.assertEqual(v["rejected"], len(bad))
        self.assertFalse(v["pages"]["x"]["present"])              # refus : rien enregistré

    def test_rate_limit_per_page_window(self):
        for i in range(xprobe.RATE_MAX):
            self.assertEqual(self.p.record(body(seq=i))[0], 200)
        self.assertEqual(self.p.record(body(seq=999))[0], 429)
        self.assertEqual(self.p.view()["pages"]["x"]["seq"], xprobe.RATE_MAX - 1)   # refusée : jamais retenue
        self.assertEqual(self.p.record(body(page="tv", mode="tv", last=None))[0], 200)   # autre page : à part
        self.clock.t += xprobe.RATE_WINDOW_S
        self.assertEqual(self.p.record(body(seq=1000))[0], 200)
        x = self.p.view()["pages"]["x"]
        self.assertEqual((x["accepted"], x["rate_limited"], x["seq"]), (xprobe.RATE_MAX + 1, 1, 1000))

    def test_ttl_short_then_absent(self):
        self.p.record(body())
        self.clock.t += xprobe.TTL_S - 1
        self.assertTrue(self.p.view()["pages"]["x"]["present"])
        self.clock.t += 1
        self.assertFalse(self.p.view()["pages"]["x"]["present"])
        self.clock.t -= 10_000                                     # horloge incohérente : jamais un faux « vivant »
        self.assertFalse(self.p.view()["pages"]["x"]["present"])


# --------------------------------------------------------------------------------------------- route
class TestProbeRoute(unittest.TestCase):
    def setUp(self):
        self.a = make_app(self)
        self.s = Servers(self, self.a)

    def post(self, b, key=True, ctype="application/json", origin=ORIGIN, extra=None, raw=None):
        h = {"Content-Type": ctype}
        if key:
            h["X-OpenTV-TV"] = self.a.tv_key if key is True else key
        if origin:
            h["Origin"] = origin
        h.update(extra or {})
        return self.s.req("POST", xprobe.PROBE_PATH, body=raw if raw is not None else json.dumps(b), headers=h)

    def present(self):
        return self.a.x_probe.view()["pages"]["x"]["present"]

    def test_ok_from_extension(self):
        st, _, b = self.post(body())
        self.assertEqual((st, json.loads(b)), (200, {"ok": True}))
        self.assertTrue(self.present())

    def test_key_required_exact_and_rotated(self):
        for key in (False, "mauvaise", self.a.tv_key[:-1]):
            st, _, b = self.post(body(), key=key)
            self.assertEqual((st, json.loads(b)["error"]), (403, "tv_key"), key)
        self.assertFalse(self.present())
        old = self.a.tv_key
        b2 = make_app(self)                                       # redémarrage : clé tournée, sonde perdue
        s2 = Servers(self, b2)
        st, _, _ = s2.req("POST", xprobe.PROBE_PATH, body=json.dumps(body()),
                          headers={"X-OpenTV-TV": old, "Content-Type": "application/json", "Origin": ORIGIN})
        self.assertEqual(st, 403)
        self.assertFalse(b2.x_probe.view()["pages"]["x"]["present"])

    def test_peer_uid_must_be_kiosk(self):
        self.a.tv_client_uids = frozenset({4242})
        self.a.peer_uid_fn = lambda ip, port, sport: 0            # même root : la sonde est réservée au kiosque
        self.assertEqual(self.post(body())[0], 403)
        self.a.peer_uid_fn = lambda ip, port, sport: None
        self.assertEqual(self.post(body())[0], 403)
        self.assertFalse(self.present())
        self.a.peer_uid_fn = lambda ip, port, sport: 4242
        self.assertEqual(self.post(body())[0], 200)

    def test_origin_mandatory_and_exact(self):
        for o in (None, "https://x.com", "https://twitter.com", "http://127.0.0.1:%d" % self.s.web.tv_port(), "null",
                  "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", ORIGIN + "/", ORIGIN.upper()):
            st, _, b = self.post(body(), origin=o)
            self.assertEqual((st, json.loads(b)["error"]), (403, "origin"), o)
        st, _, _ = self.post(body(), extra={"Referer": "https://x.com/home"})
        self.assertEqual(st, 403)
        self.assertFalse(self.present())
        self.assertEqual(self.post(body(), extra={"Referer": ORIGIN + "/background.js"})[0], 200)

    def test_host_content_type_size_json(self):
        self.assertEqual(self.post(body(), extra={"Host": "evil.example:%d" % self.s.web.tv_port()})[0], 421)
        self.assertEqual(self.post(body(), ctype="text/plain")[0], 415)
        self.assertEqual(self.post(None, raw=json.dumps(dict(body(), pad="x" * xprobe.BODY_MAX)))[0], 413)
        self.assertEqual(self.post(None, raw="[" * 3000 + "]" * 3000)[0], 413)     # > 1 Kio : jamais lu
        self.assertEqual(self.post(None, raw="[" * 400 + "]" * 400)[0], 400)
        self.assertEqual(self.post(None, raw="{")[0], 400)
        self.assertEqual(self.post(dict(body(), key="ArrowDown"))[0], 400)
        self.assertFalse(self.present())
        for m in ("GET", "PUT", "DELETE"):                       # jamais une lecture par la route d'écriture
            st, _, b = self.s.req(m, xprobe.PROBE_PATH, headers={"X-OpenTV-TV": self.a.tv_key, "Origin": ORIGIN})
            self.assertNotEqual(st, 200, m)

    def test_rate_limited_over_http(self):
        for i in range(xprobe.RATE_MAX):
            self.assertEqual(self.post(body(seq=i))[0], 200)
        st, _, b = self.post(body(seq=10_000))
        self.assertEqual((st, json.loads(b)["error"]), (429, "rate_limited"))

    def test_memory_only_no_log_no_disk(self):
        def files():
            out = set()
            for d in (self.a.cfg.state_dir, self.a.cfg.run_dir):
                for root, _, names in os.walk(d):
                    out.update(os.path.join(root, n) for n in names)
            return out
        before = files()
        self.post(body(ext="0.7.8"))
        self.post(dict(body(), key="SECRETKEY"))
        self.post(body(), key="faux")
        self.assertEqual(files(), before)
        dump = json.dumps(self.a.test_logs)
        self.assertNotIn("SECRETKEY", dump)
        self.assertNotIn(VERSION, dump)
        # journal HTTP habituel (méthode, chemin, statut) seulement ; aucun événement propre à la sonde
        other = [e for e in self.a.test_logs if e.get("event") != "http"]
        self.assertNotIn("probe", json.dumps(other))
        for e in self.a.test_logs:
            if e.get("event") == "http":
                self.assertEqual(set(e), {"event", "method", "path", "status"})

    def test_opt_out_404(self):
        b = make_app(self, OPENTV_X_TV="0")
        s2 = Servers(self, b)
        st, _, _ = s2.req("POST", xprobe.PROBE_PATH, body=json.dumps(body()),
                          headers={"X-OpenTV-TV": b.tv_key, "Content-Type": "application/json", "Origin": ORIGIN})
        self.assertEqual(st, 404)


# --------------------------------------------------------------------------------------------- lecture opérateur
class TestOperatorStatus(unittest.TestCase):
    def setUp(self):
        self.a = make_app(self)
        self.s = Servers(self, self.a)

    def get(self, extra=None):
        st, _, b = self.s.req("GET", xprobe.STATUS_PATH, headers=extra)
        return st, b

    def as_linux(self, peer):
        self.a.tv_client_uids = frozenset({4242})
        self.a.operator_uids = xprobe.OPERATOR_UIDS
        self.a.peer_uid_fn = lambda ip, port, sport: peer

    def test_unavailable_peer_check_refused(self):
        self.assertIsNone(self.a.operator_uids)                  # hors Linux : pas de /proc/net/tcp
        st, b = self.get()
        self.assertEqual((st, json.loads(b)["error"]), (403, "operator_only"))

    def test_root_only(self):
        self.a.x_probe.record(body())
        for peer in (4242, 1000, None):                           # kiosque, autre compte, inconnu
            self.as_linux(peer)
            st, b = self.get({"X-OpenTV-TV": self.a.tv_key})
            self.assertEqual(st, 403, peer)
            self.assertNotIn("feed", b)
        self.as_linux(0)
        st, b = self.get()
        self.assertEqual(st, 200)
        v = json.loads(b)
        self.assertEqual((v["ok"], v["enabled"], v["version"]), (True, True, VERSION))
        self.assertEqual(v["pages"]["x"]["keys"], {"move": 2, "ok": 1, "other": 0})
        self.assertNotIn(self.a.tv_key, b)                        # jamais la clé TV
        self.assertEqual(self.get({"Host": "evil.example:%d" % self.s.web.tv_port()})[0], 421)

    def test_cli_reads_status(self):
        self.as_linux(0)
        self.a.x_probe.record(body(page="tv", mode="tv", event="load", last=None))
        port = self.s.web.tv_port()
        out = io.StringIO()
        with mock.patch.object(cli.config.Config, "load", lambda env: types.SimpleNamespace(tv_port=port)), \
                redirect_stdout(out):
            rc = cli.main(["x-probe"])
        self.assertEqual(rc, 0)
        v = json.loads(out.getvalue())
        self.assertTrue(v["pages"]["tv"]["present"])
        self.as_linux(4242)                                       # non root : refus honnête, code 1
        out = io.StringIO()
        with mock.patch.object(cli.config.Config, "load", lambda env: types.SimpleNamespace(tv_port=port)), \
                redirect_stdout(out):
            rc = cli.main(["x-probe"])
        self.assertEqual((rc, json.loads(out.getvalue())["error"]), (1, "operator_only"))


# --------------------------------------------------------------------------------------------- rollback
class TestXTvRollback(unittest.TestCase):
    """OPENTV_X_TV=0 (rollback sans désinstaller) : sonde refusée (404) sans rien retenir, lecture opérateur « désactivé »
    sans aucune page, témoin « X ouvert » refusé (aucun armement evdev), tuile et contexte absents ; le kiosque ne
    charge alors ni l'extension ni nav.css (test_x_tv.TestKioskAndPolicy)."""

    def test_disabled_everything_inert(self):
        b = make_app(self, OPENTV_X_TV="0")
        s = Servers(self, b)
        st, _, _ = s.req("POST", xprobe.PROBE_PATH, body=json.dumps(body()),
                         headers={"X-OpenTV-TV": b.tv_key, "Content-Type": "application/json", "Origin": ORIGIN})
        self.assertEqual(st, 404)
        self.assertFalse(b.x_probe.view()["pages"]["x"]["present"])
        b.tv_client_uids, b.operator_uids = frozenset({4242}), xprobe.OPERATOR_UIDS
        b.peer_uid_fn = lambda ip, port, sport: 0
        st, _, raw = s.req("GET", xprobe.STATUS_PATH)
        self.assertEqual(st, 200)
        v = json.loads(raw)
        self.assertEqual((v["enabled"], v["pages"]), (False, {}))
        b.peer_uid_fn = lambda ip, port, sport: 4242
        st, _, raw = s.req("POST", "/api/tv/x/presence", body=json.dumps({"active": True}),
                           headers={"X-OpenTV-TV": b.tv_key, "Content-Type": "application/json"})
        self.assertEqual((st, json.loads(raw)["error"]), (409, "x_tv_disabled"))
        self.assertEqual(b.tv_state()["apps"], [])
        self.assertEqual(b.agent_x_context(), {"available": False})

    def test_kiosk_rollback_loads_no_extension(self):
        from tests.test_x_tv import TestKioskAndPolicy
        self.assertEqual(TestKioskAndPolicy.kiosk_x_args(self, {"OPENTV_X_TV": "0"}), [])


# --------------------------------------------------------------------------------------------- extension (source)
class TestExtensionProbeSource(unittest.TestCase):
    def setUp(self):
        self.nav = code_only(read(os.path.join(EXT, "nav.js")))
        self.x = code_only(read(os.path.join(EXT, "x.js")))
        self.bg = code_only(read(os.path.join(EXT, "background.js")))

    def test_nav_probe_message_closed_and_bounded(self):
        m = re.search(r"const msg = \{([^}]*)\};", self.nav)
        self.assertTrue(m)
        keys = sorted(k.strip().split(":")[0] for k in m.group(1).replace("\n", " ").split(",") if k.strip())
        self.assertEqual(keys, sorted(["type", "mode", "event", "seq", "move", "ok", "other", "synthetic", "last"]))
        last = re.search(r"probe\.last = \{([^}]*)\};", self.nav).group(1)
        self.assertEqual(sorted(k.strip().split(":")[0] for k in last.split(",")),
                         ["cls", "doc_focus", "editable", "handled", "trusted"])
        self.assertIn("trusted: true", last)
        self.assertGreaterEqual(int(re.search(r"PROBE_MIN_MS = (\d+);", self.nav).group(1)), 400)
        self.assertLessEqual(int(re.search(r"PROBE_MAX = (\d+);", self.nav).group(1)), 5000)
        pk = self.nav[self.nav.index("function probeKey(e)"):self.nav.index("function noteTyping(e)")]
        self.assertLess(pk.index('if (!e.isTrusted) { bump("synthetic"); return; }'), pk.index("probeSend("))
        for bad in ("e.key", "e.code", "keyCode", "location", "textContent", "innerText"):
            self.assertNotIn(bad, pk.replace("keyClass(e)", ""), bad)
        nt = self.nav[self.nav.index("function noteTyping(e)"):self.nav.index("function onKey(e)")]
        self.assertIn("typedFields.add(ae)", nt)                  # « tapé ici » seulement
        self.assertNotIn("sendMessage", nt)

    def test_background_probe_sender_and_closed_body(self):
        self.assertIn('if (msg.type === "probe" && fromX(sender)) {', self.bg)
        self.assertIn('queueProbe(probeBody("x", msg));', self.bg)  # page déduite de l'expéditeur
        pb = self.bg[self.bg.index("function probeBody(page, msg)"):self.bg.index("const probeQ")]
        ret = re.search(r"return \{ (v: 1.*?) \};", pb, re.S).group(1)
        keys = sorted(k.strip().split(":")[0] for k in ret.replace("\n", " ").split(",") if ":" in k)
        self.assertEqual(keys, sorted(xprobe.FIELDS))
        pump = self.bg[self.bg.index("async function pumpProbe()"):self.bg.index("async function goTv(")]
        self.assertNotIn("storage.session.remove", pump)          # jamais la clé effacée par la sonde
        self.assertIn('credentials: "omit"', pump)
        self.assertIn("probeQ.sent.length >= PROBE_MAX", pump)
        self.assertLessEqual(int(re.search(r"const PROBE_MAX = (\d+);", self.bg).group(1)),
                             xprobe.RATE_MAX)                    # le service n'a jamais à refuser une rafale normale

    def test_page_cannot_forge_extension_messages(self):
        m = json.loads(read(os.path.join(EXT, "manifest.json")))
        self.assertNotIn("externally_connectable", m)
        self.assertNotIn("web_accessible_resources", m)
        for f in ("nav.js", "x.js", "background.js", "tvkey.js"):
            src = code_only(read(os.path.join(EXT, f)))
            for bad in ("onMessageExternal", "onConnectExternal", 'addEventListener("message"', "postMessage",
                        "window.wrappedJSObject", "CustomEvent"):
                self.assertNotIn(bad, src, (f, bad))
        self.assertIn("sender.id !== chrome.runtime.id", self.bg)
        self.assertIn("sender.frameId === 0", self.bg)

    def test_no_injected_style_ring_in_extension_css(self):
        self.assertNotIn('createElement("style")', self.nav)
        self.assertNotIn("RING_CSS", self.nav)
        css = re.sub(r"/\*.*?\*/", "", read(os.path.join(EXT, "nav.css")), flags=re.S)
        self.assertNotIn("@import", css)
        self.assertNotIn("url(", css)
        for rule in re.findall(r"([^{}]+)\{", css):
            for sel in rule.split(","):
                self.assertTrue("[data-otvx-focus]" in sel or ".otvx-login-help" in sel, sel)
        self.assertIn("outline: 6px solid #ffd400 !important", css)
        self.assertIn("position: fixed !important", css)
        # repli CSSOM (jamais bloqué par la CSP) seulement si la feuille manque ; valeurs d'origine rétablies
        self.assertIn('if (cs.outlineStyle === "solid" && parseFloat(cs.outlineWidth) >= 4) { return; }', self.nav)
        self.assertIn("el.style.removeProperty(k)", self.nav)

    def test_dialog_wins_and_feed_script_yields(self):
        self.assertIn('if (modalDialog()) { return "dialog"; }', self.nav)
        pm = self.nav[self.nav.index("function pageMode()"):self.nav.index("function loginMode()")]
        self.assertLess(pm.index("modalDialog()"), pm.index("COLUMN_SEL"))   # avant la colonne principale
        ok = self.x[self.x.index("function onKey(e)"):self.x.index("function refresh()")]
        self.assertLess(ok.index("if (!feed || dialogOpen()) { return; }"), ok.index("move(-1)"))
        self.assertLess(ok.index("if (!e.isTrusted) { return; }"), ok.index("dialogOpen()"))
        self.assertIn("ensureHelp(on && !dialogOpen());", self.x)
        self.assertIn('Object.defineProperty(window, "__otvxNav"', self.nav)
        self.assertIn("writable: false, configurable: false", self.nav)

    def test_autofocused_field_rule(self):
        self.assertIn('function leaveField(el) { return lineInput(el) || (el.tagName !== "SELECT" && '
                      '!typedFields.has(el)); }', self.nav)
        lk = self.nav[self.nav.index("function loginKey(e)"):self.nav.index("function refresh()")]
        self.assertIn("if (editable(ae) && !(m.vertical && leaveField(ae))) { return false; }", lk)
        ok = self.x[self.x.index("function onKey(e)"):self.x.index("function refresh()")]
        self.assertIn('if (!((a === "up" || a === "down") && NAV && NAV.leaveField(ae))) { return; }', ok)


if __name__ == "__main__":
    unittest.main()
