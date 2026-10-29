"""« Connecter Muse » depuis la TV : consentement explicite, séance courte à usage unique, jeton SDK chiffré par le
téléphone, helper root dédié (jobs à champs fixes), progression monotone, annulation, déconnexion, reprise après plantage.

Adversarial : séance expirée/rejouée/croisée, CSRF/Origin/en-tête, jeton jamais dans une réponse/journal/audit/URL/QR,
jeton malformé/trop long/bidi, job lien symbolique/lien dur/propriétaire/mode/taille/schéma, courses d'annulation,
identité étrangère, double envoi, plantage du helper, aucun pilotage sans confirmation TV, aucune commande SDK
dangereuse, aucun fichier/shell générique.
"""
import configparser
import http.client
import io
import socket
import threading
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

from tests._common import PI4
from tests.test_service import find_chromium, make_app
from tests.test_remote import NODE, remote_servers, tmpdir
from tests import test_remote as tr
from tests import test_agents_manage as tam
from tests import test_muse_bridge as tmb
from opentv_pi import agents, config, museadmin, musebridge, musehelper, museseal, musesetup, museweb, tools

LAN = "192.168.2.40"
MDNS = tr.MDNS                                       # « opentv-test.local » : seul nom que le QR peut porter
assert LAN == tr.LAN_IP
TOKEN = "mgst_" + "Zx9_-" * 8 + "ab" + "w"          # 42 + 1 caractères après mgst_, dernier dans l'alphabet canonique
assert musesetup.SDK_TOKEN_RE.fullmatch(TOKEN)
CONTROL = ["control", "display", "feedback", "read"]
_TEMPLATE = museseal.PrivateKey()


def fast_key():
    """Clé RSA-2048 réelle, copiée d'un gabarit (la génération prend ~1 s) : chaque séance reçoit son objet propre."""
    k = object.__new__(museseal.PrivateKey)
    k.__dict__.update(_TEMPLATE.__dict__)
    return k


def other_key():
    return museseal.PrivateKey()


def wizard(tc, resolver=None, **env):
    """Service configuré + résolution mDNS simulée (par défaut, le nom désigne exactement l'adresse LAN)."""
    res = resolver or tr.Resolver()
    a = make_app(tc, app_kw={"hostname": tr.HOSTNAME + ".lan", "resolver": res}, **env)
    res.app = a
    a.test_resolver = res
    config.save_device(a.cfg, {"name": "Salon"})
    a.reload_device()
    a.muse.keygen, a.muse.async_keygen = fast_key, False
    s = remote_servers(tc, a)
    return a, s


def settle_mdns(a):
    """Vérification mDNS de l'ouverture terminée (thread joint), puis une vérification synchrone de plus."""
    if a._mdns_thread is not None:
        a._mdns_thread.join(5)
    a._mdns_check()


def tv(s, path, body):
    return s.req("POST", path, json.dumps(body), {"X-OpenTV-TV": s.app.tv_key, "Content-Type": "application/json"})


def start(tc, a, s):
    code, _, body = tv(s, "/api/tv/muse/start", {"consent": True})
    tc.assertEqual(code, 200, body)
    settle_mdns(a)
    v = a.muse_view()
    tc.assertTrue(v["active"])
    u = urllib.parse.urlsplit(v["url"])
    sid = urllib.parse.parse_qs(u.query)["s"][0]
    return sid, u.fragment, MPhone(tc, a, s)


def seal(jwk, doc):
    n, e = museseal.public_from_jwk(jwk)
    return museseal.b64u(museseal.encrypt(n, e, json.dumps(doc).encode("utf-8")))


class MPhone:
    def __init__(self, tc, a, s):
        self.tc, self.a, self.s = tc, a, s

    @property
    def port(self):
        return self.s.web.lan_port()

    @property
    def host(self):
        return "%s:%d" % (MDNS, self.port)

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": self.host if host is None else host}
        if isinstance(body, dict):
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def page(self, sid, step=1):
        return self.req("GET", "/muse?" + urllib.parse.urlencode({"s": sid, "step": step}))

    def jwk_csrf(self, sid):
        code, _, body = self.page(sid, 2)
        self.tc.assertEqual(code, 200, body)
        n = re.search(r'data-n="([A-Za-z0-9_-]+)"', body).group(1)
        e = re.search(r'data-e="([A-Za-z0-9_-]+)"', body).group(1)
        c = re.search(r'data-c="([0-9a-f]{32})"', body).group(1)
        return {"kty": "RSA", "n": n, "e": e}, c

    def post(self, path, body, origin=True, header=True, extra=None):
        h = {}
        if origin:
            h["Origin"] = "http://" + self.host
        if header:
            h["X-OpenTV-Muse"] = "1"
        h.update(extra or {})
        return self.req("POST", path, body, h)

    def submit(self, sid, f, tok=TOKEN, doc=None, c=None, **kw):
        jwk, csrf = self.jwk_csrf(sid)
        ct = seal(jwk, doc if doc is not None else {"v": 1, "s": sid, "f": f, "t": tok})
        code, _, body = self.post("/muse/submit", {"s": sid, "c": c or csrf, "ct": ct}, **kw)
        return code, json.loads(body), ct


def jobs(a):
    return sorted(os.listdir(a.cfg.muse_jobs_dir))


def read_job(a, name):
    with open(os.path.join(a.cfg.muse_jobs_dir, name), encoding="utf-8") as f:
        return json.load(f)


def result(a, jid, phase, ok=None, res=None, **extra):
    body = {"v": 1, "id": jid, "phase": phase}
    if phase == "done":
        body.update(ok=ok, result=res)
    body.update(extra)
    with open(os.path.join(a.cfg.muse_results_dir, jid + ".json"), "w", encoding="utf-8") as f:
        json.dump(body, f)


def put_box(a, owned=True, foreign=False, identity_present=None, tick=True):
    """État public de la boîte tel que le helper root le publie (box.json, aucun secret)."""
    with open(os.path.join(a.cfg.muse_results_dir, musesetup.BOX_NAME), "w", encoding="utf-8") as f:
        json.dump({"v": 1, "owned": owned, "foreign": foreign, "paired": bool(owned), "token": bool(owned),
                   "identity_present": identity_present, "at": 1.0}, f)
    if tick:
        a.tick()


def everything_text(a, s, *extra):
    """Tout ce qu'un observateur pourrait lire : journal, audit, état TV, fichiers résultats, pages."""
    parts = [json.dumps(a.test_logs, ensure_ascii=False), json.dumps(a.tv_state(), ensure_ascii=False)]
    if os.path.exists(a.cfg.audit_file):
        with open(a.cfg.audit_file, encoding="utf-8") as f:
            parts.append(f.read())
    for d in (a.cfg.muse_results_dir,):
        for n in os.listdir(d):
            with open(os.path.join(d, n), encoding="utf-8") as f:
                parts.append(f.read())
    return "\n".join(parts + [str(x) for x in extra])


