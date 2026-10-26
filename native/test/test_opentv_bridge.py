"""Tests du pont NanoPi opentv-bridge (contrat native/NANOPI-API.md, API v1).

Depuis la racine : python3 -m unittest -v native.test.test_opentv_bridge
Le test Chromium réel est sauté si aucun Chromium n'est trouvé (OPENTV_TEST_CHROMIUM=<binaire> pour forcer).
Ces tests n'exercent ni le NanoPi, ni la TV, ni le compositeur Wayland.
"""
import glob
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "scripts", "nanopi", "opentv_bridge.py")
spec = importlib.util.spec_from_file_location("opentv_bridge", SRC)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

HTML = {"content-type": "text/html; charset=UTF-8"}


def quiet():
    return redirect_stdout(io.StringIO())


class FakeSite:
    """Faux site : {url: (status, headers, body)} ; journalise les requêtes."""

    def __init__(self, routes):
        self.routes, self.log = dict(routes), []

    def __call__(self, url, timeout):
        self.log.append(url)
        r = self.routes.get(url)
        if r is None:
            return 404, HTML, b"nope"
        if isinstance(r, Exception):
            raise r
        return r


def fakrov_routes(prefix="aab2ae3", root_links=None):
    links = root_links if root_links is not None else ['favicon.png', prefix]
    body = "<html><title>fakrov</title>" + "".join('<a href="%s">x</a>' % h for h in links) + "</html>"
    return {
        "https://fakrov.com/": (200, HTML, body.encode()),
        "https://fakrov.com/%s/" % prefix: (302, {"location": "https://fakrov.com/%s/home/fakrov" % prefix}, b""),
        "https://fakrov.com/%s/home/fakrov" % prefix: (200, HTML, LISTING.replace("PFX", prefix).encode()),
    }


LISTING = """<div class="trend-row">
<a class="trend-card" href="/PFX/b/fakrov/53744425"><div class="trend-card-poster">
 <img class="trend-card-img" src="https://image.tmdb.org/t/p/w300/a.jpg" alt="Contre l'Ouragan" loading="lazy">
 <div class="trend-card-info"><span class="trend-card-cat"><span class="trend-card-date">2026</span> · Drame</span>
 <div class="trend-card-title">Contre l'Ouragan</div></div></div></a>
<a class="trend-card" href="/PFX/b/fakrov/53744425"><img src="https://image.tmdb.org/t/p/w300/a.jpg" alt="doublon"></a>
<a class="trend-card" href="https://evil.example/PFX/b/fakrov/1"><img src="https://x/y.jpg" alt="étranger"></a>
<a class="trend-card" href="javascript:alert(1)"><img src="https://x/y.jpg" alt="js"></a>
<a class="trend-card" href="/PFX/b/fakrov/77"><img src="http://insecure/p.jpg" alt="Sans affiche &amp; HTTP">
 <span class="trend-card-cat"><span class="trend-card-date">1999</span> · Comédie</span></a>
<a href="/PFX/c/fakrov/2/0">catégorie</a>
</div>"""


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cache = os.path.join(self.d, "prefix.json")
        self.now = [1000.0]

    def tearDown(self):
        shutil.rmtree(self.d)

    def R(self, site, **kw):
        return B.Resolver("https://fakrov.com/", self.cache, fetch=site, clock=lambda: self.now[0], **kw)

    def test_real_shape_relative_link_and_prefix_redirect(self):
        r = self.R(FakeSite(fakrov_routes()))
        with quiet():
            o, p, st = r.current()
        self.assertEqual((o, p, st), ("https://fakrov.com", "aab2ae3", "fresh"))
        self.assertEqual(r.cache["listing_path"], "/aab2ae3/home/fakrov")
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)

    def test_root_redirect_and_http_upgrade(self):
        routes = fakrov_routes("zz9xk2")
        routes["https://fakrov.com/"] = (301, {"location": "http://fakrov.com/zz9xk2"}, b"")
        routes["https://fakrov.com/zz9xk2"] = (301, {"location": "http://fakrov.com/zz9xk2/"}, b"")
        routes["https://fakrov.com/zz9xk2/"] = (302, {"location": "/zz9xk2/home/fakrov"}, b"")
        with quiet():
            self.assertEqual(self.R(FakeSite(routes)).current()[1], "zz9xk2")

    def test_malicious_and_absolute_links(self):
        routes = fakrov_routes("good12", root_links=["https://evil.example/bad123", "//evil.example/bad456",
                                                     "https://user:pw@fakrov.com/bad789", "https://fakrov.com/good12/"])
        with quiet():
            self.assertEqual(self.R(FakeSite(routes)).current()[1], "good12")

    def test_foreign_redirect_refused(self):
        routes = fakrov_routes()
        routes["https://fakrov.com/aab2ae3/"] = (302, {"location": "https://evil.example/aab2ae3/"}, b"")
        with quiet(), self.assertRaises(B.DiscoveryFailed):
            self.R(FakeSite(routes)).current()

    def test_redirect_out_of_prefix_refused(self):
        routes = fakrov_routes()
        routes["https://fakrov.com/aab2ae3/"] = (302, {"location": "/other/"}, b"")
        routes["https://fakrov.com/other/"] = (200, HTML, b"<html>valide mais hors prefixe</html>")
        with quiet(), self.assertRaises(B.DiscoveryFailed) as cm:
            self.R(FakeSite(routes)).current()
        self.assertEqual(str(cm.exception), "redirect_out_of_prefix")

    def test_redirect_loop_bounded(self):
        routes = fakrov_routes()
        routes["https://fakrov.com/aab2ae3/"] = (302, {"location": "/aab2ae3/x"}, b"")
        routes["https://fakrov.com/aab2ae3/x"] = (302, {"location": "/aab2ae3/"}, b"")
        site = FakeSite(routes)
        with quiet(), self.assertRaises(B.DiscoveryFailed):
            self.R(site).current()
        self.assertLessEqual(len(site.log), 12)

    def test_not_html_or_404_or_timeout(self):
        for bad in [(200, {"content-type": "application/json"}, b"{}"), (404, HTML, b""), B.FetchError("timeout")]:
            routes = fakrov_routes()
            routes["https://fakrov.com/aab2ae3/home/fakrov"] = bad
            with quiet(), self.assertRaises(B.DiscoveryFailed):
                self.R(FakeSite(routes)).current()
            self.assertFalse(os.path.exists(self.cache))

    def test_no_candidate_and_garbage_html(self):
        routes = fakrov_routes(root_links=["favicon.png", "UPPER12", "a", "x" * 40])
        routes["https://fakrov.com/"] = (200, HTML, b"<a href='favicon.png'>" + b"\xff<<<<" * 1000)
        with quiet(), self.assertRaises(B.DiscoveryFailed):
            self.R(FakeSite(routes)).current()

    def test_cache_ttl_grace_and_rotation(self):
        site = FakeSite(fakrov_routes())
        r = self.R(site, ttl=100, grace=1000)
        with quiet():
            r.current()
        n = len(site.log)
        self.now[0] += 50
        self.assertEqual(r.current(), ("https://fakrov.com", "aab2ae3", "fresh"))
        self.now[0] += 100   # périmé mais dans la grâce : pas de réseau à l'ouverture
        self.assertEqual(r.current()[2], "stale")
        self.assertEqual(len(site.log), n)
        # rotation : l'ancien préfixe renvoie 404, la racine pointe le nouveau -> une seule redécouverte
        site.routes = fakrov_routes("new77ab")
        self.now[0] += 2000
        with quiet():
            self.assertEqual(r.current(), ("https://fakrov.com", "new77ab", "fresh"))
        # reprise du cache depuis le disque (redémarrage du service)
        r2 = self.R(FakeSite({}), ttl=100, grace=1000)
        self.assertEqual(r2.current()[1], "new77ab")

    def test_expired_and_unreachable_fails(self):
        r = self.R(FakeSite(fakrov_routes()), ttl=10, grace=20)
        with quiet():
            r.current()
        r.fetch = FakeSite({"https://fakrov.com/": B.FetchError("URLError")})
        self.now[0] += 15
        self.assertEqual(r.current()[2], "stale")
        self.now[0] += 10
        with quiet(), self.assertRaises(B.DiscoveryFailed):
            r.current()

    def test_refresh_lock_wait_respects_total_budget(self):
        r = self.R(FakeSite(fakrov_routes()))
        r.lock.acquire()
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(B.DiscoveryFailed, "busy"):
                r.refresh(budget=0.05)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            r.lock.release()

    def test_corrupt_cache_ignored(self):
        with open(self.cache, "w") as f:
            f.write('{"prefix": "../../etc", "validated_at": 5}')
        self.assertEqual(self.R(FakeSite({})).status(), "none")

    def test_root_must_be_https(self):
        for bad in ("http://fakrov.com/", "https://u:p@fakrov.com/", "https://fakrov.com:8443/"):
            with self.assertRaises(ValueError):
                B.Resolver(bad, self.cache)

    def test_http_fetch_no_follow_and_size_cap(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/r":
                    self.send_response(302)
                    self.send_header("Location", "/big")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"x" * (B.MAX_BODY + 10))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            st, h, body = B.http_fetch(base + "/r", 3)
            self.assertEqual((st, h.get("location"), body), (302, "/big", b""))
            with self.assertRaises(B.FetchError):
                B.http_fetch(base + "/big", 3)
            with self.assertRaises(B.FetchError):
                B.http_fetch("http://127.0.0.1:1/", 1)
        finally:
            srv.shutdown()


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.r = B.Resolver("https://fakrov.com/", "/nonexistent/x.json", fetch=FakeSite({}))
        self.re = B.Config({}).detail_re

    def test_listing_parse(self):
        items = B.parse_listing(LISTING.replace("PFX", "aab2ae3"), "https://fakrov.com/aab2ae3/home/fakrov", self.r, self.re)
        self.assertEqual([i["detail_path"] for i in items], ["b/fakrov/53744425", "b/fakrov/77"])
        a, b = items
        self.assertEqual((a["title"], a["year"], a["genre"], a["poster"]),
                         ("Contre l'Ouragan", 2026, "Drame", "https://image.tmdb.org/t/p/w300/a.jpg"))
        self.assertEqual((b["title"], b["year"], b["genre"], b["poster"]), ("Sans affiche & HTTP", 1999, "Comédie", None))

    def test_clean_item_and_load(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "c.json")
            raw = {"generated_at": "2026-10-25T00:00:00Z", "items": [
                {"title": "Réf", "detail_url": "https://fakrov.com/old1234/b/fakrov/5", "year": 2001, "imdb_id": "tt0123456",
                 "imdb_rating": 7.4, "imdb_votes": 1234, "description": "d" * 5000},
                {"title": "Doublon", "detail_path": "b/fakrov/5"},
                {"title": "Chemin interdit", "detail_path": "../../etc/passwd"},
                {"title": "Requête", "detail_path": "b/fakrov/5?x=1"},
                {"title": 5, "detail_path": "b/fakrov/6"},
                {"title": "Mauvais types", "detail_path": "b/fakrov/7", "year": True, "imdb_id": "tt1; rm",
                 "imdb_rating": 11, "poster": "javascript:alert(1)"},
                "pas un objet"]}
            with open(p, "w") as f:
                json.dump(raw, f)
            items, gen = B.load_catalogue(p, self.re)
            self.assertEqual(gen, "2026-10-25T00:00:00Z")
            self.assertEqual([i["detail_path"] for i in items], ["b/fakrov/5", "b/fakrov/7"])
            self.assertEqual((items[0]["imdb_id"], items[0]["imdb_rating"], len(items[0]["description"])), ("tt0123456", 7.4, 1200))
            self.assertEqual((items[1]["year"], items[1]["imdb_id"], items[1]["imdb_rating"], items[1]["poster"]), (None, None, None, None))
            self.assertEqual(B.load_catalogue(os.path.join(d, "absent.json"), self.re), ([], None))
        finally:
            shutil.rmtree(d)


