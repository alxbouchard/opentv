"""opentv-pi : serve | preflight | health | net-helper | ap-guard | agent issue|list|revoke | setup reset | cec |
cec-bootstrap | manifest verify."""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

from . import VERSION, agents, apguard, cec, cecboot, config, fsutil, health, nethelper, preflight

# Boucle du service : un tick (au pire netstate.NMCLI_TIMEOUT_S + IP_TIMEOUT_S, CEC en thread) puis cette attente.
# Le test de budget vérifie que (tick + attente) × 2 ≤ WatchdogSec de opentv.service.
LOOP_INTERVAL_S = 2.0
# propriétaires admis pour chaque composant du dossier des fichiers clients (root seulement en production)
CLIENT_DIR_TRUSTED_UIDS = (0,)


def log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def sd_notify(msg):
    """Protocole sd_notify (stdlib) : READY=1, WATCHDOG=1, STOPPING=1. Sans NOTIFY_SOCKET : rien."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or addr[0] not in "@/":
        return False
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(addr)
        s.sendall(msg.encode("ascii"))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    return code


def cmd_serve(a):
    from . import agentport, web
    from .app import App
    cfg = config.Config.load(a.env)
    cfg.ensure_dirs()
    app = App(cfg, log=log)
    servers = web.WebServers(app)
    servers.start_tv()
    gid = None
    try:
        import grp
        gid = grp.getgrnam(cfg.agent_group).gr_gid
    except (ImportError, KeyError):
        log("agent_group_missing", group=cfg.agent_group)
    svc = agentport.AgentService(app, app.registry, app.audit, log=log)
    port = agentport.AgentPortServer(cfg.agent_socket, svc.handle, gid=gid)
    threading.Thread(target=port.serve_forever, daemon=True, name="agent-port").start()
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    log("started", version=VERSION, tv_port=servers.tv_port(), setup=app.setup_mode)
    sd_notify("READY=1")
    while not stop.is_set():
        try:
            app.tick()
            servers.sync_lan()
        except Exception as e:   # la boucle survit ; systemd redémarre si le watchdog n'est plus nourri
            log("tick_error", error=type(e).__name__)
        sd_notify("WATCHDOG=1")
        stop.wait(LOOP_INTERVAL_S)
    sd_notify("STOPPING=1")
    servers.stop()
    port.shutdown()
    port.server_close()
    app.close()
    log("stopped")
    return 0


def cmd_preflight(a):
    if a.root == "/" and (a.assume_arch or a.assume_nm_active):
        return _out({"ok": False, "error": "assume_flags_only_with_test_root"}, 2)
    rep = preflight.run(a.root, assume_arch=a.assume_arch, assume_nm_active=a.assume_nm_active, stage=a.stage)
    if a.text:
        for c in rep["checks"]:
            sys.stdout.write("[%-4s] %-16s %s\n" % (c["status"], c["id"], c["detail"]))
        if rep["missing_packages"]:
            sys.stdout.write("dépendances absentes (installées par prepare) : %s\n" % " ".join(rep["missing_packages"]))
        sys.stdout.write("préflight %s : %s\n" % ("matériel" if a.stage == "hardware" else "complet",
                                                   "OK" if rep["ok"] else "ÉCHEC"))
        return 0 if rep["ok"] else 1
    return _out(rep, 0 if rep["ok"] else 1)


def cmd_health(a):
    cfg = config.Config.load(a.env)
    rep = health.run(cfg, root=a.root, check_http=not a.no_http)
    return _out(rep, 0 if rep["ok"] else 1)


def cmd_net_helper(a):
    cfg = config.Config.load(a.env)
    if a.no_chown:
        owner = os.getuid() if a.owner_uid is None else a.owner_uid
    else:
        if os.geteuid() != 0:
            return _out({"ok": False, "error": "root_required"}, 2)
        import pwd
        try:
            owner = pwd.getpwnam(cfg.service_user).pw_uid
        except KeyError:
            return _out({"ok": False, "error": "service_user_missing"}, 2)
    h = nethelper.Helper(a.jobs or cfg.jobs_dir, a.results or cfg.results_dir, a.nm_dir or cfg.nm_dir, owner,
                         chown=not a.no_chown, ifname=cfg.wifi_ifname)
    res = h.run_once()
    return _out({"ok": True, "processed": len(res), "failed": sum(1 for r in res if not r["ok"])})


def cmd_ap_guard(a):
    """(root) Garde du Wi-Fi temporaire : arrête opentv-setup-ap si le service est inactif ou le budget dépassé."""
    if os.geteuid() != 0:
        return _out({"ok": False, "error": "root_required"}, 2)
    return _out(apguard.run(a.env, force=a.force_stop))


def _owner_ids(name):
    import pwd
    pw = pwd.getpwnam(name)
    return pw.pw_uid, pw.pw_gid, pw.pw_dir


def _private_group(pw):
    """Le groupe principal du compte ne contient que lui (sinon un fichier root:<gid> 0640 fuirait le jeton)."""
    import grp
    import pwd
    try:
        g = grp.getgrgid(pw.pw_gid)
    except KeyError:
        return False
    if any(m != pw.pw_name for m in g.gr_mem):
        return False
    return all(p.pw_gid != pw.pw_gid or p.pw_uid == pw.pw_uid for p in pwd.getpwall())


def _issue_root(a, cfg, reg, ids, scopes):
    """Émission par root : le fichier client va dans cfg.agent_clients_dir (root:root 0755, vérifié composant par
    composant), jamais dans le home du compte. Fichier root:<groupe privé> 0640 (root:root 0600 pour root) : lisible
    par l'agent, non modifiable. Toute validation précède l'émission ; un échec d'écriture rétablit le registre."""
    import pwd
    if a.out:
        return _out({"ok": False, "error": "out_forbidden_as_root",
                     "note": "Sous root, le fichier client est toujours créé dans %s." % cfg.agent_clients_dir}, 2)
    try:
        pw = pwd.getpwnam(a.owner or "root")
    except KeyError:
        return _out({"ok": False, "error": "owner_missing"}, 2)
    if pw.pw_uid == 0:
        gid, mode = 0, 0o600
    elif _private_group(pw):
        gid, mode = pw.pw_gid, 0o640
    else:
        return _out({"ok": False, "error": "owner_group_not_private"}, 2)
    name = agents.client_file_name(a.id, pw.pw_uid)
    try:
        dfd = fsutil.open_trusted_dir(cfg.agent_clients_dir, CLIENT_DIR_TRUSTED_UIDS)
    except OSError:
        return _out({"ok": False, "error": "clients_dir_unsafe",
                     "note": "%s doit exister, root:root 0755 (systemd-tmpfiles --create)." % cfg.agent_clients_dir},
                    2)
    try:
        prev = []
        try:
            tok = reg.issue(a.id, scopes, force=a.force, previous=prev, **ids)
        except ValueError as e:
            return _out({"ok": False, "error": str(e)}, 2)
        except OSError as e:
            return _out({"ok": False, "error": _registry_os_error(e)}, 2)
        try:
            fsutil.atomic_write(dfd, name, agents.client_config_bytes(a.id, tok, cfg.agent_socket), mode=mode,
                                uid=0, gid=gid)
        except OSError:
            return _out(dict({"ok": False, "error": "client_write_failed"},
                             **_rollback(reg, a.id, tok, prev, ids)), 2)
        finally:
            tok = None
    finally:
        os.close(dfd)
    path = os.path.join(cfg.agent_clients_dir, name)
    return _out({"ok": True, "agent": a.id, "scopes": sorted(set(scopes)), "config": path, "owner": pw.pw_name,
                 "mcp": {"command": "/opt/opentv/bin/opentv-mcp", "args": ["--config", path]},
                 "note": "Le compte %s doit être membre du groupe %s (sudo gpasswd -a %s %s) pour joindre le socket."
                         % (pw.pw_name, cfg.agent_group, pw.pw_name, cfg.agent_group)})


