"""« Regarder » côté service : porte de la source (fail-closed), résolveur NanoPi réutilisé (rotation, migration,
routage stable), automate typé, API TV, interception téléphone, aucune URL vers la TV/les agents ni dans le journal."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from unittest import mock

from tests._common import NANOPI, PI4
from tests.test_service import Servers, make_app
from opentv_pi import playback as pbmod, promotion, source as srcmod
from opentv_pi.playerclient import PlayerUnavailable

HTML = {"content-type": "text/html; charset=UTF-8"}
JSON = {"content-type": "application/json"}
ANCHOR = "https://ancra.net/"
SECRETS = ("ancra", "nouvo", "pfxaa1", "pfxbb2", "pfxcc3", "https://", "/b/")


def listing(prefix, token, pairs):
    cards = "".join('<a class="trend-card" href="/%s/b/%s/%s"><img src="https://img.test/p.jpg" alt="T%s">'
                    '<button data-id="%s">i</button></a>' % (prefix, token, link, sid, sid) for sid, link in pairs)
    return ("<html><body>%s</body></html>" % cards).encode()


def site_routes(host, prefix, token, pairs, root_extra=""):
    root = '<html><title>x</title><a href="/%s">go</a>%s</html>' % (prefix, root_extra)
    return {
        "https://%s/" % host: (200, HTML, root.encode()),
        "https://%s/%s/" % (host, prefix): (302, {"location": "https://%s/%s/home/%s" % (host, prefix, token)}, b""),
        "https://%s/%s/home/%s" % (host, prefix, token): (200, HTML, listing(prefix, token, pairs)),
    }


class Site:
    def __init__(self, routes):
        self.routes, self.log = dict(routes), []

    def __call__(self, url, timeout):
        self.log.append(url)
        if url in self.routes:
            return self.routes[url]
        if "api_films.php" in url:
            return 200, JSON, json.dumps({"films": [], "hasMore": False}).encode()
        return 404, HTML, b"nope"


def write_env(d, text, mode=0o640):
    p = os.path.join(d, "source.env")
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)
    return p


def write_cat(d, items):
    p = os.path.join(d, "catalogue.json")
    with open(p, "w") as f:
        json.dump({"items": items}, f)
    return p


CAT = [{"detail_path": "b/ancra/%d" % (500 + i), "site_id": str(101 + i), "title": "Film %d" % i, "year": 2024,
        "genre": "Drame"} for i in range(4)]
OK_ENV = "OPENTV_SOURCE_APPROVED=1\nOPENTV_SOURCE_TRUST_ANCHOR=%s\nOPENTV_PLAY_ASSIST_S=5\n" % ANCHOR


class TestSourceGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvsrc", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.engine = srcmod.load_engine(NANOPI)
        self.vh = self.engine.valid_public_host

    def load(self, text, mode=0o640):
        return srcmod.SourceConfig.load(write_env(self.d, text, mode), uid_ok=(0, os.geteuid()), valid_host=self.vh)

    def test_fail_closed_states(self):
        self.assertEqual(srcmod.SourceConfig.load(os.path.join(self.d, "absent")).status, "missing")
        self.assertEqual(self.load("OPENTV_SOURCE_TRUST_ANCHOR=%s\n" % ANCHOR).status, "unapproved")
        self.assertEqual(self.load("OPENTV_SOURCE_APPROVED=1\n").status, "no_anchor")
        for bad in ("http://ancra.net/", "https://ancra.net/pfxaa1/", "https://u:p@ancra.net/", "https://ancra.net:8443/",
                    "https://10.0.0.1/", "https://films.local/", "https://ancra.net/?q=1"):
            self.assertEqual(self.load("OPENTV_SOURCE_APPROVED=1\nOPENTV_SOURCE_TRUST_ANCHOR=%s\n" % bad).status,
                             "no_anchor", bad)
        self.assertEqual(self.load(OK_ENV, mode=0o644).status, "unsafe")   # lisible par tous : refusé
        self.assertEqual(self.load(OK_ENV, mode=0o660).status, "unsafe")   # groupe en écriture : refusé
        self.assertEqual(self.load(OK_ENV + "OPENTV_PLAYER_MAX_S=99999999\n").status, "invalid")
        self.assertEqual(self.load(OK_ENV).status, "ok")

    def test_symlink_refused(self):
        real = write_env(self.d, OK_ENV)
        link = os.path.join(self.d, "link.env")
        os.symlink(real, link)
        self.assertEqual(srcmod.SourceConfig.load(link, uid_ok=(0, os.geteuid())).status, "unsafe")

    def test_official_stream_mode_is_blocked_without_documented_endpoint(self):
        c = self.load(OK_ENV + "OPENTV_SOURCE_KIND=official-stream\n")
        self.assertEqual(c.status, "direct_mode_unconfigured")
        self.assertFalse(c.ok)

    def test_redaction_whitelist(self):
        r = srcmod.redact_fields({"host": "ancra.net", "prefix": "pfxaa1", "token": "ancra", "changed": True,
                                  "why": "status_404", "dst": "nouvo.org", "error": "a.b"})
        self.assertEqual(r, {"changed": True, "why": "status_404"})


class TestSourceResolver(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvres", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.engine = srcmod.load_engine(NANOPI)
        self.now = [1000.0]
        self.logs = []

    def make(self, site):
        cfg = srcmod.SourceConfig.load(write_env(self.d, OK_ENV), uid_ok=(0, os.geteuid()),
                                       valid_host=self.engine.valid_public_host)
        return srcmod.Source(cfg, NANOPI, os.path.join(self.d, "state"), write_cat(self.d, CAT),
                             log=lambda ev, **f: self.logs.append(dict(f, event=ev)), fetch=site,
                             clock=lambda: self.now[0], engine=self.engine)

    def test_epoch_prefix_rotation_and_migration_without_leaks(self):
        pairs = [(str(101 + i), str(500 + i)) for i in range(4)]
        site = Site(site_routes("ancra.net", "pfxaa1", "ancra", pairs))
        s = self.make(site)
        url, origin = s.detail_url({"site_id": "101"}, time.monotonic() + 5)
        self.assertEqual((url, origin), ("https://ancra.net/pfxaa1/b/ancra/500", "https://ancra.net"))
        # rotation de préfixe même origine : découverte à la prochaine vérification de session
        site.routes = site_routes("ancra.net", "pfxbb2", "ancra", pairs)
        self.now[0] += 400
        url, _ = s.detail_url({"site_id": "102"}, time.monotonic() + 5)
        self.assertEqual(url, "https://ancra.net/pfxbb2/b/ancra/501")
        # migration annoncée par la racine de confiance : nouvel hôte validé, routage par site_id stable
        moved = [(str(101 + i), str(9000 + i)) for i in range(4)]
        site.routes = {"https://ancra.net/": (200, HTML, b'<html>ancra devient nouvo.org <a href="https://nouvo.org/">'
                                                         b'ici</a></html>')}
        site.routes.update(site_routes("nouvo.org", "pfxcc3", "nouvo", moved))
        self.now[0] += 400
        url, origin = s.detail_url({"site_id": "103"}, time.monotonic() + 5)
        self.assertEqual((url, origin), ("https://nouvo.org/pfxcc3/b/nouvo/9002", "https://nouvo.org"))
        # élément absent du nouveau routage (API épuisée) : indisponible, jamais un chemin calculé
        site.routes.update(site_routes("nouvo.org", "pfxcc3", "nouvo", moved[:2]))
        s.router.state = None
        with self.assertRaises(srcmod.SourceError) as cm:
            s.detail_url({"site_id": "104"}, time.monotonic() + 5)
        self.assertEqual(cm.exception.code, "unmapped")
        text = json.dumps(self.logs)
        for x in SECRETS:
            self.assertNotIn(x, text)

    def test_foreign_redirect_and_unknown_item(self):
        r = site_routes("ancra.net", "pfxaa1", "ancra", [("101", "500")])
        r["https://ancra.net/pfxaa1/"] = (302, {"location": "https://evil-mirror.net/pfxaa1/home/ancra"}, b"")
        s = self.make(Site(r))
        with self.assertRaises(srcmod.SourceError) as cm:
            s.detail_url({"site_id": "101"}, time.monotonic() + 3)
        self.assertEqual(cm.exception.code, "discovery_failed")
        with self.assertRaises(srcmod.SourceError) as cm:
            s.detail_url({"site_id": "999"}, time.monotonic() + 3)
        self.assertEqual(cm.exception.code, "not_in_catalogue")


class FakeSource:
    available = True

    def __init__(self, fail=None, delay=0.0):
        self.fail, self.delay = fail, delay
        self.cfg = type("C", (), {"assist_s": 3.0, "settle_s": 0.1, "max_s": 600.0})()

    def detail_url(self, item, deadline):
        time.sleep(self.delay)
        if self.fail:
            raise srcmod.SourceError(self.fail)
        return "https://films-mirror.net/pfxaa1/b/tok/%s" % item["site_id"], "https://films-mirror.net"


class FakeClient:
    """Contrôleur simulé : open -> starting, puis revealed ; close -> ended (gone)."""

    def __init__(self, outcome="revealed", reveal_after=2, down=False):
        self.outcome, self.reveal_after, self.down = outcome, reveal_after, down
        self.reqs, self.polls, self.s = [], 0, None
        self.lock = threading.Lock()

    def ping(self):
        return not self.down

    def call(self, req, timeout=None):
        if self.down:
            raise PlayerUnavailable("down")
        with self.lock:
            self.reqs.append(dict(req))
            op = req["op"]
            if op == "open":
                self.s = {"sid": req["sid"], "state": "starting", "outcome": None, "reason": None, "gone": False}
                return {"ok": True}
            if op == "status":
                if self.s is None:
                    return {"ok": True, "status": None}
                self.polls += 1
                if self.s["state"] == "starting" and self.polls >= self.reveal_after:
                    if self.outcome == "revealed":
                        self.s["state"] = "revealed"
                    else:
                        self.s.update(state="ended", outcome=self.outcome, gone=True)
                return {"ok": True, "status": dict(self.s)}
            if op in ("close", "close_all"):
                if self.s is not None:
                    self.s.update(state="ended", reason=req.get("reason"), gone=True,
                                  outcome=self.s["outcome"] or ("revealed" if self.s["state"] == "revealed"
                                                                else "cancelled"))
                return {"ok": True, "gone": True, "status": self.s}
            return {"ok": True}

    def press(self, why):   # touche Retour/Quitter/Accueil vue par le contrôleur (evdev)
        with self.lock:
            self.s.update(state="ended", outcome="revealed", reason=why, gone=True)


def wait_state(pb, states, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pb.view()
        if v["state"] in states:
            return v
        time.sleep(0.01)
    return pb.view()


ITEM = {"site_id": "101", "detail_path": "b/tok/101", "title": "Film"}

# livraison installée simulée : copie des fichiers livrés sous leur chemin de destination (comme install.sh) +
# manifeste SHA-256 aux chemins ABSOLUS de ces copies
DELIVERY = [("opentv_pi/%s" % n, "opt/opentv/lib/opentv_pi/%s" % n)
            for n in sorted(os.listdir(os.path.join(PI4, "opentv_pi"))) if n.endswith(".py")] + \
    [("ui/%s" % n, "opt/opentv/share/ui/%s" % n) for n in ("tv.html", "tv.js", "keys.js", "tv.css")] + \
    [("systemd/%s" % n, "etc/systemd/system/%s" % n)
     for n in ("opentv.service", "opentv-kiosk.service", "opentv-player.service", "opentv-player-keys.service")] + \
    [("system/chromium-policy-opentv-player.json", "opt/opentv/share/chromium-player-policy/opentv-player.json"),
     ("bin/opentv-player", "opt/opentv/bin/opentv-player"), ("bin/opentv-player-keys", "opt/opentv/bin/opentv-player-keys"),
     ("../nanopi/opentv_bridge.py", "opt/opentv/lib/foryou/opentv_bridge.py"), ("README.md", "opt/opentv/README.md")]


def install_delivery(root):
    """-> chemin du manifeste (0644) d'une livraison simulée sous `root`."""
    import hashlib
    lines = []
    for src, dst in DELIVERY:
        full = os.path.join(root, dst)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        shutil.copyfile(os.path.normpath(os.path.join(PI4, src)), full)
        with open(full, "rb") as f:
            lines.append("%s  %s" % (hashlib.sha256(f.read()).hexdigest(), full))
    man = os.path.join(root, "opt", "opentv", "MANIFEST.sha256")
    with open(man, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(man, 0o644)
    return man


def trust_test_user(tc):
    """Les tests ne tournent pas en root : le compte du test est admis comme propriétaire, le temps du test."""
    patcher = mock.patch.object(promotion, "TRUSTED_UIDS", (0, os.geteuid()))
    patcher.start()
    tc.addCleanup(patcher.stop)


class TestPlaybackMachine(unittest.TestCase):
    def pb(self, src=None, client=None):
        logs = []
        p = pbmod.Playback(src or FakeSource(), client or FakeClient(), log=lambda ev, **f: logs.append(dict(f, event=ev)),
                           poll_s=0.01, gate=lambda: "promoted")
        p.refresh(force=True)
        p.test_logs = logs
        return p

    def test_play_back_close_then_ack_returns_home(self):
        c = FakeClient()
        p = self.pb(client=c)
        self.assertTrue(p.view()["available"])
        self.assertEqual(p.start("films/101", ITEM), (True, None))
        self.assertEqual(p.start("films/102", ITEM), (False, "busy"))
        self.assertEqual(wait_state(p, ("playing",))["state"], "playing")
        c.press("back")
        v = wait_state(p, ("closed",))
        self.assertEqual((v["state"], v["reason"], v["id"]), ("closed", "back", "films/101"))
        self.assertFalse(p.ack(v["seq"] - 1))
        self.assertTrue(p.ack(v["seq"]))
        self.assertEqual(p.view()["state"], "idle")
        for r in [p.view()] + p.test_logs:
            self.assertNotIn("https://", json.dumps(r))
        self.assertTrue(any(r["op"] == "open" and r["url"].startswith("https://") for r in c.reqs))

    def test_tv_close_while_loading_and_phone_close_while_playing(self):
        c = FakeClient(reveal_after=10 ** 6)
        p = self.pb(client=c)
        p.start("films/101", ITEM)
        time.sleep(0.05)
        self.assertEqual(p.close("tv"), "closing")
        v = wait_state(p, ("closed",))
        self.assertEqual((v["state"], v["reason"]), ("closed", "tv"))
        self.assertEqual([r["op"] for r in c.reqs if r["op"] == "close"][:1], ["close"])
        self.assertTrue(p.ack(v["seq"]))
        c2 = FakeClient()
        p2 = self.pb(client=c2)
        p2.start("films/101", ITEM)
        wait_state(p2, ("playing",))
        p2.close("phone")
        self.assertEqual(wait_state(p2, ("closed",))["reason"], "phone")

    def test_failures_are_unavailable_never_site(self):
        for src, cl, why in ((FakeSource(fail="discovery_failed"), FakeClient(), "source_discovery_failed"),
                             (FakeSource(), FakeClient(outcome="no-player"), "no-player"),
                             (FakeSource(), FakeClient(outcome="ambiguous"), "ambiguous"),
                             (FakeSource(), FakeClient(outcome="crashed"), "crashed")):
            p = self.pb(src, cl)
            p.start("films/101", ITEM)
            v = wait_state(p, ("unavailable",))
            self.assertEqual((v["state"], v["reason"], v["message"]), ("unavailable", why, "Lecture indisponible"))
            self.assertTrue(p.ack(v["seq"]))

    def test_controller_down_hides_watch_and_loading_timeout(self):
        p = self.pb(client=FakeClient(down=True))
        self.assertFalse(p.view()["available"])
        self.assertEqual(p.start("films/101", ITEM), (False, "unavailable"))
        c = FakeClient(reveal_after=10 ** 6)
        p = pbmod.Playback(FakeSource(), c, poll_s=0.01, load_margin_s=0.0, gate=lambda: "promoted")
        p.source.cfg.assist_s = 0.2
        p.refresh(force=True)
        p.start("films/101", ITEM)
        v = wait_state(p, ("unavailable",), timeout=5)
        self.assertEqual(v["reason"], "timeout")
        self.assertTrue(any(r["op"] == "close" for r in c.reqs))

    def test_ten_cycles(self):
        c = FakeClient()
        p = self.pb(client=c)
        for i in range(10):
            c.polls = 0
            self.assertEqual(p.start("films/101", ITEM), (True, None), i)
            wait_state(p, ("playing",))
            c.press(("back", "exit", "home")[i % 3])
            v = wait_state(p, ("closed",))
            self.assertTrue(p.ack(v["seq"]))
        self.assertEqual(p.view()["state"], "idle")


class TestStartupResetAndDemotion(unittest.TestCase):
    def test_no_open_before_startup_reset_finished(self):
        """Course corrigée : le close_all de démarrage ne peut jamais fermer une séance de ce service — « Regarder »
        reste indisponible (et start refusé) tant que reset() n'est pas terminé."""
        gate_ev, c = threading.Event(), FakeClient()
        real_call = c.call

        def slow_call(req, timeout=None):
            if req["op"] == "close_all":
                gate_ev.wait(5)   # contrôleur lent à fermer l'orpheline d'un service précédent
            return real_call(req, timeout)
        c.call = slow_call
        p = pbmod.Playback(FakeSource(), c, poll_s=0.01, gate=lambda: "promoted", needs_reset=True)
        th = threading.Thread(target=p.reset, daemon=True)
        th.start()
        p.refresh(force=True)
        self.assertFalse(p.view()["available"])
        self.assertEqual(p.start("films/101", ITEM), (False, "unavailable"))
        gate_ev.set()
        th.join(5)
        self.assertTrue(p.ready.is_set())
        p.refresh(force=True)
        self.assertTrue(p.view()["available"])
        self.assertEqual(p.start("films/101", ITEM), (True, None))
        wait_state(p, ("playing",))
        ops = [r["op"] for r in c.reqs]
        self.assertLess(ops.index("close_all"), ops.index("open"))
        self.assertEqual(ops.count("close_all"), 1)            # aucun close_all après l'ouverture
        self.assertEqual(p.view()["state"], "playing")

    def test_reset_without_source_or_controller_still_releases(self):
        for src, client in ((None, FakeClient()), (FakeSource(), FakeClient(down=True))):
            p = pbmod.Playback(src, client, gate=lambda: "promoted", needs_reset=True)
            self.assertFalse(p.ready.is_set())
            p.reset()
            self.assertTrue(p.ready.is_set())

    def test_demotion_while_playing_closes_session(self):
        st = ["promoted"]
        c = FakeClient()
        p = pbmod.Playback(FakeSource(), c, poll_s=0.01, gate=lambda: st[0])
        p.refresh(force=True)
        self.assertEqual(p.start("films/101", ITEM), (True, None))
        wait_state(p, ("playing",))
        st[0] = "missing"
        p.refresh(force=True)
        v = wait_state(p, ("closed",))
        self.assertEqual((v["state"], v["reason"]), ("closed", "demoted"))
        self.assertTrue(any(r["op"] == "close" and r["reason"] == "demoted" for r in c.reqs))
        self.assertFalse(p.view()["available"])


class TestAppIntegration(unittest.TestCase):
    def app(self):
        d = tempfile.mkdtemp(prefix="otvenv", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        env = write_env(d, OK_ENV)
        marker = os.path.join(d, "playback-promoted")
        trust_test_user(self)
        man = install_delivery(os.path.join(d, "root"))
        promotion.write(marker, manifest=man)   # promotion terrain simulée (livraison réelle du dépôt, compte du test)
        a = make_app(self, OPENTV_SOURCE_FILE=env, OPENTV_PLAYER_SOCKET=os.path.join(d, "none.sock"),
                     OPENTV_PLAYBACK_MARKER=marker, OPENTV_INSTALL_MANIFEST=man)
        self.assertTrue(a.playback.ready.wait(5))   # remise à zéro de démarrage terminée
        from opentv_pi import config
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.playback.source = FakeSource()
        a.playback.client = self.client = FakeClient()
        a.playback.refresh(force=True)
        return a

    def test_tv_api_typed_and_no_url(self):
        a = self.app()
        s = Servers(self, a)
        h = s.tv_headers()
        h["Content-Type"] = "application/json"
        st = json.loads(s.req("GET", "/api/tv/state", headers=s.tv_headers())[2])
        self.assertTrue(st["playback"]["available"])
        fy = json.loads(s.req("GET", "/api/tv/foryou", headers=s.tv_headers())[2])
        uid = fy["rows"][0]["items"][0]["id"]
        code, _, body = s.req("POST", "/api/tv/play", json.dumps({"id": "films/nope"}), headers=h)
        self.assertEqual((code, json.loads(body)["error"]), (409, "unknown_item"))
        code, _, body = s.req("POST", "/api/tv/play", json.dumps({"id": uid, "url": "https://evil.net/x"}), headers=h)
        self.assertEqual(code, 200)
        wait_state(a.playback, ("playing",))
        v = json.loads(s.req("GET", "/api/tv/play", headers=s.tv_headers())[2])
        self.assertEqual(v["state"], "playing")
        opened = [r for r in self.client.reqs if r["op"] == "open"][0]
        self.assertNotIn("evil", opened["url"])                     # jamais une URL fournie par l'appelant
        # téléphone pendant la lecture : Retour ferme côté service, rien n'est mis en file pour la page /tv cachée
        if a.remote is not None:
            seq0 = a.remote.seq
            self.assertEqual(a.remote.command({"id": "p1"}, "192.168.2.50", "up"), "ok")
            self.assertEqual(a.remote.command({"id": "p1"}, "192.168.2.50", "back"), "ok")
            self.assertEqual(a.remote.seq, seq0)
        v = wait_state(a.playback, ("closed",))
        self.assertEqual(v["reason"], "phone")
        code, _, body = s.req("POST", "/api/tv/play/ack", json.dumps({"seq": v["seq"]}), headers=h)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(a.playback.view()["state"], "idle")
        for path in ("/api/tv/state", "/api/tv/play"):
            text = s.req("GET", path, headers=s.tv_headers())[2]
            self.assertNotIn("films-mirror", text)
            self.assertNotIn("/b/tok/", text)
        self.assertNotIn("films-mirror", json.dumps(a.test_logs))
        self.assertNotIn("films-mirror", json.dumps(a.agent_status("hermes", ["read"])))

    def test_no_source_file_hides_watch(self):
        a = make_app(self, OPENTV_SOURCE_FILE="/nonexistent/source.env")
        self.assertFalse(a.tv_state()["playback"]["available"])
        self.assertEqual(a.tv_play("films/101"), (False, "unknown_item"))

    def test_source_env_alone_never_exposes_watch(self):
        """Source approuvée + contrôleur joignable, mais aucune promotion terrain : « Regarder » absent, aucun
        sondage du contrôleur, aucun open ; promotion puis démotion à chaud."""
        d = tempfile.mkdtemp(prefix="otvgate", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        marker = os.path.join(d, "playback-promoted")
        trust_test_user(self)
        man = install_delivery(os.path.join(d, "root"))
        a = make_app(self, OPENTV_SOURCE_FILE=write_env(d, OK_ENV), OPENTV_PLAYBACK_MARKER=marker,
                     OPENTV_PLAYER_SOCKET=os.path.join(d, "none.sock"), OPENTV_INSTALL_MANIFEST=man)
        self.assertTrue(a.playback.ready.wait(5))
        from opentv_pi import config
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.playback.source = FakeSource()
        a.playback.client = c = FakeClient()
        a.playback.refresh(force=True)
        self.assertFalse(a.tv_state()["playback"]["available"])
        self.assertEqual(a.playback.gate_status, "missing")
        fy = a.tv_foryou()
        uid = fy["rows"][0]["items"][0]["id"]
        self.assertEqual(a.tv_play(uid), (False, "unavailable"))
        self.assertEqual([r for r in c.reqs if r["op"] != "close_all"], [])   # jamais d'open ni de sondage
        promotion.write(marker, manifest=man)
        a.playback.refresh(force=True)
        self.assertTrue(a.tv_state()["playback"]["available"])
        os.unlink(marker)                                  # démotion : aucun open, même avant le prochain sondage
        self.assertEqual(a.tv_play(uid), (False, "unavailable"))
        self.assertEqual([r for r in c.reqs if r["op"] == "open"], [])
        self.assertFalse(a.tv_state()["playback"]["available"])
        self.assertTrue(any(x.get("event") == "playback_gate" and x.get("status") == "missing" for x in a.test_logs))


class TestPromotionMarker(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvpromo", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.m = os.path.join(self.d, "playback-promoted")
        self.root = os.path.join(self.d, "root")
        self.man = install_delivery(self.root)
        self.ok = (0, os.geteuid())

    def check(self, **kw):
        kw.setdefault("manifest", self.man)
        return promotion.check(self.m, uid_ok=self.ok, **kw)

    def write(self):
        return promotion.write(self.m, manifest=self.man, uid_ok=self.ok)

    def test_missing_valid_stale_and_invalid(self):
        self.assertEqual(self.check(), "missing")
        self.write()
        self.assertEqual(oct(os.stat(self.m).st_mode & 0o777), oct(0o644))
        self.assertEqual(self.check(), "promoted")
        self.assertEqual(self.check(version="9.9.9"), "stale")              # autre version
        for bad in (b"", b"{", b"[]", b'{"promoted": true}', b"[" * 100000 + b"]" * 100000,
                    json.dumps(dict(promotion.document(self.man, uid_ok=self.ok), field_validated=False)).encode(),
                    json.dumps(dict(promotion.document(self.man, uid_ok=self.ok), promoted=1)).encode()):
            with open(self.m, "wb") as f:
                f.write(bad[:4000])
            self.assertIn(self.check(), ("invalid", "unsafe"), bad[:20])
        self.assertTrue(promotion.remove(self.m))
        self.assertFalse(promotion.remove(self.m))

    def test_any_delivered_playback_file_change_makes_it_stale(self):
        """Empreinte de la LIVRAISON : tv.js, unités, politique du lecteur, résolveur, module… — une seule
        modification d'un fichier livré (ou du manifeste) referme la porte ; le contenu d'origine la rouvre."""
        self.write()
        self.assertEqual(self.check(), "promoted")
        for rel in ("opt/opentv/share/ui/tv.js", "etc/systemd/system/opentv-player.service",
                    "etc/systemd/system/opentv-kiosk.service", "opt/opentv/share/chromium-player-policy/opentv-player.json",
                    "opt/opentv/lib/foryou/opentv_bridge.py", "opt/opentv/lib/opentv_pi/web.py",
                    "opt/opentv/lib/opentv_pi/playerd.py", "opt/opentv/bin/opentv-player", "opt/opentv/README.md"):
            full = os.path.join(self.root, rel)
            with open(full, "rb") as f:
                orig = f.read()
            with open(full, "ab") as f:
                f.write(b"\n/* changement */\n")
            self.assertEqual(self.check(), "stale", rel)
            with open(full, "wb") as f:
                f.write(orig)
            self.assertEqual(self.check(), "promoted", rel)
        # nouvelle livraison (manifeste régénéré pour un tv.js modifié) : nouvelle empreinte => caduc aussi
        with open(os.path.join(self.root, "opt/opentv/share/ui/tv.js"), "ab") as f:
            f.write(b"\n// v2\n")
        with open(self.man) as f:
            install_lines = f.read()
        import hashlib
        tvjs = os.path.join(self.root, "opt/opentv/share/ui/tv.js")
        with open(tvjs, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        new = "\n".join(("%s  %s" % (h, tvjs)) if line.endswith("  " + tvjs) else line
                         for line in install_lines.splitlines()) + "\n"
        with open(self.man, "w") as f:
            f.write(new)
        self.assertEqual(self.check(), "stale")

    def test_manifest_must_be_complete_safe_and_present(self):
        self.write()
        with open(self.man) as f:
            lines = f.read().splitlines()
        with open(self.man, "w") as f:   # tv.js absent du manifeste : livraison incomplète
            f.write("\n".join(x for x in lines if not x.endswith("/ui/tv.js")) + "\n")
        self.assertEqual(self.check(), "stale")
        with open(self.man, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.assertEqual(self.check(), "promoted")
        os.chmod(self.man, 0o664)                                            # inscriptible par le groupe
        self.assertEqual(self.check(), "stale")
        os.chmod(self.man, 0o644)
        self.assertEqual(promotion.check(self.m, uid_ok=(os.geteuid(),), manifest=self.man), "promoted")
        self.assertEqual(self.check(manifest=os.path.join(self.d, "absent")), "stale")
        with open(self.man, "a") as f:
            f.write("pas une ligne de manifeste\n")
        self.assertEqual(self.check(), "stale")
        with self.assertRaises(ValueError):                                  # jamais de promotion invérifiable
            promotion.write(os.path.join(self.d, "other"), manifest=self.man, uid_ok=self.ok)
        self.assertFalse(os.path.exists(os.path.join(self.d, "other")))

    def test_root_only_by_default_everywhere(self):
        self.write()                                     # propriétaire = compte du test (non root)
        if os.geteuid() != 0:
            self.assertEqual(promotion.TRUSTED_UIDS, (0,))
            self.assertEqual(promotion.check(self.m, manifest=self.man), "unsafe")   # défaut : root seulement
            self.assertFalse(promotion.promoted(self.m, manifest=self.man))
        for name in ("app.py", "cli.py"):
            with open(os.path.join(PI4, "opentv_pi", name)) as f:
                text = f.read()
            for line in text.splitlines():
                if "promotion.check(" in line or "promotion.promoted(" in line:
                    self.assertNotIn("uid_ok", line, (name, line))      # jamais (0, geteuid) : root seulement
        with open(os.path.join(PI4, "opentv_pi", "promotion.py")) as f:
            self.assertNotIn("geteuid", f.read())

    def test_unsafe_files_refused(self):
        self.write()
        self.assertEqual(promotion.check(self.m, uid_ok=(os.geteuid() + 7,), manifest=self.man), "unsafe")
        for mode in (0o666, 0o664, 0o646):
            os.chmod(self.m, mode)
            self.assertEqual(self.check(), "unsafe", oct(mode))
        os.chmod(self.m, 0o644)
        link = os.path.join(self.d, "link")
        os.symlink(self.m, link)
        self.assertEqual(promotion.check(link, uid_ok=self.ok, manifest=self.man), "unsafe")
        hard = os.path.join(self.d, "hard")
        os.link(self.m, hard)
        self.assertEqual(self.check(), "unsafe")
        os.unlink(hard)
        fifo = os.path.join(self.d, "fifo")
        os.mkfifo(fifo)
        self.assertEqual(promotion.check(fifo, uid_ok=self.ok, manifest=self.man), "unsafe")   # jamais bloqué
        with open(self.m, "wb") as f:
            f.write(b" " * (promotion.CAP + 10))
        self.assertEqual(self.check(), "unsafe")

    def test_version_bumped_for_this_delivery(self):
        from opentv_pi import VERSION
        self.assertNotEqual(VERSION, "0.1.0")

    def test_cli_demote_closes_running_session(self):
        from opentv_pi import cli
        env = os.path.join(self.d, "opentv.env")
        with open(env, "w") as f:
            f.write("OPENTV_PLAYBACK_MARKER=%s\nOPENTV_INSTALL_MANIFEST=%s\nOPENTV_PLAYER_SOCKET=%s\n"
                    % (self.m, self.man, os.path.join(self.d, "p.sock")))
        self.write()
        calls = []

        class PC:
            def __init__(self, path, timeout=3.0):
                calls.append(("init", path))

            def call(self, req, timeout=None):
                calls.append(("call", req))
                return {"ok": True, "gone": True}
        import io
        import contextlib
        buf = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch("opentv_pi.playerclient.PlayerClient", PC), \
                contextlib.redirect_stdout(buf):
            code = cli.main(["playback", "demote", "--env", env])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        out = json.loads(text[text.rindex("{\n"):])          # sortie finale (indentée) après les lignes de journal
        self.assertEqual((out["removed"], out["session_closed"]), (True, True))
        self.assertFalse(os.path.exists(self.m))
        self.assertIn(("call", {"op": "close_all", "reason": "demoted"}), calls)

    def test_cli_status_and_promote_require_root_and_explicit_flag(self):
        import subprocess
        import sys
        from tests._common import PI4
        env = os.path.join(self.d, "opentv.env")
        with open(env, "w") as f:
            f.write("OPENTV_PLAYBACK_MARKER=%s\nOPENTV_SOURCE_FILE=%s\n" % (self.m, os.path.join(self.d, "nope")))
        run = lambda *a: subprocess.run([sys.executable, "-I", os.path.join(PI4, "bin", "opentv-pi"), "playback"] +  # noqa: E731
                                        list(a) + ["--env", env], capture_output=True, timeout=30)
        p = run("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.decode())
        self.assertEqual((out["promotion"], out["source"], out["available"]), ("missing", "missing", False))
        if os.geteuid() != 0:
            for act in ("promote", "demote"):
                p = run(act, "--field-validated")
                self.assertEqual(p.returncode, 2)
                self.assertEqual(json.loads(p.stdout.decode())["error"], "root_required")
            self.assertFalse(os.path.exists(self.m))
        with open(os.path.join(PI4, "opentv_pi", "cli.py")) as f:
            self.assertIn("field_validation_required", f.read())


class TestRealTvPage(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("OPENTV_TEST_PLAYWRIGHT"), "OPENTV_TEST_PLAYWRIGHT=<module playwright> requis")
    def test_watch_loading_back_same_card_and_unavailable(self):
        """Page /tv réelle 1920×1080 : Regarder en premier, écran OpenTV, Retour (CEC/HID simulé côté contrôleur)
        -> même carte, fiche rouverte sur Regarder, acquittement ; puis « Lecture indisponible » ; jamais d'iframe."""
        import shutil as _sh
        import subprocess
        from tests.test_service import find_chromium
        node = _sh.which("node")
        if not (node and find_chromium()):
            self.skipTest("node/Chromium absents")
        a = TestAppIntegration.app(self)
        c = self.client
        s = Servers(self, a)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvplay", dir="/tmp")
        os.makedirs(shots, exist_ok=True)
        marks = {}

        def driver():   # contrôleur simulé : Retour 1 s après la révélation, puis séance sans lecteur
            wait_state(a.playback, ("playing",), timeout=30)
            time.sleep(1.0)
            marks["back_at"] = time.monotonic()
            c.press("back")
            wait_state(a.playback, ("idle",), timeout=10)
            marks["idle_at"] = time.monotonic()
            c.outcome, c.polls = "no-player", 0
            wait_state(a.playback, ("loading",), timeout=30)
            wait_state(a.playback, ("idle",), timeout=30)
            c.outcome, c.reveal_after, c.polls = "revealed", 10 ** 6, 0
        th = threading.Thread(target=driver, daemon=True)
        th.start()
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "shots": shots}
        p = subprocess.run([node, os.path.join(os.path.dirname(__file__), "e2e_play.js"),
                            os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        st = {x["s"]: x for x in res["steps"]}
        card = st["card"]["focus"]
        title = st["item"]["ptitle"]
        regarder = {"t": "Regarder", "focused": True}
        self.assertEqual(st["item"]["btns"][0], regarder)
        ld = st["loading"]
        self.assertTrue(ld["overlay"])
        self.assertEqual((ld["ovRect"], ld["ovTitle"], ld["ovMsg"]), ([0, 0, 1920, 1080], title, "Chargement…"))
        # lecteur révélé : /tv (sous la fenêtre du lecteur) montre déjà la destination du retour, jamais « Chargement… »
        pl = st["playing"]
        self.assertEqual(res["playing_seen"], "playing")
        self.assertEqual((pl["overlay"], pl["focus"], pl["panel"], pl["ptitle"]), (False, card, True, title))
        self.assertEqual(pl["btns"][0], regarder)
        # Retour tenu pendant la fermeture (appuis répétés + auto-répétition) : fiche et carte intactes
        for k in ("held", "returned"):
            r = st[k]
            self.assertEqual((r["overlay"], r["focus"], r["panel"], r["ptitle"]), (False, card, True, title), k)
            self.assertEqual(r["btns"][0], regarder, k)
        self.assertEqual(res["after_return"], "idle")                       # repeint puis acquitté
        self.assertLess(marks["idle_at"] - marks["back_at"], 2.0)           # Retour -> accueil acquitté < 2 s
        # touche relâchée puis nouvel appui : Retour agit de nouveau normalement (fiche fermée sur la même carte)
        self.assertEqual((st["released"]["panel"], st["released"]["focus"]), (False, card))
        self.assertEqual((st["reopened"]["panel"], st["reopened"]["btns"][0]), (True, regarder))
        u = st["unavailable"]
        self.assertEqual((u["toast"], u["focus"], u["panel"]), ("Lecture indisponible", card, True))
        self.assertEqual(res["after_unavailable"], "idle")
        cx = st["cancelled"]
        self.assertEqual((res["after_cancel"], cx["overlay"], cx["panel"], cx["focus"]), ("idle", False, True, card))
        self.assertTrue(all(x["url"] == "/tv" and x["iframes"] == 0 for x in res["steps"]))   # aucun cadre du site
        self.assertNotIn("films-mirror", res["state_text"])
        th.join(5)


if __name__ == "__main__":
    unittest.main()
