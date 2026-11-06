"""X TV — sortie « appareil » par la touche Retour Samsung (KEY_EXIT 174) quand X est ouvert (0.7.7 hotfix).

Terrain : la touche Retour de la télécommande Samsung produit KEY_EXIT (174) sur le périphérique d'entrée CEC vc4-hdmi,
mais Chromium/Wayland ne délivre AUCUN keydown à la page : nav.js ne peut rien. Contrat vérifié ici :
- témoin « X ouvert » : écrit SEULEMENT par opentv.service (POST /api/tv/x/presence, portes TV complètes), effacé au
  chargement de /tv et au démarrage du service ; lecture refusée (fail-safe : aucune action) si lien, propriétaire
  inattendu, liens multiples, taille, JSON ou boot_id faux ;
- aide opentv-x-escape (compte opentv-keys, groupe input, sans réseau) : evdev armé SEULEMENT pendant que le témoin
  existe, périphériques vc4-hdmi SEULEMENT (jamais un clavier : la saisie privée d'Alex n'est jamais lue), KEY_EXIT
  appui seul (répétitions et relâchés ignorés), reconnexion, limite de débit, demande écrite atomiquement ;
- action root (opentv-x-escape-action.service, tirée par opentv-x-escape.path) : demande vérifiée (propriétaire,
  régulier, contenu exact) puis toujours retirée, témoin revérifié, limite de débit, puis UNE commande exacte
  `systemctl restart --no-block opentv-kiosk.service` (prouvé terrain : le kiosque repart sur /tv) ;
- /tv : sans témoin, KEY_EXIT n'est même pas lu ; aucune relance du kiosque.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests._common import PI4
from tests.test_service import make_app, Servers
from opentv_pi import config, evkeys, health, xcontext, xescape


def tmp(tc):
    d = tempfile.mkdtemp(prefix="otvxesc", dir="/tmp")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def ev(code, value, typ=evkeys.EV_KEY):
    return struct.pack(evkeys.EVENT_FMT, 0, 0, typ, code, value)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


UID = os.getuid()


def wait(pred, s=3.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class FakeDevices:
    """Périphériques evdev simulés par des tubes (nom EVIOCGNAME simulé) pour le VRAI KeyWatcher."""

    def __init__(self, tc):
        self.dir = tmp(tc)
        self.pipes = {}

    def add(self, name):
        r, w = os.pipe()
        os.set_blocking(r, False)
        p = os.path.join(self.dir, "event%d" % len(self.pipes))
        open(p, "w").close()
        self.pipes[p] = (r, w, name)
        return w

    def opener(self, p):
        if p not in self.pipes:
            raise OSError(2, "absent")
        return os.dup(self.pipes[p][0])

    def name_fn(self, fd):
        ino = os.fstat(fd).st_ino
        for r, w, n in self.pipes.values():
            if os.fstat(r).st_ino == ino:
                return n
        return None

    def factory(self, on_key, **kw):
        filt = xescape.make_device_filter(name_fn=self.name_fn, keys_fn=lambda fd, codes: True)
        return evkeys.KeyWatcher(on_key, pattern=os.path.join(self.dir, "event*"), rescan_s=0.05, opener=self.opener,
                                 has_key=filt, decoder_factory=kw["decoder_factory"])


class TestMarker(unittest.TestCase):
    def setUp(self):
        self.d = tmp(self)

    def test_set_read_clear(self):
        self.assertFalse(xescape.read_marker(self.d, UID))
        self.assertTrue(xescape.set_marker(self.d, True, boot_id="b1"))
        p = os.path.join(self.d, xescape.MARKER)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)    # lisible par l'aide (opentv-keys), écrit par opentv
        self.assertTrue(xescape.read_marker(self.d, UID, boot_id="b1"))
        self.assertFalse(xescape.read_marker(self.d, UID, boot_id="b2"))   # autre démarrage : jamais actif
        self.assertFalse(xescape.read_marker(self.d, UID + 1, boot_id="b1"))   # propriétaire inattendu
        self.assertTrue(xescape.set_marker(self.d, False))
        self.assertFalse(os.path.exists(p))
        self.assertTrue(xescape.set_marker(self.d, False))      # idempotent

    def test_fail_safe_on_tampering(self):
        p = os.path.join(self.d, xescape.MARKER)
        other = os.path.join(self.d, "other")
        with open(other, "w") as f:
            f.write(json.dumps({"v": 1, "boot_id": ""}))
        os.symlink(other, p)
        self.assertFalse(xescape.read_marker(self.d, UID))       # lien : refusé
        os.unlink(p)
        os.link(other, p)
        self.assertFalse(xescape.read_marker(self.d, UID))       # liens multiples
        os.unlink(p)
        for bad in ("", "{", "[]", json.dumps({"v": 2, "boot_id": ""}), "x" * 5000):
            with open(p, "w") as f:
                f.write(bad)
            self.assertFalse(xescape.read_marker(self.d, UID), bad[:20])
        os.unlink(p)
        os.mkdir(p)
        self.assertFalse(xescape.read_marker(self.d, UID))       # dossier
        self.assertFalse(xescape.read_marker(os.path.join(self.d, "absent"), UID))


class TestMarkerExecuteOnlyDir(unittest.TestCase):
    """Régression terrain : /run/opentv est 0751 (opentv:opentv) ; opentv-keys n'a que l'EXÉCUTION. L'ancien
    open(O_RDONLY|O_DIRECTORY) du dossier levait EACCES : l'aide ne s'armait jamais. Correctif : fd de chemin O_PATH
    (Linux) puis openat du nom connu ; jamais d'élargissement du mode ni du groupe."""
    FAKE_O_PATH = 0x40000000

    def setUp(self):
        self.d = tmp(self)
        self.assertTrue(xescape.set_marker(self.d, True, boot_id="b1"))

    def test_flags_path_fd_no_read_write_follow(self):
        f = xescape._marker_dir_flags(self.FAKE_O_PATH)
        self.assertTrue(f & self.FAKE_O_PATH)
        self.assertEqual(f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), 0)
        for want in (os.O_DIRECTORY, os.O_NOFOLLOW, os.O_CLOEXEC):
            self.assertTrue(f & want)
        fb = xescape._marker_dir_flags(0)                          # repli hors Linux : lecture seule, inchangé
        self.assertEqual(fb, xescape._DIR_FLAGS)
        if hasattr(os, "O_PATH"):
            self.assertTrue(xescape._MARKER_DIR_FLAGS & getattr(os, "O_PATH"))
            self.assertEqual(xescape._MARKER_DIR_FLAGS & (os.O_WRONLY | os.O_RDWR), 0)

    def test_simulated_execute_only_dir(self):
        """Noyau simulé : ouvrir le dossier sans O_PATH = EACCES (exécution seule) ; openat du nom connu permis."""
        real_open, d, bit = os.open, self.d, self.FAKE_O_PATH
        seen = []

        def fake_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None and os.path.abspath(path) == d:
                seen.append(flags)
                if not flags & bit:
                    raise PermissionError(13, "Permission denied", path)
                flags &= ~bit
            return real_open(path, flags, mode, dir_fd=dir_fd)

        old = xescape._MARKER_DIR_FLAGS
        xescape._MARKER_DIR_FLAGS = xescape._marker_dir_flags(0)
        xescape.os.open = fake_open
        try:
            self.assertFalse(xescape.read_marker(d, UID, boot_id="b1"))      # ancien chemin O_RDONLY : jamais armé
            xescape._MARKER_DIR_FLAGS = xescape._marker_dir_flags(bit)
            self.assertTrue(xescape.read_marker(d, UID, boot_id="b1"))       # fd de chemin : témoin lu
            self.assertFalse(xescape.read_marker(d, UID, boot_id="b2"))      # contrôles inchangés
            self.assertFalse(xescape.read_marker(d, UID + 1, boot_id="b1"))
        finally:
            xescape.os.open = real_open
            xescape._MARKER_DIR_FLAGS = old
        self.assertTrue(all(f & os.O_NOFOLLOW and not f & (os.O_WRONLY | os.O_RDWR) for f in seen))

    @unittest.skipUnless(hasattr(os, "O_PATH") and os.geteuid() != 0, "Linux O_PATH, non-root (droits réels)")
    def test_real_execute_only_dir_linux(self):
        os.chmod(self.d, 0o311)                                    # -wx : traversée sans lecture (comme « autres » 0751)
        self.addCleanup(os.chmod, self.d, 0o755)
        with self.assertRaises(PermissionError):
            os.open(self.d, xescape._DIR_FLAGS)                    # preuve : l'ancien open échoue
        self.assertTrue(xescape.read_marker(self.d, UID, boot_id="b1"))
        self.assertFalse(xescape.read_marker(self.d, UID, boot_id="b2"))
        link = self.d + ".lnk"
        os.symlink(self.d, link)
        self.addCleanup(os.unlink, link)
        self.assertFalse(xescape.read_marker(link, UID, boot_id="b1"))      # dossier lien : refusé (O_NOFOLLOW)

    def test_symlinked_run_dir_refused(self):
        link = self.d + ".lnk"
        os.symlink(self.d, link)
        self.addCleanup(os.unlink, link)
        self.assertFalse(xescape.read_marker(link, UID, boot_id="b1"))

    def test_permissions_never_widened(self):
        svc = unit("opentv.service")
        self.assertIn("RuntimeDirectory=opentv\n", svc)
        self.assertIn("RuntimeDirectoryMode=0751\n", svc)         # listing toujours fermé aux autres comptes
        helper = unit("opentv-x-escape.service")
        self.assertIn("Group=opentv-keys\n", helper)
        self.assertIn("SupplementaryGroups=input\n", helper)      # jamais le groupe opentv
        self.assertNotRegex(helper, r"(?m)^(Group|SupplementaryGroups)=.*\bopentv\b(?!-)")
        self.assertNotIn("ReadWritePaths=/run/opentv\n", helper)
        with open(os.path.join(PI4, "system", "sysusers-opentv.conf")) as f:
            su = [l.split() for l in f if l.strip() and not l.startswith("#")]
        self.assertFalse(any(w[0] == "m" and w[1] == "opentv-keys" for w in su))   # opentv-keys hors groupe opentv


