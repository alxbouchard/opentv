"""Revue finale du pilotage par Muse : l'identité DÉDIÉE du pont (« muse-device ») est repliée sur Muse partout où la
TV et le téléphone montrent l'état des assistants, sans jamais confondre « installé », « prêt » et « en pause ».

Couvre : vue/compteurs/en-tête « Connectés : Muse » avec SEULEMENT « muse-device » installé (vraie page TV dans
Chromium), activité réelle (« actif ») vue au nom de Muse par un vrai appel de l'Agent Port pendant que l'audit garde
l'identité authentifiée, empreinte de confirmation sensible au pont, conséquences exactes au téléphone pour
pilotage → lecture (pont installé mais en pause, jamais « pas installé ») et pilotage → aucun accès (identité
révoquée), refus serveur des fiches/touches pendant le retour qui suit une lecture, texte libre durci.
"""
import html
import json
import re
import unittest
import urllib.parse

from tests import test_agents_manage as tam
from tests import test_playback as tp
from tests import test_remote as tr
from tests.test_service import find_chromium
from opentv_pi import agentport, agents, app as appmod, musebridge, tools, validate, web

CONTROL = appmod.AGENT_LEVELS["control"]


def bridge_only(a, level="control"):
    """Registre réel : Muse autorisé au niveau `level`, SEULE l'identité du pont « muse-device » installée."""
    a.registry.set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
    tok = a.registry.issue("muse-device", CONTROL)
    if level != "control":
        _, fp, _ = a.registry.snapshot(appmod.KNOWN_AGENTS)
        a.registry.apply_policy_changes({"muse": appmod.AGENT_LEVELS[level]}, appmod.KNOWN_AGENTS, fp)
    return tok


def row(a, agent="muse"):
    return {x["id"]: x for x in a.assistants()}[agent]


def sections(body):
    return {m.group(1): html.unescape(m.group(0)) for m in
            re.finditer(r"<section class=\"agent\"><h2>(\w+)</h2>(.*?)</section>", body)}


def recap(body):
    return {m.group(1): html.unescape(m.group(2)) for m in re.finditer(r"<li><b>(\w+)</b>(.*?)</li>", body)}


