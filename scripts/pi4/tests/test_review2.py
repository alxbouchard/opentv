"""Revue qualité n°2 : E/S privilégiées sans chemin (dir_fd, O_NOFOLLOW, fchown/fchmod, aucun chown/chmod par
chemin), fichier client d'agent root-owned hors du home, Wi-Fi transactionnel (profil candidat, ancien profil
intact jusqu'au succès), QR non brûlable par de faux jetons, serveurs bornés (slowloris), kiosque jamais « failed »,
journal HTTP sans polls 200, install.sh après le point de non-retour."""
import collections
import contextlib
import errno
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests._common import PI4, SECRET_PSK
from tests.test_service import make_app, Servers, token_of
from tests.test_hardening import unit
from tests.test_install import INSTALL, sh
from tests.test_setup_net import kf_value
from opentv_pi import agentport, agents, app as appmod, cli, fsutil, mcp, nethelper, netjobs, nmkeyfile, web

PW = collections.namedtuple("PW", "pw_name pw_uid pw_gid pw_dir")
GR = collections.namedtuple("GR", "gr_name gr_gid gr_mem")


def _boom(*a, **k):
    raise AssertionError("chown/chmod/makedirs par chemin interdit : %r" % (a,))


@contextlib.contextmanager
def no_path_perms(makedirs=False):
    """Toute opération de permission par chemin échoue (seuls fchown/fchmod sur un descripteur sont permis)."""
    with contextlib.ExitStack() as st:
        for name in ("chown", "chmod", "lchown", "lchmod"):
            if hasattr(os, name):
                st.enter_context(mock.patch.object(os, name, _boom))
        st.enter_context(mock.patch.object(shutil, "chown", _boom))
        if makedirs:
            st.enter_context(mock.patch.object(os, "makedirs", _boom))
            st.enter_context(mock.patch.object(os, "mkdir", _boom))
        yield


class FchownLog:
    """os.fchown simulé (non-root) : enregistre (inode, uid, gid) sans rien changer."""

    def __init__(self):
        self.calls = []

    def __call__(self, fd, uid, gid):
        st = os.fstat(fd)
        self.calls.append((st.st_ino, uid, gid, stat.S_ISREG(st.st_mode)))


def snapshot(path):
    out = {}
    for d, dirs, files in os.walk(path, followlinks=False):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            body = None
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    body = f.read()
            elif stat.S_ISLNK(st.st_mode):
                body = os.readlink(p)
            out[os.path.relpath(p, path)] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid, body)
    return out


def tmpdir(tc, prefix="otv2"):
    d = os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    tc.addCleanup(shutil.rmtree, d, True)
    return d


def run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, json.loads(out.getvalue())


# --------------------------------------------------------------------------- CRITIQUE : agent issue --owner
class RootIssueEnv:
    """CLI `agent issue` comme root (geteuid simulé), comptes simulés, dossier client sûr dans un temp root."""
    AGENT_UID = 4242

    def __init__(self, tc, home_kind="dir"):
        self.tc = tc
        self.d = d = tmpdir(tc, "otvroot")
        self.state = os.path.join(d, "state")
        os.makedirs(self.state, mode=0o700)
        self.clients = os.path.join(d, "etc-opentv-agents")
        os.mkdir(self.clients, 0o755)
        os.chmod(self.clients, 0o755)
        self.env = os.path.join(d, "otv.env")
        with open(self.env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\nOPENTV_AGENT_CLIENTS_DIR=%s\n"
                    % (self.state, os.path.join(d, "run"), self.clients))
        self.victim = os.path.join(d, "victim")
        os.makedirs(os.path.join(self.victim, "opentv"))
        with open(os.path.join(self.victim, "opentv", "agent-muse.json"), "w") as f:
            f.write("ne pas toucher")
        self.home = os.path.join(d, "home-alex")
        if home_kind != "missing":
            os.mkdir(self.home, 0o700)
        if home_kind == "config-link":          # ~/.config -> dossier d'une victime
            os.symlink(self.victim, os.path.join(self.home, ".config"))
        elif home_kind == "opentv-link":        # ~/.config/opentv -> dossier d'une victime
            os.mkdir(os.path.join(self.home, ".config"))
            os.symlink(os.path.join(self.victim, "opentv"), os.path.join(self.home, ".config", "opentv"))
        elif home_kind == "dest-link":          # ~/.config/opentv/agent-muse.json -> fichier d'une victime
            os.makedirs(os.path.join(self.home, ".config", "opentv"))
            os.symlink(os.path.join(self.victim, "opentv", "agent-muse.json"),
                       os.path.join(self.home, ".config", "opentv", "agent-muse.json"))
        self.users = {"opentv": PW("opentv", os.getuid(), os.getgid(), self.state),
                      "alex": PW("alex", self.AGENT_UID, self.AGENT_UID, self.home),
                      "root": PW("root", 0, 0, "/root")}
        self.groups = {self.AGENT_UID: GR("alex", self.AGENT_UID, []), 0: GR("root", 0, [])}
        self.fchown = FchownLog()

    def getpwnam(self, n):
        return self.users[n]

    def getgrgid(self, g):
        return self.groups[g]

    @contextlib.contextmanager
    def as_root(self):
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("pwd.getpwnam", self.getpwnam))
            st.enter_context(mock.patch("pwd.getpwall", lambda: list(self.users.values())))
            st.enter_context(mock.patch("grp.getgrgid", self.getgrgid))
            st.enter_context(mock.patch.object(os, "geteuid", lambda: 0))
            st.enter_context(mock.patch.object(os, "fchown", self.fchown))
            st.enter_context(mock.patch.object(cli, "CLIENT_DIR_TRUSTED_UIDS", (0, os.getuid())))
            yield

    def issue(self, *extra, agent="muse"):
        with self.as_root(), no_path_perms(makedirs=True):
            return run_cli(["--env", self.env, "agent", "issue", "--id", agent, "--scopes", "read", "--owner",
                            "alex"] + list(extra))

    def registry(self):
        return agents.Registry(os.path.join(self.state, "agents.json"))


