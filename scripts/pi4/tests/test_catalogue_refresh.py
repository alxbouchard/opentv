"""Rafraîchissement quotidien du catalogue : succès avec enrichissements conservés, inchangé sans écriture, panne
réseau, candidat invalide, baisse/recouvrement suspects, verrou non bloquant, retour arrière atomique, catalogue
actuel dangereux, adresse non publique, CLI (root via l'unité), unités systemd et installation. Aucun hôte, préfixe,
jeton, URL ni titre dans les journaux et sorties."""
import configparser
import contextlib
import errno
import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from unittest import mock

from tests._common import NANOPI, PI4
from opentv_pi import catalogue as cat
from opentv_pi import cli, config, health
from opentv_pi import source as srcmod

HTML = {"content-type": "text/html; charset=UTF-8"}
HOST, PREFIX, TOKEN = "ancra.net", "pfxaa1", "ancra"
ANCHOR = "https://%s/" % HOST
OK_ENV = "OPENTV_SOURCE_APPROVED=1\nOPENTV_SOURCE_TRUST_ANCHOR=%s\n" % ANCHOR
SECRETS = (HOST, PREFIX, "https://", "/b/", "img.test", "tmdb.example", "Film ")
ENGINE = srcmod.load_engine(NANOPI)
REAL_GETEUID = os.geteuid


def card(sid, link, title, poster="https://img.test/p%s.jpg", year=2024, genre="Drame"):
    did = '<button data-id="%s">i</button>' % sid if sid is not None else ""
    return ('<a class="trend-card" href="/%s/b/%s/%s"><img src="%s" alt="%s">%s'
            '<div class="card-cat">Film · %s <span class="card-date">%d</span></div></a>'
            % (PREFIX, TOKEN, link, poster.replace("%s", str(link)), title, did, genre, year))


def page(cards):
    return ("<html><body>%s</body></html>" % "".join(cards)).encode()


def films(ids):
    """[(site_id, lien, titre)] : lien = site_id + 400 (identifiant opaque d'époque)."""
    return [(str(i), str(400 + i), "Film %d" % i) for i in ids]


class Site:
    """Racine de confiance -> préfixe -> page de liste ; `listing` remplaçable ; `fail` = tout en erreur réseau."""

    def __init__(self, listing):
        self.listing, self.fail, self.calls, self.on_listing = listing, False, [], None
        self.listing_status = 200

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.fail:
            raise ENGINE.FetchError("URLError")
        if url == ANCHOR:
            return 200, HTML, ('<html><a href="/%s">go</a></html>' % PREFIX).encode()
        if url == "https://%s/%s/" % (HOST, PREFIX):
            return 302, {"location": "https://%s/%s/home/%s" % (HOST, PREFIX, TOKEN)}, b""
        if url == "https://%s/%s/home/%s" % (HOST, PREFIX, TOKEN):
            if self.on_listing:
                self.on_listing()
            # 1re lecture = validation de l'époque par le résolveur ; la suivante = page de liste du rafraîchissement
            if self.listing_status != 200 and self.calls.count(url) > 1:
                return self.listing_status, HTML, b"err"
            return 200, HTML, self.listing
        return 404, HTML, b"nope"


def listing_of(rows, **kw):
    return page([card(sid, link, title, **kw) for sid, link, title in rows])


def enriched_item(sid, link, title):
    return {"detail_path": "b/%s/%s" % (TOKEN, link), "source_id": link, "site_id": sid, "source": HOST,
            "title": title, "year": 2024, "genre": "Drame", "description": "Résumé %s" % sid,
            "poster": "https://tmdb.example/w300/%s.jpg" % sid, "imdb_id": "tt%07d" % int(sid),
            "imdb_rating": 7.1, "imdb_votes": 1200, "match_status": "matched",
            "rating_updated_at": "2026-10-01T00:00:00Z", "tmdb_id": 1000 + int(sid), "tmdb_rating": 7.4,
            "tmdb_votes": 800, "tmdb_match_status": "matched", "tmdb_rating_updated_at": "2026-10-02T00:00:00Z",
            "tmdb_confidence": 0.98, "tmdb_evidence": ["imdb_id"], "tmdb_match_method": "find_imdb",
            "tmdb_match_key": "k%s" % sid, "tmdb_reason": None}


def old_doc(rows):
    items = [enriched_item(*r) for r in rows]
    return {"attribution": "Données TMDb (attribution)", "count": len(items), "generated_at": "2026-10-20T00:00:00Z",
            "source": HOST, "items": items}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otvcat", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.etc = os.path.join(self.d, "etc")
        os.mkdir(self.etc, 0o700)
        self.state = os.path.join(self.d, "state")
        os.mkdir(self.state, 0o700)
        self.src_env = os.path.join(self.etc, "source.env")
        self.write_source(OK_ENV)
        self.cfg = config.Config({"OPENTV_STATE_DIR": self.state, "OPENTV_FORYOU_ENGINE_DIR": NANOPI,
                                  "OPENTV_SOURCE_FILE": self.src_env, "OPENTV_CATALOGUE_MIN_ITEMS": "3"})
        self.catf = self.cfg.catalogue_file
        self.logs = []

    def write_source(self, text, mode=0o640):
        with open(self.src_env, "w") as f:
            f.write(text)
        os.chmod(self.src_env, mode)

    def seed(self, doc):
        data = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1).encode()
        with open(self.catf, "wb") as f:
            f.write(data)
        os.chmod(self.catf, 0o600)
        return data

    def bytes_(self, p=None):
        with open(p or self.catf, "rb") as f:
            return f.read()

    def run_refresh(self, site, **kw):
        res = cat.refresh(self.cfg, fetch=site, engine=ENGINE, log=lambda ev, **f: self.logs.append(dict(f, ev=ev)),
                          **kw)
        self.assert_no_leak(res)
        return res

    def assert_no_leak(self, res):
        text = json.dumps([res, self.logs], ensure_ascii=False)
        for s in SECRETS:
            self.assertNotIn(s, text)

    def leftovers(self):
        return [n for n in os.listdir(self.state) if n.endswith(".candidate") or n.startswith(".tmp")
                or ".tmp" in n]


