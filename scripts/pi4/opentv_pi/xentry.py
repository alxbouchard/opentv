"""X TV — saisie de l'IDENTIFIANT X depuis le téléphone déjà appairé (0.7.10 ; OPENTV_X_PHONE_ENTRY, défaut 0).

But unique : remplir UN identifiant (nom d'utilisateur, courriel ou téléphone) dans le champ de connexion X éligible au
focus, sans clavier physique ; Alex continue ensuite lui-même (passkey native). Jamais un mot de passe, un code, une
passkey ni un paiement ; aucun clic ni envoi automatique ; le téléphone et les assistants n'ouvrent jamais X.

Chaîne (le service ne voit QUE des données publiques et un chiffré de taille fixe) :
1. Sur la TV, X déjà ouvert par la télécommande (témoin xescape « X ouvert »), Alex choisit la pastille « Saisir avec
   le téléphone » par un OK réel (xentry.js, monde isolé de l'extension). L'extension tire une paire RSA-OAEP-2048
   (SHA-256) NEUVE, clé privée NON EXPORTABLE (WebCrypto), et envoie seulement la clé publique (n, e) à TV_PREFIX+start
   (portes TV + Origin exacte de l'extension). Réponse : identifiant de séance `sid` et adresse de la télécommande.
2. L'extension affiche un QR : adresse + `#<secret>.<empreinte SHA-256 de n>` (FRAGMENT : jamais envoyé au service).
3. Le téléphone déjà appairé (cookie, Origin, CSRF, en-tête REMOTE_HEADER) ouvre PHONE_PAGE ; le premier téléphone
   appairé RÉCLAME la séance (clé publique relue), vérifie l'empreinte du QR et e = 65537, chiffre {v, s, f, t} en
   RSA-OAEP (ui/muse.js, JavaScript pur : pas de crypto.subtle en HTTP LAN) et envoie UN chiffré de 256 octets.
4. L'extension relève le chiffré UNE fois (poll), le déchiffre, vérifie s/f, nettoie et borne l'identifiant, et ne
   remplit que si le MÊME champ est toujours au focus et éligible ; puis `done` (filled / refused / rejected).

Limite dite telle quelle : les pages du téléphone sont servies en HTTP sur le LAN. Un écouteur PASSIF ne voit que le
chiffré (il peut voler le cookie du téléphone pour réclamer ou annuler la séance — déni — mais rien injecter sans le
secret du QR) ; un attaquant ACTIF du LAN, ou un Pi/service OpenTV compromis, peut remplacer le script de la page :
aucune cryptographie livrée par un canal non authentifié ne l'empêche (aucune promesse « bout en bout »). L'extension
refuse donc elle-même codes, contrôles et longueurs. Aucun repli en clair.

Séance : UNE, mémoire seulement, TTL_S monotones ; close à la livraison, au refus, à l'annulation (TV ou téléphone),
à l'expiration, à la perte du témoin, au chargement de /tv (Retour Samsung : le kiosque relancé recharge /tv) et au
redémarrage : dictionnaire vidé, références lâchées (mémoire rendue par le ramasse-miettes de Python, jamais réécrite
en place). Jamais journalisée (seulement l'issue), jamais sur disque, absente des vues opérateur/TV/agents. Seule
l'ISSUE d'une séance terminée (sans matériel) reste TOMB_S pour la page du téléphone.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

from . import xcontext, xescape

TV_PREFIX = "/api/tv/x/entry/"
TV_OPS = ("start", "poll", "done", "cancel")
PHONE_PAGE = "/r/x"
PHONE_STATUS = "/r/x/status"
PHONE_POSTS = ("/r/x/claim", "/r/x/submit", "/r/x/cancel")
PHONE_STATIC = {"muse.js": "application/javascript; charset=utf-8", "xphone.js": "application/javascript; charset=utf-8"}
REMOTE_HEADER = "X-OpenTV-Remote"
TTL_S = 120.0
TOMB_S = 60.0
BODY_MAX = 1024
K = 256                                   # RSA-2048 : n et chiffré font exactement 256 octets
E = "AQAB"                                # 65537 seulement (jamais e = 1 ni un petit exposant)
SID_RE = re.compile(r"[A-Za-z0-9_-]{22}")
B64K_RE = re.compile(r"[A-Za-z0-9_-]{342}")   # 256 octets en base64url sans remplissage
# Automate (une séance) : waiting -> claimed -> submitted -> delivering -> issue ; issue = delivered | refused (champ
# changé/plus éligible) | rejected (contenu illisible/altéré/autre secret/code) | cancelled | expired.
# - TV `done` : seulement depuis delivering (chiffré remis à l'extension par `poll`) ;
# - téléphone `phone_cancel` : seulement AVANT la remise (PHONE_CANCELLABLE) ; delivering -> 409 too_late (séance
#   intacte) ; séance finie -> 409 over + issue réelle : une issue n'est JAMAIS réécrite ;
# - TV `cancel`, TTL, perte du témoin : séance vivante seulement (l'extension n'a alors rien rempli).
RESULTS = {"filled": "delivered", "refused": "refused", "rejected": "rejected"}
PHONE_STATES = {"claimed": "waiting", "submitted": "sent", "delivering": "sent"}
PHONE_CANCELLABLE = ("waiting", "claimed", "submitted")


def _noop(event, **fields):
    pass


def _h(sid):
    return hashlib.sha256(sid.encode("utf-8", "replace")).digest()


def _b64k(text):
    """Base64url de EXACTEMENT K octets (forme canonique) -> octets ; None sinon."""
    if not isinstance(text, str) or not B64K_RE.fullmatch(text):
        return None
    raw = base64.urlsafe_b64decode(text + "==")
    return raw if len(raw) == K and base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == text else None


class XEntry:
    """UNE séance en mémoire. Réponses (code HTTP, corps) ; jamais de matériel dans repr, journal ni vue."""

    def __init__(self, run_dir, mono=time.monotonic, log=_noop, presence=None):
        self.mono, self.log = mono, log
        self.presence = presence or (lambda: xescape.read_marker(run_dir, os.getuid()))
        self.lock = threading.Lock()
        self._s = None
        self._tomb = None   # (SHA-256 du sid, issue, échéance) : issue SEULE d'une séance terminée (page du téléphone)

    def __repr__(self):
        return "<XEntry active=%s>" % (self._s is not None)

    __str__ = __repr__

    def _wipe(self, final):   # sous self.lock
        s = self._s
        if s is None:
            return
        self._tomb = (_h(s["sid"]), final, self.mono() + TOMB_S)
        s.clear()   # n, chiffré, téléphone : références lâchées tout de suite
        self._s = None
        self.log("x_entry", state=final)

    def _live(self, sid):   # sous self.lock -> séance vivante de cet identifiant, sinon None
        s = self._s
        if s is not None and self.mono() >= s["exp"]:
            self._wipe("expired")
        elif s is not None and not self.presence():   # X fermé (Retour, /tv) : plus rien à remplir
            self._wipe("cancelled")
        s = self._s
        if s is None or not isinstance(sid, str) or not SID_RE.fullmatch(sid):
            return None
        return s if hmac.compare_digest(sid, s["sid"]) else None

    def _final(self, sid):   # sous self.lock : issue d'une séance terminée, sinon « gone »
        t = self._tomb
        if t is not None and self.mono() >= t[2]:
            self._tomb = t = None
        return t[1] if t is not None and isinstance(sid, str) and hmac.compare_digest(_h(sid), t[0]) else "gone"

    def active(self):
        with self.lock:
            s = self._s
            return s is not None and self._live(s["sid"]) is not None

    def wipe_all(self, final="cancelled"):
        with self.lock:
            self._wipe(final)

    # -- extension (portes TV + Origin de l'extension déjà passées)
    def start(self, body):
        if not isinstance(body, dict) or set(body) != {"n", "e"} or body["e"] != E:
            return 400, {"ok": False, "error": "bad_key"}
        n = _b64k(body["n"])
        if n is None or n[0] < 0x80:   # module de 2048 bits exactement
            return 400, {"ok": False, "error": "bad_key"}
        if not self.presence():
            return 409, {"ok": False, "error": "x_not_open"}
        with self.lock:
            self._wipe("cancelled")   # UNE séance : une nouvelle demande remplace l'ancienne
            sid = secrets.token_urlsafe(16)
            self._s = {"sid": sid, "n": body["n"], "exp": self.mono() + TTL_S, "phone": None, "state": "waiting",
                       "ct": None}
        self.log("x_entry", state="started")
        return 200, {"ok": True, "sid": sid, "ttl_s": int(TTL_S)}

    def poll(self, sid):
        with self.lock:
            s = self._live(sid)
            if s is None:
                return 200, {"ok": True, "state": self._final(sid), "ct": None}
            st, ct = s["state"], None
            if st == "submitted":   # relayé UNE fois, puis oublié par le service
                ct, s["ct"], s["state"] = s["ct"], None, "delivering"
            return 200, {"ok": True, "state": st, "ct": ct}

    def done(self, sid, result):
        if not isinstance(result, str) or result not in RESULTS:
            return 400, {"ok": False, "error": "bad_result"}
        with self.lock:
            s = self._live(sid)
            if s is None or s["state"] != "delivering":
                return 409, {"ok": False, "error": "not_delivering"}
            self._wipe(RESULTS[result])
        return 200, {"ok": True}

    def cancel(self, sid):
        with self.lock:
            if self._live(sid) is not None:
                self._wipe("cancelled")
        return 200, {"ok": True}

    # -- téléphone appairé (cookie, Origin, CSRF, en-tête déjà vérifiés)
    def exists(self, sid):
        with self.lock:
            return self._live(sid) is not None

    def claim(self, sid, phone_id):
        with self.lock:
            s = self._live(sid)
            if s is None:
                return 410, {"ok": False, "error": "expired"}
            if s["phone"] not in (None, phone_id):
                return 409, {"ok": False, "error": "claimed"}
            if s["state"] not in ("waiting", "claimed"):
                return 409, {"ok": False, "error": "already_submitted"}
            s["phone"], s["state"] = phone_id, "claimed"
            return 200, {"ok": True, "n": s["n"], "e": E}

    def submit(self, sid, phone_id, ct):
        with self.lock:
            s = self._live(sid)
            if s is None:
                return 410, {"ok": False, "error": "expired"}
            if s["phone"] != phone_id:
                return 409, {"ok": False, "error": "claimed" if s["phone"] else "not_claimed"}
            if s["state"] != "claimed":
                return 409, {"ok": False, "error": "already_submitted"}
            raw = _b64k(ct)
            if raw is None or int.from_bytes(raw, "big") >= int.from_bytes(_b64k(s["n"]), "big"):
                return 400, {"ok": False, "error": "bad_ct"}
            s["ct"], s["state"] = ct, "submitted"
        self.log("x_entry", state="submitted")
        return 200, {"ok": True}

    def phone_cancel(self, sid, phone_id):
        with self.lock:
            s = self._live(sid)
            if s is None:   # séance finie : issue réelle, jamais réécrite
                return 409, {"ok": False, "error": "over", "state": self._final(sid)}
            if s["phone"] not in (None, phone_id):
                return 409, {"ok": False, "error": "claimed"}
            if s["state"] not in PHONE_CANCELLABLE:   # chiffré déjà remis à l'extension : trop tard
                return 409, {"ok": False, "error": "too_late", "state": "sent"}
            self._wipe("cancelled")
        return 200, {"ok": True, "state": "cancelled"}

    def status(self, sid, phone_id):
        with self.lock:
            s = self._live(sid)
            if s is None:
                return self._final(sid)
            return PHONE_STATES.get(s["state"], "waiting") if s["phone"] == phone_id else "gone"


# --------------------------------------------------------------------------------------------- HTTP
def enabled(app):
    """Drapeau ET X TV ET télécommande : sinon aucune route, aucune UI, aucune séance."""
    return bool(app.cfg.x_tv and app.cfg.x_phone_entry and app.remote is not None)


def tv_page(app, page):
    """/tv : méta « opentv-x-entry » (lue par tvkey.js) — 1 seulement si la saisie est active."""
    return page.replace("{{X_ENTRY}}", "1" if enabled(app) else "0")


def on_tv(app):
    """Chargement de /tv (Retour Samsung, Accueil) : la séance éventuelle est effacée."""
    app.x_entry.wipe_all()


def static_types(app, base):
    """Scripts du téléphone servis par la télécommande SEULEMENT quand la saisie est active."""
    return dict(base, **PHONE_STATIC) if enabled(app) else base


def phone_base(app):
    """http://<nom mDNS vérifié ou adresse LAN>:<port réel de la télécommande>, ou None (télécommande fermée)."""
    with app.lock:
        ip = app.net["ip"] if app.net["mode"] in ("ethernet", "wifi") else None
        if app.remote is None or app.setup_mode or not ip or not app.remote_port_actual or app.remote_ip_actual != ip:
            return None
        host = app.mdns_name if (app.mdns_name and app.mdns["ok"] and app.mdns["ip"] == ip) else ip
        return "http://%s:%d" % (host, app.remote_port_actual)


