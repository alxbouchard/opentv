#!/usr/bin/env python3
"""opentv-bridge — côté NanoPi du contrat native/NANOPI-API.md (API v1).

Rôle : recevoir d'OpenTV (TV 192.168.2.14) une clé d'appli et des touches bornées, afficher sur HDMI 1
un catalogue local (métadonnées publiques) dans un Chromium kiosque, ouvrir la page publique d'un film,
et revenir au tableau de bord (muse-display) sur Retour, fermeture, inactivité, panne ou perte de la TV.

Invariants :
- Bibliothèque standard Python 3.13 uniquement ; utilisateur `alex`, jamais root.
- muse-display / musegadget / muse-cec-pinmux ne sont jamais arrêtés, signalés ni modifiés :
  le compositeur prend le maître DRM le temps de la session ; à sa sortie, l'fbdev du tableau de bord
  redevient visible. Seule lecture : `systemctl --user is-active <DASHBOARD_UNIT>`.
- OpenTV n'envoie jamais d'URL. Les URL ouvertes sont reconstruites ici : origine HTTPS découverte +
  /<préfixe>/ + chemin de fiche validé (b/<jeton>/<id>). Aucun flux vidéo n'est extrait, relayé ou contourné.
- Migration de domaine (ex. fakrov.com -> wadrox.com) : seulement annoncée par la racine de confiance et
  validée de bout en bout ; le lecteur suit le site_id stable via une table de routage publiée par le site.
"""
import html.parser
import http.server
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = 1
KEYS = ("up", "down", "left", "right", "ok", "back")
STATES = ("starting", "catalogue", "player", "dashboard")
APP_RE = re.compile(r"^[a-z0-9-]{1,24}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REASON_RE = re.compile(r"^[a-z0-9-]{1,24}$")
PREFIX_RE = re.compile(r"^[a-z0-9]{5,16}$")
MAX_BODY = 2 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- journal
_LOG_LOCK = threading.Lock()


def log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": event}
    rec.update(fields)
    with _LOG_LOCK:
        sys.stdout.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- configuration
def _num(env, name, default, cast=float):
    raw = env.get(name, "")
    try:
        return cast(raw) if raw != "" else default
    except ValueError:
        raise SystemExit("config: %s invalide" % name)


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = os.path.expanduser("~")
        self.bind = env.get("BIND", "0.0.0.0")
        self.port = _num(env, "PORT", 8097, int)
        self.ui_port = _num(env, "UI_PORT", 8098, int)
        self.allow_ips = frozenset(x.strip() for x in env.get("ALLOW_IPS", "192.168.2.14").split(",") if x.strip())
        # 20/s : OpenTV compte un 429 comme échec (3 d'affilée = fin) ; une touche maintenue dépasse 10/s
        self.rate = _num(env, "RATE_PER_S", 20.0)
        self.state_dir = env.get("STATE_DIR") or os.path.join(home, ".local/state/opentv-bridge")
        self.catalogue_file = env.get("CATALOGUE_FILE") or os.path.join(self.state_dir, "catalogue.json")
        self.site_root = env.get("SITE_ROOT", "https://fakrov.com/")
        # Gate humain explicite : l'installation et un simple changement de backend ne doivent jamais
        # activer par accident une source externe dont l'autorisation n'a pas été confirmée.
        self.source_approved = env.get("SOURCE_APPROVED", "0") == "1"
        # forme générique : le jeton (fakrov, wadrox, …) est revérifié contre l'époque validée à l'ouverture
        self.detail_re = re.compile(env.get("DETAIL_RE", r"^b/[a-z][a-z0-9]{2,23}/[0-9]{1,12}$"))
        self.apps = frozenset(x for x in env.get("APPS", "catalogue").split(",") if APP_RE.match(x))
        self.cols = _num(env, "COLS", 6, int)
        self.catalogue_idle_s = _num(env, "CATALOGUE_IDLE_S", 300.0)
        self.player_max_s = _num(env, "PLAYER_MAX_S", 4 * 3600.0)
        self.heartbeat_s = _num(env, "HEARTBEAT_S", 30.0)
        self.start_timeout_s = _num(env, "START_TIMEOUT_S", 45.0)
        self.prefix_ttl_s = _num(env, "PREFIX_TTL_S", 6 * 3600.0)
        self.prefix_grace_s = _num(env, "PREFIX_GRACE_S", 24 * 3600.0)
        self.fetch_timeout_s = _num(env, "FETCH_TIMEOUT_S", 4.0)
        # Pas de robot en arrière-plan par défaut. Un rafraîchissement périodique exige à la fois
        # SOURCE_APPROVED=1 et une valeur > 0 explicitement configurée.
        self.catalogue_refresh_s = _num(env, "CATALOGUE_REFRESH_S", 0.0)
        # Ouverture de session : revalidation (rotation/migration) si la dernière date de plus de SESSION_CHECK_S.
        self.session_check_s = _num(env, "SESSION_CHECK_S", 300.0)
        # Moniteur d'adresse/routage en arrière-plan (racine de confiance seulement ; 0 = désactivé).
        # N'interroge ni catalogue ni IMDb ; exige aussi SOURCE_APPROVED=1. Minimum 300 s.
        self.routing_refresh_s = _num(env, "ROUTING_REFRESH_S", 0.0)
        if 0 < self.routing_refresh_s < 300:
            raise SystemExit("config: ROUTING_REFRESH_S doit valoir 0 ou >= 300")
        self.route_max_pages = _num(env, "ROUTE_MAX_PAGES", 25, int)
        self.route_budget_s = _num(env, "ROUTE_BUDGET_S", 20.0)
        self.display = env.get("DISPLAY_BACKEND", "chromium")   # chromium | off | null (tests)
        if self.display not in ("chromium", "off", "null"):
            raise SystemExit("config: DISPLAY_BACKEND invalide")
        self.browser_cmd = shlex.split(env.get("BROWSER_CMD", "chromium"))
        self.browser_flags = shlex.split(env.get("BROWSER_FLAGS", " ".join(DEFAULT_BROWSER_FLAGS)))
        # vide = pas de compositeur (navigateur sur un affichage existant ou headless pour les tests)
        self.compositor_cmd = env.get("COMPOSITOR_CMD", DEFAULT_COMPOSITOR)
        self.output_mode = env.get("OUTPUT_MODE", "1920x1080")
        self.output_name = env.get("OUTPUT_NAME", "HDMI-A-1")
        self.dashboard_unit = env.get("DASHBOARD_UNIT", "muse-display.service")
        self.runtime_dir = env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()


DEFAULT_BROWSER_FLAGS = [
    "--ozone-platform=wayland", "--kiosk", "--start-fullscreen", "--window-size=1920,1080",
    "--force-device-scale-factor=1", "--no-first-run", "--no-default-browser-check", "--disable-sync",
    "--disable-extensions", "--disable-component-update", "--noerrdialogs", "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble", "--deny-permission-prompts", "--password-store=basic",
    "--autoplay-policy=no-user-gesture-required", "--disable-features=Translate,MediaRouter",
    "--disable-gpu",
]
DEFAULT_COMPOSITOR = ("weston --backend=drm --renderer=pixman --shell=kiosk --idle-time=0 "
                      "--socket={socket} --config={config}")
WESTON_INI = """[core]
idle-time=0
require-input=false

[output]
name={name}
mode={mode}
"""


# --------------------------------------------------------------------------- HTTP sortant borné
class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_fetch(url, timeout, limit=MAX_BODY):
    """GET sans suivre les redirections ; TLS vérifié ; corps borné. -> (status, headers{lower}, bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": "opentv-bridge/1", "Accept": "text/html"})
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:   # 3xx/4xx/5xx arrivent ici avec _NoRedirect
        resp = e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(type(e).__name__)
    try:
        body = resp.read(limit + 1) or b""
    except OSError as e:
        raise FetchError(type(e).__name__)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if len(body) > limit:
        raise FetchError("too_large")
    headers = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
    return resp.status if hasattr(resp, "status") else resp.code, headers, body


# --------------------------------------------------------------------------- préfixe rotatif + migration de domaine
class DiscoveryFailed(Exception):
    pass


class _Moved(Exception):
    """La racine de confiance n'offre plus de préfixe valide mais annonce explicitement un nouveau domaine."""

    def __init__(self, hosts, why):
        super().__init__(why)
        self.hosts = hosts


TOKEN_RE = re.compile(r"^[a-z][a-z0-9]{2,23}$")      # ex. « fakrov », « wadrox » (route /<p>/home/<jeton>)
NUM_ID_RE = re.compile(r"^[0-9]{1,12}$")             # identifiants de fiche (link_id) et internes (site_id)
MAX_MIGRATION_HOPS = 2       # sauts de domaine par rafraîchissement (fakrov -> wadrox -> suivant)
MAX_MIGRATION_CANDIDATES = 2
MIN_MIGRATION_DETAILS = 3    # fiches /<p>/b/<jeton>/<id> exigées sur la liste d'un nouveau domaine
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_RESERVED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".home.arpa",
                      ".test", ".invalid", ".example", ".onion", ".arpa")