# --------------------------------------------------------------------------- succès
class TestSuccess(Base):
    def test_update_preserves_enrichment_posters_and_top_level_fields(self):
        old = old_doc(films(range(1, 11)))
        old_bytes = self.seed(old)
        # 1 retiré (10), 1 nouveau (11) ; titre publié modifié pour 3 ; affiches du site sur la page de liste
        rows = films(range(1, 10)) + films([11])
        rows[2] = (rows[2][0], rows[2][1], "Film 3 (version longue)")
        site = Site(listing_of(rows))
        res = self.run_refresh(site)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"], "updated")
        self.assertEqual((res["count"], res["previous_count"], res["added"], res["removed"], res["retained"]),
                         (10, 10, 1, 1, 9))
        self.assertEqual(res["enriched_kept"], 9)
        new = json.loads(self.bytes_())
        self.assertEqual(new["count"], 10)
        self.assertEqual(new["attribution"], old["attribution"])           # champ de premier niveau conservé
        self.assertEqual(new["source"], HOST)
        by = {x["site_id"]: x for x in new["items"]}
        self.assertNotIn("10", by)
        for sid in map(str, range(1, 10)):
            prev = enriched_item(sid, str(400 + int(sid)), "Film %s" % sid)
            for k, v in prev.items():
                if k == "title":
                    continue
                self.assertEqual(by[sid][k], v, (sid, k))                    # IMDb, TMDb bruts, affiche TMDb…
        self.assertEqual(by["3"]["title"], "Film 3 (version longue)")     # titre publié par le site
        self.assertTrue(by["11"]["poster"].startswith("https://img.test/"))
        self.assertNotIn("imdb_rating", by["11"])
        # instantané = ancien catalogue octet pour octet ; fichiers 0600 ; aucun candidat restant
        self.assertEqual(self.bytes_(self.catf + ".prev"), old_bytes)
        for p in (self.catf, self.catf + ".prev", os.path.join(self.state, cat.STATUS_NAME)):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        self.assertEqual(self.leftovers(), [])
        # le moteur (accueil, « Regarder ») relit le nouveau catalogue sans rien écarter
        items, _, host = ENGINE.load_catalogue_meta(self.catf, ENGINE.Config({}).detail_re)
        self.assertEqual((len(items), host), (10, HOST))
        st = cat.status(self.cfg)
        self.assertEqual((st["catalogue"]["count"], st["snapshot"]["count"], st["refresh"]["result"]),
                         (10, 10, "updated"))
        self.assertEqual(st["refresh"]["last_success_at"], res["finished_at"])
        self.assert_no_leak(st)

    def test_second_run_is_unchanged_and_writes_nothing(self):
        site = Site(listing_of(films(range(1, 6))))
        self.assertEqual(self.run_refresh(site)["result"], "updated")    # premier remplissage
        before = os.stat(self.catf)
        data = self.bytes_()
        res = self.run_refresh(site)
        self.assertEqual((res["ok"], res["result"], res["count"]), (True, "unchanged", 5))
        after = os.stat(self.catf)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.bytes_(), data)

    def test_first_fill_without_catalogue(self):
        res = self.run_refresh(Site(listing_of(films(range(1, 5)))))
        self.assertEqual((res["result"], res["count"], res["previous_count"]), ("updated", 4, 0))
        self.assertFalse(os.path.exists(self.catf + ".prev"))             # aucun instantané d'un fichier absent

    def test_migration_keeps_enrichment_by_stable_site_id(self):
        self.seed(old_doc([(s, str(9000 + int(s)), t) for s, _, t in films(range(1, 6))]))   # ancienne époque
        res = self.run_refresh(Site(listing_of(films(range(1, 6)))))
        self.assertEqual((res["result"], res["retained"]), ("updated", 5))
        by = {x["site_id"]: x for x in json.loads(self.bytes_())["items"]}
        self.assertEqual(by["2"]["detail_path"], "b/%s/402" % TOKEN)     # chemin de la nouvelle époque
        self.assertEqual(by["2"]["tmdb_match_key"], "k2")

    def test_same_path_of_another_film_is_never_merged(self):
        doc = old_doc(films(range(1, 6)))
        doc["items"][0]["site_id"] = "77"                                 # même chemin, autre film
        self.seed(doc)
        res = self.run_refresh(Site(listing_of(films(range(1, 6)))))
        self.assertEqual(res["result"], "updated")
        by = {x["site_id"]: x for x in json.loads(self.bytes_())["items"]}
        self.assertNotIn("imdb_rating", by["1"])

    def test_stale_candidate_from_crash_is_removed(self):
        self.seed(old_doc(films(range(1, 6))))
        stale = os.path.join(self.state, ".catalogue.json.0123456789ab.candidate")
        with open(stale, "w") as f:
            f.write("{")
        self.run_refresh(Site(listing_of(films(range(1, 6)))))
        self.assertFalse(os.path.exists(stale))


# --------------------------------------------------------------------------- pannes : l'ancien catalogue reste intact
class TestFailuresKeepOldCatalogue(Base):
    def setUp(self):
        super().setUp()
        self.old = self.seed(old_doc(films(range(1, 11))))

    def assert_kept(self, res, code):
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["result"], code, res)
        self.assertEqual(self.bytes_(), self.old)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(cat.status(self.cfg)["refresh"]["result"], code)

    def test_network_failure(self):
        site = Site(listing_of(films(range(1, 11))))
        site.fail = True
        res = self.run_refresh(site)
        self.assert_kept(res, "discovery_failed")
        self.assertEqual(res["why"], "fetch:urlerror")

    def test_listing_http_error_and_empty_listing(self):
        site = Site(listing_of(films(range(1, 11))))
        site.listing_status = 503
        self.assert_kept(self.run_refresh(site), "listing_failed")
        site = Site(page(['<a href="/%s/b/%s/1">x</a>' % (PREFIX, TOKEN)]))     # validée, mais aucune carte titrée
        site.listing = page(['<a href="/%s/b/%s/401"></a>' % (PREFIX, TOKEN)])
        res = self.run_refresh(site)
        self.assertIn(res["result"], ("invalid_candidate", "too_few", "listing_empty"))
        self.assertEqual(self.bytes_(), self.old)

    def test_last_success_survives_a_failure(self):
        good = self.run_refresh(Site(listing_of(films(range(1, 11)))))
        self.assertEqual((good["ok"], good["result"]), (True, "unchanged"))   # même liste : aucune écriture
        self.old = self.bytes_()
        site = Site(b"")
        site.fail = True
        self.assert_kept(self.run_refresh(site), "discovery_failed")
        self.assertEqual(cat.status(self.cfg)["refresh"]["last_success_at"], good["finished_at"])

    def test_invalid_candidates(self):
        no_ids = films(range(1, 11))
        self.assert_kept(self.run_refresh(Site(page([card(None, l, t) for _, l, t in no_ids]))), "missing_site_ids")
        dup = films(range(1, 11))
        dup[1] = ("1", dup[1][1], dup[1][2])                              # même site_id pour deux liens
        self.assert_kept(self.run_refresh(Site(listing_of(dup))), "duplicate_ids")
        blank = films(range(1, 11)) + [("11", "411", "  ")]                # nouveau film sans titre
        self.assert_kept(self.run_refresh(Site(listing_of(blank))), "invalid_candidate")
        # titre publié vide pour un film connu : le titre accepté est conservé (rien à remplacer)
        kept = [(s, l, "  ") for s, l, _ in films(range(1, 11))]
        self.assertEqual(self.run_refresh(Site(listing_of(kept)))["result"], "unchanged")

    def test_count_drop_and_low_overlap_are_suspicious_truncation(self):
        self.assert_kept(self.run_refresh(Site(listing_of(films(range(1, 6))))), "count_drop")   # 10 -> 5
        self.assert_kept(self.run_refresh(Site(listing_of(films(range(50, 60))))), "low_overlap")  # autre liste
        ok = self.run_refresh(Site(listing_of(films(range(1, 9)))))       # 10 -> 8 : baisse de 20 % ≤ 25 %
        self.assertEqual(ok["result"], "updated")

    def test_min_and_max_bounds(self):
        self.cfg.catalogue_min_items = 20
        self.assert_kept(self.run_refresh(Site(listing_of(films(range(1, 12))))), "too_few")
        self.cfg.catalogue_min_items = 3
        with mock.patch.object(cat, "MAX_BYTES", 2048):                   # actuel démesuré : jamais remplacé
            res = self.run_refresh(Site(listing_of(films(range(1, 12)))))
        self.assert_kept(res, "current_unsafe")
        self.assertEqual(res["why"], "too_large")

    def test_too_large_candidate(self):
        size = len(cat.serialize(json.loads(self.old)))
        with mock.patch.object(cat, "MAX_BYTES", size + 200):              # actuel lisible, candidat trop gros
            rows = [(s, l, t + " " + "x" * 150) for s, l, t in films(range(1, 11))]
            self.assert_kept(self.run_refresh(Site(listing_of(rows))), "too_large")

    def test_replace_failure_is_atomic(self):
        real = os.replace

        def boom(*a, **k):
            if "src_dir_fd" in k:                 # renommage final du catalogue (le cache du résolveur passe)
                raise OSError(errno.EIO, "io")
            return real(*a, **k)
        with mock.patch.object(cat.os, "replace", side_effect=boom):
            res = self.run_refresh(Site(listing_of(films(range(1, 12)))))
        self.assertIs(os.replace, real)
        self.assert_kept(res, "write_failed")
        self.assertEqual(res["why"], "replace")

    def test_candidate_write_failure(self):
        real_write = os.write

        def full(fd, data):
            if len(data) > 1000:
                raise OSError(errno.ENOSPC, "full")
            return real_write(fd, data)
        with mock.patch.object(cat.os, "write", side_effect=full):
            res = self.run_refresh(Site(listing_of(films(range(1, 12)))))
        self.assert_kept(res, "write_failed")

    def test_catalogue_replaced_during_the_pass_wins(self):
        site = Site(listing_of(films(range(1, 12))))
        newer = old_doc(films(range(1, 13)))

        def admin_copy():
            self.seed(newer)
        site.on_listing = admin_copy
        res = self.run_refresh(site)
        self.assertEqual(res["result"], "catalogue_changed")
        self.assertEqual(json.loads(self.bytes_())["count"], 12)          # la copie manuelle plus récente reste
        self.assertEqual(self.leftovers(), [])


