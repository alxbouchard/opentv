"""Pont Muse : SDK officiel épinglé (transport seulement), aucune commande par défaut, liste fermée de commandes OpenTV
typées (lecture, goûts, message, pilotage) déléguées par MCP à l'Agent Port authentifié avec EXACTEMENT les scopes du
pont, bornes, nettoyage, refus fail-closed, secrets jamais journalisés, service jamais démarré sans appariement,
autorisation explicite du pilotage, installation/désinstallation, CLI status|authorize-control|pair|unpair.

Les tests qui exercent le chiffrement Noise réel (faux VM Muse en mémoire) exigent `cryptography` (python3-cryptography
sur la Pi) ; sans elle ils sont sautés avec un message explicite.
"""
import asyncio
import contextlib
import configparser
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from tests._common import PI4, NANOPI, write_catalogue
from tests import test_agent_port as tap
from opentv_pi import agentport, agents, cli, config, foryou, fsutil, museadmin, musebluez, musebridge, tools
from opentv_pi import app as appmod, web

VENDOR = os.path.join(PI4, "vendor", "muse")
PIN_COMMIT = "1db6a26f4ca56939c4465d8a12e241c1b1b5c8dc"
ARCHIVE_SHA256 = "bc02e8dfb54b7fb52cd500c584f6cda732195a9c0b0cb8958bc3a668d72fb2ee"
WHEEL_SHA256 = "f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23"
UPSTREAM = os.environ.get("OPENTV_TEST_MUSE_UPSTREAM",
                          os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(PI4))),
                                       "muse-gadget-sdk-review"))
EXPECTED = ["opentv.catalogue_list", "opentv.close_player", "opentv.display_message", "opentv.feedback",
            "opentv.history", "opentv.play_title", "opentv.present_title", "opentv.recommend", "opentv.remote_control",
            "opentv.search", "opentv.status"]
CONTROL = ["control", "display", "feedback", "read"]
HAVE_CRYPTO = importlib.util.find_spec("cryptography") is not None
needs_crypto = unittest.skipUnless(HAVE_CRYPTO, "cryptography absente (python3-cryptography sur la Pi)")
SDK_TOKEN = "mgst_" + "Q" * 42 + "A"
ACCESS, REFRESH, VM_TOKEN = "dev-access-SECRET-0123456789", "dev-refresh-SECRET-9876543210", "vm-bearer-SECRET-5555"
URL_MARKERS = ("http", "www.", "://", ".example/")


def tmpdir(tc):
    d = tempfile.mkdtemp(prefix="otvm", dir="/tmp")   # chemin court : limite AF_UNIX de macOS
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_sdk():
    return musebridge.load_sdk(VENDOR, require_websockets=False)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ControlCtx(tap.FakeCtx):
    """Contexte d'Agent Port avec pilotage simulé (l'App réelle est testée dans test_muse_control)."""

    def __init__(self, fy):
        super().__init__(fy)
        self.control = []
        self.seen = []   # appels authentifiés réellement servis (« actif » sur la TV)

    def agent_seen(self, agent):
        self.seen.append(agent)

    def agent_control(self, agent, name, a):
        self.control.append((agent, name, dict(a)))
        return {"accepted": True, "id": a.get("id"), "action": {"seq": len(self.control), "kind": name,
                                                                 "state": "queued", "reason": None}}


class AgentPortFixture:
    """Agent Port réel (socket Unix) + registre + audit + moteur « Pour toi » réel sur un catalogue piégé."""

    def __init__(self, tc, ctx=None):
        self.d = tmpdir(tc)
        cat = write_catalogue(os.path.join(self.d, "catalogue.json"))
        self.fy = foryou.ForYou(NANOPI, os.path.join(self.d, "fy"), cat, clock=lambda: 1800000000.0)
        tc.addCleanup(self.fy.close)
        self.reg = agents.Registry(os.path.join(self.d, "agents.json"))
        # Muse autorisée à piloter sur la TV (plafond de l'identité dédiée liée « muse-device ») ; autres sans accès
        self.reg.set_policy({"muse": list(CONTROL), "hermit": ["read"], "hermes": []})
        self.audit_path = os.path.join(self.d, "audit.jsonl")
        self.audit = agents.Audit(self.audit_path)
        self.ctx = ctx or ControlCtx(self.fy)
        svc = agentport.AgentService(self.ctx, self.reg, self.audit)
        self.sock = os.path.join(self.d, "a.sock")
        self.srv = agentport.AgentPortServer(self.sock, svc.handle)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        tc.addCleanup(self.srv.server_close)
        tc.addCleanup(self.srv.shutdown)
        self.client = os.path.join(self.d, "agent-muse-device.json")

    def issue(self, scopes=tuple(CONTROL), agent=musebridge.MUSE_AGENT_ID):
        self.token = self.reg.issue(agent, list(scopes), force=True)
        agents.write_client_config(self.client, agent, self.token, self.sock)
        return self.token

    def audit_records(self):
        if not os.path.exists(self.audit_path):
            return []
        return [json.loads(l) for l in read(self.audit_path).splitlines() if l.strip()]


# ------------------------------------------------------------------------------------------- chaîne d'approvisionnement
class TestSupplyChain(unittest.TestCase):
    def setUp(self):
        self.pin = musebridge.load_pin(VENDOR)

    def test_pin_is_exact_commit_and_official_archive_never_main(self):
        self.assertEqual(self.pin["commit"], PIN_COMMIT)
        self.assertEqual(self.pin["archive"]["sha256"], ARCHIVE_SHA256)
        self.assertEqual(self.pin["archive"]["size"], 6433027)
        self.assertIn(PIN_COMMIT, self.pin["archive"]["url"])
        blob = json.dumps(self.pin)
        self.assertNotIn("/main/", blob)
        self.assertNotIn("@main", blob)
        dep = self.pin["dependencies"]["websockets"]
        self.assertEqual((dep["sha256"], dep["version"]), (WHEEL_SHA256, "17.1"))
        self.assertTrue(dep["url"].startswith("https://files.pythonhosted.org/"))
        self.assertTrue(dep["url"].endswith("/" + dep["file"]))

    def test_vendored_subset_matches_pin_and_excludes_default_command_modules(self):
        v = musebridge.verify_sdk(VENDOR)
        self.assertTrue(v["ok"], v["problems"])
        for name in ("executor.py", "fileops.py", "service.py", "cli.py", "__main__.py", "doctor.py", "data"):
            self.assertFalse(os.path.lexists(os.path.join(VENDOR, "musegadget", name)), name)
        for rel, digest in self.pin["files"].items():
            self.assertEqual(sha(os.path.join(VENDOR, rel)), digest, rel)
        for root, _, files in os.walk(os.path.join(VENDOR, "musegadget")):
            for f in files:
                if f.endswith(".py"):
                    text = read(os.path.join(root, f))
                    for bad in ("COMMAND_SPECS", "system.run", "file.read", "file.write", "subprocess.Popen"):
                        self.assertNotIn(bad, text, (f, bad))

    def test_installer_and_prepare_use_the_same_pinned_wheel(self):
        dep = self.pin["dependencies"]["websockets"]
        for path in (os.path.join(PI4, "install.sh"), os.path.join(PI4, "prepare-pi.sh")):
            text = read(path)
            self.assertIn('MUSE_WHEEL="%s"' % dep["file"], text, path)
            self.assertIn('MUSE_WHEEL_SHA256="%s"' % dep["sha256"], text, path)
        self.assertIn('MUSE_WHEEL_URL="%s"' % dep["url"], read(os.path.join(PI4, "prepare-pi.sh")))
        self.assertIn("--proto '=https'", read(os.path.join(PI4, "prepare-pi.sh")))

    @unittest.skipUnless(os.path.isdir(os.path.join(UPSTREAM, ".git")), "dépôt officiel de revue absent")
    def test_vendored_bytes_equal_the_official_commit(self):
        def show(path):
            return subprocess.run(["git", "-C", UPSTREAM, "show", "%s:%s" % (PIN_COMMIT, path)], capture_output=True,
                                  check=True, timeout=30).stdout
        for rel in self.pin["files"]:
            src = "LICENSE" if rel == "LICENSE" else "linux/src/" + rel
            with open(os.path.join(VENDOR, rel), "rb") as f:
                self.assertEqual(f.read(), show(src), rel)
        lock = show("linux/src/musegadget/data/requirements.lock").decode()
        self.assertIn(WHEEL_SHA256, lock.split("websockets==17.1")[1])
        svc = show("linux/src/musegadget/service.py").decode()
        self.assertIn('DEFAULT_NOISE_HOST = "%s"' % musebridge.DEFAULT_NOISE_HOST, svc)
        cfg = show("linux/src/musegadget/config.py").decode()
        self.assertIn('re.compile(r"%s")' % museadmin.SDK_TOKEN_RE.pattern, cfg)

    def test_tampering_is_detected_and_refused(self):
        d = tmpdir(self)
        dst = os.path.join(d, "muse")
        shutil.copytree(VENDOR, dst, ignore=shutil.ignore_patterns("__pycache__"))
        self.assertTrue(musebridge.verify_sdk(dst)["ok"])
        with open(os.path.join(dst, "musegadget", "link_client.py"), "a") as f:
            f.write("\n# altéré\n")
        with open(os.path.join(dst, "musegadget", "executor.py"), "w") as f:
            f.write("COMMAND_SPECS = {}\n")
        os.remove(os.path.join(dst, "musegadget", "noise", "framing.py"))
        v = musebridge.verify_sdk(dst)
        self.assertFalse(v["ok"])
        self.assertIn("modified:musegadget/link_client.py", v["problems"])
        self.assertIn("unexpected:musegadget/executor.py", v["problems"])
        self.assertIn("excluded_present:musegadget/executor.py", v["problems"])
        self.assertIn("missing:musegadget/noise/framing.py", v["problems"])
        with self.assertRaises(musebridge.BridgeError) as e:
            musebridge.load_sdk(dst, require_websockets=False)
        self.assertEqual(e.exception.code, "sdk_unverified")
        self.assertNotIn(dst, sys.path)
        with open(os.path.join(dst, self.pin["dependencies"]["websockets"]["file"]), "wb") as f:
            f.write(b"not the pinned wheel")
        self.assertEqual(musebridge.verify_sdk(dst)["websockets"], "bad_hash")

    def test_websockets_is_required_for_run(self):
        with self.assertRaises(musebridge.BridgeError) as e:
            musebridge.load_sdk(VENDOR, require_websockets=True)
        self.assertEqual(e.exception.code, "dependency_missing")


