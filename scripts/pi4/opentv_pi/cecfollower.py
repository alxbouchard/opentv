"""HDMI-CEC : suiveur exclusif minimal (0.7.6, opentv-cec-follower.service, compte opentv, aucune capacité).

Pourquoi : sur la Samsung terrain, la TV cesse parfois d'émettre <User Control> vers OpenTV (30 s à 5 min) alors que le
bus acquitte et que les services sont sains ; sans suiveur, le noyau répond FEATURE_ABORT à <Give Device Power Status>
(0x8f), et après une extinction/rallumage rien ne rétablit l'entrée avant `cec --activate`. Un `cec-follower -m -s`
complet maintenait les touches. Ce module en garde le strict minimum, par ioctl directs (aucun texte de cec-ctl), dans
un service dédié étroit : le moniteur textuel opentv-cec-return.service reste inchangé et indépendant.

Contrat :
- adaptateur vc4 découvert dynamiquement (/dev/cecN, jamais en dur) dont la configuration est la nôtre (Playback
  « OpenTV », posée par opentv-cec.service) ; ouvert O_RDWR|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC, périphérique caractère
  exigé ; mode INITIATOR|EXCL_FOLLOWER (0x21) relu par G_MODE — jamais passthrough (0x31), refusé par le transport ;
  adresse physique, adresses logiques et masque relus à chaque événement et avant chaque émission ; aucun S_LOG_ADDRS
  ni S_PHYS_ADDR (aucune réclamation, aucun POLL : ces ioctl n'existent pas ici) ; ALLOW_RC_PASSTHRU jamais touché
  (le noyau continue seul à transformer <User Control> en touches Linux ; avertissement s'il manque).
- réponses (seulement à une demande reçue, jamais spontanées) :
  * <Give Device Power Status> dirigé vers notre adresse logique ⇒ exactement <Report Power Status> « on »
    [LA<<4|init, 0x90, 0x00] ; le noyau n'envoie plus son FEATURE_ABORT automatique (suiveur exclusif) ;
  * <Set Stream Path> diffusé PAR LA TV, longueur exacte 4, vers NOTRE adresse physique ⇒ exactement une
    <Active Source> [LA<<4|15, 0x82, PA] (la TV demande explicitement notre chemin ; aucun <Menu Status>) ; autre
    adresse, trame malformée, autre initiateur, adaptateur qui bouge ⇒ rien ;
  * autre message dirigé non traité par le noyau ⇒ FEATURE_ABORT « unrecognized » (parité avec le noyau sans
    suiveur) ; jamais pour FEATURE_ABORT, <User Control Pressed/Released>, <Report Physical Address> ni les messages
    réservés à la diffusion ; aucune réponse aux autres diffusions ;
  * opcodes « cœur » répondus par le noyau hors passthrough (9f ff 46 8c a5 83) reçus quand même ⇒ hypothèse de mode
    rompue : échec fermé (sortie 78, mode rendu).
- jamais : <Image View On>, <Text View On>, <Standby>, question d'alimentation, POLL, annonce périodique, réclamation,
  <Menu Status>, touche injectée. guard_tx (liste blanche des trois formes ci-dessus, octets exacts) est appliquée
  juste avant chaque CEC_TRANSMIT ; une trame refusée ⇒ échec fermé.
- changement d'état (TV éteinte/rallumée, HDMI) : même descripteur, mode suiveur conservé par le noyau ; relecture
  immédiate à chaque événement (aucune scrutation, réveil par poll) ; « prêt » dès que l'adaptateur est de nouveau
  configuré avec notre identité, pour ne pas manquer la fenêtre de retour. Aucune annonce sur simple HPD : seule une
  demande de la TV est servie. Configuration devenue étrangère ⇒ mode rendu (0x01, l'abandon automatique du noyau
  reprend), descripteur fermé, redécouverte bornée ; périphérique retiré (ENODEV, POLLERR) ⇒ réouverture après
  0,2 s puis 1/2/5 s — jamais de boucle serrée.
- un seul propriétaire EXCL_FOLLOWER (EBUSY ⇒ jamais volé ; persistant ⇒ 78) ; verrou CEC partagé
  /run/opentv-cec/lock exigé pour <Active Source> et FEATURE_ABORT (attente bornée LOCK_WAIT_S, sinon `lock_busy`,
  rien émis) ; <Report Power Status> (LOCK_FREE_KINDS) ne l'attend jamais : réponse directe à une question de la TV,
  sans effet de source, qui doit partir < 1 s même pendant que opentv-cec.service tient le verrou (relecture de
  l'adresse et liste blanche toujours appliquées) ; demande périmée (> STALE_MS, horodatage noyau) ignorée ; débit
  borné (TX_RATE_MAX/s, TX_MINUTE_MAX/min, <Active Source> espacées de AS_MIN_SPACING_S et ≤ AS_PER_HOUR_MAX/h) ;
  aucune réémission après un NACK.
- résultats d'émission (CEC_RECEIVE, diagnostic seulement : rien n'en dépend, jamais de réémission) : au plus
  PENDING_MAX en attente ; le plus ancien évincé au-delà, expiré après PENDING_TTL_S, tout vidé à la libération du
  descripteur — chaque perte comptée `tx_result_lost` ; un résultat inconnu (évincé) est seulement compté.
- accès refusé à l'ouverture (EACCES/EPERM : groupe udev pas encore posé, ou DeviceAllow=char-cec résolu avant le
  chargement du pilote CEC) : TRANSITOIRE — quelques relectures bornées puis sortie EXIT_TRANSIENT (1), relancée par
  systemd (Restart=on-failure, RestartSec=10, sans limite de démarrages) ; jamais 78.
- journal JSON sobre : transitions (held/ready/waiting/released), chaque <Set Stream Path> servi ou refusé avec sa
  raison, statistiques horaires seulement si elles changent, et un relevé final à toute sortie (SIGTERM compris) ;
  mode canari (OPENTV_CEC_FOLLOWER=canary, gate terrain) : une ligne par demande/réponse (opcode, latence) ET par
  demande non servie (`action: skip`, `intended`, `reason` : rx_not_ready, stale, rate_limited, duplicate_request,
  lock_busy, adapter_churn, tx_*), bornée à CANARY_LOG_MAX_PER_HOUR. Jamais d'octets bruts du bus.
- arrêt (SIGTERM), désinstallation/retour à une version sans ce module (fichier retiré ⇒ arrêt propre au réveil
  suivant, vérifié aussi juste après chaque réveil, avant tout traitement : rien émis) : mode rendu (0x01) puis
  close() — l'abandon automatique du noyau reprend, rien de persistant.
"""
import collections
import errno
import json
import os
import re
import select
import signal
import stat
import struct
import sys
import time

