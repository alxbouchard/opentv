"""OpenTV Canvas : scène d'ambiance déclarative (schéma fermé) et magasin persistant fail-closed.

Une scène est une DONNÉE validée, jamais du code : schéma v1 = `wallpaper`, `motion`, `accent`, `shade`,
`hero_focus` (énumérations fermées) et `row_order` (permutation des rangées connues, « tonight » en premier). Aucune
URL, CSS, HTML, chemin ni ressource arbitraire. Préréglages intégrés : `mist`, `cinema`, `canvas` (défaut immuable).
API publique de ce module ; schéma (`canvas_schema`), E/S (`canvas_storage`), activité de l'interface
(`canvas_activity`) et façades par rôle (`canvas_roles`) sont des modules internes réexportés ici.

Cycle de vie (séparation stricte des rôles) :
- agent (`agent_api` -> AgentCanvas : status/preview/cancel) : propose un aperçu EN MÉMOIRE seulement (identifiant
  hexadécimal minuscule de 32 caractères, échéance sur horloge monotone) et peut retirer SON aperçu. Rien côté agent ne
  confirme, n'applique, ne revient en arrière ni ne réinitialise. La façade agent ne renvoie que des codes nus : jamais
  de `detail` (raison d'occupation telle que playback/muse_setup, raison de corruption telle que wrong_owner), ni
  d'exception chaînée. Les méthodes du magasin et la façade propriétaire gardent ces détails bornés.
- propriétaire (`owner_api`, `owner_confirm|reject|apply|rollback|reset`) : appelé seulement par la feuille de
  confirmation / les Réglages de la TV ; exige `confirm=True` littéral pour toute écriture.
- interface : `human_event` (touche, télécommande téléphone…) annule l'aperçu vivant et ouvre une fenêtre de silence
  monotone ; `busy_enter(raison)` -> jeton / `busy_exit(jeton)` représentent des raisons d'occupation IMBRIQUÉES
  (panneaux, lecture, configuration…) : chaque entrée annule l'aperçu ; la confirmation d'un aperçu agent est refusée
  tant qu'une raison quelconque ou la fenêtre de silence est active.

Intégration de la feuille de confirmation (tâche 2) : la feuille « Appliquer l'ambiance proposée ? » est la SEULE
surface qui garde l'aperçu vivant. Ses touches de navigation (←/→ entre Oui et Non, OK, Retour) n'appellent ni
`human_event` ni `busy_enter` — sinon elles annuleraient l'aperçu qu'elles servent à confirmer ; OK sur « Oui »
appelle `owner_confirm`, OK sur « Non » ou Retour appelle `owner_reject`. La feuille elle-même n'est PAS une raison
d'occupation : elle n'entre jamais « panel ». Toute touche hors de la feuille reste un `human_event`, et toute autre
surface (panneau de titre, Réglages, lecture…) passe par `busy_enter`.
Délai de grâce par agent après un refus ou une annulation : volontairement hors du magasin (il changerait le contrat
de la tâche 1) ; il relève des budgets de débit par agent canonique de la tâche 4 (agentport.TOOL_RATES).

Persistance : `<dossier>/state.json` (défaut /var/lib/opentv/canvas, dossier 0700 et fichier 0600 du compte du
service) = document canonique auto-haché {schema_version, epoch, revision, history (5 dernières publications),
state_hash}. Écriture sous flock borné de `state.json.lock`, relecture forcée sous ce verrou, publication
« échouer et restaurer » (voir canvas_storage) puis relecture du disque en cas d'échec : le résultat renvoyé, le
disque et la mémoire concordent toujours. Un aperçu porte la base (epoch, revision) : toute écriture étrangère
(autre processus, CLI root, réinitialisation) le rend périmé.

Fail-closed : lien symbolique, FIFO, dossier, lien dur, taille excessive, autre propriétaire, mode inattendu, JSON
invalide (y compris RecursionError/MemoryError du décodeur), hachage ou forme incorrects ⇒ état « corrupt » : la TV
montre le défaut immuable, aucun aperçu ni écriture, octets intacts ; seule `owner_reset` remplace le fichier.

Bibliothèque standard seulement ; horloges murale et monotone injectables pour les tests.
"""
import contextlib
import hmac
import math
import os
import secrets
import threading
import time

from . import agents, fsutil
from . import canvas_storage as _st
from .canvas_activity import BUSY_REASONS, HUMAN_EVENTS, MAX_BUSY_TOKENS, NAV_QUIET_S, Activity
from .canvas_roles import AgentCanvas, OwnerCanvas
from .canvas_schema import (CHANGE_KEYS, CODES, CORRUPT_REASONS, DEFAULT_PRESET, DEFAULT_SCENE, DOC_KEYS,
                            ENTRY_KEYS, ENUMS, FIELDS, FIRST_ROW, MAX_AT, MAX_HISTORY, MAX_REVISION, PRESETS,
                            ROW_IDS, SCENE_KEYS, SCHEMA_VERSION, CanvasError, Scene, SceneError, _Corrupt, _HEX32,
                            _is_hex, _valid_agent, apply_changes, canonical_json, parse_document, preset_of,
                            render_document, scene_hash, validate_scene)
from .canvas_storage import LOCK_NAME, MAX_STATE_BYTES, STATE_NAME

