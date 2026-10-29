"""Configuration : fichier env lu (jamais exécuté), valeurs validées, chemins dérivés, état de l'appareil 0600."""
import json
import os
import re
import time

from . import validate
from .agents import atomic_write_json

ENV_FILE = "/etc/opentv/opentv.env"
KEY_RE = re.compile(r"OPENTV_[A-Z0-9_]{1,40}")
SOURCE_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")
DEFAULTS = {
    "OPENTV_STATE_DIR": "/var/lib/opentv",
    "OPENTV_RUN_DIR": "/run/opentv",
    "OPENTV_SHARE_DIR": "/opt/opentv/share/ui",
    "OPENTV_QRCODE_JS": "",
    "OPENTV_FORYOU_ENGINE_DIR": "/opt/opentv/lib/foryou",
    "OPENTV_CATALOGUE_FILE": "",
    "OPENTV_FORYOU_SOURCE_ID": "films",
    "OPENTV_TV_PORT": "8080",
    "OPENTV_SETUP_PORT": "8081",
    "OPENTV_HOTSPOT_ENABLED": "1",
    "OPENTV_HOTSPOT_GRACE_S": "60",
    "OPENTV_HOTSPOT_MAX_S": "1200",
    "OPENTV_SETUP_TOKEN_TTL_S": "600",
    "OPENTV_WIFI_IFNAME": "wlan0",
    "OPENTV_AGENT_SOCKET": "",
    # fichiers clients des agents émis par root : dossier root:root 0755 (tmpfiles), aucune composante d'un home
    "OPENTV_AGENT_CLIENTS_DIR": "/etc/opentv/agents",
    "OPENTV_AGENT_GROUP": "opentv-agent",
    "OPENTV_SERVICE_USER": "opentv",
    "OPENTV_TV_CLIENT_USER": "opentv-kiosk",
    "OPENTV_NM_DIR": "/etc/NetworkManager/system-connections",
    # HDMI-CEC (opentv-cec.timer) : déclaration Playback « OpenTV » + touche OK. Jamais de réveil de la TV ni de
    # réannonce périodique.
    "OPENTV_CEC_BOOTSTRAP": "1",
    # 1 = UNE annonce <Active Source> par démarrage de la boîte (fenêtre bornée après le boot, TV allumée seulement) :
    # sans elle la télécommande Samsung ne pilote plus OpenTV après un redémarrage. 0 = jamais d'annonce automatique.
    "OPENTV_CEC_BOOT_ANNOUNCE": "1",
    "OPENTV_CEC_KEYMAP": "1",       # 1 = touche CEC Select (KEY_OK, perdue dans Chromium) -> KEY_ENTER
    # télécommande sur téléphone (LAN, HTTP prototype) : ouverte seulement après la configuration, en ligne, sur
    # l'adresse LAN actuelle (jamais 0.0.0.0)
    "OPENTV_REMOTE_ENABLED": "1",
    "OPENTV_REMOTE_PORT": "8082",
    "OPENTV_REMOTE_MDNS": "1",      # 1 = QR vers <nom d'hôte>.local si ce nom désigne bien la boîte ; 0 = adresse
    # « Regarder » : source approuvée hors Git (root:opentv 0640, voir source.py) et contrôleur du lecteur
    # (opentv-player.service, compte opentv-kiosk) joint par socket Unix authentifié
    "OPENTV_SOURCE_FILE": "/etc/opentv/source.env",
    "OPENTV_PLAYER_SOCKET": "/run/opentv-player/player.sock",
    # porte de production (promotion.py) : témoin root:root écrit seulement après la validation terrain, jamais livré
    "OPENTV_PLAYBACK_MARKER": "/etc/opentv/playback-promoted",
    # manifeste de la livraison installée (root:root) : la promotion est liée à son empreinte (livraison entière)
    "OPENTV_INSTALL_MANIFEST": "/opt/opentv/MANIFEST.sha256",
    # aide des touches (seul détenteur du groupe input, compte opentv-keys) et compte du contrôleur du lecteur
    "OPENTV_PLAYER_KEYS_SOCKET": "/run/opentv-player-keys/keys.sock",
    "OPENTV_PLAYER_USER": "opentv-kiosk",
    "OPENTV_PLAYER_KEYS_USER": "opentv-keys",
    # rafraîchissement quotidien du catalogue (opentv-catalogue-refresh.timer, source approuvée seulement) : un candidat
    # est refusé sous OPENTV_CATALOGUE_MIN_ITEMS éléments, ou s'il perd plus de OPENTV_CATALOGUE_MAX_DROP_PCT % du
    # catalogue valide actuel (nombre ou site_id conservés) — l'ancien catalogue reste alors en place
    "OPENTV_CATALOGUE_MIN_ITEMS": "20",
    "OPENTV_CATALOGUE_MAX_DROP_PCT": "25",
}


