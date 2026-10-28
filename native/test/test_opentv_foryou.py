"""Tests « Pour toi » : stockage SQLite, moteur de recommandation, navigation, CLI agent, intégration au pont.

Depuis la racine : python3 -m unittest -v native.test.test_opentv_foryou   (ou python3 -m pytest native/test)
Aucun réseau externe, aucun NanoPi, aucune TV. Le test Chromium réel est sauté si aucun Chromium n'est trouvé.
"""
import importlib.util
import io
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FY_SRC = os.path.join(ROOT, "scripts", "nanopi", "opentv_foryou.py")
UI_FILM = os.path.join(ROOT, "scripts", "nanopi", "opentv_bridge_ui.html")
UI_FY = os.path.join(ROOT, "scripts", "nanopi", "opentv_foryou_ui.html")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TB = _load("opentv_bridge_testlib", os.path.join(HERE, "test_opentv_bridge.py"))   # Harness, StubResolver…
B = TB.B
FY = B.FY   # le module chargé par le pont lui-même (mêmes classes d'exception)

NOW = 1793000000.0          # 2026-10-26 (UTC) : horloge fixe → résultats reproductibles
DAY = 86400.0
SRC_KEY = "films"          # espace de noms logique FORYOU_SOURCE_ID (défaut), indépendant du domaine


def quiet():
    return redirect_stdout(io.StringIO())


def it(sid, title, genre=None, year=2020, imdb=None, votes=None, tmdb=None, tvotes=None, **kw):
    x = {"site_id": None if sid is None else str(sid), "title": title, "year": year, "genre": genre,
         "imdb_rating": imdb, "imdb_votes": votes, "detail_path": "b/fakrov/%s" % (9000 + (sid or 0)),
         "source_id": str(9000 + (sid or 0)), "poster": "https://image.tmdb.org/t/p/w300/%s.jpg" % sid,
         "description": "Résumé de %s. Voir https://evil.example/stream.m3u8" % title}
    if tmdb is not None:
        x.update(tmdb_rating=tmdb, tmdb_votes=tvotes, tmdb_match_status="matched", tmdb_id=sid)
    x.update(kw)
    return x


def uid(sid):
    return "%s/%s" % (SRC_KEY, sid)