# --------------------------------------------------------------------------- service + téléphone (HTTP réel)
class TestConsentAndSession(unittest.TestCase):
    def test_selecting_muse_grants_nothing_start_requires_exact_explicit_consent(self):
        a, s = wizard(self)
        before = a.registry.policy()
        for body in ({}, {"consent": "true"}, {"consent": 1}, {"consent": False}, {"consent": [True]}):
            code, _, out = tv(s, "/api/tv/muse/start", body)
            self.assertEqual(code, 409, (body, out))
            self.assertEqual(json.loads(out)["error"], "consent_required")
        self.assertFalse(a.muse_view()["active"])
        self.assertEqual(a.lan_purpose(), None)                 # aucune page LAN sans consentement
        self.assertEqual(a.registry.policy(), before)           # jamais d'autorisation implicite
        self.assertEqual(jobs(a), [])
        # sans clé TV (autre processus local) : refus
        code, _, _ = s.req("POST", "/api/tv/muse/start", json.dumps({"consent": True}),
                           {"Content-Type": "application/json"})
        self.assertEqual(code, 403)
        sid, f, ph = start(self, a, s)
        self.assertEqual(a.lan_purpose(), "muse")
        self.assertEqual(a.registry.policy(), before)           # la séance seule n'accorde toujours rien
        self.assertEqual(jobs(a), [])

    def test_tv_view_carries_only_qr_url_and_never_key_material(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        v = a.tv_state()["muse"]
        self.assertRegex(v["url"], r"^http://opentv-test\.local:\d+/muse\?s=[A-Za-z0-9_-]{22}#[A-Za-z0-9_-]{22}$")
        self.assertNotIn(LAN, json.dumps(v))                     # jamais d'adresse IP dans l'état Muse de la TV
        dump = json.dumps(a.tv_state())
        k = a.muse.cur.key
        for secret in (museseal.b64u(k.n.to_bytes(256, "big")), str(k._d), a.muse.csrf(a.muse.cur)):
            self.assertNotIn(secret, dump)
        self.assertEqual(v["state"], "await_token")
        self.assertIsNone(v["bridge"])

    def test_phone_pages_headers_three_steps_fail_closed_and_no_secret(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        for step in (1, 2, 3):
            code, h, body = ph.page(sid, step)
            self.assertEqual(code, 200, body)
            self.assertEqual(h["cache-control"], "no-store")
            self.assertEqual(h["referrer-policy"], "no-referrer")
            self.assertIn("connect-src 'self'", h["content-security-policy"])
            self.assertNotIn("unsafe-inline", h["content-security-policy"])
            self.assertIn('<meta name="referrer" content="no-referrer">', body)
            self.assertNotIn(f, body)                            # secret du QR : jamais servi
            self.assertNotIn("mgst_", body)
            self.assertNotIn("sudo", body)
            self.assertNotIn("ssh", body.lower())
            self.assertIn("Étape %d sur 3" % (1 if step == 3 else step), body)   # étape 3 : seulement après le jeton
        _, _, p1 = ph.page(sid, 1)
        self.assertIn('href="%s"' % museweb.OFFICIAL_TOKEN_PAGE, p1)
        self.assertIn('rel="noopener noreferrer"', p1)
        self.assertEqual(p1.count('class="primary"'), 1)        # une action principale…
        self.assertRegex(p1, r'<a class="primary" id="next" href="[^"]*step=2">Utiliser mon jeton existant</a>')
        self.assertNotRegex(p1, r'class="primary"[^>]*id="official"')   # …qui réutilise le jeton existant
        # un seul jeton par compte, valable pour plusieurs appareils : réutiliser, jamais en créer un second
        self.assertIn("valable pour plusieurs appareils (jusqu'à 50)", p1)
        self.assertIn("inutile d'en créer un autre", p1)
        self.assertIn("Ne révoque pas ce jeton\u00a0: tes autres gadgets Muse seraient déconnectés.", p1)
        self.assertIn("ne peut pas être recopié de là", p1)     # début masqué : note de reprise honnête
        _, _, p2 = ph.page(sid, 2)
        inp = re.search(r"<input[^>]*>", p2).group(0)
        self.assertIn('type="password"', inp)
        self.assertNotIn(" name=", inp)                          # jamais envoyé par un formulaire
        self.assertIn("le même que pour tes autres appareils Muse", p2)
        self.assertRegex(p2, r'<form id="seal"[^>]*hidden')       # caché tant que muse.js n'a rien vérifié
        self.assertIn("<noscript>", p2)
        self.assertEqual(p2.count('class="primary"'), 1)
        # aucune requête GET/POST sans JavaScript ne transporte de jeton : /muse/nojs ne lit rien
        code, _, body = ph.req("POST", "/muse/nojs", "tok=" + TOKEN, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 400)
        self.assertNotIn(TOKEN, body)
        self.assertEqual(jobs(a), [])
        # étranger : Host, pair, chemin
        self.assertEqual(ph.req("GET", "/muse?s=" + sid, host="evil.example:%d" % ph.port)[0], 421)
        self.assertEqual(ph.req("GET", "/setup?t=x")[0], 404)
        self.assertEqual(ph.req("GET", "/assistants?t=x")[0], 404)
        self.assertEqual(ph.req("GET", "/static/tv.js")[0], 404)

    def test_full_submit_writes_exactly_one_private_job_and_never_echoes_the_token(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        code, out, ct = ph.submit(sid, f)
        self.assertEqual((code, out), (200, {"ok": True, "state": "submitted"}))
        names = jobs(a)
        self.assertEqual(len(names), 1)
        p = os.path.join(a.cfg.muse_jobs_dir, names[0])
        st = os.lstat(p)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink), (0o600, 1))
        j = read_job(a, names[0])
        self.assertEqual(set(j), {"v", "id", "action", "consent", "created", "token"})
        self.assertEqual((j["action"], j["consent"], j["token"]), ("pair", "tv", TOKEN))
        self.assertEqual(os.listdir(a.cfg.muse_jobs_tmp), [])
        # double envoi (double toucher, rechargement) : jamais un second job ni un nouvel essai
        jwk = {"kty": "RSA", "n": museseal.b64u(_TEMPLATE.n.to_bytes(256, "big")), "e": "AQAB"}
        c = a.muse.csrf(a.muse.cur)
        code2, _, body2 = ph.post("/muse/submit", {"s": sid, "c": c, "ct": seal(jwk, {"v": 1, "s": sid, "f": f,
                                                                                       "t": TOKEN})})
        self.assertEqual((code2, json.loads(body2)["error"]), (409, "already_submitted"))
        code3, _, body3 = ph.post("/muse/submit", {"s": sid, "c": c, "ct": ct})   # rejeu exact
        self.assertEqual(code3, 409)
        self.assertEqual(len(jobs(a)), 1)
        # clé privée et secret f effacés dès la remise : un rejeu ne peut même plus être déchiffré
        self.assertIsNone(a.muse.cur.key)
        self.assertIsNone(a.muse.cur.f)
        # jamais dans une réponse, l'état TV, le journal, l'audit ni les pages
        _, _, page3 = ph.page(sid, 3)
        _, _, status = ph.req("GET", "/muse/status?s=" + sid)
        txt = everything_text(a, s, out, body2, body3, page3, status)
        self.assertNotIn(TOKEN, txt)
        self.assertNotIn(TOKEN[5:20], txt)
        self.assertNotIn(f, txt)
        self.assertNotIn(ct, txt)

    def test_csrf_origin_header_content_type_and_size_are_enforced_before_anything(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        jwk, c = ph.jwk_csrf(sid)
        good = {"s": sid, "c": c, "ct": seal(jwk, {"v": 1, "s": sid, "f": f, "t": TOKEN})}
        cases = [
            (dict(good, c="0" * 32), {}, 403),
            (good, {"origin": False}, 403),                                  # ni Origin ni Sec-Fetch-Site
            (good, {"extra": {"Origin": "http://evil.example"}}, 403),
            (good, {"origin": False, "extra": {"Origin": "null"}}, 403),       # null sans Sec-Fetch-Site
            (good, {"origin": False, "extra": {"Origin": "null", "Sec-Fetch-Site": "cross-site"}}, 403),
            (good, {"header": False}, 403),                                  # pas d'en-tête X-OpenTV-Muse
            (dict(good, extra="x"), {}, 400),
        ]
        for body, kw, want in cases:
            extra = kw.pop("extra", None)
            if extra and "Origin" in extra:
                kw["origin"] = False
            code, _, out = ph.post("/muse/submit", body, extra=extra, **kw)
            self.assertEqual(code, want, (body.keys(), kw, extra, out))
        code, _, _ = ph.req("POST", "/muse/submit", urllib.parse.urlencode(good),
                            {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://" + ph.host,
                             "X-OpenTV-Muse": "1"})
        self.assertEqual(code, 415)
        code, _, _ = ph.req("POST", "/muse/submit", "{" + " " * 3000 + "}",
                            {"Content-Type": "application/json", "Origin": "http://" + ph.host, "X-OpenTV-Muse": "1"})
        self.assertEqual(code, 413)
        self.assertEqual(jobs(a), [])
        self.assertEqual(a.muse.cur.state, "await_token")
        # Origin « null » + Sec-Fetch-Site same-origin (navigateur en no-referrer) : accepté
        code, out, _ = ph.submit(sid, f, origin=False, extra={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(code, 200, out)

    def test_cross_session_expired_and_replayed_sessions_are_refused(self):
        a, s = wizard(self)
        sid1, f1, ph = start(self, a, s)
        jwk1, c1 = ph.jwk_csrf(sid1)
        ct1 = seal(jwk1, {"v": 1, "s": sid1, "f": f1, "t": TOKEN})
        a.muse.keygen = other_key
        tv(s, "/api/tv/muse/start", {"consent": True})            # nouvelle séance : l'ancienne est annulée
        v = a.muse_view()
        u = urllib.parse.urlsplit(v["url"])
        sid2, f2 = urllib.parse.parse_qs(u.query)["s"][0], u.fragment
        self.assertNotEqual(sid1, sid2)
        self.assertEqual(ph.page(sid1, 1)[0], 403)               # ancienne séance : refusée
        code, _, out = ph.post("/muse/submit", {"s": sid1, "c": c1, "ct": ct1})
        self.assertEqual((code, json.loads(out)["error"]), (403, "expired"))
        jwk2, c2 = ph.jwk_csrf(sid2)
        # chiffré de la séance 1 présenté à la séance 2 (autre clé) : refusé, compté comme essai
        code, _, out = ph.post("/muse/submit", {"s": sid2, "c": c2, "ct": ct1})
        self.assertEqual((code, json.loads(out)["error"]), (400, "sealed_rejected"))
        # bonne clé mais document lié à la séance 1 (s/f) : refusé
        for doc in ({"v": 1, "s": sid1, "f": f2, "t": TOKEN}, {"v": 1, "s": sid2, "f": f1, "t": TOKEN}):
            code, _, out = ph.post("/muse/submit", {"s": sid2, "c": c2, "ct": seal(jwk2, doc)})
            self.assertEqual(code, 400, out)
        # CSRF de la séance 1 sur la séance 2 : refusé
        code, _, _ = ph.post("/muse/submit", {"s": sid2, "c": c1, "ct": seal(jwk2, {"v": 1, "s": sid2, "f": f2,
                                                                                    "t": TOKEN})})
        self.assertEqual(code, 403)
        self.assertEqual(jobs(a), [])
        # expiration (≤ 10 min) : QR, page et envoi refusés, la TV l'annonce
        a.mono_clock.t += musesetup.SESSION_TTL_S + 1
        self.assertLessEqual(musesetup.SESSION_TTL_S, 600)
        a.tick()
        code, _, body = ph.page(sid2, 2)                          # l'issue reste lisible un court instant…
        self.assertEqual(code, 200)
        self.assertIn("QR expiré", body)
        self.assertNotIn("data-n=", body)                           # …mais plus aucun champ ni clé
        self.assertEqual(ph.post("/muse/submit", {"s": sid2, "c": c2, "ct": seal(jwk2, {"v": 1, "s": sid2, "f": f2,
                                                                                       "t": TOKEN})})[0], 403)
        self.assertEqual(a.muse_view()["state"], "expired")
        self.assertEqual(a.muse_view()["last"]["state"], "expired")
        a.mono_clock.t += musesetup.LINGER_S + 1
        self.assertIsNone(a.lan_purpose())                        # page LAN fermée ensuite

    def test_malformed_oversize_bidi_tokens_and_attempt_budget(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        bad = [TOKEN + " ", " " + TOKEN, TOKEN[:-1], TOKEN + "A", TOKEN.replace("Z", "\u202e", 1),
               TOKEN.replace("x", "\u200b", 1), "MGST_" + TOKEN[5:], TOKEN[:-1] + "B", "x" * 100]
        for i, t in enumerate(bad[:musesetup.MAX_ATTEMPTS - 1]):
            code, out, _ = ph.submit(sid, f, tok=t)
            self.assertEqual((code, out["error"]), (400, "invalid_token"), repr(t))
            self.assertNotIn(t, json.dumps(out))
        code, out, _ = ph.submit(sid, f, tok=bad[-1])
        self.assertEqual((code, out["error"]), (403, "too_many_attempts"))
        self.assertEqual(a.muse_view()["last"]["state"], "failed")
        self.assertEqual(jobs(a), [])
        # documents non conformes : clés en trop/manquantes, mauvaise version, non-JSON, chiffré trop long
        a.muse.keygen = other_key
        sid, f, ph = start(self, a, s)
        jwk, c = ph.jwk_csrf(sid)
        n, e = museseal.public_from_jwk(jwk)
        for raw in (json.dumps({"v": 1, "s": sid, "f": f, "t": TOKEN, "argv": ["sh"]}).encode(),
                    json.dumps({"v": 2, "s": sid, "f": f, "t": TOKEN}).encode(), b"not json"):
            ct = museseal.b64u(museseal.encrypt(n, e, raw))
            code, _, out = ph.post("/muse/submit", {"s": sid, "c": c, "ct": ct})
            self.assertEqual((code, json.loads(out)["error"]), (400, "sealed_rejected"))
        code, _, out = ph.post("/muse/submit", {"s": sid, "c": c, "ct": "A" * 900})   # trop long : 4e essai
        self.assertEqual((code, json.loads(out)["error"]), (400, "sealed_rejected"))
        code, _, out = ph.post("/muse/submit", {"s": sid, "c": c, "ct": "A" * 342})
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(out)["error"], "too_many_attempts")
        self.assertEqual(jobs(a), [])

    def test_wrong_session_ids_are_rate_limited_per_ip(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        for i in range(30):
            self.assertEqual(ph.req("GET", "/muse?s=%s" % ("A" * 22))[0], 403)
        self.assertEqual(ph.req("GET", "/muse?s=" + sid)[0], 429)   # même la bonne séance attend
        self.assertEqual(ph.req("GET", "/muse?s=" + sid + "&s=" + sid)[0], 429)


class TestProgressCancelAndDisconnect(unittest.TestCase):
    def submitted(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        self.assertEqual(ph.submit(sid, f)[0], 200)
        return a, s, sid, ph, jobs(a)[0][:-5]

    def test_cancel_before_pickup_withdraws_job_and_token(self):
        a, s, sid, ph, jid = self.submitted()
        code, _, body = tv(s, "/api/tv/muse/cancel", {})
        self.assertEqual(code, 200, body)
        self.assertEqual(jobs(a), [])                              # le jeton disparaît avec le job
        self.assertEqual(a.muse_view()["last"]["state"], "cancelled")
        self.assertEqual(os.listdir(a.cfg.muse_jobs_tmp), [])

    def test_cancel_after_pickup_asks_the_helper_and_never_reports_early(self):
        a, s, sid, ph, jid = self.submitted()
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))   # pris par le helper
        result(a, jid, "preparing")
        a.tick()
        tv(s, "/api/tv/muse/cancel", {})
        names = jobs(a)
        self.assertEqual(len(names), 1)
        j = read_job(a, names[0])
        self.assertEqual((j["action"], j["target"]), ("cancel", jid))
        self.assertEqual(set(j), {"v", "id", "action", "target", "created"})
        self.assertTrue(a.muse_view()["cancelling"])
        self.assertTrue(a.muse_view()["active"])                    # rien d'annoncé avant l'issue du helper
        tv(s, "/api/tv/muse/cancel", {})
        self.assertEqual(len(jobs(a)), 1)                           # une seule demande
        result(a, jid, "done", ok=False, res="cancelled")
        a.tick()
        self.assertEqual(a.muse_view()["last"]["state"], "cancelled")
        # issue « connectée » arrivée malgré l'annulation (trop tard) : dite telle quelle, jamais révoquée ici
        a2, s2, sid2, ph2, jid2 = self.submitted()
        os.remove(os.path.join(a2.cfg.muse_jobs_dir, jid2 + ".json"))
        result(a2, jid2, "waiting_muse")
        a2.tick()
        tv(s2, "/api/tv/muse/cancel", {})
        result(a2, jid2, "done", ok=True, res="connected")
        a2.tick()
        self.assertEqual(a2.muse_view()["last"]["state"], "connected")

    def test_progress_is_monotonic_and_foreign_or_late_results_are_ignored(self):
        a, s, sid, ph, jid = self.submitted()
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))
        result(a, "f" * 16, "done", ok=True, res="connected")       # autre job : ignoré
        a.tick()
        self.assertEqual(a.muse_view()["state"], "submitted")
        result(a, jid, "connecting")
        a.tick()
        result(a, jid, "preparing")                                 # retour en arrière : ignoré
        a.tick()
        self.assertEqual(a.muse_view()["state"], "connecting")
        with open(os.path.join(a.cfg.muse_results_dir, jid + ".json"), "w") as f:
            json.dump({"v": 1, "id": jid, "phase": "done", "ok": True, "result": "connected", "token": TOKEN,
                       "ble_name": "MuseGadget0A0B0C; rm -rf /"}, f)
        a.tick()
        v = a.muse_view()
        self.assertEqual(v["last"]["state"], "connected")
        self.assertEqual(v["message"], "Muse est connectée.")
        self.assertNotIn(TOKEN, json.dumps(a.tv_state()))
        result(a, jid, "done", ok=False, res="helper_error")        # final : immuable
        a.tick()
        self.assertEqual(a.muse_view()["last"]["state"], "connected")
        _, _, st = ph.req("GET", "/muse/status?s=" + sid)
        self.assertEqual(json.loads(st)["state"], "connected")

    def test_helper_that_never_picks_up_fails_honestly_and_withdraws(self):
        a, s, sid, ph, jid = self.submitted()
        a.mono_clock.t += musesetup.PICKUP_S + 1
        a.tick()
        self.assertEqual(jobs(a), [])
        self.assertEqual(a.muse_view()["last"]["error"], "helper_unavailable")
        # pris mais jamais fini : échéance globale bornée
        a, s, sid, ph, jid = self.submitted()
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))
        result(a, jid, "waiting_muse", ble_name="MuseGadget0A0B0C")
        a.tick()
        self.assertEqual(a.muse_view()["ble_name"], "MuseGadget0A0B0C")
        a.mono_clock.t += musesetup.PAIR_BUDGET_S + 1
        a.tick()
        self.assertEqual(a.muse_view()["last"]["error"], "timeout")

    def test_human_priority_agents_mode_and_setup_exclusivity(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        with self.assertRaises(tools.ToolError) as cm:
            a.agent_control("muse-device", "remote_control", {"key": "ok"})
        self.assertEqual(str(cm.exception.args[0]), "tv_busy")
        self.assertEqual(a.request_agents(), (False, "busy"))      # un seul QR LAN à la fois
        a.cancel_agents("tv")
        tv(s, "/api/tv/muse/cancel", {})
        ok, _ = a.request_agents()
        self.assertTrue(ok)
        start(self, a, s)                                            # Muse ferme la gestion des assistants
        self.assertFalse(a.agents_mode)
        a.request_setup()                                            # Reconfigurer : QR Muse inutilisé fermé
        a.tick()
        self.assertFalse(a.muse_view()["active"])
        code, _, out = tv(s, "/api/tv/muse/start", {"consent": True})
        self.assertEqual((code, json.loads(out)["error"]), (409, "setup"))

    def test_phone_cancel_only_for_its_own_session(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        _, c = ph.jwk_csrf(sid)
        code, _, _ = ph.post("/muse/cancel", {"s": "B" * 22, "c": c})
        self.assertEqual(code, 403)
        self.assertTrue(a.muse_view()["active"])
        code, _, out = ph.post("/muse/cancel", {"s": sid, "c": c})
        self.assertEqual((code, json.loads(out)["state"]), (200, "cancelled"))

    def test_disconnect_requires_explicit_confirmation_then_one_unpair_job(self):
        a, s = wizard(self)
        put_box(a, owned=True, identity_present=False)
        for body in ({}, {"confirm": "yes"}, {"confirm": 1}):
            code, _, _ = tv(s, "/api/tv/muse/disconnect", body)
            self.assertEqual(code, 409)
        self.assertEqual(jobs(a), [])
        code, _, out = tv(s, "/api/tv/muse/disconnect", {"confirm": True})
        self.assertEqual(code, 200, out)
        j = read_job(a, jobs(a)[0])
        self.assertEqual(set(j), {"v", "id", "action", "confirm", "created"})
        self.assertEqual((j["action"], j["confirm"]), ("unpair", "tv"))
        self.assertEqual(tv(s, "/api/tv/muse/disconnect", {"confirm": True})[0], 409)   # une à la fois
        self.assertEqual(tv(s, "/api/tv/muse/start", {"consent": True})[0], 409)        # jamais pendant
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jobs(a)[0]))
        result(a, j["id"], "done", ok=True, res="disconnected")
        a.tick()
        v = a.muse_view()
        self.assertEqual(v["disconnect"]["state"], "done")
        self.assertEqual(v["disconnect"]["message"], "Muse est déconnectée.")
        self.assertGreater(v["disconnect_last"]["seq"], 0)

    def test_offline_or_unconfigured_never_opens_the_page(self):
        from tests.test_service import Net, OFF_DEV
        a = make_app(self, net=Net(dev=OFF_DEV, ip=""))
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.tick()
        self.assertEqual(a.muse_start(True), (False, "offline"))
        b = make_app(self)
        b.tick()
        self.assertEqual(b.muse_start(True), (False, "setup"))


