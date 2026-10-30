"""Calibrage conversationnel rapide de Muse et garde-fou anti-répétition des films déjà vus.

Contrat (autorisé par Alex) :
- exactement les 11 commandes Muse existantes : aucun outil, shell, URL ni accès base nouveau ;
- present_title / play_title portent un `mode` facultatif (enum strict new|rewatch, défaut new) ; un film marqué
  « Déjà vu » (marks.seen) est refusé `already_seen` en mode new, APRÈS résolution du titre et AVANT toute action TV
  ou Playback.start ; `rewatch` (seulement sur demande explicite d'Alex) suit le chemin normal, sans jamais démarquer ;
- catalogue_list expose des marques publiques bornées (seen/opinion/watchlist/not_tonight) lues dans le profil local
  au moment de l'appel, sans URL ni champ interne ;
- le calibrage passe par recommend (limit ≤ 20) + feedback, un film à la fois, selon un protocole français explicite ;
- aucun « vu » n'est jamais déduit d'une proposition, d'un lancement ou d'une durée.
"""
import json
import os
import time
import unittest
from unittest import mock

from tests._common import NANOPI
from tests import test_agent_port as tap
from tests import test_muse_bridge as tmb
from tests import test_playback as tp
from opentv_pi import agentport, agents, app as appmod, foryou, mcp, musebridge, tools

URLISH = ("https://", "http://", "img.example", "spam.example", "evil.example", "/b/fakrov", "www.", "fakrov")
MARK_KEYS = {"opinion", "seen", "watchlist", "not_tonight"}
ELEVEN = ["opentv.catalogue_list", "opentv.close_player", "opentv.display_message", "opentv.feedback",
          "opentv.history", "opentv.play_title", "opentv.present_title", "opentv.recommend", "opentv.remote_control",
          "opentv.search", "opentv.status"]


def no_url(tc, obj):
    blob = json.dumps(obj, ensure_ascii=False)
    for m in URLISH:
        tc.assertNotIn(m, blob)


def feedback(fy, uid, *actions):
    """Avis explicite enregistré comme le ferait Muse (CLI agent du moteur, même base que la TV)."""
    for act in actions:
        code, out = fy.cli(["feedback", "--id", uid, "--action", act])
        assert code == 0 and out.get("ok"), out


def marks(fy, uid):
    return fy.engine.marks_for(fy.tv.call("profile") or {}, [uid], fy.clock())[uid]


# ------------------------------------------------------------------------------------------- schéma `mode`
class TestModeSchema(unittest.TestCase):
    def test_mode_is_an_optional_strict_string_enum_defaulting_to_new(self):
        for name in ("present_title", "play_title"):
            sch = tools.TOOL_BY_NAME[name]["inputSchema"]
            p = sch["properties"]["mode"]
            self.assertEqual((p["type"], p["enum"], p["default"]), ("string", ["new", "rewatch"], "new"), name)
            self.assertNotIn("mode", sch["required"], name)
            self.assertFalse(sch["additionalProperties"])
            self.assertEqual(tools.validate_args(name, {"id": "films/106"})["mode"], "new")
            self.assertEqual(tools.validate_args(name, {"id": "films/106", "mode": "rewatch"})["mode"], "rewatch")
            for bad in ("REWATCH", "Rewatch", "", "again", "revoir", "new ", True, False, 1, 0, None, ["rewatch"]):
                with self.assertRaises(ValueError, msg=(name, bad)):
                    tools.validate_args(name, {"id": "films/106", "mode": bad})
        # aucun autre outil ne gagne de mode ; le validateur reste integer/string seulement (aucun booléen)
        for t in tools.TOOLS:
            if t["name"] not in ("present_title", "play_title"):
                self.assertNotIn("mode", t["inputSchema"]["properties"], t["name"])
            for p in t["inputSchema"]["properties"].values():
                self.assertIn(p["type"], ("integer", "string"), t["name"])

    def test_exactly_the_eleven_muse_commands_and_tools(self):
        self.assertEqual(sorted(musebridge.COMMANDS), ELEVEN)
        self.assertEqual(sorted(musebridge.command_specs()), ELEVEN)
        self.assertEqual(len(tools.TOOLS), 11)
        self.assertEqual(sorted(musebridge.COMMANDS.values()), sorted(t["name"] for t in tools.TOOLS))

    def test_muse_specs_expose_mode_as_optional_enum_and_bridge_validates_it(self):
        specs = musebridge.command_specs()
        self.assertEqual(set(specs["opentv.present_title"]["optional"]), {"text", "mode"})
        self.assertEqual(set(specs["opentv.play_title"]["optional"]), {"mode"})
        for cmd in ("opentv.present_title", "opentv.play_title"):
            m = specs[cmd]["optional"]["mode"]
            self.assertEqual(m["type"], "string")
            self.assertIn("new, rewatch", m["description"])
            self.assertIn("défaut new", m["description"])
            self.assertIn("explicite", m["description"])
            self.assertEqual(musebridge.validate_params(cmd, {"id": "films/106"})["mode"], "new")
            self.assertEqual(musebridge.validate_params(cmd, {"id": "films/106", "mode": None})["mode"], "new")
            self.assertEqual(musebridge.validate_params(cmd, {"id": "films/106", "mode": "rewatch"})["mode"],
                             "rewatch")
            for bad in (True, "REWATCH", "", "x", 1, "https://evil.example/x"):
                with self.assertRaises(musebridge.BridgeError, msg=(cmd, bad)):
                    musebridge.validate_params(cmd, {"id": "films/106", "mode": bad})


