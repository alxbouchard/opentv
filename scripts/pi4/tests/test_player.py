"""« Regarder » : contrôleur du lecteur (playerd), touches de fermeture (evkeys), socket authentifié, flags interdits.

Le navigateur est simulé par un faux CDP piloté par scénario et un vrai processus « sleep » (groupe tué par SIGKILL) :
on prouve la révélation seulement d'un lecteur dominant, au plus UN clic, aucun clic après Retour, la fermeture des
fenêtres surgissantes, la fin de séance si le cadre principal navigue, le nettoyage complet et l'absence d'URL dans le
journal comme dans l'état renvoyé.
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from tests import _common  # noqa: F401  (chemins)
from opentv_pi import assist, evkeys, playerd
from opentv_pi.cdp import CDPError

URL = "https://films-mirror.net/abc12/b/tok/42"
ORIGIN = "https://films-mirror.net"


class FakeCDP:
    """Faux navigateur : auto-attachement en pause (comme Chromium avec waitForDebuggerOnStart), cibles dynamiques,
    journal ordonné (méthode, paramètres, session)."""

    def __init__(self, scenario, on_event, check_states=None, popup_on_create=False, no_autoattach=False,
                 stuck=(), fast_states=None, dirty=False, hang_mouse=False):
        self.scenario, self.on_event = scenario, on_event
        self.check_states = list(check_states or [])
        self.fast_states = None if fast_states is None else list(fast_states)
        self.dirty, self.hang_mouse = dirty, hang_mouse
        self.n_full = self.n_fast = 0
        self.closed_ev = threading.Event()
        self.calls, self.clicks, self.closed = [], 0, False
        self.closed_targets = []
        self.log = []
        self.autoattach = False
        self.popup_on_create, self.no_autoattach, self.stuck = popup_on_create, no_autoattach, set(stuck)
        self.extra = []   # cibles ajoutées par le test (surgissantes, DevTools…)
        self.waiting, self.queued = set(), []   # sessions en pause (waitForDebuggerOnStart), commandes en attente
        self.lock = threading.Lock()

    def targets(self):
        base = [{"targetId": "L0", "type": "page", "url": "data:text/html,..."},
                {"targetId": "X9", "type": "background_page", "url": "chrome-extension://x"}]
        with self.lock:
            return base + [dict(t) for t in self.extra]

    def add(self, info, attach=True, waiting=True):
        with self.lock:
            self.extra.append(dict(info))
        self.on_event({"method": "Target.targetCreated", "params": {"targetInfo": dict(info)}})
        if attach and self.autoattach:
            if waiting:
                with self.lock:
                    self.waiting.add("S-" + info["targetId"])
            self.on_event({"method": "Target.attachedToTarget",
                           "params": {"sessionId": "S-" + info["targetId"], "targetInfo": dict(info),
                                      "waitingForDebugger": waiting}})

    def index(self, method, **match):
        for i, (m, prm, sess) in enumerate(self.log):
            if m == method and all((sess if k == "session" else prm.get(k)) == v for k, v in match.items()):
                return i
        return None

    def post(self, method, params=None, session=None):
        """Comme Chromium : une commande envoyée à une session en pause ne répond qu'après sa relance."""
        if self.closed:
            raise CDPError("closed")
        done = threading.Event()
        box = {}
        if session in self.paused_sessions():
            with self.lock:
                self.calls.append(method)
                self.log.append((method, dict(params or {}), session))
                self.queued.append((method, params, session, box, done))
            return (box, done, method)
        try:
            box["r"] = self.send(method, params, session)
        except CDPError as e:
            box["e"] = e
        done.set()
        return (box, done, method)

    def wait(self, token, timeout=5.0):
        box, done, method = token
        if not done.wait(min(timeout, 2.0)):
            raise CDPError("timeout:" + method)
        if "e" in box:
            raise box["e"]
        return box["r"]

    def paused_sessions(self):
        with self.lock:
            return set(self.waiting)

    def send(self, method, params=None, session=None, timeout=5.0):
        if self.closed:
            raise CDPError("closed")
        params = params or {}
        if session in self.paused_sessions() and method != "Runtime.runIfWaitingForDebugger":
            with self.lock:
                self.calls.append(method)
                self.log.append((method, dict(params), session))
            raise CDPError("timeout:" + method)   # vrai Chromium : aucune réponse tant que la cible est en pause
        with self.lock:
            self.calls.append(method)
            self.log.append((method, dict(params), session))
        if method == "Runtime.runIfWaitingForDebugger":
            with self.lock:
                self.waiting.discard(session)
                q = [x for x in self.queued if x[2] == session]
                self.queued = [x for x in self.queued if x[2] != session]
            for m, prm, sess, box, done in q:   # commandes en file : traitées dans l'ordre après la relance
                try:
                    box["r"] = self._answer(m, prm or {}, sess)
                except CDPError as e:
                    box["e"] = e
                done.set()
            return {}
        return self._answer(method, params, session)

    def _answer(self, method, params, session):
        if method == "Browser.getVersion":
            return {"product": "Chrome/fake"}
        if method == "Target.getTargets":
            return {"targetInfos": self.targets()}
        if method == "Target.setAutoAttach":
            if self.no_autoattach:
                raise CDPError("Target.setAutoAttach:unsupported")
            self.autoattach = True
            return {}
        if method == "Target.createTarget":
            assert params.get("background") is True and params.get("url") == URL
            if self.autoattach:   # événement d'attachement livré AVANT la réponse, page en pause
                with self.lock:
                    self.waiting.update(("S1", "S-P0") if self.popup_on_create else ("S1",))
                self.on_event({"method": "Target.attachedToTarget",
                               "params": {"sessionId": "S1", "waitingForDebugger": True,
                                          "targetInfo": {"targetId": "T1", "type": "page", "url": "about:blank"}}})
                if self.popup_on_create:
                    self.on_event({"method": "Target.attachedToTarget",
                                   "params": {"sessionId": "S-P0", "waitingForDebugger": True,
                                              "targetInfo": {"targetId": "P0", "type": "page", "url": ""}}})
            return {"targetId": "T1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": "LD1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            e = params["expression"]
            if e == assist.PROBE_JS:
                st = self.scenario
                return {"result": {"value": {"origin": ORIGIN if st != "foreign" else "https://other.net",
                                             "state": st if st != "foreign" else "found", "n": 1}}}
            if e == assist.MAXIMIZE_JS:
                return {"result": {"value": "ok"}}
            if e in (assist.CHECK_JS, assist.CHECK_FAST_JS):
                full = e == assist.CHECK_JS
                with self.lock:
                    if full:
                        self.n_full += 1
                    else:
                        self.n_fast += 1
                    q = self.check_states if (full or self.fast_states is None) else self.fast_states
                    st = q.pop(0) if q else "ready"
                v = {"state": st, "x": 960, "y": 540, "dirty": self.dirty} if st == "ready" else {"state": st}
                return {"result": {"value": v}}
            if e == assist.PAINTED_JS:
                return {"result": {"value": "visible"}}
            return {"result": {"value": None}}
        if method == "Input.dispatchMouseEvent":
            if params["type"] == "mousePressed":
                self.clicks += 1
                if self.hang_mouse:   # navigateur qui ne répond plus pendant le clic (jusqu'à la fermeture du tube)
                    self.closed_ev.wait(30)
                    raise CDPError("closed")
            return {}
        if method == "Target.closeTarget":
            tid = params.get("targetId")
            if tid in self.stuck:
                raise CDPError("Target.closeTarget:refused")
            self.closed_targets.append(tid)
            with self.lock:
                self.extra = [t for t in self.extra if t["targetId"] != tid]
            return {}
        return {}

    def close(self):
        self.closed = True
        self.closed_ev.set()


class Harness:
    """Session réelle de playerd avec faux navigateur ; vrai groupe de processus pour SIGKILL."""

    def __init__(self, tc, scenario="found", check_states=None, cdp_kw=None, **kw):
        self.dir = tempfile.mkdtemp(prefix="otvp")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.logs, self.procs, self.cdp = [], [], None

        def popen(argv, env, fds):
            self.argv, self.env = argv, env
            p = subprocess.Popen(["sleep", "60"], start_new_session=True, pass_fds=fds,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(p)
            tc.addCleanup(lambda: p.poll() is None and p.kill())
            return p

        def cdp_factory(w, r, on_event=None):
            os.close(w)
            os.close(r)
            self.cdp = FakeCDP(scenario, on_event, check_states, **(cdp_kw or {}))
            return self.cdp

        params = dict(assist_s=1.0, settle_s=0.05, max_s=600, env={"WAYLAND_DISPLAY": "wayland-0"},
                      run_dir=self.dir, log=lambda ev, **f: self.logs.append(dict(f, event=ev)), popen=popen,
                      cdp_factory=cdp_factory, keys=False, resolve=lambda h: ["93.184.216.34"],
                      local_addrs=lambda: set())
        params.update(kw)
        self.s = playerd.Session(playerd.new_sid(), URL, ORIGIN, "Le Grand Voyage", **params)

    def run(self, until=("revealed", "ended"), timeout=8):
        self.s.start()
        end = time.monotonic() + timeout
        while time.monotonic() < end and self.s.state not in until:
            time.sleep(0.01)
        return self.s.state


def no_url(tc, obj):
    text = json.dumps(obj)
    tc.assertNotIn("https://", text)
    tc.assertNotIn("films-mirror", text)
    tc.assertNotIn("/b/tok/", text)


class TestSession(unittest.TestCase):
    def run_or_explain(self, h, want="revealed"):
        st = h.run()
        self.assertEqual(st, want, (h.s.status(), h.logs))
        return st

    def test_dominant_player_revealed_full_screen_with_exactly_one_click(self):
        h = Harness(self)
        self.run_or_explain(h)
        time.sleep(0.4)
        self.assertEqual(h.cdp.clicks, 1)
        self.assertTrue(h.s.clicked)
        calls = h.cdp.calls
        # la fiche est préparée en arrière-plan, agrandie et vérifiée AVANT l'activation (révélation)
        self.assertLess(calls.index("Target.createTarget"), calls.index("Target.activateTarget"))
        # page d'arrière-plan émulée visible/active AVANT toute détection (lecteur à opacité 0 sinon)
        self.assertLess(calls.index("Emulation.setFocusEmulationEnabled"), calls.index("Page.createIsolatedWorld"))
        self.assertLess(calls.index("Page.setWebLifecycleState"), calls.index("Page.createIsolatedWorld"))
        self.assertIn("L0", h.cdp.closed_targets)   # onglet « Chargement… » fermé après la révélation
        self.assertTrue(h.s.close("back"))
        st = h.s.status()
        self.assertEqual((st["state"], st["reason"], st["gone"]), ("ended", "back", True))
        self.assertEqual(h.cdp.clicks, 1)
        self.assertIsNotNone(h.procs[0].poll())          # groupe tué
        self.assertEqual(os.listdir(h.dir), [])           # profil temporaire supprimé
        no_url(self, h.logs)
        no_url(self, st)

    def test_ambiguous_and_no_player_never_reveal_never_click(self):
        for sc, out in (("ambiguous", "ambiguous"), ("none", "no-player"), ("foreign", "foreign")):
            h = Harness(self, scenario=sc)
            self.assertEqual(h.run(), "ended", sc)
            st = h.s.status()
            self.assertEqual(st["outcome"], out)
            self.assertNotIn("Target.activateTarget", h.cdp.calls)
            self.assertEqual(h.cdp.clicks, 0)
            self.assertTrue(st["gone"])
            self.assertEqual(os.listdir(h.dir), [])

    def test_obstructed_player_is_never_revealed(self):
        h = Harness(self, check_states=["obstructed"] * 50)
        self.assertEqual(h.run(timeout=10), "ended")
        self.assertEqual(h.s.status()["outcome"], "obstructed")
        self.assertNotIn("Target.activateTarget", h.cdp.calls)
        self.assertEqual(h.cdp.clicks, 0)

    def test_back_during_preparation_cancels_without_reveal_or_click(self):
        h = Harness(self, settle_s=2.0)
        h.s.start()
        end = time.monotonic() + 5
        while "Page.createIsolatedWorld" not in (h.cdp.calls if h.cdp else []) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(h.s.close("back"))
        time.sleep(0.3)
        self.assertNotIn("Target.activateTarget", h.cdp.calls)
        self.assertEqual(h.cdp.clicks, 0)
        self.assertEqual(h.s.status()["reason"], "back")
        self.assertEqual(os.listdir(h.dir), [])

    def test_popup_closed_and_main_frame_navigation_ends_session(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        end = time.monotonic() + 3
        while not h.s.clicked and time.monotonic() < end:
            time.sleep(0.01)
        h.cdp.on_event({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "P5", "type": "page"}}})
        h.cdp.on_event({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "X9", "type": "page"}}})
        time.sleep(0.2)
        self.assertIn("P5", h.cdp.closed_targets)
        self.assertNotIn("X9", h.cdp.closed_targets)       # cible déjà connue : jamais fermée
        h.cdp.on_event({"method": "Page.frameStartedLoading", "sessionId": "S1", "params": {"frameId": "F1"}})
        self.assertTrue(h.s.ended.wait(5))
        self.assertEqual(h.s.status()["outcome"], "navigated")
        self.assertEqual(h.cdp.clicks, 1)

    def test_player_lost_after_reveal_ends_session(self):
        h = Harness(self, check_states=["ready", "ready", "obstructed", "obstructed", "obstructed"])
        self.assertEqual(h.run(), "revealed")
        self.assertTrue(h.s.ended.wait(6))
        self.assertEqual((h.s.status()["outcome"], h.s.status()["reason"]), ("obstructed", "player_lost"))

    def test_back_right_after_reveal_never_clicks_later(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        self.assertTrue(h.s.close("back"))   # Retour entre la révélation et le clic hérité du OK
        time.sleep(0.6)
        self.assertEqual(h.cdp.clicks, 0)
        self.assertFalse(h.s.clicked)

    def test_playback_cap(self):
        h = Harness(self, max_s=0.3)
        self.assertEqual(h.run(), "revealed")
        self.assertTrue(h.s.ended.wait(5))
        self.assertEqual(h.s.status()["reason"], "cap")

    def test_no_display_fails_closed(self):
        h = Harness(self, env=None)
        self.assertEqual(h.run(), "ended")
        self.assertEqual(h.s.status()["reason"], "no_display")

    def test_ten_cycles_leave_nothing_behind(self):
        for i in range(10):
            h = Harness(self)
            self.assertEqual(h.run(), "revealed", i)
            end = time.monotonic() + 3
            while not h.s.clicked and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(h.s.close("back"))
            self.assertEqual(h.cdp.clicks, 1)
            self.assertTrue(h.s.status()["gone"])
            self.assertEqual(os.listdir(h.dir), [])
            self.assertTrue(all(p.poll() is not None for p in h.procs))


class TestArgvAndLoading(unittest.TestCase):
    def test_flags_use_wrapper_pipe_only_and_no_forbidden_flags(self):
        a = playerd.player_argv("/run/opentv-player/s-x", "T")
        self.assertEqual(a[0], "/usr/bin/chromium")
        self.assertIn("--remote-debugging-pipe", a)
        for bad in ("--no-sandbox", "--disable-gpu", "--disable-gpu-sandbox", "--remote-debugging-port=9222",
                    "--enable-features=VaapiVideoDecoder", "--use-gl=egl", "--disable-web-security"):
            self.assertFalse(any(x.startswith(bad.split("=")[0] + ("=" if "=" in bad else "")) and x == bad
                                 for x in a))
            with self.assertRaises(ValueError):
                playerd.player_argv("/p", "T", extra=(bad,))
        self.assertFalse(any("port" in x for x in a))

    def test_loading_page_is_static_escaped_and_offline(self):
        u = playerd.loading_url('<script>alert(1)</script> & "x"')
        self.assertTrue(u.startswith("data:text/html;charset=utf-8,"))
        import urllib.parse
        html = urllib.parse.unquote(u.split(",", 1)[1])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("http", html.replace("http-equiv", ""))
        self.assertIn("Chargement", html)


class TestController(unittest.TestCase):
    def ctl(self):
        made = []

        class S:
            def __init__(self, sid, url, origin, title, **kw):
                self.sid, self.kw, self.ended = sid, kw, threading.Event()
                made.append(self)

            def start(self):
                return self

            def status(self):
                return {"sid": self.sid, "state": "starting"}

            def close(self, reason):
                self.ended.set()
                self.gone = True

        return playerd.Controller(session_factory=S, env={"WAYLAND_DISPLAY": "w"}, gate=lambda: True), made

    def test_open_validates_target_and_is_single_session(self):
        c, made = self.ctl()
        sid = playerd.new_sid()
        for bad in ({"url": "http://films-mirror.net/x", "origin": "http://films-mirror.net"},
                    {"url": URL, "origin": "https://evil.net"},
                    {"url": "https://u:p@films-mirror.net/x", "origin": "https://films-mirror.net"},
                    {"url": URL + "#f", "origin": ORIGIN}):
            r = c.handle(dict(bad, op="open", sid=sid))
            self.assertFalse(r["ok"], bad)
        self.assertFalse(c.handle({"op": "open", "sid": "x", "url": URL, "origin": ORIGIN})["ok"])
        self.assertFalse(c.handle({"op": "open", "sid": sid, "url": URL, "origin": ORIGIN, "max_s": 10 ** 9})["ok"])
        self.assertTrue(c.handle({"op": "open", "sid": sid, "url": URL, "origin": ORIGIN})["ok"])
        self.assertEqual(c.handle({"op": "open", "sid": playerd.new_sid(), "url": URL, "origin": ORIGIN})["error"],
                         "busy")
        r = c.handle({"op": "close", "sid": sid, "reason": "back"})
        self.assertTrue(r["ok"])
        self.assertTrue(c.handle({"op": "open", "sid": playerd.new_sid(), "url": URL, "origin": ORIGIN})["ok"])
        no_url(self, c.handle({"op": "status", "sid": None}))

    def test_heartbeat_closes_orphan_session(self):
        t = [0.0]
        c, made = self.ctl()
        c.mono = lambda: t[0]
        c.handle({"op": "open", "sid": playerd.new_sid(), "url": URL, "origin": ORIGIN})
        t[0] = 30.0
        c.tick()
        self.assertTrue(made[0].ended.wait(2))


class TestSocket(unittest.TestCase):
    def serve(self, allowed):
        d = tempfile.mkdtemp(prefix="otvs", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "p.sock")
        c, _ = TestController.ctl(self)
        srv = playerd.Server(path, c, allowed)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return path

    def test_peer_uid_allowed_and_refused(self):
        from opentv_pi.playerclient import PlayerClient, PlayerUnavailable
        ok = PlayerClient(self.serve({os.getuid()}))
        self.assertTrue(ok.ping())
        no = PlayerClient(self.serve({os.getuid() + 12345}))
        self.assertFalse(no.ping())
        with self.assertRaises(PlayerUnavailable):
            no.call({"op": "ping"})

    def test_socket_mode_and_bad_json(self):
        path = self.serve({os.getuid()})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o666)
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        s.sendall(b"[[[[\n")
        self.assertEqual(json.loads(s.recv(4096))["error"], "bad_json")
        s.close()