# --------------------------------------------------------------------------- QR : nom mDNS vérifié, jamais d'IP
class TestQrHostIsVerifiedMdnsOnly(unittest.TestCase):
    def assert_no_ip(self, a, *extra):
        for blob in (json.dumps(a.muse_view()), json.dumps(a.tv_state()["muse"])) + extra:
            self.assertNotIn(LAN, blob)
            self.assertNotRegex(blob, r"https?://\d+\.\d+\.\d+\.\d+")

    def test_verified_mdns_name_is_the_only_qr_host_and_ip_host_is_refused(self):
        a, s = wizard(self)
        code, _, body = tv(s, "/api/tv/muse/start", {"consent": True})
        self.assertEqual(code, 200, body)
        self.assertNotIn(LAN, body)                                  # réponse au kiosque : jamais l'adresse
        settle_mdns(a)
        a.tick()
        v = a.muse_view()
        u = urllib.parse.urlsplit(v["url"])
        self.assertEqual((u.scheme, u.hostname), ("http", MDNS))   # ce que l'appareil photo de l'iPhone affiche
        self.assert_no_ip(a)
        sid = urllib.parse.parse_qs(u.query)["s"][0]
        ph = MPhone(self, a, s)
        for step in (1, 2, 3):
            code, _, page = ph.page(sid, step)
            self.assertEqual(code, 200)
            self.assertNotIn(LAN, page)
        for host in ("%s:%d" % (LAN, ph.port), "127.0.0.1:%d" % ph.port, "other-box.local:%d" % ph.port,
                     "%s:%d" % (MDNS, ph.port + 1), MDNS, "evil.example:%d" % ph.port):
            self.assertEqual(ph.req("GET", "/muse?s=" + sid, host=host)[0], 421, host)
        self.assertEqual(ph.req("GET", "/muse?s=" + sid, host="%s:%d" % (MDNS.upper(), ph.port))[0], 200)
        # POST : Origin à IP refusée (le téléphone n'a jamais vu cette origine)
        jwk, c = ph.jwk_csrf(sid)
        code, _, _ = ph.post("/muse/submit", {"s": sid, "c": c, "ct": "x"},
                             extra={"Origin": "http://%s:%d" % (LAN, ph.port)})
        self.assertEqual(code, 403)

    def check_fail_closed(self, answer):
        res = tr.Resolver()
        res.answer = answer
        a, s = wizard(self, resolver=res)
        code, _, body = tv(s, "/api/tv/muse/start", {"consent": True})
        self.assertEqual(code, 200, body)
        self.assertIsNone(a.muse_view()["url"])                     # nom pas vérifié : aucun QR, jamais l'IP
        settle_mdns(a)                                               # vérification fraîche : démentie
        a.tick()
        v = a.muse_view()
        self.assertFalse(v["active"])
        self.assertIsNone(v["url"])
        self.assertEqual((v["last"]["state"], v["last"]["error"]), ("failed", "name_unavailable"))
        self.assertEqual(v["last"]["message"], musesetup.message_for("name_unavailable"))
        self.assertIn("joignable par son nom", v["last"]["message"])
        self.assertNotIn("192", v["last"]["message"])
        self.assertEqual(jobs(a), [])
        self.assert_no_ip(a, json.dumps(a.test_logs))
        return a

    def test_mdns_conflict_fails_closed_without_qr(self):
        self.check_fail_closed({"192.168.2.99"})                     # autre appareil (avahi : « -2 »)

    def test_mdns_ambiguous_fails_closed_without_qr(self):
        self.check_fail_closed({LAN, "192.168.2.99"})

    def test_mdns_unavailable_fails_closed_without_qr(self):
        self.check_fail_closed(set())

    def test_name_never_verified_in_time_fails_closed(self):
        class Slow(tr.Resolver):
            gate = threading.Event()

            def __call__(self, name):
                self.gate.wait(10)
                return super().__call__(name)
        res = Slow()
        self.addCleanup(res.gate.set)
        a, s = wizard(self, resolver=res)
        self.assertFalse(a.mdns["ok"])
        self.assertEqual(tv(s, "/api/tv/muse/start", {"consent": True})[0], 200)
        a.tick()
        self.assertTrue(a.muse_view()["active"])
        self.assertIsNone(a.muse_view()["url"])
        a.mono_clock.t += musesetup.HOST_WAIT_S + 1
        a.tick()
        v = a.muse_view()
        self.assertEqual((v["active"], v["url"], v["last"]["error"]), (False, None, "name_unavailable"))
        self.assert_no_ip(a)

    def test_mdns_disabled_refuses_to_open_any_qr(self):
        a, s = wizard(self, OPENTV_REMOTE_MDNS="0")
        code, _, body = tv(s, "/api/tv/muse/start", {"consent": True})
        out = json.loads(body)
        self.assertEqual((code, out["error"]), (409, "name_unavailable"))
        self.assertEqual(out["message"], musesetup.message_for("name_unavailable"))
        self.assertFalse(a.muse_view()["active"])
        self.assertIsNone(a.lan_purpose())
        self.assertNotIn(LAN, body)

    def test_tv_view_refuses_any_non_mdns_host(self):
        ms = musesetup.MuseSetup(config.Config({}), keygen=fast_key, async_keygen=False)
        self.assertEqual(ms.start(True), (True, None))
        for host in (LAN, "10.0.0.2", "box.lan", "", None):
            self.assertIsNone(ms.tv_view(host, 8081)["url"], host)
        self.assertTrue(ms.tv_view(MDNS, 8081)["url"].startswith("http://%s:8081/muse?s=" % MDNS))


# --------------------------------------------------------------------------- annulation tardive : jamais un faux « rien »
class TestLateCancelIsReportedTruthfully(unittest.TestCase):
    def picked(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        self.assertEqual(ph.submit(sid, f)[0], 200)
        jid = jobs(a)[0][:-5]
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))   # pris par le helper
        result(a, jid, "connecting")                                     # pont en cours : annulation pas lue
        a.tick()
        tv(s, "/api/tv/muse/cancel", {})
        a.mono_clock.t += musesetup.CANCEL_WAIT_S + 1
        a.tick()
        return a, s, sid, ph, jid

    def assert_pending(self, a, ph, sid):
        v = a.muse_view()
        self.assertTrue(v["active"])
        self.assertTrue(v["cancelling"] and v["cancel_pending"])
        self.assertEqual(v["label"], "Annulation demandée")
        self.assertIsNone(v["message"])
        self.assertNotEqual(v["last"]["state"], "cancelled")           # rien annoncé sans issue réelle
        self.assertNotIn("rien n'a changé", json.dumps(v, ensure_ascii=False))
        st = json.loads(ph.req("GET", "/muse/status?s=" + sid)[2])
        self.assertEqual((st["state"], st["label"]), ("connecting", "Annulation demandée"))

    def test_unresolved_cancel_says_requested_then_late_connected_is_announced(self):
        a, s, sid, ph, jid = self.picked()
        self.assert_pending(a, ph, sid)
        seq = a.muse_view()["last"]["seq"]
        result(a, jid, "done", ok=True, res="connected")                 # le pont a fini avant de lire l'annulation
        a.tick()
        v = a.muse_view()
        self.assertGreater(v["last"]["seq"], seq)
        self.assertEqual(v["last"]["state"], "connected")              # vérité : Muse EST connectée

    def test_unresolved_cancel_then_helper_confirms_cancelled(self):
        a, s, sid, ph, jid = self.picked()
        self.assert_pending(a, ph, sid)
        result(a, jid, "done", ok=False, res="cancelled")
        a.tick()
        v = a.muse_view()
        self.assertEqual((v["last"]["state"], v["last"]["message"]), ("cancelled", "Annulé : rien n'a changé."))

    def test_no_outcome_until_global_deadline_is_unconfirmed_then_reconciled(self):
        a, s, sid, ph, jid = self.picked()
        a.mono_clock.t += musesetup.PAIR_BUDGET_S
        a.tick()
        v = a.muse_view()
        self.assertEqual((v["active"], v["last"]["state"], v["last"]["error"]),
                         (False, "failed", "cancel_unconfirmed"))
        self.assertNotIn("rien n'a changé", v["last"]["message"])
        self.assertIn("Annulation demandée", v["last"]["message"])
        seq = v["last"]["seq"]
        result(a, jid, "done", ok=True, res="connected")                 # issue tardive : réconciliée
        a.tick()
        v = a.muse_view()
        self.assertGreater(v["last"]["seq"], seq)
        self.assertEqual((v["last"]["state"], v["last"]["late"]), ("connected", True))
        result(a, jid, "done", ok=False, res="helper_error")             # une seule fois
        a.tick()
        self.assertEqual(a.muse_view()["last"]["state"], "connected")

    def test_unconfirmed_cancel_then_late_paired_incomplete_is_never_cancelled(self):
        a, s, sid, ph, jid = self.picked()
        a.mono_clock.t += musesetup.PAIR_BUDGET_S
        a.tick()
        seq = a.muse_view()["last"]["seq"]
        self.assertEqual(a.muse_view()["last"]["error"], "cancel_unconfirmed")
        result(a, jid, "done", ok=False, res="paired_incomplete")         # reprise du helper : appairée
        a.tick()
        v = a.muse_view()
        self.assertGreater(v["last"]["seq"], seq)
        self.assertEqual((v["last"]["state"], v["last"]["error"], v["last"]["late"]),
                         ("failed", "paired_incomplete", True))
        self.assertEqual(v["last"]["message"], musesetup.message_for("paired_incomplete"))
        self.assertNotIn("rien n'a changé", v["last"]["message"])
        result(a, jid, "done", ok=False, res="cancelled")                 # une seule fois : jamais rétrogradé
        a.tick()
        self.assertEqual(a.muse_view()["last"]["error"], "paired_incomplete")

    def test_unconfirmed_cancel_then_late_clean_interruption_is_cancelled(self):
        a, s, sid, ph, jid = self.picked()
        a.mono_clock.t += musesetup.PAIR_BUDGET_S
        a.tick()
        result(a, jid, "done", ok=False, res="interrupted")
        a.tick()
        v = a.muse_view()
        self.assertEqual((v["last"]["state"], v["last"]["message"]), ("cancelled", "Annulé : rien n'a changé."))

    def test_live_paired_incomplete_with_or_without_cancel_says_paired(self):
        for cancel, pending in ((False, False), (True, False), (True, True)):
            a, s = wizard(self)
            sid, f, ph = start(self, a, s)
            ph.submit(sid, f)
            jid = jobs(a)[0][:-5]
            os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))
            result(a, jid, "connecting")
            a.tick()
            if cancel:
                tv(s, "/api/tv/muse/cancel", {})
            if pending:
                a.mono_clock.t += musesetup.CANCEL_WAIT_S + 1
                a.tick()
                self.assertTrue(a.muse_view()["cancel_pending"])
            result(a, jid, "done", ok=False, res="paired_incomplete")
            a.tick()
            v = a.muse_view()
            self.assertEqual((v["active"], v["last"]["state"], v["last"]["error"]),
                             (False, "failed", "paired_incomplete"), (cancel, pending))
            self.assertIn("Muse est appairée", v["last"]["message"])
            self.assertNotIn("rien n'a changé", json.dumps(v, ensure_ascii=False))
            st = json.loads(ph.req("GET", "/muse/status?s=" + sid)[2])     # téléphone : même vérité
            self.assertEqual((st["state"], st["message"]), ("failed", musesetup.message_for("paired_incomplete")))
            code, _, page = ph.page(sid, 3)
            self.assertEqual(code, 200)
            self.assertIn("Muse est appairée", page)
            self.assertNotIn("rien n", page)
            self.assertNotIn(TOKEN, everything_text(a, s, page))

    def test_live_interruption_without_cancel_is_not_called_cancelled(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        ph.submit(sid, f)
        jid = jobs(a)[0][:-5]
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))
        result(a, jid, "waiting_muse")
        a.tick()
        result(a, jid, "done", ok=False, res="interrupted")                # reprise propre après plantage
        a.tick()
        v = a.muse_view()
        self.assertEqual((v["last"]["state"], v["last"]["error"]), ("failed", "interrupted"))
        self.assertEqual(v["last"]["message"], musesetup.message_for("interrupted"))

    def test_race_result_arriving_with_the_timeout_tick_wins_over_pending(self):
        a, s = wizard(self)
        sid, f, ph = start(self, a, s)
        ph.submit(sid, f)
        jid = jobs(a)[0][:-5]
        os.remove(os.path.join(a.cfg.muse_jobs_dir, jid + ".json"))
        result(a, jid, "waiting_muse")
        a.tick()
        tv(s, "/api/tv/muse/cancel", {})
        a.mono_clock.t += musesetup.CANCEL_WAIT_S + 1
        result(a, jid, "done", ok=False, res="cancelled")                 # même tick que l'échéance : l'issue d'abord
        a.tick()
        self.assertEqual(a.muse_view()["last"]["state"], "cancelled")


# --------------------------------------------------------------------------- « Déconnecter Muse » : propriété de la boîte
class TestDisconnectNeedsBoxOwnership(unittest.TestCase):
    def test_disconnect_only_when_the_box_owns_muse(self):
        a, s = wizard(self)
        self.assertIsNone(a.muse_view()["owned"])                        # pas d'état publié : rien proposé
        code, _, out = tv(s, "/api/tv/muse/disconnect", {"confirm": True})
        self.assertEqual((code, json.loads(out)["error"]), (409, "not_owned"))
        put_box(a, owned=False, foreign=True, identity_present=False)    # identité étrangère : jamais
        self.assertEqual((a.muse_view()["owned"], a.muse_view()["foreign"]), (False, True))
        self.assertEqual(tv(s, "/api/tv/muse/disconnect", {"confirm": True})[0], 409)
        self.assertEqual(jobs(a), [])
        a.registry.set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
        a.registry.issue("muse-device", ["read"])                        # pont en pause, appairé à la boîte
        put_box(a, owned=True, identity_present=False)                   # instantané périmé (identité apparue)
        self.assertIsNone(a.muse_view()["owned"])
        self.assertEqual(tv(s, "/api/tv/muse/disconnect", {"confirm": True})[0], 409)
        put_box(a, owned=True, identity_present=True)
        v = a.muse_view()
        self.assertEqual((v["owned"], v["bridge"]), (True, "paused"))
        code, _, out = tv(s, "/api/tv/muse/disconnect", {"confirm": True})
        self.assertEqual(code, 200, out)
        self.assertEqual(read_job(a, jobs(a)[0])["action"], "unpair")

    def test_probe_is_a_fixed_schema_job_and_bounded(self):
        a, s = wizard(self)
        code, _, out = tv(s, "/api/tv/muse/probe", {})
        self.assertEqual(code, 200, out)
        names = jobs(a)
        self.assertEqual(len(names), 1)
        j = read_job(a, names[0])
        self.assertEqual((set(j), j["action"]), ({"v", "id", "action", "created"}, "probe"))
        for _ in range(3):
            tv(s, "/api/tv/muse/probe", {})
        self.assertEqual(len(jobs(a)), 1)                                # au plus une par PROBE_MIN_S
        a.mono_clock.t += musesetup.PROBE_MIN_S + 1
        tv(s, "/api/tv/muse/probe", {})
        self.assertEqual(len(jobs(a)), 2)

    def test_box_file_anomalies_offer_nothing(self):
        a, s = wizard(self)
        p = os.path.join(a.cfg.muse_results_dir, musesetup.BOX_NAME)
        for body in ("{", "[]", json.dumps({"v": 2, "owned": True}), json.dumps({"v": 1, "owned": "yes"}),
                     " " * (musesetup.BOX_MAX_BYTES + 10)):
            with open(p, "w") as f:
                f.write(body)
            a.tick()
            self.assertIsNone(a.muse_view()["owned"], body[:20])
        os.remove(p)
        target = os.path.join(a.cfg.state_dir, "fake-box.json")
        with open(target, "w") as f:
            json.dump({"v": 1, "owned": True}, f)
        os.symlink(target, p)                                            # lien : jamais suivi
        a.tick()
        self.assertIsNone(a.muse_view()["owned"])