def _registry_os_error(e):
    return "registry_busy" if isinstance(e, agents.RegistryBusy) else "registry_write_failed"


def _rollback(reg, agent_id, tok, prev, ids):
    """Retour arrière d'une émission dont le fichier client n'a pu être écrit ; un registre devenu illisible ou
    verrouillé n'est jamais forcé : l'échec est rapporté (le jeton émis n'a été écrit nulle part ailleurs)."""
    try:
        reg.rollback_issue(agent_id, tok, prev[0] if prev else None, **ids)
        return {}
    except ValueError as e:
        return {"rollback": str(e)}
    except OSError as e:
        return {"rollback": _registry_os_error(e)}


def cmd_agent(a):
    cfg = config.Config.load(a.env)
    reg = agents.Registry(cfg.registry_file)
    ids = {"uid": None, "gid": None}   # root : le registre reste la propriété de l'utilisateur du service
    root = os.geteuid() == 0
    if root:
        try:
            ids["uid"], ids["gid"] = _owner_ids(cfg.service_user)[:2]
        except KeyError:
            return _out({"ok": False, "error": "service_user_missing"}, 2)
    if a.action == "list":
        return _out({"ok": True, "agents": reg.list(), "policy": reg.policy()})
    if a.action == "revoke":
        try:
            gone = reg.revoke(a.id, **ids)
        except ValueError as e:   # registre illisible : jamais remplacé par un registre vide
            return _out({"ok": False, "error": str(e)}, 2)
        except OSError as e:
            return _out({"ok": False, "error": _registry_os_error(e)}, 2)
        return _out({"ok": True, "revoked": gone})
    scopes = [s for s in (a.scopes or "").split(",") if s]
    if root:
        return _issue_root(a, cfg, reg, ids, scopes)
    # sans privilège : fichier dans le home de l'utilisateur courant seulement (aucun autre compte)
    if a.owner:
        try:
            owner_uid = _owner_ids(a.owner)[0]
        except KeyError:
            return _out({"ok": False, "error": "owner_missing"}, 2)
        if owner_uid != os.geteuid():
            return _out({"ok": False, "error": "root_required_for_owner"}, 2)
    out = a.out or os.path.join(os.path.expanduser("~"), ".config", "opentv", "agent-%s.json" % a.id)
    prev = []
    try:
        tok = reg.issue(a.id, scopes, force=a.force, previous=prev)
    except ValueError as e:
        return _out({"ok": False, "error": str(e)}, 2)
    except OSError as e:
        return _out({"ok": False, "error": _registry_os_error(e)}, 2)
    try:
        os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
        agents.write_client_config(out, a.id, tok, cfg.agent_socket)
    except OSError:
        return _out(dict({"ok": False, "error": "client_write_failed"}, **_rollback(reg, a.id, tok, prev, ids)), 2)
    finally:
        tok = None
    return _out({"ok": True, "agent": a.id, "scopes": sorted(set(scopes)), "config": out,
                 "mcp": {"command": "/opt/opentv/bin/opentv-mcp", "args": ["--config", out]},
                 "note": "Le compte courant doit être membre du groupe %s (sudo gpasswd -a $USER %s) pour joindre "
                         "le socket." % (cfg.agent_group, cfg.agent_group)})


