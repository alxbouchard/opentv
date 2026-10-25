"""Offline test suite for the Python OpenTV autostart watcher. Stdlib unittest only.

Run:  OPENTV_REPO=/path/to/opentv python3 -m unittest -v test_opentv_autostart
Node-based cross-implementation tests are skipped when node or OPENTV_REPO is missing.
"""
from __future__ import annotations

import configparser
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
NANOPI = os.path.join(ROOT, "scripts", "nanopi")
sys.path.insert(0, HERE)
sys.path.insert(0, NANOPI)
import opentv_autostart as oa  # noqa: E402
from faketv import FakeTV  # noqa: E402

DAEMON = os.path.join(NANOPI, "opentv_autostart.py")
REPO = os.environ.get("OPENTV_REPO", "")
NODE = shutil.which("node")

FAST = dict(TV_HOST="127.0.0.1", POLL_S="0.05", OFF_DEBOUNCE_S="0.3", BOOT_DELAY_S="0.1", MAX_ATTEMPTS="3",
            RETRY_BACKOFF_S="0.1", RETRY_BACKOFF_MAX_S="0.4", VERIFY_TIMEOUT_S="1", SDB_TIMEOUT_S="0.8",
            CONNECT_TIMEOUT_S="0.3", SDB_READY_TIMEOUT_S="2", SAME_CYCLE_GRACE_S="5",
            STATE_HEARTBEAT_S="0.2", LAUNCH_ON_START="auto", CEC_CMD="")


