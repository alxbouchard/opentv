"""Notes TMDb automatiques après le rafraîchissement quotidien (ratings.py, opentv-catalogue-ratings.service) :
nouveaux éléments notés, anciens conservés, 0 vote = indisponible (jamais 0), nombre/identifiants/champs non TMDb
identiques, écriture atomique sans réécriture inutile, TMDb en panne/auth refusée => catalogue du rafraîchissement
intact et échec rapporté, jeton hors Git (root:opentv 0640, jamais journalisé ni dans l'environnement), suspension,
verrou commun, garde de perte de notes, unités systemd, installation/désinstallation, CLI, santé."""
import contextlib
import fcntl
import http.client
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import types
import unittest
import urllib.parse

from unittest import mock

from tests._common import NANOPI, PI4
from tests import test_catalogue_refresh as tcr
from opentv_pi import catalogue as cat
from opentv_pi import cli, config, health, ratings

BRIDGE = tcr.ENGINE
TMDB = ratings.load_engine(NANOPI)
REAL_TMDB_DIR = config.TMDB_DIR
REAL_SERVICE_GID = ratings.service_gid
TOKEN = "eyJ" + "Q" * 24 + "." + "R" * 24 + "." + "S" * 24          # faux jeton v4 (format seulement)
V3KEY = "0123456789abcdef0123456789abcdef"                         # fausse clé v3
JSONH = {"content-type": "application/json;charset=utf-8"}
LEAKS = (TOKEN, V3KEY, "Bearer", "Film ", "themoviedb", tcr.HOST, tcr.PREFIX, "https://", "/b/")


def movie(n, votes=None):
    return {"id": 1000 + n, "title": "Film %d" % n, "original_title": "Film %d" % n, "release_date": "2024-03-01",
            "vote_average": round(6.0 + n / 10.0, 1), "vote_count": 100 * n if votes is None else votes}


class FakeTmdb:
    """API TMDb v3 minimale : /find/{imdb}, /search/movie, /movie/{id}. `mode` : ok | down | auth | flaky."""

    def __init__(self, zero_votes=(), missing=(), flaky=()):
        self.zero, self.missing, self.flaky = set(zero_votes), set(missing), set(flaky)
        self.mode, self.calls, self.headers, self.on_call = "ok", [], [], None

    def movie(self, n):
        return movie(n, votes=0 if n in self.zero else None)

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        self.headers.append(dict(headers))
        if self.on_call:
            self.on_call()
        if self.mode == "down":
            raise TMDB.TmdbError("URLError")
        if self.mode == "auth":
            return 401, JSONH, b'{"status_code":7}'
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        m = re.match(r"^/3/find/tt0*(\d+)$", u.path)
        if m:
            n = int(m.group(1))
            if n in self.flaky:
                return 503, JSONH, b"{}"
            res = [] if n in self.missing else [self.movie(n)]
            return 200, JSONH, json.dumps({"movie_results": res}).encode()
        if u.path == "/3/search/movie":
            mm = re.match(r"^Film (\d+)$", q.get("query", ""))
            n = int(mm.group(1)) if mm else None
            if n in self.flaky:
                return 503, JSONH, b"{}"
            res = [self.movie(n)] if n is not None and n not in self.missing else []
            return 200, JSONH, json.dumps({"results": res}).encode()
        m = re.match(r"^/3/movie/(\d+)$", u.path)
        if m:
            n = int(m.group(1)) - 1000
            if n in self.missing:
                return 404, JSONH, b"{}"
            return 200, JSONH, json.dumps(self.movie(n)).encode()
        return 404, JSONH, b"{}"


def plain_item(n):
    """Élément du catalogue accepté sans aucun champ TMDb (IMDb gardé : identité distincte)."""
    it = tcr.enriched_item(str(n), str(400 + n), "Film %d" % n)
    for k in list(it):
        if k.startswith("tmdb_"):
            del it[k]
    return it


def doc_of(ns, **extra):
    items = [plain_item(n) for n in ns]
    d = {"attribution": {"imdb": "IMDb (attribution)"}, "count": len(items), "generated_at": "2026-10-20T00:00:00Z",
         "source": tcr.HOST, "items": items}
    d.update(extra)
    return d


def patch_start(case, *a, **k):
    p = mock.patch.object(*a, **k)
    got = p.start()
    case.addCleanup(p.stop)
    return got


def make_tmdb_dir(case, parent):
    """Dossier du jeton de test : <parent réel>/tmdb, 0750, groupe = groupe effectif (membre : chown permis)."""
    d = os.path.join(os.path.realpath(parent), "tmdb")
    os.mkdir(d)
    os.chown(d, -1, os.getegid())
    os.chmod(d, 0o750)
    return d


class Base(tcr.Base):
    def setUp(self):
        super().setUp()
        self.gid = os.getegid()
        os.chown(self.etc, -1, self.gid)                  # macOS : un dossier créé hérite du groupe de son parent
        self.tdir = make_tmdb_dir(self, self.etc)
        self.tok = os.path.join(self.tdir, "token")
        patch_start(self, config, "TMDB_DIR", self.tdir)
        patch_start(self, ratings, "TRUSTED_UIDS", (0, os.geteuid()))
        self.gid_patch = patch_start(self, ratings, "service_gid", side_effect=lambda cfg: self.gid)
        self.write_token("TMDB_READ_TOKEN=%s\n" % TOKEN)
        self.cfg = config.Config({"OPENTV_STATE_DIR": self.state, "OPENTV_FORYOU_ENGINE_DIR": NANOPI,
                                  "OPENTV_SOURCE_FILE": self.src_env, "OPENTV_CATALOGUE_MIN_ITEMS": "3"})
        self.catf = self.cfg.catalogue_file
        self.api = FakeTmdb()

    def write_token(self, text, mode=0o640):
        with open(self.tok, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode())
        os.chown(self.tok, -1, self.gid)
        os.chmod(self.tok, mode)

    def rate(self, api=None, **kw):
        res = ratings.run(self.cfg, fetch=api or self.api, engine=TMDB, bridge=BRIDGE, sleep=lambda s: None,
                          log=lambda ev, **f: self.logs.append(dict(f, ev=ev)), **kw)
        self.assert_no_secret(res)
        return res

    def assert_no_secret(self, *objs):
        text = json.dumps([objs, self.logs], ensure_ascii=False)
        for s in LEAKS:
            self.assertNotIn(s, text)
        self.assertNotIn(TOKEN, json.dumps(dict(os.environ)))

    def items(self):
        return json.loads(self.bytes_())["items"]

    def by_sid(self):
        return {x["site_id"]: x for x in self.items()}


# --------------------------------------------------------------------------- enrichissement réussi
class TestEnrich(Base):
    def test_new_items_are_rated_existing_kept_and_only_tmdb_fields_change(self):
        self.seed(doc_of(range(1, 6)))
        first = self.rate()
        self.assertEqual((first["ok"], first["result"], first["rated"], first["pending"]), (True, "updated", 5, 0))
        rated_once = self.by_sid()
        # rafraîchissement : deux nouveaux éléments sans aucune note, anciens conservés tels quels (merge du rafraîchi)
        d = json.loads(self.bytes_())
        d["items"] += [plain_item(6), plain_item(7)]
        d["count"] = 7
        before = self.seed(d)
        prev_snapshot = b"snapshot-of-refresh"
        with open(self.catf + ".prev", "wb") as f:
            f.write(prev_snapshot)
        self.api.calls.clear()
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["wrote"], res["complete"]), (True, "updated", True, True))
        self.assertEqual((res["items"], res["rated"], res["pending"], res["new"], res["reused"]), (7, 7, 0, 2, 5))
        self.assertEqual(len(self.api.calls), 2)                            # seulement les nouveaux
        new = json.loads(self.bytes_())
        old = json.loads(before)
        self.assertEqual(TMDB._strip(new), TMDB._strip(old))                # rien d'autre que tmdb_* n'a changé
        self.assertEqual(new["count"], 7)
        self.assertEqual([x["site_id"] for x in new["items"]], [x["site_id"] for x in old["items"]])
        by = self.by_sid()
        for sid in map(str, range(1, 6)):
            self.assertEqual(by[sid], rated_once[sid])                      # anciens : octet pour octet
        for n in (6, 7):
            x = by[str(n)]
            self.assertEqual((x["tmdb_id"], x["tmdb_rating"], x["tmdb_votes"], x["tmdb_match_status"]),
                             (1000 + n, round(6.0 + n / 10.0, 1), 100 * n, "matched"))
            self.assertEqual((x["imdb_rating"], x["imdb_votes"]), (7.1, 1200))   # IMDb : identité distincte
        self.assertEqual(new["attribution"]["imdb"], "IMDb (attribution)")
        self.assertIn("TMDB", new["attribution"]["tmdb"])
        # écriture : 0600, .prev intact, aucun candidat, servi par le moteur du catalogue sans rien écarter
        self.assertEqual(stat.S_IMODE(os.stat(self.catf).st_mode), 0o600)
        self.assertEqual(self.bytes_(self.catf + ".prev"), prev_snapshot)
        self.assertEqual(self.leftovers(), [])
        served, _, host = BRIDGE.load_catalogue_meta(self.catf, BRIDGE.Config({}).detail_re)
        self.assertEqual((len(served), host), (7, tcr.HOST))
        self.assertEqual({s["site_id"]: s["tmdb_rating"] for s in served}["7"], 6.7)
        for p in (self.catf + ratings.STATE_SUFFIX, os.path.join(self.state, ratings.STATUS_NAME)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        st = ratings.status(self.cfg)
        self.assertEqual((st["credential"], st["coverage"]["rated"], st["coverage"]["pending"], st["last"]["result"]),
                         ("present", 7, 0, "updated"))
        self.assertEqual(st["last"]["last_success_at"], res["finished_at"])
        self.assert_no_secret(st)

    def test_zero_votes_stay_unavailable_never_zero_and_unmatched_stays_unrated(self):
        self.api = FakeTmdb(zero_votes={2}, missing={3})
        doc = doc_of(range(1, 5))
        doc["items"][2].pop("imdb_id")                                        # 3 : ni /find ni titre trouvé
        self.seed(doc)
        res = self.rate()
        self.assertEqual((res["ok"], res["rated"], res["matched"], res["unmatched"]), (True, 2, 3, 1))
        by = self.by_sid()
        self.assertEqual((by["2"]["tmdb_match_status"], by["2"]["tmdb_rating"], by["2"]["tmdb_votes"]),
                         ("matched", None, 0))
        self.assertEqual(by["3"]["tmdb_match_status"], "unmatched")
        self.assertIsNone(by["3"].get("tmdb_rating"))
        self.assertNotIn("tmdb_id", by["3"])
        served = {s["site_id"]: s for s in BRIDGE.load_catalogue_meta(self.catf, BRIDGE.Config({}).detail_re)[0]}
        self.assertIsNone(served["2"]["tmdb_rating"])                          # « indisponible » à l'accueil

    def test_second_run_writes_nothing_and_makes_no_request(self):
        self.seed(doc_of(range(1, 6)))
        self.assertEqual(self.rate()["result"], "updated")
        st0, data = os.stat(self.catf), self.bytes_()
        self.api.calls.clear()
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["wrote"], res["requests"]), (True, "unchanged", False, 0))
        st1 = os.stat(self.catf)
        self.assertEqual((st0.st_ino, st0.st_mtime_ns), (st1.st_ino, st1.st_mtime_ns))
        self.assertEqual(self.bytes_(), data)
        self.assertEqual(self.api.calls, [])

    def test_stale_rating_is_refreshed_by_tmdb_id_keeping_identity(self):
        self.seed(doc_of(range(1, 4)))
        t = [1_800_000_000.0]
        self.assertEqual(self.rate(clock=lambda: t[0])["result"], "updated")
        t[0] += 8 * 86400                                                     # > 7 jours
        self.api.zero = set()
        orig = self.api.movie
        self.api.movie = lambda n: dict(orig(n), vote_average=9.1, vote_count=4321)
        self.api.calls.clear()
        res = self.rate(clock=lambda: t[0])
        self.assertEqual((res["result"], res["rating_refresh"]), ("updated", 3))
        self.assertTrue(all(urllib.parse.urlsplit(u).path.startswith("/3/movie/") for u in self.api.calls))
        self.assertEqual({(x["tmdb_id"], x["tmdb_rating"]) for x in self.items()},
                         {(1001, 9.1), (1002, 9.1), (1003, 9.1)})

    def test_bearer_header_only_never_in_url_and_v3_key_supported(self):
        self.seed(doc_of(range(1, 3)))
        self.rate()
        self.assertTrue(all(h.get("Authorization") == "Bearer " + TOKEN for h in self.api.headers))
        self.assertFalse(any(TOKEN in u for u in self.api.calls))
        self.write_token("TMDB_API_KEY='%s'\n" % V3KEY)
        d = json.loads(self.bytes_())
        d["items"].append(plain_item(3))
        d["count"] = 3
        self.seed(d)
        res = self.rate()
        self.assertEqual((res["ok"], res["rated"]), (True, 3))


