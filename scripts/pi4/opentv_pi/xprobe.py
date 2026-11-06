"""X TV — sonde canari 0.7.8 (preuve terrain, jamais une action) : l'extension est-elle VRAIMENT injectée et son service
actif sur /tv et x.com sous la vraie politique du kiosque, et les touches physiques atteignent-elles la page ?

Chaîne, sans disque ni journal de contenu :
1. nav.js (x.com, cadre principal) envoie au service de l'extension, borné et limité (≥ 400 ms entre deux envois,
   au plus SEND_MAX par chargement), un INSTANTANÉ cumulatif : mode de la page, compteurs d'APPUIS RÉELS par classe
   (`move` = flèches, `ok` = OK/Entrée, `other`) — répétitions (`e.repeat`, touche tenue) comptées à part dans
   `repeat`, si bien que move/ok se comparent 1:1 aux appuis evdev de l'aide x-escape —, compteur de touches
   synthétiques, document au focus et page visible (chaque instantané, battement compris : une absence de touches
   reste explicable), et pour la dernière touche réelle seulement des booléens (fiable, champ éditable au focus,
   document au focus, traitée par OpenTV). Champ éditable au focus : une touche `other` (lettre, chiffre, Retour
   arrière…) n'est NI comptée NI envoyée (longueur et rythme d'un mot de passe jamais observables). Jamais une valeur
   de touche, un code, un texte, une URL ni la valeur d'un champ.
2. background.js : expéditeur vérifié (CETTE extension, cadre principal, page attendue) ; la PAGE source (« x » ou
   « tv ») est déduite de l'expéditeur, jamais du message ; instantané reconstruit sur une liste FERMÉE ; au plus un
   envoi en vol, coalescé ; POST PROBE_PATH sur la boucle locale avec la clé TV éphémère de ce démarrage. Sur /tv :
   instantané « tv » à la capture de la clé puis battement toutes les 30 s (< TTL_S) envoyé par tvkey.js.
   Redémarrage du service pendant que X est ouvert : la clé gardée par l'extension (stockage de SESSION du navigateur,
   jamais sur disque) est périmée, la sonde est refusée (403, ignorée) et disparaît de la vue après TTL_S ; par
   conception l'extension ne reprend une clé QUE sur /tv (tvkey.js lit la page de CE démarrage) — retour à /tv
   (Retour/Accueil) et la sonde revient. Aucune autre voie d'acquisition ni persistance de la clé.
3. ce module : portes TV complètes déjà passées (loopback, Host exact, UID du pair = kiosque, clé TV), puis Origin
   EXACTEMENT chrome-extension://<ID> (obligatoire), Referer absent ou de l'extension, JSON ≤ BODY_MAX, champs fermés,
   débit ≤ RATE_MAX par RATE_WINDOW_S et par page (429 au-delà). Dernier instantané par page en MÉMOIRE seulement,
   TTL_S court ; perdu au redémarrage du service ; jamais journalisé, audité ni écrit sur disque.
4. lecture opérateur : GET STATUS_PATH sur la boucle locale, réservé à root (UID du pair TCP, /proc/net/tcp ; contrôle
   indisponible = refus), via `sudo opentv-pi x-probe`, qui y joint les compteurs evdev EN MÉMOIRE de l'aide
   x-escape (xescape.NavServer, même TTL). Jamais lisible par le kiosque, une page ou un agent.

Ce module porte aussi les routes HTTP X de web.TVHandler (câblage minimal côté web) : POSTS (partage xcontext, sonde,
témoin « X ouvert » de xescape) et la lecture opérateur avec sa porte root (guard_operator).
"""
import ipaddress
import json
import re
import threading
import time

from . import VERSION, xcontext, xescape

PROBE_PATH = "/api/tv/x/probe"
STATUS_PATH = "/admin/x-probe"
OPERATOR_UIDS = frozenset({0})
TTL_S = 120.0
RATE_WINDOW_S = 10.0
RATE_MAX = 40
BODY_MAX = 1024
PRESENCE_MAX = 2048
COUNT_MAX = 1000000
PAGES = ("tv", "x")
MODES = frozenset(("tv", "feed", "controls", "dialog", "loading"))
EVENTS = frozenset(("load", "mode", "key", "beat"))
CLASSES = frozenset(("move", "ok", "other"))
COUNTS = ("seq", "move", "ok", "other", "repeat", "synthetic", "sw_s")
FIELDS = frozenset(COUNTS + ("v", "page", "mode", "event", "doc_focus", "visible", "last", "ext"))
LAST_FIELDS = frozenset(("cls", "trusted", "editable", "doc_focus", "handled"))
EXT_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= COUNT_MAX


