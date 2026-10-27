"""HDMI-CEC : amorçage borné, idempotent et réversible (opentv-cec.service, lancé par opentv-cec.timer).

Rôle : rendre la télécommande de la TV utilisable par OpenTV sans surveiller ni injecter de touches. Le noyau
(vc4-hdmi, rc-cec) transforme déjà les <User Control Pressed> reçus en événements d'entrée Linux ; il suffit que
l'adaptateur soit déclaré comme lecteur (Playback « OpenTV »).

Sûreté par défaut : la minuterie ne réveille JAMAIS la TV et ne lui prend JAMAIS son entrée — aucune <Image View On>,
aucune <Active Source>, aucune question d'alimentation, quel que soit l'état de la TV (allumée, en veille, inconnue,
réveil, redémarrage de la boîte). L'annonce comme source active n'existe que par la commande explicite
`opentv-pi cec-bootstrap --force-announce` (terrain, avec l'accord de l'utilisateur), et seulement si la TV répond
qu'elle est allumée.

Une exécution (budget total RUN_BUDGET_S, chaque cec-ctl tronqué au budget restant, groupe de processus tué) :
1. énumère /dev/cecN (jamais d'adresse en dur) et lit chaque adaptateur (`cec-ctl -d <dev>`, aucune émission) ;
   seuls les adaptateurs du pilote vc4 sont pris en compte ;
2. clavier noyau de chaque adaptateur vc4 : la touche CEC « Select » (code 0x00) produit KEY_OK, qui n'a aucun
   symbole XKB ni DomCode Chromium (touche perdue dans le kiosque). Si — et seulement si — la table du noyau contient
   encore la valeur par défaut KEY_OK, elle devient KEY_ENTER (EVIOCSKEYCODE_V2 ; volatile, rétablie par --release) ;
   une valeur personnalisée n'est jamais touchée ;
3. choisit le premier adaptateur vc4 dont l'adresse physique est valide (≠ f.f.f.f : HDMI branché) ; rien n'est émis
   si aucun ne l'est ;
4. si l'adaptateur n'a aucune configuration, le déclare Playback « OpenTV » ; une configuration étrangère n'est
   jamais remplacée ; la nôtre n'est pas réécrite (idempotent) ; fin de l'exécution automatique ;
5. seulement avec --force-announce : interroge l'état d'alimentation de la TV ; allumée (« on ») ⇒ <Image View On>,
   <Active Source> avec l'adresse physique lue, <Menu Status activated> ; tout autre état (veille, transition,
   inconnu) ⇒ refus, rien d'autre n'est émis. Le témoin de cette annonce vit dans /run/opentv-cec (tmpfs : remis à
   zéro au redémarrage, jamais réutilisé pour réannoncer seul).

Journal : une ligne JSON construite ici (périphérique, adresse physique, résultats) — jamais la sortie brute de
cec-ctl, jamais de nom OSD étranger. Aucun secret n'est manipulé.
"""
import errno
import fcntl
import glob
import json
import os
import re
import stat
import struct
import time

from . import bootid
from .cec import DEV_RE, DRIVER_RE, PHYS_RE
from .nethelper import KILL_GRACE_S, default_runner

OSD_NAME = "OpenTV"
STATE_DIR = "/run/opentv-cec"
STATE_FILE = "announced.json"
LOCK_FILE = "lock"
MAX_ADAPTERS = 4
# -- budget (opentv-cec.service : TimeoutStartSec=60, démarrage de Python compris)
UNIT_TIMEOUT_START_S = 60
RUN_BUDGET_S = 40.0
READ_TIMEOUT_S = 5          # lecture d'un adaptateur (aucune émission)
CLAIM_TIMEOUT_S = 10        # réclamation d'une adresse logique (sondage du bus)
TX_TIMEOUT_S = 5            # un message (cec-ctl attend l'acquittement / la réponse ≤ 1 s par défaut)
MIN_STEP_S = 1.0            # en dessous, une commande n'est plus lancée : l'exécution s'arrête proprement

ADAPTER_RE = re.compile(r"Adapter Name\s*:\s*([A-Za-z0-9_.-]{1,32})")
MASK_RE = re.compile(r"Logical Address Mask\s*:\s*0x([0-9a-fA-F]{1,4})")
OSD_RE = re.compile(r"OSD Name\s*:\s*'([^'\n]{0,32})'")
# ancré en début de ligne : « Available Logical Addresses: 1 » (capacité du matériel) ne doit pas compter
LA_COUNT_RE = re.compile(r"^[ \t]*Logical Addresses[ \t]*:[ \t]*([0-9]{1,2})", re.M)
PLAYBACK_RE = re.compile(r"Primary Device Type\s*:\s*Playback\b")
PWR_RE = re.compile(r"pwr-state:\s*(on|standby|to-on|to-standby)\b")

