"""Notes TMDb du catalogue accepté après chaque rafraîchissement (`opentv-catalogue-ratings.service`, compte `opentv`).

Contrat :
- Moteur : `opentv_tmdb.py` du NanoPi, installé tel quel (empreinte dans le manifeste) et chargé par chemin depuis
  `OPENTV_FORYOU_ENGINE_DIR`. Correspondance prudente (imdb_id -> /find, titre exact/normalisé + année compatible,
  sinon `ambiguous`/`unmatched`), note = vote_average seulement si au moins un vote (0 vote : `null`, « indisponible »,
  jamais 0), identités IMDb et TMDb séparées : rien n'est réimplémenté ici, seule l'orchestration change.
- Déclenchement : `OnSuccess=` de `opentv-catalogue-refresh.service` (passage `updated`, `unchanged` ou `held`) ou
  `opentv-pi catalogue ratings`. Unité séparée : un TMDb absent ou en panne ne fait jamais échouer ni annuler un
  rafraîchissement déjà réussi ; le catalogue reste celui du rafraîchissement, et l'échec est rapporté (unité en
  échec, `catalogue status`, `health`).
- Reprise bornée : `opentv-catalogue-ratings-retry.timer` (toutes les heures) lance `catalogue ratings-retry`, qui ne
  refait un passage que si le DERNIER passage a échoué pour une cause transitoire (RETRYABLE : réseau, délai, TMDb
  indisponible, verrou occupé…), après un délai croissant (RETRY_BACKOFF_S) et au plus MAX_RETRIES fois par épisode.
  Succès, jeton absent, suspension ou échec non transitoire (jeton refusé, garde de perte de notes…) : aucune
  reprise, aucune requête, statut intact. Le passage quotidien remet le compteur à zéro.
- Secret : `/etc/opentv/tmdb/token` dans le dossier `/etc/opentv/tmdb` (root:opentv 0750, créé par tmpfiles à
  l'installation et au démarrage, donc présent AVANT toute unité) ; fichier root:opentv 0640, lien dur/symbolique
  refusés, groupe du service sans autre membre. Le DOSSIER (jamais le fichier, qu'un remplacement atomique
  démasquerait) est masqué par `InaccessiblePaths=-/etc/opentv/tmdb` dans toutes les autres unités ; le dépôt root
  vérifie dans /proc/<pid>/mountinfo que chaque unité du compte en cours d'exécution porte ce masque, sinon refus.
  Jamais dans Git, le manifeste, l'environnement d'une unité, argv, un journal ni une sortie. Seules les clés
  TMDB_READ_TOKEN (v4) / TMDB_API_KEY (v3) sont retenues, format validé par le moteur. Absent : `skipped`.
- Verrou commun `catalogue.json.lock` (même que le rafraîchissement, le retour arrière et la reprise) tenu pendant
  tout le passage : non bloquant (`busy`, code 75). Catalogue suspendu après un retour arrière (`.hold`) : `held`,
  aucune requête, catalogue intact.
- Écriture : copie modifiée en mémoire (champs `tmdb_*` et `attribution.tmdb` seulement), puis contrôles : catalogue
  sans `tmdb_*` identique (`_strip` du moteur), mêmes nombre et identifiants (site_id, source_id, chemin) dans le même
  ordre, champs TMDb valides, perte de notes bornée, relecture du candidat par le moteur du catalogue (même nombre
  d'éléments servis). Candidat même dossier (O_EXCL|O_NOFOLLOW, 0600, fsync), relu, catalogue actuel revérifié
  inchangé, `os.replace` atomique puis fsync du dossier. `catalogue.json.prev` (retour arrière du rafraîchissement)
  n'est jamais touché. Aucune modification : aucun octet réécrit.
- Journal et sorties : codes, nombres et durées seulement — jamais de jeton, titre, hôte ni URL.
- L'accueil voit les nouvelles notes comme tout remplacement du catalogue (signature inode/taille/mtime_ns) : ≤ ~62 s.

This product uses the TMDB API but is not endorsed or certified by TMDB.
"""
import copy
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import time
import urllib.parse

from . import agents, fsutil
from . import catalogue as cat

UNIT = "opentv-catalogue-ratings.service"
ENGINE_FILE = "opentv_tmdb.py"
ENGINE_ATTRS = ("take_credential", "Client", "enrich", "valid_tmdb_fields", "_strip", "TmdbError", "AuthError",
                "NotFound", "Abort", "STATUSES", "PROD_BASE", "http_get", "TMDB_FIELDS", "V4_RE", "V3_RE")