def cmd_setup(a):
    """Réaffiche le QR et réarme explicitement le Wi-Fi temporaire (budget OPENTV_HOTSPOT_MAX_S neuf).

    Sous root, dans un dossier appartenant au compte du service : ouverture O_NOFOLLOW|O_NONBLOCK relative au dossier,
    fichier régulier à lien unique exigé, propriétaire/mode fixés par fchown/fchmod sur le descripteur."""
    cfg = config.Config.load(a.env)
    uid = gid = owners = None
    if os.geteuid() == 0:
        try:
            uid, gid, _ = _owner_ids(cfg.service_user)
            owners = (0, uid)
        except KeyError:
            owners = (0,)
    for path in (cfg.setup_flag, cfg.hotspot_rearm):
        try:
            fsutil.touch_regular(path, 0o600, uid=uid, gid=gid, allowed_owners=owners)
        except OSError as e:
            return _out({"ok": False, "error": "unsafe_path", "path": os.path.basename(path),
                         "reason": getattr(e, "reason", "os_error")}, 2)
    return _out({"ok": True, "setup": "requested", "hotspot": "rearmed"})


CEC_ANNOUNCE_UNIT = "opentv-cec-announce.service"
CEC_ACTIVATE_TIMEOUT_S = 75   # > TimeoutStartSec=60 de l'unité : systemctl attend la fin du oneshot
CEC_BUSY_EXIT = 75            # EX_TEMPFAIL : réactivation refusée, un passage de la minuterie tenait le verrou
CEC_STATUS_TIMEOUT_S = 5
CEC_RETRY_NOTE = ("Un passage CEC automatique était en cours : réessayer dans une minute avec "
                  "sudo /opt/opentv/bin/opentv-pi cec --activate")