# --------------------------------------------------------------------------- pannes : catalogue intact, échec rapporté
class TestFailures(Base):
    def setUp(self):
        super().setUp()
        self.d0 = doc_of(range(1, 9))
        self.before = self.seed(self.d0)

    def frozen(self):
        st = os.stat(self.catf)
        return self.bytes_(), st.st_ino, st.st_mtime_ns

    def test_provider_down_leaves_catalogue_byte_identical_and_reports_failure(self):
        f0 = self.frozen()
        self.api.mode = "down"
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["circuit_open"], res["wrote"]),
                         (False, "provider_unavailable", True, False))
        self.assertEqual(len(self.api.calls), 5)                              # disjoncteur : 5 erreurs, puis arrêt
        self.assertEqual(self.frozen(), f0)
        self.assertEqual(self.leftovers(), [])
        st = ratings.status(self.cfg)
        self.assertEqual((st["last"]["ok"], st["last"]["result"], st["coverage"]["pending"]),
                         (False, "provider_unavailable", 8))
        self.assertNotIn("last_success_at", {k: v for k, v in st["last"].items() if v is not None})
        hv = ratings.health_view(self.cfg)
        self.assertEqual((hv["configured"], hv["ok"], hv["pending"]), (True, False, 8))

    def test_auth_refused_writes_nothing(self):
        f0 = self.frozen()
        self.api.mode = "auth"
        res = self.rate()
        self.assertEqual((res["ok"], res["result"]), (False, "auth_failed"))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(self.frozen(), f0)
        self.assertFalse(os.path.exists(self.catf + ratings.STATE_SUFFIX))

    def test_partial_errors_commit_good_items_and_report_not_ok(self):
        self.api = FakeTmdb(flaky={4})
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["errors"], res["wrote"], res["complete"]),
                         (False, "partial", 1, True, False))
        by = self.by_sid()
        self.assertNotIn("tmdb_match_status", by["4"])                        # rien inventé pour l'élément en erreur
        self.assertEqual(sum(1 for x in by.values() if x.get("tmdb_rating") is not None), 7)
        self.api.flaky = set()
        res = self.rate()                                                     # prochain passage : complété
        self.assertEqual((res["ok"], res["result"], res["rated"], res["pending"]), (True, "updated", 8, 0))

    def test_global_deadline_bounds_every_request_and_the_whole_run(self):
        now = [98.0]
        resolve = lambda h, to: ["93.184.216.34"]                              # noqa: E731
        fetch = ratings.guarded_fetch(TMDB, deadline=100.0, mono=lambda: now[0], resolve=resolve, local=lambda: set())
        got = []
        with mock.patch.object(TMDB, "http_get", side_effect=lambda u, h, t: got.append(t) or (200, JSONH, b"{}")):
            fetch("https://api.themoviedb.org/3/find/tt1", {}, 6)
        self.assertEqual(got, [2.0])                                          # délai tronqué au temps restant
        now[0] = 100.5
        with mock.patch.object(TMDB, "http_get", side_effect=AssertionError("network")):
            with self.assertRaises(TMDB.TmdbError) as cm:
                fetch("https://api.themoviedb.org/3/find/tt1", {}, 6)
        self.assertEqual(str(cm.exception), "deadline")
        # passage complet : échéance dépassée => disjoncteur après 5 refus, rien écrit, aucune requête réseau
        f0 = self.frozen()
        t = [0.0]

        def mono():
            t[0] += ratings.DEADLINE_S                                         # chaque lecture : une échéance entière
            return t[0]
        with mock.patch.object(TMDB, "http_get", side_effect=AssertionError("network")):
            res = ratings.run(self.cfg, engine=TMDB, bridge=BRIDGE, sleep=lambda s: None, mono=mono)
        self.assertEqual((res["ok"], res["result"], res["requests"], res["errors"]),
                         (False, "provider_unavailable", 5, 5))
        self.assertEqual(self.frozen(), f0)

    def test_guard_refuses_other_hosts_and_non_public_addresses(self):
        mono = lambda: 0.0                                                      # noqa: E731
        f = ratings.guarded_fetch(TMDB, 100.0, mono=mono, resolve=lambda h, t: ["127.0.0.1"], local=lambda: set())
        with mock.patch.object(TMDB, "http_get", side_effect=AssertionError("network")):
            for url, why in (("https://api.themoviedb.org/3/x", "non_public_address"),
                             ("https://evil.example/3/x", "host_refused")):
                with self.assertRaises(TMDB.TmdbError) as cm:
                    f(url, {}, 6)
                self.assertEqual(str(cm.exception), why)
            f = ratings.guarded_fetch(TMDB, 100.0, mono=mono, resolve=lambda h, t: [], local=lambda: set())
            with self.assertRaises(TMDB.TmdbError):
                f("https://api.themoviedb.org/3/x", {}, 6)
        got = []
        f = ratings.guarded_fetch(TMDB, 100.0, mono=mono, resolve=lambda h, t: ["93.184.216.34"], local=lambda: set())
        with mock.patch.object(TMDB, "http_get", side_effect=lambda u, h, t: got.append(t) or (200, JSONH, b"{}")):
            f("https://api.themoviedb.org/3/x", {}, 6)
        self.assertEqual(got, [6])

    def test_catalogue_replaced_during_run_wins_and_nothing_is_written(self):
        newer = json.dumps(doc_of(range(1, 10)), sort_keys=True).encode()

        def swap():
            if len(self.api.calls) == 3:
                tmp = self.catf + ".manual"
                with open(tmp, "wb") as f:
                    f.write(newer)
                os.replace(tmp, self.catf)
        self.api.on_call = swap
        res = self.rate()
        self.assertEqual((res["ok"], res["result"]), (False, "catalogue_changed"))
        self.assertEqual(self.bytes_(), newer)
        self.assertEqual(self.leftovers(), [])

    def test_replace_failure_keeps_catalogue_and_removes_candidate(self):
        f0 = self.frozen()
        real = os.replace

        def boom(src, dst, **kw):
            if kw.get("src_dir_fd") is not None:
                raise OSError(5, "EIO /var/lib/opentv/secret")
            return real(src, dst, **kw)
        with mock.patch.object(ratings.os, "replace", side_effect=boom):
            res = self.rate()
        self.assertEqual((res["result"], res["why"]), ("write_failed", "replace"))
        self.assertEqual(self.frozen(), f0)
        self.assertEqual(self.leftovers(), [])

    def test_massive_rating_loss_is_refused(self):
        self.assertTrue(self.rate()["ok"])
        f0 = self.frozen()
        self.api.missing = set(range(1, 9))                                   # toutes les fiches TMDb « disparues »
        t = 1_900_000_000.0
        res = self.rate(clock=lambda: t)
        self.assertEqual((res["ok"], res["result"]), (False, "rating_drop"))
        self.assertEqual(self.frozen(), f0)
        # une perte isolée (fiche réellement retirée) reste acceptée
        self.api.missing = {1}
        res = self.rate(clock=lambda: t)
        self.assertEqual((res["ok"], res["lost_ratings"], res["rated"]), (True, 1, 7))

    def test_engine_changing_non_tmdb_fields_or_ids_is_refused(self):
        f0 = self.frozen()
        for mutate, why in ((lambda c: c["items"][0].update(title="Autre"), "non_tmdb_change"),
                            (lambda c: c["items"].pop(), "non_tmdb_change"),
                            (lambda c: c["items"][1].update(tmdb_rating=42), "tmdb_fields")):
            def enrich(c, state, client, *a, _m=mutate, **k):
                n = TMDB.enrich(c, state, client, *a, **k)
                _m(c)
                return n
            eng = types.SimpleNamespace(**{k: getattr(TMDB, k) for k in dir(TMDB) if not k.startswith("__")})
            eng.enrich = enrich
            res = ratings.run(self.cfg, fetch=self.api, engine=eng, bridge=BRIDGE, sleep=lambda s: None)
            self.assertEqual((res["ok"], res["result"], res.get("why")), (False, "invalid_candidate", why))
            self.assertEqual(self.frozen(), f0)

    def test_invalid_or_unsafe_current_catalogue_is_refused_without_network(self):
        for doc in ({"items": "x"}, {"items": []}, dict(self.d0, count=99)):
            self.seed(doc)
            res = self.rate()
            self.assertEqual((res["ok"], res["result"]), (False, "catalogue_invalid"))
        os.unlink(self.catf)
        os.symlink(os.path.join(self.d, "elsewhere"), self.catf)
        self.assertEqual(self.rate()["result"], "current_unsafe")
        os.unlink(self.catf)
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["why"]), (True, "skipped", "no_catalogue"))
        self.assertEqual(self.api.calls, [])


    def test_unexpected_transport_exception_is_an_item_error_never_a_trace(self):
        """Régression : http.client.IncompleteRead (ni OSError ni URLError) échappait au moteur."""
        f0 = self.frozen()
        fetch = ratings.guarded_fetch(TMDB, 1e12, resolve=lambda h, t: ["93.184.216.34"], local=lambda: set())
        boom = http.client.IncompleteRead(b"partial secret body " + TOKEN.encode(), 99)
        with mock.patch.object(TMDB, "http_get", side_effect=boom):
            res = self.rate(api=fetch)
        self.assertEqual((res["ok"], res["result"], res["errors"], res["wrote"], res["circuit_open"]),
                         (False, "provider_unavailable", 5, False, True))
        self.assertEqual(self.frozen(), f0)                                   # atomique : rien écrit
        self.assertEqual(self.leftovers(), [])
        text = json.dumps([res, self.logs, ratings.status(self.cfg)])
        for leak in ("partial secret", "IncompleteRead", "Traceback", "api.themoviedb.org"):
            self.assertNotIn(leak, text)
        whys = {l.get("why") for l in self.logs if l["ev"] == "ratings_tmdb_item_error"}
        self.assertEqual(whys, {"transport_incompleteread"})
        # le décorateur ne chaîne jamais l'exception d'origine (aucun message/URL dans une trace éventuelle)
        with mock.patch.object(TMDB, "http_get", side_effect=boom):
            with self.assertRaises(TMDB.TmdbError) as cm:
                fetch("https://api.themoviedb.org/3/find/tt1?api_key=" + V3KEY, {}, 6)
        self.assertIsNone(cm.exception.__cause__)
        self.assertIsNone(cm.exception.__context__)
        self.assertEqual(str(cm.exception), "transport_incompleteread")
        # un fetch injecté qui lève autre chose est traduit aussi (jamais internal_error ni trace)
        def raw(url, headers, timeout):
            raise ConnectionResetError("reset by https://api.themoviedb.org/3/x?api_key=" + V3KEY)
        res = self.rate(api=raw)
        self.assertEqual((res["result"], res["errors"]), ("provider_unavailable", 5))
        self.assertEqual(self.frozen(), f0)
        # KeyboardInterrupt/SystemExit ne sont jamais avalés
        with self.assertRaises(KeyboardInterrupt):
            ratings._transport_safe(TMDB)(mock.Mock(side_effect=KeyboardInterrupt()))("u", {}, 1)

    def test_pre_existing_invalid_tmdb_fields_kept_after_item_error_do_not_reject_the_run(self):
        """Régression : un élément aux champs TMDb invalides HÉRITÉS, laissé tel quel parce que le fournisseur a
        échoué pour lui, ne doit pas faire refuser tout le passage ; ce passage n'introduit toujours rien d'invalide."""
        d = doc_of(range(1, 9))
        legacy = {"tmdb_id": 1004, "tmdb_rating": 7.4, "tmdb_votes": 800, "tmdb_match_status": "matched",
                  "tmdb_confidence": 0.98, "tmdb_match_method": "find_imdb", "tmdb_match_key": "k4",
                  "tmdb_evidence": ["imdb_id"], "tmdb_reason": None}              # ancien format : invalide
        d["items"][3].update(legacy)
        self.assertFalse(TMDB.valid_tmdb_fields(d["items"][3]))
        self.seed(d)
        self.api = FakeTmdb(flaky={4})
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["wrote"], res["errors"], res["invalid_kept"]),
                         (False, "partial", True, 1, 1))
        by = self.by_sid()
        self.assertEqual({k: v for k, v in by["4"].items() if k.startswith("tmdb_")}, legacy)   # intact, rien inventé
        self.assertEqual(sum(1 for x in by.values() if TMDB.valid_tmdb_fields(x)), 7)
        # fournisseur rétabli : l'élément est recorrigé par le moteur (valide)
        self.api.flaky = set()
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["invalid_kept"]), (True, "updated", 0))
        self.assertTrue(TMDB.valid_tmdb_fields(self.by_sid()["4"]))

    def test_invalid_value_introduced_by_this_run_is_still_refused(self):
        d = doc_of(range(1, 9))
        d["items"][3].update(tmdb_match_status="matched", tmdb_confidence=0.98, tmdb_match_key="k4", tmdb_id=1004)
        self.seed(d)
        f0 = self.frozen()
        for mutate in (lambda c: c["items"][3].update(tmdb_confidence=0.5),        # invalide modifié autrement
                       lambda c: c["items"][0].update(tmdb_rating=42),              # valide rendu invalide
                       lambda c: c["items"][5].update(tmdb_votes=-1)):
            def enrich(c, state, client, *a, _m=mutate, **k):
                n = TMDB.enrich(c, state, client, *a, **k)
                _m(c)
                return n
            eng = types.SimpleNamespace(**{k: getattr(TMDB, k) for k in dir(TMDB) if not k.startswith("__")})
            eng.enrich = enrich
            self.api = FakeTmdb(flaky={4})
            res = ratings.run(self.cfg, fetch=self.api, engine=eng, bridge=BRIDGE, sleep=lambda s: None)
            self.assertEqual((res["ok"], res["result"], res.get("why")), (False, "invalid_candidate", "tmdb_fields"))
            self.assertEqual(self.frozen(), f0)


