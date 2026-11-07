"""X TV — saisie de l'IDENTIFIANT X depuis le téléphone déjà appairé (0.7.10, OPENTV_X_PHONE_ENTRY, défaut 0).

Contrat vérifié ici (service ; l'extension et le téléphone : test_x_entry_web.py) :
- drapeau : défaut 0 (config, exemple, fichier installé) ; 0 = aucune route (404), aucune UI (méta /tv « 0 »), aucun
  script téléphone servi, aucune séance ;
- séance : UNE, mémoire seulement, TTL 120 s monotones, ouverte seulement avec le témoin « X ouvert » (X ouvert À LA
  TV), par l'extension seulement (Origin exacte + portes TV) ; clé publique RSA-2048 e=65537 seulement ; effacée à la
  livraison, à l'annulation, à l'expiration, au chargement de /tv (Retour), à la perte du témoin et au redémarrage ;
- téléphone : cookie déjà appairé + Origin + CSRF + en-tête dédié ; le PREMIER téléphone appairé réclame la séance ;
  un seul envoi d'un chiffré de taille FIXE (256 octets, < n) ; relayé UNE fois à l'extension puis oublié ;
- aucune trace : ni journal, ni disque, ni vue opérateur/TV/agent ne porte la séance, la clé, le chiffré ou une
  longueur de texte.
"""
import base64
import http.client
import json
import os
import re
import unittest

from tests._common import PI4
from tests.test_remote import MDNS, remote_app, remote_servers
from tests.test_service import Net
from opentv_pi import app as appmod, config, museseal, xcontext, xentry, xescape, xprobe

ORIGIN = "chrome-extension://" + xcontext.EXTENSION_ID
KEY = museseal.PrivateKey()   # « extension » de test : seule détentrice de la clé privée (le service n'a que n, e)
JWK = KEY.public_jwk()
SECRET = "Fq3_secret-du-QR-22cxx"   # « f » du QR (fragment) : connu du téléphone et de l'extension seulement


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def seal_doc(sid, ident, n=None):
    doc = json.dumps({"v": 1, "s": sid, "f": SECRET, "t": ident}, separators=(",", ":")).encode("utf-8")
    return museseal.b64u(museseal.encrypt(n or KEY.n, KEY.e, doc))


