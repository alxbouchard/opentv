"""HDMI-CEC : reprise de la télécommande après le retour de la TV depuis une application native (Samsung).

Terrain (Samsung d'Alex, 2026-10-30) : en ouvrant YouTube natif, la TV diffuse <Active Source> avec l'adresse de la
TV (0.0.0.0) ; en le quittant, elle diffuse <Routing Change> 0.0.0.0 → adresse de la boîte puis <Set Stream Path>
vers cette adresse. OpenTV redevient visible mais la TV ne lui transmet plus aucune touche. La réactivation explicite
(`sudo opentv-pi cec --activate` : question d'alimentation, <Active Source>, <Menu Status>, TV allumée seulement) suffit
à rétablir la télécommande. Ce module la rejoue automatiquement, UNE fois, quand — et seulement quand — la TV émet
elle-même cette séquence exacte vers notre adresse (opentv-cec-return.service, compte opentv).

Observation (passive) : `cec-ctl -d <dev> -s -r -m`, mode moniteur du noyau (CAP_NET_ADMIN, seule capacité de
l'unité, réseau privé) : aucun mode suiveur ni initiateur, rien n'est émis, aucune touche n'est lue, consommée ni
produite (le noyau continue seul à transformer les <User Control> en événements Linux). Chaque trame est validée par
ses octets bruts (`Raw:`), recoupés avec l'en-tête et les adresses décodées ; toute trame utile incohérente, coupée ou
trop longue, toute ligne inconnue, toute perte de messages annoncée remet l'état à zéro (échec fermé). Un
redémarrage du moniteur ou du service repart d'un état vide : une séquence à moitié vue ne déclenche jamais rien.

Séquence exigée, dans l'ordre, émise par la TV (adresse logique 0) en diffusion :
1. <Active Source> 0.0.0.0 (application native), au plus NATIVE_MAX_S avant l'étape 2 ;
2. <Routing Change> de 0.0.0.0 vers l'adresse physique lue de l'adaptateur (jamais une constante) ;
3. <Set Stream Path> vers cette même adresse, au plus ROUTE_TO_STREAM_MAX_S après l'étape 2.
Toute autre <Active Source>, tout routage ailleurs, une mise en veille, une duplication ou un ordre différent annule.
Après SETTLE_S de calme (touches déjà reçues, autre annonce ou autre source ⇒ annulation), la reprise prend le verrou
existant de /run/opentv-cec (attente bornée), relit l'adaptateur (le nôtre, même adresse, adresse logique obtenue)
puis appelle `cecboot.announce` : la même séquence sûre que la réactivation explicite, refusée si la TV n'est pas
allumée, jamais de commande de réveil ni d'alimentation (cecboot.NEVER_EMIT). Le témoin de l'annonce de démarrage n'est
ni lu ni écrit. Résultat `announced` = <Active Source> transmis et acquitté par le bus (preuve de TRANSMISSION, pas de
la reprise effective des touches, qui reste un gate terrain).

Bornes d'usage normal (allers-retours rapides YouTube ↔ OpenTV) : seule une annonce transmise consomme le délai
anti-doublon COOLDOWN_S et le quota MAX_PER_HOUR ; un retour exact pendant ce délai est REPORTÉ à sa fin (jamais
perdu ; touches ou autre annonce entre-temps ⇒ annulé). busy / refus / échec ne bloquent pas le retour suivant ; toutes
les tentatives restent bornées par MAX_ATTEMPTS_PER_HOUR (protection du bus).

Prêt et échec véridiques : un moniteur n'est « prêt » qu'après l'événement initial `Initial Event: State Change` du
noyau avec l'adresse physique et le masque attendus (journal `cec_return_ready`, une fois par moniteur) ; rien n'est
armé avant. La sortie d'erreur de cec-ctl est lue (bornée, jamais journalisée telle quelle) et classée en codes
stables (STDERR_CODES_ALL). Refus du mode moniteur (cec-ctl l'écrit sur stderr puis sort avec 0), cec-ctl absent,
adaptateur illisible ou PRE_READY_FAILURES_MAX échecs génériques consécutifs avant « prêt » (fin, sortie non nulle,
silence, état attendu sans événement INITIAL) ⇒ sortie EXIT_FATAL (78, RestartPreventExitStatus) : l'unité passe
`failed` au lieu de rester `active` sans surveiller.

Changement d'état attendu de l'adaptateur (TV éteinte/rallumée, HDMI rebranché, réclamation en cours) — après
« prêt », ou événement initial dont l'adresse physique/le masque diffèrent de l'adaptateur lu avant « prêt » (l'état
bouge) : jamais compté vers la sortie 78 ; la découverte est relue toutes les STATE_CHANGE_RETRY_S (2 s) pendant
REDISCOVER_FAST_WINDOW_S, puis toutes les REDISCOVER_SLOW_S (5 s), tant qu'aucun moniteur n'est de nouveau « prêt »
(jamais l'attente aveugle DISCOVERY_RETRY_S, réservée au démarrage du service). Adaptateur absent : le service reste
actif en attente explicite (`cec_return_waiting`, `rediscover: true`), sans jamais se dire « prêt » ni armer quoi que
ce soit. Une discordance ne blanchit pas les échecs génériques (compteur ni incrémenté ni remis à zéro) ; la MÊME
discordance sur le même adaptateur lu MISMATCH_STUCK_MAX fois de suite n'est plus un état qui bouge ⇒ 78
(`adapter_state_inconsistent`). Seul un arrêt inattendu après « prêt » suit le palier RESTART_BACKOFF_S.
"""
import collections
import os
import re
import select
import signal
import subprocess
import sys
import time