# ----------------------------------------------------------------------------- contrôleur + API HTTP
class StubResolver:
    def __init__(self, origin="https://fakrov.com", prefix="aab2ae3", fail=False, token="fakrov"):
        self.origin, self.prefix, self.fail, self.host = origin, prefix, fail, "fakrov.com"
        self.anchor, self.token, self.checks = "fakrov.com", token, 0

    def current(self, budget=None, check=False):
        if self.fail:
            raise B.DiscoveryFailed("stub")
        self.checks += bool(check)
        return self.origin, self.prefix, "fresh"

    def epoch(self):
        return {"origin": self.origin, "host": self.host, "prefix": self.prefix, "token": self.token,
                "key": "%s/%s/%s" % (self.host, self.prefix, self.token)}

    def status(self):
        return "fresh"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def write_catalogue(path, n):
    B.atomic_write_json(path, {"items": [{"title": "Film %d" % i, "detail_path": "b/fakrov/%d" % (1000 + i),
                                          "poster": "https://image.tmdb.org/t/p/w300/%d.jpg" % i} for i in range(n)]})


class Harness:
    def __init__(self, n_items=14, env=None, resolver=None, factory=None, clock=None, catalogue=None, make=None):
        self.d = tempfile.mkdtemp()
        e = {"STATE_DIR": self.d, "ALLOW_IPS": "127.0.0.1", "DASHBOARD_UNIT": "", "APPS": "catalogue,docs",
             "UI_PORT": "0", "COLS": "6", "SOURCE_APPROVED": "1"}
        e.update(env or {})
        self.cfg = B.Config(e)
        if catalogue is not None:
            B.atomic_write_json(self.cfg.catalogue_file, catalogue)
        else:
            write_catalogue(self.cfg.catalogue_file, n_items)
        router = None
        if make is not None:
            resolver, router = make(self.d)
        self.resolver, self.router = resolver, router
        self.displays = []

        def fac(cfg):
            d = B.NullDisplay(cfg)
            if self.fail_open:
                d.fail_open = True
            self.displays.append(d)
            return d
        self.fail_open = False
        self.clock = clock or time.monotonic
        self.ctl = B.Controller(self.cfg, resolver or StubResolver(), factory or fac, clock=self.clock, router=router)
        self.api = B.Server(("127.0.0.1", 0), B.make_api_handler(self.ctl, self.cfg, B.RateLimiter(self.cfg.rate)))
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.api.server_address[1]

    def req(self, method, path):
        r = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), dict(e.headers)

    def wait_state(self, st, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if self.ctl.health()[0] == st:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self.api.shutdown()
        self.api.server_close()
        self.ctl.foryou_reset()
        shutil.rmtree(self.d, ignore_errors=True)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self._out = redirect_stdout(io.StringIO())
        self.out = self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        self.h.close()

    def open(self):
        code, b, hdr = self.h.req("POST", "/v1/session?app=catalogue")
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        return b["session"]

    def test_source_requires_explicit_owner_gate(self):
        h = Harness(env={"SOURCE_APPROVED": "0"})
        try:
            code, body, _ = h.req("POST", "/v1/session?app=catalogue")
            self.assertEqual((code, body.get("error")), (503, "source_unapproved"))
            self.assertIsNone(h.ctl.s)
        finally:
            h.close()

    def key(self, s, k):
        return self.h.req("POST", "/v1/key?session=%s&k=%s" % (s, k))

    def test_full_flow_catalogue_player_back_dashboard(self):
        code, b, hdr = self.h.req("POST", "/v1/session?app=catalogue")
        self.assertEqual((code, b["ok"], b["state"] in ("starting", "catalogue")), (200, True, True))
        self.assertRegex(b["session"], r"^[A-Za-z0-9_-]{8,64}$")
        self.assertEqual(hdr.get("Access-Control-Allow-Origin"), "*")
        s = b["session"]
        self.assertTrue(self.h.wait_state("catalogue"))
        self.assertEqual(self.h.displays[0].calls[0], ("open", "http://127.0.0.1:0/ui/"))
        # idempotent
        self.assertEqual(self.h.req("POST", "/v1/session?app=catalogue")[1]["session"], s)
        for k in ("right", "right", "down"):
            self.assertEqual(self.key(s, k)[1], {"ok": True, "state": "catalogue"})
        self.assertEqual(self.h.ctl.view()["cursor"], 8)
        self.assertEqual(self.key(s, "ok")[1]["state"], "player")
        d = self.h.displays[0]
        self.assertEqual(d.calls[-1], ("navigate", "https://fakrov.com/aab2ae3/b/fakrov/1008"))
        for k in ("up", "left", "ok", "right"):
            self.assertEqual(self.key(s, k)[1]["state"], "player")
        self.assertEqual([c for c in d.calls if c[0] == "key"], [("key", "up"), ("key", "left"), ("key", "ok"), ("key", "right")])
        self.assertEqual(self.key(s, "back")[1]["state"], "catalogue")
        self.assertEqual(d.calls[-1], ("navigate", "http://127.0.0.1:0/ui/"))
        self.assertEqual(self.h.ctl.view()["cursor"], 8)   # position conservée
        self.assertEqual(self.key(s, "back")[1]["state"], "dashboard")
        self.assertTrue(d.closed.wait(2))
        self.assertEqual(self.h.req("GET", "/v1/health?session=" + s)[1], {"ok": True, "api": 1, "state": "dashboard", "session": None})
        self.assertEqual(self.key(s, "up")[:2], (410, {"ok": False, "error": "no_session"}))

    def test_grid_bounds(self):
        s = self.open()
        c = lambda: self.h.ctl.view()["cursor"]
        self.key(s, "left"); self.key(s, "up")
        self.assertEqual(c(), 0)
        for _ in range(9):
            self.key(s, "right")
        self.assertEqual(c(), 5)          # bord droit de la rangée
        self.key(s, "down"); self.key(s, "down")
        self.assertEqual(c(), 13)         # 14 éléments : dernière rangée incomplète -> dernier élément
        self.key(s, "down")
        self.assertEqual(c(), 13)

    def test_errors(self):
        self.assertEqual(self.h.req("POST", "/v1/session?app=nope")[:2], (404, {"ok": False, "error": "unknown_app"}))
        self.assertEqual(self.h.req("POST", "/v1/session?app=../x")[0], 404)
        s = self.open()
        self.assertEqual(self.h.req("POST", "/v1/session?app=docs")[:2], (409, {"ok": False, "error": "busy"}))
        for bad in ("enter", "", "ok%26k%3Dback", "UP"):
            self.assertEqual(self.key(s, bad)[:2], (400, {"ok": False, "error": "bad_key"}))
        self.assertEqual(self.key("wrongsession123", "up")[0], 410)
        self.assertEqual(self.h.req("GET", "/v1/session?app=catalogue")[0], 405)
        self.assertEqual(self.h.req("GET", "/v1/../etc/passwd")[0], 404)
        self.assertEqual(self.h.req("POST", "/v1/health")[0], 405)

    def test_close_semantics(self):
        s = self.open()
        # fermeture d'une ancienne session : ne ferme pas la session courante
        self.assertEqual(self.h.req("POST", "/v1/close?session=oldsession99&reason=user")[1], {"ok": True, "state": "dashboard"})
        self.assertEqual(self.h.ctl.health(s), ("catalogue", s))
        self.assertEqual(self.h.req("POST", "/v1/close?app=docs&reason=user")[1]["state"], "dashboard")
        self.assertEqual(self.h.ctl.health(s)[0], "catalogue")
        self.assertEqual(self.h.req("POST", "/v1/close?session=%s&reason=<script>" % s)[1], {"ok": True, "state": "dashboard"})
        self.assertIn('"reason": "tv-other"', self.out.getvalue())
        self.assertEqual(self.h.req("POST", "/v1/close?app=catalogue&reason=home")[1]["state"], "dashboard")   # idempotent
        s2 = self.open()
        self.assertEqual(self.h.req("POST", "/v1/close?app=catalogue&reason=cancel")[1]["state"], "dashboard")
        self.assertEqual(self.h.ctl.health(s2), ("dashboard", None))

    def test_back_while_starting_closes(self):
        gate = threading.Event()

        class Slow(B.NullDisplay):
            def open(self, url):
                gate.wait(3)
        self.h.ctl.factory = Slow
        code, b, _ = self.h.req("POST", "/v1/session?app=catalogue")
        self.assertEqual(b["state"], "starting")
        self.assertEqual(self.key(b["session"], "right")[1]["state"], "starting")
        self.assertEqual(self.key(b["session"], "ok")[1]["state"], "starting")
        self.assertEqual(self.key(b["session"], "back")[1]["state"], "dashboard")
        gate.set()
        time.sleep(0.2)
        self.assertEqual(self.h.ctl.health()[0], "dashboard")   # l'ouverture tardive ne ressuscite pas la session

    def test_display_open_failure_returns_dashboard(self):
        self.h.fail_open = True
        code, b, _ = self.h.req("POST", "/v1/session?app=catalogue")
        self.assertEqual(code, 200)
        self.assertTrue(self.h.wait_state("dashboard"))
        self.assertIn("display_open_failed", self.out.getvalue())
        self.assertEqual(self.h.req("GET", "/v1/health?session=" + b["session"])[1]["session"], None)

    def test_player_navigate_failure_stays_in_catalogue(self):
        s = self.open()

        def boom(url):
            raise B.CDPError("navigate:net::ERR_NAME_NOT_RESOLVED")
        self.h.displays[0].navigate = boom
        self.assertEqual(self.key(s, "ok")[1]["state"], "catalogue")
        self.assertEqual(self.h.ctl.view()["message"], "Page indisponible")

    def test_discovery_failed_503(self):
        self.h.ctl.resolver = StubResolver(fail=True)
        self.assertEqual(self.h.req("POST", "/v1/session?app=catalogue")[:2], (503, {"ok": False, "error": "discovery_failed"}))
        self.assertEqual(self.h.ctl.health(), ("dashboard", None))
        self.assertEqual(self.h.displays, [])

    def test_display_off_503(self):
        self.h.ctl.factory = None
        self.assertEqual(self.h.req("POST", "/v1/session?app=catalogue")[:2], (503, {"ok": False, "error": "display_unavailable"}))
        self.assertEqual(self.h.req("GET", "/v1/health")[1]["state"], "dashboard")

    def test_rate_limit(self):
        codes = [self.h.req("GET", "/v1/health")[0] for _ in range(30)]
        self.assertIn(429, codes)
        self.assertEqual(codes[:20], [200] * 20)


class AccessTests(unittest.TestCase):
    def test_forbidden_ip(self):
        h = Harness(env={"ALLOW_IPS": "192.168.2.14"})
        try:
            with quiet():
                self.assertEqual(h.req("POST", "/v1/session?app=catalogue")[:2], (403, {"ok": False, "error": "forbidden"}))
            self.assertEqual(h.ctl.health(), ("dashboard", None))
        finally:
            h.close()

    def test_ui_server_loopback_only_and_csp(self):
        h = Harness()
        try:
            ui = B.Server(("127.0.0.1", 0), B.make_ui_handler(h.ctl, os.path.join(ROOT, "scripts/nanopi/opentv_bridge_ui.html")))
            threading.Thread(target=ui.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % ui.server_address[1]
            with urllib.request.urlopen(base + "/ui/") as r:
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
                self.assertIn(b"window.__opentv", r.read())
            with urllib.request.urlopen(base + "/ui/catalogue.json") as r:
                self.assertEqual(len(json.loads(r.read())["items"]), 14)
            with urllib.request.urlopen(base + "/ui/tmdb-logo.svg") as r:
                self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
                self.assertTrue(r.read().startswith(b"<svg"))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/ui/../../etc/passwd")
            cm.exception.close()
            ui.shutdown()
            ui.server_close()
        finally:
            h.close()


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.h = Harness(clock=self.clock, env={"HEARTBEAT_S": "30", "CATALOGUE_IDLE_S": "300",
                                                "PLAYER_MAX_S": "1000", "START_TIMEOUT_S": "45"})
        self._out = redirect_stdout(io.StringIO())
        self.out = self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        self.h.close()

    def opened(self):
        s = self.h.ctl.open("catalogue")
        self.assertTrue(self.h.wait_state("catalogue"))
        return s["id"]

    def ended(self, reason):
        self.h.ctl.tick()
        self.assertEqual(self.h.ctl.health()[0], "dashboard")
        self.assertIn('"reason": "%s"' % reason, self.out.getvalue())
        self.assertTrue(self.h.displays[-1].closed.wait(2))

    def test_tv_lost(self):
        sid = self.opened()
        for _ in range(5):   # sondage santé toutes les 2 s : la session vit, même sans touche
            self.clock.t += 20
            self.h.ctl.health(sid)
            self.h.ctl.tick()
        self.assertEqual(self.h.ctl.health()[0], "catalogue")
        self.clock.t += 31
        self.ended("tv-lost")

    def test_catalogue_idle(self):
        sid = self.opened()
        for _ in range(16):
            self.clock.t += 20
            self.h.ctl.health(sid)
            self.h.ctl.tick()
        self.ended("idle")

    def test_player_cap_but_no_idle_in_player(self):
        sid = self.opened()
        self.h.ctl.key(sid, "ok")
        for _ in range(45):   # 900 s sans touche : la lecture continue
            self.clock.t += 20
            self.h.ctl.health(sid)
            self.h.ctl.tick()
        self.assertEqual(self.h.ctl.health()[0], "player")
        self.clock.t += 120
        self.h.ctl.health(sid)
        self.ended("player-cap")

    def test_display_died(self):
        self.opened()
        self.h.displays[0]._alive = False
        self.ended("display-died")

    def test_start_timeout(self):
        gate = threading.Event()

        class Hang(B.NullDisplay):
            def open(self, url):
                gate.wait(5)
        self.h.ctl.factory = Hang
        sid = self.h.ctl.open("catalogue")["id"]
        self.clock.t += 20
        self.h.ctl.health(sid)
        self.h.ctl.tick()
        self.assertEqual(self.h.ctl.health()[0], "starting")
        self.clock.t += 26
        self.h.ctl.health(sid)
        self.h.ctl.tick()
        self.assertEqual(self.h.ctl.health()[0], "dashboard")
        gate.set()

    def test_shutdown_closes_display(self):
        self.opened()
        self.h.ctl.shutdown()
        self.assertTrue(self.h.displays[0].closed.is_set())


class DashboardCheckTests(unittest.TestCase):
    def test_only_read_only_is_active(self):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "calls")
            shim = os.path.join(d, "systemctl")
            with open(shim, "w") as f:
                f.write('#!/bin/sh\necho "$@" >> %s\necho active\n' % log)
            os.chmod(shim, 0o755)
            h = Harness(env={"DASHBOARD_UNIT": "muse-display.service"})
            old = os.environ["PATH"]
            os.environ["PATH"] = d + os.pathsep + old
            try:
                with quiet() as out:
                    s = h.ctl.open("catalogue")
                    self.assertTrue(h.wait_state("catalogue"))
                    h.ctl.close(sid=s["id"], reason="user")
                    end = time.time() + 3
                    while time.time() < end and not os.path.exists(log):
                        time.sleep(0.05)
                    time.sleep(0.1)
            finally:
                os.environ["PATH"] = old
                h.close()
            with open(log) as f:
                self.assertEqual(f.read().splitlines(), ["--user is-active muse-display.service"])
        finally:
            shutil.rmtree(d)


# ----------------------------------------------------------------------------- Chromium réel (CDP par tube)
def find_chromium():
    env = os.environ.get("OPENTV_TEST_CHROMIUM")
    if env:
        return env
    pats = [os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
            os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[-1]
    for n in ("chromium", "chromium-browser", "google-chrome"):
        if shutil.which(n):
            return shutil.which(n)
    return None


FILM_PAGE = b"""<!doctype html><title>film</title><body><script>
window.keys = [];
document.addEventListener('keydown', function (e) {
  keys.push(e.key);
  if (e.key === 'Enter') window.open('/popup', '_blank');
});
</script>Film public</body>"""


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class RealChromiumTests(unittest.TestCase):
    def test_kiosk_cdp_pipe_end_to_end(self):
        class Film(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(FILM_PAGE)
        film = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Film)
        threading.Thread(target=film.serve_forever, daemon=True).start()
        port = film.server_address[1]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            ui_port = s.getsockname()[1]
        rt = tempfile.mkdtemp()
        kiosks = []

        def factory(cfg):
            k = B.ChromiumKiosk(cfg)
            kiosks.append(k)
            return k
        h = Harness(n_items=14, resolver=StubResolver(origin="http://127.0.0.1:%d" % port), factory=factory,
                    env={"UI_PORT": str(ui_port), "COMPOSITOR_CMD": "", "BROWSER_CMD": shlex_quote(find_chromium()),
                         "BROWSER_FLAGS": "--headless=new --no-first-run --no-default-browser-check --disable-gpu "
                                          "--use-mock-keychain --password-store=basic --window-size=1920,1080",
                         "XDG_RUNTIME_DIR": rt})
        ui = B.Server(("127.0.0.1", ui_port), B.make_ui_handler(h.ctl, os.path.join(ROOT, "scripts/nanopi/opentv_bridge_ui.html")))
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                t0 = time.time()
                s = h.ctl.open("catalogue")["id"]
                self.assertTrue(h.wait_state("catalogue", timeout=30), out.getvalue())
                k = kiosks[0]
                started = time.time() - t0

                def ev(expr, want, timeout=5):
                    end = time.time() + timeout
                    v = None
                    while time.time() < end:
                        v = k.evaluate(expr)
                        if v == want:
                            return v
                        time.sleep(0.05)
                    return v
                self.assertEqual(ev("document.body.getAttribute('data-ready')", "1"), "1")
                self.assertEqual(ev("document.querySelectorAll('.card').length", 14), 14)
                for key in ("right", "right", "down"):
                    h.ctl.key(s, key)
                self.assertEqual(ev("document.body.getAttribute('data-cursor')", "8"), "8")
                self.assertEqual(ev("document.getElementById('title').textContent", "Film 8"), "Film 8")
                self.assertEqual(h.ctl.key(s, "ok"), "player")
                self.assertEqual(ev("location.pathname", "/aab2ae3/b/fakrov/1008"), "/aab2ae3/b/fakrov/1008")
                for key in ("down", "left", "ok"):
                    h.ctl.key(s, key)
                self.assertEqual(ev("window.keys.join(',')", "ArrowDown,ArrowLeft,Enter"), "ArrowDown,ArrowLeft,Enter")
                time.sleep(1.0)   # la fenêtre surgissante ouverte par Entrée doit être refermée
                pages = [t for t in k.cdp.send("Target.getTargets")["targetInfos"] if t["type"] == "page"]
                self.assertEqual(len(pages), 1, pages)
                self.assertEqual(h.ctl.key(s, "back"), "catalogue")
                self.assertEqual(ev("document.body.getAttribute('data-cursor')", "8"), "8")
                # panne du navigateur -> le chien de garde ferme la session
                os.killpg(k.browser.pid, 9)
                time.sleep(0.5)
                self.assertFalse(k.alive())
                h.ctl.tick()
                self.assertEqual(h.ctl.health()[0], "dashboard")
                self.assertTrue(k.closed.wait(5))
                time.sleep(0.5)
                self.assertIsNone(k.profile)
                self.assertEqual(os.listdir(rt), [])
                # nouvelle session puis fermeture propre : plus aucun processus
                s2 = h.ctl.open("catalogue")["id"]
                self.assertTrue(h.wait_state("catalogue", timeout=30))
                k2 = kiosks[1]
                pid = k2.browser.pid
                h.ctl.close(sid=s2, reason="user")
                self.assertTrue(k2.closed.wait(5))
                end = time.time() + 6
                while time.time() < end and k2.browser.poll() is None:
                    time.sleep(0.1)
                self.assertIsNotNone(k2.browser.poll())
                with self.assertRaises(ProcessLookupError):
                    os.killpg(pid, 0)
            sys.stderr.write("\n  [chromium réel : ouverture %.1f s]\n" % started)
        finally:
            for k in kiosks:
                k.close()
            ui.shutdown()
            ui.server_close()
            film.shutdown()
            film.server_close()
            h.close()
            shutil.rmtree(rt, ignore_errors=True)


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


# ----------------------------------------------------------------------------- installation / retour arrière
SYSTEMCTL_SHIM = r"""#!/bin/sh
echo "systemctl $*" >> "$SHIM_LOG"
case "$*" in
  *"show muse-display.service"*) n=$(cat "$SHIM_DIR/dash_calls" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$SHIM_DIR/dash_calls"
     if [ "$n" -ge 2 ] && [ -f "$SHIM_DIR/dash_changes" ]; then printf 'active\n999\n1\n'; else printf "$DASH_STATE"; fi ;;
  *"is-active --quiet"*) exit 3 ;;
  *"is-active"*) echo active ;;
esac
exit 0
"""


class InstallScriptTests(unittest.TestCase):
    def run_install(self, dash="active\n790\n0\n", dash_changes=False, args=()):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        bindir, home = os.path.join(d, "bin"), os.path.join(d, "home")
        os.makedirs(bindir); os.makedirs(home)
        for name, body in (("systemctl", SYSTEMCTL_SHIM), ("systemd-analyze", "#!/bin/sh\necho \"systemd-analyze $*\" >> \"$SHIM_LOG\"\n"),
                           ("sleep", "#!/bin/sh\nexit 0\n"), ("pgrep", "#!/bin/sh\nexit 1\n")):
            with open(os.path.join(bindir, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(bindir, name), 0o755)
        if dash_changes:
            open(os.path.join(bindir, "dash_changes"), "w").close()
        log = os.path.join(d, "calls")
        env = {"HOME": home, "PATH": bindir + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin", "SHIM_LOG": log, "SHIM_DIR": bindir, "DASH_STATE": dash}
        r = subprocess.run(["bash", os.path.join(ROOT, "scripts/nanopi/install-bridge.sh")] + list(args),
                           env=env, capture_output=True, text=True, timeout=30)
        calls = []
        if os.path.exists(log):
            with open(log) as f:
                calls = f.read().splitlines()
        return r, calls, home

    def test_install_default_off_and_never_touches_dashboard(self):
        r, calls, home = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(home, ".config/systemd/user/opentv-bridge.env")) as f:
            conf = f.read()
        self.assertIn("DISPLAY_BACKEND=off", conf)
        self.assertIn("ALLOW_IPS=192.168.2.14", conf)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, ".config/systemd/user/opentv-bridge.env")).st_mode), 0o600)
        self.assertTrue(os.path.exists(os.path.join(home, "opentv-bridge/opentv_bridge_ui.html")))
        muse = [c for c in calls if "muse" in c]
        self.assertTrue(muse and all(" show muse-display.service " in c + " " for c in muse), muse)
        self.assertFalse([c for c in calls if re_any(c, ("stop", "kill", "restart muse", "musegadget", "cec-pinmux"))], calls)

    def test_refuses_when_dashboard_down(self):
        r, calls, home = self.run_install(dash="inactive\n0\n0\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("muse-display not active", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(home, "opentv-bridge")))

    def test_accepts_real_systemctl_value_order(self):
        # systemd ne garantit pas l'ordre demandé avec --value; le NanoPi renvoie PID, NRestarts, ActiveState.
        r, _, _ = self.run_install(dash="765\n0\nactive\n")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dashboard_change_triggers_rollback(self):
        r, calls, home = self.run_install(dash_changes=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ALERT", r.stderr)
        self.assertIn("systemctl --user disable --now opentv-bridge.service", calls)


def re_any(s, words):
    return any(w in s for w in words)


# ----------------------------------------------------------------------------- service réel (sous-processus)
class ServeSmokeTests(unittest.TestCase):
    def test_serve_subprocess_null_display(self):
        d = tempfile.mkdtemp()
        with socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0)); b.bind(("127.0.0.1", 0))
            port, ui_port = a.getsockname()[1], b.getsockname()[1]
        write_catalogue(os.path.join(d, "catalogue.json"), 3)
        B.atomic_write_json(os.path.join(d, "prefix.json"), {"prefix": "aab2ae3", "discovered_at": time.time(),
                                                            "validated_at": time.time(), "listing_path": "/aab2ae3/home/fakrov"})
        env = dict(os.environ, STATE_DIR=d, PORT=str(port), UI_PORT=str(ui_port), BIND="127.0.0.1",
                   ALLOW_IPS="127.0.0.1", DISPLAY_BACKEND="null", SOURCE_APPROVED="1",
                   CATALOGUE_REFRESH_S="0", DASHBOARD_UNIT="")
        p = subprocess.Popen([sys.executable, "-I", SRC, "serve"], env=env, stdout=subprocess.PIPE, text=True)
        try:
            base = "http://127.0.0.1:%d" % port
            end = time.time() + 5
            while True:
                try:
                    urllib.request.urlopen(base + "/v1/health", timeout=1).close()
                    break
                except OSError:
                    if time.time() > end:
                        raise
                    time.sleep(0.1)
            with urllib.request.urlopen(urllib.request.Request(base + "/v1/session?app=catalogue", method="POST", data=b"")) as resp:
                r = json.loads(resp.read())
            self.assertTrue(r["ok"])
            p.send_signal(15)
            out, _ = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0)
            events = [json.loads(l)["event"] for l in out.splitlines()]
            self.assertEqual(events[0], "start")
            self.assertIn("session_open", events)
            self.assertIn("session_end", events)
            self.assertEqual(events[-1], "stop")
        finally:
            if p.poll() is None:
                p.kill()
            shutil.rmtree(d)


