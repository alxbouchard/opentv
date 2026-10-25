"""Tests du banc d'essai GPU NanoPi (scripts/nanopi/opentv_gpu_trial.py).

Depuis la racine : python3 -m unittest -v native.test.test_opentv_gpu_trial
Mocks uniquement : aucun weston, chromium, systemd, EGL ni affichage réel n'est lancé.
"""
import argparse
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "scripts", "nanopi", "opentv_gpu_trial.py")
spec = importlib.util.spec_from_file_location("opentv_gpu_trial", SRC)
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

SECRET = "tok-SECRET-do-not-leak-91f3"
STAMP = "20261025T120000"
UNIT = "opentv-gpu-trial-%s.service" % STAMP


# --------------------------------------------------------------------------- racines factices
def _w(root, rel, text="", mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def make_root(base, kernel="4.19.232", mali=True, nodes=None, mesa=True, weston=True, tools=True, modules=""):
    nodes = {"renderD128": "rockchip-drm"} if nodes is None else nodes
    _w(base, "proc/sys/kernel/osrelease", kernel + "\n")
    _w(base, "proc/modules", modules)
    if mali:
        _w(base, "dev/mali0")
        os.makedirs(os.path.join(base, "sys/class/misc/mali0"), exist_ok=True)
    for node, drv in nodes.items():
        minor = int(node[len("renderD"):])
        d = os.path.join(base, "sys/class/drm", node)
        _w(base, "sys/class/drm/%s/dev" % node, "226:%d\n" % minor)
        drvdir = os.path.join(base, "sys/bus/platform/drivers", drv.split("|")[0])
        os.makedirs(drvdir, exist_ok=True)
        os.makedirs(os.path.join(d, "device"), exist_ok=True)
        os.symlink(os.path.relpath(drvdir, os.path.join(d, "device")), os.path.join(d, "device", "driver"))
        _w(base, "sys/class/drm/%s/device/uevent" % node, "DRIVER=%s\nOF_NAME=gpu\n" % drv.split("|")[-1])
        _w(base, "dev/dri/" + node)
    if mesa:
        _w(base, "usr/lib/aarch64-linux-gnu/dri/panfrost_dri.so")
    if weston:
        _w(base, "usr/lib/aarch64-linux-gnu/libweston-14/gl-renderer.so")
        _w(base, "usr/bin/weston", "#!/bin/sh\n", 0o755)
    if tools:
        for t in ("chromium", "systemd-run", "systemctl"):
            _w(base, "usr/bin/" + t, "#!/bin/sh\n", 0o755)
    _w(base, "run/seatd.sock")
    # un faux fichier d'environnement du pont avec un secret : ne doit jamais apparaître en sortie
    _w(base, "home/alex/.config/systemd/user/opentv-bridge.env", "TMDB_READ_TOKEN=%s\n" % SECRET, 0o600)
    return base


class FakeProbe(T.Probe):
    """Fichiers spéciaux (périphériques caractère, socket) simulés ; sonde EGL simulée."""

    def __init__(self, root, chardevs=("renderD128",), socket=True, rw=True, egl=None, rdev_override=None):
        super().__init__(root)
        self.chardevs, self.socket, self.rw = set(chardevs), socket, rw
        self.egl = egl if egl is not None else {"ok": True, "vendor": "Mesa", "renderer": "Mali-T860 (Panfrost)",
                                                 "version": "OpenGL ES 3.1 Mesa 25.0.7", "egl": "1.5"}
        self.rdev_override = rdev_override or {}
        self.egl_calls = []

    def rdev(self, rel):
        node = os.path.basename(rel)
        if node in self.rdev_override:
            return self.rdev_override[node]
        return (226, int(node[len("renderD"):])) if node in self.chardevs else None

    def is_socket(self, rel):
        return self.socket and super().exists(rel)

    def access_rw(self, rel):
        return self.rw

    def egl_probe(self, devnode):
        self.egl_calls.append(devnode)
        return dict(self.egl)


GOOD = dict(kernel="6.12.48+deb13-arm64", mali=False, nodes={"renderD128": "panfrost"})


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._td.name)
        self.addCleanup(self._td.cleanup)

    def root(self, **kw):
        return make_root(os.path.join(self.tmp, "root"), **kw)


