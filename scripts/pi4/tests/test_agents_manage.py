"""Réglages TV « Gérer les assistants » : mode dédié (QR -> téléphone), distinct de « Reconfigurer ».

Couvre : navigation TV (confirmation « Non » par défaut, QR, annulation, retour à l'accueil + confirmation),
QR/état, GET/POST du téléphone, Wi-Fi/nom/hotspot/jobs jamais touchés, mise à jour de la politique, secret des
jetons, jeton faux/expiré/rejoué, CSRF/Origin, aucune émission d'identité, « autorisé » ≠ « connecté », 1920×1080.
"""
import html
import http.client
import json
import os
import re
import socket
import subprocess
import time
import unittest
import urllib.parse
from unittest import mock

from tests._common import PI4
from tests.test_service import ETH_DEV, OFF_DEV, Net, find_chromium, make_app
from tests import test_remote as tr
from tests.test_remote import NODE, remote_servers, tmpdir
from opentv_pi import agentport, agents, config, web, wifiscan

LAN = "192.168.2.40"
SECRETISH = re.compile(r"[A-Za-z0-9_-]{40,}")


def configured(tc, net=None, **env):
    a = make_app(tc, net=net, **env)
    config.save_device(a.cfg, {"name": "Salon"})
    a.reload_device()
    s = remote_servers(tc, a)
    return a, s


def tv_post(s, path):
    return s.req("POST", path, "{}", {"X-OpenTV-TV": s.app.tv_key, "Content-Type": "application/json"})


def snapshot(a):
    """Tout ce que la gestion des assistants ne doit jamais toucher."""
    def read(p):
        try:
            with open(p, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
    return {"device": read(a.cfg.device_file), "flag": os.path.exists(a.cfg.setup_flag),
            "hotspot": read(a.cfg.hotspot_state), "apply": read(a.cfg.apply_state),
            "jobs": sorted(os.listdir(a.cfg.jobs_dir)), "pending": dict(a.pending), "ap": a.ap,
            "clients": sorted(os.listdir(a.cfg.agent_clients_dir)) if os.path.isdir(a.cfg.agent_clients_dir) else None}


def foreign_and_invalid_registry(a):
    """Registre réel avec une entrée étrangère valide (« intrus »), une identité connue aux scopes invalides (Hermit),
    une identité connue sans scope (Hermes) et un seul assistant vraiment connecté (Muse)."""
    a.registry.issue("muse", ["read"])
    with open(a.registry.path, encoding="utf-8") as f:
        d = json.load(f)
    d["agents"]["intrus"] = {"token_sha256": "a" * 64, "scopes": ["display", "read"], "created_at": 1}
    d["agents"]["hermit"] = {"token_sha256": "b" * 64, "scopes": ["root", "read"], "created_at": 1}
    d["agents"]["hermes"] = {"token_sha256": "c" * 64, "scopes": [], "created_at": 1}
    d["policy"] = {"muse": ["read"], "intrus": ["display", "feedback", "read"]}
    with open(a.registry.path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return a.tv_state()


class Phone:
    def __init__(self, tc, a, s):
        self.tc, self.a, self.s = tc, a, s

    @property
    def port(self):
        return self.s.web.lan_port()

    @property
    def host(self):
        return "%s:%d" % (LAN, self.port)

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host if host is None else host}
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def post(self, form, origin=True, **kw):
        hd = {"Origin": "http://" + self.host} if origin else {}
        hd.update(kw.pop("headers", {}))
        return self.req("POST", "/assistants", form, hd, **kw)


def open_mode(tc, a, s):
    code, _, body = tv_post(s, "/api/tv/agents")
    tc.assertEqual(code, 200, body)
    m = a.tv_state()["assistants"]["manage"]
    tc.assertTrue(m["active"])
    tok = urllib.parse.parse_qs(urllib.parse.urlsplit(m["url"]).query)["t"][0]
    return tok, Phone(tc, a, s)


def csrf_of(body):
    return re.search(r'name="c" value="([0-9a-f]{32})"', body).group(1)


def fp_of(body):
    return re.search(r'name="v" value="([0-9a-f]{16})"', body).group(1)


def confirm(ph, tok, **lv):
    """Étape 2 réelle -> (page, formulaire « Oui, enregistrer » exactement tel que le navigateur l'enverrait)."""
    code, _, body = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(choose(**lv), t=tok)))
    ph.tc.assertEqual(code, 200, body)
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">',
                           body[body.index('id="save"'):body.index("</form>", body.index('id="save"'))]))
    return body, form


def choose(**lv):
    return {"agent_muse": lv.get("muse", "none"), "agent_hermit": lv.get("hermit", "none"),
            "agent_hermes": lv.get("hermes", "none")}


def wait_closed(tc, s):
    for _ in range(50):
        if s.web.lan_port() is None:
            return
        time.sleep(0.05)
    tc.fail("écoute /assistants encore ouverte")


