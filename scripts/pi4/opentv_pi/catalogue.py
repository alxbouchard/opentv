"""Rafraîchissement quotidien du catalogue accepté (`opentv-catalogue-refresh.timer`, compte `opentv`).

Contrat :
- Source : uniquement la source approuvée (`SourceConfig`, /etc/opentv/source.env root:opentv 0640) et le moteur
  NanoPi installé et vérifié par le manifeste (`Source` -> `opentv_bridge.Resolver`, `parse_listing`, `clean_item`,
  `load_catalogue_meta`). Source absente ou non approuvée : rien n'est émis (`skipped`).
- `opentv_bridge.refresh_catalogue` n'est PAS appelé : il remplace directement sa cible, accepte n'importe quelle page
  non vide et ne garde que les champs nettoyés (les champs d'enrichissement TMDb bruts et `attribution` seraient
  perdus, les affiches TMDb remplacées). Ici, seule l'orchestration change ; découverte, analyse et validation
  restent celles du moteur.
- Verrou latéral non bloquant `catalogue.json.lock` (même nom que l'enrichissement TMDb du NanoPi) : un second passage
  sort aussitôt (`busy`, code 75) sans rien toucher.
- Candidat ensemencé par le catalogue actuel (même `site_id`, sinon même chemin de même `site_id`) : description,
  affiche, IMDb, TMDb et tout champ d'enrichissement sont conservés tels quels ; seuls l'identité de l'époque
  (chemin, identifiant de lien, hôte) et titre/année/genre publiés viennent de la page de liste.
- Candidat écrit dans le MÊME dossier (`.catalogue.json.<hasard>.candidate`, O_EXCL|O_NOFOLLOW, 0600, fsync), relu
  depuis le disque puis validé : JSON borné (taille, profondeur), schéma du moteur pour chaque élément, identifiants
  et chemins uniques, `site_id` partout, bornes de nombre, baisse et recouvrement bornés par rapport au catalogue
  valide actuel (troncature suspecte refusée). Catalogue actuel inchangé pendant la passe, sinon refus.
- Validation réussie : instantané `catalogue.json.prev`, catalogue actuel revérifié juste avant `os.replace` atomique,
  puis fsync du dossier. Tout échec laisse le catalogue actuel intact octet pour octet et retire le candidat.
- Retour arrière (`rollback`) : pose d'abord le témoin de suspension `catalogue.json.hold` (0600, compte du service),
  puis restaure l'instantané validé. Tant que ce nom existe, tout passage (minuterie ou manuel) sort `held` AVANT toute
  requête et sans toucher au catalogue ; un témoin illisible ou dangereux (lien, FIFO, dossier, lien dur, autre
  propriétaire, trop gros, JSON invalide) est traité comme une suspension (`hold_unsafe`, échec, aucune requête).
  Reprise explicite : `resume` (retire le nom sous le verrou) ; la minuterie peut alors de nouveau mettre à jour.
- Fichiers temporaires orphelins (candidats, `.tmp` des écritures atomiques de ce module) retirés sous le verrou.
- Journal et sorties : codes d'erreur, nombres et durées seulement — jamais d'hôte, de préfixe, de jeton, d'URL ni de
  titre (journal du moteur caviardé par `source.redact_fields`).
- Aucun service n'est redémarré : le service recalcule « Pour toi » dès que la signature du catalogue (inode, taille,
  mtime_ns) change et la TV le redemande toutes les 60 s ⇒ remplacement visible en ≤ ~62 s à l'accueil (une fiche ou
  une lecture ouverte diffère l'application jusqu'à sa fermeture, focus réancré sur le même film).
"""
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import urllib.parse

from . import agents, fsutil
from . import source as sourcemod