# --------------------------------------------------------------------------- préflight
class PreflightTests(Tmp):
    def test_current_nanopi_shape_is_no_go(self):
        """Production : noyau 4.19.232 constructeur, /dev/mali0 Midgard, renderD128 = rockchip-drm, pas de panfrost,
        alors que weston/chromium/Mesa panfrost_dri.so sont installés : l'espace utilisateur ne suffit pas."""
        probe = FakeProbe(self.root())
        rep = T.preflight(probe)
        self.assertEqual(rep["verdict"], "NO-GO")
        for r in ("kernel_min", "no_proprietary_mali", "panfrost_driver_bound", "panfrost_render_node", "egl_panfrost"):
            self.assertIn(r, rep["reasons"])
        for ok in ("mesa_panfrost_dri", "weston_gl_renderer", "tools", "seatd_socket"):
            self.assertNotIn(ok, rep["reasons"])
        self.assertEqual(rep["render_nodes"][0]["driver"], "rockchip-drm")
        self.assertIsNone(rep["panfrost_node"])
        self.assertEqual(probe.egl_calls, [], "aucune sonde EGL sans nœud panfrost")
        self.assertIn("/dev/mali0", next(c for c in rep["checks"] if c["name"] == "no_proprietary_mali")["detail"])

    def test_current_nanopi_shape_cli_exit_nonzero(self):
        root = self.root()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = T.main(["preflight", "--root", root])
        self.assertEqual(code, T.EXIT_NOGO)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "NO-GO")

    def test_cli_real_probe_offline_root_never_go(self):
        """Racine factice parfaite mais sans sonde injectée : EGL invérifiable hors racine réelle -> NO-GO."""
        root = self.root(**GOOD)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = T.main(["preflight", "--root", root])
        rep = json.loads(buf.getvalue())
        self.assertEqual(code, T.EXIT_NOGO)
        self.assertIn("egl_panfrost", rep["reasons"])

    def test_missing_root(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(T.main(["preflight", "--root", os.path.join(self.tmp, "nope")]), T.EXIT_NOGO)

    def test_real_panfrost_shape_is_go(self):
        probe = FakeProbe(self.root(**GOOD))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = T.main(["preflight", "--root", probe.root], probe=probe)
        rep = json.loads(buf.getvalue())
        self.assertEqual((code, rep["verdict"], rep["reasons"]), (0, "GO", []))
        self.assertEqual(rep["panfrost_node"], "/dev/dri/renderD128")
        self.assertEqual(probe.egl_calls, ["/dev/dri/renderD128"])

    def test_mainline_split_display_and_gpu_nodes(self):
        """Mainline RK3399 : affichage rockchip-drm + GPU panfrost sur renderD129."""
        probe = FakeProbe(self.root(kernel="6.1.0", mali=False,
                                    nodes={"renderD128": "rockchip-drm", "renderD129": "panfrost"}),
                          chardevs=("renderD128", "renderD129"))
        rep = T.preflight(probe)
        self.assertEqual(rep["verdict"], "GO")
        self.assertEqual(rep["panfrost_node"], "/dev/dri/renderD129")

    def test_panfrost_plus_proprietary_mali_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(kernel="6.12.0", mali=True, nodes={"renderD128": "panfrost"})))
        self.assertEqual(rep["reasons"], ["no_proprietary_mali"])

    def test_kbase_module_listed_is_no_go(self):
        mods = "mali_kbase 1234567 0 - Live 0x0000000000000000\n"
        rep = T.preflight(FakeProbe(self.root(modules=mods, **GOOD)))
        self.assertIn("no_proprietary_mali", rep["reasons"])
        rep = T.preflight(FakeProbe(make_root(os.path.join(self.tmp, "r2"),
                                              modules="midgard_kbase 1 0 - Live 0x0\n", **GOOD)))
        self.assertIn("no_proprietary_mali", rep["reasons"])

    def test_devnode_not_char_device_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(**GOOD), chardevs=()))
        self.assertIn("panfrost_render_node", rep["reasons"])
        self.assertIn("egl_panfrost", rep["reasons"])

    def test_devnode_rdev_mismatch_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(**GOOD), rdev_override={"renderD128": (226, 129)}))
        self.assertIn("panfrost_render_node", rep["reasons"])

    def test_driver_link_uevent_conflict_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(kernel="6.12.0", mali=False, nodes={"renderD128": "panfrost|rockchip-drm"})))
        self.assertTrue(rep["render_nodes"][0]["driver"].startswith("conflict:"))
        self.assertIn("panfrost_render_node", rep["reasons"])

    def test_render_node_without_rw_access_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(**GOOD), rw=False))
        self.assertEqual(rep["reasons"], ["render_node_access"])

    def test_egl_variants(self):
        root = self.root(**GOOD)
        for egl in ({"ok": True, "renderer": "llvmpipe (LLVM 19.1, 128 bits)"},
                    {"ok": True, "renderer": "Panfrost llvmpipe"},
                    {"ok": False, "error": "device_not_found"},
                    {"ok": True, "renderer": ""}):
            rep = T.preflight(FakeProbe(root, egl=egl))
            self.assertEqual(rep["reasons"], ["egl_panfrost"], egl)

    def test_userspace_missing_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(mesa=False, weston=False, tools=False, **GOOD), socket=False))
        for r in ("mesa_panfrost_dri", "weston_gl_renderer", "tools", "seatd_socket"):
            self.assertIn(r, rep["reasons"])

    def test_old_kernel_alone_is_no_go(self):
        rep = T.preflight(FakeProbe(self.root(kernel="5.10.160", mali=False, nodes={"renderD128": "panfrost"})))
        self.assertEqual(rep["reasons"], ["kernel_min"])
        self.assertEqual(T.parse_kernel("6.12.48+deb13-arm64"), (6, 12))
        self.assertIsNone(T.parse_kernel("garbage"))

    def test_output_has_no_secrets(self):
        root = self.root()
        with mock.patch.dict(os.environ, {"TMDB_READ_TOKEN": SECRET, "OPENTV_KEY_PASSWORD": SECRET}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                T.main(["preflight", "--root", root], probe=FakeProbe(root))
        self.assertNotIn(SECRET, buf.getvalue())
        self.assertNotIn("TMDB", buf.getvalue())

    def test_live_egl_probe_command_is_isolated_and_restricted(self):
        calls = []

        def runner(argv, timeout, env):
            calls.append((argv, timeout, env))
            return 0, 'noise\n{"ok": true, "renderer": "Mali-T860 (Panfrost)", "vendor": "Mesa", "x": "drop"}\n', ""

        probe = T.Probe("/", runner=runner)
        with mock.patch.dict(os.environ, {"TMDB_READ_TOKEN": SECRET}):
            res = probe.egl_probe("/dev/dri/renderD129")
        argv, timeout, env = calls[0]
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertEqual(argv[-1], "/dev/dri/renderD129")
        self.assertLessEqual(timeout, 30)
        self.assertNotIn(SECRET, json.dumps(env))
        self.assertEqual(set(env), {"PATH", "HOME"})
        self.assertEqual(res, {"ok": True, "renderer": "Mali-T860 (Panfrost)", "vendor": "Mesa"})

    def test_live_egl_probe_failures(self):
        for rc, out in ((1, ""), (0, "not json"), (124, ""), (0, "[1, 2]")):
            res = T.Probe("/", runner=lambda a, timeout, env: (rc, out, "")).egl_probe("/dev/dri/renderD128")
            self.assertFalse(res["ok"], (rc, out))

    def test_egl_probe_source_is_render_node_only(self):
        compile(T.EGL_PROBE_SRC, "egl_probe", "exec")
        src = T.EGL_PROBE_SRC
        self.assertIn("0x313F", src)            # EGL_PLATFORM_DEVICE_EXT
        self.assertIn("0x3377", src)            # EGL_DRM_RENDER_NODE_FILE_EXT
        self.assertNotIn("card", src)           # jamais le nœud primaire (maître DRM)
        self.assertNotIn("gbm", src.lower())
        self.assertNotIn("wayland", src.lower())


# --------------------------------------------------------------------------- commandes générées
class CommandTests(unittest.TestCase):
    def test_weston_is_gl_drm_kiosk(self):
        a = T.weston_argv("sock", "/r/weston.ini")
        self.assertEqual(a[0], "weston")
        for f in ("--backend=drm", "--renderer=gl", "--shell=kiosk", "--socket=sock", "--config=/r/weston.ini"):
            self.assertIn(f, a)
        self.assertFalse(any("pixman" in x for x in a))

    def test_chromium_has_no_disable_gpu(self):
        a = T.chromium_argv("/r/profile", "chrome://gpu")
        self.assertFalse(any(x.startswith("--disable-gpu") for x in a))
        for f in ("--ozone-platform=wayland", "--kiosk", "--window-size=1920,1080", "--user-data-dir=/r/profile",
                  "--force-device-scale-factor=1"):
            self.assertIn(f, a)
        self.assertEqual(a[-1], "chrome://gpu")
        self.assertFalse(any("remote-debugging-port" in x for x in a))

    def test_weston_ini_is_1080p(self):
        ini = T.WESTON_INI.format(name="HDMI-A-1", mode=T.MODE)
        self.assertIn("mode=1920x1080", ini)
        self.assertIn("name=HDMI-A-1", ini)

    def test_profile_guard(self):
        w, c = T.weston_argv("s", "i"), T.chromium_argv("p", "about:blank")
        T.assert_gpu_profile(w, c)
        with self.assertRaises(T.Refused):
            T.assert_gpu_profile([x.replace("gl", "pixman") if x == "--renderer=gl" else x for x in w], c)
        with self.assertRaises(T.Refused):
            T.assert_gpu_profile(w + ["--renderer=pixman"], c)
        with self.assertRaises(T.Refused):
            T.assert_gpu_profile(w, c + ["--disable-gpu"])
        with self.assertRaises(T.Refused):
            T.assert_gpu_profile(w, c + ["--disable-gpu=1"])
        with self.assertRaises(T.Refused):
            T.assert_gpu_profile(w, [x for x in c if not x.startswith("--window-size")])

    def test_systemd_run_command(self):
        a = T.systemd_run_argv(UNIT, "/run/user/1000/t", 60, "chrome://gpu", "t", "/run/user/1000",
                               script="/home/alex/opentv-bridge/opentv_gpu_trial.py", python="/usr/bin/python3")
        self.assertEqual(a[:2], ["systemd-run", "--user"])
        for f in ("--unit=" + UNIT, "--collect", "--property=Type=exec", "--property=RuntimeMaxSec=90",
                  "--property=KillMode=mixed", "--property=SendSIGKILL=yes", "--property=TimeoutStopSec=10",
                  "--property=Restart=no", "--property=NoNewPrivileges=yes", "--property=OOMScoreAdjust=600",
                  "--setenv=LIBSEAT_BACKEND=seatd", "--setenv=XDG_RUNTIME_DIR=/run/user/1000",
                  "--working-directory=/run/user/1000/t"):
            self.assertIn(f, a)
        tail = a[a.index("--") + 1:]
        self.assertEqual(tail, ["/usr/bin/python3", "-I", "/home/alex/opentv-bridge/opentv_gpu_trial.py", "_session",
                                "--dir", "/run/user/1000/t", "--duration", "60", "--url", "chrome://gpu",
                                "--socket", "t"])
        joined = " ".join(a)
        self.assertNotIn("opentv-bridge.env", joined)
        self.assertNotIn("EnvironmentFile", joined)
        self.assertNotIn("--disable-gpu", joined)
        self.assertNotIn("muse-display", joined)

    def test_parser_bounds(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(T.main(["run", "--duration", "5"]), T.EXIT_USAGE)
            self.assertEqual(T.main(["run", "--duration", "301"]), T.EXIT_USAGE)
            self.assertEqual(T.main(["run", "--url", "https://example.com/"]), T.EXIT_USAGE)
            self.assertEqual(T.main([]), T.EXIT_USAGE)

    def test_parse_show(self):
        self.assertEqual(T.parse_show("A=1\nB=x=y\n\nnoise\n"), {"A": "1", "B": "x=y"})


# --------------------------------------------------------------------------- run (système simulé)
class FakeSystem(T.System):
    def __init__(self, home, runtime):
        self.env = {"XDG_RUNTIME_DIR": runtime, "HOME": home, "TMDB_READ_TOKEN": SECRET}
        self.runtime = runtime
        self.calls, self.kills, self.t = [], [], 0.0
        self.dash = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "MainPID": "4242",
                     "NRestarts": "0"}
        self.dash_rc = 0
        self.bridge = {"LoadState": "loaded", "ActiveState": "inactive"}
        self.bridge_http = {"state": "dashboard", "cursor": 0}
        self.procs = [{"pid": 1, "uid": 0, "comm": "systemd", "exe": "systemd", "cgroup": "0::/init.scope\n"},
                      {"pid": 4242, "uid": 1000, "comm": "python3", "exe": "python3",
                       "cgroup": "0::/user.slice/user-1000.slice/user@1000.service/app.slice/muse-display.service\n"}]
        self.proc_error = False
        self.unit_pids = []           # processus vivants dans le cgroup de l'unité d'essai
        self.unit_states = [{"LoadState": "loaded", "ActiveState": "active", "Result": "success"}] * 3 + \
                           [{"LoadState": "not-found", "ActiveState": "inactive", "Result": "success"}]
        self.trial_units = ""
        self.launch_rc = 0
        self.session = {"outcome": "completed", "ran_s": 60.0,
                        "gpu": {"panfrost_confirmed": True, "weston_gl_renderer": "Mali-T860 (Panfrost)"}}
        self.stop_clears = True       # `systemctl stop` tue le cgroup
        self.sigkill_clears = True
        self.oskill_clears = True
        self.on_tick = None
        self.on_launch = None
        self.uid_value = 1000
        self.runtime_ok = True
        self.lock_ok = True
        self.unit = None

    # -- primitives
    def run(self, argv, timeout=T.CMD_TIMEOUT_S):
        self.calls.append(list(argv))
        if argv[0] == "systemd-run":
            self.unit = next(a for a in argv if a.startswith("--unit="))[7:]
            d = argv[argv.index("--dir") + 1]
            if self.launch_rc == 0:
                self.unit_pids = [7001, 7002, 7003]
                if self.session is not None:
                    with open(os.path.join(d, "session.json"), "w") as f:
                        json.dump(self.session, f)
            if self.on_launch:
                self.on_launch(self)
            return self.launch_rc, "", ""
        assert argv[:2] == ["systemctl", "--user"], argv
        verb = argv[2]
        if verb == "show" and argv[3] == "--property=Version":
            return 0, "257\n", ""
        if verb == "show":
            unit = argv[3]
            if unit == T.DASHBOARD_UNIT:
                return self.dash_rc, "".join("%s=%s\n" % kv for kv in self.dash.items()), ""
            if unit == T.BRIDGE_UNIT:
                return 0, "".join("%s=%s\n" % kv for kv in self.bridge.items()), ""
            if unit.startswith(T.UNIT_PREFIX):
                st = self.unit_states.pop(0) if len(self.unit_states) > 1 else self.unit_states[0]
                return 0, "".join("%s=%s\n" % kv for kv in st.items()), ""
        if verb == "list-units":
            return 0, self.trial_units, ""
        if verb == "stop":
            if self.stop_clears:
                self.unit_pids = []
            return 0, "", ""
        if verb == "kill":
            if self.sigkill_clears:
                self.unit_pids = []
            return 0, "", ""
        if verb == "reset-failed":
            return 1, "", "not failed"
        raise AssertionError("unexpected command %r" % argv)

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_tick:
            self.on_tick(self)

    def uid(self):
        return self.uid_value

    def lock(self, path):
        self.lock_path = path
        return self.lock_ok

    def check_runtime_dir(self, path):
        return self.runtime_ok and os.path.isdir(path)

    def http_json(self, url, timeout=3):
        assert url.startswith("http://127.0.0.1:"), url
        if isinstance(self.bridge_http, Exception):
            raise self.bridge_http
        return self.bridge_http

    def processes(self):
        if self.proc_error:
            raise OSError("proc")
        cg = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/%s\n" % (self.unit or "x")
        names = {7001: "python3", 7002: "weston", 7003: "chromium"}
        return self.procs + [{"pid": p, "uid": 1000, "comm": names.get(p, "chromium"), "exe": names.get(p, "chromium"),
                              "cgroup": cg} for p in self.unit_pids]

    def pid_alive(self, pid):
        return pid == int(self.dash["MainPID"])

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        if self.oskill_clears:
            self.unit_pids = [p for p in self.unit_pids if p != pid]


class RunTests(Tmp):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.runtime = os.path.join(self.tmp, "run-user-1000")
        os.makedirs(self.runtime, mode=0o700)
        self.env_file = _w(self.home, ".config/systemd/user/opentv-bridge.env",
                           "DISPLAY_BACKEND=chromium\nTMDB_READ_TOKEN=%s\n" % SECRET, 0o600)
        with open(self.env_file, "rb") as f:
            self.env_bytes = f.read()
        self.env_stat = os.stat(self.env_file)
        self.sys = FakeSystem(self.home, self.runtime)
        self.good_probe = FakeProbe(make_root(os.path.join(self.tmp, "root"), **GOOD))

    def args(self, **kw):
        a = dict(duration=60, url="chrome://gpu", ui_port=8098, dry_run=False)
        a.update(kw)
        return argparse.Namespace(**a)

    def go(self, probe=None, **kw):
        code, rep = T.run_trial(self.args(**kw), self.sys, probe=probe or self.good_probe, stamp=STAMP,
                                script="/home/alex/opentv-bridge/opentv_gpu_trial.py")
        json.dumps(rep)   # sérialisable
        self.assertNotIn(SECRET, json.dumps(rep))
        return code, rep

    def verbs(self):
        return [c[2] if c[0] == "systemctl" else c[0] for c in self.calls_()]

    def calls_(self):
        return self.sys.calls

    def assert_env_untouched(self):
        with open(self.env_file, "rb") as f:
            self.assertEqual(f.read(), self.env_bytes)
        st = os.stat(self.env_file)
        self.assertEqual((st.st_mtime_ns, st.st_ino, st.st_mode), (self.env_stat.st_mtime_ns, self.env_stat.st_ino,
                                                                   self.env_stat.st_mode))

    def assert_production_read_only(self):
        for c in self.sys.calls:
            if c[0] == "systemctl" and any(u in c for u in (T.DASHBOARD_UNIT, T.BRIDGE_UNIT)):
                self.assertEqual(c[2], "show", c)
        for c in self.sys.calls:
            if c[0] == "systemctl" and c[2] in ("stop", "kill", "reset-failed"):
                self.assertEqual(c[-1], UNIT, c)

    def assert_not_launched(self):
        self.assertFalse(any(c[0] == "systemd-run" for c in self.sys.calls))
        self.assertFalse(os.path.exists(os.path.join(self.runtime, "opentv-gpu-trial-" + STAMP)))

    # -- refus avant tout lancement
    def test_current_nanopi_shape_run_refuses_without_touching_system(self):
        code, rep = self.go(probe=FakeProbe(make_root(os.path.join(self.tmp, "np"))))
        self.assertEqual((code, rep["verdict"]), (T.EXIT_NOGO, "no-go"))
        self.assertEqual(self.sys.calls, [], "aucune commande système si le préflight est NO-GO")
        self.assert_env_untouched()

    def test_refusals(self):
        cases = {
            "run_as_root": lambda s: setattr(s, "uid_value", 0),
            "trial_locked": lambda s: setattr(s, "lock_ok", False),
            "xdg_runtime_dir_invalid": lambda s: setattr(s, "runtime_ok", False),
            "dashboard_not_healthy": lambda s: s.dash.update(ActiveState="failed"),
            "systemctl_show_failed:muse-display.service": lambda s: setattr(s, "dash_rc", 1),
            "systemctl_show_incomplete:muse-display.service": lambda s: s.dash.pop("NRestarts"),
            "dashboard_unparsable": lambda s: s.dash.update(MainPID="[not set]"),
            "bridge_session_active:catalogue": lambda s: (s.bridge.update(ActiveState="active"),
                                                         setattr(s, "bridge_http", {"state": "catalogue"})),
            "bridge_session_active:player": lambda s: (s.bridge.update(ActiveState="active"),
                                                      setattr(s, "bridge_http", {"state": "player"})),
            "bridge_session_active:unknown": lambda s: (s.bridge.update(ActiveState="active"),
                                                       setattr(s, "bridge_http", ["x"])),
            "bridge_state_unverifiable": lambda s: (s.bridge.update(ActiveState="active"),
                                                   setattr(s, "bridge_http", OSError("refused"))),
            "bridge_transitioning:activating": lambda s: s.bridge.update(ActiveState="activating"),
            "display_processes_running:900/weston": lambda s: s.procs.append(
                {"pid": 900, "uid": 1000, "comm": "weston", "exe": "weston", "cgroup": ""}),
            "display_processes_running:901/chromium-browse": lambda s: s.procs.append(
                {"pid": 901, "uid": 1001, "comm": "chromium-browse", "exe": "chromium-browser", "cgroup": ""}),
            "proc_unreadable": lambda s: setattr(s, "proc_error", True),
            "trial_unit_exists:opentv-gpu-trial-20261024T100000.service": lambda s: setattr(
                s, "trial_units", "opentv-gpu-trial-20261024T100000.service loaded failed failed OpenTV GPU trial\n"),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason=reason):
                self.sys = FakeSystem(self.home, self.runtime)
                mutate(self.sys)
                code, rep = self.go()
                self.assertEqual((code, rep["verdict"], rep["reason"]), (T.EXIT_REFUSED, "refused", reason))
                self.assert_not_launched()
                self.assert_production_read_only()
        self.assert_env_untouched()

    def test_dashboard_pid_dead_refused(self):
        self.sys.pid_alive = lambda pid: False
        code, rep = self.go()
        self.assertEqual(rep["reason"], "dashboard_not_healthy")

    def test_bridge_idle_at_dashboard_is_allowed_and_watched(self):
        self.sys.bridge.update(ActiveState="active")
        code, rep = self.go()
        self.assertEqual(code, 0, rep)
        self.assertTrue(rep["bridge"]["watch"])

    def test_unreadable_env_refused(self):
        self.sys.stat_file = lambda p: ("unreadable",)
        code, rep = self.go()
        self.assertEqual(rep["reason"], "bridge_env_unverifiable")
        self.assert_not_launched()

    def test_dry_run_prints_command_and_launches_nothing(self):
        code, rep = self.go(dry_run=True)
        self.assertEqual((code, rep["verdict"]), (0, "dry-run"))
        self.assertEqual(rep["command"][0], "systemd-run")
        self.assertIn("--property=RuntimeMaxSec=90", rep["command"])
        self.assert_not_launched()
        self.assert_production_read_only()

    # -- essai complet
    def test_happy_path(self):
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"]), (0, "completed"), rep)
        self.assertTrue(rep["dashboard_unchanged"])
        self.assertTrue(rep["bridge_env_unchanged"])
        self.assertTrue(rep["cleanup"]["ok"])
        self.assertTrue(rep["requires_on_screen_confirmation"])
        self.assertEqual(rep["dashboard_before"]["pid"], rep["dashboard_after"]["pid"])
        launch = next(c for c in self.sys.calls if c[0] == "systemd-run")
        self.assertIn("--unit=" + UNIT, launch)
        sock = launch[launch.index("--socket") + 1]
        tdir_arg = launch[launch.index("--dir") + 1]
        self.assertEqual(sock, "wayland-opentv-gpu-trial-" + STAMP)
        self.assertNotEqual(os.path.basename(tdir_arg), sock, "socket Wayland distinct du répertoire d'essai")
        self.assertIn(["systemctl", "--user", "stop", UNIT], self.sys.calls, "nettoyage même après succès")
        tdir = os.path.join(self.runtime, "opentv-gpu-trial-" + STAMP)
        self.assertEqual(os.stat(tdir).st_mode & 0o777, 0o700)
        self.assert_env_untouched()
        self.assert_production_read_only()

    def test_env_change_during_trial_is_alert(self):
        """Preuve que la comparaison fonctionne : si quelqu'un réécrit l'env pendant l'essai -> alerte."""
        def tamper(s):
            with open(self.env_file, "a") as f:
                f.write("X=1\n")
        self.sys.on_launch = tamper
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"], rep["bridge_env_unchanged"]), (T.EXIT_ALERT, "alert", False))

    def test_absent_env_stays_absent(self):
        os.remove(self.env_file)
        code, rep = self.go()
        self.assertEqual(code, 0)
        self.assertTrue(rep["bridge_env_unchanged"])
        self.assertFalse(os.path.exists(self.env_file))

    def test_completed_without_panfrost_evidence_fails(self):
        self.sys.session = {"outcome": "completed", "gpu": {"panfrost_confirmed": False, "weston_pixman": True}}
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"]), (T.EXIT_FAILED, "failed"))
        self.assertTrue(rep["cleanup"]["ok"])

    def test_missing_session_report_fails(self):
        self.sys.session = None
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"]), (T.EXIT_FAILED, "failed"))

    def test_dashboard_pid_change_aborts_and_alerts(self):
        def tick(s):
            s.dash["MainPID"] = "5555"
        self.sys.on_tick = tick
        self.sys.pid_alive = lambda pid: True
        self.sys.unit_states = [{"LoadState": "loaded", "ActiveState": "active"}]
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"], rep["abort"]), (T.EXIT_ALERT, "alert", "dashboard_changed"))
        self.assertIn(["systemctl", "--user", "stop", UNIT], self.sys.calls)
        self.assertFalse(rep["dashboard_unchanged"])
        self.assert_production_read_only()

    def test_dashboard_restart_count_change_aborts(self):
        self.sys.on_tick = lambda s: s.dash.update(NRestarts="1")
        self.sys.unit_states = [{"LoadState": "loaded", "ActiveState": "active"}]
        code, rep = self.go()
        self.assertEqual((rep["abort"], code), ("dashboard_changed", T.EXIT_ALERT))

    def test_bridge_session_starting_mid_trial_aborts(self):
        self.sys.bridge.update(ActiveState="active")
        self.sys.on_tick = lambda s: setattr(s, "bridge_http", {"state": "starting"})
        self.sys.unit_states = [{"LoadState": "loaded", "ActiveState": "active"}]
        code, rep = self.go()
        self.assertEqual(rep["abort"], "bridge:bridge_session_active:starting")
        self.assertEqual((code, rep["verdict"]), (T.EXIT_FAILED, "failed"))
        self.assertTrue(rep["cleanup"]["ok"])

    def test_unit_never_ends_hits_timeout_and_is_stopped(self):
        self.sys.unit_states = [{"LoadState": "loaded", "ActiveState": "active"}]
        code, rep = self.go(duration=10)
        self.assertEqual((rep["abort"], code), ("timeout", T.EXIT_FAILED))
        bound = 10 + T.STARTUP_GRACE_S + T.STOP_TIMEOUT_S
        self.assertLessEqual(self.sys.t, bound + 2)
        self.assertIn(["systemctl", "--user", "stop", UNIT], self.sys.calls)

    def test_systemd_run_failure_still_cleans_up(self):
        self.sys.launch_rc = 1
        code, rep = self.go()
        self.assertEqual((rep["abort"], code), ("systemd_run_failed:1", T.EXIT_FAILED))
        self.assertIn(["systemctl", "--user", "stop", UNIT], self.sys.calls)

    def test_interrupt_cleans_up(self):
        def tick(s):
            raise T.Interrupted()
        self.sys.on_tick = tick
        self.sys.unit_states = [{"LoadState": "loaded", "ActiveState": "active"}]
        code, rep = self.go()
        self.assertEqual(rep["abort"], "interrupted")
        self.assertIn(["systemctl", "--user", "stop", UNIT], self.sys.calls)

    def test_stop_ignored_then_sigkill_unit(self):
        self.sys.stop_clears = False
        code, rep = self.go()
        self.assertIn(["systemctl", "--user", "kill", "--signal=SIGKILL", UNIT], self.sys.calls)
        self.assertTrue(rep["cleanup"]["ok"])
        self.assertEqual(code, 0)

    def test_stop_and_unit_kill_ignored_then_targeted_pid_kill(self):
        self.sys.stop_clears = self.sys.sigkill_clears = False
        code, rep = self.go()
        self.assertEqual(sorted(p for p, _ in self.sys.kills), [7001, 7002, 7003])
        self.assertTrue(all(sig == signal.SIGKILL for _, sig in self.sys.kills))
        self.assertNotIn(4242, [p for p, _ in self.sys.kills], "jamais le tableau de bord")
        self.assertTrue(rep["cleanup"]["ok"])

    def test_unkillable_leftovers_is_alert(self):
        self.sys.stop_clears = self.sys.sigkill_clears = self.sys.oskill_clears = False
        code, rep = self.go()
        self.assertEqual((code, rep["verdict"]), (T.EXIT_ALERT, "alert"))
        self.assertFalse(rep["cleanup"]["ok"])
        self.assertEqual(sorted(rep["cleanup"]["leftover_unit_pids"]), [7001, 7002, 7003])

    def test_foreign_display_process_appearing_is_alert(self):
        def launch(s):
            s.procs.append({"pid": 950, "uid": 1000, "comm": "weston", "exe": "weston", "cgroup": "0::/other\n"})
        self.sys.on_launch = launch
        code, rep = self.go()
        self.assertEqual(code, T.EXIT_ALERT)
        self.assertEqual(rep["cleanup"]["leftover_display"], [{"pid": 950, "comm": "weston"}])
        self.assertNotIn(950, [p for p, _ in self.sys.kills], "seuls les PID du cgroup de l'essai sont tués")

    def test_prune_keeps_recent_trial_dirs(self):
        for stamp in ("20261020T100000", "20261021T100000", "20261022T100000", "20261023T100000"):
            os.mkdir(os.path.join(self.runtime, "opentv-gpu-trial-" + stamp), 0o700)
        os.mkdir(os.path.join(self.runtime, "unrelated"))
        code, _ = self.go()
        left = sorted(os.listdir(self.runtime))
        self.assertEqual(left, ["opentv-gpu-trial-20261022T100000", "opentv-gpu-trial-20261023T100000",
                                "opentv-gpu-trial-" + STAMP, "unrelated"])

    def test_cli_run_with_injected_system_does_not_install_signals(self):
        before = signal.getsignal(signal.SIGTERM)
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(T.time, "strftime", return_value=STAMP):
            code = T.main(["run", "--duration", "10", "--dry-run"], system=self.sys, probe=self.good_probe)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "dry-run")
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertNotIn(SECRET, buf.getvalue())