# ------------------------------------------------------------------------------------------- aucune commande par défaut
class TestNoDefaultCommands(unittest.TestCase):
    def test_registered_command_set_is_exactly_the_bounded_typed_set(self):
        specs = musebridge.command_specs()
        self.assertEqual(sorted(specs), EXPECTED)
        self.assertEqual(musebridge.MUSE_SCOPES, CONTROL)                     # ni lecture seule, ni plus
        self.assertEqual(sorted({tools.TOOL_BY_NAME[t]["scope"] for t in musebridge.COMMANDS.values()}), CONTROL)
        for name, spec in specs.items():
            self.assertFalse(name.startswith(("system.", "file.", "device.")), name)
            self.assertTrue(name.startswith("opentv."), name)
            self.assertIn(tools.TOOL_BY_NAME[musebridge.COMMANDS[name]]["scope"], musebridge.MUSE_SCOPES)
            self.assertEqual(set(spec), {"description", "required", "optional", "timeout_ms"})
            params = dict(spec["required"], **spec["optional"])
            for p, ps in params.items():
                self.assertNotIn(p, ("command", "cmd", "path", "url", "cwd", "file", "data_base64", "uri", "host",
                                     "tool", "name", "args", "duration", "repeat", "times", "code", "raw"))
                self.assertIn(ps["type"], ("integer", "string"))
                if ps["type"] == "integer":
                    self.assertRegex(ps["description"], r"de \d+ à \d+")
        # pilotage : identifiants stables et énumérations fermées seulement
        self.assertEqual(set(specs["opentv.present_title"]["required"]), {"id"})
        self.assertEqual(set(specs["opentv.present_title"]["optional"]), {"text"})
        self.assertEqual(set(specs["opentv.play_title"]["required"]), {"id"})
        self.assertEqual(specs["opentv.play_title"]["optional"], {})
        self.assertEqual((specs["opentv.close_player"]["required"], specs["opentv.close_player"]["optional"]), ({}, {}))
        self.assertEqual(set(specs["opentv.remote_control"]["required"]), {"key"})
        for k in ("up", "down", "left", "right", "ok", "back", "home"):
            self.assertIn(k, specs["opentv.remote_control"]["required"]["key"]["description"])
        self.assertEqual(tools.TOOL_BY_NAME["remote_control"]["inputSchema"]["properties"]["key"]["enum"],
                         ["up", "down", "left", "right", "ok", "back", "home"])
        # Muse est invitée à choisir et présenter elle-même, pas à recopier des pages brutes
        self.assertIn("present_title", specs["opentv.recommend"]["description"])
        self.assertIn("explicitement", specs["opentv.play_title"]["description"])

    @needs_crypto
    def test_import_guard_blocks_executor_service_fileops_and_cli(self):
        load_sdk()
        import musegadget
        self.assertTrue(os.path.realpath(musegadget.__file__).startswith(os.path.realpath(VENDOR)))
        # même si un executor.py/service.py apparaissait dans le paquet, le garde refuse de le charger
        d = tmpdir(self)
        for n in ("executor", "service", "fileops", "cli"):
            with open(os.path.join(d, n + ".py"), "w") as f:
                f.write("COMMAND_SPECS = {'system.run': {}}\n")
        with mock.patch.object(musegadget, "__path__", list(musegadget.__path__) + [d]):
            for mod in sorted(musebridge.FORBIDDEN_MODULES):
                with self.assertRaises(ImportError):
                    __import__(mod)
            # contrôle de mutation : sans le garde, le module planté serait bien chargé
            guards = [f for f in sys.meta_path if isinstance(f, musebridge._ImportGuard)]
            for g in guards:
                sys.meta_path.remove(g)
            try:
                __import__("musegadget.executor")
                self.assertIn("system.run", sys.modules["musegadget.executor"].COMMAND_SPECS)
            finally:
                sys.modules.pop("musegadget.executor", None)
                if hasattr(musegadget, "executor"):
                    delattr(musegadget, "executor")
                sys.meta_path[0:0] = guards
        for mod in musebridge.FORBIDDEN_MODULES:
            self.assertNotIn(mod, sys.modules)
        with self.assertRaises(ImportError):
            __import__("musegadget.executor")

    def test_our_code_never_references_sdk_defaults(self):
        for rel in ("opentv_pi/musebridge.py", "opentv_pi/museadmin.py", "bin/opentv-muse-bridge"):
            text = read(os.path.join(PI4, rel))
            for bad in ("COMMAND_SPECS", "Executor(", "import Executor", "from musegadget.executor",
                        "from musegadget.service", "from musegadget import service", "from musegadget import executor",
                        "run_service", "subprocess", "shell=True"):
                self.assertFalse(bad in text, (rel, bad))

    @needs_crypto
    def test_describe_registers_only_our_commands(self):
        sdk = load_sdk()
        ident = sdk.identity.Identity("02:11:22:33:44:55")
        b = musebridge.Bridge(sdk, ident, port=None, status_path=None)
        params = b.describe().register_params()
        self.assertEqual(sorted(params["commands_v2"]), EXPECTED)
        self.assertEqual((params["platform"], params["device_family"]), ("linux", "homehub"))
        self.assertEqual(params["node_id"], "homelink-334455")


