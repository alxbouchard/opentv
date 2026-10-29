"""Pont Muse : SDK Linux officiel (musegadget, sous-ensemble épinglé) utilisé SEULEMENT comme appariement et transport.

Muse ne reçoit jamais les commandes par défaut du SDK (system.run, file.read, file.write, device.health) : les modules
executor/fileops/service/cli ne sont pas livrés (vendor/muse/PIN.json) et un garde d'import refuse de les charger même
s'ils apparaissaient. Le pont enregistre sa PROPRE description d'appareil, limitée à des commandes OpenTV en lecture :

    opentv.status  opentv.search  opentv.recommend  opentv.catalogue_list  opentv.history

Chaque appel est validé (schéma strict, bornes, aucune adresse), puis délégué au serveur MCP OpenTV (opentv_pi.mcp)
qui parle à l'Agent Port Unix authentifié avec l'identité locale « muse » (scope read seulement, fichier client
root:opentv-muse 0640 émis par `opentv-pi muse pair`). L'Agent Port reste la barrière d'autorisation : identité
révoquée, scope retiré ou outil hors liste ⇒ refus. Les résultats repassent par un nettoyage récursif (aucune
adresse, aucune clé de secret, tailles bornées). Aucun paramètre de commande shell, de chemin ni d'URL n'existe.

Le pont tourne sous le compte opentv-muse (jamais root), sans accès au catalogue brut ni à l'état OpenTV. Aucun
jeton (SDK, appareil, VM, Agent Port) n'est jamais journalisé : un filtre remplace toute occurrence connue.
La boucle de reconnexion reprend la logique publique de musegadget.service.Service (Apache-2.0, Meta Platforms)
sans importer ce module (il importe la table des commandes par défaut d'executor).
"""
import argparse
import asyncio
import hashlib
import importlib.abc
import json
import logging
import math
import os
import re
import sys
import threading
import time

from . import VERSION, agentport, agents, config as otconfig, fsutil, mcp, tools, validate

MUSE_AGENT_ID = "muse"
MUSE_SCOPES = ["read"]
DEFAULT_STATE_DIR = "/var/lib/opentv-muse"
RUN_STATUS = "/run/opentv-muse/status.json"
PAIR_STATUS_NAME = "pair-status.json"
DEFAULT_NOISE_HOST = "hatch.metaaivm.com"     # = musegadget.service.DEFAULT_NOISE_HOST (vérifié par les tests)
DISPLAY_NAME = "OpenTV"
EX_CONFIG = 78                                # RestartPreventExitStatus=78 : configuration à corriger par un humain
CALL_TIMEOUT_S = 12.0
COMMAND_TIMEOUT_MS = 20000
MAX_PAYLOAD_BYTES = 64 * 1024                 # /link-control accepte 256 Kio par message : large marge
MAX_DEPTH, MAX_LIST, MAX_KEYS, MAX_STR = 8, 60, 48, 1200
PAIR_TIMEOUT_S = 600
BACKOFF_BASE_S, BACKOFF_MAX_S, AUTH_BACKOFF_MIN_S, HEALTHY_SESSION_S = 2.0, 60.0, 15.0, 30.0
TOKEN_REFRESH_AGE_S, TOKEN_RETRY_S = 3 * 3600, 300
MAX_CONCURRENT_INVOKES = 4