# ----------------------------------------------------------------------------- migration de domaine fakrov -> wadrox
JSONH = {"content-type": "application/json"}
MARIO_SID, MARIO_OLD, MARIO_NEW = "17397", "52277985", "87837453"   # faits vérifiés : 3005 × id puis 5049 × id
WAD_IDS = (17397, 17885, 17884, 17883)
API_IDS = [(13321, "Spider-Man"), (17833, "Spider-Man 2021"), (17001, "Page deux")]


def wad_listing(prefix="99c7f65", token="wadrox", ids=WAD_IDS, extra=""):
    # les liens de la fixture reproduisent la forme réelle (5049 × id) ; le code n'en déduit jamais rien
    cards = "".join('<a class="trend-card" href="/%s/b/%s/%d"><div class="trend-card-poster">'
                    '<img src="https://image.tmdb.org/t/p/w300/%d.jpg" alt="Film %d">'
                    '<button class="card-info-btn" data-id="%d" data-title="Film %d"></button></div></a>'
                    % (prefix, token, sid * 5049, sid, sid, sid, sid) for sid in ids)
    return ('<html><title>wadrox</title><div class="trend-row">%s%s</div></html>' % (cards, extra)).encode()


def api_url(offset, prefix="99c7f65", token="wadrox", host="wadrox.com"):
    return "https://%s/%s/api_films.php?offset=%d&limit=20&folder=%s&pr=%s" % (host, prefix, offset, prefix, token)