from . import cecboot
from .cec import DEV_RE
from .nethelper import KILL_GRACE_S, SAFE_ENV, default_runner

# -- messages (opcodes CEC 1.4/2.0) et adresses logiques
ACTIVE_SOURCE = 0x82
ROUTING_CHANGE = 0x80
ROUTING_INFORMATION = 0x81
SET_STREAM_PATH = 0x86
INACTIVE_SOURCE = 0x9D
STANDBY = 0x36
USER_CONTROL_PRESSED = 0x44
# longueur exacte (en-tête + opcode + paramètres) des messages qui comptent pour la séquence
WATCHED_LEN = {ACTIVE_SOURCE: 4, ROUTING_CHANGE: 6, SET_STREAM_PATH: 4, ROUTING_INFORMATION: 4,
               INACTIVE_SOURCE: 4, STANDBY: 2}
TV = 0
BROADCAST = 15
TV_ROOT_PA = b"\x00\x00"

# -- fenêtres et bornes
NATIVE_MAX_S = 12 * 3600.0          # application native ouverte (film YouTube compris) avant le retour
ROUTE_TO_STREAM_MAX_S = 5.0         # capture : les deux messages du retour arrivent ensemble
SETTLE_S = 1.5                      # calme après <Set Stream Path> avant l'annonce (reprise en quelques secondes)
COOLDOWN_S = 5.0                    # anti-doublon après une annonce transmise (report, jamais abandon)
MAX_PER_HOUR = 30                   # annonces transmises par heure glissante
MAX_ATTEMPTS_PER_HOUR = 60          # toutes tentatives (busy, refus, échec compris) : borne d'inondation du bus
LOCK_WAIT_S = 10.0
LOCK_POLL_S = 0.5
RECOVER_BUDGET_S = 30.0             # relecture + question d'alimentation + 2 émissions, chacune bornée
DISCOVER_BUDGET_S = 25.0
DISCOVERY_RETRY_S = 30.0            # adaptateur absent / pas encore déclaré par opentv-cec.timer : relecture seule
IDLE_POLL_S = 30.0
RESTART_BACKOFF_S = (5, 10, 30, 60, 120, 300)   # arrêts INATTENDUS après « prêt » seulement
HEALTHY_RUN_S = 600.0               # un moniteur resté vivant aussi longtemps remet le délai de relance au minimum
STATE_CHANGE_RETRY_S = 2.0          # changement d'état attendu de l'adaptateur : relecture rapide, sans palier
REDISCOVER_FAST_WINDOW_S = 60.0     # … toutes les 2 s pendant cette fenêtre après le changement d'état
REDISCOVER_SLOW_S = 5.0             # … puis toutes les 5 s jusqu'au prochain « prêt » (attente explicite, jamais 30 s)
MISMATCH_STUCK_MAX = 10             # même discordance, même adaptateur lu, à la suite : incohérence (pas un état qui bouge)
READY_TIMEOUT_S = 10.0              # événement initial attendu au plus tard ainsi après le lancement
PRE_READY_FAILURES_MAX = 3          # échecs génériques consécutifs avant « prêt » (lancement, refus, fin, illisible)
PRE_READY_RETRY_S = 5.0
EXIT_FATAL = 78                     # EX_CONFIG ; opentv-cec-return.service : RestartPreventExitStatus=78
FINISH_WAIT_S = 2.0                 # fin de cec-ctl : attente bornée du code de sortie et de stderr
STDERR_MAX_BYTES = 4096             # stderr conservé (le reste est lu et jeté : jamais de blocage du tube)
STDERR_MAX_LINES = 16
MAX_LINE_BYTES = 512
MAX_FRAME_LINES = 16
MONITOR_OPTS = ("-s", "-r", "-m")   # sans infos pilote, octets bruts, mode moniteur (passif)
# stderr de cec-ctl -> codes stables (le texte lui-même n'est jamais journalisé)
STDERR_CODES = (("monitor_refused", re.compile(r"Selecting monitor mode failed")),
                ("open_failed", re.compile(r"Failed to open ")),
                ("device_disconnected", re.compile(r"Device was disconnected")))