class TestKeys(unittest.TestCase):
    def ev(self, code, value, typ=1):
        return struct.pack(evkeys.EVENT_FMT, 0, 0, typ, code, value)

    def test_close_keys_from_cec_hid_and_keyboard(self):
        self.assertEqual(evkeys.parse_events(self.ev(158, 1)), ["back"])      # KEY_BACK
        self.assertEqual(evkeys.parse_events(self.ev(174, 1)), ["exit"])      # CEC Exit
        self.assertEqual(evkeys.parse_events(self.ev(172, 1)), ["home"])      # Accueil
        self.assertEqual(evkeys.parse_events(self.ev(1, 1)), ["back"])        # Échap
        self.assertEqual(evkeys.parse_events(self.ev(158, 0) + self.ev(158, 2)), [])   # relâché / répétition
        for code in (103, 108, 105, 106, 28, 352):   # flèches et OK : jamais une fermeture
            self.assertEqual(evkeys.parse_events(self.ev(code, 1)), [])
        self.assertEqual(evkeys.parse_events(self.ev(158, 1, typ=4)), [])

    def test_watcher_reads_fifo_device_and_reports_each_typed_reason(self):
        d = tempfile.mkdtemp(prefix="otvk", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        r, w = os.pipe()
        got = []
        kw = evkeys.KeyWatcher(got.append, pattern=os.path.join(d, "event*"), rescan_s=0.05,
                               opener=lambda p: os.dup(r), has_key=lambda fd: True)
        open(os.path.join(d, "event3"), "w").close()
        kw.start()
        time.sleep(0.2)
        os.write(w, self.ev(108, 1) + self.ev(174, 1) + self.ev(158, 2) + self.ev(158, 1) + self.ev(88, 1))
        time.sleep(0.3)
        kw.stop()
        os.close(r)
        os.close(w)
        self.assertEqual(got, ["exit", "back", "devtools"])   # flèche ignorée, répétition ignorée


if __name__ == "__main__":
    unittest.main()


class TestSpawnPipe(unittest.TestCase):
    def test_child_gets_pipe_on_fd3_fd4_even_with_high_parent_fds(self):
        """Vrai fils : lit fd 3 et écrit fd 4 (descripteurs parents ≥ 10, shell bash)."""
        from opentv_pi.cdp import spawn_with_pipe
        prog = ("import os; d = os.read(3, 64); os.write(4, d.upper()); os.close(4)")

        def popen(argv, env, fds):
            return subprocess.Popen(argv, env=env, pass_fds=fds, start_new_session=True)
        holders = [os.open(os.devnull, os.O_RDONLY) for _ in range(8)]   # 3..10 occupés : extrémités hautes
        try:
            proc, w, r = spawn_with_pipe(["python3", "-c", prog], dict(os.environ), popen)
        finally:
            for fd in holders:
                os.close(fd)
        self.assertGreaterEqual(min(w, r), 10)
        os.write(w, b"cdp\0")
        os.close(w)
        self.assertEqual(os.read(r, 64), b"CDP\0")
        os.close(r)
        self.assertEqual(proc.wait(10), 0)


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class TestPopupsPausedAndClosed(unittest.TestCase):
    """Surgissantes : mises en pause par l'auto-attachement AVANT d'exécuter quoi que ce soit, jamais relancées ;
    l'onglet attendu est réactivé AVANT la fermeture (fermer un onglet inactif ne sélectionne jamais la fiche)."""

    def test_auto_attach_is_armed_before_the_site_target_and_only_our_page_is_resumed(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        c = h.cdp
        self.assertLess(c.index("Target.setAutoAttach"), c.index("Target.createTarget"))
        self.assertEqual(c.log[c.index("Target.setAutoAttach")][1],
                         {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        # notre fiche : session auto-attachée (aucun attachement manuel), relancée APRÈS l'émulation de visibilité
        self.assertIsNone(c.index("Target.attachToTarget"))
        run = c.index("Runtime.runIfWaitingForDebugger", session="S1")
        self.assertIsNotNone(run)
        self.assertLess(c.index("Page.enable", session="S1"), run)
        self.assertLess(c.index("Emulation.setFocusEmulationEnabled", session="S1"), run)
        self.assertLess(run, c.index("Page.createIsolatedWorld"))

    def test_popup_attached_during_creation_is_closed_never_resumed(self):
        h = Harness(self, cdp_kw={"popup_on_create": True})
        self.assertEqual(h.run(), "revealed")
        c = h.cdp
        self.assertIn("P0", c.closed_targets)
        self.assertIsNone(c.index("Runtime.runIfWaitingForDebugger", session="S-P0"))
        self.assertNotIn("T1", c.closed_targets)

    def test_popup_before_reveal_reactivates_loading_tab_before_closing(self):
        h = Harness(self, settle_s=1.0)
        h.s.start()
        self.assertTrue(wait_for(lambda: h.cdp is not None and h.cdp.index("Page.createIsolatedWorld") is not None))
        h.cdp.add({"targetId": "P1", "type": "page", "url": ""})
        self.assertTrue(wait_for(lambda: "P1" in h.cdp.closed_targets))
        c = h.cdp
        act = c.index("Target.activateTarget", targetId="L0")
        self.assertIsNotNone(act)
        self.assertLess(act, c.index("Target.closeTarget", targetId="P1"))   # jamais la fiche sélectionnée
        self.assertIsNone(c.index("Runtime.runIfWaitingForDebugger", session="S-P1"))
        self.assertIsNone(c.index("Target.activateTarget", targetId="T1"))   # la fiche n'est pas encore révélée
        self.assertEqual(h.run(), "revealed")
        self.assertEqual(h.s.popups, 1)

    def test_popup_after_reveal_keeps_player_in_front(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        self.assertTrue(wait_for(lambda: h.s.clicked))
        n0 = len(h.cdp.log)
        h.cdp.add({"targetId": "P2", "type": "page", "url": ""})
        self.assertTrue(wait_for(lambda: "P2" in h.cdp.closed_targets))
        tail = h.cdp.log[n0:]
        acts = [i for i, (m, prm, _) in enumerate(tail) if m == "Target.activateTarget" and prm.get("targetId") == "T1"]
        close = [i for i, (m, prm, _) in enumerate(tail) if m == "Target.closeTarget" and prm.get("targetId") == "P2"]
        self.assertTrue(acts and close and acts[0] < close[0], tail)
        self.assertEqual(h.s.state, "revealed")
        self.assertEqual(h.cdp.clicks, 1)   # jamais de clic supplémentaire

    def test_unclosable_popup_ends_session_fail_closed(self):
        h = Harness(self, cdp_kw={"stuck": ("P3",)})
        self.assertEqual(h.run(), "revealed")
        h.cdp.add({"targetId": "P3", "type": "page", "url": ""})
        self.assertTrue(h.s.ended.wait(5))
        self.assertEqual(h.s.status()["reason"], "popup")
        self.assertTrue(h.s.status()["gone"])

    def test_iframes_and_workers_are_never_closed_and_paused_workers_resume(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        h.cdp.add({"targetId": "F9", "type": "iframe", "url": ""}, attach=False)
        h.cdp.add({"targetId": "W9", "type": "service_worker", "url": ""})
        self.assertTrue(wait_for(lambda: h.cdp.index("Runtime.runIfWaitingForDebugger", session="S-W9") is not None))
        time.sleep(0.8)   # au moins un balayage du moniteur
        self.assertNotIn("F9", h.cdp.closed_targets)
        self.assertNotIn("W9", h.cdp.closed_targets)
        self.assertEqual(h.s.state, "revealed")

    def test_missing_auto_attach_fails_closed_before_assist(self):
        h = Harness(self, cdp_kw={"no_autoattach": True})
        self.assertEqual(h.run(), "ended")
        st = h.s.status()
        self.assertEqual((st["outcome"], st["reason"]), ("error", "autoattach_required"))
        self.assertIsNone(h.cdp.index("Target.attachToTarget"))


class TestDevTools(unittest.TestCase):
    """DeveloperToolsAvailability=1 (exigé par le tube CDP) : DevTools ne reste jamais visible."""

    def test_devtools_target_is_closed_as_soon_as_it_appears(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        h.cdp.add({"targetId": "D1", "type": "other", "url": "devtools://devtools/bundled/devtools_app.html"},
                  attach=False)
        self.assertTrue(wait_for(lambda: "D1" in h.cdp.closed_targets))
        time.sleep(0.6)
        self.assertEqual(h.s.state, "revealed")

    def test_devtools_found_by_sweep_even_without_event(self):
        h = Harness(self)
        self.assertEqual(h.run(), "revealed")
        with h.cdp.lock:   # apparition silencieuse (aucun événement CDP) : le balayage de 0,5 s la trouve
            h.cdp.extra.append({"targetId": "D2", "type": "page", "url": "devtools://devtools/inspector.html"})
        self.assertTrue(wait_for(lambda: "D2" in h.cdp.closed_targets, 3))

    def test_devtools_that_cannot_be_closed_ends_the_session(self):
        h = Harness(self, cdp_kw={"stuck": ("D3",)})
        self.assertEqual(h.run(), "revealed")
        h.cdp.add({"targetId": "D3", "type": "other", "url": "devtools://devtools/x"}, attach=False)
        self.assertTrue(h.s.ended.wait(5))
        self.assertIn(h.s.status()["reason"], ("popup", "devtools"))
        self.assertTrue(h.s.status()["gone"])

    def test_devtools_key_from_helper_closes_session(self):
        made = []

        class Keys:
            def __init__(self, on_key, **kw):
                self.on_key, self.kw = on_key, kw
                self.stop_ev, self.armed = threading.Event(), threading.Event()
                self.armed.set()
                self.devices = 1
                made.append(self)

            def start(self):
                return self

            def stop(self):
                self.stop_ev.set()

        h = Harness(self, keys=True, key_watcher=Keys, keys_kw={"path": "/x.sock", "server_uids": {0}})
        self.assertEqual(h.run(), "revealed")
        self.assertEqual(made[0].kw, {"path": "/x.sock", "server_uids": {0}})
        made[0].on_key("devtools")
        self.assertTrue(h.s.ended.wait(5))
        st = h.s.status()
        self.assertEqual((st["reason"], st["gone"]), ("devtools", True))
        self.assertEqual(os.listdir(h.dir), [])

    def test_policy_allows_cdp_pipe_but_blocks_devtools_pages_and_documents_why(self):
        with open(os.path.join(_common.PI4, "system", "chromium-policy-opentv-player.json")) as f:
            pol = json.load(f)
        self.assertEqual(pol["DeveloperToolsAvailability"], 1)   # 2 coupe le tube CDP (mesuré sur le Pi)
        for u in ("devtools://*", "chrome://*", "view-source:*", "file://*", "chrome-untrusted://*"):
            self.assertIn(u, pol["URLBlocklist"])
        self.assertEqual((pol["DefaultPopupsSetting"], pol["DownloadRestrictions"]), (2, 3))
        self.assertEqual(pol["ExtensionInstallBlocklist"], ["*"])
        self.assertFalse(pol["PrintingEnabled"])
        with open(os.path.join(_common.PI4, "systemd", "opentv-player.service")) as f:
            unit = f.read()
        with open(os.path.join(_common.PI4, "README.md")) as f:
            readme = f.read()
        for name, text in (("unit", unit), ("playerd", playerd.__doc__), ("README", readme)):
            self.assertTrue("DeveloperToolsAvailability=1" in text, name)
        self.assertFalse(any("remote-debugging-port" in a or "remote-debugging-address" in a
                             for a in playerd.player_argv("/p", "T")))


class TestKeysRequired(unittest.TestCase):
    class Keys:
        def __init__(self, on_key, armed=True, devices=1, **kw):
            self.on_key, self.stop_ev, self.armed = on_key, threading.Event(), threading.Event()
            self.devices = devices
            if armed:
                self.armed.set()

        def start(self):
            return self

        def stop(self):
            self.stop_ev.set()

    def test_never_revealed_without_armed_close_keys(self):
        h = Harness(self, keys=True, key_watcher=lambda cb, **kw: self.Keys(cb, armed=False), keys_wait_s=0.2)
        self.assertEqual(h.run(), "ended")
        st = h.s.status()
        self.assertEqual((st["outcome"], st["reason"]), ("error", "keys_unavailable"))
        self.assertNotIn("Target.activateTarget", h.cdp.calls)
        self.assertEqual(h.cdp.clicks, 0)

    def test_lost_key_link_during_playback_ends_session(self):
        box = []

        def factory(cb, **kw):
            box.append(self.Keys(cb))
            return box[0]
        h = Harness(self, keys=True, key_watcher=factory, keys_lost_s=0.3, sweep_s=0.1)
        self.assertEqual(h.run(), "revealed")
        box[0].armed.clear()
        self.assertTrue(h.s.ended.wait(5))
        self.assertEqual((h.s.status()["outcome"], h.s.status()["reason"]), ("revealed", "keys_lost"))

    def test_close_keys_from_helper_close_with_their_reason(self):
        for why in ("back", "exit", "home"):
            box = []
            h = Harness(self, keys=True, key_watcher=lambda cb, **kw: box.append(self.Keys(cb)) or box[0])
            self.assertEqual(h.run(), "revealed")
            box[0].on_key(why)
            self.assertTrue(h.s.ended.wait(5))
            self.assertEqual(h.s.status()["reason"], why)
            box[0].on_key("rawcode")   # jamais une raison hors liste


class TestDecoder(unittest.TestCase):
    def ev(self, code, value):
        return struct.pack(evkeys.EVENT_FMT, 0, 0, 1, code, value)

    def test_devtools_shortcuts_and_modifier_state(self):
        d = evkeys.KeyDecoder()
        self.assertEqual(d.feed(self.ev(88, 1)), ["devtools"])                                    # F12
        for k in (23, 36, 46):                                                                     # I J C
            self.assertEqual(d.feed(self.ev(29, 1) + self.ev(42, 1) + self.ev(k, 1) + self.ev(42, 0) +
                                    self.ev(29, 0)), ["devtools"], k)
        self.assertEqual(d.feed(self.ev(97, 1) + self.ev(22, 1) + self.ev(97, 0)), ["devtools"])     # Ctrl+U
        self.assertEqual(d.feed(self.ev(125, 1) + self.ev(56, 1) + self.ev(23, 1) + self.ev(56, 0) +
                                self.ev(125, 0)), ["devtools"])                                     # Méta+Alt+I
        self.assertEqual(d.feed(self.ev(42, 1) + self.ev(23, 1) + self.ev(42, 0)), [])              # Maj+I seul
        self.assertEqual(d.feed(self.ev(23, 1) + self.ev(36, 1)), [])                               # I, J
        self.assertEqual(d.feed(self.ev(29, 1) + self.ev(1, 1) + self.ev(29, 0)), [])               # Ctrl+Échap
        self.assertEqual(d.feed(self.ev(1, 1)), ["back"])                                           # relâché : rien
        self.assertEqual(d.feed(self.ev(88, 2)), [])                                                # répétition
        self.assertEqual(sorted(set(evkeys.REASONS)), ["back", "devtools", "exit", "home"])


class TestKeysHelper(unittest.TestCase):
    """Aide des touches sur un vrai socket Unix : SO_PEERCRED dans les deux sens, armement seulement par abonnement,
    événements typés seulement, désarmement à la déconnexion, plafonds."""

    class FakeWatcher:
        made = []
        COUNT = 1

        def __init__(self, on_key, on_devices=None):
            self.on_key, self.on_devices, self.stopped = on_key, on_devices, False
            self.ready = threading.Event()
            self.ready.set()
            self.count = TestKeysHelper.FakeWatcher.COUNT
            TestKeysHelper.FakeWatcher.made.append(self)

        def start(self):
            return self

        def stop(self):
            self.stopped = True

    def serve(self, allowed, **hub_kw):
        from opentv_pi import playerkeys
        TestKeysHelper.FakeWatcher.made = []
        TestKeysHelper.FakeWatcher.COUNT = hub_kw.pop("count", 1)
        hub_kw.setdefault("probe", lambda: TestKeysHelper.FakeWatcher.COUNT)
        d = tempfile.mkdtemp(prefix="otvk", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "k.sock")
        hub = playerkeys.Hub(watcher_factory=TestKeysHelper.FakeWatcher, **hub_kw)
        srv = playerkeys.Server(path, hub, allowed)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(hub.close)
        self.addCleanup(srv.stopping.set)
        return path, hub

    def test_subscribe_arms_delivers_typed_events_and_disarms_on_close(self):
        from opentv_pi import playerkeys
        path, hub = self.serve({os.getuid()})
        self.assertTrue(playerkeys.ping(path, server_uids={os.getuid()}))
        self.assertFalse(hub.armed())                     # un ping n'ouvre aucun périphérique
        self.assertEqual(TestKeysHelper.FakeWatcher.made, [])
        got = []
        kc = playerkeys.KeyClient(got.append, path=path, server_uids={os.getuid()}).start()
        self.addCleanup(kc.stop)
        self.assertTrue(kc.armed.wait(3))
        self.assertTrue(hub.armed())
        w = TestKeysHelper.FakeWatcher.made[0]
        for why in ("back", "KEY_A", "devtools", "home"):
            w.on_key(why)
        self.assertTrue(wait_for(lambda: len(got) == 3))
        self.assertEqual(got, ["back", "devtools", "home"])   # raison hors liste jamais transmise
        kc.stop()
        self.assertTrue(wait_for(lambda: w.stopped and not hub.armed()))   # aucun abonné : evdev refermé

    def test_peer_refused_and_wrong_server_uid_refused(self):
        from opentv_pi import playerkeys
        path, hub = self.serve({os.getuid() + 4242})
        self.assertFalse(playerkeys.ping(path))
        kc = playerkeys.KeyClient(lambda w: None, path=path, retry_s=0.05).start()
        self.addCleanup(kc.stop)
        self.assertFalse(kc.armed.wait(0.5))
        self.assertEqual(kc.refused, "forbidden")
        self.assertFalse(hub.armed())
        path2, hub2 = self.serve({os.getuid()})
        kc2 = playerkeys.KeyClient(lambda w: None, path=path2, server_uids={os.getuid() + 1}, retry_s=0.05).start()
        self.addCleanup(kc2.stop)
        self.assertFalse(kc2.armed.wait(0.5))           # le client vérifie aussi le propriétaire du socket
        self.assertEqual(kc2.refused, "server_uid")
        self.assertFalse(playerkeys.ping(path2, server_uids={os.getuid() + 1}))

    def test_bounded_subscribers_and_rate(self):
        from opentv_pi import playerkeys
        t = [0.0]
        path, hub = self.serve({os.getuid()}, max_clients=1, mono=lambda: t[0], rate=1.0, burst=2.0)
        got = []
        kc = playerkeys.KeyClient(got.append, path=path).start()
        self.addCleanup(kc.stop)
        self.assertTrue(kc.armed.wait(3))
        kc2 = playerkeys.KeyClient(lambda w: None, path=path, retry_s=0.05).start()
        self.addCleanup(kc2.stop)
        self.assertFalse(kc2.armed.wait(0.4))
        self.assertEqual(kc2.refused, "busy")
        w = TestKeysHelper.FakeWatcher.made[0]
        for _ in range(5):
            w.on_key("back")
        self.assertTrue(wait_for(lambda: len(got) == 2))
        time.sleep(0.2)
        self.assertEqual(len(got), 2)                    # rafale plafonnée
        t[0] += 1.0
        w.on_key("exit")
        self.assertTrue(wait_for(lambda: got[-1:] == ["exit"]))

    def test_bad_requests(self):
        path, _ = self.serve({os.getuid()})
        for raw in (b"[[[[\n", b'{"op": "dump"}\n', b"\xff\n"):
            s = socket.socket(socket.AF_UNIX)
            s.connect(path)
            s.sendall(raw)
            self.assertEqual(json.loads(s.recv(4096))["error"], "bad_request")
            s.close()


class TestControllerGate(unittest.TestCase):
    def test_open_refused_unless_promoted_and_keys_available(self):
        c = playerd.Controller(session_factory=lambda *a, **k: self.fail("jamais de séance"),
                               env={"WAYLAND_DISPLAY": "w"})
        req = {"op": "open", "sid": playerd.new_sid(), "url": URL, "origin": ORIGIN}
        self.assertEqual(c.handle(dict(req))["error"], "not_promoted")   # défaut : porte fermée
        self.assertEqual(c.handle({"op": "ping"}), {"ok": True, "promoted": False, "keys": True})
        c.gate = lambda: (_ for _ in ()).throw(OSError("x"))
        self.assertEqual(c.handle(dict(req))["error"], "not_promoted")   # porte illisible : fermée
        c.gate, c.keys_ok = (lambda: True), (lambda: False)
        self.assertEqual(c.handle(dict(req))["error"], "keys_unavailable")
        self.assertEqual(c.handle({"op": "ping"}), {"ok": True, "promoted": True, "keys": False})

    def test_client_ping_requires_promoted_and_keys(self):
        from opentv_pi.playerclient import PlayerClient
        answers = iter(({"ok": True}, {"ok": True, "promoted": True}, {"ok": True, "promoted": False, "keys": True},
                        {"ok": True, "promoted": True, "keys": True}))
        pc = PlayerClient("/nonexistent")
        pc.call = lambda req, timeout=None: next(answers)
        self.assertEqual([pc.ping() for _ in range(4)], [False, False, False, True])


class TestEffectiveGroups(unittest.TestCase):
    """Groupes effectifs de chaque unité = groupe primaire + appartenances sysusers du compte + SupplementaryGroups :
    seuls Cage (opentv-kiosk.service) et l'aide des touches ont `input` ; le contrôleur du lecteur et son Chromium,
    jamais."""

    @staticmethod
    def sysusers():
        members = {}
        with open(os.path.join(_common.PI4, "system", "sysusers-opentv.conf")) as f:
            for line in f:
                w = line.split()
                if len(w) >= 3 and w[0] == "m":
                    members.setdefault(w[1], set()).add(w[2])
                elif len(w) >= 2 and w[0] == "u":
                    members.setdefault(w[1], set())
        return members

    @staticmethod
    def unit(name):
        import configparser
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        with open(os.path.join(_common.PI4, "systemd", name), encoding="utf-8") as f:
            cp.read_file(f)
        return cp["Service"]

    def effective(self, name):
        svc = self.unit(name)
        user = svc.get("User", "root")
        groups = {svc.get("Group", user)} | self.sysusers().get(user, set())
        return user, groups | set(svc.get("SupplementaryGroups", "").split())

    def test_input_only_for_cage_and_keys_helper(self):
        users = self.sysusers()
        self.assertNotIn("input", users["opentv-kiosk"])          # le compte lui-même : jamais membre d'input
        self.assertEqual(users["opentv-keys"], set())
        user, g = self.effective("opentv-player.service")
        self.assertEqual(user, "opentv-kiosk")
        self.assertNotIn("input", g)
        self.assertEqual(g, {"opentv-kiosk", "video", "render"})
        user, g = self.effective("opentv-player-keys.service")
        self.assertEqual((user, g), ("opentv-keys", {"opentv-keys", "input"}))
        self.assertIn("input", self.effective("opentv-kiosk.service")[1])   # comportement de Cage inchangé
        for u in ("opentv.service", "opentv-net-helper.service", "opentv-ap-guard.service", "opentv-splash.service"):
            p = os.path.join(_common.PI4, "systemd", u)
            if os.path.exists(p):
                self.assertNotIn("input", self.effective(u)[1], u)

    def test_keys_helper_unit_is_hardened_and_offline(self):
        svc = self.unit("opentv-player-keys.service")
        want = {"NoNewPrivileges": "yes", "CapabilityBoundingSet": "", "AmbientCapabilities": "",
                "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateNetwork": "yes",
                "RestrictAddressFamilies": "AF_UNIX", "DevicePolicy": "closed", "DeviceAllow": "char-input r",
                "RestrictNamespaces": "yes", "MemoryDenyWriteExecute": "yes", "SystemCallArchitectures": "native",
                "ProtectProc": "invisible", "User": "opentv-keys", "SupplementaryGroups": "input",
                "ExecStart": "/usr/bin/python3 -I /opt/opentv/bin/opentv-player-keys"}
        for k, v in want.items():
            self.assertEqual(svc.get(k), v, k)
        player = self.unit("opentv-player.service")
        self.assertEqual(player["NoNewPrivileges"], "yes")
        self.assertNotIn("input", player["SupplementaryGroups"].split())
        with open(os.path.join(_common.PI4, "systemd", "opentv-player.service")) as f:
            self.assertIn("Wants=opentv-player-keys.service", f.read())

    def test_installer_drops_legacy_kiosk_input_membership(self):
        with open(os.path.join(_common.PI4, "install.sh")) as f:
            text = f.read()
        self.assertIn("gpasswd -d opentv-kiosk input", text)
        self.assertLess(text.index("post systemd-sysusers"), text.index("post drop_kiosk_input"))


class TestAssistInRealChromium(unittest.TestCase):
    """CHECK_JS dans un vrai Chromium, avec preuve PIXEL : un calque `pointer-events: none` (invisible pour
    elementFromPoint), un ancêtre qui crée un contexte d'empilement (z-index positionné, flex/grid, isolation,
    will-change, transform, contain, container-type, opacity, filter, blend…) ou un conteneur de taille nulle à z-index
    maximal dont l'enfant déborde ne passent jamais ; « ready » implique 100 % de pixels du lecteur (aplat vert) sur une
    grille dense. La page parente seule est inspectée (aucune entrée dans l'iframe)."""

    PAGE = ('<!doctype html><html><head><style>body{margin:0;background:#fff}header{height:120px;background:#c00}'
            '%s</style></head><body><header>Site</header><div id="wrap"><iframe id="pl" '
            'srcdoc="<body style=\'margin:0;background:#00ff00\'></body>" '
            'style="width:1280px;height:720px;border:0"></iframe></div>%s</body></html>')
    # calque publicitaire transparent aux clics, z-index ORDINAIRE (cas a-c du relecteur) ou maximal
    AD = ('<div id="ad" style="position:fixed;left:0;right:0;bottom:0;height:300px;background:rgba(255,0,0,.9);'
          'z-index:%s;pointer-events:none">PUB</div>')
    ZERO = ('<div style="position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none">'
            '<div style="width:1920px;height:300px;background:red">PUB</div></div>')

    @staticmethod
    def reassert(prop, value, sel="wrap"):
        """La page réimpose un style APRÈS l'agrandissement (elle se bat contre la neutralisation des ancêtres)."""
        return ("document.getElementById(%r).style.setProperty(%r, %r, 'important')" % (sel, prop, value))

    def cases(self):
        overlay = ('<div style="position:fixed;left:0;right:0;bottom:0;height:140px;background:rgba(255,0,0,.8);'
                   'z-index:2147483647;pointer-events:none;%s">PUB</div>')
        many = '<div style="display:none">' + "<i></i>" * (assist.MAX_SCAN + 5) + "</div>"
        P, AD, R = self.PAGE, self.AD, self.reassert
        FULLV = "".join(R(k, v) + ";" for k, v in (("position", "absolute"), ("top", "0"), ("left", "0"),
                                                   ("width", "100vw"), ("height", "100vh")))
        flexpage = P.replace('<div id="wrap">', '<div id="flex" style="display:flex"><div id="wrap">').replace(
            '</iframe></div>', '</iframe></div></div>')
        return [
            {"name": "clean", "html": P % ("", "")},
            {"name": "pe_none_overlay", "html": P % ("", overlay % "")},
            {"name": "hidden_overlay", "html": P % ("", overlay % "display:none")},
            {"name": "invisible_overlay", "html": P % ("", overlay % "visibility:hidden")},
            {"name": "low_z_overlay", "html": P % ("", overlay.replace("2147483647", "100") % "")},
            {"name": "pseudo_overlay", "html": P % (
                'body::after{content:"PUB";position:fixed;left:0;top:0;width:300px;height:80px;'
                'background:#0c0;z-index:2147483647;pointer-events:none}', "")},
            {"name": "shadow_overlay", "html": P % ("", '<div id="host"></div>'),
             "after": "(function(){var r=document.getElementById('host').attachShadow({mode:'open'});"
                      "r.innerHTML='<div style=\"position:fixed;inset:0;z-index:2147483647;pointer-events:none;"
                      "background:rgba(0,0,0,.4)\"></div>';})()"},
            {"name": "popover_top_layer", "html": P % ("", '<div id="pop" popover>PUB</div>'),
             "after": "document.getElementById('pop').showPopover()"},
            {"name": "huge_dom", "html": P % ("", many)},
            # --- cas a-d du relecteur, tels quels : MAXIMIZE neutralise l'ancêtre -> lecteur réellement seul (pixels)
            {"name": "a_stacking_ctx_ancestor_z1", "html": P % ("#wrap{position:relative;z-index:1}", AD % "10")},
            {"name": "b_isolation_ancestor", "html": P % ("#wrap{isolation:isolate}", AD % "10")},
            {"name": "c_willchange_ancestor", "html": P % ("#wrap{will-change:opacity}", AD % "10")},
            {"name": "d_zero_size_zmax_container", "html": P % ("", self.ZERO)},
            # --- la page réimpose le contexte d'empilement après l'agrandissement : refus fail-closed
            {"name": "a_reasserted_z1", "html": P % ("#wrap{position:relative;z-index:1}", AD % "10"),
             "after": R("z-index", "1")},
            {"name": "b_reasserted_isolation", "html": P % ("#wrap{isolation:isolate}", AD % "10"),
             "after": R("isolation", "isolate")},
            {"name": "c_reasserted_willchange", "html": P % ("#wrap{will-change:opacity}", AD % "10"),
             "after": R("will-change", "opacity")},
            {"name": "flex_item_z", "html": flexpage % ("#wrap{z-index:1}", AD % "10"), "after": R("z-index", "1")},
            {"name": "grid_item_z", "html": flexpage.replace("display:flex", "display:grid") % ("", AD % "10"),
             "after": R("z-index", "2")},
            # transform/contain/backdrop-filter/perspective font aussi de l'ancêtre le bloc conteneur du lecteur
            # « fixed » : ancêtre plein écran à l'origine (le lecteur couvre toujours) -> seul l'empilement le trahit
            {"name": "transform_ancestor", "html": P % ("", AD % "10"), "after": FULLV + R("transform", "translateZ(0)")},
            {"name": "contain_ancestor", "html": P % ("", AD % "10"), "after": FULLV + R("contain", "paint")},
            {"name": "container_type_ancestor", "html": P % ("", AD % "10"),
             "after": FULLV + R("container-type", "size")},
            {"name": "backdrop_ancestor", "html": P % ("", AD % "10"), "after": FULLV + R("backdrop-filter", "blur(1px)")},
            {"name": "perspective_ancestor", "html": P % ("", AD % "10"), "after": FULLV + R("perspective", "100px")},
            {"name": "transform_ancestor_offset", "html": P % ("", AD % "10"), "after": R("transform", "translateZ(0)")},
            {"name": "translucent_ancestor", "html": P % ("#wrap{opacity:.5}", ""), "after": R("opacity", "0.5")},
            {"name": "filtered_ancestor", "html": P % ("#wrap{filter:blur(2px)}", ""), "after": R("filter", "blur(2px)")},
            {"name": "clipped_ancestor", "html": P % ("", ""), "after": R("clip-path", "inset(0 0 50% 0)")},
            {"name": "blended_ancestor", "html": P % ("", AD % "10"), "after": R("mix-blend-mode", "multiply")},
            {"name": "zero_size_zmax_offscreen_child", "html": P % ("", self.ZERO.replace(
                "width:1920px;height:300px;background:red", "position:fixed;left:0;bottom:0;width:600px;height:200px;"
                "background:red"))},
            {"name": "zero_size_zmax_text_only", "html": P % ("", '<div style="position:fixed;left:40px;top:40px;'
                                                                   'width:0;height:0;z-index:2147483647;'
                                                                   'pointer-events:none;font-size:80px;color:red;'
                                                                   'white-space:nowrap">PUBLICITÉ</div>')},
            {"name": "empty_zero_size_zmax", "html": P % ("", '<div style="position:fixed;left:0;top:0;width:0;'
                                                               'height:0;z-index:2147483647"></div>')},
        ]

    def run_cases(self):
        import shutil as _sh
        import subprocess
        from tests.test_service import find_chromium
        mod = os.environ.get("OPENTV_TEST_PLAYWRIGHT")
        node = _sh.which("node")
        if not (mod and node and find_chromium()):
            self.skipTest("OPENTV_TEST_PLAYWRIGHT=<module playwright>, node et Chromium requis")
        d = tempfile.mkdtemp(prefix="otvas", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        cfgp = os.path.join(d, "cfg.json")
        with open(cfgp, "w") as f:
            json.dump({"chromium": find_chromium(), "probe": assist.PROBE_JS, "maximize": assist.MAXIMIZE_JS,
                       "check": assist.CHECK_JS, "fast": assist.CHECK_FAST_JS, "cases": self.cases()}, f)
        p = subprocess.run([node, os.path.join(os.path.dirname(__file__), "e2e_assist.js"), mod, cfgp],
                           capture_output=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        return json.loads(p.stdout.decode().strip().splitlines()[-1])

    def test_overlays_stacking_contexts_and_pixels(self):
        res = self.run_cases()
        self.assertEqual(set(res), set(c["name"] for c in self.cases()))
        for name, r in res.items():
            self.assertEqual((r["probe"], r["max"]), ("found", "ok"), name)
            # invariant central, pour TOUS les cas : « ready » => chaque pixel échantillonné est le lecteur
            if r["check"] == "ready":
                self.assertEqual(r["px"]["green"], r["px"]["total"], (name, r))
                self.assertEqual(r["fast"], "ready", name)
        ready = ("clean", "hidden_overlay", "invisible_overlay", "low_z_overlay", "empty_zero_size_zmax",
                 # a-c du relecteur : l'ancêtre est neutralisé par MAXIMIZE, le calque z=10 passe réellement DESSOUS
                 "a_stacking_ctx_ancestor_z1", "b_isolation_ancestor", "c_willchange_ancestor")
        for name in ready:
            self.assertEqual(res[name]["check"], "ready", (name, res[name]))
        blocked = {"pe_none_overlay": "overlay", "pseudo_overlay": "overlay", "shadow_overlay": "overlay",
                   "popover_top_layer": "top_layer", "huge_dom": "too_many",
                   "d_zero_size_zmax_container": "overlay", "zero_size_zmax_offscreen_child": "overlay",
                   "zero_size_zmax_text_only": "overlay",
                   "a_reasserted_z1": "stacking", "b_reasserted_isolation": "stacking",
                   "c_reasserted_willchange": "stacking", "flex_item_z": "stacking", "grid_item_z": "stacking",
                   "contain_ancestor": "stacking", "container_type_ancestor": "stacking",
                   "backdrop_ancestor": "stacking", "perspective_ancestor": "stacking", "transform_ancestor": "stacking",
                   "translucent_ancestor": "translucent", "clipped_ancestor": "translucent",
                   "blended_ancestor": "translucent"}
        for name, why in blocked.items():
            self.assertEqual((res[name]["check"], res[name]["why"]), ("obstructed", why), (name, res[name]))
        hows = {"a_reasserted_z1": "z", "b_reasserted_isolation": "isolation", "c_reasserted_willchange": "will_change",
                "flex_item_z": "flex_grid_z", "grid_item_z": "flex_grid_z", "contain_ancestor": "contain",
                "container_type_ancestor": "container", "backdrop_ancestor": "filter",
                "perspective_ancestor": "transform", "transform_ancestor": "transform"}
        for name, how in hows.items():
            self.assertEqual(res[name]["how"], how, (name, res[name]))
        # un transform/filter d'ancêtre devient le bloc conteneur du lecteur « fixed » : refusé dès la couverture,
        # sinon par le contexte d'empilement
        for name in ("transform_ancestor_offset", "filtered_ancestor"):
            self.assertEqual(res[name]["check"], "not_covering", (name, res[name]))
        # contrôle rapide de la lecture en cours : mêmes refus pour l'empilement et la transparence (sans le DOM)
        for name in ("a_reasserted_z1", "flex_item_z", "translucent_ancestor", "popover_top_layer"):
            self.assertEqual(res[name]["fast"], "obstructed", name)
        # les obstructions réelles sont PEINTES au-dessus du lecteur (pixels non verts) alors que la grille
        # elementFromPoint seule les laissait passer : la faille est réelle et désormais refusée
        for name in ("pe_none_overlay", "pseudo_overlay", "shadow_overlay", "d_zero_size_zmax_container",
                     "zero_size_zmax_offscreen_child", "zero_size_zmax_text_only", "a_reasserted_z1",
                     "b_reasserted_isolation", "c_reasserted_willchange", "flex_item_z", "grid_item_z",
                     "contain_ancestor", "transform_ancestor", "backdrop_ancestor", "perspective_ancestor",
                     "translucent_ancestor"):
            self.assertLess(res[name]["px"]["green"], res[name]["px"]["total"], (name, res[name]))
            self.assertEqual(res[name]["grid"], "ready", name)
        # container-type : Chromium ne peint pas toujours le calque au-dessus (pixels verts), refusé quand même
        # (fail-closed : propriété de confinement sur un ancêtre)
        self.assertEqual(res["container_type_ancestor"]["check"], "obstructed")


class TestRealChromiumSession(unittest.TestCase):
    """Séance playerd complète contre un vrai Chromium (headless shell, vrai tube CDP) : la fiche est révélée, le clic
    unique déclenche une fenêtre surgissante (geste utilisateur réel), qui est mise en pause par l'auto-attachement,
    fermée sans jamais exécuter son script, et la séance continue ; puis fermeture complète."""

    def test_real_popup_is_paused_closed_and_never_runs(self):
        import http.server
        from tests.test_service import find_chromium
        hs = find_chromium()
        if not hs:
            self.skipTest("Chromium headless absent")
        d = tempfile.mkdtemp(prefix="otvrc", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        wrapper = os.path.join(d, "chromium")
        with open(wrapper, "w") as f:   # mêmes flags du lecteur, sauf l'affichage Wayland/kiosque (headless ici)
            f.write('#!/bin/bash\nargs=()\nfor a in "$@"; do case "$a" in --ozone-platform=*|--kiosk) ;; '
                    '*) args+=("$a") ;; esac; done\nexec %s --no-first-run "${args[@]}"\n' % json.dumps(hs))
        os.chmod(wrapper, 0o755)
        hits = []
        page = ('<!doctype html><html><body style="margin:0"><header style="height:100px;background:#c00">Site'
                '</header><iframe style="width:1280px;height:720px;border:0" srcdoc="&lt;body style=&quot;margin:0;'
                'height:100vh&quot;&gt;&lt;script&gt;addEventListener(&quot;click&quot;,function(){'
                'window.open(&quot;/popup&quot;,&quot;_blank&quot;);});&lt;/script&gt;lecteur&lt;/body&gt;">'
                '</iframe></body></html>').encode()
        popup = b'<!doctype html><script>fetch("/ran")</script>pub'

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = page if self.path == "/" else popup if self.path == "/popup" else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        origin = "http://127.0.0.1:%d" % srv.server_address[1]
        logs = []
        run_dir = os.path.join(d, "run")
        sess = playerd.Session(playerd.new_sid(), origin + "/", origin, "Film", assist_s=15, settle_s=0.3, max_s=600,
                               env={"LANG": "C"}, run_dir=run_dir, chromium=wrapper, keys=False, private_ok=True,
                               log=lambda ev, **f: logs.append(dict(f, event=ev)))
        self.addCleanup(sess.close, "test")
        sess.start()
        end = time.monotonic() + 40
        while time.monotonic() < end and not (sess.clicked and sess.popups) and not sess.ended.is_set():
            time.sleep(0.05)
        self.assertFalse(sess.ended.is_set(), (sess.status(), logs))
        self.assertEqual(sess.state, "revealed")
        self.assertTrue(sess.clicked)
        self.assertTrue(wait_for(lambda: sess.popups >= 1, 10), (sess.status(), logs))
        time.sleep(1.5)
        infos = sess.cdp.send("Target.getTargets")["targetInfos"]
        pages = [t for t in infos if t.get("type") == "page"]
        self.assertEqual([t["targetId"] for t in pages], [sess.tid])     # seule la fiche révélée reste
        # mesuré sur Chromium réel : la surgissante en pause ne demande même pas sa page (aucun contenu à peindre :
        # au pire un onglet vide) et son script ne s'exécute jamais
        self.assertNotIn("/popup", hits)
        self.assertNotIn("/ran", hits)
        self.assertEqual(hits.count("/"), 1)     # la fiche n'a été chargée qu'une fois (aucune navigation parasite)
        self.assertEqual(sess.state, "revealed")
        self.assertTrue(sess.close("back"))
        st = sess.status()
        self.assertEqual((st["state"], st["reason"], st["gone"]), ("ended", "back", True))
        self.assertEqual(os.listdir(run_dir), [])
        no_url(self, logs)
