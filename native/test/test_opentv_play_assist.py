"""Assistance de lecture du pont NanoPi (ChromiumKiosk.play_assist + Controller).

Depuis la racine : python3 -m unittest -v native.test.test_opentv_play_assist
Vrai Chromium headless (sauté s'il est introuvable ; OPENTV_TEST_CHROMIUM=<binaire> pour forcer).
Deux serveurs locaux sur des ports distincts = deux origines : la page parente (fiche publique) et le
lecteur intégré en iframe d'une AUTRE origine, comme sur la fiche réelle observée.
"""
import http.server
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

from native.test.test_opentv_bridge import B, Harness, StubResolver, find_chromium, shlex_quote

# ----------------------------------------------------------------------------- pages de test
PLAYER = b"""<!doctype html><title>player</title>
<style>html,body{margin:0;height:100%;background:#123}</style>
<body><div id=p>embedded player</div><script>
document.addEventListener('click', function (e) {
  fetch('/clicked?x=' + e.clientX + '&y=' + e.clientY + '&trusted=' + e.isTrusted);
});
</script></body>"""

PARENT_HEAD = """<!doctype html><title>fiche</title>
<style>body{margin:0;font-family:sans-serif} #wrap{width:1000px;margin:80px auto;transform:none}
 .side{position:absolute;right:10px;top:10px}</style><body>
<h1>Titre du film</h1><p>Synopsis public.</p>
"""

def parent_page(kind, player_origin):
    frame = '<iframe id="main" src="%s/embed" width="960" height="540" allowfullscreen></iframe>' % player_origin
    ad = '<iframe class="side" src="%s/embed" width="300" height="250"></iframe>' % player_origin
    tamper = ("<script>document.elementFromPoint=function(){return null};"
              "Element.prototype.getBoundingClientRect=function(){return {left:0,top:0,right:0,bottom:0,width:0,height:0}};"
              "</script>")
    body = {
        "single": '<div id="wrap">%s</div>%s%s' % (frame, ad, tamper),
        "late": '<div id="wrap"></div><script>setTimeout(function(){document.getElementById("wrap").innerHTML=%s},1200)</script>'
                % json.dumps(frame),
        "two": '<div id="wrap">%s%s</div>' % (frame, frame.replace('id="main"', 'id="b"').replace("960", "950")),
        "overlay": '<div id="wrap">%s</div><script>setTimeout(function(){var d=document.createElement("div");'
                   'd.id="ov";d.style.cssText="position:fixed;left:0;top:0;right:0;bottom:0;z-index:2147483647;background:rgba(0,0,0,.3)";'
                   'document.body.appendChild(d)},50)</script>' % frame,
        "none": '<div id="wrap"><p>Aucun lecteur ici.</p>%s</div>' % ad,
        "video": '<div id="wrap"><video id="main" width="960" height="540" style="background:#000"></video></div>'
                 '<script>window.vclicks=0;document.getElementById("main").addEventListener("click",function(){vclicks++})</script>',
    }[kind]
    return (PARENT_HEAD + body + "</body>").encode()


class Site:
    def __init__(self, handler):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.origin = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def player_site(log):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            log.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/embed" else "text/plain")
            self.end_headers()
            self.wfile.write(PLAYER if self.path == "/embed" else b"ok")
    return Site(H)


