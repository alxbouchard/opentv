"""Identité des agents locaux : registre (hash des jetons), politique de scopes, fichier client 0600, audit expurgé."""
import contextlib
import copy
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time

from . import fsutil, validate

SAFE_ARG_RE = re.compile(r"[a-z0-9/-]{1,40}")
SAFE_ARG_KEYS = ("action", "list", "id", "key")   # « key » : énumération fermée de remote_control
# Attente bornée du verrou inter-processus (service ⇄ CLI root) : jamais de blocage infini. Le service l'attend sous
# App.lock (QR de configuration, « Gérer les assistants ») : pendant une écriture admin concurrente — rare — le poll
# d'état de la TV peut donc rester suspendu jusqu'à cette durée, puis reprend seul (README, « Écritures concurrentes »).
LOCK_TIMEOUT_S = 5.0


# Identités DÉDIÉES dont le plafond est l'autorisation d'un agent de la TV (jamais leur propre ligne de politique) :
# « muse-device » (pont Muse, opentv-pi muse pair) suit « muse ». « Gérer les assistants » / le QR qui réduisent ou
# retirent Muse réduisent ou révoquent aussi l'identité du pont, dans la MÊME écriture ; l'authentification applique
# ce plafond à chaque appel (aucun plafond ⇒ refus). L'identité « muse » d'un autre client local n'est jamais touchée.
LINKED_POLICY = {"muse-device": "muse"}
# Scopes dont une identité liée a EXACTEMENT besoin pour fonctionner : en dessous, l'identité reste installée mais le
# pont est « en pause » — l'Agent Port refuse chacun de ses appels (BRIDGE_PAUSED), le pont ferme sa session et
# s'arrête (exit 78), et la TV ne compte pas Muse comme connecté par ce pont (musebridge.MUSE_SCOPES en dérive).
LINKED_REQUIRED = {"muse-device": ("control", "display", "feedback", "read")}
# Code d'erreur STABLE de l'Agent Port pour un pont lié en pause : TOUT appel (whoami compris) d'une identité liée dont
# les droits effectifs (enregistrés ∩ plafond du parent, relus à chaque appel) ne valent pas exactement
# LINKED_REQUIRED est refusé ainsi — jamais une session à moitié autorisée qui continuerait à lire, noter ou afficher.
# Le pont reconnaît ce code, ferme sa session Muse et s'arrête (exit 78, sans redémarrage automatique).
BRIDGE_PAUSED = "bridge_paused"


def linked_paused(agent, scopes):
    """Vrai si `agent` est une identité liée (« muse-device ») dont les droits EFFECTIFS `scopes` ne valent pas
    exactement LINKED_REQUIRED : pont « en pause ». Toujours faux pour une identité générique (« muse », « hermit »…)."""
    need = LINKED_REQUIRED.get(agent) if isinstance(agent, str) else None
    return need is not None and sorted(scopes or ()) != sorted(need)


def linked_of(parent):
    """Identités dédiées liées à `parent` (ordre stable)."""
    return tuple(sorted(c for c, p in LINKED_POLICY.items() if p == parent))


def canonical_agent(agent):
    """Agent PROPRIÉTAIRE d'une identité authentifiée : « muse-device » -> « muse ». Sert à tout ce qui est visible ou
    partagé sur la TV (nom affiché, « Proposé par … », « Message de … », budgets de débit) ; jamais à l'authentification
    ni à l'audit, qui gardent l'identité réellement authentifiée."""
    return LINKED_POLICY.get(agent, agent) if isinstance(agent, str) else agent


def ceiling(policy, agent):
    """Plafond de scopes d'`agent` dans `policy` (ligne de l'agent parent pour une identité liée). None si absent."""
    v = policy.get(LINKED_POLICY.get(agent, agent)) if isinstance(policy, dict) else None
    return v if isinstance(v, list) else None


class RegistryBusy(OSError):
    """Verrou du registre tenu trop longtemps par un autre processus : rien n'est écrit, nouvel essai possible."""

    def __init__(self):
        super().__init__(errno.EWOULDBLOCK, "registry_busy")