def _read_json(handler):
    if (handler.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
        return None, (415, {"ok": False, "error": "content_type"})
    raw = handler.read_body(BODY_MAX)
    if raw is None:
        return None, (413, {"ok": False, "error": "too_large"})
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, (400, {"ok": False, "error": "bad_json"})
    return (body, None) if isinstance(body, dict) else (None, (400, {"ok": False, "error": "bad_json"}))


def _ext_origin(headers):
    if headers.get("Origin") != xcontext.EXTENSION_ORIGIN:
        return False
    r = headers.get("Referer")
    return r is None or r.startswith(xcontext.EXTENSION_ORIGIN + "/")


def handle_tv(handler, app):
    """POST TV_PREFIX<op>, après les portes de web.TVHandler (loopback, Host, UID kiosque, clé TV)."""
    op = urllib.parse.urlsplit(handler.path).path[len(TV_PREFIX):]
    if not enabled(app) or op not in TV_OPS:
        return 404, {"ok": False, "error": "not_found"}
    if not _ext_origin(handler.headers):
        return 403, {"ok": False, "error": "origin"}
    body, err = _read_json(handler)
    if err:
        return err
    if op == "start":
        base = phone_base(app)
        if base is None:
            return 409, {"ok": False, "error": "remote_unavailable"}
        code, out = app.x_entry.start(body)
        if code == 200:
            out["url"] = base + PHONE_PAGE + "?s=" + out["sid"]
        return code, out
    want = {"sid", "result"} if op == "done" else {"sid"}
    if set(body) != want:
        return 400, {"ok": False, "error": "bad_request"}
    if op == "poll":
        return app.x_entry.poll(body["sid"])
    if op == "done":
        return app.x_entry.done(body["sid"], body["result"])
    return app.x_entry.cancel(body["sid"])


TV_POSTS = {TV_PREFIX + op: handle_tv for op in TV_OPS}


def phone_get(handler, u):
    """GET PHONE_PAGE / PHONE_STATUS (web.RemoteHandler, après pair LAN + Host + télécommande active)."""
    app = handler.app
    if not enabled(app) or u.path not in (PHONE_PAGE, PHONE_STATUS):
        return handler.send(404, "introuvable", "text/plain; charset=utf-8")
    try:
        sid = (urllib.parse.parse_qs(u.query, max_num_fields=1).get("s") or [""])[0]
    except ValueError:
        sid = ""
    phone = handler.remote.session(handler._cookie_token())
    if u.path == PHONE_STATUS:
        if phone is None:
            return handler.send_json(401, {"ok": False, "error": "not_paired"})
        return handler.send_json(200, {"ok": True, "state": app.x_entry.status(sid, phone["id"])})
    if phone is None:
        return handler.send(403, message_page("Appaire d'abord ce téléphone", "Scanne le QR « Télécommande » de "
                                              "l'écran OpenTV, puis rescanne le QR affiché sur la TV."))
    if not app.x_entry.exists(sid):
        return handler.send(410, message_page("QR expiré", "Sur la TV, choisis de nouveau « Saisir avec le "
                                              "téléphone », puis scanne le nouveau QR."))
    return handler.send(200, entry_page(sid, handler.remote.csrf(phone)))


def phone_post(handler, path):
    """POST PHONE_POSTS (web.RemoteHandler, après pair LAN + Host + télécommande active) -> JSON."""
    app = handler.app
    if not enabled(app):
        return handler.send(404, "introuvable", "text/plain; charset=utf-8")
    origin = (handler.headers.get("Origin") or "").strip().lower()   # OBLIGATOIRE et exacte (fetch POST l'envoie)
    if origin != handler.origin or handler.headers.get(REMOTE_HEADER) != "x":
        return handler.send_json(403, {"ok": False, "error": "origin"})
    phone = handler.remote.session(handler._cookie_token())
    if phone is None:
        return handler.send_json(401, {"ok": False, "error": "not_paired"})
    body, err = _read_json(handler)
    want = {"s", "c", "ct"} if path == "/r/x/submit" else {"s", "c"}
    if err or set(body) != want:
        return handler.send_json(*(err or (400, {"ok": False, "error": "bad_request"})))
    if not handler.remote.check_csrf(phone, body["c"]):
        return handler.send_json(403, {"ok": False, "error": "csrf"})
    sid, x = body["s"], app.x_entry
    if path == "/r/x/claim":
        return handler.send_json(*x.claim(sid, phone["id"]))
    if path == "/r/x/submit":
        return handler.send_json(*x.submit(sid, phone["id"], body["ct"]))
    return handler.send_json(*x.phone_cancel(sid, phone["id"]))


# --------------------------------------------------------------------------------------------- pages (téléphone)
def _page(inner, data=None, script=False):
    attrs = "".join(" data-%s=\"%s\"" % (k, html.escape(v, quote=True)) for k, v in sorted((data or {}).items()))
    scripts = ("<script src=\"/static/muse.js\" defer></script><script src=\"/static/xphone.js\" defer></script>"
               if script else "")
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"referrer\" content=\"same-origin\"><meta name=\"color-scheme\" content=\"dark\">"
            "<title>Identifiant X</title><link rel=\"stylesheet\" href=\"/static/remote.css\">%s</head>"
            "<body><main id=\"xentry\"%s>%s</main></body></html>" % (scripts, attrs, inner))


