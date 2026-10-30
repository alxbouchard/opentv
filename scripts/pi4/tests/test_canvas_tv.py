"""OpenTV Canvas (tâche 2) : API TV et cycle de confirmation par le propriétaire.

Couvre : état Canvas épuré dans /api/tv/state (jamais de chemin, d'identité authentifiée brute ni de secret),
aperçu agent visible sur la TV, feuille de confirmation (agent canonique, description fermée, « Non » par défaut,
échéance sûre, jamais pendant configuration / gestion des assistants / connexion de Muse / lecture et retour),
routes POST de la TV derrière la clé de page + Host + UID du pair, annulation par toute navigation humaine AVANT
l'action, raisons d'occupation du service conservées au rechargement de la page, audit borné et vérité après
restore_failed.
"""
import json
import os
import unittest
from unittest import mock

from tests._common import PI4
from tests.test_service import Servers, make_app
from opentv_pi import canvas as cv, canvas_storage, config, tools

HEX32 = "0" * 32


def configured(tc, **kw):
    a = make_app(tc, **kw)
    config.save_device(a.cfg, {"name": "Salon"})
    a.reload_device()
    a.tick()
    return a


def blob(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def audit(a):
    try:
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


def set_playback(a, state):
    with a.playback.lock:
        a.playback.state = state


class Base(unittest.TestCase):
    def app(self, **kw):
        return configured(self, **kw)

    def propose(self, a, agent="muse-device", changes=None):
        return a.canvas_agent_preview(agent, changes or {"preset": "mist"})

    def assert_clean(self, a, obj):
        text = blob(obj)
        for bad in (a.cfg.state_dir, "state.json", "/var/lib", ".lock", a.tv_key, "muse-device"):
            self.assertNotIn(bad, text)


# --------------------------------------------------------------------------- état TV
class TestTvState(Base):
    def test_default_state_is_scrubbed_and_stable(self):
        a = self.app()
        c = a.tv_state()["canvas"]
        self.assertEqual((c["status"], c["state"], c["revision"], c["preset"]), ("ok", "default", 0, "canvas"))
        self.assertEqual(c["hash"], cv.scene_hash(cv.DEFAULT_SCENE))
        self.assertEqual(c["scene"], cv.DEFAULT_SCENE.to_dict())
        self.assertIsNone(c["preview"])
        self.assertIsNone(c["sheet"])
        self.assertFalse(c["can_rollback"])
        self.assertNotIn("busy", c)                       # raisons internes jamais exposées
        self.assertNotIn("history", c)
        self.assert_clean(a, c)

    def test_agent_preview_is_visible_with_canonical_agent_and_sheet(self):
        a = self.app()
        r = self.propose(a, "muse-device", {"preset": "cinema"})
        self.assertEqual(set(r), {"preview_id", "hash", "base_revision", "expires_in_s", "scene", "preset"})
        c = a.tv_state()["canvas"]
        p = c["preview"]
        self.assertEqual((p["id"], p["proposed_by"], p["preset"]), (r["preview_id"], "muse", "cinema"))
        self.assertEqual(p["scene"], cv.PRESETS["cinema"].to_dict())
        self.assertNotIn("agent", p)                      # identité authentifiée jamais montrée à la TV
        sh = c["sheet"]
        self.assertEqual((sh["preview_id"], sh["proposed_by"], sh["agent_name"]), (r["preview_id"], "muse", "Muse"))
        self.assertEqual((sh["focus"], sh["choices"], sh["timeout_action"]), ("no", ["no", "yes"], "reject"))
        self.assertEqual(sh["preset"], "cinema")
        cur, new = cv.DEFAULT_SCENE.to_dict(), cv.PRESETS["cinema"].to_dict()
        want = [{"field": f, "from": cur[f], "to": new[f]} for f in cv.FIELDS if cur[f] != new[f]]
        self.assertEqual(sh["changes"], want)
        for ch in sh["changes"]:                          # description fermée : énumérations seulement
            vals = ch["to"] if isinstance(ch["to"], list) else [ch["to"]]
            allowed = cv.ROW_IDS if ch["field"] == "row_order" else cv.ENUMS[ch["field"]]
            self.assertTrue(set(vals) <= set(allowed), ch)
        self.assertTrue(0 < sh["expires_in_s"] <= 30)
        self.assert_clean(a, c)
        # audit : identité authentifiée, énumérations, empreinte courte ; jamais la scène brute
        rec = [x for x in audit(a) if x["tool"] == "canvas"]
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["agent"], rec[0]["ok"]), ("muse-device", True))
        self.assertEqual(rec[0]["args"], {"action": "proposed", "preset": "cinema", "hash": r["hash"][:16],
                                          "revision": 0})

    def test_unknown_agent_has_no_display_name(self):
        a = self.app()
        self.propose(a, "lab-bot")
        sh = a.tv_state()["canvas"]["sheet"]
        self.assertEqual((sh["proposed_by"], sh["agent_name"]), ("lab-bot", None))

    def test_agent_errors_are_bare_codes(self):
        a = self.app()
        with self.assertRaises(cv.CanvasError) as e:
            a.canvas_agent_preview("muse", {"wallpaper": "https://evil.example/x"})
        self.assertEqual((e.exception.code, e.exception.detail), ("invalid_scene", None))
        set_playback(a, "playing")
        with self.assertRaises(cv.CanvasError) as e:
            self.propose(a)
        self.assertEqual((e.exception.code, e.exception.detail, str(e.exception)), ("ui_busy", None, "ui_busy"))
        self.assertIsNone(e.exception.__context__)
        rec = [x for x in audit(a) if x["tool"] == "canvas"]
        self.assertEqual([(x["ok"], x["error"], x["args"]["action"]) for x in rec],
                         [(False, "invalid_scene", "proposed"), (False, "ui_busy", "proposed")])
        self.assertNotIn("evil", blob(audit(a)))

    def test_agent_can_withdraw_only_its_preview(self):
        a = self.app()
        r = self.propose(a, "hermit")
        with self.assertRaises(cv.CanvasError):
            a.canvas_agent_cancel("hermes", r["preview_id"])
        self.assertTrue(a.canvas_agent_cancel("hermit", r["preview_id"]))
        self.assertIsNone(a.tv_state()["canvas"]["preview"])

    def test_corrupt_store_reports_stable_status_without_detail(self):
        a = self.app()
        os.makedirs(os.path.join(a.cfg.state_dir, "canvas"), mode=0o700)
        with open(os.path.join(a.cfg.state_dir, "canvas", cv.STATE_NAME), "w") as f:
            f.write("{nope")
        os.chmod(os.path.join(a.cfg.state_dir, "canvas", cv.STATE_NAME), 0o600)
        c = a.tv_state()["canvas"]
        self.assertEqual((c["status"], c["state"], c["preset"]), ("corrupt", "corrupt", "canvas"))
        self.assertNotIn("reason", c)
        self.assertNotIn("invalid_json", blob(c))
        with self.assertRaises(cv.CanvasError) as e:
            self.propose(a)
        self.assertEqual((e.exception.code, e.exception.detail), ("store_corrupt", None))


    def test_unavailable_store_is_fail_closed(self):
        with mock.patch.object(cv, "store_for", side_effect=ValueError("root")):
            a = self.app()
        self.assertIn("canvas_unavailable", [x["event"] for x in a.test_logs])
        c = a.tv_state()["canvas"]
        self.assertEqual((c["status"], c["preset"], c["preview"], c["sheet"]), ("unavailable", "canvas", None, None))
        with self.assertRaises(cv.CanvasError) as e:
            self.propose(a)
        self.assertEqual((e.exception.code, e.exception.detail), ("store_corrupt", None))
        code, out = a.canvas_tv_post("reset", {"confirm": True})
        self.assertEqual((code, out["error"], out["outcome"]), (409, "store_corrupt", "unchanged"))
        self.assertEqual(a.canvas_tv_post("ui", {"busy": ["panel"]})[0], 200)
        self.assertEqual(a.canvas_tv_post("event", {"kind": "key"}), (200, {"ok": True, "preview_cancelled": False}))
        self.assertEqual(a.canvas_tv_post("event", {"kind": "nope"})[0], 400)
        self.assertEqual(a.agent_control("muse", "remote_control",
                                         tools.validate_args("remote_control", {"key": "down"}))["accepted"], True)


