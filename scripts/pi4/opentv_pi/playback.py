"""« Regarder » côté service : automate typé, sans aucune URL dans l'état TV ni agent.

États exposés à /tv :
  idle         rien en cours (« Regarder » utilisable si `available`)
  loading      OK reçu : écran OpenTV « Chargement… » ; résolution puis préparation cachée par le contrôleur
  playing      le contrôleur a révélé le seul lecteur dominant, plein écran
  closing      fermeture demandée (Retour/Quitter/Accueil CEC-HID-téléphone, assistant, plafond) : lecteur en cours d'arrêt
  closed       lecteur et profil disparus ; /tv doit se repeindre puis acquitter (ack) avant le retour à « idle »
  unavailable  aucun lecteur / ambigu / échec / délai / plantage : « Lecture indisponible », site jamais montré ;
               même acquittement que « closed »
`reason` est un code borné ; aucune adresse, aucun domaine, aucun jeton n'apparaît dans `view()` ni dans le journal.

Disponibilité (`available`) = source approuvée (source.env) ET porte de production promue (`gate`, témoin terrain
de promotion.py, relu à chaque sondage) ET contrôleur joignable qui se dit lui-même promu et armable (télécommande
physique capable détectée par l'aide des touches) ET remise à zéro de démarrage terminée (`ready`). source.env seul
ne montre jamais « Regarder ». Une démotion pendant une lecture la ferme (raison `demoted`).
"""
import re
import secrets
import threading
import time

from .playerclient import PlayerUnavailable
from .source import SourceError

STATES = ("idle", "loading", "playing", "closing", "closed", "unavailable")
ACTIVE = ("loading", "playing", "closing")
USER_CLOSE = ("back", "exit", "home", "phone", "tv", "devtools", "agent")
REASON_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MSG_UNAVAILABLE = "Lecture indisponible"


def _reason(r):
    return r if isinstance(r, str) and REASON_RE.match(r) else "other"


