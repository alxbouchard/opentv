"""Registre des agents : écritures sérialisées entre processus (service ⇄ CLI root) et refus d'un registre illisible.

Couvre : flock latéral `agents.json.lock` (fichier sûr : régulier, lien unique, propriétaire attendu, 0600, rendu au
compte du service quand root le crée), attente bornée (registry_busy), vrais processus concurrents sans perte
d'écriture, sauvegarde téléphone simultanée à `opentv-pi agent revoke/issue` (aucune résurrection ni perte), registre
illisible jamais remplacé (octets intacts) pour issue / revoke / set_policy / rollback_issue / apply_policy_changes,
configuration QR qui ne bloque jamais le Wi-Fi sur un registre illisible mais annonce honnêtement que les assistants
n'ont pas été enregistrés (téléphone + TV, synchrone et asynchrone), écoute /assistants impossible ⇒ mode fermé
« unavailable » sans nouvel essai, et création SIMULTANÉE du verrou (O_EXCL puis ouverture, essais bornés ⇒
registry_busy ; barrière multiprocessus sur dossiers neufs). Tout processus enfant est récolté par addCleanup, même
après un premier échec d'assertion (aucun ResourceWarning).
"""
import contextlib
import errno
import fcntl
import json
import multiprocessing
import os
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from tests._common import PI4
from tests.test_remote import remote_servers, tmpdir
from tests.test_service import make_app, token_of
from tests.test_review2 import no_path_perms
from tests import test_agents_manage as tam
from opentv_pi import agents, config, fsutil

KNOWN = ("muse", "hermit", "hermes")

# Processus enfant = vrai code CLI (`opentv_pi.cli.main`), avec en option une pause DANS l'écriture du registre
# (donc sous le flock) : « inside » sur stdout, puis attente d'une ligne sur stdin avant d'écrire réellement.
CHILD = textwrap.dedent(r"""
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from opentv_pi import agents, cli
    pause = sys.argv[2] == "pause"
    real = agents.atomic_write_json
    def paused(*a, **kw):
        if pause:
            sys.stdout.write("inside\n"); sys.stdout.flush()
            sys.stdin.readline()
        return real(*a, **kw)
    agents.atomic_write_json = paused
    code = cli.main(sys.argv[3:])
    sys.stdout.flush()
    sys.exit(code)
""")

# N processus écrivent chacun M identités distinctes (issue) puis en retirent la moitié (revoke) en même temps.
WORKER = textwrap.dedent(r"""
    import sys
    sys.path.insert(0, sys.argv[1])
    from opentv_pi import agents
    r = agents.Registry(sys.argv[2], lock_timeout=30)
    w, n = sys.argv[3], int(sys.argv[4])
    for j in range(n):
        r.issue("w%sx%d" % (w, j), ["read"])
    for j in range(0, n, 2):
        r.revoke("w%sx%d" % (w, j))
""")


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def reap(p):
    """Récolte inconditionnelle d'un Popen (cleanup) : tué s'il vit encore, tubes fermés, attendu."""
    if p.poll() is None:
        p.kill()
    for f in (p.stdin, p.stdout, p.stderr):
        if f is not None and not f.closed:
            try:
                f.close()
            except OSError:
                pass
    p.wait(timeout=30)


def popen(tc, *a, **kw):
    p = subprocess.Popen(*a, **kw)
    tc.addCleanup(reap, p)
    return p


