"""Lecture evdev des touches « fermer la lecture » et des raccourcis DevTools (lecture seule, sans saisie exclusive).

Utilisé UNIQUEMENT par l'aide `opentv-player-keys` (playerkeys.py, compte `opentv-keys`, groupe `input` donné par son
unité systemd). Ni le contrôleur du lecteur ni aucun Chromium n'ont le groupe `input` : ils reçoivent seulement des
événements typés par un socket Unix authentifié.

Pendant la lecture, la fenêtre du lecteur a le focus clavier sous Cage : la page /tv ne voit plus les touches. L'aide
lit donc en parallèle les périphériques qui exposent une touche Retour/Quitter/Accueil — télécommande de la TV par
HDMI-CEC (vc4-hdmi), télécommande USB 2,4 GHz (HID), clavier — et ne rapporte QUE des raisons bornées :
`back`, `exit`, `home` (appuis, value=1) et `devtools` (F12, Ctrl+Maj+I/J/C, Ctrl+U, Méta+Alt+I/J/C). Aucune touche
n'est injectée ni retenue (pas d'EVIOCGRAB : un raccourci DevTools atteint toujours Chromium, le contrôleur ferme alors
toute cible DevTools — voir playerd), aucun autre code n'est transmis, rien n'est lu hors d'une séance.
"""
import fcntl
import glob
import os
import select
import struct
import threading
import time

EV_KEY = 0x01
CLOSE_KEYS = {1: "back",      # KEY_ESC (clavier, mini-télécommandes)
              14: "back",     # KEY_BACKSPACE (repli clavier, comme keys.js)
              158: "back",    # KEY_BACK (HID, CEC « Retour »)
              174: "exit",    # KEY_EXIT (CEC « Exit »)
              206: "exit",    # KEY_CLOSE
              172: "home",    # KEY_HOMEPAGE (HID/CEC Accueil)
              0x113: "back"}  # BTN_SIDE : « précédent » d'une air mouse
KEY_F12, KEY_I, KEY_J, KEY_C, KEY_U = 88, 23, 36, 46, 22
CTRL = (29, 97)        # KEY_LEFTCTRL, KEY_RIGHTCTRL
SHIFT = (42, 54)       # KEY_LEFTSHIFT, KEY_RIGHTSHIFT
ALT = (56, 100)        # KEY_LEFTALT, KEY_RIGHTALT
META = (125, 126)      # KEY_LEFTMETA, KEY_RIGHTMETA
MODIFIERS = CTRL + SHIFT + ALT + META
REASONS = ("back", "exit", "home", "devtools")
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
KEY_MAX = 0x2FF
WATCHED = set(CLOSE_KEYS) | {KEY_F12}


def _eviocgbit(ev, length):
    return (2 << 30) | (length << 16) | (ord("E") << 8) | (0x20 + ev)