from . import VERSION, cecboot

# --------------------------------------------------------------------------- ABI (uapi linux/cec.h, asm-generic/ioctl.h)
CEC_MAX_MSG_SIZE = 16
MSG_FMT = "=QQIIII16sBBBBBBBx"          # struct cec_msg (56)
CAPS_FMT = "=32s32sIII"                  # struct cec_caps (76)
LA_FMT = "=4sHBBII15s4s4s4s48sx"         # struct cec_log_addrs (92)
EVENT_FMT = "=QII64s"                    # struct cec_event (80 ; union raw[16])
FORMATS = {"cec_msg": MSG_FMT, "cec_caps": CAPS_FMT, "cec_log_addrs": LA_FMT, "cec_event": EVENT_FMT}
SIZES = {"cec_msg": 56, "cec_caps": 76, "cec_log_addrs": 92, "cec_event": 80}


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("a") << 8) | nr


_R, _W, _RW = 2, 1, 3
# seuls ioctl émis par ce module : aucun setter d'adresse (S_LOG_ADDRS/S_PHYS_ADDR) — jamais de réclamation
IOCTL = {
    "CEC_ADAP_G_CAPS": _ioc(_RW, 0, 76),
    "CEC_ADAP_G_PHYS_ADDR": _ioc(_R, 1, 2),
    "CEC_ADAP_G_LOG_ADDRS": _ioc(_R, 3, 92),
    "CEC_TRANSMIT": _ioc(_RW, 5, 56),
    "CEC_RECEIVE": _ioc(_RW, 6, 56),
    "CEC_DQEVENT": _ioc(_RW, 7, 80),
    "CEC_G_MODE": _ioc(_R, 8, 4),
    "CEC_S_MODE": _ioc(_W, 9, 4),
}
EXPECTED_IOCTL = {
    "CEC_ADAP_G_CAPS": 0xC04C6100, "CEC_ADAP_G_PHYS_ADDR": 0x80026101, "CEC_ADAP_G_LOG_ADDRS": 0x805C6103,
    "CEC_TRANSMIT": 0xC0386105, "CEC_RECEIVE": 0xC0386106, "CEC_DQEVENT": 0xC0506107, "CEC_G_MODE": 0x80046108,
    "CEC_S_MODE": 0x40046109,
}

MODE_PLAIN = 0x01               # INITIATOR | NO_FOLLOWER (valeur d'ouverture)
MODE_FOLLOWER = 0x21            # INITIATOR | EXCL_FOLLOWER
MODE_PASSTHRU = 0x31            # INITIATOR | EXCL_FOLLOWER_PASSTHRU : jamais (le noyau ne répondrait plus au cœur)
CEC_CAP_TRANSMIT = 1 << 2
CEC_CAP_RC = 1 << 4
CEC_EVENT_STATE_CHANGE = 1
CEC_EVENT_LOST_MSGS = 2
CEC_EVENT_FL_INITIAL_STATE = 1 << 0
CEC_EVENT_FL_DROPPED_EVENTS = 1 << 1
CEC_TX_STATUS_OK = 1 << 0
CEC_LOG_ADDR_INVALID = 0xFF
CEC_PHYS_ADDR_INVALID = 0xFFFF
CEC_LOG_ADDRS_FL_ALLOW_RC_PASSTHRU = 1 << 1
PRIM_DEVTYPE_PLAYBACK = 4
PLAYBACK_LAS = frozenset({4, 8, 11})
TV, BROADCAST = 0, 15

