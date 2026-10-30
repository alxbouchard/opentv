"""OpenTV Canvas côté service : vue TV épurée, feuille de confirmation du propriétaire, raisons d'occupation.

Pont entre `canvas.CanvasStore` (tâche 1) et l'App / la page /tv (loopback, clé de page). Aucun rendu ici.

- Vue TV (`view`) : scène publiée (état stable ok|corrupt|unavailable, révision, empreinte, préréglage), aperçu
  vivant et feuille de confirmation. Jamais de chemin, d'identité authentifiée brute (seul l'agent CANONIQUE :
  « muse-device » -> « muse »), de raison de corruption ni de raison d'occupation.
- Feuille : agent canonique + nom affiché, description FERMÉE (champs du schéma et valeurs énumérées),
  « Non » focalisé et choisi à l'échéance ; absente s'il reste moins de SHEET_MIN_S ou si une raison d'occupation
  est active. Ses touches n'appellent JAMAIS `human_event` ni `busy_enter` (voir `canvas`) : elle n'est pas un
  panneau. Toute autre touche ou action humaine de la page passe par `human` AVANT d'agir.
- Raisons d'occupation : celles du SERVICE (configuration, assistants, Muse, lecture et retour) sont des jetons
  synchronisés sur l'état de l'App (`sync`) ; celles de la PAGE (panneau, page cachée, hors ligne) sont déclarées
  par la page (`page_busy` : ensemble complet daté (page, seq), bail périodique), appliqué sous `_mu` seulement si
  `seq` croît pour la page active ; une page jamais vue prend la main et retire l'ancienne (périmée à vie, borné).
  Un rechargement ne rend que les jetons de la PAGE (jamais `busy_reset`) ; identifiants de page jamais exposés.
- Audit : outil « canvas », identité authentifiée de l'agent, action et préréglage énumérés, révision, empreinte
  courte (16 hex) ; jamais la scène, du texte libre ni un chemin. L'auteur d'un aperçu est lié à SON identifiant
  (`_props`, verrou court `_pl` jamais tenu pendant un appel au magasin : instantané -> opération du magasin ->
  retrait conditionnel de CET identifiant seulement) ; l'aperçu annulé par un geste humain est relevé par le magasin
  sous le verrou de l'annulation. Auteur inconnu ou action du propriétaire : acteur réservé `agents.OWNER_ACTOR`,
  jamais un autre agent. Ordre : « proposed » est audité AVANT l'attribution (`_settle`) ; une annulation humaine d'un
  aperçu pas encore attribué pendant une création en cours est différée jusqu'à la fin de celle-ci (jamais perdue,
  jamais avant sa proposition).
- Exclusion aperçu ⇄ actions d'assistant (file tvcontrol) : portée par la porte de l'App (`agent_tv_gate`). Relais
  tâche 3 : la page /tv ne doit JAMAIS appliquer une action d'assistant réclamée pendant que la feuille est affichée.
- Échec `restore_failed` : jamais annoncé « inchangé » ; la réponse porte l'état relu du disque (`see_state`).
"""
import collections
import re
import threading

from . import agents, canvas as cv

SERVICE_REASONS = ("setup", "assistants", "muse_setup", "playback", "loading", "closing", "returning")
PAGE_REASONS = ("panel", "hidden", "offline")
PAGE_ID = re.compile(r"[0-9a-f]{32}")   # identifiant aléatoire d'un chargement de /tv (aucun secret)
SEQ_MAX = 2 ** 53 - 1                   # entier exact côté navigateur
RETIRED_PAGES = 32                      # pages remplacées retenues (≫ plafond de rechargements automatiques)
PROPS_MAX = 8                  # auteurs d'aperçus récents retenus (le magasin n'a qu'UN aperçu vivant)
SHEET_MIN_S = 3.0              # en deçà, plus de feuille : jamais un « Oui » qui court contre l'échéance
APPLY_KEYS = ("preset",) + tuple(f for f in cv.FIELDS if f != "row_order")   # Réglages : énumérations plates
# Codes après lesquels l'état publié est garanti inchangé ; tout autre échec renvoie l'état relu (« see_state »)
UNCHANGED = frozenset(cv.CODES) - {"restore_failed"}
BAD_INPUT = ("confirmation_required", "invalid_scene", "invalid_event")
NAMES = {"muse": "Muse", "hermit": "Hermit", "hermes": "Hermes"}
EVENTS = {"confirm": "confirmed", "cancel": "rejected", "rollback": "rolled-back", "reset": "reset",
          "apply": "applied"}


