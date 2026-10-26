"""État réseau en lecture seule (nmcli/ip, argv fixes, sans privilège) -> ethernet | wifi | hotspot | offline,\nplus ap_active (point d'accès OpenTV actif, quel que soit le mode prioritaire)."""
import ipaddress
import re

from .nmkeyfile import AP_ID

NMCLI_STATUS = ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]
IP_ADDR = ["ip", "-4", "-o", "addr", "show"]
IP_LINE_RE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+([0-9.]+)/\d+")
# Budget de la boucle du service (watchdog systemd) : ces deux lectures sont les seules commandes synchrones d'un tick.
NMCLI_TIMEOUT_S = 3
IP_TIMEOUT_S = 2


def _split_terse(line):
    """Découpe une ligne nmcli -t (les « : » littéraux sont échappés en « \\: »)."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parse_device_status(text):
    rows = []
    for line in (text or "").splitlines():
        f = _split_terse(line)
        if len(f) >= 4 and f[0]:
            rows.append({"device": f[0], "type": f[1], "state": f[2], "connection": f[3]})
    return rows


def parse_ipv4(text):
    out = {}
    for line in (text or "").splitlines():
        m = IP_LINE_RE.match(line)
        if not m:
            continue
        try:
            ip = ipaddress.IPv4Address(m.group(2))
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            continue
        out.setdefault(m.group(1), str(ip))
    return out


def snapshot(runner):
    rc1, dev = runner(NMCLI_STATUS, NMCLI_TIMEOUT_S)
    rc2, ip = runner(IP_ADDR, IP_TIMEOUT_S)
    devs = parse_device_status(dev if rc1 == 0 else "")
    ips = parse_ipv4(ip if rc2 == 0 else "")
    eth = [d for d in devs if d["type"] == "ethernet" and d["state"].startswith("connected") and d["device"] in ips]
    wifi = [d for d in devs if d["type"] == "wifi"]
    sta = [d for d in wifi if d["state"].startswith("connected") and d["connection"] != AP_ID and d["device"] in ips]
    ap = [d for d in wifi if d["state"].startswith("connected") and d["connection"] == AP_ID]
    if eth:
        mode, ipaddr = "ethernet", ips[eth[0]["device"]]
    elif sta:
        mode, ipaddr = "wifi", ips[sta[0]["device"]]
    elif ap:
        mode, ipaddr = "hotspot", ips.get(ap[0]["device"], "10.42.0.1")
    else:
        mode, ipaddr = "offline", None
    # ap_active : le point d'accès OpenTV est détecté même quand Ethernet/Wi-Fi est prioritaire (il ne doit jamais
    # rester diffusé derrière une autre connexion)
    return {"mode": mode, "ip": ipaddr, "wifi_present": bool(wifi), "nm_ok": rc1 == 0, "ap_active": bool(ap)}
