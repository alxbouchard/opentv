"""Télécommande sur téléphone (LAN local, prototype HTTP) : appairage par QR seulement, sessions aléatoires stables et
révocables, commandes de navigation seulement, file bornée vers la page TV.

Priorité produit : 1) télécommande de la TV par HDMI-CEC ; 2) ce téléphone, repli immédiat ; 3) télécommande USB HID.

Sécurité (le serveur LAN est dans web.py ; ici l'état et les règles) :
- appairage : la TV (page loopback réservée au kiosque) affiche un QR `/r?p=<secret aléatoire 256 bits>`. Aucun code
  court, rien à recopier : le secret n'existe qu'en mémoire, n'est jamais écrit sur disque ni journalisé. Il tourne
  toutes les PAIR_TTL_S et aussitôt après chaque appairage réussi (usage unique). Un GET ne fait que lire (n'appaire
  jamais, ne compte aucun essai) ; seul le POST explicite du bouton « Oui, utiliser ce téléphone » appaire. Les
  erreurs sont limitées par adresse IP (bruit seulement : 256 bits ne se devinent pas) ;
- session : jeton 256 bits en cookie HttpOnly, **stable** jusqu'à l'inactivité (SESSION_IDLE_S) ou la révocation :
  aucune rotation, donc ni une réponse perdue ni un refus CSRF ne peuvent déconnecter le vrai téléphone. Seul son
  SHA-256 est enregistré (remote.json, 0600) ; au plus MAX_PHONES téléphones (le moins récemment utilisé est oublié) ;
  « Oublier les téléphones » sur la TV révoque tout ;
- horloge : la Pi n'a pas d'horloge sauvegardée ; avant NTP l'heure peut reculer ou être très en retard. L'inactivité
  n'est jugée qu'entre deux heures synchronisées (TIMESYNC_FLAG) ; une durée négative ou une heure non synchronisée ne
  supprime jamais un téléphone, et « vu » n'est jamais reculé ;
- commandes : ACTIONS seulement (aucune URL, aucun texte libre, aucun shell, ni volume ni alimentation : aucune voie
  vérifiée sur cette TV) ; jeton CSRF par téléphone (HMAC d'un secret local, stable) ; débit borné par téléphone et
  par IP ;
- file vers la TV : au plus QUEUE_MAX événements, chacun périmé après EVENT_TTL_S (jamais rejoué plus tard) ; la
  page TV attend au plus WAIT_MAX_S (longue requête) et au plus WAITERS_MAX attentes simultanées ;
- disque : l'écriture (fsync) se fait hors du verrou de l'état, sérialisée et toujours avec l'état le plus récent.
Rien de secret n'est journalisé (ni jeton, ni secret de QR).
"""
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from .agents import atomic_write_json

ACTIONS = ("up", "down", "left", "right", "ok", "back", "home")
PAIR_SECRET_BYTES = 32           # 256 bits (exigence : au moins 192)
PAIR_TTL_S = 300.0
MAX_PHONES = 4
SESSION_IDLE_S = 30 * 86400.0
SEEN_SAVE_S = 3600.0            # « vu » réécrit sur disque au plus une fois par heure
PAIR_FAIL_PER_IP = 5
PAIR_FAIL_WINDOW_S = 600.0
CMD_BURST, CMD_RATE = 15.0, 10.0    # par téléphone (jetons, jetons/s)
IP_BURST, IP_RATE = 30.0, 20.0      # par adresse IP
TRACKED_MAX = 256
EVENT_TTL_S = 3.0
QUEUE_MAX = 16
WAIT_MAX_S = 8.0
WAITERS_MAX = 2
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[0-9a-f]{8}")
# systemd-timesyncd crée ce fichier quand l'heure est synchronisée (NTP) ; absent (avant NTP, autre démon) = heure non
# fiable : aucune expiration par inactivité, jamais de suppression
TIMESYNC_FLAG = "/run/systemd/timesync/synchronized"


def clock_synced(flag=TIMESYNC_FLAG):
    return os.path.exists(flag)


def _h(tok):
    return hashlib.sha256(tok.encode("ascii")).hexdigest()


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _noop(event, **fields):
    pass


