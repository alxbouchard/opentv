"""Travailleurs du test de création simultanée du verrou (multiprocessing « spawn » : module léger, importable seul).

Chaque travailleur reçoit (dossier neuf, numéro) par essai, attend la barrière commune puis fait, en même temps que les
autres, la toute première écriture du registre de ce dossier (création du verrou latéral comprise)."""
import os

from opentv_pi import agents


def worker(barrier, tasks, results):
    while True:
        job = tasks.get()
        if job is None:
            return
        d, wid = job
        try:
            barrier.wait(timeout=60)
            r = agents.Registry(os.path.join(d, "agents.json"), lock_timeout=60)
            r.issue("w%d" % wid, ["read"], force=True)
            results.put((wid, "ok"))
        except BaseException as e:   # rapporté au parent (jamais avalé)
            results.put((wid, "%s:%s" % (type(e).__name__, e)))