class Playback:
    def __init__(self, source, client, log=None, mono=time.monotonic, poll_s=0.2, open_budget_s=8.0,
                 load_margin_s=25.0, ping_every_s=10.0, gate=lambda: False, needs_reset=False):
        self.source, self.client, self.gate = source, client, gate
        self.log = log or (lambda *a, **k: None)
        self.mono, self.poll_s = mono, poll_s
        self.open_budget_s, self.load_margin_s, self.ping_every_s = open_budget_s, load_margin_s, ping_every_s
        self.lock = threading.Lock()
        self.state, self.seq, self.uid, self.reason = "idle", 0, None, None
        self.s = None
        self.ctl_ok, self.ping_at = None, -1e9
        self.promoted, self.gate_status = False, "missing"
        self.timing = {}
        # démarrage du service : aucune ouverture avant la fin de reset() (close_all) — sinon un close_all tardif
        # pourrait fermer la première séance de ce service
        self.ready = threading.Event()
        if not needs_reset:
            self.ready.set()

    # -- disponibilité : source approuvée + contrôleur joignable (sondé au plus toutes les ping_every_s)
    @property
    def available(self):
        return bool(self.source is not None and self.source.available and self.promoted and self.ctl_ok and
                    self.ready.is_set())

    def _check_gate(self):
        try:
            st = self.gate()
        except Exception:   # porte illisible : fermée
            st = "error"
        st = st if isinstance(st, str) else ("promoted" if st is True else "missing")
        if st != self.gate_status:
            self.log("playback_gate", status=st)
        self.gate_status, self.promoted = st, st == "promoted"
        return self.promoted

    def refresh(self, force=False):
        if self.source is None or not self.source.available:
            self.ctl_ok = False
            return
        now = self.mono()
        if force or now - self.ping_at >= self.ping_every_s:
            self.ping_at = now
            if not self._check_gate():   # non promue : aucun sondage du contrôleur, « Regarder » absent
                self.ctl_ok = False
                if self.active():   # démotion pendant une lecture : fermée (le contrôleur la ferme aussi)
                    self.close("demoted")
                return
            self.ctl_ok = self.client.ping()

    def view(self):
        with self.lock:
            return {"available": self.available, "state": self.state, "seq": self.seq, "id": self.uid,
                    "reason": self.reason, "message": MSG_UNAVAILABLE if self.state == "unavailable" else None}

    def active(self):
        with self.lock:
            return self.state in ACTIVE

    def _set(self, state, reason=None):   # sous self.lock
        self.state, self.reason = state, (_reason(reason) if reason else None)
        self.seq += 1
        self.timing[state] = self.mono()
        self.log("playback_state", state=state, reason=self.reason, seq=self.seq)

    # -- API TV
    def start(self, uid, item, require_idle=False):
        """-> (True, None) | (False, code). `item` = élément « Pour toi » validé (jamais une URL de l'appelant).
        require_idle (assistant, play_title) : refus « returning » tant qu'une fin (closed/unavailable) n'est pas
        acquittée par la page /tv — vérifié sous le même verrou que le passage à « loading » (aucune course)."""
        if not isinstance(item, dict) or not isinstance(uid, str):
            return False, "unknown_item"
        if not self.ready.is_set():   # remise à zéro de démarrage en cours : jamais d'ouverture concurrente
            return False, "unavailable"
        if not self._check_gate():   # démotion récente : jamais d'ouverture, même entre deux sondages
            self.ctl_ok = False
            return False, "unavailable"
        if not self.available:
            self.refresh(force=True)
            if not self.available:
                return False, "unavailable"
        with self.lock:
            if self.state in ACTIVE:
                return False, "busy"
            if require_idle and self.state != "idle":
                return False, "returning"
            # « closed »/« unavailable » non acquittés : un nouveau OK vient forcément d'une page /tv visible
            sess = {"sid": secrets.token_urlsafe(18), "uid": uid, "item": item, "cancel": threading.Event(),
                    "why": None}
            self.s, self.uid, self.timing = sess, uid, {}
            self._set("loading")
        threading.Thread(target=self._run, args=(sess,), daemon=True, name="playback").start()
        return True, None

    def close(self, reason="tv"):
        with self.lock:
            s = self.s
            if s is None or self.state not in ("loading", "playing"):
                return self.state
            s["why"] = _reason(reason)
            s["cancel"].set()
            self._set("closing", reason)
            return self.state

    def ack(self, seq):
        with self.lock:
            if self.state in ("closed", "unavailable") and seq == self.seq:
                t = self.timing.get(self.state)
                self.log("playback_home", seq=seq, ack_ms=int((self.mono() - t) * 1000) if t else None)
                self.s = None
                self._set("idle")
                return True
            return False

    def reset(self):
        """Démarrage du service : aucune séance orpheline côté contrôleur (lecteur fermé, profil supprimé). « Regarder »
        n'est disponible qu'APRÈS ce close_all (ready) : il ne peut jamais viser une séance de ce service."""
        try:
            if self.source is not None and self.source.available:
                try:
                    self.client.call({"op": "close_all", "reason": "service"}, timeout=12)
                except PlayerUnavailable:
                    pass
        finally:
            self.ready.set()

    # -- séance (thread)
    def _finish(self, sess, state, reason):
        with self.lock:
            if self.s is not sess:
                return
            self._set(state, reason)

    def _run(self, sess):
        try:
            self._drive(sess)
        except Exception as e:   # jamais d'état pendant : échec propre, contrôleur fermé
            self.log("playback_error", error=type(e).__name__)
            self._close_ctl(sess, "error")
            self._finish(sess, "unavailable", "internal")

    def _close_ctl(self, sess, reason):
        """Fermeture bloquante côté contrôleur ; True seulement si navigateur et profil ont disparu."""
        for _ in range(3):
            try:
                r = self.client.call({"op": "close", "sid": sess["sid"], "reason": _reason(reason)}, timeout=12)
            except PlayerUnavailable:
                continue
            if r.get("gone") is True:
                return True
        return False

    def _drive(self, sess):
        cfg = self.source.cfg
        try:
            url, origin = self.source.detail_url(sess["item"], self.mono() + self.open_budget_s)
        except SourceError as e:
            return self._finish(sess, "unavailable", "source_" + e.code)
        if sess["cancel"].is_set():
            return self._finish(sess, "closed", sess["why"])
        req = {"op": "open", "sid": sess["sid"], "url": url, "origin": origin, "title": sess["item"].get("title") or "",
               "assist_s": cfg.assist_s, "settle_s": cfg.settle_s, "max_s": cfg.max_s}
        del url
        try:
            r = self.client.call(req, timeout=5)
        except PlayerUnavailable:
            r = {"ok": False}
        req = None
        if not r.get("ok"):
            return self._finish(sess, "unavailable", "player_unavailable")
        load_deadline = self.mono() + cfg.assist_s + self.load_margin_s
        errors = 0
        while True:
            if sess["cancel"].is_set():
                gone = self._close_ctl(sess, sess["why"] or "tv")
                return self._finish(sess, "closed" if gone else "unavailable", sess["why"] if gone else "cleanup")
            try:
                st = (self.client.call({"op": "status", "sid": sess["sid"]}, timeout=3) or {}).get("status")
                errors = 0
            except PlayerUnavailable:
                st, errors = None, errors + 1
                if errors >= 5:   # contrôleur perdu : systemd tue son groupe (navigateur compris)
                    return self._finish(sess, "unavailable", "player_lost")
            if isinstance(st, dict):
                if st.get("state") == "revealed":
                    with self.lock:
                        if self.s is sess and self.state == "loading":
                            self._set("playing")
                elif st.get("state") == "ended":
                    if not st.get("gone"):
                        self._close_ctl(sess, "cleanup")
                    why = _reason(st.get("reason") or st.get("outcome"))
                    if st.get("outcome") == "revealed" or why in USER_CLOSE or why == "cap":
                        return self._finish(sess, "closed", why)
                    return self._finish(sess, "unavailable", _reason(st.get("outcome")))
            elif errors == 0 and st is None:   # contrôleur redémarré : séance inconnue
                return self._finish(sess, "unavailable", "player_lost")
            with self.lock:
                loading = self.state == "loading"
            if loading and self.mono() > load_deadline:
                self._close_ctl(sess, "timeout")
                return self._finish(sess, "unavailable", "timeout")
            sess["cancel"].wait(self.poll_s)
