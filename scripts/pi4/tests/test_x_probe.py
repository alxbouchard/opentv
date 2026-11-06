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
import pwd
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests._common import PI4
from tests.test_service import make_app, Servers
from opentv_pi import VERSION, cli, xcontext, xescape, xprobe, xstatus

EXT = os.path.join(PI4, "x-tv-extension")
ORIGIN = xcontext.EXTENSION_ORIGIN
NODE = shutil.which("node")
HARNESS = os.path.join(PI4, "tests", "x_probe_harness.js")


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
    b = {"v": 1, "page": "x", "mode": "feed", "event": "key", "seq": 3, "move": 2, "ok": 1, "other": 0, "repeat": 0,
         "synthetic": 0, "doc_focus": True, "visible": True,
         "last": {"cls": "move", "trusted": True, "editable": False, "doc_focus": True, "handled": True},
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
        self.assertEqual(set(v), {"ttl_s", "pages", "rejected", "http"})
        self.assertEqual(v["http"], {k: 0 for k in xprobe.HTTP_CLASSES})   # statuts agrégés seulement
        x = v["pages"]["x"]
        self.assertEqual(set(x), {"present", "mode", "event", "seq", "keys", "repeat", "synthetic", "doc_focus",
                                  "visible", "last", "ext", "sw_s", "age_s", "accepted", "rate_limited"})
        self.assertEqual((x["present"], x["mode"], x["keys"], x["ext"]), (True, "feed", {"move": 2, "ok": 1,
                                                                                         "other": 0}, VERSION))
        self.assertEqual(x["last"], {"cls": "move", "trusted": True, "editable": False, "doc_focus": True,
                                     "handled": True})
        self.assertEqual(v["pages"]["tv"], {"present": False, "accepted": 0, "rate_limited": 0})
        self.assertLessEqual(xprobe.TTL_S, 300)                   # TTL court

    def test_tv_heartbeat(self):
        self.assertEqual(self.p.record(body(page="tv", mode="tv", event="load", move=0, ok=0, last=None))[0], 200)
        self.assertTrue(self.p.view()["pages"]["tv"]["present"])
        # battement : focus du document et visibilité toujours rapportés (absence de touches explicable)
        self.assertEqual(self.p.record(body(page="tv", mode="tv", event="beat", move=0, ok=0, last=None,
                                            doc_focus=False, visible=False))[0], 200)
        tv = self.p.view()["pages"]["tv"]
        self.assertEqual((tv["event"], tv["doc_focus"], tv["visible"]), ("beat", False, False))

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
            # types malformés (liste/dict non hachables, flottant égal à 1) : refus 400, jamais une exception
            body(page=["x"]), body(page={"x": 1}), body(mode=["feed"]), body(mode={"feed": 1}), body(event=["key"]),
            body(event={"key": 1}), body(v=1.0), body(v="1"), body(ext=["0.7.8"]), body(repeat=-1), body(repeat=True),
            body(doc_focus="yes"), body(doc_focus=None), body(visible=1), body(last="move"),
            body(last={"cls": ["move"], "trusted": True, "editable": False, "doc_focus": True, "handled": True}),
            body(last={"cls": {"move": 1}, "trusted": True, "editable": False, "doc_focus": True, "handled": True}),
            {k: v for k, v in body().items() if k != "repeat"}, {k: v for k, v in body().items() if k != "visible"},
        ]
        for b in bad:
            with self.subTest(b=b):
                self.assertEqual(self.p.record(b), (400, {"ok": False, "error": "bad_fields"}))
        v = self.p.view()
        self.assertEqual(v["rejected"], len(bad))
        self.assertFalse(v["pages"]["x"]["present"])              # refus : rien enregistré
        self.assertEqual((v["pages"]["x"]["accepted"], v["pages"]["x"]["rate_limited"]), (0, 0))

    def test_rejected_counter_bounded(self):
        self.p._rejected = xprobe.COUNT_MAX - 1
        for _ in range(3):
            self.assertEqual(self.p.record(body(mode=["feed"]))[0], 400)
        self.assertEqual(self.p.view()["rejected"], xprobe.COUNT_MAX)
        self.assertEqual(self.p.record(body())[0], 200)          # refus antérieurs : aucune incidence

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

    def test_malformed_types_400_counted_no_traceback(self):
        """Liste/dict à la place d'un mode, d'un événement, d'une page ou de last.cls : 400 bad_fields, compté dans
        `rejected`, jamais un 500 ni une trace ; les refus AVANT lecture du corps (Origin, type, taille, JSON) ne
        comptent pas : `rejected` = instantanés refusés par la liste fermée seulement."""
        import contextlib
        err = io.StringIO()
        bad = [body(mode=["feed"]), body(event={"key": 1}), body(page=["x"]),
               body(last={"cls": ["ok"], "trusted": True, "editable": False, "doc_focus": True, "handled": True}),
               body(v=1.0), [1, 2], {"v": [1]}]
        with contextlib.redirect_stderr(err):
            for b in bad:
                st, _, raw = self.post(b)
                self.assertEqual((st, json.loads(raw)), (400, {"ok": False, "error": "bad_fields"}), b)
            self.assertEqual(self.post(body(), origin="https://x.com")[0], 403)
            self.assertEqual(self.post(body(), ctype="text/plain")[0], 415)
            self.assertEqual(self.post(None, raw="{")[0], 400)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(self.a.x_probe.view()["rejected"], len(bad))
        self.assertFalse(self.present())
        self.assertEqual(self.post(body())[0], 200)                # la route reste saine

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
        self.assertNotIn("probe", dump)                           # aucune ligne, ni événement propre à la sonde
        self.assertFalse([e for e in self.a.test_logs if e.get("event") in ("http", "http_ok_sample")])

    def test_never_logged_any_status_counted_in_memory(self):
        """M1 : AUCUNE ligne de journal HTTP pour la sonde, quel que soit le statut — 200, 400, 403 (clé périmée,
        Origin, pair non kiosque), 404, 413, 415, 429 : journald ne porte jamais le rythme des touches. Seuls des
        compteurs agrégés en mémoire (`http`) le disent à l'opérateur. Témoin : une autre route reste journalisée."""
        expect = {}
        n0 = len(self.a.test_logs)

        def hit(code, cls=None, **kw):
            self.assertEqual(self.post(kw.pop("b", body()), **kw)[0], code, kw)
            expect[cls or str(code)] = expect.get(cls or str(code), 0) + 1
        hit(200, "2xx")
        hit(400, b=body(mode=["feed"]))
        hit(403, key="cle-perimee-d-un-ancien-demarrage")       # service redémarré : clé périmée
        hit(403, origin="https://x.com")
        hit(413, raw=json.dumps(dict(body(), pad="x" * xprobe.BODY_MAX)))
        hit(415, ctype="text/plain")
        for i in range(xprobe.RATE_MAX - 1):
            hit(200, "2xx", b=body(seq=i))
        for _ in range(3):
            hit(429, b=body(seq=99))                              # rafale refusée : jamais une ligne par refus
        st, _, _ = self.s.req("GET", xprobe.PROBE_PATH, headers={"X-OpenTV-TV": self.a.tv_key})
        self.assertEqual(st, 404)
        expect["404"] = 1
        self.a.tv_client_uids = frozenset({4242})                 # pair non kiosque : 403 de la porte TV
        self.a.peer_uid_fn = lambda ip, port, sport: 1000
        hit(403)
        self.a.tv_client_uids = None
        self.assertEqual(self.a.test_logs[n0:], [])               # aucune ligne du tout
        self.assertEqual({k: v for k, v in self.a.x_probe.view()["http"].items() if v}, expect)
        self.assertEqual(set(self.a.x_probe.view()["http"]), set(xprobe.HTTP_CLASSES))
        st, _, _ = self.s.req("POST", "/api/tv/feedback", body="{}",
                              headers={"X-OpenTV-TV": "faux", "Content-Type": "application/json"})
        self.assertEqual(st, 403)
        self.assertEqual([(e["event"], e["path"], e["status"]) for e in self.a.test_logs[n0:]],
                         [("http", "/api/tv/feedback", 403)])    # témoin : journal HTTP ordinaire intact
        self.assertEqual(sum(self.a.x_probe.view()["http"].values()), sum(expect.values()))   # jamais compté ici

    def test_quiet_log_counts_only_on_tv_server(self):
        calls = []
        app = types.SimpleNamespace(x_probe=types.SimpleNamespace(note_http=calls.append))
        xprobe.quiet_log(types.SimpleNamespace(app=app), 403)              # autre serveur : rien, même pas compté
        xprobe.quiet_log(types.SimpleNamespace(app=app, probe_route=True), 429)
        self.assertEqual(calls, [429])
        self.assertEqual([xprobe.http_class(c) for c in (200, 204, 400, 403, 429, 500, 0, True)],
                         ["2xx", "2xx", "400", "403", "429", "other", "other", "other"])

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
        self.a.peer_uid_fn = lambda ip, port, sport: peer

    def test_unavailable_peer_check_refused(self):
        self.assertIsNone(self.a.tv_client_uids)                 # hors Linux : pas de /proc/net/tcp
        self.a.peer_uid_fn = lambda ip, port, sport: 0           # même un « root » annoncé : refus sans contrôle
        st, b = self.get()
        self.assertEqual((st, json.loads(b)["error"]), (403, "operator_only"))

    def test_guard_operator_loopback_and_host(self):
        """Porte opérateur (xprobe.guard_operator, hors web.py) : adresse non locale ou illisible, Host étranger,
        contrôle du pair indisponible ou pair non root : refus ; root local : autorisé."""
        self.as_linux(0)

        def h(addr, host="127.0.0.1:8080"):
            return types.SimpleNamespace(client_address=(addr, 5555), headers={"Host": host},
                                         server=types.SimpleNamespace(server_address=("127.0.0.1", 8080)))
        g = xprobe.guard_operator
        self.assertIsNone(g(h("127.0.0.1"), self.a))
        self.assertIsNone(g(h("::1", "localhost:8080"), self.a))
        self.assertEqual(g(h("192.168.1.20"), self.a), (403, {"ok": False, "error": "forbidden"}))
        self.assertEqual(g(h("pas-une-ip"), self.a), (403, {"ok": False, "error": "forbidden"}))
        self.assertEqual(g(h("127.0.0.1", "evil:8080"), self.a), (421, {"ok": False, "error": "host"}))
        self.assertEqual(g(h("127.0.0.1", None), self.a)[0], 421)
        self.as_linux(1000)
        self.assertEqual(g(h("127.0.0.1"), self.a), (403, {"ok": False, "error": "operator_only"}))
        self.a.tv_client_uids = None
        self.a.peer_uid_fn = lambda ip, port, sport: 0
        self.assertEqual(g(h("127.0.0.1"), self.a)[0], 403)

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

    def cli(self, nav_dir, port=None, listener=None):
        port, user = port or self.s.web.tv_port(), pwd.getpwuid(os.getuid()).pw_name
        out = io.StringIO()
        seen = []

        def lst(p, local):   # hors Linux (pas de /proc/net/tcp) : écoute « du compte du service » simulée
            seen.append((p, local))
            return os.getuid() if listener is None else listener(p, local)
        with mock.patch.object(cli.config.Config, "load",
                               lambda env: types.SimpleNamespace(tv_port=port, player_keys_user=user,
                                                                 service_user=user)), \
                mock.patch.object(xescape, "REQUEST_DIR", nav_dir), \
                mock.patch.object(xescape, "_peer_uid", lambda c: os.getuid()), \
                mock.patch.object(xstatus, "listener_uid", lst), redirect_stdout(out):
            rc = cli.main(["x-probe"])
        self.assertTrue(seen and all(p == port and isinstance(lo, int) and lo > 0 for p, lo in seen))
        return rc, json.loads(out.getvalue())

    def nav_dir(self):
        d = tempfile.mkdtemp(prefix="otvn", dir="/tmp")           # chemin court (sun_path)
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def fake_service(self, status, payload):
        """Écoute locale qui N'EST PAS le service : réponse fixée (forgée, malformée, texte injecté)."""
        import http.server
        import threading
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1], hits

    def test_cli_refuses_foreign_listener_before_any_request(self):
        """Écoute TCP d'un autre compte que le service (port pris pendant un redémarrage) ou contrôle indisponible :
        refus AVANT la requête — rien demandé, rien lu, rien recopié."""
        self.as_linux(0)
        d = self.nav_dir()
        for uid in (os.getuid() + 1, None):
            with self.subTest(uid=uid):
                rc, v = self.cli(d, listener=lambda p, lo, u=uid: u)
                self.assertEqual((rc, v["ok"], v["error"]), (1, False, "listener"))
                self.assertEqual(set(v), {"ok", "error", "evdev"})
        self.assertFalse([e for e in self.a.test_logs if e.get("path") == xprobe.STATUS_PATH])   # jamais atteint
        port, hits = self.fake_service(200, {"ok": True})
        rc, v = self.cli(d, port=port, listener=lambda p, lo: None)
        self.assertEqual((rc, v["error"], hits), (1, "listener", []))

    def test_cli_refuses_forged_or_malformed_evidence_without_leaking(self):
        self.as_linux(0)
        self.a.x_probe.record(body())
        good = xprobe.status_view(self.a)
        d = self.nav_dir()
        forged = [
            dict(good, injected="<script>EVIL</script>"),
            dict(good, pages=dict(good["pages"], x=dict(good["pages"]["x"], keys={"move": 1, "ok": 1, "other": 0,
                                                                                  "EVIL": 1}))),
            dict(good, pages=dict(good["pages"], x=dict(good["pages"]["x"], mode="EVIL"))),
            dict(good, version="EVIL"), dict(good, ok="EVIL"), [good], "EVIL",
        ]
        for f in forged:
            with self.subTest(f=str(f)[:60]):
                port, hits = self.fake_service(200, f)
                rc, v = self.cli(d, port=port)
                self.assertEqual((rc, v["ok"], v["error"]), (1, False, "invalid_response"))
                self.assertEqual(hits, [xprobe.STATUS_PATH])
                self.assertNotIn("EVIL", json.dumps(v))
        for st, payload in ((403, {"ok": False, "error": "EVIL <b>"}), (500, {"error": "operator_only"}),
                            (403, b"EVIL not json"), (200, b"{" * 70000)):
            with self.subTest(st=st, p=str(payload)[:30]):
                port, _ = self.fake_service(st, payload)
                rc, v = self.cli(d, port=port)
                self.assertEqual(rc, 1)
                self.assertIn(v["error"], ("refused", "invalid_response"))
                self.assertNotIn("EVIL", json.dumps(v))

    def test_cli_reads_status_and_evdev_counters(self):
        self.as_linux(0)
        self.a.x_probe.record(body(page="tv", mode="tv", event="load", last=None))
        d = self.nav_dir()
        rc, v = self.cli(d)                                       # aide absente : honnête, jamais une erreur
        self.assertEqual(rc, 0)
        self.assertTrue(v["pages"]["tv"]["present"])
        self.assertEqual(v["evdev"], {"present": False, "error": "unavailable"})
        view = {"present": True, "armed": True, "move": 4, "ok": 1, "age_s": 2.5}
        srv = xescape.NavServer(d, lambda: view, peer_uid=lambda c: 0).start()
        self.addCleanup(srv.stop)
        rc, v = self.cli(d)
        self.assertEqual((rc, v["evdev"]), (0, view))             # compteurs evdev EN MÉMOIRE de l'aide, joints
        expect = xstatus.status_clean(xprobe.status_view(self.a))
        self.assertEqual({k: x for k, x in v.items() if k != "evdev"},
                         dict(expect, pages=dict(expect["pages"], tv=dict(v["pages"]["tv"]))))
        self.as_linux(4242)                                       # non root : refus honnête, code 1
        rc, v = self.cli(d)
        self.assertEqual((rc, v["error"]), (1, "operator_only"))

    @unittest.skipUnless(os.path.exists("/proc/net/tcp"), "Linux : /proc/net/tcp")
    def test_listener_uid_real_proc_net_tcp(self):
        import http.client
        port = self.s.web.tv_port()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.connect()
        self.addCleanup(c.close)
        self.assertEqual(xstatus.listener_uid(port, c.sock.getsockname()[1]), os.getuid())
        self.assertIsNone(xstatus.listener_uid(port, 1))          # aucune connexion : None (refus)
        self.assertEqual(xstatus.read_status(port, os.getuid() + 1), (1, {"ok": False, "error": "listener"}))



