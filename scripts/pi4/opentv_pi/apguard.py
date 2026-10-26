"""Garde root du Wi-Fi temporaire OpenTV (opentv-ap-guard.timer -> opentv-ap-guard.service, oneshot).

Indépendante du service Python : même si opentv.service est arrêté, en échec, en boucle de redémarrage (configuration
invalide) ou figé, le point d'accès `opentv-setup-ap` ne survit pas. Décision stricte, fail-closed :

- aucun AP (ni actif selon NetworkManager, ni profil keyfile) -> rien ;
- opentv.service pas exactement « active » -> arrêt ;
- configuration illisible/invalide, identité de démarrage (boot_id) inconnue -> arrêt ;
- budget absent, d'un autre démarrage, marqué expiré, durée négative ou >= OPENTV_HOTSPOT_MAX_S + GUARD_SLACK_S -> arrêt.

Arrêt = `nmcli connection down` + suppression du profil + `nmcli connection delete` (argv fixes, sans shell, délais
bornés par le budget du helper). `--force-stop` (uninstall.sh) arrête sans condition. Rien d'autre n'est exécuté.
"""
import json
import os
import time

from . import bootid, config, nethelper, netstate, nmkeyfile

SERVICE = "opentv.service"
GUARD_SLACK_S = 30.0         # l'application arrête elle-même l'AP à l'échéance ; la garde est le filet de sécurité
SYSTEMCTL_TIMEOUT_S = 5
GUARD_BUDGET_S = 40.0        # opentv-ap-guard.service : TimeoutStartSec=60
UNIT_TIMEOUT_START_S = 60
STATES = ("active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance")


def service_state(runner):
    rc, out = runner(["systemctl", "is-active", SERVICE], SYSTEMCTL_TIMEOUT_S)
    lines = (out or "").strip().splitlines()
    v = lines[0].strip() if lines else ""
    return v if v in STATES else "unknown"


def read_budget(path, cap=4096):
    """hotspot.json écrit par le service (0600 opentv) ; lien refusé ; None si absent/illisible/invalide."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        raw = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > cap:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return d if isinstance(d, dict) else None


def decide(service, cfg_ok, boot, hs, now, max_s):
    """-> raison d'arrêt (str) ou None si l'AP est légitime (service actif, budget valide et non dépassé)."""
    if service != "active":
        return "service_" + service
    if not cfg_ok:
        return "config_invalid"
    if not boot:
        return "boot_id_unknown"
    if not isinstance(hs, dict) or hs.get("v") != 1 or hs.get("boot_id") != boot:
        return "no_budget"
    if hs.get("expired") is not False:
        return "expired"
    fs = hs.get("first_start")
    if not isinstance(fs, (int, float)) or isinstance(fs, bool):
        return "no_budget"
    elapsed = now - fs
    if elapsed < 0:
        return "clock"
    if elapsed >= max_s + GUARD_SLACK_S:
        return "ttl"
    return None


def run(env_path=config.ENV_FILE, runner=nethelper.default_runner, mono=time.monotonic, proc_root="/proc",
        force=False, log=nethelper._stderr_log, environ=None):
    try:
        cfg, cfg_ok = config.Config.load(env_path, environ=environ), True
    except ValueError:
        cfg, cfg_ok = config.Config({}), False   # chemins par défaut ; décision : arrêt
    profile = nmkeyfile.profile_path(cfg.nm_dir, nmkeyfile.AP_ID)
    has_profile = os.path.lexists(profile)
    active = netstate.snapshot(runner)["ap_active"]
    if not (active or has_profile or force):
        return {"ok": True, "ap": False, "action": "none"}
    if force:
        reason = "forced"
    else:
        reason = decide(service_state(runner), cfg_ok, bootid.read(proc_root), read_budget(cfg.hotspot_state),
                        mono(), cfg.hotspot_max_s)
    if reason is None:
        return {"ok": True, "ap": True, "action": "none"}
    h = nethelper.Helper(None, None, cfg.nm_dir, os.getuid(), runner=runner, mono=mono, budget_s=GUARD_BUDGET_S,
                         log=log, chown=False, ifname=cfg.wifi_ifname)
    nethelper.stop_ap(h)
    log("ap_guard_stop", reason=reason, active=active, profile=has_profile)
    return {"ok": True, "ap": bool(active or has_profile), "action": "stopped", "reason": reason}