CAT = [
    it(1, "Le Parrain", "Drame", 1972, 9.2, 2000000),
    it(2, "Ratatouille", "Animation", 2007, 8.1, 800000),
    it(3, "Superbad", "Comédie", 2007, 7.6, 600000),
    it(4, "Petit Drame", "Drame", 2026, 6.1, 3000),
    it(5, "Inconnu Total", "Thriller", 2026),                         # aucune note : jamais « mieux noté »
    it(6, "Chef-d'œuvre confidentiel", "Drame", 2025, 9.6, 25),        # note haute mais 25 votes
    it(7, "Seulement TMDb", "Science-Fiction", 2024, tmdb=7.9, tvotes=4200),
    it(8, "Élève modèle", "Comédie", 2026, 6.8, 15000),
    it(9, "Horreur Max", "Horreur", 2026, 5.2, 40000),
    it(10, "Doc Planète", "Doc, Documentaire", 2026, 8.0, 12000),
    it(None, "Sans identifiant stable", "Drame", 2026, 9.9, 900000),
]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.chmod(self.d, 0o700)
        self.db = os.path.join(self.d, "state", "foryou.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def store(self, now=NOW):
        self.now = [now]
        st = FY.Store(self.db, clock=lambda: self.now[0])
        self.addCleanup(st.close)
        return st


# ----------------------------------------------------------------------------- stockage
class StoreTests(Tmp):
    def test_schema_version_tables_and_safe_permissions(self):
        s = self.store()
        s.close()
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.db)).st_mode), 0o700)
        c = sqlite3.connect(self.db)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], FY.SCHEMA_VERSION)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        self.assertTrue({"prefs", "events", "signals", "catalogue_seen", "snapshots"} <= tables, tables)

    def test_reopen_keeps_data_and_migration_is_idempotent(self):
        s = self.store()
        s.apply(uid(1), "like", origin="cli", title="Le Parrain", genre="Drame")
        s.close()
        s2 = self.store()
        self.assertEqual(s2.state(uid(1))["opinion"], "like")
        self.assertEqual(len(s2.history("events")), 1)
        s2.close()

    def test_newer_schema_and_corrupt_file_refused(self):
        os.makedirs(os.path.dirname(self.db), mode=0o700)
        c = sqlite3.connect(self.db)
        c.execute("PRAGMA user_version=99")
        c.close()
        with self.assertRaises(FY.StoreError):
            self.store()
        os.unlink(self.db)
        with open(self.db, "wb") as f:
            f.write(b"ceci n'est pas une base sqlite" * 100)
        with self.assertRaises(FY.StoreError):
            self.store()

    def test_actions_are_idempotent_and_only_changes_are_logged(self):
        s = self.store()
        self.assertTrue(s.apply(uid(1), "like", origin="tv")["changed"])
        self.assertFalse(s.apply(uid(1), "like", origin="tv")["changed"])
        self.assertTrue(s.apply(uid(1), "watchlist", origin="cli")["changed"])
        self.assertFalse(s.apply(uid(1), "watchlist", origin="cli")["changed"])
        ev = s.history("events")
        self.assertEqual([e["action"] for e in ev], ["watchlist", "like"])   # plus récent d'abord
        self.assertEqual({e["origin"] for e in ev}, {"tv", "cli"})

    def test_like_and_dislike_are_exclusive_but_independent_of_seen_and_watchlist(self):
        s = self.store()
        s.apply(uid(2), "like")
        s.apply(uid(2), "seen")
        s.apply(uid(2), "watchlist")
        s.apply(uid(2), "dislike")
        st = s.state(uid(2))
        self.assertEqual(st, {"opinion": "dislike", "seen": True, "watchlist": True, "not_tonight": False})
        s.apply(uid(2), "clear-opinion")
        s.apply(uid(2), "unseen")
        s.apply(uid(2), "unwatchlist")
        self.assertEqual(s.state(uid(2)), {"opinion": None, "seen": False, "watchlist": False, "not_tonight": False})

    def test_opened_signal_never_marks_seen(self):
        s = self.store()
        s.record_signal(uid(3), "opened", app="pourtoi")
        s.record_signal(uid(3), "opened", app="catalogue")
        self.assertFalse(s.state(uid(3))["seen"])
        self.assertEqual(s.history("events"), [])
        self.assertEqual([x["kind"] for x in s.history("signals")], ["opened", "opened"])
        with self.assertRaises(ValueError):
            s.record_signal(uid(3), "seen")

    def test_not_tonight_expires_and_is_not_a_dislike(self):
        s = self.store()
        s.apply(uid(4), "not-tonight")
        self.assertEqual(s.state(uid(4)), {"opinion": None, "seen": False, "watchlist": False, "not_tonight": True})
        self.now[0] += FY.NOT_TONIGHT_S + 1
        self.assertFalse(s.state(uid(4))["not_tonight"])
        self.assertEqual(s.counts()["disliked"], 0)

    def test_invalid_ids_and_actions_rejected(self):
        s = self.store()
        for bad in ("17397", "films/abc", "films/1/2", "../x/1", "Films/1", "", None, "a" * 70 + "/1",
                    "fakrov.com/1", "https://fakrov.com/1", "films/1 ", "f/1"):
            with self.assertRaises(ValueError, msg=bad):
                s.apply(bad, "like")
        with self.assertRaises(ValueError):
            s.apply(uid(1), "drop table")
        self.assertEqual(s.history("events"), [])

    def test_logs_are_bounded(self):
        s = FY.Store(self.db, clock=lambda: NOW, max_log=10)
        self.addCleanup(s.close)
        for i in range(30):
            s.apply(uid(i + 1), "seen")
            s.record_signal(uid(i + 1), "opened", app="pourtoi")
        self.assertEqual(len(s.history("events", limit=100)), 10)
        self.assertEqual(len(s.history("signals", limit=100)), 10)
        self.assertEqual(s.history("events", limit=1)[0]["id"], uid(30))

    def test_catalogue_snapshots_baseline_then_diff(self):
        s = self.store()
        r = s.observe_catalogue([uid(1), uid(2)])
        self.assertEqual((r["changed"], r["baseline"], r["added"]), (True, True, 0))
        self.assertEqual(s.observe_catalogue([uid(2), uid(1)])["changed"], False)   # même ensemble : no-op
        self.now[0] += DAY
        r = s.observe_catalogue([uid(1), uid(2), uid(3), uid(4)])
        self.assertEqual((r["changed"], r["baseline"], r["added"]), (True, False, 2))
        nov = s.novelty()
        self.assertFalse(nov[uid(1)]["new"])
        self.assertTrue(nov[uid(3)]["new"])
        self.assertEqual(nov[uid(3)]["first_seen"], NOW + DAY)

    def test_empty_snapshot_never_becomes_baseline(self):
        s = self.store()
        r = s.observe_catalogue([])                                # catalogue vide (panne, premier démarrage)
        self.assertEqual((r["changed"], r["baseline"], r["added"]), (False, False, 0))
        self.assertEqual(s.snapshot_stats()["count"], 0)          # rien d'enregistré : pas une référence
        self.now[0] += DAY
        full = [uid(x["site_id"]) for x in CAT if x["site_id"]]
        r = s.observe_catalogue(full)                             # premier catalogue NON VIDE = la référence
        self.assertEqual((r["changed"], r["baseline"], r["added"]), (True, True, 0))
        nov = s.novelty()
        self.assertFalse([u for u, v in nov.items() if v["new"]])
        reasons = [y["text"] for x in rec(novelty=nov, now=self.now[0])["ranked"] for y in x["reasons"]]
        self.assertFalse([t for t in reasons if t.startswith("Ajouté au catalogue")], reasons)
        self.assertEqual(s.observe_catalogue([])["changed"], False)   # vide ensuite : ignoré aussi
        self.now[0] += DAY
        r = s.observe_catalogue(full + [uid(41), uid(42)])        # seuls les vrais ajouts sont nouveaux
        self.assertEqual((r["changed"], r["baseline"], r["added"]), (True, False, 2))
        self.assertEqual(sorted(u for u, v in s.novelty().items() if v["new"]), [uid(41), uid(42)])

    def test_legacy_empty_snapshot_is_not_used_as_baseline(self):
        s = self.store()
        s.db.execute("INSERT INTO snapshots (ts, fingerprint, count, added) VALUES (?,?,0,0)",
                     (NOW - DAY, FY.hashlib.sha256(b"").hexdigest()))   # base héritée : instantané vide
        r = s.observe_catalogue([uid(1), uid(2)])
        self.assertEqual((r["baseline"], r["added"]), (True, 0))
        self.assertFalse([u for u, v in s.novelty().items() if v["new"]])
        s.observe_catalogue([uid(1), uid(2), uid(3)])
        self.assertEqual([u for u, v in s.novelty().items() if v["new"]], [uid(3)])

    def test_busy_timeout_is_short_and_configured_once(self):
        self.assertTrue(100 <= FY.BUSY_TIMEOUT_MS <= 250, FY.BUSY_TIMEOUT_MS)
        s = self.store()
        # gestionnaire interne SQLite désactivé (son sommeil peut s'arrondir à ~1 s) : seule attente = _retry
        self.assertEqual(s.db.execute("PRAGMA busy_timeout").fetchone()[0], 0)
        with open(FY_SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(re.findall(r"busy_timeout\s*=|connect\([^)]*timeout=[1-9]", src), [])   # aucune autre valeur
        self.assertEqual(len(re.findall(r"sqlite3\.connect\(", src)), 1)
        self.assertEqual(re.findall(r"BUSY_TIMEOUT_MS\s*/", src), ["BUSY_TIMEOUT_MS /"])   # un seul usage

    def test_store_fails_fast_under_external_reserved_or_exclusive_lock(self):
        s = self.store()
        s.apply(uid(1), "like")
        for mode in ("IMMEDIATE", "EXCLUSIVE"):
            c = sqlite3.connect(self.db, isolation_level=None)
            try:
                c.execute("BEGIN " + mode)
                if mode == "EXCLUSIVE":
                    t0 = time.monotonic()
                    with self.assertRaises(FY.StoreError):
                        s.profile()
                    self.assertLess(time.monotonic() - t0, 1.0, mode)
                t0 = time.monotonic()
                with self.assertRaises(FY.StoreError):
                    s.apply(uid(2), "like")
                with self.assertRaises(FY.StoreError):
                    s.observe_catalogue([uid(1)])
                self.assertLess(time.monotonic() - t0, 1.0, mode)
            finally:
                c.execute("ROLLBACK")
                c.close()
        self.assertEqual(s.state(uid(1))["opinion"], "like")
        self.assertEqual(s.state(uid(2))["opinion"], None)


class BreakerTests(Tmp):
    """Disjoncteur du pont : après une panne de base, profil vide pendant le refroidissement, sans attente
    SQLite ni journal répété ; reprise automatique ensuite."""

    def bridge(self):
        self.mono, self.logs = [100.0], []
        b = FY.Bridge(self.db, SRC_KEY, lambda ev, **kw: self.logs.append((ev, kw)), clock=lambda: NOW,
                      mono=lambda: self.mono[0])
        self.addCleanup(b.reset)
        return b

    def test_lock_trips_breaker_once_then_recovers(self):
        st = self.store()
        st.apply(uid(3), "like", title="Superbad", genre="Comédie")
        st.close()
        b = self.bridge()
        c = sqlite3.connect(self.db, isolation_level=None)
        c.execute("BEGIN EXCLUSIVE")
        try:
            t0 = time.monotonic()
            fy = b.session(CAT)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertTrue(fy["data"]["cold_start"])                  # profil vide, vue utilisable
            t1 = time.monotonic()
            for _ in range(20):                                       # pendant le refroidissement : immédiat
                b.session(CAT)
                self.assertIsNone(b.call("profile"))
            self.assertLess(time.monotonic() - t1, 0.5)
            self.assertEqual([e for e, _ in self.logs].count("foryou_store_error"), 1)   # un seul journal
        finally:
            c.execute("ROLLBACK")
            c.close()
        self.assertIsNone(b.call("profile"))                          # toujours en refroidissement
        self.mono[0] += FY.STORE_COOLDOWN_S + 0.1
        self.assertFalse(b.session(CAT)["data"]["cold_start"])        # reprise : le « J'aime » revient
        self.assertEqual([e for e, _ in self.logs].count("foryou_store_recovered"), 1)
        self.assertEqual([e for e, _ in self.logs].count("foryou_store_error"), 1)
        self.assertTrue(1.0 <= FY.STORE_COOLDOWN_S <= 30.0)

    def test_async_worker_is_single_bounded_and_drainable(self):
        b = self.bridge()
        before = threading.active_count()
        for i in range(200):                                          # rafale de touches : jamais 200 threads
            b.opened_async(it(1 + i % 5, "x"), "catalogue")
            b.observe_async(CAT)
            self.assertLessEqual(threading.active_count(), before + 1)
            self.assertLessEqual(b.pending(), FY.ASYNC_MAX)              # file bornée (plus ancien abandonné)
        self.assertTrue(b.drain(5))
        self.assertEqual(b.pending(), 0)
        st = self.store()
        self.assertTrue(1 <= len(st.history("signals", limit=500)) <= 200)
        self.assertEqual(st.snapshot_stats()["count"], 1)                  # instantanés fusionnés, idempotents
        self.assertTrue(st.history("signals", limit=1)[0]["kind"] == "opened")
        end = time.monotonic() + 2
        while threading.active_count() > before and time.monotonic() < end:   # le worker s'arrête au repos
            time.sleep(0.01)
        self.assertLessEqual(threading.active_count(), before)


# ----------------------------------------------------------------------------- moteur
def rec(items=CAT, profile=None, novelty=None, now=NOW, absent=()):
    return FY.recommend(items, SRC_KEY, profile or {}, novelty or {}, now, absent=frozenset(absent))


def row(r, rid):
    for x in r["rows"]:
        if x["id"] == rid:
            return x
    return None


def ids(xs):
    return [x["id"] for x in xs]


class EngineTests(Tmp):
    def profile(self, *acts):
        s = self.store()
        cat = {uid(x["site_id"]): x for x in CAT if x["site_id"]}
        for i, (sid, a) in enumerate(acts):
            self.now[0] = NOW - 1000 + i
            s.apply(uid(sid), a, title=cat[uid(sid)]["title"], genre=cat[uid(sid)]["genre"])
        self.now[0] = NOW
        return s.profile()

    def test_cold_start_uses_quality_with_confidence_and_honest_note(self):
        r = rec()
        self.assertTrue(r["cold_start"])
        self.assertIn("profil", r["note"].lower())
        self.assertIn("IMDb", r["note"])
        top = ids(r["ranked"])
        self.assertEqual(top[0], uid(1))                      # 9,2 avec 2 M de votes
        self.assertLess(top.index(uid(1)), top.index(uid(6)))  # 9,6 mais 25 votes : confiance faible
        tonight = row(r, "tonight")
        self.assertEqual(tonight["title"], "Le choix de ce soir")
        self.assertEqual(len(tonight["items"]), 1)
        q = [x for x in tonight["items"][0]["reasons"] if x["code"] == "quality"][0]
        self.assertEqual(q["text"], "IMDb 9,2/10 · 2 000 000 votes")

    def test_strict_filtering_seen_disliked_snoozed_absent_and_unstable(self):
        p = self.profile((1, "seen"), (2, "dislike"), (3, "not-tonight"))
        r = rec(profile=p, absent={"8"})
        every = set(ids(r["ranked"]))
        for x in r["rows"]:
            every |= set(ids(x["items"]))
        for sid in (1, 2, 3, 8):
            self.assertNotIn(uid(sid), every, sid)
        self.assertFalse([x for x in every if not re.match(r"^films/\d+$", x)])   # sans site_id : exclu
        # pas ce soir expiré → de retour ; vu reste exclu
        r2 = rec(profile=p, now=NOW + FY.NOT_TONIGHT_S + 10)
        self.assertIn(uid(3), ids(r2["ranked"]))
        self.assertNotIn(uid(1), ids(r2["ranked"]))

    def test_learned_taste_prioritized_and_explained(self):
        p = self.profile((4, "like"), (9, "dislike"))
        r = rec(profile=p)
        self.assertFalse(r["cold_start"])
        ranked = ids(r["ranked"])
        self.assertEqual(ranked[0], uid(1))                   # Drame aimé + meilleure qualité
        self.assertLess(ranked.index(uid(6)), ranked.index(uid(2)))   # goût appris avant la qualité
        self.assertNotIn(uid(9), ranked)                      # « Pas pour moi » exclu
        because = row(r, "because")
        self.assertEqual(because["title"], "Parce que tu as aimé « Petit Drame »")
        self.assertTrue(because["items"])
        for x in because["items"]:
            self.assertIn("Drame", x["genres"])
            self.assertIn("Parce que tu as aimé « Petit Drame »", [y["text"] for y in x["reasons"]])

    def test_providers_stay_separate_and_scores_are_never_invented(self):
        r = rec()
        allx = {x["id"]: x for x in r["ranked"]}
        tm = allx[uid(7)]
        self.assertIsNone(tm["imdb"])
        self.assertEqual(tm["tmdb"], {"rating": 7.9, "votes": 4200})
        self.assertIn("TMDb 7,9/10 · 4 200 votes", [x["text"] for x in tm["reasons"]])
        self.assertNotIn("IMDb", json.dumps(tm["reasons"], ensure_ascii=False))
        unk = allx[uid(5)]
        self.assertEqual((unk["imdb"], unk["tmdb"]), (None, None))
        self.assertFalse([x for x in unk["reasons"] if x["code"] == "quality"])
        top = row(r, "top")
        self.assertEqual(top["title"], "Les mieux notés que tu n'as pas vus")
        self.assertNotIn(uid(5), ids(top["items"]))
        self.assertNotIn(uid(6), ids(top["items"]))           # 25 votes : pas « mieux noté »
        for x in r["ranked"]:
            self.assertNotIn("score", x)

    def test_deterministic_ordering_independent_of_input_order(self):
        base = json.dumps(rec(), sort_keys=True)
        for seed in range(5):
            shuffled = list(CAT)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(json.dumps(rec(items=shuffled), sort_keys=True), base)

    def test_diversity_avoids_consecutive_same_genre_when_possible(self):
        items = [it(i, "Drame %d" % i, "Drame", 2020, 8.0, 50000 - i) for i in range(1, 6)] + \
                [it(10 + i, "Comédie %d" % i, "Comédie", 2020, 7.0, 50000) for i in range(1, 3)]
        ranked = rec(items=items)["ranked"]
        g = [x["genres"][0] for x in ranked]
        self.assertEqual(g[:4], ["Drame", "Comédie", "Drame", "Comédie"])
        self.assertEqual(ids(ranked)[0], uid(1))             # la priorité de tête est conservée

    def test_rows_unique_bounded_and_titled_in_french(self):
        many = [it(i, "Film %03d" % i, ["Drame", "Comédie", "Action"][i % 3], 2026, 6.0 + (i % 30) / 10, 5000 + i)
                for i in range(1, 120)]
        r = rec(items=many)
        seen = []
        for x in r["rows"]:
            self.assertLessEqual(len(x["items"]), FY.ROW_MAX)
            seen += ids(x["items"])
        self.assertEqual(len(seen), len(set(seen)))
        titles = [x["title"] for x in r["rows"]]
        self.assertEqual(titles[0], "Le choix de ce soir")
        self.assertIn("Nouveautés disponibles", titles)
        self.assertIn("Les mieux notés que tu n'as pas vus", titles)

    def test_novelty_from_accepted_snapshot_diff(self):
        s = self.store(NOW - 10 * DAY)
        s.observe_catalogue([uid(x["site_id"]) for x in CAT[:5] if x["site_id"]])
        self.now[0] = NOW - 2 * DAY
        s.observe_catalogue([uid(x["site_id"]) for x in CAT if x["site_id"]])
        r = rec(novelty=s.novelty())
        new = row(r, "new")
        self.assertEqual(new["title"], "Nouveautés disponibles")
        got = ids(new["items"])
        self.assertTrue(set(got) <= {uid(i) for i in range(6, 11)}, got)
        self.assertTrue(got)
        reason = [y["text"] for y in new["items"][0]["reasons"] if y["code"] == "new"]
        self.assertEqual(reason, ["Ajouté au catalogue le 24/10/2026"])
        # au-delà de la fenêtre de nouveauté, plus de « nouveau » (repli sur l'année de sortie)
        r2 = rec(novelty=s.novelty(), now=NOW + 60 * DAY)
        self.assertFalse([y for x in r2["ranked"] for y in x["reasons"] if y["code"] == "new"])

    def test_novelty_fallback_uses_release_year_honestly(self):
        r = rec()
        new = row(r, "new")
        self.assertTrue(new and new["items"])
        for x in new["items"]:
            self.assertGreaterEqual(x["year"], 2025)
            self.assertIn("recent", [y["code"] for y in x["reasons"]])
            self.assertNotIn("new", [y["code"] for y in x["reasons"]])

    def test_watchlist_row_and_tonight_pick(self):
        p = self.profile((3, "watchlist"))
        r = rec(profile=p)
        self.assertEqual(ids(row(r, "tonight")["items"]), [uid(3)])
        self.assertIn("Dans ta liste « À voir plus tard »", [y["text"] for y in row(r, "tonight")["items"][0]["reasons"]])
        p2 = self.profile((3, "watchlist"), (8, "watchlist"))
        r2 = rec(profile=p2)
        self.assertEqual(row(r2, "watchlist")["title"], "À voir plus tard")
        self.assertEqual(ids(row(r2, "watchlist")["items"]), [uid(8)])   # l'autre est déjà « ce soir »

    def test_empty_catalogue_and_empty_profile(self):
        r = rec(items=[])
        self.assertEqual((r["rows"], r["ranked"], r["cold_start"]), ([], [], True))


# ----------------------------------------------------------------------------- goût appris : toutes les rangées
ANIM = "Animation, Comédie, Famille"
TOY = [
    # trois « Pas pour moi » d'animation (dont Toy Story 3, comme sur le Pi réel)
    it(31, "Toy Story 3", ANIM, 2010, 8.3, 900000),
    it(32, "Les Minions", "Animation, Comédie", 2015, 6.4, 250000),
    it(33, "Cars 3", "Animation, Aventure", 2017, 6.7, 120000),
    # animation non marquée : excellente note, récente, ajoutée en dernier → dominait new/top
    it(41, "Toy Story 2", ANIM, 1999, 7.9, 650000),
    it(42, "Toy Story 4", ANIM, 2026, 7.7, 300000),
    it(43, "Vaiana 3", "Animation, Aventure", 2026, 7.5, 40000),
    it(44, "Kung Fu Panda 5", "Animation", 2025, 7.0, 20000),
    # genres neutres : notes plus basses, ajouts plus anciens
    it(51, "Polar de minuit", "Policier", 1995, 7.4, 400000),
    it(52, "Drame d'hiver", "Drame", 2026, 6.6, 8000),
    it(53, "Thriller nordique", "Thriller", 2025, 6.3, 5000),
    it(54, "Western sec", "Western", 1968, 7.2, 90000),
    it(55, "Docu océan", "Documentaire", 2026, 6.9, 3000),
]
TOY_ANIMATED = {uid(s) for s in (41, 42, 43, 44)}
AUTOMATIC_ROWS = ("tonight", "because", "new", "top", "more")


class LearnedTasteRowTests(Tmp):
    """Régression Pi réel : 10 « Pas pour moi » dont Toy Story 3, et pourtant Toy Story 2/4 en tête des rangées
    « Nouveautés » et « Les mieux notés » (triées par date/note en ignorant le goût appris)."""

    def profile(self, *acts, items=TOY):
        s = self.store()
        cat = {uid(x["site_id"]): x for x in items}
        for i, (sid, a) in enumerate(acts):
            self.now[0] = NOW - 1000 + i
            s.apply(uid(sid), a, origin="tv", title=cat[uid(sid)]["title"], genre=cat[uid(sid)]["genre"])
        self.now[0] = NOW
        return s

    def dislikes(self):
        return self.profile((31, "dislike"), (32, "dislike"), (33, "dislike"))

    def novelty(self):
        """Ajouts détectés par instantanés : l'animation est la plus récente (cas le plus défavorable)."""
        s = self.store(NOW - 20 * DAY)
        base = [uid(x["site_id"]) for x in TOY if x["site_id"] not in ("41", "42", "43", "52", "55")]
        s.observe_catalogue(base)
        self.now[0] = NOW - 10 * DAY
        s.observe_catalogue(base + [uid(52), uid(55)])
        self.now[0] = NOW - 1 * DAY
        s.observe_catalogue([uid(x["site_id"]) for x in TOY])
        return s.novelty()

    def assert_animation_after_neutral(self, r, rid, animated=TOY_ANIMATED):
        w = row(r, rid)
        if not w:
            return
        got = ids(w["items"])
        flags = [u in animated for u in got]
        if True in flags:
            self.assertNotIn(False, flags[flags.index(True):], "%s : %s" % (rid, got))

    def test_dislike_excludes_exact_film_and_weighs_minus_one_per_genre(self):
        s = self.profile((31, "dislike"))
        r = rec(items=TOY, profile=s.profile())
        self.assertEqual(r["profile"]["genres"], {"Animation": -1.0, "Comédie": -1.0, "Famille": -1.0})
        every = set(ids(r["ranked"]))
        for w in r["rows"]:
            every |= set(ids(w["items"]))
        self.assertNotIn(uid(31), every)
        r3 = rec(items=TOY, profile=self.dislikes().profile())
        self.assertEqual(r3["profile"]["genres"],
                         {"Animation": -3.0, "Aventure": -1.0, "Comédie": -2.0, "Famille": -1.0})

    def test_animation_dislikes_demote_animation_in_every_automatic_row_release_year_fallback(self):
        r = rec(items=TOY, profile=self.dislikes().profile())
        self.assertFalse(r["cold_start"])
        self.assertNotIn(uid(41), ids(row(r, "tonight")["items"]))
        self.assertNotIn(ids(row(r, "tonight")["items"])[0], TOY_ANIMATED)
        for rid in AUTOMATIC_ROWS:
            self.assert_animation_after_neutral(r, rid)
        # bogue d'origine : « Nouveautés » (année) et « Mieux notés » (note) commençaient par l'animation
        self.assertNotIn(ids(row(r, "new")["items"])[0], TOY_ANIMATED)
        self.assertNotIn(ids(row(r, "top")["items"])[0], TOY_ANIMATED)
        ranked = ids(r["ranked"])
        last_neutral = max(ranked.index(uid(s)) for s in (51, 52, 53, 54, 55))
        self.assertTrue(all(ranked.index(u) > last_neutral for u in TOY_ANIMATED), ranked)

    def test_animation_dislikes_demote_animation_even_when_most_recently_added(self):
        r = rec(items=TOY, profile=self.dislikes().profile(), novelty=self.novelty())
        new = ids(row(r, "new")["items"])
        for rid in AUTOMATIC_ROWS:
            self.assert_animation_after_neutral(r, rid)
        # goût d'abord (0 > -4 > -6), puis le critère propre de la rangée (date d'ajout), puis la qualité
        self.assertEqual(new, [uid(55), uid(52), uid(43), uid(41), uid(42)])

    def test_row_specific_criterion_still_orders_films_of_equal_taste(self):
        r = rec(items=TOY, profile=self.dislikes().profile())
        top = row(r, "top")["items"]
        neutral = [x for x in top if x["id"] not in TOY_ANIMATED]
        self.assertEqual([x["imdb"]["rating"] for x in neutral],
                         sorted([x["imdb"]["rating"] for x in neutral], reverse=True))
        for x in top:                                          # libellés fournisseurs intacts
            self.assertIn(x["imdb"]["rating"], [y["imdb_rating"] for y in TOY])
            self.assertTrue([y for y in x["reasons"] if y["code"] == "quality" and y["text"].startswith("IMDb ")])

    def test_cold_start_rows_keep_their_own_criterion(self):
        r = rec(items=TOY)
        self.assertTrue(r["cold_start"])
        top = row(r, "top")["items"]
        self.assertEqual([x["imdb"]["rating"] for x in top], sorted([x["imdb"]["rating"] for x in top], reverse=True))
        self.assertEqual(ids(row(r, "tonight")["items"]), [uid(31)])     # meilleure note, profil vide
        rn = rec(items=TOY, novelty=self.novelty())
        firsts = [x["added_at"] for x in row(rn, "new")["items"]]
        self.assertEqual(firsts, sorted(firsts, reverse=True))

    def test_because_row_only_shows_positive_taste_and_explains_it(self):
        items = TOY + [it(61, "Comédie du dimanche", "Comédie", 2019, 6.5, 30000),
                       it(62, "Comédie de bureau", "Comédie", 2021, 6.2, 20000),
                       it(63, "Grande comédie", "Comédie", 2003, 7.8, 500000)]
        # Animation -3, Aventure -2, Comédie +1 : Toy Story 3 (8,3) partage « Comédie » mais reste négatif (-2)
        s = self.profile((33, "dislike"), (43, "dislike"), (44, "dislike"), (61, "like"), items=items)
        r = rec(items=items, profile=s.profile())
        self.assertEqual(ids(row(r, "tonight")["items"]), [uid(63)])
        because = row(r, "because")
        self.assertEqual(because["title"], "Parce que tu as aimé « Comédie du dimanche »")
        self.assertEqual(ids(because["items"]), [uid(62)])
        for x in because["items"]:
            self.assertIn("Parce que tu as aimé « Comédie du dimanche »", [y["text"] for y in x["reasons"]])
        animated = {uid(x) for x in (31, 32, 41, 42)}
        for rid in AUTOMATIC_ROWS:
            self.assert_animation_after_neutral(r, rid, animated)

    def test_explicit_watchlist_survives_genre_penalty(self):
        s = self.profile((31, "dislike"), (32, "dislike"), (33, "dislike"), (42, "watchlist"))
        r = rec(items=TOY, profile=s.profile())
        self.assertEqual(ids(row(r, "tonight")["items"]), [uid(42)])     # choix explicite pour ce film précis
        self.assertIn("Dans ta liste « À voir plus tard »",
                      [y["text"] for y in row(r, "tonight")["items"][0]["reasons"]])
        s2 = self.profile((43, "watchlist"))                   # même base : 42 et 43 dans la liste
        r2 = rec(items=TOY, profile=s2.profile())
        self.assertEqual(ids(row(r2, "tonight")["items"]), [uid(43)])    # le moins pénalisé de la liste
        self.assertEqual(ids(row(r2, "watchlist")["items"]), [uid(42)])
        for rid in ("new", "top", "more"):
            self.assert_animation_after_neutral(r2, rid)

    def test_browsing_opening_and_elapsed_time_never_change_taste(self):
        s = self.dislikes()
        before = rec(items=TOY, profile=s.profile())
        for _ in range(30):
            s.record_signal(uid(41), "opened", app="pourtoi")
            s.record_signal(uid(51), "opened", app="catalogue")
        self.now[0] = NOW + 5 * DAY
        after = rec(items=TOY, profile=s.profile(), now=NOW + 5 * DAY)
        self.assertEqual(json.dumps(after, sort_keys=True), json.dumps(before, sort_keys=True))
        s.apply(uid(41), "seen")                                # « Déjà vu » : exclu, mais neutre pour le goût
        self.assertEqual(rec(items=TOY, profile=s.profile(), now=NOW + 5 * DAY)["profile"], before["profile"])

    def test_genre_matching_ignores_case_and_accents(self):
        s = self.store()
        s.apply(uid(31), "dislike", origin="tv", title="Toy Story 3", genre="animation, comedie")
        r = rec(items=TOY, profile=s.profile())
        self.assertEqual(r["profile"]["genres"], {"animation": -1.0, "comedie": -1.0})
        animated = {uid(x) for x in (32, 33, 41, 42, 43, 44)}
        for rid in AUTOMATIC_ROWS:
            self.assert_animation_after_neutral(r, rid, animated)

    def test_deterministic_and_input_order_independent_with_learned_taste(self):
        p, nv = self.dislikes().profile(), self.novelty()
        base = json.dumps(rec(items=TOY, profile=p, novelty=nv), sort_keys=True)
        for seed in range(6):
            shuffled = list(TOY)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(json.dumps(rec(items=shuffled, profile=p, novelty=nv), sort_keys=True), base)


# ----------------------------------------------------------------------------- navigation télécommande
class NavTests(unittest.TestCase):
    def nav(self, marks=None):
        r = rec()
        return FY.Nav(r["rows"], marks or {}), r

    def test_grid_moves_with_per_row_memory(self):
        n, r = self.nav()
        self.assertEqual((n.row, n.col, n.panel), (0, 0, False))
        self.assertEqual(n.key("down"), ("stay",))
        n.key("right"); n.key("right")
        self.assertEqual(n.col, 2)
        n.key("up")
        self.assertEqual((n.row, n.col), (0, 0))
        n.key("down")
        self.assertEqual(n.col, 2)                            # colonne mémorisée par rangée
        for _ in range(50):
            n.key("right")
        self.assertEqual(n.col, len(r["rows"][1]["items"]) - 1)
        for _ in range(20):
            n.key("down")
        self.assertEqual(n.row, len(r["rows"]) - 1)
        self.assertEqual(n.view()["cursor"], sum(len(x["items"]) for x in r["rows"][:-1]) + n.col)

    def test_ok_opens_panel_with_watch_first_and_back_closes(self):
        n, r = self.nav()
        self.assertEqual(n.key("ok"), ("stay",))
        self.assertTrue(n.panel)
        self.assertEqual(n.view()["btn"], 0)
        self.assertEqual(FY.BUTTONS[0], ("watch", "Regarder"))
        self.assertEqual([b[1] for b in FY.BUTTONS],
                         ["Regarder", "J'aime", "Pas pour moi", "Déjà vu", "À voir plus tard", "Pas ce soir"])
        act = n.key("ok")
        self.assertEqual(act[0], "open")
        self.assertEqual(act[1]["id"], r["rows"][0]["items"][0]["id"])
        self.assertEqual(n.key("back"), ("stay",))
        self.assertFalse(n.panel)
        self.assertEqual(n.key("back"), ("end",))

    def test_panel_actions_toggle_from_marks(self):
        n, r = self.nav()
        first = r["rows"][0]["items"][0]["id"]
        n.key("ok")
        n.key("right")
        self.assertEqual(n.key("ok"), ("act", first, "like"))
        n.marks[first] = {"opinion": "like", "seen": False, "watchlist": False, "not_tonight": False}
        self.assertEqual(n.key("ok"), ("act", first, "clear-opinion"))
        n.key("right")
        self.assertEqual(n.key("ok"), ("act", first, "dislike"))
        n.key("right")
        self.assertEqual(n.key("ok"), ("act", first, "seen"))
        n.key("right")
        self.assertEqual(n.key("ok"), ("act", first, "watchlist"))
        n.key("right")
        self.assertEqual(n.key("ok"), ("act", first, "not-tonight"))
        for _ in range(5):
            n.key("right")
        self.assertEqual(n.view()["btn"], len(FY.BUTTONS) - 1)
        n.key("up"); n.key("down")
        self.assertTrue(n.panel)                              # haut/bas ignorés dans le panneau

    def test_empty_rows(self):
        n = FY.Nav([], {})
        self.assertEqual(n.key("ok"), ("stay",))
        self.assertEqual(n.key("right"), ("stay",))
        self.assertIsNone(n.current())
        self.assertEqual(n.key("back"), ("end",))


# ----------------------------------------------------------------------------- CLI agent
class CliTests(Tmp):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)
        B.atomic_write_json(os.path.join(self.state, "catalogue.json"),
                            {"source": "fakrov.com", "generated_at": "2026-10-25T00:00:00Z", "items": CAT})
        self.env = {"STATE_DIR": self.state, "SITE_ROOT": "https://fakrov.com/", "HOME": self.d}

    def cli(self, *args, now=NOW):
        out = io.StringIO()
        code = FY.main(["opentv_foryou.py"] + list(args), env=self.env, now=now, out=out)
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_status_and_cold_recommend(self):
        code, st, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(st["catalogue"]["items"], 11)               # dont 1 sans site_id : non annotable
        self.assertEqual(st["catalogue"]["with_stable_id"], 10)
        self.assertEqual(st["profile"], {"disliked": 0, "liked": 0, "not_tonight": 0, "seen": 0, "watchlist": 0})
        self.assertEqual(st["schema"], FY.SCHEMA_VERSION)
        code, r, raw = self.cli("recommend", "--limit", "3")
        self.assertEqual((code, r["ok"], len(r["items"]), r["has_more"], r["cold_start"]), (0, True, 3, True, True))
        self.assertEqual(r["items"][0]["id"], uid(1))
        self.assertEqual(r["tonight"]["id"], uid(1))
        code, r2, raw2 = self.cli("recommend", "--limit", "3", "--offset", "3")
        self.assertEqual(r2["offset"], 3)
        self.assertFalse(set(ids(r["items"])) & set(ids(r2["items"])))

    def test_json_is_deterministic_and_never_contains_urls(self):
        outs = []
        for args in (("recommend", "--limit", "50"), ("recommend", "--rows"), ("search", "--query", "e"),
                     ("status",), ("history",)):
            a = self.cli(*args)[2]
            b = self.cli(*args)[2]
            self.assertEqual(a, b, args)
            outs.append(a)
        blob = "\n".join(outs)
        for bad in ("http", "://", "b/fakrov", "poster", "detail_path", "m3u8", ".jpg", "source_id"):
            self.assertNotIn(bad, blob, bad)

    def test_search_accent_insensitive_and_bounded(self):
        code, r, _ = self.cli("search", "--query", "eleve")
        self.assertEqual((code, ids(r["items"])), (0, [uid(8)]))
        code, r, _ = self.cli("search", "--query", "RATA", "--limit", "1")
        self.assertEqual(ids(r["items"]), [uid(2)])
        self.assertIn("state", r["items"][0])
        for bad in (["search"], ["search", "--query", ""], ["search", "--query", "x" * 81],
                    ["search", "--query", "a", "--limit", "0"], ["search", "--query", "a", "--limit", "51"],
                    ["recommend", "--limit", "abc"], ["recommend", "--offset", "-1"], ["nope"], []):
            code, r, _ = self.cli(*bad)
            self.assertEqual((code, r["ok"], r["error"]), (2, False, "usage"), bad)

    def test_feedback_history_and_errors(self):
        code, r, _ = self.cli("feedback", "--id", uid(3), "--action", "like")
        self.assertEqual((code, r["changed"], r["state"]["opinion"]), (0, True, "like"))
        code, r, _ = self.cli("feedback", "--id", uid(3), "--action", "like")
        self.assertEqual((code, r["changed"]), (0, False))
        self.cli("feedback", "--id", uid(3), "--action", "seen")
        code, h, _ = self.cli("history")
        self.assertEqual([(e["id"], e["action"], e["origin"]) for e in h["items"]],
                         [(uid(3), "seen", "cli"), (uid(3), "like", "cli")])
        self.assertEqual(h["items"][0]["title"], "Superbad")
        self.assertRegex(h["items"][0]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        code, h, _ = self.cli("history", "--list", "liked")
        self.assertEqual(ids(h["items"]), [uid(3)])
        code, r, _ = self.cli("recommend", "--limit", "50")
        self.assertNotIn(uid(3), ids(r["items"]))           # vu → exclu
        self.assertEqual(self.cli("feedback", "--id", "films/424242", "--action", "like")[:2],
                         (4, {"ok": False, "error": "unknown_id"}))
        for bad in (["feedback", "--id", "17397", "--action", "like"],
                    ["feedback", "--id", uid(3), "--action", "regarder"],
                    ["feedback", "--id", uid(3)]):
            self.assertEqual(self.cli(*bad)[0], 2, bad)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "foryou.sqlite3")).st_mode), 0o600)

    def test_removal_actions_allowed_for_known_uid_outside_catalogue(self):
        for a in ("like", "seen", "watchlist", "not-tonight"):
            self.assertEqual(self.cli("feedback", "--id", uid(3), "--action", a)[0], 0, a)
        B.atomic_write_json(os.path.join(self.state, "catalogue.json"),          # le film quitte le catalogue
                            {"source": "fakrov.com", "items": [x for x in CAT if x["site_id"] != "3"]})
        for a in ("like", "dislike", "seen", "watchlist", "not-tonight"):     # écritures positives : refusées
            self.assertEqual(self.cli("feedback", "--id", uid(3), "--action", a)[:2],
                             (4, {"ok": False, "error": "unknown_id"}), a)
        for a in ("clear-opinion", "unseen", "unwatchlist", "clear-not-tonight"):
            code, r, _ = self.cli("feedback", "--id", uid(3), "--action", a)
            self.assertEqual((code, r["ok"], r["changed"], r["title"]), (0, True, True, "Superbad"), a)
        self.assertEqual(self.cli("feedback", "--id", uid(3), "--action", "unseen")[1]["changed"], False)
        self.assertEqual(self.cli("feedback", "--id", uid(3), "--action", "clear-opinion")[1]["state"],
                         {"opinion": None, "seen": False, "watchlist": False, "not_tonight": False})
        for a in ("clear-opinion", "unseen"):                                   # UID jamais vu : inconnu
            self.assertEqual(self.cli("feedback", "--id", "films/424242", "--action", a)[:2],
                             (4, {"ok": False, "error": "unknown_id"}), a)

    def test_store_failure_exit_3(self):
        os.makedirs(os.path.join(self.state, "foryou.sqlite3"))    # un dossier à la place de la base
        code, r, _ = self.cli("feedback", "--id", uid(3), "--action", "like")
        self.assertEqual((code, r["ok"], r["error"]), (3, False, "store_unavailable"))
        code, r, _ = self.cli("recommend")                       # lecture : profil vide, toujours utilisable
        self.assertEqual((code, r["cold_start"], r["store"]), (0, True, "unavailable"))

    def test_real_subprocess_isolated_mode(self):
        env = dict(self.env, PATH=os.environ.get("PATH", ""))
        p = subprocess.run([sys.executable, "-I", FY_SRC, "feedback", "--id", uid(2), "--action", "watchlist"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["changed"])
        p = subprocess.run([sys.executable, "-I", FY_SRC, "recommend", "--limit", "2"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["tonight"]["id"], uid(2))
        self.assertEqual(p.stderr, "")


# ----------------------------------------------------------------------------- aucune URL dans la CLI agent
URLISH = re.compile(r"https?://", re.I)


def strings(obj, path="$"):
    """Toutes les chaînes d'un objet JSON (clés comprises), avec leur chemin."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "{key}", k
            yield from strings(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from strings(v, "%s[%d]" % (path, i))


EVIL = [   # titres/résumés/genres malicieux ou accidentels : URL de flux, d'affiche, de fiche
    it(21, "Piège http://cdn.evil.example/live/stream.m3u8", "Drame", 2026, 8.9, 90000,
       description="Lien https://cdn.evil.example/a.mp4?token=x et www.evil.example/b.m3u8"),
    it(22, "HTTPS://image.tmdb.org/t/p/w300/affiche.jpg", "Comédie", 2025, 8.5, 50000),
    it(23, "Fiche https://fakrov.com/aab2ae3/b/fakrov/17397 (VF)", "Drame, http://genre.example/x", 2026, 7.0, 20000,
       description="//cdn.evil.example/c.mp4 seulement"),
    it(24, "Chef-d'œuvre confidentiel", "Drame", 2025, 9.6, 25),
    it(25, "Élève modèle : 2/3 // partie A", "Comédie", 2026, 6.8, 15000),
    it(26, "hthttp://x.example/ttp://y.example/z", "Horreur", 2026, 5.0, 12000),
]


class CliNoUrlTests(Tmp):
    """Règle stricte : aucune valeur textuelle (titre, requête, raisons, note, objets imbriqués, détail
    d'erreur) des sorties status|recommend|search|feedback|history ne contient http:// ou https://."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)
        B.atomic_write_json(os.path.join(self.state, "catalogue.json"),
                            {"source": "fakrov.com", "generated_at": "https://evil.example/when", "items": CAT + EVIL})
        self.env = {"STATE_DIR": self.state, "SITE_ROOT": "https://fakrov.com/", "HOME": self.d}

    def cli(self, *args):
        out = io.StringIO()
        code = FY.main(["opentv_foryou.py"] + list(args), env=self.env, now=NOW, out=out)
        return code, json.loads(out.getvalue()), out.getvalue()

    def assertNoUrl(self, obj, raw, ctx):
        leaks = [(p, v) for p, v in strings(obj) if URLISH.search(v)]
        self.assertEqual(leaks, [], ctx)
        self.assertIsNone(URLISH.search(raw), ctx)
        for bad in ("m3u8", ".mp4", "affiche.jpg", "b/fakrov", "evil.example"):
            self.assertNotIn(bad, raw, (ctx, bad))

    def test_every_command_output_is_url_free_including_titles_reasons_and_history(self):
        for sid in (21, 23):                                     # titres piégés mémorisés en base
            self.assertEqual(self.cli("feedback", "--id", uid(sid), "--action", "like")[0], 0)
        self.cli("feedback", "--id", uid(22), "--action", "watchlist")
        st = FY.Store(os.path.join(self.state, "foryou.sqlite3"), clock=lambda: NOW)
        st.record_signal(uid(22), "opened", app="pourtoi")
        st.db.execute("UPDATE prefs SET title='Ancien https://old.example/x.m3u8' WHERE uid=?", (uid(23),))
        st.close()                                               # base héritée : titre brut déjà stocké
        cmds = [("status",), ("recommend", "--limit", "50"), ("recommend", "--rows"), ("search", "--query", "piege"),
                ("search", "--query", "fiche"), ("search", "--query", "e", "--limit", "50"),
                ("feedback", "--id", uid(21), "--action", "seen"), ("feedback", "--id", uid(26), "--action", "like"),
                ("history",), ("history", "--list", "liked"), ("history", "--list", "watchlist"),
                ("history", "--list", "opened")]
        for args in cmds:
            code, obj, raw = self.cli(*args)
            self.assertEqual(code, 0, (args, raw))
            self.assertNoUrl(obj, raw, args)
        code, r, raw = self.cli("recommend", "--rows")
        because = [w["title"] for w in r["rows"] if w["id"] == "because"]
        self.assertEqual(because, ["Parce que tu as aimé « Piège »"])          # titre aimé nettoyé, pas vidé

    def test_url_query_is_refused_without_echo(self):
        for q in ("http://cdn.evil.example/stream.m3u8", "HTTPS://fakrov.com/x", "www.evil.example",
                  "voir https://x.example/y", "//cdn.evil.example/a"):
            code, r, raw = self.cli("search", "--query", q)
            self.assertEqual((code, r["ok"], r["error"]), (2, False, "usage"), q)
            self.assertNotIn("evil", raw)
            self.assertNoUrl(r, raw, q)
        code, r, raw = self.cli("search", "--query", "Piège")              # requête normale : réaffichée
        self.assertEqual((code, r["query"], ids(r["items"])), (0, "Piège", [uid(21)]))

    def test_other_schemes_and_bare_domains_are_cleaned_and_refused(self):
        dirty = {
            "Aimant magnet:?xt=urn:btih:abc&dn=film fin": "Aimant fin",
            "Image data:text/html;base64,PHNjcmlwdD4= ici": "Image ici",
            "Clic javascript:alert(document.cookie) ok": "Clic ok",
            "JavaScript:void(0)": "",
            "Voir evil.example/stream.m3u8 maintenant": "Voir maintenant",
            "cdn.evil.example:8443/a.mp4": "",
            "Miroir sub.evil-site.co.uk/b/fakrov/1 (VF)": "Miroir (VF)",
            "IP 10.0.0.7/live.m3u8": "IP",
        }
        for src, want in dirty.items():
            self.assertEqual(FY.clean_text(src), want, src)
            self.assertEqual(FY.clean_text(want), want)
        keep = ("Mission // Impossible", "Élève modèle : 2/3 // partie A", "Big Data: le film", "Dr. Folamour",
                "S.W.A.T. / Unité d'élite", "Ocean's 8", "Mission: Impossible", "AC/DC : Live", "1/2 Prince",
                "Data : la série", "M.A.S.H", "Vol. 2/3")
        for t in keep:
            self.assertEqual(FY.clean_text(t), t, t)
        for q in ("magnet:?xt=urn:btih:abc", "data:text/html,hi", "javascript:alert(1)", "JAVASCRIPT:x",
                  "evil.example/stream.m3u8", "cdn.evil.example:8443/a", "10.0.0.7/live"):
            code, r, raw = self.cli("search", "--query", q)
            self.assertEqual((code, r["error"]), (2, "usage"), q)
            self.assertNotIn("evil", raw)
            self.assertNotIn("btih", raw)
            self.assertNotIn("alert", raw)
        for q in ("Mission // Impossible", "Big Data: le film", "Dr. Folamour"):
            self.assertEqual(self.cli("search", "--query", q)[0], 0, q)

    def test_usage_errors_never_echo_urls(self):
        for args in (("feedback", "--id", uid(21), "--action", "https://evil.example/x.m3u8"),
                     ("feedback", "--id", "https://evil.example/1", "--action", "like"),
                     ("history", "--list", "http://evil.example/"), ("recommend", "http://evil.example/x"),
                     ("https://evil.example/cmd",)):
            code, r, raw = self.cli(*args)
            self.assertEqual((code, r["error"]), (2, "usage"), args)
            self.assertNoUrl(r, raw, args)

    def test_normal_titles_are_preserved_exactly(self):
        code, r, _ = self.cli("recommend", "--limit", "50")
        titles = {x["id"]: x["title"] for x in r["items"]}
        self.assertEqual(titles[uid(24)], "Chef-d'œuvre confidentiel")
        self.assertEqual(titles[uid(25)], "Élève modèle : 2/3 // partie A")
        self.assertEqual(titles[uid(1)], "Le Parrain")
        self.assertEqual(titles[uid(21)], "Piège")
        self.assertEqual(titles[uid(23)], "Fiche (VF)")
        self.assertEqual(titles[uid(22)], "")                    # titre réduit à une URL : vidé, jamais réaffiché
        self.assertEqual([x["genres"] for x in r["items"] if x["id"] == uid(23)], [["Drame"]])

    def test_scrub_is_global_and_idempotent(self):
        obj = {"a": ["ok", {"b": "x http://e.example/y z", "https://k.example/": 1}], "n": None, "i": 3,
               "c": "hthttp://x.example/ttp://y.example/z"}
        clean = FY.scrub(obj)
        self.assertEqual(clean["a"][1]["b"], "x z")
        self.assertEqual(FY.scrub(clean), clean)
        self.assertFalse([v for _, v in strings(clean) if URLISH.search(v)])
        self.assertEqual((clean["n"], clean["i"], clean["a"][0]), (None, 3, "ok"))

    def test_ui_keeps_only_https_posters_and_no_other_url(self):
        r = FY.recommend(CAT + EVIL, SRC_KEY, {}, {}, NOW)
        data = FY.ui_data(r, CAT + EVIL, SRC_KEY)
        for p, v in strings(data):
            if URLISH.search(v):
                self.assertTrue(p.endswith(".poster") and v.startswith("https://image.tmdb.org/"), (p, v))


# ----------------------------------------------------------------------------- identité stable (FORYOU_SOURCE_ID)
class StableIdentityTests(Tmp):
    """L'UID est « <FORYOU_SOURCE_ID>/<site_id> » : indépendant du domaine, de SITE_ROOT, du préfixe, du jeton
    et des routes. Changer SITE_ROOT (fakrov → wadrox) n'orpheline aucune préférence."""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.d, "state")
        os.makedirs(self.state, mode=0o700)

    def write_cat(self, host, token, prefix):
        items = [dict(x, detail_path="b/%s/%d" % (token, 5000 + i), source_id=str(5000 + i),
                      detail_url="https://%s/%s/b/%s/%d" % (host, prefix, token, 5000 + i))
                 for i, x in enumerate(CAT)]
        B.atomic_write_json(os.path.join(self.state, "catalogue.json"), {"source": host, "items": items})

    def cli(self, site_root, *args, **env):
        e = {"STATE_DIR": self.state, "SITE_ROOT": site_root, "HOME": self.d}
        e.update(env)
        out = io.StringIO()
        code = FY.main(["opentv_foryou.py"] + list(args), env=e, now=NOW, out=out)
        return code, json.loads(out.getvalue())

    def test_site_root_change_fakrov_to_wadrox_keeps_uid_history_and_feedback(self):
        self.write_cat("fakrov.com", "fakrov", "aab2ae3")
        code, r = self.cli("https://fakrov.com/", "feedback", "--id", "films/3", "--action", "like")
        self.assertEqual((code, r["id"]), (0, "films/3"))
        self.cli("https://fakrov.com/", "feedback", "--id", "films/2", "--action", "watchlist")
        before = self.cli("https://fakrov.com/", "recommend", "--limit", "50")[1]
        # l'opérateur bascule l'ancre et le site migre : autre domaine, autre jeton, autre préfixe/routes
        self.write_cat("wadrox.com", "wadrox", "99c7f65")
        code, st = self.cli("https://wadrox.com/", "status")
        self.assertEqual((code, st["source"]), (0, "films"))
        for lst, want in (("liked", "films/3"), ("watchlist", "films/2")):
            code, h = self.cli("https://wadrox.com/", "history", "--list", lst)
            self.assertEqual([(x["id"], x["in_catalogue"]) for x in h["items"]], [(want, True)], lst)
        code, h = self.cli("https://wadrox.com/", "history")
        self.assertEqual({x["id"] for x in h["items"]}, {"films/3", "films/2"})
        code, r = self.cli("https://wadrox.com/", "feedback", "--id", "films/3", "--action", "like")
        self.assertEqual((code, r["changed"], r["state"]["opinion"]), (0, False, "like"))   # même ligne, idempotent
        after = self.cli("https://wadrox.com/", "recommend", "--limit", "50")[1]
        self.assertEqual(ids(after["items"]), ids(before["items"]))
        self.assertEqual(after["tonight"]["id"], "films/2")
        code, r = self.cli("https://wadrox.com/", "search", "--query", "superbad")
        self.assertEqual(r["items"][0]["state"]["opinion"], "like")
        # aucun nouveau film « nouveau » : la migration de domaine ne change pas l'ensemble d'identifiants
        self.assertEqual(self.cli("https://wadrox.com/", "status")[1]["snapshots"]["count"], 1)

    def test_uid_never_depends_on_domain_and_is_strictly_validated(self):
        self.assertEqual(FY.DEFAULT_SOURCE_ID, "films")
        x = it(7, "X")
        for root in ("https://fakrov.com/", "https://wadrox.com/", "", None):
            self.assertEqual(FY.uid_for(FY.source_id({"SITE_ROOT": root} if root is not None else {}), x), "films/7")
        self.assertEqual(FY.source_id({"FORYOU_SOURCE_ID": "series"}), "series")
        for bad in ("fakrov.com", "Films", "films/x", "", " films", "f", "a" * 25, "https://x", "films\n", "1films", "fi lms"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                FY.source_id({"FORYOU_SOURCE_ID": bad})
        code, r = self.cli("https://fakrov.com/", "status", FORYOU_SOURCE_ID="fakrov.com")
        self.assertEqual((code, r["ok"], r["error"]), (2, False, "config"))
        self.assertNotIn("fakrov", json.dumps(r))

    def test_env_file_supplies_source_id_and_ignores_site_root_for_identity(self):
        self.write_cat("wadrox.com", "wadrox", "99c7f65")
        conf = os.path.join(self.d, ".config/systemd/user")
        os.makedirs(conf)
        with open(os.path.join(conf, "opentv-bridge.env"), "w") as f:
            f.write("SITE_ROOT=https://wadrox.com/\nFORYOU_SOURCE_ID=cine\nSTATE_DIR=%s\n" % self.state)
        out = io.StringIO()
        FY.main(["opentv_foryou.py", "recommend", "--limit", "1"], env={"HOME": self.d}, now=NOW, out=out)
        self.assertEqual(json.loads(out.getvalue())["items"][0]["id"], "cine/1")

    def test_env_file_strips_systemd_quotes(self):
        self.write_cat("wadrox.com", "wadrox", "99c7f65")
        conf = os.path.join(self.d, ".config/systemd/user")
        os.makedirs(conf)
        for line in ('FORYOU_SOURCE_ID="films"', "FORYOU_SOURCE_ID='films'", " FORYOU_SOURCE_ID = \"films\" "):
            with open(os.path.join(conf, "opentv-bridge.env"), "w") as f:
                f.write("# commentaire\n%s\nSTATE_DIR=\"%s\"\n" % (line, self.state))
            self.assertEqual(FY._env_file(self.d), {"FORYOU_SOURCE_ID": "films", "STATE_DIR": self.state}, line)
            out = io.StringIO()
            code = FY.main(["opentv_foryou.py", "recommend", "--limit", "1"], env={"HOME": self.d}, now=NOW, out=out)
            self.assertEqual((code, json.loads(out.getvalue())["items"][0]["id"]), (0, "films/1"), line)
        with open(os.path.join(conf, "opentv-bridge.env"), "w") as f:
            f.write("FORYOU_SOURCE_ID=\"films'\nSTATE_DIR=\"\"\n")              # guillemets non appariés : intacts
        self.assertEqual(FY._env_file(self.d), {"FORYOU_SOURCE_ID": "\"films'"})


# ----------------------------------------------------------------------------- intégration au pont
FY_CAT = {"source": "fakrov.com", "items": [dict(x, detail_path="b/fakrov/%d" % (1000 + i))
                                            for i, x in enumerate(CAT)]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._q = quiet()
        self.out = self._q.__enter__()
        self.h = TB.Harness(catalogue=FY_CAT, env={"APPS": "catalogue,pourtoi"})

    def tearDown(self):
        self._q.__exit__(None, None, None)
        self.h.close()

    def open(self, app="pourtoi"):
        code, b, _ = self.h.req("POST", "/v1/session?app=" + app)
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        return b["session"]

    def key(self, s, k):
        return self.h.req("POST", "/v1/key?session=%s&k=%s" % (s, k))[1]["state"]

    def store(self):
        st = FY.Store(self.h.cfg.foryou_db)
        self.addCleanup(st.close)
        return st

    def test_default_apps_include_pourtoi_and_films(self):
        self.assertEqual(B.Config({}).apps, frozenset({"catalogue", "pourtoi"}))
        self.assertTrue(B.Config({}).foryou_db.endswith("foryou.sqlite3"))
        self.assertEqual(B.Config({}).foryou_source, "films")
        self.assertEqual(B.Config({"SITE_ROOT": "https://wadrox.com/"}).foryou_source, "films")
        self.assertIsNone(B.Config({"FORYOU_SOURCE_ID": "wadrox.com"}).foryou_source)   # invalide : refusé

    def test_bridge_uid_survives_site_root_change(self):
        s = self.open()
        first = self.h.ctl.foryou_data()["rows"][0]["items"][0]["id"]
        self.assertRegex(first, r"^films/[0-9]+$")
        self.key(s, "ok"); self.key(s, "right"); self.key(s, "ok")   # J'aime depuis la télécommande
        self.key(s, "back"); self.key(s, "back")
        self.assertTrue(self.h.wait_state("dashboard"))
        db = self.h.cfg.foryou_db
        moved = {"source": "wadrox.com", "items": [dict(x, detail_path="b/wadrox/%d" % (7000 + i))
                                                   for i, x in enumerate(CAT)]}
        h2 = TB.Harness(catalogue=moved, env={"APPS": "catalogue,pourtoi", "FORYOU_DB": db,
                                              "SITE_ROOT": "https://wadrox.com/"})
        try:
            self.assertEqual(h2.ctl.fy.source, "films")
            marks = {}
            code, b, _ = h2.req("POST", "/v1/session?app=pourtoi")
            self.assertEqual(code, 200, b)
            self.assertTrue(h2.wait_state("catalogue"))
            marks = h2.ctl.view()["marks"]
            self.assertEqual(marks[first]["opinion"], "like")             # même UID, préférence retrouvée
            h2.req("POST", "/v1/close?session=%s&reason=user" % b["session"])
        finally:
            h2.close()

    def test_invalid_source_id_disables_pourtoi_but_keeps_films(self):
        h2 = TB.Harness(catalogue=FY_CAT, env={"APPS": "catalogue,pourtoi", "FORYOU_SOURCE_ID": "Bad.Host"})
        try:
            self.assertIsNone(h2.ctl.fy)
            self.assertEqual(h2.req("POST", "/v1/session?app=pourtoi")[:2], (404, {"ok": False, "error": "unknown_app"}))
            self.assertEqual(h2.req("POST", "/v1/session?app=catalogue")[0], 200)
        finally:
            h2.close()

    def test_bridge_view_and_messages_never_echo_title_urls(self):
        h2 = TB.Harness(catalogue={"items": [dict(x, detail_path="b/fakrov/%d" % (3000 + i))
                                             for i, x in enumerate(EVIL)]}, env={"APPS": "catalogue,pourtoi"})
        try:
            code, b, _ = h2.req("POST", "/v1/session?app=pourtoi")
            self.assertTrue(h2.wait_state("catalogue"))
            for k in ("ok", "right", "ok"):
                h2.req("POST", "/v1/key?session=%s&k=%s" % (b["session"], k))
            blob = json.dumps([h2.ctl.view(), h2.ctl.foryou_data()], ensure_ascii=False)
            for p, v in strings(json.loads(blob)):
                if URLISH.search(v):
                    self.assertTrue(p.endswith(".poster"), (p, v))
            self.assertIn("aimé", h2.ctl.view()["message"])
            h2.req("POST", "/v1/close?session=%s&reason=user" % b["session"])
        finally:
            h2.close()

    def test_pourtoi_session_opens_personal_view_even_with_empty_profile(self):
        s = self.open()
        d = self.h.displays[0]
        self.assertEqual(d.calls[0], ("open", "http://127.0.0.1:0/ui/pourtoi/"))
        v = self.h.ctl.view()
        self.assertEqual((v["app"], v["mode"], v["row"], v["col"], v["panel"]), ("pourtoi", "foryou", 0, 0, False))
        data = self.h.ctl.foryou_data()
        self.assertTrue(data["cold_start"])
        self.assertEqual(data["rows"][0]["title"], "Le choix de ce soir")
        blob = json.dumps(data)
        self.assertNotIn("detail_path", blob)
        self.assertNotIn("b/fakrov", blob)
        self.assertIn('"event": "session_open"', self.out.getvalue())
        self.assertEqual(self.key(s, "back"), "dashboard")

    def test_actions_from_remote_are_persisted_toggle_and_keep_layout(self):
        s = self.open()
        first = self.h.ctl.foryou_data()["rows"][0]["items"][0]["id"]
        self.assertEqual(self.key(s, "ok"), "catalogue")            # panneau d'actions
        self.assertTrue(self.h.ctl.view()["panel"])
        self.key(s, "right")                                         # J'aime
        self.assertEqual(self.key(s, "ok"), "catalogue")
        self.assertEqual(self.store().state(first)["opinion"], "like")
        v = self.h.ctl.view()
        self.assertEqual(v["marks"][first]["opinion"], "like")
        self.assertIn("aimé", v["message"])
        self.key(s, "ok")                                            # rappui = annuler
        self.assertIsNone(self.store().state(first)["opinion"])
        self.key(s, "right"); self.key(s, "right")                   # Déjà vu
        self.key(s, "ok")
        st = self.store().state(first)
        self.assertEqual((st["seen"], st["opinion"]), (True, None))
        rows_before = json.dumps(self.h.ctl.foryou_data()["rows"])
        self.assertEqual(json.dumps(self.h.ctl.foryou_data()["rows"]), rows_before)   # pas de disparition en séance
        self.assertEqual(self.key(s, "back"), "catalogue")           # ferme le panneau
        self.assertFalse(self.h.ctl.view()["panel"])
        self.assertEqual(self.key(s, "back"), "dashboard")
        self.assertTrue(self.h.wait_state("dashboard"))
        time.sleep(0.2)
        self.open()                                                  # nouvelle séance : film vu exclu
        allids = [x["id"] for r in self.h.ctl.foryou_data()["rows"] for x in r["items"]]
        self.assertNotIn(first, allids)

    def test_watch_reuses_film_player_and_records_open_signal_not_seen(self):
        s = self.open()
        first = self.h.ctl.foryou_data()["rows"][0]["items"][0]
        self.key(s, "ok")
        self.assertEqual(self.key(s, "ok"), "player")                # Regarder (premier bouton)
        d = self.h.displays[0]
        idx = [x["title"] for x in FY_CAT["items"]].index(first["title"])
        self.assertEqual(d.calls[-1], ("navigate", "https://fakrov.com/aab2ae3/b/fakrov/%d" % (1000 + idx)))
        self.assertTrue(self.h.ctl.fy.drain(5))                       # signal « ouvert » : file asynchrone bornée
        st = self.store()
        self.assertFalse(st.state(first["id"])["seen"])
        self.assertEqual([(x["id"], x["kind"], x["app"]) for x in st.history("signals")], [(first["id"], "opened", "pourtoi")])
        self.assertEqual(self.key(s, "back"), "catalogue")
        self.assertEqual(d.calls[-1], ("navigate", "http://127.0.0.1:0/ui/pourtoi/"))
        v = self.h.ctl.view()
        self.assertEqual((v["row"], v["col"]), (0, 0))

    def test_films_tile_unchanged_and_coexists(self):
        s = self.open("catalogue")
        self.assertEqual(self.h.displays[0].calls[0], ("open", "http://127.0.0.1:0/ui/"))
        self.assertNotIn("mode", self.h.ctl.view())
        self.assertEqual(self.h.req("POST", "/v1/session?app=pourtoi")[:2], (409, {"ok": False, "error": "busy"}))
        for k in ("right", "down"):
            self.key(s, k)
        self.assertEqual(self.h.ctl.view()["cursor"], 7)
        self.assertEqual(self.key(s, "ok"), "player")
        self.assertTrue(self.h.ctl.fy.drain(5))
        st = self.store()
        self.assertEqual([x["app"] for x in st.history("signals")], ["catalogue"])
        self.assertEqual(st.snapshot_stats()["count"], 1)            # instantané Films (asynchrone) enregistré
        self.assertEqual(st.counts()["seen"], 0)
        self.key(s, "back")
        self.assertEqual(self.key(s, "back"), "dashboard")
        self.assertTrue(self.h.wait_state("dashboard"))
        time.sleep(0.2)
        s2 = self.open("pourtoi")
        self.assertEqual(self.h.displays[-1].calls[0], ("open", "http://127.0.0.1:0/ui/pourtoi/"))
        self.assertEqual(self.h.req("POST", "/v1/close?session=%s&reason=user" % s2)[1]["state"], "dashboard")

    def test_journal_never_contains_titles_or_ids_and_contract_is_unchanged(self):
        s = self.open()
        self.key(s, "ok"); self.key(s, "right"); self.key(s, "ok")
        lines = [json.loads(l) for l in self.out.getvalue().splitlines() if l.startswith("{")]
        fb = [l for l in lines if l["event"] == "foryou_feedback"]
        self.assertEqual(fb and sorted(fb[0]), ["action", "changed", "event", "ts"])
        self.assertNotIn("Le Parrain", self.out.getvalue())
        self.assertEqual(B.STATES, ("starting", "catalogue", "player", "dashboard"))   # contrat API v1 intact
        self.assertEqual(B.Config({}).allow_ips, frozenset({"192.168.2.14"}))
        self.assertEqual(B.Config({}).catalogue_refresh_s, 0)

    def test_missing_module_refuses_pourtoi_but_keeps_films(self):
        self.h.ctl.fy = None
        self.assertEqual(self.h.req("POST", "/v1/session?app=pourtoi")[:2], (404, {"ok": False, "error": "unknown_app"}))
        s = self.open("catalogue")
        self.assertEqual(self.key(s, "ok"), "player")

    def test_store_unavailable_keeps_pourtoi_usable(self):
        self.h.cfg.foryou_db = os.path.join(self.h.d, "dir-not-db")
        os.makedirs(self.h.cfg.foryou_db)
        self.h.ctl.foryou_reset()
        s = self.open()
        self.assertTrue(self.h.ctl.foryou_data()["cold_start"])
        self.key(s, "ok"); self.key(s, "right")
        self.assertEqual(self.key(s, "ok"), "catalogue")
        self.assertIn("non enregistrée", self.h.ctl.view()["message"])
        self.assertIn("foryou_store_error", self.out.getvalue())

    def test_view_marks_are_a_copy(self):
        self.open()
        v = self.h.ctl.view()
        v["marks"]["films/999"] = {"opinion": "like"}
        self.assertNotIn("films/999", self.h.ctl.view()["marks"])
        self.assertIsNot(v["marks"], self.h.ctl.s["fy"]["nav"].marks)

    def test_close_during_slow_open_leaves_no_phantom_session(self):
        gate, entered, real = threading.Event(), threading.Event(), self.h.ctl.fy.session

        def slow(*a, **k):
            entered.set()
            gate.wait(5)
            return real(*a, **k)
        self.h.ctl.fy.session = slow
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.h.req("POST", "/v1/session?app=pourtoi")))
        t.start()
        self.assertTrue(entered.wait(5))
        t0 = time.monotonic()
        self.assertEqual(self.h.req("GET", "/v1/health")[1]["state"], "dashboard")   # verrou global libre
        self.assertEqual(self.h.req("POST", "/v1/close?app=pourtoi&reason=timeout")[1]["state"], "dashboard")
        self.assertLess(time.monotonic() - t0, 1.0)
        gate.set()
        t.join(5)
        self.assertEqual(res["r"][:2], (409, {"ok": False, "error": "busy"}))    # la TV a abandonné : rien créé
        time.sleep(0.2)
        self.assertEqual(self.h.ctl.health(), ("dashboard", None))
        self.assertEqual(self.h.displays, [])
        self.h.ctl.fy.session = real
        s = self.open()                                               # ouverture suivante normale
        self.assertEqual(self.key(s, "back"), "dashboard")

    def test_ui_server_serves_foryou_view_loopback_only(self):
        self.open()
        ui = B.Server(("127.0.0.1", 0), B.make_ui_handler(self.h.ctl, UI_FILM))
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % ui.server_address[1]
        try:
            with urllib.request.urlopen(base + "/ui/pourtoi/") as r:
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
                body = r.read().decode()
                self.assertIn("Pour toi", body)
                self.assertIn("window.__opentv", body)
            with urllib.request.urlopen(base + "/ui/foryou.json") as r:
                data = json.loads(r.read())
                self.assertEqual(data["rows"][0]["title"], "Le choix de ce soir")
            with urllib.request.urlopen(base + "/ui/state.json") as r:
                self.assertEqual(json.loads(r.read())["mode"], "foryou")
            with urllib.request.urlopen(base + "/ui/") as r:
                self.assertIn(b"Films", r.read())                    # vue Films inchangée
        finally:
            ui.shutdown()
            ui.server_close()


class ContentionTests(unittest.TestCase):
    """Une connexion externe tient la base (sauvegarde, CLI bloquée, verrou RESERVED/EXCLUSIVE) : ni Films ni
    « Pour toi » ni /v1/health, /v1/key, /v1/close n'attendent SQLite (borne < 1 s, délai TV = 6 s)."""
    BOUND = 1.0

    def setUp(self):
        self._q = quiet()
        self.out = self._q.__enter__()
        self.h = TB.Harness(catalogue=FY_CAT, env={"APPS": "catalogue,pourtoi"})
        st = FY.Store(self.h.cfg.foryou_db)                          # base existante avec un avis
        st.apply("films/3", "like", origin="cli", title="Superbad", genre="Comédie")
        st.close()

    def tearDown(self):
        self._q.__exit__(None, None, None)
        self.h.close()

    def timed(self, method, path):
        t0 = time.monotonic()
        code, body, _ = self.h.req(method, path)
        dt = time.monotonic() - t0
        self.assertLess(dt, self.BOUND, (method, path, round(dt, 3)))
        return code, body

    def hold(self, mode):
        c = sqlite3.connect(self.h.cfg.foryou_db, isolation_level=None)
        c.execute("BEGIN " + mode)
        self.addCleanup(c.close)
        return c

    def run_mode(self, mode):
        c = self.hold(mode)
        # Films : ouverture, déplacements, OK -> lecteur, Retour, fermeture
        code, b = self.timed("POST", "/v1/session?app=catalogue")
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        s = b["session"]
        self.assertEqual(self.timed("GET", "/v1/health?session=" + s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=right" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=ok" % s)[1]["state"], "player")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=back" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/close?session=%s&reason=user" % s)[1]["state"], "dashboard")
        self.assertTrue(self.h.wait_state("dashboard"))
        time.sleep(0.2)
        # Pour toi : ouverture (profil vide pendant la panne), panneau, action non enregistrée, fermeture
        code, b = self.timed("POST", "/v1/session?app=pourtoi")
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        s = b["session"]
        t0 = time.monotonic()
        self.assertTrue(self.h.ctl.foryou_data()["cold_start"])
        self.assertLess(time.monotonic() - t0, self.BOUND)
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=ok" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=right" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=ok" % s)[1]["state"], "catalogue")
        self.assertIn("non enregistrée", self.h.ctl.view()["message"])
        self.assertEqual(self.timed("GET", "/v1/health?session=" + s)[1], {"ok": True, "api": 1, "state": "catalogue",
                                                                            "session": s})
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=left" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=ok" % s)[1]["state"], "player")   # Regarder
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=back" % s)[1]["state"], "catalogue")
        self.assertEqual(self.timed("POST", "/v1/close?app=pourtoi&reason=user")[1]["state"], "dashboard")
        self.assertTrue(self.h.wait_state("dashboard"))
        self.assertEqual(self.h.ctl.health(), ("dashboard", None))   # aucune séance fantôme
        self.assertEqual(len(self.h.displays), 2)
        self.assertTrue(all(("close",) in d.calls for d in self.h.displays if d.closed.wait(2)))
        logs = [json.loads(l) for l in self.out.getvalue().splitlines() if l.startswith("{")]
        self.assertEqual([l["event"] for l in logs].count("foryou_store_error"), 1)   # un seul journal par panne
        self.assertEqual([l["event"] for l in logs].count("api_error"), 0)
        c.execute("ROLLBACK")
        self.assertTrue(self.h.ctl.fy.drain(5))
        st = FY.Store(self.h.cfg.foryou_db)
        self.addCleanup(st.close)
        self.assertEqual(st.state("films/3")["opinion"], "like")      # aucune donnée perdue ni corrompue

    def test_reserved_lock_never_blocks_controller(self):
        self.run_mode("IMMEDIATE")

    def test_exclusive_lock_never_blocks_controller(self):
        self.run_mode("EXCLUSIVE")

    def test_corrupt_db_keeps_films_and_pourtoi_responsive(self):
        self.h.ctl.foryou_reset()
        with open(self.h.cfg.foryou_db, "wb") as f:
            f.write(b"pas une base" * 500)
        code, b = self.timed("POST", "/v1/session?app=pourtoi")
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        self.assertTrue(self.h.ctl.foryou_data()["cold_start"])
        self.assertEqual(self.timed("POST", "/v1/close?session=%s&reason=user" % b["session"])[1]["state"], "dashboard")
        self.assertTrue(self.h.wait_state("dashboard"))
        time.sleep(0.2)
        code, b = self.timed("POST", "/v1/session?app=catalogue")
        self.assertEqual(code, 200, b)
        self.assertTrue(self.h.wait_state("catalogue"))
        self.assertEqual(self.timed("POST", "/v1/key?session=%s&k=ok" % b["session"])[1]["state"], "player")


