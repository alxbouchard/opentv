"""OpenTV Canvas — modèle de scène de référence (PROTOTYPE, phase 1, jamais installé ni importé par opentv_pi).

Une scène est une DONNÉE validée, jamais du code : six énumérations fermées et un ordre de rangées pris dans une
liste fermée. Aucune clé inconnue, URL, CSS, balise, chemin ou nom de ressource arbitraire n'est accepté.

Cycle de vie : aperçu temporaire (horloge monotone, retour automatique) → confirmation explicite (`confirm=True`
littéral) → publication atomique (fichier temporaire du même dossier, fsync, rename, fsync du dossier, relecture) →
historique des cinq dernières publications → retour arrière exact ou défaut immuable d'OpenTV. Un fichier corrompu
ou dangereux n'est jamais réécrit implicitement : la TV affiche le défaut et seule une réinitialisation confirmée
peut le remplacer.

Bibliothèque standard seulement ; horloges murale et monotone injectables pour les tests.
"""
import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import time

SCHEMA_VERSION = 1
MAX_HISTORY = 5
MAX_STORE_BYTES = 256 * 1024
LOCK_WAIT_S = 2.0

ENUMS = {
    "variant": ("mist", "cinema", "canvas"),
    "wallpaper": ("brume", "affiche", "aurore"),
    "motion": ("full", "reduced", "off"),
    "accent": ("ice", "ember", "jade", "violet"),
    "shade": ("soft", "balanced", "deep"),
    "hero_focus": ("title", "artwork", "calm"),
}
ROW_IDS = ("tonight", "new", "top", "watchlist")
REQUIRED_ROW = "tonight"            # la rangée principale ne peut jamais disparaître
SCENE_KEYS = ("schema_version",) + tuple(ENUMS) + ("row_order",)
PATCH_KEYS = tuple(k for k in SCENE_KEYS if k != "schema_version")
UI_STATES = ("idle", "navigating", "panel", "setup", "playback", "offline")
STORE_KEYS = ("history", "revision", "schema_version", "state_hash")
ENTRY_KEYS = ("at", "hash", "revision", "scene")


class SceneError(ValueError):
    def __init__(self, code, key=None):
        super().__init__("%s%s" % (code, (": " + key) if key else ""))
        self.code, self.key = code, key


class StoreError(RuntimeError):
    def __init__(self, code, detail=None):
        super().__init__("%s%s" % (code, (": " + detail) if detail else ""))
        self.code = code


