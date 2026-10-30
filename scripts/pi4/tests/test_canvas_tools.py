"""OpenTV Canvas (tâche 4) : permission facultative « canvas » par agent et outils MCP de proposition.

Couvre : permission absente par défaut et jamais un niveau emboîté (MUSE_SCOPES, LINKED_REQUIRED, ordre des niveaux et
les 11 outils / 11 commandes Muse inchangés), outils dynamiques par identité authentifiée (liste ET appel, isolation
des sessions, révocation appel par appel), arguments plats énumérés seulement (aucune scène brute, URL, CSS ni
balisage), aucune confirmation par un agent, retrait de SON aperçu seulement, débits 1/5 s et 20/10 min par agent
canonique, délai de grâce après un refus du propriétaire ou un geste humain, audit borné sans double « proposed »,
« Gérer les assistants » (case séparée « Changer l'ambiance OpenTV », lignes inchangées postées « keep »), et la
préservation de la permission par la reconfiguration QR et l'autorisation root du pilotage de Muse.
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.parse
from unittest import mock

from tests._common import PI4
from tests.test_agent_port import rpc, tmpdir
from tests.test_service import make_app
from tests import test_agents_manage as tam
from opentv_pi import (agentport, agents, app as appmod, canvas as cv, canvas_tools, config, mcp, musebridge, tools,
                       validate, web)

CANVAS_NAMES = ["canvas_status", "canvas_preview", "canvas_cancel"]
ELEVEN = ["status", "recommend", "catalogue_list", "search", "feedback", "history", "display_message",
          "present_title", "play_title", "close_player", "remote_control"]
CONTROL = ["control", "display", "feedback", "read"]
KNOWN = appmod.KNOWN_AGENTS


def configured(tc):
    a = make_app(tc)
    config.save_device(a.cfg, {"name": "Salon"})
    a.reload_device()
    a.tick()
    return a


def audit(a):
    try:
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Port:
    """Agent Port RÉEL (socket Unix) devant l'App réelle, horloge de débit injectable."""

    def __init__(self, tc, a, clock=None):
        d = tmpdir(tc)
        self.a, self.clock = a, clock or Clock()
        self.svc = agentport.AgentService(a, a.registry, a.audit, clock=self.clock)
        self.sock = os.path.join(d, "agent.sock")
        self.srv = agentport.AgentPortServer(self.sock, self.svc.handle)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        tc.addCleanup(self.srv.server_close)
        tc.addCleanup(self.srv.shutdown)
        self.dir = d

    def raw(self, agent, tok, tool, args=None, op="call"):
        return agentport.call(self.sock, {"v": 1, "agent": agent, "token": tok, "op": op, "tool": tool,
                                          "args": args if args is not None else {}})

    def session(self, agent, tok):
        base = {"v": 1, "agent": agent, "token": tok}
        s = mcp.Session(lambda req: agentport.call(self.sock, dict(base, **req)))
        s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return s


def names(s):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return [t["name"] for t in r["result"]["tools"]] if "result" in r else r


def mcall(s, name, args=None):
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    return r.get("result", r)


def err(res):
    """Code stable d'une erreur d'outil MCP (« Erreur OpenTV : … (code) »)."""
    if not res.get("isError"):
        return None
    return re.search(r"\(([a-z_]+)\)$", res["content"][0]["text"]).group(1)


class Base(unittest.TestCase):
    def setup_agents(self, policy, issue):
        a = configured(self)
        a.registry.set_policy(policy)
        toks = {k: a.registry.issue(k, v) for k, v in issue.items()}
        return a, toks