LOCK_OPEN_TRIES = 10   # créations/suppressions concurrentes du verrou : nombre borné d'essais, puis registry_busy
LOCK_OWNER_SETTLE_TRIES = 10   # verrou que root vient de créer : ≤ 50 ms pour voir son fchown vers le service


def _open_lock_fd(dfd, name):
    """Création exclusive OU ouverture de l'existant, sans course : O_EXCL|O_CREAT d'abord (lien, FIFO, dossier déjà
    présents ⇒ EEXIST, jamais suivis ni créés à travers) ; EEXIST ⇒ ouverture SANS O_CREAT de ce qui existe ; disparu
    entre les deux (ENOENT) ⇒ nouvel essai, au plus LOCK_OPEN_TRIES fois, puis RegistryBusy (réessayable, jamais
    « chemin dangereux »). Toute autre erreur (ELOOP sur un lien, EACCES…) ⇒ UnsafePath. -> (fd, créé_par_moi).
    (Sur macOS, l'ancien open(O_CREAT|O_NOFOLLOW) sans O_EXCL répondait ENOENT à plusieurs créateurs simultanés.)"""
    base = os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC
    for _ in range(LOCK_OPEN_TRIES):
        try:
            return os.open(name, base | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd), True
        except FileExistsError:
            pass
        except FileNotFoundError:   # macOS : O_CREAT|O_NOFOLLOW concurrents peuvent répondre ENOENT ⇒ nouvel essai
            continue
        except OSError as e:
            raise fsutil.UnsafePath("lock_open_refused:%s" % errno.errorcode.get(e.errno or 0, "?"))
        try:
            return os.open(name, base, dir_fd=dfd), False
        except FileNotFoundError:   # supprimé entre les deux appels : on recommence (borné)
            continue
        except OSError as e:   # ELOOP (lien), EACCES…
            raise fsutil.UnsafePath("lock_open_refused:%s" % errno.errorcode.get(e.errno or 0, "?"))
    raise RegistryBusy()


def open_lock(path, uid=None, gid=None):
    """Fichier verrou latéral (« agents.json.lock ») partagé par le service et la CLI root, dans le dossier d'état
    (0700 du compte du service ; root y entre par ses capacités). Créé de façon exclusive ou ouvert tel quel,
    relativement au dossier, sans jamais suivre de lien ni bloquer (FIFO) ; plusieurs processus qui le créent en même
    temps obtiennent tous le même fichier (course de création bornée : voir _open_lock_fd). Fichier régulier à lien
    unique exigé. Propriétaire : un fichier préexistant doit appartenir au compte attendu (`uid`, compte du service
    passé par root ; sinon le processus courant) — un fichier root préexistant inattendu est refusé, jamais
    « récupéré ». Seul un verrou que CE processus vient de créer est rendu au compte du service par fchown sur le
    descripteur ; mode 0600. Aucun contenu : seul compte le flock. -> descripteur."""
    d, name = os.path.split(os.path.abspath(path))
    dfd = fsutil.open_dir(d)
    try:
        fd, created = _open_lock_fd(dfd, name)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise fsutil.UnsafePath("lock_not_regular")
        if st.st_nlink != 1:
            raise fsutil.UnsafePath("lock_hardlinked")
        want = os.geteuid() if uid is None else uid
        if created:   # créé ici par O_EXCL (jamais un fichier d'autrui) : seul cas où il est rendu au service
            if st.st_uid != want:
                os.fchown(fd, want, -1 if gid is None else gid)
        else:
            # root peut être entre sa création exclusive et son fchown : brève attente bornée du transfert, puis refus
            for _ in range(LOCK_OWNER_SETTLE_TRIES):
                if st.st_uid == want:
                    break
                time.sleep(0.005)
                st = os.fstat(fd)
            if st.st_uid != want or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise fsutil.UnsafePath("lock_unexpected_owner" if st.st_uid != want else "lock_hardlinked")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def flock_bounded(fd, timeout, clock=time.monotonic, sleep=time.sleep):
    """flock exclusif, essais non bloquants pendant au plus `timeout` s. -> None ; RegistryBusy sinon."""
    deadline, delay = clock() + timeout, 0.002
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        left = deadline - clock()
        if left <= 0:
            raise RegistryBusy()
        sleep(min(delay, left))
        delay = min(delay * 2, 0.05)


