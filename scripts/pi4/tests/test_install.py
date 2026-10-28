"""Installation réversible : dry-run, rootfs temporaire, manifeste, sauvegardes, désinstallation, préparation Pi,
unités systemd, kiosque sans flags Rockchip, absence de secrets, production intacte."""
import configparser
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from tests._common import PI4, ROOT, NANOPI
from tests.test_service import fixture_root
from opentv_pi import health

INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
PREPARE = os.path.join(PI4, "prepare-pi.sh")
SPLASH = os.path.join(PI4, "system", "opentv-splash")
BOOT_CHECK = os.path.join(PI4, "system", "opentv-boot-check")
SHELL_SCRIPTS = (INSTALL, UNINSTALL, PREPARE, os.path.join(PI4, "bin", "opentv-kiosk-browser"), SPLASH, BOOT_CHECK)
UNITS = ("opentv.service", "opentv-kiosk.service", "opentv-net-helper.service", "opentv-net-helper.path",
         "opentv-ap-guard.service", "opentv-ap-guard.timer", "opentv-cec.service", "opentv-cec.timer",
         "opentv-splash.service", "opentv-player.service", "opentv-player-keys.service")


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree(root):
    out = []
    for d, _, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def sh(args, **kw):
    env = dict(os.environ, LC_ALL="C")
    env.pop("OPENTV_ROOT", None)
    return subprocess.run(["bash"] + list(args), capture_output=True, timeout=120, env=env, **kw)


class Base(unittest.TestCase):
    def root(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        return r


class TestShell(unittest.TestCase):
    def test_bash_n(self):
        for s in SHELL_SCRIPTS:
            p = subprocess.run(["bash", "-n", s], capture_output=True)
            self.assertEqual(p.returncode, 0, (s, p.stderr))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck absent")
    def test_shellcheck(self):
        p = subprocess.run(["shellcheck", "-x"] + list(SHELL_SCRIPTS), capture_output=True)
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())


