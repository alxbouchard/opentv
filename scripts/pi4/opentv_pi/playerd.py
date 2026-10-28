"""Contrôleur du lecteur « Regarder » (compte opentv-kiosk, opentv-player.service).

Rôle : préparer la fiche validée du site dans un Chromium séparé, CACHÉE derrière un écran OpenTV « Chargement… »,
puis révéler directement le seul lecteur dominant plein écran, avec au plus UN clic central lié au OK de l'utilisateur.
Le kiosque /tv (opentv-kiosk.service) n'est jamais arrêté ni rechargé.

Mécanisme sous Cage (compositeur à vue unique, pile de fenêtres, aucune API « cacher/montrer ») :
- la fenêtre du lecteur s'ouvre AU-DESSUS de /tv avec un onglet local « Chargement… » (data: statique, aucun réseau) ;
- la fiche du site est chargée dans un second onglet d'arrière-plan (Target.createTarget background) : jamais peint ;
- détection/agrandissement/vérification dans un monde isolé (assist.py) ; révélation = activation de cet onglet
  seulement si le lecteur couvre toute la fenêtre et qu'une grille de points ne touche que lui ;
- fermeture (Retour/Quitter/Accueil CEC-HID-téléphone, plafond, échec) = SIGKILL du groupe de processus du lecteur :
  Cage retire la fenêtre et /tv redevient visible ; profil temporaire supprimé ; « ended » seulement ensuite.

Sécurité : CDP par tube uniquement (--remote-debugging-pipe, jamais de port TCP) ; Chromium lancé par le
wrapper Raspberry Pi OS /usr/bin/chromium sans --no-sandbox/--disable-gpu/VA-API/V4L2 ; politique gérée dédiée
(montée par systemd à la place de celle du kiosque) ; téléchargements refusés. Cibles : jamais la boîte, la boucle
locale, RFC 1918/lien local/CGNAT ni un nom local (refus à l'ouverture, puis après résolution, avant tout lancement).
Navigation du cadre principal : chaque requête de document est mise en pause (CDP Fetch, armé avant la première
navigation) ; seule l'origine validée est poursuivie avant la révélation, aucune après — sinon requête bloquée et fin
de séance (« navigated »). Les documents des sous-cadres sont poursuivis sans que leur URL soit lue.

Cycle de vie : création des ressources (abonnement aux touches, profil, navigateur, CDP) et démontage sont
mutuellement exclusifs (td_lock) et idempotents ; close() n'attend jamais le verrou du clic et ne laisse aucun
orphelin. Porte de production relue pendant la séance : démotion ⇒ fermeture. Socket Unix authentifié par SO_PEERCRED (UID du service OpenTV, root pour l'administration).
Aucune URL n'est journalisée ni renvoyée. Porte de production : `open` refusé tant que la lecture n'est pas promue
sur le terrain (promotion.py), même si le service OpenTV la croit disponible.

Fenêtres surgissantes : auto-attachement CDP au niveau du navigateur avec `waitForDebuggerOnStart` — toute nouvelle
page est mise en pause AVANT d'exécuter un script ou de valider sa navigation. Une page qui n'est pas l'une de nos deux
(onglet « Chargement… », fiche) n'est jamais relancée : l'onglet attendu est d'abord réactivé (« Chargement… » avant
la révélation, le lecteur après), puis la surgissante est fermée — fermer un onglet inactif ne change pas la
sélection, la fiche non validée n'est donc jamais montrée. Mesuré sur Chromium réel (headless, test
TestRealChromiumSession) : la surgissante en pause ne demande même pas sa page et n'exécute aucun script. Limite du
navigateur (honnête) : Chromium active lui-même le nouvel onglet à sa création ; pendant l'aller-retour CDP (quelques
ms) un onglet VIDE peut être peint — à mesurer sur la TV. Aucune API CDP n'empêche la création d'une fenêtre demandée
par la page.

DevTools : `DeveloperToolsAvailability=1` est exigé par Chromium pour le tube CDP (la valeur 2 le coupe, mesuré sur
le Pi). Il reste sûr parce que (1) aucun port de débogage n'existe, seul ce processus détient le tube ; (2) la
politique bloque devtools://, chrome://, view-source: ; (3) toute cible DevTools ou page inconnue est fermée dès son
apparition (événements + balayage toutes les 0,5 s), et la séance se termine si elle persiste ; (4) l'aide des
touches signale F12, Ctrl+Maj+I/J/C, Ctrl+U : la séance est alors fermée (retour OpenTV, même carte) ; (5) les
télécommandes CEC/HID n'ont ni F12 ni Ctrl ; un clavier branché relève de l'accès physique.
"""
import glob
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse

from . import assist
from .cdp import CDP, CDPError, spawn_with_pipe
from .playerkeys import KeyClient, peer_uid  # noqa: F401  (peer_uid : réexporté pour les tests et la CLI)

SOCKET = "/run/opentv-player/player.sock"
RUN_DIR = "/run/opentv-player"
CHROMIUM = "/usr/bin/chromium"
SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REASON_RE = re.compile(r"^[a-z_-]{1,24}$")
MAX_REQ = 16384
# flags du profil lecteur : aucun flag de désactivation du sandbox ou du GPU, aucun flag VA-API ni V4L2 hérité
PLAYER_FLAGS = (
    "--kiosk", "--ozone-platform=wayland", "--noerrdialogs", "--disable-infobars", "--no-first-run",
    "--no-default-browser-check", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
    "--disable-features=Translate,MediaRouter", "--password-store=basic", "--overscroll-history-navigation=0",
    "--disable-pinch", "--deny-permission-prompts", "--disable-sync", "--check-for-update-interval=31536000",
    "--autoplay-policy=no-user-gesture-required",
)
# cibles qui peuvent être PEINTES : seules nos deux pages sont tolérées (jamais une surgissante ni DevTools)
VISIBLE_TYPES = ("page", "other", "webview", "app")
DEVTOOLS_PREFIXES = ("devtools://", "chrome-devtools://")
FORBIDDEN_FLAG_RE = re.compile(r"^--(no-sandbox|disable-gpu|disable-gpu-sandbox|remote-debugging-port|"
                               r"remote-debugging-address|enable-features=.*(Vaapi|V4L2)|use-gl=|"
                               r"enable-accelerated-video-decode|disable-web-security)", re.I)
