"""Profils NetworkManager au format keyfile (magasin officiel, root 0600). Secrets jamais passés en argv."""
import os
import re
import uuid as _uuid

from . import fsutil, validate

WIFI_ID = "opentv-wifi"
# profil candidat d'un wifi-apply : ID borné, autoconnect=false ; promu en opentv-wifi seulement après succès
CAND_ID = "opentv-wifi-candidate"
AP_ID = "opentv-setup-ap"
PROFILE_IDS = (WIFI_ID, CAND_ID, AP_ID)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AP_SSID_RE = re.compile(r"OpenTV-[A-Z0-9]{4}")
AP_PSK_RE = re.compile(r"[a-z2-9]{12,32}")
IFNAME_RE = re.compile(r"[a-z][a-z0-9]{1,14}")


def escape(v):
    """Échappement GKeyFile d'une valeur chaîne (les caractères de contrôle sont refusés en amont)."""
    return (v.replace("\\", "\\\\").replace(" ", "\\s").replace("\n", "\\n").replace("\t", "\\t")
            .replace("\r", "\\r"))


def _render(sections):
    out = []
    for name, kv in sections:
        out.append("[%s]" % name)
        for k, v in kv:
            out.append("%s=%s" % (k, escape(v)))
        out.append("")
    return "\n".join(out)


def _ifname(ifname):
    if not IFNAME_RE.fullmatch(ifname or ""):
        raise ValueError("bad_ifname")
    return ifname


def wifi_profile(ssid, psk, hidden=False, uuid=None, ifname="wlan0", conn_id=WIFI_ID, autoconnect=True):
    ssid, psk = validate.ssid(ssid), validate.psk(psk)
    if conn_id not in (WIFI_ID, CAND_ID):
        raise ValueError("bad_profile")
    uuid = uuid or str(_uuid.uuid4())
    if not UUID_RE.fullmatch(uuid):
        raise ValueError("bad_uuid")
    return _render([
        ("connection", [("id", conn_id), ("uuid", uuid), ("type", "wifi"),
                        ("interface-name", _ifname(ifname)), ("autoconnect", "true" if autoconnect else "false"),
                        ("autoconnect-priority", "10")]),
        ("wifi", [("mode", "infrastructure"), ("ssid", ssid), ("hidden", "true" if hidden else "false")]),
        ("wifi-security", [("key-mgmt", "wpa-psk"), ("psk", psk)]),
        ("ipv4", [("method", "auto")]),
        ("ipv6", [("method", "auto"), ("addr-gen-mode", "default")]),
    ])


def ap_profile(ssid, psk, uuid=None, ifname="wlan0", address="10.42.0.1/24"):
    if not isinstance(ssid, str) or not AP_SSID_RE.fullmatch(ssid):
        raise ValueError("bad_ap_ssid")
    if not isinstance(psk, str) or not AP_PSK_RE.fullmatch(psk):
        raise ValueError("bad_ap_psk")
    return _render([
        ("connection", [("id", AP_ID), ("uuid", uuid or str(_uuid.uuid4())), ("type", "wifi"),
                        ("interface-name", _ifname(ifname)), ("autoconnect", "false")]),
        ("wifi", [("mode", "ap"), ("band", "bg"), ("ssid", ssid)]),
        ("wifi-security", [("key-mgmt", "wpa-psk"), ("psk", psk), ("proto", "rsn"), ("pairwise", "ccmp"),
                           ("group", "ccmp")]),
        ("ipv4", [("method", "shared"), ("address1", address)]),
        ("ipv6", [("method", "disabled")]),
    ])


def profile_path(nm_dir, name):
    if name not in PROFILE_IDS:
        raise ValueError("bad_profile")
    return os.path.join(nm_dir, name + ".nmconnection")


def write_profile(nm_dir, name, text):
    """Écriture atomique 0600 par descripteur (fchmod, aucun chmod par chemin) ; un lien pré-placé est remplacé
    (rename), jamais suivi ; le dossier lui-même n'est jamais un lien."""
    path = profile_path(nm_dir, name)
    fsutil.atomic_write_path(path, text.encode("utf-8"), mode=0o600)
    return path


def read_uuid(nm_dir, name):
    """UUID ([connection] uuid=) d'un profil OpenTV, lu sans suivre de lien. -> str | None."""
    try:
        raw = fsutil.read_regular(profile_path(nm_dir, name), 65536).decode("utf-8", "replace")
    except OSError:
        return None
    section = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "connection" and line.startswith("uuid="):
            v = line[5:].strip().lower()
            return v if UUID_RE.fullmatch(v) else None
    return None


def remove_profile(nm_dir, name):
    try:
        os.remove(profile_path(nm_dir, name))
        return True
    except FileNotFoundError:
        return False
