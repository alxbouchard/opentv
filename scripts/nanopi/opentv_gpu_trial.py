#!/usr/bin/env python3
"""Banc d'essai GPU NanoPi, fermé par défaut (fail-closed). Python 3.11+, bibliothèque standard seule.

  python3 -I opentv_gpu_trial.py preflight [--root /]        -> JSON, exit 0 = GO, 1 = NO-GO
  python3 -I opentv_gpu_trial.py run [--duration 60] [--url chrome://gpu] [--dry-run]

`preflight` exige un vrai nœud de rendu DRM piloté par panfrost (Mesa) et refuse le pilote propriétaire
Midgard (/dev/mali0, mali_kbase). `run` repasse le préflight, vérifie que rien n'occupe l'affichage (session du
pont, weston, chromium), photographie muse-display (état, PID, redémarrages), puis lance weston GL + Chromium
sans --disable-gpu en 1920x1080 dans une unité systemd --user transitoire bornée (RuntimeMaxSec, KillMode=mixed).
Nettoyage garanti, journaux plafonnés, opentv-bridge.env jamais écrit (seulement comparé avant/après).
Tout critère invérifiable => refus. Ne remplace pas la confirmation à l'écran.
"""
import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.request

VERSION = 1
MODE = "1920x1080"
DASHBOARD_UNIT = "muse-display.service"
BRIDGE_UNIT = "opentv-bridge.service"
BRIDGE_ENV = os.path.join("~", ".config", "systemd", "user", "opentv-bridge.env")
BRIDGE_UI_STATE = "http://127.0.0.1:%d/ui/state.json"
UNIT_PREFIX = "opentv-gpu-trial"
KERNEL_MIN = (5, 15)
DURATION_DEFAULT, DURATION_MIN, DURATION_MAX = 60, 10, 300
STARTUP_GRACE_S = 30          # weston + chromium doivent démarrer dans ce délai
STOP_TIMEOUT_S = 10           # TimeoutStopSec de l'unité, puis SIGKILL par systemd
CMD_TIMEOUT_S = 15
LOG_CAPS = {"weston": 256 * 1024, "chromium": 512 * 1024}
KEEP_TRIAL_DIRS = 3
DISPLAY_COMMS = ("weston", "chromium", "chrome", "chromium-browse", "chromium-browser")
URL_ALLOW = ("chrome://gpu", "about:blank", "http://127.0.0.1:8098/ui/")
MALI_PATHS = ("dev/mali0", "dev/mali", "sys/class/misc/mali0", "sys/module/mali_kbase", "sys/module/mali",
              "sys/module/midgard_kbase", "sys/module/bifrost_kbase", "sys/module/mali_kbase_midgard")
MALI_MODULE_RE = re.compile(r"^(mali\w*|\w*kbase\w*)\s", re.M)
EXIT_GO, EXIT_NOGO, EXIT_USAGE, EXIT_REFUSED, EXIT_FAILED, EXIT_ALERT = 0, 1, 2, 3, 4, 5

WESTON_INI = """[core]
idle-time=0
require-input=false

[output]
name={name}
mode={mode}
"""

