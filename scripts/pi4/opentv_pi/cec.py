"""HDMI-CEC : découverte seulement (lot MVP). `cec-ctl -d <dev>` sans option n'émet rien et ne configure rien ;
aucune commande d'alimentation, de source ou de volume n'est envoyée."""
import glob
import os
import re

from .nethelper import default_runner

PHYS_RE = re.compile(r"Physical Address\s*:\s*([0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f])", re.I)
DRIVER_RE = re.compile(r"Driver Name\s*:\s*([A-Za-z0-9_.-]{1,32})")
DEV_RE = re.compile(r"cec[0-9]{1,2}")
CEC_TIMEOUT_S = 5   # découverte exécutée hors de la boucle du service (thread), bornée par adaptateur


def discover(root="/", runner=default_runner, max_adapters=4):
    adapters = []
    for p in sorted(glob.glob(os.path.join(root, "dev", "cec*")))[:max_adapters]:
        name = os.path.basename(p)
        if not DEV_RE.fullmatch(name):
            continue
        dev = "/dev/" + name
        rc, out = runner(["cec-ctl", "-d", dev], CEC_TIMEOUT_S)
        phys = PHYS_RE.search(out or "") if rc == 0 else None
        drv = DRIVER_RE.search(out or "") if rc == 0 else None
        pa = phys.group(1).lower() if phys else None
        adapters.append({"device": dev, "readable": rc == 0, "driver": drv.group(1) if drv else None,
                         "physical_address": pa, "tv_detected": bool(pa and pa != "f.f.f.f")})
    return {"adapters": adapters, "discovery_only": True}