STATUS_NAME = cat.RATINGS_STATUS_NAME
STATE_SUFFIX = cat.RATINGS_STATE_SUFFIX
STATE_CAP = 1024 * 1024
CRED_CAP = 4096
CRED_KEYS = ("TMDB_READ_TOKEN", "TMDB_API_KEY")
# mêmes motifs que opentv_tmdb.V4_RE / V3_RE (test d'égalité) : la CLI root ne charge jamais le moteur
V4_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
V3_RE = re.compile(r"^[0-9a-f]{32}$")
STATUSES = ("matched", "ambiguous", "unmatched")   # = opentv_tmdb.STATUSES (test) : lecture root sans moteur
TRUSTED_UIDS = (0,)                                 # propriétaire exigé du dossier/fichier du jeton (tests : patché)
RETRY_UNIT = "opentv-catalogue-ratings-retry.service"
RETRY_TIMER = "opentv-catalogue-ratings-retry.timer"
# unités du compte du service (User=opentv) autres que les notes : le dossier du jeton doit y être masqué (test :
# égal à l'ensemble dérivé de systemd/*.service). Les autres comptes n'ont pas le groupe opentv (dossier 0750).
MASKED_UNITS = ("opentv.service", "opentv-cec.service", "opentv-cec-announce.service",
                "opentv-catalogue-refresh.service")
MOUNTINFO_CAP = 4 * 1024 * 1024
# reprise : seulement après un échec transitoire du DERNIER passage ; délai croissant depuis sa fin, puis abandon
# (l'échec reste rapporté) jusqu'au passage quotidien suivant. Minuterie horaire : au plus une tentative par heure.
RETRYABLE = frozenset(("provider_unavailable", "partial", "provider_error", "internal_error", "catalogue_changed",
                       "write_failed", "busy"))
RETRY_BACKOFF_S = (1800, 3600, 7200, 14400, 28800)
MAX_RETRIES = len(RETRY_BACKOFF_S)
API_HOST = "api.themoviedb.org"
DEADLINE_S = 240.0                                  # tout le passage réseau ; TimeoutStartSec de l'unité au-delà
# perte de notes tolérée par passage (fiche TMDb retirée, titre publié changé puis recorrespondance) : au-delà, rien
# n'est écrit (`rating_drop`) — jamais une disparition massive des notes à cause d'une réponse anormale du fournisseur
MAX_RATING_LOSS_PCT = 10
MIN_RATING_LOSS = 2
BUSY_EXIT = cat.BUSY_EXIT
CODE_RE = cat.CODE_RE
STATUS_FIELDS = ("result", "ok", "why", "items", "rated", "matched", "ambiguous", "unmatched", "pending", "new",
                 "rating_refresh", "recheck", "reused", "errors", "deferred", "requests", "wrote", "complete",
                 "circuit_open", "lost_ratings", "invalid_kept", "bytes", "sha256", "durable", "duration_s",
                 "started_at", "finished_at", "last_success_at", "held", "retry_attempts", "retryable", "mode")


def _noop(event, **fields):
    pass


Refused = cat.Refused


# --------------------------------------------------------------------------- moteur TMDb (chargé par chemin)
def load_engine(engine_dir, name="opentv_pi_tmdb_engine"):
    path = os.path.join(engine_dir, ENGINE_FILE)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Refused("engine_missing")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except (OSError, ImportError, SyntaxError):
        raise Refused("engine_missing")
    return check_engine(mod)


def check_engine(mod):
    for attr in ENGINE_ATTRS:
        if not hasattr(mod, attr):
            raise Refused("engine_incompatible")
    return mod


def _safe_engine_log(log):
    """Journal du moteur (tmdb_item_error : index, kind, why, kept) filtré par liste blanche ; tout le reste tombe."""
    def out(event, **f):
        rec = {}
        if isinstance(f.get("index"), int) and not isinstance(f.get("index"), bool):
            rec["index"] = f["index"]
        if f.get("kind") in ("new", "rating", "recheck"):
            rec["kind"] = f["kind"]
        if isinstance(f.get("why"), str) and CODE_RE.match(f["why"]):
            rec["why"] = f["why"]
        if f.get("kept") in STATUSES:
            rec["kept"] = f["kept"]
        ev = event if isinstance(event, str) and CODE_RE.match(event) else "event"
        log("ratings_" + ev[:32], **rec)
    return out


# --------------------------------------------------------------------------- jeton (jamais affiché ni journalisé)
def service_gid(cfg):
    """GID du groupe du service, exigé PRIVÉ : groupe principal du compte du service, aucun autre membre déclaré et
    aucun autre compte ne l'ayant pour groupe principal (sinon le jeton root:<groupe> 0640 fuirait). -> gid ;
    Refused(service_group_missing | service_group_not_private) sinon."""
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(cfg.service_user)
        g = grp.getgrnam(cfg.service_user)
    except KeyError:
        raise Refused("service_group_missing")
    if pw.pw_uid == 0 or g.gr_gid == 0 or g.gr_gid != pw.pw_gid:
        raise Refused("service_group_not_private")
    if any(m != pw.pw_name for m in g.gr_mem):
        raise Refused("service_group_not_private")
    if any(p.pw_gid == g.gr_gid and p.pw_uid != pw.pw_uid for p in pwd.getpwall()):
        raise Refused("service_group_not_private")
    return g.gr_gid