# --------------------------------------------------------------------------- modèle de permission
class TestScopeModel(unittest.TestCase):
    def test_canvas_is_optional_never_a_nested_level(self):
        self.assertIn("canvas", validate.SCOPES)
        self.assertEqual(validate.OPTIONAL_SCOPES, ("canvas",))
        self.assertEqual(appmod.AGENT_LEVELS, {"none": [], "read": ["read"], "feedback": ["feedback", "read"],
                                               "full": ["display", "feedback", "read"], "control": CONTROL})
        for lvl in appmod.AGENT_LEVELS.values():
            self.assertNotIn("canvas", lvl)
        self.assertEqual(appmod.level_of(["canvas"]), "none")
        self.assertEqual(appmod.level_of(["canvas", "read"]), "read")
        self.assertEqual(appmod.level_of(["canvas"] + CONTROL), "control")
        self.assertEqual(musebridge.MUSE_SCOPES, CONTROL)
        self.assertEqual(agents.LINKED_REQUIRED, {"muse-device": tuple(CONTROL)})
        self.assertNotIn("canvas", dict(web.LEVEL_LABELS))
        self.assertNotIn("canvas", web.LEVEL_SHORT)
        self.assertEqual(agents.level_scopes(["canvas", "read"]), ["read"])
        self.assertEqual(agents.with_optional(CONTROL, ["canvas", "read"]), ["canvas"] + CONTROL)
        self.assertEqual(agents.with_optional(CONTROL, ["read"]), CONTROL)

    def test_existing_tool_and_muse_command_surfaces_are_unchanged(self):
        self.assertEqual([t["name"] for t in tools.TOOLS], ELEVEN)
        self.assertTrue(all(t["scope"] != "canvas" for t in tools.TOOLS))
        self.assertEqual(len(musebridge.COMMANDS), 11)
        self.assertEqual(sorted(musebridge.COMMANDS.values()), sorted(ELEVEN))
        self.assertEqual(sorted(musebridge.command_specs()), sorted(musebridge.COMMANDS))
        self.assertFalse(set(CANVAS_NAMES) & set(musebridge.COMMANDS.values()))
        self.assertFalse(set(CANVAS_NAMES) & set(tools.TOOL_BY_NAME))
        for scopes in (CONTROL, CONTROL + ["canvas"]):   # catalogue fixe : la permission n'y ajoute rien
            self.assertEqual([t["name"] for t in tools.public_tools(scopes)], ELEVEN)

    def test_canvas_tool_schemas_are_closed_flat_enums(self):
        self.assertEqual([t["name"] for t in canvas_tools.TOOLS], CANVAS_NAMES)
        for t in canvas_tools.TOOLS:
            self.assertEqual(t["scope"], "canvas")
            sch = t["inputSchema"]
            self.assertFalse(sch["additionalProperties"])
            for k, p in sch["properties"].items():
                self.assertEqual(p["type"], "string", (t["name"], k))
                self.assertTrue("enum" in p or "pattern" in p, (t["name"], k))
            blob = json.dumps(t).lower()
            self.assertIsNone(re.search(r"\b(confirm|publish|apply|rollback|reset|row_order|scene|url|css)\b", blob),
                              t["name"])
        props = canvas_tools.TOOL_BY_NAME["canvas_preview"]["inputSchema"]["properties"]
        self.assertEqual(set(props), {"preset", "wallpaper", "motion", "accent", "shade", "hero_focus"})
        self.assertEqual(props["preset"]["enum"], sorted(cv.PRESETS))
        for f in cv.ENUMS:
            self.assertEqual(props[f]["enum"], list(cv.ENUMS[f]))
        self.assertEqual(agentport.TOOL_RATES["canvas_preview"], ((1, 5.0), (20, 600.0)))
        for n in ("canvas_status", "canvas_cancel"):
            self.assertIn(n, agentport.TOOL_RATES)

    def test_dispatch_refuses_without_scope_even_when_called_directly(self):
        class Ctx:
            canvas_cooldown = canvas_tools.Cooldown(lambda: 0.0)

            def canvas_agent_preview(self, agent, changes):
                raise AssertionError("jamais atteint sans la permission")
        for name, args in (("canvas_preview", {"preset": "mist"}), ("canvas_status", {}),
                           ("canvas_cancel", {"preview_id": "0" * 32})):
            with self.assertRaises(tools.ToolError) as e:
                canvas_tools.dispatch(Ctx(), "hermit", ["read", "control"], name, args)
            self.assertEqual(str(e.exception), "forbidden_scope")
        self.assertEqual(canvas_tools.public_tools(CONTROL), [])

    def test_hostile_or_raw_arguments_are_refused(self):
        bad = [("canvas_preview", {}), ("canvas_preview", None), ("canvas_preview", []),
               ("canvas_preview", {"preset": "https://evil.example/x"}), ("canvas_preview", {"preset": "MIST"}),
               ("canvas_preview", {"wallpaper": "url(javascript:alert(1))"}),
               ("canvas_preview", {"accent": "<script>alert(1)</script>"}),
               ("canvas_preview", {"shade": "deep; background:red"}), ("canvas_preview", {"motion": True}),
               ("canvas_preview", {"motion": 1}), ("canvas_preview", {"motion": ["calm"]}),
               ("canvas_preview", {"scene": {"wallpaper": "brume"}}),
               ("canvas_preview", {"row_order": ["tonight", "top", "new", "watchlist", "more"]}),
               ("canvas_preview", {"preset": "mist", "confirm": True}),
               ("canvas_preview", {"preset": "mist", "text": "Belle soirée"}),
               ("canvas_preview", {"hero_focus": "title\n"}), ("canvas_cancel", {}),
               ("canvas_cancel", {"preview_id": "../" + "0" * 29}), ("canvas_cancel", {"preview_id": "A" * 32}),
               ("canvas_cancel", {"preview_id": "0" * 33}), ("canvas_status", {"x": 1}), ("canvas_confirm", {})]
        for name, args in bad:
            with self.assertRaises(ValueError, msg=(name, args)):
                canvas_tools.validate_args(name, args)
        self.assertEqual(canvas_tools.validate_args("canvas_preview", {"preset": "mist", "motion": "off"}),
                         {"preset": "mist", "motion": "off"})


# --------------------------------------------------------------------------- défauts
class TestDefaults(Base):
    def test_first_setup_never_grants_canvas(self):
        a = make_app(self)
        a.net = {"mode": "ethernet", "ip": "192.168.2.40", "wifi_present": False, "nm_ok": True}
        tok = a.tv_state()["setup"]["token"]
        code, kind, _ = a.setup_submit({"t": tok, "name": "Salon", "net": "keep", "agent_muse": "control",
                                        "agent_hermit": "full", "agent_hermes": "read"})
        self.assertEqual((code, kind), (200, "done"))
        for row in a.registry.policy().values():
            self.assertNotIn("canvas", row)

    def test_identity_without_canvas_lists_exactly_its_level_tools(self):
        a, t = self.setup_agents({"muse": CONTROL, "hermit": ["read"], "hermes": []}, {"muse": CONTROL})
        p = Port(self, a)
        self.assertEqual(names(p.session("muse", t["muse"])), ELEVEN)


