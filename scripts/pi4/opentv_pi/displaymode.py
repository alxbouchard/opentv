"""Mode HDMI exact du kiosque (Cage) : 1920x1080@60 par défaut, au démarrage ET après chaque branchement/rallumage.

Terrain Samsung (2026-11-01) : TV éteinte la nuit puis rallumée ⇒ le connecteur HDMI repasse « branché », wlroots
recrée la sortie et Cage l'active avec le mode PRÉFÉRÉ de l'EDID (3840x2160@30) alors que la surface Chromium reste
en 1920x1080 : l'accueil n'occupe plus que le quart supérieur gauche. `wlr-randr --mode 1920x1080` lancé une fois
au démarrage ne couvre ni ce rebranchement ni l'ambiguïté des fréquences (premier 1920x1080 annoncé : 23,976 Hz).

Deux points d'entrée, même logique (`Check`) :
- `apply` : lancé par l'ouvreur du kiosque (enfant de Cage, avant Chromium), quelques essais bornés ;
- `monitor` : `opentv-display-mode.service` (compte opentv-kiosk, sans capacité ni réseau IP) attend les uevents
  DRM du noyau (socket netlink, `select` sans délai quand rien n'est prévu : aucune scrutation) ; chaque événement
  `HOTPLUG=1` arme une rafale bornée de vérifications (1 s, puis 2, 4, 8 s… pendant 30 s) qui laisse wlroots
  recréer la sortie avant de la corriger. Au démarrage : même rafale, 60 s (attente bornée du socket Wayland).

Chaque vérification lit `wlr-randr` (argv fixe, délai borné), puis :
- socket Wayland absent / sortie absente (TV éteinte, débranchée) / sortie désactivée ⇒ rien ;
- mode courant = cible (largeur, hauteur exactes, fréquence à ±25 mHz) ⇒ rien (aucune écriture) ;
- cible absente de la liste annoncée par la TV ⇒ rien (jamais de mode « custom » fabriqué) ;
- sinon `wlr-randr --output <sortie> --mode <L>x<H>@<fréquence exacte annoncée>Hz` puis relecture.
Écritures bornées : au plus 3 par branchement (chaque uevent rouvre ce budget : une rafale de N recréations de la
sortie est corrigée N fois) et au plus 6 par 10 min, toutes rafales confondues. Si le plafond de 10 min a retenu une
correction et que la rafale finit hors cible, UNE seule vérification différée est programmée à l'instant où la fenêtre
se libère (`display_mode_deferred`, aucune lecture entre-temps ; annulée par un nouveau branchement, qui ouvre une
rafale ordinaire) : toute rafale finie se termine donc en mode cible, sans duel (un compositeur qui refuse l'écriture
épuise les 3 essais du branchement puis plus rien). Journal : une ligne JSON par changement d'état ou écriture, au plus
30 lignes par 10 min (puis une seule ligne `display_mode_log_muted`).
Retour à une version sans ce moniteur (≤ 0.7.4) : l'ancien installateur retire ses fichiers sans arrêter l'unité ; au
premier réveil suivant, le moniteur constate l'absence de son module ou de son lanceur et s'arrête (code 0,
`display_mode_uninstalled`) sans plus rien écrire.
"""
import errno
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
import time

WLR_RANDR = "/usr/bin/wlr-randr"
DEFAULT_OUTPUT = "HDMI-A-1"
DEFAULT_MODE = "1920x1080@60"
OUTPUT_RE = re.compile(r"HDMI-A-[12]")
MODE_RE = re.compile(r"(\d{3,4})x(\d{3,4})(?:@(\d{2,3})(?:\.(\d{1,3}))?(?:Hz)?)?")
TOLERANCE_MHZ = 25            # 60,000 Hz ≠ 59,940 Hz (60 mHz d'écart) ; arrondi d'affichage de wlr-randr absorbé
QUERY_TIMEOUT_S = 5.0
MAX_OUTPUT = 65536

SETTLE_S = 1.0                # premier contrôle 1 s après l'événement (wlroots recrée la sortie)
BURST_S = 30.0                # rafale après un branchement
STARTUP_BURST_S = 60.0        # démarrage du service : attente bornée du socket Wayland de Cage
MAX_INTERVAL_S = 8.0
BURST_APPLIES = 3
WINDOW_APPLIES, WINDOW_S = 6, 600.0
LOG_LINES, LOG_WINDOW_S = 30, 600.0