# ------------------------------------------------------------------------------------------- garde-fou App
class TestSeenGuard(unittest.TestCase):
    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)                           # source simulée, promotion terrain
        self.c = self.client
        self.fy = self.a.foryou

    def ctl(self, name, **a):
        return self.a.agent_control("muse-device", name, tools.validate_args(name, a))

    def refused(self, name, code, **a):
        with self.assertRaises(tools.ToolError) as e:
            self.ctl(name, **a)
        self.assertEqual(str(e.exception), code, (name, a))

    def opens(self):
        return [r for r in self.c.reqs if r["op"] == "open"]

    def test_present_title_refuses_a_seen_title_without_queuing_anything(self):
        self.a.tv_foryou()                                  # sélection TV figée AVANT l'avis (marques périmées)
        feedback(self.fy, "films/106", "seen")              # Alex le dit à Muse : « déjà vu »
        n = len(self.a.tv_actions.items)
        self.refused("present_title", "already_seen", id="films/106")
        self.refused("present_title", "already_seen", id="films/106", text="Grand spectacle.", mode="new")
        self.assertEqual(len(self.a.tv_actions.items), n)                    # aucune fiche mise en file
        self.assertEqual(self.a.tv_state()["actions"]["pending"], [])
        self.assertTrue(marks(self.fy, "films/106")["seen"])                 # jamais démarqué
        r = self.ctl("present_title", id="films/101")                        # titre non vu : chemin normal
        self.assertEqual((r["accepted"], r["action"]["state"]), (True, "queued"))

    def test_play_title_refuses_a_seen_title_before_playback_start(self):
        feedback(self.fy, "films/106", "seen", "like")
        with mock.patch.object(self.a.playback, "start", wraps=self.a.playback.start) as start:
            self.refused("play_title", "already_seen", id="films/106")
            self.refused("play_title", "already_seen", id="films/106", mode="new")
            start.assert_not_called()
        time.sleep(0.05)
        self.assertEqual(self.opens(), [])
        self.assertEqual(self.a.playback.view()["state"], "idle")
        self.assertEqual(self.a.tv_state()["actions"]["pending"], [])
        self.assertEqual(marks(self.fy, "films/106"), {"opinion": "like", "seen": True, "watchlist": False,
                                                        "not_tonight": False})

    def test_rewatch_follows_the_normal_path_and_never_unmarks(self):
        feedback(self.fy, "films/106", "seen")
        r = self.ctl("present_title", id="films/106", mode="rewatch", text="Tu voulais le revoir.")
        self.assertEqual((r["accepted"], r["action"]["state"]), (True, "queued"))
        pend = self.a.tv_state()["actions"]["pending"]
        self.assertEqual([(x["kind"], x["id"]) for x in pend], [("present", "films/106")])
        self.assertTrue(pend[0]["item"]["marks"]["seen"])                    # la fiche TV montre « Déjà vu »
        with mock.patch.object(self.a.playback, "start", wraps=self.a.playback.start) as start:
            r = self.ctl("play_title", id="films/106", mode="rewatch")
            self.assertEqual(start.call_count, 1)
        self.assertEqual(r["playback"]["state"], "loading")
        self.assertEqual(tp.wait_state(self.a.playback, ("playing",))["state"], "playing")
        self.assertEqual(len(self.opens()), 1)
        self.assertTrue(marks(self.fy, "films/106")["seen"])

    def test_existing_player_and_priority_gates_still_come_first(self):
        feedback(self.fy, "films/106", "seen")
        with self.a.lock:
            self.a.agents_mode = True                                       # « Gérer les assistants » ouvert
        for name in ("present_title", "play_title"):
            self.refused(name, "tv_busy", id="films/106")
            self.refused(name, "tv_busy", id="films/106", mode="rewatch")
        with self.a.lock:
            self.a.agents_mode = False
        self.refused("present_title", "unknown_id", id="films/999")
        # rewatch ne contourne aucune porte de lecture : production retirée ⇒ indisponible
        with mock.patch.object(self.a.playback, "gate", lambda: "missing"):
            self.a.playback.refresh(force=True)
            self.refused("play_title", "playback_unavailable", id="films/106", mode="rewatch")
        self.a.playback.refresh(force=True)
        self.ctl("play_title", id="films/101")
        tp.wait_state(self.a.playback, ("playing",))
        self.refused("present_title", "player_active", id="films/104", mode="rewatch")
        self.refused("play_title", "player_busy", id="films/104", mode="rewatch")

    def test_exact_documented_check_order_around_already_seen(self):
        """README « Anti-répétition » : avant = configuration, « Gérer les assistants », Muse occupée, et pour
        present_title seulement player_returning ; puis unknown_id ; puis already_seen AVANT player_active et toutes
        les portes de Playback.start (player_busy, playback_unavailable, player_returning de play_title)."""
        feedback(self.fy, "films/106", "seen")
        with self.a.lock:
            self.a.setup_mode = True
        for name in ("present_title", "play_title"):
            self.refused(name, "tv_setup_active", id="films/106")
        with self.a.lock:
            self.a.setup_mode = False
        with mock.patch.object(self.a.muse, "busy", lambda: True):
            for name in ("present_title", "play_title"):
                self.refused(name, "tv_busy", id="films/106")
        # lecteur actif : already_seen passe AVANT player_active / player_busy
        self.ctl("play_title", id="films/101")
        tp.wait_state(self.a.playback, ("playing",))
        self.refused("present_title", "already_seen", id="films/106")
        self.refused("play_title", "already_seen", id="films/106")
        self.refused("present_title", "player_active", id="films/106", mode="rewatch")
        self.refused("play_title", "player_busy", id="films/106", mode="rewatch")
        # fin de lecture non acquittée : present_title -> player_returning AVANT already_seen ; play_title -> already_seen
        self.ctl("close_player")
        v = tp.wait_state(self.a.playback, ("closed",))
        self.refused("present_title", "player_returning", id="films/106")
        self.refused("play_title", "already_seen", id="films/106")
        self.refused("play_title", "player_returning", id="films/106", mode="rewatch")
        self.assertTrue(self.a.playback.ack(v["seq"]))
        # production retirée : play_title d'un film vu -> already_seen avant playback_unavailable
        with mock.patch.object(self.a.playback, "gate", lambda: "missing"):
            self.a.playback.refresh(force=True)
            self.refused("play_title", "already_seen", id="films/106")
            self.refused("play_title", "playback_unavailable", id="films/106", mode="rewatch")
        self.a.playback.refresh(force=True)

    def test_no_seen_is_ever_inferred_from_presenting_playing_or_duration(self):
        self.ctl("present_title", id="films/101")
        self.ctl("play_title", id="films/101")
        tp.wait_state(self.a.playback, ("playing",))
        time.sleep(0.2)                                                     # « durée » de lecture
        self.ctl("close_player")
        v = tp.wait_state(self.a.playback, ("closed",))
        self.assertTrue(self.a.playback.ack(v["seq"]))
        self.assertEqual(marks(self.fy, "films/101"), {"opinion": None, "seen": False, "watchlist": False,
                                                        "not_tonight": False})
        code, out = self.fy.cli(["history", "--list", "seen"])
        self.assertEqual((code, out["items"]), (0, []))
        code, out = self.fy.cli(["history", "--list", "events"])
        self.assertEqual([e for e in out["items"] if e["action"] in ("seen", "unseen")], [])
        # le titre reste proposable puis relançable en mode new (aucune marque implicite)
        self.ctl("present_title", id="films/101")
        self.assertIn(self.ctl("play_title", id="films/101")["playback"]["state"], ("loading", "playing"))

    def test_already_seen_crosses_agent_port_mcp_and_bridge_with_a_stable_code(self):
        rates = mock.patch.dict(agentport.TOOL_RATES, clear=True)          # débits testés à part (test_muse_control)
        rates.start()
        self.addCleanup(rates.stop)
        self.a.registry.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": [], "hermes": []})
        tok = self.a.registry.issue("muse-device", appmod.AGENT_LEVELS["control"])
        svc = agentport.AgentService(self.a, self.a.registry, self.a.audit)
        feedback(self.fy, "films/106", "seen")
        req = {"v": 1, "agent": "muse-device", "token": tok, "op": "call"}
        for tool in ("present_title", "play_title"):
            self.assertEqual(svc.handle(dict(req, tool=tool, args={"id": "films/106"})),
                             {"ok": False, "error": "already_seen"})
        with open(self.a.cfg.audit_file, encoding="utf-8") as f:
            audit = [json.loads(l) for l in f if l.strip()]
        self.assertEqual([(x["tool"], x.get("error")) for x in audit[-2:]],
                         [("present_title", "already_seen"), ("play_title", "already_seen")])
        # MCP : erreur outil expliquée en français, code stable conservé
        s = mcp.Session(lambda r: svc.handle(dict(req, **r)))
        s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "present_title", "arguments": {"id": "films/106"}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("(already_seen)", r["result"]["content"][0]["text"])
        self.assertIn("déjà vu", mcp.ERRORS_FR["already_seen"])
        self.assertIn("rewatch", mcp.ERRORS_FR["already_seen"])
        # pont Muse : même code stable vers Muse ; rewatch passe
        cfgp = os.path.join(os.path.dirname(self.a.cfg.audit_file), "agent-muse-device.json")
        agents.write_client_config(cfgp, "muse-device", tok, "/nonexistent.sock")
        port = musebridge.AgentPortMCP(cfgp, call=lambda sock, r, timeout=None: svc.handle(r))
        for cmd in ("opentv.present_title", "opentv.play_title"):
            self.assertEqual(musebridge.dispatch(port, cmd, {"id": "films/106"}),
                             {"ok": False, "error": "already_seen"})
        r = musebridge.dispatch(port, "opentv.present_title", {"id": "films/106", "mode": "rewatch"})
        self.assertTrue(r["ok"], r)
        no_url(self, r)