__all__ = ("AgentCanvas", "BUSY_REASONS", "CHANGE_KEYS", "CODES", "CORRUPT_REASONS", "CanvasError", "CanvasStore",
           "DEFAULT_DIR", "DEFAULT_PRESET", "DEFAULT_SCENE", "DIR_NAME", "DOC_KEYS", "ENTRY_KEYS", "ENUMS", "FIELDS",
           "FIRST_ROW", "HUMAN_EVENTS", "LOCK_NAME", "LOCK_TIMEOUT_S", "MAX_AT", "MAX_BUSY_TOKENS", "MAX_HISTORY",
           "MAX_REVISION", "MAX_STATE_BYTES", "NAV_QUIET_S", "OwnerCanvas", "PRESETS", "ROW_IDS", "SCENE_KEYS",
           "SCHEMA_VERSION", "STATES", "STATE_NAME", "Scene", "SceneError", "apply_changes", "canonical_json",
           "preset_of", "scene_hash", "store_for", "validate_scene")
DEFAULT_DIR = "/var/lib/opentv/canvas"
DIR_NAME = "canvas"
LOCK_TIMEOUT_S = 2.0
STATES = ("default", "applied", "corrupt")


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
        self._act = Activity(mono)        # raisons d'occupation imbriquées + fenêtre de silence humaine
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

    # ------------------------------------------------------------------ disque
    @contextlib.contextmanager
    def _locked(self, create):
        """Dossier + flock borné (canvas_storage.locked). -> dfd ; None si le dossier est absent (create=False)."""
        with _st.locked(self._parent, self._name, self._owner, self._root, create, self._lock_timeout,
                        self._after_lock) as opened:
            if opened is not None:
                self._dir_gid = opened[1]
            yield None if opened is None else opened[0]

    def _load(self, dfd=None):
        """Relit le disque (sous _mu). N'abandonne PAS l'aperçu : voir _sync_preview."""
        try:
            data = _st.read_state(dfd, self._owner) if dfd is not None else \
                _st.load_raw(self._parent, self._name, self._owner, self._root)
            self._epoch, self._revision, self._history = (None, 0, []) if data is None else parse_document(data)
            self._corrupt, self._raw = None, data
        except Exception as e:
            reason = e.reason if isinstance(e, _Corrupt) else "unreadable"
            self._epoch, self._revision, self._history, self._corrupt, self._raw = None, 0, [], reason, None

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
                "busy": list(self._act.reasons()),
            }

    def _agent_status(self, agent):
        with self._mu:
            p = self._refresh()
            cur = self._current()
            return {"state": self._state(), "revision": self._revision, "hash": scene_hash(cur),
                    "scene": cur.to_dict(), "preset": preset_of(cur),
                    "preview": self._preview_view(p) if p and p["agent"] == agent else None}

    # ------------------------------------------------------------------ occupation et humain
    def busy_reasons(self):
        with self._mu:
            return self._act.reasons()

    def _cancel_live(self):
        live = self._preview is not None and self._mono() < self._preview["expires"]
        self._preview = None
        return live

    def human_event(self, kind):
        """Touche/télécommande humaine : annule l'aperçu vivant AVANT d'agir et ouvre la fenêtre de silence."""
        return self._human_event(kind)[0]

    def _human_event(self, kind):
        """human_event + identité de l'aperçu annulé {id, agent, hash, preset} (ou None), relevée sous le MÊME
        verrou que l'annulation : l'audit de la TV (canvas_tv) ne l'attribue jamais à un autre aperçu."""
        self._act.check_human(kind)
        with self._mu:
            p = self._preview
            cancelled = self._cancel_live()
            self._act.human()
            gone = {"id": p["id"], "agent": p["agent"], "hash": p["hash"], "preset": preset_of(p["scene"])} \
                if cancelled and p is not None else None
            return {"preview_cancelled": cancelled, "busy": list(self._act.reasons())}, gone

    def busy_enter(self, reason):
        """Entrée dans une raison d'occupation -> jeton (32 hex) à rendre à busy_exit. Annule l'aperçu vivant."""
        self._act.check_reason(reason)
        with self._mu:
            self._cancel_live()
            return self._act.enter(reason)       # dépassement : occupé jusqu'à busy_reset explicite

    def busy_exit(self, token):
        """Sortie d'UNE entrée ; jeton inconnu/déjà rendu ⇒ False, sans effet sur les autres."""
        with self._mu:
            return self._act.exit(token)

    def busy_reset(self):
        """Remise à zéro explicite (nouvelle page TV) : oublie toutes les entrées et le dépassement."""
        with self._mu:
            self._act.reset()

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
            busy = self._act.reasons()
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
            busy = self._act.reasons()
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
                busy = self._act.reasons()
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

    # ------------------------------------------------------------------ écriture véridique
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

    def _commit(self, dfd, epoch, revision, history, restore=True):
        """Publie puis adopte l'état ; sur tout échec, recharge le disque (résultat = disque = mémoire). État visé en
        cas d'échec : les octets précédents, ou l'absence (premier enregistrement ; `restore=False` : réinitialisation
        d'un état corrompu, dont l'ancien contenu n'est jamais réécrit)."""
        data = render_document(epoch, revision, history)
        try:
            _st.commit(dfd, data, self._raw if restore else None, self._owner, self._root, self._dir_gid)
        except CanvasError:
            self._load(dfd)
            raise
        self._epoch, self._revision, self._history, self._corrupt, self._raw = \
            epoch, revision, list(history), None, data


def store_for(cfg, **kw):
    """Magasin du produit : `cfg.canvas_dir` = `<OPENTV_STATE_DIR>/canvas` (/var/lib/opentv/canvas par défaut)."""
    return CanvasStore(cfg.canvas_dir, **kw)
