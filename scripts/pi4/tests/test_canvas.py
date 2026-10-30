"""OpenTV Canvas (production, tâche 1) : domaine de scène fermé et magasin persistant fail-closed.

Couvre : schéma/défaut/préréglages fermés, hachage canonique, révisions, aperçus agents (identifiants hexadécimaux,
échéance monotone revérifiée après le verrou inter-processus et la relecture), raisons d'occupation imbriquées
(jetons), annulation sur événement humain, séparation stricte agent (aperçu/annulation) / propriétaire
(confirmation/application/retour arrière/réinitialisation), écriture atomique (fsync, relecture, restauration),
historique des cinq dernières versions et refus de tout état dangereux ou corrompu."""
import errno
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests._common import PI4

from opentv_pi import canvas as cv  # noqa: E402


class Clock:
    def __init__(self, wall=1_800_000_000.0, mono=5000.0):
        self.w, self.m = wall, mono

    def wall(self):
        return self.w

    def mono(self):
        return self.m


def rd(path):
    with open(path, "rb") as f:
        return f.read()


def open_fds():
    d = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
    return len(os.listdir(d))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="otvcv")
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dir = os.path.join(self.root, "canvas")
        self.file = os.path.join(self.dir, cv.STATE_NAME)
        self.clock = Clock()

    def store(self, **kw):
        kw.setdefault("wall", self.clock.wall)
        kw.setdefault("mono", self.clock.mono)
        kw.setdefault("lock_timeout", 0.3)
        return cv.CanvasStore(self.dir, **kw)

    def code(self, cm):
        return cm.exception.code

    def publish(self, s, changes, agent="hermit"):
        p = s.agent_preview(agent, changes)
        return s.owner_confirm(p["preview_id"], confirm=True)


# --------------------------------------------------------------------------- schéma
class TestSchema(unittest.TestCase):
    def test_closed_fields_and_enums(self):
        self.assertEqual(cv.FIELDS, ("wallpaper", "motion", "accent", "shade", "hero_focus", "row_order"))
        self.assertEqual(set(cv.ENUMS), {"wallpaper", "motion", "accent", "shade", "hero_focus"})
        self.assertEqual(cv.ENUMS["motion"], ("off", "calm", "alive"))
        self.assertEqual(sorted(cv.PRESETS), ["canvas", "cinema", "mist"])
        self.assertEqual(cv.DEFAULT_PRESET, "canvas")
        self.assertIs(cv.DEFAULT_SCENE, cv.PRESETS["canvas"])
        self.assertEqual(cv.preset_of(cv.DEFAULT_SCENE), "canvas")
        for name, sc in cv.PRESETS.items():
            self.assertEqual(cv.validate_scene(sc.to_dict()), sc)
            self.assertEqual(cv.preset_of(sc), name)
        self.assertEqual(len({cv.scene_hash(s) for s in cv.PRESETS.values()}), 3)

    def test_every_enum_value_is_a_plain_token(self):
        for k, vals in cv.ENUMS.items():
            for v in vals:
                self.assertRegex(v, r"\A[a-z]{2,16}\Z", k)
        for r in cv.ROW_IDS:
            self.assertRegex(r, r"\A[a-z]{2,16}\Z")

    def test_hash_is_canonical_lower_hex(self):
        d = cv.DEFAULT_SCENE.to_dict()
        rev = dict(reversed(list(d.items())))
        self.assertEqual(cv.scene_hash(cv.validate_scene(rev)), cv.scene_hash(cv.DEFAULT_SCENE))
        self.assertRegex(cv.scene_hash(cv.DEFAULT_SCENE), r"\A[0-9a-f]{64}\Z")
        self.assertEqual(cv.canonical_json(cv.DEFAULT_SCENE),
                         json.dumps(d, sort_keys=True, separators=(",", ":")))

    def test_rejects_anything_outside_the_closed_schema(self):
        good = cv.DEFAULT_SCENE.to_dict()
        hostile = ["https://evil.example/x.jpg", "url(javascript:alert(1))", "<img src=x>", "/etc/passwd",
                   "../../x", "aurore;background:red", "AURORE", "aurore ", "", None, 1, True, ["aurore"],
                   {"id": "aurore"}, "aur\u00f6re", "\ud800"]

        class S(str):
            pass

        for k in cv.ENUMS:
            for v in hostile + [S(good[k])]:
                d = dict(good, **{k: v})
                with self.assertRaises(cv.SceneError, msg=(k, v)):
                    cv.validate_scene(d)
        for extra in ({"css": "x"}, {"url": "https://x"}, {"html": "<b>"}, {"asset": "x.png"}, {1: 2}):
            d = dict(good)
            d.update(extra)
            with self.assertRaises(cv.SceneError):
                cv.validate_scene(d)
        for k in list(good):
            d = dict(good)
            del d[k]
            with self.assertRaises(cv.SceneError):
                cv.validate_scene(d)
        for sv in (2, 0, "1", 1.0, True):
            with self.assertRaises(cv.SceneError):
                cv.validate_scene(dict(good, schema_version=sv))
        for v in ("x", [], list(reversed(cv.ROW_IDS)), ["tonight"], cv.ROW_IDS[:-1] + ("tonight",),
                  list(cv.ROW_IDS) + ["extra"], ["tonight", "new", "top", "watchlist", 5],
                  [["tonight"]], {"tonight": 1}):
            with self.assertRaises(cv.SceneError, msg=v):
                cv.validate_scene(dict(good, row_order=v))
        for bad in (None, [], "scene", 3):
            with self.assertRaises(cv.SceneError):
                cv.validate_scene(bad)

    def test_row_order_is_a_permutation_with_tonight_first(self):
        d = dict(cv.DEFAULT_SCENE.to_dict(), row_order=["tonight", "more", "top", "new", "watchlist"])
        self.assertEqual(cv.validate_scene(d).row_order[0], "tonight")
        with self.assertRaises(cv.SceneError):
            cv.validate_scene(dict(d, row_order=["top", "tonight", "more", "new", "watchlist"]))

    def test_apply_changes_closed_flat_keys(self):
        sc = cv.apply_changes(cv.DEFAULT_SCENE, {"preset": "cinema", "accent": "jade"})
        self.assertEqual(sc.wallpaper, cv.PRESETS["cinema"].wallpaper)
        self.assertEqual(sc.accent, "jade")
        for bad in ({}, None, [], {"preset": "evil"}, {"scene": cv.DEFAULT_SCENE.to_dict()},
                    {"schema_version": 1}, {"accent": "red"}, {"preset": ["canvas"]}):
            with self.assertRaises(cv.SceneError, msg=bad):
                cv.apply_changes(cv.DEFAULT_SCENE, bad)