class TestAgentsMode(unittest.TestCase):
    def test_tv_request_opens_agent_only_page_and_never_touches_network_or_name(self):
        net = Net()
        a, s = configured(self, net=net)
        self.assertIsNone(s.web.lan_port())                                     # configuré : aucune écoute LAN
        before = snapshot(a)
        tok, ph = open_mode(self, a, s)
        st = a.tv_state()
        self.assertFalse(st["setup"]["active"])                                 # jamais la configuration
        self.assertTrue(st["assistants"]["manage"]["url"].startswith("http://%s:%d/assistants?t=" % (LAN, ph.port)))
        self.assertEqual(s.web.lan.purpose, "agents")
        self.assertTrue(a.want_remote())                                        # télécommande téléphone intacte
        # seul /assistants existe : ni /setup (même avec ce jeton), ni balayage Wi-Fi, ni setup.js
        for path in ("/setup", "/setup?t=" + tok, "/setup?t=%s&net=keep" % tok, "/static/setup.js", "/static/tv.js",
                     "/tv", "/api/tv/state", "/r", "/"):
            self.assertEqual(ph.req("GET", path)[0], 404, path)
        self.assertEqual(ph.req("POST", "/setup", {"t": tok, "name": "Pirate", "net": "keep"},
                                {"Origin": "http://" + ph.host})[0], 404)
        self.assertEqual(ph.req("GET", "/static/setup.css")[0], 200)
        code, h, body = ph.req("GET", "/assistants?t=" + tok)
        self.assertEqual(code, 200)
        self.assertEqual(h["referrer-policy"], "same-origin")                   # Origin réelle des formulaires
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertNotIn("script-src", h["content-security-policy"])            # aucun script : utilisable sans JS
        self.assertNotIn("<script", body)
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("Étape 1 sur 3", body)
        self.assertIn("Le Wi-Fi et le nom de la TV ne changent pas", body)
        for jargon in ("Mot de passe", "psk", "ssid", "Nom de la TV", "Wi-Fi masqué", "sudo", "opentv-pi",
                       "/etc/opentv", "token", "agent.sock"):
            self.assertNotIn(jargon, body)
        _, form = confirm(ph, tok, muse="feedback")
        code, h, body = ph.post(form)
        self.assertEqual(code, 200, body)
        self.assertIn("C'est enregistré", body)
        # page finale : l'écoute se ferme juste après -> styles en ligne, autorisés par leur seule empreinte
        self.assertNotIn("/static/setup.css", body)
        self.assertIn("<style>%s</style>" % web.AGENTS_RESULT_CSS, body)
        self.assertIn("style-src 'self' " + web.AGENTS_RESULT_CSS_HASH, h["content-security-policy"])
        self.assertNotIn("unsafe-inline", h["content-security-policy"])
        self.assertNotIn("script-src", h["content-security-policy"])
        a.tick()
        s.web.sync_lan()
        wait_closed(self, s)
        after = snapshot(a)
        self.assertEqual(before, after)                                         # Wi-Fi, nom, hotspot, jobs intacts
        self.assertEqual(a.device["name"], "Salon")
        self.assertNotIn(wifiscan.NMCLI_WIFI_LIST, net.calls)                   # aucun balayage Wi-Fi
        self.assertFalse(any(c[:3] == ["nmcli", "connection", "up"] for c in net.calls))
        self.assertFalse(os.path.exists(a.cfg.agent_socket))                    # Agent Port non ouvert par ce mode
        self.assertEqual(a.registry.policy(), {"muse": ["feedback", "read"], "hermit": [], "hermes": []})

    def test_lan_page_binds_current_lan_address_and_refuses_foreign_host_and_peers(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        binds = []
        s = remote_servers(self, a, binds)
        tok, ph = open_mode(self, a, s)
        self.assertIn(LAN, binds)                                               # adresse LAN, jamais 0.0.0.0
        self.assertNotIn("0.0.0.0", binds)
        for host in ("127.0.0.1:%d" % ph.port, "evil.example:%d" % ph.port, LAN, "%s:1" % LAN, ""):
            self.assertEqual(ph.req("GET", "/assistants?t=" + tok, host=host)[0], 421, host)
        # service réel : un pair en boucle locale (ou public) est refusé (lan_peer)
        s.web.stop()
        from tests.test_service import Servers
        s2 = Servers(self, a, remote_bind=lambda ip: "127.0.0.1")
        a.cancel_agents("test")
        a.request_agents()
        s2.web.sync_lan()
        ph2 = Phone(self, a, s2)
        self.assertEqual(ph2.req("GET", "/assistants?t=" + a.agents_token.current()[0])[0], 403)
        self.assertFalse(web.lan_peer("127.0.0.1") or web.lan_peer("8.8.8.8"))

    def test_refused_while_unconfigured_in_setup_or_offline(self):
        a = make_app(self)                                                      # jamais configuré : configuration
        s = remote_servers(self, a)
        code, _, body = tv_post(s, "/api/tv/agents")
        self.assertEqual((code, json.loads(body)["error"]), (409, "setup"))
        self.assertEqual(s.web.lan.purpose, "setup")
        self.assertFalse(a.tv_state()["assistants"]["manage"]["active"])
        b = make_app(self, net=Net(dev=OFF_DEV, ip=""))
        config.save_device(b.cfg, {"name": "Salon"})
        b.reload_device()
        os.remove(b.cfg.setup_flag) if os.path.exists(b.cfg.setup_flag) else None
        sb = remote_servers(self, b)
        code, _, body = tv_post(sb, "/api/tv/agents")
        self.assertEqual((code, json.loads(body)["error"]), (409, "offline"))
        self.assertIsNone(sb.web.lan_port())

    def test_reconfigure_during_agents_mode_cancels_it_and_tokens_never_cross(self):
        a, s = configured(self)
        tok, ph = open_mode(self, a, s)
        tv_post(s, "/api/tv/setup")                                             # « Reconfigurer » confirmé sur la TV
        a.tick()
        s.web.sync_lan()
        st = a.tv_state()
        self.assertTrue(st["setup"]["active"])
        self.assertEqual(st["assistants"]["manage"]["last"]["kind"], "cancelled")
        self.assertEqual(s.web.lan.purpose, "setup")                           # serveur de configuration distinct
        lan = s.web.lan_port()
        self.assertEqual(s.req("GET", "/assistants?t=" + tok, lan=True)[0], 404)
        self.assertEqual(s.req("GET", "/setup?t=" + tok, lan=True)[0], 403)     # jeton assistants ≠ jeton Wi-Fi
        self.assertFalse(a.check_agents_token(st["setup"]["token"]))            # et réciproquement
        self.assertIsNotNone(lan)

    def test_header_names_and_counts_come_from_the_same_canonical_list(self):
        """En-tête TV (« agents ») et compteurs Autorisés/Connectés : une seule lecture des assistants connus. Une
        entrée étrangère du registre ou une identité aux scopes invalides n'apparaît jamais dans l'un sans l'autre."""
        a, _s = configured(self)
        st = foreign_and_invalid_registry(a)
        self.assertEqual(sorted(a.registry.list()), ["hermes", "hermit", "intrus", "muse"])   # bien présents au registre
        asst = st["assistants"]
        self.assertEqual(st["agents"], ["muse"])
        self.assertEqual(st["agents"], [x["id"] for x in asst["list"] if x["connected"]])
        self.assertEqual(asst["connected"], len(st["agents"]))
        self.assertEqual((asst["authorized"], asst["connected"], asst["total"]), (1, 1, 3))
        self.assertEqual([x["id"] for x in asst["list"]], ["muse", "hermit", "hermes"])
        self.assertNotIn("intrus", json.dumps(st))

    def test_get_never_consumes_and_status_is_truthful(self):
        a, s = configured(self)
        hermit_tok = a.registry.issue("hermit", ["read"])                       # installé par l'administrateur
        a.registry.set_policy({"muse": ["feedback", "read"], "hermit": ["read"], "hermes": []})
        tok, ph = open_mode(self, a, s)
        st = a.tv_state()["assistants"]
        self.assertEqual((st["authorized"], st["connected"]), (2, 1))           # Muse autorisé mais pas connecté
        self.assertEqual({x["id"]: (x["authorized"], x["connected"]) for x in st["list"]},
                         {"muse": (True, False), "hermit": (True, True), "hermes": (False, False)})
        for _ in range(12):                                                     # aucun GET n'use le QR
            self.assertEqual(ph.req("GET", "/assistants?t=" + tok)[0], 200)
        code, _, body = ph.req("GET", "/assistants?t=" + tok)
        sec = {m.group(1): m.group(0) for m in re.finditer(r"<section class=\"agent\"><h2>(\w+)</h2>(.*?)</section>",
                                                            body)}
        self.assertIn("Autorisé</b><span>Voir + donner mon avis", sec["Muse"])
        self.assertIn("Connecté</b><span>Non, pas installé sur la boîte", sec["Muse"])   # jamais « actif »
        self.assertIn("Connecté</b><span>Oui, installé sur la boîte (voir seulement) · pas encore actif", sec["Hermit"])
        self.assertIn("Autorisé</b><span>Non", sec["Hermes"])
        self.assertIn("Autoriser un assistant ne l'installe pas", html.unescape(body))
        # activité réelle seulement par un appel authentifié à l'Agent Port (socket Unix), jamais supposée
        svc = agentport.AgentService(a, a.registry, a.audit)
        self.assertFalse(svc.handle({"v": 1, "agent": "muse", "token": "x" * 43, "op": "whoami"})["ok"])
        self.assertNotIn("muse", a.agent_seen_at)
        self.assertTrue(svc.handle({"v": 1, "agent": "hermit", "token": hermit_tok, "op": "whoami"}, 1000)["ok"])
        body = html.unescape(ph.req("GET", "/assistants?t=" + tok)[2])
        self.assertIn("(voir seulement) · actif à l'instant", body)
        self.assertNotIn(hermit_tok, body)
        self.assertNotIn(agents._sha(hermit_tok), body)
        self.assertNotIn(hermit_tok, json.dumps(a.tv_state()))
        self.assertTrue(a.check_agents_token(tok))

    def test_confirm_step_explains_real_consequences_then_save_restricts_without_issuing(self):
        a, s = configured(self)
        hermit_tok = a.registry.issue("hermit", ["display", "feedback", "read"])
        hermes_tok = a.registry.issue("hermes", ["read"])
        a.registry.set_policy({"muse": [], "hermit": ["display", "feedback", "read"], "hermes": ["read"],
                               "zed": ["read"]})                               # entrée étrangère : conservée
        reg_before = set(a.registry.list())
        tok, ph = open_mode(self, a, s)
        want = choose(muse="full", hermit="read", hermes="none")
        code, _, body = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(want, t=tok)))
        self.assertEqual(code, 200)
        self.assertIn("Étape 2 sur 3", body)
        rec = {m.group(1): m.group(2) for m in re.finditer(r"<li><b>(\w+)</b>(.*?)</li>", body)}
        self.assertIn("Pas installé sur la boîte : cette autorisation ne le connecte pas", rec["Muse"])
        self.assertIn("Il perd tout de suite les droits retirés", rec["Hermit"])
        self.assertIn("Il sera déconnecté tout de suite", rec["Hermes"])
        self.assertIn("Oui, enregistrer", body)
        self.assertEqual(a.registry.policy()["hermit"], ["display", "feedback", "read"])   # rien avant le POST
        _, form = confirm(ph, tok, muse="full", hermit="read", hermes="none")
        code, _, done = ph.post(form)
        self.assertEqual(code, 200, done)
        self.assertIn("Étape 3 sur 3", done)
        pol = a.registry.policy()
        self.assertEqual(pol, {"muse": ["display", "feedback", "read"], "hermit": ["read"], "hermes": [],
                               "zed": ["read"]})
        lst = a.registry.list()
        self.assertNotIn("muse", lst)                                           # autorisé ≠ installé : aucun jeton
        self.assertEqual(lst["hermit"]["scopes"], ["read"])                     # droits réduits tout de suite
        self.assertEqual(a.registry.authenticate("hermit", hermit_tok), ["read"])
        self.assertIsNone(a.registry.authenticate("hermes", hermes_tok))        # « Aucun accès » : déconnecté
        self.assertLessEqual(set(lst), reg_before)                              # jamais d'identité ajoutée
        self.assertEqual(os.listdir(a.cfg.agent_clients_dir) if os.path.isdir(a.cfg.agent_clients_dir) else [], [])
        st = a.tv_state()["assistants"]
        self.assertEqual((st["authorized"], st["connected"]), (2, 1))
        self.assertEqual(st["manage"]["last"]["kind"], "saved")
        self.assertIn("Muse</b><span>Modifié</span><ul class=\"status\"><li class=\"st yes\"><b>Autorisé</b>"
                      "<span>Avis + messages sur la TV", done)
        self.assertIn("Non, pas installé sur la boîte", done)
        with open(a.cfg.registry_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(hermit_tok, raw)                                       # seul le hash reste sur disque
        for x in (tok, hermit_tok, hermes_tok, csrf_of(body)):
            self.assertNotIn(x, json.dumps(a.test_logs))

    def test_no_change_offers_finish_without_writing(self):
        a, s = configured(self)
        a.registry.set_policy({"muse": [], "hermit": [], "hermes": []})
        with open(a.cfg.registry_file, "rb") as f:
            raw = f.read()
        tok, ph = open_mode(self, a, s)
        body = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(choose(), t=tok)))[2]
        self.assertIn("Aucun changement", body)
        self.assertNotIn("Oui, enregistrer", body)
        self.assertIn("Terminer sans rien changer", body)
        code, _, page = ph.post({"t": tok, "c": csrf_of(body), "action": "cancel"})
        self.assertEqual(code, 200)
        self.assertIn("Rien n'a changé", html.unescape(page))
        with open(a.cfg.registry_file, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(a.tv_state()["assistants"]["manage"]["last"]["kind"], "cancelled")
        wait_closed(self, s)

    def test_wrong_token_csrf_origin_content_type_and_replay(self):
        a, s = configured(self)
        a.registry.set_policy({"muse": [], "hermit": [], "hermes": []})
        tok, ph = open_mode(self, a, s)
        _, form = confirm(ph, tok, hermit="read")
        pol = a.registry.policy()
        for bad in ({"t": "x" * 43}, {"t": ""}, {"c": "0" * 32}, {"action": "drop"},
                    {"agent_muse": "root"}, {"agent_hermes": ""}):
            code, _, body = ph.post(dict(form, **bad))
            self.assertIn(code, (400, 403), bad)
            self.assertEqual(a.registry.policy(), pol, bad)                     # politique jamais touchée
            self.assertNotIn("C'est enregistré", body)
        self.assertTrue(a.check_agents_token(tok))                              # 4 essais usés, QR encore vivant
        cases = [({"Origin": "null"}, 403), ({"Origin": "http://evil.example"}, 403),
                 ({"Origin": "http://127.0.0.1:%d" % ph.port}, 403)]
        for hd, want in cases:
            self.assertEqual(ph.post(form, origin=False, headers=hd)[0], want, hd)
        self.assertEqual(ph.post(form, origin=False, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(ph.post(form, origin=False, headers={"Referer": "http://evil.example/x"})[0], 403)
        self.assertEqual(ph.req("POST", "/assistants", json.dumps(form),
                                {"Origin": "http://" + ph.host, "Content-Type": "application/json"})[0], 415)
        self.assertEqual(a.registry.policy(), pol)
        code, _, body = ph.post(form)
        self.assertEqual(code, 200, body)
        self.assertEqual(a.registry.policy()["hermit"], ["read"])
        # rejeu (même formulaire ou autre choix) : refusé, la politique enregistrée ne bouge plus — y compris
        # pendant un nouveau mode « assistants » (nouveau QR : l'ancien jeton et son CSRF ne valent plus rien)
        for replay in (form, dict(form, agent_hermit="full")):
            self.assertEqual(a.agents_submit(replay, client="127.0.0.1")[:2], (403, "denied"))
        tok2, ph = open_mode(self, a, s)
        for replay in (form, dict(form, agent_hermit="full")):
            self.assertEqual(ph.post(replay)[0], 403)
        self.assertEqual(ph.post(dict(form, t=tok2))[0], 403)                   # CSRF lié au QR : ancien refusé
        self.assertEqual(a.registry.policy()["hermit"], ["read"])
        self.assertFalse(a.check_agents_token(tok))
        self.assertTrue(a.check_agents_token(tok2))

    def test_invalid_submissions_burn_the_qr_and_wrong_tokens_are_rate_limited(self):
        a, s = configured(self)
        tok, ph = open_mode(self, a, s)
        c = csrf_of(ph.req("GET", "/assistants?t=" + tok)[2])
        for _ in range(5):
            code, _, body = ph.post(dict(choose(muse="root"), t=tok, c=c, action="save"))
        self.assertIn(code, (400, 403))
        self.assertFalse(a.check_agents_token(tok))                             # 5 essais : QR mort
        self.assertIn("Code expiré ou déjà utilisé", body)
        a.tick()
        m = a.tv_state()["assistants"]["manage"]
        self.assertEqual((m["active"], m["last"]["kind"]), (False, "expired"))
        # jetons faux : jamais d'essai usé, mais limités par adresse
        tok, ph = open_mode(self, a, s)
        for _ in range(30):
            ph.post(dict(choose(), t="y" * 43, c="0" * 32, action="save"))
        self.assertEqual(ph.post(dict(choose(), t=tok, c=a.agents_csrf(tok), action="save"))[0], 429)
        self.assertEqual(ph.req("GET", "/assistants?t=" + tok)[0], 403)        # limité aussi en lecture
        a.mono_clock.t += 61
        self.assertTrue(a.check_agents_token(tok))

    def test_expiry_closes_page_and_tv_is_told(self):
        a, s = configured(self)
        tok, ph = open_mode(self, a, s)
        c = csrf_of(ph.req("GET", "/assistants?t=" + tok)[2])
        a.mono_clock.t += a.cfg.token_ttl_s + 1
        self.assertEqual(ph.req("GET", "/assistants?t=" + tok)[0], 403)
        self.assertEqual(ph.post(dict(choose(muse="full"), t=tok, c=c, action="save"))[0], 403)
        a.tick()
        s.web.sync_lan()
        m = a.tv_state()["assistants"]["manage"]
        self.assertEqual((m["active"], m["url"], m["last"]["kind"]), (False, None, "expired"))
        self.assertIsNone(s.web.lan_port())
        self.assertEqual(a.registry.policy(), {})

    def test_cancel_from_tv_and_network_loss_keep_policy(self):
        a, s = configured(self)
        a.registry.set_policy({"muse": ["read"], "hermit": [], "hermes": []})
        tok, ph = open_mode(self, a, s)
        self.assertEqual(tv_post(s, "/api/tv/agents/cancel")[0], 200)
        self.assertIsNone(s.web.lan_port())
        self.assertEqual(a.tv_state()["assistants"]["manage"]["last"]["kind"], "cancelled")
        self.assertFalse(a.check_agents_token(tok))
        tok, ph = open_mode(self, a, s)
        a.runner.dev, a.runner.ip = OFF_DEV, ""                                 # câble débranché
        a.tick()
        s.web.sync_lan()
        self.assertEqual(a.tv_state()["assistants"]["manage"]["last"]["kind"], "expired")
        self.assertIsNone(s.web.lan_port())
        self.assertEqual(a.registry.policy(), {"muse": ["read"], "hermit": [], "hermes": []})
        a.runner.dev = ETH_DEV

    def test_unreadable_registry_and_write_failure_change_nothing(self):
        a, s = configured(self)
        with open(a.cfg.registry_file, "w", encoding="utf-8") as f:
            f.write("{pas du json")
        tok, ph = open_mode(self, a, s)
        st = a.tv_state()["assistants"]
        self.assertTrue(st["unreadable"])
        _, form = confirm(ph, tok, muse="full")
        for f2 in (form, dict(form, v="0" * 16)):   # registre illisible : refus avant toute autre vérification
            code, _, body = ph.post(f2)
            self.assertEqual(code, 503)
            self.assertIn("Rien n'a changé", html.unescape(body))
        with open(a.cfg.registry_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{pas du json")                          # jamais remplacé par du vide
        os.remove(a.cfg.registry_file)
        a.registry.set_policy({"muse": [], "hermit": [], "hermes": []})
        with open(a.cfg.registry_file, "rb") as f:
            raw = f.read()
        _, form = confirm(ph, tok, muse="full")
        with mock.patch.object(agents, "atomic_write_json", side_effect=OSError("disque plein")):
            code, _, body = ph.post(form)
        self.assertEqual(code, 503)
        self.assertIn("L'enregistrement a échoué", html.unescape(body))
        with open(a.cfg.registry_file, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(a.registry.policy(), {"muse": [], "hermit": [], "hermes": []})   # mémoire rétablie
        self.assertTrue(a.check_agents_token(tok))                              # nouvel essai possible
        self.assertEqual(ph.post(form)[0], 200)

    def test_installed_assistant_above_its_policy_is_never_silently_revoked(self):
        """Régression (revue) : Hermes installé « voir seulement » alors que sa politique est « Aucun accès » (émis
        avec --force) ; l'utilisateur ne change QUE Muse. Hermes n'est ni réduit ni révoqué, et chaque page le dit."""
        a, s = configured(self)
        hermes_tok = a.registry.issue("hermes", ["read"], force=True)
        a.registry.set_policy({"muse": [], "hermit": [], "hermes": []})
        tok, ph = open_mode(self, a, s)
        step1 = ph.req("GET", "/assistants?t=" + tok)[2]
        sec = {m.group(1): m.group(0) for m in re.finditer(r"<section class=\"agent\"><h2>(\w+)</h2>(.*?)</section>",
                                                            step1)}
        self.assertIn("Autorisé</b><span>Non", sec["Hermes"])
        self.assertIn("Connecté</b><span>Oui, installé sur la boîte (voir seulement)", sec["Hermes"])
        self.assertIn('<option value="keep" selected>Ne rien changer pour lui</option>', sec["Hermes"])                                            # défaut : rien ne change pour lui
        self.assertIn("Installé avec plus de droits que son autorisation", html.unescape(sec["Hermes"]))
        self.assertNotIn('value="keep"', sec["Muse"] + sec["Hermit"])
        # le navigateur envoie les choix par défaut de l'étape 1 : seul Muse change
        defaults = dict(re.findall(r'<select name="(agent_\w+)">.*?<option value="(\w+)" selected>', step1))
        self.assertEqual(defaults, {"agent_muse": "none", "agent_hermit": "none", "agent_hermes": "keep"})
        body, form = confirm(ph, tok, muse="feedback", hermes="keep")
        rec = {m.group(1): m.group(2) for m in re.finditer(r"<li><b>(\w+)</b>(.*?)</li>", body)}
        self.assertIn("Aucun accès → <strong>Voir + donner mon avis", rec["Muse"])
        self.assertIn("Inchangé : Aucun accès", rec["Hermes"])
        self.assertIn("Reste connecté (voir seulement) : rien ne change pour lui", rec["Hermes"])
        self.assertNotIn("déconnecté", rec["Hermes"])
        self.assertEqual((form["agent_muse"], form["agent_hermit"], form["agent_hermes"]), ("feedback", "keep", "keep"))
        code, _, done = ph.post(form)
        self.assertEqual(code, 200, done)
        self.assertEqual(a.registry.authenticate("hermes", hermes_tok), ["read"])   # ni révoqué, ni réduit
        self.assertEqual(a.registry.policy(), {"muse": ["feedback", "read"], "hermit": [], "hermes": []})
        res = {m.group(1): m.group(2) for m in re.finditer(r"<li><b>(\w+)</b><span>(\w+)</span>", done)}
        self.assertEqual(res, {"Muse": "Modifié", "Hermit": "Inchangé", "Hermes": "Inchangé"})
        self.assertIn("Oui, installé sur la boîte (voir seulement)", done)
        self.assertIn("C'est enregistré", done)
        # même forcé à la main, un « Aucun accès » identique au niveau affiché est un VRAI changement pour lui :
        # la confirmation l'annonce explicitement avant toute écriture
        tok, ph = open_mode(self, a, s)
        body, form = confirm(ph, tok, muse="feedback", hermes="none")
        rec = {m.group(1): m.group(2) for m in re.finditer(r"<li><b>(\w+)</b>(.*?)</li>", body)}
        self.assertIn("Connecté (voir seulement) → <strong>Aucun accès", rec["Hermes"])
        self.assertIn("Il sera déconnecté tout de suite", rec["Hermes"])
        self.assertIn("Inchangé", rec["Muse"])
        self.assertEqual(a.registry.authenticate("hermes", hermes_tok), ["read"])   # rien avant « Oui »
        self.assertEqual(ph.post(form)[0], 200)
        self.assertIsNone(a.registry.authenticate("hermes", hermes_tok))        # confirmé explicitement

    def test_only_changed_rows_are_written_and_stale_pages_change_nothing(self):
        a, s = configured(self)
        hermit_tok = a.registry.issue("hermit", ["display", "feedback", "read"])
        a.registry.set_policy({"muse": [], "hermit": ["display", "feedback", "read"], "zed": ["read"]})   # incomplet
        tok, ph = open_mode(self, a, s)
        page = ph.req("GET", "/assistants?" + urllib.parse.urlencode(dict(choose(hermit="full"), t=tok)))[2]
        self.assertIn("Aucun changement", page)                                 # même niveau : « Inchangé »
        self.assertNotIn('id="save"', page)
        with open(a.cfg.registry_file, "rb") as f:
            raw = f.read()
        # envoi forgé ou ancien onglet : niveaux explicites identiques à l'état -> aucune écriture
        code, _, body = ph.post(dict(choose(hermit="full"), t=tok, c=a.agents_csrf(tok), action="save",
                                     v=a.assistants_state()[1]))
        self.assertEqual(code, 200)
        self.assertIn("Aucun choix ne modifiait un assistant", html.unescape(body))
        self.assertNotIn("/static/setup.css", body)
        with open(a.cfg.registry_file, "rb") as f:
            self.assertEqual(f.read(), raw)                                     # aucune écriture
        self.assertEqual(a.tv_state()["assistants"]["manage"]["last"]["kind"], "cancelled")
        wait_closed(self, s)
        # page de confirmation périmée : l'état a changé entre l'affichage et l'envoi -> 409, rien d'écrit
        tok, ph = open_mode(self, a, s)
        _, form = confirm(ph, tok, muse="read", hermit="full")
        muse_tok = a.registry.issue("muse", ["read"], force=True)              # administrateur, entre-temps
        with open(a.cfg.registry_file, "rb") as f:
            raw = f.read()
        code, _, body = ph.post(form)
        self.assertEqual(code, 409, body)
        self.assertIn("Les assistants ont changé pendant ton choix", html.unescape(body))
        self.assertIn("Étape 1 sur 3", body)                                    # état réel réaffiché
        with open(a.cfg.registry_file, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertTrue(a.check_agents_token(tok))
        for _ in range(6):                                                      # périmé : aucun essai usé
            self.assertEqual(ph.post(form)[0], 409)
        self.assertTrue(a.check_agents_token(tok))
        code, _, body = ph.post(dict(form, v="pas une empreinte"))              # forgé : essai usé, rien d'écrit
        self.assertEqual(code, 400)
        _, form = confirm(ph, tok, muse="keep", hermit="read")
        self.assertEqual(ph.post(form)[0], 200)
        # politique complète et sûre : Muse inchangé (lecture actuelle), Hermit modifié, Hermes absent -> []
        self.assertEqual(a.registry.policy(), {"muse": [], "hermit": ["read"], "hermes": [], "zed": ["read"]})
        self.assertEqual(a.registry.authenticate("hermit", hermit_tok), ["read"])
        self.assertEqual(a.registry.authenticate("muse", muse_tok), ["read"])     # « Ne rien changer » : intact

    def test_registry_apply_policy_changes_unit(self):
        d = tmpdir(self, "otvreg")
        r = agents.Registry(os.path.join(d, "agents.json"))
        known = ("muse", "hermit", "hermes")
        t1 = r.issue("muse", ["display", "feedback", "read"])
        t3 = r.issue("hermes", ["read"])
        r.set_policy({"muse": ["display", "feedback", "read"], "other": ["read"]})
        with open(r.path, encoding="utf-8") as f:
            d0 = json.load(f)
        d0["policy"]["hermit"] = ["bogus"]                                       # entrée invalide : lue « aucun »
        with open(r.path, "w", encoding="utf-8") as f:
            json.dump(d0, f)
        fp = lambda: r.snapshot(known)[1]   # noqa: E731
        self.assertEqual(r.apply_policy_changes({"muse": ["feedback", "read"]}, known, fp()), {"muse": "restricted"})
        self.assertEqual(r.authenticate("muse", t1), ["feedback", "read"])
        self.assertEqual(r.authenticate("hermes", t3), ["read"])                # non modifié : jamais touché
        self.assertEqual(r.policy(), {"muse": ["feedback", "read"], "hermit": [], "hermes": [], "other": ["read"]})
        self.assertEqual(r.apply_policy_changes({"muse": ["display", "feedback", "read"]}, known, fp()),
                         {"muse": "policy"})                                   # jamais d'élévation d'identité
        self.assertEqual(r.authenticate("muse", t1), ["feedback", "read"])
        self.assertEqual(r.apply_policy_changes({"muse": []}, known, fp()), {"muse": "revoked"})
        self.assertIsNone(r.authenticate("muse", t1))
        with open(r.path, "rb") as f:
            raw = f.read()
        self.assertEqual(r.apply_policy_changes({}, known, fp()), {})          # rien à faire : aucune écriture
        for bad, exp in (({"hermit": ["read"]}, "0" * 16), ({"hermit": ["read"]}, None)):
            with self.assertRaises(ValueError):                                 # état périmé
                r.apply_policy_changes(bad, known, exp)
        for bad in ({"Muse!": []}, {"muse": ["root"]}, {"muse": "read"}, {"zed": ["read"]}):
            with self.assertRaises(ValueError):
                r.apply_policy_changes(bad, known, fp())
        with open(r.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        with open(r.path, "w", encoding="utf-8") as f:
            f.write("[]")
        with self.assertRaises(ValueError):
            r.apply_policy_changes({"hermit": ["read"]}, known, fp())
        with open(r.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[]")

    def test_final_page_inline_styles_are_a_verbatim_subset_of_setup_css(self):
        def rules(css):
            css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
            return {re.sub(r"\s+", "", sel): {re.sub(r"\s+", "", d) for d in body.split(";") if d.strip()}
                    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)}
        with open(os.path.join(PI4, "ui", "setup.css"), encoding="utf-8") as f:
            full = rules(f.read())
        sub = rules(web.AGENTS_RESULT_CSS)
        self.assertGreaterEqual(len(sub), 20)
        for sel, decl in sub.items():
            self.assertEqual(decl, full.get(sel), sel)                          # même règle, mot pour mot
        import base64
        import hashlib
        self.assertEqual(web.AGENTS_RESULT_CSS_HASH, "'sha256-%s'" % base64.b64encode(
            hashlib.sha256(web.AGENTS_RESULT_CSS.encode("utf-8")).digest()).decode())
        for kind in ("saved", "cancelled", "unchanged", "limited", "refused", "bad_request", "error", "denied"):
            page = web.agents_result_page(kind, [], error="x")
            self.assertIn("<style>%s</style>" % web.AGENTS_RESULT_CSS, page, kind)
            self.assertNotIn("<link", page, kind)
            self.assertNotIn("<script", page, kind)


# --------------------------------------------------------------------------- tv.js réel (harnais Chromium)
AGENT_DRIVER = r"""
<script>
(function () {
  window.__SNAPS__ = [];
  var $ = function (id) { return document.getElementById(id); };
  function press(key) {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: key, bubbles: true, cancelable: true }));
  }
  function R(e) { var r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; }
  function vis(e) { return !!e && !e.hidden && e.getClientRects().length > 0; }
  function inside(a, b) { return a[0] >= b[0] - 0.5 && a[1] >= b[1] - 0.5 && a[2] <= b[2] + 0.5 && a[3] <= b[3] + 0.5; }
  function snap(tag) {
    var pan = $("panel"), c = $("aqr"), o = { tag: tag, panel: vis(pan), ptitle: vis(pan) ? $("ptitle").textContent : null,
      btns: vis(pan) ? Array.prototype.map.call($("pbtns").children, function (b) {
        return b.getAttribute("data-label") || b.textContent; }) : [],
      focused: vis(pan) && document.querySelector("#pbtns .focused") ?
        (document.querySelector("#pbtns .focused").getAttribute("data-label") ||
         document.querySelector("#pbtns .focused").textContent) : null,
      subs: Array.prototype.map.call(document.querySelectorAll("#pbtns .ts"), function (x) { return x.textContent; }),
      qr: vis(c) ? c.getAttribute("data-qr") : null, qrVisible: vis(c) && getComputedStyle(c).visibility !== "hidden",
      remotebox: vis($("remotebox")), home: vis($("home")), settings: $("gear").className.indexOf("focused") >= 0,
      focus: document.querySelector(".card.focused .t") ? document.querySelector(".card.focused .t").textContent : null,
      toast: vis($("toast")) ? $("toast").textContent : null, toastBig: $("toast").className.indexOf("big") >= 0,
      hint: $("help").innerText, clip: [],
      qrmsg: vis($("aqrmsg")) && vis(pan) ? $("aqrmsg").textContent : null,
      posts: (window.__CALLS__ || []).filter(function (c) { return c.indexOf("POST") === 0; }),
      toasts: (window.__TOASTS__ || []).slice(),
      agentsnote: vis($("agentsnote")) ? $("agentsnote").textContent : null };
    if (vis(pan)) {   // 1920×1080 : la feuille et tout son contenu tiennent à l'écran, rien n'est rogné ni recouvert
      var pr = R(pan);
      if (!inside(pr, [0, 0, 1920, 1080])) { o.clip.push("panel " + pr.map(Math.round)); }
      Array.prototype.forEach.call(pan.querySelectorAll("*"), function (e) {
        if (!vis(e) || getComputedStyle(e).visibility === "hidden") { return; }
        if (!inside(R(e), pr)) { o.clip.push((e.id || e.tagName) + " " + R(e).map(Math.round)); }
        var cs = getComputedStyle(e);
        if ((cs.overflowY === "hidden" || cs.overflowX === "hidden") && (e.scrollHeight > e.clientHeight + 1 ||
            e.scrollWidth > e.clientWidth + 1)) { o.clip.push("overflow " + (e.id || e.className)); }
      });
      if (o.qrVisible) {
        var r = R(c), h = document.elementFromPoint((r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
        o.qrTop = h === c;
        o.qrBox = { w: c.width, cssw: r[2] - r[0], cell: Number(c.getAttribute("data-cell")),
                    quiet: Number(c.getAttribute("data-quiet")), rendering: getComputedStyle(c).imageRendering };
        if (vis($("remotebox")) || inside(r, R($("remotebox")))) { o.clip.push("two-qrs"); }
      }
    }
    window.__SNAPS__.push(o);
  }
  (window.__PLAN__ || []).forEach(function (p) {
    setTimeout(function () { (p.keys || []).forEach(press); snap(p.tag); }, p.at);
  });
  window.__EXTRA__ = function () { return window.__SNAPS__; };
})();
</script>
"""


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestAgentsTvInChromium(unittest.TestCase):
    FY, STATE, REMOTE1 = tr.TestTvKeysInChromium.FY, tr.TestTvKeysInChromium.STATE, tr.TestTvKeysInChromium.REMOTE1
    render = tr.TestTvKeysInChromium.render
    URL = "http://192.168.2.40:8081/assistants?t=" + "Q" * 43

    def st(self, active=False, seq=0, kind=None, authorized=2, connected=1, url=None, no_url=False):
        return dict(self.STATE, remote=self.REMOTE1, assistants={
            "authorized": authorized, "connected": connected, "total": 3, "unreadable": False, "list": [],
            "manage": {"active": active, "url": (url or self.URL) if active and not no_url else None,
                       "expires_in": 600 if active else None, "last": {"seq": seq, "kind": kind}}})

    def run_plan(self, states, plan, delay=7000):
        inject = "<script>window.__PLAN__=%s;</script>%s" % (json.dumps(plan), AGENT_DRIVER)
        res = self.render(states, [], delay=delay, inject=inject)
        return res, {x["tag"]: x for x in res["extra"]}

    HEADER_EXTRA = ("<script>window.__EXTRA__=function(){var e=document.getElementById('agents');"
                    "return {text:e.textContent,hidden:e.hidden};};</script>")

    def test_header_names_match_counts_with_foreign_and_invalid_registry_entries(self):
        """État réel du service (registre avec entrée étrangère + scopes invalides) : l'en-tête ne nomme que les
        assistants comptés « Connectés » ; un état incohérent (entrée étrangère dans la liste, « agents » de
        compatibilité divergent) ne fait jamais apparaître un nom non compté."""
        a, _s = configured(self)
        real = foreign_and_invalid_registry(a)
        res = self.render(real, [], delay=1500, inject=self.HEADER_EXTRA)
        self.assertEqual(res["extra"], {"text": "Connectés : Muse", "hidden": False})
        self.assertEqual(real["assistants"]["connected"], 1)
        hostile = json.loads(json.dumps(real))
        hostile["agents"] = ["intrus", "hermit", "muse"]
        hostile["assistants"]["list"].append({"id": "intrus", "authorized": True, "connected": True})
        res = self.render(hostile, [], delay=1500, inject=self.HEADER_EXTRA)
        self.assertEqual(res["extra"], {"text": "Connectés : Muse", "hidden": False})
        none = json.loads(json.dumps(real))
        none["agents"] = ["intrus"]
        for x in none["assistants"]["list"]:
            x["connected"] = False
        none["assistants"]["connected"] = 0
        res = self.render(none, [], delay=1500, inject=self.HEADER_EXTRA)
        self.assertEqual(res["extra"], {"text": "", "hidden": True})

    def test_settings_row_counts_and_default_safe_confirmation(self):
        steps = [["ArrowUp"], ["Enter", {"tag": "open"}], ["ArrowDown", {"tag": "down"}], ["Enter", {"tag": "sheet"}],
                 ["ArrowDown", {"tag": "rights"}], ["Enter", {"tag": "ask"}],
                 ["Enter", {"tag": "no"}], ["Enter", {"tag": "ask2"}], ["ArrowUp", {"tag": "yes"}],
                 ["Escape", {"tag": "back"}], ["Escape", {"tag": "back2"}], ["Escape", {"tag": "closed"}]]
        res = self.render(self.st(), steps)
        s = {x["s"]: x for x in res["steps"]}
        self.assertEqual(s["Enter:open"]["btns"], ["Reconfigurer", "Gérer les assistants", "Oublier les téléphones",
                                                   "Fermer"])
        self.assertEqual(s["ArrowDown:down"]["btn"], "Gérer les assistants")
        # « Gérer les assistants » : Muse (connexion guidée) ou les droits de chaque assistant (QR existant)
        self.assertEqual((s["Enter:sheet"]["ptitle"], s["Enter:sheet"]["btns"], s["Enter:sheet"]["btn"]),
                         ("Gérer les assistants", ["Muse", "Droits des assistants", "Retour"], "Muse"))
        self.assertEqual(s["ArrowDown:rights"]["btn"], "Droits des assistants")
        self.assertEqual((s["Enter:ask"]["ptitle"], s["Enter:ask"]["btn"]), ("Gérer les assistants ?", "Non"))
        self.assertEqual((s["Enter:no"]["ptitle"], s["Enter:no"]["btn"]), ("Gérer les assistants",
                                                                           "Droits des assistants"))
        self.assertEqual(s["ArrowUp:yes"]["btn"], "Oui, afficher le QR")
        self.assertEqual(s["Escape:back"]["ptitle"], "Gérer les assistants")      # Retour : un niveau, rien demandé
        self.assertEqual((s["Escape:back2"]["ptitle"], s["Escape:back2"]["btn"]), ("Réglages", "Gérer les assistants"))
        self.assertFalse(s["Escape:closed"]["panel"])
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")], [])   # rien sans « Oui »
        for x in res["steps"]:
            tr.TestTvKeysInChromium.assertLayout(self, x["layout"], x["s"])

    def test_qr_sheet_then_phone_save_returns_home_with_confirmation(self):
        states = [self.st(), self.st(active=True), self.st(active=True), self.st(seq=1, kind="saved", authorized=1)]
        plan = [{"at": 1200, "keys": ["ArrowDown", "ArrowRight", "ArrowUp", "ArrowUp", "Enter"], "tag": "settings"},
                {"at": 1300, "keys": ["ArrowDown", "Enter", "ArrowDown", "Enter"], "tag": "confirm"},
                {"at": 1400, "keys": ["ArrowUp", "Enter"], "tag": "asked"},
                {"at": 1900, "keys": [], "tag": "qr"},
                {"at": 3200, "keys": ["ArrowDown", "ArrowLeft"], "tag": "qr-keys"},
                {"at": 6800, "keys": [], "tag": "after"}]
        res, s = self.run_plan(states, plan)
        self.assertTrue(s["settings"]["panel"] and s["settings"]["ptitle"] == "Réglages", s["settings"])
        self.assertIn("Autorisés : 2 · Connectés : 1", s["settings"]["subs"])
        self.assertEqual(s["confirm"]["focused"], "Non")
        posts = [c for c in res["calls"] if c.startswith("POST")]
        self.assertEqual(posts, ["POST /api/tv/agents"])
        q = s["qr"]
        self.assertEqual((q["ptitle"], q["btns"], q["focused"]), ("Gérer les assistants", ["Annuler"], "Annuler"))
        self.assertEqual(q["qr"], "ok")
        self.assertTrue(q["qrVisible"] and q["qrTop"], q)
        self.assertFalse(q["remotebox"])                                        # un seul QR à l'écran
        self.assertEqual(q["clip"], [], q)                                      # 1920×1080 : rien de rogné
        self.assertEqual(q["qrBox"]["w"], q["qrBox"]["cssw"])                   # 1 px canvas = 1 px CSS
        self.assertGreaterEqual(q["qrBox"]["quiet"], 4 * q["qrBox"]["cell"])
        self.assertEqual(q["qrBox"]["rendering"], "pixelated")
        self.assertIn("Annuler", q["hint"])
        self.assertTrue(s["qr-keys"]["panel"] and s["qr-keys"]["qr"] == "ok")   # flèches : sans effet
        a = s["after"]                                                          # enregistré sur le téléphone
        self.assertFalse(a["panel"])
        self.assertTrue(a["home"] and a["remotebox"])
        self.assertEqual(a["focus"], "Alpha")                                   # retour au début de l'accueil
        self.assertTrue(a["toastBig"], a)
        self.assertEqual(a["toast"], "C'est enregistré !\nAssistants autorisés : 1 · connectés : 1")
        for x in res["extra"]:
            self.assertNotIn("http", x["hint"] + (x["toast"] or ""))
        self.assertNotIn("192.168", res["text"])                                # aucune adresse à l'écran

    def test_back_cancels_without_double_toast_and_expiry_is_announced(self):
        states = [self.st(), self.st(active=True), self.st(active=True), self.st(seq=1, kind="cancelled")]
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter", "ArrowDown", "Enter", "ArrowUp", "Enter"], "tag": "open"},
                {"at": 2600, "keys": ["Escape"], "tag": "cancel"},
                {"at": 4800, "keys": [], "tag": "after"}]
        res, s = self.run_plan(states, plan, delay=5200)
        self.assertFalse(s["cancel"]["panel"])
        self.assertEqual(s["cancel"]["toast"], "Annulé : rien n'a changé.")
        self.assertEqual([c for c in res["calls"] if c.startswith("POST")],
                         ["POST /api/tv/agents", "POST /api/tv/agents/cancel"])
        self.assertEqual(s["after"]["toast"], "Annulé : rien n'a changé.")      # pas de seconde annonce
        states = [self.st(), self.st(active=True), self.st(active=True), self.st(seq=1, kind="expired")]
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter", "ArrowDown", "Enter", "ArrowUp", "Enter"], "tag": "open"},
                {"at": 6800, "keys": [], "tag": "after"}]
        res, s = self.run_plan(states, plan)
        self.assertFalse(s["after"]["panel"])
        self.assertEqual(s["after"]["toast"], "QR des assistants expiré : rien n'a changé.")
        # OK sur « Annuler » et Accueil annulent aussi
        for k in ("Enter", "Home"):
            states = [self.st(), self.st(active=True)]
            plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter", "ArrowDown", "Enter", "ArrowUp", "Enter"], "tag": "o"},
                    {"at": 2200, "keys": [k], "tag": "k"}]
            res, s = self.run_plan(states, plan, delay=3000)
            self.assertFalse(s["k"]["panel"], k)
            self.assertIn("POST /api/tv/agents/cancel", res["calls"], k)

    OPEN = ["ArrowUp", "Enter", "ArrowDown", "Enter", "ArrowDown", "Enter", "ArrowUp", "Enter"]

    # chaque annonce réellement affichée (chaque toast(), même texte répété) : window.__TOASTS__
    TOAST_SPY = ("<script>(function(){window.__TOASTS__=[];function go(){var t=document.getElementById('toast');"
                 "new MutationObserver(function(rs){rs.forEach(function(r){if(r.type==='childList'&&"
                 "t.textContent){__TOASTS__.push(t.textContent);}});}).observe(t,{childList:true});}"
                 "if(document.getElementById('toast')){go();}else{document.addEventListener('DOMContentLoaded',go);}"
                 "})();</script>")

    def live(self, script, plan, delay, post_agents=None, fail_cancel=0, post_delay=0):
        """État du service piloté dans le temps (window.__ST__ remplacé par le plan) ; réponse de POST
        /api/tv/agents imposée (arrivant `post_delay` ms plus tard) ; les `fail_cancel` premiers POST d'annulation
        échouent (réseau)."""
        inject = self.TOAST_SPY + ("<script>(function(){var f=window.fetch,fails=%d;window.__ST__=%s;"
                  "window.fetch=function(p,o){var m=(o&&o.method)||'GET';"
                  "if(p.indexOf('/api/tv/state')===0){__CALLS__.push(m+' '+p);var d=window.__ST__;"
                  "return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(d);}});}"
                  "if(m==='POST'&&p==='/api/tv/agents/cancel'&&fails>0){fails-=1;__CALLS__.push('POST '+p+' (lost)');"
                  "return Promise.reject(new Error('net'));}"
                  "if(m==='POST'&&p==='/api/tv/agents'&&%s){__CALLS__.push('POST '+p);var r=%s;"
                  "return new Promise(function(ok){setTimeout(function(){ok({ok:true,status:200,"
                  "json:function(){return Promise.resolve(r);}});},%d);});}"
                  "return f(p,o);};%s})();</script>"
                  % (fail_cancel, json.dumps(script[0][1]), "true" if post_agents else "false",
                     json.dumps(post_agents), post_delay, "".join("setTimeout(function(){window.__ST__=%s;},%d);"
                                                      % (json.dumps(st), at) for at, st in script[1:])))
        res = self.render(self.st(), [], delay=delay, inject=inject + "<script>window.__PLAN__=%s;</script>%s"
                          % (json.dumps(plan), AGENT_DRIVER))
        return res, {x["tag"]: x for x in res["extra"]}

    def test_missing_qr_shows_honest_state_then_closes_once_without_loop(self):
        """Mode actif mais aucune adresse de QR (écoute impossible) : jamais une feuille vide pendant 10 min. Court
        délai (QR normalement immédiat), puis « QR indisponible, réessaie. » à la place exacte du QR, puis fermeture
        + annulation unique côté service et annonce ; aucune relance automatique ensuite."""
        noqr = self.st(active=True, no_url=True)
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"},
                {"at": 2300, "keys": [], "tag": "early"},
                {"at": 5200, "keys": [], "tag": "msg"},
                {"at": 10800, "keys": [], "tag": "closed"},
                {"at": 15500, "keys": [], "tag": "later"}]
        res, s = self.live([(0, self.st()), (1000, noqr)], plan, delay=16000)
        e = s["early"]
        self.assertTrue(e["panel"] and e["ptitle"] == "Gérer les assistants", e)
        self.assertFalse(e["qrVisible"])
        self.assertIsNone(e["qrmsg"])                                           # pas d'alarme pendant l'arrivée du QR
        m = s["msg"]
        self.assertTrue(m["panel"])
        self.assertEqual(m["qrmsg"], "QR indisponible, réessaie.")
        self.assertFalse(m["qrVisible"])
        self.assertEqual(m["clip"], [], m)                                      # message dans la feuille, rien de rogné
        c = s["closed"]
        self.assertFalse(c["panel"])
        self.assertEqual(c["toast"], "QR indisponible, réessaie.\nRien n'a changé.")
        self.assertEqual(c["posts"], ["POST /api/tv/agents", "POST /api/tv/agents/cancel"])
        self.assertEqual(s["later"]["posts"], c["posts"])                       # aucune boucle : un seul essai
        self.assertFalse(s["later"]["panel"])

    def test_lost_qr_address_mid_session_is_never_left_blank(self):
        noqr = self.st(active=True, no_url=True)
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"}, {"at": 2600, "keys": [], "tag": "qr"},
                {"at": 8000, "keys": [], "tag": "msg"}, {"at": 13500, "keys": [], "tag": "closed"}]
        res, s = self.live([(0, self.st()), (1000, self.st(active=True)), (3000, noqr)], plan, delay=14000)
        self.assertEqual((s["qr"]["qr"], s["qr"]["qrVisible"], s["qr"]["qrmsg"]), ("ok", True, None))
        self.assertEqual((s["msg"]["qrVisible"], s["msg"]["qrmsg"]), (False, "QR indisponible, réessaie."))
        self.assertFalse(s["closed"]["panel"])
        self.assertEqual(s["closed"]["posts"].count("POST /api/tv/agents/cancel"), 1)

    def test_service_unavailable_result_closes_sheet_with_retry_message(self):
        """Le service annonce lui-même l'écoute impossible (résultat « unavailable », plus récent que l'ouverture)."""
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"}, {"at": 5200, "keys": [], "tag": "after"}]
        res, s = self.live([(0, self.st()), (1000, self.st(active=True, no_url=True)),
                            (1700, self.st(seq=1, kind="unavailable"))], plan, delay=6000,
                           post_agents={"ok": True, "error": None, "seq": 0})
        self.assertFalse(s["after"]["panel"])
        self.assertEqual(s["after"]["toast"], "QR indisponible, réessaie.\nRien n'a changé.")
        self.assertEqual(s["after"]["posts"], ["POST /api/tv/agents"])          # rien à annuler : déjà fermé

    UNAVAILABLE_TOAST = "QR indisponible, réessaie.\nRien n'a changé."

    def test_open_answer_already_unavailable_never_opens_the_sheet_nor_cancels(self):
        """Écoute impossible PENDANT l'ouverture : la réponse porte déjà l'issue (« unavailable », plus récente que
        sa référence). La TV l'annonce une seule fois, n'ouvre jamais la feuille vide, n'envoie aucune annulation ;
        le poll suivant (même issue) ne la réannonce pas."""
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"}, {"at": 1400, "keys": [], "tag": "answer"},
                {"at": 5000, "keys": [], "tag": "polled"}, {"at": 12500, "keys": [], "tag": "later"}]
        res, s = self.live([(0, self.st()), (1300, self.st(seq=1, kind="unavailable"))], plan, delay=13000,
                           post_agents={"ok": True, "error": None, "seq": 0,
                                        "last": {"seq": 1, "kind": "unavailable"}})
        for tag in ("answer", "polled", "later"):
            self.assertFalse(s[tag]["panel"], (tag, s[tag]))
        self.assertEqual(s["answer"]["toast"], self.UNAVAILABLE_TOAST)
        self.assertEqual(s["later"]["toasts"], [self.UNAVAILABLE_TOAST])            # une seule annonce
        self.assertEqual(s["later"]["posts"], ["POST /api/tv/agents"])             # aucune annulation inutile

    def test_poll_result_before_the_open_answer_never_reopens_the_sheet(self):
        """Course inverse : un poll lit « unavailable » (seq 1) AVANT la réponse de l'ouverture (seq 0, ancienne
        forme sans « last »). La feuille n'est jamais rouverte par la réponse tardive : aucune feuille vide, aucun
        abandon à 8 s, aucune annulation, une seule annonce."""
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"}, {"at": 3000, "keys": [], "tag": "polled"},
                {"at": 4200, "keys": [], "tag": "answered"}, {"at": 12500, "keys": [], "tag": "later"}]
        res, s = self.live([(0, self.st()), (1000, self.st(seq=1, kind="unavailable"))], plan, delay=13000,
                           post_agents={"ok": True, "error": None, "seq": 0}, post_delay=2500)
        self.assertFalse(s["polled"]["panel"])
        self.assertEqual(s["polled"]["toast"], self.UNAVAILABLE_TOAST)
        for tag in ("answered", "later"):
            self.assertFalse(s[tag]["panel"], (tag, s[tag]))
        self.assertEqual(s["later"]["toasts"], [self.UNAVAILABLE_TOAST])
        self.assertEqual(s["later"]["posts"], ["POST /api/tv/agents"])

    def test_setup_partial_success_is_announced_on_the_tv(self):
        """QR de configuration : Wi-Fi en cours puis réussi, mais politique des assistants non enregistrée. L'écran
        de connexion le dit déjà, et l'annonce finale aussi (jamais « tout est prêt » seul) ; succès complet : rien."""
        from opentv_pi import app as appmod
        msg = appmod.MSG_AGENTS_NOT_SAVED

        def setup(active, saved):
            return dict(self.st(), setup={"active": active, "phase": "applying" if active else "done",
                                          "message": "Connexion au Wi-Fi en cours…" if active else None,
                                          "url": None, "token": None, "expires_in": None, "hotspot": None,
                                          "agents_saved": saved, "agents_reason": None if saved else "busy",
                                          "agents_message": None if saved else msg})
        plan = [{"at": 1500, "keys": [], "tag": "applying"}, {"at": 4600, "keys": [], "tag": "done"}]
        res, s = self.live([(0, setup(True, False)), (2500, setup(False, False))], plan, delay=5000)
        self.assertEqual(s["applying"]["agentsnote"], msg)
        self.assertTrue(s["done"]["toastBig"])
        self.assertEqual(s["done"]["toast"], "C'est prêt ! OpenTV est configuré.\n" + msg)
        self.assertNotIn("busy", "".join(s["done"]["toasts"]))
        res, s = self.live([(0, setup(True, True)), (2500, setup(False, True))], plan, delay=5000)
        self.assertIsNone(s["applying"]["agentsnote"])
        self.assertTrue(s["done"]["toast"].startswith("C'est prêt ! OpenTV est configuré."))
        self.assertNotIn("assistants", s["done"]["toast"])

    def test_lost_tv_cancel_never_hides_a_later_phone_cancel(self):
        """Annulation TV dont le POST est perdu (le drapeau « ne pas réannoncer » resterait armé) puis nouveau
        parcours : l'annulation faite ensuite sur le téléphone est bien annoncée et ferme la feuille."""
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"},
                {"at": 2600, "keys": ["Escape"], "tag": "tvcancel"},
                {"at": 5000, "keys": self.OPEN, "tag": "reopen-keys"},
                {"at": 5600, "keys": [], "tag": "reopen"},
                {"at": 10800, "keys": [], "tag": "after"}]
        res, s = self.live([(0, self.st()), (1000, self.st(active=True)),
                            (7000, self.st(seq=1, kind="cancelled"))], plan, delay=11500, fail_cancel=1)
        self.assertEqual(s["tvcancel"]["toast"], "Annulé : rien n'a changé.")
        self.assertFalse(s["tvcancel"]["panel"])
        self.assertTrue(s["reopen"]["panel"] and s["reopen"]["ptitle"] == "Gérer les assistants", s["reopen"])
        self.assertEqual(s["reopen"]["qr"], "ok")
        self.assertFalse(s["after"]["panel"])
        self.assertEqual(s["after"]["toast"], "Annulé sur le téléphone : rien n'a changé.")

    def test_result_older_than_the_new_flow_is_never_announced(self):
        """Annulation TV traitée par le service (seq 1) mais lue seulement après la réouverture : la réponse de
        l'ouverture porte seq 1, donc ce résultat ancien ne ferme pas la nouvelle feuille et n'est pas annoncé."""
        plan = [{"at": 1200, "keys": self.OPEN, "tag": "open"},
                {"at": 1500, "keys": ["Escape"], "tag": "tvcancel"},
                {"at": 1700, "keys": self.OPEN, "tag": "reopen"},
                {"at": 6500, "keys": [], "tag": "after"}]
        res, s = self.live([(0, self.st()), (1000, self.st(active=True)),
                            (1800, self.st(active=True, seq=1, kind="cancelled"))], plan, delay=7000,
                           post_agents={"ok": True, "error": None, "seq": 1})
        a = s["after"]
        self.assertTrue(a["panel"] and a["ptitle"] == "Gérer les assistants", a)
        self.assertEqual(a["qr"], "ok")
        self.assertNotEqual(a["toast"], "Annulé sur le téléphone : rien n'a changé.")

    def test_offline_badge_is_a_small_badge_never_the_full_screen_overlay(self):
        """Régression (vue en vrai) : le badge réseau « Hors ligne » portait la classe « offline » du voile plein
        écran « Reconnexion… » et recouvrait tout l'écran d'une ellipse rouge."""
        st = dict(self.STATE, network={"mode": "offline"}, remote={"enabled": False})
        inject = ("<script>window.__EXTRA__=function(){var n=document.getElementById('net').getBoundingClientRect();"
                  "return {w:n.width,h:n.height,cls:document.getElementById('net').className,"
                  "z:getComputedStyle(document.getElementById('net')).position};};</script>")
        res = self.render(st, [["ArrowUp"], ["Enter", {"tag": "open"}]], inject=inject)
        x = res["extra"]
        self.assertEqual(x["cls"], "badge net-offline")
        self.assertLess(x["w"], 300, x)
        self.assertLess(x["h"], 60, x)
        self.assertNotEqual(x["z"], "absolute")
        self.assertEqual([y["s"] for y in res["steps"] if y["layout"]["panel_hidden_by"]], [])

    def test_old_result_is_never_announced_and_refusal_is_explained(self):
        res, s = self.run_plan([self.st(seq=7, kind="saved")], [{"at": 4000, "keys": [], "tag": "x"}], delay=4500)
        self.assertIsNone(s["x"]["toast"])
        inject = ("<script>var _f=window.fetch;window.fetch=function(p,o){if(p==='/api/tv/agents'){__CALLS__.push("
                  "'POST '+p);return Promise.resolve({ok:false,status:409,json:function(){return Promise.resolve("
                  "{ok:false,error:'offline'});}});}return _f(p,o);};</script>")
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter", "ArrowDown", "Enter", "ArrowUp", "Enter"], "tag": "o"},
                {"at": 1800, "keys": [], "tag": "refused"}]
        res = self.render(self.st(), [], delay=2500, inject=inject + "<script>window.__PLAN__=%s;</script>%s"
                          % (json.dumps(plan), AGENT_DRIVER))
        r = {x["tag"]: x for x in res["extra"]}["refused"]
        self.assertFalse(r["panel"])
        self.assertIn("doit être connectée au réseau", r["toast"])


