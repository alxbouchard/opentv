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
        # politique de Muse réduite sur la TV : l'identité liée perd control dans la même écriture et le pont est
        # aussitôt en pause (plus aucun outil, lecture comprise) ; Hermit garde son accès par scope
        _, fp, _ = self.a.registry.snapshot(appmod.KNOWN_AGENTS)
        self.a.registry.apply_policy_changes({"muse": appmod.AGENT_LEVELS["full"]}, appmod.KNOWN_AGENTS, fp)
        self.assertEqual(call("remote_control", {"key": "down"})["error"], "bridge_paused")
        self.assertEqual(call("status", {})["error"], "bridge_paused")
        self.assertTrue(call("status", {}, agent="hermit", token=ro)["ok"])


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

    def test_reauthorizing_control_never_reactivates_a_paused_bridge_until_pair(self):
        """Pilotage retiré puis réautorisé : l'identité du pont reste réduite (Agent Port : bridge_paused), rien ne
        redémarre ; seul `muse pair` la réémet et relance explicitement le pont (arrêt attendu, puis démarrage)."""
        self.admin().authorize_control(yes=True)
        self.assertEqual(self.pair()[1], 0)
        client = os.path.join(self.cfg.agent_clients_dir, agents.client_file_name("muse-device", os.getuid()))
        old = json.loads(tmb.read(client))["token"]
        self.admin().revoke_control()
        n = len(self.calls)
        r, code = self.admin().authorize_control(yes=True)
        self.assertEqual((code, r["changed"]), (0, True), r)
        self.assertEqual([c for c in self.calls[n:] if c[1] not in ("is-active", "is-enabled", "show")], [])
        reg = agents.Registry(self.cfg.registry_file)
        self.assertEqual(reg.list()["muse-device"]["scopes"], ["display", "feedback", "read"])
        self.assertTrue(agents.linked_paused("muse-device", reg.authenticate("muse-device", old)))
        n = len(self.calls)
        r, code = self.pair()
        self.assertEqual((code, r["result"]), (0, "already_paired"), r)
        cmds = [" ".join(c[1:]) for c in self.calls[n:] if c[1] not in ("is-active", "is-enabled", "show")]
        self.assertLess(cmds.index("stop --no-block " + museadmin.UNIT),
                        cmds.index("start --no-block " + museadmin.UNIT))
        self.assertEqual((r["restarted"], r["identity_reissued"], r["registered"]), (True, True, True))
        new = json.loads(tmb.read(client))["token"]
        reg = agents.Registry(self.cfg.registry_file)
        self.assertIsNone(reg.authenticate("muse-device", old))
        self.assertEqual(reg.authenticate("muse-device", new), ["control", "display", "feedback", "read"])
        self.assertFalse(agents.linked_paused("muse-device", reg.authenticate("muse-device", new)))
        n = len(self.calls)                                  # identité déjà exacte : session saine jamais coupée
        self.assertEqual(self.pair()[1], 0)
        later = [" ".join(c[1:]) for c in self.calls[n:]]
        for verb in ("stop --no-block ", "start --no-block ", "reset-failed ", "enable "):
            self.assertNotIn(verb + museadmin.UNIT, later)

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