# --------------------------------------------------------------------------------------------- schéma fermé
class TestStatusSchema(unittest.TestCase):
    """L3 : xstatus.status_clean accepte EXACTEMENT ce que produit xprobe.status_view (copie reconstruite) et refuse
    tout le reste ; status_error ne rend qu'un code d'une liste fermée."""

    def setUp(self):
        self.a = make_app(self)

    def good(self):
        self.a.x_probe.record(body())
        self.a.x_probe.note_http(200)
        return json.loads(json.dumps(xprobe.status_view(self.a)))

    def test_real_views_round_trip(self):
        v = self.good()
        self.assertEqual(xstatus.status_clean(v), v)
        self.assertIsNot(xstatus.status_clean(v)["pages"]["x"], v["pages"]["x"])   # copie, jamais l'objet reçu
        self.a.x_probe.record(body(page="tv", mode="tv", event="beat", last=None))
        v = json.loads(json.dumps(xprobe.status_view(self.a)))
        self.assertEqual(xstatus.status_clean(v), v)
        b = make_app(self, OPENTV_X_TV="0")
        off = json.loads(json.dumps(xprobe.status_view(b)))
        self.assertEqual(xstatus.status_clean(off), off)

    def test_every_deviation_refused(self):
        g = self.good()
        x = g["pages"]["x"]

        def page(**kw):
            return dict(g, pages=dict(g["pages"], x=dict(x, **kw)))

        def drop(d, k):
            return {a: b for a, b in d.items() if a != k}
        bad = [None, [], "x", dict(g, extra=1), drop(g, "http"), dict(g, ok=False), dict(g, ok=1),
               dict(g, enabled="yes"), dict(g, version="0.7.8 EVIL"), dict(g, version=[1]), dict(g, ttl_s=999),
               dict(g, ttl_s=120.0), dict(g, rejected=-1), dict(g, rejected=True), dict(g, http=dict(g["http"], x=1)),
               dict(g, http=drop(g["http"], "429")), dict(g, http=dict(g["http"], **{"403": "1"})),
               dict(g, pages={}), dict(g, pages=dict(g["pages"], evil={"present": False})),
               dict(g, pages=drop(g["pages"], "tv")), dict(g, pages=[]),
               page(extra=1), page(mode="tv"), page(mode="EVIL"), page(event="click"), page(seq=-1), page(seq=1.5),
               page(repeat=True), page(sw_s=xprobe.COUNT_MAX + 1), page(doc_focus=1), page(visible="true"),
               page(ext="EVIL"), page(age_s=xprobe.TTL_S), page(age_s=-1), page(age_s=True), page(age_s="1"),
               page(keys={"move": 1, "ok": 1}), page(keys={"move": 1, "ok": 1, "other": 0, "key": 1}),
               page(keys={"move": "1", "ok": 1, "other": 0}), page(keys=[1, 2, 3]), page(present=False),
               page(present=1), page(accepted=-1), page(rate_limited=None),
               page(last={"cls": "EVIL", "trusted": True, "editable": False, "doc_focus": True, "handled": True}),
               page(last=dict(x["last"], code=38)), page(last=dict(x["last"], trusted="yes")), page(last="move"),
               dict(g, pages=dict(g["pages"], tv={"present": False, "accepted": 0, "rate_limited": 0, "x": 1})),
               dict(g, pages=dict(g["pages"], tv={"present": True, "accepted": 0, "rate_limited": 0})),
               dict(g, pages=dict(g["pages"], tv=dict(x, mode="feed"))),          # /tv jamais un mode x.com
               {"ok": True, "enabled": False, "version": VERSION, "pages": {"x": {}}},
               {"ok": True, "enabled": False, "version": VERSION, "pages": {}, "extra": 1}]
        for b in bad:
            with self.subTest(b=str(b)[:120]):
                self.assertIsNone(xstatus.status_clean(b))

    def test_errors_closed(self):
        self.assertEqual(xstatus.status_error(403, {"ok": False, "error": "operator_only"}), "operator_only")
        self.assertEqual(xstatus.status_error(421, {"ok": False, "error": "host"}), "host")
        for st, b in ((403, {"error": "EVIL"}), (500, {"error": "operator_only"}), (403, None), (403, ["host"]),
                      (403, {"error": ["host"]}), (200, {"error": "host"})):
            self.assertEqual(xstatus.status_error(st, b), "refused")


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
        b.tv_client_uids = frozenset({4242})
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
        self.assertEqual(keys, sorted(["type", "mode", "event", "seq", "move", "ok", "other", "repeat", "synthetic",
                                       "last", "doc_focus", "visible"]))
        last = re.search(r"probe\.last = \{([^}]*)\};", self.nav).group(1)
        self.assertEqual(sorted(k.strip().split(":")[0] for k in last.split(",")),
                         ["cls", "doc_focus", "editable", "handled", "trusted"])
        self.assertIn("trusted: true", last)
        self.assertGreaterEqual(int(re.search(r"PROBE_MIN_MS = (\d+);", self.nav).group(1)), 400)
        # L1 : aucun plafond par document (le battement d'une page ouverte longtemps ne meurt jamais)
        self.assertNotIn("PROBE_MAX", self.nav)
        self.assertNotIn("probe.sent", self.nav)
        # M2 : pagehide envoie SEULEMENT l'instantané déjà en attente (aucune capture nouvelle)
        self.assertIn('window.addEventListener("pagehide", probeLeave, true);', self.nav)
        pl = self.nav[self.nav.index("function probeLeave()"):self.nav.index("function probeMode(mode)")]
        self.assertIn("if (!probe.timer) { return; }", pl)
        for bad in ("probeSend(", "bump(", "probe.last", "probe.pending =", "document.", "(e)"):
            self.assertNotIn(bad, pl, bad)
        pk = self.nav[self.nav.index("function probeKey(e)"):self.nav.index("function noteTyping(e)")]
        self.assertLess(pk.index('if (!e.isTrusted) { bump("synthetic"); return; }'), pk.index("probeSend("))
        # saisie dans un champ : ni comptée ni envoyée ; touche tenue : à part, jamais un envoi ; PUIS seulement la classe
        guard = pk.index('if (ed && cls === "other") { return; }')
        rep = pk.index('if (e.repeat) { bump("repeat"); return; }')
        self.assertLess(guard, rep)
        self.assertLess(rep, pk.index("bump(cls)"))
        self.assertLess(pk.index("bump(cls)"), pk.index("probeSend("))
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
        self.assertNotIn("storage.session", pump)                 # clé jamais touchée directement par la sonde
        self.assertIn('stale = !!j && j.error === "tv_key";', pump)   # 403 « tv_key » SEULEMENT (pas Origin)
        self.assertIn("if (stale) { await markDead(tv.key); }", pump)
        self.assertLess(pump.index("if (!tv.key) { probeQ.pending = null; break; }"), pump.index("fetch("))
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

    def test_heartbeats_periodic_within_ttl_and_tv_key_only_from_tv(self):
        """Battements x.com ET /tv périodiques, < TTL du service (au moins 2 par TTL) ; nav.js bat même page cachée
        (l'instantané dit doc_focus/visible) ; clé TV reprise SEULEMENT par tvkey.js sur /tv, jamais persistée."""
        tvkey = code_only(read(os.path.join(EXT, "tvkey.js")))
        for src in (self.nav, tvkey):
            beat = int(re.search(r"BEAT_MS = (\d+);", src).group(1))
            self.assertLessEqual(beat * 2, xprobe.TTL_S * 1000)
        self.assertIn('setInterval(() => probeSend("beat"), BEAT_MS);', self.nav)
        self.assertIn('setInterval(() => send({ type: "tvbeat" }), BEAT_MS);', tvkey)
        self.assertIn('if (msg.type === "tvbeat" && fromTv(sender)) {', self.bg)
        # clé écrite au seul message « tvkey » de /tv (clé morte refusée) ; retirée seulement par markDead
        self.assertEqual(self.bg.count("storage.session.set({ tvkey:"), 1)
        self.assertLess(self.bg.index('if (msg.type === "tvkey" && fromTv(sender)) {'),
                        self.bg.index("storage.session.set({ tvkey:"))
        self.assertEqual(self.bg.count("storage.session.set("), 2)
        self.assertEqual(self.bg.count('storage.session.remove(["tvkey"])'), 1)
        md = self.bg[self.bg.index("function markDead(key)"):self.bg.index("async function share(")]
        self.assertIn('if (s.tvkey === key) { await chrome.storage.session.remove(["tvkey"]); }', md)
        self.assertIn("await chrome.storage.session.set({ tvdead: key });", md)
        self.assertIn("s.tvdead === msg.key", self.bg)
        for src in (self.bg, tvkey, self.nav):
            for bad in ("storage.local", "storage.sync", "localStorage", "indexedDB", "document.cookie"):
                self.assertNotIn(bad, src)