def api_body(films, more):
    return json.dumps({"films": films, "hasMore": more}).encode()


FAKROV_MOVED = (b"<html><head><title>fakrov devient wadrox.com</title></head><body><h1>fakrov devient wadrox.com</h1>"
                b"<p>Nouvelle adresse : <a href=\"https://wadrox.com/\">wadrox.com</a></p></body></html>")


def wadrox_routes(prefix="99c7f65", listing=None, api=None):
    r = {
        "https://fakrov.com/": (200, HTML, FAKROV_MOVED),
        "https://fakrov.com/aab2ae3/": (404, HTML, b"gone"),
        "https://wadrox.com/": (200, HTML, ("<html><title>wadrox wadrox.com</title><a data-ref='4d01' "
                                            "href='%s'>Entrer</a></html>" % prefix).encode()),
        "https://wadrox.com/%s/" % prefix: (302, {"location": "/%s/home/wadrox" % prefix}, b""),
        "https://wadrox.com/%s/home/wadrox" % prefix: (200, HTML, listing or wad_listing(prefix)),
    }
    pages = api if api is not None else [
        ([{"id": str(s), "title": n, "link": "/%s/b/wadrox/%d" % (prefix, s * 5049)} for s, n in API_IDS[:2]], True),
        ([{"id": str(API_IDS[2][0]), "title": API_IDS[2][1], "link": "/%s/b/wadrox/%d" % (prefix, API_IDS[2][0] * 5049)}], False),
    ]
    off = 0
    for films, more in pages:
        r[api_url(off, prefix)] = (200, JSONH, api_body(films, more))
        off += len(films)
    return r