MAX_BYTES = 8 * 1024 * 1024        # catalogue réel : ~1,2 Ko par élément enrichi
MAX_ITEMS = 2000                   # borne de lecture du moteur (load_catalogue_meta cap=2000)
DISCOVERY_BUDGET_S = 60.0
BUSY_EXIT = 75                     # EX_TEMPFAIL : un autre passage tient le verrou
UNIT = "opentv-catalogue-refresh.service"
STATUS_NAME = "catalogue-refresh.json"
STATUS_CAP = 16384
HOLD_SUFFIX = ".hold"              # catalogue.json.hold : rafraîchissement suspendu après un rollback
HOLD_CAP = 4096
ENGINE_ATTRS = ("parse_listing", "clean_item", "load_catalogue_meta", "valid_public_host", "FetchError",
                "http_fetch", "DiscoveryFailed", "NUM_ID_RE")
CODE_RE = re.compile(r"^[a-z_:0-9]{1,48}$")
GEN_RE = re.compile(r"^[0-9T:.Z+-]{1,40}$")
# champs du statut persisté / affiché : liste blanche (nombres, codes, horodatages)
STATUS_FIELDS = ("result", "ok", "why", "count", "previous_count", "added", "removed", "retained", "enriched_kept",
                 "bytes", "duration_s", "started_at", "finished_at", "last_success_at", "sha256", "durable", "held")
ACTIONS = ("refresh", "rollback", "resume")


def _noop(event, **fields):
    pass


class Refused(Exception):
    """Refus fail-closed : `code` (liste fermée) + `why` facultatif (code court, jamais d'hôte ni d'URL)."""

    def __init__(self, code, why=None):
        super().__init__(code)
        self.code = code
        self.why = why if isinstance(why, str) and CODE_RE.match(why) else None


def _why(e):
    s = str(e).lower()
    return s if CODE_RE.match(s) else type(e).__name__.lower()[:48]


