"""X TV — sortie « appareil » de x.com vers /tv par la touche Retour Samsung (KEY_EXIT 174), sans JavaScript de page.

Terrain (Pi 0.7.7) : la touche Retour de la télécommande Samsung arrive en KEY_EXIT (174) sur le périphérique d'entrée
CEC du noyau (vc4-hdmi), mais Chromium/Wayland ne délivre AUCUN keydown à la page : nav.js ne peut rien et Alex reste
coincé sur x.com. Relancer opentv-kiosk.service ramène toujours /tv (prouvé terrain). Chaîne bornée, trois comptes :

1. opentv.service (compte opentv) tient le témoin « X ouvert » `/run/opentv/x-active` (0644, JSON {v, boot_id}) :
   posé SEULEMENT par POST /api/tv/x/presence {"active": true} (portes TV complètes : loopback, Host, UID kiosque, clé
   TV) juste avant que /tv n'ouvre X ; retiré à chaque chargement de /tv et au démarrage du service.
2. opentv-x-escape.service (compte opentv-keys, groupe input par l'unité seulement, sans réseau ni capacité) : lit le
   témoin toutes les POLL_S ; evdev armé SEULEMENT pendant que le témoin est valide, et SEULEMENT sur les périphériques
   CEC `vc4-hdmi*` exposant KEY_EXIT (choisis dynamiquement, rebalayés : débranchement/rebranchement) — jamais un
   clavier : la saisie privée d'Alex n'est jamais lue. Seul KEY_EXIT appui (value=1) compte : répétitions/relâchés
   ignorés, au plus une demande par HELPER_MIN_INTERVAL_S. Demande = fichier `request` au contenu fixe, posé
   atomiquement dans /run/opentv-x-escape (son dossier d'exécution).
3. opentv-x-escape.path -> opentv-x-escape-action.service (root, oneshot, sans réseau, sans périphérique) : demande
   vérifiée (régulier, lien unique, propriétaire opentv-keys, contenu exact) puis TOUJOURS retirée ; témoin revérifié
   (propriétaire opentv, même boot) ; au plus une relance par ACTION_MIN_INTERVAL_S ; puis UNE commande exacte
   RESTART_ARGV. Témoin absent (page /tv) : rien, jamais de relance inconditionnelle.

Fail-safe : tout doute (témoin illisible, lien, propriétaire, boot_id, demande invalide) = aucune action. Rien de
saisi n'est lu, journalisé ni transmis ; aucun code de touche n'est journalisé.
"""
import fcntl
import json
import os
import re
import stat
import struct
import subprocess
import threading
import time

from . import bootid, evkeys

MARKER = "x-active"
PRESENCE_PATH = "/api/tv/x/presence"
REQUEST = "request"
REQUEST_BODY = '{"op":"x_exit","v":1}\n'
REQUEST_DIR = "/run/opentv-x-escape"
STAMP_DIR = "/run/opentv-x-escape-action"
STAMP = "last"
MAX_FILE = 256
KEY_EXIT = 174
POLL_S = 0.5
HELPER_MIN_INTERVAL_S = 3.0
ACTION_MIN_INTERVAL_S = 10.0
RESTART_TIMEOUT_S = 15
RESTART_ARGV = ("systemctl", "restart", "--no-block", "opentv-kiosk.service")
DEVICE_NAME_RE = re.compile(r"vc4-hdmi(-[0-9])?")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _noop(event, **fields):
    pass


def _boot_id():
    return bootid.read()


