"""Validation stricte des entrées (formulaire QR, jobs réseau, agents). Lève ValueError(<code court>)."""
import re
import unicodedata

# « control » : pilotage sémantique d'OpenTV (proposer un film à l'écran, naviguer, lancer un film accepté par le lecteur
# officiel promu, fermer le lecteur). Jamais accordé par défaut ni par une installation : choix explicite du
# propriétaire (« Gérer les assistants » sur la TV, ou `opentv-pi muse authorize-control --yes` en root).
# « canvas » : permission FACULTATIVE, hors des niveaux emboîtés (jamais dans AGENT_LEVELS, MUSE_SCOPES ni
# LINKED_REQUIRED) : proposer une ambiance OpenTV que le propriétaire confirme sur la TV (outils canvas_tools).
SCOPES = ("canvas", "control", "display", "feedback", "read")
OPTIONAL_SCOPES = ("canvas",)
AGENT_ID_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")
HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")
# toute forme d'adresse : schéma, www., hôte.tld, IP, schémas dangereux (plus strict que le moteur : un message
# affiché sur la TV n'a jamais besoin d'un nom de domaine)
URLISH_RE = re.compile(r"[a-z][a-z0-9+.-]*://|(?<![a-z0-9])www\.|(?<![a-z0-9])(?:javascript|data|vbscript|file|blob|magnet):"
                       r"|(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:[:/]|\b)(?!\s*[a-zà-ÿ])|[0-9]{1,3}(?:\.[0-9]{1,3}){3}", re.I)

# ---- texte libre d'un agent affiché sur la TV (bannière, phrase d'une fiche proposée) : durcissement anti-usurpation.
# Catégories Unicode refusées : contrôles, formats (marques/isolats/surcharges bidi, largeur nulle, BOM, tirets
# conditionnels, étiquettes), substituts, usage privé, non attribués, séparateurs de ligne/paragraphe (U+2028/2029).
MSG_BAD_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))
# espaces admis : ordinaire, insécable, fine insécable (typographie française « … » ; ; ?), fine
MSG_SPACES = frozenset("\u0020\u00a0\u202f\u2009")
MSG_MAX_MARKS = 3   # diacritiques combinants consécutifs au plus (texte « zalgo » qui déborde sur le titre)
# Écritures de droite à gauche refusées (classes bidi R, AL, AN) : sans caractère de contrôle, elles suffisent à
# réordonner visuellement la phrase et le nom de l'assistant autour d'elle. Alex lit OpenTV en français.
MSG_RTL_BIDI = frozenset(("R", "AL", "AN"))
# équivalents visuels de « . », « / », « : » que NFKC ne ramène pas (ou pas tous) à l'ASCII
# (points médians, katakana, puces, opérateurs, point arménien… : « evil·com » se lit comme une adresse ; « WALL·E »
# reste admis, aucun domaine n'a une extension d'une lettre)
_SKEL_MAP = {ord(c): "." for c in "\u3002\uff0e\uff61\ufe12\ufe52\u2024\u2e3c\u06d4\u0701\u0702\u1362\ua4f8\ua60e"
             "\u00b7\u0387\u30fb\uff65\u2022\u2027\u2219\u22c5\u0589\u2e31\u2e33\u16eb\u1427\u2981\ua78f"
             "\u02d9\u2e30\u05c5"}
_SKEL_MAP.update({ord(c): "/" for c in "\u2044\u2215\u29f8\u2571\uff0f"})
_SKEL_MAP.update({ord(c): ":" for c in "\ua789\u2236\ufe13\ufe55\uff1a\u02d0"})
# forme « squelette » : tout ce qui y ressemble à un nom d'hôte est refusé, même suivi d'un mot (« evil.com ici »)
STRICT_HOST_RE = re.compile(r"(?<![a-z0-9-])[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
                            r"\.[a-z]{2,24}(?![a-z0-9])")
# extensions courantes pour les points ÉPELÉS (« evil dot com », « evil (point) net ») : liste fermée, sans mot
# français ordinaire (« de », « ça »…), pour garder « un point de vue »
_SPELLED_TLDS = r"(?:com|net|org|io|fr|info|biz|xyz|ru|cn|onion|online|site|app|dev|uk|eu|gg|ly)"
_SPELLED_DOT_RE = re.compile(r"(?<=[a-z0-9])\s*(?:[(\[{<]\s*(?:\.|dot|point)\s*[)\]}>]|\b(?:dot|point)\b)\s*"
                             r"(?=" + _SPELLED_TLDS + r"(?![a-z0-9]))")