# --------------------------------------------------------------------------- _session (dans l'unité)
class FakeProc:
    def __init__(self, name, events, lines=(), exit_after_polls=None, ignore_term=False, ignore_kill=False):
        self.name, self.events = name, events
        self.stdout = io.BytesIO(b"".join(lines))
        self.returncode = None
        self.polls, self.exit_after_polls = 0, exit_after_polls
        self.ignore_term, self.ignore_kill = ignore_term, ignore_kill

    def poll(self):
        self.polls += 1
        if self.returncode is None and self.exit_after_polls is not None and self.polls > self.exit_after_polls:
            self.returncode = 1
        return self.returncode

    def terminate(self):
        self.events.append(("term", self.name))
        if not self.ignore_term:
            self.returncode = -15

    def kill(self):
        self.events.append(("kill", self.name))
        if not self.ignore_kill:
            self.returncode = -9

    def wait(self, timeout=None):
        self.events.append(("wait", self.name, timeout))
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.name, timeout)
        return self.returncode


WESTON_LINES = [b"[12:00:00.000] weston 14.0.2\n", b"[12:00:00.100] Using GL renderer\n",
                b"[12:00:00.200] GL version: OpenGL ES 3.1 Mesa 25.0.7\n", b"[12:00:00.210] GL vendor: Panfrost\n",
                b"[12:00:00.220] GL renderer: Mali-T860 (Panfrost)\n"]


