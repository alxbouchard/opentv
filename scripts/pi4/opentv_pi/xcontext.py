"""X TV Mode (0.7.7) : contexte transitoire d'UN post X partagé par Alex vers Hermit/Muse.

Chaîne complète, sans API X ni lecture côté serveur :
1. la page /tv porte la clé TV de ce démarrage (meta « opentv-key ») ; l'extension locale du kiosque
   (/opt/opentv/share/x-tv-extension, ID fixe EXTENSION_ID) la relit à chaque visite de /tv (stockage de session de
   l'extension, jamais sur disque) ;
2. sur x.com, ← (télécommande) sur le post au focus : le script de contenu lit SEULEMENT le texte visible borné et
   l'auteur visible de CE post (aucune URL, aucun lien, aucun média, aucun cookie, aucun HTML) ;
3. le service de l'extension envoie POST /api/tv/x/context sur la boucle locale avec la clé : mêmes portes que
   /api/tv/* (loopback, Host exact, UID du pair = kiosque, clé TV), JSON seulement, corps borné (BODY_MAX), champs
   FERMÉS {author, text}, Origin/Referer étrangers refusés ;
4. ce module garde UN contexte en mémoire (TTL_S), nettoyé (contrôles, bidi, largeur nulle, adresses -> « [lien] »),
   jamais journalisé, jamais audité, jamais persisté, perdu au redémarrage du service ;
5. l'outil Agent Port `x_context` (scope read) le rend tel quel avec untrusted:true. Aucun envoi vers un agent :
   c'est un contexte prêt à lire quand Alex demande à Hermit ou Muse de résumer le post.

Le texte d'un post est un contenu externe NON FIABLE (injection de consignes possible) : il n'est jamais une
instruction pour l'agent.
"""
import base64
import hashlib
import json
import re
import threading
import time
import unicodedata

from . import validate

# ID Chromium de l'extension locale : dérivé de la clé PUBLIQUE de son manifest (aucune clé privée n'existe dans
# le dépôt ; elle n'a servi qu'à fixer cet ID). Politique du kiosque : ExtensionInstallAllowlist = [EXTENSION_ID].
EXTENSION_ID = "mmoccejphjkmbhejebkbhkbbmmocfafi"
EXTENSION_ORIGIN = "chrome-extension://" + EXTENSION_ID
SHARE_PATH = "/api/tv/x/context"
TEXT_MAX = 2400
AUTHOR_MAX = 120
TTL_S = 600.0
# 2400 caractères au pire en échappements JSON \uXXXX (6 octets) + auteur : 16 Kio suffisent, jamais plus
BODY_MAX = 16 * 1024
FIELDS = frozenset(("author", "text"))
LINK = "[lien]"
READY_MESSAGE = "Prêt pour Hermit ou Muse. Demande-lui de résumer ce post."

# catégories retirées : contrôles (sauf saut de ligne, tabulation -> espace), formats (bidi, largeur nulle, BOM…),
# substituts, usage privé, non attribués ; séparateurs de ligne/paragraphe -> saut de ligne
_DROP = frozenset(("Cc", "Cf", "Cs", "Co", "Cn"))
_TOKEN_RE = re.compile(r"\S+")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,24}", re.I)


