"""OpenTV Canvas — phase 1 (prototype seulement, jamais installé) : modèle de scène typé (énumérations fermées,
hachage canonique, révision, aperçu expirant, confirmation explicite, historique de cinq versions, retour arrière,
persistance atomique fail-closed) et laboratoire visuel 1920×1080 (ES5, animations transform/opacity seulement).

Rien ici ne touche à scripts/pi4/ui/, opentv_pi/, au manifeste ni aux listes d'installation : le test de non-impact
le vérifie aussi."""
import contextlib
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tests._common import PI4, ROOT

CANVAS = os.path.join(PI4, "prototypes", "canvas")
import sys  # noqa: E402

if CANVAS not in sys.path:
    sys.path.insert(0, CANVAS)
import canvas_model as cm  # noqa: E402

NODE = shutil.which("node")
LAB_FILES = ("lab.html", "lab.css", "canvas.js")


class Clock:
    """Horloges injectées : murale (audit) et monotone (expiration d'aperçu), avancées à la main."""

    def __init__(self, wall=1_800_000_000.0, mono=1000.0):
        self.w, self.m = wall, mono

    def wall(self):
        return self.w

    def mono(self):
        return self.m

    def advance(self, s):
        self.w += s
        self.m += s


def rd(path):
    with open(path, "rb") as f:
        return f.read()


def tmpdir(tc):
    d = tempfile.mkdtemp(prefix="otvcanvas")
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def store(tc, d=None, clock=None, **kw):
    clock = clock or Clock()
    d = d or tmpdir(tc)
    return cm.CanvasStore(os.path.join(d, "scene.json"), wall=clock.wall, mono=clock.mono, **kw), clock, d


# --------------------------------------------------------------------------- schéma fermé
class TestSceneSchema(unittest.TestCase):
    def test_default_is_valid_immutable_and_hashed(self):
        s = cm.DEFAULT_SCENE
        self.assertEqual(cm.validate_scene(s.to_dict()), s)
        with self.assertRaises(Exception):
            s.variant = "mist"                                        # gelé
        self.assertEqual(s.variant, "canvas")
        self.assertEqual(cm.scene_hash(s), hashlib.sha256(cm.canonical_json(s).encode()).hexdigest())

    def test_presets_cover_three_variants(self):
        self.assertEqual(sorted(cm.PRESETS), ["canvas", "cinema", "mist"])
        for name, sc in cm.PRESETS.items():
            self.assertEqual(sc.variant, name)
            self.assertEqual(cm.validate_scene(sc.to_dict()), sc)

    def test_closed_enums_reject_every_unknown_value(self):
        base = cm.DEFAULT_SCENE.to_dict()
        for key in ("variant", "wallpaper", "motion", "accent", "shade", "hero_focus"):
            bad = dict(base, **{key: "nope"})
            with self.assertRaises(cm.SceneError) as e:
                cm.validate_scene(bad)
            self.assertEqual(e.exception.code, "invalid_value", key)

    def test_unknown_or_missing_keys_fail_closed(self):
        base = cm.DEFAULT_SCENE.to_dict()
        for extra in ("css", "html", "script", "url", "asset", "style", "__proto__"):
            with self.assertRaises(cm.SceneError) as e:
                cm.validate_scene(dict(base, **{extra: "x"}))
            self.assertEqual(e.exception.code, "unknown_key", extra)
        for key in base:
            d = dict(base)
            del d[key]
            with self.assertRaises(cm.SceneError) as e:
                cm.validate_scene(d)
            self.assertEqual(e.exception.code, "missing_key", key)

    def test_urls_css_markup_and_paths_are_refused(self):
        base = cm.DEFAULT_SCENE.to_dict()
        hostile = ["https://evil.example/bg.jpg", "url(data:image/png;base64,AAAA)", "file:///etc/passwd",
                   "<script>alert(1)</script>", "red; background:url(x)", "expression(alert(1))",
                   "../../ui/tv.css", "brume.mp4", "javascript:alert(1)", " brume", "BRUME", "brume\n"]
        for v in hostile:
            for key in ("wallpaper", "accent", "variant"):
                with self.assertRaises(cm.SceneError):
                    cm.validate_scene(dict(base, **{key: v}))
            with self.assertRaises(cm.SceneError):
                cm.validate_scene(dict(base, row_order=["tonight", v]))

    def test_types_are_strict(self):
        base = cm.DEFAULT_SCENE.to_dict()
        for key, v in (("schema_version", True), ("schema_version", "1"), ("schema_version", 2),
                       ("variant", 1), ("variant", None), ("variant", ["mist"]), ("row_order", "tonight"),
                       ("row_order", []), ("row_order", ["tonight", "tonight"]), ("row_order", [1]),
                       ("row_order", ["tonight", "new", "top", "watchlist", "new"])):
            with self.assertRaises(cm.SceneError, msg=(key, v)):
                cm.validate_scene(dict(base, **{key: v}))
        with self.assertRaises(cm.SceneError):
            cm.validate_scene(["not", "a", "dict"])

    def test_row_order_must_keep_tonight_first_slot_available(self):
        base = cm.DEFAULT_SCENE.to_dict()
        ok = cm.validate_scene(dict(base, row_order=["new", "tonight"]))
        self.assertEqual(ok.row_order, ("new", "tonight"))
        with self.assertRaises(cm.SceneError):                     # rangée principale jamais supprimée
            cm.validate_scene(dict(base, row_order=["new", "top"]))

    def test_canonical_hash_is_order_independent_and_value_sensitive(self):
        a = cm.DEFAULT_SCENE.to_dict()
        b = dict(reversed(list(a.items())))
        self.assertEqual(cm.scene_hash(cm.validate_scene(a)), cm.scene_hash(cm.validate_scene(b)))
        c = dict(a, accent="ember")
        self.assertNotEqual(cm.scene_hash(cm.validate_scene(a)), cm.scene_hash(cm.validate_scene(c)))
        self.assertNotIn(" ", cm.canonical_json(cm.DEFAULT_SCENE))

    def test_patch_merges_only_known_keys(self):
        sc = cm.apply_patch(cm.DEFAULT_SCENE, {"accent": "jade", "motion": "off"})
        self.assertEqual((sc.accent, sc.motion, sc.variant), ("jade", "off", "canvas"))
        for bad in ({"schema_version": 1}, {"nope": 1}, {}, {"accent": "https://x"}, None, []):
            with self.assertRaises(cm.SceneError, msg=bad):
                cm.apply_patch(cm.DEFAULT_SCENE, bad)