# commande Muse -> outil MCP OpenTV (scope read exigé ; jamais feedback ni display_message pour cette identité)
COMMANDS = {
    "opentv.status": "status",
    "opentv.search": "search",
    "opentv.recommend": "recommend",
    "opentv.catalogue_list": "catalogue_list",
    "opentv.history": "history",
}
DESCRIPTIONS = {
    "opentv.status": "État de la boîte OpenTV (nom, réseau en mode seulement, catalogue, profil « Pour toi »). "
                     "Lecture seule, sans adresse ni secret.",
    "opentv.search": "Cherche un titre dans le catalogue OpenTV accepté (paginé). Une requête ressemblant à une "
                     "adresse est refusée. Lecture seule.",
    "opentv.recommend": "Recommandations « Pour toi » expliquées, filtrées sur le catalogue accepté (paginées). "
                        "Lecture seule.",
    "opentv.catalogue_list": "Liste alphabétique paginée de TOUT le catalogue accepté (identifiant stable, titre, "
                             "année, genres, notes séparées). Parcourir avec offset jusqu'à has_more=false en gardant "
                             "generated_at : s'il change entre deux pages (rafraîchissement quotidien), recommencer à "
                             "offset 0. Aucune URL. Lecture seule.",
    "opentv.history": "Historique local « Pour toi » (changements, ouvertures, listes) paginé. Lecture seule.",
}
# modules du SDK jamais chargés (non livrés ; refusés même s'ils apparaissaient)
FORBIDDEN_MODULES = frozenset(("musegadget.executor", "musegadget.fileops", "musegadget.service", "musegadget.cli",
                               "musegadget.__main__", "musegadget.doctor", "musegadget.muse_turn",
                               "musegadget.voice_call", "musegadget.voice_audio", "musegadget.voice_gate",
                               "musegadget.data"))
ERROR_RE = re.compile(r"[a-z][a-z0-9_]{1,39}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_SECRET_KEY_RE = re.compile(r"token|secret|passw|psk|cookie|auth|url|uri|href|link|poster|path|src|socket", re.I)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_HOST_RE = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+")
PAIR_STATES = ("starting", "open", "paired", "already_paired", "invalid_sdk_token", "no_sdk_token", "window_closed",
               "error")


class BridgeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code if isinstance(code, str) and ERROR_RE.fullmatch(code) else "failed"


# ------------------------------------------------------------------------------------------------ secrets et journal
class SecretRedactor(logging.Filter):
    """Remplace toute occurrence d'un secret connu (jetons SDK/appareil/VM/Agent Port) dans chaque ligne de journal."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._secrets = set()

    def add(self, s):
        if isinstance(s, str) and len(s) >= 8:
            with self._lock:
                self._secrets.add(s)

    def redact(self, text):
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for s in secrets:
            if s in text:
                text = text.replace(s, "[secret]")
        return text

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:   # message mal formé : jamais d'arguments bruts
            msg = str(record.msg)
        record.msg, record.args = self.redact(msg), None
        return True


REDACTOR = SecretRedactor()


def register_secret(s):
    REDACTOR.add(s)


def log_event(event, **fields):
    """Une ligne JSON (journal systemd) : codes, noms de commande, durées — jamais paramètres, résultats ni jetons."""
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    sys.stdout.write(REDACTOR.redact(json.dumps(rec, ensure_ascii=False, sort_keys=True)) + "\n")
    sys.stdout.flush()


def setup_logging(stream=None):
    h = logging.StreamHandler(stream or sys.stdout)
    h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    h.addFilter(REDACTOR)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(h)
    root.setLevel(logging.WARNING)
    logging.getLogger("musegadget").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return h


# ------------------------------------------------------------------------------------------------ SDK épinglé
def sdk_dir_candidates():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # /opt/opentv/lib ou scripts/pi4
    return (os.path.join(base, "muse"), os.path.join(base, "vendor", "muse"))


def find_sdk_dir():
    for d in sdk_dir_candidates():
        if os.path.isfile(os.path.join(d, "PIN.json")):
            return d
    return None


def sha256_file(path, cap=16 * 1024 * 1024):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > cap:
            raise fsutil.UnsafePath("not_regular")
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def load_pin(d):
    raw = fsutil.read_regular(os.path.join(d, "PIN.json"), 256 * 1024)
    pin = json.loads(raw.decode("utf-8"))
    if not isinstance(pin, dict) or pin.get("v") != 1 or not isinstance(pin.get("files"), dict) or \
            not re.fullmatch(r"[0-9a-f]{40}", str(pin.get("commit"))):
        raise ValueError("pin_invalid")
    return pin


def verify_sdk(d):
    """-> {"ok", "commit", "problems": [...] , "websockets": "ok"|"missing"|"bad_hash"} ; jamais d'exception."""
    res = {"ok": False, "commit": None, "problems": [], "websockets": "missing", "dir": d}
    if not d:
        res["problems"].append("sdk_missing")
        return res
    try:
        pin = load_pin(d)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        res["problems"].append("pin_unreadable")
        return res
    res["commit"] = pin["commit"]
    want = pin["files"]
    seen = set()
    top = os.path.join(d, "musegadget")
    for root, dirs, files in os.walk(top):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), d).replace(os.sep, "/")
            seen.add(rel)
            if rel not in want:
                res["problems"].append("unexpected:" + rel)
    for rel, digest in sorted(want.items()):
        p = os.path.join(d, rel)
        if rel.startswith("musegadget/"):
            if rel not in seen:
                res["problems"].append("missing:" + rel)
                continue
        elif not os.path.exists(p):
            res["problems"].append("missing:" + rel)
            continue
        try:
            if sha256_file(p) != digest:
                res["problems"].append("modified:" + rel)
        except OSError:
            res["problems"].append("unreadable:" + rel)
    for ex in pin.get("excluded") or []:
        if os.path.lexists(os.path.join(d, ex)):
            res["problems"].append("excluded_present:" + ex)
    dep = ((pin.get("dependencies") or {}).get("websockets") or {})
    wf = dep.get("file")
    if isinstance(wf, str) and re.fullmatch(r"websockets-[0-9.]+-py3-none-any\.whl", wf):
        p = os.path.join(d, wf)
        if os.path.lexists(p):
            try:
                res["websockets"] = "ok" if sha256_file(p) == dep.get("sha256") else "bad_hash"
            except OSError:
                res["websockets"] = "bad_hash"
        res["websockets_file"] = wf
    res["ok"] = not res["problems"]
    return res


