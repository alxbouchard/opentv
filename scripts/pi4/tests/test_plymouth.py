"""Image OpenTV de bout en bout : arc-en-ciel du firmware retiré (config.txt), thème Plymouth « opentv », jetons
cmdline, initramfs régénéré/vérifié, repli texte, passage au kiosque sans texte, désinstallation exacte.

Plymouth et initramfs-tools ne tournent pas sur macOS : les racines de test contiennent leurs fichiers Debian
(plymouthd, module script, crochet initramfs…) et de faux outils `update-initramfs`/`lsinitramfs` placés dans le PATH
journalisent chaque appel (thème sélectionné et fichiers du thème présents À CET INSTANT) et « construisent » un
initramfs8 listant ce qu'un vrai initramfs contiendrait. Les critères visuels restent des portes terrain.
"""
import configparser
import hashlib
import importlib.util
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

from tests._common import PI4
from tests.test_service import fixture_root
from opentv_pi import health

INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
PREPARE = os.path.join(PI4, "prepare-pi.sh")
SPLASH = os.path.join(PI4, "system", "opentv-splash")
HANDOFF = os.path.join(PI4, "system", "opentv-plymouth-handoff")
IRLIB = os.path.join(PI4, "system", "opentv-initramfs.sh")
BOOT_CHECK = os.path.join(PI4, "system", "opentv-boot-check")
THEME_SRC = os.path.join(PI4, "system", "plymouth")
THEME_DIR = "usr/share/plymouth/themes/opentv"
THEME_FILES = ("opentv.plymouth", "opentv.script", "opentv.png", "opentv-mask.png")
CMD = "boot/firmware/cmdline.txt"
CFG = "boot/firmware/config.txt"
PLYC = "etc/plymouth/plymouthd.conf"
INITRD = "boot/firmware/initramfs8"
BOOT_REC = "var/lib/opentv-install/cmdline-boot.added"
CFG_REC = "var/lib/opentv-install/config-splash.added"
PLY_REC = "var/lib/opentv-install/plymouth-theme.added"
PLY_ORIG = "var/lib/opentv-install/plymouthd.conf.orig"
INITRD_REC = "var/lib/opentv-install/initramfs-plymouth.rebuilt"
TXN = "var/lib/opentv-install/initramfs.txn"
PAYLOAD = INITRD + ".payload"           # faux initramfs : empreinte de chaque fichier du thème copié (hors snapshot)
KV = "6.6.51+rpt-rpi-v8"                # noyau Pi 4 64 bits (Raspberry Pi OS Bookworm)
KV5 = "6.6.51+rpt-rpi-2712"             # noyau Pi 5 : jamais régénéré par OpenTV (-k exact, jamais -k all)
INITRD_IMG = "boot/initrd.img-" + KV
SHA1_STATE = "var/lib/initramfs-tools/" + KV
ARTIFACTS = (INITRD, INITRD_IMG, SHA1_STATE)   # tout ce que « update-initramfs -u -k KV » peut réécrire
PLY_ARGS = ["splash", "plymouth.ignore-serial-consoles"]
PI_CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 fsck.repair=yes rootwait "
              "cfg80211.ieee80211_regdom=CA\n")
# config.txt de Raspberry Pi OS Bookworm (abrégé) : dernière section [all], auto_initramfs=1
PI_CONFIG = ("# For more options and information see\n# http://rptl.io/configtxt\n\ndtparam=audio=on\n"
             "camera_auto_detect=1\ndisplay_auto_detect=1\n\n# Automatically load initramfs files, if found\n"
             "auto_initramfs=1\n\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\ndisable_fw_kms_setup=1\narm_64bit=1\n"
             "disable_overscan=1\narm_boost=1\n\n[cm4]\notg_mode=1\n\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n\n[all]\n")
DEBIAN_PLYC = "# Administrator customizations go in this file\n#[Daemon]\n#Theme=fade-in\n"

FAKE_UPDATE = r'''#!/bin/sh
# faux update-initramfs de Raspberry Pi OS : /boot/initrd.img-<v> remplacé ATOMIQUEMENT (renommage), état sha1 de
# initramfs-tools réécrit, puis crochet z50-raspi-firmware qui COPIE EN PLACE vers /boot/firmware/initramfs8
# (troncature puis écriture, non atomique). Journalise le thème et ses fichiers présents à l'appel. Interruptions
# simulées à l'appel n : OTV_TRUNC_UPDATE (copie tronquée puis échec), OTV_HANG_UPDATE (copie tronquée puis blocage),
# OTV_KILL_UPDATE (copie tronquée puis SIGKILL d'install.sh/uninstall.sh : coupure), OTV_RO_UPDATE (copie tronquée,
# /var/lib/initramfs-tools non inscriptible), OTV_STUCK_UPDATE (copie tronquée, écrivain qui ignore SIGTERM),
# OTV_MISMATCH_UPDATE (initramfs8 ≠ initrd.img).
R="$OPENTV_TEST_ROOT"
n=$(( $(grep -c '^CALL' "$OTV_BOOT_LOG" 2>/dev/null || echo 0) + 1 ))
theme=$(sed -n 's/^[[:space:]]*Theme[[:space:]]*=[[:space:]]*//p' "$R/etc/plymouth/plymouthd.conf" 2>/dev/null | tr -d '\r' | tail -n 1)
files=""
for f in opentv.plymouth opentv.script opentv.png opentv-mask.png; do
  [ -f "$R/usr/share/plymouth/themes/opentv/$f" ] && files="$files+$f"
done
printf 'CALL %s %s theme=%s files=%s\n' "$n" "$*" "$theme" "$files" >> "$OTV_BOOT_LOG"
hit() { case " $1 " in *" $n "*) return 0 ;; esac; return 1; }
hit "${OTV_FAIL_UPDATE:-}" && exit 1
if [ "$1" != -u ] || [ "$2" != -k ] || [ -z "$3" ] || [ "$3" = all ]; then
  echo "faux update-initramfs : -u -k <version exacte> exigé" >&2; exit 2
fi
k="$3"
[ -d "$R/usr/lib/modules/$k" ] || { echo "faux update-initramfs : noyau $k inconnu" >&2; exit 1; }
img="$R/boot/initrd.img-$k"
{
  echo "kernel $k"
  echo usr/sbin/plymouthd
  echo usr/lib/aarch64-linux-gnu/plymouth/script.so
  echo usr/lib/aarch64-linux-gnu/plymouth/renderers/drm.so
  echo "usr/lib/modules/$k/kernel/drivers/gpu/drm/vc4/vc4.ko.xz"
  if [ -n "$theme" ] && [ -d "$R/usr/share/plymouth/themes/$theme" ]; then
    for f in "$R/usr/share/plymouth/themes/$theme"/*; do
      [ -e "$f" ] && echo "usr/share/plymouth/themes/$theme/${f##*/}"
    done
  fi
} > "$img.new" && mv "$img.new" "$img"
mkdir -p "$R/var/lib/initramfs-tools"
s1=$( (sha1sum "$img" 2>/dev/null || shasum "$img") | cut -d' ' -f1)
printf '%s  /boot/initrd.img-%s\n' "$s1" "$k" > "$R/var/lib/initramfs-tools/$k"
# contenu copié (ce qu'un vrai initramfs embarque) : chemin + empreinte, pour comparer octet pour octet
: > "$R/boot/firmware/initramfs8.payload.new"
if [ -n "$theme" ] && [ -d "$R/usr/share/plymouth/themes/$theme" ]; then
  for f in "$R/usr/share/plymouth/themes/$theme"/*; do
    [ -f "$f" ] || continue
    h=$( (sha256sum "$f" 2>/dev/null || shasum -a 256 "$f") | cut -d' ' -f1)
    echo "${f##*/} $h" >> "$R/boot/firmware/initramfs8.payload.new"
  done
fi
mv "$R/boot/firmware/initramfs8.payload.new" "$R/boot/firmware/initramfs8.payload"
case "$k" in *-v8) fw="$R/boot/firmware/initramfs8" ;; *) exit 0 ;; esac
# z50-raspi-firmware : cp EN PLACE (la cible est tronquée puis réécrite)
if hit "${OTV_TRUNC_UPDATE:-}" || hit "${OTV_HANG_UPDATE:-}" || hit "${OTV_KILL_UPDATE:-}" ||
   hit "${OTV_RO_UPDATE:-}" || hit "${OTV_STUCK_UPDATE:-}"; then
  head -c 7 "$img" > "$fw"
  if hit "${OTV_KILL_UPDATE:-}"; then
    kill -KILL "$(ps -o ppid= -p "$PPID" | tr -d ' ')"   # parent de timeout = install.sh/uninstall.sh : coupure
    exit 1
  fi
  if hit "${OTV_RO_UPDATE:-}"; then chmod 555 "$R/var/lib/initramfs-tools"; exit 1; fi
  if hit "${OTV_STUCK_UPDATE:-}"; then
    (trap '' TERM; sleep "${OTV_STUCK_S:-3}"; head -c 9 "$img" > "$fw") &
  fi
  if hit "${OTV_HANG_UPDATE:-}" || hit "${OTV_STUCK_UPDATE:-}"; then sleep 60; fi
  exit 1
fi
if hit "${OTV_MISMATCH_UPDATE:-}"; then { cat "$img"; echo "#octets en trop"; } > "$fw"; exit 0; fi
cat "$img" > "$fw"
'''
FAKE_LS = r'''#!/bin/sh
printf 'LS %s\n' "$*" >> "$OTV_BOOT_LOG"
[ -z "${OTV_LS_SLEEP:-}" ] || sleep "$OTV_LS_SLEEP"
[ "${OTV_FAIL_LS:-0}" = 1 ] && exit 1
if [ -n "${OTV_LS_DROP:-}" ]; then grep -v -- "$OTV_LS_DROP" "$1"; else cat "$1"; fi
[ -z "${OTV_LS_ADD:-}" ] || printf '%s\n' "$OTV_LS_ADD"
'''


