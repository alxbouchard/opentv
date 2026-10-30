"""OpenTV Canvas : scène d'ambiance déclarative (schéma fermé) et magasin persistant fail-closed.

Une scène est une DONNÉE validée, jamais du code : schéma v1 = `wallpaper`, `motion`, `accent`, `shade`,
`hero_focus` (énumérations fermées) et `row_order` (permutation des rangées connues, « tonight » en premier). Aucune
URL, CSS, HTML, chemin ni ressource arbitraire. Préréglages intégrés : `mist`, `cinema`, `canvas` (défaut immuable).

Cycle de vie (séparation stricte des rôles) :
- agent (`agent_api`, `agent_preview`, `agent_cancel`) : propose un aperçu EN MÉMOIRE seulement (identifiant
  hexadécimal minuscule de 32 caractères, échéance sur horloge monotone) et peut retirer SON aperçu. Aucune méthode
  côté agent ne confirme, n'applique, ne revient en arrière ni ne réinitialise.
- propriétaire (`owner_api`, `owner_confirm|reject|apply|rollback|reset`) : appelé seulement par la feuille de
  confirmation / les Réglages de la TV ; exige `confirm=True` littéral pour toute écriture.
- interface : `human_event` (touche, télécommande téléphone…) annule l'aperçu vivant et ouvre une fenêtre de silence
  monotone ; `busy_enter(raison)` -> jeton / `busy_exit(jeton)` représentent des raisons d'occupation IMBRIQUÉES
  (panneaux, lecture, configuration…) : chaque entrée annule l'aperçu ; la confirmation d'un aperçu agent est refusée
  tant qu'une raison quelconque ou la fenêtre de silence est active.

Persistance : `<dossier>/state.json` (défaut /var/lib/opentv/canvas, dossier 0700 et fichier 0600 du compte du
service) = document canonique auto-haché {schema_version, epoch, revision, history (5 dernières publications),
state_hash}. Écriture sous flock borné du fichier latéral `state.json.lock`, relecture forcée sous ce verrou,
temporaire exclusif du même dossier (fchown/fchmod sur le descripteur), fsync, rename relatif au dossier, fsync du
dossier, relecture octet pour octet ; échec ⇒ restauration des octets précédents. Un aperçu porte la base
(epoch, revision) : toute écriture étrangère (autre processus, CLI root, réinitialisation) le rend périmé.

Fail-closed : lien symbolique, FIFO, dossier, lien dur, taille excessive, autre propriétaire, mode inattendu, JSON
invalide (y compris RecursionError/MemoryError du décodeur), hachage ou forme incorrects ⇒ état « corrupt » : la TV
montre le défaut immuable, aucun aperçu ni écriture, octets intacts ; seule `owner_reset` remplace le fichier.

Bibliothèque standard seulement ; horloges murale et monotone injectables pour les tests.
"""
import contextlib
import dataclasses
import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time

from . import agents, fsutil, validate

SCHEMA_VERSION = 1
DEFAULT_DIR = "/var/lib/opentv/canvas"
DIR_NAME = "canvas"
STATE_NAME = "state.json"
LOCK_NAME = STATE_NAME + ".lock"
MAX_HISTORY = 5
MAX_STATE_BYTES = 64 * 1024
LOCK_TIMEOUT_S = 2.0
NAV_QUIET_S = 1.5
MAX_BUSY_TOKENS = 64
MAX_AT = 253402300799                 # 9999-12-31T23:59:59Z, entier seulement
MAX_REVISION = 2 ** 53 - 1            # entier sûr partagé avec le JavaScript

FIELDS = ("wallpaper", "motion", "accent", "shade", "hero_focus", "row_order")
ENUMS = {
    "wallpaper": ("brume", "affiche", "aurore"),
    "motion": ("off", "calm", "alive"),
    "accent": ("ice", "ember", "jade", "violet"),
    "shade": ("soft", "balanced", "deep"),
    "hero_focus": ("title", "artwork", "calm"),
}
ROW_IDS = ("tonight", "watchlist", "new", "top", "more")   # rangées de « Pour toi »
FIRST_ROW = "tonight"                 # « Le choix de ce soir » reste toujours la première rangée
SCENE_KEYS = ("schema_version",) + FIELDS
CHANGE_KEYS = ("preset",) + FIELDS
# raisons d'occupation (imbriquables) : jamais d'aperçu ni de confirmation pendant l'une d'elles
BUSY_REASONS = ("panel", "setup", "assistants", "muse_setup", "playback", "loading", "closing", "returning",
                "offline", "hidden")