def wait_until(pred, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class Daemon:
    """The real daemon in a subprocess: real signals, real stdout/journald-style logs."""

    def __init__(self, tv: FakeTV, tmp, **env):
        self.env = {"PATH": os.environ["PATH"], "HOME": tmp, **FAST, "SDB_PORT": str(tv.sdb_port),
                    "STATUS_PORT": str(tv.status_port), "STATE_FILE": os.path.join(tmp, "state", "state.json"),
                    **env}
        self.lines: list[str] = []
        self.p = None

    def start(self):
        self.p = subprocess.Popen([sys.executable, "-I", DAEMON], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._read, daemon=True).start()
        assert wait_until(lambda: self.events("start"), 5), self.lines
        return self

    def _read(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    @property
    def recs(self):
        out = []
        for l in list(self.lines):
            try:
                out.append(json.loads(l))
            except ValueError:
                out.append({"event": "NON_JSON", "raw": l})
        return out

    def events(self, name, **match):
        return [r for r in self.recs if r.get("event") == name and all(r.get(k) == v for k, v in match.items())]

    def outcomes(self):
        return [r["outcome"] for r in self.events("cycle_outcome")]

    def wait_outcomes(self, n, timeout=8):
        assert wait_until(lambda: len(self.outcomes()) >= n, timeout), "\n".join(self.lines)
        return self.outcomes()

    def stop(self, sig=signal.SIGTERM, timeout=5):
        if self.p.poll() is None:
            t0 = time.monotonic()
            self.p.send_signal(sig)
            rc = self.p.wait(timeout)
            return rc, time.monotonic() - t0
        return self.p.returncode, 0.0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="otv-")
        self.tv = FakeTV()
        self.daemons = []

    def tearDown(self):
        for d in self.daemons:
            if d.p and d.p.poll() is None:
                d.p.kill()
                d.p.wait()
            if d.p and d.p.stdout:
                d.p.stdout.close()
        self.tv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def daemon(self, **env):
        d = Daemon(self.tv, self.tmp, **env).start()
        self.daemons.append(d)
        return d

    def debug(self, d):
        return "\n".join(d.lines)


# ====================================================================== unit
class Unit(unittest.TestCase):
    def test_sdb_frame_layout(self):
        m = oa.sdb_msg("OPEN", 1, 0, b"shell:x\0")
        self.assertEqual(m[:4], b"OPEN")
        self.assertEqual(m[20:24], bytes(b ^ 0xFF for b in b"OPEN"))
        self.assertEqual(int.from_bytes(m[12:16], "little"), 8)
        self.assertEqual(int.from_bytes(m[16:20], "little"), sum(b"shell:x\0"))

    def test_parse_partial_and_multiple_frames(self):
        a, b = oa.sdb_msg("WRTE", 9, 1, b"hello"), oa.sdb_msg("CLSE", 9, 1)
        buf = bytearray(a[:10])
        self.assertEqual(list(oa.sdb_parse(buf)), [])
        buf += a[10:] + b
        got = list(oa.sdb_parse(buf))
        self.assertEqual([g[0] for g in got], ["WRTE", "CLSE"])
        self.assertEqual(got[0][3], b"hello")
        self.assertEqual(buf, bytearray())

    def test_parse_rejects_absurd_length(self):
        bad = bytearray(b"WRTE" + (0).to_bytes(4, "little") * 2 + (1 << 30).to_bytes(4, "little") + b"\0" * 8)
        with self.assertRaises(ValueError):
            list(oa.sdb_parse(bad))

    def test_launch_ok_matrix(self):
        for out, exp in [("app_id[OpnTV00001] launched", True), ("... cmd_ret:0", True),
                         ("spend_time:12 cmd_ret:1", False), ("launch failed cmd_ret:0", False),
                         ("closed", False), ("timeout", False), ("cmd_ret:01", False), ("", False)]:
            self.assertEqual(oa.launch_ok(out), exp, out)

    def test_cec_parse(self):
        self.assertTrue(oa.parse_cec_power("\tpwr-state: on\n"))
        self.assertFalse(oa.parse_cec_power("pwr-state: standby"))
        self.assertFalse(oa.parse_cec_power("pwr-state: to-on"))
        self.assertTrue(oa.parse_cec_power("power status: on"))
        self.assertIsNone(oa.parse_cec_power("Transmit failed"))
        self.assertIsNone(oa.parse_cec_power(""))

    def test_config_defaults_and_rejects(self):
        c = oa.load_config({})
        self.assertEqual((c["tv"], c["sdb_port"], c["status_port"], c["app"]),
                         ("192.168.2.14", 26101, 8096, "OpnTV00001.OpenTV"))
        self.assertEqual(c["launch_on_start"], "0")
        for bad in [{"LAUNCH_ON_START": "yes"}, {"APP_ID": "x; reboot"}, {"TV_HOST": "1.2.3.4;id"},
                    {"MAX_ATTEMPTS": "0"}, {"MAX_ATTEMPTS": "999"}, {"OFF_DEBOUNCE_S": "0"}]:
            with self.assertRaises(ValueError, msg=bad):
                oa.load_config(bad)

    def test_invalid_config_exits_2_without_traceback(self):
        r = subprocess.run([sys.executable, "-I", DAEMON], env={"PATH": os.environ["PATH"], "APP_ID": "bad id"},
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["event"], "config_invalid")
        self.assertNotIn("Traceback", r.stdout + r.stderr)


# ====================================================================== power cycles
class PowerCycles(Base):
    def test_cold_start_then_three_off_on_cycles_one_launch_each(self):
        d = self.daemon()
        assert wait_until(lambda: d.events("tv_off_at_start"))
        for n in range(1, 4):
            self.tv.power_on()
            self.assertEqual(d.wait_outcomes(n)[-1], "launched_verified", self.debug(d))
            time.sleep(0.6)                      # stays on: no relaunch
            self.assertEqual(self.tv.launches, n)
            self.tv.power_off()
            assert wait_until(lambda: len(d.events("tv_off")) >= n, 3), self.debug(d)
        self.assertEqual(self.tv.commands, ["shell:0 was_execute OpnTV00001.OpenTV"] * 3)

    def test_opentv_already_running_sends_nothing(self):
        d = self.daemon()
        self.tv.power_on(app_running=True)
        self.assertEqual(d.wait_outcomes(1), ["already_running"])
        self.assertEqual(self.tv.commands, [])

    def test_status_port_open_but_not_healthy_is_not_running(self):
        self.tv.status_mode = "tcp_only"         # repo faketv/autostart.mjs would call this "running"
        d = self.daemon()
        self.tv.power_on(app_running=True)
        threading.Timer(0.5, lambda: setattr(self.tv, "status_mode", "json")).start()
        out = d.wait_outcomes(1)
        self.assertNotEqual(out[0], "already_running", self.debug(d))
        self.assertGreaterEqual(self.tv.launches, 1)

    def test_unhealthy_status_variants_are_not_running(self):
        for mode in ("not_ok", "500", "garbage"):
            with self.subTest(mode=mode):
                self.assertIsNone(self._probe(mode))
        self.assertEqual(self._probe("json")["client"]["build"], "OpenTV v20 native")

    def _probe(self, mode):
        self.tv.status_mode = mode
        self.tv._start_status()
        cfg = oa.load_config({"TV_HOST": "127.0.0.1", "STATUS_PORT": str(self.tv.status_port),
                              "CONNECT_TIMEOUT_S": "0.3", "STATE_FILE": os.path.join(self.tmp, "s.json")})
        return oa.Watcher(cfg, out=io.StringIO()).status()

    def test_status_ok_false_triggers_launch(self):
        self.tv.status_mode = "not_ok"
        d = self.daemon()
        self.tv.power_on(app_running=True)
        threading.Timer(0.5, lambda: setattr(self.tv, "status_mode", "json")).start()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"], self.debug(d))
        self.assertEqual(self.tv.launches, 1)

    def test_tv_already_on_at_daemon_start_launch_on_start_0(self):
        self.tv.power_on()
        d = self.daemon(LAUNCH_ON_START="0")
        self.assertEqual(d.wait_outcomes(1), ["skipped_on_start"])
        time.sleep(0.5)
        self.assertEqual(self.tv.launches, 0)

    def test_user_exits_opentv_while_tv_on_no_relaunch(self):
        d = self.daemon()
        self.tv.power_on()
        d.wait_outcomes(1)
        self.tv.app_exit()                       # user switched to Netflix
        time.sleep(1.0)
        self.assertEqual(self.tv.launches, 1)

    def test_tv_off_during_boot_delay_aborts_without_command(self):
        d = self.daemon(BOOT_DELAY_S="0.4", SDB_READY_TIMEOUT_S="0.8")
        self.tv.power_on()
        assert wait_until(lambda: d.events("tv_on"))
        self.tv.power_off()
        self.assertEqual(d.wait_outcomes(1), ["aborted_tv_off"])
        self.assertEqual(self.tv.commands, [])

    def test_dry_run(self):
        d = self.daemon(DRY_RUN="1")
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1), ["dry_run"])
        self.assertEqual(self.tv.commands, [])