_BRACKET_DOT_RE = re.compile(r"\s*[(\[{<]\s*(?:\.|dot|point)\s*[)\]}>]\s*")
# point ASCII entouré d'espaces (« evil . com », « evil .com ») — jamais « Fin. Ça commence » (pas d'espace avant)
_SPACED_DOT_RE = re.compile(r"(?<=\w)\s+\.\s*(?=\w)")
# adresses que STRICT_HOST_RE ne voit pas : IPv4 courte/hexadécimale (« 127.1 », « 127.1:80 », « 10.0.1 », « 0x7f.1 » ;
# jamais une note « 7.8 » ni « 8.1/10 » : deux nombres seulement si le premier a trois chiffres), IPv6
# (« [::1] », « fe80::1 », 4 groupes et plus — jamais « 20:30:15 »), schémas faibles ou sans « // » (« http:/x »,
# « mailto : », « tel: », « //evil », « \\serveur »). Testés sur le squelette (minuscules, deux-points/points/barres
# exotiques déjà ramenés à l'ASCII).
ADDR_EXTRA_RE = re.compile(
    r"(?<![\w.])(?:0x[0-9a-f]{1,8}|[0-9]{1,3})(?:\.(?:0x[0-9a-f]{1,8}|[0-9]{1,3})){2,}(?![\w])"
    r"|(?<![\w.])[0-9]{1,3}\.[0-9]{1,8}\s*:\s*[0-9]{1,5}(?![0-9])"
    r"|(?<![\w.,])[1-9][0-9]{2}\.[0-9]{1,8}(?!\w|[.,][0-9])"
    r"|(?<![\w.])[0-9]{8,10}\s*:\s*[0-9]{1,5}(?![0-9])"
    r"|(?<![\w.])0x[0-9a-f]{1,8}(?![g-z])"
    r"|\[[0-9a-f.]*:[0-9a-f:.]*\]"
    r"|(?<![\w:])(?:[0-9a-f]{1,4}::(?:[0-9a-f]{1,4})?|::[0-9a-f]{1,4})(?![\w])"
    r"|(?<![\w:])[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){3,7}(?![\w:])"
    r"|(?<![a-z0-9])(?:https?|ftps?|wss?|sftp|ssh|smb|nfs|mailto|tel|sms|intent|market|itms(?:-[a-z]+)?"
    r"|view-source|about|chrome|ms-[a-z-]+|ipfs|ipns|irc|xmpp|tg|whatsapp|skype|facetime|javascript|data|vbscript"
    r"|file|blob|magnet)\s*:"
    r"|(?<![a-z0-9])[a-z][a-z0-9+.-]{1,30}:\s*[\\/]"
    r"|(?<![\w/:])//\s*[a-z0-9]|\\\\[a-z0-9]|(?<![a-z0-9])localhost(?![a-z0-9])")
# caractères invisibles ou « vides » de catégorie admise (lettres de remplissage hangûl, braille vide, sélecteurs
# de variation hors emoji, joncteur de graphèmes, voyelles khmères inhérentes, sélecteurs mongols)
MSG_INVISIBLE = frozenset("\u115f\u1160\u3164\uffa0\u2800\u034f\u17b4\u17b5\u180b\u180c\u180d\u180f")
_VS_EMOJI = frozenset("\ufe0e\ufe0f")   # présentation texte/emoji : seulement juste après un symbole ou # * 0-9


def _skeleton(s):
    """Forme de comparaison anti-usurpation (jamais affichée) : NFKC (pleine chasse, petites formes), points,
    barres et deux-points idéographiques ou exotiques -> ASCII, minuscules, accents retirés, autre lettre non latine
    -> x."""
    s = _SPACED_DOT_RE.sub(".", unicodedata.normalize("NFKC", s)).translate(_SKEL_MAP)
    s = unicodedata.normalize("NFKD", s.casefold())
    out = []
    for c in s:
        if unicodedata.category(c) == "Mn":
            continue
        if ord(c) > 127 and unicodedata.category(c).startswith("L"):
            out.append("x")
        else:
            out.append(c)
    s = "".join(out).translate(_SKEL_MAP)
    return _BRACKET_DOT_RE.sub(".", _SPELLED_DOT_RE.sub(".", s))


def message_chars_ok(s):
    """Caractères admis dans un texte libre d'agent affiché sur la TV (voir MSG_BAD_CATEGORIES et MSG_RTL_BIDI)."""
    marks, prev = 0, ""
    for c in s:
        cat = unicodedata.category(c)
        if cat in MSG_BAD_CATEGORIES or (cat == "Zs" and c not in MSG_SPACES) or c in MSG_INVISIBLE:
            return False
        if 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF:   # sélecteurs de variation
            if c not in _VS_EMOJI or not prev or not (unicodedata.category(prev) in ("So", "Sk", "Sm") or
                                                     prev in "#*0123456789"):
                return False
        prev = c
        if unicodedata.bidirectional(c) in MSG_RTL_BIDI:
            return False
        marks = marks + 1 if cat in ("Mn", "Me") else 0
        if marks > MSG_MAX_MARKS:
            return False
    return True


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
    """Texte libre d'agent affiché sur la TV (bannière, phrase d'une fiche proposée) : une ligne, 1 à 140 caractères,
    aucune adresse — y compris déguisée (pleine chasse, point idéographique, lettres non latines, « evil.com ici »),
    aucun contrôle de mise en forme (bidi, largeur nulle, U+2028/2029…), aucune écriture de droite à gauche. La page TV
    l'insère par textContent seulement ; ce filtre garde en plus l'affichage lisible et non trompeur."""
    if not isinstance(s, str):
        raise ValueError("text_type")
    if not _printable(s) or not message_chars_ok(s):
        raise ValueError("text_chars")
    s = unicodedata.normalize("NFC", s).strip()
    if not 1 <= len(s) <= max_len:
        raise ValueError("text_length")
    if not _printable(s) or not message_chars_ok(s):
        raise ValueError("text_chars")
    sk = _skeleton(s)
    if not no_url(s) or not no_url(sk) or STRICT_HOST_RE.search(sk) or ADDR_EXTRA_RE.search(sk):
        raise ValueError("text_url")
    return s


def bounded_int(v, lo, hi, name="value"):
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError("bad_" + name)
    return v
