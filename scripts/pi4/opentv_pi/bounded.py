"""Serveurs à concurrence bornée (HTTP TV/configuration, Agent Port) : au plus `max_workers` connexions servies en
parallèle (sémaphore), les suivantes reçoivent immédiatement une réponse « occupé » et sont fermées ; chaque connexion
a une échéance globale (`deadline_s`) appliquée à chaque lecture : un client qui envoie un octet de temps en temps
(slowloris) est coupé à l'échéance, et le nombre de threads ne dépasse jamais max_workers + le thread d'écoute."""
import io
import socket
import threading
import time

MAX_WORKERS = 16
DEADLINE_S = 15.0
BUSY_SEND_TIMEOUT_S = 0.2


class DeadlineReader(io.RawIOBase):
    """Lecture brute du socket bornée par une échéance absolue (et non par un délai par appel)."""

    def __init__(self, sock, deadline, mono=time.monotonic):
        super().__init__()
        self.sock, self.deadline, self.mono = sock, deadline, mono

    def readable(self):
        return True

    def remaining(self):
        return self.deadline - self.mono()

    def readinto(self, b):
        rem = self.remaining()
        if rem <= 0:
            raise socket.timeout("deadline")
        self.sock.settimeout(rem)
        return self.sock.recv_into(b)


class BoundedServerMixIn:
    """À placer avant la classe socketserver de base (remplace ThreadingMixIn)."""
    daemon_threads = True
    block_on_close = False
    max_workers = MAX_WORKERS
    request_queue_size = 64   # file d'attente du noyau : les connexions en trop reçoivent « occupé », pas un refus
    deadline_s = DEADLINE_S
    busy_response = b""

    def _slots_sem(self):
        sem = getattr(self, "_bounded_slots", None)
        if sem is None:
            sem = self._bounded_slots = threading.BoundedSemaphore(self.max_workers)
            self.busy_count = 0
        return sem

    def process_request(self, request, client_address):
        sem = self._slots_sem()
        if not sem.acquire(blocking=False):
            self.busy_count += 1
            try:
                request.settimeout(BUSY_SEND_TIMEOUT_S)
                request.sendall(self.busy_response)
            except OSError:
                pass
            self.shutdown_request(request)
            self.on_busy()
            return
        try:
            threading.Thread(target=self._bounded_worker, args=(request, client_address), daemon=True,
                             name="bounded-worker").start()
        except RuntimeError:
            sem.release()
            self.shutdown_request(request)

    def _bounded_worker(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:   # noqa: BLE001 — jamais de trace vers stderr ni vers le client
            pass
        finally:
            self.shutdown_request(request)
            self._slots_sem().release()

    def handle_error(self, request, client_address):
        pass

    def on_busy(self):
        pass


def install_deadline_reader(handler):
    """À appeler dans setup() d'un StreamRequestHandler : remplace rfile par une lecture à échéance globale."""
    deadline = time.monotonic() + float(getattr(handler.server, "deadline_s", DEADLINE_S))
    orig = handler.rfile
    handler.rfile = io.BufferedReader(DeadlineReader(handler.connection, deadline), 8192)
    handler.deadline = deadline
    orig.close()