# --------------------------------------------------------------------------- chiffrement (Python ↔ cryptography ↔ JS)
class TestSeal(unittest.TestCase):
    def test_roundtrip_tamper_and_no_oracle(self):
        k = fast_key()
        n, e = museseal.public_from_jwk(k.public_jwk())
        self.assertEqual(set(k.public_jwk()), {"kty", "alg", "n", "e"})
        ct = museseal.encrypt(n, e, b"bonjour")
        self.assertEqual(k.decrypt(ct), b"bonjour")
        for bad in (ct[:-1], ct + b"\0", bytes([ct[0] ^ 1]) + ct[1:], ct[:100] + bytes([ct[100] ^ 4]) + ct[101:],
                    b"\0" * 256, (k.n + 1).to_bytes(257, "big")[1:]):
            with self.assertRaises(museseal.SealError) as cm:
                k.decrypt(bad)
            self.assertEqual(str(cm.exception), "seal_rejected")
        self.assertNotIn(str(k._d)[:12], repr(k))
        k.wipe()
        with self.assertRaises(museseal.SealError):
            k.decrypt(ct)
        with self.assertRaises(museseal.SealError):
            museseal.b64u_dec("ab=cd", 400)

    @unittest.skipUnless(tmb.HAVE_CRYPTO, "cryptography absente")
    def test_interop_with_cryptography_both_ways(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        k = fast_key()
        pub = rsa.RSAPublicNumbers(k.e, k.n).public_key()
        self.assertEqual(k.decrypt(pub.encrypt(b"x" * 190, oaep)), b"x" * 190)
        priv = rsa.generate_private_key(65537, 2048)
        pn = priv.public_key().public_numbers()
        self.assertEqual(priv.decrypt(museseal.encrypt(pn.n, pn.e, b"abc"), oaep), b"abc")

    @unittest.skipUnless(NODE, "node absent")
    def test_phone_script_seal_is_decrypted_by_the_box(self):
        """muse.js (tel que servi au téléphone) chiffre ; museseal déchiffre. SHA-256 vérifié sur vecteurs connus."""
        k = fast_key()
        jwk = k.public_jwk()
        doc = json.dumps({"v": 1, "s": "S" * 22, "f": "F" * 22, "t": TOKEN})
        script = r"""
        const vm = require('vm'), fs = require('fs'), crypto = require('crypto');
        const win = { crypto: { getRandomValues: (a) => crypto.randomFillSync(a) } };
        const ctx = vm.createContext({ window: win, atob, btoa, BigInt, Uint8Array, unescape, encodeURIComponent });
        vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
        const M = win.OpenTVMuseSeal, inp = JSON.parse(process.argv[2]);
        const hex = (b) => Buffer.from(b).toString('hex');
        const out = { selfTest: M.selfTest(), empty: hex(M.sha256([])), long: hex(M.sha256(M.utf8('a'.repeat(1000)))),
          cts: [] };
        for (let i = 0; i < 3; i++) out.cts.push(M.seal(inp.n, inp.e, M.utf8(inp.doc), (n) => {
          const a = new Uint8Array(n); crypto.randomFillSync(a); return Array.from(a); }));
        console.log(JSON.stringify(out));
        """
        p = subprocess.run([NODE, "-e", script, os.path.join(PI4, "ui", "muse.js"),
                            json.dumps({"n": jwk["n"], "e": jwk["e"], "doc": doc})],
                           capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        out = json.loads(p.stdout)
        import hashlib
        self.assertTrue(out["selfTest"])
        self.assertEqual(out["empty"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(out["long"], hashlib.sha256(b"a" * 1000).hexdigest())
        self.assertEqual(len(set(out["cts"])), 3)                   # aléa frais à chaque envoi
        for ct in out["cts"]:
            self.assertEqual(k.decrypt(museseal.b64u_dec(ct, 400)).decode(), doc)

    def test_service_and_admin_token_shapes_match_the_pinned_sdk(self):
        self.assertEqual(musesetup.SDK_TOKEN_RE.pattern, museadmin.SDK_TOKEN_RE.pattern)
        with open(os.path.join(PI4, "ui", "muse.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$/", js)
        for bad in ("localStorage", "sessionStorage", "document.cookie", "console.", "location.search =", "eval(",
                    "innerHTML", "crypto.subtle"):
            self.assertNotIn(bad, js)


# --------------------------------------------------------------------------- helper root : validation des jobs
class NoAdmin:
    def __init__(self, tc):
        self.tc = tc

    def __call__(self, **kw):
        self.tc.fail("aucune administration Muse ne doit être appelée")


class HelperBase(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="otvmh", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        self.cfg = config.Config({"OPENTV_STATE_DIR": os.path.join(d, "state")})
        for p in (self.cfg.state_dir, self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp, self.cfg.muse_results_dir):
            os.makedirs(p, mode=0o700, exist_ok=True)
            os.chmod(p, 0o700)
        self.journal = os.path.join(d, "journal")
        os.makedirs(self.journal, mode=0o700)
        self.logs = []
        self.now = 1000000.0

    def helper(self, admin_factory=None):
        return musehelper.MuseHelper(self.cfg, os.getuid(), admin_factory=admin_factory or NoAdmin(self),
                                     clock=lambda: self.now, sleep=lambda s: None, chown=False,
                                     log=lambda ev, **f: self.logs.append(dict(f, event=ev)),
                                     journal_dir=self.journal, journal_uid=os.getuid())

    def job(self, body, name=None, mode=0o600):
        body = dict(body)
        body.setdefault("v", 1)
        body.setdefault("created", self.now)
        name = name or body["id"] + ".json"
        p = os.path.join(self.cfg.muse_jobs_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(body, f)
        os.chmod(p, mode)
        return p

    def pair_job(self, jid="a" * 16, **kw):
        return self.job(dict({"id": jid, "action": "pair", "consent": "tv", "token": TOKEN}, **kw))

    def results(self):
        out = {}
        for n in sorted(os.listdir(self.cfg.muse_results_dir)):
            with open(os.path.join(self.cfg.muse_results_dir, n), encoding="utf-8") as f:
                out[n[:-5]] = json.load(f)
        return out


class TestHelperJobValidation(HelperBase):
    def test_every_malformed_or_unsafe_job_is_removed_without_any_action(self):
        h = self.helper()
        cases = {
            "b" * 16: dict(action="pair", consent="phone", token=TOKEN),             # pas de consentement TV
            "c" * 16: dict(action="pair", token=TOKEN),                               # consentement absent
            "d" * 16: dict(action="pair", consent="tv", token=TOKEN + "x"),           # jeton invalide
            "e" * 16: dict(action="pair", consent="tv", token=TOKEN, argv=["sh", "-c", "id"]),   # champ en trop
            "f" * 16: dict(action="pair", consent="tv", token=TOKEN, created=self.now - 3600),   # trop vieux
            "g" * 16: dict(action="exec", path="/etc/shadow"),                        # action inconnue
            "h" * 16: dict(action="unpair", confirm="phone"),
            "i" * 16: dict(action="cancel", target="../../etc"),
            "j" * 16: dict(action="unpair", confirm="tv", replace_identity=True),
        }
        for jid, body in cases.items():
            self.job(dict(body, id=jid))
        self.job({"id": "k" * 16, "action": "pair", "consent": "tv", "token": TOKEN}, mode=0o644)   # mode
        p = self.job({"id": "m" * 16, "action": "pair", "consent": "tv", "token": TOKEN})
        os.link(p, os.path.join(self.cfg.state_dir, "hl"))                            # lien dur
        with open(os.path.join(self.cfg.muse_jobs_dir, "n" * 16 + ".json"), "w") as f:
            f.write("{" * 5000)                                                       # trop gros / non JSON
        os.chmod(os.path.join(self.cfg.muse_jobs_dir, "n" * 16 + ".json"), 0o600)
        target = os.path.join(self.cfg.state_dir, "victim.json")
        with open(target, "w") as f:
            json.dump({"id": "o" * 16, "v": 1, "action": "unpair", "confirm": "tv", "created": self.now}, f)
        os.symlink(target, os.path.join(self.cfg.muse_jobs_dir, "o" * 16 + ".json"))   # lien symbolique
        os.mkfifo(os.path.join(self.cfg.muse_jobs_dir, "p" * 16 + ".json"))            # FIFO
        os.mkdir(os.path.join(self.cfg.muse_jobs_dir, "q" * 16 + ".json"))             # dossier
        self.job({"id": "r" * 16, "action": "pair", "consent": "tv", "token": TOKEN}, name="evil name.json")
        self.assertEqual(h.run(), [])                                                 # rien exécuté
        self.assertEqual(os.listdir(self.cfg.muse_jobs_dir), [])                      # tout retiré
        self.assertTrue(os.path.exists(target))                                       # cible du lien intacte
        res = self.results()
        for jid in list(cases) + ["k" * 16, "m" * 16, "n" * 16]:
            self.assertEqual((res[jid]["phase"], res[jid]["ok"], res[jid]["result"]), ("done", False, "refused"), jid)
        self.assertNotIn(TOKEN, json.dumps(res) + json.dumps(self.logs))

    def test_wrong_owner_unsafe_directory_and_symlinked_directory_are_refused(self):
        self.pair_job()
        real_fstat = os.fstat

        def fstat(fd):
            st = real_fstat(fd)
            if stat.S_ISREG(st.st_mode):
                lst = list(st)
                lst[stat.ST_UID] = st.st_uid + 1
                return os.stat_result(lst)
            return st
        with mock.patch.object(musehelper.os, "fstat", fstat):
            self.assertEqual(self.helper().run(), [])
        self.assertEqual(self.results()["a" * 16]["result"], "refused")
        self.assertIn("bad_owner", json.dumps(self.logs))
        # dossier des jobs ouvert à d'autres : aucun job lu (fail-closed), rien supprimé
        self.pair_job(jid="b" * 16)
        os.chmod(self.cfg.muse_jobs_dir, 0o755)
        self.assertEqual(self.helper().run(), [])
        self.assertEqual(len(os.listdir(self.cfg.muse_jobs_dir)), 1)
        os.chmod(self.cfg.muse_jobs_dir, 0o700)
        # dossier remplacé par un lien : refusé
        real = self.cfg.muse_jobs_dir + ".real"
        os.rename(self.cfg.muse_jobs_dir, real)
        os.symlink(real, self.cfg.muse_jobs_dir)
        self.assertEqual(self.helper().run(), [])
        self.assertEqual(len(os.listdir(real)), 1)

    def test_no_generic_shell_file_or_identity_replacement_in_the_helper(self):
        for mod in ("musehelper.py", "musesetup.py", "museweb.py", "museseal.py"):
            with open(os.path.join(PI4, "opentv_pi", mod), encoding="utf-8") as f:
                src = re.sub(r'""".*?"""', "", f.read(), flags=re.S)
            for bad in ("shell=True", "os.system", "subprocess", "eval(", "exec(", "os.popen", "pickle",
                        "replace_identity=True", "force=True", "replace_token=True"):
                self.assertNotIn(bad, src, (mod, bad))
        self.assertEqual(musehelper.KEYS, {
            "pair": {"v", "id", "action", "consent", "created", "token"},
            "cancel": {"v", "id", "action", "target", "created"},
            "unpair": {"v", "id", "action", "confirm", "created"},
            "probe": {"v", "id", "action", "created"}})
        self.assertEqual(sorted(musebridge.COMMANDS), tmb.EXPECTED)           # exactement les 11 commandes
        for bad in ("system.run", "file.read", "file.write", "device.health"):
            self.assertNotIn(bad, musebridge.COMMANDS)


# --------------------------------------------------------------------------- helper root + vrai `muse pair`
class RealAdminHelperBase(tmb.AdminBase):
    def setUp(self):
        super().setUp()
        for p in (self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp, self.cfg.muse_results_dir):
            os.makedirs(p, mode=0o700, exist_ok=True)
            os.chmod(p, 0o700)
        self.journal = self.j("/var/lib/opentv-muse-setup")
        os.makedirs(self.journal, mode=0o700)
        os.chmod(self.journal, 0o700)
        reg = agents.Registry(self.cfg.registry_file)
        reg.set_policy({"muse": ["feedback", "read"], "hermit": [], "hermes": []})   # pilotage PAS encore accordé
        self.hlogs = []
        self.on_window = None

    def runner(self, argv, timeout):
        out = super().runner(argv, timeout)
        if argv[:3] == ["systemctl", "start", "--no-block"] and argv[3] == museadmin.PAIR_UNIT and self.on_window:
            self.on_window()
        return out

    def factory(self, **kw):
        pwd_mod = mock.Mock(getpwnam=lambda n: self.pwmap[n])
        return musehelper.WizardAdmin(self.cfg, root=self.r, runner=self.runner, euid=lambda: 0, pwd_mod=pwd_mod,
                                      grp_mod=self.groups, sdk_dir=self.sdk, clock=self.fake_clock,
                                      trusted_uids=(0, os.getuid()), **kw)

    def helper(self, sleep=None):
        return musehelper.MuseHelper(self.cfg, os.getuid(), admin_factory=self.factory, clock=time.time,
                                     sleep=sleep or self.fake_sleep, chown=False, journal_dir=self.journal,
                                     journal_uid=os.getuid(), log=lambda ev, **f: self.hlogs.append(dict(f, event=ev)))

    def put(self, body):
        body = dict(body, v=1, created=time.time())
        p = os.path.join(self.cfg.muse_jobs_dir, body["id"] + ".json")
        with open(p, "w") as f:
            json.dump(body, f)
        os.chmod(p, 0o600)

    def pair_job(self, jid="a" * 16):
        self.put({"id": jid, "action": "pair", "consent": "tv", "token": tmb.SDK_TOKEN})

    def policy(self):
        return agents.Registry(self.cfg.registry_file).policy()["muse"]

    def phases(self, jid):
        return [x.get("phase") for x in self.hlogs if x.get("id") == jid]

    def result(self, jid):
        with open(os.path.join(self.cfg.muse_results_dir, jid + ".json")) as f:
            return json.load(f)


class TestHelperWithRealAdmin(RealAdminHelperBase):
    def test_tv_confirmed_pair_grants_control_pairs_and_reports_progress_without_the_token(self):
        seen = []
        real = musehelper.MuseHelper._result

        def spy(h, jid, phase, **kw):
            seen.append((phase, kw.get("ble_name"), kw.get("result")))
            return real(h, jid, phase, **kw)
        self.pair_job()
        with mock.patch.object(musehelper.MuseHelper, "_result", spy):
            self.assertEqual(self.helper().run(), ["connected"])
        self.assertEqual([x[0] for x in seen], ["preparing", "waiting_muse", "connecting", "done"])
        self.assertEqual(seen[1][1], "MuseGadget0A0B0C")
        self.assertEqual(seen[-1][2], "connected")
        self.assertEqual(sorted(self.policy()), CONTROL)                     # accordé PAR ce consentement
        reg = agents.Registry(self.cfg.registry_file)
        self.assertEqual(reg.list()[musebridge.MUSE_AGENT_ID]["scopes"], CONTROL)
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        self.assertEqual(os.listdir(self.cfg.muse_jobs_dir), [])
        self.assertEqual(os.listdir(self.journal), [])
        r = self.result("a" * 16)
        self.assertEqual((r["ok"], r["result"]), (True, "connected"))
        blob = json.dumps(self.hlogs) + json.dumps(r) + self.err.getvalue()
        self.assertNotIn(tmb.SDK_TOKEN, blob)
        cmds = self.cmds()
        self.assertIn("start --no-block %s" % museadmin.PAIR_UNIT, cmds)
        self.assertFalse([c for c in cmds if "replace" in c or "force" in c])

    def test_window_failure_revokes_issued_identity_and_restores_the_previous_policy(self):
        self.window = "window_closed"
        self.pair_job()
        self.assertEqual(self.helper().run(), ["window_closed"])
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])        # rien n'a changé pour Muse
        self.assertNotIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())
        self.assertEqual(self.result("a" * 16)["result"], "window_closed")
        self.assertEqual(os.listdir(self.journal), [])

    def test_owner_decision_during_pairing_is_never_overwritten_by_the_rollback(self):
        self.window = "window_closed"
        real = self.fake_sleep

        def sleep(s):   # pendant la fenêtre, Alex retire lui-même tout accès à Muse depuis la TV
            reg = agents.Registry(self.cfg.registry_file)
            v, fp, _ = reg.snapshot(("muse", "hermit", "hermes"))
            if sorted(v["muse"]["allowed"]) == CONTROL:
                reg.apply_policy_changes({"muse": ["read"]}, ("muse", "hermit", "hermes"), fp)
            return real(s)
        self.pair_job()
        self.helper(sleep=sleep).run()
        self.assertEqual(self.policy(), ["read"])

    def test_cancel_during_the_bluetooth_wait_closes_the_window_and_changes_nothing(self):
        self.window = None                                                    # l'iPhone ne vient jamais
        self.on_window = lambda: self.put({"id": "c" * 16, "action": "cancel", "target": "a" * 16})
        self.pair_job()
        self.assertEqual(self.helper().run(), ["interrupted"])
        self.assertEqual(self.result("a" * 16)["result"], "cancelled")
        self.assertIn("stop %s" % museadmin.PAIR_UNIT, self.cmds())          # fenêtre fermée
        self.assertNotIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))

    def test_cancel_queued_with_the_pair_job_runs_nothing(self):
        self.pair_job()
        self.put({"id": "c" * 16, "action": "cancel", "target": "a" * 16})
        self.assertEqual(self.helper().run(), ["cancelled"])
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])
        self.assertFalse([c for c in self.cmds() if museadmin.PAIR_UNIT in c])

    def test_foreign_identity_is_never_replaced_and_policy_restored(self):
        agents.Registry(self.cfg.registry_file).set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
        agents.Registry(self.cfg.registry_file).issue(musebridge.MUSE_AGENT_ID, CONTROL)   # émise par un autre
        agents.Registry(self.cfg.registry_file).set_policy({"muse": ["feedback", "read"], "hermit": [], "hermes": []})
        before = agents.Registry(self.cfg.registry_file).list()[musebridge.MUSE_AGENT_ID]
        self.pair_job()
        self.assertEqual(self.helper().run(), ["foreign_identity"])
        after = agents.Registry(self.cfg.registry_file).list().get(musebridge.MUSE_AGENT_ID)
        self.assertIsNotNone(after)                                           # jamais révoquée
        self.assertEqual(after.get("token_sha256"), before.get("token_sha256"))
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])
        self.assertFalse([c for c in self.cmds() if museadmin.PAIR_UNIT in c])

    def test_helper_crash_mid_pairing_is_cleaned_up_on_restart(self):
        self.window = None

        class Crash(Exception):
            pass
        n = {"i": 0}

        def crashing(s):
            n["i"] += 1
            if n["i"] > 3:
                raise Crash()
            self.fake_sleep(s)
        self.pair_job()
        with self.assertRaises(Crash):
            self.helper(sleep=crashing).run()
        self.assertEqual(os.listdir(self.journal), ["inflight.json"])         # journal root laissé par le plantage
        self.assertEqual(sorted(self.policy()), CONTROL)
        self.assertIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())
        with open(os.path.join(self.journal, "inflight.json")) as f:
            self.assertNotIn(tmb.SDK_TOKEN, f.read())
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))   # écrit par cette séance
        self.calls.clear()
        self.helper().run()                                                   # redémarrage : reprise
        self.assertIn("stop %s" % museadmin.PAIR_UNIT, self.cmds())
        self.assertNotIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])
        self.assertEqual(os.listdir(self.journal), [])
        # état d'avant VRAIMENT rendu (aucun jeton avant la séance) : alors seulement « interrompu, rien n'a changé »
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        r = self.result("a" * 16)
        self.assertEqual((r["ok"], r["result"]), (False, "interrupted"))
        self.assertIn("rien n'a changé", musesetup.message_for("interrupted"))
        self.assertEqual(musesetup.read_box(self.cfg.muse_results_dir)["owned"], False)

    def test_crash_recovery_that_cannot_undo_everything_never_says_nothing_changed(self):
        self.window = None

        class Crash(Exception):
            pass
        n = {"i": 0}

        def crashing(s):
            n["i"] += 1
            if n["i"] > 3:
                raise Crash()
            self.fake_sleep(s)
        self.pair_job()
        with self.assertRaises(Crash):
            self.helper(sleep=crashing).run()
        stuck = {"agent_revoked": None, "agent_error": "registry_busy", "client_file_removed": False}
        with mock.patch.object(museadmin.Admin, "discard_identity", return_value=stuck):
            self.helper().run()
        r = self.result("a" * 16)
        self.assertEqual((r["ok"], r["result"]), (False, "recovery_incomplete"))
        msg = musesetup.message_for("recovery_incomplete")
        self.assertNotIn("rien n'a changé", msg)
        self.assertIn("Gérer les assistants", msg)
        self.assertEqual(os.listdir(self.journal), [])

    def tracked(self, jid="a" * 16, cancel=False):
        """Séance du service (même dossier museresults que le helper) arrivée à « Connexion »."""
        ms = musesetup.MuseSetup(self.cfg, keygen=fast_key, async_keygen=False)
        self.assertEqual(ms.start(True), (True, None))
        s = ms.cur
        s.job, s.deadline = jid, time.monotonic() + musesetup.PAIR_BUDGET_S
        s.pair_deadline, s.expires = s.deadline, s.deadline
        ms._set(s, "submitted")
        ms._set(s, "connecting")
        if cancel:
            s.cancel_requested = True
        return ms

    def assert_paired_incomplete(self, ms, late=False):
        v = ms.tv_view(MDNS, 8081, musebridge.MUSE_AGENT_ID in agents.Registry(self.cfg.registry_file).list())
        last = v["last"]
        self.assertEqual((v["active"], last["state"], last["error"]), (False, "failed", "paired_incomplete"))
        self.assertEqual(bool(last.get("late")), late)
        self.assertEqual(last["message"], musesetup.message_for("paired_incomplete"))
        self.assertIn("Muse est appairée", last["message"])
        self.assertIn("connexion doit être reprise", last["message"])
        for bad in ("rien n'a changé", "Annulé", "est connectée"):
            self.assertNotIn(bad, last["message"])
        self.assertNotIn(tmb.SDK_TOKEN, json.dumps(v))
        # actions sûres : reconnexion (nouveau consentement TV) et déconnexion confirmée (la boîte possède Muse)
        self.assertEqual(v["owned"], True)
        return v

    def test_crash_after_successful_pairing_never_revokes_the_paired_muse(self):
        fsutil = tmb.fsutil
        fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/pairing.json"), "{}")
        with open(os.path.join(self.journal, "inflight.json"), "w") as f:
            json.dump({"v": 1, "id": "a" * 16, "changed": True, "before": ["feedback", "read"]}, f)
        agents.Registry(self.cfg.registry_file).set_policy({"muse": CONTROL, "hermit": [], "hermes": []})
        ms = self.tracked()                                                   # TV : suivi en cours, sans annulation
        self.helper().run()
        self.assertEqual(sorted(self.policy()), CONTROL)
        self.assertEqual(os.listdir(self.journal), [])
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        r = self.result("a" * 16)
        self.assertEqual((r["ok"], r["result"]), (False, "paired_incomplete"))   # jamais « interrupted »
        self.assertEqual(musesetup.read_box(self.cfg.muse_results_dir)["owned"], True)
        ms.tick()
        v = self.assert_paired_incomplete(ms)
        self.assertEqual(ms.disconnect(True, owned=v["owned"]), (True, None))
        jobs_now = sorted(os.listdir(self.cfg.muse_jobs_dir))
        with open(os.path.join(self.cfg.muse_jobs_dir, jobs_now[0])) as f:
            self.assertEqual(json.load(f)["action"], "unpair")

    def test_crash_during_bridge_start_after_pairing_reports_paired_and_keeps_everything(self):
        class Crash(Exception):
            pass
        self.pair_job()
        ms = self.tracked(cancel=True)                                        # annulation demandée en plus
        with mock.patch.object(museadmin.Admin, "start_bridge", side_effect=Crash()):
            with self.assertRaises(Crash):                                    # plantage après pairing.json
                self.helper().run()
        reg = agents.Registry(self.cfg.registry_file)
        ident = reg.list()[musebridge.MUSE_AGENT_ID]
        with open(self.j("/var/lib/opentv-muse/sdk_token"), "rb") as f:
            tok = f.read()
        self.calls.clear()
        self.helper().run()                                                   # redémarrage : reprise
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        self.assertEqual(reg.list()[musebridge.MUSE_AGENT_ID].get("token_sha256"), ident.get("token_sha256"))
        with open(self.j("/var/lib/opentv-muse/sdk_token"), "rb") as f:
            self.assertEqual(f.read(), tok)                                   # jeton local gardé
        self.assertEqual(sorted(self.policy()), CONTROL)                      # pilotage gardé
        self.assertFalse([c for c in self.cmds() if "disable" in c])
        r = self.result("a" * 16)
        self.assertEqual((r["ok"], r["result"]), (False, "paired_incomplete"))
        ms.tick()
        self.assert_paired_incomplete(ms)                                     # annulation demandée : jamais « Annulé »
        self.assertEqual(ms.start(True), (True, None))                        # « Reconnecter » : nouveau consentement
        blob = json.dumps(self.hlogs) + json.dumps(r) + self.err.getvalue()
        self.assertNotIn(tmb.SDK_TOKEN, blob)
        # reprise réelle : même jeton, aucun Bluetooth, pont démarré ⇒ seulement alors « connectée »
        self.calls.clear()
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": tmb.SDK_TOKEN})
        self.assertEqual(self.helper().run(), ["connected"])
        self.assertFalse([c for c in self.cmds() if museadmin.PAIR_UNIT in c])

    def test_cancel_racing_a_completed_pairing_reports_paired_never_cancelled(self):
        self.window = None

        def on_window():   # l'iPhone termine l'appariement au moment même où Alex annule
            tmb.fsutil.atomic_write_path(self.j("/var/lib/opentv-muse/pairing.json"), "{}")
            self.put({"id": "c" * 16, "action": "cancel", "target": "a" * 16})
        self.on_window = on_window
        self.pair_job()
        ms = self.tracked(cancel=True)
        self.assertEqual(self.helper().run(), ["paired_incomplete"])
        self.assertEqual(self.result("a" * 16)["result"], "paired_incomplete")
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        self.assertTrue(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertEqual(sorted(self.policy()), CONTROL)
        ms.tick()
        self.assert_paired_incomplete(ms)

    def test_unpair_job_unpairs_forgets_token_and_revokes_control(self):
        self.pair_job()
        self.helper().run()
        self.assertEqual(sorted(self.policy()), CONTROL)
        self.unit.stop_delay = 0
        self.put({"id": "u" * 16, "action": "unpair", "confirm": "tv"})
        self.assertEqual(self.helper().run(), ["disconnected"])
        self.assertNotIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())
        self.assertNotIn("control", self.policy())
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/pairing.json")))
        self.assertFalse(os.path.exists(self.j("/var/lib/opentv-muse/sdk_token")))
        self.assertEqual(self.result("u" * 16)["result"], "disconnected")