# --------------------------------------------------------------------------- opcodes
FEATURE_ABORT = 0x00
ACTIVE_SOURCE = 0x82
SET_STREAM_PATH = 0x86
GIVE_DEVICE_POWER_STATUS = 0x8F
REPORT_POWER_STATUS = 0x90
POWER_ON = 0x00
ABORT_UNRECOGNIZED = 0x00
USER_CONTROL = frozenset({0x44, 0x45})
# répondus par le noyau lui-même hors passthrough, jamais remis au suiveur : les voir = hypothèse de mode rompue
KERNEL_CORE_OPCODES = frozenset({0x9F, 0xFF, 0x46, 0x8C, 0xA5, 0x83})
# messages que le noyau ne déclare jamais en diffusion seulement… et qu'il ne remet jamais dirigés (cec_msg_size)
BROADCAST_ONLY = frozenset({0x82, 0x85, 0x80, 0x81, 0x86, 0x84, 0x32, 0x87, 0xA6, 0xA7, 0xA8, 0xF8})
# dirigés jamais abandonnés (parité noyau : FEATURE_ABORT, touches, diffusion seule — <Report Physical Address> compris)
NO_ABORT_DIRECTED = frozenset({FEATURE_ABORT}) | USER_CONTROL | BROADCAST_ONLY
# garde : opcodes qu'un FEATURE_ABORT émis ne peut jamais porter
NEVER_ABORT = NO_ABORT_DIRECTED | KERNEL_CORE_OPCODES | {GIVE_DEVICE_POWER_STATUS}

# --------------------------------------------------------------------------- bornes
EXIT_FATAL = 78                 # opentv-cec-follower.service : RestartPreventExitStatus=78
EXIT_TRANSIENT = 1              # Restart=on-failure, RestartSec=10, StartLimitIntervalSec=0 : toujours relancé
STALE_MS = 800                  # au-delà, la demande n'est plus servie (réponse < 1 s exigée)
LOCK_WAIT_S = 0.4               # verrou CEC partagé : attente bornée, sinon rien émis
LOCK_POLL_S = 0.02
TX_RATE_MAX = 10                # émissions par seconde glissante
TX_RATE_WINDOW_S = 1.0
TX_MINUTE_MAX = 120             # émissions par minute glissante
AS_MIN_SPACING_S = 2.0          # deux <Active Source> jamais à moins de 2 s (doublon de la même demande)
AS_PER_HOUR_MAX = 30
VERIFY_S = 5.0                  # relecture locale (ioctl, aucun trafic) : mode, adresse, installation
STATS_EVERY_S = 3600.0
DISCOVER_DELAYS_S = (1.0, 2.0, 2.0, 5.0)     # aucun adaptateur à nous : relecture locale bornée, jamais serrée
REOPEN_DELAYS_S = (0.2, 1.0, 2.0, 5.0)       # descripteur perdu : réouverture rapide puis espacée
HEALTHY_HOLD_S = 60.0           # un descripteur tenu aussi longtemps remet ces paliers au début
BUSY_MAX = 3                    # EBUSY (autre suiveur exclusif) consécutifs avant l'échec véridique
BUSY_RETRY_S = 5.0
CANARY_LOG_MAX_PER_HOUR = 600
DRAIN_MAX = 64                  # messages/événements lus par réveil (le reste au réveil suivant)
PENDING_MAX = 64                # résultats d'émission attendus (diagnostic) : borne dure
PENDING_TTL_S = 30.0            # un résultat jamais reçu (perdu) est oublié après ce délai
# seule réponse émise sans le verrou CEC partagé : <Report Power Status> « on » (aucun effet de source)
LOCK_FREE_KINDS = frozenset({"reply_power"})
DEV_RE = re.compile(r"/dev/cec[0-9]{1,2}")
UNREADABLE_MAX = 5              # découvertes consécutives illisibles : accès refusé ⇒ 1 (relance), sinon 78
UNREADABLE_WHY = frozenset({"open_failed", "read_failed", "permission_denied"})
PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
FATAL = frozenset({"mode_anomaly", "mode_readback_mismatch", "mode_changed", "forbidden_tx", "abi_mismatch",
                   "exclusive_follower_busy", "s_mode_failed", "adapter_unreadable"})
ENONET = getattr(errno, "ENONET", 64)


def abi_errors():
    """-> liste des écarts entre ce module et l'ABI uapi (vide = conforme)."""
    errs = []
    for name, size in SIZES.items():
        got = struct.calcsize(globals()[{"cec_msg": "MSG_FMT", "cec_caps": "CAPS_FMT", "cec_log_addrs": "LA_FMT",
                                         "cec_event": "EVENT_FMT"}[name]])
        if got != size:
            errs.append("sizeof(%s)=%d want %d" % (name, got, size))
    if set(IOCTL) != set(EXPECTED_IOCTL):
        errs.append("ioctl_table_drift")
    for k, v in EXPECTED_IOCTL.items():
        if IOCTL.get(k) != v:
            errs.append("%s=0x%08X want 0x%08X" % (k, IOCTL.get(k) or 0, v))
    return errs


# --------------------------------------------------------------------------- identité de l'adaptateur
Identity = collections.namedtuple("Identity", "pa las mask primary flags")


def config_ours(la):
    """Configuration logique OpenTV (Playback « OpenTV »), même adaptateur déconfiguré (TV éteinte : conservée)."""
    n = la.get("num_log_addrs") or 0
    prim = la.get("primary_device_type") or [0]
    return 1 <= n <= 4 and la.get("osd_name") == cecboot.OSD_NAME and prim[0] == PRIM_DEVTYPE_PLAYBACK