# ------------------------------------------------------------------------------------------- revue : identité liée
# ------------------------------------------------------------------- course d'arrêt du pont pendant `muse pair`
class TestPairRestartRace(tmb.AdminBase):
    """Course signalée : un ancien pont en pause qui sort (exit 78, sans redémarrage) APRÈS que `muse pair` l'a
    « démarré » laissait le service arrêté derrière un faux succès. Contrat : un seul `muse pair` attend la fin réelle
    de l'ancien processus avant de changer l'identité, ne démarre qu'ensuite, et relit systemd + le statut écrit par
    le NOUVEAU processus -> pont relancé et enregistré, ou échec explicite ; jamais succès avec un pont arrêté."""

    U = museadmin.UNIT

    def setUp(self):
        super().setUp()
        self.admin().authorize_control(yes=True)
        r, code = self.pair()
        self.assertEqual((code, r["result"], self.unit.active), (0, "paired", "active"), r)
        self.first_pid = self.unit.pid
        self.client = os.path.join(self.cfg.agent_clients_dir, agents.client_file_name("muse-device", os.getuid()))

    def token(self):
        return json.loads(tmb.read(self.client))["token"]

    def reg(self):
        return agents.Registry(self.cfg.registry_file)

    def cmds_since(self, n):
        return [" ".join(c[1:]) for c in self.calls[n:] if c[1] not in ("is-active", "is-enabled", "show")]

    def watch_identity_changes(self):
        """Toute émission/révocation de « muse-device » est notée avec l'état du pont à cet instant."""
        seen = []
        orig_issue, orig_discard = museadmin.Admin.issue_identity, museadmin.Admin.discard_identity

        def issue(adm, pw):
            seen.append(("issue", self.unit.active, self.unit.pid))
            return orig_issue(adm, pw)

        def discard(adm, pw, only_owned=True):
            seen.append(("discard", self.unit.active, self.unit.pid))
            return orig_discard(adm, pw, only_owned)
        for p in (mock.patch.object(museadmin.Admin, "issue_identity", issue),
                  mock.patch.object(museadmin.Admin, "discard_identity", discard)):
            p.start()
            self.addCleanup(p.stop)
        return seen

    def assert_new_bridge_survives(self, r, code):
        new_pid = self.unit.pid
        self.fake_sleep(120)                                           # bien après la sortie prévue de l'ancien
        # d'abord le contrat lui-même : jamais « ok » avec un pont qui finit arrêté
        self.assertFalse(r.get("ok") and self.unit.active != "active",
                         "faux succès : pont arrêté après la sortie de l'ancien processus (%s)" % self.unit.active)
        self.assertEqual((self.unit.active, self.unit.pid), ("active", new_pid))
        self.assertEqual((code, r["ok"], r["result"], r["restarted"], r["registered"]),
                         (0, True, "already_paired", True, True), r)
        self.assertEqual(r["service"]["active"], "active")
        self.assertNotEqual(new_pid, self.first_pid)
        self.assertEqual(r["link"]["pid"], new_pid)                    # statut du NOUVEAU processus, jamais l'ancien
        self.assertFalse(self.unit.overlap)                            # jamais démarré pendant que l'ancien vivait
        self.assertEqual(json.loads(tmb.read(self.j(museadmin.musebridge.RUN_STATUS)))["state"], "registered")
        self.assertEqual(self.reg().authenticate("muse-device", self.token()), tmb.CONTROL)
        blob = json.dumps(r) + self.err.getvalue()
        for s in (tmb.SDK_TOKEN, tmb.ACCESS, tmb.REFRESH, self.token()):
            self.assertNotIn(s, blob)

    def test_delayed_old_exit_after_identity_change_ends_with_the_new_bridge_started(self):
        """revoke-control (identité réduite) puis réautorisation : l'ancien pont a vu la pause mais ferme lentement
        sa session (arrêt en 4 s) et sortirait de lui-même avec 78 à t+8 s. L'identité n'est réémise qu'une fois
        l'ancien processus disparu ; un seul `muse pair` finit avec le nouveau pont actif et enregistré."""
        old = self.token()
        self.admin().revoke_control()
        self.unit.doom(after=8.0, refused=False)       # refus en vol : son statut dit encore « registered »
        self.unit.stop_delay = 4.0
        self.admin().authorize_control(yes=True)
        seen = self.watch_identity_changes()
        n = len(self.calls)
        r, code = self.pair(stdin="")
        self.assert_new_bridge_survives(r, code)
        self.assertTrue(r["identity_reissued"])
        self.assertIsNone(self.reg().authenticate("muse-device", old))
        self.assertTrue(seen)
        for what, active, pid in seen:                 # identité changée seulement pont arrêté (fin attendue)
            self.assertIn(active, ("inactive", "failed"), what)
            self.assertIsNone(pid, what)
        c = self.cmds_since(n)
        order = [c.index(x + self.U) for x in ("stop --no-block ", "reset-failed ", "enable ", "start --no-block ")]
        self.assertEqual(order, sorted(order), c)
        self.assertNotIn("try-restart " + self.U, c)
        self.assertNotIn("enable --now " + self.U, c)

    def test_paused_process_with_an_already_exact_identity_is_replaced_not_trusted(self):
        """Course d'origine : plafond seul abaissé puis rétabli (identité restée exacte, « owned ») alors que l'ancien
        processus a déjà vu `bridge_paused` et sortira avec 78 dans 3 s. L'ancien code ne faisait qu'`enable --now`
        (sans effet sur une unité active) et rendait un succès ; le pont restait ensuite arrêté."""
        self.reg().set_policy({"muse": ["display", "feedback", "read"], "hermit": [], "hermes": []})
        self.unit.doom(after=3.0, refused=True)
        self.reg().set_policy({"muse": list(tmb.CONTROL), "hermit": [], "hermes": []})
        old = self.token()
        self.assertEqual(self.admin().identity_state(self.pwmap["opentv-muse"])["state"], "owned")
        r, code = self.pair(stdin="")
        self.assert_new_bridge_survives(r, code)
        self.assertFalse(r["identity_reissued"])
        self.assertEqual(self.token(), old)

    def test_old_exit_during_the_stop_wait_and_already_failed_units_are_restarted(self):
        """L'ancien sort de lui-même (78 -> failed) PENDANT l'attente d'arrêt, ou était déjà « failed » : reset-failed
        puis démarrage ; le résultat relu est le nouveau pont, jamais l'état « failed » de l'ancien."""
        self.admin().revoke_control()
        self.unit.doom(after=1.0)
        self.unit.stop_delay = 30.0
        self.admin().authorize_control(yes=True)
        r, code = self.pair(stdin="")
        self.assert_new_bridge_survives(r, code)
        self.first_pid = self.unit.pid
        self.unit.doom(after=0.0)                       # pont arrêté de lui-même (exit 78) avant `muse pair`
        self.unit.tick()
        self.assertEqual(self.unit.active, "failed")
        n = len(self.calls)
        r, code = self.pair(stdin="")
        self.assert_new_bridge_survives(r, code)
        self.assertNotIn("stop --no-block " + self.U, self.cmds_since(n))   # déjà arrêté : rien à arrêter

    def test_repeated_pair_is_idempotent_and_never_interrupts_a_healthy_bridge(self):
        pid, tok = self.unit.pid, self.token()
        for _ in range(3):
            n = len(self.calls)
            r, code = self.pair(stdin="")
            self.assertEqual((code, r["result"], r["restarted"], r["identity_reissued"], r["registered"]),
                             (0, "already_paired", False, False, True), r)
            self.assertEqual(self.cmds_since(n), [])
            self.assertEqual((self.unit.pid, self.unit.active, self.token()), (pid, "active", tok))
        self.unit.enabled = False                       # en marche mais plus activé au démarrage : enable sans --now
        n = len(self.calls)
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["restarted"], r["service"]["enabled"]), (0, False, "enabled"), r)
        self.assertEqual(self.cmds_since(n), ["enable " + self.U])
        self.assertEqual(self.unit.pid, pid)
        self.assertEqual(self.unit.spawned, [self.first_pid])          # un seul processus depuis l'appariement

    def test_policy_change_during_healthy_check_is_re_read_before_success(self):
        """La révocation/réautorisation peut tomber pendant les deux lectures de santé. L'instantané d'identité pris
        avant cette attente devient périmé : `pair` doit relire plafond + identité, arrêter l'ancien pont et réémettre,
        jamais rendre `already_paired` pour un processus qui sortira ensuite avec 78."""
        old = self.token()
        original = museadmin.Admin.healthy_bridge
        raced = []

        def healthy_then_change(adm):
            answer = original(adm)
            if not raced:
                raced.append(True)
                adm.revoke_control()                 # réduit l'identité liée dans la même écriture que la politique
                adm.authorize_control(yes=True)      # rétablit le plafond, mais ne réémet volontairement pas l'identité
                self.unit.doom(after=8.0, refused=False)
            return answer

        with mock.patch.object(museadmin.Admin, "healthy_bridge", healthy_then_change):
            r, code = self.pair(stdin="")
        self.assertTrue(raced)
        self.assert_new_bridge_survives(r, code)
        self.assertTrue(r["identity_reissued"])
        self.assertIsNone(self.reg().authenticate("muse-device", old))

    def test_foreign_identity_appearing_during_healthy_check_is_never_replaced_implicitly(self):
        """La deuxième lecture peut découvrir une identité qu'un autre administrateur vient d'émettre. La barrière
        `--replace-identity` s'applique aussi à cet instantané tardif : rien n'est révoqué, arrêté ou réémis."""
        old = self.token()
        original = museadmin.Admin.healthy_bridge
        foreign = []

        def healthy_then_foreign(adm):
            answer = original(adm)
            if not foreign:
                self.reg().revoke("muse-device")
                foreign.append(self.reg().issue("muse-device", ["read"]))
            return answer

        n = len(self.calls)
        with mock.patch.object(museadmin.Admin, "healthy_bridge", healthy_then_foreign):
            r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["error"], r["reason"]),
                         (2, False, "muse_device_identity_unexpected", "not_issued_by_muse_pair"), r)
        self.assertIn("--replace-identity", r["note"])
        self.assertEqual(self.reg().authenticate("muse-device", foreign[0]), ["read"])
        self.assertIsNone(self.reg().authenticate("muse-device", old))
        self.assertEqual(self.cmds_since(n), [])
        self.assertEqual((self.unit.active, self.unit.pid), ("active", self.first_pid))

    def test_bridge_that_never_stops_changes_nothing_and_says_so(self):
        self.admin().revoke_control()
        self.unit.hang_stop = True
        self.admin().authorize_control(yes=True)
        old = self.token()
        t0, n = self._t, len(self.calls)
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["error"]), (1, False, "bridge_stop_timeout"), r)
        self.assertNotIn("result", r)
        self.assertLessEqual(self._t - t0, museadmin.STOP_WAIT_S + museadmin.HEALTHY_SETTLE_S + 2)   # borné
        self.assertEqual(self.token(), old)             # identité réduite intacte : rien changé, relançable
        self.assertEqual(self.reg().authenticate("muse-device", old), ["display", "feedback", "read"])
        c = self.cmds_since(n)
        self.assertNotIn("start --no-block " + self.U, c)
        self.assertNotIn("enable " + self.U, c)
        self.unit.hang_stop = False                     # l'ancien finit par s'arrêter : relancer suffit
        self.unit.gone(0)
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["identity_reissued"], r["registered"]), (0, True, True, True), r)

    def test_start_failure_is_reported_and_state_kept_for_a_plain_retry(self):
        self.unit.gone(78)
        for crash, err in ((1, "bridge_start_failed"), (78, "bridge_refused")):
            self.unit.crash_next = crash
            r, code = self.pair(stdin="")
            self.assertEqual((code, r["ok"], r["error"], r["paired"]), (1, False, err, True), r)
            self.assertNotIn("result", r)
            self.assertEqual(r["service"]["active"], "failed")
            self.assertIn("relancer sudo opentv-pi muse pair", r["note"])
            self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
            self.assertEqual(self.admin().identity_state(self.pwmap["opentv-muse"])["state"], "owned")
        n = len(self.calls)
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["registered"], r["service"]["active"]), (0, True, True, "active"), r)
        self.assertNotIn("start --no-block " + museadmin.PAIR_UNIT, self.cmds_since(n))   # aucun Bluetooth

    def test_first_pairing_with_a_bridge_that_cannot_start_is_not_a_success(self):
        self.admin().unpair()
        self.unit.crash_next = 1
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["error"], r["paired"], r["service_started"]),
                         (1, False, "bridge_start_failed", True, False), r)
        self.assertNotIn("result", r)
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))   # appariement conservé
        self.assertEqual(self.reg().authenticate("muse-device", self.token()), tmb.CONTROL)
        n = len(self.calls)
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["result"], r["registered"]), (0, "already_paired", True), r)
        self.assertNotIn("start --no-block " + museadmin.PAIR_UNIT, self.cmds_since(n))

    def test_bridge_refused_after_start_is_never_reported_as_registered(self):
        """Le nouveau processus refuse (plafond abaissé entre-temps) : statut « refused » du NOUVEAU pid ou sortie 78
        -> échec explicite, jamais le « registered » d'un autre processus."""
        self.unit.gone(78)
        orig = self.unit.spawn

        def spawn():
            orig()
            self.unit.write_status("refused", reason="bridge_paused")
            self.unit.exit_at = self._t + 0.5
        self.unit.spawn = spawn
        r, code = self.pair(stdin="")
        self.assertEqual((code, r["ok"], r["error"]), (1, False, "bridge_refused"), r)
        self.assertNotEqual((r.get("link") or {}).get("state"), "registered")