class _ImportGuard(importlib.abc.MetaPathFinder):
    """Refuse tout module du SDK qui exposerait les commandes par défaut (executor, fileops, service, cli…)."""

    def find_spec(self, name, path=None, target=None):
        if name in FORBIDDEN_MODULES or any(name.startswith(m + ".") for m in FORBIDDEN_MODULES):
            raise ImportError("forbidden_musegadget_module:%s" % name)
        return None


def install_import_guard():
    if not any(isinstance(f, _ImportGuard) for f in sys.meta_path):
        sys.meta_path.insert(0, _ImportGuard())
    for name in list(sys.modules):
        if name in FORBIDDEN_MODULES:
            raise BridgeError("forbidden_module_loaded")


class SDK:
    """Modules musegadget autorisés, chargés depuis le dossier épinglé vérifié."""

    def __init__(self, d):
        import musegadget
        from musegadget import config, identity, link_client, muse_api
        real = os.path.realpath(d) + os.sep
        for m in (musegadget, config, identity, link_client, muse_api):
            if not os.path.realpath(m.__file__).startswith(real):
                raise BridgeError("sdk_shadowed")
        self.dir, self.version = d, musegadget.__version__
        self.config, self.identity, self.link_client, self.muse_api = config, identity, link_client, muse_api

    def pair_module(self):
        from musegadget import pair
        return pair


def load_sdk(d=None, require_websockets=True):
    """Vérifie le SDK épinglé (empreintes, aucun fichier en trop) PUIS l'ajoute au chemin d'import. -> SDK."""
    install_import_guard()
    d = d or find_sdk_dir()
    v = verify_sdk(d)
    if not v["ok"]:
        raise BridgeError("sdk_unverified")
    if d not in sys.path:
        sys.path.insert(0, d)
    if v["websockets"] == "ok":
        w = os.path.join(d, v["websockets_file"])
        if w not in sys.path:
            sys.path.insert(1, w)   # roue pure Python épinglée, importée telle quelle (zipimport)
    elif require_websockets:
        raise BridgeError("dependency_missing")
    try:
        return SDK(d)
    except ImportError:   # python3-cryptography absente (paquet système) : refus clair, jamais une boucle
        raise BridgeError("dependency_missing")