def identity_from(caps, pa, la):
    """(caps, adresse physique, adresses logiques) lus -> ("ready", Identity) ou (raison, None)."""
    if not (caps.get("driver") or "").startswith("vc4"):
        return "not_vc4", None
    if not caps.get("capabilities", 0) & CEC_CAP_TRANSMIT:
        return "no_transmit", None
    if not config_ours(la):
        return "not_ours", None
    if pa == CEC_PHYS_ADDR_INVALID:
        return "disconnected", None
    if pa == 0x0000:
        return "bad_physical_address", None
    mask = la.get("log_addr_mask") or 0
    n = min(la["num_log_addrs"], 4)
    las = tuple(sorted(a for a in la["log_addr"][:n] if a != CEC_LOG_ADDR_INVALID))
    if mask == 0 or not las:
        return "unconfigured", None
    if sum(1 << a for a in las) != mask or not set(las) <= PLAYBACK_LAS:
        return "bad_logical_address", None
    primary = la["log_addr"][0]
    if primary not in las:
        return "bad_logical_address", None
    return "ready", Identity(pa, las, mask, primary, la.get("flags", 0))


# --------------------------------------------------------------------------- politique pure
Action = collections.namedtuple("Action", "kind frame reason")


def _ignore(reason):
    return Action("ignore", None, reason)


def decide(frame, ident):
    """Réaction à UNE trame reçue (octets bruts) pour l'identité courante -> Action (frame None = rien émis)."""
    frame = bytes(frame)
    if len(frame) < 2:
        return _ignore("poll")
    init, dest, op = frame[0] >> 4, frame[0] & 0xF, frame[1]
    if dest == BROADCAST:
        if op != SET_STREAM_PATH:
            return _ignore("broadcast")
        if init != TV:
            return _ignore("stream_path_not_from_tv")
        if len(frame) != 4:
            return _ignore("stream_path_malformed")
        if struct.unpack(">H", frame[2:4])[0] != ident.pa:
            return _ignore("stream_path_other")
        return Action("active_source", bytes([(ident.primary << 4) | BROADCAST, ACTIVE_SOURCE,
                                              ident.pa >> 8, ident.pa & 0xFF]), "set_stream_path")
    if dest not in ident.las:
        return _ignore("not_for_us")
    if init in ident.las:
        return _ignore("from_self")
    if init == BROADCAST:
        return _ignore("from_unregistered")
    if op in KERNEL_CORE_OPCODES:
        return Action("anomaly", None, "kernel_core_opcode_%02x" % op)
    hdr = (dest << 4) | init
    if op == GIVE_DEVICE_POWER_STATUS:
        return Action("reply_power", bytes([hdr, REPORT_POWER_STATUS, POWER_ON]), "give_device_power_status")
    if op in USER_CONTROL:
        return _ignore("user_control")
    if op in BROADCAST_ONLY:
        return _ignore("broadcast_only_directed")
    if op in NO_ABORT_DIRECTED:
        return _ignore("kernel_parity_no_abort")
    return Action("feature_abort", bytes([hdr, FEATURE_ABORT, op, ABORT_UNRECOGNIZED]), "unrecognized")


class ForbiddenTx(Exception):
    pass


def guard_tx(frame, ident):
    """Dernier rempart avant CEC_TRANSMIT : seulement <Report Power Status on> dirigé, FEATURE_ABORT(op, 0) dirigé,
    ou <Active Source> diffusée avec NOTRE adresse physique, toujours depuis une de nos adresses logiques."""
    if not isinstance(frame, (bytes, bytearray)):
        raise ForbiddenTx("type")
    frame = bytes(frame)
    if len(frame) not in (3, 4):
        raise ForbiddenTx("length_%d" % len(frame))
    init, dest, op = frame[0] >> 4, frame[0] & 0xF, frame[1]
    if init not in ident.las:
        raise ForbiddenTx("initiator")
    if dest in ident.las:
        raise ForbiddenTx("destination")
    if op == REPORT_POWER_STATUS and len(frame) == 3 and frame[2] == POWER_ON and dest != BROADCAST:
        return frame
    if op == FEATURE_ABORT and len(frame) == 4 and dest != BROADCAST and frame[3] == ABORT_UNRECOGNIZED and \
            frame[2] not in NEVER_ABORT:
        return frame
    if op == ACTIVE_SOURCE and len(frame) == 4 and dest == BROADCAST and \
            struct.unpack(">H", frame[2:4])[0] == ident.pa:
        return frame
    raise ForbiddenTx("frame_%02x" % op)


# --------------------------------------------------------------------------- transport ioctl
class AdapterGone(Exception):
    pass


RxMsg = collections.namedtuple("RxMsg", "frame rx_ts tx_ts sequence rx_status tx_status nack")
RxMsg.is_tx_result = property(lambda m: bool(m.sequence and m.tx_status and not m.rx_status))
Event = collections.namedtuple("Event", "event flags pa mask lost")