# Sonde EGL lancée en sous-processus (un plantage du pilote ne tue pas le banc) : plateforme « device »
# restreinte au nœud de rendu demandé, contexte GLES sans surface. N'ouvre ni nœud primaire ni maître DRM.
EGL_PROBE_SRC = r'''
import ctypes, json, sys
node = sys.argv[1]
out = {"ok": False}
try:
    vp, EGLint, EGLBool, EGLenum = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint, ctypes.c_uint
    egl = ctypes.CDLL("libEGL.so.1")
    gles = ctypes.CDLL("libGLESv2.so.2")
    egl.eglGetProcAddress.restype, egl.eglGetProcAddress.argtypes = vp, [ctypes.c_char_p]
    def ext(name, res, *args):
        p = egl.eglGetProcAddress(name.encode())
        if not p:
            raise RuntimeError("missing:" + name)
        return ctypes.CFUNCTYPE(res, *args)(p)
    query_devices = ext("eglQueryDevicesEXT", EGLBool, EGLint, ctypes.POINTER(vp), ctypes.POINTER(EGLint))
    query_dev_str = ext("eglQueryDeviceStringEXT", ctypes.c_char_p, vp, EGLint)
    get_display = ext("eglGetPlatformDisplayEXT", vp, EGLenum, vp, ctypes.POINTER(EGLint))
    devs, n = (vp * 32)(), EGLint(0)
    if not query_devices(32, devs, ctypes.byref(n)):
        raise RuntimeError("query_devices")
    target = None
    for i in range(n.value):
        exts = query_dev_str(devs[i], 0x3055) or b""
        if b"EGL_EXT_device_drm_render_node" in exts:
            f = query_dev_str(devs[i], 0x3377)
            if f and f.decode() == node:
                target = devs[i]
    if target is None:
        raise RuntimeError("device_not_found")
    dpy = get_display(0x313F, target, None)
    if not dpy:
        raise RuntimeError("no_display")
    egl.eglInitialize.restype, egl.eglInitialize.argtypes = EGLBool, [vp, ctypes.POINTER(EGLint), ctypes.POINTER(EGLint)]
    egl.eglBindAPI.restype, egl.eglBindAPI.argtypes = EGLBool, [EGLenum]
    egl.eglCreateContext.restype, egl.eglCreateContext.argtypes = vp, [vp, vp, vp, ctypes.POINTER(EGLint)]
    egl.eglMakeCurrent.restype, egl.eglMakeCurrent.argtypes = EGLBool, [vp, vp, vp, vp]
    egl.eglDestroyContext.restype, egl.eglDestroyContext.argtypes = EGLBool, [vp, vp]
    egl.eglTerminate.restype, egl.eglTerminate.argtypes = EGLBool, [vp]
    gles.glGetString.restype, gles.glGetString.argtypes = ctypes.c_char_p, [ctypes.c_uint]
    major, minor = EGLint(0), EGLint(0)
    if not egl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor)):
        raise RuntimeError("initialize")
    if not egl.eglBindAPI(0x30A0):
        raise RuntimeError("bind_api")
    attrs = (EGLint * 3)(0x3098, 2, 0x3038)
    ctx = egl.eglCreateContext(dpy, None, None, attrs)
    if not ctx or not egl.eglMakeCurrent(dpy, None, None, ctx):
        raise RuntimeError("context")
    s = lambda e: (gles.glGetString(e) or b"").decode("utf-8", "replace")[:120]
    out.update(ok=True, vendor=s(0x1F00), renderer=s(0x1F01), version=s(0x1F02),
               egl="%d.%d" % (major.value, minor.value))
    egl.eglMakeCurrent(dpy, None, None, None)
    egl.eglDestroyContext(dpy, ctx)
    egl.eglTerminate(dpy)
except Exception as e:
    out["error"] = str(e)[:120]
print(json.dumps(out))
'''


class Refused(Exception):
    pass


class Interrupted(Exception):
    pass


def _dump(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)