# ------------------------------------------------------------------------------------------------ commandes Muse
def _param_desc(p):
    d = p.get("description") or ""
    if p["type"] == "integer":
        d += " (entier de %d à %d, défaut %d)" % (p["minimum"], p["maximum"], p["default"])
    elif "enum" in p:
        d = (d + " " if d else "") + "(une valeur parmi : %s ; défaut %s)" % (", ".join(p["enum"]), p.get("default"))
    else:
        d += " (%d à %d caractères, texte seul, jamais d'adresse)" % (p.get("minLength", 0), p.get("maxLength", 200))
    return d.strip()


def command_specs():
    """Spécifications enregistrées auprès de Muse (format commands_v2 du SDK). Toujours dérivées des schémas typés
    de l'Agent Port ; tout outil hors scope read est un défaut de construction (exception)."""
    out = {}
    for cmd, tool in COMMANDS.items():
        t = tools.TOOL_BY_NAME[tool]
        if t["scope"] != "read":
            raise RuntimeError("muse_command_not_read_only:" + cmd)
        sch = t["inputSchema"]
        req, opt = {}, {}
        for k, p in sch["properties"].items():
            (req if k in sch["required"] else opt)[k] = {"type": p["type"], "description": _param_desc(p)}
        out[cmd] = {"description": DESCRIPTIONS[cmd], "required": req, "optional": opt, "timeout_ms": COMMAND_TIMEOUT_MS}
    return out


def validate_params(cmd, params):
    """Validation stricte AVANT tout appel : champs connus seulement, types/bornes/enum du schéma, aucune adresse."""
    tool = COMMANDS.get(cmd) if isinstance(cmd, str) else None
    if tool is None:
        raise BridgeError("unknown_command")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise BridgeError("invalid_params")
    sch = tools.TOOL_BY_NAME[tool]["inputSchema"]
    if set(params) - set(sch["properties"]) or not set(sch["required"]) <= set(params):
        raise BridgeError("invalid_params")
    try:
        a = tools.validate_args(tool, params)
    except ValueError:
        raise BridgeError("invalid_params")
    for v in a.values():
        if isinstance(v, str) and not validate.no_url(v):
            raise BridgeError("invalid_params")
    return a


def scrub(obj, depth=0):
    """Nettoyage de défense en profondeur (le moteur nettoie déjà) : aucune chaîne ressemblant à une adresse, aucune
    clé de secret/chemin/URL, caractères de contrôle retirés, profondeur/listes/clés/chaînes bornées."""
    if depth > MAX_DEPTH:
        return None
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj if abs(obj) < 2 ** 53 else None
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, str):
        s = _CTRL_RE.sub(" ", obj)[:MAX_STR]
        return s if validate.no_url(s) else ""
    if isinstance(obj, dict):
        out = {}
        for k, v in list(obj.items())[:MAX_KEYS]:
            if isinstance(k, str) and _KEY_RE.fullmatch(k) and not _SECRET_KEY_RE.search(k):
                out[k] = scrub(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v, depth + 1) for v in list(obj)[:MAX_LIST]]
    return None