def _check_dir_stat(st, gid):
    """Dossier du jeton : vrai dossier (jamais un lien : O_NOFOLLOW), propriétaire de confiance, groupe du service,
    ni écriture groupe, ni accès « autres », ni setuid/setgid/collant (0750 attendu)."""
    if not stat.S_ISDIR(st.st_mode):
        return "dir_not_directory"
    if st.st_uid not in TRUSTED_UIDS:
        return "dir_owner"
    if st.st_gid != gid:
        return "dir_group"
    if stat.S_IMODE(st.st_mode) & 0o7027:
        return "dir_mode"
    return None


def _check_file_stat(st, gid):
    """Fichier du jeton : régulier, un seul lien, propriétaire de confiance, groupe du service, 0640 au plus (lecture
    groupe exigée : sinon le service ne peut pas le lire)."""
    if not stat.S_ISREG(st.st_mode):
        return "not_regular"
    if st.st_nlink != 1:
        return "hardlink"
    if st.st_uid not in TRUSTED_UIDS:
        return "owner"
    if st.st_gid != gid:
        return "group"
    if stat.S_IMODE(st.st_mode) & ~0o640 or not st.st_mode & 0o040:
        return "mode"
    if st.st_size > CRED_CAP:
        return "too_large"
    return None


def _open_cred_dir(cfg, gid):
    """-> descripteur du dossier du jeton (parent vérifié composant par composant, dossier sans lien), None si le
    dossier est absent ; Refused(credential_unsafe, why) sinon."""
    parent, name = os.path.split(os.path.abspath(cfg.tmdb_dir))
    try:
        pfd = fsutil.open_trusted_dir(parent, TRUSTED_UIDS)
    except fsutil.UnsafePath as e:
        raise Refused("credential_unsafe", "parent_" + re.sub(r"[^a-z_]", "", e.reason)[:24])
    except OSError:
        raise Refused("credential_unsafe", "parent")
    try:
        try:
            dfd = os.open(name, os.O_RDONLY | fsutil.O_DIRECTORY | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC, dir_fd=pfd)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused("credential_unsafe", "dir_open")
    finally:
        os.close(pfd)
    why = _check_dir_stat(os.fstat(dfd), gid)
    if why:
        os.close(dfd)
        raise Refused("credential_unsafe", why)
    return dfd