class SessionTests(Tmp):
    def setUp(self):
        super().setUp()
        self.runtime = os.path.join(self.tmp, "rt")
        os.makedirs(self.runtime, mode=0o700)
        self.tdir = os.path.join(self.runtime, "opentv-gpu-trial-" + STAMP)
        os.mkdir(self.tdir, 0o700)
        os.makedirs(os.path.join(self.tdir, "profile"))
        self.events, self.spawned = [], []
        self.t = [0.0]
        self.weston_kw, self.chromium_kw = {"lines": WESTON_LINES}, {}
        self.create_socket = True
        p = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.runtime, "PATH": "/usr/bin:/bin",
                                         "TMDB_READ_TOKEN": SECRET, "HOME": self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def popen(self, argv, **kw):
        name = argv[0]
        self.spawned.append((argv, kw))
        self.events.append(("spawn", name))
        proc = FakeProc(name, self.events, **(self.weston_kw if name == "weston" else self.chromium_kw))
        if name == "weston" and self.create_socket:
            open(os.path.join(self.runtime, "wayland-opentv-gpu-trial-" + STAMP), "w").close()
        return proc

    def clock(self):
        return self.t[0]

    def sleep(self, s):
        self.t[0] += s

    def ns(self, **kw):
        a = dict(dir=self.tdir, duration=10, url="chrome://gpu", socket="wayland-opentv-gpu-trial-" + STAMP)
        a.update(kw)
        return argparse.Namespace(**a)

    def run_session(self, **kw):
        code, rep = T.session_main(self.ns(**kw), popen=self.popen, clock=self.clock, sleep=self.sleep,
                                   install_signals=False, socket_ready=os.path.exists)
        with open(os.path.join(self.tdir, "session.json")) as f:
            self.assertEqual(json.load(f), json.loads(json.dumps(rep)))
        return code, rep

    def test_completed_session_order_and_evidence(self):
        code, rep = self.run_session()
        self.assertEqual((code, rep["outcome"]), (0, "completed"))
        self.assertGreaterEqual(rep["ran_s"], 10)
        order = [e for e in self.events if e[0] in ("spawn", "term")]
        self.assertEqual(order, [("spawn", "weston"), ("spawn", "chromium"), ("term", "chromium"), ("term", "weston")])
        self.assertTrue(rep["gpu"]["panfrost_confirmed"])
        self.assertEqual(rep["gpu"]["weston_gl_renderer"], "Mali-T860 (Panfrost)")
        self.assertFalse(os.path.exists(os.path.join(self.tdir, "profile")), "profil temporaire supprimé")
        wargv, wkw = self.spawned[0]
        cargv, ckw = self.spawned[1]
        self.assertIn("--renderer=gl", wargv)
        self.assertNotIn("--disable-gpu", cargv)
        self.assertEqual(ckw["env"]["WAYLAND_DISPLAY"], "wayland-opentv-gpu-trial-" + STAMP)
        self.assertNotIn("TMDB_READ_TOKEN", ckw["env"])
        self.assertNotIn("TMDB_READ_TOKEN", wkw["env"])
        with open(os.path.join(self.tdir, "weston.ini")) as f:
            self.assertIn("mode=1920x1080", f.read())
        with open(os.path.join(self.tdir, "weston.log"), "rb") as f:
            self.assertEqual(f.read(), b"".join(WESTON_LINES))

    def test_socket_timeout_stops_weston_and_never_starts_chromium(self):
        self.create_socket = False
        code, rep = self.run_session()
        self.assertEqual((code, rep["outcome"]), (3, "weston_socket_timeout"))
        self.assertNotIn(("spawn", "chromium"), self.events)
        self.assertIn(("term", "weston"), self.events)
        self.assertLessEqual(self.t[0], T.STARTUP_GRACE_S)

    def test_weston_exits_at_start(self):
        self.create_socket = False
        self.weston_kw = {"exit_after_polls": 0, "lines": [b"failed to create gl renderer\n"]}
        code, rep = self.run_session()
        self.assertEqual(rep["outcome"], "weston_exited_at_start")
        self.assertFalse(rep["gpu"]["panfrost_confirmed"])

    def test_chromium_exits_early(self):
        self.chromium_kw = {"exit_after_polls": 3, "lines": [b"Exiting GPU process due to errors during initialization\n"]}
        code, rep = self.run_session()
        self.assertEqual((code, rep["outcome"], rep["chromium_rc"]), (3, "chromium_exited", 1))
        self.assertEqual(rep["gpu"]["chromium_gpu_errors"], 1)
        self.assertFalse(rep["gpu"]["panfrost_confirmed"])

    def test_weston_dies_mid_session(self):
        self.weston_kw = {"lines": WESTON_LINES, "exit_after_polls": 5}
        code, rep = self.run_session()
        self.assertEqual(rep["outcome"], "weston_exited")
        self.assertIn(("term", "chromium"), self.events)

    def test_chromium_ignoring_sigterm_is_killed(self):
        self.chromium_kw = {"ignore_term": True}
        code, rep = self.run_session()
        self.assertEqual(rep["outcome"], "completed")
        self.assertIn(("kill", "chromium"), self.events)
        self.assertEqual(rep["chromium_rc"], -9)
        i_kill = self.events.index(("kill", "chromium"))
        i_wterm = self.events.index(("term", "weston"))
        self.assertLess(i_kill, i_wterm, "navigateur tué avant de stopper le compositeur")
        waits = [e for e in self.events if e[0] == "wait"]
        self.assertTrue(all(e[2] is not None and e[2] <= 5 for e in waits), "attentes toujours bornées")

    def test_unkillable_child_returns_none_and_does_not_hang(self):
        self.chromium_kw = {"ignore_term": True, "ignore_kill": True}
        code, rep = self.run_session()
        self.assertIsNone(rep["chromium_rc"])
        self.assertIn(("term", "weston"), self.events)

    def test_sigterm_from_systemd_stops_cleanly(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])
        fired = []

        def sleep(s):
            self.t[0] += s
            if self.t[0] > 2 and not fired:
                fired.append(1)
                os.kill(os.getpid(), signal.SIGTERM)

        code, rep = T.session_main(self.ns(), popen=self.popen, clock=self.clock, sleep=sleep, install_signals=True,
                                   socket_ready=os.path.exists)
        self.assertEqual((code, rep["outcome"]), (3, "stopped"))
        self.assertEqual([e for e in self.events if e[0] == "term"], [("term", "chromium"), ("term", "weston")])

    def test_trial_dir_validation(self):
        bad_parent = os.path.join(self.tmp, "opentv-gpu-trial-" + STAMP)
        os.mkdir(bad_parent, 0o700)
        with self.assertRaises(T.Refused):
            T.validate_trial_dir(bad_parent, self.runtime)
        bad_name = os.path.join(self.runtime, "evil")
        os.mkdir(bad_name, 0o700)
        with self.assertRaises(T.Refused):
            T.validate_trial_dir(bad_name, self.runtime)
        os.chmod(self.tdir, 0o755)
        with self.assertRaises(T.Refused):
            T.validate_trial_dir(self.tdir, self.runtime)
        os.chmod(self.tdir, 0o700)
        link = os.path.join(self.runtime, "opentv-gpu-trial-20261025T130000")
        os.symlink(bad_parent, link)
        with self.assertRaises(T.Refused):
            T.validate_trial_dir(link, self.runtime)
        self.assertEqual(T.validate_trial_dir(self.tdir, self.runtime), os.path.realpath(self.tdir))
        with self.assertRaises(T.Refused):
            T.validate_trial_dir(self.tdir, "")

    def test_spawn_failures(self):
        def boom(argv, **kw):
            raise FileNotFoundError(argv[0])
        code, rep = T.session_main(self.ns(), popen=boom, clock=self.clock, sleep=self.sleep,
                                   install_signals=False, socket_ready=os.path.exists)
        self.assertEqual((code, rep["outcome"]), (3, "weston_spawn_failed"))

        def only_weston(argv, **kw):
            if argv[0] == "chromium":
                raise FileNotFoundError(argv[0])
            return self.popen(argv, **kw)
        code, rep = T.session_main(self.ns(), popen=only_weston, clock=self.clock, sleep=self.sleep,
                                   install_signals=False, socket_ready=os.path.exists)
        self.assertEqual(rep["outcome"], "chromium_spawn_failed")
        self.assertIn(("term", "weston"), self.events)
        self.assertTrue(os.path.exists(os.path.join(self.tdir, "session.json")))

    def test_session_rejects_foreign_socket_name(self):
        for sock in ("opentv-gpu-trial-" + STAMP, "wayland-0", "../wayland-opentv-gpu-trial-" + STAMP):
            with self.assertRaises(T.Refused):
                T.session_main(self.ns(socket=sock), popen=self.popen, install_signals=False)
        self.assertEqual(self.spawned, [])

    def test_default_socket_check_rejects_regular_file(self):
        f = os.path.join(self.runtime, "wayland-opentv-gpu-trial-" + STAMP)
        open(f, "w").close()
        self.assertFalse(T._is_socket(f))
        self.assertFalse(T._is_socket(self.tdir))

    def test_session_rejects_unlisted_url(self):
        with self.assertRaises(T.Refused):
            T.session_main(self.ns(url="https://example.com/"), popen=self.popen, install_signals=False)
        self.assertEqual(self.spawned, [])

    def test_capped_log(self):
        path = os.path.join(self.tmp, "big.log")
        lg = T.CappedLog(io.BytesIO(b"x" * 900 + b"\n" + b"GL renderer: Mali-T860 (Panfrost)\n" + b"y" * 5000 + b"\n"),
                         path, 1000)
        lg.start()
        lg.join(5)
        self.assertEqual(lg.summary()["bytes"], 1000)
        self.assertEqual(lg.summary()["dropped"], 901 + 34 + 5001 - 1000)
        self.assertEqual(lg.keylines, ["GL renderer: Mali-T860 (Panfrost)"])
        self.assertLess(os.path.getsize(path), 1100)
        with open(path, "rb") as f:
            self.assertIn(b"bytes dropped", f.read())

    def test_gpu_evidence(self):
        ok = T.gpu_evidence([l.decode() for l in WESTON_LINES], [])
        self.assertTrue(ok["panfrost_confirmed"])
        self.assertFalse(T.gpu_evidence(["Using pixman renderer", "GL renderer: Mali-T860 (Panfrost)"], [])
                         ["panfrost_confirmed"])
        self.assertFalse(T.gpu_evidence(["GL renderer: llvmpipe (LLVM 19)"], [])["panfrost_confirmed"])
        self.assertFalse(T.gpu_evidence(["GL renderer: Mali-T860 (Panfrost)"],
                                        ["GPU process exited unexpectedly: exit_code=139"])["panfrost_confirmed"])
        self.assertFalse(T.gpu_evidence([], [])["panfrost_confirmed"])