class TestXEntryState(unittest.TestCase):
    def mk(self, present=True):
        self.mono, self.logs, self.present = Mono(), [], [present]
        return xentry.XEntry("/nonexistent", mono=self.mono, log=lambda ev, **f: self.logs.append(dict(f, event=ev)),
                             presence=lambda: self.present[0])

    def started(self):
        x = self.mk()
        code, out = x.start({"n": JWK["n"], "e": JWK["e"]})
        self.assertEqual(code, 200, out)
        return x, out["sid"]

    def test_start_requires_x_opened_on_tv_and_exact_public_key(self):
        x = self.mk(present=False)
        self.assertEqual(x.start({"n": JWK["n"], "e": "AQAB"}), (409, {"ok": False, "error": "x_not_open"}))
        x = self.mk()
        small = museseal.b64u(KEY.n.to_bytes(256, "big")[:255])
        low = museseal.b64u(b"\x7f" + b"\xff" * 255)                      # 2047 bits : jamais RSA-2048
        for bad in ({"n": JWK["n"], "e": "AQAC"}, {"n": JWK["n"], "e": "AQ"}, {"n": small, "e": "AQAB"},
                    {"n": low, "e": "AQAB"}, {"n": JWK["n"] + "A", "e": "AQAB"}, {"n": JWK["n"]},
                    {"n": JWK["n"], "e": "AQAB", "x": 1}, {"n": 3, "e": "AQAB"}, [], None,
                    {"n": JWK["n"][:-1] + "=", "e": "AQAB"}):
            self.assertEqual(x.start(bad), (400, {"ok": False, "error": "bad_key"}), bad)
        self.assertFalse(x.active())
        code, out = x.start({"n": JWK["n"], "e": "AQAB"})
        self.assertEqual(code, 200)
        self.assertEqual(set(out), {"ok", "sid", "ttl_s"})
        self.assertRegex(out["sid"], r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(out["ttl_s"], 120)

    def test_one_session_new_start_replaces(self):
        x, sid = self.started()
        _, out = x.start({"n": JWK["n"], "e": "AQAB"})
        self.assertNotEqual(out["sid"], sid)
        self.assertEqual(x.claim(sid, "p1")[0], 410)
        self.assertEqual(x.claim(out["sid"], "p1")[0], 200)

    def test_first_paired_phone_claims_others_refused(self):
        x, sid = self.started()
        code, out = x.claim(sid, "aaaa0001")
        self.assertEqual((code, out), (200, {"ok": True, "n": JWK["n"], "e": "AQAB"}))
        self.assertEqual(x.claim(sid, "bbbb0002"), (409, {"ok": False, "error": "claimed"}))
        self.assertEqual(x.claim(sid, "aaaa0001")[0], 200)                 # même téléphone : relire la clé
        self.assertEqual(x.submit(sid, "bbbb0002", seal_doc(sid, "alex"))[0], 409)
        self.assertEqual(x.claim("A" * 22, "aaaa0001")[0], 410)

    def test_single_fixed_size_submission_relayed_once_then_forgotten(self):
        x, sid = self.started()
        ct = seal_doc(sid, "alex")
        self.assertEqual(x.submit(sid, "p1", ct)[0], 409)                  # jamais avant la réclamation
        x.claim(sid, "p1")
        for bad in (ct[:-1], ct + "A", ct[:-1] + "=", "", None, 7,
                    museseal.b64u((KEY.n + 1).to_bytes(256, "big"))):    # ≥ n : jamais un chiffré RSA valide
            self.assertEqual(x.submit(sid, "p1", bad), (400, {"ok": False, "error": "bad_ct"}), bad)
        self.assertEqual(len(ct), 342)
        self.assertEqual(x.submit(sid, "p1", ct), (200, {"ok": True}))
        self.assertEqual(x.submit(sid, "p1", seal_doc(sid, "autre")), (409, {"ok": False, "error": "already_submitted"}))
        self.assertEqual(x.poll(sid), (200, {"ok": True, "state": "submitted", "ct": ct}))
        self.assertEqual(x.poll(sid), (200, {"ok": True, "state": "delivering", "ct": None}))   # relayé UNE fois
        self.assertEqual(x.status(sid, "p1"), "sent")
        self.assertEqual(x.done(sid, "maybe")[0], 400)
        self.assertEqual(x.done(sid, "filled"), (200, {"ok": True}))
        self.assertFalse(x.active())
        self.assertEqual(x.status(sid, "p1"), "delivered")
        self.assertEqual(x.poll(sid), (200, {"ok": True, "state": "delivered", "ct": None}))
        # la clé privée de l'extension retrouve exactement le document (le service n'a jamais eu de quoi le lire)
        doc = json.loads(KEY.decrypt(base64.urlsafe_b64decode(ct + "==")))
        self.assertEqual(doc, {"v": 1, "s": sid, "f": SECRET, "t": "alex"})

    def test_refused_and_cancel_paths_wipe(self):
        x, sid = self.started()
        x.claim(sid, "p1")
        x.submit(sid, "p1", seal_doc(sid, "a"))
        self.assertEqual(x.done(sid, "filled"), (409, {"ok": False, "error": "not_delivering"}))   # jamais avant poll
        x.poll(sid)
        x.done(sid, "refused")
        self.assertEqual(x.status(sid, "p1"), "refused")
        x, sid = self.started()
        x.claim(sid, "p1")
        self.assertEqual(x.phone_cancel(sid, "p2"), (409, {"ok": False, "error": "claimed"}))
        self.assertEqual(x.phone_cancel(sid, "p1"), (200, {"ok": True, "state": "cancelled"}))
        self.assertFalse(x.active())
        self.assertEqual(x.status(sid, "p1"), "cancelled")
        x, sid = self.started()
        self.assertEqual(x.cancel(sid), (200, {"ok": True}))
        self.assertEqual(x.poll(sid)[1]["state"], "cancelled")
        # contenu rejeté par l'extension (illisible, altéré, autre secret, code) : issue DISTINCTE de « champ changé »
        x, sid = self.started()
        x.claim(sid, "p1")
        x.submit(sid, "p1", seal_doc(sid, "a"))
        x.poll(sid)
        self.assertEqual(x.done(sid, "rejected"), (200, {"ok": True}))
        self.assertEqual(x.status(sid, "p1"), "rejected")

    def test_phone_cancel_only_before_ciphertext_handoff(self):
        # waiting / claimed / submitted : avant la remise du chiffré à l'extension -> annulable (rien relayé)
        for upto in ("waiting", "claimed", "submitted"):
            x, sid = self.started()
            if upto != "waiting":
                x.claim(sid, "p1")
            if upto == "submitted":
                x.submit(sid, "p1", seal_doc(sid, "alex"))
            self.assertEqual(x.phone_cancel(sid, "p1"), (200, {"ok": True, "state": "cancelled"}), upto)
            self.assertEqual(x.poll(sid), (200, {"ok": True, "state": "cancelled", "ct": None}), upto)  # jamais relayé
            self.assertEqual(x.status(sid, "p1"), "cancelled", upto)

    def test_phone_cancel_too_late_after_handoff_never_overwrites_delivered(self):
        x, sid = self.started()
        x.claim(sid, "p1")
        x.submit(sid, "p1", seal_doc(sid, "alex"))
        self.assertEqual(x.poll(sid)[1]["state"], "submitted")         # chiffré remis : « delivering »
        # course : Annuler du téléphone APRÈS la remise -> trop tard ; la séance continue, rien n'est effacé
        self.assertEqual(x.phone_cancel(sid, "p1"), (409, {"ok": False, "error": "too_late", "state": "sent"}))
        self.assertTrue(x.active())
        self.assertEqual(x.status(sid, "p1"), "sent")
        self.assertEqual(x.done(sid, "filled"), (200, {"ok": True}))  # la TV livre quand même : issue exacte
        self.assertEqual(x.status(sid, "p1"), "delivered")
        # course : Annuler arrivé APRÈS la livraison -> l'issue reste « delivered », jamais « cancelled »
        self.assertEqual(x.phone_cancel(sid, "p1"), (409, {"ok": False, "error": "over", "state": "delivered"}))
        self.assertEqual(x.cancel(sid), (200, {"ok": True}))           # annulation TV tardive : sans effet non plus
        self.assertEqual(x.status(sid, "p1"), "delivered")
        self.assertEqual(x.poll(sid)[1]["state"], "delivered")
        self.assertEqual([l["state"] for l in self.logs if l["state"] in ("cancelled", "delivered")], ["delivered"])
        # issue « refused » / « expired » gardée de même
        x, sid = self.started()
        x.claim(sid, "p1")
        x.submit(sid, "p1", seal_doc(sid, "alex"))
        x.poll(sid)
        x.done(sid, "refused")
        self.assertEqual(x.phone_cancel(sid, "p1"), (409, {"ok": False, "error": "over", "state": "refused"}))
        self.assertEqual(x.status(sid, "p1"), "refused")
        x, sid = self.started()
        self.mono.t += xentry.TTL_S
        self.assertEqual(x.phone_cancel(sid, "p1"), (409, {"ok": False, "error": "over", "state": "expired"}))
        self.assertEqual(x.phone_cancel("bad", "p1"), (409, {"ok": False, "error": "over", "state": "gone"}))

    def test_ttl_two_minutes_monotonic(self):
        x, sid = self.started()
        x.claim(sid, "p1")
        self.mono.t += 119.9
        self.assertTrue(x.active())
        self.mono.t += 0.1
        self.assertEqual(x.submit(sid, "p1", seal_doc(sid, "a")), (410, {"ok": False, "error": "over", "state": "expired"}))
        self.assertFalse(x.active())
        self.assertEqual(x.poll(sid)[1]["state"], "expired")
        self.mono.t += xentry.TOMB_S
        self.assertEqual(x.poll(sid)[1]["state"], "gone")                # issue oubliée elle aussi

    def test_lost_submit_response_retry_learns_real_outcome(self):
        # 1er envoi accepté, réponse perdue par le téléphone ; la TV livre ; le renvoi lit l'issue RÉELLE (410 over)
        for result, final in (("filled", "delivered"), ("refused", "refused"), ("rejected", "rejected")):
            x, sid = self.started()
            x.claim(sid, "p1")
            self.assertEqual(x.submit(sid, "p1", seal_doc(sid, "alex")), (200, {"ok": True}))
            x.poll(sid)
            x.done(sid, result)
            self.assertEqual(x.submit(sid, "p1", seal_doc(sid, "alex")),
                             (410, {"ok": False, "error": "over", "state": final}), result)
        self.assertEqual(x.submit("B" * 22, "p1", "A" * 342), (410, {"ok": False, "error": "over", "state": "gone"}))

    def test_close_after_handoff_is_unknown_never_cancelled_or_expired(self):
        # chiffré remis à l'extension (delivering) puis séance fermée SANS `done` : le service ne peut pas prouver
        # « rien saisi » -> issue « unknown » (saisie possible), jamais « expired » ni « cancelled »
        def handed():
            x, sid = self.started()
            x.claim(sid, "p1")
            x.submit(sid, "p1", seal_doc(sid, "alex"))
            x.poll(sid)
            return x, sid
        x, sid = handed()
        self.mono.t += xentry.TTL_S                                      # expiration à la milliseconde près
        self.assertEqual(x.done(sid, "filled"), (409, {"ok": False, "error": "not_delivering"}))
        self.assertEqual(x.status(sid, "p1"), "unknown")
        self.assertEqual(x.submit(sid, "p1", seal_doc(sid, "a")), (410, {"ok": False, "error": "over", "state": "unknown"}))
        self.assertEqual(x.phone_cancel(sid, "p1"), (409, {"ok": False, "error": "over", "state": "unknown"}))
        x, sid = handed()
        self.present[0] = False                                          # témoin X perdu
        self.assertEqual(x.status(sid, "p1"), "unknown")
        for close in (lambda x, sid: x.cancel(sid), lambda x, sid: x.wipe_all(),
                      lambda x, sid: x.start({"n": JWK["n"], "e": JWK["e"]})):
            x, sid = handed()
            close(x, sid)
            self.assertEqual(x.poll(sid)[1]["state"], "unknown")
        # AVANT la remise (submitted, jamais relayé) : « expired » reste vrai (rien n'a pu être saisi)
        x, sid = self.started()
        x.claim(sid, "p1")
        x.submit(sid, "p1", seal_doc(sid, "alex"))
        self.mono.t += xentry.TTL_S
        self.assertEqual(x.status(sid, "p1"), "expired")

    def test_presence_lost_wipes(self):
        x, sid = self.started()
        x.claim(sid, "p1")
        self.present[0] = False
        self.assertEqual(x.poll(sid)[1]["state"], "cancelled")
        self.assertFalse(x.active())

    def test_wipe_all_clears_material_and_reveals_nothing(self):
        x, sid = self.started()
        x.claim(sid, "p1")
        ct = seal_doc(sid, "alex@example.com")
        x.submit(sid, "p1", ct)
        s = x._s
        x.wipe_all()
        self.assertEqual(s, {})                                          # dictionnaire vidé (références lâchées)
        self.assertFalse(x.active())
        blob = repr(x) + repr(vars(x))
        for secret in (sid, ct, JWK["n"]):
            self.assertNotIn(secret, blob)
        self.assertEqual(repr(x), "<XEntry active=False>")
        for line in self.logs:
            self.assertEqual(set(line), {"event", "state"}, line)       # jamais d'identifiant de séance ni de taille
            self.assertNotIn(sid, json.dumps(line))


def tv_post(s, op, body, origin=ORIGIN, ctype="application/json", raw=None):
    h = dict(s.tv_headers(), **{"Content-Type": ctype})
    if origin:
        h["Origin"] = origin
    data = raw if raw is not None else json.dumps(body)
    code, _, text = s.req("POST", xentry.TV_PREFIX + op, data, h)
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


class TestXEntryHttp(unittest.TestCase):
    def setup(self, on=True, remote=True):
        env = {"OPENTV_X_PHONE_ENTRY": "1" if on else "0"}
        if not remote:
            env["OPENTV_REMOTE_ENABLED"] = "0"
        a = remote_app(self, **env)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = remote_servers(self, a)
        a._mdns_check()
        self.assertTrue(xescape.set_marker(a.cfg.run_dir, True))        # X ouvert À LA TV (POST presence de /tv)
        return a, s

    def preq(self, s, method, path, body=None, cookie=None, origin=True, header=True, host=None):
        port = s.web.remote_port()
        host = host or "%s:%d" % (MDNS, port)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": host}
        if cookie:
            h["Cookie"] = "otv_remote=" + cookie
        if body is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(body)
        if origin:
            h["Origin"] = "http://" + host
        if header:
            h[xentry.REMOTE_HEADER] = "x"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        try:
            return r.status, hdrs, json.loads(data)
        except ValueError:
            return r.status, hdrs, data

    def pair(self, a, s):
        port = s.web.remote_port()
        host = "%s:%d" % (MDNS, port)
        secret = a.tv_state()["remote"]["url"].split("?p=")[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/r/pair", body="p=" + secret, headers={
            "Host": host, "Origin": "http://" + host, "Content-Type": "application/x-www-form-urlencoded"})
        r = c.getresponse()
        r.read()
        tok = re.match(r"otv_remote=([A-Za-z0-9_-]{43});", r.getheader("Set-Cookie")).group(1)
        c.close()
        phone = a.remote.session(tok)
        return tok, a.remote.csrf(phone)

    def start(self, a, s):
        code, out = tv_post(s, "start", {"n": JWK["n"], "e": "AQAB"})
        self.assertEqual(code, 200, out)
        return out

    def test_flag_default_off_everywhere(self):
        self.assertFalse(config.Config({}).x_phone_entry)
        self.assertTrue(config.Config({"OPENTV_X_PHONE_ENTRY": "1"}).x_phone_entry)
        with self.assertRaises(ValueError):
            config.Config({"OPENTV_X_PHONE_ENTRY": "yes"})
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            ex = f.read()
        self.assertIn("#OPENTV_X_PHONE_ENTRY=0", ex)
        self.assertNotRegex(ex, r"(?m)^OPENTV_X_PHONE_ENTRY=")                 # jamais actif par l'exemple installé

    def test_off_no_routes_no_ui_no_static_no_session(self):
        a, s = self.setup(on=False)
        for op in xentry.TV_OPS:
            self.assertEqual(tv_post(s, op, {"n": JWK["n"], "e": "AQAB"})[0], 404, op)
        self.assertFalse(a.x_entry.active())
        code, _, page = s.req("GET", "/tv")
        self.assertIn('<meta name="opentv-x-entry" content="0">', page)
        self.assertNotIn("{{X_ENTRY}}", page)
        tok, csrf = self.pair(a, s)
        for path in ("/r/x?s=" + "A" * 22, "/r/x/status?s=" + "A" * 22, "/static/xphone.js", "/static/muse.js"):
            self.assertEqual(self.preq(s, "GET", path, cookie=tok)[0], 404, path)
        for path in xentry.PHONE_POSTS:
            self.assertEqual(self.preq(s, "POST", path, {"s": "A" * 22, "c": csrf}, cookie=tok)[0], 404, path)
        # X TV désactivé ou télécommande absente : la saisie l'est aussi, même avec le drapeau
        a2, s2 = self.setup(on=True, remote=False)
        self.assertEqual(tv_post(s2, "start", {"n": JWK["n"], "e": "AQAB"})[0], 404)
        self.assertIn('content="0"', s2.req("GET", "/tv")[2])

    def test_on_tv_meta_and_start_url_without_fragment(self):
        a, s = self.setup()
        page = s.req("GET", "/tv")[2]
        self.assertIn('<meta name="opentv-x-entry" content="1">', page)
        xescape.set_marker(a.cfg.run_dir, True)                         # /tv vient de retirer le témoin
        out = self.start(a, s)
        self.assertEqual(set(out), {"ok", "sid", "ttl_s", "url"})
        self.assertEqual(out["url"], "http://%s:%d/r/x?s=%s" % (MDNS, s.web.remote_port(), out["sid"]))
        self.assertNotIn("#", out["url"])                                # secret et empreinte : ajoutés par l'extension

    def test_tv_routes_gates(self):
        a, s = self.setup()
        good = {"n": JWK["n"], "e": "AQAB"}
        self.assertEqual(tv_post(s, "start", good, origin=None)[0], 403)
        self.assertEqual(tv_post(s, "start", good, origin="http://127.0.0.1:%d" % s.web.tv_port())[0], 403)  # /tv
        self.assertEqual(tv_post(s, "start", good, origin="https://x.com")[0], 403)       # page X : jamais
        self.assertEqual(tv_post(s, "start", good, ctype="text/plain")[0], 415)
        self.assertEqual(tv_post(s, "start", None, raw="{" * 5000)[0], 413)
        self.assertEqual(tv_post(s, "start", None, raw="[" * 400 + "]" * 400)[0], 400)
        code, _, _ = s.req("POST", xentry.TV_PREFIX + "start", json.dumps(good),
                           {"Content-Type": "application/json", "Origin": ORIGIN})
        self.assertEqual(code, 403)                                      # clé TV absente
        self.assertEqual(tv_post(s, "nope", good)[0], 404)
        xescape.set_marker(a.cfg.run_dir, False)                         # X pas ouvert à la TV : aucune séance
        self.assertEqual(tv_post(s, "start", good), (409, {"ok": False, "error": "x_not_open"}))
        self.assertFalse(a.x_entry.active())

    def test_phone_flow_end_to_end_http(self):
        a, s = self.setup()
        sid = self.start(a, s)["sid"]
        # téléphone non appairé : invité à appairer d'abord, aucune clé
        code, h, page = self.preq(s, "GET", "/r/x?s=" + sid)
        self.assertEqual(code, 403)
        self.assertIn("Télécommande", page)
        tok, csrf = self.pair(a, s)
        code, h, page = self.preq(s, "GET", "/r/x?s=" + sid, cookie=tok)
        self.assertEqual(code, 200)
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("default-src 'none'", h["content-security-policy"])
        for need in ("Identifiant seulement — jamais mot de passe ou code", ">Envoyer<", ">Annuler<",
                     'autocomplete="off"', 'spellcheck="false"', 'autocapitalize="none"', 'autocorrect="off"',
                     '/static/muse.js', '/static/xphone.js', 'data-s="%s"' % sid, 'data-c="%s"' % csrf, 'lang="fr"'):
            self.assertIn(need, page)
        self.assertNotIn(JWK["n"], page)                                 # clé seulement par la réclamation (POST)
        self.assertIsNone(re.search(r'<input[^>]*\bname=', page))        # champ SANS nom : jamais dans un formulaire
        self.assertNotRegex(page, r'type="password"')
        for path in ("/static/xphone.js", "/static/muse.js"):
            self.assertEqual(self.preq(s, "GET", path, cookie=tok)[0], 200, path)
        # réclamation : Origin, en-tête dédié, CSRF, cookie
        body = {"s": sid, "c": csrf}
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", body, cookie=tok, origin=False)[0], 403)
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", body, cookie=tok, header=False)[0], 403)
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", {"s": sid, "c": "0" * 32}, cookie=tok)[0], 403)
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", body)[0], 401)
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", dict(body, x=1), cookie=tok)[0], 400)
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", body, cookie=tok, host="evil.example:80")[0], 421)
        code, _, out = self.preq(s, "POST", "/r/x/claim", body, cookie=tok)
        self.assertEqual((code, out), (200, {"ok": True, "n": JWK["n"], "e": "AQAB"}))
        tok2, csrf2 = self.pair(a, s)                                    # second téléphone appairé : refusé
        self.assertEqual(self.preq(s, "POST", "/r/x/claim", {"s": sid, "c": csrf2}, cookie=tok2)[2]["error"],
                         "claimed")
        ct = seal_doc(sid, "alex@example.com")
        code, _, out = self.preq(s, "POST", "/r/x/submit", dict(body, ct=ct), cookie=tok)
        self.assertEqual((code, out), (200, {"ok": True}))
        self.assertEqual(self.preq(s, "GET", "/r/x/status?s=" + sid, cookie=tok)[2], {"ok": True, "state": "sent"})
        self.assertEqual(self.preq(s, "GET", "/r/x/status?s=" + sid, cookie=tok2)[2], {"ok": True, "state": "gone"})
        code, out = tv_post(s, "poll", {"sid": sid})
        self.assertEqual((code, out), (200, {"ok": True, "state": "submitted", "ct": ct}))
        code, _, out = self.preq(s, "POST", "/r/x/cancel", body, cookie=tok)   # chiffré déjà remis : trop tard
        self.assertEqual((code, out), (409, {"ok": False, "error": "too_late", "state": "sent"}))
        self.assertTrue(a.x_entry.active())
        self.assertEqual(tv_post(s, "done", {"sid": sid, "result": "filled"}), (200, {"ok": True}))
        self.assertEqual(self.preq(s, "GET", "/r/x/status?s=" + sid, cookie=tok)[2],
                         {"ok": True, "state": "delivered"})
        self.assertFalse(a.x_entry.active())
        # Annuler tardif (après la livraison) : 409 « over » + issue réelle ; jamais « cancelled »
        code, _, out = self.preq(s, "POST", "/r/x/cancel", body, cookie=tok)
        self.assertEqual((code, out), (409, {"ok": False, "error": "over", "state": "delivered"}))
        self.assertEqual(self.preq(s, "GET", "/r/x/status?s=" + sid, cookie=tok)[2],
                         {"ok": True, "state": "delivered"})
        self.assertEqual(tv_post(s, "done", {"sid": sid, "result": "nope"})[0], 400)
        # aucune trace : journal, disque, vues TV/opérateur/agent
        secrets_ = (sid, ct, JWK["n"], csrf, "alex@example.com")
        blob = json.dumps(a.test_logs, ensure_ascii=False)
        for d in (a.cfg.state_dir, a.cfg.run_dir):
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        with open(os.path.join(root, f), "rb") as fh:
                            blob += fh.read().decode("utf-8", "replace")
                    except OSError:
                        pass
        blob += json.dumps(a.tv_state(), ensure_ascii=False) + json.dumps(xprobe.status_view(a))
        blob += json.dumps(a.agent_x_context()) + repr(a.x_entry)
        for sec in secrets_:
            self.assertNotIn(sec, blob)
        self.assertFalse(any("342" in json.dumps(x) or "length" in json.dumps(x) for x in a.test_logs))

    def test_tv_page_load_return_wipes_and_restart_forgets(self):
        a, s = self.setup()
        sid = self.start(a, s)["sid"]
        tok, csrf = self.pair(a, s)
        self.preq(s, "POST", "/r/x/claim", {"s": sid, "c": csrf}, cookie=tok)
        s.req("GET", "/tv")                                              # Retour Samsung : kiosque relancé -> /tv
        self.assertFalse(a.x_entry.active())
        self.assertEqual(self.preq(s, "GET", "/r/x/status?s=" + sid, cookie=tok)[2]["state"], "cancelled")
        xescape.set_marker(a.cfg.run_dir, True)
        self.start(a, s)
        self.assertTrue(a.x_entry.active())
        # redémarrage du service : même configuration, mêmes dossiers — aucune séance (mémoire seulement)
        b = appmod.App(a.cfg, runner=Net(), mono=Mono(), log=lambda *x, **y: None,
                       proc_root=os.path.join(os.path.dirname(a.cfg.state_dir), "proc"))
        self.addCleanup(b.close)
        self.assertFalse(b.x_entry.active())

    def test_phone_page_expired_or_unknown_session(self):
        a, s = self.setup()
        tok, csrf = self.pair(a, s)
        code, _, page = self.preq(s, "GET", "/r/x?s=" + "B" * 22, cookie=tok)
        self.assertEqual(code, 410)
        self.assertIn("QR", page)
        self.assertEqual(self.preq(s, "GET", "/r/x?s=bad", cookie=tok)[0], 410)
        code, _, out = self.preq(s, "POST", "/r/x/submit", {"s": "B" * 22, "c": csrf, "ct": "A" * 342}, cookie=tok)
        self.assertEqual((code, out), (410, {"ok": False, "error": "over", "state": "gone"}))


if __name__ == "__main__":
    unittest.main()