def slurp(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def tree(root):
    out = []
    for d, _, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def snapshot(root, skip=("var/lib/opentv-install/", INITRD, INITRD_IMG, "var/lib/initramfs-tools/")):
    out = {}
    for rel in tree(root):
        if rel.startswith(skip):
            continue
        p = os.path.join(root, rel)
        if os.path.islink(p):
            out[rel] = "link"
        else:
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


def unit(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
    cp.optionxform = str
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def load_png_tool():
    spec = importlib.util.spec_from_file_location("otv_png", os.path.join(PI4, "tools", "make-plymouth-theme.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness(unittest.TestCase):
    def tmp(self):
        d = tempfile.mkdtemp(prefix="otvply", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def rootfs(self, cmdline=PI_CMDLINE, config=PI_CONFIG, plyconf=DEBIAN_PLYC, plymouth=True, binary=False,
               kernels=(KV, KV5)):
        r = fixture_root(self)

        def w(rel, data, mode=0o644):
            p = os.path.join(r, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode())
            os.chmod(p, mode)
        w(CMD, cmdline)
        if config is not None:
            w(CFG, config)
        else:
            os.remove(os.path.join(r, CFG))
        w(INITRD, "INITRD-v0\n")
        # noyaux installés (Bookworm) : initrd.img-<v> + modules ; initramfs8 = copie de celui du noyau v8 courant
        for k in kernels:
            img = b"INITRD-v0\n" if k == KV else ("INITRD-" + k + "\n").encode()
            w("boot/initrd.img-" + k, img)
            w("usr/lib/modules/%s/modules.dep" % k, "")
            w("var/lib/initramfs-tools/" + k, hashlib.sha1(img).hexdigest() + "  /boot/initrd.img-" + k + "\n")
            if k.endswith("-2712"):
                w("boot/firmware/initramfs_2712", img)
        if plymouth:
            for rel in ("usr/sbin/plymouthd", "usr/bin/plymouth", "usr/sbin/update-initramfs", "usr/bin/lsinitramfs",
                        "usr/sbin/plymouth-set-default-theme"):
                w(rel, "#!/bin/sh\n", 0o755)
            w("usr/lib/aarch64-linux-gnu/plymouth/script.so", "")
            w("usr/share/initramfs-tools/hooks/plymouth", "#!/bin/sh\n", 0o755)
            os.makedirs(os.path.join(r, "etc/plymouth"), exist_ok=True)
            os.makedirs(os.path.join(r, "usr/share/plymouth/themes/text"), exist_ok=True)
            w("usr/share/plymouth/themes/text/text.plymouth", "[Plymouth Theme]\nModuleName=text\n")
            if plyconf is not None:
                w(PLYC, plyconf)
        return r

    def bindir(self):
        d = self.tmp()
        for name, body in (("update-initramfs", FAKE_UPDATE), ("lsinitramfs", FAKE_LS)):
            with open(os.path.join(d, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(d, name), 0o755)
        return d

    def run_sh(self, args, bash="bash", **env_extra):
        d = self.bindir()
        log = os.path.join(d, "boot.log")
        env = dict(os.environ, LC_ALL="C", PATH=d + os.pathsep + os.environ["PATH"], OTV_BOOT_LOG=log,
                   OPENTV_INSTALL_TEST_BOOTCMDS="1")
        env.pop("OPENTV_ROOT", None)
        env.update(env_extra)
        p = subprocess.run([bash] + list(args), capture_output=True, timeout=180, env=env)
        calls = slurp(log).splitlines() if os.path.exists(log) else []
        return p, calls

    def install(self, r, ok=True, **env):
        p, calls = self.run_sh([INSTALL, "--root", r, "--skip-preflight"], **env)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p, calls

    def uninstall(self, r, ok=True, *extra, **env):
        p, calls = self.run_sh([UNINSTALL, "--root", r] + list(extra), **env)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        return p, calls

    def read(self, r, rel):
        with open(os.path.join(r, rel), "rb") as f:
            return f.read()

    def exists(self, r, rel):
        return os.path.lexists(os.path.join(r, rel))

    def updates(self, calls):
        return [c for c in calls if c.startswith("CALL ")]

    def prebuild(self, r):
        """initramfs8 tel qu'une régénération l'aurait produit AVANT l'installation (référence exacte)."""
        d = self.bindir()
        env = dict(os.environ, OPENTV_TEST_ROOT=r, OTV_BOOT_LOG=os.path.join(d, "pre.log"))
        subprocess.run(["sh", os.path.join(d, "update-initramfs"), "-u", "-k", KV], check=True, env=env,
                       timeout=30)
        return self.read(r, INITRD), self.read(r, PAYLOAD)

    def assert_initramfs_is(self, r, ref, why=""):
        self.assertEqual((self.read(r, INITRD), self.read(r, PAYLOAD)), ref, why)

    def artifacts(self, r):
        """Octets de chaque fichier que « update-initramfs -u -k KV » peut réécrire (None : absent)."""
        return tuple(self.read(r, rel) if self.exists(r, rel) else None for rel in ARTIFACTS)

    def assert_rolled_back(self, r, before, p, calls, art):
        self.assertNotEqual(p.returncode, 0)
        err = p.stderr.decode()
        self.assertIn("retour à l'état précédent", err)
        self.assertEqual(snapshot(r), before)
        for rel in (BOOT_REC, CFG_REC, PLY_REC, PLY_ORIG, INITRD_REC, TXN):
            self.assertFalse(self.exists(r, rel), rel)
        self.assertEqual(self.artifacts(r), art)                         # octet pour octet, jamais « resynchronisé »
        self.assertNotIn(b"plymouth/themes/opentv", self.read(r, INITRD))

    def backup_meta(self, r):
        """meta de la sauvegarde initramfs la plus récente d'install.sh : {chemin: (sha256, octets copiés)}."""
        base = os.path.join(r, "var/lib/opentv-install")
        dirs = sorted(d for d in os.listdir(base) if d.startswith("backup-")
                      and os.path.isfile(os.path.join(base, d, "initramfs", "meta")))
        d = os.path.join(base, dirs[-1], "initramfs")
        out = {}
        for line in slurp(os.path.join(d, "meta")).splitlines():
            i, h, _n, _m, path = line.split(" ", 4)
            out[path] = (h, None if h == "absent" else slurp(os.path.join(d, i), "rb"))
        return out


class TestPlymouthInstall(Harness):
    def test_install_activates_theme_once_idempotent_and_uninstall_is_byte_exact(self):
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            r = self.rootfs()
            before = snapshot(r)
            art0 = self.artifacts(r)
            p, calls = self.run_sh([INSTALL, "--root", r, "--skip-preflight"], bash=bash)
            out = p.stdout.decode()
            self.assertEqual(p.returncode, 0, out + p.stderr.decode())
            words = self.read(r, CMD).decode().split()
            for a in PLY_ARGS:
                self.assertEqual(words.count(a), 1, (bash, a))
            self.assertIn("console=serial0,115200", words)                    # consoles conservées
            self.assertIn("console=tty1", words)
            cfg = self.read(r, CFG)
            self.assertEqual(cfg, PI_CONFIG.encode() + b"disable_splash=1\n")  # dernière section [all] : 1 ligne
            self.assertEqual(self.read(r, PLYC), (DEBIAN_PLYC + "[Daemon]\nTheme=opentv\n").encode())
            for t in THEME_FILES:
                self.assertEqual(self.read(r, os.path.join(THEME_DIR, t)), slurp(os.path.join(THEME_SRC, t), "rb"))
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(r, THEME_DIR, t)).st_mode), 0o644)
            # initramfs : UNE régénération, thème complet et Theme=opentv déjà en place à cet instant, puis relu
            up = self.updates(calls)
            self.assertEqual(len(up), 1, calls)
            self.assertIn("-u -k " + KV, up[0])                              # noyau v8 exact, jamais -k all
            self.assertIn("theme=opentv files=+opentv.plymouth+opentv.script+opentv.png+opentv-mask.png", up[0])
            self.assertTrue(any(c.startswith("LS ") and c.endswith(INITRD) for c in calls), calls)
            self.assertIn("plymouth/themes/opentv/opentv.script", self.read(r, INITRD).decode())
            self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))  # copie du crochet complète
            self.assertFalse(self.exists(r, TXN))                              # transaction validée
            meta = self.backup_meta(r)                                         # octets d'avant, durables + SHA-256
            for rel, data in zip(ARTIFACTS, art0):
                self.assertEqual(meta["/" + rel], (hashlib.sha256(data).hexdigest(), data), rel)
            self.assertEqual(self.read(r, "boot/firmware/initramfs_2712"), ("INITRD-" + KV5 + "\n").encode())
            man = self.read(r, "opt/opentv/MANIFEST.sha256").decode()
            for t in THEME_FILES:
                self.assertIn("  /%s/%s\n" % (THEME_DIR, t), man)
            for f in ("/opt/opentv/bin/opentv-plymouth-handoff", "/opt/opentv/lib/opentv-initramfs.sh",
                      "/etc/systemd/system/opentv-plymouth-deactivate.service",
                      "/etc/systemd/system/opentv-plymouth-quit.service",
                      "/etc/systemd/system/opentv-plymouth-failsafe.service",
                      "/etc/systemd/system/opentv-plymouth-failsafe.timer"):
                self.assertIn("  %s\n" % f, man)
            self.assertTrue(health.verify_manifest(r)["ok"])
            with open(os.path.join(r, BOOT_REC)) as f:
                self.assertEqual(f.read().split()[-2:], PLY_ARGS)
            self.assertIn("Plymouth : Theme=opentv", out)
            self.assertIn("image OpenTV (Plymouth", out)
            bs = health.boot_splash(r)
            self.assertEqual((bs["firmware_rainbow"], bs["plymouth_theme"], bs["next_boot"], bs["plymouth_ready"],
                              bs["theme_installed"]), ("disabled", "opentv", "plymouth", True, True))
            # réinstallation : octet pour octet, aucune régénération
            self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n")
            state = {rel: self.read(r, rel) for rel in (CMD, CFG, PLYC, INITRD, BOOT_REC, CFG_REC, PLY_REC, INITRD_REC)}
            p, calls = self.install(r)
            self.assertEqual(self.updates(calls), [], calls)
            self.assertIn("initramfs : inchangé", p.stdout.decode())
            for rel, data in state.items():
                self.assertEqual(self.read(r, rel), data, (bash, rel))
            # désinstallation (--purge : /etc/opentv aussi) : fichiers de démarrage d'origine à l'octet près
            p, calls = self.uninstall(r, True, "--purge")
            self.assertEqual(snapshot(r), before, bash)
            up = self.updates(calls)
            self.assertEqual(len(up), 1, calls)
            self.assertIn("theme= files=", up[0])                             # défaut Debian rétabli, thème retiré
            self.assertNotIn("plymouth/themes/opentv", self.read(r, INITRD).decode())
            for rel in (BOOT_REC, CFG_REC, PLY_REC, PLY_ORIG, INITRD_REC, THEME_DIR, "usr/share/plymouth/themes/opentv"):
                self.assertFalse(self.exists(r, rel), (bash, rel))
            self.assertTrue(self.exists(r, "usr/share/plymouth/themes/text/text.plymouth"))

    def test_previous_theme_replaced_then_restored_and_absent_conf_created_then_removed(self):
        for conf, mode in (("[Daemon]\nTheme=pix\nShowDelay=0\n", "replace"),
                           ("[Daemon]\nShowDelay=0\n", "insert"),
                           (None, "create")):
            r = self.rootfs(plyconf=conf)
            before = snapshot(r)
            self.install(r)
            with open(os.path.join(r, PLY_REC)) as f:
                rec = dict(l.split("=", 1) for l in f.read().splitlines())
            self.assertEqual(rec["mode"], mode)
            if mode == "replace":
                self.assertEqual(rec["prev"], b"Theme=pix".hex())
                self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=opentv\nShowDelay=0\n")
            elif mode == "insert":
                self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=opentv\nShowDelay=0\n")
            else:
                self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=opentv\n")
            self.assertEqual(rec["sha"], hashlib.sha256(self.read(r, PLYC)).hexdigest())
            _, calls = self.uninstall(r, True, "--purge")
            self.assertEqual(snapshot(r), before, mode)
            if mode == "replace":
                self.assertIn("theme=pix", self.updates(calls)[0])

    def test_existing_disable_splash_and_selected_theme_are_never_duplicated_or_recorded(self):
        cfg = PI_CONFIG + "disable_splash=1\n"
        r = self.rootfs(config=cfg, plyconf="[Daemon]\nTheme=opentv\n")
        ref = self.prebuild(r)                                             # initramfs avant OpenTV (sans thème)
        before = snapshot(r)
        p, calls = self.install(r)
        out = p.stdout.decode()
        self.assertIn("disable_splash=1 déjà présent", out)
        self.assertIn("thème opentv déjà sélectionné", out)
        self.assertEqual(self.read(r, CFG), cfg.encode())
        self.assertFalse(self.exists(r, CFG_REC))
        self.assertFalse(self.exists(r, PLY_REC))
        self.assertEqual(len(self.updates(calls)), 1)                      # thème nouvellement livré : régénéré
        self.assertIn(b"plymouth/themes/opentv/opentv.script", self.read(r, INITRD))
        self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n")   # régénération NOTÉE quand même
        p, calls = self.uninstall(r, True, "--purge")
        self.assertEqual(self.read(r, CFG), cfg.encode())                  # choix locaux conservés
        self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=opentv\n")
        up = self.updates(calls)                                           # régénéré APRÈS retrait du thème
        self.assertEqual(len(up), 1, calls)
        self.assertIn("theme=opentv files=", up[0])
        self.assertTrue(up[0].endswith("files="), up[0])
        self.assertNotIn(b"plymouth/themes/opentv", self.read(r, INITRD))   # aucune charge OpenTV périmée
        self.assert_initramfs_is(r, ref)                                   # = état d'avant l'installation
        self.assertIn("régénéré sans le thème opentv", p.stdout.decode())
        self.assertFalse(self.exists(r, INITRD_REC))
        self.assertFalse(self.exists(r, THEME_DIR))
        self.assertEqual(snapshot(r), before)
        # disable_splash seulement dans une section conditionnelle : ajouté (inconditionnel) en fin de fichier
        cond = "[all]\nauto_initramfs=1\n[cm4]\ndisable_splash=1\n"
        r = self.rootfs(config=cond)
        self.install(r)
        self.assertEqual(self.read(r, CFG), (cond + "[all]\ndisable_splash=1\n").encode())
        self.uninstall(r)
        self.assertEqual(self.read(r, CFG), cond.encode())

    def test_opentv_all_header_is_kept_when_user_lines_follow_it(self):
        cond = "[all]\nauto_initramfs=1\n[cm4]\notg_mode=1\n"
        r = self.rootfs(config=cond)
        self.install(r)
        self.assertEqual(self.read(r, CFG), (cond + "[all]\ndisable_splash=1\n").encode())
        with open(os.path.join(r, CFG), "ab") as f:
            f.write(b"dtparam=i2c_arm=on\n")                                  # ligne locale APRÈS [all] d'OpenTV
        out = self.uninstall(r)[0].stdout.decode()
        self.assertIn("[all] ajouté par OpenTV conservé", out)
        # sans [all], dtparam hériterait de [cm4] (jamais appliqué sur une Pi 4)
        self.assertEqual(self.read(r, CFG), (cond + "[all]\ndtparam=i2c_arm=on\n").encode())
        # seules des lignes vides suivent : [all] d'OpenTV retiré aussi
        r = self.rootfs(config=cond)
        self.install(r)
        with open(os.path.join(r, CFG), "ab") as f:
            f.write(b"\n  \r\n")
        self.uninstall(r)
        self.assertEqual(self.read(r, CFG), cond.encode() + b"\n  \r\n")

    def test_plymouth_refusal_spellings_keep_text_fallback(self):
        for w in ("plymouth.enable=false", "plymouth.enable=no", "plymouth.enable=OFF", "rd.plymouth=0"):
            r = self.rootfs(cmdline=PI_CMDLINE.rstrip("\n") + " " + w + "\n")
            p, calls = self.install(r)
            self.assertIn("Plymouth : non utilisé (désactivé localement", p.stdout.decode(), w)
            self.assertEqual(calls, [], w)
            self.assertNotIn("splash", self.read(r, CMD).decode().split(), w)
            self.assertEqual(health.boot_splash(r)["next_boot"], "text", w)
        self.assertTrue(health.plymouth_refused(["splash", "plymouth.enable=False"]))
        self.assertFalse(health.plymouth_refused(["splash", "plymouth.enable=1"]))

    def test_local_rainbow_choice_is_respected_and_reported_by_boot_check(self):
        cfg = PI_CONFIG + "disable_splash=0\n"
        r = self.rootfs(config=cfg)
        p, _ = self.install(r)
        self.assertIn("disable_splash=0 réglé localement, respecté", p.stdout.decode())
        self.assertEqual(self.read(r, CFG), cfg.encode())
        self.assertFalse(self.exists(r, CFG_REC))
        self.assertEqual(health.boot_splash(r)["firmware_rainbow"], "shown")

    def test_crlf_no_eol_conditional_last_section_and_multiline_preserved_exactly(self):
        cases = (
            (b"[all]\r\nauto_initramfs=1\r\ndtoverlay=vc4-kms-v3d\r\n[cm5]\r\notg_mode=1",          # CRLF, sans fin
             b"\r\n[all]\r\ndisable_splash=1\r\n",
             b"[Daemon]\r\nTheme=pix\r\nShowDelay=0", b"[Daemon]\r\nTheme=opentv\r\nShowDelay=0"),
            (b"auto_initramfs=1\ndtoverlay=vc4-kms-v3d",                                          # sans section
             b"\ndisable_splash=1\n",
             b"#c\n[Daemon]", b"#c\n[Daemon]\nTheme=opentv\n"),
            (b"auto_initramfs=1\n[pi4]\narm_boost=1\n",                                           # [pi4] : [all]
             b"[all]\ndisable_splash=1\n",
             b"[Daemon]\nShowDelay=0\nTheme = spinner \n[Other]\nX=1\n",
             b"[Daemon]\nShowDelay=0\nTheme=opentv\n[Other]\nX=1\n"),
            (b"", b"disable_splash=1\n", b"", b"[Daemon]\nTheme=opentv\n"),                       # vides
        )
        for cfg, sfx, conf, conf_new in cases:
            r = self.rootfs(config=cfg, plyconf=conf)
            p, calls = self.install(r)
            self.assertEqual(self.read(r, CFG), cfg + sfx, cfg)
            if cfg:
                self.assertEqual(self.read(r, PLYC), conf_new, conf)
            else:                                   # sans auto_initramfs : Plymouth inutilisable, arc-en-ciel retiré
                self.assertIn("Plymouth : non utilisé (initramfs non chargé", p.stdout.decode())
                self.assertEqual(self.read(r, PLYC), conf)
            self.uninstall(r)
            self.assertEqual(self.read(r, CFG), cfg, cfg)
            self.assertEqual(self.read(r, PLYC), conf, conf)
            self.assertEqual(self.read(r, CMD), PI_CMDLINE.encode())

    def test_local_edits_after_install_survive_uninstall(self):
        r = self.rootfs(plyconf="[Daemon]\nTheme=pix\n")
        self.install(r)
        with open(os.path.join(r, CFG), "ab") as f:
            f.write(b"dtparam=i2c_arm=on\n")                                   # ajout local APRÈS le nôtre
        with open(os.path.join(r, PLYC), "ab") as f:
            f.write(b"ShowDelay=2\n")
        with open(os.path.join(r, CMD), "rb") as f:
            line = f.read().rstrip(b"\n")
        with open(os.path.join(r, CMD), "wb") as f:
            f.write(line + b" usbcore.autosuspend=-1\n")
        out = self.uninstall(r)[0].stdout.decode()
        self.assertIn("seule la ligne disable_splash=1 ajoutée par OpenTV est retirée", out)
        self.assertEqual(self.read(r, CFG), PI_CONFIG.encode() + b"dtparam=i2c_arm=on\n")
        self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=pix\nShowDelay=2\n")
        self.assertEqual(self.read(r, CMD), PI_CMDLINE.rstrip("\n").encode() + b" usbcore.autosuspend=-1\n")
        # thème changé localement après l'installation : conservé (jamais écrasé par la désinstallation)
        r = self.rootfs()
        self.install(r)
        with open(os.path.join(r, PLYC), "wb") as f:
            f.write(b"[Daemon]\nTheme=spinner\n")
        out = self.uninstall(r)[0].stdout.decode()
        self.assertIn("thème « spinner » choisi localement", out)
        self.assertEqual(self.read(r, PLYC), b"[Daemon]\nTheme=spinner\n")

    def test_dry_run_plans_everything_and_writes_nothing(self):
        r = self.rootfs()
        before = snapshot(r, skip=())
        p, calls = self.run_sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(snapshot(r, skip=()), before)
        self.assertEqual(calls, [])
        for s in ("splash plymouth.ignore-serial-consoles", "ajouter disable_splash=1",
                  "[dry-run] Plymouth : Theme=opentv", "[dry-run] initramfs : update-initramfs -u -k " + KV,
                  "[dry-run] initramfs : sauvegarde durable (SHA-256)", "jamais SIGKILL"):
            self.assertIn(s, out)
        self.install(r)
        before = snapshot(r, skip=())
        p, calls = self.run_sh([UNINSTALL, "--dry-run", "--root", r])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(snapshot(r, skip=()), before)
        self.assertEqual(self.updates(calls), [])
        self.assertIn("[dry-run] retirer disable_splash=1", p.stdout.decode())
        self.assertIn("[dry-run] Plymouth : thème précédent rétabli", p.stdout.decode())


class TestPlymouthFallbackAndFailures(Harness):
    def test_missing_or_disabled_plymouth_keeps_text_fallback_and_never_touches_initramfs(self):
        variants = {
            "plymouthd": dict(plymouth=False),
            "auto_initramfs": dict(config="[all]\ndtoverlay=vc4-kms-v3d\n"),
            "nosplash": dict(cmdline=PI_CMDLINE.rstrip("\n") + " nosplash\n"),
            "plymouth.enable=0": dict(cmdline=PI_CMDLINE.rstrip("\n") + " plymouth.enable=0\n"),
        }
        for why, kw in variants.items():
            r = self.rootfs(**kw)
            orig_cmd = self.read(r, CMD)
            p, calls = self.install(r)
            out = p.stdout.decode()
            self.assertIn("Plymouth : non utilisé", out, why)
            self.assertIn("repli : écran OpenTV texte sur tty1", out, why)
            self.assertEqual(calls, [], why)                                   # aucun update-initramfs
            self.assertEqual(self.read(r, INITRD), b"INITRD-v0\n", why)
            words = self.read(r, CMD).decode().split()
            for a in PLY_ARGS:
                self.assertEqual(words.count(a), orig_cmd.decode().split().count(a), (why, a))
            if self.exists(r, PLYC):
                self.assertNotIn(b"opentv", self.read(r, PLYC), why)
            self.assertFalse(self.exists(r, PLY_REC), why)
            self.assertTrue(self.read(r, CFG).endswith(b"disable_splash=1\n"), why)   # arc-en-ciel retiré quand même
            for t in THEME_FILES:                                               # livré (manifeste stable), inactif
                self.assertTrue(self.exists(r, os.path.join(THEME_DIR, t)), why)
            self.assertEqual(health.boot_splash(r)["next_boot"], "text", why)
            self.uninstall(r)
            self.assertEqual(self.read(r, CMD), orig_cmd, why)
            self.assertFalse(self.exists(r, CFG_REC), why)
        # plus tard : Plymouth installé (prepare-pi.sh) puis réinstallation ⇒ activé
        r = self.rootfs(plymouth=False)
        self.install(r)
        fresh = self.rootfs()
        for rel in ("usr/sbin/plymouthd", "usr/bin/plymouth", "usr/sbin/update-initramfs", "usr/bin/lsinitramfs",
                    "usr/lib/aarch64-linux-gnu/plymouth/script.so", "usr/share/initramfs-tools/hooks/plymouth", PLYC):
            os.makedirs(os.path.dirname(os.path.join(r, rel)), exist_ok=True)
            shutil.copy2(os.path.join(fresh, rel), os.path.join(r, rel))
        _, calls = self.install(r)
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertEqual(self.read(r, PLYC), (DEBIAN_PLYC + "[Daemon]\nTheme=opentv\n").encode())

    def test_initramfs_failures_roll_back_and_restore_exact_bytes_without_resync(self):
        slow = dict(OTV_LS_SLEEP="6", OPENTV_TEST_IR_LS_S="1")                # lsinitramfs lent : au-delà du délai
        for label, env in (("update échoue", dict(OTV_FAIL_UPDATE="1")),
                           ("copie en place tronquée puis échec", dict(OTV_TRUNC_UPDATE="1")),
                           ("relecture échoue", dict(OTV_FAIL_LS="1")),
                           ("relecture trop lente", slow),
                           ("thème absent de l'initramfs", dict(OTV_LS_DROP="opentv.png")),
                           ("module script absent", dict(OTV_LS_DROP="script.so")),
                           ("initramfs8 différent de initrd.img", dict(OTV_MISMATCH_UPDATE="1"))):
            r = self.rootfs(plyconf="[Daemon]\nTheme=pix\n")
            before, art = snapshot(r), self.artifacts(r)
            t0 = time.monotonic()
            p, calls = self.install(r, ok=False, **env)
            self.assert_rolled_back(r, before, p, calls, art)
            up = self.updates(calls)
            self.assertEqual(len(up), 1, (label, calls))                       # aucune « resynchronisation »
            self.assertIn("theme=opentv", up[0], label)
            self.assertIn("-u -k " + KV, up[0], label)
            if label != "update échoue":
                self.assertIn("rétabli octet pour octet", p.stderr.decode(), label)
            if env is slow:
                self.assertLess(time.monotonic() - t0, 30, label)              # borné, pas 6 s × appels
            if label == "initramfs8 différent de initrd.img":
                self.assertIn("différent de /boot/initrd.img-" + KV, p.stderr.decode())

    def test_late_failure_after_rebuild_restores_exact_initramfs_bytes(self):
        r = self.rootfs()
        art = self.artifacts(r)
        os.makedirs(os.path.join(r, "opt/opentv/MANIFEST.sha256.tmp"))         # manifeste impossible : APRÈS l'initramfs
        p, calls = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        up = self.updates(calls)
        self.assertEqual(len(up), 1, calls)                                   # vérifiée puis annulée : octets d'avant
        self.assertIn("theme=opentv", up[0])
        self.assertEqual(self.artifacts(r), art)
        self.assertEqual(self.read(r, CMD), PI_CMDLINE.encode())
        self.assertEqual(self.read(r, CFG), PI_CONFIG.encode())
        self.assertEqual(self.read(r, PLYC), DEBIAN_PLYC.encode())
        for rel in (BOOT_REC, CFG_REC, PLY_REC, PLY_ORIG, INITRD_REC, TXN) + tuple(os.path.join(THEME_DIR, t) for t in THEME_FILES):
            self.assertFalse(self.exists(r, rel), rel)

    def test_late_env_failure_rolls_back_config_and_plymouth(self):
        r = self.rootfs()
        before, art = snapshot(r), self.artifacts(r)
        os.makedirs(os.path.join(r, "etc/opentv"))
        os.chmod(os.path.join(r, "etc/opentv"), 0o555)
        self.addCleanup(os.chmod, os.path.join(r, "etc/opentv"), 0o755)
        p, calls = self.install(r, ok=False)
        os.chmod(os.path.join(r, "etc/opentv"), 0o755)
        self.assert_rolled_back(r, before, p, calls, art)
        self.assertEqual(self.updates(calls), [])                          # échec AVANT la régénération

    def test_unsafe_or_unexpected_boot_files_are_refused_before_any_change(self):
        def symlinked_cfg(r):
            real = os.path.join(r, "elsewhere.txt")
            shutil.move(os.path.join(r, CFG), real)
            os.symlink(real, os.path.join(r, CFG))

        def nul_cfg(r):
            with open(os.path.join(r, CFG), "ab") as f:
                f.write(b"\x00garbage\n")

        def big_cfg(r):
            with open(os.path.join(r, CFG), "ab") as f:
                f.write(b"#" * 70000 + b"\n")

        def symlinked_conf(r):
            real = os.path.join(r, "victim.conf")
            with open(real, "w") as f:
                f.write("[Daemon]\nTheme=pix\n")
            os.remove(os.path.join(r, PLYC))
            os.symlink(real, os.path.join(r, PLYC))

        def dir_conf(r):
            os.remove(os.path.join(r, PLYC))
            os.makedirs(os.path.join(r, PLYC))

        for name, mutate in (("config.txt", symlinked_cfg), ("config.txt", nul_cfg), ("config.txt", big_cfg),
                             ("plymouthd.conf", symlinked_conf), ("plymouthd.conf", dir_conf)):
            r = self.rootfs()
            mutate(r)
            before = snapshot(r, skip=())
            p, calls = self.install(r, ok=False)
            self.assertNotEqual(p.returncode, 0, mutate.__name__)
            self.assertIn(name + " : fichier inattendu", p.stderr.decode(), mutate.__name__)
            self.assertEqual(snapshot(r, skip=()), before, mutate.__name__)
            self.assertFalse(self.exists(r, "opt/opentv"), mutate.__name__)
            self.assertEqual(calls, [], mutate.__name__)

    def test_unwritable_boot_partition_fails_without_markers_or_rebuild(self):
        r = self.rootfs()
        before, art = snapshot(r), self.artifacts(r)
        os.chmod(os.path.join(r, "boot/firmware"), 0o555)
        self.addCleanup(os.chmod, os.path.join(r, "boot/firmware"), 0o755)
        p, calls = self.install(r, ok=False)
        os.chmod(os.path.join(r, "boot/firmware"), 0o755)
        self.assert_rolled_back(r, before, p, calls, art)
        self.assertEqual(self.updates(calls), [])

    def test_uninstall_refuses_before_any_change_when_config_or_conf_is_unavailable(self):
        r = self.rootfs()
        self.install(r)
        cfg = self.read(r, CFG)
        recs = {rel: self.read(r, rel) for rel in (BOOT_REC, CFG_REC, PLY_REC)}
        os.remove(os.path.join(r, CFG))
        before = snapshot(r, skip=())
        for extra in ([], ["--purge"]):
            p, calls = self.uninstall(r, False, *extra)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("config.txt", p.stderr.decode())
            self.assertIn("relancer", p.stderr.decode())
            self.assertEqual(snapshot(r, skip=()), before)                   # cmdline.txt NON modifié non plus
            self.assertEqual(self.updates(calls), [])
        for rel, data in recs.items():
            self.assertEqual(self.read(r, rel), data)
        with open(os.path.join(r, CFG), "wb") as f:
            f.write(cfg)
        conf = self.read(r, PLYC)
        os.remove(os.path.join(r, PLYC))
        os.symlink("/etc/passwd", os.path.join(r, PLYC))                       # plymouthd.conf devenu un lien
        p, _ = self.uninstall(r, False)
        self.assertIn("plymouthd.conf inattendu", p.stderr.decode())
        self.assertEqual(self.read(r, CFG), cfg)
        os.remove(os.path.join(r, PLYC))
        with open(os.path.join(r, PLYC), "wb") as f:
            f.write(conf)
        self.uninstall(r)                                                      # nouvel essai : retour exact
        self.assertEqual(self.read(r, CFG), PI_CONFIG.encode())
        self.assertEqual(self.read(r, PLYC), DEBIAN_PLYC.encode())
        self.assertEqual(self.read(r, CMD), PI_CMDLINE.encode())

    def test_uninstall_initramfs_failure_restores_bytes_and_stops_before_removing_retry_assets(self):
        for label, env in (("échec", dict(OTV_FAIL_UPDATE="1")),
                           ("copie tronquée", dict(OTV_TRUNC_UPDATE="1")),
                           ("blocage au-delà du délai", dict(OTV_HANG_UPDATE="1", OPENTV_TEST_IR_UPDATE_S="2",
                                                              OPENTV_TEST_IR_GRACE_S="5"))):
            for extra in ([], ["--purge"]):
                r = self.rootfs()
                self.install(r)
                art = self.artifacts(r)                                        # image OpenTV vérifiée, amorçable
                t0 = time.monotonic()
                p, calls = self.uninstall(r, False, *extra, **env)
                self.assertLess(time.monotonic() - t0, 40, label)
                err = p.stderr.decode()
                self.assertEqual(p.returncode, 1, (label, err))
                self.assertIn("désinstallation INTERROMPUE", err)
                self.assertIn("sudo /opt/opentv/uninstall.sh", err)
                if label != "échec":
                    self.assertIn("rétabli octet pour octet", err, label)
                self.assertEqual(self.artifacts(r), art, (label, extra))       # octets d'avant, jamais tronqués
                self.assertEqual(self.read(r, CFG), PI_CONFIG.encode())        # fichiers de démarrage déjà rendus
                self.assertEqual(self.read(r, PLYC), DEBIAN_PLYC.encode())
                for rel in ("opt/opentv/uninstall.sh", "opt/opentv/lib/opentv-initramfs.sh",
                            "opt/opentv/MANIFEST.sha256", "etc/systemd/system/opentv-kiosk.service", INITRD_REC):
                    self.assertTrue(self.exists(r, rel), (label, extra, rel))  # rien de ce qui permet de relancer
                self.assertFalse(self.exists(r, TXN), label)
                # relance avec le désinstalleur INSTALLÉ : régénération vérifiée, puis désinstallation complète
                p, calls = self.run_sh([os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r] + extra)
                self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
                self.assertEqual(len(self.updates(calls)), 1)
                self.assertFalse(self.exists(r, "opt/opentv"), label)
                self.assertFalse(self.exists(r, INITRD_REC), label)
                self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))
                self.assertNotIn(b"plymouth/themes/opentv", self.read(r, INITRD))

    def test_altered_markers_are_closed_lists(self):
        r = self.rootfs()
        self.install(r)
        with open(os.path.join(r, CFG_REC), "w") as f:
            f.write("suffix=%s\n" % b"init=/bin/sh\n".hex())                  # suffixe hors forme OpenTV
        with open(os.path.join(r, PLY_REC), "w") as f:
            f.write("mode=evil\nprev=\nsha=\n")
        with open(os.path.join(r, BOOT_REC), "a") as f:
            f.write("init=/bin/sh\n")
        out = self.uninstall(r)[0].stdout.decode()
        for s in ("marqueur config.txt hors forme OpenTV", "marqueur Plymouth invalide", "hors liste OpenTV"):
            self.assertIn(s, out)
        self.assertTrue(self.read(r, CFG).endswith(b"disable_splash=1\n"))   # jamais retiré à l'aveugle
        self.assertEqual(self.read(r, PLYC), (DEBIAN_PLYC + "[Daemon]\nTheme=opentv\n").encode())
        self.assertEqual(self.read(r, CMD), PI_CMDLINE.encode())             # jetons de la liste fermée retirés


