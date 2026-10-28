"""Aide « touches du lecteur » : seul composant OpenTV de lecture qui détient le groupe `input`.

Séparation des privilèges (revue de sécurité) :
- `opentv-player-keys.service` (compte `opentv-keys`, `SupplementaryGroups=input`, sans réseau ni capacité,
  `DeviceAllow=char-input r`) lit evdev (evkeys.py) SEULEMENT tant qu'un abonné authentifié est connecté ;
- `opentv-player.service` (compte `opentv-kiosk`, groupes video/render, jamais input) et son Chromium s'abonnent par
  ce socket Unix et ne reçoivent que des lignes `{"key": "back"|"exit"|"home"|"devtools"}`.

Authentification mutuelle par SO_PEERCRED : l'aide n'accepte que l'UID du contrôleur du lecteur (et root pour
l'administration) ; le client vérifie que le socket appartient bien au compte de l'aide (ou root). Protocole : une
ligne JSON du client — `{"op": "ping"}` (réponse `{"ok", "armed", "devices"}` puis fermeture ; hors abonnement,
`devices` vient d'une sonde qui ouvre, interroge EVIOCGBIT et referme aussitôt, sans lire aucun événement) ou
`{"op": "subscribe"}` (réponse `{"ok": true, "armed": true, "devices": n}` APRÈS le premier balayage réel, puis flux
d'événements `{"key": …}` et de comptes `{"devices": n}` jusqu'à la fermeture par l'une des parties). `devices` = nombre
de périphériques RÉELLEMENT ouverts qui exposent une touche de fermeture (CLOSE_KEYS) : 0 (EACCES, aucun périphérique
capable) ⇒ abonnement refusé `no_device` — la télécommande physique est la sortie prioritaire, le téléphone reste un
complément. Au plus MAX_CLIENTS abonnés, au plus RATE événements/s, durée d'abonnement bornée ; aucun autre code de
touche ne sort.
"""
import json
import os
import socket
import socketserver
import struct
import threading
import time

from .evkeys import REASONS, KeyWatcher, count_capable

SOCKET = "/run/opentv-player-keys/keys.sock"
MAX_CLIENTS = 2
MAX_REQ = 512
RATE, BURST = 10.0, 20.0
MAX_SUBSCRIPTION_S = 7 * 3600.0


def _noop(event, **fields):
    pass


def peer_uid(sock):
    """UID du pair d'un socket Unix (Linux SO_PEERCRED ; macOS LOCAL_PEERCRED pour les tests) ; None si inconnu."""
    try:
        data = sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), struct.calcsize("3i"))
        return struct.unpack("3i", data)[1]
    except (OSError, struct.error):
        pass
    try:   # macOS : LOCAL_PEERCRED -> struct xucred {u_int cr_version; uid_t cr_uid; ...}
        data = sock.getsockopt(0, 0x001, 76)
        return struct.unpack_from("Ii", data)[1]
    except (OSError, struct.error):
        return None


class _Client:
    def __init__(self, sock):
        self.sock, self.lock, self.alive = sock, threading.Lock(), True
        self.ready = False   # réponse d'abonnement envoyée : seulement ensuite les événements et les comptes

    def send(self, obj, hello=False):
        with self.lock:
            if not self.alive or not (self.ready or hello):
                return False
            try:
                self.sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
                return True
            except OSError:
                self.alive = False
                return False


