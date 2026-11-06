"""X TV — lecture opérateur de la sonde canari 0.7.8 (`sudo opentv-pi x-probe`, root) : preuve, jamais une action.

1. écoute vérifiée AVANT toute requête : la connexion TCP vers 127.0.0.1:<port TV> doit aboutir à un socket du COMPTE
   du service OpenTV (UID du côté serveur dans /proc/net/tcp, peeruid ; une connexion acceptée hérite de l'UID de
   l'écoute). Contrôle indisponible ou autre compte : refus, rien n'est demandé ni lu — un processus local qui aurait
   pris le port pendant un redémarrage du service ne peut fournir aucune « preuve » ;
2. réponse revalidée sur un schéma FERMÉ, identique à xprobe.status_view (status_clean) : copie reconstruite champ par
   champ ; malformée, champ inconnu, type ou borne hors liste : refus entier (`invalid_response`), rien n'en est
   recopié ; refus du service : code d'une liste fermée seulement (status_error), jamais son texte ;
3. compteurs evdev EN MÉMOIRE de l'aide x-escape (xescape.read_nav : socket Unix du compte opentv-keys, UID du serveur
   vérifié par SO_PEERCRED, vue revalidée) joints sous `evdev`.
Rien n'est écrit ni journalisé.
"""
import http.client
import json

from . import peeruid, xescape, xprobe
from .xprobe import CLASSES, EVENTS, EXT_RE, HTTP_CLASSES, LAST_FIELDS, MODES, PAGES, TTL_S, _count, _enum

RAW_MAX = 64 * 1024
VERSION_RE = EXT_RE


PAGE_PRESENT_FIELDS = frozenset(("present", "mode", "event", "seq", "keys", "repeat", "synthetic", "doc_focus",
                                 "visible", "last", "ext", "sw_s", "age_s", "accepted", "rate_limited"))
STATUS_ENABLED_FIELDS = frozenset(("ok", "enabled", "version", "ttl_s", "pages", "rejected", "http"))
STATUS_DISABLED_FIELDS = frozenset(("ok", "enabled", "version", "pages"))
STATUS_ERRORS = frozenset(("operator_only", "forbidden", "host"))