def _announce_unit_busy():
    """Le dernier échec de l'unité de réactivation vient-il du verrou ? Seul l'entier ExecMainStatus est lu (argv
    fixe, borné) ; aucune sortie de systemctl n'est rapportée."""
    rc, out = nethelper.default_runner(["systemctl", "show", "--property=ExecMainStatus", "--value",
                                        CEC_ANNOUNCE_UNIT], CEC_STATUS_TIMEOUT_S)
    v = (out or "").strip()
    return rc == 0 and v.isdigit() and len(v) <= 3 and int(v) == CEC_BUSY_EXIT


def cmd_cec(a):
    """État CEC vérifiable sans émission : adaptateurs, configuration OpenTV, annonce de ce démarrage, touche OK.

    --activate (root) : réactivation explicite — démarre opentv-cec-announce.service (compte opentv, TV allumée
    seulement, jamais de réveil, ignore le témoin de démarrage), puis relit l'état. Le code de sortie de systemctl ne
    suffit pas (unité sautée sans /dev/cec*, amorçage désactivé, HDMI débranché, configuration étrangère… sortent en
    0) : la réactivation n'est réussie que si le témoin du démarrage courant (même boot_id) a été réécrit pendant
    l'appel, ce que cecboot ne fait qu'après une <Active Source> transmise. Aucune sortie brute de systemctl."""
    state_dir = os.path.join(a.root, cecboot.STATE_DIR.lstrip("/"))
    proc_root = os.path.join(a.root, "proc")
    activation = None
    if getattr(a, "activate", False):
        if os.geteuid() != 0:
            return _out({"ok": False, "error": "root_required",
                         "note": "sudo /opt/opentv/bin/opentv-pi cec --activate"}, 2)
        boot = cecboot.bootid.read(proc_root)
        _, before = cecboot.marker_stamp(state_dir, boot)
        rc, _ = nethelper.default_runner(["systemctl", "start", CEC_ANNOUNCE_UNIT], CEC_ACTIVATE_TIMEOUT_S)
        _, after = cecboot.marker_stamp(state_dir, boot)
        announced = bool(boot) and boot == cecboot.bootid.read(proc_root) and after is not None and after != before
        activation = {"ok": rc == 0 and announced, "unit": CEC_ANNOUNCE_UNIT, "service_ok": rc == 0,
                      "announced": announced}
        if rc == 0 and not announced:
            activation["result"] = "no_announcement" if boot else "boot_unknown"
        elif rc != 0 and _announce_unit_busy():
            activation["result"] = "busy"
    rep = cec.discover(a.root)
    rep["bootstrap"] = cecboot.load_announced(state_dir, cecboot.bootid.read(proc_root))
    km = cecboot.KernelKeymap(a.root)
    for ad in rep["adapters"]:
        ad["announced_this_boot"] = rep["bootstrap"].get(ad["device"]) == ad.get("physical_address")
        if ad.get("readable") and (ad.get("driver") or "").startswith("vc4"):
            ev = cecboot.find_input(a.root, ad.get("adapter"))
            try:
                code = km.get(ev, 0x00) if ev else None
                ad["select_key"] = {cecboot.KEY_ENTER: "enter", cecboot.KEY_OK: "kernel_default_ok"}.get(
                    code, "custom" if ev else "no_input_device")
            except OSError:
                ad["select_key"] = "unreadable"
    if activation is not None:
        rep["activation"] = activation
        if not activation["ok"]:
            rep["note"] = (CEC_RETRY_NOTE if activation.get("result") == "busy"
                           else "journalctl -b -u " + CEC_ANNOUNCE_UNIT + " -n 20 --no-pager")
            return _out(rep, 1)
    return _out(rep)