# --------------------------------------------------------------------------- outils dynamiques (vrai Agent Port)
class TestDynamicTools(Base):
    def env(self):
        a, t = self.setup_agents({"muse": [], "hermit": ["canvas", "read"], "hermes": ["canvas", "display",
                                                                                        "feedback", "read"]},
                                 {"hermit": ["canvas", "read"], "hermes": ["display", "feedback", "read"]})
        return a, t, Port(self, a)

    def test_tools_are_listed_per_identity_and_sessions_never_leak(self):
        a, t, p = self.env()
        sa, sb = p.session("hermit", t["hermit"]), p.session("hermes", t["hermes"])
        want_a = ["status", "recommend", "catalogue_list", "search", "history"] + CANVAS_NAMES
        want_b = ["status", "recommend", "catalogue_list", "search", "feedback", "history", "display_message"]
        for _ in range(2):   # alterné : la liste d'une identité ne fuit jamais vers l'autre
            self.assertEqual(names(sa), want_a)
            self.assertEqual(names(sb), want_b)
        only = a.registry.issue("hermit", ["canvas"])
        self.assertEqual(names(p.session("hermit", only)), CANVAS_NAMES)

    def test_scope_is_enforced_at_call_not_only_in_the_list(self):
        a, t, p = self.env()
        sb = p.session("hermes", t["hermes"])
        self.assertEqual(err(mcall(sb, "canvas_preview", {"preset": "mist"})), "forbidden_scope")
        self.assertEqual(p.raw("hermes", t["hermes"], "canvas_preview", {"preset": "mist"})["error"],
                         "forbidden_scope")
        self.assertEqual(p.raw("hermes", t["hermes"], "canvas_status")["error"], "forbidden_scope")
        self.assertIsNone(a.tv_state()["canvas"]["preview"])
        self.assertFalse([x for x in audit(a) if x["tool"] == "canvas"])
        recs = [x for x in audit(a) if x["tool"] == "canvas_preview"]
        self.assertEqual([(x["agent"], x["error"]) for x in recs], [("hermes", "forbidden_scope")] * 2)
        self.assertFalse([k for k in p.svc.tool_calls if k[1].startswith("canvas_")])   # refusé AVANT tout débit

    def test_preview_is_only_a_proposal_owner_must_confirm(self):
        a, t, p = self.env()
        s = p.session("hermit", t["hermit"])
        st = mcall(s, "canvas_status")
        self.assertFalse(st["isError"], st)
        sc = st["structuredContent"]
        self.assertEqual((sc["state"], sc["preset"], sc["preview"]), ("default", "canvas", None))
        self.assertEqual(sc["scene"], {f: cv.DEFAULT_SCENE.to_dict()[f] for f in cv.ENUMS})
        r = mcall(s, "canvas_preview", {"preset": "mist"})
        self.assertFalse(r["isError"], r)
        res = r["structuredContent"]
        self.assertEqual((res["proposed"], res["preset"], res["confirmation"]), (True, "mist", "owner_on_tv"))
        self.assertRegex(res["preview_id"], r"\A[0-9a-f]{32}\Z")
        self.assertNotIn("row_order", json.dumps(res))
        c = a.tv_state()["canvas"]
        self.assertEqual((c["state"], c["revision"], c["preview"]["proposed_by"]), ("default", 0, "hermit"))
        self.assertEqual((c["sheet"]["agent_name"], c["sheet"]["focus"]), ("Hermit", "no"))
        own = mcall(s, "canvas_status")["structuredContent"]["preview"]
        self.assertEqual((own["preview_id"], own["preset"]), (res["preview_id"], "mist"))
        # aucun outil de confirmation : ni via MCP, ni en direct, ni par argument détourné
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                   "params": {"name": "canvas_confirm", "arguments": {}}})["error"]["code"], -32602)
        for n in ("canvas_confirm", "canvas_apply", "canvas_rollback", "canvas_reset"):
            self.assertEqual(p.raw("hermit", t["hermit"], n, {"preview_id": res["preview_id"]})["error"],
                             "unknown_tool")
        self.assertEqual(err(mcall(s, "canvas_preview", {"preset": "cinema", "confirm": True})), "invalid_params")
        c = a.tv_state()["canvas"]
        self.assertEqual((c["state"], c["revision"], c["preview"]["id"]), ("default", 0, res["preview_id"]))
        self.assertEqual(a.tv_state()["actions"]["pending"], [])      # rien en file : jamais de touche ni de fiche
        self.assertFalse(a.tv_state()["assistants"]["manage"]["active"])
        self.assertEqual(a.playback.view()["state"], "idle")
        self.assertTrue(mcall(s, "canvas_cancel", {"preview_id": res["preview_id"]})["structuredContent"]["cancelled"])
        self.assertIsNone(a.tv_state()["canvas"]["preview"])

    def test_agent_cancels_only_its_own_preview_and_never_sees_another(self):
        a, t, p = self.env()
        t["hermes"] = a.registry.issue("hermes", ["canvas", "read"])
        r = p.raw("hermit", t["hermit"], "canvas_preview", {"preset": "cinema"})
        self.assertTrue(r["ok"], r)
        pid = r["result"]["preview_id"]
        self.assertEqual(p.raw("hermes", t["hermes"], "canvas_cancel", {"preview_id": pid})["error"],
                         "unknown_preview")
        st = p.raw("hermes", t["hermes"], "canvas_status")
        self.assertIsNone(st["result"]["preview"])
        self.assertNotIn(pid, json.dumps(st))
        self.assertEqual(p.raw("hermes", t["hermes"], "canvas_preview", {"preset": "mist"})["error"],
                         "preview_pending")
        self.assertEqual(a.tv_state()["canvas"]["preview"]["id"], pid)
        self.assertTrue(p.raw("hermit", t["hermit"], "canvas_cancel", {"preview_id": pid})["ok"])

    def test_revocation_applies_to_the_next_list_and_call_of_a_live_session(self):
        a, t, p = self.env()
        s = p.session("hermit", t["hermit"])
        self.assertIn("canvas_preview", names(s))
        _, fp, _ = a.registry.snapshot(KNOWN)
        a.registry.apply_policy_changes({"hermit": ["read"]}, KNOWN, fp)
        self.assertEqual(a.registry.list()["hermit"]["scopes"], ["read"])
        self.assertNotIn("canvas_preview", names(s))
        self.assertEqual(err(mcall(s, "canvas_preview", {"preset": "mist"})), "forbidden_scope")
        self.assertEqual(err(mcall(s, "canvas_status")), "forbidden_scope")
        a.registry.revoke("hermit")
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["error"]["code"], -32603)
        self.assertEqual(err(mcall(s, "canvas_status")), "unauthorized")
        self.assertIsNone(a.tv_state()["canvas"]["preview"])

    def test_hostile_arguments_never_reach_the_store_or_the_audit(self):
        a, t, p = self.env()
        s = p.session("hermit", t["hermit"])
        hostile = [{"preset": "https://evil.example/x"}, {"wallpaper": "url(evil.example/a.png)"},
                   {"accent": "<img src=x onerror=alert(1)>"}, {"scene": {"wallpaper": "brume"}},
                   {"row_order": ["tonight"]}, {"preset": "mist", "note": "evil.example"}, {}]
        for args in hostile:
            self.assertEqual(err(mcall(s, "canvas_preview", args)), "invalid_params", args)
            self.assertEqual(p.raw("hermit", t["hermit"], "canvas_preview", args)["error"], "invalid_params")
        self.assertIsNone(a.tv_state()["canvas"]["preview"])
        blob = json.dumps(audit(a))
        for bad in ("evil", "<img", "url(", "brume", a.cfg.state_dir):
            self.assertNotIn(bad, blob)

    def test_whoami_and_linked_bridge_are_unchanged_by_a_canvas_grant(self):
        a = configured(self)
        a.registry.set_policy({"muse": ["canvas"] + CONTROL, "hermit": [], "hermes": []})
        tok = a.registry.issue("muse-device", CONTROL)
        p = Port(self, a)
        self.assertEqual(p.raw("muse-device", tok, None, op="whoami")["result"],
                         {"agent": "muse-device", "scopes": CONTROL})
        self.assertEqual(names(p.session("muse-device", tok)), ELEVEN)
        self.assertEqual(p.raw("muse-device", tok, "canvas_status")["error"], "forbidden_scope")
        muse = next(x for x in a.assistants() if x["id"] == "muse")
        self.assertEqual((muse["bridge"]["state"], muse["level"], muse["canvas"]), ("ready", "control", True))
        self.assertEqual(a.tv_state()["assistants"]["muse_bridge"], "ready")
        # une identité liée qui porterait la permission sort de LINKED_REQUIRED : pause appel par appel (jamais un
        # pont qui exposerait des outils hors de ses 11 commandes)
        tok2 = a.registry.issue("muse-device", ["canvas"] + CONTROL)
        self.assertEqual(p.raw("muse-device", tok2, None, op="whoami")["error"], "bridge_paused")
        self.assertEqual(p.raw("muse-device", tok2, "canvas_status")["error"], "bridge_paused")

    def test_real_opentv_mcp_launcher_exposes_and_calls_canvas_tools(self):
        a, t, p = self.env()
        cfg = agents.write_client_config(os.path.join(p.dir, "agent-hermit.json"), "hermit", t["hermit"], p.sock)
        lines = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "canvas_preview", "arguments": {"preset": "cinema", "motion": "off"}}}])
        out = subprocess.run([sys.executable, "-I", os.path.join(PI4, "bin", "opentv-mcp"), "--config", cfg],
                             input=lines.encode(), capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = [json.loads(x) for x in out.stdout.decode().splitlines()]
        self.assertEqual([x["name"] for x in res[1]["result"]["tools"]][-3:], CANVAS_NAMES)
        self.assertFalse(res[2]["result"]["isError"], res[2])
        c = a.tv_state()["canvas"]["preview"]
        self.assertEqual((c["proposed_by"], c["scene"]["motion"], c["scene"]["wallpaper"]), ("hermit", "off", "affiche"))
        self.assertNotIn(t["hermit"], out.stdout.decode() + out.stderr.decode())


# --------------------------------------------------------------------------- débits et délai de grâce
class TestRates(Base):
    def env(self, linked=False):
        pol = {"muse": [], "hermit": ["canvas", "read"], "hermes": ["canvas"]}
        a, t = self.setup_agents(pol, {"hermit": ["canvas"], "hermes": ["canvas"]})
        clk = Clock()
        return a, t, Port(self, a, clk), clk

    def preview(self, p, agent, tok, preset="mist"):
        return p.raw(agent, tok, "canvas_preview", {"preset": preset})

    def cancel(self, p, agent, tok, r):
        self.assertTrue(r["ok"], r)
        self.assertTrue(p.raw(agent, tok, "canvas_cancel", {"preview_id": r["result"]["preview_id"]})["ok"])

    def proposed(self, a):
        return [x for x in audit(a) if x["tool"] == "canvas" and x["args"]["action"] == "proposed"]

    def test_preview_one_per_5s_and_twenty_per_10min_without_refund(self):
        a, t, p, clk = self.env()
        self.cancel(p, "hermit", t["hermit"], self.preview(p, "hermit", t["hermit"]))
        clk.t += 4.9
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "rate_limited")
        self.assertEqual(len(self.proposed(a)), 1)                      # refus avant l'App : jamais proposé
        self.assertTrue(self.preview(p, "hermes", t["hermes"])["ok"])  # autre agent : budget séparé
        a.canvas_tv_post("cancel", {"preview_id": a.tv_state()["canvas"]["preview"]["id"]})
        clk.t += 0.1
        for _ in range(19):
            self.cancel(p, "hermit", t["hermit"], self.preview(p, "hermit", t["hermit"]))
            clk.t += 5.0
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "rate_limited")   # 21e en 10 min
        clk.t = 1000.0 + 600.0 + 0.5
        self.cancel(p, "hermit", t["hermit"], self.preview(p, "hermit", t["hermit"]))
        clk.t += 5.0
        # refus sans effet (aucun changement) : la place reste prise (aucun remboursement)
        st = p.raw("hermit", t["hermit"], "canvas_status")["result"]
        self.assertEqual(st["preset"], "canvas")
        self.assertEqual(self.preview(p, "hermit", t["hermit"], "canvas")["error"], "no_change")
        clk.t += 1.0
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "rate_limited")

    def test_budget_and_cooldown_belong_to_the_canonical_agent(self):
        with mock.patch.dict(agents.LINKED_POLICY, {"hermit-box": "hermit"}):
            a, t, p, clk = self.env()
            box = a.registry.issue("hermit-box", ["canvas"])
            r = self.preview(p, "hermit-box", box)
            self.assertTrue(r["ok"], r)
            self.assertEqual(a.tv_state()["canvas"]["preview"]["proposed_by"], "hermit")
            self.cancel(p, "hermit-box", box, r)
            clk.t += 1.0
            self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "rate_limited")
            clk.t += 5.0
            r = self.preview(p, "hermit", t["hermit"])
            a.canvas_tv_post("cancel", {"preview_id": r["result"]["preview_id"]})   # « Non » du propriétaire
            clk.t += 6.0
            self.assertEqual(self.preview(p, "hermit-box", box)["error"], "canvas_cooldown")
            self.assertTrue(self.preview(p, "hermes", t["hermes"])["ok"])          # jamais un autre agent
            recs = [x for x in audit(a) if x["tool"] == "canvas_preview"]
            self.assertEqual([(x["agent"], x["error"]) for x in recs],
                             [("hermit", "rate_limited"), ("hermit-box", "canvas_cooldown")])
            # sens inverse : le refus d'un aperçu de l'identité liée vaut pour l'agent propriétaire
            a.canvas_tv_post("cancel", {"preview_id": a.tv_state()["canvas"]["preview"]["id"]})
            a.mono_clock.t += canvas_tools.REFUSAL_COOLDOWN_S + 1
            clk.t += 6.0
            r = self.preview(p, "hermit-box", box)
            self.assertTrue(r["ok"], r)
            a.canvas_tv_post("cancel", {"preview_id": r["result"]["preview_id"]})
            clk.t += 6.0
            self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "canvas_cooldown")

    def test_owner_rejection_and_human_key_start_a_cooldown_agent_cancel_does_not(self):
        a, t, p, clk = self.env()
        r = self.preview(p, "hermit", t["hermit"])
        code, body = a.canvas_tv_post("cancel", {"preview_id": r["result"]["preview_id"]})
        self.assertEqual((code, body["ok"]), (200, True))
        clk.t += 6.0
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "canvas_cooldown")
        a.mono_clock.t += canvas_tools.REFUSAL_COOLDOWN_S - 1
        clk.t += 6.0
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "canvas_cooldown")
        a.mono_clock.t += 1.5
        clk.t += 6.0
        r = self.preview(p, "hermit", t["hermit"])
        self.assertTrue(r["ok"], r)
        self.assertTrue(a.canvas_human("key"))                               # geste humain ordinaire
        clk.t += 6.0
        self.assertEqual(self.preview(p, "hermit", t["hermit"])["error"], "canvas_cooldown")
        st = p.raw("hermit", t["hermit"], "canvas_status")["result"]
        self.assertTrue(0 < st["cooldown_s"] <= canvas_tools.REFUSAL_COOLDOWN_S)
        # retrait par l'agent lui-même : aucun délai de grâce (seul le débit 1/5 s s'applique)
        a.mono_clock.t += 2.0                                                # fin de la fenêtre de silence humaine
        r = self.preview(p, "hermes", t["hermes"])
        self.cancel(p, "hermes", t["hermes"], r)
        clk.t += 5.0
        self.assertTrue(self.preview(p, "hermes", t["hermes"])["ok"])

    def test_status_and_cancel_are_bounded_but_owner_still_decides(self):
        a, t, p, clk = self.env()
        r = self.preview(p, "hermit", t["hermit"])
        pid = r["result"]["preview_id"]
        outs = [p.raw("hermit", t["hermit"], "canvas_status").get("error") for _ in range(4)]
        self.assertIn("rate_limited", outs)
        outs = [p.raw("hermit", t["hermit"], "canvas_cancel", {"preview_id": "0" * 32}).get("error")
                for _ in range(4)]
        self.assertIn("rate_limited", outs)
        code, body = a.canvas_tv_post("confirm", {"preview_id": pid, "confirm": True})
        self.assertEqual((code, body["ok"]), (200, True), body)              # le propriétaire passe toujours
        self.assertEqual(a.tv_state()["canvas"]["preset"], "mist")