class Env:
    def __init__(self, tc):
        self.tc = tc
        self.dir = tmpdir(tc, "otvlock")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state, mode=0o700)
        self.env = os.path.join(self.dir, "opentv.env")
        with open(self.env, "w", encoding="utf-8") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\n" % (self.state, os.path.join(self.dir, "run")))
        self.path = os.path.join(self.state, "agents.json")
        self.script = os.path.join(self.dir, "child.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(CHILD)

    def cli(self, *argv, pause=False):
        """Vraie CLI dans un autre processus (sans privilège : sortie client dans le dossier temporaire)."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENTV_")}
        env["HOME"] = self.dir
        return popen(self.tc, [sys.executable, "-I", self.script, PI4, "pause" if pause else "go", "--env",
                               self.env] + list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, env=env)

    def wait_inside(self, p):
        line = p.stdout.readline().decode().strip()
        self.tc.assertEqual(line, "inside", p.stderr.read().decode()[-2000:] if line == "" else line)

    def finish(self, p, stdin=b""):
        out, err = p.communicate(stdin, timeout=60)
        lines = [x for x in out.decode().splitlines() if x.strip() and x.strip() != "inside"]
        return p.returncode, json.loads("\n".join(lines)) if lines else None, err.decode()


def still_waiting(p, s=0.6):
    """Le processus est-il toujours bloqué (sur le flock) après s secondes ?"""
    time.sleep(s)
    return p.poll() is None


class Pause:
    """Écriture du registre dans CE processus suspendue sous le flock jusqu'à release()."""

    def __init__(self):
        self.inside, self.go, self.real = threading.Event(), threading.Event(), agents.atomic_write_json
        self.armed = True

    def __call__(self, *a, **kw):
        if self.armed:
            self.armed = False
            self.inside.set()
            self.go.wait(30)
        return self.real(*a, **kw)


# --------------------------------------------------------------------------- fichier verrou et attente bornée
class TestLockFile(unittest.TestCase):
    def reg(self, **kw):
        d = tmpdir(self, "otvlk")
        return agents.Registry(os.path.join(d, "agents.json"), **kw), d

    def test_lock_is_a_private_regular_sidecar(self):
        r, d = self.reg()
        r.set_policy({"muse": ["read"]})
        st = os.lstat(os.path.join(d, "agents.json.lock"))
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual((st.st_uid, st.st_nlink, st.st_size), (os.geteuid(), 1, 0))
        os.chmod(os.path.join(d, "agents.json.lock"), 0o666)       # mode élargi par erreur : resserré au passage
        r.revoke("muse")
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(d, "agents.json.lock")).st_mode), 0o600)

    def test_unsafe_lock_paths_refuse_and_write_nothing(self):
        for kind in ("symlink", "fifo", "hardlink", "dir"):
            with self.subTest(kind=kind):
                r, d = self.reg()
                r.set_policy({"muse": ["read"]})
                raw = read_bytes(r.path)
                lock = r.lock_path
                os.unlink(lock)
                victim = os.path.join(d, "victim")
                with open(victim, "w") as f:
                    f.write("secret")
                os.chmod(victim, 0o644)
                if kind == "symlink":
                    os.symlink(victim, lock)
                elif kind == "fifo":
                    os.mkfifo(lock)
                elif kind == "hardlink":
                    os.link(victim, lock)
                else:
                    os.mkdir(lock)
                for call in (lambda: r.issue("muse", ["read"]), lambda: r.revoke("muse"),
                             lambda: r.set_policy({}), lambda: r.rollback_issue("muse", "x" * 43, None),
                             lambda: r.apply_policy_changes({}, KNOWN, r.snapshot(KNOWN)[1])):
                    with self.assertRaises(fsutil.UnsafePath):
                        call()
                self.assertEqual(read_bytes(r.path), raw)
                with open(victim) as f:
                    self.assertEqual(f.read(), "secret")
                self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644)

    def test_root_hands_the_lock_to_the_service_account_by_descriptor(self):
        """Root crée le verrou (dossier d'état du service) : rendu au compte du service par fchown sur le descripteur
        pour que le service puisse toujours l'ouvrir ; un verrou d'un tiers est refusé."""
        r, d = self.reg()
        calls = []

        def fchown(fd, uid, gid):
            st = os.fstat(fd)
            calls.append((stat.S_ISREG(st.st_mode), st.st_ino, uid, gid))
        with mock.patch.object(os, "fchown", fchown), no_path_perms():
            r.set_policy({"muse": ["read"]}, uid=4242, gid=4343)          # uid du service ≠ propriétaire actuel
        ino = os.lstat(r.lock_path).st_ino
        self.assertIn((True, ino, 4242, 4343), calls)
        with mock.patch.object(os, "geteuid", lambda: 999999):          # ni moi ni le service : refusé
            with self.assertRaisesRegex(OSError, "lock_unexpected_owner"):
                r.revoke("muse")

    def test_held_lock_times_out_as_registry_busy_and_writes_nothing(self):
        r, _d = self.reg(lock_timeout=0.3)
        r.set_policy({"muse": ["read"]})
        raw = read_bytes(r.path)
        fd = agents.open_lock(r.lock_path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t0 = time.monotonic()
            with self.assertRaises(agents.RegistryBusy) as cm:
                r.issue("muse", ["read"])
            self.assertEqual(cm.exception.errno, errno.EWOULDBLOCK)
            self.assertLess(time.monotonic() - t0, 2.5)                    # borné, jamais infini
        finally:
            os.close(fd)
        self.assertEqual(read_bytes(r.path), raw)
        self.assertTrue(r.issue("muse", ["read"]))                        # verrou libéré : nouvel essai possible

    def test_failed_write_never_leaves_phantom_state_in_memory(self):
        r, _d = self.reg()
        r.set_policy({})
        raw = read_bytes(r.path)
        with mock.patch.object(agents, "atomic_write_json", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                r.issue("muse", ["read"], force=True)
            with self.assertRaises(OSError):
                r.set_policy({"muse": ["read"]})
        self.assertEqual(r.list(), {})                                    # relu du disque : rien d'émis
        self.assertEqual(r.policy(), {})
        self.assertEqual(read_bytes(r.path), raw)


# --------------------------------------------------------------------------- création du verrou : course bornée
LOCK = "agents.json.lock"


class FakeOpen:
    """os.open simulé pour le SEUL fichier verrou : `script` = liste d'issues pour ses appels successifs
    ("eexist" ⇒ FileExistsError, "enoent" ⇒ FileNotFoundError, "real" ⇒ vrai appel) ; dernière issue répétée."""

    def __init__(self, script):
        self.script, self.calls, self.real = list(script), [], os.open

    def __call__(self, name, flags, mode=0o777, *, dir_fd=None):
        if name != LOCK:
            return self.real(name, flags, mode, dir_fd=dir_fd)
        what = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        self.calls.append(("excl" if flags & os.O_EXCL else "open", bool(flags & os.O_CREAT), what))
        if what == "eexist":
            raise FileExistsError(errno.EEXIST, "exists")
        if what == "enoent":
            raise FileNotFoundError(errno.ENOENT, "gone")
        return self.real(name, flags, mode, dir_fd=dir_fd)


class TestLockOpenRace(unittest.TestCase):
    def reg(self):
        d = tmpdir(self, "otvrace")
        r = agents.Registry(os.path.join(d, "agents.json"))
        r.set_policy({"muse": ["read"]})                                  # verrou présent, registre valide
        return r, d

    def test_vanishing_lock_is_retried_then_opened_without_o_creat(self):
        """EEXIST à la création exclusive puis ENOENT à l'ouverture (supprimé entre les deux), deux fois : nouvel
        essai borné, puis l'ouverture de l'existant (SANS O_CREAT) réussit ; l'écriture a bien lieu."""
        r, _d = self.reg()
        fake = FakeOpen(["eexist", "enoent", "eexist", "enoent", "enoent", "eexist", "real"])
        with mock.patch.object(agents.os, "open", fake):
            self.assertTrue(r.issue("muse", ["read"]))
        # ENOENT de la création exclusive elle-même (course macOS) : nouvel essai aussi, jamais UnsafePath
        self.assertEqual([c[:2] for c in fake.calls], [("excl", True), ("open", False)] * 2 +
                         [("excl", True), ("excl", True), ("open", False)])
        self.assertIn("muse", agents.Registry(r.path).list())

    def test_first_creation_uses_o_excl_and_reports_created(self):
        d = tmpdir(self, "otvrace")
        fake = FakeOpen(["real"])
        with mock.patch.object(agents.os, "open", fake):
            fd = agents.open_lock(os.path.join(d, LOCK))
        os.close(fd)
        self.assertEqual(fake.calls, [("excl", True, "real")])                 # un seul appel : création exclusive
        st = os.lstat(os.path.join(d, LOCK))
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink), (0o600, 1))

    def test_endless_create_delete_race_is_bounded_registry_busy_never_unsafe_path(self):
        r, _d = self.reg()
        raw = read_bytes(r.path)
        for script, per_try in ((["eexist", "enoent"] * 100, 2), (["enoent"], 1)):
            fake = FakeOpen(script)
            with mock.patch.object(agents.os, "open", fake):
                for call in (lambda: r.issue("hermit", ["read"], force=True), lambda: r.revoke("muse"),
                             lambda: agents.open_lock(r.lock_path)):
                    fake.calls.clear()
                    with self.assertRaises(agents.RegistryBusy) as cm:
                        call()
                    self.assertNotIsInstance(cm.exception, fsutil.UnsafePath)
                    self.assertEqual(cm.exception.errno, errno.EWOULDBLOCK)
                    self.assertEqual(len(fake.calls), per_try * agents.LOCK_OPEN_TRIES)   # borné, jamais infini
        self.assertEqual(read_bytes(r.path), raw)                          # rien d'écrit
        self.assertEqual(sorted(agents.Registry(r.path).list()), [])

    def test_cli_reports_exhaustion_as_registry_busy(self):
        """Course de création épuisée dans la vraie CLI : « registry_busy » (réessayable), jamais un chemin refusé."""
        e = Env(self)
        agents.Registry(e.path).set_policy({})
        script = os.path.join(e.dir, "busy.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(r"""
                import errno, os, sys
                sys.path.insert(0, sys.argv[1])
                from opentv_pi import agents, cli
                real = os.open
                def fake(name, flags, mode=0o777, *, dir_fd=None):
                    if name == "agents.json.lock":
                        raise (FileExistsError(errno.EEXIST, "x") if flags & os.O_EXCL else
                               FileNotFoundError(errno.ENOENT, "x"))
                    return real(name, flags, mode, dir_fd=dir_fd)
                agents.os.open = fake
                sys.exit(cli.main(sys.argv[2:]))
            """))
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENTV_")}
        env["HOME"] = e.dir
        p = popen(self, [sys.executable, "-I", script, PI4, "--env", e.env, "agent", "revoke", "--id", "muse"],
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        out, err = p.communicate(timeout=60)
        self.assertEqual((p.returncode, json.loads(out)), (2, {"ok": False, "error": "registry_busy"}), err)

    def test_unsafe_preexisting_entries_still_refused_through_the_exclusive_path(self):
        for kind in ("symlink", "dangling", "fifo", "hardlink", "dir"):
            with self.subTest(kind=kind):
                d = tmpdir(self, "otvrace")
                lock = os.path.join(d, LOCK)
                victim = os.path.join(d, "victim")
                with open(victim, "w") as f:
                    f.write("secret")
                if kind == "symlink":
                    os.symlink(victim, lock)
                elif kind == "dangling":
                    os.symlink(os.path.join(d, "absent"), lock)
                elif kind == "fifo":
                    os.mkfifo(lock)
                elif kind == "hardlink":
                    os.link(victim, lock)
                else:
                    os.mkdir(lock)
                with self.assertRaises(fsutil.UnsafePath):
                    agents.open_lock(lock)
                self.assertFalse(os.path.exists(os.path.join(d, "absent")))  # jamais créé à travers un lien
                with open(victim) as f:
                    self.assertEqual(f.read(), "secret")

    def test_only_the_creator_hands_the_lock_over_and_a_preexisting_root_lock_is_refused(self):
        """Root (uid du service ≠ lui) : le verrou qu'il CRÉE est rendu au service par fchown ; un verrou
        préexistant qui appartient encore à root (ou à un tiers) est refusé, jamais « récupéré » par fchown."""
        d = tmpdir(self, "otvrace")
        lock = os.path.join(d, LOCK)
        me, svc = os.geteuid(), os.geteuid() + 4242
        calls = []
        with mock.patch.object(os, "fchown", lambda fd, u, g: calls.append((u, g))), no_path_perms():
            os.close(agents.open_lock(lock, uid=svc, gid=77))                # créé ici ⇒ fchown
            self.assertEqual(calls, [(svc, 77)])
            # (fchown simulé : le fichier appartient toujours à « root » = moi) ⇒ préexistant inattendu : refusé
            with self.assertRaisesRegex(fsutil.UnsafePath, "lock_unexpected_owner"):
                agents.open_lock(lock, uid=svc, gid=77)
            self.assertEqual(calls, [(svc, 77)])                              # aucun fchown d'un fichier existant
        # préexistant au bon propriétaire (le service) : ouvert tel quel, aucun fchown, mode resserré
        os.chmod(lock, 0o644)
        with mock.patch.object(os, "fchown", lambda fd, u, g: calls.append((u, g))):
            os.close(agents.open_lock(lock, uid=me, gid=77))
        self.assertEqual(calls, [(svc, 77)])
        self.assertEqual(stat.S_IMODE(os.lstat(lock).st_mode), 0o600)

    def test_lock_root_is_still_handing_over_is_awaited_briefly(self):
        """Le service ouvre le verrou que root vient de créer mais pas encore rendu (fchown en cours) : brève
        attente bornée, puis ouverture normale ; propriétaire jamais corrigé ⇒ refus."""
        d = tmpdir(self, "otvrace")
        lock = os.path.join(d, LOCK)
        os.close(agents.open_lock(lock))
        real = os.fstat
        seen = {"n": 0}

        def fstat(fd, flip=3):
            st = real(fd)
            seen["n"] += 1
            if seen["n"] <= flip and stat.S_ISREG(st.st_mode):
                v = list(st[:10])
                v[4] = st.st_uid + 1                                           # encore « à root »
                return os.stat_result(v)
            return st
        with mock.patch.object(agents.os, "fstat", fstat):
            os.close(agents.open_lock(lock))
        self.assertGreater(seen["n"], 3)
        seen["n"] = 0
        with mock.patch.object(agents.os, "fstat", lambda fd: fstat(fd, flip=10 ** 6)):
            t0 = time.monotonic()
            with self.assertRaisesRegex(fsutil.UnsafePath, "lock_unexpected_owner"):
                agents.open_lock(lock)
            self.assertLess(time.monotonic() - t0, 1.0)                        # borné


STRESS_TRIALS = int(os.environ.get("OPENTV_LOCK_STRESS_TRIALS", "30"))
STRESS_WORKERS = int(os.environ.get("OPENTV_LOCK_STRESS_WORKERS", "8"))


class TestLockCreationStress(unittest.TestCase):
    def test_simultaneous_first_creation_behind_a_barrier(self):
        """N vrais processus (spawn) attendent une barrière puis font EN MÊME TEMPS la première écriture d'un dossier
        neuf (création du verrou comprise), répété sur des essais successifs : aucun refus « chemin dangereux » ni
        registry_busy, aucune écriture perdue, un seul fichier verrou sûr. Processus toujours récoltés."""
        from tests import _lockstress
        ctx = multiprocessing.get_context("spawn")
        n = STRESS_WORKERS
        barrier, tasks, results = ctx.Barrier(n), ctx.Queue(), ctx.Queue()
        procs = []

        def stop():
            for _ in procs:
                try:
                    tasks.put(None)
                except (OSError, ValueError):
                    pass
            for p in procs:
                p.join(10)
                if p.is_alive():
                    p.terminate()
                    p.join(5)
                if p.is_alive():
                    p.kill()
                    p.join(5)
                p.close()
            for q in (tasks, results):
                q.close()
                q.cancel_join_thread()
        self.addCleanup(stop)
        for _ in range(n):
            p = ctx.Process(target=_lockstress.worker, args=(barrier, tasks, results), daemon=True)
            p.start()
            procs.append(p)
        base = tmpdir(self, "otvstress")
        for t in range(STRESS_TRIALS):
            d = os.path.join(base, "t%03d" % t)
            os.mkdir(d, 0o700)
            for w in range(n):
                tasks.put((d, w))
            got = dict(results.get(timeout=120) for _ in range(n))
            self.assertEqual(got, {w: "ok" for w in range(n)}, "essai %d" % t)
            self.assertEqual(sorted(agents.Registry(os.path.join(d, "agents.json")).list()),
                             sorted("w%d" % w for w in range(n)), "essai %d" % t)
            st = os.lstat(os.path.join(d, LOCK))
            self.assertTrue(stat.S_ISREG(st.st_mode))
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink, st.st_uid), (0o600, 1, os.geteuid()))
            self.assertEqual(sorted(os.listdir(d)), ["agents.json", LOCK])     # aucun temporaire laissé


# --------------------------------------------------------------------------- QR de configuration : succès partiel
class TestSetupAgentsTruth(unittest.TestCase):
    """Le Wi-Fi et le nom passent, mais si la politique des assistants n'a pas pu être enregistrée, la réponse le dit
    (synchrone « done » comme asynchrone « applying » puis résultat Wi-Fi) : jamais « tout est prêt » à tort."""
    MSG = ("Les assistants n'ont pas été enregistrés : rien n'a changé pour eux. Réessaie depuis Réglages → Gérer "
           "les assistants.")

    def failing(self, a, kind):
        """-> gestionnaire de contexte qui fait échouer l'enregistrement de la politique selon `kind`."""
        if kind == "corrupt":
            with open(a.cfg.registry_file, "wb") as f:
                f.write(b"{pas du json")
            return contextlib.nullcontext()
        if kind == "busy":
            return mock.patch.object(agents, "flock_bounded", side_effect=agents.RegistryBusy())
        return mock.patch.object(agents, "atomic_write_json", side_effect=OSError(errno.ENOSPC, "disque plein"))

    def test_appmod_message_is_the_exact_plain_french_text(self):
        from opentv_pi import app as appmod
        self.assertEqual(appmod.MSG_AGENTS_NOT_SAVED, self.MSG)

    def test_sync_done_reports_agents_not_saved_for_corrupt_busy_and_write_failure(self):
        from tests.test_service import Servers
        for kind, reason in (("corrupt", "unreadable"), ("busy", "busy"), ("write", "write")):
            with self.subTest(kind=kind):
                a = make_app(self)
                s = Servers(self, a)
                before = read_bytes(a.cfg.registry_file) if os.path.exists(a.cfg.registry_file) else None
                with self.failing(a, kind):
                    raw = read_bytes(a.cfg.registry_file) if os.path.exists(a.cfg.registry_file) else before
                    code, _h, body = s.req("POST", "/setup", {"t": token_of(a), "name": "Salon", "net": "keep",
                                                              "agent_muse": "full"}, lan=True)
                self.assertEqual(code, 200, body)
                self.assertIn("C'est prêt", body)
                self.assertIn(html_escape(self.MSG), body)                        # page finale du téléphone
                self.assertNotIn(reason, body)                                    # aucun détail technique
                self.assertNotIn("registry", body)
                self.assertEqual(config.load_device(a.cfg)["name"], "Salon")      # nom appliqué
                st = a.tv_state()["setup"]
                self.assertFalse(st["active"])
                self.assertEqual((st["agents_saved"], st["agents_reason"], st["agents_message"]),
                                 (False, reason, self.MSG))
                if raw is not None:
                    self.assertEqual(read_bytes(a.cfg.registry_file), raw)        # rien n'a changé pour eux
                else:
                    self.assertFalse(os.path.exists(a.cfg.registry_file))

    def test_sync_success_says_nothing_about_agents(self):
        from tests.test_service import Servers
        a = make_app(self)
        s = Servers(self, a)
        out = {}
        code, _h, body = s.req("POST", "/setup", {"t": token_of(a), "name": "Salon", "net": "keep",
                                                  "agent_muse": "read"}, lan=True)
        self.assertEqual(code, 200, body)
        self.assertNotIn("assistants n'ont pas", body)
        st = a.tv_state()["setup"]
        self.assertEqual((st["agents_saved"], st["agents_reason"], st["agents_message"]), (True, None, None))
        self.assertEqual(a.registry.policy()["muse"], ["read"])
        a2 = make_app(self)
        a2.tick()
        self.assertEqual(a2.setup_submit({"t": token_of(a2), "name": "Salon", "net": "keep"}, outcome=out)[:2],
                         (200, "done"))
        self.assertEqual(out, {"agents_saved": True, "agents_reason": None})

    def test_async_applying_then_wifi_result_keeps_the_truth(self):
        from tests.test_hardening import jobs, put_result
        from tests.test_service import Net, ETH_DEV, ETH_IP
        for kind, reason in (("corrupt", "unreadable"), ("busy", "busy"), ("write", "write")):
            with self.subTest(kind=kind):
                a = make_app(self, Net(ETH_DEV, ETH_IP))
                a.tick()
                out = {}
                with self.failing(a, kind):
                    code, k, _ = a.setup_submit({"t": token_of(a), "name": "Salon", "net": "wifi", "ssid": "Maison",
                                                 "psk": "motdepasse-1"}, outcome=out)
                self.assertEqual((code, k), (200, "applying"))
                self.assertEqual(out, {"agents_saved": False, "agents_reason": reason})
                st = a.tv_state()["setup"]
                self.assertEqual((st["active"], st["phase"], st["agents_saved"], st["agents_message"]),
                                 (True, "applying", False, self.MSG))
                put_result(a, jobs(a)[0]["id"], "wifi-apply", True)            # Wi-Fi confirmé plus tard
                a.tick()
                st = a.tv_state()["setup"]
                self.assertEqual((st["active"], st["agents_saved"], st["agents_reason"], st["agents_message"]),
                                 (False, False, reason, self.MSG))
        # page « C'est presque fini » du téléphone (rendu réel)
        from opentv_pi import web
        page = web.setup_result("applying", agents_saved=False)
        self.assertIn(html_escape(self.MSG), page)
        self.assertNotIn(html_escape(self.MSG), web.setup_result("applying"))
        self.assertNotIn(html_escape(self.MSG), web.setup_result("done", "http://x.local:8082/r?p=y"))
        self.assertIn(html_escape(self.MSG), web.setup_result("done", "http://x.local:8082/r?p=y", agents_saved=False))

    def test_a_new_setup_flow_forgets_the_previous_outcome(self):
        a = make_app(self)
        a.tick()
        with self.failing(a, "write"):
            a.setup_submit({"t": token_of(a), "name": "Salon", "net": "keep"})
        self.assertFalse(a.tv_state()["setup"]["agents_saved"])
        a.request_setup()
        a.tick()
        st = a.tv_state()["setup"]
        self.assertTrue(st["active"])
        self.assertEqual((st["agents_saved"], st["agents_message"]), (True, None))


def html_escape(t):
    import html
    return html.escape(t)


# --------------------------------------------------------------------------- registre illisible : jamais remplacé
class TestCorruptRegistryRefused(unittest.TestCase):
    CASES = (("json", b"{pas du json"), ("shape", b"[]"), ("version", b'{"v": 2, "agents": {}}'),
             ("agents", b'{"v": 1, "agents": []}'), ("deep", b"[" * 100000 + b"]" * 100000))

    def test_every_mutator_refuses_and_preserves_bytes(self):
        for name, raw in self.CASES:
            with self.subTest(case=name):
                d = tmpdir(self, "otvbad")
                r = agents.Registry(os.path.join(d, "agents.json"))
                with open(r.path, "wb") as f:
                    f.write(raw)
                fp = r.snapshot(KNOWN)[1]
                for label, call in (("issue", lambda: r.issue("muse", ["read"])),
                                    ("issue-force", lambda: r.issue("muse", ["read"], force=True)),
                                    ("revoke", lambda: r.revoke("muse")),
                                    ("set_policy", lambda: r.set_policy({"muse": ["read"]})),
                                    ("set_policy-revoke", lambda: r.set_policy({}, revoke_exceeding=True)),
                                    ("rollback", lambda: r.rollback_issue("muse", "x" * 43, None)),
                                    ("apply", lambda: r.apply_policy_changes({"muse": ["read"]}, KNOWN, fp))):
                    with self.assertRaisesRegex(ValueError, "^registry_unreadable$", msg=label):
                        call()
                    self.assertEqual(read_bytes(r.path), raw, label)
                self.assertEqual([n for n in os.listdir(d) if n.startswith(".")], [])   # aucun temporaire
                self.assertTrue(r.snapshot(KNOWN)[2])

    def test_cli_issue_and_revoke_refuse_with_exit_2(self):
        e = Env(self)
        with open(e.path, "wb") as f:
            f.write(b"{pas du json")
        for argv in (("agent", "revoke", "--id", "muse"),
                     ("agent", "issue", "--id", "muse", "--scopes", "read", "--out", os.path.join(e.dir, "c.json"))):
            code, out, err = e.finish(e.cli(*argv))
            self.assertEqual((code, out), (2, {"ok": False, "error": "registry_unreadable"}), err)
            self.assertEqual(read_bytes(e.path), b"{pas du json")
        self.assertFalse(os.path.exists(os.path.join(e.dir, "c.json")))

    def test_setup_qr_never_replaces_unreadable_registry_but_wifi_and_name_proceed(self):
        a = make_app(self)
        a.tick()                                                          # Ethernet en ligne : « garder la connexion »
        with open(a.cfg.registry_file, "wb") as f:
            f.write(b"{pas du json")
        tok = token_of(a)
        code, kind, err = a.setup_submit({"t": tok, "name": "Salon", "net": "keep", "agent_muse": "full"})
        self.assertEqual((code, kind, err), (200, "done", None))
        self.assertEqual(config.load_device(a.cfg)["name"], "Salon")
        self.assertEqual(read_bytes(a.cfg.registry_file), b"{pas du json")
        self.assertIn({"event": "setup_agents_not_saved", "error": "registry_unreadable"},
                      [x for x in a.test_logs if x["event"] == "setup_agents_not_saved"])

    def test_setup_qr_revokes_exceeding_identities_in_the_same_write(self):
        a = make_app(self)
        a.tick()
        t_muse = a.registry.issue("muse", ["display", "feedback", "read"])
        t_hermit = a.registry.issue("hermit", ["read"])
        writes = []
        real = agents.atomic_write_json
        with mock.patch.object(agents, "atomic_write_json", lambda *x, **k: writes.append(1) or real(*x, **k)):
            tok = token_of(a)
            code, kind, _ = a.setup_submit({"t": tok, "name": "Salon", "net": "keep", "agent_muse": "read",
                                            "agent_hermit": "read"})
        self.assertEqual((code, kind), (200, "done"))
        self.assertEqual(len(writes), 1)                                  # politique + révocation : une écriture
        self.assertIsNone(a.registry.authenticate("muse", t_muse))         # au-delà de « voir » : révoqué
        self.assertEqual(a.registry.authenticate("hermit", t_hermit), ["read"])


# --------------------------------------------------------------------------- vrais processus concurrents
class TestCrossProcess(unittest.TestCase):
    def test_concurrent_processes_never_lose_an_update(self):
        d = tmpdir(self, "otvmp")
        path = os.path.join(d, "agents.json")
        script = os.path.join(d, "worker.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(WORKER)
        n, workers = 16, 5
        ps = [popen(self, [sys.executable, "-I", script, PI4, path, str(w), str(n)], stderr=subprocess.PIPE)
              for w in range(workers)]
        for p in ps:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode()[-2000:])
        got = set(agents.Registry(path).list())
        want = {"w%dx%d" % (w, j) for w in range(workers) for j in range(1, n, 2)}
        self.assertEqual(got, want)                                       # aucune émission ni révocation perdue

    def test_phone_save_first_then_cli_revoke_waits_and_both_changes_survive(self):
        """Téléphone (service) en pleine écriture sous le flock ; `agent revoke` (autre processus) attend, relit,
        révoque : la politique du téléphone ET la révocation sont présentes."""
        e = Env(self)
        r = agents.Registry(e.path)
        r.issue("muse", ["display", "feedback", "read"])
        r.issue("hermes", ["read"])
        r.set_policy({"muse": ["display", "feedback", "read"], "hermit": [], "hermes": ["read"]})
        fp = r.snapshot(KNOWN)[1]
        pause, res = Pause(), {}
        with mock.patch.object(agents, "atomic_write_json", pause):
            t = threading.Thread(target=lambda: res.setdefault("out", r.apply_policy_changes(
                {"muse": ["feedback", "read"], "hermit": ["read"]}, KNOWN, fp)))
            t.start()
            self.assertTrue(pause.inside.wait(10))
            p = e.cli("agent", "revoke", "--id", "hermes")
            self.assertTrue(still_waiting(p))                              # bloqué sur le flock
            pause.go.set()
            t.join(10)
        code, out, err = e.finish(p)
        self.assertEqual((code, out), (0, {"ok": True, "revoked": True}), err)
        self.assertEqual(res["out"], {"muse": "restricted", "hermit": "policy"})
        r2 = agents.Registry(e.path)
        self.assertEqual(r2.policy(), {"muse": ["feedback", "read"], "hermit": ["read"], "hermes": ["read"]})
        self.assertEqual(sorted(r2.list()), ["muse"])                      # hermes révoqué, muse restreint
        self.assertEqual(r2.list()["muse"]["scopes"], ["feedback", "read"])

    def test_cli_revoke_first_then_phone_save_is_refused_as_stale_never_resurrects(self):
        """`agent revoke` (autre processus) tient le flock ; la sauvegarde du téléphone, préparée sur l'ancien état,
        attend puis relit : état changé ⇒ « stale », rien d'écrit, l'identité révoquée ne revient jamais."""
        e = Env(self)
        r = agents.Registry(e.path)
        tok = r.issue("muse", ["display", "feedback", "read"])
        r.set_policy({"muse": ["display", "feedback", "read"], "hermit": [], "hermes": []})
        fp = r.snapshot(KNOWN)[1]
        p = e.cli("agent", "revoke", "--id", "muse", pause=True)
        e.wait_inside(p)
        res = {}

        def phone():
            try:
                res["out"] = r.apply_policy_changes({"muse": ["feedback", "read"]}, KNOWN, fp)
            except ValueError as ex:
                res["err"] = str(ex)
        t = threading.Thread(target=phone)
        t.start()
        t.join(0.6)
        self.assertTrue(t.is_alive())                                      # le service attend le flock
        code, out, err = e.finish(p, b"\n")
        t.join(10)
        self.assertEqual((code, out), (0, {"ok": True, "revoked": True}), err)
        self.assertEqual(res, {"err": "stale"})
        r2 = agents.Registry(e.path)
        self.assertNotIn("muse", r2.list())
        self.assertIsNone(r2.authenticate("muse", tok))
        self.assertEqual(r2.policy()["muse"], ["display", "feedback", "read"])   # rien écrit par le téléphone

    def test_phone_save_first_then_cli_issue_reads_the_new_policy(self):
        """Le téléphone autorise Hermes pendant qu'`agent issue --id hermes` démarre : la CLI attend et relit sous le
        flock, donc voit la nouvelle politique (sinon : scope_exceeds_policy) ; l'identité ET la politique restent."""
        e = Env(self)
        r = agents.Registry(e.path)
        r.set_policy({"muse": [], "hermit": [], "hermes": []})
        fp = r.snapshot(KNOWN)[1]
        pause, res = Pause(), {}
        with mock.patch.object(agents, "atomic_write_json", pause):
            t = threading.Thread(target=lambda: res.setdefault("out", r.apply_policy_changes(
                {"hermes": ["read"], "muse": ["feedback", "read"]}, KNOWN, fp)))
            t.start()
            self.assertTrue(pause.inside.wait(10))
            out_file = os.path.join(e.dir, "hermes.json")
            p = e.cli("agent", "issue", "--id", "hermes", "--scopes", "read", "--out", out_file)
            self.assertTrue(still_waiting(p))
            pause.go.set()
            t.join(10)
        code, out, err = e.finish(p)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(res["out"], {"hermes": "policy", "muse": "policy"})
        with open(out_file, encoding="utf-8") as f:
            ctok = json.load(f)["token"]
        r2 = agents.Registry(e.path)
        self.assertEqual(r2.authenticate("hermes", ctok), ["read"])
        self.assertEqual(r2.policy(), {"muse": ["feedback", "read"], "hermit": [], "hermes": ["read"]})

    def test_real_phone_http_save_and_cli_revoke_race(self):
        """Bout en bout service : vrai POST du téléphone (/assistants) suspendu dans son écriture, `agent revoke` réel
        en parallèle ; ensuite l'état du service lui-même (pas seulement le fichier) montre les deux changements."""
        a, s = tam.configured(self)
        a.registry.issue("hermit", ["read"])
        a.registry.issue("hermes", ["read"])
        a.registry.set_policy({"muse": [], "hermit": ["read"], "hermes": ["read"]})
        e = Env(self)
        with open(e.env, "w", encoding="utf-8") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\n" % (a.cfg.state_dir, a.cfg.run_dir))
        tok, ph = tam.open_mode(self, a, s)
        _, form = tam.confirm(ph, tok, muse="feedback", hermit="read", hermes="read")
        pause, res = Pause(), {}
        with mock.patch.object(agents, "atomic_write_json", pause):
            t = threading.Thread(target=lambda: res.setdefault("post", ph.post(form)))
            t.start()
            self.assertTrue(pause.inside.wait(10))
            p = e.cli("agent", "revoke", "--id", "hermit")
            self.assertTrue(still_waiting(p))
            pause.go.set()
            t.join(15)
        code, out, err = e.finish(p)
        self.assertEqual((code, out), (0, {"ok": True, "revoked": True}), err)
        self.assertEqual(res["post"][0], 200)
        st = {x["id"]: x for x in a.assistants()}
        self.assertEqual((st["muse"]["level"], st["muse"]["connected"]), ("feedback", False))
        self.assertEqual((st["hermit"]["authorized"], st["hermit"]["connected"]), (True, False))   # révoqué, pas ressuscité
        self.assertEqual((st["hermes"]["connected"]), True)


# --------------------------------------------------------------------------- écoute /assistants impossible
class TestAgentsListenerUnavailable(unittest.TestCase):
    def test_bind_failure_closes_mode_as_unavailable_without_retry_loop(self):
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        a = make_app(self, OPENTV_SETUP_PORT=str(busy.getsockname()[1]))
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = remote_servers(self, a)
        code, _, body = tam.tv_post(s, "/api/tv/agents")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["seq"], 0)                       # référence : rien d'antérieur annoncé
        # issue déjà connue dans la réponse : la TV ne rouvre jamais une feuille vide pour cette ouverture
        self.assertEqual(json.loads(body)["last"], {"seq": 1, "kind": "unavailable"})
        m = a.tv_state()["assistants"]["manage"]
        self.assertFalse(m["active"])                                      # jamais un QR vide pendant 10 min
        self.assertEqual(m["last"], {"seq": 1, "kind": "unavailable"})
        self.assertIsNone(s.web.lan_port())
        for _ in range(5):                                                 # aucune boucle d'essais d'écoute
            a.tick()
            s.web.sync_lan()
        errs = [x for x in a.test_logs if x["event"] == "setup_listener_error"]
        self.assertEqual([x.get("purpose") for x in errs], ["agents"])
        self.assertEqual(a.tv_state()["assistants"]["manage"]["last"]["seq"], 1)
        busy.close()                                                      # l'utilisateur réessaie : ça marche
        tok, _ph = tam.open_mode(self, a, s)
        self.assertTrue(tok)


if __name__ == "__main__":
    unittest.main()