class AgentPortMCP:
    """Client MCP en mémoire (opentv_pi.mcp.Session) devant l'Agent Port Unix, avec l'identité « muse » relue à chaque
    appel (une révocation ou une rotation s'applique immédiatement). Le jeton ne quitte jamais cet objet."""

    def __init__(self, client_config, call=agentport.call, timeout=CALL_TIMEOUT_S):
        self.client_config, self._call, self.timeout = client_config, call, timeout

    def _session(self):
        try:
            cfg = mcp.load_client_config(self.client_config)
        except mcp.ConfigError:
            raise BridgeError("bridge_identity_missing")
        if cfg["agent_id"] != MUSE_AGENT_ID:
            raise BridgeError("bridge_identity_invalid")
        register_secret(cfg["token"])
        base = {"v": 1, "agent": cfg["agent_id"], "token": cfg["token"]}
        sock = cfg["socket"]
        last = {}

        def call_fn(req):
            last.clear()
            last["called"] = True
            try:
                r = self._call(sock, dict(base, **req), timeout=self.timeout)
            except (OSError, ValueError):
                last["error"] = "service_unavailable"
                raise
            last.update(r if isinstance(r, dict) else {"ok": False, "error": "service_unavailable"})
            return r
        s = mcp.Session(call_fn)
        s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": mcp.PROTOCOL_VERSIONS[0], "capabilities": {},
                             "clientInfo": {"name": "opentv-muse-bridge", "version": VERSION}}})
        s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return s, last

    def identity_scopes(self):
        """whoami authentifié via tools/list MCP -> scopes de l'identité ; BridgeError(code) sinon."""
        s, last = self._session()
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        if "result" not in r or not last.get("ok"):
            raise BridgeError(last.get("error") or "service_unavailable")
        scopes = (last.get("result") or {}).get("scopes")
        if not isinstance(scopes, list):
            raise BridgeError("failed")
        return sorted(str(x) for x in scopes)

    def call_tool(self, name, args):
        if name not in COMMANDS.values():   # jamais feedback / display_message, même si l'identité les avait
            raise BridgeError("unknown_command")
        s, last = self._session()
        r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
        res = r.get("result")
        if not isinstance(res, dict):
            raise BridgeError("failed")
        if res.get("isError"):   # sans appel au service : refus de la validation MCP locale
            raise BridgeError((last.get("error") or "service_unavailable") if last.get("called") else "invalid_params")
        return res.get("structuredContent")


def dispatch(port, cmd, params):
    """Une commande Muse -> {"ok": True, "payload": …} | {"ok": False, "error": "<code>"}."""
    try:
        args = validate_params(cmd, params)
        payload = scrub(port.call_tool(COMMANDS[cmd], args))
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise BridgeError("result_too_large")
        return {"ok": True, "payload": payload}
    except BridgeError as e:
        return {"ok": False, "error": e.code}
    except Exception as e:   # jamais de trace ni de détail interne vers Muse
        log_event("muse_bridge_error", error=type(e).__name__)
        return {"ok": False, "error": "internal"}


