"""OpenTV Canvas (Phase 2, tâche 5) : paquet, cycle de vie, bac à sable des unités, promotion et documentation.

Couvre : dossier d'état /var/lib/opentv/canvas créé par tmpfiles (opentv:opentv 0700, jamais un fichier masqué ni
écrit par root), mise à jour qui garde la scène octet pour octet, retour arrière d'une installation interrompue (fichiers
Canvas, unités, tmpfiles) sans toucher l'état, désinstallation qui garde la scène et --purge qui l'efface sans suivre de
lien, état Canvas inaccessible à toute unité qui n'en a pas besoin (seul opentv.service l'écrit ; le kiosque passe par
l'API), promotion de « Regarder » invalidée par toute livraison Canvas, documentation de passation, et les retouches
mineures de la tâche 4 (« Aucun accès » explicite au QR, texte de conséquence véridique, message store_unavailable).
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from tests._common import PI4, ROOT, NANOPI
from tests.test_service import make_app
from tests import test_canvas_prototype as tcp
from opentv_pi import (VERSION, agents, app as appmod, canvas as cv, canvas_tools, config, health, mcp, promotion,
                       web)

INSTALL = os.path.join(PI4, "install.sh")
UNINSTALL = os.path.join(PI4, "uninstall.sh")
SYSTEMD = os.path.join(PI4, "systemd")
CANVAS_DIR = "/var/lib/opentv/canvas"
HANDOFF = os.path.join(ROOT, "docs", "opentv-canvas.md")
# seul service qui lit/écrit l'état Canvas ; le kiosque (Chromium) n'y accède que par l'API /api/tv/canvas/*
CANVAS_WRITERS = {"opentv.service"}
# unité sans espace de noms (Cage + PAM sur tty1, setuid de Chromium) : l'état reste protégé par les droits Unix
# (compte opentv-kiosk hors du groupe opentv, /var/lib/opentv et /var/lib/opentv/canvas en 0700 opentv)
DAC_ONLY = {"opentv-kiosk.service": "opentv-kiosk"}


def sh(args, **kw):
    env = dict(os.environ, LC_ALL="C")
    env.pop("OPENTV_ROOT", None)
    return subprocess.run(["bash"] + list(args), capture_output=True, timeout=300, env=env, **kw)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def sha(path):
    return hashlib.sha256(read(path, "rb")).hexdigest()


def tree(root):
    out = []
    for d, dirs, files in os.walk(root):
        for f in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def unit_text(name):
    return read(os.path.join(SYSTEMD, name))


def values(text, key):
    """Toutes les valeurs (répétées) d'une clé d'unité, commentaires exclus."""
    return " ".join(re.findall(r"^%s=(.+)$" % re.escape(key), text, re.M)).split()


def services():
    return sorted(n for n in os.listdir(SYSTEMD) if n.endswith(".service"))


def covers(paths, target):
    return any(target == p or target.startswith(p.rstrip("/") + "/") for p in paths)


def tmpfiles_lines():
    out = []
    for line in read(os.path.join(PI4, "system", "tmpfiles-opentv.conf")).splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            out.append(tuple(line.split()))
    return out


class Root(unittest.TestCase):
    def root(self):
        r = os.path.realpath(tempfile.mkdtemp(prefix="otvcvpkg", dir="/tmp"))
        self.addCleanup(shutil.rmtree, r, True)
        return r

    def install(self, r, ok=True, script=INSTALL):
        p = sh([script, "--root", r, "--skip-preflight"])
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout.decode()[-3000:] + p.stderr.decode()[-3000:])
        return p

    def uninstall(self, r, *extra):
        p = sh([UNINSTALL, "--root", r] + list(extra))
        self.assertEqual(p.returncode, 0, p.stdout.decode()[-3000:] + p.stderr.decode()[-3000:])
        return p

    def seed_canvas(self, r):
        """Scène réelle du produit (magasin de production) avec historique, dans la racine de test."""
        state = os.path.join(r, "var/lib/opentv")
        os.makedirs(state, exist_ok=True)
        os.chmod(state, 0o700)
        store = cv.CanvasStore(os.path.join(state, cv.DIR_NAME))
        owner = store.owner_api()
        owner.apply({"preset": "cinema"}, confirm=True)
        owner.apply({"motion": "alive"}, confirm=True)
        self.assertEqual(sorted(os.listdir(os.path.join(state, cv.DIR_NAME))), ["state.json", "state.json.lock"])
        return self.canvas_snapshot(r)

    def canvas_snapshot(self, r):
        d = os.path.join(r, CANVAS_DIR.lstrip("/"))
        out = {"": stat.S_IMODE(os.lstat(d).st_mode)}
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            st = os.lstat(p)
            body = os.readlink(p) if stat.S_ISLNK(st.st_mode) else read(p, "rb")
            out[n] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_nlink, body)
        return out


# --------------------------------------------------------------------------- dossier d'état : tmpfiles
class TestStateDirectory(unittest.TestCase):
    def test_tmpfiles_creates_only_the_service_owned_canvas_directory(self):
        lines = tmpfiles_lines()
        mine = [l for l in lines if l[1] == CANVAS_DIR or l[1].startswith(CANVAS_DIR + "/")]
        self.assertEqual(mine, [("d", CANVAS_DIR, "0700", "opentv", "opentv", "-")])   # aucun âge : jamais nettoyé
        paths = [l[1] for l in lines]
        self.assertLess(paths.index("/var/lib/opentv"), paths.index(CANVAS_DIR))   # parent d'abord
        self.assertIn(("d", "/var/lib/opentv", "0700", "opentv", "opentv", "-"), lines)
        # jamais un état, un verrou ou un lien posé par root : le service crée son verrou à son nom (O_EXCL)
        self.assertFalse([l for l in lines if l[0][0] not in "d" and l[1].startswith(CANVAS_DIR)])

    def test_config_names_the_canvas_directory_and_the_store_uses_it(self):
        c = config.Config({})
        self.assertEqual(c.canvas_dir, CANVAS_DIR)
        self.assertEqual(c.canvas_dir, os.path.join(c.state_dir, cv.DIR_NAME))
        d = os.path.realpath(tempfile.mkdtemp(prefix="otvcvcfg"))
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        c = config.Config({"OPENTV_STATE_DIR": d})
        self.assertEqual(c.canvas_dir, os.path.join(d, "canvas"))
        c.canvas_dir = os.path.join(d, "ailleurs")   # le magasin du produit suit Config, jamais un chemin recopié
        store = cv.store_for(c)
        store.owner_api().apply({"preset": "mist"}, confirm=True)
        self.assertTrue(os.path.isfile(os.path.join(d, "ailleurs", "state.json")))
        self.assertFalse(os.path.exists(os.path.join(d, "canvas")))

    def test_service_creates_its_lock_in_a_tmpfiles_style_empty_directory(self):
        """Comme sur la Pi : dossier VIDE préexistant (tmpfiles, 0700, au compte du service) ⇒ le service y crée son
        verrou et sa scène ; un dossier au mauvais mode n'est jamais « réparé » (fail-closed)."""
        d = os.path.realpath(tempfile.mkdtemp(prefix="otvcvtmpf"))
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        os.mkdir(os.path.join(d, "canvas"), 0o700)
        store = cv.CanvasStore(os.path.join(d, "canvas"))
        store.owner_api().apply({"preset": "cinema"}, confirm=True)
        self.assertEqual(sorted(os.listdir(os.path.join(d, "canvas"))), ["state.json", "state.json.lock"])
        os.chmod(os.path.join(d, "canvas"), 0o750)
        with self.assertRaises(cv.CanvasError):
            cv.CanvasStore(os.path.join(d, "canvas")).owner_api().apply({"preset": "mist"}, confirm=True)