class TestInitramfsRebuildRecord(Harness):
    """Toute régénération due à OpenTV est notée (même Theme=opentv antérieur) et exactement défaite."""

    def user_theme(self, r):
        d = os.path.join(r, THEME_DIR)
        os.makedirs(d)
        with open(os.path.join(d, "opentv.plymouth"), "wb") as f:
            f.write(b"[Plymouth Theme]\nName=mon theme opentv\nModuleName=two-step\n")
        with open(os.path.join(d, "custom.png"), "wb") as f:
            f.write(b"\x89PNG user")

    def test_preexisting_theme_directory_is_restored_and_initramfs_matches_pre_install(self):
        for conf in ("[Daemon]\nTheme=opentv\n", "[Daemon]\nTheme=pix\n"):
            r = self.rootfs(plyconf=conf)
            self.user_theme(r)
            ref = self.prebuild(r)
            before = snapshot(r)
            _, calls = self.install(r)
            self.assertEqual(len(self.updates(calls)), 1, conf)
            self.assertEqual(self.read(r, os.path.join(THEME_DIR, "opentv.plymouth")),
                             slurp(os.path.join(THEME_SRC, "opentv.plymouth"), "rb"))
            self.assertTrue(self.exists(r, INITRD_REC), conf)
            p, calls = self.uninstall(r, True, "--purge")
            self.assertEqual(snapshot(r), before, conf)                    # thème de l'utilisateur rétabli
            self.assertEqual(len(self.updates(calls)), 1, conf)
            self.assert_initramfs_is(r, ref, conf)                         # contenu d'avant OpenTV, octet pour octet
            self.assertFalse(self.exists(r, INITRD_REC), conf)
            if "opentv" in conf:
                self.assertIn("thème opentv d'origine", p.stdout.decode())
                self.assertIn(b"themes/opentv/custom.png", self.read(r, INITRD))

    def test_each_missing_prerequisite_keeps_text_fallback_without_rebuild_or_marker(self):
        def rm(rel):
            return lambda r: os.remove(os.path.join(r, rel))

        def no_etc_plymouth(r):
            shutil.rmtree(os.path.join(r, "etc/plymouth"))

        variants = (
            ("client plymouth absent", rm("usr/bin/plymouth"), {}),
            ("module script de Plymouth absent", rm("usr/lib/aarch64-linux-gnu/plymouth/script.so"), {}),
            ("/etc/plymouth absent", no_etc_plymouth, {}),
            ("crochet initramfs de Plymouth absent", rm("usr/share/initramfs-tools/hooks/plymouth"), {}),
            ("update-initramfs absent", rm("usr/sbin/update-initramfs"), {}),
            ("lsinitramfs absent", rm("usr/bin/lsinitramfs"), {}),
            ("/boot/firmware/initramfs8 absent", rm(INITRD), {}),
            ("aucun noyau v8 complet", rm(INITRD_IMG), {}),
            ("config.txt absent", lambda r: None, dict(config=None)),
        )
        for why, mutate, kw in variants:
            r = self.rootfs(**kw)
            mutate(r)
            p, calls = self.install(r)
            out = p.stdout.decode()
            self.assertIn("Plymouth : non utilisé (" + why, out, why)
            self.assertEqual(self.updates(calls), [], why)
            for rel in (PLY_REC, INITRD_REC):
                self.assertFalse(self.exists(r, rel), (why, rel))
            self.assertNotIn("splash", self.read(r, CMD).decode().split(), why)
            _, calls = self.uninstall(r)
            self.assertEqual(self.updates(calls), [], why)
        # « initramfs <fichier> followkernel » au lieu d'auto_initramfs=1 : utilisable
        r = self.rootfs(config="[all]\ninitramfs initramfs8 followkernel\n")
        _, calls = self.install(r)
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertTrue(self.exists(r, INITRD_REC))

    def test_uninstall_rebuild_failure_keeps_marker_then_retry_restores_pre_install_initramfs(self):
        for extra in ([], ["--purge"]):
            r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")              # aucun plymouth-theme.added
            ref = self.prebuild(r)
            self.install(r)
            self.assertFalse(self.exists(r, PLY_REC))
            p, calls = self.uninstall(r, False, *extra, OTV_FAIL_UPDATE="1")
            self.assertEqual(p.returncode, 1, extra)
            self.assertIn("initramfs-plymouth.rebuilt sont conservés", p.stderr.decode())
            self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n", extra)
            self.assertTrue(self.exists(r, "opt/opentv/uninstall.sh"), extra)  # --purge non atteint non plus
            # vérification qui trouve encore une entrée OpenTV périmée : octets restaurés, marqueur conservé aussi
            self.install(r)
            art = self.artifacts(r)
            p, _ = self.uninstall(r, False, *extra, OTV_LS_ADD=THEME_DIR + "/opentv.script")
            self.assertEqual(p.returncode, 1)
            self.assertIn("périmé", p.stderr.decode())
            self.assertEqual(self.artifacts(r), art)
            self.assertTrue(self.exists(r, INITRD_REC))
            # nouvel essai : régénéré, marqueur effacé, état d'origine
            _, calls = self.uninstall(r, True, *extra)
            self.assertEqual(len(self.updates(calls)), 1)
            self.assert_initramfs_is(r, ref, extra)
            self.assertFalse(self.exists(r, INITRD_REC))
        # désinstalleur seul avec un marqueur résiduel (manifeste absent) : régénère et efface
        r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")
        os.makedirs(os.path.join(r, "var/lib/opentv-install"))
        with open(os.path.join(r, INITRD_REC), "w") as f:
            f.write("rebuilt=opentv-plymouth\n")
        _, calls = self.uninstall(r)
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertFalse(self.exists(r, INITRD_REC))
        # aucun noyau v8 complet : arrêt AVANT toute régénération ou retrait, marqueur conservé
        r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")
        self.install(r)
        os.remove(os.path.join(r, INITRD_IMG))
        p, calls = self.uninstall(r, False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("aucun noyau v8", p.stderr.decode())
        self.assertEqual(self.updates(calls), [])
        self.assertTrue(self.exists(r, INITRD_REC))
        self.assertTrue(self.exists(r, "opt/opentv/uninstall.sh"))

    def test_install_rollback_restores_bytes_and_keeps_journal_when_restore_is_impossible(self):
        r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")
        before, art = snapshot(r), self.artifacts(r)
        p, calls = self.install(r, ok=False, OTV_TRUNC_UPDATE="1")             # copie en place tronquée : restaurée
        self.assert_rolled_back(r, before, p, calls, art)
        self.assertEqual(len(self.updates(calls)), 1)
        # restauration impossible (/var/lib/initramfs-tools non inscriptible) : journal ET marqueur conservés,
        # jamais présentés comme sains
        p, calls = self.install(r, ok=False, OTV_RO_UPDATE="1")
        st = os.path.join(r, "var/lib/initramfs-tools")
        self.addCleanup(os.chmod, st, 0o755)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("initramfs NON rétabli", p.stderr.decode())
        self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n")
        self.assertTrue(self.exists(r, TXN))
        self.assertEqual(self.read(r, INITRD), art[0])                         # restauré là où c'était possible
        self.assertEqual(snapshot(r), before)                                  # le reste est bien revenu
        os.chmod(st, 0o755)
        # désinstalleur des sources : journal rejoué AVANT tout (octets d'origine relus), puis régénération vérifiée
        p, calls = self.run_sh([UNINSTALL, "--root", r])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertIn("transaction interrompue rejouée", p.stdout.decode())
        self.assertEqual(len(self.updates(calls)), 1)
        for rel in (TXN, INITRD_REC):
            self.assertFalse(self.exists(r, rel), rel)
        self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))
        # mise à jour qui échoue après la régénération : marqueur ANTÉRIEUR conservé tel quel, octets d'avant rendus
        r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")
        self.install(r)
        art = self.artifacts(r)
        with open(os.path.join(r, THEME_DIR, "opentv.script"), "ab") as f:
            f.write(b"// local edit\n")                                             # thème à relivrer
        os.makedirs(os.path.join(r, "opt/opentv/MANIFEST.sha256.tmp"))
        p, calls = self.install(r, ok=False)
        self.assertEqual(len(self.updates(calls)), 1, calls)
        self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n")
        self.assertEqual(self.artifacts(r), art)
        self.assertFalse(self.exists(r, TXN))

    def test_cmdline_splash_readded_rebuilds_and_keeps_record(self):
        r = self.rootfs()
        self.install(r)
        line = self.read(r, CMD).decode().replace(" splash", "")
        with open(os.path.join(r, CMD), "w") as f:
            f.write(line)
        _, calls = self.install(r)
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertEqual(self.read(r, CMD).decode().split().count("splash"), 1)
        self.assertEqual(self.read(r, INITRD_REC), b"rebuilt=opentv-plymouth\n")

    def test_unsafe_marker_is_refused_and_unexpected_content_still_rebuilds(self):
        # install : marqueur lien/dossier ⇒ refus avant toute modification
        for kind in ("link", "dir"):
            r = self.rootfs()
            os.makedirs(os.path.join(r, "var/lib/opentv-install"))
            if kind == "link":
                os.symlink("/etc/passwd", os.path.join(r, INITRD_REC))
            else:
                os.makedirs(os.path.join(r, INITRD_REC))
            before = snapshot(r, skip=())
            p, calls = self.install(r, ok=False)
            self.assertNotEqual(p.returncode, 0, kind)
            self.assertIn("marqueur inattendu", p.stderr.decode(), kind)
            self.assertEqual(snapshot(r, skip=()), before, kind)
            self.assertEqual(calls, [], kind)
        # uninstall : marqueur devenu un lien ⇒ refus avant toute modification, rien régénéré
        r = self.rootfs(plyconf="[Daemon]\nTheme=opentv\n")
        self.install(r)
        os.remove(os.path.join(r, INITRD_REC))
        os.symlink("/etc/passwd", os.path.join(r, INITRD_REC))
        before = snapshot(r, skip=())
        p, calls = self.uninstall(r, False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("initramfs-plymouth.rebuilt inattendu", p.stderr.decode())
        self.assertEqual(snapshot(r, skip=()), before)
        self.assertEqual(calls, [])
        # contenu inattendu (fichier régulier) : régénération quand même, puis effacé
        os.remove(os.path.join(r, INITRD_REC))
        with open(os.path.join(r, INITRD_REC), "w") as f:
            f.write("garbage\n")
        p, calls = self.uninstall(r)
        self.assertIn("contenu inattendu", p.stdout.decode())
        self.assertEqual(len(self.updates(calls)), 1)
        self.assertNotIn(b"plymouth/themes/opentv", self.read(r, INITRD))
        self.assertFalse(self.exists(r, INITRD_REC))


class TestInitramfsTransaction(Harness):
    """Copie EN PLACE de z50-raspi-firmware : coupure, blocage et écrivain tardif rétablis octet pour octet."""

    def popen_killed(self, args, **env_extra):
        p, calls = self.run_sh(args, **env_extra)
        self.assertEqual(p.returncode, -9, p.stdout.decode() + p.stderr.decode())   # « coupure » : tué net
        return calls

    def test_power_cut_during_install_rebuild_then_next_install_or_uninstall_restores_first(self):
        for nxt in ("install", "uninstall-fail", "uninstall"):
            r = self.rootfs()
            before, art = snapshot(r), self.artifacts(r)
            self.popen_killed([INSTALL, "--root", r, "--skip-preflight"], OTV_KILL_UPDATE="1")
            self.assertEqual(len(self.read(r, INITRD)), 7)                     # initramfs8 tronqué par la copie
            self.assertTrue(self.exists(r, TXN))
            meta = self.backup_meta(r)                                         # sauvegarde durable AVANT la copie
            for rel, data in zip(ARTIFACTS, art):
                self.assertEqual(meta["/" + rel], (hashlib.sha256(data).hexdigest(), data), rel)
            if nxt == "install":
                p, calls = self.install(r)
                self.assertIn("transaction interrompue rejouée", p.stdout.decode())
                self.assertEqual(len(self.updates(calls)), 1)                  # régénération forcée après rejeu
                self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))
                self.assertIn(b"plymouth/themes/opentv/opentv.script", self.read(r, INITRD))
                self.assertFalse(self.exists(r, TXN))
                self.uninstall(r, True, "--purge")
                self.assertEqual(snapshot(r), before)
            elif nxt == "uninstall-fail":
                # rejeu prouvé octet pour octet : la régénération de la désinstallation échoue ensuite, et les
                # octets rendus sont exactement ceux d'avant l'installation coupée
                p, _ = self.uninstall(r, False, OTV_FAIL_UPDATE="1")
                self.assertEqual(p.returncode, 1)
                self.assertIn("transaction interrompue rejouée", p.stdout.decode())
                self.assertEqual(self.artifacts(r), art)
                self.assertFalse(self.exists(r, TXN))
            else:
                p, calls = self.uninstall(r)
                self.assertEqual(len(self.updates(calls)), 1)
                self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))
                for rel in (TXN, INITRD_REC):
                    self.assertFalse(self.exists(r, rel), rel)
                self.assertEqual(self.read(r, CMD), PI_CMDLINE.encode())

    def test_power_cut_during_uninstall_rebuild_is_restored_then_retry_completes(self):
        r = self.rootfs()
        before = snapshot(r)
        self.install(r)
        art = self.artifacts(r)                                                # image OpenTV, vérifiée
        self.popen_killed([UNINSTALL, "--root", r], OTV_KILL_UPDATE="1")
        self.assertEqual(len(self.read(r, INITRD)), 7)
        self.assertTrue(self.exists(r, TXN))
        self.assertTrue(self.exists(r, "opt/opentv/uninstall.sh"))
        p, _ = self.uninstall(r, False, OTV_FAIL_UPDATE="1")                   # rejeu, puis échec : octets exacts
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.artifacts(r), art)
        p, calls = self.run_sh([os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r, "--purge"])
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        self.assertEqual(snapshot(r), before)
        self.assertEqual(self.read(r, INITRD), self.read(r, INITRD_IMG))

    def test_hung_rebuild_is_terminated_without_sigkill_and_restored(self):
        r = self.rootfs()
        before, art = snapshot(r), self.artifacts(r)
        t0 = time.monotonic()
        p, calls = self.install(r, ok=False, OTV_HANG_UPDATE="1", OPENTV_TEST_IR_UPDATE_S="2",
                                OPENTV_TEST_IR_GRACE_S="5")
        self.assertLess(time.monotonic() - t0, 40)
        self.assertIn("délai de 2 s dépassé (SIGTERM)", p.stderr.decode())
        self.assert_rolled_back(r, before, p, calls, art)

    def test_writer_surviving_sigterm_is_never_raced_and_is_restored_by_next_run(self):
        r = self.rootfs()
        before, art = snapshot(r), self.artifacts(r)
        p, calls = self.install(r, ok=False, OTV_STUCK_UPDATE="1", OTV_STUCK_S="4", OPENTV_TEST_IR_UPDATE_S="1",
                                OPENTV_TEST_IR_GRACE_S="1")
        err = p.stderr.decode()
        self.assertIn("encore actifs", err)
        self.assertIn("initramfs NON restauré maintenant", err)
        self.assertTrue(self.exists(r, TXN))                                   # jamais de restauration concurrente
        self.assertTrue(self.exists(r, INITRD_REC))
        self.assertEqual(snapshot(r), before)
        deadline = time.monotonic() + 15
        while len(self.read(r, INITRD)) != 9 and time.monotonic() < deadline:   # l'écrivain tardif finit
            time.sleep(0.2)
        self.assertEqual(len(self.read(r, INITRD)), 9)
        time.sleep(0.3)
        p, calls = self.uninstall(r, False, OTV_FAIL_UPDATE="1")               # rejeu : octets d'origine
        self.assertIn("transaction interrompue rejouée", p.stdout.decode())
        self.assertEqual(self.artifacts(r), art)
        self.assertFalse(self.exists(r, TXN))

    def test_altered_backup_or_journal_is_refused_fail_closed(self):
        r = self.rootfs()
        self.popen_killed([INSTALL, "--root", r, "--skip-preflight"], OTV_KILL_UPDATE="1")
        base = os.path.join(r, "var/lib/opentv-install")
        bk = [d for d in os.listdir(base) if d.startswith("backup-")][0]
        with open(os.path.join(base, bk, "initramfs", "0"), "ab") as f:
            f.write(b"x")                                                       # sauvegarde altérée
        p, calls = self.uninstall(r, False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("altérée", p.stderr.decode())
        self.assertEqual(self.updates(calls), [])
        self.assertTrue(self.exists(r, TXN))
        self.assertEqual(len(self.read(r, INITRD)), 7)                         # jamais remplacé par une copie fausse
        p, _ = self.install(r, ok=False)
        self.assertIn("transaction initramfs interrompue non rétablie", p.stderr.decode())
        # journal pointant hors de /var/lib/opentv-install : refusé
        with open(os.path.join(r, TXN), "w") as f:
            f.write("backup=/etc\nkernel=%s\nop=install\n" % KV)
        p, _ = self.uninstall(r, False)
        self.assertIn("sans sauvegarde valide", p.stderr.decode())

    def test_power_cut_with_two_v8_kernels_restores_then_rebuilds_the_same_older_exact_kernel(self):
        """initramfs8 = copie du v8 ANCIEN ; coupure pendant la copie (initramfs8 tronqué, ne correspond plus à rien) :
        le prochain install.sh rétablit d'abord, PUIS choisit le noyau — jamais le v8 le plus récent par défaut."""
        old = "6.6.31+rpt-rpi-v8"
        cases = (("journal", None), ("journal-autre-v8", KV), ("journal-invalide", "../../etc"),
                 ("journal-sans-noyau", ""), ("uninstall", None))
        for label, txn_kernel in cases:
            r = self.rootfs(kernels=(old, KV, KV5))
            src = self.read(r, "boot/initrd.img-" + old)
            with open(os.path.join(r, INITRD), "wb") as f:
                f.write(src)
            newest = (self.read(r, INITRD_IMG), self.read(r, SHA1_STATE))
            calls = self.popen_killed([INSTALL, "--root", r, "--skip-preflight"], OTV_KILL_UPDATE="1")
            self.assertEqual([c.split()[2:5] for c in self.updates(calls)], [["-u", "-k", old]], label)
            self.assertEqual(len(self.read(r, INITRD)), 7, label)                # tronqué : aucune correspondance
            self.assertIn("kernel=%s\n" % old, slurp(os.path.join(r, TXN)), label)
            if txn_kernel is not None:                                       # noyau du journal : revérifié, jamais cru
                body = slurp(os.path.join(r, TXN))
                with open(os.path.join(r, TXN), "w") as f:
                    f.write(body.replace("kernel=" + old, "kernel=" + txn_kernel))
            if label == "journal":                                           # dry-run : noyau noté annoncé, rien écrit
                p, dcalls = self.run_sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
                self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
                self.assertIn("update-initramfs -u -k " + old + " ", p.stdout.decode())
                self.assertEqual(self.updates(dcalls), [])
                self.assertEqual(len(self.read(r, INITRD)), 7)
            if label == "uninstall":
                p, calls = self.uninstall(r)
                self.assertIn("transaction interrompue rejouée", p.stdout.decode())
            else:
                p, calls = self.install(r)
                self.assertIn("transaction interrompue rejouée", p.stdout.decode(), label)
                self.assertIn("régénération du noyau " + old + " ", p.stdout.decode(), label)
                self.assertIn(b"plymouth/themes/opentv/opentv.script", self.read(r, INITRD), label)
            # le MÊME noyau ancien exact est régénéré ; le v8 le plus récent n'est jamais touché
            self.assertEqual([c.split()[2:5] for c in self.updates(calls)], [["-u", "-k", old]], (label, calls))
            self.assertEqual(self.read(r, INITRD), self.read(r, "boot/initrd.img-" + old), label)
            self.assertEqual((self.read(r, INITRD_IMG), self.read(r, SHA1_STATE)), newest, label)
            self.assertEqual(self.read(r, "boot/firmware/initramfs_2712"), ("INITRD-" + KV5 + "\n").encode())
            self.assertFalse(self.exists(r, TXN), label)
            if label == "journal-invalide":
                self.assertIn("noyau du journal ignoré", p.stderr.decode())
            if label == "journal":                                           # désinstallation : même noyau exact
                p, calls = self.uninstall(r, True, "--purge")
                self.assertEqual([c.split()[2:5] for c in self.updates(calls)], [["-u", "-k", old]])
                self.assertNotIn(b"plymouth/themes/opentv", self.read(r, INITRD))
                self.assertEqual(self.read(r, INITRD_IMG), newest[0])

    def test_exact_v8_kernel_is_selected_never_all(self):
        old = "6.6.31+rpt-rpi-v8"
        # initramfs8 = copie du noyau v8 ANCIEN : c'est lui qui est régénéré (correspondance octet pour octet)
        r = self.rootfs(kernels=(old, KV, KV5))
        with open(os.path.join(r, "boot/initrd.img-" + old), "rb") as f:
            data = f.read()
        with open(os.path.join(r, INITRD), "wb") as f:
            f.write(data)
        p, _ = self.run_sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        self.assertIn("update-initramfs -u -k " + old + " ", p.stdout.decode())
        # aucune correspondance : le plus récent noyau v8 (kernel8.img chargé par le firmware)
        with open(os.path.join(r, INITRD), "wb") as f:
            f.write(b"autre\n")
        p, _ = self.run_sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        self.assertIn("update-initramfs -u -k " + KV + " ", p.stdout.decode())
        self.assertNotIn("-k all", p.stdout.decode())
        # régénération réelle (racine de test) : seul le noyau choisi, initramfs_2712 et l'autre v8 intacts
        r = self.rootfs(kernels=(old, KV, KV5))
        other = self.read(r, "boot/initrd.img-" + old)
        _, calls = self.install(r)
        self.assertEqual([c.split()[2:5] for c in self.updates(calls)], [["-u", "-k", KV]])
        self.assertEqual(self.read(r, "boot/initrd.img-" + old), other)
        self.assertEqual(self.read(r, "boot/firmware/initramfs_2712"), ("INITRD-" + KV5 + "\n").encode())


class TestHandoffUnitsAndScripts(Harness):
    HANDOFF_UNITS = ("opentv-plymouth-deactivate.service", "opentv-plymouth-quit.service",
                     "opentv-plymouth-failsafe.service")
    TTY_KEYS = ("TTYPath", "TTYReset", "TTYVHangup", "TTYVTDisallocate", "TTYRows", "TTYColumns")

    def test_kiosk_takes_over_plymouth_quit_without_tty_commands_or_waiting(self):
        k = unit(os.path.join(PI4, "systemd", "opentv-kiosk.service"))
        u, s = k["Unit"], k["Service"]
        conflicts, after, wants = u["Conflicts"].split(), u["After"].split(), u["Wants"].split()
        self.assertIn("plymouth-quit.service", conflicts)                   # pas de « plymouth quit » ordinaire
        self.assertIn("getty@tty1.service", conflicts)
        for a in ("plymouth-quit.service", "plymouth-start.service", "opentv.service", "getty@tty1.service",
                  "opentv-plymouth-deactivate.service"):
            self.assertIn(a, after)
        for w in ("opentv-plymouth-deactivate.service", "opentv-plymouth-quit.service"):
            self.assertIn(w, wants)
        self.assertNotIn("opentv-plymouth-quit.service", after)               # le quit attend le kiosque, pas l'inverse
        self.assertNotIn("plymouth-quit-wait.service", after)                # attendrait ce kiosque : interblocage
        for key in ("Requires", "Requisite", "BindsTo"):
            self.assertNotIn("plymouth", u.get(key, ""), key)                 # aucune dépendance dure à Plymouth
        # secours sûr : jamais OnFailure=plymouth-quit.service (en conflit : il arrêterait le kiosque)
        self.assertEqual(u["OnFailure"], "opentv-plymouth-failsafe.service")
        for key in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost", "ExecReload", "ExecCondition"):
            self.assertNotIn(key, s, key)                                       # aucune commande sous réglages TTY
        self.assertEqual(s["ExecStart"], "/usr/bin/cage -d -- /opt/opentv/bin/opentv-kiosk-browser")
        self.assertEqual((s["TTYReset"], s["TTYVHangup"], s["TTYVTDisallocate"]), ("no", "no", "no"))
        self.assertEqual(s["Restart"], "always")
        sp = unit(os.path.join(PI4, "systemd", "opentv-splash.service"))
        self.assertIn("plymouth-start.service", sp["Unit"]["After"].split())
        self.assertNotIn("plymouth-start.service", sp["Unit"].get("Requires", ""))
        self.assertEqual(sp["Install"]["WantedBy"], "sysinit.target")

    def test_handoff_units_are_bounded_root_oneshots_without_any_tty_setting(self):
        bounds = {"opentv-plymouth-deactivate.service": ("deactivate", 12), "opentv-plymouth-quit.service": ("quit", 45),
                  "opentv-plymouth-failsafe.service": ("failsafe", 30)}
        for name, (mode, tmax) in bounds.items():
            cp = unit(os.path.join(PI4, "systemd", name))
            u, s = cp["Unit"], cp["Service"]
            self.assertEqual(s["Type"], "oneshot", name)
            self.assertEqual(s["ExecStart"], "/usr/bin/bash /opt/opentv/bin/opentv-plymouth-handoff " + mode, name)
            self.assertNotIn("User", s, name)                                   # root : Plymouth n'accepte que root
            for key in self.TTY_KEYS:
                self.assertNotIn(key, s, (name, key))
            self.assertEqual(s["StandardInput"], "null", name)
            self.assertNotIn("tty", s.get("StandardOutput", "") + s.get("StandardError", ""), name)
            self.assertLessEqual(int(s["TimeoutStartSec"]), tmax, name)        # échéance dure
            self.assertNotIn("Install", cp.sections(), name)                   # jamais activée seule
            self.assertNotIn("RemainAfterExit", s, name)                       # rejouée (sans effet) à chaque relance
            for key in ("After", "Requires", "Wants", "BindsTo"):
                for bad in ("plymouth-quit-wait.service", "multi-user.target", "graphical.target", "getty.target",
                            "getty@tty1.service"):
                    self.assertNotIn(bad, u.get(key, "").split(), (name, key, bad))   # aucun interblocage
        self.assertIn("opentv-kiosk.service", unit(os.path.join(PI4, "systemd", self.HANDOFF_UNITS[0]))["Unit"]["Before"].split())
        q = unit(os.path.join(PI4, "systemd", "opentv-plymouth-quit.service"))["Unit"]
        self.assertIn("opentv-kiosk.service", q["After"].split())
        self.assertEqual(q["OnFailure"], "opentv-plymouth-failsafe.service")
        t = unit(os.path.join(PI4, "systemd", "opentv-plymouth-failsafe.timer"))
        self.assertEqual(t["Timer"]["Unit"], "opentv-plymouth-failsafe.service")
        self.assertEqual(t["Timer"]["OnBootSec"], "120s")
        self.assertEqual(t["Install"]["WantedBy"], "timers.target")

    def test_no_unit_runs_side_commands_under_destructive_tty_settings(self):
        """Régression systemd 252 : réglages TTY appliqués à CHAQUE commande ⇒ jamais ExecStartPre/Post avec eux."""
        side = ("ExecStartPre", "ExecStartPost", "ExecCondition", "ExecReload", "ExecStop", "ExecStopPost")
        d = os.path.join(PI4, "systemd")
        seen = 0
        for name in sorted(os.listdir(d)):
            cp = unit(os.path.join(d, name))
            if "Service" not in cp:
                continue
            s = cp["Service"]
            destructive = [k for k in ("TTYReset", "TTYVHangup", "TTYVTDisallocate") if s.get(k, "no") != "no"]
            if destructive:
                for k in side:
                    self.assertNotIn(k, s, (name, destructive, k))
            if "TTYPath" in s or s.get("StandardInput", "").startswith("tty"):
                seen += 1
                self.assertEqual((s.get("TTYReset", "no"), s.get("TTYVHangup", "no")), ("no", "no"), name)
                for k in ("ExecStartPre", "ExecStartPost"):
                    self.assertNotIn(k, s, (name, k))
            for key in ("After", "Requires", "Wants", "BindsTo", "Requisite"):
                self.assertNotIn("plymouth-quit-wait.service", cp["Unit"].get(key, "").split(), (name, key))
        self.assertGreaterEqual(seen, 2)                                       # kiosque + écran texte contrôlés

    def test_install_and_uninstall_wire_every_handoff_unit(self):
        with open(INSTALL) as f:
            inst = f.read()
        with open(UNINSTALL) as f:
            unin = f.read()
        enable = re.search(r"post systemctl enable ((?:[^\n]*\\\n)*[^\n]*)", inst).group(1)
        self.assertIn("opentv-plymouth-failsafe.timer", enable)
        for u in self.HANDOFF_UNITS:
            self.assertNotIn(u, enable)                                         # tirées par le kiosque seulement
        disable = re.search(r"host systemctl disable --now ((?:[^\n]*\\\n)*[^\n]*)", unin).group(1)
        for u in self.HANDOFF_UNITS + ("opentv-plymouth-failsafe.timer",):
            self.assertIn(u, disable)
        r = self.rootfs()
        self.install(r)
        man = self.read(r, "opt/opentv/MANIFEST.sha256").decode()
        for name in os.listdir(os.path.join(PI4, "systemd")):
            self.assertIn("  /etc/systemd/system/%s\n" % name, man)
        self.uninstall(r, True, "--purge")
        for name in os.listdir(os.path.join(PI4, "systemd")):
            self.assertFalse(self.exists(r, "etc/systemd/system/" + name), name)
        self.assertFalse(self.exists(r, "opt/opentv"))

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze absent (macOS)")
    def test_systemd_analyze_verify_kiosk(self):
        units = [os.path.join(PI4, "systemd", n) for n in ("opentv-kiosk.service",) + self.HANDOFF_UNITS +
                 ("opentv-plymouth-failsafe.timer",)]
        p = subprocess.run(["systemd-analyze", "verify", "--man=no"] + units, capture_output=True, timeout=60)
        bad = [l for l in p.stderr.decode().splitlines() if "opentv" in l and "not found" not in l.lower()
               and "No such file" not in l]
        self.assertEqual(bad, [])

    def splash_env(self, cmdline, pid="4242", comm="plymouthd\n"):
        d = self.tmp()
        with open(os.path.join(d, "cmdline"), "w") as f:
            f.write(cmdline)
        if pid is not None:
            with open(os.path.join(d, "pid"), "w") as f:
                f.write(pid + "\n")
        if comm is not None:
            os.makedirs(os.path.join(d, "proc", "4242"))
            with open(os.path.join(d, "proc", "4242", "comm"), "w") as f:
                f.write(comm)
        return dict(os.environ, OPENTV_PROC_CMDLINE=os.path.join(d, "cmdline"),
                    OPENTV_PLYMOUTH_PIDFILE=os.path.join(d, "pid"), OPENTV_PROC_ROOT=os.path.join(d, "proc"))

    def test_text_splash_never_writes_over_a_running_plymouth_splash(self):
        line = PI_CMDLINE.rstrip("\n") + " quiet splash plymouth.ignore-serial-consoles\n"
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            p = subprocess.run([bash, SPLASH], stdin=subprocess.PIPE, capture_output=True, timeout=10,
                               env=self.splash_env(line))
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout, b"", bash)                              # rien sur tty1 : aucun texte
            self.assertIn("plymouth actif", p.stderr.decode())                 # preuve au journal (boot-check)
            # repli : Plymouth absent, nosplash, démon mort/étranger, fichier pid corrompu ⇒ écran texte OpenTV
            for env in (self.splash_env(PI_CMDLINE), self.splash_env(line.replace(" splash", " nosplash")),
                        self.splash_env(line.replace(" splash", " splash plymouth.enable=0")),
                        self.splash_env(line.replace(" splash", " splash plymouth.enable=false")),
                        self.splash_env(line.replace(" splash", " splash plymouth.enable=No")),
                        self.splash_env(line.replace(" splash", " splash rd.plymouth=0")),
                        self.splash_env(line, comm="bash\n"), self.splash_env(line, comm=None),
                        self.splash_env(line, pid="12ab"), self.splash_env(line, pid=None)):
                p = subprocess.run([bash, SPLASH], stdin=subprocess.PIPE, capture_output=True, timeout=10, env=env)
                self.assertEqual(p.returncode, 0)
                self.assertIn("OpenTV", p.stdout.decode())
                self.assertEqual(p.stderr, b"")

    FAKE_PLY = r'''#!/bin/sh
printf '%s\n' "$*" >> "$OTV_PLY_LOG"
case "$1" in
  --ping) [ -f "$OTV_PLY_ALIVE" ] ;;
  quit) [ "${OTV_PLY_STUCK:-0}" = 1 ] || rm -f "$OTV_PLY_ALIVE"; exit "${OTV_PLY_RC:-0}" ;;
  *) exit "${OTV_PLY_RC:-0}" ;;
esac
'''
    FAKE_SYSTEMCTL = r'''#!/bin/sh
printf '%s\n' "$*" >> "$OTV_SC_LOG"
[ "$1" = show ] || exit 1
p="${2#--property=}"
cat "$OTV_SC_DIR/$p" 2>/dev/null || echo
'''

    def handoff_env(self, running=True, main_pid="777", state="active", wait="5", stuck=False, rc="0", test=True):
        d = self.tmp()
        for name, body in (("plymouth", self.FAKE_PLY), ("systemctl", self.FAKE_SYSTEMCTL)):
            with open(os.path.join(d, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(d, name), 0o755)
        sc = os.path.join(d, "sc")
        os.makedirs(sc)
        for k, v in (("MainPID", main_pid), ("ActiveState", state)):
            with open(os.path.join(sc, k), "w") as f:
                f.write(v + "\n")
        alive = os.path.join(d, "alive")
        if running:
            open(alive, "w").close()
        # sysfs : card0 = v3d (rendu seul), card1 = vc4-drm (affichage), connecteur card1-HDMI-A-1
        sysfs = os.path.join(d, "sys")
        for node, drv in (("card0", "v3d"), ("card1", "vc4-drm"), ("card1-HDMI-A-1", "vc4-drm"), ("renderD128", "v3d")):
            os.makedirs(os.path.join(sysfs, "class/drm", node, "device"))
            os.symlink("../../../../bus/platform/drivers/" + drv, os.path.join(sysfs, "class/drm", node, "device/driver"))
        proc = os.path.join(d, "proc")
        os.makedirs(os.path.join(proc, "777", "fd"))
        os.symlink("/dev/null", os.path.join(proc, "777", "fd", "0"))
        env = dict(os.environ, OTV_PLY_LOG=os.path.join(d, "log"), OTV_SC_LOG=os.path.join(d, "sclog"),
                   OTV_SC_DIR=sc, OTV_PLY_ALIVE=alive, OTV_PLY_RC=rc, OTV_PLY_STUCK="1" if stuck else "0",
                   OPENTV_PLYMOUTH_BIN=os.path.join(d, "plymouth"), OPENTV_SYSTEMCTL_BIN=os.path.join(d, "systemctl"),
                   OPENTV_PROC_ROOT=proc, OPENTV_SYS_ROOT=sysfs, OPENTV_HANDOFF_WAIT_S=wait)
        if test:
            env["OPENTV_HANDOFF_TEST"] = "1"
        return d, env

    def handoff(self, mode, env, bash="bash", actions=()):
        """actions : [(délai, fonction)] exécutées pendant que le script attend (ex. Cage ouvre un nœud DRM)."""
        sp = subprocess.Popen([bash, HANDOFF, mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t0 = time.monotonic()
        marks = []
        for delay, fn in actions:
            time.sleep(delay)
            fn()
            marks.append(time.monotonic())
        out, err = sp.communicate(timeout=60)
        done = time.monotonic()
        d = os.path.dirname(env["OTV_PLY_LOG"])
        log = slurp(env["OTV_PLY_LOG"]).splitlines() if os.path.exists(env["OTV_PLY_LOG"]) else []
        sclog = slurp(env["OTV_SC_LOG"]).splitlines() if os.path.exists(env["OTV_SC_LOG"]) else []
        self.assertEqual(out, b"")
        return sp.returncode, err.decode(), log, sclog, done - t0, (done - marks[-1] if marks else None), d

    def fd(self, env, n, target):
        return lambda: os.symlink(target, os.path.join(env["OPENTV_PROC_ROOT"], "777", "fd", str(n)))

    def test_handoff_quits_with_retain_splash_only_after_kiosk_main_pid_holds_the_vc4_node(self):
        for bash in sorted({"/bin/bash", shutil.which("bash")} - {None}):
            _, env = self.handoff_env()
            rc, err, log, _, _, _, _ = self.handoff("deactivate", env, bash)
            self.assertEqual((rc, log), (0, ["--ping", "deactivate"]), bash)
            # Cage ouvre d'abord card0 (v3d, rendu seul) : refusé ; puis card1 (vc4-drm) : retain-splash
            _, env = self.handoff_env(wait="20")
            rc, err, log, sclog, _, after, _ = self.handoff(
                "quit", env, bash, actions=((0.6, self.fd(env, 5, "/dev/dri/card0")),
                                            (0.8, self.fd(env, 9, "/dev/dri/card1"))))
            self.assertEqual(rc, 0, err)
            self.assertEqual(log[0], "--ping")
            self.assertEqual(log[1:], ["quit --retain-splash"], bash)          # jamais un « quit » ordinaire
            self.assertIn("DRM vc4 /dev/dri/card1 tenu par le kiosque, PID 777", err)
            self.assertNotIn("card0", err)
            self.assertLess(after, 2.0)
            self.assertTrue(sclog)
            self.assertEqual(set(sclog), {"show --property=MainPID --value opentv-kiosk.service"})   # argv fixe

    def test_handoff_falls_back_to_normal_quit_when_kiosk_skipped_failed_or_late(self):
        for state in ("inactive", "failed"):                                   # condition non remplie / échec
            _, env = self.handoff_env(main_pid="0", state=state, wait="20")
            rc, err, log, sclog, took, _, _ = self.handoff("quit", env)
            self.assertEqual((rc, log), (0, ["--ping", "quit", "--ping"]), state)
            self.assertIn("secours : plymouth quit ordinaire", err)
            self.assertIn("kiosque " + state, err)
            self.assertLess(took, 5, state)                                     # immédiat, pas 20 s
            self.assertIn("show --property=ActiveState --value opentv-kiosk.service", sclog)
        # kiosque qui redémarre sans jamais tenir vc4 (seulement v3d) : délai borné puis quit ordinaire
        _, env = self.handoff_env(wait="2")
        os.symlink("/dev/dri/card0", os.path.join(env["OPENTV_PROC_ROOT"], "777", "fd", "5"))
        rc, err, log, _, took, _, _ = self.handoff("quit", env)
        self.assertEqual((rc, log), (0, ["--ping", "quit", "--ping"]))
        self.assertIn("non tenu par le kiosque après 2 s", err)
        self.assertLess(took, 6)
        # Plymouth qui survit au secours : code 1 (OnFailure= déclenche le secours suivant)
        _, env = self.handoff_env(main_pid="0", state="failed", stuck=True)
        rc, err, log, _, _, _, _ = self.handoff("quit", env)
        self.assertEqual(rc, 1)
        self.assertIn("Plymouth encore actif", err)
        # quit --retain-splash refusé par le démon : secours ordinaire
        _, env = self.handoff_env(rc="1")
        os.symlink("/dev/dri/card1", os.path.join(env["OPENTV_PROC_ROOT"], "777", "fd", "9"))
        rc, err, log, _, _, _, _ = self.handoff("quit", env)
        self.assertEqual(log[:3], ["--ping", "quit --retain-splash", "quit"])

    def test_failsafe_retains_splash_only_for_a_displayed_kiosk_otherwise_normal_quit(self):
        _, env = self.handoff_env()
        os.symlink("/dev/dri/card1", os.path.join(env["OPENTV_PROC_ROOT"], "777", "fd", "9"))
        rc, err, log, _, _, _, _ = self.handoff("failsafe", env)
        self.assertEqual((rc, log), (0, ["--ping", "quit --retain-splash"]))
        self.assertIn("secours tardif", err)
        _, env = self.handoff_env(main_pid="0", state="inactive")
        rc, err, log, _, took, _, _ = self.handoff("failsafe", env)
        self.assertEqual((rc, log), (0, ["--ping", "quit", "--ping"]))
        self.assertIn("secours :", err)
        self.assertLess(took, 5)

    def test_handoff_is_a_bounded_noop_without_plymouth_and_ignores_env_outside_tests(self):
        for mode in ("deactivate", "quit", "failsafe"):
            _, env = self.handoff_env(running=False)
            rc, err, log, sclog, _, _, _ = self.handoff(mode, env)
            self.assertEqual((rc, log, sclog), (0, ["--ping"], []), mode)       # kiosque redémarré : rien
        _, env = self.handoff_env(rc="1")
        rc, err, log, _, _, _, _ = self.handoff("deactivate", env)
        self.assertEqual(rc, 0)
        self.assertIn("échec ignoré", err)
        _, env = self.handoff_env()
        p = subprocess.run(["bash", HANDOFF, "bogus"], env=env, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 2)
        # sans OPENTV_HANDOFF_TEST=1 : chemins FIXES (/usr/bin/plymouth), les variables OPENTV_* sont ignorées
        _, env = self.handoff_env(test=False)
        p = subprocess.run(["bash", HANDOFF, "quit"], env=env, capture_output=True, timeout=20)
        self.assertFalse(os.path.exists(env["OTV_PLY_LOG"]))                   # faux plymouth jamais appelé
        self.assertFalse(os.path.exists(env["OTV_SC_LOG"]))
        if not os.path.exists("/usr/bin/plymouth"):
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        with open(HANDOFF) as f:
            code = f.read()
        self.assertIn('if [ "${EUID:-0}" -ne 0 ] && [ "${OPENTV_HANDOFF_TEST:-}" = 1 ]; then', code)
        for line in code.splitlines():                                          # aucune lecture d'env hors du bloc
            if "OPENTV_" in line and not line.lstrip().startswith("#"):
                self.assertTrue(line.startswith("  ") or "OPENTV_HANDOFF_TEST" in line, line)

    def test_boot_scripts_parse_under_bash32_and_shellcheck(self):
        for s in (HANDOFF, SPLASH, BOOT_CHECK, INSTALL, UNINSTALL, PREPARE, IRLIB):
            p = subprocess.run(["/bin/bash", "-n", s], capture_output=True)
            self.assertEqual(p.returncode, 0, (s, p.stderr))
        if shutil.which("shellcheck"):
            p = subprocess.run(["shellcheck", "-x", HANDOFF, SPLASH, BOOT_CHECK, IRLIB], capture_output=True)
            self.assertEqual(p.returncode, 0, p.stdout.decode())

    def test_prepare_installs_plymouth_package_only_and_install_never_runs_apt(self):
        r = fixture_root(self)
        p = subprocess.run(["bash", PREPARE, "--root", r], capture_output=True, timeout=60,
                           env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(p.returncode, 0, p.stderr)
        line = [l for l in p.stdout.decode().splitlines() if "apt-get install" in l][0]
        self.assertIn(" plymouth", line)
        self.assertIn("plymouth-themes", line)                                 # fournit label.so (prompts du thème)
        with open(INSTALL) as f:
            code = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("apt-get", code)
        self.assertNotIn("apt install", code)


class TestThemeAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.png = load_png_tool()

    def chunks(self, data):
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        pos, out = 8, []
        while pos < len(data):
            n, typ = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + n]
            crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
            self.assertEqual(zlib.crc32(typ + body) & 0xffffffff, crc, typ)
            out.append((typ, body))
            pos += 12 + n
        self.assertEqual(out[-1][0], b"IEND")
        return out

    def test_png_assets_are_real_1920_wide_rgb_images(self):
        for name, h in (("opentv.png", 1080), ("opentv-mask.png", 96)):
            with open(os.path.join(THEME_SRC, name), "rb") as f:
                data = f.read()
            ch = self.chunks(data)
            self.assertEqual(ch[0][0], b"IHDR")
            self.assertEqual(struct.unpack(">IIBBBBB", ch[0][1]), (1920, h, 8, 2, 0, 0, 0), name)
            self.assertLess(len(data), 1200 * 1024, name)                      # initramfs raisonnable
            w, hh, rgb = self.png.png_decode(data)
            self.assertEqual((w, hh, len(rgb)), (1920, h, 1920 * h * 3))

    def px(self, rgb, x, y, w=1920):
        i = (y * w + x) * 3
        return rgb[i], rgb[i + 1], rgb[i + 2]

    def test_composition_dark_background_centered_mark_and_label_band(self):
        with open(os.path.join(THEME_SRC, "opentv.png"), "rb") as f:
            _, _, rgb = self.png.png_decode(f.read())
        luma = lambda p: 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]   # noqa: E731
        for x, y in ((10, 10), (1909, 10), (10, 1069), (1909, 1069), (300, 540), (1620, 540), (960, 100), (960, 980)):
            self.assertLess(luma(self.px(rgb, x, y)), 60, (x, y))             # fond sombre partout hors marque
        mark = [self.px(rgb, x, y) for y in range(470, 590, 2) for x in range(640, 1280, 2)]
        white = [p for p in mark if min(p) > 200]
        blue = [p for p in mark if p[2] > 200 and p[0] < 150 and p[2] - p[0] > 80]   # « TV » #6ea8ff
        self.assertGreater(len(white), 300)
        self.assertGreater(len(blue), 200)
        xs = [x for y in range(470, 590, 2) for x in range(0, 1920, 2) if max(self.px(rgb, x, y)) > 200]
        self.assertLess(abs((min(xs) + max(xs)) / 2 - 960), 40)              # marque centrée
        label = [self.px(rgb, x, y) for y in range(640, 692) for x in range(800, 1120, 2)]
        self.assertTrue(any(luma(p) > 120 for p in label))                    # « Démarrage… » visible
        self.assertFalse(any(luma(self.px(rgb, x, y)) > 120 for y in range(720, 1080, 4) for x in range(0, 1920, 8)))

    def test_mask_matches_background_without_label(self):
        with open(os.path.join(THEME_SRC, "opentv.png"), "rb") as f:
            _, _, full = self.png.png_decode(f.read())
        with open(os.path.join(THEME_SRC, "opentv-mask.png"), "rb") as f:
            _, _, mask = self.png.png_decode(f.read())
        y0 = self.png.MASK_Y
        diff = [abs(a - b) for y in range(0, 96, 3) for x in list(range(0, 700, 7)) + list(range(1220, 1920, 7))
                for a, b in zip(self.px(mask, x, y), self.px(full, x, y0 + y))]
        self.assertLess(sum(diff) / len(diff), 2.0)                           # même fond hors libellé
        self.assertFalse(any(min(self.px(mask, x, y)) > 150 for y in range(0, 96, 2) for x in range(800, 1120, 2)))

    def test_theme_files_consistent_static_and_without_system_text(self):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read(os.path.join(THEME_SRC, "opentv.plymouth"))
        self.assertEqual(cp["Plymouth Theme"]["ModuleName"], "script")
        self.assertEqual(cp["script"]["ImageDir"], "/" + THEME_DIR)
        self.assertEqual(cp["script"]["ScriptFile"], "/" + THEME_DIR + "/opentv.script")
        with open(os.path.join(THEME_SRC, "opentv.script"), "rb") as f:
            raw = f.read()
        raw.decode("ascii")                                                    # analyseur Plymouth : ASCII seul
        s = raw.decode()
        code = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
        for c, v in (("IMG_W", 1920), ("IMG_H", 1080), ("MASK_Y", self.png.MASK_Y), ("MASK_H", self.png.MASK_H)):
            self.assertIn("%s = %d;" % (c, v), code)
        for bad in ("SetMessageFunction", "SetUpdateStatusFunction", "SetRefreshFunction", "SetBootProgressFunction",
                    "Raspberry", "Linux", "Debian", "http", "login"):
            self.assertNotIn(bad, code, bad)                                   # statique, aucun texte système
        self.assertIn('Image("opentv.png")', code)
        self.assertIn('Image("opentv-mask.png")', code)
        self.assertIn('Plymouth.GetMode() != "boot"', code)


QUIT_OK_SECOURS = ("opentv-plymouth-handoff: secours : plymouth quit ordinaire, console texte rendue (kiosque failed "
                   "(condition non remplie, arrêté ou en échec))")


class TestBootCheckPlymouth(Harness):
    QUIT_OK = ("opentv-plymouth-handoff: plymouth : terminé sans retour au texte (retain-splash ; DRM vc4 /dev/dri/card1 "
               "tenu par le kiosque, PID 812)")

    def fake(self, quit_journal=QUIT_OK, failsafe_journal="opentv-plymouth-handoff: plymouth : déjà terminé",
             kiosk_journal="Started opentv-kiosk.service", splash_journal="opentv-splash: plymouth actif, écran texte non écrit",
             running=False, initrd_lines=("usr/share/plymouth/themes/opentv/opentv.script",), props_extra=None):
        d = self.tmp()
        props = {("opentv-splash.service", "UnitFileState"): "enabled", ("opentv-splash.service", "Result"): "success",
                 ("opentv-splash.service", "ExecMainStatus"): "0",
                 ("opentv-splash.service", "ExecMainStartTimestampMonotonic"): "4200000",
                 ("opentv-kiosk.service", "ExecMainStartTimestampMonotonic"): "9100000",
                 ("getty@tty1.service", "ActiveState"): "inactive", ("getty@tty1.service", "ConditionResult"): "no",
                 ("getty@tty1.service", "DropInPaths"): "/etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf",
                 ("opentv-kiosk.service", "ActiveState"): "active", ("opentv-kiosk.service", "NRestarts"): "0",
                 ("opentv-kiosk.service", "TTYReset"): "no", ("opentv-kiosk.service", "TTYVHangup"): "no",
                 ("opentv-kiosk.service", "TTYVTDisallocate"): "no",
                 ("ssh.service", "ActiveState"): "active"}
        props.update(props_extra or {})
        with open(os.path.join(d, "props"), "w") as f:
            for (u, k), v in props.items():
                f.write("%s %s=%s\n" % (u, k, v))
        with open(os.path.join(d, "initrd"), "w") as f:
            f.write("\n".join(initrd_lines) + "\n")
        journals = {"splash": splash_journal, "kiosk": kiosk_journal, "quit": quit_journal, "failsafe": failsafe_journal}
        for k, v in journals.items():
            with open(os.path.join(d, "j-" + k), "w") as f:
                f.write(v + "\n")
        scripts = {
            "systemctl": '#!/bin/sh\n[ "$1" = show ] || exit 0\ngrep -F "$5 $3=" "%s" | head -n 1 | cut -d= -f2-\n'
                         % os.path.join(d, "props"),
            "journalctl": ('#!/bin/sh\nJ="%s"\nhit=0\n'
                           'case " $* " in *" -k "*) i=0; while [ $i -lt 80 ]; do echo "l $i"; i=$((i+1)); done; exit 0 ;; esac\n'
                           'case " $* " in *" -u opentv-splash.service "*) cat "$J/j-splash"; hit=1 ;; esac\n'
                           'case " $* " in *" -u opentv-kiosk.service "*) cat "$J/j-kiosk"; hit=1 ;; esac\n'
                           'case " $* " in *" -u opentv-plymouth-quit.service "*) cat "$J/j-quit"; hit=1 ;; esac\n'
                           'case " $* " in *" -u opentv-plymouth-failsafe.service "*) cat "$J/j-failsafe"; hit=1 ;; esac\n'
                           '[ "$hit" = 1 ] || echo x\n') % d,
            "timeout": ('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n'
                        'while :; do case "$1" in -k|-s) shift 2 ;; -*) shift ;; *) break ;; esac; done\n'
                        'shift\nexec "$@"\n') % os.path.join(d, "timeouts"),
            "lsinitramfs": '#!/bin/sh\ncat "%s"\n' % os.path.join(d, "initrd"),
            "plymouth": '#!/bin/sh\n[ "$1" = --ping ] && exit %d\nexit 0\n' % (0 if running else 1),
        }
        for n, body in scripts.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(body)
            os.chmod(os.path.join(d, n), 0o755)
        return d

    GOOD = (PI_CMDLINE.rstrip("\n") + " video=HDMI-A-1:1920x1080@60 quiet loglevel=3 logo.nologo "
            "vt.global_cursor_default=0 systemd.show_status=false splash plymouth.ignore-serial-consoles\n")

    def check(self, d, cmdline=GOOD, config=PI_CONFIG + "disable_splash=1\n", conf="[Daemon]\nTheme=opentv\n"):
        for n, body in (("cmdline", cmdline), ("config.txt", config), ("plymouthd.conf", conf)):
            with open(os.path.join(d, n), "w") as f:
                f.write(body)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], LC_ALL="C",
                   OPENTV_PROC_CMDLINE=os.path.join(d, "cmdline"), OPENTV_CONFIG_TXT=os.path.join(d, "config.txt"),
                   OPENTV_PLYMOUTHD_CONF=os.path.join(d, "plymouthd.conf"), OPENTV_INITRAMFS=os.path.join(d, "x"))
        p = subprocess.run(["/bin/bash", BOOT_CHECK], capture_output=True, timeout=30, env=env)
        return p.returncode, p.stdout.decode()

    def test_boot_check_accepts_full_plymouth_chain(self):
        rc, out = self.check(self.fake())
        self.assertEqual(rc, 0, out)
        for s in ("arc-en-ciel désactivé", "thème opentv sélectionné", "consoles série ignorées",
                  "initramfs : thème opentv présent", "écran texte NON écrit", "sans retour au texte (retain-splash, DRM vc4",
                  "aucun secours Plymouth", "Plymouth terminé", "NRestarts=0", "aucun SIGHUP",
                  "TTYReset=no TTYVHangup=no TTYVTDisallocate=no", "aucun ExecStartPre/ExecStartPost"):
            self.assertTrue([l for l in out.splitlines() if s in l][0].startswith("OK     "), s)

    def test_boot_check_reports_each_plymouth_failure(self):
        cases = (
            (dict(), dict(config=PI_CONFIG), "disable_splash=1 absent"),
            (dict(), dict(config=PI_CONFIG + "disable_splash=0\n"), "réglé localement"),
            (dict(), dict(conf="[Daemon]\nTheme=pix\n"), "thème « pix » au lieu de opentv"),
            (dict(), dict(cmdline=self.GOOD.replace(" plymouth.ignore-serial-consoles", "")), "console série sans"),
            (dict(initrd_lines=("usr/sbin/plymouthd",)), dict(), "thème opentv absent de"),
            (dict(splash_journal="x"), dict(), "écran texte écrit alors que Plymouth"),
            (dict(quit_journal="x"), dict(), "passage Plymouth -> kiosque non prouvé"),
            (dict(quit_journal="opentv-plymouth-handoff: plymouth : terminé sans retour au texte (retain-splash ; x)"),
             dict(), "passage Plymouth -> kiosque non prouvé"),                   # retain sans preuve vc4
            (dict(quit_journal=QUIT_OK_SECOURS), dict(), "Plymouth terminé par le SECOURS"),
            (dict(failsafe_journal="opentv-plymouth-handoff: secours : plymouth quit ordinaire, console texte rendue (x)"),
             dict(), "Plymouth terminé par le SECOURS"),
            (dict(props_extra={("opentv-kiosk.service", "NRestarts"): "7"}), dict(), "7 redémarrage(s) depuis le boot"),
            (dict(kiosk_journal="opentv-kiosk.service: Main process exited, code=killed, status=1/HUP"), dict(),
             "Cage tué par SIGHUP"),
            (dict(props_extra={("opentv-kiosk.service", "TTYVHangup"): "yes"}), dict(), "TTYReset/TTYVHangup/TTYVTDisallocate"),
            (dict(props_extra={("opentv-kiosk.service", "ExecStartPre"): "{ path=/usr/bin/bash ; argv[]=x }"}), dict(),
             "ExecStartPre/ExecStartPost présents"),
            (dict(running=True), dict(), "Plymouth encore actif"),
        )
        for fk, ck, msg in cases:
            rc, out = self.check(self.fake(**fk), **ck)
            self.assertEqual(rc, 1, (msg, out))
            self.assertIn("ÉCHEC", [l for l in out.splitlines() if msg in l][0], msg)

    def test_boot_check_initramfs_advice_is_exact_kernel_and_read_is_bounded_like_install(self):
        d = self.fake(initrd_lines=("usr/sbin/plymouthd",))
        with open(os.path.join(d, "uname"), "w") as f:
            f.write("#!/bin/sh\necho %s\n" % KV)
        os.chmod(os.path.join(d, "uname"), 0o755)
        rc, out = self.check(d)
        self.assertEqual(rc, 1, out)
        line = [l for l in out.splitlines() if "thème opentv absent de" in l][0]
        self.assertNotIn("-k all", out)
        self.assertIn("sudo ./install.sh", line)
        self.assertIn("update-initramfs -u -k " + KV + ")", line)                # noyau exact (uname -r)
        bounds = [l for l in slurp(os.path.join(d, "timeouts")).splitlines() if "lsinitramfs" in l]
        self.assertEqual(bounds, ["-k 10 300 lsinitramfs " + os.path.join(d, "x")])   # même borne qu'install.sh
        with open(os.path.join(PI4, "system", "opentv-initramfs.sh")) as f:
            self.assertIn("IR_LS_S=300 ", f.read())
        with open(os.path.join(d, "timeout"), "w") as f:                     # lecture au-delà de la borne
            f.write('#!/bin/sh\ncase "$*" in *lsinitramfs*) exit 124 ;; esac\nshift\nexec "$@"\n')
        rc, out = self.check(d)
        self.assertEqual(rc, 1, out)
        self.assertIn("ÉCHEC", [l for l in out.splitlines() if "au-delà de 300 s" in l][0])

    def test_boot_check_text_fallback_without_splash(self):
        line = self.GOOD.replace(" splash plymouth.ignore-serial-consoles", "")
        rc, out = self.check(self.fake(splash_journal="x", quit_journal="x"), cmdline=line, conf="")
        self.assertEqual(rc, 0, out)
        self.assertIn("repli écran texte tty1", out)
        for refuse in ("plymouth.enable=false", "plymouth.enable=no", "nosplash"):   # refus local : repli, pas d'échec
            rc, out = self.check(self.fake(splash_journal="x", quit_journal="x"),
                                 cmdline=self.GOOD.rstrip("\n") + " " + refuse + "\n", conf="")
            self.assertEqual(rc, 0, (refuse, out))
            self.assertIn("Plymouth désactivé localement", out)


if __name__ == "__main__":
    unittest.main()
