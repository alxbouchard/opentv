"""Chrome DevTools Protocol sur tube (--remote-debugging-pipe, fd 3 = commandes, fd 4 = réponses).

Porté du pont NanoPi (scripts/nanopi/opentv_bridge.py, classe CDP) sans dépendance au reste du pont. Jamais de port
TCP de débogage : le navigateur ne parle qu'au processus qui détient les deux tubes.
"""
import fcntl
import json
import os
import threading

MAX_FRAME = 16 * 1024 * 1024


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, wfd, rfd, on_event=None, on_close=None):
        self.w, self.r = os.fdopen(wfd, "wb", buffering=0), os.fdopen(rfd, "rb", buffering=0)
        self.n, self.lock, self.pending = 0, threading.Lock(), {}
        self.on_event, self.on_close = on_event, on_close
        self.closed = False
        self._rthread = threading.Thread(target=self._reader, daemon=True, name="cdp-reader")
        self._rthread.start()

    def _reader(self):
        buf = b""
        try:
            while True:
                chunk = self.r.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if "id" in msg and msg["id"] in self.pending:
                        slot = self.pending[msg["id"]]
                        slot[1] = msg
                        slot[0].set()
                    elif "method" in msg and self.on_event:
                        try:
                            self.on_event(msg)
                        except Exception:   # un gestionnaire défaillant ne doit jamais couper le lecteur CDP
                            pass
                if len(buf) > MAX_FRAME:
                    break
        except (OSError, ValueError):
            pass
        self.closed = True
        for slot in list(self.pending.values()):
            slot[0].set()
        try:   # extrémité de lecture : fermée par ce thread seul (jamais pendant qu'il y lit)
            self.r.close()
        except OSError:
            pass
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    def post(self, method, params=None, session=None):
        """Envoie sans attendre la réponse -> jeton pour `wait`. Nécessaire pour une cible en pause
        (waitForDebuggerOnStart) : ses commandes ne répondent qu'après Runtime.runIfWaitingForDebugger."""
        if self.closed:
            raise CDPError("closed")
        with self.lock:
            self.n += 1
            mid = self.n
            slot = [threading.Event(), None]
            self.pending[mid] = slot
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                self.w.write(json.dumps(msg).encode() + b"\0")
            except (OSError, ValueError):
                self.pending.pop(mid, None)
                raise CDPError("write")
        return (mid, slot, method)

    def wait(self, token, timeout=5.0):
        mid, slot, method = token
        ok = slot[0].wait(timeout)
        self.pending.pop(mid, None)
        if not ok or slot[1] is None:
            raise CDPError(("timeout:" + method) if not ok else "closed")
        if "error" in slot[1]:
            err = slot[1]["error"]
            raise CDPError(method + ":" + str(err.get("message", "") if isinstance(err, dict) else "")[:120])
        res = slot[1].get("result", {})
        return res if isinstance(res, dict) else {}

    def send(self, method, params=None, session=None, timeout=5.0):
        return self.wait(self.post(method, params, session), timeout)

    def close(self):
        """Idempotent ; réveille immédiatement toute commande en attente (CDPError « closed ») : aucune fermeture de
        séance n'attend jamais la fin d'un délai CDP."""
        self.closed = True
        for slot in list(self.pending.values()):
            slot[0].set()
        got = self.lock.acquire(timeout=1.0)   # jamais pendant une écriture en cours (navigateur déjà tué : EPIPE)
        try:
            self.w.close()   # le navigateur voit la fin du tube ; le lecteur reçoit EOF et ferme son extrémité
        except OSError:
            pass
        finally:
            if got:
                self.lock.release()
        if self._rthread is not threading.current_thread():
            self._rthread.join(0.5)


def _high(fd, floor=10):
    """Déplace un descripteur au-dessus de `floor` (jamais 3/4, où le fils attend ses tubes) ; sûr entre threads."""
    if fd >= floor:
        return fd
    hi = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, floor)
    os.close(fd)
    return hi


def spawn_with_pipe(argv, env, popen):
    """Lance argv avec le tube CDP sur fd 3/4 du fils. -> (processus, fd écriture parent, fd lecture parent).

    Les quatre extrémités sont d'abord déplacées au-dessus de 10 : le petit shell `exec … 3<&N 4>&M` ne peut jamais
    refermer un tube tombé sur 3/4 (défaut corrigé sur le NanoPi), sans réserver de numéro — aucune course avec
    d'autres threads qui ouvrent des descripteurs en même temps."""
    c_r, p_w = (_high(x) for x in os.pipe())
    p_r, c_w = (_high(x) for x in os.pipe())
    # bash (pas dash : /bin/sh de Debian refuse les numéros de descripteur à deux chiffres, « Bad fd number »)
    sh = ["/bin/bash", "-c", 'exec "$0" "$@" 3<&%d 4>&%d %d<&- %d>&-' % (c_r, c_w, c_r, c_w)]
    try:
        proc = popen(sh + list(argv), env, (c_r, c_w))
    except BaseException:
        for fd in (p_w, p_r):
            os.close(fd)
        raise
    finally:
        os.close(c_r)
        os.close(c_w)
    return proc, p_w, p_r
