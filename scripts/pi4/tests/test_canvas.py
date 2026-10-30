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
        with mock.patch.object(_storage(), "_readback", return_value=b"garbage"):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(self.code(cm), "readback_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(self.store().current_scene(), cv.PRESETS["mist"])
        self.assertEqual(s.current_scene(), cv.PRESETS["mist"])

    def test_readback_failure_on_first_write_removes_file(self):
        s = self.store()
        with mock.patch.object(_storage(), "_readback", return_value=None):
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
            doc["state_hash"] = _schema()._digest(body)
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
        with mock.patch.object(json, "loads", side_effect=RecursionError):
            self.assert_corrupt("invalid_json")
        with mock.patch.object(json, "loads", side_effect=MemoryError):
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


# --------------------------------------------------------------------------- publication véridique (fail-and-restore)
def _storage():
    import importlib
    return importlib.import_module("opentv_pi.canvas_storage")


def _schema():
    import importlib
    return importlib.import_module("opentv_pi.canvas_schema")


class PostRenameFault:
    """Injecte une panne APRÈS le rename de state.json : le premier fsync de dossier qui suit (`dir_fsync`, une fois
    ou toujours) et/ou le rename suivant (celui de la restauration, `restore_rename`) lèvent EIO."""

    def __init__(self, dir_fsync="once", restore_rename=False):
        self.mode, self.restore_rename = dir_fsync, restore_rename
        self.armed, self.fired, self.renames = False, 0, 0
        self._fsync, self._replace = os.fsync, os.replace

    def fsync(self, fd):
        if self.armed and stat.S_ISDIR(os.fstat(fd).st_mode) and \
                (self.mode == "always" or (self.mode == "once" and not self.fired)):
            self.fired += 1
            raise OSError(errno.EIO, "injected dir fsync EIO")
        return self._fsync(fd)

    def replace(self, src, dst, *a, **kw):
        if dst == cv.STATE_NAME and kw.get("src_dir_fd") is not None:
            self.renames += 1
            if self.renames == 2 and self.restore_rename:
                raise OSError(errno.EIO, "injected restore rename EIO")
            r = self._replace(src, dst, *a, **kw)
            self.armed = True
            return r
        return self._replace(src, dst, *a, **kw)

    def __enter__(self):
        self._p = [mock.patch.object(os, "fsync", side_effect=self.fsync),
                   mock.patch.object(os, "replace", side_effect=self.replace)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()


class TestTruthfulCommit(Base):
    """Contrat « échouer et restaurer » : toute panne après le rename (fsync du dossier, relecture) restaure les
    octets précédents exacts, ou retire le candidat (premier enregistrement / réinitialisation vers l'absence), fsync
    le dossier, recharge, puis renvoie une erreur stable. Résultat, disque et mémoire concordent toujours."""

    def entries(self):
        return sorted(os.listdir(self.dir))

    def assert_agrees(self, s):
        fresh = self.store().status()
        mine = s.status()
        for k in ("state", "reason", "revision", "hash", "history"):
            self.assertEqual(mine[k], fresh[k], k)

    def seeded(self):
        s = self.store()
        self.publish(s, {"preset": "mist"})
        return s, rd(self.file)

    def test_owner_apply_dir_fsync_eio_restores_previous_bytes(self):
        s, before = self.seeded()
        with PostRenameFault() as f:
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(f.fired, 1)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(s.current_scene(), cv.PRESETS["mist"])
        self.assertEqual(s.status()["revision"], 1)
        self.assert_agrees(s)
        self.assertEqual(self.entries(), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_owner_confirm_dir_fsync_eio_restores_and_keeps_preview_retryable(self):
        s, before = self.seeded()
        p = s.agent_preview("hermit", {"preset": "cinema"})
        with PostRenameFault():
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_confirm(p["preview_id"], confirm=True)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(s.current_scene(), cv.PRESETS["mist"])
        self.assert_agrees(s)
        self.assertEqual(self.entries(), sorted([cv.STATE_NAME, cv.LOCK_NAME]))
        self.assertEqual(s.owner_confirm(p["preview_id"], confirm=True)["revision"], 2)   # base intacte

    def test_owner_rollback_dir_fsync_eio_restores(self):
        s, _ = self.seeded()
        s.owner_api().apply({"preset": "cinema"}, confirm=True)
        before = rd(self.file)
        with PostRenameFault():
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().rollback(confirm=True)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertEqual(rd(self.file), before)
        self.assertEqual(s.current_scene(), cv.PRESETS["cinema"])
        self.assertEqual(s.status()["revision"], 2)
        self.assert_agrees(s)
        self.assertEqual(self.entries(), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_first_write_dir_fsync_eio_removes_candidate(self):
        s = self.store()
        with PostRenameFault():
            with self.assertRaises(cv.CanvasError) as cm:
                self.publish(s, {"preset": "mist"})
        self.assertEqual(self.code(cm), "write_failed")
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual((s.status()["state"], s.status()["revision"]), ("default", 0))
        self.assertEqual(s.current_scene(), cv.DEFAULT_SCENE)
        self.assert_agrees(s)
        self.assertEqual(self.entries(), [cv.LOCK_NAME])

    def test_corrupt_reset_dir_fsync_eio_returns_to_absent_default(self):
        self.seeded()
        with open(self.file, "wb") as f:
            f.write(b"{")
        s = self.store()
        self.assertEqual(s.status()["state"], "corrupt")
        with PostRenameFault():
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertFalse(os.path.exists(self.file))              # candidat retiré, jamais l'ancien corrompu réécrit
        self.assertEqual((s.status()["state"], s.status()["reason"]), ("default", None))
        self.assert_agrees(s)
        self.assertEqual(self.entries(), [cv.LOCK_NAME])

    def test_persistent_dir_fsync_eio_still_restores_visible_state(self):
        s, before = self.seeded()
        with PostRenameFault(dir_fsync="always") as f:
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertGreaterEqual(f.fired, 2)
        self.assertEqual(self.code(cm), "write_failed")
        self.assertEqual(rd(self.file), before)
        self.assert_agrees(s)
        self.assertEqual(self.entries(), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_failed_restore_reports_restore_failed_and_memory_matches_disk(self):
        s, _ = self.seeded()
        with PostRenameFault(restore_rename=True):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(self.code(cm), "restore_failed")
        self.assertIn("restore_failed", cv.CODES)
        self.assertEqual((s._raw, s._revision), (rd(self.file), 2))   # rechargé AVANT toute lecture publique
        self.assert_agrees(s)                                    # la mémoire suit le disque, quel qu'il soit
        self.assertEqual(s.current_scene(), self.store().current_scene())
        self.assertEqual(self.entries(), sorted([cv.STATE_NAME, cv.LOCK_NAME]))

    def test_corrupt_reset_readback_failure_removes_candidate(self):
        self.seeded()
        with open(self.file, "wb") as f:
            f.write(b"{")
        s = self.store()
        with mock.patch.object(_storage(), "_readback", return_value=b"garbage"):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().reset(confirm=True)
        self.assertEqual(self.code(cm), "readback_failed")
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(s.status()["state"], "default")
        self.assert_agrees(s)
        self.assertEqual(self.entries(), [cv.LOCK_NAME])


# --------------------------------------------------------------------------- erreurs et façade agent
class TestErrors(Base):
    def test_unknown_code_is_rejected_by_real_validation(self):
        for bad in ("bogus", "", None, 3, ["ui_busy"], b"ui_busy"):
            with self.assertRaises(ValueError, msg=bad):
                cv.CanvasError(bad)

        class S(str):
            pass
        with self.assertRaises(ValueError):
            cv.CanvasError(S("ui_busy"))
        self.assertEqual(cv.CanvasError("ui_busy", "Playback/../x").detail, None)

    def test_unknown_code_rejected_under_python_O(self):
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from opentv_pi import canvas as cv\n"
                "try:\n  cv.CanvasError('bogus')\nexcept ValueError:\n  print('refused')\n"
                "print('O=%d' % sys.flags.optimize)\n")
        p = subprocess.run([sys.executable, "-O", "-I", "-c", code, PI4], capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-1500:])
        self.assertEqual(p.stdout.decode().split(), ["refused", "O=1"])


class TestAgentFacadeRedaction(Base):
    """La façade agent ne révèle jamais la raison interne (lecture, configuration Muse, propriétaire…)."""

    SECRET = ("playback", "muse_setup", "setup", "assistants", "loading", "returning", "wrong_owner", "unsafe_dir",
              "invalid_json", "navigating", "overflow")

    def assert_redacted(self, cm, code):
        e = cm.exception
        self.assertEqual((e.code, e.detail, str(e), e.args), (code, None, code, (code,)))
        self.assertIsNone(e.__context__)
        self.assertIsNone(e.__cause__)
        text = repr(e) + str(e)
        for s in self.SECRET:
            self.assertNotIn(s, text)

    def test_busy_reason_never_reaches_agent(self):
        s = self.store()
        a = s.agent_api("hermit")
        for reason in ("playback", "muse_setup"):
            tok = s.busy_enter(reason)
            with self.assertRaises(cv.CanvasError) as cm:
                a.preview({"preset": "mist"})
            self.assert_redacted(cm, "ui_busy")
            with self.assertRaises(cv.CanvasError) as cm:          # API interne/propriétaire : détail conservé
                s.agent_preview("hermit", {"preset": "mist"})
            self.assertEqual(cm.exception.detail, reason)
            s.busy_exit(tok)
        s.human_event("key")
        with self.assertRaises(cv.CanvasError) as cm:
            a.preview({"preset": "mist"})
        self.assert_redacted(cm, "ui_busy")

    def test_corrupt_reason_never_reaches_agent(self):
        self.publish(self.store(), {"preset": "mist"})
        real = os.fstat

        def fake(fd):
            st = real(fd)
            if stat.S_ISREG(st.st_mode):
                lst = list(st)
                lst[stat.ST_UID] = st.st_uid + 4242
                return os.stat_result(lst)
            return st
        with mock.patch.object(os, "fstat", side_effect=fake):
            s = self.store()
            a = s.agent_api("hermit")
            self.assertEqual(s.status()["reason"], "wrong_owner")
            st = a.status()
            with self.assertRaises(cv.CanvasError) as cm:
                a.preview({"preset": "cinema"})
        self.assert_redacted(cm, "store_corrupt")
        self.assertNotIn("reason", st)
        for secret in self.SECRET:
            self.assertNotIn(secret, json.dumps(st))

    def test_cancel_and_invalid_errors_are_plain_codes(self):
        s = self.store()
        a = s.agent_api("hermit")
        with self.assertRaises(cv.CanvasError) as cm:
            a.cancel("0" * 32)
        self.assert_redacted(cm, "unknown_preview")
        with self.assertRaises(cv.CanvasError) as cm:
            a.preview({"wallpaper": "https://x"})
        self.assert_redacted(cm, "invalid_scene")


class TestConfirmationSheetContract(Base):
    """Intégration documentée (tâche 2) : les touches de la feuille de confirmation (←/→ entre Oui et Non, OK, Retour)
    ne passent PAS par human_event/busy_enter, et la feuille n'est pas une raison d'occupation « panel »."""

    def test_sheet_keys_must_not_be_human_events(self):
        s = self.store()
        p = s.agent_preview("hermit", {"preset": "mist"})
        s.human_event("key")                     # ce que ferait à tort une touche de la feuille
        with self.assertRaises(cv.CanvasError):
            s.owner_confirm(p["preview_id"], confirm=True)
        self.clock.m += cv.NAV_QUIET_S
        s.agent_preview("hermit", {"preset": "mist"})
        tok = s.busy_enter("panel")              # ce que ferait à tort la feuille ouverte comme panneau générique
        self.assertIsNone(s.status()["preview"])
        s.busy_exit(tok)
        p = s.agent_preview("hermit", {"preset": "mist"})
        self.assertEqual(s.owner_confirm(p["preview_id"], confirm=True)["revision"], 1)   # feuille seule : OK

    def test_contract_is_documented(self):
        doc = cv.__doc__ or ""
        for needle in ("feuille de confirmation", "human_event", "busy_enter", "panel"):
            self.assertIn(needle, doc)
        self.assertNotIn("confirm", " ".join(cv.BUSY_REASONS))


# --------------------------------------------------------------------------- création du dossier et du verrou
class TestCreationHardening(Base):
    """Le dossier Canvas n'apparaît sous son nom qu'une fois COMPLET (temporaire du même parent, propriétaire/mode
    fixés sur descripteur, verrou créé dedans, rename atomique, fsync du parent) : un plantage ne laisse jamais un
    dossier root 0700 sous le nom canonique ; les créateurs concurrents convergent sur un seul dossier."""

    def litter(self):
        return sorted(n for n in os.listdir(self.root) if n != "canvas")

    def crash_at(self, stage):
        code = ("import os,sys;sys.path.insert(0,sys.argv[1]);from opentv_pi import canvas as cv, canvas_storage as st\n"
                "def hook(name):\n  if name==sys.argv[3]: os._exit(9)\n"
                "st._hook=hook\n"
                "cv.CanvasStore(sys.argv[2]).owner_api().apply({'preset':'mist'},confirm=True)\n")
        p = subprocess.run([sys.executable, "-I", "-c", code, PI4, self.dir, stage], capture_output=True, timeout=60)
        return p.returncode

    def assert_complete(self):
        st = os.lstat(self.dir)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid), (0o700, os.geteuid()))
        lk = os.lstat(os.path.join(self.dir, cv.LOCK_NAME))
        self.assertEqual((stat.S_IMODE(lk.st_mode), lk.st_uid, lk.st_nlink), (0o600, os.geteuid(), 1))

    def test_crash_at_every_stage_never_strands_partial_dir_and_recovers(self):
        st = _storage()
        self.assertEqual(st.CREATE_STAGES, ("dir_created", "dir_owned", "lock_created", "before_rename",
                                            "after_rename"))
        for stage in st.CREATE_STAGES:
            with self.subTest(stage=stage):
                shutil.rmtree(self.dir, True)
                for n in self.litter():
                    shutil.rmtree(os.path.join(self.root, n), True)
                self.assertEqual(self.crash_at(stage), 9)
                if stage == "after_rename":
                    self.assert_complete()
                else:
                    self.assertFalse(os.path.lexists(self.dir), stage)
                    self.assertEqual(len(self.litter()), 1, self.litter())
                s = self.store()
                self.assertEqual(s.status()["state"], "default")
                self.publish(s, {"preset": "mist"})               # reprise
                self.assert_complete()
                self.assertEqual(self.litter(), [])               # temporaire orphelin nettoyé
                self.assertEqual(self.store().status()["revision"], 1)

    def test_competing_creator_wins_then_loser_uses_its_dir(self):
        st = _storage()
        other = self.store()
        seen = []

        def hook(name):
            if name == "before_rename" and not seen:
                seen.append(name)
                self.assertFalse(os.path.lexists(self.dir))      # rien sous le nom canonique avant le rename
                other.owner_api().apply({"preset": "cinema"}, confirm=True)
        with mock.patch.object(st, "_hook", hook):
            r = self.store().owner_api().apply({"accent": "jade"}, confirm=True)
        self.assertEqual(r["revision"], 2)
        self.assert_complete()
        self.assertEqual(self.litter(), [])
        self.assertEqual(sorted(os.listdir(self.dir)), sorted([cv.STATE_NAME, cv.LOCK_NAME]))
        self.assertEqual(self.store().current_scene().accent, "jade")

    def test_hostile_name_planted_before_rename_fails_closed(self):
        st = _storage()
        elsewhere = os.path.join(self.root, "elsewhere")
        os.mkdir(elsewhere, 0o700)
        for plant in ("symlink", "file"):
            with self.subTest(plant=plant):
                def hook(name, plant=plant):
                    if name == "before_rename" and not os.path.lexists(self.dir):
                        if plant == "symlink":
                            os.symlink(elsewhere, self.dir)
                        else:
                            open(self.dir, "wb").close()
                with mock.patch.object(st, "_hook", hook):
                    with self.assertRaises(cv.CanvasError) as cm:
                        self.store().owner_api().apply({"preset": "mist"}, confirm=True)
                self.assertEqual(self.code(cm), "unsafe_dir")
                self.assertEqual(os.listdir(elsewhere), [])
                self.assertEqual(self.litter(), ["elsewhere"])
                os.unlink(self.dir)

    def test_root_publishes_dir_only_after_descriptor_ownership(self):
        calls = []
        real_fchown, real_fchmod = os.fchown, os.fchmod

        def rec_chown(fd, uid, gid):
            calls.append(("chown", stat.S_ISDIR(os.fstat(fd).st_mode), os.path.lexists(self.dir)))
            return real_fchown(fd, uid, gid)

        def rec_chmod(fd, mode):
            calls.append(("chmod", stat.S_ISDIR(os.fstat(fd).st_mode), os.path.lexists(self.dir)))
            return real_fchmod(fd, mode)
        with mock.patch.object(os, "geteuid", return_value=0), \
                mock.patch.object(os, "fchown", side_effect=rec_chown), \
                mock.patch.object(os, "fchmod", side_effect=rec_chmod):
            s = self.store(owner_uid=os.getuid())
            self.publish(s, {"preset": "mist"})
        creation = [c for c in calls if c[0] == "chown"][:2]
        self.assertEqual([c[1] for c in creation], [True, False])        # dossier puis verrou, par descripteur
        self.assertTrue(all(not c[2] for c in creation))                  # avant toute apparition du nom
        self.assert_complete()

    def test_parent_fsync_failure_after_publish_is_reported_and_recoverable(self):
        parent = os.stat(self.root)
        real = os.fsync
        hits = []

        def fake(fd):
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == (parent.st_dev, parent.st_ino):
                hits.append(1)
                raise OSError(errno.EIO, "parent fsync")
            return real(fd)
        s = self.store()
        with mock.patch.object(os, "fsync", side_effect=fake):
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "mist"}, confirm=True)
        self.assertEqual((self.code(cm), len(hits)), ("write_failed", 1))
        self.assert_complete()                                   # dossier complet, aucune scène écrite
        self.assertFalse(os.path.exists(self.file))
        self.assertEqual(s.status()["state"], "default")
        self.assertEqual(self.litter(), [])
        s.owner_api().apply({"preset": "mist"}, confirm=True)
        self.assertEqual(self.store().status()["revision"], 1)

    def test_root_never_creates_lock_in_existing_dir(self):
        os.mkdir(self.dir, 0o700)
        with mock.patch.object(os, "geteuid", return_value=0):
            s = self.store(owner_uid=os.getuid())
            with self.assertRaises(cv.CanvasError) as cm:
                s.owner_api().apply({"preset": "mist"}, confirm=True)
        self.assertEqual(self.code(cm), "lock_unsafe")
        self.assertEqual(os.listdir(self.dir), [])
        self.store().owner_api().apply({"preset": "mist"}, confirm=True)    # le service le crée lui-même
        self.assert_complete()