class TestDecoderAndDevices(unittest.TestCase):
    def test_exit_press_only(self):
        d = xescape.ExitDecoder()
        self.assertEqual(d.feed(ev(174, 1)), ["exit"])
        self.assertEqual(d.feed(ev(174, 2)), [])                  # répétition
        self.assertEqual(d.feed(ev(174, 0)), [])                  # relâché
        for code in (1, 14, 28, 158, 172, 206, 352, 103, 108, 30, 2):   # Échap, ←, OK, Back, Home, Close, lettres…
            self.assertEqual(d.feed(ev(code, 1)), [], code)
        self.assertEqual(d.feed(ev(174, 1, typ=4)), [])           # EV_MSC
        self.assertEqual(d.feed(ev(174, 1) + ev(174, 0) + ev(174, 1)), ["exit", "exit"])
        self.assertEqual(d.feed(b"\x00" * 5), [])                 # bloc partiel

    def test_nav_class_decoder_exit_identical_and_classes_only(self):
        """Canari 0.7.8 : KEY_EXIT exactement comme ExitDecoder ; flèches -> « move », OK/Entrée -> « ok » (classes,
        jamais la direction) ; répétitions, relâchés, autres codes et autres types : rien."""
        d, old = xescape.NavClassDecoder(), xescape.ExitDecoder()
        for data in (ev(174, 1), ev(174, 2), ev(174, 0), ev(174, 1, typ=4), b"\x00" * 5,
                     ev(174, 1) + ev(174, 0) + ev(174, 1)):
            self.assertEqual([x for x in d.feed(data) if x == "exit"], old.feed(data))
        for code in (103, 105, 106, 108):                         # ↑ ← → ↓ : même classe, direction perdue
            self.assertEqual(d.feed(ev(code, 1)), ["move"], code)
            self.assertEqual(d.feed(ev(code, 2) + ev(code, 0)), [], code)
        for code in (28, 352, 353):                               # Entrée (Select CEC remappé), KEY_OK, KEY_SELECT
            self.assertEqual(d.feed(ev(code, 1)), ["ok"], code)
        for code in (1, 14, 30, 2, 158, 172, 206, 57, 113):        # Échap, Retour arrière, lettres, Back, Home…
            self.assertEqual(d.feed(ev(code, 1)), [], code)
        self.assertEqual(d.feed(ev(103, 1, typ=4)), [])           # EV_MSC

    def test_only_vc4_hdmi_devices_with_key_exit(self):
        cases = {"vc4-hdmi-0": True, "vc4-hdmi-1": True, "vc4-hdmi": True, "USB Keyboard": False,
                 "vc4-hdmi-0 extra": False, "Logitech K400": False, "": False, None: False}
        for name, want in cases.items():
            with self.subTest(name=name):
                f = xescape.make_device_filter(name_fn=lambda fd, n=name: n, keys_fn=lambda fd, codes: True)
                self.assertEqual(f(3), want)
        f = xescape.make_device_filter(name_fn=lambda fd: "vc4-hdmi-0", keys_fn=lambda fd, codes: False)
        self.assertFalse(f(3))                                    # pas de KEY_EXIT : ignoré


