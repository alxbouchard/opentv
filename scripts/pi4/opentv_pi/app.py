"""Orchestrateur du service OpenTV Pi : mode configuration (QR + jeton), hotspot borné, messages d'agents, « Pour toi ».

Aucun privilège : les changements réseau passent par des jobs 0600 consommés par le helper root.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import string
import threading
import time

from . import (VERSION, agents, bootid, canvas_tv, cec, config, fsutil, musesetup, netjobs, netstate, nethelper,
               peeruid, phoneremote, tools, tvcontrol, validate, wifiscan)
from .foryou import ForYou, EngineError
from . import promotion
from . import source as sourcemod
from .playback import Playback
from .playerclient import PlayerClient
from .setup_token import SetupToken

KNOWN_AGENTS = ("muse", "hermit", "hermes")
AGENT_NAMES = {"muse": "Muse", "hermit": "Hermit", "hermes": "Hermes"}
# Niveaux emboîtés (moindre privilège) : aucun ⊂ lecture ⊂ avis ⊂ messages (« full ») ⊂ pilotage (« control »).
# « control » n'est JAMAIS un défaut : seulement un choix explicite du propriétaire (téléphone « Gérer les assistants »
# ou `opentv-pi muse authorize-control --yes` en root) ; aucune installation ni mise à jour ne l'accorde.
AGENT_LEVELS = {"none": [], "read": ["read"], "feedback": ["feedback", "read"],
                "full": ["display", "feedback", "read"], "control": ["control", "display", "feedback", "read"]}
# « Gérer les assistants » (Réglages TV) : mode dédié, distinct de la configuration. Il n'ouvre que la page
# /assistants sur l'adresse LAN (jamais 0.0.0.0, jamais en configuration ni hors ligne), avec son propre jeton à
# usage unique ; il ne touche ni au Wi-Fi, ni au nom, ni au hotspot, ni aux jobs réseau, et n'émet aucune identité.
AGENTS_RESULTS = ("saved", "cancelled", "expired", "unavailable")   # unavailable : écoute LAN impossible
ERR_AGENTS_LEVEL = "Choix invalide pour un assistant : choisis une option dans chaque liste."
ERR_AGENTS_REGISTRY = ("Les assistants de la boîte sont illisibles : rien n'a été changé. Redémarre la boîte "
                       "OpenTV, puis réessaie depuis la TV.")
# configuration QR réussie (Wi-Fi/nom) mais politique des assistants non enregistrée : vérité affichée au téléphone et
# à la TV, sans détail technique (registre illisible, verrou occupé ou écriture impossible : rien n'a été écrit)
MSG_AGENTS_NOT_SAVED = ("Les assistants n'ont pas été enregistrés : rien n'a changé pour eux. Réessaie depuis "
                        "Réglages → Gérer les assistants.")
ERR_AGENTS_WRITE = "L'enregistrement a échoué : rien n'a été changé. Réessaie dans un moment."
ERR_AGENTS_STALE = ("Les assistants ont changé pendant ton choix : rien n'a été enregistré. Vérifie l'état ci-dessous, "
                    "puis choisis de nouveau.")
AGENT_KEEP = "keep"   # « Ne rien changer pour lui » : aucune écriture de sa ligne, son identité n'est jamais touchée


def level_of(scopes):
    """Scopes -> préréglage affiché (none|read|feedback|full|control) : le scope le plus fort décide."""
    sc = set(scopes or ())
    return "control" if "control" in sc else "full" if "display" in sc else "feedback" if "feedback" in sc else \
        "read" if "read" in sc else "none"


def choice_changes(x, new):
    """Un choix du téléphone modifie-t-il réellement cet assistant ? (même règle pour la page et pour l'écriture)

    « keep » ⇒ jamais. Sinon : changement si le niveau choisi diffère du niveau autorisé affiché, OU si une identité
    installée dépasse ce niveau (elle serait alors réduite : c'est un vrai changement, annoncé à la confirmation).
    Un assistant affiché « Inchangé » n'est donc jamais réduit ni révoqué."""
    if new == AGENT_KEEP:
        return False
    return new != x["level"] or not set(x["have"] or ()) <= set(AGENT_LEVELS[new])


def assistant_row(a, v, seen_at, now):
    """Ligne honnête d'un assistant connu, identités liées REPLIÉES sur l'agent propriétaire.

    - « client » : niveau de l'identité du même nom (« muse » : autre client local), None si absente/sans scope ;
    - « bridge » : pont dédié lié (« muse-device ») — None si absent, sinon {"state": "ready"|"paused", "level"} :
      ses droits EFFECTIFS sont ses scopes enregistrés plafonnés par l'autorisation du propriétaire (ce que l'Agent
      Port accorde à chaque appel) ; « ready » seulement s'ils valent exactement agents.LINKED_REQUIRED (sinon le pont
      refuse de s'enregistrer : installé, mais « en pause ») ;
    - « connected » : identité du même nom installée OU pont prêt ; « connected_level » : niveau de ces identités ;
    - « have » : union des droits effectifs de toutes les identités installées (pont en pause compris) : c'est ce
      que choice_changes compare au choix du téléphone, et ce que apply_policy_changes réduit dans la même écriture ;
    - « over » : identité du même nom installée au-delà de son autorisation (un pont lié est toujours plafonné)."""
    allowed = list(v["allowed"])
    lvl = level_of(allowed)
    own = v["have"] or []
    have, conn, bridge = set(own), set(own), None
    for child, sc in sorted((v.get("linked") or {}).items()):
        if sc is None:
            continue
        eff = [x for x in sc if x in allowed]
        if not eff:   # plus aucun droit effectif : l'Agent Port la refuse, comme une identité absente
            continue
        ready = sorted(eff) == sorted(agents.LINKED_REQUIRED.get(child, ()))
        bridge = {"state": "ready" if ready else "paused", "level": level_of(eff)}
        have |= set(eff)
        if ready:
            conn |= set(eff)
    connected = bool(own) or (bridge is not None and bridge["state"] == "ready")
    return {"id": a, "name": AGENT_NAMES[a], "level": lvl, "authorized": bool(allowed), "allowed": allowed,
            "have": sorted(have), "connected": connected, "connected_level": level_of(conn) if connected else None,
            "client": level_of(own) if own else None, "bridge": bridge,
            # identité liée enregistrée (quels que soient ses droits) : sert à dater l'état public de la boîte Muse
            "linked_present": any(sc is not None for sc in (v.get("linked") or {}).values()),
            "over": bool(own) and not set(own) <= set(AGENT_LEVELS[lvl]),
            "seen_s": int(max(0, now - seen_at)) if (have and seen_at is not None) else None}
AP_PSK_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"
AP_RETRY_S = 120.0
# « Pour toi » TV : sélection recalculée au plus tard toutes les FORYOU_TTL_S, et aussitôt que la signature du
# catalogue (périphérique, inode, taille, mtime_ns) change — un remplacement atomique (rafraîchissement quotidien,
# rollback, copie manuelle) atteint donc la TV à la prochaine requête /api/tv/foryou (tv.js : toutes les 60 s).
FORYOU_TTL_S = 120.0
# Échéance d'un wifi-apply côté service : le helper refuse un job plus vieux que WIFI_APPLY_PICKUP_MAX_S et termine
# toute exécution en RUN_BUDGET_S ; au-delà, le job est abandonné (échec honnête, résultat tardif ignoré).
APPLY_DEADLINE_S = nethelper.WIFI_APPLY_PICKUP_MAX_S + nethelper.RUN_BUDGET_S + 30.0
STALE_JOBS_MAX = 32
CEC_REFRESH_S = 300.0
# Télécommande téléphone : le QR vise <nom d'hôte>.local:8082 (stable si le DHCP change d'adresse, cookie conservé)
# seulement si ce nom se résout par mDNS exactement vers l'adresse LAN liée ; sinon repli sur l'adresse liée (jamais
# affichée en texte). Vérification dans un thread (getaddrinfo, nss-mdns/avahi), au plus une par MDNS_REFRESH_S.
MDNS_REFRESH_S = 60.0
HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
SCAN_PRIME_S = 15.0           # hors ligne, avant le Wi-Fi temporaire : relevé des réseaux proches (thread, borné)
# Soumissions avec un jeton faux (jamais émis) : sans effet sur le QR (256 bits, aucun essai à deviner), mais bornées
# par adresse IP pour ne pas laisser un appareil du LAN occuper le service ; table bornée.
BAD_TOKEN_PER_IP = 30
BAD_TOKEN_WINDOW_S = 60.0
BAD_TOKEN_TRACKED_IPS = 256
ERR_FR = {
    "wifi_connect_failed": "Le Wi-Fi a refusé la connexion : vérifie le nom du réseau et le mot de passe, "
                           "puis scanne le nouveau QR.",
    "ap_start_failed": "Impossible de créer le Wi-Fi temporaire OpenTV. Branche un câble Ethernet à la boîte.",
    "apply_timeout": "La connexion au Wi-Fi n'a pas répondu à temps. Vérifie le nom du réseau et le mot de passe, "
                     "puis scanne le nouveau QR.",
}
# Erreurs du formulaire QR (texte affiché tel quel ; web.py en déduit l'étape de l'assistant à réafficher)
ERR_FORM_NAME = "Nom de la TV invalide : lettres, chiffres, espaces, apostrophes ou tirets (32 au maximum)."
ERR_FORM_NET = "Choisis comment la TV se connecte."
ERR_FORM_OFFLINE = "La TV n'a pas encore Internet : choisis ton Wi-Fi."
ERR_FORM_SSID = "Nom du Wi-Fi invalide (1 à 32 caractères, sans « ; »)."
ERR_FORM_PSK = "Mot de passe Wi-Fi invalide : il faut entre 8 et 63 caractères."
ERR_FORM_AGENT = "Choix invalide pour un assistant (Muse, Hermit ou Hermes)."
MSG_NO_NETWORK = ("Branche un câble Ethernet à la boîte OpenTV (recommandé). Sans câble, un Wi-Fi temporaire "
                  "« OpenTV » apparaîtra dans une minute.")
MSG_NO_NETWORK_NO_AP = "Branche un câble Ethernet à la boîte OpenTV pour la configurer."
MSG_BOOT_UNKNOWN = ("Wi-Fi temporaire OpenTV désactivé par sécurité (démarrage de la boîte non identifiable). "
                    "Branche un câble Ethernet à la boîte OpenTV pour la configurer.")
MSG_HOTSPOT_EXPIRED = ("Le Wi-Fi temporaire OpenTV est fermé (durée maximale atteinte). Branche un câble Ethernet à la "
                       "boîte OpenTV, ou redémarre-la (débrancher puis rebrancher) pour réessayer.")


def _noop_log(event, **fields):
    pass


def resolve_ipv4(name):
    """Nom -> ensemble d'adresses IPv4 (résolveur système : nss-mdns pour .local) ; vide en cas d'échec."""
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        return set()


def mdns_label(hostname):
    """Nom d'hôte de la boîte -> étiquette mDNS valide (minuscules, première étiquette) ou None."""
    if not isinstance(hostname, str):
        return None
    label = hostname.split(".")[0].strip().lower()
    return label if HOST_LABEL_RE.fullmatch(label) and label != "localhost" else None


def _boot_id(proc_root):
    """Identifiant du démarrage courant ; "" = inconnu ⇒ fail-closed (voir bootid.py)."""
    return bootid.read(proc_root)


class App:
    def __init__(self, cfg, runner=nethelper.default_runner, mono=time.monotonic, log=_noop_log, cec_root="/",
                 proc_root="/proc", hostname=None, resolver=resolve_ipv4, clock_synced=None):
        self.cfg, self.runner, self.mono, self.log, self.cec_root = cfg, runner, mono, log, cec_root
        self.lock = threading.RLock()
        self.fy_lock = threading.Lock()   # « Pour toi » (SQLite, catalogue) : jamais sous self.lock, que tick() prend
        self.tv_key = secrets.token_urlsafe(32)
        # page TV et API réservées au compte du kiosque (UID du pair TCP) ; None = contrôle indisponible (hors Linux)
        self.tv_client_uids = peeruid.allowed_uids(cfg.tv_client_user, proc_root)
        self.peer_uid_fn = lambda ip, port, sport: peeruid.tcp_peer_uid(ip, port, sport, proc_root)
        if self.tv_client_uids is None:
            log("tv_peer_check_unavailable")
        self.token = SetupToken(clock=mono, ttl=cfg.token_ttl_s)
        self.wifi_scan = wifiscan.Scanner(runner, mono)   # réseaux proches (assistant QR) : borné, mis en cache
        self.registry = agents.Registry(cfg.registry_file)
        # gestion des assistants (Réglages TV) : jeton distinct du QR de configuration, état mémoire seulement
        self.agents_token = SetupToken(clock=mono, ttl=cfg.token_ttl_s)
        self.agents_mode = False
        self.agents_csrf_key = secrets.token_bytes(32)
        self.agents_last = {"seq": 0, "kind": None}
        # issue de la dernière soumission du QR de configuration pour les assistants : None (rien soumis dans ce
        # parcours) ou {"saved": bool, "reason": None|"unreadable"|"busy"|"write"} — propagé à « done » comme à
        # « applying » puis au résultat Wi-Fi, pour que la TV et le téléphone ne disent jamais « tout est prêt » à tort
        self.setup_agents = None
        self.agent_seen_at = {}       # agent -> dernier appel authentifié par l'Agent Port (depuis ce démarrage)
        # « Connecter Muse » (Réglages TV → Gérer les assistants → Muse) : séance courte + jobs root dédiés
        self.muse = musesetup.MuseSetup(cfg, mono=mono, log=log)
        self.audit = agents.Audit(cfg.audit_file)
        # OpenTV Canvas : scène publiée + aperçu agent à confirmer sur la TV (canvas_tv), jamais l'inverse
        self.canvas = canvas_tv.TvCanvas.for_config(cfg, self.audit, log, mono)
        # Porte aperçu Canvas ⇄ actions d'assistant : « vérifier l'aperçu + mettre en file / lancer » et « vérifier la
        # file + créer l'aperçu » sont exclusifs (jamais une feuille ET une action en file/réclamée). Ordre : porte ->
        # self.lock / verrous du magasin et de la file ; jamais prise sous un autre verrou ni sur un chemin humain ;
        # le magasin n'y fait aucune attente de flock (aperçu et état : mémoire + lecture sans verrou de fichier).
        self.agent_tv_gate = threading.Lock()
        try:
            self.foryou = ForYou(cfg.engine_dir, cfg.foryou_state, cfg.catalogue_file, cfg.source_id, log=log)
        except (EngineError, ValueError) as e:
            self.foryou = None
            log("foryou_unavailable", error=str(e)[:40])
        self.net = {"mode": "offline", "ip": None, "wifi_present": False, "nm_ok": False}
        self.offline_since = mono()
        self.ap, self.ap_retry_at = None, 0.0
        self.pending = {}
        self.stale_jobs = []          # jobs abandonnés (échéance) : leur résultat tardif est ignoré
        self.apply = None             # {"job", "deadline"} : wifi-apply en cours, échéance monotone persistée
        self.phase, self.phase_msg = "waiting", None
        self.message = None
        self.lan_port_actual = None
        self.remote_port_actual = None
        self.remote_ip_actual = None
        # télécommande sur téléphone : repli immédiat de la télécommande CEC (état : empreintes seulement, 0600)
        rkw = {"synced": clock_synced} if clock_synced is not None else {}
        self.remote = phoneremote.PhoneRemote(cfg.remote_file, mono=mono, log=log, **rkw) if cfg.remote_enabled \
            else None
        self.mdns_name = None
        if cfg.remote_mdns:
            label = mdns_label(socket.gethostname() if hostname is None else hostname)
            self.mdns_name = label + ".local" if label else None
        self.resolver = resolver
        # dernière vérification : le nom mDNS désigne-t-il l'adresse liée ? gen = nombre de vérifications terminées
        self.mdns = {"ip": None, "ok": False, "gen": 0}
        self.muse_mdns_gen = 0        # « Connecter Muse » : vérifications faites avant l'ouverture de la séance
        self._mdns_thread, self.mdns_at = None, -1e9
        self.bad_token_ips = {}       # ip -> [horodatages monotones] des POST à jeton faux (borné)
        self.fy_session, self.fy_at, self.fy_sig = None, -1e9, None
        self.cec, self.cec_at = {"adapters": [], "discovery_only": True}, -1e9
        self._cec_thread = None
        self._scan_thread, self.scan_prime_at = None, -1e9
        self.ap_stop_at = -1e9
        self.playback = self._make_playback(cfg, log, mono)
        self.tv_actions = tvcontrol.TvActions(mono=mono, log=log)   # pilotage par assistant (scope control)
        if self.remote is not None:
            self.remote.interceptor = self._remote_intercept
        self.boot_id = _boot_id(proc_root)
        self.hs = self._hs_load()
        self.device = None
        self.setup_mode = True
        self.reload_device()
        self._apply_load()

    # -- état de l'appareil
    def reload_device(self):
        with self.lock:
            self.device = config.load_device(self.cfg)
            self.setup_mode = self.device is None or os.path.exists(self.cfg.setup_flag)
            if self.setup_mode and self.token.needs_rotation():
                self.token.issue()

    def close(self):
        if self.foryou is not None:
            self.foryou.close()
        if self.playback.active():
            self.playback.close("service")

    # -- « Regarder » : source approuvée (fail-closed) + contrôleur du lecteur ; jamais d'URL vers la TV/les agents
    @staticmethod
    def _make_playback(cfg, log, mono):
        src = None
        try:
            engine = sourcemod.load_engine(cfg.engine_dir)
            scfg = sourcemod.SourceConfig.load(cfg.source_file, uid_ok=(0, os.geteuid()),
                                               valid_host=engine.valid_public_host)
            src = sourcemod.Source(scfg, cfg.engine_dir, cfg.source_state, cfg.catalogue_file, log=log, engine=engine)
            log("source_gate", status=scfg.status, kind=scfg.kind)
        except (sourcemod.SourceError, OSError, ValueError) as e:
            log("source_gate", status="error", error=str(e)[:32])
        # porte de production : témoin de promotion terrain root:root (jamais livré par l'installation), lié à
        # l'empreinte de la livraison installée entière, relu à chaque sondage ; root SEULEMENT (promotion.TRUSTED_UIDS)
        gate = lambda: promotion.check(cfg.playback_marker, manifest=cfg.install_manifest)   # noqa: E731
        # needs_reset : « Regarder » reste indisponible tant que la remise à zéro du contrôleur (close_all d'une séance
        # orpheline d'un service précédent) n'est pas terminée — un close_all tardif ne peut donc jamais fermer une
        # séance ouverte par ce service
        pb = Playback(src, PlayerClient(cfg.player_socket), log=log, mono=mono, gate=gate, needs_reset=True)
        threading.Thread(target=pb.reset, daemon=True, name="playback-reset").start()
        return pb

    def _remote_intercept(self, action):
        """Téléphone pendant une lecture : Retour/Accueil ferment le lecteur ; toute autre action est consommée."""
        if not self.playback.active():
            return False
        if action in ("back", "home"):
            self.playback.close("phone")
        return True

    def _fy_item(self, uid):
        with self.fy_lock:
            if self.fy_session is None:
                return None
            return (self.fy_session.get("by_uid") or {}).get(uid)

    def tv_play(self, uid):
        if self.foryou is None or not isinstance(uid, str) or len(uid) > 120:
            return False, "unknown_item"
        item = self._fy_item(uid)
        if item is None:
            return False, "unknown_item"
        return self.playback.start(uid, item)

    def _touch_setup_flag(self):
        try:   # sans suivre de lien ni bloquer sur une FIFO ; fichier régulier exigé
            fsutil.touch_regular(self.cfg.setup_flag, 0o600)
        except OSError as e:
            self.log("setup_flag_error", error=getattr(e, "reason", type(e).__name__))

    def request_setup(self):
        self.tv_actions.cancel_queued("tv_setup_active")
        self._touch_setup_flag()
        with self.lock:
            self._hs_rearm("tv")   # demande explicite depuis la TV : le Wi-Fi temporaire peut de nouveau servir
        self.log("setup_requested", origin="tv")

    def _finish_setup(self):
        self.setup_mode, self.phase, self.phase_msg = False, "done", None
        self.token.burn()
        try:
            os.remove(self.cfg.setup_flag)
        except FileNotFoundError:
            pass
        self.log("setup_done", network=self.net["mode"])
        # toute réussite ferme le Wi-Fi temporaire, même si Ethernet le masque et que la connexion est « gardée »
        if self.ap is not None or self.net.get("ap_active"):
            self._stop_ap("setup_done", force=True)
        self.ap = None

    # -- Wi-Fi temporaire : budget borné par démarrage de la boîte, persistant à travers les redémarrages du service
    def _hs_fresh(self):
        # démarrage non identifiable : budget considéré épuisé (aucun hotspot automatique), jamais réarmé
        return {"v": 1, "boot_id": self.boot_id, "first_start": None, "expired": not self.boot_id}

    def _hs_load(self):
        if not self.boot_id:
            self.log("hotspot_disabled", reason="boot_id_unknown")
            return self._hs_fresh()
        try:
            with open(self.cfg.hotspot_state, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("v") != 1 or d.get("boot_id") != self.boot_id:
                raise ValueError("autre démarrage")
            fs = d.get("first_start")
            if fs is not None and (not isinstance(fs, (int, float)) or isinstance(fs, bool)):
                raise ValueError("first_start")
            return {"v": 1, "boot_id": self.boot_id, "first_start": fs, "expired": d.get("expired") is True}
        except FileNotFoundError:
            return self._hs_fresh()
        except (OSError, ValueError):
            return self._hs_fresh()   # nouveau démarrage (boot_id différent) ou fichier illisible : budget neuf

    def _hs_save(self):
        try:
            agents.atomic_write_json(self.cfg.hotspot_state, self.hs)
        except OSError as e:   # l'état mémoire reste la référence : un échec d'écriture ne réarme jamais
            self.log("hotspot_state_error", error=type(e).__name__)

    def _hs_rearm(self, origin):
        if not self.boot_id:   # identité de démarrage inconnue : budget non persistable, le hotspot reste fermé
            self.log("hotspot_rearm_refused", origin=origin, reason="boot_id_unknown")
            return
        self.hs = self._hs_fresh()
        self.ap_retry_at = 0.0
        if self.ap is not None or self.net.get("ap_active"):
            # un AP diffuse encore : son budget neuf commence maintenant (la garde root le lit dans hotspot.json)
            self.hs["first_start"] = self.mono()
            self._hs_save()
        else:
            try:
                os.remove(self.cfg.hotspot_state)
            except FileNotFoundError:
                pass
        self.log("hotspot_rearmed", origin=origin)

    def _stop_ap(self, reason, force=False):
        if "ap-stop" in self.pending.values():
            return
        if not force and self.mono() - self.ap_stop_at < AP_RETRY_S:
            return
        self.ap_stop_at = self.mono()
        self._submit("ap-stop", {})
        self.log("hotspot_stop", reason=reason)

    def _hotspot_guard(self):
        """Fail-closed : expiration du budget, AP inconnu (orphelin) ou configuration terminée ⇒ ap-stop."""
        now = self.mono()
        if os.path.exists(self.cfg.hotspot_rearm):   # opentv-pi setup reset (root) : réarmement explicite
            try:
                os.remove(self.cfg.hotspot_rearm)
            except OSError:
                pass
            self._hs_rearm("cli")
        hs = self.hs
        # durée écoulée négative (horloge d'un autre démarrage) = budget invalide : fail-closed, comme l'échéance
        if hs["first_start"] is not None and not hs["expired"] and \
                not 0 <= now - hs["first_start"] < self.cfg.hotspot_max_s:
            hs["expired"] = True
            self._hs_save()
            self.log("hotspot_expired", seconds=int(self.cfg.hotspot_max_s))
            if self.ap is not None or self.net.get("ap_active") or "ap-start" in self.pending.values():
                self._stop_ap("expired", force=True)
            self.ap = None
        if not self.net.get("ap_active"):
            return
        if not self.setup_mode:
            self._stop_ap("configured")
        elif hs["expired"]:
            self._stop_ap("expired")
        elif self.ap is None:
            self._stop_ap("orphan")   # point d'accès d'une exécution précédente : identifiants inconnus, jamais affiché

    # -- wifi-apply : échéance bornée et persistée (même démarrage de la boîte), jamais « applying » indéfiniment
    def _apply_save(self):
        try:
            if self.apply is None:
                os.remove(self.cfg.apply_state)
            else:
                agents.atomic_write_json(self.cfg.apply_state, {"v": 1, "boot_id": self.boot_id,
                                                                "job": self.apply["job"],
                                                                "deadline": self.apply["deadline"]})
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log("apply_state_error", error=type(e).__name__)

    def _apply_clear(self):
        if self.apply is not None:
            self.apply = None
            self._apply_save()

    def _apply_load(self):
        """Redémarrage du service pendant un wifi-apply : l'échéance d'origine est reprise (même boot_id, borne
        APPLY_DEADLINE_S) ; sinon (autre démarrage, identité inconnue, fichier invalide) le job est abandonné."""
        try:
            with open(self.cfg.apply_state, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            d = None
        job = d.get("job") if isinstance(d, dict) else None
        dl = d.get("deadline") if isinstance(d, dict) else None
        if self.setup_mode and isinstance(job, str) and netjobs.ID_RE.fullmatch(job) and self.boot_id \
                and d.get("boot_id") == self.boot_id and isinstance(dl, (int, float)) and not isinstance(dl, bool) \
                and 0 < dl - self.mono() <= APPLY_DEADLINE_S:
            self.apply = {"job": job, "deadline": float(dl)}
            self.pending[job] = "wifi-apply"
            self.phase, self.phase_msg = "applying", "Connexion au Wi-Fi en cours…"
            self.log("apply_resumed")
            return
        if isinstance(job, str) and netjobs.ID_RE.fullmatch(job):
            self._mark_stale(job)
            netjobs.withdraw(self.cfg.jobs_dir, job)
        self.apply = None
        self._apply_save()
        if self.setup_mode:
            self.phase, self.phase_msg = "error", ERR_FR["apply_timeout"]
        self.log("apply_abandoned", reason="restart")

    def _mark_stale(self, jid):
        self.stale_jobs.append(jid)
        del self.stale_jobs[:-STALE_JOBS_MAX]

    def _apply_deadline(self):
        if self.apply is None or self.mono() < self.apply["deadline"]:
            return
        jid = self.apply["job"]
        self.pending.pop(jid, None)
        self._mark_stale(jid)
        withdrawn = netjobs.withdraw(self.cfg.jobs_dir, jid)   # encore en file : le helper ne l'exécutera jamais
        self.apply = None
        self._apply_save()
        self.log("apply_timeout", withdrawn=withdrawn, seconds=int(APPLY_DEADLINE_S))
        if self.setup_mode:
            # échec honnête + nouveau QR ; le hotspot ne repart que selon _hotspot() (budget non réarmé)
            self.phase, self.phase_msg = "error", ERR_FR["apply_timeout"]
            self.token.issue()

    # -- boucle (toutes les ~2 s)
    def tick(self):
        net = netstate.snapshot(self.runner)
        results = netjobs.read_results(self.cfg.results_dir)
        self.muse.tick()   # progression du helper root (fichiers du service) ; verrou propre, jamais sous self.lock
        with self.lock:
            mhost = self._muse_host()
            ip = self.net["ip"] if self.net["mode"] in ("ethernet", "wifi") else None
            # démenti = une vérification TERMINÉE après l'ouverture, pour l'adresse actuelle, sans correspondance exacte
            refuted = self.mdns["gen"] > self.muse_mdns_gen and self.mdns["ip"] == ip and not self.mdns["ok"]
        self.muse.host_check(mhost is not None, refuted)   # nom non vérifié : séance fermée, jamais de QR à IP
        with self.lock:
            self.net = net
            if net["mode"] == "offline":
                if self.offline_since is None:
                    self.offline_since = self.mono()
            else:
                self.offline_since = None
            for r in results:
                self._on_result(r)
            self._apply_deadline()
            if not self.setup_mode and os.path.exists(self.cfg.setup_flag):
                self.setup_mode, self.phase, self.phase_msg = True, "waiting", None
                self.setup_agents = None   # nouveau parcours : aucune issue antérieure n'est réannoncée
                self.token.issue()
            if self.setup_mode:
                if self.phase != "applying" and self.token.needs_rotation():
                    self.token.issue()
            self._hotspot_guard()
            if self.setup_mode:
                self._hotspot()
            if self.setup_mode or self.net["mode"] not in ("ethernet", "wifi") or not self.net["ip"]:
                # configuration ou hors ligne : un QR Muse pas encore utilisé est fermé (un appariement déjà lancé
                # continue côté root et rend son issue)
                if self.muse.busy() and not self.muse.submitted():
                    self.muse.cancel("setup" if self.setup_mode else "offline")
            if self.agents_mode:
                if self.setup_mode:
                    self._agents_end("cancelled", "setup")
                elif self.net["mode"] not in ("ethernet", "wifi") or not self.net["ip"]:
                    self._agents_end("expired", "offline")
                elif self.agents_token.needs_rotation():
                    self._agents_end("expired", "token")
            if self.message and self.mono() >= self.message["until"]:
                self.message = None
            prime = self.setup_mode and net["mode"] == "offline" and net["wifi_present"] and \
                not net.get("ap_active") and self.ap is None
        self._cec_refresh()
        self._mdns_refresh()
        self.playback.refresh()
        self.canvas_sync()
        if prime:
            self._scan_prime()

    def _scan_prime(self):
        """Hors ligne, avant le Wi-Fi temporaire (qui rend wlan0 aveugle aux autres réseaux) : relève la liste des
        réseaux proches dans un thread, pour la proposer ensuite au téléphone connecté au point d'accès. Jamais dans
        la boucle (watchdog) ; au plus un relevé en cours et un par SCAN_PRIME_S."""
        if self.mono() - self.scan_prime_at < SCAN_PRIME_S:
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self.scan_prime_at = self.mono()
        self._scan_thread = threading.Thread(target=self.wifi_scan.networks, daemon=True, name="wifi-scan")
        self._scan_thread.start()

    def _cec_refresh(self):
        """Découverte CEC hors de la boucle (thread) : un cec-ctl lent ne retarde jamais le watchdog."""
        if self.mono() - self.cec_at < CEC_REFRESH_S:
            return
        if self._cec_thread is not None and self._cec_thread.is_alive():
            return
        self.cec_at = self.mono()

        def work():
            try:
                res = cec.discover(self.cec_root, runner=self.runner)
            except OSError:
                res = {"adapters": [], "discovery_only": True}
            with self.lock:
                self.cec = res
        self._cec_thread = threading.Thread(target=work, daemon=True, name="cec-discovery")
        self._cec_thread.start()

    def _mdns_check(self):
        """(thread) Le nom mDNS se résout-il exactement vers l'adresse LAN liée ? Conflit de nom (avahi renomme la
        boîte en « -2 ») ou autre appareil ⇒ non : le QR revient à l'adresse liée."""
        with self.lock:
            ip = self.net.get("ip") if self.net.get("mode") in ("ethernet", "wifi") else None
        ok = bool(ip and self.mdns_name) and self.resolver(self.mdns_name) == {ip}
        with self.lock:
            self.mdns = {"ip": ip, "ok": ok, "gen": self.mdns.get("gen", 0) + 1}

    def _mdns_refresh(self):
        if not self.mdns_name or (self.remote is None and not self.muse.busy()):
            return
        with self.lock:
            ip = self.net.get("ip") if self.net.get("mode") in ("ethernet", "wifi") else None
            stale = ip != self.mdns["ip"]
        if not ip or (not stale and self.mono() - self.mdns_at < MDNS_REFRESH_S):
            return
        if self._mdns_thread is not None and self._mdns_thread.is_alive():
            return
        self.mdns_at = self.mono()
        self._mdns_thread = threading.Thread(target=self._mdns_check, daemon=True, name="mdns-check")
        self._mdns_thread.start()

    def _submit(self, action, payload):
        jid = netjobs.submit(self.cfg.jobs_dir, self.cfg.jobs_tmp, action, payload)
        self.pending[jid] = action
        self.log("net_job_submitted", action=action)
        return jid

    def _on_result(self, r):
        action = self.pending.pop(r["id"], None)
        if action is None or action != r["action"]:
            # job abandonné à l'échéance, d'une exécution précédente ou inconnu : jamais d'effet sur l'état
            self.log("net_job_result_ignored", action=r["action"], ok=r["ok"],
                     reason="stale" if r["id"] in self.stale_jobs else "unknown")
            return
        self.log("net_job_result", action=action, ok=r["ok"], error=r["error"])
        if action == "ap-start":
            if self.ap is not None and self.ap.get("job") == r["id"]:
                self.ap["state"] = "active" if r["ok"] else "failed"
            if not r["ok"]:
                self.ap_retry_at = self.mono() + AP_RETRY_S
                self.phase_msg = ERR_FR["ap_start_failed"]
        elif action == "wifi-apply":
            self._apply_clear()
            if r["ok"]:
                self.ap = None
                self._finish_setup()
            else:
                self.phase, self.phase_msg = "error", ERR_FR.get(r["error"], ERR_FR["wifi_connect_failed"])
                self.token.issue()
        elif action == "ap-stop":
            self.ap = None

    def _hotspot(self):
        mode = self.net["mode"]
        if mode == "hotspot":
            if self.ap is not None:
                self.ap["state"] = "active"
            return
        if mode != "offline" or self.phase == "applying" or self.hs["expired"]:
            return
        if not self.cfg.hotspot_enabled or not self.net["wifi_present"]:
            return
        if self.ap is not None and self.ap["state"] == "starting":
            return
        if "ap-start" in self.pending.values() or self.mono() < self.ap_retry_at:
            return
        if self.offline_since is None or self.mono() - self.offline_since < self.cfg.hotspot_grace_s:
            return
        if self.hs["first_start"] is None:
            self.hs["first_start"] = self.mono()
            self._hs_save()
        ssid = "OpenTV-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
        psk = "".join(secrets.choice(AP_PSK_ALPHABET) for _ in range(12))
        jid = self._submit("ap-start", {"ssid": ssid, "psk": psk})
        self.ap = {"ssid": ssid, "psk": psk, "state": "starting", "job": jid}

    # -- page de configuration (LAN)
    def want_lan(self):
        return self.lan_purpose() is not None

    def lan_purpose(self):
        """Écoute LAN voulue : "setup" (configuration, 0.0.0.0, hotspot compris), "agents" (gestion des assistants,
        configuré et en ligne seulement, sur l'adresse LAN) ou None."""
        with self.lock:
            if self.setup_mode:
                return "setup" if self.net["mode"] != "offline" else None
            if self.agents_mode and self.net["mode"] in ("ethernet", "wifi") and self.net["ip"]:
                return "agents"
            if self.net["mode"] in ("ethernet", "wifi") and self.net["ip"] and self.muse.listening():
                return "muse"
            return None

    def lan_ip(self):
        with self.lock:
            return self.net["ip"] if self.net["mode"] in ("ethernet", "wifi") else None

    def _muse_host(self):   # sous self.lock
        """Nom mDNS de la boîte s'il vient d'être VÉRIFIÉ vers l'adresse LAN actuelle ; sinon None (jamais l'IP)."""
        ip = self.net["ip"] if self.net["mode"] in ("ethernet", "wifi") else None
        if ip and self.mdns_name and self.mdns["ok"] and self.mdns["ip"] == ip:
            return self.mdns_name
        return None

    def muse_host(self):
        with self.lock:
            return self._muse_host()

    def muse_hosts(self, port):
        """En-têtes Host acceptés par les pages /muse : le nom mDNS exact de la boîte seulement (avec le port réel) ;
        jamais l'adresse IP (le QR n'en porte pas)."""
        return {"%s:%d" % (self.mdns_name, port)} if self.mdns_name else set()

    # -- télécommande sur téléphone (LAN, après configuration seulement)
    def want_remote(self):
        with self.lock:
            return self.remote is not None and not self.setup_mode and self.net["mode"] in ("ethernet", "wifi")

    def remote_bind_ip(self):
        """Adresse LAN actuelle sur laquelle écouter (jamais 0.0.0.0), ou None si la télécommande doit être fermée."""
        with self.lock:
            if self.remote is None or self.setup_mode or self.net["mode"] not in ("ethernet", "wifi"):
                return None
            return self.net["ip"] or None

    def remote_hosts(self, bound_ip, port):
        """En-têtes Host acceptés : l'adresse liée et le nom mDNS exact de la boîte (avec le port réel)."""
        hosts = {"%s:%d" % (bound_ip, port)}
        if self.mdns_name:
            hosts.add("%s:%d" % (self.mdns_name, port))
        return hosts

    def remote_device_name(self):
        with self.lock:
            return (self.device or {}).get("name") or "OpenTV"

    def _remote_view(self):
        ip = self.net["ip"]
        if self.remote is None or self.setup_mode or self.net["mode"] not in ("ethernet", "wifi") \
                or not ip or not self.remote_port_actual or self.remote_ip_actual != ip:
            return {"enabled": False}
        pr = self.remote.pairing()
        host = self.mdns_name if (self.mdns_name and self.mdns["ok"] and self.mdns["ip"] == ip) else ip
        return {"enabled": True, "url": "http://%s:%d/r?p=%s" % (host, self.remote_port_actual, pr["secret"]),
                "expires_in": pr["expires_in"], "phones": self.remote.count()}

    def remote_pair_link(self):
        """Lien d'appairage pour la page « C'est prêt » du téléphone de configuration (même QR que la TV)."""
        with self.lock:
            v = self._remote_view()
        return v.get("url") if v.get("enabled") else None

    def remote_revoke(self):
        if self.remote is not None:
            self.remote.revoke_all()

    def remote_events(self, after):
        if self.remote is None:
            return {"seq": 0, "events": [], "idle": True}
        return self.remote.wait_events(after)

    def setup_context(self):
        with self.lock:
            return {"net_mode": self.net["mode"], "name": (self.device or {}).get("name") or "",
                    "policy": self.registry.policy()}

    def check_token(self, tok):
        """Affichage du formulaire (GET) : lecture seule, n'use jamais les essais (seule une soumission POST compte),
        sinon n'importe quel appareil du LAN pourrait brûler le QR en rafraîchissant une mauvaise adresse."""
        with self.lock:
            return self.setup_mode and self.phase != "applying" and self.token.check(tok)

    def setup_networks(self, tok, client=None):
        """Réseaux Wi-Fi proches pour l'étape 1 de l'assistant -> liste, ou None (jeton refusé / adresse limitée).

        Même contrôle que l'affichage du formulaire (jamais d'essai consommé). nmcli n'est lancé qu'avec un jeton
        valide, hors du verrou du service (la boucle n'attend jamais un balayage), et son coût est borné par le cache
        du Scanner quel que soit le nombre de requêtes."""
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return None
            if not (self.setup_mode and self.phase != "applying" and self.token.check(tok)):
                return None
            hotspot = self.net["mode"] == "hotspot"
        return self.wifi_scan.networks(allow_stale=hotspot)

    def _bad_token_limited(self, client, record):
        """Débit des POST à jeton faux par adresse IP (table bornée). -> True si l'adresse est limitée."""
        now, ip = self.mono(), client if isinstance(client, str) else "?"
        win = [t for t in self.bad_token_ips.get(ip, ()) if t > now - BAD_TOKEN_WINDOW_S]
        if record:
            win.append(now)
        if win:
            self.bad_token_ips[ip] = win[-BAD_TOKEN_PER_IP:]
        else:
            self.bad_token_ips.pop(ip, None)
        if len(self.bad_token_ips) > BAD_TOKEN_TRACKED_IPS:
            for k in [k for k, v in self.bad_token_ips.items() if v[-1] <= now - BAD_TOKEN_WINDOW_S]:
                del self.bad_token_ips[k]
            while len(self.bad_token_ips) > BAD_TOKEN_TRACKED_IPS:
                del self.bad_token_ips[min(self.bad_token_ips, key=lambda k: self.bad_token_ips[k][-1])]
        return len(win) >= BAD_TOKEN_PER_IP

    def setup_submit(self, form, client=None, outcome=None):
        """-> (code HTTP, page, message d'erreur). Le jeton n'est consommé que par une soumission valide.

        `outcome` (dict, facultatif) reçoit {"agents_saved": bool, "agents_reason": None|"unreadable"|"busy"|
        "write"} pour une soumission acceptée (« done » ou « applying ») : le Wi-Fi et le nom ne sont jamais bloqués
        par le fichier des assistants, mais un échec de celui-ci est annoncé honnêtement (téléphone + TV).

        Un jeton faux (ou une soumission pendant « applying ») ne compte jamais comme essai : seul le formulaire
        invalide accompagné du **bon** jeton use le QR (5 essais). Les jetons faux sont limités par IP (429)."""
        g = lambda k: form.get(k) if isinstance(form.get(k), str) else ""   # noqa: E731
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return 429, "denied", None
            if not self.setup_mode or self.phase == "applying":
                return 403, "denied", None
            if not self.token.check(g("t")):
                self._bad_token_limited(client, record=True)
                return 403, "denied", None
            err, payload = None, None
            try:
                name = validate.device_name(g("name"))
            except ValueError:
                err = ERR_FORM_NAME
            net = g("net")
            if err is None and net not in ("keep", "wifi"):
                err = ERR_FORM_NET
            if err is None and net == "keep" and self.net["mode"] not in ("ethernet", "wifi"):
                err = ERR_FORM_OFFLINE
            if err is None and net == "wifi":
                try:
                    ssid = validate.ssid(g("ssid"))
                except ValueError:
                    err = ERR_FORM_SSID
                if err is None:
                    try:
                        psk = validate.psk(g("psk"))
                    except ValueError:
                        err = ERR_FORM_PSK
                if err is None:
                    restore = self.net["mode"] == "hotspot"
                    payload = {"ssid": ssid, "psk": psk, "hidden": g("hidden") == "1",
                               "delay_s": 3 if restore else 0, "restore_ap": restore}
            policy = {}   # chaque agent connu est enregistré, même « aucun accès » (scopes vides = refus)
            for a in KNOWN_AGENTS:
                lvl = g("agent_" + a) or "none"
                if lvl not in AGENT_LEVELS:
                    err = err or ERR_FORM_AGENT
                else:
                    policy[a] = list(AGENT_LEVELS[lvl])
            if err is not None:
                self.token.fail_attempt()
                return 400, "form", err
            if not self.token.consume(g("t")):
                return 403, "denied", None
            if payload is not None:
                # Wi-Fi non encore confirmé : la configuration reste ouverte même après un redémarrage du service
                self._touch_setup_flag()
            config.save_device(self.cfg, {"name": name})
            self.device = config.load_device(self.cfg)
            reason = None
            try:   # droits réduits au QR : l'ancien accès est révoqué dans la MÊME écriture (verrou inter-processus)
                self.registry.set_policy(policy, revoke_exceeding=True)
            except (ValueError, OSError) as e:
                # registre illisible (jamais remplacé : les assistants restent refusés, la TV l'affiche) ou verrou /
                # écriture en échec : le Wi-Fi et le nom ne sont jamais bloqués par le fichier des assistants, mais
                # l'échec est propagé (page du téléphone, état et annonce TV) — jamais un « tout est prêt » à tort
                reason = "unreadable" if isinstance(e, ValueError) else \
                    "busy" if isinstance(e, agents.RegistryBusy) else "write"
                self.log("setup_agents_not_saved", error=str(e) if isinstance(e, ValueError) else type(e).__name__)
            self.setup_agents = {"saved": reason is None, "reason": reason}
            if isinstance(outcome, dict):
                outcome.update(agents_saved=reason is None, agents_reason=reason)
            self.log("setup_submitted", network=net, agents=sum(1 for v in policy.values() if v))
            if payload is not None:
                jid = self._submit("wifi-apply", payload)
                payload = None
                self.apply = {"job": jid, "deadline": self.mono() + APPLY_DEADLINE_S}
                self._apply_save()
                self.phase, self.phase_msg = "applying", "Connexion au Wi-Fi en cours…"
                return 200, "applying", None
            self._finish_setup()
            return 200, "done", None

    # -- gestion des assistants (Réglages TV -> QR -> téléphone) : aucun effet réseau, aucune identité émise
    def agent_seen(self, agent):
        """Agent Port : appel authentifié réussi (preuve d'activité réelle, mémoire seulement). Une identité liée
        (« muse-device ») est vue au nom de son agent PROPRIÉTAIRE (« muse ») : c'est lui que montre la TV. L'audit
        et le journal de l'Agent Port gardent l'identité authentifiée."""
        owner = agents.canonical_agent(agent)
        if owner in KNOWN_AGENTS:
            with self.lock:
                self.agent_seen_at[owner] = self.mono()

    def assistants(self):
        return self._assistants_read()[0]

    def assistants_state(self):
        """-> (liste, empreinte) : voir _assistants_read."""
        return self._assistants_read()[:2]

    def _assistants_read(self):
        """-> (liste, empreinte). État honnête de chaque assistant connu, lu en UNE fois : « autorisé » (plafond
        choisi au QR) et « connecté » (identité réellement installée sur la boîte par l'administrateur, jeton valide)
        sont deux informations distinctes ; « over » = installé avec plus de droits que son autorisation (état
        hérité, ex. émis avec --force). Une autorisation seule ne connecte rien ; aucun secret n'est exposé.
        L'empreinte (non secrète) permet de refuser un envoi si l'état a changé depuis la page de confirmation.
        -> (liste, empreinte, illisible) : « illisible » vient de la MÊME lecture (jamais relu ensuite depuis
        self.registry.corrupt, qu'un autre fil peut modifier entre-temps)."""
        view, fp, corrupt = self.registry.snapshot(KNOWN_AGENTS)
        out = []
        now = self.mono()
        with self.lock:
            seen = dict(self.agent_seen_at)
        for a in KNOWN_AGENTS:
            out.append(assistant_row(a, view[a], seen.get(a), now))
        return out, fp, corrupt

    def _assistants_summary(self):
        lst, _fp, corrupt = self._assistants_read()
        muse = next((x for x in lst if x["id"] == "muse"), None)
        return {"authorized": sum(1 for x in lst if x["authorized"]),
                "connected": sum(1 for x in lst if x["connected"]), "total": len(lst),
                "list": [{"id": x["id"], "authorized": x["authorized"], "connected": x["connected"]} for x in lst],
                "unreadable": corrupt,
                # pont Muse dédié (« muse-device ») : ready = connecté et autorisé à piloter ; paused = installé mais
                # en pause (droits réduits) ; None = pas installé
                "muse_bridge": (muse.get("bridge") or {}).get("state") if muse else None,
                "muse_linked": (bool(muse["linked_present"]) if muse else None) if not corrupt else None}

    def _muse_tv(self, assistants):   # sous self.lock
        port = self.lan_port_actual if (self.lan_purpose() == "muse") else None
        v = self.muse.tv_view(self._muse_host(), port, assistants.get("muse_linked"))
        v["bridge"] = assistants.get("muse_bridge")
        return v

    def request_agents(self):
        """Réglages TV « Gérer les assistants » (confirmé sur la TV) -> (ok, raison). Jamais en configuration ni
        hors ligne ; aucun fichier de configuration, hotspot, job réseau ou nom touché."""
        with self.lock:
            if self.setup_mode or self.device is None:
                return False, "setup"
            if self.net["mode"] not in ("ethernet", "wifi") or not self.net["ip"]:
                return False, "offline"
            if self.muse.busy():   # une seule page LAN à la fois : connexion de Muse en cours
                return False, "busy"
            self.agents_token.issue()
            self.agents_mode = True
        self.tv_actions.cancel_queued("tv_busy")   # jamais une action d'assistant sur la feuille des assistants
        self.log("agents_manage", state="open")
        return True, None

    def cancel_agents(self, origin):
        with self.lock:
            return self._agents_end("cancelled", origin)

    def agents_seq(self):
        with self.lock:
            return self.agents_last["seq"]

    def agents_last_result(self):
        with self.lock:
            return dict(self.agents_last)

    def agents_unavailable(self, reason):
        """Écoute /assistants impossible (bind refusé…) : le mode se ferme tout de suite, annoncé « unavailable » à
        la TV (« QR indisponible, réessaie »). Aucun nouvel essai automatique : l'utilisateur rouvre depuis Réglages."""
        with self.lock:
            return self._agents_end("unavailable", reason)

    def _agents_end(self, kind, reason):   # sous self.lock
        if not self.agents_mode:
            return False
        self.agents_mode = False
        self.agents_token.burn()
        self.agents_last = {"seq": self.agents_last["seq"] + 1, "kind": kind}
        self.log("agents_manage", state=kind, reason=reason)
        return True

    # -- « Connecter Muse » (TV loopback : consentement explicite ; téléphone : séance courte) ----------------
    def muse_start(self, consent):
        """« Autoriser et connecter » confirmé sur la TV -> (ok, raison). consent doit valoir exactement True ; jamais
        en configuration ni hors ligne ; la gestion des assistants (autre QR) est fermée d'abord."""
        if consent is not True:
            return False, "consent_required"
        with self.lock:
            if self.setup_mode or self.device is None:
                return False, "setup"
            if self.net["mode"] not in ("ethernet", "wifi") or not self.net["ip"]:
                return False, "offline"
            if not self.mdns_name:   # QR par le nom de la boîte seulement : jamais d'adresse IP en repli
                return False, "name_unavailable"
            if self.agents_mode:
                self._agents_end("cancelled", "muse")
        ok, why = self.muse.start(consent)
        if ok:
            with self.lock:   # vérification fraîche du nom : seules celles terminées APRÈS l'ouverture démentent
                self.muse_mdns_gen = self.mdns["gen"]
                self.mdns_at = -1e9
            self._mdns_refresh()
            self.tv_actions.cancel_queued("tv_busy")   # jamais une action d'assistant pendant la connexion de Muse
        return ok, why

    def muse_cancel(self, origin, sid=None):
        return self.muse.cancel(origin, sid=sid)

    def muse_disconnect(self, confirm):
        """« Déconnecter Muse » confirmé sur la TV (confirm doit valoir exactement True) -> (ok, raison)."""
        if confirm is not True:
            return False, "confirmation_required"
        with self.lock:
            if self.setup_mode or self.device is None:
                return False, "setup"
        return self.muse.disconnect(confirm, owned=self._muse_owned())

    def _muse_owned(self):
        lst, _fp, corrupt = self._assistants_read()
        muse = next((x for x in lst if x["id"] == "muse"), None)
        present = None if corrupt or muse is None else bool(muse["linked_present"])
        return self.muse.box_view(present)["owned"]

    def muse_probe(self):
        """TV : Muse ouvert dans « Gérer les assistants » -> le helper root republie l'état public de la boîte (borné)."""
        with self.lock:
            if self.setup_mode or self.device is None:
                return False
        return self.muse.probe("tv")

    def muse_unavailable(self):
        """Écoute /muse impossible : la séance (pas encore utilisée) est annulée, la TV l'annonce."""
        if not self.muse.submitted():
            self.muse.cancel("bind")

    def muse_view(self):
        assistants = self._assistants_summary()
        with self.lock:
            return self._muse_tv(assistants)

    def agents_csrf(self, tok):
        """Jeton anti-CSRF lié au QR courant (HMAC d'une clé tirée à chaque démarrage) : jamais stocké ni journalisé."""
        return hmac.new(self.agents_csrf_key, ("agents:" + (tok or "")).encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def check_agents_token(self, tok, client=None):
        """Affichage (GET) : lecture seule, n'use jamais un essai ; refusé hors du mode « assistants »."""
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return False
            return self.agents_mode and not self.setup_mode and self.agents_token.check(tok)

    def agents_submit(self, form, client=None):
        """Phone POST /assistants -> (code HTTP, type, erreur, changements). Type : denied | form | saved | cancelled
        | unchanged. Seules les lignes réellement modifiées (choice_changes) sont écrites ; « v » = empreinte de
        l'état montré à la confirmation (état changé entre-temps ⇒ 409, rien d'écrit, aucun essai usé).

        Le jeton n'est consommé que par un enregistrement réussi ou une annulation ; jeton faux = aucun essai (limité
        par IP) ; bon jeton + formulaire invalide = un essai usé, politique inchangée. Un seul verrou couvre
        vérification, écriture atomique et consommation : aucun rejeu possible."""
        g = lambda k: form.get(k) if isinstance(form.get(k), str) else ""   # noqa: E731
        with self.lock:
            if self._bad_token_limited(client, record=False):
                return 429, "denied", None, None
            if not self.agents_mode or self.setup_mode:
                return 403, "denied", None, None
            tok = g("t")
            if not self.agents_token.check(tok):
                self._bad_token_limited(client, record=True)
                return 403, "denied", None, None
            if not hmac.compare_digest(g("c").encode("utf-8"), self.agents_csrf(tok).encode("utf-8")):
                self.agents_token.fail_attempt()
                return 403, "denied", None, None
            action = g("action")
            if action == "cancel":
                self.agents_token.consume(tok)
                self._agents_end("cancelled", "phone")
                return 200, "cancelled", None, None
            if action != "save":
                self.agents_token.fail_attempt()
                return 400, "form", ERR_AGENTS_LEVEL, None
            lst, fp, corrupt = self._assistants_read()
            if corrupt:   # registre illisible (même lecture que l'empreinte) : rien n'est écrit, nouvel essai possible
                return 503, "form", ERR_AGENTS_REGISTRY, None
            if g("v") != fp:   # état modifié depuis la confirmation (ou formulaire forgé) : rien n'est écrit
                if not re.fullmatch(r"[0-9a-f]{16}", g("v")):
                    self.agents_token.fail_attempt()
                    return 400, "form", ERR_AGENTS_LEVEL, None
                return 409, "form", ERR_AGENTS_STALE, None
            changes = {}
            for x in lst:   # chaque agent connu explicitement (aucune valeur par défaut implicite)
                lvl = g("agent_" + x["id"])
                if lvl not in AGENT_LEVELS and lvl != AGENT_KEEP:
                    self.agents_token.fail_attempt()
                    return 400, "form", ERR_AGENTS_LEVEL, None
                if choice_changes(x, lvl):   # seules les lignes réellement modifiées sont écrites
                    changes[x["id"]] = list(AGENT_LEVELS[lvl])
            try:
                changed = self.registry.apply_policy_changes(changes, KNOWN_AGENTS, fp)
            except ValueError as e:
                if str(e) == "stale":
                    return 409, "form", ERR_AGENTS_STALE, None
                return 503, "form", ERR_AGENTS_REGISTRY, None
            except OSError as e:
                self.log("agents_save_error", error=type(e).__name__)
                return 503, "form", ERR_AGENTS_WRITE, None
            self.agents_token.consume(tok)
            if not changed:   # rien de réellement modifié : aucune écriture, annoncé comme tel
                self._agents_end("cancelled", "unchanged")
                return 200, "unchanged", None, {}
            self._agents_end("saved", "phone")
        self.log("agents_saved", changed=len(changed),
                 restricted=sum(1 for v in changed.values() if v == "restricted"),
                 revoked=sum(1 for v in changed.values() if v == "revoked"))
        return 200, "saved", None, changed

    def _agents_view(self):   # sous self.lock
        last = dict(self.agents_last)
        if not self.agents_mode:
            return {"active": False, "url": None, "expires_in": None, "last": last}
        cur = self.agents_token.current()
        port = self.lan_port_actual
        ip = self.net["ip"] if self.net["mode"] in ("ethernet", "wifi") else None
        url = "http://%s:%d/assistants?t=%s" % (ip, port, cur[0]) if (cur and ip and port) else None
        return {"active": True, "url": url, "expires_in": cur[1] if cur else None, "last": last}

    # -- vue TV (loopback)
    def _setup_view(self):
        sa = self.setup_agents
        agents_out = {"agents_saved": sa is None or sa["saved"], "agents_reason": sa["reason"] if sa else None,
                      "agents_message": MSG_AGENTS_NOT_SAVED if (sa and not sa["saved"]) else None}
        if not self.setup_mode:
            return dict({"active": False, "phase": self.phase, "message": None, "url": None, "token": None,
                         "expires_in": None, "hotspot": None}, **agents_out)
        mode, ip = self.net["mode"], self.net["ip"]
        cur = self.token.current() if self.phase != "applying" else None
        url = None
        port = self.lan_port_actual or self.cfg.setup_port
        if cur and mode != "offline" and ip:
            url = "http://%s:%d/setup?t=%s" % (ip, port, cur[0])
        hs = None
        if self.ap is not None and self.ap["state"] in ("starting", "active"):
            hs = {"ssid": self.ap["ssid"], "state": self.ap["state"],
                  "wifi_qr": "WIFI:T:WPA;S:%s;P:%s;;" % (self.ap["ssid"], self.ap["psk"])}
        msg = self.phase_msg
        if mode == "offline" and not hs and not self.boot_id and self.cfg.hotspot_enabled:
            msg = MSG_BOOT_UNKNOWN
        elif mode == "offline" and self.hs["expired"] and not hs:
            msg = MSG_HOTSPOT_EXPIRED
        elif msg is None and mode == "offline":
            if hs:
                msg = "Création du Wi-Fi temporaire OpenTV…"
            else:
                msg = MSG_NO_NETWORK if (self.cfg.hotspot_enabled and self.net["wifi_present"]) else MSG_NO_NETWORK_NO_AP
        return dict({"active": True, "phase": self.phase, "message": msg, "url": url,
                     "token": cur[0] if cur else None, "expires_in": cur[1] if cur else None, "hotspot": hs},
                    **agents_out)

    def tv_state(self):
        canvas = self.canvas_view()   # hors du verrou du service (lecture disque du magasin Canvas)
        with self.lock:
            m = self.message
            if m and self.mono() >= m["until"]:
                m = self.message = None
            # une seule lecture canonique : l'en-tête (« agents », compat.) et les compteurs viennent de la même liste
            # d'assistants connus réellement connectés — jamais d'entrée étrangère ni d'identité aux scopes invalides
            assistants = self._assistants_summary()
            return {"device": (self.device or {}).get("name") or "OpenTV", "configured": self.device is not None,
                    "version": VERSION, "network": {"mode": self.net["mode"], "ip": self.net["ip"]},
                    "setup": self._setup_view(),
                    "message": {"text": m["text"], "from": m["from"],
                                "seconds_left": int(m["until"] - self.mono())} if m else None,
                    "agents": [x["id"] for x in assistants["list"] if x["connected"]],
                    "cec": {"adapters": len(self.cec["adapters"])},
                    "assistants": dict(assistants, manage=self._agents_view()),
                    "muse": self._muse_tv(assistants),
                    "foryou": self.foryou is not None, "remote": self._remote_view(),
                    "playback": self.playback.view(), "canvas": canvas,
                    # pilotage par assistant : actions en attente (fiche, film lancé, touche), à réclamer une fois
                    "actions": {"pending": [] if self.setup_mode else self.tv_actions.pending()}}

    def _catalogue_sig(self):
        """Signature du catalogue (périphérique, inode, taille, mtime_ns) ; None si illisible (repli : TTL seul)."""
        try:
            st = os.stat(self.cfg.catalogue_file)
        except (OSError, ValueError):
            return None
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def _fy_rebuild(self):
        # signature relevée AVANT la lecture : un remplacement pendant la construction relance la suivante
        sig = self._catalogue_sig()
        self.fy_session, self.fy_at, self.fy_sig = self.foryou.tv_session(), self.mono(), sig

    def tv_foryou(self):
        if self.foryou is None:
            return {"rows": [], "note": "« Pour toi » est indisponible sur cette boîte.", "marks": {}, "buttons": []}
        with self.fy_lock:
            if self.fy_session is None or self.mono() - self.fy_at >= FORYOU_TTL_S:
                self._fy_rebuild()
            else:
                sig = self._catalogue_sig()
                if sig is not None and sig != self.fy_sig:   # catalogue remplacé : nouvelle sélection sans attendre
                    self._fy_rebuild()
            return self.fy_session["data"]

    def tv_feedback(self, uid, action):
        if self.foryou is None:
            return None, "« Pour toi » est indisponible."
        with self.fy_lock:
            if self.fy_session is None:
                self._fy_rebuild()
            return self.foryou.tv_feedback(self.fy_session, uid, action)

    # -- contexte des outils agents
    def agent_status(self, agent, scopes):
        with self.lock:
            mode = self.net["mode"]
            return {"device": (self.device or {}).get("name") or "OpenTV", "configured": self.device is not None,
                    "setup_active": self.setup_mode, "network": {"mode": mode, "online": mode in ("ethernet", "wifi")},
                    "cec": {"adapters": len(self.cec["adapters"]), "discovery_only": True},
                    "agent": {"id": agent, "scopes": list(scopes)}, "relay": {"status": "not_implemented"},
                    "version": VERSION, "control": self._control_view(scopes)}

    def show_message(self, agent, text, seconds):
        # nom affiché = agent PROPRIÉTAIRE (« muse-device » -> « Muse ») ; le journal garde l'identité authentifiée
        with self.lock:
            self.message = {"text": text, "from": agents.canonical_agent(agent), "until": self.mono() + seconds}
        self.log("agent_message", agent=agent, length=len(text))
        return {"shown": True, "seconds": seconds}

    # -- pilotage par assistant (scope « control ») : commandes sémantiques typées, jamais d'URL ni de touche brute
    def _control_view(self, scopes):   # sous self.lock (agent_status) : lectures sans verrou du service
        """Moindre privilège : sans scope « control », ni film en cours (id), ni état de lecture, ni actions TV."""
        if "control" not in scopes:
            return {"enabled": False}
        pb = self.playback.view()
        return {"enabled": True,
                "playback": {"available": pb["available"], "state": pb["state"], "id": pb["id"],
                             "reason": pb["reason"], "seq": pb["seq"]},
                "actions": self.tv_actions.recent()}

    def _fy_lookup(self, uid):
        """-> (élément du catalogue accepté, fiche TV) ; (None, None) si absent. Sélection reconstruite au besoin."""
        if self.foryou is None or not isinstance(uid, str):
            return None, None
        with self.fy_lock:
            if self.fy_session is None or self.mono() - self.fy_at >= FORYOU_TTL_S or \
                    (self._catalogue_sig() not in (None, self.fy_sig)):
                self._fy_rebuild()
            sess = self.fy_session
            item = (sess.get("by_uid") or {}).get(uid)
            if item is None:
                return None, None
            return item, self.foryou.tv_item(sess, uid)

    def agent_control(self, agent, name, a):
        """Outils de pilotage de l'Agent Port (déjà authentifiés, scope control vérifié, arguments validés, débit
        borné). -> résultat autoritaire (état accepté/persisté), ou tools.ToolError(code stable).

        Priorité humaine : configuration ou « Gérer les assistants » ouverts ⇒ refus ; pendant une lecture, seule la
        fermeture (close_player, back/home) est acceptée — jamais une fiche ni une touche qui volerait le focus du
        lecteur ; tant que la fin d'une lecture n'est pas acquittée par la page (retour sur la fiche), fiche, touche et
        lancement sont refusés « player_returning » (close_player reste permis). Le lancement passe par
        Playback.start, exactement comme « Regarder » (source approuvée, promotion terrain, contrôleur prêt) : aucune
        porte n'est contournée et aucune adresse n'est vue ni produite ici."""
        # fermer le lecteur est toujours permis (jamais de focus volé : on rend la main à OpenTV), même pendant un
        # réglage ; tout le reste attend que l'humain ait fini (configuration, « Gérer les assistants »)
        if name == "close_player" or (name == "remote_control" and a["key"] in ("back", "home") and
                                      self.playback.active()):
            closed = self.playback.active()
            if closed:
                self.playback.close("agent")
            self.log("agent_control", agent=agent, tool=name, ok=True)
            if name == "remote_control":
                return {"accepted": True, "effect": "player_closing", "playback": self._pb_public()}
            return {"closed": closed, "playback": self._pb_public()}
        with self.lock:
            if self.setup_mode or self.device is None:
                raise tools.ToolError("tv_setup_active")
            if self.agents_mode:
                raise tools.ToolError("tv_busy")
        if self.muse.busy():   # connexion/déconnexion de Muse en cours sur la TV : l'humain d'abord
            raise tools.ToolError("tv_busy")
        if self.canvas.preview_live():   # feuille Canvas : seul le propriétaire décide (revérifié sous la porte)
            raise tools.ToolError("tv_busy")
        if name in ("remote_control", "present_title") and self.playback.returning():
            # fin de lecture pas encore acquittée : la page /tv revient sur la fiche du film ; une touche ou une fiche
            # d'assistant ferait courir ce retour contre elle (même refus que play_title). close_player reste permis.
            raise tools.ToolError("player_returning")
        if name == "remote_control":
            if self.playback.active():   # lecteur au premier plan : seules Retour/Accueil (ci-dessus) le ferment
                raise tools.ToolError("player_active")
            act = self._push("key", agent, {"key": a["key"]})
            self.log("agent_control", agent=agent, tool=name, ok=True)
            return {"accepted": True, "action": act}
        item, tvitem = self._fy_lookup(a["id"])
        if item is None or tvitem is None:
            raise tools.ToolError("unknown_id")
        uid = a["id"]
        # anti-répétition : un film qu'Alex a explicitement marqué « Déjà vu » (marques relues dans le profil à
        # l'instant, tv_item) n'est ni proposé ni lancé en mode « new » (défaut) — refus AVANT toute action TV et
        # avant Playback.start. « rewatch » (Alex demande explicitement de revoir) suit le chemin normal et toutes
        # les portes ci-dessous ; aucune marque n'est jamais retirée ni ajoutée ici.
        if name in ("present_title", "play_title") and a.get("mode", "new") != "rewatch" and \
                (tvitem.get("marks") or {}).get("seen") is True:
            self.log("agent_control", agent=agent, tool=name, ok=False, error="already_seen")
            raise tools.ToolError("already_seen")
        if name == "present_title":
            if self.playback.active():
                raise tools.ToolError("player_active")
            payload = {"id": uid, "item": tvitem}
            if a.get("text"):
                payload["text"] = a["text"]
            act = self._push("present", agent, payload)
            self.log("agent_control", agent=agent, tool=name, ok=True)
            return {"accepted": True, "id": uid, "title": tvitem.get("title"), "action": act}
        if name == "play_title":
            # agent : seulement depuis « idle » (atomique dans Playback.start). Une lecture « closed »/« unavailable »
            # pas encore acquittée par la page /tv signifie que la TV revient encore sur la fiche précédente : un
            # nouveau lancement ferait courir ce retour contre le nouveau film (refus « player_returning »).
            with self.agent_tv_gate:   # aucun aperçu créé entre sa vérification et « loading » (raison du service)
                self._no_live_preview()
                ok, err = self.playback.start(uid, item, require_idle=True)
            if not ok:
                raise tools.ToolError({"busy": "player_busy", "unavailable": "playback_unavailable",
                                       "returning": "player_returning"}.get(err, "playback_refused"))
            pb = self._pb_public()
            try:   # la page TV montre la fiche du film sous le lecteur et y revient à la fermeture
                act = self._push("play", agent, {"id": uid, "item": tvitem, "playback_seq": pb["seq"]})
            except tools.ToolError:
                act = None   # file pleine : la lecture reste acceptée, la page la reprend par son état
            self.log("agent_control", agent=agent, tool=name, ok=True)
            return {"accepted": True, "id": uid, "title": tvitem.get("title"), "playback": pb, "action": act}
        raise tools.ToolError("unknown_tool")

    def _no_live_preview(self):   # sous agent_tv_gate
        if self.canvas.preview_live():
            raise tools.ToolError("tv_busy")

    def _push(self, kind, agent, payload):
        # « from » (affiché « Proposé par … » sur la TV) = agent PROPRIÉTAIRE : « muse-device » -> « muse »
        with self.agent_tv_gate:   # vérification de l'aperçu ET mise en file, sans aperçu créé entre les deux
            self._no_live_preview()
            try:
                return self.tv_actions.push(kind, agents.canonical_agent(agent), payload)
            except tvcontrol.QueueFull:
                raise tools.ToolError("tv_queue_full")

    def _pb_public(self):
        v = self.playback.view()
        return {"state": v["state"], "id": v["id"], "reason": v["reason"], "seq": v["seq"]}

    def tv_action(self, seq, phase, ok=None, reason=None):
        """Page /tv : claim (une seule fois) puis done. -> (code HTTP, corps)."""
        if phase == "claim":
            act = self.tv_actions.claim(seq)
            return (200, {"ok": True, "action": act}) if act is not None else (409, {"ok": False, "error": "gone"})
        if phase == "done":
            changed = self.tv_actions.done(seq, ok, reason if isinstance(reason, str) else None)
            return (200 if changed else 409), {"ok": changed}
        return 400, {"ok": False, "error": "bad_phase"}

    # -- OpenTV Canvas (canvas_tv) : raisons d'occupation du service, vue TV, routes du propriétaire, aperçu agent
    def _canvas_reasons(self):
        r = set()
        with self.lock:
            if self.setup_mode or self.device is None:
                r.add("setup")
            if self.agents_mode:
                r.add("assistants")
        if self.muse.busy():
            r.add("muse_setup")
        st = self.playback.view()["state"]
        r.update({"loading": ("loading",), "playing": ("playback",), "closing": ("closing",),
                  "closed": ("returning",), "unavailable": ("returning",)}.get(st, ()))
        return r

    def canvas_sync(self):
        self.canvas.sync(self._canvas_reasons())

    def canvas_view(self):
        self.canvas_sync()
        return self.canvas.view()

    def canvas_human(self, kind):
        """Geste humain ordinaire de la page /tv (hors feuille de confirmation) : annule l'aperçu AVANT l'action."""
        return self.canvas.human(kind)

    def canvas_tv_post(self, action, body):
        self.canvas_sync()
        return self.canvas.post(action, body)

    def canvas_agent_preview(self, agent, changes):
        """Point d'intégration de l'Agent Port (tâche 4) : aperçu proposé par l'identité authentifiée `agent`.
        -> résultat de la façade agent ; canvas.CanvasError au code nu sinon. Jamais de confirmation ici.
        Sous agent_tv_gate : aucune action d'assistant (file ENTIÈRE, en file ou réclamée) ni lancement ne s'insère
        entre la vérification et la création de l'aperçu."""
        with self.agent_tv_gate:
            self.canvas_sync()
            return self.canvas.preview(agent, changes, blocked=self.tv_actions.busy())

    def canvas_agent_cancel(self, agent, preview_id):
        return self.canvas.agent_cancel(agent, preview_id)