def parent_site(get_player_origin):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            kind = self.path.strip("/")
            if kind not in ("", "single", "late", "two", "overlay", "none", "video"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(parent_page(kind, get_player_origin()) if kind else b"<p>vide</p>")
    return Site(H)


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class RealChromiumAssistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clicks = []
        cls.player = player_site(cls.clicks)
        cls.parent = parent_site(lambda: cls.player.origin)
        cls.rt = tempfile.mkdtemp()
        cfg = B.Config({"COMPOSITOR_CMD": "", "BROWSER_CMD": shlex_quote(find_chromium()), "XDG_RUNTIME_DIR": cls.rt,
                        "BROWSER_FLAGS": "--headless=new --no-first-run --no-default-browser-check --disable-gpu "
                                         "--use-mock-keychain --password-store=basic --window-size=1920,1080 "
                                         "--autoplay-policy=no-user-gesture-required"})
        cls.k = B.ChromiumKiosk(cfg)
        with redirect_stdout(io.StringIO()):
            cls.k.open("about:blank")

    @classmethod
    def tearDownClass(cls):
        cls.k.close()
        cls.player.close()
        cls.parent.close()
        shutil.rmtree(cls.rt, ignore_errors=True)

    def go(self, kind):
        self.k.navigate("%s/%s" % (self.parent.origin, kind))
        del self.clicks[:]

    def clicked(self, wait=1.0):
        time.sleep(wait)
        return [p for p in self.clicks if p.startswith("/clicked")]

    def ev(self, expr):
        return self.k.evaluate(expr)

    def assist(self, origin=None, cancel=None, budget=6.0, settle=0.6):
        return self.k.play_assist(origin or self.parent.origin, cancel or threading.Event(), budget, settle)

    def test_single_cross_origin_iframe_maximized_and_clicked_once(self):
        self.go("single")
        self.assertEqual(self.assist(), "clicked")
        got = self.clicked()
        self.assertEqual(len(got), 1, got)
        self.assertIn("trusted=true", got[0])
        w, h = self.ev("[innerWidth, innerHeight]")
        # clic au centre du lecteur agrandi (coordonnées de l'iframe = celles de la fenêtre)
        self.assertIn("x=%d&y=%d&" % (w // 2, h // 2), got[0])
        # agrandi sur la page parente ; rien d'autre retiré, la petite iframe reste en place
        r = self.ev("(function(){var e=document.getElementById('main');var cs=getComputedStyle(e);"
                    "return [cs.position,cs.zIndex,cs.width,cs.height,getComputedStyle(document.documentElement).overflow,"
                    "document.querySelectorAll('iframe').length,document.querySelector('h1').textContent]})()")
        self.assertEqual(r, ["fixed", "2147483647", "%dpx" % w, "%dpx" % h, "hidden", 2, "Titre du film"])
        # aucune nouvelle tentative : toujours un seul clic après attente
        self.assertEqual(len(self.clicked(1.0)), 1)

    def test_late_inserted_player_is_found_within_budget(self):
        self.go("late")
        self.assertEqual(self.assist(), "clicked")
        self.assertEqual(len(self.clicked()), 1)

    def test_top_level_html5_video(self):
        self.go("video")
        self.assertEqual(self.assist(), "clicked")
        time.sleep(0.3)
        self.assertEqual(self.ev("window.vclicks"), 1)
        self.assertEqual(self.ev("getComputedStyle(document.getElementById('main')).position"), "fixed")

    def test_two_similar_players_is_ambiguous_no_action(self):
        self.go("two")
        self.assertEqual(self.assist(budget=1.5), "ambiguous")
        self.assertEqual(self.clicked(0.5), [])
        self.assertEqual(self.ev("getComputedStyle(document.getElementById('main')).position"), "static")

    def test_overlay_over_center_never_clicked(self):
        self.go("overlay")
        self.assertEqual(self.assist(), "maximized-obstructed")
        self.assertEqual(self.clicked(), [])
        self.assertIsNotNone(self.ev("document.getElementById('ov') && 1"))   # surcouche de la page intacte

    def test_no_player_and_small_iframes_ignored(self):
        self.go("none")
        t0 = time.monotonic()
        self.assertEqual(self.assist(budget=1.5), "no-player")
        self.assertLess(time.monotonic() - t0, 3.0)                         # borné
        self.assertEqual(self.clicked(0.3), [])

    def test_foreign_origin_no_action(self):
        self.go("single")
        self.assertEqual(self.assist(origin="https://wadrox.com", budget=1.2), "foreign")
        self.assertEqual(self.clicked(0.3), [])
        self.assertEqual(self.ev("getComputedStyle(document.getElementById('main')).position"), "static")

    def test_cancel_before_click(self):
        self.go("single")
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        self.assertEqual(self.assist(cancel=cancel, settle=1.0), "cancelled")
        self.assertEqual(self.clicked(), [])

    def test_navigation_back_clears_world(self):
        self.go("single")
        self.assertEqual(self.assist(), "clicked")
        self.go("none")
        self.assertEqual(self.assist(budget=1.0), "no-player")   # nouveau document, nouveau monde isolé


class StaticScriptTests(unittest.TestCase):
    def test_scripts_never_touch_media_urls_or_frame_contents(self):
        for js in (B.ASSIST_PROBE_JS, B.ASSIST_MAXIMIZE_JS, B.ASSIST_CHECK_JS):
            for bad in ("src", "contentWindow", "contentDocument", "fetch", "XMLHttpRequest", "innerHTML", "remove",
                        "muted", "currentTime", "playbackRate", ".click(", "dispatchEvent", "postMessage", "%s", "{0}"):
                self.assertNotIn(bad, js, bad)


# ----------------------------------------------------------------------------- contrôleur (affichage nul)
class ControllerAssistTests(unittest.TestCase):
    def setUp(self):
        self._out = redirect_stdout(io.StringIO())
        self.out = self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)

    def harness(self, env=None):
        h = Harness(env=env)
        self.addCleanup(h.close)
        sid = h.ctl.open("catalogue")["id"]
        self.assertTrue(h.wait_state("catalogue"))
        return h, sid

    def wait(self, pred, timeout=2):
        end = time.time() + timeout
        while time.time() < end and not pred():
            time.sleep(0.02)
        return pred()

    def test_ok_starts_one_bounded_assist_with_validated_origin(self):
        h, sid = self.harness()
        d = h.displays[0]
        d.assist_result = "clicked"
        self.assertEqual(h.ctl.key(sid, "ok"), "player")
        self.assertTrue(self.wait(lambda: '"outcome": "clicked"' in self.out.getvalue()))
        self.assertEqual([(a["origin"], a["budget_s"]) for a in d.assists], [("https://fakrov.com", 15.0)])
        self.assertEqual(h.ctl.s["assist"], "clicked")
        # touches en lecture : relayées telles quelles, aucune nouvelle assistance
        h.ctl.key(sid, "ok")
        h.ctl.key(sid, "right")
        self.assertEqual(len(d.assists), 1)
        self.assertEqual([c for c in d.calls if c[0] == "key"], [("key", "ok"), ("key", "right")])
        self.assertEqual(h.ctl.key(sid, "back"), "catalogue")
        self.assertEqual(d.calls[-1], ("navigate", "http://127.0.0.1:0/ui/"))
        self.assertEqual(h.ctl.key(sid, "ok"), "player")      # nouvelle fiche = nouvelle assistance unique
        self.assertTrue(self.wait(lambda: self.out.getvalue().count('"event": "play_assist"') == 2))
        self.assertEqual(len(d.assists), 2)

    def test_back_cancels_pending_assist(self):
        h, sid = self.harness()
        d = h.displays[0]
        d.assist_hold = threading.Event()
        h.ctl.key(sid, "ok")
        self.assertTrue(self.wait(lambda: len(d.assists) == 1))
        cancel = d.assists[0]["cancelled"]
        self.assertEqual(h.ctl.key(sid, "back"), "catalogue")
        self.assertTrue(cancel.is_set())
        d.assist_hold.set()
        self.assertTrue(self.wait(lambda: '"outcome": "cancelled"' in self.out.getvalue()))
        self.assertEqual(h.ctl.health()[0], "catalogue")

    def test_session_end_cancels_assist(self):
        h, sid = self.harness()
        d = h.displays[0]
        d.assist_hold = threading.Event()
        h.ctl.key(sid, "ok")
        self.assertTrue(self.wait(lambda: len(d.assists) == 1))
        h.ctl.close(sid=sid, reason="user")
        self.assertTrue(d.assists[0]["cancelled"].is_set())
        d.assist_hold.set()
        self.assertTrue(self.wait(lambda: '"outcome": "cancelled"' in self.out.getvalue()))

    def test_assist_failure_keeps_public_page(self):
        h, sid = self.harness()
        d = h.displays[0]

        def boom(*a, **k):
            raise RuntimeError("x")
        d.play_assist = boom
        self.assertEqual(h.ctl.key(sid, "ok"), "player")
        self.assertTrue(self.wait(lambda: "play_assist_error" in self.out.getvalue()))
        self.assertEqual(h.ctl.health()[0], "player")
        self.assertEqual(h.ctl.key(sid, "back"), "catalogue")

    def test_unknown_outcome_normalised(self):
        h, sid = self.harness()
        h.displays[0].assist_result = "<script>"
        h.ctl.key(sid, "ok")
        self.assertTrue(self.wait(lambda: '"outcome": "error"' in self.out.getvalue()))

    def test_disabled_by_config(self):
        h, sid = self.harness(env={"PLAY_ASSIST": "0"})
        self.assertEqual(h.ctl.key(sid, "ok"), "player")
        time.sleep(0.1)
        self.assertEqual(h.displays[0].assists, [])

    def test_config_bounds(self):
        for bad in ({"PLAY_ASSIST_S": "61"}, {"PLAY_ASSIST_S": "-1"}, {"PLAY_ASSIST_SETTLE_S": "11"}):
            with self.assertRaises(SystemExit):
                B.Config(bad)
        c = B.Config({})
        self.assertEqual((c.play_assist, c.play_assist_s, c.play_assist_settle_s), (True, 15.0, 1.5))


if __name__ == "__main__":
    unittest.main()