def _read_small(dfd, name, owner_uid):
    """Contenu d'un petit fichier régulier, lien unique, propriétaire attendu (None = non vérifié) ; None sinon."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_FILE:
            return None
        if owner_uid is not None and st.st_uid != owner_uid:
            return None
        data = os.read(fd, MAX_FILE + 1)
        return data if len(data) <= MAX_FILE else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _write_atomic(dfd, name, data, exclusive=False):
    """Écrit `name` (0644) par un brouillon puis rename dans le MÊME dossier ouvert (aucun lien suivi)."""
    tmp = name + ".tmp"
    try:
        os.unlink(tmp, dir_fd=dfd)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
    try:
        os.fchmod(fd, 0o644)   # UMask=0077 des unités : lisible par le compte de l'aide / root
        os.write(fd, data)
    finally:
        os.close(fd)
    if exclusive:
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
            os.unlink(tmp, dir_fd=dfd)
            return False   # une demande attend déjà : jamais réécrite
        except FileNotFoundError:
            pass
    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    return True


# --------------------------------------------------------------------------------------------- témoin (opentv)
def set_marker(run_dir, active, boot_id=None):
    """opentv.service : pose (active) ou retire le témoin « X ouvert ». -> True si l'état demandé est en place."""
    try:
        dfd = os.open(run_dir, _DIR_FLAGS)
    except OSError:
        return False
    try:
        if not active:
            try:
                os.unlink(MARKER, dir_fd=dfd)
            except FileNotFoundError:
                pass
            return True
        b = _boot_id() if boot_id is None else boot_id
        body = json.dumps({"v": 1, "boot_id": b}, sort_keys=True).encode("utf-8")
        return _write_atomic(dfd, MARKER, body)
    except OSError:
        return False
    finally:
        os.close(dfd)


def read_marker(run_dir, owner_uid, boot_id=None):
    """Témoin valide ? (régulier, lien unique, propriétaire `owner_uid`, ≤ MAX_FILE, JSON v=1, même boot_id)."""
    try:
        dfd = os.open(run_dir, _DIR_FLAGS)
    except OSError:
        return False
    try:
        data = _read_small(dfd, MARKER, owner_uid)
    finally:
        os.close(dfd)
    if data is None:
        return False
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("boot_id"), str):
        return False
    b = _boot_id() if boot_id is None else boot_id
    return doc["boot_id"] == b


# --------------------------------------------------------------------------------------------- aide (opentv-keys)
class ExitDecoder:
    """Seul KEY_EXIT appuyé (value=1) produit « exit » ; tout autre code, répétition ou relâché : rien."""

    def feed(self, data):
        out = []
        for off in range(0, len(data) - evkeys.EVENT_SIZE + 1, evkeys.EVENT_SIZE):
            _, _, typ, code, value = struct.unpack_from(evkeys.EVENT_FMT, data, off)
            if typ == evkeys.EV_KEY and code == KEY_EXIT and value == 1:
                out.append("exit")
        return out


def _eviocgname(length=256):
    return (2 << 30) | (length << 16) | (ord("E") << 8) | 0x06


def device_name(fd):
    buf = bytearray(256)
    try:
        n = fcntl.ioctl(fd, _eviocgname(len(buf)), buf, True)
    except OSError:
        return None
    raw = bytes(buf[:n if isinstance(n, int) and 0 < n <= len(buf) else len(buf)]).split(b"\0", 1)[0]
    return raw.decode("utf-8", "replace")


def make_device_filter(name_fn=device_name, keys_fn=evkeys._has_any_key):
    """Périphérique retenu SEULEMENT si son nom est exactement `vc4-hdmi[-N]` (adaptateur CEC du Pi) et qu'il expose
    KEY_EXIT : jamais un clavier, une souris ou une télécommande USB (aucune saisie lue)."""
    def ok(fd):
        name = name_fn(fd)
        return isinstance(name, str) and bool(DEVICE_NAME_RE.fullmatch(name)) and bool(keys_fn(fd, (KEY_EXIT,)))
    return ok


def _watcher(on_key, decoder_factory):
    return evkeys.KeyWatcher(on_key, has_key=make_device_filter(), decoder_factory=decoder_factory)


def write_request(request_dir):
    """-> "written" | "pending" | "failed" (dossier absent, lien, écriture impossible)."""
    try:
        dfd = os.open(request_dir, _DIR_FLAGS)
    except OSError:
        return "failed"
    try:
        return "written" if _write_atomic(dfd, REQUEST, REQUEST_BODY.encode("utf-8"), exclusive=True) else "pending"
    except OSError:
        return "failed"
    finally:
        os.close(dfd)