def valid_public_host(h):
    """Nom d'hôte DNS public en minuscules ; refuse IP littérales, ports, noms locaux/réservés, mono-label."""
    if not isinstance(h, str) or not h or len(h) > 253 or h != h.lower() or not h.isascii():
        return False
    try:
        ipaddress.ip_address(h)
        return False
    except ValueError:
        pass
    labels = h.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(x) for x in labels) or not _TLD_RE.match(labels[-1]):
        return False
    return h != "localhost" and not any(("." + h).endswith(s) for s in _RESERVED_SUFFIXES)


def _port(u):
    try:
        return u.port
    except ValueError:
        return -1


def _segs(path):
    return [s for s in (path or "").split("/") if s]


def token_from_listing(listing_path, prefix):
    s = _segs(listing_path)
    return s[2] if len(s) == 3 and s[0] == prefix and s[1] == "home" and TOKEN_RE.match(s[2]) else None


class _Page(html.parser.HTMLParser):
    """Ancres + texte visible borné (titre compris), sans exécuter de script."""

    def __init__(self, cap=5000, text_cap=65536):
        super().__init__(convert_charrefs=True)
        self.hrefs, self.cap, self.parts, self.text_cap, self._n, self._skip = [], cap, [], text_cap, 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a" and len(self.hrefs) < self.cap:
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v.strip())

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and self._n < self.text_cap:
            self.parts.append(data)
            self._n += len(data)

    @property
    def text(self):
        return " ".join(self.parts).lower()


def _parse_page(body):
    p = _Page()
    try:
        p.feed(body.decode("utf-8", "replace"))
    except Exception:
        pass
    return p


