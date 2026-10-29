"""Pont Muse, revue indépendante : identité dédiée « muse-device » (jamais l'identité « muse » d'un autre client),
migration d'une installation de développement, détecteur d'adresses conservateur (titres légitimes intacts), clés de
secret exactes, compatibilité BlueZ du SDK (MTU + greffon battery) réversible et transactionnelle, garde d'intégrité
du dossier SDK (masquage d'import, liens, bytecode planté), nulls facultatifs, requêtes « -x », journal borné et
expurgé, appariement raté sans identité résiduelle.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests._common import PI4, NANOPI
from tests import test_muse_bridge as tmb
from opentv_pi import agents, app as appmod, cli, foryou, fsutil, museadmin, musebluez, musebridge

MUSE_DEV = musebridge.MUSE_AGENT_ID
INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
DEBIAN_CONF = ("[General]\n#Name = BlueZ\n\n[Policy]\n#AutoEnable=true\n\n[GATT]\n# Default MTU\n"
               "#ExchangeMTU = 517\n#Channels = 3\n\n[CSIS]\n#SIRK = ''\n")
DEBIAN_UNIT = ("[Unit]\nDescription=Bluetooth service\nDocumentation=man:bluetoothd(8)\n\n[Service]\nType=dbus\n"
               "BusName=org.bluez\nExecStart=/usr/libexec/bluetooth/bluetoothd\nNotifyAccess=main\n\n"
               "[Install]\nWantedBy=bluetooth.target\nAlias=dbus-org.bluez.service\n")
LEGIT = ["Big Data: le film", "M.Butterfly", "Mr.Robot", "File: texte", "Data: texte", "Paris.Fin",
         "Mission // Impossible", "2/3", "AC/DC", "Face/Off", "Dr. Folamour", "S.W.A.T.", "Kill Bill: Vol. 1",
         "L.A. Confidential", "50/50", "Love.Me", "Just.Do.It", "9 1/2 Weeks", "Mr. & Mrs. Smith",
         "E.T. l'extra-terrestre", "Spider-Man: No Way Home", "Roméo + Juliette", "M*A*S*H", "Ça.Va", "-x",
         "--limit", "Star Wars: Episode IV"]
ADDRESSES = ["https://evil.example/x", "HTTP://X", "http:evil", "ftp://x.example", "www.evil.example",
             "evil.example/path", "evil.com", "Visite evil.com ce soir", "EVIL.CO.UK", "x.onion", "192.168.1.10",
             "10.0.0.1:8080", "localhost:8080", "localhost/admin", "//cdn.evil/x", "file:///etc/passwd",
             "file:/etc/passwd", "File:/Data: texte", "javascript:alert(1)", "data:text/html,x", "magnet:?xt=urn",
             "blob:https://x", "/etc/passwd", "/var/lib/opentv/catalogue.json", "/etc", "~/secret", "~alex/x",
             "../../etc", "./x", "C:\\Windows\\x", "\\\\serveur\\partage", "user:mot2passe@hote.example",
             "alex@example.com"]


def read(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)
    os.chmod(path, mode)
    return path


def sh(args, env=None):
    e = dict(os.environ, LC_ALL="C")
    e.update(env or {})
    return subprocess.run(["bash"] + list(args), capture_output=True, timeout=300, env=e)


def tree_bytes(root, sub):
    out = {}
    base = os.path.join(root, sub.lstrip("/"))
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = read(p, "rb")
    return out


# ------------------------------------------------------------------------------------------- identité dédiée
class TestDedicatedIdentity(tmb.AdminBase):
    def reg(self):
        return agents.Registry(self.cfg.registry_file)

    def client(self, agent=MUSE_DEV, uid=None):
        return os.path.join(self.cfg.agent_clients_dir, agents.client_file_name(agent, os.getuid() if uid is None
                                                                               else uid))

    def other_uid(self):
        return os.getuid() + 1

    def other_muse_client(self):
        """Un autre client local (ex. Muse MCP d'Alex) détient l'identité « muse »."""
        tok = self.reg().issue("muse", ["read"])
        agents.write_client_config(self.client("muse", self.other_uid()), "muse", tok, "/run/opentv/agent.sock")
        return tok

    def test_pair_failed_pair_and_unpair_never_touch_another_clients_muse_identity(self):
        other = self.other_muse_client()
        r, code = self.pair()
        self.assertEqual(code, 0, r)
        self.assertEqual(r["agent"]["id"], "muse-device")
        self.assertEqual(self.reg().authenticate("muse", other), ["read"])
        self.assertEqual(self.admin().unpair()[1], 0)
        self.assertEqual(self.reg().authenticate("muse", other), ["read"])
        self.window = "window_closed"
        self.assertEqual(self.pair(stdin="")[1], 1)
        self.assertEqual(self.reg().authenticate("muse", other), ["read"])
        self.assertTrue(os.path.isfile(self.client("muse", self.other_uid())))
        self.assertNotIn(self.client("muse"), os.listdir(self.cfg.agent_clients_dir))

    def test_failed_pairing_revokes_the_new_identity_and_removes_its_client_file(self):
        for how in ("window_closed", "start_failed", "interrupted"):
            self.calls.clear()
            orig = self.runner
            self.window = "window_closed" if how == "window_closed" else "paired"
            patches = []
            if how == "start_failed":
                def runner(argv, timeout, orig=orig):
                    if argv[:3] == ["systemctl", "start", "--no-block"]:
                        self.calls.append(list(argv))
                        return 1, ""
                    return orig(argv, timeout)
                self.runner = runner
            if how == "interrupted":
                patches.append(mock.patch.object(museadmin.Admin, "wait_pair", side_effect=KeyboardInterrupt))
            with contextlib.ExitStack() as st:
                for p in patches:
                    st.enter_context(p)
                r, code = self.pair(stdin=tmb.SDK_TOKEN + "\n")
            self.runner = orig
            self.assertFalse(r["ok"], how)
            self.assertEqual(r["identity"]["agent_revoked"], True, (how, r))
            self.assertTrue(r["identity"]["client_file_removed"], how)
            self.assertIsNone(self.reg().list().get(MUSE_DEV), how)
            self.assertFalse(os.path.exists(self.client()), how)
            self.assertNotIn("start --no-block " + museadmin.UNIT, self.cmds(), how)

    def test_existing_dedicated_identity_not_ours_fails_closed_unless_explicitly_replaced(self):
        foreign = self.reg().issue(MUSE_DEV, ["read"])               # émise hors de muse pair, sans notre fichier
        r, code = self.pair()
        self.assertEqual((r["error"], r["reason"], code), ("muse_device_identity_unexpected",
                                                           "not_issued_by_muse_pair", 2))
        self.assertIn("--replace-identity", r["note"])
        self.assertEqual(self.cmds(), [])
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertEqual(self.reg().authenticate(MUSE_DEV, foreign), ["read"])
        # détenue par un autre compte (fichier client à son nom portant le jeton actif)
        agents.write_client_config(self.client(MUSE_DEV, self.other_uid()), MUSE_DEV, foreign, "/run/x.sock")
        r, code = self.pair()
        self.assertEqual((r["reason"], r["foreign_clients"]), ("issued_to_another_account",
                                                             [{"uid": self.other_uid(), "live": True}]))
        # remplacement explicite : seule « muse-device » est révoquée, puis réémise à nous
        r, code = self.pair(replace_identity=True)
        self.assertEqual(code, 0, r)
        self.assertTrue(r["replaced_identity"]["agent_revoked"])
        self.assertIsNone(self.reg().authenticate(MUSE_DEV, foreign))
        mine = json.loads(read(self.client()))["token"]
        self.assertEqual(self.reg().authenticate(MUSE_DEV, mine), tmb.CONTROL)
        self.assertEqual(self.admin().identity_state(self.pwmap["opentv-muse"])["state"], "owned")
        self.assertTrue(os.path.isfile(self.client(MUSE_DEV, self.other_uid())))   # fichier d'autrui jamais retiré

    def test_owned_identity_with_other_scopes_is_reissued_exactly(self):
        """Ancien pont en lecture seule (prouvé à nous par le jeton de notre fichier client) : révoqué puis réémis
        avec exactement les scopes du pont, sans nouvel appariement Bluetooth ; jamais élargi au-delà du plafond."""
        old = self.reg().issue(MUSE_DEV, ["read"])
        agents.write_client_config(self.client(), MUSE_DEV, old, "/run/x.sock")
        fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/sdk_token"), (tmb.SDK_TOKEN + "\n").encode())
        fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/pairing.json"),
                                 json.dumps({"access_token": tmb.ACCESS, "refresh_token": tmb.REFRESH}))
        self.assertEqual(self.admin().identity_state(self.pwmap["opentv-muse"])["state"], "outdated")
        r, code = self.pair(stdin="")
        self.assertEqual((r["result"], code), ("already_paired", 0), r)
        self.assertNotIn("start --no-block " + museadmin.PAIR_UNIT, self.cmds())   # aucun Bluetooth
        self.assertIsNone(self.reg().authenticate(MUSE_DEV, old))
        new = json.loads(read(self.client()))["token"]
        self.assertEqual(self.reg().authenticate(MUSE_DEV, new), tmb.CONTROL)
        # plafond sans pilotage : refus avant toute écriture (jamais un pont à moitié autorisé ni --force)
        self.reg().set_policy({"muse": ["display", "feedback", "read"], "hermit": [], "hermes": []})
        r, code = self.pair(stdin="")
        self.assertEqual((r["error"], code), ("muse_control_not_authorized", 2))

    def test_unpair_revokes_only_an_identity_it_owns(self):
        foreign = self.reg().issue(MUSE_DEV, ["read"])
        r, code = self.admin().unpair()
        self.assertEqual(code, 0, r)
        self.assertEqual((r["agent_revoked"], r["agent_kept"]), (False, "not_issued_by_muse_pair"))
        self.assertIn("agent revoke --id muse-device", r["agent_note"])
        self.assertEqual(self.reg().authenticate(MUSE_DEV, foreign), ["read"])

    def test_legacy_dev_install_is_migrated_without_harming_unrelated_identities(self):
        legacy = self.reg().issue("muse", ["read"])                  # ancienne installation : « muse » au pont
        agents.write_client_config(self.client("muse"), "muse", legacy, "/run/x.sock")
        self.assertEqual(self.admin().status()["agent"]["legacy_migration"], "pending")
        r, code = self.pair()
        self.assertEqual(code, 0, r)
        self.assertEqual(r["migration"], {"legacy_client_removed": True, "legacy_identity_revoked": True})
        self.assertIsNone(self.reg().authenticate("muse", legacy))
        self.assertFalse(os.path.exists(self.client("muse")))
        # ancienne copie restée, mais « muse » réémise depuis à un autre client : son identité n'est jamais touchée
        self.assertEqual(self.admin().unpair()[1], 0)
        agents.write_client_config(self.client("muse"), "muse", legacy, "/run/x.sock")
        other = self.other_muse_client()
        r, code = self.pair()
        self.assertEqual(code, 0, r)
        self.assertEqual(r["migration"], {"legacy_client_removed": True, "legacy_identity_revoked": False})
        self.assertEqual(self.reg().authenticate("muse", other), ["read"])
        agents.write_client_config(self.client("muse"), "muse", legacy, "/run/x.sock")
        r, _ = self.admin().unpair()
        self.assertEqual(r["migration"]["legacy_client_removed"], True)
        self.assertEqual(self.reg().authenticate("muse", other), ["read"])

    def test_tv_policy_for_muse_caps_and_revokes_the_linked_identity(self):
        self.assertEqual(self.pair()[1], 0)
        tok = json.loads(read(self.client()))["token"]
        reg = self.reg()
        self.assertEqual(reg.authenticate(MUSE_DEV, tok), tmb.CONTROL)
        # plafond relu à chaque appel : Muse réduite sur la TV (écriture brute) ⇒ scopes réduits aussitôt
        data = json.loads(read(self.cfg.registry_file))
        data["policy"]["muse"] = []
        fsutil.atomic_write_path(self.cfg.registry_file, json.dumps(data).encode())
        self.assertIsNone(reg.authenticate(MUSE_DEV, tok))
        data["policy"]["muse"] = ["feedback", "read"]
        fsutil.atomic_write_path(self.cfg.registry_file, json.dumps(data).encode())
        self.assertEqual(reg.authenticate(MUSE_DEV, tok), ["feedback", "read"])     # jamais control ni display
        data["policy"]["muse"] = list(tmb.CONTROL)
        fsutil.atomic_write_path(self.cfg.registry_file, json.dumps(data).encode())
        self.assertEqual(reg.authenticate(MUSE_DEV, tok), tmb.CONTROL)
        # « Gérer les assistants » : Muse « Aucun accès » ⇒ identité du pont révoquée dans la même écriture
        _, fp, _ = reg.snapshot(appmod.KNOWN_AGENTS)
        self.assertEqual(reg.apply_policy_changes({"muse": []}, appmod.KNOWN_AGENTS, fp), {"muse": "policy"})
        self.assertIsNone(reg.list().get(MUSE_DEV))
        # QR de configuration (set_policy, revoke_exceeding) : même règle
        reg.set_policy({"muse": list(tmb.CONTROL), "hermit": [], "hermes": []})
        self.assertEqual(self.pair(stdin="", force=True)[1], 0)
        self.assertIsNotNone(reg.list().get(MUSE_DEV))
        self.assertIn(MUSE_DEV, reg.set_policy({"muse": [], "hermit": [], "hermes": []}, revoke_exceeding=True))
        # jamais émise par `agent issue` : identité réservée au pont
        with mock.patch.dict(os.environ, {"OPENTV_STATE_DIR": self.j("/var/lib/opentv"),
                                          "OPENTV_AGENT_CLIENTS_DIR": self.j("/etc/opentv/agents")}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["--env", "/nonexistent.env", "agent", "issue", "--id", MUSE_DEV, "--scopes", "read"])
        self.assertEqual((code, json.loads(out.getvalue())["error"]), (2, "reserved_agent_id"))

    def test_status_reports_the_dedicated_identity_and_bluetooth_without_secrets(self):
        self.assertEqual(self.pair()[1], 0)
        rep = self.admin().status()
        self.assertEqual((rep["agent"]["id"], rep["agent"]["ceiling_from"], rep["agent"]["client_file"]),
                         ("muse-device", "muse", True))
        self.assertTrue(rep["bluetooth"]["ready"], rep["bluetooth"])
        self.assertNotIn(json.loads(read(self.client()))["token"], json.dumps(rep))


# ------------------------------------------------------------------------------------------- adresses et clés
class TestAddressesAndKeys(unittest.TestCase):
    def test_legitimate_titles_are_kept_and_searchable(self):
        for t in LEGIT:
            self.assertFalse(musebridge.has_address(t), (t, musebridge.ADDRESS_RE.search(t)))
            self.assertEqual(musebridge.scrub({"title": t}), {"title": t}, t)
            if len(t) <= 80:
                self.assertEqual(musebridge.validate_params("opentv.search", {"query": t})["query"], t)

    def test_addresses_paths_and_credentials_are_refused_and_scrubbed(self):
        for t in ADDRESSES:
            self.assertTrue(musebridge.has_address(t), t)
            with self.assertRaises(musebridge.BridgeError, msg=t):
                musebridge.validate_params("opentv.search", {"query": t})
            out = musebridge.scrub({"description": "Avant %s après" % t})["description"]
            self.assertFalse(musebridge.has_address(out), (t, out))
            self.assertNotIn(t, out)
        self.assertEqual(musebridge.scrub({"title": "Voir https://evil.example/x ce film"})["title"], "Voir ce film")

    def test_detector_is_at_least_as_strict_as_the_catalogue_engine(self):
        eng, _ = foryou.load_engine(NANOPI)
        corpus = LEGIT + ADDRESSES + ["Résumé sur www.spam.example/page", "img.example/1.jpg", "a.b.c.d/x",
                                      "1.2.3.4/p", "//x", "Mission // Impossible", "data:,x"]
        for t in corpus:
            if eng.URLISH_RE.search(t) or eng.URL_RE.search(t):
                self.assertTrue(musebridge.has_address(t), t)
        for t in LEGIT:                                      # et le moteur garde lui aussi ces titres
            self.assertIsNone(eng.URLISH_RE.search(t), t)

    def test_secret_keys_are_exact_segments(self):
        kept = ("author", "during", "favourites", "security", "title", "genres", "pathe", "linked", "sources",
                "authority", "imdb_rating", "description")
        dropped = ("token", "secret", "password", "passwd", "auth", "url", "uri", "href", "link", "poster", "path",
                   "src", "socket", "access_token", "refresh_token", "poster_url", "detail_path", "api_key",
                   "authorization", "cookie", "psk", "noise_host", "api_url_v2", "mac", "ssid")
        res = musebridge.scrub({k: "x" for k in kept + dropped})
        self.assertEqual(sorted(res), sorted(kept))


# ------------------------------------------------------------------------------------------- intégrité du SDK
class TestSdkIntegrity(unittest.TestCase):
    def copy(self):
        d = tmb.tmpdir(self)
        dst = os.path.join(d, "muse")
        shutil.copytree(tmb.VENDOR, dst, ignore=shutil.ignore_patterns("__pycache__"))
        self.assertTrue(musebridge.verify_sdk(dst)["ok"])
        return d, dst

    def test_import_shadowing_files_dirs_and_symlinks_fail_closed(self):
        cases = {
            "websockets/": lambda dst: write(os.path.join(dst, "websockets", "__init__.py"), "x = 1\n"),
            "ssl.py": lambda dst: write(os.path.join(dst, "ssl.py"), "raise SystemExit\n"),
            "evil.pth": lambda dst: write(os.path.join(dst, "evil.pth"), "import os\n"),
            "symlink:musegadget/plug": lambda dst: os.symlink("/tmp", os.path.join(dst, "musegadget", "plug")),
            "symlink:extra": lambda dst: os.symlink(os.path.join(dst, "musegadget"), os.path.join(dst, "extra")),
            "symlink:LICENSE": lambda dst: (os.rename(os.path.join(dst, "LICENSE"), dst + ".lic"),
                                            os.symlink(dst + ".lic", os.path.join(dst, "LICENSE"))),
            "symlink:__pycache__": lambda dst: os.symlink("/tmp", os.path.join(dst, "__pycache__")),
            "musegadget/noise/sub/": lambda dst: write(os.path.join(dst, "musegadget", "noise", "sub", "a.py"), ""),
            "symlink:musegadget/noise/framing.py": lambda dst: (
                os.rename(os.path.join(dst, "musegadget", "noise", "framing.py"), dst + ".f"),
                os.symlink(dst + ".f", os.path.join(dst, "musegadget", "noise", "framing.py"))),
            "unexpected:websockets-0.0-py3-none-any.whl": lambda dst: write(
                os.path.join(dst, "websockets-0.0-py3-none-any.whl"), b"x"),
            "special:fifo": lambda dst: os.mkfifo(os.path.join(dst, "fifo")),
        }
        for want, plant in cases.items():
            _, dst = self.copy()
            plant(dst)
            v = musebridge.verify_sdk(dst)
            self.assertFalse(v["ok"], want)
            self.assertTrue(any(p.startswith(want.rstrip("/")) or p == "unexpected:" + want for p in v["problems"]),
                            (want, v["problems"]))
            with self.assertRaises(musebridge.BridgeError):
                musebridge.load_sdk(dst, require_websockets=False)
            self.assertNotIn(dst, sys.path)
        # __pycache__ réels (jamais lus) et la roue épinglée restent admis
        _, dst = self.copy()
        os.makedirs(os.path.join(dst, "__pycache__"))
        os.makedirs(os.path.join(dst, "musegadget", "__pycache__"))
        self.assertTrue(musebridge.verify_sdk(dst)["ok"], musebridge.verify_sdk(dst)["problems"])

    @tmb.needs_crypto
    def test_planted_bytecode_is_never_executed(self):
        _, dst = self.copy()
        src = os.path.join(dst, "musegadget", "config.py")
        evil = os.path.join(os.path.dirname(dst), "evil_config.py")
        write(evil, read(src) + "\nPLANTED = 'oui'\n")
        # chemin __pycache__ classique (sans le préfixe éventuellement posé par un load_sdk antérieur de ce processus)
        cfile = os.path.join(os.path.dirname(src), "__pycache__", "config.%s.pyc" % sys.implementation.cache_tag)
        os.makedirs(os.path.dirname(cfile), exist_ok=True)
        py_compile.compile(evil, cfile=cfile, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        probe = ("import sys; sys.path.insert(0, %r)\n" % PI4 +
                 "from opentv_pi import musebridge\n"
                 "if sys.argv[2] == 'guard':\n    musebridge.load_sdk(sys.argv[1], require_websockets=False)\n"
                 "else:\n    sys.path.insert(0, sys.argv[1])\n"
                 "import musegadget.config as c\nprint(getattr(c, 'PLANTED', 'propre'))\n")
        script = os.path.join(os.path.dirname(dst), "probe.py")
        write(script, probe)
        run = lambda mode: subprocess.run([sys.executable, "-I", script, dst, mode], capture_output=True,  # noqa: E731
                                          timeout=60)
        p = run("plain")                                     # contrôle de mutation : sans le garde, il serait exécuté
        self.assertEqual(p.stdout.decode().strip(), "oui", p.stderr)
        p = run("guard")
        self.assertEqual(p.stdout.decode().strip(), "propre", p.stderr)


# ------------------------------------------------------------------------------------------- nulls et requêtes « - »
class TestParamsThroughAgentPort(unittest.TestCase):
    def setUp(self):
        self.fx = tmb.AgentPortFixture(self)
        self.fx.issue()
        self.port = musebridge.AgentPortMCP(self.fx.client)

    def call(self, cmd, params=None):
        return musebridge.dispatch(self.port, cmd, params)

    def test_null_optional_params_are_omitted_required_and_unknown_are_not(self):
        r = self.call("opentv.catalogue_list", {"limit": None, "offset": None})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["payload"]["limit"], r["payload"]["offset"]), (20, 0))
        self.assertTrue(self.call("opentv.search", {"query": "nuit", "limit": None, "offset": None})["ok"])
        self.assertTrue(self.call("opentv.history", {"list": None})["ok"])
        self.assertTrue(self.call("opentv.recommend", {"limit": None})["ok"])
        before = len(self.fx.audit_records())
        for cmd, params in (("opentv.search", {"query": None}), ("opentv.search", {"query": "x", "bogus": None}),
                            ("opentv.status", {"x": None}), ("opentv.search", {"query": "   "})):
            self.assertEqual(self.call(cmd, params), {"ok": False, "error": "invalid_params"}, params)
        self.assertEqual(len(self.fx.audit_records()), before)

    def test_dash_queries_are_literal_titles_never_usage_errors(self):
        code, out = self.fx.fy.cli(["search", "--query", "-x"])   # forme d'avant : erreur « usage » atteignable
        self.assertEqual((code, out["error"]), (2, "usage"))
        for q in ("-x", "--limit", "-1", "--query=nuit", "--help", "-"):
            r = self.call("opentv.search", {"query": q})
            self.assertTrue(r["ok"], (q, r))
            self.assertEqual(r["payload"]["query"], q)
        self.assertNotIn("usage", json.dumps(self.fx.audit_records()))

    def test_engine_usage_error_is_mapped_to_a_stable_code(self):
        def fake_call(sock, req, timeout):
            if req.get("op") == "whoami":
                return {"ok": True, "result": {"agent": MUSE_DEV, "scopes": ["read"]}}
            return {"ok": False, "error": "usage"}
        port = musebridge.AgentPortMCP(self.fx.client, call=fake_call)
        self.assertEqual(musebridge.dispatch(port, "opentv.search", {"query": "nuit"}),
                         {"ok": False, "error": "invalid_params"})


# ------------------------------------------------------------------------------------------- journal
class TestLogging(unittest.TestCase):
    SECRET = "dev-access-SECRET-0123456789abcdef"

    def setUp(self):
        self.out = io.StringIO()
        self.h = musebridge.setup_logging(self.out)
        self.addCleanup(tmb.logging_reset, self.h)
        musebridge.register_secret(self.SECRET)
        self.log = logging.getLogger("musegadget.link_client")

    def lines(self):
        return self.out.getvalue().split("\n")

    def test_attacker_command_names_and_reset_reasons_are_bounded_and_neutralised(self):
        huge = "\x1b]0;pwn\x07" + "A" * (5 * 1024 * 1024) + "\nmusegadget INFO forged\u202e" + self.SECRET
        self.log.info("invoke %s", huge)
        self.log.warning("control stream reset: %s", "\x1b[2J\r\n" + "B" * 100000 + "\u2028fake")
        self.log.info("control connection closed: %s", ConnectionError("closed \x00\x1b " + "C" * 10 ** 6))
        text = self.out.getvalue()
        self.assertEqual(text.count("\n"), 3)
        for line in self.lines()[:3]:
            self.assertLess(len(line), musebridge.MAX_LOG_MSG + 64)
        for bad in ("\x1b", "\x07", "\r", "\u202e", "\u2028", "\x00", self.SECRET, "\nmusegadget INFO forged"):
            self.assertNotIn(bad, text)
        self.assertIn("invoke ?]0;pwn?AAAA", text)

    def test_tracebacks_are_rendered_redacted_and_bounded(self):
        try:
            raise ValueError("boom " + self.SECRET + " " + "D" * 50000)
        except ValueError:
            self.log.exception("invoke failed")
        try:
            raise RuntimeError("x\x1b[31m" + self.SECRET)
        except RuntimeError:
            logging.getLogger("asyncio").error("Task exception was never retrieved", exc_info=True)
        text = self.out.getvalue()
        self.assertIn("ValueError", text)
        self.assertIn("[secret]", text)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn("\x1b", text)
        self.assertLess(len(text), 2 * (musebridge.MAX_LOG_EXC + musebridge.MAX_LOG_MSG) + 400)

    def test_secret_is_redacted_before_clipping_and_bad_formats_are_safe(self):
        arg = "x" * (musebridge.MAX_LOG_ARG - 4) + self.SECRET
        self.log.info("invoke %s", arg)
        self.log.info("%d bytes", "pas un nombre")
        self.log.info("%(a)s", {"a": self.SECRET})
        with contextlib.redirect_stdout(io.StringIO()) as ev:
            musebridge.log_event("muse_invoke", command="?", error="x\u202e" + self.SECRET)
        text = self.out.getvalue() + ev.getvalue()
        self.assertNotIn(self.SECRET[:10], text)
        self.assertIn("[arguments]", text)
        self.assertNotIn("\u202e", text)

    @tmb.needs_crypto
    def test_fake_muse_sending_a_multi_mib_control_command_gets_a_clean_refusal(self):
        sdk = tmb.load_sdk()
        st = tmb.StateEnv(self)
        fx = tmb.AgentPortFixture(self)
        fx.issue()
        st.write("sdk_token", tmb.SDK_TOKEN + "\n")
        st.pairing()
        status = os.path.join(st.dir, "run-status.json")
        res = {}

        async def scenario():
            qd, qv = asyncio.Queue(), asyncio.Queue()
            dev_ws, vm_ws = tmb.Pipe(qd, qv), tmb.Pipe(qv, qd)

            async def connect(url, headers):
                return dev_ws
            b = musebridge.Bridge(sdk, sdk.identity.load_or_create(), musebridge.AgentPortMCP(fx.client),
                                  sdk_token=tmb.SDK_TOKEN, connect=connect,
                                  fetch_vms=lambda t, r: ([{"vm_auth_token": tmb.VM_TOKEN, "vm_id": "v",
                                                            "is_default": True}], 200),
                                  refresh=lambda *a: (None, None), status_path=status, sleep_scale=0.001)
            task = asyncio.ensure_future(b.run())
            vm = tmb.FakeVm(vm_ws)
            await vm.handshake()
            await vm.accept_control_stream()
            reg = await vm.next_message()
            await vm.send_message({"type": "res", "id": reg["id"], "ok": True})
            name = "\x1b]0;pwn\x07" + "E" * (1024 * 1024) + "\nmusegadget INFO forged " + tmb.VM_TOKEN
            await vm.send_message({"method": "link.invoke", "id": "big", "command": name, "params": None,
                                   "timeout_ms": 5000})
            await vm.send_message({"method": "link.invoke", "id": "nul", "command": "opentv.catalogue_list",
                                   "params": {"limit": None, "offset": 1}, "timeout_ms": 5000})
            got = {}
            while len(got) < 2:
                m = await asyncio.wait_for(vm.next_message(), 20)
                got[m["id"]] = m
            res["got"] = got
            await vm.send_message({"type": "evt", "event": "link.unpaired"})
            res["end"] = await asyncio.wait_for(task, 10)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asyncio.run(scenario())
        self.assertEqual((res["got"]["big"]["ok"], res["got"]["big"]["error"]), (False, "unknown_command"))
        self.assertTrue(res["got"]["nul"]["ok"], res["got"]["nul"])
        self.assertEqual(res["got"]["nul"]["payload"]["offset"], 1)
        text = self.out.getvalue() + out.getvalue()
        for line in text.split("\n"):
            self.assertLess(len(line), musebridge.MAX_LOG_MSG + 64)
            self.assertFalse(line.startswith("musegadget INFO forged"))
        for bad in ("\x1b", "\x07", tmb.VM_TOKEN, tmb.SDK_TOKEN, fx.token):
            self.assertNotIn(bad, text)


# ------------------------------------------------------------------------------------------- BlueZ (unités)
class BluezRoot(unittest.TestCase):
    def root(self, conf=DEBIAN_CONF, unit=DEBIAN_UNIT):
        r = tempfile.mkdtemp(prefix="otvbz", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        if conf is not None:
            write(os.path.join(r, "etc/bluetooth/main.conf"), conf)
        else:
            os.makedirs(os.path.join(r, "etc/bluetooth"))
        if unit is not None:
            write(os.path.join(r, "lib/systemd/system/bluetooth.service"), unit)
        return r

    @staticmethod
    def conf(r):
        return read(os.path.join(r, "etc/bluetooth/main.conf"))

    @staticmethod
    def dropin(r):
        p = os.path.join(r, musebluez.DROPIN.lstrip("/"))
        return read(p) if os.path.exists(p) else None


class TestBluezCompat(BluezRoot):
    def test_debian_default_is_applied_idempotent_and_reverted_to_exact_bytes(self):
        r = self.root()
        bz = musebluez.BlueZ(r)
        res = bz.apply()
        self.assertTrue(res["changed"])
        lines = self.conf(r).splitlines()
        i = lines.index("#ExchangeMTU = 517")
        self.assertEqual(lines[i + 1:i + 3], [musebluez.MARKER, "ExchangeMTU = 256"])     # dans [GATT]
        self.assertEqual(bz.scan_mtu(self.conf(r))["state"], "ok")
        self.assertIn("ExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=battery\n", self.dropin(r))
        self.assertEqual(bz.effective()["from"], musebluez.DROPIN)
        st = bz.status()
        self.assertEqual((st["mtu"]["state"], st["battery"]["state"], st["gate"]),
                         ("ok", "disabled_managed", "bluetooth_runtime_unknown"))
        self.assertFalse(bz.apply()["changed"])                                          # idempotent
        rev = bz.revert()
        self.assertTrue(rev["changed"])
        self.assertEqual(self.conf(r), DEBIAN_CONF)
        self.assertIsNone(self.dropin(r))
        self.assertFalse(os.path.exists(os.path.join(r, musebluez.DROPIN_DIR.lstrip("/"))))
        self.assertFalse(os.path.exists(os.path.join(r, musebluez.RECORD.lstrip("/"))))
        self.assertFalse(bz.revert()["changed"])

    def test_local_mtu_above_256_needs_the_explicit_gate_and_is_restored(self):
        conf = "[GATT]\nExchangeMTU = 517\n"
        r = self.root(conf=conf)
        bz = musebluez.BlueZ(r)
        res = bz.apply(installer=True)
        self.assertEqual(res["skipped"], ["mtu_local_value"])
        self.assertEqual(self.conf(r), conf)                                              # jamais remplacé ici
        self.assertIsNotNone(self.dropin(r))
        st = bz.status()
        self.assertEqual(st["gate"], "bluetooth_not_prepared")
        self.assertIn("--replace-mtu", st["note"])
        with self.assertRaises(musebluez.BluezError):
            bz.apply()
        bz.apply(replace_mtu=True)
        self.assertEqual(self.conf(r), "[GATT]\n%s\nExchangeMTU = 256\n" % musebluez.MARKER)
        self.assertEqual(bz.record()["mtu"]["original_line"], "ExchangeMTU = 517")
        with open(os.path.join(r, "etc/bluetooth/main.conf"), "a") as f:       # modifié localement depuis
            f.write("\n[Policy]\nAutoEnable=true\n")
        bz.revert()
        self.assertEqual(self.conf(r), conf + "\n[Policy]\nAutoEnable=true\n")

    def test_compatible_local_settings_are_left_alone(self):
        unit = DEBIAN_UNIT.replace("bluetoothd\n", "bluetoothd -P battery,sap\n")
        r = self.root(conf="[GATT]\nExchangeMTU = 185\n", unit=unit)
        bz = musebluez.BlueZ(r)
        self.assertFalse(bz.apply()["changed"])
        self.assertFalse(os.path.exists(os.path.join(r, musebluez.RECORD.lstrip("/"))))
        st = bz.status()
        self.assertEqual((st["mtu"]["state"], st["battery"]["state"]), ("ok", "disabled_local"))

    def test_noplugin_list_extended_section_appended_and_absent_file_created(self):
        unit = DEBIAN_UNIT.replace("bluetoothd\n", "bluetoothd --noplugin=sap -E\n")
        r = self.root(conf="[General]\nName = TV", unit=unit)
        bz = musebluez.BlueZ(r)
        bz.apply()
        self.assertIn("ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=sap,battery -E\n", self.dropin(r))
        self.assertEqual(self.conf(r), "[General]\nName = TV\n\n[GATT]\n%s\nExchangeMTU = 256\n" % musebluez.MARKER)
        bz.revert()
        self.assertEqual(self.conf(r), "[General]\nName = TV")
        r = self.root(conf=None)
        bz = musebluez.BlueZ(r)
        bz.apply()
        self.assertEqual(bz.scan_mtu(self.conf(r))["state"], "ok")
        bz.revert()
        self.assertFalse(os.path.exists(os.path.join(r, "etc/bluetooth/main.conf")))

    def test_foreign_dropin_complex_command_and_override_are_never_rewritten(self):
        r = self.root()
        foreign = "[Service]\nExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd -d\n"
        write(os.path.join(r, musebluez.DROPIN.lstrip("/")), foreign)
        bz = musebluez.BlueZ(r)
        self.assertIn("foreign_dropin", bz.apply(installer=True)["skipped"])
        self.assertEqual(self.dropin(r), foreign)
        with self.assertRaises(musebluez.BluezError) as e:
            bz.apply()
        self.assertEqual(e.exception.code, "foreign_dropin")
        self.assertEqual(bz.status()["battery"]["state"], "foreign_dropin")
        bz.revert()
        self.assertEqual(self.dropin(r), foreign)                                          # jamais retiré
        r = self.root(unit=DEBIAN_UNIT.replace("ExecStart=/usr/libexec/bluetooth/bluetoothd",
                                               "ExecStart=/bin/sh -c \"exec bluetoothd $OPTS\""))
        bz = musebluez.BlueZ(r)
        self.assertIn("bluetooth_unit_complex", bz.apply(installer=True)["skipped"])
        self.assertIsNone(self.dropin(r))
        r = self.root()
        bz = musebluez.BlueZ(r)
        bz.apply()
        write(os.path.join(r, "etc/systemd/system/bluetooth.service.d/zzz-local.conf"),
              "[Service]\nExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd\n")
        self.assertEqual(bz.status()["battery"]["state"], "overridden")

    def test_revert_never_follows_a_replaced_main_conf_and_never_blocks_on_a_bad_record(self):
        r = self.root()
        bz = musebluez.BlueZ(r)
        bz.apply()
        real = os.path.join(r, "admin.conf")
        write(real, "[GATT]\nExchangeMTU = 100\n")
        os.remove(os.path.join(r, "etc/bluetooth/main.conf"))
        os.symlink(real, os.path.join(r, "etc/bluetooth/main.conf"))
        rev = bz.revert()
        self.assertTrue(rev["kept"][0].startswith("unexpected_file"), rev)
        self.assertEqual(read(real), "[GATT]\nExchangeMTU = 100\n")
        self.assertIsNone(self.dropin(r))
        r = self.root()
        write(os.path.join(r, musebluez.RECORD.lstrip("/")), "{pas du json")
        self.assertEqual(musebluez.BlueZ(r).revert()["kept"], ["record_unreadable"])
        self.assertTrue(os.path.exists(os.path.join(r, musebluez.RECORD.lstrip("/"))))

    def test_symlinked_main_conf_is_refused_and_write_failure_restores_bytes(self):
        r = self.root()
        real = os.path.join(r, "elsewhere.conf")
        os.rename(os.path.join(r, "etc/bluetooth/main.conf"), real)
        os.symlink(real, os.path.join(r, "etc/bluetooth/main.conf"))
        self.assertTrue(musebluez.BlueZ(r).apply(installer=True)["skipped"][0].startswith("unexpected_file"))
        self.assertEqual(read(real), DEBIAN_CONF)
        r = self.root()
        orig = fsutil.atomic_write_path

        def failing(path, data, **kw):
            if path.endswith("zz-opentv-muse.conf"):
                raise OSError(28, "No space left on device")
            return orig(path, data, **kw)
        with mock.patch.object(fsutil, "atomic_write_path", failing), self.assertRaises(OSError):
            musebluez.BlueZ(r).apply()
        self.assertEqual(self.conf(r), DEBIAN_CONF)
        for p in (musebluez.RECORD, musebluez.MAIN_ORIG, musebluez.DROPIN_DIR):
            self.assertFalse(os.path.exists(os.path.join(r, p.lstrip("/"))), p)

    def test_runtime_gates(self):
        r = self.root()
        pid = {"v": "321"}
        bz = musebluez.BlueZ(r, runner=lambda argv, t: (0, pid["v"] + "\n"))
        bz.apply()
        mtime = os.stat(os.path.join(r, "etc/bluetooth/main.conf")).st_mtime
        tck = os.sysconf("SC_CLK_TCK")

        def proc(battery, delta):
            args = ["/usr/libexec/bluetooth/bluetoothd"] + (["--noplugin=battery"] if battery else [])
            write(os.path.join(r, "proc/321/cmdline"), "\0".join(args) + "\0")
            write(os.path.join(r, "proc/stat"), "btime %d\n" % (int(mtime) - 100))
            write(os.path.join(r, "proc/321/stat"), "321 (bluetoothd) S " + " ".join(
                ["0"] * 18 + [str(int((mtime + delta - (int(mtime) - 100)) * tck))]) + "\n")
        proc(False, 10)
        self.assertEqual(bz.status()["gate"], "bluetooth_restart_required")
        proc(True, -60)                                       # démarré avant la modification de main.conf
        self.assertEqual(bz.status()["gate"], "bluetooth_restart_required")
        proc(True, 10)
        self.assertTrue(bz.status()["ready"])
        pid["v"] = "0"
        self.assertEqual(bz.status()["gate"], "bluetooth_not_running")


class TestBluetoothGate(tmb.AdminBase):
    def test_pair_refuses_until_compatibility_is_active_and_never_claims_iphone_unaffected(self):
        self.fake_bluetoothd(battery=False)
        r, code = self.pair()
        self.assertEqual((r["error"], code), ("bluetooth_restart_required", 2))
        self.assertIn("prepare-bluetooth", r["note"])
        self.assertEqual(self.cmds(), [])
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        os.remove(self.j(musebluez.DROPIN))
        r, code = self.pair()
        self.assertEqual(r["error"], "bluetooth_not_prepared")
        for path in ("opentv_pi/museadmin.py", "README.md"):
            self.assertNotIn("iPhone non concerné", read(os.path.join(PI4, path)))

    def test_prepare_bluetooth_restarts_only_when_needed_and_never_in_a_test_root(self):
        os.remove(self.j(musebluez.DROPIN))
        self.fake_bluetoothd(battery=False)                   # bluetoothd en cours, encore sans --noplugin=battery
        r, code = self.admin().prepare_bluetooth()
        self.assertEqual((r["restart"], code), ("skipped_test_root", 1))
        self.assertNotIn("try-restart bluetooth.service", self.cmds())
        self.assertIsNotNone(read(self.j(musebluez.DROPIN)))
        self.calls.clear()
        orig = self.runner

        def runner(argv, timeout):
            if argv[1:3] == ["try-restart", "bluetooth.service"]:
                self.fake_bluetoothd(battery=True)
            return orig(argv, timeout)
        self.runner = runner
        adm = self.admin()
        adm.allow_restart = True
        r, code = adm.prepare_bluetooth()
        self.assertEqual((r["ok"], r["restarted"], code), (True, True, 0), r)
        self.assertEqual(self.cmds(), ["daemon-reload", "try-restart bluetooth.service"])
        self.calls.clear()
        r, code = adm.prepare_bluetooth()                     # déjà actif : aucun redémarrage
        self.assertEqual((r["ok"], r["restarted"], self.cmds()), (True, None, []))
        r, code = adm.prepare_bluetooth(dry_run=True)
        self.assertEqual(self.cmds(), [])
        self.euid = 1000
        self.assertEqual(museadmin.Admin(self.cfg, euid=lambda: 1000).prepare_bluetooth()[0]["error"],
                         "root_required")


# ------------------------------------------------------------------------------------------- install.sh / uninstall.sh
class TestBluezInstall(BluezRoot):
    def rootfs(self, conf=DEBIAN_CONF):
        from tests.test_service import fixture_root
        r = fixture_root(self)
        write(os.path.join(r, "etc/bluetooth/main.conf"), conf)
        write(os.path.join(r, "lib/systemd/system/bluetooth.service"), DEBIAN_UNIT)
        return r

    def run_ok(self, args, env=None):
        p = sh(args, env)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p.stdout.decode()

    def test_install_applies_owned_changes_and_uninstall_restores_exact_bytes(self):
        r = self.rootfs()
        out = self.run_ok([INSTALL, "--root", r, "--skip-preflight", "--dry-run"])
        self.assertIn("[dry-run] Muse : /etc/bluetooth/main.conf", out)
        self.assertEqual(self.conf(r), DEBIAN_CONF)
        self.assertIsNone(self.dropin(r))
        out = self.run_ok([INSTALL, "--root", r, "--skip-preflight"])
        self.assertIn("compatibilité BlueZ du SDK appliquée", out)
        self.assertIn("[racine de test, non exécuté] systemctl try-restart bluetooth.service", out)
        self.assertIn(musebluez.MARKER, self.conf(r))
        self.assertIn("--noplugin=battery", self.dropin(r))
        self.assertTrue(os.path.isfile(os.path.join(r, musebluez.RECORD.lstrip("/"))))
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        self.assertNotIn("bluetooth", man)                  # jamais un fichier livré (retiré par son propre marqueur)
        conf1, drop1 = self.conf(r), self.dropin(r)
        out = self.run_ok([INSTALL, "--root", r, "--skip-preflight"])   # idempotent, aucun redémarrage
        self.assertNotIn("try-restart bluetooth.service", out)
        self.assertEqual((self.conf(r), self.dropin(r)), (conf1, drop1))
        out = self.run_ok([UNINSTALL, "--root", r, "--dry-run"])
        self.assertIn("[dry-run] Muse :", out)
        self.assertEqual(self.conf(r), conf1)
        out = self.run_ok([UNINSTALL, "--root", r, "--purge"])
        self.assertIn("compatibilité BlueZ d'OpenTV retirée", out)
        self.assertEqual(self.conf(r), DEBIAN_CONF)
        self.assertIsNone(self.dropin(r))
        self.assertFalse(os.path.exists(os.path.join(r, musebluez.DROPIN_DIR.lstrip("/"))))

    def test_failed_install_rolls_back_bluez_changes(self):
        r = self.rootfs()
        os.makedirs(os.path.join(r, "etc/opentv"))
        os.chmod(os.path.join(r, "etc/opentv"), 0o555)      # étape suivante (opentv.env) en échec
        self.addCleanup(os.chmod, os.path.join(r, "etc/opentv"), 0o755)
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        self.assertEqual(self.conf(r), DEBIAN_CONF)
        self.assertIsNone(self.dropin(r))
        for path in (musebluez.DROPIN_DIR, musebluez.RECORD, musebluez.MAIN_ORIG):
            self.assertFalse(os.path.exists(os.path.join(r, path.lstrip("/"))), path)

    def test_local_mtu_is_preserved_and_reported(self):
        conf = "[GATT]\nExchangeMTU = 517\n"
        r = self.rootfs(conf)
        out = self.run_ok([INSTALL, "--root", r, "--skip-preflight"])
        self.assertIn("ATTENTION Muse : BlueZ non modifié (mtu_local_value)", out)
        self.assertEqual(self.conf(r), conf)
        self.assertIn("--noplugin=battery", self.dropin(r))
        self.run_ok([UNINSTALL, "--root", r])
        self.assertEqual(self.conf(r), conf)
        self.assertIsNone(self.dropin(r))


if __name__ == "__main__":
    unittest.main()