class TestBridgeIsFoldedIntoMuse(unittest.TestCase):
    def test_scopes_have_one_source(self):
        self.assertEqual(musebridge.MUSE_SCOPES, sorted(agents.LINKED_REQUIRED[musebridge.MUSE_AGENT_ID]))
        self.assertEqual(musebridge.MUSE_SCOPES, CONTROL)
        self.assertEqual(agents.linked_of("muse"), ("muse-device",))
        self.assertEqual((agents.linked_of("hermit"), agents.linked_of("hermes")), ((), ()))

    def test_only_the_bridge_installed_makes_muse_connected_everywhere(self):
        a, _s = tam.configured(self)
        bridge_only(a)
        self.assertNotIn("muse", a.registry.list())                             # aucune identité « muse »
        x = row(a)
        self.assertEqual((x["connected"], x["connected_level"], x["have"], x["client"]),
                         (True, "control", CONTROL, None))
        self.assertEqual(x["bridge"], {"state": "ready", "level": "control"})
        self.assertFalse(x["over"])
        st = a.tv_state()
        self.assertEqual(st["agents"], ["muse"])                                # en-tête « Connectés : Muse »
        self.assertEqual((st["assistants"]["authorized"], st["assistants"]["connected"]), (1, 1))
        self.assertEqual([(y["id"], y["connected"]) for y in st["assistants"]["list"]],
                         [("muse", True), ("hermit", False), ("hermes", False)])
        self.assertNotIn("muse-device", json.dumps(st))                         # jamais l'identifiant brut

    def test_reduced_bridge_is_installed_but_paused_never_counted_connected(self):
        a, _s = tam.configured(self)
        tok = bridge_only(a, "read")
        self.assertEqual(a.registry.list()["muse-device"]["scopes"], ["read"])   # toujours installé
        self.assertEqual(a.registry.authenticate("muse-device", tok), ["read"])
        x = row(a)
        self.assertEqual((x["connected"], x["connected_level"], x["have"]), (False, None, ["read"]))
        self.assertEqual(x["bridge"], {"state": "paused", "level": "read"})
        st = a.tv_state()
        self.assertEqual((st["agents"], st["assistants"]["connected"]), ([], 0))
        # identité restée au-delà d'une politique abaissée sans réduction : droits EFFECTIFS = plafond (Agent Port)
        a2, _ = tam.configured(self)
        a2.registry.set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
        a2.registry.issue("muse-device", CONTROL)
        a2.registry.set_policy({"muse": ["feedback", "read"], "hermit": [], "hermes": []})   # sans revoke_exceeding
        x = row(a2)
        self.assertEqual((x["connected"], x["have"], x["bridge"]["state"], x["over"]),
                         (False, ["feedback", "read"], "paused", False))
        a2.registry.set_policy({"muse": [], "hermit": [], "hermes": []})        # plus aucun droit effectif
        x = row(a2)
        self.assertEqual((x["connected"], x["have"], x["bridge"]), (False, [], None))

    def test_parent_identity_and_bridge_are_reported_separately(self):
        a, _s = tam.configured(self)
        bridge_only(a, "read")
        a.registry.issue("muse", ["read"])                                      # autre client local « muse »
        x = row(a)
        self.assertEqual((x["connected"], x["connected_level"], x["client"], x["bridge"]["state"]),
                         (True, "read", "read", "paused"))

    def test_confirmation_fingerprint_sees_the_bridge(self):
        a, _s = tam.configured(self)
        a.registry.set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
        _, fp0, _ = a.registry.snapshot(appmod.KNOWN_AGENTS)
        a.registry.issue("muse-device", CONTROL)
        _, fp1, _ = a.registry.snapshot(appmod.KNOWN_AGENTS)
        self.assertNotEqual(fp0, fp1)
        with self.assertRaises(ValueError) as e:                                # confirmation montrée sans le pont
            a.registry.apply_policy_changes({"muse": []}, appmod.KNOWN_AGENTS, fp0)
        self.assertEqual(str(e.exception), "stale")
        self.assertIn("muse-device", a.registry.list())


