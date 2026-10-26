"""Seul le compte du kiosque peut utiliser la page TV et son API (UID du pair TCP loopback via /proc/net/tcp)."""
import json
import os
import shutil
import tempfile
import unittest

from tests.test_service import make_app, Servers
from opentv_pi import peeruid, config

# ligne du client (navigateur) : 127.0.0.1:54321 -> 127.0.0.1:8080, uid 1001 ; ligne du serveur : uid 999
TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 11111 1 0 100 0 0 10 0
   1: 0100007F:D431 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1001        0 22222 1 0 20 4 30 10 -1
   2: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000   999        0 33333 1 0 20 4 30 10 -1
"""


class TestPeerUid(unittest.TestCase):
    def proc(self, text=TCP):
        d = tempfile.mkdtemp(prefix="otvproc", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "net"))
        with open(os.path.join(d, "net", "tcp"), "w") as f:
            f.write(text)
        return d

    def test_parse(self):
        p = self.proc()
        self.assertEqual(peeruid.tcp_peer_uid("127.0.0.1", 54321, 8080, p), 1001)
        self.assertIsNone(peeruid.tcp_peer_uid("127.0.0.1", 54322, 8080, p))
        self.assertIsNone(peeruid.tcp_peer_uid("127.0.0.1", 54321, 8080, "/nonexistent"))
        self.assertIsNone(peeruid.tcp_peer_uid("::1", 54321, 8080, p))

    def test_only_kiosk_uid_reaches_tv_page_and_api(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = Servers(self, a)
        a.tv_client_uids = frozenset({1001})
        a.peer_uid_fn = lambda ip, port, sport: 999          # autre compte local (ex. un agent)
        self.assertEqual(s.req("GET", "/tv")[0], 403)
        self.assertEqual(s.req("GET", "/api/tv/state", headers=s.tv_headers())[0], 403)
        hdr = dict(s.tv_headers(), **{"Content-Type": "application/json"})
        self.assertEqual(s.req("POST", "/api/tv/setup", "{}", hdr)[0], 403)
        self.assertEqual(s.req("GET", "/healthz")[0], 200)  # santé : lisible par tous en loopback
        a.peer_uid_fn = lambda ip, port, sport: None          # inconnu : refusé (fail-closed)
        self.assertEqual(s.req("GET", "/tv")[0], 403)
        a.peer_uid_fn = lambda ip, port, sport: 1001          # kiosque
        self.assertEqual(s.req("GET", "/tv")[0], 200)
        code, _, body = s.req("GET", "/api/tv/state", headers=s.tv_headers())
        self.assertEqual((code, json.loads(body)["device"]), (200, "Salon"))

    def test_client_uids_policy(self):
        self.assertIsNone(peeruid.allowed_uids("opentv-kiosk", "/nonexistent-proc"))       # hors Linux : contrôle absent
        p = self.proc()
        self.assertEqual(peeruid.allowed_uids("utilisateur-inexistant-xyz", p), frozenset())  # Linux : fail-closed


if __name__ == "__main__":
    unittest.main()