# ------------------------------------------------------------------------------------------- MCP + Agent Port
class TestThroughAgentPort(unittest.TestCase):
    def setUp(self):
        self.fx = AgentPortFixture(self)
        self.fx.issue()
        self.port = musebridge.AgentPortMCP(self.fx.client)

    def call(self, cmd, params=None):
        return musebridge.dispatch(self.port, cmd, params)

    def test_catalogue_list_pages_through_the_complete_catalogue_without_urls(self):
        first = self.call("opentv.catalogue_list", {"limit": 2, "offset": 0})
        self.assertTrue(first["ok"], first)
        total, ids = first["payload"]["total"], []
        generation = first["payload"]["generated_at"]                 # marqueur de génération conservé
        self.assertTrue(generation)
        self.assertIn("generated_at", musebridge.command_specs()["opentv.catalogue_list"]["description"])
        self.assertEqual(total, 6)
        offset = 0
        while True:
            r = self.call("opentv.catalogue_list", {"limit": 2, "offset": offset})
            self.assertTrue(r["ok"], r)
            ids += [x["id"] for x in r["payload"]["items"]]
            self.assertEqual(r["payload"]["generated_at"], generation)
            blob = json.dumps(r, ensure_ascii=False)
            for m in URL_MARKERS:
                self.assertNotIn(m, blob)
            for it in r["payload"]["items"]:
                self.assertNotIn("poster", it)
                self.assertNotIn("detail_path", it)
            if not r["payload"]["has_more"]:
                break
            offset += 2
        self.assertEqual(len(ids), total)
        self.assertEqual(len(set(ids)), total)
        recs = [x for x in self.fx.audit_records() if x["tool"] == "catalogue_list"]
        self.assertEqual(len(recs), 4)
        self.assertTrue(all(x["agent"] == "muse-device" and x["ok"] for x in recs))

    def test_status_search_recommend_history_pass_through_mcp_with_auth(self):
        for cmd, params in (("opentv.status", {}), ("opentv.search", {"query": "nuit"}),
                            ("opentv.recommend", {"limit": 3}), ("opentv.history", {"list": "liked"})):
            r = self.call(cmd, params)
            self.assertTrue(r["ok"], (cmd, r))
            for m in URL_MARKERS:
                self.assertNotIn(m, json.dumps(r, ensure_ascii=False))
        self.assertEqual([x["id"] for x in self.call("opentv.search", {"query": "nuit"})["payload"]["items"]],
                         ["films/102"])
        tools_seen = [x["tool"] for x in self.fx.audit_records()]
        for t in ("status", "search", "recommend", "history"):
            self.assertIn(t, tools_seen)
        self.assertNotIn(self.fx.token, read(self.fx.audit_path))
        self.assertEqual(self.port.identity_scopes(), CONTROL)

    def test_feedback_display_and_control_commands_cross_the_agent_port(self):
        rates = mock.patch.dict(agentport.TOOL_RATES, clear=True)   # débits testés à part (test_muse_control)
        rates.start()
        self.addCleanup(rates.stop)
        r = self.call("opentv.feedback", {"id": "films/101", "action": "watchlist"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.fx.fy.cli(["history", "--list", "watchlist"])[1]["items"][0]["id"], "films/101")
        self.assertTrue(self.call("opentv.display_message", {"text": "Ce soir : un thriller ?"})["ok"])
        self.assertEqual(self.fx.ctx.messages[-1][:2], ("muse-device", "Ce soir : un thriller ?"))
        for cmd, params in (("opentv.present_title", {"id": "films/106", "text": "Tu aimes la science-fiction."}),
                            ("opentv.present_title", {"id": "films/106", "text": None}),       # null = omis
                            ("opentv.play_title", {"id": "films/106"}), ("opentv.close_player", {}),
                            ("opentv.remote_control", {"key": "down"})):
            r = self.call(cmd, params)
            self.assertTrue(r["ok"], (cmd, r))
            self.assertEqual(r["payload"]["action"]["state"], "queued")
        self.assertEqual([c[1] for c in self.fx.ctx.control],
                         ["present_title", "present_title", "play_title", "close_player", "remote_control"])
        self.assertEqual(self.fx.ctx.control[1][2], {"id": "films/106"})
        self.assertTrue(all(c[0] == "muse-device" for c in self.fx.ctx.control))
        audited = [x["tool"] for x in self.fx.audit_records()]
        for t in ("feedback", "display_message", "present_title", "play_title", "close_player", "remote_control"):
            self.assertIn(t, audited)

    def test_control_withdrawn_on_the_tv_pauses_every_bridge_call(self):
        """Pilotage retiré dans « Gérer les assistants » : l'identité liée est réduite dans la même écriture et
        l'Agent Port refuse aussitôt TOUT appel du pont (bridge_paused) — lecture, avis et message compris, jamais
        une session à moitié autorisée ; l'identité reste installée (en pause) et l'audit garde « muse-device »."""
        reg = self.fx.reg
        _, fp, _ = reg.snapshot(("muse", "hermit", "hermes"))
        reg.apply_policy_changes({"muse": ["display", "feedback", "read"]}, ("muse", "hermit", "hermes"), fp)
        before = len(self.fx.audit_records())
        for cmd, params in (("opentv.present_title", {"id": "films/106"}), ("opentv.status", {}),
                            ("opentv.catalogue_list", {}), ("opentv.display_message", {"text": "Bonsoir"}),
                            ("opentv.feedback", {"id": "films/101", "action": "watchlist"})):
            self.assertEqual(self.call(cmd, params), {"ok": False, "error": "bridge_paused"}, cmd)
        with self.assertRaises(musebridge.BridgeError) as e:
            self.port.identity_scopes()
        self.assertEqual(e.exception.code, "bridge_paused")
        self.assertEqual((self.fx.ctx.control, self.fx.ctx.messages), ([], []))
        self.assertEqual(self.fx.fy.cli(["history", "--list", "watchlist"])[1].get("items", []), [])
        self.assertEqual(reg.authenticate(musebridge.MUSE_AGENT_ID, self.fx.token), ["display", "feedback", "read"])
        new = self.fx.audit_records()[before:]
        self.assertEqual(len(new), 6)
        self.assertEqual({(x["agent"], x["ok"], x["error"]) for x in new}, {("muse-device", False, "bridge_paused")})

    def test_generic_identities_are_never_paused(self):
        """La pause ne vise QUE l'identité liée : un client « muse » générique (ou Hermit) en lecture seule garde
        exactement le comportement par scope (lecture servie, outil hors scope ⇒ forbidden_scope)."""
        reg = self.fx.reg
        _, fp, _ = reg.snapshot(("muse", "hermit", "hermes"))
        reg.apply_policy_changes({"muse": ["read"]}, ("muse", "hermit", "hermes"), fp)
        for agent in ("muse", "hermit"):
            tok = reg.issue(agent, ["read"])
            base = {"v": 1, "agent": agent, "token": tok}
            r = agentport.call(self.fx.sock, dict(base, op="whoami"))
            self.assertEqual(r, {"ok": True, "result": {"agent": agent, "scopes": ["read"]}})
            self.assertTrue(agentport.call(self.fx.sock, dict(base, op="call", tool="catalogue_list", args={}))["ok"])
            self.assertEqual(agentport.call(self.fx.sock, dict(base, op="call", tool="display_message",
                                                               args={"text": "Salut"}))["error"], "forbidden_scope")
        self.assertFalse(agents.linked_paused("muse", ["read"]))
        self.assertFalse(agents.linked_paused("hermit", []))
        self.assertFalse(agents.linked_paused("muse-device", CONTROL))
        for sc in ([], ["read"], ["feedback", "read"], ["display", "feedback", "read"], CONTROL + ["extra"]):
            self.assertTrue(agents.linked_paused("muse-device", sc), sc)

    def test_default_and_write_commands_never_reach_the_agent_port(self):
        before = len(self.fx.audit_records())
        for cmd in ("system.run", "file.read", "file.write", "device.health", "opentv.exec", "opentv.open_url",
                    "opentv.key", "opentv.tv_power", "opentv.agent_issue", "feedback", "status", "present_title", "",
                    None, 7):
            self.assertEqual(self.call(cmd, {"command": "id"}), {"ok": False, "error": "unknown_command"}, cmd)
        for tool in ("exec", "agent_issue", "open_url", "system.run"):
            with self.assertRaises(musebridge.BridgeError) as e:
                self.port.call_tool(tool, {})
            self.assertEqual(e.exception.code, "unknown_command")
        self.assertEqual(len(self.fx.audit_records()), before)

    def test_invalid_or_unbounded_params_fail_before_any_call(self):
        before = len(self.fx.audit_records())
        bad = [("opentv.catalogue_list", {"limit": 51}), ("opentv.catalogue_list", {"offset": -1}),
               ("opentv.catalogue_list", {"offset": 2001}), ("opentv.catalogue_list", {"limit": True}),
               ("opentv.catalogue_list", {"limit": 2.0}), ("opentv.catalogue_list", {"limit": "2"}),
               ("opentv.catalogue_list", {"path": "/var/lib/opentv/catalogue.json"}),
               ("opentv.catalogue_list", "limit=2"), ("opentv.catalogue_list", ["limit"]),
               ("opentv.search", {}), ("opentv.search", {"query": "https://evil.example/x"}),
               ("opentv.search", {"query": "www.evil.example"}), ("opentv.search", {"query": "a\nb"}),
               ("opentv.search", {"query": "x" * 81}), ("opentv.history", {"list": "../../etc"}),
               ("opentv.status", {"url": "http://x"}), ("opentv.recommend", {"limit": 0}),
               # pilotage : identifiant stable, énumérations fermées, texte sans adresse, aucun champ en plus
               ("opentv.present_title", {}), ("opentv.present_title", {"id": "https://evil.example/f"}),
               ("opentv.present_title", {"id": "films/101/../../x"}), ("opentv.present_title", {"id": "Films/101"}),
               ("opentv.present_title", {"id": "films/101", "text": "voir www.evil.example"}),
               ("opentv.present_title", {"id": "films/101", "text": "http://x.example/a"}),
               ("opentv.present_title", {"id": "films/101", "text": "a\nb"}),
               ("opentv.present_title", {"id": "films/101", "text": "x" * 141}),
               ("opentv.present_title", {"id": "films/101", "url": "https://x"}),
               ("opentv.play_title", {}), ("opentv.play_title", {"id": "films/101", "seconds": 30}),
               ("opentv.play_title", {"id": "films/101", "source": "https://cdn.example/v.m3u8"}),
               ("opentv.play_title", {"id": 101}), ("opentv.close_player", {"force": True}),
               ("opentv.remote_control", {}), ("opentv.remote_control", {"key": "power"}),
               ("opentv.remote_control", {"key": "volume_up"}), ("opentv.remote_control", {"key": "OK"}),
               ("opentv.remote_control", {"key": "ok", "repeat": 50}), ("opentv.remote_control", {"key": "menu"}),
               ("opentv.feedback", {"id": "films/101", "action": "delete"}),
               ("opentv.display_message", {"text": "https://evil.example"}),
               ("opentv.display_message", {"text": "ok", "seconds": 3600})]
        for cmd, params in bad:
            self.assertEqual(self.call(cmd, params), {"ok": False, "error": "invalid_params"}, (cmd, params))
        self.assertEqual(len(self.fx.audit_records()), before)

    def test_unauthorized_identity_fails_closed(self):
        self.fx.reg.revoke(musebridge.MUSE_AGENT_ID)
        self.assertEqual(self.call("opentv.catalogue_list"), {"ok": False, "error": "unauthorized"})
        with self.assertRaises(musebridge.BridgeError) as e:
            self.port.identity_scopes()
        self.assertEqual(e.exception.code, "unauthorized")

    def test_bridge_identity_without_the_exact_scopes_is_paused_by_the_agent_port(self):
        self.fx.issue(scopes=("display",))
        self.assertEqual(self.call("opentv.catalogue_list"), {"ok": False, "error": "bridge_paused"})
        with self.assertRaises(musebridge.BridgeError) as e:
            self.port.identity_scopes()
        self.assertEqual(e.exception.code, "bridge_paused")

    def test_wrong_missing_or_unreachable_identity(self):
        self.fx.issue(agent="hermit")
        self.assertEqual(self.call("opentv.status"), {"ok": False, "error": "bridge_identity_invalid"})
        os.remove(self.fx.client)
        self.assertEqual(self.call("opentv.status"), {"ok": False, "error": "bridge_identity_missing"})
        self.fx.issue()
        os.chmod(self.fx.client, 0o644)   # fichier personnel lisible par d'autres : refusé par le client MCP
        self.assertEqual(self.call("opentv.status"), {"ok": False, "error": "bridge_identity_missing"})
        os.chmod(self.fx.client, 0o600)
        self.fx.srv.shutdown()
        self.fx.srv.server_close()
        self.assertEqual(self.call("opentv.status"), {"ok": False, "error": "service_unavailable"})

    def test_results_are_scrubbed_and_bounded(self):
        class Stub:
            def __init__(self, res):
                self.res = res

            def call_tool(self, name, args):
                return self.res
        nasty = {"items": [{"id": "films/1", "title": "Voir https://evil.example", "poster": "https://img/x.jpg",
                            "detail_path": "b/x", "token": "abc", "Weird-Key": 1, "note": "ok\x00\x1b[31m",
                            "nested": {"deep": [{"url_like": "www.x.example"}]}}] * 200,
                 "socket": "/run/opentv/agent.sock", "n": float("nan"), "big": 2 ** 60}
        r = musebridge.dispatch(Stub(nasty), "opentv.status", {})
        self.assertTrue(r["ok"])
        p = r["payload"]
        self.assertEqual(len(p["items"]), musebridge.MAX_LIST)
        it = p["items"][0]
        self.assertEqual(it["title"], "Voir")                           # seule l'adresse part, le titre reste
        for k in ("poster", "detail_path", "token", "Weird-Key"):
            self.assertNotIn(k, it)
        self.assertNotIn("\x1b", it["note"])
        self.assertNotIn("socket", p)
        self.assertIsNone(p["n"])
        self.assertIsNone(p["big"])
        self.assertEqual(it["nested"], {"deep": [{}]})
        huge = {"items": [{"t%d" % i: "x" * 1000 for i in range(40)} for _ in range(50)]}
        self.assertEqual(musebridge.dispatch(Stub(huge), "opentv.status", {}),
                         {"ok": False, "error": "result_too_large"})

        class Boom:
            def call_tool(self, name, args):
                raise RuntimeError("secret detail " + SDK_TOKEN)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(musebridge.dispatch(Boom(), "opentv.status", {}), {"ok": False, "error": "internal"})
        self.assertNotIn(SDK_TOKEN, out.getvalue())
        self.assertNotIn("secret detail", out.getvalue())


# ------------------------------------------------------------------------------------------- faux VM Muse (Noise réel)
class Pipe:
    def __init__(self, inbox, outbox):
        self._inbox, self._outbox = inbox, outbox

    async def send(self, data):
        await self._outbox.put(data)

    async def recv(self):
        data = await self._inbox.get()
        if data is None:
            raise ConnectionError("closed")
        return data

    async def close(self):
        await self._outbox.put(None)


class FakeVm:
    """Juste assez du VM Muse (repris des tests officiels du SDK, Apache-2.0) : répondeur Noise + flux de contrôle."""

    def __init__(self, ws):
        from musegadget.link_client import MessageDecoder
        from musegadget.noise import NoiseFrameDecoder
        self.ws, self.decoder, self.messages, self.stream_id = ws, NoiseFrameDecoder(), MessageDecoder(), 0

    async def handshake(self):
        from musegadget.noise import NoiseXXResponder
        r = NoiseXXResponder()
        r.initialize()
        await self.ws.send(r.read_message1_and_write_message2(await self.ws.recv()))
        r.read_message3(await self.ws.recv())
        self.send_cipher, self.recv_cipher = r.split()

    async def next_frame(self):
        from musegadget.noise.transport import decode_request_envelope
        while True:
            plain = self.recv_cipher.decrypt_with_ad(b"", await self.ws.recv())
            assembled = self.decoder.decode(plain)
            if assembled is not None:
                return decode_request_envelope(assembled)

    async def next_message(self):
        while True:
            frame = await self.next_frame()
            if frame.kind != "body_chunk":
                continue
            msgs = self.messages.feed(frame.value.data)
            if msgs:
                return msgs[0]

    async def send_frame(self, frame):
        from musegadget.noise import encode_noise_frames
        from musegadget.noise.transport import encode_response_envelope
        for chunk in encode_noise_frames(encode_response_envelope(frame)):
            await self.ws.send(self.send_cipher.encrypt_with_ad(b"", chunk))

    async def accept_control_stream(self):
        from musegadget.noise import ApplicationResponse, ServiceFrame
        req = await self.next_frame()
        self.stream_id = req.stream_id
        await self.send_frame(ServiceFrame.response(self.stream_id, ApplicationResponse(status=200, end_body=False)))
        return req

    async def send_message(self, message):
        from musegadget.link_client import encode_message
        from musegadget.noise import BodyChunk, ServiceFrame
        await self.send_frame(ServiceFrame.body_chunk(self.stream_id, BodyChunk(data=encode_message(message))))


class StateEnv:
    def __init__(self, tc):
        self.dir = tmpdir(tc)
        os.chmod(self.dir, 0o700)
        p = mock.patch.dict(os.environ, {"MUSEGADGET_STATE_DIR": self.dir})
        p.start()
        tc.addCleanup(p.stop)

    def write(self, name, data, mode=0o600):
        path = os.path.join(self.dir, name)
        fsutil.atomic_write_path(path, data if isinstance(data, (bytes, str)) else json.dumps(data), mode=mode)
        return path

    def pairing(self):
        return self.write("pairing.json", {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "device",
                                           "noise_host": "gw.example", "api_url_v2": "",
                                           "access_token_saved_at": int(time.time())})


@needs_crypto
class TestEndToEndFakeMuse(unittest.TestCase):
    def setUp(self):
        self.sdk = load_sdk()
        self.st = StateEnv(self)
        self.fx = AgentPortFixture(self)
        self.fx.issue()
        self.st.write("sdk_token", SDK_TOKEN + "\n")
        self.status = os.path.join(self.st.dir, "run-status.json")
        self.counts = {"fetch": 0, "connect": 0, "refresh": 0}

    def fetch(self, token, root):
        self.counts["fetch"] += 1
        return [{"vm_url": "wss://x", "vm_auth_token": VM_TOKEN, "vm_name": "vm", "vm_id": "vm-1",
                 "is_default": True}], 200

    def refresh(self, *a):
        self.counts["refresh"] += 1
        return None, None

    def bridge(self, connect=None):
        ident = self.sdk.identity.load_or_create()
        return musebridge.Bridge(self.sdk, ident, musebridge.AgentPortMCP(self.fx.client), sdk_token=SDK_TOKEN,
                                 connect=connect, fetch_vms=self.fetch, refresh=self.refresh,
                                 status_path=self.status, sleep_scale=0.001)

    def test_register_invoke_catalogue_refuse_defaults_and_unpair_without_leaking_secrets(self):
        self.st.pairing()
        logs, out = io.StringIO(), io.StringIO()
        handler = musebridge.setup_logging(logs)
        self.addCleanup(logging_reset, handler)
        to_dev, to_vm = asyncio.Queue, asyncio.Queue
        results = {}

        async def scenario():
            qd, qv = to_dev(), to_vm()
            dev_ws, vm_ws = Pipe(qd, qv), Pipe(qv, qd)
            seen = []

            async def connect(url, headers):
                self.counts["connect"] += 1
                seen.append((url, headers))
                return dev_ws
            b = self.bridge(connect)
            task = asyncio.ensure_future(b.run())
            vm = FakeVm(vm_ws)
            await vm.handshake()
            req = await vm.accept_control_stream()
            results["path"] = req.value.path
            reg = await vm.next_message()
            results["register"] = reg
            await vm.send_message({"type": "res", "id": reg["id"], "ok": True})
            invokes = [("i1", "opentv.catalogue_list", {"limit": 2, "offset": 2}),
                       ("i2", "system.run", {"command": "cat /etc/shadow"}),
                       ("i3", "file.read", {"path": "/var/lib/opentv-muse/sdk_token"}),
                       ("i4", "file.write", {"path": "/tmp/x", "data_base64": "eA=="}),
                       ("i5", "device.health", {}),
                       ("i6", "opentv.search", {"query": "https://evil.example"}),
                       ("i7", "opentv.feedback", {"id": "films/101", "action": "like"}),
                       ("i8", "opentv.present_title", {"id": "films/106", "text": "Grand spectacle ce soir."}),
                       ("i9", "opentv.remote_control", {"key": "power"}),
                       ("i10", "opentv.play_title", {"id": "https://cdn.example/x.m3u8"})]
            for iid, cmd, params in invokes:
                await vm.send_message({"method": "link.invoke", "id": iid, "command": cmd, "params": params,
                                       "timeout_ms": 5000})
            got = {}
            while len(got) < len(invokes):
                m = await asyncio.wait_for(vm.next_message(), 10)
                got[m["id"]] = m
            results["invokes"] = got
            results["registered_status"] = json.loads(read(self.status))
            await vm.send_message({"type": "evt", "event": "link.unpaired"})
            results["end"] = await asyncio.wait_for(task, 10)
            results["connect"] = seen
        with contextlib.redirect_stdout(out):
            asyncio.run(scenario())
        reg = results["register"]
        self.assertEqual(results["path"], "/link-control")
        self.assertEqual(reg["method"], "link.register")
        self.assertEqual(sorted(reg["params"]["commands_v2"]), EXPECTED)
        for bad in ("system.run", "file.read", "file.write", "device.health"):
            self.assertNotIn(bad, json.dumps(reg))
        inv = results["invokes"]
        self.assertTrue(inv["i1"]["ok"], inv["i1"])
        self.assertEqual(inv["i1"]["payload"]["offset"], 2)
        self.assertEqual(len(inv["i1"]["payload"]["items"]), 2)
        for iid in ("i2", "i3", "i4", "i5"):
            self.assertEqual((inv[iid]["ok"], inv[iid]["error"]), (False, "unknown_command"), iid)
        for iid in ("i6", "i9", "i10"):
            self.assertEqual((inv[iid]["ok"], inv[iid]["error"]), (False, "invalid_params"), iid)
        self.assertTrue(inv["i7"]["ok"], inv["i7"])                      # goût explicite : par l'Agent Port
        self.assertTrue(inv["i8"]["ok"], inv["i8"])                      # proposition : action TV en file
        self.assertEqual(inv["i8"]["payload"]["action"]["state"], "queued")
        self.assertEqual(self.fx.ctx.control, [("muse-device", "present_title",
                                                {"id": "films/106", "text": "Grand spectacle ce soir."})])
        self.assertEqual(results["registered_status"]["state"], "registered")
        self.assertEqual(results["end"], "unpaired")
        self.assertFalse(os.path.exists(os.path.join(self.st.dir, "pairing.json")))
        self.assertEqual(results["connect"][0][1], {"Authorization": "Bearer " + VM_TOKEN})
        # seules les commandes valides atteignent l'Agent Port ; les refus n'y arrivent jamais
        tools_called = [x["tool"] for x in self.fx.audit_records()]
        self.assertEqual(sorted(tools_called), ["catalogue_list", "feedback", "present_title"])
        # aucun secret dans les journaux (SDK, appareil, VM, Agent Port), l'audit ni le statut
        blob = logs.getvalue() + out.getvalue() + read(self.fx.audit_path) + read(self.status)
        self.assertIn("muse_invoke", out.getvalue())
        for secret in (SDK_TOKEN, ACCESS, REFRESH, VM_TOKEN, self.fx.token):
            self.assertNotIn(secret, blob)
        for leaked in ("cat /etc/shadow", "/var/lib/opentv-muse/sdk_token", "evil.example"):
            self.assertNotIn(leaked, blob)
        for mod in musebridge.FORBIDDEN_MODULES:
            self.assertNotIn(mod, sys.modules)

    def test_unpaired_bridge_never_connects(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = asyncio.run(self.bridge(connect=self.fail_connect).run())
        self.assertEqual(res, "not_paired")
        self.assertEqual(self.counts, {"fetch": 0, "connect": 0, "refresh": 0})
        self.assertEqual(json.loads(read(self.status))["state"], "not_paired")

    async def fail_connect(self, url, headers):
        self.counts["connect"] += 1
        raise AssertionError("connect")

    def test_identity_without_exact_scopes_or_revoked_is_fatal_before_any_muse_connection(self):
        self.st.pairing()
        for scopes in (("read",), ("feedback", "read"), ("display", "feedback", "read"), ("control", "read")):
            want = "bridge_paused"   # refus serveur (Agent Port) ; le contrôle local reste en défense en profondeur
            self.fx.issue(scopes=scopes)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(self.bridge(connect=self.fail_connect).run()), want)
        self.fx.reg.revoke(musebridge.MUSE_AGENT_ID)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(self.bridge(connect=self.fail_connect).run()), "unauthorized")
        os.remove(self.fx.client)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(self.bridge(connect=self.fail_connect).run()), "bridge_identity_missing")
        self.assertEqual((self.counts["fetch"], self.counts["connect"]), (0, 0))
        self.assertTrue(os.path.exists(os.path.join(self.st.dir, "pairing.json")))

        class WrongScopes:   # Agent Port plus ancien (sans pause serveur) : le pont refuse quand même
            def identity_scopes(self):
                return ["display", "feedback", "read"]
        b = self.bridge(connect=self.fail_connect)
        b.port = WrongScopes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(b.run()), "identity_scope_mismatch")
        self.assertEqual((self.counts["fetch"], self.counts["connect"]), (0, 0))

    def test_malformed_pairing_is_refused_without_connecting(self):
        self.st.write("pairing.json", {"access_token": ACCESS, "noise_host": "gw.example"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(self.bridge(connect=self.fail_connect).run()), "pairing_invalid")
        self.st.pairing()
        p = json.loads(read(os.path.join(self.st.dir, "pairing.json")))
        p["noise_host"] = "evil.example/x?y="
        self.st.write("pairing.json", p)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(self.bridge(connect=self.fail_connect).run()), "pairing_invalid")
        self.assertEqual((self.counts["fetch"], self.counts["connect"]), (0, 0))

    def test_opentv_down_waits_without_registering(self):
        self.st.pairing()
        self.fx.srv.shutdown()
        self.fx.srv.server_close()
        b = self.bridge(connect=self.fail_connect)

        async def scenario():
            t = asyncio.ensure_future(b.run())
            await asyncio.sleep(0.2)
            b.stop()
            return await asyncio.wait_for(t, 5)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(scenario()), "stopped")
        self.assertEqual((self.counts["fetch"], self.counts["connect"]), (0, 0))

    def test_cmd_run_exit_codes(self):
        cfg_env = {"OPENTV_AGENT_CLIENTS_DIR": self.fx.d}
        a = types.SimpleNamespace(env="/nonexistent/opentv.env")
        with mock.patch.dict(os.environ, cfg_env), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(musebridge.cmd_run(a, sdk=self.sdk, connect=self.fail_connect, fetch_vms=self.fetch,
                                                status_path=self.status, client_config=self.fx.client), 0)
            self.st.pairing()
            self.fx.issue(scopes=("read",))      # ancien pont en lecture seule : refus explicite, jamais à moitié
            self.assertEqual(musebridge.cmd_run(a, sdk=self.sdk, connect=self.fail_connect, fetch_vms=self.fetch,
                                                status_path=self.status, client_config=self.fx.client),
                             musebridge.EX_CONFIG)