class TestUnsafeOrCorruptCurrent(Base):
    def test_symlink_and_fifo_are_refused_untouched(self):
        target = os.path.join(self.d, "elsewhere.json")
        with open(target, "w") as f:
            json.dump(old_doc(films(range(1, 6))), f)
        os.symlink(target, self.catf)
        res = self.run_refresh(Site(listing_of(films(range(1, 6)))))
        self.assertEqual(res["result"], "current_unsafe")
        self.assertTrue(os.path.islink(self.catf))
        os.unlink(self.catf)
        os.mkfifo(self.catf)
        res = self.run_refresh(Site(listing_of(films(range(1, 6)))))      # jamais bloqué sur une FIFO
        self.assertEqual(res["result"], "current_unsafe")
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.catf).st_mode))

    def test_hardlink_refused(self):
        self.seed(old_doc(films(range(1, 6))))
        os.link(self.catf, os.path.join(self.d, "hl"))
        self.assertEqual(self.run_refresh(Site(listing_of(films(range(1, 6)))))["result"], "current_unsafe")

    def test_corrupt_or_deeply_nested_current_is_replaced_without_snapshot(self):
        for bad in (b"{not json", b"[" * 200000 + b"]" * 200000, b'{"items": "x"}'):
            with open(self.catf, "wb") as f:
                f.write(bad)
            res = self.run_refresh(Site(listing_of(films(range(1, 6)))))
            self.assertEqual(res["result"], "updated", res)
            self.assertEqual(res["previous_count"], 0)
            self.assertFalse(os.path.exists(self.catf + ".prev"))
            os.unlink(self.catf)


# --------------------------------------------------------------------------- verrou, source, utilisateur
class TestLockAndGates(Base):
    def test_lock_overlap_is_nonblocking_busy_and_touches_nothing(self):
        old = self.seed(old_doc(films(range(1, 6))))
        lfd = os.open(self.catf + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lfd)
        fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        site = Site(listing_of(films(range(1, 7))))
        res = self.run_refresh(site)
        self.assertEqual((res["ok"], res["result"]), (False, "busy"))
        self.assertEqual(site.calls, [])                                  # aucune requête
        self.assertEqual(self.bytes_(), old)
        self.assertFalse(os.path.exists(os.path.join(self.state, cat.STATUS_NAME)))
        fcntl.flock(lfd, fcntl.LOCK_UN)
        self.assertEqual(self.run_refresh(site)["result"], "updated")

    def test_concurrent_processes_one_runs_one_busy(self):
        """Deux vrais processus : le second échoue tout de suite (75) pendant que le premier tient le verrou."""
        self.seed(old_doc(films(range(1, 6))))
        code = ("import sys, time, fcntl, os; sys.path.insert(0, %r)\n"
                "fd = os.open(%r, os.O_RDWR | os.O_CREAT, 0o600); fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('held', flush=True); time.sleep(3)\n") % (PI4, self.catf + ".lock")
        p = subprocess.Popen([os.environ.get("PYTHON", __import__("sys").executable), "-c", code],
                             stdout=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline().strip(), b"held")
            env = os.path.join(self.d, "opentv.env")
            with open(env, "w") as f:
                f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\nOPENTV_SOURCE_FILE=%s\n"
                        % (self.state, NANOPI, self.src_env))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code_ = cli.main(["--env", env, "catalogue", "refresh"])
            self.assertEqual(code_, cat.BUSY_EXIT)
            self.assertEqual(json.loads(out.getvalue())["result"], "busy")
        finally:
            p.wait(timeout=10)
            p.stdout.close()

    def test_source_gate(self):
        old = self.seed(old_doc(films(range(1, 6))))
        site = Site(listing_of(films(range(1, 6))))
        os.unlink(self.src_env)
        res = self.run_refresh(site)
        self.assertEqual((res["ok"], res["result"], res["why"]), (True, "skipped", "source_missing"))
        self.write_source("OPENTV_SOURCE_TRUST_ANCHOR=%s\n" % ANCHOR)
        self.assertEqual(self.run_refresh(site)["why"], "source_unapproved")
        self.write_source(OK_ENV, mode=0o644)                            # lisible par tous : refusé
        res = self.run_refresh(site)
        self.assertEqual((res["ok"], res["result"]), (False, "source_unsafe"))
        self.assertEqual(site.calls, [])
        self.assertEqual(self.bytes_(), old)

    def test_wrong_user_never_writes(self):
        self.seed(old_doc(films(range(1, 6))))
        with mock.patch.object(cat.os, "geteuid", return_value=os.geteuid() + 4242):
            self.assertEqual(cat.refresh(self.cfg, fetch=Site(b""), engine=ENGINE)["result"], "wrong_user")

    def test_rollback_restores_validated_snapshot(self):
        old = self.seed(old_doc(films(range(1, 11))))
        self.assertEqual(cat.refresh(self.cfg, engine=ENGINE, rollback=True)["result"], "no_snapshot")
        self.assertEqual(self.run_refresh(Site(listing_of(films(range(1, 12)))))["result"], "updated")
        res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((res["ok"], res["result"], res["count"], res["previous_count"], res["held"]),
                         (True, "rolled_back", 10, 11, True))
        self.assertEqual(json.loads(self.bytes_()), json.loads(old))
        self.assertEqual(self.leftovers(), [])


# --------------------------------------------------------------------------- adresse publique exigée
class TestPublicFetch(unittest.TestCase):
    def test_non_public_addresses_refused_before_any_connection(self):
        called = []
        with mock.patch.object(ENGINE, "http_fetch", side_effect=lambda u, t: called.append(u) or (200, {}, b"")):
            for addrs in (["192.168.2.14"], ["127.0.0.1"], ["10.0.0.5", "93.184.216.34"], ["169.254.1.1"],
                          ["100.64.0.1"], ["::1"], ["fd00::1"], []):
                f = cat.public_fetch(ENGINE, resolve=lambda h, t, a=addrs: a, local=lambda: set())
                with self.assertRaises(ENGINE.FetchError):
                    f("https://ancra.net/", 3)
            self.assertEqual(called, [])
            f = cat.public_fetch(ENGINE, resolve=lambda h, t: ["93.184.216.34"], local=lambda: {"93.184.216.34"})
            with self.assertRaises(ENGINE.FetchError):                    # la boîte elle-même
                f("https://ancra.net/", 3)
            f = cat.public_fetch(ENGINE, resolve=lambda h, t: ["93.184.216.34"], local=lambda: set())
            f("https://ancra.net/", 3)
            self.assertEqual(called, ["https://ancra.net/"])