# --------------------------------------------------------------------------- aperçu → confirmation → historique
class TestStoreLifecycle(unittest.TestCase):
    def test_fresh_store_serves_default_revision_zero(self):
        s, _, d = store(self)
        st = s.status()
        self.assertEqual((st["revision"], st["hash"], st["state"]), (0, cm.scene_hash(cm.DEFAULT_SCENE), "default"))
        self.assertEqual(s.current(), cm.DEFAULT_SCENE)
        self.assertFalse(os.path.exists(os.path.join(d, "scene.json")))   # lire ne crée rien

    def test_preview_does_not_publish_and_expires_with_monotonic_clock(self):
        s, clk, d = store(self, preview_ttl_s=30)
        p = s.preview({"accent": "ember"})
        self.assertEqual(s.current(), cm.DEFAULT_SCENE)                  # publiée inchangée
        self.assertEqual(s.visible().accent, "ember")                    # l'aperçu est ce que montre la TV
        self.assertFalse(os.path.exists(os.path.join(d, "scene.json")))   # aucun écrit disque
        self.assertEqual(p["base_revision"], 0)
        self.assertEqual(p["expires_in_s"], 30)
        clk.m += 29.9
        self.assertEqual(s.visible().accent, "ember")
        clk.m += 0.2
        self.assertEqual(s.visible(), cm.DEFAULT_SCENE)                  # retour automatique
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "preview_expired")

    def test_wall_clock_jump_never_extends_or_kills_preview(self):
        s, clk, _ = store(self, preview_ttl_s=30)
        s.preview({"accent": "ember"})
        clk.w -= 86400                                                   # NTP recule l'heure murale
        self.assertEqual(s.visible().accent, "ember")
        clk.w += 10 * 86400
        self.assertEqual(s.visible().accent, "ember")

    def test_confirm_requires_literal_true(self):
        s, _, _ = store(self)
        p = s.preview({"accent": "ember"})
        for v in (False, None, 1, "yes", "true"):
            with self.assertRaises(cm.StoreError) as e:
                s.confirm(p["preview_id"], confirm=v)
            self.assertEqual(e.exception.code, "confirmation_required")
        self.assertEqual(s.status()["revision"], 0)

    def test_confirm_publishes_atomically_with_revision_and_hash(self):
        s, clk, d = store(self)
        p = s.preview({"accent": "ember"})
        r = s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(r["revision"], 1)
        self.assertEqual(r["hash"], p["hash"])
        self.assertEqual(s.current().accent, "ember")
        self.assertIsNone(s.status()["preview"])
        path = os.path.join(d, "scene.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(d)), ["scene.json"])          # aucun temporaire laissé
        again, _, _ = store(self, d=d, clock=clk)
        self.assertEqual(again.current().accent, "ember")
        self.assertEqual(again.status()["revision"], 1)

    def test_wrong_preview_id_and_stale_base_are_refused(self):
        s, _, _ = store(self)
        p = s.preview({"accent": "ember"})
        with self.assertRaises(cm.StoreError) as e:
            s.confirm("x" * len(p["preview_id"]), confirm=True)
        self.assertEqual(e.exception.code, "unknown_preview")
        # une publication concurrente (autre processus) rend l'aperçu périmé
        other = cm.CanvasStore(s.path, wall=s._wall, mono=s._mono)
        q = other.preview({"accent": "jade"})
        other.confirm(q["preview_id"], confirm=True)
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "stale_preview")
        self.assertEqual(cm.CanvasStore(s.path).current().accent, "jade")

    def test_new_preview_replaces_previous_and_cancel_restores(self):
        s, _, _ = store(self)
        p1 = s.preview({"accent": "ember"})
        p2 = s.preview({"accent": "jade"})
        with self.assertRaises(cm.StoreError):
            s.confirm(p1["preview_id"], confirm=True)
        self.assertEqual(s.visible().accent, "jade")
        s.cancel_preview()
        self.assertEqual(s.visible(), cm.DEFAULT_SCENE)
        with self.assertRaises(cm.StoreError):
            s.confirm(p2["preview_id"], confirm=True)

    def test_ui_busy_states_defer_any_change(self):
        s, _, _ = store(self)
        for ui in ("panel", "playback", "setup", "navigating"):
            with self.assertRaises(cm.StoreError) as e:
                s.preview({"accent": "ember"}, ui=ui)
            self.assertEqual(e.exception.code, "ui_busy")
        p = s.preview({"accent": "ember"})
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True, ui="playback")
        self.assertEqual(e.exception.code, "ui_busy")
        with self.assertRaises(cm.StoreError):
            s.preview({"accent": "ember"}, ui="nope")

    def test_invalid_patch_leaves_everything_byte_identical(self):
        s, _, d = store(self)
        s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        path = os.path.join(d, "scene.json")
        before = rd(path)
        for bad in ({"wallpaper": "https://evil/x.jpg"}, {"css": "x"}, {"row_order": ["top"]}):
            with self.assertRaises(cm.SceneError):
                s.preview(bad)
        self.assertEqual(rd(path), before)
        self.assertEqual(s.visible().accent, "ember")

    def test_history_keeps_last_five_and_rollback_walks_back(self):
        s, clk, _ = store(self)
        accents = ["ember", "jade", "violet", "ice", "ember", "jade", "violet"]
        motions = ["off", "reduced", "full", "off", "reduced", "full", "off"]
        for a, m in zip(accents, motions):
            clk.advance(1)
            s.confirm(s.preview({"accent": a, "motion": m})["preview_id"], confirm=True)
        st = s.status()
        self.assertEqual(st["revision"], 7)
        self.assertEqual(len(st["history"]), 5)
        self.assertEqual([h["revision"] for h in st["history"]], [3, 4, 5, 6, 7])
        self.assertEqual(st["history"][-1]["at"], clk.w)
        r = s.rollback(confirm=True)
        self.assertEqual((s.current().accent, s.current().motion), ("jade", "full"))   # révision 6 exacte
        self.assertEqual(r["revision"], 8)                                # compteur monotone (CAS)
        self.assertEqual(r["restored_revision"], 6)
        for _ in range(3):
            s.rollback(confirm=True)
        self.assertEqual(s.current().accent, "violet")                    # révision 3
        r = s.rollback(confirm=True)                                      # historique épuisé ⇒ défaut
        self.assertEqual(s.current(), cm.DEFAULT_SCENE)
        self.assertEqual(r["restored_revision"], 0)
        with self.assertRaises(cm.StoreError) as e:
            s.rollback(confirm=True)
        self.assertEqual(e.exception.code, "nothing_to_rollback")
        with self.assertRaises(cm.StoreError):
            s.rollback(confirm=False)

    def test_reset_default_requires_confirmation_and_clears_preview(self):
        s, _, _ = store(self)
        s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        s.preview({"accent": "jade"})
        with self.assertRaises(cm.StoreError):
            s.reset_default(confirm="yes")
        r = s.reset_default(confirm=True)
        self.assertEqual(s.current(), cm.DEFAULT_SCENE)
        self.assertEqual(s.visible(), cm.DEFAULT_SCENE)
        self.assertEqual(r["revision"], 2)
        self.assertEqual(s.status()["history"], [])

    def test_rollback_cancels_pending_preview(self):
        s, _, _ = store(self)
        s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        p = s.preview({"accent": "jade"})
        s.rollback(confirm=True)
        self.assertIsNone(s.status()["preview"])
        with self.assertRaises(cm.StoreError):
            s.confirm(p["preview_id"], confirm=True)

    def test_preview_expiry_rechecked_under_lock_before_publishing(self):
        """L'aperçu peut expirer pendant l'attente du verrou / la relecture : jamais publié après son échéance."""
        s, clk, d = store(self, preview_ttl_s=30)
        p = s.preview({"accent": "ember"})
        clk.m += 29.0
        real = s._reload

        def slow_reload(dirfd=None):
            clk.m += 2.0                                                 # verrou + relecture lents
            return real(dirfd)
        with mock.patch.object(s, "_reload", slow_reload):
            with self.assertRaises(cm.StoreError) as e:
                s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "preview_expired")
        self.assertFalse(os.path.exists(os.path.join(d, "scene.json")))   # rien publié
        self.assertEqual(s.status()["revision"], 0)
        self.assertIsNone(s.status()["preview"])