@needs_crypto
class TestLivePauseFakeMuse(unittest.TestCase):
    """Blocage de la revue finale : une session Muse DÉJÀ enregistrée doit se mettre en pause pour de vrai dès que le
    propriétaire abaisse l'autorisation de Muse (pilotage → lecture / avis + messages), et être révoquée pour
    « Aucun accès ». Agent Port réel (socket Unix, registre, audit), faux VM Muse à chiffrement Noise réel, pont réel."""

    def setUp(self):
        self.sdk = load_sdk()
        self.st = StateEnv(self)
        self.fx = AgentPortFixture(self)
        self.fx.issue()
        self.st.write("sdk_token", SDK_TOKEN + "\n")
        self.st.pairing()
        self.status = os.path.join(self.st.dir, "run-status.json")
        self.counts = {"connect": 0}
        grace = mock.patch.object(musebridge, "PAUSE_REPLY_GRACE_S", 1.0)   # marge sous charge (CI)
        grace.start()
        self.addCleanup(grace.stop)

    def fetch(self, token, root):
        return [{"vm_url": "wss://x", "vm_auth_token": VM_TOKEN, "vm_name": "vm", "vm_id": "vm-1",
                 "is_default": True}], 200

    def lower(self, level):
        _, fp, _ = self.fx.reg.snapshot(appmod.KNOWN_AGENTS)
        self.fx.reg.apply_policy_changes({"muse": list(appmod.AGENT_LEVELS[level])}, appmod.KNOWN_AGENTS, fp)

    def live(self, level, invokes, recheck_s=musebridge.POLICY_RECHECK_S):
        """Session enregistrée, un appel autorisé, abaissement par le propriétaire, puis `invokes` (envoyés
        ensemble) -> résultats observés côté VM et côté pont."""
        out, logs = io.StringIO(), io.StringIO()
        handler = musebridge.setup_logging(logs)
        self.addCleanup(logging_reset, handler)
        res = {}

        async def scenario():
            qd, qv = asyncio.Queue(), asyncio.Queue()
            dev_ws, vm_ws = Pipe(qd, qv), Pipe(qv, qd)

            async def connect(url, headers):
                self.counts["connect"] += 1
                return dev_ws
            b = musebridge.Bridge(self.sdk, self.sdk.identity.load_or_create(), musebridge.AgentPortMCP(self.fx.client),
                                  sdk_token=SDK_TOKEN, connect=connect, fetch_vms=self.fetch,
                                  refresh=lambda *a: (None, None), status_path=self.status, sleep_scale=0.001,
                                  recheck_s=recheck_s)
            task = asyncio.ensure_future(b.run())
            vm = FakeVm(vm_ws)
            await vm.handshake()
            await vm.accept_control_stream()
            reg = await vm.next_message()
            await vm.send_message({"type": "res", "id": reg["id"], "ok": True})
            await vm.send_message({"method": "link.invoke", "id": "pre", "command": "opentv.catalogue_list",
                                   "params": {"limit": 2}, "timeout_ms": 5000})
            res["pre"] = await asyncio.wait_for(vm.next_message(), 10)
            res["registered"] = json.loads(read(self.status))["state"]
            res["seen_before"] = list(self.fx.ctx.seen)
            res["audit_before"] = len(self.fx.audit_records())
            self.lower(level)                         # « Gérer les assistants » / revoke-control : une écriture
            for iid, cmd, params in invokes:
                await vm.send_message({"method": "link.invoke", "id": iid, "command": cmd, "params": params,
                                       "timeout_ms": 5000})
            got = {}
            while len(got) < len(invokes):
                m = await asyncio.wait_for(vm.next_message(), 10)
                got[m["id"]] = m
            res["got"] = got
            # dès la première réponse refusée, AVANT la fin de session (grâce) : statut « refused » de ce processus
            # (`muse pair` ne prend jamais pour sain un pont condamné encore affiché « registered »)
            res["status_on_reply"] = json.loads(read(self.status))
            res["end"] = await asyncio.wait_for(task, 10)
            try:                                       # le pont a fermé la connexion Noise de son côté
                await asyncio.wait_for(vm.next_message(), 5)
                res["closed"] = False
            except ConnectionError:
                res["closed"] = True
            res["fatal"] = b.fatal
        with contextlib.redirect_stdout(out):
            asyncio.run(scenario())
        res["log"] = out.getvalue() + logs.getvalue()
        return res

    INVOKES = [("c", "opentv.catalogue_list", {"limit": 2}),
               ("d", "opentv.display_message", {"text": "Toujours là ?"}),
               ("f", "opentv.feedback", {"id": "films/101", "action": "watchlist"})]

    def assert_paused(self, level, res, code="bridge_paused"):
        self.assertTrue(res["pre"]["ok"], res["pre"])                     # avant : session réellement servie
        self.assertEqual(res["registered"], "registered")
        self.assertEqual(res["seen_before"], ["muse-device"] * len(res["seen_before"]))
        for iid, _cmd, _p in self.INVOKES:
            self.assertEqual((res["got"][iid]["ok"], res["got"][iid]["error"]), (False, code), iid)
        self.assertEqual((res["end"], res["fatal"], res["closed"]), (code, code, True))
        self.assertEqual(self.counts["connect"], 1)                       # jamais réenregistré
        early = res["status_on_reply"]
        self.assertEqual((early["state"], early["reason"], early["pid"]), ("refused", code, os.getpid()))
        st = json.loads(read(self.status))
        self.assertEqual((st["state"], st["reason"]), ("refused", code))
        # aucun effet : ni bannière, ni goût, ni action TV, ni « actif » après l'abaissement
        self.assertEqual((self.fx.ctx.messages, self.fx.ctx.control), ([], []))
        self.assertEqual(self.fx.fy.cli(["history", "--list", "watchlist"])[1].get("items", []), [])
        self.assertEqual(self.fx.ctx.seen, res["seen_before"])
        # audit : identité authentifiée conservée, chaque appel transmis après l'abaissement refusé
        after = self.fx.audit_records()[res["audit_before"]:]
        self.assertTrue(after)
        self.assertEqual({(x["agent"], x["ok"], x["error"]) for x in after}, {("muse-device", False, code)})
        self.assertNotIn("display_message", {x["tool"] for x in self.fx.audit_records() if x["ok"]})
        self.assertIn("muse_bridge_paused", res["log"])
        blob = res["log"] + read(self.fx.audit_path) + read(self.status)
        for secret in (SDK_TOKEN, ACCESS, REFRESH, VM_TOKEN, self.fx.token):
            self.assertNotIn(secret, blob)

    def assert_ui_paused(self, level):
        view, _, _ = self.fx.reg.snapshot(appmod.KNOWN_AGENTS)
        x = appmod.assistant_row("muse", view["muse"], None, 0)
        self.assertEqual((x["connected"], x["bridge"]), (False, {"state": "paused", "level": level}))
        page = web._agent_status_html(x)
        self.assertIn("Non : pont Muse installé mais en pause", page)
        self.assertNotIn("pas installé", page)
        self.assertIn("ne le relance pas tout seul", web._consequence(x, "control"))
        return x

    def check_lowered(self, level):
        res = self.live(level, self.INVOKES)
        self.assert_paused(level, res)
        want = list(appmod.AGENT_LEVELS[level])
        self.assertEqual(self.fx.reg.list()["muse-device"]["scopes"], want)        # toujours installé (en pause)
        self.assertEqual(self.fx.reg.authenticate("muse-device", self.fx.token), want)
        self.assert_ui_paused(level)
        # réautoriser le pilotage ne relance RIEN tout seul : identité toujours réduite ⇒ toujours en pause côté
        # serveur, pont arrêté ; seul `opentv-pi muse pair` (réémission) le rétablit
        self.lower("control")
        r = agentport.call(self.fx.sock, {"v": 1, "agent": "muse-device", "token": self.fx.token, "op": "whoami"})
        self.assertEqual(r, {"ok": False, "error": "bridge_paused"})
        self.assertEqual(self.counts["connect"], 1)
        self.assertEqual(json.loads(read(self.status))["state"], "refused")
        self.assert_ui_paused(level)

    def test_control_to_read_pauses_the_live_session(self):
        self.check_lowered("read")

    def test_control_to_full_pauses_the_live_session(self):
        self.check_lowered("full")

    def test_control_to_none_revokes_and_stops_the_live_session(self):
        res = self.live("none", self.INVOKES)
        self.assert_paused("none", res, code="unauthorized")
        self.assertNotIn("muse-device", self.fx.reg.list())                       # révoquée dans la même écriture
        self.assertIsNone(self.fx.reg.authenticate("muse-device", self.fx.token))
        view, _, _ = self.fx.reg.snapshot(appmod.KNOWN_AGENTS)
        x = appmod.assistant_row("muse", view["muse"], None, 0)
        self.assertEqual((x["connected"], x["bridge"]), (False, None))
        self.assertIn("Non, pas installé sur la boîte", web._agent_status_html(x))

    def test_idle_session_pauses_on_the_periodic_recheck(self):
        """Sans aucune commande Muse : le whoami de contrôle voit la pause et ferme la session."""
        res = self.live("full", [], recheck_s=0.05)
        self.assertTrue(res["pre"]["ok"])
        self.assertEqual((res["end"], res["fatal"], res["closed"]), ("bridge_paused", "bridge_paused", True))
        self.assertEqual(json.loads(read(self.status))["reason"], "bridge_paused")
        self.assertEqual(self.counts["connect"], 1)
        self.assertEqual(self.fx.ctx.seen, res["seen_before"])
        self.assert_ui_paused("full")

    def test_unchanged_control_keeps_the_session_live(self):
        """Témoin : la vérification périodique ne coupe jamais une session dont l'autorisation est intacte."""
        out = io.StringIO()

        async def scenario():
            qd, qv = asyncio.Queue(), asyncio.Queue()
            dev_ws, vm_ws = Pipe(qd, qv), Pipe(qv, qd)

            async def connect(url, headers):
                return dev_ws
            b = musebridge.Bridge(self.sdk, self.sdk.identity.load_or_create(), musebridge.AgentPortMCP(self.fx.client),
                                  sdk_token=SDK_TOKEN, connect=connect, fetch_vms=self.fetch,
                                  refresh=lambda *a: (None, None), status_path=self.status, sleep_scale=0.001,
                                  recheck_s=0.02)
            task = asyncio.ensure_future(b.run())
            vm = FakeVm(vm_ws)
            await vm.handshake()
            await vm.accept_control_stream()
            reg = await vm.next_message()
            await vm.send_message({"type": "res", "id": reg["id"], "ok": True})
            await asyncio.sleep(0.4)                   # ~20 vérifications
            self.assertFalse(task.done())
            await vm.send_message({"method": "link.invoke", "id": "x", "command": "opentv.display_message",
                                   "params": {"text": "Bonsoir"}, "timeout_ms": 5000})
            m = await asyncio.wait_for(vm.next_message(), 10)
            self.assertTrue(m["ok"], m)
            b.stop()
            return await asyncio.wait_for(task, 10), b.fatal
        with contextlib.redirect_stdout(out):
            self.assertEqual(asyncio.run(scenario()), ("stopped", None))
        self.assertEqual(self.fx.ctx.messages[-1][:2], ("muse-device", "Bonsoir"))