class TestInstaller(Base):
    def test_dry_run_writes_nothing(self):
        r = self.root()
        p = sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(tree(r), [])
        out = p.stdout.decode()
        self.assertIn("[dry-run]", out)
        self.assertIn("/opt/opentv/lib/foryou/opentv_foryou.py", out)
        self.assertIn("systemctl enable", out)

    def test_dry_run_on_host_root_needs_no_privilege(self):
        p = sh([INSTALL, "--dry-run"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("préflight", p.stdout.decode())

    def test_real_root_requires_privilege_and_flags_are_guarded(self):
        if os.geteuid() == 0:
            self.skipTest("exécuté en root")
        p = sh([INSTALL])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("root", p.stderr.decode())
        p = sh([INSTALL, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        p = sh([INSTALL, "--root", "relative/path", "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)

    def test_install_temp_root_manifest_and_modes(self):
        r = self.root()
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        j = lambda *a: os.path.join(r, *a)   # noqa: E731
        self.assertEqual(sha(j("opt/opentv/lib/foryou/opentv_foryou.py")), sha(os.path.join(NANOPI, "opentv_foryou.py")))
        self.assertEqual(sha(j("opt/opentv/lib/foryou/opentv_bridge.py")), sha(os.path.join(NANOPI, "opentv_bridge.py")))
        self.assertEqual(sha(j("opt/opentv/share/ui/qrcode.js")), sha(os.path.join(ROOT, "native", "app", "qrcode.js")))
        for u in UNITS:
            self.assertTrue(os.path.isfile(j("etc/systemd/system", u)), u)
        self.assertEqual(stat.S_IMODE(os.stat(j("opt/opentv/bin/opentv-pi")).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(j("opt/opentv/lib/opentv_pi/app.py")).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(j("etc/opentv/opentv.env")).st_mode), 0o640)
        self.assertTrue(os.path.isfile(j("etc/sysusers.d/opentv.conf")))
        self.assertTrue(os.path.isfile(j("etc/pam.d/opentv-kiosk")))
        man = health.verify_manifest(r)
        self.assertTrue(man["ok"], man)
        self.assertGreater(man["checked"], 30)
        # le manifeste ne liste pas la configuration locale (modifiable) ni d'état
        with open(j("opt/opentv/MANIFEST.sha256")) as f:
            m = f.read()
        self.assertNotIn("/etc/opentv/opentv.env", m)
        self.assertNotIn(r, m)
        # lanceurs installés fonctionnels hors du dépôt
        q = subprocess.run([sys.executable, "-I", j("opt/opentv/bin/opentv-pi"), "manifest", "verify", "--root", r],
                           capture_output=True, timeout=30)
        self.assertEqual(q.returncode, 0, q.stderr)
        q = subprocess.run([sys.executable, "-I", j("opt/opentv/bin/opentv-mcp"), "--config", j("nope.json")],
                           capture_output=True, timeout=30)
        self.assertEqual(q.returncode, 2)
        self.assertIn(b"config_unreadable", q.stderr)
        out = p.stdout.decode()
        self.assertIn("systemctl daemon-reload", out)              # imprimé, non exécuté en rootfs de test

    def test_reinstall_backs_up_and_uninstall_restores(self):
        r = self.root()
        pam = os.path.join(r, "etc/pam.d/opentv-kiosk")
        os.makedirs(os.path.dirname(pam))
        with open(pam, "w") as f:
            f.write("# PAM local préexistant\n")
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        with open(os.path.join(r, "etc/opentv/opentv.env"), "a") as f:
            f.write("OPENTV_TV_PORT=8090\n")                         # configuration locale conservée
        with open(os.path.join(r, "opt/opentv/lib/opentv_pi/app.py"), "a") as f:
            f.write("# modif locale\n")
        self.assertFalse(health.verify_manifest(r)["ok"])
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(health.verify_manifest(r)["ok"])
        with open(os.path.join(r, "etc/opentv/opentv.env")) as f:
            self.assertIn("OPENTV_TV_PORT=8090", f.read())
        backups = [x for x in tree(r) if x.startswith("var/lib/opentv-install/backup-")]
        self.assertTrue(any(x.endswith("etc/pam.d/opentv-kiosk") for x in backups), backups)
        # désinstallation à blanc : rien ne bouge
        before = tree(r)
        p = sh([UNINSTALL, "--root", r, "--dry-run"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(tree(r), before)
        p = sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        with open(pam) as f:
            self.assertEqual(f.read(), "# PAM local préexistant\n")     # état d'avant installation restauré
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))
        for u in UNITS:
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))
        self.assertTrue(os.path.exists(os.path.join(r, "etc/opentv/opentv.env")))   # sans --purge
        p = sh([UNINSTALL, "--root", r, "--purge"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(r, "etc/opentv")))

    def test_purge_removes_state_and_opentv_nm_profiles_only(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        nm = os.path.join(r, "etc/NetworkManager/system-connections")
        os.makedirs(nm)
        for n in ("opentv-wifi.nmconnection", "opentv-setup-ap.nmconnection", "Maison.nmconnection"):
            with open(os.path.join(nm, n), "w") as f:
                f.write("[connection]\n")
        os.makedirs(os.path.join(r, "var/lib/opentv/foryou"))
        p = sh([UNINSTALL, "--root", r, "--purge"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(os.listdir(nm)), ["Maison.nmconnection", "opentv-wifi.nmconnection"])
        self.assertFalse(os.path.exists(os.path.join(r, "var/lib/opentv")))
        p = sh([UNINSTALL, "--root", r, "--purge", "--forget-wifi"])
        self.assertEqual(sorted(os.listdir(nm)), ["Maison.nmconnection"])


class TestPlaybackGateInstall(Base):
    """« Regarder » : services livrés et activés, mais jamais la capacité de lecture — le témoin de promotion n'est ni
    livré, ni écrit, ni listé ; la désinstallation le retire toujours ; source.env reste une configuration locale."""
    PROMO = "etc/opentv/playback-promoted"

    def test_install_ships_player_and_keys_helper_without_promotion(self):
        r = self.root()
        p = sh([INSTALL, "--root", r, "--skip-preflight"])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out + p.stderr.decode())
        j = lambda *a: os.path.join(r, *a)   # noqa: E731
        for rel, mode in (("opt/opentv/bin/opentv-player", 0o755), ("opt/opentv/bin/opentv-player-keys", 0o755),
                          ("etc/systemd/system/opentv-player.service", 0o644),
                          ("etc/systemd/system/opentv-player-keys.service", 0o644),
                          ("opt/opentv/share/chromium-player-policy/opentv-player.json", 0o644),
                          ("opt/opentv/lib/opentv_pi/playerkeys.py", 0o644),
                          ("opt/opentv/lib/opentv_pi/promotion.py", 0o644)):
            self.assertEqual(stat.S_IMODE(os.stat(j(rel)).st_mode), mode, rel)
        self.assertFalse(os.path.lexists(j(self.PROMO)))
        self.assertFalse(os.path.lexists(j("etc/opentv/source.env")))
        with open(j("opt/opentv/MANIFEST.sha256")) as f:
            man = f.read()
        for rel in ("/opt/opentv/bin/opentv-player-keys", "/etc/systemd/system/opentv-player-keys.service",
                    "/opt/opentv/share/chromium-player-policy/opentv-player.json"):
            self.assertIn(rel, man)
        self.assertNotIn("playback-promoted", man)
        self.assertNotIn("source.env", man)
        # la politique du lecteur n'est jamais posée dans /etc (elle remplacerait celle du kiosque)
        self.assertFalse(os.path.exists(j("etc/chromium/policies/managed/opentv-player.json")))
        enable = [l for l in out.splitlines() if "systemctl enable" in l][0]
        restart = [l for l in out.splitlines() if "systemctl restart" in l][0]
        for u in ("opentv-player.service", "opentv-player-keys.service"):
            self.assertIn(u, enable)
            self.assertIn(u, restart)
        self.assertIn("drop_kiosk_input", out)                     # ancienne appartenance input retirée
        self.assertIn("« Regarder » : NON promu", out)
        self.assertTrue(health.verify_manifest(r)["ok"])
        # le lanceur installé de l'aide des touches démarre hors du dépôt (aide sans argument : usage)
        q = subprocess.run([sys.executable, "-I", j("opt/opentv/bin/opentv-pi"), "playback", "status", "--env",
                            j("etc/opentv/opentv.env")], capture_output=True, timeout=30,
                           env=dict(os.environ, OPENTV_PLAYBACK_MARKER=j(self.PROMO),
                                    OPENTV_SOURCE_FILE=j("etc/opentv/source.env")))
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn(b'"promotion": "missing"', q.stdout)

    def test_reinstall_never_writes_promotion_and_uninstall_always_removes_it(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        promo, src = os.path.join(r, self.PROMO), os.path.join(r, "etc/opentv/source.env")
        for path, text in ((promo, '{"promoted": true}\n'), (src, "OPENTV_SOURCE_APPROVED=1\n")):
            with open(path, "w") as f:
                f.write(text)
        with open(promo, "rb") as f:
            before = f.read()
        p = sh([INSTALL, "--root", r, "--skip-preflight"])          # mise à jour : témoin jamais réécrit ni créé
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(promo, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertIn("témoin de promotion présent", p.stdout.decode())
        p = sh([UNINSTALL, "--root", r, "--dry-run"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(promo))                     # à blanc : rien ne bouge
        p = sh([UNINSTALL, "--root", r])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out + p.stderr.decode())
        self.assertFalse(os.path.lexists(promo))                   # même sans --purge
        self.assertTrue(os.path.exists(src))                       # configuration locale conservée
        self.assertIn("opentv-player-keys.service", [l for l in out.splitlines() if "disable --now" in l][0])
        with open(UNINSTALL) as f:
            self.assertIn("userdel opentv-keys", f.read())
        for u in ("opentv-player.service", "opentv-player-keys.service"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)))
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))

    def test_uninstall_removes_a_promotion_symlink_without_following_it(self):
        r = self.root()
        self.assertEqual(sh([INSTALL, "--root", r, "--skip-preflight"]).returncode, 0)
        victim = os.path.join(r, "victim")
        with open(victim, "w") as f:
            f.write("x")
        os.symlink(victim, os.path.join(r, self.PROMO))
        self.assertEqual(sh([UNINSTALL, "--root", r]).returncode, 0)
        self.assertFalse(os.path.lexists(os.path.join(r, self.PROMO)))
        self.assertTrue(os.path.exists(victim))

    def test_sysusers_and_units_split_input(self):
        with open(os.path.join(PI4, "system", "sysusers-opentv.conf")) as f:
            su = [l.split() for l in f if l.strip() and not l.startswith("#")]
        self.assertNotIn(["m", "opentv-kiosk", "input"], su)
        self.assertIn("opentv-keys", [w[1] for w in su if w[0] == "u"])
        self.assertFalse(any(w[0] == "m" and w[1] == "opentv-keys" for w in su))
        k = unit(os.path.join(PI4, "systemd", "opentv-kiosk.service"))["Service"]
        self.assertEqual(k["SupplementaryGroups"], "input")
        pl = unit(os.path.join(PI4, "systemd", "opentv-player.service"))["Service"]
        self.assertEqual(pl["SupplementaryGroups"].split(), ["video", "render"])


class TestPrepare(Base):
    def test_plan_only_by_default(self):
        r = fixture_root(self)
        before = tree(r)
        p = sh([PREPARE, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(tree(r), before)
        out = p.stdout.decode()
        self.assertIn("préflight matériel : OK", out)
        self.assertIn("apt-get install", out)
        self.assertIn("cage", out)
        self.assertIn("video=HDMI-A-1:1920x1080@60", out)                 # 1080p annoncé dans le plan d'install

    def test_yes_on_test_root_installs_with_1080p_once_and_backup(self):
        r = fixture_root(self)
        cmd = os.path.join(r, "boot/firmware/cmdline.txt")
        with open(cmd, "w") as f:
            f.write("console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootwait\n")
        for _ in range(2):
            p = sh([PREPARE, "--root", r, "--yes"])
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        with open(cmd) as f:
            text = f.read()
        self.assertEqual(text.count("video=HDMI-A-1:1920x1080@60"), 1)
        self.assertEqual(len(text.strip().splitlines()), 1)
        bks = [x for x in tree(r) if x.endswith("boot/firmware/cmdline.txt") and "opentv-install/backup-" in x]
        self.assertEqual(len(bks), 1, bks)
        self.assertIn("ignoré (racine de test)", p.stdout.decode())          # aucun apt sur un rootfs de test
        self.assertTrue(health.verify_manifest(r)["ok"])

    def test_refuses_pi5_and_32bit(self):
        r = fixture_root(self, model="Raspberry Pi 5 Model B Rev 1.0")
        p = sh([PREPARE, "--root", r])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Pi 4", p.stderr.decode())


class TestUnitsAndKiosk(unittest.TestCase):
    def parse(self, name):
        cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
        cp.optionxform = str
        with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
            cp.read_file(f)
        return cp

    def test_unit_syntax_lint(self):
        allowed = {"Unit", "Service", "Install", "Path", "Timer"}
        for u in UNITS:
            cp = self.parse(u)
            self.assertTrue(set(cp.sections()) <= allowed, u)
            self.assertIn("Description", cp["Unit"])
            if u.endswith(".service"):
                ex = cp["Service"]["ExecStart"]
                self.assertTrue(ex.startswith("/usr/bin/"), u)
                self.assertNotIn("sh -c", ex)
                if "kiosk" not in u:   # le kiosque garde le sandbox setuid de Chromium : pas de NoNewPrivileges
                    self.assertEqual(cp["Service"]["NoNewPrivileges"], "yes", u)
                    self.assertEqual(cp["Service"]["ProtectSystem"], "strict", u)
        svc = self.parse("opentv.service")["Service"]
        self.assertEqual((svc["User"], svc["Type"], svc["CapabilityBoundingSet"]), ("opentv", "notify", ""))
        self.assertIn("WatchdogSec", svc)
        self.assertEqual(self.parse("opentv-kiosk.service")["Service"]["User"], "opentv-kiosk")
        self.assertEqual(self.parse("opentv-net-helper.path")["Path"]["DirectoryNotEmpty"], "/var/lib/opentv/netjobs")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze absent (macOS)")
    def test_systemd_analyze_verify(self):
        p = subprocess.run(["systemd-analyze", "verify", "--man=no"] +
                           [os.path.join(PI4, "systemd", u) for u in UNITS], capture_output=True, timeout=60)
        bad = [l for l in p.stderr.decode().splitlines() if "opentv" in l and "not found" not in l.lower()
               and "No such file" not in l]
        self.assertEqual(bad, [])

    def test_kiosk_has_no_rockchip_or_unsafe_flags(self):
        with open(os.path.join(PI4, "bin", "opentv-kiosk-browser")) as f:
            text = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#"))
        for bad in ("rockchip", "rkmpp", "mpp_service", "video-dec0", "mali", "--no-sandbox", "--disable-gpu",
                    "pixman", "--disable-web-security", "remote-debugging"):
            self.assertNotIn(bad, text.lower(), bad)
        self.assertIn("--ozone-platform=wayland", text)
        self.assertIn("--kiosk", text)
        self.assertIn("http://127.0.0.1:${PORT}/tv", text)


# --------------------------------------------------------------------------- démarrage produit silencieux
BOOT_ARGS = ["quiet", "loglevel=3", "logo.nologo", "vt.global_cursor_default=0", "systemd.show_status=false"]
VIDEO = "video=HDMI-A-1:1920x1080@60"
PI_CMDLINE = "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 fsck.repair=yes rootwait " \
             "cfg80211.ieee80211_regdom=CA\n"
GETTY_DROPIN = "etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf"
BOOT_REC = "var/lib/opentv-install/cmdline-boot.added"
VIDEO_REC = "var/lib/opentv-install/cmdline-video.added"


def snapshot(root):
    out = {}
    for rel in tree(root):
        if rel.startswith("var/lib/opentv-install/"):
            continue
        with open(os.path.join(root, rel), "rb") as f:
            out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def unit(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
    cp.optionxform = str
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    return cp


class TestQuietBoot(Base):
    def rootfs(self, cmdline=PI_CMDLINE, binary=False):
        r = fixture_root(self)
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "wb") as f:
            f.write(cmdline if binary else cmdline.encode())
        return r

    def cmd(self, r):
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "rb") as f:
            return f.read()

    def install(self, r, ok=True, *extra):
        p = sh([INSTALL, "--root", r, "--skip-preflight"] + list(extra))
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p

    def uninstall(self, r, *extra):
        p = sh([UNINSTALL, "--root", r] + list(extra))
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p

    def ro(self, path):
        os.chmod(path, 0o555)
        self.addCleanup(os.chmod, path, 0o755)

    def test_fresh_install_adds_each_arg_once_idempotent_and_uninstall_is_byte_exact(self):
        r = self.rootfs()
        p = self.install(r)
        text = self.cmd(r).decode()
        self.assertEqual(text.count("\n"), 1)                              # monoligne conservée
        words = text.split()
        for a in BOOT_ARGS + [VIDEO]:
            self.assertEqual(words.count(a), 1, a)
        self.assertTrue(text.startswith(PI_CMDLINE.rstrip("\n") + " "))   # ajout en fin de ligne, rien de réordonné
        self.assertIn("console=tty1", words)                               # secours/emergency toujours visibles
        for bad in ("splash", "plymouth", "udev.log_level", "systemd.log_level", "console=tty3", "rd."):
            self.assertFalse(any(w.startswith(bad) for w in words if w not in PI_CMDLINE.split()), bad)
        with open(os.path.join(r, BOOT_REC)) as f:
            self.assertEqual(f.read().split(), BOOT_ARGS)
        self.assertIn("redémarrage", p.stdout.decode())
        first = self.cmd(r)
        self.install(r)                                                    # réinstallation : octet pour octet
        self.assertEqual(self.cmd(r), first)
        with open(os.path.join(r, BOOT_REC)) as f:
            self.assertEqual(f.read().split(), BOOT_ARGS)
        self.uninstall(r)
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())
        self.assertFalse(os.path.exists(os.path.join(r, BOOT_REC)))
        self.assertFalse(os.path.exists(os.path.join(r, VIDEO_REC)))

    def test_existing_keys_are_respected_never_recorded_and_survive_uninstall(self):
        orig = "console=tty1 root=PARTUUID=abcd-02 rootwait quiet loglevel=7 systemd.show_status quietx\n"
        r = self.rootfs(orig)
        out = self.install(r).stdout.decode()
        self.assertIn("loglevel déjà réglé", out)
        words = self.cmd(r).decode().split()
        self.assertEqual(words.count("quiet"), 1)
        self.assertNotIn("loglevel=3", words)                              # loglevel=7 local respecté
        self.assertNotIn("systemd.show_status=false", words)               # forme booléenne nue respectée
        self.assertIn("logo.nologo", words)
        with open(os.path.join(r, BOOT_REC)) as f:
            self.assertEqual(f.read().split(), ["logo.nologo", "vt.global_cursor_default=0"])
        self.uninstall(r)
        self.assertEqual(self.cmd(r), orig.encode())                       # quiet/quietx d'origine conservés

    def test_line_endings_trailing_space_and_extra_lines_preserved(self):
        for orig in (b"console=tty1 root=PARTUUID=abcd-02 rootwait",                 # sans fin de ligne
                     b"console=tty1 root=PARTUUID=abcd-02 rootwait \n",              # espace final
                     b"console=tty1 root=PARTUUID=abcd-02 rootwait\r\n# note\nx\n",  # CRLF + lignes suivantes
                     b"console=tty1  root=PARTUUID=abcd-02   rootwait\n"):           # espaces multiples
            r = self.rootfs(orig, binary=True)
            self.install(r)
            got = self.cmd(r)
            line = got.split(b"\n")[0]
            self.assertEqual(line.endswith(b"\r"), orig.split(b"\n")[0].endswith(b"\r"), orig)
            self.assertEqual(got.count(b"\n"), orig.count(b"\n"), orig)
            self.assertEqual(got[len(line):], orig[len(orig.split(b"\n")[0]):], orig)   # suite intacte
            for a in BOOT_ARGS:
                self.assertEqual(line.split().count(a.encode()), 1, (orig, a))
            self.uninstall(r)
            self.assertEqual(self.cmd(r), orig)

    def test_local_edits_after_install_are_kept_by_uninstall(self):
        r = self.rootfs()
        self.install(r)
        edited = self.cmd(r).decode().rstrip("\n") + " usbcore.autosuspend=-1\n"
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "w") as f:
            f.write(edited)
        with open(os.path.join(r, BOOT_REC), "a") as f:
            f.write("init=/bin/sh\nconsole=tty1\n")                        # enregistrement altéré : hors liste fermée
        out = self.uninstall(r).stdout.decode()
        self.assertIn("hors liste OpenTV", out)
        self.assertEqual(self.cmd(r).decode(), PI_CMDLINE.rstrip("\n") + " usbcore.autosuspend=-1\n")

    def test_upgrade_from_video_only_install_then_uninstall(self):
        r = self.rootfs(PI_CMDLINE.rstrip("\n") + " " + VIDEO + "\n")      # version précédente : 1080p seul
        os.makedirs(os.path.join(r, "var/lib/opentv-install"))
        with open(os.path.join(r, VIDEO_REC), "w") as f:
            f.write(VIDEO + "\n")
        self.install(r)
        words = self.cmd(r).decode().split()
        self.assertEqual(words.count(VIDEO), 1)
        for a in BOOT_ARGS:
            self.assertEqual(words.count(a), 1)
        with open(os.path.join(r, VIDEO_REC)) as f:
            self.assertEqual(f.read(), VIDEO + "\n")
        self.uninstall(r)
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())

    def test_injected_late_failure_rolls_back_cmdline_and_records(self):
        for old_rec in (None, "quiet\n"):
            r = self.rootfs("console=tty1 root=PARTUUID=abcd-02 rootwait quiet\n")
            self.install(r)
            with open(os.path.join(r, "boot/firmware/cmdline.txt"), "w") as f:
                f.write("console=tty1 root=PARTUUID=abcd-02 rootwait quiet\n")   # jetons à rajouter
            for rec in (BOOT_REC, VIDEO_REC):
                os.remove(os.path.join(r, rec))
            if old_rec:
                with open(os.path.join(r, BOOT_REC), "w") as f:
                    f.write(old_rec)
            os.remove(os.path.join(r, "etc/opentv/opentv.env"))           # l'étape env (après cmdline) devra écrire
            before = snapshot(r)
            self.ro(os.path.join(r, "etc/opentv"))
            p = self.install(r, ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("retour à l'état précédent", p.stderr.decode())
            os.chmod(os.path.join(r, "etc/opentv"), 0o755)
            self.assertEqual(snapshot(r), before)
            self.assertEqual(self.cmd(r), b"console=tty1 root=PARTUUID=abcd-02 rootwait quiet\n")
            self.assertFalse(os.path.exists(os.path.join(r, VIDEO_REC)))
            if old_rec:
                with open(os.path.join(r, BOOT_REC)) as f:
                    self.assertEqual(f.read(), old_rec)
            else:
                self.assertFalse(os.path.exists(os.path.join(r, BOOT_REC)))

    def test_unexpected_cmdline_is_refused_with_full_rollback(self):
        r = self.rootfs("ceci n'est pas une ligne de commande\n")
        before = snapshot(r)
        p = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("sans root=", p.stderr.decode())
        self.assertEqual(snapshot(r), before)
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))
        r = self.rootfs("console=tty1 root=PARTUUID=abcd-02 rootwait " + "x" * 1100 + "\n")
        before = snapshot(r)
        p = self.install(r, ok=False)
        self.assertIn("trop longue", p.stderr.decode())
        self.assertEqual(snapshot(r), before)

    def test_dry_run_plans_boot_args_and_writes_nothing(self):
        r = self.rootfs()
        before = snapshot(r)
        p = sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[dry-run] ajouter " + VIDEO + " " + " ".join(BOOT_ARGS), p.stdout.decode())
        self.assertIn("opentv-splash.service", p.stdout.decode())
        self.assertEqual(snapshot(r), before)
        self.install(r)
        before = snapshot(r)
        p = sh([UNINSTALL, "--dry-run", "--root", r])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[dry-run] retirer", p.stdout.decode())
        self.assertEqual(snapshot(r), before)

    def test_splash_getty_dropin_manifest_enable_and_exact_uninstall(self):
        r = self.rootfs()
        j = lambda *a: os.path.join(r, *a)   # noqa: E731
        out = self.install(r).stdout.decode()
        for rel, mode in (("opt/opentv/bin/opentv-splash", 0o755), ("opt/opentv/bin/opentv-boot-check", 0o755),
                          ("etc/systemd/system/opentv-splash.service", 0o644), (GETTY_DROPIN, 0o644)):
            self.assertEqual(stat.S_IMODE(os.stat(j(rel)).st_mode), mode, rel)
        self.assertEqual(sha(j("opt/opentv/bin/opentv-splash")), sha(SPLASH))
        with open(j("opt/opentv/MANIFEST.sha256")) as f:
            man = f.read()
        for p in ("/opt/opentv/bin/opentv-splash", "/opt/opentv/bin/opentv-boot-check",
                  "/etc/systemd/system/opentv-splash.service", "/" + GETTY_DROPIN):
            self.assertIn("  " + p + "\n", man)
        self.assertTrue(health.verify_manifest(r)["ok"])
        en = [l for l in out.splitlines() if "systemctl enable" in l][0]
        self.assertIn("opentv-splash.service", en)
        rs = [l for l in out.splitlines() if "systemctl restart" in l][0]
        self.assertNotIn("opentv-splash", rs)                              # jamais rejoué par-dessus Cage
        with open(j("var/lib/opentv-install/created-dirs.list")) as f:
            self.assertIn("/etc/systemd/system/getty@tty1.service.d\n", f.read())
        # intégrité : une altération du splash est détectée par le manifeste
        with open(j("opt/opentv/bin/opentv-splash"), "a") as f:
            f.write("# altéré\n")
        self.assertEqual(health.verify_manifest(r)["mismatch"], ["/opt/opentv/bin/opentv-splash"])
        self.install(r)
        self.assertTrue(health.verify_manifest(r)["ok"])
        out = self.uninstall(r).stdout.decode()
        self.assertIn("opentv-splash.service", [l for l in out.splitlines() if "disable --now" in l][0])
        self.assertLess(out.index("daemon-reload"), out.index("systemctl start getty@tty1.service"))
        for rel in ("opt/opentv", "etc/systemd/system/opentv-splash.service", GETTY_DROPIN,
                    "etc/systemd/system/getty@tty1.service.d"):
            self.assertFalse(os.path.exists(j(rel)), rel)

    def test_preexisting_getty_dropin_dir_and_foreign_conf_are_kept(self):
        r = self.rootfs()
        d = os.path.join(r, "etc/systemd/system/getty@tty1.service.d")
        os.makedirs(d)
        with open(os.path.join(d, "autologin.conf"), "w") as f:
            f.write("[Service]\nExecStart=\n")
        with open(os.path.join(d, "opentv-kiosk.conf"), "w") as f:
            f.write("# fichier local au même nom\n")
        self.install(r)
        self.uninstall(r)
        self.assertEqual(sorted(os.listdir(d)), ["autologin.conf", "opentv-kiosk.conf"])
        with open(os.path.join(d, "opentv-kiosk.conf")) as f:
            self.assertEqual(f.read(), "# fichier local au même nom\n")   # original restauré
        with open(os.path.join(d, "autologin.conf")) as f:
            self.assertEqual(f.read(), "[Service]\nExecStart=\n")

    # ------------------------------------------------ P1 : écritures cmdline.txt résistantes à une coupure
    FAKE_SYNC = r'''#!/bin/sh
# faux sync : journalise l'argv, le contenu de cmdline.txt à cet instant et chaque chemin reçu
hexof() { od -An -tx1 -v "$1" | tr -d ' \n'; }
{
  printf 'CALL %s' "$#"; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'
  if [ -f "$OTV_CMDF" ]; then printf 'CMD %s\n' "$(hexof "$OTV_CMDF")"; else printf 'CMD -\n'; fi
  if [ -f "$OTV_REC" ]; then printf 'REC 1\n'; else printf 'REC 0\n'; fi
  for a in "$@"; do
    case "$a" in -*) continue ;; esac
    if [ -f "$a" ]; then printf 'FILE %s %s\n' "$a" "$(hexof "$a")"
    elif [ -d "$a" ]; then printf 'DIR %s\n' "$a"
    else printf 'MISSING %s\n' "$a"; fi
  done
  printf 'END\n'
} >> "$OTV_SYNC_LOG"
case "$OTV_SYNC_MODE" in
  noargs) [ "$#" -eq 0 ] || exit 1 ;;
  fail) exit 1 ;;
esac
exit 0
'''

    def run_synced(self, r, args, mode="ok", bash="bash"):
        d = self.root()
        with open(os.path.join(d, "sync"), "w") as f:
            f.write(self.FAKE_SYNC)
        os.chmod(os.path.join(d, "sync"), 0o755)
        log = os.path.join(d, "sync.log")
        env = dict(os.environ, LC_ALL="C", PATH=d + os.pathsep + os.environ["PATH"], OTV_SYNC_LOG=log,
                   OTV_SYNC_MODE=mode, OTV_CMDF=os.path.join(r, "boot/firmware/cmdline.txt"),
                   OTV_REC=os.path.join(r, BOOT_REC))
        env.pop("OPENTV_ROOT", None)
        p = subprocess.run([bash] + list(args), capture_output=True, timeout=120, env=env)
        events = []
        if os.path.exists(log):
            with open(log) as f:
                for line in f:
                    k, _, v = line.rstrip("\n").partition(" ")
                    if k == "CALL":
                        n, _, rest = v.partition(" ")
                        events.append({"n": int(n), "args": rest.split(), "files": {}, "dirs": []})
                    elif k == "CMD":
                        events[-1]["cmd"] = None if v == "-" else v
                    elif k == "REC":
                        events[-1]["rec"] = v == "1"
                    elif k == "FILE":
                        path, _, hx = v.rpartition(" ")
                        events[-1]["files"][path] = hx
                    elif k == "DIR":
                        events[-1]["dirs"].append(v)
        return p, events

    def assert_durable_replace(self, events, cmdf, old, new, label):
        tmp = cmdf + ".opentv-tmp"
        for e in events:                                                   # coupure à n'importe quel instant :
            self.assertIn(e["cmd"], (old.hex(), new.hex()), label)         # jamais vide ni tronqué
        pre = [i for i, e in enumerate(events) if e["files"].get(tmp) == new.hex() and e["cmd"] == old.hex()]
        self.assertTrue(pre, (label, "fichier temporaire complet synchronisé AVANT le renommage"))
        post = [i for i, e in enumerate(events) if i > pre[0] and e["files"].get(cmdf) == new.hex()
                and os.path.dirname(cmdf) in e["dirs"]]
        self.assertTrue(post, (label, "résultat et dossier synchronisés APRÈS le renommage"))
        return pre[0], post[0]

    def test_cmdline_install_and_uninstall_sync_temp_before_and_result_after_rename(self):
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            r = self.rootfs()
            cmdf = os.path.join(r, "boot/firmware/cmdline.txt")
            p, ev = self.run_synced(r, [INSTALL, "--root", r, "--skip-preflight"], bash=bash)
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
            new = self.cmd(r)
            _, post = self.assert_durable_replace(ev, cmdf, PI_CMDLINE.encode(), new, (bash, "install"))
            self.assertTrue(ev[post]["rec"])                               # jetons notés (durablement) avant
            self.assertFalse(os.path.exists(cmdf + ".opentv-tmp"))
            p, ev = self.run_synced(r, [UNINSTALL, "--root", r], bash=bash)
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
            self.assertEqual(self.cmd(r), PI_CMDLINE.encode())
            _, post = self.assert_durable_replace(ev, cmdf, new, PI_CMDLINE.encode(), (bash, "uninstall"))
            self.assertTrue(ev[post]["rec"])                               # marqueurs effacés seulement après
            self.assertFalse(os.path.exists(os.path.join(r, BOOT_REC)))

    def test_cmdline_sync_falls_back_to_global_sync_without_file_arguments(self):
        r = self.rootfs()
        p, ev = self.run_synced(r, [INSTALL, "--root", r, "--skip-preflight"], mode="noargs")
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        new = self.cmd(r)
        tmp = os.path.join(r, "boot/firmware/cmdline.txt.opentv-tmp")
        i = [k for k, e in enumerate(ev) if tmp in e["files"] and e["files"][tmp] == new.hex()][0]
        self.assertEqual((ev[i + 1]["n"], ev[i + 1]["cmd"]), (0, PI_CMDLINE.encode().hex()))   # sync global avant
        self.assertTrue(any(e["n"] == 0 and e["cmd"] == new.hex() for e in ev[i + 2:]))       # et après

    def test_cmdline_sync_failure_aborts_before_rename_with_rollback(self):
        r = self.rootfs()
        before = snapshot(r)
        p, _ = self.run_synced(r, [INSTALL, "--root", r, "--skip-preflight"], mode="fail")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        self.assertEqual(snapshot(r), before)
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())
        for rec in (BOOT_REC, VIDEO_REC):
            self.assertFalse(os.path.exists(os.path.join(r, rec)), rec)   # aucun marqueur périmé

    def test_rollback_restores_cmdline_through_synced_temp_file(self):
        r = self.rootfs()
        os.makedirs(os.path.join(r, "etc/opentv"))
        self.ro(os.path.join(r, "etc/opentv"))                             # échec tardif (après cmdline.txt)
        p, ev = self.run_synced(r, [INSTALL, "--root", r, "--skip-preflight"])
        self.assertNotEqual(p.returncode, 0)
        cmdf = os.path.join(r, "boot/firmware/cmdline.txt")
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())
        changed = [i for i, e in enumerate(ev) if e["cmd"] != PI_CMDLINE.encode().hex()]
        self.assertTrue(changed)                                           # cmdline modifiée puis restaurée
        newhex = ev[changed[0]]["cmd"]
        self.assert_durable_replace(ev[changed[0]:], cmdf, bytes.fromhex(newhex), PI_CMDLINE.encode(), "rollback")
        for rec in (BOOT_REC, VIDEO_REC):
            self.assertFalse(os.path.exists(os.path.join(r, rec)), rec)

    def test_unwritable_boot_partition_fails_without_stale_markers(self):
        r = self.rootfs()
        before = snapshot(r)
        self.ro(os.path.join(r, "boot/firmware"))
        p = self.install(r, False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        os.chmod(os.path.join(r, "boot/firmware"), 0o755)
        self.assertEqual(snapshot(r), before)
        for rec in (BOOT_REC, VIDEO_REC):
            self.assertFalse(os.path.exists(os.path.join(r, rec)), rec)

    # ------------------------------------------------ P2 : verbosité locale (loglevel=… / debug) respectée
    def test_local_verbosity_loglevel_or_debug_never_gets_quiet(self):
        for extra in ("loglevel=7", "debug", "loglevel=3", "debug loglevel=8"):
            orig = "console=tty1 root=PARTUUID=abcd-02 rootwait %s\n" % extra
            r = self.rootfs(orig)
            out = self.install(r).stdout.decode()
            self.assertIn("verbosité locale", out, extra)
            words = self.cmd(r).decode().split()
            self.assertNotIn("quiet", words, extra)                        # quiet écraserait loglevel/debug
            self.assertEqual([w for w in words if w.startswith("loglevel=")],
                             [w for w in extra.split() if w.startswith("loglevel=")], extra)
            for a in ("logo.nologo", "vt.global_cursor_default=0", "systemd.show_status=false", VIDEO):
                self.assertEqual(words.count(a), 1, (extra, a))
            with open(os.path.join(r, BOOT_REC)) as f:
                self.assertEqual(f.read().split(), ["logo.nologo", "vt.global_cursor_default=0",
                                                    "systemd.show_status=false"], extra)
            self.uninstall(r)
            self.assertEqual(self.cmd(r), orig.encode(), extra)

    # ------------------------------------------------ P2 : désinstallation sans perte des marqueurs
    def markers(self, r):
        out = {}
        for rec in (BOOT_REC, VIDEO_REC):
            with open(os.path.join(r, rec), "rb") as f:
                out[rec] = f.read()
        return out

    def test_uninstall_keeps_markers_and_fails_when_cmdline_unavailable(self):
        r = self.rootfs()
        self.install(r)
        cmdf = os.path.join(r, "boot/firmware/cmdline.txt")
        installed = self.cmd(r)
        recs = self.markers(r)
        os.remove(cmdf)                                                    # partition non montée / fichier absent
        before = snapshot(r)
        for extra in ([], ["--dry-run"], ["--purge"]):
            p = sh([UNINSTALL, "--root", r] + extra)
            self.assertNotEqual(p.returncode, 0, extra)
            err = p.stderr.decode()
            self.assertIn("cmdline.txt", err)
            self.assertIn("relancer", err)
            self.assertEqual(self.markers(r), recs, extra)                 # conservés pour réessayer
            self.assertEqual(snapshot(r), before, extra)                   # rien retiré : uninstall.sh reste là
        with open(cmdf, "w") as f:
            f.write("pas une ligne de commande\n")                         # contenu inattendu : non traité
        p = sh([UNINSTALL, "--root", r])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("root=", p.stderr.decode())
        self.assertEqual(self.markers(r), recs)
        self.assertEqual(self.cmd(r), b"pas une ligne de commande\n")
        with open(cmdf, "wb") as f:
            f.write(installed)
        self.ro(os.path.join(r, "boot/firmware"))                          # écriture impossible
        p = sh([UNINSTALL, "--root", r])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.markers(r), recs)
        self.assertEqual(self.cmd(r), installed)
        self.assertTrue(os.path.exists(os.path.join(r, "opt/opentv/uninstall.sh")))
        os.chmod(os.path.join(r, "boot/firmware"), 0o755)
        self.uninstall(r)                                                  # nouvel essai : retour exact
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())
        for rec in (BOOT_REC, VIDEO_REC):
            self.assertFalse(os.path.exists(os.path.join(r, rec)), rec)
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))

    def test_uninstall_without_markers_or_with_tokens_already_gone_still_succeeds(self):
        r = self.rootfs()
        self.install(r)
        with open(os.path.join(r, "boot/firmware/cmdline.txt"), "w") as f:
            f.write(PI_CMDLINE)                                            # jetons retirés à la main
        out = self.uninstall(r).stdout.decode()
        self.assertIn("déjà absents", out)
        self.assertFalse(os.path.exists(os.path.join(r, BOOT_REC)))
        self.assertEqual(self.cmd(r), PI_CMDLINE.encode())

    def test_uninstall_tty1_reset_is_bounded_and_restores_console(self):
        with open(UNINSTALL) as f:
            text = f.read()
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        tty = [l for l in code if "/dev/tty1" in l and ">" in l or "tee" in l and "/dev/tty1" in l]
        self.assertTrue(tty)
        for l in tty:
            self.assertIn("timeout", l)                                    # tty1 bloquée ⇒ jamais d'attente infinie
            self.assertNotIn("> /dev/tty1", l)                             # ouverture faite sous timeout (tee)
        for tool in ("setupcon", "setfont"):
            lines = [l for l in code if tool in l and "command -v" not in l]
            self.assertTrue(lines, tool)
            for l in lines:
                self.assertIn("timeout", l, tool)
        self.assertIn("\\033]R", text)                                     # palette par défaut
        self.assertIn("\\033[?25h", text)                                  # curseur visible
        r = self.rootfs()
        self.install(r)
        out = self.uninstall(r).stdout.decode()
        self.assertIn("[plan] tty1 : palette par défaut, curseur visible, police console", out)

    def test_live_boot_partition_must_be_mounted(self):
        for s in (INSTALL, UNINSTALL):
            with open(s) as f:
                self.assertIn("mountpoint -q", f.read(), s)

    def test_boot_arg_lists_match_between_install_uninstall_and_check(self):
        with open(INSTALL) as f:
            m = re.search(r"^BOOT_ARGS=\(([^)]*)\)$", f.read(), re.M)
        self.assertEqual(m.group(1).split(), BOOT_ARGS)
        with open(UNINSTALL) as f:
            m = re.search(r"^\s*(video=HDMI-A-1:1920x1080@60\|[^)]*)\)$", f.read(), re.M)
        self.assertEqual(m.group(1).split("|"), [VIDEO] + BOOT_ARGS)
        with open(BOOT_CHECK) as f:
            m = re.search(r"^for t in ([^;]*); do$", f.read(), re.M)
        self.assertEqual(m.group(1).split(), BOOT_ARGS + [VIDEO])