STDERR_CODES_ALL = tuple(c for c, _ in STDERR_CODES) + ("stderr_unclassified",)
FATAL_NOW = ("monitor_refused", "cec_ctl_missing")   # aucune relance ne peut corriger

PA_TXT = r"[0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f]"
HEADER_RE = re.compile(r"(Received from|Transmitted by) [A-Za-z0-9 ]{1,40} to [A-Za-z0-9 ]{1,40} "
                       r"\(([0-9]{1,2}) to ([0-9]{1,2})\): (.{1,80})")
OPNAME_RE = re.compile(r"([A-Z][A-Z0-9_]{0,40}) \(0x([0-9a-f]{2})\)")
RAW_RE = re.compile(r"\tRaw:((?: 0x[0-9a-f]{2}){1,16}) \(.{1,16}\)")
ARG_RE = re.compile(r"\t[a-z][a-z0-9-]{0,40}: .{0,120}")
PA_ARG_RE = re.compile(r"\t(phys-addr|orig-phys-addr|new-phys-addr): (" + PA_TXT + r")")
EVENT_PREFIX = r"(?:Initial |[0-9][0-9:.]{0,30}s?: )?"
STATE_RE = re.compile(r"(Initial |[0-9][0-9:.]{0,30}s?: )?Event: State Change: PA: (" + PA_TXT + r"), LA mask: 0x([0-9a-f]{4})"
                      r"(?:, Conn Info: (?:yes|no))?")
LOST_RE = re.compile(r"(?:" + EVENT_PREFIX + r"Event: Lost [0-9]{1,9} messages|\(warn: .{1,40} events were lost\))")
# arguments d'adresse physique décodés par cec-ctl, recoupés avec les octets bruts
PA_ARGS = {ACTIVE_SOURCE: (("phys-addr", 2),), SET_STREAM_PATH: (("phys-addr", 2),),
           ROUTING_INFORMATION: (("phys-addr", 2),), INACTIVE_SOURCE: (("phys-addr", 2),),
           ROUTING_CHANGE: (("orig-phys-addr", 2), ("new-phys-addr", 4))}

Frame = collections.namedtuple("Frame", "rx initiator dest opcode params")