# --------------------------------------------------------------------------- lecture bornée relative au dossier
def _read_at(dfd, name, cap=None, any_owner=False, code="current_unsafe", owners=None):
    """-> (bytes, stat) ; None si absent ; Refused(code) si lien, FIFO, dossier, lien dur, autre propriétaire (sauf
    any_owner : lecture d'état seule ; `owners` : liste admise, défaut compte courant + root) ou trop gros."""
    cap = MAX_BYTES if cap is None else cap
    try:
        fd = os.open(name, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError:
        raise Refused(code, "open")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Refused(code, "not_regular")
        if not any_owner and st.st_uid not in (owners if owners is not None else (os.geteuid(), 0)):
            raise Refused(code, "owner")
        if st.st_size > cap:
            raise Refused(code, "too_large")
        chunks, left = [], cap + 1
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > cap:
            raise Refused(code, "too_large")
        return data, st
    finally:
        os.close(fd)


def _parse(data):
    """JSON borné : None si illisible (décodage, valeur, profondeur excessive : RecursionError sous Python 3.11)."""
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _doc_items(doc):
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        return doc["items"]
    return None


def _same_file(a, b):
    if a is None or b is None:
        return a is None and b is None
    return (a.st_ino, a.st_size, a.st_mtime_ns, a.st_dev) == (b.st_ino, b.st_size, b.st_mtime_ns, b.st_dev)


def _stat_at(dfd, name):
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- validation (moteur + règles explicites)
def check_engine(engine):
    for attr in ENGINE_ATTRS:
        if not hasattr(engine, attr):
            raise Refused("engine_incompatible")
    return engine


def validate_doc(engine, detail_re, doc, path, baseline=None, min_items=1, max_drop_pct=None):
    """Valide un catalogue candidat déjà relu depuis le disque. -> {count, retained} ; Refused sinon.

    baseline : {"count", "site_ids"} du catalogue valide actuel (None : pas de référence, premier remplissage)."""
    items = _doc_items(doc)
    if items is None or not isinstance(doc.get("count"), int) or doc["count"] != len(items):
        raise Refused("invalid_candidate", "shape")
    if not engine.valid_public_host(doc.get("source")):
        raise Refused("invalid_candidate", "source")
    if not isinstance(doc.get("generated_at"), str) or not GEN_RE.match(doc["generated_at"]):
        raise Refused("invalid_candidate", "generated_at")
    n = len(items)
    if n < max(1, min_items):
        raise Refused("too_few")
    if n > MAX_ITEMS:
        raise Refused("too_many")
    paths, sids, links = set(), set(), set()
    for x in items:
        c = engine.clean_item(x, detail_re)
        if c is None or not c["title"].strip():
            raise Refused("invalid_candidate", "item")
        if not c["site_id"] or not engine.NUM_ID_RE.match(str(x.get("site_id"))):
            raise Refused("missing_site_ids")
        if c["detail_path"] in paths or c["site_id"] in sids or c["source_id"] in links:
            raise Refused("duplicate_ids")
        paths.add(c["detail_path"])
        sids.add(c["site_id"])
        links.add(c["source_id"])
    # relecture par le moteur lui-même (même code que l'accueil et « Regarder ») : rien d'écarté, même hôte
    loaded, gen, host = engine.load_catalogue_meta(path, detail_re, cap=MAX_ITEMS)
    if len(loaded) != n or host != doc["source"] or gen != doc["generated_at"]:
        raise Refused("invalid_candidate", "engine_readback")
    retained = None
    if baseline and baseline.get("count") and max_drop_pct is not None:
        keep = 1.0 - max_drop_pct / 100.0
        if n < math.ceil(baseline["count"] * keep):
            raise Refused("count_drop")
        old = baseline.get("site_ids") or set()
        retained = len(old & sids)
        if old and retained < math.ceil(len(old) * keep):
            raise Refused("low_overlap")
    return {"count": n, "retained": retained}


def baseline_of(engine, detail_re, doc):
    """Référence du catalogue actuel s'il est valide selon le moteur ; None sinon (aucune comparaison possible)."""
    items = _doc_items(doc)
    if not items:
        return None
    sids, count = set(), 0
    for x in items[:MAX_ITEMS * 2]:
        c = engine.clean_item(x, detail_re)
        if c is None:
            continue
        count += 1
        if c["site_id"]:
            sids.add(c["site_id"])
    return {"count": count, "site_ids": sids} if count else None


# --------------------------------------------------------------------------- fusion : enrichissements conservés
IDENTITY = ("detail_path", "source_id", "source")
PUBLISHED = ("title", "year", "genre")


def merge(engine, detail_re, old_items, fresh):
    """Éléments frais (page de liste) ensemencés par le catalogue actuel. -> (éléments, conservés, enrichis)."""
    by_sid, by_path = {}, {}
    for x in old_items or []:
        c = engine.clean_item(x, detail_re)
        if c is None:
            continue
        if c["site_id"]:
            by_sid.setdefault(c["site_id"], x)
        by_path.setdefault(c["detail_path"], (c["site_id"], x))
    out, kept, enriched = [], 0, 0
    for it in fresh:
        sid = it.get("site_id")
        prev = by_sid.get(sid) if sid else None
        if prev is None and it["detail_path"] in by_path:
            psid, p = by_path[it["detail_path"]]
            if not psid or psid == sid:   # même chemin d'un AUTRE film : jamais fusionné
                prev = p
        if prev is None:
            out.append(dict(it))
            continue
        kept += 1
        m = copy.deepcopy(prev)
        m.pop("detail_url", None)          # forme de référence d'une ancienne époque : le chemin fait foi
        for k in IDENTITY:
            m[k] = it.get(k)
        m["site_id"] = sid or (str(prev["site_id"]) if prev.get("site_id") is not None else None)
        for k in PUBLISHED:
            if it.get(k) not in (None, ""):
                m[k] = it[k]
        if not (isinstance(m.get("poster"), str) and m["poster"].startswith("https://")) and it.get("poster"):
            m["poster"] = it["poster"]
        if any(k.startswith(("imdb_", "tmdb_")) or k == "description" for k in prev):
            enriched += 1
        out.append(m)
    return out, kept, enriched


def serialize(doc):
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


# --------------------------------------------------------------------------- récupération : adresse publique exigée
def public_fetch(engine, resolve=None, local=None):
    """http_fetch du moteur précédé d'un contrôle des adresses résolues (boucle locale, RFC 1918, lien local, CGNAT,
    multicast, la boîte elle-même : refusés). Défense en profondeur avec IPAddressDeny de l'unité ; le moteur reste
    seul juge des noms (valid_public_host) et des redirections (aucune suivie hors même hôte)."""
    from .playerd import addr_blocked, local_addresses, resolve_host
    resolve = resolve or resolve_host
    local = local or local_addresses

    def fetch(url, timeout):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        addrs = resolve(host, min(3.0, max(0.5, timeout)))
        if not addrs:
            raise engine.FetchError("dns")
        mine = local()
        if any(addr_blocked(a, mine) for a in addrs):
            raise engine.FetchError("non_public_address")
        return engine.http_fetch(url, timeout)
    return fetch


# --------------------------------------------------------------------------- passe complète
def _status_read(dfd, any_owner=False):
    try:
        got = _read_at(dfd, STATUS_NAME, STATUS_CAP, any_owner=any_owner)
    except (Refused, OSError):
        return {}
    doc = _parse(got[0]) if got else None
    return doc if isinstance(doc, dict) else {}


def _status_clean(d):
    out = {}
    for k in STATUS_FIELDS:
        v = d.get(k)
        if isinstance(v, bool) or v is None:
            if v is not None:
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str) and (CODE_RE.match(v) or re.match(r"^[0-9a-f]{64}$", v)):
            out[k] = v
    return out