class Hub:
    """Abonnés authentifiés + surveillance evdev armée seulement s'il y a au moins un abonné."""

    def __init__(self, watcher_factory=KeyWatcher, log=_noop, mono=time.monotonic, max_clients=MAX_CLIENTS,
                 rate=RATE, burst=BURST, probe=count_capable, scan_wait_s=1.5):
        self.factory, self.log, self.mono = watcher_factory, log, mono
        self.max_clients, self.rate, self.burst = max_clients, rate, burst
        self.probe, self.scan_wait_s = probe, scan_wait_s
        self.lock = threading.Lock()
        self.clients = []
        self.watcher = None
        self.tokens, self.t_last = burst, mono()

    def armed(self):
        with self.lock:
            return self.watcher is not None

    def devices(self):
        """Périphériques capables : compte réel du balayage en cours, sinon sonde ponctuelle (rien n'est lu)."""
        with self.lock:
            w = self.watcher
        if w is not None:
            return int(getattr(w, "count", 0) or 0)
        try:
            return int(self.probe())
        except Exception:
            return 0

    def add(self, c):
        """-> (None, n) si abonné avec n ≥ 1 périphérique capable ouvert ; (« busy » | « no_device », 0) sinon."""
        with self.lock:
            if len(self.clients) >= self.max_clients:
                return "busy", 0
            self.clients.append(c)
            if self.watcher is None:
                self.watcher = self.factory(self._on_key, on_devices=self._on_devices).start()
                self.log("player_keys_armed")
            w = self.watcher
        ready = getattr(w, "ready", None)
        if ready is not None:
            ready.wait(self.scan_wait_s)   # premier balayage réel : le compte annoncé est celui des fd ouverts
        n = int(getattr(w, "count", 0) or 0)
        if n < 1:
            self.remove(c)
            self.log("player_keys_no_device")
            return "no_device", 0
        return None, n

    def remove(self, c):
        w = None
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)
            if not self.clients and self.watcher is not None:
                w, self.watcher = self.watcher, None
        if w is not None:   # aucun abonné : tous les périphériques evdev refermés (par le thread de surveillance)
            w.stop()
            self.log("player_keys_disarmed")

    def _allow(self):
        now = self.mono()
        self.tokens = min(self.burst, self.tokens + (now - self.t_last) * self.rate)
        self.t_last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def _on_key(self, why):
        if why not in REASONS:
            return
        with self.lock:
            if not self._allow():
                return
            targets = list(self.clients)
        for c in targets:
            c.send({"key": why})

    def _on_devices(self, n):
        with self.lock:
            targets = list(self.clients)
        for c in targets:
            c.send({"devices": int(n)})

    def close(self):
        with self.lock:
            cs, self.clients = list(self.clients), []
            w, self.watcher = self.watcher, None
        for c in cs:
            c.alive = False
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if w is not None:
            w.stop()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        self.request.settimeout(5)
        uid = peer_uid(self.request)
        if uid is None or uid not in srv.allowed_uids:
            srv.log("player_keys_peer_refused")
            return self._send({"ok": False, "error": "forbidden"})
        buf = b""
        try:
            while b"\n" not in buf and len(buf) <= MAX_REQ:
                chunk = self.request.recv(256)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            return
        try:
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8")) if b"\n" in buf else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            req = None
        if not isinstance(req, dict) or req.get("op") not in ("ping", "subscribe"):
            return self._send({"ok": False, "error": "bad_request"})
        if req["op"] == "ping":
            return self._send({"ok": True, "armed": srv.hub.armed(), "devices": srv.hub.devices()})
        c = _Client(self.request)
        err, n = srv.hub.add(c)
        if err:
            return self._send({"ok": False, "error": err, "devices": 0})
        try:
            c.send({"ok": True, "armed": True, "devices": n}, hello=True)
            c.ready = True
            m = srv.hub.devices()
            if m != n:   # compte changé entre la réponse et l'activation : rattrapé tout de suite
                c.send({"devices": m})
            end = srv.hub.mono() + srv.max_subscription_s
            self.request.settimeout(0.5)
            while c.alive and not srv.stopping.is_set() and srv.hub.mono() < end:
                try:
                    if not self.request.recv(64):   # fermeture par le contrôleur (fin de séance)
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            c.alive = False
            srv.hub.remove(c)

    def _send(self, obj):
        try:
            self.request.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, hub, allowed_uids, log=_noop, max_subscription_s=MAX_SUBSCRIPTION_S):
        self.hub, self.allowed_uids, self.log = hub, frozenset(allowed_uids), log
        self.max_subscription_s = max_subscription_s
        self.stopping = threading.Event()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        old = os.umask(0o111)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o666)   # l'accès réel est décidé par SO_PEERCRED (contrôleur du lecteur, root)


def serve(path=SOCKET, allowed_uids=(0,), log=_noop, stop=None, watcher_factory=KeyWatcher):
    hub = Hub(watcher_factory=watcher_factory, log=log)
    srv = Server(path, hub, allowed_uids, log=log)
    threading.Thread(target=srv.serve_forever, daemon=True, name="player-keys-sock").start()
    stop = stop or threading.Event()
    log("player_keys_ready")
    stop.wait()
    srv.stopping.set()
    hub.close()
    srv.shutdown()
    srv.server_close()
    try:
        os.unlink(path)
    except OSError:
        pass


