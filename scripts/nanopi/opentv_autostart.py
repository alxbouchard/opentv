#!/usr/bin/env python3
"""OpenTV autostart watcher (Python 3.13, stdlib only) -- reference prototype for offline tests.

Launches OpnTV00001.OpenTV with the raw SDB protocol (same bytes as scripts/autostart.mjs)
once per real TV off->on transition. Logs JSON lines to stdout (journald).

Outcomes per power cycle: launched_verified | already_running | launch_unverified |
handshake_failed | rejected | aborted_tv_off | skipped_same_cycle | skipped_on_start | dry_run.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------- SDB wire format (== autostart.mjs)
A_VERSION = 0x01000000
MAX_DATA = 0x1000


def sdb_msg(cmd: str, a0: int, a1: int, payload: bytes = b"") -> bytes:
    c = cmd.encode("ascii")
    magic = bytes(b ^ 0xFF for b in c)
    return c + struct.pack("<IIII", a0 & 0xFFFFFFFF, a1 & 0xFFFFFFFF, len(payload),
                           sum(payload) & 0xFFFFFFFF) + magic + payload


def sdb_parse(buf: bytearray):
    """Yield (cmd, a0, a1, payload) for every complete frame; consumes buf in place."""
    while len(buf) >= 24:
        cmd = bytes(buf[:4]).decode("latin1")
        a0, a1, ln = struct.unpack_from("<III", buf, 4)
        if ln > 1 << 20:
            raise ValueError("frame too large")
        if len(buf) < 24 + ln:
            return
        payload = bytes(buf[24:24 + ln])
        del buf[:24 + ln]
        yield cmd, a0, a1, payload


_OK_RE = re.compile(r"launched|cmd_ret:0\b")
_BAD_RE = re.compile(r"cmd_ret:[1-9]|fail|error|denied", re.I)


def launch_ok(out: str) -> bool:
    # Repo semantics (/launched|cmd_ret:0/) but stricter: an explicit failure marker wins.
    return bool(_OK_RE.search(out)) and not _BAD_RE.search(out)


def sdb_shell(host, port, cmd, timeout, connect_timeout, stop: threading.Event):
    """Returns dict(phase=..., out=...). phase: connect_failed|handshake_failed|open_timeout|done|stopped."""
    deadline = time.monotonic() + timeout
    try:
        s = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as e:
        return {"phase": "connect_failed", "out": type(e).__name__}
    buf, out, got_cnxn = bytearray(), "", False
    try:
        s.sendall(sdb_msg("CNXN", A_VERSION, MAX_DATA, b"host::\0"))
        s.settimeout(0.25)
        while True:
            if stop.is_set():
                return {"phase": "stopped", "out": out}
            if time.monotonic() > deadline:
                return {"phase": "open_timeout" if got_cnxn else "handshake_failed", "out": out or "timeout"}
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                return {"phase": "done" if got_cnxn and out else ("open_timeout" if got_cnxn else "handshake_failed"),
                        "out": out or "closed"}
            buf += d
            for c, a0, a1, p in sdb_parse(buf):
                if c == "CNXN":
                    got_cnxn = True
                    s.sendall(sdb_msg("OPEN", 1, 0, ("shell:" + cmd + "\0").encode()))
                elif c == "WRTE":
                    out += p.decode("latin1")
                    s.sendall(sdb_msg("OKAY", a1, a0))
                elif c == "CLSE":
                    try:
                        s.sendall(sdb_msg("CLSE", a1, a0))
                    except OSError:
                        pass
                    return {"phase": "done", "out": out.strip()}
    except (OSError, ValueError) as e:
        return {"phase": "handshake_failed" if not got_cnxn else "open_timeout", "out": type(e).__name__}
    finally:
        s.close()


# ---------------------------------------------------------------- config
def _f(env, k, d):
    return float(env.get(k, d))


def load_config(env=None):
    env = os.environ if env is None else env
    cfg = {
        "tv": env.get("TV_HOST", "192.168.2.14"),
        "app": env.get("APP_ID", "OpnTV00001.OpenTV"),
        "sdb_port": int(env.get("SDB_PORT", 26101)),
        "status_port": int(env.get("STATUS_PORT", 8096)),
        "poll": _f(env, "POLL_S", 3),
        "off_debounce_s": _f(env, "OFF_DEBOUNCE_S", 15),
        "boot_delay": _f(env, "BOOT_DELAY_S", 6),
        "sdb_ready_timeout": _f(env, "SDB_READY_TIMEOUT_S", 90),
        "max_attempts": int(env.get("MAX_ATTEMPTS", 3)),
        "backoff": _f(env, "RETRY_BACKOFF_S", 4),
        "backoff_max": _f(env, "RETRY_BACKOFF_MAX_S", 30),
        "verify_timeout": _f(env, "VERIFY_TIMEOUT_S", 30),
        "sdb_timeout": _f(env, "SDB_TIMEOUT_S", 20),
        "connect_timeout": _f(env, "CONNECT_TIMEOUT_S", 1.5),
        "launch_on_start": env.get("LAUNCH_ON_START", "0"),
        "same_cycle_grace": _f(env, "SAME_CYCLE_GRACE_S", 180),
        "heartbeat": _f(env, "STATE_HEARTBEAT_S", 60),
        "state_file": env.get("STATE_FILE", os.path.expanduser("~/.local/state/opentv-autostart/state.json")),
        "dry_run": env.get("DRY_RUN", "0") == "1",
        "cec_cmd": env.get("CEC_CMD", ""),
        "disabled_file": env.get("DISABLED_FILE", os.path.expanduser("~/.local/state/opentv-autostart/disabled")),
        "suppress_until_file": env.get("SUPPRESS_UNTIL_FILE", os.path.expanduser("~/.local/state/opentv-autostart/suppress-until")),
    }
    errs = []
    if cfg["launch_on_start"] not in ("0", "1", "auto"):
        errs.append("LAUNCH_ON_START must be 0|1|auto")
    if not re.fullmatch(r"[A-Za-z0-9]{10}\.[A-Za-z0-9_]+", cfg["app"]):
        errs.append("APP_ID invalid")
    if not re.fullmatch(r"[0-9A-Za-z.\-:]+", cfg["tv"]):
        errs.append("TV_HOST invalid")
    if not 1 <= cfg["max_attempts"] <= 10:
        errs.append("MAX_ATTEMPTS must be 1..10")
    if cfg["poll"] < 0.01 or cfg["off_debounce_s"] < cfg["poll"]:
        errs.append("POLL_S/OFF_DEBOUNCE_S out of range")
    if errs:
        raise ValueError("; ".join(errs))
    return cfg


def parse_cec_power(text: str):
    m = re.search(r"(?:pwr-state|power status)\s*:\s*([a-z\- ]+)", text, re.I)
    if not m:
        return None
    v = m.group(1).strip().lower()
    if v == "on":
        return True
    if v in ("standby", "to-on", "to-standby", "in transition from standby to on",
             "in transition from on to standby", "in transition standby to on", "in transition on to standby"):
        return False
    return None


# ---------------------------------------------------------------- watcher
class Watcher:
    def __init__(self, cfg, stop=None, out=sys.stdout):
        self.cfg, self.stop, self.out = cfg, stop or threading.Event(), out
        self.state = "unknown"           # unknown | on | off
        self.unreachable_since = None
        self.cycle_handled = False
        self.last_err = None
        self.persist = self._load_state()
        self._last_hb = 0.0

    # -- logging: JSON lines, state-change only for repeated errors, never secrets
    def log(self, event, level="info", **kw):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "level": level, "event": event, **kw}
        self.out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.out.flush()

    # -- persistence (atomic, low write rate)
    def _load_state(self):
        try:
            with open(self.cfg["state_file"]) as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("not an object")
            return d
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self.log("state_corrupt_ignored", "warning", error=type(e).__name__)
            return {}

    def _save_state(self, **kw):
        self.persist.update(kw, saved_at=time.time())
        p = self.cfg["state_file"]
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.persist, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    # -- probes
    def sdb_port_open(self):
        try:
            with socket.create_connection((self.cfg["tv"], self.cfg["sdb_port"]), timeout=self.cfg["connect_timeout"]):
                return True
        except OSError:
            return False

    def cec_on(self):
        if not self.cfg["cec_cmd"]:
            return None
        try:
            r = subprocess.run(shlex.split(self.cfg["cec_cmd"]), capture_output=True, text=True, timeout=8)
            return parse_cec_power(r.stdout + r.stderr)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def power_on(self):
        sdb = self.sdb_port_open()
        if not self.cfg["cec_cmd"]:
            return sdb
        cec = self.cec_on()
        # If CEC is configured, fail closed: unreadable or transitional state never launches OpenTV.
        return sdb and cec is True

    def status(self):
        url = f"http://{self.cfg['tv']}:{self.cfg['status_port']}/status"
        try:
            with urllib.request.urlopen(url, timeout=self.cfg["connect_timeout"] + 1) as r:
                if r.status != 200:
                    return None
                d = json.loads(r.read(4096).decode("utf-8", "replace"))
                client = d.get("client") if isinstance(d, dict) else None
                return d if (d.get("ok") is True and isinstance(client, dict)
                             and isinstance(client.get("build"), str) and client["build"]) else None
        except Exception:
            return None

    # -- launch sequence for one power cycle
    def _wait(self, s):
        return self.stop.wait(s)

    def _still_on(self):
        return self.power_on()

    def run_cycle(self):
        c = self.cfg
        if os.path.exists(c["disabled_file"]):
            return self._finish("disabled")
        try:
            with open(c["suppress_until_file"]) as f:
                if float(f.read().strip()) > time.time():
                    return self._finish("suppressed")
        except (FileNotFoundError, OSError, ValueError):
            pass
        if self._wait(c["boot_delay"]):
            return "stopped"
        st = self.status()
        if st is not None:
            return self._finish("already_running", version=st.get("version"))
        if c["dry_run"]:
            return self._finish("dry_run")
        backoff = c["backoff"]
        hint_logged = False
        ready_deadline = time.monotonic() + c["sdb_ready_timeout"]
        attempt = 0
        while attempt < c["max_attempts"]:
            if not self.sdb_port_open():
                # delayed SDB readiness: wait (bounded) without consuming attempts
                if time.monotonic() > ready_deadline:
                    return self._finish("aborted_tv_off")
                if self._wait(c["poll"]):
                    return "stopped"
                continue
            attempt += 1
            st = self.status()
            if st is not None:
                return self._finish("already_running", version=st.get("version"))
            r = sdb_shell(c["tv"], c["sdb_port"], "0 was_execute " + c["app"], c["sdb_timeout"],
                          c["connect_timeout"], self.stop)
            if r["phase"] == "stopped":
                return "stopped"
            ok = r["phase"] == "done" and launch_ok(r["out"])
            self.log("launch_attempt", "info" if ok else "warning", attempt=attempt, phase=r["phase"],
                     ok=ok, out=re.sub(r"\s+", " ", r["out"])[:120])
            if r["phase"] == "handshake_failed" and not hint_logged:
                self.log("hint", "warning", msg="SDB handshake unanswered: Developer Mode Host PC IP is "
                         "probably not this device")
                hint_logged = True
            if ok:
                vdl = time.monotonic() + c["verify_timeout"]
                while time.monotonic() < vdl:
                    st = self.status()
                    if st is not None:
                        return self._finish("launched_verified", attempts=attempt, version=st.get("version"))
                    if self._wait(min(1.0, c["poll"])):
                        return "stopped"
                last = "launch_unverified"
            else:
                last = {"handshake_failed": "handshake_failed", "connect_failed": "handshake_failed",
                        "open_timeout": "rejected", "done": "rejected"}[r["phase"]]
            if attempt < c["max_attempts"]:
                if self._wait(backoff):
                    return "stopped"
                backoff = min(backoff * 2, c["backoff_max"])
        return self._finish(last, attempts=attempt)

    def _finish(self, outcome, **kw):
        lvl = "info" if outcome in ("launched_verified", "already_running", "dry_run", "disabled", "suppressed") else "error"
        self.log("cycle_outcome", lvl, outcome=outcome, **kw)
        self.cycle_handled = True
        self._save_state(handled=True, outcome=outcome, last_seen_on=time.time())
        return outcome

    # -- main loop step (one poll)
    def step(self):
        on = self.power_on()
        now = time.time()
        if on:
            self.unreachable_since = None
            if self.state != "on":
                prev = self.state
                self.state = "on"
                self.cycle_handled = False
                self.log("tv_on", prev=prev)
                if prev == "unknown" and not self._should_launch_on_start(now):
                    self.cycle_handled = True
                    self._save_state(handled=True, last_seen_on=now)
                    return
                self._save_state(handled=False, last_seen_on=now)
                self.run_cycle()
            elif now - self._last_hb >= self.cfg["heartbeat"]:
                self._last_hb = now
                self._save_state(last_seen_on=now)
        else:
            if self.unreachable_since is None:
                self.unreachable_since = now
            unreachable_for = now - self.unreachable_since
            if self.state == "on" and unreachable_for >= self.cfg["off_debounce_s"]:
                self.state = "off"
                self.log("tv_off", unreachable_s=round(unreachable_for, 3))
                self._save_state(last_seen_off=now)
            elif self.state == "on" and unreachable_for < self.cfg["poll"] * 1.5:
                self.log("tv_unreachable_debouncing", "debug")
            elif self.state == "unknown":
                self.state = "off"
                self.log("tv_off_at_start")

    def _should_launch_on_start(self, now):
        mode = self.cfg["launch_on_start"]
        if mode == "0":
            self.log("cycle_outcome", outcome="skipped_on_start")
            return False
        p = self.persist
        same = (p.get("last_seen_on") and now - float(p["last_seen_on"]) < self.cfg["same_cycle_grace"]
                and float(p.get("last_seen_off") or 0) < float(p["last_seen_on"]))
        if same and p.get("handled"):
            self.log("cycle_outcome", outcome="skipped_same_cycle")
            return False
        if mode == "1" or not same or not p.get("handled"):
            return True
        return False

    def run(self):
        c = self.cfg
        self.log("start", tv=c["tv"], app=c["app"], sdb_port=c["sdb_port"], poll=c["poll"],
                 launch_on_start=c["launch_on_start"], dry_run=c["dry_run"], cec=bool(c["cec_cmd"]),
                 python=sys.version.split()[0])
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as e:  # keep the daemon alive; systemd restarts only on hard crash
                self.log("step_error", "error", error=f"{type(e).__name__}: {e}"[:200])
            self.stop.wait(c["poll"])
        self.log("stop")


def main():
    try:
        cfg = load_config()
    except ValueError as e:
        print(json.dumps({"level": "error", "event": "config_invalid", "error": str(e)}), flush=True)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    Watcher(cfg, stop).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