def _status_write(dfd, res, prev):
    st = _status_clean(res)
    last = prev.get("last_success_at") if isinstance(prev.get("last_success_at"), (int, float)) else None
    st["last_success_at"] = res["finished_at"] if res.get("ok") and res["result"] in ("updated", "unchanged") \
        else last
    st["v"] = 1
    try:
        fsutil.atomic_write(dfd, STATUS_NAME, json.dumps(st, sort_keys=True).encode("ascii"), mode=0o600)
    except OSError:
        pass   # le statut est informatif : jamais une cause d'échec du catalogue


def refresh(cfg, fetch=None, clock=time.time, mono=time.monotonic, log=_noop, engine=None, rollback=False):
    """Une passe (ou, rollback=True, retour à `catalogue.json.prev` validé + suspension). -> dict sans hôte ni URL.

    Codes : updated | unchanged | skipped | held | busy | rolled_back | refus (voir Refused)."""
    return run(cfg, "rollback" if rollback else "refresh", fetch=fetch, clock=clock, mono=mono, log=log, engine=engine)


def resume(cfg, clock=time.time, mono=time.monotonic, log=_noop):
    """Reprise explicite après un rollback : retire `catalogue.json.hold` sous le verrou. Aucune requête.

    Codes : resumed | not_held | busy | hold_unsafe (dossier à la place du témoin : nettoyage manuel)."""
    return run(cfg, "resume", clock=clock, mono=mono, log=log)


def run(cfg, action, fetch=None, clock=time.time, mono=time.monotonic, log=_noop, engine=None):
    if action not in ACTIONS:
        raise ValueError(action)
    t0 = mono()
    res = {"ok": False, "result": "error", "started_at": round(clock(), 3)}
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
                return dict(res, result="busy")
            prev_status = _status_read(dfd)
            try:
                _cleanup(dfd, name)
                if action == "resume":
                    res.update(_resume_locked(dfd, name))
                elif action == "rollback":
                    res.update(_rollback_locked(cfg, dfd, d, name, engine, clock))
                else:
                    # suspendu après un rollback : aucune requête, aucun moteur chargé, catalogue intact
                    if _hold_state(dfd, name) is not None:
                        res.update(ok=True, result="held", held=True)
                    else:
                        res.update(_refresh_locked(cfg, dfd, d, name, fetch, clock, log, engine))
            except Refused as e:
                res.update(result=e.code)
                if e.why:
                    res["why"] = e.why
            except Exception as e:   # jamais de trace (elle pourrait contenir une URL) : type seulement
                res.update(result="internal_error", why=type(e).__name__.lower()[:48])
            res["duration_s"] = round(mono() - t0, 3)
            res["finished_at"] = round(clock(), 3)
            _status_write(dfd, res, prev_status)
            return res
        finally:
            os.close(lfd)
    finally:
        os.close(dfd)


