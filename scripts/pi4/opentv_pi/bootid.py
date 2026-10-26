"""Identité du démarrage courant (Linux : /proc/sys/kernel/random/boot_id, UUID).

"" = identité inconnue (illisible, malformée, hors Linux) : les appelants sont fail-closed (aucun Wi-Fi temporaire
automatique, garde root ⇒ arrêt), car l'horloge monotone d'un autre démarrage rendrait tout budget faux.
"""
import os
import re

BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def read(proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, "sys", "kernel", "random", "boot_id"), encoding="ascii") as f:
            v = f.read(64).strip()
    except (OSError, ValueError):
        return ""
    return v if BOOT_ID_RE.fullmatch(v) else ""