# --------------------------------------------------------------------------- annulation par l'humain / interface occupée
class TestHumanLifecycle(unittest.TestCase):
    """Sémantique choisie : annuler-sur-humain/occupé. Toute touche de navigation et toute transition panneau,
    lecture, chargement, configuration ou hors-ligne annule l'aperçu vivant AVANT la transition ; son expiration ne
    peut donc jamais reconstruire les rangées sous un panneau ou une lecture ; la confirmation est refusée tant que
    l'interface est occupée."""

    BUSY = ("panel", "playback", "loading", "setup", "offline")

    def test_every_human_or_busy_event_cancels_live_preview(self):
        for ev in ("key",) + self.BUSY:
            s, clk, _ = store(self)
            p = s.preview({"accent": "ember"})
            r = s.ui_event(ev)
            self.assertEqual(r["preview_cancelled"], True, ev)
            self.assertIsNone(s.status()["preview"], ev)
            self.assertEqual(s.visible(), cm.DEFAULT_SCENE, ev)
            with self.assertRaises(cm.StoreError) as e:                  # occupé ⇒ refus, avant tout identifiant
                s.confirm(p["preview_id"], confirm=True)
            self.assertEqual(e.exception.code, "ui_busy", ev)
            with self.assertRaises(cm.StoreError) as e:
                s.preview({"accent": "jade"})
            self.assertEqual(e.exception.code, "ui_busy", ev)
            if ev == "key":
                clk.m += cm.NAV_QUIET_S + 0.01
            else:
                self.assertEqual(s.ui_event("idle")["preview_cancelled"], False)
            with self.assertRaises(cm.StoreError) as e:                  # l'aperçu annulé ne revient jamais
                s.confirm(p["preview_id"], confirm=True)
            self.assertEqual(e.exception.code, "unknown_preview", ev)
            self.assertEqual(s.status()["revision"], 0)

    def test_event_without_preview_reports_nothing_cancelled(self):
        s, _, _ = store(self)
        self.assertEqual(s.ui_event("panel"), {"ui": "panel", "preview_cancelled": False})
        self.assertEqual(s.ui_event("idle"), {"ui": "idle", "preview_cancelled": False})

    def test_invalid_events_fail_closed_and_keep_preview(self):
        s, _, _ = store(self)
        s.preview({"accent": "ember"})
        for bad in ("nope", "Panel", "idle ", "", None, 1, b"panel", ["key"]):
            with self.assertRaises(cm.StoreError) as e:
                s.ui_event(bad)
            self.assertEqual(e.exception.code, "invalid_event", bad)
        self.assertEqual(s.visible().accent, "ember")

    def test_key_quiet_window_is_monotonic(self):
        s, clk, _ = store(self)
        s.ui_event("key")
        clk.w -= 86400                                                   # l'heure murale recule d'un jour
        with self.assertRaises(cm.StoreError) as e:
            s.preview({"accent": "ember"})
        self.assertEqual((e.exception.code, str(e.exception)), ("ui_busy", "ui_busy: navigating"))
        clk.w += 2 * 86400                                               # … puis avance : ne raccourcit rien
        with self.assertRaises(cm.StoreError):
            s.preview({"accent": "ember"})
        clk.m += cm.NAV_QUIET_S + 0.01
        self.assertEqual(s.preview({"accent": "ember"})["scene"]["accent"], "ember")   # jamais verrouillé

    def _row_preview(self):
        s, clk, d = store(self, preview_ttl_s=30)
        p = s.preview({"row_order": ["top", "tonight", "new"]})
        self.assertEqual(s.visible().row_order, ("top", "tonight", "new"))
        return s, clk, p

    def test_row_order_preview_then_enter_opens_panel_on_published_rows(self):
        s, clk, p = self._row_preview()
        self.assertTrue(s.ui_event("key")["preview_cancelled"])          # OK : touche d'abord
        self.assertFalse(s.ui_event("panel")["preview_cancelled"])       # puis la fiche
        shown = s.visible()
        self.assertEqual(shown.row_order, cm.DEFAULT_SCENE.row_order)
        clk.m += 60                                                      # échéance de l'ancien aperçu dépassée
        self.assertIs(s.visible(), shown)                                # aucune reconstruction sous la fiche
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "ui_busy")

    def test_row_order_preview_then_playback(self):
        s, clk, p = self._row_preview()
        self.assertTrue(s.ui_event("playback")["preview_cancelled"])
        shown = s.visible()
        self.assertEqual(shown, cm.DEFAULT_SCENE)
        clk.m += 60
        self.assertIs(s.visible(), shown)
        s.ui_event("idle")
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "unknown_preview")

    def test_confirm_refuses_busy_even_with_explicit_idle_argument(self):
        s, _, _ = store(self)
        p = s.preview({"accent": "ember"})
        s.ui_event("setup")
        with self.assertRaises(cm.StoreError) as e:
            s.confirm(p["preview_id"], confirm=True, ui="idle")
        self.assertEqual(e.exception.code, "ui_busy")


