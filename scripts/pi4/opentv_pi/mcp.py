"""Serveur MCP stdio minimal (JSON-RPC 2.0, une ligne JSON par message) devant l'Agent Port local.

Méthodes : initialize, ping, tools/list, tools/call ; notifications ignorées sans réponse. Les arguments sont
validés localement puis par le service (autorité). Pas de lots (supprimés par MCP 2025-06-18), pas de ressources,
pas d'exécution arbitraire. Le jeton reste dans le fichier client 0600 et n'est jamais écrit sur stdout/stderr.
"""
import argparse
import json
import os
import stat
import sys

from . import VERSION, agentport, tools, validate

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LINE = 1024 * 1024
INSTRUCTIONS = ("OpenTV (Raspberry Pi) : outils bornés « Pour toi » (status, recommend, catalogue_list, search, "
                "feedback, history), display_message et, si le propriétaire l'a autorisé, le pilotage (present_title, "
                "play_title, close_player, remote_control). Choisis toi-même un film avec recommend/search/history, "
                "explique-le en une phrase et montre-le avec present_title ; lance un film seulement quand l'humain "
                "le demande. Les sorties ne contiennent jamais d'URL. Les décisions sensibles restent à l'humain.")
ERRORS_FR = {"unauthorized": "agent non autorisé", "forbidden_scope": "outil hors des droits de cet agent",
             "invalid_params": "paramètres invalides", "rate_limited": "trop de demandes, réessaie plus tard",
             "locked": "Agent Port verrouillé temporairement", "unknown_id": "identifiant absent du catalogue accepté",
             "usage": "requête refusée", "store_unavailable": "historique indisponible",
             "service_unavailable": "service OpenTV indisponible",
             "player_active": "un film est en cours : seule la fermeture est possible",
             "player_busy": "un film est déjà en cours",
             "player_returning": "OpenTV revient sur la fiche du film précédent : réessaie dans un instant",
             "playback_unavailable": "lecture indisponible sur cette TV",
             "tv_busy": "la TV est dans un réglage : réessaie après", "tv_setup_active": "OpenTV est en configuration",
             "tv_queue_full": "trop d'actions en attente sur la TV", "control_unavailable": "pilotage indisponible"}


class ConfigError(Exception):
    pass


def load_client_config(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise ConfigError("config_unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError("config_not_file")
        if st.st_uid == os.geteuid() and st.st_uid != 0 or st.st_uid == 0 and os.geteuid() == 0:
            if st.st_mode & 0o077:   # fichier personnel (émis sans privilège) : 0600
                raise ConfigError("config_mode_must_be_0600")
        elif st.st_uid == 0:
            # émis par root dans /etc/opentv/agents : root:<groupe privé de l'agent> 0640, non modifiable par l'agent
            if st.st_gid != os.getegid() and st.st_gid not in os.getgroups():
                raise ConfigError("config_owner")
            if st.st_mode & 0o037:
                raise ConfigError("config_mode_must_be_0640")
        else:
            raise ConfigError("config_owner")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        c = json.loads(raw.decode("utf-8"))
        validate.agent_id(c["agent_id"])
        if not isinstance(c["token"], str) or not 20 <= len(c["token"]) <= 200:
            raise ValueError
        if not isinstance(c["socket"], str) or not os.path.isabs(c["socket"]):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ConfigError("config_invalid")
    return c


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _tool_error(code):
    return {"content": [{"type": "text", "text": "Erreur OpenTV : %s (%s)" % (ERRORS_FR.get(code, code), code)}],
            "isError": True}


class Session:
    def __init__(self, call_fn):
        self.call_fn, self.initialized = call_fn, False

    def _service(self, req):
        try:
            return self.call_fn(req)
        except (OSError, ValueError):
            return {"ok": False, "error": "service_unavailable"}

    def handle(self, msg):
        """-> réponse (dict) ou None pour une notification."""
        if isinstance(msg, list):
            return _err(None, -32600, "Invalid Request: batch non supporté")
        if not isinstance(msg, dict):
            return _err(None, -32600, "Invalid Request")
        mid = msg.get("id")
        if not (mid is None or isinstance(mid, str) or (isinstance(mid, int) and not isinstance(mid, bool))):
            return _err(None, -32600, "Invalid Request: id")
        if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _err(mid, -32600, "Invalid Request")
        method, params = msg["method"], msg.get("params")
        if "id" not in msg:   # notification : jamais de réponse
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if params is not None and not isinstance(params, dict):
            return _err(mid, -32602, "Invalid params")
        params = params or {}
        if method == "initialize":
            want = params.get("protocolVersion")
            self.initialized = True
            return _ok(mid, {"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "opentv-pi", "title": "OpenTV Agent Port", "version": VERSION},
                             "instructions": INSTRUCTIONS})
        if method == "ping":
            return _ok(mid, {})
        if not self.initialized:
            return _err(mid, -32002, "Server not initialized")
        if method == "tools/list":
            if params.get("cursor") is not None:
                return _err(mid, -32602, "Invalid params: cursor inconnu (liste complète en une page)")
            r = self._service({"op": "whoami"})
            if not r.get("ok"):
                return _err(mid, -32603, "OpenTV : %s" % ERRORS_FR.get(r.get("error"), r.get("error")))
            return _ok(mid, {"tools": tools.public_tools(r["result"].get("scopes") or [])})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in tools.TOOL_BY_NAME:
                return _err(mid, -32602, "Unknown tool")
            try:
                a = tools.validate_args(name, args)
            except ValueError:
                return _ok(mid, _tool_error("invalid_params"))
            r = self._service({"op": "call", "tool": name, "args": a})
            if not r.get("ok"):
                return _ok(mid, _tool_error(r.get("error") if isinstance(r.get("error"), str) else "failed"))
            res = r.get("result")
            return _ok(mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, sort_keys=True)}],
                             "structuredContent": res, "isError": False})
        return _err(mid, -32601, "Method not found")


def serve(inp, out, call_fn):
    s = Session(call_fn)
    while True:
        line = inp.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            resp = _err(None, -32600, "Invalid Request: message trop long")
            while line and not line.endswith("\n"):   # ignore la fin de la ligne géante
                line = inp.readline(MAX_LINE + 1)
        elif not line.strip():
            continue
        else:
            try:
                msg = json.loads(line)
            except ValueError:
                resp = _err(None, -32700, "Parse error")
            else:
                resp = s.handle(msg)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False, sort_keys=True) + "\n")
            out.flush()


def main(argv):
    p = argparse.ArgumentParser(prog="opentv-mcp", description="Serveur MCP stdio OpenTV (Agent Port local)")
    p.add_argument("--config", required=True, help="fichier client 0600 (opentv-pi agent issue)")
    a = p.parse_args(argv)
    try:
        cfg = load_client_config(a.config)
    except ConfigError as e:
        sys.stderr.write(json.dumps({"error": str(e)}) + "\n")
        return 2
    base = {"v": 1, "agent": cfg["agent_id"], "token": cfg["token"]}
    sock = cfg["socket"]
    cfg = None

    def call_fn(req):
        return agentport.call(sock, dict(base, **req))
    serve(sys.stdin, sys.stdout, call_fn)
    return 0
