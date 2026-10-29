"""opentv-pi muse status | authorize-control | revoke-control | pair | unpair | prepare-bluetooth | restore-bluetooth :
administration du pont Muse.

Portes humaines et barrières :
- `pair` exige root, un SDK épinglé vérifié (empreintes), sa dépendance websockets épinglée, l'absence du service
  musegadget d'origine, le compte opentv-muse (groupe privé, membre du groupe de l'Agent Port), la compatibilité BlueZ
  du SDK ACTIVE (ExchangeMTU = 256 et greffon battery désactivé dans bluetoothd en cours : Android ET iPhone ; sinon
  refus avec la commande `prepare-bluetooth`) et une politique OpenTV qui autorise DÉJÀ Muse à PILOTER OpenTV (choix
  explicite d'Alex : « Gérer les assistants » sur la TV → « Piloter OpenTV », ou `authorize-control --yes` ci-dessous).
  Le jeton SDK (mgst_…) est saisi sans écho (getpass) ou lu sur stdin (--token-stdin), jamais en argument, variable
  d'environnement ou sortie ; il est écrit 0600 opentv-muse par descripteur dans /var/lib/opentv-muse. L'identité
  Agent Port DÉDIÉE « muse-device » est émise avec EXACTEMENT musebridge.MUSE_SCOPES (fichier client root:opentv-muse
  0640) ; son plafond reste l'autorisation de Muse sur la TV, jamais au-delà (pas de --force). L'identité « muse »
  d'un autre client local n'est jamais émise, révoquée ni invalidée. Si « muse-device » existe déjà sans être prouvée
  à nous (jeton de notre fichier client) : refus, sauf remplacement explicite --replace-identity ; prouvée à nous mais
  avec d'autres scopes (ancien pont en lecture seule) : réémise avec les scopes exacts. La fenêtre Bluetooth
  bornée tourne dans l'unité bac à sable opentv-muse-pair.service ; opentv-muse.service n'est activé qu'APRÈS un
  appariement réussi ; tout échec de l'appariement révoque l'identité tout juste émise et retire son fichier client.
  Séquence du pont (déterministe, idempotente) : pont sain et connecté (statut « registered » écrit par le MainPID
  courant, relu deux fois), identité exacte, rien à changer -> jamais interrompu. Sinon : arrêt puis ATTENTE de la fin
  réelle de l'ancien processus (borne STOP_WAIT_S ; sinon bridge_stop_timeout, rien changé) AVANT toute réémission,
  révocation ou --force ; ensuite seulement reset-failed, enable, start --no-block et relecture systemd + statut du
  NOUVEAU processus : ok seulement s'il tourne ; refus/échec -> ok false, code 1, appariement et identité conservés
  (relancer `muse pair`, sans Bluetooth). Jamais un succès suivi d'un pont laissé arrêté par la sortie tardive (78)
  d'un ancien processus en pause.
- `unpair` arrête et désactive le pont, efface l'appariement (et le jeton SDK avec --forget-token), révoque
  l'identité « muse-device » SEULEMENT si c'est la nôtre (comparaison du jeton) et retire son fichier client.
- migration (pair/unpair) d'une installation de développement antérieure qui émettait « muse » au compte
  opentv-muse : son fichier client agent-muse-<uid opentv-muse>.json est retiré, et l'entrée « muse » du registre
  n'est révoquée que si elle porte encore CE jeton (jamais celle d'un autre client).
- `prepare-bluetooth` : compatibilité BlueZ du SDK (opentv_pi.musebluez), notée et réversible ; redémarre bluetooth
  seulement si nécessaire. `restore-bluetooth` (uninstall.sh) : retire seulement ce qu'OpenTV a noté.
- `authorize-control --yes` (root, décision explicite d'Alex sur la boîte) : la ligne de politique « muse » SEULE
  passe au niveau « control » (lecture, avis, messages, pilotage) dans une écriture atomique verrouillée ; aucune
  identité n'est émise ni élargie (le pont la reçoit à `muse pair`). Sans --yes : plan affiché, rien écrit.
  `revoke-control` : retire « control » de la ligne « muse » ET, dans la même écriture, de l'identité du pont (qui
  refuse alors de s'enregistrer) ; les autres droits de Muse restent. Les autres assistants ne sont jamais touchés.
- `status` ne lit jamais un fichier secret (présence/mode seulement) et n'affiche aucun jeton ni indice de jeton.
"""
import getpass
import json
import os
import re
import stat
import sys
import time

from . import agents, config, fsutil, musebluez, musebridge
from .nethelper import default_runner

MUSE_USER = "opentv-muse"
UNIT = "opentv-muse.service"
PAIR_UNIT = "opentv-muse-pair.service"
STOCK_UNIT = "musegadget.service"
STATE_DIR = musebridge.DEFAULT_STATE_DIR
INSTALLED_SDK = "/opt/opentv/lib/muse"
STOCK_PATHS = ("/etc/systemd/system/musegadget.service", "/lib/systemd/system/musegadget.service",
               "/usr/lib/systemd/system/musegadget.service",
               "/etc/systemd/system/multi-user.target.wants/musegadget.service", "/opt/musegadget",
               "/usr/local/bin/musegadget")