def write_legacy_cache(path, validated_at):
    with open(path, "w") as f:   # format v1 tel que présent sur le NanoPi : ni hôte, ni jeton
        json.dump({"prefix": "aab2ae3", "discovered_at": validated_at, "validated_at": validated_at,
                   "listing_path": "/aab2ae3/home/fakrov"}, f)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cache = os.path.join(self.d, "prefix.json")
        self.now = [100000.0]
        self._q = quiet()
        self.out = self._q.__enter__()

    def tearDown(self):
        self._q.__exit__(None, None, None)
        shutil.rmtree(self.d)

    def R(self, site, **kw):
        kw.setdefault("session_check", 300)
        return B.Resolver("https://fakrov.com/", self.cache, fetch=site, clock=lambda: self.now[0], **kw)

    def test_genuine_fakrov_to_wadrox_from_legacy_cache(self):
        write_legacy_cache(self.cache, self.now[0] - 1000)
        site = FakeSite(wadrox_routes())
        r = self.R(site)
        self.assertEqual(r.epoch()["key"], "fakrov.com/aab2ae3/fakrov")   # cache v1 relu, jeton déduit
        self.assertEqual(r.current(check=True), ("https://wadrox.com", "99c7f65", "fresh"))
        ep = r.epoch()
        self.assertEqual((ep["host"], ep["prefix"], ep["token"]), ("wadrox.com", "99c7f65", "wadrox"))
        with open(self.cache) as f:
            disk = json.load(f)
        self.assertEqual((disk["v"], disk["anchor"], disk["host"], disk["token"], disk["via"]),
                         (2, "fakrov.com", "wadrox.com", "wadrox", ["fakrov.com"]))
        self.assertEqual(disk["history"][0]["host"], "fakrov.com")
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        self.assertNotIn("https://fakrov.com/aab2ae3/", site.log)   # racine faisant autorité : pas de vieux préfixe
        self.assertTrue(all(u.startswith(("https://fakrov.com/", "https://wadrox.com/")) for u in site.log))
        self.assertIn('"migrated": true', self.out.getvalue())

    def test_fresh_install_follows_advertised_migration(self):
        r = self.R(FakeSite(wadrox_routes()))
        self.assertEqual(r.current()[:2], ("https://wadrox.com", "99c7f65"))
        self.assertEqual(r.cache["anchor"], "fakrov.com")

    def test_malicious_cross_origin_link_refused(self):
        routes = wadrox_routes()
        # ancre étrangère non citée dans le texte de la page : pas une annonce de migration
        routes["https://fakrov.com/"] = (200, HTML, b"<title>fakrov</title><a href='https://evil-mirror.com/'>x</a>")
        site = FakeSite(routes)
        with self.assertRaises(B.DiscoveryFailed):
            self.R(site).current()
        self.assertFalse(any("evil-mirror" in u for u in site.log))
        # citée, mais l'hôte ne se valide pas (jeton absent du nom, liste /home/fakrov) : refus, cache intact
        write_legacy_cache(self.cache, self.now[0] - 1000)
        routes["https://fakrov.com/"] = (200, HTML, b"<title>fakrov devient evil-mirror.com</title>"
                                                   b"<a href='https://evil-mirror.com/'>evil-mirror.com</a>")
        routes["https://evil-mirror.com/"] = (200, HTML, b"<a href='aab2ae3'>x</a>")
        routes["https://evil-mirror.com/aab2ae3/"] = (302, {"location": "/aab2ae3/home/fakrov"}, b"")
        routes["https://evil-mirror.com/aab2ae3/home/fakrov"] = (200, HTML, LISTING.replace("PFX", "aab2ae3").encode())
        r = self.R(FakeSite(routes))
        with open(self.cache) as f:
            before = f.read()
        self.assertEqual(r.current(check=True)[0], "https://fakrov.com")   # dernière époque valide conservée
        self.assertIn("token_host_mismatch", self.out.getvalue())
        with open(self.cache) as f:
            self.assertEqual(f.read(), before)

    def test_advert_forms_refused(self):
        bad = ["http://wadrox.com/", "https://wadrox.com:8443/", "https://wadrox.com:443/", "https://u:p@wadrox.com/",
               "https://wadrox.com/99c7f65/", "https://wadrox.com/?x=1", "https://wadrox.com/#a", "https://1.2.3.4/",
               "https://[::1]/", "https://localhost/", "https://wadrox.local/", "https://wadrox/", "//wadrox.com:81/",
               "https://wadrox.com./", "https://-bad-.com/", "https://192.168.2.28/"]
        for href in bad:
            routes = wadrox_routes()
            routes["https://fakrov.com/"] = (200, HTML, ("<title>fakrov devient wadrox.com %s 1.2.3.4 ::1 localhost "
                                                         "wadrox.local wadrox 192.168.2.28 -bad-.com</title>"
                                                         "<a href='%s'>go</a>" % (href, href)).encode())
            site = FakeSite(routes)
            with self.assertRaises(B.DiscoveryFailed, msg=href):
                self.R(site).current()
            self.assertEqual(site.log, ["https://fakrov.com/"], href)
            self.assertFalse(os.path.exists(self.cache))
        for h in ("wadrox.com", "a.b.co", "xn--bcher-kva.example.org"):
            self.assertTrue(B.valid_public_host(h), h)
        for h in ("site.example", "WADROX.com", "1.2.3.4", "::1", "localhost", "a", "a..b", "wadrox.123", "x.home.arpa", "", None):
            self.assertFalse(B.valid_public_host(h), h)

    def test_foreign_redirects_refused(self):
        routes = wadrox_routes()
        routes["https://fakrov.com/"] = (301, {"location": "https://wadrox.com/"}, b"")   # redirection, pas annonce
        site = FakeSite(routes)
        with self.assertRaises(B.DiscoveryFailed) as cm:
            self.R(site).current()
        self.assertEqual(str(cm.exception), "foreign_redirect")
        self.assertEqual(site.log, ["https://fakrov.com/"])
        routes = wadrox_routes()
        routes["https://wadrox.com/"] = (302, {"location": "https://evil.com/"}, b"")
        with self.assertRaises(B.DiscoveryFailed) as cm:
            self.R(FakeSite(routes)).current()
        self.assertEqual(str(cm.exception), "migration:foreign_redirect")
        routes = wadrox_routes()
        routes["https://wadrox.com/99c7f65/"] = (302, {"location": "http://wadrox.com:8080/99c7f65/home/wadrox"}, b"")
        with self.assertRaises(B.DiscoveryFailed):
            self.R(FakeSite(routes)).current()

    def test_token_and_path_mismatch(self):
        cases = {
            "token_host_mismatch": {"https://wadrox.com/99c7f65/": (302, {"location": "/99c7f65/home/fakrov"}, b""),
                                    "https://wadrox.com/99c7f65/home/fakrov": (200, HTML, wad_listing(token="fakrov"))},
            "token_mismatch": {"https://wadrox.com/99c7f65/home/wadrox": (200, HTML, wad_listing(
                extra='<a href="/99c7f65/b/fakrov/52277985">vieux</a>'))},
            "no_detail_links": {"https://wadrox.com/99c7f65/home/wadrox": (200, HTML, wad_listing(ids=(17397, 17885)))},
            "listing_shape": {"https://wadrox.com/99c7f65/": (302, {"location": "/99c7f65/liste"}, b""),
                              "https://wadrox.com/99c7f65/liste": (200, HTML, wad_listing())},
        }
        for why, patch in cases.items():
            routes = wadrox_routes()
            routes.update(patch)
            with self.assertRaises(B.DiscoveryFailed) as cm:
                self.R(FakeSite(routes)).current()
            self.assertEqual(str(cm.exception), "migration:" + why)

    def test_cache_migration_persists_reload_and_anchor_change(self):
        self.R(FakeSite(wadrox_routes())).current()
        r2 = self.R(FakeSite({}))
        self.assertEqual(r2.current(), ("https://wadrox.com", "99c7f65", "fresh"))
        self.assertEqual(r2.same_origin("https://wadrox.com/99c7f65/x"), "https://wadrox.com/99c7f65/x")
        self.assertIsNone(r2.same_origin("https://fakrov.com/99c7f65/x"))
        other = B.Resolver("https://other-anchor.com/", self.cache, fetch=FakeSite({}))
        self.assertEqual(other.status(), "none")   # cache d'une autre racine de confiance ignoré
        with open(self.cache, "w") as f:
            json.dump({"v": 2, "anchor": "fakrov.com", "host": "10.0.0.1", "prefix": "99c7f65", "token": "wadrox",
                       "validated_at": 5}, f)
        self.assertEqual(self.R(FakeSite({})).status(), "none")

    def test_same_origin_prefix_rotation_on_migrated_host(self):
        r = self.R(FakeSite(wadrox_routes()), ttl=100, grace=1000)
        r.current()
        r.fetch = FakeSite(dict(wadrox_routes(prefix="abc1234"),
                                **{"https://wadrox.com/99c7f65/": (404, HTML, b"")}))
        self.now[0] += 400
        self.assertEqual(r.current(check=True)[:2], ("https://wadrox.com", "abc1234"))
        self.assertEqual(r.cache["history"][0], {"host": "wadrox.com", "prefix": "99c7f65", "token": "wadrox",
                                                 "until": self.now[0]})
        self.assertFalse(any(u.startswith("https://fakrov.com") for u in r.fetch.log))   # hôte courant seulement

    def test_dead_migrated_host_falls_back_to_anchor(self):
        self.R(FakeSite(wadrox_routes())).current()
        routes = {"https://wadrox.com/": B.FetchError("URLError"),
                  "https://fakrov.com/": (200, HTML, b"<title>fakrov devient zadrox.net</title>"
                                                     b"<a href='https://zadrox.net/'>zadrox.net</a>"),
                  "https://zadrox.net/": (200, HTML, b"<a href='q1w2e3r'>x</a>"),
                  "https://zadrox.net/q1w2e3r/": (302, {"location": "/q1w2e3r/home/zadrox"}, b""),
                  "https://zadrox.net/q1w2e3r/home/zadrox": (200, HTML, wad_listing("q1w2e3r", "zadrox"))}
        r = self.R(FakeSite(routes))
        self.now[0] += 400
        self.assertEqual(r.current(check=True)[:2], ("https://zadrox.net", "q1w2e3r"))
        # l'ancre renvoie vers l'hôte déjà en panne : échec, dernière époque valide gardée
        self.R(FakeSite(wadrox_routes())).refresh()
        routes2 = {"https://wadrox.com/": B.FetchError("URLError"), "https://fakrov.com/": (200, HTML, FAKROV_MOVED)}
        r = self.R(FakeSite(routes2))
        self.now[0] += 400
        self.assertEqual(r.current(check=True)[0], "https://wadrox.com")
        self.assertIn("session_check_failed", self.out.getvalue())

    def test_migration_hops_bounded(self):
        routes = {}
        chain = ["fakrov.com", "aaone.com", "bbtwo.com", "ccthree.com"]
        for a, b in zip(chain, chain[1:]):
            routes["https://%s/" % a] = (200, HTML, ("<title>devient %s</title><a href='https://%s/'>x</a>" % (b, b)).encode())
        routes["https://ccthree.com/"] = (200, HTML, b"<a href='p1p2p3p'>x</a>")
        site = FakeSite(routes)
        with self.assertRaises(B.DiscoveryFailed) as cm:
            self.R(site).current()
        self.assertIn("migration_hops", str(cm.exception))
        self.assertNotIn("https://ccthree.com/", site.log)

    def test_session_check_interval(self):
        site = FakeSite(fakrov_routes())
        r = self.R(site)
        r.current()
        n = len(site.log)
        self.now[0] += 100
        r.current(check=True)
        self.assertEqual(len(site.log), n)          # vérifié il y a < 300 s : aucun réseau
        r.current()
        self.now[0] += 250
        r.current(check=True)
        self.assertGreater(len(site.log), n)        # ouverture utilisateur : revalidation (racine incluse)
        self.assertEqual(site.log[n], "https://fakrov.com/")


class RouteMapTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._q = quiet()
        self.out = self._q.__enter__()
        self.r = B.Resolver("https://fakrov.com/", os.path.join(self.d, "prefix.json"), fetch=FakeSite(wadrox_routes()))
        self.r.current()

    def tearDown(self):
        self._q.__exit__(None, None, None)
        shutil.rmtree(self.d)

    def M(self, **kw):
        return B.RouteMap(self.r, os.path.join(self.d, "routes.json"), **kw)

    def test_listing_then_bounded_api_pages(self):
        m, ep = self.M(), self.r.epoch()
        site = self.r.fetch = FakeSite(wadrox_routes())
        dl = time.monotonic() + 5
        self.assertEqual(m.resolve(ep, MARIO_SID, dl), ("found", "b/wadrox/" + MARIO_NEW))
        self.assertEqual(site.log, ["https://wadrox.com/99c7f65/home/wadrox"])   # liste suffit : pas d'API
        self.assertEqual(m.resolve(ep, "17833", dl), ("found", "b/wadrox/%d" % (17833 * 5049)))
        self.assertEqual(site.log[-1], api_url(0))
        self.assertEqual(m.resolve(ep, "17001", dl), ("found", "b/wadrox/%d" % (17001 * 5049)))
        self.assertEqual(site.log[-1], api_url(2))
        n = len(site.log)
        self.assertEqual(m.resolve(ep, "99999", dl), ("absent", None))   # hasMore=false : conclusif
        self.assertEqual(len(site.log), n)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, "routes.json")).st_mode), 0o600)
        self.assertEqual(self.M().peek(ep, MARIO_SID), ("found", "b/wadrox/" + MARIO_NEW))   # relu du disque

    def test_hostile_api_entries_ignored_and_conflicts_dropped(self):
        films = [{"id": "1", "link": "https://evil.com/99c7f65/b/wadrox/5049"},
                 {"id": "2", "link": "/99c7f65/b/fakrov/10098"},
                 {"id": "3", "link": "/99c7f65/b/wadrox/15147?x=1"},
                 {"id": "4", "link": "/other12/b/wadrox/20196"},
                 {"id": "../5", "link": "/99c7f65/b/wadrox/25245"},
                 {"id": True, "link": "/99c7f65/b/wadrox/1"},
                 {"id": "6", "link": "https://u:p@wadrox.com/99c7f65/b/wadrox/30294"},
                 {"id": "7", "link": "/99c7f65/b/wadrox/700"}, {"id": "7", "link": "/99c7f65/b/wadrox/701"},
                 {"id": "8", "link": "/99c7f65/b/wadrox/800"}, {"id": "9", "link": "/99c7f65/b/wadrox/800"},
                 {"id": "10", "link": "http://wadrox.com/99c7f65/b/wadrox/1000"}, "x", None]
        self.r.fetch = FakeSite(wadrox_routes(api=[(films, False)]))
        m, ep = self.M(), self.r.epoch()
        st = m.ensure(ep, {str(i) for i in range(1, 11)}, time.monotonic() + 5)
        self.assertEqual({k: v for k, v in st["map"].items() if int(k) < 100}, {"10": "b/wadrox/1000"})
        self.assertEqual(sorted(st["conflicts"], key=int), ["7", "8", "9"])
        self.assertEqual(m.peek(ep, "7"), ("absent", None))

    def test_page_cap_bad_json_transient_and_epoch_reset(self):
        pages = [([{"id": str(100 + i), "link": "/99c7f65/b/wadrox/%d" % (900 + i)}], True) for i in range(10)]
        site = self.r.fetch = FakeSite(wadrox_routes(api=pages))
        m, ep = self.M(max_pages=3), self.r.epoch()
        self.assertEqual(m.resolve(ep, "4242", time.monotonic() + 5), ("unknown", None))
        self.assertEqual(len([u for u in site.log if "api_films" in u]), 3)
        self.assertIn("route_page_cap", self.out.getvalue())
        # Toute erreur de transport/HTTP/format reste inconclusive et peut être retentée plus tard.
        routes = wadrox_routes()
        routes[api_url(0)] = B.FetchError("timeout")
        site = self.r.fetch = FakeSite(routes)
        m2 = B.RouteMap(self.r, os.path.join(self.d, "r2.json"))
        self.assertEqual(m2.resolve(ep, "4242", time.monotonic() + 5), ("unknown", None))
        site.routes[api_url(0)] = (200, JSONH, b"{not json")
        self.assertEqual(m2.resolve(ep, "4242", time.monotonic() + 5), ("unknown", None))
        site.routes[api_url(0)] = (200, HTML, api_body([], False))
        m3 = B.RouteMap(self.r, os.path.join(self.d, "r3.json"))
        self.assertEqual(m3.resolve(ep, "4242", time.monotonic() + 5), ("unknown", None))
        routes = wadrox_routes()
        routes[api_url(0)] = (429, HTML, b"rate limited")
        m4 = B.RouteMap(self.r, os.path.join(self.d, "r4.json"))
        self.r.fetch = FakeSite(routes)
        self.assertEqual(m4.resolve(ep, "4242", time.monotonic() + 5), ("unknown", None))
        # nouvelle époque : table remise à zéro (aucun chemin d'une ancienne époque réutilisé)
        ep2 = dict(ep, prefix="abc1234", key="wadrox.com/abc1234/wadrox")
        self.assertEqual(m.peek(ep2, "100"), ("unknown", None))

    def test_transient_listing_failure_retries_listing(self):
        ep = self.r.epoch()
        routes = wadrox_routes()
        listing = "https://wadrox.com/99c7f65/home/wadrox"
        routes[listing] = B.FetchError("timeout")
        site = self.r.fetch = FakeSite(routes)
        m = self.M()
        self.assertEqual(m.resolve(ep, MARIO_SID, time.monotonic() + 5), ("unknown", None))
        self.assertFalse(m.state["listing_done"])
        site.routes[listing] = (200, HTML, wad_listing())
        self.assertEqual(m.resolve(ep, MARIO_SID, time.monotonic() + 5),
                         ("found", "b/wadrox/" + MARIO_NEW))
        self.assertEqual(site.log.count(listing), 2)