def _has_any_key(fd, codes):
    buf = bytearray((KEY_MAX + 8) // 8)
    try:
        fcntl.ioctl(fd, _eviocgbit(EV_KEY, len(buf)), buf, True)
    except OSError:
        return False
    return any(buf[c // 8] & (1 << (c % 8)) for c in codes)


def has_watched_key(fd):
    """Le périphérique expose une touche utile à surveiller, incluant F12."""
    return _has_any_key(fd, WATCHED)


def has_close_key(fd):
    """Le périphérique peut réellement fermer la lecture (Retour/Quitter/Accueil), pas seulement ouvrir DevTools."""
    return _has_any_key(fd, CLOSE_KEYS)


class KeyDecoder:
    """État des modificateurs d'UN périphérique ; `feed(données)` -> raisons bornées (REASONS) dans l'ordre."""

    def __init__(self):
        self.down = set()

    def _mods(self, group):
        return any(m in self.down for m in group)

    def feed(self, data):
        out = []
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, typ, code, value = struct.unpack_from(EVENT_FMT, data, off)
            if typ != EV_KEY:
                continue
            if code in MODIFIERS:
                if value == 0:
                    self.down.discard(code)
                else:
                    self.down.add(code)
                continue
            if value != 1:   # relâché (0) ou répétition (2) : jamais une nouvelle action
                continue
            if code == KEY_F12:
                out.append("devtools")
            elif code in (KEY_I, KEY_J, KEY_C) and ((self._mods(CTRL) and self._mods(SHIFT)) or
                                                    (self._mods(META) and self._mods(ALT))):
                out.append("devtools")
            elif code == KEY_U and self._mods(CTRL):
                out.append("devtools")   # code source de la page (view-source: est aussi bloqué par la politique)
            elif code in CLOSE_KEYS and not (self._mods(CTRL) or self._mods(ALT) or self._mods(META)):
                out.append(CLOSE_KEYS[code])
        return out


def parse_events(data):
    """-> raisons pour un bloc d'événements entiers lu sur un périphérique sans état préalable (tests, outils)."""
    return KeyDecoder().feed(data)


def count_capable(pattern="/dev/input/event*", opener=None, has_key=has_close_key):
    """Nombre de périphériques evdev OUVRABLES qui exposent une touche de fermeture (sonde : ouverture, EVIOCGBIT,
    fermeture immédiate ; aucun événement lu). EACCES/ENOENT : périphérique non compté."""
    opener = opener or (lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
    n = 0
    for p in sorted(glob.glob(pattern))[:64]:
        try:
            fd = opener(p)
        except OSError:
            continue
        try:
            if has_key(fd):
                n += 1
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return n


class KeyWatcher:
    """Thread de surveillance actif seulement pendant une séance ; `on_key(raison)` pour chaque raison décodée.

    Propriété des descripteurs : SEUL le thread de surveillance ouvre, lit et ferme les périphériques (fermeture dans
    son `finally`) ; `stop()` ne fait que signaler et attendre — aucun `close` concurrent d'un `select`/`read` en cours
    (un numéro de descripteur réutilisé ailleurs ne peut donc jamais être lu ni fermé par erreur).
    `ready` est posé après le premier balayage ; `count` = nombre de périphériques RÉELLEMENT ouverts qui exposent
    une touche de fermeture ; `on_devices(n)` est appelé à chaque changement de ce nombre."""

    def __init__(self, on_key, pattern="/dev/input/event*", rescan_s=0.5, opener=None, has_key=has_close_key,
                 on_devices=None):
        self.on_key, self.pattern, self.rescan_s = on_key, pattern, rescan_s
        self.opener = opener or (lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
        self.has_key = has_key
        self.on_devices = on_devices
        self.stop_ev = threading.Event()
        self.ready = threading.Event()
        self.fds = {}
        self.decoders = {}
        self.count = 0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="close-keys")
        self.thread.start()
        return self

    def stop(self, timeout=2.0):
        """Signale l'arrêt et attend le thread (qui ferme lui-même tous ses descripteurs). -> True si terminé."""
        self.stop_ev.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
            return not t.is_alive()
        return True

    def _publish(self):
        n = len(self.fds)
        if n != self.count:
            self.count = n
            if self.on_devices is not None:
                try:
                    self.on_devices(n)
                except Exception:   # un abonné défaillant ne coupe jamais la surveillance
                    pass

    def _scan(self):
        for p in sorted(glob.glob(self.pattern))[:64]:
            if self.stop_ev.is_set():
                return
            if p in self.fds:
                continue
            try:
                fd = self.opener(p)
            except OSError:   # EACCES, débranché… : jamais compté
                continue
            try:
                ok = self.has_key(fd)
            except Exception:
                ok = False
            if ok:
                self.fds[p] = fd
                self.decoders[fd] = KeyDecoder()
            else:
                os.close(fd)

    def _drop(self, fd):
        for p, f in list(self.fds.items()):
            if f == fd:
                del self.fds[p]
        self.decoders.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _run(self):
        try:
            next_scan = 0.0
            while not self.stop_ev.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    self._scan()
                    next_scan = now + self.rescan_s
                    self._publish()
                    self.ready.set()
                fds = list(self.fds.values())
                if not fds:
                    self.stop_ev.wait(0.2)
                    continue
                try:
                    ready, _, _ = select.select(fds, [], [], 0.2)
                except (OSError, ValueError):
                    ready = []
                for fd in ready:
                    try:
                        data = os.read(fd, EVENT_SIZE * 64)
                    except BlockingIOError:
                        continue
                    except OSError:   # périphérique débranché
                        self._drop(fd)
                        continue
                    if not data:
                        self._drop(fd)
                        continue
                    dec = self.decoders.get(fd)
                    for why in (dec.feed(data) if dec else []):
                        if self.stop_ev.is_set():
                            break
                        self.on_key(why)
                if len(self.fds) != self.count:
                    self._publish()
        finally:   # seul propriétaire des descripteurs : fermés ici, jamais par stop()
            for fd in list(self.fds.values()):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.fds.clear()
            self.decoders.clear()
            self.count = 0
            self.ready.set()