class Adapter:
    """/dev/cecN réel (ioctl/ouverture/fermeture/poll injectables pour les tests)."""

    def __init__(self, path, ioctl_fn=None, opener=None, closer=None, poll_factory=None):
        if not isinstance(path, str) or not DEV_RE.fullmatch(path):
            raise ValueError("bad_cec_device")
        self.path = path
        if ioctl_fn is None:
            import fcntl
            ioctl_fn = fcntl.ioctl
        self._ioctl = ioctl_fn
        self._closer = closer or os.close
        self.fd = (opener or self.default_open)(path)
        self._poll = (poll_factory or select.poll)()
        self._poll.register(self.fd, select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP)

    @staticmethod
    def default_open(path):
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(errno.ENODEV, "not_char_device")
        return fd

    def _call(self, name, buf):
        if self.fd is None:
            raise AdapterGone("closed")
        try:
            self._ioctl(self.fd, IOCTL[name], buf, True)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EBADF, errno.ENXIO):
                raise AdapterGone(name)
            raise
        return buf

    def caps(self):
        d, n, avail, caps, ver = struct.unpack(CAPS_FMT, bytes(self._call("CEC_ADAP_G_CAPS", bytearray(76))))
        return {"driver": d.split(b"\0", 1)[0].decode("ascii", "replace"),
                "name": n.split(b"\0", 1)[0].decode("ascii", "replace"),
                "available_log_addrs": avail, "capabilities": caps, "version": ver}

    def phys_addr(self):
        return struct.unpack("=H", bytes(self._call("CEC_ADAP_G_PHYS_ADDR", bytearray(2))))[0]

    def log_addrs(self):
        f = struct.unpack(LA_FMT, bytes(self._call("CEC_ADAP_G_LOG_ADDRS", bytearray(92))))
        return {"log_addr": list(f[0]), "log_addr_mask": f[1], "cec_version": f[2], "num_log_addrs": f[3],
                "vendor_id": f[4], "flags": f[5], "osd_name": f[6].split(b"\0", 1)[0].decode("ascii", "replace"),
                "primary_device_type": list(f[7])}

    def mode(self):
        return struct.unpack("=I", bytes(self._call("CEC_G_MODE", bytearray(4))))[0]

    def set_mode(self, mode):
        if mode not in (MODE_FOLLOWER, MODE_PLAIN):
            raise ForbiddenTx("mode_0x%02x" % mode)
        self._call("CEC_S_MODE", bytearray(struct.pack("=I", mode)))

    def wait(self, timeout_s):
        """-> (messages prêts, événements prêts) ; AdapterGone sur POLLERR/POLLHUP/POLLNVAL."""
        res = self._poll.poll(max(0, int(timeout_s * 1000)))
        msg = ev = False
        for _fd, m in res:
            if m & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                raise AdapterGone("poll")
            msg = msg or bool(m & select.POLLIN)
            ev = ev or bool(m & select.POLLPRI)
        return msg, ev

    def receive(self):
        buf = bytearray(56)          # timeout 0 + descripteur non bloquant : EAGAIN quand vide
        try:
            self._call("CEC_RECEIVE", buf)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise
        f = struct.unpack(MSG_FMT, bytes(buf))
        n = min(f[2], CEC_MAX_MSG_SIZE)
        return RxMsg(bytes(f[6][:n]), f[1], f[0], f[4], f[8], f[9], f[11])

    def dqevent(self):
        buf = bytearray(80)
        try:
            self._call("CEC_DQEVENT", buf)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise
        _ts, ev, flags, raw = struct.unpack(EVENT_FMT, bytes(buf))
        pa, mask, _conn = struct.unpack_from("=HHH", raw, 0)
        return Event(ev, flags, pa, mask, struct.unpack_from("=I", raw, 0)[0])

    def transmit(self, frame):
        """Émission NON bloquante (résultat rendu par CEC_RECEIVE) -> numéro de séquence. Aucune attente de
        réponse (reply=0), jamais CEC_MSG_FL_RAW."""
        frame = bytes(frame)
        if not 1 <= len(frame) <= CEC_MAX_MSG_SIZE:
            raise ForbiddenTx("length")
        buf = bytearray(struct.pack(MSG_FMT, 0, 0, len(frame), 0, 0, 0, frame.ljust(16, b"\0"), 0, 0, 0, 0, 0, 0, 0))
        self._call("CEC_TRANSMIT", buf)
        return struct.unpack_from("=I", buf, 24)[0]

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                self._closer(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------- moteur
def follower_files():
    """Fichiers dont dépend le suiveur en cours : ce module et le lanceur installé s'il est connu."""
    files = [os.path.abspath(__file__)]
    if sys.argv and os.path.basename(sys.argv[0] or "") == "opentv-pi":
        files.append(os.path.abspath(sys.argv[0]))
    return files


def installed_check(files):
    files = tuple(files)
    return lambda: all(os.path.isfile(f) for f in files)


def _log(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ms(ns):
    return round(ns / 1e6, 1)


class Follower:
    def __init__(self, devices, open_adapter=Adapter, mono_ns=time.monotonic_ns, sleep=time.sleep, lock=None,
                 unlock=os.close, log=None, canary=False, installed=lambda: True, stop=lambda: False):
        self.devices, self.open_adapter, self.mono_ns, self.sleep = devices, open_adapter, mono_ns, sleep
        self.lock, self.unlock = lock or (lambda: None), unlock
        self.log = log or _log
        self.canary = bool(canary)
        self.installed, self.stop = installed, stop
        self.counters = collections.Counter()
        self.ident = None
        self.status = None
        self._why = None
        self._warned = set()
        self.pending = collections.OrderedDict()
        self.tx_times = collections.deque()
        self.as_times = collections.deque()
        self._rate_logged_ns = None
        self._canary_window = (None, 0)
        self._stats_due = None
        self._stats_last = None
        self._scan_denied = False

    # ---- journal ------------------------------------------------------------------------------------------------
    def _canary(self, obj):
        if not self.canary:
            return
        now = self.mono_ns()
        start, n = self._canary_window
        if start is None or now - start >= int(3600e9):
            start, n = now, 0
        if n >= CANARY_LOG_MAX_PER_HOUR:
            self.counters["canary_log_suppressed"] += 1
        else:
            n += 1
            self.log(obj)
        self._canary_window = (start, n)

    def _stats(self, force=False):
        now = self.mono_ns()
        if self._stats_due is None:
            self._stats_due = now + int(STATS_EVERY_S * 1e9)
        if not force and now < self._stats_due:
            return
        self._stats_due = now + int(STATS_EVERY_S * 1e9)
        snap = dict(self.counters)
        if snap and snap != self._stats_last:
            self.log({"event": "cec_follower_stats", "counters": snap})
            self._stats_last = snap

    def _waiting(self, why, dev=None):
        if why != self._why:
            self.log({"event": "cec_follower_waiting", "reason": why, "device": dev})
        self._why = why

    # ---- service ------------------------------------------------------------------------------------------------
    def serve(self, max_cycles=None):
        """Boucle du service -> 0 (arrêt, désinstallation) ou EXIT_FATAL (échec véridique, unité failed)."""
        errs = abi_errors()
        if errs:
            return self._fatal("abi_mismatch", detail=errs[:3])
        self.log({"event": "cec_follower_start", "mode": "canary" if self.canary else "on", "version": VERSION})
        disc_i = reopen_i = busy = unreadable = denied = 0
        denied_in_streak = False
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            if self.stop():
                return 0
            if not self.installed():
                self.log({"event": "cec_follower_uninstalled"})
                return 0
            found = self.discover()
            if found is None:
                # accès refusé (course udev / DeviceAllow résolu trop tôt) : transitoire, relance systemd — jamais 78
                denied = denied + 1 if self._scan_denied else 0
                if self._why in UNREADABLE_WHY:
                    unreadable += 1
                    denied_in_streak = denied_in_streak or self._scan_denied
                else:
                    unreadable, denied_in_streak = 0, False
                if denied >= UNREADABLE_MAX or (unreadable >= UNREADABLE_MAX and denied_in_streak):
                    return self._transient("permission_denied", failures=max(denied, unreadable))
                if unreadable >= UNREADABLE_MAX:
                    return self._fatal("adapter_unreadable", failures=unreadable)
                self.sleep(DISCOVER_DELAYS_S[min(disc_i, len(DISCOVER_DELAYS_S) - 1)])
                disc_i += 1
                continue
            disc_i = unreadable = denied = 0
            denied_in_streak = False
            dev, ad = found
            t0 = self.mono_ns()
            why = self.hold(dev, ad)
            if why == "exclusive_follower_busy":
                busy += 1
                if busy >= BUSY_MAX:
                    return self._fatal(why, device=dev)
                self._waiting(why, dev)
                self.sleep(BUSY_RETRY_S)
                continue
            busy = 0
            if why in FATAL:
                return self._fatal(why, device=dev)
            if why in ("stopped", "uninstalled"):
                return 0
            if self.mono_ns() - t0 >= int(HEALTHY_HOLD_S * 1e9):
                reopen_i = 0
            self.sleep(REOPEN_DELAYS_S[min(reopen_i, len(REOPEN_DELAYS_S) - 1)])
            reopen_i += 1
        return None

    def _fatal(self, why, **extra):
        self.log(dict({"event": "cec_follower_failed", "reason": why, "exit": EXIT_FATAL}, **extra))
        return EXIT_FATAL

    def _transient(self, why, **extra):
        self.log(dict({"event": "cec_follower_failed", "reason": why, "exit": EXIT_TRANSIENT}, **extra))
        return EXIT_TRANSIENT

    def discover(self):
        """Premier adaptateur vc4 émetteur dont la configuration est la nôtre -> (dev, Adapter ouvert) ; sinon None
        (raison journalisée au changement). Lecture locale seulement (ioctl), aucun trafic sur le bus."""
        why = "no_adapter"
        self._scan_denied = False
        for dev in list(self.devices())[:cecboot.MAX_ADAPTERS]:
            try:
                ad = self.open_adapter(dev)
            except (OSError, ValueError) as e:
                if getattr(e, "errno", None) in PERMISSION_ERRNOS:
                    self._scan_denied = True
                    why = "permission_denied"
                else:
                    why = "open_failed"
                continue
            keep = False
            try:
                caps = ad.caps()
                if not caps["driver"].startswith("vc4") or not caps["capabilities"] & CEC_CAP_TRANSMIT:
                    why = "not_vc4"
                elif not config_ours(ad.log_addrs()):
                    why = "not_ours"
                else:
                    keep = True
                    return dev, ad
            except (OSError, AdapterGone):
                why = "read_failed"
            finally:
                if not keep:
                    ad.close()
        self._waiting(why)
        return None

    def revalidate(self, ad, dev):
        """Relit l'adaptateur -> None, ou raison de rendre le descripteur (configuration plus la nôtre)."""
        status, ident = identity_from(ad.caps(), ad.phys_addr(), ad.log_addrs())
        if status in ("not_ours", "not_vc4", "no_transmit"):
            self.ident, self.status = None, status
            self._waiting(status, dev)
            return "config_changed"
        changed = ident != self.ident or status != self.status
        self.ident, self.status = ident, status
        if not changed:
            return None
        if ident is None:
            self._waiting(status, dev)
            return None
        self._why = None
        self.log({"event": "cec_follower_ready", "device": dev, "physical_address": "%x.%x.%x.%x" % (
            ident.pa >> 12, (ident.pa >> 8) & 15, (ident.pa >> 4) & 15, ident.pa & 15),
            "logical_address": ident.primary})
        if not ident.flags & CEC_LOG_ADDRS_FL_ALLOW_RC_PASSTHRU and "rc_passthru_off" not in self._warned:
            self._warned.add("rc_passthru_off")
            self.log({"event": "cec_follower_warning", "warning": "rc_passthru_off"})
        return None

    def hold(self, dev, ad):
        """Tient l'adaptateur en EXCL_FOLLOWER jusqu'à une raison de le rendre -> raison. Mode rendu et descripteur
        fermé sur toute sortie (exception comprise)."""
        self.ident, self.status = None, None
        set_mode = False
        try:
            try:
                ad.set_mode(MODE_FOLLOWER)
            except AdapterGone:
                return "adapter_gone"
            except OSError as e:
                return "exclusive_follower_busy" if e.errno == errno.EBUSY else "s_mode_failed"
            set_mode = True
            if ad.mode() != MODE_FOLLOWER:
                return "mode_readback_mismatch"
            self.log({"event": "cec_follower_held", "device": dev})
            why = self.revalidate(ad, dev)
            if why:
                return why
            next_verify = self.mono_ns() + int(VERIFY_S * 1e9)
            while True:
                if self.stop():
                    return "stopped"
                if not self.installed():
                    self.log({"event": "cec_follower_uninstalled"})
                    return "uninstalled"
                now = self.mono_ns()
                msg, ev = ad.wait(max(0.0, (next_verify - now) / 1e9))
                # module retiré PENDANT l'attente : rien traité ni émis après une rétrogradation
                if not self.installed():
                    self.log({"event": "cec_follower_uninstalled"})
                    return "uninstalled"
                if ev:
                    why = self._events(ad, dev)
                    if why:
                        return why
                if msg:
                    why = self._messages(ad, dev)
                    if why:
                        return why
                if self.mono_ns() >= next_verify:
                    if ad.mode() != MODE_FOLLOWER:
                        return "mode_changed"
                    why = self.revalidate(ad, dev)
                    if why:
                        return why
                    self._trim_pending(self.mono_ns())
                    self._stats()
                    next_verify = self.mono_ns() + int(VERIFY_S * 1e9)
        except AdapterGone:
            return "adapter_gone"
        except ForbiddenTx:
            return "forbidden_tx"
        finally:
            if set_mode:
                try:
                    ad.set_mode(MODE_PLAIN)
                except (OSError, AdapterGone, ForbiddenTx):
                    pass                                    # close() libère EXCL_FOLLOWER de toute façon
            ad.close()
            self.ident, self.status = None, None
            # séquences de ce descripteur : leurs résultats ne seront jamais lus
            if self.pending:
                self.counters["tx_result_lost"] += len(self.pending)
                self.pending.clear()
            if set_mode:
                self.log({"event": "cec_follower_released", "device": dev})

    def _events(self, ad, dev):
        state = False
        for _ in range(DRAIN_MAX):
            e = ad.dqevent()
            if e is None:
                break
            if e.event == CEC_EVENT_STATE_CHANGE or e.flags & CEC_EVENT_FL_DROPPED_EVENTS:
                state = True
            elif e.event == CEC_EVENT_LOST_MSGS:
                self.counters["lost_msgs"] += 1
                self.log({"event": "cec_follower_lost", "messages": e.lost})
        return self.revalidate(ad, dev) if state else None

    def _messages(self, ad, dev):
        for _ in range(DRAIN_MAX):
            m = ad.receive()
            if m is None:
                return None
            if m.is_tx_result:
                self._tx_result(m)
                continue
            why = self._rx(ad, dev, m)
            if why:
                return why
        return None

    def _tx_result(self, m):
        p = self.pending.pop(m.sequence, None)
        ok = bool(m.tx_status & CEC_TX_STATUS_OK)
        self.counters["tx_ok" if ok else "tx_failed"] += 1
        e2e = _ms(m.tx_ts - p["rx_ts"]) if p and p["rx_ts"] and m.tx_ts >= p["rx_ts"] else None
        if e2e is not None and e2e >= 1000:
            self.counters["late_ge_1s"] += 1
        if p and p["kind"] == "active_source":
            self.log({"event": "cec_follower_reply_ack", "request": "set_stream_path", "ok": ok, "e2e_ms": e2e})
        self._canary({"event": "cec_follower_tx", "kind": p["kind"] if p else None, "ok": ok, "e2e_ms": e2e})

    def _skip(self, a, result, op, init):
        self.counters[result] += 1
        if a.kind == "active_source":
            self.log({"event": "cec_follower_reply", "request": "set_stream_path", "result": result})
        self._canary({"event": "cec_follower_rx", "opcode": op, "initiator": init, "action": "skip",
                      "intended": a.kind, "reason": result})

    def _trim_pending(self, now):
        """Borne dure PENDING_MAX (le plus ancien évincé) et expiration PENDING_TTL_S ; chaque perte comptée."""
        ttl = int(PENDING_TTL_S * 1e9)
        while self.pending:
            oldest = next(iter(self.pending.values()))
            if len(self.pending) <= PENDING_MAX and now - oldest["sent"] < ttl:
                break
            self.pending.popitem(last=False)
            self.counters["tx_result_lost"] += 1

    def _rate_ok(self, now, a):
        while self.tx_times and now - self.tx_times[0] >= int(60e9):
            self.tx_times.popleft()
        while self.as_times and now - self.as_times[0] >= int(3600e9):
            self.as_times.popleft()
        last_s = sum(1 for t in self.tx_times if now - t < int(TX_RATE_WINDOW_S * 1e9))
        if last_s >= TX_RATE_MAX or len(self.tx_times) >= TX_MINUTE_MAX or \
                (a.kind == "active_source" and len(self.as_times) >= AS_PER_HOUR_MAX):
            if self._rate_logged_ns is None or now - self._rate_logged_ns >= int(60e9):
                self._rate_logged_ns = now
                self.log({"event": "cec_follower_rate_limited"})
            return "rate_limited"
        if a.kind == "active_source" and self.as_times and now - self.as_times[-1] < int(AS_MIN_SPACING_S * 1e9):
            return "duplicate_request"
        return None

    def _rx(self, ad, dev, m):
        self.counters["rx"] += 1
        op = m.frame[1] if len(m.frame) > 1 else None
        init = m.frame[0] >> 4 if m.frame else None
        if self.ident is None:
            # adaptateur déconfiguré/débranché (événement traité avant la réception du même réveil) : rien émis
            self.counters["rx_not_ready"] += 1
            self._canary({"event": "cec_follower_rx", "opcode": op, "initiator": init, "action": "skip",
                          "intended": None, "reason": "rx_not_ready"})
            return None
        a = decide(m.frame, self.ident)
        if a.kind == "anomaly":
            self.log({"event": "cec_follower_anomaly", "reason": a.reason})
            return "mode_anomaly"
        if a.frame is None:
            self.counters["ignored"] += 1
            self._canary({"event": "cec_follower_rx", "opcode": op, "initiator": init,
                          "action": a.kind, "reason": a.reason})
            return None
        now = self.mono_ns()
        if m.rx_ts and now - m.rx_ts > int(STALE_MS * 1e6):
            self._skip(a, "stale", op, init)
            return None
        why = self._rate_ok(now, a)
        if why:
            self._skip(a, why, op, init)
            return None
        tok = None
        if a.kind not in LOCK_FREE_KINDS:
            tok = self._acquire()
            if tok is None:
                self._skip(a, "lock_busy", op, init)
                return None
        try:
            # juste avant l'émission, verrou tenu : événements en file (churn) puis relecture de l'adresse
            why = self._events(ad, dev)
            if why:
                return why
            fresh = identity_from(ad.caps(), ad.phys_addr(), ad.log_addrs())[1]
            if self.ident is None or fresh != self.ident or decide(m.frame, self.ident).frame != a.frame:
                self._skip(a, "adapter_churn", op, init)
                return self.revalidate(ad, dev)
            now = self.mono_ns()
            if m.rx_ts and now - m.rx_ts > int(STALE_MS * 1e6):
                self._skip(a, "stale", op, init)
                return None
            frame = guard_tx(a.frame, self.ident)
            try:
                seq = ad.transmit(frame)
            except OSError as e:
                self._skip(a, "tx_" + (errno.errorcode.get(e.errno, "error").lower()), op, init)
                return None
        finally:
            if tok is not None:
                self.unlock(tok)
        sent = self.mono_ns()
        self.tx_times.append(sent)
        if a.kind == "active_source":
            self.as_times.append(sent)
            self.counters["active_source_replies"] += 1
            self.log({"event": "cec_follower_reply", "request": "set_stream_path", "result": "sent",
                      "submit_ms": _ms(sent - m.rx_ts) if m.rx_ts else None})
        elif a.kind == "reply_power":
            self.counters["power_replies"] += 1
        else:
            self.counters["feature_aborts"] += 1
        self.pending[seq] = {"kind": a.kind, "rx_ts": m.rx_ts, "sent": sent}
        self._trim_pending(sent)
        self._canary({"event": "cec_follower_rx", "opcode": op, "initiator": init, "action": a.kind,
                      "submit_ms": _ms(sent - m.rx_ts) if m.rx_ts else None})
        return None

    def _acquire(self):
        deadline = self.mono_ns() + int(LOCK_WAIT_S * 1e9)
        tok = self.lock()
        while tok is None and self.mono_ns() + int(LOCK_POLL_S * 1e9) <= deadline:
            self.sleep(LOCK_POLL_S)
            tok = self.lock()
        return tok


def build(root="/", state_dir=cecboot.STATE_DIR, canary=False):
    """Suiveur de production : adaptateurs réels, verrou CEC partagé, arrêt propre si ce module est retiré."""
    return Follower(devices=lambda: cecboot.list_devices(root), lock=lambda: cecboot._lock(state_dir),
                    unlock=os.close, canary=canary, installed=installed_check(follower_files()))


def run(follower):
    """Point d'entrée du service : SIGTERM ⇒ sortie 0 (mode rendu, descripteur fermé dans hold) ; échec véridique ⇒
    78 (RestartPreventExitStatus) ; transitoire (accès refusé) ou erreur inattendue ⇒ 1 après journal (relance
    systemd toutes les 10 s). Toute sortie, SIGTERM compris, écrit le relevé final des compteurs s'il a changé."""
    def stop(*_):
        raise SystemExit(0)
    prev = signal.signal(signal.SIGTERM, stop)
    try:
        code = follower.serve()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — journal stable, jamais de trace brute du bus
        follower.log({"event": "cec_follower_failed", "reason": "internal_error", "error": type(e).__name__,
                      "exit": EXIT_TRANSIENT})
        return EXIT_TRANSIENT
    finally:
        signal.signal(signal.SIGTERM, prev)
        try:
            follower._stats(force=True)
        except Exception:  # noqa: BLE001 — le relevé final ne change jamais le code de sortie
            pass
    return 0 if code is None else code
