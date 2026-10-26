"""Identité du client TCP loopback (Linux) : UID propriétaire du socket client lu dans /proc/net/tcp.

Sert à réserver la page TV et son API au compte du kiosque : un autre processus local (ex. un agent en lecture seule)
ne peut ni lire la clé de la page TV, ni écrire un avis, ni rouvrir la configuration. Hors Linux (développement),
le contrôle est absent et signalé ; sous Linux, un compte inconnu ou une ligne introuvable sont refusés.
"""
import ipaddress
import os
import socket
import struct


def _hex_addr(ip, port):
    packed = socket.inet_aton(ip)
    return "%08X:%04X" % (struct.unpack("<I", packed)[0], port)


def tcp_peer_uid(client_ip, client_port, server_port, proc_root="/proc"):
    """-> UID du socket (client_ip:client_port -> 127.0.0.1:server_port), ou None."""
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return None
    if ip.version != 4:
        return None
    local = _hex_addr(str(ip), client_port)
    remote_suffix = ":%04X" % server_port
    try:
        with open(os.path.join(proc_root, "net", "tcp"), encoding="ascii", errors="replace") as f:
            lines = f.read(4 * 1024 * 1024).splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        p = line.split()
        if len(p) >= 8 and p[1].upper() == local and p[2].upper().endswith(remote_suffix):
            try:
                return int(p[7])
            except ValueError:
                return None
    return None


def allowed_uids(user, proc_root="/proc"):
    """-> frozenset des UID autorisés, ou None si le contrôle est indisponible (pas de /proc/net/tcp)."""
    if not os.path.exists(os.path.join(proc_root, "net", "tcp")):
        return None
    try:
        import pwd
        return frozenset({pwd.getpwnam(user).pw_uid})
    except (ImportError, KeyError):
        return frozenset()
