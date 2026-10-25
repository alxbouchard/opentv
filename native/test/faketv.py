"""Controllable fake Samsung TV: raw SDB server (CNXN/OPEN/WRTE/CLSE) + OpenTV HTTP /status.

Every switch is thread-safe and can be flipped while a daemon under test is running.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentv_autostart import sdb_msg, sdb_parse


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeTV:
    def __init__(self):
        self.sdb_port, self.status_port = free_port(), free_port()
        self.lock = threading.Lock()
        self.commands: list[str] = []
        self.connections = 0
        # behaviour knobs
        self.handshake = "ok"            # ok | silent | close
        self.reply = [b"app_id[OpnTV00001] launched\n"]   # WRTE chunks for OPEN
        self.split_frames = False        # deliver bytes one at a time (TCP fragmentation)
        self.app_starts_after = 0.2      # seconds after OPEN before /status answers; None = never
        self.status_mode = "json"        # json | not_ok | 500 | garbage | tcp_only
        self._sdb_srv = self._status_srv = None
        self._clients: list[socket.socket] = []
        self.app_running = False

    # ---------------- power
    def power_on(self, sdb_delay=0.0, app_running=False):
        self.app_running = app_running
        if app_running:
            self._start_status()
        if sdb_delay:
            threading.Timer(sdb_delay, self._start_sdb).start()
        else:
            self._start_sdb()

    def power_off(self):
        self._stop_sdb()
        self._stop_status()
        self.app_running = False

    def flap(self, seconds):
        """Network drop while the TV stays on (app keeps running)."""
        was = self.app_running
        self._stop_sdb()
        self._stop_status()
        time.sleep(seconds)
        self._start_sdb()
        if was:
            self._start_status()

    def app_exit(self):
        self._stop_status()
        self.app_running = False

    def close(self):
        self.power_off()

    # ---------------- SDB server
    def _start_sdb(self):
        with self.lock:
            if self._sdb_srv:
                return
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", self.sdb_port))
            srv.listen(16)
            self._sdb_srv = srv
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _stop_sdb(self):
        with self.lock:
            srv, self._sdb_srv = self._sdb_srv, None
            clients, self._clients = self._clients, []
        if srv:
            try:
                srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            srv.close()
        for c in clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def _accept(self, srv):
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
                self._clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send(self, c, data):
        if self.split_frames:
            for i in range(len(data)):
                c.sendall(data[i:i + 1])
        else:
            c.sendall(data)

    def _serve(self, c):
        buf = bytearray()
        try:
            while True:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
                for cmd, a0, a1, p in sdb_parse(buf):
                    if cmd == "CNXN":
                        if self.handshake == "silent":
                            continue
                        if self.handshake == "close":
                            c.close()
                            return
                        self._send(c, sdb_msg("CNXN", 0x01000000, 4096, b"device::FAKE::\0"))
                    elif cmd == "OPEN":
                        svc = p.rstrip(b"\0").decode()
                        with self.lock:
                            self.commands.append(svc)
                        self._send(c, sdb_msg("OKAY", 9, a0))
                        for chunk in self.reply:
                            self._send(c, sdb_msg("WRTE", 9, a0, chunk))
                        self._send(c, sdb_msg("CLSE", 9, a0))
                        if self.reply and b"launched" in self.reply[-1] and self.app_starts_after is not None:
                            threading.Timer(self.app_starts_after, self._app_started).start()
        except OSError:
            return
        finally:
            try:
                c.close()
            except OSError:
                pass

    def _app_started(self):
        if self._sdb_srv:            # only if TV still on
            self.app_running = True
            self._start_status()

    @property
    def launches(self):
        return sum(1 for x in self.commands if "was_execute" in x)

    # ---------------- OpenTV status service
    def _start_status(self):
        with self.lock:
            if self._status_srv:
                return
            tv = self

            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):
                    if tv.status_mode == "tcp_only":
                        self.connection.close()
                        return
                    if self.path != "/status":
                        self.send_response(404); self.end_headers(); return
                    if tv.status_mode == "500":
                        self.send_response(500); self.end_headers(); self.wfile.write(b"{}"); return
                    body = {"garbage": b"not json", "not_ok": b'{"ok": false, "version": 17}'}.get(
                        tv.status_mode) or json.dumps({"ok": True, "version": 17,
                                                      "client": {"build": "OpenTV v20 native", "screen": "home"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

            ThreadingHTTPServer.allow_reuse_address = True
            srv = ThreadingHTTPServer(("127.0.0.1", self.status_port), H)
            srv.daemon_threads = True
            self._status_srv = srv
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def _stop_status(self):
        with self.lock:
            srv, self._status_srv = self._status_srv, None
        if srv:
            srv.shutdown()
            srv.server_close()
