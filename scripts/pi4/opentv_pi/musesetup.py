"""Connexion de Muse depuis la TV (service OpenTV, compte opentv, sans privilège) : séance courte + jobs root dédiés.

Parcours : Réglages → Gérer les assistants → Muse → « Autoriser et connecter » (consentement EXPLICITE du
propriétaire, sur la TV, page loopback du kiosque) ⇒ une séance à usage unique :
  - identifiant `s` (128 bits, dans le QR et l'URL), secret `f` (128 bits, FRAGMENT du QR : jamais envoyé en clair),
    paire RSA-2048 neuve (museseal, mémoire seulement), clé CSRF propre ; échéance SESSION_TTL_S (≤ 10 min) pour
    recevoir le jeton ; états strictement croissants (STATES/RANK), finaux immuables ;
  - le téléphone envoie {s, c, ct} : ct = RSA-OAEP({v, s, f, t}) ; contrôle (s, f, forme du jeton) puis UN job
    « pair » 0600 dans musejobs (même système de fichiers, rename atomique), consommé par opentv-muse-setup.service
    (root, musehelper) qui appelle le `muse pair` existant ; le jeton n'est jamais renvoyé, journalisé, audité ni écrit
    ailleurs que dans ce job (dossier 0700 du service, supprimé par le helper AVANT d'agir, retiré ici à l'annulation) ;
  - progression lue dans museresults (préparation → en attente de Muse → connexion → connectée), échéances bornées.
QR : adresse de la boîte par son NOM mDNS vérifié seulement (`<nom>.local`, même vérification que la télécommande
téléphone : il doit se résoudre exactement vers l'adresse LAN liée). Jamais d'adresse IP dans le QR, l'état TV ou une
page : nom absent, en conflit ou non vérifié à temps ⇒ séance fermée (« name_unavailable »), message de reprise, aucun
QR.
Deux durées distinctes : SESSION_TTL_S (≤ 10 min) borne l'ACCEPTATION du jeton (QR ouvert) ; après l'envoi, le suivi
de l'avancement (téléphone /muse/status, TV) dure jusqu'à l'issue du helper, borné par PAIR_BUDGET_S (+ LINGER_S pour
lire l'issue) — aucun autre envoi n'est accepté pendant ce suivi.
Annuler : avant prise en charge, le job (et donc le jeton) est retiré ; après, un job « cancel » ciblé demande au helper
de fermer la fenêtre Bluetooth qu'IL a ouverte (jamais de révocation d'un Muse déjà apparié). Seule l'issue du helper
dit « annulé » ; sans issue après CANCEL_WAIT_S, la TV dit « Annulation demandée » (jamais « rien n'a changé ») et le
suivi continue jusqu'à l'issue réelle ; au-delà de l'échéance globale : « cancel_unconfirmed », puis une issue
tardive du helper est encore annoncée telle quelle (réconciliation). Issues vraies et monotones, en direct comme en
réconciliation : « annulé, rien n'a changé » seulement si le helper prouve l'état d'avant rendu (cancelled /
interrupted) ; appariement réussi avant un plantage ⇒ « paired_incomplete » (« Muse est appairée, mais la connexion
doit être reprise » : jamais annulé, jamais connectée sans pont prouvé ; Reconnecter / Déconnecter proposés) ; reprise
partielle ⇒ « recovery_incomplete ». « Déconnecter Muse » : job « unpair » seulement
après confirmation explicite sur la TV, proposé quand la boîte possède un appariement, un jeton ou SA propre identité
(état public publié par le helper dans box.json, sans secret), jamais pour une identité étrangère.
Le compte opentv peut déjà écrire la politique des assistants (« Gérer les assistants ») : le job « pair » ne lui
donne aucun pouvoir nouveau ; il ne contient que des champs fixes (aucun chemin, aucune commande, aucun argument).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import museseal

SESSION_TTL_S = 600.0           # QR → jeton reçu (≤ 10 min)
PICKUP_S = 60.0                 # job « pair » non pris par le helper ⇒ échec honnête (helper indisponible)
PAIR_BUDGET_S = 20 * 60.0       # jeton reçu → issue finale (fenêtre Bluetooth 10 min + arrêts/démarrages bornés)
CANCEL_WAIT_S = 120.0           # annulation demandée au helper : issue attendue au plus
LINGER_S = 90.0                 # après l'issue : la page du téléphone peut encore lire l'état final
DISCONNECT_BUDGET_S = 180.0
HOST_WAIT_S = 10.0              # nom mDNS pas encore vérifié à l'ouverture : au plus, puis séance fermée (aucun QR)
PROBE_MIN_S = 15.0              # relecture de l'état public de la boîte : au plus une demande par intervalle
BOX_NAME = "box.json"           # état public de la boîte, écrit par le helper root dans museresults (aucun secret)
BOX_MAX_BYTES = 1024
MAX_ATTEMPTS = 5                # envois refusés (déchiffrement, forme) avant fermeture de la séance
JOB_MAX_BYTES = 2048
SID_RE = re.compile(r"[A-Za-z0-9_-]{22}")
JOB_ID_RE = re.compile(r"[a-z0-9]{16}")
SDK_TOKEN_RE = re.compile(r"mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")   # = museadmin.SDK_TOKEN_RE (testé)
BLE_RE = re.compile(r"MuseGadget[0-9A-F]{6}")
CT_MAX = 400                    # base64url d'un chiffré RSA-2048 : 342 caractères
PROGRESS = ("preparing", "waiting_muse", "connecting")
FINAL = ("connected", "failed", "cancelled", "expired")
STATES = ("keygen", "await_token", "submitted") + PROGRESS + FINAL
RANK = {"keygen": 0, "await_token": 1, "submitted": 2, "preparing": 3, "waiting_muse": 4, "connecting": 5}
RANK.update({k: 9 for k in FINAL})
RESULT_CODES = ("connected", "already_paired", "cancelled", "invalid_sdk_token", "pair_timeout", "window_closed",
                "pair_window_not_opened", "foreign_identity", "bluetooth_not_ready", "muse_unavailable",
                "registry_unavailable", "bridge_not_started", "helper_error", "helper_unavailable", "timeout",
                "too_many_attempts", "disconnected", "disconnect_failed", "interrupted", "busy", "refused",
                "different_token", "paired_incomplete", "recovery_incomplete")
MESSAGES = {
    "connected": "Muse est connectée.",
    "cancelled": "Annulé : rien n'a changé.",
    "expired": "QR expiré : rien n'a changé.",
    "invalid_sdk_token": ("Muse a refusé ce jeton. Vérifie que c'est le jeton complet, le même que pour tes autres "
                          "appareils, puis recommence."),
    "different_token": ("Ce jeton n'est pas celui déjà enregistré sur cette boîte : rien n'a changé. Colle le même "
                        "jeton que pour tes autres appareils Muse, puis recommence. S'il a changé chez Muse, choisis "
                        "d'abord « Déconnecter Muse » ici."),
    "name_unavailable": ("La TV n'est pas joignable par son nom sur ton Wi-Fi : rien n'a changé, aucun QR affiché. "
                         "Redémarre la boîte, puis recommence."),
    "cancel_unconfirmed": ("Annulation demandée, mais la boîte n'a pas encore confirmé l'issue. Ouvre Réglages → "
                           "Gérer les assistants → Muse pour voir l'état."),
    "pair_timeout": "L'app Muse n'a pas trouvé OpenTV à temps. Recommence, iPhone près de la TV.",
    "window_closed": "L'app Muse n'a pas trouvé OpenTV à temps. Recommence, iPhone près de la TV.",
    "pair_window_not_opened": "Le Bluetooth de la boîte n'a pas démarré. Redémarre la boîte, puis recommence.",
    "foreign_identity": ("Une autre installation de Muse existe déjà sur cette boîte : rien n'a été modifié. "
                         "Demande de l'aide à la personne qui a installé OpenTV."),
    "bluetooth_not_ready": "Le Bluetooth de la boîte n'est pas prêt. Redémarre la boîte, puis recommence.",
    "muse_unavailable": "Muse n'est pas disponible sur cette boîte : son installation est incomplète.",
    "registry_unavailable": "Les assistants de la boîte sont illisibles : rien n'a été changé.",
    "bridge_not_started": "Muse est appairée mais ne démarre pas. Redémarre la boîte, puis réessaie.",
    # plantage du helper APRÈS l'appariement (pont pas prouvé connecté) : appariement et pilotage gardés
    "paired_incomplete": ("Muse est appairée, mais la connexion doit être reprise : choisis « Reconnecter » dans "
                          "Réglages → Gérer les assistants → Muse."),
    # reprise après plantage : état d'avant entièrement rendu (prouvé par le helper)
    "interrupted": "La connexion a été interrompue : rien n'a changé. Recommence depuis la TV.",
    "recovery_incomplete": ("La connexion a été interrompue et la boîte n'a pas pu tout remettre comme avant. Ouvre "
                            "Réglages → Gérer les assistants → Muse pour vérifier."),
    "helper_unavailable": "La boîte n'a pas répondu. Redémarre-la, puis recommence.",
    "timeout": "La connexion a pris trop de temps. Recommence depuis la TV.",
    "too_many_attempts": "Trop d'essais : recommence depuis la TV.",
    "disconnected": "Muse est déconnectée.",
    "disconnect_failed": "Muse n'a pas pu être déconnectée. Redémarre la boîte, puis réessaie.",
}
DEFAULT_MESSAGE = "La connexion n'a pas abouti. Recommence depuis la TV."
LABELS = {"keygen": "Préparation…", "await_token": "En attente du téléphone", "submitted": "Préparation",
          "preparing": "Préparation", "waiting_muse": "En attente de Muse", "connecting": "Connexion",
          "connected": "Muse est connectée", "failed": "Échec", "cancelled": "Annulé", "expired": "Expiré"}
LABEL_CANCELLING = "Annulation en cours"
LABEL_CANCEL_PENDING = "Annulation demandée"


def message_for(code):
    return MESSAGES.get(code, DEFAULT_MESSAGE)


def _noop(event, **fields):
    pass


def new_job_id():
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16))


def write_job(jobs_dir, tmp_dir, body):
    """Job 0600 du service : temporaire exclusif sans lien suivi, fsync, rename atomique dans le dossier surveillé.
    Le contenu n'est jamais journalisé ; l'appelant efface sa copie aussitôt."""
    jid = body["id"]
    raw = json.dumps(body, sort_keys=True, ensure_ascii=True).encode("ascii")
    if len(raw) > JOB_MAX_BYTES:
        raise ValueError("too_big")
    tmp = os.path.join(tmp_dir, jid + ".json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        try:
            os.write(fd, raw)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, os.path.join(jobs_dir, jid + ".json"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        raw = None
    return jid


def withdraw_job(jobs_dir, jid):
    """Job encore en file ⇒ retiré (le jeton qu'il porte disparaît avec lui). -> True si retiré."""
    if not JOB_ID_RE.fullmatch(jid or ""):
        return False
    try:
        os.unlink(os.path.join(jobs_dir, jid + ".json"))
        return True
    except OSError:
        return False


def read_results(results_dir, cap=32):
    """Progression déposée par le helper root (aucun secret) ; chaque fichier est lu une fois puis retiré."""
    out = []
    try:
        names = sorted(n for n in os.listdir(results_dir) if re.fullmatch(r"[a-z0-9]{16}\.json", n))
    except OSError:
        return out
    for n in names[:cap]:
        p = os.path.join(results_dir, n)
        r = None
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                if os.fstat(fd).st_size <= JOB_MAX_BYTES:
                    r = json.loads(os.read(fd, JOB_MAX_BYTES).decode("utf-8"))
            finally:
                os.close(fd)
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            r = None
        try:
            os.unlink(p)
        except OSError:
            pass
        if not isinstance(r, dict) or r.get("v") != 1 or r.get("id") != n[:-5]:
            continue
        phase = r.get("phase")
        if phase not in PROGRESS + ("done",):
            continue
        item = {"id": r["id"], "phase": phase, "ok": r.get("ok") is True,
                "result": r.get("result") if r.get("result") in RESULT_CODES else None}
        if isinstance(r.get("ble_name"), str) and BLE_RE.fullmatch(r["ble_name"]):
            item["ble_name"] = r["ble_name"]
        if isinstance(r.get("registered"), bool):
            item["registered"] = r["registered"]
        out.append(item)
    return out


def read_box(results_dir):
    """État public de la boîte (box.json, écrit par le helper root ; jamais retiré ici) -> dict validé ou None.
    Aucun secret : propriété (owned/foreign) et présences seulement ; tout fichier anormal ⇒ None (rien proposé)."""
    try:
        fd = os.open(os.path.join(results_dir, BOX_NAME), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_nlink != 1 or st.st_size > BOX_MAX_BYTES:
            return None
        d = json.loads(os.read(fd, BOX_MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    finally:
        os.close(fd)
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    tri = lambda x: x if isinstance(x, bool) else None   # noqa: E731
    return {"owned": tri(d.get("owned")), "foreign": d.get("foreign") is True,
            "identity_present": tri(d.get("identity_present"))}


class _Session:
    __slots__ = ("sid", "f", "csrf_key", "key", "created", "expires", "state", "error", "job", "attempts",
                 "phone_seen", "ble_name", "cancel_job", "deadline", "ended_at", "registered", "cancel_requested",
                 "pair_deadline", "cancel_pending", "nohost_since")

    def __repr__(self):   # aucun secret dans une trace
        return "<MuseSession state=%s>" % self.state


class MuseSetup:
    def __init__(self, cfg, mono=time.monotonic, clock=time.time, log=_noop, keygen=None, async_keygen=True):
        self.cfg, self.mono, self.clock, self.log = cfg, mono, clock, log
        self.keygen = keygen or (lambda: museseal.PrivateKey())
        self.async_keygen = async_keygen
        self.lock = threading.RLock()
        self.cur = None
        self.seq = 0                       # change à chaque transition (TV : annonce d'une issue plus récente)
        self.last = {"seq": 0, "state": None, "error": None, "message": None}
        self.disc = None                   # {"job", "state": running|done|failed, "deadline", "error"}
        self.disc_last = {"seq": 0, "state": None}
        self.bad = {}                      # ip -> horodatages (identifiants de séance faux)
        self.reconcile = None              # job « pair » terminé sans issue confirmée : issue tardive encore annoncée
        self.reconcile_cancel = False      # … après une annulation demandée (sinon : délai dépassé)
        self.box = None                    # état public de la boîte (helper root), relu à chaque tick
        self.probe_at = -1e18

    # -- petits outils
    def _bump(self):
        self.seq += 1
        return self.seq

    def _set(self, s, state, error=None):
        """Transition monotone : rang strictement croissant ; un état final est immuable."""
        if s.state in FINAL or RANK[state] <= RANK[s.state]:
            return False
        s.state = state
        if error is not None:
            s.error = error
        self._bump()
        if state in FINAL:
            s.ended_at = self.mono()
            self._scrub(s)
            self.last = {"seq": self.seq, "state": state, "error": s.error, "message": self._final_message(s)}
            self.log("muse_setup", state=state, error=s.error)
        else:
            self.log("muse_setup", state=state)
        return True

    @staticmethod
    def _scrub(s):
        if s.key is not None:
            s.key.wipe()
        s.key = None
        s.f = None

    def _live(self, sid):
        """Séance courante pour un identifiant donné (comparaison en temps constant)."""
        s = self.cur
        if s is None or not isinstance(sid, str) or not SID_RE.fullmatch(sid):
            return None
        if not hmac.compare_digest(sid.encode("ascii"), s.sid.encode("ascii")):
            return None
        return s

    def bad_limited(self, ip, record=False, per=30, window=60.0):
        now, ip = self.mono(), ip if isinstance(ip, str) else "?"
        with self.lock:
            w = [t for t in self.bad.get(ip, ()) if t > now - window]
            if record:
                w.append(now)
            if w:
                self.bad[ip] = w[-per:]
            else:
                self.bad.pop(ip, None)
            while len(self.bad) > 256:
                del self.bad[min(self.bad, key=lambda k: self.bad[k][-1])]
            return len(w) >= per

    # -- TV (loopback, kiosque) : consentement explicite
    def start(self, consent):
        """« Autoriser et connecter » confirmé sur la TV. consent doit valoir exactement True. -> (ok, erreur)."""
        if consent is not True:
            return False, "consent_required"
        with self.lock:
            if self.disc is not None and self.disc["state"] == "running":
                return False, "busy"
            old = self.cur
            if old is not None and old.state not in FINAL:
                if RANK[old.state] >= RANK["submitted"]:
                    return False, "busy"       # appariement déjà en cours : jamais deux fenêtres Bluetooth
                self._set(old, "cancelled", "cancelled")
            s = _Session()
            s.sid, s.f = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
            s.csrf_key = secrets.token_bytes(32)
            s.key, s.created = None, self.mono()
            s.expires = s.created + SESSION_TTL_S
            s.state, s.error, s.job, s.attempts = "keygen", None, None, 0
            s.phone_seen, s.ble_name, s.cancel_job, s.deadline = False, None, None, None
            s.ended_at, s.registered, s.cancel_requested = None, None, False
            s.pair_deadline, s.cancel_pending, s.nohost_since = None, False, None
            self.cur = s
            self.reconcile = None
            self.reconcile_cancel = False
            self._bump()
            self.log("muse_setup", state="open")
        if self.async_keygen:
            threading.Thread(target=self._gen, args=(s,), daemon=True, name="muse-keygen").start()
        else:
            self._gen(s)
        return True, None

    def _gen(self, s):
        try:
            key = self.keygen()
        except Exception as e:   # noqa: BLE001 — jamais de séance bloquée sur « Préparation… »
            self.log("muse_setup_keygen_error", error=type(e).__name__)
            key = None
        with self.lock:
            if self.cur is not s or s.state != "keygen":
                if key is not None:
                    key.wipe()
                return
            if key is None:
                self._set(s, "failed", "helper_error")
                return
            s.key = key
            self._set(s, "await_token")

    def cancel(self, origin, sid=None):
        """Annulation (TV ou téléphone). Avant prise en charge par le helper : job (et jeton) retirés ⇒ annulé tout
        de suite. Après : job « cancel » ciblé ; l'issue vient du helper (fenêtre fermée) ou de l'échéance.
        sid (téléphone) : seule la séance de CE téléphone peut être annulée."""
        with self.lock:
            s = self.cur if sid is None else self._live(sid)
            if s is None or s.state in FINAL:
                return False
            if RANK[s.state] < RANK["submitted"]:
                self._set(s, "cancelled", "cancelled")
                self.log("muse_setup_cancel", origin=origin)
                return True
            if s.job and withdraw_job(self.cfg.muse_jobs_dir, s.job):
                self._set(s, "cancelled", "cancelled")
                self.log("muse_setup_cancel", origin=origin, withdrawn=True)
                return True
            if not s.cancel_requested:
                s.cancel_requested = True
                try:
                    s.cancel_job = write_job(self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp,
                                             {"v": 1, "id": new_job_id(), "action": "cancel", "target": s.job,
                                              "created": round(self.clock(), 3)})
                except (OSError, ValueError) as e:
                    self.log("muse_setup_cancel_error", error=type(e).__name__)
                s.deadline = min(s.deadline or 1e18, self.mono() + CANCEL_WAIT_S)
                self._bump()
                self.log("muse_setup_cancel", origin=origin, withdrawn=False)
            return True

    # -- nom mDNS vérifié (seule adresse du QR) : jamais d'adresse IP en repli
    def host_check(self, ok, refuted):
        """(boucle du service) ok = le nom mDNS désigne exactement l'adresse liée ; refuted = une vérification faite
        APRÈS l'ouverture l'a démenti (absent, conflit, ambigu). Avant l'envoi du jeton seulement : démenti, ou
        nom pas vérifié pendant HOST_WAIT_S ⇒ séance fermée (« name_unavailable »), aucun QR montré."""
        with self.lock:
            s = self.cur
            if s is None or s.state not in ("keygen", "await_token"):
                return
            if ok:
                s.nohost_since = None
                return
            now = self.mono()
            if s.nohost_since is None:
                s.nohost_since = now
            if refuted or now - s.nohost_since >= HOST_WAIT_S:
                self._set(s, "failed", "name_unavailable")

    # -- état public de la boîte (« Déconnecter Muse » proposé seulement si la boîte possède Muse)
    def probe(self, origin="tv"):
        """Demande au helper root de republier box.json (aucune action). Bornée : au plus une par PROBE_MIN_S."""
        with self.lock:
            if self.mono() - self.probe_at < PROBE_MIN_S:
                return False
            self.probe_at = self.mono()
            try:
                write_job(self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp,
                          {"v": 1, "id": new_job_id(), "action": "probe", "created": round(self.clock(), 3)})
            except (OSError, ValueError) as e:
                self.log("muse_probe_error", error=type(e).__name__)
                return False
            self.log("muse_probe", origin=origin)
            return True

    def box_view(self, identity_present=None):
        """-> {"owned": True|False|None, "foreign": bool}. identity_present (registre lu par le service) différent
        de celui de l'instantané du helper ⇒ instantané périmé : rien proposé (None) jusqu'à la relecture."""
        with self.lock:
            b = self.box
        if b is None:
            return {"owned": None, "foreign": False}
        stale = isinstance(identity_present, bool) and isinstance(b["identity_present"], bool) and \
            identity_present != b["identity_present"]
        if stale:
            return {"owned": None, "foreign": False}
        return {"owned": b["owned"], "foreign": b["foreign"]}

    def disconnect(self, confirm, owned=None):
        """« Déconnecter Muse » confirmé sur la TV (confirm doit valoir exactement True) et seulement si la boîte
        possède Muse (owned exactement True : appariement, jeton ou identité à elle ; jamais une identité étrangère).
        -> (ok, erreur)."""
        if confirm is not True:
            return False, "confirmation_required"
        if owned is not True:
            return False, "not_owned"
        with self.lock:
            if self.cur is not None and self.cur.state not in FINAL:
                return False, "busy"
            if self.disc is not None and self.disc["state"] == "running":
                return False, "busy"
            try:
                jid = write_job(self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp,
                                {"v": 1, "id": new_job_id(), "action": "unpair", "confirm": "tv",
                                 "created": round(self.clock(), 3)})
            except (OSError, ValueError) as e:
                self.log("muse_disconnect_error", error=type(e).__name__)
                return False, "unavailable"
            self.disc = {"job": jid, "state": "running", "deadline": self.mono() + DISCONNECT_BUDGET_S,
                         "error": None}
            self._bump()
            self.log("muse_disconnect", state="queued")
            return True, None

    # -- téléphone (LAN)
    def csrf(self, s):
        return hmac.new(s.csrf_key, b"muse-csrf:" + s.sid.encode("ascii"), hashlib.sha256).hexdigest()[:32]

    def page(self, sid):
        """GET /muse : -> vue publique de la séance (jamais de secret privé) ou None. Ne consomme rien."""
        with self.lock:
            self._expire()
            s = self._live(sid)
            if s is None or (s.state in FINAL and self.mono() - (s.ended_at or 0) > LINGER_S):
                return None
            if not s.phone_seen and s.state in ("keygen", "await_token"):
                s.phone_seen = True
                self._bump()
            out = {"state": s.state, "csrf": self.csrf(s), "error": s.error, "ble_name": s.ble_name,
                   "jwk": s.key.public_jwk() if (s.key is not None and s.state == "await_token") else None,
                   "expires_in": max(0, int(s.expires - self.mono())) if s.state == "await_token" else None,
                   "label": self._label(s), "message": self._final_message(s)}
            return out

    def status(self, sid):
        v = self.page(sid)
        if v is None:
            return None
        return {"state": v["state"], "label": v["label"], "message": v["message"], "ble_name": v["ble_name"]}

    def check_csrf(self, sid, c):
        with self.lock:
            s = self._live(sid)
            return s is not None and isinstance(c, str) and hmac.compare_digest(
                c.encode("utf-8", "replace"), self.csrf(s).encode("ascii"))

    def submit(self, sid, c, ct):
        """POST /muse/submit -> (code HTTP, corps JSON). Le corps ne contient JAMAIS le jeton ni le clair."""
        with self.lock:
            self._expire()
            s = self._live(sid)
            if s is None or s.state in FINAL:
                return 403, {"ok": False, "error": "expired"}
            if not isinstance(c, str) or not hmac.compare_digest(c.encode("utf-8", "replace"),
                                                                 self.csrf(s).encode("ascii")):
                return 403, {"ok": False, "error": "refused"}
            if s.state != "await_token":
                # double envoi (double toucher, rechargement) : jamais un second job ; état actuel seulement
                return 409, {"ok": False, "error": "already_submitted", "state": s.state}
            try:
                raw = s.key.decrypt(museseal.b64u_dec(ct, CT_MAX))
                doc = json.loads(raw.decode("utf-8"))
            except (museseal.SealError, ValueError, UnicodeDecodeError, RecursionError, AttributeError):
                doc = None
            raw = None
            tok = None
            ok_doc = isinstance(doc, dict) and set(doc) == {"v", "s", "f", "t"} and doc.get("v") == 1 and \
                isinstance(doc.get("s"), str) and isinstance(doc.get("f"), str) and \
                hmac.compare_digest(doc["s"].encode("utf-8", "replace"), s.sid.encode("ascii")) and \
                hmac.compare_digest(doc["f"].encode("utf-8", "replace"), s.f.encode("ascii"))
            if ok_doc:
                tok = doc.get("t")
            doc = None
            if not ok_doc:
                return self._attempt(s, 400, "sealed_rejected")
            if not isinstance(tok, str) or not SDK_TOKEN_RE.fullmatch(tok):
                tok = None
                return self._attempt(s, 400, "invalid_token")
            jid = new_job_id()
            try:
                write_job(self.cfg.muse_jobs_dir, self.cfg.muse_jobs_tmp,
                          {"v": 1, "id": jid, "action": "pair", "consent": "tv", "created": round(self.clock(), 3),
                           "token": tok})
            except (OSError, ValueError) as e:
                self.log("muse_setup_job_error", error=type(e).__name__)
                return 503, {"ok": False, "error": "unavailable"}
            finally:
                tok = None
            s.job = jid
            s.deadline = s.pair_deadline = self.mono() + PAIR_BUDGET_S
            s.expires = self.mono() + PICKUP_S        # pas de « preparing » à temps ⇒ helper indisponible
            self._scrub(s)                            # clé privée et secret f inutiles désormais : effacés
            self._set(s, "submitted")
            return 200, {"ok": True, "state": s.state}

    def _attempt(self, s, code, err):
        s.attempts += 1
        self._bump()
        if s.attempts >= MAX_ATTEMPTS:
            self._set(s, "failed", "too_many_attempts")
            return 403, {"ok": False, "error": "too_many_attempts"}
        return code, {"ok": False, "error": err}

    # -- boucle du service
    def _expire(self):
        s = self.cur
        if s is None or s.state in FINAL:
            return
        now = self.mono()
        if s.state in ("keygen", "await_token") and now >= s.expires:
            self._set(s, "expired", "expired")
            return
        if s.state == "submitted" and now >= s.expires and s.job and withdraw_job(self.cfg.muse_jobs_dir, s.job):
            # jamais pris en charge : job (et jeton) retirés, rien n'a pu se produire
            if s.cancel_requested:
                self._set(s, "cancelled", "cancelled")
            else:
                self._set(s, "failed", "helper_unavailable")
            return
        if s.deadline is not None and now >= s.deadline:
            if s.job:
                withdraw_job(self.cfg.muse_jobs_dir, s.job)
            if s.cancel_requested and not s.cancel_pending and s.pair_deadline is not None and \
                    now < s.pair_deadline:
                # annulation demandée, toujours sans issue du helper : jamais « rien n'a changé » sans preuve ;
                # « Annulation demandée », et le suivi continue jusqu'à l'issue réelle (échéance globale)
                s.cancel_pending = True
                s.deadline = s.pair_deadline
                self._bump()
                self.log("muse_setup_cancel", state="pending")
            elif s.cancel_requested:
                self.reconcile = s.job            # issue tardive du helper : encore annoncée telle quelle
                self.reconcile_cancel = True
                self._set(s, "failed", "cancel_unconfirmed")
            else:
                self.reconcile = s.job
                self.reconcile_cancel = False
                self._set(s, "failed", "timeout")

    def tick(self):
        results = read_results(self.cfg.muse_results_dir)
        box = read_box(self.cfg.muse_results_dir)
        with self.lock:
            self.box = box
            for r in results:
                self._on_result(r)
            self._expire()
            d = self.disc
            if d is not None and d["state"] == "running" and self.mono() >= d["deadline"]:
                withdraw_job(self.cfg.muse_jobs_dir, d["job"])
                self._disc_end("failed", "timeout")

    def _disc_end(self, state, error):
        self.disc["state"], self.disc["error"] = state, error
        self._bump()
        self.disc_last = {"seq": self.seq, "state": state}
        self.log("muse_disconnect", state=state, error=error)

    def _on_result(self, r):
        d = self.disc
        if d is not None and r["id"] == d["job"]:
            if r["phase"] == "done" and d["state"] == "running":
                self._disc_end("done" if r["ok"] else "failed", None if r["ok"] else (r["result"] or "helper_error"))
            return
        if self.reconcile is not None and r["id"] == self.reconcile and r["phase"] == "done":
            # issue réelle arrivée après une fin sans confirmation (annulation non confirmée, délai) : annoncée
            # telle quelle ; « annulé » seulement si l'annulation avait été demandée ET l'état d'avant rendu
            self.reconcile = None
            code = "connected" if r["ok"] else (r["result"] or "helper_error")
            state = "connected" if r["ok"] else \
                "cancelled" if code in ("cancelled", "interrupted") and self.reconcile_cancel else "failed"
            self._bump()
            self.last = {"seq": self.seq, "state": state, "error": None if r["ok"] else code, "late": True,
                         "message": message_for(state if state != "failed" else code)}
            self.log("muse_setup", state=state, error=self.last["error"], late=True)
            return
        s = self.cur
        if s is None or not s.job or r["id"] != s.job or s.state in FINAL:
            self.log("muse_setup_result_ignored")
            return
        if r.get("ble_name"):
            s.ble_name = r["ble_name"]
        if r["phase"] in PROGRESS:
            if s.state == "submitted":
                s.expires = s.deadline            # pris en charge : seule l'échéance globale compte désormais
            self._set(s, r["phase"])
            return
        if r["ok"]:
            s.registered = r.get("registered")
            self._set(s, "connected")
        else:
            code = r["result"] or "helper_error"
            # « annulé » seulement pour une annulation demandée et confirmée par le helper (état d'avant rendu) ;
            # paired_incomplete / recovery_incomplete ne sont JAMAIS une annulation, même demandée
            if code in ("cancelled", "interrupted") and s.cancel_requested:
                self._set(s, "cancelled", "cancelled")
            else:
                self._set(s, "failed", code)

    # -- vues
    def busy(self):
        with self.lock:
            return (self.cur is not None and self.cur.state not in FINAL) or \
                (self.disc is not None and self.disc["state"] == "running")

    def submitted(self):
        """Jeton déjà remis au helper (appariement en cours ou terminé) : une coupure de la page ne l'annule pas."""
        with self.lock:
            s = self.cur
            return s is not None and RANK[s.state] >= RANK["submitted"]

    def listening(self):
        """Écoute LAN voulue : séance non finale, ou finale depuis moins de LINGER_S (le téléphone lit l'issue)."""
        with self.lock:
            s = self.cur
            if s is None:
                return False
            if s.state not in FINAL:
                return True
            return self.mono() - (s.ended_at or 0) < LINGER_S

    @staticmethod
    def _final_message(s):
        """Message de l'issue : celui de l'état final (connectée, annulé, expiré), sinon celui du code d'échec."""
        if s is None or s.state not in FINAL:
            return None
        return message_for(s.state if s.state != "failed" else s.error)

    @staticmethod
    def _label(s):
        if s.state not in FINAL and s.cancel_requested:
            return LABEL_CANCEL_PENDING if s.cancel_pending else LABEL_CANCELLING
        return LABELS[s.state]

    def tv_view(self, host, port, identity_present=None):
        """Vue TV (loopback) : jamais de clé, de secret CSRF, de jeton ni d'adresse IP. `host` = nom mDNS VÉRIFIÉ
        de la boîte (<nom>.local) ou None : sans lui, aucun QR (jamais de repli sur l'adresse)."""
        with self.lock:
            self._expire()
            s = self.cur
            d = self.disc
            dv = None
            if d is not None:
                dv = {"state": d["state"], "message": None if d["state"] == "running" else
                      message_for("disconnected" if d["state"] == "done" else
                                  "foreign_identity" if d["error"] == "foreign_identity" else "disconnect_failed")}
            out = {"seq": self.seq, "last": dict(self.last), "disconnect": dv, "disconnect_last": dict(self.disc_last)}
            out.update(self.box_view(identity_present))
            if s is None or s.state in FINAL:
                out.update(active=False, state=s.state if s else None, url=None, expires_in=None, phone=False,
                           message=self._final_message(s), error=s.error if s else None,
                           cancelling=False, cancel_pending=False, ble_name=None)
                return out
            url = None
            if s.state == "await_token" and host and port and host.endswith(".local"):
                url = "http://%s:%d/muse?s=%s#%s" % (host, port, s.sid, s.f)
            out.update(active=True, state=s.state, url=url, label=self._label(s),
                       expires_in=max(0, int(s.expires - self.mono())) if s.state == "await_token" else None,
                       phone=bool(s.phone_seen), message=None, error=None, cancelling=bool(s.cancel_requested),
                       cancel_pending=bool(s.cancel_pending), ble_name=s.ble_name)
            return out