def _num(v, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < hi


def _clean_page(page, e):
    """Entrée de page revalidée champ par champ -> copie reconstruite, ou None (tout écart : refus entier)."""
    if not isinstance(e, dict) or not _count(e.get("accepted")) or not _count(e.get("rate_limited")):
        return None
    base = {"accepted": e["accepted"], "rate_limited": e["rate_limited"]}
    if set(e) == {"present", "accepted", "rate_limited"} and e["present"] is False:
        return dict(base, present=False)
    if set(e) != PAGE_PRESENT_FIELDS or e["present"] is not True:
        return None
    if not _enum(e["mode"], MODES) or (page == "tv") != (e["mode"] == "tv") or not _enum(e["event"], EVENTS):
        return None
    if not all(_count(e[k]) for k in ("seq", "repeat", "synthetic", "sw_s")):
        return None
    if not all(isinstance(e[k], bool) for k in ("doc_focus", "visible")) or not _num(e["age_s"], TTL_S):
        return None
    if not isinstance(e["ext"], str) or not EXT_RE.fullmatch(e["ext"]):
        return None
    k = e["keys"]
    if not isinstance(k, dict) or set(k) != set(CLASSES) or not all(_count(k[c]) for c in CLASSES):
        return None
    last = e["last"]
    if last is not None:
        if not isinstance(last, dict) or set(last) != LAST_FIELDS or not _enum(last["cls"], CLASSES) or \
                not all(isinstance(last[f], bool) for f in LAST_FIELDS - {"cls"}):
            return None
        last = {f: last[f] for f in sorted(LAST_FIELDS)}
    out = dict(base, present=True, mode=e["mode"], event=e["event"], doc_focus=e["doc_focus"],
               visible=e["visible"], ext=e["ext"], age_s=e["age_s"], last=last,
               keys={c: k[c] for c in sorted(CLASSES)})
    out.update({f: e[f] for f in ("seq", "repeat", "synthetic", "sw_s")})
    return out


def status_clean(body):
    """(CLI root) réponse 200 de STATUS_PATH revalidée sur un schéma FERMÉ, identique à status_view -> copie
    reconstruite champ par champ, ou None : réponse malformée, champ inconnu, type ou borne hors liste = refus entier,
    rien n'en est recopié (une écoute usurpée ou un service altéré ne peut ni forger une preuve ni injecter du texte)."""
    if not isinstance(body, dict) or body.get("ok") is not True or not isinstance(body.get("version"), str) or \
            not VERSION_RE.fullmatch(body["version"]) or not isinstance(body.get("enabled"), bool):
        return None
    if body["enabled"] is False:
        if set(body) != STATUS_DISABLED_FIELDS or body["pages"] != {}:
            return None
        return {"ok": True, "enabled": False, "version": body["version"], "pages": {}}
    if set(body) != STATUS_ENABLED_FIELDS or type(body["ttl_s"]) is not int or body["ttl_s"] != int(TTL_S) or \
            not _count(body["rejected"]):
        return None
    h = body["http"]
    if not isinstance(h, dict) or set(h) != set(HTTP_CLASSES) or not all(_count(h[c]) for c in HTTP_CLASSES):
        return None
    pages = body["pages"]
    if not isinstance(pages, dict) or set(pages) != set(PAGES):
        return None
    clean = {p: _clean_page(p, pages[p]) for p in PAGES}
    if any(v is None for v in clean.values()):
        return None
    return {"ok": True, "enabled": True, "version": body["version"], "ttl_s": body["ttl_s"],
            "rejected": body["rejected"], "http": {c: h[c] for c in HTTP_CLASSES}, "pages": clean}


def status_error(status, body):
    """(CLI root) refus du service -> code d'erreur d'une liste FERMÉE seulement ; jamais le texte du serveur."""
    if status in (403, 421) and isinstance(body, dict) and _enum(body.get("error"), STATUS_ERRORS):
        return body["error"]
    return "refused"


def listener_uid(port, local_port, proc_root="/proc"):
    """UID du socket SERVEUR de notre connexion (127.0.0.1:port <-> 127.0.0.1:local_port), lu dans /proc/net/tcp ;
    None si indisponible (hors Linux) : refus."""
    return peeruid.tcp_peer_uid("127.0.0.1", port, local_port, proc_root)


def read_status(port, service_uid, timeout=3.0, listener=None):
    """-> (code de sortie, vue fermée) ; GET STATUS_PATH seulement si l'écoute appartient à `service_uid`."""
    if service_uid is None:
        return 1, {"ok": False, "error": "accounts"}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.connect()
        if (listener or listener_uid)(port, c.sock.getsockname()[1]) != service_uid:   # None compris : refus
            return 1, {"ok": False, "error": "listener"}
        c.request("GET", xprobe.STATUS_PATH, headers={"Host": "127.0.0.1:%d" % port})
        r = c.getresponse()
        raw = r.read(RAW_MAX + 1)
    except (OSError, http.client.HTTPException):
        return 1, {"ok": False, "error": "service_unreachable"}
    finally:
        c.close()
    try:
        body = json.loads(raw.decode("utf-8")) if len(raw) <= RAW_MAX else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        body = None
    if r.status != 200:
        return 1, {"ok": False, "error": status_error(r.status, body), "status": r.status}
    clean = status_clean(body)
    if clean is None:
        return 1, {"ok": False, "error": "invalid_response"}
    return 0, clean


def operator_view(port, service_uid, keys_uid, timeout=3.0, listener=None):
    """`sudo opentv-pi x-probe` -> (code de sortie, sortie) : vue de la sonde (ou refus fermé) + `evdev`."""
    evdev = xescape.read_nav(xescape.REQUEST_DIR, keys_uid) if keys_uid is not None else \
        {"present": False, "error": "accounts"}
    rc, out = read_status(port, service_uid, timeout=timeout, listener=listener)
    out["evdev"] = evdev
    return rc, out