class TestReadmeGates(unittest.TestCase):
    """M2 : la gate terrain compare des DIFFÉRENCES par appui (lectures avant/après sur page stable), rejette les paires
    traversées par un rechargement ou un réarmement, tient compte du délai d'armement de l'aide et sépare le clavier
    USB ; L1/M1 : plus de plafond par document, clé périmée = silence total, aucun journal HTTP de la sonde."""

    def setUp(self):
        self.r = re.sub(r"\s+", " ", read(os.path.join(PI4, "README.md")))

    def test_delta_gate(self):
        for want in ("des **différences par appui** (Δ entre une lecture `x-probe` AVANT et une APRÈS)",
                     "attendre **≥ 2 s après `armed: true`** avant la première lecture",
                     "lecture A ; **un** appui court (jamais tenu) ; attendre **≥ 1 s**",
                     "Δ`pages.x.keys.<classe>` = 1, Δ des autres classes = 0, Δ`pages.x.repeat` = 0, "
                     "Δ`evdev.<classe>` = 1",
                     "**Paire A/B invalide**", "`pages.x.event` = `load` ou `pages.x.seq` a diminué",
                     "`evdev.move`/`evdev.ok` ont diminué (aide désarmée puis réarmée)",
                     "**Clavier USB — gate séparée**", "Δ`pages.x.keys` = 1 et Δ`evdev` = 0 — attendu, pas un échec",
                     "Δ`other` = 0", "Δ par appui"):
            self.assertIn(want, self.r)
        self.assertNotIn("égaux aux appuis physiques et à `pages.x.keys` (1:1)", self.r)   # valeurs absolues : non

    def test_cap_stale_and_logging_documented(self):
        self.assertNotIn("≤ 3000 par page", self.r)
        self.assertIn("**aucun plafond par document**", self.r)
        self.assertIn("ensuite **plus aucun envoi** — ni instantané, ni battement x.com ou `/tv` — jusqu'à une clé "
                      "**neuve** validée", self.r)
        self.assertIn("aucune ligne pour `/api/tv/x/probe`, **quel que soit le statut**", self.r)
        self.assertIn("l'instantané **déjà en attente** part tout de suite (au mieux), sans rien capturer de plus",
                      self.r)
        self.assertIn("**schéma fermé**", self.r)


