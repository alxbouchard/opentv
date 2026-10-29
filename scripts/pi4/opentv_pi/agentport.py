"""Agent Port : socket Unix locale (aucun port réseau). Une requête JSON par ligne, une réponse par ligne.

Requête : {"v":1, "agent":"muse", "token":"…", "op":"call"|"whoami", "tool":"recommend", "args":{…}}
Réponse : {"ok":true, "result":{…}} | {"ok":false, "error":"<code>"}
Contrôles : socket 0660 groupe opentv-agent, jeton par agent (hash au registre), scope par outil, validation stricte,
débit et verrouillage après trop d'échecs **par UID du pair** (SO_PEERCRED / LOCAL_PEERCRED) : un compte local hostile
ne peut ni verrouiller ni ralentir un autre compte. Sans identité de pair lisible, un seau commun « ? » est utilisé.
Audit expurgé.
"""
import collections
import json
import os
import socket
import socketserver
import stat
import struct
import threading
import time

from . import bounded, tools

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 1024 * 1024
IO_TIMEOUT_S = 10.0
AUTH_FAIL_MAX = 20
AUTH_FAIL_WINDOW_S = 60.0
AUTH_LOCK_S = 60.0
CALLS_PER_S = 10
DISPLAY_MIN_INTERVAL_S = 5.0
# Débits par AGENT (tous comptes confondus : ils protègent l'écran et le lecteur, pas le socket) : outil -> fenêtres
# (nombre maximal, durée en s). Aucune boucle de clics ni rafale de propositions/lancements n'est possible.
TOOL_RATES = {
    "display_message": ((1, DISPLAY_MIN_INTERVAL_S),),
    "present_title": ((1, 3.0), (20, 600.0)),
    "play_title": ((1, 10.0), (6, 600.0)),
    "close_player": ((1, 2.0), (30, 600.0)),
    "remote_control": ((3, 1.0), (60, 60.0)),
}
MAX_TRACKED_UIDS = 256    # tables de débit/verrouillage bornées (les entrées inactives sont purgées)


class AgentService:
    def __init__(self, ctx, registry, audit, clock=time.monotonic, log=None):
        self.ctx, self.registry, self.audit, self.clock = ctx, registry, audit, clock
        self.log = log or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.fails = {}          # uid -> deque des échecs récents
        self.locked_until = {}   # uid -> fin du verrouillage
        self.calls = {}          # (uid, agent) -> horodatages de la dernière seconde
        self.tool_calls = {}     # (agent, outil limité) -> horodatages récents (TOOL_RATES, tous comptes confondus)

    def _prune(self, now):
        """Borne les tables par UID : purge des entrées expirées, puis des plus anciennes si nécessaire."""
        for uid in [u for u, t in self.locked_until.items() if t <= now]:
            del self.locked_until[uid]
        for uid in [u for u, q in self.fails.items() if not q or q[-1] < now - AUTH_FAIL_WINDOW_S]:
            del self.fails[uid]
        for k in [k for k, w in self.calls.items() if not w or w[-1] <= now - 1.0]:
            del self.calls[k]
        for k in [k for k, w in self.tool_calls.items()
                  if not w or w[-1] <= now - max(s for _, s in TOOL_RATES.get(k[1], ((0, 0.0),)))]:
            del self.tool_calls[k]
        while len(self.fails) > MAX_TRACKED_UIDS:
            del self.fails[min(self.fails, key=lambda u: self.fails[u][-1])]
        while len(self.locked_until) > MAX_TRACKED_UIDS:
            del self.locked_until[min(self.locked_until, key=self.locked_until.get)]

    def _tool_limited(self, agent, tool, now):   # sous self.lock
        rates = TOOL_RATES[tool]
        q = self.tool_calls.get((agent, tool))
        if not q:
            return False
        longest = max(s for _, s in rates)
        while q and q[0] <= now - longest:
            q.popleft()
        return any(sum(1 for t in q if t > now - span) >= n for n, span in rates)

    def _deny(self, agent, tool, args, code, t0):
        self.audit.record(agent, tool, args, False, code, (time.monotonic() - t0) * 1000)
        return {"ok": False, "error": code}

    def handle(self, req, peer_uid=None):
        t0 = time.monotonic()
        if not isinstance(req, dict) or req.get("v") != 1 or req.get("op") not in ("call", "whoami"):
            return {"ok": False, "error": "bad_request"}
        agent, tool, args = req.get("agent"), req.get("tool"), req.get("args")
        uid = peer_uid if isinstance(peer_uid, int) and not isinstance(peer_uid, bool) else "?"
        now = self.clock()
        with self.lock:
            locked = now < self.locked_until.get(uid, 0.0)
        if locked:
            return self._deny(agent, tool, None, "locked", t0)
        scopes = self.registry.authenticate(agent, req.get("token"))
        if scopes is None:
            with self.lock:
                q = self.fails.setdefault(uid, collections.deque())
                q.append(now)
                while q and q[0] < now - AUTH_FAIL_WINDOW_S:
                    q.popleft()
                if len(q) >= AUTH_FAIL_MAX:
                    self.locked_until[uid] = now + AUTH_LOCK_S
                    q.clear()
                    self.log("agent_port_locked", uid=uid, seconds=AUTH_LOCK_S)
                self._prune(now)
            return self._deny(agent, tool, None, "unauthorized", t0)
        seen = getattr(self.ctx, "agent_seen", None)   # activité réelle (Réglages TV : « connecté », jamais supposé)
        if callable(seen):
            seen(agent)
        if req["op"] == "whoami":
            return {"ok": True, "result": {"agent": agent, "scopes": scopes}}
        t = tools.TOOL_BY_NAME.get(tool) if isinstance(tool, str) else None
        if t is None:
            return self._deny(agent, None, None, "unknown_tool", t0)
        if t["scope"] not in scopes:
            return self._deny(agent, tool, None, "forbidden_scope", t0)
        try:
            a = tools.validate_args(tool, args)
        except ValueError:
            return self._deny(agent, tool, None, "invalid_params", t0)
        key = (uid, agent)
        with self.lock:
            win = [x for x in self.calls.get(key, []) if x > now - 1.0]
            if len(win) >= CALLS_PER_S:
                self.calls[key] = win
                limited = True
            elif tool in TOOL_RATES and self._tool_limited(agent, tool, now):
                limited = True
            else:
                limited = False
                win.append(now)
                self.calls[key] = win
                if tool in TOOL_RATES:
                    self.tool_calls.setdefault((agent, tool), collections.deque()).append(now)
            if len(self.calls) > MAX_TRACKED_UIDS or len(self.tool_calls) > MAX_TRACKED_UIDS:
                self._prune(now)
        if limited:
            return self._deny(agent, tool, a, "rate_limited", t0)
        try:
            res = tools.dispatch(self.ctx, agent, scopes, tool, a)
        except tools.ToolError as e:
            return self._deny(agent, tool, a, str(e) if str(e) else "failed", t0)
        except Exception as e:   # jamais de trace ni de détail interne vers l'agent
            self.log("agent_tool_error", tool=tool, error=type(e).__name__)
            return self._deny(agent, tool, a, "internal", t0)
        self.audit.record(agent, tool, a, True, None, (time.monotonic() - t0) * 1000)
        return {"ok": True, "result": res}