def extension_id_from_key(b64key):
    """ID Chromium d'une extension = 32 premiers chiffres hexadécimaux du SHA-256 de la clé publique DER, a-p."""
    digest = hashlib.sha256(base64.b64decode(b64key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _is_address(tok):
    sk = validate._skeleton(tok)
    return bool(validate.URLISH_RE.search(tok) or validate.URLISH_RE.search(sk) or validate.STRICT_HOST_RE.search(sk)
                or validate.ADDR_EXTRA_RE.search(sk) or _EMAIL_RE.search(sk))


def _strip_chars(s):
    out = []
    for c in unicodedata.normalize("NFC", s):
        if c in "\r\n\u2028\u2029\x85":
            out.append("\n")
        elif c == "\t":
            out.append(" ")
        elif unicodedata.category(c) in _DROP or c in validate.MSG_INVISIBLE:
            continue
        elif unicodedata.category(c) == "Zs":
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)


def clean_text(s, cap):
    """Texte visible d'un post -> texte sûr à relire : caractères invisibles/de contrôle retirés, chaque jeton
    ressemblant à une adresse remplacé par « [lien] », espaces et sauts de ligne bornés, au plus `cap` caractères."""
    if not isinstance(s, str):
        raise ValueError("bad_fields")
    s = _strip_chars(s[:cap * 4].replace("\r\n", "\n"))
    s = _TOKEN_RE.sub(lambda m: LINK if _is_address(m.group(0)) else m.group(0), s)
    s = re.sub(r"[ ]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:cap].rstrip()


def clean_author(s):
    if not isinstance(s, str):
        raise ValueError("bad_fields")
    return re.sub(r"\s+", " ", clean_text(s, AUTHOR_MAX * 2)).strip()[:AUTHOR_MAX].rstrip()


class XContext:
    """UN contexte en mémoire (le dernier partagé), TTL_S. Aucun journal, aucun disque."""

    def __init__(self, mono=time.monotonic):
        self.mono = mono
        self.lock = threading.Lock()
        self._ctx = None

    def set(self, body):
        """-> (True, None) | (False, code). Corps refusé : le contexte précédent reste tel quel."""
        if not isinstance(body, dict) or set(body) != FIELDS or \
                not all(isinstance(body[k], str) for k in FIELDS):
            return False, "bad_fields"
        try:
            text = clean_text(body["text"], TEXT_MAX)
            author = clean_author(body["author"])
        except ValueError as e:
            return False, str(e)
        if not text:
            return False, "empty_text"
        with self.lock:
            self._ctx = {"author": author, "text": text, "at": self.mono()}
        return True, None

    def view(self):
        with self.lock:
            c = self._ctx
            if c is None:
                return {"available": False}
            age = self.mono() - c["at"]
            if age >= TTL_S or age < 0:
                self._ctx = None
                return {"available": False}
            return {"available": True, "source": "x", "author": c["author"], "text": c["text"],
                    "age_s": int(age), "untrusted": True}

    def clear(self):
        with self.lock:
            self._ctx = None


def share_response(enabled, store, body):
    """Route de partage (portes du kiosque déjà passées par web.TVHandler) -> (code HTTP, réponse JSON). Le contenu
    n'est ni journalisé, ni audité, ni écrit sur disque ; aucun agent n'est appelé."""
    if not enabled:
        return 404, {"ok": False, "error": "not_found"}
    ok, why = store.set(body)
    if not ok:
        return 400, {"ok": False, "error": why}
    return 200, {"ok": True, "message": READY_MESSAGE}


def origin_ok(headers):
    """Seule l'extension locale (ID fixe) peut émettre le partage depuis un navigateur : Origin absente (client du compte
    kiosque hors navigateur, déjà porteur de la clé) ou exactement chrome-extension://<ID>. Tout Origin web (x.com, la
    page /tv elle-même, « null », une autre extension) et tout Referer étranger sont refusés."""
    o = headers.get("Origin")
    if o is not None and o != EXTENSION_ORIGIN:
        return False
    r = headers.get("Referer")
    return r is None or r.startswith(EXTENSION_ORIGIN + "/")


def handle_share(handler, app):
    """POST /api/tv/x/context, appelé par web.TVHandler APRÈS ses portes (loopback, Host exact, UID du pair = kiosque,
    clé TV) -> (code HTTP, réponse JSON). JSON seulement, Origin de l'extension, corps ≤ BODY_MAX, champs fermés.
    Le contenu n'est jamais journalisé (journal HTTP : chemin + statut), audité ni écrit sur disque."""
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
    except (ValueError, UnicodeDecodeError, RecursionError):   # imbrication profonde : refus, jamais d'exception
        return 400, {"ok": False, "error": "bad_json"}
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "bad_json"}
    return share_response(app.cfg.x_tv, app.x_context, body)   # rien journalisé ni audité