class PhoneRemote:
    def __init__(self, path, wall=time.time, mono=time.monotonic, log=_noop, synced=clock_synced):
        self.path, self.wall, self.mono, self.log, self._synced = path, wall, mono, log, synced
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.save_lock = threading.Lock()   # sérialise les écritures (jamais pris sous self.lock)
        self.gen = self.saved_gen = 0
        self.secret, self.phones = self._load()
        self.pair = None
        self.fail_ip = {}
        self.buckets = collections.OrderedDict()
        self.events = collections.deque(maxlen=QUEUE_MAX)
        self.seq = 0
        self.waiters = 0
        # « Regarder » : pendant une lecture, la page /tv est sous le lecteur ; le service consomme alors les actions
        # (Retour/Accueil ferment la lecture) au lieu de les mettre en file pour une page invisible
        self.interceptor = None

    def __repr__(self):   # jamais de secret dans une trace
        return "<PhoneRemote phones=%d>" % len(self.phones)

    # -- persistance (empreintes seulement)
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.loads(f.read(65536))
            if not isinstance(d, dict) or d.get("v") not in (1, 2) or not HEX64_RE.fullmatch(str(d.get("secret"))):
                raise ValueError("format")
            phones = []
            for p in d.get("phones") or []:
                if not (isinstance(p, dict) and ID_RE.fullmatch(str(p.get("id"))) and HEX64_RE.fullmatch(str(p.get("h")))
                        and _num(p.get("created")) and _num(p.get("seen"))):
                    raise ValueError("phone")
                q = {k: p[k] for k in ("id", "h", "created", "seen")}
                q["ss"] = p.get("ss") is True    # « vu » mesuré sur une heure synchronisée ?
                phones.append(q)
            return d["secret"], phones[-MAX_PHONES:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            # illisible ou altéré : fail-closed, aucun téléphone autorisé (il faudra rescanner le QR)
            self.log("remote_state_reset", reason="invalid")
        return secrets.token_hex(32), []

    def _dirty(self):
        """(sous self.lock) l'état a changé : une écriture est due (faite par _flush, hors verrou)."""
        self.gen += 1

    def _flush(self):
        """Écrit l'état le plus récent si nécessaire. Appelé SANS self.lock : le fsync ne bloque ni les touches, ni
        la longue requête de la TV. save_lock sérialise les écrivains ; chacun prend l'instantané le plus récent."""
        with self.save_lock:
            with self.lock:
                if self.gen == self.saved_gen:
                    return
                gen = self.gen
                snap = {"v": 2, "secret": self.secret, "phones": [dict(p) for p in self.phones]}
            try:
                atomic_write_json(self.path, snap)
                with self.lock:
                    self.saved_gen = max(self.saved_gen, gen)
            except OSError as e:   # mémoire = référence ; une révocation reste effective jusqu'au redémarrage
                self.log("remote_state_error", error=type(e).__name__)

    # -- appairage (QR seulement)
    def _rotate_pair(self):
        self.pair = {"secret": secrets.token_urlsafe(PAIR_SECRET_BYTES), "exp": self.mono() + PAIR_TTL_S}

    def _pair_live(self):
        return self.pair is not None and self.mono() < self.pair["exp"]

    def pairing(self):
        """-> {"secret", "expires_in"} pour le QR de l'écran TV (loopback, kiosque seulement). Expiré ⇒ nouveau."""
        with self.lock:
            if not self._pair_live():
                self._rotate_pair()
            return {"secret": self.pair["secret"], "expires_in": int(round(self.pair["exp"] - self.mono()))}

    def check_pair_secret(self, s):
        """GET /r?p=… : lecture seule (n'appaire pas, ne compte pas d'essai, ne fait rien tourner)."""
        if not isinstance(s, str) or not SECRET_RE.fullmatch(s):
            return False
        with self.lock:
            return self._pair_live() and hmac.compare_digest(s, self.pair["secret"])

    def _fails(self, ip, now):
        win = [t for t in self.fail_ip.get(ip, ()) if now - PAIR_FAIL_WINDOW_S < t <= now]
        if win:
            self.fail_ip[ip] = win
        else:
            self.fail_ip.pop(ip, None)
        return win

    def pair_limited(self, ip):
        with self.lock:
            return len(self._fails(ip, self.mono())) >= PAIR_FAIL_PER_IP

    def pair_phone(self, ip, secret):
        """POST explicite d'appairage -> (jeton de session, None) ou (None, "limited"|"denied")."""
        with self.lock:
            now = self.mono()
            if len(self._fails(ip, now)) >= PAIR_FAIL_PER_IP:
                return None, "limited"
            ok = self._pair_live() and isinstance(secret, str) and bool(SECRET_RE.fullmatch(secret)) and \
                hmac.compare_digest(secret, self.pair["secret"])
            if not ok:
                self.fail_ip.setdefault(ip, []).append(now)
                if len(self.fail_ip) > TRACKED_MAX:
                    for k in sorted(self.fail_ip, key=lambda k: self.fail_ip[k][-1])[:len(self.fail_ip) - TRACKED_MAX]:
                        del self.fail_ip[k]
                return None, "denied"
            tok, w = secrets.token_urlsafe(32), self.wall()
            self.phones.append({"id": secrets.token_hex(4), "h": _h(tok), "created": w, "seen": w,
                                "ss": bool(self._synced())})
            del self.phones[:-MAX_PHONES]    # le moins récemment utilisé (en tête) est oublié
            self._rotate_pair()              # usage unique : le QR affiché change aussitôt
            self._dirty()
            n = len(self.phones)
        self._flush()
        self.log("remote_paired", phones=n)
        return tok, None

    # -- sessions (jeton stable)
    def session(self, tok):
        """Cookie -> téléphone (copie) ou None si inconnu/inactif depuis plus de SESSION_IDLE_S.

        L'inactivité n'est jugée que sur deux heures **synchronisées** (NTP) : la date « vu » enregistrée et l'heure
        courante. Avant NTP (heure possiblement reculée ou très en retard), aucun téléphone n'est jamais oublié et
        « vu » n'est jamais reculé ; au premier usage synchronisé, « vu » devient l'heure vraie."""
        if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
            return None
        h, w, synced = _h(tok), self.wall(), self._synced()
        found = expired = None
        with self.lock:
            for p in self.phones:
                if not hmac.compare_digest(p["h"], h):
                    continue
                idle = w - p["seen"]
                if synced and p["ss"] and idle > SESSION_IDLE_S:
                    self.phones.remove(p)
                    self._dirty()
                    expired = len(self.phones)
                    break
                if synced and not p["ss"]:       # première heure fiable : référence corrigée
                    p["seen"], p["ss"] = w, True
                    self._dirty()
                elif idle > 0:                   # jamais reculé
                    if idle >= SEEN_SAVE_S:
                        self._dirty()
                    p["seen"], p["ss"] = w, synced
                self.phones.remove(p)
                self.phones.append(p)            # ordre = moins récemment utilisé d'abord
                found = dict(p)
                break
        self._flush()
        if expired is not None:
            self.log("remote_phone_expired", phones=expired)
        return found

    def csrf(self, phone):
        return hmac.new(bytes.fromhex(self.secret), ("csrf:" + phone["id"]).encode("ascii"),
                        hashlib.sha256).hexdigest()[:32]

    def check_csrf(self, phone, value):
        return isinstance(value, str) and len(value) == 32 and hmac.compare_digest(value, self.csrf(phone))

    def forget(self, phone_id):
        with self.lock:
            n = len(self.phones)
            self.phones = [p for p in self.phones if p["id"] != phone_id]
            changed = len(self.phones) != n
            if changed:
                self._dirty()
            left = len(self.phones)
        self._flush()
        if changed:
            self.log("remote_phone_forgotten", phones=left)

    def revoke_all(self):
        with self.lock:
            self.phones = []
            self.secret = secrets.token_hex(32)   # anciens jetons CSRF invalides aussi
            self._rotate_pair()
            self._dirty()
            self.cond.notify_all()
        self._flush()
        self.log("remote_revoked")

    def count(self):
        with self.lock:
            return len(self.phones)

    # -- commandes
    def _allow(self, key, burst, rate, now):
        tokens, at = self.buckets.pop(key, (burst, now))
        tokens = min(burst, tokens + max(0.0, now - at) * rate)
        ok = tokens >= 1.0
        self.buckets[key] = (tokens - 1.0 if ok else tokens, now)
        while len(self.buckets) > TRACKED_MAX:
            self.buckets.popitem(last=False)
        return ok

    def command(self, phone, ip, action):
        """-> "ok" | "bad_action" | "limited". L'action est mise en file pour la page TV."""
        if action not in ACTIONS:
            return "bad_action"
        with self.cond:
            now = self.mono()
            if not (self._allow("ip:" + str(ip), IP_BURST, IP_RATE, now) and
                    self._allow("p:" + phone["id"], CMD_BURST, CMD_RATE, now)):
                return "limited"
        if self.interceptor is not None and self.interceptor(action):
            return "ok"
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, action, now))
            self.cond.notify_all()
        return "ok"

    def wait_events(self, after, timeout=WAIT_MAX_S):
        """Longue requête de la page TV -> {"seq", "events": [{"seq","a"}], "idle"?, "busy"?}.

        after < 0 : synchronisation (aucun ancien événement rejoué) ; aucun téléphone : réponse immédiate « idle »
        (la page revient dans quelques secondes) ; after > seq (service redémarré) : resynchronisation immédiate."""
        with self.cond:
            if after < 0 or after > self.seq:
                return {"seq": self.seq, "events": []}
            if not self.phones:
                return {"seq": self.seq, "events": [], "idle": True}
            if self.waiters >= WAITERS_MAX:
                return {"seq": self.seq, "events": [], "busy": True}
            self.waiters += 1
            try:
                end = time.monotonic() + max(0.0, min(float(timeout), WAIT_MAX_S))
                while True:
                    now = self.mono()
                    evs = [{"seq": s, "a": a} for s, a, t in self.events if s > after and now - t <= EVENT_TTL_S]
                    rem = end - time.monotonic()
                    if evs or rem <= 0 or not self.phones:
                        return {"seq": self.seq, "events": evs}
                    self.cond.wait(rem)
            finally:
                self.waiters -= 1

    def wake(self):
        with self.cond:
            self.cond.notify_all()