class Resolver:
    """Découvre et valide https://<hôte>/<préfixe>/ à partir d'une racine de confiance stable (SITE_ROOT).

    Migration de domaine bornée : uniquement si la racine actuellement de confiance n'offre plus de préfixe
    valide ET publie une ancre HTTPS explicite `https://<nouvel-hôte>/` dont le nom figure aussi dans son texte
    (ex. « fakrov devient wadrox.com »). Le nouvel hôte est validé de bout en bout (racine, préfixe, page de
    liste /<p>/home/<jeton>, fiches /<p>/b/<jeton>/<id>, jeton présent dans le nom d'hôte) avant d'être adopté.
    Jamais : redirection inter-domaines, http, identifiants, port explicite, IP littérale, nom local.
    """

    def __init__(self, root, cache_path, fetch=None, clock=time.time, ttl=6 * 3600, grace=24 * 3600,
                 timeout=4.0, session_check=300.0):
        u = urllib.parse.urlsplit(root)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not host or u.username or u.password or _port(u) not in (None, 443) \
                or not valid_public_host(host):
            raise ValueError("SITE_ROOT doit être https://hôte/ sans identifiants ni port")
        self.anchor = host
        self.cache_path, self.fetch = cache_path, fetch or http_fetch
        self.clock, self.ttl, self.grace, self.timeout = clock, ttl, grace, timeout
        self.session_check = session_check
        self.lock = threading.Lock()
        self.cache = self._load()
        self._persisted_at = self.cache["validated_at"] if self.cache else 0.0

    # -- état courant (hôte migré éventuel)
    @property
    def host(self):
        c = self.cache
        return c["host"] if c else self.anchor

    @property
    def origin(self):
        return "https://" + self.host

    @property
    def root(self):
        return self.origin + "/"

    def epoch(self):
        """{origin, host, prefix, token, key} de l'époque validée courante, ou None."""
        c = self.cache
        if not c:
            return None
        return {"origin": "https://" + c["host"], "host": c["host"], "prefix": c["prefix"], "token": c["token"],
                "key": "%s/%s/%s" % (c["host"], c["prefix"], c["token"])}

    # -- cache 0600 v2 {anchor, host, prefix, token, listing_path, *_at, history} ; v1 (préfixe seul) relu
    def _load(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if not isinstance(c, dict):
                return None
            prefix = c.get("prefix")
            validated = float(c["validated_at"])
            if not isinstance(prefix, str) or not PREFIX_RE.match(prefix) or validated <= 0:
                return None
            if c.get("anchor", self.anchor) != self.anchor:   # cache d'une autre racine de confiance
                return None
            host = c.get("host") or self.anchor              # v1 : toujours l'hôte d'ancrage
            if not valid_public_host(host):
                return None
            lp = c.get("listing_path")
            if not isinstance(lp, str) or not lp.startswith("/%s/" % prefix) or len(lp) > 200:
                lp = "/%s/" % prefix
            token = c.get("token") or token_from_listing(lp, prefix)
            if token is not None and (not isinstance(token, str) or not TOKEN_RE.match(token)):
                return None
            hist = []
            for h in c.get("history") or []:
                if isinstance(h, dict) and valid_public_host(h.get("host")) and PREFIX_RE.match(str(h.get("prefix"))):
                    hist.append({"host": h["host"], "prefix": h["prefix"], "token": h.get("token")
                                 if TOKEN_RE.match(str(h.get("token"))) else None, "until": h.get("until")})
            return {"v": 2, "anchor": self.anchor, "host": host, "prefix": prefix, "token": token,
                    "listing_path": lp, "discovered_at": float(c.get("discovered_at") or validated),
                    "validated_at": validated, "checked_at": float(c.get("checked_at") or validated),
                    "migrated_at": c.get("migrated_at"), "via": [h for h in (c.get("via") or [])
                                                                  if valid_public_host(h)][:MAX_MIGRATION_HOPS],
                    "history": hist[:5]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _save(self, c):
        atomic_write_json(self.cache_path, c)
        self._persisted_at = self.clock()

    def same_origin(self, url, host=None):
        """URL absolue acceptée (http même hôte remonté en https) ou None."""
        host = host or self.host
        u = urllib.parse.urlsplit(url)
        if u.username or u.password or (u.hostname or "").lower() != host:
            return None
        port = _port(u)
        if u.scheme == "http" and port in (None, 80):
            pass
        elif u.scheme != "https" or port not in (None, 443):
            return None
        return urllib.parse.urlunsplit(("https", host, u.path or "/", u.query, ""))

    def _get(self, url, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise DiscoveryFailed("timeout")
        try:
            return self.fetch(url, min(self.timeout, left))
        except FetchError as e:
            raise DiscoveryFailed("fetch:" + str(e))

    def _follow(self, url, deadline, must_start=None, host=None):
        """Suit ≤3 redirections même hôte (et sous must_start si donné) ; exige 200 text/html."""
        host = host or self.host
        for _ in range(4):
            status, headers, body = self._get(url, deadline)
            if status in (301, 302, 303, 307, 308):
                nxt = self.same_origin(urllib.parse.urljoin(url, headers.get("location", "")), host)
                if not nxt:
                    raise DiscoveryFailed("foreign_redirect")
                if must_start and not urllib.parse.urlsplit(nxt).path.startswith(must_start):
                    raise DiscoveryFailed("redirect_out_of_prefix")
                url = nxt
                continue
            if status != 200:
                raise DiscoveryFailed("status_%d" % status)
            if not headers.get("content-type", "").lower().startswith("text/html"):
                raise DiscoveryFailed("not_html")
            return url, body
        raise DiscoveryFailed("too_many_redirects")

    def validate(self, prefix, deadline, host=None):
        """-> (listing_path, token). Identité : /<p>/home/<jeton> et fiches /<p>/b/<jeton>/<id> cohérentes."""
        host = host or self.host
        strict = host != self.anchor
        start = "/" + prefix + "/"
        final, body = self._follow("https://%s%s" % (host, start), deadline, must_start=start, host=host)
        listing = urllib.parse.urlsplit(final).path
        token = token_from_listing(listing, prefix)
        if not token:
            raise DiscoveryFailed("listing_shape")
        if strict and token not in re.split(r"[.-]", host):
            raise DiscoveryFailed("token_host_mismatch")
        details, foreign_token = set(), 0
        for href in _parse_page(body).hrefs:
            absu = self.same_origin(urllib.parse.urljoin(final, href), host)
            if not absu:
                continue
            u = urllib.parse.urlsplit(absu)
            s = _segs(u.path)
            if len(s) == 4 and s[0] == prefix and s[1] == "b" and NUM_ID_RE.match(s[3]) and not u.query:
                if s[2] == token:
                    details.add(s[3])
                else:
                    foreign_token += 1
        if foreign_token:
            raise DiscoveryFailed("token_mismatch")
        if len(details) < (MIN_MIGRATION_DETAILS if strict else 1):
            raise DiscoveryFailed("no_detail_links")
        return listing, token

    def _advertised(self, url, cur_host, text):
        """Nouvel hôte annoncé par une ancre exacte https://hôte/ citée dans le texte de la page, sinon None."""
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or u.netloc.lower() != host or u.path not in ("", "/") or u.query or u.fragment:
            return None   # http (déclassement), identifiants, port, chemin, requête : refusés
        if host == cur_host or not valid_public_host(host) or host not in text:
            return None
        return host

    def _probe(self, host, cached, deadline):
        """Préfixe valide sur `host` -> état ; sinon _Moved(hôtes annoncés) ou DiscoveryFailed."""
        tried = set()

        def attempt(p):
            tried.add(p)
            listing, token = self.validate(p, deadline, host)
            return {"host": host, "prefix": p, "token": token, "listing_path": listing}
        try:
            final, body = self._follow("https://%s/" % host, deadline, host=host)
        except DiscoveryFailed as e:
            if cached:   # racine en panne : le dernier préfixe connu de cet hôte reste vérifiable
                try:
                    return attempt(cached["prefix"])
                except DiscoveryFailed:
                    pass
            raise e
        page = _parse_page(body)
        text = page.text
        segs = _segs(urllib.parse.urlsplit(final).path)
        cands = [segs[0]] if segs and PREFIX_RE.match(segs[0]) else []
        moves = []
        for href in page.hrefs:
            joined = urllib.parse.urljoin(final, href)
            absu = self.same_origin(joined, host)
            if absu:
                s = _segs(urllib.parse.urlsplit(absu).path)
                if len(s) == 1 and PREFIX_RE.match(s[0]) and s[0] not in cands:
                    cands.append(s[0])
                continue
            h = self._advertised(joined, host, text)
            if h and h not in moves:
                moves.append(h)
        if cached and cached["prefix"] in cands:
            cands.remove(cached["prefix"])
            cands.insert(0, cached["prefix"])
        last = "no_prefix_link"
        for c in cands[:3]:   # premier candidat valide ; borne à 3 sondes
            try:
                return attempt(c)
            except DiscoveryFailed as e:
                last = str(e)
        if moves:
            raise _Moved(moves[:MAX_MIGRATION_CANDIDATES], last)
        if cached and cached["prefix"] not in tried:
            try:
                return attempt(cached["prefix"])
            except DiscoveryFailed as e:
                last = str(e)
        raise DiscoveryFailed(last)

    def _resolve(self, host, cached, deadline, hops, visited):
        visited.add(host)
        try:
            return self._probe(host, cached if cached and cached["host"] == host else None, deadline)
        except _Moved as m:
            if hops >= MAX_MIGRATION_HOPS:
                raise DiscoveryFailed("migration_hops")
            last = "migration_loop"
            for h in m.hosts:
                if h in visited:
                    continue
                log("migration_candidate", src=host, dst=h)
                try:
                    st = self._resolve(h, cached, deadline, hops + 1, visited)
                    st["via"] = [host] + st.get("via", [])
                    return st
                except DiscoveryFailed as e:
                    last = "migration:" + str(e)
                    log("migration_rejected", src=host, dst=h, why=str(e))
            raise DiscoveryFailed(last)

    def _touch(self, c, now):
        if c:   # échec : dernière époque valide conservée telle quelle (seul checked_at avance, en mémoire)
            self.cache = dict(c, checked_at=now)

    def _commit(self, c, st, now):
        moved = bool(c) and c["host"] != st["host"]
        changed = not c or moved or c["prefix"] != st["prefix"] or c.get("token") != st["token"]
        hist = list(c.get("history", [])) if c else []
        if c and changed:
            hist.insert(0, {"host": c["host"], "prefix": c["prefix"], "token": c.get("token"), "until": now})
        n = {"v": 2, "anchor": self.anchor, "host": st["host"], "prefix": st["prefix"], "token": st["token"],
             "listing_path": st["listing_path"], "discovered_at": now if changed else c["discovered_at"],
             "validated_at": now, "checked_at": now, "history": hist[:5],
             "via": st.get("via", []) if (changed or not c) else c.get("via", []),
             "migrated_at": now if moved or (not c and st["host"] != self.anchor) else (c.get("migrated_at") if c else None)}
        self.cache = n
        # époque inchangée : horodatages en mémoire ; réécriture disque au plus toutes les ttl/4
        if changed or now - self._persisted_at >= min(self.ttl / 4.0, 3600.0):
            self._save(n)
        log("prefix_ok", host=n["host"], prefix=n["prefix"], token=n["token"], changed=changed, migrated=moved)
        return n

    def refresh(self, budget=None, min_age=None, blocking=True):
        """Revalide l'époque courante (détecte rotation de préfixe et migration annoncée). Lève DiscoveryFailed.
        blocking=False : vol unique — renvoie None sans rien faire si un rafraîchissement est déjà en cours."""
        if not self.lock.acquire(blocking):
            return None
        try:
            return self._refresh_locked(budget, min_age)
        finally:
            self.lock.release()

    def _refresh_locked(self, budget, min_age):
        deadline = time.monotonic() + (budget if budget else 3 * self.timeout + 2)
        c = self.cache
        now = self.clock()
        if min_age is not None and c and now - c["checked_at"] < min_age:
            return c   # déjà vérifié par un appel concurrent
        start = c["host"] if c else self.anchor
        visited = set()
        try:
            st = self._resolve(start, c, deadline, 0, visited)
        except DiscoveryFailed as e:
            if start == self.anchor:
                self._touch(c, now)
                raise
            # hôte migré muet : retour à l'ancre de confiance, qui peut annoncer un domaine plus récent
            log("host_failed", host=start, why=str(e))
            try:
                st = self._resolve(self.anchor, c, deadline, 0, visited)
            except DiscoveryFailed:
                self._touch(c, now)
                raise
        return self._commit(c, st, now)

    def status(self):
        c = self.cache
        if not c:
            return "none"
        age = self.clock() - float(c["validated_at"])
        return "fresh" if age < self.ttl else ("stale" if age < self.grace else "expired")

    def current(self, budget=None, check=False):
        """(origine, préfixe, 'fresh'|'stale'). Réseau si aucun préfixe utilisable, ou si check=True
        (ouverture de session par l'utilisateur) et dernière vérification plus vieille que session_check."""
        st = self.status()
        if st in ("none", "expired"):
            try:
                self.refresh(budget)
            except DiscoveryFailed as e:
                log("discovery_failed", why=str(e))
                raise
        elif check and self.clock() - self.cache["checked_at"] >= self.session_check:
            try:
                self.refresh(budget, min_age=self.session_check)
            except DiscoveryFailed as e:   # garder la dernière époque valide (marquée stale si vieille)
                log("session_check_failed", why=str(e))
        return self.origin, self.cache["prefix"], self.status()


# --------------------------------------------------------------------------- routage site_id -> fiche courante
class RouteMap:
    """Table bornée site_id (data-id interne stable) -> chemin de fiche de l'époque courante.

    Construite seulement à la demande d'une session utilisateur, à partir de la page de liste validée puis,
    si nécessaire, des pages successives de l'API publique du site (api_films.php). Aucun calcul d'identifiant :
    seul un couple (id, lien) publié par le site et revalidé (même hôte, /<p>/b/<jeton>/<n>) est retenu ;
    un site_id ou un chemin revendiqué deux fois est écarté. Remise à zéro à chaque changement d'époque.
    """

    def __init__(self, resolver, path, max_pages=25, page_size=20, max_entries=5000):
        self.r, self.path = resolver, path
        self.max_pages, self.page_size, self.max_entries = max_pages, page_size, max_entries
        self.lock = threading.Lock()
        self.state = self._load()

    def _fresh(self, key):
        return {"epoch": key, "map": {}, "conflicts": [], "listing_done": False, "offset": 0, "pages": 0,
                "exhausted": False}

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict) or not isinstance(s.get("epoch"), str) or not isinstance(s.get("map"), dict):
                return None
            tok = s["epoch"].rsplit("/", 1)[-1]
            rx = re.compile(r"^b/%s/[0-9]{1,12}$" % re.escape(tok))
            out = self._fresh(s["epoch"])
            out["map"] = {k: v for k, v in list(s["map"].items())[:self.max_entries]
                          if isinstance(k, str) and NUM_ID_RE.match(k) and isinstance(v, str) and rx.match(v)}
            out["conflicts"] = [x for x in (s.get("conflicts") or []) if isinstance(x, str) and NUM_ID_RE.match(x)][:1000]
            out["listing_done"] = s.get("listing_done") is True
            out["exhausted"] = s.get("exhausted") is True
            for k in ("offset", "pages"):
                v = s.get(k)
                out[k] = v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10 ** 6 else 0
            return out
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save(self):
        try:
            atomic_write_json(self.path, self.state)
        except OSError as e:
            log("route_save_failed", error=type(e).__name__)

    def _rel(self, ep, link):
        if not isinstance(link, str) or len(link) > 300:
            return None
        absu = self.r.same_origin(urllib.parse.urljoin(ep["origin"] + "/", link), ep["host"])
        if not absu:
            return None
        u = urllib.parse.urlsplit(absu)
        s = _segs(u.path)
        if u.query or len(s) != 4 or s[0] != ep["prefix"] or s[1] != "b" or s[2] != ep["token"] \
                or not NUM_ID_RE.match(s[3]):
            return None
        return "/".join(s[1:])

    def _ingest(self, st, pairs):
        m, conf = st["map"], st["conflicts"]
        rev = {v: k for k, v in m.items()}
        for sid, rel in pairs:
            if sid in conf:
                continue
            other = rev.get(rel)
            if other is not None and other != sid:   # une fiche revendiquée par deux ids : les deux écartés
                m.pop(other, None)
                conf.extend(x for x in (other, sid) if x not in conf)
                continue
            old = m.get(sid)
            if old is None:
                if len(m) < self.max_entries:
                    m[sid] = rel
                    rev[rel] = sid
            elif old != rel:
                m.pop(sid, None)
                conf.append(sid)
        del conf[1000:]

    def _get(self, url, deadline, json_body):
        left = deadline - time.monotonic()
        if left <= 0:
            raise DiscoveryFailed("timeout")
        try:
            status, headers, body = self.r.fetch(url, min(self.r.timeout, left))
        except FetchError as e:
            raise DiscoveryFailed("fetch:" + str(e))
        ctype = headers.get("content-type", "").lower()
        if status != 200:   # aucune redirection suivie ici
            raise DiscoveryFailed("route_status_%d" % status)
        if not ctype.startswith("application/json" if json_body else "text/html"):
            raise DiscoveryFailed("route_type")
        return body

    def _listing_step(self, ep, st, deadline):
        st["listing_done"] = True
        c = self.r.cache
        url = ep["origin"] + (c["listing_path"] if c else "/%s/" % ep["prefix"])
        body = self._get(url, deadline, False)
        cards = _Cards(4000)
        try:
            cards.feed(body.decode("utf-8", "replace"))
        except Exception:
            pass
        pairs = [(x["site_id"], self._rel(ep, x["href"])) for x in cards.items if x.get("site_id")]
        self._ingest(st, [(a, b) for a, b in pairs if b])

    def _api_step(self, ep, st, deadline):
        q = urllib.parse.urlencode([("offset", st["offset"]), ("limit", self.page_size), ("folder", ep["prefix"]),
                                    ("pr", ep["token"])])
        body = self._get("%s/%s/api_films.php?%s" % (ep["origin"], ep["prefix"], q), deadline, True)
        st["pages"] += 1
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DiscoveryFailed("route_json")
        films = obj.get("films") if isinstance(obj, dict) else None
        if not isinstance(films, list):
            raise DiscoveryFailed("route_json")
        pairs = []
        for f in films[:self.page_size * 2]:
            if not isinstance(f, dict):
                continue
            sid = f.get("id")
            sid = str(sid) if isinstance(sid, (str, int)) and not isinstance(sid, bool) else ""
            rel = self._rel(ep, f.get("link"))
            if NUM_ID_RE.match(sid) and rel:
                pairs.append((sid, rel))
        self._ingest(st, pairs)
        st["offset"] += len(films)
        if not films or obj.get("hasMore") is not True:
            st["exhausted"] = True

    def ensure(self, ep, wanted, deadline, max_steps=None):
        """Complète la table pour `wanted` (ensemble de site_id) ; borne pages/temps. -> état ou None."""
        if not ep or not ep.get("token"):
            return None
        if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return None
        try:
            st = self.state
            if not st or st["epoch"] != ep["key"]:
                st = self.state = self._fresh(ep["key"])
                log("route_reset", epoch=ep["key"])
            steps, dirty = 0, False
            while max_steps is None or steps < max_steps:
                missing = [w for w in wanted if w not in st["map"] and w not in st["conflicts"]]
                if not missing or st["exhausted"] or time.monotonic() >= deadline:
                    break
                steps, dirty = steps + 1, True
                step = "listing" if not st["listing_done"] else "api"
                try:
                    if step == "listing":
                        self._listing_step(ep, st, deadline)
                    elif st["pages"] >= self.max_pages:
                        st["exhausted"] = True
                        log("route_page_cap", pages=st["pages"])
                    else:
                        self._api_step(ep, st, deadline)
                except DiscoveryFailed as e:
                    why = str(e)
                    transient = why == "timeout" or why.startswith("fetch:") or why.startswith("route_status_5")
                    log("route_fetch_failed", step=step, why=why, transient=transient)
                    if transient:
                        break   # réessai à la prochaine demande utilisateur / tick du moniteur
                    if step == "api":
                        st["exhausted"] = True   # API changée/refusée : conclusif (Page indisponible)
            if dirty:
                self._save()
            return st
        finally:
            self.lock.release()

    def peek(self, ep, sid):
        """Sans réseau : ('found', chemin) | ('absent', None) | ('unknown', None)."""
        st = self.state
        if not ep or not st or st["epoch"] != ep.get("key"):
            return "unknown", None
        if sid in st["conflicts"]:
            return "absent", None
        if sid in st["map"]:
            return "found", st["map"][sid]
        return ("absent" if st["exhausted"] else "unknown"), None

    def resolve(self, ep, sid, deadline):
        st = self.ensure(ep, {sid}, deadline)
        if st is None:
            return "unknown", None
        return self.peek(ep, sid)


# --------------------------------------------------------------------------- catalogue (métadonnées)
class _Cards(html.parser.HTMLParser):
    """Lit les cartes <a href="/<p>/b/<jeton>/<id>"> : titre (alt/titre), affiche https, année, genre et
    identifiant interne stable (premier data-id numérique imbriqué, ex. bouton synopsis)."""

    def __init__(self, cap):
        super().__init__(convert_charrefs=True)
        self.items, self.cap, self.cur, self.stack = [], cap, None, []

    @property
    def field(self):
        return self.stack[-1] if self.stack else None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and a.get("href") and len(self.items) < self.cap:
            self.cur = {"href": a["href"], "title": "", "poster": "", "year": "", "cat": "", "site_id": None}
            self.items.append(self.cur)
            self.stack = []
        elif self.cur is not None:
            did = a.get("data-id")
            if self.cur["site_id"] is None and isinstance(did, str) and NUM_ID_RE.match(did.strip()):
                self.cur["site_id"] = did.strip()
            if tag == "img" and not self.cur["poster"]:
                self.cur["poster"] = a.get("src") or ""
                self.cur["title"] = self.cur["title"] or (a.get("alt") or "")
            elif tag in ("span", "div"):   # champ hérité du parent (date imbriquée dans la catégorie)
                f = self.field
                if "card-date" in cls:
                    f = "year"
                elif "card-title" in cls:
                    f = "title"
                elif "card-cat" in cls:
                    f = "cat"
                self.stack.append(f)

    def handle_endtag(self, tag):
        if tag == "a":
            self.cur, self.stack = None, []
        elif tag in ("span", "div") and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.cur is not None and self.field:
            if self.field == "title":
                self.cur["title"] = data.strip() or self.cur["title"]
            else:
                self.cur[self.field] += data


def parse_listing(html_text, page_url, resolver, detail_re, cap=500):
    """Page de liste -> éléments de catalogue dédupliqués par identifiant de fiche (métadonnées seules)."""
    p = _Cards(cap * 4)
    try:
        p.feed(html_text)
    except Exception:
        pass
    out, seen = [], set()
    for c in p.items:
        absu = resolver.same_origin(urllib.parse.urljoin(page_url, c["href"]))
        if not absu:
            continue
        segs = [s for s in urllib.parse.urlsplit(absu).path.split("/") if s]
        if len(segs) < 2 or not PREFIX_RE.match(segs[0]):
            continue
        rel = "/".join(segs[1:])
        if not detail_re.match(rel) or rel in seen:
            continue
        seen.add(rel)
        cat = re.sub(r"\s+", " ", c["cat"]).strip()
        genre = cat.split("·", 1)[1].strip() if "·" in cat else ""
        year = re.search(r"\b(19|20)\d{2}\b", c["year"] or cat)
        out.append({
            "source": resolver.host, "source_id": segs[-1], "detail_path": rel, "site_id": c.get("site_id"),
            "title": re.sub(r"\s+", " ", c["title"]).strip()[:200],
            "year": int(year.group(0)) if year else None, "genre": genre[:80] or None,
            "poster": c["poster"] if c["poster"].startswith("https://") else None,
        })
        if len(out) >= cap:
            break
    return out


def clean_item(x, detail_re):
    """Valide un élément de catalogue.json (produit par la tâche de métadonnées) ; None si refusé."""
    if not isinstance(x, dict):
        return None
    rel = x.get("detail_path")
    if not rel and isinstance(x.get("detail_url"), str):   # format de référence : URL complète
        segs = [s for s in urllib.parse.urlsplit(x["detail_url"]).path.split("/") if s]
        rel = "/".join(segs[1:]) if len(segs) > 1 and PREFIX_RE.match(segs[0]) else None
    if not isinstance(rel, str) or not detail_re.match(rel) or not isinstance(x.get("title"), str):
        return None
    def s(k, n):
        v = x.get(k)
        return v[:n] if isinstance(v, str) and v.strip() else None
    def num(k, lo, hi, t):
        v = x.get(k)
        return v if isinstance(v, t) and not isinstance(v, bool) and lo <= v <= hi else None
    poster = x.get("poster")
    sid = x.get("site_id")
    sid = str(sid) if isinstance(sid, (str, int)) and not isinstance(sid, bool) else ""
    return {
        "source_id": str(x.get("source_id") or rel.split("/")[-1])[:64], "detail_path": rel,
        "site_id": sid if NUM_ID_RE.match(sid) else None,
        "title": x["title"].strip()[:200], "year": num("year", 1880, 2100, int), "genre": s("genre", 80),
        "description": s("description", 1200),
        "poster": poster if isinstance(poster, str) and poster.startswith("https://") and len(poster) < 600 else None,
        "imdb_id": x["imdb_id"] if isinstance(x.get("imdb_id"), str) and re.match(r"^tt\d{5,10}$", x["imdb_id"]) else None,
        "imdb_rating": num("imdb_rating", 0, 10, (int, float)), "imdb_votes": num("imdb_votes", 0, 10 ** 9, int),
        "match_status": s("match_status", 16), "rating_updated_at": s("rating_updated_at", 40),
    }


def load_catalogue_meta(path, detail_re, cap=2000):
    """-> (éléments, generated_at, hôte source ou None). Dédoublonne par site_id puis par chemin de fiche."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return [], None, None
    items = raw.get("items") if isinstance(raw, dict) else raw
    out, seen = [], set()
    for x in (items if isinstance(items, list) else [])[:cap * 2]:
        c = clean_item(x, detail_re)
        if not c or c["detail_path"] in seen or (c["site_id"] and ("s", c["site_id"]) in seen):
            continue
        seen.add(c["detail_path"])
        if c["site_id"]:
            seen.add(("s", c["site_id"]))
        out.append(c)
    src = raw.get("source") if isinstance(raw, dict) else None
    return (out[:cap], (raw.get("generated_at") if isinstance(raw, dict) else None),
            src if valid_public_host(src) else None)


def load_catalogue(path, detail_re, cap=2000):
    items, gen, _ = load_catalogue_meta(path, detail_re, cap)
    return items, gen


def backfill_site_ids(cat, store, default_host):
    """Ajoute site_id aux éléments d'un catalogue hérité à partir du magasin brut (current.json), par
    correspondance exacte link_id == dernier segment de detail_path, même hôte. Hors ligne ; -> compteurs."""
    if not isinstance(cat, dict) or not isinstance(cat.get("items"), list) or not isinstance(store, dict):
        raise ValueError("format")
    host = cat.get("source") if valid_public_host(cat.get("source")) else default_host
    shost = (urllib.parse.urlsplit(store.get("origin") or "").hostname or "").lower()
    if shost != host:
        raise ValueError("store_host_mismatch")
    sitems = store.get("catalog", {}).get("items") if isinstance(store.get("catalog"), dict) else store.get("items")
    by_link, dup = {}, set()
    for x in sitems if isinstance(sitems, list) else []:
        if not isinstance(x, dict):
            continue
        link, sid = str(x.get("link_id") or ""), str(x.get("site_id") or "")
        if not (NUM_ID_RE.match(link) and NUM_ID_RE.match(sid)):
            continue
        if by_link.get(link, sid) != sid:
            dup.add(link)
        by_link[link] = sid
    n = {"filled": 0, "already": 0, "missing": 0, "ambiguous": 0}
    for it in cat["items"]:
        if not isinstance(it, dict):
            continue
        if NUM_ID_RE.match(str(it.get("site_id") or "")):
            n["already"] += 1
            continue
        link = str(it.get("detail_path") or "").rsplit("/", 1)[-1]
        if link in dup:
            n["ambiguous"] += 1
        elif link in by_link:
            it["site_id"] = by_link[link]
            n["filled"] += 1
        else:
            n["missing"] += 1
    return n


def atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- affichage : CDP par tube
class CDPError(Exception):
    pass


class CDP:
    """Chrome DevTools Protocol sur --remote-debugging-pipe (fd 3 = commandes, fd 4 = réponses)."""

    def __init__(self, wfd, rfd, on_event=None):
        self.w, self.r = os.fdopen(wfd, "wb", buffering=0), os.fdopen(rfd, "rb", buffering=0)
        self.n, self.lock, self.pending, self.on_event = 0, threading.Lock(), {}, on_event
        self.closed = False
        threading.Thread(target=self._reader, daemon=True, name="cdp-reader").start()

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
                    if "id" in msg and msg["id"] in self.pending:
                        slot = self.pending[msg["id"]]
                        slot[1] = msg
                        slot[0].set()
                    elif "method" in msg and self.on_event:
                        try:
                            self.on_event(msg)
                        except Exception as e:
                            log("cdp_event_error", error=type(e).__name__)
                if len(buf) > 8 * MAX_BODY:
                    break
        except (OSError, ValueError):
            pass
        self.closed = True
        for slot in list(self.pending.values()):
            slot[0].set()

    def send(self, method, params=None, session=None, timeout=5.0):
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
            except OSError:
                self.pending.pop(mid, None)
                raise CDPError("write")
        ok = slot[0].wait(timeout)
        self.pending.pop(mid, None)
        if not ok or slot[1] is None:
            raise CDPError("timeout:" + method if not ok else "closed")
        if "error" in slot[1]:
            raise CDPError(method + ":" + str(slot[1]["error"].get("message", ""))[:120])
        return slot[1].get("result", {})

    def close(self):
        self.closed = True
        for f in (self.w, self.r):
            try:
                f.close()
            except OSError:
                pass


KEYMAP = {"up": ("ArrowUp", 38), "down": ("ArrowDown", 40), "left": ("ArrowLeft", 37),
          "right": ("ArrowRight", 39), "ok": ("Enter", 13)}


class ChromiumKiosk:
    """Compositeur Wayland (optionnel) + Chromium kiosque, profil temporaire, piloté par CDP."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.comp = self.browser = self.cdp = None
        self.session = self.target = None
        self.profile = None
        self.closed = threading.Event()
        self.lock = threading.Lock()

    def _spawn(self, argv, env, pass_fds=()):
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=pass_fds,
                                close_fds=True)

    def _env(self):
        keep = ("HOME", "USER", "LANG", "LC_ALL", "PATH", "XDG_RUNTIME_DIR", "TZ")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        return env

    def open(self, url):
        cfg = self.cfg
        self.profile = tempfile.mkdtemp(prefix="opentv-bridge-", dir=cfg.runtime_dir)
        env = self._env()
        if cfg.compositor_cmd.strip():
            sock = "opentv-bridge-%d" % os.getpid()
            ini = os.path.join(self.profile, "weston.ini")
            with open(ini, "w") as f:
                f.write(WESTON_INI.format(name=cfg.output_name, mode=cfg.output_mode))
            argv = [a.format(socket=sock, config=ini) for a in shlex.split(cfg.compositor_cmd)]
            self.comp = self._spawn(argv, env)
            path = os.path.join(cfg.runtime_dir, sock)
            end = time.monotonic() + 15
            while not os.path.exists(path):
                if self.closed.is_set() or self.comp.poll() is not None or time.monotonic() > end:
                    raise CDPError("compositor_failed")
                time.sleep(0.1)
            env["WAYLAND_DISPLAY"] = sock
        if self.closed.is_set():
            raise CDPError("cancelled")
        # Chromium exige ses tubes CDP sur fd 3/4. Réserver ces numéros avant os.pipe : sur un
        # processus Linux minimal ils sont libres, c_r devient alors 3 et l'ancien shell faisait
        # `3<&3 3<&-`, fermant aussitôt le tube. Les tests macOS avaient d'autres fd déjà ouverts.
        reserved = []
        try:
            for target in (3, 4):
                try:
                    os.fstat(target)
                except OSError:
                    fd = os.open(os.devnull, os.O_RDWR)
                    if fd != target:
                        os.close(fd)
                        raise CDPError("fd_reserve")
                    reserved.append(fd)
            c_r, p_w = os.pipe()
            p_r, c_w = os.pipe()
            argv = ["/bin/sh", "-c", 'exec "$0" "$@" 3<&%d 4>&%d %d<&- %d>&-' % (c_r, c_w, c_r, c_w)]
            argv += cfg.browser_cmd + cfg.browser_flags + [
                "--user-data-dir=" + self.profile, "--remote-debugging-pipe", "about:blank"]
            try:
                self.browser = self._spawn(argv, env, pass_fds=(c_r, c_w))
            finally:
                os.close(c_r)
                os.close(c_w)
        finally:
            for fd in reserved:
                os.close(fd)
        self.cdp = CDP(p_w, p_r, self._event)
        end = time.monotonic() + 20
        while True:   # attendre la première page
            if self.closed.is_set():
                raise CDPError("cancelled")
            try:
                pages = [t for t in self.cdp.send("Target.getTargets", timeout=3)["targetInfos"] if t["type"] == "page"]
            except CDPError:
                pages = []
                if self.browser.poll() is not None:
                    raise CDPError("browser_exited")
            if pages:
                break
            if time.monotonic() > end:
                raise CDPError("no_page")
            time.sleep(0.2)
        self.target = pages[0]["targetId"]
        self.session = self.cdp.send("Target.attachToTarget", {"targetId": self.target, "flatten": True})["sessionId"]
        try:   # pas de téléchargement ; fenêtres surgissantes refermées (_event)
            self.cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        except CDPError as e:
            log("cdp_warn", what="download_deny", error=str(e))
        self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        self.cdp.send("Page.enable", session=self.session)
        self.navigate(url)

    def _event(self, msg):
        if msg.get("method") == "Target.targetCreated":
            info = msg["params"]["targetInfo"]
            if info.get("type") == "page" and self.target and info.get("targetId") != self.target:
                threading.Thread(target=self._close_target, args=(info["targetId"],), daemon=True).start()

    def _close_target(self, tid):
        try:
            self.cdp.send("Target.closeTarget", {"targetId": tid}, timeout=3)
            log("popup_closed")
        except CDPError:
            pass

    def navigate(self, url):
        r = self.cdp.send("Page.navigate", {"url": url}, session=self.session, timeout=5)
        if r.get("errorText"):
            raise CDPError("navigate:" + r["errorText"][:80])

    def key(self, k):
        name, code = KEYMAP[k]
        base = {"key": name, "code": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        down = dict(base, type="keyDown")
        if k == "ok":
            down["text"] = "\r"
        self.cdp.send("Input.dispatchKeyEvent", down, session=self.session)
        self.cdp.send("Input.dispatchKeyEvent", dict(base, type="keyUp"), session=self.session)

    def push(self, view):
        expr = "window.__opentv&&window.__opentv.update(%s)" % json.dumps(view).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        self.cdp.send("Runtime.evaluate", {"expression": expr}, session=self.session, timeout=3)

    def evaluate(self, expr):   # tests / diagnostic local uniquement
        r = self.cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=self.session)
        return r.get("result", {}).get("value")

    def alive(self):
        if self.browser is None or self.browser.poll() is not None or (self.cdp and self.cdp.closed):
            return False
        return self.comp is None or self.comp.poll() is None

    def close(self):
        self.closed.set()
        with self.lock:
            if self.cdp and not self.cdp.closed:
                try:
                    self.cdp.send("Browser.close", timeout=1.5)
                except CDPError:
                    pass
            for p in (self.browser, self.comp):   # navigateur d'abord, puis compositeur
                if p is None:
                    continue
                for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
                    if p.poll() is not None:
                        break
                    try:
                        os.killpg(p.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        p.wait(wait)
                    except subprocess.TimeoutExpired:
                        pass
            if self.cdp:
                self.cdp.close()
            if self.profile:
                shutil.rmtree(self.profile, ignore_errors=True)
                self.profile = None


class NullDisplay:
    """Aucun affichage (installation à blanc / tests) : enregistre les appels."""

    def __init__(self, cfg=None):
        self.calls, self._alive, self.fail_open, self.closed = [], True, False, threading.Event()

    def open(self, url):
        self.calls.append(("open", url))
        if self.fail_open:
            raise CDPError("forced")

    def navigate(self, url):
        self.calls.append(("navigate", url))

    def key(self, k):
        self.calls.append(("key", k))

    def push(self, view):
        self.calls.append(("push", view.get("cursor")))

    def alive(self):
        return self._alive

    def close(self):
        self.closed.set()
        self.calls.append(("close",))


# --------------------------------------------------------------------------- sessions
class ApiError(Exception):
    def __init__(self, code, error):
        super().__init__(error)
        self.code, self.error = code, error


def fill_routes(router, ep, wanted, deadline, alive=lambda: True):
    """Complète la table de routage pas à pas (verrou relâché entre deux requêtes) jusqu'à couverture,
    épuisement, échec transitoire (aucun progrès), échéance ou alive() faux. -> état ou None."""
    st = None
    while wanted and alive() and time.monotonic() < deadline:
        before = (st["listing_done"], st["pages"], st["exhausted"]) if st else None
        st = router.ensure(ep, wanted, deadline, max_steps=1)
        if st is None or st["exhausted"] or all(w in st["map"] or w in st["conflicts"] for w in wanted):
            break
        if before == (st["listing_done"], st["pages"], st["exhausted"]):
            break   # échec transitoire : pas de nouvelle tentative immédiate
    return st


class Controller:
    def __init__(self, cfg, resolver, display_factory, clock=time.monotonic, router=None):
        self.cfg, self.resolver, self.factory, self.clock = cfg, resolver, display_factory, clock
        self.router = router
        self.lock = threading.RLock()
        self.td_cv, self.td_n = threading.Condition(), 0   # fermetures en cours (pas d'ouverture pendant)
        self.s = None
        self.items, self.generated_at, self.catalogue_host = load_catalogue_meta(cfg.catalogue_file, cfg.detail_re)
        self.message = ""

    # -- routage : un élément du catalogue est-il encore adressable par son chemin enregistré ?
    def _direct_ok(self, item, ep):
        host = self.catalogue_host or getattr(self.resolver, "anchor", None)
        return bool(ep) and host == ep["host"] and item["detail_path"].split("/")[1] == ep.get("token")

    def route_wanted(self, ep):
        """site_id à remapper : éléments enregistrés sous un autre hôte/jeton que l'époque courante."""
        if not ep or not ep.get("token"):
            return set()
        with self.lock:
            return {it["site_id"] for it in self.items if it.get("site_id") and not self._direct_ok(it, ep)}

    # -- vue locale (servie au navigateur kiosque sur 127.0.0.1)
    def ui_url(self):
        return "http://127.0.0.1:%d/ui/" % self.cfg.ui_port

    def view(self):
        with self.lock:
            s = self.s
            return {"state": s["state"] if s else "dashboard", "cursor": s["cursor"] if s else 0,
                    "cols": self.cfg.cols, "count": len(self.items), "message": self.message,
                    "prefix": self.resolver.status(), "generated_at": self.generated_at}

    def _push(self, s):
        if s["display"] is not None and s["state"] == "catalogue":
            try:
                s["display"].push(self.view())
            except Exception as e:
                log("push_failed", error=str(e)[:80])

    # -- API
    def open(self, app):
        if not APP_RE.match(app or "") or app not in self.cfg.apps:
            raise ApiError(404, "unknown_app")
        if self.factory is None:   # DISPLAY_BACKEND=off : API vivante, affichage non installé/validé
            raise ApiError(503, "display_unavailable")
        if not self.cfg.source_approved:
            raise ApiError(503, "source_unapproved")
        with self.lock:
            s = self.s
            if s and s["app"] == app:
                s["seen"] = self.clock()
                return s
            if s:
                raise ApiError(409, "busy")
        with self.td_cv:
            if not self.td_cv.wait_for(lambda: self.td_n == 0, timeout=4):
                raise ApiError(409, "busy")
        try:
            self.resolver.current(budget=4.0)
        except DiscoveryFailed:
            raise ApiError(503, "discovery_failed")
        with self.lock:
            if self.s:   # course entre deux ouvertures
                if self.s["app"] == app:
                    return self.s
                raise ApiError(409, "busy")
            self.items, self.generated_at, self.catalogue_host = load_catalogue_meta(
                self.cfg.catalogue_file, self.cfg.detail_re)
            now = self.clock()
            s = {"id": secrets.token_urlsafe(18), "app": app, "state": "starting", "cursor": 0,
                 "opened": now, "seen": now, "key_at": now, "player_at": None, "display": None,
                 "prep": threading.Event(), "opening": False}
            self.s, self.message = s, ""
            log("session_open", app=app, session=s["id"][:6], items=len(self.items), prefix=self.resolver.status())
        threading.Thread(target=self._prepare, args=(s,), daemon=True, name="route-prep").start()
        threading.Thread(target=self._start, args=(s,), daemon=True, name="display-start").start()
        return s

    def _prepare(self, s):
        """Déclenché par l'ouverture utilisateur : revalide l'adresse (rotation/migration) puis remappe les
        site_id du catalogue vers les fiches de l'époque courante, borné en temps et en pages."""
        try:
            try:
                self.resolver.current(budget=6.0, check=True)
            except DiscoveryFailed:
                pass
            if self.router is not None:
                ep = self.resolver.epoch()
                fill_routes(self.router, ep, self.route_wanted(ep), time.monotonic() + self.cfg.route_budget_s,
                            alive=lambda: self.s is s)
        except Exception as e:
            log("route_prep_error", error=type(e).__name__)
        finally:
            s["prep"].set()

    def _start(self, s):
        d = self.factory(self.cfg)
        with self.lock:
            if self.s is not s:
                return
            s["display"] = d
        try:
            d.open(self.ui_url())
        except Exception as e:
            log("display_open_failed", error=str(e)[:120])
            self.end(s, "display-failed")
            return
        with self.lock:
            if self.s is s and s["state"] == "starting":
                s["state"] = "catalogue"
                log("state", state="catalogue")
                self._push(s)

    def key(self, sid, k):
        if k not in KEYS:
            raise ApiError(400, "bad_key")
        with self.lock:
            s = self.s
            if not s or s["id"] != sid:
                raise ApiError(410, "no_session")
            now = self.clock()
            s["seen"] = s["key_at"] = now
            st = s["state"]
            if st == "player":
                if k == "back":
                    return self._to_catalogue(s)
                try:
                    s["display"].key(k)
                except Exception as e:
                    log("key_relay_failed", error=str(e)[:80])
                return st
            if k == "back":
                self._end_locked(s, "back")
                return "dashboard"
            if k == "ok":
                if st != "catalogue" or not self.items or s["opening"]:
                    return st
                s["opening"] = True
                item = self.items[min(s["cursor"], len(self.items) - 1)]
            else:
                n, cols, c = len(self.items), max(1, self.cfg.cols), s["cursor"]
                if n:
                    if k == "left" and c % cols:
                        c -= 1
                    elif k == "right" and c + 1 < n and (c + 1) % cols:
                        c += 1
                    elif k == "up" and c >= cols:
                        c -= cols
                    elif k == "down" and c + cols < n:
                        c += cols
                    elif k == "down" and c // cols < (n - 1) // cols:   # dernière rangée incomplète
                        c = n - 1
                s["cursor"] = c
                self._push(s)
                return s["state"]
        # OK : résolution (réseau borné possible) hors verrou, /v1/health reste réactif pendant ce temps
        return self._open_item(s, item)

    def detail_url(self, item, deadline=None):
        """URL de fiche reconstruite : époque validée + chemin exact (catalogue même époque, ou table de
        routage site_id -> fiche publiée par le site). Jamais de calcul d'identifiant ni d'URL fournie."""
        deadline = deadline or time.monotonic() + 3.0
        self.resolver.current(budget=max(0.5, deadline - time.monotonic()))
        ep = self.resolver.epoch()
        if not ep or not ep.get("token"):
            raise ApiError(503, "no_epoch")
        origin, prefix, token = ep["origin"], ep["prefix"], ep["token"]
        direct, sid, rel = self._direct_ok(item, ep), item.get("site_id"), None
        if sid and self.router is not None:
            kind, found = (self.router.peek(ep, sid) if direct
                           else self.router.resolve(ep, sid, deadline))
            if kind == "found":
                rel = found
            elif kind == "absent":
                raise ApiError(404, "unmapped")   # retiré du site ou ambigu : Page indisponible
        if rel is None:
            if not direct:
                raise ApiError(404, "unmapped")
            rel = item["detail_path"]
        url = "%s/%s/%s" % (origin, prefix, rel)
        # revérification indépendante : même origine, sous /<préfixe>/, chemin de fiche du jeton courant
        u = urllib.parse.urlsplit(url)
        tail = u.path[len(prefix) + 2:]
        if (u.scheme + "://" + u.netloc) != origin or not u.path.startswith("/%s/" % prefix) \
                or not self.cfg.detail_re.match(tail) or not re.match(r"^b/%s/[0-9]{1,12}$" % re.escape(token), tail) \
                or u.query or u.fragment:
            raise ApiError(500, "bad_url")
        return url

    def _open_item(self, s, item):
        try:
            if not s["prep"].wait(2.5):   # revalidation de session encore en cours : borne l'attente
                log("route_prep_pending")
            url = self.detail_url(item, time.monotonic() + 2.5)
        except (DiscoveryFailed, ApiError) as e:
            url, err = None, e
        with self.lock:
            s["opening"] = False
            if self.s is not s:
                return "dashboard"
            if s["state"] != "catalogue":
                return s["state"]
            if url is not None:
                try:
                    s["display"].navigate(url)
                except (CDPError, AttributeError) as e:
                    url, err = None, e
            if url is None:
                self.message = "Page indisponible"
                log("player_open_failed", item=item["source_id"], site_id=item.get("site_id"), error=str(err)[:80])
                self._push(s)
                return s["state"]
            s["state"], s["player_at"], self.message = "player", self.clock(), ""
            log("state", state="player", item=item["source_id"], site_id=item.get("site_id"))
            return "player"

    def _to_catalogue(self, s):
        try:
            s["display"].navigate(self.ui_url())
        except Exception as e:
            log("catalogue_return_failed", error=str(e)[:80])
            self._end_locked(s, "return-failed")
            return "dashboard"
        s["state"], s["player_at"], s["key_at"] = "catalogue", None, self.clock()
        log("state", state="catalogue")
        return "catalogue"

    def close(self, sid=None, app=None, reason="other"):
        reason = reason if REASON_RE.match(reason or "") else "other"
        with self.lock:
            s = self.s
            if s and ((sid and s["id"] == sid) or (not sid and app and s["app"] == app)):
                self._end_locked(s, "tv-" + reason[:21])
        return "dashboard"

    def health(self, sid=None):
        with self.lock:
            s = self.s
            if s and sid and s["id"] == sid:
                s["seen"] = self.clock()
            return (s["state"], s["id"]) if s else ("dashboard", None)

    def end(self, s, reason):
        with self.lock:
            if self.s is s:
                self._end_locked(s, reason)

    def _end_locked(self, s, reason):
        self.s = None
        log("session_end", reason=reason, state=s["state"], seconds=round(self.clock() - s["opened"], 1))
        with self.td_cv:
            self.td_n += 1
        threading.Thread(target=self._teardown, args=(s, reason), daemon=True, name="teardown").start()

    def _teardown(self, s, reason):
        try:
            d = s["display"]
            if d is not None:
                d.close()
            self.check_dashboard()
        finally:
            with self.td_cv:
                self.td_n -= 1
                self.td_cv.notify_all()

    def check_dashboard(self):
        unit = self.cfg.dashboard_unit
        if not unit:
            return None
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5)
            ok = r.stdout.strip() == "active"
        except (OSError, subprocess.SubprocessError):
            ok = None
        log("dashboard_check", unit=unit, active=ok)
        return ok

    def tick(self):
        """Chien de garde : appelé chaque seconde."""
        with self.lock:
            s = self.s
            if not s:
                return
            now, st, cfg = self.clock(), s["state"], self.cfg
            why = None
            if now - s["seen"] > cfg.heartbeat_s:
                why = "tv-lost"
            elif st == "starting" and now - s["opened"] > cfg.start_timeout_s:
                why = "start-timeout"
            elif st in ("catalogue", "player") and s["display"] is not None and not s["display"].alive():
                why = "display-died"
            elif st == "catalogue" and now - s["key_at"] > cfg.catalogue_idle_s:
                why = "idle"
            elif st == "player" and now - s["player_at"] > cfg.player_max_s:
                why = "player-cap"
            if why:
                self._end_locked(s, why)

    def shutdown(self):
        with self.lock:
            if self.s:
                self._end_locked(self.s, "shutdown")
        with self.td_cv:
            self.td_cv.wait_for(lambda: self.td_n == 0, timeout=10)


# --------------------------------------------------------------------------- serveurs HTTP
class RateLimiter:
    def __init__(self, rate, clock=time.monotonic):
        self.rate, self.clock, self.b, self.lock = rate, clock, {}, threading.Lock()

    def allow(self, ip):
        with self.lock:
            now = self.clock()
            tokens, t = self.b.get(ip, (self.rate, now))
            tokens = min(self.rate, tokens + (now - t) * self.rate)
            if tokens < 1:
                self.b[ip] = (tokens, now)
                return False
            self.b[ip] = (tokens - 1, now)
            return True


def make_api_handler(ctl, cfg, limiter):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "opentv-bridge/1"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _send(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self, method):
            ip = self.client_address[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            if ip not in cfg.allow_ips:
                log("forbidden", ip=ip)
                return self._send(403, {"ok": False, "error": "forbidden"})
            if not limiter.allow(ip):
                return self._send(429, {"ok": False, "error": "rate_limited"})
            u = urllib.parse.urlsplit(self.path)
            try:
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, max_num_fields=8).items()}
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad_query"})
            route = (method, u.path)
            try:
                if route == ("POST", "/v1/session"):
                    s = ctl.open(q.get("app", ""))
                    return self._send(200, {"ok": True, "session": s["id"], "state": s["state"]})
                if route == ("POST", "/v1/key"):
                    st = ctl.key(q.get("session", ""), q.get("k", ""))
                    return self._send(200, {"ok": True, "state": st})
                if route == ("POST", "/v1/close"):
                    st = ctl.close(q.get("session") or None, q.get("app") or None, q.get("reason", "other"))
                    return self._send(200, {"ok": True, "state": st})
                if route == ("GET", "/v1/health"):
                    st, sid = ctl.health(q.get("session") or None)
                    return self._send(200, {"ok": True, "api": API, "state": st, "session": sid})
            except ApiError as e:
                return self._send(e.code, {"ok": False, "error": e.error})
            except Exception as e:
                log("api_error", error=type(e).__name__)
                return self._send(500, {"ok": False, "error": "internal"})
            known = {"/v1/session", "/v1/key", "/v1/close", "/v1/health"}
            if u.path in known:
                return self._send(405, {"ok": False, "error": "method_not_allowed"})
            return self._send(404, {"ok": False, "error": "not_found"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def make_ui_handler(ctl, ui_file):
    class U(http.server.BaseHTTPRequestHandler):
        server_version = "opentv-bridge-ui/1"
        sys_version = ""

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self.send_error(403)
                return
            p = urllib.parse.urlsplit(self.path).path
            if p == "/ui/":
                with open(ui_file, "rb") as f:
                    raw, ctype = f.read(), "text/html; charset=utf-8"
            elif p == "/ui/catalogue.json":
                raw, ctype = json.dumps({"items": ctl.items}).encode(), "application/json"
            elif p == "/ui/state.json":
                raw, ctype = json.dumps(ctl.view()).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src https: data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return U


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16


# --------------------------------------------------------------------------- catalogue (commande manuelle)
def refresh_catalogue(cfg, resolver):
    """Rafraîchit préfixe + catalogue (page de liste) ; garde l'ancien fichier en cas d'échec."""
    c = resolver.refresh()
    ep = resolver.epoch()
    url = ep["origin"] + c["listing_path"]
    status, headers, body = resolver.fetch(url, cfg.fetch_timeout_s * 2)
    if status != 200 or not headers.get("content-type", "").startswith("text/html"):
        raise DiscoveryFailed("listing_status_%d" % status)
    detail_re = re.compile(r"^b/%s/[0-9]{1,12}$" % re.escape(ep["token"]))
    items = parse_listing(body.decode("utf-8", "replace"), url, resolver, detail_re)
    if not items:
        raise DiscoveryFailed("listing_empty")
    old, _ = load_catalogue(cfg.catalogue_file, cfg.detail_re)
    by_sid = {x["site_id"]: x for x in old if x.get("site_id")}
    by_path = {x["detail_path"]: x for x in old}
    for it in items:   # conserve les enrichissements (description, IMDb) : site_id stable d'abord
        prev = by_sid.get(it.get("site_id")) or by_path.get(it["detail_path"]) or {}
        for k, v in prev.items():
            if it.get(k) is None and v is not None and k not in ("detail_path", "source_id"):
                it[k] = v
    atomic_write_json(cfg.catalogue_file, {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           "source": ep["host"], "count": len(items), "items": items})
    log("catalogue_ok", count=len(items))
    return items


# --------------------------------------------------------------------------- moniteur d'adresse/routage
class RoutingMonitor:
    """Surveillance supervisée (thread du service, pas de cron) de l'adresse du site.

    Toutes les ROUTING_REFRESH_S : revalide l'époque à partir de la racine actuellement de confiance
    (racine, préfixe, page de liste : 3 GET bornés), adopte une migration HTTPS annoncée et validée, puis
    complète la table site_id -> fiche seulement pour les éléments du catalogue qui en ont besoin.
    Vol unique (aucun chevauchement, ni avec une ouverture de session), délais bornés, repli exponentiel
    après échec, dernière époque valide conservée (fail-closed : jamais d'adoption partielle).
    Ne touche ni au catalogue ni aux notes IMDb.
    """

    def __init__(self, resolver, router, wanted_fn, interval, budget=20.0, route_budget=30.0, max_backoff=3):
        self.resolver, self.router, self.wanted_fn = resolver, router, wanted_fn
        self.interval, self.budget, self.route_budget, self.max_backoff = interval, budget, route_budget, max_backoff
        self.fails, self.runs = 0, 0
        self.flight = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def delay(self):
        return self.interval * (2 ** min(self.fails, self.max_backoff))

    def run_once(self):
        """-> 'unchanged' | 'changed' | 'failed' | 'busy'."""
        if not self.flight.acquire(blocking=False):
            log("routing_check_skipped", why="overlap")
            return "busy"
        try:
            self.runs += 1
            before = self.resolver.epoch()
            t0 = time.monotonic()
            try:
                c = self.resolver.refresh(budget=self.budget, blocking=False)
            except DiscoveryFailed as e:
                self.fails += 1
                log("routing_check_failed", why=str(e)[:80], fails=self.fails, kept=before and before["key"],
                    next_s=round(self.delay()))
                return "failed"
            if c is None:
                log("routing_check_skipped", why="resolver_busy")
                return "busy"
            after = self.resolver.epoch()
            changed = not before or before["key"] != after["key"]
            routes = None
            if self.router is not None:
                wanted = self.wanted_fn(after)
                if wanted:
                    st = fill_routes(self.router, after, wanted, time.monotonic() + self.route_budget,
                                     alive=lambda: not self.stop.is_set())
                    routes = {"wanted": len(wanted), "mapped": len([w for w in wanted if st and w in st["map"]]),
                              "exhausted": bool(st and st["exhausted"])}
            self.fails = 0
            log("routing_check_ok", changed=changed, epoch=after["key"], prev=before and before["key"],
                routes=routes, seconds=round(time.monotonic() - t0, 2))
            return "changed" if changed else "unchanged"
        finally:
            self.flight.release()

    def _loop(self):
        while not self.stop.wait(self.delay()):
            try:
                self.run_once()
            except Exception as e:   # le moniteur ne doit jamais tuer le service
                self.fails += 1
                log("routing_monitor_error", error=type(e).__name__)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="routing-monitor")
        self.thread.start()
        log("routing_monitor_start", interval_s=self.interval)

    def shutdown(self, timeout=5.0):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout)