def credential_check(cfg):
    """Lecture seule, aussi pour root, SANS lire le contenu -> (état, raison) : ("missing", None) | ("present", None)
    | ("unsafe", code). Mêmes contrôles que la lecture par le service : groupe privé du service, dossier et fichier
    (propriétaire, groupe, mode, lien symbolique, lien dur)."""
    try:
        gid = service_gid(cfg)
        dfd = _open_cred_dir(cfg, gid)
    except Refused as e:
        return "unsafe", e.why or e.code
    if dfd is None:
        return "missing", "dir_missing"
    try:
        try:
            st = os.stat(cfg.tmdb_token_name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unsafe", "stat"
        why = _check_file_stat(st, gid)
        return ("unsafe", why) if why else ("present", None)
    finally:
        os.close(dfd)


def credential_state(cfg):
    return credential_check(cfg)[0]


def parse_credential(text):
    """KEY=VALUE ; seules TMDB_READ_TOKEN / TMDB_API_KEY ; guillemets retirés ; rien n'est évalué."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k in CRED_KEYS and v:
            out[k] = v
    return out


def read_credential(cfg):
    """-> dict {TMDB_READ_TOKEN|TMDB_API_KEY: valeur} ; None si absent (dossier ou fichier) ;
    Refused(credential_unsafe|credential_invalid) sinon. Ouverture relative au dossier vérifié, sans suivre de lien,
    puis fstat du descripteur ouvert (aucune course entre contrôle et lecture)."""
    gid = service_gid(cfg)
    dfd = _open_cred_dir(cfg, gid)
    if dfd is None:
        return None
    try:
        try:
            fd = os.open(cfg.tmdb_token_name, os.O_RDONLY | fsutil.O_NOFOLLOW | os.O_NONBLOCK | fsutil.O_CLOEXEC,
                         dir_fd=dfd)
        except FileNotFoundError:
            return None
        except OSError:
            raise Refused("credential_unsafe", "open")
    finally:
        os.close(dfd)
    try:
        why = _check_file_stat(os.fstat(fd), gid)
        if why:
            raise Refused("credential_unsafe", why)
        data = os.read(fd, CRED_CAP + 1)
    finally:
        os.close(fd)
    if len(data) > CRED_CAP:
        raise Refused("credential_unsafe", "too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("credential_invalid", "encoding")
    finally:
        data = None
    env = parse_credential(text)
    text = None
    if not env:
        raise Refused("credential_invalid", "no_key")
    return env


def exposed_units(cfg, runner, proc="/proc"):
    """(root) Unités du compte du service (MASKED_UNITS) EN COURS D'EXÉCUTION dont l'espace de montage ne masque pas
    cfg.tmdb_dir (démarrées avant la création du dossier, ou dossier recréé depuis : le masque est alors détaché).
    -> liste de noms d'unités ; Refused(exposure_unverified) si l'état ne peut être établi (fail-closed)."""
    bad = []
    want = os.path.abspath(cfg.tmdb_dir)
    for u in MASKED_UNITS:
        rc, out = runner(["systemctl", "show", "--property=MainPID", "--value", u], 5)
        v = (out or "").strip()
        if rc != 0 or not v.isdigit() or len(v) > 10:
            raise Refused("exposure_unverified")
        pid = int(v)
        if pid == 0:
            continue                          # arrêtée : démarrera avec le masque (le dossier existe déjà)
        try:
            with open(os.path.join(proc, str(pid), "mountinfo"), "rb") as f:
                data = f.read(MOUNTINFO_CAP + 1)
        except FileNotFoundError:
            continue                          # terminée entre-temps
        except OSError:
            raise Refused("exposure_unverified")
        if len(data) > MOUNTINFO_CAP or not _masked(data, want):
            bad.append(u)
    return bad


def _masked(data, path):
    for line in data.split(b"\n"):
        f = line.split(b" ")
        if len(f) > 4:
            try:
                mp = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]), f[4]).decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
            if mp == path:
                return True
    return False


# --------------------------------------------------------------------------- API officielle, adresse publique
def _transport_code(e):
    return ("transport_" + re.sub(r"[^a-z0-9_]", "", type(e).__name__.lower()))[:40]


def _transport_safe(engine):
    """Décorateur : toute exception inattendue du transport (http.client.IncompleteRead, RemoteDisconnected, erreur
    TLS ou de décodage…) devient engine.TmdbError(code de classe) SANS contexte chaîné : le moteur la compte comme
    une erreur d'élément (élément gardé tel quel, disjoncteur), jamais une trace, une URL (clé v3) ni un en-tête."""
    def wrap(fn):
        def call(url, headers, timeout):
            try:
                return fn(url, headers, timeout)
            except engine.TmdbError:
                raise
            except Exception as e:   # noqa: BLE001 — traduit, jamais propagé avec son message
                code = _transport_code(e)
            raise engine.TmdbError(code)
        return call
    return wrap


def guarded_fetch(engine, deadline, mono=time.monotonic, resolve=None, local=None):
    """http_get du moteur (TLS vérifié, sans redirection, corps borné) précédé : de l'échéance globale du passage, du
    nom exact de l'API officielle et d'un contrôle des adresses résolues (boucle locale, privées, lien local, CGNAT,
    multicast, la boîte : refusées). Défense en profondeur avec IPAddressDeny de l'unité."""
    from .playerd import addr_blocked, local_addresses, resolve_host
    resolve = resolve or resolve_host
    local = local or local_addresses

    def fetch(url, headers, timeout):
        return _guarded(url, headers, timeout)

    @_transport_safe(engine)
    def _guarded(url, headers, timeout):
        left = deadline - mono()
        if left <= 0:
            raise engine.TmdbError("deadline")
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host != API_HOST:
            raise engine.TmdbError("host_refused")
        addrs = resolve(host, min(3.0, max(0.5, left)))
        if not addrs:
            raise engine.TmdbError("dns")
        mine = local()
        if any(addr_blocked(a, mine) for a in addrs):
            raise engine.TmdbError("non_public_address")
        return engine.http_get(url, headers, max(0.5, min(timeout, deadline - mono())))
    return fetch


# --------------------------------------------------------------------------- comptes (lecture root sans moteur)
def _rated(it):
    r, v = it.get("tmdb_rating"), it.get("tmdb_votes")
    return (it.get("tmdb_match_status") == "matched" and isinstance(r, (int, float)) and not isinstance(r, bool)
            and isinstance(v, int) and not isinstance(v, bool) and v >= 1)


def coverage(doc):
    """{items, rated, matched, ambiguous, unmatched, pending} ; pending = aucun statut TMDb (élément jamais traité,
    typiquement un ajout du dernier rafraîchissement) ; None si le document n'a pas la forme d'un catalogue."""
    items = cat._doc_items(doc)
    if items is None:
        return None
    n = {"items": len(items), "rated": 0, "pending": 0}
    n.update({s: 0 for s in STATUSES})
    for it in items:
        st = it.get("tmdb_match_status") if isinstance(it, dict) else None
        if st in STATUSES:
            n[st] += 1
            n["rated"] += _rated(it)
        else:
            n["pending"] += 1
    return n


def _ids(items):
    return [(it.get("site_id"), it.get("source_id"), it.get("detail_path")) if isinstance(it, dict) else None
            for it in items]


# --------------------------------------------------------------------------- statut persisté
def _status_clean(d):
    out = {}
    for k in STATUS_FIELDS:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)) and not (isinstance(v, float) and not math.isfinite(v)):
            out[k] = v
        elif isinstance(v, str) and (CODE_RE.match(v) or re.match(r"^[0-9a-f]{64}$", v)):
            out[k] = v
    return out