@unittest.skipUnless(NODE, "node absent")
class TestExtensionProbeRuntime(unittest.TestCase):
    """VRAIS nav.js / tvkey.js / background.js exécutés sous Node (tests/x_probe_harness.js : DOM factice, minuteries
    manuelles, chrome.* espionné) : comportement, pas seulement le texte."""

    def run_harness(self, which, ext=EXT):
        p = subprocess.run([NODE, HARNESS, ext, which], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_nav_repeat_editable_and_visibility(self):
        r = self.run_harness("nav")
        sent, steps = r["sent"], r["steps"]
        for m in sent:
            self.assertEqual(set(m), {"type", "mode", "event", "seq", "move", "ok", "other", "repeat", "synthetic",
                                      "last", "doc_focus", "visible"}, m)
        # mot de passe : 6 lettres + Retour arrière + lettre tenue -> AUCUN envoi, AUCUN compteur
        self.assertEqual(steps["load"], steps["after_password"])
        self.assertTrue(all(m["other"] == 0 and m["repeat"] == 0 for m in sent[:steps["after_password"]]))
        # ↓ tenu (1 appui + 3 répétitions) : move 1 (comparable 1:1 aux appuis evdev), repeat 3, UN seul envoi
        self.assertEqual(steps["after_hold"] - steps["after_password"], 1)
        hold = sent[steps["after_hold"] - 1]
        self.assertEqual((hold["move"], hold["repeat"], hold["other"], hold["last"]["editable"]), (1, 3, 0, True))
        last = sent[-1]
        self.assertEqual((last["move"], last["ok"], last["other"], last["repeat"], last["synthetic"]), (1, 1, 1, 3, 1))
        # battement page cachée, document sans focus : envoyé quand même, et le dit
        self.assertEqual((last["event"], last["doc_focus"], last["visible"]), ("beat", False, False))
        self.assertTrue(all(m["doc_focus"] and m["visible"] for m in sent[:-1]))
        dump = json.dumps(sent)
        for bad in ("secret", "Backspace", "ArrowDown", "Enter", "password"):
            self.assertNotIn(bad, dump)

    def test_nav_long_lived_document_heartbeat_never_dies(self):
        """L1 : 4000 appuis (> l'ancien plafond de 3000 par document) puis 12 h sans touche : chaque appui part, le
        battement continue toutes les 30 s ; jamais deux envois à moins de 400 ms."""
        r = self.run_harness("nav_long")
        self.assertGreater(r["after_keys"], 4000)
        self.assertEqual(r["beats"], 12 * 3600 // 30)
        self.assertGreaterEqual(r["min_gap"], 400)
        self.assertEqual((r["last"]["event"], r["last"]["move"], r["last"]["ok"]), ("beat", 2000, 2000))

    def test_nav_pagehide_flushes_pending_only(self):
        """M2 : départ de la page : l'instantané EN ATTENTE part tout de suite (sans attendre 400 ms), une seule fois ;
        rien en attente : aucun envoi ; jamais un champ ni un événement nouveaux."""
        r = self.run_harness("nav_pagehide")
        st, sent = r["steps"], r["sent"]
        self.assertIn("pagehide", r["listeners"])
        self.assertEqual(st["idle_hide"], st["load"])             # rien en attente : rien envoyé
        self.assertEqual(st["after_hide"], st["before_hide"] + 1)  # en attente : envoyé AU pagehide
        self.assertEqual(st["hide_at"], 0)
        self.assertEqual(st["end"], st["after_hide"])             # minuterie annulée : jamais un doublon
        self.assertEqual(st["second_hide"], st["end"])
        # page restaurée (bfcache) : la minuterie annulée au pagehide ne vide jamais un NOUVEL instantané trop tôt
        self.assertEqual(st["restored_sends"], st["second_hide"] + 3)
        self.assertGreaterEqual(st["restored_gap"], 400)
        flushed = sent[st["after_hide"] - 1]
        self.assertEqual((flushed["event"], flushed["move"], flushed["ok"]), ("key", 1, 1))
        for m in sent:
            self.assertEqual(set(m), {"type", "mode", "event", "seq", "move", "ok", "other", "repeat", "synthetic",
                                      "last", "doc_focus", "visible"})
            self.assertIn(m["event"], ("load", "key"))

    def test_background_stale_key_stops_everything_until_fresh_key(self):
        """M1 : service redémarré : premier 403 « tv_key » -> clé retirée, PLUS AUCUN envoi (instantanés ni
        battements, x.com ni /tv) ; partage : « OpenTV a redémarré », sans réseau ; l'ancienne clé n'est jamais
        reprise ; clé NEUVE de /tv : la sonde reprend. 403 tardif de l'ancienne clé : la clé neuve reste."""
        r = self.run_harness("background_stale")
        m, posts, k1, k2, k3 = r["marks"], r["posts"], r["K1"], r["K2"], r["K3"]
        self.assertEqual(m["fresh"], 2)
        self.assertEqual(m["first_403"], 3)                       # UN seul envoi refusé
        self.assertIsNone(m["key_after_403"])
        self.assertEqual(m["after_stale"], 3)                     # 20 touches + 40 battements : rien
        self.assertEqual((m["after_share"], m["share"]["ok"]), (3, False))
        self.assertIn("OpenTV a redémarré", m["share"]["message"])
        self.assertEqual((m["dead_key_refused"], m["key_after_dead"]), (3, None))   # clé morte jamais reprise
        self.assertEqual(m["reopened"], 5)
        self.assertEqual([(p["key"], p["body"]["page"], p["body"]["event"]) for p in posts[3:5]],
                         [(k2, "tv", "load"), (k2, "tv", "beat")])
        self.assertEqual(m["held"], 1)                            # 403 tardif de K2 ET clé neuve K3 : même tour
        self.assertEqual(m["key_after_race"], k3)                 # écritures sérialisées : K3 jamais effacée
        self.assertEqual([p["key"] for p in posts[5:]], [k2, k3, k3])
        self.assertEqual(m["end"], len(posts))
        self.assertTrue(all(p["path"] == xprobe.PROBE_PATH for p in posts))
        self.assertEqual([p["key"] for p in posts].count(k1), 3)  # K1 : 2 acceptés + 1 refusé, plus jamais

    def test_tvkey_periodic_beat_booleans_only(self):
        sent = self.run_harness("tvkey")["sent"]
        self.assertEqual([m["type"] for m in sent], ["tvkey", "tvbeat", "tvbeat", "tvbeat", "tvbeat"])
        self.assertEqual(set(sent[0]), {"type", "key", "doc_focus", "visible"})
        for m in sent[1:]:
            self.assertEqual(set(m), {"type", "doc_focus", "visible"})   # jamais la clé dans un battement
        self.assertEqual((sent[-1]["doc_focus"], sent[-1]["visible"]), (False, False))

    def test_background_tv_beat_closed_body_and_sender(self):
        r = self.run_harness("background")
        self.assertEqual(r["before_key"], 0)                      # battement avant la clé : rien
        posts = r["posts"]
        self.assertEqual([(p["body"]["page"], p["body"]["event"]) for p in posts],
                         [("tv", "load"), ("tv", "beat"), ("x", "key"), ("x", "beat"), ("tv", "beat")])
        # (tvbeat de x.com et probe de /tv : refusés ; valeurs non booléennes : jamais « vrai »)
        for p in posts[3:]:
            self.assertEqual((p["body"]["doc_focus"], p["body"]["visible"]), (False, False))
        for p in posts:
            self.assertEqual(set(p["body"]), set(xprobe.FIELDS))
            self.assertIsNotNone(xprobe.validate(p["body"]), p["body"])   # le service accepte ce que l'extension envoie
        self.assertEqual((posts[1]["body"]["doc_focus"], posts[1]["body"]["visible"]), (False, False))
        self.assertEqual(posts[2]["body"]["repeat"], 5)
        dump = json.dumps([p["body"] for p in posts])
        for bad in ("ArrowUp", "secret", "code"):
            self.assertNotIn(bad, dump)


if __name__ == "__main__":
    unittest.main()