# --------------------------------------------------------------------------- entrées hostiles : toujours fail-closed
class TestFailClosedInputs(unittest.TestCase):
    def test_malformed_preview_ids_are_unknown_preview(self):
        s, _, _ = store(self)
        with self.assertRaises(cm.StoreError) as e:                      # aucun aperçu
            s.confirm("é", True)
        self.assertEqual(e.exception.code, "unknown_preview")
        p = s.preview({"accent": "ember"})
        pid = p["preview_id"]
        self.assertRegex(pid, r"\A[0-9a-f]{32}\Z")
        for bad in ("é", "é" * 32, pid.upper(), pid + "0", pid[:-1], " " + pid[1:], "", "０" * 32, "\u0663" * 32,
                    pid[:-1] + "\n", pid.encode(), 123, None, [pid]):
            with self.assertRaises(cm.StoreError, msg=repr(bad)) as e:
                s.confirm(bad, True)
            self.assertEqual(e.exception.code, "unknown_preview", repr(bad))
        self.assertEqual(s.visible().accent, "ember")                     # l'aperçu valide survit aux essais
        self.assertEqual(s.confirm(pid, True)["revision"], 1)

    def good_doc(self):
        s, clk, d = store(self)
        s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        path = os.path.join(d, "scene.json")
        return json.loads(rd(path)), clk, d, path

    @staticmethod
    def rehash(doc):
        body = {k: v for k, v in doc.items() if k != "state_hash"}
        doc["state_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                      ensure_ascii=True).encode()).hexdigest()
        return doc

    def assertCorruptExactly(self, d, clk, reason, label):
        try:
            s = cm.CanvasStore(os.path.join(d, "scene.json"), wall=clk.wall, mono=clk.mono)
        except Exception as e:                                          # jamais d'exception brute au démarrage
            self.fail("%s: %r" % (label, e))
        st = s.status()
        self.assertEqual((st["state"], st["reason"]), ("corrupt", reason), label)
        self.assertEqual(s.visible(), cm.DEFAULT_SCENE, label)

    def test_exact_corrupt_files(self):
        good, clk, d, path = self.good_doc()
        ok_hash = good["history"][0]["hash"]
        cases = [
            # (libellé, mutation, recalcul du hachage global, raison exacte)
            ("at énorme (OverflowError)", lambda x: x["history"][0].__setitem__("at", 10 ** 400), True, "invalid_shape"),
            ("at négatif", lambda x: x["history"][0].__setitem__("at", -1), True, "invalid_shape"),
            ("at au-delà de l'an 9999", lambda x: x["history"][0].__setitem__("at", 10 ** 12), True, "invalid_shape"),
            ("at booléen", lambda x: x["history"][0].__setitem__("at", True), True, "invalid_shape"),
            ("at texte", lambda x: x["history"][0].__setitem__("at", "1800000000"), True, "invalid_shape"),
            ("révision énorme", lambda x: x.__setitem__("revision", 10 ** 400), True, "invalid_shape"),
            ("révision > 2^53", lambda x: x.__setitem__("revision", 2 ** 53), True, "invalid_shape"),
            ("hachage d'entrée non ASCII", lambda x: x["history"][0].__setitem__("hash", "é" * 64), True,
             "invalid_shape"),
            ("hachage d'entrée en majuscules", lambda x: x["history"][0].__setitem__("hash", ok_hash.upper()), True,
             "invalid_shape"),
            ("hachage d'entrée court", lambda x: x["history"][0].__setitem__("hash", ok_hash[:-1]), True,
             "invalid_shape"),
            ("hachage d'entrée bien formé mais faux", lambda x: x["history"][0].__setitem__("hash", "0" * 64), True,
             "hash_mismatch"),
            ("hachage global non ASCII", lambda x: x.__setitem__("state_hash", "é" * 64), False, "invalid_shape"),
            ("hachage global en majuscules", lambda x: x.__setitem__("state_hash", x["state_hash"].upper()), False,
             "invalid_shape"),
            ("hachage global de 63 car.", lambda x: x.__setitem__("state_hash", x["state_hash"][:63]), False,
             "invalid_shape"),
            ("hachage global entier", lambda x: x.__setitem__("state_hash", 7), False, "invalid_shape"),
            ("hachage global bien formé mais faux", lambda x: x.__setitem__("state_hash", "f" * 64), False,
             "hash_mismatch"),
            ("schema_version texte", lambda x: x.__setitem__("schema_version", "1"), True, "invalid_shape"),
        ]
        for label, mutate, rehash, reason in cases:
            doc = json.loads(json.dumps(good))
            mutate(doc)
            if rehash:
                self.rehash(doc)
            with open(path, "w") as f:
                f.write(json.dumps(doc, sort_keys=True, separators=(",", ":")))
            self.assertCorruptExactly(d, clk, reason, label)
        # NaN / Infinity littéraux (json.loads les accepte) : forme invalide, jamais une exception
        for lit in ("NaN", "Infinity", "-Infinity", "1e400"):
            raw = json.dumps(good, sort_keys=True, separators=(",", ":"))
            raw = re.sub(r'"at":[^,}]+', '"at":' + lit, raw)
            with open(path, "w") as f:
                f.write(raw)
            self.assertCorruptExactly(d, clk, "invalid_shape", lit)

    def test_bounded_at_and_hex_helpers_never_raise(self):
        for at in (10 ** 400, -10 ** 400, 2 ** 1100, float("nan"), float("inf"), -1, -0.5, cm.MAX_AT + 1, True, None,
                   "1", [1]):
            self.assertIs(cm._valid_at(at), False, repr(at)[:40])           # aucune conversion flottante
        for at in (0, 1_800_000_000, 1_800_000_000.25, cm.MAX_AT):
            self.assertIs(cm._valid_at(at), True, at)
        good = "a" * 64
        self.assertTrue(cm._is_hex(good, cm._HEX64))
        for bad in ("A" * 64, "é" * 64, "a" * 63, "a" * 65, good + "\n", "g" * 64, "\u0663" * 64, b"a" * 64, None, 64):
            self.assertFalse(cm._is_hex(bad, cm._HEX64), repr(bad)[:30])

    def test_type_value_overflow_errors_while_parsing_become_corrupt(self):
        _, clk, d, path = self.good_doc()
        for exc in (TypeError("x"), ValueError("x"), OverflowError("x")):
            with mock.patch.object(cm, "validate_scene", side_effect=exc):
                self.assertCorruptExactly(d, clk, "invalid_shape", repr(exc))
        self.assertEqual(cm.CanvasStore(path, wall=clk.wall, mono=clk.mono).current().accent, "ember")


# --------------------------------------------------------------------------- persistance fail-closed
class TestPersistence(unittest.TestCase):
    def published(self):
        s, clk, d = store(self)
        s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        return s, clk, d, os.path.join(d, "scene.json")

    def assertCorrupt(self, d, clk, reason=None):
        s = cm.CanvasStore(os.path.join(d, "scene.json"), wall=clk.wall, mono=clk.mono)
        st = s.status()
        self.assertEqual(st["state"], "corrupt", st)
        if reason:
            self.assertEqual(st["reason"], reason)
        self.assertEqual(s.current(), cm.DEFAULT_SCENE)                  # défaut immuable d'OpenTV
        self.assertEqual(s.visible(), cm.DEFAULT_SCENE)
        with self.assertRaises(cm.StoreError) as e:                      # aucune mutation hors réinitialisation
            s.preview({"accent": "jade"})
        self.assertEqual(e.exception.code, "store_corrupt")
        with self.assertRaises(cm.StoreError):
            s.rollback(confirm=True)
        return s

    def test_garbage_truncated_and_tampered_files(self):
        _, clk, d, path = self.published()
        good = rd(path)
        for blob, reason in ((b"", "invalid_json"), (b"{", "invalid_json"), (good[:-7], "invalid_json"),
                             (b"[]", "invalid_shape"), (b"\xff\xfe", "invalid_json"),
                             (b"[" * 100000 + b"]" * 100000, "invalid_json")):
            with open(path, "wb") as f:
                f.write(blob)
            self.assertCorrupt(d, clk, reason)
        doc = json.loads(good)
        doc["history"][-1]["scene"]["accent"] = "jade"                   # contenu modifié, hachage d'origine
        with open(path, "w") as f:
            json.dump(doc, f)
        self.assertCorrupt(d, clk, "hash_mismatch")
        doc = json.loads(good)
        doc["history"][-1]["scene"]["wallpaper"] = "https://evil/x.jpg"
        with open(path, "w") as f:
            json.dump(doc, f)
        self.assertCorrupt(d, clk)
        doc = json.loads(good)
        doc["history"] = doc["history"] * 6
        with open(path, "w") as f:
            json.dump(doc, f)
        self.assertCorrupt(d, clk)

    def test_corrupt_file_is_never_overwritten_except_by_explicit_reset(self):
        _, clk, d, path = self.published()
        with open(path, "wb") as f:
            f.write(b"{garbage")
        s = self.assertCorrupt(d, clk)
        self.assertEqual(rd(path), b"{garbage")
        r = s.reset_default(confirm=True)
        self.assertEqual(s.status()["state"], "default")
        self.assertEqual(r["revision"], 1)
        self.assertEqual(cm.CanvasStore(path).status()["state"], "default")
        self.assertEqual(json.loads(rd(path))["history"], [])

    def test_unsafe_file_types_fail_closed(self):
        _, clk, d, path = self.published()
        good = rd(path)
        os.unlink(path)
        target = os.path.join(d, "elsewhere.json")
        with open(target, "wb") as f:
            f.write(good)
        os.symlink(target, path)
        self.assertCorrupt(d, clk, "unsafe_file")
        os.unlink(path)
        os.link(target, path)                                            # lien dur
        self.assertCorrupt(d, clk, "unsafe_file")
        os.unlink(path)
        os.unlink(target)
        os.mkfifo(path)
        self.assertCorrupt(d, clk, "unsafe_file")
        os.unlink(path)
        os.mkdir(path)
        self.assertCorrupt(d, clk, "unsafe_file")
        os.rmdir(path)
        with open(path, "wb") as f:
            f.write(b" " * (cm.MAX_STORE_BYTES + 1))
        self.assertCorrupt(d, clk, "oversize")

    def test_failed_write_keeps_previous_file_and_memory_state(self):
        s, clk, d, path = self.published()
        before = rd(path)
        p = s.preview({"accent": "jade"})
        real = os.replace

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        with mock.patch.object(cm.os, "replace", boom):
            with self.assertRaises(cm.StoreError) as e:
                s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "write_failed")
        self.assertIs(os.replace, real)
        self.assertEqual(rd(path), before)
        self.assertEqual(sorted(os.listdir(d)), ["scene.json"])          # temporaire nettoyé
        self.assertEqual(s.current().accent, "ember")
        self.assertEqual(s.status()["revision"], 1)

    def test_fsync_of_file_and_directory(self):
        s, clk, d = store(self)
        calls = []
        real = os.fsync

        def spy(fd):
            calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)
        with mock.patch.object(cm.os, "fsync", spy):
            s.confirm(s.preview({"accent": "ember"})["preview_id"], confirm=True)
        self.assertIn(False, calls)                                      # fichier temporaire
        self.assertIn(True, calls)                                       # répertoire après rename
        self.assertLess(calls.index(False), calls.index(True))

    def test_readback_mismatch_is_reported_and_rolled_back(self):
        s, clk, d, path = self.published()
        before = rd(path)
        real = cm._read_store_bytes

        def tamper(dirfd, name):
            data = real(dirfd, name)
            return data.replace(b"jade", b"ice!")
        p = s.preview({"accent": "jade"})
        with mock.patch.object(cm, "_read_store_bytes", tamper):
            with self.assertRaises(cm.StoreError) as e:
                s.confirm(p["preview_id"], confirm=True)
        self.assertEqual(e.exception.code, "readback_failed")
        self.assertEqual(rd(path), before)               # ancien contenu restauré
        self.assertEqual(s.current().accent, "ember")

    def test_store_document_is_canonical_and_self_hashed(self):
        _, _, _, path = self.published()
        raw = rd(path)
        doc = json.loads(raw)
        self.assertEqual(sorted(doc), ["history", "revision", "schema_version", "state_hash"])
        body = {k: v for k, v in doc.items() if k != "state_hash"}
        self.assertEqual(doc["state_hash"],
                         hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=True).encode()).hexdigest())
        self.assertEqual(doc["history"][0]["hash"], cm.scene_hash(cm.validate_scene(doc["history"][0]["scene"])))