def _peer_uid(sock):
    """UID du processus client du socket Unix : SO_PEERCRED (Linux, struct ucred) ou LOCAL_PEERCRED (BSD/macOS,
    struct xucred). None si indisponible."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return uid
        if hasattr(socket, "LOCAL_PEERCRED"):
            raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, struct.calcsize("2I"))   # SOL_LOCAL = 0
            _, uid = struct.unpack("2I", raw[:struct.calcsize("2I")])
            return uid
    except (OSError, struct.error):
        return None
    return None


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        bounded.install_deadline_reader(self)   # échéance globale de lecture de la requête (slowloris)

    def handle(self):
        self.request.settimeout(IO_TIMEOUT_S)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
        except (OSError, socket.timeout):
            return
        if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
            resp = {"ok": False, "error": "bad_request"}
        else:
            try:
                req = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                req = None
            resp = self.server.handler(req, _peer_uid(self.request))
        data = (json.dumps(resp, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(data) > MAX_RESPONSE:
            data = b'{"error":"response_too_large","ok":false}\n'
        try:
            self.request.settimeout(IO_TIMEOUT_S)
            self.wfile.write(data)
        except OSError:
            pass


class AgentPortServer(bounded.BoundedServerMixIn, socketserver.UnixStreamServer):
    """Au plus 16 connexions servies en parallèle (réponse « busy » immédiate au-delà), échéance globale de
    lecture IO_TIMEOUT_S par connexion."""
    deadline_s = IO_TIMEOUT_S
    busy_response = b'{"error":"busy","ok":false}\n'

    def __init__(self, path, handler, mode=0o660, gid=None):
        try:
            st = os.lstat(path)
            if not stat.S_ISSOCK(st.st_mode):
                raise OSError("refuse_non_socket_path")
            os.remove(path)   # socket orphelin d'une exécution précédente
        except FileNotFoundError:
            pass
        self.handler = handler
        # mode fixé par l'umask à la création (aucun chmod par chemin) ; le service n'est jamais root
        old = os.umask(0o777 & ~mode)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        if gid is not None:   # lchown : un lien substitué n'est jamais suivi
            os.chown(path, -1, gid, follow_symlinks=False)

    def server_close(self):
        path = self.server_address
        super().server_close()
        try:
            if isinstance(path, str) and stat.S_ISSOCK(os.lstat(path).st_mode):
                os.remove(path)
        except OSError:
            pass


def call(sock_path, req, timeout=15.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_RESPONSE + 64:
                raise OSError("response_too_large")
    finally:
        s.close()
    r = json.loads(buf.decode("utf-8"))
    if not isinstance(r, dict):
        raise OSError("bad_response")
    return r