class Unavailable:
    """Magasin impossible à ouvrir (configuration du service) : défaut immuable, aucun aperçu ni écriture."""

    def __getattr__(self, name):
        raise cv.CanvasError("store_corrupt")


class TvCanvas:
    SHEET_MIN_S = SHEET_MIN_S

    def __init__(self, store, audit, log):
        self.store, self.audit, self.log = store, audit, log
        self.available = not isinstance(store, Unavailable)
        self._mu = threading.Lock()
        self._held = {}            # raison -> jeton (service ET page : raisons disjointes)
        self._page, self._seq = None, 0                 # page active et dernier numéro appliqué (sous `_mu`)
        self._retired = collections.OrderedDict()       # pages remplacées : périmées (borné, sous `_mu`)
        self._pl = threading.Lock()   # court : `_props` seulement, jamais tenu pendant un appel au magasin
        self._props = collections.OrderedDict()   # preview_id -> {"id", "agent", "hash", "preset"} (borné)
        self._inflight = 0         # créations d'aperçu en cours (sous `_pl`)
        self._deferred = []        # annulations humaines d'aperçus pas encore attribués, à auditer après leur création
        self.on_refused = None     # agent -> None : refus du propriétaire / geste humain (délai de grâce, tâche 4)

    @classmethod
    def for_config(cls, cfg, audit, log, mono):
        try:
            store = cv.store_for(cfg, mono=mono)
        except (ValueError, OSError) as e:
            log("canvas_unavailable", error=type(e).__name__)
            store = Unavailable()
        return cls(store, audit, log)

    # ------------------------------------------------------------------ occupation
    def _apply(self, owned, want):   # sous `_mu`
        for r in owned:
            if r in want and r not in self._held:
                self._held[r] = self.store.busy_enter(r)
            elif r not in want and r in self._held:
                self.store.busy_exit(self._held.pop(r))

    def _set(self, owned, want):
        with self._mu:
            self._apply(owned, want)

    def sync(self, reasons):
        """Raisons du service (ensemble courant) : entre/sort les jetons correspondants ; annule l'aperçu."""
        if self.available:
            self._set(SERVICE_REASONS, set(reasons))

    def page_busy(self, reasons, page, seq):
        """Raisons de la page (ensemble complet, [] au chargement) datées (page, seq). -> None si invalide,
        True si appliqué, False si périmé (page retirée ou numéro déjà dépassé : aucun effet)."""
        if type(reasons) is not list or len(reasons) > len(PAGE_REASONS) or len(set(reasons)) != len(reasons) or \
                any(type(r) is not str or r not in PAGE_REASONS for r in reasons) or \
                type(page) is not str or not PAGE_ID.fullmatch(page) or type(seq) is not int or not 0 < seq <= SEQ_MAX:
            return None
        with self._mu:
            if page in self._retired or (page == self._page and seq <= self._seq):
                return False
            if page != self._page:     # page jamais vue : elle prend la main, l'ancienne est retirée pour toujours
                if self._page is not None:
                    self._retired[self._page] = True
                while len(self._retired) > RETIRED_PAGES:
                    self._retired.popitem(last=False)
                self._page = page
            self._seq = seq
            if self.available:
                self._apply(PAGE_REASONS, set(reasons))
        return True

    def human(self, kind):
        """Geste humain ordinaire (hors feuille) : annule l'aperçu vivant AVANT l'action. -> bool annulé."""
        if type(kind) is not str or kind not in cv.HUMAN_EVENTS:
            raise cv.CanvasError("invalid_event")
        if not self.available:
            return False
        out, gone = self.store._human_event(kind)   # aperçu annulé relevé atomiquement par le magasin
        if gone is not None:
            with self._pl:          # aperçu pas encore attribué pendant une création : audité après sa proposition
                now = gone["id"] in self._props or not self._inflight
                self._props.pop(gone["id"], None)
                if not now:
                    self._deferred.append(gone)
            if now:
                self._audit(gone["agent"], "cancelled", True, None, gone)
        return out["preview_cancelled"]

    # ------------------------------------------------------------------ propriété des aperçus (id -> auteur)
    def _settle(self, meta):
        """Fin d'une création (`meta` : aperçu créé ET déjà audité « proposed », ou None) : attribution, puis
        annulations différées qui lui reviennent (ou toutes, s'il ne reste aucune création en cours). -> à auditer."""
        with self._pl:
            self._inflight -= 1
            if meta is not None:
                self._props[meta["id"]] = meta
                while len(self._props) > PROPS_MAX:
                    self._props.popitem(last=False)
            done, keep = [], []
            for g in self._deferred:
                (done if not self._inflight or (meta is not None and g["id"] == meta["id"]) else keep).append(g)
            self._deferred = keep
            for g in done:
                self._props.pop(g["id"], None)
        return done

    def _forget(self, pid):
        """Retrait conditionnel : l'entrée de CET identifiant seulement (jamais celle d'un aperçu plus récent)."""
        with self._pl:
            self._props.pop(pid, None)

    def _owner_of(self, pid):
        """Instantané de l'auteur de l'aperçu `pid` (liaison identifiant -> agent immuable) ; entre la création dans
        le magasin et `_settle`, l'aperçu vivant du magasin fait foi (lu hors de `_pl`). -> méta ou None."""
        if type(pid) is not str:
            return None
        with self._pl:
            meta = self._props.get(pid)
        if meta is None and self.available:
            p = self.store.status()["preview"]
            if p is not None and p["preview_id"] == pid:
                meta = {"id": pid, "agent": p["agent"], "hash": p["hash"], "preset": p["preset"]}
        return meta

    # ------------------------------------------------------------------ vue TV
    def view(self):
        if not self.available:
            d = cv.DEFAULT_SCENE
            return {"status": "unavailable", "state": "default", "revision": 0, "hash": cv.scene_hash(d),
                    "scene": d.to_dict(), "preset": cv.DEFAULT_PRESET, "can_rollback": False, "preview": None,
                    "sheet": None}
        st = self.store.status()
        p = st["preview"]
        out = {"status": "corrupt" if st["state"] == "corrupt" else "ok", "state": st["state"],
               "revision": st["revision"], "hash": st["hash"], "scene": st["scene"], "preset": st["preset"],
               "can_rollback": bool(st["history"]) and st["state"] != "corrupt", "preview": None, "sheet": None}
        if p is None:
            return out
        canon = p["proposed_by"]
        out["preview"] = {"id": p["preview_id"], "proposed_by": canon, "hash": p["hash"], "preset": p["preset"],
                          "scene": p["scene"], "base_revision": p["base_revision"],
                          "expires_in_s": p["expires_in_s"]}
        if not st["busy"] and p["expires_in_s"] >= SHEET_MIN_S:
            cur, new = st["scene"], p["scene"]
            out["sheet"] = {"preview_id": p["preview_id"], "proposed_by": canon, "agent_name": NAMES.get(canon),
                            "preset": p["preset"], "expires_in_s": p["expires_in_s"],
                            "changes": [{"field": f, "from": cur[f], "to": new[f]}
                                        for f in cv.FIELDS if cur[f] != new[f]],
                            "focus": "no", "choices": ["no", "yes"], "timeout_action": "reject"}
        return out

    # ------------------------------------------------------------------ agent (point d'intégration tâche 4)
    def preview(self, agent, changes, blocked=False):
        """Aperçu par un agent authentifié (façade agent : codes nus). `blocked` : actions d'assistant en cours."""
        with self._pl:
            self._inflight += 1
        meta = None
        try:
            try:
                if blocked:
                    raise cv.CanvasError("ui_busy")
                r = self.store.agent_api(agent).preview(changes)
            except cv.CanvasError as e:
                code = e.code
            else:
                meta = {"id": r["preview_id"], "agent": agent, "hash": r["hash"], "preset": r["preset"]}
                self._audit(agent, "proposed", True, None, dict(meta, revision=r["base_revision"]))
                return r
            self._audit(agent, "proposed", False, code, None)
            raise cv.CanvasError(code)
        finally:                    # toujours : jamais une création « en cours » ni une annulation perdue
            for g in self._settle(meta):
                self._audit(g["agent"], "cancelled", True, None, g)

    def agent_status(self, agent):
        return self.store.agent_api(agent).status()

    def agent_cancel(self, agent, preview_id):
        return self.store.agent_api(agent).cancel(preview_id)

    def preview_live(self):
        return self.available and self.store.status()["preview"] is not None

    # ------------------------------------------------------------------ propriétaire (page /tv)
    def post(self, action, body):
        """Route POST /api/tv/canvas/<action> (clé de page déjà vérifiée). -> (code HTTP, corps)."""
        if action == "event":
            try:
                return 200, {"ok": True, "preview_cancelled": self.human(body.get("kind"))}
            except cv.CanvasError:
                return 400, {"ok": False, "error": "invalid_event"}
        if action == "ui":   # périmé (stale) : 200 sans effet ; invalide : 400
            done = self.page_busy(body.get("busy"), body.get("page"), body.get("seq"))
            return (400, {"ok": False, "error": "invalid_event"}) if done is None else \
                (200, {"ok": True, "error": None, "stale": not done})
        if action not in EVENTS:
            return 404, {"ok": False, "error": "not_found"}
        # instantané AVANT l'opération : auteur de l'aperçu VISÉ (jamais « le dernier connu »)
        last = self._owner_of(body.get("preview_id")) if action in ("confirm", "cancel") else None
        try:
            result = self._owner(action, body)
        except cv.CanvasError as e:
            code = e.code
        else:
            if result is None:      # « Non » sans aperçu correspondant : rien à retirer
                return 409, self._reply(False, "unknown_preview", None)
            if last is not None:
                self._forget(last["id"])
            self._audit(last and last["agent"], EVENTS[action], True, None, dict(last or {}, **result))
            return 200, self._reply(True, None, result or None)
        if code in BAD_INPUT:       # entrée refusée avant toute opération : jamais auditée
            return 400, self._reply(False, code, None)
        cur = self.view()           # vérité relue (après restore_failed : l'état du disque fait foi)
        self._audit(last and last["agent"], EVENTS[action], False, code, cur)
        return (409 if code in UNCHANGED else 503), self._reply(False, code, None, cur)

    def _owner(self, action, body):
        confirm = body.get("confirm")
        if action == "confirm":
            pid = body.get("preview_id")
            if confirm is not True:
                raise cv.CanvasError("confirmation_required")
            return self.store.owner_confirm(pid if type(pid) is str else "", confirm=True)
        if action == "cancel":
            pid = body.get("preview_id")
            return {} if self.store.owner_reject(pid if type(pid) is str else "") else None
        if action == "apply":
            ch = body.get("changes")
            if confirm is not True:
                raise cv.CanvasError("confirmation_required")
            if type(ch) is not dict or not ch or any(k not in APPLY_KEYS or type(v) is not str
                                                     for k, v in ch.items()):
                raise cv.CanvasError("invalid_scene")
            return self.store.owner_apply(dict(ch), confirm=True)
        if confirm is not True:
            raise cv.CanvasError("confirmation_required")
        if action == "rollback":
            return self.store.owner_rollback(confirm=True)
        return self.store.owner_reset(confirm=True)

    def _reply(self, ok, error, result, cur=None):
        if ok:
            outcome = "applied" if result is not None else "unchanged"
        else:
            outcome = "unchanged" if error in UNCHANGED else "see_state"
        return {"ok": ok, "error": error, "outcome": outcome, "result": result,
                "canvas": cur if cur is not None else self.view()}

    def _audit(self, agent, event, ok, error, info):
        args = {"action": event}
        if info:
            if info.get("preset") is not None or "hash" in info:
                args["preset"] = info.get("preset") or "custom"
            if isinstance(info.get("hash"), str):
                args["hash"] = info["hash"][:16]
            if type(info.get("revision")) is int:
                args["revision"] = info["revision"]
        self.audit.record(agent or agents.OWNER_ACTOR, "canvas", args, ok, error, 0)
        if ok and agent and event in ("rejected", "cancelled") and self.on_refused is not None:
            self.on_refused(agent)   # aperçu d'un agent refusé (« Non », échéance) ou annulé par un geste humain