# --------------------------------------------------------------------------- laboratoire visuel : contrôles statiques
class TestLabStatic(unittest.TestCase):
    ES6 = re.compile(r"(?<![\w$.])(let|const|class)\s|=>|`|\.\.\.[A-Za-z_$\[]|\bfor\s*\([^)]*\bof\b")

    def read(self, name):
        with open(os.path.join(CANVAS, name), encoding="utf-8") as f:
            return f.read()

    def test_files_exist_and_are_self_contained(self):
        for name in LAB_FILES + ("README.md", "canvas_model.py"):
            self.assertTrue(os.path.isfile(os.path.join(CANVAS, name)), name)
        html_ = self.read("lab.html")
        refs = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', html_)
        self.assertEqual(sorted(refs), ["canvas.js", "lab.css"])
        for name in LAB_FILES:
            low = self.read(name).lower().replace("http-equiv", "")
            for bad in ("http", "//cdn", "@import", "<iframe", "<video", "<object", "<embed", "<img", "<form",
                        "<input", "<textarea", "contenteditable", "worker(", "eval(", "new function", "innerhtml",
                        "outerhtml", "insertadjacenthtml", "document.write", "localstorage", "sessionstorage",
                        "xmlhttprequest", "fetch(", "websocket", "backdrop-filter", "filter:", "blur(", "url(",
                        "import(", "settimeout(\"", "setinterval(\""):
                self.assertNotIn(bad, low, (name, bad))
        self.assertIn("Content-Security-Policy", html_)
        self.assertIn("default-src 'none'", html_)

    def test_css_animations_and_transitions_only_transform_opacity(self):
        css = re.sub(r"/\*.*?\*/", "", self.read("lab.css"), flags=re.S)
        for m in re.finditer(r"transition(?:-property)?\s*:\s*([^;}]+)", css):
            for part in m.group(1).split(","):
                prop = part.strip().split()[0]
                self.assertIn(prop, ("transform", "opacity", "none"), m.group(0))
        for m in re.finditer(r"@keyframes\s+[\w-]+\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", css):
            for decl in re.findall(r"\{([^{}]*)\}", m.group(1)):
                for prop in re.findall(r"([\w-]+)\s*:", decl):
                    self.assertIn(prop, ("transform", "opacity"), m.group(0)[:80])
        self.assertIn("prefers-reduced-motion", css)
        self.assertIsNone(re.search(r"will-change\s*:(?!\s*(?:transform|opacity)\b)", css))

    def test_es5_tokens(self):
        code = "\n".join(x for x in self.read("canvas.js").splitlines()
                         if not x.lstrip().startswith(("//", "*", "/*")))
        m = self.ES6.search(code)
        self.assertIsNone(m, m and code[max(0, m.start() - 40):m.end() + 20])

    @unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_ACORN") and
                         os.path.isfile(os.path.join(os.environ.get("OPENTV_TEST_ACORN", ""), "package.json")),
                         "acorn absent (OPENTV_TEST_ACORN=<chemin du module>)")
    def test_acorn_ecma5(self):
        code = ("var a=require(process.argv[1]),fs=require('fs');"
                "a.parse(fs.readFileSync(process.argv[2],'utf8'),{ecmaVersion:5});")
        p = subprocess.run([NODE, "-e", code, os.environ["OPENTV_TEST_ACORN"], os.path.join(CANVAS, "canvas.js")],
                           capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_deadlines_use_monotonic_time_only(self):
        """Échéances d'aperçu et fenêtre de silence : performance.now() (ou repli monotone), jamais Date.now."""
        js = self.read("canvas.js")
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", js, flags=re.S)
        self.assertEqual(len(re.findall(r"Date\.now\s*\(", code)), 2, "Date.now : horloge murale + repli seulement")
        self.assertIn("function wallNow() { return Date.now(); }", code)
        self.assertIn('typeof window.performance.now === "function"', code)
        m = re.search(r"function mono\(\) \{(.*?)\n  \}", code, re.S)
        self.assertIsNotNone(m)
        self.assertIn("perf.now()", m.group(1))
        for fn in ("previewLive", "busyReason", "tickPreview", "preview", "drawPreviewChip", "snapshot"):
            body = re.search(r"function %s\([^)]*\) \{(.*?)\n  \}" % fn, code, re.S)
            self.assertIsNotNone(body, fn)
            self.assertNotIn("wallNow", body.group(1), fn)
            self.assertNotIn("Date", body.group(1), fn)
        key = re.search(r"function onKey\(e\) \{(.*?)\n  \}", code, re.S).group(1)
        self.assertIn("S.lastKey = mono();", key)
        self.assertNotIn("1322", code)                                   # largeur de rangée mesurée

    def test_js_enums_match_python_model(self):
        js = self.read("canvas.js")
        m = re.search(r"var ENUMS = (\{.*?\});", js, re.S)
        self.assertIsNotNone(m)
        enums = json.loads(m.group(1))
        self.assertEqual(enums, {k: list(v) for k, v in cm.ENUMS.items()})
        m = re.search(r"var ROWS = (\[.*?\]);", js, re.S)
        self.assertEqual(json.loads(m.group(1)), list(cm.ROW_IDS))
        m = re.search(r"var PRESETS = (\{.*?\});\n", js, re.S)
        presets = json.loads(m.group(1))
        self.assertEqual(presets, {k: v.to_dict() for k, v in cm.PRESETS.items()})


# --------------------------------------------------------------------------- non-impact production (durable)
PROTECTED = ("scripts/pi4/ui", "scripts/pi4/opentv_pi", "scripts/pi4/install.sh", "scripts/pi4/prepare-pi.sh",
             "scripts/pi4/uninstall.sh", "scripts/pi4/systemd", "scripts/pi4/system", "scripts/pi4/vendor",
             "scripts/pi4/bin", "scripts/pi4/tools", "scripts/nanopi", "native")
PROTO_MARKERS = re.compile(r"canvas_model|prototypes[/\\]canvas|OTVCanvas|e2e_canvas_prototype")
# modules internes de production importés relativement dans opentv_pi (jamais le prototype `canvas_model`)
PRODUCTION_CANVAS_MODULES = frozenset(("canvas", "canvas_schema", "canvas_storage", "canvas_activity",
                                       "canvas_roles", "canvas_tv"))


def production_files():
    for rel in PROTECTED:
        top = os.path.join(ROOT, rel)
        if os.path.isfile(top):
            yield top
            continue
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "node_modules", "test", "tests")]
            for name in files:
                yield os.path.join(d, name)