class TestBridgeActivityIsSeenAsMuse(unittest.TestCase):
    def test_successful_agent_port_call_marks_muse_active_and_audit_keeps_the_identity(self):
        a, s = tam.configured(self)
        tok = bridge_only(a)
        logs = []
        svc = agentport.AgentService(a, a.registry, a.audit, log=lambda ev, **kw: logs.append((ev, kw)))
        self.assertIsNone(row(a)["seen_s"])
        self.assertFalse(svc.handle({"v": 1, "agent": "muse-device", "token": "x" * 43, "op": "call",
                                     "tool": "status", "args": {}}, 1000)["ok"])
        self.assertEqual(a.agent_seen_at, {})                                   # échec : jamais « actif »
        r = svc.handle({"v": 1, "agent": "muse-device", "token": tok, "op": "call", "tool": "status", "args": {}},
                       1000)
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(a.agent_seen_at), {"muse"})                        # vu au nom de Muse
        self.assertEqual(row(a)["seen_s"], 0)
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            audited = [json.loads(x) for x in f if x.strip()]
        self.assertEqual({x["agent"] for x in audited}, {"muse-device"})         # audit : identité authentifiée
        self.assertTrue(any(x.get("ok") for x in audited))
        self.assertNotIn(tok, json.dumps(audited) + json.dumps(logs))
        tk, ph = tam.open_mode(self, a, s)
        sec = sections(ph.req("GET", "/assistants?t=" + tk)[2])
        self.assertIn("Connecté</b><span>Oui, installé sur la boîte (piloter opentv : proposer et lancer des films)"
                      " · actif à l'instant", sec["Muse"])
        self.assertNotIn("pas installé", sec["Muse"])


    def test_paused_bridge_is_refused_on_every_call_never_seen_and_audited_as_the_bridge(self):
        """Pilotage abaissé pendant que le pont tourne : l'Agent Port de l'App réelle refuse tout appel du pont
        (bridge_paused, whoami compris), sans marquer Muse « actif » ni compter d'échec d'authentification ; la TV et
        le téléphone disent « en pause », l'audit garde « muse-device »."""
        a, s = tam.configured(self)
        tok = bridge_only(a)
        svc = agentport.AgentService(a, a.registry, a.audit)

        def req(op, tool=None, args=None):
            r = {"v": 1, "agent": "muse-device", "token": tok, "op": op}
            if op == "call":
                r.update(tool=tool, args=args or {})
            return svc.handle(r, 1000)
        self.assertTrue(req("call", "status")["ok"])
        for level in ("full", "read"):
            _, fp, _ = a.registry.snapshot(appmod.KNOWN_AGENTS)
            a.registry.apply_policy_changes({"muse": appmod.AGENT_LEVELS[level]}, appmod.KNOWN_AGENTS, fp)
            with a.lock:
                a.agent_seen_at.clear()
            for op, tool, args in (("call", "status", {}), ("call", "catalogue_list", {}),
                                   ("call", "display_message", {"text": "Salut"}),
                                   ("call", "feedback", {"id": "films/101", "action": "like"}),
                                   ("call", "present_title", {"id": "films/101"}), ("whoami", None, None)):
                self.assertEqual(req(op, tool, args), {"ok": False, "error": "bridge_paused"}, (level, tool))
            self.assertEqual(a.agent_seen_at, {})                                # jamais « actif »
            self.assertEqual((svc.fails, svc.locked_until), ({}, {}))            # jeton valide : aucun verrouillage
            x = row(a)
            self.assertEqual((x["connected"], x["bridge"], x["seen_s"]),
                             (False, {"state": "paused", "level": level}, None))
            st = a.tv_state()
            self.assertEqual((st["agents"], st["assistants"]["connected"]), ([], 0))
            self.assertEqual(a.registry.authenticate("muse-device", tok), appmod.AGENT_LEVELS[level])
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            audited = [json.loads(x) for x in f if x.strip()]
        paused = [x for x in audited if x["error"] == "bridge_paused"]
        self.assertEqual(len(paused), 12)
        self.assertEqual({x["agent"] for x in paused}, {"muse-device"})
        self.assertNotIn(tok, json.dumps(audited))
        tk, ph = tam.open_mode(self, a, s)
        sec = sections(ph.req("GET", "/assistants?t=" + tk)[2])
        self.assertIn("Connecté</b><span>Non : pont Muse installé mais en pause", sec["Muse"])
        self.assertNotIn("actif", sec["Muse"])