class TestRootAgentIssue(unittest.TestCase):
    def check_ok(self, e, code, out):
        self.assertEqual(code, 0, out)
        path = os.path.join(e.clients, "agent-muse-%d.json" % e.AGENT_UID)
        self.assertEqual(out["config"], path)
        self.assertEqual(out["mcp"]["args"], ["--config", path])
        st = os.lstat(path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)                  # root:<groupe privé de l'agent> 0640
        ino = st.st_ino
        self.assertIn((ino, 0, e.AGENT_UID, True), e.fchown.calls)         # fchown sur le descripteur, jamais le chemin
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        self.assertEqual(e.registry().authenticate("muse", c["token"]), ["read"])
        self.assertEqual([n for n in os.listdir(e.clients) if n.startswith(".")], [])   # aucun temporaire
        return c

    def test_symlinked_home_components_are_never_touched(self):
        for kind in ("dir", "config-link", "opentv-link", "dest-link", "missing"):
            with self.subTest(kind=kind):
                e = RootIssueEnv(self, kind)
                before_home = snapshot(e.home) if os.path.isdir(e.home) else None
                before_victim = snapshot(e.victim)
                code, out = e.issue()
                self.check_ok(e, code, out)
                self.assertEqual(snapshot(e.victim), before_victim)
                if before_home is None:
                    self.assertFalse(os.path.lexists(e.home))               # home absent : jamais créé
                else:
                    self.assertEqual(snapshot(e.home), before_home)
                self.assertNotIn(e.home, json.dumps(out))

    def test_out_is_refused_as_root(self):
        e = RootIssueEnv(self)
        code, out = e.issue("--out", os.path.join(e.home, "x.json"))
        self.assertEqual((code, out["error"]), (2, "out_forbidden_as_root"))
        self.assertNotIn("muse", e.registry().list())
        self.assertEqual(os.listdir(e.home), [])

    def test_unsafe_client_dir_refused_before_issuing(self):
        e = RootIssueEnv(self)
        os.chmod(e.clients, 0o777)
        code, out = e.issue()
        self.assertEqual((code, out["error"]), (2, "clients_dir_unsafe"))
        self.assertNotIn("muse", e.registry().list())
        os.chmod(e.clients, 0o755)
        shutil.rmtree(e.clients)
        os.symlink(e.victim, e.clients)                                    # dossier remplacé par un lien
        code, out = e.issue()
        self.assertEqual((code, out["error"]), (2, "clients_dir_unsafe"))
        self.assertEqual(sorted(os.listdir(e.victim)), ["opentv"])

    def test_shared_primary_group_refused(self):
        e = RootIssueEnv(self)
        e.groups[e.AGENT_UID] = GR("alex", e.AGENT_UID, ["bob"])
        code, out = e.issue()
        self.assertEqual((code, out["error"]), (2, "owner_group_not_private"))
        self.assertNotIn("muse", e.registry().list())

    def test_missing_owner_refused_before_issuing(self):
        e = RootIssueEnv(self)
        del e.users["alex"]
        code, out = e.issue()
        self.assertEqual((code, out["error"]), (2, "owner_missing"))
        self.assertNotIn("muse", e.registry().list())

    def test_client_write_failure_rolls_back_token(self):
        e = RootIssueEnv(self)
        first = self.check_ok(e, *e.issue())
        path = os.path.join(e.clients, "agent-muse-%d.json" % e.AGENT_UID)
        with open(path, "rb") as f:
            before = f.read()
        real_rename = os.rename

        def failing_rename(src, dst, *a, **k):
            if str(dst).startswith("agent-"):
                raise OSError(errno.ENOSPC, "disque plein")
            return real_rename(src, dst, *a, **k)
        with mock.patch.object(os, "rename", failing_rename):
            code, out = e.issue()
        self.assertEqual((code, out["error"]), (2, "client_write_failed"))
        reg = e.registry()
        self.assertEqual(reg.authenticate("muse", first["token"]), ["read"])   # ancien jeton restauré
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)                                  # ancien fichier client intact
        self.assertEqual([n for n in os.listdir(e.clients) if n.startswith(".")], [])
        with mock.patch.object(os, "rename", failing_rename):
            code, out = e.issue(agent="hermit")
        self.assertEqual((code, out["error"]), (2, "client_write_failed"))
        self.assertNotIn("hermit", e.registry().list())                         # nouveau : révoqué

    def test_mcp_accepts_root_owned_group_readable_file_only(self):
        d = tmpdir(self)
        p = os.path.join(d, "agent-muse-1.json")
        with open(p, "w") as f:
            json.dump({"v": 1, "agent_id": "muse", "token": "t" * 43, "socket": "/run/opentv/agent.sock"}, f)
        real = os.fstat

        def fake(mode, uid=0, gid=None):
            def f(fd):
                st = real(fd)
                return os.stat_result((stat.S_IFREG | mode, st.st_ino, st.st_dev, 1, uid,
                                       os.getegid() if gid is None else gid, st.st_size, 0, 0, 0))
            return f
        with mock.patch.object(os, "fstat", fake(0o640)):
            self.assertEqual(mcp.load_client_config(p)["agent_id"], "muse")
        for mode, uid, gid, err in ((0o660, 0, None, "config_mode"), (0o644, 0, None, "config_mode"),
                                    (0o640, 0, 987654, "config_owner"), (0o640, 12345, None, "config_owner")):
            with mock.patch.object(os, "fstat", fake(mode, uid, gid)):
                with self.assertRaises(mcp.ConfigError) as cm:
                    mcp.load_client_config(p)
                self.assertTrue(str(cm.exception).startswith(err), (mode, uid, gid, str(cm.exception)))

    def test_non_root_cannot_write_for_another_owner(self):
        e = RootIssueEnv(self)
        with mock.patch("pwd.getpwnam", e.getpwnam):
            code, out = run_cli(["--env", e.env, "agent", "issue", "--id", "muse", "--scopes", "read", "--owner",
                                 "alex"])
        self.assertEqual((code, out["error"]), (2, "root_required_for_owner"))
        self.assertNotIn("muse", e.registry().list())