def parse_kernel(release):
    m = re.match(r"(\d+)\.(\d+)", release or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_show(text):
    """Sortie `systemctl show -p A -p B` -> dict (ordre indifférent)."""
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- préflight
class Probe:
    """Inspection en lecture seule d'une racine (/ en production, arborescence factice en test)."""

    def __init__(self, root="/", runner=None):
        self.root = os.path.abspath(root)
        self.live = self.root == "/"
        self.runner = runner or run_cmd

    def p(self, rel):
        return os.path.join(self.root, rel.lstrip("/"))

    def exists(self, rel):
        return os.path.lexists(self.p(rel))

    def read(self, rel, limit=65536):
        try:
            with open(self.p(rel), "r", errors="replace") as f:
                return f.read(limit)
        except OSError:
            return None

    def executable(self, rel):
        path = self.p(rel)
        return os.path.isfile(path) and os.access(path, os.X_OK)

    # points d'injection pour les tests (fichiers spéciaux impossibles à créer sans root)
    def rdev(self, rel):
        try:
            st = os.stat(self.p(rel))
        except OSError:
            return None
        return (os.major(st.st_rdev), os.minor(st.st_rdev)) if stat.S_ISCHR(st.st_mode) else None

    def is_socket(self, rel):
        try:
            return stat.S_ISSOCK(os.stat(self.p(rel)).st_mode)
        except OSError:
            return False

    def access_rw(self, rel):
        return os.access(self.p(rel), os.R_OK | os.W_OK)

    def driver_of(self, node):
        base = "sys/class/drm/%s/device" % node
        link = None
        try:
            link = os.path.basename(os.readlink(self.p(base + "/driver")))
        except OSError:
            pass
        uevent = None
        for line in (self.read(base + "/uevent") or "").splitlines():
            if line.startswith("DRIVER="):
                uevent = line[7:].strip()
        if link and uevent and link != uevent:
            return "conflict:%s/%s" % (link, uevent)
        return link or uevent

    def render_nodes(self):
        out = []
        for path in sorted(glob.glob(self.p("sys/class/drm/renderD*"))):
            node = os.path.basename(path)
            dev = (self.read("sys/class/drm/%s/dev" % node) or "").strip()
            m = re.fullmatch(r"(\d+):(\d+)", dev)
            out.append({"node": node, "driver": self.driver_of(node),
                        "sysfs_dev": [int(m.group(1)), int(m.group(2))] if m else None,
                        "devnode": "/dev/dri/" + node})
        return out

    def egl_probe(self, devnode):
        if not self.live:
            return {"ok": False, "error": "offline_root"}
        rc, out, _ = self.runner([sys.executable, "-I", "-c", EGL_PROBE_SRC, devnode], timeout=20,
                                 env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")})
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            data = {}
        if rc != 0 or not isinstance(data, dict):
            return {"ok": False, "error": "probe_rc_%s" % rc}
        res = {k: str(v)[:120] for k, v in data.items() if k in ("vendor", "renderer", "version", "egl", "error")}
        res["ok"] = data.get("ok") is True
        return res


def preflight(probe):
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    release = (probe.read("proc/sys/kernel/osrelease") or "").strip()
    kv = parse_kernel(release)
    check("kernel_min", kv is not None and kv >= KERNEL_MIN,
          "%s (minimum %d.%d)" % (release or "unreadable", *KERNEL_MIN))

    mali = [p for p in MALI_PATHS if probe.exists(p)]
    mods = MALI_MODULE_RE.findall(probe.read("proc/modules", 1 << 20) or "")
    check("no_proprietary_mali", not mali and not mods,
          "absent" if not (mali or mods) else "present: " + ", ".join(["/" + p for p in mali] + mods))

    nodes = probe.render_nodes()
    pan = [n for n in nodes if n["driver"] == "panfrost"]
    check("panfrost_driver_bound", probe.exists("sys/bus/platform/drivers/panfrost") and bool(pan),
          "render nodes: " + (", ".join("%s=%s" % (n["node"], n["driver"]) for n in nodes) or "none"))

    target = None
    for n in pan:
        rel = n["devnode"]
        if n["sysfs_dev"] and probe.rdev(rel) == tuple(n["sysfs_dev"]) and n["sysfs_dev"][0] == 226:
            target = n
            break
    check("panfrost_render_node", target is not None,
          ("%s char %d:%d" % (target["devnode"], *target["sysfs_dev"])) if target
          else "no /dev/dri/renderD* char device matching a panfrost sysfs node")
    check("render_node_access", target is not None and probe.access_rw(target["devnode"]),
          "rw by current user" if target and probe.access_rw(target["devnode"]) else "not verified")

    dri = sorted(glob.glob(probe.p("usr/lib/*/dri/panfrost_dri.so")) + glob.glob(probe.p("usr/lib/dri/panfrost_dri.so"))
                 + glob.glob(probe.p("usr/lib64/dri/panfrost_dri.so")))
    check("mesa_panfrost_dri", bool(dri), "found" if dri else "panfrost_dri.so missing (libgl1-mesa-dri)")

    glr = sorted(glob.glob(probe.p("usr/lib/*/libweston-*/gl-renderer.so"))
                 + glob.glob(probe.p("usr/lib/libweston-*/gl-renderer.so")))
    check("weston_gl_renderer", bool(glr) and probe.executable("usr/bin/weston"),
          "weston + gl-renderer.so" if glr and probe.executable("usr/bin/weston") else "weston or gl-renderer.so missing")
    tools = [t for t in ("usr/bin/chromium", "usr/bin/systemd-run", "usr/bin/systemctl") if not probe.executable(t)]
    check("tools", not tools, "ok" if not tools else "missing: " + ", ".join("/" + t for t in tools))
    check("seatd_socket", probe.is_socket("run/seatd.sock"), "/run/seatd.sock")

    egl = probe.egl_probe(target["devnode"]) if target else {"ok": False, "error": "no_panfrost_node"}
    rend = (egl.get("renderer") or "").lower()
    check("egl_panfrost", egl.get("ok") and "panfrost" in rend and not re.search(r"llvmpipe|softpipe|swrast", rend),
          egl)

    reasons = [c["name"] for c in checks if not c["ok"]]
    return {"tool": "opentv_gpu_trial", "version": VERSION, "verdict": "GO" if not reasons else "NO-GO",
            "root": probe.root, "kernel": release or None, "render_nodes": nodes,
            "panfrost_node": target["devnode"] if target else None, "checks": checks, "reasons": reasons}


# --------------------------------------------------------------------------- commandes générées
def trial_names(stamp):
    """-> (unité, répertoire d'essai, socket Wayland) ; socket distinct du répertoire (même XDG_RUNTIME_DIR)."""
    tag = "%s-%s" % (UNIT_PREFIX, stamp)
    return tag + ".service", tag, "wayland-" + tag


def weston_argv(socket_name, config_path):
    return ["weston", "--backend=drm", "--renderer=gl", "--shell=kiosk", "--idle-time=0",
            "--socket=" + socket_name, "--config=" + config_path]


def chromium_argv(profile, url):
    w, h = MODE.split("x")
    return ["chromium", "--ozone-platform=wayland", "--kiosk", "--start-fullscreen",
            "--window-size=%s,%s" % (w, h), "--force-device-scale-factor=1", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-extensions", "--disable-component-update",
            "--disable-background-networking", "--noerrdialogs", "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble", "--deny-permission-prompts", "--password-store=basic",
            "--disable-features=Translate,MediaRouter", "--ignore-gpu-blocklist",
            "--user-data-dir=" + profile, url]


def assert_gpu_profile(wargv, cargv):
    """Garde-fou : le profil d'essai ne doit jamais redevenir le profil logiciel (ni l'inverse en douce)."""
    if "--renderer=gl" not in wargv or any(a.startswith("--renderer=") and a != "--renderer=gl" for a in wargv):
        raise Refused("weston_not_gl")
    if any(a == "--disable-gpu" or a.startswith("--disable-gpu=") for a in cargv):
        raise Refused("chromium_disable_gpu")
    if "--window-size=1920,1080" not in cargv:
        raise Refused("mode_not_1080p")


def systemd_run_argv(unit, trial_dir, duration, url, socket_name, runtime_dir, script=None, python=None):
    script = script or os.path.abspath(__file__)
    python = python or sys.executable or "/usr/bin/python3"
    props = {
        "Type": "exec",
        "RuntimeMaxSec": str(duration + STARTUP_GRACE_S),
        "KillMode": "mixed",
        "KillSignal": "SIGTERM",
        "SendSIGKILL": "yes",
        "TimeoutStopSec": str(STOP_TIMEOUT_S),
        "Restart": "no",
        "NoNewPrivileges": "yes",
        "OOMScoreAdjust": "600",
        "Nice": "5",
        "UMask": "0077",
        "LogRateLimitIntervalSec": "30s",
        "LogRateLimitBurst": "200",
    }
    argv = ["systemd-run", "--user", "--unit=" + unit, "--collect", "--quiet",
            "--description=OpenTV GPU trial (bounded, auto-cleanup)", "--working-directory=" + trial_dir]
    argv += ["--property=%s=%s" % kv for kv in props.items()]
    argv += ["--setenv=LIBSEAT_BACKEND=seatd", "--setenv=PYTHONUNBUFFERED=1",
             "--setenv=XDG_RUNTIME_DIR=" + runtime_dir]
    argv += ["--", python, "-I", script, "_session", "--dir", trial_dir, "--duration", str(duration),
             "--url", url, "--socket", socket_name]
    return argv


# --------------------------------------------------------------------------- effets de bord (mockables)
def run_cmd(argv, timeout=CMD_TIMEOUT_S, env=None):
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                           env=env, errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", type(e).__name__


class System:
    """Toutes les interactions du mode `run` avec l'hôte ; remplacé par un faux dans les tests."""

    def __init__(self):
        self.env = dict(os.environ)

    def run(self, argv, timeout=CMD_TIMEOUT_S):
        return run_cmd(argv, timeout)

    def monotonic(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)

    def uid(self):
        return os.getuid()

    def http_json(self, url, timeout=3):
        with urllib.request.urlopen(url, timeout=timeout) as r:   # nosec - 127.0.0.1 uniquement
            if r.status != 200:
                raise OSError("http_%d" % r.status)
            return json.loads(r.read(65536))

    def processes(self):
        """[{pid, uid, comm, exe, cgroup}] depuis /proc ; lève OSError si /proc est illisible."""
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            base = "/proc/" + d
            try:
                with open(base + "/comm") as f:
                    comm = f.read().strip()
                with open(base + "/cmdline", "rb") as f:
                    argv0 = f.read(4096).split(b"\0")[0].decode("utf-8", "replace")
                with open(base + "/cgroup") as f:
                    cgroup = f.read(4096)
                uid = os.stat(base).st_uid
            except OSError:
                continue   # processus disparu entre-temps
            out.append({"pid": int(d), "uid": uid, "comm": comm, "exe": os.path.basename(argv0), "cgroup": cgroup})
        if not out:
            raise OSError("proc_unreadable")
        return out

    def pid_alive(self, pid):
        return os.path.exists("/proc/%d" % pid)

    def kill(self, pid, sig):
        os.kill(pid, sig)

    def stat_file(self, path):
        """(taille, mtime_ns, inode, sha256) ou None ; le contenu n'est jamais renvoyé ni journalisé."""
        try:
            st = os.stat(path)
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return (st.st_size, st.st_mtime_ns, st.st_ino, h.hexdigest())
        except FileNotFoundError:
            return None
        except OSError:
            return ("unreadable",)

    def makedirs_private(self, path):
        os.mkdir(path, 0o700)

    def write_file(self, path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def read_json(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def list_dir(self, path):
        try:
            return os.listdir(path)
        except OSError:
            return []

    def rmtree(self, path):
        shutil.rmtree(path, ignore_errors=True)

    def lock(self, path):
        """Verrou exclusif non bloquant tenu jusqu'à la fin du processus (un seul essai à la fois)."""
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._lock_fd = fd
        return True

    def check_runtime_dir(self, path):
        st = os.stat(path)
        return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and (st.st_mode & 0o077) == 0


# --------------------------------------------------------------------------- run : gardes
def systemctl_show(system, unit, props):
    argv = ["systemctl", "--user", "show", unit] + ["--property=" + p for p in props]
    rc, out, _ = system.run(argv)
    if rc != 0:
        raise Refused("systemctl_show_failed:" + unit)
    d = parse_show(out)
    missing = [p for p in props if p not in d]
    if missing:
        raise Refused("systemctl_show_incomplete:" + unit)
    return d


def dashboard_snapshot(system, unit=DASHBOARD_UNIT):
    d = systemctl_show(system, unit, ["LoadState", "ActiveState", "SubState", "MainPID", "NRestarts"])
    try:
        pid, restarts = int(d["MainPID"]), int(d["NRestarts"])
    except ValueError:
        raise Refused("dashboard_unparsable")
    return {"unit": unit, "load": d["LoadState"], "active": d["ActiveState"], "sub": d["SubState"],
            "pid": pid, "restarts": restarts, "pid_alive": pid > 0 and system.pid_alive(pid)}


def dashboard_ok(snap):
    return snap["load"] == "loaded" and snap["active"] == "active" and snap["pid"] > 0 and snap["pid_alive"]


def same_dashboard(a, b):
    keys = ("active", "pid", "restarts")
    return a is not None and b is not None and all(a[k] == b[k] for k in keys) and dashboard_ok(b)


def bridge_state(system, ui_port):
    """'idle' (service arrêté ou au tableau de bord) ou lève Refused si une session existe/est invérifiable."""
    d = systemctl_show(system, BRIDGE_UNIT, ["LoadState", "ActiveState"])
    if d["LoadState"] == "not-found" or d["ActiveState"] in ("inactive", "failed"):
        return {"service": d["ActiveState"], "session": None, "watch": False}
    if d["ActiveState"] != "active":
        raise Refused("bridge_transitioning:" + d["ActiveState"])
    try:
        st = system.http_json(BRIDGE_UI_STATE % ui_port, timeout=3)
    except Exception:
        raise Refused("bridge_state_unverifiable")
    state = st.get("state") if isinstance(st, dict) else None
    if state != "dashboard":
        raise Refused("bridge_session_active:%s" % (state if isinstance(state, str) else "unknown"))
    return {"service": "active", "session": "dashboard", "watch": True}


def display_processes(system):
    try:
        procs = system.processes()
    except OSError:
        raise Refused("proc_unreadable")
    return [{"pid": p["pid"], "comm": p["comm"]} for p in procs
            if p["comm"] in DISPLAY_COMMS or p["exe"] in DISPLAY_COMMS]


def unit_processes(system, unit):
    try:
        procs = system.processes()
    except OSError:
        return None
    needle = "/" + unit
    return [p["pid"] for p in procs if needle in p["cgroup"]]


def existing_trial_units(system):
    rc, out, _ = system.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                             "--no-pager", UNIT_PREFIX + "-*"])
    if rc != 0:
        raise Refused("systemctl_list_failed")
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


def guards(system, ui_port):
    """Contrôles en lecture seule ; renvoie le contexte ou lève Refused au premier critère non vérifié."""
    ctx = {}
    if system.uid() == 0:
        raise Refused("run_as_root")
    rt = system.env.get("XDG_RUNTIME_DIR", "")
    try:
        if not rt or not os.path.isabs(rt) or not system.check_runtime_dir(rt):
            raise Refused("xdg_runtime_dir_invalid")
    except OSError:
        raise Refused("xdg_runtime_dir_invalid")
    ctx["runtime_dir"] = rt
    if not system.lock(os.path.join(rt, UNIT_PREFIX + ".lock")):
        raise Refused("trial_locked")
    rc, _, _ = system.run(["systemctl", "--user", "show", "--property=Version", "--value"])
    if rc != 0:
        raise Refused("user_manager_unreachable")
    dash = dashboard_snapshot(system)
    if not dashboard_ok(dash):
        raise Refused("dashboard_not_healthy")
    ctx["dashboard_before"] = dash
    ctx["bridge"] = bridge_state(system, ui_port)
    procs = display_processes(system)
    if procs:
        raise Refused("display_processes_running:" + ",".join("%d/%s" % (p["pid"], p["comm"]) for p in procs))
    units = existing_trial_units(system)
    if units:
        raise Refused("trial_unit_exists:" + ",".join(units))
    home = system.env.get("HOME", "")
    if not home:
        raise Refused("home_unknown")
    ctx["env_path"] = os.path.join(home, ".config", "systemd", "user", "opentv-bridge.env")
    ctx["env_before"] = system.stat_file(ctx["env_path"])
    if ctx["env_before"] == ("unreadable",):
        raise Refused("bridge_env_unverifiable")
    return ctx


# --------------------------------------------------------------------------- run : orchestration
def unit_state(system, unit):
    rc, out, _ = system.run(["systemctl", "--user", "show", unit, "--property=LoadState",
                             "--property=ActiveState", "--property=Result"])
    if rc != 0:
        return {"ActiveState": "unknown"}
    return parse_show(out)


def cleanup(system, unit):
    """Arrêt de l'unité (idempotent), attente bornée, SIGKILL ciblé sur son cgroup, vérification finale."""
    rep = {"stop_rc": None, "killed": [], "leftover_unit_pids": None, "leftover_display": None, "ok": False}
    rep["stop_rc"] = system.run(["systemctl", "--user", "stop", unit], timeout=STOP_TIMEOUT_S + 10)[0]
    end = system.monotonic() + STOP_TIMEOUT_S + 5
    pids = unit_processes(system, unit)
    while pids and system.monotonic() < end:
        system.sleep(0.5)
        pids = unit_processes(system, unit)
    if pids:
        system.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit])
        system.sleep(1)
        for pid in unit_processes(system, unit) or []:
            try:
                system.kill(pid, signal.SIGKILL)
                rep["killed"].append(pid)
            except OSError:
                pass
        system.sleep(0.5)
        pids = unit_processes(system, unit)
    system.run(["systemctl", "--user", "reset-failed", unit])   # notre unité seulement ; erreur ignorée
    rep["leftover_unit_pids"] = pids
    try:
        rep["leftover_display"] = display_processes(system)
    except Refused:
        rep["leftover_display"] = None
    rep["ok"] = pids == [] and rep["leftover_display"] == []
    return rep


