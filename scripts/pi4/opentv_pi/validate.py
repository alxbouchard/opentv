"""Validation stricte des entrées (formulaire QR, jobs réseau, agents). Lève ValueError(<code court>)."""
import re
import unicodedata

# « control » : pilotage sémantique d'OpenTV (proposer un film à l'écran, naviguer, lancer un film accepté par le lecteur
# officiel promu, fermer le lecteur). Jamais accordé par défaut ni par une installation : choix explicite du
# propriétaire (« Gérer les assistants » sur la TV, ou `opentv-pi muse authorize-control --yes` en root).
SCOPES = ("control", "display", "feedback", "read")
AGENT_ID_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")
HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")
# toute forme d'adresse : schéma, www., hôte.tld, IP, schémas dangereux (plus strict que le moteur : un message
# affiché sur la TV n'a jamais besoin d'un nom de domaine)
URLISH_RE = re.compile(r"[a-z][a-z0-9+.-]*://|(?<![a-z0-9])www\.|(?<![a-z0-9])(?:javascript|data|vbscript|file|blob|magnet):"
                       r"|(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:[:/]|\b)(?!\s*[a-zà-ÿ])|[0-9]{1,3}(?:\.[0-9]{1,3}){3}", re.I)


def _printable(s):
    return all(ord(c) >= 32 and ord(c) != 127 and not (0x80 <= ord(c) < 0xA0) for c in s)


def ssid(s):
    """SSID Wi-Fi : 1 à 32 octets UTF-8 imprimables, sans espace de bord ni « ; » (ambiguïté keyfile NM)."""
    if not isinstance(s, str):
        raise ValueError("ssid_type")
    n = len(s.encode("utf-8"))
    if not 1 <= n <= 32:
        raise ValueError("ssid_length")
    if not _printable(s) or ";" in s or s != s.strip():
        raise ValueError("ssid_chars")
    return s


def psk(s):
    """Phrase WPA-PSK : 8 à 63 caractères ASCII imprimables, ou clé brute de 64 hexadécimaux."""
    if not isinstance(s, str):
        raise ValueError("psk_type")
    if len(s) == 64:
        if HEX64_RE.fullmatch(s):
            return s
        raise ValueError("psk_length")
    if not 8 <= len(s) <= 63:
        raise ValueError("psk_length")
    if not all(0x20 <= ord(c) <= 0x7E for c in s):
        raise ValueError("psk_chars")
    return s


def device_name(s):
    """Nom affiché de l'appareil : 1 à 32 lettres/chiffres/espaces/apostrophes/tirets (NFC)."""
    if not isinstance(s, str):
        raise ValueError("name_type")
    if not _printable(s):
        raise ValueError("name_chars")
    s = unicodedata.normalize("NFC", s).strip()
    if not 1 <= len(s) <= 32:
        raise ValueError("name_length")
    if not all(c.isalnum() or c in " '-" for c in s) or "  " in s:
        raise ValueError("name_chars")
    return s


def agent_id(s):
    if not isinstance(s, str) or not AGENT_ID_RE.fullmatch(s):
        raise ValueError("bad_agent_id")
    return s


def scopes(items):
    if not isinstance(items, (list, tuple, set, frozenset)):
        raise ValueError("bad_scopes")
    out = sorted(set(items))
    if not all(isinstance(x, str) and x in SCOPES for x in out):
        raise ValueError("bad_scopes")
    return out


def no_url(s):
    return not URLISH_RE.search(s)


def message_text(s, max_len=140):
    """Message d'agent affiché sur la TV : une ligne, 1 à 140 caractères, aucune adresse."""
    if not isinstance(s, str):
        raise ValueError("text_type")
    if not _printable(s):
        raise ValueError("text_chars")
    s = unicodedata.normalize("NFC", s).strip()
    if not 1 <= len(s) <= max_len:
        raise ValueError("text_length")
    if not _printable(s):
        raise ValueError("text_chars")
    if not no_url(s):
        raise ValueError("text_url")
    return s


def bounded_int(v, lo, hi, name="value"):
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError("bad_" + name)
    return v