NETLINK_KOBJECT_UEVENT = 15
UEVENT_KERNEL_GROUP = 1
EX_UNAVAILABLE = 78           # RestartPreventExitStatus : environnement impossible (netlink refusé), jamais relancé


# --------------------------------------------------------------------------- configuration
def parse_target(text):
    """`1920x1080`, `1920x1080@60`, `1920x1080@59.94`, `…@60Hz` -> (largeur, hauteur, mHz) ; sans fréquence = 60 Hz
    exactement (jamais « le premier 1920x1080 annoncé »). Invalide -> None."""
    m = MODE_RE.fullmatch((text or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    hz, frac = m.group(3), m.group(4) or ""
    mhz = 60000 if hz is None else int(hz) * 1000 + int(frac.ljust(3, "0"))
    if not (320 <= w <= 7680 and 200 <= h <= 4320 and 23000 <= mhz <= 240000):
        return None
    return (w, h, mhz)


def fmt_mode(w, h, mhz):
    return "%dx%d@%d.%03d" % (w, h, mhz // 1000, mhz % 1000)


def config_from_env(env):
    """(sortie, cible, erreurs) depuis OPENTV_HDMI_OUTPUT / OPENTV_HDMI_MODE ; valeur invalide ⇒ défaut + erreur."""
    errors = []
    out = (env.get("OPENTV_HDMI_OUTPUT") or DEFAULT_OUTPUT).strip()
    if not OUTPUT_RE.fullmatch(out):
        errors.append("output_invalid")
        out = DEFAULT_OUTPUT
    raw = env.get("OPENTV_HDMI_MODE") or DEFAULT_MODE
    target = parse_target(raw)
    if target is None:
        errors.append("mode_invalid")
        target = parse_target(DEFAULT_MODE)
    return out, target, errors


# --------------------------------------------------------------------------- lecture de wlr-randr
HEAD_RE = re.compile(r'^(\S+) "')
MODE_LINE_RE = re.compile(r"^\s+(\d+)x(\d+) px(?:, ([0-9]+(?:\.[0-9]+)?) Hz)?(?: \(([a-z, ]+)\))?\s*$")


def parse_wlr_randr(text):
    """Sortie texte de wlr-randr (0.2 à 0.4) -> {nom: {"enabled": bool|None, "modes": [(w, h, mhz, préféré,
    courant)]}}. Lignes inconnues ignorées (Make/Model/Serial/Adaptive Sync…)."""
    heads, cur, in_modes = {}, None, False
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        if m:
            cur = heads.setdefault(m.group(1), {"enabled": None, "modes": []})
            in_modes = False
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Enabled:"):
            cur["enabled"] = s.split(":", 1)[1].strip() == "yes"
            in_modes = False
        elif s == "Modes:":
            in_modes = True
        elif in_modes:
            mm = MODE_LINE_RE.match(line)
            if not mm:
                in_modes = False
                continue
            hz = mm.group(3)
            flags = [f.strip() for f in (mm.group(4) or "").split(",")]
            cur["modes"].append((int(mm.group(1)), int(mm.group(2)),
                                 int(round(float(hz) * 1000)) if hz else 0,
                                 "preferred" in flags, "current" in flags))
    return heads


def same_mode(mode, target):
    return mode[0] == target[0] and mode[1] == target[1] and abs(mode[2] - target[2]) <= TOLERANCE_MHZ


def decide(heads, output, target):
    """-> (résultat, mode courant ou None, mode à appliquer ou None). Résultats : absent | disabled | ok |
    unavailable | drift. Seul « drift » écrit."""
    head = heads.get(output)
    if head is None:
        return "absent", None, None
    current = next((m for m in head["modes"] if m[4]), None)
    if head["enabled"] is not True:
        return "disabled", current, None
    if current is not None and same_mode(current, target):
        return "ok", current, None
    cands = [m for m in head["modes"] if same_mode(m, target)]
    if not cands:
        return "unavailable", current, None
    best = min(cands, key=lambda m: abs(m[2] - target[2]))
    return "drift", current, best


# --------------------------------------------------------------------------- accès au compositeur (Cage)
def wayland_env(env=None, uid=None):
    """Environnement minimal pour wlr-randr, ou None si aucun socket Wayland du compte n'existe. Dans Cage :
    WAYLAND_DISPLAY hérité. Dans le service : /run/user/<uid>/wayland-N (socket appartenant au compte)."""
    env = os.environ if env is None else env
    uid = os.geteuid() if uid is None else uid
    rt = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % uid
    names = []
    wd = env.get("WAYLAND_DISPLAY") or ""
    if re.fullmatch(r"wayland-\d{1,2}", wd):
        names.append(wd)
    names += ["wayland-%d" % i for i in range(10) if "wayland-%d" % i != wd]
    for n in names:
        try:
            st = os.lstat(os.path.join(rt, n))
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid:
            return {"XDG_RUNTIME_DIR": rt, "WAYLAND_DISPLAY": n, "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
    return None


def run_wlr_randr(args, env, timeout=QUERY_TIMEOUT_S, binary=WLR_RANDR):
    """argv fixe, sans shell, délai borné, sortie bornée -> (code, texte). Absent/délai -> (127|124, "")."""
    try:
        p = subprocess.run([binary] + list(args), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, close_fds=True)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return p.returncode, p.stdout[:MAX_OUTPUT].decode("utf-8", "replace")


class Check:
    """Une vérification complète : lecture, décision, écriture éventuelle, relecture. Injection pour les tests."""

    def __init__(self, output, target, runner=run_wlr_randr, envfn=wayland_env):
        self.output, self.target, self.runner, self.envfn = output, target, runner, envfn

    def query(self):
        env = self.envfn()
        if env is None:
            return "no_wayland", None, None
        rc, text = self.runner([], env)
        if rc != 0:
            return "query_failed", None, env
        return "ok", parse_wlr_randr(text), env

    def __call__(self, may_apply=True):
        """-> dict {result, current?, applied?}. Résultats : no_wayland, query_failed, absent, disabled, ok,
        unavailable, applied, apply_failed, apply_unverified, suppressed."""
        st, heads, env = self.query()
        if st != "ok":
            return {"result": st}
        res, cur, best = decide(heads, self.output, self.target)
        out = {"result": res, "current": fmt_mode(*cur[:3]) if cur else None}
        if res != "drift":
            return out
        if not may_apply:
            out["result"] = "suppressed"
            return out
        mode = "%dx%d@%d.%03dHz" % (best[0], best[1], best[2] // 1000, best[2] % 1000)
        rc, _ = self.runner(["--output", self.output, "--mode", mode], env)
        out["applied"] = fmt_mode(*best[:3])
        if rc != 0:
            out["result"] = "apply_failed"
            out["rc"] = rc
            return out
        st, heads, _ = self.query()
        if st == "ok":
            res2, cur2, _ = decide(heads, self.output, self.target)
            if res2 == "ok":
                out["result"] = "applied"
                out["now"] = fmt_mode(*cur2[:3])
                return out
        out["result"] = "apply_unverified"
        return out


# --------------------------------------------------------------------------- journal borné
class Log:
    def __init__(self, write=None, mono=time.monotonic, lines=LOG_LINES, window=LOG_WINDOW_S):
        self.write = write or (lambda s: (sys.stderr.write(s + "\n"), sys.stderr.flush()))
        self.mono, self.lines, self.window = mono, lines, window
        self.stamps, self.muted = [], False

    def __call__(self, event, **fields):
        now = self.mono()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) >= self.lines:
            if not self.muted:
                self.muted = True
                self.write(json.dumps({"event": "display_mode_log_muted", "window_s": int(self.window)}))
            return
        self.muted = False
        self.stamps.append(now)
        rec = {"event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        self.write(json.dumps(rec, sort_keys=True))


# --------------------------------------------------------------------------- rafales et budget d'écritures
class Monitor:
    """État pur (horloge fournie) : quand vérifier, combien écrire. `on_event` arme/prolonge une rafale et rouvre le
    budget du branchement ; `step` exécute la vérification échue ; `timeout` = délai jusqu'à la prochaine (None :
    attendre un événement sans délai — aucune scrutation)."""

    def __init__(self, check, log, target_txt="", output=""):
        self.check, self.log = check, log
        self.target_txt, self.output = target_txt, output
        self.next_at = None
        self.burst_end = 0.0
        self.interval = SETTLE_S
        self.burst_applies = 0
        self.applies = []
        self.last_key = None
        self.checks = 0
        self.deferred = False

    def on_event(self, now, burst=None, reason="hotplug"):
        burst = BURST_S if burst is None else burst
        # chaque branchement reçu est journalisé (preuve terrain), y compris pendant une rafale (« extended ») ; une
        # tempête reste bornée par le journal (30 lignes / 10 min)
        extending = self.next_at is not None and not self.deferred
        self.log("display_mode_watch", reason=reason, output=self.output, target=self.target_txt,
                 extended=True if extending else None)
        if not extending:
            # nouvelle rafale (ou attente différée annulée : jamais une rafale étirée jusqu'à son échéance)
            self.next_at, self.burst_end, self.deferred = None, now, False
        # budget par branchement : chaque recréation de la sortie peut être corrigée ; plafond global inchangé
        self.burst_applies = 0
        self.burst_end = max(self.burst_end, now + burst)
        self.interval = SETTLE_S
        cand = now + SETTLE_S if reason == "hotplug" else now
        self.next_at = cand if self.next_at is None else min(self.next_at, cand)

    def timeout(self, now):
        return None if self.next_at is None else max(0.0, self.next_at - now)

    def window_full(self, now):
        self.applies = [t for t in self.applies if now - t < WINDOW_S]
        return len(self.applies) >= WINDOW_APPLIES

    def may_apply(self, now):
        return not self.window_full(now) and self.burst_applies < BURST_APPLIES

    def step(self, now):
        """Vérification échue -> résultat ; sinon None."""
        if self.next_at is None or now < self.next_at:
            return None
        self.checks += 1
        self.deferred = False
        r = self.check(may_apply=self.may_apply(now))
        if "applied" in r:
            self.burst_applies += 1
            self.applies.append(now)
        key = (r["result"], r.get("current"))
        if "applied" in r or key != self.last_key:
            self.log("display_mode_" + r["result"], output=self.output, target=self.target_txt,
                     current=r.get("current"), applied=r.get("applied"), now=r.get("now"), rc=r.get("rc"))
        self.last_key = key
        nxt = now + self.interval
        if nxt <= self.burst_end:
            self.next_at = nxt
            self.interval = min(self.interval * 2, MAX_INTERVAL_S)
        elif r["result"] == "suppressed" and self.burst_applies < BURST_APPLIES:
            # retenue avec des essais restants au branchement ⇒ par le seul plafond de 10 min (fenêtre pleine, donc
            # self.applies non vide) : rafale finie hors cible. UNE vérification à l'instant où la plus ancienne
            # écriture sort de la fenêtre (+ marge), aucune lecture d'ici là. Branchement qui a déjà épuisé ses 3
            # essais (compositeur qui refuse/rétablit) : rien, jamais de duel
            self.next_at = max(self.applies[0] + WINDOW_S + SETTLE_S, nxt)
            self.burst_end = self.next_at
            self.interval = SETTLE_S
            self.deferred = True
            self.log("display_mode_deferred", output=self.output, target=self.target_txt,
                     in_s=int(round(self.next_at - now)))
        else:
            self.next_at = None
            self.log("display_mode_idle", result=r["result"])
        return r


# --------------------------------------------------------------------------- uevents DRM du noyau
def parse_uevent(data):
    """Datagramme uevent du noyau (« action@devpath\\0CLÉ=VAL\\0… ») -> dict, ou None (libudev, illisible)."""
    if not data or data.startswith(b"libudev"):
        return None
    parts = data.split(b"\0")
    if b"@" not in parts[0]:
        return None
    env = {}
    for p in parts[1:]:
        k, sep, v = p.partition(b"=")
        if sep:
            env[k.decode("ascii", "replace")] = v.decode("utf-8", "replace")
    return env


def is_drm_hotplug(ev):
    """Changement de connecteur (HOTPLUG=1, avec ou sans CONNECTOR=) ou carte DRM ajoutée."""
    if not ev or ev.get("SUBSYSTEM") != "drm":
        return False
    return ev.get("HOTPLUG") == "1" or (ev.get("ACTION") == "add" and ev.get("DEVTYPE") == "drm_minor")


class UeventSource:
    """Socket netlink des uevents du noyau (groupe 1, réception permise sans privilège). Seuls les messages du noyau
    (pid 0) comptent. Débordement (ENOBUFS) = un événement possible : traité comme un branchement."""

    def __init__(self, sock=None):
        if sock is None:
            family = getattr(socket, "AF_NETLINK", None)
            if family is None:   # hors Linux (tests macOS) : aucune source d'événements
                raise OSError(errno.EAFNOSUPPORT, "netlink indisponible")
            sock = socket.socket(family, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0),
                                 NETLINK_KOBJECT_UEVENT)
            try:
                sock.bind((0, UEVENT_KERNEL_GROUP))
            except OSError:
                sock.close()
                raise
        self.sock = sock
        self.sock.setblocking(False)

    def fileno(self):
        return self.sock.fileno()

    def drain(self, limit=256):
        """-> nombre d'événements DRM pertinents lus (non bloquant, au plus `limit` datagrammes)."""
        n = 0
        for _ in range(limit):
            try:
                data, addr = self.sock.recvfrom(16384)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.ENOBUFS:   # file du socket débordée : messages perdus
                    n += 1
                    continue
                raise
            if isinstance(addr, tuple) and addr and addr[0] != 0:
                continue             # pas le noyau
            if is_drm_hotplug(parse_uevent(data)):
                n += 1
        return n

    def close(self):
        self.sock.close()


def monitor_files(launcher=None):
    """Fichiers dont dépend le moniteur en cours : ce module et, s'il est connu, le lanceur installé."""
    files = [os.path.abspath(__file__)]
    if launcher:
        files.append(os.path.abspath(launcher))
    return files


def installed_check(files):
    """-> fonction vraie tant que tous les fichiers existent (un stat par réveil, jamais de scrutation)."""
    files = tuple(files)
    return lambda: all(os.path.isfile(f) for f in files)


def run_monitor(mon, source, mono=time.monotonic, wait=select.select, stop=lambda: False, max_loops=None,
                installed=lambda: True):
    """Boucle d'événements : `select` sur le socket avec le délai de la prochaine vérification, ou SANS délai quand
    aucune n'est prévue. Après chaque réveil, fichiers du moniteur retirés (retour à ≤ 0.7.4) ⇒ arrêt propre avant toute
    vérification. Renvoie le nombre de tours (tests : preuve d'absence de scrutation)."""
    loops = 0
    mon.on_event(mono(), burst=STARTUP_BURST_S, reason="startup")
    while not stop() and (max_loops is None or loops < max_loops):
        loops += 1
        to = mon.timeout(mono())
        r, _, _ = wait([source], [], [], to)
        if not installed():
            mon.log("display_mode_uninstalled", output=mon.output)
            break
        if r:
            if source.drain():
                mon.on_event(mono())
        mon.step(mono())
    return loops


# --------------------------------------------------------------------------- points d'entrée
def cmd_apply(check, log, target_txt, output, attempts=(0.0, 0.5, 1.0, 2.0), sleep=time.sleep):
    """Ouvreur du kiosque : quelques essais bornés (≈ 3,5 s + lectures) tant que Wayland/la sortie manquent.
    Toujours 0 : ne retarde ni n'empêche jamais Chromium."""
    r = {"result": "not_run"}
    for i, delay in enumerate(attempts):
        if delay:
            sleep(delay)
        r = check()
        if r["result"] not in ("no_wayland", "query_failed", "absent"):
            break
    log("display_mode_" + r["result"], phase="kiosk_start", output=output, target=target_txt,
        current=r.get("current"), applied=r.get("applied"), now=r.get("now"), rc=r.get("rc"))
    return 0


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if len(argv) != 1 or argv[0] not in ("apply", "monitor", "status"):
        sys.stderr.write("usage: opentv-display-mode apply|monitor|status\n")
        return 2
    output, target, errors = config_from_env(env)
    target_txt = fmt_mode(*target)
    log = Log()
    for e in errors:
        log("display_mode_config_" + e, used_output=output, used_target=target_txt)
    check = Check(output, target)
    if argv[0] == "status":
        r = check(may_apply=False)
        r.update(output=output, target=target_txt)
        if r["result"] == "suppressed":
            r["result"] = "drift"
        sys.stdout.write(json.dumps(r, sort_keys=True) + "\n")
        return 0
    if argv[0] == "apply":
        return cmd_apply(check, log, target_txt, output)
    try:
        source = UeventSource()
    except OSError as e:
        log("display_mode_events_unavailable", errno=e.errno)
        return EX_UNAVAILABLE
    mon = Monitor(check, log, target_txt, output)
    launcher = sys.argv[0] if sys.argv and os.path.basename(sys.argv[0] or "") == "opentv-display-mode" else None
    try:
        run_monitor(mon, source, installed=installed_check(monitor_files(launcher)))
    finally:
        source.close()
    return 0