# --------------------------------------------------------------------------- bac à sable : qui voit l'état Canvas
class TestUnitsKeepCanvasStateInaccessible(unittest.TestCase):
    def test_only_the_main_service_reaches_canvas_state(self):
        seen = set()
        for name in services():
            text = unit_text(name)
            code = re.sub(r"^#.*$", "", text, flags=re.M)
            inacc = values(code, "InaccessiblePaths")
            masks = [p[1:] for p in inacc]
            self.assertTrue(all(p.startswith("-/") for p in inacc), (name, inacc))   # absent : jamais d'échec
            if name in CANVAS_WRITERS:
                self.assertFalse(covers(masks, CANVAS_DIR), name)
                self.assertTrue(covers([p.lstrip("-") for p in values(code, "ReadWritePaths")], CANVAS_DIR), name)
                self.assertEqual(values(code, "User"), ["opentv"])
            elif name in DAC_ONLY:
                self.assertEqual(values(code, "User"), [DAC_ONLY[name]], name)
                self.assertNotIn("opentv", values(code, "SupplementaryGroups"))
                self.assertNotIn("opentv", values(code, "Group"))
            else:
                self.assertTrue(covers(masks, CANVAS_DIR), "%s : état Canvas non masqué" % name)
            seen.add(name)
        self.assertIn("opentv-catalogue-ratings-retry.service", seen)
        self.assertEqual(set(DAC_ONLY) | CANVAS_WRITERS <= seen, True)

    def test_kiosk_reaches_canvas_only_through_the_api(self):
        """Le kiosque n'a pas d'espace de noms : son compte n'est dans aucun groupe donnant accès à /var/lib/opentv
        (0700 opentv) ; seule l'API TV (clé + UID du kiosque) touche à l'ambiance."""
        sysusers = read(os.path.join(PI4, "system", "sysusers-opentv.conf"))
        self.assertFalse(re.search(r"^m\s+opentv-kiosk\s+opentv\s*$", sysusers, re.M))
        for line in tmpfiles_lines():
            if line[1] in ("/var/lib/opentv", CANVAS_DIR):
                self.assertEqual(line[2:5], ("0700", "opentv", "opentv"), line)
        self.assertIn("canvas.js", web.TV_STATIC)

    def test_masks_name_the_directory_never_a_state_file(self):
        """Un masque de FICHIER est défait par un remplacement atomique et sauté s'il manque au démarrage : seul le
        dossier (toujours présent grâce à tmpfiles) est masqué, jamais state.json, son verrou ou un temporaire."""
        for name in os.listdir(SYSTEMD):
            text = unit_text(name)
            self.assertNotIn(CANVAS_DIR + "/", text, name)
            for m in values(re.sub(r"^#.*$", "", text, flags=re.M), "InaccessiblePaths"):
                self.assertNotRegex(m, r"state\.json", name)

    def test_catalogue_units_mask_canvas_like_every_non_catalogue_state(self):
        c = config.Config({})
        for name in ("opentv-catalogue-refresh.service", "opentv-catalogue-ratings.service",
                     "opentv-catalogue-ratings-retry.service"):
            masks = [p[1:] for p in values(unit_text(name), "InaccessiblePaths")]
            self.assertIn(c.canvas_dir, masks, name)
            self.assertEqual(values(unit_text(name), "ReadWritePaths"), ["-" + c.state_dir])

    def test_root_helpers_writing_state_still_mask_canvas(self):
        for name in ("opentv-net-helper.service", "opentv-muse-setup.service"):
            text = unit_text(name)
            self.assertTrue(covers([p.lstrip("-") for p in values(text, "ReadWritePaths")], "/var/lib/opentv"))
            self.assertIn("-" + CANVAS_DIR, values(text, "InaccessiblePaths"), name)