class TestLinkedIdentityIsMuseOnScreen(unittest.TestCase):
    """« muse-device » (pont Muse) est l'identité authentifiée ; tout ce que voit Alex dit « Muse »."""

    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)
        self.a.registry.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": [], "hermes": []})

    def test_canonical_agent(self):
        self.assertEqual(agents.canonical_agent("muse-device"), "muse")
        for x in ("muse", "hermit", "hermes", "other", None):
            self.assertEqual(agents.canonical_agent(x), x)

    def test_actions_and_message_carry_the_owner_name(self):
        logs = []
        self.a.log = lambda ev, **kw: logs.append((ev, kw))
        self.a.agent_control("muse-device", "present_title",
                             tools.validate_args("present_title", {"id": "films/106", "text": "Très bien noté."}))
        self.a.agent_control("muse-device", "remote_control", tools.validate_args("remote_control", {"key": "down"}))
        self.a.show_message("muse-device", "Bonne soirée", 15)
        st = self.a.tv_state()
        self.assertEqual({x["from"] for x in st["actions"]["pending"]}, {"muse"})
        self.assertEqual(st["message"]["from"], "muse")
        self.assertNotIn("muse-device", json.dumps(st))
        # journal du service : identité authentifiée conservée (jamais réécrite en « muse »)
        self.assertIn(("agent_message", {"agent": "muse-device", "length": 12}), logs)
        self.assertEqual({kw["agent"] for ev, kw in logs if ev == "agent_control"}, {"muse-device"})

    def test_tv_page_never_prints_a_raw_agent_id(self):
        with open(os.path.join(PI4, "ui", "tv.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn('"Proposé par " + agentName(prop.from)', js)
        self.assertIn('"Message de " + agentName(s.message.from)', js)
        self.assertNotIn("|| s.message.from", js)
        self.assertNotIn('"muse-device"', js)   # canonicalisé par le service, pas masqué par la page


class TestLinkedIdentitySharesRates(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="otvlink", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        self.reg = agents.Registry(os.path.join(d, "agents.json"))
        self.reg.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": appmod.AGENT_LEVELS["control"],
                             "hermes": []})
        self.tok = {a: self.reg.issue(a, appmod.AGENT_LEVELS["control"]) for a in ("muse", "muse-device", "hermit")}
        self.calls = []
        calls = self.calls

        class Ctx:
            foryou = None

            def agent_control(self, agent, name, a):
                calls.append((agent, name))
                return {"accepted": True}

            def show_message(self, agent, text, seconds):
                calls.append((agent, "display_message"))
                return {"shown": True}

            def agent_status(self, agent, scopes):
                return {"agent": agent}
        self.clock = Clock()
        self.audit_path = os.path.join(d, "audit.jsonl")
        self.s = agentport.AgentService(Ctx(), self.reg, agents.Audit(self.audit_path), clock=self.clock)

    def call(self, agent, tool, args=None, uid=1000):
        return self.s.handle({"v": 1, "agent": agent, "token": self.tok[agent], "op": "call", "tool": tool,
                              "args": args or {}}, peer_uid=uid)

    def test_proposal_display_and_key_budgets_are_shared_with_the_parent(self):
        self.assertTrue(self.call("muse", "present_title", {"id": "films/101"})["ok"])
        self.assertEqual(self.call("muse-device", "present_title", {"id": "films/102"})["error"], "rate_limited")
        self.assertTrue(self.call("hermit", "present_title", {"id": "films/102"})["ok"])   # autre agent : son budget
        self.clock.t += 3.01
        self.assertTrue(self.call("muse-device", "present_title", {"id": "films/102"})["ok"])
        self.assertEqual(self.call("muse", "present_title", {"id": "films/103"})["error"], "rate_limited")
        self.assertTrue(self.call("muse-device", "display_message", {"text": "Salut"})["ok"])
        self.assertEqual(self.call("muse", "display_message", {"text": "Salut"})["error"], "rate_limited")
        self.clock.t += 10
        oks = [self.call(("muse", "muse-device")[i % 2], "remote_control", {"key": "down"}, uid=1000 + i)["ok"]
               for i in range(5)]
        self.assertEqual(oks, [True, True, True, False, False])                  # 3 / s pour Muse, toutes identités
        self.clock.t += 10
        self.assertTrue(self.call("muse", "play_title", {"id": "films/101"})["ok"])
        self.assertEqual(self.call("muse-device", "play_title", {"id": "films/101"})["error"], "rate_limited")
        with open(self.audit_path, encoding="utf-8") as f:
            audited = [json.loads(x)["agent"] for x in f if x.strip()]
        self.assertIn("muse-device", audited)                                     # audit : identité authentifiée
        self.assertIn("muse", audited)
        self.assertEqual([a for a, n in self.calls if n == "present_title"], ["muse", "hermit", "muse-device"])

    def test_per_second_budget_is_shared_too(self):
        res = [self.call(("muse", "muse-device")[i % 2], "status")["ok"] for i in range(agentport.CALLS_PER_S + 2)]
        self.assertEqual(res.count(True), agentport.CALLS_PER_S)


# ------------------------------------------------------------------------------------------- revue : moindre privilège
class TestStatusLeastPrivilege(unittest.TestCase):
    def test_no_playback_state_or_title_without_control(self):
        a = tp.TestAppIntegration.app(self)
        ok, err = a.playback.start("films/106", a._fy_lookup("films/106")[0])
        self.assertTrue(ok, err)
        self.assertEqual(tp.wait_state(a.playback, ("playing",))["state"], "playing")
        for scopes in ([], ["read"], ["feedback", "read"], ["display", "feedback", "read"]):
            st = a.agent_status("hermit", scopes)
            self.assertEqual(st["control"], {"enabled": False}, scopes)
            blob = json.dumps(st)
            for leak in ("films/106", "Les Étoiles", "playing", "loading", "\"actions\""):
                self.assertNotIn(leak, blob, (scopes, leak))
        st = a.agent_status("muse", appmod.AGENT_LEVELS["control"])["control"]
        self.assertEqual((st["enabled"], st["playback"]["state"], st["playback"]["id"]), (True, "playing", "films/106"))
        self.assertIn("actions", st)
        # même chose par l'outil status complet (tools.dispatch) d'un agent sans pilotage
        res = tools.dispatch(a, "hermit", ["display", "feedback", "read"], "status", {})
        self.assertEqual(res["control"], {"enabled": False})


# ------------------------------------------------------------------------------------------- revue : course au retour
class TestPlayTitleWaitsForTheReturn(unittest.TestCase):
    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)
        self.c = self.client

    def play(self, uid):
        return self.a.agent_control("muse", "play_title", tools.validate_args("play_title", {"id": uid}))

    def opened(self):
        end = time.monotonic() + 5   # ouverture envoyée par le fil de lecture : attendue avant de compter
        while time.monotonic() < end and not any(r["op"] == "open" for r in self.c.reqs):
            time.sleep(0.01)

    def refused(self, uid, code):
        self.opened()
        n = len([r for r in self.c.reqs if r["op"] == "open"])
        with self.assertRaises(tools.ToolError) as e:
            self.play(uid)
        self.assertEqual(str(e.exception), code)
        self.assertEqual(len([r for r in self.c.reqs if r["op"] == "open"]), n)   # aucune ouverture

    def test_closed_then_ack_sequence(self):
        """loading -> playing -> closing -> closed (non acquitté : la page revient sur la fiche) ⇒ refus
        « player_returning » ; acquittement de la page -> idle ⇒ nouveau lancement accepté."""
        self.assertEqual(self.play("films/106")["playback"]["state"], "loading")
        self.refused("films/101", "player_busy")                                  # loading
        tp.wait_state(self.a.playback, ("playing",))
        self.refused("films/101", "player_busy")                                  # playing
        self.c.reveal_after = 10 ** 6
        self.a.agent_control("muse", "close_player", {})
        v = tp.wait_state(self.a.playback, ("closed",))
        self.assertEqual(v["state"], "closed")
        self.refused("films/101", "player_returning")                             # closed, pas encore acquitté
        self.refused("films/106", "player_returning")
        self.assertFalse(self.a.playback.ack(v["seq"] - 1))                       # mauvais seq : toujours refusé
        self.refused("films/101", "player_returning")
        self.assertTrue(self.a.playback.ack(v["seq"]))
        self.assertEqual(self.a.playback.view()["state"], "idle")
        self.c.reveal_after, self.c.polls = 1, 0
        r = self.play("films/101")
        self.assertIn(r["playback"]["state"], ("loading", "playing"))   # accepté (le faux contrôleur révèle vite)
        self.assertEqual(r["playback"]["id"], "films/101")

    def test_unavailable_then_ack_sequence(self):
        self.c.outcome = "no-player"
        self.play("films/106")
        v = tp.wait_state(self.a.playback, ("unavailable",))
        self.assertEqual(v["state"], "unavailable")
        self.refused("films/101", "player_returning")
        self.assertTrue(self.a.playback.ack(v["seq"]))
        self.c.outcome = "revealed"
        self.assertIn(self.play("films/101")["playback"]["state"], ("loading", "playing"))

    def test_human_watch_button_keeps_its_contract(self):
        """« Regarder » d'Alex (page visible) : inchangé — un OK sur une fin non acquittée relance toujours."""
        self.c.outcome = "no-player"
        self.a.playback.start("films/106", self.a._fy_lookup("films/106")[0])
        tp.wait_state(self.a.playback, ("unavailable",))
        self.assertEqual(self.a.playback.start("films/106", self.a._fy_lookup("films/106")[0], require_idle=True),
                         (False, "returning"))
        self.c.outcome = "revealed"
        self.assertEqual(self.a.playback.start("films/106", self.a._fy_lookup("films/106")[0]), (True, None))

    def test_error_is_explained(self):
        from opentv_pi import mcp
        self.assertIn("player_returning", mcp.ERRORS_FR)