def prune_trial_dirs(system, runtime_dir, keep=KEEP_TRIAL_DIRS):
    dirs = sorted(d for d in system.list_dir(runtime_dir) if re.fullmatch(UNIT_PREFIX + r"-\d{8}T\d{6}", d))
    for d in dirs[:max(0, len(dirs) - keep + 1)]:
        system.rmtree(os.path.join(runtime_dir, d))


def run_trial(args, system, probe=None, stamp=None, script=None):
    result = {"tool": "opentv_gpu_trial", "version": VERSION, "mode": MODE, "duration_s": args.duration,
              "url": args.url, "requires_on_screen_confirmation": True}
    pf = preflight(probe or Probe("/"))
    result["preflight"] = {"verdict": pf["verdict"], "reasons": pf["reasons"], "panfrost_node": pf["panfrost_node"]}
    if pf["verdict"] != "GO":
        result["verdict"] = "no-go"
        return EXIT_NOGO, result
    try:
        ctx = guards(system, args.ui_port)
    except Refused as e:
        result.update(verdict="refused", reason=str(e))
        return EXIT_REFUSED, result
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    unit, tag, sock = trial_names(stamp)
    trial_dir = os.path.join(ctx["runtime_dir"], tag)
    argv = systemd_run_argv(unit, trial_dir, args.duration, args.url, sock, ctx["runtime_dir"], script=script)
    try:
        assert_gpu_profile(weston_argv(sock, os.path.join(trial_dir, "weston.ini")),
                           chromium_argv(os.path.join(trial_dir, "profile"), args.url))
    except Refused as e:
        result.update(verdict="refused", reason=str(e))
        return EXIT_REFUSED, result
    result.update(unit=unit, trial_dir=trial_dir, command=argv, dashboard_before=ctx["dashboard_before"],
                  bridge=ctx["bridge"])
    if args.dry_run:
        result["verdict"] = "dry-run"
        return EXIT_GO, result

    prune_trial_dirs(system, ctx["runtime_dir"])
    try:
        system.makedirs_private(trial_dir)
    except OSError:
        result.update(verdict="refused", reason="trial_dir_create_failed")
        return EXIT_REFUSED, result
    abort = None
    launched = False
    try:
        rc, _, _ = system.run(argv, timeout=CMD_TIMEOUT_S + 10)
        launched = True   # même en échec : l'unité a pu être créée, le nettoyage s'applique
        if rc != 0:
            abort = "systemd_run_failed:%d" % rc
        else:
            deadline = system.monotonic() + args.duration + STARTUP_GRACE_S + STOP_TIMEOUT_S
            while True:
                st = unit_state(system, unit)
                if st.get("ActiveState") in ("inactive", "failed") or st.get("LoadState") == "not-found":
                    result["unit_result"] = st.get("Result") or "collected"
                    break
                snap = None
                try:
                    snap = dashboard_snapshot(system)
                except Refused:
                    pass
                if not same_dashboard(ctx["dashboard_before"], snap):
                    abort = "dashboard_changed"
                    break
                if ctx["bridge"]["watch"]:
                    try:
                        bridge_state(system, args.ui_port)
                    except Refused as e:
                        abort = "bridge:" + str(e)
                        break
                if system.monotonic() > deadline:
                    abort = "timeout"
                    break
                system.sleep(1)
    except Refused as e:
        abort = str(e)
    except (Interrupted, KeyboardInterrupt):
        abort = "interrupted"
    finally:
        result["cleanup"] = cleanup(system, unit) if launched else {"ok": True, "skipped": "not_launched"}
    if abort:
        result["abort"] = abort
    session = system.read_json(os.path.join(trial_dir, "session.json"))
    result["session"] = session
    try:
        after = dashboard_snapshot(system)
    except Refused:
        after = None
    result["dashboard_after"] = after
    result["dashboard_unchanged"] = same_dashboard(ctx["dashboard_before"], after)
    result["bridge_env_unchanged"] = system.stat_file(ctx["env_path"]) == ctx["env_before"]
    if not (result["cleanup"]["ok"] and result["dashboard_unchanged"] and result["bridge_env_unchanged"]):
        result["verdict"] = "alert"
        return EXIT_ALERT, result
    ok = (not abort and isinstance(session, dict) and session.get("outcome") == "completed"
          and session.get("gpu", {}).get("panfrost_confirmed") is True)
    result["verdict"] = "completed" if ok else "failed"
    return (EXIT_GO if ok else EXIT_FAILED), result