# ------------------------------------------------------------------------------------------------ état d'exécution
def write_json_status(path, obj):
    try:
        fsutil.atomic_write_path(path, (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"), mode=0o600)
        return True
    except OSError:
        return False


class _Backoff:
    def __init__(self):
        self.failures, self.floor = 0, 0.0

    def next_delay(self):
        delay = min(BACKOFF_BASE_S * 2.0 ** min(self.failures, 16), BACKOFF_MAX_S)
        self.failures += 1
        return max(delay, self.floor)

    def reset(self):
        self.failures, self.floor = 0, 0.0


FATAL_IDENTITY = ("bridge_identity_missing", "bridge_identity_invalid", "unauthorized", "identity_not_read_only")


class Bridge:
    """Boucle de connexion (reprise de musegadget.service.Service, sans executor) et réponse aux commandes."""

    def __init__(self, sdk, identity, port, sdk_token=None, display_name=DISPLAY_NAME, connect=None, fetch_vms=None,
                 refresh=None, status_path=RUN_STATUS, sleep_scale=1.0):
        self.sdk, self.identity, self.port = sdk, identity, port
        self.sdk_token, self.display_name, self.connect = sdk_token, display_name, connect
        self.fetch_vms = fetch_vms or sdk.muse_api.fetch_vms_with_status
        self.refresh = refresh or sdk.muse_api.refresh_device_token
        self.status_path, self.sleep_scale = status_path, sleep_scale
        self._stop = asyncio.Event()
        self._last_refresh_attempt = float("-inf")
        self._slots = None
        register_secret(sdk_token)

    # -- description et commandes (aucune commande par défaut du SDK)
    def describe(self):
        return self.sdk.link_client.DeviceDescription(node_id=self.identity.node_id, display_name=self.display_name,
                                                      version=self.sdk.version, commands=command_specs())

    def run_command(self, name, params, timeout_ms=None):
        t0 = time.monotonic()
        res = dispatch(self.port, name, params)
        log_event("muse_invoke", command=name if name in COMMANDS else "?", ok=res["ok"], error=res.get("error"),
                  ms=round((time.monotonic() - t0) * 1000, 1))
        return res

    def stop(self):
        self._stop.set()

    def _status(self, state, **extra):
        if self.status_path:
            write_json_status(self.status_path, dict({"v": 1, "state": state, "since": round(time.time(), 3),
                                                      "commands": sorted(COMMANDS)}, **extra))

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self._stop.wait(), seconds * self.sleep_scale)
        except asyncio.TimeoutError:
            pass

    def _secrets_from(self, pairing):
        for k in ("access_token", "refresh_token"):
            register_secret(pairing.get(k))

    async def run(self):
        """-> code de fin : stopped | not_paired | unpaired | <erreur d'identité OpenTV> (fatale, exit 78)."""
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT_INVOKES)
        cfgmod = self.sdk.config
        backoff = _Backoff()
        while not self._stop.is_set():
            pairing = cfgmod.load_json(cfgmod.PAIRING_FILE)
            if not pairing:
                self._status("not_paired")
                log_event("muse_not_paired")
                return "not_paired"   # jamais d'attente active : le service ne tourne qu'apparié
            self._secrets_from(pairing)
            if not all(isinstance(pairing.get(k), str) and pairing.get(k) for k in ("access_token", "refresh_token")):
                self._status("refused", reason="pairing_invalid")
                log_event("muse_pairing_invalid", error="tokens")
                return "pairing_invalid"
            pairing = await self._maybe_refresh(pairing)
            if pairing is None:
                if not cfgmod.load_json(cfgmod.PAIRING_FILE):
                    continue
                await self._sleep(TOKEN_RETRY_S)
                continue
            # verrou OpenTV : identité « muse » authentifiée et STRICTEMENT en lecture avant tout enregistrement
            try:
                scopes = await asyncio.to_thread(self.port.identity_scopes)
            except BridgeError as e:
                if e.code in FATAL_IDENTITY:
                    self._status("refused", reason=e.code)
                    log_event("muse_identity_refused", error=e.code)
                    return e.code
                self._status("waiting_opentv", reason=e.code)
                await self._sleep(backoff.next_delay())
                continue
            if scopes != MUSE_SCOPES:
                self._status("refused", reason="identity_not_read_only")
                log_event("muse_identity_refused", error="identity_not_read_only")
                return "identity_not_read_only"
            api = self.sdk.muse_api.api_root(pairing.get("api_url_v2", "") if str(
                pairing.get("api_url_v2", "")).startswith("https://") else "")
            noise_host = pairing.get("noise_host") or DEFAULT_NOISE_HOST
            if not isinstance(noise_host, str) or not _HOST_RE.fullmatch(noise_host):
                log_event("muse_pairing_invalid", error="noise_host")
                self._status("refused", reason="pairing_invalid")
                return "pairing_invalid"
            vms, status = await asyncio.to_thread(self.fetch_vms, pairing["access_token"], api)
            if status == 401:
                log_event("muse_device_token_rejected")
                if await self._maybe_refresh(pairing, force=True) is None:
                    await self._sleep(TOKEN_RETRY_S)
                continue
            vm = next((v for v in vms if v.get("is_default")), vms[0] if vms else None)
            if vm is None:
                await self._sleep(backoff.next_delay())
                continue
            register_secret(vm.get("vm_auth_token"))
            outcome, lasted = await self._session(vm, noise_host)
            o = self.sdk.link_client.Outcome
            if outcome is o.STOPPED:
                break
            if outcome is o.UNPAIRED:
                cfgmod.delete_json(cfgmod.PAIRING_FILE)
                self._status("unpaired")
                log_event("muse_unpaired_by_muse")
                return "unpaired"
            if lasted >= HEALTHY_SESSION_S:
                backoff.reset()
            if outcome in (o.AUTH_REJECTED, o.FORBIDDEN):
                backoff.floor = AUTH_BACKOFF_MIN_S
            await self._sleep(backoff.next_delay())
        self._status("stopped")
        return "stopped"

    async def _session(self, vm, noise_host):
        lc = self.sdk.link_client
        session = lc.LinkSession(noise_host=noise_host, vm_id=vm.get("vm_id") or vm.get("vm_name") or "",
                                 vm_auth_token=vm["vm_auth_token"], device=self.describe(),
                                 run_command=self.run_command, connect=self.connect, invoke_slots=self._slots)
        self._status("connecting")
        started = time.monotonic()

        async def watch():
            await session.registered.wait()
            self._status("registered")
            log_event("muse_registered", commands=sorted(COMMANDS))
        watcher = asyncio.ensure_future(watch())
        try:
            outcome = await session.run(self._stop)
        except Exception as e:
            log_event("muse_session_failed", error=type(e).__name__)
            outcome = lc.Outcome.CLOSED
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        self._status("disconnected", outcome=outcome.value)
        log_event("muse_session_ended", outcome=outcome.value, seconds=round(time.monotonic() - started))
        lasted = time.monotonic() - (session.registered_at or time.monotonic())
        return outcome, lasted

    async def _maybe_refresh(self, pairing, force=False):
        cfgmod = self.sdk.config
        age = time.time() - pairing.get("access_token_saved_at", 0)
        if not force and age < TOKEN_REFRESH_AGE_S:
            return pairing
        if not force and time.monotonic() - self._last_refresh_attempt < TOKEN_RETRY_S:
            return pairing
        self._last_refresh_attempt = time.monotonic()
        tokens, status = await asyncio.to_thread(
            self.refresh, pairing["refresh_token"], self.identity.node_id,
            self.sdk.muse_api.api_root(pairing.get("api_url_v2", "")), self.sdk_token)
        if tokens:
            register_secret(tokens.get("access_token"))
            register_secret(tokens.get("refresh_token"))
            pairing = dict(pairing, access_token=tokens["access_token"], refresh_token=tokens["refresh_token"],
                           access_token_saved_at=int(time.time()))
            cfgmod.save_json(cfgmod.PAIRING_FILE, pairing)
            log_event("muse_device_token_rotated")
            return pairing
        if status == 401:
            cfgmod.delete_json(cfgmod.PAIRING_FILE)
            log_event("muse_pairing_revoked")
            return None
        return None if force else pairing


