#!/usr/bin/env python3
"""opentv_foryou — « Pour toi » côté NanoPi : historique local, recommandations explicables, CLI agent.

- Stockage SQLite (bibliothèque standard) sous STATE_DIR (0600, dossier 0700, jamais dans Git) :
  J'aime / Pas pour moi (exclusifs), Déjà vu, À voir plus tard, Pas ce soir (échéance), journal des
  changements, signaux de séance (« ouvert » n'est JAMAIS « vu »), instantanés du catalogue accepté.
- Moteur déterministe (fonction pure, horloge injectée) : catalogue accepté uniquement, sans vus ni
  « pas pour moi » ; goût appris → qualité (confiance par nombre de votes) → nouveauté → diversité.
  Aucune note fabriquée : IMDb et TMDb restent séparés et les explications citent les valeurs brutes.
- Navigation télécommande (rangées + panneau d'actions) utilisée par opentv_bridge.py.
- CLI JSON locale pour Muse/Hermit : status | recommend | search | feedback | history. Aucun port réseau,
  aucune URL (ni fiche, ni affiche, ni flux) dans les sorties : toute chaîne (titre, requête, raisons, note,
  détail d'erreur, objets imbriqués) passe par scrub() avant écriture ; une requête qui ressemble à une URL
  est refusée sans être réaffichée.

Identifiant stable d'un film : « <FORYOU_SOURCE_ID>/<site_id> » (défaut « films/<site_id> »). L'espace de noms
est logique et explicite : il ne dépend ni du domaine, ni de SITE_ROOT, ni du préfixe, du jeton ou des routes ;
changer d'ancre (ex. fakrov → wadrox) n'orpheline donc aucune préférence.
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata

SCHEMA_VERSION = 1
NOT_TONIGHT_S = 12 * 3600.0          # « Pas ce soir » : mis de côté 12 h, jamais un signal de goût
NEW_WINDOW_S = 30 * 86400.0          # un ajout au catalogue reste « nouveau » 30 jours
ROW_MAX = 12                         # éléments par rangée (DOM léger sous weston pixman)
MAX_LOG = 5000                       # événements / signaux conservés
DIVERSITY_LOOKAHEAD = 6
DEFAULT_SOURCE_ID = "films"          # espace de noms logique des identifiants (FORYOU_SOURCE_ID)
# Contention SQLite (sauvegarde, CLI, verrou externe) : SEUL réglage d'attente de la base, appliqué par
# Store._retry (pas par le gestionnaire interne de SQLite, dont le sommeil peut s'arrondir à la seconde
# selon la compilation). Court, pour que le pont réponde bien avant le délai de la TV (6 s).
BUSY_TIMEOUT_MS = 200
BUSY_STEP_S = 0.01
STORE_COOLDOWN_S = 5.0               # disjoncteur du pont : après une panne, profil vide sans toucher la base
ASYNC_MAX = 32                       # file d'écritures asynchrones (Films : instantané + « ouvert »), bornée
SOURCE_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")             # pas de point : jamais un nom d'hôte
UID_RE = re.compile(r"[a-z][a-z0-9-]{1,23}/[0-9]{1,12}")
SITE_ID_RE = re.compile(r"[0-9]{1,12}")
# toute forme d'URL ; le jeton entier est retiré :
#  schéma://…, www.…, //hôte… (relative au protocole), magnet:/data:/javascript:/vbscript:/file:/blob: suivis
#  d'un caractère non blanc, et hôte nu « domaine.tld/chemin » ou « a.b.c.d/chemin » (port facultatif).
#  « Mission // Impossible », « 2/3 », « Big Data: le film », « Dr. Folamour » restent intacts.
_HOST = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}|[0-9]{1,3}(?:\.[0-9]{1,3}){3}"
_SCHEMES = r"(?:magnet|data|javascript|vbscript|file|blob):"
URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]*://\S*|(?<![a-z0-9])www\.\S*|(?<!\S)//[^\s/]\S*"
                    r"|(?<![a-z0-9])" + _SCHEMES + r"(?=\S)\S*"
                    r"|(?<![\w.@-])(?:" + _HOST + r")(?::[0-9]{1,5})?/\S*)", re.I)
URLISH_RE = re.compile(r"https?:|://|(?<![a-z0-9])www\.|(?<!\S)//[^\s/]|(?<![a-z0-9])" + _SCHEMES + r"(?=\S)"
                       r"|(?<![\w.@-])(?:" + _HOST + r")(?::[0-9]{1,5})?/", re.I)

# action -> (colonne, valeur) ; « not-tonight » écrit une échéance
ACTIONS = {
    "like": ("opinion", "like"), "dislike": ("opinion", "dislike"), "clear-opinion": ("opinion", None),
    "seen": ("seen", 1), "unseen": ("seen", 0),
    "watchlist": ("watchlist", 1), "unwatchlist": ("watchlist", 0),
    "not-tonight": ("snooze", 1), "clear-not-tonight": ("snooze", 0),
}
REMOVALS = frozenset(("clear-opinion", "unseen", "unwatchlist", "clear-not-tonight"))   # retraits seulement
SIGNALS = ("opened",)
BUTTONS = (("watch", "Regarder"), ("like", "J'aime"), ("dislike", "Pas pour moi"), ("seen", "Déjà vu"),
           ("watchlist", "À voir plus tard"), ("not-tonight", "Pas ce soir"))
ROW_TITLES = {
    "tonight": "Le choix de ce soir", "watchlist": "À voir plus tard", "new": "Nouveautés disponibles",
    "top": "Les mieux notés que tu n'as pas vus", "more": "Autres suggestions",
}
EMPTY_MARK = {"opinion": None, "seen": False, "watchlist": False, "not_tonight": False}


class StoreError(Exception):
    pass


def _busy(e):
    return isinstance(e, sqlite3.OperationalError) and ("locked" in str(e) or "busy" in str(e))


def valid_uid(u):
    return isinstance(u, str) and bool(UID_RE.fullmatch(u))


def valid_source(s):
    return isinstance(s, str) and bool(SOURCE_RE.fullmatch(s))


def source_id(env):
    """Espace de noms des identifiants : FORYOU_SOURCE_ID (défaut « films »), validé strictement.
    Volontairement indépendant de SITE_ROOT : -> str, ValueError si invalide (jamais de repli silencieux)."""
    s = env.get("FORYOU_SOURCE_ID", DEFAULT_SOURCE_ID)
    if not valid_source(s):
        raise ValueError("bad_source_id")
    return s


def uid_for(source, item):
    sid = item.get("site_id") if isinstance(item, dict) else None
    sid = str(sid) if isinstance(sid, (str, int)) and not isinstance(sid, bool) else ""
    u = "%s/%s" % (source, sid)
    return u if SITE_ID_RE.fullmatch(sid) and valid_uid(u) else None


def clean_text(s, n=None):
    """Retire toute URL d'un texte (titre, genre, résumé…). Sans URL, le texte est rendu tel quel."""
    if not isinstance(s, str):
        return ""
    if URL_RE.search(s):
        while True:   # jusqu'au point fixe : aucun recollage ne peut reformer une URL
            t = URL_RE.sub(" ", s)
            if t == s:
                break
            s = t
        s = re.sub(r"\s+", " ", s).strip()
    return s[:n] if n is not None else s