class TestConsentWordingForTheBridge(unittest.TestCase):
    def flow(self, level, **want):
        a, s = tam.configured(self)
        tok = bridge_only(a, level)
        tk, ph = tam.open_mode(self, a, s)
        return a, s, tok, tk, ph

    def test_control_to_read_pauses_the_bridge_and_says_so(self):
        a, s, tok, tk, ph = self.flow("control")
        sec = sections(ph.req("GET", "/assistants?t=" + tk)[2])
        self.assertIn("Oui, installé sur la boîte (piloter opentv", sec["Muse"])
        body, form = tam.confirm(ph, tk, muse="read")
        cons = recap(body)["Muse"]
        self.assertIn("Piloter OpenTV : proposer et lancer des films → <strong>Voir seulement", cons)
        self.assertIn("Muse perd tout de suite le pilotage et les autres droits retirés.", cons)
        self.assertIn("Le pont Muse reste installé sur la boîte, mais il se met en pause (il ne fonctionne qu'avec "
                      "«\u00a0Piloter OpenTV\u00a0»)", cons)
        self.assertIn("il faudra réautoriser le pilotage puis réinstaller le pont", cons)
        for wrong in ("Pas installé", "déconnecté", "révoqué", "ne le connecte pas"):
            self.assertNotIn(wrong, cons)
        code, _, done = ph.post(form)
        self.assertEqual(code, 200, done)
        self.assertEqual(a.registry.list()["muse-device"]["scopes"], ["read"])   # installé, réduit
        self.assertEqual(a.registry.authenticate("muse-device", tok), ["read"])
        st = a.tv_state()
        self.assertEqual((st["agents"], st["assistants"]["connected"]), ([], 0))
        done = html.unescape(done)
        self.assertIn("Non : pont Muse installé mais en pause (il ne fonctionne qu'avec «\u00a0Piloter OpenTV\u00a0»)",
                      done)
        self.assertNotIn("pas installé sur la boîte", done.split("<b>Muse</b>")[1].split("<b>Hermit</b>")[0])

    def test_control_to_full_loses_only_control(self):
        _a, _s, _tok, tk, ph = self.flow("control")
        cons = recap(tam.confirm(ph, tk, muse="full")[0])["Muse"]
        self.assertIn("Muse perd tout de suite le pilotage. Le pont Muse reste installé", cons)

    def test_control_to_none_revokes_the_bridge_identity(self):
        a, s, tok, tk, ph = self.flow("control")
        body, form = tam.confirm(ph, tk, muse="none")
        cons = recap(body)["Muse"]
        self.assertIn("Le pont Muse sera révoqué tout de suite : Muse ne pourra plus rien faire sur la TV par ce pont.",
                      cons)
        self.assertIn("il faudra autoriser «\u00a0Piloter OpenTV\u00a0» puis réinstaller le pont sur la boîte", cons)
        self.assertNotIn("Pas installé", cons)
        self.assertNotIn("en pause", cons)
        code, _, done = ph.post(form)
        self.assertEqual(code, 200, done)
        self.assertNotIn("muse-device", a.registry.list())
        self.assertIsNone(a.registry.authenticate("muse-device", tok))
        self.assertIn("Non, pas installé sur la boîte", html.unescape(done))   # maintenant vrai

    def test_paused_bridge_page_and_choices(self):
        a, s, tok, tk, ph = self.flow("read")
        sec = sections(ph.req("GET", "/assistants?t=" + tk)[2])
        self.assertIn("Connecté</b><span>Non : pont Muse installé mais en pause", sec["Muse"])
        self.assertNotIn("pas installé", sec["Muse"])
        cons = recap(tam.confirm(ph, tk, muse="control")[0])["Muse"]
        self.assertIn("Le pont Muse est en pause : cette autorisation ne le relance pas tout seul, il faudra le "
                      "réinstaller sur la boîte.", cons)
        self.assertNotIn("ne le connecte pas", cons)
        cons = recap(tam.confirm(ph, tk, muse="feedback")[0])["Muse"]
        self.assertIn("Le pont Muse reste installé mais en pause", cons)
        cons = recap(tam.confirm(ph, tk, muse="none")[0])["Muse"]
        self.assertIn("Le pont Muse sera révoqué tout de suite", cons)
        body = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(tam.choose(muse="read"), t=tk)))[2]
        self.assertIn("Pont Muse toujours en pause : rien ne change pour lui.", recap(body)["Muse"])
        self.assertIn("Aucun changement", body)

    def test_consequence_rules_for_other_assistants_are_unchanged(self):
        x = {"name": "Hermit", "connected": True, "connected_level": "full", "client": "full", "bridge": None}
        self.assertEqual(web._consequence(x, "none"),
                         "Il sera déconnecté tout de suite (il faudra le réinstaller pour lui redonner accès).")
        self.assertEqual(web._consequence(x, "read"), "Il perd tout de suite les droits retirés.")
        self.assertEqual(web._consequence(x, "control"),
                         "Ses droits actuels restent les mêmes tant qu'il n'est pas réinstallé sur la boîte.")
        y = {"name": "Hermes", "connected": False, "connected_level": None, "client": None, "bridge": None}
        self.assertEqual(web._consequence(y, "read"),
                         "Pas installé sur la boîte : cette autorisation ne le connecte pas.")
        self.assertIsNone(web._consequence(y, "none"))