def _enum(v, allowed):
    """Chaîne de la liste fermée seulement : une liste/un dict (non hachable) n'atteint jamais `in` -> aucun 500."""
    return isinstance(v, str) and v in allowed


def validate(body):
    """-> instantané normalisé (copie, champs fermés) ou None. Toute valeur hors liste fermée : refus entier."""
    if not isinstance(body, dict) or set(body) != FIELDS:
        return None
    if type(body["v"]) is not int or body["v"] != 1 or not _enum(body["page"], PAGES) or \
            not _enum(body["mode"], MODES) or not _enum(body["event"], EVENTS):
        return None
    if not all(_count(body[k]) for k in COUNTS) or not all(isinstance(body[k], bool) for k in ("doc_focus", "visible")):
        return None
    if not isinstance(body["ext"], str) or not EXT_RE.fullmatch(body["ext"]):
        return None
    if (body["page"] == "tv") != (body["mode"] == "tv"):   # /tv n'a qu'un mode ; x.com jamais « tv »
        return None
    last = body["last"]
    if last is not None:
        if not isinstance(last, dict) or set(last) != LAST_FIELDS or not _enum(last["cls"], CLASSES) or \
                not all(isinstance(last[k], bool) for k in ("trusted", "editable", "doc_focus", "handled")):
            return None
        last = {k: last[k] for k in sorted(LAST_FIELDS)}
    out = {k: body[k] for k in ("page", "mode", "event", "seq", "repeat", "synthetic", "doc_focus", "visible", "ext",
                                "sw_s")}
    out["keys"] = {k: body[k] for k in ("move", "ok", "other")}
    out["last"] = last
    return out


class XProbe:
    """Dernier instantané par page (« tv », « x »), en mémoire, TTL_S ; débit borné par page. Aucun journal."""

    def __init__(self, mono=time.monotonic):
        self.mono = mono
        self.lock = threading.Lock()
        self._last = {}
        self._hits = {p: [] for p in PAGES}
        self._stats = {p: {"accepted": 0, "rate_limited": 0} for p in PAGES}
        self._rejected = 0

    def record(self, body):
        """-> (code HTTP, réponse). Refus : rien n'est modifié sauf le compteur de refus."""
        snap = validate(body)
        with self.lock:
            if snap is None:
                self._rejected = min(self._rejected + 1, COUNT_MAX)
                return 400, {"ok": False, "error": "bad_fields"}
            now = self.mono()
            page = snap["page"]
            hits = [t for t in self._hits[page] if 0 <= now - t < RATE_WINDOW_S]
            if len(hits) >= RATE_MAX:
                self._hits[page] = hits
                st = self._stats[page]
                st["rate_limited"] = min(st["rate_limited"] + 1, COUNT_MAX)
                return 429, {"ok": False, "error": "rate_limited"}
            hits.append(now)
            self._hits[page] = hits
            self._last[page] = (now, snap)
            st = self._stats[page]
            st["accepted"] = min(st["accepted"] + 1, COUNT_MAX)
        return 200, {"ok": True}

    def view(self):
        """Vue opérateur : pour chaque page, instantané vivant (âge < TTL_S) ou {"present": false} ; compteurs."""
        with self.lock:
            now = self.mono()
            pages = {}
            for p in PAGES:
                item = self._last.get(p)
                entry: dict = {"present": False}
                if item is not None:
                    age = now - item[0]
                    if 0 <= age < TTL_S:
                        entry = dict(item[1])
                        entry["keys"] = dict(entry["keys"])
                        entry["last"] = dict(entry["last"]) if entry["last"] else None
                        entry.pop("page", None)
                        entry["present"] = True
                        entry["age_s"] = round(age, 1)
                    else:
                        del self._last[p]
                entry["accepted"] = self._stats[p]["accepted"]
                entry["rate_limited"] = self._stats[p]["rate_limited"]
                pages[p] = entry
            return {"ttl_s": int(TTL_S), "pages": pages, "rejected": self._rejected}

    def clear(self):
        with self.lock:
            self._last.clear()