# ====================================================================== readiness / retries
class Retries(Base):
    def test_delayed_sdb_handshake_then_ready(self):
        self.tv.handshake = "silent"             # port listening, sdbd not answering yet (booting)
        threading.Timer(1.0, lambda: setattr(self.tv, "handshake", "ok")).start()
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"], self.debug(d))
        self.assertEqual(self.tv.launches, 1)
        self.assertGreaterEqual(len(d.events("launch_attempt")), 2)

    def test_sdb_port_opens_late_does_not_burn_attempts(self):
        d = self.daemon(CEC_CMD="", SDB_READY_TIMEOUT_S="3")
        self.tv.power_on()
        assert wait_until(lambda: d.events("tv_on"))
        self.tv._stop_sdb()                       # port disappears during boot delay ...
        threading.Timer(0.25, self.tv._start_sdb).start()   # ... and comes back (< debounce)
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"], self.debug(d))
        self.assertEqual(len(d.events("launch_attempt")), 1)

    def test_host_pc_ip_mismatch_bounded_retries_hint_once_daemon_alive(self):
        self.tv.handshake = "silent"
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1, 10), ["handshake_failed"], self.debug(d))
        self.assertEqual(len(d.events("launch_attempt")), 3)
        self.assertEqual(len(d.events("hint")), 1)
        time.sleep(0.8)                           # TV stays on: no more attempts
        self.assertEqual(len(d.events("launch_attempt")), 3)
        self.assertIsNone(d.p.poll())
        self.tv.power_off()
        assert wait_until(lambda: d.events("tv_off"), 3)
        self.tv.power_on()                        # next power cycle retries again (bounded)
        d.wait_outcomes(2, 10)
        self.assertEqual(len(d.events("launch_attempt")), 6)

    def test_handshake_closed_immediately(self):
        self.tv.handshake = "close"
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1, 10), ["handshake_failed"])
        self.assertEqual(self.tv.commands, [])

    def test_tv_rejects_launch_bounded(self):
        self.tv.reply = [b"spend_time:3 cmd_ret:1\n"]
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1, 10), ["rejected"])
        time.sleep(0.6)
        self.assertEqual(self.tv.launches, 3)

    def test_launch_accepted_but_status_never_appears_partial_failure(self):
        self.tv.app_starts_after = None
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1, 12), ["launch_unverified"])
        self.assertEqual(self.tv.launches, 3)
        self.assertEqual(d.events("cycle_outcome")[0]["level"], "error")

    def test_fragmented_tcp_and_multi_wrte(self):
        self.tv.split_frames = True
        self.tv.reply = [b"app_id[OpnTV00001] ", b"launched\n"]
        d = self.daemon()
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"], self.debug(d))

    def test_backoff_is_exponential_and_capped(self):
        self.tv.reply = [b"cmd_ret:1"]
        d = self.daemon(MAX_ATTEMPTS="4", RETRY_BACKOFF_S="0.1", RETRY_BACKOFF_MAX_S="0.2")
        self.tv.power_on()
        d.wait_outcomes(1, 10)
        self.assertEqual(self.tv.launches, 4)


