"""File typée service -> page TV pour le pilotage par un assistant (scope « control »).

Chaque action porte un numéro `seq` strictement croissant (par démarrage du service ; la clé de page /tv change aussi à
chaque démarrage, donc une page ne voit jamais deux séries). Cycle de vie, une seule fois par action :

    queued  --(page /tv : claim)-->  claimed  --(page /tv : done ok)-->  applied
       |                                 \\--(done ko / perdue)------->  failed
       \\--(non réclamée à temps / remplacée)--------------------------->  expired | failed(superseded)

« claim » est atomique côté service : une page rechargée, deux onglets ou un POST rejoué ne peuvent jamais appliquer
la même action deux fois. Le résultat donné à l'agent est cet état AUTORITAIRE (accepté, réclamé, appliqué par la
page), jamais une affirmation sur les pixels réellement visibles sur la TV (porte terrain HDMI séparée).

Types : present (fiche d'un film accepté + phrase courte), play (fiche du film lancé par le lecteur officiel, déjà
démarré côté service par Playback.start avec ses portes), key (touche de navigation d'une énumération fermée).
Aucune URL n'entre dans la file ; seule l'affiche https approuvée du catalogue accompagne une fiche, et elle ne sort
que vers la page TV locale (jamais vers un agent : public()).
"""
import collections
import re
import threading
import time

KINDS = ("present", "play", "key")
STATES = ("queued", "claimed", "applied", "failed", "expired")
NAV_KEYS = ("up", "down", "left", "right", "ok", "back", "home")
CLAIM_TTL_S = 20.0      # action non réclamée par la page /tv : expirée (page absente, kiosque arrêté…)
APPLY_TTL_S = 15.0      # action réclamée sans résultat (page rechargée entre les deux) : échec « lost »
PENDING_MAX = 6         # actions en attente au plus (au-delà : refus « tv_queue_full », jamais d'accumulation)
HISTORY_MAX = 24
REASON_RE = re.compile(r"[a-z][a-z0-9_]{1,31}")


def _reason(r):
    return r if isinstance(r, str) and REASON_RE.fullmatch(r) else "other"


class QueueFull(Exception):
    pass


class TvActions:
    def __init__(self, mono=time.monotonic, log=None):
        self.mono = mono
        self.log = log or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.seq = 0
        self.items = collections.OrderedDict()   # seq -> action (bornée à HISTORY_MAX)

    # -- côté service (agents)
    def _expire(self, now):   # sous self.lock
        for a in self.items.values():
            if a["state"] == "queued" and now - a["at"] >= CLAIM_TTL_S:
                self._end(a, "expired", "not_claimed")
            elif a["state"] == "claimed" and now - a["claimed_at"] >= APPLY_TTL_S:
                self._end(a, "failed", "lost")

    def _end(self, a, state, reason):   # sous self.lock
        a["state"], a["reason"], a["ended_at"] = state, _reason(reason) if reason else None, self.mono()
        self.log("tv_action", seq=a["seq"], kind=a["kind"], state=state, reason=a["reason"])

    def push(self, kind, agent, payload):
        """-> vue publique de l'action mise en file. Une nouvelle fiche (present/play) remplace une fiche ou une touche
        encore en attente (jamais une action déjà réclamée) : la TV n'enchaîne jamais des propositions périmées."""
        if kind not in KINDS:
            raise ValueError("bad_kind")
        with self.lock:
            now = self.mono()
            self._expire(now)
            if kind in ("present", "play"):
                for a in self.items.values():
                    if a["state"] == "queued":
                        self._end(a, "failed", "superseded")
            if sum(1 for a in self.items.values() if a["state"] in ("queued", "claimed")) >= PENDING_MAX:
                raise QueueFull()
            self.seq += 1
            a = {"seq": self.seq, "kind": kind, "from": agent, "state": "queued", "reason": None, "at": now,
                 "claimed_at": None, "ended_at": None, "payload": dict(payload)}
            self.items[self.seq] = a
            while len(self.items) > HISTORY_MAX:
                old = next(iter(self.items.values()))
                if old["state"] in ("queued", "claimed"):
                    break
                self.items.popitem(last=False)
            self.log("tv_action", seq=a["seq"], kind=kind, state="queued", reason=None)
            return self._public(a)

    @staticmethod
    def _public(a):
        """Vue agent : jamais l'affiche ni l'élément du catalogue, seulement l'état autoritaire et l'identifiant."""
        out = {"seq": a["seq"], "kind": a["kind"], "state": a["state"], "reason": a["reason"]}
        p = a["payload"]
        if "id" in p:
            out["id"] = p["id"]
        if "key" in p:
            out["key"] = p["key"]
        return out

    def get(self, seq):
        with self.lock:
            self._expire(self.mono())
            a = self.items.get(seq)
            return self._public(a) if a else None

    def recent(self, n=6):
        with self.lock:
            self._expire(self.mono())
            return [self._public(a) for a in list(self.items.values())[-n:]]

    def cancel_queued(self, reason):
        """Configuration, gestion des assistants : toute action non réclamée est abandonnée (jamais appliquée)."""
        with self.lock:
            for a in self.items.values():
                if a["state"] == "queued":
                    self._end(a, "failed", reason)

    # -- côté page /tv (loopback, clé de page)
    def pending(self):
        """Actions en attente pour la page TV (avec leur contenu)."""
        with self.lock:
            now = self.mono()
            self._expire(now)
            return [self._tv(a, now) for a in self.items.values() if a["state"] == "queued"]

    @staticmethod
    def _tv(a, now):
        """Vue page TV : contenu + âge (ms) — la page compare l'heure de mise en file à la dernière touche humaine."""
        return dict(a["payload"], seq=a["seq"], kind=a["kind"], age_ms=int(max(0.0, now - a["at"]) * 1000),
                    **{"from": a["from"]})

    def claim(self, seq):
        """queued -> claimed, une seule fois. -> contenu de l'action, ou None (inconnue, déjà prise, expirée)."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            return None
        with self.lock:
            self._expire(self.mono())
            a = self.items.get(seq)
            if a is None or a["state"] != "queued":
                return None
            a["state"], a["claimed_at"] = "claimed", self.mono()
            return self._tv(a, a["claimed_at"])

    def done(self, seq, ok, reason=None):
        """claimed -> applied | failed. -> True si l'état a changé."""
        if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(ok, bool):
            return False
        with self.lock:
            self._expire(self.mono())
            a = self.items.get(seq)
            if a is None or a["state"] != "claimed":
                return False
            self._end(a, "applied" if ok else "failed", None if ok else (reason or "refused"))
            return True
