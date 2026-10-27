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
import unittest

from tests._common import PI4, ROOT, NANOPI
from tests.test_service import fixture_root
from opentv_pi import health

INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
PREPARE = os.path.join(PI4, "prepare-pi.sh")
SHELL_SCRIPTS = (INSTALL, UNINSTALL, PREPARE, os.path.join(PI4, "bin", "opentv-kiosk-browser"))
UNITS = ("opentv.service", "opentv-kiosk.service", "opentv-net-helper.service", "opentv-net-helper.path",
         "opentv-ap-guard.service", "opentv-ap-guard.timer", "opentv-cec.service", "opentv-cec.timer")


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