LOADING_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>OpenTV</title><style>
html,body{margin:0;height:100%;background:#070b14;color:#f4f6fb;font-family:"Noto Sans","DejaVu Sans",sans-serif;
overflow:hidden;cursor:none}body{display:flex;flex-direction:column;align-items:center;justify-content:center;
background:radial-gradient(ellipse at 50% 40%,#1a2742 0%,#070b14 70%)}
.brand{font-size:44px;font-weight:700;letter-spacing:.04em;opacity:.92}.brand b{color:#6ea8ff}
.t{margin-top:38px;font-size:56px;font-weight:600;max-width:1500px;text-align:center;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}.s{margin-top:22px;font-size:30px;opacity:.7}
.sp{margin-top:46px;width:64px;height:64px;border-radius:50%;border:6px solid rgba(255,255,255,.18);
border-top-color:#6ea8ff;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body data-opentv-loading="1"><div class="brand">Open<b>TV</b></div><div class="t">{{TITLE}}</div>
<div class="sp"></div><div class="s">Chargement…</div></body></html>"""


def loading_url(title):
    t = html.escape((title or "")[:120], quote=True)
    return "data:text/html;charset=utf-8," + urllib.parse.quote(LOADING_HTML.replace("{{TITLE}}", t), safe="")


# noms qui désignent la boîte, le réseau local ou un service interne : jamais une cible du lecteur
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".home.arpa", ".localdomain",
                  ".corp", ".private")


def _ip(text):
    try:
        ip = ipaddress.ip_address(str(text).strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _ip_blocked(ip):
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or \
        ip.is_link_local


def host_blocked(host):
    """Nom ou littéral IP qui n'est pas une destination publique : boucle locale, localhost, RFC 1918, lien local,
    CGNAT, multicast, réservé, nom local (.local, .lan, …), nom sans point ou numérique obfusqué (0x7f.1, 2130706433)."""
    h = str(host or "").lower().rstrip(".")
    if not h or len(h) > 253:
        return True
    ip = _ip(h)
    if ip is not None:
        return _ip_blocked(ip)
    if "." not in h or h == "localhost" or h.endswith(LOCAL_SUFFIXES):
        return True
    return bool(re.fullmatch(r"[0-9a-fx.]+", h) and re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*", h))


def local_addresses(proc="/proc"):
    """Adresses de la boîte elle-même (IPv4 LOCAL de fib_trie, IPv6 de if_inet6) : la « boîte » n'est jamais une cible,
    même avec une adresse publique. Meilleur effort (hors Linux : vide)."""
    out = set()
    try:
        with open(os.path.join(proc, "net", "fib_trie"), encoding="ascii", errors="replace") as f:
            last = None
            for line in f.read(1 << 20).splitlines():
                m = re.search(r"\|--\s+([0-9.]+)\s*$", line)
                if m:
                    last = m.group(1)
                elif last and "/32 host LOCAL" in line:
                    out.add(last)
    except OSError:
        pass
    try:
        with open(os.path.join(proc, "net", "if_inet6"), encoding="ascii", errors="replace") as f:
            for line in f.read(1 << 20).splitlines():
                w = line.split()
                if w and len(w[0]) == 32:
                    ip = _ip(":".join(w[0][i:i + 4] for i in range(0, 32, 4)))
                    if ip is not None:
                        out.add(str(ip))
    except OSError:
        pass
    return out


def addr_blocked(addr, local=()):
    ip = _ip(addr)
    return ip is None or _ip_blocked(ip) or str(ip) in local


def resolve_host(host, timeout=3.0):
    """Adresses de `host` (getaddrinfo borné par un thread) ; [] si échec ou délai."""
    box = []

    def run():
        try:
            box.extend(sorted(set(ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))))
        except (OSError, UnicodeError):
            pass
    t = threading.Thread(target=run, daemon=True, name="player-dns")
    t.start()
    t.join(timeout)
    return list(box) if not t.is_alive() else []


def player_argv(profile, title, chromium=CHROMIUM, extra=()):
    argv = [chromium] + list(PLAYER_FLAGS) + list(extra) + ["--user-data-dir=" + profile, "--remote-debugging-pipe",
                                                           loading_url(title)]
    bad = [a for a in argv if FORBIDDEN_FLAG_RE.match(a)]
    if bad:
        raise ValueError("forbidden_flag")
    return argv


def _noop(event, **fields):
    pass


def wayland_env(uid=None, run_user="/run/user"):
    uid = os.getuid() if uid is None else uid
    rt = os.path.join(run_user, str(uid))
    socks = sorted(p for p in glob.glob(os.path.join(rt, "wayland-*")) if not p.endswith(".lock"))
    for p in socks:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_uid == uid:
            return {"XDG_RUNTIME_DIR": rt, "WAYLAND_DISPLAY": os.path.basename(p)}
    return None


def pids_with_marker(marker, proc="/proc"):
    """PID dont la ligne de commande contient `marker` (chemin du profil temporaire) : balayage de secours."""
    out = []
    for d in glob.glob(os.path.join(proc, "[0-9]*")):
        try:
            with open(os.path.join(d, "cmdline"), "rb") as f:
                if marker.encode() in f.read(65536):
                    out.append(int(os.path.basename(d)))
        except (OSError, ValueError):
            continue
    return out


class Session:
    """Une séance de lecture ; toutes les méthodes publiques sont sûres en concurrence et idempotentes."""

    def __init__(self, sid, url, origin, title, assist_s=15.0, settle_s=1.5, max_s=4 * 3600.0, env=None,
                 run_dir=RUN_DIR, chromium=CHROMIUM, log=_noop, popen=None, cdp_factory=CDP, mono=time.monotonic,
                 keys=True, key_watcher=KeyClient, keys_kw=None, extra_flags=(), alsa_card=None, stderr_path=None,
                 keys_wait_s=2.0, keys_lost_s=5.0, sweep_s=0.5, gate=None, gate_every_s=2.0, resolve=resolve_host,
                 local_addrs=local_addresses, private_ok=False, full_min_s=1.0, full_max_s=4.0):
        self.sid, self.url, self.origin, self.title = sid, url, origin, title
        self.assist_s, self.settle_s, self.max_s = assist_s, settle_s, max_s
        self.env, self.run_dir, self.chromium, self.log = env, run_dir, chromium, log
        self.popen = popen or self._popen
        self.cdp_factory, self.mono = cdp_factory, mono
        self.keys, self.key_watcher_cls, self.extra_flags, self.alsa_card = keys, key_watcher, extra_flags, alsa_card
        self.keys_kw = dict(keys_kw or {})
        self.keys_wait_s, self.keys_lost_s, self.sweep_s = keys_wait_s, keys_lost_s, sweep_s
        self.stderr_path = stderr_path   # diagnostic local seulement (jamais en production : stderr peut citer l'URL)
        self.gate, self.gate_every_s = gate, gate_every_s
        self.resolve, self.local_addrs, self.private_ok = resolve, local_addrs, private_ok
        self.full_min_s, self.full_max_s = full_min_s, full_max_s
        self.fast_checks = self.full_scans = 0
        self.blocked_navs = 0
        self.state, self.outcome, self.reason = "starting", None, None
        self.clicked, self.clicks, self.gone = False, 0, False
        self.cancel = threading.Event()
        self.ended = threading.Event()
        self.click_lock = threading.Lock()   # révélation/clic entre eux seulement ; close() ne l'attend JAMAIS
        # acquisition des ressources (aide des touches, profil, navigateur, CDP) et démontage mutuellement exclusifs :
        # une ressource n'est créée que sous ce verrou ET si l'annulation n'est pas posée ; le démontage (sous ce verrou,
        # annulation déjà posée) voit donc toute ressource déjà créée et aucune ne peut naître après lui
        self.td_lock = threading.RLock()
        self.torn = False
        self.attach_lock = threading.Lock()
        self.proc = self.cdp = self.watcher = None
        self.profile = None
        self.loading_tid = self.tid = self.sess = self.main_frame = self.known = None
        self.revealed_at = None
        self.t0 = mono()
        self.times = {}
        self._world = None
        self.popups = self.devtools = 0
        self.autoattach = False
        self.creating = False
        self.attached = {}          # targetId -> (sessionId, en pause)
        self.pending = []           # pages attachées pendant Target.createTarget (classées ensuite)
        self.handled = set()        # cibles indésirables déjà fermées/en cours de fermeture
        self.own_attached = threading.Event()
        self.loading_closed = False
        self.thread = None

    # -- lancement
    def _popen(self, argv, env, fds):
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        try:
            return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                    start_new_session=True, pass_fds=fds, close_fds=True)
        finally:
            if self.stderr_path:
                err.close()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name="player-" + self.sid[:6])
        self.thread.start()
        return self

    def _mark(self, k):
        self.times[k] = round(self.mono() - self.t0, 3)

    def _env(self):
        e = {"HOME": self.profile, "TMPDIR": self.profile, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "fr_FR.UTF-8",
             "XDG_CONFIG_HOME": os.path.join(self.profile, "config"), "XDG_CACHE_HOME": os.path.join(self.profile,
                                                                                                     "cache")}
        if self.alsa_card:
            e["ALSA_CARD"] = self.alsa_card
        e.update(self.env or {})
        return e

    def _spawn_thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _on_key(self, why):
        """Événement typé de l'aide des touches (jamais un code brut) : Retour/Quitter/Accueil ou raccourci DevTools."""
        if why == "devtools":   # fail-closed : impossible de prouver qu'aucun DevTools n'est peint -> fin de séance
            self.devtools += 1
            self._spawn_thread(self.close, "devtools")
        elif why in ("back", "exit", "home"):
            self._spawn_thread(self.close, why)

    def _acquire(self, make):
        """Crée et assigne une ressource sous td_lock, seulement si la séance n'est pas annulée -> False sinon."""
        with self.td_lock:
            if self.cancel.is_set() or self.torn:
                return False
            make()
            return True

    def _make_watcher(self):
        self.watcher = self.key_watcher_cls(self._on_key, **self.keys_kw).start()

    def _make_profile(self):
        os.makedirs(self.run_dir, mode=0o700, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="s-", dir=self.run_dir)

    def _make_browser(self):
        argv = player_argv(self.profile, self.title, self.chromium, self.extra_flags)
        self.proc, w, r = spawn_with_pipe(argv, self._env(), self.popen)   # assigné avant toute autre étape
        self.cdp = self.cdp_factory(w, r, on_event=self._event)

    def _target_ok(self):
        """Cible publique seulement : aucune adresse de boucle locale, RFC 1918, lien local, CGNAT, réservée ni de la
        boîte elle-même (résolution avant le lancement du navigateur)."""
        if self.private_ok:
            return None
        host = urllib.parse.urlsplit(self.url).hostname or ""
        if host_blocked(host):
            return "private_target"
        addrs = self.resolve(host) if self.resolve else []
        if not addrs:
            return "dns"
        local = self.local_addrs() if self.local_addrs else set()
        if any(addr_blocked(a, local) for a in addrs):
            return "private_target"
        return None

    def _run(self):
        try:
            if self.keys and not self._acquire(self._make_watcher):
                return self._finish("cancelled", self.reason)
            if self.env is None:
                return self._finish("error", "no_display")
            why = self._target_ok()
            if why:
                self.log("player_target_refused", why=why)
                return self._finish("error", why)
            if not self._acquire(self._make_profile):
                return self._finish("cancelled", self.reason)
            if not self._acquire(self._make_browser):
                return self._finish("cancelled", self.reason)
            self._mark("spawned")
            self._prepare()
        except CDPError as e:
            self._finish("crashed" if self.proc is not None and self.proc.poll() is not None else "error",
                         str(e)[:40].split(":")[0])
        except Exception as e:   # jamais de séance pendante : nettoyage complet dans tous les cas
            self.log("player_error", error=type(e).__name__)
            self._finish("error", "internal")

    def _prepare(self):
        c = self.cdp
        c.send("Browser.getVersion", timeout=20)
        self._mark("browser")
        try:
            c.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        except CDPError:
            pass
        pages = []
        end = self.mono() + 10
        while not pages:
            if self.cancel.is_set():
                return self._finish("cancelled", self.reason)
            pages = [t for t in c.send("Target.getTargets", timeout=3).get("targetInfos", [])
                     if t.get("type") == "page" and str(t.get("url", "")).startswith("data:")]
            if not pages:
                if self.mono() > end:
                    return self._finish("error", "no_loading_page")
                time.sleep(0.1)
        self.loading_tid = pages[0]["targetId"]
        # cibles déjà présentes (onglet « Chargement… », cibles internes du navigateur) : jamais traitées en surgissantes
        self.known = set(t.get("targetId") for t in c.send("Target.getTargets", timeout=3).get("targetInfos", []))
        # toute NOUVELLE page est mise en pause avant son premier script / sa navigation : une surgissante n'est jamais
        # relancée, seulement fermée (voir l'en-tête). Repli (navigateur sans auto-attachement) : fermeture à la découverte.
        try:
            c.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
            self.autoattach = True
        except CDPError:
            self.autoattach = False
        c.send("Target.setDiscoverTargets", {"discover": True})
        # fiche du site : onglet d'ARRIÈRE-PLAN, première navigation = la fiche (aucun historique où revenir)
        with self.attach_lock:
            self.creating = True
        try:
            self.tid = c.send("Target.createTarget", {"url": self.url, "background": True})["targetId"]
        finally:
            with self.attach_lock:
                self.creating = False
                pending, self.pending = [t for t in self.pending if t != self.tid], []
        for t in pending:   # page apparue pendant la création qui n'est pas la nôtre : surgissante, jamais relancée
            self._kill_target(t, True)
        paused = False
        end = self.mono() + 3
        while self.autoattach and self.mono() < end and not self.cancel.is_set():   # session auto-attachée (en pause)
            with self.attach_lock:
                if self.tid in self.attached:
                    self.sess, paused = self.attached[self.tid]
                    break
            self.own_attached.wait(0.05)
        if not self.sess:
            # Sans auto-attachement en pause, la première requête du document peut partir avant Fetch.enable.
            # Il serait alors impossible de prouver que toute navigation a été filtrée avant le premier octet :
            # échec fermé plutôt qu'un attachement manuel tardif.
            return self._finish("error", "autoattach_required")
        # onglet d'arrière-plan vu par la page comme une page visible et active (sans être peint à l'écran) : sans cela
        # le site garde son lecteur à opacité 0 tant que le document est « hidden » (mesuré sur le Pi), et l'assistance
        # ne trouve aucun lecteur. Pure émulation du cycle de vie : rien n'est lu ni modifié dans la page.
        # Cible en pause : ses commandes ne répondent qu'après la relance (vrai Chromium) ; elles sont donc envoyées
        # d'abord, dans l'ordre, puis la relance, puis les réponses attendues — l'émulation précède tout script.
        # navigation du cadre principal : chaque requête de document est d'abord mise en pause (Fetch, avant la relance,
        # donc dès la première navigation) ; seul un document de l'origine validée est poursuivi avant la révélation,
        # aucun après. Les documents des sous-cadres (iframe du lecteur) sont poursuivis tels quels, URL jamais lue.
        setup = [(c.post("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document",
                                                        "requestStage": "Request"}]}, session=self.sess), True),
                 (c.post("Page.enable", session=self.sess), True)]
        for meth, prm in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                          ("Page.setWebLifecycleState", {"state": "active"})):
            setup.append((c.post(meth, prm, session=self.sess), False))
        if paused:   # notre fiche seulement : relancée une fois l'émulation en file
            c.send("Runtime.runIfWaitingForDebugger", session=self.sess)
        for tok, required in setup:
            try:
                c.wait(tok, timeout=10)
            except CDPError:
                if required:
                    raise
        self.main_frame = c.send("Page.getFrameTree", session=self.sess)["frameTree"]["frame"]["id"]
        self.state = "preparing"
        self._mark("navigating")
        out = self._assist()
        if out != "revealed":
            return self._finish(out, None)
        self._monitor()

    # -- cibles : nos deux pages seulement ; toute autre surface peinte est fermée
    @staticmethod
    def _is_devtools(info):
        return str(info.get("url") or "").lower().startswith(DEVTOOLS_PREFIXES)

    def _unwanted(self, info):
        tid = info.get("targetId")
        if not tid or tid in (self.tid, self.loading_tid):
            return False
        if self._is_devtools(info):
            return True
        return info.get("type") in VISIBLE_TYPES and (self.known is None or tid not in self.known)

    def _strict(self, info):
        """Page ou DevTools : une fermeture impossible termine la séance (cible annexe « other » : best effort)."""
        return info.get("type") == "page" or self._is_devtools(info)

    def _nav_ok(self, url):
        """Document du cadre principal admis : même origine exacte que la fiche validée, sans identifiants, hôte public
        (avant la révélation seulement : après, toute navigation du cadre principal termine la séance)."""
        if self.state == "revealed" or self.cancel.is_set() or not isinstance(url, str):
            return False
        try:
            u = urllib.parse.urlsplit(url)
            u.port
        except ValueError:
            return False
        if u.username or u.password or "%s://%s" % (u.scheme, u.netloc) != self.origin:
            return False
        return self.private_ok or not host_blocked(u.hostname)

    def _on_request(self, msg, p):
        rid, sess = p.get("requestId"), msg.get("sessionId")
        if not rid or self.cdp is None:
            return
        try:
            if p.get("frameId") != (self.main_frame or self.tid):   # sous-cadre : jamais lu, poursuivi tel quel
                self.cdp.post("Fetch.continueRequest", {"requestId": rid}, session=sess)
                return
            if self._nav_ok((p.get("request") or {}).get("url")):
                self.cdp.post("Fetch.continueRequest", {"requestId": rid}, session=sess)
                return
            self.blocked_navs += 1
            self.cdp.post("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"}, session=sess)
        except CDPError:
            pass
        # navigation hors de la fiche validée (autre origine, réseau local, après révélation) : jamais chargée,
        # séance terminée (rien d'autre n'est montré)
        if not self.ended.is_set():
            self._spawn_thread(self._end_async, "navigated")

    def _event(self, msg):
        m, p = msg.get("method"), msg.get("params") or {}
        if m == "Fetch.requestPaused":
            if self.sess is not None and msg.get("sessionId") == self.sess:
                self._on_request(msg, p)
            return
        if m == "Target.attachedToTarget":
            info = p.get("targetInfo") or {}
            tid, sid, waiting = info.get("targetId"), p.get("sessionId"), bool(p.get("waitingForDebugger"))
            with self.attach_lock:
                self.attached[tid] = (sid, waiting)
                if tid is not None and tid == self.tid:
                    self.own_attached.set()
                    return
                if self.creating and self.tid is None and info.get("type") == "page" and \
                        tid not in (self.known or ()):
                    self.pending.append(tid)   # notre fiche (ou, impossible avant sa relance, une surgissante)
                    if len(self.pending) == 1:
                        self.own_attached.set()
                    return
            if self._unwanted(info):
                self._spawn_thread(self._kill_target, tid, self._strict(info))
            elif waiting and sid:   # cible annexe (service worker…) : relancée telle quelle, jamais lue
                self._spawn_thread(self._resume, sid)
        elif m in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p.get("targetInfo") or {}
            with self.attach_lock:
                creating = self.creating and self.tid is None
            if not creating and self._unwanted(info):
                self._spawn_thread(self._kill_target, info.get("targetId"), self._strict(info))
        elif m == "Target.targetDestroyed" and p.get("targetId") == self.tid and not self.cancel.is_set():
            self._spawn_thread(self._end_async, "gone")
        elif m == "Inspector.targetCrashed" or (m == "Target.targetCrashed" and p.get("targetId") == self.tid):
            self._spawn_thread(self._end_async, "crashed")
        elif m in ("Page.frameStartedLoading", "Page.frameNavigated") and msg.get("sessionId") == self.sess:
            fid = p.get("frameId") or (p.get("frame") or {}).get("id")
            if fid == self.main_frame and self.state == "revealed" and not (p.get("frame") or {}).get("parentId"):
                # le cadre principal quitte la fiche validée après révélation : séance terminée, rien d'autre affiché
                self._spawn_thread(self._end_async, "navigated")

    def _resume(self, sid):
        try:
            self.cdp.send("Runtime.runIfWaitingForDebugger", session=sid, timeout=3)
        except CDPError:
            pass

    def _refocus(self):
        """Onglet attendu au premier plan : « Chargement… » avant la révélation, le lecteur après."""
        want = self.tid if self.state == "revealed" else (None if self.loading_closed else self.loading_tid)
        if want:
            try:
                self.cdp.send("Target.activateTarget", {"targetId": want}, timeout=3)
            except CDPError:
                pass

    def _kill_target(self, tid, strict=True):
        """Surgissante/DevTools : onglet attendu réactivé AVANT la fermeture (aucun changement de sélection vers la
        fiche non validée), jamais relancée. Échec de fermeture d'une page/DevTools : fin de séance (jamais une page
        étrangère laissée visible)."""
        with self.attach_lock:
            if not tid or tid in self.handled:
                return
            self.handled.add(tid)
        self.popups += 1
        self._refocus()
        try:
            self.cdp.send("Target.closeTarget", {"targetId": tid}, timeout=3)
            self.log("player_popup_closed")
        except CDPError:
            if strict and not self.ended.is_set():
                self._finish("error", "popup")
            return
        self._refocus()

    def sweep(self):
        """Balayage : ferme toute cible peinte qui n'est pas l'une de nos pages. -> nb de cibles DevTools restantes
        (déjà visées par une fermeture précédente)."""
        try:
            infos = self.cdp.send("Target.getTargets", timeout=3).get("targetInfos", [])
        except (CDPError, AttributeError):
            return 0
        left = 0
        for info in infos:
            if not self._unwanted(info):
                continue
            with self.attach_lock:
                seen = info.get("targetId") in self.handled
            if seen and self._is_devtools(info):
                left += 1
            if not seen:
                self._kill_target(info.get("targetId"), self._strict(info))
        return left

    def _end_async(self, outcome):
        if not self.ended.is_set():
            self._finish(outcome, outcome)

    def _world_eval(self, expr, await_promise=False, timeout=3):
        frame = self.cdp.send("Page.getFrameTree", session=self.sess, timeout=3)["frameTree"]["frame"]
        key = (frame.get("id"), frame.get("loaderId"))
        if self._world is None or self._world[0] != key:
            ctx = self.cdp.send("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "opentv-assist",
                                                             "grantUniveralAccess": False},
                                session=self.sess, timeout=3)["executionContextId"]
            self._world = (key, ctx)
        r = self.cdp.send("Runtime.evaluate", {"expression": expr, "contextId": self._world[1], "returnByValue": True,
                                               "awaitPromise": await_promise}, session=self.sess, timeout=timeout)
        if r.get("exceptionDetails"):
            raise CDPError("assist_js")
        return (r.get("result") or {}).get("value")

    def _assist(self, interval=0.4):
        end = self.mono() + max(0.0, self.assist_s)
        last = "no-player"
        while True:
            if self.cancel.is_set():
                return "cancelled"
            try:
                r = self._world_eval(assist.PROBE_JS)
            except (CDPError, KeyError, TypeError):
                r = None
            if isinstance(r, dict) and isinstance(r.get("origin"), str):
                if r["origin"] != self.origin:
                    last = "foreign"
                elif r.get("state") == "found":
                    break
                elif r.get("state") == "ambiguous":
                    last = "ambiguous"
                else:
                    last = "no-player"
            if self.mono() + interval > end:
                return last
            self.cancel.wait(interval)
        self._mark("found")
        try:
            if self._world_eval(assist.MAXIMIZE_JS) != "ok":
                return "gone"
        except (CDPError, KeyError, TypeError):
            return "error"
        if self.cancel.wait(max(0.0, self.settle_s)):
            return "cancelled"
        chk, end2 = None, max(end, self.mono() + 2.0)
        while True:   # révélation seulement quand toute la fenêtre est ce lecteur
            try:
                chk = self._world_eval(assist.CHECK_JS)
            except (CDPError, KeyError, TypeError):
                chk = None
            if assist.valid_point(chk):
                break
            if self.mono() + interval > end2 or self.cancel.is_set():
                st = chk.get("state") if isinstance(chk, dict) else None
                return "cancelled" if self.cancel.is_set() else (st if st in assist.OUTCOMES else "error")
            self.cancel.wait(interval)
        return self._reveal(chk)

    def _keys_ready(self):
        """Sortie garantie : sans liaison armée avec l'aide des touches ET au moins un périphérique physique capable
        (CLOSE_KEYS) réellement ouvert par l'aide, la télécommande ne pourrait pas fermer : jamais de révélation."""
        if not self.keys:
            return True
        w = self.watcher
        if w is None or not w.armed.wait(max(0.0, self.keys_wait_s)):
            return False
        n = getattr(w, "devices", 0)
        return isinstance(n, int) and not isinstance(n, bool) and n >= 1

    def _gate_ok(self):
        if self.gate is None:
            return True
        try:
            return self.gate() is True
        except Exception:   # porte illisible : fermée
            return False

    def _reveal(self, chk):
        if not self._keys_ready():
            self.reason = self.reason or "keys_unavailable"
            return "error"
        if not self._gate_ok():   # démotion pendant la préparation : jamais de révélation
            self.reason = self.reason or "demoted"
            return "cancelled"
        self.sweep()   # aucune surgissante ni DevTools ouverte au moment de l'activation
        with self.click_lock:   # jamais de révélation ni de clic après Retour
            if self.cancel.is_set():
                return "cancelled"
            self.cdp.send("Target.activateTarget", {"targetId": self.tid})
            self.state = "revealed"
            self.revealed_at = self.mono()
            self._mark("revealed")
        self.log("player_revealed", ms=int(self.times["revealed"] * 1000))
        try:
            self.loading_closed = True
            self.cdp.send("Target.closeTarget", {"targetId": self.loading_tid}, timeout=3)
        except CDPError:
            pass
        try:   # onglet réellement peint (2 images) avant le clic : sinon le routage vers l'iframe peut le perdre
            self._world_eval(assist.PAINTED_JS, await_promise=True, timeout=3)
        except (CDPError, KeyError, TypeError):
            pass
        self.cancel.wait(0.25)
        with self.click_lock:   # UN clic central hérité du OK, seulement si ce point est toujours le lecteur
            if self.cancel.is_set() or self.clicks:
                return "revealed"
            try:
                chk2 = self._world_eval(assist.CHECK_JS)
            except (CDPError, KeyError, TypeError):
                chk2 = None
            if assist.valid_point(chk2) and not self.cancel.is_set():
                x, y = chk2["x"], chk2["y"]
                self.clicks += 1
                try:
                    for typ, extra in (("mouseMoved", {}), ("mousePressed", {"button": "left", "buttons": 1,
                                                                             "clickCount": 1})):
                        if self.cancel.is_set():   # Retour pendant le clic : rien de plus n'est envoyé
                            return "revealed"
                        self.cdp.send("Input.dispatchMouseEvent", dict({"type": typ, "x": x, "y": y}, **extra),
                                      session=self.sess)
                    if self.cancel.is_set():
                        return "revealed"
                    # relâchement : envoyé, donc clic livré (jamais renvoyé). Vrai Chromium : si ce clic ouvre une
                    # surgissante (mise en pause puis fermée), l'accusé de l'événement peut ne jamais revenir — attente
                    # bornée, sans effet sur la séance
                    tok = self.cdp.post("Input.dispatchMouseEvent",
                                        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0,
                                         "clickCount": 1}, session=self.sess)
                    self.clicked = True
                    self._mark("clicked")
                    try:
                        self.cdp.wait(tok, timeout=2)
                    except CDPError:
                        pass
                except CDPError:
                    pass
        return "revealed"

    def _monitor(self, interval=None):
        """Pendant la lecture : plafond de durée, survie du navigateur, porte de production toujours promue, lecteur
        toujours seul à l'écran, aucune surgissante ni DevTools, liaison des touches de fermeture toujours armée.

        Cadence : à chaque tick (sweep_s), contrôle RAPIDE (couverture, style du lecteur, contextes d'empilement et
        transparence des ancêtres, couche supérieure, grille de 25 points) ; balayage COMPLET du DOM (calques à z-index
        maximal, pseudo-éléments, racines fantômes) seulement si le DOM a changé (MutationObserver) et au plus toutes
        les full_min_s, et dans tous les cas au moins toutes les full_max_s (styles changés sans mutation)."""
        interval = self.sweep_s if interval is None else interval
        bad = dev = 0
        keys_down_since = None
        last_full = last_gate = self.mono()
        while not self.cancel.wait(interval):
            now = self.mono()
            if now - (self.revealed_at or self.t0) > self.max_s:
                return self._finish("revealed", "cap")
            if self.proc is not None and self.proc.poll() is not None or (self.cdp and self.cdp.closed):
                return self._finish("crashed", "crashed")
            if self.gate is not None and now - last_gate >= self.gate_every_s:
                last_gate = now
                if not self._gate_ok():   # démotion à chaud : la séance s'arrête (retour OpenTV, même carte)
                    return self._finish("revealed", "demoted")
            if self.keys and self.watcher is not None:
                if self.watcher.armed.is_set():
                    keys_down_since = None
                elif keys_down_since is None:
                    keys_down_since = now
                elif now - keys_down_since > self.keys_lost_s:
                    return self._finish("revealed", "keys_lost")
            dev = dev + 1 if self.sweep() else 0
            if dev >= 2:   # DevTools toujours là après une fermeture demandée : la séance s'arrête
                return self._finish("revealed", "devtools")
            try:
                chk = self._world_eval(assist.CHECK_FAST_JS)
                self.fast_checks += 1
            except (CDPError, KeyError, TypeError):
                chk = None
            if assist.valid_point(chk):
                now = self.mono()
                if (chk.get("dirty") is True and now - last_full >= self.full_min_s) or \
                        now - last_full >= self.full_max_s:
                    last_full = now
                    try:
                        chk = self._world_eval(assist.CHECK_JS)
                        self.full_scans += 1
                    except (CDPError, KeyError, TypeError):
                        chk = None
            if assist.valid_point(chk):
                bad = 0
                continue
            bad += 1
            if bad == 1:
                try:   # style retiré par la page : réappliqué une fois (rien n'est supprimé de la page)
                    self._world_eval(assist.MAXIMIZE_JS)
                except (CDPError, KeyError, TypeError):
                    pass
                last_full = -1e9   # contrôle suivant complet
            elif bad >= 2:
                st = chk.get("state") if isinstance(chk, dict) else "error"
                return self._finish(st if st in assist.OUTCOMES else "error", "player_lost")

    # -- fin de séance
    def close(self, reason="back"):
        """Fermeture demandée (Retour, service, plafond, démotion) : jamais bloquée par un clic en cours (aucun verrou
        de clic attendu ; l'annulation posée interdit tout envoi suivant et le démontage tue le navigateur, ce qui
        réveille toute commande CDP en attente). Bloquant jusqu'à disparition du lecteur et du profil."""
        reason = reason if isinstance(reason, str) and REASON_RE.match(reason) else "other"
        if self.reason is None:
            self.reason = reason
        self.cancel.set()
        self._finish(self.outcome or ("revealed" if self.state == "revealed" else "cancelled"), reason)
        return self.ended.wait(10)

    def _finish(self, outcome, reason):
        with self.td_lock:
            if self.ended.is_set():
                return
            self.cancel.set()
            if self.outcome is None:
                self.outcome = outcome if outcome in assist.OUTCOMES else "error"
            if self.reason is None:
                self.reason = reason if isinstance(reason, str) and REASON_RE.match(reason or "") else None
            self._teardown()
            self.state = "ended"
            self._mark("ended")
            self.ended.set()
        self.log("player_ended", outcome=self.outcome, reason=self.reason, clicked=self.clicked,
                 popups=self.popups, devtools=self.devtools, gone=self.gone,
                 ms=int(self.times.get("ended", 0) * 1000))

    def _teardown(self):
        """Sous td_lock, annulation posée : plus aucune ressource ne peut être créée (voir _acquire). Idempotent."""
        self.torn = True
        w = self.watcher
        if w is not None:
            w.stop_ev.set()
        if self.proc is not None:
            try:   # SIGKILL immédiat du groupe : la fenêtre disparaît sans aucune navigation intermédiaire
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        if self.cdp is not None:   # commandes CDP en attente réveillées (CDPError) : aucun thread ne reste bloqué
            self.cdp.close()
        if self.profile:   # processus détachés du groupe (crashpad…) : balayage par chemin du profil
            for _ in range(20):
                left = [p for p in pids_with_marker(self.profile) if p != os.getpid()]
                if not left:
                    break
                for p in left:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(0.05)
            shutil.rmtree(self.profile, ignore_errors=True)
        self.gone = (self.proc is None or self.proc.poll() is not None) and \
            not (self.profile and (os.path.exists(self.profile) or pids_with_marker(self.profile)))
        if w is not None:
            w.stop()

    def status(self):
        return {"sid": self.sid, "state": self.state, "outcome": self.outcome, "reason": self.reason,
                "gone": self.gone if self.ended.is_set() else False, "clicked": self.clicked,
                "times": dict(self.times)}