# ------------------------------------------------------------------------------------------- repli de tv_item
class TestTvItemFallbackWhenProfileUnreadable(unittest.TestCase):
    """ForYou.tv_item quand le profil local est illisible (base verrouillée/corrompue : tv.call("profile") -> None) :
    seule la sélection TV FIGÉE sert de repli. Une marque « Déjà vu » qu'elle contient (même périmée) est conservée et
    le garde refuse toujours `already_seen` ; si ni le profil ni la sélection figée ne savent que le film est vu,
    l'état est réellement inconnu : catalogue_list ne prétend jamais « pas vu » (marks null, store unavailable) et le
    garde de l'App ne peut pas le savoir (limite documentée dans le README)."""

    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)
        self.fy = self.a.foryou

    def ctl(self, name, **a):
        return self.a.agent_control("muse-device", name, tools.validate_args(name, a))

    def refused(self, name, code, **a):
        with self.assertRaises(tools.ToolError) as e:
            self.ctl(name, **a)
        self.assertEqual(str(e.exception), code, (name, a))

    def profile_down(self):
        """Seule la lecture du profil échoue (comme Bridge.call en panne) ; toute autre opération est réelle."""
        orig = self.fy.tv.call
        p = mock.patch.object(self.fy.tv, "call", side_effect=lambda fn, *a, **k:
                              None if fn == "profile" else orig(fn, *a, **k))
        p.start()
        self.addCleanup(p.stop)

    def frozen(self):
        with self.a.fy_lock:
            self.a._fy_rebuild()
            return self.a.fy_session

    def test_stale_frozen_seen_mark_is_preserved_and_still_refused(self):
        sess = self.frozen()
        for uid in ("films/106", "films/103"):                       # Alex appuie « Déjà vu » sur la fiche TV
            self.assertTrue(self.a.tv_feedback(uid, "seen")[0]["seen"], uid)
            self.assertIs(sess["nav"].marks[uid]["seen"], True, uid)  # marque inscrite dans la sélection figée
        # puis Muse retire « vu » pour films/103 dans le profil : la marque figée « vu » de la TV devient PÉRIMÉE
        feedback(self.fy, "films/103", "unseen")
        self.assertFalse(marks(self.fy, "films/103")["seen"])
        self.assertIs(sess["nav"].marks["films/103"]["seen"], True)
        self.profile_down()
        self.assertIsNone(self.fy.tv.call("profile"))
        for uid in ("films/106", "films/103"):
            pub = self.fy.tv_item(sess, uid)
            self.assertIs(pub["marks"]["seen"], True, uid)              # repli : la marque figée est conservée
            self.assertIs(self.a._fy_lookup(uid)[1]["marks"]["seen"], True, uid)
        self.assertIs(self.a.fy_session, sess)                           # aucune sélection reconstruite entre-temps
        n = len(self.a.tv_actions.items)
        with mock.patch.object(self.a.playback, "start", wraps=self.a.playback.start) as start:
            for uid in ("films/106", "films/103"):
                self.refused("present_title", "already_seen", id=uid)
                self.refused("play_title", "already_seen", id=uid)
                self.refused("play_title", "already_seen", id=uid, mode="new")
            start.assert_not_called()
        self.assertEqual(len(self.a.tv_actions.items), n)
        self.assertEqual(self.a.tv_state()["actions"]["pending"], [])
        self.assertEqual(self.a.playback.view()["state"], "idle")
        # revoir explicitement reste possible, et la fiche montre toujours « Déjà vu »
        r = self.ctl("present_title", id="films/106", mode="rewatch")
        self.assertEqual((r["accepted"], r["action"]["state"]), (True, "queued"))
        self.assertIs(self.a.tv_state()["actions"]["pending"][0]["item"]["marks"]["seen"], True)

    def test_unknown_when_neither_profile_nor_frozen_selection_knows(self):
        sess = self.frozen()                                             # sélection figée AVANT l'avis
        feedback(self.fy, "films/104", "seen")                           # « déjà vu » dit à Muse ensuite
        self.assertIs((sess["nav"].marks.get("films/104") or {}).get("seen", False), False)   # la TV l'ignore
        self.assertTrue(self.fy.tv_item(sess, "films/104")["marks"]["seen"])   # profil lisible : connu
        self.profile_down()
        # catalogue_list : jamais un faux « pas vu », l'état est déclaré indisponible
        page = self.fy.catalogue_page(50, 0)
        self.assertEqual(page["store"], "unavailable")
        self.assertTrue(page["items"])
        self.assertTrue(all(it["marks"] is None for it in page["items"]))
        # tv_item n'a ni profil ni marque figée : le garde de l'App ne PEUT pas savoir (limite du README) ;
        # aucune marque « vu » n'est inventée non plus
        self.assertIsNot(self.fy.tv_item(sess, "films/104")["marks"]["seen"], True)