# ------------------------------------------------------------------------------------------------ points d'entrée
def client_config_path(cfg, uid=None):
    return os.path.join(cfg.agent_clients_dir, agents.client_file_name(MUSE_AGENT_ID,
                                                                      os.getuid() if uid is None else uid))


def _prepare_env():
    if os.environ.pop("MUSEGADGET_SDK_TOKEN", None) is not None:   # jamais un jeton dans l'environnement
        log_event("muse_env_token_ignored")
    sd = os.environ.get("MUSEGADGET_STATE_DIR") or DEFAULT_STATE_DIR
    if not os.path.isabs(sd):
        raise BridgeError("state_dir_invalid")
    os.environ["MUSEGADGET_STATE_DIR"] = sd
    return sd


def cmd_run(a, sdk=None, connect=None, fetch_vms=None, refresh=None, status_path=RUN_STATUS, client_config=None):
    sdk = sdk or load_sdk(require_websockets=connect is None)
    cfg = otconfig.Config.load(a.env)
    try:
        token = sdk.config.sdk_token()
    except ValueError:
        log_event("muse_sdk_token_invalid")
        token = None
    port = AgentPortMCP(client_config or client_config_path(cfg))
    bridge = Bridge(sdk, sdk.identity.load_or_create(), port, sdk_token=token, connect=connect, fetch_vms=fetch_vms,
                    refresh=refresh, status_path=status_path)
    token = None

    async def main():
        loop = asyncio.get_running_loop()
        try:
            import signal
            for s in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(s, bridge.stop)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        return await bridge.run()
    log_event("muse_bridge_start", version=VERSION, sdk_commit=verify_sdk(sdk.dir).get("commit"),
              commands=sorted(COMMANDS))
    result = asyncio.run(main())
    log_event("muse_bridge_end", result=result)
    return 0 if result in ("stopped", "not_paired", "unpaired") else EX_CONFIG


