"""Commande de santé : manifeste SHA-256 des fichiers installés, unités systemd, service HTTP loopback, réseau."""
import hashlib
import http.client
import json
import os
import re

from . import netstate
from .nethelper import default_runner

MANIFEST = "/opt/opentv/MANIFEST.sha256"
LINE_RE = re.compile(r"([0-9a-f]{64})  (/[^\n]+)")
UNITS_REQUIRED = ("opentv.service", "opentv-net-helper.path", "opentv-ap-guard.timer")
UNITS_OPTIONAL = ("opentv-kiosk.service",)


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


def run(cfg, root="/", runner=default_runner, check_http=True):
    units = {}
    for u in UNITS_REQUIRED + UNITS_OPTIONAL:
        rc, out = runner(["systemctl", "is-active", u], 10)
        units[u] = (out or "").strip() or "inconnu"
    man = verify_manifest(root)
    service = http_ok(cfg.tv_port) if check_http else None
    net = netstate.snapshot(runner)
    ok = man["ok"] and all(units[u] == "active" for u in UNITS_REQUIRED) and service is not False
    return {"ok": ok, "manifest": man, "units": units, "service_http": service,
            "network": {"mode": net["mode"]}, "kiosk": units.get("opentv-kiosk.service")}
