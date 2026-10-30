"""Helper root de la connexion Muse depuis la TV (opentv-muse-setup.service, lancé par opentv-muse-setup.path).

Seul pont entre le service OpenTV (compte opentv) et l'administration Muse existante (museadmin.Admin, root). Il ne sait
faire que quatre choses, à champs fixes, sans chemin, commande, argument ni option venant du job :
  - « pair »   (consent = "tv") : compare d'abord le jeton reçu au jeton local de la boîte, en temps constant, AVANT
                 toute écriture (différent ⇒ « different_token », rien changé : ni autorisation, ni pont, ni jeton) ;
                 puis autorise le pilotage de Muse (même écriture que `muse authorize-control --yes`, notée pour
                 retour arrière) et `muse pair --token-stdin` en mode keep_token (jeton local existant JAMAIS
                 remplacé ; JAMAIS --replace-token, --replace-identity ni --force : une identité muse-device
                 étrangère reste intacte et la connexion échoue honnêtement) ;
  - « cancel » (target = job « pair ») : ferme la fenêtre Bluetooth que CE helper a ouverte (interruption pendant
                 l'attente de l'app Muse : identité tout juste émise révoquée par `muse pair` lui-même) ; un Muse déjà
                 apparié n'est jamais révoqué par une annulation ;
  - « unpair » (confirm = "tv") : `muse unpair --forget-token` puis `muse revoke-control` ; refusé sans rien toucher
                 si l'identité muse-device est étrangère ;
  - « probe »  : aucune action ; republie l'état public de la boîte (box.json : owned/foreign, sans secret), comme
                 après toute activation utile.
Contrôles de chaque job (file /run/opentv-musejobs/queue sur tmpfs, ouverte sans suivre de lien, propriétaire opentv,
0700) : fichier régulier, un seul lien, propriétaire opentv, mode exactement 0600, ≤ 2 Kio, JSON strict à clés exactes
(schéma v2), âge borné selon l'horloge du démarrage (« at », CLOCK_BOOTTIME : un saut de l'heure murale n'expire ni ne
ranime aucun job). RÉCLAMATION = point de linéarisation avec le service : après lecture et contrôles, le nom exact est
retiré AVEC SUCCÈS avant toute analyse ou action. ENOENT ⇒ le service l'a retiré (annulation, échéance, redémarrage) :
rien exécuté, aucune issue ; nom désignant un autre fichier ou autre échec ⇒ rien exécuté (fail-closed). Tout résidu
(lien, FIFO, dossier, nom invalide) est retiré sans rien exécuter. Le jeton n'existe que dans ce processus, le temps de
le passer à `muse pair` par un flux mémoire.
Reprise après plantage : un journal root (/var/lib/opentv-muse-setup/inflight.json, 0600, jamais de jeton) est écrit
avant toute action ; l'INTENTION d'autoriser le pilotage et l'instantané exact d'avant (plafond « muse » + empreinte de
l'état lu, sur laquelle porte l'écriture compare-and-set) y sont écrits (fsync) AVANT l'écriture de l'autorisation.
Au démarrage suivant, une séance interrompue est nettoyée : fenêtre Bluetooth arrêtée, identité émise sans appariement
révoquée (la nôtre seulement), jeton local écrit par cette séance retiré (même inode, aucun appariement) ; autorisation :
plafond égal à l'instantané ⇒ jamais écrite (ou déjà rendue) ; égal au pilotage ⇒ rendue par compare-and-set (une
écriture concurrente fait échouer la CAS : rien écrasé) ; autre valeur ⇒ décision du propriétaire, gardée ; registre
illisible ⇒ rien écrit. Issue publiée VRAIE (jamais « rien n'a changé » sans preuve) : appariement présent ⇒
« paired_incomplete » (appariement, identité, jeton et pilotage gardés ; pont pas prouvé connecté : à reprendre par
« Reconnecter », sans Bluetooth, ou « Déconnecter Muse ») ; état d'avant entièrement rendu ⇒ « interrupted » ; sinon ⇒
« recovery_incomplete ». Une annulation en direct et un pont non démarré après un appariement réussi suivent la même
règle (identité jamais révoquée une fois l'appariement présent).
"""
import io
import json
import math
import os
import re
import stat
import sys
import time

from . import agents, fsutil, museadmin, musesetup