class TestNonImpact(unittest.TestCase):
    """Garanties durables (indépendantes de tout commit de référence) : l'installation et le manifeste ne livrent
    jamais le prototype, et aucun code de production ne l'importe ni ne le nomme."""

    def test_installer_and_manifest_never_include_prototypes(self):
        for rel in ("scripts/pi4/install.sh", "scripts/pi4/uninstall.sh", "scripts/pi4/prepare-pi.sh"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertNotIn("prototypes", f.read(), rel)
        p = subprocess.run(["bash", os.path.join(PI4, "install.sh"), "--dry-run"], capture_output=True, timeout=120,
                           cwd=PI4)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        out = p.stdout.decode() + p.stderr.decode()
        copies = re.findall(r"^\[dry-run\] cp -- (\S+) (\S+)$", out, re.M)
        m = re.search(r"^\[dry-run\] manifeste \S+ : (\d+) fichiers$", out, re.M)
        self.assertIsNotNone(m, "liste du manifeste introuvable dans --dry-run")
        self.assertGreater(len(copies), 50)
        self.assertGreaterEqual(len(copies), int(m.group(1)))          # tout fichier manifesté est copié
        proto = os.path.join(PI4, "prototypes")
        proto_bytes = set()
        for d, _, names in os.walk(proto):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    proto_bytes.add(f.read())
        prod_canvas = os.path.realpath(os.path.join(PI4, "ui", "canvas.js"))
        for src, dst in copies:
            self.assertFalse(os.path.realpath(src).startswith(proto + os.sep), src)
            self.assertNotRegex(dst, r"(?i)prototype|canvas_model|lab\.(?:html|css)", dst)
            if re.search(r"(?i)canvas\.js", dst):   # seul le moteur de production (ui/canvas.js), jamais celui du prototype
                self.assertEqual(os.path.realpath(src), prod_canvas, (src, dst))
                self.assertRegex(dst, r"^/opt/opentv/share/ui/canvas\.js(?:\.opentv-tmp)?$", dst)
            with open(src, "rb") as f:
                self.assertNotIn(f.read(), proto_bytes, src)   # aucun fichier livré n'est une copie du prototype
        self.assertNotRegex(out, r"(?i)prototypes|canvas_model")

    def test_production_code_never_imports_or_names_the_prototype(self):
        import ast
        seen = 0
        for path in production_files():
            with open(path, "rb") as f:
                data = f.read()
            seen += 1
            text = data.decode("utf-8", "replace")
            self.assertIsNone(PROTO_MARKERS.search(text), path)
            if path.endswith(".py") or text.startswith("#!/usr/bin/env python") or text.startswith("#!/usr/bin/python"):
                try:
                    tree = ast.parse(data)
                except SyntaxError:
                    continue
                in_pkg = os.path.dirname(path) == os.path.join(PI4, "opentv_pi")
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""] + [a.name for a in node.names]
                        if in_pkg and node.level == 1:
                            # modules de PRODUCTION du paquet (opentv_pi.canvas*) et leurs symboles, jamais le
                            # prototype : `from .canvas_schema import CanvasError`, `from . import canvas_storage`
                            if node.module in PRODUCTION_CANVAS_MODULES:
                                names = []
                            elif node.module is None:
                                names = [a.name for a in node.names if a.name not in PRODUCTION_CANVAS_MODULES]
                    for n in names:
                        self.assertNotIn("canvas", n.lower(), (path, n))
                        self.assertNotIn("prototypes", n.lower(), (path, n))
        self.assertGreater(seen, 50)

    def test_production_package_import_does_not_load_prototype(self):
        # le module de production opentv_pi.canvas (phase 2) est légitime ; seul le PROTOTYPE est interdit : aucun
        # module nommé comme lui ni chargé depuis prototypes/
        code = ("import os,sys,pkgutil,importlib;sys.path.insert(0,sys.argv[1]);import opentv_pi;"
                "[importlib.import_module('opentv_pi.'+m.name) for m in pkgutil.iter_modules(opentv_pi.__path__)];"
                "proto=os.path.join(os.path.realpath(sys.argv[1]),'prototypes')+os.sep;"
                "bad=[n for n,m in list(sys.modules.items()) if 'canvas_model' in n.lower() or "
                "os.path.realpath(getattr(m,'__file__',None) or '/').startswith(proto)];"
                "print(bad);sys.exit(1 if bad or 'opentv_pi.canvas' not in sys.modules else 0)")
        p = subprocess.run([sys.executable, "-I", "-c", code, PI4], capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, (p.stdout.decode()[-1000:], p.stderr.decode()[-2000:]))

    @unittest.skipUnless(os.environ.get("OPENTV_CANVAS_BASELINE"),
                         "revue seulement : OPENTV_CANVAS_BASELINE=<commit> compare les chemins de production")
    def test_review_only_production_paths_identical_to_baseline(self):
        base = os.environ["OPENTV_CANVAS_BASELINE"]
        self.assertRegex(base, r"\A[0-9A-Za-z._/-]{1,80}\Z")
        p = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", base, "--", *PROTECTED],
                           capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.decode().strip(), "")