def _status_read(dfd, any_owner=False):
    try:
        got = cat._read_at(dfd, STATUS_NAME, cat.STATUS_CAP, any_owner=any_owner)
    except (Refused, OSError):
        return {}
    doc = cat._parse(got[0]) if got else None
    return doc if isinstance(doc, dict) else {}


def _status_write(dfd, res, prev):
    st = _status_clean(res)
    st["retryable"] = res.get("result") in RETRYABLE
    last = prev.get("last_success_at") if isinstance(prev.get("last_success_at"), (int, float)) else None
    st["last_success_at"] = res["finished_at"] if res.get("ok") and res["result"] in ("updated", "unchanged") \
        else last
    st["v"] = 1
    try:
        fsutil.atomic_write(dfd, STATUS_NAME, json.dumps(st, sort_keys=True).encode("ascii"), mode=0o600)
    except OSError:
        pass   # informatif : jamais une cause d'échec


# --------------------------------------------------------------------------- passage complet
def retry_due(last, now):
    """Décision de reprise d'après le statut du DERNIER passage -> (due, code) ; code ∈ retry_not_needed |
    retry_wait | retry_exhausted | retry_due. Aucun statut, succès, saut ou échec non transitoire : jamais."""
    if not isinstance(last, dict) or last.get("result") not in RETRYABLE or last.get("ok") is not False:
        return False, "retry_not_needed"
    n = last.get("retry_attempts")
    n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
    if n >= MAX_RETRIES:
        return False, "retry_exhausted"
    fin = last.get("finished_at")
    if not isinstance(fin, (int, float)) or isinstance(fin, bool):
        return True, "retry_due"
    elapsed = now - fin
    # horloge revenue en arrière : due (la minuterie horaire et MAX_RETRIES bornent toujours le nombre de passages)
    if 0 <= elapsed < RETRY_BACKOFF_S[n]:
        return False, "retry_wait"
    return True, "retry_due"


def run(cfg, fetch=None, clock=time.time, mono=time.monotonic, log=_noop, engine=None, bridge=None,
        sleep=time.sleep, retry=False):
    """Un passage. -> dict sans jeton, titre, hôte ni URL.

    retry=True (`catalogue ratings-retry`, minuterie horaire) : passage seulement si retry_due() sur le statut lu
    SOUS le verrou ; sinon retour immédiat (retry_not_needed | retry_wait | retry_exhausted) sans réseau ni écriture.

    Codes : updated | unchanged | partial | skipped | held | busy | refus (credential_unsafe, credential_invalid,
    auth_failed, provider_unavailable, provider_error, catalogue_invalid, invalid_candidate, rating_drop,
    catalogue_changed, write_failed, engine_missing, hold_unsafe, current_unsafe…)."""
    t0 = mono()
    res = {"ok": False, "result": "error", "started_at": round(clock(), 3), "mode": "retry" if retry else "daily"}
    d, name = os.path.split(os.path.abspath(cfg.catalogue_file))
    try:
        dfd = fsutil.open_dir(d)
    except OSError:
        return dict(res, result="unsafe_dir")
    try:
        st = os.fstat(dfd)
        if st.st_uid != os.geteuid():
            return dict(res, result="wrong_user")
        if st.st_mode & 0o022:
            return dict(res, result="unsafe_dir")
        try:
            lfd = agents.open_lock(os.path.join(d, name + ".lock"))
        except agents.RegistryBusy:
            return dict(res, result="busy")
        except OSError:
            return dict(res, result="lock_unsafe")
        try:
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                res.update(result="busy")
                if not retry:
                    # passage quotidien empêché (rafraîchissement, retour arrière ou reprise en cours) : noté sans le
                    # verrou (écriture atomique, informative) pour que la reprise horaire le refasse, jamais 24 h plus
                    # tard. Une reprise occupée ne note rien (le titulaire du verrou écrira son propre statut).
                    res["finished_at"] = round(clock(), 3)
                    res["retry_attempts"] = 0
                    _status_write(dfd, res, _status_read(dfd))
                return res
            prev_status = _status_read(dfd)
            if retry:
                due, why = retry_due(prev_status, clock())
                if not due:
                    return {"ok": True, "result": why, "mode": "retry",
                            "retry_attempts": prev_status.get("retry_attempts", 0)}
                prev_n = prev_status.get("retry_attempts")
                res["retry_attempts"] = (prev_n if isinstance(prev_n, int) and not isinstance(prev_n, bool)
                                         and prev_n >= 0 else 0) + 1
            else:
                res["retry_attempts"] = 0
            try:
                cat._cleanup(dfd, name)
                res.update(_run_locked(cfg, dfd, d, name, fetch, clock, mono, log, engine, bridge, sleep))
            except Refused as e:
                res.update(result=e.code)
                if e.why:
                    res["why"] = e.why
            except Exception as e:   # jamais de trace (URL, jeton dans un en-tête…) : type seulement
                res.update(result="internal_error", why=type(e).__name__.lower()[:48])
            res["duration_s"] = round(mono() - t0, 3)
            res["finished_at"] = round(clock(), 3)
            _status_write(dfd, res, prev_status)
            return res
        finally:
            os.close(lfd)
    finally:
        os.close(dfd)