HUMAN_EVENTS = ("key", "remote", "pointer")
STATES = ("default", "applied", "corrupt")
CORRUPT_REASONS = ("unsafe_dir", "unsafe_file", "wrong_owner", "wrong_mode", "oversize", "unreadable",
                   "invalid_json", "invalid_shape", "invalid_scene", "hash_mismatch")
CODES = frozenset(("invalid_agent", "invalid_scene", "invalid_event", "no_change", "ui_busy", "busy_overflow",
                   "preview_pending", "unknown_preview", "preview_expired", "stale_preview", "confirmation_required",
                   "store_corrupt", "store_busy", "lock_unsafe", "unsafe_dir", "write_failed", "readback_failed",
                   "nothing_to_rollback", "revision_exhausted"))
DOC_KEYS = ("epoch", "history", "revision", "schema_version", "state_hash")
ENTRY_KEYS = ("agent", "at", "hash", "revision", "scene")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_TMP_RE = re.compile(r"\." + re.escape(STATE_NAME) + r"\.[0-9a-f]{12}\.tmp")


class SceneError(ValueError):
    def __init__(self, code, key=None):
        super().__init__("%s%s" % (code, (": " + key) if isinstance(key, str) and key in SCENE_KEYS + CHANGE_KEYS
                                    else ""))
        self.code = code
        self.key = key if isinstance(key, str) and key in SCENE_KEYS + CHANGE_KEYS else None


class CanvasError(RuntimeError):
    """Refus typé : `code` (liste fermée CODES) + `detail` facultatif (code court ; jamais de chemin)."""

    def __init__(self, code, detail=None):
        assert code in CODES, code
        self.code = code
        self.detail = detail if isinstance(detail, str) and re.fullmatch(r"[a-z_]{1,32}", detail) else None
        super().__init__(code + ((": " + self.detail) if self.detail else ""))