# --------------------------------------------------------------------------- aperçu et événements humains
class TestPreview(Base):
    def test_default_without_state_and_preview_is_memory_only(self):
        s = self.store()
        st = s.status()
        self.assertEqual((st["state"], st["revision"], st["preset"]), ("default", 0, "canvas"))
        self.assertEqual(st["hash"], cv.scene_hash(cv.DEFAULT_SCENE))
        p = s.agent_preview("hermit", {"preset": "mist"})
        self.assertRegex(p["preview_id"], r"\A[0-9a-f]{32}\Z")
        self.assertRegex(p["hash"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(s.visible_scene(), cv.PRESETS["mist"])
        self.assertEqual(s.current_scene(), cv.DEFAULT_SCENE)
        self.assertEqual(s.status()["preview"]["agent"], "hermit")
        self.assertFalse(os.path.exists(self.dir))        # aucun disque touché par un aperçu

    def test_monotonic_expiry_ignores_wall_clock(self):
        s = self.store(preview_ttl_s=30)
        p = s.agent_preview("hermit", {"preset": "mist"})
        self.clock.w -= 86400 * 3
        self.assertIsNotNone(s.status()["preview"])
        self.clock.w += 86400 * 30
        self.assertIsNotNone(s.status()["preview"])
        self.clock.m += 30
        self.assertIsNone(s.status()["preview"])
        self.assertEqual(s.visible_scene(), cv.DEFAULT_SCENE)
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertIn(self.code(cm), ("preview_expired", "unknown_preview"))
        self.assertFalse(os.path.exists(self.file))

    def test_expiry_rechecked_after_lock_and_reload(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        self.clock.m += 29.9

        def late():
            self.clock.m += 1          # l'attente du verrou dépasse l'échéance
        s._after_lock = late
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "preview_expired")
        self.assertFalse(os.path.exists(self.file))

    def test_human_event_during_lock_wait_wins(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        s._after_lock = lambda: s.human_event("key")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "unknown_preview")
        self.assertFalse(os.path.exists(self.file))

    def test_busy_entered_during_lock_wait_wins(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        s._after_lock = lambda: s.busy_enter("playback")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertIn(self.code(cm), ("unknown_preview", "ui_busy"))
        self.assertFalse(os.path.exists(self.file))

    def test_confirm_requires_literal_true_and_exact_id(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        for c in (False, 1, "yes", None, [True]):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_confirm(p["preview_id"], confirm=c)
            self.assertEqual(self.code(cm), "confirmation_required")
        for pid in (p["preview_id"].upper(), p["preview_id"][:-1], "é" * 32, "\u0660" * 32, 5, None,
                    b"0" * 32, "0" * 32, [p["preview_id"]]):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_confirm(pid, confirm=True)
            self.assertEqual(self.code(cm), "unknown_preview", pid)
        self.assertIsNotNone(s.status()["preview"])
        r = s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(r["revision"], 1)
        with self.assertRaises(cv.CanvasError) as cm:      # usage unique
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "unknown_preview")

    def test_agent_facade_cannot_confirm_or_publish(self):
        s = self.store()
        a = s.agent_api("hermit")
        public = {n for n in dir(a) if not n.startswith("_")}
        self.assertEqual(public, {"status", "preview", "cancel"})
        for forbidden in ("confirm", "owner_confirm", "apply", "rollback", "reset", "owner_apply", "store"):
            self.assertFalse(hasattr(a, forbidden), forbidden)
        p = a.preview({"preset": "mist"})
        self.assertNotIn("history", a.status())
        self.assertEqual(a.status()["preview"]["preview_id"], p["preview_id"])
        self.assertTrue(a.cancel(p["preview_id"]))
        self.assertFalse(os.path.exists(self.file))
        o = s.owner_api()
        self.assertEqual({n for n in dir(o) if not n.startswith("_")},
                         {"status", "confirm", "reject", "apply", "rollback", "reset"})

    def test_agent_cannot_cancel_or_see_another_agents_preview(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_cancel("muse", p["preview_id"])
        self.assertEqual(self.code(cm), "unknown_preview")
        self.assertIsNone(s.agent_api("muse").status()["preview"])
        self.assertIsNotNone(s.status()["preview"])

    def test_one_live_preview_at_a_time(self):
        s = self.store()
        s.agent_preview("hermit", {"preset": "mist"})
        for ag in ("hermit", "muse"):
            with self.assertRaises(cv.CanvasError) as cm:
                s.agent_preview(ag, {"preset": "cinema"})
            self.assertEqual(self.code(cm), "preview_pending")
        self.clock.m += 31
        s.agent_preview("muse", {"preset": "cinema"})

    def test_preview_input_validation(self):
        s = self.store()
        for ag in ("", "Hermit", "../x", None, 3, "a" * 80):
            with self.assertRaises(cv.CanvasError) as cm:
                s.agent_preview(ag, {"preset": "mist"})
            self.assertEqual(self.code(cm), "invalid_agent")
        for ch in ({}, {"preset": "evil"}, {"wallpaper": "https://x"}, {"scene": {}}, "mist", None):
            with self.assertRaises(cv.CanvasError) as cm:
                s.agent_preview("hermit", ch)
            self.assertEqual(self.code(cm), "invalid_scene")
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_preview("hermit", {"preset": "canvas"})
        self.assertEqual(self.code(cm), "no_change")
        with self.assertRaises(ValueError):
            self.store(preview_ttl_s=1)
        with self.assertRaises(ValueError):
            self.store(preview_ttl_s=True)

    def test_human_key_cancels_and_opens_quiet_window(self):
        s = self.store()
        s.agent_preview("hermit", {"preset": "mist"})
        r = s.human_event("key")
        self.assertTrue(r["preview_cancelled"])
        self.assertIsNone(s.status()["preview"])
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_preview("hermit", {"preset": "mist"})
        self.assertEqual(self.code(cm), "ui_busy")
        self.assertIn("navigating", s.busy_reasons())
        self.clock.m += cv.NAV_QUIET_S
        self.assertEqual(s.busy_reasons(), ())
        p = s.agent_preview("hermit", {"preset": "mist"})
        s.human_event("pointer")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "ui_busy")
        with self.assertRaises(cv.CanvasError) as cm:
            s.human_event("evil")
        self.assertEqual(self.code(cm), "invalid_event")

    def test_nested_busy_reasons_are_tokens_not_a_single_state(self):
        s = self.store()
        s.agent_preview("hermit", {"preset": "mist"})
        t1 = s.busy_enter("panel")
        self.assertIsNone(s.status()["preview"])           # toute entrée occupée annule l'aperçu
        self.assertRegex(t1, r"\A[0-9a-f]{32}\Z")
        t2 = s.busy_enter("panel")
        t3 = s.busy_enter("playback")
        self.assertEqual(s.busy_reasons(), ("panel", "playback"))
        self.assertTrue(s.busy_exit(t3))
        self.assertFalse(s.busy_exit(t3))                  # double sortie : sans effet sur les autres
        self.assertEqual(s.busy_reasons(), ("panel",))
        self.assertTrue(s.busy_exit(t1))
        self.assertEqual(s.busy_reasons(), ("panel",))     # panneau imbriqué toujours ouvert
        for bad in ("0" * 32, t1.upper(), None, 7, "x"):
            self.assertFalse(s.busy_exit(bad))
        self.assertEqual(s.busy_reasons(), ("panel",))
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_preview("hermit", {"preset": "mist"})
        self.assertEqual(self.code(cm), "ui_busy")
        self.assertTrue(s.busy_exit(t2))
        self.assertEqual(s.busy_reasons(), ())
        s.agent_preview("hermit", {"preset": "mist"})
        with self.assertRaises(cv.CanvasError) as cm:
            s.busy_enter("evil")
        self.assertEqual(self.code(cm), "invalid_event")
        for r in cv.BUSY_REASONS:
            s.busy_exit(s.busy_enter(r))

    def test_busy_scope_context_manager_exits_on_error(self):
        s = self.store()
        with self.assertRaises(RuntimeError):
            with s.busy_scope("loading"):
                self.assertEqual(s.busy_reasons(), ("loading",))
                raise RuntimeError
        self.assertEqual(s.busy_reasons(), ())

    def test_busy_overflow_is_sticky_until_explicit_reset(self):
        s = self.store()
        for _ in range(cv.MAX_BUSY_TOKENS):
            s.busy_enter("panel")
        with self.assertRaises(cv.CanvasError) as cm:
            s.busy_enter("panel")
        self.assertEqual(self.code(cm), "busy_overflow")
        self.assertIn("overflow", s.busy_reasons())
        s.busy_reset()
        self.assertEqual(s.busy_reasons(), ())

    def test_owner_reject_cancels(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        self.assertTrue(s.owner_reject(p["preview_id"]))
        self.assertFalse(s.owner_reject(p["preview_id"]))
        self.assertIsNone(s.status()["preview"])


# --------------------------------------------------------------------------- persistance
class TestPersistence(Base):
    def test_confirm_publishes_atomically_with_safe_modes(self):
        s = self.store()
        r = self.publish(s, {"preset": "mist"})
        self.assertEqual(r["revision"], 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        st = os.stat(self.file)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_nlink), (0o600, os.geteuid(), 1))
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([cv.STATE_NAME, cv.LOCK_NAME]))
        s2 = self.store()
        self.assertEqual(s2.current_scene(), cv.PRESETS["mist"])
        st2 = s2.status()
        self.assertEqual((st2["state"], st2["revision"], st2["preset"]), ("applied", 1, "mist"))
        self.assertEqual(st2["history"][-1]["agent"], "hermit")
        self.assertIsNone(st2["preview"])
        doc = json.loads(rd(self.file))
        self.assertRegex(doc["state_hash"], r"\A[0-9a-f]{64}\Z")
        self.assertRegex(doc["epoch"], r"\A[0-9a-f]{32}\Z")

    def test_status_never_leaks_paths(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        s.agent_preview("hermit", {"preset": "cinema"})
        for view in (s.status(), s.agent_api("hermit").status()):
            text = json.dumps(view)
            self.assertNotIn(self.root, text)
            self.assertNotIn(cv.STATE_NAME, text)

    def test_keeps_last_five_and_rolls_back_exactly_then_default(self):
        s = self.store()
        seq = [{"accent": a} for a in ("ember", "jade", "violet")] + [{"preset": "mist"}, {"preset": "cinema"},
                                                                       {"shade": "soft"}, {"motion": "off"}]
        scenes = []
        for ch in seq:
            self.publish(s, ch)
            scenes.append(s.current_scene())
            self.clock.m += 1
        st = s.status()
        self.assertEqual(st["revision"], 7)
        self.assertEqual([e["revision"] for e in st["history"]], [3, 4, 5, 6, 7])
        o = s.owner_api()
        for want in reversed(scenes[2:6]):
            r = o.rollback(confirm=True)
            self.assertEqual(s.current_scene(), want)
            self.assertEqual(r["hash"], cv.scene_hash(want))
        o.rollback(confirm=True)
        self.assertEqual(s.current_scene(), cv.DEFAULT_SCENE)
        self.assertEqual(s.status()["revision"], 12)
        with self.assertRaises(cv.CanvasError) as cm:
            o.rollback(confirm=True)
        self.assertEqual(self.code(cm), "nothing_to_rollback")
        with self.assertRaises(cv.CanvasError) as cm:
            o.rollback()
        self.assertEqual(self.code(cm), "confirmation_required")

    def test_owner_apply_and_reset(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        o = s.owner_api()
        with self.assertRaises(cv.CanvasError) as cm:
            o.apply({"preset": "cinema"})
        self.assertEqual(self.code(cm), "confirmation_required")
        tok = s.busy_enter("panel")                        # Réglages ouverts : l'action du propriétaire passe
        r = o.apply({"preset": "cinema"}, confirm=True)
        s.busy_exit(tok)
        self.assertEqual(r["revision"], 1)
        self.assertEqual(s.current_scene(), cv.PRESETS["cinema"])
        self.assertIsNone(s.status()["preview"])            # aperçu agent annulé par l'action du propriétaire
        with self.assertRaises(cv.CanvasError):
            o.confirm(p["preview_id"], confirm=True)
        self.assertIsNone(s.status()["history"][-1]["agent"])
        r = o.reset(confirm=True)
        st = s.status()
        self.assertEqual((st["state"], st["revision"], st["history"]), ("default", 2, []))
        self.assertEqual(s.current_scene(), cv.DEFAULT_SCENE)

    def test_stale_preview_after_foreign_write(self):
        a, b = self.store(), self.store()
        p = a.agent_preview("hermit", {"preset": "mist"})
        b.owner_api().apply({"preset": "cinema"}, confirm=True)
        with self.assertRaises(cv.CanvasError) as cm:
            a.owner_confirm(p["preview_id"], confirm=True)
        self.assertIn(self.code(cm), ("stale_preview", "unknown_preview"))
        self.assertEqual(self.store().current_scene(), cv.PRESETS["cinema"])

    def test_preview_dropped_when_reset_reuses_revision(self):
        a = self.store()
        self.publish(a, {"preset": "mist"})
        p = a.agent_preview("hermit", {"preset": "cinema"})
        with open(self.file, "wb") as f:                      # corrompu par un tiers…
            f.write(b"{")
        self.store().owner_api().reset(confirm=True)          # …puis réinitialisé (révision 1 de nouveau)
        with self.assertRaises(cv.CanvasError) as cm:
            a.owner_confirm(p["preview_id"], confirm=True)
        self.assertIn(self.code(cm), ("stale_preview", "unknown_preview"))
        self.assertEqual(self.store().current_scene(), cv.DEFAULT_SCENE)

    def test_cross_process_lock_busy_writes_nothing(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        before = rd(self.file)
        fd = os.open(os.path.join(self.dir, cv.LOCK_NAME), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        p = s.agent_preview("hermit", {"preset": "cinema"})
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "store_busy")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_api().rollback(confirm=True)
        self.assertEqual(self.code(cm), "store_busy")
        self.assertEqual(rd(self.file), before)
        fcntl.flock(fd, fcntl.LOCK_UN)
        s.owner_confirm(p["preview_id"], confirm=True)

    def test_concurrent_processes_never_lose_updates(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        self.run_concurrent(6, 1)

    def test_concurrent_first_creation_of_dir_and_lock(self):
        for _ in range(3):
            shutil.rmtree(self.dir, True)
            self.run_concurrent(6, 0)
            self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, cv.LOCK_NAME)).st_mode), 0o600)

    def run_concurrent(self, n, seeded):
        # chaque processus réussit exactement UNE publication (la relecture sous verrou peut révéler qu'une écriture
        # concurrente a déjà produit la même scène : no_change, nouvel essai avec une autre valeur)
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from opentv_pi import canvas as cv;"
                "s=cv.CanvasStore(sys.argv[2],lock_timeout=20);o=s.owner_api();i=int(sys.argv[3])\n"
                "for k in range(40):\n"
                "  acc=cv.ENUMS['accent'][(i+k)%4]\n"
                "  try:\n"
                "    o.apply({'accent':acc},confirm=True);break\n"
                "  except cv.CanvasError as e:\n"
                "    assert e.code=='no_change',e.code\n"
                "else:\n"
                "  raise SystemExit('no success')\n")
        procs = [subprocess.Popen([sys.executable, "-I", "-c", code, PI4, self.dir, str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(n)]
        errs = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
            if p.returncode != 0:
                errs.append(err.decode()[-800:])
        self.assertEqual(errs, [])
        st = self.store().status()
        self.assertEqual((st["state"], st["revision"]), ("applied", n + seeded))
        self.assertEqual([e["revision"] for e in st["history"]], list(range(n + seeded - 4, n + seeded + 1)))

    def test_readback_failure_restores_previous_bytes(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        before = rd(self.file)
        with mock.patch.object(cv, "_readback", return_value=b"garbage"):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(self.code(cm), "readback_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(self.store().current_scene(), cv.PRESETS["mist"])
        self.assertEqual(s.current_scene(), cv.PRESETS["mist"])

    def test_readback_failure_on_first_write_removes_file(self):
        s = self.store()
        with mock.patch.object(cv, "_readback", return_value=None):
            with self.assertRaises(cv.CanvasError) as cm:
                self.publish(s, {"preset": "mist"})
        self.assertEqual(self.code(cm), "readback_failed")
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(self.store().status()["state"], "default")

    def test_write_failure_leaves_no_temp_and_old_state(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        before = rd(self.file)
        real = os.replace

        def boom(src, dst, *a, **kw):
            if kw.get("src_dir_fd") is not None:
                raise OSError(errno.EIO, "io")
            return real(src, dst, *a, **kw)
        with mock.patch.object(cv.os, "replace", side_effect=boom):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_stale_temp_files_are_cleaned_under_lock(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        junk = os.path.join(self.dir, ".%s.deadbeef0123.tmp" % cv.STATE_NAME)
        with open(junk, "wb") as f:
            f.write(b"x")
        os.mkfifo(os.path.join(self.dir, ".%s.cafebabe0123.tmp" % cv.STATE_NAME))
        s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_never_uses_path_based_chown_chmod_makedirs(self):
        def refuse(*a, **kw):
            raise AssertionError("appel par chemin interdit")
        with mock.patch.object(os, "chmod", refuse), mock.patch.object(os, "chown", refuse), \
                mock.patch.object(os, "makedirs", refuse):
            s = self.store()
            self.publish(s, {"preset": "mist"})
            s.owner_api().rollback(confirm=True)
            s.owner_api().reset(confirm=True)

    def test_no_descriptor_leak(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        n0 = open_fds()
        for i in range(30):
            s.status()
            s.owner_api().apply({"accent": ("ember", "jade")[i % 2]}, confirm=True)
            s.owner_api().rollback(confirm=True)
        with open(self.file, "wb") as f:
            f.write(b"[" * 100000)
        os.chmod(self.file, 0o600)
        for _ in range(10):
            s.status()
            with self.assertRaises(cv.CanvasError):
                s.owner_api().rollback(confirm=True)
        self.assertEqual(open_fds(), n0)

    def test_root_requires_explicit_service_owner_and_fchowns(self):
        with mock.patch.object(cv.os, "geteuid", return_value=0):
            with self.assertRaises(ValueError):
                self.store()
            with self.assertRaises(ValueError):
                self.store(owner_uid=0)
            calls = []
            real = os.fchown

            def rec(fd, uid, gid):
                calls.append(uid)
                return real(fd, uid, gid)
            with mock.patch.object(cv.os, "fchown", side_effect=rec):
                s = self.store(owner_uid=os.getuid())
                self.publish(s, {"preset": "mist"})
        self.assertTrue(calls)
        self.assertTrue(all(u == os.getuid() for u in calls))
        self.assertEqual(os.stat(self.file).st_uid, os.getuid())

    def test_store_for_config(self):
        from opentv_pi.config import Config
        s = cv.store_for(Config({"OPENTV_STATE_DIR": self.root}), wall=self.clock.wall, mono=self.clock.mono)
        self.assertEqual(s.status()["state"], "default")
        self.publish(s, {"preset": "mist"})
        self.assertTrue(os.path.isfile(self.file))
        self.assertEqual(cv.DEFAULT_DIR, "/var/lib/opentv/canvas")


# --------------------------------------------------------------------------- état dangereux ou corrompu
class TestFailClosed(Base):
    def seed(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        return rd(self.file)

    def assert_corrupt(self, reason=None, intact=None):
        s = self.store()
        st = s.status()
        self.assertEqual(st["state"], "corrupt", st)
        if reason:
            self.assertEqual(st["reason"], reason)
        self.assertEqual(s.visible_scene(), cv.DEFAULT_SCENE)
        self.assertEqual(st["history"], [])
        o = s.owner_api()
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_preview("hermit", {"preset": "cinema"})
        self.assertEqual(self.code(cm), "store_corrupt")
        for fn in (lambda: o.apply({"preset": "cinema"}, confirm=True), lambda: o.rollback(confirm=True)):
            with self.assertRaises(cv.CanvasError) as cm:
                fn()
            self.assertIn(self.code(cm), ("store_corrupt", "unsafe_dir"))
        if intact is not None:
            self.assertEqual(intact(), True)
        return s

    def write(self, data, mode=0o600):
        with open(self.file, "wb") as f:
            f.write(data)
        os.chmod(self.file, mode)

    def reset_ok(self, s):
        s.owner_api().reset(confirm=True)
        st = self.store().status()
        self.assertEqual(st["state"], "default")
        st = os.lstat(self.file)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_bad_documents(self):
        self.seed()
        good = json.loads(rd(self.file))
        bad_docs = [b"", b"{", b"\xff\xfe", b"null", b"[]", b"[" * 100000 + b"]" * 100000,
                    b'{"a":' * 50000 + b"1" + b"}" * 50000, b"NaN",
                    json.dumps(dict(good, extra=1)).encode(),
                    json.dumps(dict(good, state_hash="0" * 64)).encode(),
                    json.dumps(dict(good, state_hash=good["state_hash"].upper())).encode(),
                    json.dumps(dict(good, state_hash="\u00e9" * 64)).encode(),
                    (rd(self.file)[:-1] + b',"revision":1}'),       # clé dupliquée
                    json.dumps(dict(good, history=[dict(good["history"][0], at=float("nan"))])).encode(),
                    json.dumps(dict(good, history=[dict(good["history"][0], at=float("inf"))])).encode(),
                    ]

        def rehash(doc):
            body = {k: v for k, v in doc.items() if k != "state_hash"}
            doc["state_hash"] = cv._digest(body)
            return json.dumps(doc).encode()
        e0 = good["history"][0]
        for mut in ({"revision": 2 ** 53}, {"revision": -1}, {"revision": True}, {"revision": 0},
                    {"schema_version": 2}, {"epoch": "X" * 32}, {"epoch": 5},
                    {"history": [dict(e0, at=10 ** 400)]}, {"history": [dict(e0, at=True)]},
                    {"history": [dict(e0, at=1.5)]}, {"history": [dict(e0, at=-1)]},
                    {"history": [dict(e0, revision=2)]}, {"history": [e0, e0]},
                    {"history": [dict(e0, hash="0" * 64)]}, {"history": [dict(e0, agent="Evil/../x")]},
                    {"history": [dict(e0, scene=dict(e0["scene"], wallpaper="https://x"))]},
                    {"history": [dict(e0, extra=1)]}, {"history": [e0] * 6},
                    {"history": "x"}, {"history": [None]}):
            bad_docs.append(rehash(dict(json.loads(json.dumps(good)), **mut)))
        for data in bad_docs:
            with self.subTest(data=data[:60]):
                self.write(data)
                self.assert_corrupt(intact=lambda d=data: rd(self.file) == d)

    def test_recursion_error_from_parser_fails_closed(self):
        self.seed()
        with mock.patch.object(cv.json, "loads", side_effect=RecursionError):
            self.assert_corrupt("invalid_json")
        with mock.patch.object(cv.json, "loads", side_effect=MemoryError):
            self.assert_corrupt()

    def test_symlink_fifo_hardlink_oversize_dir(self):
        before = self.seed()
        target = os.path.join(self.root, "target.json")
        with open(target, "wb") as f:
            f.write(before)
        os.chmod(target, 0o600)
        os.unlink(self.file)
        os.symlink(target, self.file)
        s = self.assert_corrupt("unsafe_file", intact=lambda: rd(target) == before)
        self.reset_ok(s)
        self.assertEqual(rd(target), before)                 # la cible n'est jamais écrite
        os.unlink(self.file)
        os.mkfifo(self.file, 0o600)
        s = self.assert_corrupt("unsafe_file")               # non bloquant
        self.reset_ok(s)
        os.unlink(self.file)
        os.link(target, self.file)
        s = self.assert_corrupt("unsafe_file")
        self.reset_ok(s)
        self.assertEqual(rd(target), before)
        self.write(b" " * (cv.MAX_STATE_BYTES + 1))
        s = self.assert_corrupt("oversize")
        self.reset_ok(s)
        os.unlink(self.file)
        os.mkdir(self.file, 0o700)
        s = self.assert_corrupt("unsafe_file")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertTrue(os.path.isdir(self.file))

    def test_wrong_mode_and_owner(self):
        before = self.seed()
        os.chmod(self.file, 0o644)
        s = self.assert_corrupt("wrong_mode", intact=lambda: rd(self.file) == before)
        self.reset_ok(s)
        real = os.fstat

        def fake(fd):
            st = real(fd)
            if stat.S_ISREG(st.st_mode):
                lst = list(st)
                lst[stat.ST_UID] = st.st_uid + 4242
                return os.stat_result(lst)
            return st
        with mock.patch.object(cv.os, "fstat", side_effect=fake):
            st = self.store().status()
        self.assertEqual((st["state"], st["reason"]), ("corrupt", "wrong_owner"))

    def test_unsafe_directory(self):
        self.seed()
        os.chmod(self.dir, 0o750)
        s = self.store()
        self.assertEqual((s.status()["state"], s.status()["reason"]), ("corrupt", "unsafe_dir"))
        self.assertEqual(s.visible_scene(), cv.DEFAULT_SCENE)
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "unsafe_dir")
        os.chmod(self.dir, 0o700)
        moved = self.dir + ".real"
        os.rename(self.dir, moved)
        os.symlink(moved, self.dir)
        s = self.store()
        self.assertEqual((s.status()["state"], s.status()["reason"]), ("corrupt", "unsafe_dir"))
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "unsafe_dir")
        os.unlink(self.dir)
        with open(self.dir, "wb"):
            pass
        self.assertEqual(self.store().status()["reason"], "unsafe_dir")

    def test_unsafe_parent_refuses_creation(self):
        os.chmod(self.root, 0o777)
        self.addCleanup(os.chmod, self.root, 0o700)
        s = self.store()
        self.assertEqual((s.status()["state"], s.status()["reason"]), ("corrupt", "unsafe_dir"))
        with self.assertRaises(cv.CanvasError) as cm:
            s.agent_preview("hermit", {"preset": "mist"})
        self.assertEqual((self.code(cm), cm.exception.detail), ("store_corrupt", "unsafe_dir"))
        for fn in (s.owner_api().apply, ):
            with self.assertRaises(cv.CanvasError) as cm:
                fn({"preset": "mist"}, confirm=True)
            self.assertEqual(self.code(cm), "unsafe_dir")
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "unsafe_dir")
        self.assertFalse(os.path.exists(self.dir))

    def test_unsafe_lock_file(self):
        self.seed()
        lock = os.path.join(self.dir, cv.LOCK_NAME)
        os.unlink(lock)
        os.symlink(os.path.join(self.root, "elsewhere"), lock)
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "cinema"})
        with self.assertRaises(cv.CanvasError) as cm:
            s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "lock_unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.root, "elsewhere")))

    def test_hostile_state_never_raises_from_reads(self):
        self.seed()
        for data in (b"\x00" * 100, b'{"history":[' + b'{"scene":' * 3000 + b"}" * 3000 + b"]}"):
            self.write(data)
            s = self.store()
            s.status()
            s.visible_scene()
            s.agent_api("hermit").status()


if __name__ == "__main__":
    unittest.main()
