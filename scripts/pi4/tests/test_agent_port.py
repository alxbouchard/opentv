"""Moteur « Pour toi » réutilisé, registre/audit des agents, Agent Port (socket Unix) et serveur MCP stdio."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

from tests._common import PI4, NANOPI, write_catalogue
from opentv_pi import foryou, agents, tools, agentport, mcp

URL_MARKERS = ("http", "www.", "://", ".example/")


def tmpdir(tc):
    d = tempfile.mkdtemp(prefix="otv", dir="/tmp")   # chemin court : limite AF_UNIX de macOS
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def make_foryou(tc, now=1800000000.0):
    d = tmpdir(tc)
    cat = write_catalogue(os.path.join(d, "catalogue.json"))
    fy = foryou.ForYou(NANOPI, os.path.join(d, "state"), cat, clock=lambda: now)
    tc.addCleanup(fy.close)
    return fy, d


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def no_url(tc, obj):
    blob = json.dumps(obj, ensure_ascii=False)
    for m in URL_MARKERS:
        tc.assertNotIn(m, blob)


class FakeCtx:
    def __init__(self, fy):
        self.foryou = fy
        self.messages = []

    def agent_status(self, agent, scopes):
        return {"device": "Salon", "configured": True, "network": {"mode": "ethernet", "online": True},
                "agent": {"id": agent, "scopes": scopes}, "relay": {"status": "not_implemented"}}

    def show_message(self, agent, text, seconds):
        self.messages.append((agent, text, seconds))
        return {"shown": True, "seconds": seconds}


class TestForYouReuse(unittest.TestCase):
    def test_engine_is_loaded_from_nanopi_without_copy(self):
        fy, _ = make_foryou(self)
        self.assertEqual(os.path.realpath(fy.engine.__file__), os.path.realpath(os.path.join(NANOPI, "opentv_foryou.py")))
        with self.assertRaises(foryou.EngineError):
            foryou.ForYou(tmpdir(self), "/tmp/x", "/tmp/y")

    def test_cli_recommend_matches_engine_and_has_no_url(self):
        fy, _ = make_foryou(self)
        code, out = fy.cli(["recommend", "--limit", "5"])
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        no_url(self, out)
        items, _, _ = fy.bridge_mod.load_catalogue_meta(fy.catalogue_file, fy.bridge_mod.Config({}).detail_re)
        direct = fy.engine.recommend(items, "films", {}, {}, 1800000000.0)
        self.assertEqual([x["id"] for x in out["items"]], [x["id"] for x in direct["ranked"]][:5])

    def test_cli_search_feedback_history(self):
        fy, _ = make_foryou(self)
        code, out = fy.cli(["search", "--query", "https://evil.example"])
        self.assertEqual((code, out["error"]), (2, "usage"))
        no_url(self, out)
        code, out = fy.cli(["search", "--query", "nuit"])
        self.assertEqual([x["id"] for x in out["items"]], ["films/102"])
        code, out = fy.cli(["feedback", "--id", "films/102", "--action", "like"])
        self.assertTrue(out["changed"])
        code, out = fy.cli(["feedback", "--id", "films/999", "--action", "like"])
        self.assertEqual((code, out["error"]), (4, "unknown_id"))
        code, out = fy.cli(["history", "--list", "liked"])
        self.assertEqual([x["id"] for x in out["items"]], ["films/102"])

    def test_catalogue_page_is_complete_paged_stable_and_has_no_url(self):
        fy, _ = make_foryou(self)
        first = fy.catalogue_page(2, 0)
        second = fy.catalogue_page(2, 2)
        self.assertEqual(first["total"], 6)
        self.assertEqual((first["offset"], first["limit"], first["has_more"]), (0, 2, True))
        self.assertEqual((second["offset"], second["limit"], second["has_more"]), (2, 2, True))
        all_ids = []
        for offset in range(0, first["total"], 2):
            page = fy.catalogue_page(2, offset)
            all_ids.extend(x["id"] for x in page["items"])
            no_url(self, page)
            for item in page["items"]:
                self.assertNotIn("poster", item)
                self.assertNotIn("detail_path", item)
        self.assertEqual(len(all_ids), first["total"])
        self.assertEqual(len(set(all_ids)), first["total"])

    def test_tv_session_keeps_https_posters_only_and_toggles(self):
        fy, _ = make_foryou(self)
        s = fy.tv_session()
        rows = s["data"]["rows"]
        self.assertTrue(rows)
        for r in rows:
            for it in r["items"]:
                self.assertTrue(it["poster"] is None or it["poster"].startswith("https://"))
                self.assertNotIn("www.", it["description"] or "")
                self.assertNotIn("detail_path", it)
        uid = rows[0]["items"][0]["id"]
        state, msg = fy.tv_feedback(s, uid, "watchlist")
        self.assertTrue(state["watchlist"])
        self.assertIn("À voir plus tard", msg)
        self.assertEqual(fy.tv_feedback(s, uid, "rm -rf"), (None, "Action refusée."))
        self.assertEqual(fy.tv_feedback(s, "films/999", "like"), (None, "Action refusée."))

    def test_feedback_enum_matches_engine(self):
        fy, _ = make_foryou(self)
        self.assertEqual(tuple(sorted(fy.engine.ACTIONS)), tools.FEEDBACK_ACTIONS)


class TestRegistryAudit(unittest.TestCase):
    def test_issue_auth_revoke_hashed_0600(self):
        d = tmpdir(self)
        reg = agents.Registry(os.path.join(d, "agents.json"))
        reg.set_policy({"muse": ["read", "feedback"]})
        tok = reg.issue("muse", ["read"])
        raw = _read(reg.path)
        self.assertNotIn(tok, raw)
        self.assertEqual(stat.S_IMODE(os.stat(reg.path).st_mode), 0o600)
        self.assertEqual(reg.authenticate("muse", tok), ["read"])
        self.assertIsNone(reg.authenticate("muse", tok + "x"))
        self.assertIsNone(reg.authenticate("hermit", tok))
        with self.assertRaises(ValueError):
            reg.issue("muse", ["read", "display"])          # dépasse la politique choisie au QR
        reg2 = agents.Registry(reg.path)                    # rechargement depuis le disque
        self.assertEqual(reg2.authenticate("muse", tok), ["read"])
        reg2.revoke("muse")
        self.assertIsNone(agents.Registry(reg.path).authenticate("muse", tok))

    def test_client_config_0600(self):
        d = tmpdir(self)
        p = agents.write_client_config(os.path.join(d, "agent-muse.json"), "muse", "t" * 43, "/run/opentv/agent.sock")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(json.loads(_read(p))["agent_id"], "muse")

    def test_audit_redacts_and_rotates(self):
        d = tmpdir(self)
        a = agents.Audit(os.path.join(d, "audit.jsonl"), max_bytes=600)
        for _ in range(10):
            a.record("muse", "search", {"query": "mon film secret", "limit": 5, "token": "abc"}, True, None, 3.2)
        a.record("muse", "feedback", {"id": "films/1", "action": "like"}, False, "unknown_id", 1)
        blob = _read(a.path) + (_read(a.path + ".1") if os.path.exists(a.path + ".1") else "")
        self.assertNotIn("secret", blob)
        self.assertNotIn("abc", blob)
        self.assertIn('"action": "like"', blob)
        self.assertLessEqual(os.path.getsize(a.path), 600)
        self.assertEqual(stat.S_IMODE(os.stat(a.path).st_mode), 0o600)


class TestTools(unittest.TestCase):
    def test_schemas_are_closed_and_bounded(self):
        names = [t["name"] for t in tools.TOOLS]
        self.assertEqual(names, ["status", "recommend", "catalogue_list", "search", "feedback", "history", "display_message",
                                 "present_title", "play_title", "close_player", "remote_control"])
        for t in tools.TOOLS:
            self.assertFalse(t["inputSchema"].get("additionalProperties", True), t["name"])
            self.assertIn(t["scope"], ("read", "feedback", "display", "control"))
        self.assertEqual(tools.CONTROL_TOOLS, ("present_title", "play_title", "close_player", "remote_control"))

    def test_validate_args(self):
        self.assertEqual(tools.validate_args("recommend", {}), {"limit": 5, "offset": 0})
        self.assertEqual(tools.validate_args("catalogue_list", {}), {"limit": 20, "offset": 0})
        for name, args in (("recommend", {"limit": 0}), ("recommend", {"limit": 21}), ("recommend", {"x": 1}),
                           ("recommend", {"limit": True}), ("search", {}), ("search", {"query": "a" * 81}),
                           ("catalogue_list", {"limit": 51}), ("catalogue_list", {"offset": 2001}),
                           ("feedback", {"id": "films/1", "action": "delete-db"}), ("feedback", {"id": "../x", "action": "like"}),
                           ("history", {"list": "all"}), ("display_message", {"text": "voir www.x.com"}),
                           ("display_message", {"text": "ok", "seconds": 600}), ("nope", {}), ("status", []),
                           ("status", {"cmd": "reboot"})):
            with self.assertRaises(ValueError, msg=(name, args)):
                tools.validate_args(name, args)


class TestAgentService(unittest.TestCase):
    def setUp(self):
        self.fy, d = make_foryou(self)
        self.ctx = FakeCtx(self.fy)
        self.reg = agents.Registry(os.path.join(d, "agents.json"))
        self.audit = agents.Audit(os.path.join(d, "audit.jsonl"))
        self.clock = [1000.0]
        self.svc = agentport.AgentService(self.ctx, self.reg, self.audit, clock=lambda: self.clock[0])
        self.ro = self.reg.issue("hermit", ["read"])
        self.full = self.reg.issue("muse", ["display", "feedback", "read"])

    def call(self, agent, token, tool, args=None, op="call"):
        return self.svc.handle({"v": 1, "agent": agent, "token": token, "op": op, "tool": tool, "args": args or {}})

    def test_auth_scopes_and_results(self):
        self.assertEqual(self.call("muse", "bad", "status")["error"], "unauthorized")
        self.assertEqual(self.call("hermit", self.ro, "feedback", {"id": "films/102", "action": "like"})["error"],
                         "forbidden_scope")
        r = self.call("hermit", self.ro, "recommend", {"limit": 3})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["result"]["items"]), 3)
        no_url(self, r)
        r = self.call("hermit", self.ro, "catalogue_list", {"limit": 2, "offset": 0})
        self.assertTrue(r["ok"], r)
        self.assertEqual((len(r["result"]["items"]), r["result"]["total"]), (2, 6))
        self.assertTrue(r["result"]["has_more"])
        no_url(self, r)
        r = self.call("muse", self.full, "feedback", {"id": "films/102", "action": "seen"})
        self.assertTrue(r["result"]["changed"])
        r = self.call("hermit", self.ro, "status")
        self.assertEqual(r["result"]["agent"], {"id": "hermit", "scopes": ["read"]})
        self.assertEqual(self.call("hermit", self.ro, None, op="whoami")["result"], {"agent": "hermit", "scopes": ["read"]})
        self.assertEqual(self.call("muse", self.full, "recommend", {"limit": 99})["error"], "invalid_params")
        self.assertEqual(self.svc.handle({"v": 1, "agent": "muse", "token": self.full, "op": "exec", "tool": "x"})["error"],
                         "bad_request")
        self.assertEqual(self.svc.handle("not a dict")["error"], "bad_request")

    def test_search_result_scrubbed(self):
        r = self.call("hermit", self.ro, "search", {"query": "voir"})
        self.assertEqual(r["result"]["items"][0]["id"], "films/105")
        no_url(self, r)

    def test_display_message_rate_limited(self):
        r = self.call("muse", self.full, "display_message", {"text": "Le souper est prêt", "seconds": 10})
        self.assertTrue(r["ok"])
        self.assertEqual(self.ctx.messages, [("muse", "Le souper est prêt", 10)])
        self.assertEqual(self.call("muse", self.full, "display_message", {"text": "Encore"})["error"], "rate_limited")
        self.clock[0] += 6
        self.assertTrue(self.call("muse", self.full, "display_message", {"text": "Encore"})["ok"])

    def test_auth_lockout_after_many_failures(self):
        for _ in range(agentport.AUTH_FAIL_MAX):
            self.call("muse", "wrong", "status")
        self.assertEqual(self.call("muse", self.full, "status")["error"], "locked")
        self.clock[0] += agentport.AUTH_LOCK_S + 1
        self.assertTrue(self.call("muse", self.full, "status")["ok"])

    def test_audit_written_without_secrets(self):
        self.call("hermit", self.ro, "search", {"query": "nuit"})
        self.call("muse", "bad-token-value", "status")
        blob = _read(self.audit.path)
        self.assertIn('"tool": "search"', blob)
        self.assertNotIn("nuit", blob)
        self.assertNotIn(self.ro, blob)
        self.assertNotIn("bad-token-value", blob)


class SocketEnv:
    def __init__(self, tc):
        tc.fy, d = make_foryou(tc)
        self.dir = d
        self.reg = agents.Registry(os.path.join(d, "agents.json"))
        self.svc = agentport.AgentService(FakeCtx(tc.fy), self.reg, agents.Audit(os.path.join(d, "audit.jsonl")))
        self.sock = os.path.join(d, "agent.sock")
        self.server = agentport.AgentPortServer(self.sock, self.svc.handle)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        tc.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def client_cfg(self, agent, scopes, mode=0o600):
        tok = self.reg.issue(agent, scopes)
        p = agents.write_client_config(os.path.join(self.dir, "agent-%s.json" % agent), agent, tok, self.sock)
        os.chmod(p, mode)
        return p


class TestSocket(unittest.TestCase):
    def test_socket_roundtrip_and_mode(self):
        env = SocketEnv(self)
        self.assertEqual(stat.S_IMODE(os.stat(env.sock).st_mode), 0o660)
        tok = env.reg.issue("muse", ["read"])
        r = agentport.call(env.sock, {"v": 1, "agent": "muse", "token": tok, "op": "call", "tool": "status", "args": {}})
        self.assertTrue(r["ok"])
        # requête géante ou non JSON : rejet propre, serveur toujours vivant
        import socket as so
        s = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        s.connect(env.sock)
        s.sendall(b"x" * (agentport.MAX_REQUEST + 10) + b"\n")
        self.assertIn(b"bad_request", s.recv(4096))
        s.close()
        r = agentport.call(env.sock, {"v": 1, "agent": "muse", "token": tok, "op": "whoami"})
        self.assertTrue(r["ok"])


def rpc(lines):
    return "".join(json.dumps(l) + "\n" if not isinstance(l, str) else l + "\n" for l in lines)


class TestMCP(unittest.TestCase):
    def run_mcp(self, lines, call_fn):
        out = io.StringIO()
        mcp.serve(io.StringIO(rpc(lines)), out, call_fn)
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def fake_call(self, scopes=("read",)):
        calls = []

        def call(req):
            calls.append(req)
            if req["op"] == "whoami":
                return {"ok": True, "result": {"agent": "muse", "scopes": list(scopes)}}
            if req["tool"] == "status":
                return {"ok": True, "result": {"device": "Salon"}}
            return {"ok": False, "error": "forbidden_scope"}
        return call, calls

    def test_protocol(self):
        call, calls = self.fake_call()
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        res = self.run_mcp([
            {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
            init,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "feedback",
                                                                         "arguments": {"id": "films/1", "action": "like"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "resources/list"},
            "{not json",
            [{"jsonrpc": "2.0", "id": 7, "method": "ping"}],
            {"jsonrpc": "1.0", "id": 8, "method": "ping"},
            {"jsonrpc": "2.0", "id": 9, "method": "ping"},
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {"cursor": "abc"}},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "recommend", "arguments": {"limit": 500}}},
        ], call)
        by_id = {r.get("id"): r for r in res}
        self.assertEqual(by_id[0]["error"]["code"], -32002)
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(by_id[1]["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual([t["name"] for t in by_id[2]["result"]["tools"]],
                         ["status", "recommend", "catalogue_list", "search", "history"])
        for t in by_id[2]["result"]["tools"]:
            self.assertNotIn("scope", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertFalse(by_id[3]["result"]["isError"])
        self.assertEqual(by_id[3]["result"]["structuredContent"], {"device": "Salon"})
        self.assertEqual(json.loads(by_id[3]["result"]["content"][0]["text"]), {"device": "Salon"})
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertEqual(by_id[5]["error"]["code"], -32602)
        self.assertEqual(by_id[6]["error"]["code"], -32601)
        errs = [r for r in res if r.get("id") is None]
        self.assertEqual(sorted(e["error"]["code"] for e in errs), [-32700, -32600])
        self.assertEqual(by_id[8]["error"]["code"], -32600)
        self.assertEqual(by_id[9]["result"], {})
        self.assertEqual(by_id[10]["error"]["code"], -32602)
        self.assertTrue(by_id[11]["result"]["isError"])               # validé localement, jamais transmis
        self.assertFalse(any(c.get("tool") == "recommend" for c in calls))
        self.assertEqual(len(res), 13)                                 # aucune réponse à la notification

    def test_unsupported_version_negotiates_latest(self):
        call, _ = self.fake_call()
        res = self.run_mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}],
                           call)
        self.assertEqual(res[0]["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_service_down_is_clean_error(self):
        def down(req):
            raise OSError("no socket")
        res = self.run_mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status"}}], down)
        self.assertEqual(res[1]["error"]["code"], -32603)
        self.assertTrue(res[2]["result"]["isError"])

    def test_launcher_end_to_end_and_config_permissions(self):
        env = SocketEnv(self)
        cfg = env.client_cfg("muse", ["read", "feedback"])
        lines = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "recommend", "arguments": {"limit": 2}}},
                     {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "feedback", "arguments": {"id": "films/101", "action": "like"}}}])
        exe = os.path.join(PI4, "bin", "opentv-mcp")
        p = subprocess.run([sys.executable, "-I", exe, "--config", cfg], input=lines.encode(), capture_output=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = [json.loads(l) for l in p.stdout.decode().splitlines()]
        self.assertEqual(res[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([t["name"] for t in res[1]["result"]["tools"]],
                         ["status", "recommend", "catalogue_list", "search", "feedback", "history"])
        self.assertEqual(len(res[2]["result"]["structuredContent"]["items"]), 2)
        self.assertTrue(res[3]["result"]["structuredContent"]["changed"])
        no_url(self, res)
        tok = json.loads(_read(cfg))["token"]
        self.assertNotIn(tok, p.stderr.decode() + p.stdout.decode())
        os.chmod(cfg, 0o644)
        p = subprocess.run([sys.executable, "-I", exe, "--config", cfg], input=lines.encode(), capture_output=True,
                           timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertNotIn(tok, p.stderr.decode())


if __name__ == "__main__":
    unittest.main()
