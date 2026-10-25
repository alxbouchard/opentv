#!/usr/bin/env python3
"""opentv_tmdb — enrichissement TMDb du catalogue du pont NanoPi (commande ponctuelle, jamais en arrière-plan).

Ajoute à chaque élément de catalogue.json des champs **préfixés tmdb_** et étiquetés comme tels :
note TMDb (vote_average /10), nombre de votes, identifiant TMDb, statut/méthode/confiance de
correspondance et preuves bornées. Aucun autre champ n'est modifié ; les notes IMDb restent séparées.

Règles :
- Secret fourni **uniquement par l'environnement** au moment de l'exécution : TMDB_READ_TOKEN (jeton v4,
  en-tête Authorization) ou, à défaut, TMDB_API_KEY (clé v3). Jamais en argument, jamais journalisé,
  jamais écrit sur disque. Retiré de os.environ dès sa lecture.
- Correspondance prudente : (0) imdb_id déjà connu -> /find ; (1) titre exact (casse/espaces) + année
  compatible ; (2) titre normalisé (accents, ponctuation, article initial) + même année ; sinon
  `ambiguous` ou `unmatched`. Jamais de choix parmi plusieurs candidats plausibles.
- Note manquante (0 vote) = null (« indisponible »), jamais 0.
- Écriture atomique : verrou, vérification que le fichier n'a pas changé, contrôle que seuls des champs
  tmdb_* ont changé, copie de retour arrière `.tmdb.bak` (0600), remplacement atomique, relecture ;
  `rollback` restaure la copie. Les horodatages de vérification vivent dans un fichier d'état séparé
  pour que le catalogue reste identique octet pour octet quand rien ne change.

Usage :
  TMDB_READ_TOKEN=… python3 -I opentv_tmdb.py enrich --catalogue <catalogue.json> [--dry-run] [--max-requests N]
  python3 -I opentv_tmdb.py status   --catalogue <catalogue.json>
  python3 -I opentv_tmdb.py rollback --catalogue <catalogue.json>

This product uses the TMDB API but is not endorsed or certified by TMDB.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

TMDB_NOTICE = "This product uses the TMDB API but is not endorsed or certified by TMDB."
PROD_BASE = "https://api.themoviedb.org/3"
STATUSES = ("matched", "ambiguous", "unmatched")
METHODS = ("imdb_id", "exact_title_year", "normalized_title_year")
CONFIDENCES = ("high", "medium")
MAX_BODY = 1024 * 1024
MAX_CATALOGUE = 32 * 1024 * 1024
MAX_RESULTS = 20
V4_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
V3_RE = re.compile(r"^[0-9a-f]{32}$")
IMDB_RE = re.compile(r"^tt\d{5,10}$")
LANG_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
ARTICLES = frozenset(("le", "la", "les", "l", "un", "une", "des", "the", "a", "an"))
EXIT_OK, EXIT_CONFIG, EXIT_AUTH, EXIT_PROVIDER, EXIT_VALIDATION = 0, 2, 3, 4, 5


class TmdbError(Exception):
    """Erreur transitoire du fournisseur (réseau, 429, 5xx, JSON) : valeurs antérieures conservées."""


class AuthError(Exception):
    pass


class NotFound(Exception):
    pass


class Abort(Exception):
    def __init__(self, code, why):
        super().__init__(why)
        self.code, self.why = code, why


def log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    sys.stderr.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- secret + client HTTP
def take_credential(env=None):
    """-> ('bearer'|'api_key', valeur). Lit puis retire la variable de l'environnement. Ne l'affiche jamais."""
    env = os.environ if env is None else env
    tok, key = env.pop("TMDB_READ_TOKEN", "").strip(), env.pop("TMDB_API_KEY", "").strip()
    if tok:
        if not V4_RE.match(tok):
            raise Abort(EXIT_CONFIG, "tmdb_read_token_bad_format")
        return "bearer", tok
    if key:
        if not V3_RE.match(key):
            raise Abort(EXIT_CONFIG, "tmdb_api_key_bad_format")
        return "api_key", key
    raise Abort(EXIT_CONFIG, "tmdb_credential_missing")


def check_base(base):
    """Seule l'API officielle en HTTPS, ou un faux serveur de test en boucle locale (jamais ailleurs)."""
    if base == PROD_BASE:
        return base
    u = urllib.parse.urlsplit(base)
    if u.scheme == "http" and u.hostname == "127.0.0.1" and u.path == "/3" and not u.query and not u.username:
        return base
    raise Abort(EXIT_CONFIG, "tmdb_api_base_refused")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url, headers, timeout):
    """GET sans redirection, TLS vérifié, corps borné -> (status, headers{lower}, bytes)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        resp = e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TmdbError(type(e).__name__)
    try:
        body = resp.read(MAX_BODY + 1) or b""
    except OSError as e:
        raise TmdbError(type(e).__name__)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if len(body) > MAX_BODY:
        raise TmdbError("too_large")
    hdrs = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
    return getattr(resp, "status", None) or resp.code, hdrs, body


class Client:
    """Client TMDb v3 borné : budget de requêtes, espacement, une reprise sur 429, pas de journal d'URL."""

    def __init__(self, cred, base=PROD_BASE, fetch=None, max_requests=400, min_interval=0.1, timeout=6.0,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.kind, self._secret = cred
        self.base, self.fetch = check_base(base), fetch or http_get
        self.max_requests, self.min_interval, self.timeout = max_requests, min_interval, timeout
        self.sleep, self.monotonic = sleep, monotonic
        self.requests, self._last = 0, None

    def __repr__(self):   # jamais le secret, même dans une trace
        return "<tmdb.Client %s [REDACTED]>" % self.kind

    def budget_left(self):
        return self.max_requests - self.requests

    def get(self, path, params):
        q = [(k, v) for k, v in params if v is not None]
        headers = {"Accept": "application/json", "User-Agent": "opentv-bridge-tmdb/1"}
        if self.kind == "bearer":
            headers["Authorization"] = "Bearer " + self._secret
        else:
            q.append(("api_key", self._secret))
        url = "%s%s?%s" % (self.base, path, urllib.parse.urlencode(q))
        for attempt in (0, 1):
            if self.requests >= self.max_requests:
                raise TmdbError("budget")
            if self._last is not None:
                wait = self.min_interval - (self.monotonic() - self._last)
                if wait > 0:
                    self.sleep(wait)
            self.requests += 1
            self._last = self.monotonic()
            status, hdrs, body = self.fetch(url, headers, self.timeout)
            if status == 429 and attempt == 0:
                try:
                    ra = float(hdrs.get("retry-after", "2"))
                except ValueError:
                    ra = 2.0
                self.sleep(min(max(ra, 0.5), 10.0))
                continue
            if status in (401, 403):
                raise AuthError("tmdb_auth_failed")
            if status == 404:
                raise NotFound()
            if status != 200:
                raise TmdbError("status_%d" % status)
            if not hdrs.get("content-type", "").lower().startswith("application/json"):
                raise TmdbError("not_json")
            try:
                obj = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise TmdbError("bad_json")
            if not isinstance(obj, dict):
                raise TmdbError("bad_json")
            return obj
        raise TmdbError("rate_limited")


# --------------------------------------------------------------------------- normalisation + correspondance
def exact_title(s):
    s = unicodedata.normalize("NFC", s or "").replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", s).strip().casefold()


def loose_title(s):
    s = unicodedata.normalize("NFKD", exact_title(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = re.sub(r"[^\w]+", " ", s).split()
    if len(words) > 1 and words[0] in ARTICLES:
        words = words[1:]
    return " ".join(words)


def cand_year(c):
    m = re.match(r"^(\d{4})-\d{2}-\d{2}$", str(c.get("release_date") or ""))
    return int(m.group(1)) if m else None


def _num(v, lo, hi, t):
    return v if isinstance(v, t) and not isinstance(v, bool) and lo <= v <= hi else None


def clean_candidate(c):
    if not isinstance(c, dict) or c.get("adult") is True:
        return None
    cid = _num(c.get("id"), 1, 10 ** 9, int)
    if cid is None:
        return None
    t = c.get("title") if isinstance(c.get("title"), str) else ""
    ot = c.get("original_title") if isinstance(c.get("original_title"), str) else ""
    return {"id": cid, "title": t[:200], "original_title": ot[:200], "year": cand_year(c),
            "vote_average": _num(c.get("vote_average"), 0, 10, (int, float)),
            "vote_count": _num(c.get("vote_count"), 0, 10 ** 9, int)}


def select(item_title, item_year, cands):
    """Correspondance prudente -> (statut, candidat|None, méthode|None, confiance|None, raison, ids évoqués)."""
    uniq, seen = [], set()
    for c in cands:
        if c and c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    if item_year is None:
        return "unmatched", None, None, None, "no_year", []
    title_hit = []
    for stage, norm, method in (("exact", exact_title, "exact_title_year"),
                                ("loose", loose_title, "normalized_title_year")):
        want = norm(item_title)
        if not want:
            break
        hits = [c for c in uniq if want in (norm(c["title"]), norm(c["original_title"]))]
        title_hit.extend(c["id"] for c in hits if c["id"] not in title_hit)
        same = [c for c in hits if c["year"] == item_year]
        near = [c for c in hits if c["year"] is not None and abs(c["year"] - item_year) == 1]
        if len(same) == 1:
            return "matched", same[0], method, "high" if stage == "exact" else "medium", stage + "_year", [same[0]["id"]]
        if len(same) > 1:
            return "ambiguous", None, None, None, stage + "_multiple", [c["id"] for c in same][:5]
        if near:
            # année ±1 (sortie tardive, festival) seulement sur titre exact et candidat unique
            if stage == "exact" and len(near) == 1:
                return "matched", near[0], method, "medium", "exact_year_pm1", [near[0]["id"]]
            return "ambiguous", None, None, None, stage + "_year_pm1", [c["id"] for c in near][:5]
    if title_hit:
        return "unmatched", None, None, None, "year_mismatch", title_hit[:5]
    return "unmatched", None, None, None, "no_title_match" if uniq else "no_results", []


def match_key(item):
    raw = json.dumps([exact_title(item.get("title") or ""), item.get("year") if isinstance(item.get("year"), int) else None,
                      item.get("imdb_id") if isinstance(item.get("imdb_id"), str) and IMDB_RE.match(item["imdb_id"]) else None],
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


TMDB_FIELDS = ("tmdb_id", "tmdb_rating", "tmdb_votes", "tmdb_match_status", "tmdb_match_method",
               "tmdb_confidence", "tmdb_match_key", "tmdb_reason", "tmdb_evidence", "tmdb_rating_updated_at")


def valid_tmdb_fields(it):
    st = it.get("tmdb_match_status")
    if st is None:
        return not any(k in it for k in TMDB_FIELDS if k != "tmdb_match_status")
    if st not in STATUSES or not isinstance(it.get("tmdb_match_key"), str):
        return False
    if st == "matched":
        return (_num(it.get("tmdb_id"), 1, 10 ** 9, int) is not None and it.get("tmdb_match_method") in METHODS
                and it.get("tmdb_confidence") in CONFIDENCES
                and (it.get("tmdb_rating") is None or _num(it["tmdb_rating"], 0, 10, (int, float)) is not None)
                and (it.get("tmdb_votes") is None or _num(it["tmdb_votes"], 0, 10 ** 9, int) is not None)
                and not (it.get("tmdb_rating") is not None and not it.get("tmdb_votes")))
    return it.get("tmdb_rating") is None and it.get("tmdb_id") is None


def _evidence(c, reason, ids):
    ev = {"reason": reason, "candidates": ids[:5]}
    if c:
        ev.update({"title": c["title"], "original_title": c["original_title"], "year": c["year"]})
    return ev


def apply_match(it, key, status, c, method, conf, reason, ids, now_iso):
    """Écrit les champs tmdb_* (et seulement eux) ; garde l'horodatage si la note n'a pas changé."""
    prev = (it.get("tmdb_id"), it.get("tmdb_rating"), it.get("tmdb_votes"))
    for k in TMDB_FIELDS:
        if k != "tmdb_rating_updated_at":
            it.pop(k, None)
    it["tmdb_match_status"], it["tmdb_match_key"], it["tmdb_reason"] = status, key, reason
    it["tmdb_evidence"] = _evidence(c, reason, ids)
    if status == "matched":
        votes = c["vote_count"]
        rating = round(float(c["vote_average"]), 1) if votes and c["vote_average"] is not None else None
        it.update({"tmdb_id": c["id"], "tmdb_rating": rating, "tmdb_votes": votes if votes is not None else None,
                   "tmdb_match_method": method, "tmdb_confidence": conf})
        if (c["id"], rating, it["tmdb_votes"]) != prev or "tmdb_rating_updated_at" not in it:
            it["tmdb_rating_updated_at"] = now_iso
    else:
        it.pop("tmdb_rating_updated_at", None)


# --------------------------------------------------------------------------- enrichissement
def _strip(obj):
    """Copie sans aucun champ TMDb : doit rester identique avant/après (seul tmdb_* peut changer)."""
    o = json.loads(json.dumps(obj))
    if isinstance(o, dict):
        att = o.get("attribution")
        if isinstance(att, dict):
            att.pop("tmdb", None)
            if not att:
                o.pop("attribution")
        for it in o.get("items") or []:
            if isinstance(it, dict):
                for k in TMDB_FIELDS:
                    it.pop(k, None)
    return o


def _items(cat):
    items = cat.get("items") if isinstance(cat, dict) else None
    if not isinstance(items, list):
        raise Abort(EXIT_VALIDATION, "catalogue_shape")
    return items


def enrich(cat, state, client, languages=("fr-FR",), rating_max_age_s=7 * 86400, recheck_s=30 * 86400,
           now=None, max_consecutive_errors=5):
    """Modifie `cat` (champs tmdb_* seulement) et `state` (horodatages) en place. -> compteurs."""
    now = time.time() if now is None else now
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    items = _items(cat)
    marks = state.setdefault("items", {})
    n = {"items": len(items), "new": 0, "rating_refresh": 0, "recheck": 0, "reused": 0, "matched": 0,
         "ambiguous": 0, "unmatched": 0, "errors": 0, "deferred": 0, "skipped": 0}
    def age_of(mk, k):
        try:
            return now - float(mk.get(k) or 0)
        except (TypeError, ValueError):
            return now

    # ordre : nouveaux titres / entrées modifiées, puis notes périmées (plus vieilles d'abord), puis revérifications
    todo = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("title"), str) or not it["title"].strip():
            n["skipped"] += 1
            continue
        key = match_key(it)
        mk = marks.get(key) if isinstance(marks.get(key), dict) else {}
        st = it.get("tmdb_match_status")
        if it.get("tmdb_match_key") != key or st not in STATUSES or not valid_tmdb_fields(it):
            todo.append((0, 0.0, i, "new"))
        elif st == "matched":
            age = age_of(mk, "rating_checked_at")
            if age >= rating_max_age_s:
                todo.append((1, -age, i, "rating"))
            else:
                n["reused"] += 1
        else:
            age = age_of(mk, "checked_at")
            if age >= recheck_s:
                todo.append((2, -age, i, "recheck"))
            else:
                n["reused"] += 1
    todo.sort()
    streak = 0
    for pos, (_, _, i, kind) in enumerate(todo):
        if client.budget_left() <= 0 or streak >= max_consecutive_errors:
            n["deferred"] = len(todo) - pos
            break
        it = items[i]
        key = match_key(it)
        try:
            if kind == "rating":
                _refresh_rating(it, key, client, now_iso)
                n["rating_refresh"] += 1
                marks.setdefault(key, {})["rating_checked_at"] = now
            else:
                _match(it, key, client, languages, now_iso)
                n[kind] += 1
                m = marks.setdefault(key, {})
                m["checked_at"] = now
                if it["tmdb_match_status"] == "matched":
                    m["rating_checked_at"] = now
            streak = 0
        except TmdbError as e:
            streak += 1
            n["errors"] += 1
            log("tmdb_item_error", index=i, kind=kind, why=str(e)[:40], kept=it.get("tmdb_match_status"))
    for it in items:
        if isinstance(it, dict) and it.get("tmdb_match_status") in STATUSES:
            n[it["tmdb_match_status"]] += 1
    if streak >= max_consecutive_errors:
        n["circuit_open"] = True
    live = {k for k in (match_key(it) for it in items if isinstance(it, dict) and isinstance(it.get("title"), str))}
    for k in list(marks):
        if k not in live:
            del marks[k]
    if n["matched"]:
        cat.setdefault("attribution", {})
        if isinstance(cat["attribution"], dict):
            cat["attribution"]["tmdb"] = TMDB_NOTICE
    return n


def _match(it, key, client, languages, now_iso):
    title, year = it["title"].strip(), it.get("year") if isinstance(it.get("year"), int) else None
    imdb = it.get("imdb_id") if isinstance(it.get("imdb_id"), str) and IMDB_RE.match(it["imdb_id"]) else None
    if imdb and it.get("match_status") not in ("ambiguous", "unmatched"):
        obj = client.get("/find/" + imdb, [("external_source", "imdb_id")])
        mr = obj.get("movie_results") if isinstance(obj.get("movie_results"), list) else []
        res = [c for c in (clean_candidate(x) for x in mr[:MAX_RESULTS]) if c]
        if len(res) == 1:
            c = res[0]
            if year is None or c["year"] is None or abs(c["year"] - year) <= 1:
                return apply_match(it, key, "matched", c, "imdb_id", "high", "imdb_id", [c["id"]], now_iso)
            return apply_match(it, key, "ambiguous", None, None, None, "imdb_year_conflict", [c["id"]], now_iso)
        if len(res) > 1:
            return apply_match(it, key, "ambiguous", None, None, None, "imdb_multiple", [c["id"] for c in res], now_iso)
    if year is None:
        return apply_match(it, key, "unmatched", None, None, None, "no_year", [], now_iso)
    result = None
    for lang in languages:
        obj = client.get("/search/movie", [("query", title[:200]), ("include_adult", "false"), ("language", lang),
                                           ("page", "1")])
        raw = obj.get("results")
        if not isinstance(raw, list):
            raise TmdbError("bad_results")
        result = select(title, year, [clean_candidate(c) for c in raw[:MAX_RESULTS]])
        if result[0] != "unmatched" or client.budget_left() <= 0:
            break
    return apply_match(it, key, result[0], result[1], result[2], result[3], result[4], result[5], now_iso)


def _refresh_rating(it, key, client, now_iso):
    try:
        obj = client.get("/movie/%d" % it["tmdb_id"], [])
    except NotFound:
        return apply_match(it, key, "unmatched", None, None, None, "provider_gone", [it["tmdb_id"]], now_iso)
    c = clean_candidate(obj)
    if not c or c["id"] != it["tmdb_id"]:
        raise TmdbError("id_mismatch")
    ev = it.get("tmdb_evidence") if isinstance(it.get("tmdb_evidence"), dict) else {}
    apply_match(it, key, "matched", c, it["tmdb_match_method"], it["tmdb_confidence"],
                ev.get("reason") or it.get("tmdb_reason") or "refresh", [c["id"]], now_iso)


# --------------------------------------------------------------------------- fichiers : verrou, atomique, retour arrière
def atomic_write_bytes(path, raw):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-tmdb-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump(obj):
    # même forme que atomic_write_json du pont : sort_keys + indent=1
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def read_json(path, cap=MAX_CATALOGUE):
    try:
        with open(path, "rb") as f:
            raw = f.read(cap + 1)
    except OSError as e:
        raise Abort(EXIT_CONFIG, "catalogue_unreadable:" + type(e).__name__)
    if len(raw) > cap:
        raise Abort(EXIT_VALIDATION, "catalogue_too_large")
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Abort(EXIT_VALIDATION, "catalogue_not_json")


class FileLock:
    def __init__(self, path):
        self.path, self.fd = path + ".lock", None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise Abort(EXIT_CONFIG, "locked")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def paths(cat_path):
    return {"bak": cat_path + ".tmdb.bak", "state": cat_path + ".tmdb-state.json"}


def commit(cat_path, original_raw, original, new):
    """Valide puis remplace atomiquement ; -> True si écrit, False si inchangé. Restaure en cas d'échec de relecture."""
    if _strip(original) != _strip(new):
        raise Abort(EXIT_VALIDATION, "non_tmdb_change")
    bad = [i for i, it in enumerate(_items(new)) if isinstance(it, dict) and not valid_tmdb_fields(it)]
    if bad:
        raise Abort(EXIT_VALIDATION, "invalid_tmdb_fields:%d" % len(bad))
    raw = dump(new)
    if len(raw) > MAX_CATALOGUE:
        raise Abort(EXIT_VALIDATION, "output_too_large")
    if new == original:   # rien n'a changé : aucun octet réécrit, aucune copie
        return False
    with open(cat_path, "rb") as f:   # le fichier a-t-il changé depuis la lecture ?
        if f.read(MAX_CATALOGUE + 1) != original_raw:
            raise Abort(EXIT_VALIDATION, "catalogue_changed")
    p = paths(cat_path)
    atomic_write_bytes(p["bak"], original_raw)
    atomic_write_bytes(cat_path, raw)
    try:
        _, back = read_json(cat_path)
        if back != new:
            raise Abort(EXIT_VALIDATION, "readback_mismatch")
    except Abort:
        atomic_write_bytes(cat_path, original_raw)
        raise
    return True


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict) and isinstance(s.get("items"), dict):
            return {"v": 1, "items": {k: v for k, v in s["items"].items() if isinstance(k, str) and isinstance(v, dict)}}
    except (OSError, ValueError):
        pass
    return {"v": 1, "items": {}}


def run_enrich(cat_path, client, languages, dry_run=False, rating_max_age_s=7 * 86400, recheck_s=30 * 86400, now=None):
    with FileLock(cat_path):
        original_raw, original = read_json(cat_path)
        _items(original)
        new = json.loads(json.dumps(original))
        sp = paths(cat_path)["state"]
        state = load_state(sp)
        try:
            n = enrich(new, state, client, languages, rating_max_age_s, recheck_s, now=now)
        except AuthError:
            raise Abort(EXIT_AUTH, "tmdb_auth_failed")   # rien n'est écrit
        n["requests"] = client.requests
        n["dry_run"] = dry_run
        if dry_run:
            n["wrote"] = False
            return n
        n["wrote"] = commit(cat_path, original_raw, original, new)
        atomic_write_bytes(sp, dump(state))
        if n["wrote"]:
            n["backup"] = os.path.basename(paths(cat_path)["bak"])
        return n


def run_rollback(cat_path):
    with FileLock(cat_path):
        bak = paths(cat_path)["bak"]
        if not os.path.exists(bak):
            raise Abort(EXIT_VALIDATION, "no_backup")
        raw, obj = read_json(bak)
        _items(obj)
        atomic_write_bytes(cat_path, raw)
        _, back = read_json(cat_path)
        if back != obj:
            raise Abort(EXIT_VALIDATION, "readback_mismatch")
        return {"restored": True, "items": len(obj["items"])}


def run_status(cat_path):
    _, cat = read_json(cat_path)
    items = _items(cat)
    n = {s: 0 for s in STATUSES}
    n.update({"items": len(items), "none": 0, "rated": 0, "invalid": 0,
              "attribution": bool(isinstance(cat.get("attribution"), dict) and cat["attribution"].get("tmdb"))})
    for it in items:
        if not isinstance(it, dict):
            continue
        st = it.get("tmdb_match_status")
        if not valid_tmdb_fields(it):
            n["invalid"] += 1
        elif st in STATUSES:
            n[st] += 1
            n["rated"] += it.get("tmdb_rating") is not None
        else:
            n["none"] += 1
    n["backup"] = os.path.exists(paths(cat_path)["bak"])
    return n


def main(argv=None, env=None):
    ap = argparse.ArgumentParser(prog="opentv_tmdb.py", description="Enrichissement TMDb ponctuel du catalogue.")
    ap.add_argument("cmd", choices=("enrich", "status", "rollback"))
    ap.add_argument("--catalogue", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-requests", type=int, default=400)
    ap.add_argument("--languages", default="fr-FR,fr-CA")
    ap.add_argument("--rating-max-age-days", type=float, default=7.0)
    ap.add_argument("--recheck-days", type=float, default=30.0)
    a = ap.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if a.cmd == "status":
            out = run_status(a.catalogue)
        elif a.cmd == "rollback":
            out = run_rollback(a.catalogue)
        else:
            langs = tuple(x for x in a.languages.split(",") if x)
            if not langs or len(langs) > 3 or not all(LANG_RE.match(x) for x in langs):
                raise Abort(EXIT_CONFIG, "bad_languages")
            if not 1 <= a.max_requests <= 5000:
                raise Abort(EXIT_CONFIG, "bad_max_requests")
            if not os.path.isfile(a.catalogue):
                raise Abort(EXIT_CONFIG, "catalogue_missing")
            cred = take_credential(env)
            client = Client(cred, base=env.get("TMDB_API_BASE", PROD_BASE), max_requests=a.max_requests)
            out = run_enrich(a.catalogue, client, langs, a.dry_run, a.rating_max_age_days * 86400,
                             a.recheck_days * 86400)
            del client, cred
    except Abort as e:
        log("tmdb_abort", why=e.why)
        print(json.dumps({"ok": False, "error": e.why}, sort_keys=True))
        return e.code
    print(json.dumps(dict(out, ok=True), sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
