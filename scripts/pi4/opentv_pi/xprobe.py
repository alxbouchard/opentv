"""X TV — sonde canari 0.7.8 (preuve terrain, jamais une action) : l'extension est-elle VRAIMENT injectée et son service
actif sur /tv et x.com sous la vraie politique du kiosque, et les touches physiques atteignent-elles la page ?

Chaîne, sans disque ni journal de contenu :
1. nav.js (x.com, cadre principal) envoie au service de l'extension, borné et limité (≥ 400 ms entre deux envois,
   au plus SEND_MAX par chargement), un INSTANTANÉ cumulatif : mode de la page, compteurs de touches RÉELLES par classe
   (`move` = flèches, `ok` = OK/Entrée, `other`), compteur de touches synthétiques, et pour la dernière touche réelle
   seulement des booléens (fiable, champ éditable au focus, document au focus, traitée par OpenTV). Jamais une valeur
   de touche, un code, un texte, une URL ni la valeur d'un champ.
2. background.js : expéditeur vérifié (CETTE extension, cadre principal, page attendue) ; la PAGE source (« x » ou
   « tv ») est déduite de l'expéditeur, jamais du message ; instantané reconstruit sur une liste FERMÉE ; au plus un
   envoi en vol, coalescé ; POST PROBE_PATH sur la boucle locale avec la clé TV éphémère de ce démarrage. Sur /tv :
   battement « tv » après capture de la clé (tvkey.js injecté + service actif + clé valide).
3. ce module : portes TV complètes déjà passées (loopback, Host exact, UID du pair = kiosque, clé TV), puis Origin
   EXACTEMENT chrome-extension://<ID> (obligatoire), Referer absent ou de l'extension, JSON ≤ BODY_MAX, champs fermés,
   débit ≤ RATE_MAX par RATE_WINDOW_S et par page (429 au-delà). Dernier instantané par page en MÉMOIRE seulement,
   TTL_S court ; perdu au redémarrage du service ; jamais journalisé, audité ni écrit sur disque.
4. lecture opérateur : GET STATUS_PATH sur la boucle locale, réservé à root (UID du pair TCP, /proc/net/tcp ; contrôle
   indisponible = refus), via `sudo opentv-pi x-probe`. Jamais lisible par le kiosque, une page ou un agent.
"""
import json
import re
import threading
import time

from . import VERSION, xcontext

PROBE_PATH = "/api/tv/x/probe"
STATUS_PATH = "/admin/x-probe"
OPERATOR_UIDS = frozenset({0})
TTL_S = 120.0
RATE_WINDOW_S = 10.0
RATE_MAX = 40
BODY_MAX = 1024
COUNT_MAX = 1000000
PAGES = ("tv", "x")
MODES = frozenset(("tv", "feed", "controls", "dialog", "loading"))
EVENTS = frozenset(("load", "mode", "key", "beat"))
CLASSES = frozenset(("move", "ok", "other"))
FIELDS = frozenset(("v", "page", "mode", "event", "seq", "move", "ok", "other", "synthetic", "last", "ext", "sw_s"))
LAST_FIELDS = frozenset(("cls", "trusted", "editable", "doc_focus", "handled"))
EXT_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= COUNT_MAX


def validate(body):
    """-> instantané normalisé (copie, champs fermés) ou None. Toute valeur hors liste fermée : refus entier."""
    if not isinstance(body, dict) or set(body) != FIELDS:
        return None
    if body["v"] != 1 or isinstance(body["v"], bool) or body["page"] not in PAGES or body["mode"] not in MODES or \
            body["event"] not in EVENTS:
        return None
    if not all(_count(body[k]) for k in ("seq", "move", "ok", "other", "synthetic", "sw_s")):
        return None
    if not isinstance(body["ext"], str) or not EXT_RE.fullmatch(body["ext"]):
        return None
    if (body["page"] == "tv") != (body["mode"] == "tv"):   # /tv n'a qu'un mode ; x.com jamais « tv »
        return None
    last = body["last"]
    if last is not None:
        if not isinstance(last, dict) or set(last) != LAST_FIELDS or last["cls"] not in CLASSES or \
                not all(isinstance(last[k], bool) for k in ("trusted", "editable", "doc_focus", "handled")):
            return None
        last = {k: last[k] for k in sorted(LAST_FIELDS)}
    out = {k: body[k] for k in ("page", "mode", "event", "seq", "synthetic", "ext", "sw_s")}
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


def handle_probe(handler, app):
    """POST PROBE_PATH, appelé par web.TVHandler APRÈS ses portes (loopback, Host, UID kiosque, clé TV)."""
    if not app.cfg.x_tv:
        return 404, {"ok": False, "error": "not_found"}
    if (handler.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
        return 415, {"ok": False, "error": "content_type"}
    if not origin_ok(handler.headers):
        return 403, {"ok": False, "error": "origin"}
    raw = handler.read_body(BODY_MAX)
    if raw is None:
        return 413, {"ok": False, "error": "too_large"}
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return 400, {"ok": False, "error": "bad_json"}
    return app.x_probe.record(body)


def status_view(app):
    """Vue opérateur (GET STATUS_PATH, root seulement)."""
    out = {"ok": True, "enabled": bool(app.cfg.x_tv), "version": VERSION}
    out.update(app.x_probe.view() if app.cfg.x_tv else {"pages": {}})
    return out