@unittest.skipUnless(find_chromium(), "Chromium headless introuvable")
class TestTvHeaderWithOnlyTheBridge(unittest.TestCase):
    FY, STATE, REMOTE1 = tr.TestTvKeysInChromium.FY, tr.TestTvKeysInChromium.STATE, tr.TestTvKeysInChromium.REMOTE1
    render = tr.TestTvKeysInChromium.render
    HEADER_EXTRA = tam.TestAgentsTvInChromium.HEADER_EXTRA

    def test_header_reads_connectes_muse_then_nothing_when_paused(self):
        a, _s = tam.configured(self)
        bridge_only(a)
        st = a.tv_state()
        res = self.render(st, [], delay=1500, inject=self.HEADER_EXTRA)
        self.assertEqual(res["extra"], {"text": "Connectés : Muse", "hidden": False})
        _, fp, _ = a.registry.snapshot(appmod.KNOWN_AGENTS)
        a.registry.apply_policy_changes({"muse": ["read"]}, appmod.KNOWN_AGENTS, fp)   # pont en pause
        res = self.render(a.tv_state(), [], delay=1500, inject=self.HEADER_EXTRA)
        self.assertEqual(res["extra"], {"text": "", "hidden": True})


class TestNoProposalOrKeyDuringTheReturn(unittest.TestCase):
    def setUp(self):
        self.a = tp.TestAppIntegration.app(self)

    def ctl(self, name, args=None):
        return self.a.agent_control("muse-device", name, tools.validate_args(name, args or {}))

    def refused(self, name, args, code):
        n = len(self.a.tv_actions.recent())
        with self.assertRaises(tools.ToolError) as e:
            self.ctl(name, args)
        self.assertEqual(str(e.exception), code, name)
        self.assertEqual(len(self.a.tv_actions.recent()), n)                   # rien mis en file

    def test_closed_or_unavailable_refuse_present_and_keys_but_close_is_allowed(self):
        for outcome, end in (("revealed", "closed"), ("no-player", "unavailable")):
            self.client.outcome, self.client.reveal_after = outcome, 1
            self.ctl("play_title", {"id": "films/106"})
            if end == "closed":
                tp.wait_state(self.a.playback, ("playing",))
                self.client.reveal_after = 10 ** 6
                self.assertTrue(self.ctl("close_player")["closed"])
            v = tp.wait_state(self.a.playback, (end,))
            self.assertEqual(v["state"], end)
            self.assertTrue(self.a.playback.returning())
            self.refused("present_title", {"id": "films/101", "text": "Très bien noté."}, "player_returning")
            for key in ("down", "ok", "back", "home"):
                self.refused("remote_control", {"key": key}, "player_returning")
            self.assertEqual(self.ctl("close_player"), {"closed": False, "playback": self.a._pb_public()})
            self.assertTrue(self.a.playback.ack(v["seq"]))
            self.assertFalse(self.a.playback.returning())
            self.assertTrue(self.ctl("present_title", {"id": "films/101"})["accepted"])
            self.assertTrue(self.ctl("remote_control", {"key": "down"})["accepted"])
            self.a.tv_actions.cancel_queued("test")

    def test_tool_descriptions_announce_the_refusal(self):
        self.assertIn("retour", tools.TOOL_BY_NAME["present_title"]["description"])
        self.assertIn("player_returning", tools.TOOL_BY_NAME["remote_control"]["description"])