# --------------------------------------------------------------------------- CLI
class TestCli(Base):
    def env(self):
        p = os.path.join(self.d, "opentv.env")
        with open(p, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\nOPENTV_SOURCE_FILE=%s\n"
                    "OPENTV_CATALOGUE_MIN_ITEMS=3\n" % (self.state, NANOPI, self.src_env))
        return p

    def call(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        text = out.getvalue()
        for s in SECRETS:
            self.assertNotIn(s, text)
        return code, text

    def test_service_user_refresh_and_status(self):
        env = self.env()
        self.seed(old_doc(films(range(1, 6))))
        site = Site(listing_of(films(range(1, 7))))
        with mock.patch.object(cat, "public_fetch", return_value=site), \
                mock.patch.object(srcmod, "load_engine", return_value=ENGINE):
            code, text = self.call(["--env", env, "catalogue", "refresh"])
        self.assertEqual(code, 0, text)
        lines = [json.loads(l) for l in text.splitlines()]
        self.assertEqual((lines[-1]["event"], lines[-1]["result"], lines[-1]["count"]),
                         ("catalogue_refresh", "updated", 6))
        code, text = self.call(["catalogue", "status", "--env", env])
        self.assertEqual(code, 0)
        st = json.loads(text)
        self.assertEqual((st["catalogue"]["count"], st["refresh"]["result"]), (6, "updated"))
        site.fail = True
        with mock.patch.object(cat, "public_fetch", return_value=site), \
                mock.patch.object(srcmod, "load_engine", return_value=ENGINE):
            code, text = self.call(["--env", env, "catalogue", "refresh"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text.splitlines()[-1])["result"], "discovery_failed")

    def test_root_runs_the_sandboxed_unit_and_never_writes_itself(self):
        env = self.env()
        old = self.seed(old_doc(films(range(1, 6))))
        calls = []

        def runner(argv, timeout):
            calls.append((list(argv), timeout))
            if argv[:2] == ["systemctl", "start"]:   # l'unité (compte opentv, pas root) fait le passage
                with mock.patch.object(os, "geteuid", REAL_GETEUID):
                    cat.refresh(self.cfg, fetch=Site(listing_of(films(range(1, 7)))), engine=ENGINE)
            return 0, ""
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", side_effect=runner):
            code, text = self.call(["--env", env, "catalogue", "refresh"])
        self.assertEqual(code, 0, text)
        self.assertEqual(calls[0][0], ["systemctl", "start", "opentv-catalogue-refresh.service"])
        self.assertGreater(calls[0][1], 300)
        rep = json.loads(text)
        self.assertEqual((rep["ok"], rep["refresh"]["result"]), (True, "updated"))
        self.assertNotEqual(self.bytes_(), old)

    def test_root_busy_and_not_run(self):
        env = self.env()

        def runner(argv, timeout):
            if argv[:2] == ["systemctl", "show"]:
                return 0, "75\n"
            return 0, ""
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", side_effect=runner):
            code, text = self.call(["--env", env, "catalogue", "refresh"])
            self.assertEqual((code, json.loads(text)["result"]), (cat.BUSY_EXIT, "busy"))
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", return_value=(0, "0\n")):
            code, text = self.call(["--env", env, "catalogue", "refresh"])
        self.assertEqual((code, json.loads(text)["result"]), (1, "not_run"))

    def test_subcommand_env_does_not_override_global_env(self):
        env = self.env()
        a = cli.build_parser().parse_args(["--env", env, "catalogue", "status"])
        self.assertEqual(a.env, env)


# --------------------------------------------------------------------------- configuration, unités, installation
def unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None, empty_lines_in_values=False)
    cp.optionxform = str
    with open(os.path.join(PI4, "systemd", name), encoding="utf-8") as f:
        text = f.read()
    cp.read_string(text)
    return cp, text