# -- clavier noyau (linux/input.h) : struct input_keymap_entry { u8 flags; u8 len; u16 index; u32 keycode;
#    u8 scancode[32]; } = 40 octets ; EVIOC[GS]KEYCODE_V2 = _IO[RW]('E', 0x04, struct input_keymap_entry)
KEYMAP_FMT = "=BBHI32s"
EVIOCGKEYCODE_V2 = 0x80284504
EVIOCSKEYCODE_V2 = 0x40284504
KEY_ENTER = 28
KEY_OK = 352
# (code CEC « UI command », valeur par défaut de rc-cec, valeur OpenTV)
KEY_REMAPS = ((0x00, KEY_OK, KEY_ENTER),)   # Select → Entrée (Chromium : « Enter »)


class BudgetExceeded(Exception):
    pass


# --------------------------------------------------------------------------- lecture / analyse
def parse_info(out):
    """Sortie de `cec-ctl -d <dev>` -> champs utiles (aucune autre donnée n'est conservée)."""
    out = out or ""
    g = lambda rx: (rx.search(out).group(1) if rx.search(out) else None)   # noqa: E731
    pa = g(PHYS_RE)
    mask = g(MASK_RE)
    la = g(LA_COUNT_RE)
    return {"driver": g(DRIVER_RE), "adapter": g(ADAPTER_RE), "physical_address": pa.lower() if pa else None,
            "mask": int(mask, 16) if mask is not None else None, "osd": g(OSD_RE),
            "logical_addresses": int(la) if la is not None else None, "playback": bool(PLAYBACK_RE.search(out))}


def connected(info):
    pa = info.get("physical_address")
    return bool(pa) and pa != "f.f.f.f"


def classify(info):
    """ours = Playback « OpenTV » ; empty = aucune configuration ; foreign = autre chose (jamais remplacée)."""
    if info.get("osd") == OSD_NAME and info.get("playback"):
        return "ours"
    if not info.get("logical_addresses") and not info.get("mask") and not info.get("osd"):
        return "empty"
    return "foreign"


def is_vc4(info):
    return (info.get("driver") or "").startswith("vc4")


def list_devices(root="/", max_adapters=MAX_ADAPTERS):
    out = []
    for p in sorted(glob.glob(os.path.join(root, "dev", "cec*"))):
        name = os.path.basename(p)
        if DEV_RE.fullmatch(name):
            out.append("/dev/" + name)
    return out[:max_adapters]


# --------------------------------------------------------------------------- clavier noyau (rc-cec)
def find_input(root, adapter_name):
    """Nœud /dev/input/eventN du récepteur RC de l'adaptateur (nom d'entrée = nom d'adaptateur, sous …/rc/rcN/)."""
    if not adapter_name:
        return None
    for p in sorted(glob.glob(os.path.join(root, "sys", "class", "input", "event*"))):
        ev = os.path.basename(p)
        if not re.fullmatch(r"event[0-9]{1,3}", ev):
            continue
        try:
            with open(os.path.join(p, "device", "name"), encoding="utf-8", errors="replace") as f:
                name = f.read(64).strip()
        except OSError:
            continue
        if name == adapter_name and "/rc/rc" in os.path.realpath(p):
            return "/dev/input/" + ev
    return None


class KernelKeymap:
    """Accès EVIOC[GS]KEYCODE_V2 au périphérique d'entrée (ouvert sans suivre de lien, périphérique caractère exigé)."""

    def __init__(self, root="/"):
        self.root = root

    def _open(self, dev):
        path = os.path.join(self.root, dev.lstrip("/"))
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(errno.ENODEV, "not_char_device")
        return fd

    @staticmethod
    def _entry(scancode, keycode=0):
        return bytearray(struct.pack(KEYMAP_FMT, 0, 4, 0, keycode, struct.pack("<I", scancode)))

    def get(self, dev, scancode):
        fd = self._open(dev)
        try:
            buf = self._entry(scancode)
            fcntl.ioctl(fd, EVIOCGKEYCODE_V2, buf)
            return struct.unpack(KEYMAP_FMT, bytes(buf))[3]
        finally:
            os.close(fd)

    def set(self, dev, scancode, keycode):
        fd = self._open(dev)
        try:
            fcntl.ioctl(fd, EVIOCSKEYCODE_V2, bytes(self._entry(scancode, keycode)))
        finally:
            os.close(fd)