class KeyClient:
    """Abonnement du contrôleur du lecteur pendant UNE séance (même interface que l'ancien KeyWatcher).

    `armed` est posé dès que l'aide a confirmé l'abonnement AVEC au moins un périphérique capable réellement ouvert
    (`devices` ≥ 1) ; il retombe si la liaison est perdue ou si le compte tombe à 0 (reconnexion automatique toutes
    les `retry_s`, l'aide redémarrant avec Restart=always). `server_uids` : UID admis pour le propriétaire du socket
    de l'aide (None = non vérifié, tests seulement)."""

    def __init__(self, on_key, path=SOCKET, server_uids=None, retry_s=0.5, connect_timeout=3.0):
        # connect_timeout > Hub.scan_wait_s : la réponse d'abonnement arrive après le premier balayage réel
        self.on_key, self.path, self.server_uids = on_key, path, server_uids
        self.retry_s, self.connect_timeout = retry_s, connect_timeout
        self.stop_ev, self.armed = threading.Event(), threading.Event()
        self.sock = None
        self.lock = threading.Lock()
        self.thread = None
        self.refused = None
        self.devices = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="player-keys-client")
        self.thread.start()
        return self

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.connect_timeout)
        try:
            s.connect(self.path)
            if self.server_uids is not None and peer_uid(s) not in self.server_uids:
                self.refused = "server_uid"
                raise OSError("server_uid")
            s.sendall(b'{"op": "subscribe"}\n')
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                chunk = s.recv(1024)
                if not chunk:
                    raise OSError("eof")
                buf += chunk
            hello, _, rest = buf.partition(b"\n")
            doc = json.loads(hello.decode("utf-8"))
            n = doc.get("devices") if isinstance(doc, dict) else None
            if not (isinstance(doc, dict) and doc.get("ok") is True and doc.get("armed") is True and
                    isinstance(n, int) and not isinstance(n, bool) and n >= 1):
                self.refused = str(doc.get("error") if isinstance(doc, dict) and doc.get("error") else
                                   ("no_device" if isinstance(doc, dict) and doc.get("ok") is True else "bad"))[:16]
                raise OSError("refused")
            self.devices = n
            return s, rest
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            s.close()
            return None, b""

    def _run(self):
        while not self.stop_ev.is_set():
            s, buf = self._connect()
            if s is None:
                self.stop_ev.wait(self.retry_s)
                continue
            with self.lock:
                if self.stop_ev.is_set():
                    s.close()
                    return
                self.sock = s
            self.refused = None
            self.armed.set()
            s.settimeout(0.5)
            try:
                while not self.stop_ev.is_set():
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._line(line)
                    if len(buf) > 4096:
                        break
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            finally:
                self.armed.clear()
                self.devices = 0
                with self.lock:
                    self.sock = None
                s.close()
            if not self.stop_ev.is_set():
                self.stop_ev.wait(self.retry_s)

    def _line(self, line):
        try:
            doc = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return
        if isinstance(doc, dict) and "devices" in doc:
            n = doc.get("devices")
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                self.devices = n
                if n >= 1:
                    self.armed.set()
                else:   # plus aucune télécommande physique capable : la séance le voit (keys_lost)
                    self.armed.clear()
            return
        why = doc.get("key") if isinstance(doc, dict) else None
        if why in REASONS and not self.stop_ev.is_set():
            try:
                self.on_key(why)
            except Exception:   # un gestionnaire défaillant ne coupe jamais la liaison
                pass

    def stop(self):
        self.stop_ev.set()
        with self.lock:
            s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(2)
        self.armed.clear()


def ping(path=SOCKET, server_uids=None, timeout=1.0, min_devices=1):
    """Aide joignable, authentifiée ET au moins `min_devices` périphérique capable (CLOSE_KEYS) ouvrable ?"""
    info = ping_info(path, server_uids, timeout)
    return bool(info) and info.get("devices", 0) >= min_devices


def ping_info(path=SOCKET, server_uids=None, timeout=1.0):
    """-> {"armed": bool, "devices": n} ou None (aide injoignable, non authentifiée, réponse invalide)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        if server_uids is not None and peer_uid(s) not in server_uids:
            return None
        s.sendall(b'{"op": "ping"}\n')
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
        doc = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        if not (isinstance(doc, dict) and doc.get("ok") is True):
            return None
        n = doc.get("devices")
        return {"armed": doc.get("armed") is True,
                "devices": n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0}
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    finally:
        s.close()