class TestFreeTextLookalikes(unittest.TestCase):
    ADVERSARIAL = [
        # points de substitution
        "evil\u00b7com", "evil\u0387com", "evil\u30fbcom", "evil\uff65com", "evil\u2022com", "evil\u22c5com",
        "evil\u0589com", "evil\u2219com", "evil\u2027com", "evil\u2e31com", "Va sur EVIL\u00b7COM ce soir",
        # espaces autour du point, point épelé ou entre crochets
        "Va sur evil . com", "Va sur evil .com", "evil \uff0e com", "evil dot com", "evil (dot) com",
        "evil [.] com", "evil(.)com", "evil point com", "evil {dot} net", "evil[dot]fr", "evil <point> org",
        # invisibles de catégorie admise et sélecteurs de variation hors emoji
        "x\u3164y", "\u3164", "evil\u3164.com", "x\u115fy", "x\uffa0y", "x\u2800y", "x\u034fy", "x\u17b4y",
        "x\u180by", "x\ufe00y", "Bonjour\ufe0f", "a\U000e0100b", "\ufe0fNon", "e\ufe0e",
        # IPv4 courte / hexadécimale, IPv6, schémas faibles
        "127.1", "Va sur 127.1.", "192.168", "10.0.1", "127.1:8080", "0x7f.1", "0x7f000001", "2130706433:80",
        "[::1]", "::1", "fe80::1", "2001:db8:0:0:1", "http:/evil", "http:evil", "https :evil", "HTTP\uff1a/evil",
        "mailto:alex", "mailto : alex", "tel:5145551234", "intent:x", "//evil", "\\\\serveur\\film", "localhost",
        "view-source:x", "javascript :alert(1)", "chrome:/settings", "itms-services:x", "1.2.3", "ftp:x",
    ]
    LEGIT = [
        "WALL\u00b7E ce soir ?", "Drame \u00b7 Comédie \u00b7 2 h 05", "Un point de vue original sur la guerre.",
        "Super. Ça vaut le coup.", "Séance à 20:30, fin vers 22:15:00 ?", "Noté 7.8/10 \u2022 TMDb 8,1",
        "Je suis content : il est bien noté.", "Un opéra : La Traviata.", "\u2764\ufe0f Coup de cœur",
        "Top 3 \u2b50\ufe0f", "Film n°1, 2 h 10 \u2014 « culte »\u2026", "Ponyo \U0001F30A et Totoro \U0001F33F",
        "E.T. ce soir, IMDb 7.9.", "O.S.S. 117 : Rio ne répond plus", "Mise au point. Net et précis.",
        "1\ufe0f\u20e3 Le premier", "Rocky II (1979) \u2013 IMDb 7,3/10",
        "C\u2019est l\u2019heure ! À tout de suite\u2026",
        "Les «\u00a0Indestructibles\u00a0» : 8/10.", "Noté 10.0 par la critique", "127,5 minutes de bonheur",
        "Sorti en 2011.", "Rien à voir : un tel film, c'est rare !",
    ]

    def test_lookalikes_are_refused_everywhere(self):
        for t in self.ADVERSARIAL:
            with self.assertRaises(ValueError, msg=repr(t)):
                validate.message_text(t)
            with self.assertRaises(ValueError, msg=repr(t)):
                tools.validate_args("present_title", {"id": "films/106", "text": t})

    def test_french_typography_emoji_titles_and_ratings_are_kept(self):
        import unicodedata
        for t in self.LEGIT:
            self.assertEqual(validate.message_text(t), unicodedata.normalize("NFC", t), repr(t))
            self.assertEqual(tools.validate_args("display_message", {"text": t})["text"],
                             unicodedata.normalize("NFC", t))


if __name__ == "__main__":
    unittest.main()