# --------------------------------------------------------------------------- audit
class TestAudit(Base):
    def test_preview_is_audited_once_by_the_app_with_bounded_fields(self):
        a, t = self.setup_agents({"muse": [], "hermit": ["canvas"], "hermes": []}, {"hermit": ["canvas"]})
        p = Port(self, a)
        r = p.raw("hermit", t["hermit"], "canvas_preview", {"preset": "cinema", "accent": "jade"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(p.raw("hermit", t["hermit"], "canvas_status")["ok"])
        recs = audit(a)
        self.assertEqual([x for x in recs if x["tool"] == "canvas_preview"], [])   # jamais en double
        mine = [x for x in recs if x["tool"] == "canvas"]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["agent"], mine[0]["args"]["action"], mine[0]["args"]["preset"]),
                         ("hermit", "proposed", "custom"))
        self.assertEqual(len([x for x in recs if x["tool"] == "canvas_status"]), 1)
        blob = json.dumps(recs)
        for bad in (a.cfg.state_dir, "jade", "affiche", t["hermit"], r["result"]["preview_id"]):
            self.assertNotIn(bad, blob)
        # refus décidé par l'App (aperçu déjà en attente) : audité une seule fois, par l'App
        p.clock.t += 6.0
        self.assertEqual(p.raw("hermit", t["hermit"], "canvas_preview", {"preset": "mist"})["error"],
                         "preview_pending")
        recs = audit(a)
        self.assertEqual([x for x in recs if x["tool"] == "canvas_preview"], [])
        self.assertEqual([(x["ok"], x["error"]) for x in recs if x["tool"] == "canvas"],
                         [(True, None), (False, "preview_pending")])