def _sha(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def atomic_write_json(path, obj, mode=0o600, uid=None, gid=None):
    """Écriture atomique par descripteur (fsutil) : jamais de chown/chmod par chemin, lien pré-placé remplacé."""
    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    return fsutil.atomic_write_path(path, data, mode=mode, uid=uid, gid=gid)


def _valid_scopes(v):
    try:
        return validate.scopes(v) if isinstance(v, list) else []
    except ValueError:
        return []


def view_of(data, known):
    """Lecture validée unique, la même pour l'affichage et pour l'écriture : {agent: {"allowed": [...],
    "have": [...] | None[, "linked": {identité liée: [...] | None}]}}. Entrée de politique absente/invalide ⇒ [] ;
    identité absente ⇒ None ; scopes d'identité invalides ⇒ [] (l'Agent Port la refuse déjà). « have » ne décrit
    QUE l'identité du même nom ; les identités dédiées liées (« muse-device » pour « muse ») sont lues à part dans
    « linked » (scopes enregistrés, non plafonnés) : elles entrent dans l'empreinte, donc un pont installé ou retiré
    entre l'affichage et l'envoi rend la confirmation périmée."""
    pol = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    ags = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    out = {}
    for a in known:
        e = ags.get(a)
        out[a] = {"allowed": _valid_scopes(pol.get(a)),
                  "have": _valid_scopes(e.get("scopes")) if isinstance(e, dict) else None}
        kids = linked_of(a)
        if kids:
            out[a]["linked"] = {c: (_valid_scopes(ags[c].get("scopes")) if isinstance(ags.get(c), dict) else None)
                                for c in kids}
    return out


def view_fingerprint(view):
    """Empreinte non secrète de l'état montré au téléphone (détecte un changement entre affichage et envoi)."""
    return hashlib.sha256(json.dumps(view, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class Registry:
    """agents.json (0600, propriétaire opentv) : {"v":1, "agents": {id: {token_sha256, scopes, created_at}},
    "policy": {id: [scopes autorisés au QR]}, "policy_at": horodatage}. Rechargé quand le fichier change.

    Politique : tant que le QR n'a jamais été validé (policy_at absent et policy vide), l'administrateur local (root
    sur la boîte) peut émettre n'importe quel scope valide. Dès qu'une politique existe, elle est un plafond strict :
    agent absent/inconnu ⇒ « agent_not_in_policy », scope au-delà (y compris pour un agent « aucun accès », scopes
    vides) ⇒ « scope_exceeds_policy », sauf --force explicite sur la boîte.

    Toute écriture (issue, rollback_issue, revoke, set_policy, apply_policy_changes) est une lecture-modification-
    écriture sérialisée entre processus (service ⇄ CLI root) par un flock sur `agents.json.lock`, pris TOUJOURS après
    le verrou interne (ordre unique : App.lock → Registry.lock → flock ; aucun appel ne remonte) ; le registre est
    relu sous ce verrou (jamais d'écrasement d'une écriture concurrente). Registre présent mais illisible / invalide
    / non régulier ⇒ ValueError("registry_unreadable") : jamais remplacé, octets intacts."""

    def __init__(self, path, clock=time.time, lock_timeout=LOCK_TIMEOUT_S):
        self.path, self.clock = path, clock
        self.lock_path, self.lock_timeout = path + ".lock", lock_timeout
        self.lock = threading.Lock()
        self._sig, self.data = None, {"v": 1, "agents": {}, "policy": {}}
        self.corrupt = False   # fichier présent mais illisible/invalide : lecture fail-closed, jamais réécrit
        self._reload()

    def _reload(self):
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            self._sig, self.data, self.corrupt = None, {"v": 1, "agents": {}, "policy": {}}, False
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        corrupt = False
        try:   # lien ou FIFO à la place du registre : illisible (O_NOFOLLOW, non bloquant) ⇒ fail-closed
            d = json.loads(fsutil.read_regular(self.path, 4 * 1024 * 1024).decode("utf-8"))
            if not isinstance(d, dict) or d.get("v") != 1 or not isinstance(d.get("agents", {}), dict) or \
                    not isinstance(d.get("policy", {}), dict):
                raise ValueError
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            d, corrupt = {"v": 1, "agents": {}, "policy": {}}, True   # registre illisible : aucun agent (fail-closed)
        d.setdefault("agents", {})
        d.setdefault("policy", {})
        self._sig, self.data, self.corrupt = sig, d, corrupt

    def _save(self, uid=None, gid=None):
        atomic_write_json(self.path, self.data, uid=uid, gid=gid)
        self._sig = None

    @contextlib.contextmanager
    def _mutating(self, uid=None, gid=None):
        """Section critique d'écriture : verrou interne puis flock inter-processus (borné), relecture forcée du
        fichier sous le flock, refus d'un registre illisible. Toute exception ⇒ la mémoire sera relue du disque."""
        with self.lock:
            fd = open_lock(self.lock_path, uid, gid)
            try:
                flock_bounded(fd, self.lock_timeout)
                self._sig = None   # jamais de cache : un autre processus a pu écrire juste avant
                self._reload()
                if self.corrupt:
                    raise ValueError("registry_unreadable")
                yield
            except BaseException:
                self._sig = None
                raise
            finally:
                os.close(fd)   # libère le flock

    def set_policy(self, policy, uid=None, gid=None, revoke_exceeding=False):
        """Politique complète (QR de configuration). revoke_exceeding : dans la MÊME écriture, toute identité dont
        les scopes dépassent sa nouvelle autorisation (ou étrangère à la politique) est révoquée. -> [révoqués]."""
        clean = {validate.agent_id(k): validate.scopes(v) for k, v in policy.items()}
        with self._mutating(uid, gid):
            self.data["policy"] = clean
            self.data["policy_at"] = round(self.clock(), 3)
            gone = []
            if revoke_exceeding:
                for a, e in sorted(self.data["agents"].items()):
                    sc = e.get("scopes", []) if isinstance(e, dict) else None   # entrée non-objet ⇒ révoquée
                    if not isinstance(sc, list) or not set(map(str, sc)) <= set(ceiling(clean, a) or []):
                        gone.append(a)
                for a in gone:
                    del self.data["agents"][a]
            self._save(uid, gid)
        return gone

    def _policy_defined(self):
        pol = self.data.get("policy")
        return isinstance(pol, dict) and (bool(pol) or self.data.get("policy_at") is not None)

    def snapshot(self, known):
        """-> (vue validée, empreinte, illisible) en une seule lecture sous verrou (voir view_of)."""
        with self.lock:
            self._reload()
            v = view_of(self.data, known)
            return v, view_fingerprint(v), self.corrupt

    def apply_policy_changes(self, changes, known, expect, uid=None, gid=None):
        """Gestion des assistants depuis la TV : `changes` = {agent: scopes} pour les SEULES lignes réellement
        modifiées par l'utilisateur. Sous un seul verrou : registre illisible ⇒ ValueError("registry_unreadable") ;
        état différent de l'empreinte `expect` montrée au téléphone ⇒ ValueError("stale") ; rien n'est écrit.

        Écriture atomique d'une politique complète et sûre : agent modifié ⇒ nouveaux scopes et, dans la même
        écriture, son identité déjà émise est réduite au nouveau plafond (plus aucun scope ⇒ révoquée) ; agent connu
        non modifié ⇒ sa lecture validée actuelle (absente/invalide ⇒ [] = « Aucun accès » déjà affiché) et son
        identité n'est JAMAIS touchée ; entrées étrangères conservées telles quelles. N'émet jamais de jeton ni
        d'identité. Aucun changement ⇒ aucune écriture. Échec d'écriture ⇒ OSError, état mémoire rétabli.
        -> {agent: "policy"|"restricted"|"revoked"} pour chaque agent modifié."""
        known = tuple(validate.agent_id(k) for k in known)
        clean = {validate.agent_id(k): validate.scopes(v) for k, v in changes.items()}
        if not set(clean) <= set(known):
            raise ValueError("unknown_agent")
        with self._mutating(uid, gid):   # registre illisible ⇒ ValueError("registry_unreadable")
            cur = view_of(self.data, known)
            if not isinstance(expect, str) or not hmac.compare_digest(view_fingerprint(cur), expect):
                raise ValueError("stale")
            if not clean:
                return {}
            old = copy.deepcopy(self.data)
            pol = dict(self.data.get("policy") or {})
            for a in known:
                pol[a] = list(clean[a]) if a in clean else list(cur[a]["allowed"])
            self.data["policy"] = pol
            self.data["policy_at"] = round(self.clock(), 3)
            out = {}
            for a, allowed in clean.items():
                out[a] = "policy"
                e = self.data["agents"].get(a)
                if not isinstance(e, dict):
                    continue
                have = cur[a]["have"] or []   # déjà validé et trié ; invalide ⇒ [] ⇒ révoquée (fail-closed)
                keep = [x for x in have if x in allowed]
                if not keep:
                    del self.data["agents"][a]
                    out[a] = "revoked"
                elif keep != have:
                    e["scopes"] = keep
                    out[a] = "restricted"
            for child, parent in sorted(LINKED_POLICY.items()):   # identité dédiée liée : même plafond, même écriture
                e = self.data["agents"].get(child)
                if parent not in clean or e is None:
                    continue
                have = _valid_scopes(e.get("scopes")) if isinstance(e, dict) else []
                keep = [x for x in have if x in clean[parent]]
                if not keep:
                    del self.data["agents"][child]
                elif keep != have:
                    e["scopes"] = keep
            try:
                self._save(uid, gid)
            except BaseException:
                self.data = old
                raise
        return out

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
        with self._mutating(uid, gid):
            if (self._policy_defined() or agent_id in LINKED_POLICY) and not force:
                pol = ceiling(self.data["policy"], agent_id)
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
        with self._mutating(uid, gid):
            cur = self.data["agents"].get(agent_id)
            if not isinstance(cur, dict) or not hmac.compare_digest(str(cur.get("token_sha256")), _sha(token)):
                return False
            if isinstance(previous, dict):
                self.data["agents"][agent_id] = previous
            else:
                self.data["agents"].pop(agent_id, None)
            self._save(uid, gid)
        return True

    def revoke_token(self, agent_id, token, uid=None, gid=None):
        """Révoque `agent_id` SEULEMENT si son entrée courante est celle de `token` (identité dont l'appelant détient
        le fichier client) : une réémission par un autre client n'est jamais touchée. -> True si révoqué."""
        return self.rollback_issue(agent_id, token, None, uid=uid, gid=gid)

    def entry_matches(self, agent_id, token):
        """L'entrée courante de `agent_id` est-elle celle de `token` (comparaison en temps constant) ?"""
        with self.lock:
            self._reload()
            cur = self.data["agents"].get(agent_id)
        return isinstance(cur, dict) and isinstance(token, str) and \
            hmac.compare_digest(str(cur.get("token_sha256")), _sha(token))

    def revoke(self, agent_id, uid=None, gid=None):
        with self._mutating(uid, gid):
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
            cap = ceiling(self.data["policy"], agent_id) if agent_id in LINKED_POLICY else None
        if not isinstance(e, dict) or not isinstance(e.get("token_sha256"), str):
            return None
        if not hmac.compare_digest(_sha(token), e["token_sha256"]):
            return None
        try:
            scopes = validate.scopes(e.get("scopes") or [])
        except ValueError:
            return None
        if agent_id in LINKED_POLICY:   # identité liée : plafond du parent relu à chaque appel, absent ⇒ refus
            scopes = [x for x in scopes if x in (cap or [])]
            if not scopes:
                return None
        return scopes


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
