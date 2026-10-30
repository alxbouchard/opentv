"""« Regarder » : porte de mise en production (fail-closed), distincte de l'approbation de la source.

`/etc/opentv/source.env` dit QUELLE source est autorisée ; il ne suffit jamais à montrer « Regarder ». La lecture
n'est disponible que si un témoin de promotion terrain existe aussi :

  /etc/opentv/playback-promoted   root:root 0644, jamais livré, jamais listé dans le manifeste, jamais écrit par
                                  install.sh ; écrit seulement par `sudo opentv-pi playback promote --field-validated`
                                  APRÈS la validation HDMI réelle (Regarder → lecture visible → Retour même carte),
                                  retiré par `opentv-pi playback demote` (qui ferme aussi toute séance en cours) et par
                                  uninstall.sh.

Le témoin est lié à la version ET à l'empreinte de la LIVRAISON ENTIÈRE (`code_digest`) : SHA-256 du manifeste installé
/opt/opentv/MANIFEST.sha256 (root:root, non inscriptible par un autre compte), après vérification que CHAQUE fichier
qu'il liste a toujours l'empreinte annoncée et que tous les fichiers dont dépend la lecture y figurent (modules de
lecture et du service, page /tv — tv.js/tv.html/tv.css/keys.js/canvas.js —, modules OpenTV Canvas, unités systemd,
politique Chromium du lecteur, lanceurs, résolveur NanoPi). Toute livraison (tv.js, unité, politique, module…) ou toute modification locale d'un
fichier livré referme donc la porte jusqu'à une nouvelle promotion terrain.

Propriété : root SEULEMENT (TRUSTED_UIDS), partout — service, contrôleur du lecteur et CLI. Fichier absent, lien,
FIFO, lien dur, propriétaire ou mode inattendu, JSON invalide/trop profond, version ou empreinte différente,
manifeste absent/peu sûr/incomplet ou fichier livré modifié : non promu.
"""
import hashlib
import json
import os
import re
import stat
import threading
import time

from . import VERSION

MARKER = "/etc/opentv/playback-promoted"
MANIFEST = "/opt/opentv/MANIFEST.sha256"
CAP = 4096
MANIFEST_CAP = 1 << 20
TRUSTED_UIDS = (0,)   # propriétaire admis du témoin et du manifeste : root seulement (les tests remplacent ce tuple)
# fichiers livrés dont dépend la promesse « lecteur seul, jamais la page du site, retour même carte » : tous doivent
# figurer dans le manifeste (suffixe de chemin), sinon la livraison est jugée incomplète
REQUIRED = ("opentv_pi/assist.py", "opentv_pi/cdp.py", "opentv_pi/evkeys.py", "opentv_pi/playback.py",
            "opentv_pi/playerclient.py", "opentv_pi/playerd.py", "opentv_pi/playerkeys.py", "opentv_pi/promotion.py",
            "opentv_pi/source.py", "opentv_pi/app.py", "opentv_pi/web.py", "opentv_pi/cli.py", "opentv_pi/config.py",
            "opentv_pi/__init__.py", "ui/tv.js", "ui/tv.html", "ui/tv.css", "ui/keys.js",
            "opentv-player.service", "opentv-player-keys.service", "opentv-kiosk.service", "opentv.service",
            "chromium-player-policy/opentv-player.json", "bin/opentv-player", "bin/opentv-player-keys",
            "opentv_bridge.py",
            # OpenTV Canvas : canvas.js est chargé par tv.html et fige/coupe l'ambiance pendant le chargement, la lecture
            # et le retour ; ses modules (routes TV, occupation, aperçu d'agent, magasin) partagent les portes de
            # l'interface avec « Regarder » — une livraison Canvas incomplète n'est jamais promue.
            "ui/canvas.js", "opentv_pi/canvas.py", "opentv_pi/canvas_schema.py", "opentv_pi/canvas_storage.py",
            "opentv_pi/canvas_activity.py", "opentv_pi/canvas_roles.py", "opentv_pi/canvas_tv.py",
            "opentv_pi/canvas_tools.py")