# --------------------------------------------------------------------------- vrai Chromium 1920×1080 (Playwright)
class _LabHandler(http.server.SimpleHTTPRequestHandler):
    """Serveur borné : seulement les trois fichiers du laboratoire, en lecture."""

    def do_GET(self):
        name = self.path.split("?", 1)[0].lstrip("/") or "lab.html"
        if name not in LAB_FILES:
            self.send_error(404)
            return
        self.path = "/" + name
        return super().do_GET()

    def do_HEAD(self):
        self.send_error(405)

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def serve_lab():
    handler = functools.partial(_LabHandler, directory=CANVAS)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.timeout = 5
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d/lab.html" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def find_chromium():
    from tests.test_service import find_chromium as f
    return f()


def playwright_dir():
    p = os.environ.get("OPENTV_TEST_PLAYWRIGHT")
    return p if p and os.path.isfile(os.path.join(p, "package.json")) else None


def acorn_dir():
    p = os.environ.get("OPENTV_TEST_ACORN")
    return p if p and os.path.isfile(os.path.join(p, "package.json")) else None


class TestLabServer(unittest.TestCase):
    def test_server_serves_only_lab_files(self):
        import urllib.request
        import urllib.error
        with serve_lab() as url:
            base = url.rsplit("/", 1)[0]
            for name in LAB_FILES:
                with urllib.request.urlopen(base + "/" + name, timeout=5) as r:
                    self.assertEqual(r.status, 200)
            for bad in ("/canvas_model.py", "/README.md", "/../../ui/tv.js", "/%2e%2e/%2e%2e/ui/tv.css"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + bad, timeout=5)
                self.assertEqual(e.exception.code, 404)