# --------------------------------------------------------------------------- bout en bout réel (Playwright)
@unittest.skipUnless(find_chromium() and NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestAgentsEndToEnd(unittest.TestCase):
    def test_tv_qr_phone_without_javascript_saves_and_tv_confirms(self):
        """Vrai service + vrai Chromium : TV 1920×1080 (Réglages -> Gérer les assistants -> Oui -> QR), téléphone
        390×844 SANS JavaScript (choix -> confirmation -> « Oui, enregistrer », Origin réelle), formulaire d'une
        origine opaque refusé, puis la TV revient à l'accueil avec la confirmation. Captures conservées."""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        a = make_app(self, OPENTV_SETUP_PORT=str(port))
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        hermit_tok = a.registry.issue("hermit", ["read"])
        hermes_tok = a.registry.issue("hermes", ["read"])          # installé au-delà de sa future politique (none)
        a.registry.set_policy({"muse": [], "hermit": ["read"], "hermes": []})
        s = remote_servers(self, a)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tmpdir(self, "otvshots")
        os.makedirs(shots, exist_ok=True)
        before = snapshot(a)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "shots": shots,
               "lan_port": port}
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_agents.js"), os.environ["OPENTV_TEST_PLAYWRIGHT"],
                            json.dumps(cfg)], capture_output=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(res["settings_buttons"], ["Reconfigurer", "Gérer les assistants", "Ambiance OpenTV",
                                                   "Fermer"])   # service Canvas réel : ligne du propriétaire
        self.assertIn("Autorisés : 1 · Connectés : 2", res["settings_text"])
        self.assertEqual(res["confirm_focus"], "Non")
        q = res["qr_layout"]
        self.assertEqual(q["viewport"], [1920, 1080])
        self.assertEqual((q["qr"], q["panel_in_view"], q["remotebox_hidden"], q["clipped"]), ("ok", True, True, []))
        self.assertFalse(res["phone_js"])
        self.assertIn("Étape 1 sur 3", res["step1"])
        self.assertIn("Non, pas installé sur la boîte", res["step1"])
        self.assertEqual(res["defaults"], [["agent_muse", "none"], ["agent_hermit", "read"], ["agent_hermes", "keep"]])
        self.assertIn("Reste connecté (voir seulement) : rien ne change pour lui", res["step2"])
        self.assertIn("Étape 2 sur 3", res["step2"])
        self.assertIn("Étape 3 sur 3", res["step3"])
        self.assertTrue(res["targets_ok"], res["targets"])                       # cibles tactiles ≥ 56 px
        posts = res["posts"]
        self.assertEqual([(x["origin"], x["status"]) for x in posts if x["opaque"]], [("null", 403)])
        legit = [x for x in posts if not x["opaque"]]
        self.assertEqual([x["status"] for x in legit], [200, 200])                # « Oui, enregistrer » puis « Annuler »
        self.assertTrue(all(x["origin"] == res["origin"] for x in legit), posts)
        self.assertEqual(res["origin"], "http://%s:%d" % (LAN, port))           # vraie adresse LAN, jamais loopback
        self.assertFalse(res["after"]["panel"])
        self.assertIn("C'est enregistré", res["after"]["toast"])
        self.assertIn("Assistants autorisés : 2 · connectés : 2", res["after"]["toast"])
        self.assertEqual(a.registry.policy(), {"muse": ["feedback", "read"], "hermit": ["read"], "hermes": []})
        self.assertEqual(a.registry.authenticate("hermit", hermit_tok), ["read"])
        self.assertEqual(a.registry.authenticate("hermes", hermes_tok), ["read"])   # jamais révoqué en silence
        # en-tête TV : identités installées, dans l'ordre canonique de la liste des compteurs (Muse, Hermit, Hermes)
        self.assertEqual(res["header"], "Connectés : Hermit, Hermes")
        # pages finales servies alors que l'écoute se ferme : styles de setup.css réellement appliqués
        want = {"body_bg": "rgb(13, 17, 23)", "body_color": "rgb(240, 243, 246)", "h1_size": "28px",
                "main_max": "520px", "lead_size": "19px", "links": 0, "sheets": 1}
        for k in ("step3_style", "cancel_style"):
            self.assertEqual({x: res[k][x] for x in want}, want, (k, res[k]))
        self.assertEqual((res["step3_style"]["recap_border"], res["step3_style"]["st_bg"]),
                         ("rgb(48, 54, 61)", "rgb(18, 54, 31)"))
        self.assertTrue(res["second_url_differs"])
        self.assertIn("Rien n'a changé", res["cancelled"])
        self.assertFalse(res["after_cancel"]["panel"])
        self.assertEqual(res["after_cancel"]["toast"], "Annulé sur le téléphone : rien n'a changé.")
        self.assertIsNone(s.web.lan_port())                                     # écoute bien fermée
        self.assertEqual(a.registry.policy(), {"muse": ["feedback", "read"], "hermit": ["read"], "hermes": []})
        self.assertNotIn("muse", a.registry.list())
        self.assertEqual(snapshot(a), before)
        for t in (res["step1"], res["step2"], res["step3"], res["cancelled"]):
            self.assertNotIn(hermit_tok, t)
            self.assertNotIn(hermes_tok, t)
        for name in ("tv-agents-settings.png", "tv-agents-confirm.png", "tv-agents-qr.png", "phone-agents-step1.png",
                     "phone-agents-step2.png", "phone-agents-step3.png", "tv-agents-saved.png",
                     "phone-agents-cancelled.png", "tv-agents-cancelled.png"):
            self.assertGreater(os.path.getsize(os.path.join(shots, name)), 8000, name)


if __name__ == "__main__":
    unittest.main()