class Helper:
    """Arme la surveillance evdev (vc4-hdmi, KEY_EXIT seul) uniquement pendant que le témoin « X ouvert » est valide."""

    def __init__(self, marker_dir, request_dir, service_uid, watcher_factory=_watcher, mono=time.monotonic,
                 log=_noop, boot_id=_boot_id, min_interval_s=HELPER_MIN_INTERVAL_S):
        self.marker_dir, self.request_dir, self.service_uid = marker_dir, request_dir, service_uid
        self.factory, self.mono, self.log, self.boot_id = watcher_factory, mono, log, boot_id
        self.min_interval_s = min_interval_s
        self.lock = threading.Lock()
        self.watcher = None
        self.last = None

    def active(self):
        return read_marker(self.marker_dir, self.service_uid, boot_id=self.boot_id())

    def tick(self):
        on = self.active()
        w = None
        with self.lock:
            if on and self.watcher is None:
                self.watcher = self.factory(self._on_key, decoder_factory=ExitDecoder).start()
                self.log("x_escape_armed")
            elif not on and self.watcher is not None:
                w, self.watcher = self.watcher, None
        if w is not None:   # /tv rechargée : périphériques refermés par le thread de surveillance
            w.stop()
            self.log("x_escape_disarmed")

    def _on_key(self, why):
        if why != "exit":
            return
        if not self.active():   # /tv rechargée entre-temps : jamais de demande
            return
        with self.lock:
            now = self.mono()
            if self.last is not None and now - self.last < self.min_interval_s:
                self.log("x_escape_rate_limited")
                return
            self.last = now
        r = write_request(self.request_dir)
        if r == "written":
            self.log("x_escape_requested")
        elif r == "pending":
            self.log("x_escape_pending")
        else:
            self.log("x_escape_request_failed")

    def close(self):
        with self.lock:
            w, self.watcher = self.watcher, None
        if w is not None:
            w.stop()

    def run(self, stop, poll_s=POLL_S):
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception as e:   # jamais une sortie silencieuse : l'unité redémarre (Restart=always)
                    self.log("x_escape_tick_error", reason=type(e).__name__)
                stop.wait(poll_s)
        finally:
            self.close()


# --------------------------------------------------------------------------------------------- action (root)
def take_request(request_dir, keys_uid):
    """-> None (aucune demande), True (valide), False (invalide). Toute demande trouvée est retirée."""
    try:
        dfd = os.open(request_dir, _DIR_FLAGS)
    except OSError:
        return None
    try:
        try:
            os.stat(REQUEST, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        data = _read_small(dfd, REQUEST, keys_uid)
        try:
            os.unlink(REQUEST, dir_fd=dfd)   # nom seul, jamais suivi
        except OSError:
            pass
        return data == REQUEST_BODY.encode("utf-8")
    finally:
        os.close(dfd)


def default_restart():
    try:
        p = subprocess.run(list(RESTART_ARGV), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=RESTART_TIMEOUT_S)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _rate_ok(stamp_dir, now, min_interval_s):
    """Horloge monotone du système (commune aux processus d'un même démarrage). Dossier illisible : refus."""
    try:
        dfd = os.open(stamp_dir, _DIR_FLAGS)
    except OSError:
        return False
    try:
        data = _read_small(dfd, STAMP, os.getuid())
        try:
            last = float(data.decode("ascii")) if data else None
        except (ValueError, UnicodeDecodeError):
            last = None
        if last is not None and 0 <= now - last < min_interval_s:
            return False
        _write_atomic(dfd, STAMP, ("%.3f" % now).encode("ascii"))
        return True
    except OSError:
        return False
    finally:
        os.close(dfd)


def run_action(request_dir, marker_dir, stamp_dir, keys_uid, service_uid, restart=default_restart,
               mono=time.monotonic, log=_noop, boot_id=_boot_id, min_interval_s=ACTION_MIN_INTERVAL_S):
    req = take_request(request_dir, keys_uid)
    if req is None:
        return {"ok": True, "action": "none"}
    if not req:
        log("x_escape_refused", reason="request")
        return {"ok": False, "action": "refused", "reason": "request"}
    if not read_marker(marker_dir, service_uid, boot_id=boot_id()):
        log("x_escape_skipped", reason="not_x")
        return {"ok": True, "action": "skipped", "reason": "not_x"}
    if not _rate_ok(stamp_dir, mono(), min_interval_s):
        log("x_escape_skipped", reason="rate_limited")
        return {"ok": True, "action": "skipped", "reason": "rate_limited"}
    ok = bool(restart())
    log("x_escape_kiosk_restart", reason="ok" if ok else "failed")
    return {"ok": ok, "action": "restart"}