# ====================================================================== network flaps / CEC
class Flaps(Base):
    def test_short_flap_is_not_a_power_cycle(self):
        d = self.daemon()
        self.tv.power_on()
        d.wait_outcomes(1)
        self.tv.flap(0.12)                        # < OFF_DEBOUNCE_S (0.3 s)
        time.sleep(0.8)
        self.assertEqual(d.events("tv_off"), [], self.debug(d))
        self.assertEqual(self.tv.launches, 1)

    def test_long_flap_with_app_alive_is_already_running(self):
        d = self.daemon()
        self.tv.power_on()
        d.wait_outcomes(1)
        self.tv._stop_sdb()                       # network down longer than the debounce ...
        self.tv._stop_status()
        assert wait_until(lambda: d.events("tv_off"), 5), self.debug(d)
        self.tv._start_sdb()                      # ... back: looks like off->on, but OpenTV still answers
        self.tv._start_status()
        self.assertEqual(d.wait_outcomes(2), ["launched_verified", "already_running"], self.debug(d))
        self.assertEqual(self.tv.launches, 1)

    def _cec(self, text):
        p = os.path.join(self.tmp, "cec.txt")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_cec_standby_vetoes_open_sdb_then_on_launches(self):
        p = self._cec("pwr-state: standby\n")
        d = self.daemon(CEC_CMD=f"cat {p}")
        self.tv.power_on()
        time.sleep(0.6)
        self.assertEqual(self.tv.launches, 0)
        self._cec("pwr-state: on\n")
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"])
        self.assertEqual(self.tv.launches, 1)

    def test_cec_unreadable_fails_closed_then_valid_state_launches(self):
        p = self._cec("Transmit failed\n")
        d = self.daemon(CEC_CMD=f"cat {p}")
        self.tv.power_on()
        time.sleep(0.6)
        self.assertEqual(self.tv.launches, 0)
        self._cec("power status: on\n")
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"])