def _cleanup(dfd, name):
    """Orphelins d'un passage interrompu (coupure, SIGKILL) retirés sous le verrou : candidats et temporaires `.tmp`
    des écritures atomiques de CE module seulement (instantané, statut, témoin de suspension). Liste du dossier
    impossible : Refused(cleanup_failed), jamais une trace."""
    own = "|".join(re.escape(n) for n in (name + ".prev", STATUS_NAME, name + HOLD_SUFFIX))
    pat = re.compile(r"^(?:\.%s\.[0-9a-f]{12}\.candidate|\.(?:%s)\.[0-9a-f]{12}\.tmp)$" % (re.escape(name), own))
    try:
        entries = os.listdir(dfd)
    except OSError:
        raise Refused("cleanup_failed", "listdir")
    for entry in entries:
        if pat.match(entry):
            try:
                st = os.stat(entry, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    os.unlink(entry, dir_fd=dfd)
            except OSError:
                pass


def _load_engine(cfg, engine):
    try:
        return check_engine(engine or sourcemod.load_engine(cfg.engine_dir))
    except sourcemod.SourceError as e:
        raise Refused(e.code if CODE_RE.match(e.code) else "engine_missing")


def _current(engine, detail_re, dfd, name):
    """-> (bytes|None, stat|None, doc|None, baseline|None) ; Refused(current_unsafe) si dangereux."""
    got = _read_at(dfd, name)
    if got is None:
        return None, None, None, None
    data, st = got
    doc = _parse(data)
    base = baseline_of(engine, detail_re, doc)
    return data, st, (doc if base else None), base


def _refresh_locked(cfg, dfd, d, name, fetch, clock, log, engine):
    engine = _load_engine(cfg, engine)
    scfg = sourcemod.SourceConfig.load(cfg.source_file, uid_ok=(0, os.geteuid()), valid_host=engine.valid_public_host)
    if scfg.status in ("missing", "unapproved"):
        return {"ok": True, "result": "skipped", "why": "source_" + scfg.status}
    if not scfg.ok:
        raise Refused("source_" + scfg.status)
    src = sourcemod.Source(scfg, cfg.engine_dir, cfg.source_state, cfg.catalogue_file, log=log,
                           fetch=fetch or public_fetch(engine), clock=clock, engine=engine)
    detail_re = src.detail_re
    old_bytes, old_st, old_doc, base = _current(engine, detail_re, dfd, name)
    # découverte de l'époque (préfixe/jeton/migration) par le résolveur du moteur, budget borné
    try:
        c = src.resolver.refresh(budget=DISCOVERY_BUDGET_S)
    except engine.DiscoveryFailed as e:
        raise Refused("discovery_failed", _why(e))
    ep = src.resolver.epoch()
    if not c or not ep or not ep.get("token"):
        raise Refused("discovery_failed", "no_epoch")
    url = ep["origin"] + c["listing_path"]
    try:
        status, headers, body = src.resolver.fetch(url, scfg.fetch_timeout_s * 2)
    except engine.FetchError as e:
        raise Refused("listing_failed", "fetch:" + _why(e)[:40])
    if status != 200 or not str(headers.get("content-type", "")).lower().startswith("text/html"):
        raise Refused("listing_failed", "status_%d" % status if isinstance(status, int) else "status")
    token_re = re.compile(r"^b/%s/[0-9]{1,12}$" % re.escape(ep["token"]))
    fresh = engine.parse_listing(body.decode("utf-8", "replace"), url, src.resolver, token_re, cap=MAX_ITEMS)
    if not fresh:
        raise Refused("listing_empty")
    items, kept, enriched = merge(engine, detail_re, _doc_items(old_doc), fresh)
    if old_doc is not None and old_doc.get("source") == ep["host"] and old_doc.get("items") == items:
        return {"ok": True, "result": "unchanged", "count": len(items), "previous_count": base["count"],
                "retained": kept, "enriched_kept": enriched}
    doc = {k: v for k, v in (old_doc or {}).items() if k not in ("items", "count", "generated_at", "source")}
    doc.update(generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())), source=ep["host"],
               count=len(items), items=items)
    out = _commit(engine, detail_re, dfd, d, name, serialize(doc), old_bytes, old_st, base, cfg)
    out.update(previous_count=base["count"] if base else 0, added=len(items) - kept,
               removed=(base["count"] - kept) if base else 0, enriched_kept=enriched)
    log("catalogue_refresh_committed", count=out["count"], retained=kept)
    return out