# --------------------------------------------------------------------------- garde-fous statiques
class LockTests(Tmp):
    def test_real_lock_is_exclusive(self):
        path = os.path.join(self.tmp, "t.lock")
        a, b = T.System(), T.System()
        self.assertTrue(a.lock(path))
        self.assertFalse(b.lock(path))
        os.close(a._lock_fd)
        self.assertTrue(b.lock(path))
        os.close(b._lock_fd)


class StaticTests(unittest.TestCase):
    def setUp(self):
        with open(SRC) as f:
            self.src = f.read()

    def test_never_mutates_production_units_or_env(self):
        import re
        self.assertNotRegex(self.src, r"(stop|restart|kill|disable|enable|mask|edit|set-property)\W+[^\n]*muse-display")
        self.assertNotRegex(self.src, r"(stop|restart|kill|disable|enable|mask|edit|set-property)\W+[^\n]*BRIDGE_UNIT")
        self.assertNotIn("daemon-reload", self.src)
        # le seul usage du chemin d'env est la comparaison stat/sha256
        uses = [m.start() for m in re.finditer(r"opentv-bridge\.env", self.src)]
        self.assertGreaterEqual(len(uses), 1)
        self.assertNotRegex(self.src, r"open\([^)]*env_path[^)]*[\"'][wa]")
        self.assertNotIn("EnvironmentFile", self.src)

    def test_disable_gpu_only_in_guard(self):
        lines = [l for l in self.src.splitlines() if '"--disable-gpu' in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(all("Refused" in l or "startswith" in l or "a ==" in l for l in lines), lines)

    def test_stdlib_only_and_py311(self):
        import ast
        tree = ast.parse(self.src, feature_version=(3, 11))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module.split(".")[0])
        import sys
        self.assertTrue(mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