def classify_stderr(data):
    """Octets stderr (déjà bornés) -> code stable ou None (rien). Seules les STDERR_MAX_LINES premières lignes, chacune
    tronquée à MAX_LINE_BYTES, sont examinées ; le texte n'est jamais rendu."""
    lines = [x[:MAX_LINE_BYTES].decode("utf-8", "replace") for x in bytes(data).split(b"\n")[:STDERR_MAX_LINES]]
    for code, rx in STDERR_CODES:
        if any(rx.search(line) for line in lines):
            return code
    return "stderr_unclassified" if any(line.strip() for line in lines) else None


def pa_bytes(pa):
    """« a.b.c.d » (lu sur l'adaptateur) -> 2 octets ; ValueError si invalide."""
    if not isinstance(pa, str) or not re.fullmatch(PA_TXT, pa):
        raise ValueError("bad_physical_address")
    a, b, c, d = (int(x, 16) for x in pa.split("."))
    return bytes(((a << 4) | b, (c << 4) | d))


def monitor_argv(dev):
    """argv fixe du moniteur passif ; seul un /dev/cecN valide est accepté."""
    if not isinstance(dev, str) or not dev.startswith("/dev/") or not DEV_RE.fullmatch(dev[5:]):
        raise ValueError("bad_cec_device")
    argv = ["cec-ctl", "-d", dev] + list(MONITOR_OPTS)
    if any(o in cecboot.NEVER_EMIT for o in argv):
        raise ValueError("cec_wake_or_power_command_forbidden")
    return argv


# --------------------------------------------------------------------------- analyse de la sortie du moniteur
class Parser:
    """Lignes de `cec-ctl -s -r -m` -> éléments : ("frame", Frame) | ("state", pa, masque, initial) | ("lost",) |
    ("invalid", raison). Une trame = en-tête, arguments indentés, puis la ligne `Raw:` qui la termine."""

    def __init__(self):
        self.cur = None

    def _open_partial(self):
        out = [("invalid", "partial_frame")] if self.cur is not None else []
        self.cur = None
        return out

    def feed(self, line):
        if line == "":
            return []
        m = HEADER_RE.fullmatch(line)
        if m:
            out = self._open_partial()
            name = OPNAME_RE.match(m.group(4))
            self.cur = {"rx": m.group(1) == "Received from", "src": int(m.group(2)), "dst": int(m.group(3)),
                        "op": int(name.group(2), 16) if name else None, "args": []}
            return out
        if line.startswith("\t"):
            if self.cur is None:
                return [("invalid", "orphan_line")]
            m = RAW_RE.fullmatch(line)
            if m:
                cur, self.cur = self.cur, None
                return [self._frame(cur, bytes(int(x, 16) for x in m.group(1).split()))]
            # trames utiles : arguments au format décodé exigé ; autres trames (fournisseur…) : seulement bornées
            strict = self.cur["op"] in WATCHED_LEN or self.cur["op"] == USER_CONTROL_PRESSED
            if (strict and not ARG_RE.fullmatch(line)) or len(self.cur["args"]) >= MAX_FRAME_LINES:
                self.cur = None
                return [("invalid", "bad_argument_line")]
            self.cur["args"].append(line)
            return []
        out = self._open_partial()
        m = STATE_RE.fullmatch(line)
        if m:
            return out + [("state", m.group(2), int(m.group(3), 16), m.group(1) == "Initial ")]
        if LOST_RE.fullmatch(line):
            return out + [("lost",)]
        return out + [("invalid", "unknown_line")]

    @staticmethod
    def _frame(cur, raw):
        src, dst = raw[0] >> 4, raw[0] & 0x0F
        if (src, dst) != (cur["src"], cur["dst"]):
            return ("invalid", "header_mismatch")
        op = raw[1] if len(raw) > 1 else None
        if cur["op"] is not None and cur["op"] != op:
            return ("invalid", "opcode_mismatch")
        if op in WATCHED_LEN:
            if len(raw) != WATCHED_LEN[op] or cur["op"] != op:
                return ("invalid", "bad_length")
            decoded = {}
            for a in cur["args"]:
                m = PA_ARG_RE.fullmatch(a)
                if m:
                    decoded[m.group(1)] = pa_bytes(m.group(2))
            for name, off in PA_ARGS.get(op, ()):
                if decoded.get(name) != raw[off:off + 2]:
                    return ("invalid", "argument_mismatch")
        return ("frame", Frame(cur["rx"], src, dst, op, raw[2:]))