NAME_RE = re.compile(r"([a-z0-9]{16})\.json")
JOURNAL_DIR = "/var/lib/opentv-muse-setup"
JOURNAL = "inflight.json"
JOURNAL_V = 2                   # v2 : intention d'autorisation + instantané exact écrits AVANT l'écriture
AGE_SKEW_S = 1.0                # même horloge du démarrage, lue par deux processus (arrondi à la ms) : tolérance
NO_RESULT = ("gone", "claim_failed", "replaced")   # job non réclamé : l'issue appartient au service, jamais au helper
PAIR_PICKUP_MAX_S = 120.0
CANCEL_MAX_AGE_S = musesetup.PAIR_BUDGET_S + 60.0
UNPAIR_MAX_AGE_S = 120.0
MAX_JOBS = 16
PROBE_MAX_AGE_S = 120.0
KEYS = {"pair": {"v", "id", "action", "consent", "at", "token"},
        "cancel": {"v", "id", "action", "target", "at"},
        "unpair": {"v", "id", "action", "confirm", "at"},
        "probe": {"v", "id", "action", "at"}}
# État public de la boîte (aucun secret) publié pour la TV dans le dossier des issues : la TV propose
# « Déconnecter Muse » seulement quand la boîte possède un appariement, un jeton local ou SA propre identité
BOX_NAME = musesetup.BOX_NAME
OWNED_IDENTITY = ("owned", "outdated", "stale_client")
WINDOW_CODES = ("pair_timeout", "window_closed", "pair_window_not_opened")
UNAVAILABLE = ("sdk_unverified", "dependency_missing", "stock_musegadget_present", "muse_user_missing",
               "muse_user_is_root", "muse_user_group_not_private", "agent_group_missing", "muse_user_not_in_agent_group",
               "bluetooth_group_missing", "state_dir_missing", "state_dir_unsafe", "sdk_token_file_unsafe",
               "root_required", "muse_control_not_authorized", "clients_dir_unsafe", "token_input_required")
BRIDGE = ("bridge_start_failed", "bridge_refused", "bridge_not_started", "bridge_stop_timeout", "bridge_not_running")


def _stderr_log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    sys.stderr.write(json.dumps(rec, sort_keys=True) + "\n")


def outcome_code(res):
    """Rapport de museadmin.Admin.pair -> code public stable (aucun détail technique, aucun secret)."""
    if res.get("ok"):
        return "connected"
    err = res.get("error")
    if not isinstance(err, str):
        return "helper_error"
    if err == "invalid_sdk_token" or err == "no_sdk_token":
        return "invalid_sdk_token"
    if err == "sdk_token_mismatch":
        return "different_token"
    if err in WINDOW_CODES:
        return err
    if err in ("pair_unit_failed", "pair_unit_start_failed", "error"):
        return "pair_window_not_opened"
    if err == "interrupted":
        return "interrupted"
    if err == "muse_device_identity_unexpected":
        return "foreign_identity"
    if err.startswith("bluetooth"):
        return "bluetooth_not_ready"
    if err in ("registry_unreadable", "registry_busy", "registry_write_failed", "stale"):
        return "registry_unavailable"
    if err in UNAVAILABLE:
        return "muse_unavailable"
    if err in BRIDGE:
        return "bridge_not_started"
    return "helper_error"


class Cancelled(KeyboardInterrupt):
    """Annulation demandée par le propriétaire pendant l'attente de l'app Muse (gérée par Admin.pair : fenêtre
    fermée, identité tout juste émise révoquée)."""


class WizardAdmin(museadmin.Admin):
    """museadmin.Admin + progression (préparation → en attente de Muse → connexion) et point d'annulation, sans rien
    changer aux portes de `pair` (preflight, identité, pont)."""

    def __init__(self, cfg, progress=None, token_created=None, **kw):
        super().__init__(cfg, **kw)
        self.progress = progress or (lambda phase, **x: None)
        # jeton local écrit par CET appariement (aucun avant) : (périphérique, inode) noté au journal de reprise, pour
        # le retirer après un plantage exactement comme pair() le fait après un échec (jamais un autre fichier)
        self.token_created = token_created or (lambda dev, ino: None)
        self.pair_ctx = None
        self.waiting = False
        self.ble_name = None

    def say(self, text):   # seul message de pair : la fenêtre Bluetooth est ouverte
        self.progress("waiting_muse", ble_name=self.ble_name)

    def read_state_status(self, dfd):
        st = museadmin.Admin.read_state_status(dfd)
        if st and st.get("state") == "open" and st.get("ble_name"):
            self.ble_name = st["ble_name"]
        return st

    def _pair(self, token_stdin, replace_token, force, replace_identity, keep_token, ctx):
        self.pair_ctx = ctx
        return super()._pair(token_stdin, replace_token, force, replace_identity, keep_token, ctx)

    def wait_pair(self, dfd):
        created = (self.pair_ctx or {}).get("created")
        if created is not None:
            self.token_created(created[1], created[2])
        self.waiting = True
        try:
            return super().wait_pair(dfd)
        finally:
            self.waiting = False

    def start_bridge(self):
        self.progress("connecting")
        return super().start_bridge()


def _refuse_prompt(*_a, **_k):
    raise ValueError("token_input_required")