def serve(cfg):
    os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    resolver = make_resolver(cfg)
    router = RouteMap(resolver, os.path.join(cfg.state_dir, "routes.json"), max_pages=cfg.route_max_pages)
    factory = {"chromium": ChromiumKiosk, "null": NullDisplay, "off": None}[cfg.display]
    ctl = Controller(cfg, resolver, factory, router=router)
    monitor = None
    if cfg.source_approved and cfg.routing_refresh_s > 0:
        monitor = RoutingMonitor(resolver, router, ctl.route_wanted, cfg.routing_refresh_s,
                                 route_budget=cfg.route_budget_s)
    api = Server((cfg.bind, cfg.port), make_api_handler(ctl, cfg, RateLimiter(cfg.rate)))
    ui = Server(("127.0.0.1", cfg.ui_port), make_ui_handler(ctl, os.path.join(HERE, "opentv_bridge_ui.html")))
    stop = threading.Event()

    def watchdog():
        while not stop.wait(1.0):
            try:
                ctl.tick()
            except Exception as e:
                log("watchdog_error", error=type(e).__name__)

    def refresher():
        while not stop.is_set():
            try:
                refresh_catalogue(cfg, resolver)
                with ctl.lock:
                    if not ctl.s:
                        ctl.items, ctl.generated_at, ctl.catalogue_host = load_catalogue_meta(
                            cfg.catalogue_file, cfg.detail_re)
                wait = cfg.catalogue_refresh_s
            except (DiscoveryFailed, FetchError, OSError) as e:
                log("catalogue_refresh_failed", error=str(e)[:80])
                wait = 600
            stop.wait(wait)

    def on_term(*_):
        stop.set()
        if monitor is not None:
            monitor.stop.set()
        threading.Thread(target=api.shutdown, daemon=True).start()
        threading.Thread(target=ui.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    for fn in (watchdog, ui.serve_forever) + ((refresher,) if cfg.source_approved and cfg.catalogue_refresh_s > 0 else ()):
        threading.Thread(target=fn, daemon=True).start()
    if monitor is not None:
        monitor.start()
    log("start", port=cfg.port, ui_port=cfg.ui_port, display=cfg.display, allow=sorted(cfg.allow_ips),
        routing_refresh_s=cfg.routing_refresh_s if monitor else 0)
    api.serve_forever(poll_interval=0.5)
    if monitor is not None:
        monitor.shutdown()
    ctl.shutdown()
    log("stop")


def make_resolver(cfg):
    return Resolver(cfg.site_root, os.path.join(cfg.state_dir, "prefix.json"), ttl=cfg.prefix_ttl_s,
                    grace=cfg.prefix_grace_s, timeout=cfg.fetch_timeout_s, session_check=cfg.session_check_s)


def backfill_command(cfg, store_path):
    """Hors ligne : ajoute site_id au catalogue hérité depuis le magasin brut ; copie .bak 0600 conservée."""
    with open(cfg.catalogue_file, encoding="utf-8") as f:
        original = json.load(f)
    with open(store_path, encoding="utf-8") as f:
        store = json.load(f)
    cat = json.loads(json.dumps(original))
    n = backfill_site_ids(cat, store, urllib.parse.urlsplit(cfg.site_root).hostname)
    if n["filled"]:
        atomic_write_json(cfg.catalogue_file + ".bak", original)
        atomic_write_json(cfg.catalogue_file, cat)
    return n


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    cfg = Config()
    if cmd == "serve":
        serve(cfg)
    elif cmd == "refresh-catalogue":
        if not cfg.source_approved:
            raise SystemExit("source non approuvée: définir SOURCE_APPROVED=1 après validation humaine")
        print(len(refresh_catalogue(cfg, make_resolver(cfg))))
    elif cmd == "backfill-site-ids" and len(argv) == 3:
        print(json.dumps(backfill_command(cfg, argv[2]), sort_keys=True))
    else:
        raise SystemExit("usage: opentv_bridge.py [serve|refresh-catalogue|backfill-site-ids <current.json>]")


if __name__ == "__main__":
    main(sys.argv)
