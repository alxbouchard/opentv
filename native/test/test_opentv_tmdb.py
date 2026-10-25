"""Enrichissement TMDb du catalogue (scripts/nanopi/opentv_tmdb.py) — sans réseau réel ni vrai secret.

Depuis la racine : python3 -m unittest -v native.test.test_opentv_tmdb
"""
import http.server
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr

from native.test.test_opentv_bridge import B, ROOT

SRC = os.path.join(ROOT, "scripts", "nanopi", "opentv_tmdb.py")
spec = importlib.util.spec_from_file_location("opentv_tmdb", SRC)
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

FAKE_V4 = "eyJ" + "a" * 20 + "." + "b" * 24 + "." + "c" * 30     # forme JWT, sans valeur réelle
FAKE_V3 = "0123456789abcdef0123456789abcdef"


def movie(i, title, date, avg=7.0, votes=100, original=None, adult=False):
    return {"id": i, "title": title, "original_title": original or title, "release_date": date,
            "vote_average": avg, "vote_count": votes, "adult": adult}


class FakeTmdb:
    """fetch(url, headers, timeout) : {('/search/movie', query) | ('/find', imdb) | ('/movie', id): réponse}."""

    def __init__(self, search=None, find=None, details=None, status=None):
        self.search, self.find, self.details = search or {}, find or {}, details or {}
        self.status, self.log = status or {}, []

    def __call__(self, url, headers, timeout):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.log.append((u.path, q, dict(headers)))
        path = u.path[len("/3"):] if u.path.startswith("/3/") else u.path
        st = self.status.get(path)
        if callable(st):
            st = st()
        if st:
            return st, {"content-type": "application/json", "retry-after": "0"}, b"{}"
        if path == "/search/movie":
            body = {"results": self.search.get(q.get("query"), [])}
        elif path.startswith("/find/"):
            body = {"movie_results": self.find.get(path[6:], [])}
        elif path.startswith("/movie/"):
            d = self.details.get(int(path[7:]))
            if d is None:
                return 404, {"content-type": "application/json"}, b"{}"
            body = d
        else:
            return 404, {"content-type": "application/json"}, b"{}"
        return 200, {"content-type": "application/json;charset=utf-8"}, json.dumps(body).encode()


def client(fake, n=400, kind="bearer"):
    return T.Client((kind, FAKE_V4 if kind == "bearer" else FAKE_V3), base=T.PROD_BASE, fetch=fake, max_requests=n,
                    min_interval=0, sleep=lambda s: None)


def catalogue(items, **extra):
    d = {"generated_at": "2026-10-25T00:00:00Z", "source": "wadrox.com", "count": len(items), "items": items}
    d.update(extra)
    return d


def item(title, year, n, **kw):
    x = {"title": title, "year": year, "detail_path": "b/wadrox/%d" % n, "site_id": str(n), "source_id": str(n),
         "poster": "https://image.tmdb.org/t/p/w300/%d.jpg" % n}
    x.update(kw)
    return x


class NormalisationTests(unittest.TestCase):
    def test_exact_and_loose(self):
        self.assertEqual(T.exact_title("  L’Ouragan   Rouge "), "l'ouragan rouge")
        self.assertEqual(T.loose_title("L'Été meurtrier !"), "ete meurtrier")
        self.assertEqual(T.loose_title("The Matrix"), "matrix")
        self.assertEqual(T.loose_title("Les"), "les")              # un article seul n'est pas effacé
        self.assertEqual(T.loose_title("Spider-Man: No Way Home"), "spider man no way home")