OTHER_TOKEN = "mgst_" + "R" * 42 + "A"
assert museadmin.SDK_TOKEN_RE.fullmatch(OTHER_TOKEN) and OTHER_TOKEN != tmb.SDK_TOKEN


class TestExistingPairingIsPreserved(RealAdminHelperBase):
    """Reconnexion depuis la TV : un jeton local existant n'est jamais remplacé ; un jeton différent est refusé AVANT
    toute autorisation, tout arrêt du pont et toute écriture ; un échec de démarrage ne touche ni jeton ni identité."""
    TOK = "/var/lib/opentv-muse/sdk_token"
    PAIRING = "/var/lib/opentv-muse/pairing.json"

    def snap(self):
        p = self.j(self.TOK)
        st = os.stat(p)
        with open(p, "rb") as f:
            raw = f.read()
        ent = agents.Registry(self.cfg.registry_file).list().get(musebridge.MUSE_AGENT_ID) or {}
        rst = os.stat(self.cfg.registry_file)
        return {"token": (raw, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, st.st_ino),
                "registry": (rst.st_ino, rst.st_mtime_ns),            # aucune écriture, même transitoire
                "identity": (ent.get("token_sha256"), ent.get("scopes")), "policy": sorted(self.policy()),
                "pairing": os.path.exists(self.j(self.PAIRING))}

    def paired(self):
        self.pair_job()
        self.assertEqual(self.helper().run(), ["connected"])
        self.assertEqual((self.unit.active, self.unit.pid is not None), ("active", True))
        self.calls.clear()
        return self.snap(), self.unit.pid

    def no_secret(self, jid):
        blob = json.dumps(self.hlogs) + json.dumps(self.result(jid)) + self.err.getvalue()
        for t in (tmb.SDK_TOKEN, OTHER_TOKEN):
            self.assertNotIn(t, blob)
            self.assertNotIn(t[5:20], blob)
        import hashlib
        self.assertNotIn(hashlib.sha256(OTHER_TOKEN.encode()).hexdigest()[:16], blob)

    def test_different_token_is_refused_before_any_change_and_bridge_keeps_running(self):
        before, pid = self.paired()
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": OTHER_TOKEN})
        self.assertEqual(self.helper().run(), ["different_token"])
        self.assertEqual(self.snap(), before)                          # octets, mode, propriétaire, inode du jeton
        self.assertEqual((self.unit.active, self.unit.pid), ("active", pid))   # pont jamais arrêté
        self.assertEqual(self.cmds(), [])                              # ni stop, ni fenêtre, ni enable
        r = self.result("b" * 16)
        self.assertEqual((r["ok"], r["result"]), (False, "different_token"))
        self.assertEqual(os.listdir(self.journal), [])
        self.no_secret("b" * 16)
        self.assertIn("rien n'a changé", musesetup.message_for("different_token"))

    def test_different_token_never_grants_control_to_a_paused_bridge(self):
        self.paired()
        reg = agents.Registry(self.cfg.registry_file)
        v, fp, _ = reg.snapshot(("muse", "hermit", "hermes"))
        reg.apply_policy_changes({"muse": ["feedback", "read"]}, ("muse", "hermit", "hermes"), fp)   # en pause
        before = self.snap()
        self.calls.clear()
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": OTHER_TOKEN})
        self.assertEqual(self.helper().run(), ["different_token"])
        self.assertEqual(self.snap(), before)                          # aucune autorisation, identité inchangée
        self.assertEqual(sorted(self.policy()), ["feedback", "read"])
        self.assertEqual(self.cmds(), [])

    def test_same_token_reuses_existing_credential_and_leaves_healthy_bridge_alone(self):
        before, pid = self.paired()
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": tmb.SDK_TOKEN})
        self.assertEqual(self.helper().run(), ["connected"])
        self.assertEqual(self.snap(), before)                          # même inode : jamais réécrit
        self.assertEqual((self.unit.active, self.unit.pid), ("active", pid))
        self.assertFalse([c for c in self.cmds() if c.startswith("stop") or museadmin.PAIR_UNIT in c])
        self.no_secret("b" * 16)

    def test_bridge_start_failure_keeps_token_identity_policy_and_a_usable_bridge(self):
        before, _pid = self.paired()
        self.unit.gone(1)                                               # reconnexion échouée : pont arrêté
        self.unit.crash_next = 1                                       # le prochain démarrage échoue aussi
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": tmb.SDK_TOKEN})
        self.assertEqual(self.helper().run(), ["bridge_not_started"])
        self.assertEqual(self.snap(), before)
        self.assertFalse([c for c in self.cmds() if museadmin.PAIR_UNIT in c])   # aucun Bluetooth rouvert
        started, _rb = self.factory().start_bridge()                   # l'ancien appariement reste utilisable
        self.assertTrue(started)
        self.assertEqual(self.unit.active, "active")

    def test_failed_first_pairing_removes_only_the_token_it_wrote(self):
        self.window = "window_closed"
        self.pair_job()
        self.assertEqual(self.helper().run(), ["window_closed"])
        self.assertFalse(os.path.exists(self.j(self.TOK)))             # état d'avant : aucun jeton
        self.assertNotIn(musebridge.MUSE_AGENT_ID, agents.Registry(self.cfg.registry_file).list())

    def test_unsafe_local_token_is_never_replaced(self):
        before, _ = self.paired()
        os.chmod(self.j(self.TOK), 0o644)
        self.put({"id": "b" * 16, "action": "pair", "consent": "tv", "token": OTHER_TOKEN})
        self.assertEqual(self.helper().run(), ["muse_unavailable"])
        with open(self.j(self.TOK), "rb") as f:
            self.assertEqual(f.read(), before["token"][0])
        self.assertEqual(self.cmds(), [])

    def test_admin_layer_also_refuses_a_different_token_without_writing(self):
        before, pid = self.paired()                                    # défense en profondeur (course TOCTOU)
        r, code = self.factory(stdin=io.StringIO(OTHER_TOKEN + "\n")).pair(token_stdin=True, keep_token=True)
        self.assertEqual((r["ok"], r["error"], code), (False, "sdk_token_mismatch", 2))
        self.assertEqual(self.snap(), before)
        self.assertEqual((self.unit.active, self.unit.pid), ("active", pid))
        self.assertEqual(self.cmds(), [])
        self.assertNotIn(OTHER_TOKEN, json.dumps(r))

    def test_keep_token_refuses_replacement_options(self):
        for kw in ({"replace_token": True}, {"force": True}, {"replace_identity": True}):
            r, code = self.factory(stdin=io.StringIO(tmb.SDK_TOKEN + "\n")).pair(token_stdin=True, keep_token=True,
                                                                                 **kw)
            self.assertEqual((r["error"], code), ("bad_options", 2), kw)
        self.assertEqual(self.cmds(), [])

    def test_probe_publishes_public_ownership_and_foreign_identity_is_never_unpaired(self):
        box = os.path.join(self.cfg.muse_results_dir, musesetup.BOX_NAME)
        self.put({"id": "p" * 16, "action": "probe"})
        self.assertEqual(self.helper().run(), [])
        self.assertEqual(musesetup.read_box(self.cfg.muse_results_dir),
                         {"owned": False, "foreign": False, "identity_present": False})
        self.paired()
        b = musesetup.read_box(self.cfg.muse_results_dir)              # publié après le pair, sans probe
        self.assertEqual(b, {"owned": True, "foreign": False, "identity_present": True})
        with open(box) as f:
            raw = f.read()
        self.assertNotIn(tmb.SDK_TOKEN, raw)
        self.assertEqual(stat.S_IMODE(os.stat(box).st_mode), 0o600)
        # identité « muse-device » remplacée par un autre client : étrangère ⇒ jamais déconnectée d'ici
        reg = agents.Registry(self.cfg.registry_file)
        reg.revoke(musebridge.MUSE_AGENT_ID)
        reg.issue(musebridge.MUSE_AGENT_ID, CONTROL)
        foreign = reg.list()[musebridge.MUSE_AGENT_ID]
        self.put({"id": "u" * 16, "action": "unpair", "confirm": "tv"})
        self.assertEqual(self.helper().run(), ["disconnect_failed"])
        self.assertEqual(self.result("u" * 16)["result"], "foreign_identity")
        self.assertEqual(reg.list()[musebridge.MUSE_AGENT_ID].get("token_sha256"), foreign.get("token_sha256"))
        self.assertTrue(os.path.exists(self.j(self.PAIRING)))
        self.assertTrue(os.path.exists(self.j(self.TOK)))
        self.assertEqual(musesetup.read_box(self.cfg.muse_results_dir),
                         {"owned": False, "foreign": True, "identity_present": True})
        self.assertFalse([c for c in self.cmds() if "disable" in c])