# --------------------------------------------------------------------------- _session (dans l'unité)
class CappedLog(threading.Thread):
    """Copie un flux vers un fichier sans dépasser `cap` octets ; compte le reste et garde les lignes clés."""

    KEY_RE = re.compile(r"GL (?:renderer|vendor|version)\s*:|Using (?:GL|gl|pixman) renderer|"
                        r"GPU process|gpu_init|Exiting GPU process|EGL", re.I)

    def __init__(self, stream, path, cap):
        super().__init__(daemon=True)
        self.stream, self.path, self.cap = stream, path, cap
        self.written = self.dropped = 0
        self.keylines = []

    def run(self):
        with open(self.path, "ab") as f:
            for line in iter(self.stream.readline, b""):
                if len(self.keylines) < 40 and self.KEY_RE.search(line.decode("utf-8", "replace")):
                    self.keylines.append(line.decode("utf-8", "replace").strip()[:200])
                room = self.cap - self.written
                if room > 0:
                    chunk = line[:room]
                    f.write(chunk)
                    self.written += len(chunk)
                    self.dropped += len(line) - len(chunk)
                else:
                    self.dropped += len(line)
            if self.dropped:
                f.write(b"\n[opentv_gpu_trial: %d bytes dropped]\n" % self.dropped)

    def summary(self):
        return {"bytes": self.written, "dropped": self.dropped}