# --------------------------------------------------------------------------- IMPORTANT : chown/chmod par fd
class RootSetupEnv:
    def __init__(self, tc):
        self.d = d = tmpdir(tc, "otvset")
        self.state = os.path.join(d, "state")
        os.makedirs(self.state, mode=0o700)
        self.env = os.path.join(d, "otv.env")
        with open(self.env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_RUN_DIR=%s\n" % (self.state, os.path.join(d, "run")))
        self.victim = os.path.join(d, "victim.txt")
        with open(self.victim, "w") as f:
            f.write("secret root")
        os.chmod(self.victim, 0o644)
        self.fchown = FchownLog()

    def reset(self):
        users = {"opentv": PW("opentv", os.getuid(), os.getgid(), self.state)}
        with mock.patch("pwd.getpwnam", users.__getitem__), mock.patch.object(os, "geteuid", lambda: 0), \
                mock.patch.object(os, "fchown", self.fchown), no_path_perms():
            return run_cli(["--env", self.env, "setup", "reset"])


class TestSetupResetFd(unittest.TestCase):
    def test_normal_reset_uses_fd_ownership(self):
        e = RootSetupEnv(self)
        code, out = e.reset()
        self.assertEqual(code, 0, out)
        for n in ("setup-requested", "hotspot-rearm"):
            st = os.lstat(os.path.join(e.state, n))
            self.assertTrue(stat.S_ISREG(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            self.assertIn((st.st_ino, os.getuid(), os.getgid(), True), e.fchown.calls)

    def test_symlink_fifo_and_hardlink_refused(self):
        for kind in ("symlink", "fifo", "hardlink", "dir"):
            with self.subTest(kind=kind):
                e = RootSetupEnv(self)
                target = os.path.join(e.state, "setup-requested")
                if kind == "symlink":
                    os.symlink(e.victim, target)
                elif kind == "fifo":
                    os.mkfifo(target, 0o600)
                elif kind == "hardlink":
                    os.link(e.victim, target)
                else:
                    os.mkdir(target)
                res = {}
                t = threading.Thread(target=lambda: res.setdefault("r", e.reset()), daemon=True)
                t.start()
                t.join(5)
                self.assertFalse(t.is_alive(), "setup reset bloqué (%s)" % kind)
                code, out = res["r"]
                self.assertEqual((code, out["error"]), (2, "unsafe_path"))
                with open(e.victim) as f:
                    self.assertEqual(f.read(), "secret root")
                self.assertEqual(stat.S_IMODE(os.stat(e.victim).st_mode), 0o644)
                self.assertFalse(any(c[0] == os.stat(e.victim).st_ino for c in e.fchown.calls))


class TestAtomicWritesFd(unittest.TestCase):
    def test_registry_writes_never_use_path_permissions(self):
        d = tmpdir(self)
        victim = os.path.join(d, "victim.json")
        with open(victim, "w") as f:
            f.write("{}")
        os.chmod(victim, 0o644)
        path = os.path.join(d, "agents.json")
        os.symlink(victim, path)                              # registre pré-remplacé par un lien
        fc = FchownLog()
        r = agents.Registry(path)
        with mock.patch.object(os, "fchown", fc), no_path_perms():
            r.set_policy({"muse": ["read"]}, uid=os.getuid(), gid=os.getgid())
            tok = r.issue("muse", ["read"], uid=os.getuid(), gid=os.getgid())
            r.revoke("muse", uid=os.getuid(), gid=os.getgid())
        st = os.lstat(path)
        self.assertTrue(stat.S_ISREG(st.st_mode))             # le lien est remplacé, jamais suivi
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        with open(victim) as f:
            self.assertEqual(f.read(), "{}")
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644)
        self.assertTrue(tok)
        self.assertEqual(len(fc.calls), 3)
        self.assertTrue(all(c[3] for c in fc.calls))

    def test_atomic_write_refuses_symlinked_directory(self):
        d = tmpdir(self)
        real = os.path.join(d, "real")
        os.mkdir(real)
        os.symlink(real, os.path.join(d, "link"))
        with self.assertRaises(OSError):
            fsutil.atomic_write_path(os.path.join(d, "link", "x.json"), b"{}")
        self.assertEqual(os.listdir(real), [])

    def test_helper_results_and_keyfiles_use_fd_only(self):
        b = tmpdir(self)
        jd, tmp, rd, nm = (os.path.join(b, n) for n in ("jobs", "tmp", "res", "nm"))
        for x in (jd, tmp, rd, nm):
            os.makedirs(x, mode=0o700)
        victim_dir = os.path.join(b, "victim")
        os.mkdir(victim_dir)
        victim = os.path.join(victim_dir, "v.json")
        with open(victim, "w") as f:
            f.write("intact")
        j1 = netjobs.submit(jd, tmp, "ap-start", {"ssid": "OpenTV-AB12", "psk": "abcdefgh2345"})
        os.symlink(victim, os.path.join(rd, j1 + ".json"))   # résultat pré-placé en lien
        netjobs.submit(jd, tmp, "wifi-apply", {"ssid": "Maison", "psk": SECRET_PSK})
        fc = FchownLog()
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=lambda a, t: (0, ""), sleep=lambda s: None,
                             log=lambda *a, **k: None, chown=True)
        with mock.patch.object(os, "fchown", fc), no_path_perms():
            res = h.run_once()
        self.assertTrue(all(r["ok"] for r in res), res)
        with open(victim) as f:
            self.assertEqual(f.read(), "intact")
        for n in os.listdir(rd):
            st = os.lstat(os.path.join(rd, n))
            self.assertTrue(stat.S_ISREG(st.st_mode), n)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(sum(1 for c in fc.calls if c[1] == os.getuid()), 2)
        for n in os.listdir(nm):
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(nm, n)).st_mode), 0o600, n)
        # dossier des résultats remplacé par un lien : rien n'est écrit chez la victime
        netjobs.submit(jd, tmp, "ap-stop", {})
        shutil.rmtree(rd)
        os.symlink(victim_dir, rd)
        logs = []
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=lambda a, t: (0, ""), sleep=lambda s: None,
                             log=lambda ev, **f: logs.append(dict(f, event=ev)), chown=True)
        with mock.patch.object(os, "fchown", fc), no_path_perms():
            h.run_once()
        self.assertEqual(os.listdir(victim_dir), ["v.json"])
        self.assertTrue(any(x["event"] == "net_result_error" for x in logs))

    def test_jobs_dir_replaced_by_symlink_deletes_nothing(self):
        # le compte du service possède /var/lib/opentv : il peut remplacer netjobs par un lien vers un dossier root
        # (profils NetworkManager) ; le helper root ne doit alors rien lister, lire ni supprimer
        b = tmpdir(self)
        jd, rd, nm = (os.path.join(b, n) for n in ("jobs", "res", "nm"))
        for x in (rd, nm):
            os.makedirs(x, mode=0o700)
        with open(os.path.join(nm, "Maison.nmconnection"), "w") as f:
            f.write("[connection]\nid=Maison\n")
        os.symlink(nm, jd)
        calls, logs = [], []
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=lambda a, t: calls.append(a) or (0, ""),
                             sleep=lambda s: None, log=lambda ev, **f: logs.append(dict(f, event=ev)), chown=False)
        self.assertEqual(h.run_once(), [])
        self.assertEqual(os.listdir(nm), ["Maison.nmconnection"])
        self.assertEqual(calls, [])
        self.assertTrue(any(x["event"] == "net_jobs_dir_unsafe" for x in logs))

    def test_junk_in_jobs_dir_removed_relative_to_dir_fd(self):
        b = tmpdir(self)
        jd, tmp, rd, nm = (os.path.join(b, n) for n in ("jobs", "tmp", "res", "nm"))
        for x in (jd, tmp, rd, nm):
            os.makedirs(x, mode=0o700)
        outside = os.path.join(b, "outside")
        os.makedirs(os.path.join(outside, "keep"))
        os.symlink(outside, os.path.join(jd, "junkdirlink"))
        os.makedirs(os.path.join(jd, "junkdir", "sub"))
        os.symlink(outside, os.path.join(jd, "junkdir", "sub", "link"))
        os.mkfifo(os.path.join(jd, "fifo0001.json"))
        jid = netjobs.submit(jd, tmp, "ap-stop", {})
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=lambda a, t: (0, ""), sleep=lambda s: None,
                             log=lambda *a, **k: None, chown=False)
        with no_path_perms():
            res = h.run_once()
        self.assertEqual([(r["id"], r["ok"]) for r in res], [(jid, True)])
        self.assertEqual(os.listdir(jd), [])
        self.assertEqual(os.listdir(outside), ["keep"])