# --------------------------------------------------------------------------- unités, installation
def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        text = f.read()
    cp.read_string(text)
    return cp, text


class TestUnits(unittest.TestCase):
    def test_path_and_root_helper_units_are_narrow_and_sandboxed(self):
        cp, _ = unit("opentv-muse-setup.path")
        self.assertEqual(cp["Path"]["DirectoryNotEmpty"], "/var/lib/opentv/musejobs")
        self.assertEqual(cp["Path"]["Unit"], "opentv-muse-setup.service")
        self.assertEqual(cp["Install"]["WantedBy"], "multi-user.target")
        cp, text = unit("opentv-muse-setup.service")
        s = cp["Service"]
        self.assertNotIn("Install", cp.sections())
        self.assertNotIn("User", s)                                           # root, mais borné
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi muse-setup-helper")
        self.assertEqual(s["Type"], "oneshot")
        self.assertEqual(set(s["CapabilityBoundingSet"].split()), {"CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER"})
        self.assertEqual(set(s["ReadWritePaths"].split()), {"/var/lib/opentv", "/var/lib/opentv-muse",
                                                            "/var/lib/opentv-muse-setup", "/etc/opentv/agents"})
        for k, v in (("NoNewPrivileges", "yes"), ("ProtectSystem", "strict"), ("PrivateNetwork", "yes"),
                     ("RestrictAddressFamilies", "AF_UNIX"), ("ProtectHome", "yes"), ("PrivateDevices", "yes"),
                     ("MemoryDenyWriteExecute", "yes"), ("SystemCallFilter", "@system-service")):
            self.assertEqual(s[k], v, k)
        self.assertGreater(int(s["TimeoutStartSec"]), museadmin.PAIR_WAIT_S + museadmin.STOP_WAIT_S)
        self.assertLess(int(s["TimeoutStartSec"]), musesetup.PAIR_BUDGET_S)
        inacc = " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()
        for p in ("-/etc/opentv/tmdb", "-/etc/opentv/source.env", "-/etc/NetworkManager/system-connections"):
            self.assertIn(p, inacc)
        self.assertIn("ConditionPathExists=!/run/opentv-uninstalling", text)
        # le helper réseau n'est pas élargi
        _, nh = unit("opentv-net-helper.service")
        self.assertNotIn("muse", nh)

    def test_job_dirs_private_and_masked_from_other_service_account_units(self):
        with open(os.path.join(PI4, "system", "tmpfiles-opentv.conf"), encoding="utf-8") as f:
            tmp = f.read()
        for line in ("d /var/lib/opentv/musejobs 0700 opentv opentv -",
                     "d /var/lib/opentv/musejobs-tmp 0700 opentv opentv -",
                     "d /var/lib/opentv/museresults 0700 opentv opentv -",
                     "d /var/lib/opentv-muse-setup 0700 root root -"):
            self.assertIn(line, tmp)
        c = config.Config({})
        for name in ("opentv-catalogue-refresh.service", "opentv-catalogue-ratings.service",
                     "opentv-catalogue-ratings-retry.service"):
            _, text = unit(name)
            inacc = {p[1:] for p in " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()}
            for p in (c.muse_jobs_dir, c.muse_jobs_tmp, c.muse_results_dir):
                self.assertIn(p, inacc, (name, p))
        for name in ("opentv-muse.service", "opentv-muse-pair.service"):   # le pont ne voit jamais /var/lib/opentv
            _, text = unit(name)
            self.assertIn("-/var/lib/opentv ", text)

    def test_install_uninstall_ship_and_remove_the_wizard(self):
        with open(os.path.join(PI4, "install.sh"), encoding="utf-8") as f:
            ins = f.read()
        with open(os.path.join(PI4, "uninstall.sh"), encoding="utf-8") as f:
            uni = f.read()
        self.assertIn("opentv-muse-setup.path opentv-muse-setup.service; do", ins)
        self.assertIn("remote.js muse.js fallback.html", ins)
        self.assertRegex(ins, r"post systemctl enable [^\n]*(\\\n[^\n]*)*opentv-muse-setup\.path")
        self.assertIn("host systemctl stop opentv-muse-setup.path opentv-muse-setup.service", uni)
        self.assertIn("opentv-muse-pair.service opentv-muse-setup.path opentv-muse-setup.service\n", uni)
        self.assertLess(uni.index("systemctl stop opentv-muse-setup.path"), uni.index("-type f -delete"))
        self.assertIn("/var/lib/opentv/musejobs /var/lib/opentv/musejobs-tmp", uni)
        self.assertIn("/var/lib/opentv-muse-setup", uni)


class TestTokenReuseCopy(unittest.TestCase):
    """Un compte Muse : UN jeton actif, valable pour plusieurs appareils. Aucune surface (téléphone, TV, messages
    d'échec, script) ne conseille d'en créer un second ni de le révoquer ; seule mention de révocation : l'avertir."""
    SURFACES = ("opentv_pi/museweb.py", "opentv_pi/musesetup.py", "ui/muse.js", "ui/tv.js")
    BAD = (r"cr[ée]e(r|z)? (un )?(nouveau|autre|second) jeton", r"nouveau jeton", r"g[ée]n[èe]re", r"r[ée]voque-le",
           r"r[ée]voquez", r"supprime(r|z)? (ton|le|votre) jeton", r"revoke", r"regenerate", r"new token")

    def texts(self):
        out = {}
        for rel in self.SURFACES:
            with open(os.path.join(PI4, rel), encoding="utf-8") as f:
                out[rel] = f.read().replace("/api/tv/remote/revoke", "")   # « oublier les téléphones » : sans rapport
        return out

    def test_no_surface_recommends_a_new_token_or_revocation(self):
        for rel, txt in self.texts().items():
            for bad in self.BAD:
                self.assertIsNone(re.search(bad, txt, re.I), (rel, bad))
            for m in re.finditer(r"r[ée]voqu\w*", txt, re.I):   # révocation : seulement dans l'avertissement
                ctx = txt[max(0, m.start() - 40):m.end() + 80]
                if rel.startswith("ui/tv.js") or rel.endswith("musesetup.py"):
                    continue   # vocabulaire interne (agents, pont), jamais affiché sur le chemin du jeton
                self.assertTrue("Ne révoque pas" in ctx or "révoquer déconnecterait" in ctx or "jamais" in ctx,
                                (rel, ctx))
        for code, msg in musesetup.MESSAGES.items():
            self.assertNotIn("nouveau", msg.lower(), code)
            self.assertNotIn("révoqu", msg.lower(), code)
        self.assertIn("le même que pour tes autres", musesetup.message_for("invalid_sdk_token"))

    def test_masked_prefix_is_explained_never_accepted(self):
        js = self.texts()["ui/muse.js"]
        self.assertIn("Ceci n'est que le début masqué du jeton", js)
        for masked in ("mgst_Ab12\u2022\u2022\u2022\u2022", "mgst_Ab12…", "mgst_Ab12****", "mgst_Ab12..."):
            self.assertIsNone(musesetup.SDK_TOKEN_RE.fullmatch(masked))

    def test_pages_show_multi_device_copy_and_one_primary_action(self):
        v = {"csrf": "c" * 32, "jwk": {"n": "AQAB", "e": "AQAB"}, "state": "await_token"}
        p1 = museweb.render("S" * 22, v, 1)
        p2 = museweb.render("S" * 22, v, 2)
        self.assertIn("Utiliser mon jeton existant", p1)
        self.assertIn("plusieurs appareils", p1)
        self.assertIn("Ne révoque pas", p1)
        self.assertEqual(p1.count('class="primary"'), 1)
        self.assertEqual(p2.count('class="primary"'), 1)
        for p in (p1, p2):
            self.assertNotRegex(p, r"mgst_[A-Za-z0-9_-]{4}")      # aucun jeton (ni même un début) dans une page


# --------------------------------------------------------------------------- tv.js réel (harnais Chromium, 1920×1080)
MUSE_SNAP = tam.AGENT_DRIVER.replace(
    "agentsnote: vis($(\"agentsnote\")) ? $(\"agentsnote\").textContent : null };",
    "agentsnote: null, pmeta: vis(pan) ? $(\"pmeta\").textContent : null,"
    " caps: Array.prototype.map.call(document.querySelectorAll(\"#pcaps:not([hidden]) li\"), function (x) {"
    " return x.textContent; }), phases: Array.prototype.map.call(document.querySelectorAll("
    "\"#pphases:not([hidden]) li\"), function (x) { return [x.textContent, x.className]; }),"
    " bodies: (window.__BODIES__ || []).filter(function (b) { return b.indexOf(\"/api/tv/muse/probe\") !== 0; }),"
    " probes: (window.__BODIES__ || []).filter(function (b) { return b.indexOf(\"/api/tv/muse/probe\") === 0; })"
    ".length, text: document.body.innerText };").replace(
    "return c.indexOf(\"POST\") === 0; }),",
    "return c.indexOf(\"POST\") === 0 && c.indexOf(\"/api/tv/muse/probe\") < 0; }),")
assert MUSE_SNAP != tam.AGENT_DRIVER


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestMuseTvInChromium(unittest.TestCase):
    URL = "http://opentv-test.local:8081/muse?s=" + "S" * 22 + "#" + "F" * 22
    T = tam.TestAgentsTvInChromium
    FY, STATE, REMOTE1, render = T.FY, T.STATE, T.REMOTE1, T.render

    def st(self, active=False, state=None, url=None, lseq=0, last=None, bridge=None, phone=False, ble=None,
           message=None, disc=None, cancelling=False, owned=None, foreign=False, pending=False, lmsg=None,
           lerr=None):
        base = self.T.st(self.T)
        base["muse"] = {"seq": lseq, "last": {"seq": lseq, "state": last, "error": lerr, "message": lmsg},
                        "active": active, "state": state, "url": url if active else None, "phone": phone,
                        "ble_name": ble, "message": message, "bridge": bridge, "cancelling": cancelling,
                        "cancel_pending": pending, "expires_in": 600, "owned": owned, "foreign": foreign,
                        "disconnect": None, "disconnect_last": disc or {"seq": 0, "state": None}}
        return base

    def live(self, script, plan, delay, answer=None):
        """États successifs (au temps donné ; la TV relit l'état toutes les 2 s), réponses imposées pour
        /api/tv/muse/* (corps des POST enregistrés)."""
        inject = ("<script>(function(){var f=window.fetch;window.__ST__=%s;window.__BODIES__=[];"
                  "window.fetch=function(p,o){var m=(o&&o.method)||'GET';"
                  "if(p.indexOf('/api/tv/state')===0){__CALLS__.push(m+' '+p);var d=window.__ST__;"
                  "return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(d);}});}"
                  "if(p.indexOf('/api/tv/muse/')===0){__CALLS__.push(m+' '+p);__BODIES__.push(p+' '+(o&&o.body));"
                  "var r=%s;return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(r);}});}"
                  "return f(p,o);};%s})();</script>"
                  % (json.dumps(script[0][1]), json.dumps(answer or {"ok": True, "error": None}),
                     "".join("setTimeout(function(){window.__ST__=%s;},%d);" % (json.dumps(x), at)
                             for at, x in script[1:])))
        res = self.render(self.st(), [], delay=delay, inject=inject + self.T.TOAST_SPY +
                          "<script>window.__PLAN__=%s;</script>%s" % (json.dumps(plan), MUSE_SNAP))
        return res, {x["tag"]: x for x in res["extra"]}

    TO_MUSE = ["ArrowUp", "Enter", "ArrowDown", "Enter", "Enter"]

    def test_selecting_muse_shows_capabilities_and_safe_default_then_explicit_consent(self):
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter"], "tag": "sheet"},
                {"at": 1300, "keys": ["Enter"], "tag": "consent"},
                {"at": 1400, "keys": ["Enter"], "tag": "later"},          # OK sur « Pas maintenant »
                {"at": 1500, "keys": ["Enter"], "tag": "again"},
                {"at": 1600, "keys": ["ArrowUp"], "tag": "focus-yes"},
                {"at": 1700, "keys": ["Enter"], "tag": "yes"},
                {"at": 2600, "keys": [], "tag": "qr"}]
        script = [(0, self.st()), (1900, self.st(active=True, state="await_token", url=self.URL))]
        res, s = self.live(script, plan, delay=3200, answer={"ok": True, "error": None,
                                                             "muse": {"last": {"seq": 0}}})
        sh = s["sheet"]
        self.assertEqual((sh["ptitle"], sh["btns"], sh["focused"]), ("Gérer les assistants",
                                                                     ["Muse", "Droits des assistants", "Retour"], "Muse"))
        self.assertEqual(sh["subs"][0], "Connecter")
        c = s["consent"]
        self.assertEqual((c["ptitle"], c["btns"], c["focused"]),
                         ("Connecter Muse", ["Autoriser et connecter", "Pas maintenant"], "Pas maintenant"))
        self.assertEqual(c["caps"][:4], ["Te proposer des films à l'écran", "Apprendre tes goûts et gérer tes listes",
                                         "Naviguer dans OpenTV", "Lancer ou fermer un film quand tu le lui demandes"])
        self.assertEqual(c["clip"], [], c)
        self.assertEqual(c["posts"], [])                                  # choisir Muse n'accorde rien
        self.assertEqual(s["later"]["ptitle"], "Gérer les assistants")
        self.assertEqual(s["again"]["posts"], [])
        self.assertEqual(s["focus-yes"]["focused"], "Autoriser et connecter")
        self.assertEqual(s["yes"]["bodies"], ['/api/tv/muse/start {"consent":true}'])
        q = s["qr"]
        self.assertEqual((q["ptitle"], q["btns"], q["qr"]), ("Connecter Muse", ["Annuler"], "ok"))
        self.assertTrue(q["qrVisible"] and q["qrTop"], q)
        self.assertFalse(q["remotebox"])                                  # un seul QR à l'écran
        self.assertEqual(q["clip"], [], q)
        for x in res["extra"]:
            for bad in ("192.168", "mgst_", "sudo", "ssh", "jeton SDK", "token"):
                self.assertNotIn(bad, x["text"], (x["tag"], bad))

    def test_progress_phases_then_connected_announcement_once(self):
        def a(st, **k):
            return self.st(active=True, state=st, url=self.URL if st == "await_token" else None, **k)
        script = [(0, self.st()), (1500, a("await_token")), (3500, a("await_token", phone=True)),
                  (5500, a("preparing")), (7500, a("waiting_muse", ble="MuseGadget0A0B0C")),
                  (9500, a("connecting")), (11500, self.st(lseq=6, last="connected", state="connected",
                                                           bridge="ready"))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 2600, "keys": [], "tag": "qr"}, {"at": 4800, "keys": [], "tag": "phone"},
                {"at": 6800, "keys": [], "tag": "prep"},
                {"at": 8800, "keys": ["ArrowDown", "ArrowLeft"], "tag": "wait"}, {"at": 10800, "keys": [], "tag": "conn"},
                {"at": 12800, "keys": [], "tag": "done"}, {"at": 16000, "keys": [], "tag": "later"}]
        res, s = self.live(script, plan, delay=16500)
        self.assertEqual(s["qr"]["qr"], "ok")
        self.assertIn("Téléphone connecté", s["phone"]["pmeta"])
        self.assertEqual(s["prep"]["phases"], [["Préparation", "on"], ["En attente de Muse", ""], ["Connexion", ""],
                                               ["Muse est connectée", ""]])
        self.assertFalse(s["prep"]["qrVisible"])                          # plus de QR après le jeton
        w = s["wait"]
        self.assertEqual(w["phases"][:2], [["Préparation", "done"], ["En attente de Muse", "on"]])
        self.assertIn("Muse → Réglages → Appareils → Mode développeur → + → OpenTV", w["pmeta"])
        self.assertIn("MuseGadget0A0B0C", w["pmeta"])
        self.assertEqual(w["clip"], [], w)
        self.assertTrue(w["panel"])                                       # flèches : sans effet
        self.assertEqual(s["conn"]["phases"][2], ["Connexion", "on"])
        d = s["done"]
        self.assertFalse(d["panel"])
        self.assertTrue(d["toastBig"])
        self.assertTrue(d["toast"].startswith("Muse est connectée !"), d)
        self.assertEqual(sum(1 for t in s["later"]["toasts"] if t.startswith("Muse est connectée")), 1)
        self.assertEqual([b for b in s["later"]["bodies"] if "cancel" in b], [])

    def test_cancel_before_and_after_token_and_failure_offers_one_recovery(self):
        qr = self.st(active=True, state="await_token", url=self.URL)
        script = [(0, self.st()), (1500, qr), (3000, self.st(lseq=1, last="cancelled", state="cancelled"))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 2600, "keys": ["Escape"], "tag": "cancel"}, {"at": 4800, "keys": [], "tag": "after"}]
        res, s = self.live(script, plan, delay=5200)
        self.assertFalse(s["cancel"]["panel"])
        self.assertEqual(s["cancel"]["toast"], "Annulé : rien n'a changé.")
        self.assertIn("/api/tv/muse/cancel {}", s["after"]["bodies"])
        self.assertEqual(s["after"]["toasts"].count("Annulé : rien n'a changé."), 1)   # jamais annoncé deux fois
        # appariement en cours : Annuler attend l'issue réelle (fenêtre fermée par la boîte)
        wait = self.st(active=True, state="waiting_muse")
        script = [(0, self.st()), (1500, wait), (3500, self.st(active=True, state="waiting_muse", cancelling=True)),
                  (4500, self.st(lseq=4, last="cancelled", state="cancelled"))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 2600, "keys": ["Escape"], "tag": "cancel"}, {"at": 4600, "keys": [], "tag": "cancelling"},
                {"at": 6800, "keys": [], "tag": "after"}]
        res, s = self.live(script, plan, delay=7200)
        self.assertTrue(s["cancel"]["panel"])
        self.assertEqual(s["cancel"]["pmeta"], "Annulation en cours…")
        self.assertTrue(s["cancelling"]["panel"])
        self.assertEqual(s["cancelling"]["pmeta"], "Annulation en cours…")
        self.assertFalse(s["after"]["panel"])
        self.assertEqual(s["after"]["toast"], "Annulé : rien n'a changé.")
        # échec : une seule action de reprise, qui revient au consentement (jamais de relance implicite)
        msg = musesetup.message_for("window_closed")
        script = [(0, self.st()), (1500, wait), (2500, self.st(lseq=4, last="failed", state="failed", message=msg))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 4600, "keys": [], "tag": "failed"}, {"at": 4800, "keys": ["Enter"], "tag": "retry"}]
        res, s = self.live(script, plan, delay=5200)
        f = s["failed"]
        self.assertEqual((f["ptitle"], f["btns"], f["focused"], f["pmeta"]),
                         ("Muse n'est pas connectée", ["Réessayer", "Fermer"], "Réessayer", msg))
        r = s["retry"]
        self.assertEqual((r["ptitle"], r["focused"]), ("Connecter Muse", "Pas maintenant"))
        self.assertEqual([b for b in r["bodies"] if "start" in b], ['/api/tv/muse/start {"consent":true}'])

    def test_connected_muse_disconnect_needs_confirmation_with_safe_default(self):
        ready = self.st(bridge="ready", owned=True)
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter"], "tag": "sheet"},
                {"at": 1300, "keys": ["Enter"], "tag": "muse"}, {"at": 1400, "keys": ["ArrowUp", "Enter"], "tag": "ask"},
                {"at": 1500, "keys": ["Enter"], "tag": "no"}, {"at": 1600, "keys": ["ArrowUp", "Enter"], "tag": "ask2"},
                {"at": 1700, "keys": ["ArrowUp", "Enter"], "tag": "yes"},
                {"at": 4800, "keys": [], "tag": "done"}]
        script = [(0, ready), (2400, self.st(bridge=None, disc={"seq": 9, "state": "done"}))]
        res, s = self.live(script, plan, delay=5200)
        self.assertEqual(s["sheet"]["subs"][0], "Connectée · Déconnecter")
        m = s["muse"]
        self.assertEqual((m["ptitle"], m["btns"], m["focused"]), ("Muse est connectée", ["Déconnecter Muse", "Retour"],
                                                                  "Retour"))
        a = s["ask"]
        self.assertEqual((a["ptitle"], a["focused"]), ("Déconnecter Muse ?", "Non"))
        self.assertEqual(a["pmeta"], "Muse ne pourra plus rien faire sur la TV. Ton jeton et tes autres gadgets Muse "
                                     "ne changent pas.")                 # local seulement : jamais le jeton chez Muse
        self.assertEqual(a["clip"], [], a)
        self.assertEqual(s["no"]["ptitle"], "Muse est connectée")
        self.assertEqual(s["no"]["bodies"], [])
        self.assertEqual(s["yes"]["bodies"], ['/api/tv/muse/disconnect {"confirm":true}'])
        self.assertEqual(s["done"]["toast"], "Muse est déconnectée.")

    def owned_panel(self, st):
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter"], "tag": "sheet"},
                {"at": 1300, "keys": ["Enter"], "tag": "muse"}, {"at": 1400, "keys": ["ArrowUp", "Enter"], "tag": "ask"},
                {"at": 1500, "keys": ["Enter"], "tag": "no"}, {"at": 1600, "keys": ["ArrowUp", "ArrowUp", "Enter"],
                                                               "tag": "reconnect"}]
        res, s = self.live([(0, st)], plan, delay=2000)
        self.assertEqual(s["sheet"]["probes"], 0)
        self.assertEqual(s["muse"]["probes"], 1)                         # état de la boîte relu en ouvrant Muse
        return s

    def test_paused_bridge_owned_by_the_box_offers_confirmed_disconnect_and_reconnect(self):
        s = self.owned_panel(self.st(bridge="paused", owned=True))
        self.assertEqual(s["sheet"]["subs"][0], "En pause · Reconnecter ou déconnecter")
        m = s["muse"]
        self.assertEqual((m["ptitle"], m["btns"], m["focused"]),
                         ("Muse est en pause", ["Reconnecter", "Déconnecter Muse", "Retour"], "Retour"))
        self.assertEqual(m["clip"], [], m)
        self.assertEqual((s["ask"]["ptitle"], s["ask"]["focused"]), ("Déconnecter Muse ?", "Non"))
        self.assertEqual((s["no"]["ptitle"], s["no"]["focused"]), ("Muse est en pause", "Retour"))
        self.assertEqual(s["no"]["bodies"], [])                           # « Non » : rien envoyé
        r = s["reconnect"]
        self.assertEqual((r["ptitle"], r["focused"]), ("Connecter Muse", "Pas maintenant"))   # consentement de nouveau
        self.assertEqual(r["bodies"], [])

    def test_paired_but_not_ready_or_failed_reconnect_offers_disconnect(self):
        s = self.owned_panel(self.st(bridge=None, owned=True, lseq=3, last="failed"))
        self.assertEqual(s["sheet"]["subs"][0], "Pas connectée · Reconnecter ou déconnecter")
        m = s["muse"]
        self.assertEqual((m["ptitle"], m["btns"], m["focused"]),
                         ("Muse n'est pas connectée", ["Reconnecter", "Déconnecter Muse", "Retour"], "Retour"))
        self.assertEqual((s["ask"]["ptitle"], s["ask"]["focused"]), ("Déconnecter Muse ?", "Non"))

    def test_foreign_or_unproven_identity_never_offers_disconnect(self):
        for st, title in ((self.st(bridge="ready", owned=False, foreign=True), "Muse est installée autrement"),
                          (self.st(bridge="ready", owned=None), "Muse est connectée"),
                          (self.st(bridge="paused", owned=None), "Connecter Muse")):
            plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter"], "tag": "sheet"},
                    {"at": 1300, "keys": ["Enter"], "tag": "muse"},
                    {"at": 1400, "keys": ["ArrowUp", "ArrowUp", "Enter"], "tag": "up"}]
            res, s = self.live([(0, st)], plan, delay=1800)
            m = s["muse"]
            self.assertEqual(m["ptitle"], title, st["muse"])
            self.assertNotIn("Déconnecter Muse", m["btns"])
            self.assertNotIn("disconnect", json.dumps(s["up"]["bodies"]))
            self.assertNotEqual(s["up"]["ptitle"], "Déconnecter Muse ?")

    def test_ownership_learned_while_open_refreshes_the_choices_with_safe_focus(self):
        plan = [{"at": 1200, "keys": ["ArrowUp", "Enter", "ArrowDown", "Enter", "Enter"], "tag": "before"},
                {"at": 4200, "keys": [], "tag": "after"}]
        script = [(0, self.st(bridge="ready")), (2000, self.st(bridge="ready", owned=True))]
        res, s = self.live(script, plan, delay=4600)
        self.assertEqual(s["before"]["btns"], ["Retour"])
        a = s["after"]
        self.assertEqual((a["ptitle"], a["btns"], a["focused"]), ("Muse est connectée", ["Déconnecter Muse", "Retour"],
                                                                  "Retour"))

    def test_unresolved_cancel_says_requested_closable_then_real_outcome_announced(self):
        wait = self.st(active=True, state="connecting", lseq=2)
        cancelling = self.st(active=True, state="connecting", cancelling=True, lseq=2)
        pending = self.st(active=True, state="connecting", cancelling=True, pending=True, lseq=2)
        script = [(0, self.st(lseq=2)), (1500, wait), (3000, cancelling), (5000, pending),
                  (9500, self.st(lseq=9, last="connected", state="connected", bridge="ready", owned=True))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 2600, "keys": ["Escape"], "tag": "cancel"}, {"at": 4500, "keys": [], "tag": "cancelling"},
                {"at": 6500, "keys": [], "tag": "pending"}, {"at": 6700, "keys": ["Escape"], "tag": "closed"},
                {"at": 8800, "keys": [], "tag": "still"}, {"at": 11000, "keys": [], "tag": "done"}]
        res, s = self.live(script, plan, delay=11500)
        self.assertEqual(s["cancelling"]["pmeta"], "Annulation en cours…")
        p = s["pending"]
        self.assertTrue(p["panel"])
        self.assertIn("Annulation demandée", p["pmeta"])
        self.assertNotIn("rien n'a changé", p["text"])
        c = s["closed"]
        self.assertFalse(c["panel"])                                       # la feuille peut se fermer…
        self.assertTrue(c["toast"].startswith("Annulation demandée"), c)   # …sans prétendre que rien n'a changé
        self.assertFalse(s["still"]["panel"])                              # pas rouverte de force
        allt = s["done"]["toasts"]
        self.assertNotIn("Annulé : rien n'a changé.", allt)
        self.assertTrue(s["done"]["toast"].startswith("Muse est connectée !"), s["done"])   # issue réelle annoncée

    def paired_incomplete(self, owned, keys, delay=5300):
        msg = musesetup.message_for("paired_incomplete")
        conn = self.st(active=True, state="connecting", lseq=2, owned=owned)
        script = [(0, self.st(lseq=2, owned=owned)), (1500, conn),
                  (2500, self.st(lseq=5, last="failed", lerr="paired_incomplete", lmsg=msg, state="failed",
                                 owned=owned))]
        plan = [{"at": 1200, "keys": self.TO_MUSE + ["ArrowUp", "Enter"], "tag": "go"},
                {"at": 4700, "keys": [], "tag": "result"}, {"at": 4800, "keys": keys, "tag": "next"},
                {"at": 4900, "keys": ["Enter"], "tag": "after"}]
        res, s = self.live(script, plan, delay=delay)
        r = s["result"]
        self.assertEqual((r["ptitle"], r["pmeta"]), ("Muse est appairée", msg))   # jamais « Annulé », ni « connectée »
        for t in [r["toast"] or ""] + r["toasts"] + [r["text"]]:
            self.assertNotIn("rien n'a changé", t)
            self.assertNotIn("Muse est connectée", t)
        self.assertEqual(r["clip"], [], r)
        self.assertEqual([b for b in s["after"]["bodies"] if "disconnect" in b or "cancel" in b], [])
        return s

    def test_paired_but_bridge_incomplete_offers_reconnect_and_confirmed_disconnect(self):
        s = self.paired_incomplete(True, ["ArrowUp", "Enter"])
        r = s["result"]
        self.assertEqual((r["btns"], r["focused"]), (["Reconnecter", "Déconnecter Muse", "Fermer"], "Fermer"))
        self.assertEqual((s["next"]["ptitle"], s["next"]["focused"]), ("Déconnecter Muse ?", "Non"))
        self.assertNotEqual(s["after"]["ptitle"], "Déconnecter Muse ?")         # « Non » : rien envoyé
        s = self.paired_incomplete(True, ["ArrowUp", "ArrowUp", "Enter"])
        n = s["next"]
        self.assertEqual((n["ptitle"], n["focused"]), ("Connecter Muse", "Pas maintenant"))   # consentement de nouveau
        self.assertEqual([b for b in n["bodies"] if "start" in b], [])
        s = self.paired_incomplete(None, ["ArrowUp", "Enter"])                   # propriété pas établie : rien
        r = s["result"]
        self.assertEqual((r["btns"], r["focused"]), (["Reconnecter", "Fermer"], "Fermer"))
        self.assertNotIn("Déconnecter Muse", r["btns"])

    def test_reload_during_connection_reopens_the_sheet_and_announces_nothing_old(self):
        st = self.st(active=True, state="waiting_muse", lseq=7)
        st["muse"]["last"] = {"seq": 6, "state": "cancelled", "error": "cancelled"}
        res, s = self.live([(0, st)], [{"at": 1500, "keys": [], "tag": "x"}], delay=2000)
        x = s["x"]
        self.assertTrue(x["panel"])
        self.assertEqual(x["ptitle"], "Connecter Muse")
        self.assertIsNone(x["toast"])