def gpu_evidence(weston_lines, chromium_lines):
    renderer = None
    for ln in weston_lines:
        m = re.search(r"GL renderer\s*:\s*(.+)$", ln, re.I)
        if m:
            renderer = m.group(1).strip()[:120]
    pixman = any(re.search(r"pixman renderer", ln, re.I) for ln in weston_lines)
    gpu_errors = sum(1 for ln in chromium_lines
                     if re.search(r"GPU process (?:exited|crashed|isn't usable)|Exiting GPU process|gpu_init", ln, re.I))
    return {"weston_gl_renderer": renderer, "weston_pixman": pixman, "chromium_gpu_errors": gpu_errors,
            "panfrost_confirmed": bool(renderer and "panfrost" in renderer.lower() and not pixman and gpu_errors == 0)}


def stop_child(proc, timeout=5.0):
    """SIGTERM, attente bornée, puis SIGKILL. Renvoie le code de sortie ou None."""
    if proc is None:
        return None
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None   # systemd (KillMode=mixed) finira le travail
    return proc.returncode


def validate_trial_dir(path, runtime_dir):
    real = os.path.realpath(path)
    if not runtime_dir or os.path.dirname(real) != os.path.realpath(runtime_dir):
        raise Refused("trial_dir_outside_runtime")
    if not re.fullmatch(UNIT_PREFIX + r"-\d{8}T\d{6}", os.path.basename(real)):
        raise Refused("trial_dir_name")
    st = os.lstat(real)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refused("trial_dir_perms")
    return real