# --------------------------------------------------------------------------- portes de la feuille
class TestSheetGates(Base):
    def assert_blocked(self, a):
        with self.assertRaises(cv.CanvasError) as e:
            self.propose(a)
        self.assertEqual(e.exception.code, "ui_busy")
        self.assertIsNone(a.tv_state()["canvas"]["sheet"])

    def test_never_during_setup(self):
        a = make_app(self)                                # premier démarrage : configuration
        self.assertTrue(a.tv_state()["setup"]["active"])
        self.assert_blocked(a)
        b = self.app()
        self.propose(b)
        b.request_setup()                                 # « Reconfigurer » : aperçu retiré aussitôt
        b.tick()
        self.assertIsNone(b.tv_state()["canvas"]["preview"])
        self.assert_blocked(b)

    def test_never_during_assistants_management(self):
        a = self.app()
        self.propose(a)
        with a.lock:
            a.agents_mode = True
        self.assertIsNone(a.tv_state()["canvas"]["preview"])
        self.assert_blocked(a)
        with a.lock:
            a.agents_mode = False
        self.propose(a)
        self.assertIsNotNone(a.tv_state()["canvas"]["sheet"])

    def test_never_during_muse_setup(self):
        a = self.app()
        self.propose(a)
        with mock.patch.object(a.muse, "busy", return_value=True):
            self.assertIsNone(a.tv_state()["canvas"]["preview"])
            self.assert_blocked(a)

    def test_never_during_playback_or_return(self):
        for st in ("loading", "playing", "closing", "closed", "unavailable"):
            a = self.app()
            r = self.propose(a)
            set_playback(a, st)
            self.assertIsNone(a.tv_state()["canvas"]["preview"], st)
            self.assert_blocked(a)
            with self.assertRaises(cv.CanvasError):
                a.canvas.store.owner_confirm(r["preview_id"], confirm=True)
            set_playback(a, "idle")
            self.propose(a)                               # retour acquitté : de nouveau possible
            self.assertIsNotNone(a.tv_state()["canvas"]["sheet"], st)

    def test_confirm_syncs_service_reasons_first(self):
        a = self.app()
        r = self.propose(a)
        set_playback(a, "loading")                        # aucun tv_state entre-temps
        code, out = a.canvas_tv_post("confirm", {"preview_id": r["preview_id"], "confirm": True})
        self.assertEqual((code, out["ok"], out["outcome"]), (409, False, "unchanged"))
        self.assertEqual(a.canvas.store.status()["revision"], 0)

    def test_pending_agent_actions_block_preview_and_live_sheet_blocks_agent_keys(self):
        a = self.app()
        a.agent_control("muse", "remote_control", tools.validate_args("remote_control", {"key": "down"}))
        self.assert_blocked(a)                            # action d'assistant encore en file
        a.tv_actions.cancel_queued("test")
        self.propose(a, "hermit")
        for name, args in (("remote_control", {"key": "ok"}), ("remote_control", {"key": "down"}),
                           ("present_title", {"id": "films/101"}), ("play_title", {"id": "films/101"})):
            with self.assertRaises(tools.ToolError) as e:
                a.agent_control("muse", name, tools.validate_args(name, args))
            self.assertEqual(str(e.exception), "tv_busy", name)
        self.assertEqual(a.tv_actions.pending(), [])     # jamais une touche d'assistant sur la feuille
        self.assertEqual(a.agent_control("muse", "close_player", {})["closed"], False)   # toujours permis

    def test_sheet_never_drawn_over_a_busy_reason(self):
        """Défense en profondeur : même si le magasin montrait un aperçu pendant une raison d'occupation, aucune
        feuille n'est proposée (seul l'aperçu non interactif reste décrit)."""
        a = self.app()
        self.propose(a)
        real = a.canvas.store.status
        for reason in ("panel", "navigating", "playback"):
            with mock.patch.object(a.canvas.store, "status", side_effect=lambda: dict(real(), busy=[reason])):
                c = a.tv_state()["canvas"]
            self.assertIsNotNone(c["preview"], reason)
            self.assertIsNone(c["sheet"], reason)
        self.assertIsNotNone(a.tv_state()["canvas"]["sheet"])

    def test_timeout_is_safe(self):
        a = self.app()
        r = self.propose(a)
        a.mono_clock.t += 30 - a.canvas.SHEET_MIN_S + 0.01
        c = a.tv_state()["canvas"]
        self.assertIsNotNone(c["preview"])                # encore visible…
        self.assertIsNone(c["sheet"])                     # …mais trop court pour décider : aucune feuille
        a.mono_clock.t += a.canvas.SHEET_MIN_S
        self.assertIsNone(a.tv_state()["canvas"]["preview"])
        code, out = a.canvas_tv_post("confirm", {"preview_id": r["preview_id"], "confirm": True})
        self.assertEqual((code, out["outcome"]), (409, "unchanged"))
        self.assertIn(out["error"], ("preview_expired", "unknown_preview"))
        self.assertEqual(a.canvas.store.status()["revision"], 0)


