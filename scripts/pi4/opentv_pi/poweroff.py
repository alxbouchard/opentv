"""Réglages → « Éteindre la boîte » (0.7.23) : arrêt PROPRE de la Pi demandé à la TV, sans clavier ni réseau.

Chaîne bornée, aucune commande ni argument venus de l'utilisateur :

1. Page /tv (portes TV complètes : loopback, Host exact, UID du kiosque, clé TV ; en plus Origin EXACTE de la page) :
   - POST ARM_PATH quand la feuille de confirmation s'ouvre (« Annuler » au focus) -> défi à usage unique, mémoire
     seulement, valable ARM_TTL_S ;
   - POST CONFIRM_PATH {"challenge", "confirm": true} au plus tôt MIN_CONFIRM_S après l'armement (un OK tenu ou un
     double OK n'arrive jamais sur « Éteindre ») -> demande déposée ;
   - POST CANCEL_PATH (Annuler, Retour, Accueil) -> défi oublié.
   Aucune route LAN, aucun outil d'assistant : l'Agent Port et le téléphone n'ont rien de neuf (le pavé du téléphone
   appairé ne fait que des touches, comme la télécommande).
2. opentv.service (compte opentv) dépose `request` (JSON fermé : v, op, boot_id, at = CLOCK_BOOTTIME, id) dans
   POWER_DIR = /run/opentv-poweroff (tmpfs : vidé à chaque démarrage ; tmpfiles root:opentv 0730 — le groupe écrit
   et retire ses fichiers sans pouvoir lister, root lit sans aucune capacité). Au démarrage du service, toute demande
   ou issue restante est retirée : jamais rejouée par un nouveau processus. Pas prise en charge en PICKUP_S : retirée
   par le service (linéarisation : celui qui retire le nom gagne) -> « failed », la boîte reste allumée.
3. opentv-poweroff.path (PathExists) -> opentv-poweroff.service (root, oneshot, SANS capacité, sans réseau ni
   périphérique) : `opentv-pi poweroff-action` réclame le nom (rename puis même inode), vérifie dossier (root:<groupe du
   service> 0730), fichier (régulier, lien unique, propriétaire du service, petit), schéma, MÊME boot_id et âge
   0..MAX_AGE_S, écrit l'issue (`result`, 0644) et sort 0 SEULEMENT si tout est valide. C'est systemd qui éteint :
   `OnSuccess=poweroff.target` (`OnSuccessJobMode=replace-irreversibly`, comme `systemctl poweroff`). Aucun processus
   OpenTV n'exécute de commande ; aucun sudo ; Chromium inchangé ; aucune trame CEC ni ordre au téléviseur ; la
   touche Power des télécommandes reste ignorée (logind inchangé).

Rien n'est rejoué après un démarrage : /run est un tmpfs, la demande porte le boot_id, son âge est borné, son nom est
réclamé avant toute action, le défi vit en mémoire du service seulement.
"""
import hmac
import json
import os
import secrets
import stat
import threading
import time

ARM_PATH = "/api/tv/power/arm"
CONFIRM_PATH = "/api/tv/power/confirm"
CANCEL_PATH = "/api/tv/power/cancel"
REQUEST = "request"
RESULT = "result"
CLAIM = "claimed"
DIR_MODE = 0o730
FILE_MODE = 0o644            # root lit sans CAP_DAC_OVERRIDE ; rien de secret (aucun jeton, aucune commande)
MAX_FILE = 512
BODY_MAX = 512
ARM_TTL_S = 60.0             # feuille ouverte plus longtemps : nouveau « Éteindre » demandé (défi expiré)
MIN_CONFIRM_S = 0.6          # OK tenu / double OK : jamais une confirmation
PICKUP_S = 15.0              # demande non prise par root dans ce délai : retirée, échec récupérable
RESULT_WAIT_S = 10.0         # demande prise mais aucune issue écrite : échec « inconnu » (honnête)
MAX_AGE_S = 30.0             # côté root : demande plus vieille (ou d'un autre démarrage) refusée
STATES = ("idle", "requested", "accepted", "failed")
REASONS = frozenset(("not_started", "unknown", "write", "boot_unknown", "refused", "dir", "request", "schema",
                     "boot", "stale", "uninstalling", "disabled", "race"))
