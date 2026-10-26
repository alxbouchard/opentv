"""Préflight strict Raspberry Pi 4 / Raspberry Pi OS Bookworm 64-bit (lecture seule, racine injectable pour les tests).

fail = bloquant (installation refusée) ; warn = fonction optionnelle indisponible (CEC, hotspot…) ;
dep = dépendance absente que prepare-pi.sh installe (étape « hardware » seulement, avant apt).

Étapes : « hardware » (prepare-pi.sh, AVANT tout apt : modèle Pi 4, Raspberry Pi OS Bookworm, arm64, KMS, DRM,
cmdline.txt) et « full » (install.sh : tout, dépendances comprises). Aucun périphérique ni flag Rockchip n'est consulté.
"""
import glob
import os
import re
import sys

from .nethelper import default_runner

BIN_DIRS = ("usr/bin", "usr/sbin", "bin", "sbin", "usr/local/bin")
REQUIRED_BINS = ("nmcli", "cage", "systemctl")
PACKAGES = {"nmcli": "network-manager", "cage": "cage", "systemctl": "systemd", "chromium": "chromium"}
STAGES = ("full", "hardware")
OPTIONAL_BINS = {"cec-ctl": "découverte CEC (paquet v4l-utils)", "wlr-randr": "forçage 1920×1080 dans Cage",
                 "dnsmasq": "hotspot de configuration (dnsmasq-base)"}


def _p(root, path):
    return os.path.join(root, path.lstrip("/"))


