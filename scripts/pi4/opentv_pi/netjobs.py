"""Côté service (utilisateur opentv, sans privilège) : dépôt d'un job réseau 0600 et lecture des résultats.

Le job est écrit dans un dossier temporaire du même système de fichiers puis renommé dans le dossier surveillé
par opentv-net-helper.path : le helper ne voit jamais un fichier partiel.
"""
import json
import os
import re
import secrets
import string
import time

from . import validate, nmkeyfile

ACTIONS = ("wifi-apply", "ap-start", "ap-stop")
JOB_MAX_BYTES = 4096
_ALNUM = string.ascii_lowercase + string.digits
ID_RE = re.compile(r"[a-z0-9]{8,32}")


def new_id():
    return "".join(secrets.choice(_ALNUM) for _ in range(16))


def check_payload(action, p):
    """Validation partagée service/helper. -> payload normalisé ; ValueError sinon."""
    if action not in ACTIONS or not isinstance(p, dict):
        raise ValueError("bad_action")
    if action == "wifi-apply":
        allowed = {"ssid", "psk", "hidden", "delay_s", "restore_ap"}
        if set(p) - allowed:
            raise ValueError("bad_fields")
        hidden, restore = p.get("hidden", False), p.get("restore_ap", False)
        if not isinstance(hidden, bool) or not isinstance(restore, bool):
            raise ValueError("bad_fields")
        return {"ssid": validate.ssid(p.get("ssid")), "psk": validate.psk(p.get("psk")), "hidden": hidden,
                "delay_s": validate.bounded_int(p.get("delay_s", 0), 0, 10, "delay"), "restore_ap": restore}
    if action == "ap-start":
        if set(p) - {"ssid", "psk"}:
            raise ValueError("bad_fields")
        if not nmkeyfile.AP_SSID_RE.fullmatch(p.get("ssid") or "") or \
                not nmkeyfile.AP_PSK_RE.fullmatch(p.get("psk") or ""):
            raise ValueError("bad_ap")
        return {"ssid": p["ssid"], "psk": p["psk"]}
    if p:
        raise ValueError("bad_fields")
    return {}


def submit(jobs_dir, tmp_dir, action, payload, clock=time.time):
    payload = check_payload(action, payload)
    jid = new_id()
    body = json.dumps({"v": 1, "id": jid, "action": action, "created": clock(), "payload": payload},
                      sort_keys=True).encode("utf-8")
    if len(body) > JOB_MAX_BYTES:
        raise ValueError("too_big")
    tmp = os.path.join(tmp_dir, jid + ".json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, body)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, os.path.join(jobs_dir, jid + ".json"))
    return jid


def withdraw(jobs_dir, jid):
    """Retire un job encore en file (échéance du service) : le helper, qui supprime avant d'agir, ne l'exécutera
    jamais. -> True si retiré, False s'il était déjà pris (ou absent)."""
    if not ID_RE.fullmatch(jid or ""):
        return False
    try:
        os.remove(os.path.join(jobs_dir, jid + ".json"))
        return True
    except OSError:
        return False


def read_results(results_dir, cap=20):
    """Résultats déposés par le helper (sans secret) ; chaque fichier est lu une fois puis supprimé."""
    out = []
    try:
        names = sorted(n for n in os.listdir(results_dir) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return out
    for n in names[:cap]:
        p = os.path.join(results_dir, n)
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(fd, JOB_MAX_BYTES)
            finally:
                os.close(fd)
            r = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            r = None
        try:
            os.remove(p)
        except OSError:
            pass
        if isinstance(r, dict) and r.get("action") in ACTIONS:
            out.append({"id": str(r.get("id"))[:32], "action": r["action"], "ok": r.get("ok") is True,
                        "error": r.get("error") if isinstance(r.get("error"), str) else None})
    return out