# --------------------------------------------------------------------------- bout en bout réel (Playwright)
class E2EAdmin:
    """Administration Muse simulée pour le bout en bout : mêmes appels que museadmin.Admin depuis MuseHelper ;
    l'appariement passe par les vraies phases (fenêtre ouverte, connexion) et le vrai registre."""
    calls = []
    paired = False

    def __init__(self, app, progress=None, stdin=None, **kw):
        self.app, self.progress, self.stdin = app, progress or (lambda *a, **k: None), stdin
        self.waiting = False

    def local_token_match(self, tok):
        E2EAdmin.calls.append(("local_token_match",))
        return "same" if E2EAdmin.paired else "absent"

    def ownership(self):
        present = "muse-device" in self.app.registry.list()
        return {"paired": E2EAdmin.paired, "token": E2EAdmin.paired, "identity": "owned" if present else "absent",
                "identity_present": present, "foreign": False}

    def authorize_control(self, yes=False):
        E2EAdmin.calls.append(("authorize_control", yes))
        reg = self.app.registry
        v, fp, _ = reg.snapshot(("muse", "hermit", "hermes"))
        before = list(v["muse"]["allowed"])
        reg.apply_policy_changes({"muse": CONTROL}, ("muse", "hermit", "hermes"), fp)
        return {"ok": True, "changed": True, "policy_before": before}, 0

    def pair(self, **kw):
        E2EAdmin.calls.append(("pair", kw, self.stdin.readline()))
        self.progress("waiting_muse", ble_name="MuseGadget0A0B0C")
        time.sleep(3.0)
        self.progress("connecting")
        time.sleep(1.0)
        self.app.registry.issue("muse-device", CONTROL)
        E2EAdmin.paired = True
        return {"ok": True, "registered": True}, 0

    def registry(self):
        return self.app.registry

    def service_ids(self):
        return {"uid": None, "gid": None}