# --------------------------------------------------------------------------- installation et cycle de vie
class TestInstallLifecycle(Root):
    def test_dry_run_plans_canvas_files_and_state_directory(self):
        r = self.root()
        p = sh([INSTALL, "--dry-run", "--root", r, "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(tree(r), [])
        out = p.stdout.decode()
        for m in sorted(tcp.PRODUCTION_CANVAS_MODULES):
            self.assertIn("/opt/opentv/lib/opentv_pi/%s.py" % m, out)
        self.assertIn("/opt/opentv/share/ui/canvas.js", out)
        self.assertIn("systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf", out)
        self.assertNotIn("prototypes/canvas", out)

    def test_install_ships_reviewed_canvas_files_with_exact_modes_and_manifest(self):
        r = self.root()
        out = self.install(r).stdout.decode()
        man = read(os.path.join(r, "opt/opentv/MANIFEST.sha256"))
        ship = [("opentv_pi/%s.py" % m, "/opt/opentv/lib/opentv_pi/%s.py" % m) for m in tcp.PRODUCTION_CANVAS_MODULES]
        ship += [("ui/canvas.js", "/opt/opentv/share/ui/canvas.js"), ("system/tmpfiles-opentv.conf",
                                                                      "/etc/tmpfiles.d/opentv.conf")]
        for src, dst in ship:
            full = os.path.join(r, dst.lstrip("/"))
            self.assertEqual(sha(full), sha(os.path.join(PI4, src)), dst)
            self.assertEqual(stat.S_IMODE(os.lstat(full).st_mode), 0o644, dst)
            self.assertIn("%s  %s\n" % (sha(full), dst), man)
        self.assertTrue(health.verify_manifest(r)["ok"])
        self.assertNotIn("canvas_model", man)
        # l'état n'est jamais créé ni listé par la copie : tmpfiles (opentv:opentv 0700) le crée sur la vraie racine
        self.assertFalse(os.path.exists(os.path.join(r, CANVAS_DIR.lstrip("/"))))
        self.assertNotIn("/var/lib/opentv", man)
        self.assertIn("systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf", out)
        self.assertLess(out.index("systemd-tmpfiles --create"), out.index("systemctl restart"))   # avant tout service

    def test_upgrade_preserves_seeded_scene_byte_for_byte(self):
        r = self.root()
        self.install(r)
        before = self.seed_canvas(r)
        # livraison précédente différente (Canvas, unité, tmpfiles) + fichier obsolète de l'ancien manifeste
        for rel in ("opt/opentv/share/ui/canvas.js", "opt/opentv/lib/opentv_pi/canvas_tv.py",
                    "etc/systemd/system/opentv.service", "etc/tmpfiles.d/opentv.conf"):
            with open(os.path.join(r, rel), "a") as f:
                f.write("\n# ancienne livraison\n")
        old = os.path.join(r, "opt/opentv/lib/opentv_pi/canvas_old.py")
        with open(old, "w") as f:
            f.write("# module retiré\n")
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256"), "a") as f:
            f.write("%s  /opt/opentv/lib/opentv_pi/canvas_old.py\n" % sha(old))
        out = self.install(r).stdout.decode()
        self.assertIn("retiré (absent du nouveau manifeste) : /opt/opentv/lib/opentv_pi/canvas_old.py", out)
        self.assertEqual(self.canvas_snapshot(r), before)
        self.assertTrue(health.verify_manifest(r)["ok"])
        self.assertNotIn(CANVAS_DIR, out)                      # aucune commande ne vise l'état
        status = cv.CanvasStore(os.path.join(r, CANVAS_DIR.lstrip("/"))).status()
        self.assertEqual((status["state"], status["scene"]["wallpaper"], status["scene"]["motion"]),
                         ("applied", "affiche", "alive"))

    def test_injected_failure_rolls_back_canvas_files_units_and_keeps_state(self):
        r = self.root()
        self.install(r)
        before_state = self.seed_canvas(r)
        for rel in ("opt/opentv/share/ui/canvas.js", "opt/opentv/lib/opentv_pi/canvas_tools.py",
                    "etc/systemd/system/opentv-catalogue-refresh.service", "etc/tmpfiles.d/opentv.conf"):
            with open(os.path.join(r, rel), "a") as f:
                f.write("\n# livraison précédente\n")
        os.remove(os.path.join(r, "opt/opentv/lib/opentv_pi/canvas_storage.py"))   # nouveau pour cette livraison
        os.remove(os.path.join(r, "etc/opentv/opentv.env"))                          # l'étape env échouera
        snap = {rel: read(os.path.join(r, rel), "rb") for rel in tree(r)
                if not rel.startswith("var/lib/opentv-install/") and os.path.isfile(os.path.join(r, rel))}
        os.chmod(os.path.join(r, "etc/opentv"), 0o555)
        self.addCleanup(os.chmod, os.path.join(r, "etc/opentv"), 0o755)
        p = self.install(r, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("retour à l'état précédent", p.stderr.decode())
        os.chmod(os.path.join(r, "etc/opentv"), 0o755)
        after = {rel: read(os.path.join(r, rel), "rb") for rel in tree(r)
                 if not rel.startswith("var/lib/opentv-install/") and os.path.isfile(os.path.join(r, rel))}
        self.assertEqual(after, snap)
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv/lib/opentv_pi/canvas_storage.py")))
        self.assertEqual(self.canvas_snapshot(r), before_state)

    def test_uninstall_keeps_scene_and_purge_removes_it_without_following_links(self):
        r = self.root()
        self.install(r)
        before = self.seed_canvas(r)
        outside = os.path.join(r, "home/alex")
        os.makedirs(outside)
        with open(os.path.join(outside, "precieux.txt"), "w") as f:
            f.write("à garder\n")
        os.symlink(outside, os.path.join(r, CANVAS_DIR.lstrip("/"), "lien-dossier"))
        os.symlink(os.path.join(outside, "precieux.txt"), os.path.join(r, CANVAS_DIR.lstrip("/"), "lien-fichier"))
        out = self.uninstall(r).stdout.decode()
        self.assertIn("ambiance OpenTV conservée (%s : " % CANVAS_DIR, out)
        snap = self.canvas_snapshot(r)
        for k in ("lien-dossier", "lien-fichier"):
            snap.pop(k)
        self.assertEqual(snap, before)
        self.assertFalse(os.path.exists(os.path.join(r, "opt/opentv")))
        out = self.uninstall(r, "--purge").stdout.decode()
        self.assertFalse(os.path.lexists(os.path.join(r, CANVAS_DIR.lstrip("/"))))
        self.assertFalse(os.path.lexists(os.path.join(r, "var/lib/opentv")))
        self.assertEqual(read(os.path.join(outside, "precieux.txt")), "à garder\n")   # liens jamais suivis
        self.assertNotIn("ambiance OpenTV conservée", out)

    def test_purge_of_a_symlinked_canvas_directory_never_touches_its_target(self):
        r = self.root()
        self.install(r)
        os.makedirs(os.path.join(r, "var/lib/opentv"))
        target = os.path.join(r, "srv/autre")
        os.makedirs(target)
        with open(os.path.join(target, "state.json"), "w") as f:
            f.write("{}\n")
        os.symlink(target, os.path.join(r, CANVAS_DIR.lstrip("/")))
        self.uninstall(r, "--purge")
        self.assertFalse(os.path.lexists(os.path.join(r, "var/lib/opentv")))
        self.assertEqual(read(os.path.join(target, "state.json")), "{}\n")


# --------------------------------------------------------------------------- « Regarder » : promotion invalidée
def copy_repo(dst):
    """Arborescence minimale qu'install.sh lit (scripts/pi4, résolveurs NanoPi, qrcode.js), hors tests/prototypes."""
    shutil.copytree(PI4, os.path.join(dst, "scripts/pi4"), symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "prototypes", ".artifacts"))
    os.makedirs(os.path.join(dst, "scripts/nanopi"))
    for n in ("opentv_foryou.py", "opentv_bridge.py", "opentv_tmdb.py"):
        shutil.copy2(os.path.join(NANOPI, n), os.path.join(dst, "scripts/nanopi", n))
    os.makedirs(os.path.join(dst, "native/app"))
    shutil.copy2(os.path.join(ROOT, "native/app/qrcode.js"), os.path.join(dst, "native/app/qrcode.js"))
    return os.path.join(dst, "scripts/pi4/install.sh")


class TestPlaybackPromotionInvalidation(Root):
    def test_canvas_files_are_required_in_the_promoted_delivery(self):
        req = set(promotion.REQUIRED)
        self.assertIn("ui/canvas.js", req)
        for m in tcp.PRODUCTION_CANVAS_MODULES:
            self.assertIn("opentv_pi/%s.py" % m, req)

    def promote(self, r):
        man = os.path.join(r, "opt/opentv/MANIFEST.sha256")
        marker = os.path.join(r, "etc/opentv/playback-promoted")
        ok = (0, os.geteuid())
        with open(marker, "w") as f:
            json.dump(promotion.document(man, uid_ok=ok, root=r), f)
        os.chmod(marker, 0o644)
        return lambda **kw: promotion.check(marker, uid_ok=ok, manifest=man, root=r, **kw)

    def test_any_installed_canvas_change_makes_promotion_stale(self):
        r = self.root()
        self.install(r)
        check = self.promote(r)
        self.assertEqual(check(), "promoted")
        rels = ["opt/opentv/share/ui/canvas.js"] + \
            ["opt/opentv/lib/opentv_pi/%s.py" % m for m in sorted(tcp.PRODUCTION_CANVAS_MODULES)] + \
            ["etc/tmpfiles.d/opentv.conf", "etc/systemd/system/opentv-catalogue-refresh.service"]
        for rel in rels:
            full = os.path.join(r, rel)
            orig = read(full, "rb")
            with open(full, "ab") as f:
                f.write(b"\n# changement\n")
            self.assertEqual(check(), "stale", rel)
            with open(full, "wb") as f:
                f.write(orig)
            self.assertEqual(check(), "promoted", rel)
        self.assertEqual(check(version="0.0.0"), "stale")                  # liée à la version livrée

    def test_manifest_without_canvas_js_is_an_incomplete_delivery(self):
        r = self.root()
        self.install(r)
        check = self.promote(r)
        man = os.path.join(r, "opt/opentv/MANIFEST.sha256")
        lines = read(man).splitlines()
        with open(man, "w") as f:
            f.write("\n".join(x for x in lines if not x.endswith("/share/ui/canvas.js")) + "\n")
        self.assertEqual(check(), "stale")
        self.assertIsNone(promotion.code_digest(man, uid_ok=(0, os.geteuid()), root=r))

    def test_new_delivery_of_a_tracked_canvas_file_invalidates_promotion(self):
        """Vraie nouvelle livraison (install.sh d'une copie du dépôt où seul ui/canvas.js change) : manifeste
        régénéré et cohérent, mais empreinte de livraison différente ⇒ « stale » jusqu'à une nouvelle promotion."""
        r = self.root()
        self.install(r)
        check = self.promote(r)
        self.assertEqual(check(), "promoted")
        repo = self.root()
        script = copy_repo(repo)
        with open(os.path.join(repo, "scripts/pi4/ui/canvas.js"), "a") as f:
            f.write("\n/* livraison suivante */\n")
        out = self.install(r, script=script).stdout.decode()
        self.assertTrue(health.verify_manifest(r)["ok"])
        self.assertEqual(check(), "stale")
        self.assertIn("rend caduc", out)


# --------------------------------------------------------------------------- documentation de passation
class TestDocumentation(unittest.TestCase):
    def readme_canvas(self):
        text = read(os.path.join(PI4, "README.md"))
        m = re.search(r"^## 6 quinquies\. OpenTV Canvas.*?(?=^## )", text, re.M | re.S)
        self.assertIsNotNone(m, "section README « 6 quinquies. OpenTV Canvas » absente")
        return m.group(0)

    def test_readme_documents_owner_flow_permission_safety_recovery_budget_and_field_checks(self):
        s = self.readme_canvas()
        for need in ("Réglages → Ambiance OpenTV", "« Non »", "Oui, appliquer", "Version précédente",
                     "Ambiance d'origine", web.CANVAS_LABEL, "Gérer les assistants", "canvas_status",
                     "canvas_preview", "canvas_cancel", "1 proposition / 5 s", "20 / 10 min",
                     "Réduire les animations", "prefers-reduced-motion", "Coupé", "dégradé", CANVAS_DIR,
                     "opentv:opentv 0700", "--purge", "get_throttled", "hvs_underrun", "1920×1080",
                     "playback promote --field-validated", "Budget", "quatre couches", "transform/opacity"):
            self.assertIn(need, s, need)
        for preset in cv.PRESETS:
            self.assertIn("`%s`" % preset, s)

    def test_handoff_doc_covers_schema_modules_tools_routes_and_field_gates(self):
        text = read(HANDOFF)
        for f in cv.FIELDS:
            self.assertIn("`%s`" % f, text, f)
        for field, vals in cv.ENUMS.items():
            for v in vals:
                self.assertIn(v, text, (field, v))
        for m in sorted(tcp.PRODUCTION_CANVAS_MODULES):
            self.assertIn("opentv_pi/%s.py" % m, text, m)
        self.assertIn("ui/canvas.js", text)
        for t in canvas_tools.TOOLS:
            self.assertIn("`%s`" % t["name"], text)
        for route in ("event", "ui", "confirm", "cancel", "rollback", "reset", "apply"):
            self.assertIn("/api/tv/canvas/%s" % route, text, route)
        for need in ("Gates terrain", "get_throttled", "hvs_underrun", "playback promote --field-validated",
                     CANVAS_DIR, "InaccessiblePaths", "REQUIRED", "0.7.2"):
            self.assertIn(need, text, need)
        agents_md = read(os.path.join(ROOT, "AGENTS.md"))
        self.assertIn("docs/opentv-canvas.md", agents_md)

    def test_phase2_plan_is_part_of_the_delivery_docs(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs/plans/2026-10-30-opentv-canvas-phase2.md")))


# --------------------------------------------------------------------------- retouches de la tâche 4
class TestQrExplicitNone(unittest.TestCase):
    def reconfigure(self, policy, issue, form):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        a.registry.set_policy(policy)
        toks = {k: a.registry.issue(k, v) for k, v in issue.items()}
        a.request_setup()
        a.tick()
        a.net = {"mode": "ethernet", "ip": "192.168.2.40", "wifi_present": False, "nm_ok": True}
        st = a.tv_state()["setup"]["token"]
        code, kind, _ = a.setup_submit(dict({"t": st, "name": "Salon", "net": "keep"}, **form))
        self.assertEqual((code, kind), (200, "done"))
        return a, toks

    def test_explicit_none_clears_level_and_canvas_and_revokes_the_identity(self):
        a, toks = self.reconfigure({"muse": ["canvas", "feedback", "read"], "hermit": ["canvas", "read"],
                                    "hermes": ["canvas", "read"]},
                                   {"hermit": ["canvas", "read"], "hermes": ["canvas", "read"]},
                                   {"agent_hermit": "none", "agent_hermes": "read"})   # Muse : case décochée
        self.assertEqual(a.registry.policy(), {"muse": [], "hermit": [], "hermes": ["canvas", "read"]})
        self.assertIsNone(a.registry.authenticate("hermit", toks["hermit"]))
        self.assertEqual(a.registry.authenticate("hermes", toks["hermes"]), ["canvas", "read"])

    def test_plain_wifi_change_keeps_canvas_even_for_a_canvas_only_agent(self):
        a, toks = self.reconfigure({"muse": ["canvas", "feedback", "read"], "hermit": ["canvas"], "hermes": []},
                                   {"hermit": ["canvas"]},
                                   {"agent_muse": "feedback", "agent_hermit": "none", "agent_hermes": "none"})
        self.assertEqual(a.registry.policy(), {"muse": ["canvas", "feedback", "read"], "hermit": ["canvas"],
                                               "hermes": []})
        self.assertEqual(a.registry.authenticate("hermit", toks["hermit"]), ["canvas"])

    def test_registry_rule_is_applied_under_the_lock(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="otvcvreg"))
        self.addCleanup(shutil.rmtree, d, True)
        reg = agents.Registry(os.path.join(d, "agents.json"))
        reg.set_policy({"muse": ["canvas", "read"], "hermit": ["canvas"], "hermes": ["canvas", "read"]})
        reg.set_policy({"muse": [], "hermit": [], "hermes": ["display", "feedback", "read"]},
                       preserve=("canvas",))   # muse : lecture -> aucun accès (explicite) ; hermit : inchangé
        self.assertEqual(reg.policy(), {"muse": [], "hermit": ["canvas"],
                                        "hermes": ["canvas", "display", "feedback", "read"]})


class TestTruthfulConsequence(unittest.TestCase):
    def row(self, have, allowed):
        return {"name": "Hermit", "connected": True, "connected_level": appmod.level_of(have),
                "client": appmod.level_of(have), "bridge": None, "have": sorted(have), "allowed": sorted(allowed)}

    def test_none_with_canvas_kept_never_claims_a_disconnection(self):
        x = self.row(["canvas", "read"], ["canvas", "read"])
        cons = web._consequence(x, "none", canvas_on=True)
        self.assertNotIn("déconnecté", cons)
        self.assertIn("reste connecté", cons)
        self.assertIn("ambiance", cons)
        self.assertIn("déconnecté", web._consequence(x, "none", canvas_on=False))
        # identité sans la permission facultative : elle n'a plus aucun droit ⇒ vraiment révoquée
        y = self.row(["read"], ["canvas", "read"])
        self.assertIn("déconnecté", web._consequence(y, "none", canvas_on=True))
        self.assertEqual(web._consequence(x, "none"), web._consequence(x, "none", canvas_on=False))

    def test_confirm_page_matches_what_the_registry_write_really_does(self):
        a = make_app(self)
        a.registry.set_policy({"muse": [], "hermit": ["canvas", "read"], "hermes": []})
        tok = a.registry.issue("hermit", ["canvas", "read"])
        rows, fp, _ = a._assistants_read()
        plan = web.agents_plan(rows, {"agent_muse": appmod.AGENT_KEEP, "agent_hermit": "none",
                                      "agent_hermes": appmod.AGENT_KEEP, "canvas_hermit": "on"})
        page = web.agents_confirm_page("t" * 43, "c" * 64, plan, fp)
        li = re.search(r"<li><b>Hermit</b>(.*?)</li>", page, re.S).group(1)
        self.assertNotIn("déconnecté", li)
        self.assertIn("reste connecté", li)
        x = [r for r in rows if r["id"] == "hermit"][0]
        out = a.registry.apply_policy_changes({"hermit": agents.row_scopes(appmod.AGENT_LEVELS["none"], x["allowed"], "on")},
                                              appmod.KNOWN_AGENTS, fp)
        self.assertEqual(out, {"hermit": "restricted"})
        self.assertEqual(a.registry.authenticate("hermit", tok), ["canvas"])


class TestStoreUnavailableMessage(unittest.TestCase):
    def test_every_canvas_error_has_a_truthful_french_message(self):
        codes = set(canvas_tools.CODES.values()) | {"store_unavailable", "canvas_unavailable", "canvas_cooldown",
                                                     "forbidden_scope", "rate_limited", "invalid_params"}
        for c in codes:
            self.assertIn(c, mcp.ERRORS_FR, c)
        msg = mcp.ERRORS_FR["store_unavailable"]
        self.assertIn("indisponible", msg)
        self.assertIn("ambiance", msg)       # aussi émis par les outils Canvas : jamais « historique » seul
        self.assertNotEqual(msg, "historique indisponible")
        self.assertEqual(mcp._tool_error("store_unavailable")["content"][0]["text"],
                         "Erreur OpenTV : %s (store_unavailable)" % msg)


class TestVersion(unittest.TestCase):
    def test_canvas_shipped_in_0_7_2_and_still_present(self):
        # Canvas livré en 0.7.2 ; la version exacte courante (0.7.3, reprise CEC) est vérifiée par test_cec_return
        self.assertGreaterEqual(tuple(int(x) for x in VERSION.split(".")), (0, 7, 2))
        self.assertIn("0.7.2", read(HANDOFF))


if __name__ == "__main__":
    unittest.main()