def _load_state(dfd, name):
    try:
        got = cat._read_at(dfd, name + STATE_SUFFIX, STATE_CAP)
    except Refused:
        got = None   # état dangereux ou trop gros : ignoré (seulement des horodatages), réécrit proprement
    doc = cat._parse(got[0]) if got else None
    items = doc.get("items") if isinstance(doc, dict) else None
    if isinstance(items, dict):
        return {"v": 1, "items": {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)}}
    return {"v": 1, "items": {}}


def _run_locked(cfg, dfd, d, name, fetch, clock, mono, log, engine, bridge, sleep):
    # 1) suspendu après un retour arrière : rien (ni jeton lu, ni requête, ni moteur chargé)
    if cat._hold_state(dfd, name) is not None:
        return {"ok": True, "result": "held", "held": True}
    # 2) jeton : absent = non configuré (succès, aucune requête) ; dangereux/invalide = échec
    env = read_credential(cfg)
    if env is None:
        return {"ok": True, "result": "skipped", "why": "credential_missing"}
    # 3) catalogue actuel (fichier régulier du compte, borné)
    got = cat._read_at(dfd, name)
    if got is None:
        return {"ok": True, "result": "skipped", "why": "no_catalogue"}
    old_bytes, old_st = got
    doc = cat._parse(old_bytes)
    items = cat._doc_items(doc)
    if items is None or not items or len(items) > cat.MAX_ITEMS or \
            ("count" in doc and doc["count"] != len(items)) or not all(isinstance(x, dict) for x in items):
        raise Refused("catalogue_invalid", "shape")
    # 4) moteurs : catalogue (nombre d'éléments réellement servis, avant/après) et TMDb
    bridge = bridge or cat._load_engine(cfg, None)
    detail_re = bridge.Config({}).detail_re
    served_before = len(bridge.load_catalogue_meta(os.path.join(d, name), detail_re, cap=cat.MAX_ITEMS)[0])
    engine = check_engine(engine) if engine is not None else load_engine(cfg.engine_dir)
    engine.log = _safe_engine_log(log)
    try:
        cred = engine.take_credential(env)
    except engine.Abort:
        raise Refused("credential_invalid", "format")
    finally:
        env.clear()
    # toute exception de transport (même d'un fetch injecté) devient TmdbError : erreur d'élément, jamais une trace
    fetch = _transport_safe(engine)(fetch or guarded_fetch(engine, mono() + DEADLINE_S, mono=mono))
    client = engine.Client(cred, fetch=fetch, max_requests=cfg.tmdb_max_requests, monotonic=mono, sleep=sleep)
    cred = None
    # 5) enrichissement en mémoire (champs tmdb_* seulement), horodatages dans l'état séparé
    new = copy.deepcopy(doc)
    state = _load_state(dfd, name)
    try:
        n = engine.enrich(new, state, client, cfg.tmdb_languages, cfg.tmdb_rating_max_age_s, cfg.tmdb_recheck_s,
                          now=clock())
    except engine.AuthError:
        raise Refused("auth_failed")              # rien n'est écrit
    except engine.NotFound:
        raise Refused("provider_error", "not_found")
    except engine.Abort:
        raise Refused("catalogue_invalid", "engine")
    finally:
        requests = client.requests
        client = None
    # 6) contrôles du candidat
    if engine._strip(doc) != engine._strip(new):
        raise Refused("invalid_candidate", "non_tmdb_change")
    new_items = new["items"]
    if len(new_items) != len(items) or _ids(new_items) != _ids(items) or new.get("count", len(new_items)) != \
            doc.get("count", len(items)):
        raise Refused("invalid_candidate", "ids")
    # ce passage n'introduit JAMAIS de champ TMDb invalide : un élément invalide n'est admis que s'il est resté
    # identique (valeur antérieure laissée telle quelle après une erreur d'élément du fournisseur) ; tout élément que
    # ce passage a modifié doit être valide
    invalid_kept = 0
    for a, b in zip(items, new_items):
        if not engine.valid_tmdb_fields(b):
            if a != b:
                raise Refused("invalid_candidate", "tmdb_fields")
            invalid_kept += 1
    lost = sum(1 for a, b in zip(items, new_items) if _rated(a) and not _rated(b))
    before_rated = sum(1 for a in items if _rated(a))
    if lost > max(MIN_RATING_LOSS, math.ceil(before_rated * MAX_RATING_LOSS_PCT / 100.0)):
        raise Refused("rating_drop")
    out = {"requests": requests, "lost_ratings": lost, "invalid_kept": invalid_kept}
    for k in ("new", "rating_refresh", "recheck", "reused", "errors", "deferred"):
        out[k] = n.get(k, 0)
    out["circuit_open"] = bool(n.get("circuit_open"))
    out.update(coverage(new))
    # 7) écriture (seulement si quelque chose a changé), puis état des vérifications
    if new == doc:
        out["wrote"] = False
    else:
        data = cat.serialize(new)
        out.update(_replace(bridge, detail_re, dfd, d, name, data, old_st, new, served_before))
        out["wrote"] = True
    try:
        fsutil.atomic_write(dfd, name + STATE_SUFFIX, cat.serialize(state), mode=0o600)
    except OSError:
        pass   # horodatages seulement : le prochain passage revérifie un peu plus tôt
    out["complete"] = not out["errors"] and not out["deferred"]
    if out["circuit_open"] or out["errors"]:
        out["ok"] = False
        out["result"] = "partial" if out["wrote"] else "provider_unavailable"
    else:
        out["ok"] = True
        out["result"] = "updated" if out["wrote"] else "unchanged"
    log("catalogue_ratings_done", result=out["result"], items=out["items"], rated=out["rated"],
        pending=out["pending"], errors=out["errors"], requests=requests)
    return out


