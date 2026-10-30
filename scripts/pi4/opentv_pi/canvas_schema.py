"""OpenTV Canvas, module interne : schéma fermé de scène, préréglages, erreurs typées et document persistant.

Aucune E/S ici : validation pure, hachage canonique, analyse/rendu du document `state.json`. L'API publique reste
`opentv_pi.canvas`, qui réexporte ces noms ; ce module n'importe ni `canvas` ni `canvas_storage` (pas de cycle).
"""
import dataclasses
import hashlib
import hmac
import json
import re

from . import validate

SCHEMA_VERSION = 1
MAX_HISTORY = 5
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
CORRUPT_REASONS = ("unsafe_dir", "unsafe_file", "wrong_owner", "wrong_mode", "oversize", "unreadable",
                   "invalid_json", "invalid_shape", "invalid_scene", "hash_mismatch")
# `restore_failed` : une panne après le rename ET l'échec de la restauration ; la mémoire a été rechargée depuis le
# disque, dont l'état (relu, éventuellement la nouvelle version ou « corrupt ») fait foi.
CODES = frozenset(("invalid_agent", "invalid_scene", "invalid_event", "no_change", "ui_busy", "busy_overflow",
                   "preview_pending", "unknown_preview", "preview_expired", "stale_preview", "confirmation_required",
                   "store_corrupt", "store_busy", "lock_unsafe", "unsafe_dir", "write_failed", "readback_failed",
                   "restore_failed", "nothing_to_rollback", "revision_exhausted"))
DOC_KEYS = ("epoch", "history", "revision", "schema_version", "state_hash")
ENTRY_KEYS = ("agent", "at", "hash", "revision", "scene")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_DETAIL = re.compile(r"[a-z_]{1,32}")


class SceneError(ValueError):
    def __init__(self, code, key=None):
        super().__init__("%s%s" % (code, (": " + key) if isinstance(key, str) and key in SCENE_KEYS + CHANGE_KEYS
                                    else ""))
        self.code = code
        self.key = key if isinstance(key, str) and key in SCENE_KEYS + CHANGE_KEYS else None


class CanvasError(RuntimeError):
    """Refus typé : `code` (liste fermée CODES, vérifiée réellement — jamais par assert, actif aussi sous -O) +
    `detail` facultatif (code court ; jamais de chemin). Code inconnu ⇒ ValueError (erreur de programmation)."""

    def __init__(self, code, detail=None):
        if type(code) is not str or code not in CODES:
            raise ValueError("code CanvasError inconnu")
        self.code = code
        self.detail = detail if type(detail) is str and _DETAIL.fullmatch(detail) else None
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


def parse_document(data):
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


def render_document(epoch, revision, history):
    body = {"schema_version": SCHEMA_VERSION, "epoch": epoch, "revision": revision,
            "history": [{"revision": e["revision"], "hash": e["hash"], "scene": e["scene"].to_dict(),
                         "at": e["at"], "agent": e["agent"]} for e in history]}
    body["state_hash"] = _digest(body)
    return _canon(body).encode("ascii")