# ----------------------------------------------------------------------------- catalogue hérité + lecteur après migration
def legacy_catalogue():
    return {"source": "fakrov.com", "generated_at": "2026-10-25T00:00:00Z", "items": [
        {"title": "Super Mario Galaxy, le film", "detail_path": "b/fakrov/" + MARIO_OLD, "source_id": MARIO_OLD,
         "site_id": MARIO_SID, "year": 2026},
        {"title": "Via API", "detail_path": "b/fakrov/%d" % (17001 * 3005), "site_id": "17001"},
        {"title": "Retiré du site", "detail_path": "b/fakrov/%d" % (99999 * 3005), "site_id": "99999"},
        {"title": "Sans site_id", "detail_path": "b/fakrov/53744425"},
    ]}


class MigratedPlayerTests(unittest.TestCase):
    def setUp(self):
        self._q = quiet()
        self.out = self._q.__enter__()
        self.site = FakeSite(wadrox_routes())

        def make(d):
            write_legacy_cache(os.path.join(d, "prefix.json"), time.time() - 1000)
            r = B.Resolver("https://fakrov.com/", os.path.join(d, "prefix.json"), fetch=self.site, session_check=300)
            return r, B.RouteMap(r, os.path.join(d, "routes.json"))
        self.h = Harness(catalogue=legacy_catalogue(), make=make, env={"COLS": "4"})

    def tearDown(self):
        self._q.__exit__(None, None, None)
        self.h.close()

    def test_old_catalogue_plays_through_site_id_map(self):
        self.assertEqual(self.h.resolver.epoch()["host"], "fakrov.com")
        s = self.h.ctl.open("catalogue")
        self.assertTrue(self.h.wait_state("catalogue"))
        self.assertTrue(s["prep"].wait(5))
        self.assertEqual(self.h.resolver.epoch()["key"], "wadrox.com/99c7f65/wadrox")   # migration à l'ouverture
        self.assertEqual(self.h.ctl.items[0]["site_id"], MARIO_SID)                    # métadonnées conservées
        self.assertEqual(self.h.ctl.items[0]["detail_path"], "b/fakrov/" + MARIO_OLD)
        d = self.h.displays[0]
        self.assertEqual(self.h.ctl.key(s["id"], "ok"), "player")
        self.assertEqual(d.calls[-1], ("navigate", "https://wadrox.com/99c7f65/b/wadrox/" + MARIO_NEW))
        self.assertEqual(self.h.ctl.key(s["id"], "back"), "catalogue")
        self.h.ctl.key(s["id"], "right")
        self.assertEqual(self.h.ctl.key(s["id"], "ok"), "player")                       # trouvé par l'API
        self.assertEqual(d.calls[-1], ("navigate", "https://wadrox.com/99c7f65/b/wadrox/%d" % (17001 * 5049)))
        self.h.ctl.key(s["id"], "back")
        for _ in range(2):   # retiré du site, puis sans site_id : Page indisponible, aucune URL calculée
            self.h.ctl.key(s["id"], "right")
            n = len(d.calls)
            self.assertEqual(self.h.ctl.key(s["id"], "ok"), "catalogue")
            self.assertEqual(self.h.ctl.view()["message"], "Page indisponible")
            self.assertFalse([c for c in d.calls[n:] if c[0] == "navigate"])
        self.assertFalse([u for u in self.site.log if "/b/" in u])   # jamais de sonde de fiche
        self.assertFalse([u for u in self.site.log if str(99999 * 5049) in u])
        self.assertIn('"error": "unmapped"', self.out.getvalue().replace("'", '"'))

    def test_unmigrated_catalogue_uses_direct_path_without_route_traffic(self):
        site = FakeSite(fakrov_routes())

        def make(d):
            r = B.Resolver("https://fakrov.com/", os.path.join(d, "prefix.json"), fetch=site)
            r.current()
            return r, B.RouteMap(r, os.path.join(d, "routes.json"))
        cat = {"source": "fakrov.com", "items": [{"title": "Mario", "detail_path": "b/fakrov/" + MARIO_OLD,
                                                  "site_id": MARIO_SID}]}
        h = Harness(catalogue=cat, make=make)
        try:
            n = len(site.log)
            s = h.ctl.open("catalogue")
            self.assertTrue(h.wait_state("catalogue") and s["prep"].wait(5))
            # Même si une table complète/cappée ne contient pas cet id, le chemin du catalogue est déjà
            # celui de l'époque courante : il reste directement valide.
            resolver = h.resolver
            assert resolver is not None
            h.ctl.router.state = h.ctl.router._fresh(resolver.epoch()["key"])
            h.ctl.router.state["exhausted"] = True
            self.assertEqual(h.ctl.key(s["id"], "ok"), "player")
            self.assertEqual(h.displays[0].calls[-1], ("navigate", "https://fakrov.com/aab2ae3/b/fakrov/" + MARIO_OLD))
            self.assertEqual(len(site.log), n)
            self.assertFalse(os.path.exists(os.path.join(h.d, "routes.json")))
        finally:
            h.close()

    def test_health_stays_responsive_while_resolving(self):
        gate = threading.Event()
        s = self.h.ctl.open("catalogue")
        self.assertTrue(self.h.wait_state("catalogue") and s["prep"].wait(5))
        self.h.ctl.key(s["id"], "right")
        orig = self.h.ctl.router.resolve

        def slow(*a):
            gate.wait(2)
            return orig(*a)
        self.h.ctl.router.resolve = slow
        res = []
        th = threading.Thread(target=lambda: res.append(self.h.ctl.key(s["id"], "ok")))
        th.start()
        time.sleep(0.1)
        t0 = time.time()
        self.assertEqual(self.h.ctl.health(s["id"])[0], "catalogue")
        self.assertEqual(self.h.ctl.key(s["id"], "ok"), "catalogue")   # OK répété ignoré pendant l'ouverture
        self.assertLess(time.time() - t0, 0.5)
        gate.set()
        th.join(5)
        self.assertEqual(res, ["player"])


