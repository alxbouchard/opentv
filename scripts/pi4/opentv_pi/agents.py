"""Identité des agents locaux : registre (hash des jetons), politique de scopes, fichier client 0600, audit expurgé."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from . import fsutil, validate

SAFE_ARG_RE = re.compile(r"[a-z0-9/-]{1,40}")
SAFE_ARG_KEYS = ("action", "list", "id")


def _sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def atomic_write_json(path, obj, mode=0o600, uid=None, gid=None):
    """Écriture atomique par descripteur (fsutil) : jamais de chown/chmod par chemin, lien pré-placé remplacé."""
    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    return fsutil.atomic_write_path(path, data, mode=mode, uid=uid, gid=gid)


class Registry:
    """agents.json (0600, propriétaire opentv) : {"v":1, "agents": {id: {token_sha256, scopes, created_at}},
    "policy": {id: [scopes autorisés au QR]}, "policy_at": horodatage}. Rechargé quand le fichier change.

    Politique : tant que le QR n'a jamais été validé (policy_at absent et policy vide), l'administrateur local (root
    sur la boîte) peut émettre n'importe quel scope valide. Dès qu'une politique existe, elle est un plafond strict :
    agent absent/inconnu ⇒ « agent_not_in_policy », scope au-delà (y compris pour un agent « aucun accès », scopes
    vides) ⇒ « scope_exceeds_policy », sauf --force explicite sur la boîte."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()
        self._sig, self.data = None, {"v": 1, "agents": {}, "policy": {}}
        self._reload()

    def _reload(self):
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            self._sig, self.data = None, {"v": 1, "agents": {}, "policy": {}}
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        try:   # lien ou FIFO à la place du registre : illisible (O_NOFOLLOW, non bloquant) ⇒ fail-closed
            d = json.loads(fsutil.read_regular(self.path, 4 * 1024 * 1024).decode("utf-8"))
            if not isinstance(d, dict) or d.get("v") != 1:
                raise ValueError
        except (OSError, ValueError, UnicodeDecodeError):
            d = {"v": 1, "agents": {}, "policy": {}}   # registre illisible : aucun agent (fail-closed)
        d.setdefault("agents", {})
        d.setdefault("policy", {})
        self._sig, self.data = sig, d

    def _save(self, uid=None, gid=None):
        atomic_write_json(self.path, self.data, uid=uid, gid=gid)
        self._sig = None

    def set_policy(self, policy, uid=None, gid=None):
        clean = {validate.agent_id(k): validate.scopes(v) for k, v in policy.items()}
        with self.lock:
            self._reload()
            self.data["policy"] = clean
            self.data["policy_at"] = round(self.clock(), 3)
            self._save(uid, gid)

    def _policy_defined(self):
        pol = self.data.get("policy")
        return isinstance(pol, dict) and (bool(pol) or self.data.get("policy_at") is not None)

    def policy_defined(self):
        with self.lock:
            self._reload()
            return self._policy_defined()

    def policy(self):
        with self.lock:
            self._reload()
            return dict(self.data["policy"])

    def issue(self, agent_id, scopes, force=False, uid=None, gid=None, previous=None):
        """-> jeton. `previous` (liste) reçoit l'entrée remplacée (ou None) pour un retour arrière exact."""
        agent_id, scopes = validate.agent_id(agent_id), validate.scopes(scopes)
        if not scopes:
            raise ValueError("bad_scopes")
        with self.lock:
            self._reload()
            if self._policy_defined() and not force:
                pol = self.data["policy"].get(agent_id)
                if not isinstance(pol, list):
                    raise ValueError("agent_not_in_policy")
                if not set(scopes) <= set(pol):
                    raise ValueError("scope_exceeds_policy")
            tok = secrets.token_urlsafe(32)
            prev = self.data["agents"].get(agent_id)
            self.data["agents"][agent_id] = {"token_sha256": _sha(tok), "scopes": scopes,
                                             "created_at": round(self.clock(), 3)}
            self._save(uid, gid)
        if previous is not None:
            previous.append(dict(prev) if isinstance(prev, dict) else None)
        return tok

    def rollback_issue(self, agent_id, token, previous, uid=None, gid=None):
        """Annule une émission dont le fichier client n'a pas pu être écrit : si l'entrée courante est encore celle de
        `token`, l'entrée précédente est restaurée (ou l'agent retiré). -> True si annulé."""
        with self.lock:
            self._reload()
            cur = self.data["agents"].get(agent_id)
            if not isinstance(cur, dict) or not hmac.compare_digest(str(cur.get("token_sha256")), _sha(token)):
                return False
            if isinstance(previous, dict):
                self.data["agents"][agent_id] = previous
            else:
                self.data["agents"].pop(agent_id, None)
            self._save(uid, gid)
        return True

    def revoke(self, agent_id, uid=None, gid=None):
        with self.lock:
            self._reload()
            gone = self.data["agents"].pop(agent_id, None) is not None
            self._save(uid, gid)
        return gone

    def list(self):
        with self.lock:
            self._reload()
            return {k: {"scopes": v.get("scopes", []), "created_at": v.get("created_at")}
                    for k, v in sorted(self.data["agents"].items())}

    def authenticate(self, agent_id, token):
        """-> scopes (liste triée) ou None. Comparaison en temps constant."""
        if not isinstance(agent_id, str) or not validate.AGENT_ID_RE.fullmatch(agent_id) or \
                not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        with self.lock:
            self._reload()
            e = self.data["agents"].get(agent_id)
        if not isinstance(e, dict) or not isinstance(e.get("token_sha256"), str):
            return None
        if not hmac.compare_digest(_sha(token), e["token_sha256"]):
            return None
        try:
            return validate.scopes(e.get("scopes") or [])
        except ValueError:
            return None