def _is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def session_main(args, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep, install_signals=True,
                 socket_ready=_is_socket):
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    trial_dir = validate_trial_dir(args.dir, runtime_dir)
    if args.url not in URL_ALLOW or not (DURATION_MIN <= args.duration <= DURATION_MAX):
        raise Refused("session_args")
    if args.socket != "wayland-" + os.path.basename(trial_dir):
        raise Refused("session_socket_name")
    ini = os.path.join(trial_dir, "weston.ini")
    profile = os.path.join(trial_dir, "profile")
    with open(ini, "w") as f:
        f.write(WESTON_INI.format(name="HDMI-A-1", mode=MODE))
    wargv, cargv = weston_argv(args.socket, ini), chromium_argv(profile, args.url)
    assert_gpu_profile(wargv, cargv)
    stop = threading.Event()
    if install_signals:
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, lambda *_: stop.set())
    env = {k: os.environ[k] for k in ("HOME", "USER", "LANG", "PATH", "XDG_RUNTIME_DIR", "LIBSEAT_BACKEND", "TZ")
           if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    rep = {"outcome": None, "weston_rc": None, "chromium_rc": None, "ran_s": 0.0}
    weston = chromium = None
    logs = {}
    try:
        try:
            weston = popen(wargv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        except OSError:
            rep["outcome"] = "weston_spawn_failed"
            return 3, rep
        logs["weston"] = CappedLog(weston.stdout, os.path.join(trial_dir, "weston.log"), LOG_CAPS["weston"])
        logs["weston"].start()
        sock = os.path.join(runtime_dir, args.socket)
        end = clock() + STARTUP_GRACE_S / 2
        while not socket_ready(sock):
            if stop.is_set():
                rep["outcome"] = "stopped"
                return 3, rep
            if weston.poll() is not None:
                rep["outcome"] = "weston_exited_at_start"
                return 3, rep
            if clock() > end:
                rep["outcome"] = "weston_socket_timeout"
                return 3, rep
            sleep(0.1)
        env["WAYLAND_DISPLAY"] = args.socket
        try:
            chromium = popen(cargv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        except OSError:
            rep["outcome"] = "chromium_spawn_failed"
            return 3, rep
        logs["chromium"] = CappedLog(chromium.stdout, os.path.join(trial_dir, "chromium.log"), LOG_CAPS["chromium"])
        logs["chromium"].start()
        t0 = clock()
        while True:
            if stop.is_set():
                rep["outcome"] = "stopped"
                break
            if weston.poll() is not None:
                rep["outcome"] = "weston_exited"
                break
            if chromium.poll() is not None:
                rep["outcome"] = "chromium_exited"
                break
            if clock() - t0 >= args.duration:
                rep["outcome"] = "completed"
                break
            sleep(0.25)
        rep["ran_s"] = round(clock() - t0, 1)
        return (0 if rep["outcome"] == "completed" else 3), rep
    finally:
        rep["chromium_rc"] = stop_child(chromium)   # navigateur d'abord, puis compositeur (rend le maître DRM)
        rep["weston_rc"] = stop_child(weston)
        for lg in logs.values():
            lg.join(timeout=2)
        rep["logs"] = {k: v.summary() for k, v in logs.items()}
        rep["gpu"] = gpu_evidence(logs["weston"].keylines if "weston" in logs else [],
                                  logs["chromium"].keylines if "chromium" in logs else [])
        rep["key_lines"] = {k: v.keylines[:20] for k, v in logs.items()}
        shutil.rmtree(profile, ignore_errors=True)
        tmp = os.path.join(trial_dir, "session.json.tmp")
        with open(tmp, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True)
        os.replace(tmp, os.path.join(trial_dir, "session.json"))


# --------------------------------------------------------------------------- CLI
def _duration(v):
    n = int(v)
    if not DURATION_MIN <= n <= DURATION_MAX:
        raise argparse.ArgumentTypeError("duration must be in [%d, %d]" % (DURATION_MIN, DURATION_MAX))
    return n


def _url(v):
    if v not in URL_ALLOW:
        raise argparse.ArgumentTypeError("url must be one of: " + ", ".join(URL_ALLOW))
    return v


def build_parser():
    ap = argparse.ArgumentParser(prog="opentv_gpu_trial.py", description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight", help="inspect a root (read-only); exit 0 = GO, 1 = NO-GO")
    p.add_argument("--root", default="/")
    r = sub.add_parser("run", help="preflight + guards + bounded GPU trial in a transient user unit")
    r.add_argument("--duration", type=_duration, default=DURATION_DEFAULT)
    r.add_argument("--url", type=_url, default="chrome://gpu")
    r.add_argument("--ui-port", type=int, default=8098, help="bridge local UI port (state.json)")
    r.add_argument("--dry-run", action="store_true", help="checks + print the systemd-run command, launch nothing")
    s = sub.add_parser("_session", help=argparse.SUPPRESS)
    s.add_argument("--dir", required=True)
    s.add_argument("--duration", type=_duration, required=True)
    s.add_argument("--url", type=_url, required=True)
    s.add_argument("--socket", required=True)
    return ap


def main(argv=None, system=None, probe=None):
    if sys.version_info < (3, 11):
        print(_dump({"error": "python>=3.11 required"}))
        return EXIT_USAGE
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    if args.cmd == "preflight":
        if not os.path.isdir(args.root):
            print(_dump({"verdict": "NO-GO", "reasons": ["root_missing"]}))
            return EXIT_NOGO
        rep = preflight(probe or Probe(args.root))
        print(_dump(rep))
        return EXIT_GO if rep["verdict"] == "GO" else EXIT_NOGO
    if args.cmd == "run":
        if system is None:
            fired = []

            def _interrupt(*_):   # une seule fois : un 2e signal ne doit pas interrompre le nettoyage
                if not fired:
                    fired.append(1)
                    raise Interrupted()
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(s, _interrupt)
        code, rep = run_trial(args, system or System(), probe=probe)
        print(_dump(rep))
        return code
    try:
        code, _ = session_main(args)
    except Refused as e:
        print(_dump({"outcome": "refused", "reason": str(e)}), file=sys.stderr)
        return EXIT_REFUSED
    return code


if __name__ == "__main__":
    sys.exit(main())