def apply_keymap(io, dev, release=False):
    """-> {code CEC: résultat}. Ne modifie qu'une valeur par défaut (ou, en retour arrière, notre propre valeur)."""
    res = {}
    for sc, default, ours in KEY_REMAPS:
        want, expect = (default, ours) if release else (ours, default)
        try:
            cur = io.get(dev, sc)
            if cur == want:
                res["0x%02x" % sc] = "restored_already" if release else "already"
            elif cur == expect:
                io.set(dev, sc, want)
                res["0x%02x" % sc] = ("restored" if release else "remapped") if io.get(dev, sc) == want else "failed"
            else:
                res["0x%02x" % sc] = "custom_untouched"
        except OSError:
            res["0x%02x" % sc] = "unavailable"
    return res


# --------------------------------------------------------------------------- témoin d'annonce (par démarrage)
def load_announced(state_dir, boot):
    """Lit le témoin sans suivre de lien et sans pouvoir bloquer sur un FIFO.

    `opentv-pi cec` peut être lancé par root alors que RuntimeDirectory appartient à `opentv` : le fichier est donc
    une entrée non fiable. Exiger un fichier régulier non lié évite lecture arbitraire, FIFO et hardlink.
    """
    try:
        dfd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                      getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(STATE_FILE, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        finally:
            os.close(dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 4096:
                return {}
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                return {}
            d = json.loads(raw.decode("utf-8"))
        finally:
            os.close(fd)
        if isinstance(d, dict) and d.get("boot_id") == boot and isinstance(d.get("done"), dict):
            return {k: v for k, v in d["done"].items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    return {}


def save_announced(state_dir, boot, done):
    dfd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    tmp = ".%s.%d.tmp" % (STATE_FILE, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=dfd)
        try:
            os.write(fd, json.dumps({"v": 1, "boot_id": boot, "done": done}, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, STATE_FILE, src_dir_fd=dfd, dst_dir_fd=dfd)
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


def _lock(state_dir):
    """Verrou non bloquant (minuterie + commande manuelle) ; None si une autre exécution est en cours."""
    fd = None
    try:
        dfd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                      getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
        finally:
            os.close(dfd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fd is not None:
            os.close(fd)
        return None
    return fd


# --------------------------------------------------------------------------- exécution bornée
class _Run:
    def __init__(self, runner, mono, budget):
        self.runner, self.mono = runner, mono
        self.deadline = mono() + budget
        self.calls = 0

    def __call__(self, argv, want):
        rem = self.deadline - self.mono() - KILL_GRACE_S
        if rem < MIN_STEP_S:
            raise BudgetExceeded()
        self.calls += 1
        return self.runner(list(argv), min(float(want), rem))

    def read(self, dev):
        rc, out = self(["cec-ctl", "-d", dev], READ_TIMEOUT_S)
        return parse_info(out) if rc == 0 else None


def power_state(run, dev):
    rc, out = run(["cec-ctl", "-d", dev, "-s", "-t", "0", "--give-device-power-status"], TX_TIMEOUT_S)
    m = PWR_RE.search(out or "") if rc == 0 else None
    return m.group(1) if m else "unknown"


def announce(run, dev, pa):
    """(--force-announce seulement) TV allumée ⇒ <Image View On>, <Active Source>, <Menu Status>. Veille, transition
    ou état inconnu ⇒ refus sans autre émission (jamais de réveil). -> (résultat, ok)."""
    pwr = power_state(run, dev)
    if pwr != "on":
        return {"result": "refused_tv_not_on", "tv_power": pwr}, False
    steps = {}
    steps["image_view_on"] = run(["cec-ctl", "-d", dev, "-s", "-t", "0", "--image-view-on"], TX_TIMEOUT_S)[0] == 0
    steps["active_source"] = run(["cec-ctl", "-d", dev, "-s", "--active-source", "phys-addr=" + pa],
                                 TX_TIMEOUT_S)[0] == 0
    if not steps["active_source"]:
        return {"result": "failed", "tv_power": pwr, "steps": steps}, False
    steps["menu_status"] = run(["cec-ctl", "-d", dev, "-s", "-t", "0", "--menu-status", "menu-state=activated"],
                               TX_TIMEOUT_S)[0] == 0
    return {"result": "done", "tv_power": pwr, "steps": steps}, True


def bootstrap(root="/", runner=default_runner, mono=time.monotonic, keymap_io=None, state_dir=STATE_DIR,
              keymap=True, force_announce=False, budget=RUN_BUDGET_S, proc_root="/proc"):
    t0 = mono()
    res = {"ok": True, "result": None, "adapters": [], "selected": None}
    io = keymap_io if keymap_io is not None else KernelKeymap(root)
    run = _Run(runner, mono, budget)
    lock = None
    try:
        devs = list_devices(root)
        if not devs:
            res["result"] = "no_adapter"
            return res
        if not os.path.isdir(state_dir):
            res.update(ok=False, result="state_dir_missing")
            return res
        lock = _lock(state_dir)
        if lock is None:
            res["result"] = "busy"
            return res
        infos = {}
        for dev in devs:
            info = run.read(dev)
            entry = {"device": dev, "readable": info is not None}
            res["adapters"].append(entry)
            if info is None:
                continue
            entry.update(vc4=is_vc4(info), physical_address=info["physical_address"], connected=connected(info))
            if not entry["vc4"]:
                continue
            infos[dev] = info
            if keymap:
                ev = find_input(root, info["adapter"])
                entry["keymap"] = apply_keymap(io, ev) if ev else "no_input_device"
        sel = next((d for d in devs if d in infos and connected(infos[d])), None)
        if sel is None:
            res["result"] = "disconnected" if infos else "no_vc4_adapter"
            return res
        info = infos[sel]
        res["selected"] = sel
        kind = classify(info)
        if kind == "foreign":
            res["result"] = "foreign_config_untouched"
            return res
        if kind == "empty":
            rc, _ = run(["cec-ctl", "-d", sel, "--playback", "--osd-name", OSD_NAME], CLAIM_TIMEOUT_S)
            after = run.read(sel)
            if rc != 0 or after is None or classify(after) != "ours":
                res.update(ok=False, result="register_failed", registration="failed")
                return res
            info = after
            res["registration"] = "registered"
        else:
            res["registration"] = "existing"
        if not info.get("mask") or not connected(info):
            res["result"] = "claim_pending"           # adresse logique pas encore obtenue : prochaine exécution
            return res
        pa = info["physical_address"]
        res["physical_address"] = pa
        if not force_announce:   # défaut sûr : ni réveil ni changement d'entrée, quel que soit l'état de la TV
            res["announce"] = {"result": "not_requested"}
            res["result"] = "ready"
            return res
        ann, ok = announce(run, sel, pa)
        res["announce"] = ann
        if not ok:
            res.update(ok=False, result="tv_not_on" if ann["result"] == "refused_tv_not_on" else "announce_failed")
            return res
        boot = bootid.read(proc_root)
        done = load_announced(state_dir, boot)
        done[sel] = pa
        try:
            save_announced(state_dir, boot, done)
        except OSError:
            res.update(ok=False, result="state_write_failed")
            return res
        res["result"] = "ready"
        return res
    except BudgetExceeded:
        res.update(ok=False, result="budget_exceeded")
        return res
    finally:
        if lock is not None:
            os.close(lock)
        res["commands"] = run.calls
        res["elapsed_s"] = round(mono() - t0, 3)


def release(root="/", runner=default_runner, mono=time.monotonic, keymap_io=None, budget=RUN_BUDGET_S):
    """Retour arrière (désinstallation, root) : efface la configuration CEC seulement si c'est « OpenTV » Playback,
    rétablit la touche Select du noyau seulement si elle porte notre valeur. N'écrit aucun fichier."""
    t0 = mono()
    res = {"ok": True, "adapters": []}
    io = keymap_io if keymap_io is not None else KernelKeymap(root)
    run = _Run(runner, mono, budget)
    try:
        for dev in list_devices(root):
            info = run.read(dev)
            entry = {"device": dev}
            res["adapters"].append(entry)
            if info is None or not is_vc4(info):
                entry["result"] = "skipped"
                continue
            if classify(info) == "ours":
                rc, _ = run(["cec-ctl", "-d", dev, "--clear"], CLAIM_TIMEOUT_S)
                entry["result"] = "cleared" if rc == 0 else "clear_failed"
                res["ok"] = res["ok"] and rc == 0
            else:
                entry["result"] = "untouched"
            ev = find_input(root, info["adapter"])
            entry["keymap"] = apply_keymap(io, ev, release=True) if ev else "no_input_device"
    except BudgetExceeded:
        res.update(ok=False, result="budget_exceeded")
    res["elapsed_s"] = round(mono() - t0, 3)
    return res