PLAYBACK_MODULES = tuple(r.split("/", 1)[1] for r in REQUIRED if r.startswith("opentv_pi/"))
STATUSES = ("promoted", "missing", "unsafe", "invalid", "stale")
LINE_RE = re.compile(r"^([0-9a-f]{64})  (/[^\0\n]{1,1024})$")

_cache_lock = threading.Lock()
_cache = {}   # chemin -> (signature stat, sha256) : un fichier inchangé n'est pas relu à chaque sondage


def _trusted(uid_ok):
    return tuple(TRUSTED_UIDS if uid_ok is None else uid_ok)


def _open_checked(path, uid_ok, cap):
    """Lecture sans suivre de lien : régulier, nlink 1, propriétaire admis, non inscriptible groupe/autres, borné."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unsafe"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in uid_ok or st.st_mode & 0o022 \
                or st.st_size > cap:
            return None, "unsafe"
        data = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(data) > cap:
        return None, "unsafe"
    return data, None


def _sha_file(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    sig = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return None
    d = h.hexdigest()
    with _cache_lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[path] = (sig, d)
    return d


def code_digest(manifest=MANIFEST, uid_ok=None, root="/", verify_files=True):
    """Empreinte de la livraison installée : SHA-256 du manifeste, seulement si le manifeste est sûr, liste tous les
    fichiers REQUIRED et si chaque fichier listé a toujours l'empreinte annoncée. Sinon None (=> « stale »)."""
    data, err = _open_checked(manifest, _trusted(uid_ok), MANIFEST_CAP)
    if err or not data:
        return None
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    entries = []
    for line in lines:
        if not line.strip():
            continue
        m = LINE_RE.fullmatch(line.rstrip("\r"))
        if not m:
            return None
        entries.append((m.group(1), m.group(2)))
    if not entries:
        return None
    for req in REQUIRED:
        if not any(p.endswith("/" + req) for _, p in entries):
            return None
    if verify_files:
        for want, p in entries:
            if _sha_file(os.path.join(root, p.lstrip("/"))) != want:
                return None
    return hashlib.sha256(b"opentv-delivery\0" + data).hexdigest()


def check(path=MARKER, uid_ok=None, manifest=MANIFEST, version=VERSION, root="/", verify_files=True):
    """-> statut ∈ STATUSES ; seul « promoted » ouvre la lecture."""
    data, err = _open_checked(path, _trusted(uid_ok), CAP)
    if err or data is None:
        return err or "missing"
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return "invalid"
    if not isinstance(doc, dict) or doc.get("promoted") is not True or doc.get("field_validated") is not True:
        return "invalid"
    want = code_digest(manifest, uid_ok, root, verify_files=verify_files)
    if want is None or doc.get("version") != version or doc.get("code") != want:
        return "stale"
    return "promoted"


def promoted(path=MARKER, uid_ok=None, manifest=MANIFEST, verify_files=True):
    return check(path, uid_ok, manifest, verify_files=verify_files) == "promoted"


def document(manifest=MANIFEST, now=None, version=VERSION, uid_ok=None, root="/"):
    code = code_digest(manifest, uid_ok, root)
    if code is None:
        raise ValueError("delivery_unverified")
    return {"promoted": True, "field_validated": True, "version": version, "code": code,
            "at": int(time.time() if now is None else now)}


def write(path=MARKER, manifest=MANIFEST, uid=None, gid=None, uid_ok=None):
    """Écriture atomique (temporaire exclusif du même dossier, fsync, rename) ; root:root 0644 en production.
    Refusée (ValueError) si la livraison installée n'est pas vérifiable."""
    from .fsutil import atomic_write_path
    data = json.dumps(document(manifest, uid_ok=uid_ok), sort_keys=True) + "\n"
    atomic_write_path(path, data, mode=0o644, uid=uid, gid=gid)
    return path


def remove(path=MARKER):
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
