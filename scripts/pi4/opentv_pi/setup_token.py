"""Jeton de configuration affiché en QR : usage unique, TTL, essais bornés, jamais sur disque ni journalisé."""
import hashlib
import hmac
import secrets
import threading
import time

DEFAULT_TTL_S = 600
DEFAULT_ATTEMPTS = 5


def _h(t):
    return hashlib.sha256(t.encode("utf-8")).digest()


class SetupToken:
    def __init__(self, clock=time.time, ttl=DEFAULT_TTL_S, max_attempts=DEFAULT_ATTEMPTS):
        self.clock, self.ttl, self.max_attempts = clock, ttl, max_attempts
        self._lock = threading.Lock()
        self._tok = self._hash = None
        self._exp = 0.0
        self._attempts = 0
        self._dead = True

    def __repr__(self):   # le jeton n'apparaît jamais dans une trace
        return "<SetupToken live=%s>" % (not self._dead)

    def issue(self):
        with self._lock:
            self._tok = secrets.token_urlsafe(32)
            self._hash = _h(self._tok)
            self._exp = self.clock() + self.ttl
            self._attempts, self._dead = 0, False
            return self._tok

    def _live(self):
        return not self._dead and self._hash is not None and self.clock() < self._exp

    def check(self, tok):
        """Valide sans consommer (affichage du formulaire). Comparaison en temps constant."""
        if not isinstance(tok, str) or not 8 <= len(tok) <= 128:
            return False
        with self._lock:
            return self._live() and hmac.compare_digest(_h(tok), self._hash)

    def consume(self, tok):
        """Consommation unique : True une seule fois pour un jeton valide."""
        if not isinstance(tok, str) or not 8 <= len(tok) <= 128:
            return False
        with self._lock:
            if self._live() and hmac.compare_digest(_h(tok), self._hash):
                self._dead, self._tok = True, None
                return True
            return False

    def fail_attempt(self):
        with self._lock:
            self._attempts += 1
            if self._attempts >= self.max_attempts:
                self._dead, self._tok = True, None

    def burn(self):
        with self._lock:
            self._dead, self._tok = True, None

    def needs_rotation(self):
        with self._lock:
            return not self._live()

    def current(self):
        """-> (jeton, secondes restantes) pour l'affichage TV loopback, ou None."""
        with self._lock:
            if not self._live() or self._tok is None:
                return None
            return self._tok, int(round(self._exp - self.clock()))