class SelectTests(unittest.TestCase):
    def sel(self, title, year, cands):
        return T.select(title, year, [T.clean_candidate(c) for c in cands])

    def test_exact_title_same_year_high(self):
        st, c, m, conf, why, ids = self.sel("Contre l'Ouragan", 2026, [
            movie(1, "Contre l'Ouragan", "2026-03-01"), movie(2, "Contre l'Ouragan", "1988-01-01")])
        self.assertEqual((st, c["id"], m, conf, why), ("matched", 1, "exact_title_year", "high", "exact_year"))

    def test_original_title_and_loose(self):
        st, c, m, conf, _, _ = self.sel("Ete meurtrier", 1983, [movie(5, "Murderous Summer", "1983-05-11",
                                                                      original="L'Été meurtrier")])
        self.assertEqual((st, c["id"], m, conf), ("matched", 5, "normalized_title_year", "medium"))

    def test_multiple_same_year_ambiguous(self):
        st, c, *_ , ids = self.sel("Alice", 2010, [movie(1, "Alice", "2010-01-01"), movie(2, "Alice", "2010-06-01")])
        self.assertEqual((st, c, ids), ("ambiguous", None, [1, 2]))

    def test_year_pm1_only_exact_and_unique(self):
        self.assertEqual(self.sel("Dune", 2021, [movie(9, "Dune", "2020-12-30")])[:4], ("matched", T.clean_candidate(
            movie(9, "Dune", "2020-12-30")), "exact_title_year", "medium"))
        self.assertEqual(self.sel("Dune", 2021, [movie(9, "Dune", "2020-12-30"), movie(8, "Dune", "2022-01-01")])[0],
                         "ambiguous")
        self.assertEqual(self.sel("L'Dune!", 2021, [movie(9, "Dune", "2020-12-30")])[0], "ambiguous")

    def test_unmatched_reasons(self):
        self.assertEqual(self.sel("Dune", None, [movie(9, "Dune", "2021-01-01")])[4], "no_year")
        self.assertEqual(self.sel("Dune", 2021, [movie(9, "Dune", "1984-01-01")])[4], "year_mismatch")
        self.assertEqual(self.sel("Dune", 2021, [movie(9, "Dunkerque", "2021-01-01")])[4], "no_title_match")
        self.assertEqual(self.sel("Dune", 2021, [])[4], "no_results")
        self.assertEqual(self.sel("Dune", 2021, [movie(9, "Dune", "")])[4], "year_mismatch")

    def test_adult_and_garbage_ignored(self):
        self.assertIsNone(T.clean_candidate(movie(1, "X", "2020-01-01", adult=True)))
        for bad in ({"id": "1"}, {"id": True}, {"id": 0}, "x", None):
            self.assertIsNone(T.clean_candidate(bad))
        c = T.clean_candidate({"id": 3, "title": 5, "vote_average": 99, "vote_count": -1})
        self.assertEqual((c["title"], c["vote_average"], c["vote_count"]), ("", None, None))


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.path = os.path.join(self.d, "catalogue.json")
        self.cat = catalogue([
            item("Contre l'Ouragan", 2026, 1, imdb_rating=6.8, imdb_votes=900, imdb_id="tt0000001", match_status="matched"),
            item("Alice", 2010, 2),
            item("Sans année", None, 3),
            item("Zéro vote", 2025, 4),
            item("Inconnu", 2024, 5),
        ])
        B.atomic_write_json(self.path, self.cat)
        self.fake = FakeTmdb(
            find={"tt0000001": [movie(101, "Against the Hurricane", "2026-02-01", avg=6.43, votes=321)]},
            search={"Alice": [movie(201, "Alice", "2010-01-01"), movie(202, "Alice", "2010-09-09")],
                    "Zéro vote": [movie(401, "Zéro vote", "2025-01-01", avg=0, votes=0)],
                    "Inconnu": []},
            details={101: movie(101, "Against the Hurricane", "2026-02-01", avg=6.43, votes=321),
                     401: movie(401, "Zéro vote", "2025-01-01", avg=0, votes=0)})

    def run_enrich(self, fake=None, n=400, now=1_800_000_000, **kw):
        with redirect_stderr(io.StringIO()):
            return T.run_enrich(self.path, client(fake or self.fake, n), ("fr-FR",), now=now, **kw)

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_first_run_labels_and_preserves_everything_else(self):
        n = self.run_enrich()
        self.assertTrue(n["wrote"])
        self.assertEqual((n["matched"], n["ambiguous"], n["unmatched"], n["new"]), (2, 1, 2, 5))
        out = self.load()
        a, alice, noyear, zero, unk = out["items"]
        self.assertEqual((a["tmdb_id"], a["tmdb_rating"], a["tmdb_votes"], a["tmdb_match_method"], a["tmdb_confidence"]),
                         (101, 6.4, 321, "imdb_id", "high"))
        self.assertEqual((a["imdb_rating"], a["imdb_votes"], a["match_status"]), (6.8, 900, "matched"))   # IMDb intact
        self.assertEqual((alice["tmdb_match_status"], alice.get("tmdb_rating"), alice["tmdb_evidence"]["candidates"]),
                         ("ambiguous", None, [201, 202]))
        self.assertEqual((noyear["tmdb_match_status"], noyear["tmdb_reason"]), ("unmatched", "no_year"))
        self.assertEqual((zero["tmdb_match_status"], zero["tmdb_rating"], zero["tmdb_votes"]), ("matched", None, 0))
        self.assertEqual(unk["tmdb_reason"], "no_results")
        self.assertEqual(out["attribution"]["tmdb"], T.TMDB_NOTICE)
        self.assertEqual(T._strip(out), T._strip(self.cat))
        # pas de requête pour l'élément sans année ; bearer seulement, jamais api_key dans l'URL
        self.assertEqual(len(self.fake.log), 4)
        self.assertTrue(all("api_key" not in q and h["Authorization"] == "Bearer " + FAKE_V4 for _, q, h in self.fake.log))
        p = T.paths(self.path)
        for f in (self.path, p["bak"], p["state"]):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        with open(p["bak"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), self.cat)

    def test_second_run_reuses_and_is_byte_stable(self):
        self.run_enrich()
        with open(self.path, "rb") as f:
            first = f.read()
        self.fake.log.clear()
        n = self.run_enrich(now=1_800_000_000 + 3600)
        self.assertEqual((n["wrote"], n["reused"], n["requests"]), (False, 5, 0))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), first)

    def test_stale_rating_refresh_keeps_timestamp_when_unchanged(self):
        self.run_enrich()
        ts = self.load()["items"][0]["tmdb_rating_updated_at"]
        n = self.run_enrich(now=1_800_000_000 + 8 * 86400)
        self.assertEqual((n["rating_refresh"], n["wrote"]), (2, False))
        self.assertEqual(self.load()["items"][0]["tmdb_rating_updated_at"], ts)
        self.fake.details[101] = movie(101, "Against the Hurricane", "2026-02-01", avg=7.04, votes=400)
        n = self.run_enrich(now=1_800_000_000 + 16 * 86400)
        a = self.load()["items"][0]
        self.assertEqual((a["tmdb_rating"], a["tmdb_votes"], n["wrote"]), (7.0, 400, True))
        self.assertNotEqual(a["tmdb_rating_updated_at"], ts)

    def test_changed_title_rematches_and_gone_provider(self):
        self.run_enrich()
        del self.fake.details[101]
        n = self.run_enrich(now=1_800_000_000 + 8 * 86400)
        a = self.load()["items"][0]
        self.assertEqual((a["tmdb_match_status"], a["tmdb_reason"], a.get("tmdb_rating")), ("unmatched", "provider_gone", None))
        self.assertTrue(n["wrote"])

    def test_outage_keeps_prior_values_and_opens_circuit(self):
        self.run_enrich()
        before = self.load()
        fake = FakeTmdb(status={"/movie/101": 503, "/movie/401": 503})
        n = self.run_enrich(fake, now=1_800_000_000 + 8 * 86400)
        self.assertEqual((n["errors"], n["wrote"]), (2, False))
        self.assertEqual(self.load(), before)
        # 5 échecs consécutifs : arrêt, le reste est différé
        cat = catalogue([item("T%d" % i, 2000, 10 + i) for i in range(9)])
        B.atomic_write_json(self.path, cat)
        n = self.run_enrich(FakeTmdb(status={"/search/movie": 500}), now=1_800_000_000)
        self.assertEqual((n["errors"], n["deferred"], n.get("circuit_open"), n["wrote"]), (5, 4, True, False))

    def test_auth_failure_writes_nothing(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        with self.assertRaises(T.Abort) as cm:
            self.run_enrich(FakeTmdb(status={"/find/tt0000001": 401}))
        self.assertEqual(cm.exception.code, T.EXIT_AUTH)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertFalse(os.path.exists(T.paths(self.path)["bak"]))

    def test_429_retried_once_then_transient(self):
        calls = {"n": 0}

        def once():
            calls["n"] += 1
            return 429 if calls["n"] == 1 else None
        fake = FakeTmdb(search=self.fake.search, find=self.fake.find, status={"/find/tt0000001": once})
        n = self.run_enrich(fake)
        self.assertEqual((n["errors"], n["matched"]), (0, 2))
        n2 = FakeTmdb(status={"/search/movie": 429})
        c = client(n2)
        with self.assertRaises(T.TmdbError):
            c.get("/search/movie", [("query", "x")])
        self.assertEqual(c.requests, 2)

    def test_request_budget_defers(self):
        n = self.run_enrich(n=2)
        self.assertEqual((n["requests"], n["deferred"]), (2, 3))
        n = self.run_enrich(n=50)
        self.assertEqual(n["deferred"], 0)

    def test_dry_run_writes_nothing(self):
        n = self.run_enrich(dry_run=True)
        self.assertEqual((n["wrote"], n["matched"]), (False, 2))
        self.assertEqual(self.load(), self.cat)
        self.assertFalse(os.path.exists(T.paths(self.path)["state"]))

    def test_commit_guards(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        new = json.loads(raw)
        new["items"][0]["title"] = "modifié"
        with self.assertRaises(T.Abort) as cm:
            T.commit(self.path, raw, json.loads(raw), new)
        self.assertEqual(cm.exception.why, "non_tmdb_change")
        new = json.loads(raw)
        new["items"].pop()
        with self.assertRaises(T.Abort):
            T.commit(self.path, raw, json.loads(raw), new)
        new = json.loads(raw)
        new["items"][0].update(tmdb_match_status="matched", tmdb_match_key="k", tmdb_id=1, tmdb_rating=0,
                               tmdb_votes=0, tmdb_match_method="imdb_id", tmdb_confidence="high")
        with self.assertRaises(T.Abort) as cm:                  # note sans votes = jamais 0 affiché
            T.commit(self.path, raw, json.loads(raw), new)
        self.assertTrue(cm.exception.why.startswith("invalid_tmdb_fields"))
        new = json.loads(raw)
        new["items"][1].update(tmdb_match_status="ambiguous", tmdb_match_key="k", tmdb_rating=7.0)
        with self.assertRaises(T.Abort):
            T.commit(self.path, raw, json.loads(raw), new)

    def test_concurrent_change_and_lock(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        new = json.loads(raw)
        new["items"][1].update(tmdb_match_status="unmatched", tmdb_match_key="k", tmdb_reason="no_results")
        B.atomic_write_json(self.path, dict(self.cat, generated_at="autre"))
        with self.assertRaises(T.Abort) as cm:
            T.commit(self.path, raw, json.loads(raw), new)
        self.assertEqual(cm.exception.why, "catalogue_changed")
        with T.FileLock(self.path):
            with self.assertRaises(T.Abort) as cm:
                self.run_enrich()
            self.assertEqual(cm.exception.why, "locked")

    def test_rollback_and_status(self):
        self.run_enrich()
        st = T.run_status(self.path)
        self.assertEqual((st["matched"], st["rated"], st["ambiguous"], st["unmatched"], st["invalid"], st["backup"]),
                         (2, 1, 1, 2, 0, True))
        self.assertEqual(T.run_rollback(self.path), {"restored": True, "items": 5})
        self.assertEqual(self.load(), self.cat)
        os.unlink(T.paths(self.path)["bak"])
        with self.assertRaises(T.Abort):
            T.run_rollback(self.path)

    def test_bridge_reads_tmdb_fields_conservatively(self):
        self.run_enrich()
        items, _, _ = B.load_catalogue_meta(self.path, B.Config({}).detail_re)
        a, alice, _, zero, _ = items
        self.assertEqual((a["tmdb_rating"], a["tmdb_votes"], a["tmdb_id"], a["tmdb_match_status"]), (6.4, 321, 101, "matched"))
        self.assertEqual((a["imdb_rating"], a["imdb_votes"]), (6.8, 900))
        self.assertEqual((alice["tmdb_rating"], alice["tmdb_id"], alice["tmdb_match_status"]), (None, None, "ambiguous"))
        self.assertEqual((zero["tmdb_rating"], zero["tmdb_votes"]), (None, None))
        forged = B.clean_item({"title": "x", "detail_path": "b/wadrox/9", "tmdb_match_status": "ambiguous",
                               "tmdb_id": 5, "tmdb_rating": 9.9, "tmdb_votes": 10}, B.Config({}).detail_re)
        self.assertEqual((forged["tmdb_rating"], forged["tmdb_id"]), (None, None))


class CredentialTests(unittest.TestCase):
    def test_env_only_popped_and_validated(self):
        env = {"TMDB_READ_TOKEN": FAKE_V4, "TMDB_API_KEY": FAKE_V3}
        self.assertEqual(T.take_credential(env), ("bearer", FAKE_V4))
        self.assertEqual(env, {})
        self.assertEqual(T.take_credential({"TMDB_API_KEY": FAKE_V3}), ("api_key", FAKE_V3))
        for bad in ({"TMDB_READ_TOKEN": "abc"}, {"TMDB_API_KEY": "XYZ"}, {}):
            with self.assertRaises(T.Abort) as cm:
                T.take_credential(dict(bad))
            self.assertEqual(cm.exception.code, T.EXIT_CONFIG)
            self.assertNotIn("abc", cm.exception.why)

    def test_v3_key_in_query_and_repr_redacted(self):
        fake = FakeTmdb()
        c = client(fake, kind="api_key")
        c.get("/search/movie", [("query", "x")])
        self.assertEqual(fake.log[0][1]["api_key"], FAKE_V3)
        self.assertNotIn("Authorization", fake.log[0][2])
        self.assertNotIn(FAKE_V3, repr(c))
        self.assertNotIn(FAKE_V4, repr(client(fake)))

    def test_api_base_pinned(self):
        for bad in ("https://evil.example/3", "http://api.themoviedb.org/3", "http://192.168.2.5:80/3",
                    "http://127.0.0.1:9/3?x=1", "http://u@127.0.0.1:9/3"):
            with self.assertRaises(T.Abort):
                T.check_base(bad)
        self.assertEqual(T.check_base("http://127.0.0.1:9/3"), "http://127.0.0.1:9/3")


class CliTests(unittest.TestCase):
    """Vrai sous-processus `python3 -I opentv_tmdb.py` contre un faux TMDb HTTP en boucle locale."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.path = os.path.join(self.d, "catalogue.json")
        B.atomic_write_json(self.path, catalogue([item("Dune", 2021, 1), item("Alice", 2010, 2)]))
        fake = FakeTmdb(search={"Dune": [movie(438631, "Dune", "2021-09-15", avg=7.8, votes=12000)]})
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                st, hdrs, body = fake("http://x" + self.path, {}, 1)
                self.send_response(st)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = "http://127.0.0.1:%d/3" % self.srv.server_address[1]

    def cli(self, *args, token=FAKE_V4):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMDB_API_BASE": self.base}
        if token:
            env["TMDB_READ_TOKEN"] = token
        return subprocess.run([sys.executable, "-I", SRC] + list(args) + ["--catalogue", self.path],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_enrich_status_rollback_never_print_secret(self):
        r = self.cli("enrich")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["matched"], out["unmatched"], out["wrote"]), (True, 1, 1, True))
        # Dune : 1 recherche ; Alice sans résultat : 2e langue (fr-CA) essayée, bornée
        self.assertEqual(self.seen, ["Bearer " + FAKE_V4] * 3)
        r2 = self.cli("status", token=None)
        self.assertEqual(json.loads(r2.stdout)["matched"], 1)
        r3 = self.cli("rollback", token=None)
        self.assertEqual(json.loads(r3.stdout)["restored"], True)
        for x in (r, r2, r3):
            self.assertNotIn(FAKE_V4, x.stdout + x.stderr)
            self.assertNotIn(FAKE_V4[:20], x.stdout + x.stderr)

    def test_missing_secret_and_no_secret_argument(self):
        r = self.cli("enrich", token=None)
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (T.EXIT_CONFIG, "tmdb_credential_missing"))
        r = subprocess.run([sys.executable, "-I", SRC, "enrich", "--catalogue", self.path, "--token", FAKE_V4],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)                        # aucune option de secret en ligne de commande
        for cmd in ("status", "rollback"):
            r = subprocess.run([sys.executable, "-I", SRC, cmd, "--catalogue", os.path.join(self.d, "absent.json")],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, T.EXIT_CONFIG if cmd == "status" else T.EXIT_VALIDATION, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertFalse(json.loads(r.stdout)["ok"])

    def test_wrapper_reads_token_from_stdin_only(self):
        sh = os.path.join(ROOT, "scripts", "nanopi", "tmdb-enrich.sh")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.d, "TMDB_API_BASE": self.base,
               "CATALOGUE_FILE": self.path, "PYTHON": sys.executable}
        r = subprocess.run(["bash", sh, "--dry-run"], input=FAKE_V4 + "\n", env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["dry_run"], True)
        self.assertNotIn(FAKE_V4, r.stdout + r.stderr)
        r = subprocess.run(["bash", sh, "status"], input="", env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, json.loads(r.stdout)["items"]), (0, 2))


class UiTests(unittest.TestCase):
    def test_ui_labels_tmdb_separately_with_notice(self):
        with open(os.path.join(ROOT, "scripts/nanopi/opentv_bridge_ui.html"), encoding="utf-8") as f:
            ui = f.read()
        self.assertIn('"TMDb "', ui)
        self.assertIn("tmdb_votes", ui)
        self.assertIn(T.TMDB_NOTICE, ui)
        self.assertIn("Information courtesy of IMDb", ui)
        self.assertIn('/ui/tmdb-logo.svg', ui)
        with open(os.path.join(ROOT, "scripts/nanopi/tmdb-logo.svg"), encoding="utf-8") as f:
            logo = f.read()
        self.assertIn("<svg", logo)
        self.assertNotIn("<script", logo.lower())


if __name__ == "__main__":
    unittest.main()