# ------------------------------------------------------------------------------------------- catalogue_list
class TestCatalogueMarks(unittest.TestCase):
    def setUp(self):
        self.fy, self.d = tap.make_foryou(self)

    def page(self, limit=50, offset=0):
        r = tools.dispatch(tap.FakeCtx(self.fy), "muse", ["read"], "catalogue_list",
                           tools.validate_args("catalogue_list", {"limit": limit, "offset": offset}))
        no_url(self, r)
        return r

    def test_every_item_carries_bounded_public_marks_from_the_local_profile(self):
        before = self.page()
        self.assertEqual(before["store"], "ok")
        for it in before["items"]:
            self.assertEqual(it["marks"], {"opinion": None, "seen": False, "watchlist": False, "not_tonight": False})
        feedback(self.fy, "films/102", "seen", "like")
        feedback(self.fy, "films/103", "seen", "dislike")
        feedback(self.fy, "films/104", "watchlist")
        feedback(self.fy, "films/101", "not-tonight")
        r = self.page()                                                       # profil relu à l'appel
        by = {it["id"]: it["marks"] for it in r["items"]}
        self.assertEqual(by["films/102"], {"opinion": "like", "seen": True, "watchlist": False, "not_tonight": False})
        self.assertEqual(by["films/103"], {"opinion": "dislike", "seen": True, "watchlist": False,
                                           "not_tonight": False})
        self.assertEqual(by["films/104"], {"opinion": None, "seen": False, "watchlist": True, "not_tonight": False})
        self.assertEqual(by["films/101"], {"opinion": None, "seen": False, "watchlist": False, "not_tonight": True})
        self.assertEqual(by["films/106"], {"opinion": None, "seen": False, "watchlist": False, "not_tonight": False})
        for it in r["items"]:
            self.assertEqual(set(it["marks"]), MARK_KEYS)
            for bad in ("detail_path", "poster", "site_id", "source_id", "url", "snooze_until", "updated_at",
                        "opinion_at", "watchlist_at", "state"):
                self.assertNotIn(bad, it)
                self.assertNotIn(bad, it["marks"])
        # pagination et marqueur de génération inchangés
        self.assertEqual((r["total"], r["offset"], r["limit"], r["has_more"], r["generated_at"]),
                         (6, 0, 50, False, "2026-10-26T00:00:00Z"))
        p1, p2 = self.page(4, 0), self.page(4, 4)
        self.assertEqual((p1["has_more"], p2["has_more"], len(p1["items"]), len(p2["items"])), (True, False, 4, 2))
        self.assertEqual([x["id"] for x in p1["items"] + p2["items"]], [x["id"] for x in r["items"]])
        self.assertEqual(p1["generated_at"], p2["generated_at"])

    def test_unreadable_profile_never_claims_unseen(self):
        feedback(self.fy, "films/102", "seen")
        with mock.patch.object(self.fy.tv, "call", lambda *a, **k: None):
            r = self.page()
        self.assertEqual(r["store"], "unavailable")
        self.assertTrue(all(it["marks"] is None for it in r["items"]))
        self.assertEqual(r["total"], 6)

    def test_muse_sees_marks_through_the_bridge_and_a_full_page_fits(self):
        fx = tmb.AgentPortFixture(self)
        fx.issue()
        port = musebridge.AgentPortMCP(fx.client)
        self.assertTrue(musebridge.dispatch(port, "opentv.feedback", {"id": "films/106", "action": "seen"})["ok"])
        r = musebridge.dispatch(port, "opentv.catalogue_list", {"limit": 50})
        self.assertTrue(r["ok"], r)
        no_url(self, r)
        by = {it["id"]: it["marks"] for it in r["payload"]["items"]}
        self.assertTrue(by["films/106"]["seen"])
        self.assertFalse(by["films/101"]["seen"])
        self.assertEqual(r["payload"]["generated_at"], "2026-10-26T00:00:00Z")

    def test_worst_case_page_of_fifty_stays_under_the_muse_payload_bound(self):
        d = tap.tmpdir(self)
        items = []
        for i in range(60):
            items.append({"detail_path": "b/fakrov/%d" % (1000 + i), "site_id": str(1000 + i),
                          "title": ("Épopée très longue %02d " % i) + "é" * 170, "year": 2020,
                          "genre": "Aventure, Drame, Comédie, Science-fiction, Thriller, Action, Animation",
                          "description": "Résumé détaillé à rallonge " + "è" * 600,
                          "imdb_rating": 7.5, "imdb_votes": 123456, "tmdb_rating": 7.1, "tmdb_votes": 9876,
                          "tmdb_match_status": "matched", "poster": "https://img.example/%d.jpg" % i})
        cat = os.path.join(d, "catalogue.json")
        with open(cat, "w", encoding="utf-8") as f:
            json.dump({"generated_at": "2026-10-26T00:00:00Z", "items": items}, f)
        fy = foryou.ForYou(NANOPI, os.path.join(d, "state"), cat, clock=lambda: 1800000000.0)
        self.addCleanup(fy.close)
        for i in range(50):
            feedback(fy, "films/%d" % (1000 + i), "seen", "like", "watchlist", "not-tonight")
        payload = musebridge.scrub(tools.dispatch(tap.FakeCtx(fy), "muse-device", ["read"], "catalogue_list",
                                                  {"limit": 50, "offset": 0}))
        self.assertEqual(len(payload["items"]), 50)
        self.assertTrue(all(it["marks"]["seen"] for it in payload["items"]))
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), musebridge.MAX_PAYLOAD_BYTES)