class _Corrupt(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class Scene:
    schema_version: int
    variant: str
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
    """Scène complète → Scene gelée ; toute divergence lève SceneError (fail-closed)."""
    if type(d) is not dict:
        raise SceneError("invalid_shape")
    for k in d:
        if k not in SCENE_KEYS:
            raise SceneError("unknown_key", k if type(k) is str else None)
    for k in SCENE_KEYS:
        if k not in d:
            raise SceneError("missing_key", k)
    v = d["schema_version"]
    if type(v) is not int or v != SCHEMA_VERSION:
        raise SceneError("invalid_value", "schema_version")
    vals = {}
    for k, allowed in ENUMS.items():
        v = d[k]
        if type(v) is not str or v not in allowed:
            raise SceneError("invalid_value", k)
        vals[k] = v
    rows = d["row_order"]
    if type(rows) not in (list, tuple) or not 1 <= len(rows) <= len(ROW_IDS):
        raise SceneError("invalid_value", "row_order")
    for r in rows:
        if type(r) is not str or r not in ROW_IDS:
            raise SceneError("invalid_value", "row_order")
    if len(set(rows)) != len(rows) or REQUIRED_ROW not in rows:
        raise SceneError("invalid_value", "row_order")
    return Scene(schema_version=SCHEMA_VERSION, row_order=tuple(rows), **vals)


def apply_patch(base, patch):
    """Modification partielle (clés de PATCH_KEYS seulement) appliquée à une scène valide."""
    if type(patch) is not dict or not patch:
        raise SceneError("invalid_patch")
    for k in patch:
        if k not in PATCH_KEYS:
            raise SceneError("unknown_key", k if type(k) is str else None)
    d = base.to_dict()
    d.update(patch)
    return validate_scene(d)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_json(scene):
    return _canon(scene.to_dict())


def scene_hash(scene):
    return hashlib.sha256(canonical_json(scene).encode("ascii")).hexdigest()


DEFAULT_SCENE = Scene(SCHEMA_VERSION, "canvas", "aurore", "full", "ice", "balanced", "title",
                      ("tonight", "new", "top", "watchlist"))
PRESETS = {
    "mist": Scene(SCHEMA_VERSION, "mist", "brume", "full", "ice", "balanced", "title",
                  ("tonight", "new", "top", "watchlist")),
    "cinema": Scene(SCHEMA_VERSION, "cinema", "affiche", "full", "ember", "deep", "artwork",
                    ("tonight", "top", "new", "watchlist")),
    "canvas": DEFAULT_SCENE,
}


# --------------------------------------------------------------------------- lecture sûre (dossier + O_NOFOLLOW)
def _safe_read(dirfd, name):
    """Octets du fichier `name` relatif à `dirfd`, ou None s'il est absent. Lien symbolique, FIFO, dossier, lien
    dur ou taille excessive ⇒ _Corrupt (jamais suivi, jamais bloquant)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK, errno.EISDIR, errno.ENXIO):
            raise _Corrupt("unsafe_file")
        raise _Corrupt("unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _Corrupt("unsafe_file")
        if st.st_size > MAX_STORE_BYTES:
            raise _Corrupt("oversize")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            total += len(b)
            if total > MAX_STORE_BYTES:
                raise _Corrupt("oversize")
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_store_bytes(dirfd, name):
    """Relecture après écriture (point d'injection des tests)."""
    return _safe_read(dirfd, name)


def _parse_store(data):
    """Document persistant → (révision, historique) ; toute anomalie ⇒ _Corrupt."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Corrupt("invalid_json")
    if type(doc) is not dict or tuple(sorted(doc)) != STORE_KEYS:
        raise _Corrupt("invalid_shape")
    body = {k: v for k, v in doc.items() if k != "state_hash"}
    try:
        want = hashlib.sha256(_canon(body).encode("ascii")).hexdigest()
    except (ValueError, TypeError, RecursionError):
        raise _Corrupt("invalid_shape")
    if type(doc["state_hash"]) is not str or not hmac.compare_digest(doc["state_hash"], want):
        raise _Corrupt("hash_mismatch")
    if doc["schema_version"] != SCHEMA_VERSION or type(doc["schema_version"]) is not int:
        raise _Corrupt("invalid_shape")
    rev = doc["revision"]
    hist = doc["history"]
    if type(rev) is not int or rev < 0 or type(hist) is not list or len(hist) > MAX_HISTORY:
        raise _Corrupt("invalid_shape")
    out, last = [], 0
    for e in hist:
        if type(e) is not dict or tuple(sorted(e)) != ENTRY_KEYS:
            raise _Corrupt("invalid_shape")
        r, at = e["revision"], e["at"]
        if type(r) is not int or not last < r <= rev:
            raise _Corrupt("invalid_shape")
        if type(at) not in (int, float) or not math.isfinite(at):
            raise _Corrupt("invalid_shape")
        try:
            sc = validate_scene(e["scene"])
        except SceneError:
            raise _Corrupt("invalid_scene")
        if type(e["hash"]) is not str or not hmac.compare_digest(e["hash"], scene_hash(sc)):
            raise _Corrupt("hash_mismatch")
        out.append({"revision": r, "hash": e["hash"], "scene": sc, "at": at})
        last = r
    return rev, out


def _render_store(revision, history):
    body = {"schema_version": SCHEMA_VERSION, "revision": revision,
            "history": [{"revision": e["revision"], "hash": e["hash"], "scene": e["scene"].to_dict(), "at": e["at"]}
                        for e in history]}
    body["state_hash"] = hashlib.sha256(_canon(body).encode("ascii")).hexdigest()
    return _canon(body).encode("ascii")


# --------------------------------------------------------------------------- magasin
class CanvasStore:
    """Scène publiée + aperçu temporaire + historique (5) dans un seul fichier JSON canonique auto-haché."""

    def __init__(self, path, wall=time.time, mono=time.monotonic, preview_ttl_s=30):
        if type(preview_ttl_s) not in (int, float) or not 5 <= preview_ttl_s <= 120:
            raise ValueError("preview_ttl_s hors bornes (5..120 s)")
        self.path = os.path.abspath(path)
        self._dir, self._name = os.path.split(self.path)
        self._wall, self._mono, self._ttl = wall, mono, preview_ttl_s
        self._revision, self._history = 0, []
        self._corrupt = None
        self._preview = None
        self._reload()

    # ------------------------------------------------------------------ état
    @contextlib.contextmanager
    def _dirfd(self):
        fd = os.open(self._dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _locked(self):
        """Verrou inter-processus borné sur le dossier lui-même (aucun fichier annexe)."""
        with self._dirfd() as fd:
            deadline = self._mono_real() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self._mono_real() >= deadline:
                        raise StoreError("store_busy")
                    time.sleep(0.02)
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    @staticmethod
    def _mono_real():
        return time.monotonic()

    def _reload(self, dirfd=None):
        try:
            if dirfd is None:
                with self._dirfd() as fd:
                    data = _safe_read(fd, self._name)
            else:
                data = _safe_read(dirfd, self._name)
            if data is None:
                self._revision, self._history = 0, []
            else:
                self._revision, self._history = _parse_store(data)
            self._corrupt = None
        except _Corrupt as e:
            self._revision, self._history, self._corrupt = 0, [], e.reason
            self._preview = None
        except OSError:
            self._revision, self._history, self._corrupt = 0, [], "unreadable"
            self._preview = None

    def current(self):
        return self._history[-1]["scene"] if self._history else DEFAULT_SCENE

    def _live_preview(self):
        p = self._preview
        if p and self._mono() < p["expires"]:
            return p
        return None

    def visible(self):
        """Ce que la TV doit montrer : l'aperçu tant qu'il n'a pas expiré, sinon la scène publiée."""
        p = self._live_preview()
        return p["scene"] if p else self.current()

    def status(self):
        p = self._live_preview()
        return {
            "state": "corrupt" if self._corrupt else ("published" if self._history else "default"),
            "reason": self._corrupt,
            "revision": self._revision,
            "hash": scene_hash(self.current()),
            "history": [{"revision": e["revision"], "hash": e["hash"], "at": e["at"], "scene": e["scene"].to_dict()}
                        for e in self._history],
            "preview": None if not p else {"hash": p["hash"], "base_revision": p["base"],
                                            "expires_in_s": max(0.0, p["expires"] - self._mono())},
        }

    # ------------------------------------------------------------------ garde-fous
    @staticmethod
    def _check_ui(ui):
        if type(ui) is not str or ui not in UI_STATES:
            raise StoreError("invalid_ui")
        if ui != "idle":
            raise StoreError("ui_busy", ui)       # jamais pendant navigation, panneau, configuration ou lecture

    @staticmethod
    def _check_confirm(confirm):
        if confirm is not True:
            raise StoreError("confirmation_required")

    def _check_sane(self):
        if self._corrupt:
            raise StoreError("store_corrupt", self._corrupt)

    # ------------------------------------------------------------------ opérations
    def preview(self, patch, ui="idle"):
        self._check_ui(ui)
        self._reload()
        self._check_sane()
        sc = apply_patch(self.current(), patch)
        self._preview = {"id": secrets.token_hex(16), "scene": sc, "hash": scene_hash(sc), "base": self._revision,
                         "expires": self._mono() + self._ttl}
        return {"preview_id": self._preview["id"], "hash": self._preview["hash"], "base_revision": self._revision,
                "expires_in_s": self._ttl, "scene": sc.to_dict()}

    def cancel_preview(self):
        self._preview = None

    def confirm(self, preview_id, confirm=False, ui="idle"):
        self._check_confirm(confirm)
        self._check_ui(ui)
        self._check_sane()
        p = self._preview
        if not p or type(preview_id) is not str or not hmac.compare_digest(preview_id, p["id"]):
            raise StoreError("unknown_preview")
        if self._mono() >= p["expires"]:
            self._preview = None
            raise StoreError("preview_expired")
        with self._locked() as fd:
            self._reload(fd)
            self._check_sane()
            if self._revision != p["base"]:
                self._preview = None
                raise StoreError("stale_preview")
            rev = self._revision + 1
            hist = (self._history + [{"revision": rev, "hash": p["hash"], "scene": p["scene"],
                                      "at": self._wall()}])[-MAX_HISTORY:]
            self._commit(fd, rev, hist)
        self._preview = None
        return {"revision": rev, "hash": p["hash"]}

    def rollback(self, confirm=False):
        """Annule exactement la dernière publication (jusqu'à cinq fois), puis le défaut immuable."""
        self._check_confirm(confirm)
        self._check_sane()
        with self._locked() as fd:
            self._reload(fd)
            self._check_sane()
            if not self._history:
                raise StoreError("nothing_to_rollback")
            hist = self._history[:-1]
            rev = self._revision + 1
            self._commit(fd, rev, hist)
        self._preview = None
        return {"revision": rev, "restored_revision": hist[-1]["revision"] if hist else 0,
                "hash": scene_hash(self.current())}

    def reset_default(self, confirm=False):
        """Seule opération permise sur un magasin corrompu : remplace le fichier par le défaut d'OpenTV."""
        self._check_confirm(confirm)
        with self._locked() as fd:
            self._reload(fd)
            rev = (0 if self._corrupt else self._revision) + 1
            self._commit(fd, rev, [], replacing_corrupt=bool(self._corrupt))
        self._preview = None
        return {"revision": rev, "hash": scene_hash(DEFAULT_SCENE)}

    # ------------------------------------------------------------------ écriture atomique
    def _write_tmp(self, dirfd, data):
        tmp = ".%s.%s.tmp" % (self._name, secrets.token_hex(6))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(tmp, flags, 0o600, dir_fd=dirfd)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            _unlink_quiet(tmp, dirfd)
            raise
        os.close(fd)
        return tmp

    def _replace(self, dirfd, data):
        tmp = self._write_tmp(dirfd, data)
        try:
            os.replace(tmp, self._name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except BaseException:
            _unlink_quiet(tmp, dirfd)
            raise
        os.fsync(dirfd)

    def _commit(self, dirfd, revision, history, replacing_corrupt=False):
        data = _render_store(revision, history)
        old = None
        if not replacing_corrupt:
            try:
                old = _safe_read(dirfd, self._name)
            except _Corrupt:
                raise StoreError("store_corrupt")
        try:
            self._replace(dirfd, data)
        except OSError as e:
            raise StoreError("write_failed", e.strerror)
        try:
            back = _read_store_bytes(dirfd, self._name)
        except (_Corrupt, OSError):
            back = None
        if back != data:
            try:
                if old is not None:
                    self._replace(dirfd, old)
                elif not replacing_corrupt:
                    _unlink_quiet(self._name, dirfd)
                    os.fsync(dirfd)
            except OSError:
                pass
            raise StoreError("readback_failed")
        self._revision, self._history, self._corrupt = revision, list(history), None


def _unlink_quiet(name, dirfd):
    try:
        os.unlink(name, dir_fd=dirfd)
    except OSError:
        pass