class TestSplashAndBootUnits(Base):
    def test_splash_unit_is_early_bounded_hardened_and_never_blocks_boot(self):
        cp = unit(os.path.join(PI4, "systemd", "opentv-splash.service"))
        u, s = cp["Unit"], cp["Service"]
        self.assertEqual(u["DefaultDependencies"], "no")
        self.assertEqual(s["Type"], "oneshot")
        self.assertLessEqual(int(s["TimeoutStartSec"]), 10)
        before = u["Before"].split()
        self.assertIn("opentv-kiosk.service", before)
        for t in ("sysinit.target", "basic.target", "multi-user.target", "local-fs.target", "ssh.service"):
            self.assertNotIn(t, before)                                   # aucune cible critique n'attend le splash
        for k in ("Requires", "Requisite", "BindsTo", "PartOf", "OnFailure", "FailureAction", "SuccessAction"):
            self.assertNotIn(k, u)
        self.assertEqual(cp["Install"]["WantedBy"], "sysinit.target")
        self.assertEqual((s["StandardInput"], s["StandardOutput"], s["TTYPath"]), ("null", "tty", "/dev/tty1"))
        self.assertEqual(s["TTYVTDisallocate"], "no")
        self.assertEqual(s["ExecStart"], "/usr/bin/bash /opt/opentv/bin/opentv-splash")
        self.assertEqual(s["CapabilityBoundingSet"], "CAP_SYS_TTY_CONFIG")
        self.assertEqual((s["DevicePolicy"], s["DeviceAllow"]), ("closed", "/dev/tty1 rw"))
        for k in ("NoNewPrivileges", "PrivateNetwork", "ProtectHome", "ProtectKernelTunables", "RestrictNamespaces",
                  "MemoryDenyWriteExecute"):
            self.assertEqual(s[k], "yes", k)
        self.assertNotIn("RemainAfterExit", s)
        self.assertNotIn("Restart", s)

    def test_getty_dropin_only_conditions_tty1_on_enabled_kiosk(self):
        cp = unit(os.path.join(PI4, "system", "getty-tty1-opentv-kiosk.conf"))
        self.assertEqual(cp.sections(), ["Unit"])
        self.assertEqual(dict(cp["Unit"]), {
            "ConditionPathExists": "!/etc/systemd/system/multi-user.target.wants/opentv-kiosk.service"})
        k = unit(os.path.join(PI4, "systemd", "opentv-kiosk.service"))
        self.assertEqual(k["Install"]["WantedBy"], "multi-user.target")   # chemin du lien d'activation cohérent
        self.assertEqual(k["Service"]["TTYPath"], "/dev/tty1")
        self.assertEqual(k["Service"]["TTYVTDisallocate"], "no")          # splash conservé jusqu'à Cage
        self.assertIn("getty@tty1.service", k["Unit"]["Conflicts"])

    def run_splash(self, bash, **kw):
        t0 = time.monotonic()
        p = subprocess.run([bash, SPLASH], stdin=subprocess.PIPE, capture_output=True, timeout=10, **kw)
        return p, time.monotonic() - t0

    def test_splash_script_output_bash32_and_current(self):
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            p, dt = self.run_splash(bash)                                  # stdin jamais fermé : ne lit rien
            self.assertEqual(p.returncode, 0, (bash, p.stderr))
            self.assertLess(dt, 3)
            out = p.stdout.decode("utf-8")
            for seq in ("\x1b[?25l", "\x1b[2J", "\x1b]P00b0f17", "\x1b[9;0]"):
                self.assertIn(seq, out)
            self.assertIn("\x1b[12;38H\x1b[1;37;40mOpenTV", out)            # centré (24x80 hors console)
            self.assertIn("\x1b[14;35H\x1b[0;37;40mDémarrage...", out)
            self.assertEqual(p.stderr, b"")
            self.assertNotRegex(out, r"(?i)login|\$ |# ")

    def test_splash_never_fails_even_without_output(self):
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            p = subprocess.run([bash, "-c", 'exec 1>&-; exec "$0" "$1"', bash, SPLASH], capture_output=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            if os.path.exists("/dev/full"):
                with open("/dev/full", "wb") as full:
                    p = subprocess.run([bash, SPLASH], stdout=full, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(p.returncode, 0)

    def fake_bin(self, props, kernel_lines=80):
        d = self.root()
        table = os.path.join(d, "props")
        with open(table, "w") as f:
            for (u, k), v in props.items():
                f.write("%s %s=%s\n" % (u, k, v))
        scripts = {
            "systemctl": '#!/bin/sh\n[ "$1" = show ] || exit 0\n'
                         'grep -F "$5 $3=" "%s" | head -n 1 | cut -d= -f2-\n' % table,
            "journalctl": '#!/bin/sh\ncase " $* " in *" -k "*) n=%d ;; *" --list-boots "*) n=3 ;; *) n=5 ;; esac\n'
                          'i=0; while [ $i -lt $n ]; do echo "ligne $i"; i=$((i+1)); done\n' % kernel_lines,
            "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        }
        for n, body in scripts.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(body)
            os.chmod(os.path.join(d, n), 0o755)
        return d

    GOOD = {("opentv-splash.service", "UnitFileState"): "enabled", ("opentv-splash.service", "Result"): "success",
            ("opentv-splash.service", "ExecMainStatus"): "0",
            ("opentv-splash.service", "ExecMainStartTimestampMonotonic"): "4200000",
            ("opentv-kiosk.service", "ExecMainStartTimestampMonotonic"): "9100000",
            ("getty@tty1.service", "ActiveState"): "inactive", ("getty@tty1.service", "ConditionResult"): "no",
            ("getty@tty1.service", "DropInPaths"): "/usr/lib/systemd/system/getty@.service.d/x.conf "
                                                    "/etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf",
            ("opentv-kiosk.service", "ActiveState"): "active", ("opentv-kiosk.service", "NRestarts"): "0",
            ("ssh.service", "ActiveState"): "active"}
    GOOD_LINE = PI_CMDLINE.rstrip("\n") + " " + VIDEO + " " + " ".join(BOOT_ARGS) + "\n"

    def run_check(self, props, cmdline, kernel_lines=80):
        d = self.fake_bin(props, kernel_lines)
        cf = os.path.join(d, "cmdline")
        with open(cf, "w") as f:
            f.write(cmdline)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], OPENTV_PROC_CMDLINE=cf, LC_ALL="C")
        return subprocess.run(["/bin/bash", BOOT_CHECK], capture_output=True, timeout=30, env=env)

    def test_boot_check_passes_on_expected_state(self):
        line = PI_CMDLINE.rstrip("\n") + " " + VIDEO + " " + " ".join(BOOT_ARGS) + "\n"
        p = self.run_check(self.GOOD, line)
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("ÉCHEC", out)
        self.assertIn("écran OpenTV avant le kiosque", out)
        self.assertIn("4200 ms", out)
        self.assertIn("critère visuel", out)

    def test_boot_check_reports_each_failure(self):
        bad = dict(self.GOOD)
        bad[("getty@tty1.service", "ActiveState")] = "active"
        bad[("opentv-splash.service", "Result")] = "timeout"
        bad[("ssh.service", "ActiveState")] = "inactive"
        bad[("opentv-splash.service", "ExecMainStartTimestampMonotonic")] = "9900000"
        p = self.run_check(bad, PI_CMDLINE, kernel_lines=3)
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 1, out)
        for s in ("quiet absent", "getty@tty1 actif", "Result=timeout", "SSH inactif", "non exécuté avant le kiosque",
                  "journal noyau court"):
            self.assertIn(s, out)

    def test_boot_check_reports_local_verbosity_honestly(self):
        for extra, shown in (("loglevel=7", "loglevel=7"), ("debug", "debug"), ("quiet loglevel=7", "loglevel=7")):
            words = [w for w in self.GOOD_LINE.split() if w not in ("quiet", "loglevel=3")] + extra.split()
            p = self.run_check(self.GOOD, " ".join(words) + "\n")
            out = p.stdout.decode()
            self.assertEqual(p.returncode, 1, (extra, out))               # écran non silencieux : pas de faux OK
            self.assertIn("non silencieux", out, extra)
            self.assertIn(shown, out, extra)
            self.assertIn("respecté", out, extra)
            self.assertNotIn("quiet absent", out, extra)                   # cause exacte, pas « oubli »
            self.assertNotIn("OK     noyau : loglevel=3", out, extra)

    def test_boot_check_accepts_local_loglevel3_without_quiet(self):
        # loglevel=3 préexistant sans quiet : install.sh n'ajoute pas quiet ; silencieux, donc pas de faux ÉCHEC
        words = [w for w in self.GOOD_LINE.split() if w != "quiet"]
        p = self.run_check(self.GOOD, " ".join(words) + "\n")
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("ÉCHEC", out)
        self.assertNotIn("quiet absent", out)
        self.assertIn("OK     noyau : loglevel=3", out)
        self.assertIn("info   noyau : quiet non ajouté par OpenTV, loglevel=3 local suffit", out)
        # mais debug ou loglevel≠3 restent un ÉCHEC « non silencieux », même avec loglevel=3 ailleurs
        for extra in ("debug", "loglevel=7", "loglevel=4"):
            base = words if extra == "debug" else [w for w in words if w != "loglevel=3"]
            p = self.run_check(self.GOOD, " ".join(base + [extra]) + "\n")
            out = p.stdout.decode()
            self.assertEqual(p.returncode, 1, (extra, out))
            self.assertIn("non silencieux", out, extra)
            self.assertNotIn("loglevel=3 local suffit", out, extra)
        # sans quiet ni loglevel : vrai oubli, toujours signalé
        bare = [w for w in self.GOOD_LINE.split() if w not in ("quiet", "loglevel=3")]
        out = self.run_check(self.GOOD, " ".join(bare) + "\n").stdout.decode()
        self.assertIn("ÉCHEC  noyau : quiet absent", out)

    def test_boot_check_requires_getty_dropin_proof(self):
        for key, val in ((("getty@tty1.service", "ConditionResult"), "yes"),
                         (("getty@tty1.service", "DropInPaths"), "/usr/lib/systemd/system/getty@.service.d/x.conf"),
                         (("getty@tty1.service", "ConditionResult"), "")):
            props = dict(self.GOOD)
            props[key] = val
            p = self.run_check(props, self.GOOD_LINE)
            out = p.stdout.decode()
            self.assertEqual(p.returncode, 1, (key, val, out))
            self.assertIn("ÉCHEC  getty@tty1", out)
        out = self.run_check(self.GOOD, self.GOOD_LINE).stdout.decode()
        self.assertIn("OK     getty@tty1 ignoré par la condition OpenTV", out)

    def test_boot_check_rejects_journal_thinning_options(self):
        for opt in ("systemd.log_level=err", "udev.log_level=3", "udev.log_priority=err", "rd.udev.log_level=0",
                    "systemd.log_target=console", "rd.systemd.log_level=warning"):
            p = self.run_check(self.GOOD, self.GOOD_LINE.rstrip("\n") + " " + opt + "\n")
            out = p.stdout.decode()
            self.assertEqual(p.returncode, 1, (opt, out))
            self.assertIn("journal aminci", out, opt)
            self.assertIn(opt, out)
        out = self.run_check(self.GOOD, self.GOOD_LINE).stdout.decode()
        self.assertIn("aucune option systemd/udev n'amincit le journal", out)

    def test_boot_check_accepts_socket_activated_ssh(self):
        props = dict(self.GOOD)
        props[("ssh.service", "ActiveState")] = "inactive"
        props[("ssh.socket", "ActiveState")] = "active"
        p = self.run_check(props, self.GOOD_LINE)
        self.assertEqual(p.returncode, 0, p.stdout.decode())
        self.assertIn("SSH actif", p.stdout.decode())