class TestConfigUnitsInstall(unittest.TestCase):
    def test_config_bounds_and_documented_defaults(self):
        c = config.Config({})
        self.assertEqual((c.catalogue_min_items, c.catalogue_max_drop_pct), (20, 25))
        for k, v in (("OPENTV_CATALOGUE_MIN_ITEMS", "0"), ("OPENTV_CATALOGUE_MIN_ITEMS", "2001"),
                     ("OPENTV_CATALOGUE_MIN_ITEMS", "x"), ("OPENTV_CATALOGUE_MAX_DROP_PCT", "91"),
                     ("OPENTV_CATALOGUE_MAX_DROP_PCT", "-1"), ("OPENTV_CATALOGUE_MAX_DROP_PCT", "1.5")):
            with self.assertRaises(ValueError, msg=(k, v)):
                config.Config({k: v})
        with open(os.path.join(PI4, "opentv-pi.env.example")) as f:
            text = f.read()
        for k in ("OPENTV_CATALOGUE_MIN_ITEMS", "OPENTV_CATALOGUE_MAX_DROP_PCT"):
            self.assertIn("#%s=%s" % (k, config.DEFAULTS[k]), text)

    def test_service_is_unprivileged_sandboxed_and_never_restarts_anything(self):
        cp, text = unit("opentv-catalogue-refresh.service")
        s, u = cp["Service"], cp["Unit"]
        self.assertEqual((s["Type"], s["User"], s["Group"]), ("oneshot", "opentv", "opentv"))
        self.assertEqual(s["ExecStart"], "/usr/bin/python3 -I /opt/opentv/bin/opentv-pi catalogue refresh")
        self.assertEqual((s["CapabilityBoundingSet"], s["AmbientCapabilities"], s["NoNewPrivileges"]), ("", "", "yes"))
        self.assertEqual((s["ProtectSystem"], s["ProtectHome"], s["PrivateTmp"], s["PrivateDevices"]),
                         ("strict", "yes", "yes", "yes"))
        self.assertEqual(re.findall(r"^ReadWritePaths=(.+)$", text, re.M), ["-/var/lib/opentv"])
        self.assertNotIn("SupplementaryGroups", s)                        # ni opentv-agent, ni video
        self.assertNotIn("PrivateNetwork", s)                              # réseau requis (HTTPS sortant)
        self.assertEqual(set(s["RestrictAddressFamilies"].split()), {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"})
        self.assertIn("localhost", s["IPAddressDeny"].split())
        self.assertIn("link-local", s["IPAddressDeny"].split())
        self.assertEqual((s["SystemCallFilter"], s["SystemCallArchitectures"]), ("@system-service", "native"))
        self.assertEqual(s["SuccessExitStatus"], str(cat.BUSY_EXIT))
        self.assertLess(int(s["TimeoutStartSec"]), cli.CATALOGUE_UNIT_TIMEOUT_S)
        self.assertEqual((s["Nice"], s["IOSchedulingClass"]), ("10", "idle"))
        self.assertIn("MemoryMax", s)
        self.assertEqual(u["ConditionPathExists"], "/etc/opentv/source.env")
        self.assertIn("network-online.target", u["After"])
        inacc = " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()
        for p in ("-/var/lib/opentv/agents.json", "-/var/lib/opentv/netjobs", "-/etc/opentv/agents",
                  "-/etc/NetworkManager/system-connections", "-/var/lib/opentv/foryou"):
            self.assertIn(p, inacc)
        for bad in ("Restart=", "RemoveIPC", "ExecStartPost", "ExecStartPre", "ExecStop", "systemctl", "[Install]",
                    "StateDirectory", "DynamicUser"):
            self.assertNotIn(bad, text)                                   # jamais un redémarrage d'autre service

    def test_timer_daily_persistent_bounded_random_delay(self):
        cp, text = unit("opentv-catalogue-refresh.timer")
        t = cp["Timer"]
        self.assertRegex(t["OnCalendar"], r"^\*-\*-\* \d\d:\d\d:\d\d$")    # une fois par jour
        self.assertEqual((t["Persistent"], t["Unit"]), ("true", "opentv-catalogue-refresh.service"))
        m = re.fullmatch(r"(\d+)min", t["RandomizedDelaySec"])
        self.assertTrue(m and 0 < int(m.group(1)) <= 120, t["RandomizedDelaySec"])
        self.assertNotIn("OnUnitActiveSec", t)
        self.assertNotIn("OnBootSec", t)
        self.assertEqual(cp["Install"]["WantedBy"], "timers.target")
        self.assertIn("opentv-catalogue-refresh.timer", health.UNITS_OPTIONAL)
        self.assertNotIn("opentv-catalogue-refresh.timer", health.UNITS_REQUIRED)

    def sh(self, *args):
        env = dict(os.environ, LC_ALL="C")
        return subprocess.run(["bash"] + list(args), capture_output=True, timeout=120, env=env)

    def test_install_enables_timer_only_and_uninstall_disables_both(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        p = self.sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
        self.assertEqual(p.returncode, 0, p.stdout.decode() + p.stderr.decode())
        out = p.stdout.decode()
        for u in ("opentv-catalogue-refresh.service", "opentv-catalogue-refresh.timer"):
            self.assertTrue(os.path.isfile(os.path.join(r, "etc/systemd/system", u)), u)
        with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
            man = f.read()
        self.assertIn("/etc/systemd/system/opentv-catalogue-refresh.timer", man)
        self.assertIn("/etc/systemd/system/opentv-catalogue-refresh.service", man)
        en = [l for l in out.splitlines() if "systemctl enable" in l][0]
        rs = [l for l in out.splitlines() if "systemctl restart" in l][0]
        self.assertIn("opentv-catalogue-refresh.timer", en)
        self.assertIn("opentv-catalogue-refresh.timer", rs)
        self.assertNotIn("opentv-catalogue-refresh.service", en + rs)     # jamais de passage à l'installation
        self.assertTrue(health.verify_manifest(r)["ok"])
        q = self.sh(os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r)
        self.assertEqual(q.returncode, 0, q.stderr)
        dis = [l for l in q.stdout.decode().splitlines() if "disable --now" in l][0]
        self.assertIn("opentv-catalogue-refresh.timer", dis)
        self.assertIn("opentv-catalogue-refresh.service", dis)
        for u in ("opentv-catalogue-refresh.service", "opentv-catalogue-refresh.timer"):
            self.assertFalse(os.path.exists(os.path.join(r, "etc/systemd/system", u)), u)


# --------------------------------------------------------------------------- suspension après rollback / reprise
class HoldBase(Base):
    def setUp(self):
        super().setUp()
        self.old = self.seed(old_doc(films(range(1, 11))))
        self.hold = self.catf + cat.HOLD_SUFFIX

    def update_then_rollback(self):
        up = self.run_refresh(Site(listing_of(films(range(1, 12)))))
        self.assertEqual(up["result"], "updated")
        rb = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((rb["ok"], rb["result"], rb["held"]), (True, "rolled_back", True), rb)
        self.assertEqual(self.bytes_(), self.old)            # ancien catalogue octet pour octet (instantané)
        return rb

    def frozen(self):
        st = os.stat(self.catf)
        return self.bytes_(), st.st_ino, st.st_mtime_ns, st.st_size

    def refresh_must_not_touch_network(self, code, ok):
        """Passage pendant la suspension : aucune requête, aucun moteur chargé, catalogue et instantané intacts."""
        prevf = self.catf + ".prev"
        before = self.frozen()
        prev = self.bytes_(prevf) if os.path.exists(prevf) else None
        site = Site(listing_of(films(range(1, 13))))
        with mock.patch.object(srcmod, "load_engine", side_effect=AssertionError("moteur chargé")), \
                mock.patch.object(srcmod, "Source", side_effect=AssertionError("source ouverte")):
            res = cat.refresh(self.cfg, fetch=site, log=lambda ev, **f: self.logs.append(dict(f, ev=ev)))
        self.assert_no_leak(res)
        self.assertEqual((res["ok"], res["result"]), (ok, code), res)
        self.assertEqual(site.calls, [])
        self.assertEqual(self.frozen(), before)
        self.assertEqual(self.bytes_(prevf) if os.path.exists(prevf) else None, prev)
        self.assertEqual(self.leftovers(), [])
        return res


class TestHoldResume(HoldBase):
    def test_update_rollback_then_refresh_is_held_byte_identical_no_network(self):
        self.update_then_rollback()
        st = os.lstat(self.hold)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_nlink), (0o600, os.geteuid(), 1))
        good = cat.status(self.cfg)["refresh"]["last_success_at"]
        for _ in range(3):                                   # minuterie quotidienne, plusieurs jours de suite
            res = self.refresh_must_not_touch_network("held", True)
            self.assertTrue(res["held"])
        st = cat.status(self.cfg)
        self.assertEqual((st["hold"]["held"], st["hold"]["safe"], st["refresh"]["result"]), (True, True, "held"))
        self.assertEqual(st["refresh"]["last_success_at"], good)   # « held » n'est jamais un succès de mise à jour
        self.assert_no_leak(st)

    def test_resume_then_refresh_updates_again(self):
        self.update_then_rollback()
        self.refresh_must_not_touch_network("held", True)
        res = cat.resume(self.cfg)
        self.assertEqual((res["ok"], res["result"], res["held"]), (True, "resumed", False))
        self.assertFalse(os.path.lexists(self.hold))
        self.assertFalse(cat.status(self.cfg)["hold"]["held"])
        site = Site(listing_of(films(range(1, 12))))
        up = self.run_refresh(site)
        self.assertTrue(site.calls)
        self.assertEqual((up["ok"], up["result"], up["count"]), (True, "updated", 11))
        self.assertEqual(cat.resume(self.cfg)["result"], "not_held")   # reprise idempotente

    def test_second_rollback_while_held_keeps_hold(self):
        self.update_then_rollback()
        res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((res["result"], res["held"]), ("rolled_back", True))
        self.assertTrue(os.path.isfile(self.hold))
        self.assertEqual(self.bytes_(), self.old)

    def test_busy_lock_blocks_resume_and_rollback_without_side_effects(self):
        self.update_then_rollback()
        lfd = os.open(self.catf + ".lock", os.O_RDWR)
        self.addCleanup(os.close, lfd)
        fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(cat.resume(self.cfg)["result"], "busy")
        self.assertEqual(cat.refresh(self.cfg, engine=ENGINE, rollback=True)["result"], "busy")
        self.assertTrue(os.path.isfile(self.hold))
        self.assertEqual(self.bytes_(), self.old)

    def test_wrong_user_cannot_resume(self):
        self.update_then_rollback()
        with mock.patch.object(cat.os, "geteuid", return_value=os.geteuid() + 4242):
            self.assertEqual(cat.resume(self.cfg)["result"], "wrong_user")
        self.assertTrue(os.path.isfile(self.hold))


class TestUnsafeHoldFailsClosed(HoldBase):
    """Tout nom `catalogue.json.hold` anormal = suspension (hold_unsafe), aucune requête ; resume retire le NOM."""

    def outside(self, data=b'{"v": 1, "reason": "rollback", "held_at": 1.0}'):
        p = os.path.join(self.d, "outside-hold")
        with open(p, "wb") as f:
            f.write(data)
        return p

    def check_unsafe(self, why):
        res = self.refresh_must_not_touch_network("hold_unsafe", False)
        self.assertEqual(res["why"], why)
        h = cat.status(self.cfg)["hold"]
        self.assertEqual((h["held"], h["safe"]), (True, False))
        self.assertEqual(cat.status(self.cfg)["refresh"]["result"], "hold_unsafe")

    def test_symlink_to_valid_marker_and_dangling(self):
        target = self.outside()
        os.symlink(target, self.hold)
        self.check_unsafe("open")
        self.assertEqual(cat.resume(self.cfg)["result"], "resumed")    # le lien est retiré, jamais sa cible
        self.assertFalse(os.path.lexists(self.hold))
        self.assertTrue(os.path.isfile(target))
        os.symlink(os.path.join(self.d, "nowhere"), self.hold)
        self.check_unsafe("open")

    def test_fifo_never_blocks(self):
        os.mkfifo(self.hold)
        self.check_unsafe("not_regular")
        self.assertEqual(cat.resume(self.cfg)["result"], "resumed")
        self.assertFalse(os.path.lexists(self.hold))

    def test_hardlink(self):
        target = self.outside()
        os.link(target, self.hold)
        self.check_unsafe("not_regular")
        self.assertEqual(cat.resume(self.cfg)["result"], "resumed")
        self.assertTrue(os.path.isfile(target))

    def test_directory_is_never_removed_by_resume(self):
        os.mkdir(self.hold)
        with open(os.path.join(self.hold, "keep"), "w") as f:
            f.write("x")
        self.check_unsafe("not_regular")
        res = cat.resume(self.cfg)
        self.assertEqual((res["ok"], res["result"], res["why"]), (False, "hold_unsafe", "directory"))
        self.assertTrue(os.path.isfile(os.path.join(self.hold, "keep")))
        self.refresh_must_not_touch_network("hold_unsafe", False)

    def test_invalid_contents(self):
        for data, why in ((b"", "invalid"), (b"{not json", "invalid"), (b"[]", "invalid"),
                          (b'{"v": 2, "reason": "rollback", "held_at": 1}', "invalid"),
                          (b'{"v": 1, "reason": "manual", "held_at": 1}', "invalid"),
                          (b'{"v": 1, "reason": "rollback", "held_at": true}', "invalid"),
                          (b"[" * 100000 + b"]" * 100000, "too_large"),
                          (b"[" * 2000 + b"]" * 2000, "invalid"),
                          (b'{"v": 1, "reason": "rollback", "held_at": 1, "x": "' + b"a" * 5000 + b'"}', "too_large")):
            with open(self.hold, "wb") as f:
                f.write(data)
            os.chmod(self.hold, 0o600)
            self.check_unsafe(why)
            os.unlink(self.hold)

    def test_foreign_owner_marker_refused(self):
        marker = b'{"v": 1, "reason": "rollback", "held_at": 1}'
        with open(self.hold, "wb") as f:
            f.write(marker)
        dfd = cat.fsutil.open_dir(self.state)
        self.addCleanup(os.close, dfd)
        self.assertIsNotNone(cat._hold_state(dfd, "catalogue.json"))
        with self.assertRaises(cat.Refused) as cm:                       # root (ou autre compte) : refusé
            cat._hold_state(dfd, "catalogue.json", owner=os.geteuid() + 1)
        self.assertEqual((cm.exception.code, cm.exception.why), ("hold_unsafe", "owner"))
        real_fstat = os.fstat

        def fstat(fd):                                                  # marqueur d'un autre propriétaire
            st = real_fstat(fd)
            if stat.S_ISREG(st.st_mode) and st.st_size == len(marker):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0) + tuple(st)[5:])
            return st
        with mock.patch.object(cat.os, "fstat", side_effect=fstat):
            res = cat.refresh(self.cfg, fetch=Site(b""), engine=ENGINE)
        self.assertEqual((res["result"], res["why"]), ("hold_unsafe", "owner"))