# ====================================================================== lifecycle / persistence
class Lifecycle(Base):
    def test_sigterm_idle_and_mid_handshake(self):
        d = self.daemon()
        rc, dt = d.stop()
        self.assertEqual((rc, d.events("stop") != []), (0, True))
        self.assertLess(dt, 2)
        self.tv.handshake = "silent"
        d2 = self.daemon(SDB_TIMEOUT_S="20")
        self.tv.power_on()
        assert wait_until(lambda: self.tv.connections >= 2, 3)
        time.sleep(0.3)
        rc, dt = d2.stop()
        self.assertEqual(rc, 0)
        self.assertLess(dt, 2, "SIGTERM must interrupt a hung SDB session")

    def test_restart_same_cycle_no_relaunch(self):
        d = self.daemon()
        self.tv.power_on()
        d.wait_outcomes(1)
        time.sleep(0.3)                           # let a heartbeat land
        d.stop(signal.SIGKILL)                    # crash / systemd restart / NanoPi reboot while TV on
        self.tv.app_exit()                        # even if the user left OpenTV meanwhile
        d2 = self.daemon()
        self.assertEqual(d2.wait_outcomes(1), ["skipped_same_cycle"], self.debug(d2))
        self.assertEqual(self.tv.launches, 1)

    def test_crash_mid_cycle_resumes_launch(self):
        self.tv.handshake = "silent"
        d = self.daemon(SDB_TIMEOUT_S="20")
        self.tv.power_on()
        assert wait_until(lambda: self.tv.connections >= 2, 3)
        d.stop(signal.SIGKILL)
        self.tv.handshake = "ok"
        d2 = self.daemon()
        self.assertEqual(d2.wait_outcomes(1), ["launched_verified"], self.debug(d2))
        self.assertEqual(self.tv.launches, 1)

    def _state(self, **kw):
        p = os.path.join(self.tmp, "state", "state.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(kw.pop("raw", None) or json.dumps(kw))

    def test_stale_state_power_outage_recovery_launches(self):
        self._state(handled=True, outcome="launched_verified", last_seen_on=time.time() - 3600)
        self.tv.power_on()
        d = self.daemon()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"])

    def test_off_seen_after_last_on_is_new_cycle(self):
        now = time.time()
        self._state(handled=True, last_seen_on=now - 2, last_seen_off=now - 1)
        self.tv.power_on()
        d = self.daemon()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"])

    def test_launch_on_start_0_ignores_stale_state(self):
        self._state(handled=True, last_seen_on=time.time() - 3600)
        self.tv.power_on()
        d = self.daemon(LAUNCH_ON_START="0")
        self.assertEqual(d.wait_outcomes(1), ["skipped_on_start"])
        self.assertEqual(self.tv.launches, 0)

    def test_corrupt_state_file_is_ignored(self):
        self._state(raw="{not json")
        d = self.daemon()
        assert wait_until(lambda: d.events("state_corrupt_ignored"))
        self.tv.power_on()
        self.assertEqual(d.wait_outcomes(1), ["launched_verified"])
        with open(os.path.join(self.tmp, "state", "state.json")) as f:
            self.assertTrue(json.load(f)["handled"])

    def test_state_writes_are_atomic_and_rate_limited(self):
        d = self.daemon(STATE_HEARTBEAT_S="0.5")
        self.tv.power_on()
        d.wait_outcomes(1)
        p = os.path.join(self.tmp, "state", "state.json")
        m = []
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            with open(p) as f:
                json.load(f)                      # never torn
            m.append(os.stat(p).st_mtime_ns)
            time.sleep(0.01)
        self.assertLessEqual(len(set(m)), 4, "heartbeat must not write every poll")
        self.assertFalse(os.path.exists(p + ".tmp"))


# ====================================================================== logging
class Logging(Base):
    def test_json_lines_bounded_noise_no_secrets(self):
        d = self.daemon()
        time.sleep(1.5)                           # ~30 polls with TV off
        self.assertLessEqual(len(d.lines), 3, self.debug(d))
        self.tv.handshake = "silent"
        self.tv.power_on()
        d.wait_outcomes(1, 10)
        for r in d.recs:
            self.assertNotEqual(r["event"], "NON_JSON", r)
            self.assertTrue({"ts", "level", "event"} <= r.keys(), r)
        blob = "\n".join(d.lines).lower()
        for bad in ("password", "author.p12", "begin private", "traceback"):
            self.assertNotIn(bad, blob)
        self.assertEqual(d.events("start")[0]["python"].split(".")[0], "3")


# ====================================================================== cross implementation (repo Node)
@unittest.skipUnless(NODE and REPO and os.path.isfile(os.path.join(REPO, "scripts", "autostart.mjs")),
                     "needs node + OPENTV_REPO")
class CrossImpl(Base):
    def test_frame_bytes_identical_to_repo_autostart_mjs(self):
        with open(os.path.join(REPO, "scripts", "autostart.mjs")) as f:
            src = f.read()
        helpers = "\n".join(l for l in src.splitlines() if re.match(r"const (u32|sum|msg) =", l))
        js = helpers + ('\nconsole.log(msg("CNXN",0x01000000,0x1000,Buffer.from("host::\\0")).toString("hex"));'
                        '\nconsole.log(msg("OPEN",1,0,Buffer.from("shell:0 was_execute OpnTV00001.OpenTV\\0"))'
                        '.toString("hex"));\nconsole.log(msg("OKAY",1,9).toString("hex"));')
        r = subprocess.run([NODE, "--input-type=module", "-e", js], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), [
            oa.sdb_msg("CNXN", 0x01000000, 0x1000, b"host::\0").hex(),
            oa.sdb_msg("OPEN", 1, 0, b"shell:0 was_execute OpnTV00001.OpenTV\0").hex(),
            oa.sdb_msg("OKAY", 1, 9).hex()])

    def test_repo_node_autostart_against_python_faketv(self):
        """Proves FakeTV speaks the protocol the repo's working client expects."""
        self.tv.power_on()
        env = {"PATH": os.environ["PATH"], "SDB_PORT": str(self.tv.sdb_port),
               "STATUS_PORT": str(self.tv.status_port), "POLL_MS": "100", "BOOT_DELAY_MS": "100"}
        p = subprocess.Popen([NODE, os.path.join(REPO, "scripts", "autostart.mjs"), "127.0.0.1"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            ok = wait_until(lambda: self.tv.launches >= 1, 6)
            time.sleep(0.8)
        finally:
            p.terminate()
            out = p.communicate(timeout=5)[0]
        self.assertTrue(ok, out)
        self.assertEqual(self.tv.commands, ["shell:0 was_execute OpnTV00001.OpenTV"], out)
        self.assertIn("OK", out)

    def test_python_daemon_against_repo_node_faketv(self):
        """Repo faketv.mjs: SDB 36101, status 18096 (TCP-only, no HTTP), on 0-14 s, off 14-20 s, on 20-34 s."""
        import fcntl
        lock = open(os.path.join(tempfile.gettempdir(), "opentv-faketv-fixed-ports.lock"), "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)          # repo faketv.mjs hardcodes its ports: serialize runs
        fake = subprocess.Popen([NODE, os.path.join(REPO, "native", "test", "faketv.mjs")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.5)
        d = Daemon(self.tv, self.tmp, SDB_PORT="36101", STATUS_PORT="18096", POLL_S="0.5", OFF_DEBOUNCE_S="1.5",
                   MAX_ATTEMPTS="1", VERIFY_TIMEOUT_S="2", BOOT_DELAY_S="0.5").start()
        self.daemons.append(d)
        try:
            out = fake.communicate(timeout=45)[0]
        finally:
            if fake.poll() is None:
                fake.kill()
        d.stop()
        self.assertIn("total lancements: 2", out, out + self.debug(d))
        # repo fake status is TCP-only -> strict /status check can never verify: documented incompatibility
        self.assertEqual(d.outcomes(), ["launch_unverified", "launch_unverified"], self.debug(d))


# ====================================================================== deployment artifacts
class Deploy(unittest.TestCase):
    UNIT = os.path.join(NANOPI, "opentv-autostart.service")

    def test_unit_file_contract(self):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(self.UNIT)
        s = cp["Service"]
        self.assertTrue(s["ExecStart"].startswith("/usr/bin/python3 -I "))
        self.assertEqual(s["Restart"], "on-failure")
        self.assertNotIn("User", s)                      # user unit, never root
        self.assertEqual(cp["Install"]["WantedBy"], "default.target")
        self.assertIn("StartLimitBurst", cp["Unit"])
        self.assertLessEqual(int(s["TimeoutStopSec"]), 15)
        with open(self.UNIT) as f:
            self.assertNotIn("muse", f.read().lower())

    def _shim(self, home):
        b = os.path.join(home, "bin")
        os.makedirs(b)
        log = os.path.join(home, "calls.log")
        for tool in ("systemctl", "loginctl", "systemd-analyze"):
            with open(os.path.join(b, tool), "w") as f:
                f.write(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n'
                        'case "$*" in *"is-active --quiet opentv"*) exit 3;; esac\nexit 0\n')
            os.chmod(os.path.join(b, tool), 0o755)
        return b, log

    def test_install_then_rollback_twice_with_shims(self):
        home = tempfile.mkdtemp(prefix="otv-home-")
        try:
            b, log = self._shim(home)
            env = {"HOME": home, "PATH": b + os.pathsep + os.environ["PATH"]}
            for script in ("install.sh", "rollback.sh", "rollback.sh"):
                r = subprocess.run(["bash", os.path.join(NANOPI, script)], env=env,
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, script + r.stdout + r.stderr)
            with open(log) as f:
                calls = f.read().splitlines()
            self.assertIn("systemctl --user enable --now opentv-autostart.service", calls)
            self.assertEqual(calls.count("systemctl --user disable --now opentv-autostart.service"), 2)
            muse = [c for c in calls if "muse" in c]
            self.assertTrue(muse and all(" is-active " in c for c in muse), muse)   # read-only only
            self.assertFalse(os.path.exists(os.path.join(home, ".config/systemd/user/opentv-autostart.service")))
            with open(os.path.join(home, ".config/systemd/user/opentv-autostart.env")) as f:
                env_file = f.read()
            self.assertIn("DRY_RUN=1", env_file)          # first install is observe-only
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_scripts_parse(self):
        for s in ("install.sh", "rollback.sh"):
            r = subprocess.run(["bash", "-n", os.path.join(NANOPI, s)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