def read_env_file(path, cap=65536):
    """KEY=VALUE (guillemets simples/doubles retirés). Seules les clés OPENTV_* sont retenues ; rien n'est évalué."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(cap)
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if KEY_RE.fullmatch(k):
            out[k] = v
    return out


def _port(v, name):
    if not re.fullmatch(r"[0-9]{1,5}", v or "") or not (int(v) == 0 or 1024 <= int(v) <= 65535):
        raise ValueError("config: %s invalide" % name)
    return int(v)


def _bool(v, name):
    if v not in ("0", "1"):
        raise ValueError("config: %s doit valoir 0 ou 1" % name)
    return v == "1"


def _num(v, lo, hi, name):
    try:
        f = float(v)
    except ValueError:
        raise ValueError("config: %s invalide" % name)
    if not lo <= f <= hi:
        raise ValueError("config: %s hors bornes" % name)
    return f


class Config:
    def __init__(self, env=None):
        e = dict(DEFAULTS)
        e.update({k: v for k, v in (env or {}).items() if KEY_RE.fullmatch(k)})
        self.raw = e
        self.state_dir = e["OPENTV_STATE_DIR"]
        self.run_dir = e["OPENTV_RUN_DIR"]
        self.share_dir = e["OPENTV_SHARE_DIR"]
        self.qrcode_js = e["OPENTV_QRCODE_JS"] or os.path.join(self.share_dir, "qrcode.js")
        self.engine_dir = e["OPENTV_FORYOU_ENGINE_DIR"]
        self.catalogue_file = e["OPENTV_CATALOGUE_FILE"] or os.path.join(self.state_dir, "catalogue.json")
        if not SOURCE_RE.fullmatch(e["OPENTV_FORYOU_SOURCE_ID"]):
            raise ValueError("config: OPENTV_FORYOU_SOURCE_ID invalide")
        self.source_id = e["OPENTV_FORYOU_SOURCE_ID"]
        self.tv_port = _port(e["OPENTV_TV_PORT"], "OPENTV_TV_PORT")
        self.setup_port = _port(e["OPENTV_SETUP_PORT"], "OPENTV_SETUP_PORT")
        self.hotspot_enabled = _bool(e["OPENTV_HOTSPOT_ENABLED"], "OPENTV_HOTSPOT_ENABLED")
        self.hotspot_grace_s = _num(e["OPENTV_HOTSPOT_GRACE_S"], 10, 3600, "OPENTV_HOTSPOT_GRACE_S")
        # durée maximale cumulée du Wi-Fi temporaire depuis son premier démarrage (par démarrage de la boîte)
        self.hotspot_max_s = _num(e["OPENTV_HOTSPOT_MAX_S"], 120, 3600, "OPENTV_HOTSPOT_MAX_S")
        self.token_ttl_s = _num(e["OPENTV_SETUP_TOKEN_TTL_S"], 60, 3600, "OPENTV_SETUP_TOKEN_TTL_S")
        if not re.fullmatch(r"[a-z][a-z0-9]{1,14}", e["OPENTV_WIFI_IFNAME"]):
            raise ValueError("config: OPENTV_WIFI_IFNAME invalide")
        self.wifi_ifname = e["OPENTV_WIFI_IFNAME"]
        self.agent_socket = e["OPENTV_AGENT_SOCKET"] or os.path.join(self.run_dir, "agent.sock")
        if not os.path.isabs(e["OPENTV_AGENT_CLIENTS_DIR"]):
            raise ValueError("config: OPENTV_AGENT_CLIENTS_DIR doit être absolu")
        self.agent_clients_dir = e["OPENTV_AGENT_CLIENTS_DIR"]
        for k in ("OPENTV_AGENT_GROUP", "OPENTV_SERVICE_USER", "OPENTV_TV_CLIENT_USER", "OPENTV_PLAYER_USER",
                  "OPENTV_PLAYER_KEYS_USER"):
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", e[k]):
                raise ValueError("config: %s invalide" % k)
        self.agent_group = e["OPENTV_AGENT_GROUP"]
        self.service_user = e["OPENTV_SERVICE_USER"]
        self.tv_client_user = e["OPENTV_TV_CLIENT_USER"]
        self.nm_dir = e["OPENTV_NM_DIR"]
        self.cec_bootstrap = _bool(e["OPENTV_CEC_BOOTSTRAP"], "OPENTV_CEC_BOOTSTRAP")
        self.cec_keymap = _bool(e["OPENTV_CEC_KEYMAP"], "OPENTV_CEC_KEYMAP")
        self.cec_boot_announce = _bool(e["OPENTV_CEC_BOOT_ANNOUNCE"], "OPENTV_CEC_BOOT_ANNOUNCE")
        self.remote_enabled = _bool(e["OPENTV_REMOTE_ENABLED"], "OPENTV_REMOTE_ENABLED")
        self.remote_port = _port(e["OPENTV_REMOTE_PORT"], "OPENTV_REMOTE_PORT")
        self.remote_mdns = _bool(e["OPENTV_REMOTE_MDNS"], "OPENTV_REMOTE_MDNS")
        self.source_file = e["OPENTV_SOURCE_FILE"]
        self.player_socket = e["OPENTV_PLAYER_SOCKET"]
        self.playback_marker = e["OPENTV_PLAYBACK_MARKER"]
        self.install_manifest = e["OPENTV_INSTALL_MANIFEST"]
        self.player_keys_socket = e["OPENTV_PLAYER_KEYS_SOCKET"]
        self.player_user = e["OPENTV_PLAYER_USER"]
        self.player_keys_user = e["OPENTV_PLAYER_KEYS_USER"]
        if not re.fullmatch(r"[0-9]{1,4}", e["OPENTV_CATALOGUE_MIN_ITEMS"]) or \
                not 1 <= int(e["OPENTV_CATALOGUE_MIN_ITEMS"]) <= 2000:
            raise ValueError("config: OPENTV_CATALOGUE_MIN_ITEMS invalide (1 à 2000)")
        self.catalogue_min_items = int(e["OPENTV_CATALOGUE_MIN_ITEMS"])
        if not re.fullmatch(r"[0-9]{1,2}", e["OPENTV_CATALOGUE_MAX_DROP_PCT"]) or \
                not 0 <= int(e["OPENTV_CATALOGUE_MAX_DROP_PCT"]) <= 90:
            raise ValueError("config: OPENTV_CATALOGUE_MAX_DROP_PCT invalide (0 à 90)")
        self.catalogue_max_drop_pct = int(e["OPENTV_CATALOGUE_MAX_DROP_PCT"])
        s = self.state_dir
        self.device_file = os.path.join(s, "device.json")
        self.setup_flag = os.path.join(s, "setup-requested")
        self.hotspot_state = os.path.join(s, "hotspot.json")
        self.hotspot_rearm = os.path.join(s, "hotspot-rearm")
        self.apply_state = os.path.join(s, "wifi-apply.json")
        self.jobs_dir = os.path.join(s, "netjobs")
        self.jobs_tmp = os.path.join(s, "netjobs-tmp")
        self.results_dir = os.path.join(s, "netresults")
        self.registry_file = os.path.join(s, "agents.json")
        self.audit_file = os.path.join(s, "audit.jsonl")
        self.foryou_state = os.path.join(s, "foryou")
        self.remote_file = os.path.join(s, "remote.json")
        self.source_state = os.path.join(s, "source")

    @classmethod
    def load(cls, path=ENV_FILE, environ=None):
        env = read_env_file(path)
        env.update({k: v for k, v in (os.environ if environ is None else environ).items() if KEY_RE.fullmatch(k)})
        return cls(env)

    def ensure_dirs(self):
        for d in (self.state_dir, self.jobs_dir, self.jobs_tmp, self.results_dir, self.foryou_state):
            os.makedirs(d, mode=0o700, exist_ok=True)
        os.makedirs(self.run_dir, mode=0o750, exist_ok=True)


def load_device(cfg):
    """-> {"name", "configured_at"} ou None (non configuré / illisible : fail-closed vers la configuration)."""
    try:
        with open(cfg.device_file, encoding="utf-8") as f:
            d = json.load(f)
        return {"name": validate.device_name(d["name"]), "configured_at": d.get("configured_at")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_device(cfg, data):
    d = {"v": 1, "name": validate.device_name(data["name"]), "configured_at": round(time.time(), 3)}
    return atomic_write_json(cfg.device_file, d)
