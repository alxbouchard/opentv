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
ni lu ni écrit. Délai minimal COOLDOWN_S entre deux reprises et au plus MAX_PER_HOUR par heure.
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
COOLDOWN_S = 60.0
MAX_PER_HOUR = 6
LOCK_WAIT_S = 10.0
LOCK_POLL_S = 0.5
RECOVER_BUDGET_S = 30.0             # relecture + question d'alimentation + 2 émissions, chacune bornée
DISCOVER_BUDGET_S = 25.0
DISCOVERY_RETRY_S = 30.0            # adaptateur absent / pas encore déclaré par opentv-cec.timer : relecture seule
IDLE_POLL_S = 30.0
RESTART_BACKOFF_S = (5, 10, 30, 60, 120, 300)
HEALTHY_RUN_S = 600.0               # un moniteur resté vivant aussi longtemps remet le délai de relance au minimum
MAX_LINE_BYTES = 512
MAX_FRAME_LINES = 16
MONITOR_OPTS = ("-s", "-r", "-m")   # sans infos pilote, octets bruts, mode moniteur (passif)

PA_TXT = r"[0-9a-f]\.[0-9a-f]\.[0-9a-f]\.[0-9a-f]"
HEADER_RE = re.compile(r"(Received from|Transmitted by) [A-Za-z0-9 ]{1,40} to [A-Za-z0-9 ]{1,40} "
                       r"\(([0-9]{1,2}) to ([0-9]{1,2})\): (.{1,80})")
OPNAME_RE = re.compile(r"([A-Z][A-Z0-9_]{0,40}) \(0x([0-9a-f]{2})\)")
RAW_RE = re.compile(r"\tRaw:((?: 0x[0-9a-f]{2}){1,16}) \(.{1,16}\)")
ARG_RE = re.compile(r"\t[a-z][a-z0-9-]{0,40}: .{0,120}")
PA_ARG_RE = re.compile(r"\t(phys-addr|orig-phys-addr|new-phys-addr): (" + PA_TXT + r")")
EVENT_PREFIX = r"(?:Initial |[0-9][0-9:.]{0,30}s?: )?"
STATE_RE = re.compile(EVENT_PREFIX + r"Event: State Change: PA: (" + PA_TXT + r"), LA mask: 0x([0-9a-f]{4})"
                      r"(?:, Conn Info: (?:yes|no))?")
LOST_RE = re.compile(r"(?:" + EVENT_PREFIX + r"Event: Lost [0-9]{1,9} messages|\(warn: .{1,40} events were lost\))")
# arguments d'adresse physique décodés par cec-ctl, recoupés avec les octets bruts
PA_ARGS = {ACTIVE_SOURCE: (("phys-addr", 2),), SET_STREAM_PATH: (("phys-addr", 2),),
           ROUTING_INFORMATION: (("phys-addr", 2),), INACTIVE_SOURCE: (("phys-addr", 2),),
           ROUTING_CHANGE: (("orig-phys-addr", 2), ("new-phys-addr", 4))}