def _line(obj, code=0):
    """Une ligne JSON compacte (journal de la minuterie) ; uniquement des champs construits par cecboot."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return code


def cmd_cec_bootstrap(a):
    """Amorçage CEC (opentv-cec.service, compte opentv) ou retour arrière --release (root, désinstallation)."""
    if a.release:
        res = cecboot.release(a.root)
        return _line(dict(res, event="cec_release"), 0 if res["ok"] else 1)
    if os.geteuid() == 0:   # root n'écrit jamais dans /run/opentv-cec (dossier du compte opentv)
        unit = "opentv-cec-announce.service" if a.force_announce else "opentv-cec.service"
        return _line({"event": "cec_bootstrap", "ok": False, "error": "run_via_systemd",
                      "note": "sudo systemctl start " + unit}, 2)
    cfg = config.Config.load(a.env)
    if not cfg.cec_bootstrap:
        return _line({"event": "cec_bootstrap", "ok": True, "result": "disabled"})
    res = cecboot.bootstrap(a.root, state_dir=a.state_dir, keymap=cfg.cec_keymap, force_announce=a.force_announce,
                            boot_announce=cfg.cec_boot_announce)
    code = 0 if res["ok"] else (CEC_BUSY_EXIT if res.get("result") == "busy" else 1)
    return _line(dict(res, event="cec_bootstrap"), code)


def _uids(*users):
    import pwd
    out = {0}
    for u in users:
        try:
            out.add(pwd.getpwnam(u).pw_uid)
        except KeyError:
            log("player_user_missing", user=u)
    return out


def cmd_player_daemon(a):
    """Contrôleur du lecteur (opentv-kiosk, SANS groupe input) : socket réservé au service OpenTV et à root ;
    touches de fermeture reçues de l'aide opentv-player-keys (UID vérifié) ; porte de promotion relue à chaque open."""
    from . import playerd, promotion
    from .playerkeys import ping as keys_ping
    cfg = config.Config.load(a.env)
    uids = _uids(cfg.service_user)
    keys_uids = _uids(cfg.player_keys_user)
    output = cfg.raw.get("OPENTV_HDMI_OUTPUT") or "HDMI-A-1"
    card = "vc4hdmi1" if output == "HDMI-A-2" else "vc4hdmi0"
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    playerd.serve(cfg.player_socket, allowed_uids=sorted(uids), log=log, stop=stop,
                  run_dir=os.path.dirname(cfg.player_socket), alsa_card=card,
                  # Le namespace du lecteur monte sa politique Chromium sur /etc/chromium/policies/managed :
                  # il ne voit donc volontairement pas les mêmes octets que le manifeste à ce chemin. Le service
                  # OpenTV et la CLI vérifient chaque fichier ; le contrôleur indépendant compare le témoin root au
                  # hash du manifeste root (une mise à jour change ce hash), sans revalider les chemins masqués.
                  gate=lambda: promotion.promoted(cfg.playback_marker, manifest=cfg.install_manifest,
                                                   verify_files=False),
                  keys_ok=lambda: keys_ping(cfg.player_keys_socket, server_uids=keys_uids),
                  keys_kw={"path": cfg.player_keys_socket, "server_uids": keys_uids})
    return 0


