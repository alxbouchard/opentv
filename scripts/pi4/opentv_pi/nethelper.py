"""Helper réseau privilégié (root, lancé par opentv-net-helper.path). Strict et testable.

Pour chaque job : lstat (pas de lien), ouverture O_NOFOLLOW, propriétaire = utilisateur du service, mode 0600,
taille bornée, **suppression avant toute action** (consommation unique), puis validation (action allowlist,
âge, champs). Les secrets ne vont que dans le keyfile NetworkManager 0600 ; nmcli reçoit des argv fixes, sans
shell. Le résultat (sans SSID ni mot de passe) est déposé pour le service.

Budget : une exécution entière (tous les jobs) tient dans RUN_BUDGET_S, nettement sous TimeoutStartSec=180 de
opentv-net-helper.service. Chaque nmcli reçoit un `--wait` court et un délai d'exécution tronqué au budget restant
(groupe de processus tué à l'échéance) ; un job qui ne peut plus tenir dans le budget reste dans le dossier pour
l'activation suivante (ou échoue proprement, sans secret, s'il était déjà pris).
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid as _uuid

from . import fsutil, netjobs, nmkeyfile

NAME_RE = re.compile(r"([a-z0-9]{8,32})\.json")
KILL_GRACE_S = 1.0       # après SIGKILL du groupe : attente maximale de la fin de lecture
JUNK_SCAN_MAX = 4096     # éléments examinés par passage (les résidus sont retirés, jamais exécutés)
MIN_JOB_BUDGET_S = 15.0  # en dessous, un job n'est pas pris (il reste pour l'activation suivante)
MAX_AGE_S = 300.0
# -- budget d'une activation (opentv-net-helper.service : TimeoutStartSec=180, démarrage Python compris)
UNIT_TIMEOUT_START_S = 180
RUN_BUDGET_S = 120.0          # toutes les actions d'une exécution ; marge >= 60 s sur TimeoutStartSec
NM_GRACE_S = 5                # délai d'exécution = --wait + marge : nmcli échoue proprement avant d'être tué
NM_CMD_TIMEOUT_S = 10         # load / delete (sans attente d'activation)
AP_DOWN_WAIT_S = 10
WIFI_UP_WAIT_S = 35
AP_UP_WAIT_S = 20
OLD_UP_WAIT_S = 20            # retour arrière : réactivation de l'ancien opentv-wifi
# Retour arrière d'un wifi-apply (suppression du candidat puis hotspot OU ancien profil) : réservé AVANT l'essai du
# candidat, dont le délai est tronqué d'autant ; le retour arrière n'est donc jamais privé de budget.
ROLLBACK_RESERVE_S = NM_CMD_TIMEOUT_S + max(NM_CMD_TIMEOUT_S + AP_UP_WAIT_S + NM_GRACE_S,
                                            OLD_UP_WAIT_S + NM_GRACE_S) + 3 * 2 * KILL_GRACE_S
# pire cas réel d'un wifi-apply (délai ≤ 10 s + 7 nmcli à leur échéance + 2 × KILL_GRACE_S chacun) : AP down, load et
# up du candidat, puis échec : delete du candidat + (load + up du hotspot | up de l'ancien profil)
WIFI_APPLY_WORST_S = 10 + (AP_DOWN_WAIT_S + NM_GRACE_S) + NM_CMD_TIMEOUT_S + (WIFI_UP_WAIT_S + NM_GRACE_S) + \
    NM_CMD_TIMEOUT_S + max(NM_CMD_TIMEOUT_S + AP_UP_WAIT_S + NM_GRACE_S, OLD_UP_WAIT_S + NM_GRACE_S) + \
    7 * 2 * KILL_GRACE_S
# un wifi-apply plus vieux que ceci n'est plus exécuté : il finirait après l'échéance du service (netjobs)
WIFI_APPLY_PICKUP_MAX_S = 60.0
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def default_runner(argv, timeout):
    """Exécute argv (liste) sans shell, sortie bornée, environnement minimal. -> (code, sortie).

    Durée bornée par timeout + KILL_GRACE_S : à l'échéance, tout le groupe de processus est tué (un petit-enfant qui
    garderait la sortie ouverte ne peut pas bloquer la lecture, donc ni la boucle du service ni son watchdog)."""
    try:
        p = subprocess.Popen(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=SAFE_ENV, start_new_session=True)
    except OSError:
        return 127, ""
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        try:
            p.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        finally:
            if p.stdout is not None:
                p.stdout.close()
        try:
            p.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        return 124, ""
    return p.returncode, out.decode("utf-8", "replace")[:4000]


def _kind(mode):
    for test, name in ((stat.S_ISDIR, "dir"), (stat.S_ISLNK, "link"), (stat.S_ISREG, "file"), (stat.S_ISFIFO, "fifo"),
                       (stat.S_ISSOCK, "socket")):
        if test(mode):
            return name
    return "other"


def _stderr_log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    sys.stderr.write(json.dumps(rec, sort_keys=True) + "\n")


class Budget:
    """Échéance absolue d'une exécution du helper (horloge monotone)."""

    def __init__(self, seconds, mono=time.monotonic):
        self.mono = mono
        self.end = mono() + seconds

    def remaining(self):
        return max(0.0, self.end - self.mono())


