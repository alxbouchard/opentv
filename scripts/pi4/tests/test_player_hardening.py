"""« Regarder » — régressions de la revue finale (déterministes) :

1. course fermeture / acquisition des ressources (aide des touches, profil, Chromium, CDP) : aucun orphelin ;
2. close() jamais bloqué par un clic qui ne répond plus (< 2 s) ;
3. aide des touches : nombre RÉEL de périphériques capables ouverts, EACCES / aucun périphérique => pas de révélation ;
4. KeyWatcher : le thread seul ferme ses descripteurs (stop() ne fait que signaler/attendre) ;
5. cadence du contrôle après révélation (rapide à chaque tick, balayage complet espacé ou sur mutation) ;
6. cibles et navigations : boucle locale, RFC 1918, nom local, la boîte elle-même, navigation du cadre principal hors
   de l'origine validée — refusées ;
7. démotion pendant une séance => fermeture ;
8. unité du lecteur : ptrace/@debug interdits (EPERM), périphériques précis (décodage conservé, CEC/input/uinput
   refusés), réseau local de la boîte refusé ; politique Chromium du lecteur.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tests import _common  # noqa: F401  (chemins)
from tests import test_player as tp
from opentv_pi import evkeys, playerd, playerkeys

URL, ORIGIN = tp.URL, tp.ORIGIN


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Watch:
    """Faux abonnement à l'aide des touches : `start` peut être retenu par un crochet du test."""
    made = []

    def __init__(self, on_key, hook=None, devices=1, **kw):
        self.on_key, self.hook, self.devices = on_key, hook, devices
        self.stop_ev, self.armed = threading.Event(), threading.Event()
        self.stopped = False
        Watch.made.append(self)

    def start(self):
        if self.hook:
            self.hook()
        self.armed.set()
        return self

    def stop(self):
        self.stopped = True
        self.stop_ev.set()