def logging_reset(handler):
    import logging
    logging.getLogger().removeHandler(handler)


@needs_crypto
class TestPairEntry(unittest.TestCase):
    def setUp(self):
        self.sdk = load_sdk()
        self.st = StateEnv(self)
        self.calls = []
        pm = self.sdk.pair_module()
        self.pm = pm

        def fake_pair(on_open, force, timeout_s):
            self.calls.append((force, timeout_s))
            on_open(pm.SetupWindow("MuseGadgetABCDEF", timeout_s, has_sdk_token=True))
            return pm.PairResult(pm.PairOutcome.PAIRED)
        self.fake_pair = fake_pair
        self.a = types.SimpleNamespace(timeout=600)

    def status(self):
        p = os.path.join(self.st.dir, musebridge.PAIR_STATUS_NAME)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        return json.loads(read(p))

    def run_pair(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = musebridge.cmd_pair(self.a, sdk=self.sdk, pair_fn=self.fake_pair)
        return code, out.getvalue()

    def test_no_or_invalid_token_never_opens_a_window(self):
        self.assertEqual(self.run_pair()[0], 2)
        self.assertEqual(self.status()["state"], "no_sdk_token")
        self.st.write("sdk_token", "mgst_tooshort\n")
        self.assertEqual(self.run_pair()[0], 2)
        self.assertEqual(self.status()["state"], "invalid_sdk_token")
        self.assertEqual(self.calls, [])

    def test_pair_window_reports_ble_name_and_never_the_token(self):
        self.st.write("sdk_token", SDK_TOKEN + "\n")
        code, out = self.run_pair()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [(False, 600)])
        self.assertEqual(self.status(), {"state": "paired"})
        self.assertIn("MuseGadgetABCDEF", out)
        self.assertNotIn(SDK_TOKEN, out + read(os.path.join(self.st.dir, musebridge.PAIR_STATUS_NAME)))

    def test_already_paired_is_left_alone(self):
        self.st.write("sdk_token", SDK_TOKEN + "\n")
        self.st.pairing()
        self.assertEqual(self.run_pair()[0], 0)
        self.assertEqual(self.status()["state"], "already_paired")
        self.assertEqual(self.calls, [])

    def test_env_token_is_dropped_and_root_is_refused(self):
        with mock.patch.dict(os.environ, {"MUSEGADGET_SDK_TOKEN": SDK_TOKEN}), \
                contextlib.redirect_stdout(io.StringIO()):
            musebridge._prepare_env()
            self.assertNotIn("MUSEGADGET_SDK_TOKEN", os.environ)
        out = io.StringIO()
        with mock.patch.object(musebridge.os, "geteuid", return_value=0), contextlib.redirect_stdout(out):
            self.assertEqual(musebridge.main(["run"]), musebridge.EX_CONFIG)
        self.assertIn("run_as_root_refused", out.getvalue())