def scrub(obj):
    """Nettoyage global et récursif (clés comprises) de tout objet renvoyé par la CLI agent."""
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, dict):
        return {scrub(k) if isinstance(k, str) else k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


# --------------------------------------------------------------------------- stockage
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS prefs (
  uid TEXT PRIMARY KEY,
  opinion TEXT CHECK (opinion IN ('like', 'dislike')),
  seen INTEGER NOT NULL DEFAULT 0 CHECK (seen IN (0, 1)),
  watchlist INTEGER NOT NULL DEFAULT 0 CHECK (watchlist IN (0, 1)),
  snooze_until REAL,
  title TEXT, genre TEXT,
  opinion_at REAL, watchlist_at REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, uid TEXT NOT NULL,
  action TEXT NOT NULL, origin TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, uid TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('opened')), app TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, fingerprint TEXT NOT NULL,
  count INTEGER NOT NULL, added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalogue_seen (
  uid TEXT PRIMARY KEY, first_seen REAL NOT NULL, snapshot INTEGER NOT NULL
);
"""


class Store:
    """Base locale. Toutes les opérations sont idempotentes ; seuls les changements sont journalisés."""

    def __init__(self, path, clock=time.time, max_log=MAX_LOG):
        self.path, self.clock, self.max_log = path, clock, max_log
        self.lock = threading.Lock()
        self.db = None
        try:
            self._open(path)
        except BaseException:
            self.close()
            raise

    def _open(self, path):
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, mode=0o700, exist_ok=True)
            if not os.path.exists(path):
                os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
            if not os.path.isfile(path):
                raise StoreError("not_a_file")
            if os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
            # timeout=0 : SQLite rend SQLITE_BUSY tout de suite ; l'attente bornée est celle de _retry
            self.db = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except StoreError:
            raise
        except (OSError, sqlite3.Error) as e:
            raise StoreError(type(e).__name__)

    @staticmethod
    def _retry(fn):
        """Seule attente de verrou : réessaie fn() tant que la base est occupée, au plus BUSY_TIMEOUT_MS."""
        deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000.0
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if not _busy(e) or time.monotonic() >= deadline:
                    raise
            time.sleep(BUSY_STEP_S)

    def _begin(self):
        self._retry(lambda: self.db.execute("BEGIN IMMEDIATE"))

    def _commit(self):
        self._retry(lambda: self.db.execute("COMMIT"))   # attend au plus BUSY_TIMEOUT_MS les lecteurs externes

    def _migrate(self):
        v = self._retry(lambda: self.db.execute("PRAGMA user_version").fetchone()[0])
        if v > SCHEMA_VERSION:
            raise StoreError("schema_newer")
        if v < 1:
            self._begin()
            try:
                for stmt in _SCHEMA_V1.split(";"):
                    if stmt.strip():
                        self.db.execute(stmt)
                self.db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
                self._commit()
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        self._retry(lambda: self.db.execute("SELECT count(*) FROM prefs").fetchone())   # vérifie la lisibilité

    def close(self):
        db, self.db = self.db, None
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    def _read(self, fn):
        """Lecture sous le verrou du magasin ; toute erreur SQLite (verrou, corruption) -> StoreError."""
        with self.lock:
            if self.db is None:
                raise StoreError("closed")
            try:
                return self._retry(lambda: fn(self.db))
            except sqlite3.Error as e:
                raise StoreError(type(e).__name__)

    # -- préférences
    def _row(self, uid):
        r = self.db.execute("SELECT opinion, seen, watchlist, snooze_until FROM prefs WHERE uid=?", (uid,)).fetchone()
        return r or (None, 0, 0, None)

    def state(self, uid, now=None):
        if not valid_uid(uid):
            raise ValueError("bad_id")
        now = self.clock() if now is None else now
        op, seen, wl, sn = self._read(lambda db: self._row(uid))
        return {"opinion": op, "seen": bool(seen), "watchlist": bool(wl), "not_tonight": bool(sn and sn > now)}

    def apply(self, uid, action, origin="cli", title=None, genre=None):
        """Applique une action (valeur explicite → idempotente). -> {changed, state}."""
        if not valid_uid(uid):
            raise ValueError("bad_id")
        if action not in ACTIONS:
            raise ValueError("bad_action")
        if origin not in ("tv", "cli", "test"):
            origin = "cli"
        col, val = ACTIONS[action]
        now = self.clock()
        title = clean_text(title, 200) if isinstance(title, str) else None
        genre = clean_text(genre, 80) if isinstance(genre, str) else None
        with self.lock:
            if self.db is None:
                raise StoreError("closed")
            try:
                self._begin()
                op, seen, wl, sn = self._row(uid)
                cur = {"opinion": op, "seen": seen, "watchlist": wl, "snooze": 1 if (sn and sn > now) else 0}
                changed = cur[col] != val
                if changed:
                    new = dict(cur)
                    new[col] = val
                    if col == "snooze":
                        snooze = (now + NOT_TONIGHT_S) if val else None
                    else:   # une autre action ne prolonge ni ne réactive un « Pas ce soir »
                        snooze = sn if (sn and sn > now) else None
                    self.db.execute(
                        "INSERT INTO prefs (uid, opinion, seen, watchlist, snooze_until, title, genre, opinion_at, "
                        "watchlist_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
                        "opinion=excluded.opinion, seen=excluded.seen, watchlist=excluded.watchlist, "
                        "snooze_until=excluded.snooze_until, title=coalesce(excluded.title, prefs.title), "
                        "genre=coalesce(excluded.genre, prefs.genre), "
                        "opinion_at=CASE WHEN ? THEN excluded.opinion_at ELSE prefs.opinion_at END, "
                        "watchlist_at=CASE WHEN ? THEN excluded.watchlist_at ELSE prefs.watchlist_at END, "
                        "updated_at=excluded.updated_at",
                        (uid, new["opinion"], new["seen"], new["watchlist"], snooze, title, genre, now, now, now,
                         col == "opinion", col == "watchlist"))
                    self.db.execute("INSERT INTO events (ts, uid, action, origin) VALUES (?,?,?,?)",
                                    (now, uid, action, origin))
                    self._prune("events")
                self._commit()
            except sqlite3.Error as e:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(type(e).__name__)
        return {"changed": changed, "state": self.state(uid, now)}

    def record_signal(self, uid, kind, app="pourtoi"):
        """Signal de séance (ex. film ouvert). Ne modifie jamais « vu » ni l'avis."""
        if not valid_uid(uid) or kind not in SIGNALS:
            raise ValueError("bad_signal")
        app = app if isinstance(app, str) and re.match(r"^[a-z0-9-]{1,24}$", app) else "other"
        with self.lock:
            if self.db is None:
                raise StoreError("closed")
            try:
                self._begin()
                self.db.execute("INSERT INTO signals (ts, uid, kind, app) VALUES (?,?,?,?)", (self.clock(), uid, kind, app))
                self._prune("signals")
                self._commit()
            except sqlite3.Error as e:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(type(e).__name__)

    def _prune(self, table):
        self.db.execute("DELETE FROM %s WHERE id <= (SELECT id FROM %s ORDER BY id DESC LIMIT 1 OFFSET ?)"
                        % (table, table), (self.max_log,))

    def profile(self):
        """uid -> préférences brutes (pour le moteur)."""
        rows = self._read(lambda db: db.execute(
            "SELECT uid, opinion, seen, watchlist, snooze_until, title, genre, opinion_at, "
            "watchlist_at, updated_at FROM prefs").fetchall())
        return {r[0]: {"opinion": r[1], "seen": bool(r[2]), "watchlist": bool(r[3]), "snooze_until": r[4],
                       "title": r[5], "genre": r[6], "opinion_at": r[7], "watchlist_at": r[8], "updated_at": r[9]}
                for r in rows}

    def counts(self, now=None):
        now = self.clock() if now is None else now
        r = self._read(lambda db: db.execute(
            "SELECT coalesce(sum(opinion='like'),0), coalesce(sum(opinion='dislike'),0), coalesce(sum(seen),0), "
            "coalesce(sum(watchlist),0), coalesce(sum(snooze_until > ?),0) FROM prefs", (now,)).fetchone())
        return {"liked": r[0], "disliked": r[1], "seen": r[2], "watchlist": r[3], "not_tonight": r[4]}

    def history(self, kind="events", limit=20, offset=0):
        limit, offset = max(0, int(limit)), max(0, int(offset))
        if kind == "events":
            rows = self._read(lambda db: db.execute(
                "SELECT e.ts, e.uid, e.action, e.origin, p.title FROM events e LEFT JOIN prefs p "
                "ON p.uid=e.uid ORDER BY e.ts DESC, e.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall())
            return [{"at": iso(r[0]), "id": r[1], "action": r[2], "origin": r[3], "title": r[4]} for r in rows]
        if kind == "signals":
            rows = self._read(lambda db: db.execute(
                "SELECT s.ts, s.uid, s.kind, s.app, p.title FROM signals s LEFT JOIN prefs p "
                "ON p.uid=s.uid ORDER BY s.ts DESC, s.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall())
            return [{"at": iso(r[0]), "id": r[1], "kind": r[2], "app": r[3], "title": r[4]} for r in rows]
        raise ValueError("bad_kind")

    # -- instantanés du catalogue accepté (nouveautés sans robot : diff de catalogues déjà présents)
    # Un catalogue VIDE (panne de collecte, premier démarrage) n'est jamais enregistré : la référence est le
    # premier catalogue non vide accepté, sinon tout le catalogue suivant serait faussement « Ajouté ».
    def observe_catalogue(self, uids):
        uids = sorted({u for u in uids if valid_uid(u)})
        if not uids:
            return {"changed": False, "baseline": False, "added": 0}
        fp = hashlib.sha256("\n".join(uids).encode()).hexdigest()
        now = self.clock()
        with self.lock:
            if self.db is None:
                raise StoreError("closed")
            try:
                self._begin()
                last = self.db.execute("SELECT fingerprint FROM snapshots WHERE count > 0 "
                                       "ORDER BY id DESC LIMIT 1").fetchone()
                if last and last[0] == fp:
                    self._commit()
                    return {"changed": False, "baseline": False, "added": 0}
                # base héritée : un ancien instantané vide ne compte pas comme référence
                baseline = self.db.execute("SELECT count(*) FROM snapshots WHERE count > 0").fetchone()[0] == 0
                known = {r[0] for r in self.db.execute("SELECT uid FROM catalogue_seen")}
                added = [u for u in uids if u not in known]
                cur = self.db.execute("INSERT INTO snapshots (ts, fingerprint, count, added) VALUES (?,?,?,?)",
                                      (now, fp, len(uids), 0 if baseline else len(added)))
                sid = cur.lastrowid
                self.db.executemany("INSERT OR IGNORE INTO catalogue_seen (uid, first_seen, snapshot) VALUES (?,?,?)",
                                    [(u, now, sid) for u in added])
                self.db.execute("DELETE FROM snapshots WHERE count = 0 OR (id NOT IN (SELECT min(id) FROM snapshots "
                                "WHERE count > 0) AND id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 50))")
                self._commit()
            except sqlite3.Error as e:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StoreError(type(e).__name__)
        return {"changed": True, "baseline": baseline, "added": 0 if baseline else len(added)}

    def novelty(self):
        def q(db):
            return (db.execute("SELECT min(id) FROM snapshots WHERE count > 0").fetchone()[0],
                    db.execute("SELECT uid, first_seen, snapshot FROM catalogue_seen").fetchall())
        base, rows = self._read(q)
        return {u: {"first_seen": fs, "new": base is not None and snap > base} for u, fs, snap in rows}

    def snapshot_stats(self):
        n, last, added = self._read(lambda db: db.execute(
            "SELECT count(*), max(ts), (SELECT added FROM snapshots WHERE count > 0 ORDER BY id DESC LIMIT 1) "
            "FROM snapshots WHERE count > 0").fetchone())
        return {"count": n, "last_at": iso(last), "last_added": added or 0}


# --------------------------------------------------------------------------- moteur
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def genres_of(g):
    out = []
    for x in re.split(r"[,/·|]", clean_text(g)):
        x = re.sub(r"\s+", " ", x).strip()
        if x and x not in out:
            out.append(x[:40])
    return out[:4]


def fr_num(v, dec=None):
    if dec is not None:
        return ("%.*f" % (dec, v)).replace(".", ",")
    s = "%d" % v
    groups = []
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    return " ".join(groups)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def quality(x):
    """-> (palier de confiance 0..3, note, votes, fournisseur). IMDb sinon TMDb apparié ; jamais de moyenne."""
    r, v = _num(x.get("imdb_rating")), _num(x.get("imdb_votes"))
    if r is not None and v and v > 0:
        tier = 3 if v >= 10000 else 2 if v >= 1000 else 1
        return tier, float(r), int(v), "IMDb"
    r, v = _num(x.get("tmdb_rating")), _num(x.get("tmdb_votes"))
    if x.get("tmdb_match_status") == "matched" and r is not None and v and v > 0:
        tier = 3 if v >= 1000 else 2 if v >= 100 else 1
        return tier, float(r), int(v), "TMDb"
    return 0, 0.0, 0, None


def public_item(x, uid, reasons, added_at=None):
    ir, iv = _num(x.get("imdb_rating")), _num(x.get("imdb_votes"))
    tr, tv = _num(x.get("tmdb_rating")), _num(x.get("tmdb_votes"))
    tm = x.get("tmdb_match_status") == "matched" and tr is not None and tv and tv > 0
    return {"id": uid, "title": clean_text(x.get("title"), 200), "year": x.get("year"), "genres": genres_of(x.get("genre")),
            "imdb": {"rating": ir, "votes": iv} if ir is not None else None,
            "tmdb": {"rating": tr, "votes": tv} if tm else None,
            "reasons": reasons, "added_at": added_at}


def recommend(items, source, profile, novelty, now, absent=frozenset(), row_max=ROW_MAX):
    """Fonction pure et déterministe. items = catalogue accepté (clean_item), profile = Store.profile(),
    novelty = Store.novelty(), absent = site_id dont l'absence est prouvée par le routage.
    -> {rows, ranked, cold_start, note, profile}."""
    year_now = time.gmtime(now).tm_year
    by_uid = {}
    for x in items:
        u = uid_for(source, x)
        if u and u not in by_uid and str(x.get("site_id")) not in absent and isinstance(x.get("title"), str):
            by_uid[u] = x

    # goût appris : poids par genre (+1 J'aime, -1 Pas pour moi, +0,5 À voir plus tard). Vu seul : neutre.
    # Uniquement des marques explicites : ouvrir, parcourir ou le temps écoulé ne changent jamais le goût.
    # Genres rapprochés sans casse ni accents (« animation » = « Animation ») ; libellé = 1re forme rencontrée.
    weights, labels, liked = {}, {}, []
    for u in sorted(profile):
        p = profile[u]
        g = genres_of(p.get("genre") or (by_uid.get(u) or {}).get("genre"))
        w = (1.0 if p.get("opinion") == "like" else -1.0 if p.get("opinion") == "dislike" else 0.0) + \
            (0.5 if p.get("watchlist") else 0.0)
        for gg in g:
            k = norm(gg)
            labels.setdefault(k, gg)
            weights[k] = weights.get(k, 0.0) + w
        if p.get("opinion") == "like" and g:
            liked.append((-(p.get("opinion_at") or 0), u,
                          clean_text(p.get("title") or (by_uid.get(u) or {}).get("title"), 200), g))
    liked.sort()
    cold = not any(p.get("opinion") for p in profile.values())

    def excluded(u):
        p = profile.get(u) or {}
        return p.get("seen") or p.get("opinion") == "dislike" or (p.get("snooze_until") or 0) > now

    cands = []
    for u in sorted(by_uid):
        if excluded(u):
            continue
        x = by_uid[u]
        g = genres_of(x.get("genre"))
        taste = round(sum(weights.get(k, 0.0) for k in set(norm(gg) for gg in g)), 3)
        tier, rating, votes, prov = quality(x)
        nv = novelty.get(u) or {}
        is_new = bool(nv.get("new")) and now - (nv.get("first_seen") or 0) <= NEW_WINDOW_S
        recent = isinstance(x.get("year"), int) and x["year"] >= year_now - 1
        reasons = []
        p = profile.get(u) or {}
        if p.get("watchlist"):
            reasons.append({"code": "watchlist", "text": "Dans ta liste « À voir plus tard »"})
        if taste > 0:
            gk = set(norm(gg) for gg in g)
            for _, lu, lt, lg in liked:
                if lu != u and gk & set(norm(x) for x in lg):
                    reasons.append({"code": "taste", "text": "Parce que tu as aimé « %s »" % lt})
                    break
        if prov:
            reasons.append({"code": "quality", "text": "%s %s/10 · %s votes" % (prov, fr_num(rating, 1), fr_num(votes))})
        if is_new:
            reasons.append({"code": "new", "text": "Ajouté au catalogue le %s" % time.strftime(
                "%d/%m/%Y", time.gmtime(nv["first_seen"]))})
        elif recent:
            reasons.append({"code": "recent", "text": "Sorti en %d" % x["year"]})
        key = (-taste, -tier, -rating, -votes, 0 if is_new else 1, -(nv.get("first_seen") or 0) if is_new else 0,
               -(x.get("year") or 0) if recent else 0, norm(clean_text(x["title"])), u)
        cands.append({"uid": u, "x": x, "g": g, "taste": taste, "tier": tier, "rating": rating, "votes": votes,
                      "new": is_new, "first_seen": nv.get("first_seen") if is_new else None, "recent": recent,
                      "wl": bool(p.get("watchlist")), "wl_at": p.get("watchlist_at") or 0, "key": key,
                      "reasons": reasons})
    cands.sort(key=lambda c: c["key"])

    # diversité : évite deux genres principaux identiques d'affilée, sans dépasser un goût plus fort
    ranked, rest = [], list(cands)
    while rest:
        pick = 0
        if ranked and ranked[-1]["g"][:1] and rest[0]["g"][:1] == ranked[-1]["g"][:1]:
            for i in range(1, min(len(rest), DIVERSITY_LOOKAHEAD + 1)):
                if rest[i]["taste"] != rest[0]["taste"]:
                    break
                if rest[i]["g"][:1] != ranked[-1]["g"][:1]:
                    pick = i
                    break
        ranked.append(rest.pop(pick))

    def pub(c):
        return public_item(c["x"], c["uid"], list(c["reasons"]),
                           time.strftime("%Y-%m-%d", time.gmtime(c["first_seen"])) if c["first_seen"] else None)

    rows, used = [], set()

    def add_row(rid, title, seq, cap):
        out = []
        for c in seq:
            if c["uid"] in used:
                continue
            out.append(c)
            used.add(c["uid"])
            if len(out) >= cap:
                break
        if out:
            rows.append({"id": rid, "title": title, "items": [pub(c) for c in out]})

    # Chaque rangée choisit ses membres selon son critère, mais l'ordre est TOUJOURS : goût appris d'abord,
    # puis le critère propre de la rangée, puis la clé globale. Une rangée automatique ne peut donc jamais
    # placer un genre rejeté devant un genre neutre ou aimé. Seule exception : la liste « À voir plus
    # tard », choix explicite film par film (son meilleur élément, par goût, reste « Le choix de ce soir »).
    wl = [c for c in ranked if c["wl"]]
    add_row("tonight", ROW_TITLES["tonight"], (wl or ranked)[:1], 1)
    add_row("watchlist", ROW_TITLES["watchlist"], sorted(wl, key=lambda c: (-c["wl_at"], c["key"])), row_max)
    for _, lu, lt, lg in liked:
        # même genre qu'un J'aime ET goût net positif (sinon l'explication « Parce que tu as aimé » serait fausse)
        lk = set(norm(x) for x in lg)
        seq = [c for c in cands if c["uid"] != lu and c["taste"] > 0 and lk & set(norm(x) for x in c["g"])
               and c["uid"] not in used]
        if seq:
            seq.sort(key=lambda c: c["key"])
            add_row("because", "Parce que tu as aimé « %s »" % lt, seq, row_max)
            break
    new = sorted([c for c in cands if c["new"]], key=lambda c: (-c["taste"], -c["first_seen"], c["key"][1:]))
    if not new:
        new = sorted([c for c in cands if c["recent"]], key=lambda c: c["key"])
    add_row("new", ROW_TITLES["new"], new, row_max)
    top = sorted([c for c in cands if c["tier"] >= 2], key=lambda c: (-c["taste"], -c["rating"], -c["votes"], c["key"]))
    add_row("top", ROW_TITLES["top"], top, row_max)
    add_row("more", ROW_TITLES["more"], ranked, row_max)

    if cold:
        note = ("Ton profil est encore vide : sélection d'après les notes IMDb ou TMDb (avec leur nombre de votes) "
                "et les nouveautés. Dis « J'aime » ou « Pas pour moi » pour personnaliser.")
    else:
        note = "Sélection d'après tes « J'aime » et « Pas pour moi », puis les notes IMDb ou TMDb et les nouveautés."
    return {"rows": rows, "ranked": [pub(c) for c in ranked], "cold_start": cold, "note": note,
            "profile": {"genres": {labels[k]: weights[k] for k in sorted(weights) if weights[k]}}}


def ui_data(r, items, source):
    """Rangées pour la vue kiosque locale (127.0.0.1) : + affiche https et résumé, toujours sans lien de fiche."""
    by_uid = {}
    for x in items:
        u = uid_for(source, x)
        if u and u not in by_uid:
            by_uid[u] = x
    rows = []
    for w in r["rows"]:
        out = []
        for p in w["items"]:
            x = by_uid.get(p["id"]) or {}
            poster = x.get("poster")
            d = dict(p, poster=poster if isinstance(poster, str) and poster.startswith("https://") and len(poster) < 600
                     else None, description=_strip_urls(x.get("description"), 400))
            out.append(d)
        rows.append({"id": w["id"], "title": w["title"], "items": out})
    return {"rows": rows, "cold_start": r["cold_start"], "note": r["note"], "buttons": [list(b) for b in BUTTONS]}


def _strip_urls(s, n):
    s = re.sub(r"\s+", " ", clean_text(s)).strip()
    return s[:n] or None


def marks_for(profile, uids, now):
    out = {}
    for u in uids:
        p = profile.get(u)
        out[u] = dict(EMPTY_MARK) if not p else {"opinion": p.get("opinion"), "seen": bool(p.get("seen")),
                                                  "watchlist": bool(p.get("watchlist")),
                                                  "not_tonight": bool((p.get("snooze_until") or 0) > now)}
    return out


# --------------------------------------------------------------------------- navigation télécommande
TOGGLES = {"like": ("opinion", "like", "clear-opinion"), "dislike": ("opinion", "dislike", "clear-opinion"),
           "seen": ("seen", True, "unseen"), "watchlist": ("watchlist", True, "unwatchlist"),
           "not-tonight": ("not_tonight", True, "clear-not-tonight")}
MESSAGES = {
    "like": "C'est noté : tu as aimé « %s ».", "dislike": "« %s » ne te sera plus proposé (Pas pour moi).",
    "clear-opinion": "Avis retiré pour « %s ».", "seen": "« %s » marqué comme déjà vu.",
    "unseen": "« %s » n'est plus marqué comme vu.", "watchlist": "« %s » ajouté à « À voir plus tard ».",
    "unwatchlist": "« %s » retiré de « À voir plus tard ».",
    "not-tonight": "« %s » mis de côté pour ce soir seulement (ce n'est pas un « Pas pour moi »).",
    "clear-not-tonight": "« %s » de nouveau proposé ce soir.",
}


class Nav:
    """Curseur rangée/colonne + panneau d'actions. Retourne ('stay',) | ('end',) | ('open', item) |
    ('act', uid, action). Aucun effet de bord : le pont applique les actions."""

    def __init__(self, rows, marks):
        self.rows, self.marks = rows, marks
        self.row, self.cols, self.panel, self.btn = 0, [0] * len(rows), False, 0

    @property
    def col(self):
        return self.cols[self.row] if self.rows else 0

    def current(self):
        if not self.rows:
            return None
        return self.rows[self.row]["items"][self.col]

    def key(self, k):
        if not self.rows:
            return ("end",) if k == "back" else ("stay",)
        if self.panel:
            if k == "back":
                self.panel = False
            elif k == "left":
                self.btn = max(0, self.btn - 1)
            elif k == "right":
                self.btn = min(len(BUTTONS) - 1, self.btn + 1)
            elif k == "ok":
                it = self.current()
                b = BUTTONS[self.btn][0]
                if b == "watch":
                    return ("open", it)
                field, on, off = TOGGLES[b]
                m = self.marks.get(it["id"]) or EMPTY_MARK
                return ("act", it["id"], off if m.get(field) == on else b)
            return ("stay",)
        n = len(self.rows[self.row]["items"])
        if k == "back":
            return ("end",)
        if k == "ok":
            self.panel, self.btn = True, 0
        elif k == "left":
            self.cols[self.row] = max(0, self.col - 1)
        elif k == "right":
            self.cols[self.row] = min(n - 1, self.col + 1)
        elif k == "up":
            self.row = max(0, self.row - 1)
        elif k == "down":
            self.row = min(len(self.rows) - 1, self.row + 1)
        return ("stay",)

    def view(self):
        cur = sum(len(r["items"]) for r in self.rows[:self.row]) + self.col if self.rows else 0
        return {"row": self.row, "col": self.col, "panel": self.panel, "btn": self.btn, "cursor": cur}


# --------------------------------------------------------------------------- adaptateur du pont
class Bridge:
    """Crochets pour opentv_bridge.Controller, appelés HORS de son verrou global. Base ouverte paresseusement.
    Contention/corruption SQLite : attente bornée (BUSY_TIMEOUT_MS) puis disjoncteur monotone
    (STORE_COOLDOWN_S) : profil vide et aucune requête SQLite pendant le refroidissement, un seul journal par
    panne. Les écritures côté Films (instantané du catalogue, signal « ouvert ») passent par UNE file bornée
    traitée par au plus un thread ; la tuile « Pour toi » reste utilisable sans base."""

    def __init__(self, db_path, source, log, clock=time.time, mono=time.monotonic):
        if not valid_source(source):   # espace de noms logique (FORYOU_SOURCE_ID), jamais un domaine
            raise ValueError("bad_source_id")
        self.db_path, self.source, self.log, self.clock, self.mono = db_path, source, log, clock, mono
        self._store, self._lk = None, threading.Lock()
        self._down_until, self._down = 0.0, False
        self._q, self._cv, self._worker, self._busy = collections.deque(), threading.Condition(), None, False

    def reset(self, db_path=None):
        self.drain(2.0)
        with self._lk:
            st, self._store = self._store, None
            self.db_path = db_path or self.db_path
            self._down_until, self._down = 0.0, False
        if st is not None:
            with st.lock:   # attend l'opération en cours (bornée par BUSY_TIMEOUT_MS)
                st.close()

    # -- disjoncteur
    def _trip(self, op, e):
        with self._lk:
            self._down_until = self.mono() + STORE_COOLDOWN_S
            first, self._down = not self._down, True
        if first:   # une seule ligne par panne (pas une par appel ni par refroidissement)
            self.log("foryou_store_error", op=op, error=(str(e) or type(e).__name__)[:40], cooldown_s=STORE_COOLDOWN_S)

    def call(self, fn, *a, **k):
        """Opération du magasin, ou None (panne/refroidissement) : jamais d'exception, attente bornée."""
        with self._lk:
            if self.mono() < self._down_until:
                return None
            st = self._store
        if st is None:
            try:
                st = Store(self.db_path, clock=self.clock)
            except StoreError as e:
                self._trip(fn, e)
                return None
            with self._lk:
                if self._store is None:
                    self._store = st
                else:   # ouverture concurrente : garde la première
                    st.close()
                    st = self._store
        try:
            r = getattr(st, fn)(*a, **k)
        except Exception as e:   # base fermée/verrouillée/corrompue : jamais propagé au contrôleur
            self._trip(fn, e)
            return None
        with self._lk:
            recovered, self._down = self._down, False
        if recovered:
            self.log("foryou_store_recovered", op=fn)
        return r

    # -- file asynchrone bornée (Films : aucune attente SQLite sur le chemin de la télécommande)
    def _enqueue(self, task):
        with self._cv:
            if task[0] == "observe":   # instantanés fusionnés : seul le plus récent compte
                for i, t in enumerate(self._q):
                    if t[0] == "observe":
                        self._q[i] = task
                        break
                else:
                    self._q.append(task)
            else:
                self._q.append(task)
            while len(self._q) > ASYNC_MAX:   # file pleine : le plus ancien signal est abandonné
                self._q.popleft()
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True, name="foryou-writer")
                self._worker.start()

    def _work(self):
        while True:
            with self._cv:
                if not self._q:
                    self._worker, self._busy = None, False
                    self._cv.notify_all()
                    return
                task, self._busy = self._q.popleft(), True
            try:
                self.call(*task[1:]) if task[0] == "call" else self.call("observe_catalogue", task[1])
            except Exception as e:   # filet de sécurité : le thread ne meurt jamais avec des tâches en file
                self.log("foryou_async_error", error=type(e).__name__)

    def pending(self):
        with self._cv:
            return len(self._q)

    def drain(self, timeout=2.0):
        """Attend la fin des écritures asynchrones (tests, arrêt). -> True si la file est vide."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._q and self._worker is None, timeout=timeout)

    def _by_uid(self, items):
        out = {}
        for x in items:
            u = uid_for(self.source, x)
            if u and u not in out:
                out[u] = x
        return out

    def observe(self, items):
        uids = list(self._by_uid(items))
        if uids:   # catalogue vide : jamais une référence, aucune écriture
            self.call("observe_catalogue", uids)

    def observe_async(self, items):
        uids = list(self._by_uid(items))
        if uids:
            self._enqueue(("observe", uids))

    def session(self, items, absent=frozenset()):
        """Sélection figée pour une séance : {nav, by_uid, data}. Bornée même si la base est verrouillée."""
        self.observe(items)
        profile, novelty, now = self.call("profile") or {}, self.call("novelty") or {}, self.clock()
        r = recommend(items, self.source, profile, novelty, now, absent=absent)
        shown = [p["id"] for w in r["rows"] for p in w["items"]]
        return {"nav": Nav(r["rows"], marks_for(profile, shown, now)), "by_uid": self._by_uid(items),
                "data": ui_data(r, items, self.source)}

    @staticmethod
    def nav_key(fy, k):
        """Navigation pure (sous le verrou du contrôleur, sans base) ->
        ('end',) | ('open', élément du catalogue) | ('act', uid, action) | ('stay', '')."""
        act = fy["nav"].key(k)
        if act[0] == "open":
            return "open", fy["by_uid"][act[1]["id"]]
        if act[0] == "act":
            return act
        return act if act[0] == "end" else ("stay", "")

    def act(self, fy, uid, action):
        """Écriture d'une action (HORS verrou du contrôleur, bornée). -> (nouvel état | None, message)."""
        x = fy["by_uid"].get(uid) or {}
        r = self.call("apply", uid, action, origin="tv", title=x.get("title"), genre=x.get("genre"))
        if r is None:
            return None, "Action non enregistrée : historique indisponible."
        self.log("foryou_feedback", action=action, changed=r["changed"])   # jamais le titre ni l'identifiant
        return r["state"], MESSAGES[action] % clean_text(x.get("title"), 200)

    def key(self, fy, k):
        """Compatibilité (navigation + écriture synchrones) : -> ('end',) | ('open', élément) | ('stay', message)."""
        act = self.nav_key(fy, k)
        if act[0] != "act":
            return act
        state, msg = self.act(fy, act[1], act[2])
        if state is not None:
            fy["nav"].marks[act[1]] = state
        return "stay", msg

    def opened(self, item, app):
        """Signal de séance « ouvert » (Films ou Pour toi) : jamais « vu »."""
        u = uid_for(self.source, item)
        if u:
            self.call("record_signal", u, "opened", app=app)

    def opened_async(self, item, app):
        u = uid_for(self.source, item)
        if u:
            self._enqueue(("call", "record_signal", u, "opened", app))


# --------------------------------------------------------------------------- CLI agent (JSON local)
EXIT_OK, EXIT_USAGE, EXIT_STORE, EXIT_NOT_FOUND = 0, 2, 3, 4
ENV_KEYS = ("STATE_DIR", "CATALOGUE_FILE", "FORYOU_DB", "FORYOU_SOURCE_ID", "DETAIL_RE")   # SITE_ROOT : inutile ici


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(message[:200])

    def exit(self, status=0, message=None):
        raise _Usage((message or "help").strip()[:200])


def _bounded(lo, hi):
    def f(s):
        if not re.match(r"^[0-9]{1,6}$", s or "") or not lo <= int(s) <= hi:
            raise argparse.ArgumentTypeError("entier attendu dans [%d, %d]" % (lo, hi))
        return int(s)
    return f


def _query(s):
    s = (s or "").strip()
    if not 1 <= len(s) <= 80 or any(ord(c) < 32 for c in s):
        raise argparse.ArgumentTypeError("1 à 80 caractères imprimables")
    if URLISH_RE.search(s):   # refusée, jamais réaffichée : on cherche un titre, pas une adresse
        raise argparse.ArgumentTypeError("requête refusée : ressemble à une URL")
    return s


def _uid_arg(s):
    if not valid_uid(s):
        raise argparse.ArgumentTypeError("identifiant attendu : <espace>/<site_id>, ex. films/17397")
    return s


def build_parser():
    p = _Parser(prog="opentv_foryou.py", add_help=False)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("status", add_help=False)
    r = sub.add_parser("recommend", add_help=False)
    r.add_argument("--limit", type=_bounded(1, 50), default=10)
    r.add_argument("--offset", type=_bounded(0, 2000), default=0)
    r.add_argument("--rows", action="store_true")
    s = sub.add_parser("search", add_help=False)
    s.add_argument("--query", type=_query, required=True)
    s.add_argument("--limit", type=_bounded(1, 50), default=10)
    s.add_argument("--offset", type=_bounded(0, 2000), default=0)
    f = sub.add_parser("feedback", add_help=False)
    f.add_argument("--id", type=_uid_arg, required=True)
    f.add_argument("--action", choices=sorted(ACTIONS), required=True)
    h = sub.add_parser("history", add_help=False)
    h.add_argument("--list", choices=("events", "opened", "liked", "disliked", "seen", "watchlist", "not-tonight"),
                   default="events")
    h.add_argument("--limit", type=_bounded(1, 100), default=20)
    h.add_argument("--offset", type=_bounded(0, 5000), default=0)
    return p


def _env_file(home):
    """Lit STATE_DIR, FORYOU_SOURCE_ID… de l'env du service (même configuration que le pont), sans rien exécuter."""
    out = {}
    try:
        with open(os.path.join(home, ".config/systemd/user/opentv-bridge.env"), encoding="utf-8") as f:
            for line in f.read(65536).splitlines():
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":   # guillemets systemd : KEY="valeur"
                    v = v[1:-1]
                if k in ENV_KEYS and v:
                    out[k] = v
    except OSError:
        pass
    return out


def _bridge():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opentv_bridge.py")
    spec = importlib.util.spec_from_file_location("opentv_bridge_for_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _out(out, obj, code):
    """Seule sortie de la CLI : nettoyage global (aucune URL, quelle que soit l'origine de la chaîne)."""
    out.write(json.dumps(scrub(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return code


def main(argv, env=None, now=None, out=None):
    out = out or sys.stdout
    env = dict(os.environ if env is None else env)
    home = env.get("HOME") or os.path.expanduser("~")
    for k, v in _env_file(home).items():
        env.setdefault(k, v)
    try:
        a = build_parser().parse_args(argv[1:])
    except _Usage as e:
        return _out(out, {"ok": False, "error": "usage", "detail": str(e)}, EXIT_USAGE)
    now = time.time() if now is None else now
    state_dir = env.get("STATE_DIR") or os.path.join(home, ".local/state/opentv-bridge")
    db_path = env.get("FORYOU_DB") or os.path.join(state_dir, "foryou.sqlite3")
    cat_path = env.get("CATALOGUE_FILE") or os.path.join(state_dir, "catalogue.json")
    try:
        source = source_id(env)
    except ValueError:
        return _out(out, {"ok": False, "error": "config", "detail": "FORYOU_SOURCE_ID invalide ([a-z][a-z0-9-]{1,23})"},
                    EXIT_USAGE)
    B = _bridge()
    detail_re = re.compile(env.get("DETAIL_RE", B.Config({}).detail_re.pattern))
    items, generated_at, _ = B.load_catalogue_meta(cat_path, detail_re)
    by_uid = {}
    for x in items:
        u = uid_for(source, x)
        if u and u not in by_uid:
            by_uid[u] = x
    try:
        store = Store(db_path, clock=lambda: now)
    except StoreError:
        store = None
    try:
        return _run(a, out, now, store, source, items, by_uid, generated_at)
    finally:
        if store is not None:
            store.close()


def _cli_item(x, u, reasons, mark, added_at=None):
    d = public_item(x, u, reasons, added_at)
    d["description"] = _strip_urls(x.get("description"), 300)
    d["state"] = mark
    return d


def _run(a, out, now, store, source, items, by_uid, generated_at):
    profile, novelty = {}, {}
    if store is not None:
        try:
            if a.cmd in ("status", "recommend"):
                store.observe_catalogue(list(by_uid))
            profile, novelty = store.profile(), store.novelty()
        except StoreError:
            store = None
    st = "ok" if store is not None else "unavailable"

    if a.cmd == "status":
        return _out(out, {"ok": True, "schema": SCHEMA_VERSION, "store": st, "source": source,
                          "catalogue": {"items": len(items), "with_stable_id": len(by_uid), "generated_at": generated_at},
                          "profile": store.counts(now) if store else None,
                          "snapshots": store.snapshot_stats() if store else None}, EXIT_OK)

    if a.cmd == "recommend":
        r = recommend(items, source, profile, novelty, now)
        marks = marks_for(profile, by_uid, now)

        def full(p):
            x = by_uid[p["id"]]
            return _cli_item(x, p["id"], p["reasons"], marks[p["id"]], p["added_at"])
        res = {"ok": True, "store": st, "cold_start": r["cold_start"], "note": r["note"],
               "tonight": full(r["rows"][0]["items"][0]) if r["rows"] else None}
        if a.rows:
            res["rows"] = [{"id": w["id"], "title": w["title"], "items": [full(p) for p in w["items"][:a.limit]]}
                           for w in r["rows"]]
        else:
            page = r["ranked"][a.offset:a.offset + a.limit]
            res.update(items=[full(p) for p in page], offset=a.offset, limit=a.limit, total=len(r["ranked"]),
                       has_more=a.offset + a.limit < len(r["ranked"]))
        return _out(out, res, EXIT_OK)

    if a.cmd == "search":
        q = norm(a.query)
        hits = []
        for u, x in by_uid.items():
            t = norm(clean_text(x.get("title")))
            if q and q in t:
                hits.append((0 if t == q else 1 if t.startswith(q) else 2, t, u))
        hits.sort()
        page = hits[a.offset:a.offset + a.limit]
        marks = marks_for(profile, [h[2] for h in page], now)
        return _out(out, {"ok": True, "store": st, "query": a.query, "total": len(hits), "offset": a.offset,
                          "limit": a.limit, "has_more": a.offset + a.limit < len(hits),
                          "items": [_cli_item(by_uid[h[2]], h[2], [], marks[h[2]]) for h in page]}, EXIT_OK)

    if a.cmd == "feedback":
        if a.id in by_uid:
            x = by_uid[a.id]
        elif a.action in REMOVALS and store is not None and a.id in profile:
            # retrait sur un film déjà annoté qui a quitté le catalogue : permis (titre mémorisé, jamais réécrit)
            x = {"title": profile[a.id].get("title")}
        elif a.action in REMOVALS and store is None:
            return _out(out, {"ok": False, "error": "store_unavailable"}, EXIT_STORE)
        else:   # écriture positive ou identifiant jamais vu : uniquement sur le catalogue accepté
            return _out(out, {"ok": False, "error": "unknown_id"}, EXIT_NOT_FOUND)
        if store is None:
            return _out(out, {"ok": False, "error": "store_unavailable"}, EXIT_STORE)
        try:
            r = store.apply(a.id, a.action, origin="cli", title=x.get("title"), genre=x.get("genre"))
        except StoreError:
            return _out(out, {"ok": False, "error": "store_unavailable"}, EXIT_STORE)
        return _out(out, {"ok": True, "id": a.id, "title": clean_text(x.get("title"), 200), "action": a.action,
                          "changed": r["changed"], "state": r["state"]}, EXIT_OK)

    # history
    if store is None:
        return _out(out, {"ok": False, "error": "store_unavailable"}, EXIT_STORE)
    if a.list == "events":
        rows = store.history("events", a.limit, a.offset)
    elif a.list == "opened":
        rows = store.history("signals", a.limit, a.offset)
    else:
        def keep(p):
            return {"liked": p["opinion"] == "like", "disliked": p["opinion"] == "dislike", "seen": p["seen"],
                    "watchlist": p["watchlist"], "not-tonight": (p["snooze_until"] or 0) > now}[a.list]
        sel = sorted(((-(p["updated_at"] or 0), u) for u, p in profile.items() if keep(p)))
        rows = [{"id": u, "title": profile[u]["title"], "updated_at": iso(-t), "in_catalogue": u in by_uid}
                for t, u in sel[a.offset:a.offset + a.limit]]
    return _out(out, {"ok": True, "list": a.list, "offset": a.offset, "limit": a.limit, "items": rows}, EXIT_OK)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