class SessionKit:
    """Session playerd avec vrai groupe de processus « sleep » et faux CDP ; crochets aux points d'acquisition."""

    def __init__(self, tc, popen_hook=None, watch_hook=None, **kw):
        self.tc, self.dir = tc, tempfile.mkdtemp(prefix="otvr")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.procs, self.cdps, self.spawned = [], [], 0
        Watch.made = []
        check_states, cdp_kw = kw.pop("check_states", None), kw.pop("cdp_kw", {})

        def popen(argv, env, fds):
            self.spawned += 1
            if popen_hook:
                popen_hook(self)
            p = subprocess.Popen(["sleep", "60"], start_new_session=True, pass_fds=fds,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(p)
            tc.addCleanup(lambda: p.poll() is None and p.kill())
            return p

        def cdp_factory(w, r, on_event=None):
            os.close(w)
            os.close(r)
            c = tp.FakeCDP("found", on_event, check_states, **cdp_kw)
            self.cdps.append(c)
            return c

        params = dict(assist_s=1.0, settle_s=0.05, max_s=600, env={"WAYLAND_DISPLAY": "w"}, run_dir=self.dir,
                      popen=popen, cdp_factory=cdp_factory, keys=True,
                      key_watcher=lambda cb, **k: Watch(cb, hook=watch_hook and (lambda: watch_hook(self))),
                      resolve=lambda h: ["93.184.216.34"], local_addrs=lambda: set(), keys_wait_s=0.5)
        params.update(kw)
        self.s = playerd.Session(playerd.new_sid(), URL, ORIGIN, "T", **params)

    def nothing_left(self):
        t = self.tc
        t.assertTrue(all(p.poll() is not None for p in self.procs), "navigateur orphelin")
        t.assertEqual(os.listdir(self.dir), [], "profil orphelin")
        t.assertTrue(all(w.stopped for w in Watch.made), "abonnement aux touches orphelin")
        t.assertTrue(all(c.closed for c in self.cdps), "CDP orphelin")


class TestCloseVsAcquisitionRaces(unittest.TestCase):
    def test_close_while_spawning_kills_the_browser_before_reporting_gone(self):
        """close() arrive PENDANT le lancement (entre le contrôle d'annulation et l'existence du processus) : le
        démontage attend la fin de l'acquisition puis tue le navigateur ; « gone » n'est vrai qu'ensuite."""
        box = {}

        def hook(kit):
            box["t"] = threading.Thread(target=lambda: box.setdefault("ret", kit.s.close("back")))
            box["t"].start()
            self.assertTrue(wait_for(lambda: kit.s.cancel.is_set(), 2))   # close() a bien commencé
            box["ended_during_spawn"] = kit.s.ended.wait(0.2)             # le démontage ne peut pas passer devant
        k = SessionKit(self, popen_hook=hook)
        k.s.start()
        self.assertTrue(k.s.ended.wait(5))
        box["t"].join(5)
        self.assertFalse(box["ended_during_spawn"])
        self.assertTrue(box["ret"])
        st = k.s.status()
        self.assertEqual((st["state"], st["reason"], st["gone"]), ("ended", "back", True))
        self.assertEqual(k.spawned, 1)
        k.nothing_left()

    def test_reviewer_probe_close_waits_for_end_inside_spawn(self):
        """Sonde du relecteur : le crochet attend « ended » à l'intérieur du lancement (borné) puis lance quand
        même : aucun navigateur, profil ni abonnement ne survit."""
        def hook(kit):
            threading.Thread(target=kit.s.close, args=("back",)).start()
            kit.s.ended.wait(0.3)
        k = SessionKit(self, popen_hook=hook)
        k.s.start()
        self.assertTrue(k.s.ended.wait(5))
        time.sleep(0.2)
        self.assertTrue(k.s.status()["gone"])
        k.nothing_left()

    def test_close_while_key_subscription_starts_never_leaks_it(self):
        """Sonde du relecteur (watcher) : close() pendant la création de l'abonnement aux touches -> l'abonnement est
        quand même arrêté, aucun navigateur n'est lancé."""
        def hook(kit):
            threading.Thread(target=kit.s.close, args=("back",)).start()
            self.assertTrue(wait_for(lambda: kit.s.cancel.is_set(), 2))
        k = SessionKit(self, watch_hook=hook)
        k.s.start()
        self.assertTrue(k.s.ended.wait(5))
        self.assertEqual(len(Watch.made), 1)
        self.assertTrue(Watch.made[0].stopped)
        self.assertEqual(k.spawned, 0)
        k.nothing_left()

    def test_close_before_start_creates_nothing(self):
        k = SessionKit(self)
        self.assertTrue(k.s.close("back"))
        k.s.start()
        k.s.thread.join(3)
        self.assertEqual((k.spawned, len(Watch.made), os.listdir(k.dir)), (0, 0, []))
        self.assertEqual(k.s.status()["reason"], "back")

    def test_close_at_every_acquisition_point_leaves_nothing(self):
        """close() concurrent à chaque étape (abonnement, profil, lancement, CDP) sur 20 itérations chacune."""
        for point in ("watch", "spawn"):
            for i in range(10):
                kw = {}
                if point == "watch":
                    kw["watch_hook"] = lambda kit: threading.Thread(target=kit.s.close, args=("back",)).start()
                else:
                    kw["popen_hook"] = lambda kit: threading.Thread(target=kit.s.close, args=("back",)).start()
                k = SessionKit(self, **kw)
                k.s.start()
                self.assertTrue(k.s.ended.wait(5), (point, i))
                k.s.thread.join(3)
                k.nothing_left()


class TestClosePromptDuringHungClick(unittest.TestCase):
    def test_close_returns_under_two_seconds_while_mouse_dispatch_hangs(self):
        k = SessionKit(self, keys=False, cdp_kw={"hang_mouse": True})
        k.s.start()
        self.assertTrue(wait_for(lambda: k.cdps and k.cdps[0].clicks == 1, 8), k.s.status())
        t0 = time.monotonic()
        self.assertTrue(k.s.close("back"))
        dt = time.monotonic() - t0
        self.assertLess(dt, 2.0)
        st = k.s.status()
        self.assertEqual((st["state"], st["reason"], st["gone"]), ("ended", "back", True))
        mouse = [m for m, prm, _ in k.cdps[0].log if m == "Input.dispatchMouseEvent" and prm["type"] == "mouseReleased"]
        self.assertEqual(mouse, [])   # aucun relâchement envoyé après Retour
        k.nothing_left()


class TestKeyDeviceCount(unittest.TestCase):
    def ev_dir(self):
        d = tempfile.mkdtemp(prefix="otvev", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        for n in ("event0", "event1", "event2"):
            open(os.path.join(d, n), "w").close()
        return d

    def test_count_capable_eacces_and_no_capable_device(self):
        d = self.ev_dir()
        pat = os.path.join(d, "event*")

        def eacces(p):
            raise PermissionError(13, "EACCES")
        self.assertEqual(evkeys.count_capable(pat, opener=eacces), 0)
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.assertEqual(evkeys.count_capable(pat, opener=lambda p: os.dup(r), has_key=lambda fd: False), 0)
        self.assertEqual(evkeys.count_capable(pat, opener=lambda p: os.dup(r),
                                              has_key=lambda fd: True), 3)

    def test_f12_only_device_is_watched_but_never_counts_as_close_capable(self):
        old = evkeys.fcntl.ioctl

        def only_f12(fd, request, buf, mutate):
            buf[evkeys.KEY_F12 // 8] |= 1 << (evkeys.KEY_F12 % 8)
            return 0

        evkeys.fcntl.ioctl = only_f12
        self.addCleanup(setattr, evkeys.fcntl, "ioctl", old)
        self.assertTrue(evkeys.has_watched_key(123))
        self.assertFalse(evkeys.has_close_key(123))

    def test_watcher_reports_actual_open_count_after_scan(self):
        d = self.ev_dir()
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        seen = []
        opened = {"event0": True, "event1": False, "event2": True}

        def opener(p):
            if not opened[os.path.basename(p)]:
                raise PermissionError(13, "EACCES")
            return os.dup(r)
        kw = evkeys.KeyWatcher(lambda why: None, pattern=os.path.join(d, "event*"), rescan_s=0.05, opener=opener,
                               has_key=lambda fd: True, on_devices=seen.append).start()
        self.assertTrue(kw.ready.wait(2))
        self.assertEqual(kw.count, 2)            # EACCES jamais compté
        os.unlink(os.path.join(d, "event2"))     # retiré du balayage : le fd ouvert reste compté tant qu'il vit
        self.assertTrue(kw.stop())
        self.assertEqual(kw.count, 0)
        self.assertEqual(seen[0], 2)

    def serve(self, factory):
        d = tempfile.mkdtemp(prefix="otvk", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "k.sock")
        hub = playerkeys.Hub(watcher_factory=factory, scan_wait_s=1.0,
                             probe=lambda: evkeys.count_capable(os.path.join(d, "event*"), opener=self.eacces))
        srv = playerkeys.Server(path, hub, {os.getuid()})
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(hub.close)
        self.addCleanup(srv.stopping.set)
        return path, hub, d

    @staticmethod
    def eacces(p):
        raise PermissionError(13, "EACCES")

    def test_eacces_everywhere_means_no_device_no_arming_no_reveal(self):
        made = []

        def factory(on_key, on_devices=None):
            w = evkeys.KeyWatcher(on_key, pattern=os.path.join(dd[0], "event*"), rescan_s=0.05,
                                  opener=self.eacces, on_devices=on_devices)
            made.append(w)
            return w
        dd = [None]
        path, hub, d = self.serve(factory)
        dd[0] = d
        for n in ("event0", "event3"):
            open(os.path.join(d, n), "w").close()
        self.assertEqual(playerkeys.ping_info(path), {"armed": False, "devices": 0})
        self.assertFalse(playerkeys.ping(path))          # le contrôleur ne propose pas « Regarder »
        kc = playerkeys.KeyClient(lambda w: None, path=path, retry_s=0.05).start()
        self.addCleanup(kc.stop)
        self.assertFalse(kc.armed.wait(0.6))
        self.assertEqual((kc.refused, kc.devices), ("no_device", 0))
        self.assertTrue(wait_for(lambda: not hub.armed()))
        self.assertTrue(made and all(not w.thread.is_alive() for w in made[:-1]))
        # séance : jamais révélée sans télécommande physique capable
        k = SessionKit(self, key_watcher=lambda cb, **kw: playerkeys.KeyClient(cb, path=path, retry_s=0.05),
                       keys_wait_s=0.5)
        k.s.start()
        self.assertTrue(k.s.ended.wait(8))
        st = k.s.status()
        self.assertEqual((st["outcome"], st["reason"]), ("error", "keys_unavailable"))
        self.assertNotIn("Target.activateTarget", k.cdps[0].calls)
        self.assertEqual(k.cdps[0].clicks, 0)

    def test_armed_link_with_zero_devices_never_reveals(self):
        k = SessionKit(self, key_watcher=lambda cb, **kw: tp.TestKeysRequired.Keys(cb, devices=0))
        k.s.start()
        self.assertTrue(k.s.ended.wait(8))
        self.assertEqual(k.s.status()["reason"], "keys_unavailable")
        self.assertNotIn("Target.activateTarget", k.cdps[0].calls)

    def test_device_count_updates_flow_to_client_and_unplug_disarms(self):
        made = []

        class W:
            def __init__(self, on_key, on_devices=None):
                self.on_key, self.on_devices = on_key, on_devices
                self.ready, self.count, self.stopped = threading.Event(), 1, False
                self.ready.set()
                made.append(self)

            def start(self):
                return self

            def stop(self):
                self.stopped = True
        path, hub, d = self.serve(W)
        kc = playerkeys.KeyClient(lambda w: None, path=path, retry_s=0.05).start()
        self.addCleanup(kc.stop)
        self.assertTrue(kc.armed.wait(3))
        self.assertEqual(kc.devices, 1)
        made[0].count = 0
        made[0].on_devices(0)                     # télécommande débranchée
        self.assertTrue(wait_for(lambda: not kc.armed.is_set() and kc.devices == 0))
        made[0].count = 2
        made[0].on_devices(2)
        self.assertTrue(wait_for(lambda: kc.armed.is_set() and kc.devices == 2))
        self.assertEqual(playerkeys.ping_info(path), {"armed": True, "devices": 2})


class TestKeyWatcherOwnsFds(unittest.TestCase):
    def test_only_the_watcher_thread_closes_its_descriptors(self):
        d = tempfile.mkdtemp(prefix="otvow", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        open(os.path.join(d, "event0"), "w").close()
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        mine, closers = [], []
        real_close = os.close

        def opener(p):
            fd = os.dup(r)
            mine.append(fd)
            return fd

        def spy(fd):
            if fd in mine:
                closers.append(threading.current_thread().name)
            return real_close(fd)
        kw = evkeys.KeyWatcher(lambda why: None, pattern=os.path.join(d, "event*"), rescan_s=0.05, opener=opener,
                               has_key=lambda fd: True)
        evkeys.os.close = spy
        try:
            kw.start()
            self.assertTrue(kw.ready.wait(2))
            time.sleep(0.15)   # dans select()
            self.assertTrue(kw.stop())
        finally:
            evkeys.os.close = real_close
        self.assertEqual(len(mine), 1)
        self.assertEqual(closers, ["close-keys"])          # jamais le thread qui appelle stop()
        with self.assertRaises(OSError):
            os.fstat(mine[0])

    def test_stop_never_closes_under_a_stuck_scan_and_thread_closes_later(self):
        d = tempfile.mkdtemp(prefix="otvst", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        for n in ("event0", "event1"):
            open(os.path.join(d, n), "w").close()
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        gate, opened = threading.Event(), []

        def opener(p):
            fd = os.dup(r)
            opened.append(fd)
            return fd

        def has_key(fd):
            if len(opened) == 2:
                gate.wait(5)   # balayage bloqué sur le 2e périphérique
            return True
        kw = evkeys.KeyWatcher(lambda why: None, pattern=os.path.join(d, "event*"), rescan_s=0.05, opener=opener,
                               has_key=has_key).start()
        self.assertTrue(wait_for(lambda: len(opened) == 2))
        self.assertFalse(kw.stop(timeout=0.2))             # signalé, pas encore terminé
        os.fstat(opened[0])                                # toujours ouvert : stop() n'a rien fermé lui-même
        gate.set()
        kw.thread.join(3)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)


class TestMonitorCadence(unittest.TestCase):
    def kit(self, **kw):
        k = SessionKit(self, keys=False, sweep_s=0.05, full_min_s=0.5, full_max_s=1.0, **kw)
        k.s.start()
        self.assertTrue(wait_for(lambda: k.s.state == "revealed", 8))
        self.assertTrue(wait_for(lambda: k.s.clicked, 3))
        return k

    def test_fast_every_tick_full_scan_spaced_when_dom_is_quiet(self):
        k = self.kit()
        c = k.cdps[0]
        f0, g0, t0 = c.n_full, c.n_fast, time.monotonic()
        time.sleep(2.1)
        fast, full, dt = c.n_fast - g0, c.n_full - f0, time.monotonic() - t0
        # 25 points + empilement à CHAQUE tick (≥ 8 même avec la granularité des minuteries macOS ~0,17 s)
        self.assertGreaterEqual(fast, 8)
        self.assertLessEqual(full, int(dt / 1.0) + 1)   # balayage complet ≈ toutes les full_max_s (1 s)
        self.assertGreaterEqual(full, 1)
        self.assertGreaterEqual(fast, 3 * full)
        self.assertEqual(k.s.state, "revealed")
        k.s.close("back")

    def test_dom_mutations_trigger_full_scan_at_most_every_min_interval(self):
        k = self.kit(cdp_kw={"dirty": True})
        c = k.cdps[0]
        f0, t0 = c.n_full, time.monotonic()
        time.sleep(2.1)
        full, dt = c.n_full - f0, time.monotonic() - t0
        self.assertLessEqual(full, int(dt / 0.5) + 1)   # au plus toutes les full_min_s (0,5 s)
        self.assertGreaterEqual(full, 2)
        k.s.close("back")

    def test_overlay_only_visible_to_full_scan_ends_session_within_max_interval(self):
        k = SessionKit(self, keys=False, sweep_s=0.05, full_min_s=0.5, full_max_s=1.0,
                       check_states=["ready", "ready"] + ["obstructed"] * 20, cdp_kw={"fast_states": []})
        k.s.start()
        self.assertTrue(wait_for(lambda: k.s.state == "revealed", 8))
        t0 = time.monotonic()
        self.assertTrue(k.s.ended.wait(4))
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual((k.s.status()["outcome"], k.s.status()["reason"]), ("obstructed", "player_lost"))


class TestTargetsAndNavigation(unittest.TestCase):
    def test_host_blocked(self):
        for h in ("localhost", "LOCALHOST.", "tv.localhost", "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.4",
                  "192.168.2.14", "169.254.1.1", "100.64.0.1", "0.0.0.0", "[::1]", "::1", "fe80::1", "fc00::1",
                  "::ffff:192.168.2.14", "224.0.0.251", "opentv.local", "nas.lan", "x.home.arpa", "box.internal",
                  "printer", "2130706433", "0x7f.1", "0177.0.0.1", ""):
            self.assertTrue(playerd.host_blocked(h), h)
        for h in ("films-mirror.net", "93.184.216.34", "cdn.example.com", "2606:4700::1111"):
            self.assertFalse(playerd.host_blocked(h), h)

    def test_controller_refuses_local_private_and_self_targets(self):
        c, made = tp.TestController.ctl(self)
        for url in ("https://127.0.0.1/x", "https://localhost/x", "https://192.168.2.14/x", "https://10.1.2.3/x",
                    "https://[::1]/x", "https://opentv.local/x", "https://2130706433/x", "https://0x7f.1/x",
                    "https://films-mirror.net:8443/x"):
            u = url.split("/x")[0]
            r = c.handle({"op": "open", "sid": playerd.new_sid(), "url": url, "origin": u})
            self.assertEqual(r.get("error"), "bad_target", url)
        self.assertEqual(made, [])

    def test_session_refuses_names_resolving_to_private_or_self_before_spawning(self):
        for addrs, local, why in ((["192.168.2.14"], set(), "private_target"), (["127.0.0.1"], set(), "private_target"),
                                  (["93.184.216.34", "10.0.0.5"], set(), "private_target"),
                                  (["::ffff:10.0.0.5"], set(), "private_target"),
                                  (["93.184.216.34"], {"93.184.216.34"}, "private_target"), ([], set(), "dns")):
            k = SessionKit(self, keys=False, resolve=lambda h, a=addrs: list(a), local_addrs=lambda l=local: set(l))
            k.s.start()
            self.assertTrue(k.s.ended.wait(5))
            st = k.s.status()
            self.assertEqual((st["outcome"], st["reason"]), ("error", why), addrs)
            self.assertEqual(k.spawned, 0)
            tp.no_url(self, st)

    def test_local_addresses_parse(self):
        d = tempfile.mkdtemp(prefix="otvproc", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "net"))
        with open(os.path.join(d, "net", "fib_trie"), "w") as f:
            f.write("Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 192.168.2.40\n        /32 host LOCAL\n"
                    "     |-- 192.168.2.255\n        /32 link BROADCAST\n     |-- 203.0.113.9\n        /32 host LOCAL\n")
        with open(os.path.join(d, "net", "if_inet6"), "w") as f:
            f.write("26064700000000000000000000001111 02 40 00 80 wlan0\n"
                    "00000000000000000000000000000001 01 80 10 80 lo\n")
        got = playerd.local_addresses(d)
        self.assertEqual(got, {"192.168.2.40", "203.0.113.9", "2606:4700::1111", "::1"})
        self.assertTrue(playerd.addr_blocked("2606:4700::1111", got))   # la boîte, même publique

    def paused(self, k, url, frame):
        k.cdps[0].on_event({"method": "Fetch.requestPaused", "sessionId": "S1",
                            "params": {"requestId": "R-%s" % url[-6:], "frameId": frame, "resourceType": "Document",
                                       "request": {"url": url}}})

    def fetch_calls(self, k):
        return [(m, prm.get("requestId")) for m, prm, _ in k.cdps[0].log if m.startswith("Fetch.")]

    def test_fetch_guard_is_armed_before_first_navigation(self):
        k = SessionKit(self, keys=False)
        k.s.start()
        self.assertTrue(wait_for(lambda: k.s.state == "revealed", 8))
        c = k.cdps[0]
        en = c.index("Fetch.enable", session="S1")
        self.assertIsNotNone(en)
        self.assertLess(en, c.index("Runtime.runIfWaitingForDebugger", session="S1"))
        self.assertEqual(c.log[en][1]["patterns"], [{"urlPattern": "*", "resourceType": "Document",
                                                    "requestStage": "Request"}])
        k.s.close("back")

    def test_main_frame_navigation_guard_before_reveal(self):
        k = SessionKit(self, keys=False, settle_s=2.0)
        k.s.start()
        self.assertTrue(wait_for(lambda: k.cdps and k.cdps[0].index("Page.createIsolatedWorld") is not None, 8))
        self.paused(k, ORIGIN + "/abc12/b/tok/42", "F1")              # même origine : poursuivie
        self.paused(k, "https://ads.example/frame000001", "F7")         # sous-cadre : poursuivi, jamais jugé
        self.assertTrue(wait_for(lambda: len(self.fetch_calls(k)) >= 3))
        self.assertFalse(k.s.ended.is_set())
        self.paused(k, "https://192.168.2.14:8096/cmd", "F1")         # réseau local, autre origine : bloquée
        self.assertTrue(k.s.ended.wait(5))
        calls = self.fetch_calls(k)
        self.assertIn(("Fetch.continueRequest", "R-tok/42"), calls)
        self.assertIn(("Fetch.continueRequest", "R-000001"), calls)
        self.assertIn(("Fetch.failRequest", "R-96/cmd"), calls)
        st = k.s.status()
        self.assertEqual((st["outcome"], st["gone"]), ("navigated", True))
        self.assertNotIn("Target.activateTarget", k.cdps[0].calls)
        tp.no_url(self, st)

    def test_off_origin_and_after_reveal_main_frame_navigations_blocked(self):
        for url, reveal in (("https://evil.example/x00001", False), ("http://films-mirror.net/x00002", False),
                            ("https://u:p@films-mirror.net/x00003", False), (ORIGIN + "/other00004", True)):
            k = SessionKit(self, keys=False, settle_s=0.05 if reveal else 2.0)
            k.s.start()
            if reveal:
                self.assertTrue(wait_for(lambda: k.s.state == "revealed", 8))
            else:
                self.assertTrue(wait_for(lambda: k.cdps and k.cdps[0].index("Page.createIsolatedWorld") is not None))
            self.paused(k, url, "F1")
            self.assertTrue(k.s.ended.wait(5), url)
            self.assertIn("Fetch.failRequest", [m for m, _ in self.fetch_calls(k)], url)
            self.assertEqual(k.s.status()["outcome"], "navigated", url)
            k.nothing_left()


class TestDemotionDuringSession(unittest.TestCase):
    def test_demotion_closes_running_session(self):
        box = [True]
        k = SessionKit(self, keys=False, gate=lambda: box[0], gate_every_s=0.1, sweep_s=0.05)
        k.s.start()
        self.assertTrue(wait_for(lambda: k.s.state == "revealed", 8))
        box[0] = False
        self.assertTrue(k.s.ended.wait(3))
        st = k.s.status()
        self.assertEqual((st["outcome"], st["reason"], st["gone"]), ("revealed", "demoted", True))
        k.nothing_left()

    def test_demotion_during_preparation_never_reveals(self):
        box = [True]
        k = SessionKit(self, keys=False, gate=lambda: box[0], settle_s=0.6)
        k.s.start()
        self.assertTrue(wait_for(lambda: k.cdps and k.cdps[0].index("Page.createIsolatedWorld") is not None))
        box[0] = False
        self.assertTrue(k.s.ended.wait(5))
        self.assertEqual(k.s.status()["reason"], "demoted")
        self.assertNotIn("Target.activateTarget", k.cdps[0].calls)

    def test_controller_passes_its_gate_to_sessions(self):
        c, made = tp.TestController.ctl(self)
        c.handle({"op": "open", "sid": playerd.new_sid(), "url": URL, "origin": ORIGIN})
        self.assertIs(made[0].kw["gate"].__func__, playerd.Controller._gate)


def unit_entries(name):
    """[(clé, valeur)] de la section [Service] (clés répétées conservées, comme systemd)."""
    out, sect = [], None
    with open(os.path.join(_common.PI4, "systemd", name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                sect = line
                continue
            if sect == "[Service]" and "=" in line:
                k, _, v = line.partition("=")
                out.append((k.strip(), v.strip()))
    return out


class TestPlayerUnitHardening(unittest.TestCase):
    def test_debug_syscalls_denied_with_eperm(self):
        e = unit_entries("opentv-player.service")
        filters = [v for k, v in e if k == "SystemCallFilter"]
        self.assertIn("~@debug", filters)
        self.assertFalse(any(not v.startswith("~") for v in filters))   # liste de refus seulement (décodage gardé)
        self.assertEqual(dict(e).get("SystemCallErrorNumber"), "EPERM")
        self.assertEqual(dict(e).get("NoNewPrivileges"), "yes")

    def test_precise_device_policy_keeps_decode_and_display_but_no_cec_input_uinput(self):
        e = unit_entries("opentv-player.service")
        d = dict(e)
        self.assertEqual(d.get("DevicePolicy"), "closed")
        allow = {v.split()[0]: v.split()[1] for k, v in e if k == "DeviceAllow"}
        for need in ("char-drm", "char-video4linux", "char-media", "char-dma_heap", "char-alsa"):
            self.assertIn(need, allow, need)
        for bad in ("char-input", "char-cec", "char-misc", "char-hidraw", "char-vchiq", "char-tty", "block-"):
            self.assertFalse(any(k.startswith(bad) for k in allow), bad)
        inacc = " ".join(v for k, v in e if k == "InaccessiblePaths").split()
        for p in ("-/dev/input", "-/dev/uinput", "-/dev/cec0", "-/dev/cec1"):
            self.assertIn(p, inacc)
        self.assertNotIn("input", d.get("SupplementaryGroups", "").split())
        self.assertNotIn("PrivateDevices", d)   # DRM/V4L2 doivent rester visibles pour le décodage

    def test_player_cannot_reach_the_box_loopback_or_link_local(self):
        e = unit_entries("opentv-player.service")
        deny = " ".join(v for k, v in e if k == "IPAddressDeny").split()
        self.assertTrue({"localhost", "link-local", "multicast"} <= set(deny))
        allow = " ".join(v for k, v in e if k == "IPAddressAllow").split()
        self.assertTrue(all(a.startswith("127.0.0.5") and a.endswith("/32") for a in allow), allow)

    def test_systemd_analyze_verify_when_available(self):
        sa = shutil.which("systemd-analyze")
        if not sa:
            self.skipTest("systemd-analyze absent (macOS) : analyse par le parseur du test seulement")
        for u in ("opentv-player.service", "opentv-player-keys.service"):
            p = subprocess.run([sa, "verify", "--man=no", os.path.join(_common.PI4, "systemd", u)],
                               capture_output=True, timeout=60)
            self.assertNotIn(b"Unknown key", p.stderr, u)
            self.assertNotIn(b"Failed to parse", p.stderr, u)

    def test_player_policy_blocks_local_targets_and_insecure_schemes(self):
        with open(os.path.join(_common.PI4, "system", "chromium-policy-opentv-player.json")) as f:
            pol = json.load(f)
        for u in ("http://*", "localhost", "127.0.0.1", "[::1]", "0.0.0.0", "local", "lan", "home.arpa",
                  "devtools://*", "chrome://*", "file://*", "view-source:*"):
            self.assertIn(u, pol["URLBlocklist"], u)
        self.assertIs(pol["PrivateNetworkAccessRestrictionsEnabled"], True)
        self.assertIs(pol["LocalNetworkAccessRestrictionsEnabled"], True)
        self.assertEqual(pol["DeveloperToolsAvailability"], 1)


class TestRealChromiumNavigationGuard(unittest.TestCase):
    """Vrai Chromium : la fiche (origine A) navigue d'elle-même son cadre principal vers une autre origine (B, même
    serveur sous un autre nom) AVANT la révélation : la requête vers B n'atteint jamais le serveur, rien n'est révélé,
    la séance se termine « navigated » et tout est nettoyé."""

    def test_off_origin_main_frame_navigation_never_leaves(self):
        import http.server
        from tests.test_service import find_chromium
        hs = find_chromium()
        if not hs:
            self.skipTest("Chromium headless absent")
        d = tempfile.mkdtemp(prefix="otvnav", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        wrapper = os.path.join(d, "chromium")
        with open(wrapper, "w") as f:
            f.write('#!/bin/bash\nargs=()\nfor a in "$@"; do case "$a" in --ozone-platform=*|--kiosk) ;; '
                    '*) args+=("$a") ;; esac; done\nexec %s --no-first-run "${args[@]}"\n' % json.dumps(hs))
        os.chmod(wrapper, 0o755)
        hits = []
        srv_box = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.headers.get("Host", "").split(":")[0], self.path))
                port = srv_box["port"]
                if self.path == "/":
                    body = ('<!doctype html><script>setTimeout(function(){location.href='
                            '"http://localhost:%d/evil"},300)</script><p>fiche</p>' % port).encode()
                else:
                    body = b"evil"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = __import__("http.server").server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv_box["port"] = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        origin = "http://127.0.0.1:%d" % srv_box["port"]
        logs = []
        run_dir = os.path.join(d, "run")
        sess = playerd.Session(playerd.new_sid(), origin + "/", origin, "Film", assist_s=6, settle_s=0.3, max_s=600,
                               env={"LANG": "C"}, run_dir=run_dir, chromium=wrapper, keys=False, private_ok=True,
                               log=lambda ev, **f: logs.append(dict(f, event=ev)))
        self.addCleanup(sess.close, "test")
        sess.start()
        self.assertTrue(sess.ended.wait(30), (sess.status(), logs))
        st = sess.status()
        self.assertEqual((st["outcome"], st["gone"]), ("navigated", True), (st, logs))
        self.assertEqual(sess.clicks, 0)
        self.assertIsNone(sess.revealed_at)
        self.assertIn(("127.0.0.1", "/"), hits)
        self.assertFalse([h for h in hits if h[0] == "localhost"], hits)   # la requête hors origine n'est jamais partie
        self.assertGreaterEqual(sess.blocked_navs, 1)
        self.assertEqual(os.listdir(run_dir), [])
        tp.no_url(self, logs)


if __name__ == "__main__":
    unittest.main()