@unittest.skipUnless(NODE and playwright_dir(), "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + node")
class TestLabEndToEnd(unittest.TestCase):
    def test_three_variants_real_chromium(self):
        chromium = find_chromium()
        if not chromium:
            self.skipTest("Chromium introuvable")
        shots = os.environ.get("OPENTV_CANVAS_SHOTS") or tmpdir(self)
        os.makedirs(shots, exist_ok=True)
        with serve_lab() as url:
            cfg = {"chromium": chromium, "url": url, "shots": shots}
            acorn = acorn_dir()
            if acorn:
                cfg["acorn"] = acorn
            p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_canvas_prototype.js"), playwright_dir(),
                                json.dumps(cfg)], capture_output=True, timeout=240)
        self.assertEqual(p.returncode, 0, (p.stdout.decode()[-3000:], p.stderr.decode()[-3000:]))
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(sorted(res["variants"]), ["canvas", "cinema", "mist"])
        for v in ("mist", "cinema", "canvas"):
            r = res["variants"][v]
            self.assertTrue(r["geometry_ok"], r)
            self.assertLessEqual(r["running_layers"], 4, r)
            self.assertGreaterEqual(r["running_layers"], 1, r)
            self.assertGreaterEqual(r["min_font_px"], 17, r)
            self.assertTrue(os.path.getsize(os.path.join(shots, v + ".png")) > 50000, v)
        self.assertEqual(res["failures"], [], res["failures"])
        self.assertEqual(res["acorn"], "ok" if acorn_dir() else "absent")


if __name__ == "__main__":
    unittest.main()