# ------------------------------------------------------------------------------------------- CLI root (status/pair/unpair)
class FakePw:
    def __init__(self, name, uid, gid):
        self.pw_name, self.pw_uid, self.pw_gid, self.pw_dir = name, uid, gid, "/"


class FakeGroups:
    def __init__(self, members):
        self.members = members

    def getgrnam(self, name):
        if name not in self.members:
            raise KeyError(name)
        return types.SimpleNamespace(gr_name=name, gr_mem=self.members[name])


class FakeBridgeUnit:
    """opentv-muse.service simulé (systemd + processus du pont), horloge factice du test :
    - démarrage : ConditionPathExists=pairing.json ; le processus relit SON fichier client et l'Agent Port (registre
      réel : identité ∩ plafond de Muse) -> statut « registered » avec son pid, sinon « refused » puis exit 78 ;
    - `stop --no-block` : fin du processus après `stop_delay` (deactivating entre-temps ; `hang_stop` : jamais) ;
    - `doom(after)` : ancien processus en pause qui sortira tout seul (exit 78) plus tard, quoi qu'il arrive ;
    - `enable --now` / `try-restart` : sémantique systemd (rien si déjà actif / rien si arrêté).
    `overlap` : un démarrage demandé alors qu'un ancien processus vivait encore (ce que `muse pair` doit exclure)."""

    def __init__(self, test):
        self.t, self.active, self.pid, self.enabled = test, "inactive", None, False
        self.result, self.exit_status, self.next_pid = "success", None, 2000
        self.exit_at = self.stop_at = None
        self.stop_delay, self.hang_stop, self.crash_next = 0.0, False, None
        self.overlap, self.spawned = False, []

    def status_path(self):
        return self.t.j(musebridge.RUN_STATUS)

    def write_status(self, state, **extra):
        fsutil.atomic_write_path(self.status_path(), json.dumps(dict({"v": 1, "state": state, "pid": self.pid,
                                                                       "since": self.t._t}, **extra)))

    def gone(self, code):
        """Fin du processus : RuntimeDirectory retiré (statut effacé), failed si code non nul."""
        self.pid, self.exit_at, self.stop_at = None, None, None
        self.active, self.exit_status = ("failed", code) if code else ("inactive", 0)
        self.result = "exit-code" if code else "success"
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.status_path())

    def tick(self):
        now = self.t._t
        if self.pid is not None and self.exit_at is not None and now >= self.exit_at:
            self.gone(78)
        if self.pid is not None and self.stop_at is not None and now >= self.stop_at:
            self.gone(0)

    def spawn(self):
        if self.pid is not None:
            self.overlap = True
            return
        if not os.path.exists(self.t.j("/var/lib/opentv-muse/pairing.json")):
            self.active = "inactive"   # condition non remplie : jamais lancé
            return
        self.next_pid += 1
        self.pid, self.active, self.result, self.exit_status = self.next_pid, "active", "success", None
        self.spawned.append(self.pid)
        if self.crash_next is not None:
            code, self.crash_next = self.crash_next, None
            self.gone(code)
            return
        reg = agents.Registry(self.t.cfg.registry_file)
        client = os.path.join(self.t.cfg.agent_clients_dir, agents.client_file_name(musebridge.MUSE_AGENT_ID,
                                                                                    os.getuid()))
        try:
            tok = json.loads(read(client))["token"]
        except (OSError, ValueError, KeyError):
            tok = None
        scopes = reg.authenticate(musebridge.MUSE_AGENT_ID, tok) if tok else None
        cap = agents.ceiling(reg.policy(), musebridge.MUSE_AGENT_ID) or []
        eff = sorted(set(scopes or ()) & set(cap))
        if scopes is None or agents.linked_paused(musebridge.MUSE_AGENT_ID, eff):
            self.write_status("refused", reason="unauthorized" if scopes is None else "bridge_paused")
            self.exit_at = self.t._t + 0.5
            return
        self.write_status("registered")

    def doom(self, after, refused=True):
        """Ancien processus ayant vu la pause : sortira (exit 78) dans `after` s ; statut « refused » publié aussitôt
        (pont corrigé) ou encore « registered » (refus en vol, pas encore publié)."""
        assert self.pid is not None
        self.exit_at = self.t._t + after
        if refused:
            self.write_status("refused", reason="bridge_paused")

    def show(self):
        sub = {"active": "running", "deactivating": "stop-sigterm", "failed": "failed"}.get(self.active, "dead")
        return "\n".join(["LoadState=loaded", "ActiveState=%s" % self.active, "SubState=%s" % sub,
                          "MainPID=%d" % (self.pid or 0), "Result=%s" % self.result,
                          "ExecMainStatus=%d" % (self.exit_status or 0),
                          "UnitFileState=%s" % ("enabled" if self.enabled else "disabled")]) + "\n"

    def run(self, verb, opts):
        self.tick()
        if verb == "show":
            return 0, self.show()
        if verb == "is-active":
            return (0 if self.active == "active" else 3), self.active + "\n"
        if verb == "is-enabled":
            return (0 if self.enabled else 1), ("enabled" if self.enabled else "disabled") + "\n"
        if verb in ("enable", "disable"):
            self.enabled = verb == "enable"
            if verb == "enable" and "--now" in opts and self.active != "active":
                self.spawn()
            if verb == "disable" and "--now" in opts:
                return self.run("stop", [])
            return 0, ""
        if verb == "reset-failed":
            if self.active == "failed":
                self.active, self.result, self.exit_status = "inactive", "success", None
            return 0, ""
        if verb == "stop":
            if self.pid is not None and self.stop_at is None:
                self.active = "deactivating"
                self.stop_at = float("inf") if self.hang_stop else self.t._t + self.stop_delay
                self.tick()
            return 0, ""
        if verb == "start":
            if self.active == "active":
                return 0, ""
            self.spawn()
            return 0, ""
        if verb == "try-restart":
            if self.active == "active":
                self.gone(0)
                self.spawn()
            return 0, ""
        return 0, ""