def _replace(bridge, detail_re, dfd, d, name, data, old_st, new, served_before):
    """Candidat même dossier -> relu -> identique -> relu par le moteur du catalogue -> catalogue actuel inchangé ->
    os.replace -> fsync du dossier. Le candidat est toujours retiré ; le catalogue n'est jamais modifié avant le
    renommage final. `.prev` n'est jamais touché."""
    if len(data) > cat.MAX_BYTES:
        raise Refused("too_large")
    cand = ".%s.%s.candidate" % (name, secrets.token_hex(6))
    try:
        fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC, 0o600,
                     dir_fd=dfd)
    except OSError:
        raise Refused("write_failed", "candidate_open")
    try:
        try:
            view = memoryview(data)
            while view:
                k = os.write(fd, view)
                view = view[k:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        except OSError:
            raise Refused("write_failed", "candidate_write")
        finally:
            os.close(fd)
        got = cat._read_at(dfd, cand)
        if got is None or got[0] != data or cat._parse(got[0]) != new:
            raise Refused("invalid_candidate", "readback")
        served = bridge.load_catalogue_meta(os.path.join(d, cand), detail_re, cap=cat.MAX_ITEMS)[0]
        if len(served) != served_before:
            raise Refused("invalid_candidate", "engine_readback")
        if not cat._same_file(cat._stat_at(dfd, name), old_st):
            raise Refused("catalogue_changed")
        try:
            os.replace(cand, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError:
            raise Refused("write_failed", "replace")
        durable = True
        try:
            os.fsync(dfd)
        except OSError:
            durable = False
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "durable": durable}
    finally:
        try:
            os.unlink(cand, dir_fd=dfd)
        except OSError:
            pass


# --------------------------------------------------------------------------- état (lecture seule, aussi pour root)
def status(cfg, clock=time.time):
    """Jeton (présence seulement, contenu jamais lu), couverture du catalogue actuel, dernier passage. Aucun moteur."""
    cred, why = credential_check(cfg)
    out = {"unit": UNIT, "credential": cred, "coverage": None, "last": {}}
    if cred == "unsafe" and why:
        out["credential_why"] = why
    d, name = os.path.split(os.path.abspath(cfg.catalogue_file))
    try:
        dfd = fsutil.open_dir(d)
    except OSError:
        out["error"] = "unreadable"
        return out
    try:
        try:
            got = cat._read_at(dfd, name, any_owner=True)
        except (Refused, OSError):
            got = None
        if got is not None:
            out["coverage"] = coverage(cat._parse(got[0]))
        out["last"] = _status_clean(_status_read(dfd, any_owner=True))
    finally:
        os.close(dfd)
    out["retry"] = retry_view(out["last"], clock())
    return out


def retry_view(last, now):
    """Reprise bornée : tentatives faites pour l'échec en cours, plafond, abandon, prochaine échéance (horodatage)."""
    _, why = retry_due(last, now)
    n = last.get("retry_attempts") if isinstance(last.get("retry_attempts"), int) else 0
    v = {"attempts": n, "max": MAX_RETRIES, "state": why}
    fin = last.get("finished_at")
    if why == "retry_wait" and isinstance(fin, (int, float)) and 0 <= n < MAX_RETRIES:
        v["not_before"] = round(fin + RETRY_BACKOFF_S[n], 3)
    return v


def health_view(cfg, runner=None):
    """Résumé pour `opentv-pi health` : n'influe pas sur « ok » de l'appareil (les notes sont un enrichissement), mais
    un échec est rapporté tel quel (`ok: false` + avertissement), jamais masqué. Sous root avec un jeton présent :
    `exposed` = unités du compte en cours d'exécution sans le masque du dossier du jeton (None : invérifiable)."""
    try:
        st = status(cfg)
    except Exception:
        return {"configured": None, "ok": False, "why": "status_unreadable"}
    last, cov = st.get("last") or {}, st.get("coverage") or {}
    view = {"configured": st["credential"] == "present", "credential": st["credential"],
            "last_result": last.get("result"), "last_success_at": last.get("last_success_at"),
            "rated": cov.get("rated"), "items": cov.get("items"), "pending": cov.get("pending"),
            "retry": st.get("retry")}
    if st.get("credential_why"):
        view["credential_why"] = st["credential_why"]
    if runner is not None and os.geteuid() == 0 and st["credential"] != "missing":
        try:
            view["exposed"] = exposed_units(cfg, runner)
        except Refused:
            view["exposed"] = None
    if st["credential"] == "unsafe":
        view["ok"] = False
    elif st["credential"] == "missing":
        view["ok"] = None          # non configuré : rien à rapporter comme panne
    elif not last:
        view["ok"] = None          # configuré, jamais passé (prochain rafraîchissement)
    else:
        view["ok"] = bool(last.get("ok"))
    return view


def last_finished(cfg):
    return (status(cfg).get("last") or {}).get("finished_at")


# --------------------------------------------------------------------------- dépôt du jeton (root, sans écho)
class Exposed(Refused):
    """Dépôt refusé : des unités du compte du service tournent sans le masque du dossier du jeton."""

    def __init__(self, units):
        super().__init__("credential_exposed")
        self.units = [u for u in units if u in MASKED_UNITS]


def ensure_cred_dir(cfg, gid, trusted_dir_uids=(0,)):
    """(root) -> descripteur du dossier du jeton root:<gid> 0750. Créé s'il manque (mkdirat 0700 puis fchown/fchmod
    sur SON descripteur), sinon seulement vérifié : jamais corrigé, renommé ni recréé (un dossier recréé détacherait
    le masque des unités déjà démarrées). Parent vérifié composant par composant (root, non inscriptible)."""
    parent, name = os.path.split(os.path.abspath(cfg.tmdb_dir))
    pfd = fsutil.open_trusted_dir(parent, trusted_dir_uids)
    try:
        created = False
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
            created = True
        except FileExistsError:
            pass
        dfd = os.open(name, os.O_RDONLY | fsutil.O_DIRECTORY | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC, dir_fd=pfd)
    finally:
        os.close(pfd)
    try:
        if created:
            os.fchown(dfd, 0, gid)
            os.fchmod(dfd, 0o750)
        why = _check_dir_stat(os.fstat(dfd), gid)
        if why:
            raise Refused("credential_unsafe", why)
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def write_credential(cfg, value, gid, runner, trusted_dir_uids=(0,), proc="/proc"):
    """(root) Écrit `TMDB_READ_TOKEN=…` (v4) ou `TMDB_API_KEY=…` (v3) dans <tmdb_dir>/token, root:<gid> 0640, par
    temporaire exclusif + fchown/fchmod sur le descripteur + rename atomique DANS le dossier masqué (le masque des
    autres unités porte sur le dossier : un remplacement du fichier ne peut pas le défaire). Ordre : format validé,
    dossier créé/vérifié, PUIS aucune unité du compte en cours sans masque (Exposed sinon, rien écrit), puis écriture.
    La valeur n'est jamais renvoyée. -> "bearer" | "api_key" ; ValueError(code), Refused, OSError sinon."""
    v = (value or "").strip()
    if V4_RE.match(v):
        kind, key = "bearer", "TMDB_READ_TOKEN"
    elif V3_RE.match(v):
        kind, key = "api_key", "TMDB_API_KEY"
    else:
        raise ValueError("credential_bad_format")
    dfd = ensure_cred_dir(cfg, gid, trusted_dir_uids)
    try:
        bad = exposed_units(cfg, runner, proc=proc)
        if bad:
            raise Exposed(bad)
        fsutil.atomic_write(dfd, cfg.tmdb_token_name,
                            ("# OpenTV : jeton TMDb (secret, jamais dans Git)\n%s=%s\n" % (key, v)),
                            mode=0o640, uid=0, gid=gid)
    finally:
        os.close(dfd)
    return kind