class FakeWatcher:
    instances = []

    def __init__(self, on_key, **kw):
        self.on_key, self.kw, self.stopped = on_key, kw, False
        FakeWatcher.instances.append(self)

    def start(self):
        return self

    def stop(self, timeout=2.0):
        self.stopped = True
        return True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestHelper(unittest.TestCase):
    def setUp(self):
        self.rdir, self.req = tmp(self), tmp(self)
        FakeWatcher.instances = []
        self.clock = Clock()
        self.events = []
        self.h = xescape.Helper(self.rdir, self.req, UID, watcher_factory=FakeWatcher, mono=self.clock,
                                log=lambda e, **f: self.events.append((e, f)), boot_id=lambda: "b")

    def request(self):
        p = os.path.join(self.req, xescape.REQUEST)
        return read(p) if os.path.exists(p) else None

    def test_not_armed_on_tv_then_armed_on_x_then_disarmed(self):
        self.h.tick()
        self.assertEqual(FakeWatcher.instances, [])               # /tv : aucune lecture evdev
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        self.assertEqual(len(FakeWatcher.instances), 1)
        w = FakeWatcher.instances[0]
        self.assertIsInstance(w.kw["decoder_factory"](), xescape.NavClassDecoder)   # KEY_EXIT + classes canari
        self.h.tick()
        self.assertEqual(len(FakeWatcher.instances), 1)           # jamais deux surveillances
        xescape.set_marker(self.rdir, False)
        self.h.tick()
        self.assertTrue(w.stopped)
        self.assertIsNone(self.h.watcher)

    def test_exit_on_x_writes_one_exact_request_rate_limited(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        on_key = FakeWatcher.instances[0].on_key
        on_key("exit")
        self.assertEqual(self.request(), xescape.REQUEST_BODY)
        self.assertEqual(os.stat(os.path.join(self.req, xescape.REQUEST)).st_mode & 0o777, 0o644)
        os.unlink(os.path.join(self.req, xescape.REQUEST))       # consommée par l'action root
        self.clock.t += 1
        on_key("exit")                                            # rafale : limitée
        self.assertIsNone(self.request())
        self.clock.t += xescape.HELPER_MIN_INTERVAL_S
        on_key("exit")
        self.assertEqual(self.request(), xescape.REQUEST_BODY)
        self.assertEqual([e for e, _ in self.events if e.startswith("x_escape_req")],
                         ["x_escape_requested", "x_escape_requested"])
        self.assertTrue(all(set(f) <= {"reason"} for _, f in self.events))   # aucun code de touche journalisé

    def test_exit_after_marker_cleared_or_other_reason_does_nothing(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        on_key = FakeWatcher.instances[0].on_key
        on_key("back")
        on_key("home")
        self.assertIsNone(self.request())
        xescape.set_marker(self.rdir, False)                       # /tv rechargée entre-temps
        on_key("exit")
        self.assertIsNone(self.request())

    def test_nav_counters_memory_only_ttl_never_logged_never_an_action(self):
        """Canari : appuis move/ok comptés (classe seulement) pendant que le témoin est valide, en MÉMOIRE seulement :
        AUCUN journal (ni compteurs, ni activité par touche), aucune demande ; vue opérateur vivante tant qu'armée et
        NAV_TTL_S après le dernier armement/appui/désarmement, puis oubliée ; jamais une direction ni un code."""
        self.assertEqual(xescape.NAV_TTL_S, 120.0)
        from opentv_pi import xprobe
        self.assertEqual(xescape.NAV_TTL_S, xprobe.TTL_S)        # commentaire « même TTL que la sonde » exact
        self.assertEqual(self.h.nav_view(), {"present": False, "armed": False})   # jamais armée : rien
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        on_key = FakeWatcher.instances[0].on_key
        self.assertEqual(self.h.nav_view(), {"present": True, "armed": True, "move": 0, "ok": 0, "age_s": 0.0})
        for why in ("move", "move", "ok", "move"):
            self.clock.t += 1
            on_key(why)
        for _ in range(20):
            self.h.tick()
        self.assertIsNone(self.request())                        # aucune action
        self.assertEqual(self.h.nav_view(), {"present": True, "armed": True, "move": 3, "ok": 1, "age_s": 0.0})
        self.clock.t += xescape.NAV_TTL_S * 3                      # armée sans appui : toujours présente
        self.assertEqual(self.h.nav_view()["move"], 3)
        on_key("move")
        xescape.set_marker(self.rdir, False)                       # /tv : désarmé, compteurs gardés NAV_TTL_S
        self.h.tick()
        self.clock.t += 5
        self.assertEqual(self.h.nav_view(), {"present": True, "armed": False, "move": 4, "ok": 1, "age_s": 5.0})
        on_key("move")                                            # après désarmement : jamais compté
        self.assertEqual(self.h.nav_view()["move"], 4)
        self.clock.t += xescape.NAV_TTL_S                          # TTL écoulé : oubliés (mémoire remise à zéro)
        self.assertEqual(self.h.nav_view(), {"present": False, "armed": False})
        self.assertEqual(self.h.nav, {"move": 0, "ok": 0})
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()                                             # réarmé : compteurs repartent de zéro
        self.assertEqual(self.h.nav, {"move": 0, "ok": 0})
        self.clock.t -= 10_000                                     # horloge incohérente : jamais négatif
        self.assertEqual(self.h.nav_view()["age_s"], 0.0)
        self.h.nav["move"] = xescape.NAV_COUNT_MAX
        FakeWatcher.instances[-1].on_key("move")
        self.assertEqual(self.h.nav["move"], xescape.NAV_COUNT_MAX)       # borné
        # journal : armement/désarmement seulement — aucune ligne par touche ni aucun compteur
        self.assertEqual({e for e, _ in self.events}, {"x_escape_armed", "x_escape_disarmed"})
        self.assertTrue(all(f == {} for _, f in self.events), self.events)
        self.assertIsNone(self.request())
        self.assertFalse(hasattr(xescape, "NAV_LOG_INTERVAL_S"))

    def test_only_key_exit_writes_a_request(self):
        """Seule « exit » (KEY_EXIT décodée) produit une demande ; move/ok et toute autre raison : jamais."""
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        on_key = FakeWatcher.instances[0].on_key
        for why in ("move", "ok", "close", "back", "home", "", None, "EXIT"):
            on_key(why)
        self.assertIsNone(self.request())
        on_key("exit")
        self.assertEqual(self.request(), xescape.REQUEST_BODY)

    def test_pending_request_not_rewritten_and_unsafe_dir_refused(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.h.tick()
        with open(os.path.join(self.req, xescape.REQUEST), "w") as f:
            f.write("old")
        FakeWatcher.instances[0].on_key("exit")
        self.assertEqual(self.request(), "old")
        os.unlink(os.path.join(self.req, xescape.REQUEST))
        shutil.rmtree(self.req)
        self.clock.t += 60
        FakeWatcher.instances[0].on_key("exit")                   # dossier absent : échec propre, jamais d'exception
        self.assertIn("x_escape_request_failed", [e for e, _ in self.events])


class TestHelperRealWatcher(unittest.TestCase):
    """Vrai KeyWatcher (thread, select, rebalayage) sur des tubes : reconnexion et filtrage bout en bout."""

    def test_exit_through_real_watcher_with_reconnect(self):
        run, req = tmp(self), tmp(self)
        devs = FakeDevices(self)
        h = xescape.Helper(run, req, UID, watcher_factory=devs.factory, boot_id=lambda: "b", min_interval_s=0.0)
        kbd = devs.add("USB Keyboard")
        hdmi = devs.add("vc4-hdmi-0")
        add_dev = devs.add
        xescape.set_marker(run, True, boot_id="b")
        stop = threading.Event()
        t = threading.Thread(target=h.run, args=(stop,), kwargs={"poll_s": 0.05}, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(3)))
        reqp = os.path.join(req, xescape.REQUEST)
        self.assertTrue(wait(lambda: h.watcher is not None and h.watcher.ready.is_set()))
        os.write(kbd, ev(174, 1))                                 # clavier : jamais lu
        os.write(hdmi, ev(174, 2) + ev(174, 0))                   # répétition / relâché : rien
        time.sleep(0.3)
        self.assertFalse(os.path.exists(reqp))
        os.write(hdmi, ev(174, 1))
        self.assertTrue(wait(lambda: os.path.exists(reqp)))
        os.unlink(reqp)
        os.close(hdmi)                                            # débranché (EOF) puis rebranché
        hdmi2 = add_dev("vc4-hdmi-0")
        time.sleep(0.3)
        os.write(hdmi2, ev(174, 1))
        self.assertTrue(wait(lambda: os.path.exists(reqp)))
        xescape.set_marker(run, False)
        self.assertTrue(wait(lambda: h.watcher is None))           # /tv : désarmé, périphériques refermés


class TestRootAction(unittest.TestCase):
    def setUp(self):
        self.rdir, self.req, self.stamp = tmp(self), tmp(self), tmp(self)
        self.calls = []
        self.clock = Clock()
        self.rc = 0

    def act(self, **kw):
        def restart():
            self.calls.append(list(xescape.RESTART_ARGV))
            return self.rc == 0
        return xescape.run_action(self.req, self.rdir, self.stamp, keys_uid=UID, service_uid=UID, restart=restart,
                                  mono=self.clock, boot_id=lambda: "b", **kw)

    def put(self, body=None):
        with open(os.path.join(self.req, xescape.REQUEST), "w") as f:
            f.write(xescape.REQUEST_BODY if body is None else body)

    def test_restart_argv_is_exact(self):
        self.assertEqual(xescape.RESTART_ARGV, ("systemctl", "restart", "--no-block", "opentv-kiosk.service"))

    def test_valid_request_on_x_restarts_kiosk_once(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.put()
        r = self.act()
        self.assertEqual(r["action"], "restart")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(os.path.exists(os.path.join(self.req, xescape.REQUEST)))
        self.put()
        self.clock.t += 2
        self.assertEqual(self.act()["reason"], "rate_limited")    # double appui : une seule relance
        self.assertEqual(len(self.calls), 1)
        self.put()
        self.clock.t += xescape.ACTION_MIN_INTERVAL_S
        self.assertEqual(self.act()["action"], "restart")
        self.assertEqual(len(self.calls), 2)

    def test_on_tv_never_restarts(self):
        self.put()
        r = self.act()
        self.assertEqual((r["action"], r["reason"]), ("skipped", "not_x"))
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.req, xescape.REQUEST)))   # toujours retirée

    def test_no_request_noop(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.assertEqual(self.act()["action"], "none")
        self.assertEqual(self.calls, [])

    def test_bad_requests_refused_and_removed(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        for body in ("", "restart", xescape.REQUEST_BODY + "x", "x" * 4096):
            with self.subTest(body=body[:12]):
                self.put(body)
                r = self.act()
                self.assertEqual(r["action"], "refused")
                self.assertFalse(os.path.lexists(os.path.join(self.req, xescape.REQUEST)))
        target = os.path.join(self.req, "t")
        with open(target, "w") as f:
            f.write(xescape.REQUEST_BODY)
        os.symlink(target, os.path.join(self.req, xescape.REQUEST))
        self.assertEqual(self.act()["action"], "refused")         # lien : jamais suivi
        self.assertFalse(os.path.lexists(os.path.join(self.req, xescape.REQUEST)))
        self.assertTrue(os.path.exists(target))
        self.put()
        r = xescape.run_action(self.req, self.rdir, self.stamp, keys_uid=UID + 1, service_uid=UID,
                               restart=lambda: self.calls.append(1), mono=self.clock, boot_id=lambda: "b")
        self.assertEqual(r["action"], "refused")                  # propriétaire ≠ opentv-keys
        self.assertEqual(self.calls, [])

    def test_marker_owner_checked_by_root(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.put()
        r = xescape.run_action(self.req, self.rdir, self.stamp, keys_uid=UID, service_uid=UID + 1,
                               restart=lambda: self.calls.append(1), mono=self.clock, boot_id=lambda: "b")
        self.assertEqual(r["reason"], "not_x")
        self.assertEqual(self.calls, [])

    def test_restart_failure_reported(self):
        xescape.set_marker(self.rdir, True, boot_id="b")
        self.put()
        self.rc = 1
        r = self.act()
        self.assertEqual((r["action"], r["ok"]), ("restart", False))


class TestPresenceRoute(unittest.TestCase):
    def setUp(self):
        self.a = make_app(self)
        self.s = Servers(self, self.a)
        self.marker = os.path.join(self.a.cfg.run_dir, xescape.MARKER)

    def post(self, body, key=True, ctype="application/json"):
        h = {"Content-Type": ctype}
        if key:
            h["X-OpenTV-TV"] = self.a.tv_key
        return self.s.req("POST", xescape.PRESENCE_PATH, body=json.dumps(body) if not isinstance(body, str) else body,
                          headers=h)

    def test_set_by_tv_page_cleared_by_tv_load(self):
        r = self.post({"active": True})
        self.assertEqual(r[0], 200, r)
        self.assertTrue(os.path.exists(self.marker))
        self.assertTrue(xescape.read_marker(self.a.cfg.run_dir, os.getuid()))
        self.s.req("GET", "/tv")
        self.assertFalse(os.path.exists(self.marker))             # /tv chargée : plus jamais « X »
        self.post({"active": True})
        self.post({"active": False})
        self.assertFalse(os.path.exists(self.marker))

    def test_gates_and_closed_body(self):
        self.assertEqual(self.post({"active": True}, key=False)[0], 403)
        for bad in ({"active": "yes"}, {"active": 1}, {}, {"active": True, "url": "x"}, []):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(bad)[0], 400)
        self.assertEqual(self.post({"active": True}, ctype="text/plain")[0], 415)
        self.assertFalse(os.path.exists(self.marker))

    def test_opt_out_refuses(self):
        a = make_app(self, OPENTV_X_TV="0")
        s = Servers(self, a)
        r = s.req("POST", xescape.PRESENCE_PATH, body=json.dumps({"active": True}),
                  headers={"Content-Type": "application/json", "X-OpenTV-TV": a.tv_key})
        self.assertEqual(r[0], 409)
        self.assertFalse(os.path.exists(os.path.join(a.cfg.run_dir, xescape.MARKER)))

    def test_service_start_clears_stale_marker(self):
        xescape.set_marker(self.a.cfg.run_dir, True)
        a2 = make_app(self, OPENTV_RUN_DIR=self.a.cfg.run_dir, OPENTV_STATE_DIR=self.a.cfg.state_dir)
        self.assertFalse(os.path.exists(self.marker))
        self.assertIsNotNone(a2)


def unit(name):
    return read(os.path.join(PI4, "systemd", name))


class TestUnitsAndPackaging(unittest.TestCase):
    def test_helper_unit_sandbox(self):
        t = unit("opentv-x-escape.service")
        for want in ("User=opentv-keys", "SupplementaryGroups=input", "DeviceAllow=char-input r",
                     "DevicePolicy=closed", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
                     "NoNewPrivileges=yes", "CapabilityBoundingSet=\n", "RuntimeDirectory=opentv-x-escape",
                     "RuntimeDirectoryMode=0755", "ProtectSystem=strict", "opentv-pi x-escape-helper",
                     "InaccessiblePaths=-/etc/opentv/tmdb", "InaccessiblePaths=-/var/lib/opentv/canvas"):
            self.assertIn(want, t)
        self.assertNotIn("ReadWritePaths=/run/opentv\n", t)     # témoin lu seulement, jamais écrit par l'aide
        self.assertNotIn("ProcSubset=pid", t)                     # boot_id lisible : sinon jamais armé

    def test_action_unit_root_minimal_no_input(self):
        t = unit("opentv-x-escape-action.service")
        self.assertNotIn("User=", t)
        self.assertNotIn("input", t.replace("aucune entr", ""))
        for want in ("Type=oneshot", "PrivateDevices=yes", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX",
                     "NoNewPrivileges=yes", "ReadWritePaths=-/run/opentv-x-escape", "opentv-pi x-escape-action",
                     "RuntimeDirectory=opentv-x-escape-action", "RuntimeDirectoryPreserve=yes",
                     "InaccessiblePaths=-/etc/opentv/tmdb", "InaccessiblePaths=-/var/lib/opentv/canvas",
                     "ConditionPathExists=!/run/opentv-uninstalling", "CapabilityBoundingSet=CAP_DAC_OVERRIDE\n"):
            self.assertIn(want, t)
        self.assertNotIn("[Install]", t)
        p = unit("opentv-x-escape.path")
        self.assertIn("PathExists=/run/opentv-x-escape/request", p)
        self.assertIn("Unit=opentv-x-escape-action.service", p)

    def test_kiosk_never_gains_input_for_this(self):
        self.assertNotIn("x-escape", unit("opentv-kiosk.service"))
        self.assertNotIn("input", read(os.path.join(PI4, "bin", "opentv-kiosk-browser")).lower().replace(
            "input-", ""))

    def test_install_uninstall_health_lists(self):
        inst = read(os.path.join(PI4, "install.sh"))
        uninst = read(os.path.join(PI4, "uninstall.sh"))
        for u in ("opentv-x-escape.service", "opentv-x-escape.path", "opentv-x-escape-action.service"):
            self.assertIn(u, inst)
            self.assertIn(u, uninst)
        self.assertIn("opentv-x-escape.service", health.UNITS_OPTIONAL)
        self.assertIn("opentv-x-escape.path", health.UNITS_OPTIONAL)

    def test_cli_wires_commands(self):
        from opentv_pi import cli
        p = cli.build_parser()
        self.assertEqual(p.parse_args(["x-escape-helper"]).cmd, "x-escape-helper")
        self.assertEqual(p.parse_args(["x-escape-action"]).cmd, "x-escape-action")


class TestNavServer(unittest.TestCase):
    """Lecture opérateur des compteurs evdev : socket Unix dans le dossier d'exécution de l'aide (tmpfs /run), 0600,
    pair vérifié (root seulement ; SO_PEERCRED indisponible = refus), une ligne JSON fermée, rien lu du client, aucun
    disque ni journal ; read_nav (root) refuse un lien, un autre propriétaire, une vue hors liste fermée."""

    VIEW = {"present": True, "armed": True, "move": 2, "ok": 1, "age_s": 1.5}

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvn", dir="/tmp")     # chemin court (sun_path ≤ 104 octets sur macOS)
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, xescape.NAV_SOCK)
        # côté CLIENT (read_nav) : UID du serveur simulé = compte courant (hors Linux, SO_PEERCRED absent) ; les
        # tests du contrôle réel l'arrêtent (self.real_client_peer())
        self._client_peer = mock.patch.object(xescape, "_peer_uid", lambda c: os.getuid())
        self._client_peer.start()
        self.addCleanup(self._client_peer.stop)

    def real_client_peer(self):
        self._client_peer.stop()
        self._client_peer = mock.patch.object(xescape, "_peer_uid", xescape._peer_uid)   # rien à rétablir
        self._client_peer.start()

    def serve(self, view=None, peer=0, **kw):
        srv = xescape.NavServer(self.d, view or (lambda: dict(self.VIEW)), peer_uid=lambda c: peer, **kw).start()
        self.addCleanup(srv.stop)
        return srv

    def test_root_reads_closed_view(self):
        self.serve()
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)   # plusieurs lectures : serveur vivant
        self.assertEqual(os.listdir(self.d), [xescape.NAV_SOCK])  # rien d'autre écrit

    def test_non_root_or_unknown_peer_refused(self):
        for peer in (1000, os.getuid() if os.getuid() else 1, None):
            with self.subTest(peer=peer):
                srv = xescape.NavServer(self.d, lambda: dict(self.VIEW), peer_uid=lambda c, p=peer: p).start()
                try:
                    self.assertEqual(xescape.read_nav(self.d, os.getuid()),
                                     {"present": False, "error": "operator_only"})
                finally:
                    srv.stop()

    @unittest.skipIf(hasattr(__import__("socket"), "SO_PEERCRED"), "hors Linux seulement")
    def test_default_peer_check_unavailable_fails_closed(self):
        srv = xescape.NavServer(self.d, lambda: dict(self.VIEW)).start()
        self.addCleanup(srv.stop)
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), {"present": False, "error": "operator_only"})

    @unittest.skipUnless(hasattr(__import__("socket"), "SO_PEERCRED"), "SO_PEERCRED (Linux)")
    def test_real_peercred_uid(self):
        self.real_client_peer()
        srv = xescape.NavServer(self.d, lambda: dict(self.VIEW), allowed=frozenset({os.getuid()})).start()
        self.addCleanup(srv.stop)
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)   # vrai SO_PEERCRED des DEUX côtés
        import socket as so
        with so.socket(so.AF_UNIX, so.SOCK_STREAM) as c:        # côté client : UID de CELUI QUI ÉCOUTE
            c.connect(self.path)
            self.assertEqual(xescape._peer_uid(c), os.getuid())
        # serveur d'un autre compte (simulé : le vrai contrôle rend l'UID courant, attendu UID+1) : refus, rien lu
        real = xescape._peer_uid
        self.assertEqual(xescape.read_nav(self.d, os.getuid(), peer_uid=lambda c: real(c) + 1),
                         {"present": False, "error": "peer"})

    @unittest.skipIf(hasattr(__import__("socket"), "SO_PEERCRED"), "hors Linux seulement")
    def test_client_peer_check_unavailable_fails_closed(self):
        self.real_client_peer()
        self.serve()
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), {"present": False, "error": "peer"})

    def test_client_requires_server_uid_keys_uid_before_reading(self):
        """L2 : read_nav vérifie l'UID du processus SERVEUR (SO_PEERCRED après connect) = compte de l'aide AVANT
        toute lecture ; autre compte (root compris) ou inconnu : refus `peer`, la vue n'est jamais lue."""
        self.serve()
        seen = []

        def peer(uid):
            def f(c):
                seen.append(uid)
                return uid
            return f
        for uid in (os.getuid() + 1, 0 if os.getuid() else 1, None):
            with self.subTest(uid=uid):
                self.assertEqual(xescape.read_nav(self.d, os.getuid(), peer_uid=peer(uid)),
                                 {"present": False, "error": "peer"})
        self.assertEqual(len(seen), 3)
        self.assertEqual(xescape.read_nav(self.d, os.getuid(), peer_uid=peer(os.getuid())), self.VIEW)
        with mock.patch.object(xescape.socket.socket, "recv", side_effect=AssertionError("lu avant contrôle")):
            self.assertEqual(xescape.read_nav(self.d, os.getuid(), peer_uid=lambda c: None),
                             {"present": False, "error": "peer"})

    # ------------------------------------------------------------------------------- cycle de vie (L4)
    def nav_threads(self):
        import threading
        return [t for t in threading.enumerate() if t.name == "x-escape-nav" and t.is_alive()]

    def test_stop_ordered_signal_wake_join_close_unlink(self):
        srv = xescape.NavServer(self.d, lambda: dict(self.VIEW), peer_uid=lambda c: 0).start()
        real_sock, real_thread = srv.sock, srv.thread
        order = []

        class S:
            def shutdown(self, how):
                order.append(("wake", srv.stopping.is_set()))
                real_sock.shutdown(how)

            def close(self):
                order.append(("close", real_thread.is_alive()))
                real_sock.close()

        class T:
            def join(self, timeout=None):
                order.append(("join", timeout))
                real_thread.join(timeout)
        srv.sock, srv.thread = S(), T()
        real_unlink = os.unlink

        def unlink(p, *a, **k):
            order.append(("unlink", p == self.path))
            return real_unlink(p, *a, **k)
        with mock.patch.object(xescape.os, "unlink", unlink):
            srv.stop()
        self.assertEqual(order, [("wake", True), ("join", xescape.NAV_STOP_JOIN_S), ("close", False),
                                 ("unlink", True)])
        self.assertFalse(real_thread.is_alive())
        self.assertFalse(os.path.exists(self.path))
        self.assertGreater(xescape.NAV_STOP_JOIN_S, xescape.NAV_SOCK_TIMEOUT_S + xescape.POLL_S)
        srv.stop()                                                # idempotent
        xescape.NavServer(self.d, lambda: {}).stop()              # jamais démarré : rien
        with self.assertRaises(OSError):
            srv.start()                                           # démarrage unique

    def test_partial_start_failure_cleans_socket(self):
        """chmod, listen ou thread refusé (TasksMax de l'unité) : socket fermé, fichier lié retiré, aucun thread,
        OSError (l'aide continue sans lecture opérateur)."""
        import threading
        before = len(self.nav_threads())
        fails = (("chmod", mock.patch.object(xescape.os, "chmod", side_effect=PermissionError("chmod"))),
                 ("listen", mock.patch.object(xescape.socket.socket, "listen", side_effect=OSError("listen"))),
                 ("thread", mock.patch.object(threading.Thread, "start",
                                              side_effect=RuntimeError("can't start new thread"))))
        for name, patch in fails:
            with self.subTest(name=name):
                srv = xescape.NavServer(self.d, lambda: dict(self.VIEW), peer_uid=lambda c: 0)
                with patch, self.assertRaises(OSError):
                    srv.start()
                self.assertFalse(os.path.lexists(self.path), name)
                self.assertEqual((srv.sock, srv.thread), (None, None))
                self.assertEqual(len(self.nav_threads()), before)
                srv.stop()
        self.serve()                                              # puis un démarrage normal réussit
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)

    def test_stop_never_removes_a_successor_socket(self):
        old = xescape.NavServer(self.d, lambda: {"present": False, "armed": False}, peer_uid=lambda c: 0).start()
        self.serve()                                              # successeur : remplace le socket orphelin
        old.stop()
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)

    def test_unit_keeps_tasks_max(self):
        self.assertRegex(unit("opentv-x-escape.service"), r"(?m)^TasksMax=[1-9][0-9]?$")

    def test_read_nav_refuses_link_owner_and_invalid_views(self):
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), {"present": False, "error": "unavailable"})
        self.serve()
        self.assertEqual(xescape.read_nav(self.d, os.getuid() + 1), {"present": False, "error": "unsafe"})
        other = tempfile.mkdtemp(prefix="otvl", dir="/tmp")
        self.addCleanup(shutil.rmtree, other, True)
        os.symlink(self.path, os.path.join(other, xescape.NAV_SOCK))   # lien vers un vrai socket : jamais suivi
        self.assertEqual(xescape.read_nav(other, os.getuid()), {"present": False, "error": "unsafe"})
        for bad in ({"present": True, "armed": True, "move": 2, "ok": 1, "age_s": 1, "key": "up"},
                    {"present": True, "armed": "yes", "move": 2, "ok": 1, "age_s": 1},
                    {"present": True, "armed": True, "move": -1, "ok": 1, "age_s": 1},
                    {"present": True, "armed": True, "move": True, "ok": 1, "age_s": 1}, [1], "x" * 600):
            with self.subTest(bad=bad):
                d2 = tempfile.mkdtemp(prefix="otvb", dir="/tmp")
                self.addCleanup(shutil.rmtree, d2, True)
                srv = xescape.NavServer(d2, lambda b=bad: b, peer_uid=lambda c: 0).start()
                try:
                    self.assertEqual(xescape.read_nav(d2, os.getuid())["present"], False)
                    self.assertIn(xescape.read_nav(d2, os.getuid())["error"], ("invalid", "unavailable"))
                finally:
                    srv.stop()

    def test_view_failure_keeps_serving_and_stale_socket_replaced(self):
        calls = []

        def view():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return dict(self.VIEW)
        srv = self.serve(view=view)
        self.assertEqual(xescape.read_nav(self.d, os.getuid())["present"], False)   # échec : connexion fermée
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)          # puis servie
        srv.stop()
        self.assertFalse(os.path.exists(self.path))               # arrêt : socket retiré
        import socket as so
        stale = so.socket(so.AF_UNIX, so.SOCK_STREAM)              # socket orphelin d'un démarrage précédent
        stale.bind(self.path)
        stale.close()
        self.serve()
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), self.VIEW)

    def test_regular_file_never_unlinked(self):
        with open(self.path, "w") as f:
            f.write("x")
        with self.assertRaises(OSError):
            xescape.NavServer(self.d, lambda: dict(self.VIEW), peer_uid=lambda c: 0).start()
        with open(self.path) as f:
            self.assertEqual(f.read(), "x")

    def test_helper_view_end_to_end_via_socket(self):
        rdir, req = tmp(self), tmp(self)
        FakeWatcher.instances = []
        clock = Clock()
        h = xescape.Helper(rdir, req, UID, watcher_factory=FakeWatcher, mono=clock, boot_id=lambda: "b")
        self.serve(view=h.nav_view)
        self.assertEqual(xescape.read_nav(self.d, os.getuid()), {"present": False, "armed": False})
        xescape.set_marker(rdir, True, boot_id="b")
        h.tick()
        FakeWatcher.instances[0].on_key("move")
        FakeWatcher.instances[0].on_key("ok")
        self.assertEqual(xescape.read_nav(self.d, os.getuid()),
                         {"present": True, "armed": True, "move": 1, "ok": 1, "age_s": 0.0})
        self.assertFalse(os.path.exists(os.path.join(req, xescape.REQUEST)))