class InstallTests(unittest.TestCase):
    def test_installer_ships_foryou_module_and_view(self):
        r, calls, home = TB.InstallScriptTests.run_install(self)
        self.assertEqual(r.returncode, 0, r.stderr)
        for f in ("opentv_foryou.py", "opentv_foryou_ui.html"):
            p = os.path.join(home, "opentv-bridge", f)
            self.assertTrue(os.path.exists(p), f)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o644)
        self.assertFalse(os.path.exists(os.path.join(home, ".local/state/opentv-bridge/foryou.sqlite3")))
        with open(os.path.join(home, ".config/systemd/user/opentv-bridge.env")) as f:
            conf = f.read()
        self.assertIn("\nFORYOU_SOURCE_ID=films\n", conf)          # espace de noms explicite, sans domaine
        self.assertIn("CATALOGUE_REFRESH_S=0\n", conf)
        self.assertFalse([c for c in calls if "apt" in c or "pip" in c])


class StaticUiTests(unittest.TestCase):
    def test_foryou_view_is_light_es5_and_french(self):
        with open(UI_FY, encoding="utf-8") as f:
            src = f.read()
        script = src.split("<script>", 1)[1].split("</script>", 1)[0]
        for bad in ("=>", "`", "let ", "const ", "class ", "async ", "await "):
            self.assertNotIn(bad, script, bad)
        css = src.split("<style>", 1)[1].split("</style>", 1)[0]
        for bad in ("grid", "gap:", "inset", "backdrop-filter", "blur(", "box-shadow:0 18px"):
            self.assertNotIn(bad, css, bad)
        for label in ("Regarder", "J'aime", "Pas pour moi", "Déjà vu", "À voir plus tard", "Pas ce soir"):
            self.assertIn(label, src)
        self.assertNotIn("innerHTML", script)

    def test_xhr_has_timeout_and_single_null_callback(self):
        with open(UI_FY, encoding="utf-8") as f:
            script = f.read().split("<script>", 1)[1].split("</script>", 1)[0]
        get = script.split("function get(", 1)[1].split("\n  function ", 1)[0]
        self.assertRegex(get, r"x\.timeout\s*=\s*3000;")
        self.assertRegex(get, r"x\.ontimeout\s*=\s*function\s*\(\)\s*\{[^}]*done = true;[^}]*cb\(null\)")
        self.assertIn("if (done) return;", get.split("x.ontimeout", 1)[1])   # garde de réponse unique