class TestHoldInterruptionsAndErrors(HoldBase):
    def test_hold_write_failure_aborts_rollback_untouched(self):
        self.run_refresh(Site(listing_of(films(range(1, 12)))))
        newer = self.bytes_()
        real = cat.fsutil.atomic_write

        def boom(dfd, name, data, **k):
            if name.endswith(cat.HOLD_SUFFIX):
                raise OSError(errno.ENOSPC, "full")
            return real(dfd, name, data, **k)
        with mock.patch.object(cat.fsutil, "atomic_write", side_effect=boom):
            res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((res["ok"], res["result"], res["why"]), (False, "write_failed", "hold"))
        self.assertEqual(self.bytes_(), newer)
        self.assertFalse(os.path.lexists(self.hold))
        self.assertEqual(self.leftovers(), [])

    def test_commit_failure_after_hold_restores_previous_hold_state(self):
        self.run_refresh(Site(listing_of(films(range(1, 12)))))
        newer = self.bytes_()
        real = os.replace

        def boom(*a, **k):
            if "src_dir_fd" in k:
                raise OSError(errno.EIO, "io")
            return real(*a, **k)
        with mock.patch.object(cat.os, "replace", side_effect=boom):
            res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((res["result"], res["why"]), ("write_failed", "replace"))
        self.assertEqual(self.bytes_(), newer)
        self.assertFalse(os.path.lexists(self.hold))                      # aucun témoin avant : retiré
        self.assertEqual(self.leftovers(), [])
        site = Site(listing_of(films(range(1, 12))))
        self.assertEqual(self.run_refresh(site)["result"], "unchanged")   # la minuterie continue normalement
        # déjà suspendu avant ce rollback raté : la suspension reste
        with open(self.hold, "w") as f:
            f.write('{"v": 1, "reason": "rollback", "held_at": 1}')
        os.chmod(self.hold, 0o600)
        with mock.patch.object(cat.os, "replace", side_effect=boom):
            cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertTrue(os.path.isfile(self.hold))

    def test_hold_removal_failure_after_failed_rollback_stays_held(self):
        self.run_refresh(Site(listing_of(films(range(1, 12)))))
        newer = self.bytes_()
        real_replace, real_unlink = os.replace, os.unlink

        def boom(*a, **k):
            if "src_dir_fd" in k:
                raise OSError(errno.EIO, "io")
            return real_replace(*a, **k)

        def unlink(p, *a, **k):
            if p == "catalogue.json" + cat.HOLD_SUFFIX:
                raise OSError(errno.EIO, "io")
            return real_unlink(p, *a, **k)
        with mock.patch.object(cat.os, "replace", side_effect=boom), \
                mock.patch.object(cat.os, "unlink", side_effect=unlink):
            res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual(res["result"], "write_failed")
        self.assertEqual(self.bytes_(), newer)
        self.assertTrue(os.path.isfile(self.hold))                         # fail-closed : suspendu
        self.refresh_must_not_touch_network("held", True)

    def test_crash_between_hold_and_replace_keeps_timer_away(self):
        """SIGKILL réel après la pose du témoin, avant le renommage : au redémarrage, la minuterie ne réécrase rien."""
        self.run_refresh(Site(listing_of(films(range(1, 12)))))
        newer = self.bytes_()
        env = os.path.join(self.d, "opentv.env")
        with open(env, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\nOPENTV_SOURCE_FILE=%s\n"
                    "OPENTV_CATALOGUE_MIN_ITEMS=3\n" % (self.state, NANOPI, self.src_env))
        code = textwrap.dedent("""
            import os, signal, sys
            sys.path.insert(0, %r)
            from opentv_pi import catalogue as cat, config
            real = cat._commit
            def crash(*a, **k):
                os.kill(os.getpid(), signal.SIGKILL)
            cat._commit = crash
            cat.refresh(config.Config.load(%r), rollback=True)
        """) % (PI4, env)
        p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL, p.stderr)
        self.assertTrue(os.path.isfile(self.hold))
        self.assertEqual(self.bytes_(), newer)
        self.refresh_must_not_touch_network("held", True)
        rb = cat.refresh(self.cfg, engine=ENGINE, rollback=True)          # l'opérateur relance : terminé
        self.assertEqual(rb["result"], "rolled_back")
        self.assertEqual(self.bytes_(), self.old)

    def test_crash_mid_atomic_write_tmp_are_removed_under_lock_only_ours(self):
        self.update_then_rollback()
        ours = [".catalogue.json.hold.0123456789ab.tmp", ".catalogue.json.prev.abcdef012345.tmp",
                ".catalogue-refresh.json.00112233aabb.tmp", ".catalogue.json.fedcba987654.candidate"]
        foreign = [".device.json.0123456789ab.tmp", ".agents.json.0123456789ab.tmp", "catalogue.json.hold.tmp",
                   ".catalogue.json.hold.XYZ.tmp"]
        for n in ours + foreign:
            with open(os.path.join(self.state, n), "w") as f:
                f.write("{")
        os.symlink(os.path.join(self.d, "elsewhere"), os.path.join(self.state, ".catalogue.json.prev.aaaaaaaaaaaa.tmp"))
        res = cat.resume(self.cfg)
        self.assertEqual(res["result"], "resumed")
        left = set(os.listdir(self.state))
        for n in ours + [".catalogue.json.prev.aaaaaaaaaaaa.tmp"]:
            self.assertNotIn(n, left)
        for n in foreign:
            self.assertIn(n, left)                                         # jamais un fichier d'un autre module

    def test_cleanup_listdir_error_is_sanitized(self):
        self.update_then_rollback()
        real = os.listdir

        def boom(p=None):
            if isinstance(p, int):
                raise OSError(errno.EIO, "/var/lib/opentv secret path")
            return real(p)
        for action in ("refresh", "resume", "rollback"):
            site = Site(listing_of(films(range(1, 12))))
            with mock.patch.object(cat.os, "listdir", side_effect=boom):
                res = cat.run(self.cfg, action, fetch=site, engine=ENGINE)
            self.assertEqual((res["ok"], res["result"], res["why"]), (False, "cleanup_failed", "listdir"), action)
            self.assertNotIn("secret", json.dumps(res))
            self.assertEqual(site.calls, [])
            self.assertTrue(os.path.isfile(self.hold))
            self.assertEqual(self.bytes_(), self.old)

    def test_resume_unlink_error_and_fsync_error(self):
        self.update_then_rollback()
        real = os.unlink

        def unlink(p, *a, **k):
            if p == "catalogue.json" + cat.HOLD_SUFFIX:
                raise OSError(errno.EROFS, "ro")
            return real(p, *a, **k)
        with mock.patch.object(cat.os, "unlink", side_effect=unlink):
            res = cat.resume(self.cfg)
        self.assertEqual((res["ok"], res["result"], res["why"]), (False, "write_failed", "hold_remove"))
        self.assertTrue(os.path.isfile(self.hold))
        self.refresh_must_not_touch_network("held", True)
        real_fsync = os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "io")
            return real_fsync(fd)
        with mock.patch.object(cat.os, "fsync", side_effect=fsync):
            res = cat.resume(self.cfg)
        self.assertEqual((res["ok"], res["result"], res["durable"]), (True, "resumed", False))
        self.assertFalse(os.path.lexists(self.hold))


