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
  par la page (`page_busy`, ensemble complet). Un rechargement de la page ne rend que SES jetons : jamais de
  `busy_reset` global.
- Audit : outil « canvas », identité authentifiée de l'agent, action et préréglage énumérés, révision, empreinte
  courte (16 hex) ; jamais la scène, du texte libre ni un chemin.
- Échec `restore_failed` : jamais annoncé « inchangé » ; la réponse porte l'état relu du disque (`see_state`).
"""
import threading

from . import canvas as cv

SERVICE_REASONS = ("setup", "assistants", "muse_setup", "playback", "loading", "closing", "returning")
PAGE_REASONS = ("panel", "hidden", "offline")
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
        self._last = None          # dernier aperçu créé par `preview` : {"id", "agent", "hash", "preset"}

    @classmethod
    def for_config(cls, cfg, audit, log, mono):
        try:
            store = cv.store_for(cfg, mono=mono)
        except (ValueError, OSError) as e:
            log("canvas_unavailable", error=type(e).__name__)
            store = Unavailable()
        return cls(store, audit, log)

    # ------------------------------------------------------------------ occupation
    def _set(self, owned, want):
        with self._mu:
            for r in owned:
                if r in want and r not in self._held:
                    self._held[r] = self.store.busy_enter(r)
                elif r not in want and r in self._held:
                    self.store.busy_exit(self._held.pop(r))

    def sync(self, reasons):
        """Raisons du service (ensemble courant) : entre/sort les jetons correspondants ; annule l'aperçu."""
        if self.available:
            self._set(SERVICE_REASONS, set(reasons))

    def page_busy(self, reasons):
        """Raisons déclarées par la page (ensemble complet, [] au chargement). -> False si invalide."""
        if type(reasons) is not list or len(reasons) > len(PAGE_REASONS) or \
                any(type(r) is not str or r not in PAGE_REASONS for r in reasons):
            return False
        if self.available:
            self._set(PAGE_REASONS, set(reasons))
        return True

    def human(self, kind):
        """Geste humain ordinaire (hors feuille) : annule l'aperçu vivant AVANT l'action. -> bool annulé."""
        if type(kind) is not str or kind not in cv.HUMAN_EVENTS:
            raise cv.CanvasError("invalid_event")
        if not self.available:
            return False
        out = self.store.human_event(kind)
        if out["preview_cancelled"]:
            last, self._last = self._last, None
            self._audit(last and last["agent"], "cancelled", True, None, last)
        return out["preview_cancelled"]

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
        try:
            if blocked:
                raise cv.CanvasError("ui_busy")
            r = self.store.agent_api(agent).preview(changes)
        except cv.CanvasError as e:
            code = e.code
        else:
            self._last = {"id": r["preview_id"], "agent": agent, "hash": r["hash"], "preset": r["preset"]}
            self._audit(agent, "proposed", True, None, dict(self._last, revision=r["base_revision"]))
            return r
        self._audit(agent, "proposed", False, code, None)
        raise cv.CanvasError(code)

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
        if action == "ui":
            ok = self.page_busy(body.get("busy"))
            return (200 if ok else 400), {"ok": ok, "error": None if ok else "invalid_event"}
        if action not in EVENTS:
            return 404, {"ok": False, "error": "not_found"}
        last = self._last
        pid = body.get("preview_id")
        if last is not None and (action not in ("confirm", "cancel") or pid != last["id"]):
            last = None             # l'aperçu visé n'est pas le dernier connu : aucun agent attribué
        try:
            result = self._owner(action, body)
        except cv.CanvasError as e:
            code = e.code
        else:
            if result is None:      # « Non » sans aperçu correspondant : rien à retirer
                return 409, self._reply(False, "unknown_preview", None)
            self._last = None
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
        self.audit.record(agent or "tv", "canvas", args, ok, error, 0)