def _read(root, path, cap=65536):
    try:
        with open(_p(root, path), encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return None


def which(root, name):
    for d in BIN_DIRS:
        p = os.path.join(root, d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return "/" + os.path.join(d, name)
    return None


def os_release(text):
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"')
    return out


def run(root="/", runner=default_runner, pyver=None, assume_arch=None, assume_nm_active=False, stage="full"):
    if stage not in STAGES:
        raise ValueError("bad_stage")
    checks, missing = [], []

    def add(cid, status, detail):
        checks.append({"id": cid, "status": status, "detail": detail})

    def dep(cid, path, name):
        if path:
            add(cid, "ok", path)
            return
        missing.append(PACKAGES[name])
        add(cid, "dep" if stage == "hardware" else "fail",
            "%s introuvable (paquet %s, installé par prepare-pi.sh)" % (name, PACKAGES[name]))

    model = (_read(root, "/proc/device-tree/model") or "").replace("\0", "").strip()
    platform = None
    if model.startswith("Raspberry Pi 4 Model B"):
        platform = "pi4"
        add("model", "ok", model)
    elif model.startswith("Raspberry Pi 5"):
        add("model", "fail", "%s : Pi 5 prévu plus tard, non pris en charge par ce lot" % model)
    else:
        add("model", "fail", "modèle non pris en charge : %s" % (model or "inconnu"))

    osr = os_release(_read(root, "/etc/os-release"))
    if osr.get("VERSION_CODENAME") == "bookworm" and osr.get("ID") in ("debian", "raspbian"):
        add("os", "ok", osr.get("PRETTY_NAME", "bookworm"))
    else:
        add("os", "fail", "Raspberry Pi OS Bookworm requis (trouvé : %s)" % (osr.get("PRETTY_NAME") or "inconnu"))
    add("rpi_os", "ok" if os.path.exists(_p(root, "/etc/rpi-issue")) else "fail",
        "Raspberry Pi OS (/etc/rpi-issue)")

    if assume_arch is not None:
        arch = assume_arch
    else:
        rc, out = runner(["dpkg", "--print-architecture"], 10)
        arch = out.strip() if rc == 0 else "inconnue"
    add("arch", "ok" if arch == "arm64" else "fail", "espace utilisateur %s (arm64 requis, pas de 32 bits)" % arch)

    v = pyver or sys.version_info
    add("python", "ok" if tuple(v[:2]) >= (3, 11) else "fail", "Python %d.%d (3.11+ requis)" % (v[0], v[1]))

    cfgtxt = _read(root, "/boot/firmware/config.txt")
    kms = bool(cfgtxt) and any(re.match(r"^\s*dtoverlay=vc4-kms-v3d(?:[,\s]|$)", l) for l in cfgtxt.splitlines())
    add("kms", "ok" if kms else "fail", "dtoverlay=vc4-kms-v3d actif dans /boot/firmware/config.txt (KMS officiel)")
    hdmi = sorted(glob.glob(_p(root, "/sys/class/drm/card*-HDMI-A-*")))
    cards = glob.glob(_p(root, "/dev/dri/card*"))
    add("drm", "ok" if hdmi and cards else "fail", "connecteurs DRM HDMI : %d, nœuds /dev/dri/card* : %d"
        % (len(hdmi), len(cards)))
    add("drm_driver", "ok" if os.path.isdir(_p(root, "/sys/module/vc4")) else "fail", "pilote vc4 chargé")
    connected = [h for h in hdmi if (_read(root, os.path.join(h[len(root.rstrip("/")):], "status")) or "").strip()
                 == "connected"]
    add("hdmi_connected", "ok" if connected else "warn", "écran HDMI détecté" if connected else
        "aucun écran HDMI détecté (TV éteinte ou câble ?)")
    add("cmdline", "ok" if _read(root, "/boot/firmware/cmdline.txt") else "fail",
        "/boot/firmware/cmdline.txt (1080p forcé par install.sh : video=HDMI-A-1:1920x1080@60)")

    if stage == "hardware":
        for b in REQUIRED_BINS:
            dep("bin_" + b, which(root, b), b)
        dep("bin_chromium", which(root, "chromium") or which(root, "chromium-browser"), "chromium")
        ok = not any(c["status"] == "fail" for c in checks)
        return {"ok": ok, "stage": stage, "platform": platform if ok else None, "checks": checks,
                "missing_packages": sorted(set(missing))}

    if assume_nm_active:
        nm = "active"
    else:
        rc, out = runner(["systemctl", "is-active", "NetworkManager"], 10)
        nm = out.strip() or "inconnu"
    add("networkmanager", "ok" if nm == "active" else "fail", "NetworkManager : %s" % nm)

    for b in REQUIRED_BINS:
        dep("bin_" + b, which(root, b), b)
    dep("bin_chromium", which(root, "chromium") or which(root, "chromium-browser"), "chromium")
    for b, why in OPTIONAL_BINS.items():
        p = which(root, b)
        add("bin_" + b, "ok" if p else "warn", p or "%s absent : %s" % (b, why))

    cecs = sorted(glob.glob(_p(root, "/dev/cec*")))
    add("cec", "ok" if cecs else "warn", "%d adaptateur(s) /dev/cec*" % len(cecs) if cecs else
        "aucun /dev/cec* : CEC indisponible")
    cmdline = _read(root, "/proc/cmdline") or ""
    add("wifi_country", "ok" if "cfg80211.ieee80211_regdom=" in cmdline else "warn",
        "pays Wi-Fi défini" if "cfg80211.ieee80211_regdom=" in cmdline else
        "pays Wi-Fi non défini : le régler dans Raspberry Pi Imager (sinon Wi-Fi/hotspot bloqués)")
    blocked = []
    for d in glob.glob(_p(root, "/sys/class/rfkill/rfkill*")):
        if (_read(root, os.path.join(d[len(root.rstrip("/")):], "type")) or "").strip() == "wlan" and \
                (_read(root, os.path.join(d[len(root.rstrip("/")):], "soft")) or "").strip() == "1":
            blocked.append(os.path.basename(d))
    add("rfkill", "warn" if blocked else "ok", "Wi-Fi bloqué par rfkill" if blocked else "Wi-Fi non bloqué")

    ok = not any(c["status"] == "fail" for c in checks)
    return {"ok": ok, "stage": stage, "platform": platform if ok else None, "checks": checks,
            "missing_packages": sorted(set(missing))}