# textes de la TV pour les refus (tv.js : une réponse non 2xx est lue seulement si elle porte « message »)
MSG_KO = "L'arrêt n'a pas démarré : la boîte reste allumée.\nRéessaie : Réglages → Éteindre la boîte."
MESSAGES = {"unavailable": "L'arrêt depuis la TV n'est pas disponible sur cette boîte.",
            "in_progress": "Un arrêt est déjà en cours.",
            "not_armed": "Délai dépassé : choisis de nouveau « Éteindre la boîte ».",
            "expired": "Délai dépassé : choisis de nouveau « Éteindre la boîte ».",
            "too_soon": "Pour confirmer, appuie de nouveau sur OK.",
            "bad_request": MSG_KO, "write": MSG_KO, "boot_unknown": MSG_KO, "disabled": MSG_KO}
_O_PATH = getattr(os, "O_PATH", 0)
# fd de CHEMIN (Linux) : le service (groupe, -wx) n'a pas le droit de lister ; openat/renameat/unlinkat du nom connu
# suffisent. Hors Linux (tests macOS, propriétaire du dossier) : repli O_RDONLY.
_DIR_FLAGS = (_O_PATH or os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def boottime():
    """Horloge commune à tous les processus d'un démarrage (Linux : CLOCK_BOOTTIME, suspension comprise)."""
    clk = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clk) if clk is not None else time.monotonic()


def _noop(event, **fields):
    pass