# --------------------------------------------------------------------------- jeton
class TestCredential(Base):
    def setUp(self):
        super().setUp()
        self.f0 = self.seed(doc_of(range(1, 4)))

    def test_missing_token_skips_without_any_request(self):
        os.unlink(self.tok)
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["why"]), (True, "skipped", "credential_missing"))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bytes_(), self.f0)
        self.assertEqual(ratings.status(self.cfg)["credential"], "missing")
        self.assertIsNone(ratings.health_view(self.cfg)["ok"])                 # non configuré : pas une panne
        self.assertTrue(os.path.isdir(self.tdir))                              # le dossier (masqué) reste

    def test_unsafe_or_invalid_token_fails_closed_without_request(self):
        cases = [(0o644, "TMDB_READ_TOKEN=%s\n" % TOKEN, "credential_unsafe", "mode"),     # lisible par tous
                 (0o660, "TMDB_READ_TOKEN=%s\n" % TOKEN, "credential_unsafe", "mode"),     # inscriptible groupe
                 (0o600, "TMDB_READ_TOKEN=%s\n" % TOKEN, "credential_unsafe", "mode"),     # illisible du service
                 (0o4640, "TMDB_READ_TOKEN=%s\n" % TOKEN, "credential_unsafe", "mode"),
                 (0o640, "TMDB_READ_TOKEN=not-a-token\n", "credential_invalid", None),
                 (0o640, "OPENTV_X=1\n", "credential_invalid", None),
                 (0o640, b"\xff\xfe", "credential_invalid", None),
                 (0o640, "#" * (ratings.CRED_CAP + 1), "credential_unsafe", "too_large")]
        for mode, text, code, why in cases:
            self.write_token(text, mode)
            res = self.rate()
            self.assertEqual((res["ok"], res["result"]), (False, code), (mode, text))
            if why:
                self.assertEqual(res["why"], why)
                self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", why))
        # lien symbolique, lien dur, FIFO, dossier à la place du fichier
        os.unlink(self.tok)
        real = os.path.join(self.tdir, "real")
        with open(real, "w") as f:
            f.write("TMDB_READ_TOKEN=%s\n" % TOKEN)
        os.chmod(real, 0o640)
        os.symlink(real, self.tok)
        self.assertEqual(self.rate()["result"], "credential_unsafe")
        self.assertEqual(ratings.credential_state(self.cfg), "unsafe")
        os.unlink(self.tok)
        os.link(real, self.tok)
        self.assertEqual((self.rate()["result"], ratings.credential_check(self.cfg)),
                         ("credential_unsafe", ("unsafe", "hardlink")))
        os.unlink(self.tok)
        os.unlink(real)
        os.mkfifo(self.tok, 0o640)
        self.assertEqual(self.rate()["result"], "credential_unsafe")             # O_NONBLOCK : jamais bloqué
        self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", "not_regular"))
        os.unlink(self.tok)
        os.mkdir(self.tok)
        self.assertEqual(self.rate()["result"], "credential_unsafe")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bytes_(), self.f0)

    def test_directory_must_be_a_real_private_directory(self):
        for mode, why in ((0o770, "dir_mode"), (0o755, "dir_mode"), (0o751, "dir_mode"), (0o2750, "dir_mode")):
            os.chmod(self.tdir, mode)
            res = self.rate()
            self.assertEqual((res["result"], res.get("why")), ("credential_unsafe", why), oct(mode))
            self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", why))
        os.chmod(self.tdir, 0o750)
        self.assertEqual(ratings.credential_check(self.cfg), ("present", None))
        # dossier remplacé par un lien vers un dossier « correct » : refusé (O_NOFOLLOW), jamais suivi
        moved = self.tdir + ".real"
        os.rename(self.tdir, moved)
        os.symlink(moved, self.tdir)
        res = self.rate()
        self.assertEqual((res["result"], res.get("why")), ("credential_unsafe", "dir_open"))
        self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", "dir_open"))
        os.unlink(self.tdir)
        # dossier absent : non configuré (aucune requête), jamais une panne
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["why"]), (True, "skipped", "credential_missing"))
        self.assertEqual(ratings.credential_check(self.cfg), ("missing", "dir_missing"))
        # parent inscriptible par le groupe : refusé (le dossier pourrait y être échangé)
        os.rename(moved, self.tdir)
        os.chmod(self.etc, 0o770)
        self.addCleanup(os.chmod, self.etc, 0o700)
        self.assertEqual(self.rate()["result"], "credential_unsafe")
        self.assertEqual(ratings.credential_state(self.cfg), "unsafe")
        self.assertEqual(self.api.calls, [])

    def test_owner_and_group_must_be_root_and_service_group(self):
        with mock.patch.object(ratings, "TRUSTED_UIDS", (0,)):
            self.assertEqual(self.rate()["result"], "credential_unsafe")          # propriétaire non root
            self.assertEqual(ratings.credential_state(self.cfg), "unsafe")
        # groupe du dossier/fichier différent du groupe du service : refusé (lecture ET état)
        self.gid = os.getegid() + 1
        res = self.rate()
        self.assertEqual((res["result"], res["why"]), ("credential_unsafe", "dir_group"))
        self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", "dir_group"))
        self.gid = os.getegid()
        other = [g for g in os.getgroups() if g != self.gid]
        if other:                                                             # fichier seul d'un autre groupe
            os.chown(self.tok, -1, other[0])
            self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", "group"))
            self.assertEqual(self.rate()["why"], "group")
        # groupe du service non privé : « unsafe » rapporté exactement, aucune lecture ni requête
        self.gid_patch.side_effect = ratings.Refused("service_group_not_private")
        self.assertEqual(ratings.credential_check(self.cfg), ("unsafe", "service_group_not_private"))
        res = self.rate()
        self.assertEqual((res["ok"], res["result"]), (False, "service_group_not_private"))
        st = ratings.status(self.cfg)
        self.assertEqual((st["credential"], st["credential_why"]), ("unsafe", "service_group_not_private"))
        self.assertEqual(ratings.health_view(self.cfg)["ok"], False)
        self.assertEqual(self.api.calls, [])

    def test_service_group_must_be_private(self):
        self.addCleanup(setattr, ratings, "service_gid", ratings.service_gid)
        ratings.service_gid = REAL_SERVICE_GID
        pw = types.SimpleNamespace(pw_name="opentv", pw_uid=990, pw_gid=990)
        others = [pw, types.SimpleNamespace(pw_name="alex", pw_uid=1000, pw_gid=1000)]

        def check(members=(), allp=None, gid=990, user=pw):
            with mock.patch("pwd.getpwnam", return_value=user), \
                    mock.patch("grp.getgrnam", return_value=types.SimpleNamespace(gr_gid=gid, gr_mem=list(members))), \
                    mock.patch("pwd.getpwall", return_value=allp or others):
                try:
                    return ratings.service_gid(self.cfg)
                except ratings.Refused as e:
                    return e.code
        self.assertEqual(check(), 990)
        self.assertEqual(check(members=("opentv",)), 990)
        self.assertEqual(check(members=("alex",)), "service_group_not_private")      # membre supplémentaire
        self.assertEqual(check(allp=others + [types.SimpleNamespace(pw_name="x", pw_uid=5, pw_gid=990)]),
                         "service_group_not_private")                                # autre compte, groupe principal
        self.assertEqual(check(gid=991), "service_group_not_private")              # groupe ≠ groupe principal
        self.assertEqual(check(gid=0, user=types.SimpleNamespace(pw_name="opentv", pw_uid=990, pw_gid=0)),
                         "service_group_not_private")
        with mock.patch("pwd.getpwnam", side_effect=KeyError("opentv")):
            with self.assertRaises(ratings.Refused) as cm:
                ratings.service_gid(self.cfg)
        self.assertEqual(cm.exception.code, "service_group_missing")

    def test_token_never_logged_printed_or_left_in_environment(self):
        os.environ.pop("TMDB_READ_TOKEN", None)
        env = os.path.join(self.d, "opentv.env")
        with open(env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\n" % (self.state, NANOPI))
        out, errs = io.StringIO(), io.StringIO()
        self.api.mode = "down"
        with mock.patch.object(ratings, "load_engine", return_value=TMDB), \
                mock.patch.object(ratings, "guarded_fetch", return_value=self.api), \
                mock.patch.object(cat, "_load_engine", return_value=BRIDGE), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(errs):
            code = cli.main(["--env", env, "catalogue", "ratings"])
        self.assertEqual(code, 1)
        text = out.getvalue() + errs.getvalue()
        with open(os.path.join(self.state, ratings.STATUS_NAME)) as f:
            text += f.read()
        for s in LEAKS:
            self.assertNotIn(s, text)
        self.assertNotIn("TMDB_READ_TOKEN", os.environ)
        last = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual((last["event"], last["result"], last["ok"]),
                         ("catalogue_ratings", "provider_unavailable", False))

    def test_patterns_match_the_engine(self):
        self.assertEqual(ratings.V4_RE.pattern, TMDB.V4_RE.pattern)
        self.assertEqual(ratings.V3_RE.pattern, TMDB.V3_RE.pattern)
        self.assertEqual(ratings.STATUSES, TMDB.STATUSES)

    def fake_host(self, masked=ratings.MASKED_UNITS, pids=None):
        """systemctl show MainPID + /proc/<pid>/mountinfo simulés : `masked` portent le masque du dossier."""
        proc = os.path.join(self.d, "proc")
        shutil.rmtree(proc, True)
        pids = pids if pids is not None else {u: 4000 + i for i, u in enumerate(ratings.MASKED_UNITS)}
        for u, pid in pids.items():
            if not pid:
                continue
            os.makedirs(os.path.join(proc, str(pid)))
            lines = ["22 1 179:2 / / rw,relatime shared:1 - ext4 /dev/root rw",
                     "30 22 0:5 /etc/opentv/agents /etc/opentv/agents ro - ext4 /dev/root rw"]
            if u in masked:
                mp = self.cfg.tmdb_dir.replace(" ", "\\040")
                lines.append("41 22 0:24 /systemd/inaccessible/dir %s ro,nosuid,nodev,noexec shared:5 - tmpfs "
                             "tmpfs rw" % mp)
            else:
                lines.append("41 22 0:24 / %s.other ro - tmpfs tmpfs rw" % self.cfg.tmdb_dir)
            with open(os.path.join(proc, str(pid), "mountinfo"), "w") as f:
                f.write("\n".join(lines) + "\n")
        calls = []

        def runner(argv, timeout):
            calls.append(list(argv))
            if argv[:2] == ["systemctl", "show"] and argv[-1] in pids:
                return 0, "%d\n" % (pids[argv[-1]] or 0)
            return 1, ""
        return runner, proc, calls

    def deposit(self, value, runner, proc, **kw):
        with mock.patch.object(os, "fchown", side_effect=lambda fd, u, g: self.chown.append((u, g))):
            return ratings.write_credential(self.cfg, value, self.gid, runner, trusted_dir_uids=(0, os.geteuid()),
                                            proc=proc, **kw)

    def test_root_deposit_creates_private_dir_writes_0640_and_never_echoes(self):
        self.chown = []
        shutil.rmtree(self.tdir)
        runner, proc, calls = self.fake_host()
        kind = self.deposit(TOKEN + "\n", runner, proc)
        self.assertEqual(kind, "bearer")
        self.assertEqual(self.chown, [(0, self.gid), (0, self.gid)])        # dossier puis fichier, par descripteur
        self.assertEqual(stat.S_IMODE(os.stat(self.tdir).st_mode), 0o750)
        st = os.stat(self.tok)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_nlink), (0o640, 1))
        with open(self.tok) as f:
            self.assertEqual(ratings.parse_credential(f.read()), {"TMDB_READ_TOKEN": TOKEN})
        self.assertEqual([c[-1] for c in calls], list(ratings.MASKED_UNITS))   # chaque unité du compte vérifiée
        self.assertEqual(ratings.credential_check(self.cfg), ("present", None))
        # remplacement : même dossier (jamais recréé : le masque des unités démarrées y reste attaché)
        ino = os.stat(self.tdir).st_ino
        self.chown = []
        self.assertEqual(self.deposit(V3KEY, runner, proc), "api_key")
        self.assertEqual(os.stat(self.tdir).st_ino, ino)
        self.assertEqual(self.chown, [(0, self.gid)])                          # dossier existant : jamais modifié
        with open(self.tok) as f:
            self.assertEqual(ratings.parse_credential(f.read()), {"TMDB_API_KEY": V3KEY})
        for bad in ("", "nope", TOKEN + "x y", "Bearer " + TOKEN):
            with self.assertRaises(ValueError):
                self.deposit(bad, runner, proc)

    def test_deposit_refused_when_a_running_unit_lacks_the_directory_mask(self):
        self.chown = []
        os.unlink(self.tok)
        # opentv.service démarré avant la création du dossier (ou dossier recréé depuis) : masque absent
        runner, proc, _ = self.fake_host(masked=("opentv-cec.service",))
        with self.assertRaises(ratings.Exposed) as cm:
            self.deposit(TOKEN, runner, proc)
        self.assertEqual(cm.exception.units, ["opentv.service", "opentv-cec-announce.service",
                                              "opentv-catalogue-refresh.service"])
        self.assertFalse(os.path.lexists(self.tok))                           # rien écrit
        self.assertEqual([n for n in os.listdir(self.tdir)], [])
        # unités arrêtées (MainPID 0) : démarreront avec le masque, le dossier existant déjà
        runner, proc, _ = self.fake_host(masked=(), pids={u: 0 for u in ratings.MASKED_UNITS})
        self.assertEqual(self.deposit(TOKEN, runner, proc), "bearer")
        # état invérifiable (systemctl en échec, mountinfo illisible) : refus, jeton existant intact
        before = self.bytes_(self.tok)
        with self.assertRaises(ratings.Refused) as cm:
            self.deposit(V3KEY, lambda a, t: (1, ""), proc)
        self.assertEqual(cm.exception.code, "exposure_unverified")
        runner, proc, _ = self.fake_host()
        os.chmod(os.path.join(proc, "4000", "mountinfo"), 0)
        self.addCleanup(os.chmod, os.path.join(proc, "4000", "mountinfo"), 0o600)
        if os.geteuid() != 0:
            with self.assertRaises(ratings.Refused):
                self.deposit(V3KEY, runner, proc)
        self.assertEqual(self.bytes_(self.tok), before)
        # dossier dangereux : jamais corrigé ni suivi
        os.chmod(self.tdir, 0o770)
        runner, proc, _ = self.fake_host()
        with self.assertRaises(ratings.Refused) as cm:
            self.deposit(V3KEY, runner, proc)
        self.assertEqual((cm.exception.code, cm.exception.why), ("credential_unsafe", "dir_mode"))
        self.assertEqual(stat.S_IMODE(os.stat(self.tdir).st_mode), 0o770)
        self.assertEqual(self.bytes_(self.tok), before)

    def test_cli_deposit_checks_group_and_exposure_and_never_echoes(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="otvetc", dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        env = os.path.join(d, "none.env")

        def call(stdin, **patches):
            out = io.StringIO()
            with mock.patch.object(cli.os, "geteuid", return_value=0), \
                    mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
                    contextlib.ExitStack() as st:
                for k, v in patches.items():
                    st.enter_context(mock.patch.object(ratings, k, **v))
                code = cli.main(["--env", env, "catalogue", "ratings-credential"])
            self.assertNotIn(V3KEY, out.getvalue())
            self.assertNotIn(TOKEN, out.getvalue())
            return code, json.loads(out.getvalue())
        code, rep = call(V3KEY + "\n", write_credential={"return_value": "api_key"})
        self.assertEqual((code, rep["kind"]), (0, "api_key"))
        code, rep = call(V3KEY + "\n", service_gid={"side_effect": ratings.Refused("service_group_not_private")},
                         write_credential={"side_effect": AssertionError("written")})
        self.assertEqual((code, rep["error"]), (2, "service_group_not_private"))
        code, rep = call(TOKEN + "\n", write_credential={"side_effect": ratings.Exposed(["opentv.service"])})
        self.assertEqual((code, rep["error"], rep["units"]), (1, "credential_exposed", ["opentv.service"]))
        self.assertIn("systemctl restart opentv.service", rep["note"])
        code, rep = call(TOKEN + "\n", write_credential={"side_effect": ratings.Refused("exposure_unverified")})
        self.assertEqual((code, rep["error"]), (1, "exposure_unverified"))
        code, rep = call("x" * 5000)
        self.assertEqual((code, rep["error"]), (2, "credential_bad_format"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["catalogue", "ratings-credential"]), 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "root_required")

    def test_health_as_root_warns_when_a_running_unit_is_unmasked(self):
        runner, proc, _ = self.fake_host(masked=("opentv-cec.service",))
        real = ratings.exposed_units
        with mock.patch.object(ratings, "exposed_units", side_effect=lambda c, r: real(c, r, proc=proc)), \
                mock.patch.object(ratings.os, "geteuid", return_value=0):
            hv = ratings.health_view(self.cfg, runner=runner)
        self.assertIn("opentv.service", hv["exposed"])

        def hrunner(argv, timeout):
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, "active\n"
            return runner(argv, timeout)
        with mock.patch.object(ratings, "exposed_units", side_effect=lambda c, r: real(c, r, proc=proc)), \
                mock.patch.object(ratings.os, "geteuid", return_value=0), \
                mock.patch.object(health, "verify_manifest", return_value={"ok": True}), \
                mock.patch.object(health.netstate, "snapshot", return_value={"mode": "wifi"}):
            rep = health.run(self.cfg, runner=hrunner, check_http=False)
        self.assertIn("ratings_credential_exposed", rep["warnings"])
        self.assertTrue(rep["ok"])                                            # enrichissement : jamais « ok » faux


# --------------------------------------------------------------------------- suspension, verrou, orphelins
class TestHoldLockCleanup(Base):
    def setUp(self):
        super().setUp()
        self.f0 = self.seed(doc_of(range(1, 4)))

    def test_held_catalogue_is_never_touched_nor_token_read(self):
        with open(self.catf + cat.HOLD_SUFFIX, "w") as f:
            f.write('{"held_at": 1, "reason": "rollback", "v": 1}')
        os.chmod(self.catf + cat.HOLD_SUFFIX, 0o600)
        with mock.patch.object(ratings, "read_credential", side_effect=AssertionError("token read")):
            res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["held"]), (True, "held", True))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bytes_(), self.f0)
        os.chmod(self.catf + cat.HOLD_SUFFIX, 0o644)
        os.unlink(self.catf + cat.HOLD_SUFFIX)
        os.mkdir(self.catf + cat.HOLD_SUFFIX)
        self.assertEqual(self.rate()["result"], "hold_unsafe")                # fail-closed
        self.assertEqual(self.api.calls, [])

    def test_shared_lock_busy_exit_75(self):
        fd = os.open(self.catf + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        res = self.rate()
        self.assertEqual(res["result"], "busy")
        self.assertEqual(self.api.calls, [])
        # le rafraîchissement et l'enrichissement partagent ce verrou : l'un exclut l'autre
        r2 = cat.refresh(self.cfg, fetch=tcr.Site(b""), engine=BRIDGE)
        self.assertEqual(r2["result"], "busy")

    def test_orphan_temporaries_of_both_modules_are_cleaned_under_lock(self):
        ours = [".catalogue-ratings.json.0123456789ab.tmp", ".catalogue.json.tmdb-state.json.abcdef012345.tmp",
                ".catalogue.json.fedcba987654.candidate"]
        for n in ours:
            with open(os.path.join(self.state, n), "w") as f:
                f.write("{")
        self.rate()
        left = set(os.listdir(self.state))
        for n in ours:
            self.assertNotIn(n, left)
        for n in ours:
            with open(os.path.join(self.state, n), "w") as f:
                f.write("{")
        cat.resume(self.cfg)                                                  # le rafraîchissement aussi
        self.assertFalse(set(ours) & set(os.listdir(self.state)))


# --------------------------------------------------------------------------- reprise bornée (minuterie horaire)
class TestRetry(Base):
    def setUp(self):
        super().setUp()
        self.f0 = self.seed(doc_of(range(1, 6)))
        self.t = [1_800_000_000.0]

    def clock(self):
        return self.t[0]

    def go(self, retry, **kw):
        return self.rate(clock=self.clock, retry=retry, **kw)

    def status_bytes(self):
        p = os.path.join(self.state, ratings.STATUS_NAME)
        return self.bytes_(p) if os.path.exists(p) else None

    def test_transient_failure_is_retried_with_backoff_then_stops_on_success(self):
        self.assertEqual(self.go(True)["result"], "retry_not_needed")         # jamais passé : rien
        self.assertIsNone(self.status_bytes())
        self.api.mode = "down"
        res = self.go(False)
        self.assertEqual((res["result"], res["retry_attempts"]), ("provider_unavailable", 0))
        st0 = self.status_bytes()
        self.api.calls.clear()
        self.t[0] += 60                                                       # délai non écoulé
        res = self.go(True)
        self.assertEqual((res["ok"], res["result"]), (True, "retry_wait"))
        self.assertEqual((self.api.calls, self.status_bytes()), ([], st0))     # ni réseau ni statut réécrit
        rv = ratings.status(self.cfg, clock=self.clock)["retry"]
        self.assertEqual((rv["state"], rv["attempts"]), ("retry_wait", 0))
        self.assertEqual(rv["not_before"], self.t[0] - 60 + ratings.RETRY_BACKOFF_S[0])
        self.t[0] += ratings.RETRY_BACKOFF_S[0]
        self.api.mode = "ok"
        res = self.go(True)
        self.assertEqual((res["ok"], res["result"], res["retry_attempts"], res["mode"]), (True, "updated", 1, "retry"))
        last = ratings.status(self.cfg)["last"]
        self.assertEqual((last["retryable"], last["retry_attempts"], last["mode"]), (False, 1, "retry"))
        # succès : plus aucune reprise, aucune requête (pas de boucle)
        self.api.calls.clear()
        for _ in range(3):
            self.t[0] += 86400
            self.assertEqual(self.go(True)["result"], "retry_not_needed")
        self.assertEqual(self.api.calls, [])

    def test_attempts_are_bounded_and_daily_pass_resets_the_counter(self):
        self.api.mode = "down"
        self.go(False)
        calls = []
        for k in range(ratings.MAX_RETRIES):
            self.t[0] += ratings.RETRY_BACKOFF_S[k] - 1
            self.assertEqual(self.go(True)["result"], "retry_wait", k)        # jamais avant le délai croissant
            self.t[0] += 1
            n0 = len(self.api.calls)
            res = self.go(True)
            self.assertEqual((res["result"], res["retry_attempts"]), ("provider_unavailable", k + 1))
            calls.append(len(self.api.calls) - n0)
        self.assertTrue(all(c <= 5 for c in calls), calls)                     # disjoncteur : ≤ 5 requêtes/tentative
        self.api.calls.clear()
        self.t[0] += 10 * 86400
        res = self.go(True)
        self.assertEqual((res["ok"], res["result"]), (True, "retry_exhausted"))
        self.assertEqual(self.api.calls, [])
        st = ratings.status(self.cfg, clock=self.clock)
        self.assertEqual((st["last"]["result"], st["retry"]["state"], st["retry"]["attempts"]),
                         ("provider_unavailable", "retry_exhausted", ratings.MAX_RETRIES))
        self.assertEqual(ratings.health_view(self.cfg)["ok"], False)          # échec toujours rapporté
        res = self.go(False)                                                  # passage quotidien : compteur à zéro
        self.assertEqual(res["retry_attempts"], 0)
        self.t[0] += ratings.RETRY_BACKOFF_S[0]
        self.assertEqual(self.go(True)["retry_attempts"], 1)

    def test_clean_skips_and_non_transient_failures_are_never_retried(self):
        self.api.mode = "auth"
        self.assertEqual(self.go(False)["result"], "auth_failed")
        self.t[0] += 86400
        self.api.calls.clear()
        self.assertEqual(self.go(True)["result"], "retry_not_needed")
        self.assertEqual(self.api.calls, [])
        # échec transitoire, puis jeton retiré : la reprise devient un saut propre et s'arrête
        self.api.mode = "down"
        self.go(False)
        os.unlink(self.tok)
        self.t[0] += 3600
        res = self.go(True)
        self.assertEqual((res["ok"], res["result"], res["why"]), (True, "skipped", "credential_missing"))
        self.t[0] += 3600
        self.assertEqual(self.go(True)["result"], "retry_not_needed")
        # échec transitoire, puis catalogue suspendu (rollback) : held, sans jeton lu ni requête, puis plus rien
        self.write_token("TMDB_READ_TOKEN=%s\n" % TOKEN)
        self.go(False)
        with open(self.catf + cat.HOLD_SUFFIX, "w") as f:
            f.write('{"held_at": 1, "reason": "rollback", "v": 1}')
        os.chmod(self.catf + cat.HOLD_SUFFIX, 0o600)
        self.t[0] += 3600
        self.api.calls.clear()
        with mock.patch.object(ratings, "read_credential", side_effect=AssertionError("token read")):
            res = self.go(True)
        self.assertEqual((res["ok"], res["result"]), (True, "held"))
        self.t[0] += 3600
        self.assertEqual(self.go(True)["result"], "retry_not_needed")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.bytes_(), self.f0)

    def test_busy_daily_pass_is_noted_and_retried_but_busy_retry_notes_nothing(self):
        fd = os.open(self.catf + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            res = self.go(False)
            self.assertEqual(res["result"], "busy")
            last = ratings.status(self.cfg)["last"]
            self.assertEqual((last["result"], last["ok"], last["retryable"]), ("busy", False, True))
            st0 = self.status_bytes()
            self.t[0] += 7200
            self.assertEqual(self.go(True)["result"], "busy")                 # reprise occupée : rien noté
            self.assertEqual(self.status_bytes(), st0)
        finally:
            os.close(fd)
        self.assertEqual(self.api.calls, [])
        res = self.go(True)                                                   # verrou libre : la reprise passe
        self.assertEqual((res["ok"], res["result"], res["retry_attempts"]), (True, "updated", 1))

    def test_retry_survives_clock_going_backwards_but_stays_bounded(self):
        self.api.mode = "down"
        self.go(False)
        self.t[0] -= 86400                                                    # horloge revenue en arrière
        res = self.go(True)
        self.assertEqual(res["retry_attempts"], 1)
        for _ in range(20):
            self.t[0] -= 10
            self.go(True)
        self.assertEqual(ratings.status(self.cfg, clock=self.clock)["retry"]["attempts"], ratings.MAX_RETRIES)

    def test_retry_due_rejects_malformed_status(self):
        for last in ({}, None, {"result": "provider_unavailable", "ok": True},
                     {"result": "rating_drop", "ok": False}, {"result": "x", "ok": False}):
            self.assertFalse(ratings.retry_due(last, 1e9)[0], last)
        self.assertEqual(ratings.retry_due({"result": "partial", "ok": False, "retry_attempts": "9"}, 1e9),
                         (True, "retry_due"))                                 # compteur illisible : 0
        self.assertEqual(ratings.retry_due({"result": "partial", "ok": False, "retry_attempts": True,
                                            "finished_at": 1e9}, 1e9), (False, "retry_wait"))


# --------------------------------------------------------------------------- rafraîchissement + notes, bout en bout
class TestRefreshThenRatings(Base):
    def test_daily_refresh_new_item_rated_and_tmdb_outage_never_undoes_refresh(self):
        old = tcr.old_doc(tcr.films(range(1, 11)))
        for it in old["items"]:                       # notes TMDb déjà valides (moteur), comme en production
            for k in list(it):
                if k.startswith("tmdb_"):
                    del it[k]
        self.seed(old)
        self.assertEqual(self.rate()["rated"], 10)
        enriched = json.loads(self.bytes_())
        # rafraîchissement quotidien : un nouvel élément (11) ; notes des anciens conservées par le merge
        up = cat.refresh(self.cfg, fetch=tcr.Site(tcr.listing_of(tcr.films(range(1, 12)))), engine=BRIDGE)
        self.assertEqual((up["result"], up["added"]), ("updated", 1))
        after_refresh = self.bytes_()
        by = self.by_sid()
        self.assertNotIn("tmdb_match_status", by["11"])
        for sid in map(str, range(1, 11)):
            old_it = {x["site_id"]: x for x in enriched["items"]}[sid]
            for k, v in old_it.items():
                if k.startswith("tmdb_"):
                    self.assertEqual(by[sid][k], v, (sid, k))
        self.assertEqual(ratings.status(self.cfg)["coverage"]["pending"], 1)
        # TMDb en panne : le catalogue rafraîchi reste, valide, octet pour octet ; échec rapporté à part
        self.api.mode = "down"
        res = self.rate()
        self.assertEqual((res["ok"], res["result"]), (False, "provider_unavailable"))
        self.assertEqual(self.bytes_(), after_refresh)
        self.assertEqual(cat.status(self.cfg)["refresh"]["result"], "updated")
        self.assertEqual(len(BRIDGE.load_catalogue_meta(self.catf, BRIDGE.Config({}).detail_re)[0]), 11)
        # TMDb rétabli : le nouvel élément est noté, rien d'autre ne change
        self.api.mode = "ok"
        self.api.calls.clear()
        res = self.rate()
        self.assertEqual((res["ok"], res["result"], res["rated"], res["pending"], res["new"]),
                         (True, "updated", 11, 0, 1))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(TMDB._strip(json.loads(self.bytes_())), TMDB._strip(json.loads(after_refresh)))
        # le rafraîchissement suivant (inchangé) garde les notes : rien à refaire
        up = cat.refresh(self.cfg, fetch=tcr.Site(tcr.listing_of(tcr.films(range(1, 12)))), engine=BRIDGE)
        self.assertEqual(up["result"], "unchanged")
        self.assertEqual(self.rate()["result"], "unchanged")

    def test_hot_reload_signature_changes_on_enrichment(self):
        self.seed(doc_of(range(1, 4)))
        s0 = os.stat(self.catf)
        self.assertEqual(self.rate()["result"], "updated")
        s1 = os.stat(self.catf)
        self.assertNotEqual((s0.st_ino, s0.st_size, s0.st_mtime_ns), (s1.st_ino, s1.st_size, s1.st_mtime_ns))


# --------------------------------------------------------------------------- CLI root, santé, configuration
class TestCliHealthConfig(Base):
    def call(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def env(self):
        p = os.path.join(self.d, "opentv.env")
        with open(p, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\n" % (self.state, NANOPI))
        return p

    def test_root_starts_sandboxed_unit_and_reports_its_status(self):
        env = self.env()
        self.seed(doc_of(range(1, 4)))
        calls = []

        def runner(argv, timeout):
            calls.append((list(argv), timeout))
            if argv[:2] == ["systemctl", "start"]:
                with mock.patch.object(os, "geteuid", tcr.REAL_GETEUID):
                    ratings.run(self.cfg, fetch=self.api, engine=TMDB, bridge=BRIDGE, sleep=lambda s: None)
            return 0, ""
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", side_effect=runner):
            code, text = self.call(["--env", env, "catalogue", "ratings"])
        self.assertEqual(code, 0, text)
        self.assertEqual(calls[0][0], ["systemctl", "start", ratings.UNIT])
        self.assertGreater(calls[0][1], 360)
        rep = json.loads(text)
        self.assertEqual((rep["ok"], rep["ratings"]["result"], rep["coverage"]["rated"]), (True, "updated", 3))
        # busy / not_run
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner",
                                  side_effect=lambda a, t: (0, "75\n") if a[1] == "show" else (0, "")):
            code, text = self.call(["--env", env, "catalogue", "ratings"])
        self.assertEqual((code, json.loads(text)["result"]), (ratings.BUSY_EXIT, "busy"))
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", return_value=(0, "0\n")):
            code, text = self.call(["--env", env, "catalogue", "ratings"])
        self.assertEqual((code, json.loads(text)["result"]), (1, "not_run"))
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner",
                                  side_effect=lambda a, t: (124, "") if a[1] == "start" else (0, "0\n")):
            code, text = self.call(["--env", env, "catalogue", "ratings"])
        rep = json.loads(text)
        self.assertEqual((code, rep["result"]), (1, "timeout"))              # jamais présenté comme un passage
        self.assertIn("systemctl status", rep["note"])
        # passage quotidien empêché par le verrou : statut « busy » relu, code 75
        def busy_runner(argv, timeout):
            if argv[:2] == ["systemctl", "start"]:
                fd = os.open(self.catf + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    with mock.patch.object(os, "geteuid", tcr.REAL_GETEUID):
                        ratings.run(self.cfg, fetch=self.api, engine=TMDB, bridge=BRIDGE, sleep=lambda s: None)
                finally:
                    os.close(fd)
            return 0, "0\n"
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", side_effect=busy_runner):
            code, text = self.call(["--env", env, "catalogue", "ratings"])
        self.assertEqual((code, json.loads(text)["result"]), (ratings.BUSY_EXIT, "busy"))
        # reprise sous root : démarre l'unité de reprise (jamais exécutée en root) ; rien à reprendre ⇒ statut intact
        calls = []
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner",
                                  side_effect=lambda a, t: calls.append(a) or (0, "0\n")):
            code, text = self.call(["--env", env, "catalogue", "ratings-retry"])
        self.assertEqual(calls[0], ["systemctl", "start", ratings.RETRY_UNIT])
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)["result"], "retry_wait")            # dernier : busy, délai non écoulé
        code, text = self.call(["catalogue", "status", "--env", env])
        st = json.loads(text)
        self.assertEqual((st["ratings"]["credential"], st["ratings"]["coverage"]["rated"]), ("present", 3))
        for s in LEAKS:
            self.assertNotIn(s, text)

    def test_health_reports_failure_without_flipping_device_ok(self):
        self.seed(doc_of(range(1, 4)))
        self.api.mode = "down"
        self.rate()

        def runner(argv, timeout):
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, "failed\n" if argv[2] == ratings.UNIT else "active\n"
            return 1, ""
        with mock.patch.object(health, "verify_manifest", return_value={"ok": True}), \
                mock.patch.object(health.netstate, "snapshot", return_value={"mode": "wifi"}):
            rep = health.run(self.cfg, runner=runner, check_http=False)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["units"][ratings.UNIT], "failed")
        self.assertEqual((rep["ratings"]["ok"], rep["ratings"]["last_result"], rep["ratings"]["pending"]),
                         (False, "provider_unavailable", 3))
        self.assertIn("ratings_enrichment_failed", rep["warnings"])
        self.assertNotIn(ratings.UNIT, health.UNITS_REQUIRED)
        self.api.mode = "ok"
        self.rate()
        with mock.patch.object(health, "verify_manifest", return_value={"ok": True}), \
                mock.patch.object(health.netstate, "snapshot", return_value={"mode": "wifi"}):
            rep = health.run(self.cfg, runner=lambda a, t: (0, "active\n"), check_http=False)
        self.assertEqual((rep["ratings"]["ok"], rep["warnings"]), (True, []))

    def test_config_defaults_bounds_and_example(self):
        with mock.patch.object(config, "TMDB_DIR", "/etc/opentv/tmdb"):
            c = config.Config({"OPENTV_TMDB_FILE": "/tmp/elsewhere", "OPENTV_TMDB_DIR": "/tmp"})   # jamais lus
        self.assertEqual((c.tmdb_dir, c.tmdb_file, c.tmdb_languages, c.tmdb_max_requests),
                         ("/etc/opentv/tmdb", "/etc/opentv/tmdb/token", ("fr-FR", "fr-CA"), 300))
        self.assertEqual((c.tmdb_rating_max_age_s, c.tmdb_recheck_s), (7 * 86400, 30 * 86400))
        self.assertEqual(REAL_TMDB_DIR, "/etc/opentv/tmdb")
        self.assertNotIn("OPENTV_TMDB_FILE", config.DEFAULTS)
        for k, v in (("OPENTV_TMDB_LANGUAGES", ""),
                     ("OPENTV_TMDB_LANGUAGES", "fr,en"), ("OPENTV_TMDB_LANGUAGES", "fr-FR,en-US,de-DE,it-IT"),
                     ("OPENTV_TMDB_MAX_REQUESTS", "0"), ("OPENTV_TMDB_MAX_REQUESTS", "2001"),
                     ("OPENTV_TMDB_RATING_MAX_AGE_DAYS", "0"), ("OPENTV_TMDB_RECHECK_DAYS", "nan")):
            with self.assertRaises(ValueError, msg=(k, v)):
                config.Config({k: v})
        with open(os.path.join(PI4, "opentv-pi.env.example"), encoding="utf-8") as f:
            text = f.read()
        for k in ("OPENTV_TMDB_LANGUAGES", "OPENTV_TMDB_MAX_REQUESTS",
                  "OPENTV_TMDB_RATING_MAX_AGE_DAYS", "OPENTV_TMDB_RECHECK_DAYS"):
            self.assertIn("#%s=%s" % (k, config.DEFAULTS[k]), text)
        self.assertNotIn("TMDB_READ_TOKEN", text)                             # aucun jeton dans opentv.env
        self.assertNotIn("OPENTV_TMDB_FILE", text)
        self.assertNotIn("tmdb.env", text)
        with open(os.path.join(PI4, "tmdb-token.example"), encoding="utf-8") as f:
            ex = f.read()
        self.assertEqual(ratings.parse_credential(ex), {})                    # modèle : tout commenté, sans secret
        self.assertFalse(TMDB.V4_RE.search(ex) or re.search(r"\b[0-9a-f]{32}\b", ex))