# --------------------------------------------------------------------------- IMPORTANT : Wi-Fi transactionnel
OLD_UUID = "11111111-2222-4333-8444-555555555555"
OLD_PSK = "ancien-mot-de-passe"


class TxNM:
    """NetworkManager simulé : `up` d'un candidat réussit seulement si son keyfile porte le bon mot de passe.
    Photographie l'ancien keyfile au moment de l'activation du candidat."""

    def __init__(self, nm_dir, good_psk, hang=False):
        self.nm_dir, self.good_psk, self.hang = nm_dir, good_psk, hang
        self.calls, self.old_at_up, self.cand_at_up = [], None, None
        self.t = 1000.0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def _read(self, name):
        try:
            with open(os.path.join(self.nm_dir, name + ".nmconnection"), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if "up" in argv and argv[-2] == "uuid" and argv[-1] != OLD_UUID:
            self.old_at_up = self._read(nmkeyfile.WIFI_ID)
            self.cand_at_up = self._read(nmkeyfile.CAND_ID)
            if self.hang:
                self.t += timeout + 2 * nethelper.KILL_GRACE_S
                return 124, ""
            psk = kf_value((self.cand_at_up or b"").decode(), "wifi-security", "psk")
            return (0, "") if psk == self.good_psk else (4, "Error: secrets were required")
        return 0, ""


class TestTransactionalWifi(unittest.TestCase):
    def env(self, ap=False):
        b = tmpdir(self)
        dirs = [os.path.join(b, n) for n in ("jobs", "tmp", "res", "nm")]
        for x in dirs:
            os.makedirs(x, mode=0o700)
        old = nmkeyfile.write_profile(dirs[3], nmkeyfile.WIFI_ID,
                                      nmkeyfile.wifi_profile("HomeNet", OLD_PSK, uuid=OLD_UUID))
        if ap:
            nmkeyfile.write_profile(dirs[3], nmkeyfile.AP_ID, nmkeyfile.ap_profile("OpenTV-AB12", "abcdefgh2345"))
        with open(old, "rb") as f:
            self.old_bytes = f.read()
        self.old_ino = os.stat(old).st_ino
        return dirs, old

    def run_apply(self, dirs, nmsim, psk, restore_ap=False, budget_s=nethelper.RUN_BUDGET_S):
        jd, tmp, rd, nm = dirs
        netjobs.submit(jd, tmp, "wifi-apply", {"ssid": "HomeNet", "psk": psk, "restore_ap": restore_ap})
        logs = []
        h = nethelper.Helper(jd, rd, nm, os.getuid(), runner=nmsim, sleep=nmsim.sleep, mono=nmsim.mono,
                             log=lambda ev, **f: logs.append(dict(f, event=ev)), chown=False, budget_s=budget_s)
        t0 = nmsim.t
        res = h.run_once()
        blob = json.dumps(res) + json.dumps(logs) + json.dumps(nmsim.calls)
        for n in os.listdir(rd):
            with open(os.path.join(rd, n)) as f:
                blob += f.read()
        self.assertNotIn(psk, blob)
        self.assertNotIn(OLD_PSK, blob)
        self.assertNotIn("HomeNet", blob)
        return res[0], nmsim.t - t0

    def cand_uuid(self, nmsim):
        ups = [c for c in nmsim.calls if "up" in c and c[-2] == "uuid" and c[-1] != OLD_UUID]
        self.assertEqual(len(ups), 1, nmsim.calls)
        return ups[0][-1]

    def assert_old_intact(self, old):
        with open(old, "rb") as f:
            self.assertEqual(f.read(), self.old_bytes)
        st = os.stat(old)
        self.assertEqual((st.st_ino, stat.S_IMODE(st.st_mode)), (self.old_ino, 0o600))

    def test_wrong_password_keeps_and_reactivates_old_profile(self):
        dirs, old = self.env()
        sim = TxNM(dirs[3], good_psk="bon-mot-de-passe")
        res, _ = self.run_apply(dirs, sim, "mauvais-mot-de-passe")
        self.assertEqual((res["ok"], res["error"]), (False, "wifi_connect_failed"))
        self.assertEqual(sim.old_at_up, self.old_bytes)                 # ancien intact pendant l'essai
        self.assert_old_intact(old)
        self.assertFalse(os.path.lexists(nmkeyfile.profile_path(dirs[3], nmkeyfile.CAND_ID)))
        cand = self.cand_uuid(sim)
        self.assertIn(["nmcli", "connection", "delete", "uuid", cand], sim.calls)
        self.assertEqual(sim.calls[-1], ["nmcli", "--wait", str(nethelper.OLD_UP_WAIT_S), "connection", "up", "uuid",
                                         OLD_UUID])
        for c in sim.calls:                                               # l'ancien n'est jamais supprimé
            self.assertFalse("delete" in c and (OLD_UUID in c or nmkeyfile.WIFI_ID == c[-1]), c)
        self.assertEqual(kf_value(sim.cand_at_up.decode(), "connection", "autoconnect"), "false")
        self.assertEqual(kf_value(sim.cand_at_up.decode(), "connection", "id"), nmkeyfile.CAND_ID)

    def test_success_promotes_candidate_and_replaces_old(self):
        dirs, old = self.env(ap=True)
        sim = TxNM(dirs[3], good_psk="bon-mot-de-passe")
        res, _ = self.run_apply(dirs, sim, "bon-mot-de-passe")
        self.assertTrue(res["ok"], res)
        self.assertEqual(sim.old_at_up, self.old_bytes)
        cand = self.cand_uuid(sim)
        with open(old, encoding="utf-8") as f:
            txt = f.read()
        self.assertEqual(kf_value(txt, "wifi-security", "psk"), "bon-mot-de-passe")
        self.assertEqual(kf_value(txt, "connection", "uuid"), cand)
        self.assertEqual(kf_value(txt, "connection", "id"), nmkeyfile.WIFI_ID)
        self.assertEqual(kf_value(txt, "connection", "autoconnect"), "true")
        self.assertEqual(stat.S_IMODE(os.stat(old).st_mode), 0o600)
        self.assertNotIn(OLD_UUID, txt)
        cand_path = nmkeyfile.profile_path(dirs[3], nmkeyfile.CAND_ID)
        self.assertFalse(os.path.lexists(cand_path))
        self.assertIn(["nmcli", "connection", "load", old, cand_path], sim.calls)
        self.assertEqual(sorted(os.listdir(dirs[3])), ["opentv-wifi.nmconnection"])   # AP retiré au succès
        i_up = next(i for i, c in enumerate(sim.calls) if c[-1] == cand and "up" in c)
        self.assertFalse(any("delete" in c for c in sim.calls[:i_up]), sim.calls)    # rien supprimé avant succès

    def test_timeout_rolls_back_within_budget(self):
        for budget in (nethelper.RUN_BUDGET_S, 75.0):
            with self.subTest(budget=budget):
                dirs, old = self.env()
                sim = TxNM(dirs[3], good_psk="x", hang=True)
                res, elapsed = self.run_apply(dirs, sim, "peu-importe-123", budget_s=budget)
                self.assertEqual((res["ok"], res["error"]), (False, "wifi_connect_failed"))
                self.assertLessEqual(elapsed, budget)
                self.assert_old_intact(old)
                self.assertEqual(sim.calls[-1][:3], ["nmcli", "--wait", str(nethelper.OLD_UP_WAIT_S)])
                self.assertEqual(sim.calls[-1][-1], OLD_UUID)                 # réserve de retour jamais tronquée

    def test_hotspot_mode_failure_restores_ap_and_keeps_old(self):
        dirs, old = self.env(ap=True)
        sim = TxNM(dirs[3], good_psk="bon-mot-de-passe")
        res, _ = self.run_apply(dirs, sim, "mauvais-mot-de-passe", restore_ap=True)
        self.assertFalse(res["ok"])
        self.assert_old_intact(old)
        self.assertEqual(sim.calls[-1], ["nmcli", "--wait", str(nethelper.AP_UP_WAIT_S), "connection", "up", "id",
                                         nmkeyfile.AP_ID])

    def test_worst_case_constant_covers_rollback(self):
        self.assertLessEqual(nethelper.WIFI_APPLY_WORST_S, nethelper.RUN_BUDGET_S + nethelper.MIN_JOB_BUDGET_S)
        self.assertGreaterEqual(nethelper.ROLLBACK_RESERVE_S, nethelper.NM_CMD_TIMEOUT_S + nethelper.OLD_UP_WAIT_S)


# --------------------------------------------------------------------------- MINEUR 1 : faux jetons
class TestSetupTokenDoS(unittest.TestCase):
    def test_bogus_token_posts_never_burn_the_qr(self):
        a = make_app(self)
        s = Servers(self, a)
        tok = token_of(a)
        for i in range(appmod.BAD_TOKEN_PER_IP - 1):
            code = s.req("POST", "/setup", {"t": "faux-jeton-%03d-xxxxxxxxxxxx" % i, "name": "x", "net": "keep"},
                         lan=True)[0]
            self.assertEqual(code, 403)
        self.assertEqual(token_of(a), tok)
        code, _, body = s.req("POST", "/setup", {"t": tok, "name": "Salon", "net": "keep"}, lan=True)
        self.assertEqual(code, 200, body)

    def test_per_ip_rate_limit_is_bounded(self):
        a = make_app(self)
        a.tick()
        tok = token_of(a)
        for i in range(appmod.BAD_TOKEN_PER_IP):
            a.setup_submit({"t": "faux-%04d-xxxxxxxxxxxxxxxx" % i}, client="192.168.2.66")
        self.assertEqual(a.setup_submit({"t": tok, "name": "Salon", "net": "keep"}, client="192.168.2.66")[0], 429)
        for i in range(3000):
            a.setup_submit({"t": "faux-%04d-xxxxxxxxxxxxxxxx" % i}, client="10.0.%d.%d" % (i // 250, i % 250))
        self.assertLessEqual(len(a.bad_token_ips), appmod.BAD_TOKEN_TRACKED_IPS)
        self.assertEqual(token_of(a), tok)
        self.assertEqual(a.setup_submit({"t": tok, "name": "Salon", "net": "keep"}, client="192.168.2.10")[0], 200)

    def test_valid_token_invalid_form_still_counts(self):
        a = make_app(self)
        a.tick()
        tok = token_of(a)
        for _ in range(5):
            self.assertEqual(a.setup_submit({"t": tok, "name": "<b>", "net": "keep"}, client="192.168.2.10")[0], 400)
        self.assertEqual(a.setup_submit({"t": tok, "name": "Salon", "net": "keep"}, client="192.168.2.10")[0], 403)


# --------------------------------------------------------------------------- MINEUR 2 : concurrence bornée
def _recv_all(s, limit=1.0):
    s.settimeout(limit)
    try:
        return s.recv(4096)
    except socket.timeout:
        return None
    except OSError:
        return b""


class TestBoundedServers(unittest.TestCase):
    def idle_flood(self, connect, srv, busy_marker, n=40):
        srv.deadline_s = 1.5
        base = threading.active_count()
        socks = [connect() for _ in range(n)]
        self.addCleanup(lambda: [x.close() for x in socks])
        time.sleep(0.5)
        self.assertLessEqual(threading.active_count(), base + srv.max_workers + 1)
        busy = [x for x in socks if (_recv_all(x, 0.05) or b"").find(busy_marker) >= 0]
        self.assertGreaterEqual(len(busy), n - srv.max_workers)
        time.sleep(srv.deadline_s + 1.0)
        for x in socks:
            self.assertNotEqual(_recv_all(x, 0.5), None)                   # fermé par l'échéance globale
        time.sleep(0.2)
        self.assertLessEqual(threading.active_count(), base + 1)

    def test_http_tv_and_lan_idle_flood(self):
        a = make_app(self)
        s = Servers(self, a)
        for srv in (s.web.tv, s.web.lan):
            with self.subTest(port=srv.server_address[1]):
                self.assertEqual(srv.max_workers, 16)
                port = srv.server_address[1]
                self.idle_flood(lambda: socket.create_connection(("127.0.0.1", port)), srv, b" 503 ")
                self.assertEqual(s.req("GET", "/healthz")[0], 200)

    def test_http_slow_drip_is_cut_by_deadline(self):
        a = make_app(self)
        s = Servers(self, a)
        s.web.tv.deadline_s = 1.0
        c = socket.create_connection(("127.0.0.1", s.web.tv_port()))
        self.addCleanup(c.close)
        t0 = time.monotonic()
        closed = None
        for ch in b"GET /healthz HTTP/1.0\r\nX-A: " + b"a" * 200:
            try:
                c.send(bytes([ch]))
            except OSError:
                closed = time.monotonic() - t0
                break
            if _recv_all(c, 0.1) is not None:
                closed = time.monotonic() - t0
                break
        self.assertIsNotNone(closed)
        self.assertLess(closed, 3.0)

    def test_agent_port_idle_flood(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="otvap", dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "a.sock")
        srv = agentport.AgentPortServer(path, lambda req, uid: {"ok": True, "result": {"pong": True}})
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(srv.max_workers, 16)

        def connect():
            x = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            x.connect(path)
            return x
        self.idle_flood(connect, srv, b'"busy"')
        self.assertEqual(agentport.call(path, {"v": 1}), {"ok": True, "result": {"pong": True}})


# --------------------------------------------------------------------------- MINEURS 3 et 4 : kiosque, journal
class TestKioskAndJournal(unittest.TestCase):
    def test_kiosk_never_stays_failed(self):
        k = unit("opentv-kiosk.service")
        self.assertEqual(k["Unit"]["StartLimitIntervalSec"], "0")
        self.assertNotIn("StartLimitBurst", k["Unit"])
        self.assertEqual(k["Service"]["Restart"], "always")
        self.assertTrue(2 <= int(k["Service"]["RestartSec"]) <= 30)

    def test_http_journal_skips_successful_polls(self):
        a = make_app(self)
        s = Servers(self, a)
        with mock.patch.object(web, "HTTP_OK_SAMPLE", 10):
            for _ in range(25):
                self.assertEqual(s.req("GET", "/api/tv/state", headers=s.tv_headers())[0], 200)
            for _ in range(3):
                self.assertEqual(s.req("GET", "/api/tv/state")[0], 403)
            self.assertEqual(s.req("GET", "/tv")[0], 200)
        http = [e for e in a.test_logs if e["event"] == "http"]
        self.assertEqual([e for e in http if e["path"] == "/api/tv/state" and e["status"] == 200], [])
        self.assertEqual(len([e for e in http if e["path"] == "/api/tv/state" and e["status"] == 403]), 3)
        self.assertEqual(len([e for e in http if e["path"] == "/tv"]), 1)
        self.assertEqual(len([e for e in a.test_logs if e["event"] == "http_ok_sample"]), 2)


# --------------------------------------------------------------------------- MINEUR 6 : install.sh post-installation
class TestInstallPostFailure(unittest.TestCase):
    def test_failure_after_point_of_no_return_is_explicit(self):
        for cmd in ("systemd-sysusers", "systemctl daemon-reload", "systemctl restart"):
            with self.subTest(cmd=cmd):
                r = tmpdir(self, "otvrfs")
                env = dict(os.environ, LC_ALL="C", OPENTV_INSTALL_TEST_FAIL=cmd)
                import subprocess
                p = subprocess.run(["bash", INSTALL, "--root", r, "--skip-preflight"], capture_output=True,
                                   timeout=120, env=env)
                err = p.stderr.decode()
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(cmd, err)
                self.assertIn("aucun retour automatique", err)
                self.assertIn("sudo /opt/opentv/uninstall.sh", err)
                self.assertNotIn("retour à l'état précédent", err)          # pas de faux rollback annoncé
                self.assertTrue(os.path.isfile(os.path.join(r, "opt/opentv/MANIFEST.sha256")))

    def test_client_dir_is_root_owned_by_tmpfiles(self):
        with open(os.path.join(PI4, "system", "tmpfiles-opentv.conf")) as f:
            lines = [l.split() for l in f.read().splitlines() if l and not l.startswith("#")]
        self.assertIn(["d", "/etc/opentv", "0755", "root", "root", "-"], lines)
        self.assertIn(["d", "/etc/opentv/agents", "0755", "root", "root", "-"], lines)
        p = sh([INSTALL, "--dry-run", "--root", tmpdir(self, "otvrfs"), "--skip-preflight"])
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