# ------------------------------------------------------------------------------------------- revue : texte libre
class TestFreeTextHardening(unittest.TestCase):
    ADVERSARIAL = [
        "Regarde \u202eevil.com\u202c", "a\u202bb", "a\u202db", "x\u2066y\u2069", "x\u2067y\u2069", "x\u2068y\u2069",
        "Muse\u200f dit", "Muse\u200e dit", "x\u061cy", "ligne\u2028deux", "para\u2029deux", "x\u200by", "x\u200cy",
        "x\u200dy", "x\u2060y", "\ufeffBonjour", "x\u00ady", "x\u180ey", "x\U000e0041y", "x\ue000y", "x\u0378y",
        "Va sur evil.com maintenant", "voir evil\uff0ecom", "voir evil\u3002com", "voir evil\uff61com",
        "voir evil\ufe52com", "voir evil\u2024com", "\uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d",
        "https\uff1a\uff0f\uff0fevil", "javascript\uff1aalert(1)", "\uff57\uff57\uff57\uff0ex",
        "exampl\u0435.com", "\u0441\u0430\u0439\u0442.\u0440\u0444", "e\u0301vil.com", "x.com ce soir",
        "t.co/abc", "https\u2236\u2215\u2215evil", "192.168.2.14", "\uff11\uff19\uff12.168.2.14",
        "\u05e9\u05dc\u05d5\u05dd", "\u0645\u0631\u062d\u0628\u0627", "\u0660\u0661",
        "x\u0301\u0302\u0303\u0304y", "un\u3000deux", "un\u2003deux", "tab\tici", "x\x85y",
    ]
    FRENCH = [
        "Amélie, le fabuleux destin : une valeur sûre.", "« L’Été meurtrier » est noté 7,2 sur IMDb.",
        "Ça : Chapitre 2 ? Frissons garantis !", "Astérix & Obélix : Mission Cléopâtre, IMDb 7.0.",
        "E.T. l’extra-terrestre, pour toute la famille.", "O.S.S. 117 : Le Caire, nid d’espions",
        "Mr. Nobody, très bien noté.", "WALL·E ce soir ?", "Les 400 coups (1959)", "Cœur de pirate, 20 h 30.",
        "S.O.S. Fantômes", "Œdipe roi – Pasolini", "Prêt\u202f? C’est parti\u00a0!", "Noté 8.1/10 sur TMDb.",
        "Le Seigneur des anneaux : Le Retour du roi ❤️", "Ponyo sur la falaise 🌊", "Ma vie de Courgette",
        "Les Misérables — Ladj Ly", "Intouchables, 2011 ; 8,5/10",
    ]

    def test_adversarial_text_is_refused_everywhere(self):
        for t in self.ADVERSARIAL:
            with self.assertRaises(ValueError, msg=repr(t)):
                validate.message_text(t)
            for tool, args in (("display_message", {"text": t}), ("present_title", {"id": "films/106", "text": t})):
                with self.assertRaises(ValueError, msg=(tool, repr(t))):
                    tools.validate_args(tool, args)

    def test_legitimate_french_is_kept_verbatim(self):
        import unicodedata
        for t in self.FRENCH:
            self.assertEqual(validate.message_text(t), unicodedata.normalize("NFC", t))
            self.assertEqual(tools.validate_args("present_title", {"id": "films/106", "text": t})["text"],
                             unicodedata.normalize("NFC", t))

    def test_tv_inserts_agent_text_as_text_only(self):
        with open(os.path.join(PI4, "ui", "tv.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertIn('$("pmeta").textContent = text', js)
        self.assertIn('b.textContent = "Message de "', js)


# ------------------------------------------------------------------------------------------- revue : descriptions Muse
class TestMuseCommandDescriptions(unittest.TestCase):
    def test_descriptions_match_the_schema(self):
        from opentv_pi import musebridge
        specs = musebridge.command_specs()
        self.assertEqual(len(specs), 11)
        for cmd, spec in specs.items():
            t = tools.TOOL_BY_NAME[musebridge.COMMANDS[cmd]]
            for k, p in list(spec["required"].items()) + list(spec["optional"].items()):
                d, sch = p["description"], t["inputSchema"]["properties"][k]
                self.assertNotIn("None", d, (cmd, k))
                if sch["type"] == "string":
                    self.assertNotIn("0 à 200", d, (cmd, k))
                self.assertEqual("défaut" in d, "default" in sch, (cmd, k, d))
                if "pattern" in sch:
                    self.assertIn("section/numéro", d, (cmd, k))
                    self.assertNotIn("caractères", d, (cmd, k))
                if "enum" in sch:
                    self.assertIn(", ".join(sch["enum"]), d)
                    self.assertNotIn("caractères", d, (cmd, k))
                if sch["type"] == "integer":
                    self.assertIn("entier de %d à %d" % (sch["minimum"], sch["maximum"]), d)


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
        # chemin réel du pont Muse : Agent Port authentifié avec l'identité LIÉE « muse-device » (plafond = « muse »)
        a.registry.set_policy({"muse": appmod.AGENT_LEVELS["control"], "hermit": [], "hermes": []})
        dev_tok = a.registry.issue("muse-device", appmod.AGENT_LEVELS["control"])
        svc = agentport.AgentService(a, a.registry, a.audit)
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
                            r = svc.handle({"v": 1, "agent": "muse-device", "token": dev_tok, "op": "call",
                                            "tool": cmd["tool"], "args": cmd.get("args") or {}}, peer_uid=os.getuid())
                            out = {"ok": r["ok"], "result": r.get("result"), "error": r.get("error")}
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
        # identité liée « muse-device » : libellé du PROPRIÉTAIRE, exactement « Proposé par Muse »
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
        # 3b. priorité d'Alex aussi pour les fiches : sa fiche reste, la proposition est refusée (human_active)
        self.assertEqual(res["applied"]["present_human_first"], ["failed", "human_active"])
        ph = st["present_after_human"]
        self.assertEqual((ph["panel"], ph["ptitle"], ph["focus"]), (True, "Les Étoiles", "Les Étoiles"))
        self.assertEqual([b["focused"] for b in ph["btns"]][:2], [False, True])   # le → d'Alex est gardé
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
        # 6. message de l'identité liée : exactement « Message de Muse » (jamais l'identifiant brut ni « un assistant »)
        self.assertEqual(st["message"]["banner"], "Message de Muse : Bonne soirée, Alex !")
        for x in res["steps"]:
            if x["kicker"].startswith("Proposé par"):
                self.assertEqual(x["kicker"], "Proposé par Muse", x["s"])
        # l'audit garde l'identité réellement authentifiée
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            audit = [json.loads(line) for line in f if line.strip()]
        self.assertTrue(audit)
        self.assertEqual({x["agent"] for x in audit}, {"muse-device"})
        # jamais d'iframe, de navigation hors de /tv ni d'adresse dans le DOM
        for x in res["steps"]:
            self.assertEqual((x["iframes"], x["url"]), (0, "/tv"))
        self.assertEqual(len([r for r in c.reqs if r["op"] == "open"]), 1)