class AdminBase(unittest.TestCase):
    """Racine de test + faux comptes/systemctl pour opentv-pi muse (aucun test ici : réutilisée ailleurs)."""

    def setUp(self):
        base = os.path.realpath(tempfile.mkdtemp(prefix="otvadm"))
        self.addCleanup(shutil.rmtree, base, True)
        self.r = os.path.join(base, "root")
        self.j = lambda *a: os.path.join(self.r, *[x.lstrip("/") for x in a])   # noqa: E731
        os.makedirs(self.j("/var/lib/opentv-muse"), mode=0o700)
        os.chmod(self.j("/var/lib/opentv-muse"), 0o700)
        os.makedirs(self.j("/var/lib/opentv"), mode=0o700)
        os.makedirs(self.j("/etc/opentv/agents"))
        os.chmod(self.j("/etc/opentv/agents"), 0o755)
        os.makedirs(self.j("/run/opentv-muse"))
        os.makedirs(self.j("/etc/bluetooth"))
        sdk = os.path.join(base, "sdk")
        shutil.copytree(VENDOR, sdk, ignore=shutil.ignore_patterns("__pycache__"))
        pin = musebridge.load_pin(sdk)
        wheel = os.path.join(sdk, pin["dependencies"]["websockets"]["file"])
        with open(wheel, "wb") as f:
            f.write(b"fake wheel for tests")
        pin["dependencies"]["websockets"]["sha256"] = sha(wheel)
        with open(os.path.join(sdk, "PIN.json"), "w") as f:
            json.dump(pin, f)
        self.sdk = sdk
        self.cfg = config.Config({"OPENTV_STATE_DIR": self.j("/var/lib/opentv"),
                                  "OPENTV_AGENT_CLIENTS_DIR": self.j("/etc/opentv/agents"),
                                  "OPENTV_AGENT_SOCKET": "/run/opentv/agent.sock"})
        uid, gid = os.getuid(), os.getgid()
        self.pwmap = {"opentv-muse": FakePw("opentv-muse", uid, gid), "opentv": FakePw("opentv", uid, gid)}
        self.groups = FakeGroups({"opentv-agent": ["opentv", "opentv-muse"], "bluetooth": []})
        self.calls, self.err, self.euid = [], io.StringIO(), 0
        self.window = "paired"
        for p in (mock.patch.object(cli, "_private_group", return_value=True),
                  mock.patch.object(fsutil.os, "fchown", self.record_fchown)):
            p.start()
            self.addCleanup(p.stop)
        self.chowns = []
        self.unit = FakeBridgeUnit(self)
        reg = agents.Registry(self.cfg.registry_file)
        reg.set_policy({"muse": list(CONTROL), "hermit": [], "hermes": []})   # pilotage déjà accordé par Alex
        self.bt_pid = 4242
        self.prepare_bt()

    def w(self, path, text, mode=0o644):
        q = self.j(path)
        os.makedirs(os.path.dirname(q), exist_ok=True)
        with open(q, "w") as f:
            f.write(text)
        os.chmod(q, mode)
        return q

    def prepare_bt(self, battery=True, started_after=True, conf="[General]\n\n[GATT]\n#ExchangeMTU = 517\n"):
        """BlueZ Debian (main.conf + bluetooth.service), compatibilité appliquée par musebluez, bluetoothd en cours."""
        self.w("/etc/bluetooth/main.conf", conf)
        self.w("/lib/systemd/system/bluetooth.service",
               "[Unit]\nDescription=Bluetooth service\n\n[Service]\nType=dbus\nBusName=org.bluez\n"
               "ExecStart=/usr/libexec/bluetooth/bluetoothd\n\n[Install]\nWantedBy=bluetooth.target\n")
        musebluez.BlueZ(self.r).apply()
        self.fake_bluetoothd(battery, started_after)

    def fake_bluetoothd(self, battery=True, started_after=True):
        args = ["/usr/libexec/bluetooth/bluetoothd"] + (["--noplugin=battery"] if battery else [])
        self.w("/proc/%d/cmdline" % self.bt_pid, "\0".join(args) + "\0")
        mtime = os.stat(self.j("/etc/bluetooth/main.conf")).st_mtime
        tck = os.sysconf("SC_CLK_TCK")
        btime = int(mtime) - 1000
        start = (mtime + 60 - btime) if started_after else (mtime - 60 - btime)
        self.w("/proc/stat", "cpu  1 2 3\nbtime %d\n" % btime)
        self.w("/proc/%d/stat" % self.bt_pid, "%d (bluetoothd) S " % self.bt_pid + " ".join(
            ["0"] * 18 + [str(int(start * tck))]) + " 0 0\n")

    def record_fchown(self, fd, uid, gid):
        self.chowns.append((uid, gid))

    def admin(self, stdin=""):
        pwd_mod = types.SimpleNamespace(getpwnam=lambda n: self.pwmap[n] if n in self.pwmap else
                                        (_ for _ in ()).throw(KeyError(n)))
        return museadmin.Admin(self.cfg, root=self.r, runner=self.runner, euid=lambda: self.euid, pwd_mod=pwd_mod,
                               grp_mod=self.groups, sdk_dir=self.sdk, stdin=io.StringIO(stdin),
                               prompt=lambda *_: (_ for _ in ()).throw(AssertionError("no tty")), err=self.err,
                               sleep=self.fake_sleep, clock=self.fake_clock, trusted_uids=(0, os.getuid()))

    _t = 0.0

    def fake_clock(self):
        return self._t

    def fake_sleep(self, s):
        self._t += s
        self.unit.tick()
        st = os.path.join(self.j("/var/lib/opentv-muse"), musebridge.PAIR_STATUS_NAME)
        if self.window and os.path.exists(st) and json.loads(read(st))["state"] == "open":
            fsutil.atomic_write_path(st, json.dumps({"state": self.window}))
            if self.window == "paired":
                fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/pairing.json"), json.dumps(
                    {"access_token": ACCESS, "refresh_token": REFRESH}))

    def runner(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "start", "--no-block"] and argv[3] == museadmin.PAIR_UNIT:
            fsutil.atomic_write_path(os.path.join(self.j("/var/lib/opentv-muse"), musebridge.PAIR_STATUS_NAME),
                                     json.dumps({"state": "open", "ble_name": "MuseGadget0A0B0C", "timeout_s": 600}))
        if argv[0] == "systemctl" and argv[-1] == museadmin.UNIT:
            return self.unit.run(argv[1], argv[2:-1])
        if argv[1:5] == ["show", "-p", "MainPID", "--value"]:
            return 0, "%d\n" % self.bt_pid
        if argv[1] == "is-active":
            return 0, "activating\n"
        if argv[1] == "is-enabled":
            return 1, "disabled\n"
        return 0, ""

    def pair(self, stdin=SDK_TOKEN + "\n", **kw):
        adm = self.admin(stdin)
        kw.setdefault("token_stdin", True)
        return adm.pair(**kw)

    def cmds(self):
        return [" ".join(c[1:]) for c in self.calls if c[1] not in ("is-active", "is-enabled", "show")]


class TestAdmin(AdminBase):
    def test_non_root_and_missing_prerequisites_are_refused_before_any_write(self):
        self.euid = 1000
        self.assertEqual(self.pair()[0]["error"], "root_required")
        self.euid = 0
        os.remove(os.path.join(self.sdk, "musegadget", "streaming.py"))
        self.assertEqual(self.pair()[0]["error"], "sdk_unverified")
        shutil.copy(os.path.join(VENDOR, "musegadget", "streaming.py"), os.path.join(self.sdk, "musegadget"))
        wheel = os.path.join(self.sdk, "websockets-17.1-py3-none-any.whl")
        os.rename(wheel, self.sdk + ".whl")                  # hors du dossier (tout fichier en trop y est refusé)
        self.assertEqual(self.pair()[0]["error"], "dependency_missing")
        os.rename(self.sdk + ".whl", wheel)
        os.makedirs(self.j("/etc/systemd/system"), exist_ok=True)
        with open(self.j("/etc/systemd/system/musegadget.service"), "w") as f:
            f.write("[Service]\nExecStart=/opt/musegadget/venv/bin/musegadget run\n")
        r, code = self.pair()
        self.assertEqual((r["error"], code), ("stock_musegadget_present", 2))
        os.remove(self.j("/etc/systemd/system/musegadget.service"))
        os.symlink("/dev/null", self.j("/etc/systemd/system/musegadget.service"))   # masque OpenTV : accepté
        self.groups.members["opentv-agent"] = ["opentv"]
        self.assertEqual(self.pair()[0]["error"], "muse_user_not_in_agent_group")
        self.groups.members["opentv-agent"] = ["opentv", "opentv-muse"]
        for pol in ([], ["read"], ["display", "feedback", "read"]):   # tout sauf le pilotage explicite : refus
            agents.Registry(self.cfg.registry_file).set_policy({"muse": pol, "hermit": ["read"]})
            r, code = self.pair()
            self.assertEqual((r["error"], code), ("muse_control_not_authorized", 2), pol)
            self.assertIn("authorize-control", r["note"])
        self.assertEqual(self.cmds(), [])                                 # lectures seulement (systemctl show)
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertIsNone(agents.Registry(self.cfg.registry_file).list().get(musebridge.MUSE_AGENT_ID))

    def test_token_is_validated_and_never_echoed(self):
        r, code = self.pair(stdin="mgst_wrong\n")
        self.assertEqual((r["error"], code), ("invalid_sdk_token", 2))
        r, code = self.pair(stdin="", token_stdin=False)   # pas de terminal : jamais de jeton en argument
        self.assertEqual(r["error"], "token_input_required")
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertNotIn("mgst_wrong", json.dumps(r) + self.err.getvalue())
        self.assertEqual(self.cmds(), [])

    def test_successful_pair_writes_secret_files_issues_exact_control_identity_then_enables(self):
        r, code = self.pair()
        self.assertEqual(code, 0, r)
        self.assertEqual(r["result"], "paired")
        self.assertEqual(r["agent"]["scopes"], CONTROL)
        self.assertEqual(r["link"]["state"], "registered")
        tok = self.j("/var/lib/opentv-muse/sdk_token")
        self.assertEqual(read(tok), SDK_TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(tok).st_mode), 0o600)
        client = os.path.join(self.cfg.agent_clients_dir,
                              agents.client_file_name(musebridge.MUSE_AGENT_ID, os.getuid()))
        self.assertEqual(stat.S_IMODE(os.stat(client).st_mode), 0o640)
        self.assertIn((0, os.getgid()), self.chowns)                     # root:<groupe privé de opentv-muse>
        self.assertEqual(agents.Registry(self.cfg.registry_file).list()[musebridge.MUSE_AGENT_ID]["scopes"], CONTROL)
        c = self.cmds()
        start = c.index("start --no-block " + museadmin.PAIR_UNIT)
        enable = c.index("enable " + museadmin.UNIT)
        self.assertLess(start, enable)                                   # jamais activé avant l'appariement
        self.assertLess(enable, c.index("start --no-block " + museadmin.UNIT))
        self.assertEqual((r["service"]["active"], r["registered"], r["service_started"]), ("active", True, True))
        self.assertEqual(r["link"]["pid"], self.unit.pid)                # statut écrit par le processus démarré
        self.assertIn("MuseGadget0A0B0C", self.err.getvalue())
        blob = json.dumps(r) + self.err.getvalue()
        for s in (SDK_TOKEN, ACCESS, REFRESH, read(client)):
            self.assertNotIn(s.strip(), blob)
        self.assertNotIn(json.loads(read(client))["token"], blob)

    def test_unit_that_never_runs_is_reported_after_a_grace_period(self):
        orig = self.runner
        seen = []

        def runner(argv, timeout):
            if argv[:3] == ["systemctl", "start", "--no-block"]:
                self.calls.append(list(argv))
                return 0, ""                       # condition non remplie : aucune fenêtre, aucun statut
            if argv[1] == "is-active" and argv[2] == museadmin.PAIR_UNIT:
                seen.append(self._t)
                return 3, "inactive\n"
            return orig(argv, timeout)
        self.runner = runner
        r, code = self.pair()
        self.assertEqual((r["error"], code), ("pair_unit_failed", 1))
        self.assertGreaterEqual(min(seen), 5.0)    # jamais lu juste après le démarrage (tâche en file d'attente)
        self.assertNotIn("start --no-block " + museadmin.UNIT, self.cmds())

    def test_closed_window_never_enables_the_bridge(self):
        self.window = "window_closed"
        r, code = self.pair()
        self.assertEqual((r["ok"], r["error"], code), (False, "window_closed", 1))
        self.assertNotIn("start --no-block " + museadmin.UNIT, self.cmds())
        self.assertIn("stop " + museadmin.PAIR_UNIT, self.cmds())

    def test_already_paired_keeps_pairing_and_force_restarts_the_window(self):
        self.assertEqual(self.pair()[1], 0)
        self.calls.clear()
        r, code = self.pair(stdin="")
        self.assertEqual((r["result"], code), ("already_paired", 0))
        self.assertNotIn("start --no-block " + museadmin.PAIR_UNIT, self.cmds())
        self.assertEqual(self.cmds(), [])                                # pont sain : rien interrompu
        self.calls.clear()
        r, code = self.pair(stdin="", force=True)
        self.assertEqual((r["result"], code), ("paired", 0))
        c = self.cmds()
        self.assertLess(c.index("stop --no-block " + museadmin.UNIT), c.index("disable " + museadmin.UNIT))
        self.assertLess(c.index("disable " + museadmin.UNIT), c.index("start --no-block " + museadmin.PAIR_UNIT))
        self.assertLess(c.index("start --no-block " + museadmin.PAIR_UNIT),
                        c.index("start --no-block " + museadmin.UNIT))
        self.assertEqual(self.unit.active, "active")

    def test_status_never_reads_or_prints_secrets(self):
        self.assertEqual(self.pair()[1], 0)
        fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/identity.json"), json.dumps({"mac": "02:aa:bb:cc:dd:ee"}))
        rep = self.admin().status()
        self.assertTrue(rep["paired"])
        self.assertEqual(rep["sdk_token"], "present")
        self.assertEqual(rep["identity"], {"node_id": "homelink-ccddee", "ble_name": "MuseGadgetCCDDEE"})
        self.assertEqual(rep["agent"]["scopes"], CONTROL)
        self.assertTrue(rep["agent"]["exact_scopes"])
        self.assertTrue(rep["agent"]["control_authorized"])
        self.assertNotIn("muse_control_not_authorized", rep["gates"])
        self.assertEqual(rep["commands"], EXPECTED)
        self.assertTrue(rep["sdk"]["verified"])
        self.assertEqual(rep["link"]["state"], "registered")
        client = os.path.join(self.cfg.agent_clients_dir,
                              agents.client_file_name(musebridge.MUSE_AGENT_ID, os.getuid()))
        blob = json.dumps(rep)
        for s in (SDK_TOKEN, SDK_TOKEN[:12], ACCESS, REFRESH, json.loads(read(client))["token"]):
            self.assertNotIn(s, blob)

    def test_unpair_disables_removes_pairing_and_revokes_identity(self):
        self.assertEqual(self.pair()[1], 0)
        client = os.path.join(self.cfg.agent_clients_dir,
                              agents.client_file_name(musebridge.MUSE_AGENT_ID, os.getuid()))
        token = json.loads(read(client))["token"]
        self.calls.clear()
        r, code = self.admin().unpair()
        self.assertEqual(code, 0, r)
        self.assertEqual((r["unpaired"], r["agent_revoked"], r["client_file_removed"], r["sdk_token"]),
                         (True, True, True, "kept"))
        self.assertIn("disable --now " + museadmin.UNIT, self.cmds())
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertIsNone(agents.Registry(self.cfg.registry_file).authenticate(musebridge.MUSE_AGENT_ID, token))
        r, _ = self.admin().unpair(forget_token=True)
        self.assertEqual(r["sdk_token"], "removed")
        self.euid = 1000
        self.assertEqual(self.admin().unpair()[0]["error"], "root_required")

    def test_cli_wiring(self):
        p = cli.build_parser()
        a = p.parse_args(["muse", "pair", "--token-stdin"])
        self.assertEqual((a.cmd, a.action, a.token_stdin), ("muse", "pair", True))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["muse", "pair", "--token", SDK_TOKEN])   # jamais de jeton en argument
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            museadmin.main(p.parse_args(["muse", "status"]), cfg=self.cfg, admin=self.admin())
        self.assertEqual(json.loads(out.getvalue())["commands"], EXPECTED)


