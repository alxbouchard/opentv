"""Pilotage d'OpenTV par un assistant (scope « control ») : niveaux de politique emboîtés et jamais élargis en silence,
outils sémantiques typés (fiche proposée, lancement par le lecteur officiel, fermeture, touches d'une énumération
fermée), débits par agent, file d'actions TV réclamée une seule fois, priorité humaine, portes de lecture
inchangées, autorisation root explicite (`muse authorize-control`), aucune URL ni secret vers l'agent.

Les états restent distincts : action mise en file (queued), réclamée/appliquée par la page TV (claimed/applied),
lecture en cours (loading/playing), fermée (closed). Aucun test ne prétend aux pixels HDMI (porte terrain).
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests._common import PI4
from tests import test_muse_bridge as tmb
from tests import test_playback as tp
from tests.test_service import Servers, find_chromium, make_app
from opentv_pi import agentport, agents, app as appmod, cli, museadmin, tools, tvcontrol, validate, web

URLISH = ("https://", "http://", "img.example", "spam.example", "evil.example", "films-mirror", "/b/fakrov", "www.")


def no_url(tc, obj):
    blob = json.dumps(obj, ensure_ascii=False)
    for m in URLISH:
        tc.assertNotIn(m, blob)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------------------------------- politique et niveaux
class TestPolicyLevels(unittest.TestCase):
    def test_control_scope_and_nested_levels(self):
        self.assertIn("control", validate.SCOPES)
        lv = appmod.AGENT_LEVELS
        order = ["none", "read", "feedback", "full", "control"]
        for lo, hi in zip(order, order[1:]):
            self.assertTrue(set(lv[lo]) < set(lv[hi]), (lo, hi))          # moindre privilège : inclusion stricte
        self.assertEqual(lv["control"], ["control", "display", "feedback", "read"])
        for name in order:
            self.assertEqual(appmod.level_of(lv[name]), name)
        self.assertEqual(appmod.level_of(["control"]), "control")
        labels = dict(web.LEVEL_LABELS)
        self.assertEqual(set(labels), set(order))
        self.assertIn("Piloter OpenTV", labels["control"])
        self.assertEqual(web.LEVEL_SHORT["control"], labels["control"])

    def test_setup_qr_defaults_never_grant_control(self):
        """Première configuration : Muse = case « apprendre de mes goûts » (feedback au plus), jamais le pilotage ;
        une reconfiguration reprend la politique enregistrée telle quelle (jamais d'élévation silencieuse)."""
        a = make_app(self)
        tok = a.tv_state()["setup"]["token"]
        a.net = {"mode": "ethernet", "ip": "192.168.2.40", "wifi_present": False, "nm_ok": True}
        code, kind, _ = a.setup_submit({"t": tok, "name": "Salon", "net": "keep", "agent_muse": "feedback"})
        self.assertEqual((code, kind), (200, "done"))
        self.assertEqual(a.registry.policy()["muse"], ["feedback", "read"])
        page = web.setup_form("t", {"net_mode": "ethernet", "policy": a.registry.policy()},
                              plan={"step": 2, "mode": "keep", "ssid": None, "error": None})
        self.assertNotIn('value="control" selected', page)
        page = web.setup_form("t", {"net_mode": "ethernet", "policy": {"muse": appmod.AGENT_LEVELS["control"]}},
                              plan={"step": 2, "mode": "keep", "ssid": None, "error": None})
        self.assertIn('value="control" selected', page)                     # préréglage conservé, pas réduit

    def test_install_and_uninstall_never_write_the_agent_policy(self):
        for name in ("install.sh", "uninstall.sh", "prepare-pi.sh"):
            with open(os.path.join(PI4, name), encoding="utf-8") as f:
                text = f.read()
            for bad in ("authorize-control", "set_policy", '"control"'):
                self.assertNotIn(bad, text, (name, bad))

    def test_registry_caps_control_by_policy(self):
        d = tempfile.mkdtemp(prefix="otvpol", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        r = agents.Registry(os.path.join(d, "agents.json"))
        r.set_policy({"muse": ["display", "feedback", "read"], "hermit": [], "hermes": []})
        with self.assertRaises(ValueError) as e:
            r.issue("muse", ["control", "read"])
        self.assertEqual(str(e.exception), "scope_exceeds_policy")
        with self.assertRaises(ValueError):
            r.issue("muse-device", ["control", "display", "feedback", "read"])
        r.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": [], "hermes": []})
        tok = r.issue("muse-device", ["control", "display", "feedback", "read"])
        self.assertEqual(r.authenticate("muse-device", tok), ["control", "display", "feedback", "read"])


# ------------------------------------------------------------------------------------------- schémas et débits
class TestToolsAndRates(unittest.TestCase):
    def test_nav_keys_match_tv_page_and_queue(self):
        with open(os.path.join(PI4, "ui", "tv.js"), encoding="utf-8") as f:
            js = f.read()
        m = re.search(r"var REMOTE_ACTIONS = \{([^}]*)\}", js)
        self.assertEqual(sorted(re.findall(r"(\w+): 1", m.group(1))), sorted(tools.NAV_KEYS))
        self.assertEqual(tools.NAV_KEYS, tvcontrol.NAV_KEYS)

    def test_control_schemas_are_closed(self):
        for name in tools.CONTROL_TOOLS:
            t = tools.TOOL_BY_NAME[name]
            self.assertEqual(t["scope"], "control")
            props = t["inputSchema"]["properties"]
            self.assertLessEqual(set(props), {"id", "text", "key"}, name)
            self.assertFalse(t["inputSchema"]["additionalProperties"])
        ok = tools.validate_args("present_title", {"id": "films/106", "text": "Grand spectacle, très bien noté."})
        self.assertEqual(ok["id"], "films/106")
        for name, args in (("present_title", {"id": "films/106", "text": "voir films-mirror.net/x"}),
                           ("present_title", {"id": "films/106", "text": "192.168.2.14"}),
                           ("present_title", {"id": "/etc/passwd"}), ("play_title", {"id": "films/1", "url": "x"}),
                           ("remote_control", {"key": "power"}), ("remote_control", {"key": "KEY_POWER"}),
                           ("close_player", {"reason": "x"})):
            with self.assertRaises(ValueError, msg=(name, args)):
                tools.validate_args(name, args)

    def svc(self, scopes=("control", "display", "feedback", "read")):
        d = tempfile.mkdtemp(prefix="otvrate", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        reg = agents.Registry(os.path.join(d, "agents.json"))
        tok = reg.issue("muse", list(scopes))
        ro = reg.issue("hermit", ["display", "feedback", "read"])
        calls = []

        class Ctx:
            foryou = None

            def agent_control(self, agent, name, a):
                calls.append((agent, name, a))
                return {"accepted": True}

            def show_message(self, agent, text, seconds):
                return {"shown": True}
        clock = Clock()
        s = agentport.AgentService(Ctx(), reg, agents.Audit(os.path.join(d, "audit.jsonl")), clock=clock)

        def call(tool, args=None, agent="muse", token=tok, uid=1000):
            return s.handle({"v": 1, "agent": agent, "token": token, "op": "call", "tool": tool, "args": args or {}},
                            peer_uid=uid)
        return call, clock, calls, ro

    def test_read_feedback_display_identity_cannot_control(self):
        call, _, calls, ro = self.svc()
        for tool, args in (("present_title", {"id": "films/101"}), ("play_title", {"id": "films/101"}),
                           ("close_player", {}), ("remote_control", {"key": "ok"})):
            self.assertEqual(call(tool, args, agent="hermit", token=ro)["error"], "forbidden_scope", tool)
        self.assertEqual(calls, [])

    def test_remote_control_rate_is_bounded_per_agent_across_uids(self):
        call, clock, calls, _ = self.svc()
        res = [call("remote_control", {"key": "down"}, uid=1000 + i)["ok"] for i in range(5)]
        self.assertEqual(res, [True, True, True, False, False])          # 3 / s, tous comptes confondus
        clock.t += 1.01
        n = 3
        for _ in range(80):                                              # fenêtre minute : 60 au plus
            clock.t += 0.34
            if call("remote_control", {"key": "down"})["ok"]:
                n += 1
        self.assertLessEqual(n, 60)
        self.assertEqual(len(calls), n)

    def test_present_play_close_rates(self):
        call, clock, calls, _ = self.svc()
        self.assertTrue(call("present_title", {"id": "films/101"})["ok"])
        self.assertEqual(call("present_title", {"id": "films/102"})["error"], "rate_limited")
        clock.t += 3.01
        self.assertTrue(call("present_title", {"id": "films/102"})["ok"])
        self.assertTrue(call("play_title", {"id": "films/101"})["ok"])
        clock.t += 5
        self.assertEqual(call("play_title", {"id": "films/101"})["error"], "rate_limited")
        clock.t += 5.01
        self.assertTrue(call("play_title", {"id": "films/101"})["ok"])
        self.assertTrue(call("close_player")["ok"])
        self.assertEqual(call("close_player")["error"], "rate_limited")
        sent = 0
        for _ in range(30):                                              # 6 lancements / 10 min au plus
            clock.t += 11
            sent += call("play_title", {"id": "films/101"})["ok"]
        self.assertLessEqual(sent + 2, 6 + 2)
        self.assertLessEqual(sum(1 for c in calls if c[1] == "play_title"), 6)


# ------------------------------------------------------------------------------------------- file d'actions TV
class TestTvActionQueue(unittest.TestCase):
    def q(self):
        c = Clock()
        return tvcontrol.TvActions(mono=c), c

    def test_claim_once_then_done_once(self):
        q, _ = self.q()
        a = q.push("present", "muse", {"id": "films/101", "item": {"id": "films/101", "poster": "https://img/x"}})
        self.assertEqual((a["seq"], a["state"]), (1, "queued"))
        self.assertNotIn("item", a)
        self.assertEqual([x["seq"] for x in q.pending()], [1])
        got = q.claim(1)
        self.assertEqual((got["kind"], got["id"], got["from"]), ("present", "films/101", "muse"))
        self.assertIsNone(q.claim(1))                                      # jamais deux fois
        self.assertEqual(q.pending(), [])
        self.assertEqual(q.get(1)["state"], "claimed")
        self.assertTrue(q.done(1, True))
        self.assertFalse(q.done(1, True))
        self.assertFalse(q.done(1, False, "x"))
        self.assertEqual(q.get(1)["state"], "applied")
        for bad in (None, "1", True, 1.0, 99):
            self.assertIsNone(q.claim(bad))
        self.assertFalse(q.done(1, "yes"))

    def test_age_is_reported_for_human_priority(self):
        q, c = self.q()
        q.push("key", "muse", {"key": "down"})
        c.t += 1.5
        self.assertEqual(q.pending()[0]["age_ms"], 1500)
        c.t += 0.25
        self.assertEqual(q.claim(1)["age_ms"], 1750)                      # âge à la réclamation (heure du service)

    def test_expiry_lost_superseded_and_bounds(self):
        q, c = self.q()
        q.push("present", "muse", {"id": "films/101"})
        c.t += tvcontrol.CLAIM_TTL_S + 0.1
        self.assertEqual(q.get(1)["state"], "expired")
        self.assertIsNone(q.claim(1))
        q.push("key", "muse", {"key": "down"})
        q.push("present", "muse", {"id": "films/102"})                    # nouvelle fiche : la touche est remplacée
        self.assertEqual((q.get(2)["state"], q.get(2)["reason"]), ("failed", "superseded"))
        self.assertIsNotNone(q.claim(3))
        c.t += tvcontrol.APPLY_TTL_S + 0.1
        self.assertEqual((q.get(3)["state"], q.get(3)["reason"]), ("failed", "lost"))
        for i in range(tvcontrol.PENDING_MAX):
            q.push("key", "muse", {"key": "up"})
        with self.assertRaises(tvcontrol.QueueFull):
            q.push("key", "muse", {"key": "up"})
        q.cancel_queued("tv_busy")
        self.assertEqual(q.pending(), [])
        self.assertTrue(all(x["state"] in ("failed", "expired", "applied") for x in q.recent(50)))
        self.assertTrue(q.done(4, False, "bad reason !") is False)        # non réclamée : refus
        a = q.push("key", "muse", {"key": "ok"})
        q.claim(a["seq"])
        q.done(a["seq"], False, "Weird Reason/../x")
        self.assertEqual(q.get(a["seq"])["reason"], "other")              # raison bornée
        for _ in range(60):
            b = q.push("key", "muse", {"key": "up"})
            q.claim(b["seq"])
            q.done(b["seq"], True)
        self.assertLessEqual(len(q.items), tvcontrol.HISTORY_MAX)
        seqs = [x["seq"] for x in q.recent(50)]
        self.assertEqual(seqs, sorted(seqs))                               # numéros strictement croissants


# ------------------------------------------------------------------------------------------- service réel
class TestAppControl(unittest.TestCase):
    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)                           # source simulée, promotion terrain
        self.c = self.client
        self.s = Servers(self, self.a)
        self.h = dict(self.s.tv_headers(), **{"Content-Type": "application/json"})

    def ctl(self, name, **a):
        return self.a.agent_control("muse", name, tools.validate_args(name, a))

    def tv(self, path, body=None):
        if body is None:
            code, _, text = self.s.req("GET", path, headers=self.s.tv_headers())
        else:
            code, _, text = self.s.req("POST", path, json.dumps(body), headers=self.h)
        return code, json.loads(text)

    def test_present_queue_claim_apply_and_no_url_to_agent(self):
        with self.assertRaises(tools.ToolError) as e:
            self.ctl("present_title", id="films/999")
        self.assertEqual(str(e.exception), "unknown_id")
        r = self.ctl("present_title", id="films/106", text="De la grande science-fiction très bien notée.")
        self.assertEqual((r["accepted"], r["id"], r["title"], r["action"]["state"]), (True, "films/106", "Les Étoiles",
                                                                                    "queued"))
        no_url(self, r)
        st = self.tv("/api/tv/state")[1]
        pend = st["actions"]["pending"]
        self.assertEqual([(x["kind"], x["id"], x["from"]) for x in pend], [("present", "films/106", "muse")])
        it = pend[0]["item"]
        self.assertEqual(it["poster"], "https://img.example/106.jpg")      # affiche approuvée : page TV seulement
        self.assertNotIn("spam.example", json.dumps(it))                   # résumé sans adresse
        self.assertNotIn("detail_path", json.dumps(it))
        self.assertNotIn("/b/fakrov", json.dumps(st))
        seq = pend[0]["seq"]
        code, body = self.tv("/api/tv/action", {"seq": seq, "phase": "claim"})
        self.assertEqual((code, body["action"]["id"]), (200, "films/106"))
        self.assertEqual(self.tv("/api/tv/action", {"seq": seq, "phase": "claim"})[0], 409)   # une seule fois
        self.assertEqual(self.tv("/api/tv/state")[1]["actions"]["pending"], [])
        st = self.a.agent_status("muse", ["control", "read"])
        self.assertEqual(st["control"]["actions"][-1]["state"], "claimed")
        self.assertEqual(self.tv("/api/tv/action", {"seq": seq, "phase": "done", "ok": True})[0], 200)
        self.assertEqual(self.tv("/api/tv/action", {"seq": seq, "phase": "done", "ok": True})[0], 409)
        st = self.a.agent_status("muse", ["control", "read"])
        self.assertEqual(st["control"]["actions"][-1], {"seq": seq, "kind": "present", "state": "applied",
                                                        "reason": None, "id": "films/106"})
        no_url(self, st)
        self.assertNotIn("actions", self.a.agent_status("hermit", ["read"])["control"])   # sans control : rien
        for bad in ({"seq": "1", "phase": "claim"}, {"seq": True, "phase": "claim"}, {"seq": seq, "phase": "x"}):
            self.assertIn(self.tv("/api/tv/action", bad)[0], (400, 409))
        code, _, _ = self.s.req("POST", "/api/tv/action", json.dumps({"seq": seq, "phase": "claim"}),
                                headers={"Content-Type": "application/json", "X-OpenTV-TV": "wrong"})
        self.assertEqual(code, 403)                                         # clé de page exigée

    def test_play_uses_the_official_gates_and_states_stay_distinct(self):
        # porte de production retirée : aucun lancement, aucune ouverture du contrôleur
        with mock.patch.object(self.a.playback, "gate", lambda: "missing"):
            with self.assertRaises(tools.ToolError) as e:
                self.ctl("play_title", id="films/106")
            self.assertEqual(str(e.exception), "playback_unavailable")
        self.assertEqual([r for r in self.c.reqs if r["op"] == "open"], [])
        self.a.playback.refresh(force=True)
        self.c.reveal_after = 10 ** 6                                      # reste en chargement
        r = self.ctl("play_title", id="films/106")
        self.assertEqual((r["playback"]["state"], r["action"]["state"], r["action"]["kind"]),
                         ("loading", "queued", "play"))                    # accepté ≠ appliqué ≠ en lecture
        no_url(self, r)
        end = time.monotonic() + 5
        while time.monotonic() < end and not any(x["op"] == "open" for x in self.c.reqs):
            time.sleep(0.01)                                               # ouverture envoyée par le fil de lecture
        opened = [x for x in self.c.reqs if x["op"] == "open"]
        self.assertEqual(len(opened), 1)
        self.assertNotIn("evil", opened[0]["url"])
        # lecture active : aucune fiche ni touche ne vole le focus du lecteur ; pas de second lancement
        for name, a, code in (("present_title", {"id": "films/101"}, "player_active"),
                              ("remote_control", {"key": "up"}, "player_active"),
                              ("remote_control", {"key": "ok"}, "player_active"),
                              ("play_title", {"id": "films/101"}, "player_busy")):
            with self.assertRaises(tools.ToolError) as e:
                self.ctl(name, **a)
            self.assertEqual(str(e.exception), code, name)
        self.c.reveal_after, self.c.polls = 1, 0
        self.assertEqual(tp.wait_state(self.a.playback, ("playing",))["state"], "playing")
        st = self.a.agent_status("muse", ["control", "read"])["control"]
        self.assertEqual((st["playback"]["state"], st["playback"]["id"]), ("playing", "films/106"))
        self.assertEqual(st["actions"][-1]["state"], "queued")             # la page TV ne l'a pas encore prise
        r = self.ctl("close_player")
        self.assertTrue(r["closed"])
        v = tp.wait_state(self.a.playback, ("closed",))
        self.assertEqual((v["state"], v["reason"]), ("closed", "agent"))
        self.assertEqual(self.ctl("close_player")["closed"], False)
        self.assertTrue(self.a.playback.ack(v["seq"]))
        # touche Retour d'un assistant pendant une lecture : ferme le lecteur côté service (aucune action TV)
        self.c.polls = 0
        self.a.playback.start("films/101", self.a._fy_lookup("films/101")[0])
        tp.wait_state(self.a.playback, ("playing",))
        n = len(self.a.tv_actions.items)
        self.assertEqual(self.ctl("remote_control", key="back")["effect"], "player_closing")
        self.assertEqual(len(self.a.tv_actions.items), n)
        self.assertEqual(tp.wait_state(self.a.playback, ("closed",))["reason"], "agent")

    def test_human_sheets_and_setup_refuse_control_and_cancel_queued(self):
        self.ctl("remote_control", key="down")
        ok, _ = self.a.request_agents()                                     # « Gérer les assistants » ouvert
        self.assertTrue(ok)
        self.assertEqual(self.a.tv_actions.recent()[-1]["reason"], "tv_busy")
        for name, a in (("present_title", {"id": "films/101"}), ("remote_control", {"key": "ok"}),
                        ("remote_control", {"key": "back"}), ("play_title", {"id": "films/101"})):
            with self.assertRaises(tools.ToolError) as e:
                self.ctl(name, **a)
            self.assertEqual(str(e.exception), "tv_busy", name)
        self.assertEqual(self.ctl("close_player")["closed"], False)        # fermer reste toujours permis
        self.a.cancel_agents("tv")
        self.ctl("present_title", id="films/101")
        self.a.request_setup()
        self.a.tick()
        self.assertEqual(self.a.tv_actions.recent()[-1]["state"], "failed")
        with self.assertRaises(tools.ToolError) as e:
            self.ctl("present_title", id="films/101")
        self.assertEqual(str(e.exception), "tv_setup_active")
        self.assertEqual(self.a.tv_state()["actions"]["pending"], [])
        self.assertEqual([r for r in self.c.reqs if r["op"] == "open"], [])

    def test_through_the_real_agent_port_with_audit_and_scrub(self):
        sock = os.path.join(tempfile.mkdtemp(prefix="otvap", dir="/tmp"), "a.sock")
        self.addCleanup(shutil.rmtree, os.path.dirname(sock), True)
        self.a.registry.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": ["read"], "hermes": []})
        tok = self.a.registry.issue("muse-device", appmod.AGENT_LEVELS["control"])
        ro = self.a.registry.issue("hermit", ["read"])
        svc = agentport.AgentService(self.a, self.a.registry, self.a.audit)
        srv = agentport.AgentPortServer(sock, svc.handle)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def call(tool, args, agent="muse-device", token=tok):
            return agentport.call(sock, {"v": 1, "agent": agent, "token": token, "op": "call", "tool": tool,
                                         "args": args})
        r = call("present_title", {"id": "films/104", "text": "Action culte, IMDb 7,7."})
        self.assertTrue(r["ok"], r)
        no_url(self, r)
        self.assertEqual(call("present_title", {"id": "films/104"}, agent="hermit", token=ro)["error"],
                         "forbidden_scope")
        st = call("status", {})
        self.assertTrue(st["ok"])
        self.assertEqual(st["result"]["control"]["actions"][-1]["state"], "queued")
        no_url(self, st)
        with open(self.a.cfg.audit_file, encoding="utf-8") as f:
            audit = f.read()
        self.assertIn('"tool": "present_title"', audit)
        self.assertIn('"id": "films/104"', audit)                           # identifiant stable audité, texte non
        self.assertNotIn("Action culte", audit)                             # texte libre jamais audité
        self.assertNotIn(tok, audit)
        # politique de Muse réduite sur la TV : l'identité liée perd control dans la même écriture
        _, fp, _ = self.a.registry.snapshot(appmod.KNOWN_AGENTS)
        self.a.registry.apply_policy_changes({"muse": appmod.AGENT_LEVELS["full"]}, appmod.KNOWN_AGENTS, fp)
        self.assertEqual(call("remote_control", {"key": "down"})["error"], "forbidden_scope")


# ------------------------------------------------------------------------------------------- autorisation root
class TestAuthorizeControl(tmb.AdminBase):
    def setUp(self):
        super().setUp()
        agents.Registry(self.cfg.registry_file).set_policy({"muse": ["feedback", "read"], "hermit": ["read"],
                                                            "hermes": []})

    def pol(self):
        return agents.Registry(self.cfg.registry_file).policy()

    def test_plan_without_yes_writes_nothing_then_yes_widens_only_muse(self):
        before = tmb.read(self.cfg.registry_file).encode()
        self.euid = 1000
        self.assertEqual(self.admin().authorize_control(yes=True)[0]["error"], "root_required")
        self.euid = 0
        r, code = self.admin().authorize_control()
        self.assertEqual((r["error"], code, r["changed"]), ("confirmation_required", 2, False))
        self.assertEqual(r["policy_after"], ["control", "display", "feedback", "read"])
        self.assertEqual(tmb.read(self.cfg.registry_file).encode(), before)
        self.assertEqual(self.pair()[0]["error"], "muse_control_not_authorized")
        r, code = self.admin().authorize_control(yes=True)
        self.assertEqual((code, r["changed"]), (0, True), r)
        self.assertEqual(self.pol(), {"muse": ["control", "display", "feedback", "read"], "hermit": ["read"],
                                      "hermes": []})
        self.assertIsNone(agents.Registry(self.cfg.registry_file).list().get("muse-device"))   # aucune émission
        self.assertEqual(self.admin().authorize_control(yes=True)[0]["changed"], False)       # idempotent
        r, code = self.pair()
        self.assertEqual(code, 0, r)
        self.assertEqual(r["agent"]["scopes"], ["control", "display", "feedback", "read"])

    def test_revoke_control_reduces_the_bridge_identity_in_the_same_write(self):
        self.admin().authorize_control(yes=True)
        self.assertEqual(self.pair()[1], 0)
        client = os.path.join(self.cfg.agent_clients_dir, agents.client_file_name("muse-device", os.getuid()))
        tok = json.loads(tmb.read(client))["token"]
        r, code = self.admin().revoke_control()
        self.assertEqual((code, r["changed"]), (0, True), r)
        reg = agents.Registry(self.cfg.registry_file)
        self.assertEqual(reg.policy()["muse"], ["display", "feedback", "read"])
        self.assertEqual(reg.list()["muse-device"]["scopes"], ["display", "feedback", "read"])
        self.assertEqual(reg.authenticate("muse-device", tok), ["display", "feedback", "read"])
        self.assertEqual(reg.policy()["hermit"], ["read"])                  # autres assistants intacts
        self.assertIn("muse_control_not_authorized", self.admin().status()["gates"])

    def test_cli_wiring(self):
        p = cli.build_parser()
        a = p.parse_args(["muse", "authorize-control", "--yes"])
        self.assertEqual((a.action, a.yes), ("authorize-control", True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = museadmin.main(p.parse_args(["muse", "authorize-control"]), cfg=self.cfg, admin=self.admin())
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "confirmation_required")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(museadmin.main(p.parse_args(["muse", "revoke-control"]), cfg=self.cfg,
                                            admin=self.admin()), 0)

    def test_mac_helper_never_takes_a_token_argument(self):
        path = os.path.join(PI4, "tools", "muse-pair-from-mac.sh")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(os.access(path, os.X_OK))
        self.assertIn("read -r -s MUSE_TOKEN", text)
        self.assertIn("--token-stdin", text)
        self.assertIn('"$answer" != "oui"', text)
        self.assertNotIn("echo \"$MUSE_TOKEN", text)
        self.assertNotIn("$MUSE_TOKEN\" >", text)
        for line in text.splitlines():
            if "ssh " in line and "MUSE_TOKEN" in line:
                self.assertTrue(line.strip().startswith("printf '%s\\n' \"$MUSE_TOKEN\" | ssh"), line)
        p = subprocess.run(["bash", "-n", path], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = subprocess.run(["bash", path], capture_output=True)
        self.assertEqual(p.returncode, 2)


# ------------------------------------------------------------------------------------------- vraie page TV
@unittest.skipUnless(find_chromium() and shutil.which("node") and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestRealTvControl(unittest.TestCase):
    def test_proposal_play_close_return_and_human_priority(self):
        """Vraie page /tv 1920×1080 : fiche proposée par Muse (film hors des rangées et film des rangées), touche
        d'assistant refusée vers les Réglages et après une touche humaine, lancement par play_title (écran OpenTV
        puis lecteur), fermeture par close_player et retour sur la même fiche ; chaque action appliquée une fois,
        même après rechargement de la page."""
        a = tp.TestAppIntegration.app(self)
        # horloge réelle pour la file d'actions : l'âge d'une action (priorité humaine) se mesure en vrai temps
        a.tv_actions = tvcontrol.TvActions(mono=time.monotonic, log=a.log)
        c = self.client
        s = Servers(self, a)
        a.foryou.cli(["feedback", "--id", "films/103", "--action", "seen"])   # hors des rangées « Pour toi »
        a._fy_rebuild()
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvctl", dir="/tmp")
        os.makedirs(shots, exist_ok=True)
        ctl_path = os.path.join(shots, "ctl.jsonl")
        res_path = os.path.join(shots, "ctl-res.jsonl")
        for pth in (ctl_path, res_path):
            if os.path.exists(pth):
                os.remove(pth)
        rates = mock.patch.dict(agentport.TOOL_RATES, clear=True)
        rates.start()
        self.addCleanup(rates.stop)
        import threading
        stop = threading.Event()
        c.reveal_after = 25                                                  # ~5 s de chargement visible

        def agent():   # le script JS écrit une commande par ligne ; Python l'exécute comme l'Agent Port
            done = 0
            while not stop.is_set():
                try:
                    with open(ctl_path, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except OSError:
                    lines = []
                for line in lines[done:]:
                    cmd = json.loads(line)
                    try:
                        if cmd["tool"] == "status":
                            out = {"ok": True, "result": None}
                        elif cmd["tool"] == "wait":
                            out = {"ok": True, "result": tp.wait_state(a.playback, tuple(cmd["states"]), 15)}
                        else:
                            out = {"ok": True, "result": a.agent_control("muse", cmd["tool"], tools.validate_args(
                                cmd["tool"], cmd.get("args") or {}))}
                    except tools.ToolError as e:
                        out = {"ok": False, "error": str(e)}
                    out["status"] = a.agent_status("muse", ["control", "read"])["control"]
                    with open(res_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(out) + "\n")
                    done += 1
                time.sleep(0.03)
        th = threading.Thread(target=agent, daemon=True)
        th.start()
        self.addCleanup(stop.set)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "shots": shots,
               "ctl": ctl_path, "res": res_path}
        p = subprocess.run(["node", os.path.join(PI4, "tests", "e2e_control.js"), os.environ["OPENTV_TEST_PLAYWRIGHT"],
                            json.dumps(cfg)], capture_output=True, timeout=240)
        stop.set()
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        st = {x["s"]: x for x in res["steps"]}
        # 1. fiche proposée, film hors des rangées : panneau « Proposé par Muse » avec sa phrase, Regarder en premier
        pr = st["proposed"]
        self.assertEqual((pr["panel"], pr["kicker"], pr["ptitle"]), (True, "Proposé par Muse", "Rire Encore"))
        self.assertTrue(pr["pmeta"].startswith("« Une comédie pour finir la semaine. »"), pr["pmeta"])
        self.assertEqual(pr["btns"][0], {"t": "Regarder", "focused": True})
        self.assertEqual(res["applied"]["present1"], "applied")
        # 2. touches d'assistant : jamais OK sur « Regarder », jamais les Réglages, refusées après une touche humaine
        self.assertEqual(res["applied"]["ok_on_watch"], ["failed", "use_play_title"])
        self.assertEqual(res["applied"]["human_first"], ["failed", "human_active"])
        self.assertEqual(res["applied"]["settings_ok"], ["failed", "settings_forbidden"])
        self.assertEqual(res["applied"]["up"], ["applied", None])           # ↑ depuis la rangée 0 : cercle Réglages
        self.assertFalse(st["after_settings_try"]["panel"])                 # … mais jamais ouvert par l'assistant
        self.assertIn("focused", st["after_settings_try"]["gear"])
        # 3. film des rangées : focus sur SA carte puis fiche
        f2 = st["proposed_in_rows"]
        self.assertEqual((f2["focus"], f2["ptitle"], f2["kicker"]), ("Les Étoiles", "Les Étoiles", "Proposé par Muse"))
        # 4. play_title : écran OpenTV « Chargement… » puis lecteur ; close_player : retour sur la même fiche
        ld = st["agent_loading"]
        self.assertEqual((ld["overlay"], ld["ovTitle"], ld["ovMsg"]), (True, "Les Étoiles", "Chargement…"))
        self.assertEqual(res["playing_seen"], "playing")
        back = st["returned"]
        self.assertEqual((back["overlay"], back["panel"], back["ptitle"], back["focus"], back["kicker"]),
                         (False, True, "Les Étoiles", "Les Étoiles", "Proposé par Muse"))
        self.assertTrue(back["pmeta"].startswith("« Science-fiction très bien notée. »"), back["pmeta"])
        self.assertEqual(back["btns"][0], {"t": "Regarder", "focused": True})
        self.assertEqual(res["after_return"], "idle")
        self.assertEqual(res["applied"]["play"], "applied")
        # 5. page rechargée : aucune action déjà appliquée n'est rejouée
        self.assertEqual(res["reload_pending"], 0)
        self.assertEqual(st["reloaded"]["panel"], False)
        # jamais d'iframe, de navigation hors de /tv ni d'adresse dans le DOM
        for x in res["steps"]:
            self.assertEqual((x["iframes"], x["url"]), (0, "/tv"))
        self.assertEqual(len([r for r in c.reqs if r["op"] == "open"]), 1)