def _read_small(dfd, name, owner_uid):
    """-> (octets, fstat) d'un petit fichier régulier, lien unique, propriétaire attendu ; (None, None) sinon."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dfd)
    except OSError:
        return None, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_FILE or st.st_uid != owner_uid:
            return None, None
        data = os.read(fd, MAX_FILE + 1)
        return (data, st) if len(data) <= MAX_FILE else (None, None)
    except OSError:
        return None, None
    finally:
        os.close(fd)


def _write_atomic(dfd, name, doc):
    """Brouillon O_EXCL puis rename dans le MÊME dossier ouvert (aucun lien suivi), 0644."""
    tmp = name + ".tmp"
    try:
        os.unlink(tmp, dir_fd=dfd)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE, dir_fd=dfd)
    try:
        os.fchmod(fd, FILE_MODE)   # UMask=0077 des unités
        os.write(fd, json.dumps(doc, sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)


def _unlink(dfd, name):
    """-> True retiré, False absent (ENOENT), None autre échec."""
    try:
        os.unlink(name, dir_fd=dfd)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _is_id(v):
    return isinstance(v, str) and len(v) == 32 and all(c in "0123456789abcdef" for c in v)


def request_ok(doc):
    return isinstance(doc, dict) and set(doc) == {"v", "op", "boot_id", "at", "id"} and doc["v"] == 1 and \
        doc["op"] == "poweroff" and isinstance(doc["boot_id"], str) and 0 < len(doc["boot_id"]) <= 64 and \
        isinstance(doc["at"], (int, float)) and not isinstance(doc["at"], bool) and _is_id(doc["id"])


def result_ok(doc):
    return isinstance(doc, dict) and set(doc) == {"v", "id", "outcome", "reason"} and doc["v"] == 1 and \
        _is_id(doc["id"]) and doc["outcome"] in ("accepted", "refused") and \
        (doc["reason"] is None or doc["reason"] in REASONS)


def _err(code, error, **extra):
    return code, dict({"ok": False, "error": error, "message": MESSAGES.get(error, MSG_KO)}, **extra)


def _loads(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError, AttributeError):
        return None


# --------------------------------------------------------------------------------------------- service (opentv)
class PowerOff:
    """État mémoire de l'arrêt demandé à la TV ; une seule demande à la fois ; jamais de commande exécutée ici."""

    def __init__(self, power_dir, enabled=True, boot_id="", mono=time.monotonic, clock=boottime, log=_noop,
                 token=secrets.token_urlsafe, new_id=lambda: secrets.token_hex(16)):
        self.dir, self.enabled, self.boot_id = power_dir, bool(enabled), boot_id or ""
        self.mono, self.clock, self.log, self.token, self.new_id = mono, clock, log, token, new_id
        self.lock = threading.Lock()
        self.arm_tok = None          # (défi, horloge) : mémoire seulement
        self.req = None              # {"id", "phase": requested|taken, "deadline"}
        self.seq, self.state, self.reason = 0, "idle", None
        self._purge()

    def _dir(self):
        """fd du dossier s'il a la forme exacte attendue (dossier, 0730, groupe du service), sinon None."""
        try:
            dfd = os.open(self.dir, _DIR_FLAGS)
        except OSError:
            return None
        try:
            st = os.fstat(dfd)
            if stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == DIR_MODE and st.st_gid == os.getgid():
                return dfd
        except OSError:
            pass
        os.close(dfd)
        return None

    def _purge(self):
        """Démarrage du service : demande/issue d'un processus précédent retirées (jamais rejouées)."""
        dfd = self._dir()
        if dfd is None:
            return
        try:
            for name in (REQUEST, REQUEST + ".tmp", RESULT):
                if _unlink(dfd, name):
                    self.log("poweroff_stale_removed", name=name)
        finally:
            os.close(dfd)

    def available(self):
        if not self.enabled:
            return False
        dfd = self._dir()
        if dfd is None:
            return False
        os.close(dfd)
        return True

    def _fail(self, reason):
        self.req, self.state, self.reason = None, "failed", reason
        self.log("poweroff_failed", reason=reason)

    def _progress(self):
        """Sous self.lock : issue de root lue, sinon retrait borné d'une demande jamais prise."""
        if self.req is None:
            return
        dfd = self._dir()
        if dfd is None:
            return self._fail("dir")
        try:
            data, st = _read_small(dfd, RESULT, os.fstat(dfd).st_uid)   # écrite par le propriétaire (root)
            doc = _loads(data) if data is not None else None
            if result_ok(doc) and doc["id"] == self.req["id"]:
                _unlink(dfd, RESULT)
                if doc["outcome"] == "accepted":
                    self.req, self.state, self.reason = None, "accepted", None
                    self.log("poweroff_accepted")
                    return
                return self._fail(doc["reason"] or "refused")
            if self.mono() < self.req["deadline"]:
                return
            if self.req["phase"] == "requested":
                gone = _unlink(dfd, REQUEST)    # linéarisation : retiré ici = root ne le prendra jamais
                if gone:
                    return self._fail("not_started")
                self.req["phase"], self.req["deadline"] = "taken", self.mono() + RESULT_WAIT_S
                return
            self._fail("unknown")
        except OSError:
            self._fail("dir")
        finally:
            os.close(dfd)

    def view(self):
        with self.lock:
            self._progress()
            return {"available": self.available(), "state": self.state, "seq": self.seq, "reason": self.reason}

    def _busy(self):
        return self.state == "requested" or self.state == "accepted"

    def arm(self):
        with self.lock:
            self._progress()
            if not self.available():
                return _err(409, "unavailable")
            if self._busy():
                return _err(409, "in_progress")
            ch = self.token(24)
            self.arm_tok = (ch, self.mono())
            self.log("poweroff_armed")
            return 200, {"ok": True, "challenge": ch}

    def cancel(self):
        with self.lock:
            had = self.arm_tok is not None
            self.arm_tok = None
        if had:
            self.log("poweroff_cancelled")
        return 200, {"ok": True}

    def confirm(self, challenge, confirm):
        with self.lock:
            self._progress()
            if confirm is not True or not isinstance(challenge, str) or not 0 < len(challenge) <= 64:
                return _err(400, "bad_request")
            if self._busy():
                return _err(409, "in_progress")
            a = self.arm_tok
            if a is None or not hmac.compare_digest(a[0].encode("utf-8"), challenge.encode("utf-8")):
                return _err(409, "not_armed")
            age = self.mono() - a[1]
            if not 0 <= age <= ARM_TTL_S:
                self.arm_tok = None
                return _err(409, "expired")
            if age < MIN_CONFIRM_S:   # défi gardé : un vrai OK sur « Éteindre » reste possible juste après
                return _err(409, "too_soon")
            self.arm_tok = None
            self.seq += 1
            if not self.enabled or not self.boot_id:
                self._fail("disabled" if not self.enabled else "boot_unknown")
                return _err(409, self.reason, power=self._pub())
            rid = self.new_id()
            doc = {"v": 1, "op": "poweroff", "boot_id": self.boot_id, "at": self.clock(), "id": rid}
            dfd = self._dir()
            try:
                if dfd is None:
                    raise OSError("dir")
                try:
                    os.stat(REQUEST, dir_fd=dfd, follow_symlinks=False)
                    raise OSError("pending")   # jamais deux demandes, jamais réécrite
                except FileNotFoundError:
                    pass
                _unlink(dfd, RESULT)           # aucune issue ancienne lue pour celle-ci (id différent de toute façon)
                _write_atomic(dfd, REQUEST, doc)
            except OSError:
                self._fail("write")
                return _err(409, "write", power=self._pub())
            finally:
                if dfd is not None:
                    os.close(dfd)
            self.req = {"id": rid, "phase": "requested", "deadline": self.mono() + PICKUP_S}
            self.state, self.reason = "requested", None
            self.log("poweroff_requested")
            return 200, {"ok": True, "power": self._pub()}

    def _pub(self):
        return {"available": self.available(), "state": self.state, "seq": self.seq, "reason": self.reason}