class Controller:
    """Une séance à la fois. `heartbeat_s` : sans interrogation du service, la séance se ferme d'elle-même.

    `gate()` : porte de production (promotion terrain) relue à chaque `open` — False ⇒ refus `not_promoted`, quel
    que soit l'avis du service. `keys_ok()` : aide des touches joignable et authentifiée (sinon aucune sortie par la
    télécommande : refus `keys_unavailable`)."""

    def __init__(self, session_factory=Session, log=_noop, mono=time.monotonic, heartbeat_s=20.0,
                 gate=lambda: False, keys_ok=lambda: True, **session_kw):
        self.factory, self.log, self.mono, self.heartbeat_s = session_factory, log, mono, heartbeat_s
        self.gate, self.keys_ok = gate, keys_ok
        self.kw = session_kw
        self.lock = threading.Lock()
        self.s = None
        self.seen = mono()

    def _gate(self):
        try:
            return self.gate() is True
        except Exception:   # porte illisible : fermée
            return False

    def handle(self, req):
        op = req.get("op")
        if op == "ping":   # le service n'affiche « Regarder » que si les deux sont vrais
            return {"ok": True, "promoted": self._gate(), "keys": bool(self.keys_ok())}
        if op == "open":
            return self.open(req)
        if op == "status":
            return self.status(req.get("sid"))
        if op == "close":
            return self.close(req.get("sid"), req.get("reason"))
        if op == "close_all":
            return self.close(None, req.get("reason") or "service")
        return {"ok": False, "error": "bad_op"}

    def open(self, req):
        sid, url, origin = req.get("sid"), req.get("url"), req.get("origin")
        if not isinstance(sid, str) or not SID_RE.match(sid):
            return {"ok": False, "error": "bad_sid"}
        u = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        try:
            port = u.port
        except ValueError:
            port = -1
        if u.scheme != "https" or not u.hostname or u.username or u.password or len(url) > 600 or \
                origin != "https://" + u.netloc or u.fragment or port not in (None, 443) or host_blocked(u.hostname):
            return {"ok": False, "error": "bad_target"}   # jamais la boîte, le réseau local ni un nom local
        nums = {}
        for k, lo, hi, dv in (("assist_s", 1, 60, 15.0), ("settle_s", 0, 10, 1.5), ("max_s", 30, 6 * 3600, 14400.0)):
            v = req.get(k, dv)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                return {"ok": False, "error": "bad_" + k}
            nums[k] = float(v)
        title = req.get("title") if isinstance(req.get("title"), str) else ""
        if not self._gate():
            self.log("player_open_refused", why="not_promoted")
            return {"ok": False, "error": "not_promoted"}
        if not self.keys_ok():
            self.log("player_open_refused", why="keys_unavailable")
            return {"ok": False, "error": "keys_unavailable"}
        with self.lock:
            if self.s is not None and not self.s.ended.is_set():
                if self.s.sid == sid:
                    return {"ok": True, "status": self.s.status()}
                return {"ok": False, "error": "busy"}
            self.seen = self.mono()
            kw = dict(self.kw)
            kw.update(nums)
            kw.setdefault("gate", self._gate)   # porte relue PENDANT la séance : une démotion la ferme
            if "env" not in kw:
                kw["env"] = wayland_env()
            self.s = self.factory(sid, url, origin, title[:200], log=self.log, **kw)
            self.s.start()
            self.log("player_open", sid=sid[:6])
            return {"ok": True, "status": self.s.status()}

    def status(self, sid):
        with self.lock:
            s = self.s
            self.seen = self.mono()
        if s is None or (sid is not None and s.sid != sid):
            return {"ok": True, "status": None}
        return {"ok": True, "status": s.status()}

    def close(self, sid, reason):
        with self.lock:
            s = self.s
        if s is None or (sid is not None and s.sid != sid):
            return {"ok": True, "status": None, "gone": True}
        t0 = self.mono()
        s.close(reason if isinstance(reason, str) else "service")
        return {"ok": True, "status": s.status(), "gone": s.gone, "ms": int((self.mono() - t0) * 1000)}

    def tick(self):
        with self.lock:
            s, idle = self.s, self.mono() - self.seen
        if s is not None and not s.ended.is_set() and idle > self.heartbeat_s:
            self.log("player_service_lost")
            threading.Thread(target=s.close, args=("service_lost",), daemon=True).start()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        self.request.settimeout(5)
        uid = peer_uid(self.request)
        if uid is None or uid not in srv.allowed_uids:
            srv.log("player_peer_refused")
            return self._send({"ok": False, "error": "forbidden"})
        buf = b""
        try:
            while b"\n" not in buf and len(buf) <= MAX_REQ:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            return
        if len(buf) > MAX_REQ or b"\n" not in buf:
            return self._send({"ok": False, "error": "bad_request"})
        try:
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self._send({"ok": False, "error": "bad_json"})
        if not isinstance(req, dict):
            return self._send({"ok": False, "error": "bad_json"})
        try:
            res = srv.controller.handle(req)
        except Exception as e:
            srv.log("player_handler_error", error=type(e).__name__)
            res = {"ok": False, "error": "internal"}
        self._send(res)

    def _send(self, obj):
        try:
            self.request.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, controller, allowed_uids, log=_noop):
        self.controller, self.allowed_uids, self.log = controller, frozenset(allowed_uids), log
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        old = os.umask(0o111)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o666)   # l'accès réel est décidé par SO_PEERCRED (UID du service OpenTV, root)


def sweep_stale(run_dir, log=_noop):
    """Au démarrage : profils et navigateurs orphelins d'un contrôleur interrompu supprimés."""
    n = 0
    for d in glob.glob(os.path.join(run_dir, "s-*")):
        for p in pids_with_marker(d):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    if n:
        log("player_stale_swept", profiles=n)
    return n


def serve(path=SOCKET, allowed_uids=(0,), log=_noop, stop=None, **kw):
    run_dir = kw.get("run_dir", RUN_DIR)
    sweep_stale(run_dir, log)
    ctl = Controller(log=log, **kw)
    srv = Server(path, ctl, allowed_uids, log=log)
    threading.Thread(target=srv.serve_forever, daemon=True, name="player-sock").start()
    stop = stop or threading.Event()
    log("player_ready")
    while not stop.wait(1.0):
        ctl.tick()
    with ctl.lock:
        s = ctl.s
    if s is not None:
        s.close("shutdown")
    srv.shutdown()
    srv.server_close()
    try:
        os.unlink(path)
    except OSError:
        pass


def new_sid():
    return secrets.token_urlsafe(18)