def cmd_player_keys(a):
    """Aide des touches (opentv-keys + groupe input par l'unité) : abonnés = contrôleur du lecteur et root."""
    from . import playerkeys
    cfg = config.Config.load(a.env)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    playerkeys.serve(cfg.player_keys_socket, allowed_uids=sorted(_uids(cfg.player_user)), log=log, stop=stop)
    return 0


def cmd_playback(a):
    """Porte de production de « Regarder » : status (lecture seule), promote (root, après validation terrain), demote
    (root : témoin retiré PUIS séance en cours fermée côté contrôleur). Témoin et manifeste : root seulement."""
    from . import promotion
    from . import source as sourcemod
    from .playerclient import PlayerClient, PlayerUnavailable
    cfg = config.Config.load(a.env)
    if a.action == "status":
        st = promotion.check(cfg.playback_marker, manifest=cfg.install_manifest)
        src = sourcemod.SourceConfig.load(cfg.source_file, uid_ok=(0, os.geteuid())).status
        return _out({"ok": True, "promotion": st, "source": src, "available": st == "promoted" and src == "ok"})
    if os.geteuid() != 0:
        return _out({"ok": False, "error": "root_required"}, 2)
    if a.action == "promote":
        if not a.field_validated:
            return _out({"ok": False, "error": "field_validation_required",
                         "hint": "Regarder -> lecture visible -> Retour même carte, validé sur la TV réelle"}, 2)
        try:
            promotion.write(cfg.playback_marker, manifest=cfg.install_manifest, uid=0, gid=0)
        except ValueError:   # livraison installée non vérifiable (manifeste absent/modifié) : jamais de promotion
            return _out({"ok": False, "error": "delivery_unverified",
                         "hint": "sudo /opt/opentv/bin/opentv-pi manifest verify"}, 1)
        log("playback_promoted")
        return _out({"ok": True, "promotion": promotion.check(cfg.playback_marker, manifest=cfg.install_manifest)})
    removed = promotion.remove(cfg.playback_marker)
    closed = None
    try:   # séance en cours fermée tout de suite (le contrôleur et le service relisent aussi la porte)
        r = PlayerClient(cfg.player_socket).call({"op": "close_all", "reason": "demoted"}, timeout=12)
        closed = bool(r.get("ok")) and r.get("gone") is not False
    except PlayerUnavailable:
        closed = None   # contrôleur arrêté : aucune séance possible
    log("playback_demoted", removed=removed, closed=closed)
    return _out({"ok": True, "promotion": promotion.check(cfg.playback_marker, manifest=cfg.install_manifest),
                 "removed": removed, "session_closed": closed})


def cmd_manifest(a):
    rep = health.verify_manifest(a.root)
    return _out(rep, 0 if rep["ok"] else 1)