def origin_ok(headers):
    """Sonde : Origin OBLIGATOIRE et exactement chrome-extension://<ID> (le service de l'extension l'envoie toujours ;
    une page web, /tv, « null » ou une autre extension : refus) ; Referer absent ou de l'extension."""
    if headers.get("Origin") != xcontext.EXTENSION_ORIGIN:
        return False
    r = headers.get("Referer")
    return r is None or r.startswith(xcontext.EXTENSION_ORIGIN + "/")


def _json_ctype(handler):
    return (handler.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"


def _read_json(handler, cap):
    """-> (corps, None) ou (None, (code, réponse)) ; corps borné à `cap`, imbrication profonde refusée (jamais un 500)."""
    raw = handler.read_body(cap)
    if raw is None:
        return None, (413, {"ok": False, "error": "too_large"})
    try:
        return json.loads(raw.decode("utf-8") or "{}"), None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, (400, {"ok": False, "error": "bad_json"})


def handle_probe(handler, app):
    """POST PROBE_PATH, appelé par web.TVHandler APRÈS ses portes (loopback, Host, UID kiosque, clé TV)."""
    if not app.cfg.x_tv:
        return 404, {"ok": False, "error": "not_found"}
    if not _json_ctype(handler):
        return 415, {"ok": False, "error": "content_type"}
    if not origin_ok(handler.headers):
        return 403, {"ok": False, "error": "origin"}
    body, err = _read_json(handler, BODY_MAX)
    return err or app.x_probe.record(body)


def handle_presence(handler, app):
    """POST xescape.PRESENCE_PATH (page /tv, portes TV passées) : {"active": bool} exactement. Pose le témoin « X ouvert »
    juste avant l'ouverture de X (sortie KEY_EXIT de l'appareil, xescape) ; -> (code, réponse)."""
    if not _json_ctype(handler):
        return 415, {"ok": False, "error": "content_type"}
    body, err = _read_json(handler, PRESENCE_MAX)
    if err or not isinstance(body, dict):
        return err or (400, {"ok": False, "error": "bad_json"})
    if set(body) != {"active"} or not isinstance(body.get("active"), bool):
        return 400, {"ok": False, "error": "bad_request"}
    if body["active"] and not app.cfg.x_tv:
        return 409, {"ok": False, "error": "x_tv_disabled"}
    if not xescape.set_marker(app.cfg.run_dir, body["active"]):
        app.log("x_presence_failed")
        return 503, {"ok": False, "error": "marker"}
    return 200, {"ok": True, "active": body["active"]}


def clear_presence(app):
    """Chargement de /tv : jamais « X ouvert » (aucune relance du kiosque par KEY_EXIT sur /tv)."""
    xescape.set_marker(app.cfg.run_dir, False)


# routes POST X de web.TVHandler (après loopback, Host, UID kiosque, clé TV) : chacune porte ses propres contrôles
POSTS = {xcontext.SHARE_PATH: xcontext.handle_share, PROBE_PATH: handle_probe, xescape.PRESENCE_PATH: handle_presence}


def guard_operator(handler, app):
    """GET STATUS_PATH : loopback, Host exact, UID du pair TCP = root (sudo opentv-pi x-probe). Contrôle du pair
    indisponible (hors Linux, tv_client_uids None) : refus. Jamais le kiosque, une page, un agent ni un autre compte
    local. -> None (autorisé) ou (code, réponse)."""
    try:
        loop = ipaddress.ip_address(handler.client_address[0]).is_loopback
    except ValueError:
        loop = False
    if not loop:
        return 403, {"ok": False, "error": "forbidden"}
    port = handler.server.server_address[1]
    if handler.headers.get("Host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
        return 421, {"ok": False, "error": "host"}
    allowed = OPERATOR_UIDS if app.tv_client_uids is not None else None
    if allowed is None or app.peer_uid_fn(handler.client_address[0], handler.client_address[1], port) not in allowed:
        return 403, {"ok": False, "error": "operator_only"}
    return None


def status_view(app):
    """Vue opérateur (GET STATUS_PATH, root seulement)."""
    out = {"ok": True, "enabled": bool(app.cfg.x_tv), "version": VERSION}
    out.update(app.x_probe.view() if app.cfg.x_tv else {"pages": {}})
    return out


def handle_status(handler, app):
    """GET STATUS_PATH, appelé par web.TVHandler AVANT ses portes du kiosque (root seulement, jamais le kiosque)."""
    return guard_operator(handler, app) or (200, status_view(app))