# ------------------------------------------------------------------------------------------- protocole de calibrage
class TestCalibrationProtocol(unittest.TestCase):
    PHRASES = ("un film à la fois", "« vu et aimé »", "seen puis like", "« vu, mais bof »", "seen puis dislike",
               "« pas vu »", "aucun avis", "« à voir »", "watchlist", "suivant", "déjà vu", "jamais dislike",
               "netflix", "prime", "jamais « vu »", "limit", "20", "sans lancer le lecteur",
               "sans afficher", "demande explicite")

    def check(self, text, where):
        low = text.lower()
        for p in self.PHRASES:
            self.assertIn(p, low, (where, p))

    def test_muse_descriptions_carry_the_french_protocol(self):
        d = musebridge.DESCRIPTIONS
        self.check(d["opentv.recommend"], "muse recommend")
        rec = d["opentv.recommend"].lower()
        self.assertIn("par défaut", rec)
        self.assertIn("source", rec)
        fb = d["opentv.feedback"].lower()
        self.assertIn("seen", fb)
        self.assertIn("jamais", fb)
        for cmd in ("opentv.search", "opentv.catalogue_list"):
            self.assertIn("marks.seen" if cmd.endswith("list") else "state.seen", d[cmd], cmd)
            self.assertIn("recommend", d[cmd], cmd)
        for cmd in ("opentv.present_title", "opentv.play_title"):
            low = d[cmd].lower()
            for p in ("mode=new", "rewatch", "explicitement", "already_seen", "marks.seen", "recommend"):
                self.assertIn(p, low, (cmd, p))

    def test_mcp_instructions_and_tool_descriptions_carry_the_same_protocol(self):
        self.check(mcp.INSTRUCTIONS, "mcp instructions")
        self.check(tools.TOOL_BY_NAME["recommend"]["description"], "tool recommend")
        self.assertIn("par défaut", tools.TOOL_BY_NAME["recommend"]["description"])
        self.assertIn("marks", tools.TOOL_BY_NAME["catalogue_list"]["description"])
        for name in ("present_title", "play_title"):
            low = tools.TOOL_BY_NAME[name]["description"].lower()
            for p in ("mode=new", "rewatch", "explicitement", "already_seen"):
                self.assertIn(p, low, (name, p))
        self.assertEqual(tools.TOOL_BY_NAME["recommend"]["inputSchema"]["properties"]["limit"]["maximum"], 20)

    def test_protocol_mutations_only_use_existing_feedback_actions(self):
        for act in ("seen", "like", "dislike", "watchlist"):
            self.assertIn(act, tools.FEEDBACK_ACTIONS)


if __name__ == "__main__":
    unittest.main()