def build_parser():
    p = argparse.ArgumentParser(prog="opentv-pi", description="OpenTV Raspberry Pi 4 (%s)" % VERSION)
    p.add_argument("--env", default=config.ENV_FILE, help="fichier env (lu, jamais exécuté)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="service principal (systemd)")
    pf = sub.add_parser("preflight", help="vérifie Pi 4 / Bookworm arm64 / KMS / DRM / NetworkManager / Cage / Chromium")
    pf.add_argument("--root", default="/")
    pf.add_argument("--assume-arch", choices=("arm64", "armhf", "amd64"))
    pf.add_argument("--assume-nm-active", action="store_true")
    pf.add_argument("--text", action="store_true", help="sortie lisible (une ligne par contrôle)")
    pf.add_argument("--stage", choices=preflight.STAGES, default="full",
                    help="hardware : avant apt (prepare-pi.sh) ; full : avant installation (install.sh)")
    he = sub.add_parser("health", help="santé : manifeste, unités, HTTP, réseau")
    he.add_argument("--root", default="/")
    he.add_argument("--no-http", action="store_true")
    nh = sub.add_parser("net-helper", help="(root) consomme les jobs réseau")
    nh.add_argument("--jobs")
    nh.add_argument("--results")
    nh.add_argument("--nm-dir")
    nh.add_argument("--owner-uid", type=int)
    nh.add_argument("--no-chown", action="store_true", help="tests sans root uniquement")
    gu = sub.add_parser("ap-guard", help="(root) arrête le Wi-Fi temporaire si service inactif ou budget dépassé")
    gu.add_argument("--force-stop", action="store_true", help="arrêt sans condition (désinstallation)")
    ag = sub.add_parser("agent", help="identités des agents locaux")
    ag.add_argument("action", choices=("issue", "list", "revoke"))
    ag.add_argument("--id")
    ag.add_argument("--scopes", help="read,feedback,display")
    ag.add_argument("--owner", help="(root) compte Linux qui lance l'agent : fichier client "
                                    "/etc/opentv/agents/agent-<id>-<uid>.json, root:<groupe> 0640")
    ag.add_argument("--out", help="sans privilège seulement (refusé sous root)")
    ag.add_argument("--force", action="store_true", help="dépasser la politique choisie au QR (sur la boîte)")
    st = sub.add_parser("setup", help="réafficher le QR de configuration")
    st.add_argument("action", choices=("reset",))
    ce = sub.add_parser("cec", help="état CEC lu, sans émission sauf --activate (adaptateurs, OpenTV, annonce de ce "
                                    "démarrage, touche OK)")
    ce.add_argument("--root", default="/")
    ce.add_argument("--activate", action="store_true",
                    help="(root) réactivation explicite : OpenTV redevient la source active (TV allumée seulement, "
                         "jamais de réveil) ; ok seulement si une annonce a réellement été transmise")
    cb = sub.add_parser("cec-bootstrap", help="déclare Playback « OpenTV » + touche OK, puis UNE annonce <Active "
                                              "Source> automatique par démarrage (fenêtre bornée, TV allumée "
                                              "seulement ; jamais de réveil ni de réannonce)")
    cb.add_argument("--root", default="/")
    cb.add_argument("--state-dir", default=cecboot.STATE_DIR)
    cb.add_argument("--force-announce", action="store_true",
                    help="(opentv-cec-announce.service, via `opentv-pi cec --activate`) réactivation explicite, "
                         "ignore le témoin de démarrage ; TV déjà allumée seulement : <Active Source> + <Menu Status>, "
                         "jamais <Image View On> ni réveil — la TV passe sur l'entrée OpenTV")
    cb.add_argument("--release", action="store_true", help="(root) retour arrière : efface la configuration OpenTV")
    pd = sub.add_parser("player-daemon", help="(opentv-kiosk) contrôleur du lecteur « Regarder » (systemd)")
    pd.add_argument("--env", default=config.ENV_FILE)
    pk = sub.add_parser("player-keys", help="(opentv-keys) aide des touches de fermeture du lecteur (systemd)")
    pk.add_argument("--env", default=config.ENV_FILE)
    pb = sub.add_parser("playback", help="porte de production de « Regarder » : status | promote | demote")
    pb.add_argument("action", choices=("status", "promote", "demote"))
    pb.add_argument("--field-validated", action="store_true",
                    help="(promote) confirme la validation HDMI réelle Regarder -> lecture -> Retour même carte")
    pb.add_argument("--env", default=config.ENV_FILE)
    ma = sub.add_parser("manifest", help="vérifie le manifeste SHA-256")
    ma.add_argument("action", choices=("verify",))
    ma.add_argument("--root", default="/")
    return p


def main(argv):
    a = build_parser().parse_args(argv)
    if a.cmd == "agent" and a.action in ("issue", "revoke") and not a.id:
        return _out({"ok": False, "error": "id_required"}, 2)
    return {"serve": cmd_serve, "player-daemon": cmd_player_daemon, "player-keys": cmd_player_keys,
            "playback": cmd_playback, "preflight": cmd_preflight, "health": cmd_health, "net-helper": cmd_net_helper,
            "ap-guard": cmd_ap_guard, "agent": cmd_agent, "setup": cmd_setup, "cec": cmd_cec,
            "cec-bootstrap": cmd_cec_bootstrap, "manifest": cmd_manifest}[a.cmd](a)