def client_config_bytes(agent_id, token, socket_path):
    return (json.dumps({"v": 1, "agent_id": validate.agent_id(agent_id), "token": token, "socket": socket_path},
                       ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def write_client_config(path, agent_id, token, socket_path, uid=None, gid=None, mode=0o600):
    """Fichier de connexion d'un agent (sans privilège : dans le home de l'utilisateur courant, 0600)."""
    return fsutil.atomic_write_path(path, client_config_bytes(agent_id, token, socket_path), mode=mode, uid=uid,
                                    gid=gid)


def client_file_name(agent_id, uid):
    """Nom dans le dossier client root (/etc/opentv/agents) : aucune composante contrôlée par l'agent."""
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise ValueError("bad_uid")
    return "agent-%s-%d.json" % (validate.agent_id(agent_id), uid)


def redact_args(args):
    out = {}
    for k, v in sorted((args or {}).items()) if isinstance(args, dict) else []:
        if not isinstance(k, str) or k == "token":
            continue
        if isinstance(v, bool) or isinstance(v, int):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v if k in SAFE_ARG_KEYS and SAFE_ARG_RE.fullmatch(v) else {"len": len(v)}
        else:
            out[k] = {"type": type(v).__name__}
    return out


class Audit:
    """Journal JSONL 0600 borné (rotation simple .1). Jamais de jeton, requête, texte libre ni résultat."""

    def __init__(self, path, max_bytes=1024 * 1024, clock=time.time):
        self.path, self.max_bytes, self.clock = path, max_bytes, clock
        self.lock = threading.Lock()

    def record(self, agent, tool, args, ok, error, ms):
        rec = {"ts": round(self.clock(), 3), "agent": agent if isinstance(agent, str) and
               validate.AGENT_ID_RE.fullmatch(agent) else "?", "tool": tool if isinstance(tool, str) and
               SAFE_ARG_RE.fullmatch(tool.replace("_", "-")) else "?", "ok": bool(ok),
               "error": error if isinstance(error, str) and SAFE_ARG_RE.fullmatch(error.replace("_", "-")) else None,
               "ms": round(float(ms), 1), "args": redact_args(args)}
        line = (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with self.lock:
            try:
                try:
                    size = os.path.getsize(self.path)
                except OSError:
                    size = 0
                if size and size + len(line) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                pass   # l'audit ne doit jamais faire tomber l'outil ; le service journalise la panne ailleurs
