"""OpenTV Canvas, module interne : activité de l'interface (raisons d'occupation imbriquées + silence humain).

Mémoire du processus seulement, horloge monotone injectée. Pas de verrou propre : `CanvasStore` sérialise chaque
appel sous son verrou interne et annule lui-même l'aperçu vivant (ordre : validation, annulation, mise à jour).
"""
import secrets

from .canvas_schema import _HEX32, CanvasError, _is_hex

NAV_QUIET_S = 1.5
MAX_BUSY_TOKENS = 64
# raisons d'occupation (imbriquables) : jamais d'aperçu ni de confirmation pendant l'une d'elles
BUSY_REASONS = ("panel", "setup", "assistants", "muse_setup", "playback", "loading", "closing", "returning",
                "offline", "hidden")
HUMAN_EVENTS = ("key", "remote", "pointer")


class Activity:
    """Jetons d'occupation (jeton -> raison : imbrication sûre, jamais un état unique écrasé), dépassement collant
    jusqu'à `reset`, et fenêtre de silence monotone ouverte par le dernier événement humain."""

    def __init__(self, mono):
        self._mono = mono
        self._busy = {}
        self._overflow = False
        self._last_human = None

    def reasons(self):
        out = set(self._busy.values())
        if self._overflow:
            out.add("overflow")
        if self._last_human is not None and self._mono() - self._last_human < NAV_QUIET_S:
            out.add("navigating")
        return tuple(sorted(out))

    @staticmethod
    def check_human(kind):
        if type(kind) is not str or kind not in HUMAN_EVENTS:
            raise CanvasError("invalid_event")

    @staticmethod
    def check_reason(reason):
        if type(reason) is not str or reason not in BUSY_REASONS:
            raise CanvasError("invalid_event")

    def human(self):
        self._last_human = self._mono()

    def enter(self, reason):
        """-> jeton (32 hex) ; CanvasError(busy_overflow) au-delà de MAX_BUSY_TOKENS (occupé jusqu'à `reset`)."""
        if len(self._busy) >= MAX_BUSY_TOKENS:
            self._overflow = True
            raise CanvasError("busy_overflow")
        tok = secrets.token_hex(16)
        self._busy[tok] = reason
        return tok

    def exit(self, token):
        """Sortie d'UNE entrée ; jeton inconnu/déjà rendu/mal formé ⇒ False, sans effet sur les autres."""
        return _is_hex(token, _HEX32) and self._busy.pop(token, None) is not None

    def reset(self):
        self._busy.clear()
        self._overflow = False