def _no_constant(_name):
    raise ValueError("non_finite")   # NaN / Infinity : jamais acceptés dans un job


def _scopes_list(x):
    return isinstance(x, list) and len(x) <= 16 and all(isinstance(s, str) and len(s) <= 32 for s in x)


class MuseHelper:
    def __init__(self, cfg, owner_uid, admin_factory=None, clock=time.time, sleep=time.sleep, log=_stderr_log,
                 chown=True, journal_dir=JOURNAL_DIR, journal_uid=0, boot=musesetup.boot_clock):
        self.cfg, self.owner_uid, self.clock, self.real_sleep, self.log = cfg, owner_uid, clock, sleep, log
        self.boot = boot   # âge des jobs : horloge du démarrage (jamais l'heure murale)
        self.chown, self.journal_dir, self.journal_uid = chown, journal_dir, journal_uid
        self.admin_factory = admin_factory or (lambda **kw: WizardAdmin(cfg, **kw))
        self.queue = []          # jobs lus pendant un appariement (traités ensuite)
        self.cancel_for = set()  # jobs « pair » dont l'annulation est demandée
        self.current = None      # job « pair » en cours
        self.admin = None
        self.probe_wanted = False   # état public de la boîte à republier en fin d'activation

    # -- dossier des jobs : descripteur sans lien suivi, propriétaire = service, aucun accès groupe/autres
    def _open_jobs(self):
        try:
            dfd = fsutil.open_dir(self.cfg.muse_jobs_dir)
        except OSError as e:
            self.log("muse_jobs_dir_unsafe", error=type(e).__name__)
            return None
        st = os.fstat(dfd)
        if st.st_uid != self.owner_uid or st.st_mode & 0o077:
            os.close(dfd)
            self.log("muse_jobs_dir_unsafe", error="owner_or_mode")
            return None
        return dfd

    def _discard(self, dfd, name, st):
        try:
            if stat.S_ISDIR(st.st_mode):
                os.rmdir(name, dir_fd=dfd)   # vide seulement ; jamais de suppression récursive ici
            else:
                os.unlink(name, dir_fd=dfd)  # le lien lui-même, jamais sa cible
            self.log("muse_job_junk", removed=True)
        except OSError as e:
            self.log("muse_job_junk", removed=False, error=type(e).__name__)

    def _claim(self, dfd, name, st):
        """Réclamation = point de linéarisation avec le service (musesetup.withdraw_job / purge_orphans retirent ce
        même nom) : retrait RÉUSSI du nom exact, relatif au dossier. -> "claimed" | "gone" (ENOENT : le service a
        gagné, rien ne doit être exécuté) | "replaced" (le nom désigne un autre fichier que celui lu : laissé, rien
        exécuté) | "claim_failed" (tout autre échec : fail-closed, rien exécuté)."""
        if st is not None:
            try:
                cur = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                return "gone"
            except OSError:
                return "claim_failed"
            if (cur.st_dev, cur.st_ino) != (st.st_dev, st.st_ino):
                return "replaced"
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "claim_failed"
        return "claimed"

    def _take(self, dfd, name):
        """-> (dict | None, erreur | None). Ordre strict : fichier ouvert sans lien suivi et contrôlé (fstat), contenu
        lu (borné), PUIS réclamé (_claim : retrait réussi du nom exact), PUIS seulement analysé ; aucune action n'est
        possible sur un job non réclamé (« gone » / « replaced » / « claim_failed » : rien exécuté, aucune issue)."""
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None, "gone"
        if not stat.S_ISREG(st.st_mode):
            self._discard(dfd, name, st)
            return None, "bad_type"
        raw, err, opened = None, None, None
        try:
            fd = os.open(name, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC, dir_fd=dfd)
        except FileNotFoundError:
            return None, "gone"
        except OSError:
            err = "unreadable"
        else:
            try:
                f = opened = os.fstat(fd)
                if not stat.S_ISREG(f.st_mode):
                    err = "bad_type"
                elif f.st_nlink != 1:
                    err = "hardlinked"
                elif f.st_uid != self.owner_uid:
                    err = "bad_owner"
                elif stat.S_IMODE(f.st_mode) != 0o600:
                    err = "bad_mode"
                elif f.st_size > musesetup.JOB_MAX_BYTES:
                    err = "too_big"
                else:
                    raw = os.read(fd, musesetup.JOB_MAX_BYTES + 1)
                    if len(raw) > musesetup.JOB_MAX_BYTES:
                        raw, err = None, "too_big"
            finally:
                os.close(fd)
        claim = self._claim(dfd, name, opened)
        if claim != "claimed":
            raw = None
            return None, claim
        if err:
            return None, err
        try:
            job = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, "bad_json"
        finally:
            raw = None
        return job, None

    def _check(self, job, jid):
        """Schéma strict (clés exactes par action, âge borné). -> job validé ; ValueError(code) sinon."""
        if not isinstance(job, dict) or job.get("v") != musesetup.JOB_V or job.get("id") != jid:
            raise ValueError("bad_job")          # ancien schéma (v1, heure murale) compris
        action = job.get("action")
        if action not in KEYS or set(job) != KEYS[action]:
            raise ValueError("bad_fields")
        try:
            age = musesetup.job_age(job, self.boot())
        except ValueError:
            raise ValueError("bad_job") from None
        limit = {"pair": PAIR_PICKUP_MAX_S, "cancel": CANCEL_MAX_AGE_S, "unpair": UNPAIR_MAX_AGE_S,
                 "probe": PROBE_MAX_AGE_S}[action]
        if not math.isfinite(age) or not -AGE_SKEW_S <= age <= limit:
            raise ValueError("expired")
        if action == "pair":
            if job["consent"] != "tv":
                raise ValueError("no_consent")
            if not isinstance(job["token"], str) or not musesetup.SDK_TOKEN_RE.fullmatch(job["token"]):
                raise ValueError("bad_token")
        elif action == "cancel":
            if not isinstance(job["target"], str) or not musesetup.JOB_ID_RE.fullmatch(job["target"]):
                raise ValueError("bad_fields")
        elif action == "unpair" and job["confirm"] != "tv":
            raise ValueError("no_consent")
        return job

    def poll(self, cap=MAX_JOBS):
        """Lit et consomme les jobs présents ; annulations ciblées notées tout de suite, autres jobs mis en file.
        Tout job invalide est retiré, jamais exécuté (et un « pair » invalide reçoit une issue « refused »)."""
        dfd = self._open_jobs()
        if dfd is None:
            return
        try:
            try:
                names = sorted(os.listdir(dfd))[:4096]
            except OSError:
                return
            n = 0
            for name in names:
                m = NAME_RE.fullmatch(name)
                if m is None:
                    try:
                        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    except OSError:
                        continue
                    self._discard(dfd, name, st)
                    continue
                if n >= cap:
                    break
                n += 1
                jid = m.group(1)
                job, err = self._take(dfd, name)
                if err is None:
                    try:
                        job = self._check(job, jid)
                    except ValueError as e:
                        err = str(e)
                if err is not None:
                    job = None
                    self.log("muse_job_refused", id=jid, error=err)
                    if err not in NO_RESULT:   # job réclamé puis refusé : issue « refused » (rien exécuté)
                        self._result(jid, "done", ok=False, result="refused")
                    continue
                if job["action"] == "cancel":
                    self.cancel_for.add(job["target"])
                    self.log("muse_job", id=jid, action="cancel")
                    self._result(jid, "done", ok=True, result="cancelled")
                elif job["action"] == "probe":   # relecture de l'état public de la boîte (aucune action)
                    self.probe_wanted = True
                    self.log("muse_job", id=jid, action="probe")
                else:
                    self.queue.append(job)
        finally:
            os.close(dfd)

    # -- issues pour le service (son dossier, propriété opentv : descripteur, temporaire exclusif, rename)
    def _result(self, jid, phase, ok=None, result=None, **extra):
        body = {"v": 1, "id": jid, "phase": phase}
        if phase == "done":
            body.update(ok=bool(ok), result=result)
        for k, v in extra.items():
            if v is not None:
                body[k] = v
        try:
            dfd = fsutil.open_dir(self.cfg.muse_results_dir)
            try:
                st = os.fstat(dfd)
                if st.st_uid != self.owner_uid or st.st_mode & 0o077:
                    raise fsutil.UnsafePath("results_dir_unsafe")
                fsutil.atomic_write(dfd, jid + ".json", json.dumps(body, sort_keys=True), mode=0o600,
                                    uid=self.owner_uid if self.chown else None)
            finally:
                os.close(dfd)
        except OSError as e:
            self.log("muse_result_error", id=jid, error=type(e).__name__)

    # -- journal root de la séance en cours (reprise après plantage)
    def _journal_dir(self):
        dfd = fsutil.open_dir(self.journal_dir)
        st = os.fstat(dfd)
        if st.st_uid != self.journal_uid or st.st_mode & 0o077:
            os.close(dfd)
            raise fsutil.UnsafePath("journal_dir_unsafe")
        return dfd

    def _journal_write(self, data):
        dfd = self._journal_dir()
        try:
            fsutil.atomic_write(dfd, JOURNAL, json.dumps(data, sort_keys=True), mode=0o600)
        finally:
            os.close(dfd)

    def _journal_read(self):
        try:
            dfd = self._journal_dir()
        except OSError:
            return None, "journal_dir_unsafe"
        try:
            try:
                fd = os.open(JOURNAL, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC,
                             dir_fd=dfd)
            except FileNotFoundError:
                return None, None
            except OSError:
                return {"v": 1, "id": None, "changed": False}, "journal_unsafe"
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != self.journal_uid:
                    return {"v": 1, "id": None, "changed": False}, "journal_unsafe"
                d = json.loads(os.read(fd, 4096).decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                return {"v": 1, "id": None, "changed": False}, "journal_unsafe"
            finally:
                os.close(fd)
            if not isinstance(d, dict) or d.get("v") not in (1, JOURNAL_V):
                return {"v": 1, "id": None, "changed": False}, "journal_unsafe"
            return d, None
        finally:
            os.close(dfd)

    def _journal_clear(self):
        try:
            dfd = self._journal_dir()
        except OSError:
            return
        try:
            os.unlink(JOURNAL, dir_fd=dfd)
        except FileNotFoundError:
            pass
        finally:
            os.close(dfd)

    # -- autorisation du pilotage : intention journalisée AVANT l'écriture, retour arrière compare-and-set
    def _settle_grant(self, adm, before):
        """Ligne « muse » après une écriture d'autorisation dont l'intention (instantané `before`) est journalisée.
        -> "unchanged" : vaut `before` (écriture jamais faite, ou déjà rendue) — rien écrit ;
           "restored"  : valait exactement le pilotage (ce que la séance écrit) — rendue à `before` par une écriture
                         compare-and-set sur l'état relu (une écriture concurrente ⇒ CAS perdue ⇒ "kept") ;
           "kept"      : autre valeur (décision du propriétaire) ou CAS perdue — jamais écrasée ;
           "unreadable": registre illisible/partiel (ou intention illisible) — rien écrit ;
           "error"     : écriture impossible (verrou occupé, disque)."""
        from .app import AGENT_LEVELS, KNOWN_AGENTS
        if not _scopes_list(before):
            return "unreadable"
        try:
            reg = adm.registry()
            view, fp, corrupt = reg.snapshot(KNOWN_AGENTS)
            if corrupt:
                return "unreadable"
            cur = sorted(view["muse"]["allowed"])
            if cur == sorted(before):
                return "unchanged"
            if cur != agents.with_optional(AGENT_LEVELS["control"], before):   # exactement ce que la séance écrit
                return "kept"
            reg.apply_policy_changes({"muse": sorted(before)}, KNOWN_AGENTS, fp, **adm.service_ids())
            return "restored"
        except ValueError as e:
            if str(e) == "stale":
                return "kept"
            self.log("muse_grant_rollback_error", error="ValueError")
            return "unreadable"
        except (OSError, KeyError, TypeError) as e:
            self.log("muse_grant_rollback_error", error=type(e).__name__)
            return "error"

    def _grant_unknown(self, adm):
        """Journal sans intention exploitable (ancienne version : « changed » écrit APRÈS l'autorisation ; journal
        illisible) : une autorisation a pu être écrite sans trace. -> "unknown" si le pilotage est présent (jamais
        « rien n'a changé »), "unchanged" sinon, "unreadable" si le registre l'est."""
        from .app import KNOWN_AGENTS
        try:
            view, _fp, corrupt = adm.registry().snapshot(KNOWN_AGENTS)
        except (OSError, ValueError, KeyError, TypeError):
            return "unreadable"
        if corrupt:
            return "unreadable"
        return "unknown" if "control" in view["muse"]["allowed"] else "unchanged"

    @staticmethod
    def _journal_grant(j):
        """-> (intention {"before": [...]} | None, indécidable: bool)."""
        if j.get("v") == JOURNAL_V:
            g = j.get("grant")
            if g is None:
                return None, False
            if isinstance(g, dict) and _scopes_list(g.get("before")):
                return {"before": list(g["before"])}, False
            return None, True
        if j.get("changed") is True and _scopes_list(j.get("before")):   # v1 : écriture faite et notée
            return {"before": list(j["before"])}, False
        return None, True                                                 # v1 « changed » faux : trou possible

    def _journal_intent(self, jid, plan):
        """Intention d'autoriser + instantané exact (plafond « muse » lu et empreinte de l'état sur laquelle porte la
        CAS), ajoutés au journal de la séance et rendus durables (fsync) AVANT l'écriture de l'autorisation. Tout
        échec lève : l'autorisation n'est alors PAS écrite (« journal_failed »)."""
        j, problem = self._journal_read()
        if problem is not None or not isinstance(j, dict) or j.get("id") != jid:
            raise OSError("journal_unavailable")
        gi = {"before": sorted(plan["before"]), "fp": str(plan["fp"])}
        self._journal_write(dict(j, grant=gi, at=round(self.clock(), 3)))
        return gi

    @staticmethod
    def _state_entries(adm, pw):
        """-> {"pairing": absent|present|unsafe|None, "sdk_token": …} (présences seulement, jamais lues ; None =
        dossier d'état illisible)."""
        out = {"pairing": None, "sdk_token": None}
        if pw is None:
            return out
        try:
            dfd = adm.open_state_dir(pw)
        except OSError:
            return out
        try:
            out["pairing"] = adm.entry(dfd, "pairing.json")
            out["sdk_token"] = adm.entry(dfd, "sdk_token")
        finally:
            os.close(dfd)
        return out

    @staticmethod
    def _identity_undone(rep):
        """Rapport de discard_identity : révocation et retrait du fichier client effectivement terminés."""
        return isinstance(rep, dict) and rep.get("agent_revoked") is not None and \
            rep.get("client_file_removed") != "clients_dir_unsafe"

    def _journal_token(self, jid, dev, ino):
        """Jeton local écrit par CET appariement : (périphérique, inode) noté au journal (aucun secret)."""
        try:
            j, problem = self._journal_read()
            if problem is None and isinstance(j, dict) and j.get("id") == jid:
                self._journal_write(dict(j, token_created=[int(dev), int(ino)]))
        except (OSError, ValueError, TypeError) as e:
            self.log("muse_journal_error", error=type(e).__name__)

    def recover(self):
        """Séance interrompue (plantage, coupure) : nettoyage borné avant tout nouveau job, puis issue VRAIE.
        Appariement présent ⇒ « paired_incomplete » : appariement, identité, jeton local et pilotage gardés, pont non
        prouvé connecté (jamais « annulé » ni « rien n'a changé ») ; état d'avant entièrement rendu (identité révoquée,
        jeton écrit par la séance retiré, autorisation rendue) ⇒ « interrupted » ; sinon ⇒ « recovery_incomplete »."""
        j, problem = self._journal_read()
        if j is None and problem is None:
            return None
        if problem == "journal_dir_unsafe":
            self.log("muse_recover", state="journal_dir_unsafe")
            return "unsafe"
        adm = self.admin_factory(sleep=self.real_sleep, prompt=_refuse_prompt, stdin=io.StringIO(""),
                                 err=io.StringIO())
        adm.systemctl("stop", museadmin.PAIR_UNIT)
        report = {"identity": None, "grant": None, "token": None}
        gi, undecidable = self._journal_grant(j)
        pw, _why = adm.muse_account()
        ent = self._state_entries(adm, pw)
        paired = None if ent["pairing"] is None else ent["pairing"] == "present"
        if paired is False:   # identité émise par une séance sans appariement : la nôtre seulement est révoquée
            report["identity"] = adm.discard_identity(pw)
            created = j.get("token_created")
            if j.get("token_before") == "absent" and isinstance(created, list) and len(created) == 2 and \
                    all(isinstance(x, int) and not isinstance(x, bool) for x in created):
                # jeton écrit par cette séance (aucun avant) : retiré s'il s'agit encore du même fichier
                report["token"] = adm._rollback_created_token((pw, created[0], created[1]))
        if paired is not True:   # apparié : pilotage gardé (« paired_incomplete »)
            if gi is not None:
                report["grant"] = self._settle_grant(adm, gi["before"])
            elif undecidable:
                report["grant"] = self._grant_unknown(adm)
        if paired is True:
            code = "paired_incomplete"
        else:
            after = self._state_entries(adm, pw)
            token_ok = j.get("token_before") == "same" or (j.get("token_before") == "absent" and
                                                           after["sdk_token"] == "absent")
            undone = paired is False and ent["pairing"] == "absent" and after["pairing"] == "absent" and \
                self._identity_undone(report["identity"]) and token_ok and \
                report["grant"] in (None, "unchanged", "restored")
            code = "interrupted" if undone else "recovery_incomplete"
        jid = j.get("id")
        if isinstance(jid, str) and musesetup.JOB_ID_RE.fullmatch(jid):
            self._result(jid, "done", ok=False, result=code)
        self._journal_clear()
        self.log("muse_recover", state="done", paired=paired, grant=report["grant"], result=code,
                 identity_revoked=(report["identity"] or {}).get("agent_revoked"), token_removed=report["token"])
        report["result"] = code
        return report

    # -- actions
    def _sleep(self, s):
        """Sommeil de museadmin : pendant l'attente de l'app Muse, une annulation du propriétaire l'interrompt."""
        if self.admin is not None and self.admin.waiting:
            self.poll()
            if self.current in self.cancel_for:
                raise Cancelled()
        self.real_sleep(s)

    def do_pair(self, job):
        jid = job["id"]
        self.current = jid
        self.probe_wanted = True
        tok = job.pop("token")
        # Jeton local déjà présent : comparé EN TEMPS CONSTANT avant toute autorisation, tout arrêt du pont, toute
        # écriture. Différent ⇒ refus, rien changé (jeton, identité, pont, politique) ; identique ⇒ réutilisé tel quel.
        pre = self.admin_factory(sleep=self.real_sleep, prompt=_refuse_prompt, stdin=io.StringIO(""),
                                 err=io.StringIO())
        match = pre.local_token_match(tok)
        if match in ("different", "unsafe"):
            tok = None
            code = "different_token" if match == "different" else "muse_unavailable"
            self.current = None
            self.log("muse_job", id=jid, action="pair", ok=False, result=code)
            self._result(jid, "done", ok=False, result=code)
            return code
        stdin = io.StringIO(tok + "\n")   # seule copie restante : flux mémoire lu une fois par pair
        tok = None
        paired_before = self._paired_now(pre)
        # présence d'un jeton local AVANT la séance (absent | same ; aucun secret) : la reprise ne dira « rien n'a
        # changé » qu'après avoir retiré le jeton que la séance a écrit
        jbase = {"v": JOURNAL_V, "id": jid, "grant": None,
                 "token_before": match if match in ("absent", "same") else None}
        try:
            self._journal_write(dict(jbase, at=round(self.clock(), 3)))
        except OSError as e:
            stdin.close()
            self.log("muse_job", id=jid, action="pair", ok=False, error="journal_" + type(e).__name__)
            self._result(jid, "done", ok=False, result="helper_error")
            return "helper_error"
        adm = self.admin_factory(sleep=self._sleep, prompt=_refuse_prompt, stdin=stdin, err=io.StringIO(),
                                 progress=lambda phase, **x: self._result(jid, phase, **x),
                                 token_created=lambda dev, ino: self._journal_token(jid, dev, ino))
        self.admin = adm
        gstate = {"intent": None}

        def intent(plan):   # appelé par authorize_control entre sa lecture et son écriture compare-and-set
            gstate["intent"] = self._journal_intent(jid, plan)
        code, registered, res, called = "helper_error", None, None, False
        try:
            self._result(jid, "preparing")
            grant, gcode = adm.authorize_control(yes=True, intent=intent)
            if gcode != 0 or not grant.get("ok"):
                err = str(grant.get("error", ""))
                code = "registry_unavailable" if err.startswith("registry") or err == "stale" else \
                    "helper_error" if err == "journal_failed" else "muse_unavailable"
            else:
                self.poll()
                if jid in self.cancel_for:
                    code = "interrupted"
                else:
                    # JAMAIS --replace-token, --force ni --replace-identity : un jeton local existant est gardé
                    called = True
                    res, _rc = adm.pair(token_stdin=True, replace_token=False, force=False, replace_identity=False,
                                        keep_token=True)
                    code = outcome_code(res)
                    registered = res.get("registered") if isinstance(res.get("registered"), bool) else None
        except Cancelled:
            code = "interrupted"
        finally:
            stdin.close()
            self.admin = None
        ok = code == "connected"
        if not ok:
            now = self._paired_now(adm)
            if called and now is True and (paired_before is not True or code == "bridge_not_started"):
                # appariement présent (fait par cette séance, ou pont non démarré après lui) : jamais révoqué ni
                # « annulé » ; identité, jeton et pilotage gardés — « Reconnecter » reprend sans Bluetooth
                code = "paired_incomplete"
            elif code == "interrupted" and paired_before is not True and now is not False:
                # annulation lue alors que l'iPhone venait d'apparier (ou état illisible) : jamais « annulé »
                code = "paired_incomplete" if now else "recovery_incomplete"
        gi, rolled = gstate["intent"], None
        if not ok and code != "paired_incomplete" and gi is not None:
            # autorisation tentée (intention journalisée) : rendue par compare-and-set, jamais écrasée
            rolled = self._settle_grant(adm, gi["before"])
            if rolled in ("unreadable", "error"):
                code = "recovery_incomplete"     # pilotage peut-être encore accordé : jamais « rien n'a changé »
        if code == "interrupted" and not self._undone_live(res, gi, rolled, called):
            code = "recovery_incomplete"
        self._journal_clear()
        self.current = None
        self.log("muse_job", id=jid, action="pair", ok=ok, result=code)
        self._result(jid, "done", ok=ok, result="cancelled" if code == "interrupted" else code, registered=registered)
        return code

    def _paired_now(self, adm):
        """Appariement présent maintenant ? True | False | None (dossier d'état illisible, entrée anormale)."""
        try:
            pw, _why = adm.muse_account()
        except (AttributeError, OSError):
            return None
        p = self._state_entries(adm, pw)["pairing"]
        return None if p not in ("absent", "present") else p == "present"

    def _undone_live(self, res, gi, rolled, called):
        """Annulation en direct : état d'avant rendu ? (identité tout juste émise révoquée, jeton écrit retiré,
        autorisation rendue ou jamais écrite). Annulée avant `muse pair` : seule l'autorisation compte."""
        if gi is not None and rolled not in ("unchanged", "restored"):
            return False
        if not called:
            return True
        if not isinstance(res, dict):   # `muse pair` interrompu sans rapport : rien de prouvé
            return False
        if "identity" in res and not self._identity_undone(res["identity"]):
            return False
        return res.get("sdk_token_rolled_back") is not False

    def do_unpair(self, job):
        jid = job["id"]
        self.probe_wanted = True
        adm = self.admin_factory(sleep=self.real_sleep, prompt=_refuse_prompt, stdin=io.StringIO(""), err=io.StringIO())
        if adm.ownership().get("foreign"):
            # identité « muse-device » qui ne vient pas de cette boîte : jamais touchée, ni rien d'autre
            self.log("muse_job", id=jid, action="unpair", ok=False, result="foreign_identity")
            self._result(jid, "done", ok=False, result="foreign_identity")
            return False
        res, rc = adm.unpair(forget_token=True)
        rv, rvc = adm.revoke_control()
        ok = rc == 0 and rvc == 0
        self.log("muse_job", id=jid, action="unpair", ok=ok, kept=res.get("agent_kept"))
        self._result(jid, "done", ok=ok, result="disconnected" if ok else "disconnect_failed")
        return ok

    def publish_box(self):
        """État public de la boîte (aucun secret, aucune empreinte) pour la TV : {owned, foreign, paired, token,
        identity, identity_present}. owned = appariement, jeton local ou identité émise par muse pair, et aucune
        identité étrangère ; None = indéterminé (registre illisible…) ⇒ la TV ne propose rien (fail-closed)."""
        adm = self.admin_factory(sleep=self.real_sleep, prompt=_refuse_prompt, stdin=io.StringIO(""),
                                 err=io.StringIO())
        o = adm.ownership()
        if o.get("foreign"):
            owned = False
        elif o.get("identity") not in OWNED_IDENTITY + ("absent",):
            owned = None
        else:
            owned = bool(o.get("paired") or o.get("token") or o.get("identity") in OWNED_IDENTITY)
        body = {"v": 1, "owned": owned, "foreign": bool(o.get("foreign")),
                "paired": o.get("paired") if isinstance(o.get("paired"), bool) else None,
                "token": o.get("token") if isinstance(o.get("token"), bool) else None,
                "identity_present": o.get("identity_present") if isinstance(o.get("identity_present"), bool) else None,
                "at": round(self.clock(), 3)}
        try:
            dfd = fsutil.open_dir(self.cfg.muse_results_dir)
            try:
                st = os.fstat(dfd)
                if st.st_uid != self.owner_uid or st.st_mode & 0o077:
                    raise fsutil.UnsafePath("results_dir_unsafe")
                fsutil.atomic_write(dfd, BOX_NAME, json.dumps(body, sort_keys=True), mode=0o600,
                                    uid=self.owner_uid if self.chown else None)
            finally:
                os.close(dfd)
        except OSError as e:
            self.log("muse_box_error", error=type(e).__name__)
            return None
        self.log("muse_box", owned=owned, foreign=body["foreign"])
        return body

    def run(self, max_rounds=8):
        """Une activation : reprise éventuelle, puis jobs jusqu'à épuisement (borné)."""
        if self.recover() is not None:
            self.probe_wanted = True
        done = []
        for _ in range(max_rounds):
            self.poll()
            if not self.queue:
                break
            while self.queue:
                job = self.queue.pop(0)
                if job["action"] == "pair":
                    if job["id"] in self.cancel_for:   # annulé avant toute action : aucune autorisation, aucun pair
                        job.pop("token", None)
                        self._result(job["id"], "done", ok=False, result="cancelled")
                        done.append("cancelled")
                        continue
                    done.append(self.do_pair(job))
                else:
                    done.append("disconnected" if self.do_unpair(job) else "disconnect_failed")
        self.queue = []
        if self.probe_wanted:   # après chaque activation utile : la TV voit la vérité de la boîte
            self.probe_wanted = False
            self.publish_box()
        return done


def main(cfg, owner_uid=None, chown=True):
    if owner_uid is None:
        if os.geteuid() != 0:
            sys.stdout.write(json.dumps({"ok": False, "error": "root_required"}) + "\n")
            return 2
        import pwd
        try:
            owner_uid = pwd.getpwnam(cfg.service_user).pw_uid
        except KeyError:
            sys.stdout.write(json.dumps({"ok": False, "error": "service_user_missing"}) + "\n")
            return 2
    h = MuseHelper(cfg, owner_uid, chown=chown)
    out = h.run()
    sys.stdout.write(json.dumps({"ok": True, "processed": len(out)}) + "\n")
    return 0