def message_page(title, text):
    return _page("<h1>%s</h1><p class=\"lead\">%s</p><p class=\"more\"><a href=\"/r\">Télécommande</a></p>"
                 % (html.escape(title), html.escape(text)))


def entry_page(sid, csrf):
    """Champ SANS nom (jamais dans un formulaire envoyé) ; caché tant que xphone.js n'a pas vérifié la clé."""
    return _page(
        "<h1>Identifiant X</h1>"
        "<p class=\"xwarn\" id=\"xwarn\">Identifiant seulement — jamais mot de passe ou code</p>"
        "<p class=\"lead\">Nom d'utilisateur, courriel ou numéro de téléphone du compte X. Il part chiffré vers la "
        "TV, qui le place dans le champ au focus. Tu continues ensuite sur la TV avec ta clé d'accès.</p>"
        "<noscript><p class=\"err\">JavaScript est nécessaire : l'identifiant ne part que chiffré. Rien n'a été "
        "envoyé.</p></noscript>"
        "<p class=\"err\" id=\"xerr\" role=\"alert\" hidden></p>"
        "<form id=\"xform\" autocomplete=\"off\" hidden>"
        "<label for=\"xid\">Identifiant X</label>"
        "<input id=\"xid\" type=\"text\" inputmode=\"email\" autocomplete=\"off\" autocapitalize=\"none\" "
        "autocorrect=\"off\" spellcheck=\"false\" maxlength=\"100\" enterkeyhint=\"send\" data-1p-ignore=\"true\" "
        "data-lpignore=\"true\" aria-describedby=\"xwarn\">"
        "<button type=\"submit\" class=\"primary\" id=\"xsend\">Envoyer</button></form>"
        "<button type=\"button\" class=\"secondary\" id=\"xcancel\" hidden>Annuler</button>"
        "<p class=\"status\" id=\"xstatus\" role=\"status\" aria-live=\"polite\"></p>"
        "<p class=\"more\"><a href=\"/r\">Télécommande</a></p>",
        {"s": sid, "c": csrf}, script=True)
