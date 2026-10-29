"""Catalogue remplacé (rafraîchissement quotidien, rollback, copie manuelle) ⇒ visible sur la TV en ≤ 120 s.

Borne : le service recalcule « Pour toi » dès que la signature du catalogue change (sans attendre FORYOU_TTL_S) et
tv.js redemande /api/tv/foryou toutes les FY_REFRESH_MS ≤ 60 s (+ un poll de 2 s) ⇒ ≤ ~62 s après le remplacement.
Même carte : réponse identique ⇒ aucun nouveau rendu ; réponse différente pendant une fiche ou une lecture ⇒ appliquée
seulement à la fermeture/au retour, focus réancré par identifiant stable du film (repli : même rangée si retiré).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tests._common import PI4, catalogue_items
from tests.test_service import make_app, Servers
from tests.test_compliance import find_chromium
from tests import test_playback as tp   # module importé (jamais la classe : elle serait rejouée ici)
from opentv_pi import app as appmod, config

NEW_TITLE = "Comète Inédite"
POLL_MS = 2000
BOUND_MS = 120000


def new_items():
    out = []
    for i, x in enumerate(catalogue_items()):
        y = dict(x, site_id=str(200 + i), detail_path="b/fakrov/%d" % (200 + i),
                 title="%s %d" % (NEW_TITLE, i), poster="https://img.example/%d.jpg" % (200 + i))
        out.append(y)
    return out


def replace_atomically(path, items, size_pad=""):
    """Comme le rafraîchissement quotidien : candidat dans le même dossier puis os.replace (nouvel inode)."""
    tmp = os.path.join(os.path.dirname(path), ".catalogue.json.test.candidate")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated_at": "2026-10-28T04:30:00Z", "count": len(items), "items": items, "pad": size_pad}, f)
    os.replace(tmp, path)


def titles(data):
    return {it.get("title") for r in data.get("rows") or [] for it in r.get("items") or []}


class TestServerInvalidation(unittest.TestCase):
    def setUp(self):
        self.a = make_app(self)
        self.assertIsNotNone(self.a.foryou)
        self.calls = [0]
        orig = self.a.foryou.tv_session

        def counting():
            self.calls[0] += 1
            return orig()
        self.a.foryou.tv_session = counting
        self.cat = self.a.cfg.catalogue_file

    def test_atomic_replacement_rebuilds_without_waiting_ttl(self):
        first = self.a.tv_foryou()
        self.assertTrue(titles(first))
        self.assertFalse(any(t.startswith(NEW_TITLE) for t in titles(first)))
        self.assertIs(self.a.tv_foryou(), first)                  # catalogue inchangé : session réutilisée
        self.assertEqual(self.calls[0], 1)
        replace_atomically(self.cat, new_items())
        self.a.mono_clock.t += 1.0                                # horloge fixe : bien en deçà de FORYOU_TTL_S
        second = self.a.tv_foryou()
        self.assertEqual(self.calls[0], 2)
        self.assertTrue(titles(second))
        self.assertTrue(all(t.startswith(NEW_TITLE) for t in titles(second)), titles(second))
        self.assertIs(self.a.tv_foryou(), second)                 # puis stable jusqu'au prochain changement
        self.assertEqual(self.calls[0], 2)
        uid = second["rows"][0]["items"][0]["id"]
        state, _ = self.a.tv_feedback(uid, "like")                # la session reconstruite accepte les actions
        self.assertIsNotNone(state)

    def test_same_bytes_new_inode_and_mtime_change_are_detected(self):
        self.a.tv_foryou()
        with open(self.cat, "rb") as f:
            raw = f.read()
        tmp = self.cat + ".same"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, self.cat)                                 # mêmes octets, autre inode
        self.a.tv_foryou()
        self.assertEqual(self.calls[0], 2)
        st = os.stat(self.cat)
        os.utime(self.cat, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))   # même inode, mtime changée
        self.a.tv_foryou()
        self.assertEqual(self.calls[0], 3)

    def test_unreadable_signature_fails_safe_to_cached_session_and_ttl(self):
        first = self.a.tv_foryou()
        os.remove(self.cat)                                       # stat impossible : pas d'invalidation, pas d'erreur
        self.assertIs(self.a.tv_foryou(), first)
        self.assertEqual(self.calls[0], 1)
        orig = appmod.os.stat

        def boom(p, *a, **k):
            if p == self.cat:
                raise PermissionError(13, "denied")
            return orig(p, *a, **k)
        appmod.os.stat = boom
        self.addCleanup(setattr, appmod.os, "stat", orig)
        self.assertIs(self.a.tv_foryou(), first)
        self.a.mono_clock.t += appmod.FORYOU_TTL_S                # le TTL reste le filet de sécurité
        self.a.tv_foryou()
        self.assertEqual(self.calls[0], 2)

    def test_replacement_during_build_is_picked_up_next_request(self):
        orig = self.a.foryou.tv_session
        swapped = [False]

        def racing():
            out = orig()                                          # lecture de l'ancien catalogue…
            if not swapped[0]:
                swapped[0] = True
                replace_atomically(self.cat, new_items())         # … remplacé juste après la lecture
            return out
        self.a.foryou.tv_session = racing
        first = self.a.tv_foryou()
        self.assertFalse(any(t.startswith(NEW_TITLE) for t in titles(first)))
        second = self.a.tv_foryou()                               # signature relevée avant lecture ⇒ reconstruit
        self.assertTrue(all(t.startswith(NEW_TITLE) for t in titles(second)), titles(second))

    def test_ttl_still_applies_without_catalogue_change(self):
        self.a.tv_foryou()
        self.a.mono_clock.t += appmod.FORYOU_TTL_S - 1
        self.a.tv_foryou()
        self.assertEqual(self.calls[0], 1)
        self.a.mono_clock.t += 1
        self.a.tv_foryou()
        self.assertEqual(self.calls[0], 2)


class TestTvRefreshIntervalSource(unittest.TestCase):
    def test_tv_js_requests_foryou_at_most_every_60s(self):
        with open(os.path.join(PI4, "ui", "tv.js"), encoding="utf-8") as f:
            js = f.read()
        m = re.search(r"var FY_REFRESH_MS = (\d+);", js)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 60000)
        self.assertLessEqual(int(m.group(1)) + POLL_MS, 62000)          # pire cas ≈ 62 s, marge sous 120 s
        body = re.search(r"function loadForYou\(force\) \{(.*?)\n  \}", js, re.S).group(1)
        self.assertIn("Date.now() - fyAt < FY_REFRESH_MS", body)
        self.assertNotRegex(body, r"\b\d{5,}\b")                       # aucun délai codé en dur ailleurs
        m = re.search(r"setTimeout\(poll, (\d+)\)", js)
        self.assertEqual(int(m.group(1)), POLL_MS)


@unittest.skipUnless(find_chromium(), "Chromium introuvable")
class TestTvShowsReplacedCatalogue(unittest.TestCase):
    """Vraie page TV (Chromium headless, temps virtuel) : le catalogue remplacé après le premier affichage apparaît
    avant 120 s, sans rechargement ni redémarrage."""

    def render(self, s, budget):
        prof = tempfile.mkdtemp(prefix="otvchr", dir="/tmp")
        self.addCleanup(shutil.rmtree, prof, True)
        p = subprocess.run([find_chromium(), "--headless", "--no-first-run", "--user-data-dir=" + prof,
                            "--virtual-time-budget=%d" % budget, "--window-size=1920,1080", "--dump-dom",
                            "http://127.0.0.1:%d/tv" % s.web.tv_port()], capture_output=True, timeout=120)
        return p.stdout.decode("utf-8", "replace")

    def test_swap_visible_well_under_120s(self):
        a = make_app(self)
        config.save_device(a.cfg, {"name": "Salon"})
        a.reload_device()
        s = Servers(self, a)
        orig, n = a.tv_foryou, [0]

        def tv_foryou():
            n[0] += 1
            out = orig()
            if n[0] == 1:                                         # premier affichage servi, puis passage quotidien
                replace_atomically(a.cfg.catalogue_file, new_items())
            return out
        a.tv_foryou = tv_foryou
        dom = self.render(s, 70000)                               # 60 s + poll + marge, bien sous 120 s
        self.assertGreaterEqual(n[0], 2, "TV n'a pas redemandé /api/tv/foryou en 70 s")
        self.assertIn(NEW_TITLE, dom)
        self.assertNotIn("Le Grand Voyage", dom)
        self.assertIn('data-reloads="0"', dom)                    # aucun rechargement de page


# --------------------------------------------------------------------------- même carte : vraie page, vraie lecture
def item(sid, title, year, genre, rating):
    return {"detail_path": "b/fakrov/%d" % sid, "site_id": str(sid), "title": title, "year": year, "genre": genre,
            "description": "Résumé de %s." % title, "poster": "https://img.example/%d.jpg" % sid,
            "imdb_rating": rating, "imdb_votes": 50000}


EXTRA = [item(201, "Aube Claire", 2026, "Thriller", 9.1), item(202, "Brume", 2026, "Thriller", 8.9),
         item(203, "Cendres", 2026, "Action", 8.8)]
FILM, FALLBACK, TOP = "Nuit Noire", "Mission // Impossible", "Les mieux notés que tu n'as pas vus"


def catalogue_doc(items):
    return json.dumps({"generated_at": "2026-10-28T04:30:00Z", "count": len(items), "items": items})


def rows_of(a):
    a.mono_clock.t += 10000
    return [(r["id"], [x["title"] for x in r["items"]]) for r in a.tv_foryou()["rows"]]


class TestTvSameCardAcrossHotReload(unittest.TestCase):
    """Vraie page /tv (Chromium + Playwright) : catalogue remplacé pendant l'accueil, une fiche, une lecture et un
    chargement. Retour = même film s'il existe encore (même s'il a changé de rangée/colonne), repli sensé sinon."""

    @unittest.skipUnless(find_chromium() and shutil.which("node") and os.environ.get("OPENTV_TEST_PLAYWRIGHT"),
                         "bout en bout : OPENTV_TEST_PLAYWRIGHT=<module playwright> + Chromium + node")
    def test_same_card_across_reload_panel_playback_and_removal(self):
        a = tp.TestAppIntegration.app(self)
        c = self.client
        base = catalogue_items()
        cats = {"A": catalogue_doc(base), "B": catalogue_doc(EXTRA + base),
                "C": catalogue_doc(EXTRA + [x for x in base if x["title"] != FILM])}
        # préconditions de l'algorithme (échec explicite s'il change) : en B le film est ailleurs, et sa position
        # d'origine porte un AUTRE film — un réancrage par rangée/colonne atterrirait donc sur le mauvais titre
        rows_a = dict(rows_of(a))
        self.assertEqual(rows_a["top"].index(FILM), 1)
        replace_atomically_doc(a.cfg.catalogue_file, cats["B"])
        rows_b = dict(rows_of(a))
        self.assertEqual(rows_b["top"].index(FILM), 3)
        self.assertNotEqual(rows_b["top"][1], FILM)
        replace_atomically_doc(a.cfg.catalogue_file, cats["C"])
        rows_c = dict(rows_of(a))
        self.assertNotIn(FILM, sum(rows_c.values(), []))
        self.assertEqual(rows_c["top"][-1], FALLBACK)
        replace_atomically_doc(a.cfg.catalogue_file, cats["A"])
        rows_of(a)
        s = Servers(self, a)
        ctl = tempfile.mkdtemp(prefix="otvctl", dir="/tmp")
        self.addCleanup(shutil.rmtree, ctl, True)
        stop = threading.Event()

        def driver():   # commandes du script de page -> contrôleur simulé (Retour CEC/HID, révélation retardée)
            while not stop.is_set():
                for name in sorted(os.listdir(ctl)):
                    os.unlink(os.path.join(ctl, name))
                    if name == "slow":
                        c.reveal_after = 10 ** 6
                    elif name == "reveal":
                        c.polls, c.reveal_after = 0, 1
                    elif name == "press":
                        tp.wait_state(a.playback, ("playing",), timeout=10)
                        c.press("back")
                time.sleep(0.02)
        th = threading.Thread(target=driver, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(stop.set)
        shots = os.environ.get("OPENTV_TEST_SHOTS") or tempfile.mkdtemp(prefix="otvhot", dir="/tmp")
        os.makedirs(shots, exist_ok=True)
        cfg = {"chromium": find_chromium(), "tv": "http://127.0.0.1:%d/tv" % s.web.tv_port(),
               "catfile": a.cfg.catalogue_file, "ctl": ctl, "catalogues": cats, "shots": shots}
        p = subprocess.run([shutil.which("node"), os.path.join(PI4, "tests", "e2e_hot_reload.js"),
                            os.environ["OPENTV_TEST_PLAYWRIGHT"], json.dumps(cfg)], capture_output=True, timeout=240)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-3000:])
        res = json.loads(p.stdout.decode().strip().splitlines()[-1])
        st = {x["s"]: x for x in res["steps"]}
        self.assertTrue(all(x["url"] == "/tv" and x["iframes"] == 0 for x in res["steps"]))
        start = st["start"]
        self.assertEqual((start["focus"], start["col"], start["rowTitle"]), (FILM, 1, TOP))
        # 0) même réponse : redemandée mais aucun nouveau rendu (nœuds, focus et héros intacts)
        u = st["unchanged"]
        self.assertTrue(res["f0"])
        self.assertTrue(u["probe"], "rangées redessinées alors que la réponse est identique")
        self.assertEqual((u["focus"], u["col"], u["htitle"]), (FILM, 1, FILM))
        # 1) accueil inactif : nouveau catalogue visible (≤ 61 s), même film au focus à sa NOUVELLE place
        i = st["idle_swapped"]
        self.assertTrue(res["f1"])
        self.assertIn("Aube Claire", i["titles"])
        self.assertEqual((i["focus"], i["col"], i["rowTitle"], i["htitle"]), (FILM, 3, TOP, FILM))
        self.assertFalse(i["panel"])
        # 2) fiche ouverte : réponse reçue mais différée ; fermeture -> appliquée, même film
        d = st["panel_deferred"]
        self.assertTrue(res["f2"])
        self.assertTrue(d["probe"])
        self.assertIn("Aube Claire", d["titles"])                          # toujours l'ancien affichage (B)
        self.assertEqual((d["panel"], d["ptitle"], d["focus"]), (True, FILM, FILM))
        k = st["panel_closed"]
        self.assertFalse(k["probe"])
        self.assertNotIn("Aube Claire", k["titles"])                       # A appliqué
        self.assertEqual((k["panel"], k["focus"], k["col"], k["rowTitle"]), (False, FILM, 1, TOP))
        # 3) lecture : différé pendant la lecture ET sur la fiche rouverte ; Retour = même film ; puis appliqué
        self.assertEqual((res["playing3"], res["idle3"]), ("playing", "idle"))
        pd = st["play_deferred"]
        self.assertTrue(res["f3"])
        self.assertTrue(pd["probe"])
        self.assertNotIn("Aube Claire", pd["titles"])
        r3 = st["play_returned"]
        self.assertEqual((r3["overlay"], r3["panel"], r3["ptitle"], r3["btn0"], r3["focus"]),
                         (False, True, FILM, "Regarder", FILM))
        self.assertTrue(r3["probe"])
        c3 = st["play_closed"]
        self.assertFalse(c3["probe"])
        self.assertIn("Aube Claire", c3["titles"])
        self.assertEqual((c3["panel"], c3["focus"], c3["col"], c3["rowTitle"]), (False, FILM, 3, TOP))
        # 4) chargement : film retiré du nouveau catalogue ; différé, retour sur sa fiche, puis repli même rangée
        self.assertEqual((res["loading4"], res["playing4"], res["idle4"]), ("loading", "playing", "idle"))
        ld = st["loading_deferred"]
        self.assertTrue(res["f4"])
        self.assertTrue(ld["probe"])
        self.assertEqual((ld["overlay"], ld["ovMsg"]), (True, "Chargement…"))
        self.assertIn(FILM, ld["titles"])
        r4 = st["removed_returned"]
        self.assertEqual((r4["overlay"], r4["panel"], r4["ptitle"], r4["focus"]), (False, True, FILM, FILM))
        c4 = st["removed_closed"]
        self.assertFalse(c4["probe"])
        self.assertNotIn(FILM, c4["titles"])
        self.assertEqual((c4["panel"], c4["focus"], c4["rowTitle"], c4["htitle"]), (False, FALLBACK, TOP, FALLBACK))
        # 5) choix utilisateur pendant une sélection en attente : jamais perdu, même film au focus
        ld5 = st["like_deferred"]
        self.assertTrue(res["f5"])
        self.assertTrue(ld5["probe"])
        self.assertEqual((ld5["panel"], ld5["ptitle"]), (True, FALLBACK))
        li = st["liked_immediate"]                                         # avant la relecture du service
        self.assertEqual(li["fetches"], ld5["fetches"])
        self.assertFalse(li["probe"])
        self.assertEqual((li["panel"], li["focus"]), (False, FALLBACK))
        self.assertIn("J'aime", li["marks"])                               # jamais un badge perdu
        lk = st["liked_applied"]
        self.assertTrue(res["f5b"])
        self.assertIn("Aube Claire", lk["titles"])                         # B appliqué à la fermeture
        self.assertEqual((lk["panel"], lk["focus"], lk["rowTitle"], lk["htitle"]), (False, FALLBACK, TOP, FALLBACK))
        self.assertIn("J'aime", lk["marks"])
        uid = [x["id"] for r in a.tv_foryou()["rows"] for x in r["items"] if x["title"] == FALLBACK][0]
        self.assertEqual(a.tv_foryou()["marks"][uid].get("opinion"), "like")   # enregistré côté service
        state_text = json.dumps(a.tv_state())
        self.assertNotIn("films-mirror", state_text)


def replace_atomically_doc(path, text):
    tmp = os.path.join(os.path.dirname(path), ".catalogue.json.test.candidate")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


if __name__ == "__main__":
    unittest.main()