# --------------------------------------------------------------------------- suspension après rollback
def _hold_state(dfd, name, owner=None):
    """-> None si aucun témoin ; dict du témoin valide ; Refused(hold_unsafe) pour TOUT autre cas (lien, FIFO,
    dossier, lien dur, propriétaire autre que `owner` (défaut : compte courant), trop gros, JSON invalide) :
    fail-closed."""
    got = _read_at(dfd, name + HOLD_SUFFIX, HOLD_CAP, code="hold_unsafe",
                   owners=(os.geteuid() if owner is None else owner,))
    if got is None:
        return None
    doc = _parse(got[0])
    if not (isinstance(doc, dict) and doc.get("v") == 1 and doc.get("reason") == "rollback"
            and isinstance(doc.get("held_at"), (int, float)) and not isinstance(doc.get("held_at"), bool)):
        raise Refused("hold_unsafe", "invalid")
    return doc


def _hold_write(dfd, name, clock):
    doc = {"v": 1, "reason": "rollback", "held_at": round(clock(), 3)}
    try:
        fsutil.atomic_write(dfd, name + HOLD_SUFFIX, json.dumps(doc, sort_keys=True).encode("ascii"), mode=0o600)
    except OSError:
        raise Refused("write_failed", "hold")


def _resume_locked(dfd, name):
    hold = name + HOLD_SUFFIX
    st = _stat_at(dfd, hold)
    if st is None:
        return {"ok": True, "result": "not_held", "held": False}
    if stat.S_ISDIR(st.st_mode):
        raise Refused("hold_unsafe", "directory")   # jamais de suppression récursive : nettoyage manuel
    try:
        os.unlink(hold, dir_fd=dfd)                  # le NOM seul (lien, FIFO, lien dur : rien n'est suivi)
    except FileNotFoundError:
        pass
    except OSError:
        raise Refused("write_failed", "hold_remove")
    durable = True
    try:
        os.fsync(dfd)
    except OSError:
        durable = False
    return {"ok": True, "result": "resumed", "held": False, "durable": durable}


def _rollback_locked(cfg, dfd, d, name, engine, clock=time.time):
    """Retour à l'instantané `catalogue.json.prev` : mêmes validations (sauf baisse/recouvrement), même écriture.

    Le témoin de suspension est posé AVANT le remplacement (une coupure entre les deux ne laisse jamais la minuterie
    réécraser le catalogue restauré) ; rollback refusé et témoin créé par ce passage : témoin retiré (état antérieur)."""
    engine = _load_engine(cfg, engine)
    detail_re = engine.Config({}).detail_re
    got = _read_at(dfd, name + ".prev")
    if got is None:
        raise Refused("no_snapshot")
    old_bytes, old_st, _, base = _current(engine, detail_re, dfd, name)
    had_hold = _stat_at(dfd, name + HOLD_SUFFIX) is not None
    _hold_write(dfd, name, clock)
    try:
        out = _commit(engine, detail_re, dfd, d, name, got[0], old_bytes, old_st, None, cfg, snapshot=False)
    except BaseException:
        if not had_hold:
            try:
                os.unlink(name + HOLD_SUFFIX, dir_fd=dfd)
            except OSError:
                pass   # suppression impossible : la suspension reste (fail-closed), `resume` la lève
        raise
    out["previous_count"] = base["count"] if base else 0
    out["result"] = "rolled_back"
    out["held"] = True
    return out