# --------------------------------------------------------------------------- détection de la séquence de retour
class Detector:
    """Machine à états pure (horloge fournie) : application native -> routage vers nous -> chemin vers nous."""

    def __init__(self, pa, la_mask):
        self.pa = pa_bytes(pa)
        self.la_mask = la_mask
        self.native_at = self.route_at = self.pending = None
        self.last = "idle"

    def reset(self, why):
        self.native_at = self.route_at = self.pending = None
        self.last = why

    def deadline(self):
        return self.pending

    def defer(self, until):
        """Reprise due mais dans le délai anti-doublon : reportée (touches/autre annonce d'ici là ⇒ annulée)."""
        self.pending = until
        self.last = "deferred"

    def take(self, now):
        """Reprise due ? (consommée : une seule par séquence)."""
        if self.pending is not None and now >= self.pending:
            self.pending = None
            self.last = "due"
            return True
        return False

    def frame(self, fr, now):
        op = fr.opcode
        if not fr.rx:                       # nos propres émissions (minuterie, --activate, reprise)
            if op == ACTIVE_SOURCE:
                self.reset("own_active_source")
            return
        if op == USER_CONTROL_PRESSED:
            if self.pending is not None and fr.initiator == TV and fr.dest != BROADCAST and \
                    (self.la_mask >> fr.dest) & 1:
                self.reset("keys_already_forwarded")
            return
        if op not in WATCHED_LEN:
            return
        from_tv = fr.initiator == TV and fr.dest == BROADCAST
        if op == ACTIVE_SOURCE and from_tv and fr.params == TV_ROOT_PA:
            self.reset("native_app")
            self.native_at = now
        elif op == ROUTING_CHANGE and from_tv and self.native_at is not None and self.route_at is None and \
                self.pending is None and now - self.native_at <= NATIVE_MAX_S and \
                fr.params[:2] == TV_ROOT_PA and fr.params[2:] == self.pa:
            self.route_at = now
            self.last = "routed_to_us"
        elif op == SET_STREAM_PATH and from_tv and self.route_at is not None and \
                now - self.route_at <= ROUTE_TO_STREAM_MAX_S and fr.params == self.pa:
            self.reset("return_detected")
            self.pending = now + SETTLE_S
        else:
            self.reset("sequence_broken")