class _Corrupt(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- scène
@dataclasses.dataclass(frozen=True)
class Scene:
    schema_version: int
    wallpaper: str
    motion: str
    accent: str
    shade: str
    hero_focus: str
    row_order: tuple

    def to_dict(self):
        d = dataclasses.asdict(self)
        d["row_order"] = list(self.row_order)
        return d


def validate_scene(d):
    """Scène complète -> Scene gelée ; toute divergence (clé, type exact, valeur hors liste) lève SceneError."""
    if type(d) is not dict:
        raise SceneError("invalid_shape")
    for k in d:
        if type(k) is not str or k not in SCENE_KEYS:
            raise SceneError("unknown_key", k)
    for k in SCENE_KEYS:
        if k not in d:
            raise SceneError("missing_key", k)
    if type(d["schema_version"]) is not int or d["schema_version"] != SCHEMA_VERSION:
        raise SceneError("invalid_value", "schema_version")
    vals = {}
    for k, allowed in ENUMS.items():
        v = d[k]
        if type(v) is not str or v not in allowed:
            raise SceneError("invalid_value", k)
        vals[k] = v
    rows = d["row_order"]
    if type(rows) not in (list, tuple) or len(rows) != len(ROW_IDS):
        raise SceneError("invalid_value", "row_order")
    for r in rows:
        if type(r) is not str or r not in ROW_IDS:
            raise SceneError("invalid_value", "row_order")
    if len(set(rows)) != len(ROW_IDS) or rows[0] != FIRST_ROW:
        raise SceneError("invalid_value", "row_order")
    return Scene(schema_version=SCHEMA_VERSION, row_order=tuple(rows), **vals)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(obj):
    return hashlib.sha256(_canon(obj).encode("ascii")).hexdigest()


def canonical_json(scene):
    return _canon(scene.to_dict())


def scene_hash(scene):
    return _digest(scene.to_dict())


_ROWS = ("tonight", "watchlist", "new", "top", "more")
PRESETS = {
    "mist": Scene(SCHEMA_VERSION, "brume", "calm", "ice", "balanced", "title", _ROWS),
    "cinema": Scene(SCHEMA_VERSION, "affiche", "calm", "ember", "deep", "artwork",
                    ("tonight", "top", "new", "watchlist", "more")),
    "canvas": Scene(SCHEMA_VERSION, "aurore", "calm", "ice", "balanced", "title", _ROWS),
}
DEFAULT_PRESET = "canvas"
DEFAULT_SCENE = PRESETS[DEFAULT_PRESET]


def preset_of(scene):
    """Identifiant du préréglage identique à `scene`, sinon None (scène personnalisée)."""
    for name, sc in PRESETS.items():
        if sc == scene:
            return name
    return None


def apply_changes(base, changes):
    """Changements plats {preset?, champ: valeur…} appliqués à `base` -> Scene validée ; SceneError sinon."""
    if type(changes) is not dict or not changes:
        raise SceneError("invalid_changes")
    for k in changes:
        if type(k) is not str or k not in CHANGE_KEYS:
            raise SceneError("unknown_key", k)
    if "preset" in changes:
        p = changes["preset"]
        if type(p) is not str or p not in PRESETS:
            raise SceneError("invalid_value", "preset")
        base = PRESETS[p]
    d = base.to_dict()
    d.update({k: v for k, v in changes.items() if k != "preset"})
    return validate_scene(d)


def _is_hex(value, rx):
    """Chaîne hexadécimale minuscule de longueur exacte, vérifiée AVANT hmac.compare_digest (non-ASCII ⇒ TypeError)."""
    return type(value) is str and rx.fullmatch(value) is not None


def _valid_agent(agent):
    if type(agent) is not str:
        return False
    try:
        validate.agent_id(agent)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- document persistant
def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate_key")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError("non_finite")


def _decode(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_no_constant)


def _parse(data):
    """Octets -> (epoch, revision, historique). Toute anomalie ⇒ _Corrupt (jamais d'exception brute : RecursionError,
    MemoryError, TypeError, OverflowError… compris)."""
    try:
        doc = _decode(data)
    except Exception:
        raise _Corrupt("invalid_json")
    try:
        return _parse_doc(doc)
    except _Corrupt:
        raise
    except SceneError:
        raise _Corrupt("invalid_scene")
    except Exception:
        raise _Corrupt("invalid_shape")


def _parse_doc(doc):
    if type(doc) is not dict or tuple(sorted(doc)) != DOC_KEYS:
        raise _Corrupt("invalid_shape")
    if not _is_hex(doc["state_hash"], _HEX64):
        raise _Corrupt("invalid_shape")
    body = {k: v for k, v in doc.items() if k != "state_hash"}
    if not hmac.compare_digest(doc["state_hash"], _digest(body)):
        raise _Corrupt("hash_mismatch")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != SCHEMA_VERSION:
        raise _Corrupt("invalid_shape")
    if not _is_hex(doc["epoch"], _HEX32):
        raise _Corrupt("invalid_shape")
    rev, hist = doc["revision"], doc["history"]
    if type(rev) is not int or not 0 <= rev <= MAX_REVISION or type(hist) is not list or len(hist) > MAX_HISTORY:
        raise _Corrupt("invalid_shape")
    out, last = [], 0
    for e in hist:
        if type(e) is not dict or tuple(sorted(e)) != ENTRY_KEYS:
            raise _Corrupt("invalid_shape")
        r, at, agent = e["revision"], e["at"], e["agent"]
        if type(r) is not int or not last < r <= rev:
            raise _Corrupt("invalid_shape")
        if type(at) is not int or not 0 <= at <= MAX_AT:
            raise _Corrupt("invalid_shape")
        if agent is not None and not _valid_agent(agent):
            raise _Corrupt("invalid_shape")
        if not _is_hex(e["hash"], _HEX64):
            raise _Corrupt("invalid_shape")
        sc = validate_scene(e["scene"])
        if not hmac.compare_digest(e["hash"], scene_hash(sc)):
            raise _Corrupt("hash_mismatch")
        out.append({"revision": r, "hash": e["hash"], "scene": sc, "at": at, "agent": agent})
        last = r
    return doc["epoch"], rev, out


def _render(epoch, revision, history):
    body = {"schema_version": SCHEMA_VERSION, "epoch": epoch, "revision": revision,
            "history": [{"revision": e["revision"], "hash": e["hash"], "scene": e["scene"].to_dict(),
                         "at": e["at"], "agent": e["agent"]} for e in history]}
    body["state_hash"] = _digest(body)
    return _canon(body).encode("ascii")


# --------------------------------------------------------------------------- E/S relatives au dossier
def _read_file(dfd, owner):
    """-> octets de STATE_NAME, None s'il est absent ; _Corrupt pour tout fichier dangereux ou inattendu."""
    try:
        fd = os.open(STATE_NAME, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _Corrupt("unsafe_file" if e.errno in (errno.ELOOP, errno.EMLINK, errno.EISDIR, errno.ENXIO,
                                                     errno.ENOTDIR) else "unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _Corrupt("unsafe_file")
        if st.st_uid != owner:
            raise _Corrupt("wrong_owner")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise _Corrupt("wrong_mode")
        if st.st_size > MAX_STATE_BYTES:
            raise _Corrupt("oversize")
        chunks, left = [], MAX_STATE_BYTES + 1
        while left > 0:
            b = os.read(fd, min(left, 65536))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > MAX_STATE_BYTES:
            raise _Corrupt("oversize")
        return data
    except OSError:
        raise _Corrupt("unreadable")
    finally:
        os.close(fd)


def _readback(dfd, owner):
    """Relecture après écriture (point d'injection des tests) : None si illisible."""
    try:
        return _read_file(dfd, owner)
    except _Corrupt:
        return None


def _unlink_quiet(name, dfd):
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


# --------------------------------------------------------------------------- façades par rôle
class AgentCanvas:
    """Vue d'un agent authentifié : lecture, aperçu, retrait de SON aperçu. Rien d'autre."""
    __slots__ = ("_s", "_agent")

    def __init__(self, store, agent):
        self._s, self._agent = store, agent

    def status(self):
        return self._s._agent_status(self._agent)

    def preview(self, changes):
        return self._s.agent_preview(self._agent, changes)

    def cancel(self, preview_id):
        return self._s.agent_cancel(self._agent, preview_id)


class OwnerCanvas:
    """Vue du propriétaire (feuille de confirmation et Réglages de la TV, derrière la clé TV)."""
    __slots__ = ("_s",)

    def __init__(self, store):
        self._s = store

    def status(self):
        return self._s.status()

    def confirm(self, preview_id, confirm=False):
        return self._s.owner_confirm(preview_id, confirm=confirm)

    def reject(self, preview_id):
        return self._s.owner_reject(preview_id)

    def apply(self, changes, confirm=False):
        return self._s.owner_apply(changes, confirm=confirm)

    def rollback(self, confirm=False):
        return self._s.owner_rollback(confirm=confirm)

    def reset(self, confirm=False):
        return self._s.owner_reset(confirm=confirm)


# --------------------------------------------------------------------------- magasin
class CanvasStore:
    """Scène publiée + historique (disque) ; aperçu et raisons d'occupation (mémoire du processus).

    Ordre des verrous : flock inter-processus D'ABORD (attente bornée, sans tenir le verrou interne), puis verrou
    interne `_mu` le temps de la relecture, des revérifications et de l'écriture. Un événement humain ou une entrée
    occupée arrivant pendant l'attente du flock passe donc toujours avant la confirmation."""

    def __init__(self, path=DEFAULT_DIR, owner_uid=None, wall=time.time, mono=time.monotonic, preview_ttl_s=30,
                 lock_timeout=LOCK_TIMEOUT_S):
        if type(preview_ttl_s) not in (int, float) or not math.isfinite(preview_ttl_s) or \
                not 5 <= preview_ttl_s <= 120:
            raise ValueError("preview_ttl_s hors bornes (5..120 s)")
        if type(lock_timeout) not in (int, float) or not math.isfinite(lock_timeout) or \
                not 0 < lock_timeout <= 30:
            raise ValueError("lock_timeout hors bornes (0..30 s)")
        euid = os.geteuid()
        if owner_uid is None:
            if euid == 0:
                raise ValueError("root : compte propriétaire (service) explicite requis")
            owner_uid = euid
        if type(owner_uid) is not int or owner_uid <= 0:
            raise ValueError("owner_uid invalide (jamais root)")
        if euid not in (0, owner_uid):
            raise ValueError("owner_uid différent du compte courant")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("chemin Canvas absolu requis")
        self._parent, self._name = os.path.split(os.path.normpath(path))
        fsutil._check_name(self._name)
        self._owner, self._root = owner_uid, euid == 0
        self._wall, self._mono, self._ttl, self._lock_timeout = wall, mono, preview_ttl_s, lock_timeout
        self._mu = threading.RLock()
        self._epoch, self._revision, self._history, self._corrupt, self._raw = None, 0, [], None, None
        self._preview = None
        self._busy = {}                   # jeton -> raison (imbrication sûre, jamais un état unique écrasé)
        self._overflow = False
        self._last_human = None
        self._after_lock = None           # point d'injection des tests (après le flock, avant la relecture)
        self._dir_gid = -1
        with self._mu:
            self._load()

    # ------------------------------------------------------------------ façades
    def agent_api(self, agent):
        if not _valid_agent(agent):
            raise CanvasError("invalid_agent")
        return AgentCanvas(self, agent)

    def owner_api(self):
        return OwnerCanvas(self)

    # ------------------------------------------------------------------ dossier
    def _open_dir(self, create):
        """-> (descripteur du dossier Canvas, gid) ou None s'il est absent (create=False). Parent : dossier non
        inscriptible par le groupe/les autres, propriété du service ou de root ; dossier Canvas : jamais un lien,
        propriété du service, mode exact 0700. Créé (mkdir relatif au parent, fchown/fchmod sur le descripteur) au
        premier enregistrement seulement. CanvasError(unsafe_dir) sinon."""
        try:
            pfd = fsutil.open_dir(self._parent)
        except OSError:
            raise CanvasError("unsafe_dir")
        try:
            pst = os.fstat(pfd)
            if not stat.S_ISDIR(pst.st_mode) or pst.st_mode & 0o022 or pst.st_uid not in (self._owner, 0):
                raise CanvasError("unsafe_dir")
            flags = os.O_RDONLY | fsutil.O_DIRECTORY | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC
            created = False
            try:
                fd = os.open(self._name, flags, dir_fd=pfd)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(self._name, 0o700, dir_fd=pfd)
                    created = True
                except FileExistsError:
                    pass
                except OSError:
                    raise CanvasError("unsafe_dir")
                try:
                    fd = os.open(self._name, flags, dir_fd=pfd)
                except OSError:
                    raise CanvasError("unsafe_dir")
            except OSError:   # ELOOP (lien), ENOTDIR (fichier), EACCES…
                raise CanvasError("unsafe_dir")
            try:
                st = os.fstat(fd)
                if created and self._root and st.st_uid != self._owner:
                    os.fchown(fd, self._owner, pst.st_gid)
                if created and stat.S_IMODE(st.st_mode) != 0o700:
                    os.fchmod(fd, 0o700)
                st = os.fstat(fd)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != self._owner or stat.S_IMODE(st.st_mode) != 0o700:
                    raise CanvasError("unsafe_dir")
                return fd, st.st_gid
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(pfd)

    def _open_lock(self, dfd):
        try:
            fd, created = agents._open_lock_fd(dfd, LOCK_NAME)
        except agents.RegistryBusy:
            raise CanvasError("store_busy")
        except OSError:
            raise CanvasError("lock_unsafe")
        try:
            st = os.fstat(fd)
            if created and self._root and st.st_uid != self._owner:
                os.fchown(fd, self._owner, -1)
                st = os.fstat(fd)
            else:
                for _ in range(agents.LOCK_OWNER_SETTLE_TRIES):   # root entre sa création et son fchown
                    if st.st_uid == self._owner:
                        break
                    time.sleep(0.005)
                    st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != self._owner:
                raise CanvasError("lock_unsafe")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextlib.contextmanager
    def _locked(self, create):
        """Dossier + flock borné du verrou latéral ; nettoie les temporaires orphelins. -> dfd (None : absent)."""
        opened = self._open_dir(create)
        if opened is None:
            yield None
            return
        dfd, gid = opened
        try:
            lfd = self._open_lock(dfd)
            try:
                try:
                    agents.flock_bounded(lfd, self._lock_timeout)
                except agents.RegistryBusy:
                    raise CanvasError("store_busy")
                self._dir_gid = gid
                self._cleanup(dfd)
                hook = self._after_lock
                if hook is not None:
                    hook()
                yield dfd
            finally:
                os.close(lfd)
        finally:
            os.close(dfd)

    @staticmethod
    def _cleanup(dfd):
        try:
            names = os.listdir(dfd)
        except OSError:
            return
        for n in names:
            if _TMP_RE.fullmatch(n):
                _unlink_quiet(n, dfd)

    # ------------------------------------------------------------------ état
    def _load(self, dfd=None):
        """Relit le disque (sous _mu). N'abandonne PAS l'aperçu : voir _sync_preview."""
        try:
            if dfd is None:
                try:
                    opened = self._open_dir(False)
                except CanvasError:
                    raise _Corrupt("unsafe_dir")
                if opened is None:
                    data = None
                else:
                    try:
                        data = _read_file(opened[0], self._owner)
                    finally:
                        os.close(opened[0])
            else:
                data = _read_file(dfd, self._owner)
            if data is None:
                self._epoch, self._revision, self._history = None, 0, []
            else:
                self._epoch, self._revision, self._history = _parse(data)
            self._corrupt, self._raw = None, data
        except _Corrupt as e:
            self._epoch, self._revision, self._history, self._corrupt, self._raw = None, 0, [], e.reason, None
        except Exception:
            self._epoch, self._revision, self._history, self._corrupt, self._raw = None, 0, [], "unreadable", None

    def _base(self):
        return (self._epoch, self._revision)

    def _sync_preview(self):
        p = self._preview
        if p is None:
            return None
        if self._corrupt or p["base"] != self._base() or self._mono() >= p["expires"]:
            self._preview = None
            return None
        return p

    def _refresh(self):
        self._load()
        return self._sync_preview()

    def _current(self):
        return self._history[-1]["scene"] if self._history else DEFAULT_SCENE

    def current_scene(self):
        with self._mu:
            self._refresh()
            return self._current()

    def visible_scene(self):
        """Ce que la TV montre : l'aperçu vivant, sinon la scène publiée (défaut si absent ou corrompu)."""
        with self._mu:
            p = self._refresh()
            return p["scene"] if p else self._current()

    def _state(self):
        return "corrupt" if self._corrupt else ("applied" if self._history else "default")

    def _preview_view(self, p):
        return {"preview_id": p["id"], "agent": p["agent"], "proposed_by": agents.canonical_agent(p["agent"]),
                "hash": p["hash"], "scene": p["scene"].to_dict(), "preset": preset_of(p["scene"]),
                "base_revision": p["base"][1], "expires_in_s": round(max(0.0, p["expires"] - self._mono()), 3)}

    def status(self):
        """Vue propriétaire/TV (sans chemin) : état, scène publiée, historique, aperçu, raisons d'occupation."""
        with self._mu:
            p = self._refresh()
            cur = self._current()
            return {
                "state": self._state(), "reason": self._corrupt, "revision": self._revision,
                "hash": scene_hash(cur), "scene": cur.to_dict(), "preset": preset_of(cur),
                "history": [{"revision": e["revision"], "hash": e["hash"], "at": e["at"],
                             "preset": preset_of(e["scene"]), "agent": e["agent"]} for e in self._history],
                "preview": self._preview_view(p) if p else None,
                "busy": list(self._busy_reasons()),
            }

    def _agent_status(self, agent):
        with self._mu:
            p = self._refresh()
            cur = self._current()
            return {"state": self._state(), "revision": self._revision, "hash": scene_hash(cur),
                    "scene": cur.to_dict(), "preset": preset_of(cur),
                    "preview": self._preview_view(p) if p and p["agent"] == agent else None}

    # ------------------------------------------------------------------ occupation et humain
    def _busy_reasons(self):
        out = set(self._busy.values())
        if self._overflow:
            out.add("overflow")
        if self._last_human is not None and self._mono() - self._last_human < NAV_QUIET_S:
            out.add("navigating")
        return tuple(sorted(out))

    def busy_reasons(self):
        with self._mu:
            return self._busy_reasons()

    def _cancel_live(self):
        live = self._preview is not None and self._mono() < self._preview["expires"]
        self._preview = None
        return live

    def human_event(self, kind):
        """Touche/télécommande humaine : annule l'aperçu vivant AVANT d'agir et ouvre la fenêtre de silence."""
        if type(kind) is not str or kind not in HUMAN_EVENTS:
            raise CanvasError("invalid_event")
        with self._mu:
            cancelled = self._cancel_live()
            self._last_human = self._mono()
            return {"preview_cancelled": cancelled, "busy": list(self._busy_reasons())}

    def busy_enter(self, reason):
        """Entrée dans une raison d'occupation -> jeton (32 hex) à rendre à busy_exit. Annule l'aperçu vivant."""
        if type(reason) is not str or reason not in BUSY_REASONS:
            raise CanvasError("invalid_event")
        with self._mu:
            self._cancel_live()
            if len(self._busy) >= MAX_BUSY_TOKENS:
                self._overflow = True            # reste occupé jusqu'à busy_reset explicite
                raise CanvasError("busy_overflow")
            tok = secrets.token_hex(16)
            self._busy[tok] = reason
            return tok

    def busy_exit(self, token):
        """Sortie d'UNE entrée ; jeton inconnu/déjà rendu ⇒ False, sans effet sur les autres."""
        if not _is_hex(token, _HEX32):
            return False
        with self._mu:
            return self._busy.pop(token, None) is not None

    def busy_reset(self):
        """Remise à zéro explicite (nouvelle page TV) : oublie toutes les entrées et le dépassement."""
        with self._mu:
            self._busy.clear()
            self._overflow = False

    @contextlib.contextmanager
    def busy_scope(self, reason):
        tok = self.busy_enter(reason)
        try:
            yield tok
        finally:
            self.busy_exit(tok)

    # ------------------------------------------------------------------ agent
    def agent_preview(self, agent, changes):
        """Aperçu en mémoire proposé par `agent` (identité authentifiée). N'écrit jamais rien."""
        if not _valid_agent(agent):
            raise CanvasError("invalid_agent")
        with self._mu:
            busy = self._busy_reasons()
            if busy:
                raise CanvasError("ui_busy", busy[0])
            live = self._refresh()
            if self._corrupt:
                raise CanvasError("store_corrupt", self._corrupt)
            if live is not None:
                raise CanvasError("preview_pending")
            try:
                sc = apply_changes(self._current(), changes)
            except SceneError:
                raise CanvasError("invalid_scene")
            if sc == self._current():
                raise CanvasError("no_change")
            p = {"id": secrets.token_hex(16), "agent": agent, "scene": sc, "hash": scene_hash(sc),
                 "base": self._base(), "expires": self._mono() + self._ttl}
            self._preview = p
            return {"preview_id": p["id"], "hash": p["hash"], "base_revision": self._revision,
                    "expires_in_s": self._ttl, "scene": sc.to_dict(), "preset": preset_of(sc)}

    def agent_cancel(self, agent, preview_id):
        """Retire l'aperçu vivant de CET agent. -> True ; CanvasError(unknown_preview) sinon."""
        if not _valid_agent(agent):
            raise CanvasError("invalid_agent")
        with self._mu:
            p = self._sync_preview()
            if p is None or p["agent"] != agent or not _is_hex(preview_id, _HEX32) or \
                    not hmac.compare_digest(preview_id, p["id"]):
                raise CanvasError("unknown_preview")
            self._preview = None
            return True

    # ------------------------------------------------------------------ propriétaire
    @staticmethod
    def _check_confirm(confirm):
        if confirm is not True:
            raise CanvasError("confirmation_required")

    def _match(self, preview_id):
        p = self._preview
        if p is None or not _is_hex(preview_id, _HEX32) or not hmac.compare_digest(preview_id, p["id"]):
            raise CanvasError("unknown_preview")
        return p

    def owner_reject(self, preview_id):
        """« Non » sur la feuille de confirmation. -> True si un aperçu a été retiré."""
        with self._mu:
            try:
                self._match(preview_id)
            except CanvasError:
                return False
            self._preview = None
            return True

    def owner_confirm(self, preview_id, confirm=False):
        """« Oui » du propriétaire sur la feuille de la TV : publie l'aperçu agent. Refusé si une raison d'occupation
        ou la fenêtre de silence est active ; identité, échéance et base revérifiées APRÈS flock + relecture."""
        self._check_confirm(confirm)
        with self._mu:
            busy = self._busy_reasons()
            if busy:
                raise CanvasError("ui_busy", busy[0])
            p = self._match(preview_id)
            if self._mono() >= p["expires"]:
                self._preview = None
                raise CanvasError("preview_expired")
        with self._locked(True) as dfd:
            with self._mu:
                self._load(dfd)
                if self._corrupt:
                    self._preview = None
                    raise CanvasError("store_corrupt", self._corrupt)
                if self._preview is not p:              # annulé pendant l'attente du verrou
                    raise CanvasError("unknown_preview")
                busy = self._busy_reasons()
                if busy:
                    raise CanvasError("ui_busy", busy[0])
                if self._mono() >= p["expires"]:
                    self._preview = None
                    raise CanvasError("preview_expired")
                if p["base"] != self._base():
                    self._preview = None
                    raise CanvasError("stale_preview")
                rev = self._next_revision()
                entry = {"revision": rev, "hash": p["hash"], "scene": p["scene"], "at": self._at(),
                         "agent": p["agent"]}
                self._commit(dfd, self._epoch or secrets.token_hex(16), rev,
                             (self._history + [entry])[-MAX_HISTORY:])
                self._preview = None
                return {"revision": rev, "hash": p["hash"], "preset": preset_of(p["scene"])}

    def owner_apply(self, changes, confirm=False):
        """Réglages « Ambiance OpenTV » : publication directe par le propriétaire (annule tout aperçu agent)."""
        self._check_confirm(confirm)
        with self._locked(True) as dfd:
            with self._mu:
                self._load(dfd)
                self._preview = None
                if self._corrupt:
                    raise CanvasError("store_corrupt", self._corrupt)
                try:
                    sc = apply_changes(self._current(), changes)
                except SceneError:
                    raise CanvasError("invalid_scene")
                if sc == self._current():
                    raise CanvasError("no_change")
                rev = self._next_revision()
                h = scene_hash(sc)
                entry = {"revision": rev, "hash": h, "scene": sc, "at": self._at(), "agent": None}
                self._commit(dfd, self._epoch or secrets.token_hex(16), rev,
                             (self._history + [entry])[-MAX_HISTORY:])
                return {"revision": rev, "hash": h, "preset": preset_of(sc)}

    def owner_rollback(self, confirm=False):
        """Annule exactement la dernière publication (jusqu'à cinq fois), puis le défaut immuable."""
        self._check_confirm(confirm)
        with self._locked(False) as dfd:
            with self._mu:
                if dfd is None:
                    self._load()
                    raise CanvasError("store_corrupt", self._corrupt) if self._corrupt else \
                        CanvasError("nothing_to_rollback")
                self._load(dfd)
                self._preview = None
                if self._corrupt:
                    raise CanvasError("store_corrupt", self._corrupt)
                if not self._history:
                    raise CanvasError("nothing_to_rollback")
                rev = self._next_revision()
                hist = self._history[:-1]
                self._commit(dfd, self._epoch or secrets.token_hex(16), rev, hist)
                cur = self._current()
                return {"revision": rev, "restored_revision": hist[-1]["revision"] if hist else 0,
                        "hash": scene_hash(cur), "preset": preset_of(cur)}

    def owner_reset(self, confirm=False):
        """Seule opération permise sur un état corrompu : remplace le fichier par le défaut d'OpenTV (historique vidé,
        nouvelle époque : tout aperçu antérieur devient périmé). Un dossier dangereux n'est jamais « réparé »."""
        self._check_confirm(confirm)
        with self._locked(True) as dfd:
            with self._mu:
                self._load(dfd)
                self._preview = None
                was_corrupt = self._corrupt is not None
                rev = 1 if was_corrupt else self._next_revision()
                self._commit(dfd, secrets.token_hex(16), rev, [], restore=not was_corrupt)
                return {"revision": rev, "hash": scene_hash(DEFAULT_SCENE), "preset": DEFAULT_PRESET}

    # ------------------------------------------------------------------ écriture atomique
    def _next_revision(self):
        if self._revision >= MAX_REVISION:
            raise CanvasError("revision_exhausted")
        return self._revision + 1

    def _at(self):
        try:
            w = self._wall()
            at = int(w) if type(w) in (int, float) and math.isfinite(w) else 0
        except Exception:
            at = 0
        return min(max(at, 0), MAX_AT)

    def _write_replace(self, dfd, data):
        tmp = ".%s.%s.tmp" % (STATE_NAME, secrets.token_hex(6))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC, 0o600,
                     dir_fd=dfd)
        try:
            try:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                if self._root:
                    os.fchown(fd, self._owner, getattr(self, "_dir_gid", -1))
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, STATE_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            _unlink_quiet(tmp, dfd)
            raise
        os.fsync(dfd)

    def _commit(self, dfd, epoch, revision, history, restore=True):
        """Écrit puis relit ; relecture différente ⇒ restauration des octets précédents (ou retrait du fichier
        créé). `restore=False` (réinitialisation d'un état corrompu) : l'ancien contenu n'est jamais réécrit."""
        data = _render(epoch, revision, history)
        old = self._raw if restore else None
        try:
            self._write_replace(dfd, data)
        except OSError:
            raise CanvasError("write_failed")
        if _readback(dfd, self._owner) != data:
            try:
                if old is not None:
                    self._write_replace(dfd, old)
                elif restore:
                    _unlink_quiet(STATE_NAME, dfd)
                    os.fsync(dfd)
            except OSError:
                pass
            self._load(dfd)
            raise CanvasError("readback_failed")
        self._epoch, self._revision, self._history, self._corrupt, self._raw = \
            epoch, revision, list(history), None, data


def store_for(cfg, **kw):
    """Magasin du produit : `<OPENTV_STATE_DIR>/canvas` (/var/lib/opentv/canvas par défaut)."""
    return CanvasStore(os.path.join(cfg.state_dir, DIR_NAME), **kw)