# ------------------------------------------------------------------------------------------- unités et installation
def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def sh(args, env=None):
    e = dict(os.environ, LC_ALL="C")
    e.update(env or {})
    return subprocess.run(["bash"] + list(args), capture_output=True, timeout=180, env=e)


class TestUnitsAndInstall(unittest.TestCase):
    def test_bridge_unit_is_unprivileged_sandboxed_and_never_runs_unpaired(self):
        u = unit("opentv-muse.service")
        s, text = u["Service"], read(os.path.join(PI4, "systemd", "opentv-muse.service"))
        self.assertEqual(u["Unit"]["ConditionPathExists"], "/var/lib/opentv-muse/pairing.json")
        self.assertIn("musegadget.service", u["Unit"]["Conflicts"])
        self.assertEqual((s["User"], s["Group"]), ("opentv-muse", "opentv-muse"))
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-muse-bridge run")
        for k, v in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("CapabilityBoundingSet", ""),
                     ("PrivateDevices", "yes"), ("MemoryDenyWriteExecute", "yes"), ("RestartPreventExitStatus", "78"),
                     ("StateDirectoryMode", "0700"), ("UMask", "0077"), ("ProtectHome", "yes")):
            self.assertEqual(s[k], v, k)
        for p in ("/var/lib/opentv ", "/run/dbus", "/etc/opentv/tmdb", "/etc/opentv/source.env"):
            self.assertIn("-" + p.strip(), text)
        self.assertNotIn("SupplementaryGroups", s)
        self.assertNotIn("MUSEGADGET_SDK_TOKEN", text)
        self.assertNotIn("AF_BLUETOOTH", text)

    def test_pair_unit_is_bounded_and_never_installed_as_enabled(self):
        u = unit("opentv-muse-pair.service")
        self.assertNotIn("Install", u.sections())
        s = u["Service"]
        self.assertEqual((s["Type"], s["User"], s["SupplementaryGroups"]), ("oneshot", "opentv-muse", "bluetooth"))
        self.assertIn("pair --timeout 600", s["ExecStart"])
        text = read(os.path.join(PI4, "systemd", "opentv-muse-pair.service"))
        self.assertIn("ConditionPathExists=!/var/lib/opentv-muse/pairing.json", text)
        self.assertIn("ConditionPathExists=/var/lib/opentv-muse/sdk_token", text)

    def test_install_script_never_enables_or_starts_the_bridge(self):
        text = read(os.path.join(PI4, "install.sh"))
        logical = re.sub(r"\\\n", " ", "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))
        lines = [l for l in logical.splitlines() if re.search(r"systemctl (enable|start|restart|mask)\b", l)]
        self.assertTrue(any("opentv.service" in l for l in lines))
        for line in lines:
            self.assertNotIn("opentv-muse", line, line)
            self.assertNotIn("musegadget", line, line)
        self.assertIn("post systemctl try-restart opentv-muse.service", text)
        su = read(os.path.join(PI4, "system", "sysusers-opentv.conf"))
        self.assertIn("m opentv-muse opentv-agent", su)
        self.assertNotIn("m opentv-muse bluetooth", su)
        self.assertIn("d /var/lib/opentv-muse 0700 opentv-muse opentv-muse -",
                      read(os.path.join(PI4, "system", "tmpfiles-opentv.conf")))

    def root(self):
        r = tempfile.mkdtemp(prefix="otvmrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        return r

    def test_install_ships_pinned_subset_masks_stock_and_uninstall_reverts(self):
        r = self.root()
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out + p.stderr.decode())
        lib = os.path.join(r, "opt/opentv/lib/muse")
        v = musebridge.verify_sdk(lib)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["websockets"], "missing")
        self.assertIn("dépendance websockets absente", out)
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        for rel in ("/opt/opentv/lib/muse/musegadget/link_client.py", "/opt/opentv/lib/muse/PIN.json",
                    "/opt/opentv/bin/opentv-muse-bridge", "/etc/systemd/system/opentv-muse.service",
                    "/etc/systemd/system/opentv-muse-pair.service", "/opt/opentv/lib/opentv_pi/musebridge.py"):
            self.assertIn(rel, man)
        for bad in ("executor.py", "fileops.py", "musegadget/service.py", "musegadget/cli.py", "/etc/systemd/system/musegadget"):
            self.assertNotIn(bad, man)
        mask = os.path.join(r, "etc/systemd/system/musegadget.service")
        self.assertEqual(os.readlink(mask), "/dev/null")
        self.assertTrue(os.path.isfile(os.path.join(r, "var/lib/opentv-install/musegadget-masked")))
        self.assertFalse(os.path.lexists(os.path.join(r, "etc/systemd/system/multi-user.target.wants/opentv-muse.service")))
        q = subprocess.run([sys.executable, "-I", os.path.join(r, "opt/opentv/bin/opentv-muse-bridge"), "describe"],
                           capture_output=True, timeout=30)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(sorted(json.loads(q.stdout)["commands"]), EXPECTED)
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"])   # idempotent
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("déjà masqué", p.stdout.decode())
        p = sh([os.path.join(PI4, "uninstall.sh"), "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertIn("opentv-muse.service", [l for l in p.stdout.decode().splitlines() if "disable --now" in l][0])
        self.assertFalse(os.path.lexists(mask))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))
        self.assertFalse(os.path.exists(os.path.join(r, "var/lib/opentv-install/musegadget-masked")))

    def test_foreign_stock_unit_is_never_touched(self):
        r = self.root()
        stock = os.path.join(r, "etc/systemd/system/musegadget.service")
        os.makedirs(os.path.dirname(stock))
        with open(stock, "w") as f:
            f.write("[Service]\nExecStart=/opt/musegadget/venv/bin/musegadget run\n")
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("musegadget d'origine présent", p.stdout.decode())
        self.assertTrue(os.path.isfile(stock) and not os.path.islink(stock))
        self.assertEqual(sh([os.path.join(PI4, "uninstall.sh"), "--root", r]).returncode, 0)
        self.assertTrue(os.path.isfile(stock))

    def test_pinned_wheel_from_cache_is_installed_and_a_wrong_hash_is_refused(self):
        r = self.root()
        cache = os.path.join(r, "var/cache/opentv/muse")
        os.makedirs(cache)
        wheel = os.path.join(cache, "websockets-17.1-py3-none-any.whl")
        with open(wheel, "wb") as f:
            f.write(b"stand-in wheel bytes")
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SHA-256", p.stderr.decode())
        self.assertFalse(os.path.exists(os.path.join(r, "opt")))          # rien modifié
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"],
               env={"OPENTV_INSTALL_TEST_MUSE_WHEEL_SHA256": sha(wheel)})
        self.assertEqual(p.returncode, 0, p.stderr)
        inst = os.path.join(r, "opt/opentv/lib/muse/websockets-17.1-py3-none-any.whl")
        self.assertEqual(sha(inst), sha(wheel))
        self.assertIn("/opt/opentv/lib/muse/websockets-17.1-py3-none-any.whl",
                      read(os.path.join(r, "opt/opentv/MANIFEST.sha256")))
        os.remove(wheel)                                                    # cache vidé : copie installée conservée
        p = sh([os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight"],
               env={"OPENTV_INSTALL_TEST_MUSE_WHEEL_SHA256": sha(inst)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(inst))

    def test_prepare_plans_pinned_download_and_packages_without_network(self):
        from tests.test_service import fixture_root
        r = fixture_root(self)
        p = sh([os.path.join(PI4, "prepare-pi.sh"), "--root", r, "--no-install"])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out + p.stderr.decode())
        self.assertIn("[plan] télécharger websockets-17.1-py3-none-any.whl (SHA-256 %s" % WHEEL_SHA256, out)
        for pkg in ("bluez", "python3-cryptography", "python3-dbus", "python3-gi"):
            self.assertIn(pkg, out)
        self.assertFalse(os.path.exists(os.path.join(r, "var/cache/opentv")))


if __name__ == "__main__":
    unittest.main()