# --------------------------------------------------------------------------- sous-processus borné
class PopenLines:
    """`cec-ctl -m` en flux : lignes stdout bornées (MAX_LINE_BYTES) ; stderr lu en même temps (jamais de tube plein
    qui bloquerait cec-ctl), conservé sur STDERR_MAX_BYTES au plus puis classé (classify_stderr) ; groupe de processus
    tué à la fermeture."""

    def __init__(self, argv):
        self.p = subprocess.Popen(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=SAFE_ENV, start_new_session=True, close_fds=True)
        self.fd = self.p.stdout.fileno()
        self.efd = self.p.stderr.fileno()
        self.buf = b""
        self.err = bytearray()
        self.lines = collections.deque()
        self.eof = False
        self.err_eof = False

    def _read_err(self):
        chunk = os.read(self.efd, 4096)
        if not chunk:
            self.err_eof = True
            return
        room = STDERR_MAX_BYTES - len(self.err)
        if room > 0:
            self.err += chunk[:room]                # au-delà : lu et jeté

    def readline(self, timeout):
        """-> ("line", texte) | ("timeout", None) | ("eof", None) | ("oversize", None)."""
        if self.lines:
            return "line", self.lines.popleft()
        if self.eof:
            return "eof", None
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            fds = [self.fd] + ([] if self.err_eof else [self.efd])
            r, _, _ = select.select(fds, [], [], max(0.0, deadline - time.monotonic()))
            if self.efd in r:
                self._read_err()
            if self.fd in r:
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    self.eof = True
                    return "eof", None
                self.buf += chunk
                *done, self.buf = self.buf.split(b"\n")
                if any(len(x) > MAX_LINE_BYTES for x in done) or len(self.buf) > MAX_LINE_BYTES:
                    return "oversize", None
                self.lines.extend(x.decode("utf-8", "replace") for x in done)
                if self.lines:
                    return "line", self.lines.popleft()
            if not r or time.monotonic() >= deadline:
                return "timeout", None

    def diagnosis(self, wait=FINISH_WAIT_S):
        """Après la fin de stdout : stderr lu jusqu'à sa fin et code de sortie, attente bornée (un petit-enfant qui
        garderait stderr ouvert ne bloque rien). -> {"error": code stable | None, "exit_status": int | None}."""
        deadline = time.monotonic() + wait
        while not self.err_eof:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            r, _, _ = select.select([self.efd], [], [], rem)
            if r:
                self._read_err()
        try:
            rc = self.p.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            rc = None
        return {"error": classify_stderr(self.err), "exit_status": rc}

    def close(self):
        if self.p.poll() is None:
            for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
                try:
                    os.killpg(self.p.pid, sig)
                except OSError:
                    pass
                try:
                    self.p.wait(wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        else:
            try:                                    # cec-ctl fini : un petit-enfant resté dans le groupe est tué
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass
        for f in (self.p.stdout, self.p.stderr):
            try:
                f.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- service
class Monitor:
    def __init__(self, root="/", runner=default_runner, spawn=PopenLines, mono=time.monotonic, sleep=time.sleep,
                 state_dir=cecboot.STATE_DIR, log=None):
        self.root, self.runner, self.spawn, self.mono, self.sleep = root, runner, spawn, mono, sleep
        self.state_dir = state_dir
        self.log = log or _log
        self.last_announced = None
        self.announced = collections.deque()    # annonces transmises (quota MAX_PER_HOUR)
        self.attempts = collections.deque()     # toutes tentatives (borne MAX_ATTEMPTS_PER_HOUR)
        self._why = None
        self._churn = False                     # relecture après un changement d'état attendu (journal véridique)
        self.last_state = None                  # (adresse physique, masque) discordants vus par le dernier moniteur

    def discover(self):
        """Premier adaptateur vc4 branché (même choix que l'amorçage) s'il est déjà Playback « OpenTV » avec son
        adresse logique -> (dev, adresse physique, masque) ; sinon None (raison dans self._why). Lecture seule.
        `read_failed` : des adaptateurs existent mais AUCUN n'a pu être lu (cec-ctl absent, accès refusé)."""
        run = cecboot._Run(self.runner, self.mono, DISCOVER_BUDGET_S)
        why = "no_adapter"
        devs, readable = [], 0
        try:
            devs = cecboot.list_devices(self.root)
            for dev in devs:
                info = run.read(dev)
                if info is None:
                    continue
                readable += 1
                if not cecboot.is_vc4(info) or not cecboot.connected(info):
                    continue
                kind = cecboot.classify(info)
                if kind != "ours":
                    why = "not_ours_" + kind
                elif not info.get("mask"):
                    why = "claim_pending"
                else:
                    self._why = None
                    return dev, info["physical_address"], info["mask"]
                break
            if devs and not readable:
                why = "read_failed"
        except cecboot.BudgetExceeded:
            why = "budget_exceeded"
        if why != self._why:
            self.log({"event": "cec_return_waiting", "reason": why, "rediscover": self._churn})
        self._why = why
        return None

    def recover(self, dev, pa):
        """Même annonce sûre que `opentv-pi cec --activate`, sous le verrou existant ; aucun témoin touché.
        `announced` = transmission acquittée, pas une preuve que la TV renvoie les touches."""
        t0 = self.mono()
        lock = cecboot._lock(self.state_dir)
        while lock is None and self.mono() + LOCK_POLL_S <= t0 + LOCK_WAIT_S:
            self.sleep(LOCK_POLL_S)
            lock = cecboot._lock(self.state_dir)
        if lock is None:
            return {"result": "busy"}
        run = cecboot._Run(self.runner, self.mono, RECOVER_BUDGET_S)
        try:
            info = run.read(dev)
            if info is None or not cecboot.is_vc4(info) or cecboot.classify(info) != "ours" or \
                    not info.get("mask") or not cecboot.connected(info) or info["physical_address"] != pa:
                return {"result": "adapter_changed"}
            ann, ok = cecboot.announce(run, dev, pa)
            return dict(ann, result="announced" if ok else ann["result"])
        except cecboot.BudgetExceeded:
            return {"result": "budget_exceeded"}
        finally:
            os.close(lock)

    def _attempt(self, dev, pa, det):
        now = self.mono()
        for q in (self.attempts, self.announced):
            while q and now - q[0] >= 3600.0:
                q.popleft()
        base = {"event": "cec_return_recovery", "device": dev, "physical_address": pa}
        if len(self.announced) >= MAX_PER_HOUR or len(self.attempts) >= MAX_ATTEMPTS_PER_HOUR:
            self.log(dict(base, result="rate_limited"))
            return
        if self.last_announced is not None and now - self.last_announced < COOLDOWN_S:
            det.defer(self.last_announced + COOLDOWN_S)
            self.log(dict(base, result="cooldown_deferred"))
            return
        self.attempts.append(now)
        res = self.recover(dev, pa)
        if res.get("result") == "announced":   # seule une annonce transmise consomme délai et quota
            self.last_announced = self.mono()
            self.announced.append(self.last_announced)
        self.log(dict(base, elapsed_s=round(self.mono() - now, 3), **res))

    def _end(self, src, kind):
        if kind != "eof":
            return kind
        diag = src.diagnosis()
        if diag.get("error"):
            return diag["error"]
        return "exit_nonzero" if diag.get("exit_status") not in (0, None) else "eof"

    def watch(self, dev, pa, mask):
        """Un moniteur, état vide au départ -> (raison de sa fin, prêt). Rien n'est armé avant l'événement initial
        attendu ; rien n'est émis hors de `_attempt`. cec-ctl est fermé sur toute sortie (Detector compris)."""
        try:
            src = self.spawn(monitor_argv(dev))
        except (FileNotFoundError, PermissionError):
            return "cec_ctl_missing", False
        except (OSError, ValueError):
            return "spawn_failed", False
        ready = False
        self.last_state = None
        try:
            det, parser = Detector(pa, mask), Parser()
            ready_by = self.mono() + READY_TIMEOUT_S
            while True:
                now = self.mono()
                if ready and det.take(now):
                    self._attempt(dev, pa, det)
                    continue
                if not ready:
                    if now >= ready_by:
                        return "not_ready", False
                    timeout = ready_by - now
                else:
                    dl = det.deadline()
                    timeout = IDLE_POLL_S if dl is None else min(IDLE_POLL_S, max(0.0, dl - now))
                kind, line = src.readline(timeout)
                if kind == "timeout":
                    continue
                if kind != "line":
                    return self._end(src, kind), ready
                for item in parser.feed(line):
                    if item[0] == "state":
                        if item[1:] == (pa, mask, True) and not ready:
                            ready = True
                            self.log({"event": "cec_return_ready", "device": dev, "physical_address": pa})
                        elif item[1:3] != (pa, mask):          # état réel différent : changement attendu (TV, HDMI…)
                            self.last_state = item[1:3]
                            return "adapter_state_changed", ready
                        elif not ready:                         # même état sans événement INITIAL : sortie malformée
                            return "no_initial_state", False
                    elif not ready:
                        continue                            # avant « prêt » : rien n'arme la détection
                    elif item[0] == "frame":
                        det.frame(item[1], self.mono())
                    else:
                        det.reset(item[0] if item[0] == "lost" else "invalid_" + item[1])
        except ValueError:
            return "invalid_startup", ready
        finally:
            src.close()

    def _fatal(self, why, **extra):
        self.log(dict({"event": "cec_return_failed", "reason": why, "exit": EXIT_FATAL}, **extra))
        return EXIT_FATAL

    def _rediscover_delay(self, since):
        """Après un changement d'état attendu : 2 s pendant REDISCOVER_FAST_WINDOW_S, puis 5 s, jusqu'au « prêt »."""
        return STATE_CHANGE_RETRY_S if self.mono() - since < REDISCOVER_FAST_WINDOW_S else REDISCOVER_SLOW_S

    def serve(self, max_cycles=None):
        """Boucle du service -> None (max_cycles atteint, tests) ou EXIT_FATAL (échec véridique, unité failed).

        `churn` (instant du dernier changement d'état attendu, None sinon) : tant qu'aucun moniteur n'est de nouveau
        « prêt », toute attente est courte (≤ REDISCOVER_SLOW_S) — jamais l'attente aveugle DISCOVERY_RETRY_S. Une
        discordance d'état avant « prêt » (événement initial ≠ adaptateur lu : l'état bouge) relit vite et ne compte
        PAS dans `pre_ready` ; elle ne remet pas non plus ce compteur à zéro (une discordance intercalée ne blanchit
        pas des échecs génériques). La même discordance sur le même adaptateur lu MISMATCH_STUCK_MAX fois de suite
        n'est plus un état qui bouge : échec véridique `adapter_state_inconsistent`."""
        cycles, backoff, pre_ready = 0, 0, 0
        churn, stuck, last_mismatch = None, 0, None
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            self._churn = churn is not None
            found = self.discover()
            if found is None:
                stuck, last_mismatch = 0, None
                if self._why in ("read_failed", "budget_exceeded"):
                    pre_ready += 1
                    if pre_ready >= PRE_READY_FAILURES_MAX:
                        return self._fatal("adapter_unreadable", failures=pre_ready)
                    self.sleep(PRE_READY_RETRY_S)
                elif churn is not None:
                    self.sleep(self._rediscover_delay(churn))   # TV éteinte / HDMI : attente explicite, courte
                else:
                    self.sleep(DISCOVERY_RETRY_S)       # attente légitime : adaptateur pas encore déclaré au démarrage
                continue
            dev, pa, mask = found
            self.log({"event": "cec_return_watch", "device": dev, "physical_address": pa})
            t0 = self.mono()
            why, ready = self.watch(dev, pa, mask)
            if why in FATAL_NOW:
                return self._fatal(why)
            if why == "adapter_state_changed":         # attendu (TV éteinte, HDMI, réclamation) : relecture rapide
                if ready or churn is None:
                    churn = self.mono()
                if ready:
                    pre_ready, backoff, stuck, last_mismatch = 0, 0, 0, None
                else:
                    seen = (found, self.last_state)
                    stuck = stuck + 1 if seen == last_mismatch else 1
                    last_mismatch = seen
                    if stuck >= MISMATCH_STUCK_MAX:
                        self.log({"event": "cec_return_watch_end", "reason": why, "ready": False})
                        return self._fatal("adapter_state_inconsistent", failures=stuck)
                delay = self._rediscover_delay(churn)
            elif not ready:
                stuck, last_mismatch = 0, None
                pre_ready += 1
                if pre_ready >= PRE_READY_FAILURES_MAX:
                    self.log({"event": "cec_return_watch_end", "reason": why, "ready": False})
                    return self._fatal(why, failures=pre_ready)
                delay = PRE_READY_RETRY_S
            else:
                churn, stuck, last_mismatch, pre_ready = None, 0, None, 0
                if self.mono() - t0 >= HEALTHY_RUN_S:
                    backoff = 0
                delay = RESTART_BACKOFF_S[min(backoff, len(RESTART_BACKOFF_S) - 1)]
                backoff += 1
            self.log({"event": "cec_return_watch_end", "reason": why, "ready": ready, "retry_in_s": delay})
            self.sleep(delay)
        return None


def _log(obj):
    import json
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(monitor):
    """Point d'entrée du service : SIGTERM termine proprement (le moniteur cec-ctl est fermé dans `watch`) ; échec
    véridique -> EXIT_FATAL (78), que l'unité ne relance pas (RestartPreventExitStatus)."""
    def stop(*_):
        raise SystemExit(0)
    prev = signal.signal(signal.SIGTERM, stop)
    try:
        code = monitor.serve()
    finally:
        signal.signal(signal.SIGTERM, prev)
    return 0 if code is None else code