# --------------------------------------------------------------------------- Muse : jamais altéré par la permission
def admin_case(tc):
    """Racine de test museadmin (tests.test_muse_bridge.AdminBase) utilisable depuis un autre module."""
    from tests import test_muse_bridge as tmb

    class T(tmb.AdminBase):
        def runTest(self):
            pass
    c = T()
    c.setUp()
    tc.addCleanup(c.doCleanups)
    return c


class TestMusePolicyKeepsCanvas(unittest.TestCase):
    def test_pair_stays_idempotent_when_the_parent_also_allows_canvas(self):
        c = admin_case(self)
        self.assertEqual(c.pair()[1], 0)
        reg = agents.Registry(c.cfg.registry_file)
        _, fp, _ = reg.snapshot(KNOWN)
        reg.apply_policy_changes({"muse": ["canvas"] + CONTROL}, KNOWN, fp)
        self.assertEqual(reg.list()["muse-device"]["scopes"], CONTROL)    # identité liée intacte
        c.calls.clear()
        r, code = c.pair(stdin="")
        self.assertEqual((r["result"], code), ("already_paired", 0), r)
        self.assertEqual(c.cmds(), [])                                   # pont sain : jamais interrompu


    def test_root_control_grant_and_revoke_preserve_the_optional_permission(self):
        from tests import test_muse_bridge as tmb

        class T(tmb.AdminBase):
            def runTest(self):
                pass
        tc = T()
        tc.setUp()
        self.addCleanup(tc.doCleanups)
        reg = agents.Registry(tc.cfg.registry_file)
        reg.set_policy({"muse": ["canvas", "feedback", "read"], "hermit": [], "hermes": []})
        tc.euid = 0
        r, code = tc.admin().authorize_control(yes=True)
        self.assertEqual((code, r["changed"]), (0, True), r)
        self.assertEqual(reg.policy()["muse"], ["canvas"] + CONTROL)
        self.assertEqual(r["policy_after"], ["canvas"] + CONTROL)
        self.assertFalse(tc.admin().authorize_control(yes=True)[0]["changed"])   # idempotent avec la permission
        r, code = tc.admin().revoke_control()
        self.assertEqual(code, 0, r)
        self.assertEqual(reg.policy()["muse"], ["canvas", "display", "feedback", "read"])

    def test_grant_rollback_settles_with_the_optional_permission(self):
        from opentv_pi import musehelper
        from tests import test_muse_bridge as tmb

        class T(tmb.AdminBase):
            def runTest(self):
                pass
        tc = T()
        tc.setUp()
        self.addCleanup(tc.doCleanups)
        reg = agents.Registry(tc.cfg.registry_file)
        reg.set_policy({"muse": ["canvas", "read"], "hermit": [], "hermes": []})
        tc.euid = 0
        tc.admin().authorize_control(yes=True)
        helper = musehelper.MuseHelper.__new__(musehelper.MuseHelper)
        helper.log = lambda *a, **k: None
        self.assertEqual(helper._settle_grant(tc.admin(), ["canvas", "read"]), "restored")
        self.assertEqual(reg.policy()["muse"], ["canvas", "read"])
        # le propriétaire a ajouté la permission pendant la séance (avant : sans) : décision conservée
        reg.set_policy({"muse": ["read"], "hermit": [], "hermes": []})
        tc.admin().authorize_control(yes=True)
        _, fp, _ = reg.snapshot(KNOWN)
        reg.apply_policy_changes({"muse": ["canvas"] + CONTROL}, KNOWN, fp)
        self.assertEqual(helper._settle_grant(tc.admin(), ["read"]), "kept")