def cmd_pair(a, sdk=None, pair_fn=None):
    """(compte opentv-muse, opentv-muse-pair.service) Fenêtre Bluetooth bornée du SDK officiel ; aucune commande
    n'est exposée pendant l'appariement (seuls des identifiants sont échangés et vérifiés)."""
    sdk = sdk or load_sdk(require_websockets=False)
    status = os.path.join(sdk.config.state_dir(), PAIR_STATUS_NAME)
    write_json_status(status, {"state": "starting"})
    try:
        tok = sdk.config.sdk_token()
    except ValueError:
        write_json_status(status, {"state": "invalid_sdk_token"})
        return 2
    if not tok:
        write_json_status(status, {"state": "no_sdk_token"})
        return 2
    register_secret(tok)
    tok = None
    if sdk.config.load_json(sdk.config.PAIRING_FILE):
        write_json_status(status, {"state": "already_paired"})
        return 0
    pm = sdk.pair_module()
    pair_fn = pair_fn or pm.pair

    def on_open(w):
        write_json_status(status, {"state": "open", "ble_name": w.ble_name, "timeout_s": int(w.timeout_s)})
        log_event("muse_pair_open", ble_name=w.ble_name, timeout_s=int(w.timeout_s))
    try:
        res = pair_fn(on_open=on_open, force=False, timeout_s=a.timeout)
        state = res.outcome.value
    except Exception as e:   # D-Bus/BlueZ absent, refus de politique… : code seul
        log_event("muse_pair_error", error=type(e).__name__)
        state = "error"
    write_json_status(status, {"state": state})
    log_event("muse_pair_end", result=state)
    return 0 if state in ("paired", "already_paired") else 1


def build_parser():
    p = argparse.ArgumentParser(prog="opentv-muse-bridge",
                                description="Pont Muse OpenTV (SDK officiel = transport ; commandes OpenTV en lecture)")
    p.add_argument("--env", default=otconfig.ENV_FILE)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="(opentv-muse.service) connexion à Muse, commandes OpenTV en lecture seulement")
    pa = sub.add_parser("pair", help="(opentv-muse-pair.service) fenêtre d'appariement Bluetooth bornée")
    pa.add_argument("--timeout", type=int, default=PAIR_TIMEOUT_S, choices=range(60, 901), metavar="60..900")
    sub.add_parser("describe", help="commandes enregistrées auprès de Muse (aucun secret)")
    return p


def main(argv):
    a = build_parser().parse_args(argv)
    if a.cmd == "describe":
        sys.stdout.write(json.dumps({"commands": command_specs()}, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
        return 0
    if os.geteuid() == 0:   # le pont n'est jamais root (le SDK d'origine donnerait le compte à Muse)
        log_event("muse_bridge_refused", error="run_as_root_refused")
        return EX_CONFIG
    setup_logging()
    try:
        _prepare_env()
        if a.cmd == "pair":
            return cmd_pair(a)
        return cmd_run(a)
    except BridgeError as e:
        log_event("muse_bridge_refused", error=e.code)
        return EX_CONFIG
