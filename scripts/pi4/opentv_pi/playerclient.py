"""Client du contrôleur du lecteur (socket Unix authentifié par SO_PEERCRED côté contrôleur).

Une requête JSON par connexion, réponse JSON bornée. Aucune requête n'est journalisée (elle peut porter l'adresse
validée de la fiche, destinée au seul contrôleur)."""
import json
import socket

SOCKET = "/run/opentv-player/player.sock"
MAX_RESP = 65536


class PlayerUnavailable(Exception):
    pass


class PlayerClient:
    def __init__(self, path=SOCKET, timeout=3.0):
        self.path, self.timeout = path, timeout

    def call(self, req, timeout=None):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout or self.timeout)
        try:
            s.connect(self.path)
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf and len(buf) <= MAX_RESP:
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
        except OSError as e:
            raise PlayerUnavailable(type(e).__name__)
        finally:
            s.close()
        try:
            out = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise PlayerUnavailable("bad_response")
        if not isinstance(out, dict):
            raise PlayerUnavailable("bad_response")
        if out.get("error") == "forbidden":
            raise PlayerUnavailable("forbidden")
        return out

    def ping(self):
        """Lecture possible seulement si le contrôleur répond ET se dit promu (porte terrain) ET l'aide des touches
        est armable : un contrôleur ancien ou incomplet (champs absents) est traité comme indisponible."""
        try:
            r = self.call({"op": "ping"}, timeout=1.0)
        except PlayerUnavailable:
            return False
        return r.get("ok") is True and r.get("promoted") is True and r.get("keys") is True