# --------------------------------------------------------------------------- routes POST de la TV
class TestTvRoutes(Base):
    def setUp(self):
        self.a = self.app()
        self.s = Servers(self, self.a)
        self.h = dict(self.s.tv_headers(), **{"Content-Type": "application/json"})

    def post(self, path, body, headers=None):
        code, _, text = self.s.req("POST", "/api/tv/canvas/" + path, json.dumps(body),
                                   headers=self.h if headers is None else headers)
        try:
            return code, json.loads(text)
        except ValueError:
            return code, text

    def state(self):
        return json.loads(self.s.req("GET", "/api/tv/state", headers=self.s.tv_headers())[2])["canvas"]

    def test_tv_key_host_peer_and_content_type_guards(self):
        r = self.propose(self.a)
        body = {"preview_id": r["preview_id"], "confirm": True}
        for p in ("confirm", "cancel", "rollback", "reset", "apply", "event", "ui"):
            self.assertEqual(self.post(p, body, {"Content-Type": "application/json"}), (403, {"ok": False,
                                                                                         "error": "tv_key"}), p)
            bad = dict(self.h, **{"X-OpenTV-TV": "x" * 43})
            self.assertEqual(self.post(p, body, bad)[0], 403, p)
            self.assertEqual(self.post(p, body, dict(self.h, Host="evil.example:80"))[0], 421, p)
            self.assertEqual(self.post(p, body, dict(self.h, **{"Content-Type": "text/plain"}))[0], 415, p)
        self.a.tv_client_uids = frozenset({4242})
        self.a.peer_uid_fn = lambda *x: 1
        self.assertEqual(self.post("confirm", body)[0], 403)
        self.assertEqual(self.post("reset", {"confirm": True})[0], 403)
        self.a.tv_client_uids = None
        self.assertEqual(self.a.canvas.store.status()["revision"], 0)
        self.assertIsNotNone(self.state()["preview"])     # aucune route refusée n'a touché l'aperçu

    def test_confirm_requires_literal_true_then_publishes(self):
        r = self.propose(self.a, "muse-device", {"preset": "cinema"})
        for bad in ({"preview_id": r["preview_id"]}, {"preview_id": r["preview_id"], "confirm": 1},
                    {"preview_id": r["preview_id"], "confirm": "true"}):
            code, out = self.post("confirm", bad)
            self.assertEqual((code, out["error"], out["outcome"]), (400, "confirmation_required", "unchanged"))
        code, out = self.post("confirm", {"preview_id": "A" * 32, "confirm": True})
        self.assertEqual((code, out["error"]), (409, "unknown_preview"))
        code, out = self.post("confirm", {"preview_id": r["preview_id"], "confirm": True})
        self.assertEqual((code, out["ok"], out["outcome"]), (200, True, "applied"), out)
        self.assertEqual(out["result"], {"revision": 1, "hash": r["hash"], "preset": "cinema"})
        c = out["canvas"]
        self.assertEqual((c["state"], c["revision"], c["preset"], c["preview"], c["sheet"]),
                         ("applied", 1, "cinema", None, None))
        self.assertTrue(c["can_rollback"])
        self.assertEqual(self.state()["revision"], 1)
        self.assert_clean(self.a, out)
        rec = [x for x in audit(self.a) if x["tool"] == "canvas"][-1]
        self.assertEqual((rec["agent"], rec["ok"]), ("muse-device", True))
        self.assertEqual(rec["args"], {"action": "confirmed", "preset": "cinema", "hash": r["hash"][:16],
                                       "revision": 1})

    def test_cancel_rollback_reset(self):
        r = self.propose(self.a, "hermit")
        code, out = self.post("cancel", {"preview_id": r["preview_id"]})
        self.assertEqual((code, out["ok"], out["outcome"], out["canvas"]["preview"]), (200, True, "unchanged", None))
        code, out = self.post("cancel", {"preview_id": r["preview_id"]})
        self.assertEqual((code, out["ok"], out["error"]), (409, False, "unknown_preview"))
        code, out = self.post("rollback", {"confirm": True})
        self.assertEqual((code, out["error"], out["outcome"]), (409, "nothing_to_rollback", "unchanged"))
        r = self.propose(self.a, "hermit", {"preset": "cinema"})
        self.assertEqual(self.post("confirm", {"preview_id": r["preview_id"], "confirm": True})[0], 200)
        self.assertEqual(self.post("rollback", {})[1]["error"], "confirmation_required")
        code, out = self.post("rollback", {"confirm": True})
        self.assertEqual((code, out["outcome"], out["result"]["restored_revision"]), (200, "applied", 0))
        self.assertEqual((out["canvas"]["revision"], out["canvas"]["preset"], out["canvas"]["state"]),
                         (2, "canvas", "default"))
        self.assertEqual(self.post("reset", {"confirm": False})[1]["error"], "confirmation_required")
        code, out = self.post("reset", {"confirm": True})
        self.assertEqual((code, out["outcome"], out["canvas"]["preset"]), (200, "applied", "canvas"))
        acts = [(x["args"]["action"], x["ok"], x["error"]) for x in audit(self.a) if x["tool"] == "canvas"]
        self.assertEqual(acts, [("proposed", True, None), ("rejected", True, None),
                                ("rolled-back", False, "nothing_to_rollback"), ("proposed", True, None),
                                ("confirmed", True, None), ("rolled-back", True, None), ("reset", True, None)])
        for x in audit(self.a):
            self.assertEqual(set(x["args"]) - {"action", "preset", "hash", "revision"}, set())
            for k in ("action", "preset", "hash"):
                self.assertIsInstance(x["args"].get(k, ""), str)      # jamais {"len": …} : valeurs sûres gardées
            self.assertNotIn("scene", blob(x))

    def test_owner_apply_closed_flat_enums_only(self):
        for bad in ({"changes": {"preset": "mist"}}, {"changes": {"row_order": list(cv.ROW_IDS)}, "confirm": True},
                    {"changes": {"preset": ["mist"]}, "confirm": True}, {"changes": "mist", "confirm": True},
                    {"changes": {"wallpaper": "https://x"}, "confirm": True}):
            code, out = self.post("apply", bad)
            self.assertEqual((code, out["outcome"]), (400, "unchanged"), bad)
        code, out = self.post("apply", {"changes": {"preset": "mist", "motion": "off"}, "confirm": True})
        self.assertEqual((code, out["outcome"], out["canvas"]["scene"]["motion"]), (200, "applied", "off"))

    def test_human_event_cancels_but_sheet_flow_does_not(self):
        r = self.propose(self.a)
        code, out = self.post("event", {"kind": "key"})
        self.assertEqual((code, out["preview_cancelled"]), (200, True))
        self.assertIsNone(self.state()["preview"])
        self.assertEqual(self.post("event", {"kind": "telepathy"})[0], 400)
        rec = [x for x in audit(self.a) if x["tool"] == "canvas"][-1]
        self.assertEqual((rec["agent"], rec["args"]["action"]), ("muse-device", "cancelled"))
        self.a.mono_clock.t += cv.NAV_QUIET_S
        r = self.propose(self.a)
        # la feuille : ses touches n'appellent rien ; la confirmation passe
        code, out = self.post("confirm", {"preview_id": r["preview_id"], "confirm": True})
        self.assertEqual(code, 200, out)

    def test_ordinary_tv_actions_cancel_preview_before_acting(self):
        hj = self.h
        for path, body, meth in (("/api/tv/feedback", {"id": "films/101", "action": "like"}, "tv_feedback"),
                                 ("/api/tv/play", {"id": "films/nope"}, "tv_play"),
                                 ("/api/tv/remote/revoke", {}, "remote_revoke"),
                                 ("/api/tv/muse/probe", {}, "muse_probe"),
                                 ("/api/tv/agents/cancel", {}, "cancel_agents"),
                                 ("/api/tv/setup", {}, "request_setup")):
            self.a.mono_clock.t += cv.NAV_QUIET_S
            self.propose(self.a)
            order = []
            orig_h, orig_m = self.a.canvas_human, getattr(self.a, meth)

            def human(kind, _o=orig_h):
                order.append(("human", self.a.canvas.store.status()["preview"] is not None))
                return _o(kind)

            def action(*x, _o=orig_m, **k):
                order.append(("action", self.a.canvas.store.status()["preview"] is not None))
                return _o(*x, **k)
            with mock.patch.object(self.a, "canvas_human", side_effect=human), \
                    mock.patch.object(self.a, meth, side_effect=action):
                self.s.req("POST", path, json.dumps(body), headers=hj)
            self.assertEqual(order, [("human", True), ("action", False)], path)   # annulé AVANT l'action
        # bookkeeping de la page (action d'assistant réclamée, acquittement de lecture) : jamais un geste humain
        for path, body in (("/api/tv/action", {"seq": 99, "phase": "claim"}), ("/api/tv/play/ack", {"seq": 1})):
            with mock.patch.object(self.a, "canvas_human") as m:
                self.s.req("POST", path, json.dumps(body), headers=hj)
            m.assert_not_called()

    def test_page_ui_busy_is_page_scoped_and_reload_keeps_service_tokens(self):
        r = self.propose(self.a)
        code, out = self.post("ui", {"busy": ["panel"]})
        self.assertEqual((code, out["ok"]), (200, True))
        self.assertIsNone(self.state()["preview"])              # panneau ouvert : aperçu annulé
        with self.assertRaises(cv.CanvasError):
            self.propose(self.a)
        for bad in ({"busy": ["playback"]}, {"busy": ["setup"]}, {"busy": "panel"}, {"busy": ["panel"] * 4},
                    {"busy": [1]}, {}):
            self.assertEqual(self.post("ui", bad)[0], 400, bad)
        set_playback(self.a, "playing")
        self.a.canvas_sync()
        with mock.patch.object(self.a.canvas.store, "busy_reset", side_effect=AssertionError("busy_reset")):
            self.assertEqual(self.post("ui", {"busy": []})[0], 200)      # rechargement de la page
        self.assertIn("playback", self.a.canvas.store.busy_reasons())  # jeton du service conservé
        self.assertNotIn("panel", self.a.canvas.store.busy_reasons())
        with self.assertRaises(cv.CanvasError):
            self.propose(self.a)
        set_playback(self.a, "idle")
        self.a.canvas_sync()
        self.assertEqual(self.a.canvas.store.busy_reasons(), ())
        r = self.propose(self.a)
        self.assertEqual(self.post("ui", {"busy": ["hidden", "panel"]})[0], 200)
        self.assertEqual(self.post("ui", {"busy": ["panel"]})[0], 200)  # idempotent : un seul jeton par raison
        self.assertEqual(self.a.canvas.store.busy_reasons(), ("panel",))
        self.assertEqual(self.post("confirm", {"preview_id": r["preview_id"], "confirm": True})[0], 409)

    def test_restore_failed_reports_current_truth_never_unchanged(self):
        r = self.propose(self.a, "hermit", {"preset": "cinema"})
        real = canvas_storage.commit

        def broken(*args, **kw):
            real(*args, **kw)                                 # la nouvelle version est publiée…
            raise cv.CanvasError("restore_failed")            # …puis la restauration échoue
        with mock.patch.object(canvas_storage, "commit", side_effect=broken):
            code, out = self.post("confirm", {"preview_id": r["preview_id"], "confirm": True})
        self.assertEqual((out["ok"], out["error"]), (False, "restore_failed"))
        self.assertNotEqual(out["outcome"], "unchanged")
        self.assertEqual(out["outcome"], "see_state")
        self.assertEqual((out["canvas"]["revision"], out["canvas"]["preset"], out["canvas"]["state"]),
                         (1, "cinema", "applied"))           # vérité relue du disque
        self.assertNotIn("rien", blob(out).lower())
        self.assert_clean(self.a, out)
        rec = [x for x in audit(self.a) if x["tool"] == "canvas"][-1]
        self.assertEqual((rec["ok"], rec["error"], rec["args"]["revision"], rec["args"]["preset"]),
                         (False, "restore_failed", 1, "cinema"))

    def test_bad_bodies(self):
        for p, body in (("confirm", {"preview_id": 5, "confirm": True}), ("cancel", {"preview_id": None}),
                        ("nope", {})):
            code, _ = self.post(p, body)
            self.assertIn(code, (400, 404, 409), p)
        self.assertEqual(self.post("nope", {})[0], 404)


# --------------------------------------------------------------------------- découpage
class TestModule(unittest.TestCase):
    def test_line_caps_and_imports(self):
        import ast
        import sys
        for name, cap in (("canvas_tv", 300), ("app", 1400), ("web", 1530)):
            with open(os.path.join(PI4, "opentv_pi", name + ".py"), encoding="utf-8") as f:
                n = len(f.read().splitlines())
            self.assertLessEqual(n, cap, name)
        with open(os.path.join(PI4, "opentv_pi", "canvas_tv.py"), "rb") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                mods = [node.module] if node.module else [x.name for x in node.names]
                self.assertTrue(set(mods) <= {"agents", "canvas"}, mods)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for x in ([node.module] if isinstance(node, ast.ImportFrom) else [x.name for x in node.names]):
                    self.assertIn(x.split(".")[0], sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