# --------------------------------------------------------------------------- unités, installation
def unit(name):
    return tcr.unit(name)


RATINGS_UNITS = (ratings.UNIT, ratings.RETRY_UNIT)


def inaccessible(text):
    return " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()


class TestUnitsInstall(unittest.TestCase):
    def test_ratings_unit_is_separate_unprivileged_sandboxed_and_triggered_on_refresh_success(self):
        cp, text = unit(ratings.UNIT)
        s, u = cp["Service"], cp["Unit"]
        self.assertEqual((s["Type"], s["User"], s["Group"]), ("oneshot", "opentv", "opentv"))
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi catalogue ratings")
        self.assertEqual(s["SuccessExitStatus"], str(ratings.BUSY_EXIT))
        self.assertGreater(int(s["TimeoutStartSec"]), ratings.DEADLINE_S + 60)
        self.assertLess(int(s["TimeoutStartSec"]), cli.RATINGS_UNIT_TIMEOUT_S)
        self.assertEqual((s["CapabilityBoundingSet"], s["NoNewPrivileges"], s["ProtectSystem"]), ("", "yes", "strict"))
        self.assertEqual(re.findall(r"^ReadWritePaths=(.+)$", text, re.M), ["-/var/lib/opentv"])
        self.assertNotIn("Install", cp.sections())                            # jamais activée ni lancée seule
        self.assertNotIn("SupplementaryGroups", s)
        self.assertIn("localhost", s["IPAddressDeny"].split())
        self.assertEqual(re.findall(r"^EnvironmentFile=(.+)$", text, re.M), ["-/etc/opentv/opentv.env"])
        self.assertNotIn("LoadCredential", text)
        code = re.sub(r"^#.*$", "", text, flags=re.M)
        for bad in ("Restart=", "RemoveIPC", "ExecStartPre", "ExecStartPost", "systemctl", "tmdb.env", "/etc/opentv/tmdb"):
            self.assertNotIn(bad, code)                                       # le dossier du jeton n'est pas masqué ici
        self.assertIn("opentv-catalogue-refresh.service", u["After"].split())
        self.assertIn(ratings.RETRY_UNIT, u["After"].split())
        self.assertIn("ConditionPathExists=!/run/opentv-uninstalling", text)
        # bac à sable : même état masqué que le rafraîchissement + source approuvée et cache du résolveur
        inacc = {p[1:] for p in inaccessible(text)}
        _, rtext = unit("opentv-catalogue-refresh.service")
        rinacc = {p[1:] for p in inaccessible(rtext)}
        c = config.Config({})
        self.assertEqual(rinacc - inacc, {c.tmdb_dir})
        self.assertEqual(inacc - rinacc, {c.source_file, c.source_state})
        need = [c.catalogue_file + x for x in ("", ".lock", cat.HOLD_SUFFIX, ratings.STATE_SUFFIX)] + \
            [os.path.join(c.state_dir, ratings.STATUS_NAME), c.tmdb_dir, c.tmdb_file, c.engine_dir]
        for n in need:
            for p in inacc:
                self.assertFalse(n == p or n.startswith(p.rstrip("/") + "/"), (n, p))
        # déclenchement : après un rafraîchissement RÉUSSI seulement (jamais dans le passage lui-même)
        rcp, _ = unit("opentv-catalogue-refresh.service")
        self.assertEqual(rcp["Unit"]["OnSuccess"], ratings.UNIT)
        self.assertNotIn(ratings.UNIT, rcp["Service"].get("ExecStartPost", ""))
        self.assertNotIn("OnFailure", rcp["Unit"])
        self.assertIn("ConditionPathExists=!/run/opentv-uninstalling", rtext)
        self.assertIn("ConditionPathExists=/etc/opentv/source.env", rtext)

    def test_retry_unit_same_sandbox_bounded_timer_and_no_ordering_cycle(self):
        cp, text = unit(ratings.RETRY_UNIT)
        dcp, dtext = unit(ratings.UNIT)
        s, d = dict(cp["Service"]), dict(dcp["Service"])
        self.assertEqual(s.pop("ExecStart"), "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi catalogue ratings-retry")
        d.pop("ExecStart")
        self.assertEqual(s, d)                                                # même bac à sable, même délai
        body = lambda t: [l for l in t[t.index("\n[Service]\n"):].splitlines()     # noqa: E731
                          if l and not l.startswith("#") and not l.startswith("ExecStart=")]
        self.assertEqual(body(text), body(dtext))                             # y compris les clés répétées
        self.assertNotIn("Install", cp.sections())
        conds = re.findall(r"^ConditionPathExists=(.+)$", text, re.M)
        self.assertEqual(sorted(conds), sorted(["/etc/opentv/tmdb/token", "!/run/opentv-uninstalling"]))
        code = re.sub(r"^#.*$", "", text, flags=re.M)
        for bad in ("Restart=", "OnSuccess", "OnFailure", "ExecStartPost", "ExecStartPre"):
            self.assertNotIn(bad, code)
        # ordre : rafraîchissement et passage quotidien ATTENDENT une reprise en cours ; la reprise n'attend rien
        after = {n: unit(n)[0]["Unit"].get("After", "").split() for n in
                 ("opentv-catalogue-refresh.service", ratings.UNIT, ratings.RETRY_UNIT)}
        self.assertIn(ratings.RETRY_UNIT, after["opentv-catalogue-refresh.service"])
        self.assertIn(ratings.RETRY_UNIT, after[ratings.UNIT])
        self.assertIn("opentv-catalogue-refresh.service", after[ratings.UNIT])
        self.assertFalse({"opentv-catalogue-refresh.service", ratings.UNIT} & set(after[ratings.RETRY_UNIT]))
        for n in ("opentv-catalogue-refresh.service", ratings.UNIT, ratings.RETRY_UNIT):   # aucun Before= (cycle)
            self.assertNotIn("Before", unit(n)[0]["Unit"])
        tcp, _ = unit(ratings.RETRY_TIMER)
        t = tcp["Timer"]
        self.assertEqual((t["OnCalendar"], t["Persistent"], t["Unit"]), ("*-*-* *:10:00", "false", ratings.RETRY_UNIT))
        m = re.fullmatch(r"(\d+)min", t["RandomizedDelaySec"])
        self.assertTrue(m and 0 < int(m.group(1)) < 60)                       # au plus une tentative par heure
        for k in ("OnUnitActiveSec", "OnUnitInactiveSec", "OnBootSec", "OnActiveSec"):
            self.assertNotIn(k, t)
        self.assertEqual(tcp["Install"]["WantedBy"], "timers.target")
        for n in (ratings.RETRY_TIMER, ratings.RETRY_UNIT):
            self.assertIn(n, health.UNITS_OPTIONAL)
            self.assertNotIn(n, health.UNITS_REQUIRED)
        # bornes : MAX_RETRIES délais croissants, le premier ≥ 30 min, jamais plus d'un passage par heure
        self.assertEqual(len(ratings.RETRY_BACKOFF_S), ratings.MAX_RETRIES)
        self.assertGreaterEqual(ratings.RETRY_BACKOFF_S[0], 1800)
        self.assertEqual(list(ratings.RETRY_BACKOFF_S), sorted(ratings.RETRY_BACKOFF_S))
        self.assertLessEqual(ratings.MAX_RETRIES, 6)
        for code_ in ("auth_failed", "credential_unsafe", "credential_invalid", "rating_drop", "invalid_candidate",
                      "skipped", "held", "service_group_not_private", "catalogue_invalid", "hold_unsafe"):
            self.assertNotIn(code_, ratings.RETRYABLE)

    def test_token_directory_masked_from_every_other_unit(self):
        c = config.Config({})
        users = {}
        for name in sorted(os.listdir(os.path.join(PI4, "systemd"))):
            if not name.endswith(".service"):
                continue
            cp, text = unit(name)
            s = cp["Service"]
            users[name] = s.get("User", "root")
            self.assertNotIn("tmdb", " ".join(re.findall(r"^EnvironmentFile=(.+)$", text, re.M)), name)
            self.assertNotIn("tmdb.env", text, name)
            if name in RATINGS_UNITS:
                self.assertNotIn("-" + c.tmdb_dir, inaccessible(text), name)
                continue
            if name == "opentv-kiosk.service":
                # Cage sans espace de montage (session TTY) : protégé par le dossier 0750 root:opentv, le compte
                # opentv-kiosk n'étant pas du groupe opentv (ci-dessous)
                self.assertEqual(users[name], "opentv-kiosk")
                self.assertNotIn("opentv", (s.get("SupplementaryGroups") or "").split(), name)
                continue
            self.assertIn("-" + c.tmdb_dir, inaccessible(text), name)        # le DOSSIER, jamais le seul fichier
            self.assertNotIn("-" + c.tmdb_file, inaccessible(text), name)
            self.assertNotIn("opentv", (s.get("SupplementaryGroups") or "").split(), name)
        # unités du compte du service (hors notes) = celles dont le dépôt vérifie le masque en cours d'exécution
        self.assertEqual({n for n, u in users.items() if u == "opentv" and n not in RATINGS_UNITS},
                         set(ratings.MASKED_UNITS))
        # groupe opentv privé : aucune appartenance déclarée par sysusers
        with open(os.path.join(PI4, "system", "sysusers-opentv.conf"), encoding="utf-8") as f:
            sysu = [l.split() for l in f if l.strip() and not l.startswith("#")]
        self.assertFalse([l for l in sysu if l[0] == "m" and l[2] == "opentv"])
        # dossier créé par tmpfiles (installation + chaque démarrage), root:opentv 0750, jamais vidé par âge
        with open(os.path.join(PI4, "system", "tmpfiles-opentv.conf"), encoding="utf-8") as f:
            tmp = [l.split() for l in f if l.strip() and not l.startswith("#")]
        self.assertIn(["d", c.tmdb_dir, "0750", "root", "opentv", "-"], tmp)
        self.assertFalse([l for l in tmp if l[1].startswith(c.tmdb_dir) and l[0] != "d"])
        # installation : tmpfiles AVANT tout (re)démarrage d'unité
        with open(os.path.join(PI4, "install.sh"), encoding="utf-8") as f:
            ins = f.read()
        i_tmp = ins.index("post systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf")
        self.assertLess(i_tmp, ins.index("post systemctl restart"))
        self.assertLess(i_tmp, ins.index("post systemctl enable"))

    def sh(self, *args):
        env = dict(os.environ, LC_ALL="C")
        return subprocess.run(["bash"] + list(args), capture_output=True, timeout=120, env=env)

    def test_install_ships_engine_unchanged_never_the_token_and_uninstall_reverts(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        tdir = os.path.join(r, "etc/opentv/tmdb")
        os.makedirs(tdir)
        os.makedirs(os.path.join(r, "run"))
        tok = os.path.join(tdir, "token")
        with open(tok, "w") as f:
            f.write("TMDB_READ_TOKEN=%s\n" % TOKEN)
        os.chmod(tok, 0o640)
        marker = os.path.join(r, "run/opentv-uninstalling")
        with open(marker, "w"):
            pass                                                              # désinstallation interrompue
        p = self.sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode() + p.stderr.decode()
        self.assertNotIn(TOKEN, out)
        self.assertFalse(os.path.lexists(marker))                             # install.sh le retire
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            man = f.read()
        for dst in ("/opt/opentv/lib/foryou/opentv_tmdb.py", "/etc/systemd/system/" + ratings.UNIT,
                    "/etc/systemd/system/" + ratings.RETRY_UNIT, "/etc/systemd/system/" + ratings.RETRY_TIMER,
                    "/opt/opentv/tmdb-token.example", "/opt/opentv/lib/opentv_pi/ratings.py",
                    "/etc/tmpfiles.d/opentv.conf"):
            self.assertIn("  " + dst + "\n", man)
        self.assertNotIn("/etc/opentv/tmdb", man)
        with open(os.path.join(NANOPI, "opentv_tmdb.py"), "rb") as a, \
                open(os.path.join(r, "opt/opentv/lib/foryou/opentv_tmdb.py"), "rb") as b:
            self.assertEqual(a.read(), b.read())                              # moteur vérifié livré tel quel
        self.assertTrue(health.verify_manifest(r)["ok"])
        lines = out.splitlines()
        en = [l for l in lines if "systemctl enable" in l][0]
        rs = [l for l in lines if "systemctl restart" in l][0]
        self.assertNotIn(ratings.UNIT, en + rs)                               # jamais de passage à l'installation
        self.assertNotIn(ratings.RETRY_UNIT, en + rs)
        self.assertIn(ratings.RETRY_TIMER, en)
        self.assertIn(ratings.RETRY_TIMER, rs)
        i_tmp = [i for i, l in enumerate(lines) if "systemd-tmpfiles --create" in l][0]
        i_rs = [i for i, l in enumerate(lines) if "systemctl restart" in l][0]
        self.assertLess(i_tmp, i_rs)                                          # dossier du jeton avant les unités
        with open(tok) as f:
            self.assertEqual(f.read(), "TMDB_READ_TOKEN=%s\n" % TOKEN)        # jeton jamais touché
        p = self.sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")   # mise à jour idempotente
        self.assertEqual(p.returncode, 0, p.stderr)
        q = self.sh(os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r)
        self.assertEqual(q.returncode, 0, q.stderr)
        ul = q.stdout.decode().splitlines()

        def at(pred):
            got = [i for i, l in enumerate(ul) if pred(l)]
            self.assertTrue(got, ul)
            return got[0]
        i_timers = at(lambda l: l.endswith("systemctl stop opentv-catalogue-refresh.timer "
                                           "opentv-catalogue-ratings-retry.timer"))
        i_refresh = at(lambda l: l.endswith("systemctl stop opentv-catalogue-refresh.service"))
        i_rat = at(lambda l: "systemctl stop opentv-catalogue-ratings.service opentv-catalogue-ratings-retry.service"
                   in l)
        i_all = at(lambda l: "disable --now opentv-player.service" in l)
        self.assertLess(i_timers, i_refresh)
        self.assertLess(i_refresh, i_rat)
        self.assertLess(i_rat, i_all)
        for n in (ratings.UNIT, ratings.RETRY_UNIT, ratings.RETRY_TIMER):
            self.assertIn(n, ul[i_all])
        self.assertFalse(os.path.lexists(marker))                             # retiré à la fin
        for pth in ("etc/systemd/system/" + ratings.UNIT, "etc/systemd/system/" + ratings.RETRY_UNIT,
                    "etc/systemd/system/" + ratings.RETRY_TIMER, "opt/opentv/lib/foryou/opentv_tmdb.py",
                    "opt/opentv/tmdb-token.example"):
            self.assertFalse(os.path.exists(os.path.join(r, pth)), pth)
        self.assertTrue(os.path.isfile(tok))                                  # conservé sans --purge
        self.assertTrue(os.path.isdir(tdir))
        p = self.sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
        self.assertEqual(p.returncode, 0, p.stderr)
        q = self.sh(os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r, "--purge")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertFalse(os.path.lexists(tok))
        self.assertFalse(os.path.lexists(tdir))

    def test_uninstall_blocks_catalogue_jobs_before_stopping_them(self):
        with open(os.path.join(PI4, "uninstall.sh"), encoding="utf-8") as f:
            text = f.read()
        i_touch = text.index('act touch -- "$UNINSTALLING"')
        i_first_stop = min(text.index("host systemctl disable --now"), text.index("host systemctl stop"))
        self.assertLess(i_touch, i_first_stop)                                # témoin AVANT tout arrêt
        self.assertLess(text.index("host systemctl daemon-reload"), text.index('act rm -f -- "$UNINSTALLING"'))
        self.assertIn('UNINSTALLING="$(dest /run/opentv-uninstalling)"', text)
        for n in ("opentv-catalogue-refresh.service", ratings.UNIT, ratings.RETRY_UNIT):
            self.assertIn("ConditionPathExists=!/run/opentv-uninstalling", unit(n)[1], n)

    def test_failed_install_rolls_back_ratings_files(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        os.makedirs(os.path.join(r, "etc/pam.d"))
        os.chmod(os.path.join(r, "etc/pam.d"), 0o555)
        self.addCleanup(os.chmod, os.path.join(r, "etc/pam.d"), 0o755)
        p = self.sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
        self.assertNotEqual(p.returncode, 0)
        for pth in ("etc/systemd/system/" + ratings.UNIT, "etc/systemd/system/" + ratings.RETRY_TIMER,
                    "opt/opentv/lib/foryou/opentv_tmdb.py", "opt/opentv/tmdb-token.example",
                    "opt/opentv/MANIFEST.sha256"):
            self.assertFalse(os.path.exists(os.path.join(r, pth)), pth)


if __name__ == "__main__":
    unittest.main()