# --------------------------------------------------------------------------- « Gérer les assistants »
class TestManageCanvas(unittest.TestCase):
    def env(self, policy, issue=None, force=False):
        a, s = tam.configured(self)
        a.registry.set_policy(policy)
        toks = {k: a.registry.issue(k, v, force=force) for k, v in (issue or {}).items()}
        tok, ph = tam.open_mode(self, a, s)
        return a, s, tok, ph, toks

    def page(self, ph, tok, **q):
        code, _, body = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(q, t=tok)))
        self.assertEqual(code, 200, body)
        return html.unescape(body)

    def save_form(self, body):
        i = body.index('id="save"')
        return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', body[i:body.index("</form>", i)]))

    def test_each_agent_has_a_separate_unchecked_canvas_box(self):
        a, s, tok, ph, _ = self.env({"muse": ["read"], "hermit": ["canvas", "read"], "hermes": []})
        body = self.page(ph, tok)
        boxes = re.findall(r'<label class="check"><input type="checkbox" name="canvas_(\w+)" value="on"( checked)?>'
                           r"\s*<span>Changer l'ambiance OpenTV", body)
        self.assertEqual(boxes, [("muse", ""), ("hermit", " checked"), ("hermes", "")])
        sel = re.findall(r'<select name="agent_(\w+)">', body)
        self.assertEqual(sel, ["muse", "hermit", "hermes"])               # niveaux emboîtés : listes séparées
        self.assertNotIn('value="canvas"', body)

    def test_unchanged_rows_post_keep_and_canvas_alone_changes_only_that_row(self):
        a, s, tok, ph, toks = self.env({"muse": ["read"], "hermit": ["read"], "hermes": []},
                                       {"hermit": ["read"]})
        body = self.page(ph, tok, agent_muse="read", agent_hermit="read", agent_hermes="none")
        self.assertIn("Aucun changement", body)
        self.assertNotIn('id="save"', body)
        body = self.page(ph, tok, agent_muse="read", agent_hermit="read", canvas_hermit="on", agent_hermes="none")
        self.assertIn("Changer l'ambiance OpenTV", body)
        form = self.save_form(body)
        self.assertEqual({k: v for k, v in form.items() if k.startswith(("agent_", "canvas_"))},
                         {"agent_muse": "keep", "canvas_muse": "keep", "agent_hermit": "keep", "canvas_hermit": "on",
                          "agent_hermes": "keep", "canvas_hermes": "keep"})
        code, _, out = ph.post(form)
        self.assertEqual(code, 200, out)
        self.assertEqual(a.registry.policy(), {"muse": ["read"], "hermit": ["canvas", "read"], "hermes": []})
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["read"])   # jamais élargi ici

    def test_unchecking_canvas_trims_only_canvas_from_an_identity_above_its_level(self):
        a, s, tok, ph, toks = self.env({"muse": [], "hermit": ["canvas", "read"], "hermes": []},
                                       {"hermit": ["canvas", "display", "feedback", "read"]}, force=True)
        x = next(r for r in a.assistants() if r["id"] == "hermit")
        self.assertTrue(x["over"])                                       # au-delà de « Voir seulement »
        body = self.page(ph, tok)
        self.assertRegex(body, r'<select name="agent_hermit"><option value="keep" selected>')
        body = self.page(ph, tok, agent_muse="none", agent_hermit="keep", agent_hermes="none")
        form = self.save_form(body)
        self.assertEqual((form["agent_hermit"], form["canvas_hermit"]), ("keep", "off"))
        code, _, out = ph.post(form)
        self.assertEqual(code, 200, out)
        self.assertEqual(a.registry.policy()["hermit"], ["read"])
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["display", "feedback", "read"])

    def test_identity_holding_an_allowed_canvas_is_not_over_and_defaults_are_unchanged(self):
        a, s, tok, ph, toks = self.env({"muse": [], "hermit": ["canvas", "read"], "hermes": []},
                                       {"hermit": ["canvas", "read"]})
        x = next(r for r in a.assistants() if r["id"] == "hermit")
        self.assertEqual((x["over"], x["level"], x["canvas"]), (False, "read", True))
        self.assertFalse(appmod.choice_changes(x, "read"))
        body = self.page(ph, tok, agent_muse="none", agent_hermit="read", canvas_hermit="on", agent_hermes="none")
        self.assertNotIn('id="save"', body)
        # niveau changé, case laissée cochée : la permission suit dans la même écriture
        body = self.page(ph, tok, agent_muse="none", agent_hermit="full", canvas_hermit="on", agent_hermes="none")
        form = self.save_form(body)
        self.assertEqual((form["agent_hermit"], form["canvas_hermit"]), ("full", "keep"))
        code, _, out = ph.post(form)
        self.assertEqual(code, 200, out)
        self.assertEqual(a.registry.policy()["hermit"], ["canvas", "display", "feedback", "read"])
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["canvas", "read"])

    def test_identity_holding_canvas_beyond_policy_is_over_and_never_silently_trimmed(self):
        a, s, tok, ph, toks = self.env({"muse": [], "hermit": ["read"], "hermes": []},
                                       {"hermit": ["canvas", "read"]}, force=True)
        x = next(r for r in a.assistants() if r["id"] == "hermit")
        self.assertTrue(x["over"])
        body = self.page(ph, tok, agent_muse="none", agent_hermit="keep", agent_hermes="none")
        self.assertNotIn('id="save"', body)
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["canvas", "read"])

    def test_all_keep_post_writes_nothing_even_for_an_allowed_canvas(self):
        self.assertFalse(agents.canvas_changes({"allowed": ["canvas", "read"]}, "keep"))
        self.assertFalse(agents.canvas_changes({"allowed": ["read"]}, "keep"))
        a, s, tok, ph, toks = self.env({"muse": [], "hermit": ["canvas", "read"], "hermes": []},
                                       {"hermit": ["canvas", "read"]})
        form = self.save_form(self.page(ph, tok, agent_muse="read", agent_hermit="read", canvas_hermit="on",
                                        agent_hermes="none"))
        form.update({k: "keep" for k in form if k.startswith(("agent_", "canvas_"))})
        with open(a.registry.path, "rb") as f:
            before = f.read()
        code, _, out = ph.post(form)
        self.assertEqual(code, 200, out)
        self.assertIn("Aucun choix ne modifiait", html.unescape(out))
        with open(a.registry.path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_server_refuses_invalid_canvas_values_and_missing_field_means_keep(self):
        a, s, tok, ph, toks = self.env({"muse": [], "hermit": ["canvas", "read"], "hermes": []},
                                       {"hermit": ["canvas", "read"]})
        body = self.page(ph, tok, agent_muse="read", agent_hermit="read", canvas_hermit="on", agent_hermes="none")
        form = self.save_form(body)
        bad = dict(form, canvas_hermit="yes")
        code, _, out = ph.post(bad)
        self.assertEqual(code, 400, out)
        self.assertEqual(a.registry.policy()["hermit"], ["canvas", "read"])
        form.pop("canvas_hermit")
        code, _, out = ph.post(form)
        self.assertEqual(code, 200, out)
        self.assertEqual(a.registry.policy(), {"muse": ["read"], "hermit": ["canvas", "read"], "hermes": []})
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["canvas", "read"])

    def test_wifi_reconfiguration_keeps_the_permission_and_the_identity(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.registry.set_policy({"muse": [], "hermit": ["canvas", "read"], "hermes": ["canvas"]})
        tok = a.registry.issue("hermit", ["canvas", "read"])
        a.request_setup()
        a.tick()
        a.net = {"mode": "ethernet", "ip": "192.168.2.40", "wifi_present": False, "nm_ok": True}
        st = a.tv_state()["setup"]["token"]
        code, kind, _ = a.setup_submit({"t": st, "name": "Salon", "net": "keep", "agent_hermit": "read"})
        self.assertEqual((code, kind), (200, "done"))
        self.assertEqual(a.registry.policy(), {"muse": [], "hermit": ["canvas", "read"], "hermes": ["canvas"]})
        self.assertEqual(a.registry.authenticate("hermit", tok), ["canvas", "read"])


class TestModuleSize(unittest.TestCase):
    def test_canvas_tools_module_is_small_and_stdlib_only(self):
        with open(os.path.join(PI4, "opentv_pi", "canvas_tools.py"), encoding="utf-8") as f:
            self.assertLessEqual(len(f.read().splitlines()), 240)


if __name__ == "__main__":
    unittest.main()