Frame = collections.namedtuple("Frame", "rx initiator dest opcode params")


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
    """Lignes de `cec-ctl -s -r -m` -> éléments : ("frame", Frame) | ("state", pa, masque) | ("lost",) |
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
            return out + [("state", m.group(1), int(m.group(2), 16))]
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
    """`cec-ctl -m` en flux : lignes bornées (MAX_LINE_BYTES), groupe de processus tué à la fermeture."""

    def __init__(self, argv):
        self.p = subprocess.Popen(list(argv), shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=SAFE_ENV, start_new_session=True, close_fds=True)
        self.fd = self.p.stdout.fileno()
        self.buf = b""
        self.lines = collections.deque()
        self.eof = False

    def readline(self, timeout):
        """-> ("line", texte) | ("timeout", None) | ("eof", None) | ("oversize", None)."""
        if self.lines:
            return "line", self.lines.popleft()
        if self.eof:
            return "eof", None
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not r:
            return "timeout", None
        chunk = os.read(self.fd, 4096)
        if not chunk:
            self.eof = True
            return "eof", None
        self.buf += chunk
        *done, self.buf = self.buf.split(b"\n")
        if any(len(x) > MAX_LINE_BYTES for x in done) or len(self.buf) > MAX_LINE_BYTES:
            return "oversize", None
        self.lines.extend(x.decode("utf-8", "replace") for x in done)
        return ("line", self.lines.popleft()) if self.lines else ("timeout", None)

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
        try:
            self.p.stdout.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- service
class Monitor:
    def __init__(self, root="/", runner=default_runner, spawn=PopenLines, mono=time.monotonic, sleep=time.sleep,
                 state_dir=cecboot.STATE_DIR, log=None):
        self.root, self.runner, self.spawn, self.mono, self.sleep = root, runner, spawn, mono, sleep
        self.state_dir = state_dir
        self.log = log or _log
        self.last_attempt = None
        self.attempts = collections.deque()
        self._why = None

    def discover(self):
        """Premier adaptateur vc4 branché (même choix que l'amorçage) s'il est déjà Playback « OpenTV » avec son
        adresse logique -> (dev, adresse physique, masque) ; sinon None. Lecture seule."""
        run = cecboot._Run(self.runner, self.mono, DISCOVER_BUDGET_S)
        why = "no_adapter"
        try:
            for dev in cecboot.list_devices(self.root):
                info = run.read(dev)
                if info is None or not cecboot.is_vc4(info) or not cecboot.connected(info):
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
        except cecboot.BudgetExceeded:
            why = "budget_exceeded"
        if why != self._why:
            self.log({"event": "cec_return_waiting", "reason": why})
        self._why = why
        return None

    def recover(self, dev, pa):
        """Même annonce sûre que `opentv-pi cec --activate`, sous le verrou existant ; aucun témoin touché."""
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
            return dict(ann, result="recovered" if ok else ann["result"])
        except cecboot.BudgetExceeded:
            return {"result": "budget_exceeded"}
        finally:
            os.close(lock)

    def _attempt(self, dev, pa):
        now = self.mono()
        while self.attempts and now - self.attempts[0] >= 3600.0:
            self.attempts.popleft()
        base = {"event": "cec_return_recovery", "device": dev, "physical_address": pa}
        if self.last_attempt is not None and now - self.last_attempt < COOLDOWN_S:
            self.log(dict(base, result="cooldown"))
            return
        if len(self.attempts) >= MAX_PER_HOUR:
            self.log(dict(base, result="rate_limited"))
            return
        self.last_attempt = now
        self.attempts.append(now)
        res = self.recover(dev, pa)
        self.log(dict(base, elapsed_s=round(self.mono() - now, 3), **res))

    def watch(self, dev, pa, mask):
        """Un moniteur, état vide au départ ; rend la raison de sa fin. Rien n'est émis hors de `_attempt`."""
        try:
            src = self.spawn(monitor_argv(dev))
        except (OSError, ValueError):
            return "spawn_failed"
        det, parser = Detector(pa, mask), Parser()
        try:
            while True:
                if det.take(self.mono()):
                    self._attempt(dev, pa)
                    continue
                dl = det.deadline()
                timeout = IDLE_POLL_S if dl is None else min(IDLE_POLL_S, max(0.0, dl - self.mono()))
                kind, line = src.readline(timeout)
                if kind == "timeout":
                    continue
                if kind != "line":
                    return kind
                for item in parser.feed(line):
                    if item[0] == "frame":
                        det.frame(item[1], self.mono())
                    elif item[0] == "state":
                        if item[1] != pa or item[2] != mask:
                            return "adapter_state_changed"
                    else:
                        det.reset(item[0] if item[0] == "lost" else "invalid_" + item[1])
        finally:
            src.close()

    def serve(self, max_cycles=None):
        cycles, backoff = 0, 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            found = self.discover()
            if found is None:
                self.sleep(DISCOVERY_RETRY_S)
                continue
            dev, pa, mask = found
            self.log({"event": "cec_return_watch", "device": dev, "physical_address": pa})
            t0 = self.mono()
            why = self.watch(dev, pa, mask)
            if self.mono() - t0 >= HEALTHY_RUN_S:
                backoff = 0
            delay = RESTART_BACKOFF_S[min(backoff, len(RESTART_BACKOFF_S) - 1)]
            backoff += 1
            self.log({"event": "cec_return_watch_end", "reason": why, "retry_in_s": delay})
            self.sleep(delay)


def _log(obj):
    import json
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(monitor):
    """Point d'entrée du service : SIGTERM termine proprement (le moniteur cec-ctl est fermé dans `watch`)."""
    def stop(*_):
        raise SystemExit(0)
    prev = signal.signal(signal.SIGTERM, stop)
    try:
        monitor.serve()
    finally:
        signal.signal(signal.SIGTERM, prev)
    return 0