SECRET_RES = (re.compile(r"(?i)\b(psk|passphrase|password|token|secret)\b\s*[=:]\s*[\"'][^\"'\s]{12,}[\"']"),
              re.compile(r"(?im)^\s*(psk|passphrase|password|token|secret)=[^\s$]{8,}$"))


class TestNoSecretsAndProductionUntouched(unittest.TestCase):
    def test_no_secrets_in_deliverables(self):
        for d, _, files in os.walk(PI4):
            if "/tests" in d or "__pycache__" in d:
                continue
            for f in files:
                p = os.path.join(d, f)
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for rx in SECRET_RES:
                    for m in rx.finditer(text):
                        self.fail("secret possible dans %s : %s" % (p, m.group(0)[:30]))

    def test_env_example_has_no_active_secret(self):
        with open(os.path.join(PI4, "opentv-pi.env.example")) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    self.fail("valeur active dans l'exemple : " + line)

    @unittest.skipUnless(shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")), "git absent")
    def test_production_trees_untouched(self):
        p = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--", "native", "scripts/nanopi", "app",
                            "service.js", "package.json", "scripts/build.py", "scripts/deploy.sh"],
                           capture_output=True, timeout=30)
        self.assertEqual(p.stdout.decode().strip(), "")


if __name__ == "__main__":
    unittest.main()