class TestInvalidSnapshot(HoldBase):
    """Instantané invalide : rollback refusé, catalogue intact, AUCUNE suspension posée, aucun reste."""

    def write_prev(self, data):
        p = self.catf + ".prev"
        if os.path.lexists(p):
            os.unlink(p)
        with open(p, "wb") as f:
            f.write(data)
        os.chmod(p, 0o600)

    def assert_refused(self, code):
        before = self.frozen()
        res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual((res["ok"], res["result"]), (False, code), res)
        self.assertEqual(self.frozen(), before)
        self.assertFalse(os.path.lexists(self.hold))
        self.assertEqual(self.leftovers(), [])
        self.assert_no_leak(res)
        return res

    def test_missing_snapshot(self):
        self.assert_refused("no_snapshot")

    def test_corrupt_truncated_and_inconsistent_snapshots(self):
        good = cat.serialize(old_doc(films(range(1, 6))))
        doc = old_doc(films(range(1, 6)))
        doc["count"] = 99
        no_sid = old_doc(films(range(1, 6)))
        no_sid["items"][0].pop("site_id")
        dup = old_doc(films(range(1, 6)))
        dup["items"][1]["site_id"] = "1"
        cases = ((b"{not json", "invalid_candidate"), (good[:len(good) // 2], "invalid_candidate"),
                 (b"[" * 100000 + b"]" * 100000, "invalid_candidate"), (cat.serialize(doc), "invalid_candidate"),
                 (cat.serialize(no_sid), "missing_site_ids"), (cat.serialize(dup), "duplicate_ids"),
                 (cat.serialize(old_doc(films(range(1, 3)))), "too_few"))
        for data, code in cases:
            self.write_prev(data)
            self.assert_refused(code)

    def test_unsafe_snapshot_files(self):
        target = os.path.join(self.d, "elsewhere.json")
        with open(target, "wb") as f:
            f.write(cat.serialize(old_doc(films(range(1, 6)))))
        os.symlink(target, self.catf + ".prev")
        self.assert_refused("current_unsafe")
        os.unlink(self.catf + ".prev")
        os.mkfifo(self.catf + ".prev")
        self.assert_refused("current_unsafe")
        os.unlink(self.catf + ".prev")
        os.link(target, self.catf + ".prev")
        self.assert_refused("current_unsafe")

    def test_invalid_snapshot_while_held_keeps_hold(self):
        self.update_then_rollback()
        self.write_prev(b"{broken")
        before = self.frozen()
        res = cat.refresh(self.cfg, engine=ENGINE, rollback=True)
        self.assertEqual(res["result"], "invalid_candidate")
        self.assertEqual(self.frozen(), before)
        self.assertTrue(os.path.isfile(self.hold))                         # suspension antérieure conservée


class TestRecheckBeforeReplace(Base):
    def test_copy_during_snapshot_write_wins(self):
        self.seed(old_doc(films(range(1, 11))))
        newer = old_doc(films(range(1, 13)))
        real = cat.fsutil.atomic_write

        def snap(dfd, name, data, **k):
            out = real(dfd, name, data, **k)
            if name.endswith(".prev"):
                self.seed(newer)                                           # copie manuelle pendant l'instantané
            return out
        with mock.patch.object(cat.fsutil, "atomic_write", side_effect=snap):
            res = self.run_refresh(Site(listing_of(films(range(1, 12)))))
        self.assertEqual(res["result"], "catalogue_changed")
        self.assertEqual(json.loads(self.bytes_())["count"], 12)
        self.assertEqual(self.leftovers(), [])


class TestStatusFields(Base):
    def test_shape_ok_is_honest_and_hold_reported(self):
        self.seed(old_doc(films(range(1, 6))))
        st = cat.status(self.cfg)
        self.assertNotIn("valid", st["catalogue"])
        self.assertTrue(st["catalogue"]["shape_ok"])
        self.assertEqual(st["hold"], {"held": False})
        with open(self.catf, "w") as f:
            f.write('{"count": 3, "items": []}')
        self.assertFalse(cat.status(self.cfg)["catalogue"]["shape_ok"])
        with open(os.path.join(PI4, "README.md"), encoding="utf-8") as f:
            text = f.read()
        for needle in ("catalogue resume", "catalogue.json.hold", "hold_unsafe", "runuser"):
            self.assertIn(needle, text)


# --------------------------------------------------------------------------- root : délégation au compte du service
class TestRootDelegation(HoldBase):
    def env(self):
        p = os.path.join(self.d, "opentv.env")
        with open(p, "w") as f:
            f.write("OPENTV_STATE_DIR=%s\nOPENTV_FORYOU_ENGINE_DIR=%s\nOPENTV_SOURCE_FILE=%s\n"
                    "OPENTV_CATALOGUE_MIN_ITEMS=3\n" % (self.state, NANOPI, self.src_env))
        return p

    def call(self, argv, runner, uid=4321):
        out = io.StringIO()
        pw = mock.Mock(pw_uid=uid)
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", side_effect=runner), \
                mock.patch("pwd.getpwnam", return_value=pw), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        text = out.getvalue()
        for sec in SECRETS:
            self.assertNotIn(sec, text)
        return code, json.loads(text)

    def child(self, calls):
        """L'action déléguée exécutée « comme opentv » (vrai euid du test), sortie réelle de la CLI du compte."""
        def runner(argv, timeout):
            calls.append((list(argv), timeout))
            out = io.StringIO()
            with mock.patch.object(os, "geteuid", REAL_GETEUID), contextlib.redirect_stdout(out), \
                    mock.patch.object(srcmod, "load_engine", return_value=ENGINE):
                rc = cli.main(argv[argv.index("--env"):])
            return rc, out.getvalue()
        return runner

    def test_rollback_and_resume_delegate_fixed_argv_to_service_user(self):
        env = self.env()
        self.run_refresh(Site(listing_of(films(range(1, 12)))))
        calls = []
        code, rep = self.call(["--env", env, "catalogue", "rollback"], self.child(calls))
        self.assertEqual((code, rep["ok"], rep["result"], rep["held"], rep["delegated_to"]),
                         (0, True, "rolled_back", True, "opentv"))
        self.assertEqual(calls[0][0], ["/usr/sbin/runuser", "-u", "opentv", "--", "/usr/bin/python3", "-I",
                                       "/opt/opentv/bin/opentv-pi", "--env", env, "catalogue", "rollback"])
        self.assertLessEqual(calls[0][1], 120)
        self.assertEqual(self.bytes_(), self.old)
        self.assertEqual(os.stat(self.hold).st_uid, REAL_GETEUID())
        code, rep = self.call(["--env", env, "catalogue", "resume"], self.child(calls))
        self.assertEqual((code, rep["result"]), (0, "resumed"))
        self.assertEqual(calls[1][0][-1], "resume")
        self.assertFalse(os.path.lexists(self.hold))

    def test_root_refresh_while_held_reports_hold_and_note(self):
        env = self.env()
        self.update_then_rollback()

        def runner(argv, timeout):
            if argv[:2] == ["systemctl", "start"]:
                with mock.patch.object(os, "geteuid", REAL_GETEUID):
                    cat.refresh(self.cfg, fetch=Site(b""), engine=ENGINE)
            return 0, ""
        code, rep = self.call(["--env", env, "catalogue", "refresh"], runner)
        self.assertEqual((code, rep["ok"], rep["refresh"]["result"], rep["hold"]["held"]), (0, True, "held", True))
        self.assertIn("catalogue resume", rep["note"])
        self.assertEqual(self.bytes_(), self.old)

    def test_refusals_and_child_output_is_filtered(self):
        env = self.env()
        never = mock.Mock(side_effect=AssertionError("runner"))
        code, rep = self.call(["--env", env, "catalogue", "resume"], never, uid=0)
        self.assertEqual((code, rep["error"]), (2, "service_user_is_root"))
        with mock.patch("pwd.getpwnam", side_effect=KeyError("opentv")), \
                mock.patch.object(cli.os, "geteuid", return_value=0), \
                mock.patch.object(cli.nethelper, "default_runner", never), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--env", env, "catalogue", "rollback"]), 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "service_user_missing")
        code, rep = self.call(["--env", "relative.env", "catalogue", "rollback"], never)
        self.assertEqual((code, rep["error"]), (2, "env_path_invalid"))
        for rc, text in ((127, ""), (124, ""), (0, "garbage"), (2, '{"result": "resumed", "ok": true}'),
                         (0, '{"ok": true}')):
            code, rep = self.call(["--env", env, "catalogue", "resume"], lambda a, t, r=(rc, text): r)
            self.assertEqual((code, rep["ok"], rep["error"]), (1, False, "delegate_failed"), (rc, text))
        evil = json.dumps({"ok": False, "result": "write_failed", "why": "https://%s/%s/b/x" % (HOST, PREFIX),
                           "host": HOST, "event": "catalogue_resume"})
        code, rep = self.call(["--env", env, "catalogue", "resume"], lambda a, t: (1, "log %s\n%s\n" % (HOST, evil)))
        self.assertEqual((code, rep["result"]), (1, "write_failed"))
        self.assertNotIn("why", rep)
        self.assertNotIn("host", rep)
        code, rep = self.call(["--env", env, "catalogue", "resume"], lambda a, t: (75, '{"ok":false,"result":"busy"}'))
        self.assertEqual((code, rep["result"]), (cat.BUSY_EXIT, "busy"))


# --------------------------------------------------------------------------- bac à sable : état non catalogue masqué
class TestRefreshSandboxPaths(unittest.TestCase):
    def test_non_catalogue_state_and_local_sockets_inaccessible(self):
        _, text = unit("opentv-catalogue-refresh.service")
        inacc = " ".join(re.findall(r"^InaccessiblePaths=(.+)$", text, re.M)).split()
        self.assertTrue(all(p.startswith("-/") for p in inacc), inacc)      # absent : jamais d'échec de montage
        paths = {p[1:] for p in inacc}
        c = config.Config({})
        for attr in ("device_file", "setup_flag", "hotspot_state", "hotspot_rearm", "apply_state", "jobs_dir",
                     "jobs_tmp", "results_dir", "registry_file", "audit_file", "foryou_state", "remote_file",
                     "agent_clients_dir", "nm_dir", "canvas_dir"):
            self.assertIn(getattr(c, attr), paths, attr)
        self.assertIn(c.registry_file + ".lock", paths)
        for run in (c.run_dir, os.path.dirname(c.player_socket), os.path.dirname(c.player_keys_socket),
                    "/run/opentv-cec"):
            self.assertIn(run, paths, run)
        self.assertTrue(c.agent_socket.startswith(c.run_dir + "/"))
        # tout ce que le passage doit écrire reste accessible (aucun masque ne le couvre)
        need = [c.catalogue_file + s for s in ("", ".prev", ".lock", cat.HOLD_SUFFIX)] + \
            [os.path.join(c.state_dir, cat.STATUS_NAME), c.source_state, os.path.join(c.source_state, "prefix.json")]
        for n in need:
            for p in paths:
                self.assertFalse(n == p or n.startswith(p.rstrip("/") + "/"), (n, p))
        self.assertTrue(c.catalogue_file.startswith(c.state_dir + "/"))
        rw = re.findall(r"^ReadWritePaths=(.+)$", text, re.M)
        self.assertEqual(rw, ["-" + c.state_dir])
        s = unit("opentv-catalogue-refresh.service")[0]["Service"]
        self.assertIn("AF_UNIX", s["RestrictAddressFamilies"].split())      # DNS (nss-resolve)
        self.assertIn("AF_INET", s["RestrictAddressFamilies"].split())
        self.assertNotIn("PrivateNetwork", s)

    def test_install_preserves_hold_and_uninstall_keeps_it_unless_purge(self):
        r = tempfile.mkdtemp(prefix="otvrfs", dir="/tmp")
        self.addCleanup(shutil.rmtree, r, True)
        st = os.path.join(r, "var/lib/opentv")
        os.makedirs(st)
        hold = os.path.join(st, "catalogue.json" + cat.HOLD_SUFFIX)
        with open(hold, "w") as f:
            f.write('{"held_at": 1, "reason": "rollback", "v": 1}')
        env = dict(os.environ, LC_ALL="C")
        sh = lambda *a: subprocess.run(["bash"] + list(a), capture_output=True, timeout=120, env=env)  # noqa: E731
        for _ in range(2):                                                   # installation puis mise à jour
            p = sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(os.path.isfile(hold))
            with open(os.path.join(r, "opt/opentv/MANIFEST.sha256")) as f:
                self.assertNotIn(".hold", f.read())
        for script in (INSTALL_SH, UNINSTALL_SH):
            with open(script, encoding="utf-8") as f:
                self.assertNotIn("catalogue.json.hold", f.read())            # jamais créé ni retiré nommément
        q = sh(os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertTrue(os.path.isfile(hold))
        p = sh(os.path.join(PI4, "install.sh"), "--root", r, "--skip-preflight")
        self.assertEqual(p.returncode, 0, p.stderr)
        q = sh(os.path.join(r, "opt/opentv/uninstall.sh"), "--root", r, "--purge")
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertFalse(os.path.lexists(hold))


INSTALL_SH = os.path.join(PI4, "install.sh")
UNINSTALL_SH = os.path.join(PI4, "uninstall.sh")


if __name__ == "__main__":
    unittest.main()