class Helper:
    def __init__(self, jobs_dir, results_dir, nm_dir, owner_uid, runner=default_runner, clock=time.time,
                 sleep=time.sleep, log=_stderr_log, chown=True, ifname="wlan0", max_age=MAX_AGE_S,
                 mono=time.monotonic, budget_s=RUN_BUDGET_S, min_job_s=MIN_JOB_BUDGET_S):
        self.jobs_dir, self.results_dir, self.nm_dir = jobs_dir, results_dir, nm_dir
        self.owner_uid, self.runner, self.clock, self.sleep, self.log = owner_uid, runner, clock, sleep, log
        self.chown, self.ifname, self.max_age = chown, ifname, max_age
        self.mono, self.budget_s, self.min_job_s = mono, budget_s, min_job_s
        self.budget = Budget(budget_s, mono)
        self._dfd = None   # descripteur du dossier des jobs pendant run_once

    # -- lecture et consommation
    def run_once(self, cap=16):
        """Traite au plus `cap` jobs. Tout autre élément (nom caché ou invalide, dossier, lien, fifo…) est retiré sans
        rien exécuter : le dossier surveillé par DirectoryNotEmpty= ne reste jamais occupé par un résidu, donc
        opentv-net-helper.path ne boucle pas jusqu'à sa limite de déclenchement.

        Le dossier des jobs appartient au compte du service, qui pourrait le remplacer par un lien (vers les profils
        NetworkManager, par exemple) : il est ouvert une fois sans suivre de lien (O_DIRECTORY|O_NOFOLLOW) et toute
        lecture/suppression se fait relativement à ce descripteur."""
        try:
            dfd = fsutil.open_dir(self.jobs_dir)
        except OSError as e:
            self.log("net_jobs_dir_unsafe", error=type(e).__name__)
            return []
        try:
            if os.fstat(dfd).st_mode & 0o022:
                self.log("net_jobs_dir_unsafe", error="writable_by_others")
                return []
            self._dfd = dfd
            try:
                names = sorted(os.listdir(dfd))[:JUNK_SCAN_MAX]
            except OSError:
                return []
            jobs = []
            for n in names:
                if NAME_RE.fullmatch(n) and self._is_regular(n):
                    jobs.append(n)
                else:
                    self._discard(n)
            self.budget = Budget(self.budget_s, self.mono)
            out = []
            for n in jobs[:cap]:
                if self.budget.remaining() < self.min_job_s:   # laissé en place : activation suivante (path unit)
                    self.log("net_job_deferred", reason="budget")
                    break
                out.append(self.process(n))
            return out
        finally:
            self._dfd = None
            os.close(dfd)

    def _lstat(self, name):
        return os.stat(name, dir_fd=self._dfd, follow_symlinks=False)

    def _is_regular(self, name):
        try:
            return stat.S_ISREG(self._lstat(name).st_mode)
        except OSError:
            return False

    def _discard(self, name):
        """Supprime un résidu du dossier des jobs sans jamais suivre de lien (fail-closed : aucune action réseau)."""
        try:
            st = self._lstat(name)
        except FileNotFoundError:
            return
        try:
            if stat.S_ISDIR(st.st_mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise OSError("rmtree_unsafe")
                shutil.rmtree(name, dir_fd=self._dfd)
            else:
                os.unlink(name, dir_fd=self._dfd)   # fichier, lien (le lien lui-même), fifo, socket…
            self.log("net_job_junk", kind=_kind(st.st_mode), removed=True)
        except OSError as e:
            self.log("net_job_junk", kind=_kind(st.st_mode), removed=False, error=type(e).__name__)

    def _take(self, name):
        """-> (octets | None, erreur | None). Le fichier est toujours retiré du dossier des jobs."""
        try:
            st = self._lstat(name)
        except FileNotFoundError:
            return None, "gone"
        if not stat.S_ISREG(st.st_mode):
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=self._dfd)   # le lien lui-même, jamais sa cible
            return None, "bad_type"
        raw, err = None, None
        try:
            fd = os.open(name, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC,
                         dir_fd=self._dfd)
        except OSError:
            err = "unreadable"
        else:
            try:
                fst = os.fstat(fd)
                if not stat.S_ISREG(fst.st_mode):
                    err = "bad_type"
                elif fst.st_uid != self.owner_uid:
                    err = "bad_owner"
                elif fst.st_mode & 0o077:
                    err = "bad_mode"
                elif fst.st_size > netjobs.JOB_MAX_BYTES:
                    err = "too_big"
                else:
                    raw = os.read(fd, netjobs.JOB_MAX_BYTES + 1)
                    if len(raw) > netjobs.JOB_MAX_BYTES:
                        raw, err = None, "too_big"
            finally:
                os.close(fd)
        try:
            os.unlink(name, dir_fd=self._dfd)   # consommation unique, avant toute action
        except OSError:
            pass
        return raw, err

    def process(self, name):
        m = NAME_RE.fullmatch(name)
        raw, err = self._take(name)
        jid = m.group(1) if m else None
        if m is None:
            err = err or "bad_name"
        action = None
        if err is None:
            try:
                job = json.loads(raw.decode("utf-8"))
                if not isinstance(job, dict) or job.get("v") != 1 or job.get("id") != jid:
                    raise ValueError("bad_job")
                action = job.get("action")
                created = job.get("created")
                if not isinstance(created, (int, float)) or isinstance(created, bool) or \
                        not -60 <= self.clock() - created <= self.max_age:
                    raise ValueError("expired")
                if action == "wifi-apply" and self.clock() - created > WIFI_APPLY_PICKUP_MAX_S:
                    raise ValueError("expired")   # le service a déjà abandonné (ou va abandonner) ce job
                payload = netjobs.check_payload(action, job.get("payload"))
            except (ValueError, UnicodeDecodeError) as e:
                err = str(e) if str(e) in ("expired", "bad_action", "bad_fields", "bad_ap") else "bad_job"
                action = action if action in netjobs.ACTIONS else None
            else:
                try:
                    err = getattr(self, "_do_" + action.replace("-", "_"))(payload)
                finally:
                    payload = None   # pas de copie durable du secret dans ce processus
        res = {"v": 1, "id": jid, "action": action, "ok": err is None, "error": err, "at": round(self.clock(), 3)}
        self.log("net_job", id=jid, action=action, ok=err is None, error=err)
        if jid and action:
            self._write_result(jid, res)
        return res

    def _write_result(self, jid, res):
        """Résultat déposé pour le service dans son dossier (propriété opentv) : dossier ouvert sans suivre de lien,
        temporaire exclusif, fchown/fchmod sur le descripteur, rename relatif au dossier (jamais de chemin suivi)."""
        try:
            dfd = fsutil.open_dir(self.results_dir)
            try:
                fsutil.atomic_write(dfd, jid + ".json", json.dumps(res, sort_keys=True).encode("utf-8"), mode=0o600,
                                    uid=self.owner_uid if self.chown else None)
            finally:
                os.close(dfd)
        except OSError as e:
            self.log("net_result_error", id=jid, error=type(e).__name__)

    # -- actions (argv fixes, durée bornée par le budget de l'exécution)
    def _nm(self, *args, timeout=NM_CMD_TIMEOUT_S, wait=None, reserve=0.0):
        """nmcli en argv fixe. Délai tronqué au budget restant (moins la marge de mise à mort du groupe et la
        `reserve` gardée pour un retour arrière) ; si le budget est épuisé, rien n'est lancé (124). `wait` devient
        `--wait N` avec N < délai : nmcli rend la main."""
        avail = self.budget.remaining() - 2 * KILL_GRACE_S - reserve
        t = min(float(timeout), avail)
        argv = ["nmcli"]
        if wait is not None:
            w = int(min(wait, t - NM_GRACE_S))
            if w < 1:
                return 124
            argv += ["--wait", str(w)]
        elif t < 1:
            return 124
        return self.runner(argv + list(args), t)[0]

    def _do_wifi_apply(self, p):
        """Transactionnel : l'ancien opentv-wifi (keyfile 0600) n'est ni modifié ni supprimé avant que le candidat
        (opentv-wifi-candidate, UUID neuf, autoconnect=false) soit actif. Succès ⇒ promotion (le keyfile final, même
        UUID que le candidat, remplace l'ancien par rename atomique : point de validation). Échec, délai ou erreur
        avant ce point ⇒ candidat supprimé puis hotspot (mode configuration hors ligne) ou ancien profil remonté,
        dans un budget réservé à l'avance. Aucun SSID ni mot de passe en argv, journal ou résultat."""
        # laisse la page de confirmation partir avant de couper le point d'accès (borné par le budget)
        self.sleep(max(0.0, min(p["delay_s"], self.budget.remaining() - self.min_job_s)))
        ap_path = nmkeyfile.profile_path(self.nm_dir, nmkeyfile.AP_ID)
        ap_existed = os.path.lexists(ap_path)
        old_existed = os.path.lexists(nmkeyfile.profile_path(self.nm_dir, nmkeyfile.WIFI_ID))
        old_uuid = nmkeyfile.read_uuid(self.nm_dir, nmkeyfile.WIFI_ID) if old_existed else None
        if p["restore_ap"] or ap_existed:
            self._nm("connection", "down", "id", nmkeyfile.AP_ID, wait=AP_DOWN_WAIT_S,
                     timeout=AP_DOWN_WAIT_S + NM_GRACE_S)
        cand_uuid = str(_uuid.uuid4())
        try:
            cand = nmkeyfile.write_profile(self.nm_dir, nmkeyfile.CAND_ID, nmkeyfile.wifi_profile(
                p["ssid"], p["psk"], p["hidden"], uuid=cand_uuid, ifname=self.ifname, conn_id=nmkeyfile.CAND_ID,
                autoconnect=False))
        except (OSError, ValueError):
            cand = None
        err = "wifi_connect_failed"
        if cand is not None and \
                self._nm("connection", "load", cand, reserve=ROLLBACK_RESERVE_S) == 0 and \
                self._nm("connection", "up", "uuid", cand_uuid, wait=WIFI_UP_WAIT_S,
                         timeout=WIFI_UP_WAIT_S + NM_GRACE_S, reserve=ROLLBACK_RESERVE_S) == 0:
            err = self._promote(p, cand_uuid, cand)
            if err is None:
                if ap_existed:
                    nmkeyfile.remove_profile(self.nm_dir, nmkeyfile.AP_ID)
                    self._nm("connection", "delete", "id", nmkeyfile.AP_ID)
                return None
        self._rollback(cand_uuid, old_existed, old_uuid, p["restore_ap"], ap_existed, ap_path)
        return err

    def _promote(self, p, cand_uuid, cand_path):
        """Le keyfile final (id opentv-wifi, UUID du candidat, autoconnect) remplace l'ancien par rename atomique :
        c'est le point de validation. Avant lui, tout échec laisse l'ancien intact (retour arrière)."""
        try:
            final = nmkeyfile.write_profile(self.nm_dir, nmkeyfile.WIFI_ID, nmkeyfile.wifi_profile(
                p["ssid"], p["psk"], p["hidden"], uuid=cand_uuid, ifname=self.ifname, conn_id=nmkeyfile.WIFI_ID,
                autoconnect=True))
        except (OSError, ValueError):
            return "wifi_promote_failed"
        nmkeyfile.remove_profile(self.nm_dir, nmkeyfile.CAND_ID)
        # NetworkManager relit les deux fichiers : l'UUID du candidat (actif) est désormais porté par opentv-wifi,
        # l'ancien UUID (fichier remplacé) et le fichier candidat (retiré) disparaissent ; aucune coupure.
        if self._nm("connection", "load", final, cand_path) != 0:
            self.log("wifi_promote_reload_failed")   # fichiers en place : pris en compte au prochain rechargement
        return None

    def _rollback(self, cand_uuid, old_existed, old_uuid, restore_ap, ap_existed, ap_path):
        nmkeyfile.remove_profile(self.nm_dir, nmkeyfile.CAND_ID)   # le secret refusé ne persiste pas
        self._nm("connection", "delete", "uuid", cand_uuid)
        if restore_ap:   # configuration hors ligne : le hotspot sert à réessayer ; l'ancien profil reste intact
            if ap_existed:
                self._nm("connection", "load", ap_path)
            self._nm("connection", "up", "id", nmkeyfile.AP_ID, wait=AP_UP_WAIT_S, timeout=AP_UP_WAIT_S + NM_GRACE_S)
        elif old_existed:
            self._nm("connection", "up", *(("uuid", old_uuid) if old_uuid else ("id", nmkeyfile.WIFI_ID)),
                     wait=OLD_UP_WAIT_S, timeout=OLD_UP_WAIT_S + NM_GRACE_S)

    def _do_ap_start(self, p):
        path = nmkeyfile.write_profile(self.nm_dir, nmkeyfile.AP_ID,
                                       nmkeyfile.ap_profile(p["ssid"], p["psk"], ifname=self.ifname))
        if self._nm("connection", "load", path) == 0 and \
                self._nm("connection", "up", "id", nmkeyfile.AP_ID, wait=AP_UP_WAIT_S,
                         timeout=AP_UP_WAIT_S + NM_GRACE_S) == 0:
            return None
        nmkeyfile.remove_profile(self.nm_dir, nmkeyfile.AP_ID)
        self._nm("connection", "delete", "id", nmkeyfile.AP_ID)
        return "ap_start_failed"

    def _do_ap_stop(self, p):
        stop_ap(self)
        return None


def stop_ap(h):
    """Arrêt + suppression du point d'accès OpenTV (profil retiré même si nmcli échoue). Utilisé par ap-stop et par la
    garde root opentv-ap-guard ; argv fixes, aucun shell."""
    h._nm("connection", "down", "id", nmkeyfile.AP_ID, wait=AP_DOWN_WAIT_S, timeout=AP_DOWN_WAIT_S + NM_GRACE_S)
    removed = nmkeyfile.remove_profile(h.nm_dir, nmkeyfile.AP_ID)
    h._nm("connection", "delete", "id", nmkeyfile.AP_ID)
    return removed