# --------------------------------------------------------------------------------------------- routes (web.TVHandler)
def _body(h):
    """Portes propres à ces routes (après celles du kiosque) : Origin EXACTE de la page TV, JSON borné."""
    host = h.headers.get("Host") or ""
    if h.headers.get("Origin") != "http://" + host:
        return None, (403, {"ok": False, "error": "origin"})
    if (h.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
        return None, (415, {"ok": False, "error": "content_type"})
    raw = h.read_body(BODY_MAX)
    if raw is None:
        return None, (413, {"ok": False, "error": "too_large"})
    body = _loads(raw or b"{}")
    if not isinstance(body, dict):
        return None, (400, {"ok": False, "error": "bad_json"})
    return body, None


def handle_arm(h, app):
    body, err = _body(h)
    if err:
        return err
    app.canvas_human("key")   # geste humain ordinaire : un aperçu Canvas vivant est annulé d'abord
    return app.power.arm()


def handle_confirm(h, app):
    body, err = _body(h)
    if err:
        return err
    return app.power.confirm(body.get("challenge"), body.get("confirm"))


def handle_cancel(h, app):
    body, err = _body(h)
    if err:
        return err
    return app.power.cancel()


POSTS = {ARM_PATH: handle_arm, CONFIRM_PATH: handle_confirm, CANCEL_PATH: handle_cancel}


# --------------------------------------------------------------------------------------------- action (root)
def run_action(power_dir, service_uid, service_gid, boot_id, now, uninstalling=None, enabled=True, log=_noop):
    """(root, opentv-poweroff.service) -> {"action": "poweroff"|"none"|"withdrawn"|"refused", "reason"}.
    "poweroff" SEULEMENT pour une demande valide de CE démarrage ; l'appelant sort alors 0 et systemd éteint
    (OnSuccess=poweroff.target). Aucune commande ici. Toute demande trouvée est réclamée puis retirée."""
    try:
        dfd = os.open(power_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return {"action": "refused", "reason": "dir"}
    try:
        st = os.fstat(dfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_gid != service_gid or \
                stat.S_IMODE(st.st_mode) != DIR_MODE:
            log("poweroff_refused", reason="dir")
            return {"action": "refused", "reason": "dir"}
        try:
            os.stat(REQUEST, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return {"action": "none", "reason": None}
        data, fst = _read_small(dfd, REQUEST, service_uid)
        try:   # réclamer le NOM avant d'agir : le service qui l'a retiré entre-temps gagne
            os.rename(REQUEST, CLAIM, src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileNotFoundError:
            log("poweroff_withdrawn")
            return {"action": "withdrawn", "reason": None}
        try:
            cst = os.stat(CLAIM, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            cst = None
        _unlink(dfd, CLAIM)
        doc = _loads(data) if data is not None else None
        rid = doc.get("id") if isinstance(doc, dict) and _is_id(doc.get("id")) else None
        reason = None
        if data is None:
            reason = "request"
        elif cst is None or (cst.st_dev, cst.st_ino) != (fst.st_dev, fst.st_ino):
            reason = "race"
        elif not request_ok(doc):
            reason = "schema"
        elif not enabled:
            reason = "disabled"
        elif uninstalling and os.path.lexists(uninstalling):
            reason = "uninstalling"
        elif not boot_id or doc["boot_id"] != boot_id:
            reason = "boot"
        elif not 0 <= now - doc["at"] <= MAX_AGE_S:
            reason = "stale"
        outcome = "refused" if reason else "accepted"
        if rid is not None:
            try:
                _write_atomic(dfd, RESULT, {"v": 1, "id": rid, "outcome": outcome, "reason": reason})
            except OSError:
                log("poweroff_result_unwritten")
        if reason:
            log("poweroff_refused", reason=reason)
            return {"action": "refused", "reason": reason}
        log("poweroff_accepted")
        return {"action": "poweroff", "reason": None}
    finally:
        os.close(dfd)