class LegacyCatalogueTests(unittest.TestCase):
    def test_site_id_retained_deduped_and_listing_data_id(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "c.json")
            raw = legacy_catalogue()
            raw["items"].append({"title": "Doublon site_id", "detail_path": "b/fakrov/1", "site_id": MARIO_SID})
            raw["items"].append({"title": "site_id hostile", "detail_path": "b/fakrov/2", "site_id": "1;rm"})
            B.atomic_write_json(p, raw)
            items, gen, host = B.load_catalogue_meta(p, B.Config({}).detail_re)
            self.assertEqual(host, "fakrov.com")
            self.assertEqual([i["site_id"] for i in items], [MARIO_SID, "17001", "99999", None, None])
            self.assertEqual(B.load_catalogue(p, B.Config({}).detail_re)[1], gen)
        finally:
            shutil.rmtree(d)
        r = B.Resolver("https://fakrov.com/", "/nonexistent/x.json", fetch=FakeSite({}))
        items = B.parse_listing('<a class="film-card" href="/aab2ae3/b/fakrov/53744425"><img src="https://i/a.jpg" alt="Contre">'
                                '<button class="card-info-btn" data-id="17885"></button></a>',
                                "https://fakrov.com/aab2ae3/home/fakrov", r, B.Config({}).detail_re)
        self.assertEqual((items[0]["site_id"], items[0]["source_id"]), ("17885", "53744425"))

    def test_backfill_site_ids_from_raw_store(self):
        cat = {"source": "fakrov.com", "items": [
            {"title": "Mario", "detail_path": "b/fakrov/" + MARIO_OLD, "source_id": MARIO_OLD},
            {"title": "Déjà", "detail_path": "b/fakrov/9", "site_id": "3"},
            {"title": "Inconnu", "detail_path": "b/fakrov/77"},
            {"title": "Ambigu", "detail_path": "b/fakrov/88"}]}
        store = {"origin": "https://fakrov.com", "catalog": {"items": [
            {"link_id": MARIO_OLD, "site_id": MARIO_SID}, {"link_id": "88", "site_id": "1"},
            {"link_id": "88", "site_id": "2"}, {"link_id": "x", "site_id": "4"}]}}
        n = B.backfill_site_ids(cat, store, "fakrov.com")
        self.assertEqual(n, {"filled": 1, "already": 1, "missing": 1, "ambiguous": 1})
        self.assertEqual(cat["items"][0]["site_id"], MARIO_SID)
        self.assertNotIn("site_id", cat["items"][3])
        with self.assertRaises(ValueError):
            B.backfill_site_ids(cat, dict(store, origin="https://wadrox.com"), "fakrov.com")

    def test_backfill_command_keeps_backup(self):
        d = tempfile.mkdtemp()
        try:
            cfg = B.Config({"STATE_DIR": d})
            B.atomic_write_json(cfg.catalogue_file, {"source": "fakrov.com", "items": [
                {"title": "Mario", "detail_path": "b/fakrov/" + MARIO_OLD}]})
            sp = os.path.join(d, "current.json")
            B.atomic_write_json(sp, {"origin": "https://fakrov.com", "catalog": {"items": [
                {"link_id": MARIO_OLD, "site_id": MARIO_SID}]}})
            self.assertEqual(B.backfill_command(cfg, sp)["filled"], 1)
            self.assertEqual(B.load_catalogue(cfg.catalogue_file, cfg.detail_re)[0][0]["site_id"], MARIO_SID)
            with open(cfg.catalogue_file + ".bak") as f:
                self.assertNotIn("site_id", f.read())
        finally:
            shutil.rmtree(d)


# ----------------------------------------------------------------------------- moniteur d'adresse en arrière-plan
class RoutingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._q = quiet()
        self.out = self._q.__enter__()
        self.now = [100000.0]
        self.cache = os.path.join(self.d, "prefix.json")
        self.site = FakeSite(fakrov_routes())
        B.atomic_write_json(self.cache, {"v": 2, "anchor": "fakrov.com", "host": "fakrov.com", "prefix": "aab2ae3",
                                         "token": "fakrov", "listing_path": "/aab2ae3/home/fakrov",
                                         "validated_at": self.now[0] - 10, "checked_at": self.now[0] - 10,
                                         "discovered_at": self.now[0] - 10})
        self.r = B.Resolver("https://fakrov.com/", self.cache, fetch=self.site, clock=lambda: self.now[0])
        self.router = B.RouteMap(self.r, os.path.join(self.d, "routes.json"))
        self.items = [{"site_id": MARIO_SID, "detail_path": "b/fakrov/" + MARIO_OLD}]

        def wanted(ep):   # même règle que Controller.route_wanted, catalogue enregistré sous fakrov.com
            return {i["site_id"] for i in self.items if ep["host"] != "fakrov.com" or ep["token"] != "fakrov"}
        self.m = B.RoutingMonitor(self.r, self.router, wanted, interval=1800)

    def tearDown(self):
        self._q.__exit__(None, None, None)
        self.m.shutdown()
        shutil.rmtree(self.d)

    def snap(self):
        with open(self.cache, "rb") as f:
            return f.read(), os.stat(self.cache).st_mtime_ns

    def test_unchanged_domain_no_rewrite_then_detects_migration(self):
        before = self.snap()
        self.assertEqual(self.m.run_once(), "unchanged")
        self.assertEqual(self.snap(), before)                                   # aucune réécriture inutile
        self.assertFalse(os.path.exists(os.path.join(self.d, "routes.json")))  # aucun trafic de routage
        self.assertFalse([u for u in self.site.log if "api_films" in u])
        self.assertEqual(self.r.cache["validated_at"], self.now[0])            # fraîcheur en mémoire
        self.site.routes = wadrox_routes()
        self.now[0] += 1800
        self.assertEqual(self.m.run_once(), "changed")
        self.assertEqual(self.r.epoch()["key"], "wadrox.com/99c7f65/wadrox")
        with open(self.cache) as f:
            self.assertEqual(json.load(f)["host"], "wadrox.com")
        self.assertEqual(B.RouteMap(self.r, os.path.join(self.d, "routes.json")).peek(self.r.epoch(), MARIO_SID),
                         ("found", "b/wadrox/" + MARIO_NEW))
        ev = [json.loads(l) for l in self.out.getvalue().splitlines() if '"routing_check_ok"' in l]
        self.assertEqual([e["changed"] for e in ev], [False, True])
        self.assertEqual(ev[1]["routes"], {"wanted": 1, "mapped": 1, "exhausted": False})
        n = len(self.site.log)
        self.now[0] += 1800
        self.assertEqual(self.m.run_once(), "unchanged")   # table complète : pas de nouvelle page d'API
        self.assertFalse([u for u in self.site.log[n:] if "api_films" in u])

    def test_no_overlap_single_flight(self):
        gate, entered = threading.Event(), threading.Event()
        real = self.site

        def blocking(url, timeout):
            entered.set()
            gate.wait(3)
            return real(url, timeout)
        self.r.fetch = blocking
        res = []
        th = threading.Thread(target=lambda: res.append(self.m.run_once()))
        th.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.m.run_once(), "busy")                 # pas de second vol du moniteur
        self.assertIsNone(self.r.refresh(blocking=False))           # ni chevauchement avec une session
        gate.set()
        th.join(5)
        self.assertEqual(res, ["unchanged"])
        self.assertIn('"why": "overlap"', self.out.getvalue())
        # une ouverture de session en cours bloque le moniteur (et non l'inverse)
        self.r.lock.acquire()
        try:
            self.assertEqual(self.m.run_once(), "busy")
        finally:
            self.r.lock.release()

    def test_failure_keeps_last_good_and_backs_off(self):
        before = self.snap()
        self.site.routes = {"https://fakrov.com/": B.FetchError("URLError")}
        self.assertEqual(self.m.delay(), 1800)
        for want in (3600, 7200, 14400, 14400):
            self.assertEqual(self.m.run_once(), "failed")
            self.assertEqual(self.m.delay(), want)
        self.assertEqual(self.r.epoch()["key"], "fakrov.com/aab2ae3/fakrov")
        self.assertEqual(self.snap(), before)
        # migration annoncée mais invalide : fail-closed, aucune adoption partielle
        bad = wadrox_routes()
        bad["https://wadrox.com/99c7f65/home/wadrox"] = (200, HTML, wad_listing(ids=(1,)))
        self.site.routes = bad
        self.assertEqual(self.m.run_once(), "failed")
        self.assertEqual(self.r.epoch()["host"], "fakrov.com")
        self.assertEqual(self.snap(), before)
        self.assertFalse(os.path.exists(os.path.join(self.d, "routes.json")))
        self.site.routes = fakrov_routes()
        self.assertEqual(self.m.run_once(), "unchanged")
        self.assertEqual(self.m.delay(), 1800)

    def test_thread_runs_and_shuts_down(self):
        self.m.interval = 0.02
        self.m.start()
        end = time.time() + 3
        while self.m.runs < 2 and time.time() < end:
            time.sleep(0.01)
        self.assertGreaterEqual(self.m.runs, 2)
        self.m.shutdown(2)
        self.assertFalse(self.m.thread.is_alive())
        n = self.m.runs
        time.sleep(0.1)
        self.assertEqual(self.m.runs, n)

    def test_crash_in_check_does_not_kill_monitor(self):
        def boom(url, timeout):
            raise RuntimeError("bug")
        self.r.fetch = boom
        self.m.interval = 0.01
        self.m.max_backoff = 0
        self.m.start()
        time.sleep(0.15)
        self.assertTrue(self.m.thread.is_alive())
        self.assertIn("routing_monitor_error", self.out.getvalue())

    def test_config_gate(self):
        self.assertEqual(B.Config({}).routing_refresh_s, 0)
        self.assertEqual(B.Config({"ROUTING_REFRESH_S": "1800"}).routing_refresh_s, 1800)
        with self.assertRaises(SystemExit):
            B.Config({"ROUTING_REFRESH_S": "60"})


if __name__ == "__main__":
    unittest.main()