# --------------------------------------------------------------------------- découpage du module
class TestModuleSplit(unittest.TestCase):
    PUBLIC = ['AgentCanvas', 'BUSY_REASONS', 'CHANGE_KEYS', 'CODES', 'CORRUPT_REASONS', 'CanvasError', 'CanvasStore',
              'DEFAULT_DIR', 'DEFAULT_PRESET', 'DEFAULT_SCENE', 'DIR_NAME', 'DOC_KEYS', 'ENTRY_KEYS', 'ENUMS',
              'FIELDS', 'FIRST_ROW', 'HUMAN_EVENTS', 'LOCK_NAME', 'LOCK_TIMEOUT_S', 'MAX_AT', 'MAX_BUSY_TOKENS',
              'MAX_HISTORY', 'MAX_REVISION', 'MAX_STATE_BYTES', 'NAV_QUIET_S', 'OwnerCanvas', 'PRESETS', 'ROW_IDS',
              'SCENE_KEYS', 'SCHEMA_VERSION', 'STATES', 'STATE_NAME', 'Scene', 'SceneError', 'apply_changes',
              'canonical_json', 'preset_of', 'scene_hash', 'store_for', 'validate_scene']
    STORE_METHODS = ['agent_api', 'agent_cancel', 'agent_preview', 'busy_enter', 'busy_exit', 'busy_reasons',
                     'busy_reset', 'busy_scope', 'current_scene', 'human_event', 'owner_api', 'owner_apply',
                     'owner_confirm', 'owner_reject', 'owner_reset', 'owner_rollback', 'status', 'visible_scene']
    MODULES = {"canvas": 450, "canvas_schema": 400, "canvas_storage": 400, "canvas_activity": 400,
               "canvas_roles": 400}

    def test_public_api_preserved(self):
        import importlib
        internal = [importlib.import_module("opentv_pi." + m) for m in self.MODULES if m != "canvas"]
        self.assertEqual(sorted(cv.__all__), sorted(self.PUBLIC))
        for n in self.PUBLIC:
            self.assertTrue(hasattr(cv, n), n)
            for m in internal:
                if hasattr(m, n):
                    self.assertIs(getattr(cv, n), getattr(m, n), n)
        self.assertEqual(sorted(n for n in dir(cv.CanvasStore) if not n.startswith("_")), self.STORE_METHODS)
        self.assertLessEqual(set(cv.CODES) - {"restore_failed"},
                             {"invalid_agent", "invalid_scene", "invalid_event", "no_change", "ui_busy",
                              "busy_overflow", "preview_pending", "unknown_preview", "preview_expired",
                              "stale_preview", "confirmation_required", "store_corrupt", "store_busy",
                              "lock_unsafe", "unsafe_dir", "write_failed", "readback_failed", "nothing_to_rollback",
                              "revision_exhausted"})

    def test_line_caps(self):
        pkg = os.path.join(PI4, "opentv_pi")
        for name, cap in self.MODULES.items():
            with open(os.path.join(pkg, name + ".py"), encoding="utf-8") as f:
                n = len(f.read().splitlines())
            self.assertLessEqual(n, cap, "%s.py : %d lignes > %d" % (name, n, cap))

    def test_stdlib_only_and_acyclic(self):
        import ast
        pkg = os.path.join(PI4, "opentv_pi")
        allowed_rel = {"canvas": {"agents", "canvas_activity", "canvas_roles", "canvas_schema", "canvas_storage",
                                  "fsutil"},
                       "canvas_schema": {"validate"},
                       "canvas_storage": {"agents", "canvas_schema", "fsutil"},
                       "canvas_activity": {"canvas_schema"},
                       "canvas_roles": {"canvas_schema"}}
        for name in self.MODULES:
            with open(os.path.join(pkg, name + ".py"), "rb") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertIn(a.name.split(".")[0], sys.stdlib_module_names, (name, a.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        self.assertEqual(node.level, 1, name)
                        for n in ([node.module] if node.module else [a.name for a in node.names]):
                            self.assertIn(n, allowed_rel[name], (name, n))
                    else:
                        self.assertIn((node.module or "").split(".")[0], sys.stdlib_module_names, name)
        others = ("opentv_pi.canvas", "opentv_pi.canvas_storage", "opentv_pi.canvas_activity",
                  "opentv_pi.canvas_roles")
        for mod in ("canvas_schema", "canvas_storage", "canvas_activity", "canvas_roles", "canvas"):
            absent = [] if mod == "canvas" else [o for o in others if o != "opentv_pi." + mod]
            code = ("import sys;sys.path.insert(0,sys.argv[1]);import importlib;"
                    "importlib.import_module('opentv_pi.'+sys.argv[2]);"
                    "print([n for n in sys.argv[3:] if n in sys.modules])")
            p = subprocess.run([sys.executable, "-I", "-c", code, PI4, mod, *absent], capture_output=True,
                               timeout=60)
            self.assertEqual((p.returncode, p.stdout.decode().strip()), (0, "[]"), p.stderr.decode()[-1500:])


if __name__ == "__main__":
    unittest.main()