def _commit(engine, detail_re, dfd, d, name, data, old_bytes, old_st, base, cfg, snapshot=True):
    """Candidat même dossier -> relu -> validé -> instantané -> os.replace -> fsync du dossier. Refused sinon ;
    le catalogue actuel n'est jamais modifié avant le renommage final et le candidat est toujours retiré."""
    if len(data) > MAX_BYTES:
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
                n = os.write(fd, view)
                view = view[n:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        except OSError:
            raise Refused("write_failed", "candidate_write")
        finally:
            os.close(fd)
        got = _read_at(dfd, cand)
        if got is None or got[0] != data:
            raise Refused("invalid_candidate", "readback")
        doc = _parse(got[0])
        if doc is None:
            raise Refused("invalid_candidate", "json")
        v = validate_doc(engine, detail_re, doc, os.path.join(d, cand), baseline=base,
                         min_items=cfg.catalogue_min_items, max_drop_pct=cfg.catalogue_max_drop_pct if base else None)
        if not _same_file(_stat_at(dfd, name), old_st):
            raise Refused("catalogue_changed")   # remplacé pendant la passe (copie manuelle) : le plus récent gagne
        if snapshot and old_bytes is not None and base is not None:
            try:
                fsutil.atomic_write(dfd, name + ".prev", old_bytes, mode=0o600)
            except OSError:
                raise Refused("write_failed", "snapshot")
        if not _same_file(_stat_at(dfd, name), old_st):
            raise Refused("catalogue_changed")   # revérifié juste avant le renommage (copie pendant l'instantané)
        try:
            os.replace(cand, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError:
            raise Refused("write_failed", "replace")
        durable = True
        try:
            os.fsync(dfd)
        except OSError:
            durable = False
        return {"ok": True, "result": "updated", "count": v["count"], "retained": v["retained"], "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "durable": durable}
    finally:
        try:
            os.unlink(cand, dir_fd=dfd)
        except OSError:
            pass


# --------------------------------------------------------------------------- état (lecture seule, aussi pour root)
def status(cfg):
    """Catalogue actuel (présence, nombre, taille, empreinte, date), instantané, suspension et dernier passage.
    Jamais d'hôte. `shape_ok` = forme seulement (objet, liste `items`, `count` cohérent) : la validation complète par
    le moteur n'a lieu qu'au rafraîchissement/rollback (la CLI root ne charge jamais le moteur)."""
    d, name = os.path.split(os.path.abspath(cfg.catalogue_file))
    try:
        dfd = fsutil.open_dir(d)
    except OSError:
        return {"ok": False, "error": "unreadable"}
    try:
        out = {"ok": True}
        for key, fname in (("catalogue", name), ("snapshot", name + ".prev")):
            try:
                got = _read_at(dfd, fname, any_owner=True)
            except Refused as e:
                out[key] = {"present": True, "shape_ok": False, "why": e.why}
                continue
            except OSError:
                out[key] = {"present": None, "shape_ok": False, "why": "unreadable"}
                continue
            if got is None:
                out[key] = {"present": False}
                continue
            doc = _parse(got[0])
            items = _doc_items(doc)
            info = {"present": True, "bytes": len(got[0]), "sha256": hashlib.sha256(got[0]).hexdigest(),
                    "shape_ok": items is not None and doc.get("count", len(items)) == len(items)}
            if items is not None:
                info["count"] = len(items)
                gen = doc.get("generated_at")
                info["generated_at"] = gen if isinstance(gen, str) and GEN_RE.match(gen) else None
            out[key] = info
        out["hold"] = _hold_view(dfd, name)
        out["refresh"] = _status_clean(_status_read(dfd, any_owner=True))
        return out
    finally:
        os.close(dfd)


def _hold_view(dfd, name):
    """Lecture seule (aussi pour root) : {held, safe, held_at?}. Un témoin dangereux est rapporté comme suspension."""
    try:
        doc = _hold_state(dfd, name, owner=os.fstat(dfd).st_uid)   # root : propriétaire = celui du dossier
    except Refused as e:
        return {"held": True, "safe": False, "why": e.why}
    except OSError:
        return {"held": True, "safe": False, "why": "unreadable"}
    if doc is None:
        return {"held": False}
    return {"held": True, "safe": True, "held_at": doc["held_at"]}


def last_finished(cfg):
    """Horodatage du dernier passage terminé (statut persisté), ou None — pour la CLI root (via systemd)."""
    st = status(cfg)
    r = st.get("refresh") or {}
    return r.get("finished_at")
