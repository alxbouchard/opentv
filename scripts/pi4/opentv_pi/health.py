"""Commande de santé : manifeste SHA-256 des fichiers installés, unités systemd, service HTTP loopback, réseau."""
import hashlib
import http.client
import json
import os
import re

from . import netstate, ratings
from .nethelper import default_runner

MANIFEST = "/opt/opentv/MANIFEST.sha256"
LINE_RE = re.compile(r"([0-9a-f]{64})  (/[^\n]+)")
UNITS_REQUIRED = ("opentv.service", "opentv-net-helper.path", "opentv-ap-guard.timer")
UNITS_OPTIONAL = ("opentv-kiosk.service", "opentv-cec.timer", "opentv-cec-return.service",
                  "opentv-catalogue-refresh.timer",
                  "opentv-catalogue-ratings.service", "opentv-catalogue-ratings-retry.timer",
                  "opentv-catalogue-ratings-retry.service")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest(root="/", manifest=MANIFEST):
    res = {"ok": False, "checked": 0, "missing": [], "mismatch": [], "error": None}
    try:
        with open(os.path.join(root, manifest.lstrip("/")), encoding="utf-8") as f:
            lines = f.read(1024 * 1024).splitlines()
    except OSError:
        res["error"] = "manifest_missing"
        return res
    for line in lines:
        m = LINE_RE.fullmatch(line.strip())
        if not m:
            continue
        p = os.path.join(root, m.group(2).lstrip("/"))
        res["checked"] += 1
        if not os.path.isfile(p):
            res["missing"].append(m.group(2))
        elif sha256_file(p) != m.group(1):
            res["mismatch"].append(m.group(2))
    res["ok"] = res["checked"] > 0 and not res["missing"] and not res["mismatch"]
    return res


def http_ok(port, timeout=3.0):
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/healthz", headers={"Host": "127.0.0.1:%d" % port})
        r = c.getresponse()
        body = r.read(4096)
        c.close()
        return r.status == 200 and json.loads(body.decode("utf-8")).get("ok") is True
    except (OSError, ValueError):
        return False


def _read_text(root, path, limit=65536):
    try:
        with open(os.path.join(root, path.lstrip("/")), "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def plymouth_refused(words):
    """Plymouth refusé sur la ligne noyau : nosplash, plymouth.enable / rd.plymouth = 0|false|no|off (respecté)."""
    for w in words:
        if w == "nosplash":
            return True
        k, sep, v = w.partition("=")
        if sep and k in ("plymouth.enable", "rd.plymouth") and v.lower() in ("0", "n", "no", "false", "off"):
            return True
    return False


def boot_splash(root="/"):
    """Image de démarrage, en lecture seule (n'influe jamais sur « ok ») : arc-en-ciel du firmware (dernière valeur
    effective de disable_splash dans les sections toujours appliquées à la Pi 4), thème Plymouth sélectionné, et mode
    attendu au prochain démarrage (Plymouth si « splash » est dans cmdline.txt, sinon écran texte tty1)."""
    cfg = _read_text(root, "/boot/firmware/config.txt")
    rainbow = "unknown"
    if cfg is not None:
        sec, val, local = "all", None, False
        for line in cfg.splitlines():
            s = line.strip()
            if s.startswith("[") and "]" in s:
                sec = "on" if s[1:s.index("]")].lower() in ("all", "pi4") else "cond"
            elif re.match(r"disable_splash\s*=", s):
                v = s.split("=", 1)[1].strip()
                if sec != "cond":
                    val = v
                local = local or v != "1"
        rainbow = "disabled" if val == "1" and not local else "shown"
    theme, sec = None, ""
    for line in (_read_text(root, "/etc/plymouth/plymouthd.conf") or "").splitlines():
        s = line.strip()
        if s.startswith("["):
            sec = s
        elif sec == "[Daemon]" and re.match(r"Theme\s*=", s):
            theme = s.split("=", 1)[1].strip()
    cmd = (_read_text(root, "/boot/firmware/cmdline.txt") or "").split("\n", 1)[0].split()
    splash = "splash" in cmd and not plymouth_refused(cmd)
    return {"firmware_rainbow": rainbow, "plymouth_theme": theme or "default",
            "next_boot": "plymouth" if splash else "text",
            "plymouth_ready": splash and theme == "opentv",
            "theme_installed": os.path.isfile(os.path.join(root, "usr/share/plymouth/themes/opentv/opentv.plymouth"))}


def run(cfg, root="/", runner=default_runner, check_http=True):
    units = {}
    for u in UNITS_REQUIRED + UNITS_OPTIONAL:
        rc, out = runner(["systemctl", "is-active", u], 10)
        units[u] = (out or "").strip() or "inconnu"
    man = verify_manifest(root)
    service = http_ok(cfg.tv_port) if check_http else None
    net = netstate.snapshot(runner)
    ok = man["ok"] and all(units[u] == "active" for u in UNITS_REQUIRED) and service is not False
    # notes TMDb : enrichissement facultatif (n'influe pas sur « ok » de l'appareil) mais un échec reste visible
    rat = ratings.health_view(cfg, runner=runner)
    warnings = []
    if rat.get("ok") is False or units.get(ratings.UNIT) == "failed" or units.get(ratings.RETRY_UNIT) == "failed":
        warnings.append("ratings_enrichment_failed")
    if (rat.get("retry") or {}).get("state") == "retry_exhausted":
        warnings.append("ratings_retry_exhausted")
    if rat.get("exposed") or ("exposed" in rat and rat["exposed"] is None):
        # jeton présent mais une unité du compte tourne sans le masque de son dossier (ou invérifiable)
        warnings.append("ratings_credential_exposed")
    return {"ok": ok, "manifest": man, "units": units, "service_http": service,
            "network": {"mode": net["mode"]}, "kiosk": units.get("opentv-kiosk.service"),
            "boot_splash": boot_splash(root), "ratings": rat, "warnings": warnings}