class TestCliRuntime(unittest.TestCase):
    """Régression terrain (crash en boucle de opentv-x-escape.service : NameError `pwd` dans _uid_of) : les VRAIES
    commandes x-escape-helper / x-escape-action sont exécutées par le VRAI binaire (python3 -I, sous-processus), avec
    le compte courant comme comptes du service et des touches — chemin pwd.getpwnam réellement parcouru."""

    def setUp(self):
        import pwd
        self.d = tmp(self)
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.env = os.path.join(self.d, "opentv.env")
        with open(self.env, "w") as f:
            f.write("OPENTV_SERVICE_USER=%s\nOPENTV_PLAYER_KEYS_USER=%s\nOPENTV_RUN_DIR=%s\n"
                    % (self.user, self.user, os.path.join(self.d, "run")))
        self.argv = [sys.executable, "-I", os.path.join(PI4, "bin", "opentv-pi"), "--env", self.env]

    def test_helper_starts_resolves_uid_and_stops_cleanly(self):
        p = subprocess.Popen(self.argv + ["x-escape-helper"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        try:
            line = p.stdout.readline()   # première ligne : prête (après _uid_of), sinon traceback et sortie
            if not line:
                self.fail("aide terminée sans x_escape_ready : %s" % p.stderr.read()[-2000:])
            self.assertEqual(json.loads(line)["event"], "x_escape_ready", line)
            self.assertIsNone(p.poll())
            p.terminate()
            out, err = p.communicate(timeout=10)
        finally:
            if p.poll() is None:
                p.kill()
                p.communicate()
        self.assertEqual(p.returncode, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("x_escape_no_service_user", line + out)

    def test_action_resolves_accounts_and_noops_without_request(self):
        p = subprocess.run(self.argv + ["x-escape-action"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        out = json.loads(p.stdout)
        self.assertNotEqual(out.get("reason"), "accounts", out)   # comptes résolus par pwd, pas refusés
        self.assertTrue(out["ok"], out)

    def test_unknown_service_user_never_crashes(self):
        with open(self.env, "w") as f:
            f.write("OPENTV_SERVICE_USER=opentv-absent-zz\nOPENTV_PLAYER_KEYS_USER=opentv-absent-zz\n"
                    "OPENTV_RUN_DIR=%s\n" % os.path.join(self.d, "run"))
        p = subprocess.run(self.argv + ["x-escape-action"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["reason"], "accounts")


NODE = shutil.which("node")
INSTALL = os.path.join(PI4, "install.sh")


def full_chrome():
    c = os.environ.get("OPENTV_TEST_CHROME_FULL")
    return c if c and os.path.exists(c) else None


@unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT") and full_chrome(),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT + OPENTV_TEST_CHROME_FULL (Chromium complet) + node")
class TestXEscapeEndToEnd(unittest.TestCase):
    """Chaîne réelle hors matériel : /tv (Chromium + extension installée) -> OK humain -> témoin posé par le VRAI
    service -> X (connexion) -> KEY_EXIT 174 sur un périphérique vc4-hdmi simulé -> VRAIE aide (KeyWatcher) -> demande
    -> VRAIE action root (relance simulée du kiosque) -> /tv -> témoin retiré -> KEY_EXIT sur /tv : rien."""

    def test_key_exit_on_x_returns_to_tv_and_is_inert_on_tv(self):
        root = tempfile.mkdtemp(prefix="otvxesce2e", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        env = dict(os.environ, LC_ALL="C")
        env.pop("OPENTV_ROOT", None)
        p = subprocess.run(["bash", INSTALL, "--root", root, "--skip-preflight"], capture_output=True, timeout=180,
                           env=env)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        a = make_app(self, OPENTV_REMOTE_ENABLED="0")
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = Servers(self, a)
        run_dir, req, stamp = a.cfg.run_dir, tmp(self), tmp(self)
        tv = "http://127.0.0.1:%d/tv" % s.web.tv_port()
        cfg = {"chrome": full_chrome(), "ext": os.path.join(root, "opt/opentv/share/x-tv-extension"),
               "ext_id": xcontext.EXTENSION_ID, "tv": tv}
        node = subprocess.Popen([NODE, os.path.join(PI4, "tests", "e2e_x_escape.js"),
                                 os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: node.poll() is None and node.kill())

        def line(prefix):
            ln = node.stdout.readline()
            self.assertTrue(ln.startswith(prefix), ln + node.stderr.read()[-3000:] if not ln else ln)
            return json.loads(ln[len(prefix):].strip() or "null")

        marker = os.path.join(run_dir, xescape.MARKER)
        self.assertFalse(os.path.exists(marker))                 # /tv : jamais « X ouvert »
        ready = line("X_READY ")
        self.assertEqual(ready["url"], "https://x.com/")
        self.assertTrue(xescape.read_marker(run_dir, UID))       # posé par le VRAI service avant l'ouverture de X

        devs = FakeDevices(self)
        kbd = devs.add("Apple Internal Keyboard / Trackpad")
        hdmi = devs.add("vc4-hdmi-0")
        h = xescape.Helper(run_dir, req, UID, watcher_factory=devs.factory)
        stop = threading.Event()
        t = threading.Thread(target=h.run, args=(stop,), kwargs={"poll_s": 0.05}, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(3)))
        self.assertTrue(wait(lambda: h.watcher is not None and h.watcher.ready.is_set()))
        reqp = os.path.join(req, xescape.REQUEST)
        os.write(kbd, ev(174, 1))                                 # clavier physique : jamais lu
        os.write(hdmi, ev(174, 2) + ev(174, 0) + ev(158, 1))      # répétition, relâché, autre touche : rien
        os.write(kbd, ev(103, 1) + ev(28, 1))                     # clavier : jamais compté (jamais lu)
        os.write(hdmi, ev(108, 1) + ev(108, 0) + ev(105, 1) + ev(28, 1) + ev(28, 2))   # canari : classes seulement
        self.assertTrue(wait(lambda: h.nav == {"move": 2, "ok": 1}), h.nav)
        time.sleep(0.4)
        self.assertFalse(os.path.exists(reqp))                    # flèches/OK : jamais une demande
        os.write(hdmi, ev(174, 1))                                # Retour Samsung (KEY_EXIT) sur X
        self.assertTrue(wait(lambda: os.path.exists(reqp)))

        restarts = []

        def restart():
            restarts.append(1)
            node.stdin.write("RESTART\n")
            node.stdin.flush()
            return True
        r = xescape.run_action(req, run_dir, stamp, keys_uid=UID, service_uid=UID, restart=restart)
        self.assertEqual((r["action"], r["ok"]), ("restart", True))
        back = line("TV ")
        self.assertEqual(back["url"], tv)
        self.assertEqual(back["focus"], "X")
        self.assertFalse(os.path.exists(marker))                 # /tv rechargée : témoin retiré par le service
        self.assertTrue(wait(lambda: h.watcher is None))          # aide désarmée : evdev refermé

        # sur /tv : KEY_EXIT jamais lu ; une demande forgée n'entraîne aucune relance
        os.write(hdmi, ev(174, 1))
        time.sleep(0.3)
        self.assertFalse(os.path.exists(reqp))
        with open(reqp, "w") as f:
            f.write(xescape.REQUEST_BODY)
        r = xescape.run_action(req, run_dir, stamp, keys_uid=UID, service_uid=UID, restart=restart)
        self.assertEqual((r["action"], r["reason"]), ("skipped", "not_x"))
        self.assertEqual(len(restarts), 1)
        node.stdin.write("END\n")
        node.stdin.flush()
        out, err = node.communicate(timeout=60)
        self.assertEqual(node.returncode, 0, err[-3000:])
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["tile"], "X")
        self.assertTrue(res["opened"].startswith("https://x.com/home"), res)
        self.assertEqual(res["login_focus"], "login")             # « contrôles » : connexion réelle, jamais l'inscription
        self.assertEqual(res["before_restart"], "https://x.com/")


if __name__ == "__main__":
    unittest.main()