@unittest.skipUnless(TB.find_chromium(), "Chromium introuvable")
class RealChromiumForYouTests(unittest.TestCase):
    def test_kiosk_renders_rows_panel_and_marks(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            ui_port = s.getsockname()[1]
        rt = tempfile.mkdtemp()
        kiosks = []

        def factory(cfg):
            k = B.ChromiumKiosk(cfg)
            kiosks.append(k)
            return k
        h = TB.Harness(catalogue=FY_CAT, factory=factory,
                       env={"APPS": "catalogue,pourtoi", "UI_PORT": str(ui_port), "COMPOSITOR_CMD": "",
                            "BROWSER_CMD": TB.shlex_quote(TB.find_chromium()),
                            "BROWSER_FLAGS": "--headless=new --no-first-run --no-default-browser-check --disable-gpu "
                                             "--use-mock-keychain --password-store=basic --window-size=1920,1080",
                            "XDG_RUNTIME_DIR": rt})
        ui = B.Server(("127.0.0.1", ui_port), B.make_ui_handler(h.ctl, UI_FILM))
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                sid = h.ctl.open("pourtoi")["id"]
                self.assertTrue(h.wait_state("catalogue", timeout=30), out.getvalue())
                k = kiosks[0]

                def ev(expr, want, timeout=6):
                    end = time.time() + timeout
                    v = None
                    while time.time() < end:
                        v = k.evaluate(expr)
                        if v == want:
                            return v
                        time.sleep(0.05)
                    return v
                self.assertEqual(ev("document.body.getAttribute('data-ready')", "1"), "1")
                nrows = len(h.ctl.foryou_data()["rows"])
                self.assertEqual(ev("document.querySelectorAll('.frow').length", nrows), nrows)
                self.assertEqual(ev("document.querySelector('.frow .rtitle').textContent", "Le choix de ce soir"),
                                 "Le choix de ce soir")
                first = h.ctl.foryou_data()["rows"][1]["items"][1]
                h.ctl.key(sid, "down"); h.ctl.key(sid, "right")
                self.assertEqual(ev("document.body.getAttribute('data-pos')", "1:1"), "1:1")
                self.assertEqual(ev("document.getElementById('title').textContent", first["title"]), first["title"])
                h.ctl.key(sid, "ok"); h.ctl.key(sid, "right")
                self.assertEqual(ev("document.querySelector('#acts .act.on').textContent", "J'aime"), "J'aime")
                self.assertEqual(ev("document.body.getAttribute('data-panel')", "1"), "1")
                h.ctl.key(sid, "ok")
                self.assertEqual(ev("document.querySelector('.fcard.on .marks').textContent.indexOf('♥') >= 0", True), True)
                self.assertTrue(ev("document.getElementById('msg').style.display", "block") == "block")
                imgs = k.evaluate("document.querySelectorAll('.fcard img[src]').length")
                cards = k.evaluate("document.querySelectorAll('.fcard').length")
                self.assertLessEqual(imgs, cards)
                self.assertEqual(k.evaluate("window.__errors ? window.__errors.length : 0"), 0)
                h.ctl.close(sid=sid, reason="user")
                self.assertTrue(k.closed.wait(5))
        finally:
            for k in kiosks:
                k.close()
            ui.shutdown()
            ui.server_close()
            h.close()
            shutil.rmtree(rt, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