@unittest.skipUnless(find_chromium() and NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                     "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
class TestMuseEndToEnd(unittest.TestCase):
    def test_tv_consent_phone_encrypts_helper_pairs_tv_and_phone_follow(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        res = tr.Resolver()
        a = make_app(self, app_kw={"hostname": tr.HOSTNAME + ".lan", "resolver": res}, OPENTV_SETUP_PORT=str(port))
        res.app = a
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.registry.set_policy({"muse": ["feedback", "read"], "hermit": [], "hermes": []})
        a.muse.keygen = fast_key
        s = remote_servers(self, a)
        journal = tmpdir(self, "otvmj")
        os.chmod(journal, 0o700)
        E2EAdmin.calls, E2EAdmin.paired = [], False
        helper = musehelper.MuseHelper(a.cfg, os.getuid(), admin_factory=lambda **kw: E2EAdmin(a, **kw),
                                       chown=False, journal_dir=journal, journal_uid=os.getuid(),
                                       log=lambda ev, **f: None)
        stop, errors = threading.Event(), []

        def service():   # boucle du service (tic) : indépendante du helper, comme sur la boîte
            while not stop.is_set():
                try:
                    a.tick()
                except Exception as e:   # remonté au test (un fil ne doit jamais échouer en silence)
                    errors.append("service " + repr(e))
                    return
                stop.wait(0.2)

        def path_unit():   # opentv-muse-setup.path -> .service (root), processus séparé sur la boîte
            while not stop.is_set():
                try:
                    if os.listdir(a.cfg.muse_jobs_dir):
                        helper.run()
                except Exception as e:
                    errors.append("helper " + repr(e))
                    return
                stop.wait(0.2)
        threads = [threading.Thread(target=f, daemon=True) for f in (service, path_unit)]
        for t in threads:
            t.start()
            self.addCleanup(t.join, 10)
        self.addCleanup(stop.set)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tmpdir(self, "otvshots")
        os.makedirs(shots, exist_ok=True)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(), "shots": shots,
               "lan_port": port, "token": TOKEN}
        p = subprocess.run([NODE, os.path.join(PI4, "tests", "e2e_muse.js"), os.environ["OPENTV_TEST_PLAYWRIGHT"],
                            json.dumps(cfg)], capture_output=True, timeout=240)
        self.assertEqual(errors, [])
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        # TV : consentement explicite, QR seul, rien de technique à l'écran
        self.assertEqual(res["assistants"]["btns"], ["Muse", "Droits des assistants", "Retour"])
        c = res["consent"]
        self.assertEqual((c["title"], c["focused"]), ("Connecter Muse", "Pas maintenant"))
        self.assertEqual(res["posts_before_consent"], 0)
        self.assertEqual(res["probes_before_consent"], 1)                        # relecture seule (aucun droit)
        q = res["qr"]
        self.assertEqual((q["qr"], q["panel_in_view"], q["remotebox_hidden"], q["clipped"]), ("ok", True, True, []))
        for snap in (res["consent"], res["qr"], res["waiting"], res["connected"]):
            for bad in ("192.168", "127.0.0.1", "mgst_", "sudo", "ssh"):
                self.assertNotIn(bad, snap["text"], bad)
        self.assertEqual(res["origin"], "http://%s:%d" % (MDNS, port))   # nom mDNS vérifié : jamais d'IP
        # téléphone sans JavaScript : champ jamais affiché, rien envoyé
        self.assertIn("JavaScript désactivé", res["nojs_step2"]["text"])
        self.assertFalse(res["nojs_step2"]["input_visible"] or res["nojs_step2"]["form_visible"])
        # téléphone avec JavaScript sur http://<nom>.local : contexte non sécurisé, sans crypto.subtle
        self.assertEqual(res["secure_context"], [False, False])
        st1 = res["step1"]
        self.assertEqual((st1["official"], st1["target"]), (museweb.OFFICIAL_TOKEN_PAGE, "_blank"))
        self.assertIn("noreferrer", st1["rel"])
        self.assertGreaterEqual(st1["next_h"], 56)                               # « Utiliser mon jeton existant »
        self.assertIn("plusieurs appareils", st1["text"])
        self.assertIn("Ne révoque pas ce jeton", st1["text"])
        self.assertIn("step=2", res["step2_url"])
        self.assertIn("#", res["step2_url"])                                     # secret du QR gardé au rechargement
        self.assertEqual((res["step2"]["type"], res["step2"]["name"]), ("password", None))
        self.assertIn("jeton Muse complet", res["bad"]["err"])
        self.assertEqual(res["bad"]["value"], "")                                # jamais réaffiché
        self.assertIn("step=3", res["step3_url"])
        self.assertEqual(res["storage"], [0, 0, ""])
        for r in res["requests"]:                                                 # ni URL, ni corps, ni en-tête
            blob = r["url"] + r["body"] + json.dumps(r["headers"])
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn(TOKEN[5:25], blob)
        for r in res["requests"]:                                                 # téléphone : le nom, jamais l'IP
            self.assertTrue(r["url"].startswith("http://%s:%d/" % (MDNS, port)), r["url"])
        submits = [r for r in res["requests"] if r["url"].endswith("/muse/submit")]
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0]["headers"].get("origin"), res["origin"])     # vraie Origin, même origine
        self.assertEqual(set(json.loads(submits[0]["body"])), {"s", "c", "ct"})
        # helper : consentement TV -> autorisation, puis pair avec les options figées et le jeton
        calls = [c for c in E2EAdmin.calls if c[0] in ("local_token_match", "authorize_control", "pair")]
        self.assertEqual(calls[0], ("local_token_match",))               # jeton local comparé AVANT l'autorisation
        self.assertEqual(calls[1], ("authorize_control", True))
        self.assertEqual(calls[2], ("pair", {"token_stdin": True, "replace_token": False, "force": False,
                                             "replace_identity": False, "keep_token": True}, TOKEN + "\n"))
        self.assertEqual(res["waiting"]["phases"][1], ["En attente de Muse", "on"])
        self.assertIn("Mode développeur", res["waiting"]["meta"])
        self.assertTrue(res["phone_waiting"].startswith("En attente de Muse"))
        self.assertTrue(res["connected"]["toast"].startswith("Muse est connectée !"))
        self.assertTrue(res["phone_connected"].startswith("Muse est connectée"))
        self.assertNotIn("step=3", res["back"]["url"])                          # l'étape 2 a été remplacée
        self.assertFalse(res["back"]["input"])                                   # Retour : jamais redemandé
        self.assertIn("Termine dans l'app Muse", res["back"]["text"])
        self.assertEqual(res["assistants_after"]["btns"][0], "Muse")
        self.assertEqual((res["muse_connected"]["title"], res["muse_connected"]["focused"]),
                         ("Muse est connectée", "Retour"))
        self.assertEqual((res["disconnect_confirm"]["title"], res["disconnect_confirm"]["focused"]),
                         ("Déconnecter Muse ?", "Non"))
        self.assertTrue(res["after_escape"]["hidden"])
        self.assertEqual(sorted(a.registry.policy()["muse"]), CONTROL)
        self.assertEqual(jobs(a), [])
        self.assertNotIn(TOKEN, json.dumps(a.test_logs))


if __name__ == "__main__":
    unittest.main()