STOCK_MASK = "/etc/systemd/system/musegadget.service"
AGENT_ID = musebridge.MUSE_AGENT_ID
LEGACY_ID = musebridge.LEGACY_AGENT_ID
# = musegadget.config._SDK_TOKEN (vérifié par les tests) : mgst_ + 43 caractères base64url canoniques
SDK_TOKEN_RE = re.compile(r"mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
WORD_RE = re.compile(r"[a-z-]{1,24}")
MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
SYSTEMCTL_S = 15
PAIR_WAIT_S = musebridge.PAIR_TIMEOUT_S + 90
OPEN_WAIT_S = 45
REGISTER_WAIT_S = 30
# arrêt d'un ancien pont : TimeoutStopSec par défaut (90 s, puis SIGKILL) + marge ; jamais d'identité changée avant
STOP_WAIT_S = 100
HEALTHY_SETTLE_S = 2.0
UNIT_PROPS = ("LoadState", "ActiveState", "SubState", "MainPID", "Result", "ExecMainStatus", "UnitFileState")
PROP_RE = re.compile(r"[A-Za-z0-9_-]{0,32}")
FINAL_PAIR = ("paired", "already_paired", "invalid_sdk_token", "no_sdk_token", "window_closed", "error")
TOKEN_PROMPT = "Jeton SDK Muse (gadgets.muse.ai > Account > SDK tokens, saisie masquée) : "


def _out(obj, code=0, stream=None):
    (stream or sys.stdout).write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    return code


class Admin:
    def __init__(self, cfg, root="/", runner=default_runner, euid=os.geteuid, pwd_mod=None, grp_mod=None,
                 sdk_dir=None, stdin=None, prompt=getpass.getpass, err=None, sleep=time.sleep, clock=time.monotonic,
                 trusted_uids=None, allow_restart=None, proc_root=None):
        import grp
        import pwd
        self.cfg, self.root, self.runner, self.euid = cfg, root, runner, euid
        # bluetooth n'est redémarré que sur la vraie racine (jamais dans une racine de test ni en dry-run)
        self.allow_restart = root == "/" if allow_restart is None else allow_restart
        self.proc_root = proc_root
        self.pwd, self.grp = pwd_mod or pwd, grp_mod or grp
        self.sdk_dir = sdk_dir or self.p(INSTALLED_SDK)
        self.stdin, self.prompt, self.err = stdin or sys.stdin, prompt, err or sys.stderr
        self.sleep, self.clock = sleep, clock
        if trusted_uids is None:
            from .cli import CLIENT_DIR_TRUSTED_UIDS
            trusted_uids = CLIENT_DIR_TRUSTED_UIDS
        self.trusted_uids = trusted_uids

    # -- utilitaires
    def p(self, path):
        return path if self.root == "/" else os.path.join(self.root, path.lstrip("/"))

    def say(self, text):
        self.err.write(text + "\n")
        self.err.flush()

    def systemctl_word(self, *args):
        rc, out = self.runner(["systemctl"] + list(args), SYSTEMCTL_S)
        w = (out or "").strip().splitlines()[-1:] or [""]
        return w[0] if WORD_RE.fullmatch(w[0]) else "unknown"

    def systemctl(self, *args):
        rc, _ = self.runner(["systemctl"] + list(args), SYSTEMCTL_S)
        return rc == 0

    def stock_present(self):
        out = []
        for path in STOCK_PATHS:
            q = self.p(path)
            if path == STOCK_MASK and os.path.islink(q) and os.readlink(q) == "/dev/null":
                continue   # masque OpenTV : le service d'origine ne peut pas démarrer
            if os.path.lexists(q):
                out.append(path)
        return out

    def stock_masked(self):
        q = self.p(STOCK_MASK)
        return os.path.islink(q) and os.readlink(q) == "/dev/null"

    def muse_account(self):
        """-> (pw, problème) ; groupe privé et membre du groupe de l'Agent Port exigés."""
        from .cli import _private_group
        try:
            pw = self.pwd.getpwnam(MUSE_USER)
        except KeyError:
            return None, "muse_user_missing"
        if pw.pw_uid == 0:
            return None, "muse_user_is_root"
        if not _private_group(pw):
            return None, "muse_user_group_not_private"
        try:
            members = self.grp.getgrnam(self.cfg.agent_group).gr_mem
        except KeyError:
            return None, "agent_group_missing"
        if MUSE_USER not in members:
            return None, "muse_user_not_in_agent_group"
        return pw, None

    def open_state_dir(self, pw):
        """Dossier d'état du pont : ouvert sans suivre de lien, propriétaire opentv-muse, aucun accès groupe/autres."""
        try:
            dfd = fsutil.open_dir(self.p(STATE_DIR))
        except OSError:
            raise fsutil.UnsafePath("state_dir_missing")
        st = os.fstat(dfd)
        if st.st_uid != pw.pw_uid or st.st_mode & 0o077:
            os.close(dfd)
            raise fsutil.UnsafePath("state_dir_unsafe")
        return dfd

    @staticmethod
    def entry(dfd, name):
        """Présence d'un fichier d'état SANS le lire : absent | present | unsafe (lien, non régulier, mode)."""
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077:
            return "unsafe"
        return "present"

    def registry(self):
        return agents.Registry(self.cfg.registry_file)

    def service_ids(self):
        if self.euid() != 0:
            return {"uid": None, "gid": None}
        pw = self.pwd.getpwnam(self.cfg.service_user)
        return {"uid": pw.pw_uid, "gid": pw.pw_gid}

    def link_status(self):
        d = None
        try:
            d = json.loads(fsutil.read_regular(self.p(musebridge.RUN_STATUS), 4096).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(d, dict) or not isinstance(d.get("state"), str) or not WORD_RE.fullmatch(
                d["state"].replace("_", "-")):
            return None
        out = {"state": d["state"]}
        if isinstance(d.get("since"), (int, float)):
            out["since"] = d["since"]
        if isinstance(d.get("reason"), str) and musebridge.ERROR_RE.fullmatch(d["reason"]):
            out["reason"] = d["reason"]
        if isinstance(d.get("pid"), int) and not isinstance(d["pid"], bool) and 0 < d["pid"] < 2 ** 31:
            out["pid"] = d["pid"]
        return out

    # -- relecture systemd du pont (aucun secret : état, sous-état, PID principal, résultat, code de sortie)
    def unit_state(self, unit=UNIT):
        rc, out = self.runner(["systemctl", "show", "--property=" + ",".join(UNIT_PROPS), unit], SYSTEMCTL_S)
        d = {}
        for line in (out or "").splitlines()[:len(UNIT_PROPS) * 2]:
            k, sep, v = line.partition("=")
            if sep and k in UNIT_PROPS and k not in d and PROP_RE.fullmatch(v):
                d[k] = v
        pid, code = d.get("MainPID", ""), d.get("ExecMainStatus", "")
        return {"load": d.get("LoadState") or "unknown", "active": d.get("ActiveState") or "unknown",
                "sub": d.get("SubState") or "unknown", "result": d.get("Result") or "unknown",
                "enabled": d.get("UnitFileState") or "unknown",
                "pid": int(pid) if pid.isdigit() and 0 < int(pid) < 2 ** 31 else None,
                "exit_status": int(code) if code.isdigit() and int(code) < 256 else None}

    @staticmethod
    def _stopped(st):
        return st["active"] in ("inactive", "failed") and st["pid"] is None

    @staticmethod
    def _service_view(st):
        return {"unit": UNIT, "active": st["active"], "sub": st["sub"], "enabled": st["enabled"],
                "result": st["result"], "exit_status": st["exit_status"]}

    def healthy_bridge(self):
        """Pont en marche, sain et connecté : actif, statut ÉCRIT PAR CE PROCESSUS (pid = MainPID) et « registered »,
        relu une seconde fois après HEALTHY_SETTLE_S (un refus en vol publie « refused » aussitôt). -> état ou None."""
        st = None
        for i in range(2):
            if i:
                self.sleep(HEALTHY_SETTLE_S)
            cur, link = self.unit_state(), self.link_status()
            if cur["active"] != "active" or cur["pid"] is None or not link or link.get("pid") != cur["pid"] or \
                    link["state"] != "registered" or (st is not None and st["pid"] != cur["pid"]):
                return None
            st = cur
        return st

    def stop_bridge(self):
        """Arrête le pont et ATTEND la fin réelle de son processus (inactive/failed, MainPID 0), borné à STOP_WAIT_S :
        aucune identité ni aucun appariement n'est changé tant qu'un ancien processus peut encore sortir.
        -> (True, état) ou (False, état)."""
        st = self.unit_state()
        if self._stopped(st):
            return True, st
        self.systemctl("stop", "--no-block", UNIT)
        deadline = self.clock() + STOP_WAIT_S
        while True:
            st = self.unit_state()
            if self._stopped(st):
                return True, st
            if self.clock() >= deadline:
                return False, st
            self.sleep(0.5)

    def start_bridge(self):
        """(identité et appariement déjà écrits) Efface un échec précédent (exit 78), active puis démarre le pont et
        relit systemd + le statut écrit par le NOUVEAU processus. -> (True, relecture) seulement si le pont tourne
        (enregistré ou en cours de connexion) ; (False, relecture) s'il refuse, échoue ou ne démarre pas."""
        self.systemctl("reset-failed", UNIT)
        enabled = self.systemctl("enable", UNIT)
        if not self.systemctl("start", "--no-block", UNIT):
            st = self.unit_state()
            return False, {"error": "bridge_start_failed", "service": self._service_view(st), "enable_ok": enabled}
        t0 = self.clock()
        deadline = t0 + REGISTER_WAIT_S
        while True:
            st, link = self.unit_state(), self.link_status()
            fresh = link if link and st["pid"] is not None and link.get("pid") == st["pid"] else None
            rb = {"service": self._service_view(st), "link": fresh, "enable_ok": enabled}
            if st["active"] == "active" and fresh:
                if fresh["state"] == "registered":
                    return True, dict(rb, registered=True)
                if fresh["state"] in ("refused", "not_paired", "unpaired", "stopped"):
                    return False, dict(rb, error="bridge_refused" if fresh["state"] == "refused"
                                       else "bridge_not_running", reason=fresh.get("reason"))
            # juste après « start --no-block », la tâche peut encore être en file : inactive/failed lus après 5 s
            if st["active"] in ("inactive", "failed") and self.clock() - t0 >= 5:
                return False, dict(rb, error="bridge_refused" if st["exit_status"] == musebridge.EX_CONFIG
                                   else "bridge_start_failed")
            if self.clock() >= deadline:
                if st["active"] == "active" and st["pid"] is not None:   # démarré, Muse pas encore joint : dit tel quel
                    return True, dict(rb, registered=False)
                return False, dict(rb, error="bridge_not_started")
            self.sleep(1.0)

    def identity_public(self, dfd):
        try:
            fd = os.open("identity.json", os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            d = json.loads(os.read(fd, 4096).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            return None
        finally:
            os.close(fd)
        mac = d.get("mac") if isinstance(d, dict) else None
        if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
            return None
        suffix = mac.replace(":", "")[-6:]   # mêmes règles que musegadget.identity.Identity
        return {"node_id": "homelink-" + suffix, "ble_name": "MuseGadget" + suffix.upper()}

    def bluez(self):
        return musebluez.BlueZ(self.root, self.runner, proc_root=self.proc_root)

    # -- identité dédiée « muse-device » : propriété prouvée par le jeton de NOTRE fichier client
    def client_name(self, agent, pw):
        return agents.client_file_name(agent, pw.pw_uid)

    @staticmethod
    def _client_token(cfd, name, agent):
        """Jeton du fichier client `name` (root seulement) ; None si absent/illisible/autre agent. Jamais affiché."""
        try:
            fd = os.open(name, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC, dir_fd=cfd)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return None
            d = json.loads(os.read(fd, 8192).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            return None
        finally:
            os.close(fd)
        if not isinstance(d, dict) or d.get("agent_id") != agent or not isinstance(d.get("token"), str):
            return None
        return d["token"]

    def _clients(self):
        return fsutil.open_trusted_dir(self.cfg.agent_clients_dir, self.trusted_uids)

    def identity_state(self, pw):
        """(root) -> {"state": absent|owned|outdated|stale_client|unexpected, "reason", "foreign_clients": [...]}.
        owned = entrée du registre portant le jeton de notre fichier client ET exactement MUSE_SCOPES ; outdated =
        prouvée à nous (même jeton) mais d'autres scopes (ancien pont en lecture seule, plafond réduit) : réémise."""
        out = {"state": "absent", "reason": None, "foreign_clients": []}
        try:
            entry = self.registry().list().get(AGENT_ID)
        except (OSError, ValueError):
            return dict(out, state="unexpected", reason="registry_unreadable")
        try:
            cfd = self._clients()
        except OSError:
            return dict(out, state="unexpected", reason="clients_dir_unsafe")
        try:
            ours = self.client_name(AGENT_ID, pw)
            tok = self._client_token(cfd, ours, AGENT_ID)
            have_file = os.path.lexists(os.path.join(self.cfg.agent_clients_dir, ours))
            reg = self.registry()
            for name in sorted(os.listdir(cfd)):
                m = re.fullmatch(r"agent-%s-([0-9]{1,10})\.json" % re.escape(AGENT_ID), name)
                if m and name != ours:
                    other = self._client_token(cfd, name, AGENT_ID)
                    out["foreign_clients"].append({"uid": int(m.group(1)),
                                                   "live": bool(other and reg.entry_matches(AGENT_ID, other))})
        finally:
            os.close(cfd)
        if entry is None:
            if have_file:
                out["state"] = "stale_client"
            return out
        if any(x["live"] for x in out["foreign_clients"]):
            return dict(out, state="unexpected", reason="issued_to_another_account")
        if not tok or not self.registry().entry_matches(AGENT_ID, tok):
            return dict(out, state="unexpected", reason="not_issued_by_muse_pair")
        if entry.get("scopes") != list(musebridge.MUSE_SCOPES):
            return dict(out, state="outdated", reason="scopes_differ")
        return dict(out, state="owned")

    def discard_identity(self, pw, only_owned=True):
        """Révoque « muse-device » si c'est la nôtre (jeton de notre fichier client ; only_owned=False : remplacement
        explicite, entrée révoquée quelle qu'elle soit) puis retire NOTRE fichier client. -> rapport."""
        out = {"agent_revoked": False, "client_file_removed": False}
        try:
            cfd = self._clients()
        except OSError:
            return dict(out, client_file_removed="clients_dir_unsafe")
        try:
            name = self.client_name(AGENT_ID, pw)
            tok = self._client_token(cfd, name, AGENT_ID)
            ids = self.service_ids()
            try:
                if tok:
                    out["agent_revoked"] = self.registry().revoke_token(AGENT_ID, tok, **ids)
                if not only_owned and not out["agent_revoked"]:
                    out["agent_revoked"] = self.registry().revoke(AGENT_ID, **ids)
            except (OSError, ValueError) as e:
                out["agent_revoked"] = None
                out["agent_error"] = "registry_busy" if isinstance(e, agents.RegistryBusy) else \
                    str(e) or "registry_error"
            out["client_file_removed"] = self._unlink(cfd, name)
        finally:
            os.close(cfd)
        return out

    def migrate_legacy(self, pw):
        """Ancienne identité « muse » émise au compte opentv-muse (installation de développement) : fichier client
        retiré ; entrée « muse » révoquée seulement si elle porte encore CE jeton (jamais celle d'un autre client)."""
        out = {"legacy_client_removed": False, "legacy_identity_revoked": False}
        try:
            cfd = self._clients()
        except OSError:
            return dict(out, legacy_client_removed="clients_dir_unsafe")
        try:
            name = self.client_name(LEGACY_ID, pw)
            tok = self._client_token(cfd, name, LEGACY_ID)
            if tok:
                try:
                    out["legacy_identity_revoked"] = self.registry().revoke_token(LEGACY_ID, tok, **self.service_ids())
                except (OSError, ValueError):
                    out["legacy_identity_revoked"] = None
                    return out   # registre indisponible : fichier gardé pour retenter (preuve de propriété)
            out["legacy_client_removed"] = self._unlink(cfd, name)
        finally:
            os.close(cfd)
        return out

    # -- status
    def status(self):
        sdk = musebridge.verify_sdk(self.sdk_dir if os.path.isdir(self.sdk_dir) else None)
        rep = {"ok": True, "commands": sorted(musebridge.COMMANDS),
               "sdk": {"verified": sdk["ok"], "commit": sdk["commit"], "problems": sdk["problems"][:10],
                       "websockets": sdk["websockets"], "mode": "transport_only",
                       "default_commands": "absent"},
               "stock_musegadget": {"present": self.stock_present(), "masked": self.stock_masked()},
               "service": {"unit": UNIT, "enabled": self.systemctl_word("is-enabled", UNIT),
                           "active": self.systemctl_word("is-active", UNIT)},
               "pair_window": self.systemctl_word("is-active", PAIR_UNIT),
               "paired": None, "sdk_token": "unknown", "identity": None, "link": self.link_status()}
        try:
            pw = self.pwd.getpwnam(MUSE_USER)
        except KeyError:
            pw = None
            rep["account"] = "muse_user_missing"
        if pw is not None:
            try:
                dfd = self.open_state_dir(pw)
            except OSError as e:
                rep["state_dir"] = getattr(e, "reason", "unreadable")
            else:
                try:
                    pairing = self.entry(dfd, "pairing.json")
                    rep["paired"] = pairing == "present" if pairing != "unsafe" else None
                    rep["sdk_token"] = self.entry(dfd, "sdk_token")
                    rep["identity"] = self.identity_public(dfd)
                    ps = self.read_state_status(dfd)
                    if ps:
                        rep["last_pair"] = ps
                finally:
                    os.close(dfd)
        try:
            reg = self.registry()
            issued = reg.list().get(AGENT_ID)
            allowed = agents.ceiling(reg.policy(), AGENT_ID)
            rep["agent"] = {"id": AGENT_ID, "issued": issued is not None,
                            "scopes": (issued or {}).get("scopes"), "required_scopes": list(musebridge.MUSE_SCOPES),
                            "exact_scopes": (issued or {}).get("scopes") == list(musebridge.MUSE_SCOPES),
                            "ceiling_from": agents.LINKED_POLICY[AGENT_ID],
                            "allowed_by_policy": allowed if isinstance(allowed, list) else None,
                            "control_authorized": isinstance(allowed, list) and
                            set(musebridge.MUSE_SCOPES) <= set(allowed)}
        except (OSError, ValueError):
            rep["agent"] = {"id": AGENT_ID, "issued": None, "error": "registry_unreadable"}
        if pw is not None:   # présence seulement (jamais lus : secrets)
            d = self.cfg.agent_clients_dir
            rep["agent"]["client_file"] = os.path.lexists(os.path.join(d, self.client_name(AGENT_ID, pw)))
            if os.path.lexists(os.path.join(d, self.client_name(LEGACY_ID, pw))):
                rep["agent"]["legacy_migration"] = "pending"
        rep["bluetooth"] = self.bluez().status()
        gates = []
        if not sdk["ok"]:
            gates.append("sdk_unverified")
        if sdk["websockets"] != "ok":
            gates.append("websockets_missing")
        if rep["stock_musegadget"]["present"]:
            gates.append("stock_musegadget_present")
        if rep["bluetooth"]["gate"]:
            gates.append(rep["bluetooth"]["gate"])
        if not (rep.get("agent") or {}).get("control_authorized"):
            gates.append("muse_control_not_authorized")
        if rep["paired"] is not True:
            gates.append("human_pairing_required")
        if (rep.get("link") or {}).get("state") != "registered":
            gates.append("muse_link_not_registered")
        rep["gates"] = gates
        return rep

    # -- pair
    def preflight(self):
        """-> (pw, None) ou (None, {erreur}) ; aucune écriture."""
        if self.euid() != 0:
            return None, {"error": "root_required", "note": "sudo /opt/opentv/bin/opentv-pi muse pair"}
        sdk = musebridge.verify_sdk(self.sdk_dir if os.path.isdir(self.sdk_dir) else None)
        if not sdk["ok"]:
            return None, {"error": "sdk_unverified", "problems": sdk["problems"][:10],
                          "note": "réinstaller OpenTV (sudo ./install.sh) : SDK épinglé absent ou modifié"}
        if sdk["websockets"] != "ok":
            return None, {"error": "dependency_missing", "dependency": "websockets", "state": sdk["websockets"],
                          "note": "sudo ./prepare-pi.sh --yes (roue épinglée par SHA-256) puis sudo ./install.sh"}
        stock = self.stock_present()
        if stock:
            return None, {"error": "stock_musegadget_present", "paths": stock,
                          "note": "Le service musegadget d'origine donne system.run/file.* à Muse : le retirer "
                                  "(bash install.sh --uninstall du SDK) avant d'apparier OpenTV."}
        pw, why = self.muse_account()
        if pw is None:
            return None, {"error": why, "note": "sudo ./install.sh (systemd-sysusers crée opentv-muse)"}
        try:
            self.grp.getgrnam("bluetooth")
        except KeyError:
            return None, {"error": "bluetooth_group_missing", "note": "sudo apt install bluez"}
        bt = self.bluez().status()
        if not bt["ready"]:   # Android (MTU) ET iPhone (greffon battery) : jamais une fenêtre vouée à l'échec
            return None, {"error": bt["gate"] or "bluetooth_not_prepared", "bluetooth": bt, "note": bt["note"]}
        try:
            reg = self.registry()
            defined, pol = reg.policy_defined(), agents.ceiling(reg.policy(), AGENT_ID)
        except (OSError, ValueError):
            return None, {"error": "registry_unreadable"}
        if not defined or not isinstance(pol, list) or not set(musebridge.MUSE_SCOPES) <= set(pol):
            return None, {"error": "muse_control_not_authorized", "required_scopes": list(musebridge.MUSE_SCOPES),
                          "allowed_by_policy": pol if isinstance(pol, list) else None,
                          "note": "Muse pilote OpenTV seulement si Alex l'autorise : sur la TV, Réglages > Gérer les "
                                  "assistants > Muse « Piloter OpenTV », ou sudo /opt/opentv/bin/opentv-pi muse "
                                  "authorize-control --yes ; puis relancer."}
        return pw, None

    def read_token(self, from_stdin):
        if from_stdin:
            tok = self.stdin.readline(256)
        else:
            if not (hasattr(self.stdin, "isatty") and self.stdin.isatty()):
                raise ValueError("token_input_required")
            tok = self.prompt(TOKEN_PROMPT)
        tok = (tok or "").strip()
        if not SDK_TOKEN_RE.fullmatch(tok):
            raise ValueError("invalid_sdk_token")
        return tok

    def issue_identity(self, pw):
        from .cli import issue_root_client
        return issue_root_client(self.cfg, self.registry(), self.service_ids(), AGENT_ID,
                                 list(musebridge.MUSE_SCOPES), pw, force=False, trusted_uids=self.trusted_uids)

    def wait_pair(self, dfd):
        start = self.clock()
        # l'état de l'unité n'est lu qu'après 5 s : juste après « start --no-block », la tâche peut encore être en
        # file d'attente et se lire « inactive » (faux échec)
        deadline, announced, last_check = start + PAIR_WAIT_S, False, start
        opened_by = start + OPEN_WAIT_S
        while self.clock() < deadline:
            st = self.read_state_status(dfd)
            if st and st["state"] == "open" and not announced:
                announced = True
                self.say("Fenêtre d'appariement ouverte %d min. Dans l'app Muse : Réglages > Appareils > Mode "
                         "développeur, puis Ajouter un appareil (+) et choisir %s. Wi-Fi : garder « le réseau "
                         "actuel »." % (st.get("timeout_s", 600) // 60, st.get("ble_name", "MuseGadget…")))
            if st and st["state"] in FINAL_PAIR:
                return st
            now = self.clock()
            if now - last_check >= 5:
                last_check = now
                active = self.systemctl_word("is-active", PAIR_UNIT)
                if active in ("inactive", "failed"):
                    st = self.read_state_status(dfd)
                    return st if st and st["state"] in FINAL_PAIR else {"state": "pair_unit_failed"}
            if not announced and now > opened_by:
                return {"state": "pair_window_not_opened"}
            self.sleep(1.0)
        return {"state": "pair_timeout"}

    @staticmethod
    def read_state_status(dfd):
        try:
            fd = os.open(musebridge.PAIR_STATUS_NAME, os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = os.read(fd, 4096)
        finally:
            os.close(fd)
        try:
            d = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(d, dict) or d.get("state") not in musebridge.PAIR_STATES:
            return None
        out = {"state": d["state"]}
        if isinstance(d.get("ble_name"), str) and re.fullmatch(r"MuseGadget[0-9A-F]{6}", d["ble_name"]):
            out["ble_name"] = d["ble_name"]
        if isinstance(d.get("timeout_s"), int) and 0 < d["timeout_s"] <= 3600:
            out["timeout_s"] = d["timeout_s"]
        return out

    @staticmethod
    def _unlink(dfd, name):
        try:
            os.unlink(name, dir_fd=dfd)
            return True
        except FileNotFoundError:
            return False

    def pair(self, token_stdin=False, replace_token=False, force=False, replace_identity=False):
        pw, err = self.preflight()
        if err:
            return dict({"ok": False}, **err), 2
        ident_state = self.identity_state(pw)
        if ident_state["reason"] in ("registry_unreadable", "clients_dir_unsafe"):
            return {"ok": False, "error": ident_state["reason"],
                    "note": "Registre des agents ou %s illisible : rien modifié." % self.cfg.agent_clients_dir}, 2
        if ident_state["state"] == "unexpected" and not replace_identity:
            return {"ok": False, "error": "muse_device_identity_unexpected", "reason": ident_state["reason"],
                    "foreign_clients": ident_state["foreign_clients"],
                    "note": "L'identité %s existe déjà sans venir de cet appariement : rien modifié. Remplacement "
                            "explicite (révoque cette identité seulement) : sudo /opt/opentv/bin/opentv-pi muse pair "
                            "--replace-identity" % AGENT_ID}, 2
        try:
            dfd = self.open_state_dir(pw)
        except OSError as e:
            return {"ok": False, "error": getattr(e, "reason", "state_dir_unsafe"),
                    "note": "sudo systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf"}, 2
        try:
            tok_state = self.entry(dfd, "sdk_token")
            if tok_state == "unsafe":
                return {"ok": False, "error": "sdk_token_file_unsafe"}, 2
            if replace_token or tok_state == "absent":
                try:
                    tok = self.read_token(token_stdin)
                except ValueError as e:
                    return {"ok": False, "error": str(e),
                            "note": "Jeton saisi sans écho (terminal) ou --token-stdin ; jamais en argument."}, 2
                try:
                    fsutil.atomic_write(dfd, "sdk_token", tok + "\n", mode=0o600, uid=pw.pw_uid, gid=pw.pw_gid)
                finally:
                    tok = None
                token_written = True
            else:
                token_written = False
            migration = self.migrate_legacy(pw)
            base = {"sdk_token_written": token_written, "migration": migration}
            paired = self.entry(dfd, "pairing.json") == "present"
            # 1. idempotence : pont déjà connecté par CE processus, identité exacte à nous, rien à changer (ni jeton ni
            #    --force) -> jamais interrompu ; seulement activé au démarrage s'il ne l'était pas (sans --now)
            if paired and not force and not token_written and ident_state["state"] == "owned":
                healthy = self.healthy_bridge()
                if healthy is not None:
                    # `healthy_bridge()` attend volontairement deux lectures stables. La politique ou l'identité peut
                    # changer pendant cette attente : ne jamais rendre un faux succès à partir de l'instantané pris
                    # au début de `pair`. Tous les writers produit réduisent l'identité liée dans la même transaction
                    # que le plafond ; relire les DEUX ici rend cette réduction visible avant le retour idempotent.
                    ident_state = self.identity_state(pw)
                    try:
                        allowed = agents.ceiling(self.registry().policy(), AGENT_ID)
                    except (OSError, ValueError):
                        allowed = None
                    # Le second instantané a la même barrière de propriété que celui du début. Une identité étrangère
                    # peut apparaître pendant les deux lectures de santé ; sans --replace-identity elle est sacrée :
                    # ne pas arrêter le pont et surtout ne jamais tomber jusqu'au discard only_owned=False ci-dessous.
                    if ident_state["state"] == "unexpected" and not replace_identity:
                        return {"ok": False, "error": "muse_device_identity_unexpected",
                                "reason": ident_state["reason"],
                                "foreign_clients": ident_state["foreign_clients"],
                                "note": "L'identité %s a changé pendant la vérification sans venir de cet "
                                        "appariement : rien modifié. Remplacement explicite : sudo "
                                        "/opt/opentv/bin/opentv-pi muse pair --replace-identity" % AGENT_ID}, 2
                    if ident_state["state"] != "owned" or sorted(allowed or []) != list(musebridge.MUSE_SCOPES):
                        healthy = None
                if healthy is not None:
                    if healthy["enabled"] != "enabled":
                        self.systemctl("enable", UNIT)
                        healthy = self.unit_state()
                    return dict(base, ok=True, result="already_paired", paired=True, restarted=False,
                                identity_reissued=False, registered=True, service=self._service_view(healthy),
                                link=self.link_status(),
                                note="Déjà apparié et connecté : rien interrompu. Nouvel appariement : sudo "
                                     "opentv-pi muse pair --force"), 0
            # 2. tout autre cas : l'ancien pont est arrêté et la FIN de son processus attendue avant tout changement
            #    d'identité ou d'appariement (un ancien processus en pause ne peut plus sortir après le démarrage du
            #    nouveau et le laisser arrêté) ; arrêt non confirmé : rien changé, honnête et relançable
            stopped, st = self.stop_bridge()
            if not stopped:
                return dict(base, ok=False, error="bridge_stop_timeout", service=self._service_view(st),
                            note="L'ancien pont ne s'est pas arrêté à temps : identité et appariement inchangés. "
                                 "journalctl -u %s -n 30 --no-pager, puis relancer sudo opentv-pi muse pair" % UNIT), 1
            replaced = None
            if ident_state["state"] == "unexpected":   # --replace-identity : seule « muse-device » est révoquée
                replaced = self.discard_identity(pw, only_owned=False)
            elif ident_state["state"] in ("stale_client", "outdated"):   # outdated : à nous, autres scopes -> réémise
                self.discard_identity(pw)
            if replaced is not None:
                base["replaced_identity"] = replaced
            if force:
                self.systemctl("disable", UNIT)   # déjà arrêté (fin attendue ci-dessus)
                self._unlink(dfd, "pairing.json")
                paired = False
            if paired:
                reissued = False
                if self.identity_state(pw)["state"] != "owned":   # identité aux scopes exacts réémise si absente
                    code, ident = self.issue_identity(pw)
                    if code != 0:
                        return dict({**base, **ident}, ok=False, service=self._service_view(self.unit_state())), code
                    reissued = True
                # 3. identité et appariement écrits : seulement maintenant reset-failed, enable, start, relecture
                started, rb = self.start_bridge()
                out = dict({**base, **rb}, ok=started, paired=True, restarted=True, identity_reissued=reissued)
                if started:
                    out["result"] = "already_paired"
                    out["note"] = "Déjà apparié : pont relancé. Nouvel appariement : sudo opentv-pi muse pair --force"
                else:
                    out["note"] = ("Appariement et identité conservés, pont non démarré : journalctl -u %s -n 30 "
                                   "--no-pager, puis relancer sudo opentv-pi muse pair (aucun Bluetooth)" % UNIT)
                return out, 0 if started else 1
            code, ident = self.issue_identity(pw)
            if code != 0:
                return dict(ident, ok=False), code
            self._unlink(dfd, musebridge.PAIR_STATUS_NAME)

            def failed(error, code, **extra):
                """Échec : fenêtre fermée, identité tout juste émise révoquée et son fichier client retiré."""
                self.systemctl("stop", PAIR_UNIT)
                return dict(base, ok=False, error=error, pair_window="closed", identity=self.discard_identity(pw),
                            **extra), code
            if not self.systemctl("start", "--no-block", PAIR_UNIT):
                return failed("pair_unit_start_failed", 1, note="journalctl -u %s -n 30 --no-pager" % PAIR_UNIT)
            try:
                st = self.wait_pair(dfd)
            except KeyboardInterrupt:
                return failed("interrupted", 130)
            if st["state"] not in ("paired", "already_paired"):
                return failed(st["state"], 1, note="journalctl -u %s -n 30 --no-pager" % PAIR_UNIT)
            # porte suivante, séparée : le service ne démarre qu'apparié (ConditionPathExists=pairing.json)
            started, rb = self.start_bridge()
            out = dict({**base, **rb}, ok=started, paired=True, service_started=started,
                       agent={"id": AGENT_ID, "scopes": list(musebridge.MUSE_SCOPES), "config": ident.get("config"),
                              "ceiling_from": agents.LINKED_POLICY[AGENT_ID]},
                       commands=sorted(musebridge.COMMANDS),
                       gates=["Muse : demander « liste le catalogue OpenTV » et vérifier la réponse dans l'app",
                              "Muse : « propose-moi un film » -> fiche sur la TV (opentv-pi muse status : action "
                              "applied), puis photo HDMI",
                              "journalctl -u opentv-muse -n 30 : muse_registered puis muse_invoke ok"])
            if started:
                out["result"] = "paired"
                return out, 0
            # appariement réussi et conservé (identité comprise) : relancer `muse pair` démarre le pont sans Bluetooth
            out["note"] = ("Appariement réussi et conservé, pont non démarré : journalctl -u %s -n 30 --no-pager, puis "
                           "relancer sudo opentv-pi muse pair (aucun nouveau Bluetooth)" % UNIT)
            return out, 1
        finally:
            os.close(dfd)

    # -- unpair
    def unpair(self, forget_token=False):
        if self.euid() != 0:
            return {"ok": False, "error": "root_required", "note": "sudo /opt/opentv/bin/opentv-pi muse unpair"}, 2
        out = {"ok": True, "service": "disabled" if self.systemctl("disable", "--now", UNIT) else "disable_failed"}
        self.systemctl("stop", PAIR_UNIT)
        try:
            pw = self.pwd.getpwnam(MUSE_USER)
        except KeyError:
            pw = None
        out["unpaired"] = out["sdk_token"] = None
        out["agent_revoked"] = out["client_file_removed"] = None
        if pw is not None:
            try:
                dfd = self.open_state_dir(pw)
            except OSError as e:
                out["state_dir"] = getattr(e, "reason", "unsafe")
                out["ok"] = False
            else:
                try:
                    out["unpaired"] = self._unlink(dfd, "pairing.json")
                    self._unlink(dfd, musebridge.PAIR_STATUS_NAME)
                    out["sdk_token"] = ("removed" if self._unlink(dfd, "sdk_token") else "absent") if forget_token \
                        else "kept"
                finally:
                    os.close(dfd)
            st = self.identity_state(pw)
            if st["state"] == "unexpected":   # jamais révoquée si elle n'est pas prouvée à nous
                out["agent_revoked"] = False
                out["agent_kept"] = st["reason"]
                out["agent_note"] = ("%s n'a pas été émise par muse pair : conservée. Révocation explicite : sudo "
                                     "/opt/opentv/bin/opentv-pi agent revoke --id %s" % (AGENT_ID, AGENT_ID))
                if st["reason"] == "registry_unreadable":
                    out["ok"] = False
            else:
                d = self.discard_identity(pw)
                out.update(d)
                if d.get("agent_revoked") is None or d.get("client_file_removed") == "clients_dir_unsafe":
                    out["ok"] = False
            out["migration"] = self.migrate_legacy(pw)
        out["note"] = "Retirer aussi l'appareil dans l'app Muse (Réglages > Appareils)."
        return out, 0 if out["ok"] else 1

    # -- autorisation explicite du pilotage (root, décision d'Alex sur la boîte)
    def _control_change(self, grant, yes):
        if self.euid() != 0:
            return {"ok": False, "error": "root_required",
                    "note": "sudo /opt/opentv/bin/opentv-pi muse %s" % ("authorize-control --yes" if grant else
                                                                         "revoke-control")}, 2
        from .app import KNOWN_AGENTS, AGENT_LEVELS
        reg = self.registry()
        try:
            view, fp, corrupt = reg.snapshot(KNOWN_AGENTS)
        except (OSError, ValueError):
            return {"ok": False, "error": "registry_unreadable"}, 2
        if corrupt:
            return {"ok": False, "error": "registry_unreadable", "note": "Registre des agents illisible : rien écrit."}, 2
        before = list(view[LEGACY_ID]["allowed"])
        after = list(AGENT_LEVELS["control"]) if grant else [x for x in before if x != "control"]
        plan = {"agent": LEGACY_ID, "policy_before": before, "policy_after": after,
                "bridge_identity": AGENT_ID, "other_agents": "unchanged"}
        if sorted(after) == sorted(before):
            return dict(plan, ok=True, changed=False), 0
        if grant and not yes:
            return dict(plan, ok=False, error="confirmation_required", changed=False,
                        note="Autorise Muse à proposer des films à l'écran, naviguer dans OpenTV et lancer un film du "
                             "catalogue accepté par le lecteur officiel quand tu le lui demandes. Relancer avec --yes "
                             "pour confirmer ; retrait : opentv-pi muse revoke-control (ou Gérer les assistants)."), 2
        try:
            changed = reg.apply_policy_changes({LEGACY_ID: after}, KNOWN_AGENTS, fp, **self.service_ids())
        except ValueError as e:
            return dict(plan, ok=False, error="stale" if str(e) == "stale" else "registry_unreadable"), 2
        except OSError as e:
            return dict(plan, ok=False, error="registry_busy" if isinstance(e, agents.RegistryBusy) else
                        "registry_write_failed"), 2
        out = dict(plan, ok=True, changed=True, result=changed.get(LEGACY_ID))
        if grant:
            out["next"] = ("sudo /opt/opentv/bin/opentv-pi muse pair (réémet l'identité du pont avec les scopes "
                           "exacts ; déjà apparié : aucun Bluetooth)")
        else:
            out["bridge"] = "refuse de s'enregistrer tant que le pilotage n'est pas réautorisé"
        return out, 0

    def authorize_control(self, yes=False):
        return self._control_change(True, yes)

    def revoke_control(self):
        return self._control_change(False, True)

    # -- compatibilité BlueZ du SDK
    def prepare_bluetooth(self, replace_mtu=False, dry_run=False, restart=True):
        if self.euid() != 0 and not dry_run and self.root == "/":
            return {"ok": False, "error": "root_required",
                    "note": "sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth"}, 2
        bz = self.bluez()
        try:
            res = bz.apply(replace_mtu=replace_mtu, dry_run=dry_run)
        except (musebluez.BluezError, OSError) as e:
            st = bz.status()
            return {"ok": False, "error": getattr(e, "code", "write_failed"), "bluetooth": st,
                    "note": st.get("note")}, 2
        out = {"ok": True, "changed": res["changed"], "plan": res["plan"], "restarted": None}
        if dry_run:
            out["bluetooth"] = bz.status()
            return out, 0
        before = bz.status()
        if restart and self.allow_restart and (res["changed"] or before["gate"] in (
                "bluetooth_restart_required", "bluetooth_runtime_unknown")):
            out["restarted"] = bz.restart(daemon_reload=True)
        elif res["changed"] or before["gate"] == "bluetooth_restart_required":
            out["restart"] = "skipped" if self.allow_restart else "skipped_test_root"
        st = bz.status()
        out["bluetooth"] = st
        out["ok"] = bool(st["ready"])
        if not st["ready"]:
            out["error"], out["note"] = st["gate"], st["note"]
        return out, 0 if out["ok"] else 1


def _porcelain(lines):
    sys.stdout.write("".join(" ".join(x) + "\n" for x in lines))


def installer_bluetooth(a):
    """install.sh (transaction) : compatibilité appliquée SANS jamais remplacer une valeur locale ni un drop-in
    étranger ; sortie ligne à ligne (saved/created/dir/restart/skipped/plan) pour le retour arrière d'install.sh."""
    bz = musebluez.BlueZ(a.root)
    try:
        res = bz.apply(backup_dir=a.backup_dir, dry_run=a.dry_run, installer=True)
    except (musebluez.BluezError, OSError) as e:
        sys.stderr.write("opentv-pi muse prepare-bluetooth: %s\n" % getattr(e, "code", type(e).__name__))
        return 1
    lines = [("saved", x) for x in res["saved"]] + [("created", x) for x in res["created"]] + \
        [("dir", x) for x in res["dirs"]] + [("skipped", x) for x in res["skipped"]] + \
        [("plan", x) for x in res["plan"]]
    if res["changed"] and not a.dry_run:
        lines.append(("restart",))
    _porcelain(lines)
    return 0


def restore_bluetooth(a):
    """uninstall.sh : seules les modifications BlueZ notées par OpenTV sont retirées ; lignes changed/restart/kept."""
    bz = musebluez.BlueZ(a.root)
    try:
        res = bz.revert(dry_run=a.dry_run)
    except (musebluez.BluezError, OSError) as e:
        sys.stderr.write("opentv-pi muse restore-bluetooth: %s\n" % getattr(e, "code", type(e).__name__))
        return 1
    lines = [("kept", x) for x in res["kept"]] + [("plan", x) for x in res["plan"]]
    if res["changed"] and not a.dry_run:
        lines.append(("restart",))
    _porcelain(lines)
    return 0


def _no_host_runner(argv, timeout):   # racine de test : jamais une commande sur la machine hôte
    return 1, ""


def main(a, cfg=None, admin=None):
    root = getattr(a, "root", "/") or "/"
    if not os.path.isabs(root):
        return _out({"ok": False, "error": "root_not_absolute"}, 2)
    if a.action == "prepare-bluetooth" and getattr(a, "installer", False):
        return installer_bluetooth(a)
    if a.action == "restore-bluetooth":
        return restore_bluetooth(a)
    cfg = cfg or config.Config.load(a.env)
    adm = admin or (Admin(cfg) if root == "/" else Admin(cfg, root=root, runner=_no_host_runner))
    if a.action == "status":
        return _out(adm.status())
    if a.action == "authorize-control":
        res, code = adm.authorize_control(yes=getattr(a, "yes", False))
        return _out(res, code)
    if a.action == "revoke-control":
        res, code = adm.revoke_control()
        return _out(res, code)
    if a.action == "pair":
        res, code = adm.pair(token_stdin=a.token_stdin, replace_token=a.replace_token, force=a.force,
                             replace_identity=a.replace_identity)
        return _out(res, code)
    if a.action == "prepare-bluetooth":
        res, code = adm.prepare_bluetooth(replace_mtu=a.replace_mtu, dry_run=a.dry_run, restart=not a.no_restart)
        return _out(res, code)
    res, code = adm.unpair(forget_token=a.forget_token)
    return _out(res, code)
